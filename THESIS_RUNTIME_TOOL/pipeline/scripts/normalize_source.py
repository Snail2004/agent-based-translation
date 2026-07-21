from __future__ import annotations

import argparse
import json
from pathlib import Path

from pipeline.ingest.unified_source_normalizer import (
    normalize_source,
    write_unified_normalization,
)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Normalize EPUB, HTML, Markdown or TXT into the canonical thesis contract"
    )
    parser.add_argument("--source", required=True)
    parser.add_argument("--doc-id", required=True)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--source-language", default="en")
    parser.add_argument("--target-language", default="vi")
    parser.add_argument("--pandoc", default="pandoc")
    args = parser.parse_args()

    result = normalize_source(
        args.source,
        doc_id=args.doc_id,
        source_language=args.source_language,
        target_language=args.target_language,
        pandoc_executable=args.pandoc,
    )
    document_path, manifest_path, receipt_path = write_unified_normalization(
        result,
        args.output_dir,
    )
    payload = dict(result.receipt)
    payload["artifacts"] = {
        "document": str(Path(document_path).resolve()),
        "structure_manifest": str(Path(manifest_path).resolve()),
        "normalization_receipt": str(Path(receipt_path).resolve()),
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
