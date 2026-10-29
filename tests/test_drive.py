"""Drive storage: mocked upload returns a StoredFile with web_view_link."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from jobpilot.config import Settings
from jobpilot.models import StoredFile
from jobpilot.storage.drive import DriveFileStorage


class _FakeCreate:
    def __init__(self, recorder: dict[str, Any]) -> None:
        self._recorder = recorder

    def execute(self) -> dict[str, str]:
        return {
            "id": "drive-file-123",
            "name": self._recorder["body"]["name"],
            "webViewLink": "https://drive.google.com/file/d/drive-file-123/view",
        }


class _FakeFiles:
    def __init__(self, recorder: dict[str, Any]) -> None:
        self._recorder = recorder

    def create(self, **kwargs: Any) -> _FakeCreate:
        self._recorder.update(kwargs)
        return _FakeCreate(self._recorder)


class FakeDriveClient:
    """Captures the create() arguments and returns canned metadata."""

    def __init__(self) -> None:
        self.recorder: dict[str, Any] = {}

    def files(self) -> _FakeFiles:
        return _FakeFiles(self.recorder)


def test_upload_returns_stored_file_with_web_view_link(tmp_path: Path) -> None:
    client = FakeDriveClient()
    storage = DriveFileStorage(folder_id="folder-xyz", client=client)

    pdf = tmp_path / "cv.pdf"
    pdf.write_bytes(b"%PDF-1.4 fake")

    stored = storage.upload(pdf, "Jane_Doe_ACME_Backend_EN.pdf")

    assert isinstance(stored, StoredFile)
    assert stored.file_id == "drive-file-123"
    assert stored.web_view_link.endswith("/view")
    assert stored.name == "Jane_Doe_ACME_Backend_EN.pdf"
    # The configured folder id is passed as the parent, and the link is requested.
    assert client.recorder["body"]["parents"] == ["folder-xyz"]
    assert "webViewLink" in client.recorder["fields"]


def test_from_settings_requires_folder_id() -> None:
    with pytest.raises(ValueError, match="DRIVE_FOLDER_ID"):
        DriveFileStorage.from_settings(Settings(drive_folder_id=None))


def test_from_settings_uses_configured_folder(tmp_path: Path) -> None:
    client = FakeDriveClient()
    storage = DriveFileStorage.from_settings(
        Settings(drive_folder_id="folder-from-settings"), client=client
    )
    pdf = tmp_path / "cover.pdf"
    pdf.write_bytes(b"%PDF-1.4 fake")
    storage.upload(pdf, "cover.pdf")
    assert client.recorder["body"]["parents"] == ["folder-from-settings"]


# --------------------------------------------------------------------------- #
# OAuth mode
# --------------------------------------------------------------------------- #
def _settings(**overrides: Any) -> Settings:
    base: dict[str, Any] = {"_env_file": None}
    base.update(overrides)
    return Settings(**base)


def test_oauth_client_falls_back_to_gmail_client() -> None:
    settings = _settings(gmail_client_id="gid", gmail_client_secret="gsecret")
    assert settings.drive_oauth_client_id == "gid"
    assert settings.drive_oauth_client_secret == "gsecret"


def test_dedicated_oauth_client_wins_over_gmail_client() -> None:
    settings = _settings(
        gmail_client_id="gid",
        gmail_client_secret="gsecret",
        google_oauth_client_id="did",
        google_oauth_client_secret="dsecret",
    )
    assert settings.drive_oauth_client_id == "did"
    assert settings.drive_oauth_client_secret == "dsecret"


def test_credentials_use_oauth_when_refresh_token_is_set() -> None:
    from jobpilot.storage.drive import build_drive_credentials, uses_oauth

    settings = _settings(
        drive_refresh_token="refresh-1",
        gmail_client_id="gid",
        gmail_client_secret="gsecret",
        # A service account is also configured; the refresh token must take priority.
        google_service_account_json="{}",
    )
    assert uses_oauth(settings)
    creds = build_drive_credentials(settings)
    assert creds.refresh_token == "refresh-1"
    assert creds.client_id == "gid"
    assert "https://www.googleapis.com/auth/drive" in creds.scopes


def test_oauth_without_client_credentials_fails_clearly() -> None:
    from jobpilot.storage.drive import DriveAuthError, build_drive_credentials

    settings = _settings(drive_refresh_token="refresh-1")
    with pytest.raises(DriveAuthError, match="OAUTH_CLIENT"):
        build_drive_credentials(settings)


def test_no_drive_auth_configured_fails_clearly() -> None:
    from jobpilot.storage.drive import build_drive_credentials

    with pytest.raises(ValueError, match="auth-drive"):
        build_drive_credentials(_settings())


class _FakeFlow:
    """Stands in for InstalledAppFlow: no network, no browser."""

    def __init__(self, refresh_token: str | None) -> None:
        self.redirect_uri: str | None = None
        self.auth_kwargs: dict[str, Any] = {}
        self.fetched_with: str | None = None

        class _Creds:
            pass

        self.credentials = _Creds()
        self.credentials.refresh_token = refresh_token  # type: ignore[attr-defined]

    def authorization_url(self, **kwargs: Any) -> tuple[str, str]:
        self.auth_kwargs = kwargs
        return "https://accounts.example/auth?x=1", "state-1"

    def fetch_token(self, *, authorization_response: str) -> None:
        self.fetched_with = authorization_response


def _run_consent(
    flow: _FakeFlow, redirect: str = "http://localhost:9090/?code=abc&state=state-1"
) -> tuple[str, dict[str, Any]]:
    from jobpilot.storage.drive import obtain_refresh_token

    seen: dict[str, Any] = {"opened": [], "announced": []}

    def factory(config: dict[str, Any], scopes: list[str]) -> _FakeFlow:
        seen["config"] = config
        seen["scopes"] = scopes
        return flow

    token = obtain_refresh_token(
        "cid",
        "csecret",
        port=9090,
        flow_factory=factory,
        open_url=seen["opened"].append,
        announce=seen["announced"].append,
        wait=lambda _port: redirect,
    )
    return token, seen


def test_obtain_refresh_token_returns_token_and_requests_offline_consent() -> None:
    flow = _FakeFlow("the-refresh-token")

    token, seen = _run_consent(flow)

    assert token == "the-refresh-token"
    assert seen["config"]["installed"]["client_id"] == "cid"
    assert seen["config"]["installed"]["redirect_uris"] == ["http://localhost:9090/"]
    assert seen["scopes"] == ["https://www.googleapis.com/auth/drive"]
    assert flow.redirect_uri == "http://localhost:9090/"
    # offline + consent forces a refresh token; select_account shows the chooser.
    assert flow.auth_kwargs["access_type"] == "offline"
    assert flow.auth_kwargs["prompt"] == "select_account consent"
    # The consent page is opened and its URL is also printed as a fallback.
    assert seen["opened"] == ["https://accounts.example/auth?x=1"]
    assert "https://accounts.example/auth?x=1" in seen["announced"][0]
    # The authorization code from the redirect is exchanged for tokens.
    assert flow.fetched_with == "http://localhost:9090/?code=abc&state=state-1"


def test_obtain_refresh_token_restores_insecure_transport_env() -> None:
    import os

    os.environ.pop("OAUTHLIB_INSECURE_TRANSPORT", None)
    _run_consent(_FakeFlow("t"))
    assert "OAUTHLIB_INSECURE_TRANSPORT" not in os.environ


def test_obtain_refresh_token_errors_when_google_returns_none() -> None:
    from jobpilot.storage.drive import DriveAuthError

    with pytest.raises(DriveAuthError, match="refresh token"):
        _run_consent(_FakeFlow(None))


def test_obtain_refresh_token_reports_a_google_error_redirect() -> None:
    from jobpilot.storage.drive import DriveAuthError

    flow = _FakeFlow("t")
    with pytest.raises(DriveAuthError, match="access_denied"):
        _run_consent(flow, redirect="http://localhost:9090/?error=access_denied")
    assert flow.fetched_with is None


def _free_port() -> int:
    import socket

    with socket.socket() as sock:
        sock.bind(("localhost", 0))
        return int(sock.getsockname()[1])


def test_wait_for_redirect_survives_junk_requests_before_the_real_one() -> None:
    """Regression: a TLS handshake on the HTTP port consumed the one-shot server.

    The browser hit the port with HTTPS first; the old server spent its only request
    on that junk and then timed out with no code. The tolerant server must ignore it.
    """
    import contextlib
    import socket
    import threading
    import urllib.request

    from jobpilot.storage.drive import wait_for_redirect

    port = _free_port()
    result: dict[str, str] = {}
    thread = threading.Thread(
        target=lambda: result.update(url=wait_for_redirect(port, timeout=20)), daemon=True
    )
    thread.start()

    def connect_with_retry() -> socket.socket:
        import time

        for _ in range(50):
            try:
                return socket.create_connection(("localhost", port), timeout=2)
            except OSError:
                time.sleep(0.1)
        raise AssertionError("redirect server did not start")

    # 1) raw TLS ClientHello-like garbage, exactly what the browser sent
    with connect_with_retry() as junk:
        junk.sendall(b"\x16\x03\x01\x02\x00\x01\x00\x01\xfc\x03\x03V$n\xd7[\x07\x16U")
    # 2) a stray favicon request
    with contextlib.suppress(Exception):  # a 404 is expected and irrelevant here
        urllib.request.urlopen(f"http://localhost:{port}/favicon.ico", timeout=3)  # noqa: S310
    # 3) the real redirect carrying the code
    urllib.request.urlopen(f"http://localhost:{port}/?code=real-code&state=s", timeout=3).read()  # noqa: S310

    thread.join(timeout=10)
    assert result["url"] == f"http://localhost:{port}/?code=real-code&state=s"


def test_wait_for_redirect_times_out_with_a_clear_error() -> None:
    from jobpilot.storage.drive import DriveAuthError, wait_for_redirect

    with pytest.raises(DriveAuthError, match="Timed out"):
        wait_for_redirect(_free_port(), timeout=1.5)
