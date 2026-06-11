import hashlib
import json
import re
import unicodedata
import zipfile
from pathlib import Path
from typing import Any

from config import PIPELINE_VERSION
from services.dataset_io import write_json_atomic
from services.extraction import (
    BlockHTMLParser,
    _epub_document_type_tokens,
    _epub_rootfile,
    _epub_spine_entries,
    _epub_toc_entries,
    _txt_parts,
    block_type_for,
    chapters_to_document,
    clean_gutenberg_text,
    normalize_anchor,
    normalize_text,
)


DROP_FRACTION_WARNING_THRESHOLD = 0.30
ALLOWED_DROP_REASONS = {
    "title_page",
    "copyright",
    "imprint",
    "gutenberg_license",
    "uncopyright",
    "toc_repeat",
    "colophon",
    "front_matter",
    "back_matter",
}
ALLOWED_SEPARATORS = {" ", "\n\n"}
ALLOWED_EPUB_ROLES = {"front_matter", "back_matter", "body"}


class StructurePlanError(ValueError):
    pass


def _part_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]


def _source_fingerprint(parts: list[dict[str, Any]]) -> str:
    payload = "|".join(f"{part['index']}:{part.get('hash') or _part_hash(str(part.get('text') or ''))}" for part in parts)
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]
    return f"{len(parts)}:{digest}"


def _heading_candidate(text: str, tag: str | None = None) -> bool:
    if tag in {"h1", "h2", "h3"}:
        return True
    stripped = text.strip()
    if not stripped or len(stripped) > 120:
        return False
    if re.match(r"^(chapter|book|part)\b", stripped, re.IGNORECASE):
        return True
    if re.match(r"^[ivxlcdm]+\.?$", stripped, re.IGNORECASE):
        return True
    if stripped.isupper() and len(stripped.split()) <= 12:
        return True
    return False


def _txt_candidate_parts(source_path: Path, doc_id: str) -> dict[str, Any]:
    raw = source_path.read_text(encoding="utf-8")
    cleaned, gutenberg_report = clean_gutenberg_text(raw)
    normalized = cleaned.replace("\r\n", "\n").replace("\r", "\n")
    parts: list[dict[str, Any]] = []
    current: list[str] = []
    start_line: int | None = None

    def flush(end_line: int) -> None:
        nonlocal current, start_line
        if not current:
            return
        raw_part = "\n".join(current).strip()
        text = normalize_text(raw_part)
        if text:
            index = len(parts)
            lines = [normalize_text(line) for line in raw_part.split("\n") if normalize_text(line)]
            parts.append({
                "index": index,
                "source_ref": {
                    "line_start": start_line,
                    "line_end": end_line,
                },
                "text": text,
                "raw": raw_part,
                "hash": _part_hash(text),
                "n_lines": len(lines),
                "is_heading_candidate": _heading_candidate(text),
            })
        current = []
        start_line = None

    for line_no, line in enumerate(normalized.split("\n"), 1):
        if line.strip():
            if start_line is None:
                start_line = line_no
            current.append(line)
        else:
            flush(line_no - 1)
    flush(len(normalized.split("\n")))

    existing_parts = _txt_parts(cleaned)
    if len(existing_parts) != len(parts) or [p["text"] for p in existing_parts] != [p["text"] for p in parts]:
        raise StructurePlanError("TXT candidate parts drifted from extractor _txt_parts().")

    return _candidate_input(doc_id, "txt", source_path, parts, {"gutenberg": gutenberg_report})


def _toc_titles_by_target(toc_entries: list[dict[str, str]]) -> dict[tuple[str, str], str]:
    titles: dict[tuple[str, str], str] = {}
    for entry in toc_entries:
        titles.setdefault((entry.get("file") or "", normalize_anchor(entry.get("anchor") or "")), entry.get("title") or "")
        titles.setdefault((entry.get("file") or "", ""), entry.get("title") or "")
    return titles


def _epub_candidate_parts(source_path: Path, doc_id: str) -> dict[str, Any]:
    parts: list[dict[str, Any]] = []
    with zipfile.ZipFile(source_path) as zf:
        rootfile = _epub_rootfile(zf)
        spine_entries, found_front_matter_metadata = _epub_spine_entries(zf, rootfile)
        toc_source, toc_entries = _epub_toc_entries(zf, rootfile)
        toc_titles = _toc_titles_by_target(toc_entries)
        for spine_index, spine_entry in enumerate(spine_entries):
            file_name = str(spine_entry["file"])
            if file_name not in zf.namelist():
                continue
            raw = zf.read(file_name).decode("utf-8", errors="replace")
            parser = BlockHTMLParser()
            parser.feed(raw)
            parser.close()
            doc_tokens = sorted(_epub_document_type_tokens(zf, file_name))
            for local_index, block in enumerate(parser.blocks):
                text = str(block.get("text") or "")
                if not text:
                    continue
                tag = str(block.get("tag") or "")
                anchor = normalize_anchor(str(block.get("anchor") or ""))
                index = len(parts)
                nav_title = toc_titles.get((file_name, anchor)) or toc_titles.get((file_name, ""))
                heading_level = int(tag[1]) if tag in {"h1", "h2", "h3"} else None
                parts.append({
                    "index": index,
                    "source_ref": {
                        "href": file_name,
                        "anchor": anchor,
                        "local_index": local_index,
                    },
                    "spine_index": spine_index,
                    "spine_file": file_name,
                    "spine_skip_reason": spine_entry.get("skip_reason"),
                    "nav_title": nav_title,
                    "doc_type_tokens": doc_tokens,
                    "heading_level": heading_level,
                    "tag": tag,
                    "text": text,
                    "hash": _part_hash(text),
                    "n_lines": 1,
                    "is_heading_candidate": _heading_candidate(text, tag),
                })
    return _candidate_input(
        doc_id,
        "epub",
        source_path,
        parts,
        {
            "toc_source": toc_source,
            "toc_items": len(toc_entries),
            "found_front_matter_metadata": found_front_matter_metadata,
        },
    )


def _candidate_input(
    doc_id: str,
    source_format: str,
    source_path: Path,
    parts: list[dict[str, Any]],
    parser_report: dict[str, Any],
) -> dict[str, Any]:
    return {
        "doc_id": doc_id,
        "source_format": source_format,
        "pipeline_version": PIPELINE_VERSION,
        "source_path": str(source_path),
        "source_fingerprint": _source_fingerprint(parts),
        "parts": parts,
        "parser_report": parser_report,
    }


def build_candidate_parts(source: str | Path, doc_id: str | None = None) -> dict[str, Any]:
    source_path = Path(source)
    resolved_doc_id = doc_id or source_path.parent.name or source_path.stem
    suffix = source_path.suffix.lower()
    if suffix == ".txt":
        return _txt_candidate_parts(source_path, resolved_doc_id)
    if suffix == ".epub":
        return _epub_candidate_parts(source_path, resolved_doc_id)
    raise StructurePlanError(f"Unsupported source format for normalizer: {source_path.suffix}")


def load_plan(path: str | Path) -> dict[str, Any]:
    with Path(path).open("r", encoding="utf-8") as f:
        return json.load(f)


def _index_maps(candidate_parts: dict[str, Any]) -> tuple[dict[int, dict[str, Any]], set[int]]:
    parts_by_index = {int(part["index"]): part for part in candidate_parts.get("parts", [])}
    spine_indexes = {
        int(part["spine_index"])
        for part in candidate_parts.get("parts", [])
        if part.get("spine_index") is not None
    }
    return parts_by_index, spine_indexes


def _normalize_title(value: str) -> str:
    value = unicodedata.normalize("NFC", value or "")
    value = re.sub(r"\s+", " ", value).strip()
    value = re.sub(r"[.:\-\u2014]+$", "", value).strip()
    return value


def _strip_leading_chapter_label(value: str) -> str:
    normalized = _normalize_title(value)
    patterns = [
        r"^(?:chapter|book|part)\s+(?:[0-9]+|[ivxlcdm]+|one|two|three|four|five|six|seven|eight|nine|ten)\s*[.:\-\u2014]?\s*",
        r"^(?:[ivxlcdm]+|[0-9]+)\s*[.:\-\u2014]\s+",
    ]
    for pattern in patterns:
        stripped = re.sub(pattern, "", normalized, flags=re.IGNORECASE).strip()
        if stripped != normalized:
            return stripped
    return normalized


def _title_is_valid(raw_heading: str, title: str | None) -> bool:
    if title is None or not str(title).strip():
        return True
    raw = _normalize_title(raw_heading)
    desired = _normalize_title(str(title))
    if not desired:
        return True
    if desired.casefold() == raw.casefold():
        return True
    stripped = _strip_leading_chapter_label(raw)
    if desired.casefold() == stripped.casefold():
        return True
    return bool(stripped and desired.casefold() in stripped.casefold())


def validate_plan(candidate_parts: dict[str, Any], plan: dict[str, Any]) -> dict[str, Any]:
    errors: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []
    parts_by_index, spine_indexes = _index_maps(candidate_parts)

    def error(location: str, message: str) -> None:
        errors.append({"location": location, "message": message, "severity": "error"})

    def warning(location: str, message: str) -> None:
        warnings.append({"location": location, "message": message, "severity": "warning"})

    if plan.get("source_fingerprint") != candidate_parts.get("source_fingerprint"):
        error("source_fingerprint", "StructurePlan fingerprint does not match candidate parts.")

    confidence = plan.get("confidence", 0.0)
    if not isinstance(confidence, (int, float)) or not 0 <= float(confidence) <= 1:
        error("confidence", "confidence must be a number in [0, 1].")

    drop_indices: set[int] = set()
    for item in plan.get("drop_parts", []):
        index = item.get("part_index")
        if index not in parts_by_index:
            error("drop_parts", f"unknown part_index: {index}")
        else:
            drop_indices.add(int(index))
        reason = item.get("reason")
        if reason not in ALLOWED_DROP_REASONS:
            error("drop_parts", f"invalid drop reason for part {index}: {reason}")

    heading_indices: set[int] = set()
    for item in plan.get("chapter_headings", []):
        index = item.get("part_index")
        if index not in parts_by_index:
            error("chapter_headings", f"unknown part_index: {index}")
            continue
        heading_indices.add(int(index))
        if int(index) in drop_indices:
            error("chapter_headings", f"part {index} cannot be both dropped and a chapter heading.")
        title = item.get("title")
        if not _title_is_valid(str(parts_by_index[int(index)].get("text") or ""), title):
            warning("chapter_headings", f"title for part {index} is not directly supported by heading text.")

    merged_indices: set[int] = set()
    for item in plan.get("merge_parts", []):
        indices = item.get("part_indices") or []
        separator = item.get("separator", "\n\n")
        if separator not in ALLOWED_SEPARATORS:
            error("merge_parts", f"invalid separator: {separator!r}")
        if not indices:
            error("merge_parts", "merge entry must include part_indices.")
        for index in indices:
            if index not in parts_by_index:
                error("merge_parts", f"unknown part_index: {index}")
                continue
            if int(index) in drop_indices:
                error("merge_parts", f"part {index} cannot be merged because it is dropped.")
            if int(index) in heading_indices:
                error("merge_parts", f"part {index} cannot be merged because it is a chapter heading.")
            if int(index) in merged_indices:
                error("merge_parts", f"part {index} appears in multiple merge groups.")
            merged_indices.add(int(index))

    for item in plan.get("epub_section_roles", []):
        index = item.get("spine_index")
        role = item.get("role")
        if index not in spine_indexes:
            error("epub_section_roles", f"unknown spine_index: {index}")
        if role not in ALLOWED_EPUB_ROLES:
            error("epub_section_roles", f"invalid EPUB section role for spine {index}: {role}")

    return {
        "ok": not errors,
        "errors": errors,
        "warnings": warnings,
    }


def _snippet(text: str, limit: int = 160) -> str:
    value = re.sub(r"\s+", " ", text).strip()
    return value[:limit]


def _safe_title(part: dict[str, Any], requested: str | None, report: dict[str, Any]) -> str:
    raw = str(part.get("text") or "")
    if requested is not None and str(requested).strip() and _title_is_valid(raw, str(requested)):
        return _normalize_title(str(requested))
    if requested is not None and str(requested).strip():
        report["flags"].append({
            "part_index": part["index"],
            "flag": "needs_human_check",
            "reason": "invalid_title",
            "requested_title": requested,
            "fallback_title": raw,
        })
        report["needs_human_check"] = True
    return _normalize_title(raw) or f"Chapter {part['index']}"


def _role_drop_indices(candidate_parts: dict[str, Any], plan: dict[str, Any]) -> dict[int, dict[str, Any]]:
    roles = {
        int(item["spine_index"]): item
        for item in plan.get("epub_section_roles", [])
        if item.get("role") in {"front_matter", "back_matter"}
    }
    dropped: dict[int, dict[str, Any]] = {}
    for part in candidate_parts.get("parts", []):
        spine_index = part.get("spine_index")
        if spine_index in roles:
            role = roles[int(spine_index)]
            dropped[int(part["index"])] = {
                "part_index": int(part["index"]),
                "reason": role.get("reason") or role.get("role"),
                "via": "epub_section_roles",
            }
    return dropped


def _merge_maps(plan: dict[str, Any]) -> tuple[dict[int, dict[str, Any]], set[int]]:
    by_first: dict[int, dict[str, Any]] = {}
    members: set[int] = set()
    for item in plan.get("merge_parts", []):
        indices = [int(index) for index in item.get("part_indices", [])]
        if not indices:
            continue
        ordered = sorted(indices)
        by_first[ordered[0]] = {
            "part_indices": ordered,
            "separator": item.get("separator", "\n\n"),
            "reason": item.get("reason", ""),
        }
        members.update(ordered[1:])
    return by_first, members


def _block_from_parts(parts_by_index: dict[int, dict[str, Any]], indices: list[int], separator: str) -> dict[str, Any]:
    text = separator.join(str(parts_by_index[index].get("text") or "") for index in indices)
    if text != separator.join(str(parts_by_index[index].get("text") or "") for index in indices):
        raise StructurePlanError("Content invariance mismatch while building normalized block.")
    return {
        "source_part_refs": indices,
        "text": text,
    }


def _ensure_content_invariance(normalized_document: dict[str, Any], parts_by_index: dict[int, dict[str, Any]], merge_by_first: dict[int, dict[str, Any]]) -> None:
    for chapter in normalized_document.get("chapters", []):
        for block in chapter.get("blocks", []):
            refs = [int(index) for index in block.get("source_part_refs", [])]
            if not refs:
                raise StructurePlanError("Normalized block has no source_part_refs.")
            separator = "\n\n"
            if refs[0] in merge_by_first:
                separator = merge_by_first[refs[0]].get("separator", "\n\n")
            expected = separator.join(str(parts_by_index[index].get("text") or "") for index in refs)
            if block.get("text") != expected:
                raise StructurePlanError("Content invariance mismatch in normalized_document.json.")


def apply_plan(candidate_parts: dict[str, Any], plan: dict[str, Any], output_dir: str | Path) -> dict[str, Any]:
    validation = validate_plan(candidate_parts, plan)
    if not validation["ok"]:
        raise StructurePlanError("; ".join(error["message"] for error in validation["errors"]))

    parts = sorted(candidate_parts.get("parts", []), key=lambda part: int(part["index"]))
    parts_by_index, _ = _index_maps(candidate_parts)
    role_drops = _role_drop_indices(candidate_parts, plan)
    explicit_drops = {
        int(item["part_index"]): {
            "part_index": int(item["part_index"]),
            "reason": item.get("reason"),
            "via": "drop_parts",
        }
        for item in plan.get("drop_parts", [])
    }
    dropped = {**explicit_drops, **role_drops}
    headings = {int(item["part_index"]): item for item in plan.get("chapter_headings", [])}
    merge_by_first, merged_members = _merge_maps(plan)

    report: dict[str, Any] = {
        "doc_id": plan.get("doc_id") or candidate_parts.get("doc_id"),
        "source_format": candidate_parts.get("source_format"),
        "source_fingerprint": candidate_parts.get("source_fingerprint"),
        "pipeline_version": candidate_parts.get("pipeline_version"),
        "low_confidence": False,
        "needs_human_check": False,
        "validation_warnings": validation["warnings"],
        "dropped_parts": [],
        "merges": [],
        "flags": list(plan.get("flags", [])),
        "epub_section_roles": list(plan.get("epub_section_roles", [])),
        "ai_provenance": {
            "assist_model": None,
            "prompt_id": "source-structure-normalizer-v1",
            "assisted_at": None,
        },
    }

    total_chars = sum(len(str(part.get("text") or "")) for part in parts)
    dropped_chars = 0
    for index, info in sorted(dropped.items()):
        part = parts_by_index[index]
        dropped_chars += len(str(part.get("text") or ""))
        report["dropped_parts"].append({
            "part_index": index,
            "reason": info.get("reason"),
            "via": info.get("via"),
            "snippet": _snippet(str(part.get("text") or "")),
        })
    drop_fraction = dropped_chars / total_chars if total_chars else 0.0
    report["drop_fraction"] = round(drop_fraction, 4)
    if drop_fraction > DROP_FRACTION_WARNING_THRESHOLD:
        report["low_confidence"] = True
        report["needs_human_check"] = True
        report["flags"].append({
            "flag": "needs_human_check",
            "reason": "drop_fraction_exceeded",
            "drop_fraction": report["drop_fraction"],
        })
    if float(plan.get("confidence", 0.0)) < 0.75:
        report["low_confidence"] = True
        report["needs_human_check"] = True
        report["flags"].append({
            "flag": "needs_human_check",
            "reason": "low_plan_confidence",
            "confidence": plan.get("confidence"),
        })

    chapters: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    if not headings:
        report["low_confidence"] = True
        report["needs_human_check"] = True
        report["flags"].append({"flag": "needs_human_check", "reason": "empty_chapter_headings"})
        current = {"order_index": 1, "title": "Chapter 1", "blocks": []}
        chapters.append(current)

    def new_chapter(title: str) -> dict[str, Any]:
        chapter = {"order_index": len(chapters) + 1, "title": title, "blocks": []}
        chapters.append(chapter)
        return chapter

    for part in parts:
        index = int(part["index"])
        if index in dropped:
            continue
        if index in headings:
            current = new_chapter(_safe_title(part, headings[index].get("title"), report))
            continue
        if index in merged_members:
            continue
        if current is None:
            current = new_chapter("Pre-heading content")
            report["needs_human_check"] = True
            report["flags"].append({
                "part_index": index,
                "flag": "needs_human_check",
                "reason": "part_before_first_heading",
            })
        if index in merge_by_first:
            merge = merge_by_first[index]
            refs = [ref for ref in merge["part_indices"] if ref not in dropped and ref not in headings]
            if not refs:
                continue
            current["blocks"].append(_block_from_parts(parts_by_index, refs, merge["separator"]))
            report["merges"].append({
                "part_indices": refs,
                "separator": merge["separator"],
                "reason": merge.get("reason"),
            })
        else:
            current["blocks"].append(_block_from_parts(parts_by_index, [index], "\n\n"))

    normalized_document = {
        "doc_id": plan.get("doc_id") or candidate_parts.get("doc_id"),
        "source_format": candidate_parts.get("source_format"),
        "chapters": chapters,
    }
    _ensure_content_invariance(normalized_document, parts_by_index, merge_by_first)

    output_path = Path(output_dir)
    output_path.mkdir(parents=True, exist_ok=True)
    write_json_atomic(output_path / "structure_plan.json", plan)
    write_json_atomic(output_path / "normalized_document.json", normalized_document)
    write_json_atomic(output_path / "normalization_report.json", report)

    return {
        "normalized_document": normalized_document,
        "normalization_report": report,
        "paths": {
            "structure_plan": str(output_path / "structure_plan.json"),
            "normalized_document": str(output_path / "normalized_document.json"),
            "normalization_report": str(output_path / "normalization_report.json"),
        },
    }


def normalized_to_chapters_raw(normalized_document: dict[str, Any]) -> list[dict[str, Any]]:
    chapters_raw: list[dict[str, Any]] = []
    for chapter in normalized_document.get("chapters", []):
        title = chapter.get("title") or f"Chapter {chapter.get('order_index') or len(chapters_raw) + 1}"
        items: list[tuple[str, str]] = [("heading", str(title))]
        for block in chapter.get("blocks", []):
            text = str(block.get("text") or "")
            if text:
                items.append((block_type_for(text), text))
        chapters_raw.append({"title": title, "items": items})
    return chapters_raw


def normalized_to_document(doc_id: str, metadata: dict[str, Any], normalized_document: dict[str, Any]) -> dict[str, Any]:
    return chapters_to_document(doc_id, metadata, normalized_to_chapters_raw(normalized_document))
