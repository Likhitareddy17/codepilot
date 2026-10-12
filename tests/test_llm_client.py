"""Tests for the LLM interface and the Groq client (TASK-030, TASK-031). No network is used."""

import logging
from types import SimpleNamespace

import httpx
import openai
import pytest

from codepilot.config import Settings
from codepilot.errors import LLMError, LLMUnavailableError, RateLimitError
from codepilot.llm.client import GroqClient, LLMClient, LLMResponse, estimate_tokens, parse_tool_arguments

KEY = "gsk_test_secret_key_123456"
REQUEST = httpx.Request("POST", "https://api.example.test/v1/chat/completions")


def response_for(status, headers=None):
    return httpx.Response(status, headers=headers or {}, request=REQUEST)


def rate_limited(retry_after=None):
    headers = {"retry-after": str(retry_after)} if retry_after is not None else {}
    return openai.RateLimitError(f"rate limit for key {KEY}", response=response_for(429, headers), body=None)


def status_error(code):
    cls = {401: openai.AuthenticationError, 403: openai.PermissionDeniedError, 400: openai.BadRequestError,
           404: openai.NotFoundError, 500: openai.InternalServerError, 503: openai.InternalServerError}[code]
    return cls(f"HTTP {code} Authorization: Bearer {KEY}", response=response_for(code), body=None)


def completion(content=None, tool_calls=None, finish="stop", usage=(10, 5, 15)):
    message = SimpleNamespace(content=content, tool_calls=tool_calls)
    u = SimpleNamespace(prompt_tokens=usage[0], completion_tokens=usage[1], total_tokens=usage[2]) if usage else None
    return SimpleNamespace(choices=[SimpleNamespace(message=message, finish_reason=finish)], usage=u)


def tool_call(name, arguments, call_id="call_abc"):
    return SimpleNamespace(id=call_id, function=SimpleNamespace(name=name, arguments=arguments))


class ScriptedSDK:
    """Stands in for openai.OpenAI: pops a scripted item per create() call."""

    def __init__(self, *items):
        self.items = list(items)
        self.requests = []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    def _create(self, **kwargs):
        self.requests.append(kwargs)
        item = self.items.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item


@pytest.fixture
def settings():
    return Settings(llm_api_key=KEY, llm_model="test-model", llm_max_retries=3)


def make_client(settings, *items):
    sleeps = []
    sdk = ScriptedSDK(*items)
    return GroqClient(settings, sdk_client=sdk, sleep=sleeps.append), sdk, sleeps


MESSAGES = [{"role": "system", "content": "s"}, {"role": "user", "content": "u"}]
TOOLS = [{"type": "function", "function": {"name": "read_file", "description": "d", "parameters": {"type": "object"}}}]


# ------------------------------------------------------------ the interface


def test_llm_client_is_an_abstract_interface():
    with pytest.raises(TypeError):
        LLMClient()  # type: ignore[abstract]
    assert issubclass(GroqClient, LLMClient)


# --------------------------------------------------------- normalisation


def test_text_only_response(settings):
    client, _, _ = make_client(settings, completion(content="All done", finish="stop"))
    r = client.chat(MESSAGES, TOOLS)
    assert isinstance(r, LLMResponse)
    assert (r.text, r.tool_calls, r.finish_reason) == ("All done", [], "stop")
    assert r.usage == {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}


def test_tool_calls_are_parsed_into_dicts(settings):
    calls = [tool_call("read_file", '{"path": "app/x.py", "start_line": 2}', "c_1"), tool_call("list_files", "{}", "c_2")]
    client, _, _ = make_client(settings, completion(tool_calls=calls, finish="tool_calls"))
    r = client.chat(MESSAGES, TOOLS)
    assert [(c.id, c.name, c.arguments, c.malformed) for c in r.tool_calls] == [
        ("c_1", "read_file", {"path": "app/x.py", "start_line": 2}, False),
        ("c_2", "list_files", {}, False),
    ]
    assert r.text is None and r.finish_reason == "tool_calls"


@pytest.mark.parametrize("raw", ["", "   ", None])
def test_empty_arguments_mean_no_arguments(settings, raw):
    client, _, _ = make_client(settings, completion(tool_calls=[tool_call("read_metrics", raw)]))
    call = client.chat(MESSAGES, TOOLS).tool_calls[0]
    assert call.arguments == {} and not call.malformed


@pytest.mark.parametrize("raw", ['{"path": "app/x.py"', "not json", "[1, 2]", '"a string"', "42"])
def test_malformed_arguments_are_flagged_not_raised(settings, raw):
    client, _, _ = make_client(settings, completion(tool_calls=[tool_call("read_file", raw)]))
    call = client.chat(MESSAGES, TOOLS).tool_calls[0]
    assert call.malformed and call.arguments == {} and call.raw_arguments == raw and call.name == "read_file"


def test_missing_tool_call_ids_are_generated(settings):
    client, _, _ = make_client(settings, completion(tool_calls=[tool_call("a", "{}", None), tool_call("b", "{}", "")]))
    assert [c.id for c in client.chat(MESSAGES, TOOLS).tool_calls] == ["call_001", "call_002"]


def test_missing_usage_is_fine(settings):
    client, _, _ = make_client(settings, completion(content="x", usage=None))
    assert client.chat(MESSAGES, TOOLS).usage == {}


def test_no_choices_is_an_error(settings):
    client, _, _ = make_client(settings, SimpleNamespace(choices=[], usage=None))
    with pytest.raises(LLMError, match="no choices"):
        client.chat(MESSAGES, TOOLS)


def test_parse_tool_arguments_accepts_a_dict():
    assert parse_tool_arguments({"a": 1}) == ({"a": 1}, False)


# --------------------------------------------------------------- request


def test_request_uses_configured_model_tools_and_messages(settings):
    client, sdk, _ = make_client(settings, completion(content="x"))
    client.chat(MESSAGES, TOOLS)
    sent = sdk.requests[0]
    assert sent["model"] == "test-model" and sent["messages"] == MESSAGES
    assert sent["tools"] == TOOLS and sent["tool_choice"] == "auto" and sent["temperature"] == 0.0


def test_no_tools_means_no_tool_fields(settings):
    client, sdk, _ = make_client(settings, completion(content="x"))
    client.chat(MESSAGES, [])
    assert "tools" not in sdk.requests[0] and "tool_choice" not in sdk.requests[0]


def test_building_the_real_client_needs_a_key():
    with pytest.raises(LLMUnavailableError, match="API key"):
        GroqClient(Settings(llm_api_key=""))


def test_estimate_tokens_grows_with_content():
    small = estimate_tokens(MESSAGES, TOOLS)
    big = estimate_tokens(MESSAGES + [{"role": "user", "content": "x" * 4000}], TOOLS)
    assert 0 < small < big and big - small >= 900


# ----------------------------------------------------------------- retries


def test_rate_limit_is_retried_with_exponential_backoff(settings):
    client, sdk, sleeps = make_client(settings, rate_limited(), rate_limited(), completion(content="ok"))
    assert client.chat(MESSAGES, TOOLS).text == "ok"
    assert sleeps == [2.0, 4.0] and len(sdk.requests) == 3


def test_retry_after_header_is_honoured(settings):
    client, _, sleeps = make_client(settings, rate_limited(retry_after=7), completion(content="ok"))
    client.chat(MESSAGES, TOOLS)
    assert sleeps == [7.0]


def test_backoff_is_capped(settings):
    settings.llm_max_retries = 8
    client, _, sleeps = make_client(settings, *[rate_limited() for _ in range(8)], completion(content="ok"))
    client.chat(MESSAGES, TOOLS)
    assert sleeps == [2, 4, 8, 16, 32, 60, 60, 60]


def test_persistent_rate_limit_raises_after_the_configured_retries(settings):
    client, sdk, sleeps = make_client(settings, *[rate_limited() for _ in range(4)])
    with pytest.raises(RateLimitError) as exc:
        client.chat(MESSAGES, TOOLS)
    assert isinstance(exc.value, LLMUnavailableError)
    assert len(sdk.requests) == settings.llm_max_retries + 1 and len(sleeps) == settings.llm_max_retries


def test_a_huge_retry_after_fails_fast_without_sleeping(settings):
    client, sdk, sleeps = make_client(settings, rate_limited(retry_after=3600))
    with pytest.raises(RateLimitError) as exc:
        client.chat(MESSAGES, TOOLS)
    assert sleeps == [] and len(sdk.requests) == 1 and exc.value.retry_after == 3600


@pytest.mark.parametrize("error", [status_error(500), status_error(503), openai.APITimeoutError(request=REQUEST), openai.APIConnectionError(request=REQUEST)])
def test_server_errors_timeouts_and_connection_errors_are_retried(settings, error):
    client, _, sleeps = make_client(settings, error, completion(content="ok"))
    assert client.chat(MESSAGES, TOOLS).text == "ok" and len(sleeps) == 1


def test_exhausted_server_errors_raise_unavailable_not_rate_limit(settings):
    client, _, _ = make_client(settings, *[status_error(503) for _ in range(4)])
    with pytest.raises(LLMUnavailableError) as exc:
        client.chat(MESSAGES, TOOLS)
    assert not isinstance(exc.value, RateLimitError)


@pytest.mark.parametrize("code", [401, 403])
def test_bad_credentials_fail_immediately(settings, code):
    client, sdk, sleeps = make_client(settings, status_error(code))
    with pytest.raises(LLMUnavailableError, match="credentials"):
        client.chat(MESSAGES, TOOLS)
    assert sleeps == [] and len(sdk.requests) == 1


@pytest.mark.parametrize("code", [400, 404])
def test_rejected_requests_are_errors_not_retried(settings, code):
    client, sdk, sleeps = make_client(settings, status_error(code))
    with pytest.raises(LLMError) as exc:
        client.chat(MESSAGES, TOOLS)
    assert not isinstance(exc.value, LLMUnavailableError) and sleeps == [] and len(sdk.requests) == 1


def test_unexpected_exceptions_become_llm_errors(settings):
    client, _, sleeps = make_client(settings, ValueError("boom"))
    with pytest.raises(LLMError, match="ValueError"):
        client.chat(MESSAGES, TOOLS)
    assert sleeps == []


# ------------------------------------------------------------------ secrets


@pytest.mark.parametrize("error", [rate_limited(), status_error(400), status_error(401), ValueError(f"key={KEY}")])
def test_api_key_never_appears_in_errors_or_logs(settings, caplog, error):
    caplog.set_level(logging.DEBUG)
    errors = [error] * (settings.llm_max_retries + 1)
    client, _, _ = make_client(settings, *errors)
    with pytest.raises(LLMError) as exc:
        client.chat(MESSAGES, TOOLS)
    assert KEY not in str(exc.value) and KEY not in repr(exc.value)
    assert KEY not in caplog.text
    assert "Bearer " + KEY not in str(exc.value)


def test_settings_repr_masks_the_key():
    assert KEY not in repr(Settings(llm_api_key=KEY))
