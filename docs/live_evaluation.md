# Live evaluation procedure (TASK-093, TASK-094)

Live evaluation uses the real Groq model. It is separate from the deterministic suite: it never runs in
CI or in a plain `pytest`, and its results are evidence about model behaviour, not a pass/fail gate.

## Prerequisites
- `.env` in the repository root with `GROQ_API_KEY=...` (optional: `GROQ_MODEL`, default `openai/gpt-oss-120b`).
- The offline suite is green (`.venv\Scripts\python -m pytest -q`).

## 1. Preflight: does the model call tools? (one request)
```powershell
.venv\Scripts\python -m pytest -m live -s -k tool_calling
```
It sends the real system prompt and the real nine tool schemas once and checks that the model returns a valid
call to one of them. If it fails, stop and read the printed message before spending more quota.

## 1b. Does the model batch tool calls? (one request)
```powershell
.venv\Scripts\python -m pytest -m live -s -k batch
```
Prints how many tool calls the model returned in a single reply. A model that returns one call per reply needs
roughly one iteration per file read and per patch. The default `MAX_ITERATIONS=15` (ADR-027) was chosen for such a
model: the sandbox repair took 11 iterations. Record the limit with every run (the recorder does it). To try another
model, set `GROQ_MODEL` for one run.

## 2. One complete run on the canonical sandbox
```powershell
.venv\Scripts\python -m pytest -m live -s -k sandbox_run
```
(or `.venv\Scripts\python main.py --repo sandbox_repo`). The run folder is `runs\<run_id>`. The test checks that the
canonical sandbox is unchanged and no protected file changed; it does not require success.

## 3. Record it
```powershell
.venv\Scripts\python tests\live_record.py runs\<run_id>
```
Paste the printed row into `docs\demo\runs.md`. Record failures and rate-limit stops too.
Add a note about the failure mode when the stop reason is not `SUCCESS`.

## 4. Repeat when the quota allows
Several runs give a success rate and a median iteration count. Pass all run folders to `live_record.py` to get the
totals. Report the success rate as recorded, with every stop reason.
