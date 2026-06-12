from __future__ import annotations

import argparse
import json
from pathlib import Path

from pipeline.eval.thesis_scoring import (
    build_ruler_from_db_and_spans,
    score_oracle_on_same_ruler,
    score_thesis_translations,
)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Score S0 (or any config) translations vs oracle on the same ruler."
    )
    parser.add_argument("--db", required=True, help="Frozen memory SQLite DB.")
    parser.add_argument("--experiment", required=True, help="Experiment ID.")
    parser.add_argument("--config", default="S0", help="Config to score (default: S0).")
    parser.add_argument("--prepass", required=True, help="Prepass artifacts directory.")
    parser.add_argument("--source", required=True, help="Source document.json path.")
    parser.add_argument(
        "--oracle",
        required=True,
        help="Path to AILAB oracle project (eval-only, not used in runtime).",
    )
    parser.add_argument("--out", required=True, help="Output report JSON path.")
    args = parser.parse_args()

    # Build the common ruler from frozen DB + span_resolver (thesis's registry)
    ruler = build_ruler_from_db_and_spans(
        db_path=args.db,
        prepass_dir=args.prepass,
        source_document_path=args.source,
    )

    # Score thesis translations
    thesis_report = score_thesis_translations(
        db_path=args.db,
        experiment_id=args.experiment,
        config=args.config,
        prepass_dir=args.prepass,
        source_document_path=args.source,
        ruler_note="frozen_p2_registry + span_resolver",
        ruler=ruler,
    )

    # Score oracle on the SAME ruler
    oracle_report = score_oracle_on_same_ruler(
        oracle_project_path=args.oracle,
        ruler_block_chapters=ruler["block_chapters"],
        ruler_terms=ruler["terms"],
        ruler_entities=ruler["entities"],
        ruler_term_occurrences=ruler["term_occurrences_by_block"],
        ruler_entity_mentions=ruler["entity_mentions_by_block"],
    )

    # Build combined report
    report = {
        "scored_at": thesis_report.get("scored_at", ""),
        "ruler": {
            "registry": "frozen_p2",
            "provider": "span_resolver+apostrophe_safe_adapter",
            "metric_version": thesis_report.get("metric_version", ""),
        },
        "s0": thesis_report,
        "oracle_same_ruler": oracle_report,
        "note": (
            "FVR is always 0 because thesis registry has no forbidden_variants "
            "(this is not a bug — registry thesis does not populate forbidden list)."
        ),
    }

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    _print_summary("S0", thesis_report)
    _print_summary("Oracle (same ruler)", oracle_report)
    print(f"\nReport written: {out_path}")
    return 0


def _print_summary(label: str, report: dict) -> None:
    tar = report.get("tar", {})
    fvr = report.get("fvr", {})
    ecs = report.get("ecs", {})
    print(f"\n=== {label} ===")
    print(f"  TAR overall:    {tar.get('overall', 0.0):.4f}  ({tar.get('pairs', 0)} pairs)")
    print(f"  FVR overall:   {fvr.get('overall', 0.0):.4f}")
    print(f"  ECS overall:   {ecs.get('overall', 0.0):.4f}")
    per_ch = tar.get("per_chapter", {})
    for ch, val in sorted(per_ch.items()):
        print(f"    {ch}: {val:.4f}")


if __name__ == "__main__":
    raise SystemExit(main())
