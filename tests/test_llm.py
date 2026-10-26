"""The LLM path: prompt, reply parsing, and holding the model's steps against the code.

No network: the model is a fake command (--llm-cmd) or a fake `anthropic` module.
"""

import json
import sys
import types

import pytest

from amagi_codetour import llm
from amagi_codetour.check import check_tour
from amagi_codetour.plan import build_plan
from amagi_codetour.scan import scan


def _reply(steps, title="Tour of taskq"):
    return {"title": title, "description": "How a job flows through taskq.", "steps": steps}


GOOD_STEPS = [
    {"file": "src/taskq/cli.py", "line": 17, "anchor": "def main(argv=None):", "title": "Entry", "description": "`main` parses args and builds a `JobQueue`."},
    {"file": "src/taskq/queue.py", "line": 3, "anchor": "class JobQueue:", "title": "Queue", "description": "`JobQueue` wraps a `Store`."},
    {"file": "./src/taskq/worker.py", "line": 7, "anchor": "    def run_once(self):", "title": "Worker", "description": "`run_once` takes a job."},
    {"file": "src/taskq/nope.py", "line": 1, "anchor": "x", "title": "Ghost", "description": "Not a real file."},
    {"file": "src/taskq/store.py", "line": 5, "anchor": "def invented_function():", "title": "Made up", "description": "This line does not exist."},
    {"file": "src/taskq/models.py", "line": 6, "anchor": "class Job:", "title": "Job", "description": "`Job` is the record."},
]


def test_build_prompt_contains_numbered_files_and_rules(py_repo):
    repo = scan(py_repo)
    plan = build_plan(repo)
    prompt = llm.build_prompt(repo, plan)
    assert "# Repository: taskq" in prompt
    assert "## File: src/taskq/cli.py" in prompt
    assert "17| def main(argv=None):" in prompt
    assert "console script `taskq`" in prompt
    assert "Cover between 8 and 15 different files" in prompt
    assert '"anchor"' in prompt


def test_parse_response_handles_fences_and_prose():
    data = {"title": "t", "steps": []}
    assert llm.parse_response("```json\n" + json.dumps(data) + "\n```") == data
    assert llm.parse_response("Here you go:\n" + json.dumps(data) + "\nDone.") == data
    for bad in ("", "no json here", "{not json}", '{"title": "t"}'):
        with pytest.raises(llm.EngineError):
            llm.parse_response(bad)


def test_resolve_steps_corrects_lines_and_drops_hallucinations(py_repo):
    repo = scan(py_repo)
    plan = build_plan(repo)
    tour, notes = llm.resolve_steps(repo, plan, _reply(GOOD_STEPS), min_files=1, max_files=15)
    files = [s.get("file") for s in tour["steps"]]
    assert files[0] == "README.md"
    assert "src/taskq/nope.py" not in files
    assert files.count("src/taskq/store.py") == 0
    queue = next(s for s in tour["steps"] if s.get("file") == "src/taskq/queue.py")
    assert repo.read("src/taskq/queue.py")[queue["line"] - 1] == "class JobQueue:"
    assert queue["line"] != 3  # the model's line number was wrong; the quoted text wins
    worker = next(s for s in tour["steps"] if s.get("file") == "src/taskq/worker.py")  # "./" stripped
    assert repo.read("src/taskq/worker.py")[worker["line"] - 1].strip() == "def run_once(self):"
    assert any("nope.py" in n for n in notes)
    assert any("anchor text not found" in n for n in notes)
    assert any("model said line 3" in n for n in notes)
    assert tour["title"] == "Tour of taskq"
    assert check_tour(repo, tour).ok


def test_resolve_steps_fills_from_static_plan_when_model_covers_too_few_files(py_repo):
    repo = scan(py_repo)
    plan = build_plan(repo)
    tour, notes = llm.resolve_steps(repo, plan, _reply(GOOD_STEPS[:2]), min_files=8, max_files=15)
    files = {s["file"] for s in tour["steps"] if "file" in s and s["file"] != "README.md"}
    assert len(files) == 8
    assert any("added 6 from the static plan" in n for n in notes)
    assert check_tour(repo, tour).ok


def test_resolve_steps_caps_files_and_steps_per_file(py_repo):
    repo = scan(py_repo)
    plan = build_plan(repo)
    many = [dict(GOOD_STEPS[0]) for _ in range(5)]
    tour, notes = llm.resolve_steps(repo, plan, _reply(many + GOOD_STEPS[1:2]), min_files=1, max_files=1)
    files = [s["file"] for s in tour["steps"] if "file" in s and s["file"] != "README.md"]
    assert files == ["src/taskq/cli.py"] * 3
    assert any("more than 3 steps" in n for n in notes)
    assert any("already 1 files" in n for n in notes)


def test_resolve_steps_with_nothing_usable_raises(py_repo):
    repo = scan(py_repo)
    plan = build_plan(repo)
    plan.picks = []
    with pytest.raises(llm.EngineError):
        llm.resolve_steps(repo, plan, _reply(GOOD_STEPS[3:5]), min_files=1)


def test_run_command_engine_round_trip(tmp_path):
    script = tmp_path / "echo_model.py"
    script.write_text("import sys\nprompt = sys.stdin.read()\nprint('{\"steps\": [], \"n\": %d}' % len(prompt))\n", encoding="utf-8")
    out = llm.run_command("hello", f'"{sys.executable}" "{script}"')
    assert json.loads(out) == {"steps": [], "n": 5}
    script.write_text("import sys\nsys.exit(3)\n", encoding="utf-8")
    with pytest.raises(llm.EngineError, match="exited 3"):
        llm.run_command("hello", f'"{sys.executable}" "{script}"')


class _FakeStream:
    def __init__(self, msg):
        self.msg = msg

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def get_final_message(self):
        return self.msg


def _fake_anthropic(monkeypatch, text, stop_reason="end_turn"):
    calls = {}
    mod = types.ModuleType("anthropic")

    class APIError(Exception):
        pass

    class Anthropic:
        def __init__(self, **kw):
            calls["client"] = kw
            self.messages = self

        def stream(self, **kw):
            calls["request"] = kw
            block = types.SimpleNamespace(type="text", text=text)
            return _FakeStream(types.SimpleNamespace(content=[block], stop_reason=stop_reason))

    mod.Anthropic = Anthropic
    mod.APIError = APIError
    monkeypatch.setitem(sys.modules, "anthropic", mod)
    return calls


def test_anthropic_engine_uses_sdk_and_default_model(monkeypatch):
    calls = _fake_anthropic(monkeypatch, '{"steps": []}')
    monkeypatch.delenv("AMAGI_CODETOUR_MODEL", raising=False)
    assert llm.run_anthropic("the prompt") == '{"steps": []}'
    req = calls["request"]
    assert req["model"] == "claude-opus-5"
    assert req["messages"] == [{"role": "user", "content": "the prompt"}]
    assert req["system"] == llm.SYSTEM
    assert llm.run_anthropic("p", model="claude-sonnet-5") and calls["request"]["model"] == "claude-sonnet-5"


def test_anthropic_engine_refusal(monkeypatch):
    _fake_anthropic(monkeypatch, "", stop_reason="refusal")
    with pytest.raises(llm.EngineError, match="declined"):
        llm.run_anthropic("p")


def test_anthropic_missing_package(monkeypatch):
    monkeypatch.setitem(sys.modules, "anthropic", None)
    with pytest.raises(llm.EngineError, match="not installed"):
        llm.run_anthropic("p")


def test_pick_engine(monkeypatch):
    assert llm.pick_engine("auto", llm_cmd="cat") == "command"
    assert llm.pick_engine("static") == "static"
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setattr(llm.shutil, "which", lambda name: None)
    assert llm.pick_engine("auto") == "static"
    monkeypatch.setattr(llm.shutil, "which", lambda name: "/usr/bin/claude")
    assert llm.pick_engine("auto") == "claude"
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test")
    _fake_anthropic(monkeypatch, "")
    assert llm.pick_engine("auto") == "anthropic"


def test_claude_cli_missing(monkeypatch):
    monkeypatch.setattr(llm.shutil, "which", lambda name: None)
    with pytest.raises(llm.EngineError, match="not on PATH"):
        llm.run_claude_cli("p")
