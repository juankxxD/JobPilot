"""LLM-driven selection of CV content, validated in code against the profile.

``select_content`` asks the LLM to pick which existing profile ids to include in a
tailored CV (never to write new content). The returned selection is then validated
in code with :func:`jobpilot.profile.validation.validate_selection`, which enforces
the non-negotiable rules:

* every achievement/project id must exist in the profile;
* the chosen ``summary_variant`` must exist;
* every referenced text must have an approved translation in the target language.

On a validation failure the selection step is retried once; if it still fails a
:class:`SelectionError` is raised so the caller can mark the job ``error``.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from jobpilot.llm.prompts import build_select_content_prompt
from jobpilot.logging import get_logger
from jobpilot.models import ContentSelection
from jobpilot.profile.validation import validate_selection

if TYPE_CHECKING:
    from jobpilot.config import Language
    from jobpilot.interfaces import LlmClient
    from jobpilot.models import JobAnalysis
    from jobpilot.profile.model import ProfileModel

log = get_logger(__name__)


class SelectionError(RuntimeError):
    """Raised when content selection cannot produce profile-valid output."""


def _selection_schema_json() -> str:
    return json.dumps(ContentSelection.model_json_schema())


def _build_prompt(
    profile: ProfileModel,
    analysis: JobAnalysis,
    language: Language,
) -> str:
    return build_select_content_prompt(
        profile_json=profile.model_dump_json(),
        summary_variants=sorted(profile.summary_variants),
        achievement_ids=sorted(profile.achievement_ids()),
        project_ids=sorted(profile.project_ids()),
        skills=profile.skills.all(),
        analysis_json=analysis.model_dump_json(),
        language=language.value,
        schema_json=_selection_schema_json(),
    )


def select_content(
    analysis: JobAnalysis,
    profile: ProfileModel,
    llm: LlmClient,
    language: Language,
) -> ContentSelection:
    """Select tailored CV content for ``language``, validating ids and translations.

    Retries the selection once if the LLM cites an unknown id, an unknown summary
    variant, or a text whose translation is not approved in ``language``; raises
    :class:`SelectionError` if it still fails after the retry.
    """
    prompt = _build_prompt(profile, analysis, language)
    last_message = ""
    attempts = 2
    for attempt in range(1, attempts + 1):
        selection = llm.complete_json(prompt, ContentSelection)
        # Pin the language to the requested one: validation (and rendering) are
        # always done against the language the documents will actually use.
        selection = selection.model_copy(update={"language": language})
        issues = validate_selection(
            profile,
            summary_variant=selection.summary_variant,
            achievement_ids=selection.ordered_achievement_ids,
            project_ids=selection.project_ids,
            language=language,
            highlighted_skill_ids=selection.highlighted_skill_ids,
        )
        if issues.is_empty():
            return selection
        last_message = issues.as_message()
        log.warning(
            "selection_invalid",
            attempt=attempt,
            issues=last_message,
            language=language.value,
        )
    raise SelectionError(f"Content selection invalid after retry: {last_message}")
