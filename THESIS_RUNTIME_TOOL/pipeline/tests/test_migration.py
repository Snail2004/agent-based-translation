from __future__ import annotations

import sqlite3

import pytest

from pipeline.memory.store_init import BASE_SCHEMA_PATH, init_db, migrate_db


NEW_TABLES = {
    "translation_runs",
    "evaluation_runs",
    "reference_eval_only",
    "entity_relations",
    "qa_issues",
}

V2_TABLES = {"blocks", "entities", "glossary_entries", "memory_packs"}


def _table_names(connection: sqlite3.Connection) -> set[str]:
    rows = connection.execute(
        "SELECT name FROM sqlite_master WHERE type IN ('table','virtual')"
    ).fetchall()
    return {str(row[0]) for row in rows}


def _columns(connection: sqlite3.Connection, table: str) -> set[str]:
    rows = connection.execute(f"PRAGMA table_info({table})").fetchall()
    return {str(row[1]) for row in rows}


def _schema_version(connection: sqlite3.Connection) -> str:
    row = connection.execute(
        "SELECT value FROM memory_meta WHERE key = 'schema_version'"
    ).fetchone()
    return str(row[0])


def _create_v2_db(path) -> sqlite3.Connection:
    connection = sqlite3.connect(path)
    connection.execute("PRAGMA foreign_keys = ON")
    connection.executescript(BASE_SCHEMA_PATH.read_text(encoding="utf-8"))
    connection.execute(
        """
        INSERT INTO documents (doc_id, job_id, source_filename, metadata_json)
        VALUES ('doc1', 'job1', 'source.txt', '{}')
        """
    )
    connection.execute(
        """
        INSERT INTO blocks (block_id, doc_id, order_index, text)
        VALUES ('b1', 'doc1', 1, 'Hello world.')
        """
    )
    connection.execute(
        """
        INSERT INTO memory_packs (
          pack_id, doc_id, block_id, pack_hash, payload_json
        ) VALUES ('pack1', 'doc1', 'b1', 'hash1', '{}')
        """
    )
    connection.commit()
    connection.close()
    return sqlite3.connect(path)


def test_init_fresh_db(tmp_path):
    db_path = tmp_path / "fresh.sqlite3"
    connection = init_db(db_path)
    try:
        names = _table_names(connection)
        assert NEW_TABLES <= names
        assert V2_TABLES <= names
        assert "config" in _columns(connection, "memory_packs")
        assert _schema_version(connection) == "3"
    finally:
        connection.close()


def test_migrate_v2_db(tmp_path):
    db_path = tmp_path / "v2.sqlite3"
    setup_connection = _create_v2_db(db_path)
    setup_connection.close()

    connection = migrate_db(db_path)
    try:
        names = _table_names(connection)
        assert NEW_TABLES <= names
        assert "config" in _columns(connection, "memory_packs")
        assert _schema_version(connection) == "3"
        block = connection.execute(
            "SELECT text FROM blocks WHERE block_id = 'b1'"
        ).fetchone()
        pack = connection.execute(
            "SELECT pack_hash FROM memory_packs WHERE pack_id = 'pack1'"
        ).fetchone()
        assert block[0] == "Hello world."
        assert pack[0] == "hash1"
    finally:
        connection.close()


def test_migrate_idempotent(tmp_path):
    db_path = tmp_path / "idempotent.sqlite3"
    setup_connection = _create_v2_db(db_path)
    setup_connection.close()

    first = migrate_db(db_path)
    first.close()
    second = migrate_db(db_path)
    try:
        assert _schema_version(second) == "3"
        config_columns = [
            row for row in second.execute("PRAGMA table_info(memory_packs)").fetchall()
            if str(row[1]) == "config"
        ]
        assert len(config_columns) == 1
    finally:
        second.close()


def test_unique_run_constraint(tmp_path):
    db_path = tmp_path / "unique.sqlite3"
    connection = init_db(db_path)
    try:
        connection.execute(
            """
            INSERT INTO documents (doc_id, job_id, source_filename, metadata_json)
            VALUES ('doc1', 'job1', 'source.txt', '{}')
            """
        )
        connection.execute(
            """
            INSERT INTO blocks (block_id, doc_id, order_index, text)
            VALUES ('b1', 'doc1', 1, 'Hello world.')
            """
        )
        connection.execute(
            """
            INSERT INTO translation_runs (
              run_id, experiment_id, doc_id, block_id, config, stage
            ) VALUES ('run1', 'exp1', 'doc1', 'b1', 'SLC', 'draft')
            """
        )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """
                INSERT INTO translation_runs (
                  run_id, experiment_id, doc_id, block_id, config, stage
                ) VALUES ('run2', 'exp1', 'doc1', 'b1', 'SLC', 'draft')
                """
            )
    finally:
        connection.close()
