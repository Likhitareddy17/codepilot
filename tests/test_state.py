"""Tests for CodePilot run state."""

import pytest
from codepilot.state import RunState
from codepilot.models import StopReason, TestRunResult, PatchRecord, ToolRecord, ToolCall


def test_run_state_initialization_and_serialization():
    baseline = TestRunResult(exit_code=1, passed=4, failed=2, errors=0, failing_ids=["test_a", "test_b"])
    state = RunState(
        run_id="run_123",
        task="Fix bug in main",
        workspace_path="/tmp/workspace",
        branch="codepilot/fix-123",
        baseline=baseline,
        stop_reason=StopReason.SUCCESS,
    )

    assert state.run_id == "run_123"
    assert state.iteration == 0
    assert state.status == "running"
    assert state.baseline.failed == 2

    d = state.to_dict()
    assert d["run_id"] == "run_123"
    assert d["task"] == "Fix bug in main"
    assert d["baseline"]["failed"] == 2
    assert d["stop_reason"] == "SUCCESS"
    assert d["patches"] == []
    assert d["files_inspected"] == []
