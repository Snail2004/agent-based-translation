from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path


BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))
TOOL_ROOT = Path(__file__).resolve().parents[3]
if str(TOOL_ROOT) not in sys.path:
    sys.path.insert(0, str(TOOL_ROOT))

from test_thesis_readmodel import create_fixture_db


def test_overlay_diagnostic_status_for_ignored_drift_and_vi_segmentation(tmp_path):
    from services.thesis_overlay import load_registry_overlay

    create_fixture_db(tmp_path, job_id="d2l_p1")
    db_path = tmp_path / "d2l_p1" / "memory.sqlite3"
    with sqlite3.connect(db_path) as con:
        con.execute(
            "INSERT INTO blocks VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                "b003",
                "doc_fixture",
                3,
                None,
                "prose",
                None,
                "ch01",
                None,
                "A set of techniques.",
                "A set of techniques.",
                None,
                None,
                None,
                None,
                None,
                "2026-06-15",
                "2026-06-15",
            ),
        )
        con.execute(
            "INSERT INTO glossary_entries VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                "g-set",
                "doc_fixture",
                "set",
                "tập hợp",
                "technical",
                "document",
                None,
                None,
                0,
                0,
                json.dumps(["tập hợp", "tập"], ensure_ascii=False),
                "[]",
                "[]",
                "[]",
                0.9,
                "candidate",
                2,
                "b003",
                "[]",
                "2026-06-15",
                "2026-06-15",
            ),
        )
        con.execute(
            "INSERT INTO translation_runs VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                "run-s1-b003",
                "exp_fixture",
                "doc_fixture",
                "b003",
                "S1",
                "translate",
                None,
                None,
                "Chúng ta tập trung vào một tập hợp kỹ thuật.",
                "gpt-test",
                "p1",
                0.3,
                1,
                "fp",
                0.0,
                10,
                "2026-06-15",
                "w1",
            ),
        )
    reports_root = tmp_path / "reports"
    reports_root.mkdir()
    with (reports_root / "d2l_translation_metrics_v2.json").open("w", encoding="utf-8") as fh:
        json.dump(
            {
                "metric_version": "d2l_translate_score_v2_2",
                "experiment_id": "d2l_p1",
                "D_registry_consistency": {
                    "S1": {
                        "method": "block_surface_v2_2",
                        "terms_all": [
                            {
                                "source_term": "set",
                                "target_term": "tập hợp",
                                "status": "drift",
                                "forms_used": {"tập": 1, "tập hợp": 1},
                                "constraint_strength": "ignore_for_consistency",
                            },
                        ],
                    }
                },
            },
            fh,
            ensure_ascii=False,
        )

    overlay = load_registry_overlay(
        "d2l_p1",
        block_id="b003",
        jobs_root=tmp_path,
        reports_root=reports_root,
    )

    target = overlay["target_by_config"]["S1"]["glossary_by_id"]["g-set"]["occurrences"]
    assert [span["surface"] for span in target] == ["tập hợp"]
    assert target[0]["status"] == "drift"
    assert target[0]["display_status"] == "diagnostic"
