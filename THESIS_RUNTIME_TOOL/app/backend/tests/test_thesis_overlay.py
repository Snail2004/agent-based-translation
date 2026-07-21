from __future__ import annotations

import importlib
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


def test_registry_overlay_masks_urls_and_inline_markup():
    from pipeline.eval.surface_match import MASK_PATTERNS
    from services.thesis_overlay import _find_matches

    assert any("https" in item.pattern for item in MASK_PATTERNS)

    text = "Visit https://discuss.d2l.ai/ for AI help and see :numref:`sec_ai`."

    matches = _find_matches(text, "AI")

    assert [surface for _, _, surface in matches] == ["AI"]


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


def test_registry_overlay_adds_display_only_cascade_marks(tmp_path):
    from services.thesis_overlay import load_registry_overlay

    create_fixture_db(tmp_path, job_id="d2l_p1")
    reports_root = tmp_path / "reports"
    _write_d2l_report(reports_root)
    cascade_report = tmp_path / "cascade.json"
    cascade_report.write_text(
        json.dumps(
            {
                "decisions": [
                    {
                        "occ_id": "S0:b001:registry:agent:0",
                        "config": "S0",
                        "block_id": "b001",
                        "source_term": "agent",
                        "resolved_by": "t2_credit",
                        "decision": "rendered",
                        "target_start": 6,
                        "target_end": 15,
                        "target_surface": "xuất hiện",
                    }
                ]
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    overlay = load_registry_overlay(
        "d2l_p1",
        jobs_root=tmp_path,
        reports_root=reports_root,
        cascade_report=cascade_report,
    )

    spans = overlay["target_by_config"]["S0"]["glossary_by_id"]["g-runtime"]["occurrences"]
    cascade_spans = [span for span in spans if span.get("mark_source") == "cascade_t2"]
    assert cascade_spans
    assert cascade_spans[0]["span"] == [6, 15]
    assert cascade_spans[0]["surface"] == "xuất hiện"
    assert cascade_spans[0]["provenance"] == "cascade_report"
    assert cascade_spans[0]["located_by"] == "code_exact"
    assert overlay["meta"]["cascade_status"].startswith("loaded:1")
    assert overlay["meta"]["cascade_audit"]["by_mark_source"] == {"cascade_t2": 1}


def test_localization_overlay_uses_only_persisted_localization_pairs(tmp_path):
    from services.thesis_overlay import load_registry_overlay

    create_fixture_db(tmp_path, job_id="d2l_p1")
    reports_root = tmp_path / "reports"
    reports_root.mkdir()
    cascade_report = tmp_path / "cascade.json"
    cascade_report.write_text(
        json.dumps(
            {
                "decisions": [
                    {
                        "occ_id": "S0:b001:g-runtime:0",
                        "config": "S0",
                        "block_id": "b001",
                        "source_term": "agent",
                        "term_id": "g-runtime",
                        "source_start": 0,
                        "source_end": 5,
                        "source_surface": "Agent",
                        "resolved_by": "t2_credit",
                        "decision": "rendered",
                        "target_start": 6,
                        "target_end": 15,
                    }
                ]
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    overlay = load_registry_overlay(
        "d2l_p1",
        experiment_id="exp_fixture",
        jobs_root=tmp_path,
        reports_root=reports_root,
        cascade_report=cascade_report,
        overlay_mode="localization",
    )

    assert overlay["meta"]["overlay_mode"] == "localization"
    assert overlay["meta"]["source"] == "localization_artifact"
    assert "score_status" not in overlay["meta"]
    assert overlay["meta"]["localization_status"] == "loaded:1:skipped:0"

    source = overlay["source"]["glossary_by_id"]["g-runtime"]["occurrences"]
    assert source == [
        {
            "id": "g-runtime",
            "block_id": "b001",
            "span": [0, 5],
            "surface": "Agent",
            "source_term": "agent",
            "status": "localized",
            "display_status": "localized",
            "localization_status": "rendered",
            "provenance": "localization_artifact",
            "mark_source": "localization_source",
            "located_by": "code_exact",
            "configs": ["S0"],
            "occ_ids": ["S0:b001:g-runtime:0"],
            "accepted_forms": [],
            "mismatch_configs": [],
            "reference_status": "match",
        }
    ]

    target = overlay["target_by_config"]["S0"]["glossary_by_id"]["g-runtime"]["occurrences"]
    assert len(target) == 1
    assert target[0]["status"] == "localized"
    assert target[0]["display_status"] == "localized"
    assert target[0]["localization_status"] == "rendered"
    assert target[0]["mark_source"] == "cascade_t2"


def test_localization_overlay_fails_closed_without_run_artifact(tmp_path):
    from services.thesis_overlay import load_registry_overlay

    create_fixture_db(tmp_path, job_id="d2l_p1")
    reports_root = tmp_path / "reports"
    reports_root.mkdir()

    overlay = load_registry_overlay(
        "d2l_p1",
        experiment_id="exp_fixture",
        jobs_root=tmp_path,
        reports_root=reports_root,
        overlay_mode="localization",
    )

    assert overlay["meta"]["localization_status"] == "unavailable:not_registered"
    assert overlay["source"] == {"glossary_by_id": {}, "entities_by_id": {}}
    assert overlay["target_by_config"] == {
        "S0": {"glossary_by_id": {}, "entities_by_id": {}},
        "S1": {"glossary_by_id": {}, "entities_by_id": {}},
    }


def test_localization_block_scope_resolves_the_chapter_artifact(tmp_path):
    from services.thesis_overlay import load_registry_overlay

    create_fixture_db(tmp_path, job_id="d2l_p1")
    reports_root = tmp_path / "reports"
    exp_root = reports_root / "exp_fixture"
    exp_root.mkdir(parents=True)
    (exp_root / "manifest.json").write_text(
        json.dumps(
            {
                "schema_version": "thesis_report_manifest_v1",
                "experiment_id": "exp_fixture",
                "job_id": "d2l_p1",
                "domain": "d2l",
                "chapters": {
                    "ch01": {
                        "reports": {
                            "cascade": "cascade_ch01.json",
                        }
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    (exp_root / "cascade_ch01.json").write_text(
        json.dumps(
            {
                "decisions": [
                    {
                        "occ_id": "S0:b001:g-runtime:0",
                        "config": "S0",
                        "block_id": "b001",
                        "source_term": "agent",
                        "term_id": "g-runtime",
                        "source_start": 0,
                        "source_end": 5,
                        "source_surface": "Agent",
                        "resolved_by": "t2_credit",
                        "decision": "rendered",
                        "target_start": 6,
                        "target_end": 15,
                    }
                ]
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    overlay = load_registry_overlay(
        "d2l_p1",
        experiment_id="exp_fixture",
        block_id="b001",
        jobs_root=tmp_path,
        reports_root=reports_root,
        overlay_mode="localization",
    )

    assert overlay["meta"]["selected"]["chapter_id"] == "ch01"
    assert overlay["meta"]["localization_status"] == "loaded:1:skipped:0"
    source = overlay["source"]["glossary_by_id"]["g-runtime"]["occurrences"]
    assert [(row["block_id"], row["surface"]) for row in source] == [("b001", "Agent")]


def test_localization_overlay_marks_only_the_off_reference_occurrence_as_mismatch(tmp_path):
    from services.thesis_overlay import load_registry_overlay

    create_fixture_db(tmp_path, job_id="d2l_p1")
    with sqlite3.connect(tmp_path / "d2l_p1" / "memory.sqlite3") as con:
        con.execute(
            "UPDATE blocks SET text=?, original_text=? WHERE block_id=?",
            ("Example and example.", "Example and example.", "b001"),
        )
        con.execute(
            "UPDATE translation_runs SET output_text=? WHERE config=? AND block_id=?",
            ("mẫu và ví dụ", "S0", "b001"),
        )
        con.execute(
            "UPDATE translation_runs SET output_text=? WHERE config=? AND block_id=?",
            ("sample and sample", "S1", "b001"),
        )
    cascade_report = tmp_path / "cascade-reference-status.json"
    cascade_report.write_text(
        json.dumps(
            {
                "decisions": [
                    {
                        "occ_id": "S0:b001:g-runtime:0",
                        "config": "S0",
                        "block_id": "b001",
                        "source_term": "example",
                        "term_id": "g-runtime",
                        "source_start": 0,
                        "source_end": 7,
                        "source_surface": "Example",
                        "resolved_by": "t3_llm",
                        "decision": "localized",
                        "target_start": 0,
                        "target_end": 3,
                        "target_quote": "mẫu",
                        "accepted_forms": ["mẫu"],
                        "t3_code_score": {
                            "adherence_label": "adherent",
                            "accepted_form": "mẫu",
                        },
                    },
                    {
                        "occ_id": "S0:b001:g-runtime:1",
                        "config": "S0",
                        "block_id": "b001",
                        "source_term": "example",
                        "term_id": "g-runtime",
                        "source_start": 12,
                        "source_end": 19,
                        "source_surface": "example",
                        "resolved_by": "t3_llm",
                        "decision": "localized",
                        "target_start": 7,
                        "target_end": 12,
                        "target_quote": "ví dụ",
                        "accepted_forms": ["mẫu"],
                        "t3_code_score": {
                            "adherence_label": "off_glossary",
                            "accepted_form": "",
                        },
                    },
                    {
                        "occ_id": "S1:b001:g-runtime:1",
                        "config": "S1",
                        "block_id": "b001",
                        "source_term": "example",
                        "term_id": "g-runtime",
                        "source_start": 12,
                        "source_end": 19,
                        "source_surface": "example",
                        "resolved_by": "t2_code",
                        "decision": "rendered",
                        "target_start": 11,
                        "target_end": 17,
                        "target_quote": "sample",
                        "accepted_forms": ["sample"],
                        "t3_code_score": {
                            "adherence_label": "adherent",
                            "accepted_form": "sample",
                        },
                    },
                ]
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    overlay = load_registry_overlay(
        "d2l_p1",
        experiment_id="exp_fixture",
        jobs_root=tmp_path,
        reports_root=tmp_path / "reports",
        cascade_report=cascade_report,
        overlay_mode="localization",
    )

    target = overlay["target_by_config"]["S0"]["glossary_by_id"]["g-runtime"]["occurrences"]
    assert [(row["surface"], row["status"], row["reference_status"]) for row in target] == [
        ("mẫu", "localized", "match"),
        ("ví dụ", "localization_mismatch", "mismatch"),
    ]
    assert all(row["accepted_forms"] == ["mẫu"] for row in target)
    target_s1 = overlay["target_by_config"]["S1"]["glossary_by_id"]["g-runtime"]["occurrences"]
    assert [(row["surface"], row["status"], row["reference_status"]) for row in target_s1] == [
        ("sample", "localized", "match"),
    ]
    source = overlay["source"]["glossary_by_id"]["g-runtime"]["occurrences"]
    assert [row["status"] for row in source] == ["localized", "localization_source_warning"]
    assert source[0]["mismatch_configs"] == []
    assert source[1]["configs"] == ["S0", "S1"]
    assert source[1]["mismatch_configs"] == ["S0"]
    assert source[1]["reference_status"] == "mismatch_any"
    assert overlay["meta"]["localization_audit"]["by_reference_status"] == {
        "match": 2,
        "mismatch": 1,
    }


def test_localization_source_pairing_recovers_unique_surface_from_noncanonical_offsets(tmp_path):
    from services.thesis_overlay import load_registry_overlay

    create_fixture_db(tmp_path, job_id="d2l_p1")
    cascade_report = tmp_path / "cascade-unique-source.json"
    cascade_report.write_text(
        json.dumps(
            {
                "decisions": [
                    {
                        "occ_id": "opaque-occurrence",
                        "config": "S0",
                        "block_id": "b001",
                        "source_term": "agent",
                        "term_id": "g-runtime",
                        "source_start": 100,
                        "source_end": 105,
                        "source_surface": "Agent",
                        "resolved_by": "t2_credit",
                        "decision": "rendered",
                        "target_start": 6,
                        "target_end": 15,
                    }
                ]
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    overlay = load_registry_overlay(
        "d2l_p1",
        experiment_id="exp_fixture",
        jobs_root=tmp_path,
        reports_root=tmp_path / "reports",
        cascade_report=cascade_report,
        overlay_mode="localization",
    )

    source = overlay["source"]["glossary_by_id"]["g-runtime"]["occurrences"]
    assert [(row["span"], row["surface"]) for row in source] == [([0, 5], "Agent")]


def test_localization_source_pairing_does_not_guess_ambiguous_surface(tmp_path):
    from services.thesis_overlay import load_registry_overlay

    create_fixture_db(tmp_path, job_id="d2l_p1")
    with sqlite3.connect(tmp_path / "d2l_p1" / "memory.sqlite3") as con:
        con.execute(
            "UPDATE blocks SET text=?, original_text=? WHERE block_id=?",
            ("Agent and Agent appear.", "Agent and Agent appear.", "b001"),
        )
    cascade_report = tmp_path / "cascade-ambiguous-source.json"
    cascade_report.write_text(
        json.dumps(
            {
                "decisions": [
                    {
                        "occ_id": "opaque-occurrence",
                        "config": "S0",
                        "block_id": "b001",
                        "source_term": "agent",
                        "term_id": "g-runtime",
                        "source_start": 100,
                        "source_end": 105,
                        "source_surface": "Agent",
                        "resolved_by": "t2_credit",
                        "decision": "rendered",
                        "target_start": 6,
                        "target_end": 15,
                    }
                ]
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    overlay = load_registry_overlay(
        "d2l_p1",
        experiment_id="exp_fixture",
        jobs_root=tmp_path,
        reports_root=tmp_path / "reports",
        cascade_report=cascade_report,
        overlay_mode="localization",
    )

    assert overlay["source"] == {"glossary_by_id": {}, "entities_by_id": {}}
    target = overlay["target_by_config"]["S0"]["glossary_by_id"]["g-runtime"]["occurrences"]
    assert len(target) == 1


def test_registry_overlay_ignores_summary_sample_marks_and_counts_skip_reasons(tmp_path):
    from services.thesis_overlay import load_registry_overlay

    create_fixture_db(tmp_path, job_id="d2l_p1")
    reports_root = tmp_path / "reports"
    _write_d2l_report(reports_root)
    cascade_report = tmp_path / "cascade-summary.json"
    cascade_report.write_text(
        json.dumps(
            {
                "reports": {
                    "S0": {
                        "decisions": [
                            {
                                "occ_id": "S0:b001:registry:agent:0",
                                "config": "S0",
                                "block_id": "b001",
                                "source_term": "agent",
                                "resolved_by": "t2_credit",
                                "decision": "rendered",
                                "target_start": 6,
                                "target_end": 15,
                            },
                            {
                                "occ_id": "S0:b001:registry:agent:1",
                                "config": "S0",
                                "block_id": "b001",
                                "source_term": "agent",
                                "decision": "not_rendered",
                            },
                        ]
                    }
                },
                "t3_run_stats": {
                    "S0": {
                        "sample_marks": [
                            {
                                "occ_id": "S0:b001:registry:agent:sample",
                                "config": "S0",
                                "block_id": "b001",
                                "source_term": "agent",
                                "resolved_by": "t3_llm",
                                "target_start": 0,
                                "target_end": 5,
                            }
                        ]
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    overlay = load_registry_overlay(
        "d2l_p1",
        jobs_root=tmp_path,
        reports_root=reports_root,
        cascade_report=cascade_report,
    )

    audit = overlay["meta"]["cascade_audit"]
    assert overlay["meta"]["cascade_status"] == "loaded:1:skipped:1"
    assert audit["skipped_by_reason"] == {"not_rendered": 1}
    assert audit["by_mark_source"] == {"cascade_t2": 1}
    spans = overlay["target_by_config"]["S0"]["glossary_by_id"]["g-runtime"]["occurrences"]
    assert not [span for span in spans if span.get("occ_id") == "S0:b001:registry:agent:sample"]


def test_registry_overlay_dedupes_overlapping_surface_form_when_cascade_covers_occurrence(tmp_path):
    from services.thesis_overlay import load_registry_overlay

    create_fixture_db(tmp_path, job_id="d2l_p1")
    reports_root = tmp_path / "reports"
    _write_d2l_report(reports_root)
    cascade_report = tmp_path / "cascade.json"
    cascade_report.write_text(
        json.dumps(
            {
                "decisions": [
                    {
                        "occ_id": "S0:b001:registry:agent:0",
                        "config": "S0",
                        "block_id": "b001",
                        "source_term": "agent",
                        "resolved_by": "t2_credit",
                        "decision": "rendered",
                        "target_start": 0,
                        "target_end": 5,
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    overlay = load_registry_overlay(
        "d2l_p1",
        jobs_root=tmp_path,
        reports_root=reports_root,
        cascade_report=cascade_report,
    )

    spans = overlay["target_by_config"]["S0"]["glossary_by_id"]["g-runtime"]["occurrences"]
    assert overlay["meta"]["cascade_audit"]["deduped_surface_form"] == 1
    assert [span.get("mark_source") for span in spans] == ["cascade_t2"]
    assert spans[0]["span"] == [0, 5]


def test_registry_overlay_resolves_cascade_report_from_manifest(tmp_path):
    from services.thesis_overlay import load_registry_overlay

    create_fixture_db(tmp_path, job_id="fixture_job")
    reports_root = tmp_path / "reports"
    reports_root.mkdir(parents=True)
    _write_d2l_report(reports_root)
    exp_root = reports_root / "exp_fixture"
    exp_root.mkdir()
    (exp_root / "metrics.json").write_text(
        json.dumps({"metric_version": "fixture", "experiment_id": "exp_fixture", "D_registry_consistency": {}}),
        encoding="utf-8",
    )
    (exp_root / "manifest.json").write_text(
        json.dumps(
            {
                "schema_version": "thesis_report_manifest_v1",
                "experiment_id": "exp_fixture",
                "job_id": "fixture_job",
                "domain": "d2l",
                "reports": {
                    "score": "metrics.json",
                    "cascade": "cascade.json",
                },
            }
        ),
        encoding="utf-8",
    )
    (exp_root / "cascade.json").write_text(
        json.dumps(
            {
                "decisions": [
                    {
                        "occ_id": "S0:b001:registry:agent:0",
                        "config": "S0",
                        "block_id": "b001",
                        "source_term": "agent",
                        "resolved_by": "t2_credit",
                        "decision": "rendered",
                        "target_start": 6,
                        "target_end": 15,
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    overlay = load_registry_overlay(
        "fixture_job",
        experiment_id="exp_fixture",
        jobs_root=tmp_path,
        reports_root=reports_root,
    )

    assert overlay["meta"]["selected"]["cascade_report"].endswith("cascade.json")
    assert overlay["meta"]["cascade_status"].startswith("loaded:1")


def test_registry_overlay_prefers_materialized_overlay_from_manifest(tmp_path):
    from services.thesis_overlay import load_registry_overlay

    create_fixture_db(tmp_path, job_id="fixture_job")
    reports_root = tmp_path / "reports"
    reports_root.mkdir(parents=True)
    exp_root = reports_root / "exp_fixture"
    exp_root.mkdir()
    (exp_root / "manifest.json").write_text(
        json.dumps(
            {
                "schema_version": "thesis_report_manifest_v1",
                "experiment_id": "exp_fixture",
                "job_id": "fixture_job",
                "domain": "d2l",
                "reports": {
                    "overlay": "overlay.json",
                    "cascade": "missing-cascade.json",
                    "score": "missing-score.json",
                },
            }
        ),
        encoding="utf-8",
    )
    (exp_root / "overlay.json").write_text(
        json.dumps(
            {
                "meta": {
                    "source": "materialized_fixture",
                    "materialized_overlay": {"schema_version": "thesis_materialized_overlay_v1"},
                },
                "source": {"glossary_by_id": {}, "entities_by_id": {}},
                "target_by_config": {
                    "S0": {
                        "glossary_by_id": {
                            "g-runtime": {
                                "occurrences": [
                                    {
                                        "term_id": "g-runtime",
                                        "block_id": "b001",
                                        "config": "S0",
                                        "span": [0, 5],
                                        "surface": "Agent",
                                        "mark_source": "cascade_t2",
                                        "located_by": "code_exact",
                                    }
                                ]
                            }
                        },
                        "entities_by_id": {},
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    overlay = load_registry_overlay(
        "fixture_job",
        experiment_id="exp_fixture",
        jobs_root=tmp_path,
        reports_root=reports_root,
        cascade_report=exp_root / "missing-cascade.json",
    )

    assert overlay["meta"]["source"] == "materialized_fixture"
    assert overlay["meta"]["materialized_loaded_from"].endswith("overlay.json")
    spans = overlay["target_by_config"]["S0"]["glossary_by_id"]["g-runtime"]["occurrences"]
    assert spans[0]["located_by"] == "code_exact"


def test_registry_overlay_prefers_chapter_materialized_overlay_from_manifest(tmp_path):
    from services.thesis_overlay import load_registry_overlay

    create_fixture_db(tmp_path, job_id="fixture_job")
    reports_root = tmp_path / "reports"
    reports_root.mkdir(parents=True)
    exp_root = reports_root / "exp_fixture"
    exp_root.mkdir()
    (exp_root / "manifest.json").write_text(
        json.dumps(
            {
                "schema_version": "thesis_report_manifest_v1",
                "experiment_id": "exp_fixture",
                "job_id": "fixture_job",
                "domain": "d2l",
                "chapter_id": "d2l_multilayer_perceptrons",
                "reports": {
                    "overlay": "overlay_mlp.json",
                },
                "chapters": {
                    "d2l_preliminaries": {
                        "reports": {
                            "overlay": "overlay_preliminaries.json",
                        }
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    (exp_root / "overlay_mlp.json").write_text(
        json.dumps(
            {
                "meta": {"source": "mlp_overlay"},
                "source": {"glossary_by_id": {}, "entities_by_id": {}},
                "target_by_config": {"S0": {"glossary_by_id": {}, "entities_by_id": {}}},
            }
        ),
        encoding="utf-8",
    )
    (exp_root / "overlay_preliminaries.json").write_text(
        json.dumps(
            {
                "meta": {"source": "prelim_overlay"},
                "source": {"glossary_by_id": {}, "entities_by_id": {}},
                "target_by_config": {"S0": {"glossary_by_id": {}, "entities_by_id": {}}},
            }
        ),
        encoding="utf-8",
    )

    overlay = load_registry_overlay(
        "fixture_job",
        experiment_id="exp_fixture",
        chapter_id="d2l_preliminaries",
        jobs_root=tmp_path,
        reports_root=reports_root,
    )

    assert overlay["meta"]["source"] == "prelim_overlay"
    assert overlay["meta"]["materialized_loaded_from"].endswith("overlay_preliminaries.json")


def test_registry_overlay_auto_resolves_experiment_manifest_by_job_id(tmp_path):
    from services.thesis_overlay import load_registry_overlay

    create_fixture_db(tmp_path, job_id="fixture_job")
    reports_root = tmp_path / "reports"
    reports_root.mkdir(parents=True)
    exp_root = reports_root / "exp_fixture"
    exp_root.mkdir()
    (exp_root / "manifest.json").write_text(
        json.dumps(
            {
                "schema_version": "thesis_report_manifest_v1",
                "experiment_id": "exp_fixture",
                "job_id": "fixture_job",
                "domain": "d2l",
                "reports": {
                    "overlay": "overlay.json",
                },
            }
        ),
        encoding="utf-8",
    )
    (exp_root / "overlay.json").write_text(
        json.dumps(
            {
                "meta": {"source": "auto_materialized_fixture"},
                "source": {"glossary_by_id": {}, "entities_by_id": {}},
                "target_by_config": {"S0": {"glossary_by_id": {}, "entities_by_id": {}}},
            }
        ),
        encoding="utf-8",
    )

    overlay = load_registry_overlay(
        "fixture_job",
        jobs_root=tmp_path,
        reports_root=reports_root,
    )

    assert overlay["meta"]["source"] == "auto_materialized_fixture"
    assert overlay["meta"]["materialized_loaded_from"].endswith("overlay.json")


def test_overlay_matches_scorer_forms_with_cross_term_subsumption(tmp_path):
    from services.thesis_overlay import load_registry_overlay

    create_fixture_db(tmp_path, job_id="d2l_p1")
    db_path = tmp_path / "d2l_p1" / "memory.sqlite3"
    with sqlite3.connect(db_path) as con:
        con.execute(
            "INSERT INTO blocks VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                "b002",
                "doc_fixture",
                2,
                None,
                "prose",
                None,
                "ch01",
                None,
                "Machine learning supports learning.",
                "Machine learning supports learning.",
                None,
                None,
                None,
                None,
                None,
                "2026-06-15",
                "2026-06-15",
            ),
        )
        con.executemany(
            "INSERT INTO glossary_entries VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [
                (
                    "g-learning",
                    "doc_fixture",
                    "learning",
                    "học",
                    "technical",
                    "document",
                    None,
                    None,
                    0,
                    0,
                    json.dumps(["học"], ensure_ascii=False),
                    "[]",
                    "[]",
                    "[]",
                    0.9,
                    "candidate",
                    2,
                    "b002",
                    "[]",
                    "2026-06-15",
                    "2026-06-15",
                ),
                (
                    "g-machine-learning",
                    "doc_fixture",
                    "machine learning",
                    "học máy",
                    "technical",
                    "document",
                    None,
                    None,
                    0,
                    0,
                    json.dumps(["học máy"], ensure_ascii=False),
                    "[]",
                    "[]",
                    "[]",
                    0.9,
                    "candidate",
                    2,
                    "b002",
                    "[]",
                    "2026-06-15",
                    "2026-06-15",
                ),
            ],
        )
        con.execute(
            "INSERT INTO translation_runs VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                "run-s1-b002",
                "exp_fixture",
                "doc_fixture",
                "b002",
                "S1",
                "translate",
                None,
                None,
                "Học máy hỗ trợ học.",
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
                                "source_term": "learning",
                                "target_term": "học",
                                "status": "consistent",
                                "forms_used": {"học": 1},
                                "constraint_strength": "hard",
                            },
                            {
                                "source_term": "machine learning",
                                "target_term": "học máy",
                                "status": "consistent",
                                "forms_used": {"học máy": 1},
                                "constraint_strength": "hard",
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
        block_id="b002",
        jobs_root=tmp_path,
        reports_root=reports_root,
    )

    source = overlay["source"]["glossary_by_id"]
    assert [span["surface"] for span in source["g-machine-learning"]["occurrences"]] == ["Machine learning"]
    assert [span["surface"] for span in source["g-learning"]["occurrences"]] == ["learning"]

    target = overlay["target_by_config"]["S1"]["glossary_by_id"]
    assert [span["surface"] for span in target["g-machine-learning"]["occurrences"]] == ["Học máy"]
    assert [span["surface"] for span in target["g-learning"]["occurrences"]] == ["học"]
