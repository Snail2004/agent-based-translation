from __future__ import annotations

import copy
import hashlib
import html
import json
import os
import re
import shutil
import subprocess
import tempfile
from collections import Counter
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from .canonical_source_package import (
    CanonicalSourcePackageError,
    canonical_json_sha256,
    validate_canonical_source_package,
)
from .pdf_formula_cluster import (
    PdfFormulaClusterError,
    validate_formula_cluster,
)


OVERLAY_VERSION = "canonical_translation_overlay_v1"
EXPORT_MANIFEST_VERSION = "canonical_source_export_manifest_v1"
REVIEW_MODES = {"error", "markers"}

_OVERLAY_FIELDS = {
    "schema_version",
    "doc_id",
    "document_sha256",
    "translations",
}
_TRANSLATION_FIELDS = {"block_id", "text", "html", "markdown"}
_UNIT_POLICIES = {"translate", "preserve", "exclude", "review"}
_ASSET_TOKEN_RE = re.compile(r"\{\{asset:([A-Za-z0-9_.:-]+)\}\}")
_UNSAFE_FRAGMENT_RE = re.compile(
    r"<(?:script|style|iframe|object|embed|html|head|body)\b"
    r"|\bjavascript\s*:"
    r"|\bon[a-z]+\s*=",
    re.IGNORECASE,
)
_PROTECTED_STRUCTURED_ASSET_KINDS = {"image", "equation", "code"}
_ATX_HEADING_RE = re.compile(r"^\s{0,3}(?P<marks>#{1,6})[ \t]+(?P<title>.+?)\s*$")
_SETEXT_HEADING_RE = re.compile(r"^\s{0,3}(?P<marks>=+|-+)\s*$")
_HEADING_ATTR_RE = re.compile(r"(?P<attrs>\s*\{[^{}]*\})\s*$")
_HEADING_ID_RE = re.compile(r"#(?P<id>[A-Za-z][A-Za-z0-9_.:-]*)")
_MATHML_RE = re.compile(r"<math\b.*?</math>", re.IGNORECASE | re.DOTALL)
_FENCE_OPEN_RE = re.compile(r"^(?P<fence>`{3,}|~{3,})(?P<info>[^\r\n]*)$")
_LABEL_DIRECTIVE_RE = re.compile(
    r"^\s*:(?P<kind>label|eqlabel):`(?P<value>[^`]+)`\s*$",
    re.IGNORECASE,
)
_TAB_DIRECTIVE_RE = re.compile(
    r"^\s*:begin_tab:`(?P<tab>[^`]+)`\s*\r?\n(?P<body>.*?)"
    r"\r?\n\s*:end_tab:\s*$",
    re.IGNORECASE | re.DOTALL,
)
_SAFE_HTML_ID_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_.:-]*$")
_TRANSLATED_MATH_EXCLUDED_RE = re.compile(
    r"(?P<sphinx_role>:[A-Za-z_][A-Za-z0-9_-]*:`[^`\r\n]+`)"
    r"|(?P<inline_code>`[^`\r\n]+`)"
    r"|(?P<html_tag><[^>\r\n]+>)"
    r"|(?P<url>https?://[^\s)<]+)"
)
_INLINE_OPAQUE_TOKEN_RE = re.compile(r"\x00M(?P<index>[0-9]+)\x00")


class SourcePackageExportError(ValueError):
    pass


@dataclass(frozen=True)
class SourcePackageExportResult:
    output_dir: Path
    html_path: Path
    markdown_path: Path
    manifest_path: Path
    manifest: dict[str, Any]


@dataclass(frozen=True)
class _RenderRow:
    chapter: dict[str, Any]
    block: dict[str, Any]
    unit: dict[str, Any]
    binding: dict[str, Any]
    action: str


@dataclass(frozen=True)
class _EquationRenderBatch:
    html_by_asset_id: dict[str, str]
    candidate_asset_ids: frozenset[str]
    engine: str


@dataclass(frozen=True)
class _TranslatedMathSpan:
    start: int
    end: int
    display: str
    tex: str


@dataclass(frozen=True)
class _TranslatedMathRenderBatch:
    spans_by_block_id: dict[str, tuple[_TranslatedMathSpan, ...]]
    text_sha256_by_block_id: dict[str, str]
    mathml_by_key: dict[tuple[str, str], str]
    span_count: int
    engine: str


def _canonical_json_text(payload: Any) -> str:
    return json.dumps(
        payload,
        ensure_ascii=False,
        indent=2,
        sort_keys=True,
    ) + "\n"


def _sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _read_json_object(path: Path, *, owner: str) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise SourcePackageExportError(f"cannot read {owner}: {path}") from exc
    if not isinstance(payload, dict):
        raise SourcePackageExportError(f"{owner} must be a JSON object")
    return payload


def _require_exact_fields(
    payload: dict[str, Any],
    expected: set[str],
    *,
    owner: str,
) -> None:
    actual = set(payload)
    if actual != expected:
        raise SourcePackageExportError(
            f"{owner} fields differ; "
            f"missing={sorted(expected - actual)}, extra={sorted(actual - expected)}"
        )


def _require_nonempty_string(value: Any, *, owner: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise SourcePackageExportError(f"{owner} must be a non-empty string")
    controls = sorted(
        {
            f"U+{ord(character):04X}"
            for character in value
            if (ord(character) < 32 and character not in "\t\n\r")
            or 127 <= ord(character) <= 159
        }
    )
    if controls:
        raise SourcePackageExportError(
            f"{owner} contains forbidden control characters: {','.join(controls)}"
        )
    return value


def _block_text(block: dict[str, Any]) -> str:
    clean_text = block.get("clean_text")
    if isinstance(clean_text, str) and clean_text:
        return clean_text
    source_text = block.get("source_text")
    if isinstance(source_text, str):
        return source_text
    return ""


def _flatten_document(
    document: dict[str, Any],
) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]]]:
    chapters = document.get("chapters")
    if not isinstance(chapters, list) or not chapters:
        raise SourcePackageExportError("document.chapters must be a non-empty list")
    blocks: list[dict[str, Any]] = []
    chapter_by_block: dict[str, dict[str, Any]] = {}
    for chapter in chapters:
        if not isinstance(chapter, dict):
            raise SourcePackageExportError("document chapters must be objects")
        chapter_id = _require_nonempty_string(
            chapter.get("chapter_id"), owner="document.chapter.chapter_id"
        )
        chapter_blocks = chapter.get("blocks")
        if not isinstance(chapter_blocks, list) or not chapter_blocks:
            raise SourcePackageExportError(
                f"document chapter {chapter_id} must contain a non-empty blocks list"
            )
        for block in chapter_blocks:
            if not isinstance(block, dict):
                raise SourcePackageExportError("document blocks must be objects")
            block_id = _require_nonempty_string(
                block.get("block_id"), owner="document.block.block_id"
            )
            if block_id in chapter_by_block:
                raise SourcePackageExportError(f"duplicate block_id: {block_id}")
            chapter_by_block[block_id] = chapter
            blocks.append(block)
    return blocks, chapter_by_block


def _unit_rows(
    document: dict[str, Any], structure_manifest: dict[str, Any]
) -> dict[str, dict[str, Any]]:
    chapters = document.get("chapters")
    units = structure_manifest.get("units")
    if not isinstance(chapters, list) or not isinstance(units, list):
        raise SourcePackageExportError("document chapters and structure units are required")
    chapter_ids = [str(chapter.get("chapter_id") or "") for chapter in chapters]
    unit_chapter_ids = [str(unit.get("chapter_id") or "") for unit in units]
    if unit_chapter_ids != chapter_ids:
        raise SourcePackageExportError(
            "structure.units must map document chapters once in document order"
        )
    unit_ids: set[str] = set()
    result: dict[str, dict[str, Any]] = {}
    for index, unit in enumerate(units):
        if not isinstance(unit, dict):
            raise SourcePackageExportError("structure.units rows must be objects")
        unit_id = _require_nonempty_string(
            unit.get("unit_id"), owner=f"structure.units[{index}].unit_id"
        )
        if unit_id in unit_ids:
            raise SourcePackageExportError(f"duplicate unit_id: {unit_id}")
        unit_ids.add(unit_id)
        policy = unit.get("translation_policy")
        if policy not in _UNIT_POLICIES:
            raise SourcePackageExportError(
                f"structure.units[{index}].translation_policy is invalid"
            )
        if not isinstance(unit.get("review_required"), bool):
            raise SourcePackageExportError(
                f"structure.units[{index}].review_required must be boolean"
            )
        result[unit_chapter_ids[index]] = unit
    return result


def _effective_action(unit: dict[str, Any], binding: dict[str, Any]) -> str:
    unit_policy = str(unit["translation_policy"])
    if unit["review_required"] or unit_policy == "review":
        return "review"
    if unit_policy == "exclude":
        return "exclude"
    if unit_policy == "preserve":
        return "preserve"
    if binding["review_required"] or binding["translation_policy"] == "review":
        return "review"
    return str(binding["translation_policy"])


def _build_render_rows(
    document: dict[str, Any],
    structure_manifest: dict[str, Any],
    asset_manifest: dict[str, Any],
) -> list[_RenderRow]:
    blocks, chapter_by_block = _flatten_document(document)
    units = _unit_rows(document, structure_manifest)
    bindings = asset_manifest.get("block_bindings")
    if not isinstance(bindings, list) or len(bindings) != len(blocks):
        raise SourcePackageExportError("asset bindings do not exact-cover document blocks")
    rows: list[_RenderRow] = []
    for block, binding in zip(blocks, bindings, strict=True):
        block_id = str(block["block_id"])
        if binding.get("block_id") != block_id:
            raise SourcePackageExportError("asset bindings are not in document order")
        chapter = chapter_by_block[block_id]
        unit = units[str(chapter["chapter_id"])]
        rows.append(
            _RenderRow(
                chapter=chapter,
                block=block,
                unit=unit,
                binding=binding,
                action=_effective_action(unit, binding),
            )
        )
    return rows


def seal_translation_overlay(
    document: dict[str, Any],
    translations: list[dict[str, Any]],
) -> dict[str, Any]:
    doc_id = _require_nonempty_string(document.get("doc_id"), owner="document.doc_id")
    if not isinstance(translations, list):
        raise SourcePackageExportError("translations must be a list")
    return {
        "schema_version": OVERLAY_VERSION,
        "doc_id": doc_id,
        "document_sha256": canonical_json_sha256(document),
        "translations": copy.deepcopy(translations),
    }


def _validate_safe_fragment(value: str, *, owner: str) -> None:
    if _UNSAFE_FRAGMENT_RE.search(value):
        raise SourcePackageExportError(f"{owner} contains unsafe document-level markup")


def _protected_asset_ids(
    binding: dict[str, Any], asset_by_id: dict[str, dict[str, Any]]
) -> list[str]:
    if binding.get("semantic_kind") == "table":
        return []
    return [
        asset_id
        for asset_id in binding["asset_ids"]
        if asset_by_id[asset_id]["kind"] in _PROTECTED_STRUCTURED_ASSET_KINDS
    ]


def _validate_asset_tokens(
    fragment: str,
    expected_asset_ids: list[str],
    *,
    owner: str,
) -> None:
    counts = Counter(_ASSET_TOKEN_RE.findall(fragment))
    expected = Counter(expected_asset_ids)
    if counts != expected:
        raise SourcePackageExportError(
            f"{owner} asset placeholders differ; "
            f"expected={dict(expected)}, actual={dict(counts)}"
        )


def validate_translation_overlay(
    document: dict[str, Any],
    structure_manifest: dict[str, Any],
    asset_manifest: dict[str, Any],
    overlay: dict[str, Any],
) -> dict[str, dict[str, Any]]:
    _require_exact_fields(overlay, _OVERLAY_FIELDS, owner="translation_overlay")
    if overlay.get("schema_version") != OVERLAY_VERSION:
        raise SourcePackageExportError(
            f"translation_overlay.schema_version must be {OVERLAY_VERSION}"
        )
    if overlay.get("doc_id") != document.get("doc_id"):
        raise SourcePackageExportError("translation overlay doc_id is foreign")
    if overlay.get("document_sha256") != canonical_json_sha256(document):
        raise SourcePackageExportError("translation overlay document identity is stale")

    translations = overlay.get("translations")
    if not isinstance(translations, list):
        raise SourcePackageExportError("translation_overlay.translations must be a list")
    asset_by_id = {asset["asset_id"]: asset for asset in asset_manifest["assets"]}
    rows = _build_render_rows(document, structure_manifest, asset_manifest)
    required = {
        row.block["block_id"]: row
        for row in rows
        if row.action in {"translate", "translate_structured"}
    }
    result: dict[str, dict[str, Any]] = {}
    for index, translation in enumerate(translations):
        owner = f"translation_overlay.translations[{index}]"
        if not isinstance(translation, dict):
            raise SourcePackageExportError(f"{owner} must be an object")
        _require_exact_fields(translation, _TRANSLATION_FIELDS, owner=owner)
        block_id = _require_nonempty_string(
            translation.get("block_id"), owner=f"{owner}.block_id"
        )
        if block_id in result:
            raise SourcePackageExportError(f"duplicate translated block_id: {block_id}")
        render_row = required.get(block_id)
        if render_row is None:
            raise SourcePackageExportError(
                f"translation supplied for a non-translatable or foreign block: {block_id}"
            )
        text_value = _require_nonempty_string(
            translation.get("text"), owner=f"{owner}.text"
        )
        html_value = translation.get("html")
        markdown_value = translation.get("markdown")
        if render_row.action == "translate":
            if html_value is not None or markdown_value is not None:
                raise SourcePackageExportError(
                    f"ordinary translation {block_id} must not carry structured output"
                )
            if _ASSET_TOKEN_RE.search(text_value):
                raise SourcePackageExportError(
                    f"ordinary translation {block_id} must not carry asset placeholders"
                )
        else:
            html_value = _require_nonempty_string(
                html_value, owner=f"{owner}.html"
            )
            markdown_value = _require_nonempty_string(
                markdown_value, owner=f"{owner}.markdown"
            )
            _validate_safe_fragment(html_value, owner=f"{owner}.html")
            _validate_safe_fragment(markdown_value, owner=f"{owner}.markdown")
            expected_assets = _protected_asset_ids(render_row.binding, asset_by_id)
            _validate_asset_tokens(
                html_value,
                expected_assets,
                owner=f"{owner}.html",
            )
            _validate_asset_tokens(
                markdown_value,
                expected_assets,
                owner=f"{owner}.markdown",
            )
        result[block_id] = {
            "block_id": block_id,
            "text": text_value,
            "html": html_value,
            "markdown": markdown_value,
        }

    missing = sorted(set(required) - set(result))
    if missing:
        raise SourcePackageExportError(
            f"translation overlay is missing required blocks: {missing}"
        )
    return result


def load_translation_overlay(path: str | Path) -> dict[str, Any]:
    return _read_json_object(Path(path), owner="translation overlay")


def _collect_unresolved(
    rows: list[_RenderRow],
    asset_by_id: dict[str, dict[str, Any]],
) -> list[dict[str, str]]:
    unresolved: dict[tuple[str, str, str], dict[str, str]] = {}
    bound_asset_ids: set[str] = set()
    for row in rows:
        if row.action == "exclude":
            continue
        unit_id = str(row.unit["unit_id"])
        block_id = str(row.block["block_id"])
        if row.action == "review":
            key = ("block", block_id, "review_required")
            unresolved[key] = {
                "scope": "block",
                "id": block_id,
                "reason": "review_required",
            }
        for asset_id in row.binding["asset_ids"]:
            bound_asset_ids.add(asset_id)
            asset = asset_by_id[asset_id]
            if asset["availability"] == "materialized" and not asset["review_required"]:
                continue
            reason = (
                "asset_review_required"
                if asset["review_required"]
                else f"asset_{asset['availability']}"
            )
            key = ("asset", asset_id, reason)
            unresolved[key] = {
                "scope": "asset",
                "id": asset_id,
                "reason": reason,
                "block_id": block_id,
                "unit_id": unit_id,
            }
    for asset_id, asset in asset_by_id.items():
        if asset_id in bound_asset_ids:
            continue
        if asset["availability"] != "missing" and not asset["review_required"]:
            continue
        reason = (
            "asset_review_required"
            if asset["review_required"]
            else "asset_missing"
        )
        key = ("asset", asset_id, reason)
        unresolved[key] = {
            "scope": "asset",
            "id": asset_id,
            "reason": reason,
        }
    return sorted(
        unresolved.values(),
        key=lambda item: (item["scope"], item["id"], item["reason"]),
    )


def _asset_path(package_root: Path, asset: dict[str, Any]) -> Path:
    package_path = str(asset.get("package_path") or "")
    relative = PurePosixPath(package_path)
    if (
        asset.get("availability") != "materialized"
        or relative.is_absolute()
        or not relative.parts
        or relative.parts[0] != "assets"
        or ".." in relative.parts
    ):
        raise SourcePackageExportError(
            f"asset is not safely materialized: {asset.get('asset_id')}"
        )
    candidate = (package_root / Path(*relative.parts)).resolve()
    try:
        candidate.relative_to(package_root)
    except ValueError as exc:
        raise SourcePackageExportError("asset path escapes package root") from exc
    return candidate


def _asset_text(package_root: Path, asset: dict[str, Any]) -> str:
    try:
        return _asset_path(package_root, asset).read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise SourcePackageExportError(
            f"asset is not readable UTF-8 text: {asset.get('asset_id')}"
        ) from exc


def _is_image_media_type(media_type: str) -> bool:
    return media_type.partition(";")[0].strip().lower().startswith("image/")


def _asset_image_html(asset: dict[str, Any], *, equation: bool = False) -> str:
    metadata = asset.get("metadata") or {}
    alt_text = str(
        metadata.get("alt_text")
        or metadata.get("caption")
        or ("Formula image" if equation else "")
    )
    css_class = "source-asset source-equation-image" if equation else "source-asset"
    return (
        f'<img class="{css_class}" src="'
        + html.escape(str(asset["package_path"]), quote=True)
        + '" alt="'
        + html.escape(alt_text, quote=True)
        + '">'
    )


def _asset_image_markdown(asset: dict[str, Any], *, equation: bool = False) -> str:
    metadata = asset.get("metadata") or {}
    alt_text = str(
        metadata.get("alt_text")
        or metadata.get("caption")
        or ("Formula image" if equation else "")
    )
    return f"![{alt_text}]({asset['package_path']})"


def _resolve_local_executable(executable: str | None) -> str | None:
    if not executable:
        return None
    candidate = Path(executable)
    if candidate.is_file():
        return str(candidate.resolve())
    return shutil.which(executable)


def _tex_payload(content: str, *, media_type: str, display: str) -> str:
    stripped = content.strip()
    if "markdown" not in media_type:
        return stripped
    if display == "block":
        if stripped.startswith("$$") and stripped.endswith("$$"):
            return stripped[2:-2].strip()
        if stripped.startswith("\\[") and stripped.endswith("\\]"):
            return stripped[2:-2].strip()
    elif stripped.startswith("$") and stripped.endswith("$"):
        return stripped[1:-1].strip()
    return stripped


def _render_tex_equations_to_mathml(
    package_root: Path,
    assets: list[dict[str, Any]],
    *,
    pandoc_executable: str | None,
) -> _EquationRenderBatch:
    candidates: list[tuple[str, str, str]] = []
    for asset in sorted(assets, key=lambda item: str(item.get("asset_id") or "")):
        if (
            asset.get("kind") != "equation"
            or asset.get("availability") != "materialized"
            or asset.get("review_required")
        ):
            continue
        media_type = str(asset.get("media_type") or "")
        if _is_image_media_type(media_type):
            continue
        content = _asset_text(package_root, asset).strip()
        if "mathml" in media_type or content.lstrip().lower().startswith("<math"):
            continue
        display = "inline" if (asset.get("metadata") or {}).get("display") == "inline" else "block"
        candidates.append(
            (
                str(asset["asset_id"]),
                display,
                _tex_payload(content, media_type=media_type, display=display),
            )
        )

    candidate_ids = frozenset(asset_id for asset_id, _display, _tex in candidates)
    resolved_pandoc = _resolve_local_executable(pandoc_executable)
    if not candidates or resolved_pandoc is None:
        engine = "not_needed" if not candidates else "tex_fallback_pandoc_unavailable"
        return _EquationRenderBatch({}, candidate_ids, engine)

    key_to_asset_ids: dict[tuple[str, str], list[str]] = {}
    for asset_id, display, tex in candidates:
        key_to_asset_ids.setdefault((display, tex), []).append(asset_id)

    keys = sorted(
        key_to_asset_ids,
        key=lambda item: (item[0], _sha256_bytes(item[1].encode("utf-8")), item[1]),
    )
    source_parts: list[str] = []
    indexed_keys: list[tuple[str, str, str]] = []
    for index, (display, tex) in enumerate(keys):
        token = f"{index:06d}"
        start_marker = f"<!-- source-equation-start:{token} -->"
        end_marker = f"<!-- source-equation-end:{token} -->"
        if start_marker in tex or end_marker in tex:
            continue
        indexed_keys.append((token, display, tex))
        delimiter = "$" if display == "inline" else "$$"
        if display == "inline":
            math_source = delimiter + tex + delimiter
        else:
            math_source = delimiter + "\n" + tex + "\n" + delimiter
        source_parts.extend([start_marker, math_source, end_marker, ""])

    if not indexed_keys:
        return _EquationRenderBatch({}, candidate_ids, "tex_fallback_marker_collision")

    try:
        completed = subprocess.run(
            [
                resolved_pandoc,
                "-f",
                "markdown+tex_math_dollars",
                "-t",
                "html5",
                "--mathml",
                "--wrap=none",
            ],
            input="\n".join(source_parts),
            text=True,
            encoding="utf-8",
            capture_output=True,
            check=False,
            timeout=120,
        )
    except (OSError, subprocess.SubprocessError):
        return _EquationRenderBatch({}, candidate_ids, "tex_fallback_pandoc_failed")
    if completed.returncode != 0:
        return _EquationRenderBatch({}, candidate_ids, "tex_fallback_pandoc_failed")

    html_by_key: dict[tuple[str, str], str] = {}
    for token, display, tex in indexed_keys:
        section_match = re.search(
            rf"<!-- source-equation-start:{token} -->(.*?)"
            rf"<!-- source-equation-end:{token} -->",
            completed.stdout,
            re.DOTALL,
        )
        if section_match is None:
            continue
        math_match = _MATHML_RE.search(section_match.group(1))
        if math_match is None or _UNSAFE_FRAGMENT_RE.search(math_match.group(0)):
            continue
        wrapper = "span" if display == "inline" else "div"
        css_class = "source-equation-inline" if display == "inline" else "source-equation"
        html_by_key[(display, tex)] = (
            f'<{wrapper} class="{css_class}" data-renderer="pandoc-mathml">'
            + math_match.group(0)
            + f"</{wrapper}>"
        )

    html_by_asset_id: dict[str, str] = {}
    for key, asset_ids in key_to_asset_ids.items():
        rendered = html_by_key.get(key)
        if rendered is None:
            continue
        for asset_id in asset_ids:
            html_by_asset_id[asset_id] = rendered
    return _EquationRenderBatch(html_by_asset_id, candidate_ids, "pandoc_mathml")


def _translated_math_excluded_spans(text: str) -> list[tuple[int, int]]:
    return [
        (match.start(), match.end())
        for match in _TRANSLATED_MATH_EXCLUDED_RE.finditer(text)
    ]


def _translated_math_covering_end(
    position: int, spans: list[tuple[int, int]]
) -> int | None:
    for start, end in spans:
        if start <= position < end:
            return end
        if start > position:
            break
    return None


def _translated_math_is_escaped(text: str, position: int) -> bool:
    slashes = 0
    cursor = position - 1
    while cursor >= 0 and text[cursor] == "\\":
        slashes += 1
        cursor -= 1
    return slashes % 2 == 1


def _translated_math_find_token_close(
    text: str,
    start: int,
    token: str,
    excluded: list[tuple[int, int]],
) -> int | None:
    cursor = start
    while True:
        index = text.find(token, cursor)
        if index < 0:
            return None
        covering_end = _translated_math_covering_end(index, excluded)
        if covering_end is not None:
            cursor = covering_end
            continue
        if not _translated_math_is_escaped(text, index):
            return index
        cursor = index + len(token)


def _translated_math_find_dollar_close(
    text: str,
    start: int,
    *,
    display: bool,
    excluded: list[tuple[int, int]],
) -> int | None:
    cursor = start
    token = "$$" if display else "$"
    while cursor < len(text):
        index = text.find(token, cursor)
        if index < 0:
            return None
        covering_end = _translated_math_covering_end(index, excluded)
        if covering_end is not None:
            cursor = covering_end
            continue
        if _translated_math_is_escaped(text, index):
            cursor = index + len(token)
            continue
        if not display and text.startswith("$$", index):
            raise SourcePackageExportError(
                f"mixed $ and $$ delimiters at offset {index}"
            )
        return index
    return None


def _translated_math_looks_like_literal_dollar(text: str, position: int) -> bool:
    tail = text[position + 1 :]
    if not tail:
        return True
    if tail[0].isspace() or tail[0] in ",.;:!?)]}":
        return True
    return re.match(r"[ \t]*[0-9](?:[0-9,._]*)(?:\b|$)", tail) is not None


def _translated_math_spans(text: str) -> tuple[_TranslatedMathSpan, ...]:
    excluded = _translated_math_excluded_spans(text)
    spans: list[_TranslatedMathSpan] = []
    cursor = 0
    while cursor < len(text):
        covering_end = _translated_math_covering_end(cursor, excluded)
        if covering_end is not None:
            cursor = covering_end
            continue

        if text.startswith(r"\)", cursor) or text.startswith(r"\]", cursor):
            if not _translated_math_is_escaped(text, cursor):
                raise SourcePackageExportError(
                    f"unexpected closing math delimiter at offset {cursor}"
                )

        if text.startswith(r"\(", cursor) and not _translated_math_is_escaped(
            text, cursor
        ):
            end = _translated_math_find_token_close(
                text, cursor + 2, r"\)", excluded
            )
            if end is None:
                raise SourcePackageExportError(
                    f"unclosed \\( math delimiter at offset {cursor}"
                )
            tex = text[cursor + 2 : end]
            if not tex.strip():
                raise SourcePackageExportError(
                    f"empty \\( math delimiter at offset {cursor}"
                )
            spans.append(_TranslatedMathSpan(cursor, end + 2, "inline", tex))
            cursor = end + 2
            continue

        if text.startswith(r"\[", cursor) and not _translated_math_is_escaped(
            text, cursor
        ):
            end = _translated_math_find_token_close(
                text, cursor + 2, r"\]", excluded
            )
            if end is None:
                raise SourcePackageExportError(
                    f"unclosed \\[ math delimiter at offset {cursor}"
                )
            tex = text[cursor + 2 : end].strip()
            if not tex:
                raise SourcePackageExportError(
                    f"empty \\[ math delimiter at offset {cursor}"
                )
            spans.append(_TranslatedMathSpan(cursor, end + 2, "block", tex))
            cursor = end + 2
            continue

        if text.startswith("$$", cursor) and not _translated_math_is_escaped(
            text, cursor
        ):
            end = _translated_math_find_dollar_close(
                text, cursor + 2, display=True, excluded=excluded
            )
            if end is None:
                raise SourcePackageExportError(
                    f"unclosed $$ math delimiter at offset {cursor}"
                )
            tex = text[cursor + 2 : end].strip()
            if not tex:
                raise SourcePackageExportError(
                    f"empty $$ math delimiter at offset {cursor}"
                )
            spans.append(_TranslatedMathSpan(cursor, end + 2, "block", tex))
            cursor = end + 2
            continue

        if text[cursor] == "$" and not _translated_math_is_escaped(text, cursor):
            end = _translated_math_find_dollar_close(
                text, cursor + 1, display=False, excluded=excluded
            )
            if end is None:
                if _translated_math_looks_like_literal_dollar(text, cursor):
                    cursor += 1
                    continue
                raise SourcePackageExportError(
                    f"unclosed $ math delimiter at offset {cursor}"
                )
            tex = text[cursor + 1 : end]
            if not tex or "\n" in tex or tex != tex.strip():
                if _translated_math_looks_like_literal_dollar(text, cursor):
                    cursor += 1
                    continue
                raise SourcePackageExportError(
                    f"invalid inline math delimiter at offset {cursor}"
                )
            spans.append(_TranslatedMathSpan(cursor, end + 1, "inline", tex))
            cursor = end + 1
            continue
        cursor += 1
    return tuple(spans)


def _render_translated_math_to_mathml(
    rows: list[_RenderRow],
    translations: dict[str, dict[str, Any]],
    *,
    pandoc_executable: str | None,
) -> _TranslatedMathRenderBatch:
    row_by_block_id = {str(row.block["block_id"]): row for row in rows}
    spans_by_block_id: dict[str, tuple[_TranslatedMathSpan, ...]] = {}
    text_sha256_by_block_id: dict[str, str] = {}
    keys: set[tuple[str, str]] = set()
    span_count = 0
    for block_id, translation in translations.items():
        if translation.get("html") is not None or translation.get("markdown") is not None:
            continue
        row = row_by_block_id[block_id]
        source_kind = str(row.binding.get("source_kind") or "")
        if source_kind in {"directive", "preformatted"}:
            continue
        text = str(translation["text"])
        render_text = _plain_heading_text(text) if source_kind == "heading" else text
        try:
            spans = _translated_math_spans(render_text)
        except SourcePackageExportError as exc:
            raise SourcePackageExportError(
                f"translated math is malformed in block {block_id}: {exc}"
            ) from exc
        if not spans:
            continue
        spans_by_block_id[block_id] = spans
        text_sha256_by_block_id[block_id] = _sha256_bytes(
            render_text.encode("utf-8")
        )
        span_count += len(spans)
        keys.update((span.display, span.tex) for span in spans)

    if not keys:
        return _TranslatedMathRenderBatch({}, {}, {}, 0, "not_needed")
    resolved_pandoc = _resolve_local_executable(pandoc_executable)
    if resolved_pandoc is None:
        raise SourcePackageExportError(
            "translated math requires a local Pandoc MathML renderer"
        )

    ordered_keys = sorted(
        keys,
        key=lambda item: (item[0], _sha256_bytes(item[1].encode("utf-8")), item[1]),
    )
    source_parts: list[str] = []
    indexed_keys: list[tuple[str, str, str]] = []
    for index, (display, tex) in enumerate(ordered_keys):
        token = f"{index:06d}"
        start_marker = f"<!-- translated-math-start:{token} -->"
        end_marker = f"<!-- translated-math-end:{token} -->"
        if start_marker in tex or end_marker in tex:
            raise SourcePackageExportError(
                "translated math collides with an internal rendering marker"
            )
        indexed_keys.append((token, display, tex))
        if display == "inline":
            math_source = "$" + tex + "$"
        else:
            math_source = "$$\n" + tex + "\n$$"
        source_parts.extend([start_marker, math_source, end_marker, ""])

    try:
        completed = subprocess.run(
            [
                resolved_pandoc,
                "-f",
                "markdown+tex_math_dollars",
                "-t",
                "html5",
                "--mathml",
                "--wrap=none",
            ],
            input="\n".join(source_parts),
            text=True,
            encoding="utf-8",
            capture_output=True,
            check=False,
            timeout=120,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise SourcePackageExportError(
            "Pandoc failed while rendering translated math"
        ) from exc
    if completed.returncode != 0:
        raise SourcePackageExportError(
            "Pandoc rejected translated math; export stopped before publication"
        )

    mathml_by_key: dict[tuple[str, str], str] = {}
    for token, display, tex in indexed_keys:
        section_match = re.search(
            rf"<!-- translated-math-start:{token} -->(.*?)"
            rf"<!-- translated-math-end:{token} -->",
            completed.stdout,
            re.DOTALL,
        )
        if section_match is None:
            continue
        math_match = _MATHML_RE.search(section_match.group(1))
        if math_match is None or _UNSAFE_FRAGMENT_RE.search(math_match.group(0)):
            continue
        mathml_by_key[(display, tex)] = math_match.group(0)
    missing = set(ordered_keys) - set(mathml_by_key)
    if missing:
        missing_hashes = sorted(
            _sha256_bytes((display + "\0" + tex).encode("utf-8"))
            for display, tex in missing
        )
        raise SourcePackageExportError(
            "Pandoc could not render every translated formula; formula_sha256="
            + ",".join(missing_hashes)
        )
    return _TranslatedMathRenderBatch(
        spans_by_block_id,
        text_sha256_by_block_id,
        mathml_by_key,
        span_count,
        "pandoc_mathml",
    )


def _inline_markup_html(
    text: str,
    *,
    opaque: dict[str, str] | None = None,
) -> str:
    """Render the small safe inline-Markdown subset used by source text.

    Formula MathML is passed as opaque tokens so emphasis markers may span an
    inline formula without allowing source text to become raw HTML.
    """
    opaque = opaque or {}
    root: list[str] = []
    stack: list[tuple[str, str, list[str]]] = [("root", "", root)]

    def append(value: str) -> None:
        stack[-1][2].append(value)

    def can_open(index: int, width: int) -> bool:
        return index + width < len(text) and not text[index + width].isspace()

    def can_close(index: int) -> bool:
        return index > 0 and not text[index - 1].isspace()

    def close(kind: str) -> bool:
        if len(stack) <= 1 or stack[-1][0] != kind:
            return False
        frame_kind, _marker, content = stack.pop()
        tag = "strong" if frame_kind == "strong" else "em"
        append(f"<{tag}>{''.join(content)}</{tag}>")
        return True

    index = 0
    while index < len(text):
        token = _INLINE_OPAQUE_TOKEN_RE.match(text, index)
        if token is not None:
            marker = token.group(0)
            append(opaque.get(marker, html.escape(marker)))
            index = token.end()
            continue

        if text[index] == "\\" and index + 1 < len(text) and text[index + 1] == "*":
            append("*")
            index += 2
            continue
        if text[index] == "\\":
            append("\\")
            index += 1
            continue

        if text[index] != "*":
            start = index
            while index < len(text) and text[index] not in "*\\\x00":
                index += 1
            append(html.escape(text[start:index]))
            continue

        run_end = index
        while run_end < len(text) and text[run_end] == "*":
            run_end += 1
        width = run_end - index
        if width > 3:
            append(html.escape(text[index:run_end]))
            index = run_end
            continue

        opening = can_open(index, width)
        closing = can_close(index)
        if width == 3:
            if (
                closing
                and len(stack) >= 3
                and stack[-1][0] == "em"
                and stack[-2][0] == "strong"
            ):
                close("em")
                close("strong")
                index = run_end
                continue
            if opening:
                stack.append(("strong", "**", []))
                stack.append(("em", "*", []))
                index = run_end
                continue
        else:
            kind = "strong" if width == 2 else "em"
            if closing and close(kind):
                index = run_end
                continue
            if opening:
                stack.append((kind, "*" * width, []))
                index = run_end
                continue

        append(html.escape(text[index:run_end]))
        index = run_end

    while len(stack) > 1:
        _kind, marker, content = stack.pop()
        stack[-1][2].append(html.escape(marker) + "".join(content))
    return "".join(root)


def _translated_text_html(
    text: str,
    block_id: str,
    rendering: _TranslatedMathRenderBatch | None,
) -> str:
    if rendering is None or block_id not in rendering.spans_by_block_id:
        return _inline_markup_html(text)
    observed_sha256 = _sha256_bytes(text.encode("utf-8"))
    if observed_sha256 != rendering.text_sha256_by_block_id[block_id]:
        raise SourcePackageExportError(
            f"translated text changed after math rendering was sealed: {block_id}"
        )
    parts: list[str] = []
    opaque: dict[str, str] = {}
    cursor = 0
    for index, span in enumerate(rendering.spans_by_block_id[block_id]):
        parts.append(text[cursor : span.start])
        mathml = rendering.mathml_by_key[(span.display, span.tex)]
        css_class = (
            "source-equation-inline translated-equation"
            if span.display == "inline"
            else "source-equation translated-equation"
        )
        tex_sha256 = _sha256_bytes(span.tex.encode("utf-8"))
        marker = f"\x00M{index}\x00"
        opaque[marker] = (
            f'<span class="{css_class}" data-renderer="pandoc-mathml" '
            f'data-tex-sha256="{tex_sha256}">{mathml}</span>'
        )
        parts.append(marker)
        cursor = span.end
    parts.append(text[cursor:])
    return _inline_markup_html("".join(parts), opaque=opaque)


def _fenced_code_payload(content: str) -> tuple[str, str | None]:
    lines = content.strip("\r\n").splitlines()
    if len(lines) < 2:
        return content, None
    opening = _FENCE_OPEN_RE.match(lines[0].strip())
    if opening is None:
        return content, None
    fence = opening.group("fence")
    closing = lines[-1].strip()
    if set(closing) != {fence[0]} or len(closing) < len(fence):
        return content, None
    info = opening.group("info").strip()
    language: str | None = None
    if info.startswith("{") and info.endswith("}"):
        language = next(
            (token[1:] for token in info[1:-1].split() if token.startswith(".")),
            None,
        )
    elif info:
        language = info.split()[0]
    if language is not None and not re.fullmatch(r"[A-Za-z0-9_+.-]+", language):
        language = None
    return "\n".join(lines[1:-1]), language


def _directive_html(text: str) -> str:
    label = _LABEL_DIRECTIVE_RE.fullmatch(text)
    if label is not None:
        value = label.group("value").strip()
        id_attr = (
            f' id="{html.escape(value, quote=True)}"'
            if _SAFE_HTML_ID_RE.fullmatch(value)
            else ""
        )
        return (
            '<span class="source-anchor" aria-hidden="true"'
            + id_attr
            + ' data-directive="'
            + html.escape(label.group("kind").lower(), quote=True)
            + '"></span>'
        )
    tab = _TAB_DIRECTIVE_RE.fullmatch(text)
    if tab is not None:
        body = html.escape(tab.group("body").strip()).replace("\n", "<br>\n")
        return (
            '<div class="source-tab" data-tab="'
            + html.escape(tab.group("tab").strip(), quote=True)
            + '"><p>'
            + body
            + "</p></div>"
        )
    return '<pre class="source-directive">' + html.escape(text) + "</pre>"


def _review_marker_html(identifier: str, reason: str) -> str:
    return (
        '<aside class="review-required" data-review-id="'
        + html.escape(identifier, quote=True)
        + '"><strong>Review required:</strong> '
        + html.escape(reason)
        + "</aside>"
    )


def _review_marker_markdown(identifier: str, reason: str) -> str:
    return f"> [!WARNING]\n> Review required for `{identifier}`: {reason}"


def _asset_html(
    package_root: Path,
    asset: dict[str, Any],
    *,
    marker_mode: bool,
) -> str:
    asset_id = str(asset["asset_id"])
    if asset["availability"] != "materialized" or asset["review_required"]:
        if marker_mode:
            return _review_marker_html(asset_id, "asset is not safely materialized")
        raise SourcePackageExportError(f"asset cannot be rendered: {asset_id}")
    kind = str(asset["kind"])
    media_type = str(asset["media_type"])
    metadata = asset.get("metadata") or {}
    if kind == "image":
        return _asset_image_html(asset)
    if kind == "equation" and _is_image_media_type(media_type):
        return _asset_image_html(asset, equation=True)
    content = _asset_text(package_root, asset)
    lowered = content.lstrip().lower()
    if kind == "equation":
        if lowered.startswith("<math") or "mathml" in media_type:
            if _UNSAFE_FRAGMENT_RE.search(content):
                if marker_mode:
                    return _review_marker_html(asset_id, "unsafe source equation fragment")
                raise SourcePackageExportError(
                    f"asset contains unsafe source markup: {asset_id}"
                )
            return content
        rendered_mathml = asset.get("_rendered_mathml")
        if isinstance(rendered_mathml, str):
            return rendered_mathml
        stripped = content.strip()
        if metadata.get("display") == "inline":
            return (
                '<span class="source-equation-inline tex-fallback" '
                'data-render-status="tex-fallback"><code>$'
                + html.escape(stripped)
                + "$</code></span>"
            )
        return (
            '<pre class="source-equation tex-fallback" '
            'data-render-status="tex-fallback">'
            + html.escape(stripped)
            + "</pre>"
        )
    if kind == "code":
        if "html" in media_type and lowered.startswith("<"):
            return content
        code, language = _fenced_code_payload(content)
        language_class = (
            f' class="language-{html.escape(language, quote=True)}"'
            if language
            else ""
        )
        return (
            '<pre class="source-code"><code'
            + language_class
            + ">"
            + html.escape(code)
            + "</code></pre>"
        )
    if kind in {"table", "raw_fragment"}:
        if "html" in media_type or "xhtml" in media_type:
            if _UNSAFE_FRAGMENT_RE.search(content):
                if marker_mode:
                    return _review_marker_html(asset_id, "unsafe source HTML fragment")
                raise SourcePackageExportError(
                    f"asset contains unsafe source markup: {asset_id}"
                )
            return content
        css_class = "source-table" if kind == "table" else "source-fragment"
        return f'<pre class="{css_class}">' + html.escape(content) + "</pre>"
    return ""


def _asset_markdown(
    package_root: Path,
    asset: dict[str, Any],
    *,
    marker_mode: bool,
) -> str:
    asset_id = str(asset["asset_id"])
    if asset["availability"] != "materialized" or asset["review_required"]:
        if marker_mode:
            return _review_marker_markdown(
                asset_id, "asset is not safely materialized"
            )
        raise SourcePackageExportError(f"asset cannot be rendered: {asset_id}")
    kind = str(asset["kind"])
    media_type = str(asset["media_type"])
    metadata = asset.get("metadata") or {}
    if kind == "image":
        return _asset_image_markdown(asset)
    if kind == "equation" and _is_image_media_type(media_type):
        return _asset_image_markdown(asset, equation=True)
    content = _asset_text(package_root, asset)
    if kind in {"equation", "table", "raw_fragment"} and _UNSAFE_FRAGMENT_RE.search(
        content
    ):
        if marker_mode:
            return _review_marker_markdown(asset_id, "unsafe source markup")
        raise SourcePackageExportError(
            f"asset contains unsafe source markup: {asset_id}"
        )
    if kind == "equation":
        if "mathml" in media_type or content.lstrip().lower().startswith("<math"):
            return content
        stripped = content.strip()
        if "markdown" in media_type and stripped.startswith("$$") and stripped.endswith("$$"):
            return stripped
        if metadata.get("display") == "inline":
            return "$" + stripped + "$"
        return "$$\n" + stripped + "\n$$"
    if kind == "code":
        if "markdown" in media_type and content.lstrip().startswith("```"):
            return content
        return "```\n" + content.rstrip() + "\n```"
    return content


def _replace_asset_tokens(
    fragment: str,
    *,
    package_root: Path,
    asset_by_id: dict[str, dict[str, Any]],
    output_format: str,
    marker_mode: bool,
) -> str:
    def replace(match: re.Match[str]) -> str:
        asset = asset_by_id[match.group(1)]
        if output_format == "html":
            return _asset_html(package_root, asset, marker_mode=marker_mode)
        return _asset_markdown(package_root, asset, marker_mode=marker_mode)

    return _ASSET_TOKEN_RE.sub(replace, fragment)


def _primary_preserved_assets(
    binding: dict[str, Any], asset_by_id: dict[str, dict[str, Any]]
) -> list[dict[str, Any]]:
    preferred_kind = {
        "image": "image",
        "equation": "equation",
        "code": "code",
        "table": "table",
        "structural": "raw_fragment",
    }.get(str(binding["semantic_kind"]))
    if preferred_kind is None:
        return []
    return [
        asset_by_id[asset_id]
        for asset_id in binding["asset_ids"]
        if asset_by_id[asset_id]["kind"] == preferred_kind
    ]


def _formula_cluster_member(
    row: _RenderRow,
    asset_by_id: dict[str, dict[str, Any]],
) -> tuple[dict[str, Any], str, dict[str, Any]] | None:
    matches: list[tuple[dict[str, Any], str, dict[str, Any]]] = []
    for asset_id in row.binding["asset_ids"]:
        asset = asset_by_id[asset_id]
        metadata = asset.get("metadata")
        formula_detection = (
            metadata.get("formula_detection") if isinstance(metadata, dict) else None
        )
        cluster = (
            formula_detection.get("formula_cluster")
            if isinstance(formula_detection, dict)
            else None
        )
        role = (
            formula_detection.get("cluster_member_role")
            if isinstance(formula_detection, dict)
            else None
        )
        if cluster is not None or role is not None:
            if not isinstance(cluster, dict) or not isinstance(role, str):
                raise SourcePackageExportError(
                    "formula cluster metadata is incomplete"
                )
            matches.append((cluster, role, asset))
    if not matches:
        return None
    if len(matches) != 1:
        raise SourcePackageExportError(
            f"formula cluster block must bind one visual asset: {row.block['block_id']}"
        )
    return matches[0]


def _formula_region_ids(formula_detection: dict[str, Any]) -> list[str]:
    if isinstance(formula_detection.get("region"), dict):
        region_id = formula_detection["region"].get("region_id")
        return [str(region_id)] if isinstance(region_id, str) and region_id else []
    regions = formula_detection.get("regions")
    if not isinstance(regions, list):
        return []
    return [
        str(region["region_id"])
        for region in regions
        if isinstance(region, dict)
        and isinstance(region.get("region_id"), str)
        and region["region_id"]
    ]


def _formula_detector_region_index(
    structure: dict[str, Any],
) -> tuple[dict[str, dict[str, Any]], float]:
    detector = structure.get("formula_detection")
    if not isinstance(detector, dict) or detector.get("status") != "completed":
        return {}, 0.0
    regions = detector.get("regions")
    postprocessing = detector.get("postprocessing")
    if not isinstance(regions, list) or not isinstance(postprocessing, dict):
        raise SourcePackageExportError("formula detector manifest is incomplete")
    threshold = postprocessing.get("acceptance_threshold")
    if not isinstance(threshold, (int, float)) or isinstance(threshold, bool):
        raise SourcePackageExportError(
            "formula detector acceptance threshold is invalid"
        )
    by_id: dict[str, dict[str, Any]] = {}
    for region in regions:
        if not isinstance(region, dict):
            raise SourcePackageExportError("formula detector region is invalid")
        region_id = region.get("region_id")
        if (
            not isinstance(region_id, str)
            or not region_id
            or region_id in by_id
        ):
            raise SourcePackageExportError(
                "formula detector region identity is invalid"
            )
        by_id[region_id] = region
    return by_id, float(threshold)


def _formula_detection_regions(formula_detection: dict[str, Any]) -> list[dict[str, Any]]:
    region = formula_detection.get("region")
    if isinstance(region, dict):
        return [region]
    regions = formula_detection.get("regions")
    if isinstance(regions, list) and all(isinstance(item, dict) for item in regions):
        return regions
    return []


def _validate_formula_clusters(
    rows: list[_RenderRow],
    asset_by_id: dict[str, dict[str, Any]],
    *,
    document: dict[str, Any],
    structure: dict[str, Any],
    source_sha256: str,
) -> dict[str, tuple[dict[str, Any], str, dict[str, Any]]]:
    detector_regions, acceptance_threshold = _formula_detector_region_index(
        structure
    )
    by_block: dict[str, tuple[dict[str, Any], str, dict[str, Any]]] = {}
    by_cluster: dict[str, list[str]] = {}
    records: dict[str, dict[str, Any]] = {}
    for row in rows:
        member = _formula_cluster_member(row, asset_by_id)
        if member is None:
            continue
        cluster, role, asset = member
        try:
            validated = validate_formula_cluster(cluster)
        except PdfFormulaClusterError as exc:
            raise SourcePackageExportError(str(exc)) from exc
        if (
            validated["doc_id"] != document["doc_id"]
            or validated["source_sha256"] != source_sha256
            or validated["normalizer_version"] != structure.get("normalizer_version")
        ):
            raise SourcePackageExportError(
                "formula cluster package or source identity mismatch"
            )
        block_id = str(row.block["block_id"])
        member_rows = {
            str(item["block_id"]): item for item in validated["members"]
        }
        expected_member = member_rows.get(block_id)
        if expected_member is None or expected_member["role"] != role:
            raise SourcePackageExportError(
                "formula cluster member ownership mismatch"
            )
        formula_detection = asset["metadata"]["formula_detection"]
        if _formula_region_ids(formula_detection) != expected_member[
            "detector_region_ids"
        ]:
            raise SourcePackageExportError(
                "formula cluster member detector regions mismatch"
            )
        embedded_regions = _formula_detection_regions(formula_detection)
        if not embedded_regions or any(
            detector_regions.get(str(region.get("region_id"))) != region
            for region in embedded_regions
        ):
            raise SourcePackageExportError(
                "formula cluster detector evidence mismatch"
            )
        if role == "duplicate_evidence" and (
            len(embedded_regions) != 1
            or embedded_regions[0].get("bbox_pdf") != expected_member["bbox_pdf"]
        ):
            raise SourcePackageExportError(
                "formula cluster duplicate evidence geometry mismatch"
            )
        locator = asset.get("source_locator") or {}
        if (
            row.action != "preserve"
            or asset.get("kind") != "equation"
            or asset.get("availability") != "materialized"
            or asset.get("review_required")
            or locator.get("block_id") != block_id
            or locator.get("page_number") != validated["page_number"]
            or locator.get("bbox_pdf") != expected_member["bbox_pdf"]
        ):
            raise SourcePackageExportError(
                "formula cluster member is not safe for preserved publication"
            )
        cluster_id = validated["formula_cluster_id"]
        existing = records.setdefault(cluster_id, validated)
        if existing != validated:
            raise SourcePackageExportError("formula cluster records disagree")
        by_cluster.setdefault(cluster_id, []).append(block_id)
        by_block[block_id] = (validated, role, asset)

    for cluster_id, actual_block_ids in by_cluster.items():
        cluster = records[cluster_id]
        selected_regions = [
            detector_regions.get(region_id)
            for region_id in cluster["detector_region_ids"]
        ]
        if any(region is None for region in selected_regions):
            raise SourcePackageExportError(
                "formula cluster references a foreign detector region"
            )
        labels = [str(region["label"]) for region in selected_regions]
        confidences = [float(region["confidence"]) for region in selected_regions]
        pages = [int(region["page_number"]) for region in selected_regions]
        if (
            labels.count("isolate_formula") != 1
            or labels.count("formula_caption") > 1
            or any(
                label not in {"isolate_formula", "formula_caption"}
                for label in labels
            )
            or any(confidence < acceptance_threshold for confidence in confidences)
            or any(page != cluster["page_number"] for page in pages)
        ):
            raise SourcePackageExportError(
                "formula cluster detector evidence does not satisfy closed gates"
            )
        expected_block_ids = [
            str(member["block_id"]) for member in cluster["members"]
        ]
        if actual_block_ids != expected_block_ids:
            raise SourcePackageExportError(
                "formula cluster members are missing, duplicated, or reordered"
            )
        publication_block_id = cluster["publication_block_id"]
        publication = by_block[publication_block_id]
        if (
            publication[1] != "publication_visual"
            or publication[2]["source_locator"].get("bbox_pdf")
            != cluster["publication_bbox_pdf"]
        ):
            raise SourcePackageExportError(
                "formula cluster publication visual identity mismatch"
            )
    return by_block


def _heading_metadata(source_text: str) -> tuple[int, str, str | None]:
    lines = source_text.strip().splitlines()
    level = 2
    title_line = lines[0].strip() if lines else ""
    atx = _ATX_HEADING_RE.match(title_line)
    if atx:
        level = len(atx.group("marks"))
        title_line = re.sub(r"\s+#+\s*$", "", atx.group("title")).strip()
    elif len(lines) >= 2 and (setext := _SETEXT_HEADING_RE.match(lines[1])):
        level = 1 if setext.group("marks").startswith("=") else 2
    attr_match = _HEADING_ATTR_RE.search(title_line)
    attrs = attr_match.group("attrs").strip() if attr_match else ""
    anchor_match = _HEADING_ID_RE.search(attrs)
    anchor = anchor_match.group("id") if anchor_match else None
    return level, attrs, anchor


def _plain_heading_text(text: str) -> str:
    lines = text.strip().splitlines()
    if not lines:
        return ""
    value = lines[0].strip()
    atx = _ATX_HEADING_RE.match(value)
    if atx:
        value = re.sub(r"\s+#+\s*$", "", atx.group("title")).strip()
    value = _HEADING_ATTR_RE.sub("", value).strip()
    return value


def _text_html(
    text: str,
    source_kind: str,
    *,
    source_text: str = "",
    block_id: str = "",
    translated_math: _TranslatedMathRenderBatch | None = None,
) -> str:
    if source_kind == "heading":
        level, _attrs, anchor = _heading_metadata(source_text)
        id_attr = f' id="{html.escape(anchor, quote=True)}"' if anchor else ""
        heading_html = _translated_text_html(
            _plain_heading_text(text), block_id, translated_math
        )
        return f"<h{level}{id_attr}>{heading_html}</h{level}>"
    if source_kind == "directive":
        return _directive_html(text)
    escaped = _translated_text_html(text, block_id, translated_math)
    if source_kind in {"dialogue", "block_quote"}:
        return f"<blockquote>{escaped}</blockquote>"
    if source_kind in {"list", "list_item"}:
        return f'<p class="source-list-item">{escaped}</p>'
    if source_kind == "verse":
        return (
            '<p class="source-verse">'
            + escaped.replace("\n", "<br>\n")
            + "</p>"
        )
    if source_kind == "preformatted":
        return f'<pre class="source-preformatted">{escaped}</pre>'
    return f"<p>{escaped}</p>"


def _text_markdown(text: str, source_kind: str, *, source_text: str = "") -> str:
    if source_kind == "heading":
        level, attrs, _anchor = _heading_metadata(source_text)
        suffix = f" {attrs}" if attrs else ""
        return "#" * level + " " + _plain_heading_text(text) + suffix
    if source_kind in {"dialogue", "block_quote"}:
        return "\n".join("> " + line for line in text.splitlines())
    if source_kind == "verse":
        return "  \n".join(text.splitlines())
    if source_kind == "preformatted":
        return "<pre>\n" + text + "\n</pre>"
    return text


def _render_block_html(
    row: _RenderRow,
    translation: dict[str, Any] | None,
    *,
    package_root: Path,
    asset_by_id: dict[str, dict[str, Any]],
    marker_mode: bool,
    translated_math: _TranslatedMathRenderBatch | None,
) -> str:
    block_id = str(row.block["block_id"])
    formula_member = _formula_cluster_member(row, asset_by_id)
    attrs = (
        f'data-block-id="{html.escape(block_id, quote=True)}" '
        f'data-policy="{html.escape(row.action, quote=True)}"'
    )
    if formula_member is not None:
        cluster, role, _asset = formula_member
        attrs += (
            f' data-formula-cluster-id="{html.escape(cluster["formula_cluster_id"], quote=True)}"'
            f' data-formula-cluster-role="{html.escape(role, quote=True)}"'
        )
        if role == "duplicate_evidence":
            return (
                f'<div class="source-block formula-cluster-duplicate" {attrs} hidden>'
                "<!-- duplicate formula evidence retained in package -->"
                "</div>"
            )
    if row.action == "review":
        body = _review_marker_html(block_id, "block or unit policy requires review")
    elif row.action == "translate":
        if translation is None:
            raise SourcePackageExportError(f"missing translated block: {block_id}")
        body = _text_html(
            str(translation["text"]),
            str(row.binding["source_kind"]),
            source_text=str(row.block.get("source_text") or ""),
            block_id=block_id,
            translated_math=translated_math,
        )
    elif row.action == "translate_structured":
        if translation is None or not isinstance(translation["html"], str):
            raise SourcePackageExportError(
                f"missing structured HTML translation: {block_id}"
            )
        body = _replace_asset_tokens(
            translation["html"],
            package_root=package_root,
            asset_by_id=asset_by_id,
            output_format="html",
            marker_mode=marker_mode,
        )
    else:
        assets = _primary_preserved_assets(row.binding, asset_by_id)
        if assets:
            body = "\n".join(
                _asset_html(package_root, asset, marker_mode=marker_mode)
                for asset in assets
            )
        else:
            body = _text_html(
                _block_text(row.block),
                str(row.binding["source_kind"]),
                source_text=str(row.block.get("source_text") or ""),
            )
    return f'<div class="source-block" {attrs}>\n{body}\n</div>'


def _render_block_markdown(
    row: _RenderRow,
    translation: dict[str, Any] | None,
    *,
    package_root: Path,
    asset_by_id: dict[str, dict[str, Any]],
    marker_mode: bool,
) -> str:
    block_id = str(row.block["block_id"])
    prefix = f"<!-- block_id={block_id} policy={row.action} -->"
    formula_member = _formula_cluster_member(row, asset_by_id)
    if formula_member is not None:
        cluster, role, _asset = formula_member
        prefix += (
            f"\n<!-- formula_cluster_id={cluster['formula_cluster_id']} role={role} -->"
        )
        if role == "duplicate_evidence":
            return prefix + "\n<!-- duplicate formula evidence retained in package -->"
    if row.action == "review":
        body = _review_marker_markdown(block_id, "block or unit policy requires review")
    elif row.action == "translate":
        if translation is None:
            raise SourcePackageExportError(f"missing translated block: {block_id}")
        body = _text_markdown(
            str(translation["text"]),
            str(row.binding["source_kind"]),
            source_text=str(row.block.get("source_text") or ""),
        )
    elif row.action == "translate_structured":
        if translation is None or not isinstance(translation["markdown"], str):
            raise SourcePackageExportError(
                f"missing structured Markdown translation: {block_id}"
            )
        body = _replace_asset_tokens(
            translation["markdown"],
            package_root=package_root,
            asset_by_id=asset_by_id,
            output_format="markdown",
            marker_mode=marker_mode,
        )
    else:
        assets = _primary_preserved_assets(row.binding, asset_by_id)
        if assets:
            body = "\n\n".join(
                _asset_markdown(package_root, asset, marker_mode=marker_mode)
                for asset in assets
            )
        else:
            body = _text_markdown(
                _block_text(row.block),
                str(row.binding["source_kind"]),
                source_text=str(row.block.get("source_text") or ""),
            )
    return prefix + "\n\n" + body


def _render_documents(
    document: dict[str, Any],
    rows: list[_RenderRow],
    translations: dict[str, dict[str, Any]],
    *,
    package_root: Path,
    asset_by_id: dict[str, dict[str, Any]],
    marker_mode: bool,
    translated_math: _TranslatedMathRenderBatch | None,
) -> tuple[str, str, dict[str, int]]:
    metadata = document.get("metadata") or {}
    title = str(metadata.get("title") or document.get("doc_id") or "Translated document")
    target_language = str(metadata.get("target_language") or "vi")
    html_parts = [
        "<!doctype html>",
        f'<html lang="{html.escape(target_language, quote=True)}">',
        "<head>",
        '<meta charset="utf-8">',
        f"<title>{html.escape(title)}</title>",
        "<style>",
        "body{font-family:system-ui,sans-serif;line-height:1.6;max-width:76rem;"
        "margin:0 auto;padding:2rem;}",
        ".source-unit{margin:0 0 2rem}.source-block{margin:.75rem 0}"
        ".source-asset{display:block;max-width:100%;height:auto;margin:1.5rem auto;}",
        ".source-verse{white-space:normal;margin:1rem 0 1rem 2rem;}",
        ".source-equation{display:block;overflow-x:auto;margin:1rem 0;padding:.5rem 0;"
        "text-align:center}.source-equation math[display=block]{margin:0 auto}",
        ".source-equation-inline{display:inline-block;max-width:100%;vertical-align:baseline}",
        ".tex-fallback{border-left:3px solid #b54708;background:#fffaeb;"
        "text-align:left}.source-anchor{display:block;position:relative;top:-1rem;"
        "height:0;overflow:hidden}.source-tab{border-left:3px solid #d0d5dd;"
        "padding-left:1rem}.source-tab p{margin:.5rem 0}",
        ".review-required{border:2px solid #b42318;background:#fef3f2;"
        "padding:.75rem;color:#7a271a;}",
        "pre{overflow:auto;padding:.75rem;background:#f6f8fa;}"
        "table{border-collapse:collapse;}td,th{border:1px solid #bbb;padding:.35rem;}",
        "</style>",
        "</head>",
        f'<body><main data-doc-id="{html.escape(str(document["doc_id"]), quote=True)}">',
    ]
    markdown_parts = [
        f"<!-- doc_id={document['doc_id']} -->",
        f"<!-- title={title} -->",
    ]
    counts = {
        "chapters_rendered": 0,
        "chapters_excluded": 0,
        "blocks_translated": 0,
        "blocks_structured": 0,
        "blocks_preserved": 0,
        "blocks_excluded": 0,
        "review_markers": 0,
        "formula_cluster_visuals": 0,
        "formula_cluster_duplicate_rows_suppressed": 0,
    }
    rows_by_chapter: dict[str, list[_RenderRow]] = {}
    for row in rows:
        rows_by_chapter.setdefault(str(row.chapter["chapter_id"]), []).append(row)
    for chapter in document["chapters"]:
        chapter_id = str(chapter["chapter_id"])
        chapter_rows = rows_by_chapter[chapter_id]
        if all(row.action == "exclude" for row in chapter_rows):
            counts["chapters_excluded"] += 1
            counts["blocks_excluded"] += len(chapter_rows)
            continue
        counts["chapters_rendered"] += 1
        unit = chapter_rows[0].unit
        unit_id = str(unit["unit_id"])
        html_parts.append(
            '<section class="source-unit" '
            f'data-unit-id="{html.escape(unit_id, quote=True)}" '
            f'data-chapter-id="{html.escape(chapter_id, quote=True)}">'
        )
        markdown_parts.append(
            f"<!-- unit_id={unit_id} chapter_id={chapter_id} "
            f"policy={unit['translation_policy']} -->"
        )
        for row in chapter_rows:
            if row.action == "exclude":
                counts["blocks_excluded"] += 1
                continue
            if row.action == "translate":
                counts["blocks_translated"] += 1
            elif row.action == "translate_structured":
                counts["blocks_structured"] += 1
            elif row.action == "preserve":
                counts["blocks_preserved"] += 1
            elif row.action == "review":
                counts["review_markers"] += 1
            formula_member = _formula_cluster_member(row, asset_by_id)
            if formula_member is not None:
                if formula_member[1] == "publication_visual":
                    counts["formula_cluster_visuals"] += 1
                else:
                    counts["formula_cluster_duplicate_rows_suppressed"] += 1
            block_id = str(row.block["block_id"])
            translation = translations.get(block_id)
            html_parts.append(
                _render_block_html(
                    row,
                    translation,
                    package_root=package_root,
                    asset_by_id=asset_by_id,
                    marker_mode=marker_mode,
                    translated_math=translated_math,
                )
            )
            markdown_parts.append(
                _render_block_markdown(
                    row,
                    translation,
                    package_root=package_root,
                    asset_by_id=asset_by_id,
                    marker_mode=marker_mode,
                )
            )
        html_parts.append("</section>")
    html_parts.extend(["</main></body>", "</html>"])
    return "\n".join(html_parts) + "\n", "\n\n".join(markdown_parts) + "\n", counts


def _copy_materialized_assets(
    package_root: Path,
    temporary_output: Path,
    assets: list[dict[str, Any]],
) -> list[dict[str, str]]:
    copied_by_path: dict[str, dict[str, str]] = {}
    for asset in sorted(assets, key=lambda item: str(item.get("package_path") or "")):
        if asset["availability"] != "materialized":
            continue
        source = _asset_path(package_root, asset)
        relative = PurePosixPath(str(asset["package_path"]))
        destination = temporary_output / Path(*relative.parts)
        destination.parent.mkdir(parents=True, exist_ok=True)
        payload = source.read_bytes()
        if _sha256_bytes(payload) != asset["sha256"]:
            raise SourcePackageExportError(
                f"asset changed while exporting: {asset['asset_id']}"
            )
        relative_path = relative.as_posix()
        existing = copied_by_path.get(relative_path)
        if existing is not None:
            if existing["sha256"] != asset["sha256"]:
                raise SourcePackageExportError(
                    f"assets collide at one package path: {relative_path}"
                )
            continue
        destination.write_bytes(payload)
        copied_by_path[relative_path] = {
            "path": relative_path,
            "sha256": asset["sha256"],
        }
    return [copied_by_path[path] for path in sorted(copied_by_path)]


def export_source_package(
    package_root: str | Path,
    translation_overlay: dict[str, Any] | str | Path,
    output_dir: str | Path,
    *,
    review_mode: str = "error",
    pandoc_executable: str | None = "pandoc",
) -> SourcePackageExportResult:
    if review_mode not in REVIEW_MODES:
        raise SourcePackageExportError(
            f"review_mode must be one of {sorted(REVIEW_MODES)}"
        )
    package_root_path = Path(package_root).resolve()
    output_path = Path(output_dir).resolve()
    try:
        output_path.relative_to(package_root_path)
    except ValueError:
        pass
    else:
        raise SourcePackageExportError(
            "output directory must not live inside the canonical source package"
        )
    if output_path.exists():
        raise SourcePackageExportError(f"output directory already exists: {output_path}")
    document = _read_json_object(package_root_path / "document.json", owner="document")
    structure = _read_json_object(
        package_root_path / "structure_manifest.json", owner="structure manifest"
    )
    asset_manifest = _read_json_object(
        package_root_path / "asset_manifest.json", owner="asset manifest"
    )
    try:
        validation_report = validate_canonical_source_package(
            document,
            structure,
            asset_manifest,
            package_root=package_root_path,
        )
    except CanonicalSourcePackageError as exc:
        raise SourcePackageExportError(str(exc)) from exc
    overlay = (
        load_translation_overlay(translation_overlay)
        if isinstance(translation_overlay, (str, Path))
        else copy.deepcopy(translation_overlay)
    )
    if not isinstance(overlay, dict):
        raise SourcePackageExportError("translation overlay must be an object")
    rows = _build_render_rows(document, structure, asset_manifest)
    asset_by_id = {
        asset["asset_id"]: copy.deepcopy(asset) for asset in asset_manifest["assets"]
    }
    _validate_formula_clusters(
        rows,
        asset_by_id,
        document=document,
        structure=structure,
        source_sha256=str(asset_manifest["source"]["sha256"]),
    )
    unresolved = _collect_unresolved(rows, asset_by_id)
    if unresolved and review_mode == "error":
        raise SourcePackageExportError(
            "source package contains unresolved review rows: "
            + json.dumps(unresolved, ensure_ascii=False, sort_keys=True)
        )
    translations = validate_translation_overlay(
        document, structure, asset_manifest, overlay
    )
    equation_rendering = _render_tex_equations_to_mathml(
        package_root_path,
        asset_manifest["assets"],
        pandoc_executable=pandoc_executable,
    )
    translated_math_rendering = _render_translated_math_to_mathml(
        rows,
        translations,
        pandoc_executable=pandoc_executable,
    )
    for asset_id, rendered_mathml in equation_rendering.html_by_asset_id.items():
        asset_by_id[asset_id]["_rendered_mathml"] = rendered_mathml
    html_text, markdown_text, counts = _render_documents(
        document,
        rows,
        translations,
        package_root=package_root_path,
        asset_by_id=asset_by_id,
        marker_mode=review_mode == "markers",
        translated_math=translated_math_rendering,
    )
    counts["equation_assets_mathml"] = len(equation_rendering.html_by_asset_id)
    counts["equation_assets_tex_fallback"] = len(
        equation_rendering.candidate_asset_ids
        - equation_rendering.html_by_asset_id.keys()
    )
    counts["translated_math_spans"] = translated_math_rendering.span_count
    counts["translated_mathml_spans"] = translated_math_rendering.span_count
    counts["translated_math_unique_expressions"] = len(
        translated_math_rendering.mathml_by_key
    )

    output_path.parent.mkdir(parents=True, exist_ok=True)
    temporary_output = Path(
        tempfile.mkdtemp(prefix=f".{output_path.name}.", dir=output_path.parent)
    )
    try:
        copied_assets = _copy_materialized_assets(
            package_root_path, temporary_output, asset_manifest["assets"]
        )
        html_bytes = html_text.encode("utf-8")
        markdown_bytes = markdown_text.encode("utf-8")
        (temporary_output / "document.html").write_bytes(html_bytes)
        (temporary_output / "document.md").write_bytes(markdown_bytes)
        counts["assets_copied"] = len(copied_assets)
        manifest: dict[str, Any] = {
            "schema_version": EXPORT_MANIFEST_VERSION,
            "doc_id": document["doc_id"],
            "review_mode": review_mode,
            "rendering": {
                "equations": {
                    "engine": equation_rendering.engine,
                    "mathml_assets": counts["equation_assets_mathml"],
                    "tex_fallback_assets": counts["equation_assets_tex_fallback"],
                },
                "translated_text_math": {
                    "engine": translated_math_rendering.engine,
                    "mathml_spans": counts["translated_mathml_spans"],
                    "unique_expressions": counts[
                        "translated_math_unique_expressions"
                    ],
                }
            },
            "package": {
                "source_sha256": asset_manifest["source"]["sha256"],
                "document_sha256": canonical_json_sha256(document),
                "structure_sha256": canonical_json_sha256(structure),
                "asset_manifest_payload_sha256": asset_manifest["integrity"][
                    "manifest_payload_sha256"
                ],
                "validation_status": validation_report["status"],
            },
            "translation_overlay_sha256": canonical_json_sha256(overlay),
            "artifacts": {
                "html": {
                    "path": "document.html",
                    "sha256": _sha256_bytes(html_bytes),
                },
                "markdown": {
                    "path": "document.md",
                    "sha256": _sha256_bytes(markdown_bytes),
                },
            },
            "assets": copied_assets,
            "counts": counts,
            "unresolved": unresolved,
        }
        manifest["integrity"] = {
            "export_payload_sha256": canonical_json_sha256(manifest)
        }
        (temporary_output / "export_manifest.json").write_text(
            _canonical_json_text(manifest), encoding="utf-8", newline="\n"
        )
        os.replace(temporary_output, output_path)
    except Exception:
        shutil.rmtree(temporary_output, ignore_errors=True)
        raise
    return SourcePackageExportResult(
        output_dir=output_path,
        html_path=output_path / "document.html",
        markdown_path=output_path / "document.md",
        manifest_path=output_path / "export_manifest.json",
        manifest=manifest,
    )


__all__ = [
    "EXPORT_MANIFEST_VERSION",
    "OVERLAY_VERSION",
    "REVIEW_MODES",
    "SourcePackageExportError",
    "SourcePackageExportResult",
    "export_source_package",
    "load_translation_overlay",
    "seal_translation_overlay",
    "validate_translation_overlay",
]
