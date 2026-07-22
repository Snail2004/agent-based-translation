from __future__ import annotations

import hashlib
from typing import Any, Mapping

from pipeline.eval.alignment_manifest_v1 import validate_alignment_bindings
from pipeline.eval.common_input_v1 import CommonEvaluationInputV1
from pipeline.eval.contracts_v1 import (
    CanonicalPolicy,
    ContractValidationError,
    assert_no_forbidden_runtime_data,
    canonicalize,
    require_commit,
    require_enum,
    require_exact_keys,
    require_int,
    require_list,
    require_mapping,
    require_rfc3339,
    require_sha256,
    require_string,
    require_unique,
    seal_payload,
    validate_producer,
    verify_payload_hash,
)
from pipeline.eval.d2l_community_alignment_v1 import (
    D2LCommunityAlignmentError,
    D2LCommunityTargetReadModelV1,
    D2LStructuralAuditPlanV1,
    build_d2l_structural_audit_plan,
)


SCHEMA_ID = "D2LStructuralAlignmentAuditPacketV1"
SCHEMA_VERSION = "1.0.0"
SELF_HASH_PATH = ("integrity", "packet_sha256")
_COMPONENT = "d2l_structural_alignment_audit_packet"
_COMPONENT_VERSION = "1.0.0"
_ELIGIBLE_ADMISSIONS = frozenset({"translate", "translate_structured"})
_SELECTION_REASONS = frozenset(
    {"section_first", "section_last", "deterministic_hash"}
)


AUDIT_PACKET_CANONICAL_POLICY = CanonicalPolicy(
    set_like_paths=frozenset({("items", "*", "selection_reasons")}),
    semantic_sequence_paths=frozenset({("items",)}),
)


__all__ = [
    "AUDIT_PACKET_CANONICAL_POLICY",
    "SCHEMA_ID",
    "SCHEMA_VERSION",
    "build_d2l_structural_audit_packet",
    "validate_d2l_structural_audit_packet",
    "validate_d2l_structural_audit_packet_bindings",
]


def build_d2l_structural_audit_packet(
    review_manifest: Mapping[str, Any],
    audit_plan: D2LStructuralAuditPlanV1,
    common: CommonEvaluationInputV1,
    target: D2LCommunityTargetReadModelV1,
    *,
    packet_id: str,
    created_at: str,
    producer_code_commit: str,
) -> dict[str, Any]:
    manifest = validate_alignment_bindings(
        review_manifest, common, target.snapshot
    )
    expected_plan = build_d2l_structural_audit_plan(
        manifest, common, target
    )
    if audit_plan != expected_plan:
        raise D2LCommunityAlignmentError(
            "audit_plan_binding",
            "audit plan differs from the deterministic plan for these inputs",
        )

    source_by_id = {
        block.block_id: block
        for block in common.blocks
        if block.admission in _ELIGIBLE_ADMISSIONS
    }
    target_by_id = {
        segment.segment_id: segment for segment in target.snapshot.segments
    }
    target_structure_by_id = {
        row.segment_id: row for row in target.structural_rows
    }
    mapping_by_id = {
        row["mapping_id"]: row for row in manifest["mappings"]
    }

    items: list[dict[str, Any]] = []
    for selection in audit_plan.selections:
        mapping = mapping_by_id.get(selection.mapping_id)
        if mapping is None:
            raise D2LCommunityAlignmentError(
                "audit_mapping_reference",
                f"unknown selected mapping: {selection.mapping_id}",
            )
        if (
            mapping["source_block_ids"] != [selection.source_block_id]
            or mapping["target_segment_ids"] != [selection.target_segment_id]
        ):
            raise D2LCommunityAlignmentError(
                "audit_selection_binding",
                f"selection differs from mapping {selection.mapping_id}",
            )
        source = source_by_id[selection.source_block_id]
        target_segment = target_by_id[selection.target_segment_id]
        target_structure = target_structure_by_id[selection.target_segment_id]
        items.append(
            {
                "mapping_id": selection.mapping_id,
                "section_slug": selection.section_slug,
                "selection_reasons": list(selection.selection_reasons),
                "source": {
                    "block_id": source.block_id,
                    "chapter_id": source.chapter_id,
                    "order_index": source.order_index,
                    "block_type": source.block_type,
                    "text": source.source_text,
                    "text_sha256": _text_sha256(source.source_text),
                },
                "target": {
                    "segment_id": target_segment.segment_id,
                    "chapter_id": target_segment.chapter_id,
                    "order_index": target_segment.order_index,
                    "block_type": target_structure.block_type,
                    "text": target_segment.text,
                    "text_sha256": target_segment.text_sha256,
                },
            }
        )

    payload = {
        "schema_id": SCHEMA_ID,
        "schema_version": SCHEMA_VERSION,
        "packet_id": packet_id,
        "created_at": created_at,
        "producer": {
            "workstream": "evaluation",
            "component": _COMPONENT,
            "component_version": _COMPONENT_VERSION,
            "code_commit": producer_code_commit,
        },
        "policy": {
            "policy_id": audit_plan.policy_id,
            "policy_version": audit_plan.policy_version,
        },
        "bindings": {
            "alignment_manifest_sha256": manifest["integrity"][
                "manifest_sha256"
            ],
            "source_read_model_sha256": audit_plan.source_read_model_sha256,
            "target_artifact_sha256": target.snapshot.artifact_sha256,
            "target_segments_sha256": audit_plan.target_segments_sha256,
            "origin_files_sha256": audit_plan.origin_files_sha256,
            "selection_sha256": audit_plan.selection_sha256,
        },
        "sampling": {
            "population_count": audit_plan.population_count,
            "sample_count": audit_plan.sample_count,
        },
        "items": items,
        "integrity": {"packet_sha256": "0" * 64},
    }
    sealed = seal_payload(
        payload,
        policy=AUDIT_PACKET_CANONICAL_POLICY,
        hash_path=SELF_HASH_PATH,
    )
    return validate_d2l_structural_audit_packet(sealed)


def validate_d2l_structural_audit_packet(
    payload: Mapping[str, Any],
) -> dict[str, Any]:
    assert_no_forbidden_runtime_data(payload)
    root = require_mapping(payload, path="$")
    require_exact_keys(
        root,
        required={
            "schema_id",
            "schema_version",
            "packet_id",
            "created_at",
            "producer",
            "policy",
            "bindings",
            "sampling",
            "items",
            "integrity",
        },
        path="$",
    )
    producer = validate_producer(
        root["producer"], path="$.producer", workstream="evaluation"
    )
    if producer["component"] != _COMPONENT:
        raise ContractValidationError(
            "component",
            "$.producer.component",
            f"expected {_COMPONENT}",
        )
    if producer["component_version"] != _COMPONENT_VERSION:
        raise ContractValidationError(
            "component_version",
            "$.producer.component_version",
            f"expected {_COMPONENT_VERSION}",
        )

    normalized: dict[str, Any] = {
        "schema_id": require_enum(root["schema_id"], {SCHEMA_ID}, path="$.schema_id"),
        "schema_version": require_enum(
            root["schema_version"], {SCHEMA_VERSION}, path="$.schema_version"
        ),
        "packet_id": require_string(root["packet_id"], path="$.packet_id"),
        "created_at": require_rfc3339(root["created_at"], path="$.created_at"),
        "producer": producer,
        "policy": _validate_policy(root["policy"]),
        "bindings": _validate_bindings(root["bindings"]),
        "sampling": _validate_sampling(root["sampling"]),
        "items": _validate_items(root["items"]),
        "integrity": _validate_integrity(root["integrity"]),
    }
    if normalized["sampling"]["sample_count"] != len(normalized["items"]):
        raise ContractValidationError(
            "sample_count",
            "$.sampling.sample_count",
            "sample count must equal the number of packet items",
        )
    if not verify_payload_hash(
        normalized,
        policy=AUDIT_PACKET_CANONICAL_POLICY,
        hash_path=SELF_HASH_PATH,
    ):
        raise ContractValidationError(
            "packet_hash",
            "$.integrity.packet_sha256",
            "packet self-hash does not match canonical content",
        )
    canonical = canonicalize(
        normalized, policy=AUDIT_PACKET_CANONICAL_POLICY
    )
    if not isinstance(canonical, dict):
        raise AssertionError("canonical audit packet must remain an object")
    return canonical


def validate_d2l_structural_audit_packet_bindings(
    payload: Mapping[str, Any],
    review_manifest: Mapping[str, Any],
    audit_plan: D2LStructuralAuditPlanV1,
    common: CommonEvaluationInputV1,
    target: D2LCommunityTargetReadModelV1,
) -> dict[str, Any]:
    packet = validate_d2l_structural_audit_packet(payload)
    expected = build_d2l_structural_audit_packet(
        review_manifest,
        audit_plan,
        common,
        target,
        packet_id=packet["packet_id"],
        created_at=packet["created_at"],
        producer_code_commit=packet["producer"]["code_commit"],
    )
    if packet != expected:
        raise D2LCommunityAlignmentError(
            "audit_packet_binding",
            "packet does not reproduce from the exact manifest, plan, and text inputs",
        )
    return packet


def _validate_policy(value: Any) -> dict[str, str]:
    path = "$.policy"
    row = require_mapping(value, path=path)
    require_exact_keys(
        row, required={"policy_id", "policy_version"}, path=path
    )
    return {
        "policy_id": require_string(row["policy_id"], path=f"{path}.policy_id"),
        "policy_version": require_string(
            row["policy_version"], path=f"{path}.policy_version"
        ),
    }


def _validate_bindings(value: Any) -> dict[str, str]:
    path = "$.bindings"
    row = require_mapping(value, path=path)
    fields = {
        "alignment_manifest_sha256",
        "source_read_model_sha256",
        "target_artifact_sha256",
        "target_segments_sha256",
        "origin_files_sha256",
        "selection_sha256",
    }
    require_exact_keys(row, required=fields, path=path)
    return {
        field: require_sha256(row[field], path=f"{path}.{field}")
        for field in sorted(fields)
    }


def _validate_sampling(value: Any) -> dict[str, int]:
    path = "$.sampling"
    row = require_mapping(value, path=path)
    require_exact_keys(
        row, required={"population_count", "sample_count"}, path=path
    )
    population_count = require_int(
        row["population_count"], path=f"{path}.population_count", minimum=1
    )
    sample_count = require_int(
        row["sample_count"], path=f"{path}.sample_count", minimum=1
    )
    if sample_count > population_count:
        raise ContractValidationError(
            "sample_count",
            f"{path}.sample_count",
            "sample count cannot exceed population count",
        )
    return {
        "population_count": population_count,
        "sample_count": sample_count,
    }


def _validate_items(value: Any) -> list[dict[str, Any]]:
    path = "$.items"
    rows = require_list(value, path=path)
    if not rows:
        raise ContractValidationError("empty_array", path, "audit items are required")
    result: list[dict[str, Any]] = []
    for index, value_row in enumerate(rows):
        row_path = f"{path}[{index}]"
        row = require_mapping(value_row, path=row_path)
        require_exact_keys(
            row,
            required={
                "mapping_id",
                "section_slug",
                "selection_reasons",
                "source",
                "target",
            },
            path=row_path,
        )
        reasons = [
            require_enum(
                item,
                _SELECTION_REASONS,
                path=f"{row_path}.selection_reasons[{reason_index}]",
            )
            for reason_index, item in enumerate(
                require_list(
                    row["selection_reasons"],
                    path=f"{row_path}.selection_reasons",
                )
            )
        ]
        if not reasons:
            raise ContractValidationError(
                "empty_array",
                f"{row_path}.selection_reasons",
                "at least one selection reason is required",
            )
        require_unique(reasons, path=f"{row_path}.selection_reasons")
        result.append(
            {
                "mapping_id": require_string(
                    row["mapping_id"], path=f"{row_path}.mapping_id"
                ),
                "section_slug": require_string(
                    row["section_slug"], path=f"{row_path}.section_slug"
                ),
                "selection_reasons": reasons,
                "source": _validate_text_row(
                    row["source"], path=f"{row_path}.source", id_field="block_id"
                ),
                "target": _validate_text_row(
                    row["target"], path=f"{row_path}.target", id_field="segment_id"
                ),
            }
        )
    require_unique([row["mapping_id"] for row in result], path=f"{path}.mapping_id")
    require_unique(
        [row["source"]["block_id"] for row in result], path=f"{path}.source.block_id"
    )
    require_unique(
        [row["target"]["segment_id"] for row in result],
        path=f"{path}.target.segment_id",
    )
    return result


def _validate_text_row(
    value: Any, *, path: str, id_field: str
) -> dict[str, Any]:
    row = require_mapping(value, path=path)
    require_exact_keys(
        row,
        required={
            id_field,
            "chapter_id",
            "order_index",
            "block_type",
            "text",
            "text_sha256",
        },
        path=path,
    )
    text = require_string(row["text"], path=f"{path}.text")
    text_sha256 = require_sha256(
        row["text_sha256"], path=f"{path}.text_sha256"
    )
    if _text_sha256(text) != text_sha256:
        raise ContractValidationError(
            "text_hash",
            f"{path}.text_sha256",
            "text hash does not match exact UTF-8 text",
        )
    return {
        id_field: require_string(row[id_field], path=f"{path}.{id_field}"),
        "chapter_id": require_string(row["chapter_id"], path=f"{path}.chapter_id"),
        "order_index": require_int(
            row["order_index"], path=f"{path}.order_index", minimum=0
        ),
        "block_type": require_string(row["block_type"], path=f"{path}.block_type"),
        "text": text,
        "text_sha256": text_sha256,
    }


def _validate_integrity(value: Any) -> dict[str, str]:
    path = "$.integrity"
    row = require_mapping(value, path=path)
    require_exact_keys(row, required={"packet_sha256"}, path=path)
    return {
        "packet_sha256": require_sha256(
            row["packet_sha256"], path=f"{path}.packet_sha256"
        )
    }


def _text_sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()
