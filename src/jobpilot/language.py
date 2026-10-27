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
from jobpilot.llm.schemas import ConfirmedLanguage, ConfirmLanguage
from jobpilot.logging import get_logger

if TYPE_CHECKING:
    from jobpilot.interfaces import LlmClient

log = get_logger(__name__)

# Maps the confirmation LLM's answer to a supported language, or ``None`` when the
# model says the offer is written in neither Spanish nor English ("other").
_CONFIRMED_TO_LANGUAGE: dict[ConfirmedLanguage, Language] = {
    ConfirmedLanguage.es: Language.es,
    ConfirmedLanguage.en: Language.en,
}


def _build_detector() -> object:
    """Build a lingua detector over its full language set.

    Detecting across all languages (rather than only Spanish and English) lets
    unsupported text — e.g. a French offer — surface as its real language instead
    of being forced into the closest of es/en. When the best match is not a
    supported language, :func:`detect_language` returns ``None`` so the Telegram
    language-choice flow can take over.
    """
    from lingua import LanguageDetectorBuilder

    return LanguageDetectorBuilder.from_all_languages().build()


def _lingua_to_language(value: object) -> Language | None:
    from lingua import Language as LinguaLanguage

    if value == LinguaLanguage.SPANISH:
        return Language.es
    if value == LinguaLanguage.ENGLISH:
        return Language.en
    return None


def _detect_with_confidence(text: str, detector: object) -> tuple[Language | None, float]:
    """Return lingua's single best language (mapped to es/en or ``None``) and confidence.

    The best language overall is considered. If it is not a supported language the
    result is ``(None, confidence)``, so unsupported text never clears the
    threshold as es/en.
    """
    confidences = detector.compute_language_confidence_values(text)  # type: ignore[attr-defined]
    best = next(iter(confidences), None)
    if best is None:
        return None, 0.0
    return _lingua_to_language(best.language), float(best.value)


def detect_language(
    text: str,
    config: LanguageConfig,
    llm: LlmClient | None = None,
    *,
    detector: object | None = None,
) -> Language | None:
    """Detect the language of ``text``.

    * Run ``lingua``; if the best language is a supported one (es/en) and its
      confidence is at least ``config.min_confidence``, return it.
    * If the best language is confidently an unsupported one (not es/en), return
      ``None`` — an unsupported offer must reach the Telegram language-choice flow,
      never be forced into es/en.
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

    if language is None and confidence >= config.min_confidence:
        # Confidently a language we do not support: stay ambiguous for the
        # Telegram choice flow instead of guessing es/en.
        log.info("language_unsupported", confidence=round(confidence, 3))
        return None

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

    mapped = _CONFIRMED_TO_LANGUAGE.get(confirmed.language)
    if mapped is not None and confirmed.confidence >= config.min_confidence:
        log.info(
            "language_confirmed_by_llm",
            language=mapped.value,
            confidence=round(confirmed.confidence, 3),
        )
        return mapped

    # The LLM answered "other" (unsupported), or was not confident enough: stay
    # ambiguous so the Telegram Español/Inglés/Descartar choice flow takes over.
    log.info(
        "language_ambiguous",
        llm_answer=confirmed.language.value,
        llm_confidence=round(confirmed.confidence, 3),
    )
    return None


def _confirm_language_schema_json() -> str:
    import json

    return json.dumps(ConfirmLanguage.model_json_schema())
