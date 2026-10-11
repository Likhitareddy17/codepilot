"""Tool registry and argument validation (TASK-011, TASK-023).

The registry exposes only the nine approved tools, validates every call with a
small hand-written validator (no ``jsonschema`` dependency, decision T9), runs
the tool, and always returns the standard result envelope. It never raises.
"""

from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Optional

from codepilot.errors import ToolFailure
from codepilot.models import ToolErrorModel, ToolResult
from codepilot.tools import ToolContext, ToolSpec, json_len
from codepilot.tools.filesystem import LIST_FILES, READ_FILE, SEARCH_CODE
from codepilot.tools.git import GIT_DIFF
from codepilot.tools.logs import READ_LOGS
from codepilot.tools.metrics import READ_METRICS
from codepilot.tools.patch import APPLY_PATCH

APPROVED_TOOLS = (
    "list_files", "read_file", "search_code", "run_tests", "read_logs",
    "read_metrics", "apply_patch", "git_diff", "get_tool_result",
)

_TYPES = {"string": str, "integer": int, "number": (int, float), "boolean": bool}


def _invalid(message: str) -> ToolFailure:
    return ToolFailure("INVALID_ARGUMENTS", message)


def validate_arguments(schema: Dict[str, Any], args: Any, max_arg_chars: int) -> Dict[str, Any]:
    """Validate ``args`` against a JSON-schema subset and apply defaults.

    Supports object properties, required, additionalProperties=false, type
    (string/integer/number/boolean), minimum, maximum, minLength, enum and
    pattern. Values are never coerced: ``"5"`` is not an integer.
    """
    if args is None:
        args = {}
    if not isinstance(args, dict):
        raise _invalid("Arguments must be a JSON object.")
    properties: Dict[str, Any] = schema.get("properties", {})
    if schema.get("additionalProperties") is False:
        extra = sorted(set(args) - set(properties))
        if extra:
            allowed = ", ".join(sorted(properties)) or "none"
            raise _invalid(f"Unexpected argument(s): {', '.join(extra)}. Allowed arguments: {allowed}.")
    for name in schema.get("required", []):
        if name not in args:
            raise _invalid(f"Missing required argument: {name}.")

    clean: Dict[str, Any] = {}
    for name, value in args.items():
        spec = properties[name]
        expected = spec.get("type")
        python_type = _TYPES.get(expected)
        if python_type is not None:
            wrong = not isinstance(value, python_type)
            if expected in ("integer", "number") and isinstance(value, bool):
                wrong = True
            if wrong:
                raise _invalid(f"Argument '{name}' must be of type {expected}.")
        if isinstance(value, str):
            if len(value) > max_arg_chars:
                raise _invalid(f"Argument '{name}' is too long ({len(value)} characters; limit {max_arg_chars}).")
            if len(value) < spec.get("minLength", 0):
                raise _invalid(f"Argument '{name}' must not be empty.")
            if "pattern" in spec and not re.search(spec["pattern"], value):
                raise _invalid(f"Argument '{name}' has an invalid format.")
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            if "minimum" in spec and value < spec["minimum"]:
                raise _invalid(f"Argument '{name}' must be at least {spec['minimum']}.")
            if "maximum" in spec and value > spec["maximum"]:
                raise _invalid(f"Argument '{name}' must be at most {spec['maximum']}.")
        if "enum" in spec and value not in spec["enum"]:
            raise _invalid(f"Argument '{name}' must be one of: {', '.join(map(str, spec['enum']))}.")
        clean[name] = value
    for name, spec in properties.items():
        if name not in clean and "default" in spec:
            clean[name] = spec["default"]
    return clean


class ToolRegistry:
    """Binds tool names to functions and executes calls safely."""

    def __init__(self, ctx: ToolContext) -> None:
        self.ctx = ctx
        self._tools: Dict[str, ToolSpec] = {}
        self._counter = 0
        self.internal_errors: List[str] = []

    def register(self, spec: ToolSpec) -> None:
        """Register one of the nine approved tools."""
        if spec.name not in APPROVED_TOOLS:
            raise ValueError(f"{spec.name!r} is not an approved tool.")
        if spec.name in self._tools:
            raise ValueError(f"{spec.name!r} is already registered.")
        self._tools[spec.name] = spec

    def tool_names(self) -> List[str]:
        return [name for name in APPROVED_TOOLS if name in self._tools]

    def llm_tool_schemas(self) -> List[Dict[str, Any]]:
        """Tool definitions in the OpenAI-compatible function-calling format."""
        return [
            {
                "type": "function",
                "function": {
                    "name": spec.name,
                    "description": spec.description,
                    "parameters": spec.parameters,
                },
            }
            for spec in (self._tools[n] for n in self.tool_names())
        ]

    def next_call_id(self) -> str:
        self._counter += 1
        return f"c{self._counter:03d}"

    def execute(self, name: Any, args: Any) -> ToolResult:
        """Run one tool call. Never raises; failures become ``ok=False`` results."""
        call_id = self.next_call_id()

        def fail(code: str, message: str) -> ToolResult:
            return ToolResult(call_id=call_id, ok=False, data={},
                              error=ToolErrorModel(code, message).to_dict())

        spec: Optional[ToolSpec] = self._tools.get(name) if isinstance(name, str) else None
        if spec is None:
            return fail("UNKNOWN_TOOL", f"Unknown tool {name!r}. Available tools: {', '.join(self.tool_names())}.")
        try:
            clean = validate_arguments(spec.parameters, args, self.ctx.settings.max_arg_chars)
            data, truncated = spec.func(self.ctx, clean)
        except ToolFailure as exc:
            return fail(exc.code, exc.message)
        except Exception as exc:  # noqa: BLE001 - tools must never crash the loop
            self.internal_errors.append(f"{call_id} {name}: {type(exc).__name__}: {exc}")
            return fail("INTERNAL_ERROR", f"Unexpected internal error in tool {name!r}. The error was logged.")

        budget = self.ctx.budget
        if json_len(data) > budget:  # safety net; tools normally fit themselves
            preview = json.dumps(data, ensure_ascii=False)[: max(budget - 60, 0)]
            data, truncated = {"preview": preview}, True
        return ToolResult(call_id=call_id, ok=True, data=data, truncated=truncated)


def build_default_registry(ctx: ToolContext) -> ToolRegistry:
    """Registry with every tool implemented so far.

    ``run_tests`` and ``get_tool_result`` are registered by later tasks.
    """
    registry = ToolRegistry(ctx)
    for spec in (LIST_FILES, READ_FILE, SEARCH_CODE, READ_LOGS, READ_METRICS, APPLY_PATCH, GIT_DIFF):
        registry.register(spec)
    return registry
