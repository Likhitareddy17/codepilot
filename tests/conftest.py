"""Shared fixtures for the tool and registry tests (TESTING.md section 4)."""

import os
from pathlib import Path

import pytest

from codepilot.config import Settings
from codepilot.tools import ToolContext
from codepilot.tools.history import ResultStore
from codepilot.tools.registry import build_default_registry


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
SLOW_FIXTURES = {"run", "ws", "git_registry"}   # fixtures that create a real Git workspace
SLOW_NAMES = (
    "capture_baseline", "end_to_end", "regex", "test_workspace_creation", "test_workspace_cleanup_default",
    "test_workspace_keep_workspace", "test_workspace_cleanup_after_failure",
)


def pytest_configure(config):
    config.addinivalue_line("markers", "slow: starts pytest or Git subprocesses; skip with -m 'not slow'")


def pytest_collection_modifyitems(config, items):
    for item in items:
        if (
            Path(str(item.fspath)).name in SLOW_MODULES
            or SLOW_FIXTURES & set(getattr(item, "fixturenames", ()))
            or any(name in item.name for name in SLOW_NAMES)
        ):
            item.add_marker(pytest.mark.slow)
