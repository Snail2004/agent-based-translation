from __future__ import annotations

import importlib
import json
import sys
from pathlib import Path


BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from test_thesis_readmodel import create_fixture_db


def _write_d2l_report(reports_root: Path) -> None:
    reports_root.mkdir(parents=True, exist_ok=True)
    with open(reports_root / "d2l_translation_metrics_v2.json", "w", encoding="utf-8") as fh:
        json.dump(
            {
                "metric_version": "fixture",
                "experiment_id": "d2l_p1",
                "doc_id": "doc_fixture",
                "chapters": ["ch01"],
                "D_registry_consistency": {
                    "S0": {
                        "overall": 0.5,
                        "worst_terms": [
                            {
                                "source_term": "agent",
                                "target_term": "agent",
                                "status": "drift",
                                "forms_used": {"Agent": 1},
                                "source_blocks": ["b001"],
                            }
                        ],
                    },
                    "S1": {
                        "overall": 1.0,
                        "worst_terms": [],
                    },
                },
            },
            fh,
        )


def test_registry_overlay_builds_source_and_target_spans_from_runtime_only(tmp_path):
    from services.thesis_overlay import load_registry_overlay

    create_fixture_db(tmp_path, job_id="d2l_p1")
    reports_root = tmp_path / "reports"
    _write_d2l_report(reports_root)

    overlay = load_registry_overlay("d2l_p1", jobs_root=tmp_path, reports_root=reports_root)

    source_term = overlay["source"]["glossary_by_id"]["g-runtime"]["occurrences"][0]
    assert source_term["block_id"] == "b001"
    assert source_term["span"] == [0, 5]
    assert source_term["surface"] == "Agent"

    source_entity = overlay["source"]["entities_by_id"]["e1"]["mentions"][0]
    assert source_entity["surface"] == "Jim"
    assert source_entity["span"] == [10, 13]
    assert overlay["source"]["entities_by_id"]["e1"]["source"] == "mentions"

    target_spans = overlay["target_by_config"]["S0"]["glossary_by_id"]["g-runtime"]["occurrences"]
    assert len(target_spans) == 1
    assert target_spans[0]["surface"] == "Agent"
    assert target_spans[0]["status"] == "drift"
    assert target_spans[0]["forms_used"] == {"Agent": 1}
    assert target_spans[0]["forms_source"] == "score_report.forms_used"
    assert target_spans[0]["scored"] is True

    serialized = json.dumps(overlay, ensure_ascii=False)
    assert "eval_glossary_gold" not in serialized
    assert "reference_eval_only" not in serialized
    assert "gold-1" not in serialized


def test_registry_overlay_route_is_read_only_and_zero_gold(tmp_path, monkeypatch):
    create_fixture_db(tmp_path, job_id="d2l_p1")
    reports_root = tmp_path / "reports"
    _write_d2l_report(reports_root)

    monkeypatch.setenv("THESIS_JOBS_ROOT", str(tmp_path))
    monkeypatch.setenv("THESIS_REPORTS_ROOT", str(reports_root))
    monkeypatch.setenv("THESIS_TOOL_PROJECTS_ROOT", str(tmp_path / "projects"))
    monkeypatch.setenv("THESIS_APP_MODE", "cockpit")

    for name in list(sys.modules):
        if name == "app" or name == "config" or name == "routes" or name.startswith("routes.") or name.startswith("services."):
            sys.modules.pop(name, None)

    app_module = importlib.import_module("app")
    client = app_module.create_app().test_client()
    response = client.get("/api/thesis/overlay/d2l_p1")

    assert response.status_code == 200
    payload = response.get_json()["data"]
    assert payload["meta"]["read_only"] is True
    assert payload["source"]["glossary_by_id"]["g-runtime"]["occurrences"][0]["surface"] == "Agent"
    assert payload["target_by_config"]["S0"]["glossary_by_id"]["g-runtime"]["occurrences"][0]["status"] == "drift"


def test_registry_overlay_scopes_by_block(tmp_path):
    from services.thesis_overlay import load_registry_overlay

    create_fixture_db(tmp_path, job_id="d2l_p1")
    reports_root = tmp_path / "reports"
    _write_d2l_report(reports_root)

    overlay = load_registry_overlay(
        "d2l_p1",
        block_id="b001",
        jobs_root=tmp_path,
        reports_root=reports_root,
    )

    assert overlay["meta"]["selected"]["block_id"] == "b001"
    source_spans = overlay["source"]["glossary_by_id"]["g-runtime"]["occurrences"]
    target_spans = overlay["target_by_config"]["S0"]["glossary_by_id"]["g-runtime"]["occurrences"]
    assert {span["block_id"] for span in source_spans} == {"b001"}
    assert {span["block_id"] for span in target_spans} == {"b001"}
