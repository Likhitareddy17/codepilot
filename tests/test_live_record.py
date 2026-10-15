"""Offline tests for the live-evaluation recorder (the live tests themselves never run in CI)."""

from codepilot.models import StopReason
from codepilot.trace import RunInfo, build_trace
from live_record import COLUMNS, facts, header, row, summary_lines
from support_results import make_result

INFO = RunInfo(repository="sandbox_repo", model="openai/gpt-oss-120b", started_at="2026-10-15T10:15:00+00:00",
               finished_at="t1", max_iterations=6, cleanup="deleted")


def trace_of(result):
    return build_trace(result, INFO)


def test_success_row_has_every_column_and_the_recorded_numbers():
    trace = trace_of(make_result())
    trace["events"][1]["usage"] = {"total_tokens": 1200}
    values = facts(trace)
    assert list(values) == COLUMNS
    assert values["Date"] == "2026-10-15" and values["Model"] == "openai/gpt-oss-120b" and values["Prompt"] != "n/a"
    assert (values["Baseline"], values["Final"], values["Stop reason"]) == ("2/4", "4/4", "SUCCESS")
    assert values["Iterations"] == "2" and values["LLM calls"] == "1" and values["Tool calls"] == "3"
    assert values["Patches"] == "1 applied, 0 reverted" and values["Tokens"] == "1200" and values["Notes"] == ""
    assert values["Protected files"] == "none (0 attempt(s) refused)"


def test_unsuccessful_run_keeps_its_failure_mode():
    values = facts(trace_of(make_result(StopReason.MAX_ITERATIONS, verified=False)))
    assert values["Stop reason"] == "MAX_ITERATIONS" and values["Final"] == "3/4" and values["Notes"]
    assert values["Tokens"] == "n/a"


def test_touched_protected_files_are_reported():
    trace = trace_of(make_result())
    trace["outcome"]["changed_files"] = ["app/calc.py", "tests/test_calc.py"]
    assert "TOUCHED: tests/test_calc.py" in facts(trace)["Protected files"]


def test_refused_protected_attempts_are_counted():
    trace = trace_of(make_result())
    trace["events"] += [{"event": "protected_path_attempt", "iteration": 1}] * 2
    assert facts(trace)["Protected files"] == "none (2 attempt(s) refused)"


def test_markdown_row_is_one_line_with_matching_columns():
    values = facts(trace_of(make_result(StopReason.NO_PROGRESS, verified=False)))
    values["Notes"] = "a | b\nc"
    line = row(values)
    assert "\n" not in line and line.count("|") == len(COLUMNS) + 1 and "a / b c" in line
    assert header()[0].count("|") == line.count("|")


def test_summary_counts_every_run():
    rows = [facts(trace_of(make_result())), facts(trace_of(make_result(StopReason.MAX_ITERATIONS, verified=False)))]
    first, second = summary_lines(rows, successes=1)
    assert "Runs: 2; verified successes: 1; median iterations: 2" in first
    assert second == "Stop reasons: MAX_ITERATIONS x1, SUCCESS x1"
