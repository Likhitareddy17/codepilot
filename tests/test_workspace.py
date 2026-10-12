"""Tests for workspace creation, cleanup, audit, commit and export (TASK-007 to TASK-010).

Workspace tests use real Git, as required by TESTING.md section 19.
"""

import hashlib
import os
import shutil
import stat
import subprocess
import tempfile
from pathlib import Path

import pytest

from codepilot.errors import GitError, WorkspaceError
from codepilot.models import AuditResult
from codepilot.workspace import Workspace


# ---------------------------------------------------------------- helpers


def _write(path: Path, data: str | bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data.encode("utf-8") if isinstance(data, str) else data)


def _tree_hash(root: Path) -> dict:
    return {
        p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(root.rglob("*"))
        if p.is_file()
    }


def _edit(ws: Workspace, rel: str, old: str, new: str) -> None:
    target = ws.temp_dir / rel
    data = target.read_bytes().decode("utf-8")
    assert old in data
    target.write_bytes(data.replace(old, new).encode("utf-8"))


@pytest.fixture
def source_repo(tmp_path: Path) -> Path:
    """A tiny canonical repo: one source file, one test, one config file."""
    src = tmp_path / "canonical"
    _write(src / "app" / "__init__.py", "")
    _write(src / "app" / "calc.py", "def add(a, b):\n    return a - b\n")
    _write(src / "app" / "crlf.py", b"x = 1\r\ny = 2\r\n")
    _write(
        src / "tests" / "test_calc.py",
        "from app.calc import add\n\n\ndef test_add():\n    assert add(1, 2) == 3\n",
    )
    _write(src / "pytest.ini", "[pytest]\n")
    _write(src / "README.md", "# demo\n")
    return src


@pytest.fixture
def ws(source_repo: Path):
    workspace = Workspace(str(source_repo), run_id="t1")
    workspace.create()
    yield workspace
    workspace.cleanup()


# ------------------------------------------- TASK-007 / TASK-008 (original)


def test_workspace_creation():
    current_dir = Path(__file__).parent.parent
    workspace = Workspace(source_dir=str(current_dir), run_id="testrun123")
    try:
        ws_path = workspace.create()
        assert ws_path.exists()
        assert (ws_path / ".git").exists()

        branch_output = workspace._run_git(["branch", "--show-current"])
        assert branch_output == "codepilot/fix-testrun123"

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

    assert ws_path is not None
    assert not ws_path.exists()


# ------------------------------------------------------ creation details


def test_copy_excludes_runtime_artifacts_and_secrets(tmp_path):
    src = tmp_path / "src"
    _write(src / "app" / "keep.py", "x = 1\n")
    _write(src / ".git" / "HEAD", "ref: refs/heads/dummy\n")
    _write(src / "__pycache__" / "x.pyc", b"\x00")
    _write(src / "app" / "__pycache__" / "keep.cpython-311.pyc", b"\x00")
    _write(src / ".pytest_cache" / "v", "x")
    _write(src / ".venv" / "lib.py", "x")
    _write(src / "runs" / "old" / "trace.json", "{}")
    _write(src / ".env", "GROQ_API_KEY=secret\n")
    _write(src / ".env.local", "X=1\n")
    _write(src / "server.pem", "-----BEGIN-----")
    workspace = Workspace(str(src), run_id="excl")
    try:
        root = workspace.create()
        present = {p.relative_to(root).as_posix() for p in root.rglob("*") if ".git/" not in p.as_posix()}
        assert "app/keep.py" in present
        for unwanted in (
            "__pycache__", "app/__pycache__", ".pytest_cache", ".venv",
            "runs", ".env", ".env.local", "server.pem",
        ):
            assert unwanted not in present
        # The copied .git is the fresh repository, not the source's dummy one.
        assert "dummy" not in (root / ".git" / "HEAD").read_text()
    finally:
        workspace.cleanup()


def test_baseline_branch_and_local_git_config(ws, source_repo):
    assert ws.current_branch() == "codepilot/fix-t1"
    assert ws.default_branch and ws.default_branch != ws.current_branch()
    assert ws.baseline_commit == ws._run_git(["rev-parse", ws.default_branch])
    assert ws._run_git(["rev-list", "--count", "HEAD"]) == "1"
    assert ws._run_git(["config", "--local", "user.name"]) == "CodePilot Agent"
    assert ws._run_git(["config", "--local", "user.email"]) == "agent@codepilot.local"
    assert ws._run_git(["config", "--local", "core.autocrlf"]) == "false"
    assert ws._run_git(["status", "--porcelain"]) == ""


def test_crlf_files_are_copied_byte_for_byte_without_diff_noise(ws, source_repo):
    assert (ws.temp_dir / "app" / "crlf.py").read_bytes() == b"x = 1\r\ny = 2\r\n"
    assert ws.diff().diff == ""
    assert ws.changed_files() == []


def test_create_twice_is_rejected(ws):
    with pytest.raises(WorkspaceError, match="already"):
        ws.create()


def test_missing_source_directory(tmp_path):
    workspace = Workspace(str(tmp_path / "nope"), run_id="x")
    with pytest.raises(WorkspaceError, match="not found"):
        workspace.create()
    assert workspace.temp_dir is None


def test_git_missing_gives_clear_error(monkeypatch, source_repo):
    monkeypatch.setattr("codepilot.workspace.shutil.which", lambda *a, **k: None)
    workspace = Workspace(str(source_repo), run_id="nogit")
    with pytest.raises(GitError, match="git"):
        workspace.create()
    assert workspace.temp_dir is None


def test_failed_create_removes_partial_workspace(monkeypatch, source_repo):
    created = []
    real_mkdtemp = tempfile.mkdtemp

    def spy(*args, **kwargs):
        path = real_mkdtemp(*args, **kwargs)
        created.append(path)
        return path

    real_run_git = Workspace._run_git

    def boom(self, args):
        if args and args[0] == "commit":
            raise GitError("simulated commit failure")
        return real_run_git(self, args)

    monkeypatch.setattr("codepilot.workspace.tempfile.mkdtemp", spy)
    monkeypatch.setattr(Workspace, "_run_git", boom)
    workspace = Workspace(str(source_repo), run_id="partial")
    with pytest.raises(GitError, match="simulated"):
        workspace.create()
    assert created and not Path(created[0]).exists()
    assert workspace.temp_dir is None


def test_context_manager_honours_keep_workspace(source_repo):
    with Workspace(str(source_repo), run_id="kept", keep_workspace=True) as workspace:
        path = workspace.temp_dir
    try:
        assert path.exists()
    finally:
        Workspace(str(source_repo)).cleanup()
        shutil.rmtree(path, ignore_errors=True)


def test_cleanup_removes_read_only_files(source_repo):
    workspace = Workspace(str(source_repo), run_id="ro")
    root = workspace.create()
    read_only = root / "app" / "calc.py"
    os.chmod(read_only, stat.S_IREAD)
    assert workspace.cleanup() is True
    assert not root.exists()


def test_cleanup_return_values(source_repo):
    workspace = Workspace(str(source_repo), run_id="rv")
    assert workspace.cleanup() is True  # never created
    root = workspace.create()
    assert workspace.cleanup(keep_workspace=True) is False
    assert root.exists()
    assert workspace.cleanup() is True
    assert not root.exists()


# -------------------------------------------------- forbidden git operations


@pytest.mark.parametrize(
    "command", ["push", "pull", "fetch", "remote", "reset", "rebase", "clean", "stash"]
)
def test_forbidden_git_commands_are_refused(ws, command):
    with pytest.raises(GitError, match="not permitted"):
        ws._run_git([command])


# ------------------------------------------------------------ diff / audit


def test_diff_and_changed_files_after_edit(ws):
    _edit(ws, "app/calc.py", "a - b", "a + b")
    result = ws.diff()
    assert result.ok
    assert result.changed_files == ["app/calc.py"]
    assert "-    return a - b" in result.diff
    assert "+    return a + b" in result.diff
    assert ws.changed_files() == ["app/calc.py"]


def test_diff_for_single_path_and_empty_diff(ws):
    assert ws.diff().diff == ""
    _edit(ws, "app/calc.py", "a - b", "a + b")
    assert ws.diff("app/crlf.py").diff == ""
    assert "calc.py" in ws.diff("app/calc.py").diff


def test_diff_path_cannot_inject_git_options(ws):
    _edit(ws, "app/calc.py", "a - b", "a + b")
    # The path goes after `--`, so this is treated as a (missing) path, not an option.
    assert ws.diff("--stat").diff == ""


def test_audit_passes_for_source_only_change(ws):
    _edit(ws, "app/calc.py", "a - b", "a + b")
    audit = ws.audit_changes()
    assert isinstance(audit, AuditResult)
    assert audit.ok
    assert audit.changed_files == ["app/calc.py"]
    assert audit.violations == []
    assert audit.to_dict()["ok"] is True


def test_audit_passes_with_no_changes(ws):
    audit = ws.audit_changes()
    assert audit.ok and audit.changed_files == [] and audit.violations == []


@pytest.mark.parametrize(
    "action,expected",
    [
        ("modify_test", "protected path modified: tests/test_calc.py"),
        ("modify_config", "protected path modified: pytest.ini"),
        ("modify_readme", "non-source file modified: README.md"),
        ("create_source", "new file created: app/new.py"),
        ("create_test", "new file created: tests/test_new.py [protected path]"),
        ("create_env", "new file created: .env [protected path]"),
        ("delete_source", "file deleted: app/calc.py"),
        ("delete_test", "file deleted: tests/test_calc.py [protected path]"),
    ],
)
def test_audit_detects_violations(ws, action, expected):
    root = ws.temp_dir
    if action == "modify_test":
        _edit(ws, "tests/test_calc.py", "== 3", "== 4")
    elif action == "modify_config":
        _write(root / "pytest.ini", "[pytest]\naddopts = --deselect x\n")
    elif action == "modify_readme":
        _write(root / "README.md", "# changed\n")
    elif action == "create_source":
        _write(root / "app" / "new.py", "y = 2\n")
    elif action == "create_test":
        _write(root / "tests" / "test_new.py", "def test_x():\n    pass\n")
    elif action == "create_env":
        _write(root / ".env", "KEY=1\n")
    elif action == "delete_source":
        (root / "app" / "calc.py").unlink()
    elif action == "delete_test":
        (root / "tests" / "test_calc.py").unlink()
    audit = ws.audit_changes()
    assert not audit.ok
    assert expected in audit.violations


def test_audit_reports_every_violation(ws):
    _edit(ws, "app/calc.py", "a - b", "a + b")
    _edit(ws, "tests/test_calc.py", "== 3", "== 4")
    _write(ws.temp_dir / "app" / "new.py", "y = 2\n")
    audit = ws.audit_changes()
    assert not audit.ok
    assert len(audit.violations) == 2
    assert audit.changed_files == ["app/calc.py", "app/new.py", "tests/test_calc.py"]


# ------------------------------------------------------------ commit / export


def test_commit_fix_creates_one_commit_on_fix_branch(ws):
    _edit(ws, "app/calc.py", "a - b", "a + b")
    result = ws.commit_fix("Fix add()")
    assert result.ok
    assert result.commit_hash == ws._run_git(["rev-parse", "HEAD"])
    assert result.changed_files == ["app/calc.py"]
    assert ws._run_git(["rev-list", "--count", "HEAD"]) == "2"
    assert ws._run_git(["log", "-1", "--format=%an"]) == "CodePilot Agent"
    assert ws._run_git(["status", "--porcelain"]) == ""
    # Default branch is untouched.
    assert ws._run_git(["rev-parse", ws.default_branch]) == ws.baseline_commit
    assert ws.current_branch() == "codepilot/fix-t1"


def test_commit_fix_refuses_when_audit_fails(ws):
    _edit(ws, "app/calc.py", "a - b", "a + b")
    _edit(ws, "tests/test_calc.py", "== 3", "== 4")
    result = ws.commit_fix("Fix add()")
    assert not result.ok
    assert "audit failed" in result.message
    assert "tests/test_calc.py" in result.message
    assert ws._run_git(["rev-list", "--count", "HEAD"]) == "1"
    assert ws._run_git(["diff", "--cached", "--name-only"]) == ""


def test_commit_fix_never_creates_an_empty_commit(ws):
    result = ws.commit_fix("Nothing changed")
    assert not result.ok
    assert ws._run_git(["rev-list", "--count", "HEAD"]) == "1"

    _edit(ws, "app/calc.py", "a - b", "a + b")
    assert ws.commit_fix("Fix add()").ok
    again = ws.commit_fix("Again")
    assert not again.ok
    assert ws._run_git(["rev-list", "--count", "HEAD"]) == "2"


def test_commit_fix_requires_message_and_repair_branch(ws):
    _edit(ws, "app/calc.py", "a - b", "a + b")
    with pytest.raises(WorkspaceError, match="message"):
        ws.commit_fix("   ")
    ws._run_git(["checkout", ws.default_branch])
    with pytest.raises(GitError, match="repair branch"):
        ws.commit_fix("Fix add()")


def test_export_patch_applies_to_a_fresh_copy_and_survives_cleanup(ws, source_repo, tmp_path):
    _edit(ws, "app/calc.py", "a - b", "a + b")
    assert ws.commit_fix("Fix add()").ok
    patch = ws.export_patch(str(tmp_path / "runs" / "t1" / "fix.patch"))
    assert patch.exists()
    assert b"diff --git a/app/calc.py b/app/calc.py" in patch.read_bytes()

    fresh = tmp_path / "fresh"
    shutil.copytree(source_repo, fresh)
    subprocess.run(["git", "-c", "core.autocrlf=false", "apply", str(patch)], cwd=fresh, check=True)
    assert (fresh / "app" / "calc.py").read_bytes() == (ws.temp_dir / "app" / "calc.py").read_bytes()
    assert (fresh / "app" / "crlf.py").read_bytes() == b"x = 1\r\ny = 2\r\n"

    assert ws.cleanup() is True
    assert patch.exists()


def test_export_patch_preserves_crlf_files(ws, source_repo, tmp_path):
    _edit(ws, "app/crlf.py", "x = 1", "x = 100")
    assert ws.commit_fix("Change x").ok
    patch = ws.export_patch(str(tmp_path / "fix.patch"))
    fresh = tmp_path / "fresh"
    shutil.copytree(source_repo, fresh)
    subprocess.run(["git", "-c", "core.autocrlf=false", "apply", str(patch)], cwd=fresh, check=True)
    assert (fresh / "app" / "crlf.py").read_bytes() == b"x = 100\r\ny = 2\r\n"


def test_export_patch_without_commit_is_rejected(ws, tmp_path):
    _edit(ws, "app/calc.py", "a - b", "a + b")
    with pytest.raises(WorkspaceError, match="No committed changes"):
        ws.export_patch(str(tmp_path / "fix.patch"))
    assert not (tmp_path / "fix.patch").exists()


def test_export_patch_cannot_write_inside_workspace(ws):
    _edit(ws, "app/calc.py", "a - b", "a + b")
    assert ws.commit_fix("Fix add()").ok
    with pytest.raises(WorkspaceError, match="outside the workspace"):
        ws.export_patch(str(ws.temp_dir / "fix.patch"))


# ------------------------------------------------------------ isolation


def test_canonical_source_is_never_modified(source_repo, tmp_path):
    before = _tree_hash(source_repo)
    workspace = Workspace(str(source_repo), run_id="iso")
    workspace.create()
    _edit(workspace, "app/calc.py", "a - b", "a + b")
    _write(workspace.temp_dir / "app" / "extra.py", "z = 3\n")
    workspace.commit_fix("whatever")
    workspace.cleanup()
    assert _tree_hash(source_repo) == before


def test_workspace_works_when_parent_env_injects_git_config(monkeypatch, source_repo):
    # Some tools (e.g. Gemini CLI) set GIT_CONFIG_COUNT/KEY_n/VALUE_n.
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "core.pager")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", "cat")
    monkeypatch.setenv("GIT_DIR", str(source_repo / "nonexistent.git"))
    workspace = Workspace(str(source_repo), run_id="envinject")
    try:
        workspace.create()
        assert workspace.current_branch() == "codepilot/fix-envinject"
    finally:
        workspace.cleanup()
