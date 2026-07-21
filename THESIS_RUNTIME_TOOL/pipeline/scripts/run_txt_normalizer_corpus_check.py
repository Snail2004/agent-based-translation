from __future__ import annotations

import argparse
import json
import time
from collections import Counter
from pathlib import Path
from typing import Any

from pipeline.ingest.txt_normalizer import normalize_txt


def _render_markdown(report: dict[str, Any]) -> str:
    lines = [
        "# TXT Normalizer Corpus Check v1",
        "",
        f"Repeat count: {report['repeat']}",
        "",
        "| Source | Deterministic | Units | Content | Front | Back | Review | Blocks | Pandoc coverage | Seconds |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for row in report["rows"]:
        roles = row["unit_roles"]
        lines.append(
            "| {source} | {deterministic} | {unit_count} | {content} | {front} | {back} | "
            "{review} | {blocks} | {coverage:.6f} | {duration:.3f} |".format(
                source=row["source"],
                deterministic=row["deterministic"],
                unit_count=row["unit_count"],
                content=roles.get("content_unit", 0),
                front=roles.get("front_matter", 0),
                back=roles.get("back_matter", 0),
                review=row["review_required_count"],
                blocks=row["block_count"],
                coverage=row["cross_check"].get("native_covered_by_pandoc", 0.0),
                duration=row["duration_seconds"],
            )
        )
    lines.extend(
        [
            "",
            "This is an offline structure, provenance and Pandoc cross-check. It does not call an LLM.",
            "",
        ]
    )
    return "\n".join(lines)


def _doc_id(source: Path) -> str:
    parent = "".join(
        character if character.isalnum() else "_" for character in source.parent.name
    ).strip("_").casefold()
    return f"txt_{parent or 'document'}"


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the TXT normalizer over explicit sources")
    parser.add_argument("--source", action="append", required=True)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--repeat", type=int, default=2)
    args = parser.parse_args()
    if args.repeat < 2:
        raise ValueError("repeat must be at least 2 for a determinism check")

    rows: list[dict[str, Any]] = []
    for source_value in args.source:
        source = Path(source_value).resolve()
        runs = []
        durations = []
        for _ in range(args.repeat):
            started = time.perf_counter()
            runs.append(normalize_txt(source, doc_id=_doc_id(source)))
            durations.append(time.perf_counter() - started)
        first = runs[0]
        manifest = first.structure_manifest
        blocks = [block for chapter in first.document["chapters"] for block in chapter["blocks"]]
        rows.append(
            {
                "source": source.parent.name,
                "source_sha256": manifest["source"]["sha256"],
                "structure_sha256": manifest["structure_sha256"],
                "deterministic": all(
                    run.structure_manifest["structure_sha256"] == manifest["structure_sha256"]
                    and run.document == first.document
                    for run in runs[1:]
                ),
                "unit_count": len(manifest["units"]),
                "unit_roles": dict(sorted(Counter(unit["role"] for unit in manifest["units"]).items())),
                "unit_titles": [unit["title"] for unit in manifest["units"]],
                "translatable_count": len(manifest["translatable_chapter_ids"]),
                "review_required_count": len(manifest["review_required_unit_ids"]),
                "block_count": len(blocks),
                "block_kinds": dict(sorted(Counter(block["block_type"] for block in blocks).items())),
                "exact_cover": manifest["exact_cover"]["coverage"],
                "warnings": manifest["warnings"],
                "cross_check": manifest["cross_check"],
                "duration_seconds": round(sum(durations) / len(durations), 3),
            }
        )

    report = {
        "schema_version": "txt_normalizer_corpus_check_v1",
        "repeat": args.repeat,
        "rows": rows,
    }
    destination = Path(args.output_dir)
    destination.mkdir(parents=True, exist_ok=True)
    (destination / "corpus_check.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    (destination / "corpus_check.md").write_text(
        _render_markdown(report),
        encoding="utf-8",
        newline="\n",
    )
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
