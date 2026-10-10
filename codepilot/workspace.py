"""Workspace management and Git operations for CodePilot."""

import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import List, Optional


class Workspace:
    """Manages a temporary Git workspace copied from a canonical source repo."""

    def __init__(self, source_dir: str, run_id: str = "default"):
        self.source_dir = Path(source_dir).resolve()
        self.run_id = run_id
        self.temp_dir: Optional[Path] = None
        self.branch_name = f"codepilot/fix-{run_id}"

    def create(self) -> Path:
        """Create the temporary workspace, initialize Git, commit baseline, and checkout fix branch."""
        # 1. Create temporary directory
        self.temp_dir = Path(tempfile.mkdtemp(prefix=f"codepilot_ws_{self.run_id}_"))

        # 2. Copy source files (ignoring unwanted build/env artifacts)
        ignore_patterns = shutil.ignore_patterns(
            ".git",
            ".venv",
            "__pycache__",
            ".pytest_cache",
            "*.egg-info",
            ".env",
            "runs",
            ".gemini",
        )
        
        for item in self.source_dir.iterdir():
            if item.name in [".git", ".venv", "__pycache__", ".pytest_cache", ".env", "runs", ".gemini"]:
                continue
            if item.is_dir():
                shutil.copytree(item, self.temp_dir / item.name, ignore=ignore_patterns, dirs_exist_ok=True)
            else:
                shutil.copy2(item, self.temp_dir / item.name)

        # 3. Git initialization
        self._run_git(["init"])
        self._run_git(["config", "user.name", "CodePilot Agent"])
        self._run_git(["config", "user.email", "agent@codepilot.local"])

        # 4. Baseline commit
        self._run_git(["add", "."])
        self._run_git(["commit", "-m", "Initial baseline commit"])

        # 5. Create and checkout repair branch
        self._run_git(["checkout", "-b", self.branch_name])

        return self.temp_dir

    def _run_git(self, args: List[str]) -> str:
        """Run a git command in the workspace directory."""
        if not self.temp_dir:
            raise RuntimeError("Workspace not initialized.")
        result = subprocess.run(
            ["git"] + args,
            cwd=self.temp_dir,
            capture_output=True,
            text=True,
            check=True,
        )
        return result.stdout.strip()
