"""Run state and persistence state for CodePilot."""

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Union
from codepilot.models import (
    PatchRecord, RunComparison, StopReason, TestRunResult, ToolCall, ToolRecord,
)


@dataclass
class RunState:
    """Tracks the state of a CodePilot execution run."""

    run_id: str
    task: str
    workspace_path: str
    branch: str
    iteration: int = 0
    status: str = "running"  # running, finished
    baseline: Optional[TestRunResult] = None
    latest_tests: Optional[TestRunResult] = None
    failing_ids: List[str] = field(default_factory=list)
    main_error: Optional[str] = None
    patches: List[PatchRecord] = field(default_factory=list)
    changed_files: List[str] = field(default_factory=list)
    files_inspected: List[str] = field(default_factory=list)
    tool_calls: List[ToolCall] = field(default_factory=list)
    tool_records: List[ToolRecord] = field(default_factory=list)
    retrieval_count: int = 0
    regression_state: bool = False
    stop_reason: Optional[Union[StopReason, str]] = None
    final_verification: bool = False
    commit_state: bool = False
    notes: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        """Convert run state to a dictionary for tracing and reporting."""
        return {
            "run_id": self.run_id,
            "task": self.task,
            "workspace_path": self.workspace_path,
            "branch": self.branch,
            "iteration": self.iteration,
            "status": self.status,
            "baseline": self.baseline.to_dict() if self.baseline else None,
            "latest_tests": self.latest_tests.to_dict() if self.latest_tests else None,
            "failing_ids": self.failing_ids,
            "main_error": self.main_error,
            "patches": [p.to_dict() for p in self.patches],
            "changed_files": self.changed_files,
            "files_inspected": self.files_inspected,
            "tool_calls": [{"id": c.id, "name": c.name, "arguments": c.arguments} for c in self.tool_calls],
            "tool_records": [tr.to_dict() for tr in self.tool_records],
            "retrieval_count": self.retrieval_count,
            "regression_state": self.regression_state,
            "stop_reason": self.stop_reason.value if isinstance(self.stop_reason, StopReason) else self.stop_reason,
            "final_verification": self.final_verification,
            "commit_state": self.commit_state,
            "notes": self.notes,
        }


def compare_test_runs(
    baseline: TestRunResult,
    previous: Optional[TestRunResult],
    current: TestRunResult,
) -> RunComparison:
    """Compare two full-suite runs by test id (TASK-025).

    ``previous`` is the run before the latest patch (``None`` means the
    baseline). A regression is a test that passed at baseline and fails now,
    or a drop in passing tests below the baseline. A lower failing count never
    hides either of them.
    """
    prior = previous if previous is not None else baseline
    for run in (baseline, prior, current):
        if run.scope != "full":
            raise ValueError("Comparison requires full-suite runs, not targeted runs.")

    base_ids, prior_ids, now_ids = set(baseline.failing_ids), set(prior.failing_ids), set(current.failing_ids)
    regressed = sorted(now_ids - base_ids)
    passed_delta = current.passed - baseline.passed
    return RunComparison(
        newly_failing=sorted(now_ids - prior_ids),
        still_failing=sorted(now_ids & prior_ids),
        fixed=sorted(prior_ids - now_ids),
        regressed=regressed,
        fixed_since_baseline=sorted(base_ids - now_ids),
        remained_fixed=sorted((base_ids - prior_ids) - now_ids),
        reopened=sorted((now_ids & base_ids) - prior_ids),
        passed_delta_vs_baseline=passed_delta,
        same_failing_set=now_ids == prior_ids,
        same_main_error=current.main_error == prior.main_error,
        is_regression=bool(regressed) or passed_delta < 0,
        equal_count_regression=bool(regressed) and len(now_ids) <= len(prior_ids),
        all_passing=current.all_passing,
    )
