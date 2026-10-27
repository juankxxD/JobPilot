-- JobPilot initial schema (R12).
-- Applied by src/jobpilot/storage/migrate.py, which records the version in
-- schema_migrations. Idempotency constraints live on the jobs table.

CREATE TABLE IF NOT EXISTS schema_migrations (
    version    text PRIMARY KEY,
    applied_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS jobs (
    id                   bigserial PRIMARY KEY,
    source               text NOT NULL,
    external_id          text NOT NULL,
    url                  text NOT NULL,
    apply_url            text,
    title                text NOT NULL,
    company              text NOT NULL,
    location             text,
    remote               boolean NOT NULL DEFAULT false,
    salary_text          text,
    contact_email        text,
    language             text,
    description          text NOT NULL DEFAULT '',
    analysis             jsonb,
    score                integer,
    score_reason         text,
    missing_requirements jsonb,
    status               text NOT NULL DEFAULT 'new',
    found_at             timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT jobs_source_external_id_key UNIQUE (source, external_id),
    CONSTRAINT jobs_url_key UNIQUE (url),
    CONSTRAINT jobs_status_check CHECK (
        status IN (
            'new',
            'filtered_out',
            'scored',
            'proposed',
            'approved',
            'applied',
            'discarded',
            'error'
        )
    )
);

CREATE TABLE IF NOT EXISTS applications (
    id               bigserial PRIMARY KEY,
    job_id           bigint NOT NULL REFERENCES jobs (id),
    language         text NOT NULL,
    cv_variant       text NOT NULL,
    cv_url           text,
    cover_letter_url text,
    channel          text NOT NULL,
    applied_at       timestamptz,
    follow_up_at     timestamptz,
    outcome          text NOT NULL DEFAULT 'pending',
    notes            text,
    created_at       timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT applications_channel_check CHECK (
        channel IN ('email', 'form_assisted', 'portal', 'manual')
    ),
    CONSTRAINT applications_outcome_check CHECK (
        outcome IN ('pending', 'interview', 'rejected', 'offer', 'no_response')
    )
);

CREATE TABLE IF NOT EXISTS telegram_state (
    key   text PRIMARY KEY,
    value text
);
