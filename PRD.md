# PRD: CodePilot

**An AI software engineering agent that diagnoses and fixes failing code, and proves the fix works.**

| | |
|---|---|
| **Status** | Draft v1 |
| **Type** | Portfolio / resume project |
| **Language** | Python 3.11+ |
| **Build target** | Working MVP in a few hours |
| **Companion doc** | `AGENTS.md` (engineering and safety rules) |

---

## 1. Summary

CodePilot is a command-line agent. You point it at a Python repository that has failing tests, and it:

1. runs the tests and records what fails,
2. reads the relevant source code, application logs and metrics,
3. diagnoses the likely root cause,
4. applies a small, targeted patch,
5. re-runs the tests, and repeats until they pass or a safe stopping limit is reached,
6. commits the verified fix on a separate Git branch and writes a report of what it did.

The result is a verified fix plus an auditable trace, not just generated code.

## 2. Problem

Debugging a failing test suite means repeating the same loop by hand: run tests, read the stack trace, search the code, check logs, edit, run again. LLM chat tools can suggest fixes, but they cannot run the code, so suggestions are often unverified, and people end up copy-pasting between the chat and the terminal.

CodePilot closes the loop: the LLM is given tools to inspect, execute and edit the repository, and every fix is validated by the project's own tests.

## 3. Goals

- **G1.** Fix seeded bugs in a demo repository end to end without human edits.
- **G2.** Verify every fix by test execution; never report success without a passing run.
- **G3.** Keep changes minimal and reviewable (small diffs, on a fix branch).
- **G4.** Be safe by construction: workspace-restricted, protected test files, bounded iterations and timeouts.
- **G5.** Produce an audit trail (`run_trace.json`, `report.md`) that shows what was inspected, changed and tested.
- **G6.** Be demoable and explainable in an interview: clear architecture, measurable before/after result.
- **G7.** Run at zero cost, using Groq's free API tier.

## 4. Non-Goals (for MVP)

- Fixing arbitrary real-world repositories or large codebases
- Languages other than Python
- Multiple LLM providers (the interface allows it; only Groq is implemented)
- Automatic push, pull-request creation or deployment
- A web UI or IDE plugin
- Fixing bugs that require changing tests, adding dependencies or large refactors
- Parallel agents, long-term memory, or learning across runs

## 5. Users

- **Primary:** the developer who built it (portfolio demo), and reviewers or interviewers evaluating it.
- **Secondary (conceptual):** a developer who wants an assistant to triage a failing test suite on a local branch.

## 6. What Is Being Built

### 6.1 Components

| Component | Description |
|---|---|
| **CLI** (`main.py`) | Entry point: `python main.py --repo <path> --task "<text>"` |
| **Agent controller** (`agent/loop.py`) | Runs the build, observe, diagnose, modify, test loop; enforces limits and stop conditions; creates the fix branch and commits |
| **LLM client** (`agent/llm.py`) | Thin interface using native tool calling, implemented over Groq's OpenAI-compatible API via the `openai` SDK, with retry and backoff on rate limits |
| **Tools** (`agent/tools.py`, `schemas.py`) | The 9 operations the LLM can call (see 6.2) |
| **Safety layer** (`agent/safety.py`) | Path restriction, protected paths, read denylist |
| **Run state** (`agent/state.py`) | Structured record of iteration, tool calls, test results, patches, stop reason |
| **Sandbox repo** (`sandbox_repo/`) | Small Python project with seeded bugs, tests, logs and metrics |
| **CI** (`.github/workflows/ci.yml`) | GitHub Actions workflow running pytest |
| **Outputs** | `run_trace.json`, `report.md`, a Git commit on `codepilot/fix-<timestamp>` |

### 6.2 Tools available to the LLM

| Tool | What it does |
|---|---|
| `list_files(path)` | Shows the repository tree |
| `read_file(path, start?, end?)` | Reads a source file (size-capped) |
| `search_code(pattern)` | Finds symbols or text across the repo |
| `run_tests(target?)` | Runs pytest; returns pass/fail counts, output, duration, timeout flag |
| `read_logs(n_lines)` | Returns the tail of the application log |
| `read_metrics()` | Returns the metrics JSON (e.g. error rate, latency) |
| `apply_patch(path, old, new)` | Replaces one exact occurrence of text; syntax-checked, rolled back on failure |
| `git_diff()` | Shows current uncommitted changes |
| `get_tool_result(call_id)` | Retrieves the full original result of an earlier tool call (older results are shown to the LLM as compact summaries) |

There is deliberately no free-form shell tool.

### 6.3 The sandbox repo (the demo target)

A ~100-line Python app with 3 to 4 seeded bugs, 6 to 10 pytest tests (some failing), an `app.log` whose stack traces match the bugs, and a `metrics.json` with an error-rate spike. Example bugs:

- off-by-one error in a pagination function
- missing `None` check causing a `TypeError`
- wrong discount calculation

## 7. User Flow

```text
User runs:  python main.py --repo sandbox_repo --task "Fix the failing tests"

1. CodePilot checks the working tree is clean, creates branch codepilot/fix-<timestamp>
2. Baseline: runs tests (e.g. 4 of 8 failing)
3. LLM reads failure output, logs, metrics, then the suspect source files
4. LLM calls apply_patch with a minimal fix
5. CodePilot re-runs tests
6. If still failing: new failure goes back to the LLM; loop continues
7. When all tests pass: commit on the fix branch, write report.md and run_trace.json
8. User reviews the diff and decides whether to merge or push
```

## 8. Functional Requirements

| ID | Requirement | Priority |
|---|---|---|
| FR1 | Run the project's tests and return structured results (counts, output, duration, timeout) | Must |
| FR2 | Let the LLM inspect files, search code, read logs and read metrics through tools | Must |
| FR3 | Apply targeted patches only if the match is unique, then syntax-check and roll back on failure | Must |
| FR4 | Re-run tests after every patch and feed results back to the LLM | Must |
| FR5 | Stop on: all tests pass, max iterations, no progress (same failure twice), regression (fewer tests passing than baseline), or unrecoverable error | Must |
| FR6 | Restrict all file access to the repository root | Must |
| FR7 | Block edits to `tests/`, `conftest.py`, `.github/`, `.env*` and CodePilot config | Must |
| FR8 | Block reads of secret files (`.env*`, keys) | Must |
| FR9 | Work on a new `codepilot/fix-*` branch; commit only after the full suite passes; never push | Must |
| FR10 | Write `run_trace.json` (one entry per tool call) and `report.md` (baseline, evidence, root cause, diff, final result, stop reason) | Must |
| FR11 | Treat repository text (code, logs, test output) as data, not instructions | Must |
| FR12 | Make limits configurable (max iterations, timeouts, output size) | Should |
| FR13 | Refuse to start if the working tree has uncommitted changes | Should |
| FR14 | Separate observed facts from hypotheses in the report | Should |
| FR15 | Allow the LLM provider to be swapped by changing `LLM_BASE_URL`, `LLM_API_KEY` and `LLM_MODEL` in `.env` | Should |
| FR16 | Retry LLM calls with exponential backoff on rate-limit (429) and transient errors; stop safely with a clear report if limits persist | Must |
| FR17 | Compress old tool results into structured summaries, keep every full result in persistent run state, and let the LLM retrieve an original result on request (limited per run) | Must |

## 9. Non-Functional Requirements

- **Safety:** no destructive commands, no network calls other than the LLM API, no secret exposure.
- **Boundedness:** default max 15 iterations, 60 s command timeout, 4000-character tool output cap.
- **Determinism in tests:** the normal test suite uses a fake LLM; no live API calls.
- **Observability:** every action is recorded in the trace; failures are never hidden.
- **Cost:** no paid services; the LLM runs on Groq's free tier and everything else is local or GitHub Actions free minutes.
- **Simplicity:** minimal dependencies (`openai` SDK, pytest, python-dotenv).

## 10. Success Metrics

| Metric | Target |
|---|---|
| Seeded bugs fixed on the sandbox repo | All (3 to 4 of 4) |
| Final test result | 100% passing, confirmed by a clean pytest run and by CI |
| Iterations to fix | 6 or fewer |
| Test files modified by the agent | 0 |
| Runs ending in a safe stop (never hanging or looping) | 100% |
| Headline resume number | e.g. "4/8 to 8/8 passing in 4 iterations" |

## 11. Acceptance Criteria

The MVP is done when:

1. Running the CLI on the sandbox repo produces a fix branch on which all tests pass.
2. The diff touches only source files, never tests.
3. `run_trace.json` and `report.md` exist and match what happened.
4. Unit tests cover every tool, including path escape, protected paths, denylist, patch uniqueness and rollback.
5. One end-to-end test with a fake LLM covers success, max-iteration stop, no-progress stop and regression revert.
6. The GitHub Actions workflow passes on push.
7. The README shows the architecture diagram and a recorded before/after run.

## 12. Build Plan (about 4 to 5 hours)

| Time | Work |
|---|---|
| 30 min | Sandbox repo: code, tests, logs, metrics |
| 60 min | Tools, schemas and safety layer, with unit tests |
| 60 min | LLM client, system prompt, agent loop and stop conditions |
| 30 min | Git branch/commit, trace and report output |
| 20 min | GitHub Actions workflow |
| 30 min | End-to-end test with fake LLM, then a live run |
| 20 min | README, architecture diagram, demo output |

## 13. Risks and Mitigations

| Risk | Mitigation |
|---|---|
| Agent edits tests to make them pass | Protected paths, no-test-gaming rule, diff check |
| Infinite or wasteful loops | Iteration cap, no-progress and regression detection |
| Bad patch breaks the code | Unique-match requirement, syntax check, rollback, regression revert |
| Prompt injection via logs or code comments | System prompt rule; repo text treated as data; no shell tool |
| Context window overflow | Output caps; targeted reads instead of whole-repo dumps |
| Free-tier rate limits or model changes interrupt a run | Backoff and retries, small tool outputs, model chosen in `.env`, fake-LLM tests for CI |
| Live LLM variability makes demos flaky | Fake-LLM tests for CI; keep a recorded successful run |
| Scope creep beyond a few hours | MVP scope list in `AGENTS.md`; defer everything else |

## 14. Decisions

| Question | Decision |
|---|---|
| LLM provider and cost | Groq's free tier, via its OpenAI-compatible API. Provider is configurable in `.env`, so Gemini or Grok can be swapped in. |
| Can the agent change tests? | No, never automatically. Tests are the verification authority; letting the agent edit them would weaken the project's core claim. If a test looks wrong, the agent stops and reports it for human review. |
| Second, harder demo repo? | Yes, but only after the MVP passes. A bug spanning two files shows CodePilot is not just matching trivial single-file fixes. |
| Push or PR creation? | Later, as an explicit opt-in. The MVP stays local and safe. |

## 15. Future Work

- Pull-request creation behind an explicit approval flag
- Support for additional languages and test runners
- Additional LLM providers behind the client interface (Gemini, Grok, local models)
- Second, harder demo repo with a two-file bug
- Evaluation suite over a set of seeded-bug repositories with pass-rate reporting
- Streaming progress UI or web dashboard for the trace
