"""Tests for language detection (lingua + optional LLM confirmation)."""

from __future__ import annotations

from dataclasses import dataclass

from jobpilot.config import Language, LanguageConfig
from jobpilot.language import detect_language
from jobpilot.llm.schemas import ConfirmLanguage
from tests.fakes import ScriptedFakeLlmClient

_CONFIG = LanguageConfig(min_confidence=0.80)

_ES_TEXT = (
    "Buscamos un ingeniero backend con experiencia en Java y Spring Boot para "
    "construir y mantener nuestros servicios de pagos en la nube."
)
_EN_TEXT = (
    "We are hiring a senior backend engineer to design and maintain our "
    "event-driven payment platform using Java, Spring Boot and AWS."
)


# A fake lingua detector returning a fixed (language, confidence) list. ---------
@dataclass
class _Lang:
    name: str


@dataclass
class _Conf:
    language: object
    value: float


class _FakeDetector:
    """Mimics lingua's ``compute_language_confidence_values`` return shape."""

    def __init__(self, values: list[_Conf]) -> None:
        self._values = values

    def compute_language_confidence_values(self, _text: str) -> list[_Conf]:
        return self._values


def _lingua_spanish() -> object:
    from lingua import Language as LinguaLanguage

    return LinguaLanguage.SPANISH


def _lingua_english() -> object:
    from lingua import Language as LinguaLanguage

    return LinguaLanguage.ENGLISH


def test_clear_spanish_detected_without_llm() -> None:
    llm = ScriptedFakeLlmClient()
    result = detect_language(_ES_TEXT, _CONFIG, llm)
    assert result is Language.es
    assert llm.prompts == []  # LLM never consulted when lingua is confident


def test_clear_english_detected_without_llm() -> None:
    llm = ScriptedFakeLlmClient()
    result = detect_language(_EN_TEXT, _CONFIG, llm)
    assert result is Language.en
    assert llm.prompts == []


def test_low_confidence_calls_llm_and_accepts() -> None:
    detector = _FakeDetector([_Conf(_lingua_spanish(), 0.55), _Conf(_lingua_english(), 0.45)])
    llm = ScriptedFakeLlmClient([ConfirmLanguage(language=Language.es, confidence=0.95)])
    result = detect_language("ambiguous text", _CONFIG, llm, detector=detector)
    assert result is Language.es
    assert len(llm.prompts) == 1


def test_low_confidence_llm_still_ambiguous_returns_none() -> None:
    detector = _FakeDetector([_Conf(_lingua_english(), 0.52), _Conf(_lingua_spanish(), 0.48)])
    llm = ScriptedFakeLlmClient([ConfirmLanguage(language=Language.en, confidence=0.40)])
    result = detect_language("ambiguous text", _CONFIG, llm, detector=detector)
    assert result is None
    assert len(llm.prompts) == 1


def test_low_confidence_without_llm_returns_none() -> None:
    detector = _FakeDetector([_Conf(_lingua_english(), 0.52), _Conf(_lingua_spanish(), 0.48)])
    result = detect_language("ambiguous text", _CONFIG, None, detector=detector)
    assert result is None


def test_empty_text_returns_none() -> None:
    assert detect_language("   ", _CONFIG, None) is None
