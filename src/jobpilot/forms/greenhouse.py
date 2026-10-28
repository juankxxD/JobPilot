"""Greenhouse assisted form filler (Playwright, LOCAL only).

Greenhouse application forms expose a stable set of field ids (``#first_name``,
``#email``, ...), a resume and cover-letter file input, and a list of custom
questions rendered as labelled inputs. This filler maps the known profile fields,
uploads the CV and cover letter in the chosen language, and routes every custom
question through the shared :meth:`~jobpilot.forms.base.FormFiller.resolve_question`
so sensitive questions never reach the LLM.

All the safety behaviour (CAPTCHA stop, unexpected-structure safe abort, uncertain
tracking, required-field validation, screenshot, never-submit boundary) lives on the
base class, along with the custom-question discovery (driven by the schema's
``question_container_selector`` through the real Playwright element API); this module
only provides the Greenhouse selectors. Playwright is opened lazily via
:mod:`jobpilot.forms.runtime`, so importing this module needs no browser.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from jobpilot.forms.base import (
    FormFiller,
    FormSchema,
    Platform,
    RequiredField,
)
from jobpilot.forms.runtime import open_form_page

if TYPE_CHECKING:
    from jobpilot.config import FormsConfig
    from jobpilot.interfaces import FillContext, FillResult, LlmClient
    from jobpilot.models import JobAnalysis
    from jobpilot.profile.model import ProfileModel

_GREENHOUSE_SCHEMA = FormSchema(
    field_map={
        "#first_name": "first_name",
        "#last_name": "last_name",
        "#email": "email",
        "#phone": "phone",
        "#job_application_location": "location",
        "#job_application_answers_attributes_linkedin": "linkedin",
    },
    cv_selector="input#resume",
    cover_letter_selector="input#cover_letter",
    required_markers=("#application_form", "#first_name", "#last_name", "#email"),
    captcha_markers=(
        "iframe[src*='recaptcha']",
        ".g-recaptcha",
        "iframe[src*='hcaptcha']",
        ".h-captcha",
        "#px-captcha",
        "iframe[src*='turnstile']",
        ".cf-turnstile",
        "iframe[src*='arkoselabs']",
        "iframe[title*='captcha' i]",
    ),
    submit_selector="#submit_app",
    # Greenhouse shows a confirmation ("application submitted") after a successful
    # submit; at least one must be present post-click for the submit to be recorded.
    success_markers=("#application_confirmation", ".application--confirmation"),
    # A validation summary indicates the submission was rejected (nothing accepted).
    post_submit_error_markers=("#error_box", ".field_with_errors"),
    # Greenhouse renders each custom question inside a ``.field`` wrapper whose label
    # is the visible question text and whose control is a ``textarea``/``input``.
    question_container_selector=".field.custom-question",
    required_fields=(
        RequiredField(selector="#first_name", label="Nombre"),
        RequiredField(selector="#last_name", label="Apellido"),
        RequiredField(selector="#email", label="Correo electrónico"),
    ),
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

    def fill(self, url: str, ctx: FillContext) -> FillResult:
        """Open the Greenhouse form at ``url`` and fill it, never submitting.

        On a CAPTCHA or an unexpected DOM the run aborts safely: a screenshot is
        saved and a :class:`~jobpilot.models.FillResult` with ``aborted=True`` is
        returned so the caller notifies the owner and submits nothing.
        """
        with open_form_page(url, self._config) as page:
            return self.fill_with_safe_abort(page, ctx)
