from __future__ import annotations

import json
import sys

import pytest

from pipeline.scripts import run_translate
from pipeline.tests.test_translate_runner import _make_doc_db


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


def test_purge_runtime_state_clears_run_tables_keeps_static(tmp_path):
    import sqlite3

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
