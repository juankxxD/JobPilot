"""Fakes implementing the project's Protocols, for use in tests."""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import TypeVar

from pydantic import BaseModel

from jobpilot.channels.telegram import TelegramDeliveryError
from jobpilot.interfaces import Button, Update, UpdateBatch
from jobpilot.models import Application, Attachment, Job, JobStatus, RawJob, StoredFile

SchemaT = TypeVar("SchemaT", bound=BaseModel)


class FakeLlmClient:
    """An LlmClient that returns a pre-built schema instance and records prompts."""

    def __init__(self, response: BaseModel) -> None:
        self._response = response
        self.prompts: list[str] = []

    def complete_json(self, prompt: str, schema: type[SchemaT], *, max_retries: int = 1) -> SchemaT:
        self.prompts.append(prompt)
        if not isinstance(self._response, schema):
            raise AssertionError(
                f"FakeLlmClient was asked for {schema.__name__} but holds "
                f"{type(self._response).__name__}"
            )
        return self._response


class ScriptedFakeLlmClient:
    """An LlmClient that returns queued responses matched by schema type.

    Several Phase 3+ flows need multiple sequential LLM outputs (analyse, then
    score, then confirm-language). Enqueue responses with :meth:`queue`; each
    ``complete_json`` call pops the next response whose type matches the requested
    schema. Every prompt is recorded and asserted to embed the anti-invention
    rules, so a prompt built without them fails loudly in tests.
    """

    def __init__(self, responses: Iterable[BaseModel] = ()) -> None:
        self._responses: list[BaseModel] = list(responses)
        self.prompts: list[str] = []

    def queue(self, *responses: BaseModel) -> None:
        self._responses.extend(responses)

    def complete_json(self, prompt: str, schema: type[SchemaT], *, max_retries: int = 1) -> SchemaT:
        assert "STRICT RULES" in prompt, "prompt is missing the anti-invention block"
        self.prompts.append(prompt)
        for index, response in enumerate(self._responses):
            if isinstance(response, schema):
                del self._responses[index]
                return response
        raise AssertionError(
            f"ScriptedFakeLlmClient has no queued {schema.__name__} response; "
            f"queued: {[type(r).__name__ for r in self._responses]}"
        )


class FakeJobRepository:
    """In-memory JobRepository that enforces the idempotency constraints.

    Deduplicates jobs by ``(source, external_id)`` and by ``url`` (the two unique
    constraints from ``001_init.sql``) and records status changes and key/value
    state so tests can assert on them.
    """

    def __init__(self) -> None:
        self._jobs: dict[int, Job] = {}
        self._by_source_external: dict[tuple[str, str], int] = {}
        self._by_url: dict[str, int] = {}
        self._applications: list[Application] = []
        self._state: dict[str, str] = {}
        self._email_send_claims: dict[int, str] = {}
        self._next_job_id = 1
        self._next_app_id = 1
        self.upsert_calls = 0
        self.fail_finalize = False
        self.status_changes: list[tuple[int, JobStatus]] = []

    def upsert_job(self, job: RawJob) -> Job:
        self.upsert_calls += 1
        key = (job.source, job.external_id)
        existing_id = self._by_source_external.get(key)
        if existing_id is None:
            existing_id = self._by_url.get(job.url)
        if existing_id is not None:
            current = self._jobs[existing_id]
            updated = current.model_copy(
                update={
                    "url": job.url,
                    "apply_url": job.apply_url,
                    "title": job.title,
                    "company": job.company,
                    "location": job.location,
                    "remote": job.remote,
                    "modality": job.modality,
                    "salary_text": job.salary_text,
                    # Prefer a freshly supplied source email, but preserve a stored
                    # (e.g. analysis-derived) address when recollection yields None
                    # (mirrors COALESCE(EXCLUDED.contact_email, jobs.contact_email)).
                    "contact_email": job.contact_email
                    if job.contact_email is not None
                    else current.contact_email,
                    # Only fill language while still unknown so a language established
                    # later by analysis is never overwritten (mirrors the COALESCE in
                    # NeonJobRepository.upsert_job).
                    "language": current.language if current.language is not None else job.language,
                    "description": job.description,
                }
            )
            self._jobs[existing_id] = updated
            self._by_source_external[key] = existing_id
            self._by_url[job.url] = existing_id
            return updated

        job_id = self._next_job_id
        self._next_job_id += 1
        stored = Job(
            id=job_id,
            source=job.source,
            external_id=job.external_id,
            url=job.url,
            apply_url=job.apply_url,
            title=job.title,
            company=job.company,
            location=job.location,
            remote=job.remote,
            modality=job.modality,
            salary_text=job.salary_text,
            contact_email=job.contact_email,
            language=job.language,
            description=job.description,
            found_at=job.found_at,
            status=JobStatus.new,
        )
        self._jobs[job_id] = stored
        self._by_source_external[key] = job_id
        self._by_url[job.url] = job_id
        return stored

    def get_job(self, job_id: int) -> Job | None:
        return self._jobs.get(job_id)

    def set_status(self, job_id: int, status: JobStatus) -> None:
        job = self._jobs[job_id]
        self._jobs[job_id] = job.model_copy(
            update={"status": status, "status_changed_at": self._now()}
        )
        self.status_changes.append((job_id, status))

    def save_job(self, job: Job) -> Job:
        if job.id is None:
            raise ValueError("save_job requires a job with an id")
        # Mirror the concrete repository, which stamps status_changed_at on every
        # save_job, so the dedupe window measures from the proposal time.
        self._jobs[job.id] = job.model_copy(update={"status_changed_at": self._now()})
        return self._jobs[job.id]

    def _now(self) -> datetime:
        return datetime.now(UTC)

    def create_application(self, app: Application) -> Application:
        app_id = self._next_app_id
        self._next_app_id += 1
        stored = app.model_copy(update={"id": app_id})
        self._applications.append(stored)
        return stored

    # Email-send idempotency (mirrors NeonJobRepository, migration 002) --- #
    def claim_email_send(self, job_id: int) -> bool:
        """Create a unique in-progress email-send claim; False if one exists."""
        if job_id in self._email_send_claims:
            return False
        self._email_send_claims[job_id] = "in_progress"
        return True

    def release_email_send(self, job_id: int) -> None:
        """Drop an in-progress claim (provider rejected; safe to retry)."""
        if self._email_send_claims.get(job_id) == "in_progress":
            del self._email_send_claims[job_id]

    def finalize_email_send(self, job_id: int, app: Application) -> Application:
        """Atomically record the application, mark applied and close the claim.

        ``fail_finalize`` lets a test simulate a database failure AFTER the provider
        accepted the message: nothing is recorded, the job stays proposed, and the
        claim is left ``in_progress`` so a later Enviar cannot resend.
        """
        if self.fail_finalize:
            raise RuntimeError("simulated database failure after provider acceptance")
        stored = self.create_application(app)
        self.set_status(job_id, JobStatus.applied)
        self._email_send_claims[job_id] = "done"
        return stored

    def email_claim_state(self, job_id: int) -> str | None:
        """Return the stored email-send claim state for ``job_id`` (test helper)."""
        return self._email_send_claims.get(job_id)

    def get_state(self, key: str) -> str | None:
        return self._state.get(key)

    def set_state(self, key: str, value: str) -> None:
        self._state[key] = value

    # Proposal guards (mirror NeonJobRepository helpers, assumption A3) -- #
    def count_applications_on(self, day: date) -> int:
        """Count applications whose ``applied_at`` falls on ``day``."""
        return sum(
            1
            for app in self._applications
            if app.applied_at is not None and app.applied_at.date() == day
        )

    def recent_company_role(self, company: str, role: str, *, within_days: int) -> bool:
        """Return whether a job for the same company+role was recently proposed.

        Mirrors the concrete repository: a proposed/approved/applied job for the
        same company and role counts only when its last status transition
        (``status_changed_at``) falls within ``within_days``. A job whose proposal
        is older than the window no longer blocks a new identical proposal.
        """
        seen = {JobStatus.proposed, JobStatus.approved, JobStatus.applied}
        cutoff = self._now() - timedelta(days=within_days)
        for job in self._jobs.values():
            if job.company != company or job.title != role or job.status not in seen:
                continue
            changed = job.status_changed_at
            if changed is None or changed >= cutoff:
                return True
        return False

    def list_proposed(self) -> list[Job]:
        """Return every job currently in the ``proposed`` status."""
        return self.list_by_status(JobStatus.proposed)

    def list_by_status(self, status: JobStatus) -> list[Job]:
        """Return every job currently in ``status`` (ordered by id)."""
        return [
            job
            for job in sorted(self._jobs.values(), key=lambda j: j.id or 0)
            if job.status is status
        ]

    def set_status_changed_at(self, job_id: int, when: datetime) -> None:
        """Override a job's status-transition timestamp (test helper for dedupe)."""
        self._jobs[job_id] = self._jobs[job_id].model_copy(update={"status_changed_at": when})

    def seed_job(self, job: Job) -> Job:
        """Insert a fully-formed ``Job`` directly (test helper for later phases).

        Phases after collection start from jobs already scored/proposed; this stores
        such a job and registers its dedupe keys without going through ``upsert_job``.
        """
        if job.id is None:
            job = job.model_copy(update={"id": self._next_job_id})
        assert job.id is not None
        self._jobs[job.id] = job
        self._by_source_external[(job.source, job.external_id)] = job.id
        self._by_url[job.url] = job.id
        self._next_job_id = max(self._next_job_id, job.id + 1)
        return job

    # Test conveniences ------------------------------------------------- #
    @property
    def jobs(self) -> list[Job]:
        return list(self._jobs.values())

    @property
    def applications(self) -> list[Application]:
        return list(self._applications)


class FakeJobSource:
    """A JobSource that yields canned RawJobs or raises a configured error."""

    def __init__(
        self,
        name: str,
        jobs: Iterable[RawJob] = (),
        *,
        enabled: bool = True,
        error: Exception | None = None,
    ) -> None:
        self.name = name
        self.enabled = enabled
        self._jobs = list(jobs)
        self._error = error
        self.fetch_calls = 0

    def fetch(self) -> Iterable[RawJob]:
        self.fetch_calls += 1
        if self._error is not None:
            raise self._error
        return list(self._jobs)


class FakeFileStorage:
    """A FileStorage that records uploads and returns a canned StoredFile."""

    def __init__(self, *, base_url: str = "https://drive.example.com/file") -> None:
        self._base_url = base_url
        self.uploads: list[tuple[Path, str]] = []
        self._counter = 0

    def upload(self, path: Path, name: str) -> StoredFile:
        self._counter += 1
        self.uploads.append((path, name))
        file_id = f"file_{self._counter}"
        return StoredFile(
            file_id=file_id,
            name=name,
            web_view_link=f"{self._base_url}/{file_id}",
        )


class SentEmail(BaseModel):
    """A record of one :meth:`FakeMailer.send` call."""

    to: str
    subject: str
    body: str
    attachments: list[str] = []
    attachment_bytes: list[bytes] = []


class FakeMailer:
    """A Mailer that records sends and can simulate a provider rejection.

    Records each ``send`` (recipient, subject, body and attachment filenames) so
    tests can assert the email path was taken with the right documents. Set
    ``fail`` to have the next ``send`` raise, exercising the retry-safe path where a
    failed send must not record an application. Set ``record_bytes`` to also capture
    the raw attachment bytes, so a test can assert byte identity with the approved
    proposal documents.
    """

    def __init__(self, *, fail: bool = False, record_bytes: bool = False) -> None:
        self.sent: list[SentEmail] = []
        self._fail = fail
        self._record_bytes = record_bytes

    def send(
        self,
        to: str,
        subject: str,
        body: str,
        attachments: Sequence[Attachment],
    ) -> None:
        if self._fail:
            raise RuntimeError("simulated mail provider rejection")
        self.sent.append(
            SentEmail(
                to=to,
                subject=subject,
                body=body,
                attachments=[a.filename for a in attachments],
                attachment_bytes=[a.content for a in attachments] if self._record_bytes else [],
            )
        )


class SentMessage(BaseModel):
    """A record of one :meth:`FakeNotifier.send_message` call."""

    text: str
    buttons: list[Button] = []
    attachments: list[str] = []


class FakeNotifier:
    """A Notifier that records sent messages and returns scripted updates.

    Records each ``send_message`` (text, buttons and attachment filenames) so tests
    can assert on the Spanish copy and the inline keyboard, and replays a queued
    list of :class:`Update` objects, honouring the ``offset`` so already-acknowledged
    updates are not returned twice (mirroring Telegram's ``getUpdates`` contract).

    ``highest_update_id`` is reported over every queued update (including ones a
    real notifier would drop for not belonging to the owner), so the pipeline can
    checkpoint an all-ignored batch. ``fail_send_after`` lets a test simulate the
    Telegram Bot API rejecting a send: once that many ``send_message`` calls have
    succeeded, the next raises :class:`TelegramDeliveryError`.
    """

    def __init__(
        self,
        updates: Iterable[Update] = (),
        *,
        highest_update_id: int | None = None,
        fail_send_after: int | None = None,
    ) -> None:
        self.messages: list[SentMessage] = []
        self._updates: list[Update] = list(updates)
        self._highest_update_id = highest_update_id
        self._fail_send_after = fail_send_after
        self.get_updates_calls: list[int | None] = []

    def queue_updates(self, *updates: Update) -> None:
        self._updates.extend(updates)

    def send_message(
        self,
        text: str,
        buttons: Sequence[Button] | None = None,
        attachments: Sequence[Attachment] | None = None,
    ) -> None:
        if self._fail_send_after is not None and len(self.messages) >= self._fail_send_after:
            raise TelegramDeliveryError("simulated Telegram Bot API rejection")
        self.messages.append(
            SentMessage(
                text=text,
                buttons=list(buttons or ()),
                attachments=[a.filename for a in (attachments or ())],
            )
        )

    def _visible(self, offset: int | None) -> list[Update]:
        if offset is None:
            return list(self._updates)
        return [u for u in self._updates if u.update_id >= offset]

    def _highest(self, offset: int | None) -> int | None:
        if self._highest_update_id is not None:
            return self._highest_update_id
        visible = self._visible(offset)
        return max((u.update_id for u in visible), default=None)

    def get_updates(self, offset: int | None = None) -> list[Update]:
        self.get_updates_calls.append(offset)
        return self._visible(offset)

    def get_update_batch(self, offset: int | None = None) -> UpdateBatch:
        self.get_updates_calls.append(offset)
        return UpdateBatch(updates=self._visible(offset), highest_update_id=self._highest(offset))

    # Test conveniences ------------------------------------------------- #
    @property
    def callback_values(self) -> list[str]:
        """Every ``callback_data`` across all buttons sent, in order."""
        return [b.callback_data for msg in self.messages for b in msg.buttons]

    def last(self) -> SentMessage:
        return self.messages[-1]
