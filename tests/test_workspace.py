"""Tests for workspace creation and cleanup (TASK-007, TASK-008)."""

import pytest
from pathlib import Path
from codepilot.workspace import Workspace


def test_workspace_creation():
    current_dir = Path(__file__).parent.parent
    workspace = Workspace(source_dir=str(current_dir), run_id="testrun123")
    try:
        ws_path = workspace.create()
        assert ws_path.exists()
        assert (ws_path / ".git").exists()
        
        # Check branch name
        branch_output = workspace._run_git(["branch", "--show-current"])
        assert branch_output == "codepilot/fix-testrun123"
        
        # Check baseline commit exists
        log_output = workspace._run_git(["log", "--oneline"])
        assert "Initial baseline commit" in log_output
    finally:
        workspace.cleanup(keep_workspace=False)


def test_workspace_cleanup_default():
    current_dir = Path(__file__).parent.parent
    workspace = Workspace(source_dir=str(current_dir), run_id="cleanup_test")
    ws_path = workspace.create()
    assert ws_path.exists()
    
    workspace.cleanup(keep_workspace=False)
    assert not ws_path.exists()


def test_workspace_keep_workspace():
    current_dir = Path(__file__).parent.parent
    workspace = Workspace(source_dir=str(current_dir), run_id="keep_test")
    ws_path = workspace.create()
    assert ws_path.exists()
    try:
        workspace.cleanup(keep_workspace=True)
        assert ws_path.exists()
    finally:
        # manual cleanup for test hygiene
        import shutil
        shutil.rmtree(ws_path, ignore_errors=True)


def test_workspace_cleanup_after_failure():
    current_dir = Path(__file__).parent.parent
    workspace = Workspace(source_dir=str(current_dir), run_id="fail_test")
    ws_path = None
    try:
        with workspace:
            ws_path = workspace.temp_dir
            assert ws_path.exists()
            raise ValueError("Simulated agent failure")
    except ValueError:
        pass
    
    # Verify workspace cleaned up even after exception
    assert ws_path is not None
    assert not ws_path.exists()
