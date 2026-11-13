-- Phase 14.2: persistent public-document retrieval evidence.
-- Additive only. Raw datasets and all Phase 14/14.1 audit rows stay untouched.

CREATE TABLE IF NOT EXISTS recovery_retrieval_cache (
    url                TEXT PRIMARY KEY,
    final_url          TEXT NOT NULL DEFAULT '',
    title              TEXT NOT NULL DEFAULT '',
    content_type       TEXT NOT NULL DEFAULT '',
    text               TEXT NOT NULL DEFAULT '',
    text_chars         INTEGER NOT NULL DEFAULT 0,
    sha256             TEXT NOT NULL DEFAULT '',
    bytes_read         INTEGER NOT NULL DEFAULT 0,
    page_count         INTEGER,
    extraction_method  TEXT NOT NULL DEFAULT '',
    http_status        INTEGER,
    warnings_json      TEXT NOT NULL DEFAULT '[]',
    fetched_at         TEXT NOT NULL,
    status             TEXT NOT NULL DEFAULT 'ok',
    error              TEXT NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_recovery_retrieval_cache_sha
    ON recovery_retrieval_cache (sha256);
CREATE INDEX IF NOT EXISTS idx_recovery_retrieval_cache_status
    ON recovery_retrieval_cache (status, fetched_at);
