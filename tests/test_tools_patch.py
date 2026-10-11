"""Tests for apply_patch and revert (TASK-018)."""

import pytest

from conftest import symlink_or_skip, write
from codepilot.tools.patch import revert_patch


def call(registry, name, **args):
    return registry.execute(name, args)


def code(result) -> str:
    assert not result.ok, result.data
    return result.error["code"]


def patch(registry, path="app/pagination.py", old="start = page * size", new="start = (page - 1) * size"):
    return call(registry, "apply_patch", path=path, old=old, new=new)


def test_unique_match_is_applied_and_diffed(registry, ctx, tiny_repo):
    result = patch(registry)
    assert result.ok and not result.truncated
    assert "(page - 1) * size" in (tiny_repo / "app" / "pagination.py").read_text()
    data = result.data
    assert data["applied"] is True and data["syntax_ok"] is True
    assert data["patch_attempts_for_file"] == 1
    assert (data["lines_added"], data["lines_removed"]) == (1, 1)
    assert "--- a/app/pagination.py" in data["diff"] and "+    start = (page - 1) * size" in data["diff"]
    assert len(ctx.patches) == 1 and ctx.patches[0].applied and ctx.patches[0].path == "app/pagination.py"


def test_match_not_found(registry, tiny_repo):
    before = (tiny_repo / "app" / "pagination.py").read_bytes()
    result = patch(registry, old="does not exist")
    assert code(result) == "MATCH_NOT_FOUND" and "read_file" in result.error["message"]
    assert (tiny_repo / "app" / "pagination.py").read_bytes() == before


def test_match_not_unique(registry, tiny_repo):
    before = (tiny_repo / "app" / "pagination.py").read_bytes()
    result = patch(registry, old="start", new="begin")
    assert code(result) == "MATCH_NOT_UNIQUE" and "times" in result.error["message"]
    assert (tiny_repo / "app" / "pagination.py").read_bytes() == before


def test_crlf_file_matches_lf_old_text_and_keeps_crlf(registry, tiny_repo):
    result = patch(registry, path="app/crlf.py", old="x = 1\ny = 2", new="x = 1\ny = 3")
    assert result.ok
    assert (tiny_repo / "app" / "crlf.py").read_bytes() == b"x = 1\r\ny = 3\r\n"


def test_empty_new_deletes_the_old_text(registry, tiny_repo):
    result = patch(registry, path="app/orders.py", old="    # size of the order\n", new="")
    assert result.ok
    assert "# size of the order" not in (tiny_repo / "app" / "orders.py").read_text()


def test_syntax_error_is_rolled_back_byte_for_byte_but_counts(registry, ctx, tiny_repo):
    before = (tiny_repo / "app" / "pagination.py").read_bytes()
    result = patch(registry, old="return items[start:start + size]", new="return items[start:")
    assert code(result) == "SYNTAX_ERROR" and "rolled back" in result.error["message"]
    assert (tiny_repo / "app" / "pagination.py").read_bytes() == before
    assert ctx.patch_attempts["app/pagination.py"] == 1
    assert ctx.patches == [] and ctx.undo_stack == []


def test_per_file_limit_counts_written_patches_only(registry, ctx, tiny_repo):
    for _ in range(5):  # rejected calls must not use up the allowance (T6)
        assert code(patch(registry, old="nothing like this")) == "MATCH_NOT_FOUND"
    ctx.patch_attempts.clear()
    assert patch(registry, old="page * size", new="page * size  # 1").ok
    assert patch(registry, old="# 1", new="# 2").ok
    assert patch(registry, old="# 2", new="# 3").ok
    fourth = patch(registry, old="# 3", new="# 4")
    assert code(fourth) == "PATCH_LIMIT_REACHED"
    assert ctx.patch_attempts["app/pagination.py"] == 3


@pytest.mark.parametrize(
    "path,expected",
    [
        ("tests/test_pagination.py", "PATH_PROTECTED"),
        ("Tests/Test_Pagination.py", "PATH_PROTECTED"),
        ("app/test_helpers.py", "PATH_PROTECTED"),
        ("app/helpers_test.py", "PATH_PROTECTED"),
        ("conftest.py", "PATH_PROTECTED"),
        (".github/workflows/ci.yml", "PATH_PROTECTED"),
        ("pytest.ini", "PATH_PROTECTED"),
        ("pyproject.toml", "PATH_PROTECTED"),
        ("requirements.txt", "PATH_PROTECTED"),
        (".gitignore", "PATH_PROTECTED"),
        (".env", "PATH_DENIED"),
        ("../outside.py", "PATH_OUTSIDE_WORKSPACE"),
        ("/etc/hosts", "PATH_OUTSIDE_WORKSPACE"),
    ],
)
def test_protected_denied_and_escaping_paths_are_rejected(registry, path, expected):
    assert code(patch(registry, path=path, old="x", new="y")) == expected


def test_protected_test_file_is_unchanged_after_rejection(registry, tiny_repo):
    before = (tiny_repo / "tests" / "test_pagination.py").read_bytes()
    patch(registry, path="tests/test_pagination.py", old="== [1, 2]", new="== []")
    assert (tiny_repo / "tests" / "test_pagination.py").read_bytes() == before


@pytest.mark.parametrize("path", ["README.md", "logs/app.log", "metrics.json"])
def test_only_py_files_are_editable(registry, path):
    assert code(patch(registry, path=path, old="a", new="b")) == "FILE_TYPE_NOT_EDITABLE"


def test_new_files_cannot_be_created(registry, tiny_repo):
    result = patch(registry, path="app/new_module.py", old="a", new="b")
    assert code(result) == "NOT_FOUND" and "cannot create" in result.error["message"]
    assert not (tiny_repo / "app" / "new_module.py").exists()


def test_directory_target(registry):
    assert code(patch(registry, path="app", old="a", new="b")) == "NOT_A_FILE"


def test_identical_old_and_new(registry):
    assert code(patch(registry, old="page", new="page")) == "INVALID_ARGUMENTS"


def test_binary_and_non_utf8_python_files(registry, tiny_repo):
    write(tiny_repo / "app" / "blob.py", b"x = 1\x00\n")
    write(tiny_repo / "app" / "latin.py", b"x = '\xe9'\n")
    assert code(patch(registry, path="app/blob.py", old="x", new="y")) == "BINARY_FILE"
    assert code(patch(registry, path="app/latin.py", old="x", new="y")) == "BINARY_FILE"


def test_symlink_to_a_protected_file_is_rejected(registry, tiny_repo):
    symlink_or_skip(tiny_repo / "app" / "alias.py", tiny_repo / "tests" / "test_pagination.py")
    before = (tiny_repo / "tests" / "test_pagination.py").read_bytes()
    assert code(patch(registry, path="app/alias.py", old="paginate", new="x")) == "PATH_PROTECTED"
    assert (tiny_repo / "tests" / "test_pagination.py").read_bytes() == before


def test_extra_and_missing_arguments_are_invalid(registry):
    assert code(call(registry, "apply_patch", path="app/pagination.py", old="a")) == "INVALID_ARGUMENTS"
    assert code(call(registry, "apply_patch", path="app/pagination.py", old="a", new="b", force=True)) == "INVALID_ARGUMENTS"


def test_large_diff_output_is_capped_but_the_patch_record_is_complete(registry, ctx, tiny_repo):
    result = patch(registry, old="return items[start:start + size]",
                   new="return items[start:start + size]  # " + "z" * 6000)
    assert result.ok and result.truncated
    assert len(result.data["diff"]) < 6000
    assert len(ctx.patches[0].diff) > 6000


def test_revert_restores_exact_bytes_and_marks_the_record(registry, ctx, tiny_repo):
    original = (tiny_repo / "app" / "pagination.py").read_bytes()
    assert patch(registry).ok
    record = revert_patch(ctx, "app/pagination.py")
    assert record is not None and record.reverted
    assert (tiny_repo / "app" / "pagination.py").read_bytes() == original
    assert ctx.patch_attempts["app/pagination.py"] == 1   # reverted patches still count
    assert revert_patch(ctx) is None


def test_revert_undoes_the_most_recent_patch_first(registry, ctx, tiny_repo):
    first = (tiny_repo / "app" / "pagination.py").read_bytes()
    assert patch(registry, old="page * size", new="page * size  # a").ok
    second = (tiny_repo / "app" / "pagination.py").read_bytes()
    assert patch(registry, old="# a", new="# b").ok
    revert_patch(ctx)
    assert (tiny_repo / "app" / "pagination.py").read_bytes() == second
    revert_patch(ctx)
    assert (tiny_repo / "app" / "pagination.py").read_bytes() == first
