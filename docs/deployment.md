# Deployment

JobPilot runs as a batch pipeline on **GitHub Actions**: a cron schedule every 2
hours (configurable) plus manual `workflow_dispatch`. The deployment workflow lives
in [`.github/workflows/pipeline.yml`](../.github/workflows/pipeline.yml). Each run
installs the WeasyPrint system libraries via `apt`, installs the project (without
the `forms`/Playwright extra), materializes the owner profile, then runs
`jobpilot migrate` followed by `jobpilot run`.

A separate [`.github/workflows/ci.yml`](../.github/workflows/ci.yml) is the quality
gate. It installs the same native libraries plus the `dev` extra, sets `CI=true`,
and runs `ruff check`, `ruff format --check`, `mypy`, and `pytest`. Because `CI=true`
disables the local WeasyPrint skip, the PDF/ATS tests **execute** there (both-language
rendering and ATS header order are verified against the installed native libraries);
a missing library or a rendering regression fails the run instead of skipping.

**Phase 8 (assisted form filling) never runs in CI.** Neither workflow installs the
`forms` extra and `apply-form` is never invoked — browser automation is local-only,
by design.

## Changing the schedule

The cadence is one cron expression in `pipeline.yml`'s `on.schedule`. Edit it (for
example `"0 */4 * * *"` for every four hours) and keep `config.yaml`
`schedule.cron_hours` in sync so the documentation reflects reality. On the normal
path the pipeline is idempotent, so a shorter or longer cadence changes only how
often work is picked up. The `concurrency` group ensures two runs never overlap.

Idempotency is scoped to the happy path. A few accepted failure windows are **not**
covered and are tracked as deferred durability debt — see
[Known durability limitations](#known-durability-limitations) before assuming a
retry after a crash is always safe. Shortening the cadence increases how often those
windows are entered.

## The owner profile (required)

`profile.yaml` is the owner-approved single source of truth. It is `.gitignore`d and
**never committed**. The deployment provides it through a protected secret:

1. Build the file locally (copy `profile.example.yaml` to `profile.yaml`, or use
   `jobpilot import-cv`) and review it.
2. Store its full contents in the `PROFILE_YAML` GitHub Actions **secret**.
3. The workflow's *Materialize profile.yaml* step writes it to the project root with
   owner-only permissions (`umask 177`) before `jobpilot run`. If the secret is
   unset, the run fails fast with a clear message rather than exiting mid-pipeline on
   a missing profile.

## Required GitHub Actions configuration

Add these under **Settings → Secrets and variables → Actions**. Every value comes
from `.env.example` (plus `PROFILE_YAML`); nothing secret is ever committed.

### Secrets (sensitive — referenced as `secrets.*`)

| Secret | Required | Purpose |
|--------|----------|---------|
| `PROFILE_YAML` | yes | Full contents of your reviewed `profile.yaml` |
| `GROQ_API_KEY` | yes | Groq LLM access (primary) |
| `DATABASE_URL` | yes | Neon Postgres connection string |
| `TELEGRAM_BOT_TOKEN` | yes | Telegram bot token |
| `TELEGRAM_OWNER_CHAT_ID` | yes | Your chat id (the only chat the bot answers) |
| `GOOGLE_SERVICE_ACCOUNT_JSON` | yes | Service-account JSON for Drive uploads |
| `DRIVE_FOLDER_ID`, `DRIVE_REFRESH_TOKEN`, `GOOGLE_OAUTH_CLIENT_ID`, `GOOGLE_OAUTH_CLIENT_SECRET` | yes | Drive folder id plus user-OAuth credentials (see ADR-014) |
| `MAIL_FROM_ADDRESS` | yes | Sender address used by the mailer |
| `GRAPH_CLIENT_ID` / `GRAPH_REFRESH_TOKEN` | if `MAIL_PROVIDER=outlook` | Outlook via Microsoft Graph (default mailer) |
| `GMAIL_CLIENT_ID` / `GMAIL_CLIENT_SECRET` / `GMAIL_REFRESH_TOKEN` | if `MAIL_PROVIDER=gmail` | Gmail mailer |
| `ADZUNA_APP_ID` / `ADZUNA_APP_KEY` | only if Adzuna enabled | Used when the Adzuna fetcher is enabled in `config.yaml` |

Provide the credential set for the mailer you actually use. The Graph refresh token
comes from running `jobpilot auth-mail` once locally and storing the printed token.

### Variables (non-secret tunables — referenced as `vars.*`)

All of these are **optional**. When a variable is unset the workflow drops the empty
value so the application applies its own in-code default (it never exports an empty
string, which would fail `Settings` validation).

| Variable | Default | Purpose |
|----------|---------|---------|
| `GROQ_MODEL` | `openai/gpt-oss-120b` | Groq model override |
| `GRAPH_TENANT_ID` | `consumers` | Azure tenant (use `consumers` for personal Outlook/Hotmail) |
| `MAIL_PROVIDER` | `outlook` | `outlook` or `gmail` |
| `MIN_SCORE` | `70` | Score threshold to propose an offer |
| `DAILY_APPLICATION_LIMIT` | `5` | Max proposals per day (see durability note) |
| `DEFAULT_LANGUAGE` | `es` | Fallback language, used only when enabled in `config.yaml` |

A minimal deployment therefore only needs `PROFILE_YAML`, the service secrets you
use, and nothing from the variables table.

## Migrating to long polling on a VM

Nothing in the business logic assumes GitHub Actions. To run continuously on a VM
(for lower latency on Telegram replies) **without changing any logic**:

1. **Keep the same code.** `channels/telegram.py` stores the last processed
   `update_id` in the `telegram_state` table and, on the normal path, processes each
   update once.
2. **Replace the single tick with a loop.** On CI, one run calls `get_updates` once.
   On a VM, wrap `run_once` (or just the Telegram-processing step) in a loop that
   calls `get_updates` on a short interval. The persisted offset lets the loop pick
   up from the last processed update after a restart on the happy path.
3. **Run migrations once on deploy**, then start the loop. `jobpilot migrate` is
   idempotent, so running it on every start is also safe.
4. **Provide the same environment variables** as the CI secrets/variables above via
   the VM's `.env` or the process manager (systemd, pm2, Docker env), and place the
   reviewed `profile.yaml` on the VM with owner-only permissions.
5. **Install the WeasyPrint system libraries** on the VM the same way CI does
   (`apt-get install libpango-1.0-0 libpangocairo-1.0-0 libgdk-pixbuf2.0-0
   libffi-dev` on Debian/Ubuntu).

Moving from scheduled batch runs to a continuous loop changes only *when* work is
picked up, not *what* happens on the normal path, because dedupe keys, the Telegram
offset, and send claims are all persisted in Postgres. The loop is **not** fully
resume-safe across a crash mid-action: review the limitations below before treating a
restart as free of lost or duplicated work. The assisted-form flow remains
local-only regardless of where the pipeline runs.

## Known durability limitations

Idempotency holds on the happy path, but the Phase 5/6 reviews accepted a small set
of crash/retry windows as deferred technical debt (a durable transactional outbox),
recorded in [ADR-011](decisions.md#adr-011-telegram-processing-is-idempotent-by-update_id-full-transactional-outbox-is-deferred)
and [ADR-012](decisions.md#adr-012-phase-6-send-path-durability--one-bug-fixed-two-transport-atomicity-items-deferred).
An operator should know that a crash or retry is **not** always free of lost or
duplicated externally visible work:

- **Lost owner action (P5-013).** A language-choice/adjust can be checkpointed as
  processed before its effect completes, so an interrupted action may not resume.
- **Duplicated proposal artifacts (P5-014).** A partial proposal delivery is not
  resumed per-part, so a retry can resend attachments or re-upload to Drive.
- **Daily limit exceeded across runs (P5-015).** `DAILY_APPLICATION_LIMIT` counts
  within a single run and is not a durable per-day reservation, so it can be exceeded
  across the 2-hour cron. Shorter cadences widen this gap.
- **Possible email resend (P6-004).** An ambiguous mail-transport failure
  (timeout/reset after the provider may have accepted) releases the send claim, so a
  later Enviar could resend.
- **Possible duplicate manual application (P6-006).** Manual recording and the job
  status change are not one atomic unit with a uniqueness constraint, so a
  status-write failure after the insert could let a later "Ya apliqué" duplicate it.

The happy-path send is still guarded against double-send by the eligible-state check
and the email claim. These windows will be closed by the durable-outbox iteration.
