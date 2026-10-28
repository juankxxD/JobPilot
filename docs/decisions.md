# Architecture Decision Records

## ADR-001: uv for dependency management
Chosen over plain pip for fast, reproducible installs with a lockfile, native to
`pyproject.toml`. `requirements.txt` is exported for CI so the workflow does not
depend on uv.

## ADR-002: Versioned SQL migrations over Alembic
A single-owner project with a small, stable schema does not justify Alembic's
weight. `migrations/NNN_*.sql` are applied in order by a small runner that records
applied versions in `schema_migrations`. Can migrate to Alembic later if needed.

## ADR-003: Outlook (Microsoft Graph) is the default mailer
`MAIL_PROVIDER` selects the implementation; default is `outlook`. Graph uses OAuth
2.0 (msal), delegated `Mail.Send`, `saveToSentItems=true`. Gmail is the secondary
implementation. SMTP+password is documented but disabled by default.

## ADR-004: WeasyPrint for HTML -> PDF
Full control over an ATS layout via HTML/CSS. System dependencies (pango, cairo)
are installed via apt in CI and documented per OS for local use.

## ADR-005: Default Groq model is openai/gpt-oss-120b
`llama-3.3-70b-versatile` was retired from Groq's free/developer tier in August
2026. `openai/gpt-oss-120b` is the recommended self-serve replacement;
`openai/gpt-oss-20b` is a cheaper option. Configurable via `GROQ_MODEL`.

## ADR-006: lingua for language detection
More accurate than langdetect on short, mixed job descriptions. The LLM confirms
only in low-confidence cases.

## ADR-007: No default-language document generation on timeout
If language detection and the LLM cannot decide and the owner does not pick a
language, the offer stays `proposed` awaiting the button. `DEFAULT_LANGUAGE` only
forces a language when explicitly enabled via `config.yaml`
(`language.use_default_on_timeout`). This avoids generating documents in the wrong
language.

## ADR-008: Phase 1 ships a minimal working GroqClient
`import-cv` needs a real LLM to run end to end. Rather than stub it until Phase 3,
Phase 1 ships a functional `GroqClient` (chat completions in JSON mode) plus
`complete_json` with one schema-retry. `OllamaClient` and the automatic
`FallbackLlmClient` are scaffolded now and hardened (quota/429 handling, fallback
exercised) in Phase 3. This lets the owner validate `import-cv` with a real CV by
setting only `GROQ_API_KEY`.

## ADR-009: Draft import leaves owner-only sections as scaffolds
`answers_bank` and `preferences` are owner-approved content, not facts to extract
from a resume, so the importer scaffolds them empty rather than letting the LLM
invent them. Incomplete translations are always marked `pending_review`.

## ADR-010: Cover-letter anti-invention is enforced by a technology catalogue
The first cover-letter validator only extracted technologies already present in
the profile and then checked that subset against the profile, so the "unknown" set
was always empty and an invented technology could never be caught (Phase 5 review
finding P5-012). The validator now scans the generated body against a curated
catalogue of common technology names (`find_invented_technologies`) and rejects any
catalogue technology that appears in the letter but is absent from the profile,
retrying once and marking the job `error` if it persists. The catalogue is
deliberately broad; a missing entry only means a fabricated name is not caught, never
that a legitimate profile technology is rejected.

## ADR-011: Telegram processing is idempotent-by-update_id; full transactional outbox is deferred
The Telegram flow is idempotent in the normal case: each `update_id` is claimed via
`telegram_state` before its handler runs and processed once; the happy path for
propose/approve/adjust works end to end and is covered by tests. Three hardening
items from the Phase 5 review remain as known technical debt, tracked for a
dedicated "durable Telegram outbox" iteration rather than resolved here, because the
automated review loop was not converging on them (each pass surfaced another variant
of the same transactional-durability gap):

- **P5-013** — a retryable language-choice/adjust failure can be checkpointed as
  processed (offset advanced before the effect completes), so an interrupted owner
  action may not be resumed.
- **P5-014** — a partial proposal delivery (one attachment sent, a later call fails)
  is not resumed per-part, so a retry can resend attachments / re-upload to Drive.
- **P5-015** — `DAILY_APPLICATION_LIMIT` counts proposals within a single run; it
  does not persist a durable daily reservation across the 2-hour cron, so the limit
  can be exceeded across runs.

Resolution plan: persist a per-`update_id` operation lifecycle and a durable
document/delivery outbox, claim-then-complete effects atomically, and count prior
same-day proposals from a persisted timestamp. These do not block the happy-path
flow or the later phases; they harden behaviour under partial crashes and across
scheduled runs. Noted in the PR for the owner's review.

## ADR-012: Phase 6 send-path durability — one bug fixed, two transport-atomicity items deferred
The Phase 6 review surfaced four issues. Two are fixed here; two join the durable
outbox debt of ADR-011 because they are the same transactional-atomicity concern
and the automated review loop was not converging on them.

Fixed:
- **P6-005 (critical, migration collision)** — `002_status_changed_at.sql` (Phase 5)
  and the new email-claims migration both used version `002`; the runner keys
  `schema_migrations` by the numeric prefix only, so an upgraded DB would skip the
  claims table and a fresh run would fail on the duplicate. Renamed the email-claims
  migration to `003_email_send_claims.sql` and added a `DuplicateMigrationVersionError`
  guard in `discover_migrations` plus tests (duplicate rejected; real migrations are
  001/002/003 and unique).
- **P6-002 (contact email lost on recollection)** — `contact_email` is now preserved
  with `COALESCE(EXCLUDED.contact_email, jobs.contact_email)` in both upsert paths
  (and mirrored in the fakes), so an analysis-derived address is not erased when a
  later source fetch returns null; a fresh source email still takes priority. Tests
  cover preserve-on-null and prefer-fresh.

Deferred to the durable-outbox iteration (ADR-011):
- **P6-004** — an ambiguous email-provider transport failure (timeout/reset after
  the provider may have accepted) currently releases the send claim, so a later
  Enviar could re-send. Correct fix: release only on a typed outcome proving no
  acceptance; keep an in-progress/ambiguous claim otherwise.
- **P6-006** — manual-application recording and the job status change are not a
  single atomic unit and there is no per-job application uniqueness constraint, so a
  status-write failure after the application insert could let a later "Ya apliqué"
  create a duplicate. Correct fix: atomic insert-or-recover backed by a uniqueness
  constraint.
These harden behaviour only under partial crashes; the happy-path send flow is
guarded against double-send by the eligible-state check and the email claim, and is
covered by tests.

## ADR-013: Assisted-form safety is fail-safe by design, not an exhaustive classifier
The Phase 8 review loop did not converge on the sensitive-question classifier: each
pass found another natural-language phrasing or DOM shape that a deterministic rule
set missed, because a rule set over arbitrary form text/HTML is inherently
incomplete and can only be fully validated against a real browser and real
Greenhouse/Lever forms (which is this phase's local done-criterion).

Rather than chase an impossible "perfect" classifier, the design is fail-safe: the
LLM is consulted for a form question ONLY when it is positively recognized as an
open motivation/interest prompt (`is_confidently_non_sensitive`). Any question that
is neither confidently non-sensitive nor served by an exact `answers_bank` match is
left BLANK and reported to the owner (uncertain / unanswered_sensitive), and every
such field appears in the pre-submit Telegram screenshot + uncertain-field list.
A classifier gap therefore produces a visible blank the owner reviews, never a
silent wrong or sensitive answer. This upholds R15.5 (never-LLM for sensitive) and
the human-in-the-loop principle even when the classifier is imperfect.

Residual items to verify LOCALLY with a real browser (tracked, not blocking; the
fail-safe covers them until then):
- Exact option label/value/selector matching for real `<select>`/radio controls and
  required radio-group validation (P8-010) — depends on live DOM semantics the fakes
  only approximate.
- Lever cover-letter destination placement and pre-submit equality check against a
  real Lever form (P8-011).
- Post-dispatch CAPTCHA / click-timeout claim retention under a real network
  (P8-013/P8-008): an ambiguous post-click outcome retains the durable submit claim
  so a retry cannot duplicate; "stop + screenshot + notify" always fires.
- Pre-submit Playwright interaction/DOM errors routed through the safe-abort path
  (P8-014).
These are exercised with mocked Playwright in CI and must be confirmed with
`playwright install` against a real Greenhouse/Lever form before relying on assisted
submit. The module never submits an incomplete form and never submits without the
owner's explicit Enviar.
