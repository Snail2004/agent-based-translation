from __future__ import annotations

import copy
import hashlib
import json
from collections import Counter
from typing import Any, Mapping, Protocol

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
    require_nullable_string,
    require_rfc3339,
    require_sha256,
    require_string,
    require_unique,
    seal_payload,
    validate_producer,
    verify_payload_hash,
)
from pipeline.eval.execution_runner_v1 import (
    validate_evaluation_job_observation_v1,
)
from pipeline.eval.live_pilot_preflight_v1 import (
    validate_evaluation_live_pilot_preflight_binding,
)
from pipeline.eval.live_pilot_sf_qe_v1 import (
    validate_pilot_local_sf_qe_binding_v1,
)
from pipeline.eval.offline_orchestrator_v1 import (
    EvaluationJobV1,
    build_evaluation_plan,
    validate_evaluation_run_config,
)
from pipeline.eval.scorer_input_packets_v1 import build_scorer_input_packet


__all__ = [
    "EVALUATION_LIVE_PILOT_EXECUTION_SCHEMA_ID",
    "EVALUATION_LIVE_PILOT_EXECUTION_SCHEMA_VERSION",
    "execute_evaluation_live_pilot_v1",
    "seal_evaluation_live_pilot_execution",
    "validate_evaluation_live_pilot_execution",
    "validate_evaluation_live_pilot_execution_binding",
]


EVALUATION_LIVE_PILOT_EXECUTION_SCHEMA_ID = "EvaluationLivePilotExecutionV1"
EVALUATION_LIVE_PILOT_EXECUTION_SCHEMA_VERSION = "1.0.0"
PILOT_EXECUTION_SELF_HASH_PATH = ("integrity", "execution_sha256")

PILOT_EXECUTION_POLICY = CanonicalPolicy(
    set_like_paths=frozenset(),
    semantic_sequence_paths=frozenset(
        {
            ("binding", "selected_arm_ids"),
            ("jobs",),
            ("jobs", "*", "presentation_arm_ids"),
            ("jobs", "*", "semantic_output", "flags"),
            ("jobs", "*", "semantic_output", "tags"),
        }
    ),
)

_METHOD_IDS = frozenset({"sf_qe", "sf_bt", "pj"})

class EvaluationPilotJobExecutorV1(Protocol):
    @property
    def execution_binding(self) -> Mapping[str, Any]: ...

    def __call__(self, packet: Mapping[str, Any]) -> Mapping[str, Any]: ...

    def begin_sf_qe_execution(self) -> None: ...

    def assert_sf_qe_exact_cover(self) -> None: ...


def execute_evaluation_live_pilot_v1(
    common_input: CommonEvaluationInputV1,
    config_payload: Mapping[str, Any],
    preflight_payload: Mapping[str, Any],
    executor: EvaluationPilotJobExecutorV1,
    *,
    created_at: str,
    runner_code_commit: str,
) -> dict[str, Any]:
    """Execute only the jobs sealed by a validated calibration preflight."""

    timestamp = require_rfc3339(created_at, path="$.created_at")
    code_commit = require_commit(
        runner_code_commit, path="$.runner_code_commit"
    )
    execution_binding = _validate_executor_binding(executor.execution_binding)
    logical_run_id = execution_binding["evaluation_logical_run_id"]
    attempt_run_id = execution_binding["evaluation_attempt_run_id"]
    profile_id = execution_binding["evaluation_profile_id"]
    profile_sha256 = execution_binding["evaluation_profile_sha256"]
    local_sf_qe = execution_binding["local_sf_qe"]
    config = validate_evaluation_run_config(config_payload)
    preflight = validate_evaluation_live_pilot_preflight_binding(
        preflight_payload, common_input, config
    )
    plan = build_evaluation_plan(common_input, config)
    jobs_by_id = {row.job_id: row for row in plan.jobs}

    executor.begin_sf_qe_execution()
    job_rows: list[dict[str, Any]] = []
    for index, preflight_job in enumerate(preflight["jobs"]):
        path = f"$.preflight.jobs[{index}]"
        try:
            job = jobs_by_id[preflight_job["job_id"]]
        except KeyError as exc:
            raise ContractValidationError(
                "pilot_job_reference", path, "preflight references a foreign job"
            ) from exc
        _require_preflight_job_identity(preflight_job, job, path=path)
        packet = build_scorer_input_packet(
            common_input,
            plan,
            job.job_id,
            created_at=preflight["created_at"],
            producer_code_commit=preflight["producer"]["code_commit"],
        )
        if packet["integrity"]["packet_sha256"] != preflight_job["packet_sha256"]:
            raise ContractValidationError(
                "pilot_packet_binding",
                f"{path}.packet_sha256",
                "preflight packet hash differs from reconstructed input",
            )
        raw_observation = executor(copy.deepcopy(packet))
        observation = validate_evaluation_job_observation_v1(
            raw_observation, method_id=job.method_id
        )
        job_rows.append(_job_result_row(job, packet, observation))

    executor.assert_sf_qe_exact_cover()
    coverage = _derive_coverage(job_rows)
    execution_id = "pilot-execution-" + _digest(
        preflight["integrity"]["preflight_sha256"],
        timestamp,
        code_commit,
        logical_run_id,
        attempt_run_id,
        profile_sha256,
        _binding_digest(local_sf_qe),
    )[:24]
    sealed = seal_evaluation_live_pilot_execution(
        {
            "schema_id": EVALUATION_LIVE_PILOT_EXECUTION_SCHEMA_ID,
            "schema_version": EVALUATION_LIVE_PILOT_EXECUTION_SCHEMA_VERSION,
            "execution_id": execution_id,
            "created_at": timestamp,
            "producer": {
                "workstream": "evaluation",
                "component": "live_pilot_runner_v1",
                "component_version": "1.0.0",
                "code_commit": code_commit,
            },
            "binding": {
                "project_id": preflight["binding"]["project_id"],
                "document_id": preflight["binding"]["document_id"],
                "config_id": preflight["binding"]["config_id"],
                "config_sha256": preflight["binding"]["config_sha256"],
                "input_set_sha256": preflight["binding"]["input_set_sha256"],
                "plan_id": preflight["binding"]["plan_id"],
                "plan_sha256": preflight["binding"]["plan_sha256"],
                "preflight_sha256": preflight["integrity"]["preflight_sha256"],
                "selected_arm_ids": list(preflight["binding"]["selected_arm_ids"]),
                "evaluation_logical_run_id": logical_run_id,
                "evaluation_attempt_run_id": attempt_run_id,
                "evaluation_profile_id": profile_id,
                "evaluation_profile_sha256": profile_sha256,
                "local_sf_qe": local_sf_qe,
            },
            "coverage": coverage,
            "jobs": job_rows,
            "claim": {
                "scope": "calibration_only",
                "status": "inconclusive",
                "verdict": "INCONCLUSIVE",
                "reason_code": "pilot_not_headline_evidence",
            },
            "integrity": {"execution_sha256": "0" * 64},
        }
    )
    return validate_evaluation_live_pilot_execution_binding(
        sealed,
        common_input,
        config,
        preflight,
        evaluation_logical_run_id=logical_run_id,
        evaluation_attempt_run_id=attempt_run_id,
        evaluation_profile_id=profile_id,
        evaluation_profile_sha256=profile_sha256,
        local_sf_qe_binding=local_sf_qe,
    )


def seal_evaluation_live_pilot_execution(
    payload: Mapping[str, Any],
) -> dict[str, Any]:
    return seal_payload(
        payload,
        policy=PILOT_EXECUTION_POLICY,
        hash_path=PILOT_EXECUTION_SELF_HASH_PATH,
    )


def validate_evaluation_live_pilot_execution(
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
            "claim",
            "integrity",
        },
        path="$",
    )
    normalized = {
        "schema_id": require_enum(
            root["schema_id"],
            {EVALUATION_LIVE_PILOT_EXECUTION_SCHEMA_ID},
            path="$.schema_id",
        ),
        "schema_version": require_enum(
            root["schema_version"],
            {EVALUATION_LIVE_PILOT_EXECUTION_SCHEMA_VERSION},
            path="$.schema_version",
        ),
        "execution_id": require_string(
            root["execution_id"], path="$.execution_id"
        ),
        "created_at": require_rfc3339(root["created_at"], path="$.created_at"),
        "producer": validate_producer(
            root["producer"], path="$.producer", workstream="evaluation"
        ),
        "binding": _validate_binding(root["binding"]),
        "coverage": _validate_coverage(root["coverage"]),
        "jobs": _validate_jobs(root["jobs"]),
        "claim": _validate_claim(root["claim"]),
        "integrity": _validate_integrity(root["integrity"]),
    }
    _validate_internal_consistency(normalized)
    if not verify_payload_hash(
        normalized,
        policy=PILOT_EXECUTION_POLICY,
        hash_path=PILOT_EXECUTION_SELF_HASH_PATH,
    ):
        raise ContractValidationError(
            "pilot_execution_hash",
            "$.integrity.execution_sha256",
            "pilot execution self-hash does not match canonical content",
        )
    canonical = canonicalize(normalized, policy=PILOT_EXECUTION_POLICY)
    if not isinstance(canonical, dict):
        raise AssertionError("canonical pilot execution must remain an object")
    return canonical


def validate_evaluation_live_pilot_execution_binding(
    payload: Mapping[str, Any],
    common_input: CommonEvaluationInputV1,
    config_payload: Mapping[str, Any],
    preflight_payload: Mapping[str, Any],
    *,
    evaluation_logical_run_id: str,
    evaluation_attempt_run_id: str,
    evaluation_profile_id: str,
    evaluation_profile_sha256: str,
    local_sf_qe_binding: Mapping[str, Any],
) -> dict[str, Any]:
    validated = validate_evaluation_live_pilot_execution(payload)
    logical_run_id = require_string(
        evaluation_logical_run_id, path="$.evaluation_logical_run_id"
    )
    attempt_run_id = require_string(
        evaluation_attempt_run_id, path="$.evaluation_attempt_run_id"
    )
    profile_id = require_string(
        evaluation_profile_id, path="$.evaluation_profile_id"
    )
    profile_sha256 = require_sha256(
        evaluation_profile_sha256, path="$.evaluation_profile_sha256"
    )
    local_sf_qe = validate_pilot_local_sf_qe_binding_v1(local_sf_qe_binding)
    config = validate_evaluation_run_config(config_payload)
    preflight = validate_evaluation_live_pilot_preflight_binding(
        preflight_payload, common_input, config
    )
    plan = build_evaluation_plan(common_input, config)
    expected_binding = {
        "project_id": preflight["binding"]["project_id"],
        "document_id": preflight["binding"]["document_id"],
        "config_id": preflight["binding"]["config_id"],
        "config_sha256": preflight["binding"]["config_sha256"],
        "input_set_sha256": preflight["binding"]["input_set_sha256"],
        "plan_id": preflight["binding"]["plan_id"],
        "plan_sha256": preflight["binding"]["plan_sha256"],
        "preflight_sha256": preflight["integrity"]["preflight_sha256"],
        "selected_arm_ids": list(preflight["binding"]["selected_arm_ids"]),
        "evaluation_logical_run_id": logical_run_id,
        "evaluation_attempt_run_id": attempt_run_id,
        "evaluation_profile_id": profile_id,
        "evaluation_profile_sha256": profile_sha256,
        "local_sf_qe": local_sf_qe,
    }
    if validated["binding"] != expected_binding:
        raise ContractValidationError(
            "pilot_execution_binding",
            "$.binding",
            "execution references another input, plan, config or preflight",
        )
    expected_id = "pilot-execution-" + _digest(
        preflight["integrity"]["preflight_sha256"],
        validated["created_at"],
        validated["producer"]["code_commit"],
        logical_run_id,
        attempt_run_id,
        profile_sha256,
        _binding_digest(local_sf_qe),
    )[:24]
    if validated["execution_id"] != expected_id:
        raise ContractValidationError(
            "pilot_execution_id",
            "$.execution_id",
            "execution ID differs from its bound run identity",
        )
    jobs_by_id = {row.job_id: row for row in plan.jobs}
    if len(validated["jobs"]) != len(preflight["jobs"]):
        raise ContractValidationError(
            "pilot_job_exact_cover",
            "$.jobs",
            "execution does not cover every preflight job",
        )
    for index, (row, preflight_job) in enumerate(
        zip(validated["jobs"], preflight["jobs"], strict=True)
    ):
        path = f"$.jobs[{index}]"
        if row["job_id"] != preflight_job["job_id"]:
            raise ContractValidationError(
                "pilot_job_order", path, "execution job order differs from preflight"
            )
        job = jobs_by_id[row["job_id"]]
        _require_job_result_identity(row, job, path=path)
        packet = build_scorer_input_packet(
            common_input,
            plan,
            job.job_id,
            created_at=preflight["created_at"],
            producer_code_commit=preflight["producer"]["code_commit"],
        )
        if (
            row["packet_id"] != packet["packet_id"]
            or row["packet_sha256"] != packet["integrity"]["packet_sha256"]
            or row["packet_sha256"] != preflight_job["packet_sha256"]
        ):
            raise ContractValidationError(
                "pilot_packet_binding", path, "job references stale packet evidence"
            )
    return validated


def _validate_binding(value: Any) -> dict[str, Any]:
    path = "$.binding"
    row = require_mapping(value, path=path)
    required = {
        "project_id",
        "document_id",
        "config_id",
        "config_sha256",
        "input_set_sha256",
        "plan_id",
        "plan_sha256",
        "preflight_sha256",
        "selected_arm_ids",
        "evaluation_logical_run_id",
        "evaluation_attempt_run_id",
        "evaluation_profile_id",
        "evaluation_profile_sha256",
        "local_sf_qe",
    }
    require_exact_keys(row, required=required, path=path)
    arms = [
        require_string(item, path=f"{path}.selected_arm_ids[{index}]")
        for index, item in enumerate(
            require_list(row["selected_arm_ids"], path=f"{path}.selected_arm_ids")
        )
    ]
    require_unique(arms, path=f"{path}.selected_arm_ids")
    if len(arms) != 2:
        raise ContractValidationError(
            "pilot_arm_count", f"{path}.selected_arm_ids", "expected two arms"
        )
    result = {
        field: require_string(row[field], path=f"{path}.{field}")
        for field in required
        if field not in {"selected_arm_ids", "local_sf_qe"}
    }
    for field in (
        "config_sha256",
        "input_set_sha256",
        "plan_sha256",
        "preflight_sha256",
        "evaluation_profile_sha256",
    ):
        result[field] = require_sha256(row[field], path=f"{path}.{field}")
    result["selected_arm_ids"] = arms
    result["local_sf_qe"] = validate_pilot_local_sf_qe_binding_v1(
        row["local_sf_qe"]
    )
    return result


def _validate_executor_binding(value: Any) -> dict[str, Any]:
    path = "$.executor.execution_binding"
    row = require_mapping(value, path=path)
    require_exact_keys(
        row,
        required={
            "evaluation_logical_run_id",
            "evaluation_attempt_run_id",
            "evaluation_profile_id",
            "evaluation_profile_sha256",
            "local_sf_qe",
        },
        path=path,
    )
    return {
        "evaluation_logical_run_id": require_string(
            row["evaluation_logical_run_id"],
            path=f"{path}.evaluation_logical_run_id",
        ),
        "evaluation_attempt_run_id": require_string(
            row["evaluation_attempt_run_id"],
            path=f"{path}.evaluation_attempt_run_id",
        ),
        "evaluation_profile_id": require_string(
            row["evaluation_profile_id"], path=f"{path}.evaluation_profile_id"
        ),
        "evaluation_profile_sha256": require_sha256(
            row["evaluation_profile_sha256"],
            path=f"{path}.evaluation_profile_sha256",
        ),
        "local_sf_qe": validate_pilot_local_sf_qe_binding_v1(
            row["local_sf_qe"]
        ),
    }


def _validate_coverage(value: Any) -> dict[str, Any]:
    path = "$.coverage"
    row = require_mapping(value, path=path)
    require_exact_keys(
        row,
        required={
            "selected_unit_count",
            "selected_job_count",
            "succeeded_job_count",
            "failed_job_count",
            "method_job_counts",
        },
        path=path,
    )
    method_counts = require_mapping(
        row["method_job_counts"], path=f"{path}.method_job_counts"
    )
    require_exact_keys(method_counts, required=_METHOD_IDS, path=f"{path}.method_job_counts")
    return {
        "selected_unit_count": require_int(
            row["selected_unit_count"], path=f"{path}.selected_unit_count", minimum=1
        ),
        "selected_job_count": require_int(
            row["selected_job_count"], path=f"{path}.selected_job_count", minimum=1
        ),
        "succeeded_job_count": require_int(
            row["succeeded_job_count"], path=f"{path}.succeeded_job_count", minimum=0
        ),
        "failed_job_count": require_int(
            row["failed_job_count"], path=f"{path}.failed_job_count", minimum=0
        ),
        "method_job_counts": {
            method_id: require_int(
                method_counts[method_id],
                path=f"{path}.method_job_counts.{method_id}",
                minimum=0,
            )
            for method_id in sorted(_METHOD_IDS)
        },
    }


def _validate_jobs(value: Any) -> list[dict[str, Any]]:
    path = "$.jobs"
    result: list[dict[str, Any]] = []
    for index, raw in enumerate(require_list(value, path=path)):
        item_path = f"{path}[{index}]"
        row = require_mapping(raw, path=item_path)
        require_exact_keys(
            row,
            required={
                "job_id",
                "unit_id",
                "method_id",
                "method_version",
                "scorer_kind",
                "presentation_arm_ids",
                "packet_id",
                "packet_sha256",
                "status",
                "semantic_output",
                "error_code",
            },
            path=item_path,
        )
        method_id = require_enum(
            row["method_id"], _METHOD_IDS, path=f"{item_path}.method_id"
        )
        observation = validate_evaluation_job_observation_v1(
            {
                "status": row["status"],
                "semantic_output": row["semantic_output"],
                "error_code": row["error_code"],
            },
            method_id=method_id,
        )
        arms = [
            require_string(item, path=f"{item_path}.presentation_arm_ids[{arm_index}]")
            for arm_index, item in enumerate(
                require_list(
                    row["presentation_arm_ids"],
                    path=f"{item_path}.presentation_arm_ids",
                )
            )
        ]
        require_unique(arms, path=f"{item_path}.presentation_arm_ids")
        result.append(
            {
                "job_id": require_string(row["job_id"], path=f"{item_path}.job_id"),
                "unit_id": require_string(row["unit_id"], path=f"{item_path}.unit_id"),
                "method_id": method_id,
                "method_version": require_string(
                    row["method_version"], path=f"{item_path}.method_version"
                ),
                "scorer_kind": require_enum(
                    row["scorer_kind"],
                    {"unary", "pairwise"},
                    path=f"{item_path}.scorer_kind",
                ),
                "presentation_arm_ids": arms,
                "packet_id": require_string(
                    row["packet_id"], path=f"{item_path}.packet_id"
                ),
                "packet_sha256": require_sha256(
                    row["packet_sha256"], path=f"{item_path}.packet_sha256"
                ),
                **observation,
            }
        )
    require_unique([row["job_id"] for row in result], path=f"{path}.job_id")
    return result


def _validate_claim(value: Any) -> dict[str, str]:
    path = "$.claim"
    row = require_mapping(value, path=path)
    require_exact_keys(
        row, required={"scope", "status", "verdict", "reason_code"}, path=path
    )
    return {
        "scope": require_enum(row["scope"], {"calibration_only"}, path=f"{path}.scope"),
        "status": require_enum(row["status"], {"inconclusive"}, path=f"{path}.status"),
        "verdict": require_enum(row["verdict"], {"INCONCLUSIVE"}, path=f"{path}.verdict"),
        "reason_code": require_enum(
            row["reason_code"],
            {"pilot_not_headline_evidence"},
            path=f"{path}.reason_code",
        ),
    }


def _validate_integrity(value: Any) -> dict[str, str]:
    path = "$.integrity"
    row = require_mapping(value, path=path)
    require_exact_keys(row, required={"execution_sha256"}, path=path)
    return {
        "execution_sha256": require_sha256(
            row["execution_sha256"], path=f"{path}.execution_sha256"
        )
    }


def _validate_internal_consistency(payload: Mapping[str, Any]) -> None:
    jobs = payload["jobs"]
    coverage = payload["coverage"]
    expected = _derive_coverage(jobs)
    if coverage != expected:
        raise ContractValidationError(
            "pilot_execution_coverage",
            "$.coverage",
            "coverage does not match executed job rows",
        )
    sf_qe_rows = [row for row in jobs if row["method_id"] == "sf_qe"]
    local_binding = payload["binding"]["local_sf_qe"]
    if local_binding["selected_job_count"] != len(sf_qe_rows):
        raise ContractValidationError(
            "sf_qe_binding_count",
            "$.binding.local_sf_qe.selected_job_count",
            "local SF-QE binding does not cover every pilot SF-QE job",
        )
    packet_set_sha256 = _sequence_digest(
        [row["packet_sha256"] for row in sf_qe_rows]
    )
    if local_binding["packet_set_sha256"] != packet_set_sha256:
        raise ContractValidationError(
            "sf_qe_packet_set",
            "$.binding.local_sf_qe.packet_set_sha256",
            "local SF-QE packet set differs from executed pilot rows",
        )
    if any(row["status"] != "succeeded" for row in sf_qe_rows):
        raise ContractValidationError(
            "sf_qe_result_cover",
            "$.jobs",
            "sealed local SF-QE rows must all produce accepted score observations",
        )
    score_set_sha256 = _sequence_digest(
        [row["semantic_output"]["score"] for row in sf_qe_rows]
    )
    if local_binding["score_set_sha256"] != score_set_sha256:
        raise ContractValidationError(
            "sf_qe_score_set",
            "$.binding.local_sf_qe.score_set_sha256",
            "local SF-QE score set differs from executed pilot rows",
        )


def _derive_coverage(jobs: list[dict[str, Any]]) -> dict[str, Any]:
    methods = Counter(row["method_id"] for row in jobs)
    return {
        "selected_unit_count": len({row["unit_id"] for row in jobs}),
        "selected_job_count": len(jobs),
        "succeeded_job_count": sum(row["status"] == "succeeded" for row in jobs),
        "failed_job_count": sum(row["status"] == "failed" for row in jobs),
        "method_job_counts": {
            method_id: methods[method_id] for method_id in sorted(_METHOD_IDS)
        },
    }


def _job_result_row(
    job: EvaluationJobV1,
    packet: Mapping[str, Any],
    observation: Mapping[str, Any],
) -> dict[str, Any]:
    return {
        "job_id": job.job_id,
        "unit_id": job.unit_id,
        "method_id": job.method_id,
        "method_version": job.method_version,
        "scorer_kind": job.scorer_kind,
        "presentation_arm_ids": list(job.presentation_arm_ids),
        "packet_id": packet["packet_id"],
        "packet_sha256": packet["integrity"]["packet_sha256"],
        "status": observation["status"],
        "semantic_output": copy.deepcopy(observation["semantic_output"]),
        "error_code": observation["error_code"],
    }


def _require_preflight_job_identity(
    row: Mapping[str, Any], job: EvaluationJobV1, *, path: str
) -> None:
    expected = {
        "job_id": job.job_id,
        "unit_id": job.unit_id,
        "method_id": job.method_id,
        "presentation_arm_count": len(job.presentation_arm_ids),
    }
    if any(row[field] != value for field, value in expected.items()):
        raise ContractValidationError(
            "pilot_job_identity", path, "preflight job differs from the sealed plan"
        )


def _require_job_result_identity(
    row: Mapping[str, Any], job: EvaluationJobV1, *, path: str
) -> None:
    expected = {
        "job_id": job.job_id,
        "unit_id": job.unit_id,
        "method_id": job.method_id,
        "method_version": job.method_version,
        "scorer_kind": job.scorer_kind,
        "presentation_arm_ids": list(job.presentation_arm_ids),
    }
    if any(row[field] != value for field, value in expected.items()):
        raise ContractValidationError(
            "pilot_job_identity", path, "execution job differs from the sealed plan"
        )


def _digest(*parts: str) -> str:
    return hashlib.sha256("\x1f".join(parts).encode("utf-8")).hexdigest()


def _binding_digest(value: Mapping[str, Any]) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _sequence_digest(value: list[Any]) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()
