"""Standalone wrapper: verify the Google Drive service-account credential."""

from jobpilot.checks import check_drive

if __name__ == "__main__":
    print(check_drive().render())
