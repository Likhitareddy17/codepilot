"""Tests for git_diff (TASK-019), using a real temporary Git workspace."""

import json

import pytest

from codepilot.tools import ToolContext
from codepilot.tools.registry import build_default_registry
from codepilot.workspace import Workspace


@pytest.fixture
def git_registry(tiny_repo, settings):
    workspace = Workspace(str(tiny_repo), run_id="gitdiff")
    workspace.create()
    ctx = ToolContext(root=workspace.temp_dir, settings=settings, workspace=workspace)
    yield build_default_registry(ctx), ctx
    workspace.cleanup()


def call(registry, name, **args):
    return registry.execute(name, args)


def test_empty_diff(git_registry):
    registry, _ = git_registry
    result = call(registry, "git_diff")
    assert result.ok
    assert result.data == {"files_changed": [], "insertions": 0, "deletions": 0, "diff": ""}


def test_diff_after_a_patch(git_registry):
    registry, ctx = git_registry
    assert call(registry, "apply_patch", path="app/pagination.py",
                old="start = page * size", new="start = (page - 1) * size").ok
    result = call(registry, "git_diff")
    assert result.ok and not result.truncated
    assert result.data["files_changed"] == ["app/pagination.py"]
    assert (result.data["insertions"], result.data["deletions"]) == (1, 1)
    assert "-    start = page * size" in result.data["diff"]
    assert "+    start = (page - 1) * size" in result.data["diff"]


def test_diff_for_one_path(git_registry):
    registry, _ = git_registry
    call(registry, "apply_patch", path="app/pagination.py", old="page * size", new="page * size  # x")
    assert call(registry, "git_diff", path="app/pagination.py").data["files_changed"] == ["app/pagination.py"]
    assert call(registry, "git_diff", path="app/orders.py").data["files_changed"] == []


@pytest.mark.parametrize(
    "path,expected",
    [(".env", "PATH_DENIED"), (".git/config", "PATH_DENIED"), ("../x", "PATH_OUTSIDE_WORKSPACE")],
)
def test_diff_path_validation(git_registry, path, expected):
    registry, _ = git_registry
    result = call(registry, "git_diff", path=path)
    assert not result.ok and result.error["code"] == expected


def test_diff_path_cannot_inject_git_options(git_registry):
    registry, _ = git_registry
    call(registry, "apply_patch", path="app/pagination.py", old="page * size", new="page * size  # x")
    result = call(registry, "git_diff", path="--stat")
    assert result.ok and result.data["files_changed"] == []


def test_diff_output_is_capped(git_registry):
    registry, ctx = git_registry
    call(registry, "apply_patch", path="app/pagination.py",
         old="return items[start:start + size]", new="return items[start:start + size]  # " + "z" * 9000)
    result = call(registry, "git_diff")
    assert result.ok and result.truncated
    assert len(json.dumps(result.data)) <= ctx.settings.max_tool_output_chars
    assert result.data["files_changed"] == ["app/pagination.py"]


def test_no_diff_workspace_attached(registry):
    result = call(registry, "git_diff")
    assert not result.ok and result.error["code"] == "GIT_ERROR"


def test_git_diff_is_read_only(git_registry):
    registry, ctx = git_registry
    call(registry, "git_diff")
    assert ctx.workspace.audit_changes().changed_files == []
