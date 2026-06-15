"""ScoreReadModel adapter — read-only.

Reads scorer report JSON files from data/reports/ and surfaces
headlines, drift, per_chapter, inspection, and provenance
WITHOUT recomputing any metric.

APP-D01 | LOCK (nn).6 Drift hạng-nhất, (nn).7 Metric traceable
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from config import THESIS_REPORTS_ROOT
from services.thesis_readmodel import ThesisReadModelError


JOB_ID_RE = re.compile(r"^[A-Za-z0-9_.-]+$")

# Map job_id → domain + report file(s) inside reports_root
_JOB_REPORT_MAP: dict[str, dict[str, Any]] = {
    "d2l_p1": {
        "domain": "d2l",
        "files": ["d2l_translation_metrics.json"],
    },
    "d2l_p3": {
        "domain": "d2l",
        "files": ["d2l_translation_metrics.json"],
    },
    "treasure_island_p2": {
        "domain": "ti",
        "files": [
            "s0_pilot_consistency.json",
            "s1_pilot_consistency.json",
            "oracle_consistency.json",
        ],
    },
    "treasure_island_p3": {
        "domain": "ti",
        "files": [
            "s0_pilot_consistency.json",
            "s1_pilot_consistency.json",
            "oracle_consistency.json",
        ],
    },
}


def _safe_job_id(job_id: str) -> str:
    value = (job_id or "").strip()
    if not value or not JOB_ID_RE.match(value):
        raise ThesisReadModelError("invalid_job_id", "Invalid thesis job id.", 400)
    return value


def _read_json(path: Path) -> dict[str, Any]:
    """Read a JSON report file.  Returns {} if file does not exist."""
    if not path.exists():
        return {}
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


# ═══════════════════ D2L domain ═══════════════════


def _d2l_headlines(report: dict[str, Any], report_path: str) -> list[dict[str, Any]]:
    """Extract B / D / A headlines from d2l_translation_metrics.json.

    Uses *occurrence_weighted* B as the canonical headline value
    (consistent with LOCK: "occ-weighted headline B + D").
    """
    headlines: list[dict[str, Any]] = []
    provenance_base = {
        "metric_version": report.get("metric_version"),
        "experiment_id": report.get("experiment_id"),
        "doc_id": report.get("doc_id"),
        "chapters": report.get("chapters"),
        "scope": "translation_runs",
        "scored_at": report.get("scored_at"),
        "report_path": report_path,
    }

    # B (TAR vs gold) — occurrence-weighted
    b = report.get("B_tar_vs_gold") or {}
    for config in ("S0", "S1"):
        cfg = b.get(config) or {}
        flat = cfg.get("flat") or {}
        recurring = cfg.get("recurring") or {}
        if flat or recurring:
            headlines.append({
                "name": f"B_tar_vs_gold_{config}",
                "value": flat.get("occurrence_weighted"),
                "value_flat": flat.get("overall"),
                "value_recurring_occ": recurring.get("occurrence_weighted"),
                "pairs": flat.get("pairs"),
                "domain": "d2l",
                "provenance": {**provenance_base, "scorer_metric": "B_tar_vs_gold"},
            })

    # D (registry consistency)
    d = report.get("D_registry_consistency") or {}
    for config in ("S0", "S1"):
        cfg = d.get(config) or {}
        if cfg:
            headlines.append({
                "name": f"D_registry_consistency_{config}",
                "value": cfg.get("overall"),
                "detected_only": cfg.get("detected_only"),
                "terms": cfg.get("terms"),
                "consistent_terms": cfg.get("consistent_terms"),
                "drift_terms": cfg.get("drift_terms"),
                "undetected_terms": cfg.get("undetected_terms"),
                "domain": "d2l",
                "provenance": {**provenance_base, "scorer_metric": "D_registry_consistency"},
            })

    # A (TAR vs registry) — diagnostic
    a = report.get("A_tar_vs_registry") or {}
    for config in ("S1",):
        cfg = a.get(config) or {}
        if cfg:
            headlines.append({
                "name": f"A_tar_vs_registry_{config}",
                "value": cfg.get("overall"),
                "occurrence_weighted": cfg.get("occurrence_weighted"),
                "pairs": cfg.get("pairs"),
                "domain": "d2l",
                "provenance": {**provenance_base, "scorer_metric": "A_tar_vs_registry"},
            })

    return headlines


def _d2l_drift(report: dict[str, Any]) -> list[dict[str, Any]]:
    """Extract drift items from D_registry_consistency worst_terms."""
    drift: list[dict[str, Any]] = []
    d = report.get("D_registry_consistency") or {}
    for config in ("S0", "S1"):
        cfg = d.get(config) or {}
        for term in cfg.get("worst_terms") or []:
            drift.append({
                "config": config,
                "source_term": term.get("source_term"),
                "target_term": term.get("target_term"),
                "status": term.get("status"),
                "forms_used": term.get("forms_used") or {},
                "source_blocks": term.get("source_blocks"),
                "drift_category": "glossary-term",
            })
    return drift


def _d2l_per_chapter(report: dict[str, Any]) -> dict[str, Any]:
    """Extract per-chapter from B."""
    result: dict[str, Any] = {}
    b = report.get("B_tar_vs_gold") or {}
    for config in ("S0", "S1"):
        flat = (b.get(config) or {}).get("flat") or {}
        result[f"B_{config}"] = flat.get("per_chapter") or {}
    return result


def _d2l_scores(report: dict[str, Any], report_path: str, requested_job_id: str) -> dict[str, Any]:
    meta: dict[str, Any] = {
        "source": "thesis_score_readmodel",
        "job_id": report.get("experiment_id"),
        "domain": "d2l",
        "report_paths": [report_path],
        "read_only": True,
    }
    # Hardening (C01 review §6 note 1): warn when requested job ≠ report identity
    report_identity = report.get("experiment_id") or report.get("project")
    if report_identity and requested_job_id != report_identity:
        meta["scope_warning"] = (
            f"Requested job_id '{requested_job_id}' differs from report "
            f"experiment_id/project '{report_identity}'. "
            "Verify the correct report is mapped."
        )
    return {
        "meta": meta,
        "headline": _d2l_headlines(report, report_path),
        "drift": _d2l_drift(report),
        "per_chapter": _d2l_per_chapter(report),
        "injection": report.get("injection"),
        "stage_gate": report.get("stage_gate"),
        "scope": report.get("scope"),
        "samples": report.get("samples"),
        "limitations": report.get("limitations"),
        "known_gap": [
            "scorer-command / run_id-list not stored in report; trace via experiment_id + config",
            "judge metric link: see B01 observability call detail for judge calls",
        ],
    }


# ═══════════════════ TI (Treasure Island) domain ═══════════════════


def _ti_metric_from_report(
    report: dict[str, Any],
    config_key: str,
) -> dict[str, Any] | None:
    """Extract tar/fvr/ecs from a TI consistency report section."""
    section = report.get(config_key) or report  # oracle has flat structure
    if not section:
        return None
    return {
        "tar": section.get("tar"),
        "fvr": section.get("fvr"),
        "ecs": section.get("ecs"),
        "inspection": section.get("inspection"),
        "project": section.get("project"),
        "scored_at": section.get("scored_at") or report.get("scored_at"),
        "source": section.get("source"),
        "metric_version": section.get("metric_version")
            or (report.get("ruler") or {}).get("metric_version"),
    }


def _ti_headlines(
    s0: dict[str, Any] | None,
    s1: dict[str, Any] | None,
    oracle: dict[str, Any] | None,
    report_paths: list[str],
) -> list[dict[str, Any]]:
    """Build headlines for TI: TAR/FVR/ECS + oracle compare."""
    headlines: list[dict[str, Any]] = []
    provenance_base = {
        "scope": "translation_runs",
        "report_paths": report_paths,
    }

    for label, metrics in [("S0", s0), ("S1", s1)]:
        if not metrics:
            continue
        tar = metrics.get("tar") or {}
        fvr = metrics.get("fvr") or {}
        ecs = metrics.get("ecs") or {}
        prov = {
            **provenance_base,
            "metric_version": metrics.get("metric_version"),
            "project": metrics.get("project"),
            "scored_at": metrics.get("scored_at"),
        }
        headlines.append({
            "name": f"TAR_{label}",
            "value": tar.get("overall"),
            "occurrence_weighted": tar.get("occurrence_weighted"),
            "pairs": tar.get("pairs"),
            "domain": "ti",
            "provenance": {**prov, "scorer_metric": "TAR"},
        })
        headlines.append({
            "name": f"FVR_{label}",
            "value": fvr.get("overall"),
            "domain": "ti",
            "provenance": {**prov, "scorer_metric": "FVR"},
        })
        headlines.append({
            "name": f"ECS_{label}",
            "value": ecs.get("overall"),
            "entities_scored": ecs.get("entities_scored"),
            "domain": "ti",
            "provenance": {**prov, "scorer_metric": "ECS"},
        })

    if oracle:
        tar = oracle.get("tar") or {}
        fvr = oracle.get("fvr") or {}
        ecs = oracle.get("ecs") or {}
        prov = {
            **provenance_base,
            "metric_version": oracle.get("metric_version"),
            "project": oracle.get("project"),
            "scored_at": oracle.get("scored_at"),
            "scorer_metric": "oracle_same_ruler",
        }
        headlines.append({
            "name": "TAR_oracle",
            "value": tar.get("overall"),
            "occurrence_weighted": tar.get("occurrence_weighted"),
            "pairs": tar.get("pairs"),
            "domain": "ti",
            "provenance": prov,
        })
        headlines.append({
            "name": "FVR_oracle",
            "value": fvr.get("overall"),
            "domain": "ti",
            "provenance": prov,
        })
        headlines.append({
            "name": "ECS_oracle",
            "value": ecs.get("overall"),
            "entities_scored": ecs.get("entities_scored"),
            "domain": "ti",
            "provenance": prov,
        })

    return headlines


def _ti_drift(
    s0: dict[str, Any] | None,
    s1: dict[str, Any] | None,
) -> list[dict[str, Any]]:
    """Build drift items from TI ECS lowest_ecs_entities (entity-name / xưng-hô)."""
    drift: list[dict[str, Any]] = []
    for label, metrics in [("S0", s0), ("S1", s1)]:
        if not metrics:
            continue
        ecs = metrics.get("ecs") or {}
        for entity in ecs.get("per_entity") or []:
            if entity.get("coverage", 1.0) < 1.0:
                drift.append({
                    "config": label,
                    "source_term": entity.get("entity"),
                    "target_term": entity.get("entity_id"),
                    # Hardening (C01 review §6 note 2): label derived fields
                    "target_term_kind": "entity_id",
                    "status": "low_coverage" if entity.get("coverage", 0) > 0 else "undetected",
                    "status_source": "derived_from_coverage",
                    "forms_used": entity.get("forms_used") or {},
                    "source_blocks": entity.get("name_mention_blocks"),
                    "coverage": entity.get("coverage"),
                    "drift_category": "entity-name",
                })
    return drift


def _ti_scores(
    s0_report: dict[str, Any],
    s1_report: dict[str, Any],
    oracle_report: dict[str, Any],
    report_paths: list[str],
    job_id: str,
) -> dict[str, Any]:
    s0 = _ti_metric_from_report(s0_report, "s0") if s0_report else None
    s1 = _ti_metric_from_report(s1_report, "s1") if s1_report else None

    # oracle: try keyed first, then flat structure (top-level tar/fvr/ecs)
    oracle = _ti_metric_from_report(oracle_report, "oracle") if oracle_report else None
    if oracle_report and (not oracle or not oracle.get("tar")):
        oracle = _ti_metric_from_report(oracle_report, "__flat_sentinel__")

    # oracle_same_ruler sections from S0/S1 reports
    oracle_s0 = s0_report.get("oracle_same_ruler") if s0_report else None
    oracle_s1 = s1_report.get("oracle_same_ruler") if s1_report else None

    headlines = _ti_headlines(s0, s1, oracle, report_paths)

    # Hardening (C01 review §6 note 1): scope_warning when job_id ≠ report project
    meta: dict[str, Any] = {
        "source": "thesis_score_readmodel",
        "job_id": job_id,
        "domain": "ti",
        "report_paths": [p for p in report_paths if p],
        "read_only": True,
    }
    report_projects = set()
    for metrics in (s0, s1):
        if metrics and metrics.get("project"):
            report_projects.add(metrics["project"])
    if report_projects and job_id not in report_projects:
        meta["scope_warning"] = (
            f"Requested job_id '{job_id}' differs from report "
            f"project(s) {report_projects}. "
            "Verify the correct reports are mapped."
        )

    return {
        "meta": meta,
        "headline": headlines,
        "drift": _ti_drift(s0, s1),
        "per_chapter": {
            label: (metrics.get("tar") or {}).get("per_chapter") or {}
            for label, metrics in [("S0", s0), ("S1", s1)]
            if metrics
        },
        "inspection": {
            label: metrics.get("inspection") or {}
            for label, metrics in [("S0", s0), ("S1", s1)]
            if metrics
        },
        "oracle_compare": {
            "oracle_standalone": oracle,
            "oracle_same_ruler_s0": oracle_s0,
            "oracle_same_ruler_s1": oracle_s1,
        },
        "known_gap": [
            "scorer-command not stored in report; trace via project + scored_at",
            "run_id-list not persisted in TI consistency report",
            "judge metric link: see B01 observability call detail",
        ],
    }


# ═══════════════════ Report export (tối giản) ═══════════════════


def export_report_bundle(
    job_id: str,
    *,
    reports_root: Path | None = None,
    jobs_root: Path | None = None,
) -> dict[str, Any]:
    """Bundle metrics + provenance for committee report (JSON).

    Low-priority — minimal implementation per spec §2.
    """
    scores = load_scores(job_id, reports_root=reports_root, jobs_root=jobs_root)
    return {
        "job_id": job_id,
        "domain": scores["meta"]["domain"],
        "report_paths": scores["meta"]["report_paths"],
        "headline": scores["headline"],
        "drift_summary": {
            "total": len(scores.get("drift") or []),
            "by_status": _group_by(scores.get("drift") or [], "status"),
        },
        "known_gap": scores.get("known_gap") or [],
        "read_only": True,
        "export_note": "Bundle for committee; translations available via A01 dataset endpoint.",
    }


def _group_by(items: list[dict[str, Any]], key: str) -> dict[str, int]:
    result: dict[str, int] = {}
    for item in items:
        val = item.get(key) or "unknown"
        result[val] = result.get(val, 0) + 1
    return result


# ═══════════════════ Public API ═══════════════════


def load_scores(
    job_id: str,
    *,
    reports_root: Path | None = None,
    jobs_root: Path | None = None,
) -> dict[str, Any]:
    """Load score report for a given job.  Read-only — NEVER recomputes.

    Parameters
    ----------
    job_id : str
        Logical job identifier (e.g. ``d2l_p1``, ``treasure_island_p2``).
    reports_root : Path | None
        Override the reports directory (for tests).  Defaults to
        ``THESIS_RUNTIME_TOOL/data/reports/``.
    jobs_root : Path | None
        Unused — accepted for API consistency with A01/B01 adapters.
    """
    safe_job = _safe_job_id(job_id)
    root = (reports_root or THESIS_REPORTS_ROOT).resolve()

    spec = _JOB_REPORT_MAP.get(safe_job)
    if spec is None:
        raise ThesisReadModelError(
            "job_not_found",
            f"No report mapping for job {safe_job}.",
            404,
        )

    domain = spec["domain"]
    report_paths: list[str] = []
    reports: list[dict[str, Any]] = []
    for filename in spec["files"]:
        path = root / filename
        report_paths.append(str(path))
        reports.append(_read_json(path))

    if domain == "d2l":
        report = reports[0] if reports else {}
        if not report:
            raise ThesisReadModelError(
                "report_not_found",
                f"D2L report file not found for job {safe_job}.",
                404,
            )
        return _d2l_scores(report, report_paths[0], safe_job)

    # domain == "ti"
    s0_report = reports[0] if len(reports) > 0 else {}
    s1_report = reports[1] if len(reports) > 1 else {}
    oracle_report = reports[2] if len(reports) > 2 else {}
    if not s0_report and not s1_report:
        raise ThesisReadModelError(
            "report_not_found",
            f"TI report files not found for job {safe_job}.",
            404,
        )
    return _ti_scores(s0_report, s1_report, oracle_report, report_paths, safe_job)
