from __future__ import annotations

import copy
import json
import subprocess
from pathlib import Path

import pytest

from pipeline.eval.common_input_v1 import (
    AdmissionPolicyIdentityV1,
    CanonicalComponentIdentityV1,
    CanonicalProjectionIdentityV1,
    CanonicalSourcePackageBindingV1,
    CommonArmV1,
    CommonBlockV1,
    CommonEvaluationInputV1,
)
from pipeline.eval.contracts_v1 import ContractValidationError
from pipeline.eval.d2l_community_alignment_v1 import (
    build_d2l_community_target_read_model,
)
from pipeline.eval.d2l_community_five_chapter_v1 import (
    D2LCommunityFiveChapterError,
    D2LCommunityFiveChapterInputsV1,
    ManualAlignmentOverrideV1,
    apply_d2l_community_alignment_audit,
    build_d2l_community_alignment_audit_plan,
    build_d2l_community_chapter_review_manifest,
    build_d2l_community_manual_decision,
    load_d2l_community_five_chapter_inputs,
    record_d2l_community_alignment_audit,
    validate_d2l_community_alignment_audit_record,
    validate_d2l_community_manual_decision,
    write_d2l_community_alignment_bundle,
)
from pipeline.ingest.canonical_source_package import canonical_json_sha256


CH_SPLIT = "d2l_split"
CH_MERGE = "d2l_merge"
COMMIT = "b" * 40


def _source_binding() -> CanonicalSourcePackageBindingV1:
    return CanonicalSourcePackageBindingV1(
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
    )


def _common(chapter_id: str, blocks: list[CommonBlockV1]) -> CommonEvaluationInputV1:
    return CommonEvaluationInputV1(
        source_schema_id="CanonicalSourcePackageV1",
        source_schema_version="1.5.0",
        source_binding=_source_binding(),
        blocks=tuple(blocks),
        arms=(
            CommonArmV1(
                artifact_id=f"anchor-{chapter_id}",
                artifact_sha256="6" * 64,
                logical_run_id="alignment-only",
                attempt_run_id="alignment-only",
                arm_id="alignment_language_anchor",
                profile_id="alignment-only",
                profile_config_sha256="7" * 64,
                source_language="en",
                target_language="vi",
            ),
        ),
        translations=(),
    )


def _write_split_chapter(root: Path) -> tuple[Path, list[CommonBlockV1]]:
    chapter = root / "chapter_split"
    chapter.mkdir(parents=True)
    origin_rows = ["# Heading\nSource introduction."] + [
        f"Source paragraph {index}." for index in range(35)
    ]
    target_rows = ["# Tiêu đề", "Giới thiệu."] + [
        f"Đoạn {index}." for index in range(35)
    ]
    chapter.joinpath("index_origin.md").write_text(
        "\n\n".join(origin_rows) + "\n", encoding="utf-8", newline="\n"
    )
    chapter.joinpath("index.md").write_text(
        "\n\n".join(target_rows) + "\n", encoding="utf-8", newline="\n"
    )
    blocks = [
        CommonBlockV1(
            f"{CH_SPLIT}_index_b{index + 1:03d}",
            CH_SPLIT,
            index,
            "heading" if index == 0 else "prose",
            text,
            "translate",
        )
        for index, text in enumerate(origin_rows)
    ]
    return chapter, blocks


def _write_merge_chapter(root: Path) -> tuple[Path, list[CommonBlockV1]]:
    chapter = root / "chapter_merge"
    chapter.mkdir(parents=True)
    origin_rows = ["Wrapper start.", "Wrapped body.", "Wrapper end."] + [
        f"Later source {index}." for index in range(35)
    ]
    target_rows = ["Bắt đầu. Nội dung. Kết thúc."] + [
        f"Đoạn sau {index}." for index in range(35)
    ]
    chapter.joinpath("index_origin.md").write_text(
        "\n\n".join(origin_rows) + "\n", encoding="utf-8", newline="\n"
    )
    chapter.joinpath("index.md").write_text(
        "\n\n".join(target_rows) + "\n", encoding="utf-8", newline="\n"
    )
    blocks = [
        CommonBlockV1(
            f"{CH_MERGE}_index_b{index + 1:03d}",
            CH_MERGE,
            100 + index,
            "prose",
            text,
            "translate",
        )
        for index, text in enumerate(origin_rows)
    ]
    return chapter, blocks


def _inputs(tmp_path: Path) -> D2LCommunityFiveChapterInputsV1:
    split_root, split_blocks = _write_split_chapter(tmp_path)
    merge_root, merge_blocks = _write_merge_chapter(tmp_path)
    split_common = _common(CH_SPLIT, split_blocks)
    merge_common = _common(CH_MERGE, merge_blocks)
    split_target = build_d2l_community_target_read_model(
        split_root,
        repository_commit=COMMIT,
        artifact_id="community-split",
        project_id="project-d2l",
        document_id="document-d2l",
        chapter_id=CH_SPLIT,
    )
    merge_target = build_d2l_community_target_read_model(
        merge_root,
        repository_commit=COMMIT,
        artifact_id="community-merge",
        project_id="project-d2l",
        document_id="document-d2l",
        chapter_id=CH_MERGE,
    )
    return D2LCommunityFiveChapterInputsV1(
        finalization_payload_sha256="8" * 64,
        candidate_tree_sha256="9" * 64,
        repository_commit=COMMIT,
        chapter_order=(CH_SPLIT, CH_MERGE),
        common_by_chapter={CH_SPLIT: split_common, CH_MERGE: merge_common},
        target_by_chapter={CH_SPLIT: split_target, CH_MERGE: merge_target},
    )


def _overrides() -> list[ManualAlignmentOverrideV1]:
    return [
        ManualAlignmentOverrideV1(
            "split-heading",
            CH_SPLIT,
            (f"{CH_SPLIT}_index_b001",),
            (
                f"community__{CH_SPLIT}__index__t001",
                f"community__{CH_SPLIT}__index__t002",
            ),
            "source_markdown_combines_heading_and_prose",
        ),
        ManualAlignmentOverrideV1(
            "merge-wrapper",
            CH_MERGE,
            (
                f"{CH_MERGE}_index_b001",
                f"{CH_MERGE}_index_b002",
                f"{CH_MERGE}_index_b003",
            ),
            (f"community__{CH_MERGE}__index__t001",),
            "target_markdown_combines_wrapper_span",
        ),
    ]


def _decision(inputs: D2LCommunityFiveChapterInputsV1) -> dict:
    return build_d2l_community_manual_decision(
        inputs,
        _overrides(),
        decision_id="manual-decision-v1",
        created_at="2026-07-23T00:00:00Z",
        reviewer_kind="ai_assisted_manual",
        reviewer_id="CodeX",
    )


def _review_manifests(
    inputs: D2LCommunityFiveChapterInputsV1, decision: dict
) -> dict[str, dict]:
    return {
        chapter_id: build_d2l_community_chapter_review_manifest(
            inputs,
            decision,
            chapter_id=chapter_id,
            manifest_id=f"{chapter_id}-review-v1",
            created_at="2026-07-23T00:00:00Z",
            producer_code_commit="c" * 40,
            implementation_commit="d" * 40,
        )
        for chapter_id in inputs.chapter_order
    }


def test_manual_split_merge_exact_cover_and_audit_finalize(tmp_path: Path) -> None:
    inputs = _inputs(tmp_path)
    decision = _decision(inputs)
    review = _review_manifests(inputs, decision)

    assert review[CH_SPLIT]["coverage"] == {
        "source_block_count": 36,
        "target_segment_count": 37,
        "accepted_mapping_count": 1,
        "review_mapping_count": 35,
        "ambiguous_mapping_count": 0,
        "missing_mapping_count": 0,
        "added_mapping_count": 0,
        "accepted_source_block_count": 1,
        "review_source_block_count": 35,
        "ambiguous_source_block_count": 0,
        "missing_source_block_count": 0,
        "accepted_target_segment_count": 2,
        "review_target_segment_count": 35,
        "ambiguous_target_segment_count": 0,
        "added_target_segment_count": 0,
    }
    assert review[CH_MERGE]["mappings"][0]["mapping_kind"] == "N:1"

    plan = build_d2l_community_alignment_audit_plan(
        inputs,
        review,
        plan_id="audit-plan-v1",
        created_at="2026-07-23T00:00:00Z",
    )
    assert plan["population_count"] == 70
    assert plan["sample_count"] == 30
    record = record_d2l_community_alignment_audit(
        plan,
        {row["mapping_id"]: True for row in plan["selections"]},
        record_id="audit-record-v1",
        created_at="2026-07-23T00:10:00Z",
        reviewer_kind="ai_assisted_manual",
        reviewer_id="CodeX",
    )
    finalized = apply_d2l_community_alignment_audit(inputs, review, plan, record)
    assert all(
        manifest["coverage"]["review_mapping_count"] == 0
        for manifest in finalized.values()
    )
    assert sum(
        manifest["coverage"]["accepted_source_block_count"]
        for manifest in finalized.values()
    ) == 74


def test_failed_sample_keeps_its_complete_section_review_held(tmp_path: Path) -> None:
    inputs = _inputs(tmp_path)
    review = _review_manifests(inputs, _decision(inputs))
    plan = build_d2l_community_alignment_audit_plan(
        inputs,
        review,
        plan_id="audit-plan-v1",
        created_at="2026-07-23T00:00:00Z",
    )
    outcomes = {row["mapping_id"]: True for row in plan["selections"]}
    failed = next(
        row for row in plan["selections"] if row["chapter_id"] == CH_SPLIT
    )
    outcomes[failed["mapping_id"]] = False
    record = record_d2l_community_alignment_audit(
        plan,
        outcomes,
        record_id="audit-record-v1",
        created_at="2026-07-23T00:10:00Z",
        reviewer_kind="human",
        reviewer_id="reviewer-1",
    )

    finalized = apply_d2l_community_alignment_audit(inputs, review, plan, record)

    assert finalized[CH_SPLIT]["coverage"]["review_mapping_count"] == 35
    assert finalized[CH_MERGE]["coverage"]["review_mapping_count"] == 0


def test_manual_decision_tamper_and_foreign_anchor_fail_closed(
    tmp_path: Path,
) -> None:
    inputs = _inputs(tmp_path)
    decision = _decision(inputs)
    tampered = copy.deepcopy(decision)
    tampered["overrides"][0]["target_segment_ids"].reverse()
    with pytest.raises(ContractValidationError, match="decision_hash"):
        validate_d2l_community_manual_decision(tampered)

    foreign = _overrides()
    foreign[0] = ManualAlignmentOverrideV1(
        foreign[0].override_id,
        foreign[0].chapter_id,
        foreign[0].source_block_ids,
        (
            f"community__{CH_SPLIT}__index__t002",
            f"community__{CH_SPLIT}__index__t003",
        ),
        foreign[0].reason_code,
    )
    wrong = build_d2l_community_manual_decision(
        inputs,
        foreign,
        decision_id="manual-decision-v2",
        created_at="2026-07-23T00:00:00Z",
        reviewer_kind="human",
        reviewer_id="reviewer-1",
    )
    with pytest.raises(D2LCommunityFiveChapterError, match="manual_anchor_order"):
        build_d2l_community_chapter_review_manifest(
            inputs,
            wrong,
            chapter_id=CH_SPLIT,
            manifest_id="bad-review-v1",
            created_at="2026-07-23T00:00:00Z",
            producer_code_commit="c" * 40,
            implementation_commit="d" * 40,
        )


def test_audit_record_requires_exact_sample_cover_and_valid_hash(
    tmp_path: Path,
) -> None:
    inputs = _inputs(tmp_path)
    review = _review_manifests(inputs, _decision(inputs))
    plan = build_d2l_community_alignment_audit_plan(
        inputs,
        review,
        plan_id="audit-plan-v1",
        created_at="2026-07-23T00:00:00Z",
    )
    outcomes = {row["mapping_id"]: True for row in plan["selections"]}
    outcomes.pop(next(iter(outcomes)))
    with pytest.raises(D2LCommunityFiveChapterError, match="audit_exact_cover"):
        record_d2l_community_alignment_audit(
            plan,
            outcomes,
            record_id="audit-record-v1",
            created_at="2026-07-23T00:10:00Z",
            reviewer_kind="human",
            reviewer_id="reviewer-1",
        )

    valid_outcomes = {
        row["mapping_id"]: True for row in plan["selections"]
    }
    record = record_d2l_community_alignment_audit(
        plan,
        valid_outcomes,
        record_id="audit-record-v1",
        created_at="2026-07-23T00:10:00Z",
        reviewer_kind="human",
        reviewer_id="reviewer-1",
    )
    record["outcomes"][0]["alignment_ok"] = False
    with pytest.raises(ContractValidationError, match="audit_summary"):
        validate_d2l_community_alignment_audit_record(record)


def test_content_addressed_bundle_is_deterministic_and_keeps_target_text(
    tmp_path: Path,
) -> None:
    inputs = _inputs(tmp_path / "input")
    decision = _decision(inputs)
    review = _review_manifests(inputs, decision)
    plan = build_d2l_community_alignment_audit_plan(
        inputs,
        review,
        plan_id="audit-plan-v1",
        created_at="2026-07-23T00:00:00Z",
    )
    record = record_d2l_community_alignment_audit(
        plan,
        {row["mapping_id"]: True for row in plan["selections"]},
        record_id="audit-record-v1",
        created_at="2026-07-23T00:10:00Z",
        reviewer_kind="ai_assisted_manual",
        reviewer_id="CodeX",
    )
    finalized = apply_d2l_community_alignment_audit(inputs, review, plan, record)

    first = write_d2l_community_alignment_bundle(
        output_parent=tmp_path / "output",
        inputs=inputs,
        manual_decision=decision,
        audit_plan=plan,
        audit_record=record,
        chapter_manifests=finalized,
        created_at="2026-07-23T00:20:00Z",
    )
    second = write_d2l_community_alignment_bundle(
        output_parent=tmp_path / "output",
        inputs=inputs,
        manual_decision=decision,
        audit_plan=plan,
        audit_record=record,
        chapter_manifests=finalized,
        created_at="2026-07-23T00:20:00Z",
    )

    assert first == second
    bundle = json.loads((first / "alignment_bundle.json").read_text("utf-8"))
    assert first.name == bundle["integrity"]["bundle_sha256"]
    assert bundle["status"] == "accepted_alignment"
    snapshot = json.loads(
        (
            first
            / "chapters"
            / CH_SPLIT
            / "target_snapshot.json"
        ).read_text("utf-8")
    )
    assert snapshot["snapshot"]["segments"][0]["text"] == "# Tiêu đề"
    assert snapshot["snapshot"]["segments"][1]["text"] == "Giới thiệu."


def _write_real_loader_fixture(tmp_path: Path) -> tuple[Path, Path, str]:
    project = tmp_path / "project"
    candidate = project / "working" / "source_package_candidates" / (
        "srcpkg_" + "a" * 64
    )
    candidate.mkdir(parents=True)
    community = tmp_path / "community"
    split_root, split_blocks = _write_split_chapter(community)

    document = {
        "schema_version": "1.5.0",
        "doc_id": "fixture-doc",
        "metadata": {},
        "chapters": [
            {
                "chapter_id": CH_SPLIT,
                "order_index": 0,
                "title": "Split",
                "blocks": [
                    {
                        "annotations": {},
                        "block_id": block.block_id,
                        "block_type": (
                            "heading" if block.block_type == "heading" else "paragraph"
                        ),
                        "clean_text": block.source_text,
                        "is_chapter_opening": index == 0,
                        "order_index": index,
                        "page_ids": [],
                        "quality_flags": [],
                        "sentences": [],
                        "source_text": block.source_text,
                    }
                    for index, block in enumerate(split_blocks)
                ],
            }
        ],
    }
    structure = {"schema_version": "fixture-structure-v1", "rows": []}
    assets = {"schema_version": "fixture-assets-v1", "assets": []}
    document_sha = canonical_json_sha256(document)
    structure_sha = canonical_json_sha256(structure)
    assets_sha = canonical_json_sha256(assets)
    projection_body = {
        "schema_version": "admitted_projection_v1",
        "doc_id": "fixture-doc",
        "inputs": {
            "document": {"sha256": document_sha},
            "structure": {"sha256": structure_sha},
            "asset_manifest": {"sha256": assets_sha},
        },
        "policy": {},
        "rows": [
            {
                "block_id": block.block_id,
                "chapter_id": CH_SPLIT,
                "channel": "semantic_text",
            }
            for block in split_blocks
        ],
    }
    projection = {
        **projection_body,
        "integrity": {
            "payload_sha256": canonical_json_sha256(projection_body),
            "row_count": len(split_blocks),
        },
    }
    projection_sha = canonical_json_sha256(projection)
    for name, value in (
        ("document.json", document),
        ("structure_manifest.json", structure),
        ("asset_manifest.json", assets),
        ("admitted_projection_v1.json", projection),
    ):
        (candidate / name).write_text(
            json.dumps(value, ensure_ascii=False, sort_keys=True),
            encoding="utf-8",
            newline="\n",
        )
    finalization_body = {
        "schema_version": "source_package_finalization_v1",
        "lifecycle": "finalized_pre_run",
        "doc_id": "fixture-doc",
        "candidate": {
            "candidate_id": "srcpkg_" + "a" * 64,
            "file_count": 4,
            "relative_path": candidate.relative_to(project).as_posix(),
            "tree_sha256": "a" * 64,
        },
        "package": {
            "document": {"schema_version": "1.5.0", "sha256": document_sha},
            "structure": {
                "schema_version": "fixture-structure-v1",
                "sha256": structure_sha,
            },
            "asset_manifest": {
                "schema_version": "fixture-assets-v1",
                "sha256": assets_sha,
            },
            "admitted_projection": {
                "schema_version": "admitted_projection_v1",
                "sha256": projection_sha,
            },
        },
        "policies": {
            "admission": {
                "policy_id": "canonical_source_admission",
                "policy_version": "1.0.0",
                "policy_sha256": "f" * 64,
            }
        },
    }
    finalization = {
        **finalization_body,
        "integrity": {
            "payload_sha256": canonical_json_sha256(finalization_body)
        },
    }
    finalization_dir = project / "working" / "source_package_finalizations"
    finalization_dir.mkdir(parents=True)
    finalization_path = finalization_dir / "srcfin_fixture.json"
    finalization_path.write_text(
        json.dumps(finalization, ensure_ascii=False, sort_keys=True),
        encoding="utf-8",
        newline="\n",
    )
    assert split_root == community / "chapter_split"
    subprocess.run(["git", "init", str(community)], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(community), "config", "user.name", "CodeX"],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        [
            "git",
            "-C",
            str(community),
            "config",
            "user.email",
            "codex@example.invalid",
        ],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(community), "add", "chapter_split"],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(community), "commit", "-m", "fixture"],
        check=True,
        capture_output=True,
    )
    repository_commit = subprocess.run(
        ["git", "-C", str(community), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    ).stdout.strip()
    return finalization_path, community, repository_commit


def test_finalized_source_loader_binds_components_and_rejects_drift(
    tmp_path: Path,
) -> None:
    finalization, community, repository_commit = _write_real_loader_fixture(tmp_path)
    loaded = load_d2l_community_five_chapter_inputs(
        finalization_path=finalization,
        community_repository_root=community,
        repository_commit=repository_commit,
        chapter_directories={CH_SPLIT: "chapter_split"},
        chapter_order=[CH_SPLIT],
    )
    assert len(loaded.common_by_chapter[CH_SPLIT].blocks) == 36
    assert len(loaded.target_by_chapter[CH_SPLIT].snapshot.segments) == 37

    with pytest.raises(D2LCommunityFiveChapterError, match="community_commit"):
        load_d2l_community_five_chapter_inputs(
            finalization_path=finalization,
            community_repository_root=community,
            repository_commit=COMMIT,
            chapter_directories={CH_SPLIT: "chapter_split"},
            chapter_order=[CH_SPLIT],
        )

    community_target = community / "chapter_split" / "index.md"
    target_bytes = community_target.read_bytes()
    community_target.write_bytes(target_bytes + b"\n")
    with pytest.raises(D2LCommunityFiveChapterError, match="community_worktree"):
        load_d2l_community_five_chapter_inputs(
            finalization_path=finalization,
            community_repository_root=community,
            repository_commit=repository_commit,
            chapter_directories={CH_SPLIT: "chapter_split"},
            chapter_order=[CH_SPLIT],
        )
    community_target.write_bytes(target_bytes)

    payload = json.loads(finalization.read_text("utf-8"))
    candidate = finalization.parents[2] / payload["candidate"]["relative_path"]
    document_path = candidate / "document.json"
    document = json.loads(document_path.read_text("utf-8"))
    document["chapters"][0]["blocks"][0]["clean_text"] += " drift"
    document_path.write_text(
        json.dumps(document, ensure_ascii=False, sort_keys=True),
        encoding="utf-8",
        newline="\n",
    )
    with pytest.raises(D2LCommunityFiveChapterError, match="component_hash"):
        load_d2l_community_five_chapter_inputs(
            finalization_path=finalization,
            community_repository_root=community,
            repository_commit=repository_commit,
            chapter_directories={CH_SPLIT: "chapter_split"},
            chapter_order=[CH_SPLIT],
        )
