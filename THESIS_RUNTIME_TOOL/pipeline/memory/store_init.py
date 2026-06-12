from __future__ import annotations

import sqlite3
from pathlib import Path


BASE_SCHEMA_PATH = Path(__file__).with_name("schema_v2_base.sql")
MIGRATION_003_PATH = Path(__file__).parent / "migrations" / "003_thesis_runs.sql"
MIGRATION_004_PATH = Path(__file__).parent / "migrations" / "004_freeze_triggers.sql"
MIGRATION_005_PATH = Path(__file__).parent / "migrations" / "005_window_id.sql"


def _connect(path: str | Path) -> sqlite3.Connection:
    db_path = Path(path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(db_path)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


def _read_sql(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _table_exists(connection: sqlite3.Connection, table: str) -> bool:
    row = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (table,),
    ).fetchone()
    return row is not None


def _column_exists(connection: sqlite3.Connection, table: str, column: str) -> bool:
    rows = connection.execute(f"PRAGMA table_info({table})").fetchall()
    return column in {str(row["name"]) for row in rows}


def _add_column_if_missing(
    connection: sqlite3.Connection,
    table: str,
    column: str,
    ddl: str,
) -> None:
    if not _table_exists(connection, table):
        raise RuntimeError(f"Required table does not exist: {table}")
    if _column_exists(connection, table, column):
        return
    connection.execute(f"ALTER TABLE {table} ADD COLUMN {ddl}")


def _apply_migration_003(connection: sqlite3.Connection) -> None:
    _add_column_if_missing(connection, "memory_packs", "config", "config TEXT")
    connection.executescript(_read_sql(MIGRATION_003_PATH))


def _apply_migration_004(connection: sqlite3.Connection) -> None:
    connection.executescript(_read_sql(MIGRATION_004_PATH))


def _apply_migration_005(connection: sqlite3.Connection) -> None:
    """Add window_id column to translation_runs if not present, then create indexes."""
    _add_column_if_missing(connection, "translation_runs", "window_id", "window_id TEXT")
    connection.executescript(_read_sql(MIGRATION_005_PATH))


def init_db(path: str | Path) -> sqlite3.Connection:
    """Create a fresh thesis runtime DB from schema v2 plus migrations 003–005."""
    connection = _connect(path)
    try:
        connection.executescript(_read_sql(BASE_SCHEMA_PATH))
        _apply_migration_003(connection)
        _apply_migration_004(connection)
        _apply_migration_005(connection)
        connection.commit()
    except Exception:
        connection.close()
        raise
    return connection


def migrate_db(path: str | Path) -> sqlite3.Connection:
    """Apply all migrations to an existing DB (003, 004, 005)."""
    connection = _connect(path)
    try:
        _apply_migration_003(connection)
        _apply_migration_004(connection)
        _apply_migration_005(connection)
        connection.commit()
    except Exception:
        connection.close()
        raise
    return connection
