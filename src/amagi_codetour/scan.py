"""Find the files in a repo and read them.

Uses `git ls-files` when the folder is a git work tree (so .gitignore is
respected), and a plain directory walk with sensible excludes otherwise.
"""

from __future__ import annotations

import os
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

SKIP_DIRS = {
    ".git", ".hg", ".svn", "node_modules", ".venv", "venv", "env", "__pycache__",
    "dist", "build", ".tox", ".nox", ".mypy_cache", ".pytest_cache", ".ruff_cache",
    "target", ".next", ".nuxt", "coverage", "htmlcov", "site-packages", ".idea",
    ".vscode", ".tours", ".cache", "out", "bower_components", ".gradle", "Pods",
}

LANG_BY_EXT = {
    ".py": "python",
    ".js": "js", ".jsx": "js", ".mjs": "js", ".cjs": "js",
    ".ts": "js", ".tsx": "js", ".mts": "js", ".cts": "js",
    ".go": "go",
    ".rs": "rust",
    ".rb": "ruby",
    ".java": "java", ".kt": "java", ".kts": "java", ".scala": "java",
    ".cs": "csharp",
    ".php": "php",
    ".c": "c", ".h": "c", ".cc": "c", ".cpp": "c", ".hpp": "c", ".cxx": "c",
    ".swift": "swift",
    ".lua": "lua",
    ".ex": "elixir", ".exs": "elixir",
    ".sh": "shell", ".bash": "shell",
    ".vue": "js", ".svelte": "js",
}

MAX_BYTES = 400_000

TEST_DIR_NAMES = {"tests", "test", "__tests__", "spec", "specs", "testdata", "testing"}
LOW_VALUE_DIRS = {
    "examples", "example", "docs", "doc", "fixtures", "fixture", "migrations",
    "vendor", "third_party", "thirdparty", "scripts", "benchmarks", "bench",
    "samples", "sample", "demo", "demos", "e2e", "generated", "gen", "stubs",
}


@dataclass
class SourceFile:
    path: str  # repo-relative, forward slashes
    lang: str
    text: str
    lines: list = field(default_factory=list)

    @property
    def name(self) -> str:
        return self.path.rsplit("/", 1)[-1]

    @property
    def n_lines(self) -> int:
        return len(self.lines)


@dataclass
class Repo:
    root: Path
    paths: list  # every non-skipped repo-relative path (any type)
    files: dict  # path -> SourceFile, source files only

    def read(self, rel: str):
        """Return the lines of any text file in the repo, or None."""
        if rel in self.files:
            return self.files[rel].lines
        p = self.root / rel
        try:
            if not p.is_file() or p.stat().st_size > MAX_BYTES:
                return None
            return _decode(p.read_bytes()).splitlines()
        except OSError:
            return None

    def exists(self, rel: str) -> bool:
        return (self.root / rel).exists()


def is_test_path(path: str) -> bool:
    parts = path.lower().split("/")
    if any(p in TEST_DIR_NAMES for p in parts[:-1]):
        return True
    name = parts[-1]
    return bool(
        re.match(r"test_.*\.py$", name)
        or re.search(r"_test\.(py|go|rb|exs?)$", name)
        or re.search(r"\.(test|spec)\.[cm]?[jt]sx?$", name)
        or re.search(r"(Test|Tests|Spec)\.(java|kt|cs|scala)$", path.rsplit("/", 1)[-1])
        or name in {"conftest.py", "setupTests.js", "setupTests.ts"}
    )


def is_low_value_path(path: str) -> bool:
    parts = path.lower().split("/")
    if any(p in LOW_VALUE_DIRS for p in parts[:-1]):
        return True
    name = parts[-1]
    return bool(
        re.search(r"\.(min|bundle|generated|pb|d)\.[a-z]+$", name)
        or name.endswith(("_pb2.py", ".pb.go", "_generated.go"))
        or name in {"setup.py", "conf.py", "noxfile.py", "tasks.py", "fabfile.py"}
        or re.match(r"(webpack|vite|rollup|babel|jest|vitest|eslint|prettier|tailwind|postcss|next|nuxt|svelte|astro|playwright)\.config\.", name)
    )


def _decode(data: bytes) -> str:
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return data.decode("latin-1")


def _git_paths(root: Path):
    try:
        out = subprocess.run(
            ["git", "-C", str(root), "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
            capture_output=True, timeout=60,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0:
        return None
    paths = [p for p in _decode(out.stdout).split("\0") if p]
    # git ls-files reports paths relative to the given dir when it's a subfolder of a repo
    return sorted(set(paths))


def _walk_paths(root: Path):
    paths = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(
            d for d in dirnames
            if d not in SKIP_DIRS and not d.endswith(".egg-info") and not d.startswith(".")
        )
        rel_dir = os.path.relpath(dirpath, root).replace(os.sep, "/")
        for f in filenames:
            rel = f if rel_dir == "." else f"{rel_dir}/{f}"
            paths.append(rel)
    return sorted(paths)


def _skipped(rel: str) -> bool:
    parts = rel.split("/")
    return any(p in SKIP_DIRS or p.endswith(".egg-info") for p in parts[:-1])


def scan(root, use_git: bool = True) -> Repo:
    root = Path(root).resolve()
    if not root.is_dir():
        raise NotADirectoryError(str(root))
    paths = _git_paths(root) if use_git else None
    if paths is None:
        paths = _walk_paths(root)
    paths = [p for p in paths if not _skipped(p) and (root / p).is_file()]
    files = {}
    for rel in paths:
        ext = os.path.splitext(rel)[1].lower()
        lang = LANG_BY_EXT.get(ext)
        if not lang:
            continue
        p = root / rel
        try:
            if p.stat().st_size > MAX_BYTES:
                continue
            data = p.read_bytes()
        except OSError:
            continue
        if b"\0" in data[:4096]:
            continue
        text = _decode(data)
        files[rel] = SourceFile(rel, lang, text, text.splitlines())
    return Repo(root, paths, files)
