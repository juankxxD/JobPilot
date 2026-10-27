"""Fetcher base class, normalization helper and source registry.

Every job source subclasses :class:`JobSource` and yields :class:`RawJob`
instances from :meth:`fetch`. A fetcher must isolate its own errors: the registry
driving collection expects ``fetch`` to never raise, so the failure of one source
cannot stop the pipeline.

Sources register themselves with :func:`register`; :func:`iter_enabled` builds only
the sources whose ``config.yaml`` flag is on.
"""

from __future__ import annotations

import hashlib
from abc import ABC, abstractmethod
from collections.abc import Iterable
from typing import TYPE_CHECKING, Any, Protocol

from jobpilot.config import AppConfig, Language
from jobpilot.logging import get_logger
from jobpilot.models import Modality, RawJob

if TYPE_CHECKING:
    from collections.abc import Callable

log = get_logger(__name__)


class HttpResponse(Protocol):
    """The subset of an HTTP response the fetchers use."""

    text: str

    def json(self) -> Any: ...

    def raise_for_status(self) -> None: ...


class HttpClient(Protocol):
    """The subset of ``httpx.Client`` the fetchers use (so fakes match it)."""

    def get(self, url: str, **kwargs: Any) -> HttpResponse: ...

    def close(self) -> None: ...


class JobSource(ABC):
    """Abstract base for a job source."""

    #: Stable source name; must match the key used in ``config.yaml`` ``sources``.
    name: str = ""

    def __init__(self, *, enabled: bool = True) -> None:
        self.enabled = enabled

    @abstractmethod
    def fetch(self) -> Iterable[RawJob]:
        """Yield normalized jobs. Implementations must not raise."""
        raise NotImplementedError


def _clean(value: str | None) -> str | None:
    if value is None:
        return None
    trimmed = value.strip()
    return trimmed or None


def _stable_external_id(source: str, external_id: str | None, url: str) -> str:
    """Return a stable external id, deriving one from the url when absent."""
    candidate = _clean(external_id)
    if candidate:
        return candidate
    digest = hashlib.sha1(f"{source}:{url}".encode()).hexdigest()[:16]
    return f"url-{digest}"


def normalize(
    *,
    source: str,
    external_id: str | None,
    url: str,
    title: str,
    company: str,
    apply_url: str | None = None,
    location: str | None = None,
    remote: bool = False,
    modality: Modality | None = None,
    salary_text: str | None = None,
    contact_email: str | None = None,
    language: Language | None = None,
    description: str = "",
) -> RawJob:
    """Build a validated :class:`RawJob` with trimmed fields and a stable id.

    Raises ``ValueError`` when a required field (url, title, company) is blank, so a
    malformed payload is rejected by the fetcher rather than persisted.

    ``modality`` and ``language`` carry whatever the source itself declares; both are
    optional (``None`` means "not declared"). When a source marks a job ``remote``
    but does not declare an explicit modality, the modality is inferred as
    ``Modality.remote`` so the prefilter can act on it.
    """
    clean_url = _clean(url)
    clean_title = _clean(title)
    clean_company = _clean(company)
    if not clean_url:
        raise ValueError("normalize: url is required")
    if not clean_title:
        raise ValueError("normalize: title is required")
    if not clean_company:
        raise ValueError("normalize: company is required")

    resolved_modality = modality
    if resolved_modality is None and remote:
        resolved_modality = Modality.remote

    return RawJob(
        source=source,
        external_id=_stable_external_id(source, external_id, clean_url),
        url=clean_url,
        apply_url=_clean(apply_url),
        title=clean_title,
        company=clean_company,
        location=_clean(location),
        remote=remote,
        modality=resolved_modality,
        salary_text=_clean(salary_text),
        contact_email=_clean(contact_email),
        language=language,
        description=(description or "").strip(),
    )


# --------------------------------------------------------------------------- #
# Registry
# --------------------------------------------------------------------------- #
_REGISTRY: dict[str, Callable[[], JobSource]] = {}


def register(name: str, factory: Callable[[], JobSource]) -> None:
    """Register a source factory under ``name`` (idempotent per name)."""
    _REGISTRY[name] = factory


def registered_sources() -> dict[str, Callable[[], JobSource]]:
    """Return a copy of the registry (name -> factory)."""
    return dict(_REGISTRY)


def iter_enabled(config: AppConfig) -> list[JobSource]:
    """Instantiate every registered source whose ``config.yaml`` flag is enabled."""
    _load_builtin_sources()
    sources: list[JobSource] = []
    for name, factory in _REGISTRY.items():
        if not config.source_enabled(name):
            continue
        try:
            sources.append(factory())
        except Exception as exc:  # noqa: BLE001 - a bad source must not break others
            log.error("source_factory_failed", source=name, error=str(exc))
    return sources


def _load_builtin_sources() -> None:
    """Import the built-in fetcher modules so they self-register.

    Imported lazily to avoid a circular import at module load time.
    """
    from jobpilot.fetchers import (  # noqa: F401
        adzuna,
        getonboard,
        gmail_alerts,
        himalayas,
        remoteok,
        remotive,
        weworkremotely,
    )
