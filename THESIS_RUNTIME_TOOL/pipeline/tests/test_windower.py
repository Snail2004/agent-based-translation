from __future__ import annotations

import json
import sqlite3
import tempfile
from pathlib import Path

import pytest

from pipeline.ingest.document_loader import load_document
from pipeline.memory.store_init import init_db
from pipeline.translate.windower import Window, build_windows


def _write_json(path: Path, data: dict) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def _make_db_with_document(
    tmp_path: Path,
    blocks: list[dict],
    chapter_ids: list[str] | None = None,
) -> tuple[sqlite3.Connection, Path]:
    """Create a fresh DB with document+blocks using load_document.

    Uses the real loader to avoid schema mismatches.
    """
    doc_id = "ti"
    doc = {
        "doc_id": doc_id,
        "metadata": {"source_language": "en", "target_language": "vi"},
        "chapters": [
            {
                "chapter_id": cid,
                "blocks": [b for b in blocks if b.get("chapter_id") == cid],
            }
            for cid in (chapter_ids or [])
        ],
    }
    doc_path = tmp_path / "document.json"
    _write_json(doc_path, doc)

    db_path = tmp_path / "memory.sqlite3"
    load_document(db_path, doc_path)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn, db_path


def _make_doc_for_windower(tmp_path: Path, blocks: list[dict]) -> tuple[sqlite3.Connection, Path, list[str]]:
    """Build a test DB for windower tests. Returns (conn, db_path, doc_ids)."""
    chapters = list(dict.fromkeys(b["chapter_id"] for b in blocks))
    conn, db_path = _make_db_with_document(tmp_path, blocks, chapters)
    return conn, db_path, chapters


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_windower_budget_min_oversize(tmp_path):
    """Block alone exceeding budget gets its own 1-block window; others pack by budget."""
    blocks = [
        {"chapter_id": "ti_ch02", "block_id": "ch02_b001", "order_index": 0,
         "block_type": "paragraph", "clean_text": "A" * 800, "source_text": "A" * 800},
        {"chapter_id": "ti_ch02", "block_id": "ch02_b002", "order_index": 1,
         "block_type": "paragraph", "clean_text": "B" * 800, "source_text": "B" * 800},
        {"chapter_id": "ti_ch02", "block_id": "ch02_b003", "order_index": 2,
         "block_type": "paragraph", "clean_text": "C" * 5000, "source_text": "C" * 5000},
        {"chapter_id": "ti_ch02", "block_id": "ch02_b004", "order_index": 3,
         "block_type": "paragraph", "clean_text": "D" * 800, "source_text": "D" * 800},
    ]
    conn, _, _ = _make_doc_for_windower(tmp_path, blocks)

    windows = build_windows(conn, "ti", ["ti_ch02"], target_tokens=1100, max_blocks=8)

    ids = [w.block_ids for w in windows]
    assert len(windows) == 3
    assert ids[0] == ["ch02_b001", "ch02_b002"]
    assert ids[1] == ["ch02_b003"]     # oversize block alone
    assert ids[2] == ["ch02_b004"]
    assert [w.window_id for w in windows] == [
        "w_ch02_001",
        "w_ch02_002",
        "w_ch02_003",
    ]
    for w in windows:
        assert len(w.block_ids) >= 1


def test_windower_chapter_boundary(tmp_path):
    """Windows never cross chapter boundaries."""
    blocks = [
        {"chapter_id": "ti_ch02", "block_id": "ch02_b001", "order_index": 0,
         "block_type": "paragraph", "clean_text": "X" * 2000, "source_text": "X" * 2000},
        {"chapter_id": "ti_ch02", "block_id": "ch02_b002", "order_index": 1,
         "block_type": "paragraph", "clean_text": "Y" * 2000, "source_text": "Y" * 2000},
        {"chapter_id": "ti_ch03", "block_id": "ch03_b001", "order_index": 0,
         "block_type": "paragraph", "clean_text": "Z" * 2000, "source_text": "Z" * 2000},
        {"chapter_id": "ti_ch03", "block_id": "ch03_b002", "order_index": 1,
         "block_type": "paragraph", "clean_text": "W" * 2000, "source_text": "W" * 2000},
    ]
    conn, _, _ = _make_doc_for_windower(tmp_path, blocks)

    windows = build_windows(conn, "ti", ["ti_ch02", "ti_ch03"], target_tokens=1100)

    ch02_ids = {bid for w in windows for bid in w.block_ids if bid.startswith("ch02")}
    ch03_ids = {bid for w in windows for bid in w.block_ids if bid.startswith("ch03")}
    assert ch02_ids == {"ch02_b001", "ch02_b002"}
    assert ch03_ids == {"ch03_b001", "ch03_b002"}
    # Window ids separate per chapter
    window_ids = [w.window_id for w in windows]
    assert window_ids[0].startswith("w_ch02")
    assert window_ids[-1].startswith("w_ch03")


def test_windower_phase_boundary(tmp_path):
    """A new window starts at valid_from_block_id of entity_relations."""
    blocks = [
        {"chapter_id": "ti_ch02", "block_id": "ch02_b001", "order_index": 0,
         "block_type": "paragraph", "clean_text": "A" * 500, "source_text": "A" * 500},
        {"chapter_id": "ti_ch02", "block_id": "ch02_b002", "order_index": 1,
         "block_type": "paragraph", "clean_text": "B" * 500, "source_text": "B" * 500},
        {"chapter_id": "ti_ch02", "block_id": "ch02_b003", "order_index": 2,
         "block_type": "paragraph", "clean_text": "C" * 500, "source_text": "C" * 500},
        {"chapter_id": "ti_ch02", "block_id": "ch02_b004", "order_index": 3,
         "block_type": "paragraph", "clean_text": "D" * 500, "source_text": "D" * 500},
    ]
    conn, _, _ = _make_doc_for_windower(tmp_path, blocks)

    # Insert entities first (required by FK), then entity_relations
    conn.execute(
        """
        INSERT INTO entities (entity_id, doc_id, canonical_source, canonical_target)
        VALUES (?, ?, ?, ?)
        """,
        ("ent_jim", "ti", "Jim", "Jim"),
    )
    conn.execute(
        """
        INSERT INTO entities (entity_id, doc_id, canonical_source, canonical_target)
        VALUES (?, ?, ?, ?)
        """,
        ("ent_silver", "ti", "Silver", "Silver"),
    )
    conn.execute(
        """
        INSERT INTO entity_relations (
          relation_id, doc_id, source_entity_id, target_entity_id,
          relation_type, state_label, valid_from_block_id
        )
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        ("rel_1", "ti", "ent_jim", "ent_silver", "ally",
         "initial_trust", "ch02_b002"),
    )
    conn.commit()

    windows = build_windows(conn, "ti", ["ti_ch02"], target_tokens=1100)

    # ch02_b002 must be first in its window (phase boundary)
    for w in windows:
        if "ch02_b002" in w.block_ids:
            assert w.block_ids[0] == "ch02_b002"


def test_windower_dialogue_run_kept(tmp_path):
    """Consecutive dialogue blocks stay together."""
    blocks = [
        {"chapter_id": "ti_ch02", "block_id": "ch02_b001", "order_index": 0,
         "block_type": "dialogue", "clean_text": "Hello!", "source_text": "Hello!"},
        {"chapter_id": "ti_ch02", "block_id": "ch02_b002", "order_index": 1,
         "block_type": "dialogue", "clean_text": "Hi there.", "source_text": "Hi there."},
        {"chapter_id": "ti_ch02", "block_id": "ch02_b003", "order_index": 2,
         "block_type": "dialogue", "clean_text": "Good day.", "source_text": "Good day."},
        {"chapter_id": "ti_ch02", "block_id": "ch02_b004", "order_index": 3,
         "block_type": "dialogue", "clean_text": "Farewell.", "source_text": "Farewell."},
        {"chapter_id": "ti_ch02", "block_id": "ch02_b005", "order_index": 4,
         "block_type": "paragraph", "clean_text": "Narrative continues.", "source_text": "Narrative continues."},
    ]
    conn, _, _ = _make_doc_for_windower(tmp_path, blocks)

    windows = build_windows(conn, "ti", ["ti_ch02"], target_tokens=1100, max_blocks=8)

    # All 4 dialogues in one window
    for w in windows:
        if w.block_ids[0] == "ch02_b001":
            assert set(w.block_ids) == {
                "ch02_b001", "ch02_b002", "ch02_b003", "ch02_b004"
            }


def test_windower_max_blocks(tmp_path):
    """Windows cap at max_blocks even if tokens fit."""
    blocks = [
        {"chapter_id": "ti_ch02", "block_id": f"ch02_b{i:03d}", "order_index": i,
         "block_type": "paragraph", "clean_text": "A" * 100, "source_text": "A" * 100}
        for i in range(10)
    ]
    conn, _, _ = _make_doc_for_windower(tmp_path, blocks)

    windows = build_windows(conn, "ti", ["ti_ch02"], target_tokens=2000, max_blocks=4)

    for w in windows:
        assert len(w.block_ids) <= 4


def test_windower_deterministic(tmp_path):
    """Calling twice produces identical window plan."""
    blocks = [
        {"chapter_id": "ti_ch02", "block_id": f"ch02_b{i:03d}", "order_index": i,
         "block_type": "paragraph", "clean_text": "A" * 300, "source_text": "A" * 300}
        for i in range(6)
    ]
    conn, _, _ = _make_doc_for_windower(tmp_path, blocks)

    run1 = build_windows(conn, "ti", ["ti_ch02"], target_tokens=700, max_blocks=8)
    run2 = build_windows(conn, "ti", ["ti_ch02"], target_tokens=700, max_blocks=8)

    assert len(run1) == len(run2)
    for w1, w2 in zip(run1, run2):
        assert w1.window_id == w2.window_id
        assert w1.block_ids == w2.block_ids


def test_windower_window_ids_unique_and_sequential(tmp_path):
    """Multiple windows in one chapter must not reuse w_chXX_001."""
    blocks = [
        {"chapter_id": "ti_ch02", "block_id": f"ch02_b{i:03d}", "order_index": i,
         "block_type": "paragraph", "clean_text": "A" * 800, "source_text": "A" * 800}
        for i in range(6)
    ]
    conn, _, _ = _make_doc_for_windower(tmp_path, blocks)

    windows = build_windows(conn, "ti", ["ti_ch02"], target_tokens=250, max_blocks=8)
    window_ids = [w.window_id for w in windows]

    assert len(window_ids) == len(set(window_ids))
    assert window_ids == [f"w_ch02_{index:03d}" for index in range(1, 7)]


def test_windower_empty_chapter(tmp_path):
    """Requesting a chapter not in DB returns empty list."""
    blocks = [
        {"chapter_id": "ti_ch02", "block_id": "ch02_b001", "order_index": 0,
         "block_type": "paragraph", "clean_text": "A", "source_text": "A"},
    ]
    conn, _, _ = _make_doc_for_windower(tmp_path, blocks)

    windows = build_windows(conn, "ti", ["nonexistent_ch"])
    assert windows == []
