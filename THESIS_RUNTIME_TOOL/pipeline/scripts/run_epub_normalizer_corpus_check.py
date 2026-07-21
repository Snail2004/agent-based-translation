from __future__ import annotations

import argparse
import hashlib
import json
import re
import time
from collections import Counter
from pathlib import Path

from pipeline.ingest.epub_normalizer import normalize_epub


def _source(value: str) -> tuple[str, Path]:
    if "=" not in value:
        raise argparse.ArgumentTypeError("Source must be LABEL=PATH")
    label, raw_path = value.split("=", 1)
    path = Path(raw_path).expanduser().resolve()
    if not label.strip() or not path.is_file():
        raise argparse.ArgumentTypeError(f"Invalid source: {value}")
    return label.strip(), path


def _doc_id(label: str) -> str:
    value = re.sub(r"[^a-z0-9]+", "_", label.casefold()).strip("_")
    return value or "epub"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the EPUB normalizer across a local corpus")
    parser.add_argument("--source", action="append", type=_source, required=True, help="LABEL=PATH")
    parser.add_argument("--repeat", type=int, default=2)
    parser.add_argument("--pandoc", default="pandoc")
    parser.add_argument("--output-dir", required=True)
    return parser.parse_args()


def _render(report: dict[str, object]) -> str:
    lines = [
        "# EPUB Normalizer Corpus Check v1",
        "",
        f"Repeat count: {report['repeat']}",
        "",
        "| Source | Deterministic | Units | Content | Containers | Review | Blocks | Exact cover | Seconds |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for source in report["sources"]:
        roles = source["roles"]
        lines.append(
            "| {label} | {deterministic} | {units} | {content} | {containers} | {review} | {blocks} | {coverage:.3f} | {seconds:.2f} |".format(
                label=source["label"],
                deterministic=str(source["deterministic"]).lower(),
                units=source["units"],
                content=roles.get("content_unit", 0),
                containers=roles.get("container", 0),
                review=source["review_required_units"],
                blocks=source["blocks"],
                coverage=source["exact_cover"],
                seconds=source["mean_seconds"],
            )
        )
    lines.extend(
        [
            "",
            "This is an offline structural/provenance check. It does not call an LLM and does not evaluate translation quality.",
            "",
        ]
    )
    return "\n".join(lines)


def main() -> int:
    args = parse_args()
    if args.repeat < 1:
        raise SystemExit("--repeat must be at least 1")
    sources = []
    for label, path in args.source:
        attempts = []
        durations = []
        for _ in range(args.repeat):
            started = time.perf_counter()
            result = normalize_epub(
                path,
                doc_id=_doc_id(label),
                pandoc_executable=args.pandoc,
            )
            durations.append(time.perf_counter() - started)
            attempts.append(result)
        first = attempts[0].structure_manifest
        hashes = [attempt.structure_manifest["structure_sha256"] for attempt in attempts]
        roles = Counter(unit["role"] for unit in first["units"])
        sources.append(
            {
                "label": label,
                "source_name": path.name,
                "source_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                "deterministic": len(set(hashes)) == 1,
                "repeat_structure_hashes": hashes,
                "mean_seconds": round(sum(durations) / len(durations), 3),
                "units": len(first["units"]),
                "roles": dict(sorted(roles.items())),
                "blocks": first["exact_cover"]["expected_blocks"],
                "exact_cover": first["exact_cover"]["coverage"],
                "review_required_units": len(first["review_required_unit_ids"]),
                "mapped_navigation_entries": first["navigation"]["mapped_entry_count"],
                "native_fallback_files": first["extractor"]["native_empty_spine_fallback_files"],
            }
        )
    report = {
        "schema_version": "epub_normalizer_corpus_check_v1",
        "repeat": args.repeat,
        "sources": sources,
    }
    output = Path(args.output_dir)
    output.mkdir(parents=True, exist_ok=True)
    (output / "corpus_check.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    (output / "corpus_check.md").write_text(_render(report), encoding="utf-8")
    print(output / "corpus_check.json")
    print(output / "corpus_check.md")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
