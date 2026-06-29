from __future__ import annotations

import inspect
import json
import sqlite3
from pathlib import Path

from pipeline.memory.store_init import init_db
from pipeline.prepass import builder_v2_render as b2
from pipeline.prepass.runner import PrepassWindow


def _fixture_db(tmp_path: Path) -> sqlite3.Connection:
    conn = init_db(tmp_path / "memory.sqlite3")
    conn.execute(
        """
        INSERT INTO documents (doc_id, job_id, source_filename, metadata_json)
        VALUES ('d2l', 'job', 'source', '{}')
        """
    )
    blocks = [
        ("b1", "d2l", 1, "d2l_preliminaries", "prose", "A feature map defines the model."),
        ("b2", "d2l", 2, "d2l_preliminaries", "prose", "These features help the model, but future is only a normal word here."),
        ("b3", "d2l", 3, "d2l_preliminaries", "prose", "The future term appears later."),
    ]
    conn.executemany(
        """
        INSERT INTO blocks (block_id, doc_id, order_index, chapter_id, block_type, text, original_text, translation_mode)
        VALUES (?, ?, ?, ?, ?, ?, ?, 'translate')
        """,
        [(b, d, o, c, t, text, text) for b, d, o, c, t, text in blocks],
    )
    terms = [
        (
            "gl_feature",
            "feature",
            "đặc trưng",
            '["d2l_feature"]',
            '["b1"]',
            1,
        ),
        (
            "gl_model",
            "model",
            "mô hình",
            '[]',
            '["b1"]',
            1,
        ),
        (
            "gl_future",
            "future",
            "tương lai",
            '[]',
            '["b3"]',
            1,
        ),
    ]
    conn.executemany(
        """
        INSERT INTO glossary_entries (
          glossary_id, doc_id, source_term, target_term, allowed_variants_json,
          evidence_span_ids_json, occurrences_count
        )
        VALUES (?, 'd2l', ?, ?, ?, ?, ?)
        """,
        terms,
    )
    conn.commit()
    return conn


def _window(block_id: str, text: str, order: int = 2) -> PrepassWindow:
    return PrepassWindow(
        window_id="wb_d2l_preliminaries_002",
        chapter_id="d2l_preliminaries",
        blocks=[
            {
                "block_id": block_id,
                "chapter_id": "d2l_preliminaries",
                "order_index": order,
                "clean_text": text,
                "source_text": text,
            }
        ],
        est_src_tokens=max(1, len(text) // 4),
    )


def test_prompt_v8_contains_required_contract():
    assert "RECALL RULE (mandatory)" in b2.SYSTEM_PROMPT
    assert 'conflict_type "termhood_suspected"' in b2.SYSTEM_PROMPT
    assert "reference/gold" in b2.SYSTEM_PROMPT
    assert "any Vietnamese you were not given" not in b2.SYSTEM_PROMPT


def test_pack_audit_chronological_and_concept_variant(tmp_path):
    conn = _fixture_db(tmp_path)
    entries = b2.load_registry_entries(conn)
    window = _window("b2", "These features help the model, but future is only a normal word here.")

    pack, audit = b2.build_memory_pack(entries, window, pack_mode="proxy_chronological")

    assert audit["pack_provenance"] == "glossary_entries"
    assert audit["pack_source_mode"] == "proxy_chronological"
    assert set(audit) >= {
        "included_by_exact_surface",
        "included_by_concept_key",
        "excluded_no_surface_match",
        "dropped_by_budget",
        "pack_token_estimate",
        "window_term_surfaces_detected",
        "pack_source_mode",
        "pack_provenance",
    }
    assert "model" in audit["included_by_exact_surface"]
    assert {"source_term": "feature", "related_surface_seen": "features"} in audit[
        "included_by_concept_key"
    ]
    packed_terms = {
        item["source_term"] for item in pack["matched_existing_terms"]
    } | {item["source_term"] for item in pack["near_number_variants"]}
    assert "future" not in packed_terms
    for item in [*pack["matched_existing_terms"], *pack["near_number_variants"]]:
        assert "evidence_block_ids" not in item
    assert audit["pack_token_estimate"] <= b2.PACK_TOKEN_CAP


def test_full_registry_mode_can_include_future_proxy(tmp_path):
    conn = _fixture_db(tmp_path)
    entries = b2.load_registry_entries(conn)
    window = _window("b2", "future")

    pack, audit = b2.build_memory_pack(entries, window, pack_mode="proxy_full_registry")

    assert audit["pack_source_mode"] == "proxy_full_registry"
    assert "future" in {item["source_term"] for item in pack["matched_existing_terms"]}


def test_render_window_caps_and_determinism(tmp_path):
    conn = _fixture_db(tmp_path)
    entries = b2.load_registry_entries(conn)
    window = _window("b2", "These features help the model.")

    first = b2.render_window(entries, window, pack_mode="proxy_chronological")
    second = b2.render_window(entries, window, pack_mode="proxy_chronological")

    assert json.dumps(first, ensure_ascii=False, sort_keys=True) == json.dumps(
        second, ensure_ascii=False, sort_keys=True
    )
    assert first["token_estimate"]["prompt"] <= b2.PROMPT_TOKEN_CAP
    assert first["audit"]["pack_token_estimate"] <= b2.PACK_TOKEN_CAP
    prompt = b2.prompt_text(first["messages"])
    assert '"matched_existing_terms":[' in prompt
    assert "\n  \"matched_existing_terms\"" not in prompt


def test_no_llm_or_gold_source_references():
    module_source = inspect.getsource(b2)
    assert "LLMClient" not in module_source
    assert "eval_glossary_gold" not in module_source
    assert "glossary.md" not in module_source
    assert "reference_eval_only" not in module_source
