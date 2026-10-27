"""Language detection for job offers.

Detection is deterministic first: ``lingua`` scores the text and, when it is
confident enough (>= ``language.min_confidence``), its verdict is used directly.
Only when confidence is low do we ask the LLM to confirm the language, keeping the
expensive call for the genuinely ambiguous cases.

The function returns a :class:`~jobpilot.config.Language` or ``None`` when the
language stays ambiguous after both steps; ``None`` signals the Telegram
language-button flow added in a later phase.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from jobpilot.config import Language, LanguageConfig
from jobpilot.llm.prompts import build_confirm_language_prompt
from jobpilot.llm.schemas import ConfirmLanguage
from jobpilot.logging import get_logger

if TYPE_CHECKING:
    from jobpilot.interfaces import LlmClient

log = get_logger(__name__)

# Only Spanish and English are supported offer languages.
_SUPPORTED = (Language.es, Language.en)


def _build_detector() -> object:
    """Build a lingua detector restricted to the supported languages."""
    from lingua import Language as LinguaLanguage
    from lingua import LanguageDetectorBuilder

    return LanguageDetectorBuilder.from_languages(
        LinguaLanguage.SPANISH, LinguaLanguage.ENGLISH
    ).build()


def _lingua_to_language(value: object) -> Language | None:
    from lingua import Language as LinguaLanguage

    if value == LinguaLanguage.SPANISH:
        return Language.es
    if value == LinguaLanguage.ENGLISH:
        return Language.en
    return None


def _detect_with_confidence(text: str, detector: object) -> tuple[Language | None, float]:
    """Return lingua's best supported language and its confidence [0, 1]."""
    confidences = detector.compute_language_confidence_values(text)  # type: ignore[attr-defined]
    for item in confidences:
        language = _lingua_to_language(item.language)
        if language is not None:
            return language, float(item.value)
    return None, 0.0


def detect_language(
    text: str,
    config: LanguageConfig,
    llm: LlmClient | None = None,
    *,
    detector: object | None = None,
) -> Language | None:
    """Detect the language of ``text``.

    * Run ``lingua``; if its confidence is at least ``config.min_confidence``,
      return its verdict.
    * Otherwise, if an ``llm`` is provided, ask it to confirm. Accept the LLM's
      answer only when its own confidence clears the threshold.
    * Return ``None`` when the language stays ambiguous.
    """
    if not text.strip():
        log.info("language_empty_text")
        return None

    active_detector = detector if detector is not None else _build_detector()
    language, confidence = _detect_with_confidence(text, active_detector)

    if language is not None and confidence >= config.min_confidence:
        log.info("language_detected", language=language.value, confidence=round(confidence, 3))
        return language

    log.info(
        "language_low_confidence",
        language=language.value if language else None,
        confidence=round(confidence, 3),
        threshold=config.min_confidence,
    )

    if llm is None:
        return None

    prompt = build_confirm_language_prompt(text=text, schema_json=_confirm_language_schema_json())
    try:
        confirmed = llm.complete_json(prompt, ConfirmLanguage)
    except Exception as exc:  # noqa: BLE001 - a failed confirmation stays ambiguous
        log.warning("language_confirm_failed", error=str(exc))
        return None

    if confirmed.language in _SUPPORTED and confirmed.confidence >= config.min_confidence:
        log.info(
            "language_confirmed_by_llm",
            language=confirmed.language.value,
            confidence=round(confirmed.confidence, 3),
        )
        return confirmed.language

    log.info("language_ambiguous", llm_confidence=round(confirmed.confidence, 3))
    return None


def _confirm_language_schema_json() -> str:
    import json

    return json.dumps(ConfirmLanguage.model_json_schema())
