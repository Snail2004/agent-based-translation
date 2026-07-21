from __future__ import annotations

import gc
import json
import shutil
import sys
import tempfile
from contextlib import contextmanager, nullcontext
from importlib.metadata import version
from pathlib import Path
from typing import Any, Iterator


@contextmanager
def _ascii_stage(source: Path) -> Iterator[Path]:
    if all(ord(character) < 128 for character in str(source)):
        with nullcontext(source) as stable:
            yield stable
        return
    with tempfile.TemporaryDirectory(prefix="inputnorm_docling_") as directory:
        staged = Path(directory) / f"source{source.suffix.lower()}"
        shutil.copy2(source, staged)
        yield staged


def _label_value(item: Any) -> str:
    label = getattr(item, "label", "")
    return str(getattr(label, "value", label) or "").strip().lower()


def _text_for_item(item: Any, document: Any, label: str) -> str:
    text = str(getattr(item, "text", "") or "").strip()
    if text:
        return text
    if label == "table" and hasattr(item, "export_to_markdown"):
        return str(item.export_to_markdown(document) or "").strip()
    if hasattr(item, "caption_text"):
        return str(item.caption_text(document) or "").strip()
    return ""


def parse_source(source: Path) -> dict[str, Any]:
    from docling.document_converter import DocumentConverter

    with _ascii_stage(source) as staged:
        converter = DocumentConverter()
        result = converter.convert(staged)
        document = result.document
        blocks: list[dict[str, Any]] = []
        skipped_labels: dict[str, int] = {}
        for item, level in document.iterate_items():
            label = _label_value(item)
            text = _text_for_item(item, document, label)
            if not text:
                skipped_labels[label or type(item).__name__] = skipped_labels.get(label or type(item).__name__, 0) + 1
                continue
            kind_map = {
                "title": "heading",
                "section_header": "heading",
                "text": "paragraph",
                "paragraph": "paragraph",
                "list_item": "list_item",
                "code": "code",
                "formula": "formula",
                "table": "table",
                "picture": "image",
                "footnote": "footnote",
            }
            kind = kind_map.get(label, label or "paragraph")
            blocks.append(
                {
                    "kind": kind,
                    "text": text,
                    "heading_level": int(level) if kind == "heading" else None,
                    "source_ref": str(getattr(item, "self_ref", "") or "") or None,
                    "native_provenance": bool(getattr(item, "prov", None)),
                    "docling_label": label,
                }
            )
        origin = getattr(document, "origin", None)
        if hasattr(origin, "model_dump"):
            origin = origin.model_dump(mode="json", exclude_none=True)
        elif origin is not None:
            origin = str(origin)
        payload = {
            "adapter_version": version("docling"),
            "status": str(getattr(result.status, "value", result.status)),
            "name": str(getattr(document, "name", "") or ""),
            "origin": origin,
            "staged_ascii_copy": any(ord(character) >= 128 for character in str(source)),
            "blocks": blocks,
            "skipped_labels": skipped_labels,
        }
        del document, result, converter
        gc.collect()
    return payload


def main() -> int:
    if len(sys.argv) != 2:
        raise SystemExit("usage: normalization_docling_worker.py SOURCE")
    payload = parse_source(Path(sys.argv[1]).resolve())
    json.dump(payload, sys.stdout, ensure_ascii=False, sort_keys=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
