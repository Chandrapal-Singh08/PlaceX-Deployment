-- Additive storage for user-generated dynamic Journey Guides.
CREATE TABLE IF NOT EXISTS journey_guides (
    id                BIGSERIAL PRIMARY KEY,
    user_id           BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    company_name      TEXT NOT NULL,
    job_role          TEXT NOT NULL,
    location          TEXT,
    experience_level  TEXT,
    employment_type   TEXT,
    guidance          JSONB NOT NULL CHECK (jsonb_typeof(guidance) = 'object'),
    retrieved_context JSONB NOT NULL DEFAULT '{}'::jsonb
                      CHECK (jsonb_typeof(retrieved_context) = 'object'),
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_journey_guides_user
    ON journey_guides (user_id, created_at DESC);
