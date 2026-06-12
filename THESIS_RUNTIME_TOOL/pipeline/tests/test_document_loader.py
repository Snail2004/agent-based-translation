from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from pipeline.ingest.document_loader import load_document


FIXTURE_PATH = Path(__file__).parent / "fixtures" / "mini_document.json"
REAL_SOURCE_PATH = Path("data/sources/treasure_island/document.json")


def _rows(db_path: Path):
    connection = sqlite3.connect(db_path)
    connection.row_factory = sqlite3.Row
    try:
        return [
            dict(row)
            for row in connection.execute(
                """
                SELECT block_id, doc_id, order_index, block_type, chapter_id,
                       text, original_text, style_json
                FROM blocks
                ORDER BY order_index
                """
            ).fetchall()
        ]
    finally:
        connection.close()


def _write_json(path: Path, data: dict) -> Path:
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def test_load_fixture_counts_and_mapping(tmp_path):
    db_path = tmp_path / "memory.sqlite3"
    report = load_document(db_path, FIXTURE_PATH)
    rows = _rows(db_path)
    paragraph = next(row for row in rows if row["block_id"] == "mini_ch01_b002")
    style = json.loads(paragraph["style_json"])

    assert report.doc_id == "mini_doc"
    assert report.chapters == 2
    assert report.blocks == 6
    assert report.warnings == ["mini_ch02_b003: empty clean_text for paragraph"]
    assert paragraph["text"] == "Một “quote” — và tiếng Việt có dấu."
    assert paragraph["original_text"] == "A “quote” — and Vietnamese dấu."
    assert paragraph["chapter_id"] == "mini_ch01"
    assert paragraph["block_type"] == "paragraph"
    assert style == {
        "is_chapter_opening": False,
        "page_ids": [1],
        "quality_flags": ["ok"],
    }


def test_global_order_monotonic(tmp_path):
    db_path = tmp_path / "memory.sqlite3"
    load_document(db_path, FIXTURE_PATH)
    rows = _rows(db_path)

    assert [row["order_index"] for row in rows] == list(range(6))
    assert [row["block_id"] for row in rows] == [
        "mini_ch01_b001",
        "mini_ch01_b002",
        "mini_ch01_b003",
        "mini_ch02_b001",
        "mini_ch02_b002",
        "mini_ch02_b003",
    ]


def test_idempotent_reload(tmp_path):
    db_path = tmp_path / "memory.sqlite3"
    first = load_document(db_path, FIXTURE_PATH)
    rows_first = _rows(db_path)
    second = load_document(db_path, FIXTURE_PATH)
    rows_second = _rows(db_path)

    assert first == second
    assert rows_first == rows_second


def test_duplicate_block_id_raises(tmp_path):
    data = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
    data["chapters"][1]["blocks"][0]["block_id"] = "mini_ch01_b001"
    bad_path = _write_json(tmp_path / "duplicate.json", data)

    with pytest.raises(ValueError, match="Duplicate block_id"):
        load_document(tmp_path / "memory.sqlite3", bad_path)


def test_unstripped_annotations_raises(tmp_path):
    data = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
    data["chapters"][0]["blocks"][1]["annotations"] = {"term_occurrences": ["g_001"]}
    bad_path = _write_json(tmp_path / "unstripped.json", data)

    with pytest.raises(ValueError, match="still has annotations"):
        load_document(tmp_path / "memory.sqlite3", bad_path)


@pytest.mark.skipif(
    not REAL_SOURCE_PATH.exists(),
    reason="Prepared Treasure Island source is not present",
)
def test_load_real_treasure_island(tmp_path):
    source = json.loads(REAL_SOURCE_PATH.read_text(encoding="utf-8"))
    expected_text = None
    for chapter in source["chapters"]:
        for block in chapter["blocks"]:
            if block["block_id"] == "treasure_island_ch02_b003":
                expected_text = block["clean_text"]
                break
        if expected_text:
            break
    assert expected_text is not None

    db_path = tmp_path / "memory.sqlite3"
    report = load_document(db_path, REAL_SOURCE_PATH)
    rows = _rows(db_path)
    target_row = next(
        row for row in rows if row["block_id"] == "treasure_island_ch02_b003"
    )

    assert report.chapters == 40
    assert report.blocks == 1476
    assert target_row["text"] == expected_text
