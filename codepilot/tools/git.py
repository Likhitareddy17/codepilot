"""Read-only git_diff tool (TASK-019)."""

from __future__ import annotations

from typing import Any, Dict

from codepilot.errors import GitError, GitTimeoutError, ToolFailure
from codepilot.safety import ensure_readable, resolve_path
from codepilot.tools import ToolContext, ToolOutput, ToolSpec, cap_text, json_len


def git_diff(ctx: ToolContext, args: Dict[str, Any]) -> ToolOutput:
    """Show the changes made so far in this run, against the baseline commit."""
    if ctx.workspace is None:
        raise ToolFailure("GIT_ERROR", "No Git workspace is attached to this run.")
    rel = None
    if args.get("path") is not None:
        rp = resolve_path(ctx.root, args["path"])
        ensure_readable(rp, ctx.settings.read_denylist)
        rel = rp.rel or None
    try:
        result = ctx.workspace.diff(rel)
    except GitTimeoutError as exc:
        raise ToolFailure("TIMEOUT", str(exc)) from exc
    except GitError as exc:
        raise ToolFailure("GIT_ERROR", str(exc)) from exc

    diff = result.diff or ""
    lines = diff.splitlines()
    data: Dict[str, Any] = {
        "files_changed": result.changed_files,
        "insertions": sum(1 for ln in lines if ln.startswith("+") and not ln.startswith("+++")),
        "deletions": sum(1 for ln in lines if ln.startswith("-") and not ln.startswith("---")),
        "diff": diff,
    }
    truncated = False
    if json_len(data) > ctx.budget:
        room = ctx.budget - (json_len({**data, "diff": ""}) + 60)
        data["diff"], truncated = cap_text(diff, max(room, 0))
        truncated = True
    return data, truncated


GIT_DIFF = ToolSpec(
    name="git_diff",
    description="Show the changes made so far in this run, as a diff against the starting point.",
    parameters={
        "type": "object",
        "properties": {"path": {"type": "string"}},
        "additionalProperties": False,
    },
    func=git_diff,
)
