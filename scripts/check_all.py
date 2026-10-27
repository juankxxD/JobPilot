"""Standalone wrapper: run all credential checks.

Exit code 0 only if all checks pass.
"""

import sys

from jobpilot.checks import run_all_checks

if __name__ == "__main__":
    sys.exit(0 if run_all_checks() else 1)
