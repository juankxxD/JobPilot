-- Email-send idempotency (Phase 6, R11.1 no-double-send).
--
-- A successful Graph/Gmail send followed by a database failure must never allow a
-- second Enviar to re-send the same email. We make the email-send operation a
-- durable, uniquely-claimed row that is created BEFORE the provider is called and
-- finalized (together with the application row and the job status) in one
-- transaction afterwards.
--
-- States:
--   in_progress  a claim was taken; the provider call is underway or its outcome
--                is ambiguous (provider accepted but the database update failed).
--                Never auto-resent: it blocks a second claim until resolved.
--   done         the send completed and the application/job were finalized.
--
-- The PRIMARY KEY on (job_id, channel) is the one-email-application-per-job
-- invariant: at most one email claim can ever exist for a job, so a replayed
-- Enviar cannot invoke the provider again.

CREATE TABLE IF NOT EXISTS email_send_claims (
    job_id     bigint NOT NULL REFERENCES jobs (id),
    channel    text NOT NULL DEFAULT 'email',
    state      text NOT NULL DEFAULT 'in_progress',
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (job_id, channel),
    CONSTRAINT email_send_claims_state_check CHECK (state IN ('in_progress', 'done'))
);
