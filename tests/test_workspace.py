"""Tests for workspace creation (TASK-007)."""

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
        if workspace.temp_dir and workspace.temp_dir.exists():
            import shutil
            shutil.rmtree(workspace.temp_dir, ignore_errors=True)
