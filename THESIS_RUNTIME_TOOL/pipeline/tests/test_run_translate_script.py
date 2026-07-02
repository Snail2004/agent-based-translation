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
