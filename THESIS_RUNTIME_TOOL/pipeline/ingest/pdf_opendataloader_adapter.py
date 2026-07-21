from __future__ import annotations

import hashlib
import importlib.metadata
import json
import math
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable


EXPECTED_OPENDATALOADER_PDF_VERSION = "2.4.3"
ADAPTER_VERSION = "opendataloader_pdf_adapter_v1"

_ROOT_FIELDS = {
    "file name",
    "number of pages",
    "author",
    "title",
    "creation date",
    "modification date",
    "kids",
}
_NODE_TYPES = {
    "caption",
    "footer",
    "formula",
    "header",
    "heading",
    "image",
    "line art",
    "list",
    "list item",
    "paragraph",
    "table",
    "table cell",
    "table row",
    "text",
    "text block",
}
_CHILD_FIELDS = ("kids", "list items", "rows", "cells")
_POSITIONED_TYPES = _NODE_TYPES - {"table row"}


class PdfAdapterError(ValueError):
    pass


@dataclass(frozen=True)
class PdfExtraction:
    payload: dict[str, Any]
    package_version: str
    java_version: str
    adapter_version: str
    raw_json_sha256: str


ConvertExecutor = Callable[[Path, Path], None]


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _installed_package_version() -> str:
    try:
        return importlib.metadata.version("opendataloader-pdf")
    except importlib.metadata.PackageNotFoundError as exc:
        raise PdfAdapterError(
            "opendataloader-pdf is unavailable; install the pinned 2.4.3 package"
        ) from exc


def _installed_java_version() -> str:
    try:
        completed = subprocess.run(
            ["java", "-version"],
            check=False,
            capture_output=True,
            text=True,
            timeout=15,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise PdfAdapterError("Java is unavailable for opendataloader-pdf") from exc
    output = "\n".join(part for part in (completed.stderr, completed.stdout) if part)
    first_line = next((line.strip() for line in output.splitlines() if line.strip()), "")
    if completed.returncode != 0 or not first_line:
        raise PdfAdapterError("Java version probe failed")
    match = re.search(r'version\s+"([^"]+)"', first_line)
    return match.group(1) if match else first_line


def _default_convert(source: Path, output_dir: Path) -> None:
    try:
        from opendataloader_pdf import convert
    except ImportError as exc:
        raise PdfAdapterError("opendataloader-pdf public API import failed") from exc
    convert(
        input_path=str(source),
        output_dir=str(output_dir),
        format=["json"],
        quiet=True,
        include_header_footer=True,
    )


def _finite_number(value: Any, *, owner: str) -> float:
    if isinstance(value, bool):
        raise PdfAdapterError(f"{owner} must be numeric")
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise PdfAdapterError(f"{owner} must be numeric") from exc
    if not math.isfinite(number):
        raise PdfAdapterError(f"{owner} must be finite")
    return number


def _validate_bbox(value: Any, *, owner: str) -> None:
    if not isinstance(value, list) or len(value) != 4:
        raise PdfAdapterError(f"{owner} must contain four coordinates")
    x0, y0, x1, y1 = [
        _finite_number(part, owner=f"{owner}[{index}]")
        for index, part in enumerate(value)
    ]
    if x1 <= x0 or y1 <= y0:
        raise PdfAdapterError(f"{owner} must have positive area")


def _validate_node(node: Any, *, path: str, page_count: int) -> None:
    if not isinstance(node, dict):
        raise PdfAdapterError(f"{path} must be an object")
    node_type = str(node.get("type") or "").strip().casefold()
    if node_type not in _NODE_TYPES:
        raise PdfAdapterError(f"{path}.type is unsupported: {node_type or '<empty>'}")

    if node_type in _POSITIONED_TYPES:
        page_number = node.get("page number")
        if (
            isinstance(page_number, bool)
            or not isinstance(page_number, int)
            or page_number < 1
            or page_number > page_count
        ):
            raise PdfAdapterError(f"{path}.page number is invalid")
        _validate_bbox(node.get("bounding box"), owner=f"{path}.bounding box")

    content = node.get("content")
    if content is not None and not isinstance(content, str):
        raise PdfAdapterError(f"{path}.content must be a string or null")

    for field in _CHILD_FIELDS:
        children = node.get(field)
        if children is None:
            continue
        if not isinstance(children, list):
            raise PdfAdapterError(f"{path}.{field} must be a list")
        for index, child in enumerate(children):
            _validate_node(
                child,
                path=f"{path}.{field}[{index}]",
                page_count=page_count,
            )


def _validate_payload(payload: Any, *, source: Path) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise PdfAdapterError("OpenDataLoader JSON root must be an object")
    actual_fields = set(payload)
    if actual_fields != _ROOT_FIELDS:
        raise PdfAdapterError(
            "OpenDataLoader JSON root fields drifted; "
            f"missing={sorted(_ROOT_FIELDS - actual_fields)}, "
            f"extra={sorted(actual_fields - _ROOT_FIELDS)}"
        )
    if payload.get("file name") != source.name:
        raise PdfAdapterError("OpenDataLoader JSON file name differs from the source")
    page_count = payload.get("number of pages")
    if isinstance(page_count, bool) or not isinstance(page_count, int) or page_count < 1:
        raise PdfAdapterError("OpenDataLoader page count must be a positive integer")
    for field in ("author", "title", "creation date", "modification date"):
        if payload.get(field) is not None and not isinstance(payload.get(field), str):
            raise PdfAdapterError(f"OpenDataLoader root {field!r} must be string or null")
    kids = payload.get("kids")
    if not isinstance(kids, list):
        raise PdfAdapterError("OpenDataLoader root kids must be a list")
    for index, node in enumerate(kids):
        _validate_node(node, path=f"root.kids[{index}]", page_count=page_count)
    return payload


def extract_pdf(
    source_path: str | Path,
    *,
    convert_executor: ConvertExecutor | None = None,
    package_version: str | None = None,
    java_version: str | None = None,
) -> PdfExtraction:
    source = Path(source_path).resolve()
    if source.suffix.casefold() != ".pdf":
        raise PdfAdapterError("OpenDataLoader PDF adapter requires a .pdf source")
    if not source.is_file():
        raise FileNotFoundError(source)

    resolved_package_version = package_version or _installed_package_version()
    if resolved_package_version != EXPECTED_OPENDATALOADER_PDF_VERSION:
        raise PdfAdapterError(
            "opendataloader-pdf version drift; "
            f"expected {EXPECTED_OPENDATALOADER_PDF_VERSION}, "
            f"found {resolved_package_version}"
        )
    resolved_java_version = java_version or _installed_java_version()
    if not isinstance(resolved_java_version, str) or not resolved_java_version.strip():
        raise PdfAdapterError("Java version identity is missing")

    executor = convert_executor or _default_convert
    with tempfile.TemporaryDirectory(prefix="thesis_odl_pdf_") as temporary:
        workspace = Path(temporary)
        source_sha256 = _file_sha256(source)
        staged_source = workspace / f"source_{source_sha256[:16]}.pdf"
        shutil.copyfile(source, staged_source)
        if _file_sha256(staged_source) != source_sha256:
            raise PdfAdapterError("staged PDF differs from the original source")

        output_dir = workspace / "parser_output"
        output_dir.mkdir()
        executor(staged_source, output_dir)
        json_paths = sorted(output_dir.glob("*.json"))
        if len(json_paths) != 1:
            raise PdfAdapterError(
                "OpenDataLoader must emit exactly one top-level JSON artifact"
            )
        raw_json = json_paths[0].read_bytes()
        try:
            payload = json.loads(raw_json.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise PdfAdapterError("OpenDataLoader emitted malformed UTF-8 JSON") from exc
        validated = _validate_payload(payload, source=staged_source)

    return PdfExtraction(
        payload=validated,
        package_version=resolved_package_version,
        java_version=resolved_java_version.strip(),
        adapter_version=ADAPTER_VERSION,
        raw_json_sha256=hashlib.sha256(raw_json).hexdigest(),
    )


__all__ = [
    "ADAPTER_VERSION",
    "EXPECTED_OPENDATALOADER_PDF_VERSION",
    "PdfAdapterError",
    "PdfExtraction",
    "extract_pdf",
]
