-- Add a status-transition timestamp to jobs (Phase 5).
--
-- The company+role dedupe window (proposals.dedupe_company_role_days) must be
-- measured from when a job was *proposed*, not from when it was first collected
-- (found_at). A job collected long ago but proposed today must still block an
-- identical proposal made immediately afterwards. status_changed_at records the
-- last time the row's status changed and is what the dedupe query compares.

ALTER TABLE jobs
    ADD COLUMN IF NOT EXISTS status_changed_at timestamptz NOT NULL DEFAULT now();
