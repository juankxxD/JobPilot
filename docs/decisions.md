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
