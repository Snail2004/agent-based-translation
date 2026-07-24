from __future__ import annotations

import json
import sqlite3
import tempfile
from dataclasses import replace
from hashlib import sha256
from pathlib import Path

import pytest

from pipeline.agents.llm_client import LLMResult, LLMUsage
from pipeline.ingest.document_loader import load_document
from pipeline.memory.store_init import migrate_db
from pipeline.translate.runner import (
    TranslateReport,
    WindowRunReport,
    _pack_summary_for_event,
    translate_windows,
)
from pipeline.translate.d2l_protected_spans_v1 import (
    POLICY_ID as D2L_PROTECTED_SPANS_POLICY_ID,
    PROMPT_VERSION as D2L_PROTECTED_SPANS_PROMPT_VERSION,
)
from pipeline.translate.d2l_latex_protected_spans_v2 import (
    POLICY_ID as D2L_LATEX_PROTECTED_SPANS_POLICY_ID,
    PROMPT_VERSION as D2L_LATEX_PROTECTED_SPANS_PROMPT_VERSION,
)
from pipeline.translate.d2l_latex_markup_line_protected_spans_v4 import (
    POLICY_ID as D2L_LINE_PROTECTED_SPANS_POLICY_ID,
    PROMPT_VERSION as D2L_LINE_PROTECTED_SPANS_PROMPT_VERSION,
)
from pipeline.translate.d2l_latex_markup_line_protected_spans_v5 import (
    POLICY_ID as D2L_HARDENED_PROTECTED_SPANS_POLICY_ID,
    PROMPT_VERSION as D2L_HARDENED_PROTECTED_SPANS_PROMPT_VERSION,
)
from pipeline.translate.d2l_prompt_json_envelope_v1 import (
    POLICY_ID as D2L_PROMPT_JSON_ENVELOPE_POLICY_ID,
)
from pipeline.translate.d2l_prompt_json_envelope_v2 import (
    POLICY_ID as D2L_PROMPT_JSON_ENVELOPE_V2_POLICY_ID,
)
from pipeline.translate.d2l_translation_slots_v1 import (
    GLOSSARY_REVIEW_POLICY_ID,
    POLICY_ID as D2L_TRANSLATION_SLOTS_POLICY_ID,
    PROMPT_VERSION as D2L_TRANSLATION_SLOTS_PROMPT_VERSION,
    PROTECTED_LEXICAL_GLOSSARY_REVIEW_POLICY_ID,
)
from pipeline.translate.run_events import EventSink
from pipeline.translate.windower import Window
from pipeline.translate.prompt import build_messages, prompt_version_for_config
from pipeline.retrieval.context_builder import Anchors, ContextPack, DroppedItem


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


def _experiment_scope(experiment_id: str) -> str:
    return sha256(experiment_id.encode("utf-8")).hexdigest()[:12]


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


class _SharedPreset:
    def __init__(self, role_id: str) -> None:
        self.role_id = role_id


class _SharedFakeClient(_FakeClient):
    uses_shared_backend = True

    def __init__(
        self,
        responses: list[LLMResult],
        *,
        role_id: str,
        transport_identity: str,
        resume_transport_identity: str | None = None,
    ) -> None:
        super().__init__(responses)
        self.config = _Config()
        self.preset = _SharedPreset(role_id)
        self.transport_identity = transport_identity
        if resume_transport_identity is not None:
            self.resume_transport_identity = resume_transport_identity


def _stable_translation_rows(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        """
        SELECT run_id, experiment_id, doc_id, block_id, config, stage,
               window_id, pack_id, output_text, model, prompt_version,
               temperature, seed, system_fingerprint, cost, latency_ms
        FROM translation_runs
        ORDER BY run_id
        """
    ).fetchall()
    return [dict(row) for row in rows]


def _stable_pack_rows(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        """
        SELECT pack_id, doc_id, block_id, pack_hash, prompt_version,
               estimated_tokens, payload_json, memory_refs_json,
               retrieval_debug_json, config
        FROM memory_packs
        ORDER BY pack_id
        """
    ).fetchall()
    return [dict(row) for row in rows]


def _read_events(path: Path) -> list[dict]:
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def test_pack_summary_for_event_maps_context_pack_counts() -> None:
    pack = ContextPack(
        glossary_lines=["gradient -> gradient"],
        preserve_lines=["PyTorch"],
        context_sensitive_lines=["shape -> hinh dang"],
        entity_lines=[],
        address_lines=[],
        token_estimate=123,
        anchors=Anchors(
            doc_id="d2l",
            block_ids=["b001"],
            term_block_ids={"gradient": ["b001"], "shape": ["b001"]},
            term_counts={"gradient": 1, "shape": 1},
            entity_block_ids={},
            entity_counts={},
            has_dialogue=False,
        ),
        dropped_by_budget=[
            DroppedItem(item_id="x", item_type="term", line="x -> y", reason="budget")
        ],
    )

    assert _pack_summary_for_event(pack) == {
        "injected": 3,
        "mandatory": 1,
        "soft": 1,
        "preserve": 1,
        "quarantine": 0,
        "address": 0,
        "dropped_by_budget": 1,
        "est_tokens": 123,
        "sample": {
            "mandatory": ["gradient -> gradient"],
            "soft": ["shape -> hinh dang"],
            "preserve": ["PyTorch"],
        },
        "more": {},
    }


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
    assert report.protected_spans is None
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


def test_shared_runner_binds_resume_identity_and_preserves_unknown_cost(
    tmp_path,
) -> None:
    conn, _ = _make_doc_db(tmp_path)
    block_ids = ["ch02_b001", "ch02_b002"]
    result = replace(
        _fake_result(_ok_response(block_ids)),
        cost_usd=None,
        system_fingerprint="shared:sealed-attempt",
    )
    identity = "a" * 64
    client = _SharedFakeClient(
        [result],
        role_id="d2l.translator.s0",
        transport_identity="physical-attempt-1",
        resume_transport_identity=identity,
    )
    windows = [
        Window(
            window_id="w_ch02_001",
            block_ids=block_ids,
            est_src_tokens=50,
        )
    ]

    report = translate_windows(conn, windows, client, "exp_shared", "S0")

    assert report.windows_translated == 1
    assert report.transport_identity == identity
    assert report.total_usage["cost_usd"] is None
    assert report.total_usage["incremental_cost_usd"] is None
    assert report.total_usage["cost_status"] == "unknown"
    assert report.reports[0].cost_usd is None
    assert report.reports[0].incremental_cost_usd is None
    assert report.to_json_dict()["shared_backend_used"] is True
    rows = conn.execute(
        "SELECT run_id, pack_id, cost FROM translation_runs ORDER BY block_id"
    ).fetchall()
    assert len(rows) == 2
    assert all("exp_shared" not in str(row["run_id"]) for row in rows)
    assert all(str(row["pack_id"]).endswith(identity[:20]) for row in rows)
    assert all(row["cost"] is None for row in rows)
    pack = conn.execute(
        "SELECT payload_json FROM memory_packs WHERE pack_id = ?",
        (rows[0]["pack_id"],),
    ).fetchone()
    assert json.loads(str(pack["payload_json"]))["transport_identity"] == identity

    resumed_client = _SharedFakeClient(
        [],
        role_id="d2l.translator.s0",
        transport_identity="physical-attempt-2",
        resume_transport_identity=identity,
    )
    resumed = translate_windows(
        conn,
        windows,
        resumed_client,
        "exp_shared",
        "S0",
    )
    assert resumed.windows_skipped == 1
    assert len(resumed_client.calls) == 0

    legacy = _FakeClient([_fake_result(_ok_response(block_ids))])
    legacy.config = _Config()
    with pytest.raises(RuntimeError, match="cannot resume shared-backend rows"):
        translate_windows(conn, windows, legacy, "exp_shared", "S0")

    foreign = _SharedFakeClient(
        [_fake_result(_ok_response(block_ids))],
        role_id="d2l.translator.s0",
        transport_identity="b" * 64,
        resume_transport_identity="c" * 64,
    )
    with pytest.raises(RuntimeError, match="resume identity conflicts"):
        translate_windows(conn, windows, foreign, "exp_shared", "S0")


@pytest.mark.parametrize(
    ("stored_prompt_version", "stored_policy"),
    [
        ("s1_d2l_v1", "d2l_soft_glossary_policy_v1_3"),
        ("s1_d2l_soft_glossary_v2_3", "d2l_soft_glossary_policy_v1"),
    ],
)
def test_legacy_resume_rejects_prompt_or_policy_drift_before_call(
    tmp_path,
    stored_prompt_version: str,
    stored_policy: str,
) -> None:
    conn, doc_id = _make_doc_db(tmp_path)
    experiment_id = "exp_soft_resume"
    scope = _experiment_scope(experiment_id)
    pack_id = f"pk_S1_w_ch02_001_{scope}"
    payload = {
        "experiment_id": experiment_id,
        "window_id": "w_ch02_001",
        "block_ids": ["ch02_b001"],
        "config": "S1",
        "terminology_policy": stored_policy,
        "term_override_match_rule": (
            "unicode_nfkc_casefold_alnum_tokens_exact_once_v1"
        ),
    }
    conn.execute(
        """
        INSERT INTO memory_packs (
          pack_id, doc_id, block_id, pack_hash, prompt_version,
          estimated_tokens, payload_json, config
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            pack_id,
            doc_id,
            "ch02_b001",
            "old-pack",
            stored_prompt_version,
            1,
            json.dumps(payload),
            "S1",
        ),
    )
    conn.execute(
        """
        INSERT INTO translation_runs (
          run_id, experiment_id, doc_id, block_id, config, stage,
          window_id, pack_id, output_text, model, prompt_version
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            f"tr_S1_ch02_b001_{scope}",
            experiment_id,
            doc_id,
            "ch02_b001",
            "S1",
            "draft",
            "w_ch02_001",
            pack_id,
            "Ban dich cu.",
            "gpt-5.4-mini",
            stored_prompt_version,
        ),
    )
    conn.commit()
    client = _FakeClient([])
    client.config = _Config()
    windows = [
        Window(window_id="w_ch02_001", block_ids=["ch02_b001"], est_src_tokens=20)
    ]

    with pytest.raises(RuntimeError, match="resume prompt/policy conflicts"):
        translate_windows(
            conn,
            windows,
            client,
            experiment_id,
            "S1",
            profile_name="technical_d2l_v1",
        )
    assert client.calls == []


def test_shared_resume_rejects_prompt_drift_before_call(tmp_path) -> None:
    conn, _ = _make_doc_db(tmp_path)
    identity = "c" * 64
    client = _SharedFakeClient(
        [_fake_result({"ch02_b001": "Ban dich moi."})],
        role_id="d2l.translator.s1",
        transport_identity=identity,
    )
    windows = [
        Window(window_id="w_ch02_001", block_ids=["ch02_b001"], est_src_tokens=20)
    ]

    first = translate_windows(
        conn,
        windows,
        client,
        "exp_shared_soft_resume",
        "S1",
        profile_name="technical_d2l_v1",
    )
    assert first.windows_translated == 1
    conn.execute(
        "UPDATE translation_runs SET prompt_version = 's1_d2l_v1'"
    )
    conn.execute(
        "UPDATE memory_packs SET prompt_version = 's1_d2l_v1'"
    )
    conn.commit()

    with pytest.raises(RuntimeError, match="resume prompt/policy conflicts"):
        translate_windows(
            conn,
            windows,
            client,
            "exp_shared_soft_resume",
            "S1",
            profile_name="technical_d2l_v1",
        )
    assert len(client.calls) == 1


def test_legacy_resume_rejects_historical_unscoped_ids_before_call(
    tmp_path,
) -> None:
    conn, doc_id = _make_doc_db(tmp_path)
    experiment_id = "exp_historical_unscoped"
    prompt_version = prompt_version_for_config("S1", "technical_d2l_v1")
    pack_id = "pk_S1_w_ch02_001"
    payload = {
        "window_id": "w_ch02_001",
        "block_ids": ["ch02_b001"],
        "config": "S1",
        "prompt_version": prompt_version,
        "terminology_policy": "d2l_soft_glossary_policy_v1_3",
        "term_override_match_rule": (
            "unicode_nfkc_casefold_alnum_tokens_exact_once_v1"
        ),
    }
    conn.execute(
        """
        INSERT INTO memory_packs (
          pack_id, doc_id, block_id, pack_hash, prompt_version,
          estimated_tokens, payload_json, config
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            pack_id,
            doc_id,
            "ch02_b001",
            "historical-pack",
            prompt_version,
            1,
            json.dumps(payload),
            "S1",
        ),
    )
    conn.execute(
        """
        INSERT INTO translation_runs (
          run_id, experiment_id, doc_id, block_id, config, stage,
          window_id, pack_id, output_text, model, prompt_version
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            "tr_S1_ch02_b001",
            experiment_id,
            doc_id,
            "ch02_b001",
            "S1",
            "draft",
            "w_ch02_001",
            pack_id,
            "Ban dich lich su.",
            "gpt-5.4-mini",
            prompt_version,
        ),
    )
    conn.commit()
    client = _FakeClient([])
    client.config = _Config()

    with pytest.raises(RuntimeError, match="historical unscoped resume rows"):
        translate_windows(
            conn,
            [
                Window(
                    window_id="w_ch02_001",
                    block_ids=["ch02_b001"],
                    est_src_tokens=20,
                )
            ],
            client,
            experiment_id,
            "S1",
            profile_name="technical_d2l_v1",
        )
    assert client.calls == []


def test_shared_runner_rejects_wrong_s0_s1_role(tmp_path) -> None:
    conn, _ = _make_doc_db(tmp_path)
    client = _SharedFakeClient(
        [],
        role_id="d2l.translator.s1",
        transport_identity="a" * 64,
    )
    windows = [
        Window(
            window_id="w_ch02_001",
            block_ids=["ch02_b001"],
            est_src_tokens=20,
        )
    ]

    with pytest.raises(RuntimeError, match="requires role d2l.translator.s0"):
        translate_windows(conn, windows, client, "exp_shared", "S0")


def test_event_sink_emits_window_sequence_and_uncommitted_preview(tmp_path):
    conn, doc_id = _make_doc_db(tmp_path)
    client = _FakeClient([_fake_result(_ok_response(["ch02_b001", "ch02_b002"]))])
    client.config = _Config()
    event_path = tmp_path / "run_events" / "run_test.jsonl"
    sink = EventSink(event_path, run_id="run_test", attempt_id="run_test")
    windows = [
        Window(window_id="w_ch02_001", block_ids=["ch02_b001", "ch02_b002"], est_src_tokens=50),
    ]

    report = translate_windows(conn, windows, client, "exp_test", "S0", event_sink=sink)

    assert report.windows_translated == 1
    events = _read_events(event_path)
    names = [event["event"] for event in events]
    assert names == [
        "window_started",
        "prompt_built",
        "request_sent",
        "response_received",
        "json_parsed",
        "window_preview_available",
        "persist_buffered",
        "run_committed",
    ]
    preview = next(event for event in events if event["event"] == "window_preview_available")
    assert preview["committed"] is False
    assert "ch02_b001" in preview["translations"]
    assert "preview" in preview["translations"]["ch02_b001"]
    assert "content" not in json.dumps(next(event for event in events if event["event"] == "prompt_built"))


def test_event_sink_best_effort_failure_does_not_crash(tmp_path):
    class FailingSink:
        def emit(self, event, **payload):
            raise OSError("event sink unavailable")

    conn, doc_id = _make_doc_db(tmp_path)
    client = _FakeClient([_fake_result(_ok_response(["ch02_b001"]))])
    client.config = _Config()

    report = translate_windows(
        conn,
        [Window(window_id="w_ch02_001", block_ids=["ch02_b001"], est_src_tokens=50)],
        client,
        "exp_test",
        "S0",
        event_sink=FailingSink(),
    )

    assert report.windows_translated == 1
    assert _stable_translation_rows(conn)[0]["block_id"] == "ch02_b001"


def test_event_sink_on_off_is_compute_identical_on_cloned_dbs(tmp_path):
    off_root = tmp_path / "off"
    on_root = tmp_path / "on"
    off_root.mkdir()
    on_root.mkdir()
    off_conn, _ = _make_doc_db(off_root)
    on_conn, _ = _make_doc_db(on_root)
    windows = [
        Window(window_id="w_ch02_001", block_ids=["ch02_b001"], est_src_tokens=50),
        Window(window_id="w_ch02_002", block_ids=["ch02_b002"], est_src_tokens=50),
    ]
    off_client = _FakeClient([
        _fake_result(_ok_response(["ch02_b001"])),
        _fake_result(_ok_response(["ch02_b002"])),
    ])
    on_client = _FakeClient([
        _fake_result(_ok_response(["ch02_b001"])),
        _fake_result(_ok_response(["ch02_b002"])),
    ])
    off_client.config = _Config()
    on_client.config = _Config()
    sink = EventSink(tmp_path / "run_events" / "run_test.jsonl", run_id="run_test")

    off_report = translate_windows(off_conn, windows, off_client, "exp_test", "S0")
    on_report = translate_windows(on_conn, windows, on_client, "exp_test", "S0", event_sink=sink)

    assert off_report.to_json_dict() == on_report.to_json_dict()
    assert _stable_translation_rows(off_conn) == _stable_translation_rows(on_conn)
    assert _stable_pack_rows(off_conn) == _stable_pack_rows(on_conn)


def test_runner_resume_skips_completed_windows(tmp_path):
    """Windows where all blocks already have runs are skipped (no transport call)."""
    conn, _ = _make_doc_db(tmp_path)

    windows = [
        Window(window_id="w_ch02_001", block_ids=["ch02_b001", "ch02_b002"], est_src_tokens=50),
    ]
    first_client = _FakeClient(
        [_fake_result(_ok_response(["ch02_b001", "ch02_b002"]))]
    )
    first_client.config = _Config()
    first = translate_windows(conn, windows, first_client, "exp_test", "S0")
    assert first.windows_translated == 1

    client = _FakeClient([])
    client.config = _Config()

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


def test_runner_hygiene_reasks_foreign_script_and_persists_clean_result(tmp_path):
    conn, doc_id = _make_doc_db(tmp_path)
    client = _FakeClient([
        _fake_result({"ch02_b001": "Bản dịch còn chữ либо không hợp lệ."}),
        _fake_result({"ch02_b001": "Bản dịch đã sạch."}),
    ])
    client.config = _Config()

    report = translate_windows(
        conn,
        [Window(window_id="w_ch02_001", block_ids=["ch02_b001"], est_src_tokens=50)],
        client,
        "exp_test",
        "S0",
    )

    row = conn.execute(
        "SELECT output_text FROM translation_runs WHERE block_id = 'ch02_b001'"
    ).fetchone()
    qa_count = conn.execute("SELECT COUNT(*) FROM qa_issues").fetchone()[0]
    pack = json.loads(
        conn.execute("SELECT payload_json FROM memory_packs").fetchone()[0]
    )

    assert report.windows_translated == 1
    assert len(client.calls) == 2
    assert "deterministic integrity checks" in client.calls[1]["messages"][-1]["content"]
    assert "unexpected_output_script" in client.calls[1]["messages"][-1]["content"]
    assert row["output_text"] == "Bản dịch đã sạch."
    assert qa_count == 0
    assert pack["translator_attempt_count"] == 2
    assert pack["deterministic_quality"]["reasked_blocks"] == ["ch02_b001"]
    assert pack["deterministic_quality"]["final_issues"] == []
    assert pack["deterministic_quality"]["retry_history"][0]["block_id"] == (
        "ch02_b001"
    )
    assert pack["deterministic_quality"]["retry_history"][0]["issue_type"] == (
        "unexpected_output_script"
    )
    assert report.total_usage["calls"] == 2


def test_runner_foreign_script_fails_row_after_single_reask_still_bad(tmp_path):
    conn, doc_id = _make_doc_db(tmp_path)
    client = _FakeClient([
        _fake_result({"ch02_b001": "Bản dịch còn либо."}),
        _fake_result({"ch02_b001": "Vẫn còn либо."}),
    ])
    client.config = _Config()

    report = translate_windows(
        conn,
        [Window(window_id="w_ch02_001", block_ids=["ch02_b001"], est_src_tokens=50)],
        client,
        "exp_test",
        "S0",
    )

    persisted = conn.execute(
        "SELECT COUNT(*) FROM translation_runs WHERE block_id = 'ch02_b001'"
    ).fetchone()[0]
    pack = json.loads(
        conn.execute("SELECT payload_json FROM memory_packs").fetchone()[0]
    )

    assert report.windows_translated == 0
    assert report.windows_failed == 1
    assert len(client.calls) == 2
    assert persisted == 0
    assert pack["translator_attempt_count"] == 2
    assert pack["deterministic_quality"]["final_issues"][0]["issue_type"] == (
        "unexpected_output_script"
    )


def test_runner_hygiene_allows_script_present_in_source(tmp_path):
    conn, doc_id = _make_doc_db(tmp_path)
    conn.execute(
        """
        UPDATE blocks
        SET text = 'The source token либо should be preserved.',
            original_text = 'The source token либо should be preserved.'
        WHERE block_id = 'ch02_b001'
        """
    )
    conn.commit()
    client = _FakeClient([
        _fake_result({"ch02_b001": "Giữ nguyên token nguồn либо trong bản dịch."}),
    ])
    client.config = _Config()

    report = translate_windows(
        conn,
        [Window(window_id="w_ch02_001", block_ids=["ch02_b001"], est_src_tokens=50)],
        client,
        "exp_test",
        "S0",
    )

    qa_count = conn.execute("SELECT COUNT(*) FROM qa_issues").fetchone()[0]
    assert report.windows_translated == 1
    assert len(client.calls) == 1
    assert qa_count == 0
    assert report.hygiene["flagged_blocks"] == 0


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
    assert packs[0]["pack_id"] == (
        f"pk_S0_w_ch02_001_{_experiment_scope('exp_test')}"
    )
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
        "SELECT payload_json, config FROM memory_packs WHERE pack_id = ?",
        (f"pk_S1_w_ch02_001_{_experiment_scope('exp_test')}",),
    ).fetchone()
    run = conn.execute(
        "SELECT config, prompt_version FROM translation_runs WHERE block_id = 'ch02_b001'"
    ).fetchone()
    payload = json.loads(pack["payload_json"])
    user_prompt = client.calls[0]["messages"][1]["content"]

    assert report.context_stats["windows_with_context"] == 1
    assert report.protected_spans is None
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


def test_runner_raises_before_api_when_context_drops_by_budget(tmp_path):
    conn, doc_id = _make_doc_db(tmp_path)
    client = _FakeClient([_fake_result(_ok_response(["ch02_b001"]))])
    client.config = _Config()
    windows = [
        Window(window_id="w_ch02_001", block_ids=["ch02_b001"], est_src_tokens=50),
    ]

    def context_builder(db, window, blocks_for_prompt):
        return ContextPack(
            glossary_lines=[],
            preserve_lines=[],
            context_sensitive_lines=[],
            entity_lines=[],
            address_lines=[],
            token_estimate=0,
            anchors=Anchors(
                doc_id="ti",
                block_ids=["ch02_b001"],
                term_block_ids={},
                term_counts={},
                entity_block_ids={},
                entity_counts={},
                has_dialogue=False,
            ),
            dropped_by_budget=[
                DroppedItem("g-too-large", "term", "large term -> thuật ngữ", "budget")
            ],
        )

    with pytest.raises(RuntimeError, match="Context budget fuse tripped"):
        translate_windows(
            conn,
            windows,
            client,
            "exp_test",
            "S1",
            context_builder=context_builder,
        )

    assert client.calls == []


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


def test_d2l_s1_soft_glossary_persists_validated_override(tmp_path):
    conn, _ = _make_doc_db(tmp_path)
    response = {
        "ch02_b001": "Gia tri mac dinh duoc su dung.",
        "__term_overrides__": [
            {
                "source_term": "defaults",
                "preferred_target_vi": "vo no",
                "used_target_vi": "mac dinh",
                "block_id": "ch02_b001",
                "reason_code": "different_source_sense",
            }
        ],
    }
    client = _FakeClient([_fake_result(response)])
    client.config = _Config()
    windows = [
        Window(window_id="w_ch02_001", block_ids=["ch02_b001"], est_src_tokens=50)
    ]

    def context_builder(db, window, blocks_for_prompt):
        return ContextPack(
            glossary_lines=["defaults -> vo no"],
            preserve_lines=["API (keep unchanged)"],
            context_sensitive_lines=[],
            entity_lines=[],
            address_lines=[],
            token_estimate=20,
            anchors=Anchors(
                doc_id="ti",
                block_ids=["ch02_b001"],
                term_block_ids={"term_defaults": ["ch02_b001"]},
                term_counts={"term_defaults": 1},
                entity_block_ids={},
                entity_counts={},
                has_dialogue=False,
            ),
        )

    report = translate_windows(
        conn,
        windows,
        client,
        "exp_d2l_soft",
        "S1",
        context_builder=context_builder,
        profile_name="technical_d2l_v1",
    )

    pack = conn.execute(
        "SELECT payload_json FROM memory_packs WHERE config = 'S1'"
    ).fetchone()
    payload = json.loads(pack["payload_json"])
    prompt = "\n".join(row["content"] for row in client.calls[0]["messages"])
    assert report.windows_translated == 1
    assert report.reports[0].term_overrides == 1
    assert report.terminology == {
        "policy_id": "d2l_soft_glossary_policy_v1_3",
        "windows_reporting_present": 1,
        "windows_reporting_omitted": 0,
        "windows_with_overrides": 1,
        "overrides_total": 1,
        "reason_counts": {"different_source_sense": 1},
    }
    assert payload["terminology_policy"] == "d2l_soft_glossary_policy_v1_3"
    assert payload["term_override_match_rule"] == (
        "unicode_nfkc_casefold_alnum_tokens_exact_once_v1"
    )
    assert payload["term_override_reporting_present"] is True
    assert payload["term_overrides"] == response["__term_overrides__"]
    pack_summary = _pack_summary_for_event(
        context_builder(None, None, None),
        terminology_policy="d2l_soft_glossary_policy_v1_3",
    )
    assert pack_summary["preferred"] == 1
    assert pack_summary["mandatory"] == 0
    assert pack_summary["terminology_policy"] == "d2l_soft_glossary_policy_v1_3"
    assert "PREFERRED TECHNICAL TERMS" in prompt
    assert "MANDATORY TERMINOLOGY" not in prompt
    assert "s1_d2l_soft_glossary_v2_3" in prompt


def _empty_d2l_context_builder(db, window, blocks_for_prompt):
    block_ids = [str(block["block_id"]) for block in blocks_for_prompt]
    return ContextPack(
        glossary_lines=[],
        preserve_lines=[],
        context_sensitive_lines=[],
        entity_lines=[],
        address_lines=[],
        token_estimate=0,
        anchors=Anchors(
            doc_id="ti",
            block_ids=block_ids,
            term_block_ids={},
            term_counts={},
            entity_block_ids={},
            entity_counts={},
            has_dialogue=False,
        ),
    )


def _set_protected_source(conn: sqlite3.Connection) -> str:
    source = "We denote $f: \\mathbb{R} \\rightarrow \\mathbb{R}$ using `f`."
    conn.execute(
        "UPDATE blocks SET text = ?, original_text = ? WHERE block_id = ?",
        (source, source, "ch02_b001"),
    )
    return source


def test_d2l_protected_spans_restore_exact_bytes_before_persistence(tmp_path):
    conn, _ = _make_doc_db(tmp_path)
    _set_protected_source(conn)
    response = {
        "ch02_b001": (
            "Ta bieu dien [[D2LPS_0001]] bang [[D2LPS_0002]]."
        )
    }
    client = _FakeClient([_fake_result(response)])
    client.config = _Config()
    windows = [
        Window(window_id="w_ch02_001", block_ids=["ch02_b001"], est_src_tokens=50)
    ]

    report = translate_windows(
        conn,
        windows,
        client,
        "exp_d2l_protected",
        "S1",
        context_builder=_empty_d2l_context_builder,
        profile_name="technical_d2l_v1",
        protected_spans_policy=D2L_PROTECTED_SPANS_POLICY_ID,
    )

    output = conn.execute(
        "SELECT output_text, prompt_version FROM translation_runs"
    ).fetchone()
    pack = json.loads(
        conn.execute("SELECT payload_json FROM memory_packs").fetchone()[
            "payload_json"
        ]
    )
    prompt = "\n".join(row["content"] for row in client.calls[0]["messages"])
    assert report.windows_translated == 1
    assert output["output_text"] == (
        "Ta bieu dien $f: \\mathbb{R} \\rightarrow \\mathbb{R}$ bang `f`."
    )
    assert output["prompt_version"] == D2L_PROTECTED_SPANS_PROMPT_VERSION
    assert pack["protected_spans"]["policy_id"] == D2L_PROTECTED_SPANS_POLICY_ID
    assert report.protected_spans == {
        "policy_id": D2L_PROTECTED_SPANS_POLICY_ID,
        "prompt_version": D2L_PROTECTED_SPANS_PROMPT_VERSION,
        "windows": 1,
        "blocks": 1,
        "spans": 2,
        "windows_reasked": 0,
        "blocks_flagged": 0,
        "windows_failed": 0,
        "final_issue_count": 0,
    }
    assert "[[D2LPS_0001]]" in prompt
    assert json.dumps(
        "$f: \\mathbb{R} \\rightarrow \\mathbb{R}$"
    ) in prompt
    assert "`f`" in prompt
    assert "We denote [[D2LPS_0001]] using [[D2LPS_0002]]." in prompt


def test_d2l_protected_spans_reask_once_then_restore(tmp_path):
    conn, _ = _make_doc_db(tmp_path)
    _set_protected_source(conn)
    client = _FakeClient(
        [
            _fake_result({"ch02_b001": "Ta bieu dien f bang f."}),
            _fake_result(
                {
                    "ch02_b001": (
                        "Ta bieu dien [[D2LPS_0001]] bang [[D2LPS_0002]]."
                    )
                }
            ),
        ]
    )
    client.config = _Config()
    windows = [
        Window(window_id="w_ch02_001", block_ids=["ch02_b001"], est_src_tokens=50)
    ]

    report = translate_windows(
        conn,
        windows,
        client,
        "exp_d2l_protected_reask",
        "S1",
        context_builder=_empty_d2l_context_builder,
        profile_name="technical_d2l_v1",
        protected_spans_policy=D2L_PROTECTED_SPANS_POLICY_ID,
    )

    assert report.windows_translated == 1
    assert report.reports[0].calls == 2
    assert report.protected_spans["windows_reasked"] == 1
    assert report.protected_spans["windows_failed"] == 0
    assert "Copy every [[D2LPS_####]]" in client.calls[1]["messages"][-1][
        "content"
    ]


def test_d2l_protected_spans_fail_closed_after_second_mismatch(tmp_path):
    conn, _ = _make_doc_db(tmp_path)
    _set_protected_source(conn)
    bad = _fake_result({"ch02_b001": "Ta bieu dien f bang f."})
    client = _FakeClient([bad, bad])
    client.config = _Config()
    windows = [
        Window(window_id="w_ch02_001", block_ids=["ch02_b001"], est_src_tokens=50)
    ]

    report = translate_windows(
        conn,
        windows,
        client,
        "exp_d2l_protected_fail",
        "S1",
        context_builder=_empty_d2l_context_builder,
        profile_name="technical_d2l_v1",
        protected_spans_policy=D2L_PROTECTED_SPANS_POLICY_ID,
    )

    assert report.windows_failed == 1
    assert report.blocks_failed == 1
    assert report.protected_spans["windows_failed"] == 1
    assert report.protected_spans["final_issue_count"] == 1
    assert conn.execute("SELECT COUNT(*) FROM translation_runs").fetchone()[0] == 0


def test_d2l_translation_slots_keep_model_to_translation_only(tmp_path):
    conn, _ = _make_doc_db(tmp_path)
    source = "Defaults use :eqref:`eq_derivative` with $x$."
    conn.execute(
        "UPDATE blocks SET text = ?, original_text = ? WHERE block_id = ?",
        (source, source, "ch02_b001"),
    )
    response = {
        "translations": {
            "T01": "Giá trị có sẵn dùng [[D2LPS_0001]] với [[D2LPS_0002]]."
        }
    }
    client = _FakeClient([_fake_result(response)])
    client.config = _Config()
    windows = [
        Window(window_id="w_ch02_001", block_ids=["ch02_b001"], est_src_tokens=50)
    ]

    def context_builder(db, window, blocks_for_prompt):
        return ContextPack(
            glossary_lines=["defaults -> mặc định"],
            preserve_lines=[],
            context_sensitive_lines=[],
            entity_lines=[],
            address_lines=[],
            token_estimate=10,
            anchors=Anchors(
                doc_id="ti",
                block_ids=["ch02_b001"],
                term_block_ids={"term_defaults": ["ch02_b001"]},
                term_counts={"term_defaults": 1},
                entity_block_ids={},
                entity_counts={},
                has_dialogue=False,
            ),
        )

    report = translate_windows(
        conn,
        windows,
        client,
        "exp_d2l_slots",
        "S1",
        context_builder=context_builder,
        profile_name="technical_d2l_v1",
        protected_spans_policy=D2L_PROTECTED_SPANS_POLICY_ID,
        translation_output_policy=D2L_TRANSLATION_SLOTS_POLICY_ID,
    )

    output = conn.execute(
        "SELECT block_id, output_text, prompt_version FROM translation_runs"
    ).fetchone()
    payload = json.loads(
        conn.execute("SELECT payload_json FROM memory_packs").fetchone()[
            "payload_json"
        ]
    )
    prompt = "\n".join(row["content"] for row in client.calls[0]["messages"])

    assert report.windows_translated == 1
    assert report.terminology is None
    assert report.reports[0].term_overrides == 0
    assert report.reports[0].glossary_reviews == 1
    assert report.translation_output == {
        "policy_id": D2L_TRANSLATION_SLOTS_POLICY_ID,
        "glossary_review_policy_id": GLOSSARY_REVIEW_POLICY_ID,
        "windows_with_reviews": 1,
        "review_rows_total": 1,
        "review_blocks": 1,
    }
    assert report.protected_spans == {
        "policy_id": D2L_PROTECTED_SPANS_POLICY_ID,
        "prompt_version": D2L_PROTECTED_SPANS_PROMPT_VERSION,
        "windows": 1,
        "blocks": 1,
        "spans": 2,
        "windows_reasked": 0,
        "blocks_flagged": 0,
        "windows_failed": 0,
        "final_issue_count": 0,
    }
    assert output["block_id"] == "ch02_b001"
    assert output["prompt_version"] == D2L_TRANSLATION_SLOTS_PROMPT_VERSION
    assert output["output_text"] == (
        "Giá trị có sẵn dùng :eqref:`eq_derivative` với $x$."
    )
    assert payload["translation_output_policy"] == D2L_TRANSLATION_SLOTS_POLICY_ID
    assert payload["slot_map"] == {"T01": "ch02_b001"}
    assert payload["glossary_review_policy"] == GLOSSARY_REVIEW_POLICY_ID
    assert payload["glossary_reviews"][0]["block_id"] == "ch02_b001"
    assert "term_overrides" not in payload
    assert "term_override_match_rule" not in payload
    assert "[T01] Defaults use" in prompt
    assert "__term_overrides__" not in prompt
    assert "[ch02_b001] Defaults use" not in prompt
    assert client.calls[0]["response_format"] == {"type": "json_object"}


def test_d2l_v4_restores_line_skeleton_and_normalizes_exact_json_fence(tmp_path):
    conn, _ = _make_doc_db(tmp_path)
    source = "* First item.\n* Second item."
    conn.execute(
        "UPDATE blocks SET text = ?, original_text = ? WHERE block_id = ?",
        (source, source, "ch02_b001"),
    )
    raw = (
        "```json\n"
        '{"translations":{"T01":"[[LINE_REF_0001]]Muc thu nhat.'
        '[[LINE_REF_0002]]Muc thu hai."}}\n'
        "```"
    )
    result = LLMResult(
        text=raw,
        parsed_json=None,
        json_error="Expecting value",
        model="gemini-3.5-flash",
        system_fingerprint=None,
        usage=LLMUsage(prompt_tokens=120, completion_tokens=40),
        cost_usd=0.0,
        latency_ms=50,
        from_cache=False,
        cache_key="fenced-v4",
    )
    client = _FakeClient([result])
    client.config = _Config()

    report = translate_windows(
        conn,
        [Window(window_id="w_v4", block_ids=["ch02_b001"], est_src_tokens=30)],
        client,
        "exp_d2l_v4_fence",
        "S1",
        context_builder=_empty_d2l_context_builder,
        profile_name="technical_d2l_v1",
        protected_spans_policy=D2L_LINE_PROTECTED_SPANS_POLICY_ID,
        translation_output_policy=D2L_TRANSLATION_SLOTS_POLICY_ID,
        response_envelope_policy=D2L_PROMPT_JSON_ENVELOPE_POLICY_ID,
    )

    output = conn.execute(
        "SELECT output_text, prompt_version FROM translation_runs"
    ).fetchone()
    pack = json.loads(
        conn.execute("SELECT payload_json FROM memory_packs").fetchone()[
            "payload_json"
        ]
    )

    assert report.windows_translated == 1
    assert report.reports[0].calls == 1
    assert output["output_text"] == "* Muc thu nhat.\n* Muc thu hai."
    assert output["prompt_version"] == D2L_LINE_PROTECTED_SPANS_PROMPT_VERSION
    assert report.translation_output["responses_envelope_normalized"] == 1
    assert report.translation_output["glossary_review_policy_id"] == (
        PROTECTED_LEXICAL_GLOSSARY_REVIEW_POLICY_ID
    )
    assert pack["response_envelope_policy"] == D2L_PROMPT_JSON_ENVELOPE_POLICY_ID
    assert pack["protected_spans"]["line_span_count"] == 2


def test_d2l_v4_normalizes_one_json_object_with_harmless_trailing_prose(tmp_path):
    conn, _ = _make_doc_db(tmp_path)
    source = "* First item.\n* Second item."
    conn.execute(
        "UPDATE blocks SET text = ?, original_text = ? WHERE block_id = ?",
        (source, source, "ch02_b001"),
    )
    raw = (
        '{"translations":{"T01":"[[LINE_REF_0001]]Muc thu nhat.'
        '[[LINE_REF_0002]]Muc thu hai."}} Converted to JSON._\n'
    )
    result = LLMResult(
        text=raw,
        parsed_json=None,
        json_error="Extra data",
        model="gemini-3.5-flash",
        system_fingerprint=None,
        usage=LLMUsage(prompt_tokens=120, completion_tokens=40),
        cost_usd=0.0,
        latency_ms=50,
        from_cache=False,
        cache_key="trailing-prose-v4",
    )
    client = _FakeClient([result])
    client.config = _Config()

    report = translate_windows(
        conn,
        [Window(window_id="w_v4_tail", block_ids=["ch02_b001"], est_src_tokens=30)],
        client,
        "exp_d2l_v4_tail",
        "S1",
        context_builder=_empty_d2l_context_builder,
        profile_name="technical_d2l_v1",
        protected_spans_policy=D2L_LINE_PROTECTED_SPANS_POLICY_ID,
        translation_output_policy=D2L_TRANSLATION_SLOTS_POLICY_ID,
        response_envelope_policy=D2L_PROMPT_JSON_ENVELOPE_V2_POLICY_ID,
    )

    output = conn.execute(
        "SELECT output_text FROM translation_runs"
    ).fetchone()
    pack = json.loads(
        conn.execute("SELECT payload_json FROM memory_packs").fetchone()[
            "payload_json"
        ]
    )

    assert report.windows_translated == 1
    assert report.reports[0].calls == 1
    assert output["output_text"] == "* Muc thu nhat.\n* Muc thu hai."
    assert report.translation_output["responses_envelope_normalized"] == 1
    assert pack["response_envelope_policy"] == D2L_PROMPT_JSON_ENVELOPE_V2_POLICY_ID


def test_d2l_v4_fixed_only_block_is_preserved_without_false_retry(tmp_path):
    conn, _ = _make_doc_db(tmp_path)
    source = r"$$\operatorname*{argmin}_{x} f(x)$$"
    conn.execute(
        "UPDATE blocks SET text = ?, original_text = ? WHERE block_id = ?",
        (source, source, "ch02_b001"),
    )
    client = _FakeClient(
        [_fake_result({"translations": {"T01": "[[MATH_REF_0001]]"}})]
    )
    client.config = _Config()

    report = translate_windows(
        conn,
        [Window(window_id="w_fixed", block_ids=["ch02_b001"], est_src_tokens=20)],
        client,
        "exp_d2l_v4_fixed",
        "S1",
        context_builder=_empty_d2l_context_builder,
        profile_name="technical_d2l_v1",
        protected_spans_policy=D2L_LINE_PROTECTED_SPANS_POLICY_ID,
        translation_output_policy=D2L_TRANSLATION_SLOTS_POLICY_ID,
        response_envelope_policy=D2L_PROMPT_JSON_ENVELOPE_V2_POLICY_ID,
    )

    output = conn.execute(
        "SELECT output_text FROM translation_runs"
    ).fetchone()["output_text"]
    assert report.windows_translated == 1
    assert report.reports[0].calls == 1
    assert output == source


def test_d2l_v5_fixed_only_block_discards_model_authored_prose(tmp_path):
    conn, _ = _make_doc_db(tmp_path)
    source = (
        r"(**$$f'(x) = \lim_{h \rightarrow 0} "
        r"\frac{f(x+h) - f(x)}{h},$$**)"
        "\n:eqlabel:`eq_derivative`"
    )
    conn.execute(
        "UPDATE blocks SET text = ?, original_text = ? WHERE block_id = ?",
        (source, source, "ch02_b001"),
    )
    client = _FakeClient(
        [
            _fake_result(
                {
                    "translations": {
                        "T01": (
                            "Ta co [[MATH_REF_0001]]"
                            "[[STRUCT_REF_0001]]"
                        )
                    }
                }
            )
        ]
    )
    client.config = _Config()

    report = translate_windows(
        conn,
        [Window(window_id="w_fixed_v5", block_ids=["ch02_b001"], est_src_tokens=20)],
        client,
        "exp_d2l_v5_fixed",
        "S1",
        context_builder=_empty_d2l_context_builder,
        profile_name="technical_d2l_v1",
        protected_spans_policy=D2L_HARDENED_PROTECTED_SPANS_POLICY_ID,
        translation_output_policy=D2L_TRANSLATION_SLOTS_POLICY_ID,
        response_envelope_policy=D2L_PROMPT_JSON_ENVELOPE_V2_POLICY_ID,
    )

    output = conn.execute(
        "SELECT output_text, prompt_version FROM translation_runs"
    ).fetchone()
    assert report.windows_translated == 1
    assert report.reports[0].calls == 1
    assert output["output_text"] == source
    assert output["prompt_version"] == (
        D2L_HARDENED_PROTECTED_SPANS_PROMPT_VERSION
    )


def test_d2l_v5_restores_bracketed_emphasis_around_translation(tmp_path):
    conn, _ = _make_doc_db(tmp_path)
    source = (
        "As with an ordinary Python array,\n"
        "we [**can access the length of a tensor**]\n"
        "by calling Python's built-in `len()` function."
    )
    conn.execute(
        "UPDATE blocks SET text = ?, original_text = ? WHERE block_id = ?",
        (source, source, "ch02_b001"),
    )
    client = _FakeClient(
        [
            _fake_result(
                {
                    "translations": {
                        "T01": (
                            "Cũng như với một mảng Python thông thường,\n"
                            "ta [[FORMAT_REF_0001|có thể truy cập độ dài của một tensor]]\n"
                            "bằng cách gọi hàm [[STRUCT_REF_0001]] tích hợp sẵn."
                        )
                    }
                }
            )
        ]
    )
    client.config = _Config()

    report = translate_windows(
        conn,
        [
            Window(
                window_id="w_bracketed_v5",
                block_ids=["ch02_b001"],
                est_src_tokens=35,
            )
        ],
        client,
        "exp_d2l_v5_bracketed",
        "S1",
        context_builder=_empty_d2l_context_builder,
        profile_name="technical_d2l_v1",
        protected_spans_policy=D2L_HARDENED_PROTECTED_SPANS_POLICY_ID,
        translation_output_policy=D2L_TRANSLATION_SLOTS_POLICY_ID,
        response_envelope_policy=D2L_PROMPT_JSON_ENVELOPE_V2_POLICY_ID,
    )

    output = conn.execute(
        "SELECT output_text FROM translation_runs"
    ).fetchone()["output_text"]
    assert report.windows_translated == 1
    assert report.reports[0].calls == 1
    assert output == (
        "Cũng như với một mảng Python thông thường,\n"
        "ta [**có thể truy cập độ dài của một tensor**]\n"
        "bằng cách gọi hàm `len()` tích hợp sẵn."
    )


def test_d2l_s0_uses_protected_slots_without_glossary_context(tmp_path):
    conn, _ = _make_doc_db(tmp_path)
    source = r"Use $x$."
    conn.execute(
        "UPDATE blocks SET text = ?, original_text = ? WHERE block_id = ?",
        (source, source, "ch02_b001"),
    )
    client = _FakeClient(
        [_fake_result({"translations": {"T01": "Dung [[MATH_REF_0001]]."}})]
    )
    client.config = _Config()

    def forbidden_context_builder(*_args, **_kwargs):
        raise AssertionError("S0 must not build or receive glossary context")

    report = translate_windows(
        conn,
        [Window(window_id="w_s0_safe", block_ids=["ch02_b001"], est_src_tokens=20)],
        client,
        "exp_d2l_s0_safe",
        "S0",
        context_builder=forbidden_context_builder,
        profile_name="technical_d2l_v1",
        protected_spans_policy=D2L_LINE_PROTECTED_SPANS_POLICY_ID,
        translation_output_policy=D2L_TRANSLATION_SLOTS_POLICY_ID,
        response_envelope_policy=D2L_PROMPT_JSON_ENVELOPE_V2_POLICY_ID,
    )

    output = conn.execute(
        "SELECT output_text FROM translation_runs"
    ).fetchone()["output_text"]
    prompt = "\n".join(row["content"] for row in client.calls[0]["messages"])
    assert report.windows_translated == 1
    assert output == "Dung $x$."
    assert "[T01] Use [[MATH_REF_0001]]." in prompt
    assert "glossary" not in prompt.casefold()
    assert "term_overrides" not in prompt


def test_d2l_v4_context_retains_inline_code_while_review_ignores_protected_identifier(
    tmp_path,
):
    conn, _ = _make_doc_db(tmp_path)
    source = "We define `ones` as $f$.\n:eqlabel:`eq_derivative`"
    conn.execute(
        "UPDATE blocks SET text = ?, original_text = ? WHERE block_id = ?",
        (source, source, "ch02_b001"),
    )
    observed_context_sources: list[str] = []

    def context_builder(db, window, blocks_for_prompt):
        observed_context_sources.extend(
            str(block.get("source_text") or "") for block in blocks_for_prompt
        )
        return ContextPack(
            glossary_lines=["derivative -> dao ham"],
            preserve_lines=["ones (keep unchanged)"],
            context_sensitive_lines=[],
            entity_lines=[],
            address_lines=[],
            token_estimate=5,
            anchors=Anchors(
                doc_id="ti",
                block_ids=["ch02_b001"],
                term_block_ids={"term_derivative": ["ch02_b001"]},
                term_counts={"term_derivative": 1},
                entity_block_ids={},
                entity_counts={},
                has_dialogue=False,
            ),
        )

    response = {
        "translations": {
            "T01": (
                "Ta dinh nghia [[STRUCT_REF_0001]] la "
                "[[MATH_REF_0001]].[[LINE_REF_0001]]"
            )
        }
    }
    client = _FakeClient([_fake_result(response)])
    client.config = _Config()

    report = translate_windows(
        conn,
        [Window(window_id="w_v4_lex", block_ids=["ch02_b001"], est_src_tokens=30)],
        client,
        "exp_d2l_v4_lexical",
        "S1",
        context_builder=context_builder,
        profile_name="technical_d2l_v1",
        protected_spans_policy=D2L_LINE_PROTECTED_SPANS_POLICY_ID,
        translation_output_policy=D2L_TRANSLATION_SLOTS_POLICY_ID,
    )

    assert report.windows_translated == 1
    assert report.reports[0].glossary_reviews == 0
    assert "`ones`" in observed_context_sources[0]
    assert "eq_derivative" not in observed_context_sources[0]


def test_d2l_response_envelope_policy_is_part_of_resume_identity(tmp_path):
    conn, _ = _make_doc_db(tmp_path)
    source = "* First item.\n* Second item."
    conn.execute(
        "UPDATE blocks SET text = ?, original_text = ? WHERE block_id = ?",
        (source, source, "ch02_b001"),
    )
    response = {
        "translations": {
            "T01": "[[LINE_REF_0001]]Muc mot.[[LINE_REF_0002]]Muc hai."
        }
    }
    first = _FakeClient([_fake_result(response)])
    first.config = _Config()
    window = Window(window_id="w_v4_resume", block_ids=["ch02_b001"], est_src_tokens=30)
    translate_windows(
        conn,
        [window],
        first,
        "exp_d2l_v4_resume",
        "S1",
        context_builder=_empty_d2l_context_builder,
        profile_name="technical_d2l_v1",
        protected_spans_policy=D2L_LINE_PROTECTED_SPANS_POLICY_ID,
        translation_output_policy=D2L_TRANSLATION_SLOTS_POLICY_ID,
    )

    resumed = _FakeClient([])
    resumed.config = _Config()
    with pytest.raises(RuntimeError, match="prompt/policy conflicts"):
        translate_windows(
            conn,
            [window],
            resumed,
            "exp_d2l_v4_resume",
            "S1",
            context_builder=_empty_d2l_context_builder,
            profile_name="technical_d2l_v1",
            protected_spans_policy=D2L_LINE_PROTECTED_SPANS_POLICY_ID,
            translation_output_policy=D2L_TRANSLATION_SLOTS_POLICY_ID,
            response_envelope_policy=D2L_PROMPT_JSON_ENVELOPE_POLICY_ID,
        )
    assert resumed.calls == []


def test_d2l_latex_v2_hides_source_bytes_and_restores_before_persistence(tmp_path):
    conn, _ = _make_doc_db(tmp_path)
    source = (
        r"Vector $\mathbf{x}$ belongs to \(\mathbb{R}^{m\times n}\); "
        r"call `reshape`."
    )
    conn.execute(
        "UPDATE blocks SET text = ?, original_text = ? WHERE block_id = ?",
        (source, source, "ch02_b001"),
    )
    response = {
        "translations": {
            "T01": (
                "Vector [[MATH_REF_0001]] thuoc [[MATH_REF_0002]]; "
                "goi [[STRUCT_REF_0001]]."
            )
        }
    }
    client = _FakeClient([_fake_result(response)])
    client.config = _Config()
    windows = [
        Window(window_id="w_ch02_001", block_ids=["ch02_b001"], est_src_tokens=50)
    ]

    report = translate_windows(
        conn,
        windows,
        client,
        "exp_d2l_latex_v2",
        "S1",
        context_builder=_empty_d2l_context_builder,
        profile_name="technical_d2l_v1",
        protected_spans_policy=D2L_LATEX_PROTECTED_SPANS_POLICY_ID,
        translation_output_policy=D2L_TRANSLATION_SLOTS_POLICY_ID,
    )

    output = conn.execute(
        "SELECT output_text, prompt_version FROM translation_runs"
    ).fetchone()
    pack = json.loads(
        conn.execute("SELECT payload_json FROM memory_packs").fetchone()[
            "payload_json"
        ]
    )
    prompt = "\n".join(row["content"] for row in client.calls[0]["messages"])

    assert report.windows_translated == 1
    assert output["output_text"] == (
        r"Vector $\mathbf{x}$ thuoc \(\mathbb{R}^{m\times n}\); goi `reshape`."
    )
    assert output["prompt_version"] == D2L_LATEX_PROTECTED_SPANS_PROMPT_VERSION
    assert pack["protected_spans"]["policy_id"] == (
        D2L_LATEX_PROTECTED_SPANS_POLICY_ID
    )
    assert pack["protected_spans"]["latex_visible_to_model"] is False
    assert report.protected_spans["latex_visible_to_model"] is False
    assert "[[MATH_REF_0001]]" in prompt
    assert r"\mathbf{x}" not in prompt
    assert r"\mathbb{R}" not in prompt
    assert "`reshape`" not in prompt


def test_d2l_translation_slots_reask_on_model_authored_metadata(tmp_path):
    conn, _ = _make_doc_db(tmp_path)
    _set_protected_source(conn)
    client = _FakeClient(
        [
            _fake_result(
                {
                    "translations": {
                        "T01": "Dùng [[D2LPS_0001]] với [[D2LPS_0002]]."
                    },
                    "__term_overrides__": [],
                }
            ),
            _fake_result(
                {
                    "translations": {
                        "T01": "Dùng [[D2LPS_0001]] với [[D2LPS_0002]]."
                    }
                }
            ),
        ]
    )
    client.config = _Config()
    windows = [
        Window(window_id="w_ch02_001", block_ids=["ch02_b001"], est_src_tokens=50)
    ]

    report = translate_windows(
        conn,
        windows,
        client,
        "exp_d2l_slots_reask",
        "S1",
        context_builder=_empty_d2l_context_builder,
        profile_name="technical_d2l_v1",
        protected_spans_policy=D2L_PROTECTED_SPANS_POLICY_ID,
        translation_output_policy=D2L_TRANSLATION_SLOTS_POLICY_ID,
    )

    assert report.windows_translated == 1
    assert report.reports[0].calls == 2
    assert "Unexpected top-level key: __term_overrides__" in client.calls[1][
        "messages"
    ][-1]["content"]
    assert "exactly these slots: T01" in client.calls[1]["messages"][-1][
        "content"
    ]


def test_d2l_translation_slots_resume_rejects_legacy_output_contract(tmp_path):
    conn, _ = _make_doc_db(tmp_path)
    windows = [
        Window(window_id="w_ch02_001", block_ids=["ch02_b001"], est_src_tokens=50)
    ]
    legacy_client = _FakeClient([_fake_result({"ch02_b001": "Bản dịch cũ."})])
    legacy_client.config = _Config()
    translate_windows(
        conn,
        windows,
        legacy_client,
        "exp_d2l_resume_contract",
        "S1",
        context_builder=_empty_d2l_context_builder,
        profile_name="technical_d2l_v1",
    )

    slot_client = _FakeClient([])
    slot_client.config = _Config()
    with pytest.raises(RuntimeError, match="prompt/policy conflicts"):
        translate_windows(
            conn,
            windows,
            slot_client,
            "exp_d2l_resume_contract",
            "S1",
            context_builder=_empty_d2l_context_builder,
            profile_name="technical_d2l_v1",
            protected_spans_policy=D2L_PROTECTED_SPANS_POLICY_ID,
            translation_output_policy=D2L_TRANSLATION_SLOTS_POLICY_ID,
        )
    assert slot_client.calls == []


def test_d2l_latex_v2_resume_rejects_v1_protection_identity(tmp_path):
    conn, _ = _make_doc_db(tmp_path)
    _set_protected_source(conn)
    windows = [
        Window(window_id="w_ch02_001", block_ids=["ch02_b001"], est_src_tokens=50)
    ]
    v1_client = _FakeClient(
        [
            _fake_result(
                {
                    "translations": {
                        "T01": (
                            "Use [[D2LPS_0001]] with [[D2LPS_0002]]."
                        )
                    }
                }
            )
        ]
    )
    v1_client.config = _Config()
    translate_windows(
        conn,
        windows,
        v1_client,
        "exp_d2l_latex_policy_drift",
        "S1",
        context_builder=_empty_d2l_context_builder,
        profile_name="technical_d2l_v1",
        protected_spans_policy=D2L_PROTECTED_SPANS_POLICY_ID,
        translation_output_policy=D2L_TRANSLATION_SLOTS_POLICY_ID,
    )

    v2_client = _FakeClient([])
    v2_client.config = _Config()
    with pytest.raises(RuntimeError, match="prompt/policy conflicts"):
        translate_windows(
            conn,
            windows,
            v2_client,
            "exp_d2l_latex_policy_drift",
            "S1",
            context_builder=_empty_d2l_context_builder,
            profile_name="technical_d2l_v1",
            protected_spans_policy=D2L_LATEX_PROTECTED_SPANS_POLICY_ID,
            translation_output_policy=D2L_TRANSLATION_SLOTS_POLICY_ID,
        )
    assert v2_client.calls == []


def test_legacy_soft_glossary_isolates_two_experiments_in_one_db(
    tmp_path,
) -> None:
    conn, _ = _make_doc_db(tmp_path)
    windows = [
        Window(window_id="w_ch02_001", block_ids=["ch02_b001"], est_src_tokens=50)
    ]

    def context_builder(db, window, blocks_for_prompt):
        return ContextPack(
            glossary_lines=["defaults -> vo no"],
            preserve_lines=[],
            context_sensitive_lines=[],
            entity_lines=[],
            address_lines=[],
            token_estimate=10,
            anchors=Anchors(
                doc_id="ti",
                block_ids=["ch02_b001"],
                term_block_ids={"term_defaults": ["ch02_b001"]},
                term_counts={"term_defaults": 1},
                entity_block_ids={},
                entity_counts={},
                has_dialogue=False,
            ),
        )

    responses = {
        "exp_a": {
            "ch02_b001": "Gia tri mac dinh.",
            "__term_overrides__": [
                {
                    "source_term": "defaults",
                    "preferred_target_vi": "vo no",
                    "used_target_vi": "mac dinh",
                    "block_id": "ch02_b001",
                    "reason_code": "different_source_sense",
                }
            ],
        },
        "exp_b": {
            "ch02_b001": "Gia tri san co.",
            "__term_overrides__": [
                {
                    "source_term": "defaults",
                    "preferred_target_vi": "vo no",
                    "used_target_vi": "san co",
                    "block_id": "ch02_b001",
                    "reason_code": "different_source_sense",
                }
            ],
        },
    }
    clients = {}
    for experiment_id, response in responses.items():
        client = _FakeClient([_fake_result(response)])
        client.config = _Config()
        clients[experiment_id] = client
        report = translate_windows(
            conn,
            windows,
            client,
            experiment_id,
            "S1",
            context_builder=context_builder,
            profile_name="technical_d2l_v1",
        )
        assert report.windows_translated == 1
        assert report.reports[0].term_overrides == 1

    rows = conn.execute(
        """
        SELECT tr.experiment_id, tr.run_id, tr.output_text, tr.pack_id,
               mp.payload_json
        FROM translation_runs AS tr
        JOIN memory_packs AS mp ON mp.pack_id = tr.pack_id
        ORDER BY tr.experiment_id
        """
    ).fetchall()
    assert len(rows) == 2
    assert len({str(row["run_id"]) for row in rows}) == 2
    assert len({str(row["pack_id"]) for row in rows}) == 2
    assert {str(row["experiment_id"]) for row in rows} == {"exp_a", "exp_b"}
    expected_targets = {"exp_a": "mac dinh", "exp_b": "san co"}
    expected_outputs = {
        "exp_a": "Gia tri mac dinh.",
        "exp_b": "Gia tri san co.",
    }
    for row in rows:
        experiment_id = str(row["experiment_id"])
        payload = json.loads(str(row["payload_json"]))
        assert payload["experiment_id"] == experiment_id
        assert row["output_text"] == expected_outputs[experiment_id]
        assert payload["term_overrides"][0]["used_target_vi"] == (
            expected_targets[experiment_id]
        )
        assert len(clients[experiment_id].calls) == 1


def test_d2l_s1_false_override_is_reasked_and_never_counted_or_persisted(
    tmp_path,
) -> None:
    conn, _ = _make_doc_db(tmp_path)
    false_override = {
        "ch02_b001": "Ban dich khong he co tu da khai.",
        "__term_overrides__": [
            {
                "source_term": "defaults",
                "preferred_target_vi": "vo no",
                "used_target_vi": "mac dinh",
                "block_id": "ch02_b001",
                "reason_code": "different_source_sense",
            }
        ],
    }
    corrected = {
        "ch02_b001": "Ban dich khong can ghi de.",
        "__term_overrides__": [],
    }
    client = _FakeClient(
        [_fake_result(false_override), _fake_result(corrected)]
    )
    client.config = _Config()
    windows = [
        Window(window_id="w_ch02_001", block_ids=["ch02_b001"], est_src_tokens=50)
    ]

    def context_builder(db, window, blocks_for_prompt):
        return ContextPack(
            glossary_lines=["defaults -> vo no"],
            preserve_lines=[],
            context_sensitive_lines=[],
            entity_lines=[],
            address_lines=[],
            token_estimate=10,
            anchors=Anchors(
                doc_id="ti",
                block_ids=["ch02_b001"],
                term_block_ids={"term_defaults": ["ch02_b001"]},
                term_counts={"term_defaults": 1},
                entity_block_ids={},
                entity_counts={},
                has_dialogue=False,
            ),
        )

    report = translate_windows(
        conn,
        windows,
        client,
        "exp_d2l_false_override",
        "S1",
        context_builder=context_builder,
        profile_name="technical_d2l_v1",
    )

    payload = json.loads(
        conn.execute(
            "SELECT payload_json FROM memory_packs WHERE config = 'S1'"
        ).fetchone()["payload_json"]
    )
    assert report.windows_translated == 1
    assert report.reports[0].calls == 2
    assert report.terminology["overrides_total"] == 0
    assert payload["term_overrides"] == []
    assert payload["term_override_reporting_present"] is True
    assert conn.execute(
        "SELECT output_text FROM translation_runs WHERE block_id = 'ch02_b001'"
    ).fetchone()["output_text"] == corrected["ch02_b001"]


def test_legacy_report_shape_does_not_add_terminology_fields() -> None:
    report = TranslateReport(
        experiment_id="legacy",
        config="S0",
        windows_total=1,
        windows_translated=1,
        windows_failed=0,
        windows_skipped=0,
        blocks_translated=1,
        blocks_failed=0,
        json_fail_rate=0.0,
        total_usage={},
        context_stats={},
        hygiene={},
        model="fake",
        seed=0,
        system_fingerprint=None,
        reports=[
            WindowRunReport(
                window_id="w1",
                status="translated",
                calls=1,
                block_count=1,
                prompt_tokens=1,
                completion_tokens=1,
                reasoning_tokens=0,
                cost_usd=0.0,
                incremental_cost_usd=0.0,
                from_cache=False,
                system_fingerprint=None,
                errors=[],
            )
        ],
    )
    payload = report.to_json_dict()
    assert "terminology" not in payload
    assert "term_overrides" not in payload["reports"][0]
