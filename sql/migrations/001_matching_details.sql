-- Additive Phase 2 migration; safe to run more than once.
ALTER TABLE analysis_reports
    ADD COLUMN IF NOT EXISTS matching_details JSONB NOT NULL DEFAULT '{}'::jsonb;
