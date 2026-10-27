"""GetOnBoard job source (public JSON API).

https://www.getonbrd.com/api — returns remote-friendly tech jobs, mostly LATAM.
HTTP is injectable so tests parse a saved fixture without any network call.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from jobpilot.fetchers.base import HttpClient, JobSource, normalize, register
from jobpilot.logging import get_logger
from jobpilot.models import Modality, RawJob

log = get_logger(__name__)

SOURCE = "getonboard"
API_URL = "https://www.getonbrd.com/api/v0/search/jobs"

# GetOnBoard declares modality with its own vocabulary; map it to our enum.
_MODALITY_MAP = {
    "fully_remote": Modality.remote,
    "remote": Modality.remote,
    "hybrid": Modality.hybrid,
    "no_remote": Modality.onsite,
    "on_site": Modality.onsite,
    "onsite": Modality.onsite,
}


class GetOnBoardSource(JobSource):
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
            resp = client.get(API_URL, params={"per_page": 50, "expand": "company"})
            resp.raise_for_status()
            data: dict[str, Any] = resp.json()
            return data
        finally:
            if self._client is None:
                client.close()

    def _parse(self, payload: dict[str, Any]) -> Iterable[RawJob]:
        for item in payload.get("data", []):
            try:
                yield self._parse_item(item)
            except (ValueError, KeyError, TypeError) as exc:
                log.warning("skip_malformed_item", source=SOURCE, error=str(exc))

    def _parse_item(self, item: dict[str, Any]) -> RawJob:
        attrs: dict[str, Any] = item.get("attributes", {})
        company = attrs.get("company", {})
        company_name = company.get("data", {}).get("attributes", {}).get("name")
        if not company_name:
            company_name = attrs.get("company_name")
        raw_modality = attrs.get("modality")
        modality = _MODALITY_MAP.get(str(raw_modality).lower()) if raw_modality else None
        if modality is None and bool(attrs.get("remote")):
            modality = Modality.remote
        remote = modality == Modality.remote
        salary = None
        if attrs.get("min_salary") or attrs.get("max_salary"):
            salary = f"{attrs.get('min_salary') or ''}-{attrs.get('max_salary') or ''} USD"
        return normalize(
            source=SOURCE,
            external_id=str(item.get("id", "")),
            url=attrs.get("public_url") or attrs.get("url") or "",
            apply_url=attrs.get("apply_url"),
            title=attrs.get("title") or "",
            company=company_name or "",
            location=attrs.get("country") or attrs.get("city"),
            remote=remote,
            modality=modality,
            salary_text=salary,
            description=attrs.get("description") or "",
        )


register(SOURCE, GetOnBoardSource)
