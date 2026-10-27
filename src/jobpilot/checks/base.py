"""Shared helpers for the credential-verification checks."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(slots=True)
class CheckResult:
    """Outcome of a single credential check."""

    service: str
    ok: bool
    detail: str

    def render(self) -> str:
        mark = "OK  " if self.ok else "FAIL"
        return f"[{mark}] {self.service}: {self.detail}"


def missing(service: str, *names: str) -> CheckResult:
    """Build a failure result for one or more missing environment variables."""
    joined = ", ".join(names)
    return CheckResult(service, False, f"missing configuration: {joined}")
