# Architecture

See `.kiro/specs/jobpilot/design.md` for the authoritative design. This file is the
operator-facing summary of how the pieces fit together.

JobPilot is a **batch pipeline, not a long-running service**. Each run (a GitHub
Actions cron tick or a manual invocation) executes the full cycle once, processes
any pending Telegram responses, records everything, and exits. All durable state
lives in Postgres (Neon) and in `profile.yaml`; nothing is held in memory between
runs. This is what makes the free-tier, serverless-cron deployment possible and
what makes each stage idempotent **on the normal path** — re-running does not
duplicate the work that completed. A few crash-mid-action windows are accepted as
deferred durability debt; see [Normal-path idempotency and its limits](#normal-path-idempotency-and-its-limits).

## The pipeline, stage by stage

```mermaid
flowchart LR
    subgraph Trigger
        CRON["GitHub Actions<br/>cron (every 2h)"]
        MAN["jobpilot run<br/>(manual)"]
    end

    CRON --> COLLECT
    MAN --> COLLECT

    subgraph Run["One pipeline run"]
        COLLECT["collect"] --> DEDUPE["dedupe<br/>(upsert)"]
        DEDUPE --> PREFILTER["prefilter<br/>(deterministic)"]
        PREFILTER --> ANALYZE["analyze"]
        ANALYZE --> SCORE["score"]
        SCORE --> GATE{"score >=<br/>MIN_SCORE?"}
        GATE -->|no| DISC["discarded"]
        GATE -->|yes| SELECT["select content"]
        SELECT --> RENDER["render<br/>CV + cover (PDF)"]
        RENDER --> UPLOAD["upload<br/>(Drive)"]
        UPLOAD --> PROPOSE["propose<br/>(Telegram)"]
        PROPOSE --> PROCESS["process_telegram_updates<br/>(approve / adjust / discard / send)"]
        PROCESS --> FOLLOW["follow_up<br/>(reminders, outcomes, weekly report)"]
    end
```

1. **collect** — each enabled `JobSource` returns normalized `RawJob`s. A source
   that fails is isolated and logged; the run continues with the others.
2. **dedupe** — `upsert_job` writes each offer idempotently, keyed on
   `(source, external_id)` and on `url`. Re-running never creates duplicates.
3. **prefilter** — deterministic rules from `config.yaml` (country/remote, required
   and excluded keywords, accepted languages, modality, min salary) run **before**
   any LLM call. Failures become `filtered_out`.
4. **analyze + score** — the LLM extracts structure (stack, seniority, modality,
   salary, language) and scores the offer 0–100 with configurable weights. Every
   prompt embeds the anti-invention rules; every returned id is code-validated.
5. **gate** — only `score >= MIN_SCORE` continues; the rest become `discarded`.
6. **select content** — the LLM picks which profile achievements/projects to
   highlight, by `id` only. Ids and the approved language are code-validated.
7. **render** — Jinja2 templates → WeasyPrint PDF. The CV is single-column ATS
   layout; the cover letter is validated at ≤ 200 words before rendering.
8. **upload** — the PDFs go to Google Drive; the web view link is stored.
9. **propose** — Telegram message with attachments and inline buttons.
10. **process_telegram_updates** — on the normal path each `update_id` is processed
    once (offset stored in `telegram_state`), driving status transitions and, on an
    explicit Enviar, the send (email or manual) with the application recorded. The
    offset is a checkpoint, not a transactional outbox: a crash between advancing the
    offset and completing an action is an accepted window (ADR-011/012), not an
    exactly-once guarantee.
11. **follow_up** — due reminders, outcome transitions from `/entrevista` and
    `/rechazo`, and the weekly report on the configured weekday.

## The seven interfaces

Everything external is reached through a Protocol (`src/jobpilot/interfaces.py`), so
every service is swappable and every test uses an in-memory fake instead of the
network:

| Protocol | Concrete implementation(s) | Fake in tests |
|----------|----------------------------|---------------|
| `LlmClient` | `GroqClient` → `OllamaClient` via `FallbackLlmClient` | `ScriptedFakeLlmClient` |
| `JobSource` | GetOnBoard, RemoteOK, Remotive, We Work Remotely (+ optional Himalayas, Adzuna, Gmail alerts) | `FakeJobSource` |
| `FileStorage` | `DriveFileStorage` (R2-ready) | `FakeFileStorage` |
| `Notifier` | `TelegramNotifier` | `FakeNotifier` |
| `Mailer` | `GraphMailer` (default), `GmailMailer` | `FakeMailer` |
| `JobRepository` | `NeonJobRepository` (psycopg 3) | `FakeJobRepository` |
| `FormFiller` | Greenhouse, Lever (Playwright, **local only**) | `FakeFormFiller` |

```mermaid
flowchart TB
    PIPE["pipeline/run.py<br/>(orchestration)"]
    PIPE --> LLM["LlmClient"]
    PIPE --> SRC["JobSource"]
    PIPE --> FS["FileStorage"]
    PIPE --> NOT["Notifier"]
    PIPE --> MAIL["Mailer"]
    PIPE --> REPO["JobRepository"]
    FORM["FormFiller<br/>(local apply-form only)"] --> REPO
    FORM --> NOT
    FORM --> LLM
```

## Job lifecycle

`new → filtered_out` | `new → scored → discarded` | `scored → proposed →
approved → applied`, with `error` reachable from `new`/`scored` when LLM output
fails validation after one retry. Once `applied`, the application's `outcome`
moves `pending → interview | rejected | offer | no_response` via follow-up.

## Why batch + idempotent

- **Cost**: the whole thing fits in free tiers (GitHub Actions minutes, Neon,
  Groq, Drive) because there is no always-on process.
- **Safety (normal path)**: on the happy path a crash mid-run is recoverable — the
  next run re-derives state from the database. Dedupe keys, the Telegram offset, and
  send claims keep re-processing from duplicating completed work.
- **Portability**: the same code runs continuously on a VM by wrapping the single
  `get_updates` call in a poll loop; no business logic changes. The loop is
  resume-safe on the normal path but not across every crash-mid-action window. See
  [deployment.md](deployment.md).

## Normal-path idempotency and its limits

Idempotency here means the *completed* work of a run is safe to re-run: dedupe keys
make collection repeatable, the Telegram offset skips already-processed updates, and
the email send claim blocks a double-send. It does **not** mean exactly-once
processing or that any crash is free of visible effects.

The Phase 5/6 reviews accepted a small set of crash/retry windows as deferred debt,
to be closed by a durable transactional outbox. They are recorded in
[ADR-011](decisions.md#adr-011-telegram-processing-is-idempotent-by-update_id-full-transactional-outbox-is-deferred)
and [ADR-012](decisions.md#adr-012-phase-6-send-path-durability--one-bug-fixed-two-transport-atomicity-items-deferred),
and summarized for operators under
[Known durability limitations](deployment.md#known-durability-limitations):

- an interrupted language-choice/adjust can be checkpointed before its effect
  completes and never resume (P5-013);
- a partial proposal delivery can resend attachments / re-upload on retry (P5-014);
- `DAILY_APPLICATION_LIMIT` is per-run, so it can be exceeded across the cron (P5-015);
- an ambiguous mail-transport failure can resend on a later Enviar (P6-004);
- a partial DB failure can let a later manual "Ya apliqué" duplicate an application
  (P6-006).

Treat these as the boundary of the idempotency guarantee until the outbox work lands.
