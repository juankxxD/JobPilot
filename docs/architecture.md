# Architecture

See `.kiro/specs/jobpilot/design.md` for the authoritative design. This file holds
the operator-facing summary; it is expanded with a full diagram in Phase 9.

JobPilot is a batch pipeline, not a long-running service. Each run (GitHub Actions
cron or manual) executes the full cycle, processes pending Telegram responses, and
exits. State lives in Postgres (Neon) and in `profile.yaml`.

```
collect -> dedupe -> prefilter -> analyze -> score -> select ->
render -> upload -> propose -> process_telegram_updates -> follow_up
```

Interfaces (Protocols) decouple the pipeline from concrete services:
`LlmClient`, `JobSource`, `FileStorage`, `Notifier`, `Mailer`, `JobRepository`,
`FormFiller`.
