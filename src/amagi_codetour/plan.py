"""Pick the 8-15 files a newcomer needs, in reading order, each with an anchor line.

Static and deterministic: entry points, the import graph, and a few naming
hints. The LLM engines start from this plan; `--engine static` uses it as-is.
"""

from __future__ import annotations

import ast
import math
import posixpath
import re
from collections import Counter, deque
from dataclasses import dataclass, field

from .entry import find_entries
from .graph import build_graph, reverse_edges
from .scan import TEST_DIR_NAMES, is_low_value_path, is_test_path
from .symbols import file_symbols

NAME_HINTS = {
    "core", "app", "main", "cli", "server", "api", "routes", "router", "models", "model",
    "schema", "schemas", "config", "settings", "db", "database", "store", "engine",
    "handler", "handlers", "service", "services", "client", "types", "pipeline", "runner",
    "commands", "views", "controller", "controllers", "index", "lib", "parser", "plugin",
}

README_NAMES = ("README.md", "README.rst", "README.txt", "README", "readme.md", "Readme.md")


@dataclass
class FilePick:
    path: str
    score: float
    reasons: list
    dist: object = None  # int hops from an entry point, or None if unreachable
    entry_reason: str = ""
    summary: str = ""
    symbols: list = field(default_factory=list)
    anchor_line: int = 1
    anchor_symbol: str = ""
    anchor_why: str = ""
    imports: list = field(default_factory=list)  # [(path, [names])] into other picked files
    used_by: list = field(default_factory=list)  # picked files that import this one
    n_lines: int = 0


@dataclass
class Plan:
    root: str
    name: str
    entries: list
    picks: list  # ordered FilePick
    ranked: list  # every candidate FilePick, best first (picks are a subset)
    readme: str = ""
    test_dir: str = ""
    test_cmd: str = ""
    languages: dict = field(default_factory=dict)
    edges: dict = field(default_factory=dict)


def _bfs(edges, roots):
    dist = {}
    q = deque()
    for r in roots:
        if r not in dist:
            dist[r] = 0
            q.append(r)
    while q:
        cur = q.popleft()
        for nxt in edges.get(cur, {}):
            if nxt not in dist:
                dist[nxt] = dist[cur] + 1
                q.append(nxt)
    return dist


def _meaningful_line(lines) -> int:
    in_block = False
    for i, raw in enumerate(lines, 1):
        s = raw.strip()
        if in_block:
            if '"""' in s or "'''" in s or "*/" in s:
                in_block = False
            continue
        if not s or s.startswith(("#", "//", "--", "*")):
            continue
        if s.startswith(('"""', "'''")) and not (len(s) > 3 and s.endswith(s[:3])):
            in_block = True
            continue
        if s.startswith("/*") and "*/" not in s:
            in_block = True
            continue
        if re.match(r"^(import|from|package|using|require|use|#include|'use |\"use )", s):
            continue
        return i
    for i, raw in enumerate(lines, 1):
        if raw.strip():
            return i
    return 1


def _choose_anchor(pick, sf, entry, imported_names):
    syms = pick.symbols
    by_name = {}
    for s in syms:
        by_name.setdefault(s.name, s)
        by_name.setdefault(s.name.split(".")[-1], s)
    if entry and entry.symbol and entry.symbol in by_name:
        s = by_name[entry.symbol]
        return s.line, s.name, f"the `{s.name}` function the entry point calls"
    # prefer real entry points (functions, classes) over exceptions and constants;
    # on a tie, a name that matches the file (scan() in scan.py), then definition order
    stem = posixpath.splitext(posixpath.basename(pick.path))[0].lower().replace("-", "_")

    def _rank(item):
        name, n = item
        s = by_name.get(name)
        weak = name.endswith(("Error", "Exception")) or (s is not None and s.kind == "const")
        low = name.lower()
        named = 0 if low == stem else 1 if stem and stem in low else 2
        return (-n, weak, named, s.line if s is not None else 1 << 30, name)

    for name, n in sorted(imported_names.items(), key=_rank):
        if name == "default":
            for i, text in enumerate(sf.lines, 1):
                if re.match(r"^\s*export\s+default\b", text):
                    return i, "default export", "the default export other files import"
            continue
        if name in by_name:
            s = by_name[name]
            who = "file" if n == 1 else "files"
            return s.line, s.name, f"`{s.name}`, imported by {n} other tour {who}"
    if entry:
        for i, text in enumerate(sf.lines, 1):
            if re.match(r"""^if\s+__name__\s*==\s*['"]__main__['"]""", text):
                return i, "__main__", "the `__main__` block"
        if "main" in by_name:
            s = by_name["main"]
            return s.line, s.name, "the `main` function"
    public = [s for s in syms if s.kind != "method" and not s.name.startswith("_")]
    public.sort(key=lambda s: s.name.endswith(("Error", "Exception", "Warning")))  # stable: errors last
    for kind in ("class", "type", "function", "const"):
        for s in public:
            if s.kind == kind:
                return s.line, s.name, f"the first top-level {kind}, `{s.name}`"
    if syms:
        return syms[0].line, syms[0].name, f"`{syms[0].name}`"
    return _meaningful_line(sf.lines), "", "the first line of code"


def _trivial_python(text: str) -> bool:
    """True when a module only re-exports, sets dunders, or carries a docstring."""
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError):
        return False
    for node in tree.body:
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            continue
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
            continue
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            if all(isinstance(t, ast.Name) and t.id.startswith("__") and t.id.endswith("__") for t in targets):
                continue
        return False
    return True


def _test_info(repo, entries):
    test_files = [p for p in repo.files if is_test_path(p)]
    # a tests/ (spec/, __tests__/ ...) folder gets its own tour step; colocated tests do not
    dirs = Counter()
    for p in test_files:
        parts = p.split("/")[:-1]
        for i, part in enumerate(parts):
            if part.lower() in TEST_DIR_NAMES:
                dirs["/".join(parts[: i + 1])] += 1
                break
    test_dir = ""
    if dirs:
        test_dir = min(dirs.items(), key=lambda kv: (-kv[1], kv[0].count("/"), kv[0]))[0]
    cmd = ""
    langs = Counter(repo.files[p].lang for p in test_files)
    lang = langs.most_common(1)[0][0] if langs else ""
    if lang == "python":
        cmd = "pytest"
    elif lang == "js":
        pkg = "\n".join(repo.read("package.json") or [])
        cmd = "npm test" if re.search(r'"test"\s*:', pkg) else ""
    elif lang == "go":
        cmd = "go test ./..."
    elif lang == "rust" or (not lang and "Cargo.toml" in repo.paths):
        cmd = "cargo test"
    elif lang == "ruby":
        cmd = "bundle exec rspec" if test_dir == "spec" else "bundle exec rake test"
    if not test_dir and "Cargo.toml" in repo.paths:
        cmd = "cargo test"
    return test_dir, cmd


def build_plan(repo, min_files: int = 8, max_files: int = 15) -> Plan:
    if min_files < 1 or max_files < min_files:
        raise ValueError("need 1 <= min_files <= max_files")
    edges = build_graph(repo)
    rev = reverse_edges(edges)
    entries = find_entries(repo, is_test=is_test_path)
    entry_by_path = {e.path: e for e in entries}
    candidates = [p for p in repo.files if not is_test_path(p)]
    if not candidates:
        candidates = list(repo.files)

    roots = []
    if entries:
        top = entries[0].strength
        roots = [e.path for e in entries if e.strength >= top - 10][:3]
    dist = _bfs(edges, roots)

    ranked = []
    for p in candidates:
        sf = repo.files[p]
        reasons = []
        score = 0.0
        e = entry_by_path.get(p)
        if p in roots:
            score += 100
            reasons.append(f"entry point: {e.reason}")
        elif e:
            score += e.strength / 10
            reasons.append(f"possible entry: {e.reason}")
        d = dist.get(p)
        if d is not None and d > 0:
            score += 30 / (1 + d)
            reasons.append(f"{d} import hop{'s' if d > 1 else ''} from the entry point")
        importers = [s for s in rev.get(p, {}) if not is_test_path(s)]
        if importers:
            score += 8 * math.log2(1 + len(importers))
            reasons.append(f"imported by {len(importers)} file{'s' if len(importers) > 1 else ''}")
        n = sf.n_lines
        if n < 8:
            score -= 12
        elif 30 <= n <= 800:
            score += 4
        name = posixpath.basename(p)
        stem = name.split(".")[0].lower()
        if sf.lang == "python" and _trivial_python(sf.text):
            score -= 40
            reasons.append("only imports, a docstring or a version string")
        if stem in NAME_HINTS or posixpath.basename(posixpath.dirname(p)).lower() in NAME_HINTS:
            score += 5
            reasons.append("name suggests a core module")
        if is_low_value_path(p):
            score -= 25
            reasons.append("lives in examples/docs/scripts/vendor (lower priority)")
        if p.count("/") > 4:
            score -= 2 * (p.count("/") - 4)
        syms, summary = file_symbols(sf)
        ranked.append(FilePick(p, round(score, 2), reasons, d, e.reason if e else "", summary, syms, n_lines=n))

    ranked.sort(key=lambda f: (-f.score, f.path))
    good = [f for f in ranked if f.score >= 6]
    usable = [f for f in ranked if f.score > -10] or ranked[:1]  # never pad with near-empty files
    n_pick = min(max_files, max(min_files, len(good)))
    chosen = usable[:n_pick]
    chosen_set = {f.path for f in chosen}

    # reading order: entry points first, then breadth-first along imports, then the rest by score
    order_index = {}
    seen = []
    q = deque(r for r in roots if r in chosen_set)
    for r in q:
        seen.append(r)
    while q:
        cur = q.popleft()
        for nxt in edges.get(cur, {}):
            if nxt in chosen_set and nxt not in seen:
                seen.append(nxt)
                q.append(nxt)
    rest = [f.path for f in chosen if f.path not in seen]
    for i, p in enumerate(seen + rest):
        order_index[p] = i
    picks = sorted(chosen, key=lambda f: order_index[f.path])

    for f in picks:
        sf = repo.files[f.path]
        names = Counter()
        for src, nm in rev.get(f.path, {}).items():
            if src in chosen_set and src != f.path:
                for x in nm:
                    if x != "*":
                        names[x] += 1
        f.anchor_line, f.anchor_symbol, f.anchor_why = _choose_anchor(f, sf, entry_by_path.get(f.path), names)
        f.imports = [(dst, sorted(n for n in nm if n != "*")) for dst, nm in edges.get(f.path, {}).items() if dst in chosen_set]
        f.used_by = [src for src in rev.get(f.path, {}) if src in chosen_set and src != f.path]

    readme = next((r for r in README_NAMES if r in repo.paths), "")
    test_dir, test_cmd = _test_info(repo, entries)
    langs = Counter(sf.lang for sf in repo.files.values())
    return Plan(str(repo.root), repo.root.name, entries, picks, ranked, readme, test_dir, test_cmd, dict(langs.most_common()), edges)
