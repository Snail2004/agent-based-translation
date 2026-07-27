from __future__ import annotations

import argparse
import json
from pathlib import Path

from pipeline.literary.b4_input_manifest_v1 import (
    build_b4_input_manifest_from_chapter_run_v1,
    write_b4_input_manifest_v1,
)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Bind a completed B0-B3 chapter prefix to one B4 manifest."
    )
    parser.add_argument("--chapter-run-root", required=True, type=Path)
    parser.add_argument("--target-chapter-order", required=True, type=int)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    manifest = build_b4_input_manifest_from_chapter_run_v1(
        chapter_run_root=args.chapter_run_root,
        target_chapter_order=args.target_chapter_order,
    )
    path = write_b4_input_manifest_v1(manifest, output_path=args.output)
    print(
        json.dumps(
            {
                "status": "complete",
                "manifest_path": str(path),
                "target_chapter_id": manifest["target_chapter_id"],
                "target_chapter_order": manifest["target_chapter_order"],
                "chapter_count": len(manifest["chapters"]),
                "provider_calls": 0,
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
