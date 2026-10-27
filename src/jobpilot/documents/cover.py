"""Constrained cover-letter composition from approved profile facts.

The owner's free-text ``Ajustar`` instruction must be able to change the wording of
the cover letter (for example "hazla más formal"), but a cover letter can never
invent a fact or technology the profile does not contain, and the instruction text
itself must never leak into a document. This module reconciles both:

* the LLM composes only the message *body* from the approved summary, the selected
  achievements and the profile's known technologies (anti-invention embedded);
* the returned body is validated in code — at most 200 words, no technology outside
  the profile, and no echo of the instruction text — and retried once on failure;
* greeting and sign-off are added around the body in code (never by the LLM), so
  the final letter keeps a stable, localised frame.

The result is a full cover-letter body string ready for
:func:`jobpilot.documents.render.render_cover_letter`.
"""

from __future__ import annotations

import json
import re
from typing import TYPE_CHECKING

from jobpilot.config import Language
from jobpilot.documents.render import MAX_COVER_WORDS, count_words
from jobpilot.llm.prompts import build_cover_letter_prompt
from jobpilot.llm.schemas import CoverLetterDraft
from jobpilot.logging import get_logger
from jobpilot.profile.validation import extract_mentioned_technologies, find_unknown_technologies

if TYPE_CHECKING:
    from jobpilot.interfaces import LlmClient
    from jobpilot.models import ContentSelection, JobAnalysis
    from jobpilot.profile.model import ProfileModel

log = get_logger(__name__)


class CoverLetterError(RuntimeError):
    """Raised when a profile-valid cover-letter body cannot be composed."""


def _selected_achievement_texts(
    profile: ProfileModel,
    selection: ContentSelection,
    language: Language,
) -> list[str]:
    """Return the approved texts of the selected achievements, in selection order."""
    texts: list[str] = []
    for ach_id in selection.ordered_achievement_ids:
        ach = profile.achievement_by_id(ach_id)
        if ach is None:
            continue
        approved = ach.approved_text(language)
        if approved is not None:
            texts.append(approved)
    return texts


def _instruction_leaked(body: str, instruction: str | None) -> bool:
    """Return whether a non-trivial fragment of ``instruction`` appears in ``body``.

    The LLM must honour the instruction's intent without quoting it. We flag a leak
    when the whole instruction (normalised) is reproduced, so a genuine change in
    tone is allowed while a verbatim echo of the owner's note is rejected.
    """
    if not instruction or not instruction.strip():
        return False
    needle = re.sub(r"\s+", " ", instruction.strip().lower())
    haystack = re.sub(r"\s+", " ", body.lower())
    return needle in haystack


def generate_cover_letter_body(
    profile: ProfileModel,
    analysis: JobAnalysis,
    selection: ContentSelection,
    llm: LlmClient,
    *,
    language: Language,
    company: str,
    instruction: str | None = None,
) -> str:
    """Compose the full cover-letter body (greeting + message + sign-off).

    The message paragraphs are composed by the LLM from approved facts only and are
    validated in code (word limit, profile-only technologies, no instruction echo),
    retried once on failure. The owner's ``Ajustar`` instruction guides the wording
    but never appears in the output. Raises :class:`CoverLetterError` when a valid
    body cannot be produced after the retry, so the caller marks the job ``error``
    rather than sending an invented or non-compliant letter.
    """
    summary_variant = profile.summary_variants[selection.summary_variant]
    summary = summary_variant.text(language)
    achievements = _selected_achievement_texts(profile, selection, language)
    technologies = profile.skills.all()

    # Reserve a few words for the greeting and closing the code adds around the body.
    greeting, closing = _frame(profile, language=language, company=company)
    frame_words = count_words(greeting) + count_words(closing)
    max_body_words = max(MAX_COVER_WORDS - frame_words, 1)

    prompt = build_cover_letter_prompt(
        language=language.value,
        summary=summary,
        achievements=achievements,
        technologies=technologies,
        analysis_json=analysis.model_dump_json(),
        schema_json=json.dumps(CoverLetterDraft.model_json_schema()),
        max_words=max_body_words,
        instruction=instruction,
    )

    last_reason = ""
    for attempt in range(1, 3):
        draft = llm.complete_json(prompt, CoverLetterDraft)
        message = draft.body.strip()
        reason = _reject_reason(
            message,
            profile=profile,
            instruction=instruction,
            max_words=max_body_words,
        )
        if reason is None:
            body = "\n\n".join(p for p in (greeting, message, closing) if p)
            log.info("cover_letter_generated", language=language.value, words=count_words(body))
            return body
        last_reason = reason
        log.warning("cover_letter_invalid", attempt=attempt, reason=reason, language=language.value)

    raise CoverLetterError(f"cover letter invalid after retry: {last_reason}")


def _reject_reason(
    message: str,
    *,
    profile: ProfileModel,
    instruction: str | None,
    max_words: int,
) -> str | None:
    """Return why a composed message body is rejected, or ``None`` if it is valid."""
    if not message:
        return "empty body"
    if count_words(message) > max_words:
        return f"body has {count_words(message)} words, the limit is {max_words}"
    mentioned = extract_mentioned_technologies(profile, message)
    unknown = find_unknown_technologies(profile, mentioned)
    if unknown:
        return f"technologies not in profile: {unknown}"
    if _instruction_leaked(message, instruction):
        return "instruction text leaked into the body"
    return None


def _frame(profile: ProfileModel, *, language: Language, company: str) -> tuple[str, str]:
    """Return the localised greeting and closing added around the LLM body."""
    if language is Language.es:
        greeting = f"Estimado equipo de {company}:"
        closing = f"Quedo atento a sus comentarios.\n\n{profile.basics.name}"
    else:
        greeting = f"Dear {company} team,"
        closing = f"I look forward to hearing from you.\n\n{profile.basics.name}"
    return greeting, closing
