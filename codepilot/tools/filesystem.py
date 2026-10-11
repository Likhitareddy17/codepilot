"""File listing, reading and code search tools (TASK-012, 013, 014)."""

from __future__ import annotations

import os
import re
import time
from multiprocessing import get_context
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Tuple

from codepilot.errors import ToolFailure
from codepilot.safety import (
    SKIPPED_DIRS, ensure_readable, is_denied, is_protected, is_within, resolve_path,
)
from codepilot.tools import ToolContext, ToolOutput, ToolSpec, cap_text, fit_items, json_len

SEARCH_COUNT_LIMIT = 1000        # stop counting matches after this many
MAX_SEARCH_FILE_BYTES = 2_000_000
LIST_WALK_LIMIT = 20_000         # hard safety bound on entries visited
_NUL_PROBE_BYTES = 8192


# ------------------------------------------------------------------ helpers


def _rel(root: Path, full: str) -> str:
    return os.path.relpath(full, root).replace(os.sep, "/")


def _require_existing(path: Path, rel: str) -> None:
    if not path.exists():
        raise ToolFailure("NOT_FOUND", f"{rel or '.'!r} does not exist. Use list_files to see what is available.")


# --------------------------------------------------------------- list_files


def list_files(ctx: ToolContext, args: Dict[str, Any]) -> ToolOutput:
    """List files and folders below a directory, up to a depth."""
    s = ctx.settings
    rp = resolve_path(ctx.root, args.get("path", "."))
    ensure_readable(rp, s.read_denylist)
    _require_existing(rp.abs_path, rp.rel)
    if not rp.abs_path.is_dir():
        raise ToolFailure("NOT_A_DIRECTORY", f"{rp.rel!r} is a file, not a directory. Use read_file to read it.")
    max_depth = min(args.get("max_depth") or s.list_max_depth, 6)

    entries: List[Dict[str, Any]] = []
    root_str = str(ctx.root)

    def walk(directory: Path, depth_left: int) -> None:
        try:
            names = sorted(os.listdir(directory))
        except OSError:
            return
        for name in names:
            if len(entries) >= LIST_WALK_LIMIT:
                return
            full = os.path.join(directory, name)
            rel = _rel(ctx.root, full)
            if is_denied(rel, s.read_denylist):
                continue
            is_link = os.path.islink(full)
            if is_link and not is_within(Path(os.path.realpath(full)), ctx.root):
                continue
            if os.path.isdir(full):
                if name.lower() in SKIPPED_DIRS:
                    continue
                entries.append({"path": rel, "type": "dir"})
                if depth_left > 1 and not is_link:
                    walk(Path(full), depth_left - 1)
            elif os.path.isfile(full):
                entries.append({
                    "path": rel,
                    "type": "file",
                    "size_bytes": os.path.getsize(full),
                    "protected": is_protected(rel, s.protected_paths),
                })

    walk(rp.abs_path, max_depth)
    entries.sort(key=lambda e: e["path"])
    total = len(entries)
    capped = entries[: s.max_list_entries]

    def build(items: List[Dict[str, Any]]) -> Dict[str, Any]:
        return {"path": rp.rel or ".", "entries": items, "total_entries": total}

    kept, cut = fit_items(capped, build, ctx.budget)
    return build(kept), cut or len(capped) < total


# ---------------------------------------------------------------- read_file


def read_file(ctx: ToolContext, args: Dict[str, Any]) -> ToolOutput:
    """Read a text file, optionally a range of lines."""
    s = ctx.settings
    rp = resolve_path(ctx.root, args["path"])
    ensure_readable(rp, s.read_denylist)
    _require_existing(rp.abs_path, rp.rel)
    if not rp.abs_path.is_file():
        raise ToolFailure("NOT_A_FILE", f"{rp.rel!r} is a directory. Use list_files to see its contents.")

    raw = rp.abs_path.read_bytes()
    if b"\x00" in raw[:_NUL_PROBE_BYTES] or b"\x00" in raw:
        raise ToolFailure("BINARY_FILE", f"{rp.rel!r} is a binary file and cannot be read as text.")
    lines = raw.decode("utf-8", errors="replace").splitlines()
    total = len(lines)

    start = args.get("start_line", 1)
    requested_end = args.get("end_line")
    if requested_end is not None and requested_end < start:
        raise ToolFailure("INVALID_ARGUMENTS", "end_line must be greater than or equal to start_line.")
    if total and start > total:
        raise ToolFailure("INVALID_ARGUMENTS", f"start_line {start} is beyond the end of the file ({total} lines).")

    hard_end = start + s.max_read_lines - 1
    end = min(requested_end if requested_end is not None else hard_end, hard_end, total)
    selected = lines[start - 1:end] if total else []
    truncated = (requested_end is not None and requested_end > end) or (
        requested_end is None and end < total
    )

    def build(items: List[str]) -> Dict[str, Any]:
        return {
            "path": rp.rel,
            "start_line": start,
            "end_line": start + len(items) - 1 if items else start - 1,
            "total_lines": total,
            "content": "\n".join(items),
        }

    kept, cut = fit_items(selected, build, ctx.budget)
    if not kept and selected:
        # Even one line is too large: cut inside the line.
        overhead = json_len(build([""]))
        limit = max(ctx.budget - overhead - 40, 0)
        text, _ = cap_text(selected[0], limit)
        while json_len(build([text])) > ctx.budget and text:
            text = text[: len(text) // 2]
        kept, cut = [text], True
    return build(kept), truncated or cut


# -------------------------------------------------------------- search_code


def _search_files(root: str, start: str, deny: List[str]) -> Iterator[Tuple[str, str]]:
    """Yield (absolute, relative) text-file candidates in a deterministic order."""
    root_path = Path(root)
    if os.path.isfile(start):
        yield start, _rel(root_path, start)
        return
    for dirpath, dirnames, filenames in os.walk(start):
        dirnames[:] = sorted(
            d for d in dirnames
            if d.lower() not in SKIPPED_DIRS
            and not os.path.islink(os.path.join(dirpath, d))
            and not is_denied(_rel(root_path, os.path.join(dirpath, d)), deny)
        )
        for name in sorted(filenames):
            full = os.path.join(dirpath, name)
            rel = _rel(root_path, full)
            if is_denied(rel, deny):
                continue
            if os.path.islink(full) and not is_within(Path(os.path.realpath(full)), root_path):
                continue
            yield full, rel


def run_search(params: Dict[str, Any]) -> Dict[str, Any]:
    """Search files for a pattern. Pure function of picklable ``params``.

    Raises ``TimeoutError`` when the cooperative time budget is exceeded
    (literal searches). Regex searches are additionally hard-killed by the
    parent process, because a backtracking regex cannot be interrupted here.
    """
    flags = 0 if params["case_sensitive"] else re.IGNORECASE
    source = params["pattern"] if params["regex"] else re.escape(params["pattern"])
    rx = re.compile(source, flags)
    deadline = time.monotonic() + params["time_budget"]
    matches: List[Dict[str, Any]] = []
    count = 0
    for full, rel in _search_files(params["root"], params["start"], params["deny"]):
        if time.monotonic() > deadline:
            raise TimeoutError
        try:
            if os.path.getsize(full) > MAX_SEARCH_FILE_BYTES:
                continue
            with open(full, "rb") as handle:
                data = handle.read()
        except OSError:
            continue
        if b"\x00" in data[:_NUL_PROBE_BYTES]:
            continue
        for number, line in enumerate(data.decode("utf-8", errors="replace").splitlines(), 1):
            if rx.search(line):
                count += 1
                if len(matches) < params["max_results"]:
                    text, _ = cap_text(line.strip(), params["max_line_chars"])
                    matches.append({"path": rel, "line": number, "text": text})
                if count >= SEARCH_COUNT_LIMIT:
                    return {"matches": matches, "match_count": count, "hit_limit": True}
    return {"matches": matches, "match_count": count, "hit_limit": False}


def _search_child(conn: Any, started: Any, params: Dict[str, Any]) -> None:
    """Child-process entry point for regex searches."""
    started.set()  # a plain semaphore: works even while a regex holds the GIL
    try:
        conn.send(("ok", run_search(params)))
    except TimeoutError:
        conn.send(("timeout", None))
    except BaseException as exc:  # noqa: BLE001 - report anything back to the parent
        conn.send(("error", f"{type(exc).__name__}: {exc}"))
    finally:
        conn.close()


def _run_regex_search(params: Dict[str, Any], timeout_s: int) -> Dict[str, Any]:
    """Run a regex search in a child process that is killed on timeout.

    The timeout is measured from the moment the child has started, so slow
    process start-up (common on Windows) never counts against the search.
    """
    mp = get_context("spawn")
    started = mp.Event()
    parent_conn, child_conn = mp.Pipe(duplex=False)
    proc = mp.Process(target=_search_child, args=(child_conn, started, params), daemon=True)
    try:
        proc.start()
    except OSError as exc:
        raise ToolFailure("INTERNAL_ERROR", f"Could not start the search process: {exc}") from exc
    child_conn.close()
    try:
        if not started.wait(60) or not parent_conn.poll(timeout_s):
            raise ToolFailure(
                "TIMEOUT",
                f"The regex search did not finish within {timeout_s}s and was stopped. "
                "Use a simpler pattern or a literal search.",
            )
        kind, payload = parent_conn.recv()
    except EOFError:
        raise ToolFailure("INTERNAL_ERROR", "The search process ended unexpectedly.") from None
    finally:
        parent_conn.close()
        if proc.is_alive():
            proc.terminate()
            proc.join(2)
            if proc.is_alive():
                proc.kill()
        proc.join(1)
    if kind == "timeout":
        raise ToolFailure("TIMEOUT", f"The search did not finish within {timeout_s}s. Narrow the path or the pattern.")
    if kind == "error":
        raise ToolFailure("INTERNAL_ERROR", f"Search failed: {payload}")
    return payload


def search_code(ctx: ToolContext, args: Dict[str, Any]) -> ToolOutput:
    """Search text files for a literal string (or a regex when asked)."""
    s = ctx.settings
    pattern = args["pattern"]
    use_regex = args.get("regex", False)
    case_sensitive = args.get("case_sensitive", False)
    rp = resolve_path(ctx.root, args.get("path", "."))
    ensure_readable(rp, s.read_denylist)
    _require_existing(rp.abs_path, rp.rel)
    try:
        re.compile(pattern if use_regex else re.escape(pattern))
    except re.error as exc:
        raise ToolFailure("INVALID_ARGUMENTS", f"Invalid regular expression: {exc}") from exc

    params = {
        "root": str(ctx.root),
        "start": str(rp.abs_path),
        "pattern": pattern,
        "regex": use_regex,
        "case_sensitive": case_sensitive,
        "deny": list(s.read_denylist),
        "max_results": s.max_search_results,
        "max_line_chars": s.max_search_line_chars,
        "time_budget": float(s.command_timeout_s),
    }
    if use_regex:
        found = _run_regex_search(params, s.command_timeout_s)
    else:
        try:
            found = run_search(params)
        except TimeoutError:
            raise ToolFailure("TIMEOUT", f"The search did not finish within {s.command_timeout_s}s. Narrow the path.") from None

    def build(items: List[Dict[str, Any]]) -> Dict[str, Any]:
        return {"pattern": pattern, "match_count": found["match_count"], "matches": items}

    kept, cut = fit_items(found["matches"], build, ctx.budget)
    return build(kept), cut or len(kept) < found["match_count"]


# ----------------------------------------------------------------- specs

LIST_FILES = ToolSpec(
    name="list_files",
    description=(
        "List files and folders under a directory in the repository. Use it to "
        "understand the project layout. Entries marked protected cannot be edited."
    ),
    parameters={
        "type": "object",
        "properties": {
            "path": {"type": "string", "default": "."},
            "max_depth": {"type": "integer", "minimum": 1, "maximum": 6},
        },
        "additionalProperties": False,
    },
    func=list_files,
)

READ_FILE = ToolSpec(
    name="read_file",
    description="Read a text file, optionally a range of lines. Prefer reading only the lines you need.",
    parameters={
        "type": "object",
        "properties": {
            "path": {"type": "string"},
            "start_line": {"type": "integer", "minimum": 1, "default": 1},
            "end_line": {"type": "integer", "minimum": 1},
        },
        "required": ["path"],
        "additionalProperties": False,
    },
    func=read_file,
)

SEARCH_CODE = ToolSpec(
    name="search_code",
    description=(
        "Search text files in the repository for a string (or a regular expression "
        "if regex is true). Returns matching lines with file and line number."
    ),
    parameters={
        "type": "object",
        "properties": {
            "pattern": {"type": "string", "minLength": 1},
            "path": {"type": "string", "default": "."},
            "regex": {"type": "boolean", "default": False},
            "case_sensitive": {"type": "boolean", "default": False},
        },
        "required": ["pattern"],
        "additionalProperties": False,
    },
    func=search_code,
)
