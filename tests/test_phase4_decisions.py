"""Pins the Phase 4 decisions: UNUSABLE baselines, the 60 s test timeout, required run_dir."""

import pytest

from codepilot.config import Settings
from codepilot.models import BaselineEvidence, PytestOutcome, StopReason, TestRunResult
from codepilot.state import compare_test_runs
from codepilot.tools import ToolContext
from codepilot.tools.registry import build_default_registry
from codepilot.tools.tests import capture_baseline, evaluate_baseline
from conftest import PASSING_TESTS, make_suite


def settings(timeout: int = 60) -> Settings:
    return Settings(llm_api_key="x", command_timeout_s=timeout, max_tool_output_chars=4000)


def tree(root):
    return sorted(p.relative_to(root).as_posix() for p in root.rglob("*"))


# ---- 1. UNUSABLE is its own status; zero tests and collection errors are never success


def test_zero_collected_tests_baseline_is_unusable_not_success(tmp_path):
    repo = make_suite(tmp_path, test_empty="x = 1\n")
    evidence = capture_baseline(repo, tmp_path / "run", settings())
    assert evidence.status == BaselineEvidence.UNUSABLE
    assert evidence.stop_reason is None          # never NO_FAILURES: the controller must map it
    assert not evidence.needs_repair
    assert not evidence.result.all_passing


def test_collection_error_baseline_needs_repair_and_is_not_success(tmp_path):
    repo = make_suite(tmp_path, test_broken="import module_that_does_not_exist\n\n\ndef test_x():\n    pass\n")
    evidence = capture_baseline(repo, tmp_path / "run", settings())
    assert evidence.status == BaselineEvidence.NEEDS_REPAIR
    assert evidence.stop_reason is None
    assert evidence.failing_ids == ["tests/test_broken.py"]
    assert not evidence.result.all_passing


@pytest.mark.parametrize(
    "kwargs",
    [
        dict(exit_code=5, passed=0, failed=0, errors=0),                      # nothing collected
        dict(exit_code=0, passed=0, failed=0, errors=0, skipped=2),           # everything skipped
        dict(exit_code=2, passed=0, failed=0, errors=1, failing_ids=["tests/test_x.py"]),  # collection error
        dict(exit_code=-1, passed=0, failed=0, errors=0, timed_out=True),     # timeout
        dict(exit_code=4, passed=0, failed=0, errors=0),                      # pytest usage error
    ],
)
def test_only_a_clean_run_with_passing_tests_is_ever_no_failures(kwargs):
    evidence = evaluate_baseline(PytestOutcome(result=TestRunResult(**kwargs)))
    assert evidence.status != BaselineEvidence.NO_FAILURES
    assert evidence.stop_reason != StopReason.NO_FAILURES


def test_zero_tests_and_collection_errors_are_never_a_passing_verification():
    baseline = TestRunResult(exit_code=1, passed=5, failed=1, errors=0, failing_ids=["tests/test_a.py::test_x"])
    zero = TestRunResult(exit_code=5, passed=0, failed=0, errors=0)
    collection = TestRunResult(exit_code=2, passed=0, failed=0, errors=1, failing_ids=["tests/test_a.py"])
    assert not compare_test_runs(baseline, baseline, zero).all_passing
    after_collection_error = compare_test_runs(baseline, baseline, collection)
    assert not after_collection_error.all_passing
    assert after_collection_error.is_regression       # the broken module's tests no longer pass


# ---- 2. The test timeout is command_timeout_s, default 60


def test_default_test_timeout_is_sixty_seconds(monkeypatch):
    monkeypatch.delenv("COMMAND_TIMEOUT_SECONDS", raising=False)
    assert Settings(llm_api_key="x").command_timeout_s == 60


def test_the_configured_timeout_is_the_one_used_for_test_runs(tmp_path):
    repo = make_suite(tmp_path, test_slow="import time\n\n\ndef test_slow():\n    time.sleep(120)\n")
    ctx = ToolContext(root=repo, settings=settings(timeout=2), run_dir=tmp_path / "runs" / "t1")
    result = build_default_registry(ctx).execute("run_tests", {})
    assert result.ok and result.data["timed_out"] is True
    assert "timed out after 2s" in result.data["main_error"]


# ---- 3. run_dir is required, and must be outside the workspace


def test_run_tests_without_a_run_dir_fails_safely_and_touches_nothing(tmp_path):
    repo = make_suite(tmp_path, test_pass=PASSING_TESTS)
    before = tree(repo)
    ctx = ToolContext(root=repo, settings=settings())            # run_dir not supplied
    result = build_default_registry(ctx).execute("run_tests", {})
    assert not result.ok and result.error["code"] == "INTERNAL_ERROR"
    assert "run directory" in result.error["message"]
    assert tree(repo) == before                                   # pytest was never started


def test_run_dir_inside_the_workspace_is_refused_and_touches_nothing(tmp_path):
    repo = make_suite(tmp_path, test_pass=PASSING_TESTS)
    before = tree(repo)
    ctx = ToolContext(root=repo, settings=settings(), run_dir=repo / "runs" / "t1")
    registry = build_default_registry(ctx)
    result = registry.execute("run_tests", {})
    assert not result.ok and result.error["code"] == "INTERNAL_ERROR"
    assert "outside the workspace" in registry.internal_errors[0]
    assert tree(repo) == before


def test_run_dir_outside_the_workspace_receives_the_junit_file(tmp_path):
    repo = make_suite(tmp_path, test_pass=PASSING_TESTS)
    run_dir = tmp_path / "runs" / "t1"
    result = build_default_registry(ToolContext(root=repo, settings=settings(), run_dir=run_dir)).execute("run_tests", {})
    assert result.ok and result.data["all_passing"] is True
    assert (run_dir / "junit.xml").is_file()
    assert not (repo / "junit.xml").exists()
