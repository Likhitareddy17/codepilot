"""Report tests (TASK-049, TASK-050): honest outcome, labelled evidence, redaction."""

from codepilot.models import StopReason
from codepilot.report import before_after, build_final_tests, build_report, headline, write_outputs
from codepilot.trace import RunInfo
from support_results import ADD, make_result

KEY = "gsk_TESTKEY1234567890abcdef"
INFO = RunInfo(repository="calc_repo", model="m", started_at="t0", finished_at="t1", max_iterations=6,
               cleanup="deleted")


def test_success_report_states_the_before_and_after_counts():
    result = make_result()
    assert before_after(result) == "2/4 -> 4/4 passing in 2 iteration(s)"
    report = build_report(result, INFO)
    assert "**Result:** SUCCESS" in report and "2/4 -> 4/4 passing" in report
    assert "```diff" in report and "+    return a + b" in report
    assert "a1b2c3d4e5f6" in report and "codepilot/fix-20261015-101500" in report


def test_report_separates_observed_inferred_and_verified():
    report = build_report(make_result(), INFO)
    assert "**Observed:**" in report
    assert "**Inferred (the model's own words, not verified by CodePilot):** The add function subtracted" in report
    assert "**Verified:** only that the full test suite passes" in report
    assert f"`{ADD}`: AssertionError in test_add" in report


def test_missing_model_explanation_is_stated():
    assert "the model gave no explanation" in build_report(make_result(model_text=None), INFO)


def test_failed_run_never_claims_success():
    result = make_result(StopReason.MAX_ITERATIONS, verified=False)
    report = build_report(result, INFO)
    assert headline(result).startswith("NOT FIXED") and "SUCCESS" not in report.split("##")[0]
    assert "**Verified:** nothing" in report and "None of these changes were committed." in report
    assert "Still failing" in report and "`tests/test_calc.py::test_mul`" in report
    assert "fix.patch" not in report.split("## Artifacts")[1]


def test_success_stop_reason_without_every_condition_is_flagged():
    result = make_result()
    result.state.commit_state = False
    assert headline(result).startswith("NOT FIXED") and "defect" in headline(result)
    assert "SUCCESS" not in build_report(result, INFO).split("\n")[2]


def test_baseline_already_passing_is_not_reported_as_a_fix():
    result = make_result(StopReason.NO_FAILURES, verified=False)
    assert headline(result).startswith("NO REPAIR NEEDED")
    assert "Nothing: no repair was needed." in build_report(result, INFO)


def test_reverted_patch_is_labelled():
    assert "applied, then reverted by the controller" in build_report(make_result(reverted=True), INFO)


def test_evidence_section_lists_what_the_model_inspected():
    report = build_report(make_result(), INFO)
    assert "list_files x1" in report and "read_file x1" in report and "apply_patch x1" in report
    assert "Files read: `app/calc.py`" in report and "run_tests" not in report.split("## Evidence")[1].split("## Root")[0]


def test_workspace_status_in_artifacts():
    assert "deleted after the run" in build_report(make_result(), INFO)
    kept = RunInfo("r", "m", "a", "b", 6, cleanup="kept", workspace_kept="/tmp/ws")
    assert "kept at `/tmp/ws`" in build_report(make_result(), kept)


def test_final_tests_text():
    text = build_final_tests(make_result())
    assert "passed: 4 | failed: 0" in text and "failing tests: none" in text
    assert build_final_tests(make_result(StopReason.NO_FAILURES, verified=False)) is not None


def test_write_outputs_writes_three_redacted_files(tmp_path):
    result = make_result(model_text=f"leaked {KEY}")
    errors = write_outputs(tmp_path / "run", result, INFO, secrets=[KEY])
    assert errors == {}
    names = sorted(p.name for p in (tmp_path / "run").iterdir())
    assert names == ["final_tests.txt", "report.md", "run_trace.json"]
    for path in (tmp_path / "run").iterdir():
        assert KEY not in path.read_text(encoding="utf-8")


def test_write_outputs_reports_a_failure_without_hiding_the_others(tmp_path, monkeypatch):
    def boom(*_args, **_kwargs):
        raise OSError("disk full")
    monkeypatch.setattr("codepilot.report.write_trace", boom)
    errors = write_outputs(tmp_path / "run", make_result(), INFO)
    assert list(errors) == ["run_trace.json"] and "disk full" in errors["run_trace.json"]
    assert (tmp_path / "run" / "report.md").is_file()
