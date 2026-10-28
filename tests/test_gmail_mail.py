"""Gmail mailer: builds a MIME message with the PDF attachment via a faked client."""

from __future__ import annotations

import base64
from email import message_from_bytes
from email.message import EmailMessage
from email.policy import default as default_policy
from typing import Any

from jobpilot.mail.gmail import GmailMailer, build_mime_message
from jobpilot.models import Attachment


class _FakeSend:
    def __init__(self, recorder: dict[str, Any]) -> None:
        self._recorder = recorder

    def execute(self) -> dict[str, str]:
        return {"id": "gmail-msg-1"}


class _FakeMessages:
    def __init__(self, recorder: dict[str, Any]) -> None:
        self._recorder = recorder

    def send(self, *, userId: str, body: dict[str, Any]) -> _FakeSend:  # noqa: N803 - Gmail API name
        self._recorder["userId"] = userId
        self._recorder["body"] = body
        return _FakeSend(self._recorder)


class _FakeUsers:
    def __init__(self, recorder: dict[str, Any]) -> None:
        self._recorder = recorder

    def messages(self) -> _FakeMessages:
        return _FakeMessages(self._recorder)


class FakeGmailClient:
    def __init__(self) -> None:
        self.recorder: dict[str, Any] = {}

    def users(self) -> _FakeUsers:
        return _FakeUsers(self.recorder)


def _pdf(name: str) -> Attachment:
    return Attachment(filename=name, content=b"%PDF-1.4 fake-cv", mime_type="application/pdf")


def test_build_mime_message_attaches_pdf() -> None:
    message = build_mime_message(
        sender="me@example.com",
        to="recruiter@acme.test",
        subject="Application: Backend",
        body="Hello.",
        attachments=[_pdf("CV_EN.pdf")],
    )
    assert message["To"] == "recruiter@acme.test"
    assert message["From"] == "me@example.com"
    assert message["Subject"] == "Application: Backend"
    pdf_parts = [
        part for part in message.iter_attachments() if part.get_content_type() == "application/pdf"
    ]
    assert len(pdf_parts) == 1
    assert pdf_parts[0].get_filename() == "CV_EN.pdf"
    assert pdf_parts[0].get_content() == b"%PDF-1.4 fake-cv"


def test_send_calls_gmail_client_with_raw_message() -> None:
    client = FakeGmailClient()
    mailer = GmailMailer(sender="me@example.com", client=client)

    mailer.send(
        "recruiter@acme.test",
        "Application: Backend",
        "Hello, my CV is attached.",
        [_pdf("CV_EN.pdf")],
    )

    assert client.recorder["userId"] == "me"
    raw = client.recorder["body"]["raw"]
    # The raw payload is base64url-encoded MIME; decode and confirm the PDF survives.
    decoded = base64.urlsafe_b64decode(raw)
    parsed = message_from_bytes(decoded, _class=EmailMessage, policy=default_policy)
    assert parsed["To"] == "recruiter@acme.test"
    attachments = [p for p in parsed.iter_attachments() if p.get_filename() == "CV_EN.pdf"]
    assert len(attachments) == 1
    assert attachments[0].get_content() == b"%PDF-1.4 fake-cv"
