"""LLM clients.

Phase 1 ships a working ``GroqClient`` so ``import-cv`` runs end to end with a real
``GROQ_API_KEY``. ``OllamaClient`` and the automatic ``FallbackLlmClient`` are
scaffolded here and completed in Phase 3 (local fallback + quota handling).

All clients implement :class:`~jobpilot.interfaces.LlmClient`: ``complete_json``
returns an instance of the requested Pydantic schema, retrying once with the parse
error appended to the prompt if the first response is not valid JSON for the schema.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from pydantic import BaseModel, ValidationError

from jobpilot.config import Settings, get_settings
from jobpilot.logging import get_logger

if TYPE_CHECKING:
    from collections.abc import Callable

log = get_logger(__name__)


class LlmError(RuntimeError):
    """Raised when an LLM call cannot produce valid structured output."""


def _extract_json(text: str) -> str:
    """Best-effort extraction of a JSON object from a model response.

    Handles fenced code blocks and leading/trailing prose even though prompts ask
    for raw JSON, because models occasionally wrap output.
    """
    stripped = text.strip()
    if stripped.startswith("```"):
        # Remove the opening fence (optionally ```json) and the closing fence.
        stripped = stripped.split("```", 2)[1]
        if stripped.lstrip().lower().startswith("json"):
            stripped = stripped.lstrip()[4:]
        stripped = stripped.rsplit("```", 1)[0]
        stripped = stripped.strip()
    start = stripped.find("{")
    end = stripped.rfind("}")
    if start != -1 and end != -1 and end > start:
        return stripped[start : end + 1]
    return stripped


def _parse_into_schema[T: BaseModel](raw: str, schema: type[T]) -> T:
    payload = json.loads(_extract_json(raw))
    return schema.model_validate(payload)


def _complete_json_with_retry[T: BaseModel](
    call: Callable[[str], str],
    prompt: str,
    schema: type[T],
    max_retries: int,
) -> T:
    """Run ``call`` and parse into ``schema``, retrying once on parse failure."""
    last_error: Exception | None = None
    attempts = max_retries + 1
    current_prompt = prompt
    for attempt in range(1, attempts + 1):
        raw = call(current_prompt)
        try:
            return _parse_into_schema(raw, schema)
        except (json.JSONDecodeError, ValidationError) as exc:
            last_error = exc
            log.warning("llm_output_invalid", attempt=attempt, error=str(exc))
            if attempt <= max_retries:
                current_prompt = (
                    f"{prompt}\n\nYour previous response was not valid JSON for the "
                    f"schema. Error:\n{exc}\nReturn ONLY corrected JSON."
                )
    raise LlmError(f"LLM did not return valid JSON for {schema.__name__}: {last_error}")


class GroqClient:
    """LLM client backed by Groq's chat completions API (JSON mode)."""

    def __init__(self, settings: Settings | None = None) -> None:
        self._settings = settings or get_settings()
        if not self._settings.groq_api_key:
            raise LlmError("GROQ_API_KEY is not set.")
        from groq import Groq

        self._client = Groq(api_key=self._settings.groq_api_key)
        self._model = self._settings.groq_model

    def _call(self, prompt: str) -> str:
        response = self._client.chat.completions.create(
            model=self._model,
            messages=[
                {
                    "role": "system",
                    "content": "You output only valid JSON matching the requested schema.",
                },
                {"role": "user", "content": prompt},
            ],
            temperature=0.2,
            response_format={"type": "json_object"},
        )
        return response.choices[0].message.content or ""

    def complete_json[T: BaseModel](
        self, prompt: str, schema: type[T], *, max_retries: int = 1
    ) -> T:
        return _complete_json_with_retry(self._call, prompt, schema, max_retries)


class OllamaClient:
    """Local fallback client (Ollama). Fully wired in Phase 3."""

    def __init__(self, settings: Settings | None = None) -> None:
        self._settings = settings or get_settings()

    def _call(self, prompt: str) -> str:
        import httpx

        url = f"{self._settings.ollama_base_url.rstrip('/')}/api/generate"
        resp = httpx.post(
            url,
            json={
                "model": self._settings.ollama_model,
                "prompt": prompt,
                "format": "json",
                "stream": False,
                "options": {"temperature": 0.2},
            },
            timeout=120,
        )
        resp.raise_for_status()
        return str(resp.json().get("response", ""))

    def complete_json[T: BaseModel](
        self, prompt: str, schema: type[T], *, max_retries: int = 1
    ) -> T:
        return _complete_json_with_retry(self._call, prompt, schema, max_retries)


def _classify_primary_failure(exc: Exception) -> str:
    """Return a short reason describing why the primary client failed.

    Recognises Groq quota/rate-limit (429), authentication and connection errors,
    as well as our own :class:`LlmError` (schema failure after the single retry).
    The reason is only used for logging; the fallback is attempted regardless.
    """
    try:
        from groq import APIConnectionError, APIStatusError, APITimeoutError, RateLimitError
    except Exception:  # pragma: no cover - groq always installed, defensive only
        APIConnectionError = APIStatusError = APITimeoutError = RateLimitError = ()  # type: ignore[assignment,misc]

    if isinstance(exc, RateLimitError):
        return "rate_limit_429"
    if isinstance(exc, APIStatusError):
        status = getattr(exc, "status_code", None)
        if status == 429:
            return "quota_429"
        return f"api_status_{status}"
    if isinstance(exc, APITimeoutError):
        return "timeout"
    if isinstance(exc, APIConnectionError):
        return "connection_error"
    if isinstance(exc, LlmError):
        return "schema_failure"
    return exc.__class__.__name__


class FallbackLlmClient:
    """Try the primary client; fall back to the secondary on failure.

    Falls back to the secondary (Ollama) on quota/429, connection/timeout errors and
    schema failures (``LlmError`` after the single retry). The classified reason is
    logged so fallbacks are observable.
    """

    def __init__(self, primary: object, secondary: object | None = None) -> None:
        self._primary = primary
        self._secondary = secondary

    def complete_json[T: BaseModel](
        self, prompt: str, schema: type[T], *, max_retries: int = 1
    ) -> T:
        try:
            return self._primary.complete_json(  # type: ignore[attr-defined,no-any-return]
                prompt, schema, max_retries=max_retries
            )
        except Exception as exc:  # noqa: BLE001 - fall back on any primary failure
            reason = _classify_primary_failure(exc)
            if self._secondary is None:
                log.warning("llm_primary_failed_no_fallback", reason=reason, error=str(exc))
                raise
            log.warning("llm_primary_failed_falling_back", reason=reason, error=str(exc))
            return self._secondary.complete_json(  # type: ignore[attr-defined,no-any-return]
                prompt, schema, max_retries=max_retries
            )


def build_default_client() -> FallbackLlmClient:
    """Build the production client: Groq primary, Ollama fallback."""
    settings = get_settings()
    primary = GroqClient(settings)
    secondary: OllamaClient | None = None
    try:
        secondary = OllamaClient(settings)
    except Exception as exc:  # noqa: BLE001 - optional in Phase 1
        log.info("ollama_fallback_unavailable", error=str(exc))
    return FallbackLlmClient(primary, secondary)
