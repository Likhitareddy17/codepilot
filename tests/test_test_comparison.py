"""Tests for identity-based run comparison and baseline evidence (TASK-025, TASK-026)."""

import pytest

from codepilot.models import BaselineEvidence, PytestOutcome, StopReason, TestRunResult
from codepilot.state import compare_test_runs
from codepilot.tools.tests import capture_baseline, evaluate_baseline
from conftest import FAILING_TESTS, PASSING_TESTS, make_suite
from codepilot.config import Settings


def run(failing, passed=10, error="E", scope="full", exit_code=None):
    failing = list(failing)
    return TestRunResult(
        exit_code=(1 if failing else 0) if exit_code is None else exit_code,
        passed=passed, failed=len(failing), errors=0, failing_ids=sorted(failing),
        main_error=error if failing else None, scope=scope,
    )


# --------------------------------------------------------------- comparison


def test_progress_without_regression():
    base = run("abcd")
    cmp = compare_test_runs(base, base, run("bcd", passed=11))
    assert cmp.fixed == ["a"] and cmp.newly_failing == [] and cmp.still_failing == ["b", "c", "d"]
    assert cmp.regressed == [] and not cmp.is_regression and not cmp.equal_count_regression
    assert cmp.fixed_since_baseline == ["a"] and cmp.passed_delta_vs_baseline == 1


def test_equal_count_regression_is_detected_by_identity():
    base = run("abcd")
    cmp = compare_test_runs(base, base, run(["b", "c", "d", "x"]))   # fixed a, broke x: still 4 failing
    assert cmp.regressed == ["x"] and cmp.newly_failing == ["x"] and cmp.fixed == ["a"]
    assert cmp.is_regression and cmp.equal_count_regression


def test_fewer_failures_can_still_be_a_regression():
    base = run("abcd")
    cmp = compare_test_runs(base, base, run(["x"], passed=13))
    assert cmp.is_regression and cmp.equal_count_regression
    assert not cmp.all_passing and cmp.fixed_since_baseline == ["a", "b", "c", "d"]


def test_previously_fixed_tests_remain_fixed():
    base = run("abc")
    cmp = compare_test_runs(base, run("bc"), run("b"))
    assert cmp.fixed == ["c"] and cmp.remained_fixed == ["a"]
    assert cmp.fixed_since_baseline == ["a", "c"] and cmp.reopened == []


def test_reopening_a_fixed_test_is_reported_but_is_not_a_baseline_regression():
    base = run("ab")
    cmp = compare_test_runs(base, run("b"), run("ab"))
    assert cmp.newly_failing == ["a"] and cmp.reopened == ["a"]
    assert cmp.regressed == [] and not cmp.is_regression


def test_no_change_between_runs():
    base = run("ab", error="same")
    cmp = compare_test_runs(base, base, run("ab", error="same"))
    assert cmp.same_failing_set and cmp.same_main_error
    assert cmp.newly_failing == [] and cmp.fixed == [] and not cmp.is_regression


def test_main_error_change_is_visible_even_when_ids_match():
    base = run("ab", error="TypeError")
    cmp = compare_test_runs(base, base, run("ab", error="KeyError"))
    assert cmp.same_failing_set and not cmp.same_main_error


def test_passing_count_below_baseline_is_a_regression_even_without_new_ids():
    base = run("ab", passed=10)
    cmp = compare_test_runs(base, base, run("ab", passed=8))
    assert cmp.regressed == [] and cmp.is_regression and cmp.passed_delta_vs_baseline == -2


def test_previous_none_means_baseline():
    base = run("ab")
    assert compare_test_runs(base, None, run("b")).fixed == ["a"]


def test_all_passing_flag():
    base = run("ab")
    assert compare_test_runs(base, base, run([], passed=12)).all_passing
    assert not compare_test_runs(base, base, run([], passed=0)).all_passing


def test_targeted_runs_cannot_be_compared():
    base = run("ab")
    with pytest.raises(ValueError, match="full-suite"):
        compare_test_runs(base, base, run("a", scope="targeted"))
    with pytest.raises(ValueError):
        compare_test_runs(run("a", scope="targeted"), None, base)


def test_comparison_serializes_to_a_dict():
    d = compare_test_runs(run("a"), None, run([], passed=11)).to_dict()
    assert d["fixed"] == ["a"] and d["all_passing"] is True and d["is_regression"] is False


# ------------------------------------------------------------ result model


def test_all_passing_semantics():
    assert TestRunResult(exit_code=0, passed=3, failed=0, errors=0).all_passing
    assert not TestRunResult(exit_code=5, passed=0, failed=0, errors=0).all_passing    # no tests collected
    assert not TestRunResult(exit_code=0, passed=0, failed=0, errors=0, skipped=4).all_passing
    assert not TestRunResult(exit_code=0, passed=3, failed=0, errors=0, timed_out=True).all_passing
    assert not TestRunResult(exit_code=1, passed=3, failed=1, errors=0).all_passing
    assert not TestRunResult(exit_code=2, passed=0, failed=0, errors=1).all_passing


def test_result_dict_includes_the_new_evidence_fields():
    d = TestRunResult(exit_code=1, passed=1, failed=1, errors=0, failing_ids=["t::a"], main_error="boom",
                      failure_messages={"t::a": "boom"}, skipped=2, scope="targeted").to_dict()
    assert d["skipped"] == 2 and d["scope"] == "targeted"
    assert d["main_error"] == "boom" and d["failure_messages"] == {"t::a": "boom"}


# ------------------------------------------------------------- baseline


def outcome(**kwargs) -> PytestOutcome:
    return PytestOutcome(result=TestRunResult(**kwargs))


@pytest.mark.parametrize(
    "kwargs,status",
    [
        (dict(exit_code=0, passed=5, failed=0, errors=0), BaselineEvidence.NO_FAILURES),
        (dict(exit_code=1, passed=3, failed=2, errors=0, failing_ids=["a", "b"], main_error="E"), BaselineEvidence.NEEDS_REPAIR),
        (dict(exit_code=2, passed=0, failed=0, errors=1, failing_ids=["tests/test_x.py"]), BaselineEvidence.NEEDS_REPAIR),
        (dict(exit_code=5, passed=0, failed=0, errors=0), BaselineEvidence.UNUSABLE),
        (dict(exit_code=4, passed=0, failed=0, errors=0, main_error="usage"), BaselineEvidence.UNUSABLE),
        (dict(exit_code=3, passed=0, failed=0, errors=0), BaselineEvidence.UNUSABLE),
        (dict(exit_code=-1, passed=0, failed=0, errors=0, timed_out=True), BaselineEvidence.UNUSABLE),
        (dict(exit_code=0, passed=0, failed=0, errors=0, skipped=3), BaselineEvidence.UNUSABLE),
    ],
)
def test_baseline_classification(kwargs, status):
    assert evaluate_baseline(outcome(**kwargs)).status == status


def test_baseline_evidence_properties():
    passing = evaluate_baseline(outcome(exit_code=0, passed=5, failed=0, errors=0))
    assert passing.stop_reason == StopReason.NO_FAILURES and not passing.needs_repair
    failing = evaluate_baseline(outcome(exit_code=1, passed=3, failed=1, errors=0, failing_ids=["a"], main_error="E"))
    assert failing.needs_repair and failing.stop_reason is None
    assert failing.failing_ids == ["a"] and failing.main_error == "E"
    d = failing.to_dict()
    assert d["status"] == "NEEDS_REPAIR" and d["failing_ids"] == ["a"] and d["result"]["failed"] == 1


def test_capture_baseline_on_a_failing_repo(tmp_path):
    repo = make_suite(tmp_path, test_pass=PASSING_TESTS, test_fail=FAILING_TESTS)
    evidence = capture_baseline(repo, tmp_path / "run", Settings(llm_api_key="x", command_timeout_s=60))
    assert evidence.needs_repair and evidence.result.scope == "full"
    assert evidence.failing_ids == ["tests/test_fail.py::test_bad"]
    assert "3 == 4" in evidence.main_error


def test_capture_baseline_on_a_passing_repo(tmp_path):
    repo = make_suite(tmp_path, test_pass=PASSING_TESTS)
    evidence = capture_baseline(repo, tmp_path / "run", Settings(llm_api_key="x", command_timeout_s=60))
    assert evidence.status == BaselineEvidence.NO_FAILURES and evidence.stop_reason == StopReason.NO_FAILURES


def test_capture_baseline_with_no_tests_is_unusable(tmp_path):
    repo = make_suite(tmp_path, test_empty="x = 1\n")
    evidence = capture_baseline(repo, tmp_path / "run", Settings(llm_api_key="x", command_timeout_s=60))
    assert evidence.status == BaselineEvidence.UNUSABLE and "No tests" in evidence.reason
