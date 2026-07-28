from __future__ import annotations

from typing import Any, Mapping

from pipeline.eval.contracts_v1 import (
    ContractValidationError,
    require_enum,
    require_exact_keys,
    require_int,
    require_mapping,
    require_nullable_string,
    require_sha256,
    require_string,
)
from pipeline.eval.terminology_evidence.context_substitution.v2.contracts.common import sha256_text


SOURCE_LOCATOR_KIND = "block_relative_unicode_codepoint_range_v1"
SOURCE_ARTIFACT_NAMES = ("candidate_index", "glossary", "document")


def build_block_source_provenance(
    *,
    document_id: str,
    chapter_id: str,
    block_id: str,
    source_text: str,
) -> dict[str, Any]:
    return {
        "document_id": document_id,
        "chapter_id": chapter_id,
        "block_id": block_id,
        "sentence_id": None,
        "source_start": 0,
        "source_end": len(source_text),
        "source_locator_kind": SOURCE_LOCATOR_KIND,
        "source_hash": sha256_text(source_text),
    }


def source_provenance_from_context(
    context: Mapping[str, Any],
) -> dict[str, Any]:
    return validate_source_provenance(
        context["source_provenance"], path="$.context.source_provenance"
    )


def validate_source_provenance(
    value: Any,
    *,
    path: str,
    source_text: str | None = None,
) -> dict[str, Any]:
    row = require_mapping(value, path=path)
    require_exact_keys(
        row,
        required={
            "document_id",
            "chapter_id",
            "block_id",
            "sentence_id",
            "source_start",
            "source_end",
            "source_locator_kind",
            "source_hash",
        },
        path=path,
    )
    source_start = require_int(
        row["source_start"], path=f"{path}.source_start", minimum=0
    )
    source_end = require_int(
        row["source_end"], path=f"{path}.source_end", minimum=0
    )
    if source_end < source_start:
        raise ContractValidationError(
            "source_range",
            path,
            "source_end must be greater than or equal to source_start",
        )
    normalized = {
        "document_id": require_string(
            row["document_id"], path=f"{path}.document_id", maximum=500
        ),
        "chapter_id": require_string(
            row["chapter_id"], path=f"{path}.chapter_id", maximum=500
        ),
        "block_id": require_string(
            row["block_id"], path=f"{path}.block_id", maximum=500
        ),
        "sentence_id": require_nullable_string(
            row["sentence_id"], path=f"{path}.sentence_id", maximum=500
        ),
        "source_start": source_start,
        "source_end": source_end,
        "source_locator_kind": require_enum(
            row["source_locator_kind"],
            {SOURCE_LOCATOR_KIND},
            path=f"{path}.source_locator_kind",
        ),
        "source_hash": require_sha256(
            row["source_hash"], path=f"{path}.source_hash"
        ),
    }
    if source_text is not None:
        if source_end - source_start != len(source_text):
            raise ContractValidationError(
                "source_range",
                path,
                "source range length must match the supplied context text",
            )
        if normalized["source_hash"] != sha256_text(source_text):
            raise ContractValidationError(
                "source_hash", f"{path}.source_hash", "source text hash mismatch"
            )
    return normalized


def validate_source_artifact_bindings(
    value: Any,
    *,
    path: str,
) -> dict[str, dict[str, str]]:
    root = require_mapping(value, path=path)
    require_exact_keys(root, required=set(SOURCE_ARTIFACT_NAMES), path=path)
    result: dict[str, dict[str, str]] = {}
    for name in SOURCE_ARTIFACT_NAMES:
        binding_path = f"{path}.{name}"
        row = require_mapping(root[name], path=binding_path)
        require_exact_keys(
            row,
            required={"ref", "physical_sha256"},
            path=binding_path,
        )
        result[name] = {
            "ref": require_string(
                row["ref"], path=f"{binding_path}.ref", maximum=4_000
            ),
            "physical_sha256": require_sha256(
                row["physical_sha256"],
                path=f"{binding_path}.physical_sha256",
            ),
        }
    return result


