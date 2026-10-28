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


class SubmitAmbiguousError(RuntimeError):
    """Raised when a submit click returns but success cannot be confirmed.

    After the final click the platform must present a recognised success state and
    no new anti-bot / validation challenge. When neither a success nor a clear
    failure can be established — a timeout, a silent validation error, or a changed
    page — the attempt is AMBIGUOUS: the remote site may or may not have accepted it,
    so nothing is recorded and the owner is told to reconcile it manually. It is a
    stop condition distinct from a clean safe abort because a submission may already
    have been accepted (P8-008).
    """


class IncompleteFormError(FormStructureError):
    """Raised when a required visible control is missing or left blank.

    A subclass of :class:`FormStructureError` so it is handled identically — safe
    abort with a screenshot and notification, never a submit — while carrying the
    specific labels of the controls that are not complete so the owner is told
    exactly what is missing (R15.7).
    """

    def __init__(self, missing: list[str]) -> None:
        self.missing = missing
        super().__init__(f"required controls not completed: {missing}")


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


def _split_name(full_name: str) -> tuple[str, str]:
    """Split an owner-approved full name into (first_name, last_name).

    The first whitespace-separated token is the first name; everything after it is
    the last name. A single-token name yields an empty last name, which the required
    -field validation flags so the owner completes it rather than the system guessing.
    No data is invented — only the owner's own ``basics.name`` is partitioned.
    """
    parts = full_name.split()
    if not parts:
        return "", ""
    if len(parts) == 1:
        return parts[0], ""
    return parts[0], " ".join(parts[1:])


def is_unsupported_url(url: str) -> bool:
    """Return whether ``url`` is an explicitly out-of-scope platform (LinkedIn/Indeed).

    These are never driven even if their DOM later resembles a supported ATS, so the
    apply flow rejects them before a browser opens.
    """
    return _matches_any(url, _UNSUPPORTED_URL_MARKERS)


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
    # salary expectation / compensation
    "salary",
    "compensation",
    "pay expectation",
    "expected pay",
    "expected pay rate",
    "desired pay",
    "desired compensation",
    "pay range",
    "rate expectation",
    "remuneration",
    "pretensión salarial",
    "aspiración salarial",
    "expectativa salarial",
    "expectativa de salario",
    "salario",
    "remuneración",
    "sueldo",
    # work authorization / visa / eligibility
    "work authorization",
    "work authorisation",
    "authorized to work",
    "authorised to work",
    "legally authorized",
    "legally authorised",
    "legally eligible",
    "eligible to work",
    "eligible for employment",
    "legal right to work",
    "right to work",
    "lawfully permitted to work",
    "lawfully authorized",
    "lawfully authorised",
    "permitted to work",
    "permanently authorized",
    "permanently authorised",
    "require sponsorship",
    "need sponsorship",
    "visa",
    "sponsorship",
    "work permit",
    "employment authorization",
    "citizenship",
    "citizen",
    "citizen of",
    "security clearance",
    "clearance",
    "autorización de trabajo",
    "autorización para trabajar",
    "permiso de trabajo",
    "patrocinio",
    "ciudadanía",
    "nacionalidad",
    # relocation
    "relocate",
    "relocation",
    "willing to move",
    "willing to relocate",
    "able to relocate",
    "open to relocation",
    "open to moving",
    "open to move",
    "move to",
    "moving to",
    "move for",
    "moving for",
    "commute to",
    "reubicar",
    "reubicación",
    "reubicarse",
    "dispuesto a mudarte",
    "dispuesta a mudarte",
    "mudarte",
    "trasladarte",
    "mudar",
    # background / legal
    "background check",
    "criminal",
    "felony",
    "misdemeanor",
    "misdemeanour",
    "conviction",
    "convicted",
    "arrest",
    "arrested",
    "charged with",
    "offense",
    "offence",
    "incarcerat",
    "probation",
    "parole",
    "legal status",
    "non-compete",
    "noncompete",
    "non compete",
    "disciplinary",
    "litigation",
    "lawsuit",
    "drug test",
    "drug screening",
    "antecedentes",
    "penales",
    "judiciales",
    "no competencia",
    "cláusula de no competencia",
    # demographics (EEO and similar)
    "gender",
    "pronoun",
    "pronouns",
    "race",
    "ethnicity",
    "ethnic",
    "ethnic origin",
    "national origin",
    "hispanic",
    "latino",
    "latina",
    "latinx",
    "latin origin",
    "disability",
    "veteran",
    "sexual orientation",
    "marital status",
    "religion",
    "religious",
    "nationality",
    "age",
    "date of birth",
    "birth date",
    "birthdate",
    "género",
    "pronombres",
    "raza",
    "etnia",
    "etnicidad",
    "discapacidad",
    "veterano",
    "orientación sexual",
    "estado civil",
    "religión",
    "edad",
    "fecha de nacimiento",
)

# A question is sensitive when it asks for years of experience (e.g. "How many years
# of experience do you have with Java?"). We detect the years-of-experience phrasing
# and, when it names a concrete subject (a technology), always treat it as sensitive —
# regardless of whether that technology appears in the candidate's profile. R15.5
# forbids ANY years-in-a-specific-technology question from reaching the LLM, so the
# detection must not depend on the profile catalogue.
_YEARS_PATTERNS: tuple[str, ...] = (
    "years of experience",
    "years of ",
    "years experience",
    "year of experience",
    "yrs of experience",
    "yrs experience",
    "years using",
    "years with",
    "years of hands-on",
    "years hands-on",
    "number of years",
    "how many years",
    "(years)",
    "(yrs)",
    "in years",
    "- years",
    "– years",
    "— years",
    ": years",
    "years:",
    "years experience",
    " years",
    "years ",
    "años de experiencia",
    "año de experiencia",
    "años usando",
    "años con",
    "número de años",
    "cuántos años",
    "cuantos años",
    "(años)",
)

# Words that commonly carry the years-of-experience phrasing WITHOUT naming a concrete
# technology (generic tenure questions). These are stripped before deciding whether a
# question names a specific subject, so "How many years of total experience?" is not
# mistaken for a tech-specific one. A bare generic-tenure question is still routed
# through the normal (non-sensitive) path unless another sensitive marker matches.
_YEARS_GENERIC_SUBJECTS: tuple[str, ...] = (
    "total",
    "overall",
    "professional",
    "relevant",
    "work",
    "working",
    "software",
    "development",
    "engineering",
    "programming",
    "general",
    "totales",
    "profesional",
    "profesionales",
    "relevante",
    "relevantes",
    "laboral",
    "desarrollo",
    "programación",
    "programacion",
)

# Tokens that appear in years-of-experience phrasing itself; dropped when probing for
# a named technology so the phrasing words are not mistaken for a subject.
_YEARS_PHRASE_TOKENS: frozenset[str] = frozenset(
    {
        "how",
        "many",
        "number",
        "num",
        "years",
        "year",
        "yrs",
        "of",
        "experience",
        "hands",
        "on",
        "do",
        "you",
        "have",
        "with",
        "in",
        "using",
        "use",
        "the",
        "a",
        "an",
        "your",
        "please",
        "specify",
        "enter",
        "indicate",
        "state",
        "cuantos",
        "cuántos",
        "numero",
        "número",
        "anos",
        "años",
        "ano",
        "año",
        "de",
        "experiencia",
        "tienes",
        "tengo",
        "con",
        "en",
        "usando",
        "uso",
        "tiene",
        "el",
        "la",
        "los",
        "las",
        "tu",
        "indica",
        "especifica",
    }
)


def _asks_years_in_technology(question: str) -> bool:
    """Return whether a question asks for years of experience in a specific subject.

    This looks only at the *shape* of the question, never at the profile catalogue:
    a years-of-experience phrasing that names any concrete subject (a technology,
    tool or stack) is sensitive, so it is answered only from the approved
    ``answers_bank`` or left blank — the LLM is never asked to quantify tech-specific
    tenure (R15.5). A generic-tenure question ("years of total experience") names no
    technology and is not treated as sensitive by this rule.
    """
    lowered = question.lower()
    if not any(p in lowered for p in _YEARS_PATTERNS):
        return False
    # Tokenise and drop the years-phrasing words and the known generic subjects; if
    # any content token remains, the question names a specific subject (a technology).
    tokens = re.findall(r"[a-zA-Z0-9.+#]+", lowered)
    remaining = [
        tok
        for tok in tokens
        if tok not in _YEARS_PHRASE_TOKENS and tok not in _YEARS_GENERIC_SUBJECTS
    ]
    return bool(remaining)


def is_sensitive_question(question: str) -> bool:
    """Return whether a form question is SENSITIVE and must never reach the LLM.

    Sensitive questions are answered only from the approved ``answers_bank`` (or left
    blank and reported) — never by the LLM. The years-of-experience-in-a-technology
    case is detected from the question shape alone, independent of the profile.
    """
    if _matches_any(question, _SENSITIVE_PATTERNS):
        return True
    return _asks_years_in_technology(question)


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
# Custom-question discovery (shared, via the real Playwright element API)
# --------------------------------------------------------------------------- #
def _control_selector(control: FormElement) -> str | None:
    """Build a stable CSS selector for a question's input/textarea control.

    Prefers the control's ``id`` (``#id``) and falls back to its ``name``
    (``[name="..."]``); a control with neither cannot be reliably targeted, so it is
    skipped by the caller rather than guessed at.
    """
    control_id = control.get_attribute("id")
    if control_id:
        return f"#{control_id}"
    name = control.get_attribute("name")
    if name:
        return f'[name="{name}"]'
    return None


# Selectors for the free-text controls the driver can answer. Anything matched by
# ``question_container_selector`` whose control is NOT one of these (a select, a
# radio/checkbox group, a number spinner) is a control the driver cannot safely
# answer; when such a control is required it must abort the form rather than guess.
_OPEN_TEXT_CONTROL_SELECTOR = (
    "textarea, input[type='text'], input[type='email'], input[type='url'], "
    "input[type='tel'], input:not([type])"
)
# Controls that look like a question but are NOT free-text (so the driver cannot
# author an answer for them). A required one aborts the form.
_NON_TEXT_CONTROL_SELECTOR = (
    "select, input[type='radio'], input[type='checkbox'], input[type='number']"
)


def discover_questions(page: FormPage, schema: FormSchema) -> list[OpenQuestion]:
    """Discover the form's answerable custom questions via the real element API.

    Each container matched by ``schema.question_container_selector`` is read with
    ``query_selector_all`` and inspected with ``ElementHandle`` methods
    (``inner_text`` for the label, ``query_selector`` for the control, ``get_attribute``
    for a stable selector). Only free-text controls are returned here — those the
    driver can actually answer. Non-text question controls (selects, radios,
    checkboxes, number spinners) are handled by :func:`validate_required_controls`,
    which aborts the form when such a control is required, so an unanswerable
    required question is never left silently blank (R15.3/R15.7).
    """
    questions: list[OpenQuestion] = []
    for container in page.query_selector_all(schema.question_container_selector):
        control = container.query_selector(_OPEN_TEXT_CONTROL_SELECTOR)
        if control is None:
            continue
        selector = _control_selector(control)
        if selector is None:
            continue
        label = container.inner_text().strip()
        if not label:
            continue
        questions.append(OpenQuestion(selector=selector, label=label))
    return questions


def _control_kind(control: FormElement) -> str:
    """Classify a non-text control as ``select``/``radio``/``checkbox``/``number``."""
    control_type = (control.get_attribute("type") or "").lower()
    if control_type in {"radio", "checkbox", "number"}:
        return control_type
    return "select"


#: Affirmative tokens that may check a sensitive checkbox from an approved-bank answer.
_AFFIRMATIVE_TOKENS: frozenset[str] = frozenset({"yes", "y", "true", "sí", "si"})


def _is_numeric(value: str) -> bool:
    """Return whether ``value`` is a plain non-negative number (for a number input)."""
    try:
        float(value)
    except ValueError:
        return False
    return True


def _control_option_values(page: FormPage, selector: str) -> list[str]:
    """Return the selectable option values/labels for a select or radio control.

    Reads the control's ``<option>`` children (a select) or the sibling radio inputs
    sharing its name, so a sensitive answer is only applied when it exactly matches
    one offered choice. Each option contributes both its ``value`` attribute and its
    visible text, so an approved answer can match either.
    """
    control = page.query_selector(selector)
    if control is None:
        return []
    values: list[str] = []
    for option in control.query_selector_all("option"):
        value = option.get_attribute("value")
        if value:
            values.append(value)
        text = option.inner_text().strip()
        if text:
            values.append(text)
    # A radio group exposes its own value directly on the input.
    own_value = control.get_attribute("value")
    if own_value:
        values.append(own_value)
    return values


def _exact_option_match(options: list[str], value: str) -> str | None:
    """Return the option exactly matching ``value`` (case-insensitive), else ``None``.

    Matching is exact (not substring) so a sensitive answer is never applied to an
    option it only partially resembles; ambiguity leaves the control blank.
    """
    lowered = value.lower()
    matches = [opt for opt in options if opt.lower() == lowered]
    if len(matches) == 1 or (matches and len({m.lower() for m in matches}) == 1):
        return matches[0]
    return None


def discover_nontext_questions(page: FormPage, schema: FormSchema) -> list[NonTextQuestion]:
    """Discover custom-question controls the driver cannot free-type an answer into.

    Each container matched by ``schema.question_container_selector`` whose control is
    a select, radio, checkbox or number input is returned with its label, a stable
    selector, its kind and whether it is marked required. These are routed through
    :meth:`FormFiller.resolve_nontext_question` so a SENSITIVE non-text question is
    answered only by an exact approved-bank match, otherwise left blank and reported;
    the LLM is never consulted for one (R15.5). A required one with no safe answer is
    caught by :func:`validate_required_controls` and aborts the form (R15.3/R15.7).
    """
    questions: list[NonTextQuestion] = []
    for container in page.query_selector_all(schema.question_container_selector):
        if container.query_selector(_OPEN_TEXT_CONTROL_SELECTOR) is not None:
            continue  # answerable free-text question, handled by discover_questions
        control = container.query_selector(_NON_TEXT_CONTROL_SELECTOR)
        if control is None:
            continue
        selector = _control_selector(control)
        if selector is None:
            continue
        label = container.inner_text().strip()
        if not label:
            continue
        required = control.get_attribute("required") is not None or (
            container.get_attribute("required") is not None
        )
        questions.append(
            NonTextQuestion(
                selector=selector,
                label=label,
                kind=_control_kind(control),
                required=required,
            )
        )
    return questions


def _control_is_complete(control: FormElement) -> bool:
    """Return whether a single control carries an acceptable, non-empty answer.

    * a radio/checkbox is complete only when it is checked;
    * any other control (text, textarea, select, number) is complete only when it
      has a non-blank value.

    A hidden control is never complete: the owner cannot see or correct it.
    """
    if not control.is_visible():
        return False
    control_type = (control.get_attribute("type") or "").lower()
    if control_type in {"radio", "checkbox"}:
        return control.is_checked()
    return bool(control.input_value().strip())


def _label_for(container: FormElement, control: FormElement) -> str:
    """Best-effort human label for a required control, for the owner's abort notice."""
    text = container.inner_text().strip()
    if text:
        return text
    for attr in ("aria-label", "name", "id"):
        value = control.get_attribute(attr)
        if value:
            return value
    return "(sin etiqueta)"


def discover_unanswerable_required_controls(page: FormPage, schema: FormSchema) -> list[str]:
    """Return labels of required question controls the driver cannot answer.

    Every container matched by ``question_container_selector`` whose control is a
    select, radio, checkbox or number input (not a free-text field) and which is
    marked required but is not already complete is reported. These are controls the
    driver must never guess at, so a required, incomplete one aborts the form safely
    (R15.3/R15.7).
    """
    unanswerable: list[str] = []
    for container in page.query_selector_all(schema.question_container_selector):
        if container.query_selector(_OPEN_TEXT_CONTROL_SELECTOR) is not None:
            continue  # answerable free-text question, handled elsewhere
        control = container.query_selector(_NON_TEXT_CONTROL_SELECTOR)
        if control is None:
            continue
        required = control.get_attribute("required") is not None or (
            container.get_attribute("required") is not None
        )
        if not required:
            continue
        if _control_is_complete(control):
            continue
        unanswerable.append(_label_for(container, control))
    return unanswerable


def discover_dom_required_controls(page: FormPage, schema: FormSchema) -> list[str]:
    """Return labels of every DOM-marked ``[required]`` control that is incomplete.

    Beyond each platform's static ``required_fields``, forms mark their own required
    controls with the ``required`` attribute. Every visible such control must carry
    a value (or be checked); any that does not is reported so the form aborts rather
    than reaching submit incomplete (R15.7). The look-up uses the real element API.
    """
    incomplete: list[str] = []
    for control in page.query_selector_all("[required]"):
        if not control.is_visible():
            incomplete.append(_dom_label(control))
            continue
        if not _control_is_complete(control):
            incomplete.append(_dom_label(control))
    return incomplete


def _dom_label(control: FormElement) -> str:
    """Label a bare DOM-required control for the owner's notice."""
    for attr in ("aria-label", "name", "id"):
        value = control.get_attribute(attr)
        if value:
            return value
    label = control.inner_text().strip()
    return label or "(control requerido)"


def validate_required_controls(page: FormPage, schema: FormSchema) -> list[str]:
    """Return the labels of EVERY required control that is missing, hidden or blank.

    This is the single completeness gate used both before asking the owner to
    confirm and again immediately before the final click. It unions three sources so
    nothing required can slip through to submit incomplete (R15.3/R15.7):

    #. the platform's static ``required_fields`` (name/email controls);
    #. every DOM-marked ``[required]`` control on the page;
    #. every required non-text question control (select/radio/checkbox/number) the
       driver cannot author an answer for.

    The returned list is empty only when every required control is present, visible
    and complete; a non-empty list is the caller's signal to abort safely.
    """
    incomplete: list[str] = []
    for required in schema.required_fields:
        control = page.query_selector(required.selector)
        if control is None or not _control_is_complete(control):
            incomplete.append(required.label)
    incomplete.extend(discover_dom_required_controls(page, schema))
    incomplete.extend(discover_unanswerable_required_controls(page, schema))
    # De-duplicate while preserving order (a control can appear via two sources).
    seen: set[str] = set()
    unique: list[str] = []
    for label in incomplete:
        if label not in seen:
            seen.add(label)
            unique.append(label)
    return unique


# --------------------------------------------------------------------------- #
# Page abstraction (a real Playwright Page satisfies this; tests pass a fake)
# --------------------------------------------------------------------------- #
@runtime_checkable
class FormElement(Protocol):
    """The minimal element-handle surface the shared driver reads.

    A real Playwright ``ElementHandle`` provides these with compatible signatures,
    so question discovery and required-control validation run against the real API
    (``query_selector_all`` returns these handles). Tests pass fakes of the same
    shape, so no browser is launched while still exercising the production path.
    """

    def get_attribute(self, name: str) -> str | None: ...

    def inner_text(self) -> str: ...

    def input_value(self) -> str: ...

    def is_visible(self) -> bool: ...

    def is_checked(self) -> bool: ...

    def query_selector(self, selector: str) -> FormElement | None: ...

    def query_selector_all(self, selector: str) -> list[FormElement]: ...


@runtime_checkable
class FormPage(Protocol):
    """The page surface a FormFiller drives, mirroring the real Playwright ``Page``.

    Every method here matches a real Playwright ``Page`` signature, so the shared
    driver's question discovery (:meth:`query_selector_all`) and required-control
    validation (``input_value``/``is_visible`` on the returned handles) exercise the
    exact API a local run uses. Tests pass a fake implementing the same shape so no
    browser is launched, and the production path is the path under test.
    """

    def content(self) -> str: ...

    def fill(self, selector: str, value: str) -> None: ...

    def set_input_files(self, selector: str, files: str) -> None: ...

    def file_input_value(self, selector: str) -> str: ...

    def select_option(self, selector: str, value: str) -> None: ...

    def check(self, selector: str) -> None: ...

    def query_selector(self, selector: str) -> FormElement | None: ...

    def query_selector_all(self, selector: str) -> list[FormElement]: ...

    def click(self, selector: str) -> None: ...

    def screenshot(self, *, path: str) -> None: ...


@dataclass(slots=True)
class OpenQuestion:
    """One open (free-text) question discovered on the form."""

    selector: str
    label: str


@dataclass(slots=True)
class NonTextQuestion:
    """One non-free-text question control (select/radio/checkbox/number).

    The driver never authors an answer for these: a SENSITIVE one is answered only
    by an exact approved-bank match (``kind`` decides how the match is applied), and
    otherwise left blank and reported. ``required`` tells the completeness gate
    whether a blank one must abort the form.
    """

    selector: str
    label: str
    kind: str
    required: bool


@dataclass(frozen=True, slots=True)
class RequiredField:
    """A control that must be present, visible and non-empty before submit.

    ``selector`` locates the input; ``label`` is the human name reported to the
    owner when the control is missing or left blank. Required controls are validated
    both before asking the owner to confirm and again immediately before the final
    click, so an incomplete form is never submitted (R15.7).
    """

    selector: str
    label: str


@dataclass(frozen=True, slots=True)
class FormSchema:
    """Platform-specific selectors and markers the shared driver needs.

    Each concrete platform supplies one of these so the field-mapping, file-upload,
    CAPTCHA-detection, question-discovery and structure-validation logic can live
    once on the ABC. The ``required_markers`` are selectors that MUST be present for
    the page to look like this platform; their absence triggers a safe abort
    (``FormStructureError``).
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
    #: Selector matching each custom-question container the driver discovers. Each
    #: matched element must expose a ``label`` (text) and a control (``textarea`` /
    #: ``input``) with a stable ``id`` or ``name`` the driver turns into a selector.
    question_container_selector: str
    #: Controls that must be present, visible and non-empty before the form can be
    #: submitted. A missing or blank required control aborts safely, never submits.
    required_fields: tuple[RequiredField, ...] = ()
    #: Whether the CV upload control MUST be present (its absence aborts the form).
    #: A CV is always expected on a real application, so this defaults to ``True``.
    cv_required: bool = True
    #: Optional selector for a free-text "additional information" control that serves
    #: as the cover-letter destination on platforms with no dedicated upload (Lever).
    #: When set and the dedicated upload is absent, the approved cover-letter *text*
    #: (``FillContext.cover_letter_text``) is written here — never regenerated by the
    #: LLM. ``None`` means the platform has no text fallback.
    cover_letter_text_selector: str | None = None
    #: Selectors that, after the final click, confirm the platform accepted the
    #: application (a confirmation banner / "thank you" page). At least one must be
    #: present post-click for the submit to be recorded; otherwise the attempt is
    #: AMBIGUOUS and left for manual reconciliation (P8-008).
    success_markers: tuple[str, ...] = ()
    #: Selectors that, after the final click, indicate the platform REJECTED the
    #: submission (a validation summary / error banner). Their presence means the
    #: click did not succeed, so nothing is recorded.
    post_submit_error_markers: tuple[str, ...] = ()


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

    def discover_open_questions(self, page: FormPage) -> list[OpenQuestion]:
        """Return the open (free-text) questions present on the form.

        Shared across platforms: the per-platform DOM differences are captured by
        ``schema.question_container_selector``, and :func:`discover_questions` reads
        each container with the real Playwright element API. The driver then routes
        every discovered question through :meth:`resolve_question`, enforcing the
        sensitive guard.
        """
        return discover_questions(page, self.schema)

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
        self._answer_nontext_questions(page, language, acc)

        # Never ask the owner to confirm an incomplete form. The full required-control
        # set (static fields, DOM-marked [required], and unanswerable required
        # selects/radios/checkboxes/number inputs) must be complete, and both the CV
        # and cover-letter documents must be actually attached. Any gap is a safe abort.
        self._ensure_complete(page, schema, ctx)

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
        except IncompleteFormError as exc:
            return self._abort_result(page, ctx.job_id, "incomplete_form", exc)
        except FormStructureError as exc:
            return self._abort_result(page, ctx.job_id, "unexpected_structure", exc)

    def submit(self, page: FormPage, ctx: FillContext) -> None:
        """Click the platform's final submit button, re-running every safety guard.

        Called ONLY after the owner's explicit Enviar confirmation; filling never
        calls this. The page may have changed during the Telegram review (a CAPTCHA
        may have appeared, a required control may have been reset, the DOM may have
        been re-rendered), so immediately before the click this re-runs the full
        safety gate:

        #. a CAPTCHA / anti-bot challenge raises :class:`CaptchaError` (never evaded);
        #. an unexpected structure or a missing submit control raises
           :class:`FormStructureError`;
        #. any required control that is now blank/hidden, or a cover letter / CV no
           longer attached, raises :class:`IncompleteFormError`.

        Then the click itself is wrapped: a Playwright failure (a detached/disabled
        control, a timeout) becomes a :class:`FormStructureError` so the caller
        safe-aborts with a screenshot instead of crashing. After the click the
        platform's post-submit state is verified — a new CAPTCHA raises
        :class:`CaptchaError`, a recognised rejection raises :class:`FormStructureError`,
        and an unconfirmable outcome raises :class:`SubmitAmbiguousError` — so a
        submission is recorded only when the platform clearly accepted it (P8-008).
        """
        schema = self.schema
        # Re-detect anti-bot and re-validate structure first: a challenge or DOM
        # change after the owner confirmed must stop the submit, not sail past it.
        if _detect_captcha(page, schema):
            log.warning("form_submit_blocked_captcha", platform=self.platform.value)
            raise CaptchaError(f"CAPTCHA detected on {self.platform.value} form before submit")
        _require_structure(page, schema, platform=self.platform.value, dom=page.content())
        selector = schema.submit_selector
        if page.query_selector(selector) is None:
            raise FormStructureError(f"submit control {selector} not found")
        # Re-validate completeness: nothing must have become blank or hidden and both
        # documents must still be attached. An incomplete form is never submitted.
        self._ensure_complete(page, schema, ctx)
        try:
            page.click(selector)
        except (CaptchaError, FormStructureError):
            raise
        except Exception as exc:  # noqa: BLE001 - any click failure is a safe abort
            log.warning("form_submit_click_failed", platform=self.platform.value, error=str(exc))
            raise FormStructureError(f"submit click failed: {exc}") from exc
        self._verify_post_submit(page, schema)
        log.info("form_submitted", platform=self.platform.value)

    def _verify_post_submit(self, page: FormPage, schema: FormSchema) -> None:
        """Verify the platform accepted the submission after the final click.

        Raises :class:`CaptchaError` if an anti-bot challenge appeared, a
        :class:`FormStructureError` if the platform shows a recognised rejection, and
        :class:`SubmitAmbiguousError` if neither a success marker nor a clear failure
        can be established. A clean success (a success marker present, no challenge,
        no error) returns normally so the caller records the application.
        """
        if _detect_captcha(page, schema):
            log.warning("form_post_submit_captcha", platform=self.platform.value)
            raise CaptchaError(
                f"CAPTCHA / anti-bot challenge after submit on {self.platform.value}"
            )
        errors = schema.post_submit_error_markers
        if any(page.query_selector(marker) is not None for marker in errors):
            log.warning("form_post_submit_rejected", platform=self.platform.value)
            raise FormStructureError(f"{self.platform.value} rejected the submission")
        if not schema.success_markers:
            return
        if any(page.query_selector(marker) is not None for marker in schema.success_markers):
            return
        log.warning("form_post_submit_ambiguous", platform=self.platform.value)
        raise SubmitAmbiguousError(
            f"could not confirm {self.platform.value} accepted the submission"
        )

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
        """Map known field names to their profile values (empty strings skipped).

        ``first_name``/``last_name`` are split from the owner-approved full name so a
        platform that has separate name controls (Greenhouse) gets each half in the
        right field instead of the whole name in ``first_name``. A single-token name
        fills only ``first_name`` and leaves ``last_name`` blank, which the required
        -field validation then flags so the owner completes it before submit.
        """
        basics = self._profile.basics
        links = basics.links
        first_name, last_name = _split_name(basics.name)
        values: dict[str, str] = {
            "name": basics.name,
            "first_name": first_name,
            "last_name": last_name,
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
        """Upload the CV and cover letter (already rendered in the chosen language).

        Both uploads are mandatory: a supported form must expose a CV control and a
        cover-letter control, and :meth:`_ensure_complete` aborts when either is
        absent. Here we upload whichever controls are present and record them; the
        completeness gate is what enforces that both landed.
        """
        if page.query_selector(schema.cv_selector) is not None:
            page.set_input_files(schema.cv_selector, ctx.cv_path)
            acc.known("cv")
        self._attach_cover_letter(page, schema, ctx, acc)

    def _attach_cover_letter(
        self, page: FormPage, schema: FormSchema, ctx: FillContext, acc: FillAccumulator
    ) -> None:
        """Place the approved cover letter into the platform's actual destination.

        A dedicated upload control takes the rendered PDF. A platform with no such
        control (Lever) instead writes the approved cover-letter *text* into its
        "additional information" control (``cover_letter_text_selector``) — never a
        letter regenerated by the LLM. If neither destination is usable the cover
        letter is simply not attached here; :meth:`_missing_documents` then decides
        whether that is a safe abort (it is, whenever a cover letter is expected).
        """
        if page.query_selector(schema.cover_letter_selector) is not None:
            page.set_input_files(schema.cover_letter_selector, ctx.cover_letter_path)
            acc.known("cover_letter")
            return
        text_selector = schema.cover_letter_text_selector
        if (
            text_selector is not None
            and ctx.cover_letter_text
            and page.query_selector(text_selector) is not None
        ):
            page.fill(text_selector, ctx.cover_letter_text)
            acc.known("cover_letter")

    def _missing_documents(self, page: FormPage, schema: FormSchema, ctx: FillContext) -> list[str]:
        """Return labels for any mandatory document that is not actually attached.

        This checks the live attachment STATE, not merely that an upload control
        exists: a DOM re-render during the Telegram review can keep the selectors but
        clear the file inputs, so a selector-only check would let an incomplete form
        reach submit (P8-007). A CV must be attached on every supported form. A cover
        letter must reach a real destination — the dedicated upload's file, or the
        approved cover-letter *text* in the platform's additional-information control
        (Lever) — otherwise it is missing and the form aborts, never submitting a
        CV-only application (P8-011/R15.3).
        """
        missing: list[str] = []
        if schema.cv_required and not self._cv_attached(page, schema):
            missing.append("CV (hoja de vida)")
        if not self._cover_letter_attached(page, schema, ctx):
            missing.append("Carta de presentación")
        return missing

    def _cv_attached(self, page: FormPage, schema: FormSchema) -> bool:
        """Return whether the CV file input currently holds an attached file."""
        if page.query_selector(schema.cv_selector) is None:
            return False
        return bool(page.file_input_value(schema.cv_selector).strip())

    def _cover_letter_attached(self, page: FormPage, schema: FormSchema, ctx: FillContext) -> bool:
        """Return whether the approved cover letter reached a real destination.

        A dedicated upload must currently hold a file; otherwise the additional
        -information text control must hold the approved cover-letter text. Either is
        acceptable; neither means the cover letter is not attached and the form must
        abort (never a CV-only submit).
        """
        if page.query_selector(schema.cover_letter_selector) is not None:
            return bool(page.file_input_value(schema.cover_letter_selector).strip())
        text_selector = schema.cover_letter_text_selector
        if text_selector is not None and page.query_selector(text_selector) is not None:
            control = page.query_selector(text_selector)
            return control is not None and bool(control.input_value().strip())
        return False

    def _ensure_complete(self, page: FormPage, schema: FormSchema, ctx: FillContext) -> None:
        """Abort safely unless every required control and both documents are complete.

        Unions the required-control gate with the mandatory CV + cover-letter
        attachment check so an incomplete form — a blank/hidden required field, an
        unanswerable required select/radio/checkbox/number input, or a document not
        actually attached — is never shown to the owner for confirmation nor
        submitted (R15.3/R15.7).
        """
        incomplete = [
            *self._missing_documents(page, schema, ctx),
            *validate_required_controls(page, schema),
        ]
        if incomplete:
            log.warning(
                "form_required_incomplete", platform=self.platform.value, missing=incomplete
            )
            raise IncompleteFormError(incomplete)

    def _answer_open_questions(
        self, page: FormPage, language: Language, acc: FillAccumulator
    ) -> None:
        """Answer every discovered open question, enforcing the sensitive guard."""
        for question in self.discover_open_questions(page):
            outcome = self.resolve_question(question.label, language)
            if outcome.value is not None and page.query_selector(question.selector) is not None:
                page.fill(question.selector, outcome.value)
            acc.answered(question.label, outcome)

    def _answer_nontext_questions(
        self, page: FormPage, language: Language, acc: FillAccumulator
    ) -> None:
        """Handle select/radio/checkbox/number questions, enforcing the guard.

        A non-text question is never authored by the LLM. A SENSITIVE one is answered
        only when an exact approved-bank entry matches and the match can be applied to
        the control (an option selected, a radio/checkbox checked, a numeric value
        filled); otherwise it is left blank and reported in ``unanswered_sensitive``.
        A non-sensitive non-text question is left untouched here — the driver cannot
        safely author a choice — and a required blank one is caught by the
        completeness gate (:func:`validate_required_controls`), which aborts the form
        rather than guessing (R15.3/R15.5/R15.7).
        """
        for question in discover_nontext_questions(page, self.schema):
            if not is_sensitive_question(question.label):
                # The LLM must never choose an option/radio/checkbox for the owner, so
                # a non-sensitive non-text control is left as-is; the required-control
                # gate aborts it when blank-and-required.
                continue
            entry = find_answer_bank_entry(self._profile, question.label)
            applied = False
            if entry is not None:
                applied = self._apply_bank_to_nontext(page, question, entry.text(language))
            if applied:
                log.info(
                    "form_sensitive_nontext_from_bank", question=question.label, kind=question.kind
                )
                acc.known(question.label)
            else:
                log.info("form_sensitive_nontext_unanswered", question=question.label)
                acc.answered(question.label, _AnswerOutcome(value=None, unanswered_sensitive=True))

    def _apply_bank_to_nontext(self, page: FormPage, question: NonTextQuestion, value: str) -> bool:
        """Apply an approved-bank ``value`` to a non-text control by exact matching.

        Returns whether the value could be applied. For a select/radio the value must
        exactly match an available option (case-insensitive); for a checkbox it must
        be an affirmative token; for a number it must be numeric. Anything that is not
        an exact, unambiguous match is refused so the control stays blank and is
        reported rather than guessed (R15.5).
        """
        if page.query_selector(question.selector) is None:
            return False
        normalized = value.strip()
        if not normalized:
            return False
        if question.kind in {"select", "radio"}:
            options = _control_option_values(page, question.selector)
            match = _exact_option_match(options, normalized)
            if match is None:
                return False
            if question.kind == "select":
                page.select_option(question.selector, match)
            else:
                page.check(question.selector)
            return True
        if question.kind == "checkbox":
            if normalized.lower() not in _AFFIRMATIVE_TOKENS:
                return False
            page.check(question.selector)
            return True
        if question.kind == "number":
            if not _is_numeric(normalized):
                return False
            page.fill(question.selector, normalized)
            return True
        return False

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
        if is_sensitive_question(question):
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
