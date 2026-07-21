from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from collections import Counter
from dataclasses import dataclass, replace
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Iterable, Sequence

from pipeline.ingest.document_contract import runtime_block_type
from pipeline.ingest.normalization_adapters import AdapterUnavailableError, run_pandoc
from pipeline.ingest.normalization_ir import TOKEN_RE, normalize_kind, normalize_text
from pipeline.ingest.source_package_materializer import materialize_source_package


NORMALIZER_VERSION = "html_hybrid_normalizer_v1"
MANIFEST_SCHEMA_VERSION = "html_structure_manifest_v1"
DOCUMENT_SCHEMA_VERSION = "1.5.0"

UNIT_ROLES = {
    "front_matter",
    "content_unit",
    "container",
    "back_matter",
    "unknown",
}

_VOID_TAGS = {
    "area",
    "base",
    "br",
    "col",
    "embed",
    "hr",
    "img",
    "input",
    "link",
    "meta",
    "param",
    "source",
    "track",
    "wbr",
}
_SKIP_TAGS = {"head", "script", "style", "template", "noscript"}
_CONTAINER_TAGS = {
    "html",
    "body",
    "header",
    "footer",
    "nav",
    "main",
    "article",
    "section",
    "aside",
    "div",
    "figure",
    "details",
    "ul",
    "ol",
    "dl",
}
_LEAF_BLOCK_TAGS = {"p", "pre", "figcaption", "dt", "dd", "address", "summary"}
_STRUCTURAL_TAGS = _CONTAINER_TAGS | _LEAF_BLOCK_TAGS | {
    "blockquote",
    "li",
    "table",
    "h1",
    "h2",
    "h3",
    "h4",
    "h5",
    "h6",
}
_AUTO_CLOSE_SAME = {"p", "li", "dt", "dd", "tr", "th", "td", "option"}

_FRONT_TOKENS = {
    "cover",
    "dedication",
    "doc-dedication",
    "doc-epigraph",
    "doc-introduction",
    "doc-preface",
    "epigraph",
    "front",
    "frontmatter",
    "front-matter",
    "halftitlepage",
    "imprint",
    "introduction",
    "preface",
    "pg-header",
    "titlepage",
    "toc",
}
_BACK_TOKENS = {
    "afterword",
    "appendix",
    "back",
    "backmatter",
    "back-matter",
    "bibliography",
    "colophon",
    "doc-colophon",
    "doc-endnotes",
    "endnotes",
    "footnotes",
    "glossary",
    "index",
    "license",
    "notes",
    "pg-footer",
    "project-gutenberg-license",
    "uncopyright",
}
_CONTENT_TOKENS = {
    "chapter",
    "doc-chapter",
    "doc-epilogue",
    "doc-prologue",
    "epilogue",
    "prologue",
}
_CONTAINER_TOKENS = {"book", "doc-part", "part", "volume"}

_ROMAN_RE = re.compile(r"^[ivxlcdm]+[.:-]?$", re.IGNORECASE)
_ARABIC_RE = re.compile(r"^\d+[.:-]?$", re.IGNORECASE)
_PREFIXED_CONTENT_RE = re.compile(
    r"^(?:chapter|chapitre|capitulo|capitolo|kapitel|chuong|stave|letter)\s+[\wivxlcdm]+",
    re.IGNORECASE,
)
_PREFIXED_CONTAINER_RE = re.compile(
    r"^(?:book|part|volume)\s+[\wivxlcdm]+",
    re.IGNORECASE,
)
_FRONT_TITLE_RE = re.compile(
    r"^(?:contents|table\s+of\s+contents|illustrations|list\s+of\s+illustrations|"
    r"preface|introduction|dedication|title\s*page|imprint|cover)$",
    re.IGNORECASE,
)
_BACK_TITLE_RE = re.compile(
    r"^(?:afterword|endnotes|notes|colophon|bibliography|index|appendix|appendices|glossary)$",
    re.IGNORECASE,
)
_LICENSE_RE = re.compile(r"(?:project\s+gutenberg.*license|full\s+license)", re.IGNORECASE)

_VERSE_TOKENS = {"poem", "poetry", "song", "stanza", "verse", "verses"}
_CODE_TOKENS = {
    "code",
    "highlight",
    "highlighted",
    "listing",
    "program",
    "programming",
    "source-code",
    "sourcecode",
}
_CODE_LINE_RE = re.compile(
    r"^\s*(?:class|const|def|enum|fn|for|from|function|if|import|interface|"
    r"let|package|return|struct|var|while)\b|^\s*#\s*include\b|"
    r"^\s*[A-Za-z_$][\w$]*\s*\([^\n]*\)\s*[;{]?\s*$"
)


@dataclass
class HtmlNode:
    tag: str
    attrs: dict[str, str]
    path: str
    line_start: int
    parent: HtmlNode | None = None
    children: list[HtmlNode | str] | None = None
    line_end: int | None = None

    def __post_init__(self) -> None:
        if self.children is None:
            self.children = []


@dataclass(frozen=True)
class HtmlBlock:
    ordinal: int
    kind: str
    text: str
    heading_level: int | None
    html_path: str
    line_start: int
    line_end: int
    attr_id: str | None
    classes: tuple[str, ...]
    ancestor_paths: tuple[str, ...]
    structural_tokens: tuple[str, ...]


@dataclass(frozen=True)
class HtmlContainer:
    path: str
    parent_path: str | None
    tag: str
    attr_id: str | None
    classes: tuple[str, ...]
    structural_tokens: tuple[str, ...]
    start_block: int
    end_block: int
    depth: int
    title: str | None
    heading_level: int | None


@dataclass(frozen=True)
class BoundaryCandidate:
    index: int
    title: str
    role: str
    confidence: float
    evidence: tuple[str, ...]
    heading_level: int | None = None
    container_path: str | None = None
    parent_container_path: str | None = None


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
    confidence: float
    evidence: tuple[str, ...]
    review_required: bool


@dataclass(frozen=True)
class HtmlNormalizationResult:
    document: dict[str, Any]
    structure_manifest: dict[str, Any]


class _TreeParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.root = HtmlNode(tag="document", attrs={}, path="", line_start=1)
        self.stack = [self.root]

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        name = tag.casefold()
        if self.stack[-1].tag == "p" and name in _STRUCTURAL_TAGS:
            self.stack.pop()
        if name in _AUTO_CLOSE_SAME and self.stack[-1].tag == name:
            self.stack.pop()
        parent = self.stack[-1]
        siblings = [child for child in parent.children or [] if isinstance(child, HtmlNode) and child.tag == name]
        path = f"{parent.path}/{name}[{len(siblings) + 1}]"
        node = HtmlNode(
            tag=name,
            attrs={str(key).casefold(): str(value or "") for key, value in attrs},
            path=path,
            line_start=self.getpos()[0],
            parent=parent,
        )
        parent.children.append(node)
        if name not in _VOID_TAGS:
            self.stack.append(node)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)
        if tag.casefold() not in _VOID_TAGS:
            self.handle_endtag(tag)

    def handle_endtag(self, tag: str) -> None:
        name = tag.casefold()
        for index in range(len(self.stack) - 1, 0, -1):
            if self.stack[index].tag != name:
                continue
            line = self.getpos()[0]
            for node in self.stack[index:]:
                node.line_end = line
            del self.stack[index:]
            return

    def handle_data(self, data: str) -> None:
        self.stack[-1].children.append(data)

    def close(self) -> None:
        super().close()
        for node in self.stack[1:]:
            node.line_end = node.line_start


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
    return runtime_block_type(kind)


def _split_tokens(value: str) -> set[str]:
    normalized = unicodedata.normalize("NFKD", str(value or ""))
    normalized = normalized.encode("ascii", "ignore").decode("ascii").casefold()
    return {token for token in re.split(r"[^a-z0-9-]+", normalized) if token}


def _node_tokens(node: HtmlNode) -> set[str]:
    values = {node.tag}
    for key in ("id", "class", "role", "epub:type", "data-type", "data-role"):
        values.update(_split_tokens(node.attrs.get(key, "")))
    return values


def _is_hidden(node: HtmlNode) -> bool:
    if node.tag in _SKIP_TAGS or "hidden" in node.attrs:
        return True
    if node.attrs.get("aria-hidden", "").casefold() == "true":
        return True
    style = re.sub(r"\s+", "", node.attrs.get("style", "").casefold())
    return "display:none" in style or "visibility:hidden" in style


def _node_text(node: HtmlNode, *, skip_block_descendants: bool = False) -> str:
    parts: list[str] = []

    def visit(value: HtmlNode | str, *, root: bool = False) -> None:
        if isinstance(value, str):
            parts.append(value)
            return
        if _is_hidden(value):
            return
        if not root and skip_block_descendants and value.tag in _STRUCTURAL_TAGS:
            return
        if value.tag == "br":
            parts.append("\n")
            return
        if value.tag == "img":
            parts.append(value.attrs.get("alt", ""))
            return
        for child in value.children or []:
            visit(child)

    visit(node, root=True)
    return normalize_text(" ".join(parts))


def _node_text_with_breaks(node: HtmlNode) -> str:
    """Keep authored ``br`` boundaries without treating source wrapping as layout."""

    parts: list[str] = []

    def visit(value: HtmlNode | str) -> None:
        if isinstance(value, str):
            parts.append(re.sub(r"\s+", " ", value))
            return
        if _is_hidden(value):
            return
        if value.tag == "br":
            parts.append("\n")
            return
        if value.tag == "img":
            parts.append(value.attrs.get("alt", ""))
            return
        for child in value.children or []:
            visit(child)

    visit(node)
    value = unicodedata.normalize("NFC", "".join(parts))
    lines = [
        re.sub(r"[ \t\f\v]+", " ", line).strip()
        for line in value.split("\n")
    ]
    while lines and not lines[0]:
        lines.pop(0)
    while lines and not lines[-1]:
        lines.pop()
    return "\n".join(lines)


def _node_preformatted_text(node: HtmlNode) -> str:
    """Preserve meaningful indentation and line breaks inside a pre element."""

    parts: list[str] = []

    def visit(value: HtmlNode | str) -> None:
        if isinstance(value, str):
            parts.append(value)
            return
        if _is_hidden(value):
            return
        if value.tag == "br":
            parts.append("\n")
            return
        if value.tag == "img":
            parts.append(value.attrs.get("alt", ""))
            return
        for child in value.children or []:
            visit(child)

    visit(node)
    value = unicodedata.normalize("NFC", "".join(parts))
    value = value.replace("\r\n", "\n").replace("\r", "\n")
    lines = value.split("\n")
    while lines and not lines[0].strip():
        lines.pop(0)
    while lines and not lines[-1].strip():
        lines.pop()
    return "\n".join(line.rstrip() for line in lines)


def _has_descendant_tag(node: HtmlNode, tag: str) -> bool:
    for child in node.children or []:
        if not isinstance(child, HtmlNode):
            continue
        if child.tag == tag or _has_descendant_tag(child, tag):
            return True
    return False


def _preformatted_kind(node: HtmlNode, text: str) -> str:
    tokens = _node_tokens(node)
    if tokens & _VERSE_TOKENS:
        return "verse"
    if _has_descendant_tag(node, "code") or tokens & _CODE_TOKENS:
        return "code"
    nonempty_lines = [line for line in text.splitlines() if line.strip()]
    code_lines = sum(bool(_CODE_LINE_RE.search(line)) for line in nonempty_lines)
    if code_lines >= 2 and code_lines * 2 >= len(nonempty_lines):
        return "code"
    return "preformatted"


def _image_label(node: HtmlNode) -> str:
    for value in (node.attrs.get("alt"), node.attrs.get("title")):
        normalized = normalize_text(value or "")
        if normalized:
            return normalized
    target = str(node.attrs.get("src") or "").split("?", 1)[0].split("#", 1)[0]
    filename = target.replace("\\", "/").rsplit("/", 1)[-1]
    return filename or "image"


def _descendant_anchor(node: HtmlNode) -> str | None:
    own = node.attrs.get("id")
    if own:
        return own
    for child in node.children or []:
        if not isinstance(child, HtmlNode):
            continue
        found = _descendant_anchor(child)
        if found:
            return found
    return None


def _extract_native_blocks(root: HtmlNode) -> tuple[tuple[HtmlBlock, ...], tuple[HtmlContainer, ...]]:
    blocks: list[HtmlBlock] = []
    containers: list[HtmlContainer] = []

    def emit(
        node: HtmlNode,
        kind: str,
        text: str,
        ancestors: tuple[HtmlNode, ...],
        *,
        preserve_layout: bool = False,
    ) -> None:
        normalized = text if preserve_layout else normalize_text(text, kind)
        if not normalized:
            return
        tokens: set[str] = set()
        for ancestor in ancestors:
            tokens.update(_node_tokens(ancestor))
        tokens.update(_node_tokens(node))
        if kind == "paragraph" and tokens & {"footnote", "footnotes", "endnote", "endnotes"}:
            kind = "footnote"
        heading_level = int(node.tag[1]) if re.fullmatch(r"h[1-6]", node.tag) else None
        blocks.append(
            HtmlBlock(
                ordinal=len(blocks),
                kind=normalize_kind(kind, normalized),
                text=normalized,
                heading_level=heading_level,
                html_path=node.path,
                line_start=node.line_start,
                line_end=node.line_end or node.line_start,
                attr_id=_descendant_anchor(node),
                classes=tuple(sorted(_split_tokens(node.attrs.get("class", "")))),
                ancestor_paths=tuple(ancestor.path for ancestor in ancestors),
                structural_tokens=tuple(sorted(tokens)),
            )
        )

    def visit_children(node: HtmlNode, ancestors: tuple[HtmlNode, ...]) -> None:
        inline_parts: list[str] = []

        def flush() -> None:
            text = normalize_text(" ".join(inline_parts))
            inline_parts.clear()
            if text:
                emit(node, "paragraph", text, ancestors)

        for child in node.children or []:
            if isinstance(child, str):
                inline_parts.append(child)
                continue
            if _is_hidden(child):
                continue
            if child.tag in _STRUCTURAL_TAGS or child.tag == "img":
                flush()
                visit(child, ancestors)
            else:
                inline_parts.append(_node_text(child))
        flush()

    def visit(node: HtmlNode, ancestors: tuple[HtmlNode, ...]) -> None:
        if _is_hidden(node):
            return
        if re.fullmatch(r"h[1-6]", node.tag):
            emit(node, "heading", _node_text(node), ancestors)
            return
        if node.tag in _LEAF_BLOCK_TAGS:
            tokens = _node_tokens(node)
            if node.tag == "pre":
                text = _node_preformatted_text(node)
                emit(
                    node,
                    _preformatted_kind(node, text),
                    text,
                    ancestors,
                    preserve_layout=True,
                )
            elif tokens & _VERSE_TOKENS:
                emit(
                    node,
                    "verse",
                    _node_text_with_breaks(node),
                    ancestors,
                    preserve_layout=True,
                )
            else:
                emit(node, "paragraph", _node_text(node), ancestors)
            return
        if node.tag == "blockquote":
            has_blocks = any(
                isinstance(child, HtmlNode) and child.tag in _STRUCTURAL_TAGS
                for child in node.children or []
            )
            if has_blocks:
                visit_children(node, ancestors + (node,))
            else:
                emit(node, "block_quote", _node_text(node), ancestors)
            return
        if node.tag == "li":
            direct = _node_text(node, skip_block_descendants=True)
            if direct:
                emit(node, "list_item", direct, ancestors)
            for child in node.children or []:
                if isinstance(child, HtmlNode) and child.tag in _STRUCTURAL_TAGS:
                    visit(child, ancestors + (node,))
            return
        if node.tag == "table":
            emit(node, "table", _node_text(node), ancestors)
            return
        if node.tag == "img":
            emit(node, "image", _image_label(node), ancestors)
            return

        start = len(blocks)
        next_ancestors = ancestors + ((node,) if node.tag in _CONTAINER_TAGS else ())
        visit_children(node, next_ancestors)
        end = len(blocks)
        if node.tag in _CONTAINER_TAGS and end > start:
            unit_blocks = blocks[start:end]
            heading = next((block for block in unit_blocks if block.kind == "heading"), None)
            title = heading.text if heading else None
            containers.append(
                HtmlContainer(
                    path=node.path,
                    parent_path=next(
                        (ancestor.path for ancestor in reversed(ancestors) if ancestor.tag in _CONTAINER_TAGS),
                        None,
                    ),
                    tag=node.tag,
                    attr_id=node.attrs.get("id") or None,
                    classes=tuple(sorted(_split_tokens(node.attrs.get("class", "")))),
                    structural_tokens=tuple(sorted(_node_tokens(node))),
                    start_block=start,
                    end_block=end,
                    depth=len(ancestors),
                    title=title,
                    heading_level=heading.heading_level if heading else None,
                )
            )

    for child in root.children or []:
        if isinstance(child, HtmlNode):
            visit(child, ())
    return tuple(blocks), tuple(containers)


def _parse_html(source: Path) -> tuple[tuple[HtmlBlock, ...], tuple[HtmlContainer, ...], dict[str, str]]:
    text = source.read_text(encoding="utf-8-sig", errors="replace")
    parser = _TreeParser()
    parser.feed(text)
    parser.close()
    blocks, containers = _extract_native_blocks(parser.root)

    title = ""
    author = ""
    language = ""
    for node in _iter_nodes(parser.root):
        if node.tag == "title" and not title:
            title = _node_text(node)
        if node.tag == "html" and not language:
            language = node.attrs.get("lang", "")
        if node.tag == "meta":
            name = node.attrs.get("name", "").casefold()
            prop = node.attrs.get("property", "").casefold()
            if not author and (name == "author" or prop in {"author", "article:author"}):
                author = node.attrs.get("content", "")
            if not title and prop in {"og:title", "twitter:title"}:
                title = node.attrs.get("content", "")
    return blocks, containers, {"title": title, "author": author, "language": language}


def _iter_nodes(node: HtmlNode) -> Iterable[HtmlNode]:
    yield node
    for child in node.children or []:
        if isinstance(child, HtmlNode):
            yield from _iter_nodes(child)


def _title_role(title: str) -> tuple[str | None, float, tuple[str, ...]]:
    value = normalize_text(title)
    if _LICENSE_RE.search(value):
        return "back_matter", 0.98, ("standardized_title:license",)
    if _BACK_TITLE_RE.fullmatch(value):
        return "back_matter", 0.84, ("standardized_title:back_matter",)
    if _FRONT_TITLE_RE.fullmatch(value):
        return "front_matter", 0.84, ("standardized_title:front_matter",)
    if _PREFIXED_CONTAINER_RE.match(_ascii_fold(value)):
        return "container", 0.82, ("standardized_heading:container",)
    if _PREFIXED_CONTENT_RE.match(_ascii_fold(value)):
        return "content_unit", 0.88, ("standardized_heading:content_unit",)
    return None, 0.0, ()


def _ascii_fold(value: str) -> str:
    folded = unicodedata.normalize("NFKD", str(value or ""))
    return folded.encode("ascii", "ignore").decode("ascii").casefold().strip()


def _container_role(container: HtmlContainer) -> tuple[str | None, float, tuple[str, ...]]:
    tokens = set(container.structural_tokens)
    title_role = _title_role(container.title or "")
    if title_role[0] is not None:
        return title_role
    if tokens & _BACK_TOKENS or container.tag == "footer":
        return "back_matter", 0.96, tuple(f"html_semantic:{token}" for token in sorted(tokens & _BACK_TOKENS)) or ("html_tag:footer",)
    if tokens & _FRONT_TOKENS or container.tag in {"header", "nav"}:
        return "front_matter", 0.94, tuple(f"html_semantic:{token}" for token in sorted(tokens & _FRONT_TOKENS)) or (f"html_tag:{container.tag}",)
    if tokens & _CONTAINER_TOKENS:
        return "container", 0.92, tuple(f"html_semantic:{token}" for token in sorted(tokens & _CONTAINER_TOKENS))
    id_tokens = _split_tokens(container.attr_id or "")
    chapter_id = bool(re.search(r"(?:^|[-_])chap(?:ter)?[-_]?\d+$", container.attr_id or "", re.IGNORECASE))
    if tokens & _CONTENT_TOKENS or chapter_id or id_tokens & _CONTENT_TOKENS:
        if not container.title:
            return None, 0.0, ()
        return "content_unit", 0.96, tuple(f"html_semantic:{token}" for token in sorted((tokens | id_tokens) & _CONTENT_TOKENS)) or ("html_id:chapter_sequence",)
    if container.tag == "article" and container.title:
        return "content_unit", 0.72, ("html_tag:article",)
    return None, 0.0, ()


def _heading_family(block: HtmlBlock) -> str | None:
    value = _ascii_fold(block.text)
    if _ROMAN_RE.fullmatch(value):
        return "roman"
    if _ARABIC_RE.fullmatch(value):
        return "arabic"
    if _PREFIXED_CONTENT_RE.match(value):
        return "prefixed"
    if block.attr_id and re.fullmatch(r"chap(?:ter)?[-_]?\d+", block.attr_id, re.IGNORECASE):
        return "anchored_chapter"
    return None


def _non_overlapping_leaf_content(
    candidates: Sequence[tuple[HtmlContainer, str, float, tuple[str, ...]]],
) -> list[tuple[HtmlContainer, str, float, tuple[str, ...]]]:
    content = [item for item in candidates if item[1] == "content_unit"]

    # Nested HTML wrappers can expose the same block span (for example
    # html > body > article). Prefer the most specific container before
    # removing true outer containers; equal spans are not containment.
    by_span: dict[
        tuple[int, int],
        tuple[HtmlContainer, str, float, tuple[str, ...]],
    ] = {}
    for item in content:
        span = (item[0].start_block, item[0].end_block)
        previous = by_span.get(span)
        rank = (item[2], item[0].path.count("/"))
        if previous is None or rank > (previous[2], previous[0].path.count("/")):
            by_span[span] = item

    unique_content = list(by_span.values())
    leaves = [
        item
        for item in unique_content
        if not any(
            other[0].path != item[0].path
            and item[0].start_block <= other[0].start_block
            and other[0].end_block <= item[0].end_block
            and (
                item[0].start_block < other[0].start_block
                or other[0].end_block < item[0].end_block
            )
            for other in unique_content
        )
    ]
    result: list[tuple[HtmlContainer, str, float, tuple[str, ...]]] = []
    for item in sorted(leaves, key=lambda value: (value[0].start_block, value[0].end_block)):
        if result and item[0].start_block < result[-1][0].end_block:
            continue
        result.append(item)
    return result


def _select_boundaries(
    blocks: Sequence[HtmlBlock],
    containers: Sequence[HtmlContainer],
) -> list[BoundaryCandidate]:
    classified = [
        (container, *(_container_role(container)))
        for container in containers
    ]
    classified = [item for item in classified if item[1] is not None]
    content_containers = _non_overlapping_leaf_content(classified)

    candidates: list[BoundaryCandidate] = []
    for container, role, confidence, evidence in content_containers:
        candidates.append(
            BoundaryCandidate(
                index=container.start_block,
                title=container.title or f"Unit {len(candidates) + 1}",
                role=role,
                confidence=confidence,
                evidence=evidence,
                heading_level=container.heading_level,
                container_path=container.path,
                parent_container_path=container.parent_path,
            )
        )

    if not candidates:
        families: dict[tuple[int, str], list[HtmlBlock]] = {}
        for block in blocks:
            family = _heading_family(block)
            if block.kind == "heading" and block.heading_level is not None and family:
                families.setdefault((block.heading_level, family), []).append(block)
        eligible = [
            (len(items), -level, family, items)
            for (level, family), items in families.items()
            if len(items) >= 2
        ]
        if eligible:
            _count, _neg_level, family, selected = max(eligible)
            for block in selected:
                candidates.append(
                    BoundaryCandidate(
                        index=block.ordinal,
                        title=block.text,
                        role="content_unit",
                        confidence=0.86,
                        evidence=(f"repeated_heading_family:{family}",),
                        heading_level=block.heading_level,
                    )
                )

    content_indexes = sorted(candidate.index for candidate in candidates if candidate.role == "content_unit")
    if content_indexes:
        first_content = content_indexes[0]
        last_content = content_indexes[-1]
        if first_content > 0:
            candidates.append(
                BoundaryCandidate(
                    index=0,
                    title="Front matter",
                    role="front_matter",
                    confidence=0.9,
                    evidence=("position_before_first_content",),
                )
            )

        back_options: list[BoundaryCandidate] = []
        for container, role, confidence, evidence in classified:
            if role == "back_matter" and container.start_block > last_content:
                back_options.append(
                    BoundaryCandidate(
                        index=container.start_block,
                        title=container.title or "Back matter",
                        role=role,
                        confidence=confidence,
                        evidence=evidence,
                        heading_level=container.heading_level,
                        container_path=container.path,
                        parent_container_path=container.parent_path,
                    )
                )
        for block in blocks:
            role, confidence, evidence = _title_role(block.text) if block.kind == "heading" else (None, 0.0, ())
            if role == "back_matter" and block.ordinal > last_content:
                back_options.append(
                    BoundaryCandidate(
                        index=block.ordinal,
                        title=block.text,
                        role=role,
                        confidence=confidence,
                        evidence=evidence,
                        heading_level=block.heading_level,
                    )
                )
        if back_options:
            candidates.append(min(back_options, key=lambda item: item.index))

        levels = {candidate.heading_level for candidate in candidates if candidate.role == "content_unit"}
        levels.discard(None)
        if len(levels) == 1:
            level = next(iter(levels))
            back_start = min(
                (candidate.index for candidate in candidates if candidate.role == "back_matter"),
                default=len(blocks),
            )
            tail = next(
                (
                    block
                    for block in blocks
                    if last_content < block.ordinal < back_start
                    and block.kind == "heading"
                    and block.heading_level == level
                    and _heading_family(block) is None
                ),
                None,
            )
            if tail is not None:
                candidates.append(
                    BoundaryCandidate(
                        index=tail.ordinal,
                        title=tail.text,
                        role="unknown",
                        confidence=0.45,
                        evidence=("same_level_tail_after_content_sequence",),
                        heading_level=tail.heading_level,
                    )
                )
    else:
        repeated_levels = Counter(
            block.heading_level
            for block in blocks
            if block.kind == "heading" and block.heading_level is not None
        )
        levels = [level for level, count in repeated_levels.items() if count >= 2]
        if levels:
            level = min(levels)
            for block in blocks:
                if block.kind == "heading" and block.heading_level == level:
                    role, confidence, evidence = _title_role(block.text)
                    candidates.append(
                        BoundaryCandidate(
                            index=block.ordinal,
                            title=block.text,
                            role=role or "unknown",
                            confidence=confidence or 0.4,
                            evidence=evidence or ("repeated_heading_level_requires_review",),
                            heading_level=block.heading_level,
                        )
                    )

    if not candidates and blocks:
        candidates.append(
            BoundaryCandidate(
                index=0,
                title=next((block.text for block in blocks if block.kind == "heading"), "Document"),
                role="unknown",
                confidence=0.25,
                evidence=("single_unit_fallback",),
            )
        )
    if candidates and min(candidate.index for candidate in candidates) > 0:
        candidates.append(
            BoundaryCandidate(
                index=0,
                title="Unclassified prefix",
                role="unknown",
                confidence=0.35,
                evidence=("exact_cover_prefix_requires_review",),
            )
        )

    precedence = {"back_matter": 5, "content_unit": 4, "container": 3, "front_matter": 2, "unknown": 1}
    by_index: dict[int, BoundaryCandidate] = {}
    for candidate in candidates:
        prior = by_index.get(candidate.index)
        if prior is None or (candidate.confidence, precedence[candidate.role]) > (
            prior.confidence,
            precedence[prior.role],
        ):
            by_index[candidate.index] = candidate
    return [by_index[index] for index in sorted(by_index)]


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
    blocks: Sequence[HtmlBlock],
) -> tuple[NormalizedUnit, ...]:
    path_to_unit: dict[str, str] = {}
    provisional: list[NormalizedUnit] = []
    for order, boundary in enumerate(boundaries):
        end = boundaries[order + 1].index if order + 1 < len(boundaries) else len(blocks)
        if end <= boundary.index:
            continue
        unit_id = f"u{order + 1:04d}_{_slug(boundary.title, fallback='unit')}"
        if boundary.container_path:
            path_to_unit[boundary.container_path] = unit_id
        provisional.append(
            NormalizedUnit(
                unit_id=unit_id,
                order_index=len(provisional),
                title=boundary.title,
                start_block=boundary.index,
                end_block=end,
                role=boundary.role,
                translation_policy=_role_policy(boundary.role),
                parent_unit_id=None,
                confidence=boundary.confidence,
                evidence=boundary.evidence,
                review_required=boundary.role == "unknown",
            )
        )
    result: list[NormalizedUnit] = []
    for unit, boundary in zip(provisional, boundaries):
        result.append(replace(unit, parent_unit_id=path_to_unit.get(boundary.parent_container_path or "")))
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


def _counter_coverage(left: Sequence[str], right: Sequence[str]) -> float:
    if not left:
        return 1.0
    left_counts = Counter(left)
    right_counts = Counter(right)
    matched = sum(min(count, right_counts[token]) for token, count in left_counts.items())
    return matched / sum(left_counts.values())


def _cross_check(
    source: Path,
    blocks: Sequence[HtmlBlock],
    units: Sequence[NormalizedUnit],
    pandoc_executable: str | None,
) -> dict[str, Any]:
    if pandoc_executable is None:
        return {"status": "skipped", "review_required": False}
    try:
        pandoc = run_pandoc(source, executable=pandoc_executable)
    except (AdapterUnavailableError, RuntimeError, ValueError) as exc:
        return {
            "status": "unavailable",
            "review_required": True,
            "reason": str(exc),
        }
    native_all = [token.casefold() for block in blocks for token in TOKEN_RE.findall(block.text)]
    content_ordinals = {
        index
        for unit in units
        if unit.role == "content_unit"
        for index in range(unit.start_block, unit.end_block)
    }
    native_content = [
        token.casefold()
        for block in blocks
        if block.ordinal in content_ordinals
        for token in TOKEN_RE.findall(block.text)
    ]
    pandoc_tokens = [
        token.casefold()
        for block in pandoc.blocks
        for token in TOKEN_RE.findall(block.text)
    ]
    content_coverage = _counter_coverage(native_content, pandoc_tokens)
    return {
        "status": "ok",
        "adapter_version": pandoc.adapter_version,
        "native_block_count": len(blocks),
        "pandoc_block_count": len(pandoc.blocks),
        "native_token_count": len(native_all),
        "native_content_token_count": len(native_content),
        "pandoc_token_count": len(pandoc_tokens),
        "native_all_covered_by_pandoc": round(_counter_coverage(native_all, pandoc_tokens), 6),
        "native_content_covered_by_pandoc": round(content_coverage, 6),
        "pandoc_covered_by_native": round(_counter_coverage(pandoc_tokens, native_all), 6),
        "review_required": bool(native_content and content_coverage < 0.98),
        "threshold": 0.98,
    }


def normalize_html(
    source_path: str | Path,
    *,
    doc_id: str,
    source_language: str = "en",
    target_language: str = "vi",
    pandoc_executable: str | None = "pandoc",
) -> HtmlNormalizationResult:
    source = Path(source_path).resolve()
    if source.suffix.casefold() not in {".html", ".htm"}:
        raise ValueError("HTML normalizer requires a .html or .htm source")
    if not source.is_file():
        raise FileNotFoundError(source)

    blocks, containers, metadata = _parse_html(source)
    if not blocks:
        raise ValueError("HTML source contains no visible canonical text blocks")
    boundaries = _select_boundaries(blocks, containers)
    units = _materialize_units(boundaries, blocks)
    exact_cover = _validate_exact_cover(units, len(blocks))
    cross_check = _cross_check(source, blocks, units, pandoc_executable)
    global_review = bool(cross_check.get("review_required"))
    if global_review:
        units = tuple(
            replace(
                unit,
                review_required=True,
                evidence=unit.evidence + ("pandoc_content_cross_check_failed",),
            )
            if unit.role == "content_unit"
            else unit
            for unit in units
        )

    document_chapters: list[dict[str, Any]] = []
    source_map: list[dict[str, Any]] = []
    for unit in units:
        chapter_id = f"{_slug(doc_id, fallback='doc')}_{unit.unit_id}"
        chapter_blocks: list[dict[str, Any]] = []
        for local_order, block_index in enumerate(range(unit.start_block, unit.end_block)):
            block = blocks[block_index]
            block_id = f"{chapter_id}_b{local_order + 1:04d}"
            canonical_kind = _canonical_document_kind(block.kind)
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
                    "quality_flags": [],
                    "annotations": {},
                }
            )
            source_map.append(
                {
                    "block_id": block_id,
                    "html_path": block.html_path,
                    "html_anchor": block.attr_id,
                    "line_range": [block.line_start, block.line_end],
                    "source_block_kind": block.kind,
                    "ancestor_paths": list(block.ancestor_paths),
                    "provenance_precision": "html_element_line_range_plus_dom_path",
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
            "confidence": round(unit.confidence, 3),
            "evidence": list(unit.evidence),
            "review_required": unit.review_required,
        }
        for index, unit in enumerate(units)
    ]
    translatable = [
        document_chapters[index]["chapter_id"]
        for index, unit in enumerate(units)
        if unit.role == "content_unit" and not unit.review_required
    ]
    manifest = {
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "normalizer_version": NORMALIZER_VERSION,
        "doc_id": doc_id,
        "source": {
            "path": str(source),
            "sha256": source_sha256,
            "format": "html",
        },
        "extractor": {
            "name": "python_html_parser",
            "version": "stdlib",
            "mode": "native_dom_text_blocks",
        },
        "cross_check": cross_check,
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
            "cross_check": cross_check,
        }
    )
    document = {
        "schema_version": DOCUMENT_SCHEMA_VERSION,
        "doc_id": doc_id,
        "metadata": {
            "title": metadata.get("title") or source.stem,
            "author": metadata.get("author") or "",
            "domain": "unknown",
            "genre": "unknown",
            "source_language": metadata.get("language") or source_language,
            "target_language": target_language,
            "source_format": "html",
            "license": "unknown",
            "raw_sha256": source_sha256,
            "extraction_tool": NORMALIZER_VERSION,
            "pipeline_version": NORMALIZER_VERSION,
            "contamination_risk": "medium",
        },
        "chapters": document_chapters,
    }
    return HtmlNormalizationResult(document=document, structure_manifest=manifest)


def write_html_normalization(
    result: HtmlNormalizationResult,
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
    "HtmlNormalizationResult",
    "MANIFEST_SCHEMA_VERSION",
    "NORMALIZER_VERSION",
    "normalize_html",
    "write_html_normalization",
]
