"""Fakes implementing the project's Protocols, for use in tests."""

from __future__ import annotations

from collections.abc import Iterable
from typing import TypeVar

from pydantic import BaseModel

from jobpilot.models import Application, Job, JobStatus, RawJob

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
        self._next_job_id = 1
        self._next_app_id = 1
        self.upsert_calls = 0
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
                    "contact_email": job.contact_email,
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
        self._jobs[job_id] = job.model_copy(update={"status": status})
        self.status_changes.append((job_id, status))

    def save_job(self, job: Job) -> Job:
        if job.id is None:
            raise ValueError("save_job requires a job with an id")
        self._jobs[job.id] = job
        return job

    def create_application(self, app: Application) -> Application:
        app_id = self._next_app_id
        self._next_app_id += 1
        stored = app.model_copy(update={"id": app_id})
        self._applications.append(stored)
        return stored

    def get_state(self, key: str) -> str | None:
        return self._state.get(key)

    def set_state(self, key: str, value: str) -> None:
        self._state[key] = value

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
