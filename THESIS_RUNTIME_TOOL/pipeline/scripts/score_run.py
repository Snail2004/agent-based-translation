from __future__ import annotations

import argparse
import json
from pathlib import Path

from pipeline.eval.thesis_scoring import (
    build_ruler_from_db_and_spans,
    score_oracle_on_same_ruler,
    score_thesis_translations,
)
from pipeline.eval.d2l_translate_score import score_d2l_translation_run


def main() -> int:
    parser = argparse.ArgumentParser(description="Score thesis translation runs.")
    parser.add_argument("--db", required=True, help="Frozen memory SQLite DB.")
    parser.add_argument("--experiment", default="d2l_p3", help="Experiment ID.")
    parser.add_argument("--config", default="S0", help="Config to score (default: S0).")
    parser.add_argument("--prepass", help="Prepass artifacts directory.")
    parser.add_argument("--source", help="Source document.json path.")
    parser.add_argument(
        "--oracle",
        help="Path to AILAB oracle project (eval-only, not used in runtime).",
    )
    parser.add_argument("--chapters", nargs="+", help="D2L chapters to score.")
    parser.add_argument("--profile", default="technical_d2l_v1", help="Document profile.")
    parser.add_argument("--gold-variants", help="Eval-only D2L gold variants CSV.")
    parser.add_argument("--out", required=True, help="Output report JSON path.")
    args = parser.parse_args()

    if args.chapters:
        report = score_d2l_translation_run(
            args.db,
            chapters=args.chapters,
            out_path=args.out,
            experiment_id=args.experiment,
            profile_name=args.profile,
            gold_variants_path=args.gold_variants,
        )
        _print_d2l_summary(report)
        print(f"\nReport written: {args.out}")
        return 0

    if not args.prepass or not args.source or not args.oracle:
        raise SystemExit(
            "Legacy TI scoring requires --prepass, --source, and --oracle. "
            "D2L scoring requires --chapters."
        )

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
        args.config.lower(): thesis_report,
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

    _print_summary(args.config.upper(), thesis_report)
    _print_summary("Oracle (same ruler)", oracle_report)
    print(f"\nReport written: {out_path}")
    return 0


def _print_d2l_summary(report: dict) -> None:
    print("\n=== D2L Translation Metrics ===")
    print(f"Profile: {report.get('profile')}  Chapters: {report.get('chapters')}")
    for config in ["S0", "S1"]:
        b = report["B_tar_vs_gold"][config]["flat"]
        br = report["B_tar_vs_gold"][config]["recurring"]
        d = report["D_registry_consistency"][config]
        print(
            f"{config}: B flat={b['overall']:.4f} ({b['pairs']} pairs), "
            f"B recurring={br['overall']:.4f} ({br['pairs']} pairs), "
            f"D={d['overall']:.4f} ({d['terms']} terms)"
        )
    a = report["A_tar_vs_registry"]["S1"]
    print(f"S1 A registry TAR={a['overall']:.4f} ({a['pairs']} pairs)")
    print(f"Stage gate: {json.dumps(report.get('stage_gate', {}), ensure_ascii=False)}")


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
