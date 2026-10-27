# Deployment

The pipeline runs on GitHub Actions (cron every 2 hours, configurable) and via
`workflow_dispatch`. Phase 8 (assisted form filling) runs only on your local PC,
never in CI. This document is expanded in Phase 9.

## GitHub Actions secrets

Add each `.env` value as a repository secret:

`GROQ_API_KEY`, `GROQ_MODEL`, `DATABASE_URL`, `TELEGRAM_BOT_TOKEN`,
`TELEGRAM_OWNER_CHAT_ID`, `GOOGLE_SERVICE_ACCOUNT_JSON`, `DRIVE_FOLDER_ID`,
`MAIL_PROVIDER`, and the mail-provider credentials
(`GRAPH_CLIENT_ID` / `GRAPH_TENANT_ID` / `GRAPH_REFRESH_TOKEN` or the Gmail trio).

## Migrating to long polling on a VM

Each CI run uses Telegram `getUpdates` once and stores the last `update_id` in
`telegram_state`. To run continuously on a VM instead, keep the same
`channels/telegram.py` layer and replace the single `get_updates` call with a loop
that polls continuously. The business logic does not change.
