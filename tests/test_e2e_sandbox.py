"""Offline end-to-end scenarios on the real sandbox (TASK-066 to TASK-073).

Each test runs the real CLI (``cli.main``) on a copy of the canonical sandbox with a scripted
FakeLLM and real pytest. Patches come from the sandbox manifest, so a scenario cannot drift away
from the sandbox it exercises. Scenarios that only need controller logic on a tiny repository
(LLM failures, malformed calls, protected paths, audit gate) live in test_controller.py.
"""

import json

import pytest

from codepilot import cli
from codepilot.llm import fake
from codepilot.llm.fake import FALSE_SUCCESS_TEXT, FakeLLM, call, respond, say
from support_policy import FILES, ReadFirstModel, visible_files
from support_sandbox import SANDBOX, apply_edit, copy_sandbox, load_manifest, sandbox_files, tree_hash

pytestmark = pytest.mark.slow


def manifest_patch(edit):
    return (edit["path"], edit["old"], edit["new"])


@pytest.fixture
def manifest():
    return load_manifest()


@pytest.fixture
def fixes(manifest):
    """The four reference fixes, in bug order, as (path, old, new)."""
    return [manifest_patch(manifest["bugs"][bug]["fix"]) for bug in sorted(manifest["bugs"])]


def run_cli(env, repo, script, *extra):
    """Run the CLI on ``repo``; return (exit code, trace, run dir)."""
    code = cli.main(["--repo", str(repo), *extra], llm=FakeLLM(script))
    runs = list(env.runs.iterdir())
    assert len(runs) == 1, "expected exactly one run folder"
    return code, json.loads((runs[0] / "run_trace.json").read_text(encoding="utf-8")), runs[0]


def event_kinds(trace):
    return [e["event"] for e in trace["events"]]


def assert_clean(env, trace, before):
    assert tree_hash(SANDBOX) == before, "the canonical sandbox was modified"
    assert not list(env.scratch.iterdir()), "the temporary workspace was not deleted"
    assert trace["outcome"]["committed"] is False or trace["outcome"]["success"], "an unverified commit exists"


# -------------------------------------------------------------- TASK-066


def test_baseline_pass_makes_no_patch_and_no_llm_call(offline_cli_env, tmp_path, manifest, fixes):
    repo = copy_sandbox(tmp_path / "already_fixed")
    for path, old, new in fixes:
        apply_edit(repo, {"path": path, "old": old, "new": new})
    llm = FakeLLM([])
    code = cli.main(["--repo", str(repo)], llm=llm)
    run = next(offline_cli_env.runs.iterdir())
    trace = json.loads((run / "run_trace.json").read_text(encoding="utf-8"))
    assert code == 0 and llm.call_count == 0
    assert trace["outcome"]["stop_reason"] == "NO_FAILURES" and trace["patches"] == []
    assert trace["outcome"]["commit"] is None and not (run / "fix.patch").exists()


# -------------------------------------------------------------- TASK-067


def test_budget_exhausted_with_partial_progress_stops_with_max_iterations(offline_cli_env, fixes, manifest):
    before = tree_hash(SANDBOX)
    noop = manifest_patch(manifest["noop_patches"][0])
    script = [fake.investigate(),
              respond(call("read_file", path=fixes[0][0]), call("apply_patch", path=fixes[0][0], old=fixes[0][1], new=fixes[0][2])),
              respond(call("apply_patch", path=fixes[2][0], old=fixes[2][1], new=fixes[2][2])),
              respond(call("apply_patch", path=noop[0], old=noop[1], new=noop[2])),
              fake.investigate()]
    code, trace, run = run_cli(offline_cli_env, SANDBOX, script, "--max-iterations", "5")
    assert code == 1 and trace["outcome"]["stop_reason"] == "MAX_ITERATIONS" and trace["outcome"]["iterations"] == 5
    assert trace["final_tests"]["failed"] == 2 and not trace["outcome"]["success"]
    assert trace["outcome"]["commit"] is None and not (run / "fix.patch").exists()
    assert_clean(offline_cli_env, trace, before)


# -------------------------------------------------------------- TASK-068


def test_two_unchanged_patches_stop_with_no_progress(offline_cli_env, manifest):
    before = tree_hash(SANDBOX)
    first, second = (manifest_patch(p) for p in manifest["noop_patches"])
    code, trace, run = run_cli(offline_cli_env, SANDBOX, fake.no_progress(first, second))
    base = manifest["baseline"]
    assert code == 1 and trace["outcome"]["stop_reason"] == "NO_PROGRESS"
    assert trace["final_tests"]["failing_ids"] == sorted(base["failing_ids"])
    assert len(trace["patches"]) == 2 and trace["outcome"]["commit"] is None
    assert_clean(offline_cli_env, trace, before)


# -------------------------------------------------------------- TASK-069


def test_regression_is_reverted_and_the_run_recovers_to_a_full_pass(offline_cli_env, tmp_path, manifest, fixes):
    before = tree_hash(SANDBOX)
    bad = manifest_patch(manifest["regression_patch"])
    script = fake.regression(bad, then=fixes[0])
    script += [respond(call("apply_patch", path=p, old=o, new=n)) for p, o, n in fixes[1:]] + [say("done")]
    code, trace, run = run_cli(offline_cli_env, SANDBOX, script)
    assert code == 0 and trace["outcome"]["success"] and trace["outcome"]["regression_seen"] is True
    assert "regression" in event_kinds(trace)
    assert trace["patches"][0]["reverted"] is True and trace["patches"][0]["path"] == manifest["regression_patch"]["path"]
    assert trace["final_tests"]["failed"] == 0 and trace["final_tests"]["passed"] == manifest["baseline"]["total"]
    diff = (run / "fix.patch").read_text(encoding="utf-8")
    assert "_ORDERS.get(str(order_id))" not in diff, "the reverted regression leaked into the final patch"
    assert_clean(offline_cli_env, trace, before)


# -------------------------------------------------------------- TASK-070


def test_two_regressions_stop_the_run_with_everything_reverted(offline_cli_env, manifest):
    before = tree_hash(SANDBOX)
    bad = manifest_patch(manifest["regression_patch"])
    again = bad                                                    # the revert restored the file, so it applies again
    script = [fake.investigate(), respond(call("apply_patch", path=bad[0], old=bad[1], new=bad[2])),
              respond(call("apply_patch", path=again[0], old=again[1], new=again[2])), say("done")]
    code, trace, run = run_cli(offline_cli_env, SANDBOX, script)
    assert code == 1 and trace["outcome"]["stop_reason"] == "REPEATED_REGRESSION"
    assert [p["reverted"] for p in trace["patches"]] == [True, True]
    assert trace["outcome"]["commit"] is None and trace["outcome"]["changed_files"] == []
    assert_clean(offline_cli_env, trace, before)


# -------------------------------------------------------------- TASK-071


def test_equal_failure_count_with_different_ids_is_a_regression(offline_cli_env, manifest, fixes):
    before = tree_hash(SANDBOX)
    swap = manifest["equal_count_regression_patch"]
    script = [fake.investigate(), respond(call("apply_patch", path=swap["path"], old=swap["old"], new=swap["new"])),
              respond(call("apply_patch", path=fixes[0][0], old=fixes[0][1], new=fixes[0][2]))]
    code, trace, run = run_cli(offline_cli_env, SANDBOX, script, "--max-iterations", "3")
    events = {e["event"]: e for e in trace["events"] if e["iteration"] == 2}
    assert "regression" in events, "swapping one failure for another must be a regression"
    assert events["tests_after_patch"]["failed"] == manifest["baseline"]["failed"], "the failing count did not change"
    assert sorted(events["regression"]["regressed"]) == sorted(swap["newly_failing_ids"])
    assert swap["path"] in events["regression"]["reverted"] and trace["patches"][0]["reverted"] is True
    assert code == 1 and trace["outcome"]["stop_reason"] == "MAX_ITERATIONS"
    assert_clean(offline_cli_env, trace, before)


# -------------------------------------------------------------- TASK-072


def test_a_claim_of_success_is_checked_and_rejected_while_tests_fail(offline_cli_env, manifest):
    before = tree_hash(SANDBOX)
    claims = [say(FALSE_SUCCESS_TEXT) for _ in range(3)]          # three invalid replies in a row stop the run
    code, trace, run = run_cli(offline_cli_env, SANDBOX, claims)
    assert code == 1 and not trace["outcome"]["success"] and trace["outcome"]["commit"] is None
    assert "completion_claim" in event_kinds(trace)
    assert trace["final_tests"]["failing_ids"] == sorted(manifest["baseline"]["failing_ids"])
    assert trace["outcome"]["stop_reason"] == "BAD_MODEL_OUTPUT"
    assert_clean(offline_cli_env, trace, before)


# -------------------------------------------------------------- TASK-073


def test_a_passing_targeted_test_is_not_success(offline_cli_env, manifest, fixes):
    before = tree_hash(SANDBOX)
    repaired = manifest["bugs"]["BUG-01"]["failing_ids"][0]
    script = [fake.investigate(),
              respond(call("read_file", path=fixes[0][0]), call("apply_patch", path=fixes[0][0], old=fixes[0][1], new=fixes[0][2])),
              respond(call("run_tests", target=repaired)),
              say(FALSE_SUCCESS_TEXT), say(FALSE_SUCCESS_TEXT)]
    code, trace, run = run_cli(offline_cli_env, SANDBOX, script, "--max-iterations", "5")
    targeted = [c for c in trace["tool_calls"] if c["tool"] == "run_tests" and c["source"] == "model"]
    assert targeted and targeted[0]["ok"], "the targeted run passed"
    assert code == 1 and not trace["outcome"]["success"] and trace["outcome"]["commit"] is None
    assert trace["outcome"]["stop_reason"] == "MAX_ITERATIONS" and event_kinds(trace).count("completion_claim") == 2
    assert trace["final_tests"]["failed"] == manifest["baseline"]["failed"] - 1       # three bugs still unfixed
    assert repaired not in trace["final_tests"]["failing_ids"]
    assert_clean(offline_cli_env, trace, before)


def test_the_sandbox_files_are_untouched_by_all_of_the_above(manifest):
    assert sandbox_files(SANDBOX) == sorted(manifest["files"])


# ------------------------------------------------ file-read retention (ADR-010 amendment)


def test_six_files_read_one_per_reply_are_all_visible_to_the_model(offline_cli_env, manifest):
    """Live run 20261018-111755 re-read files because only the last three results were visible."""
    script = [fake.investigate()] + [respond(call("read_file", path=p, start_line=1, end_line=400)) for p in FILES] + [fake.investigate()]
    llm = FakeLLM(script)
    cli.main(["--repo", str(SANDBOX), "--max-iterations", str(len(script))], llm=llm)
    assert visible_files(llm.calls[-1].messages) >= set(FILES), "file contents fell out of the model's context"


def test_a_patched_file_is_no_longer_shown_with_its_stale_contents(offline_cli_env, manifest):
    fix = manifest["bugs"]["BUG-02"]["fix"]
    script = [respond(call("read_file", path="app/orders.py", start_line=1, end_line=400)),
              respond(call("read_file", path="app/pagination.py", start_line=1, end_line=400)),
              respond(call("apply_patch", path=fix["path"], old=fix["old"], new=fix["new"])),
              fake.investigate(),
              respond(call("read_file", path="app/orders.py", start_line=1, end_line=400)),
              fake.investigate()]
    llm = FakeLLM(script)
    cli.main(["--repo", str(SANDBOX), "--max-iterations", str(len(script))], llm=llm)
    after_patch = visible_files(llm.calls[3].messages)            # request after the patch result is in the history
    assert "app/pagination.py" in after_patch and "app/orders.py" not in after_patch
    assert "app/orders.py" in visible_files(llm.calls[5].messages), "the re-read must be visible again"


def test_a_read_everything_first_model_can_finish_within_the_default_budget(offline_cli_env, manifest):
    """Simulator evidence only: it shows the history no longer makes the repair impossible, not that a live model succeeds."""
    model = ReadFirstModel()
    code = cli.main(["--repo", str(SANDBOX)], llm=model)                  # default limits: 15 iterations, window of 3
    run = next(offline_cli_env.runs.iterdir())
    trace = json.loads((run / "run_trace.json").read_text(encoding="utf-8"))
    assert code == 0 and trace["outcome"]["success"], model.log
    assert trace["outcome"]["iterations"] <= 14, model.log
    # The only legitimate re-read is app/orders.py after its own patch: BUG-04 needs the fresh text.
    assert [step for step in model.log if step.startswith("re-read")] == ["re-read app/orders.py"], model.log
