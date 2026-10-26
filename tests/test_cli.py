"""The command line, end to end on fixture repos (static engine and a fake model command)."""

import json
import subprocess
import sys

from amagi_codetour import __version__
from amagi_codetour.cli import main

from test_llm import GOOD_STEPS, _reply


def run(*args):
    return main([str(a) for a in args])


def test_version_and_help(capsys):
    proc = subprocess.run([sys.executable, "-m", "amagi_codetour", "--version"], capture_output=True, text=True)
    assert proc.returncode == 0 and proc.stdout.strip() == f"amagi-codetour {__version__}"
    assert run() == 2
    assert "generate" in capsys.readouterr().out


def test_plan_text_and_json(py_repo, capsys):
    assert run("plan", py_repo) == 0
    out = capsys.readouterr().out
    assert "src/taskq/cli.py  (console script `taskq`" in out
    assert " 1. src/taskq/cli.py:17" in out
    assert run("plan", py_repo, "--json") == 0
    data = json.loads(capsys.readouterr().out)
    assert data["picks"][0] == {**data["picks"][0], "path": "src/taskq/cli.py", "line": 17, "symbol": "main"}


def test_generate_static_then_check(py_repo, capsys):
    assert run("generate", py_repo, "--engine", "static") == 0
    out = capsys.readouterr().out
    assert out.startswith("Wrote .tours/getting-started.tour: 11 steps across 10 files (static engine), all anchors verified.")
    tour = json.loads((py_repo / ".tours/getting-started.tour").read_text(encoding="utf-8"))
    assert tour["steps"][1]["file"] == "src/taskq/cli.py"
    assert run("check", py_repo) == 0
    assert "0 broken, 0 warnings -> OK" in capsys.readouterr().out
    # refuses to overwrite without --force
    assert run("generate", py_repo, "--engine", "static") == 2
    assert "amagi-codetour: .tours/getting-started.tour exists; use --force" in capsys.readouterr().err
    assert run("generate", py_repo, "--engine", "static", "--force", "-q") == 0


def test_check_drift_exit_code_and_fix(py_repo, capsys):
    run("generate", py_repo, "--engine", "static", "-q")
    p = py_repo / "src/taskq/worker.py"
    p.write_text("# moved\n# moved\n" + p.read_text(encoding="utf-8"), encoding="utf-8")
    assert run("check", py_repo) == 1
    out = capsys.readouterr().out
    assert "[FAIL] step" in out and "code moved: anchored text is now at line" in out
    assert "Run again with --fix" in out
    assert run("check", py_repo, "--fix") == 0
    out = capsys.readouterr().out
    assert "fixed 1 step(s)" in out
    assert run("check", py_repo, "--json") == 0
    data = json.loads(capsys.readouterr().out)
    assert data[0]["ok"] is True


def test_check_strict_and_explicit_tour(py_repo, tmp_path, capsys):
    tour = {"title": "t", "steps": [{"file": "src/taskq/queue.py", "line": 7, "description": "`JobQueue`"}]}
    t = tmp_path / "mine.tour"
    t.write_text(json.dumps(tour), encoding="utf-8")
    assert run("check", py_repo, "--tour", t) == 0
    assert run("check", py_repo, "--tour", t, "--strict") == 1


def test_check_without_tours_is_usage_error(py_repo, capsys):
    assert run("check", py_repo) == 2
    assert "no .tour files" in capsys.readouterr().err


def test_generate_with_llm_cmd(py_repo, tmp_path, capsys):
    reply = tmp_path / "reply.json"
    reply.write_text(json.dumps(_reply(GOOD_STEPS)), encoding="utf-8")
    fake = tmp_path / "fake_model.py"
    fake.write_text(
        "import sys\nprompt = sys.stdin.read()\nassert '## File: src/taskq/cli.py' in prompt\n"
        f"sys.stdout.write(open(r'{reply}', encoding='utf-8').read())\n",
        encoding="utf-8",
    )
    cmd = f'"{sys.executable}" "{fake}"'
    assert run("generate", py_repo, "--llm-cmd", cmd, "--min-files", "4", "-o", ".tours/llm.tour") == 0
    cap = capsys.readouterr()
    assert "(command engine), all anchors verified." in cap.out
    assert "note: dropped step 4: `src/taskq/nope.py` is not a file in the repo" in cap.err
    tour = json.loads((py_repo / ".tours/llm.tour").read_text(encoding="utf-8"))
    assert tour["title"] == "Tour of taskq"
    assert run("check", py_repo, "--tour", py_repo / ".tours/llm.tour") == 0


def test_generate_llm_failure_exits_1(py_repo, tmp_path, capsys):
    fake = tmp_path / "broken.py"
    fake.write_text("print('I cannot help with that')\n", encoding="utf-8")
    assert run("generate", py_repo, "--llm-cmd", f'"{sys.executable}" "{fake}"') == 1
    assert "no JSON object" in capsys.readouterr().err
    assert not (py_repo / ".tours").exists()


def test_generate_pattern_only_and_stdout(py_repo, capsys):
    assert run("generate", py_repo, "--engine", "static", "--pattern-only", "--stdout", "--no-readme") == 0
    tour = json.loads(capsys.readouterr().out)
    assert tour["steps"][0]["file"] == "src/taskq/cli.py"
    assert all("line" not in s for s in tour["steps"] if "file" in s)
    assert not (py_repo / ".tours").exists()


def test_bad_path_is_usage_error(tmp_path, capsys):
    assert run("plan", tmp_path / "missing") == 2
    assert "not a directory" in capsys.readouterr().err


def test_print_prompt(py_repo, capsys):
    assert run("generate", py_repo, "--print-prompt") == 0
    out = capsys.readouterr().out
    assert out.startswith("# Repository: taskq")
    assert "## File: src/taskq/cli.py" in out
    assert not (py_repo / ".tours").exists()
