"""Temporary Git workspace, audit, commit and patch export (TASK-007 to TASK-010).

The canonical repository is copied into a temporary directory, turned into
its own Git repository, and all repair work happens there. The only Git
operations CodePilot performs are the ones below; push, reset, rebase and
similar commands are refused.
"""

from __future__ import annotations

import logging
import os
import shutil
import stat
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Iterable, List, Optional, Sequence, Tuple

from codepilot.errors import GitError, GitTimeoutError, WorkspaceError
from codepilot.models import AuditResult, GitResult
from codepilot.safety import is_denied, is_editable, is_protected, is_within, sanitized_env

logger = logging.getLogger("codepilot.workspace")

# Never copied into the workspace, at any depth.
SKIP_ANYWHERE = {".git", ".venv", "venv", "__pycache__", ".pytest_cache", ".gemini"}
# Skipped only at the top level of the source repository.
SKIP_AT_ROOT = {"runs"}

# Git sub-commands CodePilot refuses to run (AGENTS.md A6).
FORBIDDEN_GIT_COMMANDS = {
    "push", "pull", "fetch", "remote", "reset", "rebase", "clean",
    "stash", "merge", "cherry-pick", "revert",
}


def check_git_available() -> str:
    """Return the path of the git executable or raise a clear error."""
    path = shutil.which("git")
    if not path:
        raise GitError(
            "git was not found on PATH. Install Git and make sure "
            "`git --version` works in your terminal."
        )
    return path


def _handle_remove_error(func, path, _exc) -> None:
    """rmtree error handler: clear the read-only flag and retry (Windows .git)."""
    try:
        os.chmod(path, stat.S_IWRITE | stat.S_IREAD)
        func(path)
    except OSError:
        pass


class Workspace:
    """A temporary Git workspace copied from a canonical source repo."""

    def __init__(
        self,
        source_dir: str,
        run_id: str = "default",
        keep_workspace: bool = False,
        timeout_s: int = 60,
    ) -> None:
        self.source_dir = Path(source_dir).resolve()
        self.run_id = run_id
        self.keep_workspace = keep_workspace
        self.timeout_s = timeout_s
        self.temp_dir: Optional[Path] = None
        self.branch_name = f"codepilot/fix-{run_id}"
        self.default_branch: Optional[str] = None
        self.baseline_commit: Optional[str] = None

    # ------------------------------------------------------------------ create

    def create(self) -> Path:
        """Copy the source, init Git, commit the baseline, switch to the fix branch."""
        if self.temp_dir is not None:
            raise WorkspaceError("Workspace has already been created.")
        if not self.source_dir.is_dir():
            raise WorkspaceError(f"Source repository not found: {self.source_dir}")
        check_git_available()

        self.temp_dir = Path(tempfile.mkdtemp(prefix=f"codepilot_ws_{self.run_id}_"))
        try:
            shutil.copytree(
                self.source_dir,
                self.temp_dir,
                ignore=self._ignore,
                symlinks=True,
                dirs_exist_ok=True,
            )
            self._run_git(["init"])
            self._run_git(["config", "user.name", "CodePilot Agent"])
            self._run_git(["config", "user.email", "agent@codepilot.local"])
            # Set before the first `git add` so line endings are stored untouched
            # and diffs never show CRLF/LF noise (TOOLS.md section 10).
            self._run_git(["config", "core.autocrlf", "false"])
            self._run_git(["config", "commit.gpgsign", "false"])
            self._run_git(["add", "-A"])
            self._run_git(["commit", "--allow-empty", "--no-verify", "-m", "Initial baseline commit"])
            self.baseline_commit = self._run_git(["rev-parse", "HEAD"])
            self.default_branch = self._run_git(["branch", "--show-current"])
            self._run_git(["checkout", "-b", self.branch_name])
        except BaseException:
            self.cleanup(keep_workspace=False)
            self.temp_dir = None
            raise
        return self.temp_dir

    def _ignore(self, directory: str, names: List[str]) -> List[str]:
        """copytree callback: skip caches, venvs, run output and secret files."""
        at_root = Path(directory).resolve() == self.source_dir
        skipped = []
        for name in names:
            lowered = name.lower()
            if (
                lowered in SKIP_ANYWHERE
                or lowered.endswith(".egg-info")
                or lowered.endswith(".pyc")
                or (at_root and lowered in SKIP_AT_ROOT)
                or is_denied(name)
            ):
                skipped.append(name)
        return skipped

    # ----------------------------------------------------------------- cleanup

    def cleanup(self, keep_workspace: bool = False) -> bool:
        """Delete the workspace unless kept. True if it no longer exists."""
        if self.temp_dir is None:
            return True
        if keep_workspace:
            return False
        if self.temp_dir.exists():
            if sys.version_info >= (3, 12):
                shutil.rmtree(self.temp_dir, onexc=_handle_remove_error)
            else:
                shutil.rmtree(self.temp_dir, onerror=_handle_remove_error)
        removed = not self.temp_dir.exists()
        if not removed:
            logger.warning("Could not fully remove workspace %s", self.temp_dir)
        return removed

    def __enter__(self) -> "Workspace":
        self.create()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.cleanup(keep_workspace=self.keep_workspace)

    # --------------------------------------------------------------------- git

    def _git_raw(self, args: Sequence[str]) -> "subprocess.CompletedProcess[bytes]":
        """Run git with an argument list and a sanitised environment."""
        if self.temp_dir is None:
            raise WorkspaceError("Workspace not initialized.")
        if args and args[0] in FORBIDDEN_GIT_COMMANDS:
            raise GitError(f"git {args[0]} is not permitted in CodePilot.")
        try:
            proc = subprocess.run(
                ["git", *args],
                cwd=self.temp_dir,
                capture_output=True,
                env=sanitized_env({"GIT_TERMINAL_PROMPT": "0"}),
                timeout=self.timeout_s,
            )
        except subprocess.TimeoutExpired as exc:
            raise GitTimeoutError(f"git {args[0]} timed out after {self.timeout_s}s") from exc
        except OSError as exc:
            raise GitError(f"git {args[0]} could not run: {exc}") from exc
        if proc.returncode != 0:
            stderr = proc.stderr.decode("utf-8", errors="replace").strip()
            raise GitError(f"git {' '.join(args)} failed: {stderr}")
        return proc

    def _git_text(self, args: Sequence[str]) -> str:
        """Run git and return stdout decoded, unmodified."""
        return self._git_raw(args).stdout.decode("utf-8", errors="replace")

    def _run_git(self, args: Sequence[str]) -> str:
        """Run git and return stripped stdout."""
        return self._git_text(args).strip()

    def current_branch(self) -> str:
        """Name of the checked-out branch."""
        return self._run_git(["branch", "--show-current"])

    def _require_baseline(self) -> str:
        if not self.baseline_commit:
            raise WorkspaceError("Workspace not initialized.")
        return self.baseline_commit

    # ------------------------------------------------------- changes and audit

    def _change_entries(self) -> List[Tuple[str, str]]:
        """(status, path) for every change versus the baseline commit.

        Status is a git letter (M, A, D, T, ...) or '?' for untracked files.
        """
        baseline = self._require_baseline()
        tokens = self._git_text(
            ["diff", "--name-status", "--no-renames", "-z", baseline]
        ).split("\0")
        tokens = [t for t in tokens if t]
        entries = [(tokens[i], tokens[i + 1]) for i in range(0, len(tokens) - 1, 2)]
        untracked = self._git_text(
            ["ls-files", "--others", "--exclude-standard", "-z"]
        ).split("\0")
        entries.extend(("?", path) for path in untracked if path)
        return entries

    def changed_files(self) -> List[str]:
        """Sorted paths changed versus the baseline (including untracked)."""
        return sorted({path for _, path in self._change_entries()})

    def audit_changes(
        self, editable_extensions: Optional[Iterable[str]] = None
    ) -> AuditResult:
        """Final changed-file audit (TASK-010).

        Passes only if every change is a modification of an existing, editable,
        non-protected file. New files, deletions, test and config changes fail.
        """
        extensions = list(editable_extensions) if editable_extensions else None
        violations: List[str] = []
        for status, path in self._change_entries():
            protected = is_protected(path)
            if status in ("A", "?"):
                tag = " [protected path]" if protected else ""
                violations.append(f"new file created: {path}{tag}")
            elif status == "D":
                tag = " [protected path]" if protected else ""
                violations.append(f"file deleted: {path}{tag}")
            elif status == "M":
                if protected:
                    violations.append(f"protected path modified: {path}")
                elif not is_editable(path, extensions):
                    violations.append(f"non-source file modified: {path}")
            else:
                violations.append(f"unsupported change type {status}: {path}")
        return AuditResult(
            ok=not violations,
            changed_files=self.changed_files(),
            violations=sorted(violations),
        )

    def diff(self, path: Optional[str] = None) -> GitResult:
        """Diff of the working tree against the baseline (read-only)."""
        baseline = self._require_baseline()
        diff_args = ["diff", "--no-color", "--no-ext-diff", "--no-textconv", baseline]
        name_args = ["diff", "--name-only", "--no-renames", "-z", baseline]
        if path is not None:
            diff_args += ["--", path]
            name_args += ["--", path]
        text = self._git_text(diff_args)
        files = [f for f in self._git_text(name_args).split("\0") if f]
        return GitResult(ok=True, diff=text, changed_files=files)

    # ------------------------------------------------------ commit and export

    def commit_fix(self, message: str) -> GitResult:
        """Commit a verified fix on the repair branch (TASK-009).

        Refuses (``ok=False``) when the audit fails or there is nothing new to
        commit, so no unverified or empty commit is ever created. Only tracked,
        modified files are staged.
        """
        if not message.strip():
            raise WorkspaceError("A commit message is required.")
        if self.current_branch() != self.branch_name:
            raise GitError(
                f"Refusing to commit: not on repair branch {self.branch_name}."
            )
        audit = self.audit_changes()
        if not audit.ok:
            return GitResult(
                ok=False,
                message="Final audit failed: " + "; ".join(audit.violations),
                changed_files=audit.changed_files,
            )
        if not self._run_git(["status", "--porcelain"]):
            return GitResult(ok=False, message="Nothing new to commit.")
        self._run_git(["add", "-u"])
        self._run_git(["commit", "--no-verify", "-m", message])
        return GitResult(
            ok=True,
            commit_hash=self._run_git(["rev-parse", "HEAD"]),
            message=message,
            changed_files=audit.changed_files,
        )

    def export_patch(self, dest: str) -> Path:
        """Write the committed fix (baseline..HEAD) to ``dest`` outside the workspace."""
        baseline = self._require_baseline()
        dest_path = Path(dest).resolve()
        if self.temp_dir is not None and is_within(dest_path, self.temp_dir.resolve()):
            raise WorkspaceError("The patch must be written outside the workspace.")
        proc = self._git_raw(
            ["diff", "--no-color", "--no-ext-diff", "--no-textconv",
             "--no-renames", baseline, "HEAD"]
        )
        if not proc.stdout.strip():
            raise WorkspaceError(
                "No committed changes to export; commit the verified fix first."
            )
        dest_path.parent.mkdir(parents=True, exist_ok=True)
        dest_path.write_bytes(proc.stdout)
        return dest_path
