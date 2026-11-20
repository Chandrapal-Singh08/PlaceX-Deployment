-- Allow a saved free-text Journey Guide to become a progress-tracked roadmap.
ALTER TABLE roadmaps
    ALTER COLUMN jd_id DROP NOT NULL;

ALTER TABLE roadmaps
    ADD COLUMN IF NOT EXISTS source_guide_id BIGINT REFERENCES journey_guides(id),
    ADD COLUMN IF NOT EXISTS company_name TEXT,
    ADD COLUMN IF NOT EXISTS role_title TEXT,
    ADD COLUMN IF NOT EXISTS daily_study_hours SMALLINT,
    ADD COLUMN IF NOT EXISTS deadline_days SMALLINT,
    ADD COLUMN IF NOT EXISTS deadline_date DATE;

CREATE UNIQUE INDEX IF NOT EXISTS idx_roadmaps_source_guide
    ON roadmaps (source_guide_id)
    WHERE source_guide_id IS NOT NULL;

ALTER TABLE roadmaps
    DROP CONSTRAINT IF EXISTS roadmaps_jd_or_source_guide_check;
ALTER TABLE roadmaps
    ADD CONSTRAINT roadmaps_jd_or_source_guide_check
    CHECK (
        (jd_id IS NOT NULL AND source_guide_id IS NULL)
        OR (jd_id IS NULL AND source_guide_id IS NOT NULL)
    );

ALTER TABLE roadmaps
    DROP CONSTRAINT IF EXISTS roadmaps_daily_study_hours_check;
ALTER TABLE roadmaps
    ADD CONSTRAINT roadmaps_daily_study_hours_check
    CHECK (daily_study_hours IS NULL OR daily_study_hours BETWEEN 1 AND 8);

ALTER TABLE roadmaps
    DROP CONSTRAINT IF EXISTS roadmaps_deadline_days_check;
ALTER TABLE roadmaps
    ADD CONSTRAINT roadmaps_deadline_days_check
    CHECK (deadline_days IS NULL OR deadline_days > 0);

ALTER TABLE roadmap_items
    ALTER COLUMN skill_id DROP NOT NULL;
ALTER TABLE roadmap_items
    ADD COLUMN IF NOT EXISTS title TEXT NOT NULL DEFAULT '',
    ADD COLUMN IF NOT EXISTS resource_title TEXT,
    ADD COLUMN IF NOT EXISTS resource_url TEXT;

ALTER TABLE roadmap_items
    DROP CONSTRAINT IF EXISTS roadmap_items_skill_or_title_check;
ALTER TABLE roadmap_items
    ADD CONSTRAINT roadmap_items_skill_or_title_check
    CHECK (skill_id IS NOT NULL OR length(trim(title)) > 0);
