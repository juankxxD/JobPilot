"""Tests for the optional, feature-flagged fetchers."""

from __future__ import annotations

import base64
import json
from pathlib import Path
from typing import Any

from jobpilot.config import AppConfig, Settings, SourceFlag
from jobpilot.fetchers.adzuna import AdzunaSource
from jobpilot.fetchers.base import iter_enabled
from jobpilot.fetchers.gmail_alerts import (
    GmailAlertsSource,
    _GmailApiService,
    build_gmail_alerts_source,
)
from jobpilot.fetchers.himalayas import HimalayasSource
from tests.httpx_fakes import FakeClient, FakeResponse

FIXTURES = Path(__file__).parent / "fixtures"


def _load_json(name: str) -> Any:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def test_optional_sources_disabled_by_default() -> None:
    config = AppConfig(
        sources={
            "himalayas": SourceFlag(enabled=False),
            "adzuna": SourceFlag(enabled=False),
            "gmail_alerts": SourceFlag(enabled=False),
        }
    )
    names = {s.name for s in iter_enabled(config)}
    assert "himalayas" not in names
    assert "adzuna" not in names
    assert "gmail_alerts" not in names


def test_himalayas_parses_when_enabled() -> None:
    client = FakeClient(FakeResponse(json_data=_load_json("himalayas.json")))
    jobs = list(HimalayasSource(client=client).fetch())
    assert len(jobs) == 1
    assert jobs[0].company == "Himalaya Labs"
    assert jobs[0].location == "Americas, LATAM"


def test_adzuna_requires_credentials() -> None:
    # Without credentials the source yields nothing and never calls HTTP.
    settings = Settings(adzuna_app_id=None, adzuna_app_key=None)
    client = FakeClient(FakeResponse(json_data=_load_json("adzuna.json")))
    assert list(AdzunaSource(client=client, settings=settings).fetch()) == []


def test_adzuna_parses_with_credentials() -> None:
    settings = Settings(adzuna_app_id="id", adzuna_app_key="key")
    client = FakeClient(FakeResponse(json_data=_load_json("adzuna.json")))
    jobs = list(AdzunaSource(client=client, settings=settings).fetch())
    assert len(jobs) == 1
    assert jobs[0].company == "Adzuna Partner Ltd"
    assert jobs[0].location == "London, UK"


class FakeGmailService:
    """A fake Gmail service that returns one alert email; never scrapes."""

    def __init__(self, html: str) -> None:
        encoded = base64.urlsafe_b64encode(html.encode()).decode()
        self._messages = {
            "m1": {
                "payload": {
                    "headers": [{"name": "From", "value": "jobalerts-noreply@linkedin.com"}],
                    "parts": [{"mimeType": "text/html", "body": {"data": encoded}}],
                }
            }
        }
        self.scrape_calls = 0

    def list_messages(self, query: str) -> list[dict[str, Any]]:
        return [{"id": "m1"}]

    def get_message(self, message_id: str) -> dict[str, Any]:
        return self._messages[message_id]


def test_gmail_alerts_reads_email_links_only() -> None:
    html = (
        "<html><body>"
        '<a href="https://www.linkedin.com/jobs/view/123">Backend Engineer</a>'
        '<a href="https://www.linkedin.com/legal/privacy">Privacy</a>'
        "</body></html>"
    )
    service = FakeGmailService(html)
    jobs = list(GmailAlertsSource(service=service).fetch())
    assert len(jobs) == 1  # only the job link, not the privacy link
    assert jobs[0].title == "Backend Engineer"
    assert jobs[0].company == "LinkedIn Alert"
    assert jobs[0].url == "https://www.linkedin.com/jobs/view/123"


def test_gmail_alerts_without_credentials_yields_nothing() -> None:
    # No injected service and no credentials -> nothing collected, no network call.
    settings = Settings(gmail_client_id=None, gmail_client_secret=None, gmail_refresh_token=None)
    assert list(GmailAlertsSource(service=None, settings=settings).fetch()) == []


def test_gmail_alerts_builds_lazy_adapter_from_credentials() -> None:
    # With credentials present, the source lazily builds a read-only Gmail adapter
    # (constructed without any network call — the client is built on first request).
    settings = Settings(
        gmail_client_id="id",
        gmail_client_secret="secret",
        gmail_refresh_token="refresh",
    )
    source = build_gmail_alerts_source(settings=settings)
    service = source._resolve_service()
    assert isinstance(service, _GmailApiService)


def test_gmail_alerts_enabled_in_registry_exercises_adapter() -> None:
    # Registry-level: when the feature flag is on, the registered factory yields a
    # ready-to-fetch source whose configured adapter is a real Gmail read adapter.
    config = AppConfig(sources={"gmail_alerts": SourceFlag(enabled=True)})
    settings = Settings(
        gmail_client_id="id",
        gmail_client_secret="secret",
        gmail_refresh_token="refresh",
    )
    sources = {s.name: s for s in iter_enabled(config)}
    assert "gmail_alerts" in sources
    source = sources["gmail_alerts"]
    assert isinstance(source, GmailAlertsSource)
    # Inject the configured settings and confirm it resolves to the Gmail adapter.
    source._settings = settings
    assert isinstance(source._resolve_service(), _GmailApiService)
