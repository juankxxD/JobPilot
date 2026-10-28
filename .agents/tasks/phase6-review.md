# Phase 6 review

Verdict: **CHANGES_REQUESTED**

Reviewed `f149d8f015286ed9b44ebc32b78416e8f6a93716` and follow-up fix `069861c22c24043a318bd951c147329e13f5714d` against Phase 6 of `.agents/tasks/plan.md`, R5/R10–R12/R16 in `.kiro/specs/jobpilot/requirements.md`, the design, and ADR-003/ADR-011. The latest commit records the required gate: Ruff check and format passed, mypy found no issues in 100 source files, and pytest reported 235 passed and 9 WeasyPrint-gated skips. I did not rerun the suites. A narrow source inspection of installed MSAL 1.39.0 confirmed its device-flow response path can expose the refresh token and that the corrected `Mail.Send` scope is valid. P6-001 and P6-003 are resolved. The following blockers remain.

## Blocking findings

### P6-002 — Recollection erases an analysis-derived contact email before approval is processed

`score_new_jobs` now copies `analysis.contact_email` onto the job at `src/jobpilot/pipeline/run.py:217`, but both Neon recollection paths overwrite the stored value with the source value at `src/jobpilot/storage/neon.py:205` and `:258`. The concrete sources commonly return `None`. Production runs collect first and process Telegram last (`src/jobpilot/pipeline/run.py:1422` and `:1436`), while only `new` jobs are rescored (`:192`). Therefore, when the owner presses Enviar between scheduled runs, the next collection can replace the proposed job's analysed address with `NULL`; the guard at `:893` then routes it to manual mode.

**Correction:** preserve the existing address when a recollected source address is null, while allowing a new non-null source address to replace it, in both upsert paths and the fake. Add a score → recollect with null address → Enviar regression test proving the email provider is used.

### P6-004 — An ambiguous provider transport failure releases the send claim and permits a duplicate

The Graph call at `src/jobpilot/mail/graph.py:148` and Gmail execution at `src/jobpilot/mail/gmail.py:135` can raise after the request has left the process, such as a read timeout or connection reset after provider acceptance. `_send_by_email` catches every exception from `mailer.send` at `src/jobpilot/pipeline/run.py:967` and unconditionally deletes the durable claim at `:970`, asserting that nothing was sent. A later Enviar can then claim again and send a duplicate, so the fix does not establish the required no-double-send guarantee for a reachable transport outcome.

**Correction:** distinguish a definitive pre-dispatch/provider rejection from an unknown delivery outcome. Release the claim only for a typed failure that proves the provider did not accept the message; retain an `in_progress`/ambiguous claim for transport failures, or conservatively retain it for every unclassified exception. Add failure injection for an exception raised after simulated provider acceptance and prove a second Enviar cannot call the provider.

### P6-005 — The Phase 6 migration reuses version 002, so deployed databases never get a complete schema

Both `migrations/002_email_send_claims.sql:19` and `migrations/002_status_changed_at.sql:10` have version `002`. The runner loads applied versions once and treats only the numeric prefix as identity (`src/jobpilot/storage/migrate.py:98-113`). On a database that already ran Phase 5, both files are skipped and `email_send_claims` is absent, causing Enviar to fail when it calls `claim_email_send`. On a fresh database, the email migration is applied first, then the status migration's duplicate `schema_migrations.version` insert fails and rolls back that migration; a rerun skips both as version 002.

**Correction:** rename the new email-claim migration to the next unique version, `003_email_send_claims.sql`. Add a project-migration test that rejects duplicate discovered versions and verifies both `status_changed_at` and `email_send_claims` migrations are independently applied.

### P6-006 — Manual application recording and status change are not atomic or uniquely idempotent

`_handle_mark_applied` inserts the manual application at `src/jobpilot/pipeline/run.py:1071-1079` and only then changes the job to `applied` at `:1080`. `NeonJobRepository.create_application` commits independently at `src/jobpilot/storage/neon.py:322-326`, and the applications table has no job/channel uniqueness constraint (`migrations/001_init.sql:47-64`). If the status write fails after the insert commits, the job remains `proposed` or `approved`, both eligible at `pipeline/run.py:812`; another Ya apliqué callback inserts a duplicate application. This corrupts application history and inflates daily-limit/follow-up data.

**Correction:** finalize a manual application and the job's `applied` status in one repository transaction, backed by an appropriate per-job idempotency constraint or idempotent insert. Add failure injection between the insert and status update and prove a later callback leaves exactly one application.
