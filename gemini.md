# CodePilot: Project Instructions for Gemini CLI

CodePilot is an AI software engineering agent (Python, LLMs, pytest, Git, GitHub Actions). It inspects a repo, runs tests, reads logs, and applies targeted patches in a build, observe, diagnose, modify, test loop.

## Source of truth
Read these documents before doing any work, and re-read the relevant one before each task:
- @AGENTS.md (spec)
- @PRD.md (requirements)
- @ARCHITECTURE.md (design)
- @ADR.md (architecture decisions and their reasons)
- @TOOLS.md (tool definitions)
- @SANDBOX_REPO.md, @PROMPTS_CodePilot.md, @TESTING.md (supporting specs)
- @TASKS_CodePilot.md (build order)
- @README.md (already drafted; update it at the end to match what was actually built)

If a file is missing or two documents contradict each other, stop and ask me. Do not guess.

## Workflow rules
1. Start with a short summary of what you understood, then wait for my go-ahead.
2. Work through TASKS_CodePilot.md strictly in order, one task at a time.
3. After each task: run `pytest`, show me the result, then make a Git commit with a clear message.
4. Do not add features, files, or dependencies that are not in the docs.
5. Ask questions instead of making assumptions. This is a resume project, so every design choice must be one I can explain.
6. After the MVP passes on the first sandbox repo, stop and report. Do not start the second repo or any push/PR features.

## Technical decisions (do not change without asking)
- Language: Python. Dependencies go in `requirements.txt`.
- Development is on Windows: use Windows-compatible paths and commands (PowerShell, not bash-only syntax).
- LLM provider: Groq free tier. Read the key from the `GROQ_API_KEY` environment variable. Never hardcode or print it.
- The agent runs on a temporary copy of the sandbox repo with `git init`.
- The controller runs the full test suite automatically after every patch.
- Test results are parsed from pytest's built-in JUnit XML.
- The final fix is saved as `fix.patch`. The temp workspace is deleted unless `--keep-workspace` is passed.
- MVP agent modifies existing files only (no creating or deleting files).
- The agent must NEVER modify tests.
- NO_PROGRESS means the same failing tests and the same main error after 2 consecutive patches.
- Pushing or creating PRs is a later opt-in feature. The MVP stays local.

## Long-conversation handling
- Store each full tool result as one JSON file per tool call in persistent run state.
- Keep the 3 most recent tool results in full and compress older ones into structured summaries.
- The agent can retrieve an original result through a retrieval tool, up to 10 retrievals per run.

## Code style
- Small, readable functions with type hints and docstrings.
- Every module gets tests. Keep the code simple enough to explain in an interview.
- Never open, print, or commit `.env` (it holds my API key). Create `.gitignore` before the first commit and make sure it excludes `.env`, temp workspaces, `__pycache__`, `.pytest_cache` and virtual environments.