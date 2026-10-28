"""Lever assisted form filler (Playwright, LOCAL only).

Lever application forms expose named inputs (``input[name="name"]``,
``input[name="email"]``, ...), a resume upload input, and "additional information"
custom questions rendered as labelled text areas. This filler maps the known
profile fields, uploads the CV and cover letter in the chosen language, and routes
every custom question through the shared
:meth:`~jobpilot.forms.base.FormFiller.resolve_question`, so sensitive questions
never reach the LLM.

As with Greenhouse, all the safety behaviour (CAPTCHA stop, unexpected-structure
safe abort, uncertain tracking, required-field validation, screenshot, never-submit
boundary) and the custom-question discovery live on the base class; this module only
supplies the Lever selectors. Playwright is opened lazily via
:mod:`jobpilot.forms.runtime`.
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

_LEVER_SCHEMA = FormSchema(
    field_map={
        # Lever uses a single full-name control; the profile's full name belongs here.
        "input[name='name']": "name",
        "input[name='email']": "email",
        "input[name='phone']": "phone",
        "input[name='urls[LinkedIn]']": "linkedin",
        "input[name='urls[GitHub]']": "github",
        # NOTE: input[name='org'] is the candidate's *current company*, which the
        # profile does not expose as a single field, so it is left blank (never the
        # location) rather than filled with semantically wrong data.
    },
    cv_selector="input[name='resume']",
    cover_letter_selector="input[name='cover_letter']",
    required_markers=(".application-form", "input[name='name']", "input[name='email']"),
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
    submit_selector="button[data-qa='btn-submit']",
    # Lever renders each "additional information" question inside an
    # ``.application-question`` card whose label is the question text and whose
    # control is a ``textarea``/``input`` with a stable ``name``.
    question_container_selector=".application-question",
    required_fields=(
        RequiredField(selector="input[name='name']", label="Nombre completo"),
        RequiredField(selector="input[name='email']", label="Correo electrónico"),
    ),
    # Lever often has no dedicated cover-letter upload (its "additional information"
    # free-text field serves that role), so a missing cover-letter control must not
    # make an otherwise complete Lever form impossible to submit.
    cover_letter_required=False,
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

    def fill(self, url: str, ctx: FillContext) -> FillResult:
        """Open the Lever form at ``url`` and fill it, never submitting.

        On a CAPTCHA or an unexpected DOM the run aborts safely: a screenshot is
        saved and a :class:`~jobpilot.models.FillResult` with ``aborted=True`` is
        returned so the caller notifies the owner and submits nothing.
        """
        with open_form_page(url, self._config) as page:
            return self.fill_with_safe_abort(page, ctx)
