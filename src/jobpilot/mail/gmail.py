"""Gmail mailer via the Gmail API.

``GmailMailer`` builds a MIME message (body plus the CV/cover-letter PDFs attached)
and sends it with the Gmail API ``users().messages().send`` endpoint. Gmail stores
sent messages in the Sent label automatically, so the owner keeps a record. OAuth
credentials come from a stored refresh token (``GMAIL_REFRESH_TOKEN`` with
``GMAIL_CLIENT_ID``/``GMAIL_CLIENT_SECRET``); the Gmail client is built lazily and
injectable so tests never contact Google.
"""

from __future__ import annotations

import base64
from collections.abc import Sequence
from email.message import EmailMessage
from typing import Any, Protocol

from jobpilot.config import Settings, get_settings
from jobpilot.logging import get_logger
from jobpilot.mail.base import MailConfigurationError, Mailer
from jobpilot.models import Attachment

log = get_logger(__name__)

GMAIL_SEND_SCOPES = ["https://www.googleapis.com/auth/gmail.send"]


class GmailClient(Protocol):
    """The minimal surface of the Gmail v1 client we depend on.

    Declared as a Protocol so tests can inject a fake and production can pass the
    real ``googleapiclient`` resource without a typing shim.
    """

    def users(self) -> Any: ...


def build_gmail_client(settings: Settings) -> GmailClient:
    """Build a real Gmail v1 client from the OAuth refresh token (lazy imports)."""
    if not settings.gmail_client_id:
        raise MailConfigurationError("GMAIL_CLIENT_ID is not configured")
    if not settings.gmail_client_secret:
        raise MailConfigurationError("GMAIL_CLIENT_SECRET is not configured")
    if not settings.gmail_refresh_token:
        raise MailConfigurationError("GMAIL_REFRESH_TOKEN is not configured")

    from google.oauth2.credentials import Credentials  # noqa: PLC0415 - optional, lazy dep
    from googleapiclient.discovery import build  # noqa: PLC0415 - optional, lazy dep

    creds = Credentials(
        token=None,
        refresh_token=settings.gmail_refresh_token,
        client_id=settings.gmail_client_id,
        client_secret=settings.gmail_client_secret,
        token_uri="https://oauth2.googleapis.com/token",
        scopes=GMAIL_SEND_SCOPES,
    )
    client: GmailClient = build("gmail", "v1", credentials=creds, cache_discovery=False)
    return client


def build_mime_message(
    *,
    sender: str | None,
    to: str,
    subject: str,
    body: str,
    attachments: Sequence[Attachment],
) -> EmailMessage:
    """Compose a MIME message with the body and each attachment (CV as a PDF)."""
    message = EmailMessage()
    message["To"] = to
    message["Subject"] = subject
    if sender:
        message["From"] = sender
    message.set_content(body)
    for attachment in attachments:
        maintype, _, subtype = attachment.mime_type.partition("/")
        message.add_attachment(
            attachment.content,
            maintype=maintype or "application",
            subtype=subtype or "octet-stream",
            filename=attachment.filename,
        )
    return message


class GmailMailer(Mailer):
    """Gmail API implementation of :class:`~jobpilot.mail.base.Mailer`."""

    def __init__(
        self,
        *,
        sender: str | None = None,
        client: GmailClient | None = None,
        settings: Settings | None = None,
    ) -> None:
        self._sender = sender
        self._client = client
        self._settings = settings

    @classmethod
    def from_settings(
        cls,
        settings: Settings | None = None,
        *,
        client: GmailClient | None = None,
    ) -> GmailMailer:
        """Build from :class:`Settings`; the client is built lazily on first send."""
        settings = settings or get_settings()
        return cls(sender=settings.mail_from_address, client=client, settings=settings)

    def _ensure_client(self) -> GmailClient:
        if self._client is None:
            self._client = build_gmail_client(self._settings or get_settings())
        return self._client

    def send(
        self,
        to: str,
        subject: str,
        body: str,
        attachments: Sequence[Attachment],
    ) -> None:
        """Build the MIME message and send it with the Gmail API."""
        client = self._ensure_client()
        message = build_mime_message(
            sender=self._sender,
            to=to,
            subject=subject,
            body=body,
            attachments=attachments,
        )
        raw = base64.urlsafe_b64encode(message.as_bytes()).decode("ascii")
        client.users().messages().send(userId="me", body={"raw": raw}).execute()
        log.info("gmail_send_done", to=to, attachments=len(attachments))
