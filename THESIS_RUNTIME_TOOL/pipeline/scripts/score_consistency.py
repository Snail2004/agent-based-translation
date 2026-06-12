from __future__ import annotations

import argparse
import json
from pathlib import Path

from pipeline.eval.consistency import score_consistency
from pipeline.eval.loaders import load_oracle_project


def main() -> int:
    parser = argparse.ArgumentParser(description="Score TAR/FVR/ECS consistency metrics.")
    parser.add_argument("--project", required=True, help="Path to AI-LAB project root.")
    parser.add_argument("--out", required=True, help="Report JSON output path.")
    args = parser.parse_args()

    loaded = load_oracle_project(args.project)
    report = score_consistency(
        project=loaded.project,
        terms=loaded.terms,
        entities=loaded.entities,
        term_occurrences_by_block=loaded.term_occurrences_by_block,
        entity_mentions_by_block=loaded.entity_mentions_by_block,
        translations_by_block=loaded.translations_by_block,
        block_chapters=loaded.block_chapters,
    )
    _validate_report(report)

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    _print_summary(report)
    return 0


def _validate_report(report: dict) -> None:
    if report["tar"]["pairs"] <= 0:
        raise SystemExit("No TAR pairs were scored")
    for label, value in [
        ("TAR", report["tar"]["overall"]),
        ("FVR", report["fvr"]["overall"]),
        ("ECS", report["ecs"]["overall"]),
    ]:
        if not 0.0 <= value <= 1.0:
            raise SystemExit(f"{label} out of range: {value}")


def _print_summary(report: dict) -> None:
    print(f"Project: {report['project']}")
    print(f"TAR overall: {report['tar']['overall']:.4f} ({report['tar']['pairs']} pairs)")
    print(f"FVR overall: {report['fvr']['overall']:.4f}")
    print(f"ECS overall: {report['ecs']['overall']:.4f}")
    print("Top-5 worst terms:")
    for item in report["tar"]["worst_terms"][:5]:
        print(f"  {item['term']}: {item['rate']:.4f} ({item['pairs']} pairs)")
    print("Top-5 lowest ECS entities:")
    for item in report["ecs"]["lowest_coverage"][:5]:
        print(
            f"  {item['entity']}: {item['coverage']:.4f} "
            f"({item['name_mention_blocks']} blocks)"
        )


if __name__ == "__main__":
    raise SystemExit(main())
