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


Summary: 5 runs, 1 verified success, median 11 iterations. Stop reasons: MAX_ITERATIONS x4, SUCCESS x1.

Observations:
- A live probe (`pytest -m live -k batch`) showed the model returns one tool call per reply, so the first two runs hit the 6-iteration limit before any patch was applied. The default limit is now 15 (ADR-027).
- Run 5 used the documented defaults (limit 15, recent window of 3) and applied no patch; the same source and test files were read repeatedly. A shortened `read_file` result contains no code, which the history now compensates for (ADR-010 amendment). That change has not been tested live yet; record its runs as a separate batch.
- One of five runs reached SUCCESS, but this sample is too small to establish a reliable success rate. More runs are needed before drawing conclusions.