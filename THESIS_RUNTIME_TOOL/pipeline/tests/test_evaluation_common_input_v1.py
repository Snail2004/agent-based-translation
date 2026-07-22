from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from pipeline.eval.common_input_v1 import (
    AdmissionPolicyIdentityV1,
    CanonicalComponentIdentityV1,
    CanonicalProjectionIdentityV1,
    CanonicalSourcePackageBindingV1,
    CommonBlockV1,
    CommonSourceSnapshotV1,
    LegacyD2LSourceBindingV1,
    build_common_evaluation_input,
    project_d2l_source_snapshot,
    seal_translation_artifact,
    source_binding_to_dict,
    validate_translation_artifact,
)
from pipeline.eval.contracts_v1 import ContractValidationError


FIXTURES = Path(__file__).parent / "fixtures" / "evaluation_v1"


def _load_d2l() -> dict:
    return json.loads((FIXTURES / "d2l_input_valid.json").read_text(encoding="utf-8"))


def _canonical_source(
    blocks: tuple[CommonBlockV1, ...] | None = None,
) -> CommonSourceSnapshotV1:
    return CommonSourceSnapshotV1(
        source_schema_id="CanonicalSourcePackageV1",
        source_schema_version="1.0.0",
        source_binding=CanonicalSourcePackageBindingV1(
            project_id="project",
            document_id="document",
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
        blocks=blocks
        or (
            CommonBlockV1("b1", "ch1", 0, "paragraph", "one", "translate"),
            CommonBlockV1("b2", "ch1", 1, "paragraph", "two", "preserve"),
        ),
    )


def _translation_artifact(
    source: CommonSourceSnapshotV1,
    *,
    arm_id: str = "s1",
    attempt_run_id: str = "attempt-s1",
    status_overrides: dict[str, str] | None = None,
    source_binding_overrides: dict | None = None,
    producer_workstream: str = "d2l",
) -> dict:
    status_overrides = status_overrides or {}
    rows = []
    for block in source.blocks:
        default_status = {
            "translate": "translated",
            "translate_structured": "translated",
            "preserve": "preserved",
            "exclude": "excluded",
            "review_required": "review_held",
        }[block.admission]
        status = status_overrides.get(block.block_id, default_status)
        target_text = None
        error_code = None
        if status == "translated":
            target_text = f"translated::{arm_id}::{block.block_id}"
        elif status == "preserved":
            target_text = block.source_text
        elif status == "failed":
            error_code = "fixture_failure"
        rows.append(
            {
                "block_id": block.block_id,
                "status": status,
                "target_text": target_text,
                "error_code": error_code,
            }
        )
    counts = {
        status: 0
        for status in (
            "translated",
            "preserved",
            "excluded",
            "review_held",
            "missing",
            "failed",
        )
    }
    for row in rows:
        counts[row["status"]] += 1
    source_binding = source_binding_to_dict(source.source_binding)
    source_binding.update(source_binding_overrides or {})
    return seal_translation_artifact(
        {
            "schema_id": "TranslationArtifactV1",
            "schema_version": "1.0.0",
            "artifact_id": f"translation-{arm_id}",
            "created_at": "2026-07-17T00:00:00Z",
            "producer": {
                "workstream": producer_workstream,
                "component": "fixture_translation_writer",
                "component_version": "1.0.0",
                "code_commit": "a" * 40,
            },
            "source_binding": source_binding,
            "run_identity": {
                "logical_run_id": f"logical-{arm_id}",
                "attempt_run_id": attempt_run_id,
                "arm_id": arm_id,
                "profile_id": "technical_d2l_v1",
                "profile_config_sha256": "9" * 64,
                "source_language": "en",
                "target_language": "vi",
            },
            "translations": rows,
            "coverage": {
                "source_block_count": len(rows),
                "eligible_count": counts["translated"] + counts["missing"] + counts["failed"],
                "translated_count": counts["translated"],
                "preserved_count": counts["preserved"],
                "excluded_count": counts["excluded"],
                "review_held_count": counts["review_held"],
                "missing_count": counts["missing"],
                "failed_count": counts["failed"],
            },
            "integrity": {"artifact_sha256": "0" * 64},
        }
    )


def test_d2l_source_projection_is_immutable_and_source_only():
    payload = _load_d2l()
    before = copy.deepcopy(payload)

    source = project_d2l_source_snapshot(payload)

    assert payload == before
    assert [block.block_id for block in source.blocks] == ["b001", "b002"]
    assert isinstance(source.source_binding, LegacyD2LSourceBindingV1)
    assert source.source_binding.binding_kind == "legacy_d2l"
    assert source.source_binding.source_db_sha256 == payload["identity"]["source_db_sha256"]
    assert (
        source.source_binding.runtime_manifest_sha256
        == payload["identity"]["runtime_manifest_sha256"]
    )
    assert not hasattr(source, "runtime_terms")
    assert not hasattr(source, "injection_rows")


def test_translation_artifact_validates_without_mutation_and_preserves_row_order():
    source = _canonical_source()
    artifact = _translation_artifact(source)
    before = copy.deepcopy(artifact)

    validated = validate_translation_artifact(artifact)

    assert artifact == before
    assert [row["block_id"] for row in validated["translations"]] == ["b1", "b2"]
    assert validated["coverage"] == {
        "eligible_count": 1,
        "excluded_count": 0,
        "failed_count": 0,
        "missing_count": 0,
        "preserved_count": 1,
        "review_held_count": 0,
        "source_block_count": 2,
        "translated_count": 1,
    }


def test_evaluation_authored_artifact_reseal_still_fails_public_validation():
    artifact = _translation_artifact(_canonical_source())
    artifact["producer"]["workstream"] = "evaluation"
    artifact = seal_translation_artifact(artifact)

    with pytest.raises(ContractValidationError) as exc_info:
        validate_translation_artifact(artifact)

    assert exc_info.value.code == "enum"
    assert exc_info.value.path == "$.producer.workstream"


def test_legacy_binding_is_offline_only_and_cross_kind_relabels_fail_closed():
    legacy_source = project_d2l_source_snapshot(_load_d2l())
    legacy_artifact = _translation_artifact(legacy_source)

    with pytest.raises(ContractValidationError) as exc_info:
        validate_translation_artifact(legacy_artifact)
    assert exc_info.value.code == "enum"
    assert exc_info.value.path == "$.source_binding.binding_kind"

    relabeled_legacy = copy.deepcopy(legacy_artifact)
    relabeled_legacy["source_binding"]["binding_kind"] = "canonical_source_package_v1"
    relabeled_legacy = seal_translation_artifact(relabeled_legacy)
    with pytest.raises(ContractValidationError, match="missing_keys"):
        build_common_evaluation_input(legacy_source, [relabeled_legacy])

    canonical_source = _canonical_source()
    relabeled_canonical = _translation_artifact(canonical_source)
    relabeled_canonical["source_binding"]["binding_kind"] = "legacy_d2l"
    relabeled_canonical = seal_translation_artifact(relabeled_canonical)
    with pytest.raises(ContractValidationError, match="enum"):
        validate_translation_artifact(relabeled_canonical)


@pytest.mark.parametrize(
    "component, hash_field",
    [
        ("document", "sha256"),
        ("structure", "sha256"),
        ("asset_manifest", "sha256"),
        ("admitted_projection", "payload_sha256"),
        ("admission_policy", "policy_sha256"),
    ],
)
def test_each_canonical_component_hash_drift_reseals_but_fails_join(
    component, hash_field
):
    source = _canonical_source()
    artifact = _translation_artifact(source)
    artifact["source_binding"][component][hash_field] = "f" * 64
    artifact = seal_translation_artifact(artifact)

    validate_translation_artifact(artifact)
    with pytest.raises(ContractValidationError, match="source_binding"):
        build_common_evaluation_input(source, [artifact])


def test_common_input_joins_full_binding_and_keeps_exact_translation_text():
    source = project_d2l_source_snapshot(_load_d2l())
    artifact = _translation_artifact(source)

    common = build_common_evaluation_input(source, [artifact])

    assert common.project_id == source.project_id
    assert common.document_id == source.document_id
    assert common.arms[0].attempt_run_id == "attempt-s1"
    assert common.arms[0].artifact_sha256 == artifact["integrity"]["artifact_sha256"]
    translated = next(row for row in common.translations if row.block_id == "b001")
    assert translated.target_text == "translated::s1::b001"
    assert not hasattr(common, "runtime_terms")
    assert not hasattr(common, "injection_rows")


@pytest.mark.parametrize(
    "mutation, expected_code",
    [
        (lambda row: row.update({"unexpected": 1}), "unknown_keys"),
        (lambda row: row["coverage"].update({"translated_count": 99}), "coverage_mismatch"),
        (lambda row: row["run_identity"].update({"attempt_run_id": ""}), "empty_string"),
    ],
)
def test_artifact_contract_rejects_unknown_keys_bad_coverage_and_empty_attempt(
    mutation, expected_code
):
    source = _canonical_source()
    artifact = _translation_artifact(source)
    mutation(artifact)

    with pytest.raises(ContractValidationError) as exc_info:
        validate_translation_artifact(artifact)

    assert exc_info.value.code == expected_code


def test_source_hash_drift_foreign_blocks_and_wrong_order_fail_closed():
    source = _canonical_source()
    drifted = _translation_artifact(
        source,
        source_binding_overrides={
            "document": {"schema_version": "1.5.0", "sha256": "f" * 64}
        },
    )
    with pytest.raises(ContractValidationError, match="source_binding"):
        build_common_evaluation_input(source, [drifted])

    foreign = _translation_artifact(source)
    foreign["translations"][0]["block_id"] = "foreign-block"
    foreign = seal_translation_artifact(foreign)
    with pytest.raises(ContractValidationError, match="block_coverage"):
        build_common_evaluation_input(source, [foreign])

    duplicate = _translation_artifact(source)
    duplicate["translations"][1]["block_id"] = duplicate["translations"][0]["block_id"]
    duplicate = seal_translation_artifact(duplicate)
    with pytest.raises(ContractValidationError, match="duplicate"):
        validate_translation_artifact(duplicate)

    reordered = _translation_artifact(source)
    reordered["translations"].reverse()
    reordered = seal_translation_artifact(reordered)
    with pytest.raises(ContractValidationError, match="block_coverage"):
        build_common_evaluation_input(source, [reordered])


def test_admission_status_and_preserved_text_are_checked_against_source():
    source = _canonical_source()
    wrong_status = _translation_artifact(source, status_overrides={"b2": "missing"})
    with pytest.raises(ContractValidationError, match="admission_status"):
        build_common_evaluation_input(source, [wrong_status])

    wrong_text = _translation_artifact(source)
    wrong_text["translations"][1]["target_text"] = "changed"
    wrong_text = seal_translation_artifact(wrong_text)
    with pytest.raises(ContractValidationError, match="preserved_text"):
        build_common_evaluation_input(source, [wrong_text])


def test_missing_and_failed_rows_remain_explicit_and_are_not_imputed():
    source = _canonical_source(
        blocks=(
            CommonBlockV1("b1", "ch1", 0, "paragraph", "one", "translate"),
            CommonBlockV1("b2", "ch1", 1, "paragraph", "two", "translate"),
        ),
    )
    artifact = _translation_artifact(
        source, status_overrides={"b1": "missing", "b2": "failed"}
    )

    common = build_common_evaluation_input(source, [artifact])

    assert [(row.status, row.target_text, row.error_code) for row in common.translations] == [
        ("missing", None, None),
        ("failed", None, "fixture_failure"),
    ]


def test_common_source_snapshot_rejects_duplicate_ids_and_reopened_chapters():
    duplicate = _canonical_source(
        blocks=(
            CommonBlockV1("b1", "ch1", 0, "paragraph", "one", "translate"),
            CommonBlockV1("b1", "ch1", 1, "paragraph", "two", "translate"),
        ),
    )
    with pytest.raises(ContractValidationError, match="duplicate"):
        build_common_evaluation_input(duplicate, [_translation_artifact(duplicate)])

    reopened = _canonical_source(
        blocks=(
            CommonBlockV1("a1", "ch1", 0, "paragraph", "one", "translate"),
            CommonBlockV1("b1", "ch2", 0, "paragraph", "two", "translate"),
            CommonBlockV1("a2", "ch1", 1, "paragraph", "three", "translate"),
        ),
    )
    with pytest.raises(ContractValidationError, match="block_order"):
        build_common_evaluation_input(reopened, [_translation_artifact(reopened)])
