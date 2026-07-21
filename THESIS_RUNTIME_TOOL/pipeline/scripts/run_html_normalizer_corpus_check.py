from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

from pipeline.ingest.html_normalizer import normalize_html


def _row(name: str, source: Path, *, repeat: int) -> dict[str, object]:
    attempts = []
    durations = []
    for _ in range(repeat):
        started = time.perf_counter()
        attempts.append(normalize_html(source, doc_id=name))
        durations.append(time.perf_counter() - started)
    manifest = attempts[0].structure_manifest
    hashes = [attempt.structure_manifest["structure_sha256"] for attempt in attempts]
    roles: dict[str, int] = {}
    for unit in manifest["units"]:
        role = str(unit["role"])
        roles[role] = roles.get(role, 0) + 1
    return {
        "source": name,
        "source_sha256": manifest["source"]["sha256"],
        "deterministic": len(set(hashes)) == 1,
        "structure_sha256": hashes[0],
        "unit_count": len(manifest["units"]),
        "roles": roles,
        "translatable_count": len(manifest["translatable_chapter_ids"]),
        "review_required_count": len(manifest["review_required_unit_ids"]),
        "block_count": manifest["exact_cover"]["expected_blocks"],
        "exact_cover": manifest["exact_cover"]["coverage"],
        "cross_check": manifest["cross_check"],
        "duration_seconds": round(sum(durations) / len(durations), 3),
    }


def _markdown(report: dict[str, object]) -> str:
    lines = [
        "# HTML Normalizer Corpus Check v1",
        "",
        f"Repeat count: {report['repeat']}",
        "",
        "| Source | Deterministic | Units | Content | Front | Back | Review | Blocks | Content coverage | Seconds |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for row in report["rows"]:
        roles = row["roles"]
        cross_check = row["cross_check"]
        lines.append(
            "| {source} | {deterministic} | {unit_count} | {content} | {front} | {back} | "
            "{review_required_count} | {block_count} | {coverage:.3f} | {duration_seconds:.2f} |".format(
                **row,
                content=roles.get("content_unit", 0),
                front=roles.get("front_matter", 0),
                back=roles.get("back_matter", 0),
                coverage=float(cross_check.get("native_content_covered_by_pandoc", 0.0)),
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


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the HTML normalizer over a bounded corpus")
    parser.add_argument("--corpus-root", required=True)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--repeat", type=int, default=2)
    args = parser.parse_args()

    corpus = Path(args.corpus_root)
    sources = sorted(corpus.glob("*/source.html")) + sorted(corpus.glob("*/source.htm"))
    if not sources:
        raise SystemExit("No */source.html or */source.htm files found")
    report = {
        "schema_version": "html_normalizer_corpus_check_v1",
        "repeat": args.repeat,
        "rows": [_row(source.parent.name, source, repeat=args.repeat) for source in sources],
    }
    output = Path(args.output_dir)
    output.mkdir(parents=True, exist_ok=True)
    (output / "corpus_check.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    (output / "corpus_check.md").write_text(_markdown(report), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
