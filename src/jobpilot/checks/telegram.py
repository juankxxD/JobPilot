"""Verify the Telegram bot token via getMe."""

from __future__ import annotations

from jobpilot.checks.base import CheckResult, missing
from jobpilot.config import get_settings


def check_telegram() -> CheckResult:
    settings = get_settings()
    if not settings.telegram_bot_token:
        return missing("Telegram", "TELEGRAM_BOT_TOKEN")
    if not settings.telegram_owner_chat_id:
        return missing("Telegram", "TELEGRAM_OWNER_CHAT_ID")

    try:
        import httpx

        url = f"https://api.telegram.org/bot{settings.telegram_bot_token}/getMe"
        resp = httpx.get(url, timeout=15)
        data = resp.json()
        if resp.status_code == 200 and data.get("ok"):
            username = data["result"].get("username", "?")
            return CheckResult("Telegram", True, f"connected as @{username}")
        return CheckResult("Telegram", False, f"API returned: {data}")
    except Exception as exc:  # noqa: BLE001
        return CheckResult("Telegram", False, f"connection failed: {exc}")
