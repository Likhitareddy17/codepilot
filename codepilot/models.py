"""Typed domain models, result envelopes, and stop reasons for CodePilot."""

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional, Union


class StopReason(str, Enum):
    """Approved stopping outcomes for a CodePilot run."""

    SUCCESS = "SUCCESS"
    NO_FAILURES = "NO_FAILURES"
    MAX_ITERATIONS = "MAX_ITERATIONS"
    NO_PROGRESS = "NO_PROGRESS"
    REGRESSION = "REGRESSION"
    REPEATED_REGRESSION = "REPEATED_REGRESSION"
    LLM_UNAVAILABLE = "LLM_UNAVAILABLE"
    BAD_MODEL_OUTPUT = "BAD_MODEL_OUTPUT"
    PROTECTED_PATH_VIOLATION = "PROTECTED_PATH_VIOLATION"
    PROTECTED_PATH_NEEDED = "PROTECTED_PATH_NEEDED"
    INTERNAL_ERROR = "INTERNAL_ERROR"
    TOOL_LIMIT_EXCEEDED = "TOOL_LIMIT_EXCEEDED"
    OTHER = "OTHER"


@dataclass
class ToolErrorModel:
    """Structured tool error per specification."""

    code: str
    message: str

    def to_dict(self) -> Dict[str, Any]:
        return {"code": self.code, "message": self.message}


@dataclass
class ToolResult:
    """Standardized tool result envelope."""

    call_id: str
    ok: bool
    data: Dict[str, Any] = field(default_factory=dict)
    error: Optional[Union[Dict[str, Any], str]] = None
    truncated: bool = False

    def to_dict(self) -> Dict[str, Any]:
        err = self.error
        if isinstance(err, ToolErrorModel):
            err = err.to_dict()
        return {
            "call_id": self.call_id,
            "ok": self.ok,
            "data": self.data,
            "error": err,
            "truncated": self.truncated,
        }


@dataclass
class ToolCall:
    """Parsed LLM tool call request."""

    id: str
    name: str
    arguments: Dict[str, Any]
    malformed: bool = False      # the arguments were not a valid JSON object
    raw_arguments: str = ""      # the original argument text, kept for the error message


@dataclass
class ToolRecord:
    """Persistent tool record stored in the trace."""

    call_id: str
    iteration: int
    tool: str
    args: Dict[str, Any]
    ok: bool
    summary: Dict[str, Any] = field(default_factory=dict)
    result_path: str = ""
    retrieved_count: int = 0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "call_id": self.call_id,
            "iteration": self.iteration,
            "tool": self.tool,
            "args": self.args,
            "ok": self.ok,
            "summary": self.summary,
            "result_path": self.result_path,
            "retrieved_count": self.retrieved_count,
        }


@dataclass
class TestRunResult:
    """Results from running test suite (JUnit XML parsed or pytest execution)."""

    __test__ = False

    exit_code: int
    passed: int
    failed: int
    errors: int
    failing_ids: List[str] = field(default_factory=list)
    duration_s: float = 0.0
    timed_out: bool = False
    output_excerpt: str = ""
    skipped: int = 0
    scope: str = "full"  # "full" (no target) or "targeted"
    main_error: Optional[str] = None
    failure_messages: Dict[str, str] = field(default_factory=dict)

    @property
    def all_passing(self) -> bool:
        """True only for a clean run that actually executed passing tests.

        Exit code 5 (nothing collected), zero passed tests and timeouts never
        count as passing (TESTING.md section 14).
        """
        return (
            self.exit_code == 0
            and not self.timed_out
            and self.failed == 0
            and self.errors == 0
            and self.passed > 0
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "exit_code": self.exit_code,
            "passed": self.passed,
            "failed": self.failed,
            "errors": self.errors,
            "failing_ids": self.failing_ids,
            "duration_s": self.duration_s,
            "timed_out": self.timed_out,
            "output_excerpt": self.output_excerpt,
            "skipped": self.skipped,
            "scope": self.scope,
            "main_error": self.main_error,
            "failure_messages": self.failure_messages,
        }


@dataclass
class PytestOutcome:
    """One pytest execution: the parsed result plus the raw captured evidence."""

    result: TestRunResult
    stdout: str = ""
    stderr: str = ""
    junit_path: Optional[str] = None
    command: List[str] = field(default_factory=list)


@dataclass
class RunComparison:
    """Identity-based comparison of full-suite runs (TASK-025).

    Decisions are made on test ids, never on counts alone.
    """

    newly_failing: List[str] = field(default_factory=list)   # failing now, not in the previous run
    still_failing: List[str] = field(default_factory=list)   # failing in both runs
    fixed: List[str] = field(default_factory=list)           # failing before, passing now
    regressed: List[str] = field(default_factory=list)       # failing now, passing at baseline
    fixed_since_baseline: List[str] = field(default_factory=list)
    remained_fixed: List[str] = field(default_factory=list)  # fixed earlier and still passing
    reopened: List[str] = field(default_factory=list)        # fixed earlier, failing again
    passed_delta_vs_baseline: int = 0
    same_failing_set: bool = False
    same_main_error: bool = False
    is_regression: bool = False
    equal_count_regression: bool = False  # a regression hidden by an equal or lower failing count
    all_passing: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return dict(self.__dict__)


@dataclass
class BaselineEvidence:
    """Outcome of the baseline full-suite run at the start of a run (TASK-026)."""

    NEEDS_REPAIR = "NEEDS_REPAIR"
    NO_FAILURES = "NO_FAILURES"
    UNUSABLE = "UNUSABLE"

    status: str
    reason: str
    outcome: PytestOutcome

    @property
    def result(self) -> TestRunResult:
        return self.outcome.result

    @property
    def failing_ids(self) -> List[str]:
        return list(self.outcome.result.failing_ids)

    @property
    def main_error(self) -> Optional[str]:
        return self.outcome.result.main_error

    @property
    def needs_repair(self) -> bool:
        return self.status == self.NEEDS_REPAIR

    @property
    def stop_reason(self) -> Optional[StopReason]:
        """``NO_FAILURES`` when nothing is broken; otherwise None (keep going or fail)."""
        return StopReason.NO_FAILURES if self.status == self.NO_FAILURES else None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "status": self.status,
            "reason": self.reason,
            "failing_ids": self.failing_ids,
            "main_error": self.main_error,
            "result": self.outcome.result.to_dict(),
        }


@dataclass
class PatchRecord:
    """Record of a patch attempted on a file."""

    iteration: int
    path: str
    diff: str
    applied: bool
    reverted: bool = False
    tests_after: Optional[TestRunResult] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "iteration": self.iteration,
            "path": self.path,
            "diff": self.diff,
            "applied": self.applied,
            "reverted": self.reverted,
            "tests_after": self.tests_after.to_dict() if self.tests_after else None,
        }


@dataclass
class GitResult:
    """Result of Git operations (init, commit, diff, status)."""

    ok: bool
    commit_hash: Optional[str] = None
    message: Optional[str] = None
    diff: Optional[str] = None
    changed_files: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "ok": self.ok,
            "commit_hash": self.commit_hash,
            "message": self.message,
            "diff": self.diff,
            "changed_files": self.changed_files,
        }


@dataclass
class RunMetadata:
    """Metadata describing a CodePilot execution run."""

    run_id: str
    task: str
    start_time: str
    repo_path: str
    branch: str

    def to_dict(self) -> Dict[str, Any]:
        return {
            "run_id": self.run_id,
            "task": self.task,
            "start_time": self.start_time,
            "repo_path": self.repo_path,
            "branch": self.branch,
        }


@dataclass
class FinalReportData:
    """Data compiled for final report and trace summary."""

    run_id: str
    status: str
    stop_reason: Optional[Union[StopReason, str]] = None
    baseline_tests: Optional[TestRunResult] = None
    latest_tests: Optional[TestRunResult] = None
    patches_applied: int = 0
    iterations: int = 0
    summary_text: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "run_id": self.run_id,
            "status": self.status,
            "stop_reason": self.stop_reason.value if isinstance(self.stop_reason, StopReason) else self.stop_reason,
            "baseline_tests": self.baseline_tests.to_dict() if self.baseline_tests else None,
            "latest_tests": self.latest_tests.to_dict() if self.latest_tests else None,
            "patches_applied": self.patches_applied,
            "iterations": self.iterations,
            "summary_text": self.summary_text,
        }


@dataclass
class AuditResult:
    """Outcome of the final changed-file audit before a commit."""

    ok: bool
    changed_files: List[str] = field(default_factory=list)
    violations: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "ok": self.ok,
            "changed_files": self.changed_files,
            "violations": self.violations,
        }
