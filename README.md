# CodePilot — AI Software Engineering Agent

> **LLM proposes → controlled tools execute → tests provide evidence → controller decides → verified Git commit/report**

CodePilot is an AI-assisted software engineering agent that investigates failing tests, gathers repository evidence, proposes targeted source-code fixes, verifies those fixes with automated tests, and produces an auditable Git patch and execution report.

The key design principle is that the LLM is **not** given unrestricted control of the repository. It is one reasoning component inside a controlled software-engineering workflow.

---

## Status

CodePilot is a working MVP: every component is implemented and tested, and it has repaired the bundled sandbox with a real LLM.

| Area | State |
|---|---|
| Implementation | Complete: workspace and Git isolation, nine controlled tools, evidence-driven controller, verification, audit, commit, trace, report, CLI |
| Offline tests | 849 tests (846 run by default; 3 live tests need a Groq key and run only with `-m live`). No normal test calls an LLM. |
| CI | GitHub Actions on Ubuntu and Windows, Python 3.11 ([workflow](.github/workflows/ci.yml)) |
| Live evaluation | 5 recorded runs on `openai/gpt-oss-120b` (Groq free tier): 1 verified success. Details below and in [`docs/demo/runs.md`](docs/demo/runs.md). |

### Live evaluation (honest summary)

Every live run is recorded, including the failures. Procedure: [`docs/live_evaluation.md`](docs/live_evaluation.md).

| Run | Prompt | Iteration limit | Result | Iterations | Tokens | Time |
|---|---|---|---|---|---|---|
| `20261015-170826` | v1 | 6 | `MAX_ITERATIONS`, 4/8 to 4/8 passing | 6 | 16,258 | 66 s |
| `20261016-074108` | v2 | 6 | `MAX_ITERATIONS`, 4/8 to 4/8 passing | 6 | 17,288 | 75 s |
| `20261016-075658` | v2 | 14 | **Verified success**, 4/8 to 8/8 passing, 4 patches | 11 | 42,750 | 203 s |
| `20261017-124438` | v2 | 14 | `MAX_ITERATIONS`, 4/8 to 7/8 passing, 3 patches | 14 | 56,545 | 343 s |
| `20261018-111755` | v2 | 15 | `MAX_ITERATIONS`, 4/8 to 4/8 passing, no patch | 15 | 55,179 | 324 s |

What this does and does not show:

- The model returns **one tool call per reply** (checked with a live probe), so a repair needs roughly one iteration per file read and per patch. The first two runs ran out of the old 6-iteration budget before applying any patch. The default is now 15 ([ADR-027](ADR.md)).
- The fourth run applied three of the four fixes and stopped one fix short at 7/8. About four of its 14 iterations went on re-reading files and on one patch whose `old` text did not match, so 15 iterations is roughly the minimum observed need, not generous headroom.
- The fifth run used the documented defaults (limit 15, recent window of 3) and applied no patch in 15 iterations while the same source and test files were read repeatedly. A shortened `read_file` result contains no code, so a one-call-per-reply model loses file contents after three more results. The history now keeps the latest read of each file (ADR-010 amendment). An offline simulation shows the repair is no longer impossible within 15 iterations; **no live run has yet tested this change**.
- One of five runs reached SUCCESS, but this sample is too small to establish a reliable success rate. The first two runs were limited by the iteration budget, not by a wrong fix. More runs are needed before drawing conclusions.
- Free-tier limits (about 8,000 tokens per minute) make a run take minutes, and only a few full runs fit in a day.
- The deterministic FakeLLM tests prove the controller, not the model. The live runs are the evidence about the model.

---

## 1. Problem

An AI coding assistant can generate a plausible fix, but generating code is not the same as proving that the software is repaired.

A useful software-engineering agent needs to answer:

- What is failing?
- Where is the failure?
- What evidence supports the diagnosis?
- What source change should be made?
- Did the change actually fix the problem?
- Did it introduce a regression?
- Did the agent modify anything it should not have touched?
- Can another engineer independently verify the fix?

CodePilot addresses these questions through an evidence-driven repair loop.

---

## 2. How CodePilot Works

```text
                         ┌──────────────────┐
                         │       CLI        │
                         └────────┬─────────┘
                                  │
                                  ▼
                         ┌──────────────────┐
                         │    Controller    │
                         │ State + Safety   │
                         └───────┬──────────┘
                                 │
                    ┌────────────┴────────────┐
                    │                         │
                    ▼                         ▼
             ┌─────────────┐           ┌─────────────┐
             │  LLMClient  │           │  Run State  │
             │ Groq/FakeLLM│           │ + History   │
             └──────┬──────┘           └──────┬──────┘
                    │                         │
                    └────────────┬────────────┘
                                 ▼
                       ┌───────────────────┐
                       │   Tool Registry   │
                       └─────────┬─────────┘
                                 │
          ┌──────────────────────┼──────────────────────┐
          │                      │                      │
          ▼                      ▼                      ▼
     Repository              Tests / Logs              Git
       Tools                   Metrics                 Diff
          │                      │                      │
          └──────────────────────┼──────────────────────┘
                                 ▼
                       ┌───────────────────┐
                       │ Verification +    │
                       │ Regression Check  │
                       └─────────┬─────────┘
                                 ▼
                       ┌───────────────────┐
                       │ Trace / Report    │
                       │ + fix.patch       │
                       └───────────────────┘
```

The approved high-level flow (the diagram above adds the verification step and the artifacts):

```text
                    ┌──────────────┐
                    │     CLI      │
                    └──────┬───────┘
                           │
                    ┌──────▼───────┐
                    │  Controller  │
                    └──┬────────┬──┘
                       │        │
              ┌────────▼──┐  ┌─▼──────────┐
              │ LLMClient │  │ Run State  │
              └─────┬─────┘  └────┬───────┘
                    │             │
                    └──────┬──────┘
                           │
                    ┌──────▼───────┐
                    │ Tool Registry│
                    └──────┬───────┘
                           │
        ┌──────────────────┼──────────────────┐
        │                  │                  │
   Repository           Tests/Logs          Git
      Tools              Metrics            Diff
        │                  │                  │
        └──────────────────┼──────────────────┘
                           │
                    ┌──────▼───────┐
                    │ Trace/Report │
                    └──────────────┘
```

The repair loop is:

```text
BASELINE
   ↓
OBSERVE
   ↓
DIAGNOSE
   ↓
PATCH
   ↓
VERIFY
   ↓
DECIDE
   ├── SUCCESS
   ├── STOP
   └── NEXT ITERATION
```

### Baseline

CodePilot first executes the full test suite and records the initial state.

If the repository already passes, it does not make unnecessary changes.

### Observe

The agent gathers evidence through controlled tools:

- source files
- code search
- tests
- logs
- metrics
- Git diff
- previous tool results

### Diagnose

The LLM analyzes the evidence and proposes a diagnosis.

The system distinguishes between:

- observed fact
- inference
- hypothesis
- verified result

### Patch

The LLM proposes a targeted source-code patch.

The controller validates the patch before allowing it to be applied.

### Verify

The patch is tested.

A targeted test passing is **not** sufficient for final success.

### Decide

The controller determines whether the agent should:

- continue
- retry
- stop
- report success

The LLM cannot independently declare the run successful.

---

## 3. Controlled Tool Surface

The MVP exposes exactly nine tools:

| Tool | Purpose |
|---|---|
| `list_files` | Inspect repository structure |
| `read_file` | Read source/evidence files |
| `search_code` | Search source code |
| `run_tests` | Execute the approved pytest workflow |
| `read_logs` | Inspect application logs |
| `read_metrics` | Inspect JSON metrics |
| `apply_patch` | Apply controlled source changes |
| `git_diff` | Inspect repository modifications |
| `get_tool_result` | Retrieve older tool results |

There is intentionally **no generic shell or `run_command` tool**.

This keeps the agent's execution surface limited to capabilities required by the MVP.

---

## 4. Safety Model

CodePilot follows a least-privilege model.

### Protected paths

The agent cannot modify protected files/directories such as:

```text
tests/        test_*.py        *_test.py        conftest.py
.github/      .gitignore       codepilot/
.env          .env.*
pytest.ini    tox.ini          setup.cfg        pyproject.toml
requirements*.txt
```

The agent also cannot **read** secret files: `.env`, `.env.*`, `*.pem`, `*.key`, `id_rsa*` and `.git/`. These files are not copied into the temporary workspace, and the read tools would refuse or hide them anyway (`read_file` is refused; `list_files` and `search_code` leave them out).

### Allowed edits

The MVP permits modification of:

```text
existing .py source files
```

The agent cannot:

- modify tests
- create new files
- modify dependency files
- modify protected configuration
- modify GitHub workflows
- execute arbitrary shell commands

Repository contents, logs, metrics, test output, and historical tool results are treated as **untrusted data**, not as instructions to the agent.

---

## 5. Verification Authority

Tests are the verification authority.

The model cannot simply say:

> "The fix worked."

and have CodePilot accept that as proof.

Final success requires:

1. A source patch was applied.
2. The full test suite passes.
3. The controller performs final verification.
4. The final changed-file audit passes.
5. The verified result is committed.

### Regression detection

Regression detection compares **test identities**, not only the number of failures.

For example:

```text
Before:
FAIL test_orders.py::test_discount
FAIL test_pricing.py::test_total

After:
FAIL test_orders.py::test_discount
FAIL test_pagination.py::test_boundary
```

The failure count is still two, but the failing test set changed. CodePilot treats the newly failing test as a regression.

---

## 6. Iteration and Stopping

The MVP enforces:

```text
Maximum iterations = 15   (default; set MAX_ITERATIONS to change it, see ADR-027)
```

An iteration is one LLM reply plus the execution of its tool calls. A model that returns one tool call per reply (observed with `openai/gpt-oss-120b` on Groq) needed 11 iterations for the sandbox; a model that batches tool calls needs far fewer.

CodePilot also detects no progress.

If two consecutive patches produce:

```text
same failing-test set
+
same main error
```

the controller stops with:

```text
NO_PROGRESS
```

Other controlled stopping conditions include:

- baseline already passing
- maximum iterations
- regression/repeated regression
- tool-call limit
- invalid model behavior
- protected-path violation
- LLM failure
- unexpected internal failure

This prevents an agent from running indefinitely or repeatedly making ineffective changes.

---

## 7. Context Management

Tool output can become large over multiple iterations.

CodePilot therefore uses a hybrid history strategy:

```text
Most recent 3 tool results (default, HISTORY_KEEP_RECENT_RESULTS)
+ the latest read of each file (up to 8 files / 12,000 characters)
        ↓
      full

Older results
        ↓
structured summaries
        ↓
complete result persisted in runs/<run_id>/results/
        ↓
get_tool_result(call_id)
```

The complete result of every tool call remains persisted.

Historical retrieval is limited to:

```text
10 retrievals per run
```

This balances context size with evidence retention.

---

## 8. LLM Architecture

The controller communicates through a provider-independent `LLMClient`.

The current live provider is **Groq**, with the model configured through `GROQ_MODEL` (or `LLM_MODEL`); the default is `openai/gpt-oss-120b`.

Provider-specific implementation details remain isolated from the controller.

### FakeLLM

Automated tests use a deterministic `FakeLLM`.

The normal test suite does not require live Groq requests.

FakeLLM supports scenarios such as:

- successful repair
- multiple patches
- maximum iterations
- no progress
- regression
- repeated regression
- malformed tool calls
- unknown tools
- protected-path attacks
- false-success model behavior

This makes controller behavior reproducible.

---

## 9. Workspace Isolation

CodePilot never performs repairs directly against the canonical sandbox.

Instead:

```text
Canonical sandbox
       │
       ▼
Temporary workspace
       │
       ▼
Git repair branch
       │
       ▼
Agent execution
       │
       ▼
Verified patch + commit
```

The canonical repository remains unchanged.

The temporary workspace is deleted after execution by default.

For debugging, it can be preserved using:

```bash
--keep-workspace
```

---

## 10. Git and Auditability

Git provides the audit trail for a repair.

A successful run produces:

- repair branch
- Git diff
- verified commit
- `fix.patch`
- execution trace
- final report

The MVP does **not** automatically:

- push to a remote
- create a pull request
- force-push
- perform destructive Git recovery

Repository delivery remains human-controlled.

The repair branch and the commit live in the temporary workspace and are deleted with it unless `--keep-workspace` is used. What always survives is the run folder `runs/<run_id>/`: the trace, the report and, for a verified fix, `fix.patch` (see section 14).

---

## 11. Sandbox Repository

The deterministic demonstration repository is:

```text
sandbox_repo/
├── app/
│   ├── __init__.py
│   ├── pagination.py
│   ├── orders.py
│   └── pricing.py
├── tests/
│   ├── test_pagination.py
│   ├── test_orders.py
│   └── test_pricing.py
├── logs/
│   └── app.log
└── metrics.json
```

The sandbox contains four deliberate defect categories:

1. Pagination off-by-one
2. `None` handling
3. Discount calculation
4. Cross-evidence diagnostic defect

The sandbox starts with **8 tests: 4 passing, 4 failing**. Each defect has exactly one failing test, and the pagination tests include two that pass on both the buggy and the fixed code.

| Defect | Failing test | Evidence needed |
|---|---|---|
| BUG-01 pagination off-by-one (`app/pagination.py`) | `test_pagination.py::test_second_page` | test output |
| BUG-02 missing `None` handling (`app/orders.py`) | `test_orders.py::test_missing_order` | test output and log traceback |
| BUG-03 discount calculation (`app/pricing.py`) | `test_pricing.py::test_percentage_discount` | test output and metrics |
| BUG-04 case-sensitive tax region (`app/orders.py`) | `test_orders.py::test_order_total` | log warnings and metrics |

The machine-readable truth (test IDs, reference fixes, regression and no-op patches) is `tests/fixtures/sandbox_expected.json`; the design is in `SANDBOX_REPO.md`. Integrity tests prove that each reference fix repairs only its own defect, that all four give 8/8, and that a CodePilot run never changes the canonical sandbox.

The sandbox is intentionally small and deterministic so the complete repair loop can be demonstrated and tested repeatedly.

---

## 12. Testing Strategy

CodePilot uses multiple levels of testing.

### Unit tests

Components are tested independently:

- configuration
- state
- errors
- tool registry
- all tools
- history
- workspace
- Git
- LLM client
- prompts
- controller
- reporting

### Offline E2E tests

The FakeLLM is used to test complete agent runs:

- successful repair
- baseline already passing
- maximum iterations
- no progress
- regression recovery
- repeated regression
- equal-count regression
- model false success
- targeted-pass-not-success
- malformed tool calls
- unknown tools
- LLM unavailable/rate limit
- protected-path attack
- output audit
- cleanup
- source isolation
- CLI smoke tests

### Adversarial tests

The system is tested against:

- prompt injection
- path traversal
- option injection
- secret exfiltration
- output flooding
- forbidden Git operations
- test gaming
- new-file creation
- retrieval-ID attacks

### Independent patch verification

After a successful run, `fix.patch` is applied to a clean sandbox copy and the full test suite is run independently.

This verifies the delivered patch rather than trusting CodePilot's internal state.

---

## 13. CI

CI is implemented in `.github/workflows/ci.yml`:

```text
Python 3.11
Ubuntu + Windows (matrix, fail-fast off)
python -m pytest -ra --junitxml=test-results/junit.xml
```

The suite is offline: no Groq key is set in CI, and live tests are never collected there. The JUnit XML is uploaded as an artifact on every run, including failures.

---

## 14. Generated Artifacts

Each run writes a folder under `runs/` (outside the workspace, ignored by Git):

```text
runs/<run_id>/
├── run_trace.json    run metadata, baseline, one entry per tool call, model turns, patches, stop reason, cleanup status
├── report.md         honest outcome, observed facts vs the model's unverified explanation, patches, verification
├── final_tests.txt   the controller's latest full-suite result
├── fix.patch         the verified diff (only for a verified fix)
├── junit.xml         pytest JUnit evidence
└── results/          every full tool result (c001.json, c002.json, ...)
```

The temporary workspace is deleted after the run unless `--keep-workspace` is used. Secrets and secret-shaped tokens are redacted from the trace and report.

These artifacts provide an auditable record of:

- what the agent observed
- what it changed
- what tests were run
- why it stopped
- whether the repair was verified


---

## 15. Installation

Requirements: Python 3.11+, Git.

```powershell
git clone https://github.com/Likhitareddy17/codepilot
cd codepilot
python -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt      # Linux/macOS: .venv/bin/python
```

Optional: `.venv\Scripts\python -m pip install -e .` adds a `codepilot` command; `python main.py` works without it.

### Configuration

Create a `.env` file in the repository root (it is ignored by Git and protected from the agent):

```text
GROQ_API_KEY=<your-key>
GROQ_MODEL=openai/gpt-oss-120b      # optional, this is the default
```

**Never commit API keys.** All limits are environment variables with defaults in `codepilot/config.py`; the full table is in `AGENTS.md` (A2). The ones you are most likely to change:

| Variable | Default | Meaning |
|---|---|---|
| `MAX_ITERATIONS` | 15 | LLM replies allowed per run |
| `COMMAND_TIMEOUT_SECONDS` | 60 | timeout for each pytest or Git command |
| `HISTORY_KEEP_RECENT_RESULTS` | 3 | tool results kept in full in the model's context |
| `HISTORY_KEEP_FILE_READS` | 8 | files whose latest read stays in full (0 turns this off) |
| `HISTORY_FILE_READS_MAX_CHARS` | 12000 | size cap for those retained reads |
| `MAX_TOOL_CALLS_PER_ITERATION` | 10 | tool calls accepted from one reply |
| `RUNS_DIR` | `runs` | where run folders are written |
| `KEEP_WORKSPACE` | false | keep the temporary workspace |

---

## 16. Running Tests

```powershell
.venv\Scripts\python -m pytest -q                  # the full offline suite
.venv\Scripts\python -m pytest -q -m "not slow"    # fast loop: no Git or pytest subprocesses
.venv\Scripts\python -m pytest -m live -s          # live Groq tests (needs GROQ_API_KEY; never run in CI)
```

The offline suite is deterministic and never calls an LLM. It ignores your `.env` and shell settings, and it does not collect `sandbox_repo/` (that suite fails on purpose; run it with `cd sandbox_repo` then `python -m pytest`).

---

## 17. Running CodePilot

```powershell
.venv\Scripts\python main.py --repo sandbox_repo --task "Fix the failing tests"
```

| Option | Meaning |
|---|---|
| `--repo PATH` | repository to repair (default `.`); it is copied, never modified |
| `--task TEXT` | task description (default "Fix the failing tests") |
| `--max-iterations N` | override `MAX_ITERATIONS` |
| `--keep-workspace` | keep the temporary Git workspace for debugging |

Startup problems (missing repository, no Git, no API key, invalid settings) exit with code 2 and a clear message, before any workspace is created.

### Example run

A real run on the sandbox (run `20261016-075658`, `openai/gpt-oss-120b`, `MAX_ITERATIONS=14`):

```text
Run 20261016-075658
Result: SUCCESS (fix verified by the full test suite, audited and committed)
Stop reason: SUCCESS: The full test suite passes and the fix was committed (f0c85409).
Tests: 4/8 -> 8/8 passing in 11 iteration(s)
Commit: f0c85409 on codepilot/fix-20261016-075658
Outputs: runs\20261016-075658
Fix: <repo>\runs\20261016-075658\fix.patch
```

To check the fix independently, apply only `fix.patch` to a clean copy of the sandbox and run plain pytest:

```powershell
$patch = (Resolve-Path runs\<run_id>\fix.patch).Path
Copy-Item sandbox_repo $env:TEMP\sb_verify -Recurse
cd $env:TEMP\sb_verify; git apply $patch; <path-to-venv>\Scripts\python -m pytest -q
```

Expected: `8 passed`.

---

## 18. Exit Codes

CodePilot uses:

```text
0 → success / no repair required
1 → controlled stop or unsuccessful run
2 → startup/configuration error
```

This allows the CLI to integrate with scripts and CI systems.

---

## 19. Design Principles

### Evidence before action

The agent should inspect relevant evidence before proposing a fix.

### Small, targeted changes

The agent should make focused changes rather than rewriting unrelated code.

### Tests over model confidence

Model confidence is not proof.

### Controller over autonomy

The controller owns safety and stopping decisions.

### Least privilege

The agent receives only the tools required for the MVP.

### Auditability

Important actions must be reconstructable from the trace and Git artifacts.

### Deterministic testing

Automated tests must not depend on a live LLM provider.

### Human-controlled delivery

The MVP does not automatically push code or create pull requests.

---

## 20. Known MVP Limitations

CodePilot deliberately does not attempt to solve every software-engineering-agent problem.

Current limitations include:

- Python-focused MVP
- existing `.py` files only
- no new-file creation
- no autonomous test repair
- no arbitrary shell execution
- no automatic dependency installation
- no automatic PR creation
- no automatic remote push
- JSON-only metrics support
- bounded context retrieval
- imperfect semantic test-gaming detection
- live LLM behavior can differ from deterministic FakeLLM behavior
- the live success rate is not established: 1 verified success in 5 recorded runs, and the file-read retention change has not yet been tested live (see Status)
- models that return one tool call per reply need many iterations (about 11 for the sandbox); the free-tier token limit makes such runs take minutes
- secret-shaped text inside files the agent may read (source, logs) is not redacted in tool output; only secret files and the process environment are blocked, and the trace and report are redacted
- `PROTECTED_PATH_NEEDED` (the agent asking for an edit it is not allowed to make) is not produced yet; protected edits are refused and counted instead
- the request size estimate (`MAX_REQUEST_TOKENS_ESTIMATE`) is not enforced; measured requests on the sandbox were about 2,000 to 4,000 tokens

These are explicit scope boundaries.

---

## 21. Future Extensions

Potential future work includes:

- additional programming languages
- richer repository indexing
- static-analysis tools
- browser/UI for run visualization
- automatic PR generation
- human approval checkpoints
- remote repository integration
- richer observability
- improved test-gaming detection
- multiple LLM/provider support
- more advanced planning and retrieval

These are intentionally outside the MVP.

---

## 22. Project Structure

The high-level structure is:

```text
CodePilot/
├── codepilot/
│   ├── config.py
│   ├── errors.py
│   ├── models.py
│   ├── safety.py
│   ├── state.py
│   ├── workspace.py
│   ├── controller.py
│   ├── prompts.py
│   ├── trace.py
│   ├── report.py
│   ├── cli.py
│   │
│   ├── tools/
│   │   ├── registry.py
│   │   ├── filesystem.py
│   │   ├── tests.py
│   │   ├── logs.py
│   │   ├── metrics.py
│   │   ├── patch.py
│   │   ├── git.py
│   │   └── history.py
│   │
│   └── llm/
│       ├── client.py
│       └── fake.py
│
├── main.py             command line entry point (also installed as `codepilot` with `pip install -e .`)
├── requirements.txt
├── pyproject.toml
├── tests/              unit, offline E2E, adversarial and sandbox tests; tests/live/ for live Groq tests
├── sandbox_repo/       the deterministic demonstration repository
├── docs/               live evaluation procedure and record
├── .github/workflows/  CI
│
├── AGENTS.md
├── gemini.md
├── PRD.md
├── ARCHITECTURE.md
├── TOOLS.md
├── PROMPTS_CodePilot.md
├── SANDBOX_REPO.md
├── TESTING.md
├── TASKS_CodePilot.md
├── ADR.md
└── README.md
```

---

## 23. Project Documentation

The project is organized around these documents:

| Document | Purpose |
|---|---|
| `AGENTS.md` | Agent rules, safety boundaries, and operating policy |
| `gemini.md` | Project instructions for the optional Gemini CLI development workflow |
| `PRD.md` | Product requirements and MVP scope |
| `ARCHITECTURE.md` | System architecture and control flow |
| `TOOLS.md` | Tool contracts and tool safety |
| `SANDBOX_REPO.md` | Deterministic sandbox and seeded defects |
| `PROMPTS_CodePilot.md` | LLM prompts, reasoning policy and prompt version history |
| `TESTING.md` | Test strategy and verification specification |
| `TASKS_CodePilot.md` | Dependency-ordered implementation plan and Definition of Done |
| `docs/live_evaluation.md`, `docs/demo/runs.md` | Live evaluation procedure and the record of every live run |
| `ADR.md` | Important architectural decisions and rationale |
| `README.md` | Project-facing documentation |

---

## 24. Final Mental Model

CodePilot should not be thought of as:

> **"ChatGPT that edits code."**

It should be thought of as:

> **"A controlled software-engineering workflow in which an LLM is one reasoning component."**

The architecture deliberately separates:

```text
Reasoning       → LLM
Execution       → Controlled tools
Safety          → Controller
Verification    → Tests
Auditability    → Git + Trace
Communication   → Report
```

That separation is the core engineering idea behind CodePilot.
