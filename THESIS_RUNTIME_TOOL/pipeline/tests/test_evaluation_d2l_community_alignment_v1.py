from __future__ import annotations

import copy
import hashlib
from dataclasses import replace
from pathlib import Path

import pytest

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
from pipeline.eval.d2l_community_alignment_v1 import (
    COMMUNITY_ARM_ID,
    D2LCommunityAlignmentError,
    build_d2l_community_target_read_model,
    build_d2l_structural_audit_plan,
    build_d2l_structural_review_manifest,
    resolve_d2l_structural_audit_sections,
)
from pipeline.eval.d2l_alignment_audit_packet_v1 import (
    AUDIT_PACKET_CANONICAL_POLICY,
    build_d2l_structural_audit_packet,
    validate_d2l_structural_audit_packet,
    validate_d2l_structural_audit_packet_bindings,
)
from pipeline.eval.contracts_v1 import (
    ContractValidationError,
    seal_payload,
)


CHAPTER_ID = "d2l_multilayer_perceptrons"


def _source(*, bad_block_id: bool = False) -> CommonSourceSnapshotV1:
    first_id = "foreign_index_b001" if bad_block_id else f"{CHAPTER_ID}_index_b001"
    blocks = (
        CommonBlockV1(first_id, CHAPTER_ID, 0, "heading", "# MLP", "translate"),
        CommonBlockV1(
            f"{CHAPTER_ID}_index_b002",
            CHAPTER_ID,
            1,
            "prose",
            "Chapter introduction.",
            "translate",
        ),
        CommonBlockV1(
            f"{CHAPTER_ID}_index_b003",
            CHAPTER_ID,
            2,
            "code",
            "```toc\nmlp\n```",
            "preserve",
        ),
        CommonBlockV1(
            f"{CHAPTER_ID}_mlp_b001",
            CHAPTER_ID,
            3,
            "heading",
            "# Section",
            "translate",
        ),
        CommonBlockV1(
            f"{CHAPTER_ID}_mlp_b002",
            CHAPTER_ID,
            4,
            "prose",
            "First paragraph.",
            "translate",
        ),
        CommonBlockV1(
            f"{CHAPTER_ID}_mlp_b003",
            CHAPTER_ID,
            5,
            "code",
            "```python\nx = 1\n```",
            "preserve",
        ),
        CommonBlockV1(
            f"{CHAPTER_ID}_mlp_b004",
            CHAPTER_ID,
            6,
            "prose",
            "Second paragraph.",
            "translate",
        ),
    )
    return CommonSourceSnapshotV1(
        source_schema_id="CanonicalSourcePackageV1",
        source_schema_version="1.0.0",
        source_binding=CanonicalSourcePackageBindingV1(
            project_id="project-d2l",
            document_id="document-d2l",
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
        blocks=blocks,
    )


def _artifact(source: CommonSourceSnapshotV1) -> dict:
    rows = []
    counts = {"translated": 0, "preserved": 0}
    for block in source.blocks:
        status = "translated" if block.admission == "translate" else "preserved"
        counts[status] += 1
        rows.append(
            {
                "block_id": block.block_id,
                "status": status,
                "target_text": (
                    f"machine::{block.block_id}"
                    if status == "translated"
                    else block.source_text
                ),
                "error_code": None,
            }
        )
    return seal_translation_artifact(
        {
            "schema_id": "TranslationArtifactV1",
            "schema_version": "1.0.0",
            "artifact_id": "translation-s1",
            "created_at": "2026-07-18T00:00:00Z",
            "producer": {
                "workstream": "d2l",
                "component": "fixture_writer",
                "component_version": "1.0.0",
                "code_commit": "a" * 40,
            },
            "source_binding": source_binding_to_dict(source.source_binding),
            "run_identity": {
                "logical_run_id": "logical-s1",
                "attempt_run_id": "attempt-s1",
                "arm_id": "s1",
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
                "preserved_count": counts["preserved"],
                "excluded_count": 0,
                "review_held_count": 0,
                "missing_count": 0,
                "failed_count": 0,
            },
            "integrity": {"artifact_sha256": "0" * 64},
        }
    )


def _common(*, bad_block_id: bool = False):
    source = _source(bad_block_id=bad_block_id)
    return build_common_evaluation_input(source, [_artifact(source)])


def _write_target(
    tmp_path: Path,
    *,
    extra_prose: bool = False,
    first_type: str = "heading",
) -> Path:
    chapter = tmp_path / "chapter_multilayer-perceptrons"
    chapter.mkdir(parents=True)
    heading = "# Perceptron đa tầng" if first_type == "heading" else "Perceptron đa tầng"
    chapter.joinpath("index.md").write_text(
        f"{heading}\n\nGiới thiệu chương.\n\n```toc\nmlp\n```\n",
        encoding="utf-8",
    )
    chapter.joinpath("index_origin.md").write_text(
        "# MLP\n\nChapter introduction.\n\n```toc\nmlp\n```\n",
        encoding="utf-8",
    )
    middle = "\n\nĐoạn được thêm." if extra_prose else ""
    chapter.joinpath("mlp.md").write_text(
        "# Mục chính\n\nĐoạn thứ nhất."
        + middle
        + "\n\n```python\nx = 1\n```\n\nĐoạn thứ hai.\n",
        encoding="utf-8",
    )
    chapter.joinpath("mlp_origin.md").write_text(
        "# Section\n\nFirst paragraph.\n\n"
        "```python\nx = 1\n```\n\nSecond paragraph.\n",
        encoding="utf-8",
    )
    return chapter


def _target(tmp_path: Path, **kwargs):
    return build_d2l_community_target_read_model(
        _write_target(tmp_path, **kwargs),
        repository_commit="b" * 40,
        artifact_id="community-mlp-v1",
        project_id="project-d2l",
        document_id="document-d2l",
        chapter_id=CHAPTER_ID,
    )


def _manifest(common, target):
    return build_d2l_structural_review_manifest(
        common,
        target,
        manifest_id="alignment-mlp-review-v1",
        created_at="2026-07-18T00:00:00Z",
        producer_code_commit="c" * 40,
        implementation_commit="d" * 40,
    )


def test_exact_structure_builds_deterministic_review_held_manifest(tmp_path: Path):
    common = _common()
    target = _target(tmp_path)

    manifest = _manifest(common, target)

    assert target.snapshot.arm_id == COMMUNITY_ARM_ID
    assert len(target.snapshot.segments) == 5
    assert [row.text for row in target.snapshot.segments] == [
        "# Perceptron đa tầng",
        "Giới thiệu chương.",
        "# Mục chính",
        "Đoạn thứ nhất.",
        "Đoạn thứ hai.",
    ]
    assert manifest["coverage"]["review_mapping_count"] == 5
    assert manifest["coverage"]["accepted_mapping_count"] == 0
    assert all(row["mapping_kind"] == "1:1" for row in manifest["mappings"])
    assert all(
        row["decision_state"] == "review_required"
        and row["confidence"] is None
        for row in manifest["mappings"]
    )
    assert manifest == _manifest(common, target)

    audit_plan = build_d2l_structural_audit_plan(manifest, common, target)
    repeated = build_d2l_structural_audit_plan(manifest, common, target)
    assert audit_plan == repeated
    assert audit_plan.population_count == 5
    assert audit_plan.sample_count == 5
    assert audit_plan.origin_files_sha256 == target.origin_files_sha256
    assert {row.section_slug for row in audit_plan.selections} == {"index", "mlp"}
    assert all(row.selection_reasons for row in audit_plan.selections)
    assert any(
        "section_first" in row.selection_reasons for row in audit_plan.selections
    )
    assert any(
        "section_last" in row.selection_reasons for row in audit_plan.selections
    )

    passed = resolve_d2l_structural_audit_sections(
        audit_plan, {row.mapping_id: True for row in audit_plan.selections}
    )
    assert passed.accepted_sections == ("index", "mlp")
    assert passed.review_required_sections == ()
    assert passed.failed_mapping_ids == ()


def test_audit_packet_is_deterministic_and_contains_no_outcome(tmp_path: Path):
    common = _common()
    target = _target(tmp_path)
    manifest = _manifest(common, target)
    audit_plan = build_d2l_structural_audit_plan(manifest, common, target)
    manifest_before = copy.deepcopy(manifest)

    packet = build_d2l_structural_audit_packet(
        manifest,
        audit_plan,
        common,
        target,
        packet_id="mlp-structural-audit-v1",
        created_at="2026-07-18T00:00:00Z",
        producer_code_commit="e" * 40,
    )
    repeated = build_d2l_structural_audit_packet(
        manifest,
        audit_plan,
        common,
        target,
        packet_id="mlp-structural-audit-v1",
        created_at="2026-07-18T00:00:00Z",
        producer_code_commit="e" * 40,
    )

    assert packet == repeated
    assert manifest == manifest_before
    assert packet["sampling"] == {"population_count": 5, "sample_count": 5}
    assert len(packet["items"]) == 5
    assert all(row["source"]["text"] for row in packet["items"])
    assert all(row["target"]["text"] for row in packet["items"])
    assert all(
        "outcome" not in row and "decision" not in row
        for row in packet["items"]
    )
    assert (
        validate_d2l_structural_audit_packet_bindings(
            packet, manifest, audit_plan, common, target
        )
        == packet
    )


def test_resealed_audit_packet_cannot_drift_from_bound_text(tmp_path: Path):
    common = _common()
    target = _target(tmp_path)
    manifest = _manifest(common, target)
    audit_plan = build_d2l_structural_audit_plan(manifest, common, target)
    packet = build_d2l_structural_audit_packet(
        manifest,
        audit_plan,
        common,
        target,
        packet_id="mlp-structural-audit-v1",
        created_at="2026-07-18T00:00:00Z",
        producer_code_commit="e" * 40,
    )
    tampered = copy.deepcopy(packet)
    tampered["items"][0]["source"]["text"] += " drift"
    tampered["items"][0]["source"]["text_sha256"] = hashlib.sha256(
        tampered["items"][0]["source"]["text"].encode("utf-8")
    ).hexdigest()
    tampered = seal_payload(
        tampered,
        policy=AUDIT_PACKET_CANONICAL_POLICY,
        hash_path=("integrity", "packet_sha256"),
    )

    validate_d2l_structural_audit_packet(tampered)
    with pytest.raises(D2LCommunityAlignmentError) as exc_info:
        validate_d2l_structural_audit_packet_bindings(
            tampered, manifest, audit_plan, common, target
        )
    assert exc_info.value.code == "audit_packet_binding"


def test_audit_packet_rejects_forged_plan_and_forbidden_fields(tmp_path: Path):
    common = _common()
    target = _target(tmp_path)
    manifest = _manifest(common, target)
    audit_plan = build_d2l_structural_audit_plan(manifest, common, target)
    forged_plan = replace(audit_plan, selection_sha256="f" * 64)

    with pytest.raises(D2LCommunityAlignmentError) as exc_info:
        build_d2l_structural_audit_packet(
            manifest,
            forged_plan,
            common,
            target,
            packet_id="mlp-structural-audit-v1",
            created_at="2026-07-18T00:00:00Z",
            producer_code_commit="e" * 40,
        )
    assert exc_info.value.code == "audit_plan_binding"

    packet = build_d2l_structural_audit_packet(
        manifest,
        audit_plan,
        common,
        target,
        packet_id="mlp-structural-audit-v1",
        created_at="2026-07-18T00:00:00Z",
        producer_code_commit="e" * 40,
    )
    forbidden = copy.deepcopy(packet)
    forbidden["items"][0]["score"] = 1
    forbidden = seal_payload(
        forbidden,
        policy=AUDIT_PACKET_CANONICAL_POLICY,
        hash_path=("integrity", "packet_sha256"),
    )
    with pytest.raises(ContractValidationError) as exc_info:
        validate_d2l_structural_audit_packet(forbidden)
    assert exc_info.value.code == "forbidden_runtime_data"


def test_failed_audit_row_routes_its_entire_section_to_review(tmp_path: Path):
    common = _common()
    target = _target(tmp_path)
    audit_plan = build_d2l_structural_audit_plan(
        _manifest(common, target), common, target
    )
    outcomes = {row.mapping_id: True for row in audit_plan.selections}
    failed = next(
        row for row in audit_plan.selections if row.section_slug == "mlp"
    )
    outcomes[failed.mapping_id] = False

    disposition = resolve_d2l_structural_audit_sections(audit_plan, outcomes)

    assert disposition.accepted_sections == ("index",)
    assert disposition.review_required_sections == ("mlp",)
    assert disposition.failed_mapping_ids == (failed.mapping_id,)


def test_audit_outcomes_are_exact_cover_and_boolean(tmp_path: Path):
    common = _common()
    target = _target(tmp_path)
    audit_plan = build_d2l_structural_audit_plan(
        _manifest(common, target), common, target
    )
    outcomes = {row.mapping_id: True for row in audit_plan.selections}
    omitted = audit_plan.selections[0].mapping_id

    with pytest.raises(D2LCommunityAlignmentError) as exc_info:
        resolve_d2l_structural_audit_sections(
            audit_plan,
            {key: value for key, value in outcomes.items() if key != omitted},
        )
    assert exc_info.value.code == "audit_exact_cover"
    assert omitted in exc_info.value.detail

    with pytest.raises(D2LCommunityAlignmentError) as exc_info:
        resolve_d2l_structural_audit_sections(
            audit_plan, {**outcomes, "foreign-mapping": True}
        )
    assert exc_info.value.code == "audit_exact_cover"
    assert "foreign-mapping" in exc_info.value.detail

    with pytest.raises(D2LCommunityAlignmentError) as exc_info:
        resolve_d2l_structural_audit_sections(
            audit_plan, {**outcomes, omitted: 1}
        )
    assert exc_info.value.code == "audit_outcome_type"
    assert omitted in exc_info.value.detail


def test_exact_target_bytes_are_hashed_and_byte_drift_changes_identity(tmp_path: Path):
    original = _target(tmp_path / "first")
    changed_chapter = _write_target(tmp_path / "second")
    mlp = changed_chapter / "mlp.md"
    mlp.write_text(
        mlp.read_text(encoding="utf-8").replace("Đoạn thứ nhất.", "Đoạn thứ nhất!"),
        encoding="utf-8",
    )
    changed = build_d2l_community_target_read_model(
        changed_chapter,
        repository_commit="b" * 40,
        artifact_id="community-mlp-v1",
        project_id="project-d2l",
        document_id="document-d2l",
        chapter_id=CHAPTER_ID,
    )

    assert original.files_sha256 != changed.files_sha256
    assert original.snapshot.artifact_sha256 != changed.snapshot.artifact_sha256
    assert original.snapshot.segments_sha256 != changed.snapshot.segments_sha256


def test_origin_bytes_are_hashed_without_changing_target_identity(tmp_path: Path):
    original = _target(tmp_path / "first")
    changed_chapter = _write_target(tmp_path / "second")
    origin = changed_chapter / "mlp_origin.md"
    origin.write_text(
        origin.read_text(encoding="utf-8").replace(
            "First paragraph.", "First paragraph!"
        ),
        encoding="utf-8",
    )
    changed = build_d2l_community_target_read_model(
        changed_chapter,
        repository_commit="b" * 40,
        artifact_id="community-mlp-v1",
        project_id="project-d2l",
        document_id="document-d2l",
        chapter_id=CHAPTER_ID,
    )

    assert original.origin_files_sha256 != changed.origin_files_sha256
    assert original.files_sha256 == changed.files_sha256
    assert original.snapshot.artifact_sha256 == changed.snapshot.artifact_sha256
    assert original.snapshot.segments_sha256 == changed.snapshot.segments_sha256


def test_added_target_prose_fails_closed_instead_of_guessing(tmp_path: Path):
    with pytest.raises(D2LCommunityAlignmentError) as exc_info:
        _manifest(_common(), _target(tmp_path, extra_prose=True))

    assert exc_info.value.code == "structural_mismatch"
    assert "counts=5/6" in exc_info.value.detail


def test_structural_type_drift_fails_closed(tmp_path: Path):
    with pytest.raises(D2LCommunityAlignmentError) as exc_info:
        _manifest(_common(), _target(tmp_path, first_type="prose"))

    assert exc_info.value.code == "structural_mismatch"
    assert "source=('index', 0, 'heading')" in exc_info.value.detail


def test_origin_source_text_drift_fails_before_alignment_acceptance(tmp_path: Path):
    target = _target(tmp_path)
    drifted_blocks = list(_source().blocks)
    drifted_blocks[4] = CommonBlockV1(
        drifted_blocks[4].block_id,
        drifted_blocks[4].chapter_id,
        drifted_blocks[4].order_index,
        drifted_blocks[4].block_type,
        "First paragraph!",
        drifted_blocks[4].admission,
    )
    source = _source()
    drifted_source = CommonSourceSnapshotV1(
        source_schema_id=source.source_schema_id,
        source_schema_version=source.source_schema_version,
        source_binding=source.source_binding,
        blocks=tuple(drifted_blocks),
    )
    common = build_common_evaluation_input(
        drifted_source, [_artifact(drifted_source)]
    )

    with pytest.raises(D2LCommunityAlignmentError) as exc_info:
        _manifest(common, target)

    assert exc_info.value.code == "origin_source_text"
    assert drifted_blocks[4].block_id in exc_info.value.detail


def test_foreign_source_address_fails_closed(tmp_path: Path):
    with pytest.raises(D2LCommunityAlignmentError) as exc_info:
        _manifest(_common(bad_block_id=True), _target(tmp_path))

    assert exc_info.value.code == "source_block_address"


def test_target_adapter_rejects_unpinned_or_missing_input(tmp_path: Path):
    with pytest.raises(D2LCommunityAlignmentError) as exc_info:
        build_d2l_community_target_read_model(
            tmp_path / "missing",
            repository_commit="b" * 40,
            artifact_id="community-mlp-v1",
            project_id="project-d2l",
            document_id="document-d2l",
            chapter_id=CHAPTER_ID,
        )
    assert exc_info.value.code == "chapter_directory"

    chapter = _write_target(tmp_path / "present")
    chapter.joinpath("mlp_origin.md").unlink()
    with pytest.raises(D2LCommunityAlignmentError) as exc_info:
        build_d2l_community_target_read_model(
            chapter,
            repository_commit="b" * 40,
            artifact_id="community-mlp-v1",
            project_id="project-d2l",
            document_id="document-d2l",
            chapter_id=CHAPTER_ID,
        )
    assert exc_info.value.code == "origin_section"

    with pytest.raises(D2LCommunityAlignmentError) as exc_info:
        build_d2l_community_target_read_model(
            chapter,
            repository_commit="unpinned",
            artifact_id="community-mlp-v1",
            project_id="project-d2l",
            document_id="document-d2l",
            chapter_id=CHAPTER_ID,
        )
    assert exc_info.value.code == "repository_commit"
