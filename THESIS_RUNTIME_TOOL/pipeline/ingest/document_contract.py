from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator


DOCUMENT_SCHEMA_PATH = (
    Path(__file__).resolve().parents[2]
    / "dataset_spec"
    / "schema"
    / "document.schema.json"
)
RUNTIME_BLOCK_TYPES = frozenset({"heading", "paragraph", "dialogue", "footnote"})


class DocumentContractError(ValueError):
    pass


@lru_cache(maxsize=1)
def _locked_validator() -> Draft202012Validator:
    schema = json.loads(DOCUMENT_SCHEMA_PATH.read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema)


def _json_path(parts: list[object]) -> str:
    path = "$"
    for part in parts:
        if isinstance(part, int):
            path += f"[{part}]"
        else:
            path += f".{part}"
    return path


def validate_locked_document(document: dict[str, Any]) -> None:
    if not isinstance(document, dict):
        raise DocumentContractError("document must be an object")
    errors = sorted(
        _locked_validator().iter_errors(document),
        key=lambda error: (
            tuple(str(part) for part in error.absolute_path),
            error.message,
        ),
    )
    if not errors:
        return
    details = "; ".join(
        f"{_json_path(list(error.absolute_path))}: {error.message}"
        for error in errors
    )
    raise DocumentContractError(f"document violates locked Draft 2020-12 schema: {details}")


def runtime_block_type(source_kind: str) -> str:
    kind = str(source_kind or "paragraph")
    return kind if kind in RUNTIME_BLOCK_TYPES else "paragraph"


__all__ = [
    "DOCUMENT_SCHEMA_PATH",
    "RUNTIME_BLOCK_TYPES",
    "DocumentContractError",
    "runtime_block_type",
    "validate_locked_document",
]
