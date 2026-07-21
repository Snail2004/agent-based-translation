from __future__ import annotations

import hashlib
import json
import re
import subprocess
import unicodedata
from collections import Counter
from dataclasses import dataclass, replace
from pathlib import Path, PurePosixPath
from typing import Any, Iterable, Iterator, Sequence
import zipfile
from xml.etree import ElementTree as ET

from pipeline.ingest.epub_package import (
    EpubNavEntry,
    EpubPackageIndex,
    EpubTarget,
    read_epub_package,
)
from pipeline.ingest.document_contract import runtime_block_type
from pipeline.ingest.normalization_adapters import (
    AdapterUnavailableError,
    _inline_text,
    _nested_pandoc_blocks,
    _pandoc_blocks_text,
)
from pipeline.ingest.normalization_ir import normalize_kind, normalize_text
from pipeline.ingest.source_package_materializer import materialize_source_package


NORMALIZER_VERSION = "epub_hybrid_normalizer_v1"
MANIFEST_SCHEMA_VERSION = "epub_structure_manifest_v1"
DOCUMENT_SCHEMA_VERSION = "1.5.0"

UNIT_ROLES = {
    "front_matter",
    "content_unit",
    "container",
    "back_matter",
    "unknown",
}
TRANSLATION_POLICIES = {"translate", "preserve", "exclude", "review"}

_FRONT_TOKENS = {
    "acknowledgments",
    "cover",
    "dedication",
    "doc-dedication",
    "doc-epigraph",
    "doc-introduction",
    "doc-preface",
    "epigraph",
    "frontmatter",
    "halftitlepage",
    "imprint",
    "introduction",
    "preface",
    "titlepage",
    "toc",
}
_BACK_TOKENS = {
    "afterword",
    "appendix",
    "backmatter",
    "bibliography",
    "colophon",
    "doc-colophon",
    "doc-endnotes",
    "endnotes",
    "footnotes",
    "glossary",
    "index",
    "notes",
    "uncopyright",
}
_CONTENT_TOKENS = {
    "chapter",
    "doc-chapter",
    "doc-prologue",
    "doc-epilogue",
    "prologue",
    "epilogue",
}
_CONTAINER_TOKENS = {"part", "doc-part", "volume", "book"}
_ROMAN_RE = re.compile(r"^[ivxlcdm]+(?:[.:\-](?:\s+|$)|\s+|$)", re.IGNORECASE)
_ARABIC_RE = re.compile(r"^\d+[.:\-]?\s*(?:.*)?$")
_PREFIXED_RE = re.compile(
    r"^(chapter|chapitre|cap[ií]tulo|capitolo|kapitel|chương|stave|letter|book|part|volume)\s+",
    re.IGNORECASE,
)
_CONTENT_PREFIX_RE = re.compile(
    r"^(chapter|chapitre|cap[ií]tulo|capitolo|kapitel|chương|stave|letter)\s+",
    re.IGNORECASE,
)
_LICENSE_RE = re.compile(r"(?:project\s+gutenberg.*license|full\s+license)", re.IGNORECASE)
_FRONT_TITLE_RE = re.compile(
    r"^(?:contents|table\s+of\s+contents|illustrations|list\s+of\s+illustrations|preface|introduction|dedication|titlepage|title\s+page|imprint|cover)$",
    re.IGNORECASE,
)
_BACK_TITLE_RE = re.compile(
    r"^(?:endnotes|notes|colophon|bibliography|index|uncopyright|appendix|appendices)$",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class RichBlock:
    ordinal: int
    kind: str
    text: str
    heading_level: int | None
    pandoc_path: str
    attr_id: str | None = None
    classes: tuple[str, ...] = ()
    key_values: tuple[tuple[str, str], ...] = ()
    source_file_hint: str | None = None
    resource_targets: tuple[str, ...] = ()
    math_fragments: tuple[str, ...] = ()


@dataclass(frozen=True)
class BoundaryCandidate:
    index: int
    title: str
    target: EpubTarget | None
    nav_entry_id: str | None
    parent_nav_id: str | None
    depth: int
    evidence: tuple[str, ...]
    role_hint: str | None = None
    confidence: float = 0.0


@dataclass(frozen=True)
class NormalizedUnit:
    unit_id: str
    order_index: int
    title: str
    start_block: int
    end_block: int
    role: str
    translation_policy: str
    parent_unit_id: str | None
    source_target: EpubTarget | None
    confidence: float
    evidence: tuple[str, ...]
    review_required: bool


@dataclass(frozen=True)
class EpubNormalizationResult:
    document: dict[str, Any]
    structure_manifest: dict[str, Any]


def _slug(value: str, *, fallback: str) -> str:
    ascii_value = unicodedata.normalize("NFKD", str(value or ""))
    ascii_value = ascii_value.encode("ascii", "ignore").decode("ascii")
    slug = re.sub(r"[^a-z0-9]+", "_", ascii_value.casefold()).strip("_")
    return slug[:60] or fallback


def _canonical_hash(payload: Any) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _canonical_document_kind(kind: str) -> str:
    """Project rich parser kinds onto the locked document.schema block enum.

    The richer Pandoc kind remains auditable in ``quality_flags`` and the
    structure sidecar.  Live D2L/Literary loaders continue to receive the
    legacy-compatible four-kind document contract.
    """

    return runtime_block_type(kind)


def _pandoc_document(
    source: Path,
    executable: str,
    *,
    package: EpubPackageIndex,
) -> tuple[dict[str, Any], str]:
    try:
        version = subprocess.run(
            [executable, "--version"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=True,
        ).stdout.splitlines()[0].strip()
    except (FileNotFoundError, subprocess.CalledProcessError) as exc:
        raise AdapterUnavailableError(f"Pandoc unavailable: {exc}") from exc
    merged_blocks: list[dict[str, Any]] = []
    fallback_files: list[str] = []
    api_version: list[int] | None = None
    with zipfile.ZipFile(source) as archive:
        for spine_item in package.spine:
            if not spine_item.linear:
                continue
            try:
                source_bytes = archive.read(spine_item.file)
            except KeyError as exc:
                raise ValueError(f"Linear spine item is missing: {spine_item.file}") from exc
            completed = subprocess.run(
                [executable, "-f", "html", "-t", "json"],
                input=source_bytes,
                capture_output=True,
                check=False,
            )
            if completed.returncode != 0:
                detail = completed.stderr.decode("utf-8", errors="replace").strip()
                raise RuntimeError(
                    detail or f"Pandoc failed for spine item {spine_item.file}"
                )
            part = json.loads(completed.stdout.decode("utf-8"))
            part_blocks = part.get("blocks") or []
            if not part_blocks:
                part_blocks = _native_xhtml_fallback(source_bytes)
                if part_blocks:
                    fallback_files.append(spine_item.file)
            if api_version is None:
                api_version = part.get("pandoc-api-version")
            basename = PurePosixPath(spine_item.file).name
            merged_blocks.append(
                {
                    "t": "Div",
                    "c": [
                        [
                            f"{basename}_spine",
                            ["epub-spine-item"],
                            [["epub-source-file", spine_item.file]],
                        ],
                        part_blocks,
                    ],
                }
            )
    if not merged_blocks:
        raise ValueError("EPUB contains no linear spine content")
    return {
        "pandoc-api-version": api_version,
        "meta": {"epub-native-fallback-files": fallback_files},
        "blocks": merged_blocks,
    }, version


def _plain_inlines(value: str) -> list[dict[str, Any]]:
    inlines: list[dict[str, Any]] = []
    for index, token in enumerate(str(value).split()):
        if index:
            inlines.append({"t": "Space"})
        inlines.append({"t": "Str", "c": token})
    return inlines


def _native_xhtml_fallback(source_bytes: bytes) -> list[dict[str, Any]]:
    try:
        root = ET.fromstring(source_bytes)
    except ET.ParseError:
        return []
    blocks: list[dict[str, Any]] = []
    block_tags = {"p", "pre", "blockquote", "li", "figcaption"}

    def visit(element: ET.Element) -> None:
        tag = str(element.tag).rsplit("}", 1)[-1].casefold()
        text = normalize_text(" ".join("".join(element.itertext()).split()))
        if re.fullmatch(r"h[1-6]", tag) and text:
            identifier = str(element.attrib.get("id") or "")
            blocks.append(
                {
                    "t": "Header",
                    "c": [int(tag[1]), [identifier, [], []], _plain_inlines(text)],
                }
            )
            return
        if tag in block_tags and text:
            blocks.append({"t": "Para", "c": _plain_inlines(text)})
            return
        for child in list(element):
            visit(child)

    visit(root)
    return blocks


def _attr(value: Any) -> tuple[str | None, tuple[str, ...], tuple[tuple[str, str], ...]]:
    if not isinstance(value, list) or len(value) < 3:
        return None, (), ()
    identifier = str(value[0] or "") or None
    classes = tuple(str(item) for item in (value[1] or []))
    pairs = tuple((str(item[0]), str(item[1])) for item in (value[2] or []) if len(item) >= 2)
    return identifier, classes, pairs


def _file_from_identifier(identifier: str | None, spine_files: Sequence[str]) -> str | None:
    if not identifier:
        return None
    folded = identifier.casefold()
    matches: list[tuple[int, str]] = []
    for file_name in spine_files:
        base = PurePosixPath(file_name).name.casefold()
        stem = PurePosixPath(file_name).stem.casefold()
        for token in {base, stem}:
            if token and (folded == token or folded.startswith(f"{token}_")):
                matches.append((len(token), file_name))
    return max(matches, default=(0, None))[1]


def _inline_components(value: Any) -> tuple[tuple[str, ...], tuple[str, ...]]:
    resources: list[str] = []
    mathematics: list[str] = []

    def visit(item: Any) -> None:
        if isinstance(item, dict):
            tag = item.get("t")
            content = item.get("c")
            if tag == "Image" and isinstance(content, list) and content:
                target = content[-1]
                if isinstance(target, list) and target and isinstance(target[0], str):
                    resources.append(target[0])
            elif tag == "Math":
                expression = content[-1] if isinstance(content, list) and content else content
                if isinstance(expression, str) and expression:
                    mathematics.append(expression)
            visit(content)
        elif isinstance(item, list):
            for child in item:
                visit(child)

    visit(value)
    return tuple(dict.fromkeys(resources)), tuple(dict.fromkeys(mathematics))


def _walk_rich_blocks(
    blocks: Iterable[dict[str, Any]],
    *,
    spine_files: Sequence[str],
    path: str = "/blocks",
    inherited_file: str | None = None,
) -> Iterator[RichBlock]:
    for index, block in enumerate(blocks or []):
        tag = str(block.get("t") or "")
        content = block.get("c")
        block_path = f"{path}/{index}"
        if tag == "Div":
            identifier, _classes, pairs = _attr(content[0] if isinstance(content, list) else None)
            declared_file = dict(pairs).get("epub-source-file")
            file_hint = declared_file or _file_from_identifier(identifier, spine_files) or inherited_file
            children = content[1] if isinstance(content, list) and len(content) >= 2 else []
            yield from _walk_rich_blocks(
                children,
                spine_files=spine_files,
                path=f"{block_path}/div",
                inherited_file=file_hint,
            )
            continue

        kind: str | None = None
        text = ""
        heading_level: int | None = None
        identifier: str | None = None
        classes: tuple[str, ...] = ()
        pairs: tuple[tuple[str, str], ...] = ()
        if tag == "Header" and isinstance(content, list) and len(content) >= 3:
            heading_level = int(content[0])
            identifier, classes, pairs = _attr(content[1])
            text = _inline_text(content[2])
            kind = "heading"
        elif tag in {"Para", "Plain"}:
            text = _inline_text(content or [])
            only_image = bool(content) and all(
                item.get("t") in {"Image", "Space", "SoftBreak"} for item in content
            )
            kind = "image" if only_image else "paragraph"
        elif tag == "CodeBlock":
            identifier, classes, pairs = _attr(content[0] if isinstance(content, list) else None)
            text = str(content[1] if isinstance(content, list) and len(content) >= 2 else "")
            kind = "code"
        elif tag == "BlockQuote":
            text = _pandoc_blocks_text(content or [])
            kind = "block_quote"
        elif tag in {"BulletList", "OrderedList"}:
            items = content[1] if tag == "OrderedList" and isinstance(content, list) else content
            for item_index, item in enumerate(items or []):
                item_text = _pandoc_blocks_text(item)
                if normalize_text(item_text):
                    yield RichBlock(
                        ordinal=-1,
                        kind="list_item",
                        text=normalize_text(item_text),
                        heading_level=None,
                        pandoc_path=f"{block_path}/items/{item_index}",
                        source_file_hint=inherited_file,
                    )
            continue
        elif tag == "DefinitionList":
            for item_index, item in enumerate(content or []):
                term = _inline_text(item[0] if item else [])
                definitions = " ".join(
                    _pandoc_blocks_text(value) for value in (item[1] if len(item) > 1 else [])
                )
                item_text = normalize_text(f"{term}: {definitions}" if definitions else term)
                if item_text:
                    yield RichBlock(
                        ordinal=-1,
                        kind="list_item",
                        text=item_text,
                        heading_level=None,
                        pandoc_path=f"{block_path}/definitions/{item_index}",
                        source_file_hint=inherited_file,
                    )
            continue
        elif tag == "Table":
            text = _pandoc_blocks_text(_nested_pandoc_blocks(content))
            kind = "table"
        elif tag == "Figure":
            children = content[-1] if isinstance(content, list) and content else []
            yield from _walk_rich_blocks(
                children,
                spine_files=spine_files,
                path=f"{block_path}/figure",
                inherited_file=inherited_file,
            )
            continue
        elif tag == "LineBlock":
            text = " ".join(_inline_text(line) for line in content or [])
            kind = "paragraph"

        normalized = normalize_text(text, kind or "paragraph")
        if kind and normalized:
            own_file = _file_from_identifier(identifier, spine_files) or inherited_file
            resource_targets, math_fragments = _inline_components(content)
            yield RichBlock(
                ordinal=-1,
                kind=normalize_kind(kind, normalized),
                text=normalized,
                heading_level=heading_level,
                pandoc_path=f"pandoc:{block_path}",
                attr_id=identifier,
                classes=classes,
                key_values=pairs,
                source_file_hint=own_file,
                resource_targets=resource_targets,
                math_fragments=math_fragments,
            )


def _rich_blocks(document: dict[str, Any], package: EpubPackageIndex) -> tuple[RichBlock, ...]:
    raw = list(
        _walk_rich_blocks(
            document.get("blocks") or [],
            spine_files=[item.file for item in package.spine],
        )
    )
    return tuple(replace(block, ordinal=index) for index, block in enumerate(raw))


def _heading_key(value: str) -> tuple[str, ...]:
    normalized = unicodedata.normalize("NFKD", str(value or ""))
    normalized = normalized.encode("ascii", "ignore").decode("ascii").casefold()
    return tuple(re.findall(r"[a-z0-9]+", normalized))


def _title_matches(nav_title: str, heading: str) -> bool:
    left = _heading_key(nav_title)
    right = _heading_key(heading)
    if not left or not right:
        return False
    if left == right:
        return True
    shortest = min(len(left), len(right))
    return shortest <= 3 and left[:shortest] == right[:shortest]


def _anchor_matches(entry: EpubNavEntry, block: RichBlock) -> bool:
    anchor = str(entry.target.anchor or "").casefold()
    identifier = str(block.attr_id or "").casefold()
    if not anchor or not identifier:
        return False
    return identifier == anchor or identifier.endswith(f"_{anchor}")


def _map_navigation(
    package: EpubPackageIndex,
    blocks: Sequence[RichBlock],
) -> dict[str, int]:
    result: dict[str, int] = {}
    cursor = 0
    for entry in package.navigation:
        anchored = [
            block.ordinal
            for block in blocks
            if block.ordinal >= cursor and _anchor_matches(entry, block)
        ]
        if anchored:
            result[entry.entry_id] = anchored[0]
            cursor = anchored[0]
            continue
        if entry.target.anchor:
            # An explicit EPUB fragment is a stronger contract than a fuzzy
            # title match. Missing fragments remain unresolved for review.
            continue
        file_and_title = [
            block.ordinal
            for block in blocks
            if block.ordinal >= cursor
            and block.kind == "heading"
            and block.source_file_hint == entry.target.file
            and _title_matches(entry.title, block.text)
        ]
        if file_and_title:
            result[entry.entry_id] = file_and_title[0]
            cursor = file_and_title[0]
            continue
        titled = [
            block.ordinal
            for block in blocks
            if block.ordinal >= cursor
            and block.kind == "heading"
            and _title_matches(entry.title, block.text)
        ]
        if titled:
            result[entry.entry_id] = titled[0]
            cursor = titled[0]
            continue
        file_only = [
            block.ordinal
            for block in blocks
            if block.ordinal >= cursor and block.source_file_hint == entry.target.file
        ]
        if file_only:
            result[entry.entry_id] = file_only[0]
            cursor = file_only[0]
    return result


def _combined_tokens(entry: EpubNavEntry, package: EpubPackageIndex) -> set[str]:
    values = set(entry.type_tokens)
    for item in package.spine:
        if item.file == entry.target.file:
            values.update(item.properties)
            values.update(item.type_tokens)
    for landmark in package.landmarks:
        same_target = (
            landmark.target.file == entry.target.file
            and landmark.target.anchor == entry.target.anchor
        )
        if same_target:
            values.update(landmark.type_tokens)
    return {value.casefold() for value in values}


def _structural_role(entry: EpubNavEntry, package: EpubPackageIndex) -> tuple[str | None, float, tuple[str, ...]]:
    tokens = _combined_tokens(entry, package)
    if tokens & _CONTAINER_TOKENS:
        return "container", 0.99, tuple(f"epub_type:{value}" for value in sorted(tokens & _CONTAINER_TOKENS))
    if tokens & _BACK_TOKENS:
        return "back_matter", 0.98, tuple(f"epub_type:{value}" for value in sorted(tokens & _BACK_TOKENS))
    if tokens & _FRONT_TOKENS:
        return "front_matter", 0.98, tuple(f"epub_type:{value}" for value in sorted(tokens & _FRONT_TOKENS))
    if tokens & _CONTENT_TOKENS:
        return "content_unit", 0.99, tuple(f"epub_type:{value}" for value in sorted(tokens & _CONTENT_TOKENS))
    title = entry.title.strip()
    if _LICENSE_RE.search(title):
        return "back_matter", 0.96, ("standardized_title:license",)
    if _BACK_TITLE_RE.fullmatch(title):
        return "back_matter", 0.86, ("standardized_title:back_matter",)
    if _FRONT_TITLE_RE.fullmatch(title):
        return "front_matter", 0.84, ("standardized_title:front_matter",)
    return None, 0.0, ()


def _sequence_family(entries: Sequence[EpubNavEntry]) -> set[str]:
    families: dict[str, list[EpubNavEntry]] = {"roman": [], "arabic": [], "prefixed": []}
    for entry in entries:
        title = entry.title.strip()
        if _PREFIXED_RE.match(title):
            families["prefixed"].append(entry)
        elif _ARABIC_RE.match(title):
            families["arabic"].append(entry)
        elif _ROMAN_RE.match(title):
            families["roman"].append(entry)
    eligible = [(len(items), name, items) for name, items in families.items() if len(items) >= 2]
    if not eligible:
        return set()
    _count, family, selected = max(eligible)
    return {entry.entry_id for entry in selected}


def _select_boundaries(
    package: EpubPackageIndex,
    blocks: Sequence[RichBlock],
    nav_map: dict[str, int],
) -> tuple[list[BoundaryCandidate], set[str]]:
    sequence_ids = _sequence_family(package.navigation)
    selected: list[BoundaryCandidate] = []
    content_ids: set[str] = set()
    for entry in package.navigation:
        if entry.entry_id not in nav_map:
            continue
        role, confidence, evidence = _structural_role(entry, package)
        include = False
        if role is not None:
            include = True
        elif entry.entry_id in sequence_ids:
            role = "content_unit"
            confidence = 0.82
            evidence = ("repeated_heading_family",)
            include = True
        elif entry.depth == 0:
            include = True
            evidence = ("navigation_top_level",)
            confidence = 0.55
        if not include:
            continue
        if role == "content_unit":
            content_ids.add(entry.entry_id)
        selected.append(
            BoundaryCandidate(
                index=nav_map[entry.entry_id],
                title=entry.title,
                target=entry.target,
                nav_entry_id=entry.entry_id,
                parent_nav_id=entry.parent_id,
                depth=entry.depth,
                evidence=evidence,
                role_hint=role,
                confidence=confidence,
            )
        )

    heading_blocks = [block for block in blocks if block.kind == "heading"]
    contents_indexes = [
        block.ordinal
        for block in heading_blocks
        if block.text.strip().casefold() in {"contents", "table of contents"}
    ]
    back_indexes = [
        block.ordinal
        for block in heading_blocks
        if _LICENSE_RE.search(block.text) or _BACK_TITLE_RE.fullmatch(block.text.strip())
    ]
    contents_end = max(contents_indexes) if contents_indexes else None
    back_start = (
        min(index for index in back_indexes if contents_end is None or index > contents_end)
        if any(contents_end is None or index > contents_end for index in back_indexes)
        else len(blocks)
    )
    post_contents_level = None
    if contents_end is not None:
        post_contents_levels = Counter(
            block.heading_level
            for block in heading_blocks
            if (
                contents_end < block.ordinal < back_start
                and block.heading_level is not None
                and not _FRONT_TITLE_RE.fullmatch(block.text.strip())
                and not _BACK_TITLE_RE.fullmatch(block.text.strip())
                and not _LICENSE_RE.search(block.text)
            )
        )
        repeated_levels = [level for level, count in post_contents_levels.items() if count >= 2]
        post_contents_level = min(repeated_levels) if repeated_levels else None

    # Standardized publication boilerplate may share a spine file with the
    # final narrative unit and therefore have no navigation entry of its own.
    # Preserve it as a separate non-translatable unit instead of deleting it or
    # allowing it to leak into the last story chapter.
    for block in heading_blocks:
        role = None
        evidence: tuple[str, ...] = ()
        confidence = 0.0
        if _LICENSE_RE.search(block.text):
            role = "back_matter"
            evidence = ("standardized_heading:license",)
            confidence = 0.97
        elif _CONTENT_PREFIX_RE.match(block.text.strip()):
            role = "content_unit"
            evidence = ("standardized_heading:content_unit",)
            confidence = 0.88
        elif _BACK_TITLE_RE.fullmatch(block.text.strip()):
            role = "back_matter"
            evidence = ("standardized_heading:back_matter",)
            confidence = 0.86
        elif _FRONT_TITLE_RE.fullmatch(block.text.strip()):
            role = "front_matter"
            evidence = ("standardized_heading:front_matter",)
            confidence = 0.84
        elif (
            contents_end is not None
            and post_contents_level is not None
            and contents_end < block.ordinal < back_start
            and block.heading_level == post_contents_level
        ):
            # Gutenberg-style EPUBs often expose only one navigation target for
            # the whole body. The repeated top-level headings after Contents
            # are structural chapter boundaries; no title-language inference
            # is needed here.
            role = "content_unit"
            evidence = ("standardized_heading:post_contents_repeated_level",)
            confidence = 0.82
        if role is not None:
            selected.append(
                BoundaryCandidate(
                    index=block.ordinal,
                    title=block.text,
                    target=(
                        EpubTarget(file=block.source_file_hint)
                        if block.source_file_hint
                        else None
                    ),
                    nav_entry_id=None,
                    parent_nav_id=None,
                    depth=0,
                    evidence=evidence,
                    role_hint=role,
                    confidence=confidence,
                )
            )

    if not selected:
        heading_indexes = [block.ordinal for block in blocks if block.kind == "heading"]
        repeated_levels = Counter(
            block.heading_level for block in blocks if block.kind == "heading" and block.heading_level
        )
        levels = [level for level, count in repeated_levels.items() if count >= 2]
        level = min(levels) if levels else None
        if level is not None:
            for index in heading_indexes:
                block = blocks[index]
                if block.heading_level == level:
                    selected.append(
                        BoundaryCandidate(
                            index=index,
                            title=block.text,
                            target=EpubTarget(file=block.source_file_hint) if block.source_file_hint else None,
                            nav_entry_id=None,
                            parent_nav_id=None,
                            depth=0,
                            evidence=("pandoc_repeated_heading_level",),
                            confidence=0.45,
                        )
                    )
    if selected and selected[0].index > 0:
        first_boundary = min(selected, key=lambda item: item.index)
        prefix_is_content = first_boundary.role_hint == "back_matter"
        selected.append(
            BoundaryCandidate(
                index=0,
                title=(
                    next((block.text for block in blocks if block.kind == "heading"), "Content")
                    if prefix_is_content
                    else "Front matter"
                ),
                target=EpubTarget(file=blocks[0].source_file_hint) if blocks[0].source_file_hint else None,
                nav_entry_id=None,
                parent_nav_id=None,
                depth=0,
                evidence=(
                    "exact_cover_content_before_back_matter"
                    if prefix_is_content
                    else "exact_cover_prefix"
                ,),
                role_hint="content_unit" if prefix_is_content else "front_matter",
                confidence=0.9,
            )
        )
    if not selected and blocks:
        selected.append(
            BoundaryCandidate(
                index=0,
                title=next((block.text for block in blocks if block.kind == "heading"), "Document"),
                target=EpubTarget(file=blocks[0].source_file_hint) if blocks[0].source_file_hint else None,
                nav_entry_id=None,
                parent_nav_id=None,
                depth=0,
                evidence=("single_unit_fallback",),
                confidence=0.35,
            )
        )

    by_index: dict[int, BoundaryCandidate] = {}
    for candidate in selected:
        prior = by_index.get(candidate.index)
        if prior is None or candidate.confidence > prior.confidence:
            by_index[candidate.index] = candidate
    ordered = [by_index[index] for index in sorted(by_index)]
    return ordered, content_ids


def _role_policy(role: str) -> str:
    return {
        "content_unit": "translate",
        "front_matter": "preserve",
        "container": "preserve",
        "back_matter": "exclude",
        "unknown": "review",
    }[role]


def _materialize_units(
    boundaries: Sequence[BoundaryCandidate],
    blocks: Sequence[RichBlock],
    package: EpubPackageIndex,
) -> tuple[NormalizedUnit, ...]:
    nav_to_unit: dict[str, str] = {}
    provisional: list[NormalizedUnit] = []
    for order, boundary in enumerate(boundaries):
        end = boundaries[order + 1].index if order + 1 < len(boundaries) else len(blocks)
        unit_id = f"u{order + 1:04d}_{_slug(boundary.title, fallback='unit')}"
        role = boundary.role_hint or "unknown"
        if boundary.nav_entry_id:
            nav_to_unit[boundary.nav_entry_id] = unit_id
        provisional.append(
            NormalizedUnit(
                unit_id=unit_id,
                order_index=order,
                title=boundary.title,
                start_block=boundary.index,
                end_block=end,
                role=role,
                translation_policy=_role_policy(role),
                parent_unit_id=None,
                source_target=boundary.target,
                confidence=boundary.confidence,
                evidence=boundary.evidence,
                review_required=role == "unknown",
            )
        )

    explicit_content = [unit for unit in provisional if unit.role == "content_unit"]
    if not explicit_content:
        runs: list[list[NormalizedUnit]] = []
        current: list[NormalizedUnit] = []
        prior_target: tuple[str, str] | None = None
        for unit in provisional:
            source_file = unit.source_target.file if unit.source_target else None
            source_anchor = unit.source_target.anchor if unit.source_target else None
            source_target = (source_file or "", source_anchor or "")
            qualifies = (
                unit.role == "unknown"
                and bool(source_file)
                and source_target != prior_target
            )
            if qualifies:
                current.append(unit)
                prior_target = source_target
            else:
                if len(current) >= 2:
                    runs.append(current)
                current = []
                prior_target = None
        if len(current) >= 2:
            runs.append(current)
        if runs:
            selected_run = max(
                runs,
                key=lambda run: (
                    len(run),
                    sum(unit.end_block - unit.start_block for unit in run),
                ),
            )
            selected_orders = {unit.order_index for unit in selected_run}
            for index, unit in enumerate(provisional):
                if unit.order_index in selected_orders:
                    provisional[index] = replace(
                        unit,
                        role="content_unit",
                        translation_policy="translate",
                        confidence=max(unit.confidence, 0.76),
                        evidence=unit.evidence + ("distinct_navigation_target_run",),
                        review_required=False,
                    )
            explicit_content = [unit for unit in provisional if unit.role == "content_unit"]
    if not explicit_content and provisional:
        candidates = [
            unit
            for unit in provisional
            if unit.role not in {"front_matter", "back_matter", "container"}
        ]
        if candidates:
            largest = max(
                candidates,
                key=lambda unit: sum(len(blocks[index].text) for index in range(unit.start_block, unit.end_block)),
            )
            provisional[largest.order_index] = replace(
                largest,
                role="content_unit",
                translation_policy="translate",
                confidence=max(largest.confidence, 0.68),
                evidence=largest.evidence + ("single_dominant_narrative_unit",),
                review_required=False,
            )

    content_orders = [unit.order_index for unit in provisional if unit.role == "content_unit"]
    positionally_bounded = any(
        unit.role == "content_unit" and "single_dominant_narrative_unit" not in unit.evidence
        for unit in provisional
    )
    if content_orders and positionally_bounded:
        first_content, last_content = min(content_orders), max(content_orders)
        for index, unit in enumerate(provisional):
            if unit.role != "unknown":
                continue
            if index < first_content:
                provisional[index] = replace(
                    unit,
                    role="front_matter",
                    translation_policy="preserve",
                    confidence=max(unit.confidence, 0.62),
                    evidence=unit.evidence + ("position_before_body",),
                    review_required=False,
                )
            elif index > last_content:
                provisional[index] = replace(
                    unit,
                    role="back_matter",
                    translation_policy="exclude",
                    confidence=max(unit.confidence, 0.62),
                    evidence=unit.evidence + ("position_after_body",),
                    review_required=False,
                )

    boundary_by_index = {boundary.index: boundary for boundary in boundaries}
    result: list[NormalizedUnit] = []
    for unit in provisional:
        boundary = boundary_by_index[unit.start_block]
        parent = nav_to_unit.get(boundary.parent_nav_id or "")
        result.append(replace(unit, parent_unit_id=parent))
    return tuple(result)


def _validate_exact_cover(units: Sequence[NormalizedUnit], block_count: int) -> dict[str, Any]:
    ownership: list[int | None] = [None] * block_count
    overlaps: list[int] = []
    for unit in units:
        if unit.start_block < 0 or unit.end_block > block_count or unit.start_block >= unit.end_block:
            raise ValueError(f"Invalid unit range: {unit.unit_id}")
        for index in range(unit.start_block, unit.end_block):
            if ownership[index] is not None:
                overlaps.append(index)
            ownership[index] = unit.order_index
    missing = [index for index, owner in enumerate(ownership) if owner is None]
    if overlaps or missing:
        raise ValueError(f"Unit exact-cover failed: overlaps={overlaps[:10]} missing={missing[:10]}")
    return {
        "expected_blocks": block_count,
        "covered_blocks": sum(owner is not None for owner in ownership),
        "overlap_count": 0,
        "missing_count": 0,
        "coverage": 1.0,
    }


def _block_source_file(block: RichBlock, unit: NormalizedUnit) -> str | None:
    return block.source_file_hint or (unit.source_target.file if unit.source_target else None)


def normalize_epub(
    source_path: str | Path,
    *,
    doc_id: str,
    source_language: str = "en",
    target_language: str = "vi",
    pandoc_executable: str = "pandoc",
) -> EpubNormalizationResult:
    source = Path(source_path).resolve()
    if source.suffix.casefold() != ".epub":
        raise ValueError("EPUB normalizer requires a .epub source")
    if not source.is_file():
        raise FileNotFoundError(source)
    package = read_epub_package(source)
    pandoc_document, pandoc_version = _pandoc_document(
        source,
        pandoc_executable,
        package=package,
    )
    blocks = _rich_blocks(pandoc_document, package)
    if not blocks:
        raise ValueError("Pandoc produced no canonical text blocks")
    nav_map = _map_navigation(package, blocks)
    boundaries, _content_ids = _select_boundaries(package, blocks, nav_map)
    units = _materialize_units(boundaries, blocks, package)
    exact_cover = _validate_exact_cover(units, len(blocks))

    document_chapters: list[dict[str, Any]] = []
    source_map: list[dict[str, Any]] = []
    for unit in units:
        chapter_id = f"{_slug(doc_id, fallback='doc')}_{unit.unit_id}"
        chapter_blocks: list[dict[str, Any]] = []
        for local_order, block_index in enumerate(range(unit.start_block, unit.end_block)):
            block = blocks[block_index]
            block_id = f"{chapter_id}_b{local_order + 1:04d}"
            source_file = _block_source_file(block, unit)
            source_anchor = block.attr_id or (unit.source_target.anchor if unit.source_target else None)
            canonical_kind = _canonical_document_kind(block.kind)
            quality_flags = [
                f"unit_role:{unit.role}",
                f"translation_policy:{unit.translation_policy}",
                "source_format:epub",
            ]
            if unit.review_required:
                quality_flags.append("structure_review_required")
            chapter_blocks.append(
                {
                    "block_id": block_id,
                    "order_index": local_order + 1,
                    "page_ids": [],
                    "block_type": canonical_kind,
                    "is_chapter_opening": local_order == 0,
                    "source_text": block.text,
                    "clean_text": block.text,
                    "sentences": [],
                    "quality_flags": quality_flags,
                    "annotations": {},
                }
            )
            source_map.append(
                {
                    "block_id": block_id,
                    "epub_file": source_file,
                    "epub_anchor": source_anchor,
                    "pandoc_path": block.pandoc_path,
                    "source_block_kind": block.kind,
                    "resource_targets": list(block.resource_targets),
                    "math_fragments": list(block.math_fragments),
                    "provenance_precision": "epub_file_or_unit_anchor_plus_pandoc_path",
                }
            )
        document_chapters.append(
            {
                "chapter_id": chapter_id,
                "order_index": unit.order_index + 1,
                "title": unit.title,
                "blocks": chapter_blocks,
            }
        )

    translatable = [
        document_chapters[index]["chapter_id"]
        for index, unit in enumerate(units)
        if unit.role == "content_unit"
    ]
    source_sha256 = hashlib.sha256(source.read_bytes()).hexdigest()
    structure_payload = [
        {
            "unit_id": unit.unit_id,
            "chapter_id": document_chapters[index]["chapter_id"],
            "order_index": unit.order_index,
            "title": unit.title,
            "block_range": [unit.start_block, unit.end_block],
            "role": unit.role,
            "translation_policy": unit.translation_policy,
            "parent_unit_id": unit.parent_unit_id,
            "source_target": (
                {"file": unit.source_target.file, "anchor": unit.source_target.anchor}
                if unit.source_target
                else None
            ),
            "confidence": round(unit.confidence, 3),
            "evidence": list(unit.evidence),
            "review_required": unit.review_required,
        }
        for index, unit in enumerate(units)
    ]
    navigation_title_counts = Counter(entry.title.strip().casefold() for entry in package.navigation)
    navigation_target_counts = Counter(
        (entry.target.file, entry.target.anchor or "") for entry in package.navigation
    )
    manifest = {
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "normalizer_version": NORMALIZER_VERSION,
        "doc_id": doc_id,
        "source": {
            "path": str(source),
            "sha256": source_sha256,
            "format": "epub",
            "opf_rootfile": package.rootfile,
        },
        "extractor": {
            "name": "pandoc",
            "version": pandoc_version,
            "mode": "per_linear_spine_html",
            "native_empty_spine_fallback_files": list(
                (pandoc_document.get("meta") or {}).get("epub-native-fallback-files") or []
            ),
        },
        "navigation": {
            "source": package.navigation_source,
            "entry_count": len(package.navigation),
            "mapped_entry_count": len(nav_map),
            "landmark_count": len(package.landmarks),
            "spine_item_count": len(package.spine),
            "linear_spine_item_count": sum(item.linear for item in package.spine),
            "nonlinear_spine_item_count": sum(not item.linear for item in package.spine),
            "unmapped_entry_ids": [
                entry.entry_id for entry in package.navigation if entry.entry_id not in nav_map
            ],
            "duplicate_titles": sorted({
                entry.title
                for entry in package.navigation
                if navigation_title_counts[entry.title.strip().casefold()] > 1
            }),
            "duplicate_targets": sorted({
                f"{entry.target.file}#{entry.target.anchor or ''}"
                for entry in package.navigation
                if navigation_target_counts[(entry.target.file, entry.target.anchor or "")] > 1
            }),
        },
        "package_structure": {
            "spine": [
                {
                    "order": item.order,
                    "file": item.file,
                    "linear": item.linear,
                    "properties": list(item.properties),
                    "type_tokens": list(item.type_tokens),
                }
                for item in package.spine
            ],
            "navigation": [
                {
                    "entry_id": entry.entry_id,
                    "title": entry.title,
                    "target": {"file": entry.target.file, "anchor": entry.target.anchor},
                    "depth": entry.depth,
                    "parent_id": entry.parent_id,
                    "source": entry.source,
                    "type_tokens": list(entry.type_tokens),
                    "mapped_block": nav_map.get(entry.entry_id),
                }
                for entry in package.navigation
            ],
            "landmarks": [
                {
                    "entry_id": entry.entry_id,
                    "title": entry.title,
                    "target": {"file": entry.target.file, "anchor": entry.target.anchor},
                    "type_tokens": list(entry.type_tokens),
                }
                for entry in package.landmarks
            ],
        },
        "units": structure_payload,
        "translatable_chapter_ids": translatable,
        "review_required_unit_ids": [unit.unit_id for unit in units if unit.review_required],
        "review_required_chapter_ids": [
            document_chapters[index]["chapter_id"]
            for index, unit in enumerate(units)
            if unit.review_required
        ],
        "exact_cover": exact_cover,
        "source_map": source_map,
    }
    manifest["structure_sha256"] = _canonical_hash(
        {
            "normalizer_version": NORMALIZER_VERSION,
            "source_sha256": source_sha256,
            "units": structure_payload,
            "source_map": source_map,
        }
    )
    document = {
        "schema_version": DOCUMENT_SCHEMA_VERSION,
        "doc_id": doc_id,
        "metadata": {
            "title": package.titles[0] if package.titles else source.stem,
            "author": "",
            "domain": "literature",
            "genre": "novel",
            "source_language": source_language,
            "target_language": target_language,
            "source_format": "epub",
            "license": "unknown",
            "raw_sha256": source_sha256,
            "extraction_tool": NORMALIZER_VERSION,
            "pipeline_version": NORMALIZER_VERSION,
            "contamination_risk": "medium",
        },
        "chapters": document_chapters,
    }
    return EpubNormalizationResult(document=document, structure_manifest=manifest)


def write_epub_normalization(
    result: EpubNormalizationResult,
    output_dir: str | Path,
) -> tuple[Path, Path]:
    destination = Path(output_dir)
    destination.mkdir(parents=True, exist_ok=True)
    document_path = destination / "document.json"
    manifest_path = destination / "structure_manifest.json"
    document_temp = destination / ".document.json.tmp"
    manifest_temp = destination / ".structure_manifest.json.tmp"
    document_temp.write_text(
        json.dumps(result.document, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    manifest_temp.write_text(
        json.dumps(result.structure_manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    document_temp.replace(document_path)
    manifest_temp.replace(manifest_path)
    materialize_source_package(
        result.document,
        result.structure_manifest,
        destination,
    )
    return document_path, manifest_path


__all__ = [
    "DOCUMENT_SCHEMA_VERSION",
    "EpubNormalizationResult",
    "MANIFEST_SCHEMA_VERSION",
    "NORMALIZER_VERSION",
    "TRANSLATION_POLICIES",
    "UNIT_ROLES",
    "normalize_epub",
    "write_epub_normalization",
]
