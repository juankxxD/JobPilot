"""Himalayas job source (public JSON API). Optional, disabled by default.

https://himalayas.app/jobs/api — HTTP is injectable so tests parse a saved fixture
without any network call.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from jobpilot.fetchers.base import HttpClient, JobSource, normalize, register
from jobpilot.logging import get_logger
from jobpilot.models import RawJob

log = get_logger(__name__)

SOURCE = "himalayas"
API_URL = "https://himalayas.app/jobs/api"


class HimalayasSource(JobSource):
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
        locations = item.get("locationRestrictions") or []
        location = ", ".join(str(loc) for loc in locations) if locations else None
        return normalize(
            source=SOURCE,
            external_id=str(item.get("guid") or item.get("id", "")),
            url=item.get("applicationLink") or item.get("url") or "",
            title=item.get("title") or "",
            company=item.get("companyName") or "",
            location=location,
            remote=True,
            description=item.get("description") or "",
        )


register(SOURCE, HimalayasSource)
