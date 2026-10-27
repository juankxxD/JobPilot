"""Adzuna job source (public JSON API). Optional, disabled by default.

https://developer.adzuna.com/ — requires ``ADZUNA_APP_ID`` and ``ADZUNA_APP_KEY``
credentials from settings. HTTP is injectable so tests parse a saved fixture
without any network call; credentials are never hardcoded.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from jobpilot.config import Settings, get_settings
from jobpilot.fetchers.base import HttpClient, JobSource, normalize, register
from jobpilot.logging import get_logger
from jobpilot.models import RawJob

log = get_logger(__name__)

SOURCE = "adzuna"
# Country is part of the path; default to the global remote board for "gb".
API_URL = "https://api.adzuna.com/v1/api/jobs/gb/search/1"


class AdzunaSource(JobSource):
    name = SOURCE

    def __init__(
        self,
        *,
        client: HttpClient | None = None,
        settings: Settings | None = None,
        enabled: bool = True,
    ) -> None:
        super().__init__(enabled=enabled)
        self._client = client
        self._settings = settings or get_settings()

    def fetch(self) -> Iterable[RawJob]:
        if not (self._settings.adzuna_app_id and self._settings.adzuna_app_key):
            log.warning("adzuna_missing_credentials", source=SOURCE)
            return []
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
            resp = client.get(
                API_URL,
                params={
                    "app_id": self._settings.adzuna_app_id,
                    "app_key": self._settings.adzuna_app_key,
                    "results_per_page": 50,
                    "what": "developer",
                },
            )
            resp.raise_for_status()
            data: dict[str, Any] = resp.json()
            return data
        finally:
            if self._client is None:
                client.close()

    def _parse(self, payload: dict[str, Any]) -> Iterable[RawJob]:
        for item in payload.get("results", []):
            try:
                yield self._parse_item(item)
            except (ValueError, KeyError, TypeError) as exc:
                log.warning("skip_malformed_item", source=SOURCE, error=str(exc))

    def _parse_item(self, item: dict[str, Any]) -> RawJob:
        company = item.get("company", {}).get("display_name")
        location = item.get("location", {}).get("display_name")
        salary = None
        if item.get("salary_min") or item.get("salary_max"):
            salary = f"{item.get('salary_min') or ''}-{item.get('salary_max') or ''}"
        return normalize(
            source=SOURCE,
            external_id=str(item.get("id", "")),
            url=item.get("redirect_url") or "",
            title=item.get("title") or "",
            company=company or "",
            location=location,
            remote=False,
            salary_text=salary,
            description=item.get("description") or "",
        )


register(SOURCE, AdzunaSource)
