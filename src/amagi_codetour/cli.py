"""amagi-codetour command line: generate, plan, check."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import __version__
from .check import apply_fixes, check_tour, norm_path
from .plan import build_plan
from .scan import scan
from .tour import find_tours, load_tour, static_tour, write_tour

DEFAULT_OUT = ".tours/getting-started.tour"


def _err(msg: str) -> None:
    print(f"amagi-codetour: {msg}", file=sys.stderr)


def _root(path: str) -> Path:
    root = Path(path).resolve()
    if not root.is_dir():
        raise SystemExit(_usage_error(f"not a directory: {path}"))
    return root


def _usage_error(msg: str) -> int:
    _err(msg)
    return 2


def _print_report(rep, root: Path, quiet: bool = False) -> None:
    try:
        shown = Path(rep.path).resolve().relative_to(root).as_posix()
    except ValueError:
        shown = rep.path
    n_fail, n_warn = len(rep.failed), len(rep.warned)
    status = "OK" if rep.ok and not n_warn else ("OK with warnings" if rep.ok else "FAIL")
    print(f"{shown}: {rep.title!r}: {len(rep.steps)} steps, {n_fail} broken, {n_warn} warnings -> {status}")
    for e in rep.schema:
        print(f"  schema: {e}")
    for s in rep.steps:
        if s.status == "ok" or (quiet and s.status == "warn"):
            continue
        tag = "FAIL" if s.status == "fail" else "warn"
        print(f"  [{tag}] step {s.index} {s.target}: " + "; ".join(s.messages))


# ------------------------------------------------------------------ plan

def cmd_plan(args) -> int:
    root = _root(args.path)
    repo = scan(root)
    try:
        plan = build_plan(repo, args.min_files, args.max_files)
    except ValueError as e:
        return _usage_error(str(e))
    if args.json:
        out = {
            "name": plan.name,
            "languages": plan.languages,
            "entries": [{"path": e.path, "reason": e.reason, "strength": e.strength} for e in plan.entries],
            "picks": [{"path": p.path, "score": p.score, "line": p.anchor_line, "symbol": p.anchor_symbol,
                       "why": p.anchor_why, "reasons": p.reasons} for p in plan.picks],
            "readme": plan.readme, "tests": plan.test_dir, "test_cmd": plan.test_cmd,
        }
        print(json.dumps(out, indent=2))
        return 0
    print(f"{plan.name}: {len(repo.files)} source files ({', '.join(f'{k} {v}' for k, v in plan.languages.items()) or 'none'})")
    if plan.entries:
        print("Entry points:")
        for e in plan.entries[:5]:
            print(f"  {e.path}  ({e.reason})")
    else:
        print("Entry points: none found; ranking by imports and names only")
    print(f"Tour order ({len(plan.picks)} files):")
    width = max((len(f"{p.path}:{p.anchor_line}") for p in plan.picks), default=0)
    for i, p in enumerate(plan.picks, 1):
        loc = f"{p.path}:{p.anchor_line}"
        print(f"  {i:>2}. {loc.ljust(width)}  {p.anchor_symbol or '-'}  [{p.score:g}]")
    return 0


# ------------------------------------------------------------------ check

def _tour_paths(root: Path, given):
    if given:
        return [Path(t) if Path(t).is_absolute() else (Path.cwd() / t) for t in given]
    return find_tours(root)


def cmd_check(args) -> int:
    root = _root(args.path)
    paths = _tour_paths(root, args.tour)
    if not paths:
        _err(f"no .tour files in {root} (looked in .tours/, .vscode/tours/, .github/tours/)")
        return 2
    repo = scan(root)
    reports = []
    any_fail = False
    any_warn = False
    for p in paths:
        try:
            tour = load_tour(p)
        except (OSError, ValueError) as e:
            _err(f"{p}: cannot read tour: {e}")
            any_fail = True
            continue
        rep = check_tour(repo, tour, str(p), check_mentions=not args.no_mentions)
        if args.fix and (rep.fixable or any(s.status in {"ok", "warn"} for s in rep.steps)):
            n = apply_fixes(repo, tour, rep)
            if n:
                write_tour(tour, p)
                print(f"{p.name}: fixed {n} step(s)")
                rep = check_tour(repo, tour, str(p), check_mentions=not args.no_mentions)
        reports.append(rep)
        any_fail |= not rep.ok
        any_warn |= bool(rep.warned)
    if args.json:
        print(json.dumps([{
            "tour": r.path, "title": r.title, "ok": r.ok, "schema": r.schema,
            "steps": [{"step": s.index, "target": s.target, "status": s.status, "messages": s.messages,
                       "moved_to": s.new_line} for s in r.steps],
        } for r in reports], indent=2))
    else:
        for r in reports:
            _print_report(r, root, args.quiet)
        if any_fail and not args.fix and any(r.fixable for r in reports):
            print("Run again with --fix to move drifted steps to where their code is now.")
    if any_fail or (args.strict and any_warn):
        return 1
    return 0


# ------------------------------------------------------------------ generate

def cmd_generate(args) -> int:
    from . import llm

    root = _root(args.path)
    repo = scan(root)
    if not repo.files:
        _err(f"no source files found under {root}")
        return 2
    try:
        plan = build_plan(repo, args.min_files, args.max_files)
    except ValueError as e:
        return _usage_error(str(e))
    if args.print_prompt:
        # for pasting into any chat model; the reply can be fed back with --llm-cmd "cat reply.json"
        print(llm.build_prompt(repo, plan, args.min_files, args.max_files, args.title))
        return 0
    engine = llm.pick_engine(args.engine, args.llm_cmd)
    with_readme = not args.no_readme
    notes = []
    if engine == "static":
        tour = static_tour(repo, plan, args.title, with_readme=with_readme)
    else:
        if not args.quiet:
            print(f"Asking {engine} to trace {plan.name} ({len(plan.ranked)} candidate files)...", file=sys.stderr)
        prompt = llm.build_prompt(repo, plan, args.min_files, args.max_files, args.title)
        try:
            reply = llm.ask(engine, prompt, model=args.model, llm_cmd=args.llm_cmd, timeout=args.timeout)
            data = llm.parse_response(reply)
            tour, notes = llm.resolve_steps(repo, plan, data, args.min_files, args.max_files,
                                            with_readme=with_readme, title=args.title)
        except llm.EngineError as e:
            _err(f"{engine} engine failed: {e}")
            if args.engine == "auto" and not args.llm_cmd:
                _err("falling back to --engine static")
                engine = "static"
                tour = static_tour(repo, plan, args.title, with_readme=with_readme)
            else:
                return 1
    if args.pattern_only:
        for s in tour["steps"]:
            if "pattern" in s and "line" in s:
                del s["line"]

    report = check_tour(repo, tour, "(generated)")
    if args.stdout:
        print(json.dumps(tour, indent=2, ensure_ascii=False))
    else:
        out = Path(args.output)
        if not out.is_absolute():
            out = root / out
        if out.exists() and not args.force:
            _err(f"{args.output} exists; use --force to overwrite or -o to pick another name")
            return 2
        write_tour(tour, out)
        files = {norm_path(s["file"]) for s in tour["steps"] if "file" in s}
        verdict = "all anchors verified" if report.ok else f"{len(report.failed)} anchors FAILED verification"
        try:
            shown = out.relative_to(root).as_posix()
        except ValueError:
            shown = str(out)
        print(f"Wrote {shown}: {len(tour['steps'])} steps across {len(files)} files ({engine} engine), {verdict}.")
    sys.stdout.flush()
    if not args.quiet:
        for n in notes:
            print(f"  note: {n}", file=sys.stderr)
        for s in report.steps:
            if s.status != "ok":
                print(f"  [{s.status}] step {s.index} {s.target}: " + "; ".join(s.messages), file=sys.stderr)
    return 0 if report.ok else 1


# ------------------------------------------------------------------ main

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="amagi-codetour",
        description="Generate VS Code CodeTour walkthroughs for any repo, and check that their line anchors still match the code.",
    )
    p.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = p.add_subparsers(dest="command", metavar="COMMAND")

    g = sub.add_parser("generate", help="trace the repo and write a .tour file")
    g.add_argument("path", nargs="?", default=".", help="repository root (default: .)")
    g.add_argument("-o", "--output", default=DEFAULT_OUT, help=f"tour file, relative to the repo (default: {DEFAULT_OUT})")
    g.add_argument("--title", default="", help="tour title (default: 'Getting started with <repo>')")
    g.add_argument("--engine", choices=["auto", "static", "claude", "anthropic"], default="auto",
                   help="who writes the tour: static (no LLM), claude (Claude Code CLI), anthropic (API). "
                        "auto picks anthropic if ANTHROPIC_API_KEY is set, else claude if installed, else static")
    g.add_argument("--llm-cmd", default="", help="any command that reads the prompt on stdin and prints JSON (overrides --engine)")
    g.add_argument("--model", default="", help="model name passed to the claude/anthropic engine")
    g.add_argument("--timeout", type=int, default=900, help="seconds to wait for the model (default: 900)")
    g.add_argument("--min-files", type=int, default=8, help="fewest files to cover (default: 8)")
    g.add_argument("--max-files", type=int, default=15, help="most files to cover (default: 15)")
    g.add_argument("--no-readme", action="store_true", help="do not open the tour on the README")
    g.add_argument("--pattern-only", action="store_true", help="anchor steps by pattern only (no line numbers)")
    g.add_argument("--stdout", action="store_true", help="print the tour instead of writing it")
    g.add_argument("--print-prompt", action="store_true", help="print the prompt the LLM engines would send, then exit")
    g.add_argument("--force", action="store_true", help="overwrite an existing tour file")
    g.add_argument("-q", "--quiet", action="store_true", help="only print the summary line")
    g.set_defaults(func=cmd_generate)

    pl = sub.add_parser("plan", help="show the entry points and files a tour would cover (no LLM)")
    pl.add_argument("path", nargs="?", default=".")
    pl.add_argument("--min-files", type=int, default=8)
    pl.add_argument("--max-files", type=int, default=15)
    pl.add_argument("--json", action="store_true")
    pl.set_defaults(func=cmd_plan)

    c = sub.add_parser("check", help="verify every step of existing tours against the code")
    c.add_argument("path", nargs="?", default=".")
    c.add_argument("--tour", action="append", help="tour file to check (repeatable; default: every tour in the repo)")
    c.add_argument("--fix", action="store_true", help="move drifted steps to where their anchored code is now")
    c.add_argument("--strict", action="store_true", help="exit 1 on warnings too")
    c.add_argument("--no-mentions", action="store_true", help="skip checking `names` in descriptions")
    c.add_argument("--json", action="store_true")
    c.add_argument("-q", "--quiet", action="store_true", help="hide warnings, show only broken steps")
    c.set_defaults(func=cmd_check)
    return p


def main(argv=None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):
            pass
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "command", None):
        parser.print_help()
        return 2
    try:
        return args.func(args)
    except SystemExit as e:
        return e.code if isinstance(e.code, int) else 2
    except KeyboardInterrupt:
        return 130
