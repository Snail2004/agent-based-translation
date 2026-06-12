from __future__ import annotations

import argparse
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


TOP_LEVEL_KEYS = ("schema_version", "doc_id", "metadata", "chapters")
CHAPTER_KEYS = ("chapter_id", "order_index", "title", "blocks")
BLOCK_KEYS = (
    "block_id",
    "order_index",
    "page_ids",
    "block_type",
    "is_chapter_opening",
    "source_text",
    "clean_text",
    "sentences",
    "quality_flags",
)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Copy a canonical document.json and strip all annotations."
    )
    parser.add_argument("--from", dest="source", required=True, help="Source document.json")
    parser.add_argument("--to", dest="target", required=True, help="Target document.json")
    args = parser.parse_args()

    source = Path(args.source)
    target = Path(args.target)
    prepare_source(source, target)
    print(f"Prepared stripped source: {target}")
    return 0


def prepare_source(source: str | Path, target: str | Path) -> dict[str, str]:
    source_path = Path(source)
    target_path = Path(target)
    original_bytes = source_path.read_bytes()
    document = json.loads(original_bytes.decode("utf-8"))
    stripped = _strip_document(document)

    target_path.parent.mkdir(parents=True, exist_ok=True)
    stripped_text = json.dumps(stripped, ensure_ascii=False, indent=2) + "\n"
    target_path.write_text(stripped_text, encoding="utf-8")

    original_sha256 = hashlib.sha256(original_bytes).hexdigest()
    stripped_sha256 = hashlib.sha256(stripped_text.encode("utf-8")).hexdigest()
    provenance_path = target_path.with_name("PROVENANCE.md")
    provenance_path.write_text(
        _provenance_text(
            source_path=source_path,
            target_path=target_path,
            original_sha256=original_sha256,
            stripped_sha256=stripped_sha256,
        ),
        encoding="utf-8",
    )
    return {
        "source": str(source_path),
        "target": str(target_path),
        "original_sha256": original_sha256,
        "stripped_sha256": stripped_sha256,
    }


def _strip_document(document: dict[str, Any]) -> dict[str, Any]:
    stripped = {key: document.get(key) for key in TOP_LEVEL_KEYS if key in document}
    chapters = []
    for chapter in document.get("chapters") or []:
        clean_chapter = {key: chapter.get(key) for key in CHAPTER_KEYS if key != "blocks"}
        clean_blocks = []
        for block in chapter.get("blocks") or []:
            clean_block = {key: block.get(key) for key in BLOCK_KEYS if key in block}
            clean_block["annotations"] = {}
            clean_blocks.append(clean_block)
        clean_chapter["blocks"] = clean_blocks
        chapters.append(clean_chapter)
    stripped["chapters"] = chapters
    return stripped


def _provenance_text(
    *,
    source_path: Path,
    target_path: Path,
    original_sha256: str,
    stripped_sha256: str,
) -> str:
    copied_at = datetime.now(timezone.utc).isoformat()
    return (
        "# Source Provenance\n\n"
        f"- Source file: `{source_path}`\n"
        f"- Target file: `{target_path}`\n"
        f"- Copied at: `{copied_at}`\n"
        "- Transform: annotations stripped theo LOCK §6.3; every block has "
        "`annotations: {}`.\n"
        f"- Original sha256: `{original_sha256}`\n"
        f"- Stripped sha256: `{stripped_sha256}`\n"
    )


if __name__ == "__main__":
    raise SystemExit(main())
