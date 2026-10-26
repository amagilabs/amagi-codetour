import textwrap
from pathlib import Path

import pytest


def write_tree(root: Path, files: dict) -> Path:
    for rel, text in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(textwrap.dedent(text).lstrip("\n"), encoding="utf-8")
    return root


def _helpers(prefix: str, n: int = 6) -> str:
    """Filler functions so fixture modules have realistic size (indented to match the dedent)."""
    pad = " " * 8
    out = ["\n"]
    for i in range(n):
        out.append(f"{pad}def _{prefix}_helper_{i}(value):\n{pad}    return value * {i + 1}\n\n\n")
    return "".join(out)


PY_PROJECT = {
    "pyproject.toml": """
        [project]
        name = "taskq"
        version = "0.1.0"

        [project.scripts]
        taskq = "taskq.cli:main"
    """,
    "README.md": """
        # taskq

        A tiny job queue used as a test fixture.
    """,
    "src/taskq/__init__.py": '''
        """taskq: a tiny job queue."""

        __version__ = "0.1.0"
    ''',
    "src/taskq/cli.py": '''
        """Command line for taskq."""

        import argparse

        from .config import load_config
        from .queue import JobQueue
        from .worker import Worker
        from . import scheduler


        def build_parser():
            p = argparse.ArgumentParser(prog="taskq")
            p.add_argument("command")
            return p


        def main(argv=None):
            args = build_parser().parse_args(argv)
            cfg = load_config()
            q = JobQueue(cfg)
            if args.command == "run":
                Worker(q).run_forever()
            elif args.command == "tick":
                scheduler.tick(q)
            return 0
    ''' + _helpers("cli"),
    "src/taskq/config.py": '''
        """Load taskq settings from the environment."""

        import os
        from dataclasses import dataclass


        @dataclass
        class Config:
            db_path: str
            workers: int


        def load_config():
            return Config(os.environ.get("TASKQ_DB", "taskq.db"), int(os.environ.get("TASKQ_WORKERS", "2")))
    ''' + _helpers("config"),
    "src/taskq/models.py": '''
        """Data model for jobs."""

        from dataclasses import dataclass, field


        @dataclass
        class Job:
            id: int
            name: str
            payload: dict = field(default_factory=dict)
            attempts: int = 0
    ''' + _helpers("models"),
    "src/taskq/store.py": '''
        """SQLite persistence for jobs."""

        import sqlite3

        from .models import Job


        class Store:
            def __init__(self, path):
                self.conn = sqlite3.connect(path)

            def save(self, job: Job):
                self.conn.execute("insert into jobs values (?, ?)", (job.id, job.name))

            def next_job(self):
                return None
    ''' + _helpers("store"),
    "src/taskq/queue.py": '''
        """The queue: put jobs in, take jobs out."""

        from .models import Job
        from .store import Store


        class JobQueue:
            def __init__(self, cfg):
                self.store = Store(cfg.db_path)

            def put(self, name, payload=None):
                job = Job(0, name, payload or {})
                self.store.save(job)
                return job

            def take(self):
                return self.store.next_job()
    ''' + _helpers("queue"),
    "src/taskq/worker.py": '''
        """Workers pull jobs and run their handlers."""

        from .handlers import dispatch
        from .retry import with_retry


        class Worker:
            def __init__(self, queue):
                self.queue = queue

            def run_once(self):
                job = self.queue.take()
                if job:
                    with_retry(lambda: dispatch(job))

            def run_forever(self):
                while True:
                    self.run_once()
    ''' + _helpers("worker"),
    "src/taskq/handlers.py": '''
        """Map job names to functions."""

        HANDLERS = {}


        def handler(name):
            def wrap(fn):
                HANDLERS[name] = fn
                return fn
            return wrap


        def dispatch(job):
            return HANDLERS[job.name](job.payload)
    ''' + _helpers("handlers"),
    "src/taskq/retry.py": '''
        """Retry with backoff."""

        import time


        def with_retry(fn, attempts=3):
            for i in range(attempts):
                try:
                    return fn()
                except Exception:
                    time.sleep(2 ** i)
            raise RuntimeError("gave up")
    ''' + _helpers("retry"),
    "src/taskq/scheduler.py": '''
        """Periodic jobs."""

        from .queue import JobQueue


        def tick(queue: JobQueue):
            queue.put("cleanup")
    ''' + _helpers("scheduler"),
    "src/taskq/metrics.py": '''
        """Counters."""

        from .models import Job


        def count(jobs):
            return len([j for j in jobs if isinstance(j, Job)])
    ''' + _helpers("metrics"),
    "tests/test_queue.py": '''
        from taskq.queue import JobQueue


        def test_put():
            assert JobQueue
    ''',
    "examples/demo.py": '''
        from taskq.queue import JobQueue

        if __name__ == "__main__":
            print(JobQueue)
    ''',
}


JS_PROJECT = {
    "package.json": """
        {
          "name": "linkcheck",
          "version": "1.0.0",
          "bin": {"linkcheck": "./dist/cli.js"},
          "main": "dist/index.js",
          "scripts": {"test": "vitest"}
        }
    """,
    "tsconfig.json": """
        {
          // comments are allowed here
          "compilerOptions": {
            "baseUrl": ".",
            "paths": {"@/*": ["src/*"]},
          }
        }
    """,
    "README.md": "# linkcheck\n\nFinds dead links.\n",
    "src/cli.ts": """
        #!/usr/bin/env node
        import { crawl } from "./crawler";
        import { report } from "@/report";
        import parseArgs from "./args";

        export async function run(argv: string[]) {
          const opts = parseArgs(argv);
          const results = await crawl(opts.url);
          report(results);
        }

        run(process.argv.slice(2));
    """,
    "src/index.ts": """
        export { crawl } from "./crawler";
        export { report } from "./report";
    """,
    "src/args.ts": """
        export default function parseArgs(argv: string[]) {
          return { url: argv[0] };
        }
    """,
    "src/crawler.ts": """
        import { fetchPage } from "./http";
        import { extractLinks } from "./parse";

        export async function crawl(url: string) {
          const html = await fetchPage(url);
          return extractLinks(html);
        }
    """,
    "src/http.ts": """
        export async function fetchPage(url: string): Promise<string> {
          const res = await fetch(url);
          return res.text();
        }
    """,
    "src/parse.ts": """
        const LINK = /href="([^"]+)"/g;

        export function extractLinks(html: string): string[] {
          return [...html.matchAll(LINK)].map((m) => m[1]);
        }
    """,
    "src/report.ts": """
        import type { Result } from "./types";

        export function report(results: Result[]) {
          for (const r of results) console.log(r);
        }
    """,
    "src/types.ts": """
        export interface Result {
          url: string;
          ok: boolean;
        }
    """,
    "src/crawler.test.ts": """
        import { crawl } from "./crawler";
        test("crawl", () => expect(crawl).toBeDefined());
    """,
}


GO_PROJECT = {
    "go.mod": "module example.com/gostat\n\ngo 1.22\n",
    "main.go": """
        package main

        import (
        \t"fmt"

        \t"example.com/gostat/internal/stats"
        )

        func main() {
        \tfmt.Println(stats.Mean([]float64{1, 2, 3}))
        }
    """,
    "internal/stats/stats.go": """
        package stats

        // Mean returns the average.
        func Mean(xs []float64) float64 {
        \ttotal := 0.0
        \tfor _, x := range xs {
        \t\ttotal += x
        \t}
        \treturn total / float64(len(xs))
        }
    """,
    "internal/stats/stats_test.go": "package stats\n",
}


RUST_PROJECT = {
    "Cargo.toml": '[package]\nname = "rcount"\nversion = "0.1.0"\n',
    "src/main.rs": """
        mod counter;

        use crate::counter::count_words;

        fn main() {
            println!("{}", count_words("a b c"));
        }
    """,
    "src/counter.rs": """
        /// Count whitespace-separated words.
        pub fn count_words(s: &str) -> usize {
            s.split_whitespace().count()
        }
    """,
}


@pytest.fixture
def py_repo(tmp_path):
    return write_tree(tmp_path / "taskq", PY_PROJECT)


@pytest.fixture
def js_repo(tmp_path):
    return write_tree(tmp_path / "linkcheck", JS_PROJECT)


@pytest.fixture
def go_repo(tmp_path):
    return write_tree(tmp_path / "gostat", GO_PROJECT)


@pytest.fixture
def rust_repo(tmp_path):
    return write_tree(tmp_path / "rcount", RUST_PROJECT)
