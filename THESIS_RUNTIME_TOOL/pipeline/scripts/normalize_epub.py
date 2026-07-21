from __future__ import annotations

import argparse
import json
from pathlib import Path

from pipeline.ingest.epub_normalizer import normalize_epub, write_epub_normalization


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Normalize one EPUB into tool-compatible document.json and structure_manifest.json"
    )
    parser.add_argument("source", help="Input EPUB path")
    parser.add_argument("--doc-id", required=True, help="Stable document identifier")
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--source-language", default="en")
    parser.add_argument("--target-language", default="vi")
    parser.add_argument("--pandoc", default="pandoc", help="Pandoc executable")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    result = normalize_epub(
        args.source,
        doc_id=args.doc_id,
        source_language=args.source_language,
        target_language=args.target_language,
        pandoc_executable=args.pandoc,
    )
    document_path, manifest_path = write_epub_normalization(result, args.output_dir)
    manifest = result.structure_manifest
    summary = {
        "document": str(document_path),
        "structure_manifest": str(manifest_path),
        "units": len(manifest["units"]),
        "translatable_units": len(manifest["translatable_chapter_ids"]),
        "review_required_units": len(manifest["review_required_unit_ids"]),
        "exact_cover": manifest["exact_cover"],
    }
    print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
