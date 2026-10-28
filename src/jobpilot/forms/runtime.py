"""Playwright runtime for assisted forms (LOCAL only).

This is the only place that touches Playwright, and it does so lazily: the import
happens inside :func:`open_form_page`, so every other forms module imports cleanly
in CI with no browser installed. The context manager owns the browser lifecycle
(launch -> new page -> navigate -> always close), and the visible/headless choice
plus the slow-motion delay come from :class:`~jobpilot.config.FormsConfig`, so the
owner watches the form being filled on their own machine.

Nothing here is ever invoked in CI: the pipeline's form entrypoint is local-only
and the tests drive the platforms through a fake page instead of this runtime.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import TYPE_CHECKING, Any

from jobpilot.logging import get_logger

if TYPE_CHECKING:
    from jobpilot.config import FormsConfig

log = get_logger(__name__)


@contextmanager
def open_form_page(url: str, config: FormsConfig) -> Iterator[Any]:
    """Open ``url`` in a Playwright-controlled browser and yield the page.

    Playwright is imported here (never at module import time) so this file only
    needs a browser when a real local run opens one. The browser is launched
    visible by default (``FormsConfig.headless`` is ``False``) so the owner sees the
    form being filled, and is always closed on exit even if filling raises.
    """
    from playwright.sync_api import sync_playwright  # noqa: PLC0415 - lazy, local only

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=config.headless, slow_mo=config.slow_mo_ms)
        try:
            page = browser.new_page()
            log.info("form_page_open", url=url, headless=config.headless)
            page.goto(url)
            yield page
        finally:
            browser.close()
            log.info("form_page_closed", url=url)
