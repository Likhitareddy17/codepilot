"""CLI tests (TASK-003, TASK-051 to TASK-054): arguments, startup errors, exit codes, outputs.

Startup-error tests are fast. Tests that create a Git workspace are marked slow.
The LLM is always a FakeLLM; nothing here needs a Groq key or network access.
"""

import hashlib
import io
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from codepilot import cli
from codepilot.errors import GitError
from codepilot.llm import fake
from codepilot.llm.fake import FakeLLM, call, respond
from codepilot.models import StopReason
from codepilot.trace import RunInfo
from codepilot.workspace import _handle_remove_error
from support_results import make_result
from test_controller import ADD_FIX, make_repo, sim_runner

KEY = "gsk_TESTKEY1234567890abcdef"
ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def cli_env(tmp_path, monkeypatch):
    """Runs go to tmp_path/runs, temp workspaces to tmp_path/ws_tmp; a fake key is set."""
    runs, scratch = tmp_path / "runs", tmp_path / "ws_tmp"
    scratch.mkdir()
    monkeypatch.setenv("RUNS_DIR", str(runs))
    monkeypatch.setenv("GROQ_API_KEY", KEY)
    for name in ("LLM_API_KEY", "MAX_ITERATIONS", "KEEP_WORKSPACE"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(tempfile, "tempdir", str(scratch))
    return SimpleNamespace(runs=runs, scratch=scratch)


def tree_hash(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(p for p in root.rglob("*") if p.is_file()):
        digest.update(path.relative_to(root).as_posix().encode() + path.read_bytes())
    return digest.hexdigest()


def only_run(env) -> Path:
    runs = list(env.runs.iterdir())
    assert len(runs) == 1
    return runs[0]


def read_all(directory: Path) -> str:
    return "\n".join(p.read_text(encoding="utf-8", errors="replace") for p in directory.rglob("*") if p.is_file())


# ------------------------------------------------------------- arguments


def test_parse_args_defaults():
    args = cli.parse_args([])
    assert args.repo == "." and args.task == "Fix the failing tests"
    assert args.max_iterations is None and args.keep_workspace is False


def test_parse_args_custom():
    args = cli.parse_args(["--repo", "sandbox_repo", "--task", "Fix bug in parser", "--max-iterations", "4",
                           "--keep-workspace"])
    assert (args.repo, args.task, args.max_iterations, args.keep_workspace) == (
        "sandbox_repo", "Fix bug in parser", 4, True)


def test_help_exits_zero(capsys):
    assert cli.main(["--help"]) == 0
    assert "--keep-workspace" in capsys.readouterr().out


def test_usage_error_exits_two(capsys):
    assert cli.main(["--no-such-option"]) == 2
    assert "usage" in capsys.readouterr().err.lower()


# ---------------------------------------------------------- startup errors


def assert_clean_startup_failure(code, capsys, env, text):
    out, err = capsys.readouterr()
    assert code == 2 and text in err and "Traceback" not in err + out
    assert not list(env.scratch.iterdir()), "a temporary workspace was left behind"
    assert not env.runs.exists(), "a run folder was created for a run that never started"


def test_missing_repository(cli_env, tmp_path, capsys):
    code = cli.main(["--repo", str(tmp_path / "nope")], llm=FakeLLM([]))
    assert_clean_startup_failure(code, capsys, cli_env, "Repository not found")


def test_repository_path_is_a_file(cli_env, tmp_path, capsys):
    (tmp_path / "file.txt").write_text("x")
    code = cli.main(["--repo", str(tmp_path / "file.txt")], llm=FakeLLM([]))
    assert_clean_startup_failure(code, capsys, cli_env, "not a directory")


def test_missing_api_key(cli_env, tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("GROQ_API_KEY")
    code = cli.main(["--repo", str(make_repo(tmp_path))])
    assert_clean_startup_failure(code, capsys, cli_env, "GROQ_API_KEY")


def test_git_missing(cli_env, tmp_path, monkeypatch, capsys):
    def no_git():
        raise GitError("git was not found on PATH.")
    monkeypatch.setattr(cli, "check_git_available", no_git)
    code = cli.main(["--repo", str(make_repo(tmp_path))], llm=FakeLLM([]))
    assert_clean_startup_failure(code, capsys, cli_env, "git was not found")


def test_invalid_numeric_configuration(cli_env, tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("MAX_ITERATIONS", "many")
    code = cli.main(["--repo", str(make_repo(tmp_path))], llm=FakeLLM([]))
    assert_clean_startup_failure(code, capsys, cli_env, "Invalid configuration")


def test_max_iterations_must_be_positive(cli_env, tmp_path, capsys):
    code = cli.main(["--repo", str(make_repo(tmp_path)), "--max-iterations", "0"], llm=FakeLLM([]))
    assert_clean_startup_failure(code, capsys, cli_env, "at least 1")


# --------------------------------------------------- console, ids, exit codes


def test_configure_console_sets_utf8_and_replace():
    class Stream:
        def reconfigure(self, **kwargs):
            self.kwargs = kwargs
    stream = Stream()
    cli.configure_console([stream])
    assert stream.kwargs == {"encoding": "utf-8", "errors": "replace"}


def test_configure_console_tolerates_streams_that_cannot_be_reconfigured():
    cli.configure_console([io.StringIO(), object()])


def test_console_survives_characters_the_encoding_cannot_show():
    stream = io.TextIOWrapper(io.BytesIO(), encoding="ascii")
    cli.configure_console([stream])
    stream.write("caf\u00e9 \u2192 ok")
    stream.flush()                                   # would raise UnicodeEncodeError before


def test_run_id_is_a_timestamp_and_never_collides(tmp_path):
    from datetime import datetime
    now = datetime(2026, 10, 15, 10, 15, 0)
    assert cli.make_run_id(tmp_path, now) == "20261015-101500"
    (tmp_path / "20261015-101500").mkdir()
    assert cli.make_run_id(tmp_path, now) == "20261015-101500-2"
    (tmp_path / "20261015-101500-2").mkdir()
    assert cli.make_run_id(tmp_path, now) == "20261015-101500-3"


@pytest.mark.parametrize("stop, verified, expected", [
    (StopReason.SUCCESS, True, 0),
    (StopReason.NO_FAILURES, False, 0),
    (StopReason.MAX_ITERATIONS, False, 1),
    (StopReason.NO_PROGRESS, False, 1),
    (StopReason.REPEATED_REGRESSION, False, 1),
    (StopReason.LLM_UNAVAILABLE, False, 1),
    (StopReason.PROTECTED_PATH_VIOLATION, False, 1),
    (StopReason.INTERNAL_ERROR, False, 1),
])
def test_exit_codes(stop, verified, expected):
    assert cli.exit_code_for(make_result(stop, verified=verified)) == expected


def test_a_success_stop_reason_without_verification_is_not_exit_zero():
    result = make_result()
    result.state.final_verification = False
    assert cli.exit_code_for(result) == 1


def test_summary_is_plain_ascii_and_mentions_the_essentials():
    info = RunInfo("calc", "m", "a", "b", 6, cleanup="deleted")
    text = cli.format_summary(make_result(), info, Path("runs/20261015-101500"))
    assert text.isascii()
    assert "Result: SUCCESS" in text and "2/4 -> 4/4 passing" in text and "Commit: a1b2c3d4" in text
    assert "Workspace kept" not in text
    kept = RunInfo("calc", "m", "a", "b", 6, cleanup="kept", workspace_kept="/tmp/ws")
    assert "Workspace kept at: /tmp/ws" in cli.format_summary(make_result(), kept, Path("runs/x"))


# -------------------------------------------- whole runs (offline, slow)


@pytest.mark.slow
def test_successful_run_writes_every_output_and_cleans_up(cli_env, tmp_path, capsys):
    src = make_repo(tmp_path, bugs=("add",))
    before = tree_hash(src)
    script = fake.normal_repair(ADD_FIX)
    script[0] = respond(call("list_files"), text=f"leaked {KEY}")          # a key in model text must not reach outputs
    code = cli.main(["--repo", str(src), "--task", "Fix add"], llm=FakeLLM(script), suite_runner=sim_runner([]))
    out, err = capsys.readouterr()
    assert code == 0 and "Result: SUCCESS" in out
    run = only_run(cli_env)
    assert {"run_trace.json", "report.md", "final_tests.txt", "fix.patch", "results"} <= {p.name for p in run.iterdir()}
    trace = json.loads((run / "run_trace.json").read_text(encoding="utf-8"))
    assert trace["outcome"]["success"] is True and trace["cleanup"]["status"] == "deleted"
    assert trace["run"]["history_keep_recent_results"] == 3 and trace["run"]["max_iterations"] == 15
    assert "+    return a + b   # ADD" in (run / "fix.patch").read_text(encoding="utf-8")
    assert KEY not in read_all(run) + out + err
    assert not list(cli_env.scratch.iterdir()), "the temporary workspace was not deleted"
    assert tree_hash(src) == before, "the canonical repository was modified"


@pytest.mark.slow
def test_keep_workspace_preserves_it_and_prints_the_path(cli_env, tmp_path, capsys):
    src = make_repo(tmp_path, bugs=("add",))
    code = cli.main(["--repo", str(src), "--keep-workspace"], llm=FakeLLM(fake.normal_repair(ADD_FIX)),
                    suite_runner=sim_runner([]))
    kept = list(cli_env.scratch.iterdir())
    assert code == 0 and len(kept) == 1 and (kept[0] / "app" / "calc.py").is_file()
    assert f"Workspace kept at: {kept[0]}" in capsys.readouterr().out
    trace = json.loads((only_run(cli_env) / "run_trace.json").read_text(encoding="utf-8"))
    assert trace["cleanup"] == {"status": "kept", "workspace_kept": str(kept[0])}
    if sys.version_info >= (3, 12):                                  # read-only .git files on Windows
        shutil.rmtree(kept[0], onexc=_handle_remove_error)
    else:
        shutil.rmtree(kept[0], onerror=_handle_remove_error)


@pytest.mark.slow
def test_baseline_already_passing_exits_zero_without_a_patch(cli_env, tmp_path, capsys):
    llm = FakeLLM([])
    code = cli.main(["--repo", str(make_repo(tmp_path, bugs=())), "--task", "x"], llm=llm,
                    suite_runner=sim_runner([]))
    run = only_run(cli_env)
    assert code == 0 and llm.call_count == 0 and "NO REPAIR NEEDED" in capsys.readouterr().out
    assert not (run / "fix.patch").exists() and (run / "report.md").is_file()


@pytest.mark.slow
def test_iteration_limit_exits_one_and_still_reports(cli_env, tmp_path, capsys):
    code = cli.main(["--repo", str(make_repo(tmp_path)), "--max-iterations", "2"],
                    llm=FakeLLM(fake.max_iterations(2)), suite_runner=sim_runner([]))
    run = only_run(cli_env)
    report = (run / "report.md").read_text(encoding="utf-8")
    assert code == 1 and "NOT FIXED" in report and "MAX_ITERATIONS" in capsys.readouterr().out
    assert not (run / "fix.patch").exists() and not list(cli_env.scratch.iterdir())


@pytest.mark.slow
def test_llm_failure_exits_one_with_outputs(cli_env, tmp_path):
    code = cli.main(["--repo", str(make_repo(tmp_path))], llm=FakeLLM(fake.llm_unavailable(after=1)),
                    suite_runner=sim_runner([]))
    trace = json.loads((only_run(cli_env) / "run_trace.json").read_text(encoding="utf-8"))
    assert code == 1 and trace["outcome"]["stop_reason"] == "LLM_UNAVAILABLE"
    assert not list(cli_env.scratch.iterdir())


@pytest.mark.slow
def test_internal_failure_is_a_controlled_stop_with_cleanup(cli_env, tmp_path):
    runner, calls = sim_runner([]), []

    def flaky(root, run_dir, settings, target):
        calls.append(target)
        if len(calls) > 1:
            raise RuntimeError("simulated crash")
        return runner(root, run_dir, settings, target)
    code = cli.main(["--repo", str(make_repo(tmp_path, bugs=("add",)))], llm=FakeLLM(fake.normal_repair(ADD_FIX)),
                    suite_runner=flaky)
    trace = json.loads((only_run(cli_env) / "run_trace.json").read_text(encoding="utf-8"))
    assert code == 1 and trace["outcome"]["stop_reason"] == "INTERNAL_ERROR"
    assert not list(cli_env.scratch.iterdir())


@pytest.mark.slow
def test_workspace_is_deleted_even_when_the_run_raises(cli_env, tmp_path):
    with pytest.raises(AssertionError):                       # the exhausted script is a test bug, not a stop reason
        cli.main(["--repo", str(make_repo(tmp_path))], llm=FakeLLM([fake.investigate()]),
                 suite_runner=sim_runner([]))
    assert not list(cli_env.scratch.iterdir())


@pytest.mark.slow
def test_real_pytest_end_to_end(cli_env, tmp_path, capsys):
    src = make_repo(tmp_path, bugs=("add",))
    code = cli.main(["--repo", str(src)], llm=FakeLLM(fake.normal_repair(ADD_FIX)))
    run = only_run(cli_env)
    assert code == 0 and "2/3 -> 3/3 passing" in capsys.readouterr().out
    assert (run / "junit.xml").is_file() and (run / "fix.patch").is_file()


@pytest.mark.slow
def test_real_entry_point_reports_a_missing_repository(tmp_path):
    proc = subprocess.run([sys.executable, str(ROOT / "main.py"), "--repo", str(tmp_path / "nope")],
                          capture_output=True, text=True, cwd=ROOT, timeout=60)
    assert proc.returncode == 2 and "Repository not found" in proc.stderr and "Traceback" not in proc.stderr
