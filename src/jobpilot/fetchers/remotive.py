"""Remotive job source (public JSON API).

https://remotive.com/api/remote-jobs — HTTP is injectable so tests parse a saved
fixture without any network call.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from jobpilot.fetchers.base import HttpClient, JobSource, normalize, register
from jobpilot.logging import get_logger
from jobpilot.models import RawJob

log = get_logger(__name__)

SOURCE = "remotive"
API_URL = "https://remotive.com/api/remote-jobs"


class RemotiveSource(JobSource):
    name = SOURCE

    def __init__(self, *, client: HttpClient | None = None, enabled: bool = True) -> None:
        super().__init__(enabled=enabled)
        self._client = client

    def fetch(self) -> Iterable[RawJob]:
        try:
            payload = self._get()
        except Exception as exc:  # noqa: BLE001 - isolate source failures
            log.error("fetch_failed", source=SOURCE, error=str(exc))
            return []
        return list(self._parse(payload))

    def _get(self) -> dict[str, Any]:
        import httpx

        client = self._client or httpx.Client(timeout=30)
        try:
            resp = client.get(API_URL)
            resp.raise_for_status()
            data: dict[str, Any] = resp.json()
            return data
        finally:
            if self._client is None:
                client.close()

    def _parse(self, payload: dict[str, Any]) -> Iterable[RawJob]:
        for item in payload.get("jobs", []):
            try:
                yield self._parse_item(item)
            except (ValueError, KeyError, TypeError) as exc:
                log.warning("skip_malformed_item", source=SOURCE, error=str(exc))

    def _parse_item(self, item: dict[str, Any]) -> RawJob:
        return normalize(
            source=SOURCE,
            external_id=str(item.get("id", "")),
            url=item.get("url") or "",
            title=item.get("title") or "",
            company=item.get("company_name") or "",
            location=item.get("candidate_required_location"),
            remote=True,
            salary_text=item.get("salary") or None,
            description=item.get("description") or "",
        )


register(SOURCE, RemotiveSource)
