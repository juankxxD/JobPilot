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
