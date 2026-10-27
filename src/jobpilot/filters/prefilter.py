"""Deterministic prefilter applied BEFORE any LLM call (R3 / R12 pipeline).

A :class:`~jobpilot.models.RawJob` passes when it clears every configured rule:

* country / remote — remote jobs always pass; otherwise the location must match
  one of the configured countries (case-insensitive substring);
* required keywords — at least one must appear in the title or description;
* excluded keywords — none may appear in the title or description;
* accepted languages — if a language is supplied it must be in the accepted list;
* modality — if the config lists modalities and the job declares one (via remote),
  it must be allowed;
* minimum salary — only enforced when the offer declares a parseable salary.

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
        return True
    return language in config.accepted_languages


def _modality_ok(raw: RawJob, config: PrefilterConfig) -> bool:
    if not config.modalities:
        return True
    modality = "remote" if raw.remote else "onsite"
    allowed = {m.lower() for m in config.modalities}
    # "hybrid" offers are indistinguishable from onsite here; accept when onsite is
    # allowed or hybrid is allowed, since remote jobs already match "remote".
    if modality == "onsite":
        return "onsite" in allowed or "hybrid" in allowed
    return modality in allowed


_SALARY_NUMBER_RE = re.compile(r"(\d[\d,.]*)")


def _declared_salary_usd(raw: RawJob) -> int | None:
    """Return the lowest number in a USD-denominated salary string, else None."""
    if not raw.salary_text:
        return None
    text = raw.salary_text.lower()
    if "usd" not in text and "$" not in text:
        return None
    numbers: list[int] = []
    for match in _SALARY_NUMBER_RE.finditer(raw.salary_text):
        digits = match.group(1).replace(",", "").split(".")[0]
        if digits.isdigit():
            numbers.append(int(digits))
    return min(numbers) if numbers else None


def _salary_ok(raw: RawJob, config: PrefilterConfig) -> bool:
    if config.min_salary_usd <= 0:
        return True
    declared = _declared_salary_usd(raw)
    if declared is None:
        # Only drop on salary when the offer actually declares one.
        return True
    return declared >= config.min_salary_usd


def prefilter(raw: RawJob, config: PrefilterConfig, *, language: Language | None = None) -> bool:
    """Return True if the job passes the deterministic prefilter."""
    haystack = _haystack(raw)
    if not _matches_country(raw, config):
        log.debug("prefilter_drop", reason="country", source=raw.source, id=raw.external_id)
        return False
    if not _keywords_ok(haystack, config):
        log.debug("prefilter_drop", reason="keywords", source=raw.source, id=raw.external_id)
        return False
    if not _language_ok(language, config):
        log.debug("prefilter_drop", reason="language", source=raw.source, id=raw.external_id)
        return False
    if not _modality_ok(raw, config):
        log.debug("prefilter_drop", reason="modality", source=raw.source, id=raw.external_id)
        return False
    if not _salary_ok(raw, config):
        log.debug("prefilter_drop", reason="salary", source=raw.source, id=raw.external_id)
        return False
    return True
