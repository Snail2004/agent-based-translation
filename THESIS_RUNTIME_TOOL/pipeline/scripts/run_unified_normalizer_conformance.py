from __future__ import annotations

import argparse
import json
import tempfile
import time
from pathlib import Path
from typing import Any

from pipeline.ingest.document_loader import load_document
from pipeline.ingest.unified_source_normalizer import (
    normalize_source,
    write_unified_normalization,
)


def _doc_id(source: Path, index: int) -> str:
    raw = f"unified_{index:02d}_{source.parent.name}_{source.stem}".casefold()
    return "".join(character if character.isalnum() else "_" for character in raw).strip("_")


def _render_markdown(report: dict[str, Any]) -> str:
    lines = [
        "# Unified Source Normalizer Conformance v1",
        "",
        "| Source | Format | Status | Units | Translatable | Review | Blocks | Loader warnings | Seconds |",
        "|---|---|---|---:|---:|---:|---:|---:|---:|",
    ]
    for row in report["rows"]:
        counts = row["counts"]
        lines.append(
            "| {source} | {source_format} | {status} | {units} | {translatable} | "
            "{review} | {blocks} | {warnings} | {seconds:.3f} |".format(
                source=row["source"],
                source_format=row["source_format"],
                status=row["status"],
                units=counts["units"],
                translatable=counts["translatable_units"],
                review=counts["review_required_units"],
                blocks=counts["blocks"],
                warnings=len(row["loader_warnings"]),
                seconds=row["duration_seconds"],
            )
        )
    lines.extend(
        [
            "",
            "Each row passed the shared contract validator and loaded its generated "
            "`document.json` into an isolated SQLite database.",
            "",
        ]
    )
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run the unified normalizer and loader over explicit sources"
    )
    parser.add_argument("--source", action="append", required=True)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--pandoc", default="pandoc")
    args = parser.parse_args()

    rows: list[dict[str, Any]] = []
    for index, source_value in enumerate(args.source, start=1):
        source = Path(source_value).resolve()
        started = time.perf_counter()
        result = normalize_source(
            source,
            doc_id=_doc_id(source, index),
            pandoc_executable=args.pandoc,
        )
        with tempfile.TemporaryDirectory(prefix="unified-normalizer-") as temporary:
            document_path, _manifest_path, _receipt_path = write_unified_normalization(
                result,
                temporary,
            )
            loader = load_document(Path(temporary) / "memory.sqlite3", document_path)
        rows.append(
            {
                "source": f"{source.parent.name}/{source.name}",
                "source_format": result.receipt["source_format"],
                "source_sha256": result.receipt["source_sha256"],
                "structure_sha256": result.receipt["structure_sha256"],
                "status": result.receipt["status"],
                "counts": result.receipt["counts"],
                "manifest_schema_version": result.receipt["manifest_schema_version"],
                "normalizer_version": result.receipt["normalizer_version"],
                "loader_warnings": loader.warnings,
                "duration_seconds": round(time.perf_counter() - started, 3),
            }
        )

    report = {
        "schema_version": "unified_source_normalizer_conformance_v1",
        "rows": rows,
    }
    output = Path(args.output_dir)
    output.mkdir(parents=True, exist_ok=True)
    (output / "conformance.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    (output / "conformance.md").write_text(
        _render_markdown(report),
        encoding="utf-8",
        newline="\n",
    )
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
