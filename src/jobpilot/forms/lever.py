"""Lever assisted form filler (Playwright, LOCAL only).

Lever application forms expose named inputs (``input[name="name"]``,
``input[name="email"]``, ...), a resume upload input, and "additional information"
custom questions rendered as labelled text areas. This filler maps the known
profile fields, uploads the CV and cover letter in the chosen language, and routes
every custom question through the shared
:meth:`~jobpilot.forms.base.FormFiller.resolve_question`, so sensitive questions
never reach the LLM.

As with Greenhouse, all the safety behaviour (CAPTCHA stop, unexpected-structure
safe abort, uncertain tracking, screenshot, never-submit boundary) lives on the base
class; this module only supplies the Lever selectors and its custom-question
discovery. Playwright is opened lazily via :mod:`jobpilot.forms.runtime`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from jobpilot.forms.base import (
    FormFiller,
    FormSchema,
    OpenQuestion,
    Platform,
)
from jobpilot.forms.runtime import open_form_page

if TYPE_CHECKING:
    from jobpilot.config import FormsConfig
    from jobpilot.forms.base import FormPage
    from jobpilot.interfaces import FillContext, FillResult, LlmClient
    from jobpilot.models import JobAnalysis
    from jobpilot.profile.model import ProfileModel

_LEVER_SCHEMA = FormSchema(
    field_map={
        "input[name='name']": "name",
        "input[name='email']": "email",
        "input[name='phone']": "phone",
        "input[name='org']": "location",
        "input[name='urls[LinkedIn]']": "linkedin",
        "input[name='urls[GitHub]']": "github",
    },
    cv_selector="input[name='resume']",
    cover_letter_selector="input[name='cover_letter']",
    required_markers=(".application-form", "input[name='email']"),
    captcha_markers=("iframe[src*='recaptcha']", ".g-recaptcha", "#px-captcha"),
    submit_selector="button[data-qa='btn-submit']",
)


class LeverFormFiller(FormFiller):
    """Fill (never submit) a Lever application form."""

    platform = Platform.lever

    def __init__(
        self,
        profile: ProfileModel,
        llm: LlmClient,
        config: FormsConfig,
        analysis: JobAnalysis | None = None,
    ) -> None:
        super().__init__(profile, llm, analysis)
        self._config = config

    @property
    def schema(self) -> FormSchema:
        return _LEVER_SCHEMA

    def discover_open_questions(self, page: FormPage) -> list[OpenQuestion]:
        """Return the Lever "additional information" questions as open questions.

        Lever renders custom cards whose label is the question text and whose
        textarea carries a stable name; ``query_question_fields`` (implemented by the
        real page and the test fake alike) returns ``(selector, label)`` pairs, so
        this DOM detail stays out of the shared driver.
        """
        getter = getattr(page, "query_question_fields", None)
        if not callable(getter):
            return []
        pairs = getter()
        return [OpenQuestion(selector=selector, label=label) for selector, label in pairs]

    def fill(self, url: str, ctx: FillContext) -> FillResult:
        """Open the Lever form at ``url`` and fill it, never submitting.

        On a CAPTCHA or an unexpected DOM the run aborts safely: a screenshot is
        saved and a :class:`~jobpilot.models.FillResult` with ``aborted=True`` is
        returned so the caller notifies the owner and submits nothing.
        """
        with open_form_page(url, self._config) as page:
            return self.fill_with_safe_abort(page, ctx)
