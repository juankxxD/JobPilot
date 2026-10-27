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
from typing import TYPE_CHECKING, Any, Protocol

from jobpilot.fetchers.base import JobSource, normalize, register
from jobpilot.logging import get_logger
from jobpilot.models import RawJob

if TYPE_CHECKING:
    from jobpilot.config import Settings

log = get_logger(__name__)

SOURCE = "gmail_alerts"
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

    def fetch(self) -> Iterable[RawJob]:
        service = self._service
        if service is None:
            log.warning("gmail_alerts_no_service", source=SOURCE)
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


register(SOURCE, GmailAlertsSource)
