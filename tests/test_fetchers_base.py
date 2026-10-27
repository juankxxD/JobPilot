"""Tests for the fetcher base: normalize helper and source registry."""

from __future__ import annotations

import pytest

from jobpilot.config import AppConfig, SourceFlag
from jobpilot.fetchers.base import iter_enabled, normalize


def test_normalize_trims_and_builds_rawjob() -> None:
    raw = normalize(
        source="src",
        external_id="  e1  ",
        url="  https://example.com/1  ",
        title="  Engineer  ",
        company="  Acme  ",
        description="  hello  ",
    )
    assert raw.external_id == "e1"
    assert raw.url == "https://example.com/1"
    assert raw.title == "Engineer"
    assert raw.company == "Acme"
    assert raw.description == "hello"


def test_normalize_derives_stable_external_id_from_url() -> None:
    a = normalize(source="src", external_id=None, url="https://x/1", title="t", company="c")
    b = normalize(source="src", external_id="", url="https://x/1", title="t", company="c")
    assert a.external_id == b.external_id
    assert a.external_id.startswith("url-")


def test_normalize_requires_core_fields() -> None:
    with pytest.raises(ValueError):
        normalize(source="src", external_id="e", url="", title="t", company="c")
    with pytest.raises(ValueError):
        normalize(source="src", external_id="e", url="https://x", title="", company="c")
    with pytest.raises(ValueError):
        normalize(source="src", external_id="e", url="https://x", title="t", company="")


def test_iter_enabled_returns_only_enabled_sources() -> None:
    config = AppConfig(
        sources={
            "getonboard": SourceFlag(enabled=True),
            "remoteok": SourceFlag(enabled=False),
            "remotive": SourceFlag(enabled=True),
            "weworkremotely": SourceFlag(enabled=False),
            "himalayas": SourceFlag(enabled=False),
            "adzuna": SourceFlag(enabled=False),
            "gmail_alerts": SourceFlag(enabled=False),
        }
    )
    names = {source.name for source in iter_enabled(config)}
    assert names == {"getonboard", "remotive"}
