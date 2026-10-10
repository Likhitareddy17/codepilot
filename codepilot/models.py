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
