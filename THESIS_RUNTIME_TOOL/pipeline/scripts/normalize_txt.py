from __future__ import annotations

import argparse
import json

from pipeline.ingest.txt_normalizer import normalize_txt, write_txt_normalization


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Normalize one TXT file into canonical document and structure artifacts"
    )
    parser.add_argument("--source", required=True)
    parser.add_argument("--doc-id", required=True)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--source-language", default="en")
    parser.add_argument("--target-language", default="vi")
    parser.add_argument("--pandoc", default="pandoc")
    args = parser.parse_args()

    result = normalize_txt(
        args.source,
        doc_id=args.doc_id,
        source_language=args.source_language,
        target_language=args.target_language,
        pandoc_executable=args.pandoc,
    )
    document_path, manifest_path = write_txt_normalization(result, args.output_dir)
    manifest = result.structure_manifest
    print(
        json.dumps(
            {
                "document": str(document_path),
                "structure_manifest": str(manifest_path),
                "units": len(manifest["units"]),
                "translatable_units": len(manifest["translatable_chapter_ids"]),
                "review_required_units": len(manifest["review_required_unit_ids"]),
                "blocks": manifest["exact_cover"]["expected_blocks"],
                "cross_check": manifest["cross_check"],
                "structure_sha256": manifest["structure_sha256"],
            },
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
