# JobPilot

A personal, human-in-the-loop job application bot. JobPilot fetches job offers,
scores them against a single profile source of truth, tailors an ATS-friendly CV
and a cover letter in the offer's language, proposes everything over Telegram, and
— only after you approve with a button — sends by email or helps you fill the
application form. Everything is recorded in Postgres.

> Nothing is ever sent without your explicit approval.

## Requirements

- Python 3.12
- [uv](https://docs.astral.sh/uv/) for dependency management
- System libraries for WeasyPrint (Phase 4): `pango`, `cairo`, `gdk-pixbuf`
  - macOS: `brew install pango cairo gdk-pixbuf libffi`
  - Debian/Ubuntu: `apt-get install libpango-1.0-0 libpangocairo-1.0-0 libgdk-pixbuf2.0-0 libffi-dev`

## Setup

```bash
uv venv --python 3.12
uv pip install -e ".[dev]"
cp .env.example .env            # fill in your secrets
cp profile.example.yaml profile.yaml   # or import with `jobpilot import-cv` (Phase 1)
```

## Commands

```bash
jobpilot check-all      # verify every credential (Groq, Neon, Telegram, Drive, mail)
jobpilot migrate        # apply database migrations (Phase 2)
jobpilot import-cv CV.pdf   # import your CV into a reviewable draft (Phase 1)
jobpilot auth-mail      # one-time OAuth consent for the mail provider (Phase 6)
jobpilot run            # run the full pipeline once (Phase 5+)
```

Run `python -m jobpilot --help` to see all commands.

## Configuration

- **Secrets** live in `.env` (see `.env.example`). Never commit `.env`.
- **Behaviour** lives in `config.yaml` (prefilter rules, scoring weights, feature
  flags, follow-up periods).
- **Profile** is the single source of truth in `profile.yaml` (gitignored). Use
  `profile.example.yaml` as a template.

## Manual setup checklist (Phase 0)

You need to create accounts and obtain these credentials:

| Service | Variables |
|---------|-----------|
| Groq | `GROQ_API_KEY`, `GROQ_MODEL` |
| Neon (Postgres) | `DATABASE_URL` |
| Telegram | `TELEGRAM_BOT_TOKEN`, `TELEGRAM_OWNER_CHAT_ID` |
| Google Drive (service account) | `GOOGLE_SERVICE_ACCOUNT_JSON`, `DRIVE_FOLDER_ID` |
| Outlook via Graph (default mailer) | `GRAPH_CLIENT_ID`, `GRAPH_TENANT_ID`, `GRAPH_REFRESH_TOKEN` |
| Gmail (alternative mailer) | `GMAIL_CLIENT_ID`, `GMAIL_CLIENT_SECRET`, `GMAIL_REFRESH_TOKEN` |

Then run `jobpilot check-all`. Each service reports OK or FAIL.

## Development

```bash
uv run ruff check .
uv run ruff format --check .
uv run mypy src
uv run pytest
```

## Documentation

- [docs/architecture.md](docs/architecture.md)
- [docs/decisions.md](docs/decisions.md)
- [docs/deployment.md](docs/deployment.md)

The full spec lives in `.kiro/specs/jobpilot/` (requirements, design, tasks).
