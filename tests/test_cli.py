"""Tests for CLI entry point."""

import pytest
from codepilot.cli import parse_args, main


def test_parse_args_defaults():
    args = parse_args([])
    assert args.repo == "."
    assert args.task == "Fix the failing tests"
    assert args.max_iterations is None
    assert args.keep_workspace is False


def test_parse_args_custom():
    args = parse_args([
        "--repo", "sandbox_repo",
        "--task", "Fix bug in parser",
        "--max-iterations", "4",
        "--keep-workspace"
    ])
    assert args.repo == "sandbox_repo"
    assert args.task == "Fix bug in parser"
    assert args.max_iterations == 4
    assert args.keep_workspace is True


def test_main_execution():
    exit_code = main(["--repo", ".", "--task", "Test task"])
    assert exit_code == 0
