from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

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
    require_relative_path,
    require_rfc3339,
    require_sha256,
    require_string,
    seal_payload,
    validate_producer,
    verify_payload_hash,
)
from pipeline.eval.offline_orchestrator_v1 import (
    EVALUATION_PLAN_POLICY,
    EvaluationJobV1,
    EvaluationPlanV1,
    build_evaluation_plan,
    evaluation_plan_to_dict,
    validate_evaluation_run_config,
)


__all__ = [
    "OfflineFixtureRunSummaryV1",
    "run_offline_fixture_evaluation",
]


PLAN_ARTIFACT_SCHEMA_ID = "EvaluationPlanArtifactV1"
PLAN_ARTIFACT_SCHEMA_VERSION = "1.0.0"
CHECKPOINT_SCHEMA_ID = "EvaluationCheckpointV1"
CHECKPOINT_SCHEMA_VERSION = "1.0.0"
ATTEMPT_MANIFEST_SCHEMA_ID = "EvaluationAttemptManifestV1"
ATTEMPT_MANIFEST_SCHEMA_VERSION = "1.0.0"

_PLAN_HASH_PATH = ("integrity", "artifact_sha256")
_CHECKPOINT_HASH_PATH = ("integrity", "checkpoint_sha256")
_ATTEMPT_HASH_PATH = ("integrity", "manifest_sha256")

_PLAN_ARTIFACT_POLICY = CanonicalPolicy(
    set_like_paths=frozenset(
        {("plan",) + path for path in EVALUATION_PLAN_POLICY.set_like_paths}
    ),
    semantic_sequence_paths=frozenset(
        {("plan",) + path for path in EVALUATION_PLAN_POLICY.semantic_sequence_paths}
    ),
)
_CHECKPOINT_POLICY = CanonicalPolicy(
    set_like_paths=frozenset(),
    semantic_sequence_paths=frozenset({("jobs",)}),
)
_ATTEMPT_POLICY = CanonicalPolicy(
    set_like_paths=frozenset(),
    semantic_sequence_paths=frozenset(),
)

_SAFE_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,127}$")
_ATTEMPT_DIR_RE = re.compile(r"^attempt-(\d{4})$")
_FIXTURE_FAILURE_KINDS = frozenset(
    {"transport_failure", "response_contract_failure"}
)


@dataclass(frozen=True, slots=True)
class OfflineFixtureRunSummaryV1:
    plan_id: str
    plan_sha256: str
    status: str
    ready_job_count: int
    blocked_job_count: int
    succeeded_job_count: int
    exhausted_job_count: int
    pending_job_count: int
    attempt_count: int
    checkpoint_sha256: str
    run_root: str


@dataclass(frozen=True, slots=True)
class _AttemptStateV1:
    attempt_index: int
    attempt_id: str
    status: str
    complete: bool


@dataclass(frozen=True, slots=True)
class _JobDiskStateV1:
    status: str
    complete_attempt_count: int
    incomplete_attempt_count: int
    latest_attempt_id: str | None
    next_attempt_index: int

    @property
    def attempt_count(self) -> int:
        return self.complete_attempt_count + self.incomplete_attempt_count


def run_offline_fixture_evaluation(
    common_input: CommonEvaluationInputV1,
    config_payload: Mapping[str, Any],
    run_root: str | Path,
    *,
    runner_code_commit: str,
    failure_schedule: Mapping[tuple[str, int], str] | None = None,
    max_jobs: int | None = None,
) -> OfflineFixtureRunSummaryV1:
    """Execute a deterministic 0-API fixture run with crash-safe resume."""

    code_commit = require_commit(runner_code_commit, path="$.runner_code_commit")
    if max_jobs is not None:
        max_jobs = require_int(max_jobs, path="$.max_jobs", minimum=1)
    schedule = _validate_failure_schedule(failure_schedule or {})
    root = _resolve_run_root(run_root)
    config = validate_evaluation_run_config(config_payload)
    plan = build_evaluation_plan(common_input, config)
    _validate_failure_schedule_against_plan(
        schedule,
        plan,
        max_attempts=config["retry_policy"]["max_transport_attempts"],
    )

    _ensure_immutable_json(root / "run_config.json", config)
    plan_artifact = _seal_plan_artifact(
        plan,
        created_at=config["created_at"],
        runner_code_commit=code_commit,
    )
    _ensure_plan_artifact(root / "evaluation_plan.json", plan_artifact, plan)
    checkpoint = _reconcile_checkpoint(root, plan, config)

    newly_terminal = 0
    for job in plan.jobs:
        if job.status == "blocked":
            continue
        state = _scan_job_state(
            root,
            plan,
            job,
            max_attempts=config["retry_policy"]["max_transport_attempts"],
        )
        if state.status in {"succeeded", "exhausted"}:
            continue
        if max_jobs is not None and newly_terminal >= max_jobs:
            break

        while state.status == "pending":
            attempt_index = state.next_attempt_index
            failure_kind = schedule.get((job.job_id, attempt_index))
            _write_fixture_attempt(
                root,
                plan,
                job,
                attempt_index=attempt_index,
                failure_kind=failure_kind,
            )
            checkpoint = _reconcile_checkpoint(root, plan, config)
            state = _scan_job_state(
                root,
                plan,
                job,
                max_attempts=config["retry_policy"]["max_transport_attempts"],
            )
        newly_terminal += 1

    checkpoint = _reconcile_checkpoint(root, plan, config)
    return _build_summary(root, plan, checkpoint)


def _seal_plan_artifact(
    plan: EvaluationPlanV1,
    *,
    created_at: str,
    runner_code_commit: str,
) -> dict[str, Any]:
    return seal_payload(
        {
            "schema_id": PLAN_ARTIFACT_SCHEMA_ID,
            "schema_version": PLAN_ARTIFACT_SCHEMA_VERSION,
            "created_at": created_at,
            "producer": {
                "workstream": "evaluation",
                "component": "offline_runner_v1",
                "component_version": "1.0.0",
                "code_commit": runner_code_commit,
            },
            "plan": evaluation_plan_to_dict(plan),
            "integrity": {"artifact_sha256": "0" * 64},
        },
        policy=_PLAN_ARTIFACT_POLICY,
        hash_path=_PLAN_HASH_PATH,
    )


def _validate_plan_artifact(
    payload: Mapping[str, Any], expected_plan: EvaluationPlanV1
) -> dict[str, Any]:
    root = require_mapping(payload, path="$")
    require_exact_keys(
        root,
        required={
            "schema_id",
            "schema_version",
            "created_at",
            "producer",
            "plan",
            "integrity",
        },
        path="$",
    )
    integrity = require_mapping(root["integrity"], path="$.integrity")
    require_exact_keys(
        integrity,
        required={"artifact_sha256"},
        path="$.integrity",
    )
    normalized: dict[str, Any] = {
        "schema_id": require_enum(
            root["schema_id"], {PLAN_ARTIFACT_SCHEMA_ID}, path="$.schema_id"
        ),
        "schema_version": require_enum(
            root["schema_version"],
            {PLAN_ARTIFACT_SCHEMA_VERSION},
            path="$.schema_version",
        ),
        "created_at": require_rfc3339(root["created_at"], path="$.created_at"),
        "producer": validate_producer(
            root["producer"],
            path="$.producer",
            workstream="evaluation",
        ),
        "plan": copy.deepcopy(require_mapping(root["plan"], path="$.plan")),
        "integrity": {
            "artifact_sha256": require_sha256(
                integrity["artifact_sha256"],
                path="$.integrity.artifact_sha256",
            )
        },
    }
    if not verify_payload_hash(
        normalized,
        policy=_PLAN_ARTIFACT_POLICY,
        hash_path=_PLAN_HASH_PATH,
    ):
        raise ContractValidationError(
            "plan_artifact_hash",
            "$.integrity.artifact_sha256",
            "plan artifact self-hash does not match content",
        )
    canonical = canonicalize(normalized, policy=_PLAN_ARTIFACT_POLICY)
    expected = canonicalize(
        evaluation_plan_to_dict(expected_plan),
        policy=EVALUATION_PLAN_POLICY,
    )
    if canonical["plan"] != expected:
        raise ContractValidationError(
            "plan_artifact_binding",
            "$.plan",
            "persisted plan differs from the current sealed inputs and config",
        )
    return canonical


def _ensure_plan_artifact(
    path: Path,
    expected_payload: Mapping[str, Any],
    expected_plan: EvaluationPlanV1,
) -> None:
    if path.exists():
        loaded = _load_json_object(path)
        validated = _validate_plan_artifact(loaded, expected_plan)
        expected = _validate_plan_artifact(expected_payload, expected_plan)
        if validated != expected:
            raise ContractValidationError(
                "immutable_conflict",
                str(path),
                "existing plan artifact differs from the requested runner identity",
            )
        return
    _write_json_atomic(path, expected_payload)


def _reconcile_checkpoint(
    root: Path,
    plan: EvaluationPlanV1,
    config: Mapping[str, Any],
) -> dict[str, Any]:
    max_attempts = config["retry_policy"]["max_transport_attempts"]
    desired_jobs = [
        _checkpoint_job_row(
            job,
            _scan_job_state(
                root,
                plan,
                job,
                max_attempts=max_attempts,
            ),
        )
        for job in plan.jobs
    ]
    path = root / "checkpoint.json"
    previous: dict[str, Any] | None = None
    if path.exists():
        previous = _validate_checkpoint(_load_json_object(path), plan)
        if _checkpoint_state(previous) == desired_jobs:
            return previous
    generation = 0 if previous is None else previous["generation"] + 1
    payload = seal_payload(
        {
            "schema_id": CHECKPOINT_SCHEMA_ID,
            "schema_version": CHECKPOINT_SCHEMA_VERSION,
            "plan_id": plan.plan_id,
            "plan_sha256": plan.plan_sha256,
            "config_sha256": plan.config_sha256,
            "input_set_sha256": plan.input_set_sha256,
            "generation": generation,
            "jobs": desired_jobs,
            "integrity": {"checkpoint_sha256": "0" * 64},
        },
        policy=_CHECKPOINT_POLICY,
        hash_path=_CHECKPOINT_HASH_PATH,
    )
    _write_json_atomic(path, payload, replace=True)
    return _validate_checkpoint(payload, plan)


def _validate_checkpoint(
    payload: Mapping[str, Any],
    plan: EvaluationPlanV1,
) -> dict[str, Any]:
    root = require_mapping(payload, path="$")
    require_exact_keys(
        root,
        required={
            "schema_id",
            "schema_version",
            "plan_id",
            "plan_sha256",
            "config_sha256",
            "input_set_sha256",
            "generation",
            "jobs",
            "integrity",
        },
        path="$",
    )
    rows = require_list(root["jobs"], path="$.jobs")
    expected_jobs = {job.job_id: job for job in plan.jobs}
    normalized_rows: list[dict[str, Any]] = []
    for index, raw_row in enumerate(rows):
        path = f"$.jobs[{index}]"
        row = require_mapping(raw_row, path=path)
        require_exact_keys(
            row,
            required={
                "job_id",
                "planner_status",
                "execution_status",
                "complete_attempt_count",
                "incomplete_attempt_count",
                "latest_attempt_id",
            },
            path=path,
        )
        job_id = require_string(row["job_id"], path=f"{path}.job_id")
        job = expected_jobs.get(job_id)
        if job is None:
            raise ContractValidationError(
                "checkpoint_job",
                f"{path}.job_id",
                "checkpoint references an unknown job",
            )
        planner_status = require_enum(
            row["planner_status"],
            {"ready", "blocked"},
            path=f"{path}.planner_status",
        )
        if planner_status != job.status:
            raise ContractValidationError(
                "checkpoint_job",
                f"{path}.planner_status",
                "checkpoint planner status differs from plan",
            )
        execution_status = require_enum(
            row["execution_status"],
            {"blocked", "pending", "succeeded", "exhausted"},
            path=f"{path}.execution_status",
        )
        normalized_rows.append(
            {
                "job_id": job_id,
                "planner_status": planner_status,
                "execution_status": execution_status,
                "complete_attempt_count": require_int(
                    row["complete_attempt_count"],
                    path=f"{path}.complete_attempt_count",
                    minimum=0,
                ),
                "incomplete_attempt_count": require_int(
                    row["incomplete_attempt_count"],
                    path=f"{path}.incomplete_attempt_count",
                    minimum=0,
                ),
                "latest_attempt_id": require_nullable_string(
                    row["latest_attempt_id"],
                    path=f"{path}.latest_attempt_id",
                ),
            }
        )
    if [row["job_id"] for row in normalized_rows] != [
        job.job_id for job in plan.jobs
    ]:
        raise ContractValidationError(
            "checkpoint_job_order",
            "$.jobs",
            "checkpoint must exact-cover plan jobs in plan order",
        )
    integrity = require_mapping(root["integrity"], path="$.integrity")
    require_exact_keys(
        integrity,
        required={"checkpoint_sha256"},
        path="$.integrity",
    )
    normalized = {
        "schema_id": require_enum(
            root["schema_id"], {CHECKPOINT_SCHEMA_ID}, path="$.schema_id"
        ),
        "schema_version": require_enum(
            root["schema_version"],
            {CHECKPOINT_SCHEMA_VERSION},
            path="$.schema_version",
        ),
        "plan_id": require_enum(
            root["plan_id"], {plan.plan_id}, path="$.plan_id"
        ),
        "plan_sha256": require_enum(
            root["plan_sha256"], {plan.plan_sha256}, path="$.plan_sha256"
        ),
        "config_sha256": require_enum(
            root["config_sha256"], {plan.config_sha256}, path="$.config_sha256"
        ),
        "input_set_sha256": require_enum(
            root["input_set_sha256"],
            {plan.input_set_sha256},
            path="$.input_set_sha256",
        ),
        "generation": require_int(
            root["generation"], path="$.generation", minimum=0
        ),
        "jobs": normalized_rows,
        "integrity": {
            "checkpoint_sha256": require_sha256(
                integrity["checkpoint_sha256"],
                path="$.integrity.checkpoint_sha256",
            )
        },
    }
    if not verify_payload_hash(
        normalized,
        policy=_CHECKPOINT_POLICY,
        hash_path=_CHECKPOINT_HASH_PATH,
    ):
        raise ContractValidationError(
            "checkpoint_hash",
            "$.integrity.checkpoint_sha256",
            "checkpoint self-hash does not match content",
        )
    canonical = canonicalize(normalized, policy=_CHECKPOINT_POLICY)
    if not isinstance(canonical, dict):
        raise AssertionError("canonical checkpoint must remain an object")
    return canonical


def _checkpoint_state(checkpoint: Mapping[str, Any]) -> list[dict[str, Any]]:
    return copy.deepcopy(checkpoint["jobs"])


def _checkpoint_job_row(
    job: EvaluationJobV1,
    state: _JobDiskStateV1,
) -> dict[str, Any]:
    return {
        "job_id": job.job_id,
        "planner_status": job.status,
        "execution_status": "blocked" if job.status == "blocked" else state.status,
        "complete_attempt_count": state.complete_attempt_count,
        "incomplete_attempt_count": state.incomplete_attempt_count,
        "latest_attempt_id": state.latest_attempt_id,
    }


def _scan_job_state(
    root: Path,
    plan: EvaluationPlanV1,
    job: EvaluationJobV1,
    *,
    max_attempts: int,
) -> _JobDiskStateV1:
    if job.status == "blocked":
        return _JobDiskStateV1("blocked", 0, 0, None, 1)
    job_dir = _safe_child(root, "jobs", _safe_id(job.job_id))
    if not job_dir.exists():
        return _JobDiskStateV1("pending", 0, 0, None, 1)

    attempts: list[_AttemptStateV1] = []
    for child in sorted(job_dir.iterdir(), key=lambda value: value.name):
        if not child.is_dir():
            raise ContractValidationError(
                "job_directory",
                str(child),
                "job directory may contain attempt directories only",
            )
        match = _ATTEMPT_DIR_RE.fullmatch(child.name)
        if match is None:
            raise ContractValidationError(
                "attempt_directory",
                str(child),
                "attempt directory name is invalid",
            )
        attempt_index = int(match.group(1))
        manifest_path = child / "manifest.json"
        if not manifest_path.exists():
            attempts.append(
                _AttemptStateV1(
                    attempt_index=attempt_index,
                    attempt_id=f"{job.job_id}-attempt-{attempt_index:04d}",
                    status="incomplete",
                    complete=False,
                )
            )
            continue
        manifest = _validate_attempt_manifest(
            _load_json_object(manifest_path),
            root=root,
            plan=plan,
            job=job,
            attempt_index=attempt_index,
        )
        attempts.append(
            _AttemptStateV1(
                attempt_index=attempt_index,
                attempt_id=manifest["attempt_id"],
                status=manifest["status"],
                complete=True,
            )
        )

    indexes = [attempt.attempt_index for attempt in attempts]
    if len(indexes) != len(set(indexes)):
        raise ContractValidationError(
            "attempt_duplicate",
            str(job_dir),
            "attempt indexes must be unique",
        )
    if indexes != list(range(1, len(indexes) + 1)):
        raise ContractValidationError(
            "attempt_sequence",
            str(job_dir),
            "attempt indexes must be contiguous and start at one",
        )
    if len(attempts) > max_attempts:
        raise ContractValidationError(
            "attempt_cap",
            str(job_dir),
            "attempt count exceeds the sealed retry cap",
        )
    successful = [attempt for attempt in attempts if attempt.status == "succeeded"]
    if len(successful) > 1:
        raise ContractValidationError(
            "attempt_success",
            str(job_dir),
            "a job may have only one successful attempt",
        )
    if successful and any(
        attempt.attempt_index > successful[0].attempt_index for attempt in attempts
    ):
        raise ContractValidationError(
            "attempt_after_success",
            str(job_dir),
            "attempts may not continue after success",
        )

    complete_count = sum(attempt.complete for attempt in attempts)
    incomplete_count = len(attempts) - complete_count
    latest = max(attempts, key=lambda row: row.attempt_index) if attempts else None
    next_index = 1 if latest is None else latest.attempt_index + 1
    if successful:
        status = "succeeded"
    elif len(attempts) >= max_attempts:
        status = "exhausted"
    else:
        status = "pending"
    return _JobDiskStateV1(
        status=status,
        complete_attempt_count=complete_count,
        incomplete_attempt_count=incomplete_count,
        latest_attempt_id=None if latest is None else latest.attempt_id,
        next_attempt_index=next_index,
    )


def _write_fixture_attempt(
    root: Path,
    plan: EvaluationPlanV1,
    job: EvaluationJobV1,
    *,
    attempt_index: int,
    failure_kind: str | None,
) -> None:
    if failure_kind is not None and failure_kind not in _FIXTURE_FAILURE_KINDS:
        raise ValueError(f"unsupported fixture failure kind: {failure_kind}")
    attempt_id = f"{job.job_id}-attempt-{attempt_index:04d}"
    attempt_dir = _safe_child(
        root,
        "jobs",
        _safe_id(job.job_id),
        f"attempt-{attempt_index:04d}",
    )
    if attempt_dir.exists():
        raise ContractValidationError(
            "attempt_conflict",
            str(attempt_dir),
            "runner refuses to overwrite an existing attempt directory",
        )
    try:
        attempt_dir.mkdir(parents=True)
    except FileExistsError as exc:
        raise ContractValidationError(
            "attempt_conflict",
            str(attempt_dir),
            "another writer claimed this attempt directory",
        ) from exc

    if failure_kind == "transport_failure":
        status = "transport_failed"
    elif failure_kind == "response_contract_failure":
        status = "response_contract_failed"
    else:
        status = "succeeded"
    failure_code = _fixture_failure_code(status)
    expected_artifacts = _fixture_artifact_payloads(
        plan,
        job,
        attempt_index=attempt_index,
        status=status,
    )
    artifact_refs: dict[str, dict[str, str] | None] = {}
    for name, payload in expected_artifacts.items():
        artifact_refs[name] = (
            None
            if payload is None
            else _write_attempt_artifact(
                root,
                attempt_dir / f"{name}.json",
                payload,
            )
        )

    manifest = seal_payload(
        {
            "schema_id": ATTEMPT_MANIFEST_SCHEMA_ID,
            "schema_version": ATTEMPT_MANIFEST_SCHEMA_VERSION,
            "attempt_id": attempt_id,
            "plan_id": plan.plan_id,
            "plan_sha256": plan.plan_sha256,
            "job_id": job.job_id,
            "attempt_index": attempt_index,
            "executor": {
                "executor_id": "offline_fixture_executor",
                "executor_version": "1.0.0",
            },
            "status": status,
            "failure_code": failure_code,
            "artifacts": artifact_refs,
            "integrity": {"manifest_sha256": "0" * 64},
        },
        policy=_ATTEMPT_POLICY,
        hash_path=_ATTEMPT_HASH_PATH,
    )
    _write_json_atomic(attempt_dir / "manifest.json", manifest)


def _fixture_failure_code(status: str) -> str | None:
    return {
        "succeeded": None,
        "transport_failed": "fixture_transport_failure",
        "response_contract_failed": "fixture_response_contract_failure",
    }[status]


def _fixture_artifact_payloads(
    plan: EvaluationPlanV1,
    job: EvaluationJobV1,
    *,
    attempt_index: int,
    status: str,
) -> dict[str, dict[str, Any] | None]:
    request = {
        "schema_id": "FixtureEvaluationRequestV1",
        "schema_version": "1.0.0",
        "plan_id": plan.plan_id,
        "job_id": job.job_id,
        "attempt_index": attempt_index,
        "method_id": job.method_id,
        "method_version": job.method_version,
        "scorer_kind": job.scorer_kind,
        "unit_id": job.unit_id,
        "opaque_candidate_slots": [
            f"candidate_{index + 1}"
            for index in range(len(job.presentation_arm_ids))
        ],
    }
    usage = {
        "schema_id": "FixtureUsageV1",
        "schema_version": "1.0.0",
        "accounting_basis": "fixture_zero",
        "input_tokens": 0,
        "output_tokens": 0,
        "total_tokens": 0,
    }
    if status == "transport_failed":
        response = None
        result = None
    elif status == "response_contract_failed":
        response = {"malformed_fixture_response": True}
        result = None
    else:
        response = {
            "schema_id": "FixtureEvaluationResponseV1",
            "schema_version": "1.0.0",
            "fixture_outcome": "accepted",
        }
        result = {
            "schema_id": "FixtureEvaluationResultV1",
            "schema_version": "1.0.0",
            "job_id": job.job_id,
            "status": "fixture_succeeded",
        }
    return {
        "request": request,
        "response": response,
        "result": result,
        "usage": usage,
    }


def _validate_attempt_manifest(
    payload: Mapping[str, Any],
    *,
    root: Path,
    plan: EvaluationPlanV1,
    job: EvaluationJobV1,
    attempt_index: int,
) -> dict[str, Any]:
    data = require_mapping(payload, path="$")
    require_exact_keys(
        data,
        required={
            "schema_id",
            "schema_version",
            "attempt_id",
            "plan_id",
            "plan_sha256",
            "job_id",
            "attempt_index",
            "executor",
            "status",
            "failure_code",
            "artifacts",
            "integrity",
        },
        path="$",
    )
    executor = require_mapping(data["executor"], path="$.executor")
    require_exact_keys(
        executor,
        required={"executor_id", "executor_version"},
        path="$.executor",
    )
    artifacts = require_mapping(data["artifacts"], path="$.artifacts")
    require_exact_keys(
        artifacts,
        required={"request", "response", "result", "usage"},
        path="$.artifacts",
    )
    status = require_enum(
        data["status"],
        {"succeeded", "transport_failed", "response_contract_failed"},
        path="$.status",
    )
    normalized_artifacts = {
        key: _validate_artifact_ref(
            artifacts[key],
            path=f"$.artifacts.{key}",
            nullable=key in {"response", "result"},
        )
        for key in ("request", "response", "result", "usage")
    }
    if status == "succeeded":
        if (
            normalized_artifacts["response"] is None
            or normalized_artifacts["result"] is None
        ):
            raise ContractValidationError(
                "attempt_artifact",
                "$.artifacts",
                "successful attempt requires response and result artifacts",
            )
    elif status == "transport_failed":
        if (
            normalized_artifacts["response"] is not None
            or normalized_artifacts["result"] is not None
        ):
            raise ContractValidationError(
                "attempt_artifact",
                "$.artifacts",
                "transport failure cannot claim response or result artifacts",
            )
    elif (
        normalized_artifacts["response"] is None
        or normalized_artifacts["result"] is not None
    ):
        raise ContractValidationError(
            "attempt_artifact",
            "$.artifacts",
            "response contract failure requires response and no result",
        )
    integrity = require_mapping(data["integrity"], path="$.integrity")
    require_exact_keys(
        integrity,
        required={"manifest_sha256"},
        path="$.integrity",
    )
    expected_attempt_id = f"{job.job_id}-attempt-{attempt_index:04d}"
    expected_failure_code = _fixture_failure_code(status)
    if expected_failure_code is None:
        if data["failure_code"] is not None:
            raise ContractValidationError(
                "attempt_failure",
                "$.failure_code",
                "successful attempt cannot carry a failure code",
            )
        failure_code = None
    else:
        failure_code = require_enum(
            data["failure_code"],
            {expected_failure_code},
            path="$.failure_code",
        )
    normalized = {
        "schema_id": require_enum(
            data["schema_id"],
            {ATTEMPT_MANIFEST_SCHEMA_ID},
            path="$.schema_id",
        ),
        "schema_version": require_enum(
            data["schema_version"],
            {ATTEMPT_MANIFEST_SCHEMA_VERSION},
            path="$.schema_version",
        ),
        "attempt_id": require_enum(
            data["attempt_id"], {expected_attempt_id}, path="$.attempt_id"
        ),
        "plan_id": require_enum(
            data["plan_id"], {plan.plan_id}, path="$.plan_id"
        ),
        "plan_sha256": require_enum(
            data["plan_sha256"], {plan.plan_sha256}, path="$.plan_sha256"
        ),
        "job_id": require_enum(data["job_id"], {job.job_id}, path="$.job_id"),
        "attempt_index": require_int(
            data["attempt_index"], path="$.attempt_index", minimum=1
        ),
        "executor": {
            "executor_id": require_enum(
                executor["executor_id"],
                {"offline_fixture_executor"},
                path="$.executor.executor_id",
            ),
            "executor_version": require_enum(
                executor["executor_version"],
                {"1.0.0"},
                path="$.executor.executor_version",
            ),
        },
        "status": status,
        "failure_code": failure_code,
        "artifacts": normalized_artifacts,
        "integrity": {
            "manifest_sha256": require_sha256(
                integrity["manifest_sha256"],
                path="$.integrity.manifest_sha256",
            )
        },
    }
    if normalized["attempt_index"] != attempt_index:
        raise ContractValidationError(
            "attempt_index",
            "$.attempt_index",
            "manifest attempt index differs from directory",
        )
    if not verify_payload_hash(
        normalized,
        policy=_ATTEMPT_POLICY,
        hash_path=_ATTEMPT_HASH_PATH,
    ):
        raise ContractValidationError(
            "attempt_manifest_hash",
            "$.integrity.manifest_sha256",
            "attempt manifest self-hash does not match content",
        )
    canonical = canonicalize(normalized, policy=_ATTEMPT_POLICY)
    expected_dir = _safe_child(
        root,
        "jobs",
        _safe_id(job.job_id),
        f"attempt-{attempt_index:04d}",
    )
    expected_payloads = _fixture_artifact_payloads(
        plan,
        job,
        attempt_index=attempt_index,
        status=status,
    )
    for name, ref in canonical["artifacts"].items():
        expected_payload = expected_payloads[name]
        if ref is None:
            if expected_payload is not None:
                raise ContractValidationError(
                    "attempt_artifact",
                    f"$.artifacts.{name}",
                    "required fixture artifact is absent",
                )
            continue
        expected_path = (expected_dir / f"{name}.json").relative_to(root).as_posix()
        if ref["path"] != expected_path:
            raise ContractValidationError(
                "attempt_artifact_path",
                f"$.artifacts.{name}.path",
                "attempt artifact reference points outside its fixed slot",
            )
        artifact_path = _safe_child(root, *Path(ref["path"]).parts)
        if not artifact_path.is_file():
            raise ContractValidationError(
                "attempt_artifact_missing",
                f"$.artifacts.{name}.path",
                "referenced attempt artifact is missing",
            )
        if _file_sha256(artifact_path) != ref["sha256"]:
            raise ContractValidationError(
                "attempt_artifact_hash",
                f"$.artifacts.{name}.sha256",
                "referenced attempt artifact hash does not match bytes",
            )
        if expected_payload is None or _load_json_object(artifact_path) != expected_payload:
            raise ContractValidationError(
                "attempt_artifact_contract",
                f"$.artifacts.{name}",
                "fixture artifact content differs from its closed contract",
            )
    return canonical


def _validate_artifact_ref(
    value: Any,
    *,
    path: str,
    nullable: bool,
) -> dict[str, str] | None:
    if value is None:
        if nullable:
            return None
        raise ContractValidationError(
            "artifact_ref",
            path,
            "artifact reference is required",
        )
    row = require_mapping(value, path=path)
    require_exact_keys(row, required={"path", "sha256"}, path=path)
    return {
        "path": require_relative_path(row["path"], path=f"{path}.path"),
        "sha256": require_sha256(row["sha256"], path=f"{path}.sha256"),
    }


def _write_attempt_artifact(
    root: Path,
    path: Path,
    payload: Mapping[str, Any],
) -> dict[str, str]:
    _write_json_atomic(path, payload)
    return {
        "path": path.relative_to(root).as_posix(),
        "sha256": _file_sha256(path),
    }


def _build_summary(
    root: Path,
    plan: EvaluationPlanV1,
    checkpoint: Mapping[str, Any],
) -> OfflineFixtureRunSummaryV1:
    counts: dict[str, int] = {
        "blocked": 0,
        "pending": 0,
        "succeeded": 0,
        "exhausted": 0,
    }
    attempt_count = 0
    for row in checkpoint["jobs"]:
        counts[row["execution_status"]] += 1
        attempt_count += (
            row["complete_attempt_count"] + row["incomplete_attempt_count"]
        )
    if counts["pending"]:
        status = "paused"
    elif counts["exhausted"]:
        status = "completed_with_exhausted"
    else:
        status = "completed"
    return OfflineFixtureRunSummaryV1(
        plan_id=plan.plan_id,
        plan_sha256=plan.plan_sha256,
        status=status,
        ready_job_count=sum(job.status == "ready" for job in plan.jobs),
        blocked_job_count=counts["blocked"],
        succeeded_job_count=counts["succeeded"],
        exhausted_job_count=counts["exhausted"],
        pending_job_count=counts["pending"],
        attempt_count=attempt_count,
        checkpoint_sha256=checkpoint["integrity"]["checkpoint_sha256"],
        run_root=str(root),
    )


def _validate_failure_schedule(
    schedule: Mapping[tuple[str, int], str],
) -> dict[tuple[str, int], str]:
    result: dict[tuple[str, int], str] = {}
    for key, value in schedule.items():
        if (
            not isinstance(key, tuple)
            or len(key) != 2
            or not isinstance(key[0], str)
            or not isinstance(key[1], int)
            or isinstance(key[1], bool)
            or key[1] < 1
        ):
            raise ValueError("failure schedule keys must be (job_id, positive attempt_index)")
        if value not in _FIXTURE_FAILURE_KINDS:
            raise ValueError(f"unsupported fixture failure kind: {value}")
        result[(key[0], key[1])] = value
    return result


def _validate_failure_schedule_against_plan(
    schedule: Mapping[tuple[str, int], str],
    plan: EvaluationPlanV1,
    *,
    max_attempts: int,
) -> None:
    jobs = {job.job_id: job for job in plan.jobs}
    for job_id, attempt_index in schedule:
        job = jobs.get(job_id)
        if job is None:
            raise ValueError(f"failure schedule references unknown job: {job_id}")
        if job.status != "ready":
            raise ValueError(f"failure schedule references blocked job: {job_id}")
        if attempt_index > max_attempts:
            raise ValueError(
                "failure schedule attempt exceeds retry cap: "
                f"{job_id} attempt {attempt_index} > {max_attempts}"
            )


def _resolve_run_root(value: str | Path) -> Path:
    root = Path(value).resolve()
    root.mkdir(parents=True, exist_ok=True)
    if not root.is_dir():
        raise ContractValidationError("run_root", str(root), "run root must be a directory")
    return root


def _safe_id(value: str) -> str:
    if _SAFE_ID_RE.fullmatch(value) is None:
        raise ContractValidationError(
            "unsafe_id",
            "$.job_id",
            "ID is unsafe for a filesystem component",
        )
    return value


def _safe_child(root: Path, *parts: str) -> Path:
    candidate = root.joinpath(*parts).resolve()
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise ContractValidationError(
            "path_escape",
            str(candidate),
            "path escapes the evaluation run root",
        ) from exc
    return candidate


def _ensure_immutable_json(path: Path, payload: Mapping[str, Any]) -> None:
    if path.exists():
        if _load_json_object(path) != payload:
            raise ContractValidationError(
                "immutable_conflict",
                str(path),
                "existing immutable artifact differs from requested content",
            )
        return
    _write_json_atomic(path, payload)


def _write_json_atomic(
    path: Path,
    payload: Mapping[str, Any],
    *,
    replace: bool = False,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and not replace:
        raise ContractValidationError(
            "immutable_conflict",
            str(path),
            "refusing to overwrite immutable JSON artifact",
        )
    encoded = _stable_json_bytes(payload)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("xb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def _stable_json_bytes(payload: Mapping[str, Any]) -> bytes:
    try:
        text = json.dumps(
            payload,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    except (TypeError, ValueError) as exc:
        raise ContractValidationError(
            "json_encoding",
            "$",
            "artifact must be finite JSON data",
        ) from exc
    return (text + "\n").encode("utf-8")


def _load_json_object(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(
            path.read_text(encoding="utf-8"),
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_non_finite,
        )
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ContractValidationError(
            "json_read",
            str(path),
            "artifact is not valid UTF-8 JSON",
        ) from exc
    if not isinstance(value, dict):
        raise ContractValidationError("json_type", str(path), "artifact must be an object")
    return value


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ContractValidationError(
                "duplicate_key",
                "$",
                f"duplicate JSON key: {key}",
            )
        result[key] = value
    return result


def _reject_non_finite(value: str) -> None:
    raise ContractValidationError(
        "non_finite",
        "$",
        f"non-finite JSON value is forbidden: {value}",
    )


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
