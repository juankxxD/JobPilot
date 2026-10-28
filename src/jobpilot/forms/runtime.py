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
            yield _PlaywrightPage(page)
        finally:
            browser.close()
            log.info("form_page_closed", url=url)


class _PlaywrightPage:
    """Adapt a real Playwright ``Page`` to the :class:`~jobpilot.forms.base.FormPage`.

    The real ``Page`` already provides ``content``/``fill``/``set_input_files``/
    ``select_option``/``check``/``query_selector``/``query_selector_all``/``click``/
    ``screenshot`` with compatible signatures; only ``file_input_value`` has no direct
    equivalent, so it is derived from the file input's own ``input_value`` (which a
    browser reports as the attached file path). Everything else delegates unchanged.
    """

    def __init__(self, page: Any) -> None:
        self._page = page

    def content(self) -> str:
        return str(self._page.content())

    def fill(self, selector: str, value: str) -> None:
        self._page.fill(selector, value)

    def set_input_files(self, selector: str, files: str) -> None:
        self._page.set_input_files(selector, files)

    def file_input_value(self, selector: str) -> str:
        control = self._page.query_selector(selector)
        if control is None:
            return ""
        return str(control.input_value())

    def select_option(self, selector: str, value: str) -> None:
        self._page.select_option(selector, value)

    def check(self, selector: str) -> None:
        self._page.check(selector)

    def query_selector(self, selector: str) -> Any:
        return self._page.query_selector(selector)

    def query_selector_all(self, selector: str) -> list[Any]:
        return list(self._page.query_selector_all(selector))

    def click(self, selector: str) -> None:
        self._page.click(selector)

    def screenshot(self, *, path: str) -> None:
        self._page.screenshot(path=path)
