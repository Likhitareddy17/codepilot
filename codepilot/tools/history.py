"""Tool-result persistence, context compression and retrieval (TASK-027 to TASK-029).

Rule: compress context, not evidence. Every result is stored in memory and on
disk (``runs/<run_id>/results/<call_id>.json``). The conversation carries the
most recent results in full and older ones as short structured summaries; the
original can always be fetched back with ``get_tool_result``.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

from codepilot.config import Settings
from codepilot.errors import ToolFailure
from codepilot.models import ToolRecord, ToolResult
from codepilot.tools import ToolContext, ToolOutput, ToolSpec, json_len

CALL_ID_PATTERN = re.compile(r"^c[0-9]{3,9}\Z")   # bounded: a 500-digit id must not reach the file system
_EXCEPTION_NAME = re.compile(r"\b([A-Z][A-Za-z0-9]*(?:Error|Exception))\b")
MAX_LISTED = 10          # cap for lists inside a summary
MAX_FAILING_IDS = 20
MAX_SERIES = 5


# --------------------------------------------------------------- summaries


def _numbers(values: List[Any]) -> List[float]:
    return [v for v in values if isinstance(v, (int, float)) and not isinstance(v, bool)]


def _series_facts(metrics: Dict[str, Any]) -> Dict[str, Any]:
    """min / max / position of max for numeric series (also reduced arrays)."""
    series: Dict[str, Any] = {}

    def visit(name: str, value: Any, depth: int) -> None:
        if len(series) >= MAX_SERIES:
            return
        if isinstance(value, dict) and value.get("_reduced") is True:
            pairs = [(k["index"], k["value"]) for k in value.get("kept", [])
                     if isinstance(k.get("value"), (int, float)) and not isinstance(k.get("value"), bool)]
            if pairs:
                top = max(pairs, key=lambda p: p[1])
                series[name] = {"min": min(v for _, v in pairs), "max": top[1], "max_index": top[0], "length": value.get("length")}
        elif isinstance(value, list) and value and len(_numbers(value)) == len(value):
            top = max(range(len(value)), key=lambda i: value[i])
            series[name] = {"min": min(value), "max": value[top], "max_index": top, "length": len(value)}
        elif isinstance(value, dict) and depth < 3:
            for key, inner in value.items():
                visit(f"{name}.{key}" if name else str(key), inner, depth + 1)

    visit("", metrics, 0)
    return series


def summarize(tool: str, args: Dict[str, Any], result: Dict[str, Any]) -> Dict[str, Any]:
    """Structured summary of one stored result (ARCHITECTURE.md section 5.3)."""
    ok = bool(result.get("ok"))
    data: Dict[str, Any] = result.get("data") or {}
    summary: Dict[str, Any] = {
        "call_id": result.get("call_id"),
        "tool": tool,
        "args": args,
        "ok": ok,
        "truncated": bool(result.get("truncated")),
        "retrievable": True,
    }
    if not ok:
        error = result.get("error") or {}
        code = error.get("code", "ERROR") if isinstance(error, dict) else "ERROR"
        message = error.get("message", "") if isinstance(error, dict) else str(error)
        summary["outcome"] = f"failed: {code}"
        summary["key_facts"] = {"error_code": code, "message": message[:200]}
        return summary

    facts: Dict[str, Any]
    if tool == "list_files":
        entries = data.get("entries", [])
        facts = {"path": data.get("path"), "total_entries": data.get("total_entries"),
                 "files": sum(1 for e in entries if e.get("type") == "file"),
                 "dirs": sum(1 for e in entries if e.get("type") == "dir")}
        outcome = f"{facts['total_entries']} entries under {facts['path']}"
    elif tool == "read_file":
        facts = {"path": data.get("path"), "start_line": data.get("start_line"),
                 "end_line": data.get("end_line"), "total_lines": data.get("total_lines")}
        outcome = f"read {facts['path']} lines {facts['start_line']}-{facts['end_line']} of {facts['total_lines']}"
    elif tool == "search_code":
        files = sorted({m.get("path") for m in data.get("matches", [])})
        facts = {"pattern": data.get("pattern"), "match_count": data.get("match_count"),
                 "files": files[:MAX_LISTED]}
        outcome = f"{facts['match_count']} match(es) for {facts['pattern']!r} in {len(files)} file(s)"
    elif tool == "run_tests":
        facts = {k: data.get(k) for k in ("scope", "passed", "failed", "errors", "timed_out")}
        facts["failing_ids"] = (data.get("failing_ids") or [])[:MAX_FAILING_IDS]
        outcome = (f"{facts['scope']} run: {facts['passed']} passed, {facts['failed']} failed, "
                   f"{facts['errors']} errors" + (" (timed out)" if facts["timed_out"] else ""))
    elif tool == "read_logs":
        names = sorted({n for line in data.get("lines", []) for n in _EXCEPTION_NAME.findall(line)})
        facts = {"total_lines": data.get("total_lines"), "returned_lines": data.get("returned_lines"),
                 "exception_types": names[:MAX_LISTED]}
        outcome = f"{facts['returned_lines']} log line(s); exceptions: {', '.join(names[:MAX_LISTED]) or 'none'}"
    elif tool == "read_metrics":
        metrics = data.get("metrics") if isinstance(data.get("metrics"), dict) else {}
        scalars = {k: v for k, v in metrics.items() if isinstance(v, (int, float)) and not isinstance(v, bool)}
        facts = {"top_level_keys": list(metrics)[:MAX_LISTED * 2], "scalars": dict(list(scalars.items())[:MAX_LISTED]),
                 "series": _series_facts(metrics)}
        outcome = f"metrics with {len(metrics)} top-level key(s)"
    elif tool == "apply_patch":
        facts = {"path": data.get("path"), "applied": data.get("applied"),
                 "lines_added": data.get("lines_added"), "lines_removed": data.get("lines_removed")}
        outcome = f"patched {facts['path']} (+{facts['lines_added']} -{facts['lines_removed']})"
    elif tool == "git_diff":
        facts = {"files_changed": data.get("files_changed"), "insertions": data.get("insertions"),
                 "deletions": data.get("deletions")}
        outcome = f"{len(facts['files_changed'] or [])} file(s) changed, +{facts['insertions']} -{facts['deletions']}"
    elif tool == "get_tool_result":
        facts = {"call_id": data.get("call_id"), "tool": data.get("tool")}
        outcome = f"retrieved {facts['call_id']} ({facts['tool']})"
    else:
        facts = {"keys": sorted(data)[:MAX_LISTED]}
        outcome = f"{tool} succeeded"
    summary["outcome"] = outcome
    summary["key_facts"] = facts
    return summary


# ------------------------------------------------------------------- store


class ResultStore:
    """Holds every tool result in memory and on disk, and renders context views."""

    def __init__(self, run_dir: Optional[Path], settings: Settings) -> None:
        self.run_dir = Path(run_dir) if run_dir is not None else None
        self.keep_recent = settings.history_keep_recent_results
        self.keep_file_reads = settings.history_keep_file_reads
        self.file_reads_max_chars = settings.history_file_reads_max_chars
        self.max_retrievals = settings.max_result_retrievals_per_run
        self.retrieval_count = 0
        self.records: List[ToolRecord] = []
        self.write_errors: List[str] = []
        self._results: Dict[str, Dict[str, Any]] = {}
        self._order: List[str] = []
        self._pinned: Set[str] = set()
        self._file_reads: Dict[str, str] = {}      # path -> call_id of its latest successful read, oldest first
        self._sizes: Dict[str, int] = {}           # call_id -> stored size in characters (file reads only)
        self._retained: Optional[Set[str]] = None  # cache of the retained read ids; reset by add()

    # --- persistence ---

    @property
    def results_dir(self) -> Optional[Path]:
        return self.run_dir / "results" if self.run_dir is not None else None

    def _path(self, call_id: str) -> Optional[Path]:
        if self.results_dir is None or not CALL_ID_PATTERN.match(call_id):
            return None
        return self.results_dir / f"{call_id}.json"

    def add(self, call_id: str, tool: str, args: Any, result: ToolResult, iteration: int = 0) -> ToolRecord:
        """Store one full result (memory first, then disk) and return its trace record."""
        safe_args = args if isinstance(args, dict) else {"_raw": repr(args)}
        entry = {
            "call_id": call_id, "tool": tool, "args": safe_args, "iteration": iteration,
            "ok": result.ok, "result": result.to_dict(),
        }
        self._results[call_id] = entry
        self._order.append(call_id)
        self._track_file_reads(call_id, tool, result)
        path = self._path(call_id)
        saved = ""
        if path is not None:
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                tmp = path.with_suffix(".json.tmp")
                tmp.write_text(json.dumps(entry, indent=2, ensure_ascii=False, default=repr), encoding="utf-8")
                os.replace(tmp, path)
                saved = str(path)
            except OSError as exc:  # memory still holds the evidence
                self.write_errors.append(f"{call_id}: {exc}")
        record = ToolRecord(
            call_id=call_id, iteration=iteration, tool=tool, args=safe_args, ok=result.ok,
            summary=summarize(tool, safe_args, entry["result"]), result_path=saved,
        )
        self.records.append(record)
        return record

    # --- file-read retention ---

    def _track_file_reads(self, call_id: str, tool: str, result: ToolResult) -> None:
        """Remember the latest successful read of each file; forget it when a patch changes the file."""
        path = result.data.get("path") if isinstance(result.data, dict) else None
        if not result.ok or not isinstance(path, str):
            return
        if tool == "read_file":
            self._file_reads.pop(path, None)               # a re-read replaces the older copy and becomes the newest
            self._file_reads[path] = call_id
            self._sizes[call_id] = len(json.dumps(result.to_dict(), ensure_ascii=False, default=repr))
            self._retained = None
        elif tool == "apply_patch":
            self.invalidate_file_read(path)

    def invalidate_file_read(self, path: str) -> None:
        """Forget the retained read of a path after a patch or revert."""
        if self._file_reads.pop(path, None) is not None:
            self._retained = None                         # the retained contents are now stale

    def _retained_reads(self) -> Set[str]:
        """Call ids of the file reads kept in full: newest first, until the count or size cap is reached."""
        if self._retained is None:
            kept: Set[str] = set()
            total = 0
            for call_id in reversed(list(self._file_reads.values())):
                size = self._sizes.get(call_id, 0)
                if size > self.file_reads_max_chars:
                    continue
                if len(kept) >= self.keep_file_reads or total + size > self.file_reads_max_chars:
                    break
                kept.add(call_id)
                total += size
            self._retained = kept
        return self._retained

    def get(self, call_id: str) -> Optional[Dict[str, Any]]:
        """Stored entry for ``call_id``: memory first, then the results file."""
        if call_id in self._results:
            return self._results[call_id]
        path = self._path(call_id)
        if path is not None and path.is_file():
            try:
                return json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                return None
        return None

    def record(self, call_id: str) -> Optional[ToolRecord]:
        return next((r for r in self.records if r.call_id == call_id), None)

    def annotate_summary(self, call_id: str, **facts: Any) -> None:
        """Let the controller add facts (e.g. ``tests_after`` for a patch) to a summary."""
        record = self.record(call_id)
        if record is not None:
            record.summary.setdefault("key_facts", {}).update(facts)

    # --- retrieval (TASK-029) ---

    def retrieve(self, call_id: str) -> Dict[str, Any]:
        """Return a stored entry for the LLM, enforcing the per-run retrieval limit."""
        entry = self.get(call_id)
        if entry is None:
            raise ToolFailure("RESULT_NOT_FOUND", f"No stored result with call_id {call_id!r}.")
        if entry["tool"] == "get_tool_result":
            raise ToolFailure("INVALID_ARGUMENTS", "A retrieval result cannot be retrieved again; retrieve the original call_id.")
        if self.retrieval_count >= self.max_retrievals:
            raise ToolFailure(
                "RETRIEVAL_LIMIT_REACHED",
                f"The limit of {self.max_retrievals} result retrievals per run has been reached. Work with the evidence you already have.",
            )
        self.retrieval_count += 1
        record = self.record(call_id)
        if record is not None:
            record.retrieved_count += 1
        return entry

    # --- context management (TASK-028) ---

    def pin(self, call_id: str) -> None:
        """Keep a result in full for the whole run (e.g. the baseline test result)."""
        self._pinned.add(call_id)

    def _latest_run_tests(self) -> Optional[str]:
        for call_id in reversed(self._order):
            entry = self._results[call_id]
            if entry["tool"] == "run_tests" and entry["ok"]:
                return call_id
        return None

    def is_full(self, call_id: str) -> bool:
        """Recent results, pinned results, patch results, the latest test run and retained file reads stay in full."""
        entry = self._results.get(call_id)
        if entry is None:
            return False
        recent = set(self._order[-self.keep_recent:]) if self.keep_recent > 0 else set()
        return (
            call_id in recent
            or call_id in self._pinned
            or call_id in self._retained_reads()
            or call_id == self._latest_run_tests()
            or (entry["tool"] == "apply_patch" and entry["ok"])
        )

    def render(self, call_id: str) -> str:
        """Message content for one tool result: full JSON, or its structured summary."""
        entry = self._results[call_id]
        if self.is_full(call_id):
            return json.dumps(entry["result"], ensure_ascii=False)
        record = self.record(call_id)
        return json.dumps(record.summary if record else summarize(entry["tool"], entry["args"], entry["result"]),
                          ensure_ascii=False)

    def render_all(self) -> Dict[str, str]:
        """Context view of every stored result, in call order."""
        return {call_id: self.render(call_id) for call_id in self._order}


# -------------------------------------------------------------------- tool


def get_tool_result(ctx: ToolContext, args: Dict[str, Any]) -> ToolOutput:
    """Return the full original result of an earlier tool call."""
    if ctx.history is None:
        raise ToolFailure("INTERNAL_ERROR", "No result store is attached to this run.")
    entry = ctx.history.retrieve(args["call_id"])
    stored = entry["result"]
    data: Dict[str, Any] = {
        "call_id": entry["call_id"], "tool": entry["tool"], "args": entry["args"],
        "ok": entry["ok"], "result": stored,
    }
    truncated = False
    notes = []
    if stored.get("truncated"):
        notes.append(f"The original {entry['tool']} output was already cut.")
    if json_len(data) > ctx.budget:  # the wrapper adds a little on top of an already-capped result
        raw = json.dumps(stored, ensure_ascii=False)
        size = max(ctx.budget - 500, 0)
        data["result"] = {"preview": raw[:size]}
        while size > 0 and json_len(data) > ctx.budget:
            size = size * 3 // 4
            data["result"] = {"preview": raw[:size]}
        notes.append("The stored result is larger than the output limit and is shown cut.")
        truncated = True
    if notes:
        data["note"] = " ".join(notes) + f" Call {entry['tool']} again with a narrower range or path if you need the missing part."
    return data, truncated or bool(stored.get("truncated"))


GET_TOOL_RESULT = ToolSpec(
    name="get_tool_result",
    description=(
        "Get the full original output of an earlier tool call by its call_id. Older "
        "outputs are shown as short summaries; use this when you need the details again."
    ),
    parameters={
        "type": "object",
        "properties": {"call_id": {"type": "string", "pattern": "^c[0-9]{3,}$"}},
        "required": ["call_id"],
        "additionalProperties": False,
    },
    func=get_tool_result,
)
