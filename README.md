# CodePilot — AI Software Engineering Agent

> **LLM proposes → controlled tools execute → tests provide evidence → controller decides → verified Git commit/report**

CodePilot is an AI-assisted software engineering agent that investigates failing tests, gathers repository evidence, proposes targeted source-code fixes, verifies those fixes with automated tests, and produces an auditable Git patch and execution report.

The key design principle is that the LLM is **not** given unrestricted control of the repository. It is one reasoning component inside a controlled software-engineering workflow.

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
tests/
conftest.py
.github/
.env
.env.*
pytest.ini
pyproject.toml
requirements*.txt
.gitignore
```

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
Maximum iterations = 6
```

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
Most recent 5 tool results
        ↓
      full

Older results
        ↓
structured summaries
        ↓
complete result retained in run state/trace
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

The current live provider is **Groq**, with the model configured through:

```text
LLM_MODEL
```

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

The exact expected values and test IDs are defined by `SANDBOX_REPO.md`.

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

The intended CI environment is:

```text
Python 3.11
Ubuntu
Windows
```

The standard automated suite does not require a Groq API key.

Pytest JUnit XML and normal stdout/stderr evidence are captured for diagnosis.

---

## 14. Generated Artifacts

A run can produce artifacts such as:

```text
run/
├── trace.json
├── report.json
├── report.md
├── fix.patch
├── junit.xml
└── metadata.json
```

These artifacts provide an auditable record of:

- what the agent observed
- what it changed
- what tests were run
- why it stopped
- whether the repair was verified

Exact artifact paths and names are determined by the implementation.

---

## 15. Installation

### Requirements

```text
Python 3.11+
Git
pytest
```

Install the project's dependencies using the dependency configuration included with the repository.

For live Groq evaluation, configure the required API credential through the environment.

Example:

```text
GROQ_API_KEY=<your-key>
LLM_MODEL=<configured-model>
```

**Never commit API keys to Git.**

---

## 16. Running Tests

The standard test command is:

```bash
pytest -v
```

JUnit XML output is used where structured CI/test evidence is required.

The normal test suite is deterministic and does not require live LLM calls.

---

## 17. Running CodePilot

The final CLI interface is defined by the implementation.

Conceptually:

```bash
codepilot <repository-or-sandbox>
```

To preserve the temporary workspace for debugging:

```bash
codepilot <repository-or-sandbox> --keep-workspace
```

The public CLI should expose only approved MVP controls.

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

The intended high-level structure is:

```text
CodePilot/
├── codepilot/
│   ├── config.py
│   ├── errors.py
│   ├── models.py
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
├── tests/
├── sandbox_repo/
├── .github/
│   └── workflows/
│
├── AGENTS.md
├── PRD.md
├── ARCHITECTURE.md
├── TOOLS.md
├── PROMPTS.md
├── SANDBOX_REPO.md
├── TESTING.md
├── TASKS.md
├── ADR.md
└── README.md
```

---

## 23. Project Documentation

The project is organized around these documents:

| Document | Purpose |
|---|---|
| `AGENTS.md` | Agent rules, safety boundaries, and operating policy |
| `PRD.md` | Product requirements and MVP scope |
| `ARCHITECTURE.md` | System architecture and control flow |
| `TOOLS.md` | Tool contracts and tool safety |
| `SANDBOX_REPO.md` | Deterministic sandbox and seeded defects |
| `PROMPTS.md` | LLM prompts and reasoning policy |
| `TESTING.md` | Test strategy and verification specification |
| `TASKS.md` | Dependency-ordered implementation plan |
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
