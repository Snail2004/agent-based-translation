from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any


DOC_ID = "d2l"


@dataclass(frozen=True)
class D2LSourceBlock:
    block_id: str
    chapter_id: str
    chapter_slug: str
    section_slug: str
    source_path: str
    source_sha256: str
    section_order: int
    block_order_in_section: int
    block_type: str
    text: str
    translation_mode: str
    line_start: int
    line_end: int


@dataclass(frozen=True)
class D2LMarkdownReport:
    doc_id: str
    chapters: int
    loaded_chapters: int
    sections: int
    blocks: int
    prose_blocks: int
    source_commit: str
    source_files: int
    warnings: list[str]

    def to_json_dict(self) -> dict[str, Any]:
        return asdict(self)


def load_d2l_markdown(
    conn: sqlite3.Connection,
    source_root: str | Path,
    *,
    source_commit: str,
    doc_id: str = DOC_ID,
) -> D2LMarkdownReport:
    """Load D2L EN *_origin.md files into documents/blocks.

    The Vietnamese .md files are intentionally ignored. Runtime sees only the
    English source snapshot; glossary gold is stored separately for eval only.
    """

    root = Path(source_root)
    blocks, manifest, warnings = parse_d2l_markdown(root)
    chapter_count = len(_chapter_dirs_in_book_order(root))
    loaded_chapter_count = len({block.chapter_id for block in blocks})
    section_count = len({(block.chapter_id, block.section_slug) for block in blocks})
    prose_blocks = sum(1 for block in blocks if block.block_type == "prose")

    metadata = {
        "source_name": "D2L Vietnamese repo EN origin snapshot",
        "source_remote": "https://github.com/d2l-ai/d2l-vi.git",
        "source_commit": source_commit,
        "source_layout": "*_origin.md only; Vietnamese .md files ignored",
        "manifest": manifest,
    }

    conn.execute(
        """
        INSERT INTO documents (
          doc_id, job_id, source_filename, source_lang, target_lang,
          metadata_json, updated_at
        )
        VALUES (?, ?, ?, 'en', 'vi', ?, CURRENT_TIMESTAMP)
        ON CONFLICT(doc_id) DO UPDATE SET
          job_id = excluded.job_id,
          source_filename = excluded.source_filename,
          source_lang = excluded.source_lang,
          target_lang = excluded.target_lang,
          metadata_json = excluded.metadata_json,
          updated_at = CURRENT_TIMESTAMP
        """,
        (doc_id, doc_id, str(root), json.dumps(metadata, ensure_ascii=False, sort_keys=True)),
    )
    conn.execute("DELETE FROM blocks WHERE doc_id = ?", (doc_id,))

    for order_index, block in enumerate(blocks):
        style_json = {
            "source_path": block.source_path,
            "source_sha256": block.source_sha256,
            "source_commit": source_commit,
            "chapter_slug": block.chapter_slug,
            "section_slug": block.section_slug,
            "section_order": block.section_order,
            "block_order_in_section": block.block_order_in_section,
            "line_start": block.line_start,
            "line_end": block.line_end,
        }
        conn.execute(
            """
            INSERT INTO blocks (
              block_id, doc_id, order_index, block_type, chapter_id,
              text, original_text, style_json, translation_mode, content_kind
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                block.block_id,
                doc_id,
                order_index,
                block.block_type,
                block.chapter_id,
                block.text,
                block.text,
                json.dumps(style_json, ensure_ascii=False, sort_keys=True),
                block.translation_mode,
                block.block_type,
            ),
        )

    return D2LMarkdownReport(
        doc_id=doc_id,
        chapters=chapter_count,
        loaded_chapters=loaded_chapter_count,
        sections=section_count,
        blocks=len(blocks),
        prose_blocks=prose_blocks,
        source_commit=source_commit,
        source_files=len(manifest),
        warnings=warnings,
    )


def parse_d2l_markdown(source_root: str | Path) -> tuple[list[D2LSourceBlock], list[dict[str, Any]], list[str]]:
    root = Path(source_root)
    if not root.exists():
        raise FileNotFoundError(root)
    chapter_dirs = _chapter_dirs_in_book_order(root)
    manifest: list[dict[str, Any]] = []
    blocks: list[D2LSourceBlock] = []
    warnings: list[str] = []
    seen_ids: set[str] = set()

    for chapter_order, chapter_dir in enumerate(chapter_dirs):
        chapter_slug = _slug_from_chapter_dir(chapter_dir)
        chapter_id = f"d2l_{chapter_slug}"
        section_files = _section_files_in_order(chapter_dir)
        if not section_files:
            warnings.append(f"no_origin_sections:{chapter_dir.name}")
            continue
        for section_order, section_path in enumerate(section_files):
            rel_path = section_path.relative_to(root).as_posix()
            source_sha = _sha256_file(section_path)
            manifest.append(
                {
                    "path": rel_path,
                    "sha256": source_sha,
                    "chapter_order": chapter_order,
                    "section_order": section_order,
                }
            )
            section_slug = _slug(section_path.name.removesuffix("_origin.md"))
            for block_order, (text, line_start, line_end) in enumerate(_split_markdown_blocks(section_path)):
                block_type = classify_block(text)
                block_id = f"d2l_{chapter_slug}_{section_slug}_b{block_order + 1:03d}"
                if block_id in seen_ids:
                    raise ValueError(f"Duplicate D2L block_id: {block_id}")
                seen_ids.add(block_id)
                blocks.append(
                    D2LSourceBlock(
                        block_id=block_id,
                        chapter_id=chapter_id,
                        chapter_slug=chapter_slug,
                        section_slug=section_slug,
                        source_path=rel_path,
                        source_sha256=source_sha,
                        section_order=section_order,
                        block_order_in_section=block_order,
                        block_type=block_type,
                        text=text,
                        translation_mode="translate" if block_type == "prose" else "passthrough",
                        line_start=line_start,
                        line_end=line_end,
                    )
                )
    return blocks, manifest, warnings


def classify_block(text: str) -> str:
    stripped_lines = [line.strip() for line in text.splitlines() if line.strip()]
    first = stripped_lines[0] if stripped_lines else ""
    if first.startswith("#"):
        return "heading"
    if first.startswith("```") or first.startswith("~~~"):
        return "code"
    if first == "$$":
        return "math_block"
    if first.startswith("!["):
        return "image"
    if first.startswith((":label:", ":numref:", ":eqlabel:")):
        return "label"
    return "prose"


def _chapter_dirs_in_book_order(root: Path) -> list[Path]:
    from_index: list[Path] = []
    index_path = root / "index.md"
    if index_path.exists():
        for line in index_path.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if not stripped.startswith("chapter_"):
                continue
            chapter_name = stripped.split("/", 1)[0]
            chapter_dir = root / chapter_name
            if chapter_dir.is_dir() and chapter_dir not in from_index:
                from_index.append(chapter_dir)
    known = set(from_index)
    extras = sorted(
        [path for path in root.glob("chapter_*") if path.is_dir() and path not in known],
        key=lambda path: path.name,
    )
    return [*from_index, *extras]


def _section_files_in_order(chapter_dir: Path) -> list[Path]:
    origin_files = {path.stem.removesuffix("_origin"): path for path in chapter_dir.glob("*_origin.md")}
    ordered: list[Path] = []
    index_origin = chapter_dir / "index_origin.md"
    if index_origin.exists():
        ordered.append(index_origin)
        for line in index_origin.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith(":") or stripped.startswith("`"):
                continue
            if re.match(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$", stripped):
                candidate = origin_files.get(stripped)
                if candidate and candidate not in ordered:
                    ordered.append(candidate)
    for path in sorted(origin_files.values(), key=lambda value: value.name):
        if path not in ordered:
            ordered.append(path)
    return ordered


def _split_markdown_blocks(path: Path) -> list[tuple[str, int, int]]:
    blocks: list[tuple[str, int, int]] = []
    current: list[str] = []
    start_line = 1
    in_fence = False
    in_math = False
    lines = path.read_text(encoding="utf-8").splitlines()

    def flush(end_line: int) -> None:
        nonlocal current, start_line
        text = "\n".join(current).strip()
        if text:
            blocks.append((text, start_line, end_line))
        current = []
        start_line = end_line + 1

    for index, line in enumerate(lines, 1):
        stripped = line.strip()
        opens_or_closes_fence = stripped.startswith("```") or stripped.startswith("~~~")
        toggles_math = stripped == "$$"
        if not current and stripped:
            start_line = index
        if not stripped and not in_fence and not in_math:
            flush(index - 1)
            continue
        current.append(line)
        if opens_or_closes_fence:
            in_fence = not in_fence
        elif toggles_math:
            in_math = not in_math
    flush(len(lines))
    return blocks


def _slug_from_chapter_dir(chapter_dir: Path) -> str:
    return _slug(chapter_dir.name.removeprefix("chapter_"))


def _slug(value: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9]+", "_", value.strip().lower()).strip("_")
    return slug or "section"


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()
