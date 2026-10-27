"""Deterministic prefilter applied BEFORE any LLM call (R3 / R12 pipeline).

A :class:`~jobpilot.models.RawJob` passes when it clears every configured rule:

* country / remote — remote jobs always pass; otherwise the location must match
  one of the configured countries (case-insensitive substring);
* required keywords — at least one must appear in the title or description;
* excluded keywords — none may appear in the title or description;
* accepted languages — if a language is known it must be in the accepted list;
* modality — if the config lists modalities and the job declares one, its actual
  modality (remote / hybrid / onsite) must be allowed;
* minimum salary — only enforced when the offer declares a parseable salary, which
  is normalized to monthly USD before comparison.

The function is pure and performs no I/O.
"""

from __future__ import annotations

import re

from jobpilot.config import Language, PrefilterConfig
from jobpilot.logging import get_logger
from jobpilot.models import RawJob

log = get_logger(__name__)


def _haystack(raw: RawJob) -> str:
    return f"{raw.title}\n{raw.description}\n{raw.location or ''}".lower()


def _matches_country(raw: RawJob, config: PrefilterConfig) -> bool:
    if config.remote_ok and raw.remote:
        return True
    if not config.countries:
        return True
    location = (raw.location or "").lower()
    if not location:
        # No location declared and not remote: cannot confirm country, so reject.
        return False
    return any(country.lower() in location for country in config.countries)


def _keywords_ok(haystack: str, config: PrefilterConfig) -> bool:
    for excluded in config.excluded_keywords:
        if excluded.lower() in haystack:
            return False
    if config.required_keywords:
        return any(req.lower() in haystack for req in config.required_keywords)
    return True


def _language_ok(language: Language | None, config: PrefilterConfig) -> bool:
    if language is None:
        # Language is not yet known; it is confirmed in a later pipeline step.
        return True
    return language in config.accepted_languages


def _modality_ok(raw: RawJob, config: PrefilterConfig) -> bool:
    if not config.modalities:
        return True
    if raw.modality is None:
        # Source did not declare a modality; cannot filter on it here.
        return True
    allowed = {m.lower() for m in config.modalities}
    return raw.modality.value in allowed


# A number optionally followed by a magnitude suffix (k = thousands).
_SALARY_NUMBER_RE = re.compile(r"(\d[\d,.]*)\s*(k)?", re.IGNORECASE)
# Pay cadence keywords, mapped to the factor that converts the amount to monthly.
_CADENCE_FACTORS: list[tuple[tuple[str, ...], float]] = [
    (("per year", "/year", "/yr", "per annum", "annual", "yearly", "año", "anual"), 1.0 / 12.0),
    (("per hour", "/hour", "/hr", "hourly", "hora"), 160.0),
    (("per week", "/week", "weekly", "semana"), 52.0 / 12.0),
    (("per month", "/month", "monthly", "mensual", "mes"), 1.0),
]


def _cadence_factor(text: str) -> float | None:
    """Return the factor that turns the declared amount into a monthly amount.

    ``None`` means no cadence keyword was found, so the caller must infer it.
    """
    for keywords, factor in _CADENCE_FACTORS:
        if any(keyword in text for keyword in keywords):
            return factor
    return None


def _declared_salary_usd(raw: RawJob) -> float | None:
    """Return the lowest declared salary, normalized to monthly USD, else None."""
    if not raw.salary_text:
        return None
    text = raw.salary_text.lower()
    if "usd" not in text and "$" not in text:
        return None

    amounts: list[float] = []
    for match in _SALARY_NUMBER_RE.finditer(raw.salary_text):
        digits = match.group(1).replace(",", "")
        suffix = match.group(2)
        try:
            value = float(digits)
        except ValueError:
            continue
        if suffix:  # "80k" -> 80000
            value *= 1000.0
        amounts.append(value)
    if not amounts:
        return None

    lowest = min(amounts)
    factor = _cadence_factor(text)
    if factor is not None:
        return lowest * factor
    # No explicit cadence: treat yearly-sized figures as annual, otherwise monthly.
    # A value at/above ~12 monthly wages is read as annual (e.g. "$80k - $110k USD"
    # -> 80000/12 monthly), while a smaller figure is treated as already monthly.
    if lowest >= 12000:
        return lowest / 12.0
    return lowest


def _salary_ok(raw: RawJob, config: PrefilterConfig) -> bool:
    if config.min_salary_usd <= 0:
        return True
    declared = _declared_salary_usd(raw)
    if declared is None:
        # Only drop on salary when the offer actually declares one.
        return True
    return declared >= config.min_salary_usd


def prefilter(raw: RawJob, config: PrefilterConfig, *, language: Language | None = None) -> bool:
    """Return True if the job passes the deterministic prefilter.

    ``language`` lets a caller pass a language resolved elsewhere; when omitted the
    job's own declared ``raw.language`` is used.
    """
    effective_language = language if language is not None else raw.language
    haystack = _haystack(raw)
    if not _matches_country(raw, config):
        log.debug("prefilter_drop", reason="country", source=raw.source, id=raw.external_id)
        return False
    if not _keywords_ok(haystack, config):
        log.debug("prefilter_drop", reason="keywords", source=raw.source, id=raw.external_id)
        return False
    if not _language_ok(effective_language, config):
        log.debug("prefilter_drop", reason="language", source=raw.source, id=raw.external_id)
        return False
    if not _modality_ok(raw, config):
        log.debug("prefilter_drop", reason="modality", source=raw.source, id=raw.external_id)
        return False
    if not _salary_ok(raw, config):
        log.debug("prefilter_drop", reason="salary", source=raw.source, id=raw.external_id)
        return False
    return True
