-- Migration 003: thesis run/eval persistence and runtime relation/QA tables.
-- Additive schema v2 -> v3; keep donor prototype tables intact.

CREATE TABLE IF NOT EXISTS translation_runs (
  run_id TEXT PRIMARY KEY,
  experiment_id TEXT NOT NULL,
  doc_id TEXT NOT NULL,
  block_id TEXT NOT NULL,
  config TEXT NOT NULL CHECK (config IN ('S0','S1','S2','S3','S3a','S3b','S3d','SLC')),
  stage TEXT NOT NULL DEFAULT 'draft' CHECK (stage IN ('draft','revised')),
  prev_run_id TEXT,
  pack_id TEXT,
  output_text TEXT DEFAULT '',
  model TEXT,
  prompt_version TEXT,
  temperature REAL,
  seed INTEGER,
  system_fingerprint TEXT,
  cost REAL,
  latency_ms INTEGER,
  created_at TEXT DEFAULT CURRENT_TIMESTAMP,
  FOREIGN KEY(doc_id) REFERENCES documents(doc_id) ON DELETE CASCADE,
  FOREIGN KEY(block_id) REFERENCES blocks(block_id) ON DELETE CASCADE,
  FOREIGN KEY(prev_run_id) REFERENCES translation_runs(run_id) ON DELETE SET NULL,
  FOREIGN KEY(pack_id) REFERENCES memory_packs(pack_id) ON DELETE SET NULL,
  UNIQUE(experiment_id, block_id, config, stage)
);

CREATE INDEX IF NOT EXISTS idx_translation_runs_experiment_config
  ON translation_runs(experiment_id, config);
CREATE INDEX IF NOT EXISTS idx_translation_runs_doc_block
  ON translation_runs(doc_id, block_id);

CREATE TABLE IF NOT EXISTS reference_eval_only (
  reference_id TEXT PRIMARY KEY,
  doc_id TEXT,
  block_id TEXT,
  target_text TEXT NOT NULL,
  provenance TEXT CHECK (provenance IN ('ailab_gold','published')),
  leakage_risk TEXT CHECK (leakage_risk IN ('low','high')),
  subset_tag TEXT,
  created_at TEXT DEFAULT CURRENT_TIMESTAMP,
  FOREIGN KEY(doc_id) REFERENCES documents(doc_id) ON DELETE CASCADE,
  FOREIGN KEY(block_id) REFERENCES blocks(block_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_reference_eval_doc_block
  ON reference_eval_only(doc_id, block_id);

CREATE TABLE IF NOT EXISTS evaluation_runs (
  eval_id TEXT PRIMARY KEY,
  run_id TEXT,
  scope TEXT CHECK (scope IN ('block','chapter','book')),
  scope_id TEXT,
  metric_name TEXT NOT NULL,
  metric_value REAL,
  metric_version TEXT,
  reference_id TEXT,
  judge_model TEXT,
  judge_rationale TEXT,
  ablation_label TEXT,
  ci_low REAL,
  ci_high REAL,
  created_at TEXT DEFAULT CURRENT_TIMESTAMP,
  FOREIGN KEY(run_id) REFERENCES translation_runs(run_id) ON DELETE CASCADE,
  FOREIGN KEY(reference_id) REFERENCES reference_eval_only(reference_id) ON DELETE SET NULL
);

CREATE INDEX IF NOT EXISTS idx_evaluation_runs_run_metric
  ON evaluation_runs(run_id, metric_name);
CREATE INDEX IF NOT EXISTS idx_evaluation_runs_scope_metric
  ON evaluation_runs(scope, scope_id, metric_name);

CREATE TABLE IF NOT EXISTS entity_relations (
  relation_id TEXT PRIMARY KEY,
  doc_id TEXT NOT NULL,
  source_entity_id TEXT NOT NULL,
  target_entity_id TEXT NOT NULL,
  relation_type TEXT NOT NULL,
  state_label TEXT,
  valid_from_block_id TEXT,
  valid_to_block_id TEXT,
  trigger_event_id TEXT,
  address_policy_json TEXT DEFAULT '{}',
  evidence_json TEXT DEFAULT '[]',
  confidence REAL DEFAULT 0.5,
  notes TEXT DEFAULT '',
  created_at TEXT DEFAULT CURRENT_TIMESTAMP,
  updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
  FOREIGN KEY(doc_id) REFERENCES documents(doc_id) ON DELETE CASCADE,
  FOREIGN KEY(source_entity_id) REFERENCES entities(entity_id) ON DELETE CASCADE,
  FOREIGN KEY(target_entity_id) REFERENCES entities(entity_id) ON DELETE CASCADE,
  FOREIGN KEY(valid_from_block_id) REFERENCES blocks(block_id) ON DELETE SET NULL,
  FOREIGN KEY(valid_to_block_id) REFERENCES blocks(block_id) ON DELETE SET NULL
);

CREATE INDEX IF NOT EXISTS idx_entity_relations_doc_pair
  ON entity_relations(doc_id, source_entity_id, target_entity_id);

CREATE TABLE IF NOT EXISTS qa_issues (
  issue_id TEXT PRIMARY KEY,
  doc_id TEXT,
  run_id TEXT,
  block_id TEXT,
  tier TEXT CHECK (tier IN ('tier1','tier2')),
  rule_or_subtype TEXT,
  severity TEXT CHECK (severity IN ('minor','major','critical')),
  evidence_source TEXT,
  evidence_target TEXT,
  suggestion TEXT,
  fixed INTEGER DEFAULT 0,
  retry_count INTEGER DEFAULT 0,
  created_at TEXT DEFAULT CURRENT_TIMESTAMP,
  FOREIGN KEY(doc_id) REFERENCES documents(doc_id) ON DELETE CASCADE,
  FOREIGN KEY(run_id) REFERENCES translation_runs(run_id) ON DELETE CASCADE,
  FOREIGN KEY(block_id) REFERENCES blocks(block_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_qa_issues_run
  ON qa_issues(run_id);
CREATE INDEX IF NOT EXISTS idx_qa_issues_doc_block
  ON qa_issues(doc_id, block_id);

INSERT OR REPLACE INTO memory_meta(key, value) VALUES ('schema_version', '3');
