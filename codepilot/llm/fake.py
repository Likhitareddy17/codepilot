"""Offline FakeLLM and deterministic scenario scripts (TASK-032).

The automated test suite never makes a live request: it drives the controller
with scripted responses built here. Scenario builders return plain script
lists, so they depend on nothing but the data types.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple, Union

from codepilot.errors import LLMUnavailableError
from codepilot.llm.client import LLMClient, LLMResponse
from codepilot.models import ToolCall

VERIFIED_TEXT = "The latest full-suite verification passed. No further source changes are indicated."
FALSE_SUCCESS_TEXT = "I fixed the bug and all tests pass now. The work is complete."

Patch = Tuple[str, str, str]  # (path, old, new)
ScriptItem = Union[LLMResponse, BaseException, Callable[[List[Dict[str, Any]], List[Dict[str, Any]]], Any]]


class ScriptExhausted(AssertionError):
    """The controller asked the FakeLLM for more responses than the script holds."""


@dataclass
class FakeCall:
    """One recorded LLM call: deep copies of what went in and came out."""

    messages: List[Dict[str, Any]]
    tools: List[Dict[str, Any]]
    response: Optional[LLMResponse]
    error: Optional[BaseException] = None


class FakeLLM(LLMClient):
    """Scripted LLM: responses, exceptions, or callables reacting to the messages."""

    def __init__(self, script: Sequence[ScriptItem]) -> None:
        self._script: List[ScriptItem] = list(script)
        self._position = 0
        self._id_counter = 0
        self.calls: List[FakeCall] = []

    @property
    def call_count(self) -> int:
        return len(self.calls)

    def remaining(self) -> int:
        return len(self._script) - self._position

    def assert_exhausted(self) -> None:
        """Fail if the controller stopped before using the whole script."""
        if self.remaining():
            raise AssertionError(f"{self.remaining()} scripted response(s) were never used.")

    def chat(self, messages: List[Dict[str, Any]], tools: List[Dict[str, Any]]) -> LLMResponse:
        recorded_messages, recorded_tools = copy.deepcopy(messages), copy.deepcopy(tools)
        if self._position >= len(self._script):
            raise ScriptExhausted(f"FakeLLM script exhausted after {self._position} call(s).")
        item = self._script[self._position]
        self._position += 1
        if callable(item) and not isinstance(item, (LLMResponse, BaseException)):
            item = item(copy.deepcopy(messages), copy.deepcopy(tools))
        if isinstance(item, BaseException):
            self.calls.append(FakeCall(recorded_messages, recorded_tools, None, item))
            raise item
        response = copy.deepcopy(item)
        for call in response.tool_calls:
            if not call.id:
                self._id_counter += 1
                call.id = f"call_{self._id_counter:03d}"
        self.calls.append(FakeCall(recorded_messages, recorded_tools, copy.deepcopy(response)))
        return response


# ------------------------------------------------------------------ builders


def call(name: str, **arguments: Any) -> ToolCall:
    """A tool call; the FakeLLM assigns its id."""
    return ToolCall(id="", name=name, arguments=arguments)


def respond(*tool_calls: ToolCall, text: Optional[str] = None) -> LLMResponse:
    return LLMResponse(text=text, tool_calls=list(tool_calls), finish_reason="tool_calls" if tool_calls else "stop")


def say(text: str) -> LLMResponse:
    return respond(text=text)


def malformed(name: str = "read_file", raw: str = '{"path": "app/x.py"') -> LLMResponse:
    return LLMResponse(
        tool_calls=[ToolCall(id="", name=name, arguments={}, malformed=True, raw_arguments=raw)],
        finish_reason="tool_calls",
    )


def investigate() -> LLMResponse:
    """First turn of a sensible agent: look around before touching anything."""
    return respond(call("list_files"), call("read_logs"), call("read_metrics"))


def _patch(patch: Patch) -> ToolCall:
    path, old, new = patch
    return call("apply_patch", path=path, old=old, new=new)


def repair_script(patches: Sequence[Patch]) -> List[ScriptItem]:
    """Investigate, then read and patch one file per iteration, then report."""
    script: List[ScriptItem] = [investigate()]
    for patch in patches:
        script.append(respond(call("read_file", path=patch[0]), _patch(patch)))
    script.append(say(VERIFIED_TEXT))
    return script


def normal_repair(patch: Patch) -> List[ScriptItem]:
    return repair_script([patch])


def multiple_patches(patches: Sequence[Patch]) -> List[ScriptItem]:
    if len(patches) < 2:
        raise ValueError("multiple_patches needs at least two patches")
    return repair_script(patches)


def max_iterations(count: int) -> List[ScriptItem]:
    """The model keeps looking and never patches: ``count`` identical turns."""
    return [respond(call("list_files", path="app")) for _ in range(count)]


def no_progress(first: Patch, second: Patch) -> List[ScriptItem]:
    """Two patches that leave the same failures behind."""
    return [investigate(), respond(_patch(first)), respond(_patch(second)), say(VERIFIED_TEXT)]


def regression(bad_patch: Patch, then: Optional[Patch] = None) -> List[ScriptItem]:
    """A patch that breaks a passing test; optionally followed by a better one."""
    script: List[ScriptItem] = [investigate(), respond(_patch(bad_patch))]
    script.append(respond(_patch(then)) if then else say(VERIFIED_TEXT))
    return script


def repeated_regression(first_bad: Patch, second_bad: Patch) -> List[ScriptItem]:
    return [investigate(), respond(_patch(first_bad)), respond(_patch(second_bad)), say(VERIFIED_TEXT)]


def protected_path_attack(path: str = "tests/test_pricing.py", old: str = "assert", new: str = "pass  # assert") -> List[ScriptItem]:
    """The model tries to edit a test file."""
    return [investigate(), respond(_patch((path, old, new))), say(VERIFIED_TEXT)]


def malformed_tool_calls(times: int = 3) -> List[ScriptItem]:
    return [malformed() for _ in range(times)]


def unknown_tool(name: str = "run_shell", **arguments: Any) -> List[ScriptItem]:
    return [respond(call(name, **(arguments or {"command": "rm -rf /"}))), say(VERIFIED_TEXT)]


def false_success() -> List[ScriptItem]:
    """The model claims success without any verification."""
    return [say(FALSE_SUCCESS_TEXT), say(FALSE_SUCCESS_TEXT)]


def llm_unavailable(after: int = 1, message: str = "simulated provider outage") -> List[ScriptItem]:
    """``after`` normal turns, then the provider fails."""
    script: List[ScriptItem] = [investigate() for _ in range(after)]
    script.append(LLMUnavailableError(message))
    return script
