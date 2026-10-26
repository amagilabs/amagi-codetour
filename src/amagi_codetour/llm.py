"""Ask Claude to write the tour, then hold every step it returns against the code.

Engines:
  claude     the Claude Code CLI (`claude -p`), using whatever login it already has
  anthropic  the Anthropic API through the official SDK (`pip install amagi-codetour[anthropic]`)
  command    any shell command that reads the prompt on stdin and prints the reply
             (`--llm-cmd`), for other models or for tests

Claude never gets to invent a line number: each step must quote the exact text
of the line it points at, and `resolve_steps` finds that text in the file.
Steps whose file or quoted line cannot be found are dropped and reported.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile

from .anchors import locate
from .check import norm_path
from .tour import SCHEMA_URL, make_step, readme_step, static_description, tests_step

DEFAULT_MODEL = "claude-opus-5"
MAX_PROMPT_CHARS = 180_000
MAX_LINES_PER_FILE = 320
FILES_IN_PROMPT = 24

SYSTEM = (
    "You write onboarding tours for the VS Code CodeTour extension. A tour is a sequence of "
    "steps, each pinned to one line of one file, that a developer new to the codebase clicks "
    "through in order. You only describe what the code shown to you actually does."
)


class EngineError(RuntimeError):
    pass


def _numbered(lines, limit):
    out = []
    width = len(str(min(len(lines), limit)))
    for i, text in enumerate(lines[:limit], 1):
        out.append(f"{str(i).rjust(width)}| {text}")
    if len(lines) > limit:
        out.append(f"... ({len(lines) - limit} more lines not shown)")
    return "\n".join(out)


def build_prompt(repo, plan, min_files: int = 8, max_files: int = 15, title: str = "") -> str:
    parts = []
    parts.append(f"# Repository: {plan.name}")
    if plan.languages:
        parts.append("Languages (file counts): " + ", ".join(f"{k} {v}" for k, v in plan.languages.items()))
    if plan.readme:
        rl = repo.read(plan.readme) or []
        parts.append(f"## {plan.readme} (first 60 lines)\n" + "\n".join(rl[:60]))
    tree = [p for p in repo.paths if not p.startswith(".")][:400]
    parts.append("## Files\n" + "\n".join(tree) + ("\n... (more files not listed)" if len(repo.paths) > 400 else ""))
    if plan.entries:
        parts.append("## Entry point candidates (found statically, strongest first)\n" + "\n".join(
            f"- {e.path}: {e.reason}" for e in plan.entries[:6]))
    parts.append("## Static ranking of important files (import graph + entry points)\n" + "\n".join(
        f"- {f.path} (score {f.score}): " + "; ".join(f.reasons[:4]) for f in plan.ranked[:30]))
    budget = MAX_PROMPT_CHARS - sum(len(p) for p in parts) - 6000
    shown = 0
    for f in plan.ranked[:FILES_IN_PROMPT]:
        lines = repo.read(f.path) or []
        block = f"## File: {f.path}\n```\n{_numbered(lines, MAX_LINES_PER_FILE)}\n```"
        if len(block) > budget:
            if shown >= min_files:
                break
            block = f"## File: {f.path}\n```\n{_numbered(lines, 120)}\n```"
            if len(block) > budget:
                break
        parts.append(block)
        budget -= len(block)
        shown += 1
    want_title = title or f"Getting started with {plan.name}"
    parts.append(f"""## Task
Write a CodeTour for a developer who has never seen this repository.

Rules:
1. Start at the real entry point (where execution begins), then follow the code path a newcomer needs, in reading order.
2. Cover between {min_files} and {max_files} different files (fewer only if the repository has fewer meaningful files). Use 1 or 2 steps per file. Skip tests, fixtures and generated code.
3. Every step points at one line from the numbered listings above. Give that line number in "line" and copy the line's text exactly, without the number prefix, into "anchor". Pick a line that matters: a function or class definition, a dispatch, a key call. Never a blank line.
4. "description" is GitHub markdown, 2 to 5 sentences: what this code does, why it matters, and how it connects to the previous or next step. Only name functions, classes and variables that appear in the code shown. Wrap code names in backticks.
5. "title" is short (2 to 6 words).
6. Only use files whose contents are shown above.

Reply with JSON only, no prose and no code fence, in exactly this shape:
{{"title": "{want_title}", "description": "one sentence about the tour", "steps": [{{"file": "path/from/repo/root", "line": 12, "anchor": "exact text of line 12", "title": "short title", "description": "markdown"}}]}}""")
    return "\n\n".join(parts)


def parse_response(text: str) -> dict:
    if not text or not text.strip():
        raise EngineError("the model returned an empty reply")
    s = text.strip()
    fence = re.search(r"```(?:json)?\s*(\{.*\})\s*```", s, re.S)
    if fence:
        s = fence.group(1)
    start, end = s.find("{"), s.rfind("}")
    if start < 0 or end <= start:
        raise EngineError("no JSON object in the model reply")
    try:
        data = json.loads(s[start:end + 1])
    except ValueError as e:
        raise EngineError(f"model reply is not valid JSON: {e}") from None
    if not isinstance(data, dict) or not isinstance(data.get("steps"), list):
        raise EngineError("model reply has no `steps` list")
    return data


def resolve_steps(repo, plan, data: dict, min_files: int = 8, max_files: int = 15,
                  with_readme: bool = True, with_tests: bool = True, title: str = ""):
    """Turn the model's steps into verified CodeTour steps. Returns (tour, notes)."""
    notes = []
    steps = []
    files = []
    per_file = {}
    readme = plan.readme
    for i, raw in enumerate(data.get("steps", []), 1):
        if not isinstance(raw, dict):
            notes.append(f"dropped step {i}: not an object")
            continue
        desc = raw.get("description")
        if not isinstance(desc, str) or not desc.strip():
            notes.append(f"dropped step {i}: no description")
            continue
        f = norm_path(raw.get("file", ""))
        if not f:
            notes.append(f"dropped step {i}: no file")
            continue
        lines = repo.read(f)
        if lines is None or f not in repo.paths:
            notes.append(f"dropped step {i}: `{f}` is not a file in the repo")
            continue
        if f == readme:
            continue  # we add our own overview step
        try:
            hint = int(raw.get("line") or 0)
        except (TypeError, ValueError):
            hint = 0
        anchor = raw.get("anchor") if isinstance(raw.get("anchor"), str) else ""
        line, how = locate(anchor, lines, hint)
        if line is None:
            notes.append(f"dropped step {i}: {f}: {how} ({anchor.strip()[:60]!r})")
            continue
        if how == "fuzzy":
            notes.append(f"step {i}: {f}: quoted line was not exact; anchored to the closest match, line {line}")
        elif hint and line != hint:
            notes.append(f"step {i}: {f}: model said line {hint}, quoted text is on line {line}; using {line}")
        if f not in per_file:
            if len(files) >= max_files:
                notes.append(f"dropped step {i}: already {max_files} files")
                continue
            files.append(f)
            per_file[f] = 0
        if per_file[f] >= 3:
            notes.append(f"dropped step {i}: more than 3 steps in {f}")
            continue
        per_file[f] += 1
        title_s = raw.get("title") if isinstance(raw.get("title"), str) and raw.get("title").strip() else os.path.basename(f)
        steps.append(make_step(repo, f, line, title_s.strip(), desc.strip()))
    if len(files) < min_files:
        extra = [p for p in plan.picks if p.path not in per_file]
        need = min_files - len(files)
        for pick in extra[:need]:
            steps.append(make_step(repo, pick.path, pick.anchor_line, f"{pick.anchor_symbol or os.path.basename(pick.path)}: {os.path.basename(pick.path)}",
                                   static_description(pick, len(steps), len(plan.picks))))
            files.append(pick.path)
        if extra[:need]:
            notes.append(f"the model covered fewer than {min_files} files; added {len(extra[:need])} from the static plan")
    if not steps:
        raise EngineError("none of the model's steps could be anchored to the code")
    out = []
    if with_readme and readme:
        out.append(readme_step(plan, repo))
    out += steps
    if with_tests and plan.test_dir:
        out.append(tests_step(plan))
    tour = {
        "$schema": SCHEMA_URL,
        "title": title or (data.get("title") if isinstance(data.get("title"), str) and data.get("title").strip() else f"Getting started with {plan.name}"),
        "description": (data.get("description") if isinstance(data.get("description"), str) else "") or f"A guided walk through {len(files)} files of {plan.name}.",
        "isPrimary": True,
        "steps": out,
    }
    return tour, notes


# ---------------------------------------------------------------- engines

def run_claude_cli(prompt: str, model: str = "", timeout: int = 900) -> str:
    exe = shutil.which("claude")
    if not exe:
        raise EngineError("the `claude` CLI is not on PATH (install Claude Code, or use --engine anthropic / static)")
    cmd = [exe, "-p", "--output-format", "text"]
    if model:
        cmd += ["--model", model]
    with tempfile.TemporaryDirectory(prefix="amagi-codetour-") as tmp:
        try:
            proc = subprocess.run(cmd, input=SYSTEM + "\n\n" + prompt, capture_output=True, text=True,
                                  encoding="utf-8", errors="replace", timeout=timeout, cwd=tmp)
        except subprocess.TimeoutExpired:
            raise EngineError(f"`claude -p` did not answer within {timeout}s") from None
    if proc.returncode != 0:
        raise EngineError(f"`claude -p` exited {proc.returncode}: {(proc.stderr or proc.stdout).strip()[:400]}")
    return proc.stdout


def run_anthropic(prompt: str, model: str = "", timeout: int = 900) -> str:
    try:
        import anthropic
    except ImportError:
        raise EngineError("the anthropic package is not installed: pipx install 'amagi-codetour[anthropic]'") from None
    client = anthropic.Anthropic(timeout=float(timeout))
    try:
        with client.messages.stream(
            model=model or os.environ.get("AMAGI_CODETOUR_MODEL") or DEFAULT_MODEL,
            max_tokens=16000,
            system=SYSTEM,
            messages=[{"role": "user", "content": prompt}],
        ) as stream:
            msg = stream.get_final_message()
    except anthropic.APIError as e:
        raise EngineError(f"Anthropic API error: {e}") from None
    if msg.stop_reason == "refusal":
        raise EngineError("the model declined the request")
    return "".join(b.text for b in msg.content if b.type == "text")


def run_command(prompt: str, command: str, timeout: int = 900) -> str:
    try:
        proc = subprocess.run(command, shell=True, input=prompt, capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=timeout)
    except subprocess.TimeoutExpired:
        raise EngineError(f"`{command}` did not answer within {timeout}s") from None
    if proc.returncode != 0:
        raise EngineError(f"`{command}` exited {proc.returncode}: {(proc.stderr or proc.stdout).strip()[:400]}")
    return proc.stdout


def pick_engine(engine: str, llm_cmd: str = "") -> str:
    if llm_cmd:
        return "command"
    if engine != "auto":
        return engine
    if os.environ.get("ANTHROPIC_API_KEY"):
        try:
            import anthropic  # noqa: F401
            return "anthropic"
        except ImportError:
            pass
    if shutil.which("claude"):
        return "claude"
    return "static"


def ask(engine: str, prompt: str, model: str = "", llm_cmd: str = "", timeout: int = 900) -> str:
    if engine == "command":
        return run_command(prompt, llm_cmd, timeout)
    if engine == "claude":
        return run_claude_cli(prompt, model, timeout)
    if engine == "anthropic":
        return run_anthropic(prompt, model, timeout)
    raise EngineError(f"unknown engine {engine!r}")
