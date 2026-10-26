# amagi-codetour

Generate a VS Code CodeTour for any repo: Claude traces the entry point and the 8 to 15 files a newcomer needs, writes one step per important line, and every step is checked against the code before the tour is saved.

![amagi-codetour planning a tour, then catching steps that drifted after an edit and moving them back](https://raw.githubusercontent.com/amagilabs/amagi-codetour/main/assets/demo.gif)

## Install

```bash
pipx install amagi-codetour
```

Not on PyPI yet. Until it is, install from a clone with `pipx install .` or `pip install .`

Python 3.9 or newer, no required dependencies. To write the tour with Claude you need either the [Claude Code](https://docs.anthropic.com/en/docs/claude-code) CLI (`claude`) logged in, or an Anthropic API key with `pipx install "amagi-codetour[anthropic]"`. Without either, `--engine static` builds the tour from the import graph alone.

Open the result in VS Code with the [CodeTour extension](https://marketplace.visualstudio.com/items?itemName=vsls-contrib.codetour). It picks up `.tours/` on its own.

## Usage

```bash
amagi-codetour plan .                      # entry points and the files a tour would cover (no LLM)
amagi-codetour generate .                  # write .tours/getting-started.tour
amagi-codetour check .                     # verify every step of every tour
amagi-codetour check . --fix               # move drifted steps to where their code is now
```

`plan` shows what the tour will cover before any model is called. This is the real output on this repo:

```
$ amagi-codetour plan .
PLAN_OUTPUT
```

The number in brackets is the file's score: entry points, distance from the entry point, how many other files import it, and its size. The symbol is the line each step will point at.

`generate` sends the numbered source of those files to Claude and asks for a tour where every step quotes the line it points at. The tool then looks each quote up in the file. A step whose quote is not in the file is dropped, with a note. A step whose line number is off is moved to the line that holds the quote. Then the finished tour goes through `check` before it is written:

```
$ amagi-codetour generate .
GENERATE_OUTPUT
```

Each step stores the line number and a `pattern` copied from that line, which is how `check` tells when the code moved under the tour:

```json
STEP_JSON
```

### Keeping tours accurate

Tours go stale quietly. Someone adds a function above the one a step points at, and the step now lands on the wrong line. `check` catches that. This is what it printed on this repo after two small edits to `cli.py` and `plan.py`:

```
$ amagi-codetour check .
CHECK_FAIL
$ amagi-codetour check . --fix
FIX_OUTPUT
```

`check` exits 1 when any step is broken, so it can run in CI or a pre-commit hook. A step fails when its file is gone, its line is past the end of the file, or the line no longer matches its `pattern`. It warns when a description names an identifier in backticks that does not appear in the file. `--strict` turns warnings into failures. `--fix` only moves steps whose anchored line still exists somewhere in the file. If the code was deleted, regenerate.

### Engines

| `--engine` | What writes the steps |
|---|---|
| `auto` (default) | `anthropic` if `ANTHROPIC_API_KEY` is set and the SDK is installed, else `claude` if it is on PATH, else `static` |
| `claude` | `claude -p`, using the login Claude Code already has |
| `anthropic` | The Anthropic API. Model from `--model`, `AMAGI_CODETOUR_MODEL`, or `claude-opus-5` |
| `static` | No model. Steps come from docstrings, definitions and the import graph |

`--llm-cmd "CMD"` sends the prompt on stdin to any command and reads the JSON reply from its stdout, so you can use another model or a script. `generate --print-prompt` prints the prompt and exits, if you want to paste it into a chat yourself.

Other options: `--min-files` and `--max-files` (default 8 and 15), `--title`, `-o` for the output path, `--stdout`, `--no-readme`, and `--pattern-only` for steps that store a pattern but no line number. `plan --json` and `check --json` print machine-readable results.

The import graph understands Python, JavaScript and TypeScript (including `tsconfig` path aliases), Go and Rust. Entry points come from `pyproject.toml` scripts, `__main__.py`, `package.json` `bin` and `main`, Go `main` packages, Rust `src/main.rs` and `src/bin/`, Java and C# `main` methods, Ruby `bin/`, and common names like `main.py`, `app.py` and `index.ts`.

## Claude Code skill

[`.claude/skills/amagi-codetour/SKILL.md`](.claude/skills/amagi-codetour/SKILL.md) teaches Claude Code when to run the tool: onboarding someone, a request for a walkthrough of a repo, or a repo whose tours may be out of date. Copy the folder into `~/.claude/skills/` or into a project's `.claude/skills/`.

## Limits

- `check` verifies anchors and identifier names. It does not check that every sentence of a description is true. Read the tour once before you share it.
- The `anthropic` engine is covered by unit tests against a stand-in SDK, not by a live API call. The `claude` engine was run for real to write this repo's own tour in [`.tours/`](.tours/).
- Other languages still get a tour, but without an import graph the files are ranked by entry points, names and size only.
- Very large repos are cut to fit the prompt: the top 24 ranked files, up to 320 lines each.

## Star

If this got someone through their first day in a codebase, a star helps other people find it.

## More from AmagiLabs

[amagi-legal-review-skills](https://github.com/amagilabs/amagi-legal-review-skills) is a pack of Claude Code skills for a first-pass review of NDAs, MSAs and leases. [github.com/amagilabs](https://github.com/amagilabs) has the other small open source dev tools from the same lab.

## License

MIT, see [LICENSE](LICENSE). Copyright (c) 2026 Amagisolutionsgroup, Inc. (AmagiLabs).
