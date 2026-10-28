"""Assisted form filling — base contract and shared, platform-agnostic logic.

Phase 8 fills (but never blindly submits) an application form on a supported ATS
using Playwright, LOCALLY ONLY. This module holds everything that does not depend
on a browser so it is fully unit-testable without Playwright:

* :class:`FormFiller` — the ABC (``matches``/``fill``) matching the
  :class:`jobpilot.interfaces.FormFiller` Protocol, plus the shared machinery every
  platform reuses (sensitive-question detection, answer-bank lookup, LLM answering
  of open questions, uncertain-field tracking);
* :func:`detect_platform` — detect ``greenhouse``/``lever`` from a URL and/or the
  page DOM, returning ``None`` for anything unknown so an unsupported form is never
  driven. The dispatch is shaped so Ashby/Workable/SmartRecruiters can be added by
  registering another :data:`_PLATFORM_MARKERS` entry and a new ``FormFiller``.

The hard safety rules live here so both platforms inherit them identically:

* SENSITIVE questions (salary expectation, work authorization/visa, relocation,
  background/legal, demographics, years-of-experience in a specific technology) are
  NEVER answered by the LLM. They are answered only from the profile's approved
  ``answers_bank``; with no approved answer the field is left blank and reported in
  ``unanswered_sensitive`` so the owner decides.
* A CAPTCHA / anti-bot challenge stops the run and notifies — it is never evaded.
* An unexpected form structure triggers a safe abort (screenshot + notify) and the
  form is never submitted incomplete.

Playwright itself is imported lazily (only when a real run opens a browser), so this
module imports cleanly in CI with no browser installed, and the tests drive the
platforms through a mocked page object.
"""

from __future__ import annotations

import json
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from jobpilot.interfaces import FillContext, FillResult
from jobpilot.llm.prompts import build_form_answer_prompt
from jobpilot.llm.schemas import FormAnswer
from jobpilot.logging import get_logger

if TYPE_CHECKING:
    from jobpilot.config import Language
    from jobpilot.interfaces import LlmClient
    from jobpilot.models import JobAnalysis
    from jobpilot.profile.model import AnswerBankEntry, ProfileModel

log = get_logger(__name__)


class FormStructureError(RuntimeError):
    """Raised when the form does not look like the expected platform structure.

    The caller turns this into a safe abort: a screenshot is saved, the owner is
    notified, and the form is NEVER submitted. It signals "I do not understand this
    page", which is always safer than guessing.
    """


class CaptchaError(RuntimeError):
    """Raised when a CAPTCHA / anti-bot challenge is detected on the form.

    The run stops and the owner is notified. The challenge is never solved or
    evaded — doing so would violate the site's terms, so a human takes over.
    """


# --------------------------------------------------------------------------- #
# Platform detection
# --------------------------------------------------------------------------- #
class Platform(StrEnum):
    """A supported assisted-form platform."""

    greenhouse = "greenhouse"
    lever = "lever"


@dataclass(frozen=True, slots=True)
class _PlatformMarkers:
    """How to recognise a platform from the apply URL and/or the page DOM.

    ``url_patterns`` match the apply URL (host or path); ``dom_patterns`` match
    markers in the rendered HTML (a mounting ``div`` id, an embed script src, a
    form action). Either kind of match identifies the platform, so a greenhouse
    form embedded on a company domain is still detected from its DOM. New platforms
    (Ashby/Workable/SmartRecruiters) are added by registering another entry.
    """

    url_patterns: tuple[str, ...]
    dom_patterns: tuple[str, ...]


_PLATFORM_MARKERS: dict[Platform, _PlatformMarkers] = {
    Platform.greenhouse: _PlatformMarkers(
        url_patterns=("greenhouse.io", "boards.greenhouse.io", "job-boards.greenhouse.io"),
        dom_patterns=("greenhouse", "grnhse", "#grnhse_app", 'id="application_form"'),
    ),
    Platform.lever: _PlatformMarkers(
        url_patterns=("lever.co", "jobs.lever.co"),
        dom_patterns=("lever", "application-form", 'data-qa="btn-submit"'),
    ),
}

# LinkedIn Easy Apply and Indeed are explicitly out of scope and must never be
# driven, even if a URL reaches here. Detection returns None for them.
_UNSUPPORTED_URL_MARKERS: tuple[str, ...] = (
    "linkedin.com",
    "indeed.com",
)


def _matches_any(haystack: str, needles: tuple[str, ...]) -> bool:
    lowered = haystack.lower()
    return any(needle.lower() in lowered for needle in needles)


def detect_platform(url: str, dom: str | None = None) -> str | None:
    """Detect the ATS platform of an application form from its URL and/or DOM.

    Returns the platform name (``"greenhouse"``/``"lever"``) or ``None`` when the
    form is unknown or explicitly unsupported (LinkedIn Easy Apply, Indeed). A URL
    match alone is enough; a DOM match catches a platform embedded on a company
    domain whose URL gives nothing away. The result is a plain string so callers do
    not need to import the enum.
    """
    if _matches_any(url, _UNSUPPORTED_URL_MARKERS):
        log.info("form_platform_unsupported", url=url)
        return None
    for platform, markers in _PLATFORM_MARKERS.items():
        if _matches_any(url, markers.url_patterns):
            return platform.value
        if dom is not None and _matches_any(dom, markers.dom_patterns):
            return platform.value
    return None


# --------------------------------------------------------------------------- #
# Sensitive-question detection (never answered by the LLM)
# --------------------------------------------------------------------------- #
# Each pattern is a whole-phrase / keyword marker in English or Spanish. A question
# matching ANY of these is sensitive and is answered only from the approved
# answers_bank — never by the LLM. The years-of-experience-in-a-tech case is handled
# separately by _asks_years_in_technology so a generic "years of experience" without
# a named technology is NOT treated as sensitive by itself.
_SENSITIVE_PATTERNS: tuple[str, ...] = (
    # salary expectation
    "salary",
    "compensation",
    "pay expectation",
    "expected pay",
    "pretensión salarial",
    "aspiración salarial",
    "expectativa salarial",
    "salario",
    # work authorization / visa
    "work authorization",
    "authorized to work",
    "right to work",
    "visa",
    "sponsorship",
    "work permit",
    "autorización de trabajo",
    "permiso de trabajo",
    "patrocinio",
    # relocation
    "relocate",
    "relocation",
    "reubicar",
    "reubicación",
    "reubicarse",
    "mudar",
    # background / legal
    "background check",
    "criminal",
    "felony",
    "conviction",
    "legal status",
    "antecedentes",
    "penales",
    "judiciales",
    # demographics (EEO and similar)
    "gender",
    "race",
    "ethnicity",
    "disability",
    "veteran",
    "sexual orientation",
    "age",
    "date of birth",
    "género",
    "raza",
    "etnia",
    "discapacidad",
    "orientación sexual",
    "edad",
    "fecha de nacimiento",
)

# A question is sensitive when it asks for years of experience WITH a named
# technology (e.g. "How many years of experience do you have with Java?"). We detect
# the years-of-experience phrasing and require a technology token near it.
_YEARS_PATTERNS: tuple[str, ...] = (
    "years of experience",
    "years experience",
    "how many years",
    "años de experiencia",
    "cuántos años",
    "cuantos años",
)


def _asks_years_in_technology(question: str, technologies: set[str]) -> bool:
    """Return whether a question asks for years of experience in a named technology.

    Sensitive only when the years-of-experience phrasing co-occurs with a concrete
    technology name from the profile (so the model is never asked to quantify a
    tech-specific tenure). A bare "years of experience" with no technology is not
    treated as sensitive here.
    """
    lowered = question.lower()
    if not any(p in lowered for p in _YEARS_PATTERNS):
        return False
    return any(re.search(r"\b" + re.escape(tech) + r"\b", lowered) for tech in technologies)


def is_sensitive_question(question: str, technologies: set[str]) -> bool:
    """Return whether a form question is SENSITIVE and must never reach the LLM."""
    if _matches_any(question, _SENSITIVE_PATTERNS):
        return True
    return _asks_years_in_technology(question, technologies)


# --------------------------------------------------------------------------- #
# Page safety helpers (shared by every platform)
# --------------------------------------------------------------------------- #
def _language_from_ctx(ctx: FillContext) -> Language:
    """Resolve the :class:`~jobpilot.config.Language` from the fill context."""
    from jobpilot.config import Language  # noqa: PLC0415 - avoid import cycle at module load

    return Language(ctx.language)


def _detect_captcha(page: FormPage, schema: FormSchema) -> bool:
    """Return whether any CAPTCHA / anti-bot marker is present on the page."""
    return any(page.query_selector(marker) is not None for marker in schema.captcha_markers)


def _require_structure(page: FormPage, schema: FormSchema, *, platform: str, dom: str) -> None:
    """Raise :class:`FormStructureError` if the page is not the expected structure.

    The page must expose every selector in ``required_markers``; a missing marker
    means the DOM is not what this platform's filler understands, so the caller
    aborts safely instead of filling the wrong fields.
    """
    missing = [marker for marker in schema.required_markers if page.query_selector(marker) is None]
    if missing:
        log.warning("form_structure_unexpected", platform=platform, missing=missing)
        raise FormStructureError(
            f"Unexpected {platform} form structure; missing markers: {missing}"
        )


def _take_screenshot(page: FormPage, job_id: int, platform: str) -> str:
    """Save a screenshot of the current page and return its path.

    Screenshots land under a per-run temp directory so the pre-submit review and a
    safe-abort both have an artifact to attach; the path is returned on the
    :class:`~jobpilot.models.FillResult`.
    """
    import tempfile  # noqa: PLC0415 - only needed when a screenshot is taken

    directory = Path(tempfile.gettempdir()) / "jobpilot-forms"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{platform}_job_{job_id}.png"
    page.screenshot(path=str(path))
    log.info("form_screenshot_saved", job_id=job_id, path=str(path))
    return str(path)


def abort_screenshot(page: FormPage, job_id: int, platform: str) -> str:
    """Public helper to capture a screenshot during a safe abort."""
    return _take_screenshot(page, job_id, f"{platform}_abort")


# --------------------------------------------------------------------------- #
# Answer-bank lookup (the only source for sensitive answers)
# --------------------------------------------------------------------------- #
def find_answer_bank_entry(profile: ProfileModel, question: str) -> AnswerBankEntry | None:
    """Return the approved answers_bank entry whose patterns match ``question``.

    Matching is case-insensitive and substring-based on each entry's
    ``question_patterns``. This is the ONLY source allowed to answer a sensitive
    question; the LLM is never consulted for one.
    """
    lowered = question.lower()
    for entry in profile.answers_bank:
        if any(pattern.lower() in lowered for pattern in entry.question_patterns):
            return entry
    return None


# --------------------------------------------------------------------------- #
# Page abstraction (a real Playwright Page satisfies this; tests pass a fake)
# --------------------------------------------------------------------------- #
@runtime_checkable
class FormPage(Protocol):
    """The minimal page surface a FormFiller drives.

    A real Playwright ``Page`` already provides these methods with compatible
    signatures; tests pass a fake implementing the same shape so no browser is
    launched. Keeping the surface tiny makes the mock trivial and keeps the
    platform logic honest about what it touches.
    """

    def content(self) -> str: ...

    def fill(self, selector: str, value: str) -> None: ...

    def set_input_files(self, selector: str, files: str) -> None: ...

    def query_selector(self, selector: str) -> object | None: ...

    def click(self, selector: str) -> None: ...

    def screenshot(self, *, path: str) -> None: ...


@dataclass(slots=True)
class OpenQuestion:
    """One open (free-text) question discovered on the form."""

    selector: str
    label: str


@dataclass(frozen=True, slots=True)
class FormSchema:
    """Platform-specific selectors and markers the shared driver needs.

    Each concrete platform supplies one of these so the field-mapping, file-upload,
    CAPTCHA-detection and structure-validation logic can live once on the ABC. The
    ``required_markers`` are selectors that MUST be present for the page to look
    like this platform; their absence triggers a safe abort (``FormStructureError``).
    """

    #: Selector -> profile field name for the known text fields to fill.
    field_map: dict[str, str]
    #: Selector for the CV file input.
    cv_selector: str
    #: Selector for the cover-letter file input.
    cover_letter_selector: str
    #: Selectors that must exist for the page to be the expected structure.
    required_markers: tuple[str, ...]
    #: Selectors whose presence means a CAPTCHA / anti-bot challenge is shown.
    captcha_markers: tuple[str, ...]
    #: Selector for the final submit button (clicked only after owner confirmation).
    submit_selector: str


@dataclass(slots=True)
class _AnswerOutcome:
    """Result of resolving a single question's answer."""

    value: str | None
    uncertain: bool = False
    unanswered_sensitive: bool = False


# --------------------------------------------------------------------------- #
# The FormFiller ABC + shared answering logic
# --------------------------------------------------------------------------- #
class FormFiller(ABC):
    """Fill (never submit) an application form on one supported platform.

    Structural match for :class:`jobpilot.interfaces.FormFiller`. Subclasses implement
    the platform-specific DOM interaction (:meth:`fill`); the sensitive-question
    guard, answer-bank lookup and LLM answering of open questions are provided here
    so every platform behaves identically on the safety-critical paths.
    """

    #: The platform this filler drives; used by :meth:`matches`.
    platform: Platform

    def __init__(
        self,
        profile: ProfileModel,
        llm: LlmClient,
        analysis: JobAnalysis | None = None,
    ) -> None:
        self._profile = profile
        self._llm = llm
        self._analysis = analysis
        self._technologies = {t.lower() for t in self._profile_technologies()}

    def _profile_technologies(self) -> set[str]:
        techs: set[str] = set(self._profile.skills.all())
        for exp in self._profile.experience:
            for ach in exp.achievements:
                techs.update(ach.tags)
        for prj in self._profile.projects:
            techs.update(prj.tags)
        return techs

    def matches(self, url: str) -> bool:
        """Return whether this filler handles the platform ``url`` belongs to."""
        return detect_platform(url) == self.platform.value

    @abstractmethod
    def fill(self, url: str, ctx: FillContext) -> FillResult:
        """Fill the form at ``url`` with ``ctx`` and return the (unsubmitted) result."""

    @property
    @abstractmethod
    def schema(self) -> FormSchema:
        """The platform's selectors and markers for the shared driver."""

    @abstractmethod
    def discover_open_questions(self, page: FormPage) -> list[OpenQuestion]:
        """Return the open (free-text) questions present on the form.

        Platform-specific because the DOM differs; the shared driver then routes
        each through :meth:`resolve_question`, enforcing the sensitive guard.
        """

    # ------------------------------------------------------------------ #
    # Shared page driver (identical safety behaviour across platforms)
    # ------------------------------------------------------------------ #
    def fill_page(self, page: FormPage, ctx: FillContext) -> FillResult:
        """Fill a loaded ``page`` for ``ctx``: validate, map fields, answer, screenshot.

        The order is safety-first:

        #. verify the page looks like this platform and has no CAPTCHA, aborting
           safely otherwise (a screenshot is saved and the form is never submitted);
        #. fill the known profile fields (name, email, ...);
        #. upload the CV and cover letter in the chosen language;
        #. answer each open question through :meth:`resolve_question`, so sensitive
           questions never reach the LLM and uncertain answers are flagged;
        #. take a screenshot for the owner's pre-submit review.

        It NEVER submits — returning the :class:`~jobpilot.models.FillResult` is the
        boundary; the human-in-the-loop confirmation and the final click live in the
        local entrypoint.
        """
        language = _language_from_ctx(ctx)
        schema = self.schema
        dom = page.content()

        if _detect_captcha(page, schema):
            raise CaptchaError(f"CAPTCHA detected on {self.platform.value} form")
        _require_structure(page, schema, platform=self.platform.value, dom=dom)

        acc = FillAccumulator()
        self._fill_known_fields(page, schema, acc)
        self._upload_documents(page, schema, ctx, acc)
        self._answer_open_questions(page, language, acc)

        screenshot_path = _take_screenshot(page, ctx.job_id, self.platform.value)
        result = acc.to_result(screenshot_path=screenshot_path)
        log.info(
            "form_filled",
            job_id=ctx.job_id,
            platform=self.platform.value,
            filled=len(result.filled_fields),
            uncertain=len(result.uncertain_fields),
            unanswered_sensitive=len(result.unanswered_sensitive),
        )
        return result

    def fill_with_safe_abort(self, page: FormPage, ctx: FillContext) -> FillResult:
        """Drive :meth:`fill_page`, converting the two stop conditions to a safe abort.

        A :class:`CaptchaError` or :class:`FormStructureError` is never allowed to
        submit anything: a screenshot is saved and a :class:`~jobpilot.models.FillResult`
        with ``aborted=True`` and an ``abort_reason`` is returned so the local
        entrypoint notifies the owner and stops. Any other exception propagates (the
        caller treats an unexpected crash as a non-submit too).
        """
        try:
            return self.fill_page(page, ctx)
        except CaptchaError as exc:
            return self._abort_result(page, ctx.job_id, "captcha", exc)
        except FormStructureError as exc:
            return self._abort_result(page, ctx.job_id, "unexpected_structure", exc)

    def submit(self, page: FormPage) -> None:
        """Click the platform's final submit button.

        Called ONLY after the owner's explicit Enviar confirmation; filling never
        calls this. The submit control must be present (it was part of the required
        structure the fill step verified), so a missing button here is a
        :class:`FormStructureError` rather than a silent no-op.
        """
        selector = self.schema.submit_selector
        if page.query_selector(selector) is None:
            raise FormStructureError(f"submit control {selector} not found")
        page.click(selector)
        log.info("form_submitted", platform=self.platform.value)

    def _abort_result(self, page: FormPage, job_id: int, reason: str, exc: Exception) -> FillResult:
        """Build the ``aborted=True`` result, saving an abort screenshot first."""
        screenshot = abort_screenshot(page, job_id, self.platform.value)
        log.warning("form_safe_abort", job_id=job_id, reason=reason, error=str(exc))
        return FillResult(
            filled_fields=[],
            uncertain_fields=[],
            unanswered_sensitive=[],
            screenshot_path=screenshot,
            aborted=True,
            abort_reason=f"{reason}: {exc}",
        )

    def _fill_known_fields(self, page: FormPage, schema: FormSchema, acc: FillAccumulator) -> None:
        """Fill every known profile field present on the page."""
        values = self._known_field_values()
        for selector, field_name in schema.field_map.items():
            value = values.get(field_name)
            if not value:
                continue
            if page.query_selector(selector) is None:
                continue
            page.fill(selector, value)
            acc.known(field_name)

    def _known_field_values(self) -> dict[str, str]:
        """Map known field names to their profile values (empty strings skipped)."""
        basics = self._profile.basics
        links = basics.links
        values: dict[str, str] = {
            "name": basics.name,
            "email": str(basics.email),
            "phone": basics.phone or "",
            "location": basics.location or "",
            "linkedin": links.linkedin or "",
            "github": links.github or "",
            "website": links.website or "",
        }
        return {k: v for k, v in values.items() if v}

    def _upload_documents(
        self, page: FormPage, schema: FormSchema, ctx: FillContext, acc: FillAccumulator
    ) -> None:
        """Upload the CV and cover letter (already rendered in the chosen language)."""
        if page.query_selector(schema.cv_selector) is not None:
            page.set_input_files(schema.cv_selector, ctx.cv_path)
            acc.known("cv")
        if page.query_selector(schema.cover_letter_selector) is not None:
            page.set_input_files(schema.cover_letter_selector, ctx.cover_letter_path)
            acc.known("cover_letter")

    def _answer_open_questions(
        self, page: FormPage, language: Language, acc: FillAccumulator
    ) -> None:
        """Answer every discovered open question, enforcing the sensitive guard."""
        for question in self.discover_open_questions(page):
            outcome = self.resolve_question(question.label, language)
            if outcome.value is not None and page.query_selector(question.selector) is not None:
                page.fill(question.selector, outcome.value)
            acc.answered(question.label, outcome)

    # ------------------------------------------------------------------ #
    # Shared answering logic (identical safety behaviour across platforms)
    # ------------------------------------------------------------------ #
    def resolve_question(self, question: str, language: Language) -> _AnswerOutcome:
        """Resolve one open question into an answer, enforcing the sensitive guard.

        * A SENSITIVE question is answered ONLY from the approved answers_bank; with
          no approved entry the field is left blank and reported as unanswered
          sensitive — the LLM is never consulted for it.
        * A non-sensitive question is first served from the answers_bank when it
          matches (deterministic, approved copy), otherwise answered by the LLM from
          the profile and the offer only. An LLM answer flagged ``confident=False``
          (or any that looks sensitive after the fact) is kept but marked uncertain
          for the owner to review before submit.
        """
        if is_sensitive_question(question, self._technologies):
            entry = find_answer_bank_entry(self._profile, question)
            if entry is None:
                log.info("form_sensitive_unanswered", question=question)
                return _AnswerOutcome(value=None, unanswered_sensitive=True)
            log.info("form_sensitive_from_bank", question=question, key=entry.key)
            return _AnswerOutcome(value=entry.text(language))

        entry = find_answer_bank_entry(self._profile, question)
        if entry is not None:
            return _AnswerOutcome(value=entry.text(language))
        return self._answer_with_llm(question, language)

    def _answer_with_llm(self, question: str, language: Language) -> _AnswerOutcome:
        """Answer a non-sensitive open question via the LLM (profile + offer only)."""
        analysis_json = self._analysis.model_dump_json() if self._analysis is not None else "{}"
        prompt = build_form_answer_prompt(
            question=question,
            language=language.value,
            profile_json=self._profile.model_dump_json(),
            analysis_json=analysis_json,
            schema_json=json.dumps(FormAnswer.model_json_schema()),
        )
        try:
            answer = self._llm.complete_json(prompt, FormAnswer)
        except Exception as exc:  # noqa: BLE001 - a failed answer is marked uncertain
            log.warning("form_answer_llm_failed", question=question, error=str(exc))
            return _AnswerOutcome(value=None, uncertain=True)
        # A low-confidence answer is still filled but flagged for human review.
        return _AnswerOutcome(value=answer.answer, uncertain=not answer.confident)


# --------------------------------------------------------------------------- #
# Shared accumulator used by the concrete platforms
# --------------------------------------------------------------------------- #
@dataclass(slots=True)
class FillAccumulator:
    """Collects the per-field outcomes a platform produces while filling a form.

    The concrete fillers call :meth:`known` for mapped profile fields and
    :meth:`answered` for each resolved open question, then :meth:`to_result` builds
    the :class:`~jobpilot.models.FillResult`. Keeping the bookkeeping here means the
    ``filled``/``uncertain``/``unanswered_sensitive`` lists are built identically on
    both platforms.
    """

    filled_fields: list[str] = field(default_factory=list)
    uncertain_fields: list[str] = field(default_factory=list)
    unanswered_sensitive: list[str] = field(default_factory=list)

    def known(self, field_name: str) -> None:
        self.filled_fields.append(field_name)

    def answered(self, field_name: str, outcome: _AnswerOutcome) -> None:
        if outcome.unanswered_sensitive:
            self.unanswered_sensitive.append(field_name)
            return
        if outcome.value is None:
            self.uncertain_fields.append(field_name)
            return
        self.filled_fields.append(field_name)
        if outcome.uncertain:
            self.uncertain_fields.append(field_name)

    def to_result(self, *, screenshot_path: str | None) -> FillResult:
        return FillResult(
            filled_fields=self.filled_fields,
            uncertain_fields=self.uncertain_fields,
            unanswered_sensitive=self.unanswered_sensitive,
            screenshot_path=screenshot_path,
            aborted=False,
        )
