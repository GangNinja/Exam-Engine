-- Phase 14.2 continuation: preserve Crawl4AI page representations and links.
-- Additive only; existing retrieval rows remain valid with empty defaults.

ALTER TABLE recovery_retrieval_cache ADD COLUMN markdown TEXT NOT NULL DEFAULT '';
ALTER TABLE recovery_retrieval_cache ADD COLUMN links_json TEXT NOT NULL DEFAULT '[]';
ALTER TABLE recovery_retrieval_cache ADD COLUMN tables_json TEXT NOT NULL DEFAULT '[]';
ALTER TABLE recovery_retrieval_cache ADD COLUMN metadata_json TEXT NOT NULL DEFAULT '{}';
ALTER TABLE recovery_retrieval_cache ADD COLUMN canonical_url TEXT NOT NULL DEFAULT '';
ALTER TABLE recovery_retrieval_cache ADD COLUMN crawler TEXT NOT NULL DEFAULT 'http';
ALTER TABLE recovery_retrieval_cache ADD COLUMN browser_ms INTEGER;

CREATE INDEX IF NOT EXISTS idx_recovery_retrieval_cache_crawler
    ON recovery_retrieval_cache (crawler, status);
