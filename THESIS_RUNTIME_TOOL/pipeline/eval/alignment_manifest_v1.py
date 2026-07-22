from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from pipeline.eval.common_input_v1 import (
    CommonEvaluationInputV1,
    source_binding_to_dict,
)
from pipeline.eval.contracts_v1 import (
    CanonicalPolicy,
    ContractValidationError,
    canonical_sha256,
    canonicalize,
    require_enum,
    require_exact_keys,
    require_int,
    require_list,
    require_mapping,
    require_nullable_number,
    require_nullable_string,
    require_rfc3339,
    require_sha256,
    require_string,
    require_unique,
    seal_payload,
    validate_method,
    validate_producer,
    verify_payload_hash,
)


__all__ = [
    "ALIGNMENT_MANIFEST_CANONICAL_POLICY",
    "ALIGNMENT_MANIFEST_SCHEMA_ID",
    "ALIGNMENT_MANIFEST_SCHEMA_VERSION",
    "AlignmentTargetSegmentV1",
    "AlignmentTargetSnapshotV1",
    "alignment_source_read_model_sha256",
    "alignment_target_segments_sha256",
    "build_alignment_target_snapshot",
    "make_alignment_target_segment",
    "seal_alignment_manifest",
    "validate_alignment_bindings",
    "validate_alignment_manifest",
    "validate_alignment_target_snapshot",
]


ALIGNMENT_MANIFEST_SCHEMA_ID = "AlignmentManifestV1"
ALIGNMENT_MANIFEST_SCHEMA_VERSION = "1.0.0"
ALIGNMENT_MANIFEST_SELF_HASH_PATH = ("integrity", "manifest_sha256")

ALIGNMENT_MANIFEST_CANONICAL_POLICY = CanonicalPolicy(
    set_like_paths=frozenset(),
    semantic_sequence_paths=frozenset(
        {
            ("mappings",),
            ("mappings", "*", "source_block_ids"),
            ("mappings", "*", "target_segment_ids"),
        }
    ),
)

_SOURCE_READ_MODEL_POLICY = CanonicalPolicy(
    set_like_paths=frozenset(),
    semantic_sequence_paths=frozenset({("blocks",)}),
)

_TARGET_SEGMENTS_POLICY = CanonicalPolicy(
    set_like_paths=frozenset(),
    semantic_sequence_paths=frozenset({("segments",)}),
)

_ELIGIBLE_ADMISSIONS = frozenset({"translate", "translate_structured"})
_ACCEPTED_STATES = frozenset({"exact_id", "auto_accepted", "reviewed"})
_DECISION_STATES = frozenset(
    {
        "exact_id",
        "auto_accepted",
        "review_required",
        "reviewed",
        "missing",
        "added",
        "ambiguous",
    }
)
_MAPPING_KINDS = frozenset({"1:1", "1:N", "N:1", "N:M", "missing", "added", "ambiguous"})


@dataclass(frozen=True, slots=True)
class AlignmentTargetSegmentV1:
    segment_id: str
    chapter_id: str
    order_index: int
    text: str
    text_sha256: str


@dataclass(frozen=True, slots=True)
class AlignmentTargetSnapshotV1:
    artifact_id: str
    artifact_sha256: str
    project_id: str
    document_id: str
    arm_id: str
    source_language: str
    target_language: str
    segments_sha256: str
    segments: tuple[AlignmentTargetSegmentV1, ...]


def _exact_text_sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def make_alignment_target_segment(
    *,
    segment_id: str,
    chapter_id: str,
    order_index: int,
    text: str,
) -> AlignmentTargetSegmentV1:
    if not isinstance(text, str) or not text:
        raise ContractValidationError(
            "empty_string", "$.target_segment.text", "target text must not be empty"
        )
    return AlignmentTargetSegmentV1(
        segment_id=segment_id,
        chapter_id=chapter_id,
        order_index=order_index,
        text=text,
        text_sha256=_exact_text_sha256(text),
    )


def build_alignment_target_snapshot(
    *,
    artifact_id: str,
    artifact_sha256: str,
    project_id: str,
    document_id: str,
    arm_id: str,
    source_language: str,
    target_language: str,
    segments: Sequence[AlignmentTargetSegmentV1],
) -> AlignmentTargetSnapshotV1:
    snapshot = AlignmentTargetSnapshotV1(
        artifact_id=artifact_id,
        artifact_sha256=artifact_sha256,
        project_id=project_id,
        document_id=document_id,
        arm_id=arm_id,
        source_language=source_language,
        target_language=target_language,
        segments_sha256="0" * 64,
        segments=tuple(segments),
    )
    snapshot = AlignmentTargetSnapshotV1(
        artifact_id=snapshot.artifact_id,
        artifact_sha256=snapshot.artifact_sha256,
        project_id=snapshot.project_id,
        document_id=snapshot.document_id,
        arm_id=snapshot.arm_id,
        source_language=snapshot.source_language,
        target_language=snapshot.target_language,
        segments_sha256=alignment_target_segments_sha256(snapshot),
        segments=snapshot.segments,
    )
    validate_alignment_target_snapshot(snapshot)
    return snapshot


def alignment_source_read_model_sha256(common: CommonEvaluationInputV1) -> str:
    payload = {
        "source_schema_id": common.source_schema_id,
        "source_schema_version": common.source_schema_version,
        "source_binding": source_binding_to_dict(common.source_binding),
        "blocks": [
            {
                "block_id": block.block_id,
                "chapter_id": block.chapter_id,
                "order_index": block.order_index,
                "block_type": block.block_type,
                "source_text_sha256": _exact_text_sha256(block.source_text),
                "admission": block.admission,
            }
            for block in common.blocks
        ],
    }
    return canonical_sha256(payload, policy=_SOURCE_READ_MODEL_POLICY)


def alignment_target_segments_sha256(snapshot: AlignmentTargetSnapshotV1) -> str:
    payload = {
        "artifact_id": snapshot.artifact_id,
        "artifact_sha256": snapshot.artifact_sha256,
        "project_id": snapshot.project_id,
        "document_id": snapshot.document_id,
        "arm_id": snapshot.arm_id,
        "source_language": snapshot.source_language,
        "target_language": snapshot.target_language,
        "segments": [
            {
                "segment_id": segment.segment_id,
                "chapter_id": segment.chapter_id,
                "order_index": segment.order_index,
                "text_sha256": segment.text_sha256,
            }
            for segment in snapshot.segments
        ],
    }
    return canonical_sha256(payload, policy=_TARGET_SEGMENTS_POLICY)


def validate_alignment_target_snapshot(snapshot: AlignmentTargetSnapshotV1) -> None:
    require_string(snapshot.artifact_id, path="$.target.artifact_id")
    require_sha256(snapshot.artifact_sha256, path="$.target.artifact_sha256")
    require_string(snapshot.project_id, path="$.target.project_id")
    require_string(snapshot.document_id, path="$.target.document_id")
    require_string(snapshot.arm_id, path="$.target.arm_id")
    require_string(snapshot.source_language, path="$.target.source_language")
    require_string(snapshot.target_language, path="$.target.target_language")
    require_sha256(snapshot.segments_sha256, path="$.target.segments_sha256")
    if not snapshot.segments:
        raise ContractValidationError(
            "empty_array", "$.target.segments", "target segments are required"
        )

    segment_ids: list[str] = []
    seen_chapters: set[str] = set()
    active_chapter: str | None = None
    last_order_by_chapter: dict[str, int] = {}
    for index, segment in enumerate(snapshot.segments):
        path = f"$.target.segments[{index}]"
        segment_id = require_string(segment.segment_id, path=f"{path}.segment_id")
        chapter_id = require_string(segment.chapter_id, path=f"{path}.chapter_id")
        order_index = require_int(
            segment.order_index, path=f"{path}.order_index", minimum=0
        )
        if not isinstance(segment.text, str) or not segment.text:
            raise ContractValidationError(
                "empty_string", f"{path}.text", "target text must not be empty"
            )
        require_sha256(segment.text_sha256, path=f"{path}.text_sha256")
        if segment.text_sha256 != _exact_text_sha256(segment.text):
            raise ContractValidationError(
                "target_text_hash",
                f"{path}.text_sha256",
                "target segment hash does not match exact UTF-8 text bytes",
            )
        segment_ids.append(segment_id)
        if chapter_id != active_chapter:
            if chapter_id in seen_chapters:
                raise ContractValidationError(
                    "target_order",
                    path,
                    "a target chapter may not reappear after another chapter",
                )
            seen_chapters.add(chapter_id)
            active_chapter = chapter_id
        previous = last_order_by_chapter.get(chapter_id)
        if previous is not None and order_index <= previous:
            raise ContractValidationError(
                "target_order",
                f"{path}.order_index",
                "target order must increase within a chapter",
            )
        last_order_by_chapter[chapter_id] = order_index
    require_unique(segment_ids, path="$.target.segments.segment_id")
    if snapshot.segments_sha256 != alignment_target_segments_sha256(snapshot):
        raise ContractValidationError(
            "target_segments_hash",
            "$.target.segments_sha256",
            "target segment-set hash does not match the immutable snapshot",
        )


def seal_alignment_manifest(payload: Mapping[str, Any]) -> dict[str, Any]:
    return seal_payload(
        payload,
        policy=ALIGNMENT_MANIFEST_CANONICAL_POLICY,
        hash_path=ALIGNMENT_MANIFEST_SELF_HASH_PATH,
    )


def validate_alignment_manifest(payload: Mapping[str, Any]) -> dict[str, Any]:
    root = require_mapping(payload, path="$")
    require_exact_keys(
        root,
        required={
            "schema_id",
            "schema_version",
            "manifest_id",
            "created_at",
            "producer",
            "source_read_model",
            "target_snapshot",
            "method",
            "mappings",
            "coverage",
            "integrity",
        },
        path="$",
    )
    normalized: dict[str, Any] = {
        "schema_id": require_enum(
            root["schema_id"], {ALIGNMENT_MANIFEST_SCHEMA_ID}, path="$.schema_id"
        ),
        "schema_version": require_enum(
            root["schema_version"],
            {ALIGNMENT_MANIFEST_SCHEMA_VERSION},
            path="$.schema_version",
        ),
        "manifest_id": require_string(root["manifest_id"], path="$.manifest_id"),
        "created_at": require_rfc3339(root["created_at"], path="$.created_at"),
        "producer": validate_producer(
            root["producer"], path="$.producer", workstream="evaluation"
        ),
        "source_read_model": _validate_source_read_model_binding(
            root["source_read_model"]
        ),
        "target_snapshot": _validate_target_snapshot_binding(root["target_snapshot"]),
        "method": validate_method(root["method"], path="$.method"),
        "mappings": _validate_mappings(root["mappings"]),
        "coverage": _validate_coverage(root["coverage"]),
        "integrity": _validate_integrity(root["integrity"]),
    }
    _validate_declared_coverage(normalized)
    if not verify_payload_hash(
        normalized,
        policy=ALIGNMENT_MANIFEST_CANONICAL_POLICY,
        hash_path=ALIGNMENT_MANIFEST_SELF_HASH_PATH,
    ):
        raise ContractValidationError(
            "manifest_hash",
            "$.integrity.manifest_sha256",
            "alignment manifest self-hash does not match canonical content",
        )
    canonical = canonicalize(normalized, policy=ALIGNMENT_MANIFEST_CANONICAL_POLICY)
    if not isinstance(canonical, dict):
        raise AssertionError("canonical alignment manifest must remain an object")
    return canonical


def validate_alignment_bindings(
    payload: Mapping[str, Any],
    common: CommonEvaluationInputV1,
    target: AlignmentTargetSnapshotV1,
) -> dict[str, Any]:
    manifest = validate_alignment_manifest(payload)
    validate_alignment_target_snapshot(target)

    eligible_blocks = [
        block for block in common.blocks if block.admission in _ELIGIBLE_ADMISSIONS
    ]
    expected_source_ids = [block.block_id for block in eligible_blocks]
    expected_target_ids = [segment.segment_id for segment in target.segments]
    source_binding = manifest["source_read_model"]
    expected_source_binding = {
        "project_id": common.project_id,
        "document_id": common.document_id,
        "source_schema_id": common.source_schema_id,
        "source_schema_version": common.source_schema_version,
        "source_read_model_sha256": alignment_source_read_model_sha256(common),
        "eligible_source_block_count": len(expected_source_ids),
    }
    if source_binding != expected_source_binding:
        raise ContractValidationError(
            "source_read_model_binding",
            "$.source_read_model",
            "manifest does not bind the exact Evaluation source read model",
        )

    target_binding = manifest["target_snapshot"]
    expected_target_binding = {
        "artifact_id": target.artifact_id,
        "artifact_sha256": target.artifact_sha256,
        "project_id": target.project_id,
        "document_id": target.document_id,
        "arm_id": target.arm_id,
        "source_language": target.source_language,
        "target_language": target.target_language,
        "segments_sha256": target.segments_sha256,
        "target_segment_count": len(target.segments),
    }
    if target_binding != expected_target_binding:
        raise ContractValidationError(
            "target_snapshot_binding",
            "$.target_snapshot",
            "manifest does not bind the exact target segment snapshot",
        )
    if target.project_id != common.project_id or target.document_id != common.document_id:
        raise ContractValidationError(
            "target_document",
            "$.target_snapshot",
            "target snapshot belongs to a different project or document",
        )
    common_pairs = {
        (arm.source_language, arm.target_language) for arm in common.arms
    }
    if common_pairs != {(target.source_language, target.target_language)}:
        raise ContractValidationError(
            "target_language_pair",
            "$.target_snapshot",
            "target snapshot language pair differs from machine arms",
        )
    if target.arm_id in {arm.arm_id for arm in common.arms}:
        raise ContractValidationError(
            "duplicate_arm",
            "$.target_snapshot.arm_id",
            "aligned target arm duplicates an existing machine arm",
        )

    source_by_id = {block.block_id: block for block in eligible_blocks}
    target_by_id = {segment.segment_id: segment for segment in target.segments}
    flattened_source_ids: list[str] = []
    flattened_target_ids: list[str] = []
    for index, mapping in enumerate(manifest["mappings"]):
        path = f"$.mappings[{index}]"
        source_ids = mapping["source_block_ids"]
        target_ids = mapping["target_segment_ids"]
        foreign_source = [item for item in source_ids if item not in source_by_id]
        foreign_target = [item for item in target_ids if item not in target_by_id]
        if foreign_source:
            raise ContractValidationError(
                "foreign_source_id",
                f"{path}.source_block_ids",
                "foreign source IDs: " + ",".join(foreign_source),
            )
        if foreign_target:
            raise ContractValidationError(
                "foreign_target_id",
                f"{path}.target_segment_ids",
                "foreign target IDs: " + ",".join(foreign_target),
            )
        chapters = {
            source_by_id[item].chapter_id for item in source_ids
        } | {target_by_id[item].chapter_id for item in target_ids}
        if chapters != {mapping["chapter_id"]}:
            raise ContractValidationError(
                "mapping_chapter",
                path,
                "mapping spans must stay inside their declared chapter",
            )
        flattened_source_ids.extend(source_ids)
        flattened_target_ids.extend(target_ids)

    if flattened_source_ids != expected_source_ids:
        raise ContractValidationError(
            "source_exact_cover",
            "$.mappings.source_block_ids",
            "eligible source blocks must appear exactly once in source order",
        )
    if flattened_target_ids != expected_target_ids:
        raise ContractValidationError(
            "target_exact_cover",
            "$.mappings.target_segment_ids",
            "target segments must appear exactly once in target order",
        )
    return manifest


def _validate_source_read_model_binding(value: Any) -> dict[str, Any]:
    path = "$.source_read_model"
    row = require_mapping(value, path=path)
    require_exact_keys(
        row,
        required={
            "project_id",
            "document_id",
            "source_schema_id",
            "source_schema_version",
            "source_read_model_sha256",
            "eligible_source_block_count",
        },
        path=path,
    )
    return {
        "project_id": require_string(row["project_id"], path=f"{path}.project_id"),
        "document_id": require_string(row["document_id"], path=f"{path}.document_id"),
        "source_schema_id": require_string(
            row["source_schema_id"], path=f"{path}.source_schema_id"
        ),
        "source_schema_version": require_string(
            row["source_schema_version"], path=f"{path}.source_schema_version"
        ),
        "source_read_model_sha256": require_sha256(
            row["source_read_model_sha256"],
            path=f"{path}.source_read_model_sha256",
        ),
        "eligible_source_block_count": require_int(
            row["eligible_source_block_count"],
            path=f"{path}.eligible_source_block_count",
            minimum=0,
        ),
    }


def _validate_target_snapshot_binding(value: Any) -> dict[str, Any]:
    path = "$.target_snapshot"
    row = require_mapping(value, path=path)
    require_exact_keys(
        row,
        required={
            "artifact_id",
            "artifact_sha256",
            "project_id",
            "document_id",
            "arm_id",
            "source_language",
            "target_language",
            "segments_sha256",
            "target_segment_count",
        },
        path=path,
    )
    return {
        "artifact_id": require_string(row["artifact_id"], path=f"{path}.artifact_id"),
        "artifact_sha256": require_sha256(
            row["artifact_sha256"], path=f"{path}.artifact_sha256"
        ),
        "project_id": require_string(row["project_id"], path=f"{path}.project_id"),
        "document_id": require_string(row["document_id"], path=f"{path}.document_id"),
        "arm_id": require_string(row["arm_id"], path=f"{path}.arm_id"),
        "source_language": require_string(
            row["source_language"], path=f"{path}.source_language"
        ),
        "target_language": require_string(
            row["target_language"], path=f"{path}.target_language"
        ),
        "segments_sha256": require_sha256(
            row["segments_sha256"], path=f"{path}.segments_sha256"
        ),
        "target_segment_count": require_int(
            row["target_segment_count"],
            path=f"{path}.target_segment_count",
            minimum=0,
        ),
    }


def _validate_mappings(value: Any) -> list[dict[str, Any]]:
    rows = require_list(value, path="$.mappings")
    if not rows:
        raise ContractValidationError(
            "empty_array", "$.mappings", "at least one mapping is required"
        )
    normalized: list[dict[str, Any]] = []
    mapping_ids: list[str] = []
    source_ids_seen: list[str] = []
    target_ids_seen: list[str] = []
    for index, value_row in enumerate(rows):
        path = f"$.mappings[{index}]"
        row = require_mapping(value_row, path=path)
        require_exact_keys(
            row,
            required={
                "mapping_id",
                "chapter_id",
                "mapping_kind",
                "decision_state",
                "confidence",
                "source_block_ids",
                "target_segment_ids",
                "decision_artifact_id",
                "decision_artifact_sha256",
            },
            path=path,
        )
        source_ids = _validate_id_sequence(
            row["source_block_ids"], path=f"{path}.source_block_ids"
        )
        target_ids = _validate_id_sequence(
            row["target_segment_ids"], path=f"{path}.target_segment_ids"
        )
        mapping_kind = require_enum(
            row["mapping_kind"], _MAPPING_KINDS, path=f"{path}.mapping_kind"
        )
        decision_state = require_enum(
            row["decision_state"], _DECISION_STATES, path=f"{path}.decision_state"
        )
        confidence = require_nullable_number(
            row["confidence"], path=f"{path}.confidence", minimum=0
        )
        if confidence is not None and confidence > 1:
            raise ContractValidationError(
                "range", f"{path}.confidence", "confidence must be <= 1"
            )
        decision_artifact_id = require_nullable_string(
            row["decision_artifact_id"], path=f"{path}.decision_artifact_id"
        )
        raw_decision_hash = require_nullable_string(
            row["decision_artifact_sha256"],
            path=f"{path}.decision_artifact_sha256",
        )
        decision_artifact_sha256 = (
            require_sha256(raw_decision_hash, path=f"{path}.decision_artifact_sha256")
            if raw_decision_hash is not None
            else None
        )
        _validate_mapping_semantics(
            mapping_kind=mapping_kind,
            decision_state=decision_state,
            confidence=confidence,
            source_ids=source_ids,
            target_ids=target_ids,
            decision_artifact_id=decision_artifact_id,
            decision_artifact_sha256=decision_artifact_sha256,
            path=path,
        )
        mapping_id = require_string(row["mapping_id"], path=f"{path}.mapping_id")
        mapping_ids.append(mapping_id)
        source_ids_seen.extend(source_ids)
        target_ids_seen.extend(target_ids)
        normalized.append(
            {
                "mapping_id": mapping_id,
                "chapter_id": require_string(
                    row["chapter_id"], path=f"{path}.chapter_id"
                ),
                "mapping_kind": mapping_kind,
                "decision_state": decision_state,
                "confidence": confidence,
                "source_block_ids": source_ids,
                "target_segment_ids": target_ids,
                "decision_artifact_id": decision_artifact_id,
                "decision_artifact_sha256": decision_artifact_sha256,
            }
        )
    require_unique(mapping_ids, path="$.mappings.mapping_id")
    require_unique(source_ids_seen, path="$.mappings.source_block_ids")
    require_unique(target_ids_seen, path="$.mappings.target_segment_ids")
    return normalized


def _validate_id_sequence(value: Any, *, path: str) -> list[str]:
    rows = require_list(value, path=path)
    values = [
        require_string(item, path=f"{path}[{index}]")
        for index, item in enumerate(rows)
    ]
    require_unique(values, path=path)
    return values


def _validate_mapping_semantics(
    *,
    mapping_kind: str,
    decision_state: str,
    confidence: int | float | None,
    source_ids: Sequence[str],
    target_ids: Sequence[str],
    decision_artifact_id: str | None,
    decision_artifact_sha256: str | None,
    path: str,
) -> None:
    expected_kind = _expected_mapping_kind(
        decision_state=decision_state,
        source_count=len(source_ids),
        target_count=len(target_ids),
        path=path,
    )
    if mapping_kind != expected_kind:
        raise ContractValidationError(
            "mapping_cardinality",
            f"{path}.mapping_kind",
            f"declared {mapping_kind} but cardinality/state require {expected_kind}",
        )
    if decision_state == "exact_id" and confidence != 1:
        raise ContractValidationError(
            "confidence", f"{path}.confidence", "exact_id requires confidence 1"
        )
    if decision_state == "auto_accepted" and confidence is None:
        raise ContractValidationError(
            "confidence", f"{path}.confidence", "auto_accepted requires confidence"
        )
    if decision_state in {"missing", "added"} and confidence is not None:
        raise ContractValidationError(
            "confidence",
            f"{path}.confidence",
            "missing and added coverage rows cannot claim semantic confidence",
        )
    decision_fields = (decision_artifact_id, decision_artifact_sha256)
    if decision_state == "reviewed":
        if any(item is None for item in decision_fields):
            raise ContractValidationError(
                "review_provenance",
                path,
                "reviewed mappings require a decision artifact ID and hash",
            )
    elif any(item is not None for item in decision_fields):
        raise ContractValidationError(
            "review_provenance",
            path,
            "only reviewed mappings may bind a review decision artifact",
        )


def _expected_mapping_kind(
    *,
    decision_state: str,
    source_count: int,
    target_count: int,
    path: str,
) -> str:
    if decision_state == "missing":
        if source_count < 1 or target_count != 0:
            raise ContractValidationError(
                "mapping_cardinality", path, "missing requires source only"
            )
        return "missing"
    if decision_state == "added":
        if source_count != 0 or target_count < 1:
            raise ContractValidationError(
                "mapping_cardinality", path, "added requires target only"
            )
        return "added"
    if source_count < 1 or target_count < 1:
        raise ContractValidationError(
            "mapping_cardinality",
            path,
            "accepted, review, and ambiguous mappings require both spans",
        )
    if decision_state == "ambiguous":
        return "ambiguous"
    if source_count == 1 and target_count == 1:
        return "1:1"
    if source_count == 1:
        return "1:N"
    if target_count == 1:
        return "N:1"
    return "N:M"


def _validate_coverage(value: Any) -> dict[str, int]:
    path = "$.coverage"
    row = require_mapping(value, path=path)
    fields = {
        "source_block_count",
        "target_segment_count",
        "accepted_mapping_count",
        "review_mapping_count",
        "ambiguous_mapping_count",
        "missing_mapping_count",
        "added_mapping_count",
        "accepted_source_block_count",
        "review_source_block_count",
        "ambiguous_source_block_count",
        "missing_source_block_count",
        "accepted_target_segment_count",
        "review_target_segment_count",
        "ambiguous_target_segment_count",
        "added_target_segment_count",
    }
    require_exact_keys(row, required=fields, path=path)
    return {
        field: require_int(row[field], path=f"{path}.{field}", minimum=0)
        for field in fields
    }


def _validate_integrity(value: Any) -> dict[str, str]:
    path = "$.integrity"
    row = require_mapping(value, path=path)
    require_exact_keys(row, required={"manifest_sha256"}, path=path)
    return {
        "manifest_sha256": require_sha256(
            row["manifest_sha256"], path=f"{path}.manifest_sha256"
        )
    }


def _validate_declared_coverage(manifest: Mapping[str, Any]) -> None:
    expected = _coverage_from_mappings(manifest["mappings"])
    if manifest["coverage"] != expected:
        raise ContractValidationError(
            "coverage_mismatch",
            "$.coverage",
            "declared alignment coverage does not match mapping rows",
        )
    source_binding = manifest["source_read_model"]
    target_binding = manifest["target_snapshot"]
    if expected["source_block_count"] != source_binding["eligible_source_block_count"]:
        raise ContractValidationError(
            "coverage_mismatch",
            "$.coverage.source_block_count",
            "source count differs from the bound eligible source count",
        )
    if expected["target_segment_count"] != target_binding["target_segment_count"]:
        raise ContractValidationError(
            "coverage_mismatch",
            "$.coverage.target_segment_count",
            "target count differs from the bound target segment count",
        )


def _coverage_from_mappings(mappings: Sequence[Mapping[str, Any]]) -> dict[str, int]:
    counts = {
        "source_block_count": 0,
        "target_segment_count": 0,
        "accepted_mapping_count": 0,
        "review_mapping_count": 0,
        "ambiguous_mapping_count": 0,
        "missing_mapping_count": 0,
        "added_mapping_count": 0,
        "accepted_source_block_count": 0,
        "review_source_block_count": 0,
        "ambiguous_source_block_count": 0,
        "missing_source_block_count": 0,
        "accepted_target_segment_count": 0,
        "review_target_segment_count": 0,
        "ambiguous_target_segment_count": 0,
        "added_target_segment_count": 0,
    }
    for mapping in mappings:
        source_count = len(mapping["source_block_ids"])
        target_count = len(mapping["target_segment_ids"])
        state = mapping["decision_state"]
        counts["source_block_count"] += source_count
        counts["target_segment_count"] += target_count
        if state in _ACCEPTED_STATES:
            counts["accepted_mapping_count"] += 1
            counts["accepted_source_block_count"] += source_count
            counts["accepted_target_segment_count"] += target_count
        elif state == "review_required":
            counts["review_mapping_count"] += 1
            counts["review_source_block_count"] += source_count
            counts["review_target_segment_count"] += target_count
        elif state == "ambiguous":
            counts["ambiguous_mapping_count"] += 1
            counts["ambiguous_source_block_count"] += source_count
            counts["ambiguous_target_segment_count"] += target_count
        elif state == "missing":
            counts["missing_mapping_count"] += 1
            counts["missing_source_block_count"] += source_count
        elif state == "added":
            counts["added_mapping_count"] += 1
            counts["added_target_segment_count"] += target_count
        else:
            raise AssertionError(f"unhandled decision state: {state}")
    return counts
