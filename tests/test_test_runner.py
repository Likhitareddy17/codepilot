"""Tests for the pytest runner, JUnit parsing and the run_tests tool (TASK-015, TASK-024)."""

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from codepilot.config import Settings
from codepilot.errors import WorkspaceError
from codepilot.tools import ToolContext
from codepilot.tools.registry import build_default_registry
from codepilot.tools.tests import (
    MAX_CAPTURE_BYTES, normalize_message, parse_junit, parse_summary_output, run_pytest,
)
from conftest import FAILING_TESTS, PASSING_TESTS, make_suite, write

MIXED = '''import pytest


def test_ok():
    assert True


def test_fail():
    assert 3 == 4


@pytest.mark.parametrize("v", ["a.b", 1])
def test_param(v):
    assert v != 1


@pytest.fixture
def boom():
    raise RuntimeError("fixture boom")


def test_err(boom):
    pass


@pytest.mark.skip(reason="skipped on purpose")
def test_skip():
    pass


class TestKlass:
    def test_method(self):
        assert False
'''


def make_settings(timeout: int = 60) -> Settings:
    return Settings(llm_api_key="x", command_timeout_s=timeout, max_tool_output_chars=4000)


@pytest.fixture(scope="module")
def mixed(tmp_path_factory):
    base = tmp_path_factory.mktemp("mixed")
    repo = make_suite(base, test_mixed=MIXED)
    run_dir = base / "runs" / "r1"
    return repo, run_dir, run_pytest(repo, run_dir, make_settings())


def pid_alive(pid: int) -> bool:
    if os.name == "nt":
        out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}"], capture_output=True, text=True).stdout
        return str(pid) in out
    stat = Path(f"/proc/{pid}/stat")
    if stat.exists():
        return stat.read_text().rsplit(")", 1)[-1].split()[0] != "Z"
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


# ------------------------------------------------------- structured results


def test_mixed_suite_counts_and_exit_code(mixed):
    _, _, outcome = mixed
    r = outcome.result
    assert r.exit_code == 1
    assert (r.passed, r.failed, r.errors, r.skipped) == (2, 3, 1, 1)
    assert r.scope == "full" and not r.timed_out and not r.all_passing
    assert r.duration_s > 0


def test_failing_ids_are_full_node_ids_sorted_and_unique(mixed):
    _, _, outcome = mixed
    assert outcome.result.failing_ids == [
        "tests/test_mixed.py::TestKlass::test_method",
        "tests/test_mixed.py::test_err",
        "tests/test_mixed.py::test_fail",
        "tests/test_mixed.py::test_param[1]",
    ]


def test_main_error_is_the_first_failing_tests_message(mixed):
    _, _, outcome = mixed
    r = outcome.result
    assert "assert False" in r.main_error
    assert r.main_error == r.failure_messages[r.failing_ids[0]]
    assert set(r.failure_messages) == set(r.failing_ids)
    assert "3 == 4" in r.failure_messages["tests/test_mixed.py::test_fail"]


def test_excerpt_contains_the_failure_details(mixed):
    _, _, outcome = mixed
    assert "RuntimeError" in outcome.result.output_excerpt or "assert" in outcome.result.output_excerpt
    assert len(outcome.result.output_excerpt) <= 3000 + len("\n... [truncated]")


def test_raw_evidence_and_junit_are_kept_outside_the_workspace(mixed):
    repo, run_dir, outcome = mixed
    assert outcome.command[:3] == [sys.executable, "-m", "pytest"]
    assert "passed" in outcome.stdout
    assert Path(outcome.junit_path) == (run_dir / "junit.xml").resolve()
    assert (run_dir / "junit.xml").is_file()
    assert not (repo / "junit.xml").exists()
    assert not list(run_dir.glob("pytest_*"))  # temp capture files are removed


def test_run_leaves_no_cache_or_bytecode_in_the_workspace(mixed):
    repo, _, _ = mixed
    assert not (repo / ".pytest_cache").exists()
    assert not list(repo.rglob("__pycache__"))


@pytest.mark.parametrize(
    "target,passed,failed,all_passing",
    [
        ("tests/test_mixed.py::test_ok", 1, 0, True),
        ("tests/test_mixed.py::test_param[a.b]", 1, 0, True),
        ("tests/test_mixed.py::TestKlass", 0, 1, False),
        ("tests/test_mixed.py::TestKlass::test_method", 0, 1, False),
    ],
)
def test_targeted_runs(mixed, target, passed, failed, all_passing):
    repo, run_dir, _ = mixed
    r = run_pytest(repo, run_dir, make_settings(), target=target).result
    assert r.scope == "targeted"
    assert (r.passed, r.failed) == (passed, failed)
    assert r.all_passing is all_passing


def test_passing_suite(tmp_path):
    repo = make_suite(tmp_path, test_pass=PASSING_TESTS)
    r = run_pytest(repo, tmp_path / "run", make_settings()).result
    assert r.exit_code == 0 and r.passed == 2 and r.failing_ids == []
    assert r.all_passing and r.main_error is None


def test_collection_error_is_reported_with_the_file_as_id(tmp_path):
    repo = make_suite(tmp_path, test_broken="import module_that_does_not_exist\n\n\ndef test_x():\n    pass\n")
    r = run_pytest(repo, tmp_path / "run", make_settings()).result
    assert r.exit_code == 2 and r.errors == 1 and not r.all_passing
    assert r.failing_ids == ["tests/test_broken.py"]
    assert "ModuleNotFoundError" in r.main_error


def test_syntax_error_in_a_test_module_is_a_collection_error(tmp_path):
    repo = make_suite(tmp_path, test_syntax="def test_x(:\n    pass\n")
    r = run_pytest(repo, tmp_path / "run", make_settings()).result
    assert r.errors == 1 and r.failing_ids == ["tests/test_syntax.py"] and not r.all_passing


def test_zero_tests_never_counts_as_passing(tmp_path):
    repo = make_suite(tmp_path, test_empty="x = 1\n")
    r = run_pytest(repo, tmp_path / "run", make_settings()).result
    assert r.exit_code == 5 and r.passed == 0 and not r.all_passing


def test_nonexistent_target_has_no_stale_result(tmp_path):
    repo = make_suite(tmp_path, test_pass=PASSING_TESTS)
    run_dir = tmp_path / "run"
    write(run_dir / "junit.xml", '<testsuites><testsuite tests="99"><testcase classname="x" name="y"/></testsuites>')
    r = run_pytest(repo, run_dir, make_settings(), target="tests/nofile.py").result
    assert r.exit_code == 4 and r.passed == 0 and not r.all_passing


# ------------------------------------------------- timeouts and processes


def test_timeout_stops_the_run(tmp_path):
    repo = make_suite(tmp_path, test_slow="import time\n\n\ndef test_slow():\n    time.sleep(120)\n")
    started = time.monotonic()
    r = run_pytest(repo, tmp_path / "run", make_settings(timeout=3)).result
    assert time.monotonic() - started < 40
    assert r.timed_out and r.exit_code == -1 and not r.all_passing
    assert "timed out" in r.main_error and "timed out" in r.output_excerpt


def test_timeout_kills_the_whole_process_tree(tmp_path):
    pidfile = tmp_path / "grandchild.pid"
    source = (
        "import pathlib, subprocess, sys, time\n\n\n"
        "def test_spawns_a_child():\n"
        "    code = \"import os, pathlib, time; pathlib.Path(r'%s').write_text(str(os.getpid())); time.sleep(300)\"\n"
        "    subprocess.Popen([sys.executable, '-c', code])\n"
        "    for _ in range(100):\n"
        "        if pathlib.Path(r'%s').exists():\n"
        "            break\n"
        "        time.sleep(0.1)\n"
        "    time.sleep(300)\n"
    ) % (pidfile.as_posix(), pidfile.as_posix())
    repo = make_suite(tmp_path, test_tree=source)
    r = run_pytest(repo, tmp_path / "run", make_settings(timeout=6)).result
    assert r.timed_out
    assert pidfile.exists(), "the grandchild never started"
    pid = int(pidfile.read_text())
    deadline = time.monotonic() + 10
    while pid_alive(pid) and time.monotonic() < deadline:
        time.sleep(0.2)
    assert not pid_alive(pid), "the grandchild process survived the timeout"


def test_very_large_output_is_capped(tmp_path):
    source = "def test_noisy():\n    print('x' * 1000 + '\\n') if False else None\n    for _ in range(4000):\n        print('y' * 1000)\n    assert False\n"
    repo = make_suite(tmp_path, test_noisy=source)
    started = time.monotonic()
    outcome = run_pytest(repo, tmp_path / "run", make_settings(), excerpt_chars=3000)
    assert time.monotonic() - started < 60
    assert outcome.result.failing_ids == ["tests/test_noisy.py::test_noisy"]
    assert len(outcome.stdout) <= MAX_CAPTURE_BYTES + 100
    assert len(outcome.result.output_excerpt) <= 3000 + len("\n... [truncated]")


# ------------------------------------------------------------ environment


def test_environment_is_sanitized(tmp_path, monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "gsk_secret")
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_secret")
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("PYTEST_ADDOPTS", "-k nothing_matches_this")
    source = (
        "import os\n\n\n"
        "def test_no_secrets_in_environment():\n"
        "    assert 'GROQ_API_KEY' not in os.environ\n"
        "    assert 'GITHUB_TOKEN' not in os.environ\n"
        "    assert 'GIT_CONFIG_COUNT' not in os.environ\n"
        "    assert 'PYTEST_ADDOPTS' not in os.environ\n"
        "    assert os.environ.get('PYTHONDONTWRITEBYTECODE') == '1'\n"
    )
    repo = make_suite(tmp_path, test_env=source)
    r = run_pytest(repo, tmp_path / "run", make_settings()).result
    assert r.all_passing, r.output_excerpt  # also proves PYTEST_ADDOPTS did not deselect the test


# ------------------------------------------------------------- validation


def test_run_dir_inside_the_workspace_is_rejected(tmp_path):
    repo = make_suite(tmp_path, test_pass=PASSING_TESTS)
    with pytest.raises(WorkspaceError, match="outside the workspace"):
        run_pytest(repo, repo / "runs", make_settings())


@pytest.mark.parametrize("target", ["-k", "--version", "-x", "", "  "])
def test_option_like_targets_are_rejected(tmp_path, target):
    repo = make_suite(tmp_path, test_pass=PASSING_TESTS)
    with pytest.raises(ValueError):
        run_pytest(repo, tmp_path / "run", make_settings(), target=target)


# ---------------------------------------------------------- run_tests tool


@pytest.fixture
def suite_registry(tmp_path):
    repo = make_suite(tmp_path, test_pass=PASSING_TESTS, test_fail=FAILING_TESTS)
    ctx = ToolContext(root=repo, settings=make_settings(timeout=5), run_dir=tmp_path / "runs" / "t1")
    return build_default_registry(ctx), ctx


def call(registry, **args):
    return registry.execute("run_tests", args)


def test_tool_full_suite(suite_registry):
    registry, ctx = suite_registry
    result = call(registry)
    assert result.ok
    d = result.data
    assert d["scope"] == "full" and d["exit_code"] == 1
    assert (d["passed"], d["failed"]) == (3, 1)
    assert d["failing_ids"] == ["tests/test_fail.py::test_bad"]
    assert d["all_passing"] is False and d["timed_out"] is False
    assert "3 == 4" in d["main_error"]
    assert len(json.dumps(d)) <= ctx.settings.max_tool_output_chars


def test_tool_targeted_run_is_marked_targeted(suite_registry):
    registry, _ = suite_registry
    d = call(registry, target="tests/test_pass.py").data
    assert d["scope"] == "targeted" and d["all_passing"] is True and d["passed"] == 2
    d = call(registry, target="tests\\test_pass.py::test_one").data
    assert d["scope"] == "targeted" and d["passed"] == 1


@pytest.mark.parametrize(
    "args,expected",
    [
        ({"target": "tests/nofile.py"}, "NOT_FOUND"),
        ({"target": "tests/test_pass.py::test_nope"}, "NOT_FOUND"),
        ({"target": "../outside.py"}, "PATH_OUTSIDE_WORKSPACE"),
        ({"target": "/etc/passwd"}, "PATH_OUTSIDE_WORKSPACE"),
        ({"target": ".env"}, "PATH_DENIED"),
        ({"target": "-x"}, "INVALID_ARGUMENTS"),
        ({"target": "--version"}, "INVALID_ARGUMENTS"),
        ({"target": "::test_one"}, "INVALID_ARGUMENTS"),
        ({"target": ""}, "INVALID_ARGUMENTS"),
        ({"target": 5}, "INVALID_ARGUMENTS"),
        ({"flags": "-x"}, "INVALID_ARGUMENTS"),
    ],
)
def test_tool_rejects_bad_targets(suite_registry, args, expected):
    registry, _ = suite_registry
    result = call(registry, **args)
    assert not result.ok and result.error["code"] == expected


def test_tool_needs_a_run_directory(suite_registry):
    registry, ctx = suite_registry
    ctx.run_dir = None
    result = call(registry)
    assert not result.ok and result.error["code"] == "INTERNAL_ERROR"


def test_tool_reports_a_timeout_as_data(tmp_path):
    repo = make_suite(tmp_path, test_slow="import time\n\n\ndef test_slow():\n    time.sleep(120)\n")
    ctx = ToolContext(root=repo, settings=make_settings(timeout=3), run_dir=tmp_path / "runs" / "t1")
    result = call(build_default_registry(ctx))
    assert result.ok and result.data["timed_out"] is True and result.data["all_passing"] is False


def test_tool_output_fits_the_budget_with_many_failures(tmp_path):
    source = "".join(f"def test_f{i:03d}():\n    assert {i} == -1, 'message number {i} ' + 'x' * 150\n\n\n" for i in range(80))
    repo = make_suite(tmp_path, test_many=source)
    ctx = ToolContext(root=repo, settings=make_settings(), run_dir=tmp_path / "runs" / "t1")
    result = call(build_default_registry(ctx))
    assert result.ok and result.truncated
    assert len(json.dumps(result.data)) <= ctx.settings.max_tool_output_chars
    assert result.data["failed"] == 80


# ------------------------------------------------------------ pure parsing

XUNIT = '''<?xml version="1.0"?><testsuites><testsuite name="pytest" tests="6">
<testcase classname="tests.test_a" name="test_ok" file="tests/test_a.py"/>
<testcase classname="tests.test_a" name="test_fail" file="tests/test_a.py"><failure message="assert 1 == 2">E assert 1 == 2</failure></testcase>
<testcase classname="tests.test_a" name="test_p[a.b-1]" file="tests/test_a.py"><failure message="boom at 0x7f3a2c">x</failure></testcase>
<testcase classname="tests.test_a.TestK" name="test_m" file="tests/test_a.py"><error message="oops">x</error></testcase>
<testcase classname="tests.test_a" name="test_skip" file="tests/test_a.py"><skipped message="why"/></testcase>
<testcase classname="" name="tests.test_c" file="tests/test_c.py"><error message="collection failure">Traceback\nE   ImportError: no module named zzz</error></testcase>
</testsuite></testsuites>'''


def test_parse_junit_statuses_ids_and_messages(tmp_path):
    parsed = parse_junit(XUNIT, tmp_path)
    assert (parsed.passed, parsed.failed, parsed.errors, parsed.skipped) == (1, 2, 2, 1)
    assert parsed.failing_ids == [
        "tests/test_a.py::TestK::test_m",
        "tests/test_a.py::test_fail",
        "tests/test_a.py::test_p[a.b-1]",
        "tests/test_c.py",
    ]
    assert parsed.messages["tests/test_a.py::test_fail"] == "assert 1 == 2"
    assert parsed.messages["tests/test_a.py::test_p[a.b-1]"] == "boom at 0x?"
    assert parsed.messages["tests/test_c.py"] == "ImportError: no module named zzz"


def test_parse_junit_without_file_attribute_finds_the_module_on_disk(tmp_path):
    write(tmp_path / "tests" / "pkg" / "test_b.py", "")
    xml = ('<testsuites><testsuite><testcase classname="tests.pkg.test_b.TestK" name="test_m"><failure message="x"/></testcase>'
           '<testcase classname="tests.pkg.test_b" name="test_n"><failure message="y"/></testcase>'
           '<testcase classname="" name="tests.pkg.test_b"><error message="z"/></testcase></testsuite></testsuites>')
    assert parse_junit(xml, tmp_path).failing_ids == [
        "tests/pkg/test_b.py", "tests/pkg/test_b.py::TestK::test_m", "tests/pkg/test_b.py::test_n",
    ]


def test_a_test_with_failure_and_teardown_error_is_one_failing_id(tmp_path):
    xml = '<testsuites><testsuite><testcase classname="tests.t" name="test_x" file="tests/t.py"><failure message="a"/><error message="b"/></testcase></testsuite></testsuites>'
    parsed = parse_junit(xml, tmp_path)
    assert parsed.failing_ids == ["tests/t.py::test_x"] and (parsed.failed, parsed.errors) == (1, 0)


@pytest.mark.parametrize("text", ["", "   ", "<testsuites><testsuite><testcase", "not xml at all", "<other/>"])
def test_parse_junit_returns_none_for_empty_or_malformed_input(tmp_path, text):
    assert parse_junit(text, tmp_path) is None


def test_parse_junit_empty_suite_is_zero_tests(tmp_path):
    parsed = parse_junit('<testsuites><testsuite tests="0"/></testsuites>', tmp_path)
    assert (parsed.passed, parsed.failed, parsed.errors, parsed.failing_ids) == (0, 0, 0, [])


def test_summary_fallback_parser():
    stdout = (
        "=========================== short test summary info ============================\n"
        "FAILED tests/test_a.py::test_fail - assert 1 == 2\n"
        "FAILED tests/test_a.py::test_p[a.b-1] - AssertionError\n"
        "ERROR tests/test_c.py\n"
        "======== 2 failed, 5 passed, 1 skipped, 1 error in 0.12s ========\n"
    )
    parsed = parse_summary_output(stdout)
    assert (parsed.passed, parsed.failed, parsed.errors, parsed.skipped) == (5, 2, 1, 1)
    assert parsed.failing_ids == ["tests/test_a.py::test_fail", "tests/test_a.py::test_p[a.b-1]", "tests/test_c.py"]
    assert parsed.messages["tests/test_a.py::test_fail"] == "assert 1 == 2"


def test_normalize_message():
    assert normalize_message("\n\n  assert   <Foo at 0x7ffd1234>  == 3\nsecond line") == "assert <Foo at 0x?> == 3"
    assert normalize_message("") == ""
    assert len(normalize_message("x" * 1000)) == 200
