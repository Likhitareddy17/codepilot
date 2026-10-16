# ARCHITECTURE.md

**CodePilot: system design for the MVP**

| | |
|---|---|
| **Status** | Draft v4 (all decisions in section 15 confirmed) |
| **Read with** | `PRD.md` (what), `AGENTS.md` (rules). If this file disagrees with `AGENTS.md` on a safety rule, `AGENTS.md` wins. |
| **Fixed inputs** | Python 3.11+, Windows development, `requirements.txt`, Groq free tier (OpenAI-compatible API), pytest, Git, GitHub Actions |

This document tells a coding agent **how the system is structured**: which modules exist, what each owns, how data flows, and what must not be built. Tool-level detail is in `TOOLS.md`, the demo project in `SANDBOX_REPO.md`, prompts in `PROMPTS.md`.

---

## 1. System Context

```text
 Developer (CLI)
      |
      v
 +-----------------------------------------------------------+
 | CodePilot process (Python, local Windows machine)         |
 |                                                           |
 |  main.py -> Controller (agent loop)                       |
 |                |            |             |               |
 |                v            v             v               |
 |           LLMClient    ToolRegistry    Workspace/Git      |
 |                |            |             |               |
 +----------------|------------|-------------|---------------+
                  |            |             |
                  v            v             v
        Groq API (HTTPS)   Tools operate   Temp workspace dir
        only network        ONLY inside    (copy of sandbox repo,
        dependency          the workspace   own git repo)
                                              |
                                              v
                                   runs/<run_id>/ (trace, report,
                                   patch) written OUTSIDE workspace
```

Only two things leave the machine: LLM requests to Groq, and (later, opt-in) nothing else in the MVP. CodePilot never pushes.

---

## 2. Design Rules

1. The **controller** (plain Python) owns the loop, limits, Git, and the final verdict. The **LLM** only chooses tool calls and writes explanations.
2. **Success is decided by the controller's own test run**, never by what the LLM says.
3. Tools are plain functions behind a registry. The controller and the LLM client know nothing about individual tools.
4. The LLM client is the only module that knows about Groq or the `openai` SDK.
5. All state is held in typed Python objects and saved to the trace, not left only in the chat history.
6. Every external operation (LLM call, subprocess, file I/O, Git) returns a structured result or raises one defined error type. No bare exceptions escape to the loop.

---

## 3. Modules

```text
codepilot/
├── codepilot/
│   ├── __init__.py
│   ├── cli.py               CLI entry point
│   ├── config.py            Settings loaded from environment / CLI flags
│   ├── errors.py            Defined error types
│   ├── models.py            Typed domain models
│   ├── state.py             Run state and iteration tracking
│   ├── workspace.py         Create temp workspace, git init, branch, commit, export patch, cleanup
│   ├── controller.py        The agent loop, limits, stop conditions
│   ├── trace.py             Trace recording and output management
│   ├── report.py            Write run_trace.json and report.md
│   ├── prompts.py           System prompt and message builders
│   ├── llm/
│   │   ├── __init__.py
│   │   ├── client.py        LLMClient interface + OpenAI-compatible (Groq) implementation
│   │   └── fake.py          Offline FakeLLM for automated tests
│   └── tools/
│       ├── __init__.py
│       ├── registry.py      ToolRegistry: name -> (function, JSON schema), argument validation
│       ├── filesystem.py    File listing, reading, and patching tools
│       ├── tests.py         Test execution tool
│       ├── logs.py          Log reading tool
│       ├── metrics.py       Metrics reading tool
│       ├── patch.py         Patch application tool
│       ├── git.py           Git diff tool
│       └── history.py       Result retrieval tool and history management
├── sandbox_repo/            Demo project with seeded bugs (never modified in place)
├── runs/                    Output per run (git-ignored)
├── tests/
├── .github/workflows/ci.yml
├── requirements.txt
└── pyproject.toml
```

### Module responsibilities

| Module | Owns | Depends on | Must NOT |
|---|---|---|---|
| `cli.py` | Parse CLI args, call `Controller.run`, print the final summary | `config`, `controller` | Contain logic |
| `config.py` | Single `Settings` object (limits, paths, provider settings) | stdlib, `python-dotenv` | Hold secrets in logs or repr |
| `errors.py` | Defined error types | stdlib | Do I/O |
| `models.py` | Typed domain models | stdlib | Do I/O |
| `state.py` | `RunState`, `TestRunResult`, `PatchRecord`, `ToolResult`, `StopReason` | stdlib | Do I/O |
| `workspace.py` | Everything Git and temp-folder related | `subprocess`, `shutil`, `tempfile` | Push, reset, rebase, force anything |
| `controller.py` | Loop, iteration counting, stop conditions, final verification, calling `workspace` and `report` | everything below | Know how a specific tool works |
| `trace.py` | Trace recording and persistence | `state` | Alter state |
| `report.py` | Serialise state to `run_trace.json` and `report.md` | `state` | Alter state |
| `prompts.py` | Static system prompt text; helpers to format observations | none | Call the LLM |
| `llm/client.py` | Send messages + tool schemas, return text and/or tool calls, retry with backoff | `openai`, `config`, `errors` | Execute tools, touch files |
| `llm/fake.py` | Offline FakeLLM implementation | `llm/client` | Touch the network |
| `tools/registry.py` | Look up a tool, validate arguments against its schema, call it, wrap errors into `ToolResult` | `tools/*` | Call the LLM |
| `tools/*.py` | The individual tool operations | `safety`, `test_runner`, `workspace`, `history` | Contain LLM reasoning |

---

## 4. Run Lifecycle

```text
 1. Startup checks
    - Python >= 3.11, git on PATH, source repo path exists, API key present (never printed)
 2. Create run directory   runs/<run_id>/      (run_id = timestamp)
 3. Create workspace       temp dir; copy source repo; git init; set local git identity;
                           commit everything as "baseline" on the default branch;
                           create and switch to branch codepilot/fix-<run_id>
 4. Baseline test run      full suite; save result; record failing test ids
                           - if nothing fails: stop with reason NO_FAILURES
 5. Loop (up to MAX_ITERATIONS)          see section 5
 6. Final verification     controller runs the FULL suite itself
 7. On pass: commit on the fix branch; export fix.patch
 8. Write outputs          run_trace.json, report.md, fix.patch, final_tests.txt
 9. Print summary          stop reason, before/after counts, where outputs are
10. Cleanup                delete workspace unless --keep-workspace
```

If any step before the loop fails (for example Git missing), stop with a clear message and a non-zero exit code. Never start editing without a working baseline commit.

---

## 5. The Agent Loop

### 5.1 Message flow

```text
system prompt            (rules, constraints, untrusted-content warning)
user task message        (task text + baseline test summary + tool list)
loop:
    assistant -> tool call(s) | final text
    tool result messages appended
```

- The conversation uses the provider's **native tool calling**.
- Each assistant turn may contain several tool calls; the controller executes at most `MAX_TOOL_CALLS_PER_ITERATION`.
- An **iteration** = one LLM response plus execution of its tool calls.
- **Iteration budget.** `MAX_ITERATIONS` defaults to 15 (ADR-027). Some models return only one tool call per reply (observed: `openai/gpt-oss-120b` on Groq, which needed 11 iterations for the sandbox), so the budget must allow roughly one iteration per file read and per patch. A model that batches independent reads needs far fewer.

### 5.2 Per-iteration algorithm

```text
for iteration in 1..MAX_ITERATIONS:
    response = llm.chat(messages, tool_schemas)          # retries handled inside llm.py
    record response in state
    if response has tool calls:
        for each call (up to the per-iteration cap):
            result = registry.execute(call)              # never raises
            if call was apply_patch and succeeded:
                run full test suite (controller), store TestRunResult,
                append a compact test summary to that tool result
                check regression and no-progress rules (section 6)
            append result message
        continue
    else:                                                # model says it is done
        verdict = controller runs FULL suite itself
        if all pass: stop SUCCESS
        else: append the real failures as a user message and continue
stop MAX_ITERATIONS
```

Why the controller runs tests after each patch: it saves LLM requests (important on a free tier), and it guarantees that regression detection and verification do not depend on the model remembering to test.

### 5.3 Message history management

Free-tier limits are per minute and per day, so the history sent to the LLM must stay small without losing information the agent may need later.

**Rule:** every full tool result is stored; the conversation carries summaries of old results; the agent can ask for an original back.

1. **Store.** Every tool result (after safety filtering and the `MAX_TOOL_OUTPUT_CHARS` cap) is saved by `history.ResultStore` under a `call_id`, both in memory and on disk in `runs/<run_id>/results/<call_id>.json`.
2. **Keep recent results in full.** The most recent results (count set by `HISTORY_KEEP_RECENT_RESULTS` in config) stay in the messages as full text.
3. **Summarise older results.** An older result is replaced in the messages by a **structured summary**:

```text
call_id         e.g. "c014"
tool            e.g. "read_file"
args            the arguments used (paths, ranges, patterns)
ok              true / false
outcome         one-line headline
key_facts       tool-specific facts (see below)
truncated       whether the stored result was already cut
retrievable     true
```

   Key facts per tool (the coding agent implements one summariser per tool, listed in `TOOLS.md`): `run_tests` keeps pass/fail counts and failing test ids; `read_file` keeps path, line range and size; `search_code` keeps match count and the files matched; `read_logs`/`read_metrics` keep the headline findings (error types, error-rate extremes); `apply_patch` keeps path, applied or not, and the test counts after it; `git_diff` keeps changed files and line counts.
4. **Never summarised:** the system prompt, the task message, the baseline and latest test results, and all patch records and their diffs.
5. **Retrieval.** The LLM can call the `get_tool_result` tool with a `call_id` to get the original stored result back. This is read-only, returns at most `MAX_TOOL_OUTPUT_CHARS`, counts toward the per-iteration tool-call cap, and is limited to `MAX_RESULT_RETRIEVALS_PER_RUN` per run so it cannot become a loop. A retrieved result counts as recent and stays in full until it ages out like any other.
6. **Safety.** Stored results were already filtered when produced, so retrieval cannot expose denylisted files or secrets.
7. **Config only.** `HISTORY_KEEP_RECENT_RESULTS` and `MAX_RESULT_RETRIEVALS_PER_RUN` are configuration values; the coding agent must not hard-code numbers taken from this document.

---

## 6. Stop Conditions

All are evaluated by the controller. Each maps to a `StopReason`.

| StopReason | Trigger |
|---|---|
| `SUCCESS` | Controller's full-suite run passes after at least one patch |
| `NO_FAILURES` | Baseline already passes |
| `MAX_ITERATIONS` | Iteration count reached the limit |
| `NO_PROGRESS` | After two consecutive patches, the set of failing test ids and the main error message are unchanged |
| `REGRESSION` | After a patch, a test that passed at baseline now fails. The controller reverts that patch (restores the file) and tells the LLM; a second regression stops the run |
| `LLM_UNAVAILABLE` | LLM call still failing after `LLM_MAX_RETRIES` (including persistent 429) |
| `BAD_MODEL_OUTPUT` | Repeated malformed tool calls beyond a configured count |
| `PROTECTED_PATH_NEEDED` | The model keeps trying to edit a protected file; the run stops and reports it for human review |
| `INTERNAL_ERROR` | Unexpected failure in CodePilot itself |

On any stop the report states: what failed, why, what was attempted, what remains unresolved.

---

## 7. Workspace and Git Model

**Why a temp copy:** the agent creates branches and commits. Doing that inside the CodePilot repo (or the sandbox folder) would pollute your own history and risk touching real work. The sandbox source stays pristine and can be reused for every demo run.

| Step | Detail |
|---|---|
| Copy | `shutil.copytree` from the source repo, ignoring `.git`, `__pycache__`, `.pytest_cache`, `.venv`, `runs` |
| Init | `git init` in the workspace; set **local** `user.name` and `user.email` (never global) so commits work without your identity |
| Baseline | One commit containing the copied files, on the default branch |
| Fix branch | `codepilot/fix-<run_id>` |
| Commit | Only after the controller's final run passes. Stage tracked, modified files only |
| Export | `git diff <baseline>..HEAD` saved as `runs/<run_id>/fix.patch` so the fix survives workspace deletion |
| Keep | `--keep-workspace` leaves the folder and prints its path |

Rules (match `AGENTS.md` section A6): no push, no force, no reset of the user's work, no rebase. In the MVP the agent can **modify existing files only**; it cannot create or delete files.

---

## 8. Core Data Models

Plain `dataclasses` (no pydantic for the MVP). Field lists are the contract; the coding agent may add private helpers.

```text
Settings
    provider: base_url, api_key (hidden in repr), model
    limits: max_iterations, max_tool_calls_per_iteration, command_timeout_s,
            max_tool_output_chars, max_patch_attempts_per_file, llm_max_retries,
            history_keep_recent_results, max_result_retrievals_per_run
    paths: source_repo, runs_dir, log_path (relative), metrics_path (relative)
    flags: keep_workspace

ToolResult
    ok: bool
    data: dict
    error: str | None
    truncated: bool

TestRunResult
    exit_code: int
    passed: int
    failed: int
    errors: int
    failing_ids: list[str]
    duration_s: float
    timed_out: bool
    output_excerpt: str          (capped)

ToolRecord
    call_id: str
    iteration: int
    tool: str
    args: dict
    ok: bool
    summary: dict                 (structured summary, section 5.3)
    result_path: str              (file under runs/<run_id>/results/)
    retrieved_count: int

PatchRecord
    iteration: int
    path: str
    diff: str
    applied: bool
    reverted: bool
    tests_after: TestRunResult | None

RunState
    run_id, task, workspace_path, branch
    iteration: int
    baseline: TestRunResult
    latest_tests: TestRunResult | None
    files_inspected: list[str]
    tool_records: list[ToolRecord]   (what the trace stores)
    patches: list[PatchRecord]
    status: running | finished
    stop_reason: StopReason | None
    notes: list[str]             (observed facts / inferences / hypotheses, labelled)

StopReason (enum)               see section 6
```

---

## 9. LLM Client

```text
class LLMClient (interface):
    chat(messages, tools) -> LLMResponse

LLMResponse:
    text: str | None
    tool_calls: list[ToolCall]       (id, name, arguments as parsed dict)
    usage: token counts if provided
    raw_finish_reason: str
```

The Groq implementation:

- uses the `openai` SDK with `base_url`, `api_key`, `model` from `Settings`
- retries on HTTP 429, timeouts and 5xx with exponential backoff, honouring any `retry-after` header
- raises `LLMUnavailableError` after `llm_max_retries`
- if tool-call arguments are not valid JSON, returns them flagged as malformed so the controller can send an error message back instead of crashing
- never logs the API key or full request headers

The coding agent must verify the chosen Groq model supports tool calling before relying on it, and must read the model name from config.

---

## 10. Tool Registry

- `schemas.py` defines one JSON schema per tool; `tools.py` defines one function per tool; `registry.py` binds them.
- `registry.execute(name, args)`:
  1. unknown tool name -> `ToolResult(ok=False, error="unknown tool")`
  2. arguments fail schema validation -> `ToolResult(ok=False, error=<what is wrong>)`
  3. run the function inside try/except; known errors become `ToolResult`, unknown ones become `INTERNAL_ERROR` entries in the trace
  4. truncate output to the cap and set `truncated`
- Tools receive the workspace root and settings through the registry; they do not read global state.

There are 9 tools, listed in `AGENTS.md` A3; `get_tool_result` is described in section 5.3. Exact tool inputs, outputs and checks are defined in `TOOLS.md`.

---

## 11. Safety Layer

Implements `AGENTS.md` section A4 in one module so the rules live in one place.

**Path check (every file tool calls it first):**

1. Join the requested path to the workspace root.
2. Resolve it fully (follow symlinks and junctions).
3. Reject if the resolved path is not inside the workspace root. Compare with Windows case-insensitivity in mind (normalise case and separators before comparing).
4. For writes, also reject if the path matches the protected list.
5. For reads, also reject if the path matches the denylist.

**Subprocess environment:** pytest and Git run with a **sanitised environment** that removes `GROQ_API_KEY`, `LLM_API_KEY` and any variable whose name contains `KEY`, `TOKEN`, `SECRET` or `PASSWORD`. The code under test is untrusted and must not be able to read the key.

**Subprocess rules:** argument lists only, never `shell=True`; working directory is the workspace; timeout always set; Python is invoked as `sys.executable -m pytest`, not a bare `pytest` command.

---

## 12. Test Runner

- Command: `sys.executable -m pytest -q -p no:cacheprovider --junitxml=<runs/run_id/junit.xml>` plus an optional target.
- Environment adds `PYTHONDONTWRITEBYTECODE=1` so the workspace stays clean and the diff contains only real changes.
- Results are parsed from the JUnit XML (built into pytest, no extra dependency), falling back to the summary line if the XML is missing.
- On timeout: kill the child process, set `timed_out=True`, return what was captured.
- The JUnit file is written **outside** the workspace.

---

## 13. Outputs

All written to `runs/<run_id>/`:

| File | Content |
|---|---|
| `run_trace.json` | One entry per tool call: the structured summary and a pointer to the full result (format in `AGENTS.md` A7) plus LLM turns summarised without secrets |
| `results/<call_id>.json` | Full stored result of each tool call (source for retrieval) |
| `report.md` | Final report (sections listed in `AGENTS.md` A7) |
| `fix.patch` | Diff of the fix (only when a commit was made) |
| `final_tests.txt` | Output of the controller's final test run |
| `junit.xml` | Last test run's raw result |

---

## 14. Windows Requirements

Because development and demos happen on Windows, the coding agent must handle these explicitly:

| Concern | Requirement |
|---|---|
| Search tool | No `grep`/`ripgrep` dependency. `search_code` is implemented in pure Python (walk files, regex) |
| Paths | Use `pathlib`; never build paths with string concatenation or hard-coded `/` or `\`; tool paths shown to the LLM use forward slashes |
| Line endings | Open files with explicit `encoding="utf-8"` and `newline=""`. `apply_patch` must detect the file's line ending (CRLF or LF) and adapt `old` and `new` so a match does not fail only because of `\r\n` |
| Temp cleanup | `shutil.rmtree` can fail on read-only files inside `.git`; use an error handler that clears the read-only flag and retries |
| Console encoding | Reconfigure stdout/stderr to UTF-8 so printing code or logs does not crash |
| Process kill on timeout | Make sure the pytest child is terminated, not left running |
| Git | Verify `git` is on PATH at startup and print a clear message if not |
| Python launcher | Use `sys.executable`, not `python` or `py` |

---

## 15. Decisions and Items for Your Confirmation

**Confirmed by you**

| # | Decision |
|---|---|
| C1 | The agent runs against a temp copy of the sandbox, `git init` there |
| C2 | Development and demo on Windows |
| C3 | Dependencies in `requirements.txt` |
| C4 | LLM: Groq free tier via OpenAI-compatible API |
| C5 | Agent never edits tests; push/PR are later opt-ins; second harder repo only after the MVP |
| C6 | The controller runs the full test suite automatically after every successful patch (the LLM may still call `run_tests` for targeted runs) |
| C7 | The fix is exported as `fix.patch`; the temp workspace is deleted unless `--keep-workspace` is passed |
| C8 | The MVP agent can only modify existing files; it cannot create or delete files |
| C9 | Test results are parsed from pytest's built-in JUnit XML |
| C10 | `NO_PROGRESS` = same failing tests and same main error after 2 consecutive patches |
| C11 | Old tool results are compressed into structured summaries; full results are preserved in persistent run state; the agent can retrieve an original result when needed (adds the `get_tool_result` tool) |
| C12 | Full tool results are stored on disk as one JSON file per tool call under `runs/<run_id>/results/`, and also held in memory during the run |
| C13 | Starting config defaults: keep the 3 most recent results in full; allow 10 retrievals per run (both configurable) |

**Proposed by me, not yet confirmed:** none. Tool-level proposals are tracked in `TOOLS.md` section 12.

---

## 16. Out of Scope for This Document

- Exact tool schemas -> `TOOLS.md`
- The seeded bugs and their tests -> `SANDBOX_REPO.md`
- Prompt wording -> `PROMPTS.md`
- Test cases and fake-LLM scenarios -> `TESTING.md`
- Build order -> `TASKS.md`
