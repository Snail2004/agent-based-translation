from __future__ import annotations

import posixpath
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from urllib.parse import unquote
from xml.etree import ElementTree as ET


EPUB_TYPE_NAMESPACES = {
    "http://www.idpf.org/2007/ops",
    "http://www.idpf.org/2007/ops#",
}


@dataclass(frozen=True)
class EpubTarget:
    file: str
    anchor: str | None = None


@dataclass(frozen=True)
class EpubNavEntry:
    entry_id: str
    title: str
    target: EpubTarget
    depth: int
    parent_id: str | None
    source: str
    type_tokens: tuple[str, ...] = ()


@dataclass(frozen=True)
class EpubSpineItem:
    order: int
    file: str
    linear: bool
    properties: tuple[str, ...]
    type_tokens: tuple[str, ...]


@dataclass(frozen=True)
class EpubPackageIndex:
    rootfile: str
    titles: tuple[str, ...]
    spine: tuple[EpubSpineItem, ...]
    navigation: tuple[EpubNavEntry, ...]
    landmarks: tuple[EpubNavEntry, ...]
    navigation_source: str


def _local_name(tag: str) -> str:
    return str(tag).rsplit("}", 1)[-1]


def _element_text(element: ET.Element | None) -> str:
    if element is None:
        return ""
    return " ".join("".join(element.itertext()).split())


def _tokens(value: str | None) -> set[str]:
    return {
        token.strip().casefold()
        for token in str(value or "").replace(",", " ").split()
        if token.strip()
    }


def _epub_type_tokens(element: ET.Element) -> set[str]:
    values: set[str] = set()
    for key, value in element.attrib.items():
        local = _local_name(key)
        if local in {"type", "role"}:
            values.update(_tokens(value))
    return values


def _safe_member(base: PurePosixPath, href: str | None) -> EpubTarget | None:
    if not href:
        return None
    decoded = unquote(str(href)).replace("\\", "/")
    file_part, separator, anchor = decoded.partition("#")
    joined = posixpath.normpath(str(base.joinpath(file_part)))
    if joined.startswith("../") or joined == ".." or joined.startswith("/"):
        return None
    normalized_anchor = (anchor or None) if separator else None
    return EpubTarget(file=joined, anchor=normalized_anchor)


def _rootfile(archive: zipfile.ZipFile) -> str:
    try:
        root = ET.fromstring(archive.read("META-INF/container.xml"))
    except (KeyError, ET.ParseError) as exc:
        raise ValueError("EPUB container.xml is missing or invalid") from exc
    for element in root.iter():
        if _local_name(element.tag) == "rootfile" and element.attrib.get("full-path"):
            return str(element.attrib["full-path"])
    raise ValueError("EPUB container.xml has no rootfile")


def _manifest(
    opf: ET.Element,
    base: PurePosixPath,
) -> dict[str, dict[str, object]]:
    result: dict[str, dict[str, object]] = {}
    for element in opf.iter():
        if _local_name(element.tag) != "item":
            continue
        item_id = str(element.attrib.get("id") or "")
        target = _safe_member(base, element.attrib.get("href"))
        if not item_id or target is None:
            continue
        result[item_id] = {
            "file": target.file,
            "media_type": str(element.attrib.get("media-type") or ""),
            "properties": tuple(sorted(_tokens(element.attrib.get("properties")))),
        }
    return result


def _document_type_tokens(archive: zipfile.ZipFile, file_name: str) -> tuple[str, ...]:
    if file_name not in archive.namelist():
        return ()
    try:
        root = ET.fromstring(archive.read(file_name))
    except ET.ParseError:
        return ()
    values: set[str] = set()
    body: ET.Element | None = None
    for element in root.iter():
        local = _local_name(element.tag)
        if local in {"html", "body"}:
            values.update(_epub_type_tokens(element))
        if local == "body" and body is None:
            body = element
    if body is not None:
        for element in list(body):
            if _local_name(element.tag) in {"article", "section"}:
                values.update(_epub_type_tokens(element))
    return tuple(sorted(values))


def _nav_entries(
    root: ET.Element,
    *,
    nav_file: str,
    source: str,
    wanted_type: str,
) -> list[EpubNavEntry]:
    base = PurePosixPath(nav_file).parent
    entries: list[EpubNavEntry] = []

    def visit_list(element: ET.Element, depth: int, parent_id: str | None) -> None:
        for child in list(element):
            if _local_name(child.tag) != "li":
                continue
            link = next(
                (node for node in list(child) if _local_name(node.tag) in {"a", "span"}),
                None,
            )
            title = _element_text(link)
            raw_href = link.attrib.get("href") if link is not None else None
            if raw_href and str(raw_href).startswith("#"):
                raw_href = f"{PurePosixPath(nav_file).name}{raw_href}"
            target = _safe_member(base, raw_href)
            entry_id: str | None = None
            if title and target is not None:
                entry_id = f"{source}_{len(entries):04d}"
                entries.append(
                    EpubNavEntry(
                        entry_id=entry_id,
                        title=title,
                        target=target,
                        depth=depth,
                        parent_id=parent_id,
                        source=source,
                        type_tokens=tuple(sorted(_epub_type_tokens(link))) if link is not None else (),
                    )
                )
            nested = next((node for node in list(child) if _local_name(node.tag) == "ol"), None)
            if nested is not None:
                visit_list(nested, depth + 1, entry_id or parent_id)

    for nav in root.iter():
        if _local_name(nav.tag) != "nav":
            continue
        nav_types = _epub_type_tokens(nav)
        if wanted_type not in nav_types:
            continue
        ordered = next((node for node in list(nav) if _local_name(node.tag) == "ol"), None)
        if ordered is not None:
            visit_list(ordered, 0, None)
    return entries


def _ncx_entries(root: ET.Element, *, ncx_file: str) -> list[EpubNavEntry]:
    base = PurePosixPath(ncx_file).parent
    entries: list[EpubNavEntry] = []

    def visit(point: ET.Element, depth: int, parent_id: str | None) -> None:
        label = next(
            (
                child
                for child in point.iter()
                if _local_name(child.tag) == "text"
            ),
            None,
        )
        content = next(
            (child for child in list(point) if _local_name(child.tag) == "content"),
            None,
        )
        title = _element_text(label)
        raw_src = content.attrib.get("src") if content is not None else None
        if raw_src and str(raw_src).startswith("#"):
            raw_src = f"{PurePosixPath(ncx_file).name}{raw_src}"
        target = _safe_member(base, raw_src)
        entry_id: str | None = None
        if title and target is not None:
            entry_id = f"ncx_{len(entries):04d}"
            entries.append(
                EpubNavEntry(
                    entry_id=entry_id,
                    title=title,
                    target=target,
                    depth=depth,
                    parent_id=parent_id,
                    source="ncx",
                )
            )
        for child in list(point):
            if _local_name(child.tag) == "navPoint":
                visit(child, depth + 1, entry_id or parent_id)

    nav_map = next((element for element in root.iter() if _local_name(element.tag) == "navMap"), None)
    if nav_map is not None:
        for point in list(nav_map):
            if _local_name(point.tag) == "navPoint":
                visit(point, 0, None)
    return entries


def read_epub_package(path: str | Path) -> EpubPackageIndex:
    source = Path(path).resolve()
    with zipfile.ZipFile(source) as archive:
        rootfile = _rootfile(archive)
        try:
            opf = ET.fromstring(archive.read(rootfile))
        except (KeyError, ET.ParseError) as exc:
            raise ValueError("EPUB package document is missing or invalid") from exc
        base = PurePosixPath(rootfile).parent
        manifest = _manifest(opf, base)
        titles = tuple(
            title
            for title in (_element_text(element) for element in opf.iter() if _local_name(element.tag) == "title")
            if title
        )

        spine: list[EpubSpineItem] = []
        for element in opf.iter():
            if _local_name(element.tag) != "itemref":
                continue
            item = manifest.get(str(element.attrib.get("idref") or ""))
            if item is None:
                continue
            file_name = str(item["file"])
            spine.append(
                EpubSpineItem(
                    order=len(spine),
                    file=file_name,
                    linear=str(element.attrib.get("linear") or "yes").casefold() != "no",
                    properties=tuple(item["properties"]),
                    type_tokens=_document_type_tokens(archive, file_name),
                )
            )

        nav_files = [
            str(item["file"])
            for item in manifest.values()
            if "nav" in item["properties"]
        ]
        navigation: list[EpubNavEntry] = []
        landmarks: list[EpubNavEntry] = []
        for nav_file in nav_files:
            if nav_file not in archive.namelist():
                continue
            try:
                nav_root = ET.fromstring(archive.read(nav_file))
            except ET.ParseError:
                continue
            navigation = _nav_entries(
                nav_root,
                nav_file=nav_file,
                source="nav",
                wanted_type="toc",
            )
            landmarks = _nav_entries(
                nav_root,
                nav_file=nav_file,
                source="landmark",
                wanted_type="landmarks",
            )
            if navigation:
                break

        navigation_source = "nav" if navigation else "none"
        if not navigation:
            ncx_files = [
                str(item["file"])
                for item in manifest.values()
                if item["media_type"] == "application/x-dtbncx+xml"
            ]
            for ncx_file in ncx_files:
                if ncx_file not in archive.namelist():
                    continue
                try:
                    ncx_root = ET.fromstring(archive.read(ncx_file))
                except ET.ParseError:
                    continue
                navigation = _ncx_entries(ncx_root, ncx_file=ncx_file)
                if navigation:
                    navigation_source = "ncx"
                    break

        guide_entries: list[EpubNavEntry] = []
        for element in opf.iter():
            if _local_name(element.tag) != "reference":
                continue
            target = _safe_member(base, element.attrib.get("href"))
            title = str(element.attrib.get("title") or element.attrib.get("type") or "")
            if target is None or not title:
                continue
            guide_entries.append(
                EpubNavEntry(
                    entry_id=f"guide_{len(guide_entries):04d}",
                    title=title,
                    target=target,
                    depth=0,
                    parent_id=None,
                    source="guide",
                    type_tokens=tuple(sorted(_tokens(element.attrib.get("type")))),
                )
            )
        if not landmarks:
            landmarks = guide_entries

    return EpubPackageIndex(
        rootfile=rootfile,
        titles=titles,
        spine=tuple(spine),
        navigation=tuple(navigation),
        landmarks=tuple(landmarks),
        navigation_source=navigation_source,
    )
