from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path

import pytest

from pipeline.eval.benchmark_v1 import (
    BENCHMARK_ARM_IDS_V1,
    BENCHMARK_CHAPTER_IDS_V1,
    augment_common_input_with_benchmark_overlays_v1,
    build_benchmark_manifest_v1,
    build_benchmark_preflight_v1,
    build_marked_markdown_overlay_v1,
    build_overlay_from_common_arm_v1,
    build_review_held_overlay_v1,
    persist_benchmark_bundle_v1,
    source_read_model_sha256_v1,
    validate_benchmark_manifest_v1,
    validate_benchmark_overlay_v1,
    validate_benchmark_preflight_v1,
)
from pipeline.eval.common_input_v1 import (
    AdmissionPolicyIdentityV1,
    CanonicalComponentIdentityV1,
    CanonicalProjectionIdentityV1,
    CanonicalSourcePackageBindingV1,
    CommonArmV1,
    CommonBlockV1,
    CommonEvaluationInputV1,
    CommonSourceSnapshotV1,
    CommonTranslationV1,
    LegacyD2LSourceBindingV1,
)
from pipeline.eval.contracts_v1 import ContractValidationError


NOW = "2026-07-21T15:00:00Z"
COMMIT = "c" * 40
SOURCE_DB = "1" * 64


def _sources() -> list[CommonSourceSnapshotV1]:
    result = []
    order = 10
    for chapter_id in BENCHMARK_CHAPTER_IDS_V1:
        blocks = (
            CommonBlockV1(f"{chapter_id}_b001", chapter_id, order, "heading", f"# {chapter_id}", "translate"),
            CommonBlockV1(f"{chapter_id}_b002", chapter_id, order + 1, "code", "x = 1", "preserve"),
        )
        result.append(
            CommonSourceSnapshotV1(
                source_schema_id="D2LEvaluationInputV1",
                source_schema_version="1.0.0",
                source_binding=LegacyD2LSourceBindingV1("d2l", "d2l", SOURCE_DB, hashlib.sha256(chapter_id.encode()).hexdigest()),
                blocks=blocks,
            )
        )
        order += 2
    return result


def _evidence(sources: list[CommonSourceSnapshotV1]) -> list[dict]:
    return [
        {
            "chapter_id": source.blocks[0].chapter_id,
            "source_artifact_id": f"source-{index}",
            "source_artifact_sha256": hashlib.sha256(f"source-{index}".encode()).hexdigest(),
            "source_evidence_kind": "d2l_evaluation_package" if index == 2 else "google_translate_source_input",
        }
        for index, source in enumerate(sources)
    ]


def _canonical_sources() -> list[CommonSourceSnapshotV1]:
    binding = CanonicalSourcePackageBindingV1(
        project_id="d2l",
        document_id="d2l",
        document=CanonicalComponentIdentityV1("1.5.0", "1" * 64),
        structure=CanonicalComponentIdentityV1("structure-v1", "2" * 64),
        asset_manifest=CanonicalComponentIdentityV1(
            "canonical_asset_manifest_v1", "3" * 64
        ),
        admitted_projection=CanonicalProjectionIdentityV1(
            "admitted_projection_v1", "4" * 64
        ),
        admission_policy=AdmissionPolicyIdentityV1(
            "canonical_source_admission", "1.0.0", "5" * 64
        ),
    )
    return [
        CommonSourceSnapshotV1(
            source_schema_id="CanonicalSourcePackageV1",
            source_schema_version="1.5.0",
            source_binding=binding,
            blocks=(
                CommonBlockV1(
                    "d2l_preliminaries_b001",
                    "d2l_preliminaries",
                    0,
                    "paragraph",
                    "Canonical source.",
                    "translate",
                ),
                CommonBlockV1(
                    "d2l_preliminaries_b002",
                    "d2l_preliminaries",
                    1,
                    "code",
                    "x = 1",
                    "preserve",
                ),
            ),
        )
    ]


def _manifest(sources: list[CommonSourceSnapshotV1]) -> dict:
    return build_benchmark_manifest_v1(
        sources,
        _evidence(sources),
        benchmark_id="d2l-five-chapter-v1",
        created_at=NOW,
        producer_code_commit=COMMIT,
    )


def _common(source: CommonSourceSnapshotV1) -> CommonEvaluationInputV1:
    arms = []
    rows = []
    for arm_id in ("S0", "S1"):
        arms.append(CommonArmV1(f"artifact-{arm_id}", hashlib.sha256(arm_id.encode()).hexdigest(), "run", "attempt", arm_id, "profile", "2" * 64, "en", "vi"))
        rows.extend(
            [
                CommonTranslationV1(arm_id, source.blocks[0].block_id, "translated", f"Dich {arm_id}", None),
                CommonTranslationV1(arm_id, source.blocks[1].block_id, "preserved", source.blocks[1].source_text, None),
            ]
        )
    return CommonEvaluationInputV1(source.source_schema_id, source.source_schema_version, source.source_binding, source.blocks, tuple(arms), tuple(rows))


def _all_ready_overlays(sources: list[CommonSourceSnapshotV1]) -> list[dict]:
    result = []
    roles = dict((arm_id, role) for arm_id, role, _ in (
        ("S0", "pipeline_ablation", ""),
        ("S1", "thesis_system", ""),
        ("community", "human_community", ""),
        ("google_nmt", "conventional_nmt", ""),
        ("llm_lc", "long_context_diagnostic", ""),
    ))
    for source in sources:
        common = _common(source)
        result.extend(
            build_overlay_from_common_arm_v1(common, chapter_id=source.blocks[0].chapter_id, arm_id=arm_id, benchmark_role=roles[arm_id], created_at=NOW, producer_code_commit=COMMIT)
            for arm_id in ("S0", "S1")
        )
        marked = "".join(
            f"[[B{number:04d}]]\n{('# Dich' if number == source.blocks[0].order_index + 1 else 'x = 1')}\n"
            for number in range(1, source.blocks[-1].order_index + 2)
        ).encode()
        llm = build_marked_markdown_overlay_v1(
            source,
            marked,
            created_at=NOW,
            producer_code_commit=COMMIT,
            model_profile_id="gpt-web",
            model_profile_sha256="3" * 64,
            logical_run_id="web-run",
            attempt_run_id="web-attempt",
        )
        result.append(llm)
        for arm_id, role in (("google_nmt", "conventional_nmt"), ("community", "human_community")):
            clone = copy.deepcopy(llm)
            clone["overlay_id"] = f"overlay-{arm_id}-{source.blocks[0].chapter_id}"
            clone["arm"].update(
                {
                    "arm_id": arm_id,
                    "benchmark_role": role,
                    "origin_kind": "community_alignment" if arm_id == "community" else "google_translate_capture",
                    "evidence_artifact_id": f"evidence-{arm_id}",
                }
            )
            clone["integrity"]["overlay_sha256"] = "0" * 64
            from pipeline.eval.benchmark_v1 import _OVERLAY_POLICY
            from pipeline.eval.contracts_v1 import seal_payload
            clone = seal_payload(clone, policy=_OVERLAY_POLICY, hash_path=("integrity", "overlay_sha256"))
            result.append(validate_benchmark_overlay_v1(clone))
    return result


def test_manifest_is_closed_self_hashed_and_does_not_merge_chapter_manifests() -> None:
    sources = _sources()
    manifest = _manifest(sources)
    assert manifest["scope"]["block_count"] == 10
    assert [row["runtime_manifest_sha256"] for row in manifest["chapters"]] == [source.source_binding.runtime_manifest_sha256 for source in sources]
    assert len(set(row["runtime_manifest_sha256"] for row in manifest["chapters"])) == 5
    changed = copy.deepcopy(manifest)
    changed["unknown"] = True
    with pytest.raises(ContractValidationError, match="unknown_keys"):
        validate_benchmark_manifest_v1(changed)
    changed = copy.deepcopy(manifest)
    changed["chapters"][0]["block_count"] += 1
    with pytest.raises(ContractValidationError, match="block count drift"):
        validate_benchmark_manifest_v1(changed)


def test_marked_markdown_keeps_full_evidence_hash_and_detects_preserve_violation() -> None:
    source = _sources()[0]
    text = "".join(
        f"[[B{number:04d}]]\n{('Dich heading' if number == 11 else 'changed code' if number == 12 else 'outside')}\n"
        for number in range(1, 21)
    ).encode()
    overlay = build_marked_markdown_overlay_v1(
        source,
        text,
        created_at=NOW,
        producer_code_commit=COMMIT,
        model_profile_id="gpt-web",
        model_profile_sha256="4" * 64,
        logical_run_id="full-book",
        attempt_run_id="full-book-attempt",
    )
    assert overlay["arm"]["evidence_sha256"] == hashlib.sha256(text).hexdigest()
    assert [row["alignment_status"] for row in overlay["rows"]] == ["aligned", "aligned"]
    assert overlay["rows"][1]["issue_codes"] == ["preserve_violation"]
    assert overlay["coverage"]["preserve_violation_count"] == 1

    overlays = _all_ready_overlays(_sources())
    overlays = [
        overlay if row["arm"]["arm_id"] == "llm_lc" and row["source"]["chapter_id"] == source.blocks[0].chapter_id else row
        for row in overlays
    ]
    report = build_benchmark_preflight_v1(
        _manifest(_sources()),
        _sources(),
        overlays,
        created_at=NOW,
        producer_code_commit=COMMIT,
    )
    assert report["status"] == "ready"


def test_partial_marked_capture_cannot_cover_a_later_selected_chapter() -> None:
    sources = _sources()
    source = sources[-1]
    partial = "".join(
        f"[[B{number:04d}]]\nEarlier material\n" for number in range(1, 16)
    ).encode()
    partial_overlay = build_marked_markdown_overlay_v1(
        source,
        partial,
        created_at=NOW,
        producer_code_commit=COMMIT,
        model_profile_id="gpt-web",
        model_profile_sha256="4" * 64,
        logical_run_id="partial-web-run",
        attempt_run_id="partial-web-attempt",
    )
    assert partial_overlay["coverage"]["missing_count"] == len(source.blocks)

    overlays = [
        partial_overlay
        if row["arm"]["arm_id"] == "llm_lc"
        and row["source"]["chapter_id"] == source.blocks[0].chapter_id
        else row
        for row in _all_ready_overlays(sources)
    ]
    report = build_benchmark_preflight_v1(
        _manifest(sources),
        sources,
        overlays,
        created_at=NOW,
        producer_code_commit=COMMIT,
    )
    assert report["status"] == "blocked"
    assert any(
        row["code"] == "nonready_rows"
        and row["arm_id"] == "llm_lc"
        and row["chapter_id"] == source.blocks[0].chapter_id
        for row in report["blockers"]
    )


def test_preflight_blocks_missing_and_review_held_arms_without_calling_scorers() -> None:
    sources = _sources()
    manifest = _manifest(sources)
    overlays = []
    for source in sources:
        common = _common(source)
        overlays.extend(
            build_overlay_from_common_arm_v1(common, chapter_id=source.blocks[0].chapter_id, arm_id=arm_id, benchmark_role="pipeline_ablation" if arm_id == "S0" else "thesis_system", created_at=NOW, producer_code_commit=COMMIT)
            for arm_id in ("S0", "S1")
        )
        overlays.append(
            build_review_held_overlay_v1(
                source,
                arm_id="community",
                benchmark_role="human_community",
                evidence_artifact_id="community-tree",
                evidence_sha256="5" * 64,
                origin_kind="community_repository_pending",
                created_at=NOW,
                producer_code_commit=COMMIT,
            )
        )
    report = build_benchmark_preflight_v1(manifest, sources, overlays, created_at=NOW, producer_code_commit=COMMIT)
    assert report["status"] == "blocked"
    assert report["coverage"] == {
        "expected_chapter_count": 5,
        "expected_arm_count": 5,
        "expected_arm_chapter_count": 25,
        "ready_arm_chapter_count": 10,
        "blocker_count": 15,
    }
    assert {row["code"] for row in report["blockers"]} == {"missing_overlay", "nonready_rows"}


def test_preflight_ready_requires_all_25_arm_chapter_cells() -> None:
    sources = _sources()
    manifest = _manifest(sources)
    overlays = _all_ready_overlays(sources)
    report = build_benchmark_preflight_v1(manifest, sources, overlays, created_at=NOW, producer_code_commit=COMMIT)
    assert report["status"] == "ready"
    assert report["coverage"]["ready_arm_chapter_count"] == 25
    assert report["blockers"] == []
    validate_benchmark_preflight_v1(report)


def test_overlay_block_order_drift_fails_before_preflight() -> None:
    source = _sources()[0]
    overlay = build_overlay_from_common_arm_v1(_common(source), chapter_id=source.blocks[0].chapter_id, arm_id="S0", benchmark_role="pipeline_ablation", created_at=NOW, producer_code_commit=COMMIT)
    changed = copy.deepcopy(overlay)
    changed["rows"].reverse()
    changed["integrity"]["overlay_sha256"] = "0" * 64
    from pipeline.eval.benchmark_v1 import _OVERLAY_POLICY
    from pipeline.eval.contracts_v1 import seal_payload
    changed = seal_payload(changed, policy=_OVERLAY_POLICY, hash_path=("integrity", "overlay_sha256"))
    with pytest.raises(ContractValidationError, match="block_exact_cover"):
        build_benchmark_preflight_v1(_manifest(_sources()), _sources(), [changed], created_at=NOW, producer_code_commit=COMMIT)


def test_augment_common_input_adds_private_projection_without_public_authority() -> None:
    source = _sources()[0]
    common = _common(source)
    held = build_review_held_overlay_v1(
        source,
        arm_id="community",
        benchmark_role="human_community",
        evidence_artifact_id="community-tree",
        evidence_sha256="6" * 64,
        origin_kind="community_repository_pending",
        created_at=NOW,
        producer_code_commit=COMMIT,
    )
    augmented = augment_common_input_with_benchmark_overlays_v1(common, [held])
    assert [row.arm_id for row in augmented.arms] == ["S0", "S1", "community"]
    assert [row.status for row in augmented.translations if row.arm_id == "community"] == ["review_held", "review_held"]
    assert held["authority"]["public_translation_artifact"] is False


def test_persist_is_immutable_and_input_objects_are_not_mutated(tmp_path: Path) -> None:
    sources = _sources()
    manifest = _manifest(sources)
    overlays = _all_ready_overlays(sources)
    report = build_benchmark_preflight_v1(manifest, sources, overlays, created_at=NOW, producer_code_commit=COMMIT)
    before = copy.deepcopy((manifest, overlays, report))
    root = persist_benchmark_bundle_v1(tmp_path / "bundle", manifest=manifest, overlays=overlays, preflight=report)
    persist_benchmark_bundle_v1(root, manifest=manifest, overlays=overlays, preflight=report)
    assert (manifest, overlays, report) == before
    assert (root / "benchmark_manifest.json").is_file()
    path = root / "preflight.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    data["status"] = "blocked"
    path.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(ContractValidationError):
        persist_benchmark_bundle_v1(root, manifest=manifest, overlays=overlays, preflight=report)


def test_source_read_model_hash_changes_with_one_source_byte() -> None:
    source = _sources()[0]
    changed = copy.deepcopy(source)
    changed = CommonSourceSnapshotV1(
        changed.source_schema_id,
        changed.source_schema_version,
        changed.source_binding,
        (CommonBlockV1(changed.blocks[0].block_id, changed.blocks[0].chapter_id, changed.blocks[0].order_index, changed.blocks[0].block_type, changed.blocks[0].source_text + "!", changed.blocks[0].admission), changed.blocks[1]),
    )
    assert source_read_model_sha256_v1(source) != source_read_model_sha256_v1(changed)


def test_manifest_and_preflight_accept_registered_bounded_selection() -> None:
    sources = _sources()[2:4]
    chapter_ids = tuple(source.blocks[0].chapter_id for source in sources)
    arm_ids = ("S0", "S1", "google_nmt")
    manifest = build_benchmark_manifest_v1(
        sources,
        _evidence(sources),
        benchmark_id="d2l-bounded-selection-v1",
        created_at=NOW,
        producer_code_commit=COMMIT,
        selected_chapter_ids=chapter_ids,
        selected_arm_ids=arm_ids,
    )
    overlays = [
        row
        for row in _all_ready_overlays(sources)
        if row["arm"]["arm_id"] in arm_ids
    ]
    preflight = build_benchmark_preflight_v1(
        manifest,
        sources,
        overlays,
        created_at=NOW,
        producer_code_commit=COMMIT,
    )

    assert manifest["scope"]["benchmark_kind"] == "bounded_registered_selection_d2l_v1"
    assert [row["chapter_id"] for row in manifest["chapters"]] == list(chapter_ids)
    assert [row["arm_id"] for row in manifest["arm_contracts"]] == list(arm_ids)
    assert preflight["status"] == "ready"
    assert preflight["coverage"] == {
        "expected_chapter_count": 2,
        "expected_arm_count": 3,
        "expected_arm_chapter_count": 6,
        "ready_arm_chapter_count": 6,
        "blocker_count": 0,
    }


def test_canonical_manifest_and_overlay_keep_explicit_source_binding() -> None:
    sources = _canonical_sources()
    manifest = build_benchmark_manifest_v1(
        sources,
        [
            {
                "chapter_id": "d2l_preliminaries",
                "source_artifact_id": "canonical-source-package",
                "source_artifact_sha256": "a" * 64,
                "source_evidence_kind": "canonical_source_package_v1",
            }
        ],
        benchmark_id="canonical-benchmark",
        created_at=NOW,
        producer_code_commit=COMMIT,
        selected_chapter_ids=("d2l_preliminaries",),
        selected_arm_ids=("S0", "S1"),
    )
    overlay = build_overlay_from_common_arm_v1(
        _common(sources[0]),
        chapter_id="d2l_preliminaries",
        arm_id="S0",
        benchmark_role="pipeline_ablation",
        created_at=NOW,
        producer_code_commit=COMMIT,
    )

    assert manifest["schema_version"] == "1.2.0"
    assert manifest["scope"]["source_binding"]["binding_kind"] == (
        "canonical_source_package_v1"
    )
    assert "source_db_sha256" not in manifest["scope"]
    assert overlay["source"]["source_binding"] == manifest["scope"]["source_binding"]
    assert "source_db_sha256" not in overlay["source"]


def test_canonical_manifest_rejects_mixed_foreign_and_resealed_tamper() -> None:
    canonical = _canonical_sources()[0]
    legacy = _sources()[1]
    with pytest.raises(ContractValidationError, match="source-binding kind"):
        build_benchmark_manifest_v1(
            [canonical, legacy],
            [
                {
                    "chapter_id": "d2l_preliminaries",
                    "source_artifact_id": "canonical",
                    "source_artifact_sha256": "a" * 64,
                    "source_evidence_kind": "canonical_source_package_v1",
                },
                {
                    "chapter_id": "d2l_linear_networks",
                    "source_artifact_id": "legacy",
                    "source_artifact_sha256": "b" * 64,
                    "source_evidence_kind": "d2l_evaluation_package",
                },
            ],
            benchmark_id="mixed",
            created_at=NOW,
            producer_code_commit=COMMIT,
            selected_chapter_ids=(
                "d2l_preliminaries",
                "d2l_linear_networks",
            ),
            selected_arm_ids=("S0", "S1"),
        )

    manifest = build_benchmark_manifest_v1(
        [canonical],
        [
            {
                "chapter_id": "d2l_preliminaries",
                "source_artifact_id": "canonical",
                "source_artifact_sha256": "a" * 64,
                "source_evidence_kind": "canonical_source_package_v1",
            }
        ],
        benchmark_id="canonical-tamper",
        created_at=NOW,
        producer_code_commit=COMMIT,
        selected_chapter_ids=("d2l_preliminaries",),
        selected_arm_ids=("S0", "S1"),
    )
    from pipeline.eval.benchmark_v1 import _MANIFEST_POLICY
    from pipeline.eval.contracts_v1 import seal_payload

    tampered = copy.deepcopy(manifest)
    tampered["chapters"][0]["source_binding"]["document"]["sha256"] = "f" * 64
    tampered["integrity"]["manifest_sha256"] = "0" * 64
    tampered = seal_payload(
        tampered,
        policy=_MANIFEST_POLICY,
        hash_path=("integrity", "manifest_sha256"),
    )
    with pytest.raises(ContractValidationError, match="canonical source package"):
        validate_benchmark_manifest_v1(tampered)

    relabeled = copy.deepcopy(manifest)
    relabeled["scope"].pop("source_binding")
    relabeled["scope"]["source_db_sha256"] = "1" * 64
    relabeled["chapters"][0].pop("source_binding")
    relabeled["chapters"][0]["source_db_sha256"] = "1" * 64
    relabeled["chapters"][0]["runtime_manifest_sha256"] = "2" * 64
    relabeled["integrity"]["manifest_sha256"] = "0" * 64
    relabeled = seal_payload(
        relabeled,
        policy=_MANIFEST_POLICY,
        hash_path=("integrity", "manifest_sha256"),
    )
    relabeled = validate_benchmark_manifest_v1(relabeled)
    with pytest.raises(ContractValidationError, match="source drift"):
        build_benchmark_preflight_v1(
            relabeled,
            [canonical],
            [],
            created_at=NOW,
            producer_code_commit=COMMIT,
        )
