"""Line anchors: turn a line of code into a CodeTour `pattern`, and find it again later.

Every step we write carries both `line` (what CodeTour jumps to) and `pattern`
(which CodeTour only reads when `line` is missing). The pattern is the exact
text of the anchored line, so `check` can tell when code moved under the tour.
"""

from __future__ import annotations

import difflib
import re

# characters that are special in both Python `re` and JavaScript RegExp
_SPECIAL = set("\\^$.|?*+()[]{}/")


def regex_escape(text: str) -> str:
    """Escape for a regex that both Python and JavaScript (CodeTour) accept."""
    return "".join("\\" + c if c in _SPECIAL else c for c in text)


def pattern_for(line_text: str) -> str:
    """CodeTour pattern that matches this line wherever it moves (leading indent ignored)."""
    return r"^\s*" + regex_escape(line_text.strip())


def pattern_matches(pattern: str, line_text: str) -> bool:
    try:
        return re.search(pattern, line_text) is not None
    except re.error:
        return False


def find_pattern(pattern: str, lines, near: int = 1):
    """1-based line numbers matching pattern, closest to `near` first."""
    try:
        rx = re.compile(pattern)
    except re.error:
        return []
    hits = [i for i, text in enumerate(lines, 1) if rx.search(text)]
    return sorted(hits, key=lambda i: (abs(i - near), i))


def _norm(s: str) -> str:
    return " ".join(s.strip().split())


def locate(anchor: str, lines, hint: int = 0):
    """Find the line for an anchor text. Returns (line, how) or (None, reason).

    how is "exact" (equal, ignoring whitespace), "contains" (the anchor is a
    fragment of the line; the match nearest `hint` wins), "fuzzy" (>= 0.85
    similar) or "line" (empty anchor, so the hinted non-blank line is used).
    """
    want = _norm(anchor)
    if not want:
        if 1 <= hint <= len(lines) and lines[hint - 1].strip():
            return hint, "line"
        return None, "empty anchor"
    exact = [i for i, t in enumerate(lines, 1) if _norm(t) == want]
    if exact:
        return min(exact, key=lambda i: (abs(i - hint) if hint else 0, i)), "exact"
    if len(want) >= 8:
        contains = [i for i, t in enumerate(lines, 1) if want in _norm(t)]
        if contains:
            return min(contains, key=lambda i: (abs(i - hint) if hint else 0, i)), "contains"
    best, best_ratio = None, 0.0
    for i, t in enumerate(lines, 1):
        n = _norm(t)
        if not n or abs(len(n) - len(want)) > max(12, len(want) // 2):
            continue
        r = difflib.SequenceMatcher(None, want, n).ratio()
        if r > best_ratio or (r == best_ratio and hint and best and abs(i - hint) < abs(best - hint)):
            best, best_ratio = i, r
    if best is not None and best_ratio >= 0.85:
        return best, "fuzzy"
    return None, "anchor text not found in file"
