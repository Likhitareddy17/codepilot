"""Tests for result persistence, context compression and retrieval (TASK-027 to TASK-029)."""

import json
from pathlib import Path

import pytest

from codepilot.models import ToolResult
from codepilot.tools import ToolContext
from codepilot.tools.history import ResultStore, summarize
from codepilot.tools.registry import build_default_registry


def call(registry, name, **args):
    return registry.execute(name, args)


def fake(call_id, tool="read_file", ok=True, data=None, truncated=False, error=None):
    return ToolResult(call_id=call_id, ok=ok, data=data if data is not None else {"path": "x.py"},
                      error=error, truncated=truncated)


def fill(store, n, tool="read_file"):
    for i in range(1, n + 1):
        store.add(f"c{i:03d}", tool, {"path": f"f{i}.py"}, fake(f"c{i:03d}", tool))


def is_full(text: str) -> bool:
    d = json.loads(text)
    return "data" in d and "key_facts" not in d


# ------------------------------------------------------------- persistence


def test_every_result_is_stored_in_memory_and_on_disk_exactly(registry, ctx):
    results = [
        call(registry, "read_file", path="app/pagination.py"),     # success
        call(registry, "read_file", path="missing.py"),            # tool failure
        call(registry, "read_file"),                               # validation failure
        call(registry, "no_such_tool"),                            # unknown tool
    ]
    assert [r.call_id for r in results] == ["c001", "c002", "c003", "c004"]
    for result in results:
        on_disk = json.loads((ctx.run_dir / "results" / f"{result.call_id}.json").read_text(encoding="utf-8"))
        assert on_disk["result"] == result.to_dict()
        assert ctx.history.get(result.call_id)["result"] == result.to_dict()
    assert [r.call_id for r in ctx.history.records] == ["c001", "c002", "c003", "c004"]
    assert not list((ctx.run_dir / "results").glob("*.tmp"))


def test_records_carry_tool_args_outcome_and_result_path(registry, ctx):
    call(registry, "read_file", path="app/pagination.py")
    record = ctx.history.records[0]
    assert (record.call_id, record.tool, record.ok, record.args) == ("c001", "read_file", True, {"path": "app/pagination.py"})
    assert record.result_path.endswith("c001.json") and Path(record.result_path).is_file()
    assert record.summary["tool"] == "read_file" and record.summary["retrievable"] is True


def test_store_can_reload_results_from_disk(registry, ctx, settings):
    result = call(registry, "read_file", path="app/pagination.py")
    fresh = ResultStore(ctx.run_dir, settings)          # empty memory, same run directory
    assert fresh.get(result.call_id)["result"] == result.to_dict()
    assert fresh.get("c999") is None


def test_a_failed_disk_write_keeps_the_result_in_memory(tmp_path, settings):
    blocker = tmp_path / "not_a_directory"
    blocker.write_text("x")
    store = ResultStore(blocker, settings)              # results/ cannot be created under a file
    record = store.add("c001", "read_file", {}, fake("c001"))
    assert record.result_path == "" and store.write_errors
    assert store.get("c001")["result"]["data"] == {"path": "x.py"}


def test_non_dict_arguments_are_stored_safely(tmp_path, settings):
    store = ResultStore(tmp_path, settings)
    store.add("c001", "read_file", ["not", "a", "dict"], fake("c001"))
    assert "_raw" in store.get("c001")["args"]


def test_call_ids_that_could_escape_the_results_folder_are_ignored(tmp_path, settings):
    store = ResultStore(tmp_path / "run", settings)
    write = tmp_path / "evil.json"
    write.write_text(json.dumps({"call_id": "x", "tool": "read_file", "args": {}, "ok": True, "result": {}}))
    assert store.get("../evil") is None and store.get("c001/../../evil") is None


# --------------------------------------------------- context management


def test_only_the_three_most_recent_results_stay_in_full(tmp_path, settings):
    store = ResultStore(tmp_path, settings)
    fill(store, 6)
    view = store.render_all()
    assert [is_full(view[f"c{i:03d}"]) for i in range(1, 7)] == [False, False, False, True, True, True]
    older = json.loads(view["c001"])
    assert older["retrievable"] is True and older["outcome"] and "key_facts" in older and "data" not in older


@pytest.mark.parametrize("keep,expected_full", [(1, 1), (5, 5), (0, 0)])
def test_recent_count_comes_from_configuration(tmp_path, settings, keep, expected_full):
    settings.history_keep_recent_results = keep
    store = ResultStore(tmp_path, settings)
    fill(store, 8)
    assert sum(is_full(t) for t in store.render_all().values()) == expected_full


def test_patch_results_pinned_results_and_the_latest_test_run_are_never_summarised(tmp_path, settings):
    store = ResultStore(tmp_path, settings)
    store.add("c001", "run_tests", {}, fake("c001", "run_tests", data={"scope": "full", "passed": 1, "failed": 3, "errors": 0, "failing_ids": ["a"], "timed_out": False}))
    store.add("c002", "run_tests", {}, fake("c002", "run_tests", data={"scope": "full", "passed": 2, "failed": 2, "errors": 0, "failing_ids": ["a"], "timed_out": False}))
    store.add("c003", "apply_patch", {}, fake("c003", "apply_patch", data={"path": "a.py", "applied": True, "diff": "-x\n+y\n"}))
    store.add("c004", "read_file", {}, fake("c004"))
    store.add("c005", "read_file", {}, fake("c005"))
    store.add("c006", "read_file", {}, fake("c006"))
    store.add("c007", "read_file", {}, fake("c007"))
    store.add("c008", "read_file", {}, fake("c008"))
    store.pin("c004")
    view = store.render_all()
    assert not is_full(view["c001"])        # an old test run is summarised...
    assert is_full(view["c002"])            # ...but the latest one is not
    assert is_full(view["c003"])            # patch diffs are never summarised
    assert is_full(view["c004"])            # pinned (e.g. the baseline)
    assert not is_full(view["c005"])
    assert all(is_full(view[c]) for c in ("c006", "c007", "c008"))


def test_a_failed_test_run_is_not_treated_as_the_latest_test_result(tmp_path, settings):
    store = ResultStore(tmp_path, settings)
    store.add("c001", "run_tests", {}, fake("c001", "run_tests", data={"scope": "full", "passed": 1, "failed": 1, "errors": 0, "failing_ids": [], "timed_out": False}))
    store.add("c002", "run_tests", {}, fake("c002", "run_tests", ok=False, data={}, error={"code": "NOT_FOUND", "message": "x"}))
    fill_more = [store.add(f"c{i:03d}", "read_file", {}, fake(f"c{i:03d}")) for i in (3, 4, 5)]
    assert is_full(store.render("c001")) and not is_full(store.render("c002"))


# ----------------------------------------------------------- summaries


def test_summaries_keep_the_important_facts_per_tool(registry, ctx):
    call(registry, "list_files")
    call(registry, "read_file", path="app/pagination.py", start_line=1, end_line=2)
    call(registry, "search_code", pattern="paginate")
    call(registry, "read_logs")
    call(registry, "read_metrics")
    call(registry, "apply_patch", path="app/pagination.py", old="page * size", new="page * size  # x")
    s = {r.tool: r.summary for r in ctx.history.records}
    assert s["list_files"]["key_facts"]["files"] > 0 and s["list_files"]["key_facts"]["total_entries"] > 0
    assert s["read_file"]["key_facts"] == {"path": "app/pagination.py", "start_line": 1, "end_line": 2, "total_lines": 3}
    assert s["search_code"]["key_facts"]["match_count"] >= 2
    assert "app/pagination.py" in s["search_code"]["key_facts"]["files"]
    assert s["read_logs"]["key_facts"]["exception_types"] == ["TypeError"]
    assert "operations" in s["read_metrics"]["key_facts"]["top_level_keys"]
    assert s["read_metrics"]["key_facts"]["scalars"]["error_rate"] == 0.18
    assert s["apply_patch"]["key_facts"] == {"path": "app/pagination.py", "applied": True, "lines_added": 1, "lines_removed": 1}


def test_run_tests_and_git_diff_summaries():
    tests = summarize("run_tests", {}, fake("c1", "run_tests", data={"scope": "full", "passed": 4, "failed": 2, "errors": 1,
                      "timed_out": False, "failing_ids": ["t::a", "t::b"]}).to_dict())
    assert tests["key_facts"] == {"scope": "full", "passed": 4, "failed": 2, "errors": 1, "timed_out": False, "failing_ids": ["t::a", "t::b"]}
    assert "4 passed" in tests["outcome"]
    diff = summarize("git_diff", {}, fake("c2", "git_diff", data={"files_changed": ["a.py"], "insertions": 3, "deletions": 1, "diff": "..."}).to_dict())
    assert diff["key_facts"] == {"files_changed": ["a.py"], "insertions": 3, "deletions": 1}


def test_metrics_summary_reports_min_max_and_position_of_the_max():
    metrics = {"service": "x", "latency": [10, 12, 90, 11], "ops": {"orders": {"error_rate": [0.1, 0.4, 0.2]}}}
    s = summarize("read_metrics", {}, fake("c1", "read_metrics", data={"path": "metrics.json", "metrics": metrics}).to_dict())
    assert s["key_facts"]["series"]["latency"] == {"min": 10, "max": 90, "max_index": 2, "length": 4}
    assert s["key_facts"]["series"]["ops.orders.error_rate"]["max_index"] == 1


def test_metrics_summary_understands_reduced_series():
    reduced = {"latency": {"_reduced": True, "length": 2000, "kept": [{"index": 0, "value": 10}, {"index": 700, "value": 9999}, {"index": 1999, "value": 11}]}}
    s = summarize("read_metrics", {}, fake("c1", "read_metrics", data={"path": "m.json", "metrics": reduced}).to_dict())
    assert s["key_facts"]["series"]["latency"] == {"min": 10, "max": 9999, "max_index": 700, "length": 2000}


def test_failed_results_are_summarised_with_their_error_code():
    s = summarize("read_file", {"path": "x"}, fake("c1", ok=False, data={}, error={"code": "NOT_FOUND", "message": "x" * 500}).to_dict())
    assert s["ok"] is False and s["outcome"] == "failed: NOT_FOUND"
    assert s["key_facts"]["error_code"] == "NOT_FOUND" and len(s["key_facts"]["message"]) == 200


def test_summary_records_whether_the_stored_result_was_truncated_and_annotations(tmp_path, settings):
    store = ResultStore(tmp_path, settings)
    store.add("c001", "apply_patch", {}, fake("c001", "apply_patch", truncated=True, data={"path": "a.py", "applied": True, "lines_added": 1, "lines_removed": 1}))
    assert store.record("c001").summary["truncated"] is True
    store.annotate_summary("c001", tests_after={"passed": 5, "failed": 0})
    assert store.record("c001").summary["key_facts"]["tests_after"] == {"passed": 5, "failed": 0}


def test_unknown_tool_summary_is_generic():
    s = summarize("future_tool", {}, fake("c1", "future_tool", data={"b": 1, "a": 2}).to_dict())
    assert s["key_facts"] == {"keys": ["a", "b"]}


# ------------------------------------------------------- get_tool_result


def test_retrieval_returns_the_original_result_exactly(registry, ctx):
    original = call(registry, "read_file", path="app/pagination.py")
    retrieved = call(registry, "get_tool_result", call_id=original.call_id)
    assert retrieved.ok
    d = retrieved.data
    assert (d["call_id"], d["tool"], d["args"], d["ok"]) == ("c001", "read_file", {"path": "app/pagination.py"}, True)
    assert d["result"] == original.to_dict()
    assert ctx.history.retrieval_count == 1 and ctx.history.record("c001").retrieved_count == 1


def test_older_result_is_summarised_in_context_but_retrievable(registry, ctx):
    first = call(registry, "read_file", path="app/pagination.py")
    for _ in range(4):
        call(registry, "list_files", path="app")
    assert not is_full(ctx.history.render(first.call_id))
    assert call(registry, "get_tool_result", call_id=first.call_id).data["result"]["data"]["content"].startswith("def paginate")


def test_a_retrieved_result_counts_as_recent_and_ages_out_like_any_other(registry, ctx):
    first = call(registry, "read_file", path="app/pagination.py")
    for _ in range(3):
        call(registry, "list_files", path="app")
    retrieval = call(registry, "get_tool_result", call_id=first.call_id)
    assert is_full(ctx.history.render(retrieval.call_id))
    assert json.loads(ctx.history.render(retrieval.call_id))["data"]["result"]["data"]["content"]
    for _ in range(3):
        call(registry, "list_files", path="app")
    summary = json.loads(ctx.history.render(retrieval.call_id))
    assert summary["tool"] == "get_tool_result" and summary["key_facts"] == {"call_id": "c001", "tool": "read_file"}


@pytest.mark.parametrize("bad", ["", "c1", "C001", "c001/../x", "../c001", "x" * 5, 5])
def test_malformed_call_ids_are_invalid(registry, bad):
    result = call(registry, "get_tool_result", call_id=bad)
    assert not result.ok and result.error["code"] == "INVALID_ARGUMENTS"


def test_unknown_id_fails_safely_and_is_not_counted(registry, ctx):
    result = call(registry, "get_tool_result", call_id="c999")
    assert not result.ok and result.error["code"] == "RESULT_NOT_FOUND"
    assert ctx.history.retrieval_count == 0


def test_a_retrieval_cannot_be_retrieved(registry, ctx):
    original = call(registry, "read_file", path="app/pagination.py")
    retrieval = call(registry, "get_tool_result", call_id=original.call_id)
    chained = call(registry, "get_tool_result", call_id=retrieval.call_id)
    assert not chained.ok and chained.error["code"] == "INVALID_ARGUMENTS"
    assert ctx.history.retrieval_count == 1           # the rejected attempt used no allowance


def test_retrieval_limit_is_enforced_from_configuration(registry, ctx):
    ctx.settings.max_result_retrievals_per_run = 2
    ctx.history.max_retrievals = 2
    original = call(registry, "read_file", path="app/pagination.py")
    assert call(registry, "get_tool_result", call_id=original.call_id).ok
    assert call(registry, "get_tool_result", call_id=original.call_id).ok
    third = call(registry, "get_tool_result", call_id=original.call_id)
    assert not third.ok and third.error["code"] == "RETRIEVAL_LIMIT_REACHED"
    assert ctx.history.retrieval_count == 2


def test_default_retrieval_limit_is_ten(tmp_path, settings):
    assert ResultStore(tmp_path, settings).max_retrievals == 10
    assert ResultStore(tmp_path, settings).keep_recent == 3


def test_truncated_original_tells_the_model_to_call_the_tool_again(registry, ctx, tiny_repo):
    (tiny_repo / "wide.txt").write_text("y" * 50_000 + "\n")
    original = call(registry, "read_file", path="wide.txt")
    assert original.truncated
    retrieved = call(registry, "get_tool_result", call_id=original.call_id)
    assert retrieved.ok and retrieved.truncated
    assert "narrower" in retrieved.data["note"] and "read_file" in retrieved.data["note"]


def test_retrieval_output_fits_the_output_budget(registry, ctx, tiny_repo):
    (tiny_repo / "big.py").write_text("".join(f"x{i} = {i}\n" for i in range(1, 600)))
    original = call(registry, "read_file", path="big.py")
    retrieved = call(registry, "get_tool_result", call_id=original.call_id)
    assert retrieved.ok
    assert len(json.dumps(retrieved.data)) <= ctx.settings.max_tool_output_chars


def test_retrieval_needs_a_result_store(tiny_repo, settings):
    registry = build_default_registry(ToolContext(root=tiny_repo, settings=settings))
    result = call(registry, "get_tool_result", call_id="c001")
    assert not result.ok and result.error["code"] == "INTERNAL_ERROR"


def test_retrieval_adds_no_new_information(registry, ctx):
    original = call(registry, "read_file", path="app/pagination.py")
    retrieved = call(registry, "get_tool_result", call_id=original.call_id)
    assert retrieved.data["result"] == original.to_dict()   # exactly what the model was already shown
