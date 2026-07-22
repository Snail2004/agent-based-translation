from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any, Mapping

from pipeline.eval.alignment_manifest_v1 import (
    AlignmentTargetSnapshotV1,
    validate_alignment_bindings,
)
from pipeline.eval.common_input_v1 import CommonEvaluationInputV1
from pipeline.eval.contracts_v1 import CanonicalPolicy, canonical_sha256


__all__ = [
    "CommonArmUnitCoverageV1",
    "CommonEvaluationUnitCoverageV1",
    "CommonEvaluationUnitSetV1",
    "CommonEvaluationUnitV1",
    "CommonUnitArmViewV1",
    "build_common_evaluation_units",
]


_ACCEPTED_ALIGNMENT_STATES = frozenset({"exact_id", "auto_accepted", "reviewed"})

_UNIT_SET_POLICY = CanonicalPolicy(
    set_like_paths=frozenset(),
    semantic_sequence_paths=frozenset(
        {
            ("units",),
            ("units", "*", "source_block_ids"),
            ("units", "*", "source_text_sha256s"),
            ("units", "*", "arm_views"),
            ("units", "*", "arm_views", "*", "segment_ids"),
            ("units", "*", "arm_views", "*", "segment_statuses"),
            ("units", "*", "arm_views", "*", "text_sha256s"),
            ("units", "*", "arm_views", "*", "error_codes"),
            ("added_target_segment_ids",),
            ("coverage", "arm_coverage"),
        }
    ),
)


@dataclass(frozen=True, slots=True)
class CommonUnitArmViewV1:
    arm_id: str
    artifact_id: str
    artifact_sha256: str
    status: str
    segment_ids: tuple[str, ...]
    segment_statuses: tuple[str, ...]
    text_parts: tuple[str | None, ...]
    error_codes: tuple[str | None, ...]


@dataclass(frozen=True, slots=True)
class CommonEvaluationUnitV1:
    unit_id: str
    chapter_id: str
    mapping_kind: str
    alignment_state: str
    source_block_ids: tuple[str, ...]
    source_text_parts: tuple[str, ...]
    arm_views: tuple[CommonUnitArmViewV1, ...]


@dataclass(frozen=True, slots=True)
class CommonArmUnitCoverageV1:
    arm_id: str
    ready_unit_count: int
    missing_unit_count: int
    failed_unit_count: int
    review_held_unit_count: int
    not_applicable_unit_count: int


@dataclass(frozen=True, slots=True)
class CommonEvaluationUnitCoverageV1:
    source_unit_count: int
    all_arm_ready_unit_count: int
    added_target_segment_count: int
    arm_coverage: tuple[CommonArmUnitCoverageV1, ...]


@dataclass(frozen=True, slots=True)
class CommonEvaluationUnitSetV1:
    project_id: str
    document_id: str
    alignment_manifest_id: str
    alignment_manifest_sha256: str
    aligned_target_arm_id: str
    unit_set_sha256: str
    units: tuple[CommonEvaluationUnitV1, ...]
    added_target_segment_ids: tuple[str, ...]
    coverage: CommonEvaluationUnitCoverageV1


def build_common_evaluation_units(
    common: CommonEvaluationInputV1,
    target: AlignmentTargetSnapshotV1,
    alignment_manifest: Mapping[str, Any],
) -> CommonEvaluationUnitSetV1:
    manifest = validate_alignment_bindings(alignment_manifest, common, target)
    block_by_id = {block.block_id: block for block in common.blocks}
    target_by_id = {segment.segment_id: segment for segment in target.segments}
    translation_by_arm_block = {
        (row.arm_id, row.block_id): row for row in common.translations
    }
    arm_by_id = {arm.arm_id: arm for arm in common.arms}

    units: list[CommonEvaluationUnitV1] = []
    added_target_segment_ids: list[str] = []
    for mapping in manifest["mappings"]:
        if mapping["decision_state"] == "added":
            added_target_segment_ids.extend(mapping["target_segment_ids"])
            continue

        source_block_ids = tuple(mapping["source_block_ids"])
        source_text_parts = tuple(
            block_by_id[block_id].source_text for block_id in source_block_ids
        )
        arm_views = [
            _machine_arm_view(
                arm_id=arm_id,
                source_block_ids=source_block_ids,
                arm=arm_by_id[arm_id],
                translation_by_arm_block=translation_by_arm_block,
            )
            for arm_id in sorted(arm_by_id)
        ]
        arm_views.append(_aligned_target_view(mapping, target, target_by_id))
        units.append(
            CommonEvaluationUnitV1(
                unit_id=mapping["mapping_id"],
                chapter_id=mapping["chapter_id"],
                mapping_kind=mapping["mapping_kind"],
                alignment_state=mapping["decision_state"],
                source_block_ids=source_block_ids,
                source_text_parts=source_text_parts,
                arm_views=tuple(sorted(arm_views, key=lambda row: row.arm_id)),
            )
        )

    coverage = _build_coverage(units, len(added_target_segment_ids))
    provisional = CommonEvaluationUnitSetV1(
        project_id=common.project_id,
        document_id=common.document_id,
        alignment_manifest_id=manifest["manifest_id"],
        alignment_manifest_sha256=manifest["integrity"]["manifest_sha256"],
        aligned_target_arm_id=target.arm_id,
        unit_set_sha256="0" * 64,
        units=tuple(units),
        added_target_segment_ids=tuple(added_target_segment_ids),
        coverage=coverage,
    )
    return CommonEvaluationUnitSetV1(
        project_id=provisional.project_id,
        document_id=provisional.document_id,
        alignment_manifest_id=provisional.alignment_manifest_id,
        alignment_manifest_sha256=provisional.alignment_manifest_sha256,
        aligned_target_arm_id=provisional.aligned_target_arm_id,
        unit_set_sha256=_unit_set_sha256(provisional),
        units=provisional.units,
        added_target_segment_ids=provisional.added_target_segment_ids,
        coverage=provisional.coverage,
    )


def _machine_arm_view(
    *,
    arm_id: str,
    source_block_ids: tuple[str, ...],
    arm: Any,
    translation_by_arm_block: Mapping[tuple[str, str], Any],
) -> CommonUnitArmViewV1:
    rows = [
        translation_by_arm_block[(arm_id, block_id)] for block_id in source_block_ids
    ]
    statuses = tuple(row.status for row in rows)
    if all(status == "translated" for status in statuses):
        status = "ready"
    elif "failed" in statuses:
        status = "failed"
    elif "missing" in statuses:
        status = "missing"
    else:
        status = "not_applicable"
    return CommonUnitArmViewV1(
        arm_id=arm_id,
        artifact_id=arm.artifact_id,
        artifact_sha256=arm.artifact_sha256,
        status=status,
        segment_ids=source_block_ids,
        segment_statuses=statuses,
        text_parts=tuple(row.target_text for row in rows),
        error_codes=tuple(row.error_code for row in rows),
    )


def _aligned_target_view(
    mapping: Mapping[str, Any],
    target: AlignmentTargetSnapshotV1,
    target_by_id: Mapping[str, Any],
) -> CommonUnitArmViewV1:
    decision_state = mapping["decision_state"]
    segment_ids = tuple(mapping["target_segment_ids"])
    if decision_state in _ACCEPTED_ALIGNMENT_STATES:
        status = "ready"
        segment_statuses = tuple("translated" for _ in segment_ids)
    elif decision_state == "missing":
        status = "missing"
        segment_statuses = ()
    else:
        status = "review_held"
        segment_statuses = tuple("review_held" for _ in segment_ids)
    return CommonUnitArmViewV1(
        arm_id=target.arm_id,
        artifact_id=target.artifact_id,
        artifact_sha256=target.artifact_sha256,
        status=status,
        segment_ids=segment_ids,
        segment_statuses=segment_statuses,
        text_parts=tuple(target_by_id[segment_id].text for segment_id in segment_ids),
        error_codes=tuple(None for _ in segment_ids),
    )


def _build_coverage(
    units: list[CommonEvaluationUnitV1],
    added_target_segment_count: int,
) -> CommonEvaluationUnitCoverageV1:
    all_arm_ready = 0
    status_counts: dict[str, dict[str, int]] = {}
    for unit in units:
        for view in unit.arm_views:
            arm_counts = status_counts.setdefault(
                view.arm_id,
                {
                    "ready": 0,
                    "missing": 0,
                    "failed": 0,
                    "review_held": 0,
                    "not_applicable": 0,
                },
            )
            arm_counts[view.status] += 1
        if all(view.status == "ready" for view in unit.arm_views):
            all_arm_ready += 1
    arm_coverage = tuple(
        CommonArmUnitCoverageV1(
            arm_id=arm_id,
            ready_unit_count=counts["ready"],
            missing_unit_count=counts["missing"],
            failed_unit_count=counts["failed"],
            review_held_unit_count=counts["review_held"],
            not_applicable_unit_count=counts["not_applicable"],
        )
        for arm_id, counts in sorted(status_counts.items())
    )
    return CommonEvaluationUnitCoverageV1(
        source_unit_count=len(units),
        all_arm_ready_unit_count=all_arm_ready,
        added_target_segment_count=added_target_segment_count,
        arm_coverage=arm_coverage,
    )


def _exact_text_sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _unit_set_sha256(unit_set: CommonEvaluationUnitSetV1) -> str:
    payload = {
        "project_id": unit_set.project_id,
        "document_id": unit_set.document_id,
        "alignment_manifest_id": unit_set.alignment_manifest_id,
        "alignment_manifest_sha256": unit_set.alignment_manifest_sha256,
        "aligned_target_arm_id": unit_set.aligned_target_arm_id,
        "units": [
            {
                "unit_id": unit.unit_id,
                "chapter_id": unit.chapter_id,
                "mapping_kind": unit.mapping_kind,
                "alignment_state": unit.alignment_state,
                "source_block_ids": list(unit.source_block_ids),
                "source_text_sha256s": [
                    _exact_text_sha256(text) for text in unit.source_text_parts
                ],
                "arm_views": [
                    {
                        "arm_id": view.arm_id,
                        "artifact_id": view.artifact_id,
                        "artifact_sha256": view.artifact_sha256,
                        "status": view.status,
                        "segment_ids": list(view.segment_ids),
                        "segment_statuses": list(view.segment_statuses),
                        "text_sha256s": [
                            _exact_text_sha256(text) if text is not None else None
                            for text in view.text_parts
                        ],
                        "error_codes": list(view.error_codes),
                    }
                    for view in unit.arm_views
                ],
            }
            for unit in unit_set.units
        ],
        "added_target_segment_ids": list(unit_set.added_target_segment_ids),
        "coverage": {
            "source_unit_count": unit_set.coverage.source_unit_count,
            "all_arm_ready_unit_count": unit_set.coverage.all_arm_ready_unit_count,
            "added_target_segment_count": unit_set.coverage.added_target_segment_count,
            "arm_coverage": [
                {
                    "arm_id": row.arm_id,
                    "ready_unit_count": row.ready_unit_count,
                    "missing_unit_count": row.missing_unit_count,
                    "failed_unit_count": row.failed_unit_count,
                    "review_held_unit_count": row.review_held_unit_count,
                    "not_applicable_unit_count": row.not_applicable_unit_count,
                }
                for row in unit_set.coverage.arm_coverage
            ],
        },
    }
    return canonical_sha256(payload, policy=_UNIT_SET_POLICY)
