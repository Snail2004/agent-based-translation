from __future__ import annotations

import json
import sqlite3
import tempfile
from pathlib import Path

import pytest

from pipeline.eval.thesis_scoring import (
    build_ruler_from_db_and_spans,
    normalize_apostrophe,
    score_thesis_translations,
    _block_chapter,
    _load_terms,
    _load_entities,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _make_thesis_db(tmp_path: Path | None = None) -> tuple[sqlite3.Connection, Path]:
    """In-memory thesis DB with terms, entities, and blocks (for FK constraints)."""
    from pipeline.memory.store_init import init_db
    import tempfile as _tmp

    if tmp_path is None:
        t = _tmp.NamedTemporaryFile(suffix=".sqlite3", delete=False)
        t.close()
        db_path = Path(t.name)
    else:
        db_path = tmp_path / "thesis_db.sqlite3"

    conn = init_db(str(db_path))

    conn.execute(
        "INSERT INTO documents (doc_id, job_id, source_lang, target_lang) "
        "VALUES ('ti', 'ti', 'en', 'vi')"
    )

    # Minimal blocks so translation_runs FK passes
    block_data = [
        ("ch02_b001", "The rum was excellent."),
        ("ch02_b002", "A clasp\u2019knife gleamed."),
        ("ch02_b003", "Jim Hawkins watched from the deck."),
        ("ch02_b004", "Hispaniola sailed away."),
    ]
    for idx, (bid, btext) in enumerate(block_data):
        conn.execute(
            "INSERT INTO blocks "
            "(block_id, doc_id, order_index, chapter_id, text, original_text) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (bid, "ti", idx, "ti_ch02", btext, btext),
        )

    # Terms with apostrophe variations (P2-02 follow-up)
    conn.execute(
        "INSERT INTO glossary_entries "
        "(glossary_id, doc_id, source_term, target_term, term_type, "
        "do_not_translate, allowed_variants_json) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        ("gl_rum", "ti", "rum", "rượu rum", "nautical", 0, '["rượu rum"]'),
    )
    conn.execute(
        "INSERT INTO glossary_entries "
        "(glossary_id, doc_id, source_term, target_term, term_type, "
        "do_not_translate, allowed_variants_json) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        ("gl_clasp", "ti", "clasp\u2019knife", "dao gấp", "object", 0, '["dao gấp"]'),
    )
    conn.execute(
        "INSERT INTO glossary_entries "
        "(glossary_id, doc_id, source_term, target_term, term_type, "
        "do_not_translate, allowed_variants_json) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        ("gl_hispaniola", "ti", "Hispaniola", "Hispaniola", "place", 1, "[]"),
    )

    # Entities
    conn.execute(
        "INSERT INTO entities "
        "(entity_id, doc_id, canonical_source, canonical_target, entity_type, "
        "aliases_source_json, aliases_target_json) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        ("ent_jim", "ti", "Jim Hawkins", "Jim Hawkins", "person",
         '["Jim"]', '["Jim"]'),
    )
    conn.execute(
        "INSERT INTO entities "
        "(entity_id, doc_id, canonical_source, canonical_target, entity_type, "
        "aliases_source_json, aliases_target_json) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        ("ent_silver", "ti", "Long John Silver", "Long John Silver", "person",
         '["Silver","Long John"]', '["Silver","Long John"]'),
    )

    conn.commit()
    return conn, db_path


def _make_mini_document(tmp_path: Path) -> Path:
    doc = {
        "doc_id": "ti",
        "chapters": [
            {
                "chapter_id": "ti_ch02",
                "blocks": [
                    {
                        "block_id": "ch02_b001",
                        "order_index": 0,
                        "block_type": "paragraph",
                        "source_text": "The rum was excellent.",
                        "clean_text": "The rum was excellent.",
                    },
                    {
                        "block_id": "ch02_b002",
                        "order_index": 1,
                        "block_type": "paragraph",
                        "source_text": "A clasp\u2019knife gleamed in the sun.",
                        "clean_text": "A clasp\u2019knife gleamed in the sun.",
                    },
                    {
                        "block_id": "ch02_b003",
                        "order_index": 2,
                        "block_type": "paragraph",
                        "source_text": "Jim Hawkins watched from the deck.",
                        "clean_text": "Jim Hawkins watched from the deck.",
                    },
                    {
                        "block_id": "ch02_b004",
                        "order_index": 3,
                        "block_type": "paragraph",
                        "source_text": "Hispaniola sailed away.",
                        "clean_text": "Hispaniola sailed away.",
                    },
                ],
            },
        ],
    }
    path = tmp_path / "document.json"
    path.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")
    return path


def _make_prepass_artifacts(tmp_path: Path) -> Path:
    # Create a subdirectory so document.json stays separate from glob results
    prepass_dir = tmp_path / "prepass"
    prepass_dir.mkdir()
    artifact = {
        "chapter_id": "ti_ch02",
        "glossary_candidates": [
            {"source_term": "rum", "proposed_target_vi": "rượu rum",
             "do_not_translate": False, "category": "nautical", "block_ids": ["ch02_b001"]},
            {"source_term": "clasp'knife", "proposed_target_vi": "dao gấp",
             "do_not_translate": False, "category": "object", "block_ids": ["ch02_b002"]},
            {"source_term": "Hispaniola", "proposed_target_vi": "Hispaniola",
             "do_not_translate": True, "category": "place", "block_ids": ["ch02_b003"]},
        ],
        "entities": [
            {"entity_id": "ent_jim", "canonical_source": "Jim Hawkins",
             "aliases_source": ["Jim"], "entity_type": "person",
             "proposed_target_vi": "Jim Hawkins", "aliases_target_vi": ["Jim"]},
        ],
        "relations": [],
        "mention_surfaces": [
            {"entity_id": "ent_jim", "surfaces": ["Jim", "Jim Hawkins"]},
        ],
        "chapter_summary_vi": "Chuong 2.",
        "motifs": [],
    }
    path = prepass_dir / "ti_ch02.json"
    path.write_text(json.dumps(artifact, ensure_ascii=False), encoding="utf-8")
    return prepass_dir


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_normalize_apostrophe():
    assert normalize_apostrophe("clasp\u2019knife") == "clasp'knife"
    assert normalize_apostrophe("Jim\u2019s") == "Jim's"
    assert normalize_apostrophe("plain text") == "plain text"


def test_load_terms():
    conn, _ = _make_thesis_db()
    terms = _load_terms(conn)
    conn.close()

    assert "gl_rum" in terms
    assert "gl_hispaniola" in terms
    assert terms["gl_rum"]["source_term"] == "rum"
    assert terms["gl_hispaniola"]["do_not_translate"] is True


def test_load_entities():
    conn, _ = _make_thesis_db()
    entities = _load_entities(conn)
    conn.close()

    assert "ent_jim" in entities
    assert entities["ent_jim"]["canonical_source"] == "Jim Hawkins"
    assert entities["ent_jim"]["canonical_target"] == "Jim Hawkins"


def test_block_chapter():
    assert _block_chapter("ch02_b001") == "ch02"
    assert _block_chapter("ti_ch03_b010") == "ch03"
    assert _block_chapter("treasure_island_ch05_b001") == "ch05"
    assert _block_chapter("unknown") == "unknown"


def test_build_ruler_from_db_and_spans(tmp_path):
    """build_ruler_from_db_and_spans produces complete ruler data."""
    _, db_path = _make_thesis_db(tmp_path)
    doc_path = _make_mini_document(tmp_path)
    prepath = _make_prepass_artifacts(tmp_path)

    ruler = build_ruler_from_db_and_spans(db_path, prepath, doc_path)

    assert "terms" in ruler
    assert "entities" in ruler
    assert "term_occurrences_by_block" in ruler
    assert "entity_mentions_by_block" in ruler
    assert "block_chapters" in ruler

    occs = ruler["term_occurrences_by_block"]
    assert "ch02_b001" in occs
    assert "ch02_b002" in occs
    assert occs["ch02_b001"] == ["gl_rum"]
    assert "gl_clasp" in occs["ch02_b002"]
    assert "rum" not in occs["ch02_b001"]

    mentions = ruler["entity_mentions_by_block"]
    assert "ch02_b003" in mentions  # Jim Hawkins lives here


def test_score_thesis_translations(tmp_path):
    """Full scoring pipeline: load registry, span, then score."""
    _, db_path = _make_thesis_db(tmp_path)
    doc_path = _make_mini_document(tmp_path)
    prepath = _make_prepass_artifacts(tmp_path)

    from pipeline.memory.store_init import init_db
    conn2 = init_db(str(db_path))

    translations = {
        "ch02_b001": "Rượu rum thật tuyệt.",
        "ch02_b002": "Con dao gấp lấp lánh dưới nắng.",
        "ch02_b003": "Jim Hawkins đứng trên boong tàu.",
        "ch02_b004": "Tàu Hispaniola rời đi.",
    }
    for block_id, text in translations.items():
        conn2.execute(
            "INSERT INTO translation_runs "
            "(run_id, experiment_id, doc_id, block_id, config, stage, "
            "output_text, model, prompt_version) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (f"tr_S0_{block_id}", "exp_pilot", "ti", block_id, "S0", "draft",
             text, "gpt-5.4-mini", "s0_v1"),
        )
    conn2.commit()
    conn2.close()

    report = score_thesis_translations(
        db_path=str(db_path),
        experiment_id="exp_pilot",
        config="S0",
        prepass_dir=str(prepath),
        source_document_path=str(doc_path),
    )

    assert "tar" in report
    assert "ecs" in report
    assert "fvr" in report
    assert report["tar"]["pairs"] >= 3
    assert report["tar"]["overall"] == pytest.approx(1.0)
    assert 0.0 <= report["ecs"]["overall"] <= 1.0


def test_apostrophe_matching_survives_at_ruler_provider(tmp_path):
    """Artifact straight apostrophe and source curly apostrophe still map to glossary_id."""
    _, db_path = _make_thesis_db(tmp_path)
    doc_path = _make_mini_document(tmp_path)
    prepath = _make_prepass_artifacts(tmp_path)

    ruler = build_ruler_from_db_and_spans(db_path, prepath, doc_path)

    assert "gl_clasp" in ruler["term_occurrences_by_block"]["ch02_b002"]


def test_score_same_ruler_consistency(tmp_path):
    """Both S0 and oracle on same ruler gives comparable scores."""
    _, db_path = _make_thesis_db(tmp_path)
    doc_path = _make_mini_document(tmp_path)
    prepath = _make_prepass_artifacts(tmp_path)

    from pipeline.memory.store_init import init_db
    conn2 = init_db(str(db_path))

    # S0 translation: misses rum target, has clasp-knife
    conn2.execute(
        "INSERT INTO translation_runs "
        "(run_id, experiment_id, doc_id, block_id, config, stage, "
        "output_text, model, prompt_version) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        ("tr_S0_ch02_b001", "exp_pilot", "ti", "ch02_b001", "S0", "draft",
         "Thứ rượu này ngon.", "gpt-5.4-mini", "s0_v1"),
    )
    conn2.execute(
        "INSERT INTO translation_runs "
        "(run_id, experiment_id, doc_id, block_id, config, stage, "
        "output_text, model, prompt_version) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        ("tr_S0_ch02_b002", "exp_pilot", "ti", "ch02_b002", "S0", "draft",
         "Con dao gấp lấp lánh.", "gpt-5.4-mini", "s0_v1"),
    )
    conn2.commit()
    conn2.close()

    report = score_thesis_translations(
        db_path=str(db_path),
        experiment_id="exp_pilot",
        config="S0",
        prepass_dir=str(prepath),
        source_document_path=str(doc_path),
    )

    assert "tar" in report
    assert "ecs" in report
    assert "fvr" in report
    assert report["tar"]["pairs"] >= 3
    assert 0.0 < report["tar"]["overall"] < 1.0
    # FVR should be 0 (no forbidden_variants in registry)
    assert report["fvr"]["overall"] == 0.0
