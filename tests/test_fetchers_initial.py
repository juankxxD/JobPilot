"""Tests for the four initial fetchers (fixtures parsed, HTTP faked)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from jobpilot.fetchers.getonboard import GetOnBoardSource
from jobpilot.fetchers.remoteok import RemoteOkSource
from jobpilot.fetchers.remotive import RemotiveSource
from jobpilot.fetchers.weworkremotely import WeWorkRemotelySource
from tests.httpx_fakes import FakeClient, FakeResponse, RaisingClient

FIXTURES = Path(__file__).parent / "fixtures"


def _load_json(name: str) -> object:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def test_getonboard_parses_fixture() -> None:
    client = FakeClient(FakeResponse(json_data=_load_json("getonboard.json")))
    jobs = list(GetOnBoardSource(client=client).fetch())
    assert len(jobs) == 2
    first = jobs[0]
    assert first.source == "getonboard"
    assert first.external_id == "gob-1"
    assert first.company == "Acme LATAM"
    assert first.remote is True
    assert first.salary_text == "4000-6000 USD"


def test_remoteok_skips_legal_entry() -> None:
    client = FakeClient(FakeResponse(json_data=_load_json("remoteok.json")))
    jobs = list(RemoteOkSource(client=client).fetch())
    assert [j.external_id for j in jobs] == ["rok-1", "rok-2"]
    assert all(j.remote for j in jobs)


def test_remotive_parses_fixture() -> None:
    client = FakeClient(FakeResponse(json_data=_load_json("remotive.json")))
    jobs = list(RemotiveSource(client=client).fetch())
    assert [j.external_id for j in jobs] == ["101", "102"]
    assert jobs[0].company == "Globant Remote"
    assert jobs[0].salary_text == "$80k - $110k USD"


def test_weworkremotely_parses_rss_title_split() -> None:
    feed_text = (FIXTURES / "weworkremotely.rss").read_text(encoding="utf-8")
    jobs = list(WeWorkRemotelySource(feed_text=feed_text).fetch())
    assert len(jobs) == 2
    assert jobs[0].company == "Acme Corp"
    assert jobs[0].title == "Senior Backend Engineer"
    assert jobs[0].remote is True


@pytest.mark.parametrize(
    "source_cls",
    [GetOnBoardSource, RemoteOkSource, RemotiveSource],
)
def test_initial_fetchers_isolate_errors(source_cls: type) -> None:
    # A failing HTTP client must yield an empty list, never raise.
    source = source_cls(client=RaisingClient())
    assert list(source.fetch()) == []


def test_weworkremotely_isolates_errors() -> None:
    source = WeWorkRemotelySource(client=RaisingClient())
    assert list(source.fetch()) == []
