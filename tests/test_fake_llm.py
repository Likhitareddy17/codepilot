"""Tests for FakeLLM and the deterministic scenarios (TASK-032)."""

import pytest

from codepilot.errors import LLMUnavailableError
from codepilot.llm import fake
from codepilot.llm.client import LLMClient, LLMResponse
from codepilot.llm.fake import FakeLLM, ScriptExhausted
from codepilot.models import ToolCall

MESSAGES = [{"role": "user", "content": "hi"}]
TOOLS = [{"type": "function", "function": {"name": "x"}}]
PATCH = ("app/pagination.py", "start = page * size", "start = (page - 1) * size")


def names(response: LLMResponse):
    return [c.name for c in response.tool_calls]


def drain(script):
    llm = FakeLLM(script)
    out = []
    for _ in range(len(script)):
        try:
            out.append(llm.chat(MESSAGES, TOOLS))
        except LLMUnavailableError as exc:
            out.append(exc)
    return out


def test_fake_llm_implements_the_client_interface():
    assert isinstance(FakeLLM([]), LLMClient)


def test_responses_are_returned_in_order():
    llm = FakeLLM([fake.say("one"), fake.say("two")])
    assert [llm.chat(MESSAGES, TOOLS).text, llm.chat(MESSAGES, TOOLS).text] == ["one", "two"]
    assert llm.call_count == 2 and llm.remaining() == 0
    llm.assert_exhausted()


def test_an_exhausted_script_fails_loudly():
    llm = FakeLLM([fake.say("only")])
    llm.chat(MESSAGES, TOOLS)
    with pytest.raises(ScriptExhausted):
        llm.chat(MESSAGES, TOOLS)
    assert issubclass(ScriptExhausted, AssertionError)


def test_unused_responses_are_detected():
    llm = FakeLLM([fake.say("a"), fake.say("b")])
    llm.chat(MESSAGES, TOOLS)
    with pytest.raises(AssertionError, match="never used"):
        llm.assert_exhausted()


def test_every_call_is_recorded_with_deep_copies():
    llm = FakeLLM([fake.say("a")])
    messages = [{"role": "user", "content": "original"}]
    response = llm.chat(messages, TOOLS)
    messages[0]["content"] = "changed later"
    response.text = "mutated"
    record = llm.calls[0]
    assert record.messages[0]["content"] == "original"
    assert record.tools == TOOLS and record.response.text == "a"


def test_returned_responses_are_copies_of_the_script():
    script = [fake.respond(fake.call("list_files"))]
    llm = FakeLLM(script)
    llm.chat(MESSAGES, TOOLS).tool_calls[0].name = "tampered"
    assert script[0].tool_calls[0].name == "list_files" and script[0].tool_calls[0].id == ""


def test_tool_call_ids_are_assigned_deterministically():
    llm = FakeLLM([fake.respond(fake.call("a"), fake.call("b")), fake.respond(fake.call("c"))])
    ids = [c.id for r in (llm.chat(MESSAGES, TOOLS), llm.chat(MESSAGES, TOOLS)) for c in r.tool_calls]
    assert ids == ["call_001", "call_002", "call_003"]


def test_explicit_ids_are_kept():
    llm = FakeLLM([LLMResponse(tool_calls=[ToolCall(id="mine", name="a", arguments={})])])
    assert llm.chat(MESSAGES, TOOLS).tool_calls[0].id == "mine"


def test_reactive_responses_see_the_incoming_messages():
    def react(messages, tools):
        return fake.say(f"saw {len(messages)} message(s) and {len(tools)} tool(s)")

    assert FakeLLM([react]).chat(MESSAGES, TOOLS).text == "saw 1 message(s) and 1 tool(s)"


def test_provider_failures_are_raised_and_recorded():
    llm = FakeLLM([LLMUnavailableError("down")])
    with pytest.raises(LLMUnavailableError, match="down"):
        llm.chat(MESSAGES, TOOLS)
    assert llm.calls[0].response is None and isinstance(llm.calls[0].error, LLMUnavailableError)


# ------------------------------------------------------------- scenarios


def test_normal_repair_investigates_reads_patches_then_reports():
    script = fake.normal_repair(PATCH)
    r = drain(script)
    assert names(r[0]) == ["list_files", "read_logs", "read_metrics"]
    assert names(r[1]) == ["read_file", "apply_patch"]
    assert r[1].tool_calls[1].arguments == {"path": PATCH[0], "old": PATCH[1], "new": PATCH[2]}
    assert r[2].text == fake.VERIFIED_TEXT and not r[2].tool_calls


def test_multiple_patches_scenario():
    patches = [PATCH, ("app/orders.py", "a", "b"), ("app/pricing.py", "c", "d")]
    r = drain(fake.multiple_patches(patches))
    assert [names(x) for x in r[1:4]] == [["read_file", "apply_patch"]] * 3 and len(r) == 5
    with pytest.raises(ValueError):
        fake.multiple_patches([PATCH])


def test_max_iterations_scenario_never_patches():
    r = drain(fake.max_iterations(6))
    assert len(r) == 6 and all(names(x) == ["list_files"] for x in r)


def test_no_progress_and_regression_scenarios_apply_the_given_patches():
    assert [names(x) for x in drain(fake.no_progress(PATCH, PATCH))[1:3]] == [["apply_patch"], ["apply_patch"]]
    r = drain(fake.regression(PATCH, then=("app/orders.py", "a", "b")))
    assert names(r[1]) == ["apply_patch"] and r[2].tool_calls[0].arguments["path"] == "app/orders.py"
    assert drain(fake.regression(PATCH))[2].text == fake.VERIFIED_TEXT
    assert len(drain(fake.repeated_regression(PATCH, PATCH))) == 4


def test_protected_path_attack_targets_a_test_file():
    r = drain(fake.protected_path_attack())
    assert r[1].tool_calls[0].arguments["path"].startswith("tests/")


def test_malformed_scenario_flags_every_call():
    r = drain(fake.malformed_tool_calls(3))
    assert len(r) == 3 and all(x.tool_calls[0].malformed and x.tool_calls[0].raw_arguments for x in r)


def test_unknown_tool_and_false_success_scenarios():
    r = drain(fake.unknown_tool())
    assert names(r[0]) == ["run_shell"]
    f = drain(fake.false_success())
    assert all(not x.tool_calls and "all tests pass" in x.text for x in f)


def test_llm_unavailable_scenario_fails_after_normal_turns():
    r = drain(fake.llm_unavailable(after=2))
    assert isinstance(r[2], LLMUnavailableError) and len(r) == 3


# ------------------------------------------------- scenarios against real tools


def test_scenario_tool_calls_run_against_the_real_registry(registry):
    def run(response):
        return [registry.execute(c.name, c.arguments) for c in response.tool_calls]

    patched = drain(fake.normal_repair(PATCH))[1]
    results = run(patched)
    assert results[0].ok and results[1].ok and results[1].data["applied"] is True

    attack = run(drain(fake.protected_path_attack("tests/test_pagination.py", "assert", "pass"))[1])
    assert attack[0].error["code"] == "PATH_PROTECTED"
    assert run(drain(fake.unknown_tool())[0])[0].error["code"] == "UNKNOWN_TOOL"
    assert run(drain(fake.max_iterations(1))[0])[0].ok
