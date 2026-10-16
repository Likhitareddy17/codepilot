r"""Turn finished run folders into rows of the live-evaluation record (docs/demo/runs.md).

Usage (PowerShell):  .venv\Scripts\python tests\live_record.py runs\<run_id> [runs\<run_id> ...]
It reads only run_trace.json, so the numbers come from the recorded run, not from memory.
"""

import json
import statistics
import sys
from collections import Counter
from pathlib import Path
from typing import Any, Dict, List

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from codepilot.safety import is_protected  # noqa: E402

COLUMNS = ["Date", "Model", "Prompt", "Run ID", "Baseline", "Final", "Stop reason", "Iterations", "LLM calls",
           "Tool calls", "Patches", "Tokens", "Runtime", "Protected files", "Notes"]


def load_trace(run_dir: Path) -> Dict[str, Any]:
    return json.loads((Path(run_dir) / "run_trace.json").read_text(encoding="utf-8"))


def _ratio(tests: Dict[str, Any] | None) -> str:
    if not tests:
        return "n/a"
    return f"{tests['passed']}/{tests['passed'] + tests['failed'] + tests['errors']}"


def facts(trace: Dict[str, Any]) -> Dict[str, str]:
    """The recorded facts of one run, as display strings (keys are COLUMNS)."""
    run, outcome, events = trace["run"], trace["outcome"], trace["events"]
    llm = [e for e in events if e.get("event") == "llm_response"]
    tokens = sum((e.get("usage") or {}).get("total_tokens", 0) for e in llm)
    patches = trace["patches"]
    attempts = sum(1 for e in events if e.get("event") == "protected_path_attempt")
    touched = [f for f in outcome["changed_files"] if is_protected(f)]
    success = outcome["success"]
    config = f"max_iterations={run['max_iterations']}"
    if run.get("history_keep_recent_results") is not None:
        config += f", keep_recent={run['history_keep_recent_results']}"
    return {
        "Date": (run.get("started_at") or "")[:10],
        "Model": run["model"],
        "Prompt": str(run.get("prompt_version", "n/a")),
        "Run ID": run["run_id"],
        "Baseline": _ratio((trace.get("baseline") or {}).get("tests")),
        "Final": _ratio(trace.get("final_tests")),
        "Stop reason": outcome["stop_reason"],
        "Iterations": str(outcome["iterations"]),
        "LLM calls": str(len(llm)),
        "Tool calls": str(sum(1 for c in trace["tool_calls"] if c["source"] == "model")),
        "Patches": f"{sum(1 for p in patches if p['applied'])} applied, {sum(1 for p in patches if p['reverted'])} reverted",
        "Tokens": str(tokens) if tokens else "n/a",
        "Runtime": f"{run['duration_s']:.0f}s",
        "Protected files": ("TOUCHED: " + ", ".join(touched)) if touched else f"none ({attempts} attempt(s) refused)",
        "Notes": config if success else f"{config}; {outcome['summary']}",
    }


def header() -> List[str]:
    return ["| " + " | ".join(COLUMNS) + " |", "|" + "|".join(" --- " for _ in COLUMNS) + "|"]


def row(values: Dict[str, str]) -> str:
    return "| " + " | ".join(values[c].replace("|", "/").replace("\n", " ") for c in COLUMNS) + " |"


def summary_lines(rows: List[Dict[str, str]], successes: int) -> List[str]:
    """Honest totals: every run counts, unsuccessful ones included."""
    iterations = [int(r["Iterations"]) for r in rows]
    reasons = ", ".join(f"{k} x{v}" for k, v in sorted(Counter(r["Stop reason"] for r in rows).items()))
    return [f"Runs: {len(rows)}; verified successes: {successes}; median iterations: {statistics.median(iterations):g}",
            f"Stop reasons: {reasons}"]


def main(argv: List[str]) -> int:
    if not argv:
        print(__doc__)
        return 2
    traces = [load_trace(Path(a)) for a in argv]
    rows = [facts(t) for t in traces]
    print("\n".join(header() + [row(r) for r in rows]))
    print()
    print("\n".join(summary_lines(rows, sum(1 for t in traces if t["outcome"]["success"]))))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
