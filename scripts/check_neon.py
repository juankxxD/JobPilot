"""Standalone wrapper: verify the Neon (Postgres) credential."""

from jobpilot.checks import check_neon

if __name__ == "__main__":
    print(check_neon().render())
