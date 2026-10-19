# Live evaluation record

Every live run against the canonical sandbox is recorded here, including the unsuccessful ones.
README and resume numbers must come from this table, never from a single best-case run.
Rows are produced by `tests\live_record.py` from each run's `run_trace.json`; see `docs/live_evaluation.md`.

Free-plan Groq limits (gpt-oss-120b, sample figures from Groq's documentation, read 2026-10) are about 8,000
tokens per minute and 200,000 per day, per organisation, so only a handful of full runs fit in a day.

| Date | Model | Prompt | Run ID | Baseline | Final | Stop reason | Iterations | LLM calls | Tool calls | Patches | Tokens | Runtime | Protected files | Notes |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 2026-10-15 | openai/gpt-oss-120b | 1 | 20261015-170826 | 4/8 | 4/8 | MAX_ITERATIONS | 6 | 6 | 6 | 0 applied, 0 reverted | 16258 | 66s | none (0 attempt(s) refused) | max_iterations=6; The iteration limit (6) was reached without a verified fix. |
| 2026-10-16 | openai/gpt-oss-120b | 2 | 20261016-074108 | 4/8 | 4/8 | MAX_ITERATIONS | 6 | 6 | 6 | 0 applied, 0 reverted | 17288 | 75s | none (0 attempt(s) refused) | max_iterations=6; The iteration limit (6) was reached without a verified fix. |
| 2026-10-16 | openai/gpt-oss-120b | 2 | 20261016-075658 | 4/8 | 8/8 | SUCCESS | 11 | 11 | 11 | 4 applied, 0 reverted | 42750 | 203s | none (0 attempt(s) refused) | max_iterations=14 |
| 2026-10-17 | openai/gpt-oss-120b | 2 | 20261017-124438 | 4/8 | 7/8 | MAX_ITERATIONS | 14 | 14 | 14 | 3 applied, 0 reverted | 56545 | 343s | none (0 attempt(s) refused) | max_iterations=14, keep_recent=8; The iteration limit (14) was reached without a verified fix. |
| 2026-10-18 | openai/gpt-oss-120b | 2 | 20261018-111755 | 4/8 | 4/8 | MAX_ITERATIONS | 15 | 15 | 15 | 0 applied, 0 reverted | 55179 | 324s | none (0 attempt(s) refused) | max_iterations=15, keep_recent=3; The iteration limit (15) was reached without a verified fix. |
| 2026-10-19 | openai/gpt-oss-120b | 2 | 20261019-232011 | 4/8 | 8/8 | SUCCESS | 13 | 13 | 13 | 4 applied, 0 reverted | 51185 | 292s | none (0 attempts refused) | max_iterations=15, keep_recent=3; file-read retention enabled; live validation successful |


**Summary: ** 6 live runs, 2 verified successes, median 12 iterations.

**Observations: **
- The initial runs exposed iteration-budget and history-management limitations, which informed subsequent improvements.
- Run `20261019-232011` successfully repaired the canonical sandbox from 4/8 to 8/8 passing tests in 13 iterations, applying four patches with none reverted.
- Results are preliminary; the small sample does not establish a reliable success rate or prove that history retention alone caused the successful outcome.