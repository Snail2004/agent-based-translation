from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from pipeline.memory.store_init import init_db
from pipeline.retrieval import context_builder as cb
from pipeline.retrieval.context_builder import build_context_pack, plan_anchors
from pipeline.translate.prompt import build_messages
from pipeline.translate.windower import Window


def _fixture_db(tmp_path: Path) -> sqlite3.Connection:
    conn = init_db(tmp_path / "memory.sqlite3")
    conn.execute(
        """
        INSERT INTO documents (doc_id, job_id, source_filename, metadata_json)
        VALUES ('doc1', 'job1', 'source.txt', '{}')
        """
    )
    blocks = [
        (
            "b1",
            "doc1",
            1,
            "ch01",
            "paragraph",
            "Rum, not rumor, lay beside the sailor’s clasp-knife as Jim met the captain.",
        ),
        (
            "b2",
            "doc1",
            2,
            "ch01",
            "dialogue",
            "Jim said good day to the captain.",
        ),
        (
            "b3",
            "doc1",
            3,
            "ch01",
            "paragraph",
            "Jim watched the captain drink rum near the cove and the cutlass.",
        ),
    ]
    conn.executemany(
        """
        INSERT INTO blocks (block_id, doc_id, order_index, chapter_id, block_type, text)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        blocks,
    )
    terms = [
        ("gl_rum", "doc1", "rum", "rượu rum", 0),
        ("gl_clasp", "doc1", "sailor's clasp-knife", "dao xếp thủy thủ", 0),
        ("gl_cove", "doc1", "cove", "vịnh nhỏ", 0),
        ("gl_cutlass", "doc1", "cutlass", "kiếm cong", 0),
    ]
    conn.executemany(
        """
        INSERT INTO glossary_entries (
          glossary_id, doc_id, source_term, target_term, do_not_translate
        )
        VALUES (?, ?, ?, ?, ?)
        """,
        terms,
    )
    entities = [
        (
            "ent_jim",
            "doc1",
            "Jim",
            "Jim",
            json.dumps(["Jim Hawkins"]),
            json.dumps(["Jim"]),
        ),
        (
            "ent_captain",
            "doc1",
            "Billy Bones",
            "thuyền trưởng",
            json.dumps(["captain", "the captain", "Billy"]),
            json.dumps(["thuyền trưởng"]),
        ),
    ]
    conn.executemany(
        """
        INSERT INTO entities (
          entity_id, doc_id, canonical_source, canonical_target,
          aliases_source_json, aliases_target_json
        )
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        entities,
    )
    relations = [
        (
            "rel_wary",
            "doc1",
            "ent_jim",
            "ent_captain",
            "wary_contact",
            "wary",
            "b1",
            "b2",
            json.dumps({"a_to_b": "ông", "b_to_a": "cậu"}),
        ),
        (
            "rel_tense",
            "doc1",
            "ent_jim",
            "ent_captain",
            "tense_contact",
            "tense",
            "b3",
            None,
            json.dumps({"a_to_b": "ông", "b_to_a": "Jim"}),
        ),
    ]
    conn.executemany(
        """
        INSERT INTO entity_relations (
          relation_id, doc_id, source_entity_id, target_entity_id, relation_type,
          state_label, valid_from_block_id, valid_to_block_id, address_policy_json
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        relations,
    )
    conn.commit()
    return conn


def _blocks(conn: sqlite3.Connection, block_ids: list[str]) -> list[dict]:
    placeholders = ",".join("?" * len(block_ids))
    rows = conn.execute(
        f"""
        SELECT block_id, doc_id, chapter_id, order_index, block_type,
               text AS clean_text, original_text AS source_text
        FROM blocks
        WHERE block_id IN ({placeholders})
        ORDER BY order_index
        """,
        block_ids,
    ).fetchall()
    return [dict(row) for row in rows]


def test_anchor_scan(tmp_path):
    conn = _fixture_db(tmp_path)
    anchors = plan_anchors(conn, _blocks(conn, ["b1", "b2"]))

    assert anchors.term_counts["gl_rum"] == 1
    assert anchors.term_counts["gl_clasp"] == 1
    assert anchors.term_block_ids["gl_rum"] == ["b1"]
    assert anchors.entity_block_ids["ent_jim"] == ["b1", "b2"]
    assert anchors.entity_block_ids["ent_captain"] == ["b1", "b2"]
    assert anchors.has_dialogue is True


def test_no_registry_dump(tmp_path):
    conn = _fixture_db(tmp_path)
    for index in range(20):
        conn.execute(
            """
            INSERT INTO glossary_entries (glossary_id, doc_id, source_term, target_term)
            VALUES (?, 'doc1', ?, ?)
            """,
            (f"gl_extra_{index:02d}", f"navterm{index:02d}", f"thuật ngữ {index:02d}"),
        )
    conn.execute(
        """
        INSERT INTO blocks (block_id, doc_id, order_index, chapter_id, block_type, text)
        VALUES ('b4', 'doc1', 4, 'ch01', 'paragraph', 'navterm00 navterm05 navterm19')
        """
    )
    conn.commit()

    window = Window("w_ch01_004", ["b4"], 10)
    anchors = plan_anchors(conn, _blocks(conn, ["b4"]))
    pack = build_context_pack(conn, window, anchors)

    assert len(pack.glossary_lines) == 3
    assert all("navterm" in line for line in pack.glossary_lines)


def test_address_policy_active_state(tmp_path):
    conn = _fixture_db(tmp_path)
    window = Window("w_ch01_003", ["b3"], 10)
    anchors = plan_anchors(conn, _blocks(conn, ["b3"]))

    pack = build_context_pack(conn, window, anchors)

    assert pack.address_lines == [
        'Jim->Billy Bones: "ông", Billy Bones->Jim: "Jim" (tense)'
    ]


def test_budget_drop_priority(tmp_path):
    conn = _fixture_db(tmp_path)
    window = Window("w_ch01_003", ["b3"], 10)
    anchors = plan_anchors(conn, _blocks(conn, ["b3"]))

    pack = build_context_pack(conn, window, anchors, budget_tokens=3)

    assert pack.address_lines
    assert any(item.item_type == "term" for item in pack.dropped_by_budget)
    assert all(item.item_type != "address" for item in pack.dropped_by_budget)


def test_coverage_checker_flags(tmp_path, monkeypatch):
    conn = _fixture_db(tmp_path)
    window = Window("w_ch01_001", ["b1"], 10)
    anchors = plan_anchors(conn, _blocks(conn, ["b1"]))
    original = cb._build_context_pack_once

    def broken_once(conn_arg, window_arg, anchors_arg, budget_arg):
        pack, included = original(conn_arg, window_arg, anchors_arg, budget_arg)
        included.discard("term:gl_rum")
        return pack, included

    monkeypatch.setattr(cb, "_build_context_pack_once", broken_once)

    pack = build_context_pack(conn, window, anchors)

    assert pack.low_context is True
    assert any("coverage_missing" in warning for warning in pack.warnings)


def test_prompt_s1_contains_constraints_s0_unchanged(tmp_path):
    conn = _fixture_db(tmp_path)
    blocks = _blocks(conn, ["b1"])
    window = Window("w_ch01_001", ["b1"], 10)
    pack = build_context_pack(conn, window, plan_anchors(conn, blocks))

    s0_original = build_messages(blocks)
    s0_explicit = build_messages(blocks, config="S0")
    s1_messages = build_messages(blocks, config="S1", context_pack=pack)
    s1_user = s1_messages[1]["content"]

    assert s0_explicit == s0_original
    assert "MANDATORY TERMINOLOGY & NAMES" in s1_user
    assert "ADDRESS POLICY" in s1_user
    assert "rum -> rượu rum" in s1_user
    assert "SOURCE WINDOW" in s1_user
