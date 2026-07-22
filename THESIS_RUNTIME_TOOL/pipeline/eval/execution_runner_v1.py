from __future__ import annotations

import copy
import hashlib
import json
from collections import defaultdict
from typing import Any, Callable, Mapping

from pipeline.eval.common_input_v1 import CommonEvaluationInputV1
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
    require_nullable_number,
    require_nullable_string,
    require_number,
    require_rfc3339,
    require_sha256,
    require_string,
    require_unique,
    seal_payload,
    validate_producer,
    verify_payload_hash,
)
from pipeline.eval.offline_orchestrator_v1 import (
    EvaluationJobV1,
    EvaluationPlanV1,
    build_evaluation_plan,
    validate_evaluation_run_config,
)
from pipeline.eval.scorer_input_packets_v1 import (
    build_scorer_input_packet,
)
from pipeline.eval.scorer_prompts_v3 import (
    parse_pj_response_v2,
    parse_sf_bt_semantic_response_v3,
)


__all__ = [
    "EVALUATION_EXECUTION_SCHEMA_ID",
    "EVALUATION_EXECUTION_SCHEMA_VERSION",
    "aggregate_evaluation_job_rows_v1",
    "execute_evaluation_plan_v1",
    "seal_evaluation_execution_artifact",
    "validate_evaluation_job_observation_v1",
    "validate_evaluation_execution_artifact",
    "validate_evaluation_execution_binding",
    "validate_evaluation_aggregates_v1",
]


EVALUATION_EXECUTION_SCHEMA_ID = "EvaluationExecutionArtifactV1"
EVALUATION_EXECUTION_SCHEMA_VERSION = "1.0.0"
EXECUTION_SELF_HASH_PATH = ("integrity", "artifact_sha256")

EVALUATION_EXECUTION_POLICY = CanonicalPolicy(
    set_like_paths=frozenset(
        {
            ("binding", "selected_arm_ids"),
            ("jobs", "*", "semantic_output", "flags"),
            ("jobs", "*", "semantic_output", "tags"),
            ("aggregates", "*", "arm_values"),
            ("aggregates", "*", "source_job_ids"),
            ("claim", "reason_codes"),
            ("claim", "source_aggregate_ids"),
        }
    ),
    semantic_sequence_paths=frozenset(
        {
            ("jobs",),
            ("jobs", "*", "presentation_arm_ids"),
            ("aggregates",),
            ("aggregates", "*", "comparison_pair_arm_ids"),
            ("aggregates", "*", "caveats"),
        }
    ),
)

_SUPPORTED_METHODS = frozenset({"sf_qe", "sf_bt", "pj"})
_JOB_STATUSES = frozenset({"blocked", "succeeded", "failed"})
_PJ_VERDICTS = frozenset({"candidate_1", "candidate_2", "tie"})

EvaluationJobExecutorV1 = Callable[[Mapping[str, Any]], Mapping[str, Any]]


def validate_evaluation_aggregates_v1(value: Any) -> list[dict[str, Any]]:
    """Validate the aggregate rows shared by chapter and benchmark reports."""

    return _validate_aggregates(copy.deepcopy(value))


def aggregate_evaluation_job_rows_v1(
    job_rows: list[Mapping[str, Any]],
    *,
    selected_arm_ids: tuple[str, ...],
    baseline_arm_id: str | None = None,
    candidate_arm_id: str | None = None,
) -> list[dict[str, Any]]:
    """Aggregate already-validated jobs across immutable execution artifacts.

    This is used by the benchmark coordinator to avoid averaging chapter means.
    It performs no scoring and accepts no raw model output.
    """

    arms = tuple(
        require_string(arm_id, path=f"$.selected_arm_ids[{index}]")
        for index, arm_id in enumerate(selected_arm_ids)
    )
    if not arms:
        raise ContractValidationError(
            "empty_array", "$.selected_arm_ids", "at least one arm is required"
        )
    require_unique(list(arms), path="$.selected_arm_ids")
    baseline, candidate = _validate_comparison_roles(
        arms,
        baseline_arm_id=baseline_arm_id,
        candidate_arm_id=candidate_arm_id,
    )
    normalized = _validate_jobs(copy.deepcopy(list(job_rows)))
    allowed = set(arms)
    versions: dict[str, set[str]] = defaultdict(set)
    for index, row in enumerate(normalized):
        foreign = set(row["presentation_arm_ids"]) - allowed
        if foreign:
            raise ContractValidationError(
                "foreign_arm",
                f"$.jobs[{index}].presentation_arm_ids",
                "job references an arm outside the benchmark selection",
            )
        versions[row["method_id"]].add(row["method_version"])
    if any(len(values) != 1 for values in versions.values()):
        raise ContractValidationError(
            "method_version_drift",
            "$.jobs",
            "a method version differs across chapter executions",
        )
    return validate_evaluation_aggregates_v1(
        _aggregate_jobs(
            normalized,
            selected_arm_ids=arms,
            baseline_arm_id=baseline,
            candidate_arm_id=candidate,
        )
    )


def execute_evaluation_plan_v1(
    common_input: CommonEvaluationInputV1,
    config_payload: Mapping[str, Any],
    executor: EvaluationJobExecutorV1,
    *,
    created_at: str,
    runner_code_commit: str,
    baseline_arm_id: str | None = None,
    candidate_arm_id: str | None = None,
) -> dict[str, Any]:
    """Execute all planned jobs through an injected scorer and aggregate them.

    The function has no transport, model, retry, persistence, or verdict policy.
    A live executor must use the pipeline-owned local scorer or shared LLM
    adapter. Exceptions from the executor fail the run closed.
    """

    timestamp = require_rfc3339(created_at, path="$.created_at")
    code_commit = require_commit(runner_code_commit, path="$.runner_code_commit")
    config = validate_evaluation_run_config(config_payload)
    plan = build_evaluation_plan(common_input, config)
    selected_arm_ids = tuple(plan.selected_arm_ids)
    baseline, candidate = _validate_comparison_roles(
        selected_arm_ids,
        baseline_arm_id=baseline_arm_id,
        candidate_arm_id=candidate_arm_id,
    )
    unsupported = sorted(
        {job.method_id for job in plan.jobs if job.method_id not in _SUPPORTED_METHODS}
    )
    if unsupported:
        raise ContractValidationError(
            "unsupported_method",
            "$.methods",
            "semantic runner does not implement methods: " + ", ".join(unsupported),
        )

    job_rows: list[dict[str, Any]] = []
    for job in plan.jobs:
        if job.status == "blocked":
            job_rows.append(_blocked_job_row(job))
            continue
        packet = build_scorer_input_packet(
            common_input,
            plan,
            job.job_id,
            created_at=timestamp,
            producer_code_commit=code_commit,
        )
        detached_packet = copy.deepcopy(packet)
        raw_observation = executor(detached_packet)
        observation = _validate_executor_observation(
            raw_observation,
            method_id=job.method_id,
        )
        job_rows.append(_executed_job_row(job, packet, observation))

    aggregates = _aggregate_jobs(
        job_rows,
        selected_arm_ids=selected_arm_ids,
        baseline_arm_id=baseline,
        candidate_arm_id=candidate,
    )
    counts = _coverage_counts(job_rows)
    execution_id = "execution-" + _digest(
        plan.plan_sha256,
        timestamp,
        code_commit,
        baseline or "none",
        candidate or "none",
    )[:24]
    sealed = seal_evaluation_execution_artifact(
        {
            "schema_id": EVALUATION_EXECUTION_SCHEMA_ID,
            "schema_version": EVALUATION_EXECUTION_SCHEMA_VERSION,
            "execution_id": execution_id,
            "created_at": timestamp,
            "producer": {
                "workstream": "evaluation",
                "component": "execution_runner_v1",
                "component_version": "1.0.0",
                "code_commit": code_commit,
            },
            "binding": {
                "plan_id": plan.plan_id,
                "plan_sha256": plan.plan_sha256,
                "config_id": plan.config_id,
                "config_sha256": plan.config_sha256,
                "input_set_sha256": plan.input_set_sha256,
                "project_id": plan.project_id,
                "document_id": plan.document_id,
                "selected_arm_ids": list(selected_arm_ids),
                "baseline_arm_id": baseline,
                "candidate_arm_id": candidate,
            },
            "coverage": counts,
            "jobs": job_rows,
            "aggregates": aggregates,
            "claim": {
                "status": "insufficient",
                "verdict": "INCONCLUSIVE",
                "reason_codes": ["claim_policy_not_frozen"],
                "source_aggregate_ids": [
                    row["aggregate_id"] for row in aggregates
                ],
            },
            "integrity": {"artifact_sha256": "0" * 64},
        }
    )
    validated = validate_evaluation_execution_artifact(sealed)
    validate_evaluation_execution_binding(
        validated,
        common_input,
        plan,
    )
    return validated


def seal_evaluation_execution_artifact(
    payload: Mapping[str, Any],
) -> dict[str, Any]:
    return seal_payload(
        payload,
        policy=EVALUATION_EXECUTION_POLICY,
        hash_path=EXECUTION_SELF_HASH_PATH,
    )


def validate_evaluation_execution_artifact(
    payload: Mapping[str, Any],
) -> dict[str, Any]:
    root = require_mapping(payload, path="$")
    require_exact_keys(
        root,
        required={
            "schema_id",
            "schema_version",
            "execution_id",
            "created_at",
            "producer",
            "binding",
            "coverage",
            "jobs",
            "aggregates",
            "claim",
            "integrity",
        },
        path="$",
    )
    normalized = {
        "schema_id": require_enum(
            root["schema_id"], {EVALUATION_EXECUTION_SCHEMA_ID}, path="$.schema_id"
        ),
        "schema_version": require_enum(
            root["schema_version"],
            {EVALUATION_EXECUTION_SCHEMA_VERSION},
            path="$.schema_version",
        ),
        "execution_id": require_string(root["execution_id"], path="$.execution_id"),
        "created_at": require_rfc3339(root["created_at"], path="$.created_at"),
        "producer": validate_producer(
            root["producer"], path="$.producer", workstream="evaluation"
        ),
        "binding": _validate_binding(root["binding"]),
        "coverage": _validate_coverage(root["coverage"]),
        "jobs": _validate_jobs(root["jobs"]),
        "aggregates": _validate_aggregates(root["aggregates"]),
        "claim": _validate_claim(root["claim"]),
        "integrity": _validate_integrity(root["integrity"]),
    }
    _validate_internal_semantics(normalized)
    if not verify_payload_hash(
        normalized,
        policy=EVALUATION_EXECUTION_POLICY,
        hash_path=EXECUTION_SELF_HASH_PATH,
    ):
        raise ContractValidationError(
            "artifact_hash",
            "$.integrity.artifact_sha256",
            "execution artifact self-hash does not match canonical content",
        )
    canonical = canonicalize(normalized, policy=EVALUATION_EXECUTION_POLICY)
    if not isinstance(canonical, dict):
        raise AssertionError("canonical execution artifact must remain an object")
    return canonical


def validate_evaluation_execution_binding(
    payload: Mapping[str, Any],
    common_input: CommonEvaluationInputV1,
    plan: EvaluationPlanV1,
) -> dict[str, Any]:
    validated = validate_evaluation_execution_artifact(payload)
    expected_binding = {
        "plan_id": plan.plan_id,
        "plan_sha256": plan.plan_sha256,
        "config_id": plan.config_id,
        "config_sha256": plan.config_sha256,
        "input_set_sha256": plan.input_set_sha256,
        "project_id": plan.project_id,
        "document_id": plan.document_id,
        "selected_arm_ids": list(plan.selected_arm_ids),
        "baseline_arm_id": validated["binding"]["baseline_arm_id"],
        "candidate_arm_id": validated["binding"]["candidate_arm_id"],
    }
    if validated["binding"] != expected_binding:
        raise ContractValidationError(
            "execution_binding", "$.binding", "artifact references another plan or input"
        )
    expected_execution_id = "execution-" + _digest(
        plan.plan_sha256,
        validated["created_at"],
        validated["producer"]["code_commit"],
        validated["binding"]["baseline_arm_id"] or "none",
        validated["binding"]["candidate_arm_id"] or "none",
    )[:24]
    if validated["execution_id"] != expected_execution_id:
        raise ContractValidationError(
            "execution_id",
            "$.execution_id",
            "execution ID does not match its bound plan and comparison roles",
        )
    if len(validated["jobs"]) != len(plan.jobs):
        raise ContractValidationError(
            "job_exact_cover", "$.jobs", "artifact does not cover every planned job"
        )
    created_at = validated["created_at"]
    code_commit = validated["producer"]["code_commit"]
    for index, (row, job) in enumerate(zip(validated["jobs"], plan.jobs, strict=True)):
        path = f"$.jobs[{index}]"
        structural = {
            "job_id": job.job_id,
            "method_id": job.method_id,
            "method_version": job.method_version,
            "scorer_kind": job.scorer_kind,
            "unit_id": job.unit_id,
            "presentation_arm_ids": list(job.presentation_arm_ids),
        }
        if any(row[key] != value for key, value in structural.items()):
            raise ContractValidationError(
                "job_binding", path, "job row differs from the sealed plan"
            )
        if job.status == "blocked":
            if row["status"] != "blocked" or row["error_code"] != job.reason_code:
                raise ContractValidationError(
                    "blocked_job_binding", path, "blocked job status or reason drifted"
                )
            continue
        packet = build_scorer_input_packet(
            common_input,
            plan,
            job.job_id,
            created_at=created_at,
            producer_code_commit=code_commit,
        )
        if (
            row["packet_id"] != packet["packet_id"]
            or row["packet_sha256"] != packet["integrity"]["packet_sha256"]
        ):
            raise ContractValidationError(
                "packet_binding", path, "job row references a stale scorer packet"
            )
    return validated


def _validate_executor_observation(
    value: Mapping[str, Any], *, method_id: str
) -> dict[str, Any]:
    path = "$.executor_observation"
    row = require_mapping(value, path=path)
    require_exact_keys(
        row,
        required={"status", "semantic_output", "error_code"},
        path=path,
    )
    status = require_enum(
        row["status"], {"succeeded", "failed"}, path=f"{path}.status"
    )
    error_code = require_nullable_string(row["error_code"], path=f"{path}.error_code")
    if status == "failed":
        if row["semantic_output"] is not None or error_code is None:
            raise ContractValidationError(
                "failed_observation",
                path,
                "failed observation needs an error and no semantic output",
            )
        return {"status": status, "semantic_output": None, "error_code": error_code}
    if error_code is not None:
        raise ContractValidationError(
            "successful_observation",
            path,
            "successful observation cannot carry an error",
        )
    output = _validate_semantic_output(row["semantic_output"], method_id=method_id)
    return {"status": status, "semantic_output": output, "error_code": None}


def validate_evaluation_job_observation_v1(
    value: Mapping[str, Any], *, method_id: str
) -> dict[str, Any]:
    """Validate one scorer observation without granting report authority."""

    if method_id not in _SUPPORTED_METHODS:
        raise ContractValidationError(
            "unsupported_method",
            "$.method_id",
            f"no semantic output contract for {method_id!r}",
        )
    return _validate_executor_observation(value, method_id=method_id)


def _validate_semantic_output(value: Any, *, method_id: str) -> dict[str, Any]:
    path = "$.semantic_output"
    row = require_mapping(value, path=path)
    if method_id == "sf_qe":
        require_exact_keys(row, required={"score"}, path=path)
        score = require_number(row["score"], path=f"{path}.score", minimum=0)
        if score > 100:
            raise ContractValidationError(
                "score_range", f"{path}.score", "score must not exceed 100"
            )
        return {"score": score}
    try:
        encoded = json.dumps(
            dict(row),
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    except (TypeError, ValueError) as exc:
        raise ContractValidationError(
            "semantic_output",
            path,
            "semantic output must be finite canonical JSON",
        ) from exc
    if method_id == "sf_bt":
        return parse_sf_bt_semantic_response_v3(encoded)
    if method_id == "pj":
        return parse_pj_response_v2(encoded)
    raise ContractValidationError(
        "unsupported_method", path, f"no semantic output contract for {method_id!r}"
    )


def _blocked_job_row(job: EvaluationJobV1) -> dict[str, Any]:
    return {
        **_job_identity(job),
        "packet_id": None,
        "packet_sha256": None,
        "status": "blocked",
        "semantic_output": None,
        "error_code": job.reason_code,
    }


def _executed_job_row(
    job: EvaluationJobV1,
    packet: Mapping[str, Any],
    observation: Mapping[str, Any],
) -> dict[str, Any]:
    return {
        **_job_identity(job),
        "packet_id": packet["packet_id"],
        "packet_sha256": packet["integrity"]["packet_sha256"],
        "status": observation["status"],
        "semantic_output": copy.deepcopy(observation["semantic_output"]),
        "error_code": observation["error_code"],
    }


def _job_identity(job: EvaluationJobV1) -> dict[str, Any]:
    return {
        "job_id": job.job_id,
        "method_id": job.method_id,
        "method_version": job.method_version,
        "scorer_kind": job.scorer_kind,
        "unit_id": job.unit_id,
        "presentation_arm_ids": list(job.presentation_arm_ids),
    }


def _aggregate_jobs(
    jobs: list[dict[str, Any]],
    *,
    selected_arm_ids: tuple[str, ...],
    baseline_arm_id: str | None,
    candidate_arm_id: str | None,
) -> list[dict[str, Any]]:
    grouped: dict[tuple[str, tuple[str, ...]], list[dict[str, Any]]] = defaultdict(list)
    for row in jobs:
        pair = (
            tuple(sorted(row["presentation_arm_ids"]))
            if row["method_id"] == "pj"
            else ()
        )
        grouped[(row["method_id"], pair)].append(row)

    result: list[dict[str, Any]] = []
    for (method_id, pair), rows in grouped.items():
        if method_id in {"sf_qe", "sf_bt"}:
            aggregate = _aggregate_unary(
                method_id,
                rows,
                selected_arm_ids=selected_arm_ids,
                baseline_arm_id=baseline_arm_id,
                candidate_arm_id=candidate_arm_id,
            )
        else:
            aggregate = _aggregate_pj(
                rows,
                pair=pair,
                baseline_arm_id=baseline_arm_id,
                candidate_arm_id=candidate_arm_id,
            )
        result.append(aggregate)
    return result


def _aggregate_unary(
    method_id: str,
    rows: list[dict[str, Any]],
    *,
    selected_arm_ids: tuple[str, ...],
    baseline_arm_id: str | None,
    candidate_arm_id: str | None,
) -> dict[str, Any]:
    by_arm: dict[str, list[dict[str, Any]]] = {
        arm_id: [] for arm_id in selected_arm_ids
    }
    for row in rows:
        by_arm[row["presentation_arm_ids"][0]].append(row)
    arm_values: list[dict[str, Any]] = []
    observed_by_unit: dict[str, dict[str, float]] = defaultdict(dict)
    for arm_id in selected_arm_ids:
        arm_rows = by_arm[arm_id]
        scores = [
            float(row["semantic_output"]["score"])
            for row in arm_rows
            if row["status"] == "succeeded"
        ]
        for row in arm_rows:
            if row["status"] == "succeeded":
                observed_by_unit[row["unit_id"]][arm_id] = float(
                    row["semantic_output"]["score"]
                )
        expected = len(arm_rows)
        observed = len(scores)
        total = sum(scores) if scores else None
        arm_values.append(
            {
                "arm_id": arm_id,
                "value": None if total is None else total / observed,
                "numerator": total,
                "denominator": observed,
                "expected_count": expected,
                "observed_count": observed,
                "missing_count": expected - observed,
            }
        )
    observed_total = sum(row["observed_count"] for row in arm_values)
    expected_total = sum(row["expected_count"] for row in arm_values)
    comparison = _unary_comparison(
        observed_by_unit,
        baseline_arm_id=baseline_arm_id,
        candidate_arm_id=candidate_arm_id,
    )
    return {
        "aggregate_id": "aggregate-" + _digest(method_id, "unary")[:24],
        "method_id": method_id,
        "method_version": rows[0]["method_version"],
        "scorer_kind": "unary",
        "status": _aggregate_status(observed_total, expected_total),
        "unit": "score_0_100",
        "expected_job_count": expected_total,
        "observed_job_count": observed_total,
        "missing_job_count": expected_total - observed_total,
        "comparison_pair_arm_ids": [],
        "arm_values": arm_values,
        "comparison": comparison,
        "source_job_ids": [row["job_id"] for row in rows],
        "caveats": _missing_caveats(expected_total - observed_total),
    }


def _unary_comparison(
    observed_by_unit: Mapping[str, Mapping[str, float]],
    *,
    baseline_arm_id: str | None,
    candidate_arm_id: str | None,
) -> dict[str, Any]:
    if baseline_arm_id is None or candidate_arm_id is None:
        return _empty_comparison("not_applicable")
    pairs = [
        (scores[baseline_arm_id], scores[candidate_arm_id])
        for scores in observed_by_unit.values()
        if baseline_arm_id in scores and candidate_arm_id in scores
    ]
    if not pairs:
        return _empty_comparison(
            "insufficient",
            baseline_arm_id=baseline_arm_id,
            candidate_arm_id=candidate_arm_id,
        )
    wins = sum(candidate > baseline for baseline, candidate in pairs)
    ties = sum(candidate == baseline for baseline, candidate in pairs)
    losses = len(pairs) - wins - ties
    delta = sum(candidate - baseline for baseline, candidate in pairs) / len(pairs)
    return {
        "status": "available",
        "baseline_arm_id": baseline_arm_id,
        "candidate_arm_id": candidate_arm_id,
        "delta": delta,
        "wins": wins,
        "ties": ties,
        "losses": losses,
        "paired_denominator": len(pairs),
    }


def _aggregate_pj(
    rows: list[dict[str, Any]],
    *,
    pair: tuple[str, ...],
    baseline_arm_id: str | None,
    candidate_arm_id: str | None,
) -> dict[str, Any]:
    wins = {arm_id: 0 for arm_id in pair}
    ties = 0
    observed = 0
    for row in rows:
        if row["status"] != "succeeded":
            continue
        observed += 1
        verdict = row["semantic_output"]["overall_verdict"]
        if verdict == "tie":
            ties += 1
        else:
            index = 0 if verdict == "candidate_1" else 1
            wins[row["presentation_arm_ids"][index]] += 1
    expected = len(rows)
    arm_values = [
        {
            "arm_id": arm_id,
            "value": wins[arm_id],
            "numerator": wins[arm_id],
            "denominator": observed,
            "expected_count": expected,
            "observed_count": observed,
            "missing_count": expected - observed,
        }
        for arm_id in pair
    ]
    if (
        baseline_arm_id is not None
        and candidate_arm_id is not None
        and {baseline_arm_id, candidate_arm_id} == set(pair)
        and observed
    ):
        comparison = {
            "status": "available",
            "baseline_arm_id": baseline_arm_id,
            "candidate_arm_id": candidate_arm_id,
            "delta": wins[candidate_arm_id] - wins[baseline_arm_id],
            "wins": wins[candidate_arm_id],
            "ties": ties,
            "losses": wins[baseline_arm_id],
            "paired_denominator": observed,
        }
    elif baseline_arm_id is None or candidate_arm_id is None:
        comparison = _empty_comparison("not_applicable")
    else:
        comparison = _empty_comparison(
            "insufficient",
            baseline_arm_id=baseline_arm_id,
            candidate_arm_id=candidate_arm_id,
        )
    return {
        "aggregate_id": "aggregate-" + _digest("pj", *pair)[:24],
        "method_id": "pj",
        "method_version": rows[0]["method_version"],
        "scorer_kind": "pairwise",
        "status": _aggregate_status(observed, expected),
        "unit": "pairwise_counts",
        "expected_job_count": expected,
        "observed_job_count": observed,
        "missing_job_count": expected - observed,
        "comparison_pair_arm_ids": list(pair),
        "arm_values": arm_values,
        "comparison": comparison,
        "source_job_ids": [row["job_id"] for row in rows],
        "caveats": _missing_caveats(expected - observed),
    }


def _aggregate_status(observed: int, expected: int) -> str:
    if observed == 0:
        return "failed"
    return "available" if observed == expected else "partial"


def _missing_caveats(missing: int) -> list[str]:
    return [] if missing == 0 else [f"{missing} planned job(s) lack a valid observation"]


def _empty_comparison(
    status: str,
    *,
    baseline_arm_id: str | None = None,
    candidate_arm_id: str | None = None,
) -> dict[str, Any]:
    return {
        "status": status,
        "baseline_arm_id": baseline_arm_id,
        "candidate_arm_id": candidate_arm_id,
        "delta": None,
        "wins": None,
        "ties": None,
        "losses": None,
        "paired_denominator": 0,
    }


def _coverage_counts(jobs: list[dict[str, Any]]) -> dict[str, int]:
    return {
        "planned_job_count": len(jobs),
        "blocked_job_count": sum(row["status"] == "blocked" for row in jobs),
        "succeeded_job_count": sum(row["status"] == "succeeded" for row in jobs),
        "failed_job_count": sum(row["status"] == "failed" for row in jobs),
    }


def _validate_comparison_roles(
    selected_arm_ids: tuple[str, ...],
    *,
    baseline_arm_id: str | None,
    candidate_arm_id: str | None,
) -> tuple[str | None, str | None]:
    if (baseline_arm_id is None) != (candidate_arm_id is None):
        raise ContractValidationError(
            "comparison_roles",
            "$.comparison_roles",
            "baseline and candidate must both be supplied or both be absent",
        )
    if baseline_arm_id is None:
        return None, None
    baseline = require_string(baseline_arm_id, path="$.baseline_arm_id")
    candidate = require_string(candidate_arm_id, path="$.candidate_arm_id")
    if baseline == candidate:
        raise ContractValidationError(
            "comparison_roles", "$.comparison_roles", "comparison arms must differ"
        )
    selected = set(selected_arm_ids)
    if baseline not in selected or candidate not in selected:
        raise ContractValidationError(
            "comparison_roles",
            "$.comparison_roles",
            "comparison role references an unselected arm",
        )
    return baseline, candidate


def _validate_binding(value: Any) -> dict[str, Any]:
    path = "$.binding"
    row = require_mapping(value, path=path)
    require_exact_keys(
        row,
        required={
            "plan_id",
            "plan_sha256",
            "config_id",
            "config_sha256",
            "input_set_sha256",
            "project_id",
            "document_id",
            "selected_arm_ids",
            "baseline_arm_id",
            "candidate_arm_id",
        },
        path=path,
    )
    selected = [
        require_string(item, path=f"{path}.selected_arm_ids[{index}]")
        for index, item in enumerate(
            require_list(row["selected_arm_ids"], path=f"{path}.selected_arm_ids")
        )
    ]
    if not selected:
        raise ContractValidationError(
            "empty_array", f"{path}.selected_arm_ids", "selected arms are required"
        )
    require_unique(selected, path=f"{path}.selected_arm_ids")
    baseline = require_nullable_string(
        row["baseline_arm_id"], path=f"{path}.baseline_arm_id"
    )
    candidate = require_nullable_string(
        row["candidate_arm_id"], path=f"{path}.candidate_arm_id"
    )
    _validate_comparison_roles(
        tuple(selected), baseline_arm_id=baseline, candidate_arm_id=candidate
    )
    return {
        "plan_id": require_string(row["plan_id"], path=f"{path}.plan_id"),
        "plan_sha256": require_sha256(
            row["plan_sha256"], path=f"{path}.plan_sha256"
        ),
        "config_id": require_string(row["config_id"], path=f"{path}.config_id"),
        "config_sha256": require_sha256(
            row["config_sha256"], path=f"{path}.config_sha256"
        ),
        "input_set_sha256": require_sha256(
            row["input_set_sha256"], path=f"{path}.input_set_sha256"
        ),
        "project_id": require_string(row["project_id"], path=f"{path}.project_id"),
        "document_id": require_string(
            row["document_id"], path=f"{path}.document_id"
        ),
        "selected_arm_ids": selected,
        "baseline_arm_id": baseline,
        "candidate_arm_id": candidate,
    }


def _validate_coverage(value: Any) -> dict[str, int]:
    path = "$.coverage"
    row = require_mapping(value, path=path)
    fields = {
        "planned_job_count",
        "blocked_job_count",
        "succeeded_job_count",
        "failed_job_count",
    }
    require_exact_keys(row, required=fields, path=path)
    return {
        field: require_int(row[field], path=f"{path}.{field}", minimum=0)
        for field in fields
    }


def _validate_jobs(value: Any) -> list[dict[str, Any]]:
    path = "$.jobs"
    result: list[dict[str, Any]] = []
    for index, raw in enumerate(require_list(value, path=path)):
        row_path = f"{path}[{index}]"
        row = require_mapping(raw, path=row_path)
        require_exact_keys(
            row,
            required={
                "job_id",
                "method_id",
                "method_version",
                "scorer_kind",
                "unit_id",
                "presentation_arm_ids",
                "packet_id",
                "packet_sha256",
                "status",
                "semantic_output",
                "error_code",
            },
            path=row_path,
        )
        method_id = require_enum(
            row["method_id"], _SUPPORTED_METHODS, path=f"{row_path}.method_id"
        )
        arms = [
            require_string(item, path=f"{row_path}.presentation_arm_ids[{arm_index}]")
            for arm_index, item in enumerate(
                require_list(
                    row["presentation_arm_ids"],
                    path=f"{row_path}.presentation_arm_ids",
                )
            )
        ]
        expected_arms = 2 if method_id == "pj" else 1
        if len(arms) != expected_arms or len(set(arms)) != len(arms):
            raise ContractValidationError(
                "presentation_arms",
                f"{row_path}.presentation_arm_ids",
                f"{method_id} requires {expected_arms} distinct presentation arm(s)",
            )
        status = require_enum(row["status"], _JOB_STATUSES, path=f"{row_path}.status")
        scorer_kind = require_enum(
            row["scorer_kind"],
            {"unary", "pairwise"},
            path=f"{row_path}.scorer_kind",
        )
        expected_kind = "pairwise" if method_id == "pj" else "unary"
        if scorer_kind != expected_kind:
            raise ContractValidationError(
                "scorer_kind",
                f"{row_path}.scorer_kind",
                f"{method_id} requires scorer_kind={expected_kind}",
            )
        packet_id = require_nullable_string(row["packet_id"], path=f"{row_path}.packet_id")
        packet_sha256 = (
            None
            if row["packet_sha256"] is None
            else require_sha256(row["packet_sha256"], path=f"{row_path}.packet_sha256")
        )
        error_code = require_nullable_string(
            row["error_code"], path=f"{row_path}.error_code"
        )
        if status == "blocked":
            if packet_id is not None or packet_sha256 is not None or error_code is None:
                raise ContractValidationError(
                    "blocked_job", row_path, "blocked job needs only an error code"
                )
            output = None
        else:
            if packet_id is None or packet_sha256 is None:
                raise ContractValidationError(
                    "packet_reference", row_path, "executed job needs a scorer packet"
                )
            if status == "failed":
                if row["semantic_output"] is not None or error_code is None:
                    raise ContractValidationError(
                        "failed_job", row_path, "failed job needs an error and no output"
                    )
                output = None
            else:
                if error_code is not None:
                    raise ContractValidationError(
                        "succeeded_job", row_path, "successful job cannot carry an error"
                    )
                output = _validate_semantic_output(
                    row["semantic_output"], method_id=method_id
                )
        result.append(
            {
                "job_id": require_string(row["job_id"], path=f"{row_path}.job_id"),
                "method_id": method_id,
                "method_version": require_string(
                    row["method_version"], path=f"{row_path}.method_version"
                ),
                "scorer_kind": scorer_kind,
                "unit_id": require_string(row["unit_id"], path=f"{row_path}.unit_id"),
                "presentation_arm_ids": arms,
                "packet_id": packet_id,
                "packet_sha256": packet_sha256,
                "status": status,
                "semantic_output": output,
                "error_code": error_code,
            }
        )
    require_unique([row["job_id"] for row in result], path=path)
    return result


def _validate_aggregates(value: Any) -> list[dict[str, Any]]:
    path = "$.aggregates"
    rows = require_list(value, path=path)
    result: list[dict[str, Any]] = []
    for index, raw in enumerate(rows):
        row_path = f"{path}[{index}]"
        row = require_mapping(raw, path=row_path)
        require_exact_keys(
            row,
            required={
                "aggregate_id",
                "method_id",
                "method_version",
                "scorer_kind",
                "status",
                "unit",
                "expected_job_count",
                "observed_job_count",
                "missing_job_count",
                "comparison_pair_arm_ids",
                "arm_values",
                "comparison",
                "source_job_ids",
                "caveats",
            },
            path=row_path,
        )
        result.append(
            {
                "aggregate_id": require_string(
                    row["aggregate_id"], path=f"{row_path}.aggregate_id"
                ),
                "method_id": require_enum(
                    row["method_id"], _SUPPORTED_METHODS, path=f"{row_path}.method_id"
                ),
                "method_version": require_string(
                    row["method_version"], path=f"{row_path}.method_version"
                ),
                "scorer_kind": require_enum(
                    row["scorer_kind"],
                    {"unary", "pairwise"},
                    path=f"{row_path}.scorer_kind",
                ),
                "status": require_enum(
                    row["status"],
                    {"available", "partial", "failed"},
                    path=f"{row_path}.status",
                ),
                "unit": require_enum(
                    row["unit"],
                    {"score_0_100", "pairwise_counts"},
                    path=f"{row_path}.unit",
                ),
                "expected_job_count": require_int(
                    row["expected_job_count"],
                    path=f"{row_path}.expected_job_count",
                    minimum=0,
                ),
                "observed_job_count": require_int(
                    row["observed_job_count"],
                    path=f"{row_path}.observed_job_count",
                    minimum=0,
                ),
                "missing_job_count": require_int(
                    row["missing_job_count"],
                    path=f"{row_path}.missing_job_count",
                    minimum=0,
                ),
                "comparison_pair_arm_ids": _validate_string_list(
                    row["comparison_pair_arm_ids"],
                    path=f"{row_path}.comparison_pair_arm_ids",
                    unique=True,
                ),
                "arm_values": _validate_arm_values(
                    row["arm_values"], path=f"{row_path}.arm_values"
                ),
                "comparison": _validate_comparison(
                    row["comparison"], path=f"{row_path}.comparison"
                ),
                "source_job_ids": _validate_string_list(
                    row["source_job_ids"],
                    path=f"{row_path}.source_job_ids",
                    unique=True,
                ),
                "caveats": _validate_string_list(
                    row["caveats"], path=f"{row_path}.caveats", unique=False
                ),
            }
        )
    require_unique([row["aggregate_id"] for row in result], path=path)
    return result


def _validate_arm_values(value: Any, *, path: str) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for index, raw in enumerate(require_list(value, path=path)):
        row_path = f"{path}[{index}]"
        row = require_mapping(raw, path=row_path)
        require_exact_keys(
            row,
            required={
                "arm_id",
                "value",
                "numerator",
                "denominator",
                "expected_count",
                "observed_count",
                "missing_count",
            },
            path=row_path,
        )
        result.append(
            {
                "arm_id": require_string(row["arm_id"], path=f"{row_path}.arm_id"),
                "value": require_nullable_number(row["value"], path=f"{row_path}.value"),
                "numerator": require_nullable_number(
                    row["numerator"], path=f"{row_path}.numerator", minimum=0
                ),
                "denominator": require_int(
                    row["denominator"], path=f"{row_path}.denominator", minimum=0
                ),
                "expected_count": require_int(
                    row["expected_count"], path=f"{row_path}.expected_count", minimum=0
                ),
                "observed_count": require_int(
                    row["observed_count"], path=f"{row_path}.observed_count", minimum=0
                ),
                "missing_count": require_int(
                    row["missing_count"], path=f"{row_path}.missing_count", minimum=0
                ),
            }
        )
    require_unique([row["arm_id"] for row in result], path=path)
    return result


def _validate_comparison(value: Any, *, path: str) -> dict[str, Any]:
    row = require_mapping(value, path=path)
    require_exact_keys(
        row,
        required={
            "status",
            "baseline_arm_id",
            "candidate_arm_id",
            "delta",
            "wins",
            "ties",
            "losses",
            "paired_denominator",
        },
        path=path,
    )
    status = require_enum(
        row["status"],
        {"available", "insufficient", "not_applicable"},
        path=f"{path}.status",
    )
    result = {
        "status": status,
        "baseline_arm_id": require_nullable_string(
            row["baseline_arm_id"], path=f"{path}.baseline_arm_id"
        ),
        "candidate_arm_id": require_nullable_string(
            row["candidate_arm_id"], path=f"{path}.candidate_arm_id"
        ),
        "delta": require_nullable_number(row["delta"], path=f"{path}.delta"),
        "wins": _nullable_int(row["wins"], path=f"{path}.wins"),
        "ties": _nullable_int(row["ties"], path=f"{path}.ties"),
        "losses": _nullable_int(row["losses"], path=f"{path}.losses"),
        "paired_denominator": require_int(
            row["paired_denominator"], path=f"{path}.paired_denominator", minimum=0
        ),
    }
    if status == "available":
        if any(
            result[field] is None
            for field in (
                "baseline_arm_id",
                "candidate_arm_id",
                "delta",
                "wins",
                "ties",
                "losses",
            )
        ) or result["paired_denominator"] == 0:
            raise ContractValidationError(
                "comparison", path, "available comparison is incomplete"
            )
    elif any(result[field] is not None for field in ("delta", "wins", "ties", "losses")):
        raise ContractValidationError(
            "comparison", path, "unavailable comparison cannot carry outcomes"
        )
    return result


def _validate_claim(value: Any) -> dict[str, Any]:
    path = "$.claim"
    row = require_mapping(value, path=path)
    require_exact_keys(
        row,
        required={"status", "verdict", "reason_codes", "source_aggregate_ids"},
        path=path,
    )
    status = require_enum(row["status"], {"insufficient"}, path=f"{path}.status")
    verdict = require_enum(row["verdict"], {"INCONCLUSIVE"}, path=f"{path}.verdict")
    reasons = _validate_string_list(
        row["reason_codes"], path=f"{path}.reason_codes", unique=True
    )
    if reasons != ["claim_policy_not_frozen"]:
        raise ContractValidationError(
            "claim_policy", f"{path}.reason_codes", "C0 cannot publish a quality claim"
        )
    return {
        "status": status,
        "verdict": verdict,
        "reason_codes": reasons,
        "source_aggregate_ids": _validate_string_list(
            row["source_aggregate_ids"],
            path=f"{path}.source_aggregate_ids",
            unique=True,
        ),
    }


def _validate_integrity(value: Any) -> dict[str, str]:
    path = "$.integrity"
    row = require_mapping(value, path=path)
    require_exact_keys(row, required={"artifact_sha256"}, path=path)
    return {
        "artifact_sha256": require_sha256(
            row["artifact_sha256"], path=f"{path}.artifact_sha256"
        )
    }


def _validate_internal_semantics(payload: Mapping[str, Any]) -> None:
    jobs = payload["jobs"]
    if payload["coverage"] != _coverage_counts(jobs):
        raise ContractValidationError(
            "coverage", "$.coverage", "coverage does not match persisted jobs"
        )
    selected = set(payload["binding"]["selected_arm_ids"])
    if any(
        arm_id not in selected
        for row in jobs
        for arm_id in row["presentation_arm_ids"]
    ):
        raise ContractValidationError(
            "arm_reference", "$.jobs", "job references an unselected arm"
        )
    expected_aggregates = _aggregate_jobs(
        jobs,
        selected_arm_ids=tuple(payload["binding"]["selected_arm_ids"]),
        baseline_arm_id=payload["binding"]["baseline_arm_id"],
        candidate_arm_id=payload["binding"]["candidate_arm_id"],
    )
    actual_aggregate_view = canonicalize(
        {"aggregates": payload["aggregates"]},
        policy=EVALUATION_EXECUTION_POLICY,
    )
    expected_aggregate_view = canonicalize(
        {"aggregates": expected_aggregates},
        policy=EVALUATION_EXECUTION_POLICY,
    )
    if actual_aggregate_view != expected_aggregate_view:
        raise ContractValidationError(
            "aggregate_recompute",
            "$.aggregates",
            "aggregates do not match the persisted job observations",
        )
    aggregate_ids = [row["aggregate_id"] for row in payload["aggregates"]]
    if set(payload["claim"]["source_aggregate_ids"]) != set(aggregate_ids):
        raise ContractValidationError(
            "claim_sources",
            "$.claim.source_aggregate_ids",
            "claim must reference the exact aggregate set",
        )


def _validate_string_list(value: Any, *, path: str, unique: bool) -> list[str]:
    rows = [
        require_string(item, path=f"{path}[{index}]")
        for index, item in enumerate(require_list(value, path=path))
    ]
    if unique:
        require_unique(rows, path=path)
    return rows


def _nullable_int(value: Any, *, path: str) -> int | None:
    if value is None:
        return None
    return require_int(value, path=path, minimum=0)


def _digest(*parts: str) -> str:
    material = "\x1f".join(parts).encode("utf-8")
    return hashlib.sha256(material).hexdigest()
