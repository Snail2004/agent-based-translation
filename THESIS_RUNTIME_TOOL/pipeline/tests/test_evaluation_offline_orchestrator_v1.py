from __future__ import annotations

import copy
import json
from collections import Counter
from pathlib import Path

import pytest

from pipeline.eval.common_input_v1 import (
    AdmissionPolicyIdentityV1,
    CanonicalComponentIdentityV1,
    CanonicalProjectionIdentityV1,
    CanonicalSourcePackageBindingV1,
    CommonBlockV1,
    CommonEvaluationInputV1,
    CommonSourceSnapshotV1,
    build_common_evaluation_input,
    project_d2l_source_snapshot,
    seal_translation_artifact,
    source_binding_to_dict,
)
from pipeline.eval.contracts_v1 import ContractValidationError
from pipeline.eval.offline_orchestrator_v1 import (
    build_evaluation_plan,
    evaluation_plan_to_dict,
    seal_evaluation_run_config,
    validate_evaluation_run_config,
)


FIXTURES = Path(__file__).parent / "fixtures" / "evaluation_v1"


def _load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def _artifact(
    source: CommonSourceSnapshotV1,
    *,
    arm_id: str,
    statuses: dict[str, str] | None = None,
) -> dict:
    statuses = statuses or {}
    rows = []
    for block in source.blocks:
        status = statuses.get(
            block.block_id,
            {
                "translate": "translated",
                "translate_structured": "translated",
                "preserve": "preserved",
                "exclude": "excluded",
                "review_required": "review_held",
            }[block.admission],
        )
        rows.append(
            {
                "block_id": block.block_id,
                "status": status,
                "target_text": (
                    f"translated::{arm_id}::{block.block_id}"
                    if status == "translated"
                    else block.source_text if status == "preserved" else None
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
            "created_at": "2026-07-17T00:00:00Z",
            "producer": {
                "workstream": "d2l",
                "component": "fixture_translation_writer",
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


def _config(
    common: CommonEvaluationInputV1,
    *,
    pairwise: bool = False,
    comparison_pairs: list[dict[str, str]] | None = None,
    before: int = 1,
    after: int = 1,
) -> dict:
    methods = [
        {
            "method_id": "sf_qe",
            "method_version": "1.0.0",
            "scorer_kind": "unary",
            "profile_scope": "common",
            "eligible_admissions": ["translate", "translate_structured"],
        }
    ]
    if pairwise:
        methods.append(
            {
                "method_id": "pj",
                "method_version": "1.0.0",
                "scorer_kind": "pairwise",
                "profile_scope": "common",
                "eligible_admissions": ["translate", "translate_structured"],
            }
        )
    payload = {
        "schema_id": "EvaluationRunConfigV1",
        "schema_version": "1.0.0",
        "config_id": "config-test",
        "created_at": "2026-07-17T00:00:00Z",
        "producer": {
            "workstream": "evaluation",
            "component": "offline_orchestrator_test",
            "component_version": "1.0.0",
            "code_commit": "b" * 40,
        },
        "input_binding": {
            "source_schema_id": common.source_schema_id,
            "source_schema_version": common.source_schema_version,
            "source_binding": source_binding_to_dict(common.source_binding),
            "arm_artifacts": [
                {
                    "arm_id": arm.arm_id,
                    "translation_artifact_id": arm.artifact_id,
                    "translation_artifact_sha256": arm.artifact_sha256,
                    "logical_run_id": arm.logical_run_id,
                    "attempt_run_id": arm.attempt_run_id,
                    "profile_id": arm.profile_id,
                    "profile_config_sha256": arm.profile_config_sha256,
                }
                for arm in common.arms
            ],
        },
        "methods": methods,
        "comparison_pairs": comparison_pairs or [],
        "unit_policy": {
            "unit_kind": "block",
            "context_before_blocks": before,
            "context_after_blocks": after,
        },
        "blinding": {"mode": "opaque_counterbalanced", "seed": "seed-v1"},
        "retry_policy": {"max_transport_attempts": 2},
        "integrity": {"config_sha256": "0" * 64},
    }
    return seal_evaluation_run_config(payload)


def _d2l_common(*, status: str = "translated") -> CommonEvaluationInputV1:
    source = project_d2l_source_snapshot(_load("d2l_input_valid.json"))
    return build_common_evaluation_input(
        source,
        [_artifact(source, arm_id="s1", statuses={"b001": status})],
    )


def _multi_block_source(count: int = 5) -> CommonSourceSnapshotV1:
    return CommonSourceSnapshotV1(
        source_schema_id="FixtureSourceV1",
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
        blocks=tuple(
            CommonBlockV1(
                block_id=f"b{index}",
                chapter_id="ch1",
                order_index=index,
                block_type="paragraph",
                source_text=f"source {index}",
                admission="translate",
            )
            for index in range(count)
        ),
    )


def test_static_config_is_valid_closed_and_immutable():
    config = _load("evaluation_run_config_valid.json")
    before = copy.deepcopy(config)

    validated = validate_evaluation_run_config(config)

    assert config == before
    assert validated["schema_id"] == "EvaluationRunConfigV1"
    assert validated["input_binding"]["arm_artifacts"][0]["attempt_run_id"] == "attempt-s1"

    unknown = copy.deepcopy(config)
    unknown["unit_policy"]["token_budget"] = 1000
    with pytest.raises(ContractValidationError, match="unknown_keys"):
        validate_evaluation_run_config(unknown)


def test_one_arm_plan_is_deterministic_and_has_no_pairwise_work():
    common = _d2l_common()
    config = _load("evaluation_run_config_valid.json")

    first = build_evaluation_plan(common, config)
    second = build_evaluation_plan(common, copy.deepcopy(config))

    assert evaluation_plan_to_dict(first) == evaluation_plan_to_dict(second)
    assert first.plan_sha256 == second.plan_sha256
    assert [unit.block_id for unit in first.units] == ["b001", "b002"]
    assert first.units[0].context_block_ids == ("b001", "b002")
    assert first.units[1].status == "not_applicable"
    assert [(job.scorer_kind, job.status) for job in first.jobs] == [("unary", "ready")]
    assert first.coverage.eligible_unit_count == 1
    assert first.coverage.not_applicable_unit_count == 1


def test_config_binding_rejects_stale_source_artifact_and_attempt_identity():
    common = _d2l_common()

    stale_source = _config(common)
    stale_source["input_binding"]["source_binding"]["source_db_sha256"] = "f" * 64
    stale_source = seal_evaluation_run_config(stale_source)
    with pytest.raises(ContractValidationError, match="input_binding"):
        build_evaluation_plan(common, stale_source)

    stale_attempt = _config(common)
    stale_attempt["input_binding"]["arm_artifacts"][0]["attempt_run_id"] = "other"
    stale_attempt = seal_evaluation_run_config(stale_attempt)
    with pytest.raises(ContractValidationError, match="arm_binding"):
        build_evaluation_plan(common, stale_attempt)

    stale_artifact_id = _config(common)
    stale_artifact_id["input_binding"]["arm_artifacts"][0][
        "translation_artifact_id"
    ] = "other-artifact"
    stale_artifact_id = seal_evaluation_run_config(stale_artifact_id)
    with pytest.raises(ContractValidationError, match="arm_binding"):
        build_evaluation_plan(common, stale_artifact_id)


@pytest.mark.parametrize("status", ["missing", "failed"])
def test_missing_and_failed_translations_create_blocked_jobs_and_coverage(status):
    common = _d2l_common(status=status)
    plan = build_evaluation_plan(common, _config(common))

    assert len(plan.jobs) == 1
    assert plan.jobs[0].status == "blocked"
    assert plan.jobs[0].reason_code == f"translation_{status}"
    assert plan.coverage.ready_job_count == 0
    assert plan.coverage.blocked_job_count == 1
    assert plan.coverage.by_arm[0].unavailable_translation_count == 1


def test_neighbor_context_never_crosses_chapter_and_preserves_order():
    source = CommonSourceSnapshotV1(
        source_schema_id="FixtureSourceV1",
        source_schema_version="1.0.0",
        source_binding=_multi_block_source(1).source_binding,
        blocks=(
            CommonBlockV1("a1", "ch-a", 0, "paragraph", "a1", "translate"),
            CommonBlockV1("a2", "ch-a", 1, "paragraph", "a2", "translate"),
            CommonBlockV1("b1", "ch-b", 0, "paragraph", "b1", "translate"),
            CommonBlockV1("b2", "ch-b", 1, "paragraph", "b2", "translate"),
        ),
    )
    common = build_common_evaluation_input(source, [_artifact(source, arm_id="s1")])
    plan = build_evaluation_plan(common, _config(common, before=4, after=4))

    assert [unit.context_block_ids for unit in plan.units] == [
        ("a1", "a2"),
        ("a1", "a2"),
        ("b1", "b2"),
        ("b1", "b2"),
    ]


def test_pairwise_jobs_are_explicit_and_counterbalanced_with_difference_at_most_one():
    source = _multi_block_source(5)
    common = build_common_evaluation_input(
        source,
        [_artifact(source, arm_id="s0"), _artifact(source, arm_id="s1")],
    )
    config = _config(
        common,
        pairwise=True,
        comparison_pairs=[{"pair_id": "s0-v-s1", "arm_1_id": "s0", "arm_2_id": "s1"}],
    )

    plan = build_evaluation_plan(common, config)
    pairwise_jobs = [job for job in plan.jobs if job.scorer_kind == "pairwise"]
    orientations = Counter(job.presentation_arm_ids for job in pairwise_jobs)

    assert len(pairwise_jobs) == 5
    assert set(orientations) == {("s0", "s1"), ("s1", "s0")}
    assert abs(orientations[("s0", "s1")] - orientations[("s1", "s0")]) <= 1
    assert all(job.status == "ready" for job in pairwise_jobs)


def test_pairwise_method_requires_declared_pairs_and_never_expands_pairs_itself():
    source = _multi_block_source(2)
    common = build_common_evaluation_input(
        source,
        [
            _artifact(source, arm_id="s0"),
            _artifact(source, arm_id="s1"),
            _artifact(source, arm_id="s2"),
        ],
    )
    without_pair = _config(common, pairwise=True)
    with pytest.raises(ContractValidationError, match="comparison_pairs"):
        validate_evaluation_run_config(without_pair)

    one_pair = _config(
        common,
        pairwise=True,
        comparison_pairs=[{"pair_id": "only", "arm_1_id": "s0", "arm_2_id": "s2"}],
    )
    plan = build_evaluation_plan(common, one_pair)
    pairwise_jobs = [job for job in plan.jobs if job.scorer_kind == "pairwise"]
    assert len(pairwise_jobs) == 2
    assert all(set(job.presentation_arm_ids) == {"s0", "s2"} for job in pairwise_jobs)


def test_set_like_config_reordering_keeps_hash_but_context_order_is_semantic():
    source = _multi_block_source(2)
    common = build_common_evaluation_input(
        source, [_artifact(source, arm_id="s0"), _artifact(source, arm_id="s1")]
    )
    config = _config(
        common,
        pairwise=True,
        comparison_pairs=[{"pair_id": "pair", "arm_1_id": "s0", "arm_2_id": "s1"}],
    )
    reordered = copy.deepcopy(config)
    reordered["methods"].reverse()
    reordered["input_binding"]["arm_artifacts"].reverse()
    resealed = seal_evaluation_run_config(reordered)

    assert resealed["integrity"]["config_sha256"] == config["integrity"]["config_sha256"]
    plan = build_evaluation_plan(common, resealed)
    assert plan.units[0].context_block_ids == ("b0", "b1")
    assert plan.units[1].context_block_ids == ("b0", "b1")
