"""Structured logging setup using structlog.

Emits human-friendly console output locally and JSON lines in CI (detected via the
``CI`` or ``JOBPILOT_JSON_LOGS`` environment variables).
"""

from __future__ import annotations

import logging
import os
import sys
from typing import Any

import structlog

_configured = False


def _want_json_logs() -> bool:
    return bool(os.environ.get("CI") or os.environ.get("JOBPILOT_JSON_LOGS"))


def configure_logging(level: str = "INFO") -> None:
    """Configure structlog + stdlib logging once.

    Safe to call multiple times; only the first call takes effect.
    """
    global _configured
    if _configured:
        return

    numeric_level = getattr(logging, level.upper(), logging.INFO)

    shared_processors: list[Any] = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.StackInfoRenderer(),
    ]

    if _want_json_logs():
        renderer: Any = structlog.processors.JSONRenderer()
    else:
        renderer = structlog.dev.ConsoleRenderer()

    structlog.configure(
        processors=[*shared_processors, structlog.processors.format_exc_info, renderer],
        wrapper_class=structlog.make_filtering_bound_logger(numeric_level),
        logger_factory=structlog.PrintLoggerFactory(file=sys.stderr),
        cache_logger_on_first_use=True,
    )
    logging.basicConfig(level=numeric_level, stream=sys.stderr)
    _configured = True


def get_logger(name: str | None = None) -> structlog.stdlib.BoundLogger:
    """Return a bound structlog logger, configuring logging on first use."""
    if not _configured:
        configure_logging()
    return structlog.get_logger(name)  # type: ignore[no-any-return]
