from __future__ import annotations

import copy
import json
from collections import Counter
from dataclasses import replace
from pathlib import Path

import pytest

from pipeline.eval.alignment_manifest_v1 import (
    alignment_source_read_model_sha256,
    build_alignment_target_snapshot,
    make_alignment_target_segment,
    seal_alignment_manifest,
    validate_alignment_bindings,
    validate_alignment_manifest,
    validate_alignment_target_snapshot,
)
from pipeline.eval.common_input_v1 import (
    AdmissionPolicyIdentityV1,
    CanonicalComponentIdentityV1,
    CanonicalProjectionIdentityV1,
    CanonicalSourcePackageBindingV1,
    CommonBlockV1,
    CommonSourceSnapshotV1,
    build_common_evaluation_input,
    seal_translation_artifact,
    source_binding_to_dict,
)
from pipeline.eval.contracts_v1 import ContractValidationError


FIXTURES = Path(__file__).parent / "fixtures" / "evaluation_v1"


def _fixture() -> dict:
    return json.loads(
        (FIXTURES / "alignment_mixed_case.json").read_text(encoding="utf-8")
    )


def _source(fixture: dict, *, text_suffix: str = "") -> CommonSourceSnapshotV1:
    return CommonSourceSnapshotV1(
        source_schema_id="FixtureSourceV1",
        source_schema_version="1.0.0",
        source_binding=CanonicalSourcePackageBindingV1(
            project_id="project-alignment",
            document_id="document-alignment",
            document=CanonicalComponentIdentityV1("1.5.0", "1" * 64),
            structure=CanonicalComponentIdentityV1("1.0.0", "2" * 64),
            asset_manifest=CanonicalComponentIdentityV1("1.0.0", "3" * 64),
            admitted_projection=CanonicalProjectionIdentityV1(
                "admitted_projection_v1", "4" * 64
            ),
            admission_policy=AdmissionPolicyIdentityV1(
                "canonical_source_admission", "1.0.0", "5" * 64
            ),
        ),
        blocks=tuple(
            CommonBlockV1(
                block_id=row["block_id"],
                chapter_id=row["chapter_id"],
                order_index=row["order_index"],
                block_type="paragraph",
                source_text=row["text"] + text_suffix,
                admission="translate",
            )
            for row in fixture["source_blocks"]
        ),
    )


def _artifact(source: CommonSourceSnapshotV1, *, arm_id: str = "s1") -> dict:
    rows = [
        {
            "block_id": block.block_id,
            "status": "translated",
            "target_text": f"machine::{arm_id}::{block.block_id}",
            "error_code": None,
        }
        for block in source.blocks
    ]
    counts = Counter(row["status"] for row in rows)
    return seal_translation_artifact(
        {
            "schema_id": "TranslationArtifactV1",
            "schema_version": "1.0.0",
            "artifact_id": f"translation-{arm_id}",
            "created_at": "2026-07-18T00:00:00Z",
            "producer": {
                "workstream": "d2l",
                "component": "alignment_fixture_writer",
                "component_version": "1.0.0",
                "code_commit": "a" * 40,
            },
            "source_binding": source_binding_to_dict(source.source_binding),
            "run_identity": {
                "logical_run_id": f"logical-{arm_id}",
                "attempt_run_id": f"attempt-{arm_id}",
                "arm_id": arm_id,
                "profile_id": "technical_d2l_v1",
                "profile_config_sha256": "9" * 64,
                "source_language": "en",
                "target_language": "vi",
            },
            "translations": rows,
            "coverage": {
                "source_block_count": len(rows),
                "eligible_count": counts["translated"],
                "translated_count": counts["translated"],
                "preserved_count": 0,
                "excluded_count": 0,
                "review_held_count": 0,
                "missing_count": 0,
                "failed_count": 0,
            },
            "integrity": {"artifact_sha256": "0" * 64},
        }
    )


def _common(fixture: dict, *, text_suffix: str = ""):
    source = _source(fixture, text_suffix=text_suffix)
    return build_common_evaluation_input(source, [_artifact(source)])


def _target(fixture: dict):
    return build_alignment_target_snapshot(
        artifact_id="human-artifact",
        artifact_sha256="6" * 64,
        project_id="project-alignment",
        document_id="document-alignment",
        arm_id="human",
        source_language="en",
        target_language="vi",
        segments=[
            make_alignment_target_segment(
                segment_id=row["segment_id"],
                chapter_id=row["chapter_id"],
                order_index=row["order_index"],
                text=row["text"],
            )
            for row in fixture["target_segments"]
        ],
    )


def _coverage() -> dict[str, int]:
    return {
        "source_block_count": 9,
        "target_segment_count": 9,
        "accepted_mapping_count": 4,
        "review_mapping_count": 1,
        "ambiguous_mapping_count": 1,
        "missing_mapping_count": 1,
        "added_mapping_count": 1,
        "accepted_source_block_count": 6,
        "review_source_block_count": 1,
        "ambiguous_source_block_count": 1,
        "missing_source_block_count": 1,
        "accepted_target_segment_count": 6,
        "review_target_segment_count": 1,
        "ambiguous_target_segment_count": 1,
        "added_target_segment_count": 1,
    }


def _manifest(common, target, fixture: dict) -> dict:
    return seal_alignment_manifest(
        {
            "schema_id": "AlignmentManifestV1",
            "schema_version": "1.0.0",
            "manifest_id": "alignment-mixed-v1",
            "created_at": "2026-07-18T00:00:00Z",
            "producer": {
                "workstream": "evaluation",
                "component": "alignment_fixture",
                "component_version": "1.0.0",
                "code_commit": "b" * 40,
            },
            "source_read_model": {
                "project_id": common.project_id,
                "document_id": common.document_id,
                "source_schema_id": common.source_schema_id,
                "source_schema_version": common.source_schema_version,
                "source_read_model_sha256": alignment_source_read_model_sha256(common),
                "eligible_source_block_count": 9,
            },
            "target_snapshot": {
                "artifact_id": target.artifact_id,
                "artifact_sha256": target.artifact_sha256,
                "project_id": target.project_id,
                "document_id": target.document_id,
                "arm_id": target.arm_id,
                "source_language": target.source_language,
                "target_language": target.target_language,
                "segments_sha256": target.segments_sha256,
                "target_segment_count": 9,
            },
            "method": {
                "method_id": "fixture_alignment",
                "method_version": "1.0.0",
                "implementation_commit": "c" * 40,
                "prompt_version": None,
                "model_id": None,
            },
            "mappings": fixture["mappings"],
            "coverage": _coverage(),
            "integrity": {"manifest_sha256": "0" * 64},
        }
    )


def test_mixed_manifest_validates_all_mapping_kinds_without_mutation():
    fixture = _fixture()
    common = _common(fixture)
    target = _target(fixture)
    manifest = _manifest(common, target, fixture)
    before = copy.deepcopy(manifest)

    validated = validate_alignment_bindings(manifest, common, target)

    assert manifest == before
    assert [row["mapping_kind"] for row in validated["mappings"]] == [
        "1:1",
        "1:N",
        "N:1",
        "N:M",
        "missing",
        "added",
        "1:1",
        "ambiguous",
    ]
    assert validated["coverage"] == _coverage()


def test_target_snapshot_rejects_exact_text_tampering():
    fixture = _fixture()
    target = _target(fixture)
    tampered_segment = replace(target.segments[0], text="changed")
    tampered = replace(
        target, segments=(tampered_segment, *target.segments[1:])
    )

    with pytest.raises(ContractValidationError) as exc_info:
        validate_alignment_target_snapshot(tampered)

    assert exc_info.value.code == "target_text_hash"


def test_resealed_manifest_still_rejects_source_and_target_binding_drift():
    fixture = _fixture()
    common = _common(fixture)
    target = _target(fixture)
    manifest = _manifest(common, target, fixture)

    changed_source = _common(fixture, text_suffix=" changed")
    with pytest.raises(ContractValidationError) as exc_info:
        validate_alignment_bindings(manifest, changed_source, target)
    assert exc_info.value.code == "source_read_model_binding"

    changed_target = replace(target, artifact_sha256="7" * 64)
    changed_target = replace(
        changed_target,
        segments_sha256=target.segments_sha256,
    )
    with pytest.raises(ContractValidationError) as exc_info:
        validate_alignment_bindings(manifest, common, changed_target)
    assert exc_info.value.code == "target_segments_hash"

    drifted_source_binding = copy.deepcopy(manifest)
    drifted_source_binding["source_read_model"]["source_read_model_sha256"] = "f" * 64
    drifted_source_binding = seal_alignment_manifest(drifted_source_binding)
    with pytest.raises(ContractValidationError) as exc_info:
        validate_alignment_bindings(drifted_source_binding, common, target)
    assert exc_info.value.code == "source_read_model_binding"

    drifted_target_binding = copy.deepcopy(manifest)
    drifted_target_binding["target_snapshot"]["artifact_sha256"] = "f" * 64
    drifted_target_binding = seal_alignment_manifest(drifted_target_binding)
    with pytest.raises(ContractValidationError) as exc_info:
        validate_alignment_bindings(drifted_target_binding, common, target)
    assert exc_info.value.code == "target_snapshot_binding"


def test_unknown_keys_non_finite_confidence_and_coverage_drift_fail_closed():
    fixture = _fixture()
    common = _common(fixture)
    target = _target(fixture)

    unknown = _manifest(common, target, fixture)
    unknown["unexpected"] = True
    with pytest.raises(ContractValidationError) as exc_info:
        validate_alignment_manifest(unknown)
    assert exc_info.value.code == "unknown_keys"

    non_finite = _manifest(common, target, fixture)
    non_finite["mappings"][1]["confidence"] = float("nan")
    with pytest.raises(ContractValidationError) as exc_info:
        validate_alignment_manifest(non_finite)
    assert exc_info.value.code == "non_finite"

    bad_coverage = _manifest(common, target, fixture)
    bad_coverage["coverage"]["accepted_mapping_count"] += 1
    bad_coverage = seal_alignment_manifest(bad_coverage)
    with pytest.raises(ContractValidationError) as exc_info:
        validate_alignment_manifest(bad_coverage)
    assert exc_info.value.code == "coverage_mismatch"


def test_overlap_foreign_ids_and_non_monotonic_order_fail_closed():
    fixture = _fixture()
    common = _common(fixture)
    target = _target(fixture)

    overlap_fixture = copy.deepcopy(fixture)
    overlap_fixture["mappings"][1]["source_block_ids"] = ["b01"]
    with pytest.raises(ContractValidationError) as exc_info:
        validate_alignment_manifest(_manifest(common, target, overlap_fixture))
    assert exc_info.value.code == "duplicate"

    foreign_fixture = copy.deepcopy(fixture)
    foreign_fixture["mappings"][6]["target_segment_ids"] = ["foreign"]
    foreign_manifest = _manifest(common, target, foreign_fixture)
    with pytest.raises(ContractValidationError) as exc_info:
        validate_alignment_bindings(foreign_manifest, common, target)
    assert exc_info.value.code == "foreign_target_id"

    reordered_fixture = copy.deepcopy(fixture)
    reordered_fixture["mappings"][1], reordered_fixture["mappings"][2] = (
        reordered_fixture["mappings"][2],
        reordered_fixture["mappings"][1],
    )
    reordered_manifest = _manifest(common, target, reordered_fixture)
    with pytest.raises(ContractValidationError) as exc_info:
        validate_alignment_bindings(reordered_manifest, common, target)
    assert exc_info.value.code == "source_exact_cover"


def test_cardinality_and_review_provenance_are_closed():
    fixture = _fixture()
    common = _common(fixture)
    target = _target(fixture)

    wrong_kind = copy.deepcopy(fixture)
    wrong_kind["mappings"][1]["mapping_kind"] = "1:1"
    with pytest.raises(ContractValidationError) as exc_info:
        validate_alignment_manifest(_manifest(common, target, wrong_kind))
    assert exc_info.value.code == "mapping_cardinality"

    missing_review_hash = copy.deepcopy(fixture)
    missing_review_hash["mappings"][2]["decision_artifact_sha256"] = None
    with pytest.raises(ContractValidationError) as exc_info:
        validate_alignment_manifest(
            _manifest(common, target, missing_review_hash)
        )
    assert exc_info.value.code == "review_provenance"
