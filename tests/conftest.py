"""Shared fixtures for the tool and registry tests (TESTING.md section 4)."""

import os
from pathlib import Path

import pytest

from codepilot.config import Settings
from codepilot.tools import ToolContext
from codepilot.tools.registry import build_default_registry


def write(path: Path, data) -> None:
    """Write text or bytes exactly as given (no newline translation)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data.encode("utf-8") if isinstance(data, str) else data)


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
    return ToolContext(root=tiny_repo, settings=settings)


@pytest.fixture
def registry(ctx: ToolContext):
    return build_default_registry(ctx)


def symlink_or_skip(link: Path, target: Path) -> None:
    """Create a symlink, or skip the test where the OS does not allow it."""
    try:
        os.symlink(target, link)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks are not available in this environment")
