"""Top-level definitions and a one-line summary for each source file."""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass


@dataclass
class Symbol:
    name: str
    kind: str  # function, class, type, const, method
    line: int  # 1-based
    doc: str = ""


_JS_DEFS = [
    (re.compile(r"^\s*export\s+default\s+(?:async\s+)?function\s*\*?\s*([A-Za-z_$][\w$]*)"), "function"),
    (re.compile(r"^\s*(?:export\s+)?(?:async\s+)?function\s*\*?\s*([A-Za-z_$][\w$]*)"), "function"),
    (re.compile(r"^\s*(?:export\s+)?(?:default\s+)?(?:abstract\s+)?class\s+([A-Za-z_$][\w$]*)"), "class"),
    (re.compile(r"^\s*(?:export\s+)?(?:interface|type|enum)\s+([A-Za-z_$][\w$]*)"), "type"),
    (re.compile(r"^\s*(?:export\s+)?(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*(?::[^=]+)?=\s*(?:async\s*)?(?:\([^)]*\)|[A-Za-z_$][\w$]*)\s*(?::[^=]+)?=>"), "function"),
    (re.compile(r"^\s*(?:export\s+)?(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*(?::[^=]+)?=\s*(?:async\s+)?function"), "function"),
    (re.compile(r"^export\s+(?:const|let|var)\s+([A-Za-z_$][\w$]*)"), "const"),
]
_GO_DEFS = [
    (re.compile(r"^func\s+\([^)]*\)\s*([A-Za-z_]\w*)"), "method"),
    (re.compile(r"^func\s+([A-Za-z_]\w*)"), "function"),
    (re.compile(r"^type\s+([A-Za-z_]\w*)"), "type"),
]
_RUST_DEFS = [
    (re.compile(r"^\s*(?:pub(?:\([^)]*\))?\s+)?(?:async\s+)?(?:unsafe\s+)?fn\s+([A-Za-z_]\w*)"), "function"),
    (re.compile(r"^\s*(?:pub(?:\([^)]*\))?\s+)?(?:struct|enum|trait|union)\s+([A-Za-z_]\w*)"), "type"),
    (re.compile(r"^\s*impl(?:<[^>]*>)?\s+(?:[\w:<>]+\s+for\s+)?([A-Za-z_]\w*)"), "type"),
]
_RUBY_DEFS = [
    (re.compile(r"^\s*(?:class|module)\s+([A-Z][\w:]*)"), "class"),
    (re.compile(r"^\s*def\s+(?:self\.)?([A-Za-z_]\w*[?!=]?)"), "function"),
]
_CLASSLIKE_DEFS = [
    (re.compile(r"^\s*(?:(?:public|private|protected|internal|static|final|abstract|sealed|partial|data|open|export)\s+)*(?:class|interface|enum|record|struct|object|trait)\s+([A-Za-z_]\w*)"), "class"),
    (re.compile(r"^\s*(?:(?:public|private|protected|internal|static|final|abstract|override|async|virtual)\s+)+[\w<>\[\],?]+\s+([A-Za-z_]\w*)\s*\("), "function"),
    (re.compile(r"^\s*(?:(?:public|private|protected|internal|override|suspend)\s+)*fun\s+([A-Za-z_]\w*)"), "function"),
    (re.compile(r"^\s*(?:(?:public|private|protected|static)\s+)*function\s+([A-Za-z_]\w*)"), "function"),
]
_C_DEFS = [
    (re.compile(r"^(?:static\s+|inline\s+|extern\s+)*[A-Za-z_][\w\s\*]*?\b([A-Za-z_]\w*)\s*\([^;]*$"), "function"),
    (re.compile(r"^(?:typedef\s+)?struct\s+([A-Za-z_]\w*)"), "type"),
]
_GENERIC_DEFS = [
    (re.compile(r"^\s*(?:local\s+)?function\s+([A-Za-z_][\w.:]*)"), "function"),
    (re.compile(r"^\s*(?:defmodule|def|defp)\s+([A-Za-z_][\w.!?]*)"), "function"),
    (re.compile(r"^\s*([A-Za-z_]\w*)\s*\(\)\s*\{"), "function"),
]

_LANG_DEFS = {
    "js": _JS_DEFS,
    "go": _GO_DEFS,
    "rust": _RUST_DEFS,
    "ruby": _RUBY_DEFS,
    "java": _CLASSLIKE_DEFS,
    "csharp": _CLASSLIKE_DEFS,
    "php": _CLASSLIKE_DEFS,
    "swift": _CLASSLIKE_DEFS,
    "c": _C_DEFS,
}

_C_KEYWORDS = {"if", "for", "while", "switch", "return", "sizeof", "else", "do"}


def _first_sentence(text: str, limit: int = 200) -> str:
    text = " ".join(text.strip().split())
    if not text:
        return ""
    m = re.match(r"(.+?[.!?])(\s|$)", text)
    s = m.group(1) if m else text
    if len(s) > limit:
        s = s[: limit - 3].rstrip() + "..."
    return s


def python_symbols(text: str):
    """Top-level defs/classes (plus methods of top-level classes) and the module docstring."""
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError):
        return None, ""
    out = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            kind = "class" if isinstance(node, ast.ClassDef) else "function"
            out.append(Symbol(node.name, kind, node.lineno, _first_sentence(ast.get_docstring(node) or "")))
            if isinstance(node, ast.ClassDef):
                for sub in node.body:
                    if isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef)) and not sub.name.startswith("__"):
                        out.append(Symbol(f"{node.name}.{sub.name}", "method", sub.lineno,
                                          _first_sentence(ast.get_docstring(sub) or "")))
        elif isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for t in targets:
                if isinstance(t, ast.Name) and (t.id.isupper() or t.id in {"app", "router", "api", "cli", "bp", "blueprint"}):
                    out.append(Symbol(t.id, "const", node.lineno))
    return out, _first_sentence(ast.get_docstring(tree) or "")


def _leading_comment(lines) -> str:
    """Summary from a file's leading comment block (//, #, /* */, --, ///, //!)."""
    buf = []
    in_block = False
    for raw in lines[:60]:
        line = raw.strip()
        if not buf and not in_block and (not line or line.startswith("#!") or line.startswith(("'use ", '"use ', "package ", "<?php"))):
            continue
        if in_block:
            end = "*/" in line
            line = line.split("*/")[0].lstrip("*").strip()
            if line and not line.startswith("@"):
                buf.append(line)
            if end:
                break
            continue
        if line.startswith("/*"):
            in_block = True
            end = "*/" in line[2:]
            line = line[2:].lstrip("*!").split("*/")[0].strip()
            if line:
                buf.append(line)
            if end:
                break
            continue
        m = re.match(r"^(///?!?|#|--)\s?(.*)$", line)
        if m and not line.startswith("#include") and not line.startswith("#[") and not line.startswith("#!"):
            if m.group(2).strip():
                buf.append(m.group(2).strip())
            continue
        break
    text = " ".join(buf)
    if re.search(r"(?i)copyright|license|spdx|eslint-disable|@ts-|prettier-ignore|-\*-", text):
        return ""
    return _first_sentence(text)


def regex_symbols(lang: str, lines):
    defs = _LANG_DEFS.get(lang, _GENERIC_DEFS)
    out = []
    seen = set()
    for i, line in enumerate(lines, 1):
        if len(line) > 300:
            continue
        for rx, kind in defs:
            m = rx.match(line)
            if not m:
                continue
            name = m.group(1)
            if lang == "c" and (name in _C_KEYWORDS or line.startswith((" ", "\t"))):
                break
            if lang in {"java", "csharp", "php", "swift"} and kind == "function" and line.startswith(("\t\t", "        ")):
                break
            key = (name, i)
            if key not in seen:
                seen.add(key)
                out.append(Symbol(name, kind, i))
            break
    return out


def file_symbols(sf):
    """(symbols, summary) for a SourceFile."""
    if sf.lang == "python":
        syms, doc = python_symbols(sf.text)
        if syms is not None:
            return syms, doc or _leading_comment(sf.lines)
    return regex_symbols(sf.lang, sf.lines), _leading_comment(sf.lines)
