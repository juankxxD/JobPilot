"""Outlook / Hotmail mailer via Microsoft Graph.

``GraphMailer`` sends an application email through the Graph ``sendMail`` endpoint
using a *delegated* ``Mail.Send`` permission (the message is sent as the signed-in
owner, not an application identity) and ``saveToSentItems=true`` so a copy lands in
the owner's Sent folder. Access tokens are minted on demand from a stored refresh
token (``GRAPH_REFRESH_TOKEN``) via msal; the token acquisition and the HTTP client
are injected so tests never contact Microsoft.

The refresh token is obtained once, interactively, with the OAuth device-code flow
exposed through :func:`obtain_refresh_token_device_code` and wired to the
``jobpilot auth-mail`` CLI command.
"""

from __future__ import annotations

import base64
from collections.abc import Sequence
from typing import TYPE_CHECKING, Any, Protocol

from jobpilot.config import Settings, get_settings
from jobpilot.logging import get_logger
from jobpilot.mail.base import MailConfigurationError, Mailer
from jobpilot.models import Attachment

if TYPE_CHECKING:
    from collections.abc import Callable

log = get_logger(__name__)

# Delegated scope required to send mail as the signed-in user. ``offline_access`` is
# requested during the device-code flow so msal returns a refresh token to store.
GRAPH_SEND_SCOPES = ["Mail.Send"]
_DEVICE_FLOW_SCOPES = ["Mail.Send", "offline_access"]
_GRAPH_SENDMAIL_URL = "https://graph.microsoft.com/v1.0/me/sendMail"


def _authority(tenant_id: str) -> str:
    return f"https://login.microsoftonline.com/{tenant_id}"


class GraphHttpClient(Protocol):
    """The minimal HTTP surface used to call Graph ``sendMail`` (injected in tests)."""

    def post(self, url: str, **kwargs: Any) -> Any: ...


class _HttpxClient:
    """Default transport backed by ``httpx`` (imported lazily)."""

    def __init__(self, *, timeout: float = 30.0) -> None:
        self._timeout = timeout

    def post(self, url: str, **kwargs: Any) -> Any:
        import httpx  # noqa: PLC0415 - lazy so importing the module needs no network

        return httpx.post(url, timeout=self._timeout, **kwargs)


def _build_msal_token_provider(settings: Settings) -> Callable[[], str]:
    """Return a callable that mints a Graph access token from the refresh token."""
    if not settings.graph_client_id:
        raise MailConfigurationError("GRAPH_CLIENT_ID is not configured")
    if not settings.graph_refresh_token:
        raise MailConfigurationError(
            "GRAPH_REFRESH_TOKEN is not configured (run `jobpilot auth-mail`)"
        )
    client_id = settings.graph_client_id
    refresh_token = settings.graph_refresh_token
    tenant_id = settings.graph_tenant_id

    def acquire() -> str:
        import msal  # noqa: PLC0415 - optional, lazy dep

        app = msal.PublicClientApplication(client_id, authority=_authority(tenant_id))
        result = app.acquire_token_by_refresh_token(refresh_token, scopes=GRAPH_SEND_SCOPES)
        token = result.get("access_token")
        if not token:
            error = result.get("error_description", result.get("error", "unknown error"))
            raise MailConfigurationError(f"Graph token refresh failed: {error}")
        return str(token)

    return acquire


def _attachment_payload(attachment: Attachment) -> dict[str, Any]:
    """Encode an attachment as a Graph ``fileAttachment`` resource."""
    return {
        "@odata.type": "#microsoft.graph.fileAttachment",
        "name": attachment.filename,
        "contentType": attachment.mime_type,
        "contentBytes": base64.b64encode(attachment.content).decode("ascii"),
    }


class GraphMailer(Mailer):
    """Microsoft Graph implementation of :class:`~jobpilot.mail.base.Mailer`."""

    def __init__(
        self,
        *,
        token_provider: Callable[[], str],
        sender: str | None = None,
        http: GraphHttpClient | None = None,
    ) -> None:
        self._token_provider = token_provider
        self._sender = sender
        self._http = http or _HttpxClient()

    @classmethod
    def from_settings(
        cls,
        settings: Settings | None = None,
        *,
        token_provider: Callable[[], str] | None = None,
        http: GraphHttpClient | None = None,
    ) -> GraphMailer:
        """Build from :class:`Settings`, wiring the msal refresh-token provider."""
        settings = settings or get_settings()
        provider = token_provider or _build_msal_token_provider(settings)
        return cls(token_provider=provider, sender=settings.mail_from_address, http=http)

    def send(
        self,
        to: str,
        subject: str,
        body: str,
        attachments: Sequence[Attachment],
    ) -> None:
        """POST a ``sendMail`` request, saving a copy to Sent Items.

        The message is sent with a delegated token, ``saveToSentItems`` true, and
        each attachment encoded as a base64 ``fileAttachment``. A non-2xx response
        raises so the caller never records an application that was not actually
        sent.
        """
        token = self._token_provider()
        message: dict[str, Any] = {
            "subject": subject,
            "body": {"contentType": "Text", "content": body},
            "toRecipients": [{"emailAddress": {"address": to}}],
        }
        if attachments:
            message["attachments"] = [_attachment_payload(a) for a in attachments]
        payload = {"message": message, "saveToSentItems": True}
        headers = {"Authorization": f"Bearer {token}"}
        resp = self._http.post(_GRAPH_SENDMAIL_URL, json=payload, headers=headers)
        status = getattr(resp, "status_code", 0)
        if status >= 400:
            text = getattr(resp, "text", "")
            log.warning("graph_sendmail_error", status=status, response=text)
            raise MailConfigurationError(f"Graph sendMail failed: HTTP {status} {text}")
        log.info("graph_sendmail_done", to=to, attachments=len(attachments))


def obtain_refresh_token_device_code(
    client_id: str,
    *,
    tenant_id: str = "consumers",
    prompt: Callable[[str], None] | None = None,
    app_factory: Callable[[str, str], Any] | None = None,
) -> str:
    """Run the OAuth device-code flow and return the acquired refresh token.

    Prints the verification URL and user code (via ``prompt``), blocks on
    :meth:`PublicClientApplication.acquire_token_by_device_flow` until the owner
    completes consent in a browser, and returns the ``refresh_token`` to store as a
    secret. ``app_factory`` and ``prompt`` are injected so the CLI test drives the
    flow without msal or any network. Never prints or returns an access token — only
    the long-lived refresh token the mailer later exchanges.
    """
    emit = prompt or (lambda message: None)
    if app_factory is None:

        def app_factory(cid: str, authority: str) -> Any:
            import msal  # noqa: PLC0415 - optional, lazy dep

            return msal.PublicClientApplication(cid, authority=authority)

    app = app_factory(client_id, _authority(tenant_id))
    flow = app.initiate_device_flow(scopes=_DEVICE_FLOW_SCOPES)
    if "user_code" not in flow:
        error = flow.get("error_description", flow.get("error", "unknown error"))
        raise MailConfigurationError(f"Device flow could not be started: {error}")
    emit(flow.get("message", "Complete the sign-in in your browser to continue."))
    result = app.acquire_token_by_device_flow(flow)
    refresh_token = result.get("refresh_token")
    if not refresh_token:
        error = result.get("error_description", result.get("error", "unknown error"))
        raise MailConfigurationError(f"Device flow did not return a refresh token: {error}")
    return str(refresh_token)
