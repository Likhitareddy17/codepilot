# TOOLS.md

**CodePilot: specification of the 9 tools the LLM can call**

| | |
|---|---|
| **Status** | Draft v1 (decisions T1 to T10 in section 12 need your confirmation) |
| **Read with** | `ARCHITECTURE.md` (modules, loop, history), `AGENTS.md` (safety rules A4, A5) |
| **Implemented in** | `agent/tools.py` (functions), `agent/schemas.py` (schemas), `agent/registry.py` (validation and dispatch), `agent/safety.py` (path rules) |

If this file disagrees with `AGENTS.md` on a safety rule, `AGENTS.md` wins.

---

## 1. Overview

| # | Tool | Kind | Changes files? |
|---|---|---|---|
| 1 | `list_files` | inspect | no |
| 2 | `read_file` | inspect | no |
| 3 | `search_code` | inspect | no |
| 4 | `run_tests` | execute | no (writes JUnit XML outside the workspace) |
| 5 | `read_logs` | inspect | no |
| 6 | `read_metrics` | inspect | no |
| 7 | `apply_patch` | modify | **yes (only tool that can)** |
| 8 | `git_diff` | inspect | no |
| 9 | `get_tool_result` | retrieve | no |

There is **no** shell tool, no create-file tool, no delete tool, and no tool that can run Git write operations. Branch creation, commit and export are done by the controller, not the LLM.

---

## 2. Conventions Shared by All Tools

### 2.1 Result envelope

Every tool returns this shape. Tools never raise to the loop.

```json
{
  "call_id": "c014",
  "ok": true,
  "data": {},
  "error": null,
  "truncated": false
}
```

On failure:

```json
{
  "call_id": "c015",
  "ok": false,
  "data": {},
  "error": { "code": "MATCH_NOT_UNIQUE", "message": "old text appears 3 times; include more surrounding lines" },
  "truncated": false
}
```

- `call_id` is assigned by the registry: `c001`, `c002`, ... in call order within a run.
- `error.message` must be written so the LLM can act on it (what was wrong and what to do next).
- `truncated` is true when output was cut to fit `MAX_TOOL_OUTPUT_CHARS` or a tool-specific cap.

### 2.2 Error codes

| Code | Meaning |
|---|---|
| `UNKNOWN_TOOL` | Tool name not registered |
| `INVALID_ARGUMENTS` | Missing, extra, wrongly typed or out-of-range argument |
| `PATH_OUTSIDE_WORKSPACE` | Path resolves outside the workspace |
| `PATH_DENIED` | Path is on the read denylist |
| `PATH_PROTECTED` | Path is on the protected (no-write) list |
| `NOT_FOUND` | File or directory does not exist |
| `NOT_A_FILE` / `NOT_A_DIRECTORY` | Wrong kind of path |
| `BINARY_FILE` | File is not text |
| `FILE_TYPE_NOT_EDITABLE` | Extension not in `EDITABLE_EXTENSIONS` |
| `MATCH_NOT_FOUND` | `old` text not found |
| `MATCH_NOT_UNIQUE` | `old` text found more than once |
| `SYNTAX_ERROR` | File failed the syntax check after the patch (patch was rolled back) |
| `PATCH_LIMIT_REACHED` | `MAX_PATCH_ATTEMPTS_PER_FILE` exceeded for this file |
| `TIMEOUT` | Command exceeded `COMMAND_TIMEOUT_SECONDS` |
| `INVALID_FORMAT` | File exists but cannot be parsed (for example bad JSON) |
| `RESULT_NOT_FOUND` | `get_tool_result` called with an unknown `call_id` |
| `RETRIEVAL_LIMIT_REACHED` | `MAX_RESULT_RETRIEVALS_PER_RUN` exceeded |
| `GIT_ERROR` | A read-only Git command failed |
| `INTERNAL_ERROR` | Unexpected failure inside CodePilot (also written to the trace) |

### 2.3 Path arguments

All path arguments are **workspace-relative** and use forward slashes (`app/pagination.py`). The registry passes every path through `safety.resolve_path` before the tool sees it. A path is rejected with `PATH_OUTSIDE_WORKSPACE` if any of these hold:

- it is absolute, or has a drive letter (`C:\...`), or is a UNC path (`\\server\share`)
- it contains `..` that escapes the workspace after resolution
- it resolves (following symlinks and Windows junctions) to somewhere outside the workspace
- it contains a NUL character, an alternate-data-stream marker (`file.txt:stream`), or a Windows reserved device name (`CON`, `NUL`, `COM1`, ...)

Comparison against workspace root, denylist and protected list is **case-insensitive and separator-insensitive** on Windows (`Tests\Test_X.py` is the same file as `tests/test_x.py`).

### 2.4 Lists used by the safety layer

| List | Patterns (workspace-relative, matched case-insensitively) |
|---|---|
| **Read denylist** (cannot be read, searched or listed) | `.env`, `.env.*`, `*.pem`, `*.key`, `id_rsa*`, `.git/**` |
| **Protected** (cannot be written) | `tests/**`, `test_*.py`, `*_test.py`, `conftest.py`, `.github/**`, `.env*`, plus the extra files in decision T7: `pytest.ini`, `tox.ini`, `setup.cfg`, `pyproject.toml`, `requirements*.txt`, `.gitignore` |
| **Skipped directories** (not listed or searched) | `.git`, `__pycache__`, `.pytest_cache`, `.venv`, `venv`, `node_modules` |

Denylisted files do not appear in `list_files` or `search_code` output at all. Protected files do appear (the LLM must be able to read tests) and are marked `protected: true` in `list_files`.

### 2.5 Argument validation (done by the registry, before the tool runs)

- Unknown argument names are rejected (`additionalProperties: false`).
- Types, required fields, integer min/max and enums are enforced.
- Any string argument longer than `MAX_ARG_CHARS` is rejected.
- Defaults are applied by the registry, not by the LLM.
- A `call_id` is assigned even when validation fails, and the failed call is stored and traced like any other.

### 2.6 Output size

After a tool returns, the registry cuts `data` to `MAX_TOOL_OUTPUT_CHARS`, sets `truncated: true`, and stores the capped result in the `ResultStore` (see `ARCHITECTURE.md` 5.3). Tool-specific caps below apply first.

### 2.7 Config keys used by tools

All values come from `Settings`; none may be hard-coded in a tool. Starting defaults are proposals (decision T10).

| Key | Used by | Starting default |
|---|---|---|
| `MAX_TOOL_OUTPUT_CHARS` | all | 8000 |
| `MAX_ARG_CHARS` | registry | 20000 |
| `LIST_MAX_DEPTH` | `list_files` | 3 |
| `MAX_LIST_ENTRIES` | `list_files` | 200 |
| `MAX_READ_LINES` | `read_file` | 400 |
| `MAX_SEARCH_RESULTS` | `search_code` | 50 |
| `MAX_SEARCH_LINE_CHARS` | `search_code` | 200 |
| `MAX_LOG_LINES` | `read_logs` | 200 |
| `COMMAND_TIMEOUT_SECONDS` | `run_tests`, `git_diff` | 60 |
| `MAX_PATCH_ATTEMPTS_PER_FILE` | `apply_patch` | 3 |
| `EDITABLE_EXTENSIONS` | `apply_patch` | `.py` |
| `MAX_RESULT_RETRIEVALS_PER_RUN` | `get_tool_result` | 10 |
| `HISTORY_KEEP_RECENT_RESULTS` | history | 5 |
| `LOG_PATH`, `METRICS_PATH` | `read_logs`, `read_metrics` | set per target repo, workspace-relative |

---

## 3. `list_files`

**LLM-facing description:** "List files and folders under a directory in the repository. Use it to understand the project layout. Entries marked protected cannot be edited."

**Parameters**

```json
{
  "type": "object",
  "properties": {
    "path": { "type": "string", "default": "." },
    "max_depth": { "type": "integer", "minimum": 1, "maximum": 6 }
  },
  "additionalProperties": false
}
```

`max_depth` defaults to `LIST_MAX_DEPTH` and cannot exceed 6.

**Output (`data`)**

```json
{
  "path": "app",
  "entries": [
    { "path": "app/pagination.py", "type": "file", "size_bytes": 812, "protected": false },
    { "path": "app/utils", "type": "dir" }
  ],
  "total_entries": 2
}
```

**Behavior:** sorted by path; skipped directories and denylisted files omitted; at most `MAX_LIST_ENTRIES` entries (then `truncated: true`).
**Errors:** `INVALID_ARGUMENTS`, `PATH_OUTSIDE_WORKSPACE`, `NOT_FOUND`, `NOT_A_DIRECTORY`.
**Summary key_facts:** `path`, `total_entries`, count of files and dirs.

---

## 4. `read_file`

**LLM-facing description:** "Read a text file, optionally a range of lines. Prefer reading only the lines you need."

**Parameters**

```json
{
  "type": "object",
  "properties": {
    "path": { "type": "string" },
    "start_line": { "type": "integer", "minimum": 1, "default": 1 },
    "end_line": { "type": "integer", "minimum": 1 }
  },
  "required": ["path"],
  "additionalProperties": false
}
```

**Output (`data`)**

```json
{
  "path": "app/pagination.py",
  "start_line": 1,
  "end_line": 40,
  "total_lines": 62,
  "content": "def paginate(items, page, size):\n    ..."
}
```

**Behavior:**
- `content` is the **raw text** with no line-number prefixes (so it can be copied into `apply_patch`).
- Returns at most `MAX_READ_LINES` lines per call; if more were requested or the file is longer, `truncated: true` and `end_line` shows where it stopped.
- Read as UTF-8; invalid bytes replaced, not fatal. A file containing NUL bytes is `BINARY_FILE`.
- Protected files can be read. Denylisted files return `PATH_DENIED`.
- `end_line < start_line` is `INVALID_ARGUMENTS`.

**Errors:** `INVALID_ARGUMENTS`, `PATH_OUTSIDE_WORKSPACE`, `PATH_DENIED`, `NOT_FOUND`, `NOT_A_FILE`, `BINARY_FILE`.
**Summary key_facts:** `path`, `start_line`-`end_line`, `total_lines`.
**Controller side effect:** the path is added to `RunState.files_inspected`.

---

## 5. `search_code`

**LLM-facing description:** "Search text files in the repository for a string (or a regular expression if regex is true). Returns matching lines with file and line number."

**Parameters**

```json
{
  "type": "object",
  "properties": {
    "pattern": { "type": "string", "minLength": 1 },
    "path": { "type": "string", "default": "." },
    "regex": { "type": "boolean", "default": false },
    "case_sensitive": { "type": "boolean", "default": false }
  },
  "required": ["pattern"],
  "additionalProperties": false
}
```

**Output (`data`)**

```json
{
  "pattern": "paginate",
  "match_count": 4,
  "matches": [
    { "path": "app/pagination.py", "line": 3, "text": "def paginate(items, page, size):" }
  ]
}
```

**Behavior:**
- Pure Python (directory walk plus `re`); no external search program.
- Literal match by default; `regex: true` uses Python `re`. Invalid regex is `INVALID_ARGUMENTS` with the regex error text.
- Skips skipped directories, denylisted files, and binary files.
- Each matched line is cut to `MAX_SEARCH_LINE_CHARS`.
- At most `MAX_SEARCH_RESULTS` matches returned; `match_count` is the total found (counted up to a safety limit), and `truncated` is true when matches were cut.
- Regex runs must be protected against runaway patterns: apply the per-call time limit `COMMAND_TIMEOUT_SECONDS` and stop with `TIMEOUT`.

**Errors:** `INVALID_ARGUMENTS`, `PATH_OUTSIDE_WORKSPACE`, `NOT_FOUND`, `TIMEOUT`.
**Summary key_facts:** `pattern`, `match_count`, list of files matched (capped).

---

## 6. `run_tests`

**LLM-facing description:** "Run the project's tests with pytest and return the results. With no target it runs the whole suite. Pass a test file or test id to run less."

**Parameters**

```json
{
  "type": "object",
  "properties": {
    "target": { "type": "string", "description": "Workspace-relative file, folder, or pytest node id, e.g. tests/test_cart.py::test_discount" }
  },
  "additionalProperties": false
}
```

**Output (`data`)**: the `TestRunResult` fields from `ARCHITECTURE.md` section 8, plus `scope`.

```json
{
  "scope": "full",
  "exit_code": 1,
  "passed": 5,
  "failed": 3,
  "errors": 0,
  "failing_ids": ["tests/test_cart.py::test_discount", "tests/test_pagination.py::test_last_page"],
  "duration_s": 1.8,
  "timed_out": false,
  "output_excerpt": "..."
}
```

**Behavior:**
- Command: `sys.executable -m pytest -q -p no:cacheprovider --junitxml=<runs/run_id/junit.xml> [target]`, working directory = workspace, argument list (no shell), sanitised environment, `PYTHONDONTWRITEBYTECODE=1`.
- `target` is validated: the file part must resolve inside the workspace (section 2.3), and it must **not start with `-`** (prevents passing pytest options). Only one target is accepted.
- `scope` is `"full"` when no target is given, otherwise `"targeted"`.
- Counts and `failing_ids` come from the JUnit XML; fallback is the pytest summary line.
- `output_excerpt` holds the failure sections of pytest's output (tracebacks, assertion messages), capped.
- On timeout the process is killed, `timed_out: true`, `ok` stays true (the tool worked; the tests did not finish), and partial output is returned.
- `ok: false` only for tool-level problems (`INVALID_ARGUMENTS`, path errors, pytest could not start).

**Rule (decision T2):** only **full-suite** runs (scope `"full"`) are used by the controller for `SUCCESS`, `REGRESSION` and `NO_PROGRESS`. Targeted runs are information for the LLM only.

**Errors:** `INVALID_ARGUMENTS`, `PATH_OUTSIDE_WORKSPACE`, `NOT_FOUND`, `INTERNAL_ERROR`.
**Summary key_facts:** `scope`, `passed`, `failed`, `errors`, `failing_ids`, `timed_out`.

---

## 7. `read_logs`

**LLM-facing description:** "Read the last lines of the application log. Use it to find error messages and stack traces related to the failing tests. Optionally keep only lines containing some text."

**Parameters**

```json
{
  "type": "object",
  "properties": {
    "n_lines": { "type": "integer", "minimum": 1, "default": 100 },
    "contains": { "type": "string", "minLength": 1 }
  },
  "additionalProperties": false
}
```

`n_lines` is capped at `MAX_LOG_LINES`.

**Output (`data`)**

```json
{
  "path": "logs/app.log",
  "total_lines": 412,
  "returned_lines": 100,
  "lines": ["2026-... ERROR ...", "Traceback (most recent call last):", "..."]
}
```

**Behavior:**
- The log file is fixed by `LOG_PATH` in config; the LLM cannot choose another file.
- Without `contains`: the last `n_lines` lines.
- With `contains`: case-insensitive substring filter applied to the whole file first, then the last `n_lines` of the matches are returned (each with its original line number in `line_numbers`).
- Log content is **untrusted text** (see `AGENTS.md` A4); the tool does not interpret it.

**Errors:** `INVALID_ARGUMENTS`, `NOT_FOUND` (no log configured or file missing), `PATH_DENIED` (if `LOG_PATH` is misconfigured to a denylisted file).
**Summary key_facts:** `total_lines`, `returned_lines`, distinct exception type names found (for example `TypeError`, `IndexError`).

---

## 8. `read_metrics`

**LLM-facing description:** "Read the application metrics file (for example error rate and latency over time). Use it to see when a problem started and how large it is."

**Parameters:** none.

```json
{ "type": "object", "properties": {}, "additionalProperties": false }
```

**Output (`data`)**

```json
{
  "path": "logs/metrics.json",
  "metrics": { }
}
```

**Behavior:**
- The file is fixed by `METRICS_PATH` in config. MVP supports **JSON only** (decision T4).
- Parsed with the standard `json` module. If the serialised result exceeds `MAX_TOOL_OUTPUT_CHARS`, long arrays are reduced (keep first and last elements and any extreme values) and `truncated: true`.
- Invalid JSON is `INVALID_FORMAT` with the parser's message and line.

**Errors:** `NOT_FOUND`, `INVALID_FORMAT`.
**Summary key_facts:** top-level keys, and for numeric series the min, max and the position of the max.

---

## 9. `apply_patch`

The **only** tool that changes files. Implements `AGENTS.md` A5.

**LLM-facing description:** "Replace one exact piece of text in one file with new text. The old text must appear exactly once; include a few surrounding lines if needed. Make the smallest change that fixes the cause. Test files and configuration cannot be edited."

**Parameters**

```json
{
  "type": "object",
  "properties": {
    "path": { "type": "string" },
    "old": { "type": "string", "minLength": 1 },
    "new": { "type": "string" }
  },
  "required": ["path", "old", "new"],
  "additionalProperties": false
}
```

`new` may be an empty string (deleting the old text).

**Procedure (in this order; stop at the first failure):**

1. Validate path (section 2.3). Reject `PATH_PROTECTED`, `PATH_DENIED`.
2. File must exist and be a file. Extension must be in `EDITABLE_EXTENSIONS` (`FILE_TYPE_NOT_EDITABLE` otherwise).
3. If `old == new`: `INVALID_ARGUMENTS`.
4. If the per-file counter has reached `MAX_PATCH_ATTEMPTS_PER_FILE`: `PATCH_LIMIT_REACHED`.
5. Read the file as bytes, decode UTF-8, remember the **original bytes** and the file's dominant line ending (CRLF or LF).
6. Convert line endings in `old` and `new` to the file's line ending, so a match is not lost only because of `\r\n` versus `\n`.
7. Count occurrences of `old`. Zero: `MATCH_NOT_FOUND` (message suggests re-reading the file). More than one: `MATCH_NOT_UNIQUE`.
8. Write the new content (UTF-8, `newline=""` so endings are preserved).
9. Increment the per-file counter (decision T6: counts patches that were written, including ones later reverted).
10. If the file ends in `.py`, run a syntax check (`py_compile`, or `ast.parse` on the new text). On failure: **restore the original bytes exactly** and return `SYNTAX_ERROR` with the compiler message.
11. Build a unified diff (`difflib`), store a `PatchRecord`, return success.

**Output (`data`)**

```json
{
  "path": "app/pagination.py",
  "applied": true,
  "syntax_ok": true,
  "diff": "--- a/app/pagination.py\n+++ b/app/pagination.py\n@@ ...",
  "patch_attempts_for_file": 1
}
```

**Not done by this tool:** running tests. After a successful patch the **controller** runs the full suite and attaches a compact result to this tool's message (`ARCHITECTURE.md` 5.2). If that run shows a regression, the controller restores the file from the stored original bytes (the tool exposes a `revert(path)` helper for the controller only; it is not an LLM tool).

**Errors:** `INVALID_ARGUMENTS`, `PATH_OUTSIDE_WORKSPACE`, `PATH_DENIED`, `PATH_PROTECTED`, `NOT_FOUND`, `NOT_A_FILE`, `FILE_TYPE_NOT_EDITABLE`, `BINARY_FILE`, `PATCH_LIMIT_REACHED`, `MATCH_NOT_FOUND`, `MATCH_NOT_UNIQUE`, `SYNTAX_ERROR`.
**Summary key_facts:** `path`, `applied`, lines added and removed, and the full-suite counts after it (added by the controller).

---

## 10. `git_diff`

**LLM-facing description:** "Show the changes made so far in this run, as a diff against the starting point."

**Parameters**

```json
{
  "type": "object",
  "properties": {
    "path": { "type": "string" }
  },
  "additionalProperties": false
}
```

**Output (`data`)**

```json
{
  "files_changed": ["app/pagination.py"],
  "insertions": 1,
  "deletions": 1,
  "diff": "diff --git a/app/pagination.py ..."
}
```

**Behavior:**
- Runs `git diff --no-color --no-ext-diff --no-textconv [-- <path>]` and `git diff --numstat` in the workspace, as argument lists with a timeout. `--no-ext-diff` and `--no-textconv` stop repository configuration from running external programs.
- The workspace's local Git config sets `core.autocrlf=false` so line-ending differences do not appear as changes (set by `workspace.py` at init).
- Optional `path` is validated like any other path.
- Empty diff returns `files_changed: []` and `diff: ""`.
- Output capped; `truncated: true` if cut.

**Errors:** `INVALID_ARGUMENTS`, `PATH_OUTSIDE_WORKSPACE`, `PATH_DENIED`, `TIMEOUT`, `GIT_ERROR`.
**Summary key_facts:** `files_changed`, `insertions`, `deletions`.

---

## 11. `get_tool_result`

**LLM-facing description:** "Get the full original output of an earlier tool call by its call_id. Older outputs are shown as short summaries; use this when you need the details again."

**Parameters**

```json
{
  "type": "object",
  "properties": {
    "call_id": { "type": "string", "pattern": "^c[0-9]{3,}$" }
  },
  "required": ["call_id"],
  "additionalProperties": false
}
```

**Output (`data`)**

```json
{
  "call_id": "c004",
  "tool": "read_file",
  "args": { "path": "app/pagination.py" },
  "ok": true,
  "result": { }
}
```

**Behavior:**
- Reads from the `ResultStore` (memory first, then `runs/<run_id>/results/<call_id>.json`).
- Returns the result exactly as stored (already capped and safety-filtered when first produced). If the original had `truncated: true`, the cut part cannot be recovered; the message tells the LLM to call the original tool again with a narrower range.
- It cannot retrieve a result produced by `get_tool_result` itself (`INVALID_ARGUMENTS`), to prevent chains.
- Counts toward the per-iteration tool-call cap and the per-run retrieval limit.
- Read-only. Cannot expose anything that was not already returned to the LLM.

**Errors:** `INVALID_ARGUMENTS`, `RESULT_NOT_FOUND`, `RETRIEVAL_LIMIT_REACHED`.
**Summary key_facts:** `call_id` retrieved, tool name.

---

## 12. Decisions for Your Confirmation

These are my proposals. None of them has been confirmed by you yet.

| # | Proposal | Why | Alternative |
|---|---|---|---|
| T1 | Tool errors are an object `{code, message}` (this replaces the plain string shown in `AGENTS.md` A3) | Machine-checkable codes plus a readable message | Plain string |
| T2 | Only full-suite test runs count for `SUCCESS`, `REGRESSION`, `NO_PROGRESS`; targeted `run_tests` calls are informational | A passing single test must never end the run | Count any run |
| T3 | `search_code` is literal by default, regex only with `regex: true` | Fewer invalid-pattern errors from the LLM | Always regex |
| T4 | `read_metrics` supports JSON only in the MVP | Matches the sandbox design in the PRD | JSON and CSV |
| T5 | `read_logs` has an optional `contains` filter | Finds a specific stack trace in a long log | Tail only |
| T6 | The per-file patch limit counts patches that were written (including reverted ones), not rejected calls | A bad `old` string should not use up the allowance | Count every call |
| T7 | Extend the protected list beyond `AGENTS.md` A4 with `pytest.ini`, `tox.ini`, `setup.cfg`, `pyproject.toml`, `requirements*.txt`, `.gitignore` | Otherwise the agent could change pytest settings (for example deselect failing tests) instead of fixing code | Keep A4 list only |
| T8 | `EDITABLE_EXTENSIONS` defaults to `.py` only; syntax check runs for `.py` | Smallest scope for the MVP | Allow any non-protected text file |
| T9 | Arguments are validated by a small hand-written validator (type, required, range, enum, no extra fields), not the `jsonschema` package | Keeps dependencies minimal (`AGENTS.md` section B3) | Add `jsonschema` |
| T10 | Starting numeric defaults in section 2.7 | Small enough for free-tier limits; adjust after the first live run | Other values |

If you confirm these, the edits needed in other files are: `AGENTS.md` A3 (error shape T1) and A4 (protected list T7).

---

## 13. Required Tests (summary; details go in `TESTING.md`)

| Area | Must be tested |
|---|---|
| Path rules | `..` escape, absolute path, drive letter, UNC path, NUL, `file.txt:stream`, reserved names (`CON`), mixed case (`Tests/Test_X.py`), symlink or junction out of the workspace |
| Denylist and protected | `.env` unreadable and unlisted; every protected pattern rejected by `apply_patch`; protected files still readable |
| `apply_patch` | unique match applies; zero matches; multiple matches; CRLF file with LF `old`; empty `new`; syntax error is rolled back with byte-identical file; per-file limit; non-`.py` file rejected |
| `run_tests` | passing suite, failing suite, timeout, target starting with `-` rejected, targeted vs full `scope` |
| `read_file` | line range, cap and `truncated`, binary file, non-UTF-8 bytes |
| `search_code` | literal vs regex, invalid regex, result cap, skips denylisted and binary files |
| `read_logs` / `read_metrics` | tail, `contains`, missing file, invalid JSON, large series reduction |
| `git_diff` | empty diff, diff after patch, no CRLF noise, external diff drivers not executed |
| `get_tool_result` | retrieve, unknown id, limit reached, cannot retrieve a retrieval, truncated original |
| Registry | unknown tool, extra argument, wrong type, oversized string, `call_id` assigned on failure |
