"""Google Drive implementation of the :class:`~jobpilot.interfaces.FileStorage`
Protocol.

``DriveFileStorage`` uploads a generated document to a Drive folder with a service
account and returns a :class:`~jobpilot.models.StoredFile` carrying the file's
``web_view_link``. The Drive client is built lazily (and can be injected in tests),
and the whole backend sits behind the ``FileStorage`` Protocol, so a Cloudflare R2
(or other) backend can replace it without touching any caller.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

from jobpilot.config import Settings, get_settings
from jobpilot.logging import get_logger
from jobpilot.models import StoredFile

if TYPE_CHECKING:
    from collections.abc import Mapping

log = get_logger(__name__)

_SCOPES = ["https://www.googleapis.com/auth/drive.file"]


class DriveClient(Protocol):
    """The minimal surface of the Google Drive v3 client we depend on.

    Declared as a Protocol so tests can inject a fake and production can pass the
    real ``googleapiclient`` resource without a typing shim.
    """

    def files(self) -> Any: ...


def _load_service_account_info(value: str) -> Mapping[str, object]:
    """Accept either a path to a JSON key file or inline JSON (matches checks)."""
    candidate = Path(value)
    raw = candidate.read_text(encoding="utf-8") if candidate.exists() else value
    data: dict[str, object] = json.loads(raw)
    return data


def _guess_mime_type(path: Path) -> str:
    if path.suffix.lower() == ".pdf":
        return "application/pdf"
    return "application/octet-stream"


def build_drive_client(settings: Settings) -> DriveClient:
    """Build a real Drive v3 client from the service-account settings (lazy imports)."""
    if not settings.google_service_account_json:
        raise ValueError("GOOGLE_SERVICE_ACCOUNT_JSON is not configured")
    from google.oauth2 import service_account  # noqa: PLC0415 - optional, lazy dep
    from googleapiclient.discovery import build  # noqa: PLC0415 - optional, lazy dep
    from googleapiclient.http import MediaFileUpload  # noqa: F401, PLC0415 - import check

    info = _load_service_account_info(settings.google_service_account_json)
    creds = service_account.Credentials.from_service_account_info(info, scopes=_SCOPES)
    client: DriveClient = build("drive", "v3", credentials=creds, cache_discovery=False)
    return client


class DriveFileStorage:
    """Upload documents to Google Drive and return their ``web_view_link``."""

    def __init__(
        self,
        *,
        folder_id: str,
        client: DriveClient | None = None,
        settings: Settings | None = None,
    ) -> None:
        self._folder_id = folder_id
        self._client = client
        self._settings = settings

    @classmethod
    def from_settings(
        cls,
        settings: Settings | None = None,
        *,
        client: DriveClient | None = None,
    ) -> DriveFileStorage:
        """Build from :class:`Settings`, requiring ``DRIVE_FOLDER_ID``."""
        settings = settings or get_settings()
        if not settings.drive_folder_id:
            raise ValueError("DRIVE_FOLDER_ID is not configured")
        return cls(folder_id=settings.drive_folder_id, client=client, settings=settings)

    def _ensure_client(self) -> DriveClient:
        if self._client is None:
            self._client = build_drive_client(self._settings or get_settings())
        return self._client

    def upload(self, path: Path, name: str) -> StoredFile:
        """Upload ``path`` to the configured folder and return its stored metadata."""
        from googleapiclient.http import MediaFileUpload  # noqa: PLC0415 - optional, lazy dep

        client = self._ensure_client()
        metadata = {"name": name, "parents": [self._folder_id]}
        media = MediaFileUpload(str(path), mimetype=_guess_mime_type(path), resumable=False)
        created = (
            client.files()
            .create(
                body=metadata,
                media_body=media,
                fields="id,name,webViewLink",
                supportsAllDrives=True,
            )
            .execute()
        )
        stored = StoredFile(
            file_id=str(created["id"]),
            name=str(created.get("name", name)),
            web_view_link=str(created["webViewLink"]),
        )
        log.info("drive_upload_done", name=stored.name, file_id=stored.file_id)
        return stored
