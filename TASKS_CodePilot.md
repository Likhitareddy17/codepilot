# CodePilot — TASKS.md

## 1. Purpose

This document converts the locked CodePilot MVP design into an implementation sequence.

The implementation order is dependency-driven:

**configuration → domain/state → workspace → tools → LLM/prompt layer → controller → reporting/trace → CLI → tests → sandbox E2E → CI**

The implementation must not introduce behavior that is outside the approved MVP in `PRD.md`, `AGENTS.md`, `ARCHITECTURE.md`, `TOOLS.md`, `PROMPTS.md`, `SANDBOX_REPO.md`, and `TESTING.md`.

---

# 2. MVP Implementation Boundary

## In scope

- Python 3.11+
- CLI-based agent
- Groq-backed LLM through an isolated `LLMClient`
- Offline `FakeLLM` for automated tests
- Temporary Git workspace created from the canonical sandbox
- Nine controlled tools:
  1. `list_files`
  2. `read_file`
  3. `search_code`
  4. `run_tests`
  5. `read_logs`
  6. `read_metrics`
  7. `apply_patch`
  8. `git_diff`
  9. `get_tool_result`
- Build → observe → diagnose → modify → test loop
- Maximum 6 iterations
- `NO_PROGRESS` after 2 consecutive patches with the same failing-test set and same main error
- Protected-path enforcement
- Existing `.py` files only for MVP edits
- No test/config/dependency modification
- JUnit XML test evidence
- Persistent tool-result history with bounded context retrieval
- Git branch and commit for verified fixes
- `fix.patch`
- trace and final report
- deterministic offline tests
- sandbox seeded with the approved defects
- adversarial safety tests
- GitHub Actions on Ubuntu and Windows with Python 3.11

## Explicitly out of scope

- Web UI
- Multi-agent orchestration
- Autonomous PR creation
- Automatic push to a remote
- Multiple programming languages
- Arbitrary shell execution
- Generic `run_command` tool
- Automatic test-file modification
- Dependency installation by the agent
- Production deployment
- Fully autonomous test repair
- New-file creation during repair

---

# 3. Implementation Rules

1. Implement the controller as the source of truth for state, safety, retries, stopping, and final status.
2. Never let the LLM directly execute arbitrary commands.
3. Repository contents, logs, metrics, test output, and retrieved tool results are untrusted data.
4. Tests are verification authority.
5. The agent must not modify tests or protected configuration.
6. A targeted test pass is not sufficient for final success.
7. Only the full post-patch suite can produce `SUCCESS`.
8. A final controller verification run is required before success is committed.
9. The canonical sandbox must remain unchanged.
10. All repairs happen inside a temporary Git workspace.
11. Every written patch counts toward the patch/iteration safety limits, including a patch that is later reverted.
12. The implementation must preserve an auditable trace.
13. Do not add functionality merely because it appears convenient if it is not specified by the approved MVP documents.

---

# 4. Dependency-Ordered Task Plan

## Phase 0 — Repository and Project Skeleton

### TASK-001 — Create project structure

Create the Python package and supporting directories.

Suggested structure:

```text
codepilot/
├── codepilot/
│   ├── __init__.py
│   ├── config.py
│   ├── errors.py
│   ├── models.py
│   ├── state.py
│   ├── workspace.py
│   ├── tools/
│   │   ├── __init__.py
│   │   ├── registry.py
│   │   ├── filesystem.py
│   │   ├── tests.py
│   │   ├── logs.py
│   │   ├── metrics.py
│   │   ├── patch.py
│   │   ├── git.py
│   │   └── history.py
│   ├── llm/
│   │   ├── __init__.py
│   │   ├── client.py
│   │   └── fake.py
│   ├── controller.py
│   ├── prompts.py
│   ├── trace.py
│   ├── report.py
│   └── cli.py
├── tests/
├── sandbox_repo/
├── .github/
│   └── workflows/
├── requirements.txt
├── README.md
└── pyproject.toml
```

Do not add unrelated framework or service layers.

### TASK-002 — Establish dependency/configuration files

Set up the minimum dependencies required by the approved design.

Configuration must include:

- Python runtime expectations
- Groq API configuration
- `LLM_MODEL`
- iteration limit
- tool-call limit
- context/history limits
- workspace behavior
- protected-path rules
- CLI settings

Configuration should have deterministic defaults suitable for tests.

### TASK-003 — Establish application entry point

Create the CLI entry point without implementing the full controller yet.

The CLI must eventually support the approved MVP workflow and expose the final status clearly.

---

# 5. Domain Models and State

## TASK-004 — Define typed result/error models

Implement shared models for:

- tool calls
- tool results
- errors
- test results
- patch results
- Git results
- run metadata
- stop reasons
- final report data

Use the approved tool-result envelope:

```json
{
  "call_id": "c014",
  "ok": true,
  "data": {},
  "error": null,
  "truncated": false
}
```

Tool errors must use:

```json
{
  "code": "ERROR_CODE",
  "message": "Human-readable explanation"
}
```

### TASK-005 — Define run state

Implement the state required to track:

- run ID
- workspace
- branch
- current iteration
- current status
- baseline test result
- latest test result
- failing-test IDs
- main error
- patches applied
- changed files
- tool calls
- tool-result history
- retrieval count
- regression state
- stop reason
- final verification state
- commit state

The state must be serializable for trace/report generation.

### TASK-006 — Define stop reasons

Implement the approved stopping outcomes, including:

- successful completion
- baseline already passing
- maximum iterations
- no progress
- regression
- repeated regression
- tool-call limit
- malformed/invalid model behavior
- unavailable/rate-limited LLM after allowed recovery
- protected-path violation
- unexpected internal failure
- other explicitly defined safety stops

Do not invent additional semantic success states.

---

# 6. Workspace and Git Isolation

## TASK-007 — Implement temporary workspace creation

Implement:

```text
canonical sandbox
        ↓
temporary workspace copy
        ↓
Git initialization
        ↓
repair branch
        ↓
agent execution
```

The canonical source repository must never be modified.

The branch format should follow the approved design:

```text
codepilot/fix-<timestamp>
```

or the equivalent `<run_id>` form.

### TASK-008 — Implement workspace cleanup

At the end of a run:

- delete the temporary workspace by default
- preserve it when `--keep-workspace` is supplied
- ensure reports/artifacts needed outside the workspace are retained

Cleanup must also be tested after failures.

### TASK-009 — Implement Git audit operations

Implement only the Git behavior required by the MVP:

- initialize/prepare workspace
- create repair branch
- inspect diff
- create commit after verified success

Do not implement:

- force push
- remote push
- reset/discard as an agent recovery mechanism
- autonomous PR creation

### TASK-010 — Implement final changed-file audit

Before committing:

1. inspect changed files
2. verify only allowed existing `.py` source files changed
3. verify tests are untouched
4. verify protected configuration is untouched
5. verify no unexpected files were created
6. stop safely if the audit fails

---

# 7. Tool Layer

## TASK-011 — Implement tool registry

Create a central registry for the nine approved tools.

The registry must:

- expose only approved tools
- validate tool names
- validate arguments
- return the standard result envelope
- prevent accidental access to unregistered capabilities

### TASK-012 — Implement `list_files`

Requirements:

- operate only within the workspace
- prevent path traversal
- return deterministic file information
- support the approved scope/filters
- never expose files outside the workspace

### TASK-013 — Implement `read_file`

Requirements:

- validate paths
- prevent traversal
- enforce file-size/output limits
- return useful errors
- treat file content as untrusted data

### TASK-014 — Implement `search_code`

Requirements:

- literal search by default
- regex only when explicitly requested
- validate arguments
- enforce output limits
- regex execution must have a genuine hard timeout
- prevent path escape

### TASK-015 — Implement `run_tests`

Requirements:

- execute only the approved pytest workflow
- capture stdout/stderr
- generate JUnit XML
- return structured test evidence
- identify failing test IDs
- distinguish baseline, targeted, and full-suite runs
- prevent command/option injection

The tool must not become a generic shell executor.

### TASK-016 — Implement `read_logs`

Requirements:

- read approved log files only
- enforce output limits
- support the optional `contains` filter
- return deterministic errors for invalid paths

### TASK-017 — Implement `read_metrics`

MVP scope:

- JSON metrics only
- validate JSON
- return structured metric data
- enforce output/size limits
- do not execute or interpret arbitrary formats as code

### TASK-018 — Implement `apply_patch`

Requirements:

- modify existing `.py` source files only
- reject tests and protected paths
- reject new-file creation
- reject configuration/dependency modification
- validate patch structure
- enforce patch-size limits
- count every actually written patch toward the safety limit

### TASK-019 — Implement `git_diff`

Return the current workspace diff needed for:

- verification
- traceability
- final audit
- report generation

### TASK-020 — Implement `get_tool_result`

Requirements:

- retrieve prior full tool results by valid `call_id`
- prevent retrieval of nonexistent IDs
- enforce the maximum retrieval count of 10 per run
- preserve full historical results even when older context has been compressed

---

# 8. Tool Safety and Validation

## TASK-021 — Centralize path validation

Create one safety mechanism for:

- workspace containment
- protected paths
- allowed editable extensions
- test paths
- hidden/sensitive files
- path traversal

All relevant tools must use the same policy rather than implementing independent versions.

### TASK-022 — Implement protected-path policy

Protect at minimum:

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

Apply the complete protected-path policy from `AGENTS.md` and `TOOLS.md`.

### TASK-023 — Implement argument validation

Perform handwritten validation before tool execution.

Reject:

- malformed arguments
- unexpected arguments
- invalid paths
- invalid ranges
- invalid regex options
- unsafe test options
- unsupported commands/options

---

# 9. Test Runner and Evidence

## TASK-024 — Implement structured test execution

Build the test-runner layer around pytest.

Record:

- exit status
- stdout
- stderr
- JUnit XML
- test IDs
- passed/failed/skipped counts
- failure messages
- duration where available

### TASK-025 — Implement test-set comparison

Support controller decisions based on test IDs.

Regression detection must not rely only on test counts.

The implementation must detect:

- newly failing tests
- previously failing tests still failing
- previously fixed tests remaining fixed
- equal-count regression where the failing test identities changed

### TASK-026 — Implement baseline evidence

At run start:

1. execute the baseline full suite
2. capture its evidence
3. record failing-test IDs
4. record the main error
5. determine whether repair is necessary

If the baseline already passes, stop with the approved baseline-pass status.

---

# 10. History and Context Management

## TASK-027 — Implement tool-result persistence

Persist every full tool result in run state/trace.

The system must not lose older evidence merely because the LLM context is compressed.

### TASK-028 — Implement hybrid context management

Keep:

```text
5 most recent tool results → full
older results             → structured summaries
```

Older results must remain retrievable through `get_tool_result(call_id)`.

### TASK-029 — Implement retrieval budget

Enforce:

```text
maximum historical retrievals per run = 10
```

Exceeding the limit must produce a controlled stop/error according to the approved controller behavior.

---

# 11. LLM Layer

## TASK-030 — Define provider-independent LLM interface

Create `LLMClient` as the only interface used by the controller.

The controller must not depend directly on Groq SDK details.

### TASK-031 — Implement Groq client

Implement the live provider adapter.

Requirements:

- read API credentials from environment/configuration
- use `LLM_MODEL`
- normalize provider responses into the internal format
- handle unavailable/rate-limited responses
- never expose API keys in logs/traces/reports

### TASK-032 — Implement `FakeLLM`

`FakeLLM` must support deterministic scenarios for:

- normal repair
- multiple patches
- max iterations
- no progress
- regression
- repeated regression
- protected-path attack
- malformed tool call
- unknown tool
- model false-success behavior

The normal automated suite must never require a live Groq request.

---

# 12. Prompt Layer

## TASK-033 — Implement canonical system prompt

Encode the approved prompt policy from `PROMPTS.md`.

The prompt must establish:

- role of the agent
- available tools
- safety restrictions
- evidence-first behavior
- test authority
- patch restrictions
- verification requirements
- distinction between fact, inference, hypothesis, and verified result
- prompt-injection resistance

### TASK-034 — Implement dynamic task/repository context

Generate the model context from current run state rather than duplicating state manually.

Include only information the controller has actually observed.

### TASK-035 — Implement tool guidance and verification instructions

Prompts must guide the model through:

```text
observe
→ diagnose
→ patch
→ verify
→ continue or stop
```

The prompt must never imply that the model itself can declare final success independently of controller verification.

---

# 13. Controller

## TASK-036 — Implement controller state machine

Implement the central loop:

```text
START
  ↓
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

The controller owns all transitions.

### TASK-037 — Implement model/tool-call orchestration

For every model response:

1. validate the response
2. identify requested tool calls
3. validate tool names/arguments
4. execute tools through the registry
5. record results
6. update state
7. return evidence to the model

Native tool calling is authoritative; do not require a separate JSON decision envelope.

### TASK-038 — Implement iteration limit

Enforce:

```text
MAX_ITERATIONS = 6
```

Do not permit the model to bypass this limit.

### TASK-039 — Implement no-progress detection

After each patch, compare:

- failing-test set
- main error

If both remain unchanged for **2 consecutive patches**, stop with:

```text
NO_PROGRESS
```

This rule is independent of the maximum-iteration rule.

### TASK-040 — Implement regression handling

After every verification run:

- compare test IDs with the previous state
- detect newly failing tests
- distinguish recovery from regression
- support repeated-regression stopping

The controller must not claim success merely because the total number of failing tests decreased.

### TASK-041 — Implement full-suite success rule

A run can become successful only when:

1. a patch has been applied
2. the full test suite passes
3. the final controller verification run confirms the result
4. the changed-file audit passes

Only then may the controller proceed to commit/report success.

### TASK-042 — Implement protected-path/model-abuse handling

Detect and stop on repeated unsafe model behavior, including:

- invalid tool names
- invalid arguments
- protected-path patch attempts
- disallowed file modifications

Use the approved default threshold for repeated bad-model/protected-path behavior.

### TASK-043 — Implement tool-call budget

Enforce the configured tool-call limit.

A runaway model must result in a controlled stop rather than an unbounded execution loop.

### TASK-044 — Implement LLM error recovery

Handle:

- transient unavailable errors
- rate limits
- malformed model responses
- repeated failures

Use only the recovery behavior defined by the approved MVP.

---

# 14. Patch Verification and Commit

## TASK-045 — Implement post-patch verification pipeline

For every accepted patch:

```text
apply patch
→ inspect diff
→ run targeted evidence if required
→ run full suite
→ compare test IDs
→ check regression
→ update state
```

A targeted pass alone must never produce final success.

### TASK-046 — Implement final audit

Before success/commit, verify:

- final full-suite pass
- final changed-file list
- no protected files changed
- no tests changed
- no new files created
- expected source changes only
- no secrets leaked into generated artifacts

### TASK-047 — Implement Git commit

Commit only after all success conditions are satisfied.

The commit should contain the verified source repair and remain on the temporary repair branch.

No remote push is performed.

---

# 15. Trace and Reporting

## TASK-048 — Implement execution trace

Record enough information to reconstruct the run:

- run metadata
- baseline result
- iterations
- model interactions
- tool calls
- tool results
- patches
- test results
- regressions
- stop reason
- final verification
- changed files
- commit information
- cleanup status

### TASK-049 — Implement `fix.patch`

Generate a patch artifact from the verified Git diff.

This artifact must be suitable for independent verification outside the agent.

### TASK-050 — Implement final report

The final report should summarize:

- run ID
- initial failure state
- diagnosis
- patches attempted
- verification results
- final status
- stop reason
- changed files
- commit
- important evidence
- artifacts produced

Do not report success unless the controller's success invariant was satisfied.

---

# 16. CLI

## TASK-051 — Implement end-to-end CLI command

Connect:

```text
CLI
→ config
→ workspace
→ baseline
→ controller
→ report/trace
→ cleanup
```

### TASK-052 — Implement CLI options

Expose only approved MVP controls, including the workspace-preservation option:

```text
--keep-workspace
```

Avoid exposing internal implementation details as public CLI features unless specified.

### TASK-053 — Implement exit codes

Use the approved semantics:

```text
0 = success / no repair required
1 = controlled stop or unsuccessful run
2 = startup/configuration error
```

### TASK-054 — Implement console-safe output

Ensure useful output on Windows and Linux, including safe handling of console encoding.

---

# 17. Sandbox Repository

## TASK-055 — Build the canonical sandbox

Create the approved structure:

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

Keep the sandbox small, deterministic, and suitable for repeated CI runs.

### TASK-056 — Seed the approved defects

Implement the four deliberate defects defined by `SANDBOX_REPO.md`.

The exact test IDs, expected values, log values, and defect implementation must follow the sandbox manifest rather than being invented during implementation.

Required defect categories:

1. pagination off-by-one
2. `None` handling
3. discount calculation
4. cross-evidence diagnostic defect

### TASK-057 — Verify sandbox baseline

Confirm:

- expected files exist
- expected tests exist
- baseline has the intended failures
- logs are deterministic
- metrics are deterministic
- canonical sandbox is unchanged by an agent run

---

# 18. Unit Tests

## TASK-058 — Implement configuration tests

Test:

- defaults
- environment variables
- invalid configuration
- numeric bounds
- `LLM_MODEL`
- protected-path configuration

### TASK-059 — Implement model/state tests

Test:

- state transitions
- serialization
- error structures
- stop reasons
- run metadata

### TASK-060 — Implement all tool unit tests

Cover all nine tools, including:

- normal operation
- invalid arguments
- path traversal
- output limits
- protected paths
- malformed inputs
- tool-specific safety rules

### TASK-061 — Implement history tests

Test:

- five-result full context window
- older-result summarization
- persistent full-result storage
- retrieval by `call_id`
- retrieval limit of 10

### TASK-062 — Implement workspace/Git tests

Test:

- temporary copy
- canonical repo isolation
- branch creation
- diff collection
- commit behavior
- cleanup
- `--keep-workspace`

### TASK-063 — Implement LLM/prompt tests

Test:

- provider abstraction
- FakeLLM
- prompt construction
- prompt injection resistance
- malformed provider responses
- rate-limit/unavailable handling
- secret non-disclosure

### TASK-064 — Implement controller unit tests

Cover:

- baseline pass
- success
- max iterations
- no progress
- regression
- repeated regression
- tool-call cap
- model misuse
- protected-path attempts
- final audit failure

---

# 19. Offline End-to-End Tests

## TASK-065 — Implement successful repair E2E

Use `FakeLLM` to repair the seeded sandbox and verify:

- intended source changes occur
- all tests pass
- only allowed files change
- trace/report are produced
- commit is created
- canonical sandbox remains unchanged

### TASK-066 — Implement baseline-pass E2E

Start from a passing repository and verify that the controller does not create unnecessary patches.

### TASK-067 — Implement max-iteration E2E

Force the FakeLLM to continue proposing unsuccessful patches and verify:

```text
MAX_ITERATIONS
```

### TASK-068 — Implement no-progress E2E

Return repeated patches that leave:

- the same failing-test set
- the same main error

for two consecutive patches.

Verify:

```text
NO_PROGRESS
```

### TASK-069 — Implement regression recovery E2E

Simulate:

```text
initial failure
→ patch
→ regression
→ corrective patch
→ full pass
```

Verify the controller recovers safely.

### TASK-070 — Implement repeated-regression E2E

Force repeated regression and verify controlled stopping.

### TASK-071 — Implement equal-count regression E2E

Ensure the controller detects regression when the number of failing tests is unchanged but the failing test IDs have changed.

### TASK-072 — Implement false-success E2E

Make the model claim success while the full suite still fails.

Verify the controller rejects the claim.

### TASK-073 — Implement targeted-pass E2E

Make a targeted test pass while another test remains failing.

Verify final success is not reported.

### TASK-074 — Implement malformed/unknown tool E2E

Verify safe handling of:

- malformed tool calls
- unknown tools
- invalid arguments

### TASK-075 — Implement LLM failure E2E

Cover:

- unavailable LLM
- rate-limit recovery
- repeated provider failure

### TASK-076 — Implement protected-path attack E2E

Attempt modifications to:

- tests
- configuration
- dependency files
- `.env`
- GitHub workflow files

Verify rejection and safe stop.

### TASK-077 — Implement output-audit E2E

Ensure an apparently successful run is rejected if the final changed-file audit detects an unexpected modification.

### TASK-078 — Implement cleanup/source-isolation E2E

Verify:

- temporary workspace is deleted by default
- `--keep-workspace` preserves it
- canonical source remains unchanged

### TASK-079 — Implement CLI smoke E2E

Run the real CLI entry point with `FakeLLM` injection and verify exit codes and artifacts.

---

# 20. Adversarial Tests

## TASK-080 — Prompt-injection test

Place malicious instructions inside:

- source code
- logs
- metrics
- test output

Verify the model/controller treats them as untrusted repository data.

### TASK-081 — Path traversal test

Attempt paths such as:

```text
../
../../
absolute paths
workspace escapes
```

Verify rejection.

### TASK-082 — Option-injection test

Attempt unsafe pytest/tool arguments.

Verify they cannot turn the test runner into arbitrary command execution.

### TASK-083 — Secret-exfiltration test

Place sentinel secrets in controlled files and verify they are not unnecessarily emitted into traces/reports/model-facing output.

### TASK-084 — Output-flood test

Generate oversized tool output and verify truncation/limits.

### TASK-085 — Forbidden-Git-operation test

Attempt unsupported Git operations and verify the agent cannot perform them through the approved tool surface.

### TASK-086 — Test-gaming test

Attempt:

- modifying tests
- weakening assertions
- skipping tests
- adding xfail behavior

The MVP must reject protected-file modifications. The known limitation around detecting every semantic form of test gaming must remain documented.

### TASK-087 — New-file creation test

Attempt to create a new source/config/test file through patching.

Verify rejection.

### TASK-088 — Retrieval-ID attack test

Attempt:

- nonexistent `call_id`
- malformed IDs
- repeated retrieval beyond the limit

Verify safe handling.

---

# 21. Independent Fix Verification

## TASK-089 — Verify generated patch independently

After a successful run:

1. obtain `fix.patch`
2. apply it to a clean copy of the canonical sandbox
3. run the full pytest suite independently
4. verify the result without using CodePilot's internal state

This demonstrates that the patch itself, rather than the agent's report, is the source of truth.

---

# 22. CI

## TASK-090 — Configure GitHub Actions

CI must run on:

- Ubuntu
- Windows

with:

```text
Python 3.11
```

### TASK-091 — Run deterministic test suite in CI

CI must execute the normal offline suite without requiring a Groq API key.

### TASK-092 — Preserve test evidence

Use pytest JUnit XML and normal stdout/stderr capture.

CI failures must be diagnosable from the produced evidence.

---

# 23. Live Evaluation

## TASK-093 — Create live evaluation procedure

Run the agent against the canonical sandbox using the configured Groq model.

Record:

- model
- run ID
- initial failures
- iterations
- tool calls
- patches
- final status
- runtime
- token/cost information when available
- failure mode if unsuccessful

### TASK-094 — Do not mix live evaluation with deterministic tests

The standard test suite must remain offline and deterministic.

Live evaluation is evidence of practical behavior, not a prerequisite for every unit test.

---

# 24. Documentation Completion

## TASK-095 — Complete README

README must explain:

- what CodePilot is
- architecture
- setup
- configuration
- CLI usage
- sandbox
- test commands
- safety model
- generated artifacts
- example run

### TASK-096 — Add architecture diagram

Include the approved high-level flow:

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

---

# 25. Final Verification Sequence

Before considering the MVP complete, execute the following sequence:

```text
1. Run static/project sanity checks
2. Run unit tests
3. Run adversarial tests
4. Run offline FakeLLM E2E tests
5. Verify sandbox integrity
6. Verify CLI smoke test
7. Verify independent fix.patch application
8. Run CI on Ubuntu
9. Run CI on Windows
10. Perform live Groq evaluation
11. Review trace/report artifacts
12. Confirm no protected files were modified
13. Confirm canonical sandbox remains unchanged
14. Confirm all Definition-of-Done criteria
```

---

# 26. Definition of Done

The CodePilot MVP is complete only when all of the following are true:

- [ ] CLI runs successfully from a clean environment.
- [ ] Canonical sandbox is never modified by an agent run.
- [ ] Temporary Git workspace is created correctly.
- [ ] Repair branch is created correctly.
- [ ] All nine approved tools work and enforce their contracts.
- [ ] No arbitrary shell tool exists.
- [ ] Only existing `.py` source files may be modified.
- [ ] Tests/configuration/dependencies remain protected.
- [ ] Baseline evidence is captured.
- [ ] Full-suite verification controls final success.
- [ ] Final controller verification is performed.
- [ ] Maximum iterations are enforced at 6.
- [ ] No-progress detection triggers after two consecutive unchanged failures.
- [ ] Regression detection is test-ID based.
- [ ] Tool-call budget is enforced.
- [ ] Historical tool results remain retrievable.
- [ ] Five most recent tool results remain full in active context.
- [ ] Older results are compressed into structured summaries.
- [ ] Retrieval limit is enforced at 10 per run.
- [ ] FakeLLM enables deterministic offline tests.
- [ ] Normal tests make no live Groq calls.
- [ ] Protected-path and prompt-injection defenses are tested.
- [ ] Output and path safety limits are tested.
- [ ] Final changed-file audit passes before commit.
- [ ] Verified successful runs create a Git commit.
- [ ] `fix.patch` is generated.
- [ ] Trace and final report are generated.
- [ ] Cleanup works on success and failure.
- [ ] `--keep-workspace` works.
- [ ] CLI exit codes follow the approved contract.
- [ ] Sandbox contains the intended seeded defects.
- [ ] Independent application of `fix.patch` passes the full suite.
- [ ] CI passes on Ubuntu and Windows using Python 3.11.
- [ ] Live evaluation is recorded honestly.
- [ ] README and architecture documentation are complete.

---

# 27. Recommended Implementation Order

For actual coding, follow this exact order rather than implementing features randomly:

```text
TASK-001  Project skeleton
    ↓
TASK-002  Configuration
    ↓
TASK-004–006  Models + state + stop reasons
    ↓
TASK-007–010  Workspace + Git
    ↓
TASK-011–023  Tool registry + all tools + safety
    ↓
TASK-024–026  Test runner + baseline evidence
    ↓
TASK-027–029  History/context management
    ↓
TASK-030–032  LLM abstraction + Groq + FakeLLM
    ↓
TASK-033–035  Prompts
    ↓
TASK-036–044  Controller
    ↓
TASK-045–047  Verification + audit + commit
    ↓
TASK-048–050  Trace + patch + report
    ↓
TASK-051–054  CLI
    ↓
TASK-055–057  Sandbox
    ↓
TASK-058–064  Unit tests
    ↓
TASK-065–079  Offline E2E
    ↓
TASK-080–088  Adversarial tests
    ↓
TASK-089  Independent patch verification
    ↓
TASK-090–092  CI
    ↓
TASK-093–094  Live evaluation
    ↓
TASK-095–096  Documentation
```

## Final implementation principle

Do not start by making the LLM autonomous.

Build the **controlled execution system first**:

> **State → tools → evidence → controller → LLM → verification → Git → report**

The LLM proposes actions. CodePilot's controller decides whether those actions are legal, executes them through controlled tools, evaluates the resulting evidence, and determines whether the run may continue or stop.
