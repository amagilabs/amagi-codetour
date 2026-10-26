"""Tour building, anchors, and the validation pass (drift detection and --fix)."""

import json
import re

from amagi_codetour.anchors import find_pattern, locate, pattern_for, regex_escape
from amagi_codetour.check import apply_fixes, check_tour
from amagi_codetour.plan import build_plan
from amagi_codetour.scan import scan
from amagi_codetour.tour import find_tours, load_tour, schema_errors, static_tour, write_tour


def _static(repo_path):
    repo = scan(repo_path)
    return repo, static_tour(repo, build_plan(repo))


def test_pattern_round_trip_with_regex_specials():
    line = "    def main(argv: list[str] | None = None) -> int:  # $1.50 {x} a/b ^"
    pat = pattern_for(line)
    assert re.search(pat, line)
    assert re.search(pat, "def main(argv: list[str] | None = None) -> int:  # $1.50 {x} a/b ^")
    assert not re.search(pat, "def main(argv) -> int:")
    assert regex_escape("a/b") == "a\\/b"  # `/` escaped so the pattern is also valid in a JS regex literal


def test_find_pattern_prefers_nearest():
    lines = ["x = 1", "y", "x = 1", "z"]
    assert find_pattern(pattern_for("x = 1"), lines, near=4) == [3, 1]


def test_locate_exact_contains_fuzzy_and_missing():
    lines = ["import os", "", "def load_config(path):", "    return path", "def load_config(path):"]
    assert locate("def load_config(path):", lines, hint=5) == (5, "exact")
    assert locate("def load_config(path):", lines, hint=2) == (3, "exact")
    assert locate("load_config(path", lines) == (3, "contains")
    assert locate("def load_confg(path):", lines)[1] == "fuzzy"
    assert locate("class Nope:", lines) == (None, "anchor text not found in file")
    assert locate("", lines, hint=1) == (1, "line")
    assert locate("", lines, hint=2)[0] is None  # blank hinted line


def test_static_tour_shape_and_schema(py_repo):
    repo, tour = _static(py_repo)
    assert tour["$schema"] == "https://aka.ms/codetour-schema"
    assert tour["title"] == "Getting started with taskq"
    assert tour["isPrimary"] is True
    assert schema_errors(tour) == []
    steps = tour["steps"]
    assert steps[0]["file"] == "README.md" and steps[0]["title"] == "Overview"
    assert steps[1]["file"] == "src/taskq/cli.py" and steps[1]["title"].startswith("Entry point")
    assert "**Start here.**" in steps[1]["description"]
    assert steps[-1] == {"directory": "tests", "title": "Tests", "description": steps[-1]["description"]}
    for s in steps:
        if "file" in s:
            lines = repo.read(s["file"])
            assert 1 <= s["line"] <= len(lines)
            assert re.search(s["pattern"], lines[s["line"] - 1])


def test_generated_static_tours_verify_clean(py_repo, js_repo, go_repo, rust_repo):
    for path in (py_repo, js_repo, go_repo, rust_repo):
        repo, tour = _static(path)
        report = check_tour(repo, tour)
        assert report.ok, [(s.target, s.messages) for s in report.failed]
        assert not report.warned, [(s.target, s.messages) for s in report.warned]


def test_write_load_and_find(py_repo):
    repo, tour = _static(py_repo)
    out = write_tour(tour, py_repo / ".tours" / "getting-started.tour")
    assert json.loads(out.read_text(encoding="utf-8"))["title"] == tour["title"]
    assert find_tours(py_repo) == [out]
    assert load_tour(out) == tour


def _insert_lines_at_top(path, n=3):
    text = path.read_text(encoding="utf-8")
    lines = text.splitlines(keepends=True)
    path.write_text(lines[0] + "".join("# added\n" for _ in range(n)) + "".join(lines[1:]), encoding="utf-8")


def test_check_detects_drift_and_fix_moves_the_step(py_repo):
    repo, tour = _static(py_repo)
    step = next(s for s in tour["steps"] if s.get("file") == "src/taskq/queue.py")
    old_line = step["line"]
    _insert_lines_at_top(py_repo / "src/taskq/queue.py", 3)
    repo = scan(py_repo)
    report = check_tour(repo, tour)
    assert not report.ok
    bad = [s for s in report.failed]
    assert len(bad) == 1 and bad[0].target == f"src/taskq/queue.py:{old_line}"
    assert bad[0].new_line == old_line + 3
    assert "code moved" in bad[0].messages[0]
    assert apply_fixes(repo, tour, report) == 1
    assert step["line"] == old_line + 3
    assert check_tour(repo, tour).ok


def test_check_reports_deleted_code_and_missing_files(py_repo):
    repo, tour = _static(py_repo)
    tour["steps"].append({"file": "src/taskq/gone.py", "line": 1, "description": "x"})
    tour["steps"].append({"file": "src/taskq/cli.py", "line": 9999, "description": "x"})
    tour["steps"].append({"directory": "nope", "description": "x"})
    p = py_repo / "src/taskq/retry.py"
    p.write_text(p.read_text(encoding="utf-8").replace("def with_retry(fn, attempts=3):", "def retry_call(fn, attempts=3):"), encoding="utf-8")
    report = check_tour(scan(py_repo), tour)
    msgs = {s.target: s.messages for s in report.failed}
    assert any(t.startswith("src/taskq/retry.py:") and m == ["anchored text no longer exists in the file"] for t, m in msgs.items())
    assert msgs["src/taskq/gone.py"] == ["file not found"]
    assert "past the end of the file" in msgs["src/taskq/cli.py:9999"][0]
    assert msgs["nope/"] == ["directory does not exist"]


def test_check_warns_on_unknown_identifiers_and_missing_pattern(py_repo):
    repo = scan(py_repo)
    tour = {"title": "t", "steps": [
        {"file": "src/taskq/queue.py", "line": 7, "description": "`JobQueue` wraps `Store` and calls `explode_everything()`."},
    ]}
    report = check_tour(repo, tour)
    assert report.ok  # warnings do not fail
    msgs = report.steps[0].messages
    assert any("no pattern" in m for m in msgs)
    assert any("`explode_everything()`" in m for m in msgs)
    assert not any("`JobQueue`" in m for m in msgs)
    apply_fixes(repo, tour, report)
    assert tour["steps"][0]["pattern"] == pattern_for("class JobQueue:")


def test_pattern_only_steps(py_repo):
    repo = scan(py_repo)
    good = {"file": "src/taskq/queue.py", "pattern": pattern_for("class JobQueue:"), "description": "d"}
    bad = {"file": "src/taskq/queue.py", "pattern": "^nothing here$", "description": "d"}
    report = check_tour(repo, {"title": "t", "steps": [good, bad]})
    assert report.steps[0].status == "ok"
    assert report.steps[1].status == "fail"


def test_schema_errors():
    assert schema_errors([]) == ["tour is not a JSON object"]
    errs = schema_errors({"steps": [{"file": 3, "line": "x"}, "nope"]})
    assert "missing string `title`" in errs
    assert "step 1: missing string `description`" in errs
    assert "step 1: `line` must be a number" in errs
    assert "step 1: `file` must be a string" in errs
    assert "step 2: not an object" in errs
