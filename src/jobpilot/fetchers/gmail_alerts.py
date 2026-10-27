"""Gmail job-alert source. Optional, disabled by default.

This source reads job-alert *emails* the owner already receives in Gmail (LinkedIn,
Indeed or Google Jobs alerts) and extracts the postings linked inside them. It
NEVER scrapes LinkedIn/Indeed and performs no browser automation — it only reads
email the owner subscribed to, via the Gmail API.

The Gmail client is injectable (any object exposing the subset of the Gmail API we
use) so tests pass a fake message list and never contact Google.
"""

from __future__ import annotations

import base64
import re
from collections.abc import Iterable
from typing import Any, Protocol

from jobpilot.config import Settings, get_settings
from jobpilot.fetchers.base import JobSource, normalize, register
from jobpilot.logging import get_logger
from jobpilot.models import RawJob

log = get_logger(__name__)

SOURCE = "gmail_alerts"
# Read-only access: the source only reads alert emails, never modifies the mailbox.
GMAIL_READONLY_SCOPE = "https://www.googleapis.com/auth/gmail.readonly"
# Only alert senders are read; nothing else in the mailbox is touched.
ALERT_QUERY = (
    "from:(jobalerts-noreply@linkedin.com OR alert@indeed.com OR "
    "jobs-noreply@google.com) newer_than:2d"
)

# Extract "<a href=...>Title</a>" links to job postings from the alert HTML.
_LINK_RE = re.compile(
    r'<a[^>]+href="(?P<url>https?://[^"]+)"[^>]*>(?P<title>[^<]+)</a>',
    re.IGNORECASE,
)


class GmailService(Protocol):
    """The narrow slice of the Gmail API client this source relies on."""

    def list_messages(self, query: str) -> list[dict[str, Any]]: ...

    def get_message(self, message_id: str) -> dict[str, Any]: ...


class GmailAlertsSource(JobSource):
    name = SOURCE

    def __init__(
        self,
        *,
        service: GmailService | None = None,
        settings: Settings | None = None,
        enabled: bool = True,
    ) -> None:
        super().__init__(enabled=enabled)
        self._service = service
        self._settings = settings

    def _resolve_service(self) -> GmailService | None:
        """Return the injected service, or lazily build one from settings.

        Tests inject a fake service; in production the registry builds the source
        with no service, so we construct a read-only Gmail adapter from the
        environment-backed OAuth credentials when they are present.
        """
        if self._service is not None:
            return self._service
        settings = self._settings or get_settings()
        if not (
            settings.gmail_client_id
            and settings.gmail_client_secret
            and settings.gmail_refresh_token
        ):
            log.warning("gmail_alerts_missing_credentials", source=SOURCE)
            return None
        self._service = _GmailApiService(settings)
        return self._service

    def fetch(self) -> Iterable[RawJob]:
        service = self._resolve_service()
        if service is None:
            return []
        try:
            jobs: list[RawJob] = []
            for meta in service.list_messages(ALERT_QUERY):
                message = service.get_message(str(meta.get("id", "")))
                jobs.extend(self._parse_message(message))
            return jobs
        except Exception as exc:  # noqa: BLE001 - isolate source failures
            log.error("fetch_failed", source=SOURCE, error=str(exc))
            return []

    def _parse_message(self, message: dict[str, Any]) -> Iterable[RawJob]:
        sender = _header(message, "From")
        html = _message_html(message)
        for match in _LINK_RE.finditer(html):
            url = match.group("url")
            if "/jobs/view" not in url and "/viewjob" not in url and "google.com/search" not in url:
                continue
            title = match.group("title").strip()
            if not title:
                continue
            try:
                yield normalize(
                    source=SOURCE,
                    external_id=url,
                    url=url,
                    title=title,
                    company=_sender_label(sender),
                    remote=False,
                    description=f"Imported from Gmail alert ({sender}).",
                )
            except ValueError as exc:
                log.warning("skip_malformed_item", source=SOURCE, error=str(exc))


def _header(message: dict[str, Any], name: str) -> str:
    headers = message.get("payload", {}).get("headers", [])
    for header in headers:
        if str(header.get("name", "")).lower() == name.lower():
            return str(header.get("value", ""))
    return ""


def _sender_label(sender: str) -> str:
    sender = sender.lower()
    if "linkedin" in sender:
        return "LinkedIn Alert"
    if "indeed" in sender:
        return "Indeed Alert"
    if "google" in sender:
        return "Google Jobs Alert"
    return "Job Alert"


def _message_html(message: dict[str, Any]) -> str:
    payload = message.get("payload", {})
    parts = payload.get("parts") or [payload]
    for part in parts:
        if part.get("mimeType") in (None, "text/html", "text/plain"):
            data = part.get("body", {}).get("data")
            if data:
                return base64.urlsafe_b64decode(data.encode()).decode("utf-8", "replace")
    return ""


class _GmailApiService:
    """Read-only Gmail adapter over ``google-api-python-client``.

    Implements the :class:`GmailService` Protocol. The underlying client is built
    lazily on first use from the stored OAuth refresh token with the minimum
    ``gmail.readonly`` scope, so importing this module never contacts Google and
    unit tests inject a fake service instead.
    """

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._client: Any = None

    def _resource(self) -> Any:
        if self._client is None:
            from google.oauth2.credentials import Credentials
            from googleapiclient.discovery import build

            creds = Credentials(
                token=None,
                refresh_token=self._settings.gmail_refresh_token,
                client_id=self._settings.gmail_client_id,
                client_secret=self._settings.gmail_client_secret,
                token_uri="https://oauth2.googleapis.com/token",
                scopes=[GMAIL_READONLY_SCOPE],
            )
            self._client = build("gmail", "v1", credentials=creds, cache_discovery=False)
        return self._client

    def list_messages(self, query: str) -> list[dict[str, Any]]:
        resource = self._resource()
        response = resource.users().messages().list(userId="me", q=query, maxResults=25).execute()
        messages: list[dict[str, Any]] = response.get("messages", [])
        return messages

    def get_message(self, message_id: str) -> dict[str, Any]:
        resource = self._resource()
        message: dict[str, Any] = (
            resource.users().messages().get(userId="me", id=message_id, format="full").execute()
        )
        return message


def build_gmail_alerts_source(settings: Settings | None = None) -> GmailAlertsSource:
    """Factory used by the registry: build the source with a lazy Gmail adapter.

    The service stays ``None`` at construction time; the adapter is built on first
    ``fetch`` from environment-backed credentials (see
    :meth:`GmailAlertsSource._resolve_service`). Tests bypass this factory and inject
    a fake service directly.
    """
    return GmailAlertsSource(settings=settings)


register(SOURCE, build_gmail_alerts_source)
