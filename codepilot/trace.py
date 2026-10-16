"""Run trace: a complete, secret-free record of one run (TASK-048).

The controller keeps everything in memory (``RunResult``). This module turns
that into ``run_trace.json``: run metadata, the baseline, one entry per tool
call (AGENTS.md A7), the model's turns, patches, test results, the stop
reason, the commit and the cleanup status. Secrets are removed from every
string before anything is written.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Dict, Iterable, List, Optional, Sequence

from codepilot.models import StopReason, TestRunResult
from codepilot.prompts import PROMPT_VERSION

if TYPE_CHECKING:
    from codepilot.controller import RunResult

SCHEMA_VERSION = 1
TRACE_FILENAME = "run_trace.json"
REPORT_FILENAME = "report.md"
FINAL_TESTS_FILENAME = "final_tests.txt"
PATCH_FILENAME = "fix.patch"
JUNIT_FILENAME = "junit.xml"
MIN_SECRET_LENGTH = 4

# Shapes of secrets that must never reach an output file, even if the exact
# key is unknown (for example a key printed by repository code).
_TOKEN_PATTERNS = (
    re.compile(r"\bgsk_[A-Za-z0-9]{8,}"),
    re.compile(r"\bsk-[A-Za-z0-9_\-]{16,}"),
    re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._\-]{8,}"),
)
_ASSIGNMENT = re.compile(
    r"(?i)\b([A-Z0-9_]*(?:API_KEY|TOKEN|SECRET|PASSWORD)[A-Z0-9_]*)(\s*[=:]\s*)[^\s\"',;]+"
)
REDACTED = "***"


def utc_now_iso() -> str:
    """Current UTC time as an ISO-8601 string (seconds)."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def redact_text(text: str, secrets: Iterable[str] = ()) -> str:
    """Remove known secrets and secret-shaped tokens from ``text``."""
    for secret in secrets:
        if secret and len(secret) >= MIN_SECRET_LENGTH:
            text = text.replace(secret, REDACTED)
    for pattern in _TOKEN_PATTERNS:
        text = pattern.sub(REDACTED, text)
    return _ASSIGNMENT.sub(lambda m: f"{m.group(1)}{m.group(2)}{REDACTED}", text)


def redact_obj(obj: Any, secrets: Iterable[str] = ()) -> Any:
    """Redact every string inside a JSON-like structure (before serialising it)."""
    secrets = tuple(secrets)
    if isinstance(obj, str):
        return redact_text(obj, secrets)
    if isinstance(obj, dict):
        return {redact_text(str(k), secrets): redact_obj(v, secrets) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [redact_obj(v, secrets) for v in obj]
    return obj


@dataclass
class RunInfo:
    """Facts about a run that the controller does not own."""

    repository: str
    model: str
    started_at: str
    finished_at: str
    max_iterations: int
    cleanup: str = "pending"                 # deleted | kept | failed: <reason>
    workspace_kept: Optional[str] = None     # path, only when the workspace was kept
    history_keep_recent_results: Optional[int] = None   # setting that affects what the model sees


def verified_success(result: "RunResult") -> bool:
    """True only if every success condition holds (never trust the stop reason alone)."""
    state, final = result.state, result.final_tests
    return bool(
        result.stop_reason == StopReason.SUCCESS
        and state.final_verification
        and state.commit_state
        and result.commit_hash
        and final is not None
        and final.all_passing
    )


# ------------------------------------------------------------------ helpers


def _counts(run: Optional[TestRunResult]) -> Optional[Dict[str, Any]]:
    if run is None:
        return None
    return {
        "passed": run.passed, "failed": run.failed, "errors": run.errors, "skipped": run.skipped,
        "exit_code": run.exit_code, "timed_out": run.timed_out, "duration_s": run.duration_s,
        "failing_ids": list(run.failing_ids), "main_error": run.main_error,
    }


def _suite_counts(record: Any) -> Optional[Dict[str, int]]:
    """Pass/fail counts of a full-suite run_tests record, else None."""
    if record.tool != "run_tests" or not record.ok:
        return None
    facts = (record.summary or {}).get("key_facts") or {}
    if facts.get("scope") != "full" or facts.get("passed") is None:
        return None
    return {"passing": facts["passed"], "failing": (facts.get("failed") or 0) + (facts.get("errors") or 0)}


def _relative(path: str, run_dir: Optional[Path]) -> Optional[str]:
    if not path:
        return None
    if run_dir is not None:
        try:
            return Path(path).resolve().relative_to(Path(run_dir).resolve()).as_posix()
        except (ValueError, OSError):
            pass
    return Path(path).name


def _tool_entries(result: "RunResult", run_dir: Optional[Path]) -> List[Dict[str, Any]]:
    """One entry per stored tool result, with the test counts known at that point.

    A patch entry shows the counts measured *after* it (the controller's next
    full-suite run in the same iteration); every other entry shows the latest
    full-suite counts so far.
    """
    records = list(result.state.tool_records)
    stamps = {e["call_id"]: e.get("timestamp") for e in result.events if e.get("event") == "tool_call" and "call_id" in e}
    suites = [(i, r.iteration, _suite_counts(r)) for i, r in enumerate(records) if _suite_counts(r)]
    entries: List[Dict[str, Any]] = []
    for index, record in enumerate(records):
        counts: Optional[Dict[str, int]] = None
        if record.tool == "apply_patch":
            counts = next((c for i, it, c in suites if i > index and it == record.iteration), None)
        if counts is None:
            counts = next((c for i, _, c in reversed(suites) if i <= index), None)
        entries.append({
            "call_id": record.call_id,
            "iteration": record.iteration,
            "tool": record.tool,
            "source": "controller" if record.tool == "run_tests" and set(record.args) == {"source"} else "model",
            "args": record.args,
            "ok": record.ok,
            "result_summary": (record.summary or {}).get("outcome"),
            "truncated": (record.summary or {}).get("truncated", False),
            "tests_passing": counts["passing"] if counts else None,
            "tests_failing": counts["failing"] if counts else None,
            "timestamp": stamps.get(record.call_id),
            "result_file": _relative(record.result_path, run_dir),
            "retrieved_count": record.retrieved_count,
        })
    return entries


def _patch_entries(result: "RunResult") -> List[Dict[str, Any]]:
    entries = []
    for patch in result.state.patches:
        after = patch.tests_after
        entries.append({
            "iteration": patch.iteration, "path": patch.path, "applied": patch.applied,
            "reverted": patch.reverted, "diff": patch.diff,
            "tests_after": None if after is None else {
                "passed": after.passed, "failed": after.failed, "errors": after.errors,
                "failing_ids": list(after.failing_ids),
            },
        })
    return entries


def existing_artifacts(run_dir: Optional[Path], final_tests: bool = True) -> List[str]:
    """Names of the outputs of a run: the ones always written, plus those that exist."""
    names = [TRACE_FILENAME, REPORT_FILENAME] + ([FINAL_TESTS_FILENAME] if final_tests else [])
    if run_dir is not None:
        names += [n for n in (PATCH_FILENAME, JUNIT_FILENAME, "results") if (Path(run_dir) / n).exists()]
    return names


# -------------------------------------------------------------------- trace


def build_trace(result: "RunResult", info: RunInfo, run_dir: Optional[Path] = None) -> Dict[str, Any]:
    """The complete trace as a JSON-ready dictionary (not yet redacted)."""
    state = result.state
    return {
        "schema_version": SCHEMA_VERSION,
        "run": {
            "run_id": state.run_id, "task": state.task, "repository": info.repository,
            "branch": state.branch, "model": info.model, "prompt_version": PROMPT_VERSION,
            "max_iterations": info.max_iterations, "history_keep_recent_results": info.history_keep_recent_results,
            "started_at": info.started_at, "finished_at": info.finished_at, "duration_s": result.duration_s,
        },
        "outcome": {
            "stop_reason": result.stop_reason.value,
            "summary": result.summary,
            "success": verified_success(result),
            "iterations": state.iteration,
            "final_verification": state.final_verification,
            "committed": state.commit_state,
            "commit": result.commit_hash,
            "changed_files": list(state.changed_files),
            "fix_patch": PATCH_FILENAME if result.patch_path else None,
            "regression_seen": state.regression_state,
        },
        "baseline": None if result.baseline is None else {
            "status": result.baseline.status, "reason": result.baseline.reason,
            "failing_ids": result.baseline.failing_ids, "main_error": result.baseline.main_error,
            "tests": _counts(result.baseline.result),
        },
        "final_tests": _counts(result.final_tests),
        "tool_calls": _tool_entries(result, run_dir),
        "patches": _patch_entries(result),
        "events": list(result.events),
        "retrievals_used": state.retrieval_count,
        "files_inspected": list(state.files_inspected),
        "cleanup": {"status": info.cleanup, "workspace_kept": info.workspace_kept},
        "artifacts": existing_artifacts(run_dir, result.final_tests is not None),
    }


def write_json_atomic(path: Path, data: Any) -> Path:
    """Write JSON through a temporary file so a crash never leaves a half file."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False, default=str) + "\n", encoding="utf-8")
    os.replace(tmp, path)
    return path


def write_trace(
    run_dir: Path, result: "RunResult", info: RunInfo, secrets: Sequence[str] = (),
) -> Path:
    """Write ``run_trace.json`` into the run directory."""
    trace = redact_obj(build_trace(result, info, run_dir), secrets)
    return write_json_atomic(Path(run_dir) / TRACE_FILENAME, trace)
