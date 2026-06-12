from __future__ import annotations

import json
import sqlite3

import pytest

from pipeline.prepass.persist import build_memory
from pipeline.prepass.prompt import build_messages


def _write_json(path, data):
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def _document():
    return {
        "schema_version": "1.5.0",
        "doc_id": "mini_doc",
        "metadata": {"source_language": "en", "target_language": "vi"},
        "chapters": [
            {
                "chapter_id": "mini_ch01",
                "blocks": [
                    {
                        "block_id": "mini_ch01_b001",
                        "order_index": 0,
                        "block_type": "heading",
                        "clean_text": "Chapter 1",
                        "source_text": "Chapter 1",
                        "annotations": {},
                    },
                    {
                        "block_id": "mini_ch01_b002",
                        "order_index": 1,
                        "block_type": "paragraph",
                        "clean_text": "Rum brought Jim Hawkins to Captain Flint.",
                        "source_text": "Rum brought Jim Hawkins to Captain Flint.",
                        "annotations": {},
                    },
                    {
                        "block_id": "mini_ch01_b003",
                        "order_index": 2,
                        "block_type": "paragraph",
                        "clean_text": "Jim feared the captain.",
                        "source_text": "Jim feared the captain.",
                        "annotations": {},
                    },
                ],
            },
            {
                "chapter_id": "mini_ch02",
                "blocks": [
                    {
                        "block_id": "mini_ch02_b001",
                        "order_index": 0,
                        "block_type": "heading",
                        "clean_text": "Chapter 2",
                        "source_text": "Chapter 2",
                        "annotations": {},
                    },
                    {
                        "block_id": "mini_ch02_b002",
                        "order_index": 1,
                        "block_type": "paragraph",
                        "clean_text": "Billy met Jim by the cove.",
                        "source_text": "Billy met Jim by the cove.",
                        "annotations": {},
                    },
                ],
            },
        ],
    }


def _artifact_ch01():
    return {
        "chapter_id": "mini_ch01",
        "glossary_candidates": [
            {
                "source_term": "rum",
                "proposed_target_vi": "rượu rum",
                "do_not_translate": False,
                "category": "cultural",
                "block_ids": ["mini_ch01_b002"],
            },
            {
                "source_term": "Captain Flint",
                "proposed_target_vi": "Thuyền trưởng Flint",
                "do_not_translate": False,
                "category": "other",
                "block_ids": ["mini_ch01_b002"],
            },
        ],
        "entities": [
            {
                "entity_id": "ent_narrator",
                "canonical_source": "Jim Hawkins / first-person narrator",
                "aliases_source": ["I", "me", "my", "Jim", "Jim Hawkins"],
                "entity_type": "person",
                "proposed_target_vi": "Jim Hawkins",
                "aliases_target_vi": ["Jim"],
            },
            {
                "entity_id": "ent_captain",
                "canonical_source": "the captain",
                "aliases_source": ["captain", "the captain"],
                "entity_type": "person",
                "proposed_target_vi": "thuyền trưởng",
                "aliases_target_vi": ["thuyền trưởng"],
            },
        ],
        "relations": [
            {
                "a": "ent_narrator",
                "b": "ent_captain",
                "relation": "wary_contact",
                "address_a_to_b_vi": "ông",
                "address_b_to_a_vi": "cậu",
                "state_label": "wary",
                "trigger_block_id": "mini_ch01_b002",
                "notes": "initial wary state",
            }
        ],
        "mention_surfaces": [
            {"entity_id": "ent_narrator", "surfaces": ["Jim Hawkins", "Jim", "I"]},
            {"entity_id": "ent_captain", "surfaces": ["captain", "the captain"]},
        ],
        "chapter_summary_vi": "Jim gặp thuyền trưởng và rum xuất hiện.",
        "motifs": [{"note": "Rum báo hiệu nguy hiểm.", "block_ids": ["mini_ch01_b002"]}],
    }


def _artifact_ch02():
    return {
        "chapter_id": "mini_ch02",
        "glossary_candidates": [
            {
                "source_term": "cove",
                "proposed_target_vi": "vịnh nhỏ",
                "do_not_translate": False,
                "category": "place",
                "block_ids": ["mini_ch02_b002"],
            }
        ],
        "entities": [
            {
                "entity_id": "ent_narrator",
                "canonical_source": "Jim Hawkins",
                "aliases_source": ["Jim"],
                "entity_type": "person",
                "proposed_target_vi": "Jim Hawkins",
                "aliases_target_vi": ["Jim"],
            },
            {
                "entity_id": "ent_captain",
                "canonical_source": "Billy",
                "aliases_source": ["Billy", "Bill"],
                "entity_type": "person",
                "proposed_target_vi": "Billy",
                "aliases_target_vi": ["Billy"],
            },
        ],
        "relations": [
            {
                "a": "ent_narrator",
                "b": "ent_captain",
                "relation": "care_under_pressure",
                "address_a_to_b_vi": "ông",
                "address_b_to_a_vi": "Jim",
                "state_label": "crisis_care",
                "trigger_block_id": "mini_ch02_b002",
                "notes": "later crisis state",
            }
        ],
        "mention_surfaces": [
            {"entity_id": "ent_narrator", "surfaces": ["Jim"]},
            {"entity_id": "ent_captain", "surfaces": ["Billy", "Bill"]},
        ],
        "chapter_summary_vi": "Billy gặp Jim gần vịnh.",
        "motifs": [{"note": "Tên mới của thuyền trưởng lộ ra.", "block_ids": ["mini_ch02_b002"]}],
    }


def _fixture_paths(tmp_path):
    document_path = _write_json(tmp_path / "document.json", _document())
    prepass_dir = tmp_path / "prepass"
    prepass_dir.mkdir()
    _write_json(prepass_dir / "mini_ch01.json", _artifact_ch01())
    _write_json(prepass_dir / "mini_ch02.json", _artifact_ch02())
    return document_path, prepass_dir


def _connect(db_path):
    connection = sqlite3.connect(db_path)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


def test_persist_mapping(tmp_path):
    document_path, prepass_dir = _fixture_paths(tmp_path)
    db_path = tmp_path / "memory.sqlite3"

    report = build_memory(db_path, document_path, prepass_dir, freeze=False)

    assert report.doc_id == "mini_doc"
    assert report.glossary == 3
    assert report.entities == 2
    assert report.mentions >= 4
    assert report.relations == 2
    assert report.memory_items == 4
    with _connect(db_path) as connection:
        glossary = {
            row["source_term"]: dict(row)
            for row in connection.execute("SELECT * FROM glossary_entries")
        }
        narrator = connection.execute(
            "SELECT * FROM entities WHERE entity_id = 'ent_narrator'"
        ).fetchone()
        mentions = connection.execute("SELECT * FROM mentions").fetchall()
        memory_items = connection.execute("SELECT * FROM memory_items").fetchall()

    assert glossary["rum"]["target_term"] == "rượu rum"
    assert glossary["rum"]["status"] == "approved"
    assert glossary["rum"]["occurrences_count"] == 1
    assert narrator["canonical_source"] == "Jim Hawkins"
    assert "I" not in json.loads(narrator["aliases_source_json"])
    assert "me" not in json.loads(narrator["aliases_source_json"])
    assert {row["mention_type"] for row in mentions} == {"name"}
    assert {row["status"] for row in memory_items} == {"approved"}


def test_relations_timeline(tmp_path):
    document_path, prepass_dir = _fixture_paths(tmp_path)
    db_path = tmp_path / "memory.sqlite3"
    build_memory(db_path, document_path, prepass_dir, freeze=False)

    with _connect(db_path) as connection:
        rows = [
            dict(row)
            for row in connection.execute(
                """
                SELECT state_label, valid_from_block_id, valid_to_block_id,
                       address_policy_json
                FROM entity_relations
                ORDER BY valid_from_block_id
                """
            )
        ]

    assert rows[0]["state_label"] == "wary"
    assert rows[0]["valid_from_block_id"] == "mini_ch01_b002"
    assert rows[0]["valid_to_block_id"] == "mini_ch02_b001"
    assert rows[1]["state_label"] == "crisis_care"
    assert rows[1]["valid_from_block_id"] == "mini_ch02_b002"
    assert rows[1]["valid_to_block_id"] is None
    assert json.loads(rows[1]["address_policy_json"]) == {"a_to_b": "Jim", "b_to_a": "ông"}


def test_freeze_blocks_writes(tmp_path):
    document_path, prepass_dir = _fixture_paths(tmp_path)
    db_path = tmp_path / "memory.sqlite3"
    build_memory(db_path, document_path, prepass_dir, freeze=True)

    insert_checks = [
        """
        INSERT INTO glossary_entries (
          glossary_id, doc_id, source_term, target_term
        ) VALUES ('gl_new', 'mini_doc', 'new', 'moi')
        """,
        """
        INSERT INTO entities (
          entity_id, doc_id, canonical_source
        ) VALUES ('ent_new', 'mini_doc', 'New')
        """,
        """
        INSERT INTO mentions (
          mention_id, doc_id, block_id, surface
        ) VALUES ('m_new', 'mini_doc', 'mini_ch01_b002', 'New')
        """,
        """
        INSERT INTO entity_relations (
          relation_id, doc_id, source_entity_id, target_entity_id, relation_type
        ) VALUES ('rel_new', 'mini_doc', 'ent_narrator', 'ent_captain', 'test')
        """,
        """
        INSERT INTO memory_items (
          memory_id, doc_id, memory_type, scope, content
        ) VALUES ('mi_new', 'mini_doc', 'motif', 'chapter', 'new')
        """,
    ]
    update_checks = [
        "UPDATE glossary_entries SET status = 'candidate'",
        "UPDATE entities SET status = 'candidate'",
        "UPDATE mentions SET confidence = 0.1",
        "UPDATE entity_relations SET confidence = 0.1",
        "UPDATE memory_items SET status = 'candidate'",
    ]
    delete_checks = [
        "DELETE FROM glossary_entries WHERE glossary_id = 'gl_rum'",
        "DELETE FROM entities WHERE entity_id = 'ent_captain'",
        "DELETE FROM mentions WHERE mention_id IN (SELECT mention_id FROM mentions LIMIT 1)",
        "DELETE FROM entity_relations WHERE relation_id IN (SELECT relation_id FROM entity_relations LIMIT 1)",
        "DELETE FROM memory_items WHERE memory_id IN (SELECT memory_id FROM memory_items LIMIT 1)",
    ]
    with _connect(db_path) as connection:
        for statement in [*insert_checks, *update_checks, *delete_checks]:
            with pytest.raises(sqlite3.IntegrityError, match="memory frozen"):
                connection.execute(statement)
            connection.rollback()

        connection.execute(
            """
            INSERT INTO translation_runs (
              run_id, experiment_id, doc_id, block_id, config, stage
            ) VALUES ('run1', 'exp1', 'mini_doc', 'mini_ch01_b002', 'S0', 'draft')
            """
        )
        connection.commit()


def test_build_refuses_frozen_db(tmp_path):
    document_path, prepass_dir = _fixture_paths(tmp_path)
    db_path = tmp_path / "memory.sqlite3"
    build_memory(db_path, document_path, prepass_dir, freeze=True)

    with pytest.raises(RuntimeError, match="already frozen"):
        build_memory(db_path, document_path, prepass_dir, freeze=True)


def test_prompt_no_external_knowledge():
    chapter = {
        "chapter_id": "mini_ch01",
        "blocks": [
            {
                "block_id": "mini_ch01_b001",
                "clean_text": "I watched the old captain from the inn door.",
            }
        ],
    }
    messages = build_messages(chapter, "(registry empty)")
    combined = "\n".join(message["content"] for message in messages)

    assert "Treasure Island" not in combined
    assert "Jim" not in combined
    assert "Hawkins" not in combined
    assert "ent_narrator" in combined
    assert "Re-emit" in combined or "re-emit" in combined
    assert "plain pronouns" in combined
