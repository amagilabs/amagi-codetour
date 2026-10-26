"""Scanning, the import graph, entry points and the file plan, on fixture repos."""

from amagi_codetour.entry import find_entries
from amagi_codetour.graph import build_graph, reverse_edges
from amagi_codetour.plan import build_plan
from amagi_codetour.scan import is_low_value_path, is_test_path, scan
from amagi_codetour.symbols import file_symbols

from conftest import write_tree


def test_scan_skips_binaries_and_vendor_dirs(tmp_path):
    write_tree(tmp_path, {"a.py": "x = 1\n", "node_modules/lib/index.js": "module.exports = 1\n"})
    (tmp_path / "blob.py").write_bytes(b"\x00\x01binary")
    repo = scan(tmp_path, use_git=False)
    assert "a.py" in repo.files
    assert "blob.py" not in repo.files
    assert not any(p.startswith("node_modules/") for p in repo.paths)


def test_test_and_low_value_paths():
    assert is_test_path("tests/test_x.py")
    assert is_test_path("src/crawler.test.ts")
    assert is_test_path("pkg/stats_test.go")
    assert is_test_path("src/main/java/FooTest.java")
    assert not is_test_path("src/contest.py")
    assert not is_test_path("src/app.py")
    assert is_low_value_path("examples/demo.py")
    assert is_low_value_path("vite.config.ts")
    assert not is_low_value_path("src/app.py")


def test_python_graph_resolves_relative_and_module_imports(py_repo):
    repo = scan(py_repo)
    edges = build_graph(repo)
    cli = edges["src/taskq/cli.py"]
    assert cli["src/taskq/config.py"] == {"load_config"}
    assert "JobQueue" in cli["src/taskq/queue.py"]
    # `from . import scheduler` then `scheduler.tick(q)` records `tick`
    assert "tick" in cli["src/taskq/scheduler.py"]
    rev = reverse_edges(edges)
    assert {"src/taskq/queue.py", "src/taskq/store.py", "src/taskq/metrics.py"} <= set(rev["src/taskq/models.py"])


def test_js_graph_uses_tsconfig_alias_and_default_import(js_repo):
    repo = scan(js_repo)
    edges = build_graph(repo)
    cli = edges["src/cli.ts"]
    assert cli["src/crawler.ts"] == {"crawl"}
    assert cli["src/report.ts"] == {"report"}  # via "@/report"
    assert cli["src/args.ts"] == {"default"}
    assert "src/types.ts" in edges["src/report.ts"]


def test_go_and_rust_graphs(go_repo, rust_repo):
    g = build_graph(scan(go_repo))
    assert "internal/stats/stats.go" in g["main.go"]
    r = build_graph(scan(rust_repo))
    assert "src/counter.rs" in r["src/main.rs"]


def test_entry_points_per_language(py_repo, js_repo, go_repo, rust_repo):
    e = find_entries(scan(py_repo), is_test=is_test_path)
    assert e[0].path == "src/taskq/cli.py" and e[0].symbol == "main"
    assert "console script `taskq`" in e[0].reason
    e = find_entries(scan(js_repo), is_test=is_test_path)
    assert e[0].path == "src/cli.ts"  # bin ./dist/cli.js mapped back to src/cli.ts
    assert find_entries(scan(go_repo))[0].path == "main.go"
    assert find_entries(scan(rust_repo))[0].path == "src/main.rs"


def test_symbols_and_summary(py_repo):
    repo = scan(py_repo)
    syms, summary = file_symbols(repo.files["src/taskq/queue.py"])
    names = {s.name: s for s in syms}
    assert summary.startswith("The queue")
    assert names["JobQueue"].kind == "class"
    assert repo.files["src/taskq/queue.py"].lines[names["JobQueue"].line - 1].startswith("class JobQueue")
    assert "JobQueue.put" in names


def test_plan_picks_entry_first_and_skips_tests_examples_and_trivial_init(py_repo):
    repo = scan(py_repo)
    plan = build_plan(repo)
    paths = [p.path for p in plan.picks]
    assert 8 <= len(paths) <= 15
    assert paths[0] == "src/taskq/cli.py"
    assert plan.picks[0].anchor_symbol == "main"
    assert repo.files[paths[0]].lines[plan.picks[0].anchor_line - 1].startswith("def main(")
    assert not any(is_test_path(p) for p in paths)
    assert "examples/demo.py" not in paths
    assert "src/taskq/__init__.py" not in paths
    # breadth-first reading order: direct imports of cli.py come before second-hop files
    assert paths.index("src/taskq/queue.py") < paths.index("src/taskq/store.py")
    assert plan.readme == "README.md"
    assert plan.test_dir == "tests" and plan.test_cmd == "pytest"


def test_plan_anchor_prefers_most_imported_symbol(py_repo):
    plan = build_plan(scan(py_repo))
    by_path = {p.path: p for p in plan.picks}
    assert by_path["src/taskq/models.py"].anchor_symbol == "Job"
    assert by_path["src/taskq/handlers.py"].anchor_symbol == "dispatch"


def test_plan_respects_max_files(py_repo):
    plan = build_plan(scan(py_repo), min_files=2, max_files=4)
    assert len(plan.picks) == 4


def test_plan_rejects_bad_bounds(py_repo):
    import pytest

    with pytest.raises(ValueError):
        build_plan(scan(py_repo), min_files=10, max_files=5)


def test_colocated_tests_get_no_directory_step(js_repo, go_repo):
    plan = build_plan(scan(js_repo))
    assert plan.test_dir == "" and plan.test_cmd == "npm test"
    plan = build_plan(scan(go_repo))
    assert plan.test_dir == "" and plan.test_cmd == "go test ./..."
