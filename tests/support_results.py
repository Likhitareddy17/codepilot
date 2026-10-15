"""Builders for synthetic run results (no Git, no pytest, no subprocess)."""

from typing import Any, Dict, List, Optional

from codepilot.controller import RunResult
from codepilot.models import (
    BaselineEvidence, PatchRecord, PytestOutcome, StopReason, TestRunResult, ToolRecord,
)
from codepilot.state import RunState

ADD, MUL = "tests/test_calc.py::test_add", "tests/test_calc.py::test_mul"
DIFF = "--- a/app/calc.py\n+++ b/app/calc.py\n@@ -1,2 +1,2 @@\n def add(a, b):\n-    return a - b\n+    return a + b\n"
HASH = "a1b2c3d4e5f60718293a4b5c6d7e8f9012345678"


def suite(passed: int, failed: int, failing=(), error: str = "") -> TestRunResult:
    ids = list(failing)
    return TestRunResult(
        exit_code=1 if failed else 0, passed=passed, failed=failed, errors=0, failing_ids=ids, scope="full",
        failure_messages={i: f"AssertionError in {i.split('::')[1]}" for i in ids},
        main_error=error or (f"AssertionError in {ids[0].split('::')[1]}" if ids else None),
        output_excerpt="1 failed" if failed else "4 passed",
    )


def record(call_id: str, iteration: int, tool: str, args: Dict[str, Any], run: Optional[TestRunResult] = None) -> ToolRecord:
    facts: Dict[str, Any] = {}
    if run is not None:
        facts = {"scope": run.scope, "passed": run.passed, "failed": run.failed, "errors": run.errors}
    return ToolRecord(
        call_id=call_id, iteration=iteration, tool=tool, args=args, ok=True,
        summary={"outcome": f"{tool} done", "key_facts": facts, "truncated": False},
        result_path=f"/runs/r1/results/{call_id}.json",
    )


def make_result(
    stop: StopReason = StopReason.SUCCESS,
    *,
    verified: bool = True,
    model_text: Optional[str] = "The add function subtracted instead of adding.",
    reverted: bool = False,
    final_run: Optional[TestRunResult] = None,
) -> RunResult:
    """A two-iteration run: baseline 2/4 passing, one patch, then the full suite."""
    baseline = suite(2, 2, [ADD, MUL])
    final = final_run or (suite(4, 0) if verified else suite(3, 1, [MUL]))
    state = RunState(run_id="20261015-101500", task="Fix the failing tests", workspace_path="/tmp/ws",
                     branch="codepilot/fix-20261015-101500")
    state.baseline, state.latest_tests, state.iteration = baseline, final, 2
    state.final_verification = state.commit_state = verified
    state.changed_files = ["app/calc.py"] if verified else []
    state.files_inspected = ["app/calc.py"]
    state.patches = [PatchRecord(iteration=2, path="app/calc.py", diff=DIFF, applied=True, reverted=reverted)]
    state.tool_records = [
        record("c001", 0, "run_tests", {"source": "baseline"}, baseline),
        record("c002", 1, "list_files", {}),
        record("c003", 2, "read_file", {"path": "app/calc.py"}),
        record("c004", 2, "apply_patch", {"path": "app/calc.py", "old": "a - b", "new": "a + b"}),
        record("c005", 2, "run_tests", {"source": "after_patch"}, final),
        record("c006", 2, "run_tests", {"source": "final_verification"}, final),
    ]
    events: List[Dict[str, Any]] = [
        {"event": "iteration_start", "iteration": 1, "timestamp": "2026-10-15T10:15:00+00:00"},
        {"event": "llm_response", "iteration": 2, "timestamp": "2026-10-15T10:15:02+00:00",
         "tool_calls": ["apply_patch"], "has_text": bool(model_text), "usage": {}, "text": model_text or ""},
        {"event": "tool_call", "iteration": 2, "timestamp": "2026-10-15T10:15:03+00:00", "tool": "apply_patch",
         "call_id": "c004", "ok": True, "error": None},
    ]
    return RunResult(
        state=state, stop_reason=stop, summary="The full test suite passes and the fix was committed (a1b2c3d4).",
        baseline=BaselineEvidence(status=BaselineEvidence.NEEDS_REPAIR, reason="2 failing",
                                  outcome=PytestOutcome(result=baseline)),
        final_tests=final, commit_hash=HASH if verified else None,
        patch_path="/runs/r1/fix.patch" if verified else None, events=events, duration_s=1.5,
    )
