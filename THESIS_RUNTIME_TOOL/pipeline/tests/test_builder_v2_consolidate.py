from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from pipeline.memory.store_init import init_db
from pipeline.prepass.builder_v2_consolidate import (
    NOTEBOOK_STATUS_CONFLICT,
    Notebook,
    apply_builder_output,
    notebook_to_canonical_json,
)
from pipeline.scripts import builder_v2_consolidate_sim as sim


def _new(source: str, target: str, block: str = "b1", count: int = 1) -> dict:
    return {
        "source_term": source,
        "canonical_target_vi": target,
        "term_type": "term",
        "do_not_translate": False,
        "evidence_block_ids": [block],
        "occurrence_count": count,
    }


def test_created_entry_and_seen_existing_log():
    notebook = Notebook()
    apply_builder_output(
        notebook,
        {"new_terms": [_new("feature", "đặc trưng")]},
        window_id="w1",
        block_types_by_id={"b1": "prose"},
    )
    apply_builder_output(
        notebook,
        {"seen_existing_terms": [{"source_term": "feature", "evidence_block_ids": ["b2"]}]},
        window_id="w2",
        block_types_by_id={"b2": "prose"},
    )

    entry = notebook.entries["feature"]
    assert entry.canonical_source_term == "feature"
    assert entry.occurrences_total == 2
    assert [item.action for item in notebook.decision_log] == ["created", "seen_existing"]


def test_number_variant_merges_and_keeps_source_surfaces():
    notebook = Notebook()
    apply_builder_output(
        notebook,
        {"new_terms": [_new("feature", "đặc trưng", "b1", 1)]},
        window_id="w1",
        block_types_by_id={"b1": "prose"},
    )
    apply_builder_output(
        notebook,
        {"new_terms": [_new("features", "các đặc trưng", "b2", 3)]},
        window_id="w2",
        block_types_by_id={"b2": "prose"},
    )

    assert list(notebook.entries) == ["feature"]
    entry = notebook.entries["feature"]
    assert {variant.surface for variant in entry.source_variants} == {"feature", "features"}
    assert entry.occurrences_total == 4
    assert any(item.action == "merged_by_concept_key" for item in notebook.decision_log)
    assert any(conflict.type == "plural_only_difference" for conflict in entry.conflict_ledger)


def test_stoplist_rejects_plain_singleton_but_keeps_heading():
    rejected = Notebook()
    apply_builder_output(
        rejected,
        {"new_terms": [_new("set", "tập hợp", "b1")]},
        window_id="w1",
        block_types_by_id={"b1": "prose"},
    )
    assert "set" not in rejected.entries
    assert rejected.rejected_terms[0]["source_term"] == "set"
    assert rejected.decision_log[0].action == "rejected_stoplist"

    kept = Notebook()
    apply_builder_output(
        kept,
        {"new_terms": [_new("set", "tập hợp", "b2")]},
        window_id="w2",
        block_types_by_id={"b2": "heading"},
    )
    assert "set" in kept.entries


def test_updates_add_variants_without_mutating_canonical():
    notebook = Notebook()
    apply_builder_output(
        notebook,
        {"new_terms": [_new("activation function", "hàm kích hoạt", "b1")]},
        window_id="w1",
        block_types_by_id={"b1": "prose"},
    )
    apply_builder_output(
        notebook,
        {
            "updates_to_existing": [
                {
                    "source_term": "activation function",
                    "source_variants": ["activation functions"],
                    "target_variants": [
                        {
                            "text": "chức năng kích hoạt",
                            "evidence_block_id": "b2",
                            "variant_reason": "style variant in context",
                        }
                    ],
                    "evidence_block_ids": ["b2"],
                    "occurrence_count": 2,
                }
            ]
        },
        window_id="w2",
    )

    entry = notebook.entries["activation function"]
    assert entry.canonical_target_vi == "hàm kích hoạt"
    assert {variant.surface for variant in entry.source_variants} == {
        "activation function",
        "activation functions",
    }
    assert [variant.text for variant in entry.target_variants] == ["chức năng kích hoạt"]
    assert {item.action for item in notebook.decision_log} >= {
        "updated_source_variant",
        "updated_target_variant",
    }


def test_loss_polysemy_sets_conflict_pending_without_target_mix():
    notebook = Notebook()
    apply_builder_output(
        notebook,
        {"new_terms": [_new("loss", "hàm mất mát", "b1")]},
        window_id="w1",
        block_types_by_id={"b1": "definition"},
    )
    apply_builder_output(
        notebook,
        {"new_terms": [_new("losses", "giá trị mất mát", "b2")]},
        window_id="w2",
        block_types_by_id={"b2": "prose"},
    )

    entry = notebook.entries["loss"]
    assert entry.status == NOTEBOOK_STATUS_CONFLICT
    assert entry.target_variants == []
    assert [conflict.type for conflict in entry.conflict_ledger] == ["polysemy_suspected"]
    assert any(item.action == "conflict_logged" for item in notebook.decision_log)


def test_determinism_same_inputs_same_bytes():
    def build() -> str:
        notebook = Notebook()
        apply_builder_output(
            notebook,
            {
                "new_terms": [
                    _new("feature", "đặc trưng", "b1"),
                    _new("features", "các đặc trưng", "b2"),
                ]
            },
            window_id="w",
            block_types_by_id={"b1": "prose", "b2": "prose"},
        )
        return notebook_to_canonical_json(notebook)

    assert build() == build()


def test_offline_sim_integration_conserves_occurrence_and_reduces_entries(tmp_path: Path):
    db_path = tmp_path / "memory.sqlite3"
    conn = init_db(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute(
        """
        INSERT INTO documents (doc_id, job_id, source_filename, metadata_json)
        VALUES ('d2l', 'job', 'source', '{}')
        """
    )
    conn.executemany(
        """
        INSERT INTO blocks (block_id, doc_id, order_index, chapter_id, block_type, text, original_text, translation_mode)
        VALUES (?, 'd2l', ?, 'chapter', ?, ?, ?, 'translate')
        """,
        [
            ("b1", 1, "prose", "feature", "feature"),
            ("b2", 2, "prose", "features", "features"),
        ],
    )
    conn.executemany(
        """
        INSERT INTO glossary_entries (
          glossary_id, doc_id, source_term, target_term, allowed_variants_json,
          evidence_span_ids_json, occurrences_count
        )
        VALUES (?, 'd2l', ?, ?, '[]', ?, ?)
        """,
        [
            ("gl_feature", "feature", "đặc trưng", json.dumps(["b1"]), 1),
            ("gl_features", "features", "các đặc trưng", json.dumps(["b2"]), 2),
        ],
    )
    conn.commit()
    conn.close()

    conn = sim._connect_ro(db_path)
    conn.row_factory = sqlite3.Row
    try:
        rows = sim._load_registry_rows(conn, "d2l")
        block_meta = sim._load_block_meta(conn, "d2l")
    finally:
        conn.close()
    notebook = Notebook()
    for index, row in enumerate(
        sorted(rows, key=lambda item: sim._first_evidence_order(item["evidence_block_ids"], block_meta))
    ):
        sim.apply_builder_output(
            notebook,
            {"new_terms": [sim._row_to_new_term(row)]},
            window_id=sim._window_id(row, index),
            block_types_by_id={
                block_id: str(block_meta[block_id]["block_type"])
                for block_id in row["evidence_block_ids"]
                if block_id in block_meta
            },
        )
    report = sim._build_report(
        rows=rows,
        notebook=notebook,
        db_path=db_path,
        doc_id="d2l",
        db_sha256=sim._sha256(db_path),
    )

    assert report["entries"]["before"] == 2
    assert report["entries"]["after_notebook"] == 1
    assert report["conservation"]["occurrence_conserved"] is True
