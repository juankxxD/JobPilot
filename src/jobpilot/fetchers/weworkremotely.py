"""We Work Remotely job source (RSS feed, parsed with feedparser).

https://weworkremotely.com/remote-jobs.rss — the feed text is injectable so tests
parse a saved fixture without any network call. WWR titles look like
``Company: Job Title``; we split on the first colon to separate the two.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from jobpilot.fetchers.base import HttpClient, JobSource, normalize, register
from jobpilot.logging import get_logger
from jobpilot.models import RawJob

log = get_logger(__name__)

SOURCE = "weworkremotely"
FEED_URL = "https://weworkremotely.com/remote-jobs.rss"


class WeWorkRemotelySource(JobSource):
    name = SOURCE

    def __init__(
        self,
        *,
        client: HttpClient | None = None,
        feed_text: str | None = None,
        enabled: bool = True,
    ) -> None:
        super().__init__(enabled=enabled)
        self._client = client
        self._feed_text = feed_text

    def fetch(self) -> Iterable[RawJob]:
        try:
            feed_text = self._feed_text if self._feed_text is not None else self._get()
            return list(self._parse(feed_text))
        except Exception as exc:  # noqa: BLE001 - isolate source failures
            log.error("fetch_failed", source=SOURCE, error=str(exc))
            return []

    def _get(self) -> str:
        import httpx

        client = self._client or httpx.Client(timeout=30)
        try:
            resp = client.get(FEED_URL, headers={"User-Agent": "jobpilot"})
            resp.raise_for_status()
            return resp.text
        finally:
            if self._client is None:
                client.close()

    def _parse(self, feed_text: str) -> Iterable[RawJob]:
        import feedparser

        parsed = feedparser.parse(feed_text)
        for entry in parsed.entries:
            try:
                yield self._parse_entry(entry)
            except (ValueError, KeyError, TypeError) as exc:
                log.warning("skip_malformed_item", source=SOURCE, error=str(exc))

    def _parse_entry(self, entry: Any) -> RawJob:
        raw_title = str(entry.get("title", ""))
        company, _, title = raw_title.partition(":")
        if title.strip():
            company_name, job_title = company, title
        else:
            # No "Company: Title" split available; fall back to the raw title.
            company_name, job_title = (entry.get("author") or ""), raw_title
        link = str(entry.get("link", ""))
        region = None
        tags = entry.get("tags") or []
        if tags:
            region = str(tags[0].get("term", "")) or None
        return normalize(
            source=SOURCE,
            external_id=str(entry.get("id") or entry.get("guid") or link),
            url=link,
            title=job_title,
            company=company_name,
            location=region,
            remote=True,
            description=str(entry.get("summary", "")),
        )


register(SOURCE, WeWorkRemotelySource)
