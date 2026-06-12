from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path


MIGRATION_004_PATH = Path(__file__).parent / "migrations" / "004_freeze_triggers.sql"


def apply_freeze_migration(connection: sqlite3.Connection) -> None:
    connection.executescript(MIGRATION_004_PATH.read_text(encoding="utf-8"))


def is_memory_frozen(connection: sqlite3.Connection) -> bool:
    row = connection.execute(
        "SELECT value FROM memory_meta WHERE key = 'memory_frozen'"
    ).fetchone()
    return row is not None and str(row[0]) == "1"


def freeze_memory(connection: sqlite3.Connection) -> str:
    frozen_at = datetime.now(timezone.utc).isoformat()
    connection.execute(
        """
        INSERT INTO memory_meta(key, value)
        VALUES ('memory_frozen', '1')
        ON CONFLICT(key) DO UPDATE SET value = excluded.value
        """
    )
    connection.execute(
        """
        INSERT INTO memory_meta(key, value)
        VALUES ('frozen_at', ?)
        ON CONFLICT(key) DO UPDATE SET value = excluded.value
        """,
        (frozen_at,),
    )
    return frozen_at
