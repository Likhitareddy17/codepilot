"""Trace tests (TASK-048): structure, per-call test counts, secret redaction."""

import json

from codepilot.models import StopReason
from codepilot.trace import (
    RunInfo, build_trace, redact_obj, redact_text, verified_success, write_trace,
)
from support_results import make_result

KEY = "gsk_TESTKEY1234567890abcdef"
INFO = RunInfo(repository="calc_repo", model="m", started_at="t0", finished_at="t1", max_iterations=6,
               cleanup="deleted")


def test_redaction_removes_known_keys_and_secret_shapes():
    text = f"key={KEY} Authorization: Bearer abcdef123456 GROQ_API_KEY=hunter2hunter2 password: s3cretvalue"
    cleaned = redact_text(text, [KEY])
    for leaked in (KEY, "abcdef123456", "hunter2hunter2", "s3cretvalue"):
        assert leaked not in cleaned
    assert "GROQ_API_KEY=***" in cleaned


def test_redaction_leaves_ordinary_text_alone():
    text = "AssertionError in test_add: 3 != 5 (tokenizer ok)"
    assert redact_text(text, [KEY, "ab"]) == text      # a very short secret is ignored, not applied everywhere


def test_redact_obj_keeps_json_valid_and_covers_nested_values():
    data = {"a": [f"x {KEY}", {"b": "SECRET_TOKEN=abc123def"}], "n": 3, "ok": True}
    cleaned = redact_obj(data, [KEY])
    assert json.loads(json.dumps(cleaned)) == cleaned
    assert KEY not in json.dumps(cleaned) and "abc123def" not in json.dumps(cleaned)
    assert cleaned["n"] == 3 and cleaned["ok"] is True


def test_trace_has_the_documented_sections():
    trace = build_trace(make_result(), INFO)
    assert {"schema_version", "run", "outcome", "baseline", "final_tests", "tool_calls", "patches", "events",
            "cleanup", "artifacts"} <= set(trace)
    assert trace["run"]["run_id"] == "20261015-101500" and trace["run"]["model"] == "m"
    assert trace["run"]["prompt_version"]
    assert trace["outcome"]["success"] is True and trace["outcome"]["commit"].startswith("a1b2c3")
    assert trace["baseline"]["failing_ids"] and trace["final_tests"]["passed"] == 4
    assert trace["cleanup"] == {"status": "deleted", "workspace_kept": None}
    assert trace["patches"][0]["path"] == "app/calc.py" and "a + b" in trace["patches"][0]["diff"]


def test_every_tool_call_entry_has_the_a7_fields():
    for entry in build_trace(make_result(), INFO)["tool_calls"]:
        assert {"iteration", "tool", "args", "result_summary", "tests_passing", "tests_failing", "timestamp"} <= set(entry)


def test_patch_entry_shows_the_counts_measured_after_it():
    entries = {e["call_id"]: e for e in build_trace(make_result(), INFO)["tool_calls"]}
    assert (entries["c002"]["tests_passing"], entries["c002"]["tests_failing"]) == (2, 2)   # before the patch
    assert (entries["c004"]["tests_passing"], entries["c004"]["tests_failing"]) == (4, 0)   # after it
    assert entries["c004"]["timestamp"] == "2026-10-15T10:15:03+00:00"


def test_controller_runs_are_marked_and_result_files_are_relative():
    entries = {e["call_id"]: e for e in build_trace(make_result(), INFO)["tool_calls"]}
    assert entries["c001"]["source"] == "controller" and entries["c003"]["source"] == "model"
    assert entries["c003"]["result_file"] == "c003.json"      # no run_dir given: file name only


def test_failed_run_is_never_marked_successful():
    result = make_result(StopReason.MAX_ITERATIONS, verified=False)
    trace = build_trace(result, INFO)
    assert trace["outcome"]["success"] is False and trace["outcome"]["commit"] is None
    assert trace["outcome"]["fix_patch"] is None


def test_success_needs_every_condition():
    assert verified_success(make_result())
    unverified = make_result()
    unverified.state.final_verification = False
    assert not verified_success(unverified)
    no_commit = make_result()
    no_commit.commit_hash = None
    assert not verified_success(no_commit)
    failing_final = make_result(final_run=make_result(verified=False).final_tests)
    assert not verified_success(failing_final)


def test_write_trace_creates_valid_redacted_json(tmp_path):
    result = make_result(model_text=f"my key is {KEY}")
    path = write_trace(tmp_path / "run", result, INFO, secrets=[KEY])
    text = path.read_text(encoding="utf-8")
    assert path.name == "run_trace.json" and KEY not in text
    assert json.loads(text)["run"]["task"] == "Fix the failing tests"
    assert not list(path.parent.glob("*.tmp"))


def test_trace_records_the_kept_workspace():
    info = RunInfo("r", "m", "a", "b", 6, cleanup="kept", workspace_kept="/tmp/ws")
    assert build_trace(make_result(), info)["cleanup"] == {"status": "kept", "workspace_kept": "/tmp/ws"}


def test_trace_records_the_settings_that_shape_what_the_model_sees():
    info = RunInfo("r", "m", "a", "b", 14, cleanup="deleted", history_keep_recent_results=8)
    run = build_trace(make_result(), info)["run"]
    assert run["max_iterations"] == 14 and run["history_keep_recent_results"] == 8
