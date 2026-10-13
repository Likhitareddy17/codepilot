"""Controller tests (TASK-036 to TASK-047): scripted FakeLLM, real Git workspace.

Most tests use a simulated test suite that reads the workspace files, so they
run without starting pytest; the last tests run the real pytest end to end.
"""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from codepilot.config import Settings
from codepilot.controller import Controller
from codepilot.errors import LLMError, LLMUnavailableError, RateLimitError
from codepilot.llm import fake
from codepilot.llm.fake import FakeLLM, respond, call, say
from codepilot.models import PytestOutcome, StopReason, TestRunResult
from codepilot.workspace import Workspace
from conftest import write

ADD_FIX = ("app/calc.py", "return a - b   # ADD", "return a + b   # ADD")
MUL_FIX = ("app/calc.py", "return a + b   # MUL", "return a * b   # MUL")
SUB_BREAK = ("app/calc.py", "return a - b   # SUB", "return a + b   # SUB")
TEST_ID = {"add": "tests/test_calc.py::test_add", "mul": "tests/test_calc.py::test_mul", "sub": "tests/test_calc.py::test_sub"}
NEEDLE = {"add": "return a + b   # ADD", "mul": "return a * b   # MUL", "sub": "return a - b   # SUB"}


def make_repo(base: Path, bugs=("add", "mul")) -> Path:
    src = base / "canonical"
    add = "a - b" if "add" in bugs else "a + b"
    mul = "a + b" if "mul" in bugs else "a * b"
    write(src / "app" / "__init__.py", "")
    write(src / "app" / "calc.py",
          f"def add(a, b):\n    return {add}   # ADD\n\n\ndef mul(a, b):\n    return {mul}   # MUL\n\n\n"
          "def sub(a, b):\n    return a - b   # SUB\n")
    write(src / "tests" / "test_calc.py",
          "from app.calc import add, mul, sub\n\n\ndef test_add():\n    assert add(2, 3) == 5\n\n\n"
          "def test_mul():\n    assert mul(2, 3) == 6\n\n\ndef test_sub():\n    assert sub(5, 3) == 2\n")
    write(src / "pytest.ini", "[pytest]\n")
    return src


def sim_runner(calls, override=None):
    """A suite simulated from the workspace files: no subprocess."""
    def runner(root, run_dir, settings, target):
        index = len(calls)
        calls.append(target)
        if override is not None:
            forced = override(index)
            if forced is not None:
                return PytestOutcome(result=forced)
        text = (Path(root) / "app" / "calc.py").read_text()
        failing = sorted(TEST_ID[k] for k, needle in NEEDLE.items() if needle not in text)
        messages = {i: f"AssertionError in {i.split('::')[1]}" for i in failing}
        return PytestOutcome(result=TestRunResult(
            exit_code=1 if failing else 0, passed=3 - len(failing), failed=len(failing), errors=0,
            failing_ids=failing, failure_messages=messages,
            main_error=messages[failing[0]] if failing else None, scope="full"))
    return runner


def unusable(exit_code=5):
    return TestRunResult(exit_code=exit_code, passed=0, failed=0, errors=0, scope="full")


@pytest.fixture
def run(tmp_path):
    workspaces = []

    def go(script, bugs=("add", "mul"), runner=None, settings=None, real=False, task=""):
        src = make_repo(tmp_path, bugs)
        workspace = Workspace(str(src), run_id="t1")
        workspace.create()
        workspaces.append(workspace)
        s = settings or Settings(llm_api_key="x", max_iterations=6, command_timeout_s=60)
        calls = []
        llm = script if isinstance(script, FakeLLM) else FakeLLM(script)
        controller = Controller(
            workspace, llm, s, tmp_path / "runs" / "t1", task=task, repository_name="calc_repo",
            suite_runner=None if real else (runner or sim_runner(calls)))
        controller.sim_calls = calls
        return controller.run(), controller, llm, workspace, src

    yield go
    for workspace in workspaces:
        workspace.cleanup()


def file_text(workspace, rel="app/calc.py"):
    return (workspace.temp_dir / rel).read_text()


# ------------------------------------------------------------ baseline


def test_no_failures_baseline_stops_without_calling_the_llm(run):
    llm = FakeLLM([])
    result, controller, _, workspace, _ = run(llm, bugs=())
    assert result.stop_reason == StopReason.NO_FAILURES and llm.call_count == 0
    assert result.commit_hash is None and not workspace.changed_files()
    assert result.state.status == "finished" and result.state.stop_reason == StopReason.NO_FAILURES


@pytest.mark.parametrize("code", [5, 4, 3])
def test_unusable_baseline_maps_to_internal_error(run, code):
    llm = FakeLLM([])
    result, *_ = run(llm, runner=sim_runner([], override=lambda i: unusable(code)))
    assert result.stop_reason == StopReason.INTERNAL_ERROR and "unusable" in result.summary
    assert llm.call_count == 0 and result.commit_hash is None


def test_baseline_is_recorded_pinned_and_given_to_the_model(run):
    result, controller, llm, *_ = run(fake.normal_repair(ADD_FIX), bugs=("add",))
    first_call = llm.calls[0]
    assert first_call.messages[0]["role"] == "system"
    assert TEST_ID["add"] in first_call.messages[1]["content"] and "calc_repo" in first_call.messages[1]["content"]
    assert len(first_call.tools) == 9
    assert controller.history.record("c001").args == {"source": "baseline"}
    assert result.baseline.status == "NEEDS_REPAIR" and result.baseline.failing_ids == [TEST_ID["add"]]


# ------------------------------------------------------------- success


def test_normal_repair_succeeds_with_a_verified_commit(run, tmp_path):
    result, controller, llm, workspace, src = run(fake.normal_repair(ADD_FIX), bugs=("add",))
    assert result.success and result.stop_reason == StopReason.SUCCESS
    assert result.state.final_verification and result.state.commit_state
    assert result.final_tests.all_passing and result.state.failing_ids == []
    assert workspace._run_git(["rev-list", "--count", "HEAD"]) == "2"
    assert workspace._run_git(["rev-parse", "HEAD"]) == result.commit_hash
    assert workspace._run_git(["status", "--porcelain"]) == ""
    assert workspace.changed_files() == ["app/calc.py"] and result.state.changed_files == ["app/calc.py"]
    assert result.state.files_inspected == ["app/calc.py"]
    assert len(result.state.patches) == 1 and result.state.patches[0].applied
    assert llm.call_count == 2                       # investigate, then read+patch; the controller finished
    # baseline, after the patch, and an independent final verification
    assert len(controller.sim_calls) == 3


def test_the_exported_patch_reproduces_the_fix_on_a_fresh_copy(run, tmp_path):
    result, _, _, workspace, src = run(fake.normal_repair(ADD_FIX), bugs=("add",))
    patch = Path(result.patch_path)
    assert patch.is_file() and patch.parent == tmp_path / "runs" / "t1"
    fresh = tmp_path / "fresh"
    shutil.copytree(src, fresh)
    subprocess.run(["git", "-c", "core.autocrlf=false", "apply", str(patch)], cwd=fresh, check=True)
    assert (fresh / "app" / "calc.py").read_bytes() == (workspace.temp_dir / "app" / "calc.py").read_bytes()
    assert "# ADD" in (src / "app" / "calc.py").read_text() and "a - b   # ADD" in (src / "app" / "calc.py").read_text()  # canonical untouched


def test_multiple_patches_across_iterations(run):
    result, controller, llm, workspace, _ = run(fake.multiple_patches([ADD_FIX, MUL_FIX]))
    assert result.success and len(result.state.patches) == 2
    assert result.state.iteration == 3 and llm.call_count == 3
    assert "return a + b   # ADD" in file_text(workspace) and "return a * b   # MUL" in file_text(workspace)


def test_patch_result_carries_the_controllers_full_suite_summary(run):
    _, controller, llm, *_ = run(fake.multiple_patches([ADD_FIX, MUL_FIX]))
    after_first_patch = llm.calls[2].messages            # request for iteration 3
    tool_texts = [m["content"] for m in after_first_patch if m["role"] == "tool"]
    assert any("CONTROLLER FULL-SUITE RESULT AFTER THIS PATCH" in t and "Fixed by this patch" in t for t in tool_texts)


def test_success_requires_a_fresh_final_verification(run):
    # The suite passes after the patch but fails in the independent final run: no commit yet.
    def flaky(index):
        if index == 2:      # 0 baseline, 1 after patch, 2 final verification
            return TestRunResult(exit_code=1, passed=2, failed=1, errors=0, failing_ids=[TEST_ID["sub"]],
                                 failure_messages={TEST_ID["sub"]: "flaky"}, main_error="flaky", scope="full")
        return None

    calls = []
    script = fake.normal_repair(ADD_FIX)[:2] + [say("Done"), say("Done")]
    result, controller, llm, workspace, _ = run(script, bugs=("add",), runner=sim_runner(calls, flaky))
    assert result.success and len(calls) == 4
    verification_messages = [m["content"] for c in llm.calls for m in c.messages if m["role"] == "user" and m["content"].startswith("VERIFICATION FAILED")]
    assert verification_messages and TEST_ID["sub"] in verification_messages[0]
    assert workspace._run_git(["rev-list", "--count", "HEAD"]) == "2"


def test_no_commit_unless_the_audit_passes(run):
    def make_extra_file(messages, tools):
        write(workspace_box[0].temp_dir / "app" / "extra.py", "x = 1\n")
        return respond(call("read_file", path="app/calc.py"), call("apply_patch", path=ADD_FIX[0], old=ADD_FIX[1], new=ADD_FIX[2]))

    workspace_box = []
    original = Workspace.create

    def create_and_remember(self):
        path = original(self)
        workspace_box.append(self)
        return path

    Workspace.create = create_and_remember
    try:
        result, _, _, workspace, _ = run([fake.investigate(), make_extra_file], bugs=("add",))
    finally:
        Workspace.create = original
    assert result.stop_reason == StopReason.INTERNAL_ERROR and "audit failed" in result.summary
    assert "new file created: app/extra.py" in result.summary
    assert result.commit_hash is None and workspace._run_git(["rev-list", "--count", "HEAD"]) == "1"


# -------------------------------------------------------- stop conditions


def test_max_iterations(run):
    settings = Settings(llm_api_key="x", max_iterations=4)
    result, _, llm, workspace, _ = run(fake.max_iterations(4), settings=settings)
    assert result.stop_reason == StopReason.MAX_ITERATIONS and llm.call_count == 4
    assert result.commit_hash is None and not workspace.changed_files()


def test_no_progress_after_two_unchanged_patches(run):
    first = ("app/calc.py", "return a - b   # ADD", "return a - b   # ADD note1")
    second = ("app/calc.py", "note1", "note2")
    result, _, llm, workspace, _ = run(fake.no_progress(first, second))
    assert result.stop_reason == StopReason.NO_PROGRESS and result.commit_hash is None
    assert llm.call_count == 3 and len(result.state.patches) == 2


def test_progress_resets_the_no_progress_counter(run):
    noop = ("app/calc.py", "return a - b   # ADD", "return a - b   # ADD note1")
    script = [fake.investigate(), respond(call("apply_patch", path=noop[0], old=noop[1], new=noop[2])),
              respond(call("apply_patch", path=MUL_FIX[0], old=MUL_FIX[1], new=MUL_FIX[2])),
              respond(call("apply_patch", path=noop[0], old="note1", new="note2")),
              respond(call("apply_patch", path=ADD_FIX[0], old="return a - b   # ADD note2", new=ADD_FIX[2]))]
    result, *_ = run(script, settings=Settings(llm_api_key="x", max_iterations=6, max_patch_attempts_per_file=5))
    assert result.success


def test_regression_is_reverted_reported_and_recoverable(run):
    script = [fake.investigate(), respond(call("apply_patch", path=SUB_BREAK[0], old=SUB_BREAK[1], new=SUB_BREAK[2])),
              respond(call("apply_patch", path=ADD_FIX[0], old=ADD_FIX[1], new=ADD_FIX[2])),
              respond(call("apply_patch", path=MUL_FIX[0], old=MUL_FIX[1], new=MUL_FIX[2]))]
    result, controller, llm, workspace, _ = run(script)
    assert result.success
    assert "return a - b   # SUB" in file_text(workspace)          # the bad patch stayed reverted
    assert any(e["event"] == "regression" and e["reverted"] == ["app/calc.py"] for e in result.events)
    user_after = [m["content"] for m in llm.calls[2].messages if m["role"] == "user"]
    assert any(m.startswith("REGRESSION DETECTED") and TEST_ID["sub"] in m for m in user_after)
    tool_texts = [m["content"] for m in llm.calls[2].messages if m["role"] == "tool"]
    assert any("reverted this patch" in t for t in tool_texts)
    assert result.state.regression_state is True


def test_a_second_regression_stops_the_run_with_everything_reverted(run):
    second = ("app/calc.py", "return a - b   # ADD", "return a - b   # ADD x")
    script = fake.repeated_regression(SUB_BREAK, SUB_BREAK)
    # first bad patch is reverted, so the identical second one applies cleanly again
    result, _, llm, workspace, src = run(script)
    assert result.stop_reason == StopReason.REPEATED_REGRESSION and result.commit_hash is None
    assert file_text(workspace) == (src / "app" / "calc.py").read_text()
    assert not workspace.changed_files()


def test_an_equal_count_regression_is_still_a_regression(run):
    # Fixes add but breaks sub: two tests fail before and after, with different identities.
    script = [fake.investigate(), respond(
        call("apply_patch", path=ADD_FIX[0], old=ADD_FIX[1], new=ADD_FIX[2]),
        call("apply_patch", path=SUB_BREAK[0], old=SUB_BREAK[1], new=SUB_BREAK[2])), say("done")]
    result, controller, llm, workspace, src = run(script, settings=Settings(llm_api_key="x", max_iterations=3))
    event = next(e for e in result.events if e["event"] == "regression")
    assert event["regressed"] == [TEST_ID["sub"]] and sorted(event["reverted"]) == ["app/calc.py", "app/calc.py"]
    assert file_text(workspace) == (src / "app" / "calc.py").read_text()
    assert result.commit_hash is None


def test_protected_path_attempts_are_blocked_then_stop_the_run(run):
    result, _, llm, workspace, src = run(fake.protected_path_attack("tests/test_calc.py", "== 5", "== -1") * 1 + [respond(call("apply_patch", path="tests/test_calc.py", old="== 6", new="== 5"))])
    assert result.stop_reason == StopReason.PROTECTED_PATH_VIOLATION and result.commit_hash is None
    assert (workspace.temp_dir / "tests" / "test_calc.py").read_text() == (src / "tests" / "test_calc.py").read_text()
    assert sum(1 for e in result.events if e["event"] == "protected_path_attempt") == 2


def test_a_single_protected_attempt_is_only_an_error_for_the_model(run):
    script = fake.protected_path_attack("tests/test_calc.py", "== 5", "== -1")[:2] + [respond(call("apply_patch", path=ADD_FIX[0], old=ADD_FIX[1], new=ADD_FIX[2])),
              respond(call("apply_patch", path=MUL_FIX[0], old=MUL_FIX[1], new=MUL_FIX[2]))]
    result, _, llm, *_ = run(script)
    assert result.success
    tool_texts = [m["content"] for m in llm.calls[2].messages if m["role"] == "tool"]
    assert any("PATH_PROTECTED" in t for t in tool_texts)


def test_repeated_malformed_tool_calls_stop_the_run(run):
    result, _, llm, *_ = run(fake.malformed_tool_calls(3))
    assert result.stop_reason == StopReason.BAD_MODEL_OUTPUT and llm.call_count == 3
    tool_texts = [m["content"] for m in llm.calls[1].messages if m["role"] == "tool"]
    assert tool_texts and "INVALID_ARGUMENTS" in tool_texts[0]
    assistant = [m for m in llm.calls[1].messages if m["role"] == "assistant"][0]
    assert assistant["tool_calls"][0]["function"]["arguments"] == '{"path": "app/x.py"'


def test_a_valid_call_resets_the_bad_output_streak(run):
    script = [fake.malformed(), fake.malformed(), fake.investigate(), fake.malformed(), fake.malformed(),
              respond(call("apply_patch", path=ADD_FIX[0], old=ADD_FIX[1], new=ADD_FIX[2])),
              respond(call("apply_patch", path=MUL_FIX[0], old=MUL_FIX[1], new=MUL_FIX[2]))]
    result, *_ = run(script, settings=Settings(llm_api_key="x", max_iterations=8))
    assert result.success


def test_unknown_tools_are_errors_and_repeated_ones_stop_the_run(run):
    result, _, llm, *_ = run(fake.unknown_tool(), settings=Settings(llm_api_key="x", max_iterations=2))
    assert result.stop_reason == StopReason.MAX_ITERATIONS        # a single unknown tool is just an error
    tool_texts = [m["content"] for m in llm.calls[1].messages if m["role"] == "tool"]
    assert "UNKNOWN_TOOL" in tool_texts[0]
    result, *_ = run([respond(call("run_shell", command="x")) for _ in range(3)])
    assert result.stop_reason == StopReason.BAD_MODEL_OUTPUT


def test_false_success_claims_are_not_believed(run):
    settings = Settings(llm_api_key="x", max_iterations=2)
    result, _, llm, workspace, _ = run(fake.false_success(), settings=settings)
    assert result.stop_reason == StopReason.MAX_ITERATIONS and result.commit_hash is None
    assert not workspace.changed_files()
    corrections = [m["content"] for m in llm.calls[1].messages if m["role"] == "user" and m["content"].startswith("VERIFICATION FAILED")]
    assert corrections and "does NOT pass" in corrections[0]


def test_repeated_false_claims_become_bad_model_output(run):
    result, *_ = run([say("fixed!")] * 3)
    assert result.stop_reason == StopReason.BAD_MODEL_OUTPUT


def test_a_claim_after_an_incomplete_patch_triggers_a_real_verification_run(run):
    script = [fake.investigate(), respond(call("apply_patch", path=ADD_FIX[0], old=ADD_FIX[1], new=ADD_FIX[2])), say("All done."), say("Still done.")]
    result, controller, llm, workspace, _ = run(script, settings=Settings(llm_api_key="x", max_iterations=4))
    assert result.stop_reason == StopReason.MAX_ITERATIONS and result.commit_hash is None
    assert len(controller.sim_calls) == 4        # baseline, after patch, verification x2
    assert any(m["content"].startswith("VERIFICATION FAILED") for m in llm.calls[3].messages if m["role"] == "user")


@pytest.mark.parametrize("error,reason", [
    (LLMUnavailableError("down"), StopReason.LLM_UNAVAILABLE),
    (RateLimitError("quota", retry_after=3600), StopReason.LLM_UNAVAILABLE),
    (LLMError("bad request"), StopReason.INTERNAL_ERROR),
])
def test_llm_failures(run, error, reason):
    result, _, llm, workspace, _ = run([fake.investigate(), error])
    assert result.stop_reason == reason and result.commit_hash is None
    assert llm.call_count == 2 and not workspace.changed_files()


def test_too_many_tool_calls_in_one_turn(run):
    script = [respond(*[call("list_files") for _ in range(11)])]
    result, controller, *_ = run(script)
    assert result.stop_reason == StopReason.TOOL_LIMIT_EXCEEDED
    assert [r.tool for r in controller.history.records] == ["run_tests"]     # nothing was executed


def test_unexpected_errors_become_internal_error(run):
    class Boom(FakeLLM):
        def chat(self, messages, tools):
            raise RuntimeError("boom")

    result, *_ = run(Boom([]))
    assert result.stop_reason == StopReason.INTERNAL_ERROR and "RuntimeError" in result.summary


# ----------------------------------------------------- history and state


def test_the_model_can_retrieve_the_baseline_result(run):
    script = [respond(call("get_tool_result", call_id="c001")), say("ok")]
    result, controller, llm, *_ = run(script, settings=Settings(llm_api_key="x", max_iterations=1))
    assert result.state.retrieval_count == 1
    record = controller.history.record("c002")
    assert record.ok and record.summary["key_facts"] == {"call_id": "c001", "tool": "run_tests"}


def test_old_results_are_compressed_in_later_requests(run):
    script = [respond(call("list_files")) for _ in range(5)]
    result, controller, llm, *_ = run(script, settings=Settings(llm_api_key="x", max_iterations=5))
    last = [m["content"] for m in llm.calls[-1].messages if m["role"] == "tool"]
    assert len(last) == 4
    assert "key_facts" in last[0] and "key_facts" not in last[-1]       # oldest summarised, newest full


def test_run_state_and_events_are_serialisable(run):
    result, *_ = run(fake.normal_repair(ADD_FIX), bugs=("add",))
    data = json.loads(json.dumps(result.state.to_dict()))
    assert data["stop_reason"] == "SUCCESS" and data["status"] == "finished" and data["commit_state"] is True
    kinds = [e["event"] for e in result.events]
    assert kinds[0] == "iteration_start" or kinds[0] == "baseline"
    for expected in ("baseline", "llm_response", "tool_call", "tests_after_patch", "final_verification", "audit", "commit", "stop"):
        assert expected in kinds
    json.dumps(result.events)


def test_the_canonical_repository_is_never_modified(run, tmp_path):
    result, _, _, workspace, src = run(fake.normal_repair(ADD_FIX), bugs=("add",))
    assert result.success
    assert "return a - b   # ADD" in (src / "app" / "calc.py").read_text()


def test_the_controller_requires_a_created_workspace(tmp_path):
    workspace = Workspace(str(make_repo(tmp_path)), run_id="x")
    with pytest.raises(ValueError, match="created"):
        Controller(workspace, FakeLLM([]), Settings(llm_api_key="x"), tmp_path / "runs")


# ------------------------------------------------------- real pytest end to end


def test_end_to_end_with_real_pytest(run, tmp_path):
    script = fake.multiple_patches([ADD_FIX, MUL_FIX])
    result, controller, llm, workspace, src = run(script, real=True, settings=Settings(llm_api_key="x", max_iterations=6, command_timeout_s=120))
    assert result.success, result.summary
    assert result.baseline.failing_ids == [TEST_ID["add"], TEST_ID["mul"]]
    assert result.final_tests.passed == 3 and result.final_tests.failed == 0
    # Prove it independently: the exported patch fixes a fresh copy of the repository.
    fresh = tmp_path / "fresh"
    shutil.copytree(src, fresh)
    subprocess.run(["git", "-c", "core.autocrlf=false", "apply", result.patch_path], cwd=fresh, check=True)
    verify = subprocess.run([__import__("sys").executable, "-m", "pytest", "-q", "-p", "no:cacheprovider"], cwd=fresh, capture_output=True, text=True)
    assert verify.returncode == 0, verify.stdout


def test_end_to_end_real_pytest_detects_a_regression(run, tmp_path):
    script = [fake.investigate(), respond(call("apply_patch", path=SUB_BREAK[0], old=SUB_BREAK[1], new=SUB_BREAK[2])), say("done")]
    result, controller, llm, workspace, src = run(script, real=True, settings=Settings(llm_api_key="x", max_iterations=2, command_timeout_s=120))
    event = next(e for e in result.events if e["event"] == "regression")
    assert event["regressed"] == [TEST_ID["sub"]]
    assert file_text(workspace) == (src / "app" / "calc.py").read_text()
    assert result.stop_reason == StopReason.MAX_ITERATIONS and result.commit_hash is None
