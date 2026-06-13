-- Migration 006: eval-only glossary gold for D2L.
-- This table is intentionally separate from runtime glossary_entries.

CREATE TABLE IF NOT EXISTS eval_glossary_gold (
  gold_id TEXT PRIMARY KEY,
  doc_id TEXT NOT NULL,
  source_term TEXT NOT NULL,
  target_term TEXT NOT NULL,
  discussion_url TEXT DEFAULT '',
  source_path TEXT NOT NULL,
  source_commit TEXT NOT NULL,
  source_line INTEGER,
  subset_tag TEXT DEFAULT 'd2l_glossary',
  created_at TEXT DEFAULT CURRENT_TIMESTAMP,
  FOREIGN KEY(doc_id) REFERENCES documents(doc_id) ON DELETE CASCADE
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_eval_glossary_gold_doc_pair
  ON eval_glossary_gold(doc_id, source_term, target_term);

CREATE INDEX IF NOT EXISTS idx_eval_glossary_gold_doc_source
  ON eval_glossary_gold(doc_id, source_term);

INSERT OR REPLACE INTO memory_meta(key, value) VALUES ('schema_version', '3');
