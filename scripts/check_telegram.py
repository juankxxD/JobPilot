"""Standalone wrapper: verify the Telegram bot token."""

from jobpilot.checks import check_telegram

if __name__ == "__main__":
    print(check_telegram().render())
