from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any, Mapping

from pipeline.eval.common_input_v1 import (
    CommonEvaluationInputV1,
    CommonTranslationV1,
    SourceBindingV1,
    _validate_source_binding_for_offline_planning,
    source_binding_to_dict,
)
from pipeline.eval.contracts_v1 import (
    CanonicalPolicy,
    ContractValidationError,
    canonical_sha256,
    canonicalize,
    require_enum,
    require_exact_keys,
    require_int,
    require_list,
    require_mapping,
    require_rfc3339,
    require_sha256,
    require_string,
    require_unique,
    seal_payload,
    validate_producer,
    verify_payload_hash,
)


__all__ = [
    "EVALUATION_RUN_CONFIG_SCHEMA_ID",
    "EVALUATION_RUN_CONFIG_SCHEMA_VERSION",
    "EvaluationPlanV1",
    "build_evaluation_plan",
    "evaluation_plan_to_dict",
    "seal_evaluation_run_config",
    "validate_evaluation_run_config",
]


EVALUATION_RUN_CONFIG_SCHEMA_ID = "EvaluationRunConfigV1"
EVALUATION_RUN_CONFIG_SCHEMA_VERSION = "1.0.0"
CONFIG_SELF_HASH_PATH = ("integrity", "config_sha256")

EVALUATION_RUN_CONFIG_POLICY = CanonicalPolicy(
    set_like_paths=frozenset(
        {
            ("input_binding", "arm_artifacts"),
            ("methods",),
            ("methods", "*", "eligible_admissions"),
            ("comparison_pairs",),
        }
    ),
    semantic_sequence_paths=frozenset(),
)

EVALUATION_PLAN_POLICY = CanonicalPolicy(
    set_like_paths=frozenset(
        {
            ("selected_arm_ids",),
            ("units", "*", "eligible_method_ids"),
            ("units", "*", "translation_statuses"),
            ("coverage", "by_arm"),
        }
    ),
    semantic_sequence_paths=frozenset(
        {
            ("units",),
            ("units", "*", "context_block_ids"),
            ("jobs",),
            ("jobs", "*", "presentation_arm_ids"),
        }
    ),
)

_ELIGIBLE_ADMISSIONS = frozenset({"translate", "translate_structured"})


@dataclass(frozen=True, slots=True)
class UnitTranslationStatusV1:
    arm_id: str
    status: str


@dataclass(frozen=True, slots=True)
class EvaluationUnitV1:
    unit_id: str
    block_id: str
    chapter_id: str
    order_index: int
    admission: str
    status: str
    eligible_method_ids: tuple[str, ...]
    context_block_ids: tuple[str, ...]
    translation_statuses: tuple[UnitTranslationStatusV1, ...]


@dataclass(frozen=True, slots=True)
class EvaluationJobV1:
    job_id: str
    method_id: str
    method_version: str
    scorer_kind: str
    unit_id: str
    presentation_arm_ids: tuple[str, ...]
    status: str
    reason_code: str | None


@dataclass(frozen=True, slots=True)
class ArmCoverageV1:
    arm_id: str
    eligible_block_count: int
    ready_translation_count: int
    unavailable_translation_count: int


@dataclass(frozen=True, slots=True)
class EvaluationCoverageV1:
    source_block_count: int
    eligible_unit_count: int
    not_applicable_unit_count: int
    ready_job_count: int
    blocked_job_count: int
    by_arm: tuple[ArmCoverageV1, ...]


@dataclass(frozen=True, slots=True)
class EvaluationPlanV1:
    plan_id: str
    config_id: str
    config_sha256: str
    input_set_sha256: str
    project_id: str
    document_id: str
    source_binding: SourceBindingV1
    selected_arm_ids: tuple[str, ...]
    units: tuple[EvaluationUnitV1, ...]
    jobs: tuple[EvaluationJobV1, ...]
    coverage: EvaluationCoverageV1
    plan_sha256: str


def seal_evaluation_run_config(payload: Mapping[str, Any]) -> dict[str, Any]:
    return seal_payload(
        payload,
        policy=EVALUATION_RUN_CONFIG_POLICY,
        hash_path=CONFIG_SELF_HASH_PATH,
    )


def validate_evaluation_run_config(payload: Mapping[str, Any]) -> dict[str, Any]:
    root = require_mapping(payload, path="$")
    require_exact_keys(
        root,
        required={
            "schema_id",
            "schema_version",
            "config_id",
            "created_at",
            "producer",
            "input_binding",
            "methods",
            "comparison_pairs",
            "unit_policy",
            "blinding",
            "retry_policy",
            "integrity",
        },
        path="$",
    )
    normalized: dict[str, Any] = {
        "schema_id": require_enum(
            root["schema_id"], {EVALUATION_RUN_CONFIG_SCHEMA_ID}, path="$.schema_id"
        ),
        "schema_version": require_enum(
            root["schema_version"],
            {EVALUATION_RUN_CONFIG_SCHEMA_VERSION},
            path="$.schema_version",
        ),
        "config_id": require_string(root["config_id"], path="$.config_id"),
        "created_at": require_rfc3339(root["created_at"], path="$.created_at"),
        "producer": validate_producer(
            root["producer"], path="$.producer", workstream="evaluation"
        ),
        "input_binding": _validate_input_binding(root["input_binding"]),
        "methods": _validate_methods(root["methods"]),
        "comparison_pairs": _validate_comparison_pairs(root["comparison_pairs"]),
        "unit_policy": _validate_unit_policy(root["unit_policy"]),
        "blinding": _validate_blinding(root["blinding"]),
        "retry_policy": _validate_retry_policy(root["retry_policy"]),
        "integrity": _validate_config_integrity(root["integrity"]),
    }
    _validate_config_references(normalized)
    if not verify_payload_hash(
        normalized,
        policy=EVALUATION_RUN_CONFIG_POLICY,
        hash_path=CONFIG_SELF_HASH_PATH,
    ):
        raise ContractValidationError(
            "config_hash",
            "$.integrity.config_sha256",
            "evaluation config self-hash does not match canonical content",
        )
    canonical = canonicalize(normalized, policy=EVALUATION_RUN_CONFIG_POLICY)
    if not isinstance(canonical, dict):
        raise AssertionError("canonical evaluation config must remain an object")
    return canonical


def build_evaluation_plan(
    common_input: CommonEvaluationInputV1,
    config_payload: Mapping[str, Any],
) -> EvaluationPlanV1:
    config = validate_evaluation_run_config(config_payload)
    _validate_input_binding_matches(common_input, config["input_binding"])

    methods = config["methods"]
    pairs = config["comparison_pairs"]
    unit_policy = config["unit_policy"]
    seed = config["blinding"]["seed"]
    selected_arm_ids = tuple(
        row["arm_id"] for row in config["input_binding"]["arm_artifacts"]
    )
    translation_index = {
        (row.arm_id, row.block_id): row for row in common_input.translations
    }
    chapter_blocks: dict[str, list[str]] = {}
    for block in common_input.blocks:
        chapter_blocks.setdefault(block.chapter_id, []).append(block.block_id)

    units: list[EvaluationUnitV1] = []
    jobs: list[EvaluationJobV1] = []
    pair_ranks: dict[tuple[str, str], int] = {}
    eligible_block_ids: set[str] = set()

    config_sha256 = config["integrity"]["config_sha256"]
    input_set_sha256 = _common_input_set_sha256(common_input, selected_arm_ids)
    plan_id = "plan-" + _digest(
        config_sha256,
        input_set_sha256,
        config["config_id"],
    )[:24]

    for block in common_input.blocks:
        eligible_methods = tuple(
            row["method_id"]
            for row in methods
            if block.admission in row["eligible_admissions"]
        )
        unit_status = "eligible" if eligible_methods else "not_applicable"
        if eligible_methods:
            eligible_block_ids.add(block.block_id)
        context_ids = _context_block_ids(
            chapter_blocks[block.chapter_id],
            block.block_id,
            before=unit_policy["context_before_blocks"],
            after=unit_policy["context_after_blocks"],
        )
        translation_statuses = tuple(
            UnitTranslationStatusV1(
                arm_id=arm_id,
                status=_translation_status(translation_index.get((arm_id, block.block_id))),
            )
            for arm_id in selected_arm_ids
        )
        unit_id = _source_unit_id(common_input.source_binding, block.block_id)
        units.append(
            EvaluationUnitV1(
                unit_id=unit_id,
                block_id=block.block_id,
                chapter_id=block.chapter_id,
                order_index=block.order_index,
                admission=block.admission,
                status=unit_status,
                eligible_method_ids=eligible_methods,
                context_block_ids=context_ids,
                translation_statuses=translation_statuses,
            )
        )

        for method in methods:
            if block.admission not in method["eligible_admissions"]:
                continue
            if method["scorer_kind"] == "unary":
                for arm_id in selected_arm_ids:
                    translation = translation_index.get((arm_id, block.block_id))
                    ready = translation is not None and translation.status == "translated"
                    jobs.append(
                        _make_job(
                            config_sha256=config_sha256,
                            input_set_sha256=input_set_sha256,
                            method=method,
                            unit_id=unit_id,
                            presentation_arm_ids=(arm_id,),
                            ready=ready,
                            reason_code=None if ready else _unary_reason(translation),
                        )
                    )
                continue

            for pair in pairs:
                counter_key = (method["method_id"], pair["pair_id"])
                rank = pair_ranks.get(counter_key, 0)
                pair_ranks[counter_key] = rank + 1
                presentation = _counterbalanced_pair(
                    pair["arm_1_id"],
                    pair["arm_2_id"],
                    seed=seed,
                    method_id=method["method_id"],
                    pair_id=pair["pair_id"],
                    rank=rank,
                )
                ready = all(
                    (translation := translation_index.get((arm_id, block.block_id)))
                    is not None
                    and translation.status == "translated"
                    for arm_id in presentation
                )
                jobs.append(
                    _make_job(
                        config_sha256=config_sha256,
                        input_set_sha256=input_set_sha256,
                        method=method,
                        unit_id=unit_id,
                        presentation_arm_ids=presentation,
                        ready=ready,
                        reason_code=None if ready else "pair_translation_unavailable",
                    )
                )

    ready_jobs = sum(job.status == "ready" for job in jobs)
    blocked_jobs = len(jobs) - ready_jobs
    by_arm = tuple(
        _arm_coverage(
            arm_id,
            eligible_block_ids=eligible_block_ids,
            translation_index=translation_index,
        )
        for arm_id in selected_arm_ids
    )
    coverage = EvaluationCoverageV1(
        source_block_count=len(common_input.blocks),
        eligible_unit_count=len(eligible_block_ids),
        not_applicable_unit_count=len(common_input.blocks) - len(eligible_block_ids),
        ready_job_count=ready_jobs,
        blocked_job_count=blocked_jobs,
        by_arm=by_arm,
    )
    draft = EvaluationPlanV1(
        plan_id=plan_id,
        config_id=config["config_id"],
        config_sha256=config_sha256,
        input_set_sha256=input_set_sha256,
        project_id=common_input.project_id,
        document_id=common_input.document_id,
        source_binding=common_input.source_binding,
        selected_arm_ids=selected_arm_ids,
        units=tuple(units),
        jobs=tuple(jobs),
        coverage=coverage,
        plan_sha256="",
    )
    plan_sha256 = canonical_sha256(
        evaluation_plan_to_dict(draft, include_hash=False),
        policy=EVALUATION_PLAN_POLICY,
    )
    return EvaluationPlanV1(
        plan_id=draft.plan_id,
        config_id=draft.config_id,
        config_sha256=draft.config_sha256,
        input_set_sha256=draft.input_set_sha256,
        project_id=draft.project_id,
        document_id=draft.document_id,
        source_binding=draft.source_binding,
        selected_arm_ids=draft.selected_arm_ids,
        units=draft.units,
        jobs=draft.jobs,
        coverage=draft.coverage,
        plan_sha256=plan_sha256,
    )


def evaluation_plan_to_dict(
    plan: EvaluationPlanV1, *, include_hash: bool = True
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "plan_id": plan.plan_id,
        "config_id": plan.config_id,
        "config_sha256": plan.config_sha256,
        "input_set_sha256": plan.input_set_sha256,
        "project_id": plan.project_id,
        "document_id": plan.document_id,
        "source_binding": source_binding_to_dict(plan.source_binding),
        "selected_arm_ids": list(plan.selected_arm_ids),
        "units": [
            {
                "unit_id": unit.unit_id,
                "block_id": unit.block_id,
                "chapter_id": unit.chapter_id,
                "order_index": unit.order_index,
                "admission": unit.admission,
                "status": unit.status,
                "eligible_method_ids": list(unit.eligible_method_ids),
                "context_block_ids": list(unit.context_block_ids),
                "translation_statuses": [
                    {"arm_id": row.arm_id, "status": row.status}
                    for row in unit.translation_statuses
                ],
            }
            for unit in plan.units
        ],
        "jobs": [
            {
                "job_id": job.job_id,
                "method_id": job.method_id,
                "method_version": job.method_version,
                "scorer_kind": job.scorer_kind,
                "unit_id": job.unit_id,
                "presentation_arm_ids": list(job.presentation_arm_ids),
                "status": job.status,
                "reason_code": job.reason_code,
            }
            for job in plan.jobs
        ],
        "coverage": {
            "source_block_count": plan.coverage.source_block_count,
            "eligible_unit_count": plan.coverage.eligible_unit_count,
            "not_applicable_unit_count": plan.coverage.not_applicable_unit_count,
            "ready_job_count": plan.coverage.ready_job_count,
            "blocked_job_count": plan.coverage.blocked_job_count,
            "by_arm": [
                {
                    "arm_id": row.arm_id,
                    "eligible_block_count": row.eligible_block_count,
                    "ready_translation_count": row.ready_translation_count,
                    "unavailable_translation_count": row.unavailable_translation_count,
                }
                for row in plan.coverage.by_arm
            ],
        },
    }
    if include_hash:
        payload["plan_sha256"] = plan.plan_sha256
    return payload


def _validate_input_binding(value: Any) -> dict[str, Any]:
    path = "$.input_binding"
    row = require_mapping(value, path=path)
    require_exact_keys(
        row,
        required={
            "source_schema_id",
            "source_schema_version",
            "source_binding",
            "arm_artifacts",
        },
        path=path,
    )
    raw_arms = require_list(row["arm_artifacts"], path=f"{path}.arm_artifacts")
    if not raw_arms:
        raise ContractValidationError(
            "empty_array", f"{path}.arm_artifacts", "at least one arm is required"
        )
    arms: list[dict[str, str]] = []
    for index, raw_arm in enumerate(raw_arms):
        arm_path = f"{path}.arm_artifacts[{index}]"
        arm = require_mapping(raw_arm, path=arm_path)
        require_exact_keys(
            arm,
            required={
                "arm_id",
                "translation_artifact_id",
                "translation_artifact_sha256",
                "logical_run_id",
                "attempt_run_id",
                "profile_id",
                "profile_config_sha256",
            },
            path=arm_path,
        )
        arms.append(
            {
                "arm_id": require_string(arm["arm_id"], path=f"{arm_path}.arm_id"),
                "translation_artifact_id": require_string(
                    arm["translation_artifact_id"],
                    path=f"{arm_path}.translation_artifact_id",
                ),
                "translation_artifact_sha256": require_sha256(
                    arm["translation_artifact_sha256"],
                    path=f"{arm_path}.translation_artifact_sha256",
                ),
                "logical_run_id": require_string(
                    arm["logical_run_id"], path=f"{arm_path}.logical_run_id"
                ),
                "attempt_run_id": require_string(
                    arm["attempt_run_id"], path=f"{arm_path}.attempt_run_id"
                ),
                "profile_id": require_string(
                    arm["profile_id"], path=f"{arm_path}.profile_id"
                ),
                "profile_config_sha256": require_sha256(
                    arm["profile_config_sha256"],
                    path=f"{arm_path}.profile_config_sha256",
                ),
            }
        )
    require_unique([arm["arm_id"] for arm in arms], path=f"{path}.arm_artifacts.arm_id")
    require_unique(
        [arm["translation_artifact_id"] for arm in arms],
        path=f"{path}.arm_artifacts.translation_artifact_id",
    )
    require_unique(
        [arm["translation_artifact_sha256"] for arm in arms],
        path=f"{path}.arm_artifacts.translation_artifact_sha256",
    )
    return {
        "source_schema_id": require_string(
            row["source_schema_id"], path=f"{path}.source_schema_id"
        ),
        "source_schema_version": require_string(
            row["source_schema_version"], path=f"{path}.source_schema_version"
        ),
        "source_binding": _validate_source_binding_for_offline_planning(
            row["source_binding"]
        ),
        "arm_artifacts": arms,
    }


def _validate_methods(value: Any) -> list[dict[str, Any]]:
    path = "$.methods"
    rows = require_list(value, path=path)
    if not rows:
        raise ContractValidationError("empty_array", path, "at least one method is required")
    result: list[dict[str, Any]] = []
    for index, raw_row in enumerate(rows):
        row_path = f"{path}[{index}]"
        row = require_mapping(raw_row, path=row_path)
        require_exact_keys(
            row,
            required={
                "method_id",
                "method_version",
                "scorer_kind",
                "profile_scope",
                "eligible_admissions",
            },
            path=row_path,
        )
        admissions = [
            require_enum(item, _ELIGIBLE_ADMISSIONS, path=f"{row_path}.eligible_admissions[{i}]")
            for i, item in enumerate(
                require_list(row["eligible_admissions"], path=f"{row_path}.eligible_admissions")
            )
        ]
        if not admissions:
            raise ContractValidationError(
                "empty_array", f"{row_path}.eligible_admissions", "admissions are required"
            )
        require_unique(admissions, path=f"{row_path}.eligible_admissions")
        result.append(
            {
                "method_id": require_string(row["method_id"], path=f"{row_path}.method_id"),
                "method_version": require_string(
                    row["method_version"], path=f"{row_path}.method_version"
                ),
                "scorer_kind": require_enum(
                    row["scorer_kind"], {"unary", "pairwise"}, path=f"{row_path}.scorer_kind"
                ),
                "profile_scope": require_enum(
                    row["profile_scope"], {"common"}, path=f"{row_path}.profile_scope"
                ),
                "eligible_admissions": admissions,
            }
        )
    require_unique([row["method_id"] for row in result], path=f"{path}.method_id")
    return result


def _validate_comparison_pairs(value: Any) -> list[dict[str, str]]:
    path = "$.comparison_pairs"
    rows = require_list(value, path=path)
    result: list[dict[str, str]] = []
    for index, raw_row in enumerate(rows):
        row_path = f"{path}[{index}]"
        row = require_mapping(raw_row, path=row_path)
        require_exact_keys(
            row, required={"pair_id", "arm_1_id", "arm_2_id"}, path=row_path
        )
        arm_1_id = require_string(row["arm_1_id"], path=f"{row_path}.arm_1_id")
        arm_2_id = require_string(row["arm_2_id"], path=f"{row_path}.arm_2_id")
        if arm_1_id == arm_2_id:
            raise ContractValidationError(
                "comparison_pair", row_path, "a comparison requires two different arms"
            )
        result.append(
            {
                "pair_id": require_string(row["pair_id"], path=f"{row_path}.pair_id"),
                "arm_1_id": arm_1_id,
                "arm_2_id": arm_2_id,
            }
        )
    require_unique([row["pair_id"] for row in result], path=f"{path}.pair_id")
    unordered = [tuple(sorted((row["arm_1_id"], row["arm_2_id"]))) for row in result]
    if len(unordered) != len(set(unordered)):
        raise ContractValidationError("duplicate", path, "comparison pairs must be unique")
    return result


def _validate_unit_policy(value: Any) -> dict[str, Any]:
    path = "$.unit_policy"
    row = require_mapping(value, path=path)
    require_exact_keys(
        row,
        required={"unit_kind", "context_before_blocks", "context_after_blocks"},
        path=path,
    )
    return {
        "unit_kind": require_enum(row["unit_kind"], {"block"}, path=f"{path}.unit_kind"),
        "context_before_blocks": require_int(
            row["context_before_blocks"], path=f"{path}.context_before_blocks", minimum=0
        ),
        "context_after_blocks": require_int(
            row["context_after_blocks"], path=f"{path}.context_after_blocks", minimum=0
        ),
    }


def _validate_blinding(value: Any) -> dict[str, str]:
    path = "$.blinding"
    row = require_mapping(value, path=path)
    require_exact_keys(row, required={"mode", "seed"}, path=path)
    return {
        "mode": require_enum(
            row["mode"], {"opaque_counterbalanced"}, path=f"{path}.mode"
        ),
        "seed": require_string(row["seed"], path=f"{path}.seed"),
    }


def _validate_retry_policy(value: Any) -> dict[str, int]:
    path = "$.retry_policy"
    row = require_mapping(value, path=path)
    require_exact_keys(row, required={"max_transport_attempts"}, path=path)
    attempts = require_int(
        row["max_transport_attempts"], path=f"{path}.max_transport_attempts", minimum=1
    )
    if attempts > 3:
        raise ContractValidationError(
            "retry_cap", f"{path}.max_transport_attempts", "transport attempts may not exceed 3"
        )
    return {"max_transport_attempts": attempts}


def _validate_config_integrity(value: Any) -> dict[str, str]:
    path = "$.integrity"
    row = require_mapping(value, path=path)
    require_exact_keys(row, required={"config_sha256"}, path=path)
    return {
        "config_sha256": require_sha256(
            row["config_sha256"], path=f"{path}.config_sha256"
        )
    }


def _validate_config_references(config: Mapping[str, Any]) -> None:
    selected_arms = {row["arm_id"] for row in config["input_binding"]["arm_artifacts"]}
    pairwise = any(row["scorer_kind"] == "pairwise" for row in config["methods"])
    pairs = config["comparison_pairs"]
    if pairwise and not pairs:
        raise ContractValidationError(
            "comparison_pairs", "$.comparison_pairs", "pairwise methods require a declared pair"
        )
    if not pairwise and pairs:
        raise ContractValidationError(
            "unused_comparison_pairs",
            "$.comparison_pairs",
            "comparison pairs require at least one pairwise method",
        )
    for index, pair in enumerate(pairs):
        for field in ("arm_1_id", "arm_2_id"):
            if pair[field] not in selected_arms:
                raise ContractValidationError(
                    "arm_reference",
                    f"$.comparison_pairs[{index}].{field}",
                    "comparison references an unselected arm",
                )


def _validate_input_binding_matches(
    common_input: CommonEvaluationInputV1, binding: Mapping[str, Any]
) -> None:
    expected_source = {
        "source_schema_id": common_input.source_schema_id,
        "source_schema_version": common_input.source_schema_version,
        "source_binding": source_binding_to_dict(common_input.source_binding),
    }
    actual_source = {key: binding[key] for key in expected_source}
    if actual_source != expected_source:
        raise ContractValidationError(
            "input_binding", "$.input_binding", "config source binding is stale or foreign"
        )
    common_arms = {arm.arm_id: arm for arm in common_input.arms}
    for index, row in enumerate(binding["arm_artifacts"]):
        arm = common_arms.get(row["arm_id"])
        if arm is None:
            raise ContractValidationError(
                "arm_reference",
                f"$.input_binding.arm_artifacts[{index}].arm_id",
                "config references an unknown arm",
            )
        expected = {
            "arm_id": arm.arm_id,
            "translation_artifact_id": arm.artifact_id,
            "translation_artifact_sha256": arm.artifact_sha256,
            "logical_run_id": arm.logical_run_id,
            "attempt_run_id": arm.attempt_run_id,
            "profile_id": arm.profile_id,
            "profile_config_sha256": arm.profile_config_sha256,
        }
        if row != expected:
            raise ContractValidationError(
                "arm_binding",
                f"$.input_binding.arm_artifacts[{index}]",
                "config arm binding is stale or foreign",
            )


def _common_input_set_sha256(
    common_input: CommonEvaluationInputV1, selected_arm_ids: tuple[str, ...]
) -> str:
    selected = {arm_id for arm_id in selected_arm_ids}
    arms = [
        {
            "arm_id": arm.arm_id,
            "artifact_sha256": arm.artifact_sha256,
            "logical_run_id": arm.logical_run_id,
            "attempt_run_id": arm.attempt_run_id,
            "profile_id": arm.profile_id,
            "profile_config_sha256": arm.profile_config_sha256,
        }
        for arm in common_input.arms
        if arm.arm_id in selected
    ]
    policy = CanonicalPolicy(
        set_like_paths=frozenset({("arms",)}), semantic_sequence_paths=frozenset()
    )
    return canonical_sha256(
        {
            "source_binding": source_binding_to_dict(common_input.source_binding),
            "arms": arms,
        },
        policy=policy,
    )


def _context_block_ids(
    chapter_block_ids: list[str], active_block_id: str, *, before: int, after: int
) -> tuple[str, ...]:
    active_index = chapter_block_ids.index(active_block_id)
    start = max(0, active_index - before)
    end = min(len(chapter_block_ids), active_index + after + 1)
    return tuple(chapter_block_ids[start:end])


def _translation_status(row: CommonTranslationV1 | None) -> str:
    return "missing_row" if row is None else row.status


def _unary_reason(row: CommonTranslationV1 | None) -> str:
    if row is None:
        return "translation_row_missing"
    return f"translation_{row.status}"


def _counterbalanced_pair(
    arm_1_id: str,
    arm_2_id: str,
    *,
    seed: str,
    method_id: str,
    pair_id: str,
    rank: int,
) -> tuple[str, str]:
    first_flip = int(_digest(seed, method_id, pair_id), 16) % 2
    if (first_flip + rank) % 2 == 0:
        return (arm_1_id, arm_2_id)
    return (arm_2_id, arm_1_id)


def _make_job(
    *,
    config_sha256: str,
    input_set_sha256: str,
    method: Mapping[str, Any],
    unit_id: str,
    presentation_arm_ids: tuple[str, ...],
    ready: bool,
    reason_code: str | None,
) -> EvaluationJobV1:
    job_id = "job-" + _digest(
        config_sha256,
        input_set_sha256,
        method["method_id"],
        method["method_version"],
        unit_id,
        *presentation_arm_ids,
    )[:24]
    return EvaluationJobV1(
        job_id=job_id,
        method_id=method["method_id"],
        method_version=method["method_version"],
        scorer_kind=method["scorer_kind"],
        unit_id=unit_id,
        presentation_arm_ids=presentation_arm_ids,
        status="ready" if ready else "blocked",
        reason_code=reason_code,
    )


def _arm_coverage(
    arm_id: str,
    *,
    eligible_block_ids: set[str],
    translation_index: Mapping[tuple[str, str], CommonTranslationV1],
) -> ArmCoverageV1:
    ready = sum(
        translation_index.get((arm_id, block_id)) is not None
        and translation_index[(arm_id, block_id)].status == "translated"
        for block_id in eligible_block_ids
    )
    return ArmCoverageV1(
        arm_id=arm_id,
        eligible_block_count=len(eligible_block_ids),
        ready_translation_count=ready,
        unavailable_translation_count=len(eligible_block_ids) - ready,
    )


def _digest(*parts: str) -> str:
    payload = "\x1f".join(parts).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _source_unit_id(binding: SourceBindingV1, block_id: str) -> str:
    payload = {
        "source_binding": source_binding_to_dict(binding),
        "block_id": block_id,
    }
    policy = CanonicalPolicy(
        set_like_paths=frozenset(),
        semantic_sequence_paths=frozenset(),
    )
    return "unit-" + canonical_sha256(payload, policy=policy)[:24]
