from __future__ import annotations

import argparse
import copy
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any


TOOL_ROOT = Path(__file__).resolve().parents[2]
BACKEND_ROOT = TOOL_ROOT / "app" / "backend"
if str(TOOL_ROOT) not in sys.path:
    sys.path.insert(0, str(TOOL_ROOT))
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from config import THESIS_REPORTS_ROOT  # noqa: E402
from services.thesis_overlay import load_registry_overlay  # noqa: E402
from services.thesis_scores import resolve_experiment_artifact_path  # noqa: E402


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _cascade_decisions_by_config(cascade_report: Path) -> dict[str, list[dict[str, Any]]]:
    payload = _read_json(cascade_report)
    reports = payload.get("reports") if isinstance(payload, dict) else None
    result: dict[str, list[dict[str, Any]]] = {}
    if isinstance(reports, dict):
        for config, report in reports.items():
            decisions = report.get("decisions") if isinstance(report, dict) else None
            if isinstance(decisions, list):
                result[str(config)] = [item for item in decisions if isinstance(item, dict)]
    decisions = payload.get("decisions") if isinstance(payload, dict) else None
    if isinstance(decisions, list):
        for item in decisions:
            if isinstance(item, dict) and item.get("config"):
                result.setdefault(str(item["config"]), []).append(item)
    return result


def _t3_stats_by_config(cascade_report: Path) -> dict[str, dict[str, Any]]:
    payload = _read_json(cascade_report)
    stats = payload.get("t3_run_stats") if isinstance(payload, dict) else None
    return {str(k): v for k, v in stats.items() if isinstance(v, dict)} if isinstance(stats, dict) else {}


def _target_rows_for_config(overlay: dict[str, Any], config: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    cfg = ((overlay.get("target_by_config") or {}).get(config) or {})
    for bucket_name in ("glossary_by_id", "entities_by_id"):
        for item_id, row in (cfg.get(bucket_name) or {}).items():
            span_key = "mentions" if bucket_name == "entities_by_id" else "occurrences"
            for span in row.get(span_key) or []:
                if not isinstance(span, dict):
                    continue
                span.setdefault("term_id" if bucket_name == "glossary_by_id" else "entity_id", str(item_id))
                if bucket_name == "glossary_by_id":
                    span.setdefault("term_id", str(item_id))
                span.setdefault("located_by", "block_detect")
                rows.append(span)
    return rows


def _stats_for_config(
    overlay: dict[str, Any],
    config: str,
    *,
    decisions: list[dict[str, Any]],
    t3_stats: dict[str, Any],
) -> dict[str, Any]:
    rows = _target_rows_for_config(overlay, config)
    by_mark_source = Counter(str(row.get("mark_source") or "missing") for row in rows)
    by_located_by = Counter(str(row.get("located_by") or "missing") for row in rows)
    flags = Counter()
    for row in rows:
        for flag in ("masquerade_suspect", "clean_text_fallback", "gpt_fallback", "cross_term_overlap"):
            if row.get(flag):
                flags[flag] += 1
    decision_total = len(decisions)
    not_rendered = sum(1 for item in decisions if item.get("decision") == "not_rendered")
    cascade_mark_total = by_mark_source.get("cascade_t2", 0) + by_mark_source.get("cascade_t3_llm", 0)
    return {
        "config": config,
        "total_ui_marks": len(rows),
        "cascade_decisions": decision_total,
        "cascade_rendered_marks": cascade_mark_total,
        "not_rendered": not_rendered,
        "by_mark_source": dict(sorted(by_mark_source.items())),
        "by_located_by": dict(sorted(by_located_by.items())),
        "flags": dict(sorted(flags.items())),
        "gpt_fallback_calls": int(t3_stats.get("gpt_fallback_calls") or 0),
        "gpt_fallback_marks": int(flags.get("gpt_fallback", 0)),
        "note": (
            "gpt_fallback_marks counts rendered spans only; fallback calls that concluded "
            "not_rendered have no span to display."
        ),
    }


def _with_only_config(overlay: dict[str, Any], config: str, stats: dict[str, Any]) -> dict[str, Any]:
    payload = copy.deepcopy(overlay)
    payload["target_by_config"] = {config: copy.deepcopy((overlay.get("target_by_config") or {}).get(config) or {})}
    meta = payload.setdefault("meta", {})
    meta["materialized_overlay"] = {
        "schema_version": "thesis_materialized_overlay_v1",
        "config": config,
        "audit": stats,
    }
    return payload


def _update_manifest(manifest_path: Path, combined_name: str, per_config_names: dict[str, str]) -> None:
    manifest = _read_json(manifest_path)
    reports = manifest.setdefault("reports", {})
    reports["overlay"] = combined_name
    reports["overlay_by_config"] = per_config_names
    _write_json(manifest_path, manifest)


def main() -> int:
    parser = argparse.ArgumentParser(description="Materialize a UI-ready thesis overlay from frozen reports.")
    parser.add_argument("--job-id", default="exp_s0s1_full")
    parser.add_argument("--experiment-id", default="exp_s0s1_builderv2_v1")
    parser.add_argument("--chapter-id", default="d2l_multilayer_perceptrons")
    parser.add_argument("--reports-root", type=Path, default=THESIS_REPORTS_ROOT)
    parser.add_argument("--out-dir", type=Path, default=None)
    parser.add_argument("--combined-name", default="overlay_mlp.json")
    parser.add_argument("--configs", nargs="+", default=["S0", "S1"])
    args = parser.parse_args()

    out_dir = args.out_dir or (args.reports_root / args.experiment_id)
    manifest_path = out_dir / "manifest.json"
    cascade_report = resolve_experiment_artifact_path(args.experiment_id, "cascade", reports_root=args.reports_root)
    if not cascade_report:
        raise SystemExit(f"No cascade report found for experiment {args.experiment_id}.")

    overlay = load_registry_overlay(
        args.job_id,
        experiment_id=args.experiment_id,
        chapter_id=args.chapter_id,
        reports_root=args.reports_root,
        prefer_materialized=False,
    )
    decisions_by_config = _cascade_decisions_by_config(cascade_report)
    t3_stats = _t3_stats_by_config(cascade_report)
    stats_by_config = {
        config: _stats_for_config(
            overlay,
            config,
            decisions=decisions_by_config.get(config, []),
            t3_stats=t3_stats.get(config, {}),
        )
        for config in args.configs
    }
    combined = copy.deepcopy(overlay)
    combined_meta = combined.setdefault("meta", {})
    combined_meta["materialized_overlay"] = {
        "schema_version": "thesis_materialized_overlay_v1",
        "configs": stats_by_config,
        "source_cascade_report": str(cascade_report),
    }

    combined_path = out_dir / args.combined_name
    _write_json(combined_path, combined)
    per_config_names: dict[str, str] = {}
    for config in args.configs:
        name = f"overlay_mlp_{config}.json"
        per_config_names[config] = name
        _write_json(out_dir / name, _with_only_config(overlay, config, stats_by_config[config]))
    _update_manifest(manifest_path, args.combined_name, per_config_names)

    print(json.dumps({
        "combined": str(combined_path),
        "per_config": per_config_names,
        "stats": stats_by_config,
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
