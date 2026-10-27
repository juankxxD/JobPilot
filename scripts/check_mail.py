"""Standalone wrapper: verify the configured mail provider's credentials."""

from jobpilot.checks import check_mail

if __name__ == "__main__":
    print(check_mail().render())
