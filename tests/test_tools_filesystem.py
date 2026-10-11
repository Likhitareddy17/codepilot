"""Tests for list_files, read_file and search_code (TASK-012, 013, 014)."""

import json
import time

import pytest

from conftest import symlink_or_skip, write


def call(registry, name, **args):
    return registry.execute(name, args)


def code(result) -> str:
    assert not result.ok, result.data
    return result.error["code"]


# ---------------------------------------------------------------- list_files


def test_list_files_root_is_sorted_and_hides_skipped_and_denied(registry):
    result = call(registry, "list_files")
    assert result.ok
    paths = [e["path"] for e in result.data["entries"]]
    assert paths == sorted(paths)
    assert "app/pagination.py" in paths and "tests/test_pagination.py" in paths
    joined = " ".join(paths)
    for hidden in (".env", "key.pem", ".git", "__pycache__"):
        assert hidden not in joined
    assert result.data["total_entries"] == len(paths)


def test_list_files_marks_protected_entries(registry):
    entries = {e["path"]: e for e in call(registry, "list_files").data["entries"]}
    assert entries["tests/test_pagination.py"]["protected"] is True
    assert entries["pytest.ini"]["protected"] is True
    assert entries["app/pagination.py"]["protected"] is False
    assert entries["app"]["type"] == "dir" and "protected" not in entries["app"]
    assert entries["app/pagination.py"]["size_bytes"] > 0


def test_list_files_depth_and_subdirectory(registry):
    shallow = [e["path"] for e in call(registry, "list_files", max_depth=1).data["entries"]]
    assert "app" in shallow and "app/pagination.py" not in shallow
    sub = call(registry, "list_files", path="app")
    assert sub.data["path"] == "app"
    assert all(e["path"].startswith("app/") for e in sub.data["entries"])


@pytest.mark.parametrize(
    "args,expected",
    [
        ({"path": ".git"}, "PATH_DENIED"),
        ({"path": ".."}, "PATH_OUTSIDE_WORKSPACE"),
        ({"path": "C:\\"}, "PATH_OUTSIDE_WORKSPACE"),
        ({"path": "nope"}, "NOT_FOUND"),
        ({"path": "README.md"}, "NOT_A_DIRECTORY"),
        ({"max_depth": 7}, "INVALID_ARGUMENTS"),
        ({"max_depth": 0}, "INVALID_ARGUMENTS"),
    ],
)
def test_list_files_errors(registry, args, expected):
    assert code(call(registry, "list_files", **args)) == expected


def test_list_files_entry_cap(registry, ctx):
    ctx.settings.max_list_entries = 3
    result = call(registry, "list_files")
    assert len(result.data["entries"]) == 3
    assert result.truncated
    assert result.data["total_entries"] > 3


def test_list_files_omits_symlinks_that_leave_the_workspace(registry, tiny_repo, tmp_path):
    write(tmp_path / "outside.txt", "x\n")
    symlink_or_skip(tiny_repo / "app" / "leak.txt", tmp_path / "outside.txt")
    paths = [e["path"] for e in call(registry, "list_files").data["entries"]]
    assert "app/leak.txt" not in paths


# ----------------------------------------------------------------- read_file


def test_read_file_returns_raw_text_without_line_prefixes(registry):
    result = call(registry, "read_file", path="app/pagination.py")
    assert result.ok and not result.truncated
    assert result.data["content"].startswith("def paginate(items, page, size):\n    start")
    assert (result.data["start_line"], result.data["end_line"], result.data["total_lines"]) == (1, 3, 3)


def test_read_file_line_range(registry):
    result = call(registry, "read_file", path="app/pagination.py", start_line=2, end_line=3)
    assert result.data["content"] == "    start = page * size\n    return items[start:start + size]"
    assert result.data["start_line"] == 2 and result.data["end_line"] == 3


def test_read_file_normalises_crlf_in_content(registry):
    assert call(registry, "read_file", path="app/crlf.py").data["content"] == "x = 1\ny = 2"


def test_read_file_protected_files_are_readable(registry):
    assert call(registry, "read_file", path="tests/test_pagination.py").ok


@pytest.mark.parametrize(
    "args,expected",
    [
        ({"path": ".env"}, "PATH_DENIED"),
        ({"path": "key.pem"}, "PATH_DENIED"),
        ({"path": ".git/config"}, "PATH_DENIED"),
        ({"path": "../outside.txt"}, "PATH_OUTSIDE_WORKSPACE"),
        ({"path": "missing.py"}, "NOT_FOUND"),
        ({"path": "app"}, "NOT_A_FILE"),
        ({"path": "data.bin"}, "BINARY_FILE"),
        ({"path": "app/pagination.py", "start_line": 3, "end_line": 2}, "INVALID_ARGUMENTS"),
        ({"path": "app/pagination.py", "start_line": 99}, "INVALID_ARGUMENTS"),
    ],
)
def test_read_file_errors(registry, args, expected):
    assert code(call(registry, "read_file", **args)) == expected


def test_read_file_replaces_invalid_utf8_instead_of_failing(registry, tiny_repo):
    write(tiny_repo / "latin.txt", b"caf\xe9\n")
    result = call(registry, "read_file", path="latin.txt")
    assert result.ok and "caf\ufffd" in result.data["content"]


def test_read_file_empty_file(registry, tiny_repo):
    write(tiny_repo / "empty.txt", "")
    result = call(registry, "read_file", path="empty.txt")
    assert result.ok and result.data["content"] == "" and result.data["total_lines"] == 0


@pytest.fixture
def long_file(tiny_repo):
    write(tiny_repo / "app" / "long.py", "".join(f"x{i} = {i}\n" for i in range(1, 601)))
    return "app/long.py"


def test_read_file_line_cap(registry, ctx, long_file):
    ctx.settings.max_tool_output_chars = 100_000
    ctx.settings.max_read_lines = 50
    capped = call(registry, "read_file", path=long_file)
    assert capped.data["end_line"] == 50 and capped.truncated
    within = call(registry, "read_file", path=long_file, end_line=30)
    assert within.data["end_line"] == 30 and not within.truncated
    over = call(registry, "read_file", path=long_file, end_line=100)
    assert over.data["end_line"] == 50 and over.truncated


def test_read_file_output_budget(registry, ctx, long_file):
    result = call(registry, "read_file", path=long_file)
    assert result.ok and result.truncated
    assert len(json.dumps(result.data)) <= ctx.settings.max_tool_output_chars
    assert result.data["end_line"] < 400
    assert result.data["content"].endswith(f"x{result.data['end_line']} = {result.data['end_line']}")


def test_read_file_single_huge_line_is_cut_inside_the_line(registry, ctx, tiny_repo):
    write(tiny_repo / "wide.txt", "y" * 50_000 + "\n")
    result = call(registry, "read_file", path="wide.txt")
    assert result.ok and result.truncated
    assert len(json.dumps(result.data)) <= ctx.settings.max_tool_output_chars


# -------------------------------------------------------------- search_code


def test_search_code_literal_default_is_case_insensitive(registry):
    result = call(registry, "search_code", pattern="PAGINATE")
    assert result.ok
    hits = {(m["path"], m["line"]) for m in result.data["matches"]}
    assert ("app/pagination.py", 1) in hits
    assert any(path.startswith("tests/") for path, _ in hits)
    assert result.data["match_count"] == len(result.data["matches"])


def test_search_code_case_sensitive(registry):
    assert call(registry, "search_code", pattern="PAGINATE", case_sensitive=True).data["match_count"] == 0
    assert call(registry, "search_code", pattern="paginate", case_sensitive=True).data["match_count"] > 0


def test_search_code_is_literal_unless_regex_requested(registry):
    literal = call(registry, "search_code", pattern="start = page * size")
    assert literal.data["match_count"] == 1
    assert literal.data["matches"][0]["text"] == "start = page * size"


def test_search_code_regex(registry):
    result = call(registry, "search_code", pattern=r"def \w+\(", regex=True)
    assert result.ok
    assert any(m["path"] == "app/pagination.py" and m["line"] == 1 for m in result.data["matches"])


def test_search_code_invalid_regex(registry):
    result = call(registry, "search_code", pattern="a(", regex=True)
    assert code(result) == "INVALID_ARGUMENTS"
    assert "regular expression" in result.error["message"]


def test_search_code_skips_denied_binary_and_skipped_directories(registry):
    result = call(registry, "search_code", pattern="needle")
    assert [m["path"] for m in result.data["matches"]] == ["README.md"]


def test_search_code_path_scope_and_errors(registry):
    scoped = call(registry, "search_code", pattern="paginate", path="tests")
    assert {m["path"] for m in scoped.data["matches"]} == {"tests/test_pagination.py"}
    assert call(registry, "search_code", pattern="x", path="app/pagination.py").ok
    assert code(call(registry, "search_code", pattern="x", path=".git")) == "PATH_DENIED"
    assert code(call(registry, "search_code", pattern="x", path="..")) == "PATH_OUTSIDE_WORKSPACE"
    assert code(call(registry, "search_code", pattern="x", path="nope")) == "NOT_FOUND"


def test_search_code_result_and_line_caps(registry, ctx):
    ctx.settings.max_search_results = 2
    ctx.settings.max_search_line_chars = 10
    result = call(registry, "search_code", pattern="e")
    assert len(result.data["matches"]) == 2
    assert result.data["match_count"] > 2 and result.truncated
    assert all(len(m["text"]) <= 10 for m in result.data["matches"])


def test_search_code_regex_runs_in_a_killable_process_and_times_out(registry, ctx, tiny_repo):
    write(tiny_repo / "evil.txt", "a" * 40 + "!\n")
    ctx.settings.command_timeout_s = 1
    started = time.monotonic()
    result = call(registry, "search_code", pattern=r"^(a+)+$", regex=True, path="evil.txt")
    assert code(result) == "TIMEOUT"
    assert time.monotonic() - started < 30
