"""Verify the configured mail provider's credentials.

Selects Outlook (Microsoft Graph) or Gmail based on ``MAIL_PROVIDER``. The check
acquires an access token from a stored refresh token; it does not send any email.
"""

from __future__ import annotations

from jobpilot.checks.base import CheckResult, missing
from jobpilot.config import MailProvider, get_settings

_GRAPH_SCOPES = ["Mail.Send"]


def _check_outlook() -> CheckResult:
    settings = get_settings()
    if not settings.graph_client_id:
        return missing("Mail (Outlook)", "GRAPH_CLIENT_ID")
    if not settings.graph_refresh_token:
        return missing("Mail (Outlook)", "GRAPH_REFRESH_TOKEN (run `jobpilot auth-mail`)")

    try:
        import msal

        authority = f"https://login.microsoftonline.com/{settings.graph_tenant_id}"
        app = msal.PublicClientApplication(settings.graph_client_id, authority=authority)
        result = app.acquire_token_by_refresh_token(
            settings.graph_refresh_token, scopes=_GRAPH_SCOPES
        )
        if "access_token" in result:
            return CheckResult("Mail (Outlook)", True, "acquired access token via refresh token")
        error = result.get("error_description", result.get("error", "unknown error"))
        return CheckResult("Mail (Outlook)", False, f"token refresh failed: {error}")
    except Exception as exc:  # noqa: BLE001
        return CheckResult("Mail (Outlook)", False, f"connection failed: {exc}")


def _check_gmail() -> CheckResult:
    settings = get_settings()
    if not settings.gmail_client_id:
        return missing("Mail (Gmail)", "GMAIL_CLIENT_ID")
    if not settings.gmail_client_secret:
        return missing("Mail (Gmail)", "GMAIL_CLIENT_SECRET")
    if not settings.gmail_refresh_token:
        return missing("Mail (Gmail)", "GMAIL_REFRESH_TOKEN")

    try:
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials

        creds = Credentials(
            token=None,
            refresh_token=settings.gmail_refresh_token,
            client_id=settings.gmail_client_id,
            client_secret=settings.gmail_client_secret,
            token_uri="https://oauth2.googleapis.com/token",
            scopes=["https://www.googleapis.com/auth/gmail.send"],
        )
        creds.refresh(Request())
        if creds.token:
            return CheckResult("Mail (Gmail)", True, "acquired access token via refresh token")
        return CheckResult("Mail (Gmail)", False, "token refresh returned no access token")
    except Exception as exc:  # noqa: BLE001
        return CheckResult("Mail (Gmail)", False, f"connection failed: {exc}")


def check_mail() -> CheckResult:
    settings = get_settings()
    if settings.mail_provider is MailProvider.gmail:
        return _check_gmail()
    return _check_outlook()
