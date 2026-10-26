"""Who imports whom: a file-level import graph for Python, JS/TS, Go and Rust.

edges[src][dst] is the set of names src imports from dst ("*" when the import
does not name anything, e.g. `import pkg.mod` or a side-effect JS import).
Imports that do not resolve to a file in the repo are ignored.
"""

from __future__ import annotations

import ast
import json
import posixpath
import re
from collections import defaultdict

JS_EXTS = [".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".mts", ".cts", ".vue", ".svelte"]


# ---------------------------------------------------------------- python

def _py_module_names(path: str):
    """Possible dotted module names for a .py path ("src/pkg/a.py" -> pkg.a, src.pkg.a)."""
    if not path.endswith(".py"):
        return []
    mod = path[:-3]
    if mod.endswith("/__init__"):
        mod = mod[: -len("/__init__")]
    elif mod == "__init__":
        return []
    parts = mod.split("/")
    names = {".".join(parts)}
    for prefix in ("src", "lib", "python", "app"):
        if parts[0] == prefix and len(parts) > 1:
            names.add(".".join(parts[1:]))
    return sorted(names)


def build_py_index(paths):
    index = {}
    for p in paths:
        for name in _py_module_names(p):
            index.setdefault(name, p)
    return index


def _py_package_of(path: str, index_rev):
    mod = index_rev.get(path, "")
    if path.endswith("/__init__.py"):
        return mod
    return mod.rsplit(".", 1)[0] if "." in mod else ""


def python_imports(path: str, text: str, index: dict, index_rev: dict):
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError):
        return {}
    found = defaultdict(set)
    bound = {}  # local name -> file, for `import pkg.mod as m` / `from . import mod`
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                name = alias.name
                while name:
                    if name in index:
                        found[index[name]].add("*")
                        if alias.asname or name == alias.name:
                            bound[alias.asname or name] = index[name]
                        break
                    name = name.rsplit(".", 1)[0] if "." in name else ""
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                pkg = _py_package_of(path, index_rev)
                parts = pkg.split(".") if pkg else []
                up = node.level - 1
                if up > len(parts):
                    continue
                base_parts = parts[: len(parts) - up] if up else parts
                base = ".".join(base_parts + ([node.module] if node.module else []))
            else:
                base = node.module or ""
            for alias in node.names:
                sub = f"{base}.{alias.name}" if base else alias.name
                if sub in index:
                    found[index[sub]].add("*")
                    bound[alias.asname or alias.name] = index[sub]
                elif base in index:
                    found[index[base]].add(alias.name)
                else:
                    name = base
                    while name:
                        if name in index:
                            found[index[name]].add(alias.name)
                            break
                        name = name.rsplit(".", 1)[0] if "." in name else ""
    if bound:
        # `mod.func(...)` after `from . import mod`: record func as a name imported from mod
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.value.id in bound:
                found[bound[node.value.id]].add(node.attr)
    found.pop(path, None)
    return dict(found)


# ---------------------------------------------------------------- js / ts

_JS_IMPORT_RX = re.compile(
    r"""(?:^|[\s;])(?:import|export)\s+(?:type\s+)?(?P<clause>[\w$*{}\s,]+?)\s+from\s+['"](?P<src>[^'"]+)['"]"""
    r"""|(?:^|[\s;])import\s+['"](?P<side>[^'"]+)['"]"""
    r"""|\brequire\(\s*['"](?P<req>[^'"]+)['"]\s*\)"""
    r"""|\bimport\(\s*['"](?P<dyn>[^'"]+)['"]\s*\)""",
    re.M,
)


def _js_clause_names(clause: str):
    names = set()
    clause = clause.strip()
    m = re.search(r"\{([^}]*)\}", clause)
    if m:
        for part in m.group(1).split(","):
            part = part.strip()
            if not part:
                continue
            part = re.sub(r"^type\s+", "", part)
            names.add(part.split(" as ")[0].strip())
        clause = clause[: m.start()] + clause[m.end():]
    clause = clause.strip().strip(",").strip()
    if clause.startswith("*"):
        names.add("*")
    elif clause:
        names.add("default")
    return names or {"*"}


def load_js_aliases(repo):
    """Path aliases from tsconfig/jsconfig compilerOptions.paths, plus '@/' -> src/ as a guess."""
    aliases = []
    for cfg in ("tsconfig.json", "jsconfig.json"):
        lines = repo.read(cfg)
        if not lines:
            continue
        text = "\n".join(lines)
        text = re.sub(r"(?m)^\s*//.*$", "", text)
        text = re.sub(r",(\s*[}\]])", r"\1", text)
        try:
            data = json.loads(text)
        except ValueError:
            continue
        opts = data.get("compilerOptions") or {}
        base = (opts.get("baseUrl") or ".").strip("./") or ""
        for key, targets in (opts.get("paths") or {}).items():
            if not targets:
                continue
            prefix = key.rstrip("*")
            target = targets[0].rstrip("*").lstrip("./")
            aliases.append((prefix, posixpath.normpath(posixpath.join(base, target)) + ("/" if target.endswith("/") or key.endswith("/*") else "")))
    if not any(a[0] == "@/" for a in aliases):
        aliases.append(("@/", "src/"))
        aliases.append(("~/", "src/"))
    return aliases


def _resolve_js(spec: str, path: str, pathset, aliases):
    if spec.startswith("."):
        base = posixpath.normpath(posixpath.join(posixpath.dirname(path), spec))
    else:
        base = None
        for prefix, target in aliases:
            if spec.startswith(prefix):
                base = posixpath.normpath(target + spec[len(prefix):])
                break
        if base is None:
            return None
    if base.startswith("../"):
        return None
    candidates = [base]
    stem, ext = posixpath.splitext(base)
    if ext in {".js", ".jsx", ".mjs", ".cjs"}:  # TS projects import './x.js' for x.ts
        candidates += [stem + e for e in (".ts", ".tsx", ".mts", ".cts")]
    candidates += [base + e for e in JS_EXTS]
    candidates += [f"{base}/index{e}" for e in JS_EXTS]
    for c in candidates:
        if c in pathset:
            return c
    return None


def js_imports(path, text, pathset, aliases):
    found = defaultdict(set)
    for m in _JS_IMPORT_RX.finditer(text):
        if m.group("src"):
            spec, names = m.group("src"), _js_clause_names(m.group("clause"))
        else:
            spec = m.group("side") or m.group("req") or m.group("dyn")
            names = {"*"}
        target = _resolve_js(spec, path, pathset, aliases)
        if target and target != path:
            found[target] |= names
    return dict(found)


# ---------------------------------------------------------------- go

def go_module(repo):
    lines = repo.read("go.mod") or []
    for line in lines:
        m = re.match(r"^\s*module\s+(\S+)", line)
        if m:
            return m.group(1)
    return None


def go_imports(path, text, module, dirs):
    if not module:
        return {}
    found = defaultdict(set)
    specs = re.findall(r'^\s*(?:import\s+)?(?:[\w.]+\s+)?"([^"]+)"', text, re.M)
    for spec in specs:
        if spec == module:
            d = ""
        elif spec.startswith(module + "/"):
            d = spec[len(module) + 1:]
        else:
            continue
        for f in dirs.get(d, []):
            if f != path:
                found[f].add("*")
    return dict(found)


# ---------------------------------------------------------------- rust

def rust_imports(path, text, pathset):
    found = defaultdict(set)
    d = posixpath.dirname(path)
    name = posixpath.basename(path)
    mod_dir = d if name in {"main.rs", "lib.rs", "mod.rs"} else posixpath.join(d, name[:-3])
    for m in re.finditer(r"^\s*(?:pub(?:\([^)]*\))?\s+)?mod\s+([A-Za-z_]\w*)\s*;", text, re.M):
        for c in (f"{mod_dir}/{m.group(1)}.rs", f"{mod_dir}/{m.group(1)}/mod.rs"):
            c = c.lstrip("/")
            if c in pathset:
                found[c].add("*")
                break
    crate_root = path.split("/src/")[0] + "/src" if "/src/" in path else ("src" if path.startswith("src/") else d)
    for m in re.finditer(r"\buse\s+crate::([A-Za-z_]\w*)(?:::([A-Za-z_]\w*))?", text):
        first, second = m.group(1), m.group(2)
        for c in (f"{crate_root}/{first}/{second}.rs" if second else None,
                  f"{crate_root}/{first}.rs", f"{crate_root}/{first}/mod.rs"):
            if c and c in pathset and c != path:
                found[c].add(second or "*")
                break
    return dict(found)


# ---------------------------------------------------------------- all

def build_graph(repo):
    """Return edges: {src: {dst: set(names)}} over repo.files."""
    paths = list(repo.files)
    pathset = set(repo.paths)
    py_index = build_py_index([p for p in paths if p.endswith(".py")])
    py_rev = {}
    for name, p in py_index.items():
        # prefer the shortest module name (the one with src/ stripped)
        if p not in py_rev or len(name) < len(py_rev[p]):
            py_rev[p] = name
    aliases = load_js_aliases(repo)
    module = go_module(repo)
    go_dirs = defaultdict(list)
    for p in paths:
        if p.endswith(".go") and not p.endswith("_test.go"):
            go_dirs[posixpath.dirname(p)].append(p)
    edges = {}
    for p, sf in repo.files.items():
        if sf.lang == "python":
            e = python_imports(p, sf.text, py_index, py_rev)
        elif sf.lang == "js":
            e = js_imports(p, sf.text, pathset, aliases)
        elif sf.lang == "go":
            e = go_imports(p, sf.text, module, go_dirs)
        elif sf.lang == "rust":
            e = rust_imports(p, sf.text, pathset)
        else:
            e = {}
        edges[p] = {k: v for k, v in e.items() if k in repo.files}
    return edges


def reverse_edges(edges):
    rev = defaultdict(dict)
    for src, dsts in edges.items():
        for dst, names in dsts.items():
            rev[dst][src] = names
    return rev
