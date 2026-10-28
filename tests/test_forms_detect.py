"""Platform detection: greenhouse/lever by URL and DOM; unknown/unsupported -> None."""

from __future__ import annotations

from jobpilot.forms.base import Platform, detect_platform


def test_greenhouse_detected_by_url() -> None:
    assert detect_platform("https://boards.greenhouse.io/acme/jobs/123") == Platform.greenhouse


def test_lever_detected_by_url() -> None:
    assert detect_platform("https://jobs.lever.co/acme/abc-123") == Platform.lever


def test_greenhouse_detected_by_dom_on_company_domain() -> None:
    # A greenhouse board embedded on a company domain: the URL gives nothing away,
    # but the mounting div marker in the DOM identifies the platform.
    url = "https://careers.acme.com/apply"
    dom = '<div id="grnhse_app"></div>'
    assert detect_platform(url, dom) == Platform.greenhouse


def test_lever_detected_by_dom() -> None:
    url = "https://careers.acme.com/apply"
    dom = '<form class="application-form"><button data-qa="btn-submit"></button></form>'
    assert detect_platform(url, dom) == Platform.lever


def test_unknown_platform_returns_none() -> None:
    assert detect_platform("https://careers.acme.com/apply") is None
    assert detect_platform("https://careers.acme.com/apply", "<div>nothing here</div>") is None


def test_linkedin_and_indeed_are_never_supported() -> None:
    # Out of scope by policy: no Easy Apply, no Indeed, even with a greenhouse marker.
    assert detect_platform("https://www.linkedin.com/jobs/view/123") is None
    assert detect_platform("https://www.indeed.com/viewjob?jk=123") is None
    assert detect_platform("https://www.linkedin.com/apply", '<div id="grnhse_app"></div>') is None
