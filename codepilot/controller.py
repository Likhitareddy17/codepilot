"""The agent controller (TASK-036 to TASK-047).

The controller owns every decision: the LLM proposes tool calls, the
controller executes them, runs the full test suite after each patch, detects
regression and lack of progress, and alone decides when a run has succeeded.
SUCCESS requires a fresh full-suite pass, a clean changed-file audit and a
local Git commit.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from codepilot import prompts
from codepilot.config import Settings
from codepilot.errors import LLMError, LLMUnavailableError
from codepilot.llm.client import LLMClient, LLMResponse
from codepilot.models import (
    BaselineEvidence, PytestOutcome, RunComparison, StopReason, TestRunResult, ToolResult,
)
from codepilot.state import RunState, compare_test_runs
from codepilot.tools import ToolContext
from codepilot.tools.history import ResultStore
from codepilot.tools.patch import revert_patch
from codepilot.tools.registry import ToolRegistry, build_default_registry
from codepilot.tools.tests import evaluate_baseline, run_pytest
from codepilot.workspace import Workspace

MAX_REGRESSIONS = 2          # the second regression stops the run (ARCHITECTURE.md section 6)
NO_PROGRESS_THRESHOLD = 2    # consecutive patches that change nothing (ADR)
BAD_OUTPUT_CODES = {"UNKNOWN_TOOL", "INVALID_ARGUMENTS"}
MAX_EVENT_TEXT_CHARS = 500   # model text kept in the trace (the report quotes it as unverified)
SuiteRunner = Callable[[Path, Path, Settings, Optional[str]], PytestOutcome]


@dataclass
class RunResult:
    """Everything a caller needs after a run (trace and report build on this)."""

    state: RunState
    stop_reason: StopReason
    summary: str
    baseline: Optional[BaselineEvidence] = None
    final_tests: Optional[TestRunResult] = None
    commit_hash: Optional[str] = None
    patch_path: Optional[str] = None
    events: List[Dict[str, Any]] = field(default_factory=list)
    duration_s: float = 0.0

    @property
    def success(self) -> bool:
        return self.stop_reason == StopReason.SUCCESS


class _Stop(Exception):
    """Internal: unwinds the loop with a final stop reason."""

    def __init__(self, reason: StopReason, summary: str) -> None:
        super().__init__(summary)
        self.reason = reason
        self.summary = summary


class Controller:
    """Runs one repair session inside an already created ``Workspace``."""

    def __init__(
        self,
        workspace: Workspace,
        llm: LLMClient,
        settings: Settings,
        run_dir: Path,
        task: str = "",
        repository_name: str = "repository",
        suite_runner: Optional[SuiteRunner] = None,
    ) -> None:
        if workspace.temp_dir is None:
            raise ValueError("The workspace must be created before the controller runs.")
        self.workspace = workspace
        self.llm = llm
        self.settings = settings
        self.run_dir = Path(run_dir)
        self.repository_name = repository_name
        self._suite_runner: SuiteRunner = suite_runner or (
            lambda root, run_dir, s, target: run_pytest(root, run_dir, s, target)
        )
        self.state = RunState(
            run_id=workspace.run_id, task=task, workspace_path=str(workspace.temp_dir),
            branch=workspace.branch_name,
        )
        self.history = ResultStore(self.run_dir, settings)
        self.ctx = ToolContext(
            root=workspace.temp_dir, settings=settings, workspace=workspace,
            run_dir=self.run_dir, history=self.history,
        )
        self.registry: ToolRegistry = build_default_registry(self.ctx)
        self.events: List[Dict[str, Any]] = []
        self.baseline: Optional[BaselineEvidence] = None
        self._conversation: Optional[prompts.Conversation] = None
        self._regressions = 0
        self._no_progress_streak = 0
        self._bad_output_streak = 0
        self._protected_attempts = 0
        self._commit_hash: Optional[str] = None
        self._patch_path: Optional[str] = None

    # ------------------------------------------------------------------ run

    def run(self) -> RunResult:
        """Execute the whole session. Never raises (except for test-script bugs)."""
        started = time.monotonic()
        try:
            reason, summary = self._run()
        except _Stop as stop:
            reason, summary = stop.reason, stop.summary
        except AssertionError:
            raise
        except Exception as exc:  # noqa: BLE001 - the controller must always finish cleanly
            reason, summary = StopReason.INTERNAL_ERROR, f"Unexpected internal error: {type(exc).__name__}: {exc}"
        return self._finish(reason, summary, time.monotonic() - started)

    def _run(self) -> "tuple[StopReason, str]":
        self._establish_baseline()
        self._conversation = prompts.Conversation(
            prompts.SYSTEM_PROMPT,
            prompts.build_task_message(
                self.state, self.settings, self.registry.tool_names(), self.repository_name),
        )
        for iteration in range(1, self.settings.max_iterations + 1):
            self.state.iteration = self.ctx.iteration = iteration
            self._event("iteration_start")
            response = self._ask_llm()
            self._conversation.add_assistant(response)
            if response.tool_calls:
                self._handle_tool_calls(response)
            else:
                self._handle_claim_of_completion(response)
        return StopReason.MAX_ITERATIONS, (
            f"The iteration limit ({self.settings.max_iterations}) was reached without a verified fix.")

    # ------------------------------------------------------------- baseline

    def _run_suite(self, target: Optional[str] = None) -> PytestOutcome:
        return self._suite_runner(self.ctx.root, self.run_dir, self.settings, target)

    def _record_suite_result(self, result: TestRunResult, source: str) -> str:
        """Store a controller-run test result like any tool result (so it is retrievable)."""
        call_id = self.registry.next_call_id()
        self.history.add(call_id, "run_tests", {"source": source}, ToolResult(
            call_id=call_id, ok=True, data={**result.to_dict(), "all_passing": result.all_passing}),
            iteration=self.state.iteration)
        return call_id

    def _establish_baseline(self) -> None:
        outcome = self._run_suite()
        evidence = evaluate_baseline(outcome)
        self.baseline = evidence
        self.state.baseline = self.state.latest_tests = outcome.result
        self.state.failing_ids = list(outcome.result.failing_ids)
        self.state.main_error = outcome.result.main_error
        self.history.pin(self._record_suite_result(outcome.result, "baseline"))
        self._event("baseline", status=evidence.status, failing=len(outcome.result.failing_ids))
        if evidence.status == BaselineEvidence.NO_FAILURES:
            raise _Stop(StopReason.NO_FAILURES, "The full test suite already passes; nothing to repair.")
        if evidence.status == BaselineEvidence.UNUSABLE:
            raise _Stop(StopReason.INTERNAL_ERROR, f"The baseline test run is unusable: {evidence.reason}")

    # ------------------------------------------------------------------ LLM

    def _ask_llm(self) -> LLMResponse:
        try:
            response = self.llm.chat(self._conversation.messages(self.history), self.registry.llm_tool_schemas())
        except LLMUnavailableError as exc:
            raise _Stop(StopReason.LLM_UNAVAILABLE, f"The LLM service is unavailable: {exc}") from None
        except LLMError as exc:
            raise _Stop(StopReason.INTERNAL_ERROR, f"The LLM request failed: {exc}") from None
        self._event("llm_response", tool_calls=[c.name for c in response.tool_calls],
                    has_text=bool(response.text), usage=response.usage,
                    text=(response.text or "")[:MAX_EVENT_TEXT_CHARS])
        return response

    # ----------------------------------------------------------- tool calls

    def _handle_tool_calls(self, response: LLMResponse) -> None:
        limit = self.settings.max_tool_calls_per_iteration
        if len(response.tool_calls) > limit:
            raise _Stop(StopReason.TOOL_LIMIT_EXCEEDED,
                        f"The model requested {len(response.tool_calls)} tool calls in one turn (limit {limit}).")
        pending: List[Dict[str, Any]] = []
        patched_this_iteration = False
        for tool_call in response.tool_calls:
            self.state.tool_calls.append(tool_call)
            if tool_call.malformed:
                self._bad_output()
                pending.append({"id": tool_call.id, "content": (
                    "INVALID_ARGUMENTS: the arguments were not a valid JSON object. "
                    f"Received: {tool_call.raw_arguments[:200]!r}")})
                self._event("malformed_tool_call", tool=tool_call.name)
                continue
            result = self.registry.execute(tool_call.name, tool_call.arguments)
            pending.append({"id": tool_call.id, "call_id": result.call_id, "tool": tool_call.name, "result": result})
            self._event("tool_call", tool=tool_call.name, call_id=result.call_id, ok=result.ok,
                        error=(result.error or {}).get("code"))
            code = (result.error or {}).get("code")
            if result.ok:
                self._bad_output_streak = 0
                if tool_call.name == "read_file":
                    path = result.data.get("path")
                    if path and path not in self.state.files_inspected:
                        self.state.files_inspected.append(path)
                if tool_call.name == "apply_patch":
                    patched_this_iteration = True
            elif code in BAD_OUTPUT_CODES:
                self._bad_output()
            elif tool_call.name == "apply_patch" and code in ("PATH_PROTECTED", "PATH_DENIED"):
                self._protected_attempts += 1
                self._event("protected_path_attempt", path=tool_call.arguments.get("path"))
                if self._protected_attempts >= self.settings.max_protected_path_attempts:
                    self._flush(pending)
                    raise _Stop(StopReason.PROTECTED_PATH_VIOLATION,
                                f"The model repeatedly tried to modify protected files "
                                f"({self._protected_attempts} attempts).")
        notices = self._after_patches(pending) if patched_this_iteration else []
        self._flush(pending)
        for notice in notices:
            self._conversation.add_user(notice)

    def _bad_output(self) -> None:
        self._bad_output_streak += 1
        if self._bad_output_streak >= self.settings.max_bad_model_outputs:
            raise _Stop(StopReason.BAD_MODEL_OUTPUT,
                        f"The model produced {self._bad_output_streak} invalid tool calls in a row.")

    def _flush(self, pending: List[Dict[str, Any]]) -> None:
        """Add the tool messages for this turn to the conversation, in call order."""
        for item in pending:
            if "result" in item:
                self._conversation.add_tool_result(item["id"], item["call_id"], extra=item.get("extra", ""))
            else:
                self._conversation.add_tool_result(item["id"], content=item["content"])
        pending.clear()

    # --------------------------------------------- verification after patches

    def _after_patches(self, pending: List[Dict[str, Any]]) -> List[str]:
        """Run the full suite after a patching turn and apply the stop rules (TASK-038 to 040)."""
        outcome = self._run_suite()
        current = outcome.result
        self._record_suite_result(current, "after_patch")
        previous = self.state.latest_tests
        comparison = compare_test_runs(self.state.baseline, previous, current)
        self._event("tests_after_patch", passed=current.passed, failed=current.failed,
                    errors=current.errors, regression=comparison.is_regression)

        last_patch = next((p for p in reversed(pending) if p.get("tool") == "apply_patch" and p["result"].ok), None)
        reverted: List[str] = []
        if comparison.is_regression:
            reverted = self._revert_iteration_patches()
            self._regressions += 1
            self.state.regression_state = True
            self._no_progress_streak = 0
            self._event("regression", regressed=comparison.regressed, reverted=reverted,
                        count=self._regressions)
        else:
            self.state.latest_tests = current
            self.state.failing_ids = list(current.failing_ids)
            self.state.main_error = current.main_error
        if last_patch is not None:
            summary = prompts.compact_test_summary(current, comparison)
            if reverted:
                summary += "\nThe controller reverted this patch because of the regression."
            last_patch["extra"] = summary

        if comparison.is_regression:
            if self._regressions >= MAX_REGRESSIONS:
                self._flush(pending)
                raise _Stop(StopReason.REPEATED_REGRESSION,
                            f"A second regression occurred; the patches were reverted and the run stopped "
                            f"({', '.join(comparison.regressed) or 'fewer tests pass than at baseline'}).")
            return [prompts.regression_message(comparison, reverted[0] if reverted else None)]

        if current.all_passing:
            self._flush(pending)
            return self._verify_and_finish(previous_message_flushed=True)

        unchanged = comparison.same_failing_set and comparison.same_main_error
        self._no_progress_streak = self._no_progress_streak + 1 if unchanged else 0
        if self._no_progress_streak >= NO_PROGRESS_THRESHOLD:
            self._flush(pending)
            raise _Stop(StopReason.NO_PROGRESS,
                        "The same failing tests and main error persisted after two consecutive patches.")
        return []

    def _revert_iteration_patches(self) -> List[str]:
        """Undo every patch applied in the current iteration, newest first."""
        reverted: List[str] = []
        while self.ctx.undo_stack and self.ctx.undo_stack[-1][0].iteration == self.state.iteration:
            record = revert_patch(self.ctx)
            if record is None:
                break
            reverted.append(record.path)
            self.history.invalidate_file_read(record.path)
        return reverted

    # ------------------------------------------------ claims, final decision

    def _handle_claim_of_completion(self, response: LLMResponse) -> None:
        """The model stopped calling tools: verify before believing it."""
        self._event("completion_claim", has_text=bool(response.text))
        if not self.ctx.patches:
            self._bad_output()
            self._conversation.add_user(prompts.verification_failed_message(self.state.baseline))
            return
        self._verify_and_finish(previous_message_flushed=True)

    def _verify_and_finish(self, previous_message_flushed: bool = True) -> List[str]:
        """Final verification, audit, commit and patch export (TASK-045 to TASK-047)."""
        outcome = self._run_suite()
        final = outcome.result
        self._record_suite_result(final, "final_verification")
        self.state.latest_tests = final
        self.state.failing_ids = list(final.failing_ids)
        self.state.main_error = final.main_error
        comparison = compare_test_runs(self.state.baseline, self.state.baseline, final)
        self._event("final_verification", passed=final.passed, failed=final.failed, errors=final.errors,
                    all_passing=final.all_passing)
        if not final.all_passing:
            self._conversation.add_user(prompts.verification_failed_message(final))
            return []
        if comparison.is_regression:
            raise _Stop(StopReason.REGRESSION, "Final verification found a regression against the baseline; nothing was committed.")
        self.state.final_verification = True

        changed = self.workspace.changed_files()
        if not changed:
            raise _Stop(StopReason.NO_FAILURES, "The suite passes without any change (possibly a flaky test); nothing to commit.")
        audit = self.workspace.audit_changes(self.settings.editable_extensions)
        self._event("audit", ok=audit.ok, changed=audit.changed_files, violations=audit.violations)
        if not audit.ok:
            protected = any("protected" in v for v in audit.violations)
            raise _Stop(StopReason.PROTECTED_PATH_VIOLATION if protected else StopReason.INTERNAL_ERROR,
                        "The final changed-file audit failed: " + "; ".join(audit.violations))
        commit = self.workspace.commit_fix(self._commit_message(changed, final))
        if not commit.ok:
            raise _Stop(StopReason.INTERNAL_ERROR, f"The commit was refused: {commit.message}")
        self._commit_hash = commit.commit_hash
        self.state.commit_state = True
        self.state.changed_files = list(changed)
        self._patch_path = str(self.workspace.export_patch(str(self.run_dir / "fix.patch")))
        self._event("commit", commit=self._commit_hash, patch=self._patch_path)
        raise _Stop(StopReason.SUCCESS, f"The full test suite passes and the fix was committed ({self._commit_hash[:8]}).")

    def _commit_message(self, changed: List[str], final: TestRunResult) -> str:
        base = self.state.baseline
        return (
            f"CodePilot: fix failing tests ({len(changed)} file(s))\n\n"
            f"Files: {', '.join(changed)}\n"
            f"Tests before: {base.passed} passed, {base.failed + base.errors} failing\n"
            f"Tests after: {final.passed} passed, {final.failed + final.errors} failing\n"
            f"Run: {self.state.run_id}"
        )

    # --------------------------------------------------------------- finish

    def _event(self, kind: str, **data: Any) -> None:
        self.events.append({
            "event": kind, "iteration": self.state.iteration,
            "timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds"), **data,
        })

    def _finish(self, reason: StopReason, summary: str, duration: float) -> RunResult:
        s = self.state
        s.stop_reason, s.status = reason, "finished"
        s.patches = list(self.ctx.patches)
        s.tool_records = list(self.history.records)
        s.retrieval_count = self.history.retrieval_count
        try:
            s.changed_files = self.workspace.changed_files()
        except Exception:  # noqa: BLE001
            pass
        self._event("stop", reason=reason.value, summary=summary)
        return RunResult(
            state=s, stop_reason=reason, summary=summary, baseline=self.baseline, final_tests=s.latest_tests,
            commit_hash=self._commit_hash, patch_path=self._patch_path, events=self.events, duration_s=round(duration, 3),
        )
