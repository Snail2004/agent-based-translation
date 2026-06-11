import hashlib
import html
import posixpath
import re
import unicodedata
import zipfile
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from xml.etree import ElementTree as ET

from config import DATASET_FILES, EXTRACTION_TOOL, PIPELINE_VERSION, SCHEMA_VERSION
from services.audit_log import log_event
from services.dataset_io import default_review_state, read_json, read_jsonl, write_json_atomic, write_jsonl_atomic
from services.workspace import ProjectError, ensure_project_dirs, ensure_working_files, find_source_file


CHAPTER_RE = re.compile(
    r"^\s*(chapter\s+([0-9]+|[ivxlcdm]+|one|two|three|four|five|six|seven|eight|nine|ten|"
    r"eleven|twelve|thirteen|fourteen|fifteen|sixteen|seventeen|eighteen|nineteen|twenty)\b.*)$",
    re.IGNORECASE,
)
GUTENBERG_START_RE = re.compile(
    r"^\*\*\*\s*START OF TH(?:E|IS) PROJECT GUTENBERG EBOOK.*\*\*\*\s*$",
    re.IGNORECASE | re.MULTILINE,
)
GUTENBERG_END_RE = re.compile(
    r"^\*\*\*\s*END OF TH(?:E|IS) PROJECT GUTENBERG EBOOK.*\*\*\*\s*$",
    re.IGNORECASE | re.MULTILINE,
)
EPUB_GUTENBERG_LICENSE_BLOCK_RE = re.compile(
    r"^\s*(?:"
    r"\*\*\*\s*(?:START|END) OF TH(?:E|IS) PROJECT GUTENBERG"
    r"|THE FULL PROJECT GUTENBERG.*LICENSE"
    r"|This eBook is for the use of anyone anywhere"
    r")",
    re.IGNORECASE,
)
FRONT_MATTER_TYPES = {
    "frontmatter",
    "backmatter",
    "cover",
    "toc",
    "title-page",
    "titlepage",
    "halftitle",
    "halftitlepage",
    "copyright-page",
    "copyright",
    "dedication",
    "epigraph",
    "foreword",
    "preface",
    "introduction",
    "acknowledgements",
    "acknowledgments",
    "colophon",
    "imprint",
    "endnotes",
    "uncopyright",
}
BODY_MATTER_TYPES = {"bodymatter"}
FRONT_BACK_TITLE_KEYS = {
    "titlepage": "titlepage",
    "title page": "titlepage",
    "halftitle": "halftitlepage",
    "halftitlepage": "halftitlepage",
    "halftitle page": "halftitlepage",
    "half title": "halftitlepage",
    "imprint": "imprint",
    "contents": "toc",
    "table of contents": "toc",
    "dedication": "dedication",
    "epigraph": "epigraph",
    "preface": "preface",
    "introduction": "introduction",
    "colophon": "colophon",
    "uncopyright": "uncopyright",
    "endnotes": "endnotes",
}
TOC_MATCH_THRESHOLD = 0.6
TOC_HEADING_RE = re.compile(r"^(contents|table of contents|mục lục)$", re.IGNORECASE)
DOT_LEADER_PAGE_RE = re.compile(r"\s*[.\u2026\u00b7]{2,}\s*\d+\s*$")
TOC_BOILERPLATE_TITLE_RE = re.compile(r"(project gutenberg|license|full license|contents|table of contents)", re.IGNORECASE)


def default_toc_report() -> dict[str, Any]:
    return {
        "toc_source": "none",
        "toc_items": 0,
        "chapters_matched": 0,
        "match_rate": 0.0,
        "fallback_used": True,
        "low_confidence": False,
        "ambiguous_titles": [],
        "ambiguous_count": 0,
    }


class BlockHTMLParser(HTMLParser):
    block_tags = {"h1", "h2", "h3", "p", "li", "blockquote"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.blocks: list[dict[str, str | None]] = []
        self._tag_stack: list[str] = []
        self._current_tag: str | None = None
        self._current_anchor: str | None = None
        self._pending_anchor: str | None = None
        self._chunks: list[str] = []

    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        attrs_map = {name.lower(): value for name, value in attrs}
        anchor = attrs_map.get("id") or attrs_map.get("name")
        if anchor and not self._current_tag:
            self._pending_anchor = normalize_anchor(anchor)
        if tag in self.block_tags:
            self._flush()
            self._current_tag = tag
            self._current_anchor = self._pending_anchor
            self._pending_anchor = None
            self._chunks = []
        self._tag_stack.append(tag)

    def handle_endtag(self, tag):
        tag = tag.lower()
        if self._current_tag == tag:
            self._flush()
        if tag in self._tag_stack:
            self._tag_stack.remove(tag)

    def handle_data(self, data):
        if self._current_tag:
            self._chunks.append(data)

    def _flush(self):
        if not self._current_tag:
            return
        text = normalize_text(" ".join(self._chunks))
        if text:
            self.blocks.append({
                "tag": self._current_tag,
                "text": text,
                "anchor": self._current_anchor,
            })
        self._current_tag = None
        self._current_anchor = None
        self._chunks = []

    def close(self):
        self._flush()
        super().close()


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def normalize_text(text: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(text)).strip()


def normalize_toc_title(text: str) -> str:
    normalized = unicodedata.normalize("NFC", html.unescape(text or ""))
    normalized = re.sub(r"\s+", " ", normalized).strip()
    normalized = DOT_LEADER_PAGE_RE.sub("", normalized).strip()
    return normalized


def normalize_anchor(anchor: str | None) -> str:
    return unicodedata.normalize("NFC", html.unescape(anchor or "")).strip()


def toc_key(text: str) -> str:
    return normalize_toc_title(text).casefold()


def is_toc_boilerplate_title(text: str) -> bool:
    title = normalize_toc_title(text)
    return bool(TOC_BOILERPLATE_TITLE_RE.search(title))


def is_work_title_toc_entry(text: str, package_titles: set[str]) -> bool:
    key = toc_key(text)
    for title in package_titles:
        title_key = toc_key(title)
        if len(title_key) >= 8 and (key == title_key or key.startswith(f"{title_key} by ")):
            return True
    return False


def set_toc_report(
    report: dict[str, Any] | None,
    source: str,
    toc_items: int,
    chapters_matched: int,
    fallback_used: bool,
    low_confidence: bool,
    ambiguous_titles: list[str] | None = None,
    extra: dict[str, Any] | None = None,
) -> None:
    if report is None:
        return
    match_rate = chapters_matched / toc_items if toc_items else 0.0
    ambiguous_titles = ambiguous_titles or []
    report["toc"] = {
        "toc_source": source,
        "toc_items": toc_items,
        "chapters_matched": chapters_matched,
        "match_rate": round(match_rate, 4),
        "fallback_used": fallback_used,
        "low_confidence": low_confidence,
        "ambiguous_titles": ambiguous_titles,
        "ambiguous_count": len(ambiguous_titles),
    }
    if extra:
        report["toc"].update(extra)


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def default_metadata(
    doc_id: str,
    source_path: Path,
    metadata: dict[str, Any] | None = None,
    report: dict[str, Any] | None = None,
) -> dict[str, Any]:
    meta = dict(metadata or {})
    source_format = source_path.suffix.lower().lstrip(".") or "txt"
    declared_format = str(meta.get("source_format") or "").strip().lower()
    if report is not None and declared_format and declared_format != source_format:
        report["source_format_mismatch"] = {
            "declared": declared_format,
            "actual": source_format,
        }
    return {
        "title": meta.get("title") or doc_id,
        "author": meta.get("author") or "",
        "domain": meta.get("domain") or "literature",
        "genre": meta.get("genre") or "novel",
        "source_language": "en",
        "target_language": "vi",
        "source_format": source_format,
        "license": meta.get("license") or "unknown",
        "license_url": meta.get("license_url") or "",
        "source_url": meta.get("source_url") or "",
        "raw_sha256": sha256_file(source_path),
        "retrieved_at": meta.get("retrieved_at") or now_iso(),
        "extraction_tool": EXTRACTION_TOOL,
        "pipeline_version": PIPELINE_VERSION,
        "contamination_risk": meta.get("contamination_risk") or "medium",
    }


def clean_gutenberg_text(raw: str) -> tuple[str, dict[str, Any]]:
    report = {
        "start_marker_found": False,
        "end_marker_found": False,
        "removed_prefix_chars": 0,
        "removed_suffix_chars": 0,
    }
    start = GUTENBERG_START_RE.search(raw)
    text = raw
    if start:
        report["start_marker_found"] = True
        report["removed_prefix_chars"] = start.end()
        text = raw[start.end():]

    end = GUTENBERG_END_RE.search(text)
    if end:
        report["end_marker_found"] = True
        report["removed_suffix_chars"] = len(text) - end.start()
        text = text[:end.start()]
    return text, report


def _legacy_block_type_for(text: str, tag: str | None = None) -> str:
    lowered = text.lower()
    if tag in {"h1", "h2", "h3"} or CHAPTER_RE.match(text):
        return "heading"
    if lowered.startswith("[editor") or lowered.startswith("[translator") or lowered.startswith("note:"):
        return "footnote"
    quote_count = text.count('"') + text.count("“") + text.count("”")
    if text.startswith(('"', "“", "'")) or quote_count >= 2:
        return "dialogue"
    return "paragraph"


OPENING_QUOTE_CHARS = {'"', "'", "\u201c", "\u2018"}
QUOTE_CHARS = {'"', "'", "\u201c", "\u201d", "\u2018", "\u2019"}


def _quoted_ratio(text: str) -> float:
    quoted = 0
    in_quote = False
    for char in text:
        if char in QUOTE_CHARS:
            in_quote = not in_quote
            continue
        if in_quote and not char.isspace():
            quoted += 1
    total = sum(1 for char in text if not char.isspace())
    return quoted / total if total else 0.0


def _is_dialogue_like(text: str) -> bool:
    stripped = text.lstrip()
    if not stripped:
        return False
    if stripped[0] in OPENING_QUOTE_CHARS:
        return True
    quote_count = sum(text.count(char) for char in QUOTE_CHARS)
    return quote_count >= 2 and _quoted_ratio(text) >= 0.6


def block_type_for(text: str, tag: str | None = None) -> str:
    lowered = text.lower()
    if tag in {"h1", "h2", "h3"} or CHAPTER_RE.match(text):
        return "heading"
    if lowered.startswith("[editor") or lowered.startswith("[translator") or lowered.startswith("note:"):
        return "footnote"
    if _is_dialogue_like(text):
        return "dialogue"
    return "paragraph"


def make_block(doc_id: str, chapter_id: str, index: int, text: str, btype: str, opening: bool) -> dict[str, Any]:
    block_id = f"{chapter_id}_b{index:03d}"
    return {
        "block_id": block_id,
        "order_index": index,
        "page_ids": [],
        "block_type": btype,
        "is_chapter_opening": opening,
        "source_text": text,
        "clean_text": text,
        "sentences": [{"sent_id": f"{block_id}_s001", "text": text, "span": [0, len(text)]}],
        "quality_flags": ["ok"],
    }


def _txt_parts(cleaned: str) -> list[dict[str, Any]]:
    normalized = cleaned.replace("\r\n", "\n").replace("\r", "\n")
    parts = []
    for part in re.split(r"\n\s*\n+", normalized):
        raw_part = part.strip()
        if not raw_part:
            continue
        text = normalize_text(raw_part)
        if text:
            parts.append({
                "raw": raw_part,
                "text": text,
                "lines": [normalize_text(line) for line in raw_part.split("\n") if normalize_text(line)],
            })
    return parts


def _split_txt_legacy(parts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    chapters: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None

    def new_chapter(title: str) -> dict[str, Any]:
        chapter = {"title": normalize_text(title), "items": []}
        chapters.append(chapter)
        return chapter

    for part in parts:
        text = part["text"]
        if not text:
            continue
        if CHAPTER_RE.match(text):
            current = new_chapter(text)
            current["items"].append(("heading", text))
            continue
        if current is None:
            current = new_chapter("Chapter 1")
            current["items"].append(("heading", "Chapter 1"))
        current["items"].append((block_type_for(text), text))

    if not chapters:
        current = new_chapter("Chapter 1")
        current["items"].append(("heading", "Chapter 1"))
        current["items"].append(("paragraph", "Empty source"))

    return chapters


def _looks_like_toc_entry(text: str) -> bool:
    value = normalize_toc_title(text)
    if not value or TOC_HEADING_RE.match(value):
        return False
    if len(value) > 120:
        return False
    if len(value.split()) > 16:
        return False
    if re.search(r"[.!?;]$", value) and len(value.split()) > 8:
        return False
    return True


def _find_text_toc(parts: list[dict[str, Any]]) -> tuple[int | None, int, list[str]]:
    toc_index: int | None = None
    for index, part in enumerate(parts):
        if TOC_HEADING_RE.match(part["text"]):
            toc_index = index
            break
    if toc_index is None:
        return None, 0, []

    entries: list[str] = []
    body_start = toc_index + 1
    for index in range(toc_index + 1, len(parts)):
        lines = parts[index]["lines"]
        candidates = [normalize_toc_title(line) for line in lines if _looks_like_toc_entry(line)]
        if not candidates:
            if entries:
                body_start = index
                break
            return toc_index, toc_index + 1, []
        if entries and len(candidates) != len(lines):
            body_start = index
            break
        if entries and toc_key(candidates[0]) in {toc_key(entry) for entry in entries}:
            body_start = index
            break
        entries.extend(candidates)
        body_start = index + 1
    return toc_index, body_start, entries


def _split_txt_with_toc(parts: list[dict[str, Any]], entries: list[str], body_start: int) -> tuple[list[dict[str, Any]], int]:
    expected = [toc_key(entry) for entry in entries]
    matches: list[tuple[int, str]] = []
    cursor = 0
    for index in range(body_start, len(parts)):
        if cursor >= len(expected):
            break
        if toc_key(parts[index]["text"]) == expected[cursor]:
            matches.append((index, normalize_toc_title(entries[cursor])))
            cursor += 1

    chapters: list[dict[str, Any]] = []
    for match_index, (part_index, title) in enumerate(matches):
        next_part = matches[match_index + 1][0] if match_index + 1 < len(matches) else len(parts)
        heading_text = parts[part_index]["text"]
        items = [("heading", heading_text)]
        for body_index in range(part_index + 1, next_part):
            text = parts[body_index]["text"]
            items.append((block_type_for(text), text))
        chapters.append({"title": title or heading_text, "items": items})
    return chapters, len(matches)


def _txt_ambiguous_titles(parts: list[dict[str, Any]], entries: list[str], body_start: int) -> list[str]:
    titles_by_key = {toc_key(entry): normalize_toc_title(entry) for entry in entries}
    counts = {key: 0 for key in titles_by_key}
    for part in parts[body_start:]:
        key = toc_key(part["text"])
        if key in counts:
            counts[key] += 1
    return [titles_by_key[key] for key, count in counts.items() if count > 1]


def split_txt(raw: str, report: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    cleaned, gutenberg_report = clean_gutenberg_text(raw)
    if report is not None:
        report["gutenberg"] = gutenberg_report
    parts = _txt_parts(cleaned)
    _, body_start, toc_entries = _find_text_toc(parts)
    if toc_entries:
        ambiguous_titles = _txt_ambiguous_titles(parts, toc_entries, body_start)
        toc_chapters, matched = _split_txt_with_toc(parts, toc_entries, body_start)
        match_rate = matched / len(toc_entries) if toc_entries else 0.0
        if toc_chapters and match_rate >= TOC_MATCH_THRESHOLD:
            set_toc_report(report, "text", len(toc_entries), matched, False, bool(ambiguous_titles), ambiguous_titles)
            return toc_chapters
        set_toc_report(report, "text", len(toc_entries), matched, True, True, ambiguous_titles)
        return _split_txt_legacy(parts)

    set_toc_report(report, "none", 0, 0, True, False)
    return _split_txt_legacy(parts)


def _epub_rootfile(zf: zipfile.ZipFile) -> str:
    container = ET.fromstring(zf.read("META-INF/container.xml"))
    rootfile = container.find(".//{*}rootfile")
    if rootfile is None or not rootfile.attrib.get("full-path"):
        raise ProjectError("EPUB container does not declare a rootfile.")
    return rootfile.attrib["full-path"]


def _epub_href_target(base: Path, href: str | None) -> dict[str, str] | None:
    if not href:
        return None
    clean_href, _, raw_anchor = html.unescape(href).partition("#")
    clean_href = clean_href.replace("\\", "/")
    if not clean_href:
        return None
    joined = (base / clean_href).as_posix()
    return {
        "file": posixpath.normpath(joined),
        "anchor": normalize_anchor(raw_anchor),
    }


def _epub_path(base: Path, href: str | None) -> str | None:
    target = _epub_href_target(base, href)
    return target["file"] if target else None


def _attr_tokens(elem: ET.Element, local_name: str) -> set[str]:
    values: list[str] = []
    for key, value in elem.attrib.items():
        if key == local_name or key.endswith(f"}}{local_name}"):
            values.append(value)
    tokens: set[str] = set()
    for value in values:
        tokens.update(token.strip().lower() for token in value.split() if token.strip())
    return tokens


def _front_matter_reason(tokens: set[str]) -> str | None:
    matches = sorted(token for token in tokens if token in FRONT_MATTER_TYPES)
    if not matches:
        return None
    return matches[0]


def _body_matter_reason(tokens: set[str]) -> str | None:
    matches = sorted(token for token in tokens if token in BODY_MATTER_TYPES)
    if not matches:
        return None
    return matches[0]


def _epub_xml_root(zf: zipfile.ZipFile, file_name: str) -> ET.Element | None:
    if file_name not in zf.namelist():
        return None
    try:
        return ET.fromstring(zf.read(file_name))
    except ET.ParseError:
        return None


def _epub_document_type_tokens(zf: zipfile.ZipFile, file_name: str) -> set[str]:
    root = _epub_xml_root(zf, file_name)
    if root is None:
        return set()
    tokens = set(_attr_tokens(root, "type"))
    body = root.find(".//{*}body")
    if body is not None:
        tokens.update(_attr_tokens(body, "type"))
    return tokens


def _epub_document_title(zf: zipfile.ZipFile, file_name: str) -> str:
    root = _epub_xml_root(zf, file_name)
    if root is None:
        return ""
    for path in (".//{*}title", ".//{*}h1", ".//{*}h2"):
        title = _element_text(root.find(path))
        if title:
            return title
    return ""


def _front_back_title_reason(title: str) -> str | None:
    key = toc_key(title)
    return FRONT_BACK_TITLE_KEYS.get(key)


def _epub_manifest(opf: ET.Element, base: Path) -> dict[str, dict[str, Any]]:
    manifest: dict[str, dict[str, Any]] = {}
    for item in opf.findall(".//{*}manifest/{*}item"):
        item_id = item.attrib.get("id")
        file_name = _epub_path(base, item.attrib.get("href"))
        if not item_id or not file_name:
            continue
        manifest[item_id] = {
            "file": file_name,
            "href": item.attrib.get("href") or "",
            "media_type": item.attrib.get("media-type") or "",
            "properties": set((item.attrib.get("properties") or "").lower().split()),
        }
    return manifest


def _element_text(elem: ET.Element | None) -> str:
    if elem is None:
        return ""
    return normalize_text(" ".join(elem.itertext()))


def _epub_package_titles(zf: zipfile.ZipFile, rootfile: str) -> set[str]:
    opf = ET.fromstring(zf.read(rootfile))
    titles = {
        normalize_toc_title(_element_text(title))
        for title in opf.findall(".//{*}metadata/{*}title")
        if _element_text(title)
    }
    return titles


def _epub_nav_toc_entries(
    zf: zipfile.ZipFile,
    manifest: dict[str, dict[str, Any]],
) -> list[dict[str, str]]:
    entries: list[dict[str, str]] = []
    nav_files = [item["file"] for item in manifest.values() if "nav" in item.get("properties", set())]
    for nav_file in nav_files:
        if nav_file not in zf.namelist():
            continue
        try:
            nav_root = ET.fromstring(zf.read(nav_file))
        except ET.ParseError:
            continue
        nav_base = Path(nav_file).parent
        navs = []
        fallback_navs = []
        for nav in nav_root.findall(".//{*}nav"):
            tokens = _attr_tokens(nav, "type")
            if "toc" in tokens:
                navs.append(nav)
            elif "landmarks" not in tokens and "page-list" not in tokens:
                fallback_navs.append(nav)
        for nav in navs or fallback_navs:
            for link in nav.findall(".//{*}a"):
                title = _element_text(link)
                target = _epub_href_target(nav_base, link.attrib.get("href"))
                if title and target:
                    entries.append({
                        "title": normalize_toc_title(title),
                        "file": target["file"],
                        "anchor": target["anchor"],
                    })
        if entries:
            break
    return entries


def _epub_ncx_toc_entries(
    zf: zipfile.ZipFile,
    manifest: dict[str, dict[str, Any]],
) -> list[dict[str, str]]:
    entries: list[dict[str, str]] = []
    ncx_files = [item["file"] for item in manifest.values() if item.get("media_type") == "application/x-dtbncx+xml"]
    for ncx_file in ncx_files:
        if ncx_file not in zf.namelist():
            continue
        try:
            ncx_root = ET.fromstring(zf.read(ncx_file))
        except ET.ParseError:
            continue
        ncx_base = Path(ncx_file).parent
        for point in ncx_root.findall(".//{*}navPoint"):
            title = _element_text(point.find(".//{*}navLabel/{*}text"))
            content = point.find(".//{*}content")
            target = _epub_href_target(ncx_base, content.attrib.get("src") if content is not None else None)
            if title and target:
                entries.append({
                    "title": normalize_toc_title(title),
                    "file": target["file"],
                    "anchor": target["anchor"],
                })
        if entries:
            break
    return entries


def _epub_toc_entries(
    zf: zipfile.ZipFile,
    rootfile: str,
) -> tuple[str, list[dict[str, str]]]:
    opf = ET.fromstring(zf.read(rootfile))
    base = Path(rootfile).parent
    manifest = _epub_manifest(opf, base)
    nav_entries = _epub_nav_toc_entries(zf, manifest)
    if nav_entries:
        return "nav", nav_entries
    ncx_entries = _epub_ncx_toc_entries(zf, manifest)
    if ncx_entries:
        return "ncx", ncx_entries
    return "none", []


def _epub_front_matter_files(
    zf: zipfile.ZipFile,
    opf: ET.Element,
    base: Path,
    manifest: dict[str, dict[str, Any]],
) -> tuple[dict[str, str], bool, set[str]]:
    skipped: dict[str, str] = {}
    found_metadata = False
    bodymatter_files: set[str] = set()

    for reference in opf.findall(".//{*}guide/{*}reference"):
        tokens = set()
        if reference.attrib.get("type"):
            tokens.add(reference.attrib["type"].strip().lower())
        reason = _front_matter_reason(tokens)
        file_name = _epub_path(base, reference.attrib.get("href"))
        if file_name and reason:
            found_metadata = True
            skipped[file_name] = f"opf-guide:front-matter:{reason}"

    nav_files = [item["file"] for item in manifest.values() if "nav" in item.get("properties", set())]
    for nav_file in nav_files:
        if nav_file not in zf.namelist():
            continue
        try:
            nav_root = ET.fromstring(zf.read(nav_file))
        except ET.ParseError:
            continue
        nav_base = Path(nav_file).parent
        for nav in nav_root.findall(".//{*}nav"):
            nav_tokens = _attr_tokens(nav, "type")
            if "landmarks" not in nav_tokens:
                continue
            found_metadata = True
            for link in nav.findall(".//{*}a"):
                tokens = _attr_tokens(link, "type") | _attr_tokens(link, "rel")
                reason = _front_matter_reason(tokens)
                body_reason = _body_matter_reason(tokens)
                file_name = _epub_path(nav_base, link.attrib.get("href"))
                if file_name and reason:
                    skipped[file_name] = f"nav-landmarks:front-matter:{reason}"
                if file_name and body_reason:
                    bodymatter_files.add(file_name)

    return skipped, found_metadata, bodymatter_files


def _epub_spine_entries(zf: zipfile.ZipFile, rootfile: str) -> tuple[list[dict[str, Any]], bool]:
    opf = ET.fromstring(zf.read(rootfile))
    base = Path(rootfile).parent
    manifest = _epub_manifest(opf, base)
    front_matter, found_metadata, bodymatter_files = _epub_front_matter_files(zf, opf, base, manifest)
    entries: list[dict[str, Any]] = []
    for itemref in opf.findall(".//{*}spine/{*}itemref"):
        item = manifest.get(itemref.attrib.get("idref"))
        if item:
            file_name = item["file"]
            skip_reason = front_matter.get(file_name)
            if skip_reason is None and "nav" in item.get("properties", set()):
                skip_reason = "manifest:front-matter:toc"
                found_metadata = True
            if skip_reason is None:
                tokens = _epub_document_type_tokens(zf, file_name)
                reason = _front_matter_reason(tokens)
                if reason:
                    skip_reason = f"epub-type:front-matter:{reason}"
                    found_metadata = True
                elif _body_matter_reason(tokens):
                    found_metadata = True
            if skip_reason is None:
                title_reason = _front_back_title_reason(_epub_document_title(zf, file_name))
                if title_reason:
                    skip_reason = f"title-heuristic:front-matter:{title_reason}"
            entries.append({
                "file": file_name,
                "skip_reason": skip_reason,
            })
    bodymatter_indexes = [
        index
        for index, entry in enumerate(entries)
        if entry["file"] in bodymatter_files or _body_matter_reason(_epub_document_type_tokens(zf, entry["file"]))
    ]
    if bodymatter_indexes:
        found_metadata = True
        first_bodymatter = min(bodymatter_indexes)
        for index, entry in enumerate(entries[:first_bodymatter]):
            if entry.get("skip_reason") is None:
                entry["skip_reason"] = "nav-landmarks:before-bodymatter"
    return entries, found_metadata


def _epub_spine_files(zf: zipfile.ZipFile, rootfile: str) -> list[str]:
    entries, _ = _epub_spine_entries(zf, rootfile)
    return [entry["file"] for entry in entries]


def _trim_epub_gutenberg_license_blocks(
    items: list[dict[str, str | None]],
    file_name: str,
) -> tuple[list[dict[str, str | None]], dict[str, Any] | None]:
    for index, item in enumerate(items):
        text = str(item.get("text") or "")
        if EPUB_GUTENBERG_LICENSE_BLOCK_RE.match(text):
            return items[:index], {
                "file": file_name,
                "trigger": text[:120],
                "blocks_removed": len(items) - index,
            }
    return items, None


def _items_to_chapter_items(items: list[dict[str, str | None]]) -> list[tuple[str, str]]:
    return [
        (str(item.get("btype") or "paragraph"), str(item.get("text") or ""))
        for item in items
        if item.get("text")
    ]


def _anchor_indexes(items: list[dict[str, str | None]]) -> dict[str, int]:
    indexes: dict[str, int] = {}
    for index, item in enumerate(items):
        anchor = normalize_anchor(str(item.get("anchor") or ""))
        if anchor and anchor not in indexes:
            indexes[anchor] = index
    return indexes


def _split_epub_items_by_anchors(
    items: list[dict[str, str | None]],
    toc_entries: list[dict[str, str]],
    file_name: str,
) -> tuple[list[dict[str, Any]] | None, dict[str, Any] | None]:
    if len(toc_entries) <= 1:
        return None, None
    indexes = _anchor_indexes(items)
    missing: list[dict[str, str]] = []
    starts: list[int] = []
    for entry_index, entry in enumerate(toc_entries):
        anchor = entry.get("anchor") or ""
        if anchor and anchor in indexes:
            starts.append(indexes[anchor])
        elif not anchor and entry_index == 0:
            starts.append(0)
        else:
            missing.append({"title": entry["title"], "anchor": anchor})
    if missing:
        return None, {
            "file": file_name,
            "reason": "missing-anchor",
            "missing": missing,
        }

    chapters: list[dict[str, Any]] = []
    for entry_index, entry in enumerate(toc_entries):
        start = starts[entry_index]
        end = starts[entry_index + 1] if entry_index + 1 < len(starts) else len(items)
        section_items = items[start:end]
        if not section_items:
            continue
        title = entry["title"]
        chapter_items = _items_to_chapter_items(section_items)
        if not any(btype == "heading" for btype, _ in chapter_items):
            chapter_items.insert(0, ("heading", title))
        chapters.append({
            "title": title,
            "items": chapter_items,
        })
    return chapters, None


def split_epub(path: Path, report: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    chapters: list[dict[str, Any]] = []
    skipped = report.setdefault("skipped", []) if report is not None else []
    with zipfile.ZipFile(path) as zf:
        rootfile = _epub_rootfile(zf)
        package_titles = _epub_package_titles(zf, rootfile)
        entries, found_front_matter_metadata = _epub_spine_entries(zf, rootfile)
        toc_source, toc_entries = _epub_toc_entries(zf, rootfile)
        spine_files = {entry["file"] for entry in entries}
        spine_index = {entry["file"]: index for index, entry in enumerate(entries)}
        toc_front_index = max(
            (index for index, entry in enumerate(entries) if entry.get("skip_reason") and "toc" in entry["skip_reason"]),
            default=-1,
        )
        if toc_entries:
            skipped_spine_reasons = {
                entry["file"]: entry["skip_reason"]
                for entry in entries
                if entry.get("skip_reason")
            }
            toc_entries = [
                entry for entry in toc_entries
                if (
                    (
                        entry["file"] not in skipped_spine_reasons
                        or (
                            "toc" in str(skipped_spine_reasons.get(entry["file"]))
                            and bool(entry.get("anchor"))
                        )
                    )
                    and not is_toc_boilerplate_title(entry["title"])
                    and not is_work_title_toc_entry(entry["title"], package_titles)
                    and (
                        spine_index.get(entry["file"], -1) > toc_front_index
                        or (
                            "toc" in str(skipped_spine_reasons.get(entry["file"]))
                            and bool(entry.get("anchor"))
                        )
                    )
                )
            ]
        title_counts: dict[str, int] = {}
        target_counts: dict[str, int] = {}
        for entry in toc_entries:
            title_counts[toc_key(entry["title"])] = title_counts.get(toc_key(entry["title"]), 0) + 1
            target_key = f"{entry['file']}#{entry.get('anchor') or ''}"
            target_counts[target_key] = target_counts.get(target_key, 0) + 1
        duplicate_titles = sorted({
            normalize_toc_title(entry["title"])
            for entry in toc_entries
            if title_counts.get(toc_key(entry["title"]), 0) > 1
        })
        duplicate_targets = sorted(target for target, count in target_counts.items() if count > 1)
        unresolved_targets = sorted({entry["file"] for entry in toc_entries if entry["file"] not in spine_files})
        matched_toc_items = sum(1 for entry in toc_entries if entry["file"] in spine_files)
        toc_match_rate = matched_toc_items / len(toc_entries) if toc_entries else 0.0
        use_toc = bool(toc_entries) and toc_match_rate >= TOC_MATCH_THRESHOLD
        if toc_entries:
            toc_ambiguous = bool(duplicate_titles or duplicate_targets or unresolved_targets)
            set_toc_report(
                report,
                toc_source,
                len(toc_entries),
                matched_toc_items,
                not use_toc,
                (not use_toc) or toc_ambiguous,
                duplicate_titles,
                {
                    "duplicate_targets": duplicate_targets,
                    "unresolved_targets": unresolved_targets,
                },
            )
        else:
            set_toc_report(report, "none", 0, 0, True, False)
        if report is not None:
            heuristic_skips = [
                entry
                for entry in entries
                if str(entry.get("skip_reason") or "").startswith("title-heuristic:")
            ]
            if heuristic_skips:
                report["toc"]["low_confidence"] = True
                report["toc"]["heuristic_front_back_files"] = [
                    {"file": entry["file"], "reason": entry["skip_reason"]}
                    for entry in heuristic_skips
                ]
        toc_files = {entry["file"] for entry in toc_entries}
        toc_titles_by_file: dict[str, str] = {}
        toc_entries_by_file: dict[str, list[dict[str, str]]] = {}
        for entry in toc_entries:
            toc_titles_by_file.setdefault(entry["file"], entry["title"])
            toc_entries_by_file.setdefault(entry["file"], []).append(entry)
        if report is not None:
            report["front_matter_metadata"] = found_front_matter_metadata
        for entry in entries:
            file_name = entry["file"]
            file_toc_entries = toc_entries_by_file.get(file_name, []) if use_toc else []
            if entry.get("skip_reason") and not (
                "toc" in str(entry.get("skip_reason")) and file_toc_entries
            ):
                skipped.append({"file": file_name, "reason": entry["skip_reason"]})
                continue
            if use_toc and file_name not in toc_files:
                skipped.append({"file": file_name, "reason": "not-in-toc"})
                continue
            if file_name not in zf.namelist():
                continue
            raw = zf.read(file_name).decode("utf-8", errors="replace")
            parser = BlockHTMLParser()
            parser.feed(raw)
            parser.close()
            items = [
                {
                    "btype": block_type_for(str(block["text"]), str(block["tag"])),
                    "text": str(block["text"]),
                    "anchor": normalize_anchor(str(block.get("anchor") or "")),
                }
                for block in parser.blocks
            ]
            items, trim_details = _trim_epub_gutenberg_license_blocks(items, file_name)
            if trim_details and report is not None:
                report.setdefault("gutenberg", {}).setdefault("epub_body_trimmed", []).append(trim_details)
            if not items:
                continue
            anchor_chapters, anchor_error = _split_epub_items_by_anchors(items, file_toc_entries, file_name)
            if anchor_error and report is not None:
                report["toc"]["low_confidence"] = True
                report["toc"].setdefault("anchor_split_failed", []).append(anchor_error)
            if anchor_chapters:
                chapters.extend(anchor_chapters)
                continue
            title_override = toc_titles_by_file.get(file_name) if use_toc else None
            chapter_items = _items_to_chapter_items(items)
            heading = title_override or next((text for btype, text in chapter_items if btype == "heading"), Path(file_name).stem)
            if not any(btype == "heading" for btype, _ in chapter_items):
                chapter_items.insert(0, ("heading", heading))
            chapters.append({"title": heading, "items": chapter_items})
    if not chapters:
        raise ProjectError("No readable text blocks found in EPUB.")
    return chapters


def chapters_to_document(doc_id: str, metadata: dict[str, Any], chapters_raw: list[dict[str, Any]]) -> dict[str, Any]:
    chapters = []
    for cidx, source_chapter in enumerate(chapters_raw, 1):
        chapter_id = f"{doc_id}_ch{cidx:02d}"
        blocks = []
        opening_seen = False
        for bidx, (btype, text) in enumerate(source_chapter["items"], 1):
            opening = False
            if btype != "heading" and not opening_seen:
                opening = True
                opening_seen = True
            blocks.append(make_block(doc_id, chapter_id, bidx, text, btype, opening))
        chapters.append({
            "chapter_id": chapter_id,
            "order_index": cidx,
            "title": source_chapter.get("title") or f"Chapter {cidx}",
            "blocks": blocks,
        })
    return {
        "schema_version": SCHEMA_VERSION,
        "doc_id": doc_id,
        "metadata": metadata,
        "chapters": chapters,
    }


def load_project_metadata(project_path: Path) -> dict[str, Any]:
    meta_path = project_path / "working" / "project_meta.json"
    if meta_path.exists():
        return read_json(meta_path).get("metadata", {})
    document_path = project_path / "canonical" / DATASET_FILES["document"]
    if document_path.exists():
        return read_json(document_path).get("metadata", {})
    return {}


def write_empty_sidecars(project_path: Path, overwrite: bool = False) -> None:
    canonical = project_path / "canonical"
    for key in ("glossary", "entities", "chapter_summaries", "manual_reference_subset"):
        path = canonical / DATASET_FILES[key]
        if overwrite or not path.exists():
            write_jsonl_atomic(path, [])


def _annotation_reasons(project_path: Path) -> list[str]:
    canonical = project_path / "canonical"
    reasons: list[str] = []
    for key, label in (
        ("glossary", "glossary terms"),
        ("entities", "entities"),
        ("chapter_summaries", "chapter summaries"),
        ("manual_reference_subset", "reference translations"),
    ):
        path = canonical / DATASET_FILES[key]
        if read_jsonl(path):
            reasons.append(label)

    drafts_path = project_path / "working" / "drafts.json"
    if drafts_path.exists() and read_json(drafts_path).get("references"):
        reasons.append("reference drafts")

    review_path = project_path / "working" / "review_state.json"
    if review_path.exists():
        review_state = read_json(review_path)
        blocks = review_state.get("blocks", {})
        if any(value.get("reviewed") for value in blocks.values() if isinstance(value, dict)):
            reasons.append("reviewed blocks")

    return reasons


def _reset_working_after_extract(project_path: Path, document: dict[str, Any], reset_drafts: bool = False) -> None:
    write_json_atomic(project_path / "working" / "review_state.json", default_review_state(document))
    if reset_drafts:
        write_json_atomic(project_path / "working" / "drafts.json", {"references": {}})


def write_job(project_path: Path, job: dict[str, Any]) -> None:
    path = project_path / "working" / "jobs" / f"{job['job_id']}.json"
    write_json_atomic(path, job)


def read_job(project_path: Path, job_id: str) -> dict[str, Any]:
    path = project_path / "working" / "jobs" / f"{job_id}.json"
    if not path.exists():
        raise ProjectError(f"Job not found: {job_id}")
    return read_json(path)


def extract_project(
    project_path: Path,
    doc_id: str,
    overwrite: bool = False,
    force: bool = False,
    user: str = "local",
) -> dict[str, Any]:
    dirs = ensure_project_dirs(doc_id)
    ensure_working_files(project_path)
    source_path = find_source_file(project_path)
    if source_path is None:
        raise ProjectError("No TXT/EPUB source file found in raw/.")
    document_path = dirs["canonical"] / DATASET_FILES["document"]
    if document_path.exists() and not overwrite:
        raise ProjectError("Re-extracting can overwrite current document draft. Confirm overwrite first.")
    annotation_reasons = _annotation_reasons(project_path) if document_path.exists() else []
    if document_path.exists() and overwrite and annotation_reasons and not force:
        raise ProjectError(
            "annotations_present: re-extract is blocked because existing annotation data depends on current block IDs "
            f"({', '.join(annotation_reasons)})."
        )

    job_id = f"extract_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
    source_format = source_path.suffix.lower().lstrip(".") or "txt"
    report: dict[str, Any] = {
        "schema_version": "1.0",
        "dataset_schema_version": SCHEMA_VERSION,
        "pipeline_version": PIPELINE_VERSION,
        "source_file": source_path.name,
        "source_format": source_format,
        "generated_at": now_iso(),
        "gutenberg": {
            "start_marker_found": False,
            "end_marker_found": False,
            "removed_prefix_chars": 0,
            "removed_suffix_chars": 0,
        },
        "front_matter_metadata": False,
        "skipped": [],
        "toc": default_toc_report(),
        "chapters": 0,
        "blocks": 0,
    }
    job = {
        "job_id": job_id,
        "type": "extract",
        "status": "running",
        "started_at": now_iso(),
        "finished_at": None,
        "message": "",
    }
    write_job(project_path, job)

    try:
        if source_path.suffix.lower() == ".txt":
            raw = source_path.read_text(encoding="utf-8", errors="replace")
            chapters_raw = split_txt(raw, report)
        elif source_path.suffix.lower() == ".epub":
            chapters_raw = split_epub(source_path, report)
        else:
            raise ProjectError("Only .txt and .epub sources are supported in MVP.")
        metadata = default_metadata(doc_id, source_path, load_project_metadata(project_path), report)
        document = chapters_to_document(doc_id, metadata, chapters_raw)
        write_json_atomic(document_path, document)
        write_empty_sidecars(project_path, overwrite=bool(force and annotation_reasons))
        ensure_working_files(project_path, document)
        _reset_working_after_extract(project_path, document, reset_drafts=bool(force and annotation_reasons))
        report["chapters"] = len(document["chapters"])
        report["blocks"] = sum(len(ch["blocks"]) for ch in document["chapters"])
        write_json_atomic(project_path / "working" / "extraction_report.json", report)
        job.update({
            "status": "done",
            "finished_at": now_iso(),
            "message": f"Extracted {sum(len(ch['blocks']) for ch in document['chapters'])} blocks in {len(document['chapters'])} chapters",
            "document": {"chapters": len(document["chapters"]), "blocks": sum(len(ch["blocks"]) for ch in document["chapters"])},
            "extraction_report": {
                "path": "working/extraction_report.json",
                "skipped": len(report.get("skipped", [])),
                "source_format_mismatch": report.get("source_format_mismatch"),
            },
        })
        write_job(project_path, job)
        if report.get("source_format_mismatch"):
            log_event(project_path, "extract_warning", {
                "job_id": job_id,
                "warning": "source_format_mismatch",
                "source_format_mismatch": report["source_format_mismatch"],
            }, user)
        log_event(project_path, "extract", {"job_id": job_id, "status": "done"}, user)
        return job
    except Exception as exc:
        job.update({"status": "failed", "finished_at": now_iso(), "message": str(exc)})
        write_job(project_path, job)
        log_event(project_path, "extract", {"job_id": job_id, "status": "failed", "message": str(exc)}, user)
        raise
