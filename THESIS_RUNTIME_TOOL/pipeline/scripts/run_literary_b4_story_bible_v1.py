"""Assemble an as-of B4 pack, evidence index, window slices, and UI graph."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from pipeline.literary.b4_story_bible_assembler_v1 import (
    B4StoryBibleError,
    assemble_b4_story_bible_v1,
    load_b4_input_manifest_v1,
    load_b4_profile_v1,
    write_b4_assembly_v1,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input-manifest", required=True, type=Path)
    parser.add_argument("--profile", type=Path)
    parser.add_argument("--out-dir", required=True, type=Path)
    args = parser.parse_args(argv)

    manifest = load_b4_input_manifest_v1(args.input_manifest)
    profile = load_b4_profile_v1(args.profile)
    assembly = assemble_b4_story_bible_v1(
        manifest=manifest,
        profile=profile,
    )
    written = write_b4_assembly_v1(assembly, out_dir=args.out_dir)
    result = {
        "status": "complete",
        "chapter_id": assembly.stable["chapter_id"],
        "story_bible_artifact_hash": assembly.stable["artifact_hash"],
        "window_count": len(assembly.window_slices),
        "provider_calls": 0,
        "metrics": assembly.report["metrics"],
        "written_files": [str(path.resolve()) for path in written],
    }
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except B4StoryBibleError as exc:
        raise SystemExit(f"B4 story-bible assembly refused the input: {exc}") from exc
