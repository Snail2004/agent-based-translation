from __future__ import annotations

import importlib
import os
import sqlite3
import sys
from pathlib import Path


BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))


def create_fixture_db(jobs_root: Path) -> Path:
    job_dir = jobs_root / "fixture_job"
    job_dir.mkdir(parents=True, exist_ok=True)
    db_path = job_dir / "memory.sqlite3"
    con = sqlite3.connect(db_path)
    con.executescript(
        """
        CREATE TABLE documents (
          doc_id TEXT PRIMARY KEY,
          job_id TEXT,
          source_filename TEXT,
          source_lang TEXT,
          target_lang TEXT,
          created_at TEXT,
          updated_at TEXT,
          metadata_json TEXT
        );
        CREATE TABLE blocks (
          block_id TEXT PRIMARY KEY,
          doc_id TEXT,
          order_index INTEGER,
          page INTEGER,
          block_type TEXT,
          parent_type TEXT,
          chapter_id TEXT,
          scene_id TEXT,
          text TEXT,
          original_text TEXT,
          bbox_json TEXT,
          style_json TEXT,
          translation_mode TEXT,
          content_kind TEXT,
          strategy TEXT,
          created_at TEXT,
          updated_at TEXT
        );
        CREATE TABLE glossary_entries (
          glossary_id TEXT PRIMARY KEY,
          doc_id TEXT,
          source_term TEXT,
          target_term TEXT,
          term_type TEXT,
          scope TEXT,
          chapter_id TEXT,
          scene_id TEXT,
          do_not_translate INTEGER,
          case_sensitive INTEGER,
          allowed_variants_json TEXT,
          forbidden_variants_json TEXT,
          examples_json TEXT,
          evidence_span_ids_json TEXT,
          confidence REAL,
          status TEXT,
          occurrences_count INTEGER,
          last_block_id TEXT,
          supersedes_json TEXT,
          created_at TEXT,
          updated_at TEXT
        );
        CREATE TABLE entities (
          entity_id TEXT PRIMARY KEY,
          doc_id TEXT,
          canonical_source TEXT,
          canonical_target TEXT,
          entity_type TEXT,
          gender TEXT,
          role TEXT,
          importance TEXT,
          first_block_id TEXT,
          latest_block_id TEXT,
          aliases_source_json TEXT,
          aliases_target_json TEXT,
          source_pronouns_json TEXT,
          preferred_vietnamese_forms_json TEXT,
          social_role TEXT,
          speaker_style TEXT,
          relations_json TEXT,
          evidence_span_ids_json TEXT,
          confidence REAL,
          visibility TEXT,
          valid_from TEXT,
          valid_to TEXT,
          supersedes_json TEXT,
          conflicts_with_json TEXT,
          status TEXT,
          created_at TEXT,
          updated_at TEXT
        );
        CREATE TABLE entity_relations (
          relation_id TEXT PRIMARY KEY,
          doc_id TEXT,
          source_entity_id TEXT,
          target_entity_id TEXT,
          relation_type TEXT,
          state_label TEXT,
          valid_from_block_id TEXT,
          valid_to_block_id TEXT,
          trigger_event_id TEXT,
          address_policy_json TEXT,
          evidence_json TEXT,
          confidence REAL,
          notes TEXT,
          created_at TEXT,
          updated_at TEXT
        );
        CREATE TABLE translation_runs (
          run_id TEXT PRIMARY KEY,
          experiment_id TEXT,
          doc_id TEXT,
          block_id TEXT,
          config TEXT,
          stage TEXT,
          prev_run_id TEXT,
          pack_id TEXT,
          output_text TEXT,
          model TEXT,
          prompt_version TEXT,
          temperature REAL,
          seed INTEGER,
          system_fingerprint TEXT,
          cost REAL,
          latency_ms INTEGER,
          created_at TEXT,
          window_id TEXT
        );
        CREATE TABLE eval_glossary_gold (
          gold_id TEXT PRIMARY KEY,
          doc_id TEXT,
          source_term TEXT,
          target_term TEXT,
          discussion_url TEXT,
          source_path TEXT,
          source_commit TEXT,
          source_line INTEGER,
          subset_tag TEXT,
          created_at TEXT
        );
        CREATE TABLE reference_eval_only (
          reference_id TEXT PRIMARY KEY,
          doc_id TEXT,
          block_id TEXT,
          target_text TEXT,
          provenance TEXT,
          leakage_risk TEXT,
          subset_tag TEXT,
          created_at TEXT
        );
        """
    )
    con.execute(
        "INSERT INTO documents VALUES (?,?,?,?,?,?,?,?)",
        ("doc_fixture", "fixture_job", "fixture.md", "en", "vi", "2026-06-15", "2026-06-15", '{"title":"Fixture"}'),
    )
    con.execute(
        "INSERT INTO blocks VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        ("b001", "doc_fixture", 1, None, "prose", None, "ch01", None, "Agent appears.", "Agent appears.", None, None, None, None, None, "2026-06-15", "2026-06-15"),
    )
    con.execute(
        "INSERT INTO glossary_entries VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        ("g-runtime", "doc_fixture", "agent", "tác nhân", "technical", "document", None, None, 0, 0, '["tác nhân"]', '[]', '[]', '[]', 0.9, "candidate", 3, "b001", "[]", "2026-06-15", "2026-06-15"),
    )
    con.execute(
        "INSERT INTO entities VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        ("e1", "doc_fixture", "Jim", "Jim", "person", "male", None, "major", "b001", "b001", '["Jim"]', '["Jim"]', '["he"]', '["tôi"]', None, None, "[]", "[]", 0.8, "public", None, None, "[]", "[]", "candidate", "2026-06-15", "2026-06-15"),
    )
    con.execute(
        "INSERT INTO entity_relations VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        ("r1", "doc_fixture", "e1", "e1", "self", "default", "b001", None, None, '{"source_to_target":{"self_term":"tôi"}}', '[{"block_id":"b001"}]', 0.8, "", "2026-06-15", "2026-06-15"),
    )
    con.execute(
        "INSERT INTO translation_runs VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        ("run-s0", "exp_fixture", "doc_fixture", "b001", "S0", "translate", None, None, "Agent xuất hiện.", "gpt-test", "p1", 0.3, 1, "fp", 0.0, 10, "2026-06-15", "w1"),
    )
    con.execute(
        "INSERT INTO translation_runs VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        ("run-s1", "exp_fixture", "doc_fixture", "b001", "S1", "translate", None, None, "Tác nhân xuất hiện.", "gpt-test", "p1", 0.3, 1, "fp", 0.0, 10, "2026-06-15", "w1"),
    )
    con.execute(
        "INSERT INTO eval_glossary_gold VALUES (?,?,?,?,?,?,?,?,?,?)",
        ("gold-1", "doc_fixture", "agent", "tác tử", None, "gold.md", "abc", 12, "gold", "2026-06-15"),
    )
    con.execute(
        "INSERT INTO reference_eval_only VALUES (?,?,?,?,?,?,?,?)",
        ("ref-1", "doc_fixture", "b001", "Tác tử xuất hiện.", "human_gold", "eval_only", "sample", "2026-06-15"),
    )
    con.commit()
    con.close()
    return db_path


def test_readmodel_keeps_runtime_memory_and_gold_eval_only_separate(tmp_path):
    from services.thesis_readmodel import load_thesis_dataset

    create_fixture_db(tmp_path)
    data = load_thesis_dataset("fixture_job", jobs_root=tmp_path)

    runtime_terms = data["runtime_memory"]["glossary_entries"]
    gold_terms = data["eval_only"]["gold_glossary"]

    assert data["meta"]["read_only"] is True
    assert runtime_terms == [runtime_terms[0]]
    assert runtime_terms[0]["source_term"] == "agent"
    assert runtime_terms[0]["target_term"] == "tác nhân"
    assert runtime_terms[0]["provenance"]["source"] == "glossary_entries"
    assert all(term["provenance"]["branch"] == "runtime_memory" for term in runtime_terms)

    assert gold_terms[0]["source_term"] == "agent"
    assert gold_terms[0]["target_term"] == "tác tử"
    assert gold_terms[0]["provenance"]["source"] == "eval_glossary_gold"
    assert gold_terms[0]["provenance"]["injectable"] is False
    assert gold_terms[0]["target_term"] not in {term["target_term"] for term in runtime_terms}


def test_readmodel_translations_are_keyed_by_config_and_attached_to_blocks(tmp_path):
    from services.thesis_readmodel import load_thesis_dataset

    create_fixture_db(tmp_path)
    data = load_thesis_dataset("fixture_job", jobs_root=tmp_path)

    assert sorted(data["translations"]) == ["S0", "S1"]
    assert data["translations"]["S1"][0]["stage"] == "translate"
    assert data["translations"]["S1"][0]["target_text"] == "Tác nhân xuất hiện."
    assert data["blocks"][0]["translations"]["S0"]["target_text"] == "Agent xuất hiện."
    assert data["blocks"][0]["translations"]["S1"]["target_text"] == "Tác nhân xuất hiện."
    assert data["meta"]["available_runs"][0]["config"] == "S0"


def test_routes_load_fixture_and_quarantine_gold_authoring(tmp_path, monkeypatch):
    create_fixture_db(tmp_path)
    monkeypatch.setenv("THESIS_JOBS_ROOT", str(tmp_path))
    monkeypatch.setenv("THESIS_TOOL_PROJECTS_ROOT", str(tmp_path / "projects"))
    monkeypatch.setenv("THESIS_APP_MODE", "cockpit")

    for name in list(sys.modules):
        if name == "app" or name == "config" or name == "routes" or name.startswith("routes.") or name.startswith("services.thesis_readmodel"):
            sys.modules.pop(name, None)

    app_module = importlib.import_module("app")
    client = app_module.create_app().test_client()

    response = client.get("/api/thesis/datasets/fixture_job")
    assert response.status_code == 200
    payload = response.get_json()["data"]
    assert payload["runtime_memory"]["glossary_entries"][0]["target_term"] == "tác nhân"
    assert payload["eval_only"]["gold_glossary"][0]["target_term"] == "tác tử"

    annotation_response = client.post("/api/projects/doc_fixture/annotate/input", json={})
    assert annotation_response.status_code == 404

    normalize_response = client.post("/api/projects/doc_fixture/normalize/candidate-parts", json={})
    assert normalize_response.status_code == 404
    assert normalize_response.get_json()["errors"][0]["code"] == "legacy_feature_quarantined"
