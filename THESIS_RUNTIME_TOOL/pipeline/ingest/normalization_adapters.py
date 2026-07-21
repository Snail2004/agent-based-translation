from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Iterable, Iterator, Sequence

from pipeline.ingest.normalization_ir import (
    AdapterResult,
    ObservedBlock,
    ObservedUnit,
    normalize_kind,
    normalize_text,
    segment_units,
    source_format_for_path,
)


class AdapterUnavailableError(RuntimeError):
    pass


def _run_json_worker(command: Sequence[str], *, cwd: Path | None = None) -> dict[str, Any]:
    environment = dict(os.environ)
    environment["PYTHONUTF8"] = "1"
    completed = subprocess.run(
        list(command),
        cwd=str(cwd) if cwd else None,
        env=environment,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip() or f"exit {completed.returncode}"
        raise RuntimeError(detail)
    try:
        return json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Worker returned invalid JSON: {completed.stdout[:300]!r}") from exc


def run_app_current(source_path: str | Path) -> AdapterResult:
    source = Path(source_path).resolve()
    started = time.perf_counter()
    worker = Path(__file__).with_name("normalization_app_worker.py")
    payload = _run_json_worker([sys.executable, str(worker), str(source)])
    blocks: list[ObservedBlock] = []
    units: list[ObservedUnit] = []
    for chapter in payload.get("chapters") or []:
        unit_ordinals: list[int] = []
        for item_index, item in enumerate(chapter.get("items") or []):
            kind, text = item
            ordinal = len(blocks)
            unit_ordinals.append(ordinal)
            blocks.append(
                ObservedBlock(
                    ordinal=ordinal,
                    kind=str(kind),
                    text=str(text),
                    heading_level=1 if str(kind) == "heading" else None,
                    source_ref=f"app-current:/chapters/{len(units)}/items/{item_index}",
                    native_provenance=False,
                )
            )
        if unit_ordinals:
            units.append(
                ObservedUnit(
                    ordinal=len(units),
                    title=str(chapter.get("title") or f"Unit {len(units) + 1}"),
                    unit_kind="parser_chapter",
                    boundary_level=(payload.get("report") or {}).get("structure", {}).get("chapter_boundary_level"),
                    block_ordinals=tuple(unit_ordinals),
                )
            )
    report = payload.get("report") or {}
    warnings = [
        f"skipped:{entry.get('file')}:{entry.get('reason')}"
        for entry in report.get("skipped") or []
    ]
    if report.get("toc", {}).get("low_confidence"):
        warnings.append("toc_low_confidence")
    return AdapterResult(
        adapter="app_current",
        adapter_version=str(payload.get("adapter_version") or "unknown"),
        source_path=str(source),
        source_format=source_format_for_path(source),
        blocks=tuple(blocks),
        units=tuple(units),
        warnings=tuple(warnings),
        metadata={"extraction_report": report},
        duration_seconds=time.perf_counter() - started,
    )


def _inline_text(inlines: Iterable[dict[str, Any]]) -> str:
    parts: list[str] = []
    for inline in inlines or []:
        tag = inline.get("t")
        content = inline.get("c")
        if tag == "Str":
            parts.append(str(content))
        elif tag in {"Space", "SoftBreak", "LineBreak"}:
            parts.append(" ")
        elif tag in {"Code", "Math"}:
            parts.append(str(content[-1] if isinstance(content, list) else content))
        elif tag in {"Emph", "Strong", "Strikeout", "SmallCaps", "Superscript", "Subscript"}:
            parts.append(_inline_text(content or []))
        elif tag in {"Link", "Image", "Span", "Quoted", "Cite"}:
            nested = []
            if isinstance(content, list):
                for value in content:
                    if isinstance(value, list) and value and isinstance(value[0], dict):
                        nested = value
                        break
            parts.append(_inline_text(nested))
        elif tag == "Note":
            parts.append(_pandoc_blocks_text(content or []))
    return normalize_text("".join(parts))


def _pandoc_blocks_text(blocks: Iterable[dict[str, Any]]) -> str:
    texts: list[str] = []
    for block in blocks or []:
        tag = block.get("t")
        content = block.get("c")
        if tag in {"Para", "Plain"}:
            texts.append(_inline_text(content or []))
        elif tag == "Header":
            texts.append(_inline_text(content[2] if isinstance(content, list) and len(content) >= 3 else []))
        elif tag == "CodeBlock":
            texts.append(str(content[1] if isinstance(content, list) and len(content) >= 2 else ""))
        elif tag == "Div":
            texts.append(_pandoc_blocks_text(content[1] if isinstance(content, list) and len(content) >= 2 else []))
        elif tag == "BlockQuote":
            texts.append(_pandoc_blocks_text(content or []))
        elif tag in {"BulletList", "OrderedList"}:
            items = content[1] if tag == "OrderedList" and isinstance(content, list) else content
            for item in items or []:
                texts.append(_pandoc_blocks_text(item))
    return normalize_text(" ".join(text for text in texts if text))


def _walk_pandoc_blocks(
    blocks: Iterable[dict[str, Any]],
    *,
    path: str = "/blocks",
) -> Iterator[tuple[str, str, int | None, str]]:
    for index, block in enumerate(blocks or []):
        tag = str(block.get("t") or "")
        content = block.get("c")
        ref = f"pandoc:{path}/{index}"
        if tag == "Header":
            level = int(content[0])
            text = _inline_text(content[2])
            if text:
                yield "heading", text, level, ref
        elif tag in {"Para", "Plain"}:
            text = _inline_text(content or [])
            if text:
                only_image = bool(content) and all(item.get("t") in {"Image", "Space", "SoftBreak"} for item in content)
                yield ("image" if only_image else "paragraph"), text, None, ref
        elif tag == "CodeBlock":
            text = str(content[1] if isinstance(content, list) and len(content) >= 2 else "")
            if text.strip():
                yield "code", text, None, ref
        elif tag == "BlockQuote":
            text = _pandoc_blocks_text(content or [])
            if text:
                yield "block_quote", text, None, ref
        elif tag == "Div":
            children = content[1] if isinstance(content, list) and len(content) >= 2 else []
            yield from _walk_pandoc_blocks(children, path=f"{path}/{index}/div")
        elif tag in {"BulletList", "OrderedList"}:
            items = content[1] if tag == "OrderedList" and isinstance(content, list) else content
            for item_index, item in enumerate(items or []):
                text = _pandoc_blocks_text(item)
                if text:
                    yield "list_item", text, None, f"{ref}/items/{item_index}"
        elif tag == "DefinitionList":
            for item_index, item in enumerate(content or []):
                term = _inline_text(item[0] if item else [])
                definitions = " ".join(_pandoc_blocks_text(value) for value in (item[1] if len(item) > 1 else []))
                text = normalize_text(f"{term}: {definitions}" if definitions else term)
                if text:
                    yield "list_item", text, None, f"{ref}/definitions/{item_index}"
        elif tag == "Table":
            text = _pandoc_blocks_text(_nested_pandoc_blocks(content))
            if text:
                yield "table", text, None, ref
        elif tag == "Figure":
            children = content[-1] if isinstance(content, list) and content else []
            yield from _walk_pandoc_blocks(children, path=f"{path}/{index}/figure")
        elif tag == "LineBlock":
            text = " ".join(_inline_text(line) for line in content or [])
            if normalize_text(text):
                yield "paragraph", text, None, ref


def _nested_pandoc_blocks(value: Any) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    if isinstance(value, dict):
        if "t" in value:
            found.append(value)
        else:
            for nested in value.values():
                found.extend(_nested_pandoc_blocks(nested))
    elif isinstance(value, list):
        for nested in value:
            found.extend(_nested_pandoc_blocks(nested))
    return found


def _pandoc_reader(source_format: str) -> str:
    return {
        "epub": "epub",
        "markdown": "markdown",
        "html": "html",
        "txt": "markdown",
    }[source_format]


def run_pandoc(source_path: str | Path, *, executable: str = "pandoc") -> AdapterResult:
    source = Path(source_path).resolve()
    source_format = source_format_for_path(source)
    started = time.perf_counter()
    try:
        version_result = subprocess.run(
            [executable, "--version"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=True,
        )
    except (FileNotFoundError, subprocess.CalledProcessError) as exc:
        raise AdapterUnavailableError(f"Pandoc unavailable: {exc}") from exc
    completed = subprocess.run(
        [executable, str(source), "-f", _pandoc_reader(source_format), "-t", "json"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError(completed.stderr.strip() or f"Pandoc exited {completed.returncode}")
    document = json.loads(completed.stdout)
    observed = list(_walk_pandoc_blocks(document.get("blocks") or []))
    blocks = tuple(
        ObservedBlock(
            ordinal=index,
            kind=kind,
            text=text,
            heading_level=heading_level,
            source_ref=source_ref,
            native_provenance=False,
        )
        for index, (kind, text, heading_level, source_ref) in enumerate(observed)
    )
    return AdapterResult(
        adapter="pandoc",
        adapter_version=version_result.stdout.splitlines()[0].strip(),
        source_path=str(source),
        source_format=source_format,
        blocks=blocks,
        units=segment_units(blocks, fallback_title=source.stem),
        metadata={
            "pandoc_api_version": document.get("pandoc-api-version"),
            "metadata_keys": sorted((document.get("meta") or {}).keys()),
        },
        duration_seconds=time.perf_counter() - started,
    )


def run_docling(
    source_path: str | Path,
    *,
    python_executable: str | Path,
) -> AdapterResult:
    source = Path(source_path).resolve()
    python_path = Path(python_executable).resolve()
    if not python_path.exists():
        raise AdapterUnavailableError(f"Docling Python does not exist: {python_path}")
    probe = subprocess.run(
        [str(python_path), "-c", "import docling"],
        capture_output=True,
        text=True,
        check=False,
    )
    if probe.returncode != 0:
        raise AdapterUnavailableError(probe.stderr.strip() or "Docling import failed")
    started = time.perf_counter()
    worker = Path(__file__).with_name("normalization_docling_worker.py")
    payload = _run_json_worker([str(python_path), str(worker), str(source)])
    observed_items = [
        item
        for item in payload.get("blocks") or []
        if normalize_text(str(item.get("text") or ""), str(item.get("kind") or "paragraph"))
    ]
    blocks = tuple(
        ObservedBlock(
            ordinal=index,
            kind=str(item.get("kind") or "paragraph"),
            text=str(item.get("text") or ""),
            heading_level=item.get("heading_level"),
            source_ref=item.get("source_ref"),
            native_provenance=bool(item.get("native_provenance")),
        )
        for index, item in enumerate(observed_items)
    )
    warnings = [
        f"skipped_label:{label}:{count}"
        for label, count in sorted((payload.get("skipped_labels") or {}).items())
    ]
    return AdapterResult(
        adapter="docling",
        adapter_version=str(payload.get("adapter_version") or "unknown"),
        source_path=str(source),
        source_format=source_format_for_path(source),
        blocks=blocks,
        units=segment_units(blocks, fallback_title=str(payload.get("name") or source.stem)),
        warnings=tuple(warnings),
        metadata={
            "conversion_status": payload.get("status"),
            "document_name": payload.get("name"),
            "origin": payload.get("origin"),
            "staged_ascii_copy": bool(payload.get("staged_ascii_copy")),
        },
        duration_seconds=time.perf_counter() - started,
    )
