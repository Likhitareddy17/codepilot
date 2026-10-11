"""The only tool that changes files: apply_patch (TASK-018). Implements AGENTS.md A5."""

from __future__ import annotations

import ast
import difflib
from typing import Any, Dict, Optional

from codepilot.errors import ToolFailure
from codepilot.models import PatchRecord
from codepilot.safety import (
    ensure_writable, is_editable, normalize_rel_path, resolve_path,
)
from codepilot.tools import ToolContext, ToolOutput, ToolSpec, cap_text, json_len


def _to_eol(text: str, eol: str) -> str:
    """Rewrite line endings of ``text`` to ``eol`` so a match survives CRLF files."""
    text = text.replace("\r\n", "\n")
    return text.replace("\n", "\r\n") if eol == "\r\n" else text


def apply_patch(ctx: ToolContext, args: Dict[str, Any]) -> ToolOutput:
    """Replace one exact piece of text in one existing source file."""
    s = ctx.settings
    rp = resolve_path(ctx.root, args["path"])
    ensure_writable(rp, s.read_denylist, s.protected_paths)

    target = rp.abs_path
    if not target.exists():
        raise ToolFailure(
            "NOT_FOUND",
            f"{rp.rel!r} does not exist. CodePilot can only modify existing files; "
            "it cannot create new ones.",
        )
    if not target.is_file():
        raise ToolFailure("NOT_A_FILE", f"{rp.rel!r} is a directory, not a file.")
    if not is_editable(rp.rel, s.editable_extensions):
        raise ToolFailure(
            "FILE_TYPE_NOT_EDITABLE",
            f"Only files with these extensions can be edited: {', '.join(s.editable_extensions)}.",
        )
    old, new = args["old"], args["new"]
    if old == new:
        raise ToolFailure("INVALID_ARGUMENTS", "old and new are identical; nothing to change.")
    attempts = ctx.patch_attempts.get(rp.rel, 0)
    if attempts >= s.max_patch_attempts_per_file:
        raise ToolFailure(
            "PATCH_LIMIT_REACHED",
            f"{rp.rel!r} has already been patched {attempts} times (limit "
            f"{s.max_patch_attempts_per_file}). Step back and re-check your diagnosis.",
        )

    original = target.read_bytes()
    try:
        text = original.decode("utf-8")
    except UnicodeDecodeError:
        raise ToolFailure("BINARY_FILE", f"{rp.rel!r} is not valid UTF-8 text.") from None
    if "\x00" in text:
        raise ToolFailure("BINARY_FILE", f"{rp.rel!r} is a binary file.")

    crlf = text.count("\r\n")
    eol = "\r\n" if crlf > text.count("\n") - crlf else "\n"
    old_n, new_n = _to_eol(old, eol), _to_eol(new, eol)
    occurrences = text.count(old_n)
    if occurrences == 0:
        raise ToolFailure(
            "MATCH_NOT_FOUND",
            "The old text was not found. Re-read the file with read_file and copy the exact text.",
        )
    if occurrences > 1:
        raise ToolFailure(
            "MATCH_NOT_UNIQUE",
            f"The old text appears {occurrences} times; include more surrounding lines so it matches exactly once.",
        )

    new_text = text.replace(old_n, new_n, 1)
    target.write_bytes(new_text.encode("utf-8"))
    ctx.patch_attempts[rp.rel] = attempts + 1  # counts patches that were written (T6)

    if rp.rel.lower().endswith(".py"):
        try:
            ast.parse(new_text, filename=rp.rel)
        except (SyntaxError, ValueError) as exc:
            target.write_bytes(original)  # byte-identical restore
            detail = f"{type(exc).__name__}: {getattr(exc, 'msg', exc)}"
            line = getattr(exc, "lineno", None)
            raise ToolFailure(
                "SYNTAX_ERROR",
                f"The patch was rolled back because the file no longer parses: {detail}"
                + (f" (line {line})." if line else "."),
            ) from None

    diff_lines = list(difflib.unified_diff(
        text.splitlines(keepends=True), new_text.splitlines(keepends=True),
        fromfile=f"a/{rp.rel}", tofile=f"b/{rp.rel}",
    ))
    body = diff_lines[2:]
    diff = "".join(diff_lines)
    record = PatchRecord(iteration=ctx.iteration, path=rp.rel, diff=diff, applied=True)
    ctx.patches.append(record)
    ctx.undo_stack.append((record, original))

    data: Dict[str, Any] = {
        "path": rp.rel,
        "applied": True,
        "syntax_ok": True,
        "diff": diff,
        "patch_attempts_for_file": attempts + 1,
        "lines_added": sum(1 for ln in body if ln.startswith("+")),
        "lines_removed": sum(1 for ln in body if ln.startswith("-")),
    }
    truncated = False
    if json_len(data) > ctx.budget:
        room = ctx.budget - (json_len({**data, "diff": ""}) + 60)
        data["diff"], truncated = cap_text(diff, max(room, 0))
        truncated = True
    return data, truncated


def revert_patch(ctx: ToolContext, path: Optional[str] = None) -> Optional[PatchRecord]:
    """Controller-only helper: undo the most recent patch (optionally of one file).

    Restores the exact bytes from before the patch. The per-file attempt counter
    is not decremented: reverted patches still count (decision T6).
    """
    wanted = normalize_rel_path(path).lower() if path is not None else None
    for index in range(len(ctx.undo_stack) - 1, -1, -1):
        record, original = ctx.undo_stack[index]
        if wanted is None or record.path.lower() == wanted:
            (ctx.root / record.path).write_bytes(original)
            record.reverted = True
            del ctx.undo_stack[index]
            return record
    return None


APPLY_PATCH = ToolSpec(
    name="apply_patch",
    description=(
        "Replace one exact piece of text in one file with new text. The old text must "
        "appear exactly once; include a few surrounding lines if needed. Make the "
        "smallest change that fixes the cause. Test files and configuration cannot be edited."
    ),
    parameters={
        "type": "object",
        "properties": {
            "path": {"type": "string"},
            "old": {"type": "string", "minLength": 1},
            "new": {"type": "string"},
        },
        "required": ["path", "old", "new"],
        "additionalProperties": False,
    },
    func=apply_patch,
)
