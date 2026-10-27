"""Standalone wrapper: verify the Groq credential."""

from jobpilot.checks import check_groq

if __name__ == "__main__":
    print(check_groq().render())
