# Deployment

JobPilot runs as a batch pipeline on **GitHub Actions**: a cron schedule every 2
hours (configurable) plus manual `workflow_dispatch`. The workflow lives in
[`.github/workflows/pipeline.yml`](../.github/workflows/pipeline.yml). Each run
installs the WeasyPrint system libraries via `apt`, installs the project (without
the `forms`/Playwright extra), then runs `jobpilot migrate` followed by
`jobpilot run`.

**Phase 8 (assisted form filling) never runs in CI.** The `forms` extra is not
installed and `apply-form` is never invoked by the workflow — browser automation is
local-only, by design.

## Changing the schedule

The cadence is one cron expression in the workflow's `on.schedule`. Edit it (for
example `"0 */4 * * *"` for every four hours) and keep `config.yaml`
`schedule.cron_hours` in sync so the documentation reflects reality. The pipeline is
idempotent, so a shorter or longer cadence changes only how often work is picked up,
never correctness. The `concurrency` group ensures two runs never overlap.

## Required GitHub Actions configuration

Add these under **Settings → Secrets and variables → Actions**. Every value comes
from `.env.example`; nothing secret is ever committed.

### Secrets (sensitive — always `secrets.*`)

| Secret | Purpose |
|--------|---------|
| `GROQ_API_KEY` | Groq LLM access (primary) |
| `GROQ_MODEL` | Model override; defaults to `openai/gpt-oss-120b` if unset |
| `DATABASE_URL` | Neon Postgres connection string |
| `TELEGRAM_BOT_TOKEN` | Telegram bot token |
| `TELEGRAM_OWNER_CHAT_ID` | Your chat id (the only chat the bot answers) |
| `GOOGLE_SERVICE_ACCOUNT_JSON` | Service-account JSON for Drive uploads |
| `DRIVE_FOLDER_ID` | Target Drive folder id |
| `MAIL_PROVIDER` | `outlook` (default) or `gmail` |
| `GRAPH_CLIENT_ID` / `GRAPH_TENANT_ID` / `GRAPH_REFRESH_TOKEN` | Outlook via Microsoft Graph (default mailer) |
| `MAIL_FROM_ADDRESS` | Sender address used by the mailer |
| `GMAIL_CLIENT_ID` / `GMAIL_CLIENT_SECRET` / `GMAIL_REFRESH_TOKEN` | Gmail mailer (only if `MAIL_PROVIDER=gmail`) |
| `ADZUNA_APP_ID` / `ADZUNA_APP_KEY` | Only if the Adzuna fetcher is enabled in `config.yaml` |

Provide the credential set for the mailer you actually use. The Graph refresh token
comes from running `jobpilot auth-mail` once locally and storing the printed token.

### Variables (non-secret tunables — `vars.*`)

| Variable | Default | Purpose |
|----------|---------|---------|
| `MIN_SCORE` | `70` | Score threshold to propose an offer |
| `DAILY_APPLICATION_LIMIT` | `5` | Max proposals per day |
| `DEFAULT_LANGUAGE` | `es` | Fallback language, used only when enabled in `config.yaml` |

Unset optional variables fall back to the in-code defaults, so a minimal setup only
needs the secrets for the services you use.

## Migrating to long polling on a VM

Nothing in the business logic assumes GitHub Actions. To run continuously on a VM
(for lower latency on Telegram replies) **without changing any logic**:

1. **Keep the same code.** `channels/telegram.py` already stores the last processed
   `update_id` in the `telegram_state` table and processes each update exactly once.
2. **Replace the single tick with a loop.** On CI, one run calls `get_updates` once.
   On a VM, wrap `run_once` (or just the Telegram-processing step) in a loop that
   calls `get_updates` on a short interval. The persisted offset makes the loop
   resume-safe: a restart continues from the last processed update.
3. **Run migrations once on deploy**, then start the loop. `jobpilot migrate` is
   idempotent, so running it on every start is also safe.
4. **Provide the same environment variables** as the CI secrets/variables above via
   the VM's `.env` or the process manager (systemd, pm2, Docker env).
5. **Install the WeasyPrint system libraries** on the VM the same way CI does
   (`apt-get install libpango-1.0-0 libpangocairo-1.0-0 libgdk-pixbuf2.0-0
   libffi-dev` on Debian/Ubuntu).

Because dedupe keys, the Telegram offset, and send claims are all persisted in
Postgres, moving from scheduled batch runs to a continuous loop changes only *when*
work is picked up, never *what* happens. The assisted-form flow remains local-only
regardless of where the pipeline runs.
