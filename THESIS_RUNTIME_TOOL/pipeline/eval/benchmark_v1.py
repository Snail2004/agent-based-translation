from __future__ import annotations

import copy
import hashlib
import json
import os
import re
from collections import Counter
from pathlib import Path
from typing import Any, Mapping, Sequence

from pipeline.eval.common_input_v1 import (
    CanonicalSourcePackageBindingV1,
    CommonArmV1,
    CommonBlockV1,
    CommonEvaluationInputV1,
    CommonSourceSnapshotV1,
    CommonTranslationV1,
    LegacyD2LSourceBindingV1,
    source_binding_to_dict,
    validate_source_binding,
)
from pipeline.eval.contracts_v1 import (
    CanonicalPolicy,
    ContractValidationError,
    canonical_sha256,
    canonicalize,
    require_commit,
    require_enum,
    require_exact_keys,
    require_int,
    require_list,
    require_mapping,
    require_nullable_string,
    require_rfc3339,
    require_sha256,
    require_string,
    require_unique,
    seal_payload,
    validate_producer,
    verify_payload_hash,
)
from pipeline.eval.d2l_input_v1 import validate_d2l_evaluation_input
from pipeline.eval.google_translate_baseline_v1 import (
    validate_google_translate_capture_v1,
    validate_google_translate_source_input_v1,
)


__all__ = [
    "BENCHMARK_CHAPTER_IDS_V1",
    "BENCHMARK_ARM_IDS_V1",
    "BENCHMARK_MANIFEST_SCHEMA_ID",
    "BENCHMARK_OVERLAY_SCHEMA_ID",
    "BENCHMARK_PREFLIGHT_SCHEMA_ID",
    "augment_common_input_with_benchmark_overlays_v1",
    "build_benchmark_manifest_v1",
    "build_benchmark_preflight_v1",
    "build_google_capture_overlay_v1",
    "build_marked_markdown_overlay_v1",
    "build_overlay_from_common_arm_v1",
    "build_review_held_overlay_v1",
    "persist_benchmark_bundle_v1",
    "project_google_source_input_v1",
    "project_d2l_source_input_v1",
    "slice_common_input_chapter_v1",
    "source_read_model_sha256_v1",
    "validate_benchmark_source_read_models_v1",
    "validate_benchmark_manifest_v1",
    "validate_benchmark_overlay_v1",
    "validate_benchmark_preflight_v1",
]


BENCHMARK_MANIFEST_SCHEMA_ID = "EvaluationBenchmarkManifestV1"
BENCHMARK_OVERLAY_SCHEMA_ID = "EvaluationBenchmarkArmOverlayV1"
BENCHMARK_PREFLIGHT_SCHEMA_ID = "EvaluationBenchmarkPreflightV1"
SCHEMA_VERSION = "1.2.0"
LEGACY_SCHEMA_VERSION = "1.1.0"
_SUPPORTED_SCHEMA_VERSIONS = {LEGACY_SCHEMA_VERSION, SCHEMA_VERSION}

BENCHMARK_CHAPTER_IDS_V1 = (
    "d2l_preliminaries",
    "d2l_linear_networks",
    "d2l_multilayer_perceptrons",
    "d2l_deep_learning_computation",
    "d2l_convolutional_neural_networks",
)
BENCHMARK_ARM_IDS_V1 = ("S0", "S1", "community", "google_nmt", "llm_lc")

_ARM_CONTRACTS = (
    ("S0", "pipeline_ablation", "exact_source_id"),
    ("S1", "thesis_system", "exact_source_id"),
    ("community", "human_community", "reviewed_alignment"),
    ("google_nmt", "conventional_nmt", "exact_source_id"),
    ("llm_lc", "long_context_diagnostic", "marked_source_order"),
)
_ALIGNMENT_STATUSES = (
    "aligned",
    "review_held",
    "missing",
    "failed",
)
_ISSUE_CODES = ("preserve_violation",)
_ADMISSIONS = (
    "translate",
    "translate_structured",
    "preserve",
    "exclude",
    "review_required",
)
_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,191}$")
_MARKER = re.compile(r"(?m)^\[\[B(?P<number>[0-9]{4,})\]\][ \t]*\r?$")

_MANIFEST_POLICY = CanonicalPolicy(
    set_like_paths=frozenset(),
    semantic_sequence_paths=frozenset({("chapters",), ("arm_contracts",)}),
)
_OVERLAY_POLICY = CanonicalPolicy(
    set_like_paths=frozenset({("rows", "*", "issue_codes")}),
    semantic_sequence_paths=frozenset({("rows",)}),
)
_PREFLIGHT_POLICY = CanonicalPolicy(
    set_like_paths=frozenset(),
    semantic_sequence_paths=frozenset(
        {
            ("chapter_checks",),
            ("arm_checks",),
            ("arm_checks", "*", "chapter_checks"),
            ("arm_checks", "*", "chapter_checks", "*", "sample_nonready_block_ids"),
            ("blockers",),
        }
    ),
)
_SOURCE_READ_POLICY = CanonicalPolicy(
    set_like_paths=frozenset(), semantic_sequence_paths=frozenset({("blocks",)})
)


def project_d2l_source_input_v1(payload: Mapping[str, Any]) -> CommonSourceSnapshotV1:
    validated = validate_d2l_evaluation_input(payload)
    identity = validated["identity"]
    return CommonSourceSnapshotV1(
        source_schema_id=validated["schema_id"],
        source_schema_version=validated["schema_version"],
        source_binding=LegacyD2LSourceBindingV1(
            project_id=identity["project_id"],
            document_id=identity["document_id"],
            source_db_sha256=identity["source_db_sha256"],
            runtime_manifest_sha256=identity["runtime_manifest_sha256"],
        ),
        blocks=tuple(_common_block(row) for row in validated["blocks"]),
    )


def project_google_source_input_v1(payload: Mapping[str, Any]) -> CommonSourceSnapshotV1:
    validated = validate_google_translate_source_input_v1(payload)
    identity = validated["identity"]
    return CommonSourceSnapshotV1(
        source_schema_id=validated["schema_id"],
        source_schema_version=validated["schema_version"],
        source_binding=LegacyD2LSourceBindingV1(
            project_id=identity["project_id"],
            document_id=identity["document_id"],
            source_db_sha256=identity["source_db_sha256"],
            runtime_manifest_sha256=identity["runtime_manifest_sha256"],
        ),
        blocks=tuple(_common_block(row) for row in validated["blocks"]),
    )


def source_read_model_sha256_v1(source: CommonSourceSnapshotV1) -> str:
    _validate_source_snapshot(source)
    return canonical_sha256(
        {
            "source_schema_id": source.source_schema_id,
            "source_schema_version": source.source_schema_version,
            "source_binding": source_binding_to_dict(source.source_binding),
            "blocks": [
                {
                    "block_id": row.block_id,
                    "chapter_id": row.chapter_id,
                    "order_index": row.order_index,
                    "block_type": row.block_type,
                    "source_text_sha256": _text_sha256(row.source_text),
                    "admission": row.admission,
                }
                for row in source.blocks
            ],
        },
        policy=_SOURCE_READ_POLICY,
    )


def build_benchmark_manifest_v1(
    sources: Sequence[CommonSourceSnapshotV1],
    source_evidence: Sequence[Mapping[str, Any]],
    *,
    benchmark_id: str,
    created_at: str,
    producer_code_commit: str,
    selected_chapter_ids: Sequence[str] | None = None,
    selected_arm_ids: Sequence[str] | None = None,
) -> dict[str, Any]:
    chapter_ids = _validate_known_selection(
        BENCHMARK_CHAPTER_IDS_V1
        if selected_chapter_ids is None
        else selected_chapter_ids,
        allowed=BENCHMARK_CHAPTER_IDS_V1,
        minimum=1,
        path="$.selected_chapter_ids",
    )
    arm_ids = _validate_known_selection(
        BENCHMARK_ARM_IDS_V1 if selected_arm_ids is None else selected_arm_ids,
        allowed=BENCHMARK_ARM_IDS_V1,
        minimum=2,
        path="$.selected_arm_ids",
    )
    source_by_chapter = _source_map(sources)
    evidence_by_chapter = _source_evidence_map(source_evidence)
    if tuple(source_by_chapter) != chapter_ids:
        raise ContractValidationError(
            "benchmark_chapters",
            "$.sources",
            "benchmark source order differs from the selected chapter scope",
        )
    if tuple(evidence_by_chapter) != chapter_ids:
        raise ContractValidationError(
            "source_evidence",
            "$.source_evidence",
            "source evidence must exactly cover selected chapters in order",
        )
    for source in source_by_chapter.values():
        _validate_source_snapshot(source)
    projects = {row.project_id for row in source_by_chapter.values()}
    documents = {row.document_id for row in source_by_chapter.values()}
    binding_kinds = {
        source_binding_to_dict(row.source_binding)["binding_kind"]
        for row in source_by_chapter.values()
    }
    if projects != {"d2l"} or documents != {"d2l"} or len(binding_kinds) != 1:
        raise ContractValidationError(
            "benchmark_source_identity",
            "$.sources",
            "locked benchmark requires one D2L document and one source-binding kind",
        )
    binding_kind = next(iter(binding_kinds))
    if binding_kind == "legacy_d2l":
        source_dbs = {
            row.source_binding.source_db_sha256
            for row in source_by_chapter.values()
            if isinstance(row.source_binding, LegacyD2LSourceBindingV1)
        }
        if len(source_dbs) != 1:
            raise ContractValidationError(
                "benchmark_source_identity",
                "$.sources",
                "legacy benchmark chapters must share one frozen source DB",
            )
        scope_source = {
            "source_db_sha256": next(iter(source_dbs)),
        }
    else:
        canonical_bindings = [
            source_binding_to_dict(row.source_binding)
            for row in source_by_chapter.values()
        ]
        if any(row != canonical_bindings[0] for row in canonical_bindings[1:]):
            raise ContractValidationError(
                "benchmark_source_identity",
                "$.sources",
                "canonical benchmark chapters must share one exact source package binding",
            )
        scope_source = {
            "source_binding": source_binding_to_dict(
                next(iter(source_by_chapter.values())).source_binding
            )
        }
    chapters = []
    for ordinal, chapter_id in enumerate(chapter_ids):
        source = source_by_chapter[chapter_id]
        evidence = evidence_by_chapter[chapter_id]
        chapters.append(
            {
                "chapter_id": chapter_id,
                "ordinal": ordinal,
                "source_schema_id": source.source_schema_id,
                "source_schema_version": source.source_schema_version,
                **_manifest_source_identity(source),
                "source_read_model_sha256": source_read_model_sha256_v1(source),
                "source_artifact_id": evidence["source_artifact_id"],
                "source_artifact_sha256": evidence["source_artifact_sha256"],
                "source_evidence_kind": evidence["source_evidence_kind"],
                "block_count": len(source.blocks),
                "first_order_index": source.blocks[0].order_index,
                "last_order_index": source.blocks[-1].order_index,
            }
        )
    draft = {
        "schema_id": BENCHMARK_MANIFEST_SCHEMA_ID,
        "schema_version": SCHEMA_VERSION,
        "benchmark_id": benchmark_id,
        "created_at": created_at,
        "producer": _producer("evaluation_benchmark_manifest_v1", producer_code_commit),
        "scope": {
            "benchmark_kind": (
                "narrow_five_chapter_d2l_v1"
                if chapter_ids == BENCHMARK_CHAPTER_IDS_V1
                and arm_ids == BENCHMARK_ARM_IDS_V1
                else "bounded_registered_selection_d2l_v1"
            ),
            "profile_scope": "technical_d2l",
            "project_id": "d2l",
            "document_id": "d2l",
            **scope_source,
            "chapter_count": len(chapters),
            "block_count": sum(row["block_count"] for row in chapters),
            "contiguous_source_order": _is_contiguous_global_order(
                source_by_chapter.values()
            ),
        },
        "chapters": chapters,
        "arm_contracts": [
            {
                "arm_id": arm_id,
                "benchmark_role": role,
                "alignment_mode": alignment,
                "required": True,
            }
            for arm_id, role, alignment in _ARM_CONTRACTS
            if arm_id in arm_ids
        ],
        "integrity": {"manifest_sha256": "0" * 64},
    }
    return validate_benchmark_manifest_v1(
        seal_payload(draft, policy=_MANIFEST_POLICY, hash_path=("integrity", "manifest_sha256"))
    )


def validate_benchmark_manifest_v1(payload: Mapping[str, Any]) -> dict[str, Any]:
    root = require_mapping(payload, path="$")
    require_exact_keys(
        root,
        required={
            "schema_id",
            "schema_version",
            "benchmark_id",
            "created_at",
            "producer",
            "scope",
            "chapters",
            "arm_contracts",
            "integrity",
        },
        path="$",
    )
    schema_version = require_enum(
        root["schema_version"],
        _SUPPORTED_SCHEMA_VERSIONS,
        path="$.schema_version",
    )
    normalized = {
        "schema_id": require_enum(root["schema_id"], {BENCHMARK_MANIFEST_SCHEMA_ID}, path="$.schema_id"),
        "schema_version": schema_version,
        "benchmark_id": _identifier(root["benchmark_id"], "$.benchmark_id"),
        "created_at": require_rfc3339(root["created_at"], path="$.created_at"),
        "producer": _validate_component(
            root["producer"],
            "evaluation_benchmark_manifest_v1",
            schema_version=schema_version,
        ),
        "scope": _validate_scope(root["scope"], schema_version=schema_version),
        "chapters": _validate_manifest_chapters(
            root["chapters"], schema_version=schema_version
        ),
        "arm_contracts": _validate_arm_contracts(root["arm_contracts"]),
        "integrity": _validate_integrity(root["integrity"], "manifest_sha256"),
    }
    if normalized["scope"]["chapter_count"] != len(normalized["chapters"]):
        raise ContractValidationError("coverage", "$.scope.chapter_count", "chapter count drift")
    if normalized["scope"]["block_count"] != sum(row["block_count"] for row in normalized["chapters"]):
        raise ContractValidationError("coverage", "$.scope.block_count", "block count drift")
    scope_binding = _serialized_manifest_source_identity(normalized["scope"])
    chapter_bindings = [
        _serialized_manifest_source_identity(row) for row in normalized["chapters"]
    ]
    if scope_binding["binding_kind"] == "legacy_d2l":
        if any(
            row["binding_kind"] != "legacy_d2l"
            or row["source_db_sha256"] != scope_binding["source_db_sha256"]
            for row in chapter_bindings
        ):
            raise ContractValidationError(
                "source_identity", "$.chapters", "legacy source DB identity drift"
            )
    elif any(row != scope_binding for row in chapter_bindings):
        raise ContractValidationError(
            "source_identity",
            "$.chapters",
            "canonical source package identity drift",
        )
    chapter_ids = tuple(row["chapter_id"] for row in normalized["chapters"])
    arm_ids = tuple(row["arm_id"] for row in normalized["arm_contracts"])
    expected_kind = (
        "narrow_five_chapter_d2l_v1"
        if chapter_ids == BENCHMARK_CHAPTER_IDS_V1
        and arm_ids == BENCHMARK_ARM_IDS_V1
        else "bounded_registered_selection_d2l_v1"
    )
    if normalized["scope"]["benchmark_kind"] != expected_kind:
        raise ContractValidationError(
            "benchmark_kind",
            "$.scope.benchmark_kind",
            "benchmark kind does not match the selected chapter/arm scope",
        )
    intervals_are_contiguous = all(
        right["first_order_index"] == left["last_order_index"] + 1
        for left, right in zip(
            normalized["chapters"], normalized["chapters"][1:]
        )
    )
    if normalized["scope"]["contiguous_source_order"] != intervals_are_contiguous:
        raise ContractValidationError(
            "source_order",
            "$.scope.contiguous_source_order",
            "contiguous-source flag differs from chapter intervals",
        )
    if not verify_payload_hash(normalized, policy=_MANIFEST_POLICY, hash_path=("integrity", "manifest_sha256")):
        raise ContractValidationError("manifest_hash", "$.integrity.manifest_sha256", "hash drift")
    canonical = canonicalize(normalized, policy=_MANIFEST_POLICY)
    assert isinstance(canonical, dict)
    return canonical


def validate_benchmark_source_read_models_v1(
    benchmark_manifest: Mapping[str, Any],
    sources: Sequence[CommonSourceSnapshotV1],
) -> tuple[CommonSourceSnapshotV1, ...]:
    """Bind explicit source read models to a sealed benchmark manifest."""

    manifest = validate_benchmark_manifest_v1(benchmark_manifest)
    source_by_chapter = _source_map(sources)
    for source in source_by_chapter.values():
        _validate_source_snapshot(source)
    _validate_sources_against_manifest(source_by_chapter, manifest)
    return tuple(source_by_chapter.values())


def build_overlay_from_common_arm_v1(
    common: CommonEvaluationInputV1,
    *,
    chapter_id: str,
    arm_id: str,
    benchmark_role: str,
    created_at: str,
    producer_code_commit: str,
) -> dict[str, Any]:
    chapter = slice_common_input_chapter_v1(common, chapter_id)
    arm = next((row for row in chapter.arms if row.arm_id == arm_id), None)
    if arm is None:
        raise ContractValidationError("arm_id", "$.arm_id", "common input lacks requested arm")
    rows = [row for row in chapter.translations if row.arm_id == arm_id]
    return _build_overlay(
        source=_source_from_common(chapter),
        arm_id=arm_id,
        benchmark_role=benchmark_role,
        origin_kind="producer_common_input",
        evidence_artifact_id=arm.artifact_id,
        evidence_sha256=arm.artifact_sha256,
        logical_run_id=arm.logical_run_id,
        attempt_run_id=arm.attempt_run_id,
        profile_id=arm.profile_id,
        profile_config_sha256=arm.profile_config_sha256,
        rows=[_translation_dict(row) for row in rows],
        created_at=created_at,
        producer_code_commit=producer_code_commit,
    )


def build_google_capture_overlay_v1(
    source: CommonSourceSnapshotV1,
    capture_payload: Mapping[str, Any],
    *,
    created_at: str,
    producer_code_commit: str,
) -> dict[str, Any]:
    capture = validate_google_translate_capture_v1(capture_payload)
    chapter_id = _single_chapter(source)
    if capture["source"]["chapter_id"] != chapter_id:
        raise ContractValidationError("chapter_id", "$.capture.source.chapter_id", "capture belongs to another chapter")
    if not isinstance(source.source_binding, LegacyD2LSourceBindingV1):
        raise ContractValidationError("source_binding", "$.source", "Google baseline import currently requires legacy D2L source evidence")
    for field in ("project_id", "document_id", "source_db_sha256"):
        if capture["source"][field] != getattr(source.source_binding, field):
            raise ContractValidationError("source_binding", f"$.capture.source.{field}", "capture source identity drift")
    capture_rows = copy.deepcopy(capture["translations"])
    _require_overlay_exact_cover(source, capture_rows, path="$.capture.translations")
    rows = [_alignment_row_from_translation(row) for row in capture_rows]
    run = capture["run_identity"]
    return _build_overlay(
        source=source,
        arm_id="google_nmt",
        benchmark_role="conventional_nmt",
        origin_kind="google_translate_capture",
        evidence_artifact_id=capture["capture_id"],
        evidence_sha256=capture["integrity"]["capture_sha256"],
        logical_run_id=run["logical_run_id"],
        attempt_run_id=run["attempt_run_id"],
        profile_id=run["profile_id"],
        profile_config_sha256=run["profile_config_sha256"],
        rows=rows,
        created_at=created_at,
        producer_code_commit=producer_code_commit,
    )


def build_marked_markdown_overlay_v1(
    source: CommonSourceSnapshotV1,
    evidence_bytes: bytes,
    *,
    created_at: str,
    producer_code_commit: str,
    model_profile_id: str,
    model_profile_sha256: str,
    logical_run_id: str,
    attempt_run_id: str,
) -> dict[str, Any]:
    try:
        text = evidence_bytes.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ContractValidationError("encoding", "$.evidence", "marked Markdown must be UTF-8") from exc
    markers = list(_MARKER.finditer(text))
    numbers = [int(row.group("number")) for row in markers]
    if not numbers or numbers[0] != 1 or numbers != list(range(1, numbers[-1] + 1)):
        raise ContractValidationError("marker_sequence", "$.evidence", "whole-file markers must form one exact B0001..Bnnnn sequence")
    segments: dict[int, str] = {}
    for index, marker in enumerate(markers):
        end = markers[index + 1].start() if index + 1 < len(markers) else len(text)
        segments[numbers[index]] = text[marker.end():end].strip("\r\n")
    rows: list[dict[str, Any]] = []
    for block in source.blocks:
        marker_id = block.order_index + 1
        target = segments.get(marker_id)
        issues: list[str] = []
        if target is None or (block.admission != "exclude" and not target.strip()):
            alignment_status, error, target = "missing", "marker_or_text_missing", None
        elif block.admission == "review_required":
            alignment_status, error = "review_held", "source_admission_review_required"
        else:
            alignment_status, error = "aligned", None
            if block.admission == "exclude":
                target = None
            elif block.admission == "preserve" and _normalized_newlines(target) != _normalized_newlines(block.source_text):
                issues.append("preserve_violation")
        rows.append(
            {
                "block_id": block.block_id,
                "alignment_status": alignment_status,
                "target_text": target,
                "error_code": error,
                "issue_codes": issues,
            }
        )
    evidence_sha256 = hashlib.sha256(evidence_bytes).hexdigest()
    return _build_overlay(
        source=source,
        arm_id="llm_lc",
        benchmark_role="long_context_diagnostic",
        origin_kind="marked_markdown_capture",
        evidence_artifact_id=f"gpt-web-marked-{evidence_sha256[:24]}",
        evidence_sha256=evidence_sha256,
        logical_run_id=logical_run_id,
        attempt_run_id=attempt_run_id,
        profile_id=model_profile_id,
        profile_config_sha256=require_sha256(model_profile_sha256, path="$.model_profile_sha256"),
        rows=rows,
        created_at=created_at,
        producer_code_commit=producer_code_commit,
    )


def build_review_held_overlay_v1(
    source: CommonSourceSnapshotV1,
    *,
    arm_id: str,
    benchmark_role: str,
    evidence_artifact_id: str,
    evidence_sha256: str,
    origin_kind: str,
    created_at: str,
    producer_code_commit: str,
) -> dict[str, Any]:
    profile_sha = canonical_sha256(
        {"origin_kind": origin_kind, "evidence_sha256": evidence_sha256},
        policy=CanonicalPolicy(set_like_paths=frozenset(), semantic_sequence_paths=frozenset()),
    )
    return _build_overlay(
        source=source,
        arm_id=arm_id,
        benchmark_role=benchmark_role,
        origin_kind=origin_kind,
        evidence_artifact_id=evidence_artifact_id,
        evidence_sha256=evidence_sha256,
        logical_run_id=f"{arm_id}-alignment-pending",
        attempt_run_id=f"{arm_id}-alignment-pending-v1",
        profile_id="evaluation.review_held.v1",
        profile_config_sha256=profile_sha,
        rows=[
            {
                "block_id": row.block_id,
                "alignment_status": "review_held",
                "target_text": None,
                "error_code": "alignment_not_accepted",
                "issue_codes": [],
            }
            for row in source.blocks
        ],
        created_at=created_at,
        producer_code_commit=producer_code_commit,
    )


def validate_benchmark_overlay_v1(payload: Mapping[str, Any]) -> dict[str, Any]:
    root = require_mapping(payload, path="$")
    require_exact_keys(
        root,
        required={
            "schema_id",
            "schema_version",
            "overlay_id",
            "created_at",
            "producer",
            "authority",
            "source",
            "arm",
            "rows",
            "coverage",
            "integrity",
        },
        path="$",
    )
    schema_version = require_enum(
        root["schema_version"],
        _SUPPORTED_SCHEMA_VERSIONS,
        path="$.schema_version",
    )
    normalized = {
        "schema_id": require_enum(root["schema_id"], {BENCHMARK_OVERLAY_SCHEMA_ID}, path="$.schema_id"),
        "schema_version": schema_version,
        "overlay_id": _identifier(root["overlay_id"], "$.overlay_id"),
        "created_at": require_rfc3339(root["created_at"], path="$.created_at"),
        "producer": _validate_component(
            root["producer"],
            "evaluation_benchmark_overlay_v1",
            schema_version=schema_version,
        ),
        "authority": _validate_authority(root["authority"]),
        "source": _validate_overlay_source(
            root["source"], schema_version=schema_version
        ),
        "arm": _validate_overlay_arm(root["arm"]),
        "rows": _validate_overlay_rows(root["rows"]),
        "coverage": _validate_status_counts(root["coverage"]),
        "integrity": _validate_integrity(root["integrity"], "overlay_sha256"),
    }
    if len(normalized["rows"]) != normalized["source"]["block_count"]:
        raise ContractValidationError("coverage", "$.source.block_count", "row count drift")
    actual = Counter(row["alignment_status"] for row in normalized["rows"])
    issue_counts = Counter(issue for row in normalized["rows"] for issue in row["issue_codes"])
    if any(normalized["coverage"][f"{status}_count"] != actual[status] for status in _ALIGNMENT_STATUSES):
        raise ContractValidationError("coverage", "$.coverage", "status counts drift")
    if any(normalized["coverage"][f"{issue}_count"] != issue_counts[issue] for issue in _ISSUE_CODES):
        raise ContractValidationError("coverage", "$.coverage", "issue counts drift")
    if not verify_payload_hash(normalized, policy=_OVERLAY_POLICY, hash_path=("integrity", "overlay_sha256")):
        raise ContractValidationError("overlay_hash", "$.integrity.overlay_sha256", "hash drift")
    canonical = canonicalize(normalized, policy=_OVERLAY_POLICY)
    assert isinstance(canonical, dict)
    return canonical


def build_benchmark_preflight_v1(
    benchmark_manifest: Mapping[str, Any],
    sources: Sequence[CommonSourceSnapshotV1],
    overlays: Sequence[Mapping[str, Any]],
    *,
    created_at: str,
    producer_code_commit: str,
) -> dict[str, Any]:
    manifest = validate_benchmark_manifest_v1(benchmark_manifest)
    source_by_chapter = _source_map(sources)
    _validate_sources_against_manifest(source_by_chapter, manifest)
    overlay_rows = [validate_benchmark_overlay_v1(row) for row in overlays]
    overlay_by_key: dict[tuple[str, str], dict[str, Any]] = {}
    expected_chapters = [row["chapter_id"] for row in manifest["chapters"]]
    expected_arms = [row["arm_id"] for row in manifest["arm_contracts"]]
    for overlay in overlay_rows:
        key = (overlay["arm"]["arm_id"], overlay["source"]["chapter_id"])
        if key in overlay_by_key:
            raise ContractValidationError("duplicate", "$.overlays", f"duplicate overlay {key}")
        if key[0] not in expected_arms or key[1] not in expected_chapters:
            raise ContractValidationError("foreign_overlay", "$.overlays", f"foreign overlay {key}")
        source = source_by_chapter[key[1]]
        _validate_overlay_binding(overlay, source)
        overlay_by_key[key] = overlay

    arm_checks: list[dict[str, Any]] = []
    blockers: list[dict[str, Any]] = []
    for arm_id in expected_arms:
        chapter_checks: list[dict[str, Any]] = []
        for chapter_id in expected_chapters:
            source = source_by_chapter[chapter_id]
            overlay = overlay_by_key.get((arm_id, chapter_id))
            if overlay is None:
                check = {
                    "chapter_id": chapter_id,
                    "status": "missing_overlay",
                    "overlay_sha256": None,
                    **{f"{status}_count": 0 for status in _ALIGNMENT_STATUSES},
                    **{f"{issue}_count": 0 for issue in _ISSUE_CODES},
                    "sample_nonready_block_ids": [row.block_id for row in source.blocks[:5]],
                }
                blockers.append(_blocker("missing_overlay", arm_id, chapter_id, len(source.blocks)))
            else:
                counts = overlay["coverage"]
                nonready = [
                    row["block_id"]
                    for row in overlay["rows"]
                    if row["alignment_status"] != "aligned"
                ]
                check_status = "ready" if not nonready else "not_ready"
                check = {
                    "chapter_id": chapter_id,
                    "status": check_status,
                    "overlay_sha256": overlay["integrity"]["overlay_sha256"],
                    **counts,
                    "sample_nonready_block_ids": nonready[:5],
                }
                if nonready:
                    blockers.append(_blocker("nonready_rows", arm_id, chapter_id, len(nonready)))
            chapter_checks.append(check)
        arm_checks.append(
            {
                "arm_id": arm_id,
                "status": "ready" if all(row["status"] == "ready" for row in chapter_checks) else "blocked",
                "chapter_checks": chapter_checks,
            }
        )
    chapter_checks = [
        {
            "chapter_id": chapter_id,
            "source_read_model_sha256": source_read_model_sha256_v1(source_by_chapter[chapter_id]),
            "block_count": len(source_by_chapter[chapter_id].blocks),
            "status": "verified",
        }
        for chapter_id in expected_chapters
    ]
    ready = not blockers
    draft = {
        "schema_id": BENCHMARK_PREFLIGHT_SCHEMA_ID,
        "schema_version": SCHEMA_VERSION,
        "preflight_id": f"preflight-{manifest['integrity']['manifest_sha256'][:24]}",
        "created_at": created_at,
        "producer": _producer("evaluation_benchmark_preflight_v1", producer_code_commit),
        "benchmark_manifest_sha256": manifest["integrity"]["manifest_sha256"],
        "status": "ready" if ready else "blocked",
        "chapter_checks": chapter_checks,
        "arm_checks": arm_checks,
        "blockers": blockers,
        "coverage": {
            "expected_chapter_count": len(expected_chapters),
            "expected_arm_count": len(expected_arms),
            "expected_arm_chapter_count": len(expected_chapters) * len(expected_arms),
            "ready_arm_chapter_count": sum(
                row["status"] == "ready"
                for arm in arm_checks
                for row in arm["chapter_checks"]
            ),
            "blocker_count": len(blockers),
        },
        "integrity": {"preflight_sha256": "0" * 64},
    }
    return validate_benchmark_preflight_v1(
        seal_payload(draft, policy=_PREFLIGHT_POLICY, hash_path=("integrity", "preflight_sha256"))
    )


def validate_benchmark_preflight_v1(payload: Mapping[str, Any]) -> dict[str, Any]:
    root = require_mapping(payload, path="$")
    require_exact_keys(
        root,
        required={
            "schema_id",
            "schema_version",
            "preflight_id",
            "created_at",
            "producer",
            "benchmark_manifest_sha256",
            "status",
            "chapter_checks",
            "arm_checks",
            "blockers",
            "coverage",
            "integrity",
        },
        path="$",
    )
    schema_version = require_enum(
        root["schema_version"],
        _SUPPORTED_SCHEMA_VERSIONS,
        path="$.schema_version",
    )
    normalized = {
        "schema_id": require_enum(root["schema_id"], {BENCHMARK_PREFLIGHT_SCHEMA_ID}, path="$.schema_id"),
        "schema_version": schema_version,
        "preflight_id": _identifier(root["preflight_id"], "$.preflight_id"),
        "created_at": require_rfc3339(root["created_at"], path="$.created_at"),
        "producer": _validate_component(
            root["producer"],
            "evaluation_benchmark_preflight_v1",
            schema_version=schema_version,
        ),
        "benchmark_manifest_sha256": require_sha256(root["benchmark_manifest_sha256"], path="$.benchmark_manifest_sha256"),
        "status": require_enum(root["status"], {"ready", "blocked"}, path="$.status"),
        "chapter_checks": _validate_preflight_chapters(root["chapter_checks"]),
        "arm_checks": _validate_preflight_arms(root["arm_checks"]),
        "blockers": _validate_blockers(root["blockers"]),
        "coverage": _validate_preflight_coverage(root["coverage"]),
        "integrity": _validate_integrity(root["integrity"], "preflight_sha256"),
    }
    ready_count = sum(
        chapter["status"] == "ready"
        for arm in normalized["arm_checks"]
        for chapter in arm["chapter_checks"]
    )
    coverage = normalized["coverage"]
    chapter_ids = [row["chapter_id"] for row in normalized["chapter_checks"]]
    arm_ids = [row["arm_id"] for row in normalized["arm_checks"]]
    if any(
        [row["chapter_id"] for row in arm["chapter_checks"]] != chapter_ids
        for arm in normalized["arm_checks"]
    ):
        raise ContractValidationError(
            "benchmark_chapters",
            "$.arm_checks",
            "every arm must use the exact selected chapter order",
        )
    expected_dimensions = {
        "expected_chapter_count": len(chapter_ids),
        "expected_arm_count": len(arm_ids),
        "expected_arm_chapter_count": len(chapter_ids) * len(arm_ids),
    }
    if any(coverage[key] != value for key, value in expected_dimensions.items()):
        raise ContractValidationError(
            "coverage", "$.coverage", "expected benchmark dimensions drift"
        )
    if coverage["ready_arm_chapter_count"] != ready_count or coverage["blocker_count"] != len(normalized["blockers"]):
        raise ContractValidationError("coverage", "$.coverage", "preflight counts drift")
    expected_status = "ready" if not normalized["blockers"] and ready_count == coverage["expected_arm_chapter_count"] else "blocked"
    if normalized["status"] != expected_status:
        raise ContractValidationError("preflight_status", "$.status", "status does not follow blockers and exact readiness")
    if not verify_payload_hash(normalized, policy=_PREFLIGHT_POLICY, hash_path=("integrity", "preflight_sha256")):
        raise ContractValidationError("preflight_hash", "$.integrity.preflight_sha256", "hash drift")
    canonical = canonicalize(normalized, policy=_PREFLIGHT_POLICY)
    assert isinstance(canonical, dict)
    return canonical


def slice_common_input_chapter_v1(common: CommonEvaluationInputV1, chapter_id: str) -> CommonEvaluationInputV1:
    chapter = require_string(chapter_id, path="$.chapter_id")
    blocks = tuple(row for row in common.blocks if row.chapter_id == chapter)
    if not blocks:
        raise ContractValidationError("chapter_id", "$.chapter_id", "chapter is absent from common input")
    block_ids = {row.block_id for row in blocks}
    translations = tuple(row for row in common.translations if row.block_id in block_ids)
    expected = len(blocks) * len(common.arms)
    if len(translations) != expected:
        raise ContractValidationError("translation_exact_cover", "$.translations", "chapter slice is not exact-cover")
    return CommonEvaluationInputV1(
        source_schema_id=common.source_schema_id,
        source_schema_version=common.source_schema_version,
        source_binding=common.source_binding,
        blocks=blocks,
        arms=common.arms,
        translations=translations,
    )


def augment_common_input_with_benchmark_overlays_v1(
    common: CommonEvaluationInputV1,
    overlays: Sequence[Mapping[str, Any]],
) -> CommonEvaluationInputV1:
    chapters = {row.chapter_id for row in common.blocks}
    if len(chapters) != 1:
        raise ContractValidationError("chapter_scope", "$.common_input", "overlay projection requires one chapter")
    source = _source_from_common(common)
    existing = {row.arm_id for row in common.arms}
    new_arms: list[CommonArmV1] = []
    new_translations: list[CommonTranslationV1] = []
    for raw in overlays:
        overlay = validate_benchmark_overlay_v1(raw)
        _validate_overlay_binding(overlay, source)
        arm_id = overlay["arm"]["arm_id"]
        if arm_id in existing or arm_id in {row.arm_id for row in new_arms}:
            raise ContractValidationError("duplicate_arm", "$.overlays", f"duplicate arm {arm_id}")
        arm = overlay["arm"]
        new_arms.append(
            CommonArmV1(
                artifact_id=overlay["overlay_id"],
                artifact_sha256=overlay["integrity"]["overlay_sha256"],
                logical_run_id=arm["logical_run_id"],
                attempt_run_id=arm["attempt_run_id"],
                arm_id=arm_id,
                profile_id=arm["profile_id"],
                profile_config_sha256=arm["profile_config_sha256"],
                source_language=arm["source_language"],
                target_language=arm["target_language"],
            )
        )
        new_translations.extend(
            _common_translation_from_overlay(arm_id, block, row)
            for block, row in zip(common.blocks, overlay["rows"], strict=True)
        )
    block_rank = {row.block_id: index for index, row in enumerate(common.blocks)}
    return CommonEvaluationInputV1(
        source_schema_id=common.source_schema_id,
        source_schema_version=common.source_schema_version,
        source_binding=common.source_binding,
        blocks=common.blocks,
        arms=tuple(sorted((*common.arms, *new_arms), key=lambda row: row.arm_id)),
        translations=tuple(
            sorted((*common.translations, *new_translations), key=lambda row: (row.arm_id, block_rank[row.block_id]))
        ),
    )


def persist_benchmark_bundle_v1(
    output_root: str | Path,
    *,
    manifest: Mapping[str, Any],
    overlays: Sequence[Mapping[str, Any]],
    preflight: Mapping[str, Any],
) -> Path:
    root = Path(output_root).resolve()
    root.mkdir(parents=True, exist_ok=True)
    manifest_row = validate_benchmark_manifest_v1(manifest)
    overlay_rows = [validate_benchmark_overlay_v1(row) for row in overlays]
    preflight_row = validate_benchmark_preflight_v1(preflight)
    if preflight_row["benchmark_manifest_sha256"] != manifest_row["integrity"]["manifest_sha256"]:
        raise ContractValidationError("manifest_binding", "$.preflight", "preflight belongs to another benchmark")
    _write_immutable_json(root / "benchmark_manifest.json", manifest_row, _MANIFEST_POLICY)
    for row in overlay_rows:
        path = root / "overlays" / row["arm"]["arm_id"] / f"{row['source']['chapter_id']}.json"
        _write_immutable_json(path, row, _OVERLAY_POLICY)
    _write_immutable_json(root / "preflight.json", preflight_row, _PREFLIGHT_POLICY)
    return root


def _build_overlay(
    *,
    source: CommonSourceSnapshotV1,
    arm_id: str,
    benchmark_role: str,
    origin_kind: str,
    evidence_artifact_id: str,
    evidence_sha256: str,
    logical_run_id: str,
    attempt_run_id: str,
    profile_id: str,
    profile_config_sha256: str,
    rows: Sequence[Mapping[str, Any]],
    created_at: str,
    producer_code_commit: str,
) -> dict[str, Any]:
    chapter_id = _single_chapter(source)
    normalized_rows = [dict(row) for row in rows]
    _require_overlay_exact_cover(source, normalized_rows, path="$.rows")
    counts = Counter(row["alignment_status"] for row in normalized_rows)
    issue_counts = Counter(issue for row in normalized_rows for issue in row["issue_codes"])
    evidence_hash = require_sha256(evidence_sha256, path="$.evidence_sha256")
    overlay_id = f"overlay-{arm_id.lower()}-{chapter_id}-{evidence_hash[:16]}"
    draft = {
        "schema_id": BENCHMARK_OVERLAY_SCHEMA_ID,
        "schema_version": SCHEMA_VERSION,
        "overlay_id": overlay_id,
        "created_at": created_at,
        "producer": _producer("evaluation_benchmark_overlay_v1", producer_code_commit),
        "authority": {
            "evaluation_only": True,
            "public_translation_artifact": False,
            "source_text_immutable": True,
            "target_text_immutable": True,
        },
        "source": {
            "project_id": source.project_id,
            "document_id": source.document_id,
            "chapter_id": chapter_id,
            "source_schema_id": source.source_schema_id,
            "source_schema_version": source.source_schema_version,
            **_manifest_source_identity(source),
            "source_read_model_sha256": source_read_model_sha256_v1(source),
            "block_count": len(source.blocks),
        },
        "arm": {
            "arm_id": arm_id,
            "benchmark_role": benchmark_role,
            "origin_kind": origin_kind,
            "evidence_artifact_id": evidence_artifact_id,
            "evidence_sha256": evidence_hash,
            "logical_run_id": logical_run_id,
            "attempt_run_id": attempt_run_id,
            "profile_id": profile_id,
            "profile_config_sha256": profile_config_sha256,
            "source_language": "en",
            "target_language": "vi",
        },
        "rows": normalized_rows,
        "coverage": {
            **{f"{status}_count": counts[status] for status in _ALIGNMENT_STATUSES},
            **{f"{issue}_count": issue_counts[issue] for issue in _ISSUE_CODES},
        },
        "integrity": {"overlay_sha256": "0" * 64},
    }
    return validate_benchmark_overlay_v1(
        seal_payload(draft, policy=_OVERLAY_POLICY, hash_path=("integrity", "overlay_sha256"))
    )


def _validate_overlay_binding(overlay: Mapping[str, Any], source: CommonSourceSnapshotV1) -> None:
    source_row = overlay["source"]
    expected = {
        "project_id": source.project_id,
        "document_id": source.document_id,
        "chapter_id": _single_chapter(source),
        "source_schema_id": source.source_schema_id,
        "source_schema_version": source.source_schema_version,
        **_manifest_source_identity(source),
        "source_read_model_sha256": source_read_model_sha256_v1(source),
        "block_count": len(source.blocks),
    }
    if source_row != expected:
        raise ContractValidationError("source_binding", "$.overlay.source", "overlay belongs to another source read model")
    _require_overlay_exact_cover(source, overlay["rows"], path="$.overlay.rows")
    for index, (block, row) in enumerate(zip(source.blocks, overlay["rows"], strict=True)):
        path = f"$.overlay.rows[{index}]"
        if row["alignment_status"] != "aligned":
            continue
        target = row["target_text"]
        if block.admission in {"translate", "translate_structured", "preserve"} and target is None:
            raise ContractValidationError("target_text", f"{path}.target_text", "aligned source row requires target text")
        if block.admission == "exclude" and target is not None:
            raise ContractValidationError("target_text", f"{path}.target_text", "excluded source row cannot carry target text")
        if block.admission == "review_required":
            raise ContractValidationError("source_admission", path, "review-required source row cannot be aligned")
        preserve_changed = block.admission == "preserve" and _normalized_newlines(target or "") != _normalized_newlines(block.source_text)
        flagged = "preserve_violation" in row["issue_codes"]
        if preserve_changed != flagged:
            raise ContractValidationError("issue_binding", f"{path}.issue_codes", "preserve violation flag differs from evidence bytes")


def _require_overlay_exact_cover(source: CommonSourceSnapshotV1, rows: Sequence[Mapping[str, Any]], *, path: str) -> None:
    expected = [row.block_id for row in source.blocks]
    actual = [row.get("block_id") for row in rows]
    if actual != expected:
        raise ContractValidationError("block_exact_cover", path, "rows must match source IDs and order exactly")


def _validate_sources_against_manifest(sources: Mapping[str, CommonSourceSnapshotV1], manifest: Mapping[str, Any]) -> None:
    expected_chapters = tuple(row["chapter_id"] for row in manifest["chapters"])
    if tuple(sources) != expected_chapters:
        raise ContractValidationError("benchmark_chapters", "$.sources", "source chapters/order drift")
    for declared in manifest["chapters"]:
        source = sources[declared["chapter_id"]]
        actual = {
            "chapter_id": declared["chapter_id"],
            "ordinal": declared["ordinal"],
            "source_schema_id": source.source_schema_id,
            "source_schema_version": source.source_schema_version,
            **_manifest_source_identity(source),
            "source_read_model_sha256": source_read_model_sha256_v1(source),
            "source_artifact_id": declared["source_artifact_id"],
            "source_artifact_sha256": declared["source_artifact_sha256"],
            "source_evidence_kind": declared["source_evidence_kind"],
            "block_count": len(source.blocks),
            "first_order_index": source.blocks[0].order_index,
            "last_order_index": source.blocks[-1].order_index,
        }
        if actual != declared:
            raise ContractValidationError("source_binding", "$.sources", f"source drift for {declared['chapter_id']}")


def _source_map(sources: Sequence[CommonSourceSnapshotV1]) -> dict[str, CommonSourceSnapshotV1]:
    result: dict[str, CommonSourceSnapshotV1] = {}
    for source in sources:
        chapter_id = _single_chapter(source)
        if chapter_id in result:
            raise ContractValidationError("duplicate", "$.sources", f"duplicate chapter {chapter_id}")
        result[chapter_id] = source
    return result


def _source_evidence_map(rows: Sequence[Mapping[str, Any]]) -> dict[str, dict[str, str]]:
    result: dict[str, dict[str, str]] = {}
    for index, raw in enumerate(rows):
        path = f"$.source_evidence[{index}]"
        row = require_mapping(raw, path=path)
        require_exact_keys(row, required={"chapter_id", "source_artifact_id", "source_artifact_sha256", "source_evidence_kind"}, path=path)
        chapter_id = require_string(row["chapter_id"], path=f"{path}.chapter_id")
        if chapter_id in result:
            raise ContractValidationError("duplicate", path, "chapter evidence duplicated")
        result[chapter_id] = {
            "chapter_id": chapter_id,
            "source_artifact_id": require_string(row["source_artifact_id"], path=f"{path}.source_artifact_id"),
            "source_artifact_sha256": require_sha256(row["source_artifact_sha256"], path=f"{path}.source_artifact_sha256"),
            "source_evidence_kind": require_enum(
                row["source_evidence_kind"],
                {
                    "d2l_evaluation_package",
                    "google_translate_source_input",
                    "canonical_source_package_v1",
                },
                path=f"{path}.source_evidence_kind",
            ),
        }
    return result


def _validate_source_snapshot(source: CommonSourceSnapshotV1) -> None:
    require_string(source.source_schema_id, path="$.source.source_schema_id")
    require_string(source.source_schema_version, path="$.source.source_schema_version")
    if isinstance(source.source_binding, LegacyD2LSourceBindingV1):
        if source.source_schema_id != "D2LEvaluationInputV1":
            raise ContractValidationError(
                "legacy_source_schema",
                "$.source.source_schema_id",
                "legacy binding is restricted to D2LEvaluationInputV1 compatibility",
            )
        require_sha256(
            source.source_binding.source_db_sha256,
            path="$.source.source_db_sha256",
        )
        require_sha256(
            source.source_binding.runtime_manifest_sha256,
            path="$.source.runtime_manifest_sha256",
        )
    elif isinstance(source.source_binding, CanonicalSourcePackageBindingV1):
        validate_source_binding(source_binding_to_dict(source.source_binding))
    else:
        raise ContractValidationError(
            "source_binding", "$.source", "unsupported source binding type"
        )
    if not source.blocks:
        raise ContractValidationError("empty_array", "$.source.blocks", "source blocks are required")
    block_ids: list[str] = []
    previous = -1
    chapter_ids = set()
    for index, row in enumerate(source.blocks):
        path = f"$.source.blocks[{index}]"
        block_ids.append(require_string(row.block_id, path=f"{path}.block_id"))
        chapter_ids.add(require_string(row.chapter_id, path=f"{path}.chapter_id"))
        order = require_int(row.order_index, path=f"{path}.order_index", minimum=0)
        if order <= previous:
            raise ContractValidationError("block_order", f"{path}.order_index", "order must increase")
        previous = order
        require_string(row.block_type, path=f"{path}.block_type")
        require_string(row.source_text, path=f"{path}.source_text", allow_empty=True)
        require_enum(row.admission, _ADMISSIONS, path=f"{path}.admission")
    require_unique(block_ids, path="$.source.blocks.block_id")
    if len(chapter_ids) != 1:
        raise ContractValidationError("chapter_scope", "$.source.blocks", "one source snapshot must contain one chapter")


def _single_chapter(source: CommonSourceSnapshotV1) -> str:
    _validate_source_snapshot(source)
    return source.blocks[0].chapter_id


def _is_contiguous_global_order(
    sources: Sequence[CommonSourceSnapshotV1] | Any,
) -> bool:
    order = [block.order_index for source in sources for block in source.blocks]
    return order == list(range(order[0], order[-1] + 1))


def _common_block(row: Mapping[str, Any]) -> CommonBlockV1:
    return CommonBlockV1(
        block_id=row["block_id"],
        chapter_id=row["chapter_id"],
        order_index=row["order_index"],
        block_type=row["block_type"],
        source_text=row["source_text"],
        admission=row["admission"],
    )


def _source_from_common(common: CommonEvaluationInputV1) -> CommonSourceSnapshotV1:
    return CommonSourceSnapshotV1(
        source_schema_id=common.source_schema_id,
        source_schema_version=common.source_schema_version,
        source_binding=common.source_binding,
        blocks=common.blocks,
    )


def _translation_dict(row: CommonTranslationV1) -> dict[str, Any]:
    return _alignment_row_from_translation(
        {
            "block_id": row.block_id,
            "status": row.status,
            "target_text": row.target_text,
            "error_code": row.error_code,
        }
    )


def _alignment_row_from_translation(row: Mapping[str, Any]) -> dict[str, Any]:
    status = row["status"]
    alignment_status = "aligned" if status in {"translated", "preserved", "excluded"} else status
    return {
        "block_id": row["block_id"],
        "alignment_status": alignment_status,
        "target_text": row["target_text"],
        "error_code": row["error_code"],
        "issue_codes": [],
    }


def _common_translation_from_overlay(
    arm_id: str,
    block: CommonBlockV1,
    row: Mapping[str, Any],
) -> CommonTranslationV1:
    alignment_status = row["alignment_status"]
    if alignment_status == "aligned":
        if block.admission in {"translate", "translate_structured"}:
            if row["target_text"] is None:
                raise ContractValidationError("target_text", "$.overlay.rows", "translated source row lacks aligned target text")
            return CommonTranslationV1(arm_id, block.block_id, "translated", row["target_text"], None)
        if block.admission == "preserve":
            return CommonTranslationV1(arm_id, block.block_id, "preserved", block.source_text, None)
        if block.admission == "exclude":
            return CommonTranslationV1(arm_id, block.block_id, "excluded", None, None)
        raise ContractValidationError("source_admission", "$.overlay.rows", "review-required source row cannot be scoring-ready")
    return CommonTranslationV1(
        arm_id,
        block.block_id,
        alignment_status,
        None,
        row["error_code"] if alignment_status == "failed" else None,
    )


def _manifest_source_identity(
    source: CommonSourceSnapshotV1,
) -> dict[str, Any]:
    if isinstance(source.source_binding, LegacyD2LSourceBindingV1):
        return {
            "source_db_sha256": source.source_binding.source_db_sha256,
            "runtime_manifest_sha256": source.source_binding.runtime_manifest_sha256,
        }
    if isinstance(source.source_binding, CanonicalSourcePackageBindingV1):
        return {"source_binding": source_binding_to_dict(source.source_binding)}
    raise ContractValidationError(
        "source_binding", "$.source", "unsupported source binding type"
    )


def _serialized_manifest_source_identity(
    value: Mapping[str, Any],
) -> dict[str, Any]:
    if "source_binding" in value:
        return copy.deepcopy(dict(value["source_binding"]))
    result = {
        "binding_kind": "legacy_d2l",
        "project_id": value["project_id"] if "project_id" in value else "d2l",
        "document_id": value["document_id"] if "document_id" in value else "d2l",
        "source_db_sha256": value["source_db_sha256"],
    }
    if "runtime_manifest_sha256" in value:
        result["runtime_manifest_sha256"] = value["runtime_manifest_sha256"]
    return result


def _validate_canonical_binding_at(value: Any, *, path: str) -> dict[str, Any]:
    try:
        binding = validate_source_binding(value)
    except ContractValidationError as exc:
        raise ContractValidationError(
            exc.code,
            path,
            f"invalid canonical source binding: {exc}",
        ) from exc
    if binding["binding_kind"] != "canonical_source_package_v1":
        raise ContractValidationError(
            "source_binding",
            path,
            "benchmark canonical identity requires canonical_source_package_v1",
        )
    return binding


def _producer(
    component: str,
    commit: str,
    *,
    schema_version: str = SCHEMA_VERSION,
) -> dict[str, str]:
    return {
        "workstream": "evaluation",
        "component": component,
        "component_version": schema_version,
        "code_commit": require_commit(commit, path="$.producer_code_commit"),
    }


def _validate_component(
    value: Any,
    component: str,
    *,
    schema_version: str,
) -> dict[str, str]:
    row = validate_producer(value, path="$.producer", workstream="evaluation")
    if row["component"] != component or row["component_version"] != schema_version:
        raise ContractValidationError("producer", "$.producer", "producer component/version drift")
    return row


def _identifier(value: Any, path: str) -> str:
    result = require_string(value, path=path)
    if _SAFE_ID.fullmatch(result) is None:
        raise ContractValidationError("identifier", path, "identifier contains unsupported characters")
    return result


def _validate_scope(value: Any, *, schema_version: str) -> dict[str, Any]:
    path = "$.scope"
    row = require_mapping(value, path=path)
    common_keys = {
        "benchmark_kind",
        "profile_scope",
        "project_id",
        "document_id",
        "chapter_count",
        "block_count",
        "contiguous_source_order",
    }
    has_legacy = "source_db_sha256" in row
    has_canonical = "source_binding" in row
    if has_legacy == has_canonical:
        raise ContractValidationError(
            "source_binding",
            path,
            "scope must carry exactly one legacy or canonical source identity",
        )
    if schema_version == LEGACY_SCHEMA_VERSION and has_canonical:
        raise ContractValidationError(
            "source_binding",
            f"{path}.source_binding",
            "benchmark 1.1.0 cannot carry canonical source identity",
        )
    require_exact_keys(
        row,
        required=common_keys | ({"source_db_sha256"} if has_legacy else {"source_binding"}),
        path=path,
    )
    contiguous = row["contiguous_source_order"]
    if not isinstance(contiguous, bool):
        raise ContractValidationError(
            "type_error",
            f"{path}.contiguous_source_order",
            "contiguous_source_order must be boolean",
        )
    result = {
        "benchmark_kind": require_enum(
            row["benchmark_kind"],
            {
                "narrow_five_chapter_d2l_v1",
                "bounded_registered_selection_d2l_v1",
            },
            path=f"{path}.benchmark_kind",
        ),
        "profile_scope": require_enum(row["profile_scope"], {"technical_d2l"}, path=f"{path}.profile_scope"),
        "project_id": require_enum(row["project_id"], {"d2l"}, path=f"{path}.project_id"),
        "document_id": require_enum(row["document_id"], {"d2l"}, path=f"{path}.document_id"),
        "chapter_count": require_int(row["chapter_count"], path=f"{path}.chapter_count", minimum=1),
        "block_count": require_int(row["block_count"], path=f"{path}.block_count", minimum=1),
        "contiguous_source_order": contiguous,
    }
    if has_legacy:
        result["source_db_sha256"] = require_sha256(
            row["source_db_sha256"], path=f"{path}.source_db_sha256"
        )
    else:
        result["source_binding"] = _validate_canonical_binding_at(
            row["source_binding"], path=f"{path}.source_binding"
        )
    return result


def _validate_manifest_chapters(
    value: Any, *, schema_version: str
) -> list[dict[str, Any]]:
    rows = require_list(value, path="$.chapters")
    result = []
    for index, raw in enumerate(rows):
        path = f"$.chapters[{index}]"
        row = require_mapping(raw, path=path)
        common_keys = {
            "chapter_id",
            "ordinal",
            "source_schema_id",
            "source_schema_version",
            "source_read_model_sha256",
            "source_artifact_id",
            "source_artifact_sha256",
            "source_evidence_kind",
            "block_count",
            "first_order_index",
            "last_order_index",
        }
        has_legacy = "source_db_sha256" in row or "runtime_manifest_sha256" in row
        has_canonical = "source_binding" in row
        if has_legacy == has_canonical:
            raise ContractValidationError(
                "source_binding",
                path,
                "chapter must carry exactly one legacy or canonical source identity",
            )
        if schema_version == LEGACY_SCHEMA_VERSION and has_canonical:
            raise ContractValidationError(
                "source_binding",
                f"{path}.source_binding",
                "benchmark 1.1.0 cannot carry canonical source identity",
            )
        identity_keys = (
            {"source_db_sha256", "runtime_manifest_sha256"}
            if has_legacy
            else {"source_binding"}
        )
        require_exact_keys(row, required=common_keys | identity_keys, path=path)
        normalized = {
                "chapter_id": require_string(row["chapter_id"], path=f"{path}.chapter_id"),
                "ordinal": require_int(row["ordinal"], path=f"{path}.ordinal", minimum=0),
                "source_schema_id": require_string(row["source_schema_id"], path=f"{path}.source_schema_id"),
                "source_schema_version": require_string(row["source_schema_version"], path=f"{path}.source_schema_version"),
                "source_read_model_sha256": require_sha256(row["source_read_model_sha256"], path=f"{path}.source_read_model_sha256"),
                "source_artifact_id": require_string(row["source_artifact_id"], path=f"{path}.source_artifact_id"),
                "source_artifact_sha256": require_sha256(row["source_artifact_sha256"], path=f"{path}.source_artifact_sha256"),
                "source_evidence_kind": require_enum(
                    row["source_evidence_kind"],
                    {
                        "d2l_evaluation_package",
                        "google_translate_source_input",
                        "canonical_source_package_v1",
                    },
                    path=f"{path}.source_evidence_kind",
                ),
                "block_count": require_int(row["block_count"], path=f"{path}.block_count", minimum=1),
                "first_order_index": require_int(row["first_order_index"], path=f"{path}.first_order_index", minimum=0),
                "last_order_index": require_int(row["last_order_index"], path=f"{path}.last_order_index", minimum=0),
        }
        if has_legacy:
            normalized["source_db_sha256"] = require_sha256(
                row["source_db_sha256"], path=f"{path}.source_db_sha256"
            )
            normalized["runtime_manifest_sha256"] = require_sha256(
                row["runtime_manifest_sha256"],
                path=f"{path}.runtime_manifest_sha256",
            )
        else:
            normalized["source_binding"] = _validate_canonical_binding_at(
                row["source_binding"], path=f"{path}.source_binding"
            )
        result.append(normalized)
    chapter_ids = _validate_known_selection(
        [row["chapter_id"] for row in result],
        allowed=BENCHMARK_CHAPTER_IDS_V1,
        minimum=1,
        path="$.chapters.chapter_id",
    )
    if [row["ordinal"] for row in result] != list(range(len(chapter_ids))):
        raise ContractValidationError(
            "benchmark_chapters", "$.chapters", "chapter ordinals drift"
        )
    return result


def _validate_arm_contracts(value: Any) -> list[dict[str, Any]]:
    rows = require_list(value, path="$.arm_contracts")
    result = []
    for index, raw in enumerate(rows):
        path = f"$.arm_contracts[{index}]"
        row = require_mapping(raw, path=path)
        require_exact_keys(row, required={"arm_id", "benchmark_role", "alignment_mode", "required"}, path=path)
        if row["required"] is not True:
            raise ContractValidationError(
                "required_arm", f"{path}.required", "every selected arm is required"
            )
        result.append(
            {
                "arm_id": require_string(row["arm_id"], path=f"{path}.arm_id"),
                "benchmark_role": require_enum(row["benchmark_role"], {item[1] for item in _ARM_CONTRACTS}, path=f"{path}.benchmark_role"),
                "alignment_mode": require_enum(row["alignment_mode"], {item[2] for item in _ARM_CONTRACTS}, path=f"{path}.alignment_mode"),
                "required": True,
            }
        )
    arm_ids = _validate_known_selection(
        [row["arm_id"] for row in result],
        allowed=BENCHMARK_ARM_IDS_V1,
        minimum=2,
        path="$.arm_contracts.arm_id",
    )
    selected = set(arm_ids)
    expected = [
        {
            "arm_id": arm_id,
            "benchmark_role": role,
            "alignment_mode": alignment,
            "required": True,
        }
        for arm_id, role, alignment in _ARM_CONTRACTS
        if arm_id in selected
    ]
    if result != expected:
        raise ContractValidationError(
            "arm_contracts", "$.arm_contracts", "selected arm contract drift"
        )
    return result


def _validate_authority(value: Any) -> dict[str, bool]:
    path = "$.authority"
    row = require_mapping(value, path=path)
    require_exact_keys(row, required={"evaluation_only", "public_translation_artifact", "source_text_immutable", "target_text_immutable"}, path=path)
    expected = {"evaluation_only": True, "public_translation_artifact": False, "source_text_immutable": True, "target_text_immutable": True}
    if dict(row) != expected:
        raise ContractValidationError("authority", path, "benchmark overlay authority drift")
    return expected


def _validate_overlay_source(
    value: Any, *, schema_version: str
) -> dict[str, Any]:
    path = "$.source"
    row = require_mapping(value, path=path)
    common_keys = {
        "project_id",
        "document_id",
        "chapter_id",
        "source_schema_id",
        "source_schema_version",
        "source_read_model_sha256",
        "block_count",
    }
    has_legacy = "source_db_sha256" in row or "runtime_manifest_sha256" in row
    has_canonical = "source_binding" in row
    if has_legacy == has_canonical:
        raise ContractValidationError(
            "source_binding",
            path,
            "overlay must carry exactly one legacy or canonical source identity",
        )
    if schema_version == LEGACY_SCHEMA_VERSION and has_canonical:
        raise ContractValidationError(
            "source_binding",
            f"{path}.source_binding",
            "overlay 1.1.0 cannot carry canonical source identity",
        )
    identity_keys = (
        {"source_db_sha256", "runtime_manifest_sha256"}
        if has_legacy
        else {"source_binding"}
    )
    require_exact_keys(row, required=common_keys | identity_keys, path=path)
    result = {
        "project_id": require_string(row["project_id"], path=f"{path}.project_id"),
        "document_id": require_string(row["document_id"], path=f"{path}.document_id"),
        "chapter_id": require_string(row["chapter_id"], path=f"{path}.chapter_id"),
        "source_schema_id": require_string(row["source_schema_id"], path=f"{path}.source_schema_id"),
        "source_schema_version": require_string(row["source_schema_version"], path=f"{path}.source_schema_version"),
        "source_read_model_sha256": require_sha256(row["source_read_model_sha256"], path=f"{path}.source_read_model_sha256"),
        "block_count": require_int(row["block_count"], path=f"{path}.block_count", minimum=1),
    }
    if has_legacy:
        result["source_db_sha256"] = require_sha256(
            row["source_db_sha256"], path=f"{path}.source_db_sha256"
        )
        result["runtime_manifest_sha256"] = require_sha256(
            row["runtime_manifest_sha256"],
            path=f"{path}.runtime_manifest_sha256",
        )
    else:
        result["source_binding"] = _validate_canonical_binding_at(
            row["source_binding"], path=f"{path}.source_binding"
        )
    return result


def _validate_overlay_arm(value: Any) -> dict[str, str]:
    path = "$.arm"
    row = require_mapping(value, path=path)
    require_exact_keys(row, required={"arm_id", "benchmark_role", "origin_kind", "evidence_artifact_id", "evidence_sha256", "logical_run_id", "attempt_run_id", "profile_id", "profile_config_sha256", "source_language", "target_language"}, path=path)
    return {
        "arm_id": require_string(row["arm_id"], path=f"{path}.arm_id"),
        "benchmark_role": require_enum(row["benchmark_role"], {item[1] for item in _ARM_CONTRACTS}, path=f"{path}.benchmark_role"),
        "origin_kind": require_enum(row["origin_kind"], {"producer_common_input", "google_translate_capture", "marked_markdown_capture", "community_repository_pending", "community_alignment"}, path=f"{path}.origin_kind"),
        "evidence_artifact_id": require_string(row["evidence_artifact_id"], path=f"{path}.evidence_artifact_id"),
        "evidence_sha256": require_sha256(row["evidence_sha256"], path=f"{path}.evidence_sha256"),
        "logical_run_id": require_string(row["logical_run_id"], path=f"{path}.logical_run_id"),
        "attempt_run_id": require_string(row["attempt_run_id"], path=f"{path}.attempt_run_id"),
        "profile_id": require_string(row["profile_id"], path=f"{path}.profile_id"),
        "profile_config_sha256": require_sha256(row["profile_config_sha256"], path=f"{path}.profile_config_sha256"),
        "source_language": require_enum(row["source_language"], {"en"}, path=f"{path}.source_language"),
        "target_language": require_enum(row["target_language"], {"vi"}, path=f"{path}.target_language"),
    }


def _validate_overlay_rows(value: Any) -> list[dict[str, Any]]:
    rows = require_list(value, path="$.rows")
    result = []
    for index, raw in enumerate(rows):
        path = f"$.rows[{index}]"
        row = require_mapping(raw, path=path)
        require_exact_keys(row, required={"block_id", "alignment_status", "target_text", "error_code", "issue_codes"}, path=path)
        status = require_enum(row["alignment_status"], _ALIGNMENT_STATUSES, path=f"{path}.alignment_status")
        target = require_nullable_string(row["target_text"], path=f"{path}.target_text")
        error = require_nullable_string(row["error_code"], path=f"{path}.error_code")
        issue_codes = [
            require_enum(issue, set(_ISSUE_CODES), path=f"{path}.issue_codes[{issue_index}]")
            for issue_index, issue in enumerate(require_list(row["issue_codes"], path=f"{path}.issue_codes"))
        ]
        require_unique(issue_codes, path=f"{path}.issue_codes")
        if status in {"missing", "failed"} and target is not None:
            raise ContractValidationError("target_text", f"{path}.target_text", "unavailable row cannot carry target text")
        if status in {"failed", "missing", "review_held"} and error is None:
            raise ContractValidationError("error_code", f"{path}.error_code", "nonready row requires a reason")
        if status == "aligned" and error is not None:
            raise ContractValidationError("error_code", f"{path}.error_code", "aligned row cannot carry a blocking error")
        if status != "aligned" and issue_codes:
            raise ContractValidationError("issue_codes", f"{path}.issue_codes", "nonready row cannot carry quality findings")
        result.append({"block_id": require_string(row["block_id"], path=f"{path}.block_id"), "alignment_status": status, "target_text": target, "error_code": error, "issue_codes": issue_codes})
    require_unique([row["block_id"] for row in result], path="$.rows.block_id")
    return result


def _validate_status_counts(value: Any) -> dict[str, int]:
    path = "$.coverage"
    row = require_mapping(value, path=path)
    keys = {f"{status}_count" for status in _ALIGNMENT_STATUSES} | {f"{issue}_count" for issue in _ISSUE_CODES}
    require_exact_keys(row, required=keys, path=path)
    return {key: require_int(row[key], path=f"{path}.{key}", minimum=0) for key in sorted(keys)}


def _validate_integrity(value: Any, field: str) -> dict[str, str]:
    path = "$.integrity"
    row = require_mapping(value, path=path)
    require_exact_keys(row, required={field}, path=path)
    return {field: require_sha256(row[field], path=f"{path}.{field}")}


def _blocker(code: str, arm_id: str, chapter_id: str, count: int) -> dict[str, Any]:
    return {"code": code, "arm_id": arm_id, "chapter_id": chapter_id, "affected_block_count": count}


def _validate_preflight_chapters(value: Any) -> list[dict[str, Any]]:
    rows = require_list(value, path="$.chapter_checks")
    result = []
    for index, raw in enumerate(rows):
        path = f"$.chapter_checks[{index}]"
        row = require_mapping(raw, path=path)
        require_exact_keys(row, required={"chapter_id", "source_read_model_sha256", "block_count", "status"}, path=path)
        result.append({"chapter_id": require_string(row["chapter_id"], path=f"{path}.chapter_id"), "source_read_model_sha256": require_sha256(row["source_read_model_sha256"], path=f"{path}.source_read_model_sha256"), "block_count": require_int(row["block_count"], path=f"{path}.block_count", minimum=1), "status": require_enum(row["status"], {"verified"}, path=f"{path}.status")})
    _validate_known_selection(
        [row["chapter_id"] for row in result],
        allowed=BENCHMARK_CHAPTER_IDS_V1,
        minimum=1,
        path="$.chapter_checks.chapter_id",
    )
    return result


def _validate_preflight_arms(value: Any) -> list[dict[str, Any]]:
    rows = require_list(value, path="$.arm_checks")
    result = []
    for i, raw in enumerate(rows):
        path = f"$.arm_checks[{i}]"
        row = require_mapping(raw, path=path)
        require_exact_keys(row, required={"arm_id", "status", "chapter_checks"}, path=path)
        chapters = []
        for j, raw_chapter in enumerate(require_list(row["chapter_checks"], path=f"{path}.chapter_checks")):
            chapter_path = f"{path}.chapter_checks[{j}]"
            chapter = require_mapping(raw_chapter, path=chapter_path)
            required = {"chapter_id", "status", "overlay_sha256", "sample_nonready_block_ids"} | {f"{status}_count" for status in _ALIGNMENT_STATUSES} | {f"{issue}_count" for issue in _ISSUE_CODES}
            require_exact_keys(chapter, required=required, path=chapter_path)
            samples = [require_string(item, path=f"{chapter_path}.sample_nonready_block_ids[{k}]") for k, item in enumerate(require_list(chapter["sample_nonready_block_ids"], path=f"{chapter_path}.sample_nonready_block_ids"))]
            if len(samples) > 5:
                raise ContractValidationError("sample_cap", f"{chapter_path}.sample_nonready_block_ids", "sample exceeds five IDs")
            chapters.append({
                "chapter_id": require_string(chapter["chapter_id"], path=f"{chapter_path}.chapter_id"),
                "status": require_enum(chapter["status"], {"ready", "not_ready", "missing_overlay"}, path=f"{chapter_path}.status"),
                "overlay_sha256": None if chapter["overlay_sha256"] is None else require_sha256(chapter["overlay_sha256"], path=f"{chapter_path}.overlay_sha256"),
                **{f"{status}_count": require_int(chapter[f"{status}_count"], path=f"{chapter_path}.{status}_count", minimum=0) for status in _ALIGNMENT_STATUSES},
                **{f"{issue}_count": require_int(chapter[f"{issue}_count"], path=f"{chapter_path}.{issue}_count", minimum=0) for issue in _ISSUE_CODES},
                "sample_nonready_block_ids": samples,
            })
        _validate_known_selection(
            [row["chapter_id"] for row in chapters],
            allowed=BENCHMARK_CHAPTER_IDS_V1,
            minimum=1,
            path=f"{path}.chapter_checks.chapter_id",
        )
        status = require_enum(row["status"], {"ready", "blocked"}, path=f"{path}.status")
        if status != ("ready" if all(ch["status"] == "ready" for ch in chapters) else "blocked"):
            raise ContractValidationError("arm_status", f"{path}.status", "arm status drift")
        result.append({"arm_id": require_string(row["arm_id"], path=f"{path}.arm_id"), "status": status, "chapter_checks": chapters})
    _validate_known_selection(
        [row["arm_id"] for row in result],
        allowed=BENCHMARK_ARM_IDS_V1,
        minimum=2,
        path="$.arm_checks.arm_id",
    )
    return result


def _validate_blockers(value: Any) -> list[dict[str, Any]]:
    rows = require_list(value, path="$.blockers")
    result = []
    for index, raw in enumerate(rows):
        path = f"$.blockers[{index}]"
        row = require_mapping(raw, path=path)
        require_exact_keys(row, required={"code", "arm_id", "chapter_id", "affected_block_count"}, path=path)
        result.append({"code": require_enum(row["code"], {"missing_overlay", "nonready_rows"}, path=f"{path}.code"), "arm_id": require_string(row["arm_id"], path=f"{path}.arm_id"), "chapter_id": require_string(row["chapter_id"], path=f"{path}.chapter_id"), "affected_block_count": require_int(row["affected_block_count"], path=f"{path}.affected_block_count", minimum=1)})
    return result


def _validate_preflight_coverage(value: Any) -> dict[str, int]:
    path = "$.coverage"
    row = require_mapping(value, path=path)
    require_exact_keys(row, required={"expected_chapter_count", "expected_arm_count", "expected_arm_chapter_count", "ready_arm_chapter_count", "blocker_count"}, path=path)
    return {
        key: require_int(row[key], path=f"{path}.{key}", minimum=0)
        for key in row
    }


def _validate_known_selection(
    values: Sequence[Any],
    *,
    allowed: Sequence[str],
    minimum: int,
    path: str,
) -> tuple[str, ...]:
    rows = tuple(
        require_string(item, path=f"{path}[{index}]")
        for index, item in enumerate(values)
    )
    if len(rows) < minimum:
        raise ContractValidationError(
            "selection_size", path, f"selection requires at least {minimum} item(s)"
        )
    if len(rows) != len(set(rows)):
        raise ContractValidationError(
            "selection_duplicate", path, "selection items must be unique"
        )
    positions = {item: index for index, item in enumerate(allowed)}
    if any(item not in positions for item in rows):
        raise ContractValidationError(
            "selection_unknown", path, "selection contains an unsupported item"
        )
    if tuple(sorted(rows, key=positions.__getitem__)) != rows:
        raise ContractValidationError(
            "selection_order", path, "selection must preserve canonical order"
        )
    return rows


def _normalized_newlines(value: str) -> str:
    return value.replace("\r\n", "\n").replace("\r", "\n")


def _text_sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _write_immutable_json(path: Path, payload: Mapping[str, Any], policy: CanonicalPolicy) -> None:
    encoded = (json.dumps(canonicalize(payload, policy=policy), ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if path.read_bytes() != encoded:
            raise ContractValidationError("immutable_artifact", str(path), "existing artifact bytes differ")
        return
    temporary = path.with_name(path.name + f".tmp-{os.getpid()}")
    temporary.write_bytes(encoded)
    os.replace(temporary, path)
