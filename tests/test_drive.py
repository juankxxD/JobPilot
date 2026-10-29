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
    def __init__(self, refresh_token: str | None) -> None:
        self._refresh_token = refresh_token
        self.run_kwargs: dict[str, Any] = {}

    def run_local_server(self, **kwargs: Any) -> Any:
        self.run_kwargs = kwargs

        class _Creds:
            refresh_token = self._refresh_token

        return _Creds()


def test_obtain_refresh_token_returns_token_and_requests_offline_consent() -> None:
    from jobpilot.storage.drive import obtain_refresh_token

    flow = _FakeFlow("the-refresh-token")
    seen: dict[str, Any] = {}

    def factory(config: dict[str, Any], scopes: list[str]) -> _FakeFlow:
        seen["config"] = config
        seen["scopes"] = scopes
        return flow

    token = obtain_refresh_token("cid", "csecret", port=9090, flow_factory=factory)

    assert token == "the-refresh-token"
    assert seen["config"]["installed"]["client_id"] == "cid"
    assert seen["config"]["installed"]["redirect_uris"] == ["http://localhost:9090/"]
    assert seen["scopes"] == ["https://www.googleapis.com/auth/drive"]
    # offline + consent is what forces Google to issue a refresh token.
    assert flow.run_kwargs["access_type"] == "offline"
    assert flow.run_kwargs["prompt"] == "consent"
    assert flow.run_kwargs["port"] == 9090


def test_obtain_refresh_token_errors_when_google_returns_none() -> None:
    from jobpilot.storage.drive import DriveAuthError, obtain_refresh_token

    with pytest.raises(DriveAuthError, match="refresh token"):
        obtain_refresh_token("cid", "csecret", flow_factory=lambda c, s: _FakeFlow(None))
