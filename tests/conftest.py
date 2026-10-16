"""Shared fixtures for the tool and registry tests (TESTING.md section 4)."""

import os
import tempfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from codepilot.config import Settings
from codepilot.tools import ToolContext
from codepilot.tools.history import ResultStore
from codepilot.tools.registry import build_default_registry
from support_sandbox import copy_sandbox, load_manifest


def write(path: Path, data) -> None:
    """Write text or bytes exactly as given (no newline translation)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data.encode("utf-8") if isinstance(data, str) else data)


PASSING_TESTS = "def test_one():\n    assert True\n\n\ndef test_two():\n    assert 1 + 1 == 2\n"
FAILING_TESTS = "def test_good():\n    assert True\n\n\ndef test_bad():\n    assert 3 == 4\n"


def make_suite(base: Path, **files: str) -> Path:
    """Create a tiny pytest repository: ``pytest.ini`` plus ``tests/<name>.py`` files."""
    repo = base / "suite"
    write(repo / "pytest.ini", "[pytest]\n")
    for name, source in files.items():
        write(repo / "tests" / f"{name}.py", source)
    return repo


@pytest.fixture
def settings() -> Settings:
    """Deterministic settings, independent of the developer's environment."""
    return Settings(
        llm_api_key="test-key",
        max_tool_output_chars=4000,
        max_arg_chars=20000,
        list_max_depth=3,
        max_list_entries=200,
        max_read_lines=400,
        max_search_results=50,
        max_search_line_chars=200,
        max_log_lines=200,
        command_timeout_s=10,
        max_patch_attempts_per_file=3,
        history_keep_recent_results=3,
        max_result_retrievals_per_run=10,
        log_path="logs/app.log",
        metrics_path="metrics.json",
    )


@pytest.fixture
def tiny_repo(tmp_path: Path) -> Path:
    """A small repository shaped like the sandbox, plus files tools must skip."""
    repo = tmp_path / "repo"
    write(repo / "app" / "__init__.py", "")
    write(repo / "app" / "pagination.py",
          "def paginate(items, page, size):\n"
          "    start = page * size\n"
          "    return items[start:start + size]\n")
    write(repo / "app" / "orders.py",
          "def process(order):\n"
          "    # size of the order\n"
          "    return order['total']\n")
    write(repo / "app" / "crlf.py", b"x = 1\r\ny = 2\r\n")
    write(repo / "tests" / "test_pagination.py",
          "from app.pagination import paginate\n\n\n"
          "def test_first_page():\n"
          "    assert paginate([1, 2, 3], 0, 2) == [1, 2]\n")
    write(repo / "logs" / "app.log",
          "2026-10-08 10:14:01 INFO order_service Order 1 processed\n"
          "2026-10-08 10:14:03 ERROR order_service Order lookup failed for order_id=404\n"
          "Traceback (most recent call last):\n"
          "  File \"app/orders.py\", line 3, in process\n"
          "TypeError: 'NoneType' object is not subscriptable\n"
          "2026-10-08 10:14:10 WARNING order_service Tax rate lookup failed for region=us, defaulting to 0\n"
          "2026-10-08 10:14:11 WARNING order_service Tax rate lookup failed for region=us, defaulting to 0\n")
    write(repo / "metrics.json",
          '{"service": "sandbox-app", "requests": 100, "errors": 18, "error_rate": 0.18,\n'
          ' "operations": {"orders": {"error_rate": 0.2, "tax_lookup_failures": 5}}}\n')
    write(repo / "pytest.ini", "[pytest]\n")
    write(repo / "README.md", "needle in the readme\n")
    # Things that must never be listed, searched or read.
    write(repo / ".env", "GROQ_API_KEY=needle-secret\n")
    write(repo / "key.pem", "needle-private-key\n")
    write(repo / ".git" / "config", "needle in git config\n")
    write(repo / "app" / "__pycache__" / "x.pyc", "needle in cache\n")
    write(repo / "data.bin", b"needle\x00binary")
    return repo


@pytest.fixture
def ctx(tiny_repo: Path, settings: Settings) -> ToolContext:
    run_dir = tiny_repo.parent / "runs" / "t1"
    return ToolContext(root=tiny_repo, settings=settings, run_dir=run_dir, history=ResultStore(run_dir, settings))


@pytest.fixture
def registry(ctx: ToolContext):
    return build_default_registry(ctx)


def symlink_or_skip(link: Path, target: Path) -> None:
    """Create a symlink, or skip the test where the OS does not allow it."""
    try:
        os.symlink(target, link)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks are not available in this environment")

# --- slow tests -----------------------------------------------------------
# Tests that start pytest or Git processes are marked "slow" automatically
# (by module, by workspace fixture, or explicitly with @pytest.mark.slow).
# Quick run:  python -m pytest -m "not slow"      Full run: python -m pytest

SLOW_MODULES = {"test_test_runner.py", "test_phase4_decisions.py", "test_workspace.py", "test_tools_git.py"}
SLOW_FIXTURES = {"run", "ws", "git_registry", "git_sb"}   # fixtures that create a real Git workspace
SLOW_NAMES = (
    "capture_baseline", "end_to_end", "regex", "test_workspace_creation", "test_workspace_cleanup_default",
    "test_workspace_keep_workspace", "test_workspace_cleanup_after_failure",
)


def pytest_configure(config):
    config.addinivalue_line("markers", "slow: starts pytest or Git subprocesses; skip with -m 'not slow'")
    config.addinivalue_line("markers", "live: calls the real Groq model; runs only with -m live (never in CI)")


def pytest_collection_modifyitems(config, items):
    if "live" not in (config.getoption("markexpr") or ""):          # live tests need an explicit -m live
        kept = [item for item in items if "live" not in item.keywords]
        if len(kept) != len(items):
            config.hook.pytest_deselected(items=[i for i in items if i not in kept])
            items[:] = kept
    for item in items:
        if (
            Path(str(item.fspath)).name in SLOW_MODULES
            or SLOW_FIXTURES & set(getattr(item, "fixturenames", ()))
            or any(name in item.name for name in SLOW_NAMES)
        ):
            item.add_marker(pytest.mark.slow)


SETTING_ENV_VARS = (
    "COMMAND_TIMEOUT_SECONDS", "HISTORY_KEEP_RECENT_RESULTS", "KEEP_WORKSPACE", "LIST_MAX_DEPTH",
    "LLM_API_KEY", "LLM_BASE_URL", "LLM_MAX_RETRIES", "LLM_MODEL", "GROQ_API_KEY", "GROQ_BASE_URL", "GROQ_MODEL",
    "LOG_PATH", "MAX_ARG_CHARS", "MAX_BAD_MODEL_OUTPUTS", "MAX_ITERATIONS", "MAX_LIST_ENTRIES", "MAX_LOG_LINES",
    "MAX_PATCH_ATTEMPTS_PER_FILE", "MAX_PROTECTED_PATH_ATTEMPTS", "MAX_READ_LINES", "MAX_REQUEST_TOKENS_ESTIMATE",
    "MAX_RESULT_RETRIEVALS_PER_RUN", "MAX_SEARCH_LINE_CHARS", "MAX_SEARCH_RESULTS", "MAX_TOOL_CALLS_PER_ITERATION",
    "MAX_TOOL_OUTPUT_CHARS", "METRICS_PATH", "RUNS_DIR", "SOURCE_REPO",
)


@pytest.fixture(autouse=True)
def isolate_settings_from_the_environment(request, monkeypatch):
    """A developer's .env or shell must never change what the offline tests see.

    ``codepilot.config`` loads ``.env`` into ``os.environ`` at import time, so values such as
    ``MAX_ITERATIONS=14`` would otherwise leak into every test. Live tests are the exception:
    they need the real key and settings. Tests that need a value set it themselves.
    """
    if request.node.get_closest_marker("live"):
        return
    for name in SETTING_ENV_VARS:
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def sandbox_expected() -> dict:
    """The sandbox manifest: counts, test ids, reference fixes (TESTING.md section 4.1)."""
    return load_manifest()


@pytest.fixture
def sandbox_copy(tmp_path: Path) -> Path:
    """A private copy of the canonical sandbox; the canonical one is never touched."""
    return copy_sandbox(tmp_path / "sandbox")


@pytest.fixture
def offline_cli_env(tmp_path, monkeypatch):
    """Runs go to tmp_path/runs and temporary workspaces to tmp_path/ws_tmp; a fake key is set."""
    runs, scratch = tmp_path / "runs", tmp_path / "ws_tmp"
    scratch.mkdir()
    monkeypatch.setenv("RUNS_DIR", str(runs))
    monkeypatch.setenv("GROQ_API_KEY", "gsk_TESTKEY1234567890abcdef")
    monkeypatch.setattr(tempfile, "tempdir", str(scratch))
    return SimpleNamespace(runs=runs, scratch=scratch)
