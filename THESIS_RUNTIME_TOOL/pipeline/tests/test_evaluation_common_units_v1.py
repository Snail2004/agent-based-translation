from __future__ import annotations

import copy
import json
from collections import Counter
from pathlib import Path

import pytest

from pipeline.eval.alignment_manifest_v1 import (
    alignment_source_read_model_sha256,
    build_alignment_target_snapshot,
    make_alignment_target_segment,
    seal_alignment_manifest,
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
from pipeline.eval.common_units_v1 import build_common_evaluation_units
from pipeline.eval.contracts_v1 import ContractValidationError


FIXTURES = Path(__file__).parent / "fixtures" / "evaluation_v1"


def _fixture() -> dict:
    return json.loads(
        (FIXTURES / "alignment_mixed_case.json").read_text(encoding="utf-8")
    )


def _source(fixture: dict) -> CommonSourceSnapshotV1:
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
                source_text=row["text"],
                admission="translate",
            )
            for row in fixture["source_blocks"]
        ),
    )


def _artifact(
    source: CommonSourceSnapshotV1,
    *,
    arm_id: str,
    status_overrides: dict[str, str] | None = None,
    text_prefix: str | None = None,
) -> dict:
    status_overrides = status_overrides or {}
    text_prefix = text_prefix or f"machine::{arm_id}"
    rows = []
    for block in source.blocks:
        status = status_overrides.get(block.block_id, "translated")
        rows.append(
            {
                "block_id": block.block_id,
                "status": status,
                "target_text": (
                    f"{text_prefix}::{block.block_id}"
                    if status == "translated"
                    else None
                ),
                "error_code": "fixture_failure" if status == "failed" else None,
            }
        )
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
                "eligible_count": (
                    counts["translated"] + counts["missing"] + counts["failed"]
                ),
                "translated_count": counts["translated"],
                "preserved_count": 0,
                "excluded_count": 0,
                "review_held_count": 0,
                "missing_count": counts["missing"],
                "failed_count": counts["failed"],
            },
            "integrity": {"artifact_sha256": "0" * 64},
        }
    )


def _common(
    fixture: dict,
    *,
    s0_statuses: dict[str, str] | None = None,
    s1_statuses: dict[str, str] | None = None,
    s1_text_prefix: str | None = None,
):
    source = _source(fixture)
    return build_common_evaluation_input(
        source,
        [
            _artifact(source, arm_id="s0", status_overrides=s0_statuses),
            _artifact(
                source,
                arm_id="s1",
                status_overrides=s1_statuses,
                text_prefix=s1_text_prefix,
            ),
        ],
    )


def _target(fixture: dict, *, arm_id: str = "human", source_language: str = "en"):
    return build_alignment_target_snapshot(
        artifact_id=f"{arm_id}-artifact",
        artifact_sha256="6" * 64,
        project_id="project-alignment",
        document_id="document-alignment",
        arm_id=arm_id,
        source_language=source_language,
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


def _manifest(common, target, fixture: dict) -> dict:
    coverage = {
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
            "coverage": coverage,
            "integrity": {"manifest_sha256": "0" * 64},
        }
    )


def test_common_units_preserve_spans_states_and_added_coverage():
    fixture = _fixture()
    common = _common(fixture)
    target = _target(fixture)
    unit_set = build_common_evaluation_units(
        common, target, _manifest(common, target, fixture)
    )

    assert [unit.unit_id for unit in unit_set.units] == [
        "m01",
        "m02",
        "m03",
        "m04",
        "m05",
        "m07",
        "m08",
    ]
    merged = next(unit for unit in unit_set.units if unit.unit_id == "m03")
    assert merged.source_block_ids == ("b03", "b04")
    assert merged.source_text_parts == ("Source three.", "Source four.")
    human = next(view for view in merged.arm_views if view.arm_id == "human")
    assert human.status == "ready"
    assert human.segment_ids == ("h04",)
    assert human.text_parts == ("Target three and four.",)

    split = next(unit for unit in unit_set.units if unit.unit_id == "m02")
    human = next(view for view in split.arm_views if view.arm_id == "human")
    assert human.segment_ids == ("h02", "h03")
    assert human.text_parts == ("Target two A.", "Target two B.")

    missing = next(unit for unit in unit_set.units if unit.unit_id == "m05")
    human = next(view for view in missing.arm_views if view.arm_id == "human")
    assert human.status == "missing"
    assert human.text_parts == ()

    review_states = {
        unit.unit_id: next(
            view for view in unit.arm_views if view.arm_id == "human"
        ).status
        for unit in unit_set.units
        if unit.unit_id in {"m07", "m08"}
    }
    assert review_states == {"m07": "review_held", "m08": "review_held"}
    assert unit_set.added_target_segment_ids == ("h07",)
    assert unit_set.coverage.source_unit_count == 7
    assert unit_set.coverage.all_arm_ready_unit_count == 4
    arm_coverage = {
        row.arm_id: row for row in unit_set.coverage.arm_coverage
    }
    assert arm_coverage["human"].ready_unit_count == 4
    assert arm_coverage["human"].missing_unit_count == 1
    assert arm_coverage["human"].review_held_unit_count == 2
    assert unit_set.coverage.added_target_segment_count == 1


def test_projection_is_deterministic_immutable_and_preserves_exact_text_bytes():
    fixture = _fixture()
    fixture["target_segments"][0]["text"] = "a\u0301"
    common = _common(fixture)
    target = _target(fixture)
    manifest = _manifest(common, target, fixture)
    manifest_before = copy.deepcopy(manifest)
    common_before = copy.deepcopy(common)

    first = build_common_evaluation_units(common, target, manifest)
    second = build_common_evaluation_units(common, target, copy.deepcopy(manifest))

    assert first == second
    assert first.unit_set_sha256 == second.unit_set_sha256
    assert common == common_before
    assert manifest == manifest_before
    first_human = next(
        view for view in first.units[0].arm_views if view.arm_id == "human"
    )
    assert first_human.text_parts[0].encode("utf-8") == "a\u0301".encode("utf-8")


def test_machine_missing_and_failed_rows_stay_explicit():
    fixture = _fixture()
    common = _common(
        fixture,
        s0_statuses={"b02": "missing"},
        s1_statuses={"b03": "failed"},
    )
    target = _target(fixture)
    unit_set = build_common_evaluation_units(
        common, target, _manifest(common, target, fixture)
    )

    split = next(unit for unit in unit_set.units if unit.unit_id == "m02")
    s0 = next(view for view in split.arm_views if view.arm_id == "s0")
    assert s0.status == "missing"
    assert s0.text_parts == (None,)

    merged = next(unit for unit in unit_set.units if unit.unit_id == "m03")
    s1 = next(view for view in merged.arm_views if view.arm_id == "s1")
    assert s1.status == "failed"
    assert s1.segment_statuses == ("failed", "translated")
    assert s1.error_codes == ("fixture_failure", None)

    arm_coverage = {
        row.arm_id: row for row in unit_set.coverage.arm_coverage
    }
    assert arm_coverage["s0"].missing_unit_count == 1
    assert arm_coverage["s1"].failed_unit_count == 1
    assert unit_set.coverage.all_arm_ready_unit_count == 2


def test_target_arm_collision_and_language_mismatch_fail_closed():
    fixture = _fixture()
    common = _common(fixture)

    duplicate_target = _target(fixture, arm_id="s1")
    duplicate_manifest = _manifest(common, duplicate_target, fixture)
    with pytest.raises(ContractValidationError) as exc_info:
        build_common_evaluation_units(
            common, duplicate_target, duplicate_manifest
        )
    assert exc_info.value.code == "duplicate_arm"

    wrong_language = _target(fixture, source_language="fr")
    wrong_language_manifest = _manifest(common, wrong_language, fixture)
    with pytest.raises(ContractValidationError) as exc_info:
        build_common_evaluation_units(
            common, wrong_language, wrong_language_manifest
        )
    assert exc_info.value.code == "target_language_pair"


def test_unit_set_hash_changes_when_a_machine_translation_changes():
    fixture = _fixture()
    target = _target(fixture)

    first_common = _common(fixture)
    first = build_common_evaluation_units(
        first_common,
        target,
        _manifest(first_common, target, fixture),
    )

    second_common = _common(fixture, s1_text_prefix="changed")
    second = build_common_evaluation_units(
        second_common,
        target,
        _manifest(second_common, target, fixture),
    )

    assert first.unit_set_sha256 != second.unit_set_sha256
