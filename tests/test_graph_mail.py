"""Graph mailer: sendMail payload, saveToSentItems, attachments, auth-mail flow."""

from __future__ import annotations

import base64
from typing import Any

import pytest
from typer.testing import CliRunner

from jobpilot.cli import app
from jobpilot.mail.base import MailConfigurationError
from jobpilot.mail.graph import (
    GraphMailer,
    obtain_refresh_token_device_code,
)
from jobpilot.models import Attachment

runner = CliRunner()


class _FakeResponse:
    def __init__(self, status_code: int = 202, text: str = "") -> None:
        self.status_code = status_code
        self.text = text


class _RecordingHttp:
    """Records the single POST and returns a canned response."""

    def __init__(self, status_code: int = 202) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self._status_code = status_code

    def post(self, url: str, **kwargs: Any) -> _FakeResponse:
        self.calls.append((url, kwargs))
        return _FakeResponse(status_code=self._status_code)


def _pdf(name: str) -> Attachment:
    return Attachment(filename=name, content=b"%PDF-1.4 fake", mime_type="application/pdf")


def test_send_posts_graph_payload_with_save_to_sent_items_and_attachment() -> None:
    http = _RecordingHttp()
    mailer = GraphMailer(
        token_provider=lambda: "access-token-xyz",
        sender="me@example.com",
        http=http,
    )

    mailer.send(
        "recruiter@acme.test",
        "Application: Backend Engineer",
        "Hello, please find my CV attached.",
        [_pdf("CV_EN.pdf"), _pdf("Cover_EN.pdf")],
    )

    assert len(http.calls) == 1
    url, kwargs = http.calls[0]
    assert url.endswith("/me/sendMail")
    # Delegated send as the signed-in user, with the bearer token.
    assert kwargs["headers"]["Authorization"] == "Bearer access-token-xyz"
    payload = kwargs["json"]
    assert payload["saveToSentItems"] is True
    message = payload["message"]
    assert message["toRecipients"][0]["emailAddress"]["address"] == "recruiter@acme.test"
    assert message["subject"] == "Application: Backend Engineer"
    attachments = message["attachments"]
    assert [a["name"] for a in attachments] == ["CV_EN.pdf", "Cover_EN.pdf"]
    assert all(a["@odata.type"] == "#microsoft.graph.fileAttachment" for a in attachments)
    # Content is base64-encoded bytes of the PDF.
    assert base64.b64decode(attachments[0]["contentBytes"]) == b"%PDF-1.4 fake"


def test_send_raises_on_non_2xx_response() -> None:
    http = _RecordingHttp(status_code=401)
    mailer = GraphMailer(token_provider=lambda: "token", http=http)
    with pytest.raises(MailConfigurationError, match="Graph sendMail failed"):
        mailer.send("x@acme.test", "s", "b", [])


# --------------------------------------------------------------------------- #
# Device-code flow (auth-mail)
# --------------------------------------------------------------------------- #
class _FakeMsalApp:
    """A fake msal PublicClientApplication driving the device-code flow."""

    def __init__(self, *, flow: dict[str, Any], result: dict[str, Any]) -> None:
        self._flow = flow
        self._result = result
        self.acquired_with: dict[str, Any] | None = None

    def initiate_device_flow(self, *, scopes: list[str]) -> dict[str, Any]:
        self._scopes = scopes
        return self._flow

    def acquire_token_by_device_flow(self, flow: dict[str, Any]) -> dict[str, Any]:
        self.acquired_with = flow
        return self._result


def test_obtain_refresh_token_device_code_returns_token() -> None:
    prompts: list[str] = []
    fake = _FakeMsalApp(
        flow={"user_code": "ABC-123", "message": "Go to https://aka.ms/devicelogin"},
        result={"refresh_token": "the-refresh-token", "access_token": "ignored"},
    )

    token = obtain_refresh_token_device_code(
        "client-123",
        prompt=prompts.append,
        app_factory=lambda cid, authority: fake,
    )

    assert token == "the-refresh-token"
    # The verification message is shown; offline_access is requested so a refresh
    # token comes back.
    assert any("devicelogin" in p for p in prompts)
    assert "offline_access" in fake._scopes


def test_obtain_refresh_token_raises_without_user_code() -> None:
    fake = _FakeMsalApp(flow={"error": "bad"}, result={})
    with pytest.raises(MailConfigurationError, match="Device flow could not be started"):
        obtain_refresh_token_device_code("client-123", app_factory=lambda cid, authority: fake)


def test_auth_mail_cli_without_client_id_fails_cleanly(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GRAPH_CLIENT_ID", raising=False)
    from jobpilot.config import get_settings

    get_settings.cache_clear()
    result = runner.invoke(app, ["auth-mail"])
    assert result.exit_code == 1
    assert "GRAPH_CLIENT_ID" in result.output


def test_auth_mail_cli_prints_refresh_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GRAPH_CLIENT_ID", "client-123")
    from jobpilot.config import get_settings

    get_settings.cache_clear()

    def fake_obtain(client_id: str, **kwargs: Any) -> str:
        prompt = kwargs.get("prompt")
        if prompt is not None:
            prompt("Go to https://aka.ms/devicelogin and enter ABC-123")
        return "printed-refresh-token"

    monkeypatch.setattr("jobpilot.mail.graph.obtain_refresh_token_device_code", fake_obtain)
    result = runner.invoke(app, ["auth-mail"])
    assert result.exit_code == 0
    assert "printed-refresh-token" in result.output
    get_settings.cache_clear()
