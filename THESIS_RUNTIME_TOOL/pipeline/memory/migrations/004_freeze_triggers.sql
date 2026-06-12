-- Migration 004: DB-level FREEZE guard for thesis pre-pass memory T1-T4.
-- Frozen memory blocks writes to glossary_entries, entities, mentions,
-- entity_relations, and memory_items. Runtime tables remain writable.

INSERT OR IGNORE INTO memory_meta(key, value) VALUES ('memory_frozen', '0');

CREATE TRIGGER IF NOT EXISTS trg_freeze_glossary_entries_insert
BEFORE INSERT ON glossary_entries
WHEN (SELECT value FROM memory_meta WHERE key = 'memory_frozen') = '1'
BEGIN
  SELECT RAISE(ABORT, 'memory frozen (LOCK §3.3)');
END;

CREATE TRIGGER IF NOT EXISTS trg_freeze_glossary_entries_update
BEFORE UPDATE ON glossary_entries
WHEN (SELECT value FROM memory_meta WHERE key = 'memory_frozen') = '1'
BEGIN
  SELECT RAISE(ABORT, 'memory frozen (LOCK §3.3)');
END;

CREATE TRIGGER IF NOT EXISTS trg_freeze_glossary_entries_delete
BEFORE DELETE ON glossary_entries
WHEN (SELECT value FROM memory_meta WHERE key = 'memory_frozen') = '1'
BEGIN
  SELECT RAISE(ABORT, 'memory frozen (LOCK §3.3)');
END;

CREATE TRIGGER IF NOT EXISTS trg_freeze_entities_insert
BEFORE INSERT ON entities
WHEN (SELECT value FROM memory_meta WHERE key = 'memory_frozen') = '1'
BEGIN
  SELECT RAISE(ABORT, 'memory frozen (LOCK §3.3)');
END;

CREATE TRIGGER IF NOT EXISTS trg_freeze_entities_update
BEFORE UPDATE ON entities
WHEN (SELECT value FROM memory_meta WHERE key = 'memory_frozen') = '1'
BEGIN
  SELECT RAISE(ABORT, 'memory frozen (LOCK §3.3)');
END;

CREATE TRIGGER IF NOT EXISTS trg_freeze_entities_delete
BEFORE DELETE ON entities
WHEN (SELECT value FROM memory_meta WHERE key = 'memory_frozen') = '1'
BEGIN
  SELECT RAISE(ABORT, 'memory frozen (LOCK §3.3)');
END;

CREATE TRIGGER IF NOT EXISTS trg_freeze_mentions_insert
BEFORE INSERT ON mentions
WHEN (SELECT value FROM memory_meta WHERE key = 'memory_frozen') = '1'
BEGIN
  SELECT RAISE(ABORT, 'memory frozen (LOCK §3.3)');
END;

CREATE TRIGGER IF NOT EXISTS trg_freeze_mentions_update
BEFORE UPDATE ON mentions
WHEN (SELECT value FROM memory_meta WHERE key = 'memory_frozen') = '1'
BEGIN
  SELECT RAISE(ABORT, 'memory frozen (LOCK §3.3)');
END;

CREATE TRIGGER IF NOT EXISTS trg_freeze_mentions_delete
BEFORE DELETE ON mentions
WHEN (SELECT value FROM memory_meta WHERE key = 'memory_frozen') = '1'
BEGIN
  SELECT RAISE(ABORT, 'memory frozen (LOCK §3.3)');
END;

CREATE TRIGGER IF NOT EXISTS trg_freeze_entity_relations_insert
BEFORE INSERT ON entity_relations
WHEN (SELECT value FROM memory_meta WHERE key = 'memory_frozen') = '1'
BEGIN
  SELECT RAISE(ABORT, 'memory frozen (LOCK §3.3)');
END;

CREATE TRIGGER IF NOT EXISTS trg_freeze_entity_relations_update
BEFORE UPDATE ON entity_relations
WHEN (SELECT value FROM memory_meta WHERE key = 'memory_frozen') = '1'
BEGIN
  SELECT RAISE(ABORT, 'memory frozen (LOCK §3.3)');
END;

CREATE TRIGGER IF NOT EXISTS trg_freeze_entity_relations_delete
BEFORE DELETE ON entity_relations
WHEN (SELECT value FROM memory_meta WHERE key = 'memory_frozen') = '1'
BEGIN
  SELECT RAISE(ABORT, 'memory frozen (LOCK §3.3)');
END;

CREATE TRIGGER IF NOT EXISTS trg_freeze_memory_items_insert
BEFORE INSERT ON memory_items
WHEN (SELECT value FROM memory_meta WHERE key = 'memory_frozen') = '1'
BEGIN
  SELECT RAISE(ABORT, 'memory frozen (LOCK §3.3)');
END;

CREATE TRIGGER IF NOT EXISTS trg_freeze_memory_items_update
BEFORE UPDATE ON memory_items
WHEN (SELECT value FROM memory_meta WHERE key = 'memory_frozen') = '1'
BEGIN
  SELECT RAISE(ABORT, 'memory frozen (LOCK §3.3)');
END;

CREATE TRIGGER IF NOT EXISTS trg_freeze_memory_items_delete
BEFORE DELETE ON memory_items
WHEN (SELECT value FROM memory_meta WHERE key = 'memory_frozen') = '1'
BEGIN
  SELECT RAISE(ABORT, 'memory frozen (LOCK §3.3)');
END;
