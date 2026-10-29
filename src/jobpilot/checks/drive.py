"""Verify the Google Drive credential (user OAuth or service account).

Confirms the configured folder is reachable with whichever auth mode is configured.
"""

from __future__ import annotations

from jobpilot.checks.base import CheckResult, missing
from jobpilot.config import get_settings


def check_drive() -> CheckResult:
    settings = get_settings()
    if not settings.drive_folder_id:
        return missing("Drive", "DRIVE_FOLDER_ID")
    if not settings.drive_refresh_token and not settings.google_service_account_json:
        return missing("Drive", "DRIVE_REFRESH_TOKEN (run `jobpilot auth-drive`)")

    try:
        from jobpilot.storage.drive import build_drive_client, uses_oauth

        service = build_drive_client(settings)
        meta = (
            service.files()
            .get(fileId=settings.drive_folder_id, fields="id,name", supportsAllDrives=True)
            .execute()
        )
        mode = "OAuth" if uses_oauth(settings) else "service account"
        return CheckResult("Drive", True, f"folder reachable via {mode}: '{meta.get('name')}'")
    except Exception as exc:  # noqa: BLE001
        return CheckResult("Drive", False, f"connection failed: {exc}")
