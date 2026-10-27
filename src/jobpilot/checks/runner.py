"""Run every credential check and print a summary."""

from __future__ import annotations

from collections.abc import Callable

from jobpilot.checks.base import CheckResult
from jobpilot.checks.drive import check_drive
from jobpilot.checks.groq import check_groq
from jobpilot.checks.mail import check_mail
from jobpilot.checks.neon import check_neon
from jobpilot.checks.telegram import check_telegram

CHECKS: list[Callable[[], CheckResult]] = [
    check_groq,
    check_neon,
    check_telegram,
    check_drive,
    check_mail,
]


def run_all_checks() -> bool:
    """Run all checks, print each result, and return True if all passed."""
    results = [check() for check in CHECKS]
    print("\nJobPilot credential checks")
    print("=" * 40)
    for result in results:
        print(result.render())
    print("=" * 40)
    passed = sum(1 for r in results if r.ok)
    print(f"{passed}/{len(results)} checks passed.\n")
    return all(r.ok for r in results)
