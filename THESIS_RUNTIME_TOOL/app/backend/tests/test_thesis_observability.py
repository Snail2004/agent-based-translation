from __future__ import annotations

import importlib
import json
import sqlite3
import sys
from pathlib import Path


BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))


def _insert_cache_row(
    con: sqlite3.Connection,
    *,
    table: str,
    cache_key: str,
    tag: str,
    prompt_tokens: int,
    cached_tokens: int,
    completion_tokens: int,
    cost: float,
) -> None:
    request_json = json.dumps({
        "model": "gpt-test",
        "temperature": 0.3,
        "seed": 1,
        "messages": [
            {"role": "system", "content": "SYSTEM PROMPT"},
            {"role": "user", "content": f"USER PROMPT for {tag}"},
        ],
        "response_format": {"type": "json_object"},
    })
    usage_json = json.dumps({
        "prompt_tokens": prompt_tokens,
        "cached_tokens": cached_tokens,
        "completion_tokens": completion_tokens,
        "reasoning_tokens": 0,
    })
    con.execute(
        f"""
        INSERT INTO {table} (
          cache_key, model, tag, request_json, response_text,
          usage_json, cost_usd, latency_ms, created_at
        ) VALUES (?,?,?,?,?,?,?,?,?)
        """,
        (
            cache_key,
            "gpt-test",
            tag,
            request_json,
            '{"ok": true}',
            usage_json,
            cost,
            1234,
            "2026-06-15 01:02:03",
        ),
    )


def create_observability_fixture(jobs_root: Path) -> None:
    job_dir = jobs_root / "fixture_job"
    job_dir.mkdir(parents=True, exist_ok=True)
    memory = sqlite3.connect(job_dir / "memory.sqlite3")
    memory.executescript(
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
        CREATE TABLE memory_packs (
          pack_id TEXT PRIMARY KEY,
          doc_id TEXT,
          block_id TEXT,
          pack_hash TEXT,
          prompt_version TEXT,
          estimated_tokens INTEGER,
          payload_json TEXT,
          memory_refs_json TEXT,
          retrieval_debug_json TEXT,
          created_at TEXT,
          config TEXT
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
        """
    )
    memory.execute(
        "INSERT INTO documents VALUES (?,?,?,?,?,?,?,?)",
        ("doc_fixture", "fixture_job", "fixture.md", "en", "vi", "2026-06-15", "2026-06-15", '{"title":"Fixture"}'),
    )
    memory.execute(
        "INSERT INTO blocks VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        ("b001", "doc_fixture", 1, None, "prose", None, "ch01", None, "Agent appears.", "Agent appears.", None, None, None, None, None, "2026-06-15", "2026-06-15"),
    )
    memory.execute(
        "INSERT INTO memory_packs VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (
            "pk_S1_w1",
            "doc_fixture",
            "b001",
            "hash-pack",
            "s1_fixture",
            42,
            json.dumps({"window_id": "w1", "block_ids": ["b001"], "context_pack": {"glossary": ["agent -> tac nhan"]}}),
            json.dumps(["glossary:g1"]),
            json.dumps({"included": ["g1"], "excluded": ["g2"], "dropped_by_budget": ["g3"]}),
            "2026-06-15 01:00:00",
            "S1",
        ),
    )
    memory.execute(
        "INSERT INTO translation_runs VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        ("run-1", "exp_fixture", "doc_fixture", "b001", "S1", "draft", None, "pk_S1_w1", "Translated.", "gpt-test", "s1_fixture", 0.3, 1, "fp", 0.001, 1234, "2026-06-15 01:02:03", "w1"),
    )
    memory.commit()
    memory.close()

    translate = sqlite3.connect(jobs_root / "translate_cache.sqlite3")
    translate.executescript(
        """
        CREATE TABLE llm_call_cache (
          cache_key TEXT PRIMARY KEY,
          model TEXT,
          tag TEXT,
          request_json TEXT,
          response_text TEXT,
          system_fingerprint TEXT,
          usage_json TEXT,
          cost_usd REAL,
          latency_ms INTEGER,
          created_at TEXT
        );
        CREATE TABLE usage_daily (date TEXT PRIMARY KEY, total_tokens INTEGER, calls INTEGER);
        """
    )
    _insert_cache_row(translate, table="llm_call_cache", cache_key="key-translate", tag="S1_w1", prompt_tokens=120, cached_tokens=64, completion_tokens=20, cost=0.001)
    _insert_cache_row(translate, table="llm_call_cache", cache_key="key-other", tag="S1_unrelated", prompt_tokens=999, cached_tokens=0, completion_tokens=1, cost=9.0)
    translate.execute("INSERT INTO usage_daily VALUES (?,?,?)", ("2026-06-15", 140, 1))
    translate.commit()
    translate.close()

    prepass = sqlite3.connect(jobs_root / "prepass_cache.sqlite3")
    prepass.executescript(
        """
        CREATE TABLE llm_call_cache (
          cache_key TEXT PRIMARY KEY,
          model TEXT,
          tag TEXT,
          request_json TEXT,
          response_text TEXT,
          system_fingerprint TEXT,
          usage_json TEXT,
          cost_usd REAL,
          latency_ms INTEGER,
          created_at TEXT
        );
        CREATE TABLE usage_daily (date TEXT PRIMARY KEY, total_tokens INTEGER, calls INTEGER);
        """
    )
    _insert_cache_row(prepass, table="llm_call_cache", cache_key="key-prepass", tag="prepass_doc_fixture_ch01", prompt_tokens=80, cached_tokens=0, completion_tokens=10, cost=0.002)
    prepass.execute("INSERT INTO usage_daily VALUES (?,?,?)", ("2026-06-15", 90, 1))
    prepass.commit()
    prepass.close()

    judge = sqlite3.connect(job_dir / "judge_cache.sqlite3")
    judge.executescript(
        """
        CREATE TABLE judge_call_cache (
          cache_key TEXT PRIMARY KEY,
          model TEXT,
          tag TEXT,
          request_json TEXT,
          response_text TEXT,
          usage_json TEXT,
          cost_usd REAL,
          latency_ms INTEGER,
          created_at TEXT
        );
        CREATE TABLE usage_daily (date TEXT PRIMARY KEY, total_tokens INTEGER, calls INTEGER);
        """
    )
    _insert_cache_row(judge, table="judge_call_cache", cache_key="key-judge", tag="gemba:b001:S1", prompt_tokens=70, cached_tokens=0, completion_tokens=30, cost=0.003)
    judge.execute("INSERT INTO usage_daily VALUES (?,?,?)", ("2026-06-15", 100, 1))
    judge.commit()
    judge.close()


def test_observability_readmodel_lists_calls_and_totals(tmp_path):
    from services.thesis_observability import load_observability

    create_observability_fixture(tmp_path)
    data = load_observability("fixture_job", jobs_root=tmp_path)

    assert data["meta"]["read_only"] is True
    assert data["meta"]["source"] == "thesis_observability_readmodel"
    assert "replay-hit events are not logged" in data["meta"]["known_gap"]

    calls = data["calls"]
    assert {call["agent"] for call in calls} == {"Builder", "Translator", "Judge"}
    assert {call["cache_key"] for call in calls} == {"key-translate", "key-prepass", "key-judge"}
    assert all(call["read_only"] for call in calls)

    translator = next(call for call in calls if call["agent"] == "Translator")
    assert translator["usage"]["prompt_tokens"] == 120
    assert translator["usage"]["cached_tokens"] == 64
    assert translator["usage"]["total_quota_tokens"] == 140
    assert translator["cache"]["provider"]["cache_hit"] is True
    assert translator["link"]["pack_id"] == "pk_S1_w1"
    assert translator["link"]["pack_linked"] is True

    assert data["totals"]["overall"]["calls"] == 3
    assert data["totals"]["overall"]["total_quota_tokens"] == 330
    assert round(data["totals"]["overall"]["cost_usd"], 3) == 0.006
    assert data["totals"]["by_agent"]["Translator"]["calls"] == 1
    assert data["totals"]["by_config"]["S1"]["total_quota_tokens"] == 140


def test_observability_call_detail_parses_messages_and_memory_pack(tmp_path):
    from services.thesis_observability import load_call_detail

    create_observability_fixture(tmp_path)
    detail = load_call_detail("fixture_job", "translate:key-translate", jobs_root=tmp_path)

    assert detail["call_id"] == "translate:key-translate"
    assert [message["role"] for message in detail["messages"]] == ["system", "user"]
    assert detail["messages"][0]["content"] == "SYSTEM PROMPT"
    assert detail["memory_pack"]["pack_id"] == "pk_S1_w1"
    assert detail["memory_pack"]["estimated_tokens"] == 42
    assert detail["memory_pack"]["payload"]["context_pack"]["glossary"] == ["agent -> tac nhan"]
    assert detail["memory_pack"]["retrieval_debug"]["included"] == ["g1"]
    assert detail["memory_pack"]["retrieval_debug"]["dropped_by_budget"] == ["g3"]
    assert detail["memory_pack"]["context_audit"]["included_count"] == 1
    assert detail["memory_pack"]["context_audit"]["excluded_count"] == 1
    assert detail["memory_pack"]["context_audit"]["dropped_by_budget_count"] == 1
    assert detail["memory_pack"]["context_audit"]["included_sample"] == ["g1"]
    assert detail["token_breakdown"]["actual_prompt_tokens"] == 120
    assert detail["token_breakdown"]["estimated_memory_pack_tokens"] == 42
    assert detail["linked_runs"][0]["run_id"] == "run-1"


def test_observability_routes_are_separate_from_dataset_readmodel(tmp_path, monkeypatch):
    create_observability_fixture(tmp_path)
    monkeypatch.setenv("THESIS_JOBS_ROOT", str(tmp_path))
    monkeypatch.setenv("THESIS_TOOL_PROJECTS_ROOT", str(tmp_path / "projects"))
    monkeypatch.setenv("THESIS_APP_MODE", "cockpit")

    for name in list(sys.modules):
        if name == "app" or name == "config" or name == "routes" or name.startswith("routes.") or name.startswith("services.thesis_"):
            sys.modules.pop(name, None)

    app_module = importlib.import_module("app")
    client = app_module.create_app().test_client()

    response = client.get("/api/thesis/observability/fixture_job")
    assert response.status_code == 200
    observability = response.get_json()["data"]
    assert observability["calls"]
    assert "usage_daily" in observability

    detail_response = client.get("/api/thesis/observability/fixture_job/calls/translate:key-translate")
    assert detail_response.status_code == 200
    detail = detail_response.get_json()["data"]
    assert detail["memory_pack"]["pack_id"] == "pk_S1_w1"

    dataset_response = client.get("/api/thesis/datasets/fixture_job")
    assert dataset_response.status_code == 200
    dataset = dataset_response.get_json()["data"]
    assert "runtime_memory" in dataset
    assert "calls" not in dataset
    assert "usage_daily" not in dataset
