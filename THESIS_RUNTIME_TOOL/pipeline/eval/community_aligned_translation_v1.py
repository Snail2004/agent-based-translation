from __future__ import annotations

import copy
import hashlib
import json
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Mapping, Sequence

from pipeline.eval.alignment_manifest_v1 import (
    AlignmentTargetSegmentV1,
    AlignmentTargetSnapshotV1,
    validate_alignment_bindings,
    validate_alignment_target_snapshot,
)
from pipeline.eval.common_input_v1 import (
    CanonicalProjectionIdentityV1,
    CanonicalSourcePackageBindingV1,
    CommonArmV1,
    CommonBlockV1,
    CommonEvaluationInputV1,
    CommonSourceSnapshotV1,
    CommonTranslationV1,
    build_common_evaluation_input,
    source_binding_to_dict,
    validate_source_binding,
    validate_translation_artifact,
)
from pipeline.eval.contracts_v1 import (
    CanonicalPolicy,
    ContractValidationError,
    canonicalize,
    require_commit,
    require_enum,
    require_exact_keys,
    require_int,
    require_list,
    require_mapping,
    require_relative_path,
    require_rfc3339,
    require_sha256,
    require_string,
    require_unique,
    seal_payload,
    verify_payload_hash,
)
from pipeline.ingest.canonical_source_package import canonical_json_sha256


__all__ = [
    "COMMUNITY_ALIGNED_TRANSLATION_SCHEMA_ID",
    "build_community_aligned_translation_v1",
    "build_common_aligned_evaluation_input_v1",
    "validate_community_aligned_translation_v1",
]


COMMUNITY_ALIGNED_TRANSLATION_SCHEMA_ID = "CommunityAlignedTranslationV1"
_SCHEMA_VERSION = "1.0.0"
_HASH_PATH = ("integrity", "artifact_sha256")
_POLICY = CanonicalPolicy(
    set_like_paths=frozenset(),
    semantic_sequence_paths=frozenset(
        {
            ("chapters",),
            ("chapters", "*", "units"),
            ("chapters", "*", "units", "*", "source_block_ids"),
            ("chapters", "*", "units", "*", "target_segments"),
        }
    ),
)
_MAPPING_KINDS = frozenset({"1:1", "1:N", "N:1", "N:M"})
_ACCEPTED_STATES = frozenset({"exact_id", "auto_accepted", "reviewed"})
_UNIT_SEPARATOR = "\n\n"
_MACHINE_ARM_ORDER = ("s0", "s1", "google_nmt", "llm_lc")
_FIVE_ARM_ORDER = ("s0", "s1", "community", "google_nmt", "llm_lc")


def build_community_aligned_translation_v1(
    source: CommonSourceSnapshotV1,
    *,
    alignment_bundle_root: Path,
    source_finalization_path: Path,
    created_at: str,
    producer_code_commit: str,
) -> dict[str, Any]:
    """Consolidate the accepted Community alignment without block-local rewriting."""

    root = Path(alignment_bundle_root).resolve()
    bundle_path = root / "alignment_bundle.json"
    bundle = _read_json(bundle_path, label="Community alignment bundle")
    require_exact_keys(
        bundle,
        required={
            "schema_id",
            "schema_version",
            "project_id",
            "document_id",
            "created_at",
            "status",
            "community_arm_id",
            "chapter_order",
            "chapters",
            "source_identity",
            "target_identity",
            "manual_decision_sha256",
            "audit_plan_sha256",
            "audit_record_sha256",
            "artifact_index",
            "integrity",
        },
        path="$bundle",
    )
    if (
        bundle["schema_id"] != "D2LCommunityFiveChapterAlignmentBundleV1"
        or bundle["schema_version"] != "1.0.0"
        or bundle["status"] != "accepted_alignment"
    ):
        raise ContractValidationError(
            "community_alignment_status",
            "$bundle",
            "Community alignment bundle is not accepted V1 evidence",
        )
    logical_bundle_sha256 = require_sha256(
        require_mapping(bundle["integrity"], path="$bundle.integrity")[
            "bundle_sha256"
        ],
        path="$bundle.integrity.bundle_sha256",
    )
    if root.name.lower() != logical_bundle_sha256:
        raise ContractValidationError(
            "community_alignment_identity",
            "$bundle.integrity.bundle_sha256",
            "accepted bundle directory identity drifted",
        )
    artifact_paths = _validate_artifact_index(root, bundle["artifact_index"])
    if physical_sha256(bundle_path) != physical_sha256(
        artifact_paths["alignment_bundle.json"]
    ):
        raise AssertionError("bundle path must resolve to the indexed bundle")
    historical_projection_sha256 = _validate_source_identity_bridge(
        source,
        bundle=bundle,
        finalization=_read_json(
            Path(source_finalization_path), label="source finalization"
        ),
    )

    source_binding = source_binding_to_dict(source.source_binding)
    if bundle["project_id"] != source.project_id or bundle["document_id"] != source.document_id:
        raise ContractValidationError(
            "community_alignment_source",
            "$bundle",
            "Community alignment belongs to another source package",
        )
    chapter_order = [
        require_string(value, path=f"$bundle.chapter_order[{index}]")
        for index, value in enumerate(
            require_list(bundle["chapter_order"], path="$bundle.chapter_order")
        )
    ]
    require_unique(chapter_order, path="$bundle.chapter_order")
    source_chapter_order = list(dict.fromkeys(block.chapter_id for block in source.blocks))
    if chapter_order != source_chapter_order:
        raise ContractValidationError(
            "community_alignment_chapters",
            "$bundle.chapter_order",
            "Community alignment chapter order differs from selected source order",
        )

    chapter_rows: list[dict[str, Any]] = []
    all_source_ids: list[str] = []
    all_target_ids: list[str] = []
    for chapter_id in chapter_order:
        manifest_ref = f"chapters/{chapter_id}/alignment_manifest.json"
        target_ref = f"chapters/{chapter_id}/target_snapshot.json"
        manifest = _read_json(
            artifact_paths[manifest_ref], label=f"{chapter_id} alignment manifest"
        )
        target_wrapper = _read_json(
            artifact_paths[target_ref], label=f"{chapter_id} target snapshot"
        )
        target = _target_snapshot(
            require_mapping(target_wrapper.get("snapshot"), path=f"${chapter_id}.snapshot")
        )
        chapter_common = _chapter_alignment_source(
            source,
            chapter_id,
            historical_projection_sha256=historical_projection_sha256,
        )
        validated_manifest = validate_alignment_bindings(
            manifest, chapter_common, target
        )
        if any(
            row["decision_state"] not in _ACCEPTED_STATES
            for row in validated_manifest["mappings"]
        ):
            raise ContractValidationError(
                "community_alignment_review",
                f"${chapter_id}.mappings",
                "Community alignment contains a non-accepted mapping",
            )
        target_by_id = {row.segment_id: row for row in target.segments}
        units: list[dict[str, Any]] = []
        for mapping in validated_manifest["mappings"]:
            source_ids = list(mapping["source_block_ids"])
            target_ids = list(mapping["target_segment_ids"])
            units.append(
                {
                    "unit_id": mapping["mapping_id"],
                    "mapping_kind": mapping["mapping_kind"],
                    "source_block_ids": source_ids,
                    "target_segments": [
                        {
                            "segment_id": target_id,
                            "text": target_by_id[target_id].text,
                            "text_sha256": target_by_id[target_id].text_sha256,
                        }
                        for target_id in target_ids
                    ],
                }
            )
            all_source_ids.extend(source_ids)
            all_target_ids.extend(target_ids)
        chapter_rows.append(
            {
                "chapter_id": chapter_id,
                "alignment_manifest_id": validated_manifest["manifest_id"],
                "alignment_manifest_sha256": validated_manifest["integrity"][
                    "manifest_sha256"
                ],
                "target_artifact_id": target.artifact_id,
                "target_artifact_sha256": target.artifact_sha256,
                "units": units,
            }
        )

    eligible_ids = [
        block.block_id
        for block in source.blocks
        if block.admission in {"translate", "translate_structured"}
    ]
    if all_source_ids != eligible_ids:
        raise ContractValidationError(
            "community_alignment_exact_cover",
            "$.chapters.units.source_block_ids",
            "Community units do not exact-cover canonical translatable blocks",
        )
    require_unique(all_target_ids, path="$.chapters.units.target_segments.segment_id")
    profile_sha256 = hashlib.sha256(
        (logical_bundle_sha256 + bundle["target_identity"]["repository_commit"]).encode(
            "ascii"
        )
    ).hexdigest()
    draft = {
        "schema_id": COMMUNITY_ALIGNED_TRANSLATION_SCHEMA_ID,
        "schema_version": _SCHEMA_VERSION,
        "artifact_id": f"community-aligned-{logical_bundle_sha256[:24]}",
        "created_at": require_rfc3339(created_at, path="$.created_at"),
        "producer": {
            "workstream": "d2l",
            "component": "d2l_community_alignment_bridge_v1",
            "component_version": _SCHEMA_VERSION,
            "code_commit": require_commit(
                producer_code_commit, path="$.producer_code_commit"
            ),
        },
        "source_binding": source_binding,
        "run_identity": {
            "logical_run_id": f"community-alignment-{logical_bundle_sha256[:16]}",
            "attempt_run_id": f"community-alignment-{logical_bundle_sha256[:16]}-attempt-1",
            "arm_id": "community",
            "profile_id": "d2l.community.accepted_alignment_v1",
            "profile_config_sha256": profile_sha256,
            "source_language": "en",
            "target_language": "vi",
        },
        "accepted_bundle": {
            "artifact_ref": "accepted/community/alignment_bundle.json",
            "physical_sha256": physical_sha256(bundle_path),
            "logical_bundle_sha256": logical_bundle_sha256,
            "repository_commit": require_commit(
                bundle["target_identity"]["repository_commit"],
                path="$bundle.target_identity.repository_commit",
            ),
            "source_finalization_sha256": bundle["source_identity"][
                "finalization_payload_sha256"
            ],
            "source_candidate_tree_sha256": bundle["source_identity"][
                "candidate_tree_sha256"
            ],
            "historical_projection_file_sha256": historical_projection_sha256,
        },
        "chapters": chapter_rows,
        "coverage": {
            "source_block_count": len(all_source_ids),
            "target_segment_count": len(all_target_ids),
            "unit_count": sum(len(row["units"]) for row in chapter_rows),
        },
        "integrity": {"artifact_sha256": "0" * 64},
    }
    return validate_community_aligned_translation_v1(
        seal_payload(draft, policy=_POLICY, hash_path=_HASH_PATH)
    )


def validate_community_aligned_translation_v1(
    value: Mapping[str, Any],
) -> dict[str, Any]:
    root = require_mapping(value, path="$")
    require_exact_keys(
        root,
        required={
            "schema_id",
            "schema_version",
            "artifact_id",
            "created_at",
            "producer",
            "source_binding",
            "run_identity",
            "accepted_bundle",
            "chapters",
            "coverage",
            "integrity",
        },
        path="$",
    )
    producer = require_mapping(root["producer"], path="$.producer")
    require_exact_keys(
        producer,
        required={"workstream", "component", "component_version", "code_commit"},
        path="$.producer",
    )
    if (
        producer["workstream"] != "d2l"
        or producer["component"] != "d2l_community_alignment_bridge_v1"
        or producer["component_version"] != _SCHEMA_VERSION
    ):
        raise ContractValidationError(
            "community_alignment_producer",
            "$.producer",
            "foreign Community aligned artifact producer",
        )
    run = _run_identity(root["run_identity"])
    accepted = require_mapping(root["accepted_bundle"], path="$.accepted_bundle")
    require_exact_keys(
        accepted,
        required={
            "artifact_ref",
            "physical_sha256",
            "logical_bundle_sha256",
            "repository_commit",
            "source_finalization_sha256",
            "source_candidate_tree_sha256",
            "historical_projection_file_sha256",
        },
        path="$.accepted_bundle",
    )
    chapters: list[dict[str, Any]] = []
    source_ids: list[str] = []
    target_ids: list[str] = []
    unit_ids: list[str] = []
    for chapter_index, raw_chapter in enumerate(
        require_list(root["chapters"], path="$.chapters")
    ):
        path = f"$.chapters[{chapter_index}]"
        chapter = require_mapping(raw_chapter, path=path)
        require_exact_keys(
            chapter,
            required={
                "chapter_id",
                "alignment_manifest_id",
                "alignment_manifest_sha256",
                "target_artifact_id",
                "target_artifact_sha256",
                "units",
            },
            path=path,
        )
        units: list[dict[str, Any]] = []
        for unit_index, raw_unit in enumerate(
            require_list(chapter["units"], path=f"{path}.units")
        ):
            unit_path = f"{path}.units[{unit_index}]"
            unit = require_mapping(raw_unit, path=unit_path)
            require_exact_keys(
                unit,
                required={
                    "unit_id",
                    "mapping_kind",
                    "source_block_ids",
                    "target_segments",
                },
                path=unit_path,
            )
            unit_source_ids = [
                require_string(row, path=f"{unit_path}.source_block_ids[{index}]")
                for index, row in enumerate(
                    require_list(
                        unit["source_block_ids"],
                        path=f"{unit_path}.source_block_ids",
                    )
                )
            ]
            segments = []
            for segment_index, raw_segment in enumerate(
                require_list(
                    unit["target_segments"], path=f"{unit_path}.target_segments"
                )
            ):
                segment_path = f"{unit_path}.target_segments[{segment_index}]"
                segment = require_mapping(raw_segment, path=segment_path)
                require_exact_keys(
                    segment,
                    required={"segment_id", "text", "text_sha256"},
                    path=segment_path,
                )
                text = require_string(segment["text"], path=f"{segment_path}.text")
                text_sha256 = require_sha256(
                    segment["text_sha256"], path=f"{segment_path}.text_sha256"
                )
                if hashlib.sha256(text.encode("utf-8")).hexdigest() != text_sha256:
                    raise ContractValidationError(
                        "community_target_hash",
                        f"{segment_path}.text_sha256",
                        "Community target text hash drifted",
                    )
                segments.append(
                    {
                        "segment_id": require_string(
                            segment["segment_id"],
                            path=f"{segment_path}.segment_id",
                        ),
                        "text": text,
                        "text_sha256": text_sha256,
                    }
                )
            mapping_kind = require_enum(
                unit["mapping_kind"], _MAPPING_KINDS, path=f"{unit_path}.mapping_kind"
            )
            expected_kind = _mapping_kind(len(unit_source_ids), len(segments))
            if mapping_kind != expected_kind:
                raise ContractValidationError(
                    "community_mapping_kind",
                    f"{unit_path}.mapping_kind",
                    "mapping kind disagrees with source/target cardinality",
                )
            unit_id = require_string(unit["unit_id"], path=f"{unit_path}.unit_id")
            unit_ids.append(unit_id)
            source_ids.extend(unit_source_ids)
            target_ids.extend(row["segment_id"] for row in segments)
            units.append(
                {
                    "unit_id": unit_id,
                    "mapping_kind": mapping_kind,
                    "source_block_ids": unit_source_ids,
                    "target_segments": segments,
                }
            )
        chapters.append(
            {
                "chapter_id": require_string(
                    chapter["chapter_id"], path=f"{path}.chapter_id"
                ),
                "alignment_manifest_id": require_string(
                    chapter["alignment_manifest_id"],
                    path=f"{path}.alignment_manifest_id",
                ),
                "alignment_manifest_sha256": require_sha256(
                    chapter["alignment_manifest_sha256"],
                    path=f"{path}.alignment_manifest_sha256",
                ),
                "target_artifact_id": require_string(
                    chapter["target_artifact_id"],
                    path=f"{path}.target_artifact_id",
                ),
                "target_artifact_sha256": require_sha256(
                    chapter["target_artifact_sha256"],
                    path=f"{path}.target_artifact_sha256",
                ),
                "units": units,
            }
        )
    require_unique([row["chapter_id"] for row in chapters], path="$.chapters")
    require_unique(unit_ids, path="$.chapters.units.unit_id")
    require_unique(source_ids, path="$.chapters.units.source_block_ids")
    require_unique(target_ids, path="$.chapters.units.target_segments.segment_id")
    coverage = require_mapping(root["coverage"], path="$.coverage")
    require_exact_keys(
        coverage,
        required={"source_block_count", "target_segment_count", "unit_count"},
        path="$.coverage",
    )
    normalized_coverage = {
        "source_block_count": require_int(
            coverage["source_block_count"],
            path="$.coverage.source_block_count",
            minimum=1,
        ),
        "target_segment_count": require_int(
            coverage["target_segment_count"],
            path="$.coverage.target_segment_count",
            minimum=1,
        ),
        "unit_count": require_int(
            coverage["unit_count"], path="$.coverage.unit_count", minimum=1
        ),
    }
    if normalized_coverage != {
        "source_block_count": len(source_ids),
        "target_segment_count": len(target_ids),
        "unit_count": len(unit_ids),
    }:
        raise ContractValidationError(
            "community_alignment_coverage",
            "$.coverage",
            "Community aligned artifact coverage does not reconcile",
        )
    integrity = require_mapping(root["integrity"], path="$.integrity")
    require_exact_keys(
        integrity, required={"artifact_sha256"}, path="$.integrity"
    )
    normalized = {
        "schema_id": require_enum(
            root["schema_id"],
            {COMMUNITY_ALIGNED_TRANSLATION_SCHEMA_ID},
            path="$.schema_id",
        ),
        "schema_version": require_enum(
            root["schema_version"], {_SCHEMA_VERSION}, path="$.schema_version"
        ),
        "artifact_id": require_string(root["artifact_id"], path="$.artifact_id"),
        "created_at": require_rfc3339(root["created_at"], path="$.created_at"),
        "producer": {
            "workstream": "d2l",
            "component": "d2l_community_alignment_bridge_v1",
            "component_version": _SCHEMA_VERSION,
            "code_commit": require_commit(
                producer["code_commit"], path="$.producer.code_commit"
            ),
        },
        "source_binding": validate_source_binding(root["source_binding"]),
        "run_identity": run,
        "accepted_bundle": {
            "artifact_ref": require_relative_path(
                accepted["artifact_ref"], path="$.accepted_bundle.artifact_ref"
            ),
            "physical_sha256": require_sha256(
                accepted["physical_sha256"],
                path="$.accepted_bundle.physical_sha256",
            ),
            "logical_bundle_sha256": require_sha256(
                accepted["logical_bundle_sha256"],
                path="$.accepted_bundle.logical_bundle_sha256",
            ),
            "repository_commit": require_commit(
                accepted["repository_commit"],
                path="$.accepted_bundle.repository_commit",
            ),
            "source_finalization_sha256": require_sha256(
                accepted["source_finalization_sha256"],
                path="$.accepted_bundle.source_finalization_sha256",
            ),
            "source_candidate_tree_sha256": require_sha256(
                accepted["source_candidate_tree_sha256"],
                path="$.accepted_bundle.source_candidate_tree_sha256",
            ),
            "historical_projection_file_sha256": require_sha256(
                accepted["historical_projection_file_sha256"],
                path="$.accepted_bundle.historical_projection_file_sha256",
            ),
        },
        "chapters": chapters,
        "coverage": normalized_coverage,
        "integrity": {
            "artifact_sha256": require_sha256(
                integrity["artifact_sha256"],
                path="$.integrity.artifact_sha256",
            )
        },
    }
    if not verify_payload_hash(normalized, policy=_POLICY, hash_path=_HASH_PATH):
        raise ContractValidationError(
            "community_alignment_hash",
            "$.integrity.artifact_sha256",
            "Community aligned artifact self-hash drifted",
        )
    canonical = canonicalize(normalized, policy=_POLICY)
    if not isinstance(canonical, dict):
        raise AssertionError("Community aligned artifact must remain an object")
    return canonical


def build_common_aligned_evaluation_input_v1(
    source: CommonSourceSnapshotV1,
    *,
    machine_translation_artifacts: Mapping[str, Mapping[str, Any]],
    community_aligned_artifact: Mapping[str, Any],
) -> CommonEvaluationInputV1:
    """Project five arms onto reviewed common units and omit held source rows."""

    if tuple(machine_translation_artifacts) != _MACHINE_ARM_ORDER:
        raise ContractValidationError(
            "arm_order",
            "$.machine_translation_artifacts",
            "expected exact ordered s0, s1, google_nmt, llm_lc artifacts",
        )
    validated_machine = {
        arm_id: validate_translation_artifact(payload)
        for arm_id, payload in machine_translation_artifacts.items()
    }
    base = build_common_evaluation_input(
        source, [validated_machine[arm_id] for arm_id in _MACHINE_ARM_ORDER]
    )
    community = validate_community_aligned_translation_v1(
        community_aligned_artifact
    )
    if community["source_binding"] != source_binding_to_dict(source.source_binding):
        raise ContractValidationError(
            "source_binding",
            "$.community.source_binding",
            "Community alignment binds another canonical source package",
        )

    block_by_id = {row.block_id: row for row in source.blocks}
    machine_rows = {
        (row.arm_id, row.block_id): row for row in base.translations
    }
    community_unit_by_source: dict[str, dict[str, Any]] = {}
    community_units: dict[str, dict[str, Any]] = {}
    for chapter in community["chapters"]:
        for unit in chapter["units"]:
            unit_row = {**unit, "chapter_id": chapter["chapter_id"]}
            community_units[unit["unit_id"]] = unit_row
            for block_id in unit["source_block_ids"]:
                if block_id in community_unit_by_source:
                    raise ContractValidationError(
                        "community_alignment_exact_cover",
                        "$.community.chapters.units",
                        "Community source block appears in multiple units",
                    )
                community_unit_by_source[block_id] = unit_row
    eligible_ids = [
        row.block_id
        for row in source.blocks
        if row.admission in {"translate", "translate_structured"}
    ]
    if list(community_unit_by_source) != eligible_ids:
        raise ContractValidationError(
            "community_alignment_exact_cover",
            "$.community.chapters.units",
            "Community units do not preserve canonical source block order",
        )

    output_blocks: list[CommonBlockV1] = []
    translations: list[CommonTranslationV1] = []
    emitted_units: set[str] = set()
    order_by_chapter: dict[str, int] = {}
    for source_block in source.blocks:
        if source_block.admission == "review_required":
            continue
        if source_block.admission in {"translate", "translate_structured"}:
            unit = community_unit_by_source[source_block.block_id]
            if unit["unit_id"] in emitted_units:
                continue
            emitted_units.add(unit["unit_id"])
            source_blocks = [block_by_id[row] for row in unit["source_block_ids"]]
            block_id = unit["unit_id"]
            source_text = _UNIT_SEPARATOR.join(row.source_text for row in source_blocks)
            admission = (
                "translate_structured"
                if any(row.admission == "translate_structured" for row in source_blocks)
                else "translate"
            )
            target_by_arm = {
                arm_id: _joined_machine_target(
                    arm_id, source_blocks, machine_rows
                )
                for arm_id in _MACHINE_ARM_ORDER
            }
            community_target = _UNIT_SEPARATOR.join(
                row["text"] for row in unit["target_segments"]
            )
            if not community_target:
                raise ContractValidationError(
                    "community_target",
                    f"$.community.units[{block_id}]",
                    "accepted Community unit has no target text",
                )
            target_by_arm["community"] = community_target
            block_type = (
                source_blocks[0].block_type
                if len(source_blocks) == 1
                else "aligned_unit"
            )
        elif source_block.admission == "preserve":
            block_id = f"aligned-preserve::{source_block.block_id}"
            source_text = source_block.source_text
            admission = "preserve"
            block_type = source_block.block_type
            target_by_arm = {
                arm_id: source_text for arm_id in _FIVE_ARM_ORDER
            }
        elif source_block.admission == "exclude":
            block_id = f"aligned-exclude::{source_block.block_id}"
            source_text = source_block.source_text
            admission = "exclude"
            block_type = source_block.block_type
            target_by_arm = {arm_id: None for arm_id in _FIVE_ARM_ORDER}
        else:
            raise ContractValidationError(
                "source_admission",
                f"$.source.blocks[{source_block.block_id}]",
                "unsupported source admission in common-unit bridge",
            )
        order_index = order_by_chapter.get(source_block.chapter_id, 0)
        order_by_chapter[source_block.chapter_id] = order_index + 1
        output_blocks.append(
            CommonBlockV1(
                block_id=block_id,
                chapter_id=source_block.chapter_id,
                order_index=order_index,
                block_type=block_type,
                source_text=source_text,
                admission=admission,
            )
        )
        for arm_id in _FIVE_ARM_ORDER:
            translations.append(
                CommonTranslationV1(
                    arm_id=arm_id,
                    block_id=block_id,
                    status=(
                        "translated"
                        if admission in {"translate", "translate_structured"}
                        else "preserved"
                        if admission == "preserve"
                        else "excluded"
                    ),
                    target_text=target_by_arm[arm_id],
                    error_code=None,
                )
            )

    machine_arms = {row.arm_id: row for row in base.arms}
    run = community["run_identity"]
    arms = tuple(
        (
            CommonArmV1(
                artifact_id=community["artifact_id"],
                artifact_sha256=community["integrity"]["artifact_sha256"],
                logical_run_id=run["logical_run_id"],
                attempt_run_id=run["attempt_run_id"],
                arm_id="community",
                profile_id=run["profile_id"],
                profile_config_sha256=run["profile_config_sha256"],
                source_language=run["source_language"],
                target_language=run["target_language"],
            )
            if arm_id == "community"
            else machine_arms[arm_id]
        )
        for arm_id in _FIVE_ARM_ORDER
    )
    translation_by_key = {
        (row.arm_id, row.block_id): row for row in translations
    }
    return CommonEvaluationInputV1(
        source_schema_id=source.source_schema_id,
        source_schema_version=source.source_schema_version,
        source_binding=source.source_binding,
        blocks=tuple(output_blocks),
        arms=arms,
        translations=tuple(
            translation_by_key[(arm_id, block.block_id)]
            for arm_id in _FIVE_ARM_ORDER
            for block in output_blocks
        ),
    )


def physical_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _chapter_alignment_source(
    source: CommonSourceSnapshotV1,
    chapter_id: str,
    *,
    historical_projection_sha256: str,
) -> CommonEvaluationInputV1:
    blocks = tuple(
        row
        for row in source.blocks
        if row.chapter_id == chapter_id
    )
    if not blocks:
        raise ContractValidationError(
            "community_alignment_chapters",
            f"$.source[{chapter_id}]",
            "selected chapter has no translatable blocks",
        )
    binding = source.source_binding
    if not isinstance(binding, CanonicalSourcePackageBindingV1):
        raise ContractValidationError(
            "community_alignment_source",
            "$.source_binding",
            "Community alignment requires a canonical source package",
        )
    historical_binding = replace(
        binding,
        admitted_projection=CanonicalProjectionIdentityV1(
            schema_version=binding.admitted_projection.schema_version,
            payload_sha256=historical_projection_sha256,
        ),
    )
    alignment_blocks = tuple(
        replace(row, block_type="prose" if row.block_type == "paragraph" else row.block_type)
        for row in blocks
    )
    language_anchor = CommonArmV1(
        artifact_id=f"alignment_language_anchor__{chapter_id}",
        artifact_sha256=hashlib.sha256(
            f"{historical_projection_sha256}\0{chapter_id}\0en\0vi".encode("utf-8")
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
    return CommonEvaluationInputV1(
        source_schema_id=source.source_schema_id,
        source_schema_version=source.source_schema_version,
        source_binding=historical_binding,
        blocks=alignment_blocks,
        arms=(language_anchor,),
        translations=(),
    )


def _validate_source_identity_bridge(
    source: CommonSourceSnapshotV1,
    *,
    bundle: Mapping[str, Any],
    finalization: Mapping[str, Any],
) -> str:
    if not isinstance(source.source_binding, CanonicalSourcePackageBindingV1):
        raise ContractValidationError(
            "community_alignment_source",
            "$.source_binding",
            "Community alignment requires a canonical source package",
        )
    if (
        finalization.get("schema_version") != "source_package_finalization_v1"
        or not str(finalization.get("lifecycle") or "").startswith("finalized")
    ):
        raise ContractValidationError(
            "community_alignment_finalization",
            "$.source_finalization",
            "source finalization is not a finalized V1 package",
        )
    integrity = require_mapping(
        finalization.get("integrity"), path="$.source_finalization.integrity"
    )
    require_exact_keys(
        integrity,
        required={"payload_sha256"},
        path="$.source_finalization.integrity",
    )
    finalization_sha256 = require_sha256(
        integrity["payload_sha256"],
        path="$.source_finalization.integrity.payload_sha256",
    )
    unhashed = copy.deepcopy(dict(finalization))
    unhashed.pop("integrity")
    if canonical_json_sha256(unhashed) != finalization_sha256:
        raise ContractValidationError(
            "community_alignment_finalization",
            "$.source_finalization.integrity.payload_sha256",
            "source finalization self-hash drifted",
        )
    source_identity = require_mapping(
        bundle["source_identity"], path="$bundle.source_identity"
    )
    require_exact_keys(
        source_identity,
        required={"finalization_payload_sha256", "candidate_tree_sha256"},
        path="$bundle.source_identity",
    )
    candidate = require_mapping(
        finalization.get("candidate"), path="$.source_finalization.candidate"
    )
    if (
        finalization_sha256
        != require_sha256(
            source_identity["finalization_payload_sha256"],
            path="$bundle.source_identity.finalization_payload_sha256",
        )
        or require_sha256(
            candidate.get("tree_sha256"),
            path="$.source_finalization.candidate.tree_sha256",
        )
        != require_sha256(
            source_identity["candidate_tree_sha256"],
            path="$bundle.source_identity.candidate_tree_sha256",
        )
    ):
        raise ContractValidationError(
            "community_alignment_source",
            "$bundle.source_identity",
            "accepted alignment binds another finalized source revision",
        )
    if (
        finalization.get("doc_id") != source.document_id
        or bundle.get("project_id") != source.project_id
        or bundle.get("document_id") != source.document_id
    ):
        raise ContractValidationError(
            "community_alignment_source",
            "$bundle",
            "accepted alignment belongs to another project or document",
        )
    package = require_mapping(
        finalization.get("package"), path="$.source_finalization.package"
    )
    production = source_binding_to_dict(source.source_binding)
    for finalization_key, production_key in (
        ("document", "document"),
        ("structure", "structure"),
        ("asset_manifest", "asset_manifest"),
    ):
        component = require_mapping(
            package.get(finalization_key),
            path=f"$.source_finalization.package.{finalization_key}",
        )
        if {
            "schema_version": component.get("schema_version"),
            "sha256": str(component.get("sha256") or "").lower(),
        } != production[production_key]:
            raise ContractValidationError(
                "community_alignment_source",
                f"$.source_finalization.package.{finalization_key}",
                "accepted alignment source component differs from production",
            )
    projection = require_mapping(
        package.get("admitted_projection"),
        path="$.source_finalization.package.admitted_projection",
    )
    if (
        projection.get("schema_version")
        != production["admitted_projection"]["schema_version"]
    ):
        raise ContractValidationError(
            "community_alignment_source",
            "$.source_finalization.package.admitted_projection.schema_version",
            "accepted alignment projection schema differs from production",
        )
    policies = require_mapping(
        finalization.get("policies"), path="$.source_finalization.policies"
    )
    admission = require_mapping(
        policies.get("admission"),
        path="$.source_finalization.policies.admission",
    )
    if {
        "policy_id": admission.get("policy_id"),
        "policy_version": admission.get("policy_version"),
        "policy_sha256": str(admission.get("policy_sha256") or "").lower(),
    } != production["admission_policy"]:
        raise ContractValidationError(
            "community_alignment_source",
            "$.source_finalization.policies.admission",
            "accepted alignment admission policy differs from production",
        )
    return require_sha256(
        projection.get("sha256"),
        path="$.source_finalization.package.admitted_projection.sha256",
    )


def _target_snapshot(value: Mapping[str, Any]) -> AlignmentTargetSnapshotV1:
    segments = tuple(
        AlignmentTargetSegmentV1(
            segment_id=require_string(
                row["segment_id"], path=f"$.target.segments[{index}].segment_id"
            ),
            chapter_id=require_string(
                row["chapter_id"], path=f"$.target.segments[{index}].chapter_id"
            ),
            order_index=require_int(
                row["order_index"],
                path=f"$.target.segments[{index}].order_index",
                minimum=0,
            ),
            text=require_string(
                row["text"], path=f"$.target.segments[{index}].text"
            ),
            text_sha256=require_sha256(
                row["text_sha256"],
                path=f"$.target.segments[{index}].text_sha256",
            ),
        )
        for index, raw in enumerate(require_list(value["segments"], path="$.target.segments"))
        for row in (require_mapping(raw, path=f"$.target.segments[{index}]"),)
    )
    snapshot = AlignmentTargetSnapshotV1(
        artifact_id=require_string(value["artifact_id"], path="$.target.artifact_id"),
        artifact_sha256=require_sha256(
            value["artifact_sha256"], path="$.target.artifact_sha256"
        ),
        project_id=require_string(value["project_id"], path="$.target.project_id"),
        document_id=require_string(
            value["document_id"], path="$.target.document_id"
        ),
        arm_id=require_string(value["arm_id"], path="$.target.arm_id"),
        source_language=require_string(
            value["source_language"], path="$.target.source_language"
        ),
        target_language=require_string(
            value["target_language"], path="$.target.target_language"
        ),
        segments_sha256=require_sha256(
            value["segments_sha256"], path="$.target.segments_sha256"
        ),
        segments=segments,
    )
    validate_alignment_target_snapshot(snapshot)
    return snapshot


def _validate_artifact_index(
    root: Path, value: Any
) -> dict[str, Path]:
    result: dict[str, Path] = {
        "alignment_bundle.json": (root / "alignment_bundle.json").resolve()
    }
    for index, raw in enumerate(require_list(value, path="$bundle.artifact_index")):
        path = f"$bundle.artifact_index[{index}]"
        row = require_mapping(raw, path=path)
        require_exact_keys(
            row, required={"artifact_ref", "sha256", "sha256_kind"}, path=path
        )
        artifact_ref = require_relative_path(
            row["artifact_ref"], path=f"{path}.artifact_ref"
        )
        if row["sha256_kind"] != "physical":
            raise ContractValidationError(
                "hash_kind", f"{path}.sha256_kind", "bundle files require physical hashes"
            )
        candidate = (root / Path(*artifact_ref.split("/"))).resolve()
        try:
            candidate.relative_to(root)
        except ValueError as exc:
            raise ContractValidationError(
                "path_escape", f"{path}.artifact_ref", "bundle path escapes root"
            ) from exc
        if (
            not candidate.is_file()
            or candidate.is_symlink()
            or physical_sha256(candidate)
            != require_sha256(row["sha256"], path=f"{path}.sha256")
        ):
            raise ContractValidationError(
                "artifact_hash",
                f"{path}.sha256",
                "Community alignment bundle file drifted",
            )
        result[artifact_ref] = candidate
    return result


def _run_identity(value: Any) -> dict[str, str]:
    row = require_mapping(value, path="$.run_identity")
    require_exact_keys(
        row,
        required={
            "logical_run_id",
            "attempt_run_id",
            "arm_id",
            "profile_id",
            "profile_config_sha256",
            "source_language",
            "target_language",
        },
        path="$.run_identity",
    )
    return {
        "logical_run_id": require_string(
            row["logical_run_id"], path="$.run_identity.logical_run_id"
        ),
        "attempt_run_id": require_string(
            row["attempt_run_id"], path="$.run_identity.attempt_run_id"
        ),
        "arm_id": require_enum(
            row["arm_id"], {"community"}, path="$.run_identity.arm_id"
        ),
        "profile_id": require_string(
            row["profile_id"], path="$.run_identity.profile_id"
        ),
        "profile_config_sha256": require_sha256(
            row["profile_config_sha256"],
            path="$.run_identity.profile_config_sha256",
        ),
        "source_language": require_enum(
            row["source_language"], {"en"}, path="$.run_identity.source_language"
        ),
        "target_language": require_enum(
            row["target_language"], {"vi"}, path="$.run_identity.target_language"
        ),
    }


def _mapping_kind(source_count: int, target_count: int) -> str:
    if source_count == 1 and target_count == 1:
        return "1:1"
    if source_count == 1:
        return "1:N"
    if target_count == 1:
        return "N:1"
    return "N:M"


def _joined_machine_target(
    arm_id: str,
    source_blocks: Sequence[CommonBlockV1],
    machine_rows: Mapping[tuple[str, str], CommonTranslationV1],
) -> str:
    rows = [machine_rows[(arm_id, block.block_id)] for block in source_blocks]
    if any(row.status != "translated" or row.target_text is None for row in rows):
        raise ContractValidationError(
            "machine_alignment_status",
            f"$.machine_translation_artifacts.{arm_id}",
            "machine arm is not translated for an accepted common unit",
        )
    return _UNIT_SEPARATOR.join(str(row.target_text) for row in rows)


def _read_json(path: Path, *, label: str) -> dict[str, Any]:
    candidate = Path(path).resolve()
    if not candidate.is_file() or candidate.is_symlink():
        raise ContractValidationError(
            "missing_artifact", f"$.{label}", f"file is absent: {candidate}"
        )
    try:
        value = json.loads(candidate.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ContractValidationError(
            "invalid_json", f"$.{label}", f"cannot read JSON: {candidate}"
        ) from exc
    return dict(require_mapping(value, path=f"$.{label}"))
