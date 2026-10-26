"""Find a repo's entry points: console scripts, package.json bin/main, main.go, src/main.rs..."""

from __future__ import annotations

import json
import posixpath
import re
from dataclasses import dataclass

from .graph import build_py_index


@dataclass
class Entry:
    path: str
    reason: str
    strength: int  # higher = more likely the real front door
    symbol: str = ""  # function the entry calls, when known (console script "pkg.cli:main")


def _section(lines, header_rx):
    """Lines of a TOML/INI section whose header matches header_rx."""
    out, inside = [], False
    for line in lines:
        s = line.strip()
        if s.startswith("["):
            inside = bool(re.match(header_rx, s))
            continue
        if inside:
            out.append(s)
    return out


def _py_target(spec: str, py_index):
    spec = spec.strip().strip("'\"")
    mod, _, func = spec.partition(":")
    mod = mod.strip()
    func = func.strip().split(".")[0].split("[")[0].strip()
    path = py_index.get(mod)
    return path, func


def python_entries(repo):
    py_paths = [p for p in repo.files if p.endswith(".py")]
    py_index = build_py_index(py_paths)
    out = []
    pyproject = repo.read("pyproject.toml") or []
    for header in (r"\[project\.scripts\]", r"\[tool\.poetry\.scripts\]", r"\[project\.gui-scripts\]"):
        for s in _section(pyproject, header):
            m = re.match(r"""^["']?([\w.\-]+)["']?\s*=\s*["']([\w.]+:[\w.]+)["']""", s)
            if m:
                path, func = _py_target(m.group(2), py_index)
                if path:
                    out.append(Entry(path, f"console script `{m.group(1)}` -> `{m.group(2)}` (pyproject.toml)", 100, func))
    setup_cfg = repo.read("setup.cfg") or []
    inside = False
    for line in setup_cfg:
        s = line.strip()
        if s.startswith("["):
            inside = s == "[options.entry_points]"
            continue
        if inside:
            m = re.match(r"^([\w.\-]+)\s*=\s*([\w.]+:[\w.]+)", s)
            if m:
                path, func = _py_target(m.group(2), py_index)
                if path:
                    out.append(Entry(path, f"console script `{m.group(1)}` -> `{m.group(2)}` (setup.cfg)", 95, func))
    setup_py = "\n".join(repo.read("setup.py") or [])
    for m in re.finditer(r"""['"]([\w.\-]+)\s*=\s*([\w.]+:[\w.]+)['"]""", setup_py):
        path, func = _py_target(m.group(2), py_index)
        if path:
            out.append(Entry(path, f"console script `{m.group(1)}` -> `{m.group(2)}` (setup.py)", 95, func))
    for p in py_paths:
        name = posixpath.basename(p)
        depth = p.count("/")
        if name == "__main__.py":
            out.append(Entry(p, "`python -m` entry (`__main__.py`)", 80 - depth))
        elif name == "manage.py" and depth == 0:
            out.append(Entry(p, "Django `manage.py`", 85))
        elif name in {"main.py", "app.py", "server.py", "cli.py", "wsgi.py", "asgi.py", "run.py"} and depth <= 1:
            out.append(Entry(p, f"conventional entry file `{name}`", 60 - 5 * depth))
    for p in py_paths:
        sf = repo.files[p]
        if re.search(r"""^if\s+__name__\s*==\s*['"]__main__['"]""", sf.text, re.M):
            out.append(Entry(p, "has an `if __name__ == \"__main__\"` block", 40 - p.count("/") * 3))
    return out


def js_entries(repo):
    out = []
    for pkg_path in [p for p in repo.paths if posixpath.basename(p) == "package.json" and p.count("/") <= 1]:
        try:
            data = json.loads("\n".join(repo.read(pkg_path) or []) or "{}")
        except ValueError:
            continue
        if not isinstance(data, dict):
            continue
        base = posixpath.dirname(pkg_path)
        depth_penalty = 10 if base else 0

        def add(target, reason, strength):
            if not isinstance(target, str):
                return
            t = posixpath.normpath(posixpath.join(base, target)).lstrip("./") if target else ""
            for cand in _js_source_for(t, repo):
                out.append(Entry(cand, reason, strength - depth_penalty))
                return

        b = data.get("bin")
        if isinstance(b, str):
            add(b, f"`bin` in {pkg_path}", 100)
        elif isinstance(b, dict):
            for name, target in b.items():
                add(target, f"`bin.{name}` in {pkg_path}", 100)
        for key, strength in (("main", 85), ("module", 80), ("source", 88)):
            add(data.get(key), f"`{key}` in {pkg_path}", strength)
        exp = data.get("exports")
        if isinstance(exp, str):
            add(exp, f"`exports` in {pkg_path}", 80)
        elif isinstance(exp, dict) and isinstance(exp.get("."), str):
            add(exp["."], f"`exports[\".\"]` in {pkg_path}", 80)
        scripts = data.get("scripts") or {}
        for key in ("start", "dev", "serve"):
            cmd = scripts.get(key)
            if isinstance(cmd, str):
                m = re.search(r"\b(?:node|ts-node|tsx|bun|deno run|nodemon)\s+(?:--?\S+\s+)*([\w./\-]+\.[cm]?[jt]sx?)", cmd)
                if m:
                    add(m.group(1), f"`scripts.{key}` in {pkg_path}", 90)
    for cand, strength in (
        ("src/index.ts", 70), ("src/index.tsx", 70), ("src/main.ts", 72), ("src/main.tsx", 72),
        ("src/index.js", 68), ("src/main.js", 70), ("index.ts", 65), ("index.js", 65),
        ("server.js", 66), ("server.ts", 66), ("app.js", 60), ("app.ts", 60),
        ("src/App.tsx", 55), ("src/App.jsx", 55), ("src/app.ts", 60), ("src/server.ts", 66),
        ("app/page.tsx", 58), ("app/layout.tsx", 57), ("pages/index.tsx", 56), ("pages/index.js", 56),
        ("src/app/page.tsx", 58), ("src/app/layout.tsx", 57), ("src/pages/index.tsx", 56),
    ):
        if cand in repo.files:
            out.append(Entry(cand, f"conventional entry file `{cand}`", strength))
    return out


def _js_source_for(target: str, repo):
    """Map a package.json target (often a build output like dist/index.js) back to a source file."""
    if target in repo.files:
        yield target
        return
    stem = re.sub(r"\.[cm]?[jt]sx?$", "", target)
    for e in (".ts", ".tsx", ".js", ".mjs", ".cjs", ".jsx"):
        if stem + e in repo.files:
            yield stem + e
            return
    for e in (".ts", ".tsx", ".js"):
        if f"{stem}/index{e}" in repo.files:
            yield f"{stem}/index{e}"
            return
    # dist/index.js or lib/index.js -> src/index.ts
    m = re.match(r"^(?:dist|lib|build|out|esm|cjs)(?:/(?:esm|cjs|src))?/(.+)$", stem)
    if m:
        rest = m.group(1)
        for e in (".ts", ".tsx", ".js", ".mjs"):
            if f"src/{rest}{e}" in repo.files:
                yield f"src/{rest}{e}"
                return


def other_entries(repo):
    out = []
    for p, sf in repo.files.items():
        name = posixpath.basename(p)
        depth = p.count("/")
        if sf.lang == "go" and not p.endswith("_test.go"):
            if re.search(r"^package\s+main\b", sf.text, re.M) and re.search(r"^func\s+main\s*\(", sf.text, re.M):
                strength = 100 if depth == 0 else (95 if p.startswith("cmd/") else 80)
                out.append(Entry(p, "Go `package main` with `func main()`", strength - depth, "main"))
        elif sf.lang == "rust":
            if p.endswith("src/main.rs"):
                out.append(Entry(p, "Rust binary crate root (`src/main.rs`)", 100 - depth, "main"))
            elif "/src/bin/" in "/" + p:
                out.append(Entry(p, "Rust binary (`src/bin/`)", 85 - depth, "main"))
            elif p.endswith("src/lib.rs"):
                out.append(Entry(p, "Rust library crate root (`src/lib.rs`)", 75 - depth))
        elif sf.lang in {"java", "csharp"}:
            if re.search(r"static\s+(?:async\s+)?(?:void|int|Task(?:<int>)?)\s+[Mm]ain\s*\(", sf.text):
                out.append(Entry(p, "has a `main` method", 90 - depth, "main"))
            elif name == "Program.cs":
                out.append(Entry(p, "`Program.cs` (top-level statements)", 85 - depth))
        elif sf.lang == "ruby":
            if p.startswith(("bin/", "exe/")):
                out.append(Entry(p, "Ruby executable in `bin/`", 85))
            elif name in {"config.ru", "app.rb", "main.rb"} and depth == 0:
                out.append(Entry(p, f"conventional entry file `{name}`", 70))
        elif sf.lang == "c" and re.search(r"^\s*int\s+main\s*\(", sf.text, re.M):
            out.append(Entry(p, "defines `int main()`", 90 - depth, "main"))
        elif sf.lang == "php" and name in {"index.php", "artisan"} and depth <= 1:
            out.append(Entry(p, f"conventional entry file `{name}`", 70))
    for p in repo.paths:  # extension-less executables (bin/foo with a shebang) are not in repo.files
        if p.startswith("bin/") and "." not in posixpath.basename(p):
            lines = repo.read(p) or []
            if lines and lines[0].startswith("#!"):
                out.append(Entry(p, "executable script in `bin/`", 70))
    return out


def find_entries(repo, is_test=None):
    """All entry candidates, strongest first, one per path (keeps the best reason)."""
    found = python_entries(repo) + js_entries(repo) + other_entries(repo)
    best = {}
    for e in found:
        if is_test and is_test(e.path):
            continue
        cur = best.get(e.path)
        if cur is None or e.strength > cur.strength or (e.strength == cur.strength and e.symbol and not cur.symbol):
            if cur and cur.symbol and not e.symbol:
                e.symbol = cur.symbol
            best[e.path] = e
        elif e.symbol and not cur.symbol:
            cur.symbol = e.symbol
    return sorted(best.values(), key=lambda e: (-e.strength, e.path))
