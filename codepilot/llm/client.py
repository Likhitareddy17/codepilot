"""Provider-independent LLM interface and the Groq client (TASK-030, TASK-031).

This is the only module that knows about the ``openai`` SDK. The controller
depends on ``LLMClient`` and ``LLMResponse`` alone.
"""

from __future__ import annotations

import json
import logging
import re
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

import openai

from codepilot.config import Settings
from codepilot.errors import LLMError, LLMUnavailableError, RateLimitError
from codepilot.models import ToolCall

logger = logging.getLogger("codepilot.llm")

BACKOFF_BASE_S = 2.0
MAX_BACKOFF_S = 60.0
MAX_RETRY_AFTER_S = 120.0   # a longer wait (for example a daily quota) is not worth waiting for
TEMPERATURE = 0.0
MAX_PROVIDER_MESSAGE_CHARS = 200


@dataclass
class LLMResponse:
    """One normalised model response."""

    text: Optional[str] = None
    tool_calls: List[ToolCall] = field(default_factory=list)
    usage: Dict[str, int] = field(default_factory=dict)
    finish_reason: str = ""


class LLMClient(ABC):
    """The only interface the controller uses to talk to a model."""

    @abstractmethod
    def chat(self, messages: List[Dict[str, Any]], tools: List[Dict[str, Any]]) -> LLMResponse:
        """Send messages and tool schemas; return text and/or tool calls.

        Raises ``LLMUnavailableError`` when the model cannot be reached after
        retries, and ``LLMError`` for any other provider failure.
        """


def estimate_tokens(messages: List[Dict[str, Any]], tools: Optional[List[Dict[str, Any]]] = None) -> int:
    """Rough token estimate (about four characters per token) for budget checks."""
    return len(json.dumps([messages, tools or []], ensure_ascii=False, default=str)) // 4 + 1


def parse_tool_arguments(raw: Any) -> Tuple[Dict[str, Any], bool]:
    """Parse tool-call arguments. Returns (arguments, malformed)."""
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return {}, False                      # a tool with no parameters
    if isinstance(raw, dict):
        return raw, False
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        return {}, True
    if not isinstance(parsed, dict):
        return {}, True
    return parsed, False


class GroqClient(LLMClient):
    """Groq's OpenAI-compatible API through the ``openai`` SDK."""

    def __init__(
        self,
        settings: Settings,
        sdk_client: Any = None,
        sleep: Callable[[float], None] = time.sleep,
        timeout_s: float = 60.0,
    ) -> None:
        self.settings = settings
        self._sleep = sleep
        if sdk_client is None:
            if not settings.llm_api_key:
                raise LLMUnavailableError("No LLM API key is configured. Set GROQ_API_KEY in your .env file.")
            sdk_client = openai.OpenAI(
                base_url=settings.llm_base_url, api_key=settings.llm_api_key,
                timeout=timeout_s, max_retries=0,   # retries are handled here, with backoff
            )
        self._client = sdk_client

    # ------------------------------------------------------------------ chat

    def chat(self, messages: List[Dict[str, Any]], tools: List[Dict[str, Any]]) -> LLMResponse:
        kwargs: Dict[str, Any] = {
            "model": self.settings.llm_model,
            "messages": messages,
            "temperature": TEMPERATURE,
        }
        if tools:
            kwargs["tools"] = tools
            kwargs["tool_choice"] = "auto"

        attempt = 0
        while True:
            try:
                raw = self._client.chat.completions.create(**kwargs)
            except Exception as exc:  # noqa: BLE001 - classified below
                retryable, rate_limited, retry_after, reason = self._classify(exc)
                if not retryable:
                    raise self._fatal(exc, reason) from None
                too_long = retry_after is not None and retry_after > MAX_RETRY_AFTER_S
                if attempt >= self.settings.llm_max_retries or too_long:
                    message = f"The LLM service is unavailable after {attempt + 1} attempt(s): {reason}."
                    if rate_limited:
                        raise RateLimitError(message, retry_after=int(retry_after) if retry_after else None) from None
                    raise LLMUnavailableError(message, retry_after=int(retry_after) if retry_after else None) from None
                delay = retry_after if retry_after is not None else min(BACKOFF_BASE_S * 2 ** attempt, MAX_BACKOFF_S)
                logger.warning("LLM call failed (%s); retrying in %.1fs (attempt %d)", reason, delay, attempt + 1)
                self._sleep(delay)
                attempt += 1
                continue
            return self._normalize(raw)

    # ------------------------------------------------------------- internals

    def _redact(self, text: str) -> str:
        key = self.settings.llm_api_key
        if key:
            text = text.replace(key, "***")
        text = re.sub(r"(?i)bearer\s+\S+", "Bearer ***", text)
        return text[:MAX_PROVIDER_MESSAGE_CHARS]

    def _classify(self, exc: Exception) -> Tuple[bool, bool, Optional[float], str]:
        """(retryable, is_rate_limit, retry_after_seconds, reason)."""
        if isinstance(exc, openai.RateLimitError):
            return True, True, self._retry_after(exc), "rate limited (HTTP 429)"
        if isinstance(exc, openai.APITimeoutError):
            return True, False, None, "the request timed out"
        if isinstance(exc, openai.APIConnectionError):
            return True, False, None, "could not connect to the service"
        if isinstance(exc, openai.APIStatusError):
            status = exc.status_code
            if status >= 500 or status == 408:
                return True, False, self._retry_after(exc), f"server error (HTTP {status})"
            return False, False, None, f"HTTP {status}"
        return False, False, None, type(exc).__name__

    @staticmethod
    def _retry_after(exc: Exception) -> Optional[float]:
        response = getattr(exc, "response", None)
        value = None
        if response is not None:
            try:
                value = response.headers.get("retry-after")
            except Exception:  # noqa: BLE001
                value = None
        try:
            seconds = float(value)
        except (TypeError, ValueError):
            return None
        return seconds if seconds >= 0 else None

    def _fatal(self, exc: Exception, reason: str) -> LLMError:
        status = getattr(exc, "status_code", None)
        if status in (401, 403):
            return LLMUnavailableError(f"The LLM service rejected the credentials (HTTP {status}). Check GROQ_API_KEY.")
        detail = self._redact(str(getattr(exc, "message", "") or exc))
        return LLMError(f"The LLM request failed ({reason}): {detail}")

    def _normalize(self, raw: Any) -> LLMResponse:
        choices = getattr(raw, "choices", None)
        if not choices:
            raise LLMError("The LLM service returned no choices.")
        choice = choices[0]
        message = choice.message
        calls: List[ToolCall] = []
        for index, item in enumerate(getattr(message, "tool_calls", None) or [], 1):
            raw_args = getattr(item.function, "arguments", None)
            arguments, malformed = parse_tool_arguments(raw_args)
            calls.append(ToolCall(
                id=getattr(item, "id", None) or f"call_{index:03d}",
                name=item.function.name,
                arguments=arguments,
                malformed=malformed,
                raw_arguments=raw_args if isinstance(raw_args, str) else "",
            ))
        usage_obj = getattr(raw, "usage", None)
        usage: Dict[str, int] = {}
        if usage_obj is not None:
            for name in ("prompt_tokens", "completion_tokens", "total_tokens"):
                value = getattr(usage_obj, name, None)
                if isinstance(value, int):
                    usage[name] = value
        return LLMResponse(
            text=getattr(message, "content", None) or None,
            tool_calls=calls,
            usage=usage,
            finish_reason=getattr(choice, "finish_reason", None) or "",
        )
