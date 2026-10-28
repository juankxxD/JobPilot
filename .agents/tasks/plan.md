# JobPilot — Implementation Plan (Phases 2–9)

Worktree (ABSOLUTE): `/Users/juanhoyos/proyectos/jobpilot/.worktrees/phases-2-9`
Branch: `feat/phases-2-9`. Phases 0 and 1 are DONE and committed on `main`; do NOT redo them.

ALWAYS use the absolute worktree path. Relative paths resolve into the parent workspace by mistake.

## Project facts discovered during exploration (honor exactly)

- Python 3.12 (`requires-python = ">=3.12,<3.13"`), managed with uv. A `.venv` already
  exists in the worktree — activate it: `source .venv/bin/activate`.
- Build/quality gate (run ALL three green before every phase commit):
  - `ruff check src tests scripts` and `ruff format --check src tests scripts`
  - `mypy src` (strict; `[tool.mypy] strict = true`, pydantic plugin)
  - `pytest -q`
- Base is green today: `ruff` passes, `mypy` reports "no issues found in 32 source files",
  `pytest` 42 passed.
- When dependencies change: `uv pip compile pyproject.toml -o requirements.txt` (ADR-001).
  Any mypy "ignore_missing_imports" override for a newly-imported untyped lib goes in
  `pyproject.toml` under `[[tool.mypy.overrides]]` (pattern already present for
  weasyprint/feedparser/lingua/google/msal).
- Pydantic v2 everywhere. Domain models live in `src/jobpilot/models.py`; config in
  `src/jobpilot/config.py` (`Settings` from env, `AppConfig` from `config.yaml`).
- The 7 Protocols are in `src/jobpilot/interfaces.py`. Match these signatures exactly:
  - `LlmClient.complete_json(prompt, schema, *, max_retries=1) -> SchemaT`
  - `JobSource`: attrs `name: str`, `enabled: bool`; `fetch() -> Iterable[RawJob]`
  - `FileStorage.upload(path: Path, name: str) -> StoredFile`
  - `Notifier.send_message(text, buttons=None, attachments=None)`, `get_updates(offset=None) -> list[Update]`
  - `Mailer.send(to, subject, body, attachments)`
  - `JobRepository`: `upsert_job(RawJob) -> Job`, `get_job(id) -> Job | None`,
    `set_status(job_id, status)`, `save_job(Job) -> Job`, `create_application(app) -> Application`,
    `get_state(key) -> str | None`, `set_state(key, value)`
  - `FormFiller.matches(url) -> bool`, `fill(url, ctx: FillContext) -> FillResult`
  - `Button`, `Update`, `FillContext`, `FillResult` are defined in `interfaces.py`.
- Tests use fakes implementing the Protocols in `tests/fakes.py` (currently only
  `FakeLlmClient`, which holds ONE response). Structured logging via
  `jobpilot.logging.get_logger(__name__)`. External services are ALWAYS mocked/faked —
  no live service may be contacted; the owner supplies real credentials only at the end.
- LLM: default model `openai/gpt-oss-120b` (ADR-005). NEVER reintroduce
  `llama-3.3-70b-versatile`. `GroqClient`/`OllamaClient`/`FallbackLlmClient` already exist
  in `src/jobpilot/llm/client.py`; `_complete_json_with_retry` does one schema retry.
- Anti-invention: every LLM prompt embeds `ANTI_INVENTION_RULES` (`src/jobpilot/llm/prompts.py`);
  every LLM-returned id is code-validated via `src/jobpilot/profile/validation.py`
  (`validate_selection`, `find_unknown_technologies`). On validation failure: retry once,
  else mark the job `error`.
- Idempotency is mandatory: dedupe jobs by `(source, external_id)` AND by `url`;
  process each Telegram `update_id` once (store `last_update_id` in `telegram_state`).
- Human-in-the-loop is mandatory: nothing is sent without an explicit Telegram button.
- Language/English rules: code, identifiers, comments, commit messages in ENGLISH;
  Telegram user-facing messages in SPANISH; CV/cover in the offer language.
- `JobStatus` enum (in models.py) already has: new, filtered_out, scored, proposed,
  approved, applied, discarded, error. `ApplicationChannel`: email, form_assisted, portal,
  manual. `ApplicationOutcome`: pending, interview, rejected, offer, no_response.
- Each phase is its own commit with a clear English message. Run the full gate before each
  commit. WeasyPrint-native-lib (pango/cairo) PDF tests may SKIP locally but must run in CI.

## Assumptions (made where the spec was ambiguous; consistent with ADRs)

- A1. `tests/fakes.py` will be extended with fakes for the new Protocols
  (`FakeJobRepository`, `FakeNotifier`, `FakeMailer`, `FakeFileStorage`, `FakeJobSource`,
  `FakeFormFiller`) and a `ScriptedFakeLlmClient` that returns a queue of responses by
  schema type, because several phases need multiple sequential LLM outputs. Added in
  Phase 3 (first need) and reused later.
- A2. Neon connection retry uses exponential backoff on `psycopg.OperationalError`/timeout
  (design: "retry N veces"); default N=5. Unit tests inject a fake connection factory —
  no real Postgres is contacted.
- A3. `JobRepository` methods beyond the Protocol (e.g. `list_proposed`, lookups by
  company+role within a window, `pending_without_outcome`) are added to the concrete
  `NeonJobRepository` and exercised via a fake; the Protocol stays the minimal contract.
- A4. Language decision that stays ambiguous after lingua + LLM leaves the job `proposed`
  awaiting a Telegram language button; `DEFAULT_LANGUAGE` only forces a language when
  `language.use_default_on_timeout` is true (ADR-007/008).
- A5. `confirm_language` runs only when lingua confidence < `language.min_confidence`.
- A6. Phase 8 (Playwright) is LOCAL ONLY and is excluded from CI; its tests mock Playwright
  entirely so they run (and pass) in CI without a browser.

---

# Implementation Plan

## Phase 2 — Collection
Spec "done": a single run fills `jobs` with no duplicates. Commit message e.g.
`Phase 2: collection — migrations, Neon repository, fetchers, prefilter, pipeline wiring`.

- [ ] 1. Add `migrations/001_init.sql`: create `schema_migrations(version text primary key, applied_at timestamptz default now())`, and tables `jobs`, `applications`, `telegram_state` per design R12. `jobs` columns: id (bigserial pk), source, external_id, url, apply_url, title, company, location, remote bool, salary_text, contact_email, language, description, analysis jsonb, score int, score_reason text, missing_requirements jsonb, status text, found_at timestamptz; constraints `unique(source, external_id)` and `unique(url)`; a CHECK restricting status to new/filtered_out/scored/proposed/approved/applied/discarded/error. `applications`: id, job_id fk -> jobs(id), language, cv_variant, cv_url, cover_letter_url, channel, applied_at, follow_up_at, outcome, notes, created_at default now(). `telegram_state(key text pk, value text)`.
      Files: `migrations/001_init.sql`
      Verify: `pytest -q` (SQL parsed by the migration-runner test in item 3); file exists and is valid SQL text.

- [ ] 2. Add `src/jobpilot/storage/migrate.py`: a runner that connects via psycopg 3, ensures `schema_migrations` exists, reads `migrations/NNN_*.sql` in sorted order, applies each not-yet-recorded file inside a transaction, and records its version. Expose `run_migrations(conn_factory, migrations_dir) -> list[str]` (returns applied versions) so tests inject a fake/in-memory connection. Connection uses the retry helper from item 4.
      Files: `src/jobpilot/storage/migrate.py`
      Verify: `pytest -q tests/test_migrate.py` — applying twice is a no-op the second run (idempotent), applied versions recorded.

- [ ] 3. Wire `migrate` CLI command: replace the stub in `src/jobpilot/cli.py` `migrate()` to build a connection from `settings.database_url` and call `run_migrations`, printing applied versions (English). Fail clearly if `DATABASE_URL` is unset.
      Files: `src/jobpilot/cli.py`
      Verify: `pytest -q tests/test_cli.py` — `migrate` with no `DATABASE_URL` exits non-zero with a clear message (CliRunner, no DB contacted).

- [ ] 4. Add `src/jobpilot/storage/neon.py`: `NeonJobRepository` implementing `JobRepository` with psycopg 3. Include a module-level `connect_with_retry(dsn, *, retries=5, factory=psycopg.connect)` doing exponential backoff on `OperationalError`/timeout (Neon wake-up, R12.1). Implement `upsert_job` as an idempotent INSERT ... ON CONFLICT (source, external_id) DO UPDATE returning the row (also respecting the url unique constraint), `get_job`, `set_status`, `save_job` (persist analysis/score/status), `create_application`, `get_state`/`set_state` (UPSERT into telegram_state). Add query helpers per assumption A3 (`list_proposed`, `recent_company_role(company, role, within_days)`, `count_applications_on(date)`, `applications_without_outcome(before)`). All SQL parameterized. Use `get_logger(__name__)`.
      Files: `src/jobpilot/storage/neon.py`
      Verify: `pytest -q tests/test_neon_repo.py` — retry invoked on simulated `OperationalError` then success; upsert called twice yields one logical row and returns an id (fake connection/cursor, no real DB).

- [ ] 5. Add `src/jobpilot/fetchers/base.py`: `JobSource` ABC (attrs `name`, `enabled`, method `fetch() -> Iterable[RawJob]`) plus a registry: a `register`/`iter_enabled(config)` mechanism mapping source name -> factory, gated by `AppConfig.source_enabled(name)`. Add a `normalize(...) -> RawJob` helper that builds a `RawJob` (models.py) with trimmed/validated fields and a stable `external_id`.
      Files: `src/jobpilot/fetchers/base.py`
      Verify: `pytest -q tests/test_fetchers_base.py` — registry returns only enabled sources for a given `AppConfig`; `normalize` produces a valid `RawJob`.

- [ ] 6. Add the four initial fetchers, each subclassing the ABC and using `httpx` (or `feedparser` for RSS), each isolating its own errors (never raising out of the registry-driven collect): `src/jobpilot/fetchers/getonboard.py` (GetOnBoard API JSON), `remoteok.py` (RemoteOK API JSON), `remotive.py` (Remotive API JSON), `weworkremotely.py` (RSS via `feedparser`). Each maps its payload to `RawJob` via `normalize`. No network in tests — inject the HTTP client / feed text.
      Files: `src/jobpilot/fetchers/getonboard.py`, `src/jobpilot/fetchers/remoteok.py`, `src/jobpilot/fetchers/remotive.py`, `src/jobpilot/fetchers/weworkremotely.py`
      Verify: `pytest -q tests/test_fetchers_initial.py` — each fetcher parses a saved fixture payload into `RawJob`s (fixtures under `tests/fixtures/`); HTTP is faked.

- [ ] 7. Add the feature-flagged optional fetchers gated by `config.yaml` `sources` flags: `src/jobpilot/fetchers/himalayas.py` (API), `adzuna.py` (API, uses `ADZUNA_APP_ID`/`ADZUNA_APP_KEY`), `gmail_alerts.py` (reads Gmail alert emails ONLY — LinkedIn/Indeed/Google Jobs alerts; NO scraping, NO LinkedIn/Indeed automation). Default disabled in `config.yaml` (already so). Register them.
      Files: `src/jobpilot/fetchers/himalayas.py`, `src/jobpilot/fetchers/adzuna.py`, `src/jobpilot/fetchers/gmail_alerts.py`
      Verify: `pytest -q tests/test_fetchers_optional.py` — each disabled by default (not returned by the registry); when enabled, parses a fixture payload; gmail_alerts reads a faked message list, never scrapes.

- [ ] 8. Add `src/jobpilot/filters/prefilter.py`: deterministic `prefilter(raw: RawJob, config: PrefilterConfig, *, language: str | None) -> bool` applying country/remote, required & excluded keywords, accepted languages, modality, and min salary (only when the offer declares one). Pure function, no LLM.
      Files: `src/jobpilot/filters/prefilter.py`
      Verify: `pytest -q tests/test_prefilter.py` — edge cases: remote passes without matching country; excluded keyword drops; missing required keyword drops; salary below min drops only when declared; language not in accepted drops.

- [ ] 9. Add `src/jobpilot/pipeline/run.py` (partial, Phase 2 scope): `collect_and_persist(sources, repo, config)` that iterates enabled sources in try/except (isolating per-source failures and logging them), normalizes, dedupes via `upsert_job`, and runs the prefilter, setting `filtered_out` for failures and leaving others `new`. Return a small summary (counts). Later phases extend this module; keep the function composable.
      Files: `src/jobpilot/pipeline/run.py`
      Verify: `pytest -q tests/test_pipeline_collect.py` — one failing fetcher does not stop others; running collect twice does not create duplicate jobs (fake repo asserts upsert idempotency); prefiltered jobs get `filtered_out` (all via fakes).

- [ ] 10. Extend `tests/fakes.py` with `FakeJobRepository` (in-memory, enforces `(source,external_id)` and `url` dedupe, records status changes and state) and `FakeJobSource` (yields canned `RawJob`s or raises). Add fixtures for raw source payloads under `tests/fixtures/`.
      Files: `tests/fakes.py`, `tests/fixtures/*` (source payloads)
      Verify: full gate — `ruff check src tests scripts && mypy src && pytest -q` all green. Then commit Phase 2.

## Phase 3 — Analysis & scoring
Spec "done": valid LLM outputs and coherent scores over es/en fixtures. Commit e.g.
`Phase 3: analysis & scoring — schemas, prompts, fallback hardening, language, scorer`.

- [ ] 11. Extend `src/jobpilot/llm/schemas.py` with the analysis/scoring schemas IF they need to differ from `models.py` (`JobAnalysis`, `JobScore` already exist in models.py and may be reused directly as LLM schemas). Add a `ConfirmLanguage` schema (`language: Language`, `confidence: float`). Keep `CvImportResult` untouched.
      Files: `src/jobpilot/llm/schemas.py`
      Verify: `pytest -q tests/test_llm_schemas.py` — schemas validate sample JSON; invalid JSON rejected.

- [ ] 12. Extend `src/jobpilot/llm/prompts.py` with `build_analyze_job_prompt`, `build_score_job_prompt(profile, analysis, weights)`, and `build_confirm_language_prompt`, each embedding `ANTI_INVENTION_RULES`. Score prompt must require `met_requirements` to cite an `achievement_id` from the profile.
      Files: `src/jobpilot/llm/prompts.py`
      Verify: `pytest -q tests/test_prompts.py` — each prompt contains the anti-invention block and the offer/profile data; `achievement_id` instruction present.

- [ ] 13. Harden the LLM fallback path in `src/jobpilot/llm/client.py`: `FallbackLlmClient` must fall back to Ollama specifically on quota/429 and network errors (classify Groq `APIStatusError`/429 and connection errors) as well as schema failure after the retry; log the reason. Keep `complete_json` signature. Do not reintroduce `llama-3.3-70b-versatile`.
      Files: `src/jobpilot/llm/client.py`
      Verify: `pytest -q tests/test_llm_client.py` — a primary raising a simulated 429 triggers the Ollama fallback; a primary returning bad-then-good JSON succeeds via the single retry (Groq/Ollama calls faked, no network).

- [ ] 14. Add `src/jobpilot/language.py`: `detect_language(text, config.language, llm)` using `lingua` to detect; when confidence < `language.min_confidence`, call the LLM `confirm_language` prompt; return a `Language` or `None` (ambiguous) signalling the Telegram button flow. Pure except the optional LLM call (injected).
      Files: `src/jobpilot/language.py`
      Verify: `pytest -q tests/test_language.py` — clear es text -> es without LLM; clear en -> en; low-confidence text calls the faked LLM; still-ambiguous -> None.

- [ ] 15. Add `src/jobpilot/scoring/scorer.py`: `analyze_and_score(job, profile, llm, config) -> (JobAnalysis, JobScore)` that calls analyze then score, validates `met_requirements` achievement ids via `profile/validation.py` (retry once on invalid id, else raise a scoring error so the caller sets job `error`). Apply the configurable weighted model from `config.yaml` (stack 40 / seniority+years 20 / modality+timezone 15 / language 10 / salary 10 / other 5) and the `MIN_SCORE` threshold check as a helper `passes_threshold(score, settings.min_score)`.
      Files: `src/jobpilot/scoring/scorer.py`
      Verify: `pytest -q tests/test_scorer.py` — weighted score computed from a fixture analysis; id-invalid `met_requirements` triggers one retry then error; threshold gating correct.

- [ ] 16. Add es/en fixtures (`tests/fixtures/jobs_es.json`, `jobs_en.json`, and example LLM responses) and a `ScriptedFakeLlmClient` in `tests/fakes.py` (returns queued responses matched by schema type; asserts prompt contains anti-invention). Wire analyze+score into `pipeline/run.py` (jobs that are `new` and passed prefilter -> `scored`, below threshold flagged, `error` on unrecoverable LLM failure).
      Files: `tests/fixtures/jobs_es.json`, `tests/fixtures/jobs_en.json`, `tests/fixtures/llm_*.json`, `tests/fakes.py`, `src/jobpilot/pipeline/run.py`
      Verify: full gate green; `pytest -q tests/test_pipeline_score.py` covers es+en end-to-end with the scripted fake. Commit Phase 3.

## Phase 4 — Documents
Spec "done": ATS-readable CV in both languages + stored link. Commit e.g.
`Phase 4: documents — content selector, ATS templates, WeasyPrint render, Drive storage`.

- [ ] 17. Add `src/jobpilot/documents/selector.py`: `select_content(job, profile, llm) -> ContentSelection` using a new `build_select_content_prompt` (anti-invention embedded; add to `prompts.py`). Code-validate the returned ids AND approved-language via `validate_selection` from `profile/validation.py`; retry once on validation failure, else raise so caller marks `error`.
      Files: `src/jobpilot/documents/selector.py`, `src/jobpilot/llm/prompts.py`
      Verify: `pytest -q tests/test_selector.py` — nonexistent achievement id fails after one retry; unapproved translation rejected; valid selection returns a `ContentSelection`.

- [ ] 18. Add templates `src/jobpilot/documents/templates/cv_es.html`, `cv_en.html`, `cover_es.html`, `cover_en.html`, and `styles.css`: single column, no tables/images/icons, localized headers (Resumen/Summary, Experiencia/Experience, Educación/Education, Habilidades/Skills, Certificaciones/Certifications, Proyectos/Projects), common fonts, selectable text, one page default (max two). Jinja2 variables bind to profile + selection.
      Files: `src/jobpilot/documents/templates/cv_es.html`, `cv_en.html`, `cover_es.html`, `cover_en.html`, `styles.css`
      Verify: `pytest -q tests/test_templates.py` — Jinja2 renders each template to HTML containing the localized headers for its language (no WeasyPrint needed here).

- [ ] 19. Add `src/jobpilot/documents/render.py`: `render_cv(...)` and `render_cover_letter(...)` using jinja2 + WeasyPrint (HTML->PDF). Code-validate the cover letter is <= 200 words BEFORE rendering (raise on violation). Build the output filename `Nombre_Apellido_Empresa_Cargo_ES.pdf` / `_EN.pdf` via a safe-slug helper. Import WeasyPrint lazily so the module imports without native libs.
      Files: `src/jobpilot/documents/render.py`
      Verify: `pytest -q tests/test_render.py` — 201-word cover raises; filename slug correct for accented/spaced inputs; a render smoke test is marked to SKIP gracefully when WeasyPrint native libs (pango/cairo) are missing locally but runs in CI.

- [ ] 20. Add the ATS read-order PDF test for BOTH languages: render a CV to PDF, extract text with `pypdf`, assert localized headers appear in the expected order. Use a pytest skip marker (e.g. `skip_without_weasyprint`) that detects missing pango/cairo and skips locally while running in CI.
      Files: `tests/test_pdf_ats.py`, `tests/conftest.py` (add the skip marker/helper)
      Verify: `pytest -q tests/test_pdf_ats.py` — passes in CI; skips cleanly locally if native libs absent (confirm with a local run showing SKIP, not FAIL).

- [ ] 21. Add `src/jobpilot/storage/drive.py`: `DriveFileStorage` implementing `FileStorage` (`upload(path, name) -> StoredFile`) via a Google service account, storing and returning `web_view_link`. Keep the implementation behind the `FileStorage` Protocol so an R2 backend can replace it without touching callers. Build the Drive client lazily; inject it in tests.
      Files: `src/jobpilot/storage/drive.py`
      Verify: `pytest -q tests/test_drive.py` — upload is mocked (no real Drive); returns a `StoredFile` with `web_view_link`; folder id read from settings.

- [ ] 22. Extend `tests/fakes.py` with `FakeFileStorage` (records uploads, returns a canned `StoredFile`). Wire select -> render -> upload into `pipeline/run.py` (store the Drive `web_view_link` for later persistence on the application).
      Files: `tests/fakes.py`, `src/jobpilot/pipeline/run.py`
      Verify: full gate green; commit Phase 4.

## Phase 5 — Telegram
Spec "done": full flow to `proposed` + processing approvals with guards. Commit e.g.
`Phase 5: telegram — notifier, inline keyboard, commands, pipeline orchestration & guards`.

- [ ] 23. Add `src/jobpilot/channels/telegram.py`: `TelegramNotifier` implementing `Notifier`. `send_proposal(proposal)` sends title/company/score/language/summary/missing/offer-link with CV+cover attachments and an inline keyboard (callback_data encodes action+job_id): Enviar / Ajustar / Descartar / Ya apliqué / Español / Inglés. `get_updates(offset)` wraps Telegram `getUpdates`, normalizes to `Update`, and ignores any chat != `TELEGRAM_OWNER_CHAT_ID`. All user-facing text in SPANISH. HTTP injected for tests.
      Files: `src/jobpilot/channels/telegram.py`
      Verify: `pytest -q tests/test_telegram.py` — proposal payload includes the six buttons and attachments; updates from a non-owner chat are dropped; owner updates parsed (HTTP faked).

- [ ] 24. Implement command + callback handling in `channels/telegram.py` (or a `handlers` helper): parse `/pendientes`, `/estado`, `/entrevista id`, `/rechazo id`, `/buscar`, and callback actions. Dispatch returns structured intents for the pipeline to act on (keeps I/O testable). Spanish responses.
      Files: `src/jobpilot/channels/telegram.py`
      Verify: `pytest -q tests/test_telegram_commands.py` — each command and callback parses to the expected intent; unknown input handled gracefully.

- [ ] 25. Implement the ambiguous-language flow and Ajustar flow: when language is `None`, send the Español/Inglés/Descartar prompt and leave the job `proposed` awaiting the button (ADR-008); on language choice, proceed to document generation; Ajustar captures free-text and triggers document regeneration with that instruction. Model this as intents consumed in `pipeline/run.py`.
      Files: `src/jobpilot/channels/telegram.py`, `src/jobpilot/pipeline/run.py`
      Verify: `pytest -q tests/test_language_flow.py` — ambiguous language keeps job `proposed` and sends the 3-button prompt; choosing Español sets language and advances; Ajustar re-renders.

- [ ] 26. Complete `src/jobpilot/pipeline/run.py` orchestration to `proposed` plus `process_telegram_updates(repo, notifier, ...)`: read `last_update_id` from `telegram_state`, process each update exactly once, persist new `last_update_id`. Enforce `DAILY_APPLICATION_LIMIT` (default 5) and company+role dedupe within `proposals.dedupe_company_role_days`. Drive status transitions (scored -> proposed -> approved/discarded).
      Files: `src/jobpilot/pipeline/run.py`
      Verify: `pytest -q tests/test_pipeline_telegram.py` — reprocessing the same `update_id` is a no-op; daily limit and company+role guards block extra proposals; status transitions correct.

- [ ] 27. Extend `tests/fakes.py` with `FakeNotifier` (records sent proposals/messages, returns scripted updates). Full gate green; commit Phase 5.
      Files: `tests/fakes.py`
      Verify: `ruff check src tests scripts && mypy src && pytest -q` all green.

## Phase 6 — Sending
Spec "done": one test application recorded end-to-end. Commit e.g.
`Phase 6: sending — Mailer abstraction, Graph & Gmail, post-approval send & recording`.

- [ ] 28. Add `src/jobpilot/mail/base.py`: `Mailer` ABC (`send(to, subject, body, attachments)`) and a `build_mailer(settings) -> Mailer` selecting by `MAIL_PROVIDER` (outlook default | gmail). Document (disabled) SMTP+password only in a comment/docstring; do not implement it.
      Files: `src/jobpilot/mail/base.py`
      Verify: `pytest -q tests/test_mailer_select.py` — provider selection returns the right class for each `MAIL_PROVIDER` (constructors faked).

- [ ] 29. Add `src/jobpilot/mail/graph.py`: Graph `sendMail` with delegated `Mail.Send`, `saveToSentItems=true`, OAuth via msal using a stored refresh token from settings. Add the `jobpilot auth-mail` device-code flow in `cli.py` that obtains and prints the refresh token to store as a secret.
      Files: `src/jobpilot/mail/graph.py`, `src/jobpilot/cli.py`
      Verify: `pytest -q tests/test_graph_mail.py` — `send` posts the expected Graph payload with `saveToSentItems` true and the attachment (httpx + msal faked); `auth-mail` wired (CliRunner, msal device flow faked, no network).

- [ ] 30. Add `src/jobpilot/mail/gmail.py`: Gmail API send with the CV attached as PDF (OAuth refresh token from settings, client built lazily and injectable).
      Files: `src/jobpilot/mail/gmail.py`
      Verify: `pytest -q tests/test_gmail_mail.py` — builds a MIME message with the PDF attachment and calls the faked Gmail client.

- [ ] 31. Implement post-approval send in `pipeline/run.py`: on Enviar, if `contact_email` present send via the active Mailer (channel `email`); else manual mode (send link + documents + "Ya apliqué" button, channel `manual`). Record an `Application` with applied_at timestamp, channel, language, cv_variant, cv_url, cover_letter_url via `create_application`. Ensure no double-send (idempotent on job status `applied`).
      Files: `src/jobpilot/pipeline/run.py`
      Verify: `pytest -q tests/test_send_flow.py` — email path records channel `email`; no-contact path records `manual`; a second Enviar does not resend; application fields recorded correctly.

- [ ] 32. Extend `tests/fakes.py` with `FakeMailer` (records sends). Full gate green; commit Phase 6.
      Files: `tests/fakes.py`
      Verify: `ruff check src tests scripts && mypy src && pytest -q` all green.

## Phase 7 — Follow-up
Spec "done": reminders, outcome commands, weekly report working. Commit e.g.
`Phase 7: follow-up — reminders, outcome transitions, weekly Telegram report`.

- [ ] 33. Add `src/jobpilot/pipeline/follow_up.py`: compute `follow_up_at = applied_at + follow_up.reminder_after_days` (default 7) on creation; a `send_due_reminders(repo, notifier, now)` that reminds for applications with no outcome past their `follow_up_at`. Outcome changes via `/entrevista id` (-> interview) and `/rechazo id` (-> rejected), wired through the Telegram intents from Phase 5. Weekly report via Telegram on `follow_up.weekly_report_weekday`.
      Files: `src/jobpilot/pipeline/follow_up.py`, `src/jobpilot/pipeline/run.py` (invoke follow-up at end of a run)
      Verify: `pytest -q tests/test_follow_up.py` — `follow_up_at` computed correctly; due reminder sent once; `/entrevista`/`/rechazo` transition outcomes; weekly report generated only on the configured weekday (clock injected).

- [ ] 34. Full gate green; commit Phase 7.
      Files: (none new)
      Verify: `ruff check src tests scripts && mypy src && pytest -q` all green.

## Phase 8 — Assisted forms (LOCAL only, Playwright)
Spec "done": a real form filled up to the pre-submit step with a Telegram screenshot.
Tests MOCK Playwright (no real browser in CI). Commit e.g.
`Phase 8: assisted forms — FormFiller, Greenhouse/Lever, sensitive-question guard, safe abort`.

- [ ] 35. Add `src/jobpilot/forms/base.py`: `FormFiller` ABC (`matches(url) -> bool`, `fill(url, ctx: FillContext) -> FillResult`) and `detect_platform(url, dom) -> str | None` detecting greenhouse/lever by URL and DOM markers. Interface shaped so Ashby/Workable/SmartRecruiters can be added later.
      Files: `src/jobpilot/forms/base.py`
      Verify: `pytest -q tests/test_forms_detect.py` — greenhouse and lever URLs/DOM detected; unknown returns None.

- [ ] 36. Add `src/jobpilot/forms/greenhouse.py` and `lever.py`: Playwright-driven (visible browser configurable; LOCAL only), filling known fields from the profile, uploading CV+cover in the correct language, answering open questions with the LLM (profile+offer only), marking uncertain fields. Import Playwright lazily so the module imports without a browser. SENSITIVE questions (salary expectation, work authorization/visa, relocation, background/legal, demographics, years-of-experience in a specific tech) are NEVER answered by the LLM — only from `answers_bank`; if absent, leave blank and add to `unanswered_sensitive`.
      Files: `src/jobpilot/forms/greenhouse.py`, `src/jobpilot/forms/lever.py`
      Verify: `pytest -q tests/test_forms_fill.py` — with Playwright fully MOCKED: known fields mapped from profile; a sensitive question is never routed to the (faked) LLM and lands in `unanswered_sensitive`; CV/cover uploaded in the chosen language.

- [ ] 37. Implement pre-submit confirmation + safety in the form flow: before submit, send a Telegram screenshot + uncertain-field list with Enviar/Cancelar; only Enviar performs the final click and records the application (channel `form_assisted`). CAPTCHA/anti-bot -> stop + notify, never evade. Unexpected DOM -> safe abort + screenshot + notify, never submit incomplete. No LinkedIn Easy Apply, no Indeed.
      Files: `src/jobpilot/forms/greenhouse.py`, `src/jobpilot/forms/lever.py`, `src/jobpilot/pipeline/run.py` (local-only entrypoint)
      Verify: `pytest -q tests/test_forms_safety.py` — unexpected DOM yields `FillResult(aborted=True)` with a screenshot path and no submit; CAPTCHA detection stops and notifies; final click happens only on Enviar (Playwright + Notifier faked).

- [ ] 38. Extend `tests/fakes.py` with a `FakeFormFiller` and a Playwright page fake. Ensure the forms modules are excluded from any CI pipeline path (documented for Phase 9). Full gate green; commit Phase 8.
      Files: `tests/fakes.py`
      Verify: `ruff check src tests scripts && mypy src && pytest -q` all green (no real browser launched).

## Phase 9 — Docs & deployment
Spec "done": complete docs and a GitHub Actions deployment guide. Commit e.g.
`Phase 9: docs & deployment — CI workflow, architecture/deployment docs, README`.

- [ ] 39. Flesh out `.github/workflows/pipeline.yml`: `schedule` cron every 2h (configurable) + `workflow_dispatch`; set up Python 3.12 + uv (or `pip install -r requirements.txt`), install WeasyPrint system libs via apt (pango, cairo, etc.), run `jobpilot migrate` then `jobpilot run`, using GitHub Actions secrets for all credentials. Phase 8 (forms) MUST NOT run in CI — no `playwright` extra, no forms entrypoint invoked.
      Files: `.github/workflows/pipeline.yml`
      Verify: `python -c "import yaml,sys; yaml.safe_load(open('.github/workflows/pipeline.yml'))"` parses; manual read confirms cron, dispatch, apt libs, secrets, and absence of Phase 8.

- [ ] 40. Write `docs/architecture.md` (overview + a diagram of the collect->...->follow_up pipeline and the 7 interfaces), append any NEW ADRs to `docs/decisions.md` (e.g. an ADR for the scripted fake / repository-query additions if warranted), and write `docs/deployment.md` (required GitHub secrets list + how to migrate to long polling on a VM without changing logic). Update `README.md` (overview, setup, commands, per-phase manual-config checklist).
      Files: `docs/architecture.md`, `docs/decisions.md`, `docs/deployment.md`, `README.md`
      Verify: docs reference real commands/secrets consistent with `.env.example` and `cli.py`; `ruff check src tests scripts && mypy src && pytest -q` still green. Commit Phase 9.

## Final
- [ ] 41. Push `feat/phases-2-9` and open a PR against `main` (do NOT merge). PR body: summary of phases 2–9, what was tested (ruff+mypy+pytest green; WeasyPrint PDF tests run in CI), and the manual-credential steps the owner must still perform.
      Verify: PR created; branch pushed with upstream tracking.
