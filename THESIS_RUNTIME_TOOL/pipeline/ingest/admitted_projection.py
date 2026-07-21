from __future__ import annotations

from typing import Any

from pipeline.ingest.canonical_source_package import canonical_json_sha256
from pipeline.ingest.document_contract import (
    DocumentContractError,
    validate_locked_document,
)


PROJECTION_SCHEMA_VERSION = "admitted_projection_v1"
ADMISSION_POLICY_ID = "canonical_source_admission"
ADMISSION_POLICY_VERSION = "1.0.0"
ADMISSION_CHANNELS = (
    "semantic_text",
    "structured_translate",
    "preserve_only",
    "exclude",
    "review_required",
)
PRESERVE_ONLY_SOURCE_KINDS = frozenset(
    {
        "code",
        "equation",
        "image",
        "math",
        "math_block",
        "raw_html",
    }
)

_TOP_LEVEL_FIELDS = {
    "schema_version",
    "doc_id",
    "policy",
    "inputs",
    "rows",
    "integrity",
}
_POLICY_FIELDS = {"policy_id", "policy_version", "policy_sha256"}
_INPUT_FIELDS = {"document", "structure", "asset_manifest"}
_IDENTITY_FIELDS = {"schema_version", "sha256"}
_ROW_FIELDS = {"chapter_id", "block_id", "channel"}
_INTEGRITY_FIELDS = {"row_count", "payload_sha256"}


class AdmissionProjectionError(ValueError):
    pass


def _validate_document(document: dict[str, Any]) -> None:
    try:
        validate_locked_document(document)
    except DocumentContractError as exc:
        raise AdmissionProjectionError(str(exc)) from exc


def _require_exact_fields(payload: dict[str, Any], fields: set[str], *, owner: str) -> None:
    actual = set(payload)
    if actual == fields:
        return
    raise AdmissionProjectionError(
        f"{owner} fields differ; missing={sorted(fields - actual)}, "
        f"extra={sorted(actual - fields)}"
    )


def _policy_payload() -> dict[str, Any]:
    return {
        "policy_id": ADMISSION_POLICY_ID,
        "policy_version": ADMISSION_POLICY_VERSION,
        "channels": list(ADMISSION_CHANNELS),
        "review_required_precedence": True,
        "translation_policy_channels": {
            "exclude": "exclude",
            "preserve": "preserve_only",
            "review": "review_required",
            "translate": "semantic_text",
            "translate_structured": "structured_translate",
        },
        "preserve_only_source_kinds": sorted(PRESERVE_ONLY_SOURCE_KINDS),
        "asset_bearing_translate_forbidden": True,
    }


def admission_policy_identity() -> dict[str, str]:
    return {
        "policy_id": ADMISSION_POLICY_ID,
        "policy_version": ADMISSION_POLICY_VERSION,
        "policy_sha256": canonical_json_sha256(_policy_payload()),
    }


def _flatten_document(
    document: dict[str, Any],
) -> list[tuple[str, str]]:
    rows: list[tuple[str, str]] = []
    for chapter in document.get("chapters") or []:
        chapter_id = str(chapter.get("chapter_id") or "")
        for block in chapter.get("blocks") or []:
            rows.append((chapter_id, str(block.get("block_id") or "")))
    return rows


def _channel_for_binding(binding: dict[str, Any]) -> str:
    policy = str(binding.get("translation_policy") or "")
    source_kind = str(binding.get("source_kind") or "")
    semantic_kind = str(binding.get("semantic_kind") or "")
    render_role = str(binding.get("render_role") or "")
    asset_ids = binding.get("asset_ids")
    if not isinstance(asset_ids, list):
        raise AdmissionProjectionError("asset binding asset_ids must be a list")

    if (
        binding.get("review_required") is True
        or policy == "review"
        or semantic_kind == "unknown"
        or render_role == "placeholder"
    ):
        return "review_required"
    if policy == "exclude":
        return "exclude"
    if policy == "preserve":
        return "preserve_only"
    if policy == "translate_structured":
        return "structured_translate"
    if policy != "translate":
        raise AdmissionProjectionError(f"unsupported translation policy: {policy}")
    if source_kind in PRESERVE_ONLY_SOURCE_KINDS:
        raise AdmissionProjectionError(
            f"preserve-only source kind may not use translate policy: {source_kind}"
        )
    if asset_ids:
        raise AdmissionProjectionError(
            "asset-bearing blocks may not enter semantic_text"
        )
    if semantic_kind != "text" or render_role != "text":
        raise AdmissionProjectionError(
            "only asset-free text bindings may enter semantic_text"
        )
    return "semantic_text"


def _input_identities(
    document: dict[str, Any],
    structure_manifest: dict[str, Any],
    asset_manifest: dict[str, Any],
) -> dict[str, dict[str, Any]]:
    return {
        "document": {
            "schema_version": document.get("schema_version"),
            "sha256": canonical_json_sha256(document),
        },
        "structure": {
            "schema_version": structure_manifest.get("schema_version"),
            "sha256": canonical_json_sha256(structure_manifest),
        },
        "asset_manifest": {
            "schema_version": asset_manifest.get("schema_version"),
            "sha256": canonical_json_sha256(asset_manifest),
        },
    }


def _projection_payload(projection: dict[str, Any]) -> dict[str, Any]:
    return {
        key: value
        for key, value in projection.items()
        if key != "integrity"
    }


def build_admitted_projection(
    document: dict[str, Any],
    structure_manifest: dict[str, Any],
    asset_manifest: dict[str, Any],
) -> dict[str, Any]:
    _validate_document(document)
    expected = _flatten_document(document)
    bindings = asset_manifest.get("block_bindings")
    if not isinstance(bindings, list):
        raise AdmissionProjectionError("asset_manifest.block_bindings must be a list")
    binding_ids = [str(binding.get("block_id") or "") for binding in bindings]
    if binding_ids != [block_id for _chapter_id, block_id in expected]:
        raise AdmissionProjectionError(
            "asset bindings must exact-cover document blocks in source order"
        )
    rows = [
        {
            "chapter_id": chapter_id,
            "block_id": block_id,
            "channel": _channel_for_binding(binding),
        }
        for (chapter_id, block_id), binding in zip(expected, bindings, strict=True)
    ]
    projection: dict[str, Any] = {
        "schema_version": PROJECTION_SCHEMA_VERSION,
        "doc_id": document.get("doc_id"),
        "policy": admission_policy_identity(),
        "inputs": _input_identities(document, structure_manifest, asset_manifest),
        "rows": rows,
    }
    projection["integrity"] = {
        "row_count": len(rows),
        "payload_sha256": canonical_json_sha256(projection),
    }
    validate_admitted_projection(
        projection,
        document,
        structure_manifest,
        asset_manifest,
    )
    return projection


def validate_admitted_projection(
    projection: dict[str, Any],
    document: dict[str, Any],
    structure_manifest: dict[str, Any],
    asset_manifest: dict[str, Any],
) -> None:
    _validate_document(document)
    if not isinstance(projection, dict):
        raise AdmissionProjectionError("admitted projection must be an object")
    _require_exact_fields(projection, _TOP_LEVEL_FIELDS, owner="projection")
    if projection.get("schema_version") != PROJECTION_SCHEMA_VERSION:
        raise AdmissionProjectionError(
            f"projection.schema_version must be {PROJECTION_SCHEMA_VERSION}"
        )
    if projection.get("doc_id") != document.get("doc_id"):
        raise AdmissionProjectionError("projection doc_id differs from document")

    policy = projection.get("policy")
    if not isinstance(policy, dict):
        raise AdmissionProjectionError("projection.policy must be an object")
    _require_exact_fields(policy, _POLICY_FIELDS, owner="projection.policy")
    if policy != admission_policy_identity():
        raise AdmissionProjectionError("projection policy identity is stale")

    identities = projection.get("inputs")
    if not isinstance(identities, dict):
        raise AdmissionProjectionError("projection.inputs must be an object")
    _require_exact_fields(identities, _INPUT_FIELDS, owner="projection.inputs")
    expected_identities = _input_identities(document, structure_manifest, asset_manifest)
    for name, expected_identity in expected_identities.items():
        identity = identities.get(name)
        if not isinstance(identity, dict):
            raise AdmissionProjectionError(f"projection.inputs.{name} must be an object")
        _require_exact_fields(
            identity,
            _IDENTITY_FIELDS,
            owner=f"projection.inputs.{name}",
        )
        if identity != expected_identity:
            raise AdmissionProjectionError(f"projection {name} identity is stale")

    rows = projection.get("rows")
    if not isinstance(rows, list):
        raise AdmissionProjectionError("projection.rows must be a list")
    expected_blocks = _flatten_document(document)
    if len(rows) != len(expected_blocks):
        raise AdmissionProjectionError(
            "projection rows must exact-cover document blocks in source order"
        )
    bindings = asset_manifest.get("block_bindings")
    if not isinstance(bindings, list) or len(bindings) != len(expected_blocks):
        raise AdmissionProjectionError(
            "asset bindings must exact-cover document blocks"
        )
    actual_blocks: list[tuple[str, str]] = []
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            raise AdmissionProjectionError(f"projection.rows[{index}] must be an object")
        _require_exact_fields(row, _ROW_FIELDS, owner=f"projection.rows[{index}]")
        chapter_id = row.get("chapter_id")
        block_id = row.get("block_id")
        channel = row.get("channel")
        if not isinstance(chapter_id, str) or not chapter_id:
            raise AdmissionProjectionError(f"projection.rows[{index}].chapter_id is invalid")
        if not isinstance(block_id, str) or not block_id:
            raise AdmissionProjectionError(f"projection.rows[{index}].block_id is invalid")
        if channel not in ADMISSION_CHANNELS:
            raise AdmissionProjectionError(f"projection.rows[{index}].channel is invalid")
        actual_blocks.append((chapter_id, block_id))
        binding = bindings[index]
        if not isinstance(binding, dict) or binding.get("block_id") != block_id:
            raise AdmissionProjectionError(
                "asset binding order differs from admitted projection"
            )
        if channel != _channel_for_binding(binding):
            raise AdmissionProjectionError(
                f"projection.rows[{index}] channel differs from admission policy"
            )
    if actual_blocks != expected_blocks:
        raise AdmissionProjectionError(
            "projection rows must exact-cover document blocks in source order"
        )

    integrity = projection.get("integrity")
    if not isinstance(integrity, dict):
        raise AdmissionProjectionError("projection.integrity must be an object")
    _require_exact_fields(integrity, _INTEGRITY_FIELDS, owner="projection.integrity")
    if integrity.get("row_count") != len(rows):
        raise AdmissionProjectionError("projection integrity row_count differs")
    expected_hash = canonical_json_sha256(_projection_payload(projection))
    if integrity.get("payload_sha256") != expected_hash:
        raise AdmissionProjectionError("projection payload hash differs")


__all__ = [
    "ADMISSION_CHANNELS",
    "ADMISSION_POLICY_ID",
    "ADMISSION_POLICY_VERSION",
    "PRESERVE_ONLY_SOURCE_KINDS",
    "PROJECTION_SCHEMA_VERSION",
    "AdmissionProjectionError",
    "admission_policy_identity",
    "build_admitted_projection",
    "validate_admitted_projection",
]
