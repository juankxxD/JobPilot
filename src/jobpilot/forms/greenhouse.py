"""Greenhouse assisted form filler (Playwright, LOCAL only).

Greenhouse application forms expose a stable set of field ids (``#first_name``,
``#email``, ...), a resume and cover-letter file input, and a list of custom
questions rendered as labelled inputs. This filler maps the known profile fields,
uploads the CV and cover letter in the chosen language, and routes every custom
question through the shared :meth:`~jobpilot.forms.base.FormFiller.resolve_question`
so sensitive questions never reach the LLM.

All the safety behaviour (CAPTCHA stop, unexpected-structure safe abort, uncertain
tracking, screenshot, never-submit boundary) lives on the base class; this module
only provides the Greenhouse selectors and its custom-question discovery. Playwright
is opened lazily via :mod:`jobpilot.forms.runtime`, so importing this module needs
no browser.
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

_GREENHOUSE_SCHEMA = FormSchema(
    field_map={
        "#first_name": "name",
        "#email": "email",
        "#phone": "phone",
        "#job_application_location": "location",
        "#job_application_answers_attributes_linkedin": "linkedin",
    },
    cv_selector="input#resume",
    cover_letter_selector="input#cover_letter",
    required_markers=("#application_form", "#email"),
    captcha_markers=("iframe[src*='recaptcha']", ".g-recaptcha", "#px-captcha"),
    submit_selector="#submit_app",
)


class GreenhouseFormFiller(FormFiller):
    """Fill (never submit) a Greenhouse application form."""

    platform = Platform.greenhouse

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
        return _GREENHOUSE_SCHEMA

    def discover_open_questions(self, page: FormPage) -> list[OpenQuestion]:
        """Return the Greenhouse custom questions as open-question descriptors.

        Greenhouse renders each custom question inside ``.field`` wrappers whose
        ``<label>`` is the visible question text and whose ``<textarea>`` carries a
        stable ``id``. ``query_question_fields`` is a thin page method the real
        Playwright page and the test fake both implement, returning ``(selector,
        label)`` pairs; keeping the extraction on the page keeps this DOM-shape
        detail out of the shared driver.
        """
        getter = getattr(page, "query_question_fields", None)
        if not callable(getter):
            return []
        pairs = getter()
        return [OpenQuestion(selector=selector, label=label) for selector, label in pairs]

    def fill(self, url: str, ctx: FillContext) -> FillResult:
        """Open the Greenhouse form at ``url`` and fill it, never submitting.

        On a CAPTCHA or an unexpected DOM the run aborts safely: a screenshot is
        saved and a :class:`~jobpilot.models.FillResult` with ``aborted=True`` is
        returned so the caller notifies the owner and submits nothing.
        """
        with open_form_page(url, self._config) as page:
            return self.fill_with_safe_abort(page, ctx)
