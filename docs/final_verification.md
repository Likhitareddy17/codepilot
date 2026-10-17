# Final verification (TASKS_CodePilot.md sections 25 and 26)

This file maps the 14-step verification sequence and the Definition of Done to commands and to the
automated tests that cover them. **Automated** means a test enforces it on every run. **Your check** means
it needs a person at a real machine (clean environment, GitHub, a live Groq key). Tick a box only after
you have seen the evidence, and record the date.

## Verification sequence

| # | Step | How | Status |
|---|---|---|---|
| 1 | Static and project sanity checks | `.venv\Scripts\python -m compileall -q codepilot tests` | [ ] Your check |
| 2 | Unit tests | `.venv\Scripts\python -m pytest -q -m "not slow"` | [ ] Your check |
| 3 | Adversarial tests | `.venv\Scripts\python -m pytest -q tests/test_adversarial.py` | [ ] Your check |
| 4 | Offline FakeLLM E2E tests | `.venv\Scripts\python -m pytest -q tests/test_e2e_sandbox.py tests/test_controller.py tests/test_cli.py` | [ ] Your check |
| 5 | Sandbox integrity | `.venv\Scripts\python -m pytest -q tests/test_sandbox.py` | [ ] Your check |
| 6 | CLI smoke test from a clean environment | fresh clone, new venv, `pip install -r requirements.txt`, then `python main.py --repo sandbox_repo` (needs a key), or `python main.py --help` and a missing-repo run without one | [ ] Your check |
| 7 | Independent `fix.patch` application | `tests/test_sandbox.py` does it for the offline run; for a live run use the commands in README section 17 | [ ] Your check (live run `20261016-075658`) |
| 8 | CI on Ubuntu | GitHub Actions, latest commit on `main` | [ ] Your check |
| 9 | CI on Windows | GitHub Actions, latest commit on `main` | [ ] Your check |
| 10 | Live Groq evaluation | `docs/live_evaluation.md`; every run in `docs/demo/runs.md` | [ ] Your check (3 runs recorded, 1 verified success) |
| 11 | Review trace and report artifacts | open `runs\<run_id>\report.md` and `run_trace.json` of a live run | [ ] Your check |
| 12 | No protected file modified | automated in every sandbox run and in the live test; also read `fix.patch` (only `app/*.py` may change) | Automated + [ ] Your check |
| 13 | Canonical sandbox unchanged | automated by hash in `tests/test_sandbox.py`, `tests/test_e2e_sandbox.py` and the live test | Automated |
| 14 | Definition of Done | table below | see below |

## Definition of Done

| Criterion | Evidence | Status |
|---|---|---|
| CLI runs successfully from a clean environment | step 6 | Your check |
| Canonical sandbox never modified by a run | hash checks in `test_sandbox.py`, `test_e2e_sandbox.py`, `tests/live` | Automated |
| Temporary Git workspace created correctly | `test_workspace.py` | Automated |
| Repair branch created correctly | `test_workspace.py`, `test_cli.py` | Automated |
| Nine tools work and enforce their contracts | `test_tools_*.py`, `test_registry.py`, `test_adversarial.py` | Automated |
| No arbitrary shell tool | `test_adversarial.py` (tool surface is exactly nine tools; git and shell names are unknown) | Automated |
| Only existing `.py` files may be modified | `test_tools_patch.py`, `test_adversarial.py` (new-file attempts) | Automated |
| Tests, configuration and dependencies protected | `test_safety.py`, `test_adversarial.py` (21 protected paths, test-gaming edits) | Automated |
| Baseline evidence captured | `test_test_runner.py`, `test_phase4_decisions.py`, `test_controller.py` | Automated |
| Full-suite verification controls success | `test_controller.py`, `test_e2e_sandbox.py` (false success, targeted pass) | Automated |
| Final controller verification performed | `test_controller.py` | Automated |
| Maximum iterations enforced (default 15) | `test_e2e_sandbox.py`, `test_config.py`, ADR-027 | Automated |
| No-progress after two unchanged failures | `test_e2e_sandbox.py`, `test_controller.py` | Automated |
| Regression detection is test-ID based | `test_test_comparison.py`, `test_e2e_sandbox.py` (equal-count regression) | Automated |
| Tool-call budget enforced | `test_controller.py` | Automated |
| Historical results retrievable | `test_history.py`, `test_adversarial.py` | Automated |
| Three recent results full in context; older compressed | `test_history.py`, `test_controller.py` | Automated |
| Retrieval limit enforced (10 per run) | `test_history.py`, `test_adversarial.py` | Automated |
| FakeLLM enables deterministic offline tests | `test_fake_llm.py` and every E2E test | Automated |
| Normal tests make no live Groq calls | live tests need `-m live`; CI sets no key; `conftest.py` clears settings variables | Automated |
| Protected-path and prompt-injection defences tested | `test_adversarial.py` | Automated |
| Output and path safety limits tested | `test_adversarial.py`, `test_path_resolution.py`, tool tests | Automated |
| Changed-file audit passes before commit | `test_controller.py` | Automated |
| Verified runs create a Git commit | `test_cli.py`, `test_sandbox.py` | Automated |
| `fix.patch` generated | `test_cli.py`, `test_sandbox.py` | Automated |
| Trace and report generated | `test_trace.py`, `test_report.py`, `test_cli.py` | Automated |
| Cleanup on success and failure | `test_cli.py`, `test_e2e_sandbox.py` | Automated |
| `--keep-workspace` works | `test_cli.py` | Automated |
| Exit codes follow the contract | `test_cli.py` | Automated |
| Sandbox contains the intended seeded defects | `test_sandbox.py` | Automated |
| Independent `fix.patch` application passes | `test_sandbox.py` (offline), step 7 (live) | Automated + Your check |
| CI passes on Ubuntu and Windows (Python 3.11) | steps 8 and 9 | Your check |
| Live evaluation recorded honestly | `docs/demo/runs.md` | Your check |
| README and architecture documentation complete | `README.md`, `ARCHITECTURE.md` (approved diagram) | Done in Phase 18; review it |

## Known limitations that stay open (documented in README section 20)

- The live success rate is not established (1 verified success in 3 recorded runs).
- Secret-shaped text inside readable source and log files is not redacted in tool output.
- `PROTECTED_PATH_NEEDED` is not produced; `MAX_REQUEST_TOKENS_ESTIMATE` is not enforced.
- Semantic test gaming inside an allowed source file cannot be detected by the MVP.
