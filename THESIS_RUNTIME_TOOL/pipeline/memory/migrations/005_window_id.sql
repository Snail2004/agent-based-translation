-- Migration 005: window_id column for translation_runs.
-- Tracks which WINDOW each translation run belongs to.
-- Additive: adds a column only if not present.

-- The ALTER TABLE ADD COLUMN form is used; SQLite ignores the IF NOT EXISTS
-- at the column level (unlike table-level IF NOT EXISTS).  The _add_column_if_missing
-- helper in store_init.py guards against adding twice.
-- (No standalone DDL needed here since store_init.py uses Python-side guard.)

-- Index for efficient lookup by (experiment_id, window_id).
CREATE INDEX IF NOT EXISTS idx_translation_runs_experiment_window
  ON translation_runs(experiment_id, window_id);

-- Index for efficient lookup by window_id across all runs.
CREATE INDEX IF NOT EXISTS idx_translation_runs_window
  ON translation_runs(window_id);
