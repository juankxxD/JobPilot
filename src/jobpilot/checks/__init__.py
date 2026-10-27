"""Credential-verification checks (Phase 0).

These live inside the package so both the ``jobpilot check-all`` CLI command and
the standalone ``scripts/check_*.py`` wrappers can import them.
"""

from jobpilot.checks.base import CheckResult, missing
from jobpilot.checks.drive import check_drive
from jobpilot.checks.groq import check_groq
from jobpilot.checks.mail import check_mail
from jobpilot.checks.neon import check_neon
from jobpilot.checks.runner import run_all_checks
from jobpilot.checks.telegram import check_telegram

__all__ = [
    "CheckResult",
    "missing",
    "check_drive",
    "check_groq",
    "check_mail",
    "check_neon",
    "check_telegram",
    "run_all_checks",
]
