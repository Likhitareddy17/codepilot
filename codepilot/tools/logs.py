"""Application log tool (TASK-016)."""

from __future__ import annotations

from typing import Any, Dict, List

from codepilot.errors import ToolFailure
from codepilot.safety import ensure_readable, resolve_path
from codepilot.tools import ToolContext, ToolOutput, ToolSpec, fit_items


def read_logs(ctx: ToolContext, args: Dict[str, Any]) -> ToolOutput:
    """Return the last lines of the configured log, optionally filtered."""
    s = ctx.settings
    if not s.log_path:
        raise ToolFailure("NOT_FOUND", "No log file is configured for this repository.")
    rp = resolve_path(ctx.root, s.log_path)
    ensure_readable(rp, s.read_denylist)
    if not rp.abs_path.is_file():
        raise ToolFailure("NOT_FOUND", f"The log file {rp.rel!r} does not exist.")

    all_lines = rp.abs_path.read_bytes().decode("utf-8", errors="replace").splitlines()
    requested = min(args.get("n_lines", 100), s.max_log_lines)
    needle = args.get("contains")
    if needle:
        lowered = needle.lower()
        numbered = [(i, ln) for i, ln in enumerate(all_lines, 1) if lowered in ln.lower()]
    else:
        numbered = list(enumerate(all_lines, 1))
    available = len(numbered)
    chosen = numbered[-requested:] if requested else []

    def build(items: List[Any]) -> Dict[str, Any]:
        data: Dict[str, Any] = {
            "path": rp.rel,
            "total_lines": len(all_lines),
            "returned_lines": len(items),
            "lines": [ln for _, ln in items],
        }
        if needle:
            data["line_numbers"] = [n for n, _ in items]
        return data

    kept, cut = fit_items(chosen, build, ctx.budget, keep="tail")
    wanted = min(args.get("n_lines", 100), available)
    return build(kept), cut or len(kept) < wanted


READ_LOGS = ToolSpec(
    name="read_logs",
    description=(
        "Read the last lines of the application log. Use it to find error messages "
        "and stack traces related to the failing tests. Optionally keep only lines "
        "containing some text."
    ),
    parameters={
        "type": "object",
        "properties": {
            "n_lines": {"type": "integer", "minimum": 1, "default": 100},
            "contains": {"type": "string", "minLength": 1},
        },
        "additionalProperties": False,
    },
    func=read_logs,
)
