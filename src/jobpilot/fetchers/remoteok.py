"""RemoteOK job source (public JSON API).

https://remoteok.com/api — the first element of the array is legal metadata and is
skipped. HTTP is injectable so tests parse a saved fixture without any network call.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from jobpilot.fetchers.base import HttpClient, JobSource, normalize, register
from jobpilot.logging import get_logger
from jobpilot.models import RawJob

log = get_logger(__name__)

SOURCE = "remoteok"
API_URL = "https://remoteok.com/api"


class RemoteOkSource(JobSource):
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

    def _get(self) -> list[dict[str, Any]]:
        import httpx

        client = self._client or httpx.Client(timeout=30)
        try:
            resp = client.get(API_URL, headers={"User-Agent": "jobpilot"})
            resp.raise_for_status()
            data: list[dict[str, Any]] = resp.json()
            return data
        finally:
            if self._client is None:
                client.close()

    def _parse(self, payload: list[dict[str, Any]]) -> Iterable[RawJob]:
        for item in payload:
            # The legal/metadata entry has no "id"/"position"; skip it.
            if "id" not in item and "position" not in item:
                continue
            try:
                yield self._parse_item(item)
            except (ValueError, KeyError, TypeError) as exc:
                log.warning("skip_malformed_item", source=SOURCE, error=str(exc))

    def _parse_item(self, item: dict[str, Any]) -> RawJob:
        salary = None
        if item.get("salary_min") or item.get("salary_max"):
            salary = f"{item.get('salary_min') or ''}-{item.get('salary_max') or ''} USD"
        return normalize(
            source=SOURCE,
            external_id=str(item.get("id", "")),
            url=item.get("url") or "",
            apply_url=item.get("apply_url"),
            title=item.get("position") or "",
            company=item.get("company") or "",
            location=item.get("location"),
            remote=True,
            salary_text=salary,
            description=item.get("description") or "",
        )


register(SOURCE, RemoteOkSource)
