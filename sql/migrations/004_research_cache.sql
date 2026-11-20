-- Additive, shared cache for source-attributed external research results.
CREATE TABLE IF NOT EXISTS research_cache (
    query_hash   CHAR(64) PRIMARY KEY,
    provider_name TEXT NOT NULL,
    query_text    TEXT NOT NULL,
    results       JSONB NOT NULL DEFAULT '[]'::jsonb
                  CHECK (jsonb_typeof(results) = 'array'),
    retrieved_at  TIMESTAMPTZ NOT NULL,
    expires_at    TIMESTAMPTZ NOT NULL,
    CHECK (expires_at >= retrieved_at)
);

CREATE INDEX IF NOT EXISTS idx_research_cache_expiry
    ON research_cache (expires_at);
