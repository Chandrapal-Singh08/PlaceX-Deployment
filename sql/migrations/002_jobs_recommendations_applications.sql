-- Additive job discovery, preference, recommendation, application, and roadmap migration.
-- Safe to apply repeatedly. No existing records are deleted or rewritten except that
-- sample seed records are explicitly classified as sample data by their existing names.

ALTER TABLE companies
    ADD COLUMN IF NOT EXISTS data_classification TEXT NOT NULL DEFAULT 'unverified';
ALTER TABLE job_descriptions
    ADD COLUMN IF NOT EXISTS data_classification TEXT NOT NULL DEFAULT 'user_provided',
    ADD COLUMN IF NOT EXISTS source_url TEXT,
    ADD COLUMN IF NOT EXISTS retrieved_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS last_verified_at TIMESTAMPTZ;
ALTER TABLE hiring_rounds
    ADD COLUMN IF NOT EXISTS data_classification TEXT NOT NULL DEFAULT 'unverified';
ALTER TABLE roadmap_items
    ADD COLUMN IF NOT EXISTS level TEXT NOT NULL DEFAULT 'INTERMEDIATE',
    ADD COLUMN IF NOT EXISTS practice_task TEXT NOT NULL DEFAULT '',
    ADD COLUMN IF NOT EXISTS milestone TEXT NOT NULL DEFAULT '';

UPDATE companies SET data_classification='sample' WHERE name ILIKE '%(sample)';
UPDATE job_descriptions j SET data_classification='sample'
FROM companies c
WHERE c.id=j.company_id AND c.data_classification='sample';
UPDATE hiring_rounds r SET data_classification='sample'
FROM companies c
WHERE c.id=r.company_id AND c.data_classification='sample';

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='companies_data_classification_check') THEN
        ALTER TABLE companies ADD CONSTRAINT companies_data_classification_check
            CHECK (data_classification IN ('sample','user_provided','source_reported','unverified'));
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='job_descriptions_data_classification_check') THEN
        ALTER TABLE job_descriptions ADD CONSTRAINT job_descriptions_data_classification_check
            CHECK (data_classification IN ('sample','user_provided','source_reported','unverified'));
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='hiring_rounds_data_classification_check') THEN
        ALTER TABLE hiring_rounds ADD CONSTRAINT hiring_rounds_data_classification_check
            CHECK (data_classification IN ('sample','user_provided','source_reported','unverified'));
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='roadmap_items_level_check') THEN
        ALTER TABLE roadmap_items ADD CONSTRAINT roadmap_items_level_check
            CHECK (level IN ('BEGINNER','INTERMEDIATE','ADVANCED'));
    END IF;
END $$;

CREATE TABLE IF NOT EXISTS external_jobs (
    id                       BIGSERIAL PRIMARY KEY,
    source_name              TEXT NOT NULL,
    external_id              TEXT NOT NULL,
    source_url               TEXT NOT NULL CHECK (source_url ~ '^https?://'),
    company_name             TEXT NOT NULL,
    role_title               TEXT NOT NULL,
    description              TEXT NOT NULL,
    location                 TEXT,
    is_remote                BOOLEAN NOT NULL DEFAULT FALSE,
    employment_type          TEXT,
    experience_level         TEXT,
    experience_requirements  TEXT,
    education_requirements   TEXT,
    skills                   JSONB NOT NULL DEFAULT '[]'::jsonb CHECK (jsonb_typeof(skills)='array'),
    status                   TEXT NOT NULL DEFAULT 'ACTIVE' CHECK (status IN ('ACTIVE','EXPIRED','UNAVAILABLE')),
    is_verified              BOOLEAN NOT NULL DEFAULT FALSE,
    retrieved_at             TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_verified_at         TIMESTAMPTZ,
    expires_at               TIMESTAMPTZ,
    jd_id                    BIGINT REFERENCES job_descriptions(id) ON DELETE SET NULL,
    raw_data                 JSONB NOT NULL DEFAULT '{}'::jsonb CHECK (jsonb_typeof(raw_data)='object'),
    UNIQUE (source_name, source_url),
    UNIQUE (source_name, external_id)
);

CREATE TABLE IF NOT EXISTS user_job_preferences (
    user_id              BIGINT PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
    preferred_roles      TEXT[] NOT NULL DEFAULT '{}',
    preferred_locations  TEXT[] NOT NULL DEFAULT '{}',
    remote_preference    TEXT NOT NULL DEFAULT 'ANY' CHECK (remote_preference IN ('ANY','REMOTE','ONSITE')),
    employment_types     TEXT[] NOT NULL DEFAULT '{}',
    experience_level     TEXT,
    updated_at           TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS job_recommendations (
    id               BIGSERIAL PRIMARY KEY,
    user_id          BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    resume_id        BIGINT NOT NULL REFERENCES resumes(id) ON DELETE CASCADE,
    external_job_id  BIGINT NOT NULL REFERENCES external_jobs(id) ON DELETE CASCADE,
    match_score      NUMERIC(5,2) NOT NULL CHECK (match_score BETWEEN 0 AND 100),
    category         TEXT NOT NULL CHECK (category IN ('STRONG_MATCH','ACHIEVABLE_MATCH','SKILL_BUILDING')),
    reasons          JSONB NOT NULL DEFAULT '[]'::jsonb CHECK (jsonb_typeof(reasons)='array'),
    matching_details JSONB NOT NULL DEFAULT '{}'::jsonb,
    dismissed        BOOLEAN NOT NULL DEFAULT FALSE,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (user_id, resume_id, external_job_id)
);

CREATE TABLE IF NOT EXISTS applications (
    id                BIGSERIAL PRIMARY KEY,
    user_id           BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    external_job_id   BIGINT REFERENCES external_jobs(id) ON DELETE RESTRICT,
    jd_id             BIGINT REFERENCES job_descriptions(id) ON DELETE RESTRICT,
    status            TEXT NOT NULL DEFAULT 'SAVED'
        CHECK (status IN ('SAVED','APPLIED','ONLINE_ASSESSMENT','TECHNICAL_INTERVIEW',
                          'HR_INTERVIEW','REJECTED','OFFER_RECEIVED')),
    applied_at        TIMESTAMPTZ,
    next_action_at    TIMESTAMPTZ,
    notes             TEXT NOT NULL DEFAULT '',
    related_resume_id BIGINT REFERENCES resumes(id) ON DELETE SET NULL,
    interview_details JSONB NOT NULL DEFAULT '[]'::jsonb CHECK (jsonb_typeof(interview_details)='array'),
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK ((external_job_id IS NOT NULL)::integer + (jd_id IS NOT NULL)::integer = 1)
);

CREATE TABLE IF NOT EXISTS application_events (
    id              BIGSERIAL PRIMARY KEY,
    application_id  BIGINT NOT NULL REFERENCES applications(id) ON DELETE CASCADE,
    user_id         BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    event_type      TEXT NOT NULL,
    from_status     TEXT,
    to_status       TEXT,
    notes           TEXT NOT NULL DEFAULT '',
    details         JSONB NOT NULL DEFAULT '{}'::jsonb,
    occurred_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_external_jobs_status_verified
    ON external_jobs (status, is_verified, last_verified_at DESC);
CREATE INDEX IF NOT EXISTS idx_external_jobs_search
    ON external_jobs (lower(role_title), lower(location));
CREATE INDEX IF NOT EXISTS idx_job_recommendations_user
    ON job_recommendations (user_id, dismissed, match_score DESC);
CREATE INDEX IF NOT EXISTS idx_applications_user_status
    ON applications (user_id, status, next_action_at);
CREATE UNIQUE INDEX IF NOT EXISTS idx_applications_user_external_job
    ON applications (user_id, external_job_id) WHERE external_job_id IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS idx_applications_user_jd
    ON applications (user_id, jd_id) WHERE jd_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_application_events_user
    ON application_events (user_id, occurred_at DESC);
