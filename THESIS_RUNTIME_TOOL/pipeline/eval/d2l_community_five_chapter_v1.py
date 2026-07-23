from __future__ import annotations

import copy
import hashlib
import json
import math
import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

from pipeline.eval.alignment_manifest_v1 import (
    ALIGNMENT_MANIFEST_SCHEMA_ID,
    ALIGNMENT_MANIFEST_SCHEMA_VERSION,
    alignment_source_read_model_sha256,
    seal_alignment_manifest,
    validate_alignment_bindings,
)
from pipeline.eval.common_input_v1 import (
    AdmissionPolicyIdentityV1,
    CanonicalComponentIdentityV1,
    CanonicalProjectionIdentityV1,
    CanonicalSourcePackageBindingV1,
    CommonArmV1,
    CommonBlockV1,
    CommonEvaluationInputV1,
)
from pipeline.eval.contracts_v1 import (
    CanonicalPolicy,
    ContractValidationError,
    canonical_sha256,
    canonicalize,
    require_commit,
    require_enum,
    require_exact_keys,
    require_list,
    require_mapping,
    require_rfc3339,
    require_sha256,
    require_string,
    require_unique,
    seal_payload,
    verify_payload_hash,
)
from pipeline.eval.d2l_community_alignment_v1 import (
    D2LCommunityAlignmentError,
    D2LCommunityTargetReadModelV1,
    build_d2l_community_target_read_model,
)
from pipeline.ingest.canonical_source_package import canonical_json_sha256


__all__ = [
    "AUDIT_PLAN_SCHEMA_ID",
    "AUDIT_RECORD_SCHEMA_ID",
    "BUNDLE_SCHEMA_ID",
    "MANUAL_DECISION_SCHEMA_ID",
    "D2LCommunityFiveChapterError",
    "D2LCommunityFiveChapterInputsV1",
    "ManualAlignmentOverrideV1",
    "apply_d2l_community_alignment_audit",
    "build_d2l_community_alignment_audit_plan",
    "build_d2l_community_chapter_review_manifest",
    "build_d2l_community_manual_decision",
    "load_d2l_community_five_chapter_inputs",
    "record_d2l_community_alignment_audit",
    "validate_d2l_community_alignment_audit_plan",
    "validate_d2l_community_alignment_audit_record",
    "validate_d2l_community_manual_decision",
    "write_d2l_community_alignment_bundle",
]


MANUAL_DECISION_SCHEMA_ID = "D2LCommunityManualAlignmentDecisionV1"
AUDIT_PLAN_SCHEMA_ID = "D2LCommunityAlignmentAuditPlanV1"
AUDIT_RECORD_SCHEMA_ID = "D2LCommunityAlignmentAuditRecordV1"
BUNDLE_SCHEMA_ID = "D2LCommunityFiveChapterAlignmentBundleV1"
SCHEMA_VERSION = "1.0.0"

_ELIGIBLE_ADMISSIONS = frozenset({"translate", "translate_structured"})
_CHANNEL_TO_ADMISSION = {
    "semantic_text": "translate",
    "structured_translate": "translate_structured",
    "preserve_only": "preserve",
    "exclude": "exclude",
    "review_required": "review_required",
}
_SOURCE_BLOCK_SUFFIX = re.compile(r"^(?P<section>.+)_b(?P<number>[0-9]{3,})$")
_MANUAL_REASON_CODES = frozenset(
    {
        "source_markdown_combines_heading_and_prose",
        "target_markdown_combines_wrapper_span",
        "manual_structural_boundary",
    }
)
_AUDIT_POLICY_ID = "d2l_community_five_chapter_alignment_audit_v1"
_AUDIT_POLICY_VERSION = "1.0.0"
_AUDIT_MINIMUM = 30
_AUDIT_FRACTION = 0.10

_MANUAL_POLICY = CanonicalPolicy(
    set_like_paths=frozenset(),
    semantic_sequence_paths=frozenset(
        {
            ("chapters",),
            ("overrides",),
            ("overrides", "*", "source_block_ids"),
            ("overrides", "*", "target_segment_ids"),
        }
    ),
)
_AUDIT_PLAN_POLICY = CanonicalPolicy(
    set_like_paths=frozenset(
        {
            ("selections", "*", "selection_reasons"),
        }
    ),
    semantic_sequence_paths=frozenset(
        {
            ("chapter_manifest_bindings",),
            ("selections",),
        }
    ),
)
_AUDIT_RECORD_POLICY = CanonicalPolicy(
    set_like_paths=frozenset(),
    semantic_sequence_paths=frozenset({("outcomes",)}),
)
_BUNDLE_POLICY = CanonicalPolicy(
    set_like_paths=frozenset(),
    semantic_sequence_paths=frozenset(
        {
            ("chapter_order",),
            ("chapters",),
            ("artifact_index",),
        }
    ),
)


class D2LCommunityFiveChapterError(ValueError):
    def __init__(self, code: str, detail: str) -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail


@dataclass(frozen=True, slots=True)
class ManualAlignmentOverrideV1:
    override_id: str
    chapter_id: str
    source_block_ids: tuple[str, ...]
    target_segment_ids: tuple[str, ...]
    reason_code: str


@dataclass(frozen=True, slots=True)
class D2LCommunityFiveChapterInputsV1:
    finalization_payload_sha256: str
    candidate_tree_sha256: str
    repository_commit: str
    chapter_order: tuple[str, ...]
    common_by_chapter: Mapping[str, CommonEvaluationInputV1]
    target_by_chapter: Mapping[str, D2LCommunityTargetReadModelV1]


def load_d2l_community_five_chapter_inputs(
    *,
    finalization_path: str | Path,
    community_repository_root: str | Path,
    repository_commit: str,
    chapter_directories: Mapping[str, str],
    chapter_order: Sequence[str],
) -> D2LCommunityFiveChapterInputsV1:
    """Load exact source and Community snapshots without mutating either producer."""

    require_commit(repository_commit, path="$.repository_commit")
    selected = tuple(chapter_order)
    if not selected:
        raise D2LCommunityFiveChapterError(
            "chapter_order", "at least one chapter is required"
        )
    if len(selected) != len(set(selected)):
        raise D2LCommunityFiveChapterError(
            "chapter_order", "chapter IDs must be unique"
        )
    if set(chapter_directories) != set(selected):
        raise D2LCommunityFiveChapterError(
            "chapter_directories",
            "chapter directory mapping must exact-cover chapter_order",
        )

    finalization_file = Path(finalization_path).resolve()
    finalization = _load_json(finalization_file, "finalization")
    finalization_sha = _validate_finalization(finalization)
    project_root = finalization_file.parents[2]
    candidate = require_mapping(finalization["candidate"], path="$.candidate")
    candidate_root = _contained_path(
        project_root,
        require_string(candidate["relative_path"], path="$.candidate.relative_path"),
        label="candidate.relative_path",
    )
    candidate_tree_sha = require_sha256(
        candidate["tree_sha256"], path="$.candidate.tree_sha256"
    )
    expected_candidate_id = "srcpkg_" + candidate_tree_sha
    if candidate.get("candidate_id") != expected_candidate_id:
        raise D2LCommunityFiveChapterError(
            "candidate_identity", "candidate_id does not match candidate tree hash"
        )

    package = require_mapping(finalization["package"], path="$.package")
    document = _load_bound_component(
        candidate_root / "document.json",
        package,
        key="document",
    )
    structure = _load_bound_component(
        candidate_root / "structure_manifest.json",
        package,
        key="structure",
    )
    asset_manifest = _load_bound_component(
        candidate_root / "asset_manifest.json",
        package,
        key="asset_manifest",
    )
    projection = _load_bound_component(
        candidate_root / "admitted_projection_v1.json",
        package,
        key="admitted_projection",
    )

    document_id = require_string(document.get("doc_id"), path="$.document.doc_id")
    if document_id != finalization.get("doc_id"):
        raise D2LCommunityFiveChapterError(
            "document_identity", "document and finalization doc_id differ"
        )
    document_blocks = _validate_document(document)
    projection_by_id = _validate_projection(
        projection,
        document_blocks=document_blocks,
        document_sha256=package["document"]["sha256"],
        structure_sha256=package["structure"]["sha256"],
        asset_manifest_sha256=package["asset_manifest"]["sha256"],
    )

    policy = require_mapping(finalization["policies"], path="$.policies")
    admission_policy = require_mapping(policy["admission"], path="$.policies.admission")
    source_binding = CanonicalSourcePackageBindingV1(
        project_id=document_id,
        document_id=document_id,
        document=CanonicalComponentIdentityV1(
            require_string(package["document"]["schema_version"], path="$.package.document.schema_version"),
            require_sha256(package["document"]["sha256"], path="$.package.document.sha256"),
        ),
        structure=CanonicalComponentIdentityV1(
            require_string(package["structure"]["schema_version"], path="$.package.structure.schema_version"),
            require_sha256(package["structure"]["sha256"], path="$.package.structure.sha256"),
        ),
        asset_manifest=CanonicalComponentIdentityV1(
            require_string(
                package["asset_manifest"]["schema_version"],
                path="$.package.asset_manifest.schema_version",
            ),
            require_sha256(
                package["asset_manifest"]["sha256"],
                path="$.package.asset_manifest.sha256",
            ),
        ),
        admitted_projection=CanonicalProjectionIdentityV1(
            require_string(
                package["admitted_projection"]["schema_version"],
                path="$.package.admitted_projection.schema_version",
            ),
            require_sha256(
                package["admitted_projection"]["sha256"],
                path="$.package.admitted_projection.sha256",
            ),
        ),
        admission_policy=AdmissionPolicyIdentityV1(
            require_string(
                admission_policy["policy_id"], path="$.policies.admission.policy_id"
            ),
            require_string(
                admission_policy["policy_version"],
                path="$.policies.admission.policy_version",
            ),
            require_sha256(
                admission_policy["policy_sha256"],
                path="$.policies.admission.policy_sha256",
            ),
        ),
    )

    by_chapter: dict[str, list[Mapping[str, Any]]] = {}
    for row in document_blocks:
        by_chapter.setdefault(str(row["chapter_id"]), []).append(row)
    missing_chapters = [chapter_id for chapter_id in selected if chapter_id not in by_chapter]
    if missing_chapters:
        raise D2LCommunityFiveChapterError(
            "chapter_scope", "missing chapters: " + ",".join(missing_chapters)
        )

    common_by_chapter: dict[str, CommonEvaluationInputV1] = {}
    target_by_chapter: dict[str, D2LCommunityTargetReadModelV1] = {}
    community_root = Path(community_repository_root).resolve()
    _validate_repository_snapshot(community_root, repository_commit)
    for chapter_id in selected:
        blocks: list[CommonBlockV1] = []
        for row in by_chapter[chapter_id]:
            projection_row = projection_by_id[row["block_id"]]
            admission = _CHANNEL_TO_ADMISSION[projection_row["channel"]]
            blocks.append(
                CommonBlockV1(
                    block_id=str(row["block_id"]),
                    chapter_id=chapter_id,
                    order_index=int(row["order_index"]),
                    block_type=_alignment_block_type(str(row["block_type"])),
                    source_text=str(row["clean_text"]),
                    admission=admission,
                )
            )
        language_anchor = CommonArmV1(
            artifact_id=f"alignment_language_anchor__{chapter_id}",
            artifact_sha256=hashlib.sha256(
                f"{finalization_sha}\0{chapter_id}\0en\0vi".encode("utf-8")
            ).hexdigest(),
            logical_run_id="alignment_only",
            attempt_run_id="alignment_only",
            arm_id="alignment_language_anchor",
            profile_id="alignment_only_en_vi_v1",
            profile_config_sha256=hashlib.sha256(
                b"alignment_only_en_vi_v1"
            ).hexdigest(),
            source_language="en",
            target_language="vi",
        )
        common_by_chapter[chapter_id] = CommonEvaluationInputV1(
            source_schema_id="CanonicalSourcePackageV1",
            source_schema_version=require_string(
                document["schema_version"], path="$.document.schema_version"
            ),
            source_binding=source_binding,
            blocks=tuple(blocks),
            arms=(language_anchor,),
            translations=(),
        )

        relative_directory = Path(chapter_directories[chapter_id])
        if relative_directory.is_absolute() or ".." in relative_directory.parts:
            raise D2LCommunityFiveChapterError(
                "community_path", f"invalid chapter directory: {relative_directory}"
            )
        chapter_root = (community_root / relative_directory).resolve()
        if community_root not in chapter_root.parents:
            raise D2LCommunityFiveChapterError(
                "community_path", "chapter directory escapes Community repository"
            )
        target_by_chapter[chapter_id] = build_d2l_community_target_read_model(
            chapter_root,
            repository_commit=repository_commit,
            artifact_id=f"community__{chapter_id}__{repository_commit[:12]}",
            project_id=document_id,
            document_id=document_id,
            chapter_id=chapter_id,
        )
        _validate_origin_exact(
            common_by_chapter[chapter_id],
            target_by_chapter[chapter_id],
        )

    # Keep these component loads live: their canonical hashes are part of the
    # source binding even though alignment reads blocks from document/projection.
    if not structure or not asset_manifest:
        raise D2LCommunityFiveChapterError(
            "source_binding", "structure and asset manifest must be non-empty"
        )
    return D2LCommunityFiveChapterInputsV1(
        finalization_payload_sha256=finalization_sha,
        candidate_tree_sha256=candidate_tree_sha,
        repository_commit=repository_commit,
        chapter_order=selected,
        common_by_chapter=common_by_chapter,
        target_by_chapter=target_by_chapter,
    )


def build_d2l_community_manual_decision(
    inputs: D2LCommunityFiveChapterInputsV1,
    overrides: Sequence[ManualAlignmentOverrideV1],
    *,
    decision_id: str,
    created_at: str,
    reviewer_kind: str,
    reviewer_id: str,
) -> dict[str, Any]:
    require_rfc3339(created_at, path="$.created_at")
    require_string(decision_id, path="$.decision_id")
    if reviewer_kind not in {"human", "ai_assisted_manual"}:
        raise D2LCommunityFiveChapterError(
            "reviewer_kind", "reviewer_kind must be human or ai_assisted_manual"
        )
    require_string(reviewer_id, path="$.reviewer.identifier")

    normalized_overrides = _validate_override_objects(inputs, overrides)
    chapters = []
    for chapter_id in inputs.chapter_order:
        common = inputs.common_by_chapter[chapter_id]
        target = inputs.target_by_chapter[chapter_id]
        chapters.append(
            {
                "chapter_id": chapter_id,
                "source_read_model_sha256": alignment_source_read_model_sha256(common),
                "target_artifact_sha256": target.snapshot.artifact_sha256,
                "target_segments_sha256": target.snapshot.segments_sha256,
                "target_files_sha256": target.files_sha256,
                "origin_files_sha256": target.origin_files_sha256,
            }
        )
    payload = {
        "schema_id": MANUAL_DECISION_SCHEMA_ID,
        "schema_version": SCHEMA_VERSION,
        "decision_id": decision_id,
        "created_at": created_at,
        "reviewer": {"kind": reviewer_kind, "identifier": reviewer_id},
        "source_identity": {
            "finalization_payload_sha256": inputs.finalization_payload_sha256,
            "candidate_tree_sha256": inputs.candidate_tree_sha256,
        },
        "target_identity": {
            "repository_commit": inputs.repository_commit,
        },
        "chapters": chapters,
        "overrides": normalized_overrides,
        "integrity": {"decision_sha256": "0" * 64},
    }
    return validate_d2l_community_manual_decision(
        seal_payload(
            payload,
            policy=_MANUAL_POLICY,
            hash_path=("integrity", "decision_sha256"),
        )
    )


def validate_d2l_community_manual_decision(
    value: Mapping[str, Any],
) -> dict[str, Any]:
    root = require_mapping(value, path="$")
    require_exact_keys(
        root,
        required={
            "schema_id",
            "schema_version",
            "decision_id",
            "created_at",
            "reviewer",
            "source_identity",
            "target_identity",
            "chapters",
            "overrides",
            "integrity",
        },
        path="$",
    )
    reviewer = require_mapping(root["reviewer"], path="$.reviewer")
    require_exact_keys(
        reviewer, required={"kind", "identifier"}, path="$.reviewer"
    )
    source_identity = require_mapping(
        root["source_identity"], path="$.source_identity"
    )
    require_exact_keys(
        source_identity,
        required={"finalization_payload_sha256", "candidate_tree_sha256"},
        path="$.source_identity",
    )
    target_identity = require_mapping(
        root["target_identity"], path="$.target_identity"
    )
    require_exact_keys(
        target_identity,
        required={"repository_commit"},
        path="$.target_identity",
    )
    chapters = _validate_manual_chapters(root["chapters"])
    overrides = _validate_manual_override_rows(root["overrides"])
    integrity = require_mapping(root["integrity"], path="$.integrity")
    require_exact_keys(
        integrity, required={"decision_sha256"}, path="$.integrity"
    )
    normalized = {
        "schema_id": require_enum(
            root["schema_id"], {MANUAL_DECISION_SCHEMA_ID}, path="$.schema_id"
        ),
        "schema_version": require_enum(
            root["schema_version"], {SCHEMA_VERSION}, path="$.schema_version"
        ),
        "decision_id": require_string(root["decision_id"], path="$.decision_id"),
        "created_at": require_rfc3339(root["created_at"], path="$.created_at"),
        "reviewer": {
            "kind": require_enum(
                reviewer["kind"],
                {"human", "ai_assisted_manual"},
                path="$.reviewer.kind",
            ),
            "identifier": require_string(
                reviewer["identifier"], path="$.reviewer.identifier"
            ),
        },
        "source_identity": {
            "finalization_payload_sha256": require_sha256(
                source_identity["finalization_payload_sha256"],
                path="$.source_identity.finalization_payload_sha256",
            ),
            "candidate_tree_sha256": require_sha256(
                source_identity["candidate_tree_sha256"],
                path="$.source_identity.candidate_tree_sha256",
            ),
        },
        "target_identity": {
            "repository_commit": require_commit(
                target_identity["repository_commit"],
                path="$.target_identity.repository_commit",
            )
        },
        "chapters": chapters,
        "overrides": overrides,
        "integrity": {
            "decision_sha256": require_sha256(
                integrity["decision_sha256"], path="$.integrity.decision_sha256"
            )
        },
    }
    if not verify_payload_hash(
        normalized,
        policy=_MANUAL_POLICY,
        hash_path=("integrity", "decision_sha256"),
    ):
        raise ContractValidationError(
            "decision_hash",
            "$.integrity.decision_sha256",
            "manual decision self-hash does not match canonical content",
        )
    canonical = canonicalize(normalized, policy=_MANUAL_POLICY)
    if not isinstance(canonical, dict):
        raise AssertionError("manual decision must remain an object")
    return canonical


def build_d2l_community_chapter_review_manifest(
    inputs: D2LCommunityFiveChapterInputsV1,
    manual_decision: Mapping[str, Any],
    *,
    chapter_id: str,
    manifest_id: str,
    created_at: str,
    producer_code_commit: str,
    implementation_commit: str,
) -> dict[str, Any]:
    decision = validate_d2l_community_manual_decision(manual_decision)
    _validate_manual_binding(inputs, decision)
    if chapter_id not in inputs.common_by_chapter:
        raise D2LCommunityFiveChapterError(
            "chapter_id", f"chapter is not selected: {chapter_id}"
        )
    require_commit(producer_code_commit, path="$.producer.code_commit")
    require_commit(implementation_commit, path="$.method.implementation_commit")

    common = inputs.common_by_chapter[chapter_id]
    target = inputs.target_by_chapter[chapter_id]
    source_rows = _source_structural_rows(common)
    target_rows = list(target.structural_rows)
    overrides = [
        row for row in decision["overrides"] if row["chapter_id"] == chapter_id
    ]
    override_by_source = {row["source_block_ids"][0]: row for row in overrides}
    override_by_target = {row["target_segment_ids"][0]: row for row in overrides}

    mappings: list[dict[str, Any]] = []
    source_index = 0
    target_index = 0
    mapping_index = 0
    while source_index < len(source_rows) and target_index < len(target_rows):
        source_id = source_rows[source_index]["block_id"]
        target_id = target_rows[target_index].segment_id
        by_source = override_by_source.get(source_id)
        by_target = override_by_target.get(target_id)
        if by_source is not None or by_target is not None:
            if by_source is None or by_target is None or by_source != by_target:
                raise D2LCommunityFiveChapterError(
                    "manual_anchor_order",
                    f"manual source/target anchors diverge at {source_id}/{target_id}",
                )
            source_ids = by_source["source_block_ids"]
            target_ids = by_source["target_segment_ids"]
            observed_source = [
                row["block_id"]
                for row in source_rows[
                    source_index : source_index + len(source_ids)
                ]
            ]
            observed_target = [
                row.segment_id
                for row in target_rows[
                    target_index : target_index + len(target_ids)
                ]
            ]
            if observed_source != source_ids or observed_target != target_ids:
                raise D2LCommunityFiveChapterError(
                    "manual_anchor_span",
                    f"manual override is not contiguous at {by_source['override_id']}",
                )
            mapping_index += 1
            mappings.append(
                {
                    "mapping_id": f"{chapter_id}__community_map_{mapping_index:04d}",
                    "chapter_id": chapter_id,
                    "mapping_kind": _mapping_kind(
                        len(source_ids), len(target_ids)
                    ),
                    "decision_state": "reviewed",
                    "confidence": None,
                    "source_block_ids": source_ids,
                    "target_segment_ids": target_ids,
                    "decision_artifact_id": decision["decision_id"],
                    "decision_artifact_sha256": decision["integrity"][
                        "decision_sha256"
                    ],
                }
            )
            source_index += len(source_ids)
            target_index += len(target_ids)
            continue

        source_row = source_rows[source_index]
        target_row = target_rows[target_index]
        if (
            source_row["section_slug"] != target_row.section_slug
            or source_row["block_type"] != target_row.block_type
        ):
            raise D2LCommunityFiveChapterError(
                "unreviewed_structural_difference",
                "unreviewed structural mismatch at "
                f"{source_row['block_id']}/{target_row.segment_id}",
            )
        mapping_index += 1
        mappings.append(
            {
                "mapping_id": f"{chapter_id}__community_map_{mapping_index:04d}",
                "chapter_id": chapter_id,
                "mapping_kind": "1:1",
                "decision_state": "review_required",
                "confidence": None,
                "source_block_ids": [source_row["block_id"]],
                "target_segment_ids": [target_row.segment_id],
                "decision_artifact_id": None,
                "decision_artifact_sha256": None,
            }
        )
        source_index += 1
        target_index += 1

    if source_index != len(source_rows) or target_index != len(target_rows):
        raise D2LCommunityFiveChapterError(
            "alignment_exact_cover",
            f"alignment stopped at {source_index}/{len(source_rows)} source and "
            f"{target_index}/{len(target_rows)} target rows",
        )
    manifest = seal_alignment_manifest(
        {
            "schema_id": ALIGNMENT_MANIFEST_SCHEMA_ID,
            "schema_version": ALIGNMENT_MANIFEST_SCHEMA_VERSION,
            "manifest_id": manifest_id,
            "created_at": created_at,
            "producer": {
                "workstream": "evaluation",
                "component": "d2l_community_manual_alignment",
                "component_version": SCHEMA_VERSION,
                "code_commit": producer_code_commit,
            },
            "source_read_model": {
                "project_id": common.project_id,
                "document_id": common.document_id,
                "source_schema_id": common.source_schema_id,
                "source_schema_version": common.source_schema_version,
                "source_read_model_sha256": alignment_source_read_model_sha256(
                    common
                ),
                "eligible_source_block_count": len(source_rows),
            },
            "target_snapshot": {
                "artifact_id": target.snapshot.artifact_id,
                "artifact_sha256": target.snapshot.artifact_sha256,
                "project_id": target.snapshot.project_id,
                "document_id": target.snapshot.document_id,
                "arm_id": target.snapshot.arm_id,
                "source_language": target.snapshot.source_language,
                "target_language": target.snapshot.target_language,
                "segments_sha256": target.snapshot.segments_sha256,
                "target_segment_count": len(target.snapshot.segments),
            },
            "method": {
                "method_id": "d2l_manual_anchor_structural_alignment",
                "method_version": SCHEMA_VERSION,
                "implementation_commit": implementation_commit,
                "prompt_version": None,
                "model_id": None,
            },
            "mappings": mappings,
            "coverage": _coverage(mappings),
            "integrity": {"manifest_sha256": "0" * 64},
        }
    )
    return validate_alignment_bindings(manifest, common, target.snapshot)


def build_d2l_community_alignment_audit_plan(
    inputs: D2LCommunityFiveChapterInputsV1,
    chapter_manifests: Mapping[str, Mapping[str, Any]],
    *,
    plan_id: str,
    created_at: str,
) -> dict[str, Any]:
    validated = _validate_manifest_set(inputs, chapter_manifests)
    population: list[dict[str, Any]] = []
    section_mapping_ids: dict[tuple[str, str], list[str]] = {}
    for chapter_id in inputs.chapter_order:
        common = inputs.common_by_chapter[chapter_id]
        target = inputs.target_by_chapter[chapter_id]
        source_by_id = {row.block_id: row for row in common.blocks}
        target_by_id = {
            row.segment_id: segment
            for row, segment in zip(
                target.structural_rows, target.snapshot.segments, strict=True
            )
        }
        source_meta = {
            row["block_id"]: row for row in _source_structural_rows(common)
        }
        for mapping in validated[chapter_id]["mappings"]:
            if mapping["decision_state"] != "review_required":
                continue
            if mapping["mapping_kind"] != "1:1":
                raise D2LCommunityFiveChapterError(
                    "audit_population", "only 1:1 rows may enter structural sampling"
                )
            mapping_id = mapping["mapping_id"]
            source_id = mapping["source_block_ids"][0]
            target_id = mapping["target_segment_ids"][0]
            section_slug = source_meta[source_id]["section_slug"]
            detail = {
                "mapping_id": mapping_id,
                "chapter_id": chapter_id,
                "section_slug": section_slug,
                "source_block_id": source_id,
                "target_segment_id": target_id,
                "source_text": source_by_id[source_id].source_text,
                "target_text": target_by_id[target_id].text,
            }
            population.append(detail)
            section_mapping_ids.setdefault((chapter_id, section_slug), []).append(
                mapping_id
            )
    if not population:
        raise D2LCommunityFiveChapterError(
            "audit_population", "no review-required 1:1 mappings remain"
        )

    reasons: dict[str, set[str]] = {}
    for mapping_ids in section_mapping_ids.values():
        reasons.setdefault(mapping_ids[0], set()).add("section_first")
        reasons.setdefault(mapping_ids[-1], set()).add("section_last")
    sample_count = min(
        len(population),
        max(
            _AUDIT_MINIMUM,
            math.ceil(len(population) * _AUDIT_FRACTION),
            len(reasons),
        ),
    )
    manifest_bindings = [
        {
            "chapter_id": chapter_id,
            "manifest_id": validated[chapter_id]["manifest_id"],
            "manifest_sha256": validated[chapter_id]["integrity"][
                "manifest_sha256"
            ],
        }
        for chapter_id in inputs.chapter_order
    ]
    seed = canonical_sha256(
        {
            "policy_id": _AUDIT_POLICY_ID,
            "policy_version": _AUDIT_POLICY_VERSION,
            "chapter_manifest_bindings": manifest_bindings,
        },
        policy=CanonicalPolicy(
            set_like_paths=frozenset(),
            semantic_sequence_paths=frozenset({("chapter_manifest_bindings",)}),
        ),
    )
    remaining = [
        row for row in population if row["mapping_id"] not in reasons
    ]
    remaining.sort(
        key=lambda row: hashlib.sha256(
            f"{seed}\0{row['mapping_id']}".encode("utf-8")
        ).hexdigest()
    )
    for row in remaining[: sample_count - len(reasons)]:
        reasons.setdefault(row["mapping_id"], set()).add("deterministic_hash")

    selections = []
    for row in population:
        selection_reasons = reasons.get(row["mapping_id"])
        if not selection_reasons:
            continue
        selections.append(
            {
                **row,
                "selection_reasons": sorted(selection_reasons),
            }
        )
    payload = {
        "schema_id": AUDIT_PLAN_SCHEMA_ID,
        "schema_version": SCHEMA_VERSION,
        "plan_id": plan_id,
        "created_at": created_at,
        "policy_id": _AUDIT_POLICY_ID,
        "policy_version": _AUDIT_POLICY_VERSION,
        "population_count": len(population),
        "sample_count": len(selections),
        "chapter_manifest_bindings": manifest_bindings,
        "selections": selections,
        "integrity": {"plan_sha256": "0" * 64},
    }
    return validate_d2l_community_alignment_audit_plan(
        seal_payload(
            payload,
            policy=_AUDIT_PLAN_POLICY,
            hash_path=("integrity", "plan_sha256"),
        )
    )


def validate_d2l_community_alignment_audit_plan(
    value: Mapping[str, Any],
) -> dict[str, Any]:
    root = require_mapping(value, path="$")
    require_exact_keys(
        root,
        required={
            "schema_id",
            "schema_version",
            "plan_id",
            "created_at",
            "policy_id",
            "policy_version",
            "population_count",
            "sample_count",
            "chapter_manifest_bindings",
            "selections",
            "integrity",
        },
        path="$",
    )
    bindings = []
    for index, raw in enumerate(
        require_list(
            root["chapter_manifest_bindings"],
            path="$.chapter_manifest_bindings",
        )
    ):
        path = f"$.chapter_manifest_bindings[{index}]"
        row = require_mapping(raw, path=path)
        require_exact_keys(
            row,
            required={"chapter_id", "manifest_id", "manifest_sha256"},
            path=path,
        )
        bindings.append(
            {
                "chapter_id": require_string(
                    row["chapter_id"], path=f"{path}.chapter_id"
                ),
                "manifest_id": require_string(
                    row["manifest_id"], path=f"{path}.manifest_id"
                ),
                "manifest_sha256": require_sha256(
                    row["manifest_sha256"], path=f"{path}.manifest_sha256"
                ),
            }
        )
    require_unique(
        [row["chapter_id"] for row in bindings],
        path="$.chapter_manifest_bindings.chapter_id",
    )
    selections = []
    for index, raw in enumerate(
        require_list(root["selections"], path="$.selections")
    ):
        path = f"$.selections[{index}]"
        row = require_mapping(raw, path=path)
        require_exact_keys(
            row,
            required={
                "mapping_id",
                "chapter_id",
                "section_slug",
                "source_block_id",
                "target_segment_id",
                "source_text",
                "target_text",
                "selection_reasons",
            },
            path=path,
        )
        selection_reasons = [
            require_enum(
                item,
                {"section_first", "section_last", "deterministic_hash"},
                path=f"{path}.selection_reasons[{reason_index}]",
            )
            for reason_index, item in enumerate(
                require_list(
                    row["selection_reasons"],
                    path=f"{path}.selection_reasons",
                )
            )
        ]
        if not selection_reasons:
            raise ContractValidationError(
                "empty_array",
                f"{path}.selection_reasons",
                "selection reasons are required",
            )
        selections.append(
            {
                "mapping_id": require_string(
                    row["mapping_id"], path=f"{path}.mapping_id"
                ),
                "chapter_id": require_string(
                    row["chapter_id"], path=f"{path}.chapter_id"
                ),
                "section_slug": require_string(
                    row["section_slug"], path=f"{path}.section_slug"
                ),
                "source_block_id": require_string(
                    row["source_block_id"], path=f"{path}.source_block_id"
                ),
                "target_segment_id": require_string(
                    row["target_segment_id"], path=f"{path}.target_segment_id"
                ),
                "source_text": require_string(
                    row["source_text"], path=f"{path}.source_text"
                ),
                "target_text": require_string(
                    row["target_text"], path=f"{path}.target_text"
                ),
                "selection_reasons": selection_reasons,
            }
        )
    require_unique(
        [row["mapping_id"] for row in selections],
        path="$.selections.mapping_id",
    )
    integrity = require_mapping(root["integrity"], path="$.integrity")
    require_exact_keys(integrity, required={"plan_sha256"}, path="$.integrity")
    population_count = _require_nonnegative_int(
        root["population_count"], "$.population_count"
    )
    sample_count = _require_nonnegative_int(root["sample_count"], "$.sample_count")
    if sample_count != len(selections) or sample_count > population_count:
        raise ContractValidationError(
            "sample_count",
            "$.sample_count",
            "sample count must equal selections and not exceed population",
        )
    normalized = {
        "schema_id": require_enum(
            root["schema_id"], {AUDIT_PLAN_SCHEMA_ID}, path="$.schema_id"
        ),
        "schema_version": require_enum(
            root["schema_version"], {SCHEMA_VERSION}, path="$.schema_version"
        ),
        "plan_id": require_string(root["plan_id"], path="$.plan_id"),
        "created_at": require_rfc3339(root["created_at"], path="$.created_at"),
        "policy_id": require_enum(
            root["policy_id"], {_AUDIT_POLICY_ID}, path="$.policy_id"
        ),
        "policy_version": require_enum(
            root["policy_version"],
            {_AUDIT_POLICY_VERSION},
            path="$.policy_version",
        ),
        "population_count": population_count,
        "sample_count": sample_count,
        "chapter_manifest_bindings": bindings,
        "selections": selections,
        "integrity": {
            "plan_sha256": require_sha256(
                integrity["plan_sha256"], path="$.integrity.plan_sha256"
            )
        },
    }
    if not verify_payload_hash(
        normalized,
        policy=_AUDIT_PLAN_POLICY,
        hash_path=("integrity", "plan_sha256"),
    ):
        raise ContractValidationError(
            "plan_hash",
            "$.integrity.plan_sha256",
            "audit plan self-hash does not match canonical content",
        )
    canonical = canonicalize(normalized, policy=_AUDIT_PLAN_POLICY)
    if not isinstance(canonical, dict):
        raise AssertionError("audit plan must remain an object")
    return canonical


def record_d2l_community_alignment_audit(
    audit_plan: Mapping[str, Any],
    outcomes: Mapping[str, bool],
    *,
    record_id: str,
    created_at: str,
    reviewer_kind: str,
    reviewer_id: str,
) -> dict[str, Any]:
    plan = validate_d2l_community_alignment_audit_plan(audit_plan)
    expected_ids = [row["mapping_id"] for row in plan["selections"]]
    if set(outcomes) != set(expected_ids) or len(outcomes) != len(expected_ids):
        raise D2LCommunityFiveChapterError(
            "audit_exact_cover", "audit outcomes must exact-cover sampled mappings"
        )
    if any(type(value) is not bool for value in outcomes.values()):
        raise D2LCommunityFiveChapterError(
            "audit_outcome_type", "audit outcomes must be booleans"
        )
    payload = {
        "schema_id": AUDIT_RECORD_SCHEMA_ID,
        "schema_version": SCHEMA_VERSION,
        "record_id": record_id,
        "created_at": created_at,
        "reviewer": {"kind": reviewer_kind, "identifier": reviewer_id},
        "plan_id": plan["plan_id"],
        "plan_sha256": plan["integrity"]["plan_sha256"],
        "outcomes": [
            {"mapping_id": mapping_id, "alignment_ok": outcomes[mapping_id]}
            for mapping_id in expected_ids
        ],
        "all_sampled_mappings_passed": all(outcomes.values()),
        "integrity": {"record_sha256": "0" * 64},
    }
    return validate_d2l_community_alignment_audit_record(
        seal_payload(
            payload,
            policy=_AUDIT_RECORD_POLICY,
            hash_path=("integrity", "record_sha256"),
        )
    )


def validate_d2l_community_alignment_audit_record(
    value: Mapping[str, Any],
) -> dict[str, Any]:
    root = require_mapping(value, path="$")
    require_exact_keys(
        root,
        required={
            "schema_id",
            "schema_version",
            "record_id",
            "created_at",
            "reviewer",
            "plan_id",
            "plan_sha256",
            "outcomes",
            "all_sampled_mappings_passed",
            "integrity",
        },
        path="$",
    )
    reviewer = require_mapping(root["reviewer"], path="$.reviewer")
    require_exact_keys(
        reviewer, required={"kind", "identifier"}, path="$.reviewer"
    )
    outcomes = []
    for index, raw in enumerate(require_list(root["outcomes"], path="$.outcomes")):
        path = f"$.outcomes[{index}]"
        row = require_mapping(raw, path=path)
        require_exact_keys(
            row, required={"mapping_id", "alignment_ok"}, path=path
        )
        if type(row["alignment_ok"]) is not bool:
            raise ContractValidationError(
                "type", f"{path}.alignment_ok", "alignment_ok must be boolean"
            )
        outcomes.append(
            {
                "mapping_id": require_string(
                    row["mapping_id"], path=f"{path}.mapping_id"
                ),
                "alignment_ok": row["alignment_ok"],
            }
        )
    require_unique(
        [row["mapping_id"] for row in outcomes], path="$.outcomes.mapping_id"
    )
    all_passed = root["all_sampled_mappings_passed"]
    if type(all_passed) is not bool:
        raise ContractValidationError(
            "type",
            "$.all_sampled_mappings_passed",
            "all_sampled_mappings_passed must be boolean",
        )
    if all_passed != all(row["alignment_ok"] for row in outcomes):
        raise ContractValidationError(
            "audit_summary",
            "$.all_sampled_mappings_passed",
            "audit summary does not match outcomes",
        )
    integrity = require_mapping(root["integrity"], path="$.integrity")
    require_exact_keys(integrity, required={"record_sha256"}, path="$.integrity")
    normalized = {
        "schema_id": require_enum(
            root["schema_id"], {AUDIT_RECORD_SCHEMA_ID}, path="$.schema_id"
        ),
        "schema_version": require_enum(
            root["schema_version"], {SCHEMA_VERSION}, path="$.schema_version"
        ),
        "record_id": require_string(root["record_id"], path="$.record_id"),
        "created_at": require_rfc3339(root["created_at"], path="$.created_at"),
        "reviewer": {
            "kind": require_enum(
                reviewer["kind"],
                {"human", "ai_assisted_manual"},
                path="$.reviewer.kind",
            ),
            "identifier": require_string(
                reviewer["identifier"], path="$.reviewer.identifier"
            ),
        },
        "plan_id": require_string(root["plan_id"], path="$.plan_id"),
        "plan_sha256": require_sha256(
            root["plan_sha256"], path="$.plan_sha256"
        ),
        "outcomes": outcomes,
        "all_sampled_mappings_passed": all_passed,
        "integrity": {
            "record_sha256": require_sha256(
                integrity["record_sha256"], path="$.integrity.record_sha256"
            )
        },
    }
    if not verify_payload_hash(
        normalized,
        policy=_AUDIT_RECORD_POLICY,
        hash_path=("integrity", "record_sha256"),
    ):
        raise ContractValidationError(
            "record_hash",
            "$.integrity.record_sha256",
            "audit record self-hash does not match canonical content",
        )
    canonical = canonicalize(normalized, policy=_AUDIT_RECORD_POLICY)
    if not isinstance(canonical, dict):
        raise AssertionError("audit record must remain an object")
    return canonical


def apply_d2l_community_alignment_audit(
    inputs: D2LCommunityFiveChapterInputsV1,
    chapter_manifests: Mapping[str, Mapping[str, Any]],
    audit_plan: Mapping[str, Any],
    audit_record: Mapping[str, Any],
) -> dict[str, dict[str, Any]]:
    manifests = _validate_manifest_set(inputs, chapter_manifests)
    plan = validate_d2l_community_alignment_audit_plan(audit_plan)
    record = validate_d2l_community_alignment_audit_record(audit_record)
    expected_bindings = [
        {
            "chapter_id": chapter_id,
            "manifest_id": manifests[chapter_id]["manifest_id"],
            "manifest_sha256": manifests[chapter_id]["integrity"][
                "manifest_sha256"
            ],
        }
        for chapter_id in inputs.chapter_order
    ]
    if plan["chapter_manifest_bindings"] != expected_bindings:
        raise D2LCommunityFiveChapterError(
            "audit_manifest_binding", "audit plan binds a different manifest set"
        )
    if (
        record["plan_id"] != plan["plan_id"]
        or record["plan_sha256"] != plan["integrity"]["plan_sha256"]
    ):
        raise D2LCommunityFiveChapterError(
            "audit_plan_binding", "audit record binds a different plan"
        )
    outcomes = {
        row["mapping_id"]: row["alignment_ok"] for row in record["outcomes"]
    }
    if list(outcomes) != [row["mapping_id"] for row in plan["selections"]]:
        raise D2LCommunityFiveChapterError(
            "audit_outcome_order", "audit record outcome order differs from plan"
        )
    failed_sections = {
        (row["chapter_id"], row["section_slug"])
        for row in plan["selections"]
        if not outcomes[row["mapping_id"]]
    }

    finalized: dict[str, dict[str, Any]] = {}
    for chapter_id in inputs.chapter_order:
        original = manifests[chapter_id]
        mappings = copy.deepcopy(original["mappings"])
        source_meta = {
            row["block_id"]: row
            for row in _source_structural_rows(inputs.common_by_chapter[chapter_id])
        }
        for mapping in mappings:
            if mapping["decision_state"] != "review_required":
                continue
            section_slug = source_meta[mapping["source_block_ids"][0]][
                "section_slug"
            ]
            if (chapter_id, section_slug) in failed_sections:
                continue
            mapping["decision_state"] = "auto_accepted"
            mapping["confidence"] = 1.0
        replacement = copy.deepcopy(original)
        replacement["mappings"] = mappings
        replacement["coverage"] = _coverage(mappings)
        replacement["integrity"]["manifest_sha256"] = "0" * 64
        sealed = seal_alignment_manifest(replacement)
        finalized[chapter_id] = validate_alignment_bindings(
            sealed,
            inputs.common_by_chapter[chapter_id],
            inputs.target_by_chapter[chapter_id].snapshot,
        )
    return finalized


def write_d2l_community_alignment_bundle(
    *,
    output_parent: str | Path,
    inputs: D2LCommunityFiveChapterInputsV1,
    manual_decision: Mapping[str, Any],
    audit_plan: Mapping[str, Any],
    audit_record: Mapping[str, Any],
    chapter_manifests: Mapping[str, Mapping[str, Any]],
    created_at: str,
) -> Path:
    decision = validate_d2l_community_manual_decision(manual_decision)
    plan = validate_d2l_community_alignment_audit_plan(audit_plan)
    record = validate_d2l_community_alignment_audit_record(audit_record)
    manifests = _validate_manifest_set(inputs, chapter_manifests)
    _validate_manual_binding(inputs, decision)

    parent = Path(output_parent).resolve()
    parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix=".community_alignment_", dir=parent))
    try:
        files: list[tuple[str, Mapping[str, Any]]] = [
            ("manual_decision.json", decision),
            ("audit_plan.json", plan),
            ("audit_record.json", record),
        ]
        for chapter_id in inputs.chapter_order:
            files.append(
                (
                    f"chapters/{chapter_id}/target_snapshot.json",
                    _target_snapshot_payload(
                        inputs.target_by_chapter[chapter_id]
                    ),
                )
            )
            files.append(
                (
                    f"chapters/{chapter_id}/alignment_manifest.json",
                    manifests[chapter_id],
                )
            )
        artifact_index = []
        for relative, payload in files:
            path = temporary / relative
            _write_json(path, payload)
            artifact_index.append(
                {
                    "artifact_ref": relative,
                    "sha256": _file_sha256(path),
                    "sha256_kind": "physical",
                }
            )
        accepted = all(
            manifest["coverage"]["review_mapping_count"] == 0
            and manifest["coverage"]["ambiguous_mapping_count"] == 0
            and manifest["coverage"]["missing_mapping_count"] == 0
            and manifest["coverage"]["added_mapping_count"] == 0
            for manifest in manifests.values()
        )
        bundle = seal_payload(
            {
                "schema_id": BUNDLE_SCHEMA_ID,
                "schema_version": SCHEMA_VERSION,
                "created_at": created_at,
                "project_id": next(iter(inputs.common_by_chapter.values())).project_id,
                "document_id": next(iter(inputs.common_by_chapter.values())).document_id,
                "community_arm_id": "community_unverified",
                "status": "accepted_alignment" if accepted else "review_required",
                "source_identity": {
                    "finalization_payload_sha256": inputs.finalization_payload_sha256,
                    "candidate_tree_sha256": inputs.candidate_tree_sha256,
                },
                "target_identity": {
                    "repository_commit": inputs.repository_commit,
                },
                "chapter_order": list(inputs.chapter_order),
                "chapters": [
                    {
                        "chapter_id": chapter_id,
                        "source_block_count": manifests[chapter_id]["coverage"][
                            "source_block_count"
                        ],
                        "target_segment_count": manifests[chapter_id]["coverage"][
                            "target_segment_count"
                        ],
                        "manifest_sha256": manifests[chapter_id]["integrity"][
                            "manifest_sha256"
                        ],
                    }
                    for chapter_id in inputs.chapter_order
                ],
                "manual_decision_sha256": decision["integrity"]["decision_sha256"],
                "audit_plan_sha256": plan["integrity"]["plan_sha256"],
                "audit_record_sha256": record["integrity"]["record_sha256"],
                "artifact_index": artifact_index,
                "integrity": {"bundle_sha256": "0" * 64},
            },
            policy=_BUNDLE_POLICY,
            hash_path=("integrity", "bundle_sha256"),
        )
        bundle_hash = bundle["integrity"]["bundle_sha256"]
        _write_json(temporary / "alignment_bundle.json", bundle)
        destination = parent / bundle_hash
        if destination.exists():
            if _tree_digest(destination) != _tree_digest(temporary):
                raise D2LCommunityFiveChapterError(
                    "immutable_output",
                    "content-addressed output exists with different bytes",
                )
            shutil.rmtree(temporary)
            return destination
        os.replace(temporary, destination)
        return destination
    except Exception:
        if temporary.exists():
            shutil.rmtree(temporary)
        raise


def _validate_override_objects(
    inputs: D2LCommunityFiveChapterInputsV1,
    overrides: Sequence[ManualAlignmentOverrideV1],
) -> list[dict[str, Any]]:
    rows = [
        {
            "override_id": require_string(
                row.override_id, path=f"$.overrides[{index}].override_id"
            ),
            "chapter_id": require_string(
                row.chapter_id, path=f"$.overrides[{index}].chapter_id"
            ),
            "source_block_ids": list(row.source_block_ids),
            "target_segment_ids": list(row.target_segment_ids),
            "reason_code": require_enum(
                row.reason_code,
                _MANUAL_REASON_CODES,
                path=f"$.overrides[{index}].reason_code",
            ),
        }
        for index, row in enumerate(overrides)
    ]
    validated = _validate_manual_override_rows(rows)
    selected = set(inputs.chapter_order)
    if any(row["chapter_id"] not in selected for row in validated):
        raise D2LCommunityFiveChapterError(
            "override_chapter", "manual override names an unselected chapter"
        )
    return validated


def _validate_manual_chapters(value: Any) -> list[dict[str, Any]]:
    rows = []
    for index, raw in enumerate(require_list(value, path="$.chapters")):
        path = f"$.chapters[{index}]"
        row = require_mapping(raw, path=path)
        require_exact_keys(
            row,
            required={
                "chapter_id",
                "source_read_model_sha256",
                "target_artifact_sha256",
                "target_segments_sha256",
                "target_files_sha256",
                "origin_files_sha256",
            },
            path=path,
        )
        rows.append(
            {
                "chapter_id": require_string(
                    row["chapter_id"], path=f"{path}.chapter_id"
                ),
                "source_read_model_sha256": require_sha256(
                    row["source_read_model_sha256"],
                    path=f"{path}.source_read_model_sha256",
                ),
                "target_artifact_sha256": require_sha256(
                    row["target_artifact_sha256"],
                    path=f"{path}.target_artifact_sha256",
                ),
                "target_segments_sha256": require_sha256(
                    row["target_segments_sha256"],
                    path=f"{path}.target_segments_sha256",
                ),
                "target_files_sha256": require_sha256(
                    row["target_files_sha256"],
                    path=f"{path}.target_files_sha256",
                ),
                "origin_files_sha256": require_sha256(
                    row["origin_files_sha256"],
                    path=f"{path}.origin_files_sha256",
                ),
            }
        )
    require_unique([row["chapter_id"] for row in rows], path="$.chapters.chapter_id")
    return rows


def _validate_manual_override_rows(value: Any) -> list[dict[str, Any]]:
    rows = []
    seen_source: list[str] = []
    seen_target: list[str] = []
    for index, raw in enumerate(require_list(value, path="$.overrides")):
        path = f"$.overrides[{index}]"
        row = require_mapping(raw, path=path)
        require_exact_keys(
            row,
            required={
                "override_id",
                "chapter_id",
                "source_block_ids",
                "target_segment_ids",
                "reason_code",
            },
            path=path,
        )
        source_ids = [
            require_string(item, path=f"{path}.source_block_ids[{item_index}]")
            for item_index, item in enumerate(
                require_list(
                    row["source_block_ids"], path=f"{path}.source_block_ids"
                )
            )
        ]
        target_ids = [
            require_string(item, path=f"{path}.target_segment_ids[{item_index}]")
            for item_index, item in enumerate(
                require_list(
                    row["target_segment_ids"], path=f"{path}.target_segment_ids"
                )
            )
        ]
        if not source_ids or not target_ids or (
            len(source_ids) == 1 and len(target_ids) == 1
        ):
            raise ContractValidationError(
                "override_cardinality",
                path,
                "manual overrides must be non-empty and non-1:1",
            )
        require_unique(source_ids, path=f"{path}.source_block_ids")
        require_unique(target_ids, path=f"{path}.target_segment_ids")
        seen_source.extend(source_ids)
        seen_target.extend(target_ids)
        rows.append(
            {
                "override_id": require_string(
                    row["override_id"], path=f"{path}.override_id"
                ),
                "chapter_id": require_string(
                    row["chapter_id"], path=f"{path}.chapter_id"
                ),
                "source_block_ids": source_ids,
                "target_segment_ids": target_ids,
                "reason_code": require_enum(
                    row["reason_code"],
                    _MANUAL_REASON_CODES,
                    path=f"{path}.reason_code",
                ),
            }
        )
    require_unique([row["override_id"] for row in rows], path="$.overrides.override_id")
    require_unique(seen_source, path="$.overrides.source_block_ids")
    require_unique(seen_target, path="$.overrides.target_segment_ids")
    return rows


def _validate_manual_binding(
    inputs: D2LCommunityFiveChapterInputsV1, decision: Mapping[str, Any]
) -> None:
    if decision["source_identity"] != {
        "finalization_payload_sha256": inputs.finalization_payload_sha256,
        "candidate_tree_sha256": inputs.candidate_tree_sha256,
    }:
        raise D2LCommunityFiveChapterError(
            "manual_source_binding", "manual decision binds a different source"
        )
    if decision["target_identity"]["repository_commit"] != inputs.repository_commit:
        raise D2LCommunityFiveChapterError(
            "manual_target_binding", "manual decision binds a different repository"
        )
    expected_chapters = []
    for chapter_id in inputs.chapter_order:
        common = inputs.common_by_chapter[chapter_id]
        target = inputs.target_by_chapter[chapter_id]
        expected_chapters.append(
            {
                "chapter_id": chapter_id,
                "source_read_model_sha256": alignment_source_read_model_sha256(common),
                "target_artifact_sha256": target.snapshot.artifact_sha256,
                "target_segments_sha256": target.snapshot.segments_sha256,
                "target_files_sha256": target.files_sha256,
                "origin_files_sha256": target.origin_files_sha256,
            }
        )
    if decision["chapters"] != expected_chapters:
        raise D2LCommunityFiveChapterError(
            "manual_chapter_binding", "manual decision chapter hashes drifted"
        )


def _validate_manifest_set(
    inputs: D2LCommunityFiveChapterInputsV1,
    manifests: Mapping[str, Mapping[str, Any]],
) -> dict[str, dict[str, Any]]:
    if set(manifests) != set(inputs.chapter_order):
        raise D2LCommunityFiveChapterError(
            "manifest_exact_cover", "manifest set must exact-cover selected chapters"
        )
    return {
        chapter_id: validate_alignment_bindings(
            manifests[chapter_id],
            inputs.common_by_chapter[chapter_id],
            inputs.target_by_chapter[chapter_id].snapshot,
        )
        for chapter_id in inputs.chapter_order
    }


def _source_structural_rows(
    common: CommonEvaluationInputV1,
) -> list[dict[str, Any]]:
    rows = []
    for block in common.blocks:
        if block.admission not in _ELIGIBLE_ADMISSIONS:
            continue
        prefix = block.chapter_id + "_"
        if not block.block_id.startswith(prefix):
            raise D2LCommunityFiveChapterError(
                "source_block_id", f"invalid chapter prefix: {block.block_id}"
            )
        match = _SOURCE_BLOCK_SUFFIX.fullmatch(block.block_id[len(prefix) :])
        if match is None:
            raise D2LCommunityFiveChapterError(
                "source_block_id", f"invalid section suffix: {block.block_id}"
            )
        if block.block_type not in {"heading", "prose"}:
            raise D2LCommunityFiveChapterError(
                "source_block_type",
                f"eligible alignment block has unsupported type: {block.block_id}",
            )
        rows.append(
            {
                "block_id": block.block_id,
                "section_slug": match.group("section"),
                "block_order_in_section": int(match.group("number")) - 1,
                "block_type": block.block_type,
            }
        )
    return rows


def _validate_origin_exact(
    common: CommonEvaluationInputV1,
    target: D2LCommunityTargetReadModelV1,
) -> None:
    source_rows = _source_structural_rows(common)
    origin_rows = list(target.origin_structural_rows)
    if len(source_rows) != len(origin_rows):
        raise D2LCommunityFiveChapterError(
            "origin_exact_cover",
            f"source/origin counts differ: {len(source_rows)}/{len(origin_rows)}",
        )
    source_by_id = {row.block_id: row for row in common.blocks}
    for source, origin in zip(source_rows, origin_rows, strict=True):
        if (
            source["section_slug"] != origin.section_slug
            or source["block_order_in_section"] != origin.block_order_in_section
            or source["block_type"] != origin.block_type
        ):
            raise D2LCommunityFiveChapterError(
                "origin_structure",
                f"source/origin structural drift at {source['block_id']}",
            )
        if source_by_id[source["block_id"]].source_text != origin.source_text:
            raise D2LCommunityFiveChapterError(
                "origin_text",
                f"source/origin text drift at {source['block_id']}",
            )


def _validate_finalization(value: Mapping[str, Any]) -> str:
    if value.get("schema_version") != "source_package_finalization_v1":
        raise D2LCommunityFiveChapterError(
            "finalization_schema", "unsupported finalization schema"
        )
    if not str(value.get("lifecycle") or "").startswith("finalized"):
        raise D2LCommunityFiveChapterError(
            "finalization_state", "source package is not finalized"
        )
    integrity = require_mapping(value.get("integrity"), path="$.integrity")
    require_exact_keys(
        integrity, required={"payload_sha256"}, path="$.integrity"
    )
    expected = require_sha256(
        integrity["payload_sha256"], path="$.integrity.payload_sha256"
    )
    body = copy.deepcopy(dict(value))
    body.pop("integrity")
    if canonical_json_sha256(body) != expected:
        raise D2LCommunityFiveChapterError(
            "finalization_hash", "finalization payload hash drift"
        )
    return expected


def _load_bound_component(
    path: Path, package: Mapping[str, Any], *, key: str
) -> dict[str, Any]:
    value = _load_json(path, key)
    binding = require_mapping(package.get(key), path=f"$.package.{key}")
    expected = require_sha256(binding.get("sha256"), path=f"$.package.{key}.sha256")
    if canonical_json_sha256(value) != expected:
        raise D2LCommunityFiveChapterError(
            "component_hash", f"{key} canonical hash drift"
        )
    return value


def _validate_document(document: Mapping[str, Any]) -> list[dict[str, Any]]:
    if document.get("schema_version") != "1.5.0":
        raise D2LCommunityFiveChapterError(
            "document_schema", "document schema must remain 1.5.0"
        )
    chapters = document.get("chapters")
    if not isinstance(chapters, list) or not chapters:
        raise D2LCommunityFiveChapterError(
            "document_chapters", "document chapters are missing"
        )
    rows = []
    seen_chapters: set[str] = set()
    seen_blocks: set[str] = set()
    global_order = 0
    for chapter_position, raw_chapter in enumerate(chapters):
        chapter = require_mapping(
            raw_chapter, path=f"$.document.chapters[{chapter_position}]"
        )
        chapter_id = require_string(
            chapter.get("chapter_id"),
            path=f"$.document.chapters[{chapter_position}].chapter_id",
        )
        if chapter_id in seen_chapters or chapter.get("order_index") != chapter_position:
            raise D2LCommunityFiveChapterError(
                "chapter_order", f"invalid chapter order: {chapter_id}"
            )
        seen_chapters.add(chapter_id)
        blocks = chapter.get("blocks")
        if not isinstance(blocks, list) or not blocks:
            raise D2LCommunityFiveChapterError(
                "chapter_blocks", f"chapter has no blocks: {chapter_id}"
            )
        for block_position, raw_block in enumerate(blocks):
            block = require_mapping(
                raw_block,
                path=f"$.document.chapters[{chapter_position}].blocks[{block_position}]",
            )
            block_id = require_string(
                block.get("block_id"), path="$.document.block.block_id"
            )
            if block_id in seen_blocks or block.get("order_index") != global_order:
                raise D2LCommunityFiveChapterError(
                    "block_order", f"invalid global block order: {block_id}"
                )
            seen_blocks.add(block_id)
            block_type = require_string(
                block.get("block_type"), path="$.document.block.block_type"
            )
            clean_text = block.get("clean_text")
            if not isinstance(clean_text, str):
                raise D2LCommunityFiveChapterError(
                    "block_text", f"block clean_text is invalid: {block_id}"
                )
            rows.append(
                {
                    "chapter_id": chapter_id,
                    "block_id": block_id,
                    "order_index": global_order,
                    "block_type": block_type,
                    "clean_text": clean_text,
                }
            )
            global_order += 1
    return rows


def _validate_projection(
    projection: Mapping[str, Any],
    *,
    document_blocks: Sequence[Mapping[str, Any]],
    document_sha256: str,
    structure_sha256: str,
    asset_manifest_sha256: str,
) -> dict[str, dict[str, str]]:
    if projection.get("schema_version") != "admitted_projection_v1":
        raise D2LCommunityFiveChapterError(
            "projection_schema", "unsupported admitted projection"
        )
    integrity = require_mapping(projection.get("integrity"), path="$.projection.integrity")
    expected_payload = require_sha256(
        integrity.get("payload_sha256"),
        path="$.projection.integrity.payload_sha256",
    )
    body = copy.deepcopy(dict(projection))
    body.pop("integrity")
    if canonical_json_sha256(body) != expected_payload:
        raise D2LCommunityFiveChapterError(
            "projection_hash", "projection payload hash drift"
        )
    inputs = require_mapping(projection.get("inputs"), path="$.projection.inputs")
    expected_inputs = {
        "document": document_sha256,
        "structure": structure_sha256,
        "asset_manifest": asset_manifest_sha256,
    }
    for key, expected in expected_inputs.items():
        row = require_mapping(inputs.get(key), path=f"$.projection.inputs.{key}")
        if row.get("sha256") != expected:
            raise D2LCommunityFiveChapterError(
                "projection_input", f"projection {key} binding drift"
            )
    raw_rows = projection.get("rows")
    if not isinstance(raw_rows, list) or len(raw_rows) != len(document_blocks):
        raise D2LCommunityFiveChapterError(
            "projection_exact_cover", "projection does not exact-cover document"
        )
    if integrity.get("row_count") != len(raw_rows):
        raise D2LCommunityFiveChapterError(
            "projection_count", "projection row_count drift"
        )
    result: dict[str, dict[str, str]] = {}
    for index, (raw, source) in enumerate(zip(raw_rows, document_blocks, strict=True)):
        row = require_mapping(raw, path=f"$.projection.rows[{index}]")
        block_id = require_string(
            row.get("block_id"), path=f"$.projection.rows[{index}].block_id"
        )
        chapter_id = require_string(
            row.get("chapter_id"), path=f"$.projection.rows[{index}].chapter_id"
        )
        channel = require_enum(
            row.get("channel"),
            _CHANNEL_TO_ADMISSION,
            path=f"$.projection.rows[{index}].channel",
        )
        if block_id != source["block_id"] or chapter_id != source["chapter_id"]:
            raise D2LCommunityFiveChapterError(
                "projection_order", f"projection order drift at row {index}"
            )
        result[block_id] = {
            "block_id": block_id,
            "chapter_id": chapter_id,
            "channel": channel,
        }
    return result


def _target_snapshot_payload(
    target: D2LCommunityTargetReadModelV1,
) -> dict[str, Any]:
    return {
        "schema_id": "AlignmentTargetSnapshotV1",
        "schema_version": SCHEMA_VERSION,
        "repository_commit": target.repository_commit,
        "files_sha256": target.files_sha256,
        "origin_files_sha256": target.origin_files_sha256,
        "snapshot": {
            **{
                key: value
                for key, value in asdict(target.snapshot).items()
                if key != "segments"
            },
            "segments": [asdict(segment) for segment in target.snapshot.segments],
        },
    }


def _coverage(mappings: Sequence[Mapping[str, Any]]) -> dict[str, int]:
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
        if state in {"exact_id", "auto_accepted", "reviewed"}:
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
            raise AssertionError(f"unknown alignment state: {state}")
    return counts


def _mapping_kind(source_count: int, target_count: int) -> str:
    if source_count == 1 and target_count == 1:
        return "1:1"
    if source_count == 1:
        return "1:N"
    if target_count == 1:
        return "N:1"
    return "N:M"


def _alignment_block_type(value: str) -> str:
    if value == "paragraph":
        return "prose"
    return value


def _require_nonnegative_int(value: Any, path: str) -> int:
    if type(value) is not int or value < 0:
        raise ContractValidationError(
            "type", path, "value must be a non-negative integer"
        )
    return value


def _contained_path(root: Path, relative_value: str, *, label: str) -> Path:
    relative = Path(relative_value)
    if relative.is_absolute() or ".." in relative.parts:
        raise D2LCommunityFiveChapterError(label, "path must be relative and contained")
    resolved = (root / relative).resolve()
    if root.resolve() not in resolved.parents:
        raise D2LCommunityFiveChapterError(label, "path escapes project root")
    if not resolved.is_dir():
        raise D2LCommunityFiveChapterError(label, f"directory is missing: {resolved}")
    return resolved


def _validate_repository_snapshot(root: Path, expected_commit: str) -> None:
    if not root.is_dir():
        raise D2LCommunityFiveChapterError(
            "community_repository", f"repository is missing: {root}"
        )
    try:
        top_level = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "--show-toplevel"],
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
        ).stdout.strip()
        head = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
        ).stdout.strip()
        status = subprocess.run(
            [
                "git",
                "-C",
                str(root),
                "status",
                "--porcelain",
                "--untracked-files=all",
            ],
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
        ).stdout
    except (OSError, subprocess.CalledProcessError) as exc:
        raise D2LCommunityFiveChapterError(
            "community_repository", "cannot verify Community Git repository"
        ) from exc
    if Path(top_level).resolve() != root:
        raise D2LCommunityFiveChapterError(
            "community_repository",
            "community_repository_root must be the exact Git root",
        )
    if head != expected_commit:
        raise D2LCommunityFiveChapterError(
            "community_commit",
            f"repository HEAD {head} differs from sealed commit {expected_commit}",
        )
    if status:
        raise D2LCommunityFiveChapterError(
            "community_worktree", "Community repository must be clean"
        )


def _load_json(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise D2LCommunityFiveChapterError(
            "json_read", f"cannot read {label}: {path}"
        ) from exc
    if not isinstance(value, dict):
        raise D2LCommunityFiveChapterError(
            "json_type", f"{label} must be a JSON object"
        )
    return value


def _write_json(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = (
        json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    ).encode("utf-8")
    path.write_bytes(data)


def _file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _tree_digest(root: Path) -> str:
    rows = []
    for path in sorted(root.rglob("*"), key=lambda item: item.as_posix()):
        if path.is_file():
            rows.append(
                {
                    "path": path.relative_to(root).as_posix(),
                    "sha256": _file_sha256(path),
                }
            )
    return hashlib.sha256(
        json.dumps(rows, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
