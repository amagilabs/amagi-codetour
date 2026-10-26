"""The validation pass: check every step of a tour against the code on disk.

Problems (exit 1):  missing file/directory, line out of range, anchored line
                    no longer matches its pattern (drift), pattern gone.
Warnings:           blank anchor line, step without a pattern to verify,
                    `identifiers` in a description that are not in the file.
"""

from __future__ import annotations

import posixpath
import re
from dataclasses import dataclass, field

from .anchors import find_pattern, pattern_for, pattern_matches
from .tour import schema_errors

_IDENT = re.compile(r"`([^`\n]{1,80})`")
_IDENT_OK = re.compile(r"^[A-Za-z_$][\w$]*(?:(?:\.|::|->|#)[A-Za-z_$][\w$]*)*(?:\(\))?$")
_SKIP_WORDS = {
    "true", "false", "none", "null", "nil", "self", "this", "main", "__main__", "default",
    "pytest", "npm", "pip", "pipx", "python", "node", "go", "cargo", "git", "make",
}


@dataclass
class StepResult:
    index: int  # 1-based
    target: str
    status: str  # ok | warn | fail
    messages: list = field(default_factory=list)
    new_line: object = None  # suggested line when drifted and the pattern was found


@dataclass
class TourReport:
    path: str
    title: str
    steps: list
    schema: list

    @property
    def failed(self):
        return [s for s in self.steps if s.status == "fail"]

    @property
    def warned(self):
        return [s for s in self.steps if s.status == "warn"]

    @property
    def ok(self) -> bool:
        return not self.failed and not self.schema

    @property
    def fixable(self):
        return [s for s in self.steps if s.new_line]


def norm_path(p) -> str:
    p = str(p).replace("\\", "/")
    while p.startswith("./"):
        p = p[2:]
    return p


def _mentions(description: str):
    out = []
    for m in _IDENT.finditer(description or ""):
        tok = m.group(1).strip()
        if not _IDENT_OK.match(tok):
            continue
        base = re.split(r"\.|::|->|#", tok.replace("()", ""))[-1]
        if base.lower() in _SKIP_WORDS or len(base) < 3:
            continue
        out.append((tok, base))
    return out


def _mention_warnings(description, file_text, repo_paths):
    missing = []
    for tok, base in _mentions(description):
        if re.search(r"(?<![\w$])" + re.escape(base) + r"(?![\w$])", file_text):
            continue
        if any(posixpath.basename(p) == tok or p == tok for p in repo_paths):
            continue
        missing.append(tok)
    return missing


def check_tour(repo, tour: dict, path: str = "", check_mentions: bool = True) -> TourReport:
    schema = schema_errors(tour)
    steps = tour.get("steps") if isinstance(tour, dict) else None
    results = []
    file_texts = {}
    for i, step in enumerate(steps if isinstance(steps, list) else [], 1):
        if not isinstance(step, dict):
            results.append(StepResult(i, "?", "fail", ["step is not an object"]))
            continue
        if "directory" in step and "file" not in step:
            d = str(step["directory"]).strip("/")
            ok = (repo.root / d).is_dir()
            results.append(StepResult(i, d + "/", "ok" if ok else "fail", [] if ok else ["directory does not exist"]))
            continue
        if "file" not in step:
            results.append(StepResult(i, step.get("uri") or step.get("view") or "(content step)", "ok"))
            continue
        f = norm_path(step["file"])
        r = StepResult(i, f, "ok")
        results.append(r)
        lines = repo.read(f)
        if lines is None:
            r.status = "fail"
            r.messages.append("file not found")
            continue
        file_texts[f] = "\n".join(lines)
        line = step.get("line")
        pattern = step.get("pattern")
        if line is None and not pattern:
            r.status = "warn"
            r.messages.append("no line or pattern: step opens the file at the top")
        elif line is not None:
            if not isinstance(line, (int, float)) or isinstance(line, bool):
                r.status = "fail"
                r.messages.append("line is not a number")
                continue
            line = int(line)
            r.target = f"{f}:{line}"
            if line < 1 or line > len(lines):
                r.status = "fail"
                r.messages.append(f"line {line} is past the end of the file ({len(lines)} lines)")
            elif not lines[line - 1].strip():
                r.status = "warn"
                r.messages.append("anchored line is blank")
            if pattern:
                if 1 <= line <= len(lines) and pattern_matches(pattern, lines[line - 1]):
                    pass
                else:
                    hits = find_pattern(pattern, lines, near=line)
                    r.status = "fail"
                    if hits:
                        r.new_line = hits[0]
                        r.messages.append(f"code moved: anchored text is now at line {hits[0]}")
                    else:
                        r.messages.append("anchored text no longer exists in the file")
            elif r.status == "ok":
                r.status = "warn"
                r.messages.append("no pattern, so drift cannot be detected")
        else:  # pattern only: CodeTour resolves it at open time
            hits = find_pattern(pattern, lines)
            r.target = f"{f} /{pattern}/"
            if not hits:
                r.status = "fail"
                r.messages.append("pattern matches nothing in the file")
            elif len(hits) > 1:
                r.status = "warn"
                r.messages.append(f"pattern matches {len(hits)} lines; CodeTour uses the first")
    if check_mentions:
        # after the loop, so a mention can point at a tour file that comes later in the tour
        others = "\n".join(file_texts.values())
        for r in results:
            step = steps[r.index - 1]
            if r.status == "fail" or not isinstance(step, dict) or "file" not in step:
                continue
            f = norm_path(step["file"])
            missing = _mention_warnings(step.get("description", ""), file_texts.get(f, ""), repo.paths)
            missing = [m for m in missing if not re.search(r"(?<![\w$])" + re.escape(re.split(r"\.|::|->|#", m.replace("()", ""))[-1]) + r"(?![\w$])", others)]
            if missing:
                if r.status == "ok":
                    r.status = "warn"
                r.messages.append("mentions " + ", ".join(f"`{m}`" for m in missing[:5]) + " but that name is not in the file")
    title = tour.get("title", "") if isinstance(tour, dict) else ""
    return TourReport(path, title, results, schema)


def apply_fixes(repo, tour: dict, report: TourReport) -> int:
    """Move drifted steps to where their anchored text now lives. Returns steps changed."""
    fixed = 0
    by_index = {s.index: s for s in report.steps}
    for i, step in enumerate(tour.get("steps", []), 1):
        r = by_index.get(i)
        if r and r.new_line:
            step["line"] = r.new_line
            fixed += 1
        elif r and r.status in {"ok", "warn"} and "file" in step and isinstance(step.get("line"), int) and not step.get("pattern"):
            lines = repo.read(step["file"]) or []
            ln = step["line"]
            if 1 <= ln <= len(lines) and lines[ln - 1].strip():
                step["pattern"] = pattern_for(lines[ln - 1])
                fixed += 1
    return fixed
