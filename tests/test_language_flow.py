"""Ambiguous-language prompt and Ajustar regeneration flows."""

from __future__ import annotations

from jobpilot.channels.telegram import CallbackAction, encode_callback
from jobpilot.config import AppConfig, Language, Settings
from jobpilot.interfaces import Update
from jobpilot.llm.schemas import CoverLetterDraft
from jobpilot.models import ContentSelection, Job, JobAnalysis, JobStatus
from jobpilot.pipeline.run import (
    PENDING_ADJUST_KEY,
    process_telegram_updates,
    propose_scored_jobs,
)
from jobpilot.profile.model import ProfileModel
from tests.conftest import skip_without_weasyprint
from tests.fakes import FakeFileStorage, FakeJobRepository, FakeNotifier, ScriptedFakeLlmClient


def _scored_job(*, language: Language | None, job_id: int = 1) -> Job:
    return Job(
        id=job_id,
        source="remoteok",
        external_id=f"rok-{job_id}",
        url=f"https://example.com/job/{job_id}",
        title="Backend Engineer",
        company="ACME Corp",
        language=language,
        score=90,
        status=JobStatus.scored,
        analysis=JobAnalysis(normalized_title="Backend Engineer"),
    )


def _selection(language: Language) -> ContentSelection:
    return ContentSelection(
        summary_variant="backend_java",
        ordered_achievement_ids=["ach_acme_latency"],
        project_ids=[],
        highlighted_skill_ids=[],
        language=language,
    )


def _cover() -> CoverLetterDraft:
    return CoverLetterDraft(
        body="Me entusiasma postularme y aportar mi experiencia en Java a su equipo."
    )


def test_ambiguous_language_keeps_proposed_and_sends_three_buttons(
    example_profile: ProfileModel,
) -> None:
    repo = FakeJobRepository()
    stored = repo.seed_job(_scored_job(language=None))
    notifier = FakeNotifier()
    llm = ScriptedFakeLlmClient()
    storage = FakeFileStorage()

    summary = propose_scored_jobs(
        [stored],
        repo,
        notifier,
        example_profile,
        llm,
        storage,
        AppConfig(),
        Settings(daily_application_limit=5),
    )

    assert summary.language_prompts == 1
    job = repo.get_job(1)
    assert job is not None
    assert job.status is JobStatus.proposed
    # No documents were generated (no uploads) while the language is undecided.
    assert storage.uploads == []
    labels = [b.text for b in notifier.last().buttons]
    assert labels == ["Español", "Inglés", "Descartar"]


@skip_without_weasyprint
def test_choosing_language_advances_and_generates_documents(
    example_profile: ProfileModel,
) -> None:
    repo = FakeJobRepository()
    repo.seed_job(_scored_job(language=None))
    repo.set_status(1, JobStatus.proposed)
    notifier = FakeNotifier(
        [Update(update_id=5, chat_id="1", callback_data=encode_callback(CallbackAction.lang_es, 1))]
    )
    llm = ScriptedFakeLlmClient([_selection(Language.es), _cover()])
    storage = FakeFileStorage()

    process_telegram_updates(repo, notifier, example_profile, llm, storage, AppConfig())

    job = repo.get_job(1)
    assert job is not None
    assert job.language is Language.es
    assert len(storage.uploads) == 2
    # A full proposal (six buttons) is sent after the language choice.
    assert len(notifier.last().buttons) == 6


@skip_without_weasyprint
def test_adjust_captures_instruction_then_regenerates(example_profile: ProfileModel) -> None:
    repo = FakeJobRepository()
    repo.seed_job(_scored_job(language=Language.es))
    repo.set_status(1, JobStatus.proposed)
    notifier = FakeNotifier(
        [
            Update(
                update_id=7,
                chat_id="1",
                callback_data=encode_callback(CallbackAction.adjust, 1),
            ),
            Update(update_id=8, chat_id="1", text="usa un tono más formal"),
        ]
    )
    llm = ScriptedFakeLlmClient([_selection(Language.es), _cover()])
    storage = FakeFileStorage()

    process_telegram_updates(repo, notifier, example_profile, llm, storage, AppConfig())

    # The Ajustar button first asks for the instruction, then the free text triggers
    # a regeneration (two uploads) and the pending-adjust marker is cleared.
    assert len(storage.uploads) == 2
    assert repo.get_state(PENDING_ADJUST_KEY) == ""
    adjusted = repo.get_job(1)
    assert adjusted is not None
    assert adjusted.language is Language.es
