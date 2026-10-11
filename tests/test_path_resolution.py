"""Tests for filesystem-aware path validation (TASK-021)."""

import pytest

from codepilot.config import DEFAULT_PROTECTED_PATHS, DEFAULT_READ_DENYLIST
from codepilot.errors import ToolFailure
from codepilot.safety import ensure_readable, ensure_writable, is_within, resolve_path
from conftest import symlink_or_skip, write


def code_of(exc_info) -> str:
    return exc_info.value.code


@pytest.mark.parametrize(
    "bad",
    [
        "../outside.py",
        "app/../../outside.py",
        "/etc/passwd",
        "\\\\server\\share\\x.py",
        "C:\\Windows\\system.ini",
        "C:relative.py",
        "app/pagination.py:stream",
        "app/pag\x00ination.py",
        "CON",
        "app/nul.txt",
        "app/COM1",
        "aux.py",
        "app/pagination.py.",
        "app /x.py",
    ],
)
def test_bad_paths_are_rejected(tiny_repo, bad):
    with pytest.raises(ToolFailure) as exc:
        resolve_path(tiny_repo, bad)
    assert code_of(exc) == "PATH_OUTSIDE_WORKSPACE"


def test_non_string_path_is_invalid(tiny_repo):
    with pytest.raises(ToolFailure) as exc:
        resolve_path(tiny_repo, 5)  # type: ignore[arg-type]
    assert code_of(exc) == "INVALID_ARGUMENTS"


@pytest.mark.parametrize(
    "good,rel",
    [
        ("app/pagination.py", "app/pagination.py"),
        ("./app/../app/pagination.py", "app/pagination.py"),
        ("app\\pagination.py", "app/pagination.py"),
        ("", ""),
        (".", ""),
        ("app/does_not_exist.py", "app/does_not_exist.py"),
    ],
)
def test_good_paths_resolve_inside_workspace(tiny_repo, good, rel):
    resolved = resolve_path(tiny_repo, good)
    assert resolved.rel == rel
    assert is_within(resolved.abs_path, tiny_repo.resolve())


def test_sibling_directory_sharing_a_name_prefix_is_outside(tiny_repo):
    sibling = tiny_repo.parent / "repo-evil"
    write(sibling / "x.py", "x = 1\n")
    with pytest.raises(ToolFailure) as exc:
        resolve_path(tiny_repo, "../repo-evil/x.py")
    assert code_of(exc) == "PATH_OUTSIDE_WORKSPACE"
    assert not is_within(sibling, tiny_repo)


def test_symlink_pointing_outside_is_rejected(tiny_repo, tmp_path):
    outside = tmp_path / "outside.txt"
    write(outside, "secret\n")
    symlink_or_skip(tiny_repo / "app" / "leak.txt", outside)
    with pytest.raises(ToolFailure) as exc:
        resolve_path(tiny_repo, "app/leak.txt")
    assert code_of(exc) == "PATH_OUTSIDE_WORKSPACE"


def test_symlink_inside_resolves_to_its_real_target(tiny_repo):
    symlink_or_skip(tiny_repo / "app" / "alias.py", tiny_repo / "tests" / "test_pagination.py")
    resolved = resolve_path(tiny_repo, "app/alias.py")
    assert resolved.rel == "tests/test_pagination.py"
    with pytest.raises(ToolFailure) as exc:
        ensure_writable(resolved, DEFAULT_READ_DENYLIST, DEFAULT_PROTECTED_PATHS)
    assert code_of(exc) == "PATH_PROTECTED"


def test_denied_files_cannot_be_read(tiny_repo):
    for rel in (".env", ".env.local", "key.pem", ".git/config"):
        with pytest.raises(ToolFailure) as exc:
            ensure_readable(resolve_path(tiny_repo, rel), DEFAULT_READ_DENYLIST)
        assert code_of(exc) == "PATH_DENIED"


def test_protected_files_remain_readable(tiny_repo):
    ensure_readable(resolve_path(tiny_repo, "tests/test_pagination.py"), DEFAULT_READ_DENYLIST)


@pytest.mark.parametrize(
    "rel,code",
    [
        ("tests/test_pagination.py", "PATH_PROTECTED"),
        ("Tests/Test_Pagination.py", "PATH_PROTECTED"),
        ("pytest.ini", "PATH_PROTECTED"),
        (".github/workflows/ci.yml", "PATH_PROTECTED"),
        (".env", "PATH_DENIED"),
    ],
)
def test_writes_to_protected_or_denied_paths_are_rejected(tiny_repo, rel, code):
    with pytest.raises(ToolFailure) as exc:
        ensure_writable(resolve_path(tiny_repo, rel), DEFAULT_READ_DENYLIST, DEFAULT_PROTECTED_PATHS)
    assert code_of(exc) == code


def test_source_files_are_writable(tiny_repo):
    ensure_writable(resolve_path(tiny_repo, "app/pagination.py"), DEFAULT_READ_DENYLIST, DEFAULT_PROTECTED_PATHS)
