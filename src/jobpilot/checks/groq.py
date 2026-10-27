"""Verify the Groq credential by listing available models."""

from __future__ import annotations

from jobpilot.checks.base import CheckResult, missing
from jobpilot.config import get_settings


def check_groq() -> CheckResult:
    settings = get_settings()
    if not settings.groq_api_key:
        return missing("Groq", "GROQ_API_KEY")

    try:
        from groq import Groq

        client = Groq(api_key=settings.groq_api_key)
        models = client.models.list()
        ids = [m.id for m in models.data]
        if settings.groq_model not in ids:
            return CheckResult(
                "Groq",
                True,
                f"connected, but GROQ_MODEL '{settings.groq_model}' not in "
                f"{len(ids)} available models (check the model id)",
            )
        return CheckResult("Groq", True, f"connected; model '{settings.groq_model}' available")
    except Exception as exc:  # noqa: BLE001 - surface any connection error to the user
        return CheckResult("Groq", False, f"connection failed: {exc}")
