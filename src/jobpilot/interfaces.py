"""Structural interfaces (Protocols) that decouple the pipeline from concrete
implementations. Fakes implementing these Protocols are used in tests.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import Protocol, TypeVar, runtime_checkable

from pydantic import BaseModel

from jobpilot.models import (
    Application,
    Attachment,
    Job,
    JobStatus,
    RawJob,
    StoredFile,
)

SchemaT = TypeVar("SchemaT", bound=BaseModel)


@runtime_checkable
class LlmClient(Protocol):
    """A client that returns structured JSON validated against a Pydantic schema."""

    def complete_json(
        self, prompt: str, schema: type[SchemaT], *, max_retries: int = 1
    ) -> SchemaT: ...


@runtime_checkable
class JobSource(Protocol):
    """A source of job offers (API, RSS, email alerts, ...)."""

    name: str
    enabled: bool

    def fetch(self) -> Iterable[RawJob]: ...


@runtime_checkable
class FileStorage(Protocol):
    """Blob storage for generated documents (Drive today, R2 tomorrow)."""

    def upload(self, path: Path, name: str) -> StoredFile: ...


class Button(BaseModel):
    text: str
    callback_data: str


class Update(BaseModel):
    """A minimal normalized Telegram update."""

    update_id: int
    chat_id: str | None = None
    text: str | None = None
    callback_data: str | None = None


class UpdateBatch(BaseModel):
    """A fetched batch of Telegram updates plus the highest raw ``update_id`` seen.

    ``updates`` only contains the owner's updates (everything else is refused), but
    ``highest_update_id`` reflects every update returned by Telegram, including the
    ignored non-owner ones. The pipeline checkpoints ``highest_update_id`` so a
    batch made entirely of unauthorized messages still advances the cursor and can
    never starve a later owner update.
    """

    updates: list[Update]
    highest_update_id: int | None = None


@runtime_checkable
class Notifier(Protocol):
    """The Telegram-facing channel."""

    def send_message(
        self,
        text: str,
        buttons: Sequence[Button] | None = None,
        attachments: Sequence[Attachment] | None = None,
    ) -> None: ...

    def get_updates(self, offset: int | None = None) -> list[Update]: ...

    def get_update_batch(self, offset: int | None = None) -> UpdateBatch: ...


@runtime_checkable
class Mailer(Protocol):
    """Outbound email with attachments."""

    def send(
        self,
        to: str,
        subject: str,
        body: str,
        attachments: Sequence[Attachment],
    ) -> None: ...


@runtime_checkable
class JobRepository(Protocol):
    """Persistence for jobs, applications and key/value state."""

    def upsert_job(self, job: RawJob) -> Job: ...

    def get_job(self, job_id: int) -> Job | None: ...

    def set_status(self, job_id: int, status: JobStatus) -> None: ...

    def save_job(self, job: Job) -> Job: ...

    def create_application(self, app: Application) -> Application: ...

    def get_state(self, key: str) -> str | None: ...

    def set_state(self, key: str, value: str) -> None: ...


class FillContext(BaseModel):
    """Everything a FormFiller needs: the job and the chosen documents."""

    job_id: int
    language: str
    cv_path: str
    cover_letter_path: str


class FillResult(BaseModel):
    """Outcome of filling (never submitting) a form."""

    filled_fields: list[str]
    uncertain_fields: list[str]
    unanswered_sensitive: list[str]
    screenshot_path: str | None = None
    aborted: bool = False
    abort_reason: str | None = None


@runtime_checkable
class FormFiller(Protocol):
    """Fills (but never submits) an application form on a supported platform."""

    def matches(self, url: str) -> bool: ...

    def fill(self, url: str, ctx: FillContext) -> FillResult: ...
