"""The local assisted-form entrypoint (P8-012), Playwright + Telegram MOCKED.

Proves the assisted flow is reachable for an approved Greenhouse/Lever job, loads the
approved artifacts, passes the required document metadata, and refuses to proceed for
a non-approved job, an unsupported URL, or missing approved documents. No browser is
launched and no Telegram call is made.
"""

from __future__ import annotations

import base64
import json
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, contextmanager
from typing import TypeVar

import pytest
from pydantic import BaseModel

from jobpilot.config import AppConfig, Language
from jobpilot.forms.apply import FormApplyStatus
from jobpilot.forms.base import FormPage
from jobpilot.forms.submit import SubmitAction
from jobpilot.interfaces import Update
from jobpilot.llm.schemas import FormAnswer
from jobpilot.models import ApplicationChannel, Job, JobStatus
from jobpilot.pipeline.run import PROPOSAL_ARTIFACTS_PREFIX
from jobpilot.profile.model import ProfileModel
from tests.fakes import FakeFormPage, FakeJobRepository, FakeNotifier

SchemaT = TypeVar("SchemaT", bound=BaseModel)

_GH_PRESENT = {
    "#application_form",
    "#first_name",
    "#last_name",
    "#email",
    "input#resume",
    "input#cover_letter",
    "#submit_app",
}
_NONCE = "localnonce"


class _StubLlm:
    def complete_json(self, prompt: str, schema: type[SchemaT], *, max_retries: int = 1) -> SchemaT:
        answer = FormAnswer(answer="n/a", confident=True)
        assert isinstance(answer, schema)
        return answer


def _job() -> Job:
    return Job(
        id=42,
        source="greenhouse",
        external_id="e42",
        url="https://boards.greenhouse.io/acme/jobs/42",
        apply_url="https://boards.greenhouse.io/acme/jobs/42",
        title="Backend Engineer",
        company="ACME Corp",
        language=Language.en,
        status=JobStatus.approved,
    )


def _seed_artifacts(repo: FakeJobRepository, job_id: int) -> None:
    """Store approved proposal artifacts (metadata + PDF bytes) for ``job_id``."""
    payload = {
        "language": Language.en.value,
        "cv_variant": "backend_en",
        "cv_url": "https://drive.example/cv_en.pdf",
        "cover_letter_url": "https://drive.example/cover_en.pdf",
        "cv": {
            "filename": "cv_en.pdf",
            "mime_type": "application/pdf",
            "content_b64": base64.b64encode(b"cv-bytes").decode("ascii"),
        },
        "cover_letter": {
            "filename": "cover_en.pdf",
            "mime_type": "application/pdf",
            "content_b64": base64.b64encode(b"cover-bytes").decode("ascii"),
        },
        "cover_letter_body": "Dear team, I am excited to apply.",
    }
    repo.set_state(PROPOSAL_ARTIFACTS_PREFIX + str(job_id), json.dumps(payload))


def _page_factory(
    page: FakeFormPage,
) -> Callable[[str, AppConfig], AbstractContextManager[FormPage]]:
    @contextmanager
    def factory(url: str, config: AppConfig) -> Iterator[FormPage]:
        yield page

    return factory


def _run(
    repo: FakeJobRepository,
    notifier: FakeNotifier,
    profile: ProfileModel,
    page: FakeFormPage,
    monkeypatch: pytest.MonkeyPatch,
) -> object:
    """Invoke the local entrypoint with the page factory and nonce injected."""
    import jobpilot.forms.apply as apply_mod
    import jobpilot.forms.local as local_mod

    original = apply_mod.apply_via_form

    def patched(*args: object, **kwargs: object) -> object:
        kwargs.setdefault("page_factory", _page_factory(page))
        kwargs.setdefault("confirmation_nonce", _NONCE)
        return original(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(local_mod, "apply_via_form", patched)
    return local_mod.apply_to_approved_job_via_form(
        42, repo, notifier, profile, _StubLlm(), AppConfig()
    )


def test_local_form_flow_reaches_submit_for_approved_greenhouse_job(
    example_profile: ProfileModel, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = FakeJobRepository()
    repo.seed_job(_job())
    _seed_artifacts(repo, 42)
    notifier = FakeNotifier(
        updates_after_send=[
            Update(
                update_id=1,
                chat_id="owner",
                callback_data=f"{SubmitAction.submit.value}:42:{_NONCE}",
            )
        ]
    )
    page = FakeFormPage(present=_GH_PRESENT, present_after_click={"#application_confirmation"})

    result = _run(repo, notifier, example_profile, page, monkeypatch)

    assert result.status is FormApplyStatus.submitted  # type: ignore[attr-defined]
    assert page.clicked == ["#submit_app"]
    assert len(repo.applications) == 1
    app = repo.applications[0]
    assert app.channel is ApplicationChannel.form_assisted
    # R12.6: the recorded application carries the CV variant and both document URLs.
    assert app.cv_variant == "backend_en"
    assert str(app.cv_url) == "https://drive.example/cv_en.pdf"
    assert str(app.cover_letter_url) == "https://drive.example/cover_en.pdf"


def test_local_form_flow_rejects_non_approved_job(
    example_profile: ProfileModel, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = FakeJobRepository()
    repo.seed_job(_job().model_copy(update={"status": JobStatus.proposed}))
    _seed_artifacts(repo, 42)
    notifier = FakeNotifier()
    page = FakeFormPage(present=_GH_PRESENT)

    result = _run(repo, notifier, example_profile, page, monkeypatch)

    assert result.status is not FormApplyStatus.submitted  # type: ignore[attr-defined]
    assert page.clicked == []
    assert repo.applications == []


def test_local_form_flow_rejects_unsupported_url(
    example_profile: ProfileModel, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = FakeJobRepository()
    repo.seed_job(_job().model_copy(update={"apply_url": "https://www.linkedin.com/jobs/view/42"}))
    _seed_artifacts(repo, 42)
    notifier = FakeNotifier()
    page = FakeFormPage(present=_GH_PRESENT)

    result = _run(repo, notifier, example_profile, page, monkeypatch)

    assert result.status is FormApplyStatus.unsupported  # type: ignore[attr-defined]
    assert repo.applications == []


def test_local_form_flow_requires_approved_documents(
    example_profile: ProfileModel, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = FakeJobRepository()
    repo.seed_job(_job())
    # No artifacts stored: the approved documents are missing, so the flow refuses.
    notifier = FakeNotifier()
    page = FakeFormPage(present=_GH_PRESENT)

    result = _run(repo, notifier, example_profile, page, monkeypatch)

    assert result.status is FormApplyStatus.unsupported  # type: ignore[attr-defined]
    assert repo.applications == []
