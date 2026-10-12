"""Tests for the tool registry and argument validation (TASK-011, TASK-023)."""

import pytest

from codepilot.errors import ToolFailure
from codepilot.tools import ToolSpec
from codepilot.tools.registry import APPROVED_TOOLS, ToolRegistry, build_default_registry, validate_arguments


def error_code(result) -> str:
    assert not result.ok
    return result.error["code"]


def test_default_registry_exposes_only_implemented_approved_tools(registry):
    assert registry.tool_names() == [
        "list_files", "read_file", "search_code", "run_tests", "read_logs",
        "read_metrics", "apply_patch", "git_diff",
    ]
    assert set(registry.tool_names()) <= set(APPROVED_TOOLS)
    assert len(APPROVED_TOOLS) == 9


def test_unapproved_or_duplicate_tools_cannot_be_registered(ctx):
    registry = ToolRegistry(ctx)
    with pytest.raises(ValueError, match="not an approved tool"):
        registry.register(ToolSpec("run_shell", "x", {"type": "object"}, lambda c, a: ({}, False)))
    spec = ToolSpec("read_metrics", "x", {"type": "object", "properties": {}}, lambda c, a: ({}, False))
    registry.register(spec)
    with pytest.raises(ValueError, match="already registered"):
        registry.register(spec)


@pytest.mark.parametrize("name", ["run_command", "bash", "git_push", "", None, 5, "LIST_FILES"])
def test_unknown_tool_names(registry, name):
    assert error_code(registry.execute(name, {})) == "UNKNOWN_TOOL"


def test_extra_argument_is_rejected(registry):
    result = registry.execute("read_file", {"path": "README.md", "mode": "w"})
    assert error_code(result) == "INVALID_ARGUMENTS"
    assert "mode" in result.error["message"]


@pytest.mark.parametrize(
    "args",
    [
        {},                                    # missing required
        {"path": 5},                           # wrong type
        {"path": "README.md", "start_line": "1"},   # no string -> int coercion
        {"path": "README.md", "start_line": True},  # bool is not an integer
        {"path": "README.md", "start_line": 0},     # below minimum
        {"path": "README.md", "start_line": 1.5},   # float is not an integer
    ],
)
def test_invalid_read_file_arguments(registry, args):
    assert error_code(registry.execute("read_file", args)) == "INVALID_ARGUMENTS"


@pytest.mark.parametrize("args", [None, "text", ["a"], 7])
def test_arguments_must_be_an_object(registry, args):
    assert error_code(registry.execute("read_file", args)) == "INVALID_ARGUMENTS"


def test_no_argument_tools_accept_none_and_empty(registry):
    assert registry.execute("read_metrics", None).ok
    assert registry.execute("read_metrics", {}).ok


def test_oversized_string_argument_is_rejected(registry):
    result = registry.execute("apply_patch", {"path": "app/pagination.py", "old": "x" * 20001, "new": "y"})
    assert error_code(result) == "INVALID_ARGUMENTS"
    assert "too long" in result.error["message"]


def test_empty_required_string_is_rejected(registry):
    assert error_code(registry.execute("search_code", {"pattern": ""})) == "INVALID_ARGUMENTS"


def test_call_ids_are_sequential_and_assigned_even_on_failure(registry):
    ids = [
        registry.execute("nope", {}).call_id,
        registry.execute("read_file", {}).call_id,
        registry.execute("read_file", {"path": "README.md"}).call_id,
    ]
    assert ids == ["c001", "c002", "c003"]


def test_result_envelope_shape(registry):
    ok = registry.execute("read_file", {"path": "README.md"}).to_dict()
    bad = registry.execute("read_file", {"path": "missing.txt"}).to_dict()
    assert set(ok) == {"call_id", "ok", "data", "error", "truncated"}
    assert ok["ok"] is True and ok["error"] is None
    assert bad["ok"] is False and bad["data"] == {}
    assert set(bad["error"]) == {"code", "message"}


def test_defaults_are_applied_by_the_registry(ctx):
    seen = {}

    def echo(context, args):
        seen.update(args)
        return {}, False

    registry = ToolRegistry(ctx)
    registry.register(ToolSpec("list_files", "x", {
        "type": "object",
        "properties": {"path": {"type": "string", "default": "."}, "flag": {"type": "boolean", "default": False}},
        "additionalProperties": False,
    }, echo))
    assert registry.execute("list_files", {}).ok
    assert seen == {"path": ".", "flag": False}


def test_tool_failure_becomes_error_result(ctx):
    def refuse(context, args):
        raise ToolFailure("MATCH_NOT_FOUND", "old text missing")

    registry = ToolRegistry(ctx)
    registry.register(ToolSpec("apply_patch", "x", {"type": "object", "properties": {}}, refuse))
    result = registry.execute("apply_patch", {})
    assert error_code(result) == "MATCH_NOT_FOUND"
    assert result.error["message"] == "old text missing"


def test_unexpected_exception_is_contained_and_logged(ctx):
    def explode(context, args):
        raise RuntimeError("secret internal detail")

    registry = ToolRegistry(ctx)
    registry.register(ToolSpec("git_diff", "x", {"type": "object", "properties": {}}, explode))
    result = registry.execute("git_diff", {})
    assert error_code(result) == "INTERNAL_ERROR"
    assert "secret internal detail" not in result.error["message"]
    assert "RuntimeError" in registry.internal_errors[0]


def test_oversized_output_is_cut_by_the_safety_net(ctx):
    registry = ToolRegistry(ctx)
    registry.register(ToolSpec("read_metrics", "x", {"type": "object", "properties": {}},
                               lambda c, a: ({"big": "x" * 10000}, False)))
    result = registry.execute("read_metrics", {})
    assert result.ok and result.truncated
    assert len(result.data["preview"]) <= ctx.settings.max_tool_output_chars


def test_llm_tool_schemas_use_function_calling_format(registry):
    schemas = registry.llm_tool_schemas()
    assert [s["function"]["name"] for s in schemas] == registry.tool_names()
    for schema in schemas:
        assert schema["type"] == "function"
        assert schema["function"]["description"]
        assert schema["function"]["parameters"]["additionalProperties"] is False


def test_validate_arguments_enum_pattern_and_maximum():
    schema = {
        "type": "object",
        "properties": {
            "mode": {"type": "string", "enum": ["a", "b"]},
            "call_id": {"type": "string", "pattern": "^c[0-9]{3,}$"},
            "depth": {"type": "integer", "maximum": 6},
        },
        "additionalProperties": False,
    }
    assert validate_arguments(schema, {"mode": "a", "call_id": "c004", "depth": 6}, 100)
    for bad in ({"mode": "z"}, {"call_id": "x1"}, {"depth": 7}):
        with pytest.raises(ToolFailure) as exc:
            validate_arguments(schema, bad, 100)
        assert exc.value.code == "INVALID_ARGUMENTS"
