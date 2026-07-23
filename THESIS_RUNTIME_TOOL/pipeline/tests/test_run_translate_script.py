from __future__ import annotations

import json
import sqlite3
import sys

import pytest

from pipeline.scripts import run_translate
from pipeline.tests.test_translate_runner import _make_doc_db
from pipeline.translate.runner import TranslateReport
from pipeline.translate.d2l_protected_spans_v1 import (
    POLICY_ID as D2L_PROTECTED_SPANS_POLICY_ID,
)
from pipeline.translate.d2l_latex_protected_spans_v2 import (
    POLICY_ID as D2L_LATEX_PROTECTED_SPANS_POLICY_ID,
)
from pipeline.translate.d2l_translation_slots_v1 import (
    POLICY_ID as D2L_TRANSLATION_SLOTS_POLICY_ID,
)


def test_preflight_only_writes_report_and_truncation_metadata(tmp_path, monkeypatch):
    conn, _ = _make_doc_db(tmp_path)
    conn.close()
    db_path = tmp_path / "memory.sqlite3"
    report_path = tmp_path / "preflight.json"

    monkeypatch.setattr(
        sys,
        "argv",
        [
            "run_translate",
            "--db",
            str(db_path),
            "--chapters",
            "ti_ch02",
            "--configs",
            "S0",
            "--preflight-only",
            "--max-windows",
            "1",
            "--report",
            str(report_path),
        ],
    )

    assert run_translate.main() == 0

    payload = json.loads(report_path.read_text(encoding="utf-8"))
    assert payload["zero_api"] is True
    assert payload["run"]["zero_api"] is True
    assert payload["run"]["context_budget"] == 1500
    assert payload["preflight"]["windows_original"] == payload["preflight"]["windows"]
    assert payload["preflight"]["windows_truncated_to"] == 1
    assert len(payload["preflight"]["window_ids"]) == payload["preflight"]["windows"]


def test_non_preflight_requires_workdb(tmp_path, monkeypatch):
    conn, _ = _make_doc_db(tmp_path)
    conn.close()
    db_path = tmp_path / "memory.sqlite3"

    monkeypatch.setattr(
        sys,
        "argv",
        [
            "run_translate",
            "--db",
            str(db_path),
            "--chapters",
            "ti_ch02",
            "--configs",
            "S0",
        ],
    )

    with pytest.raises(SystemExit, match="requires --workdb"):
        run_translate.main()


def test_workdb_cannot_equal_frozen_db(tmp_path):
    conn, _ = _make_doc_db(tmp_path)
    conn.close()
    db_path = (tmp_path / "memory.sqlite3").resolve()

    with pytest.raises(SystemExit, match="equal to --db"):
        run_translate._prepare_workdb(db_path, str(db_path))


def test_readonly_db_connection_rejects_writes(tmp_path):
    conn, _ = _make_doc_db(tmp_path)
    conn.close()
    db_path = tmp_path / "memory.sqlite3"

    ro = run_translate._open_db(str(db_path), read_only=True)
    try:
        with pytest.raises(sqlite3.OperationalError):
            ro.execute("CREATE TABLE readonly_probe (id TEXT)")
    finally:
        ro.close()


def test_non_preflight_writes_only_workdb_and_keeps_source_hash(tmp_path, monkeypatch):
    conn, _ = _make_doc_db(tmp_path)
    conn.close()
    source_db = tmp_path / "memory.sqlite3"
    workdb = tmp_path.parent / f"{tmp_path.name}_work" / "memory.sqlite3"
    report_path = tmp_path / "run_report.json"
    source_hash_before = run_translate._file_sha256(source_db)

    class _NoopClient:
        def __init__(self, *args, **kwargs):
            pass

    def _fake_translate_windows(db, windows, client, *, experiment_id, config, **kwargs):
        db.execute("CREATE TABLE IF NOT EXISTS n1_workdb_probe (config TEXT)")
        db.execute("INSERT INTO n1_workdb_probe VALUES (?)", (config,))
        db.commit()
        return TranslateReport(
            experiment_id=experiment_id,
            config=config,
            windows_total=len(windows),
            windows_translated=len(windows),
            windows_failed=0,
            windows_skipped=0,
            blocks_translated=sum(len(window.block_ids) for window in windows),
            blocks_failed=0,
            json_fail_rate=0.0,
            total_usage={
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "reasoning_tokens": 0,
                "cost_usd": 0.0,
                "incremental_cost_usd": 0.0,
                "calls": 0,
                "cache_hits": 0,
            },
            context_stats={
                "windows_with_context": 0,
                "windows_low_context": 0,
                "dropped_by_budget": 0,
            },
            hygiene={"reask_count": 0, "still_bad": 0},
            model="test",
            seed=0,
            system_fingerprint=None,
            reports=[],
        )

    monkeypatch.setattr(run_translate, "_ensure_api_key", lambda: None)
    monkeypatch.setattr(run_translate, "LLMClient", _NoopClient)
    monkeypatch.setattr(run_translate, "translate_windows", _fake_translate_windows)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "run_translate",
            "--db",
            str(source_db),
            "--workdb",
            str(workdb),
            "--chapters",
            "ti_ch02",
            "--configs",
            "S0",
            "--report",
            str(report_path),
        ],
    )

    assert run_translate.main() == 0
    assert run_translate._file_sha256(source_db) == source_hash_before
    assert workdb.exists()
    assert run_translate._file_sha256(workdb) != source_hash_before
    work = sqlite3.connect(workdb)
    try:
        assert work.execute("SELECT COUNT(*) FROM n1_workdb_probe").fetchone()[0] == 1
    finally:
        work.close()
    payload = json.loads(report_path.read_text(encoding="utf-8"))
    assert payload["run"]["frozen_db_sha256_before"] == source_hash_before
    assert payload["run"]["frozen_db_sha256_after"] == source_hash_before


def test_shared_factories_exact_cover_s0_s1_without_legacy_credentials(
    tmp_path, monkeypatch
) -> None:
    conn, _ = _make_doc_db(tmp_path)
    conn.close()
    source_db = tmp_path / "memory.sqlite3"
    workdb = tmp_path.parent / f"{tmp_path.name}_shared_work" / "memory.sqlite3"
    seen: list[tuple[str, str]] = []

    class _Factory:
        uses_shared_backend = True

        def __init__(self, label: str) -> None:
            self.label = label

        def __call__(self, config, cache_path):
            seen.append((self.label, str(cache_path)))
            return type("SharedClient", (), {"label": self.label})()

    def _fake_translate_windows(
        db, windows, client, *, experiment_id, config, **kwargs
    ):
        assert client.label == config
        return TranslateReport(
            experiment_id=experiment_id,
            config=config,
            windows_total=len(windows),
            windows_translated=len(windows),
            windows_failed=0,
            windows_skipped=0,
            blocks_translated=sum(len(window.block_ids) for window in windows),
            blocks_failed=0,
            json_fail_rate=0.0,
            total_usage={
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "reasoning_tokens": 0,
                "cost_usd": None,
                "incremental_cost_usd": None,
                "cost_status": "unknown",
                "calls": 0,
                "cache_hits": 0,
            },
            context_stats={
                "windows_with_context": 0,
                "windows_low_context": 0,
                "dropped_by_budget": 0,
            },
            hygiene={"reask_count": 0, "still_bad": 0},
            model="test",
            seed=0,
            system_fingerprint="shared:seal",
            reports=[],
            transport_identity=f"identity-{config}",
        )

    monkeypatch.setattr(
        run_translate,
        "_ensure_api_key",
        lambda: pytest.fail("shared path must not load a legacy credential"),
    )
    monkeypatch.setattr(run_translate, "translate_windows", _fake_translate_windows)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "run_translate",
            "--db",
            str(source_db),
            "--workdb",
            str(workdb),
            "--chapters",
            "ti_ch02",
            "--configs",
            "S0",
            "S1",
        ],
    )

    assert (
        run_translate.main(
            shared_client_factories={
                "S0": _Factory("S0"),
                "S1": _Factory("S1"),
            }
        )
        == 0
    )
    assert [label for label, _ in seen] == ["S0", "S1"]
    assert run_translate._format_cost(None) == "unknown"


def test_shared_factory_selection_rejects_unmarked_or_partial_maps() -> None:
    with pytest.raises(RuntimeError, match="unmarked"):
        run_translate._shared_factories_for_configs(
            ["S0"], {"S0": lambda config, cache_path: object()}
        )

    class _Factory:
        uses_shared_backend = True

        def __call__(self, config, cache_path):
            return object()

    with pytest.raises(RuntimeError, match="exact-cover"):
        run_translate._shared_factories_for_configs(
            ["S0", "S1"], {"S0": _Factory()}
        )


def test_translation_slot_cli_policy_requires_technical_s1_and_protection() -> None:
    run_translate._validate_translation_policy_args(
        ["S1"],
        profile_name="technical_d2l_v1",
        protected_spans_policy=D2L_PROTECTED_SPANS_POLICY_ID,
        translation_output_policy=D2L_TRANSLATION_SLOTS_POLICY_ID,
    )

    run_translate._validate_translation_policy_args(
        ["S1"],
        profile_name="technical_d2l_v1",
        protected_spans_policy=D2L_LATEX_PROTECTED_SPANS_POLICY_ID,
        translation_output_policy=D2L_TRANSLATION_SLOTS_POLICY_ID,
    )

    with pytest.raises(SystemExit, match="requires a protected-span policy"):
        run_translate._validate_translation_policy_args(
            ["S1"],
            profile_name="technical_d2l_v1",
            protected_spans_policy=None,
            translation_output_policy=D2L_TRANSLATION_SLOTS_POLICY_ID,
        )
    with pytest.raises(SystemExit, match="require technical_d2l_v1 with S1"):
        run_translate._validate_translation_policy_args(
            ["S0"],
            profile_name="literary_v1",
            protected_spans_policy=D2L_PROTECTED_SPANS_POLICY_ID,
            translation_output_policy=None,
        )
    with pytest.raises(SystemExit, match="Opaque LaTeX protection requires"):
        run_translate._validate_translation_policy_args(
            ["S1"],
            profile_name="technical_d2l_v1",
            protected_spans_policy=D2L_LATEX_PROTECTED_SPANS_POLICY_ID,
            translation_output_policy=None,
        )


def test_purge_runtime_state_clears_run_tables_keeps_static(tmp_path):
    from pipeline.scripts.run_translate import _purge_runtime_state

    db_path = tmp_path / "work.sqlite3"
    conn = sqlite3.connect(db_path)
    conn.execute("CREATE TABLE blocks (block_id TEXT PRIMARY KEY, text TEXT)")
    conn.execute("CREATE TABLE translation_runs (run_id TEXT, experiment_id TEXT)")
    conn.execute("CREATE TABLE memory_packs (pack_id TEXT)")
    conn.execute("INSERT INTO blocks VALUES ('b1', 'source text')")
    conn.execute("INSERT INTO translation_runs VALUES ('r1', 'old_exp')")
    conn.execute("INSERT INTO memory_packs VALUES ('pk_old')")
    conn.commit()
    conn.close()

    # evaluation_runs/qa_issues absent on purpose: purge must skip missing tables.
    _purge_runtime_state(db_path)

    conn = sqlite3.connect(db_path)
    assert conn.execute("SELECT COUNT(*) FROM translation_runs").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM memory_packs").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM blocks").fetchone()[0] == 1
    conn.close()
