---
name: amagi-codetour
description: Generate a VS Code CodeTour (.tours/*.tour) that walks a newcomer from a repo's entry point through the 8-15 files they need first, with every step anchored to a real line, and check existing tours for anchors that drifted when the code changed. Use when onboarding someone to a codebase, when asked for a guided tour or walkthrough of a repo, or when a .tour file may be out of date.
---

# amagi-codetour

`amagi-codetour` writes CodeTour files from a repo and verifies them against the code. It finds
the entry point, follows the import graph to choose the files worth reading, and writes one step
per important line. Every step stores both `line` and a `pattern` copied from that line, so
`amagi-codetour check` can tell when the code moved under the tour.

## When to use this skill

- Someone new is joining a project and needs a guided path through the code.
- The user asks for a "tour", "walkthrough" or "where do I start" for a repo.
- A repo already has `.tours/*.tour` files and the code has changed since they were written.
- Before a release, to confirm the shipped tour still points at the right lines (`check` exits 1 if not).

## How to use it

```bash
amagi-codetour plan .                      # entry points + the files a tour would cover (no LLM)
amagi-codetour generate .                  # write .tours/getting-started.tour
amagi-codetour generate . --engine static  # no LLM: built from the import graph alone
amagi-codetour check .                     # verify every step of every tour
amagi-codetour check . --fix               # move drifted steps to where their code is now
```

Engines for `generate`:

- `claude` runs `claude -p` (the Claude Code CLI) with the repo's key files in the prompt.
- `anthropic` calls the Anthropic API (`pipx install "amagi-codetour[anthropic]"`, needs `ANTHROPIC_API_KEY`).
- `static` uses no model. Steps are built from the import graph, docstrings and definitions.
- `--llm-cmd "CMD"` sends the prompt on stdin to any command that prints the JSON reply.
- `auto` (the default) picks `anthropic` when a key is set, else `claude` when it is installed, else `static`.

## What to tell the user

- The model never chooses a line number on its own. Each step quotes the line it points at, and
  the tool looks that text up in the file. Steps whose quote cannot be found are dropped, and
  `generate` prints a note for each one.
- `check` fails a step when its file is gone, its line is past the end of the file, or the line no
  longer matches the step's `pattern`. It warns when a description names an identifier (in
  backticks) that does not appear in the file.
- Open the tour in VS Code with the CodeTour extension (`vsls-contrib.codetour`). It loads tours
  from `.tours/` automatically.

## Do not

- Do not edit line numbers by hand to make `check` pass. Run `check --fix`, or regenerate if the
  anchored code was deleted.
- Do not claim the tour's descriptions were verified line by line. `check` verifies anchors and
  identifier names, not every sentence. Read the tour once before sharing it.
