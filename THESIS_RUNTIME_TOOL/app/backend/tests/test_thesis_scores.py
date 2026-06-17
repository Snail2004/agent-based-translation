"""Tests for thesis_scores (APP-D01) — ScoreReadModel + Drift view + report export.

Acceptance criteria:
  - headline has provenance (metric_version, experiment_id, scope, report_path)
  - drift returns forms_used/status FROM report (no recompute)
  - GUARD: scores endpoint SEPARATE from datasets + observability
  - read_only=True
"""
from __future__ import annotations

import importlib
import json
import sys
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))


# ── Fixtures ──

D2L_REPORT = {
    "scored_at": "2026-06-14T12:47:05.501150+00:00",
    "metric_version": "d2l_translate_score_v2",
    "experiment_id": "d2l_p3",
    "profile": "technical_d2l_v1",
    "doc_id": "d2l",
    "chapters": ["d2l_introduction", "d2l_preliminaries"],
    "scope": {
        "scope_blocks": 100,
        "passthrough_blocks": 50,
        "translation_counts": {"S0": 100, "S1": 100},
        "scope_equals_translation_runs": {"S0": True, "S1": True},
    },
    "B_tar_vs_gold": {
        "S0": {
            "flat": {
                "overall": 0.7519,
                "pairs": 2330,
                "adherent_pairs": 1752,
                "occurrence_weighted": 0.7639,
                "per_chapter": {"d2l_introduction": 0.752, "d2l_preliminaries": 0.691},
                "worst_terms": [{"source_term": "example", "misses": 117, "pairs": 131}],
            },
            "recurring": {
                "overall": 0.7520,
                "pairs": 2294,
                "occurrence_weighted": 0.7640,
            },
        },
        "S1": {
            "flat": {
                "overall": 0.8172,
                "pairs": 2330,
                "adherent_pairs": 1904,
                "occurrence_weighted": 0.8320,
                "per_chapter": {"d2l_introduction": 0.847, "d2l_preliminaries": 0.790},
                "worst_terms": [{"source_term": "layer", "misses": 64, "pairs": 69}],
            },
            "recurring": {
                "overall": 0.8195,
                "pairs": 2294,
                "occurrence_weighted": 0.8339,
            },
        },
    },
    "D_registry_consistency": {
        "S0": {
            "method": "block_surface_v2",
            "alignment": False,
            "headline_ready": False,
            "overall": 0.5930,
            "detected_only": 0.6163,
            "terms": 715,
            "consistent_terms": 424,
            "drift_terms": 264,
            "undetected_terms": 27,
            "terms_all": [
                {
                    "source_term": "AI",
                    "target_term": "trÃ­ tuá»‡ nhÃ¢n táº¡o",
                    "source_blocks": 80,
                    "status": "drift",
                    "forms_used": {"trÃ­ tuá»‡ nhÃ¢n táº¡o": 2, "AI": 80},
                },
                {
                    "source_term": "model",
                    "target_term": "mÃ´ hÃ¬nh",
                    "source_blocks": 10,
                    "status": "consistent",
                    "forms_used": {"mÃ´ hÃ¬nh": 10},
                },
                {
                    "source_term": "agent",
                    "target_term": "tÃ¡c nhÃ¢n",
                    "source_blocks": 6,
                    "status": "undetected",
                    "forms_used": {},
                },
            ],
            "worst_terms": [
                {
                    "source_term": "AI",
                    "target_term": "trí tuệ nhân tạo",
                    "source_blocks": 80,
                    "status": "drift",
                    "forms_used": {"trí tuệ nhân tạo": 2, "AI": 80},
                },
                {
                    "source_term": "agent",
                    "target_term": "tác nhân",
                    "source_blocks": 6,
                    "status": "undetected",
                    "forms_used": {},
                },
            ],
        },
        "S1": {
            "method": "block_surface_v2",
            "alignment": False,
            "headline_ready": False,
            "overall": 0.7007,
            "detected_only": 0.7017,
            "terms": 715,
            "consistent_terms": 501,
            "drift_terms": 213,
            "undetected_terms": 1,
            "terms_all": [
                {
                    "source_term": "AI",
                    "target_term": "trÃ­ tuá»‡ nhÃ¢n táº¡o",
                    "source_blocks": 80,
                    "status": "drift",
                    "forms_used": {"trÃ­ tuá»‡ nhÃ¢n táº¡o": 10, "AI": 71},
                },
                {
                    "source_term": "model",
                    "target_term": "mÃ´ hÃ¬nh",
                    "source_blocks": 10,
                    "status": "consistent",
                    "forms_used": {"mÃ´ hÃ¬nh": 10},
                },
            ],
            "worst_terms": [
                {
                    "source_term": "AI",
                    "target_term": "trí tuệ nhân tạo",
                    "source_blocks": 80,
                    "status": "drift",
                    "forms_used": {"trí tuệ nhân tạo": 10, "AI": 71},
                },
            ],
        },
    },
    "A_tar_vs_registry": {
        "S1": {
            "overall": 0.9343,
            "pairs": 8594,
            "adherent_pairs": 8029,
            "occurrence_weighted": 0.9436,
        },
    },
    "injection": {"registry": {"raw_registry": 1608}},
    "stage_gate": {"scope_equals_translation_runs": {"S0": True, "S1": True}},
    "limitations": ["D_surface_v2 note"],
}

TI_S0_REPORT = {
    "scored_at": "2026-06-12T18:17:40.050728+00:00",
    "ruler": {
        "registry": "frozen_p2",
        "provider": "span_resolver",
        "metric_version": "consistency_v1",
    },
    "s0": {
        "project": "thesis_exp_pilot_p3_S0",
        "scored_at": "2026-06-12T18:17:40.050728+00:00",
        "source": "thesis_exp_pilot_p3_S0:frozen_p2_registry",
        "metric_version": "consistency_v1",
        "tar": {
            "overall": 0.4151,
            "pairs": 53,
            "occurrence_weighted": 0.386,
            "per_chapter": {"ch02": 0.382, "ch03": 0.474},
            "worst_terms": [{"term": "cutlass", "rate": 0.0, "pairs": 3}],
        },
        "fvr": {"overall": 0.0, "violations": []},
        "ecs": {
            "overall": 0.7556,
            "entities_scored": 10,
            "entities_skipped": 0,
            "per_entity": [
                {
                    "entity": "Billy Bones",
                    "entity_id": "ent_captain",
                    "coverage": 0.8,
                    "name_mention_blocks": 35,
                    "forms_used": {"thuyền trưởng": 28, "Billy Bones": 2},
                },
            ],
            "lowest_coverage": [],
        },
        "inspection": {
            "lowest_tar_blocks": ["treasure_island_ch02_b012"],
            "lowest_ecs_entities": ["Squire Trelawney"],
        },
    },
    "oracle_same_ruler": {
        "project": "oracle_preview_same_ruler",
        "scored_at": "2026-06-12T18:17:40.086380+00:00",
        "source": "oracle_preview",
        "metric_version": "consistency_v1",
        "tar": {"overall": 0.6226, "pairs": 53},
        "fvr": {"overall": 0.0, "violations": []},
        "ecs": {"overall": 0.7667, "entities_scored": 10},
    },
}

TI_S1_REPORT = {
    "scored_at": "2026-06-12T19:24:45.261703+00:00",
    "ruler": {
        "registry": "frozen_p2",
        "provider": "span_resolver",
        "metric_version": "consistency_v1",
    },
    "s1": {
        "project": "thesis_exp_pilot_p3_S1",
        "scored_at": "2026-06-12T19:24:45.261703+00:00",
        "source": "thesis_exp_pilot_p3_S1:frozen_p2_registry",
        "metric_version": "consistency_v1",
        "tar": {
            "overall": 1.0,
            "pairs": 53,
            "occurrence_weighted": 1.0,
            "per_chapter": {"ch02": 1.0, "ch03": 1.0},
            "worst_terms": [],
        },
        "fvr": {"overall": 0.0, "violations": []},
        "ecs": {
            "overall": 0.8111,
            "entities_scored": 10,
            "entities_skipped": 0,
            "per_entity": [
                {
                    "entity": "Doctor Livesey",
                    "entity_id": "ent_doctor_livesey",
                    "coverage": 0.353,
                    "name_mention_blocks": 17,
                    "forms_used": {"bác sĩ Livesey": 7},
                },
            ],
        },
        "inspection": {
            "lowest_tar_blocks": ["treasure_island_ch02_b004"],
            "lowest_ecs_entities": ["Doctor Livesey"],
        },
    },
    "oracle_same_ruler": {
        "project": "oracle_preview_same_ruler",
        "scored_at": "2026-06-12T19:24:45.301030+00:00",
        "source": "oracle_preview",
        "metric_version": "consistency_v1",
        "tar": {"overall": 0.6226, "pairs": 53},
        "fvr": {"overall": 0.0, "violations": []},
        "ecs": {"overall": 0.7667, "entities_scored": 10},
    },
}

TI_ORACLE_REPORT = {
    "project": "treasure_island",
    "scored_at": "2026-06-11T20:40:33.273448+00:00",
    "source": "oracle_gpt55_preview",
    "metric_version": "consistency_v1",
    "tar": {
        "overall": 0.8866,
        "pairs": 811,
        "occurrence_weighted": 0.8882,
        "per_chapter": {"ch01": 1.0, "ch02": 0.9286},
    },
    "fvr": {"overall": 0.0, "violations": []},
    "ecs": {
        "overall": 0.9195,
        "entities_scored": 58,
        "entities_skipped": 0,
        "per_entity": [],
    },
    "inspection": {
        "lowest_tar_blocks": ["treasure_island_ch37_b004"],
        "lowest_ecs_entities": ["Jim Hawkins"],
    },
}


def _create_d2l_fixture(reports_root: Path) -> None:
    reports_root.mkdir(parents=True, exist_ok=True)
    with open(reports_root / "d2l_translation_metrics_v2.json", "w", encoding="utf-8") as fh:
        json.dump(D2L_REPORT, fh)


def _create_ti_fixture(reports_root: Path) -> None:
    reports_root.mkdir(parents=True, exist_ok=True)
    with open(reports_root / "s0_pilot_consistency.json", "w", encoding="utf-8") as fh:
        json.dump(TI_S0_REPORT, fh)
    with open(reports_root / "s1_pilot_consistency.json", "w", encoding="utf-8") as fh:
        json.dump(TI_S1_REPORT, fh)
    with open(reports_root / "oracle_consistency.json", "w", encoding="utf-8") as fh:
        json.dump(TI_ORACLE_REPORT, fh)


# ═══════════════════ D2L tests ═══════════════════


def test_d2l_headline_has_provenance(tmp_path):
    from services.thesis_scores import load_scores

    _create_d2l_fixture(tmp_path)
    data = load_scores("d2l_p1", reports_root=tmp_path)

    assert data["meta"]["read_only"] is True
    assert data["meta"]["source"] == "thesis_score_readmodel"
    assert data["meta"]["domain"] == "d2l"
    assert len(data["meta"]["report_paths"]) >= 1

    headlines = data["headline"]
    assert len(headlines) >= 4  # B_S0, B_S1, D_S0, D_S1, A_S1

    for headline in headlines:
        prov = headline["provenance"]
        assert "metric_version" in prov
        assert "experiment_id" in prov
        assert "scope" in prov or "report_path" in prov or "report_paths" in prov
        assert "scored_at" in prov

    # B headline uses occurrence_weighted
    b_s0 = next(h for h in headlines if h["name"] == "B_tar_vs_gold_S0")
    assert b_s0["value"] == 0.7639  # occurrence_weighted
    assert b_s0["domain"] == "d2l"
    assert b_s0["provenance"]["metric_version"] == "d2l_translate_score_v2"
    assert b_s0["provenance"]["experiment_id"] == "d2l_p3"

    b_s1 = next(h for h in headlines if h["name"] == "B_tar_vs_gold_S1")
    assert b_s1["value"] == 0.8320

    # D headline
    d_s1 = next(h for h in headlines if h["name"] == "D_registry_consistency_S1")
    assert d_s1["value"] == 0.7007
    assert d_s1["drift_terms"] == 213
    assert d_s1["metric_label"] == "D_surface_v2 (hard-tier)"
    assert d_s1["method"] == "block_surface_v2"
    assert d_s1["alignment"] is False
    assert d_s1["headline_ready"] is False


def test_d2l_drift_returns_forms_used_from_report(tmp_path):
    """Drift items come FROM report — no recompute."""
    from services.thesis_scores import load_scores

    _create_d2l_fixture(tmp_path)
    data = load_scores("d2l_p1", reports_root=tmp_path)

    drift = data["drift"]
    assert len(drift) >= 5  # terms_all includes consistent terms, not just worst_terms

    # Check the canonical AI drift item from S1
    ai_s1 = next(
        d for d in drift
        if d["source_term"] == "AI" and d["config"] == "S1"
    )
    assert ai_s1["status"] == "drift"
    assert ai_s1["forms_used"].get("AI") == 71
    assert 10 in ai_s1["forms_used"].values()
    assert ai_s1["target_term"]
    assert ai_s1["drift_category"] == "glossary-term"
    assert ai_s1["metric_label"] == "D_surface_v2 (hard-tier)"
    assert ai_s1["alignment"] is False

    model_s1 = next(
        d for d in drift
        if d["source_term"] == "model" and d["config"] == "S1"
    )
    assert model_s1["status"] == "consistent"
    assert model_s1["forms_used"] == {"mÃ´ hÃ¬nh": 10}

    # Check undetected
    agent_s0 = next(
        d for d in drift
        if d["source_term"] == "agent" and d["config"] == "S0"
    )
    assert agent_s0["status"] == "undetected"
    assert agent_s0["forms_used"] == {}


def test_d2l_no_recompute_guard(tmp_path):
    """Verify UI render numbers FROM report — not self-computed."""
    from services.thesis_scores import load_scores

    _create_d2l_fixture(tmp_path)
    data = load_scores("d2l_p1", reports_root=tmp_path)

    # All values come directly from fixture JSON — if they match,
    # no recompute happened (adapter just reads).
    assert data["headline"][0]["value"] == D2L_REPORT["B_tar_vs_gold"]["S0"]["flat"]["occurrence_weighted"]

    # Stage gate comes from report
    assert data["stage_gate"] == D2L_REPORT["stage_gate"]

    # Limitations pass through
    assert data["limitations"] == D2L_REPORT["limitations"]


# ═══════════════════ TI tests ═══════════════════


def test_ti_headline_tar_fvr_ecs_plus_oracle(tmp_path):
    from services.thesis_scores import load_scores

    _create_ti_fixture(tmp_path)
    data = load_scores("treasure_island_p2", reports_root=tmp_path)

    assert data["meta"]["read_only"] is True
    assert data["meta"]["domain"] == "ti"
    assert len(data["meta"]["report_paths"]) == 3

    headlines = data["headline"]
    names = {h["name"] for h in headlines}

    # Required: TAR/FVR/ECS for S0, S1, oracle
    assert "TAR_S0" in names
    assert "FVR_S0" in names
    assert "ECS_S0" in names
    assert "TAR_S1" in names
    assert "TAR_oracle" in names
    assert "ECS_oracle" in names

    tar_s0 = next(h for h in headlines if h["name"] == "TAR_S0")
    assert tar_s0["value"] == 0.4151
    assert tar_s0["provenance"]["metric_version"] == "consistency_v1"

    tar_s1 = next(h for h in headlines if h["name"] == "TAR_S1")
    assert tar_s1["value"] == 1.0

    tar_oracle = next(h for h in headlines if h["name"] == "TAR_oracle")
    assert tar_oracle["value"] == 0.8866

    ecs_s0 = next(h for h in headlines if h["name"] == "ECS_S0")
    assert ecs_s0["value"] == 0.7556


def test_ti_drift_entity_coverage(tmp_path):
    from services.thesis_scores import load_scores

    _create_ti_fixture(tmp_path)
    data = load_scores("treasure_island_p2", reports_root=tmp_path)

    drift = data["drift"]
    assert len(drift) >= 1  # Billy Bones coverage < 1.0 in S0

    billy = next(
        (d for d in drift if d["source_term"] == "Billy Bones"),
        None,
    )
    assert billy is not None
    assert billy["drift_category"] == "entity-name"
    assert billy["forms_used"]["thuyền trưởng"] == 28
    assert billy["coverage"] < 1.0


def test_ti_oracle_compare(tmp_path):
    from services.thesis_scores import load_scores

    _create_ti_fixture(tmp_path)
    data = load_scores("treasure_island_p2", reports_root=tmp_path)

    oracle_compare = data["oracle_compare"]
    assert oracle_compare["oracle_standalone"] is not None
    assert oracle_compare["oracle_standalone"]["tar"]["overall"] == 0.8866
    assert oracle_compare["oracle_same_ruler_s0"]["tar"]["overall"] == 0.6226
    assert oracle_compare["oracle_same_ruler_s1"]["tar"]["overall"] == 0.6226


# ═══════════════════ Separation guard ═══════════════════


def test_scores_endpoint_separate_from_dataset_and_observability(tmp_path, monkeypatch):
    """GUARD: scores ⊥ dataset ⊥ observability."""
    from tests.test_thesis_observability import create_observability_fixture

    # observability fixture already creates documents, blocks, translation_runs,
    # memory_packs — which is enough for both dataset and observability endpoints.
    create_observability_fixture(tmp_path)
    _create_d2l_fixture(tmp_path / "reports")

    monkeypatch.setenv("THESIS_JOBS_ROOT", str(tmp_path))
    monkeypatch.setenv("THESIS_REPORTS_ROOT", str(tmp_path / "reports"))
    monkeypatch.setenv("THESIS_TOOL_PROJECTS_ROOT", str(tmp_path / "projects"))
    monkeypatch.setenv("THESIS_APP_MODE", "cockpit")

    for name in list(sys.modules):
        if (
            name == "app"
            or name == "config"
            or name == "routes"
            or name.startswith("routes.")
            or name.startswith("services.thesis_")
        ):
            sys.modules.pop(name, None)

    app_module = importlib.import_module("app")
    client = app_module.create_app().test_client()

    # Scores endpoint
    scores_response = client.get("/api/thesis/scores/d2l_p1")
    assert scores_response.status_code == 200
    scores = scores_response.get_json()["data"]
    assert scores["meta"]["source"] == "thesis_score_readmodel"
    assert "headline" in scores
    assert "drift" in scores
    # Scores does NOT have dataset fields
    assert "blocks" not in scores
    assert "runtime_memory" not in scores
    # Scores does NOT have observability fields
    assert "calls" not in scores
    assert "usage_daily" not in scores

    # Dataset endpoint — has blocks, no drift
    dataset_response = client.get("/api/thesis/datasets/fixture_job")
    assert dataset_response.status_code == 200
    dataset = dataset_response.get_json()["data"]
    assert "blocks" in dataset
    assert "headline" not in dataset
    assert "drift" not in dataset

    # Observability endpoint — has calls, no headline
    observability_response = client.get("/api/thesis/observability/fixture_job")
    assert observability_response.status_code == 200
    observability = observability_response.get_json()["data"]
    assert "calls" in observability
    assert "headline" not in observability
    assert "drift" not in observability


# ═══════════════════ Export ═══════════════════


def test_export_report_bundle(tmp_path):
    from services.thesis_scores import export_report_bundle

    _create_d2l_fixture(tmp_path)
    bundle = export_report_bundle("d2l_p1", reports_root=tmp_path)

    assert bundle["read_only"] is True
    assert bundle["domain"] == "d2l"
    assert len(bundle["headline"]) >= 4
    assert bundle["drift_summary"]["total"] >= 3
    assert "drift" in bundle["drift_summary"]["by_status"]


# ═══════════════════ Error handling ═══════════════════


def test_invalid_job_returns_404(tmp_path):
    from services.thesis_readmodel import ThesisReadModelError
    from services.thesis_scores import load_scores

    try:
        load_scores("nonexistent_job_xyz", reports_root=tmp_path)
        assert False, "Expected ThesisReadModelError"
    except ThesisReadModelError as exc:
        assert exc.status == 404
        assert exc.code == "job_not_found"


def test_read_only_flag(tmp_path):
    from services.thesis_scores import load_scores

    _create_d2l_fixture(tmp_path)
    data = load_scores("d2l_p1", reports_root=tmp_path)
    assert data["meta"]["read_only"] is True

    _create_ti_fixture(tmp_path)
    data = load_scores("treasure_island_p2", reports_root=tmp_path)
    assert data["meta"]["read_only"] is True
