"""Final report and run outputs (TASK-049, TASK-050).

``report.md`` keeps four kinds of statements apart (AGENTS.md A7):

* **Observed**: what the controller measured (test runs, tool results).
* **Inferred**: what the model said about the cause; never verified by CodePilot.
* **Verified**: only a full-suite run after the change (and the audit and commit).

The report is built from the run state, not by asking the model, so it cannot
claim anything the controller did not observe.
"""

from __future__ import annotations

from collections import Counter
from pathlib import Path
from typing import TYPE_CHECKING, Dict, List, Optional, Sequence

from codepilot.models import StopReason, TestRunResult
from codepilot.trace import (
    FINAL_TESTS_FILENAME, REPORT_FILENAME, RunInfo, existing_artifacts, redact_text,
    verified_success, write_trace,
)

if TYPE_CHECKING:
    from codepilot.controller import RunResult

MAX_LISTED_TESTS = 10
MAX_EXCERPT_CHARS = 1500
MAX_DIFF_CHARS = 6000

HEADLINES = {
    StopReason.NO_FAILURES: "NO REPAIR NEEDED (the baseline suite already passes)",
}


def total_tests(run: Optional[TestRunResult]) -> int:
    """Tests that ran: passed + failed + errors (skipped tests are not counted)."""
    return 0 if run is None else run.passed + run.failed + run.errors


def ratio(run: Optional[TestRunResult]) -> str:
    """``passed/total``, for example ``4/8``."""
    return "n/a" if run is None else f"{run.passed}/{total_tests(run)}"


def before_after(result: "RunResult") -> str:
    """The headline numbers, for example ``4/8 -> 8/8 passing in 4 iterations``."""
    baseline = result.state.baseline
    return f"{ratio(baseline)} -> {ratio(result.final_tests)} passing in {result.state.iteration} iteration(s)"


def headline(result: "RunResult") -> str:
    """One honest line about the outcome; success only if every condition holds."""
    if verified_success(result):
        return "SUCCESS (fix verified by the full test suite, audited and committed)"
    if result.stop_reason in HEADLINES:
        return HEADLINES[result.stop_reason]
    if result.stop_reason == StopReason.SUCCESS:
        return "NOT FIXED (the success conditions were not all met; this is a CodePilot defect)"
    return f"NOT FIXED (stopped: {result.stop_reason.value})"


def _fence(text: str, language: str = "") -> str:
    fence = "````" if "```" in text else "```"
    return f"{fence}{language}\n{text.rstrip()}\n{fence}"


def _cap(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:limit] + "\n... (cut)"


def _failing_lines(run: Optional[TestRunResult]) -> List[str]:
    if run is None or not run.failing_ids:
        return ["- none"]
    lines = []
    for test_id in run.failing_ids[:MAX_LISTED_TESTS]:
        message = run.failure_messages.get(test_id)
        lines.append(f"- `{test_id}`" + (f": {message}" if message else ""))
    if len(run.failing_ids) > MAX_LISTED_TESTS:
        lines.append(f"- ... and {len(run.failing_ids) - MAX_LISTED_TESTS} more")
    return lines


def _evidence_lines(result: "RunResult") -> List[str]:
    """What the model looked at, from the stored tool records (controller runs excluded)."""
    records = [r for r in result.state.tool_records if not (r.tool == "run_tests" and set(r.args) == {"source"})]
    if not records:
        return ["- The model inspected nothing."]
    counts = Counter(r.tool for r in records)
    lines = ["- Tool calls: " + ", ".join(f"{tool} x{n}" for tool, n in sorted(counts.items()))]
    files = result.state.files_inspected
    lines.append("- Files read: " + (", ".join(f"`{f}`" for f in files) if files else "none"))
    for tool, label in (("read_logs", "Logs"), ("read_metrics", "Metrics")):
        lines.append(f"- {label} read: {'yes' if counts.get(tool) else 'no'}")
    failed = sum(1 for r in records if not r.ok)
    if failed:
        lines.append(f"- {failed} tool call(s) failed or were refused.")
    return lines


def _model_explanation(result: "RunResult") -> Optional[str]:
    """The last text the model wrote (event ``llm_response``), if any."""
    texts = [e.get("text") for e in result.events if e.get("event") == "llm_response" and e.get("text")]
    return texts[-1] if texts else None


def _patch_lines(result: "RunResult") -> List[str]:
    patches = result.state.patches
    if not patches:
        return ["No patch was applied."]
    lines: List[str] = []
    for number, patch in enumerate(patches, 1):
        status = "applied, then reverted by the controller" if patch.reverted else ("applied" if patch.applied else "not applied")
        lines += [f"### Patch {number}: `{patch.path}` (iteration {patch.iteration}, {status})", "",
                  _fence(_cap(patch.diff or "(no diff recorded)", MAX_DIFF_CHARS), "diff"), ""]
    if not verified_success(result):
        lines.append("None of these changes were committed.")
    return lines


def _unresolved(result: "RunResult") -> List[str]:
    if verified_success(result):
        return ["Nothing known. The full suite passed; semantic test gaming is a documented limitation, so review the diff."]
    if result.stop_reason == StopReason.NO_FAILURES:
        return ["Nothing: no repair was needed."]
    lines = [f"- Stop reason: `{result.stop_reason.value}`. {result.summary}"]
    latest = result.state.latest_tests
    if latest is not None and latest.failing_ids:
        lines.append(f"- Still failing at the last full run ({ratio(latest)} passing):")
        lines += [f"  {line}" for line in _failing_lines(latest)]
    if result.state.regression_state:
        lines.append("- A regression was detected during the run.")
    return lines


def build_report(result: "RunResult", info: RunInfo, run_dir: Optional[Path] = None) -> str:
    """The full ``report.md`` text (not yet redacted)."""
    state, baseline, final = result.state, result.state.baseline, result.final_tests
    success = verified_success(result)
    out: List[str] = [
        "# CodePilot run report", "",
        f"**Result:** {headline(result)}  ",
        f"**Stop reason:** `{result.stop_reason.value}`: {result.summary}  ",
        f"**Tests:** {before_after(result)}", "",
        "## Task and repository", "",
        f"- Task: {state.task or '(none)'}",
        f"- Repository: `{info.repository}`",
        f"- Run id: `{state.run_id}`",
        f"- Model: `{info.model}`",
        f"- Branch: `{state.branch}`",
        f"- Started: {info.started_at}; finished: {info.finished_at} ({result.duration_s:.1f}s)", "",
        "## Baseline (observed)", "",
    ]
    if baseline is None:
        out += ["No baseline test run was recorded."]
    else:
        out += [f"{baseline.passed} passed, {baseline.failed} failed, {baseline.errors} errors "
                f"(exit code {baseline.exit_code}). Failing tests:", ""]
        out += _failing_lines(baseline)
        if baseline.main_error:
            out += ["", f"Main error: {baseline.main_error}"]
    out += ["", "## Evidence inspected (observed)", ""] + _evidence_lines(result)

    explanation = _model_explanation(result)
    out += ["", "## Root cause", "",
            "- **Observed:** the failing tests and messages listed above.",
            "- **Inferred (the model's own words, not verified by CodePilot):** "
            + (explanation.replace("\n", " ") if explanation else "the model gave no explanation."),
            "- **Verified:** " + ("only that the full test suite passes after the patch(es) below; "
                                  "a passing run does not prove which cause was the real one."
                                  if success else "nothing. No fix was verified.")]
    out += ["", "## Patches", ""] + _patch_lines(result)

    out += ["", "## Final verification", ""]
    if final is None:
        out += ["No full-suite run was recorded."]
    else:
        out += [f"- Last full-suite run: {final.passed} passed, {final.failed} failed, {final.errors} errors "
                f"(exit code {final.exit_code}).",
                f"- Controller final verification: {'passed' if state.final_verification else 'not reached'}.",
                f"- Changed-file audit and commit: {'passed' if state.commit_state else 'not performed'}."]
    out += [f"- Changed files: {', '.join(f'`{f}`' for f in state.changed_files) or 'none committed'}",
            f"- Commit: `{result.commit_hash}` on `{state.branch}`" if result.commit_hash else "- Commit: none"]
    if result.patch_path:
        out += ["- `fix.patch` holds the verified diff; apply it to a clean copy of the repository to check it."]

    out += ["", "## Unresolved", ""] + _unresolved(result)
    out += ["", "## Artifacts", "",
            "- Workspace: " + {"deleted": "deleted after the run", "kept": f"kept at `{info.workspace_kept}`"}.get(
                info.cleanup, info.cleanup)]
    out += [f"- `{name}`" for name in existing_artifacts(run_dir, final is not None)]
    return "\n".join(out) + "\n"


def build_final_tests(result: "RunResult") -> Optional[str]:
    """Text of ``final_tests.txt``: the controller's latest full-suite result."""
    run = result.final_tests
    if run is None:
        return None
    lines = [
        "Latest full-suite run recorded by the controller",
        f"exit code: {run.exit_code} | passed: {run.passed} | failed: {run.failed} | errors: {run.errors} | "
        f"skipped: {run.skipped} | duration: {run.duration_s}s | timed out: {'yes' if run.timed_out else 'no'}",
        "failing tests: " + (", ".join(run.failing_ids) if run.failing_ids else "none"),
        f"main error: {run.main_error or 'none'}",
        "", "--- pytest output (excerpt) ---", _cap(run.output_excerpt or "(no output captured)", MAX_EXCERPT_CHARS * 3),
    ]
    return "\n".join(lines) + "\n"


def write_outputs(
    run_dir: Path, result: "RunResult", info: RunInfo, secrets: Sequence[str] = (),
) -> Dict[str, str]:
    """Write ``run_trace.json``, ``report.md`` and ``final_tests.txt``.

    Returns ``{file name: error text}`` for any file that could not be written;
    an empty dict means everything was written. Never raises for I/O problems.
    """
    run_dir = Path(run_dir)
    errors: Dict[str, str] = {}
    run_dir.mkdir(parents=True, exist_ok=True)
    jobs = {
        "run_trace.json": lambda: write_trace(run_dir, result, info, secrets),
        REPORT_FILENAME: lambda: _write_text(run_dir / REPORT_FILENAME, build_report(result, info, run_dir), secrets),
    }
    final_tests = build_final_tests(result)
    if final_tests is not None:
        jobs[FINAL_TESTS_FILENAME] = lambda: _write_text(run_dir / FINAL_TESTS_FILENAME, final_tests, secrets)
    for name, job in jobs.items():
        try:
            job()
        except Exception as exc:  # noqa: BLE001 - one failed output must not hide the others
            errors[name] = f"{type(exc).__name__}: {exc}"
    return errors


def _write_text(path: Path, text: str, secrets: Sequence[str]) -> Path:
    path.write_text(redact_text(text, secrets), encoding="utf-8", newline="\n")
    return path
