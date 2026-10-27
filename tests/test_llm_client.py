"""Tests for the LLM client's JSON extraction and retry logic (no network)."""

from __future__ import annotations

import pytest
from pydantic import BaseModel

from jobpilot.llm.client import (
    FallbackLlmClient,
    LlmError,
    _complete_json_with_retry,
    _extract_json,
)


class _Schema(BaseModel):
    value: int


def test_extract_json_plain() -> None:
    assert _extract_json('{"value": 1}') == '{"value": 1}'


def test_extract_json_fenced() -> None:
    fenced = '```json\n{"value": 2}\n```'
    assert _extract_json(fenced).strip() == '{"value": 2}'


def test_extract_json_with_surrounding_prose() -> None:
    text = 'Here you go: {"value": 3} hope it helps'
    assert _extract_json(text) == '{"value": 3}'


def test_retry_succeeds_on_second_attempt() -> None:
    responses = iter(["not json", '{"value": 7}'])

    def call(_prompt: str) -> str:
        return next(responses)

    result = _complete_json_with_retry(call, "prompt", _Schema, max_retries=1)
    assert result.value == 7


def test_retry_exhausted_raises() -> None:
    def call(_prompt: str) -> str:
        return "still not json"

    with pytest.raises(LlmError):
        _complete_json_with_retry(call, "prompt", _Schema, max_retries=1)


def test_retry_prompt_includes_error() -> None:
    seen: list[str] = []

    def call(prompt: str) -> str:
        seen.append(prompt)
        return "bad" if len(seen) == 1 else '{"value": 1}'

    _complete_json_with_retry(call, "base prompt", _Schema, max_retries=1)
    assert "base prompt" in seen[1]
    assert "not valid JSON" in seen[1]


class _Primary:
    def complete_json(self, prompt, schema, *, max_retries=1):  # type: ignore[no-untyped-def]
        raise RuntimeError("primary down")


class _Secondary:
    def __init__(self) -> None:
        self.called = False

    def complete_json(self, prompt, schema, *, max_retries=1):  # type: ignore[no-untyped-def]
        self.called = True
        return schema(value=42)


def test_fallback_uses_secondary_on_primary_failure() -> None:
    secondary = _Secondary()
    client = FallbackLlmClient(_Primary(), secondary)
    result = client.complete_json("p", _Schema)
    assert result.value == 42
    assert secondary.called is True


def test_fallback_without_secondary_reraises() -> None:
    client = FallbackLlmClient(_Primary(), None)
    with pytest.raises(RuntimeError, match="primary down"):
        client.complete_json("p", _Schema)


class _RateLimitPrimary:
    """Primary that raises a Groq rate-limit (429) error."""

    def complete_json(self, prompt, schema, *, max_retries=1):  # type: ignore[no-untyped-def]
        import httpx
        from groq import RateLimitError

        request = httpx.Request("POST", "https://api.groq.com/v1/chat")
        response = httpx.Response(429, request=request)
        raise RateLimitError("quota exceeded", response=response, body=None)


def test_fallback_on_groq_429_uses_secondary() -> None:
    secondary = _Secondary()
    client = FallbackLlmClient(_RateLimitPrimary(), secondary)
    result = client.complete_json("p", _Schema)
    assert result.value == 42
    assert secondary.called is True


def test_classify_rate_limit_reason() -> None:
    import httpx
    from groq import RateLimitError

    from jobpilot.llm.client import _classify_primary_failure

    request = httpx.Request("POST", "https://api.groq.com/v1/chat")
    response = httpx.Response(429, request=request)
    exc = RateLimitError("quota", response=response, body=None)
    assert _classify_primary_failure(exc) == "rate_limit_429"


def test_classify_schema_failure_reason() -> None:
    from jobpilot.llm.client import _classify_primary_failure

    assert _classify_primary_failure(LlmError("bad json")) == "schema_failure"
