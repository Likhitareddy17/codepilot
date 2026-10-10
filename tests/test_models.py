"""Tests for domain models and result envelopes."""

import pytest
from codepilot.models import (
    StopReason,
    ToolErrorModel,
    ToolResult,
    ToolCall,
    ToolRecord,
    TestRunResult,
    PatchRecord,
    GitResult,
    RunMetadata,
    FinalReportData,
)


def test_stop_reason_values():
    assert StopReason.SUCCESS == "SUCCESS"
    assert StopReason.NO_FAILURES == "NO_FAILURES"
    assert StopReason.MAX_ITERATIONS == "MAX_ITERATIONS"
    assert StopReason.NO_PROGRESS == "NO_PROGRESS"
    assert StopReason.REGRESSION == "REGRESSION"
    assert StopReason.REPEATED_REGRESSION == "REPEATED_REGRESSION"
    assert StopReason.LLM_UNAVAILABLE == "LLM_UNAVAILABLE"
    assert StopReason.BAD_MODEL_OUTPUT == "BAD_MODEL_OUTPUT"
    assert StopReason.PROTECTED_PATH_VIOLATION == "PROTECTED_PATH_VIOLATION"
    assert StopReason.PROTECTED_PATH_NEEDED == "PROTECTED_PATH_NEEDED"
    assert StopReason.INTERNAL_ERROR == "INTERNAL_ERROR"
    assert StopReason.TOOL_LIMIT_EXCEEDED == "TOOL_LIMIT_EXCEEDED"
    assert StopReason.OTHER == "OTHER"


def test_tool_error_model_to_dict():
    err = ToolErrorModel(code="INVALID_ARGUMENT", message="Missing file path")
    assert err.to_dict() == {
        "code": "INVALID_ARGUMENT",
        "message": "Missing file path",
    }


def test_tool_result_envelope():
    # Success result
    res_ok = ToolResult(
        call_id="c014",
        ok=True,
        data={"content": "hello"},
        error=None,
        truncated=False,
    )
    d_ok = res_ok.to_dict()
    assert d_ok["call_id"] == "c014"
    assert d_ok["ok"] is True
    assert d_ok["data"] == {"content": "hello"}
    assert d_ok["error"] is None
    assert d_ok["truncated"] is False

    # Error result with ToolErrorModel
    err_model = ToolErrorModel(code="NOT_FOUND", message="File does not exist")
    res_err = ToolResult(
        call_id="c015",
        ok=False,
        data={},
        error=err_model,
        truncated=False,
    )
    d_err = res_err.to_dict()
    assert d_err["call_id"] == "c015"
    assert d_err["ok"] is False
    assert d_err["error"] == {"code": "NOT_FOUND", "message": "File does not exist"}


def test_tool_call_and_record():
    call = ToolCall(id="call_1", name="read_file", arguments={"path": "main.py"})
    assert call.id == "call_1"
    assert call.name == "read_file"
    assert call.arguments == {"path": "main.py"}

    record = ToolRecord(
        call_id="call_1",
        iteration=1,
        tool="read_file",
        args={"path": "main.py"},
        ok=True,
        summary={"size": 100},
        result_path="runs/123/results/call_1.json",
        retrieved_count=0,
    )
    d = record.to_dict()
    assert d["call_id"] == "call_1"
    assert d["iteration"] == 1
    assert d["tool"] == "read_file"
    assert d["ok"] is True


def test_test_run_result():
    test_res = TestRunResult(
        exit_code=0,
        passed=5,
        failed=0,
        errors=0,
        failing_ids=[],
        duration_s=1.23,
        timed_out=False,
        output_excerpt="5 passed",
    )
    d = test_res.to_dict()
    assert d["exit_code"] == 0
    assert d["passed"] == 5
    assert d["failed"] == 0


def test_patch_record():
    test_res = TestRunResult(exit_code=0, passed=6, failed=0, errors=0)
    patch = PatchRecord(
        iteration=1,
        path="main.py",
        diff="--- a/main.py\n+++ b/main.py\n",
        applied=True,
        reverted=False,
        tests_after=test_res,
    )
    d = patch.to_dict()
    assert d["iteration"] == 1
    assert d["path"] == "main.py"
    assert d["applied"] is True
    assert d["tests_after"]["passed"] == 6


def test_git_run_metadata_and_report():
    git_res = GitResult(ok=True, commit_hash="abcdef", message="baseline", changed_files=["main.py"])
    assert git_res.to_dict()["commit_hash"] == "abcdef"

    meta = RunMetadata(run_id="run_001", task="fix bug", start_time="2026-10-10", repo_path="repo", branch="main")
    assert meta.to_dict()["run_id"] == "run_001"

    report = FinalReportData(
        run_id="run_001",
        status="SUCCESS",
        stop_reason=StopReason.SUCCESS,
        iterations=2,
        patches_applied=1,
        summary_text="Fixed successfully",
    )
    d_rep = report.to_dict()
    assert d_rep["status"] == "SUCCESS"
    assert d_rep["stop_reason"] == "SUCCESS"
    assert d_rep["iterations"] == 2
