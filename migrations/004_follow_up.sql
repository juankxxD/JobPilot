-- Follow-up reminder bookkeeping (Phase 7).
--
-- A pending application whose follow_up_at has passed gets a single Telegram
-- reminder. reminder_sent_at records when that reminder went out so a later run
-- does not remind again for the same application (the reminder is sent once).
-- It stays NULL until the first reminder and is irrelevant once the application
-- leaves the 'pending' outcome.

ALTER TABLE applications
    ADD COLUMN IF NOT EXISTS reminder_sent_at timestamptz;
