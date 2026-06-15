from __future__ import annotations

import json
import sqlite3
import tempfile
from pathlib import Path

import pytest

from pipeline.agents.llm_client import LLMResult, LLMUsage
from pipeline.ingest.document_loader import load_document
from pipeline.memory.store_init import migrate_db
from pipeline.translate.runner import TranslateReport, translate_windows
from pipeline.translate.windower import Window
from pipeline.translate.prompt import build_messages


def _fake_result(
    json_body: dict | None,
    *,
    prompt: int = 200,
    completion: int = 50,
    cache: bool = False,
    json_error: str | None = None,
) -> LLMResult:
    return LLMResult(
        text=json.dumps(json_body) if json_body is not None else "INVALID",
        parsed_json=json_body,
        json_error=json_error,
        model="gpt-5.4-mini",
        system_fingerprint="fp_test",
        usage=LLMUsage(prompt_tokens=prompt, cached_tokens=0,
                       completion_tokens=completion, reasoning_tokens=0),
        cost_usd=0.0,
        latency_ms=100,
        from_cache=cache,
        cache_key="test_key",
    )


def _ok_response(block_ids: list[str]) -> dict:
    return {bid: f"Translation of {bid}." for bid in block_ids}


def _write_json(path: Path, data: dict) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def _make_doc_db(tmp_path: Path) -> tuple[sqlite3.Connection, str]:
    """Create a fresh DB with document+blocks using load_document."""
    doc = {
        "doc_id": "ti",
        "metadata": {"source_language": "en", "target_language": "vi"},
        "chapters": [
            {
                "chapter_id": "ti_ch02",
                "blocks": [
                    {"block_id": "ch02_b001", "order_index": 0,
                     "block_type": "paragraph",
                     "clean_text": "Hello, Jim.", "source_text": "Hello, Jim.",
                     "annotations": {}},
                    {"block_id": "ch02_b002", "order_index": 1,
                     "block_type": "paragraph",
                     "clean_text": "Good day, captain.", "source_text": "Good day, captain.",
                     "annotations": {}},
                    {"block_id": "ch02_b003", "order_index": 2,
                     "block_type": "paragraph",
                     "clean_text": "The sea is rough.", "source_text": "The sea is rough.",
                     "annotations": {}},
                ],
            },
            {
                "chapter_id": "ti_ch03",
                "blocks": [
                    {"block_id": "ch03_b001", "order_index": 3,
                     "block_type": "paragraph",
                     "clean_text": "We arrived at the island.",
                     "source_text": "We arrived at the island.",
                     "annotations": {}},
                ],
            },
        ],
    }
    doc_path = tmp_path / "document.json"
    _write_json(doc_path, doc)
    db_path = tmp_path / "memory.sqlite3"
    load_document(db_path, doc_path)
    migrate_db(db_path)   # applies 005 migration

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn, "ti"


class _FakeClient:
    def __init__(self, responses: list[LLMResult]) -> None:
        self.responses = list(responses)
        self.calls: list = []

    def call(self, messages, *, response_format=None, tag=""):
        self.calls.append({"messages": messages, "response_format": response_format, "tag": tag})
        if not self.responses:
            return _fake_result(None, json_error="no more responses")
        return self.responses.pop(0)


class _Config:
    model = "gpt-5.4-mini"
    temperature = 0.3
    seed = 20260612
    reasoning_effort = "none"
    verbosity = "low"
    max_output_tokens = 4096
    daily_token_cap = 2_400_000
    pricing = {"input": 0.25, "cached_input": 0.025, "output": 2.0}


def test_runner_translate_one_window(tmp_path):
    """One window → one call → persists translation_runs + memory_packs."""
    conn, doc_id = _make_doc_db(tmp_path)
    client = _FakeClient([_fake_result(_ok_response(["ch02_b001", "ch02_b002"]))])
    client.config = _Config()

    windows = [
        Window(window_id="w_ch02_001", block_ids=["ch02_b001", "ch02_b002"], est_src_tokens=50),
    ]

    report = translate_windows(conn, windows, client, "exp_test", "S0")

    assert report.windows_total == 1
    assert report.windows_translated == 1
    assert report.windows_failed == 0
    assert report.windows_skipped == 0
    assert report.blocks_translated == 2
    assert report.json_fail_rate == 0.0
    assert len(client.calls) == 1
    assert client.calls[0]["tag"] == "S0_w_ch02_001"

    rows = conn.execute(
        "SELECT block_id, output_text, config, window_id FROM translation_runs"
    ).fetchall()
    assert len(rows) == 2
    block_ids = {str(r["block_id"]) for r in rows}
    assert block_ids == {"ch02_b001", "ch02_b002"}
    for r in rows:
        assert r["config"] == "S0"
        assert r["window_id"] == "w_ch02_001"


def test_runner_resume_skips_completed_windows(tmp_path):
    """Windows where all blocks already have runs are skipped (no transport call)."""
    conn, doc_id = _make_doc_db(tmp_path)

    # Pre-seed translation runs for ALL blocks in the window
    for block_id in ["ch02_b001", "ch02_b002"]:
        conn.execute(
            """
            INSERT INTO translation_runs (
              run_id, experiment_id, doc_id, block_id, config, stage,
              output_text, model, prompt_version
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (f"tr_S0_{block_id}", "exp_test", doc_id, block_id, "S0", "draft",
             f"Pre-existing translation for {block_id}.", "gpt-5.4-mini", "s0_v1"),
        )
    conn.commit()

    client = _FakeClient([_fake_result(_ok_response(["ch02_b001", "ch02_b002"]))])
    client.config = _Config()

    windows = [
        Window(window_id="w_ch02_001", block_ids=["ch02_b001", "ch02_b002"], est_src_tokens=50),
    ]

    report = translate_windows(conn, windows, client, "exp_test", "S0")

    assert report.windows_skipped == 1
    assert report.windows_translated == 0
    assert len(client.calls) == 0


def test_runner_reask_then_fail(tmp_path):
    """First call returns bad JSON → re-ask once → still fail → window failed."""
    conn, doc_id = _make_doc_db(tmp_path)
    client = _FakeClient([
        _fake_result({"ch02_b002": "wrong"}),
        _fake_result({"ch02_b002": "wrong again"}),
    ])
    client.config = _Config()

    windows = [
        Window(window_id="w_ch02_001", block_ids=["ch02_b001"], est_src_tokens=50),
    ]

    report = translate_windows(conn, windows, client, "exp_test", "S0")

    assert report.windows_failed == 1
    assert report.windows_translated == 0
    assert len(client.calls) == 2
    assert report.blocks_failed == 1


def test_runner_partial_block_mismatch(tmp_path):
    """JSON has all blocks but missing one key → re-ask."""
    conn, doc_id = _make_doc_db(tmp_path)
    client = _FakeClient([
        _fake_result({"ch02_b001": "Translation 1"}),
        _fake_result(_ok_response(["ch02_b001", "ch02_b002"])),
    ])
    client.config = _Config()

    windows = [
        Window(window_id="w_ch02_001", block_ids=["ch02_b001", "ch02_b002"], est_src_tokens=50),
    ]

    report = translate_windows(conn, windows, client, "exp_test", "S0")

    assert report.windows_translated == 1
    assert report.windows_failed == 0
    assert len(client.calls) == 2


def test_runner_memory_packs_persisted(tmp_path):
    """memory_packs row written for each translated window."""
    conn, doc_id = _make_doc_db(tmp_path)
    client = _FakeClient([_fake_result(_ok_response(["ch02_b001"]))])
    client.config = _Config()

    windows = [
        Window(window_id="w_ch02_001", block_ids=["ch02_b001"], est_src_tokens=50),
    ]

    translate_windows(conn, windows, client, "exp_test", "S0")

    packs = conn.execute(
        "SELECT pack_id, payload_json, config FROM memory_packs"
    ).fetchall()
    assert len(packs) == 1
    assert packs[0]["pack_id"] == "pk_S0_w_ch02_001"
    assert packs[0]["config"] == "S0"
    import json as _json
    payload = _json.loads(packs[0]["payload_json"])
    assert payload["window_id"] == "w_ch02_001"


def test_runner_persists_pack_breakdown(tmp_path):
    """S1 memory_packs payload logs hard-constraint context observability."""
    conn, doc_id = _make_doc_db(tmp_path)
    conn.execute(
        """
        INSERT INTO glossary_entries (glossary_id, doc_id, source_term, target_term)
        VALUES ('gl_jim', 'ti', 'Jim', 'Jim')
        """
    )
    conn.commit()
    client = _FakeClient([_fake_result(_ok_response(["ch02_b001"]))])
    client.config = _Config()
    windows = [
        Window(window_id="w_ch02_001", block_ids=["ch02_b001"], est_src_tokens=50),
    ]

    report = translate_windows(conn, windows, client, "exp_test", "S1")

    pack = conn.execute(
        "SELECT payload_json, config FROM memory_packs WHERE pack_id = 'pk_S1_w_ch02_001'"
    ).fetchone()
    run = conn.execute(
        "SELECT config, prompt_version FROM translation_runs WHERE block_id = 'ch02_b001'"
    ).fetchone()
    payload = json.loads(pack["payload_json"])
    user_prompt = client.calls[0]["messages"][1]["content"]

    assert report.context_stats["windows_with_context"] == 1
    assert pack["config"] == "S1"
    assert run["config"] == "S1"
    assert run["prompt_version"] == "s1_literary_translator_v2"
    assert payload["zones"]["system_tokens"] > 0
    assert payload["zones"]["hard_constraints_tokens"] > 0
    assert payload["zones"]["source_tokens"] > 0
    assert payload["anchors_count"]["terms"] == 1
    assert payload["low_context"] is False
    assert payload["dropped_by_budget"] == []
    assert "MANDATORY TERMINOLOGY & NAMES" in user_prompt


def test_runner_multiple_windows(tmp_path):
    """Multiple windows translate sequentially."""
    conn, doc_id = _make_doc_db(tmp_path)
    client = _FakeClient([
        _fake_result(_ok_response(["ch02_b001"])),
        _fake_result(_ok_response(["ch02_b002"])),
        _fake_result(_ok_response(["ch02_b003"])),
    ])
    client.config = _Config()

    windows = [
        Window(window_id="w_ch02_001", block_ids=["ch02_b001"], est_src_tokens=50),
        Window(window_id="w_ch02_002", block_ids=["ch02_b002"], est_src_tokens=50),
        Window(window_id="w_ch02_003", block_ids=["ch02_b003"], est_src_tokens=50),
    ]

    report = translate_windows(conn, windows, client, "exp_test", "S0")

    assert report.windows_translated == 3
    assert report.blocks_translated == 3
    assert len(client.calls) == 3


def test_runner_report_fields(tmp_path):
    """TranslateReport contains all required fields."""
    conn, doc_id = _make_doc_db(tmp_path)
    client = _FakeClient([_fake_result(_ok_response(["ch02_b001"]), prompt=300, completion=80)])
    client.config = _Config()

    windows = [
        Window(window_id="w_ch02_001", block_ids=["ch02_b001"], est_src_tokens=50),
    ]

    report = translate_windows(conn, windows, client, "exp_test", "S0")

    assert report.experiment_id == "exp_test"
    assert report.config == "S0"
    assert report.total_usage["prompt_tokens"] == 300
    assert report.total_usage["completion_tokens"] == 80
    assert report.total_usage["calls"] == 1
    assert report.system_fingerprint == "fp_test"

    d = report.to_json_dict()
    assert d["windows_total"] == 1
    assert d["windows_translated"] == 1
