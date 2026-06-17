from __future__ import annotations

import csv
import json
import sqlite3
from pathlib import Path

import pytest

from pipeline.eval.d2l_translate_score import (
    _count_non_overlapping_forms,
    _count_source_matches,
    score_d2l_translation_run,
)
from pipeline.memory.store_init import init_db
from pipeline.retrieval.context_builder import build_context_pack, plan_anchors
from pipeline.translate.prompt import build_messages, purity_check
from pipeline.translate.profiles import get_profile
from pipeline.translate.windower import Window, build_windows


def _fixture_db(tmp_path: Path) -> Path:
    db_path = tmp_path / "memory.sqlite3"
    conn = init_db(db_path)
    conn.execute(
        """
        INSERT INTO documents (doc_id, job_id, source_filename, metadata_json)
        VALUES ('d2l', 'job', 'fixture', '{}')
        """
    )
    blocks = [
        (
            "b_head",
            "d2l",
            1,
            "d2l_preliminaries",
            "heading",
            "## Agent models",
            "## Agent models",
            "passthrough",
        ),
        (
            "b1",
            "d2l",
            2,
            "d2l_preliminaries",
            "prose",
            "An agent learns a model.",
            "An agent learns a model.",
            "translate",
        ),
        (
            "b2",
            "d2l",
            3,
            "d2l_preliminaries",
            "prose",
            "The agent updates the model.",
            "The agent updates the model.",
            "translate",
        ),
        (
            "b_code",
            "d2l",
            4,
            "d2l_preliminaries",
            "code",
            "agent = model.fit(x)",
            "agent = model.fit(x)",
            "passthrough",
        ),
        (
            "b3",
            "d2l",
            5,
            "d2l_preliminaries",
            "prose",
            "PyTorch exposes .shape once.",
            "PyTorch exposes .shape once.",
            "translate",
        ),
    ]
    conn.executemany(
        """
        INSERT INTO blocks (
          block_id, doc_id, order_index, chapter_id, block_type, text,
          original_text, translation_mode
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        blocks,
    )
    glossary = [
        ("gl_agent", "d2l", "agent", "tác nhân", "term", 0, 2, '["tác tử"]'),
        ("gl_model", "d2l", "model", "mô hình", "term", 0, 2, "[]"),
        ("gl_pytorch", "d2l", "PyTorch", "PyTorch", "proper_noun", 1, 1, "[]"),
        ("gl_shape", "d2l", ".shape", ".shape", "code_api", 1, 1, "[]"),
        ("gl_once", "d2l", "exposes", "phơi bày", "term", 0, 1, "[]"),
    ]
    conn.executemany(
        """
        INSERT INTO glossary_entries (
          glossary_id, doc_id, source_term, target_term, term_type,
          do_not_translate, occurrences_count, allowed_variants_json, status
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'approved')
        """,
        glossary,
    )
    gold = [
        ("gold_agent", "d2l", "agent", "tác nhân", "glossary.md", "abc"),
        ("gold_model", "d2l", "model", "mô hình", "glossary.md", "abc"),
    ]
    conn.executemany(
        """
        INSERT INTO eval_glossary_gold (
          gold_id, doc_id, source_term, target_term, source_path, source_commit
        ) VALUES (?, ?, ?, ?, ?, ?)
        """,
        gold,
    )
    runs = [
        ("tr_s0_b_head", "d2l_p3", "d2l", "b_head", "S0", "draft", "Mô hình tác tử", "gpt", "s0_d2l_v1"),
        ("tr_s0_b1", "d2l_p3", "d2l", "b1", "S0", "draft", "Một tác tử học một mô hình.", "gpt", "s0_d2l_v1"),
        ("tr_s0_b2", "d2l_p3", "d2l", "b2", "S0", "draft", "Tác nhân cập nhật model.", "gpt", "s0_d2l_v1"),
        ("tr_s0_b3", "d2l_p3", "d2l", "b3", "S0", "draft", "PyTorch để lộ .shape một lần.", "gpt", "s0_d2l_v1"),
        ("tr_s1_b_head", "d2l_p3", "d2l", "b_head", "S1", "draft", "Mô hình tác nhân", "gpt", "s1_d2l_v1"),
        ("tr_s1_b1", "d2l_p3", "d2l", "b1", "S1", "draft", "Một tác nhân học một mô hình.", "gpt", "s1_d2l_v1"),
        ("tr_s1_b2", "d2l_p3", "d2l", "b2", "S1", "draft", "Tác nhân cập nhật mô hình.", "gpt", "s1_d2l_v1"),
        ("tr_s1_b3", "d2l_p3", "d2l", "b3", "S1", "draft", "PyTorch để lộ .shape một lần.", "gpt", "s1_d2l_v1"),
    ]
    conn.executemany(
        """
        INSERT INTO translation_runs (
          run_id, experiment_id, doc_id, block_id, config, stage,
          output_text, model, prompt_version
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        runs,
    )
    conn.commit()
    conn.close()
    return db_path


def test_d2l_block_filter_excludes_passthrough(tmp_path: Path) -> None:
    db_path = _fixture_db(tmp_path)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    profile = get_profile("technical_d2l_v1")

    windows = build_windows(
        conn,
        "d2l",
        ["preliminaries"],
        target_tokens=100,
        block_types=profile.translatable_block_types,
    )
    block_ids = [block_id for window in windows for block_id in window.block_ids]

    assert "b_code" not in block_ids
    assert set(block_ids) == {"b_head", "b1", "b2", "b3"}


def test_d2l_prompt_is_technical_not_literary() -> None:
    messages = build_messages(
        [{"block_id": "b1", "clean_text": "An agent learns."}],
        config="S0",
        profile_name="technical_d2l_v1",
    )
    combined = "\n".join(message["content"] for message in messages)

    assert "s0_d2l_v1" in combined
    assert "technical translator" in combined
    assert "DIALOGUE" not in combined
    assert "Newmark" not in combined


def test_d2l_s0_purity_check(tmp_path: Path) -> None:
    messages = build_messages(
        [{"block_id": "b9", "clean_text": "A tensor is useful."}],
        config="S0",
        profile_name="technical_d2l_v1",
    )
    violations = purity_check(
        messages,
        [{"source_term": "agent", "proposed_target_vi": "tác nhân"}],
        [],
        [],
    )

    assert violations == []


def test_d2l_injection_policy_occ_role_and_canonical_only(tmp_path: Path) -> None:
    db_path = _fixture_db(tmp_path)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    blocks = [
        dict(row)
        for row in conn.execute(
            """
            SELECT block_id, doc_id, chapter_id, order_index, block_type,
                   text AS clean_text, original_text AS source_text
            FROM blocks
            WHERE block_id IN ('b1', 'b2', 'b3')
            ORDER BY order_index
            """
        )
    ]

    anchors = plan_anchors(conn, blocks, profile_name="technical_d2l_v1")
    pack = build_context_pack(conn, Window("w", ["b1", "b2", "b3"], 10), anchors)
    rendered = pack.render_hard_constraints()

    assert "agent -> tác nhân" in rendered
    assert "model -> mô hình" in rendered
    assert "tác tử" not in rendered
    assert "PyTorch" not in rendered
    assert ".shape" not in rendered
    assert "exposes" not in rendered


def test_d2l_scorer_scope_gold_variants_b_d_a(tmp_path: Path) -> None:
    db_path = _fixture_db(tmp_path)
    variants = tmp_path / "variants.csv"
    with variants.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=["source_term", "variant_vi", "note"])
        writer.writeheader()
        writer.writerow({"source_term": "agent", "variant_vi": "tác tử", "note": "valid"})

    report = score_d2l_translation_run(
        db_path,
        chapters=["preliminaries"],
        out_path=tmp_path / "report.json",
        gold_variants_path=variants,
    )

    assert report["scope"]["translated_block_types"] == {"heading": 1, "prose": 3}
    assert report["scope"]["passthrough_block_types"] == {"code": 1}
    # Gold occurrences inside b_code are excluded from the denominator.
    assert report["B_tar_vs_gold"]["S1"]["flat"]["pairs"] == 5
    assert report["B_tar_vs_gold"]["S1"]["flat"]["overall"] == 1.0
    assert report["B_tar_vs_gold"]["S0"]["flat"]["overall"] < 1.0
    assert report["D_registry_consistency"]["S0"]["drift_terms"] >= 1
    assert report["D_registry_consistency"]["S1"]["overall"] == 1.0
    assert report["D_registry_consistency"]["S1"]["method"] == "block_surface_v2"
    assert report["D_registry_consistency"]["S1"]["alignment"] is False
    assert report["D_registry_consistency"]["S1"]["headline_ready"] is False
    assert report["D_registry_consistency"]["S1"]["terms_all"]
    assert any(
        item["source_term"] == "model" and item["status"] == "consistent"
        for item in report["D_registry_consistency"]["S1"]["terms_all"]
    )
    assert report["A_tar_vs_registry"]["S1"]["overall"] == 1.0
    assert report["stage_gate"]["no_passthrough_translated"]["S1"] is True
    assert report["stage_gate"]["preserve_terms_excluded_from_injection"] is True


def test_longest_match_no_nesting() -> None:
    forms = _count_non_overlapping_forms(
        "Độ chính xác phân loại tăng, độ chính xác tổng thể cũng tăng.",
        ["chính xác", "độ chính xác", "độ chính xác phân loại"],
    )

    assert dict(forms) == {
        "độ chính xác phân loại": 1,
        "độ chính xác": 1,
    }


def test_url_masked_no_false_term() -> None:
    assert _count_source_matches("See https://discuss.d2l.ai/t/39 for details.", "AI") == 0
    assert _count_source_matches("AI systems are discussed outside the URL.", "AI") == 1


def test_case_sensitive_acronym() -> None:
    assert _count_source_matches("ai AI Ai", "AI", case_sensitive=True) == 1
    assert _count_source_matches("ai AI Ai", "AI", case_sensitive=False) == 3


def test_terms_all_present(tmp_path: Path) -> None:
    report = score_d2l_translation_run(
        _fixture_db(tmp_path),
        chapters=["preliminaries"],
        out_path=tmp_path / "report.json",
    )

    terms_all = report["D_registry_consistency"]["S1"]["terms_all"]
    assert terms_all
    statuses = {item["status"] for item in terms_all}
    assert "consistent" in statuses
    assert report["D_registry_consistency"]["S1"]["worst_terms"] == []
