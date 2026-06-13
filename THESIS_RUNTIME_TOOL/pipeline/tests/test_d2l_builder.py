from __future__ import annotations

import json
import sqlite3
from types import SimpleNamespace

import pytest

from pipeline.agents.llm_client import LLMResult, LLMUsage
from pipeline.eval.builder_gold import score_builder_vs_gold
from pipeline.memory.store_init import init_db
from pipeline.prepass.db_source import load_document_from_db
from pipeline.prepass.persist import build_memory_from_db
from pipeline.prepass.prompt import build_messages
from pipeline.prepass.registry import PrepassRegistry
from pipeline.prepass.runner import (
    build_d2l_prepass_windows,
    normalize_d2l_terminology_output,
    run_prepass_document,
)
from pipeline.prepass.schemas import validate_chapter_output


def _insert_doc(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        INSERT INTO documents (doc_id, job_id, source_filename, metadata_json)
        VALUES ('d2l', 'job_d2l', 'd2l', '{}')
        """
    )
    rows = [
        (
            "d2l_introduction_index_b001",
            "d2l",
            1,
            "d2l_introduction",
            "prose",
            "An agent learns a model from data.",
            "An agent learns a model from data.",
            "translate",
        ),
        (
            "d2l_introduction_index_b002",
            "d2l",
            2,
            "d2l_introduction",
            "prose",
            "The model is trained with gradient descent.",
            "The model is trained with gradient descent.",
            "translate",
        ),
        (
            "d2l_linear_networks_index_b001",
            "d2l",
            3,
            "d2l_linear_networks",
            "prose",
            "Linear regression uses a loss function.",
            "Linear regression uses a loss function.",
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
        rows,
    )
    conn.commit()


def _insert_gold(conn: sqlite3.Connection) -> None:
    rows = [
        ("gold_agent", "d2l", "agent", "tác nhân", "glossary.md", "abc123"),
        ("gold_model", "d2l", "model", "mô hình", "glossary.md", "abc123"),
        ("gold_loss", "d2l", "loss function", "hàm mất mát", "glossary.md", "abc123"),
    ]
    conn.executemany(
        """
        INSERT INTO eval_glossary_gold (
          gold_id, doc_id, source_term, target_term, source_path, source_commit
        ) VALUES (?, ?, ?, ?, ?, ?)
        """,
        rows,
    )
    conn.commit()


def _artifact(chapter_id: str, target_for_agent: str = "tác tử") -> dict:
    return {
        "chapter_id": chapter_id,
        "glossary_candidates": [
            {
                "source_term": "agent",
                "canonical_source": "agent",
                "proposed_target_vi": target_for_agent,
                "canonical_target": target_for_agent,
                "termhood": "technical term for an acting learning system",
                "term_type": "term",
                "do_not_translate": False,
                "category": "other",
                "allowed_variants": [],
                "forbidden_variants": ["đại lý"],
                "block_ids": ["d2l_introduction_index_b001"],
                "evidence_span_ids": ["d2l_introduction_index_b001"],
            },
            {
                "source_term": "model",
                "canonical_source": "model",
                "proposed_target_vi": "mô hình",
                "canonical_target": "mô hình",
                "termhood": "core machine-learning term",
                "term_type": "term",
                "do_not_translate": False,
                "category": "other",
                "allowed_variants": [],
                "forbidden_variants": [],
                "block_ids": ["d2l_introduction_index_b001"],
                "evidence_span_ids": ["d2l_introduction_index_b001"],
            },
            {
                "source_term": "PyTorch",
                "canonical_source": "PyTorch",
                "proposed_target_vi": "PyTorch",
                "canonical_target": "PyTorch",
                "termhood": "framework name",
                "term_type": "proper_noun",
                "do_not_translate": True,
                "category": "other",
                "allowed_variants": [],
                "forbidden_variants": [],
                "block_ids": ["d2l_introduction_index_b002"],
                "evidence_span_ids": ["d2l_introduction_index_b002"],
            },
        ],
        "entities": [],
        "relations": [],
        "mention_surfaces": [],
        "chapter_summary_vi": "Chương giới thiệu các khái niệm học sâu.",
        "motifs": [],
    }


def _write_artifact(prepass_dir, artifact: dict) -> None:
    prepass_dir.mkdir(parents=True, exist_ok=True)
    path = prepass_dir / f"{artifact['chapter_id']}.json"
    path.write_text(json.dumps(artifact, ensure_ascii=False, indent=2), encoding="utf-8")


def test_d2l_prompt_and_schema_require_technical_fields():
    chapter = {
        "chapter_id": "d2l_introduction",
        "blocks": [
            {
                "block_id": "d2l_introduction_index_b001",
                "clean_text": "An agent learns a model from data.",
            }
        ],
    }

    messages = build_messages(
        chapter,
        "Glossary:\n- old registry term -> thuật ngữ cũ",
        mode="d2l_terminology",
    )
    combined = "\n".join(message["content"] for message in messages)

    assert "d2l_terminology_v7" in combined
    assert "REGISTRY_POLICY" in combined
    assert "old registry term" not in combined
    assert "Vietnamese diacritics" in combined
    assert "source WINDOW" in combined
    assert "termhood" in combined
    assert "canonical_target" in combined
    assert "evidence_span_ids" in combined
    assert "[d2l_introduction_index_b001]" in combined

    valid = _artifact("d2l_introduction")
    assert validate_chapter_output(
        valid,
        expected_chapter_id="d2l_introduction",
        valid_block_ids={"d2l_introduction_index_b001", "d2l_introduction_index_b002"},
        mode="d2l_terminology",
    ) == []

    invalid = json.loads(json.dumps(valid, ensure_ascii=False))
    invalid["glossary_candidates"][0].pop("termhood")
    invalid["glossary_candidates"][1]["term_type"] = "phrase"
    errors = validate_chapter_output(
        invalid,
        expected_chapter_id="d2l_introduction",
        valid_block_ids={"d2l_introduction_index_b001", "d2l_introduction_index_b002"},
        mode="d2l_terminology",
    )

    assert any("termhood is required" in error for error in errors)
    assert any("term_type is invalid" in error for error in errors)


def test_registry_compress_caps_glossary():
    registry = PrepassRegistry()
    for index in range(100):
        registry.merge(
            {
                "chapter_id": "ch",
                "glossary_candidates": [
                    {
                        "source_term": f"very long source term {index}",
                        "proposed_target_vi": f"thuật ngữ rất dài {index}",
                        "block_ids": [f"b{index}"],
                    }
                ],
            }
        )

    compressed = registry.compress(max_tokens=20)

    assert len(compressed) <= 80 + len("Glossary:")
    assert "very long source term 99" not in compressed


def test_db_loader_maps_spec_slugs_to_d2l_chapter_ids(tmp_path):
    db_path = tmp_path / "memory.sqlite3"
    conn = init_db(db_path)
    _insert_doc(conn)
    conn.close()

    document = load_document_from_db(db_path, ["introduction", "linear_networks"])

    assert [chapter["chapter_id"] for chapter in document["chapters"]] == [
        "d2l_introduction",
        "d2l_linear_networks",
    ]
    assert document["chapters"][0]["blocks"][0]["block_id"] == "d2l_introduction_index_b001"


def test_d2l_normalization_is_glossary_only_without_term_cap():
    obj = _artifact("d2l_introduction")
    obj["glossary_candidates"] = [
        {**obj["glossary_candidates"][0], "source_term": f"term {index}"}
        for index in range(25)
    ]
    obj["entities"] = [{"bad": "shape"}]
    obj["relations"] = [{"bad": "shape"}]
    obj["mention_surfaces"] = [{"bad": "shape"}]
    obj["motifs"] = [{"bad": "shape"}]

    normalized = normalize_d2l_terminology_output(obj)

    assert len(normalized["glossary_candidates"]) == 25
    assert normalized["entities"] == []
    assert normalized["relations"] == []
    assert normalized["mention_surfaces"] == []
    assert normalized["motifs"] == []

    filtered = normalize_d2l_terminology_output(
        {
            **obj,
            "glossary_candidates": [
                {**obj["glossary_candidates"][0], "block_ids": ["ok"], "evidence_span_ids": ["ok"]},
                {**obj["glossary_candidates"][1], "block_ids": ["bad"], "evidence_span_ids": ["bad"]},
            ],
        },
        valid_block_ids={"ok"},
    )
    assert [term["block_ids"] for term in filtered["glossary_candidates"]] == [["ok"]]


def test_d2l_window_builder_partitions_blocks():
    chapter = {
        "chapter_id": "d2l_demo",
        "blocks": [
            {"block_id": "b1", "order_index": 1, "clean_text": "a" * 20},
            {"block_id": "b2", "order_index": 2, "clean_text": "b" * 20},
            {"block_id": "b3", "order_index": 3, "clean_text": "c" * 20},
        ],
    }

    windows = build_d2l_prepass_windows(chapter, target_tokens=10, max_blocks=2)

    assert [window.window_id for window in windows] == [
        "wb_d2l_demo_001",
        "wb_d2l_demo_002",
    ]
    assert [[block["block_id"] for block in window.blocks] for window in windows] == [
        ["b1", "b2"],
        ["b3"],
    ]


class FakeWindowClient:
    def __init__(self):
        self.config = SimpleNamespace(model="fake-model", seed=20260612)
        self.calls = 0

    def call(self, messages, *, response_format=None, tag="", bypass_cache=False):
        self.calls += 1
        block_id = f"d2l_demo_b{self.calls:03d}"
        payload = _artifact("d2l_demo")
        payload["glossary_candidates"] = [
            {
                **payload["glossary_candidates"][0],
                "source_term": f"term {self.calls}",
                "canonical_source": f"term {self.calls}",
                "block_ids": [block_id],
                "evidence_span_ids": [block_id],
            }
        ]
        return LLMResult(
            text=json.dumps(payload, ensure_ascii=False),
            parsed_json=payload,
            json_error=None,
            model="fake-model",
            system_fingerprint="fp_fake",
            usage=LLMUsage(prompt_tokens=10, completion_tokens=5),
            cost_usd=0.0,
            latency_ms=1,
            from_cache=False,
            cache_key=f"key-{self.calls}",
        )


def test_d2l_windowed_runner_merges_window_outputs(tmp_path):
    document = {
        "doc_id": "d2l",
        "chapters": [
            {
                "chapter_id": "d2l_demo",
                "blocks": [
                    {"block_id": "d2l_demo_b001", "order_index": 1, "clean_text": "term 1 " * 20},
                    {"block_id": "d2l_demo_b002", "order_index": 2, "clean_text": "term 2 " * 20},
                    {"block_id": "d2l_demo_b003", "order_index": 3, "clean_text": "term 3 " * 20},
                ],
            }
        ],
    }
    client = FakeWindowClient()

    report = run_prepass_document(
        document,
        ["demo"],
        client,
        tmp_path / "out",
        mode="d2l_terminology",
        d2l_window_target_tokens=10,
        d2l_window_max_blocks=1,
    )

    artifact = json.loads((tmp_path / "out" / "d2l_demo.json").read_text(encoding="utf-8"))
    assert client.calls == 3
    assert report.chapters[0].counts["windows"] == 3
    assert artifact["window_count"] == 3
    assert [term["source_term"] for term in artifact["glossary_candidates"]] == [
        "term 1",
        "term 2",
        "term 3",
    ]


def test_build_memory_from_db_consolidates_conflicts_and_does_not_read_gold(
    tmp_path,
    monkeypatch,
):
    db_path = tmp_path / "memory.sqlite3"
    conn = init_db(db_path)
    _insert_doc(conn)
    _insert_gold(conn)
    conn.close()
    prepass_dir = tmp_path / "prepass"
    artifact = _artifact("d2l_introduction")
    artifact["glossary_candidates"].append(
        {
            **artifact["glossary_candidates"][0],
            "canonical_target": "tác nhân",
            "proposed_target_vi": "tác nhân",
            "allowed_variants": ["tác tử"],
        }
    )
    _write_artifact(prepass_dir, artifact)

    from pipeline.prepass import persist as persist_module

    original_init_db = persist_module.init_db

    def guarded_init_db(path):
        guarded_conn = original_init_db(path)

        def authorizer(action, arg1, arg2, _db_name, _trigger):
            if action == sqlite3.SQLITE_READ and arg1 == "eval_glossary_gold":
                return sqlite3.SQLITE_DENY
            return sqlite3.SQLITE_OK

        guarded_conn.set_authorizer(authorizer)
        return guarded_conn

    monkeypatch.setattr(persist_module, "init_db", guarded_init_db)

    report = build_memory_from_db(db_path, prepass_dir, freeze=False)

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    rows = {
        row["source_term"]: dict(row)
        for row in conn.execute(
            "SELECT source_term, target_term, allowed_variants_json FROM glossary_entries"
        )
    }
    conn.close()

    assert report.glossary == 3
    assert rows["agent"]["target_term"] == "tác tử"
    assert "tác nhân" in json.loads(rows["agent"]["allowed_variants_json"])
    assert "tác nhân" not in {row["target_term"] for row in rows.values() if row["source_term"] != "agent"}


def test_builder_vs_gold_scores_recall_agreement_conflict_and_extra(tmp_path):
    db_path = tmp_path / "memory.sqlite3"
    conn = init_db(db_path)
    _insert_doc(conn)
    _insert_gold(conn)
    conn.execute(
        """
        INSERT INTO glossary_entries (
          glossary_id, doc_id, source_term, target_term, allowed_variants_json, status
        ) VALUES ('gl_agent', 'd2l', 'agent', 'tác tử', '["tác tử"]', 'approved')
        """
    )
    conn.execute(
        """
        INSERT INTO glossary_entries (
          glossary_id, doc_id, source_term, target_term, allowed_variants_json, status
        ) VALUES ('gl_model', 'd2l', 'model', 'mo hinh', '[]', 'approved')
        """
    )
    conn.execute(
        """
        INSERT INTO glossary_entries (
          glossary_id, doc_id, source_term, target_term, allowed_variants_json, status
        ) VALUES ('gl_extra', 'd2l', 'softmax', 'softmax', '[]', 'approved')
        """
    )
    conn.commit()

    report = score_builder_vs_gold(
        conn,
        doc_id="d2l",
        chapters=["introduction", "linear_networks"],
    )
    conn.close()

    assert report.chapters == ["d2l_introduction", "d2l_linear_networks"]
    assert report.gold_terms_present == 3
    assert report.builder_terms == 3
    assert report.matched_terms == 2
    assert report.agreement_terms == 1
    assert report.recall == 0.666667
    assert report.agreement == 0.5
    assert report.missing_terms == [
        {"source_term": "loss function", "gold_target": "hàm mất mát"}
    ]
    assert report.conflicts == [
        {
            "source_term": "agent",
            "builder_target": "tác tử",
            "gold_target": "tác nhân",
        }
    ]
    assert report.extra_terms == [{"source_term": "softmax", "builder_target": "softmax"}]


def test_freeze_blocks_memory_writes_after_db_build(tmp_path):
    db_path = tmp_path / "memory.sqlite3"
    conn = init_db(db_path)
    _insert_doc(conn)
    conn.close()
    prepass_dir = tmp_path / "prepass"
    _write_artifact(prepass_dir, _artifact("d2l_introduction"))

    report = build_memory_from_db(db_path, prepass_dir, freeze=True)

    assert report.frozen_at is not None
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    with pytest.raises(sqlite3.IntegrityError, match="memory frozen"):
        conn.execute(
            """
            INSERT INTO glossary_entries (glossary_id, doc_id, source_term, target_term)
            VALUES ('gl_new', 'd2l', 'new term', 'thuật ngữ mới')
            """
        )
    conn.close()
