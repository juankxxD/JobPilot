"""Verify the Google Drive service-account credential.

Confirms the configured folder is reachable with the service account.
"""

from __future__ import annotations

import json
from pathlib import Path

from jobpilot.checks.base import CheckResult, missing
from jobpilot.config import get_settings

_SCOPES = ["https://www.googleapis.com/auth/drive"]


def _load_service_account_info(value: str) -> dict[str, object]:
    """Accept either a path to a JSON file or inline JSON."""
    candidate = Path(value)
    raw = candidate.read_text(encoding="utf-8") if candidate.exists() else value
    data: dict[str, object] = json.loads(raw)
    return data


def check_drive() -> CheckResult:
    settings = get_settings()
    if not settings.google_service_account_json:
        return missing("Drive", "GOOGLE_SERVICE_ACCOUNT_JSON")
    if not settings.drive_folder_id:
        return missing("Drive", "DRIVE_FOLDER_ID")

    try:
        from google.oauth2 import service_account
        from googleapiclient.discovery import build

        info = _load_service_account_info(settings.google_service_account_json)
        creds = service_account.Credentials.from_service_account_info(info, scopes=_SCOPES)
        service = build("drive", "v3", credentials=creds, cache_discovery=False)
        meta = (
            service.files()
            .get(fileId=settings.drive_folder_id, fields="id,name", supportsAllDrives=True)
            .execute()
        )
        return CheckResult("Drive", True, f"folder reachable: '{meta.get('name')}'")
    except Exception as exc:  # noqa: BLE001
        return CheckResult("Drive", False, f"connection failed: {exc}")
