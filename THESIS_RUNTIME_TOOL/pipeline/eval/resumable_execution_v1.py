from __future__ import annotations

import copy
import ctypes
import hashlib
import json
import os
import re
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping, Protocol, Sequence

from pipeline.eval.contracts_v1 import (
    ContractValidationError,
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
    validate_producer,
)
from pipeline.eval.execution_runner_v1 import (
    execute_evaluation_plan_v1,
    validate_evaluation_job_observation_v1,
)
from pipeline.eval.llm_adapter_v1 import (
    validate_evaluation_accepted_attempt_outcome_v1,
)
from pipeline.eval.method_executors_v1 import SharedEvaluationRoleCallV1
from pipeline.eval.offline_orchestrator_v1 import EvaluationPlanV1
from pipeline.eval.scorer_prompts_v3 import RenderedPromptV3
from pipeline.llm_backend import canonical_sha256 as shared_canonical_sha256
from pipeline.llm_backend import validate_resolved_llm_run_seal


__all__ = [
    "EVALUATION_RUN_STAGE_SCHEDULE_V1",
    "EvaluationRunHaltedV1",
    "EvaluationRunStateStoreV1",
    "ResumableEvaluationRoleRunnerV1",
    "execute_evaluation_plan_resumable_v1",
    "validate_evaluation_run_state_manifest_v1",
    "validate_evaluation_run_status_v1",
]


RUN_STATE_MANIFEST_SCHEMA_ID = "EvaluationRunStateManifestV1"
RUN_EVENT_SCHEMA_ID = "EvaluationRunEventV1"
RUN_STATUS_SCHEMA_ID = "EvaluationRunStatusV1"
ACCEPTED_CALL_SCHEMA_ID = "EvaluationAcceptedCallCheckpointV1"
COMPLETED_JOB_SCHEMA_ID = "EvaluationCompletedJobCheckpointV1"
SCHEMA_VERSION = "1.0.0"

EVALUATION_RUN_STAGE_SCHEDULE_V1 = (
    "preflight",
    "common_input",
    "sf_qe",
    "sf_bt",
    "pj",
    "aggregate",
    "report",
)
_EVENT_TYPES = {
    "run_initialized",
    "run_resumed",
    "stage_started",
    "stage_completed",
    "call_started",
    "call_accepted",
    "call_reused",
    "call_rejected",
    "call_failed",
    "job_started",
    "job_completed",
    "job_reused",
    "job_failed",
    "run_halted",
    "run_completed",
}
_SAFE_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,255}$")


class EvaluationRunHaltedV1(RuntimeError):
    def __init__(self, reason_code: str) -> None:
        self.reason_code = _identifier(reason_code, "$.reason_code")
        super().__init__(f"evaluation run halted: {self.reason_code}")


class _RoleRunnerV1(Protocol):
    @property
    def execution_binding(self) -> Mapping[str, str]: ...

    @property
    def semantic_contract(self) -> Mapping[str, Any]: ...

    @property
    def attempt_runtime_binding(self) -> Mapping[str, Any]: ...

    @property
    def cache_mode(self) -> str: ...

    def execute(
        self,
        *,
        role_id: str,
        scorer_input_packet_sha256: str,
        rendered_prompt: RenderedPromptV3,
        stage_id: str,
        logical_request_id: str,
        extra_bindings: Sequence[Mapping[str, str]] = (),
    ) -> SharedEvaluationRoleCallV1: ...


@dataclass(frozen=True, slots=True)
class _RunBindingV1:
    config_id: str
    config_sha256: str
    input_set_sha256: str
    plan_id: str
    plan_sha256: str
    semantic_contract_sha256: str


class EvaluationRunStateStoreV1:
    """Content-addressed call/job checkpoints plus an append-only status log."""

    def __init__(
        self,
        root: Path,
        *,
        manifest: Mapping[str, Any],
        clock: Callable[[], str] | None = None,
    ) -> None:
        self.root = root.resolve()
        self.manifest = validate_evaluation_run_state_manifest_v1(manifest)
        self._clock = clock or _utc_now
        self._events_dir = self.root / "events"
        self._calls_dir = self.root / "calls"
        self._jobs_dir = self.root / "jobs"
        self._manifest_path = self.root / "manifest.json"
        self._status_path = self.root / "status.json"
        self._lock_path = self.root / "runner.lock"
        self.root.mkdir(parents=True, exist_ok=True)
        self._events_dir.mkdir(parents=True, exist_ok=True)
        self._calls_dir.mkdir(parents=True, exist_ok=True)
        self._jobs_dir.mkdir(parents=True, exist_ok=True)
        if self._manifest_path.exists():
            persisted = validate_evaluation_run_state_manifest_v1(_load_json(self._manifest_path))
            if persisted != self.manifest:
                raise ContractValidationError("resume_binding", str(self._manifest_path), "run-state manifest differs from requested semantic run")
        else:
            _write_immutable_json(self._manifest_path, self.manifest)
        self._last_sequence, self._last_event_sha256 = self._audit_events()
        if self._last_sequence == 0:
            self.append_event("run_initialized")
        else:
            try:
                self._validate_status_projection()
            except ContractValidationError as exc:
                if exc.code not in {
                    "status_missing",
                    "status_event_binding",
                    "status_checkpoint_binding",
                }:
                    raise
                self._write_status_projection(self._latest_event())

    @classmethod
    def open_existing(
        cls,
        root: Path,
        *,
        clock: Callable[[], str] | None = None,
    ) -> "EvaluationRunStateStoreV1":
        manifest_path = root.resolve() / "manifest.json"
        if not manifest_path.is_file():
            raise ContractValidationError(
                "manifest_missing",
                str(manifest_path),
                "Evaluation run-state manifest is absent",
            )
        return cls(root, manifest=_load_json(manifest_path), clock=clock)

    @classmethod
    def open_or_create(
        cls,
        root: Path,
        *,
        plan: EvaluationPlanV1,
        semantic_contract: Mapping[str, Any],
        evaluation_logical_run_id: str,
        evaluation_attempt_run_id: str,
        evaluation_profile_id: str,
        policy_profile_id: str | None,
        baseline_arm_id: str | None,
        candidate_arm_id: str | None,
        created_at: str,
        producer_code_commit: str,
        clock: Callable[[], str] | None = None,
    ) -> "EvaluationRunStateStoreV1":
        semantic_sha = shared_canonical_sha256(semantic_contract)
        draft = {
            "schema_id": RUN_STATE_MANIFEST_SCHEMA_ID,
            "schema_version": SCHEMA_VERSION,
            "evaluation_logical_run_id": evaluation_logical_run_id,
            "evaluation_attempt_run_id": evaluation_attempt_run_id,
            "created_at": created_at,
            "producer": _producer("evaluation_run_state_v1", producer_code_commit),
            "binding": {
                "config_id": plan.config_id,
                "config_sha256": plan.config_sha256,
                "input_set_sha256": plan.input_set_sha256,
                "plan_id": plan.plan_id,
                "plan_sha256": plan.plan_sha256,
                "semantic_contract_sha256": semantic_sha,
            },
            "report_binding": {
                "evaluation_profile_id": evaluation_profile_id,
                "policy_profile_id": policy_profile_id,
                "baseline_arm_id": baseline_arm_id,
                "candidate_arm_id": candidate_arm_id,
            },
            "job_counts": {
                "planned_job_count": len(plan.jobs),
                "ready_job_count": sum(row.status == "ready" for row in plan.jobs),
                "blocked_job_count": sum(row.status == "blocked" for row in plan.jobs),
            },
            "stage_schedule": list(EVALUATION_RUN_STAGE_SCHEDULE_V1),
            "integrity": {"manifest_sha256": "0" * 64},
        }
        return cls(
            root,
            manifest=_seal_internal(draft, "manifest_sha256"),
            clock=clock,
        )

    @property
    def binding(self) -> _RunBindingV1:
        row = self.manifest["binding"]
        return _RunBindingV1(**row)

    @property
    def semantic_contract_sha256(self) -> str:
        return self.manifest["binding"]["semantic_contract_sha256"]

    @contextmanager
    def run_lock(self) -> Iterator[None]:
        self._recover_dead_lock()
        try:
            descriptor = os.open(self._lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError as exc:
            raise ContractValidationError("run_locked", str(self._lock_path), "another process owns this Evaluation run") from exc
        try:
            os.write(descriptor, f"{os.getpid()}\n".encode("ascii"))
            os.close(descriptor)
            yield
        finally:
            try:
                self._lock_path.unlink()
            except FileNotFoundError:
                pass

    def append_event(
        self,
        event_type: str,
        *,
        stage_id: str | None = None,
        job_id: str | None = None,
        call_id: str | None = None,
        reason_code: str | None = None,
    ) -> dict[str, Any]:
        sequence = self._last_sequence + 1
        occurred_at = require_rfc3339(self._clock(), path="$.clock")
        draft = {
            "schema_id": RUN_EVENT_SCHEMA_ID,
            "schema_version": SCHEMA_VERSION,
            "event_id": "pending",
            "sequence": sequence,
            "occurred_at": occurred_at,
            "evaluation_logical_run_id": self.manifest["evaluation_logical_run_id"],
            "evaluation_attempt_run_id": self.manifest["evaluation_attempt_run_id"],
            "event_type": event_type,
            "stage_id": stage_id,
            "job_id": job_id,
            "call_id": call_id,
            "reason_code": reason_code,
            "previous_event_sha256": self._last_event_sha256,
            "producer": self.manifest["producer"],
            "integrity": {"event_sha256": "0" * 64},
        }
        identity = _json_sha256({key: value for key, value in draft.items() if key not in {"event_id", "integrity"}})
        draft["event_id"] = f"event-{sequence:08d}-{identity[:16]}"
        event = validate_evaluation_run_event_v1(_seal_internal(draft, "event_sha256"))
        path = self._events_dir / f"{sequence:08d}-{event['event_id']}.json"
        _write_immutable_json(path, event)
        self._last_sequence = sequence
        self._last_event_sha256 = event["integrity"]["event_sha256"]
        self._write_status_projection(event)
        return event

    def load_accepted_call(self, binding: Mapping[str, Any]) -> SharedEvaluationRoleCallV1 | None:
        normalized_binding = _validate_call_binding(binding)
        call_id = _call_id(normalized_binding)
        path = self._calls_dir / f"{call_id}.json"
        if not path.exists():
            return None
        bundle = _validate_call_bundle(_load_json(path))
        if bundle["binding"] != normalized_binding or bundle["call_id"] != call_id:
            raise ContractValidationError("call_binding", str(path), "accepted call checkpoint belongs to another logical request")
        _validate_call_seal_binding(
            bundle["binding"],
            bundle["seal"],
            bundle["outcome"],
            self.manifest,
        )
        return SharedEvaluationRoleCallV1(seal=bundle["seal"], outcome=bundle["outcome"])

    def persist_accepted_call(
        self,
        binding: Mapping[str, Any],
        call: SharedEvaluationRoleCallV1,
    ) -> dict[str, Any]:
        normalized_binding = _validate_call_binding(binding)
        seal = validate_resolved_llm_run_seal(call.seal)
        outcome = validate_evaluation_accepted_attempt_outcome_v1(call.outcome, seal=seal)
        _validate_call_seal_binding(normalized_binding, seal, outcome, self.manifest)
        call_id = _call_id(normalized_binding)
        draft = {
            "schema_id": ACCEPTED_CALL_SCHEMA_ID,
            "schema_version": SCHEMA_VERSION,
            "call_id": call_id,
            "created_at": require_rfc3339(self._clock(), path="$.clock"),
            "producer": self.manifest["producer"],
            "binding": normalized_binding,
            "seal": seal,
            "outcome": outcome,
            "integrity": {"checkpoint_sha256": "0" * 64},
        }
        bundle = _validate_call_bundle(_seal_internal(draft, "checkpoint_sha256"))
        _write_immutable_json(self._calls_dir / f"{call_id}.json", bundle)
        return bundle

    def load_completed_job(self, binding: Mapping[str, Any]) -> dict[str, Any] | None:
        normalized_binding = _validate_job_binding(binding)
        checkpoint_id = _job_checkpoint_id(normalized_binding)
        path = self._jobs_dir / f"{checkpoint_id}.json"
        if not path.exists():
            return None
        bundle = _validate_job_bundle(_load_json(path))
        if bundle["binding"] != normalized_binding or bundle["checkpoint_id"] != checkpoint_id:
            raise ContractValidationError("job_binding", str(path), "job checkpoint belongs to another scorer packet")
        return copy.deepcopy(bundle["observation"])

    def persist_completed_job(
        self,
        binding: Mapping[str, Any],
        observation: Mapping[str, Any],
    ) -> dict[str, Any]:
        normalized_binding = _validate_job_binding(binding)
        normalized_observation = validate_evaluation_job_observation_v1(
            observation,
            method_id=normalized_binding["method_id"],
        )
        if normalized_observation["status"] != "succeeded":
            raise ContractValidationError("job_status", "$.observation.status", "only successful jobs are checkpointable")
        checkpoint_id = _job_checkpoint_id(normalized_binding)
        draft = {
            "schema_id": COMPLETED_JOB_SCHEMA_ID,
            "schema_version": SCHEMA_VERSION,
            "checkpoint_id": checkpoint_id,
            "created_at": require_rfc3339(self._clock(), path="$.clock"),
            "producer": self.manifest["producer"],
            "binding": normalized_binding,
            "observation": normalized_observation,
            "integrity": {"checkpoint_sha256": "0" * 64},
        }
        bundle = _validate_job_bundle(_seal_internal(draft, "checkpoint_sha256"))
        _write_immutable_json(self._jobs_dir / f"{checkpoint_id}.json", bundle)
        return bundle

    def status(self) -> dict[str, Any]:
        return self._validate_status_projection()

    def _audit_events(self) -> tuple[int, str | None]:
        previous: str | None = None
        expected_sequence = 1
        for path in sorted(self._events_dir.glob("*.json")):
            event = validate_evaluation_run_event_v1(_load_json(path))
            if event["sequence"] != expected_sequence or event["previous_event_sha256"] != previous:
                raise ContractValidationError("event_chain", str(path), "event sequence or previous hash drift")
            expected_name = f"{expected_sequence:08d}-{event['event_id']}.json"
            if path.name != expected_name:
                raise ContractValidationError("event_path", str(path), "event filename differs from event identity")
            if event["evaluation_logical_run_id"] != self.manifest["evaluation_logical_run_id"] or event["evaluation_attempt_run_id"] != self.manifest["evaluation_attempt_run_id"]:
                raise ContractValidationError("event_binding", str(path), "event belongs to another run")
            previous = event["integrity"]["event_sha256"]
            expected_sequence += 1
        return expected_sequence - 1, previous

    def _latest_event(self) -> dict[str, Any]:
        rows = sorted(self._events_dir.glob("*.json"))
        if not rows:
            raise ContractValidationError(
                "event_missing", str(self._events_dir), "run has no event to project"
            )
        return validate_evaluation_run_event_v1(_load_json(rows[-1]))

    def _write_status_projection(self, event: Mapping[str, Any]) -> None:
        if event["event_type"] == "run_halted":
            state = "halted"
        elif event["event_type"] == "run_completed":
            state = "completed"
        else:
            state = "running"
        draft = {
            "schema_id": RUN_STATUS_SCHEMA_ID,
            "schema_version": SCHEMA_VERSION,
            "evaluation_logical_run_id": self.manifest["evaluation_logical_run_id"],
            "evaluation_attempt_run_id": self.manifest["evaluation_attempt_run_id"],
            "state": state,
            "current_stage_id": event["stage_id"],
            "last_event_sequence": event["sequence"],
            "last_event_sha256": event["integrity"]["event_sha256"],
            "accepted_call_count": len(list(self._calls_dir.glob("call-*.json"))),
            "completed_job_count": len(list(self._jobs_dir.glob("job-*.json"))),
            **self.manifest["job_counts"],
            "reason_code": event["reason_code"] if state == "halted" else None,
            "updated_at": event["occurred_at"],
            "integrity": {"status_sha256": "0" * 64},
        }
        status = validate_evaluation_run_status_v1(_seal_internal(draft, "status_sha256"))
        _write_json_atomic(self._status_path, status)

    def _validate_status_projection(self) -> dict[str, Any]:
        if not self._status_path.exists():
            raise ContractValidationError("status_missing", str(self._status_path), "run status projection is absent")
        status = validate_evaluation_run_status_v1(_load_json(self._status_path))
        if status["evaluation_logical_run_id"] != self.manifest["evaluation_logical_run_id"] or status["evaluation_attempt_run_id"] != self.manifest["evaluation_attempt_run_id"]:
            raise ContractValidationError("status_binding", str(self._status_path), "status belongs to another run")
        if status["last_event_sequence"] != self._last_sequence or status["last_event_sha256"] != self._last_event_sha256:
            raise ContractValidationError("status_event_binding", str(self._status_path), "status does not project the latest event")
        if status["accepted_call_count"] != len(list(self._calls_dir.glob("call-*.json"))) or status["completed_job_count"] != len(list(self._jobs_dir.glob("job-*.json"))):
            raise ContractValidationError("status_checkpoint_binding", str(self._status_path), "status checkpoint counts drift")
        return status

    def _recover_dead_lock(self) -> None:
        if not self._lock_path.exists():
            return
        try:
            pid = int(self._lock_path.read_text(encoding="ascii").strip())
        except (OSError, ValueError) as exc:
            raise ContractValidationError("run_lock", str(self._lock_path), "run lock is malformed") from exc
        if _pid_is_alive(pid):
            return
        self._lock_path.unlink()


class ResumableEvaluationRoleRunnerV1:
    def __init__(self, base: _RoleRunnerV1, store: EvaluationRunStateStoreV1) -> None:
        self._base = base
        self._store = store
        self._semantic_contract = copy.deepcopy(dict(base.semantic_contract))
        semantic_sha = shared_canonical_sha256(self._semantic_contract)
        if semantic_sha != store.semantic_contract_sha256:
            raise ContractValidationError("semantic_contract", "$.llm_roles", "model, prompt, schema, validator, generation, or route family changed during resume")
        binding = base.execution_binding
        if binding["evaluation_logical_run_id"] != store.manifest["evaluation_logical_run_id"] or binding["evaluation_attempt_run_id"] != store.manifest["evaluation_attempt_run_id"]:
            raise ContractValidationError("run_binding", "$.llm_roles", "role runner belongs to another logical or attempt run")

    @property
    def execution_binding(self) -> dict[str, str]:
        return {
            "evaluation_logical_run_id": self._store.manifest["evaluation_logical_run_id"],
            "evaluation_attempt_run_id": self._store.manifest["evaluation_attempt_run_id"],
            "evaluation_profile_id": f"evaluation.semantic-contract.v1.{self._store.semantic_contract_sha256[:16]}",
            "evaluation_profile_sha256": self._store.semantic_contract_sha256,
        }

    @property
    def semantic_contract(self) -> dict[str, Any]:
        return copy.deepcopy(self._semantic_contract)

    @property
    def attempt_runtime_binding(self) -> Mapping[str, Any]:
        return self._base.attempt_runtime_binding

    @property
    def cache_mode(self) -> str:
        return self._base.cache_mode

    def execute(
        self,
        *,
        role_id: str,
        scorer_input_packet_sha256: str,
        rendered_prompt: RenderedPromptV3,
        stage_id: str,
        logical_request_id: str,
        extra_bindings: Sequence[Mapping[str, str]] = (),
    ) -> SharedEvaluationRoleCallV1:
        normalized_extra = _normalize_extra_bindings(extra_bindings)
        binding = {
            "role_id": _identifier(role_id, "$.role_id"),
            "stage_id": _identifier(stage_id, "$.stage_id"),
            "logical_request_id": _identifier(logical_request_id, "$.logical_request_id"),
            "scorer_input_packet_sha256": require_sha256(scorer_input_packet_sha256, path="$.scorer_input_packet_sha256"),
            "rendered_prompt_sha256": require_sha256(rendered_prompt.rendered_prompt_sha256, path="$.rendered_prompt_sha256"),
            "extra_bindings": normalized_extra,
            "semantic_contract_sha256": self._store.semantic_contract_sha256,
        }
        call_id = _call_id(binding)
        checkpoint = self._store.load_accepted_call(binding)
        if checkpoint is not None:
            self._store.append_event("call_reused", stage_id=role_id, call_id=call_id)
            return checkpoint
        self._store.append_event("call_started", stage_id=role_id, call_id=call_id)
        try:
            call = self._base.execute(
                role_id=role_id,
                scorer_input_packet_sha256=scorer_input_packet_sha256,
                rendered_prompt=rendered_prompt,
                stage_id=stage_id,
                logical_request_id=logical_request_id,
                extra_bindings=extra_bindings,
            )
        except Exception as exc:
            reason = _exception_reason(exc)
            self._store.append_event("call_failed", stage_id=role_id, call_id=call_id, reason_code=reason)
            raise EvaluationRunHaltedV1(reason) from exc
        if call.outcome.get("status") == "accepted":
            self._store.persist_accepted_call(binding, call)
            self._store.append_event("call_accepted", stage_id=role_id, call_id=call_id)
        else:
            reason = _outcome_reason(call.outcome)
            self._store.append_event("call_rejected", stage_id=role_id, call_id=call_id, reason_code=reason)
        return call


class _CheckpointingJobExecutorV1:
    def __init__(self, executor: Callable[[Mapping[str, Any]], Mapping[str, Any]], store: EvaluationRunStateStoreV1) -> None:
        self._executor = executor
        self._store = store

    def __call__(self, packet: Mapping[str, Any]) -> dict[str, Any]:
        root = require_mapping(packet, path="$.packet")
        packet_binding = require_mapping(root.get("binding"), path="$.packet.binding")
        integrity = require_mapping(root.get("integrity"), path="$.packet.integrity")
        binding = {
            "job_id": _identifier(packet_binding.get("job_id"), "$.packet.binding.job_id"),
            "method_id": require_enum(packet_binding.get("method_id"), {"sf_qe", "sf_bt", "pj"}, path="$.packet.binding.method_id"),
            "packet_sha256": require_sha256(integrity.get("packet_sha256"), path="$.packet.integrity.packet_sha256"),
            "plan_sha256": self._store.binding.plan_sha256,
        }
        job_id = binding["job_id"]
        stage = binding["method_id"]
        checkpoint = self._store.load_completed_job(binding)
        if checkpoint is not None:
            self._store.append_event("job_reused", stage_id=stage, job_id=job_id)
            return checkpoint
        self._store.append_event("job_started", stage_id=stage, job_id=job_id)
        try:
            raw = self._executor(copy.deepcopy(root))
            observation = validate_evaluation_job_observation_v1(raw, method_id=stage)
        except EvaluationRunHaltedV1:
            self._store.append_event("job_failed", stage_id=stage, job_id=job_id, reason_code="llm_attempt_halted")
            raise
        except Exception as exc:
            reason = _exception_reason(exc)
            self._store.append_event("job_failed", stage_id=stage, job_id=job_id, reason_code=reason)
            raise EvaluationRunHaltedV1(reason) from exc
        if observation["status"] != "succeeded":
            reason = _identifier(observation["error_code"], "$.observation.error_code")
            self._store.append_event("job_failed", stage_id=stage, job_id=job_id, reason_code=reason)
            raise EvaluationRunHaltedV1(reason)
        self._store.persist_completed_job(binding, observation)
        self._store.append_event("job_completed", stage_id=stage, job_id=job_id)
        return observation


def execute_evaluation_plan_resumable_v1(
    common_input: Any,
    config_payload: Mapping[str, Any],
    executor: Callable[[Mapping[str, Any]], Mapping[str, Any]],
    store: EvaluationRunStateStoreV1,
    *,
    created_at: str,
    runner_code_commit: str,
    baseline_arm_id: str | None = None,
    candidate_arm_id: str | None = None,
    finalize_run: bool = True,
    acquire_run_lock: bool = True,
) -> dict[str, Any]:
    report_binding = store.manifest["report_binding"]
    if (
        baseline_arm_id != report_binding["baseline_arm_id"]
        or candidate_arm_id != report_binding["candidate_arm_id"]
    ):
        raise ContractValidationError(
            "comparison_binding",
            "$.baseline_arm_id",
            "resume requested another baseline/candidate comparison",
        )
    if require_rfc3339(created_at, path="$.created_at") != store.manifest["created_at"]:
        raise ContractValidationError(
            "created_at_binding", "$.created_at", "resume changed the sealed run timestamp"
        )
    if require_commit(runner_code_commit, path="$.runner_code_commit") != store.manifest["producer"]["code_commit"]:
        raise ContractValidationError(
            "runner_commit_binding",
            "$.runner_code_commit",
            "resume changed the sealed runner implementation",
        )
    lock = store.run_lock() if acquire_run_lock else nullcontext()
    with lock:
        prior_status = store.status()
        if prior_status["state"] in {"halted", "running"} and prior_status["last_event_sequence"] > 1:
            store.append_event("run_resumed", stage_id=prior_status["current_stage_id"])
        store.append_event("stage_started", stage_id="common_input")
        store.append_event("stage_completed", stage_id="common_input")
        try:
            execution = execute_evaluation_plan_v1(
                common_input,
                config_payload,
                _CheckpointingJobExecutorV1(executor, store),
                created_at=created_at,
                runner_code_commit=runner_code_commit,
                baseline_arm_id=baseline_arm_id,
                candidate_arm_id=candidate_arm_id,
            )
        except Exception as exc:
            reason = exc.reason_code if isinstance(exc, EvaluationRunHaltedV1) else _exception_reason(exc)
            store.append_event(
                "run_halted",
                stage_id=store.status()["current_stage_id"],
                reason_code=reason,
            )
            if isinstance(exc, EvaluationRunHaltedV1):
                raise
            raise EvaluationRunHaltedV1(reason) from exc
        store.append_event("stage_started", stage_id="aggregate")
        store.append_event("stage_completed", stage_id="aggregate")
        if finalize_run:
            store.append_event("run_completed")
        return execution


def validate_evaluation_run_state_manifest_v1(value: Mapping[str, Any]) -> dict[str, Any]:
    row = require_mapping(value, path="$")
    require_exact_keys(row, required={"schema_id", "schema_version", "evaluation_logical_run_id", "evaluation_attempt_run_id", "created_at", "producer", "binding", "report_binding", "job_counts", "stage_schedule", "integrity"}, path="$")
    binding = require_mapping(row["binding"], path="$.binding")
    require_exact_keys(binding, required={"config_id", "config_sha256", "input_set_sha256", "plan_id", "plan_sha256", "semantic_contract_sha256"}, path="$.binding")
    report_binding = require_mapping(row["report_binding"], path="$.report_binding")
    require_exact_keys(report_binding, required={"evaluation_profile_id", "policy_profile_id", "baseline_arm_id", "candidate_arm_id"}, path="$.report_binding")
    baseline = _nullable_identifier(report_binding["baseline_arm_id"], "$.report_binding.baseline_arm_id")
    candidate = _nullable_identifier(report_binding["candidate_arm_id"], "$.report_binding.candidate_arm_id")
    if (baseline is None) != (candidate is None) or (baseline is not None and baseline == candidate):
        raise ContractValidationError("comparison_binding", "$.report_binding", "comparison arms must be absent together or distinct")
    job_counts = require_mapping(row["job_counts"], path="$.job_counts")
    require_exact_keys(job_counts, required={"planned_job_count", "ready_job_count", "blocked_job_count"}, path="$.job_counts")
    normalized_job_counts = {
        key: require_int(job_counts[key], path=f"$.job_counts.{key}", minimum=0)
        for key in ("planned_job_count", "ready_job_count", "blocked_job_count")
    }
    if normalized_job_counts["planned_job_count"] != normalized_job_counts["ready_job_count"] + normalized_job_counts["blocked_job_count"]:
        raise ContractValidationError("job_counts", "$.job_counts", "planned jobs must equal ready plus blocked jobs")
    stages = [_identifier(item, f"$.stage_schedule[{index}]") for index, item in enumerate(require_list(row["stage_schedule"], path="$.stage_schedule"))]
    if stages != list(EVALUATION_RUN_STAGE_SCHEDULE_V1):
        raise ContractValidationError("stage_schedule", "$.stage_schedule", "stage schedule differs from Evaluation v1")
    result = {
        "schema_id": require_enum(row["schema_id"], {RUN_STATE_MANIFEST_SCHEMA_ID}, path="$.schema_id"),
        "schema_version": require_enum(row["schema_version"], {SCHEMA_VERSION}, path="$.schema_version"),
        "evaluation_logical_run_id": _identifier(row["evaluation_logical_run_id"], "$.evaluation_logical_run_id"),
        "evaluation_attempt_run_id": _identifier(row["evaluation_attempt_run_id"], "$.evaluation_attempt_run_id"),
        "created_at": require_rfc3339(row["created_at"], path="$.created_at"),
        "producer": _validate_producer(row["producer"], "evaluation_run_state_v1"),
        "binding": {
            "config_id": _identifier(binding["config_id"], "$.binding.config_id"),
            "config_sha256": require_sha256(binding["config_sha256"], path="$.binding.config_sha256"),
            "input_set_sha256": require_sha256(binding["input_set_sha256"], path="$.binding.input_set_sha256"),
            "plan_id": _identifier(binding["plan_id"], "$.binding.plan_id"),
            "plan_sha256": require_sha256(binding["plan_sha256"], path="$.binding.plan_sha256"),
            "semantic_contract_sha256": require_sha256(binding["semantic_contract_sha256"], path="$.binding.semantic_contract_sha256"),
        },
        "report_binding": {
            "evaluation_profile_id": _identifier(report_binding["evaluation_profile_id"], "$.report_binding.evaluation_profile_id"),
            "policy_profile_id": _nullable_identifier(report_binding["policy_profile_id"], "$.report_binding.policy_profile_id"),
            "baseline_arm_id": baseline,
            "candidate_arm_id": candidate,
        },
        "job_counts": normalized_job_counts,
        "stage_schedule": stages,
        "integrity": _integrity(row["integrity"], "manifest_sha256"),
    }
    _verify_internal(result, "manifest_sha256")
    return result


def validate_evaluation_run_event_v1(value: Mapping[str, Any]) -> dict[str, Any]:
    row = require_mapping(value, path="$")
    require_exact_keys(row, required={"schema_id", "schema_version", "event_id", "sequence", "occurred_at", "evaluation_logical_run_id", "evaluation_attempt_run_id", "event_type", "stage_id", "job_id", "call_id", "reason_code", "previous_event_sha256", "producer", "integrity"}, path="$")
    event_type = require_enum(row["event_type"], _EVENT_TYPES, path="$.event_type")
    stage = _nullable_identifier(row["stage_id"], "$.stage_id")
    job = _nullable_identifier(row["job_id"], "$.job_id")
    call = _nullable_identifier(row["call_id"], "$.call_id")
    reason = _nullable_identifier(row["reason_code"], "$.reason_code")
    if event_type in {"call_started", "call_accepted", "call_reused", "call_rejected", "call_failed"} and (stage is None or call is None):
        raise ContractValidationError("event_shape", "$", "call event requires stage and call IDs")
    if event_type in {"job_started", "job_completed", "job_reused", "job_failed"} and (stage is None or job is None):
        raise ContractValidationError("event_shape", "$", "job event requires stage and job IDs")
    if event_type in {"stage_started", "stage_completed"} and stage is None:
        raise ContractValidationError("event_shape", "$.stage_id", "stage event requires a stage ID")
    if event_type in {"run_halted", "call_rejected", "call_failed", "job_failed"} and reason is None:
        raise ContractValidationError("event_shape", "$.reason_code", "failure event requires reason code")
    if event_type not in {"run_halted", "call_rejected", "call_failed", "job_failed"} and reason is not None:
        raise ContractValidationError("event_shape", "$.reason_code", "successful event cannot carry a failure reason")
    result = {
        "schema_id": require_enum(row["schema_id"], {RUN_EVENT_SCHEMA_ID}, path="$.schema_id"),
        "schema_version": require_enum(row["schema_version"], {SCHEMA_VERSION}, path="$.schema_version"),
        "event_id": _identifier(row["event_id"], "$.event_id"),
        "sequence": require_int(row["sequence"], path="$.sequence", minimum=1),
        "occurred_at": require_rfc3339(row["occurred_at"], path="$.occurred_at"),
        "evaluation_logical_run_id": _identifier(row["evaluation_logical_run_id"], "$.evaluation_logical_run_id"),
        "evaluation_attempt_run_id": _identifier(row["evaluation_attempt_run_id"], "$.evaluation_attempt_run_id"),
        "event_type": event_type,
        "stage_id": stage,
        "job_id": job,
        "call_id": call,
        "reason_code": reason,
        "previous_event_sha256": None if row["previous_event_sha256"] is None else require_sha256(row["previous_event_sha256"], path="$.previous_event_sha256"),
        "producer": _validate_producer(row["producer"], "evaluation_run_state_v1"),
        "integrity": _integrity(row["integrity"], "event_sha256"),
    }
    _verify_internal(result, "event_sha256")
    return result


def validate_evaluation_run_status_v1(value: Mapping[str, Any]) -> dict[str, Any]:
    row = require_mapping(value, path="$")
    require_exact_keys(row, required={"schema_id", "schema_version", "evaluation_logical_run_id", "evaluation_attempt_run_id", "state", "current_stage_id", "last_event_sequence", "last_event_sha256", "accepted_call_count", "completed_job_count", "planned_job_count", "ready_job_count", "blocked_job_count", "reason_code", "updated_at", "integrity"}, path="$")
    state = require_enum(row["state"], {"running", "halted", "completed"}, path="$.state")
    reason = _nullable_identifier(row["reason_code"], "$.reason_code")
    if (state == "halted") != (reason is not None):
        raise ContractValidationError("status_reason", "$.reason_code", "only halted status carries a reason")
    planned_jobs = require_int(row["planned_job_count"], path="$.planned_job_count", minimum=0)
    ready_jobs = require_int(row["ready_job_count"], path="$.ready_job_count", minimum=0)
    blocked_jobs = require_int(row["blocked_job_count"], path="$.blocked_job_count", minimum=0)
    completed_jobs = require_int(row["completed_job_count"], path="$.completed_job_count", minimum=0)
    if planned_jobs != ready_jobs + blocked_jobs or completed_jobs > ready_jobs:
        raise ContractValidationError("job_counts", "$", "status job counts are inconsistent")
    result = {
        "schema_id": require_enum(row["schema_id"], {RUN_STATUS_SCHEMA_ID}, path="$.schema_id"),
        "schema_version": require_enum(row["schema_version"], {SCHEMA_VERSION}, path="$.schema_version"),
        "evaluation_logical_run_id": _identifier(row["evaluation_logical_run_id"], "$.evaluation_logical_run_id"),
        "evaluation_attempt_run_id": _identifier(row["evaluation_attempt_run_id"], "$.evaluation_attempt_run_id"),
        "state": state,
        "current_stage_id": _nullable_identifier(row["current_stage_id"], "$.current_stage_id"),
        "last_event_sequence": require_int(row["last_event_sequence"], path="$.last_event_sequence", minimum=1),
        "last_event_sha256": require_sha256(row["last_event_sha256"], path="$.last_event_sha256"),
        "accepted_call_count": require_int(row["accepted_call_count"], path="$.accepted_call_count", minimum=0),
        "completed_job_count": completed_jobs,
        "planned_job_count": planned_jobs,
        "ready_job_count": ready_jobs,
        "blocked_job_count": blocked_jobs,
        "reason_code": reason,
        "updated_at": require_rfc3339(row["updated_at"], path="$.updated_at"),
        "integrity": _integrity(row["integrity"], "status_sha256"),
    }
    _verify_internal(result, "status_sha256")
    return result


def _validate_call_binding(value: Mapping[str, Any]) -> dict[str, Any]:
    row = require_mapping(value, path="$.binding")
    require_exact_keys(row, required={"role_id", "stage_id", "logical_request_id", "scorer_input_packet_sha256", "rendered_prompt_sha256", "extra_bindings", "semantic_contract_sha256"}, path="$.binding")
    return {
        "role_id": _identifier(row["role_id"], "$.binding.role_id"),
        "stage_id": _identifier(row["stage_id"], "$.binding.stage_id"),
        "logical_request_id": _identifier(row["logical_request_id"], "$.binding.logical_request_id"),
        "scorer_input_packet_sha256": require_sha256(row["scorer_input_packet_sha256"], path="$.binding.scorer_input_packet_sha256"),
        "rendered_prompt_sha256": require_sha256(row["rendered_prompt_sha256"], path="$.binding.rendered_prompt_sha256"),
        "extra_bindings": _normalize_extra_bindings(require_list(row["extra_bindings"], path="$.binding.extra_bindings")),
        "semantic_contract_sha256": require_sha256(row["semantic_contract_sha256"], path="$.binding.semantic_contract_sha256"),
    }


def _validate_call_bundle(value: Mapping[str, Any]) -> dict[str, Any]:
    row = require_mapping(value, path="$")
    require_exact_keys(row, required={"schema_id", "schema_version", "call_id", "created_at", "producer", "binding", "seal", "outcome", "integrity"}, path="$")
    binding = _validate_call_binding(row["binding"])
    seal = validate_resolved_llm_run_seal(require_mapping(row["seal"], path="$.seal"))
    outcome = validate_evaluation_accepted_attempt_outcome_v1(require_mapping(row["outcome"], path="$.outcome"), seal=seal)
    result = {
        "schema_id": require_enum(row["schema_id"], {ACCEPTED_CALL_SCHEMA_ID}, path="$.schema_id"),
        "schema_version": require_enum(row["schema_version"], {SCHEMA_VERSION}, path="$.schema_version"),
        "call_id": _identifier(row["call_id"], "$.call_id"),
        "created_at": require_rfc3339(row["created_at"], path="$.created_at"),
        "producer": _validate_producer(row["producer"], "evaluation_run_state_v1"),
        "binding": binding,
        "seal": seal,
        "outcome": outcome,
        "integrity": _integrity(row["integrity"], "checkpoint_sha256"),
    }
    if result["call_id"] != _call_id(binding):
        raise ContractValidationError("call_id", "$.call_id", "call ID differs from semantic binding")
    _verify_internal(result, "checkpoint_sha256")
    return result


def _validate_job_binding(value: Mapping[str, Any]) -> dict[str, str]:
    row = require_mapping(value, path="$.binding")
    require_exact_keys(row, required={"job_id", "method_id", "packet_sha256", "plan_sha256"}, path="$.binding")
    return {
        "job_id": _identifier(row["job_id"], "$.binding.job_id"),
        "method_id": require_enum(row["method_id"], {"sf_qe", "sf_bt", "pj"}, path="$.binding.method_id"),
        "packet_sha256": require_sha256(row["packet_sha256"], path="$.binding.packet_sha256"),
        "plan_sha256": require_sha256(row["plan_sha256"], path="$.binding.plan_sha256"),
    }


def _validate_job_bundle(value: Mapping[str, Any]) -> dict[str, Any]:
    row = require_mapping(value, path="$")
    require_exact_keys(row, required={"schema_id", "schema_version", "checkpoint_id", "created_at", "producer", "binding", "observation", "integrity"}, path="$")
    binding = _validate_job_binding(row["binding"])
    observation = validate_evaluation_job_observation_v1(require_mapping(row["observation"], path="$.observation"), method_id=binding["method_id"])
    if observation["status"] != "succeeded":
        raise ContractValidationError("job_status", "$.observation.status", "completed checkpoint is not successful")
    result = {
        "schema_id": require_enum(row["schema_id"], {COMPLETED_JOB_SCHEMA_ID}, path="$.schema_id"),
        "schema_version": require_enum(row["schema_version"], {SCHEMA_VERSION}, path="$.schema_version"),
        "checkpoint_id": _identifier(row["checkpoint_id"], "$.checkpoint_id"),
        "created_at": require_rfc3339(row["created_at"], path="$.created_at"),
        "producer": _validate_producer(row["producer"], "evaluation_run_state_v1"),
        "binding": binding,
        "observation": observation,
        "integrity": _integrity(row["integrity"], "checkpoint_sha256"),
    }
    if result["checkpoint_id"] != _job_checkpoint_id(binding):
        raise ContractValidationError("checkpoint_id", "$.checkpoint_id", "job checkpoint ID differs from binding")
    _verify_internal(result, "checkpoint_sha256")
    return result


def _validate_call_seal_binding(binding: Mapping[str, Any], seal: Mapping[str, Any], outcome: Mapping[str, Any], manifest: Mapping[str, Any]) -> None:
    for field in ("role_id", "stage_id"):
        if seal[field] != binding[field]:
            raise ContractValidationError("call_seal_binding", f"$.seal.{field}", "seal differs from call binding")
    if seal["run_id"] != manifest["evaluation_logical_run_id"] or seal["attempt_run_id"] != manifest["evaluation_attempt_run_id"]:
        raise ContractValidationError("call_run_binding", "$.seal", "seal belongs to another Evaluation run")
    if outcome["logical_request_id"] != binding["logical_request_id"]:
        raise ContractValidationError("logical_request", "$.outcome.logical_request_id", "outcome differs from call binding")
    indexed = {row["name"]: row["sha256"] for row in seal["input_bindings"]}
    expected = {
        "scorer_input_packet": binding["scorer_input_packet_sha256"],
        "rendered_prompt": binding["rendered_prompt_sha256"],
        **{row["name"]: row["sha256"] for row in binding["extra_bindings"]},
    }
    for name, digest in expected.items():
        if indexed.get(name) != digest:
            raise ContractValidationError("call_input_binding", "$.seal.input_bindings", f"seal input {name!r} differs from checkpoint")


def _normalize_extra_bindings(value: Sequence[Mapping[str, str]]) -> list[dict[str, str]]:
    result = []
    for index, raw in enumerate(value):
        row = require_mapping(raw, path=f"$.extra_bindings[{index}]")
        require_exact_keys(row, required={"name", "sha256"}, path=f"$.extra_bindings[{index}]")
        result.append({"name": _identifier(row["name"], f"$.extra_bindings[{index}].name"), "sha256": require_sha256(row["sha256"], path=f"$.extra_bindings[{index}].sha256")})
    require_unique([row["name"] for row in result], path="$.extra_bindings.name")
    return sorted(result, key=lambda row: row["name"])


def _call_id(binding: Mapping[str, Any]) -> str:
    return "call-" + _json_sha256(binding)[:32]


def _job_checkpoint_id(binding: Mapping[str, Any]) -> str:
    return "job-" + _json_sha256(binding)[:32]


def _producer(component: str, commit: str) -> dict[str, str]:
    return {"workstream": "evaluation", "component": component, "component_version": SCHEMA_VERSION, "code_commit": require_commit(commit, path="$.producer_code_commit")}


def _validate_producer(value: Any, component: str) -> dict[str, str]:
    row = validate_producer(value, path="$.producer", workstream="evaluation")
    if row["component"] != component or row["component_version"] != SCHEMA_VERSION:
        raise ContractValidationError("producer", "$.producer", "run-state producer component/version drift")
    return row


def _integrity(value: Any, field: str) -> dict[str, str]:
    row = require_mapping(value, path="$.integrity")
    require_exact_keys(row, required={field}, path="$.integrity")
    return {field: require_sha256(row[field], path=f"$.integrity.{field}")}


def _seal_internal(value: Mapping[str, Any], field: str) -> dict[str, Any]:
    result = copy.deepcopy(dict(value))
    result["integrity"][field] = "0" * 64
    result["integrity"][field] = _json_sha256(result)
    return result


def _verify_internal(value: Mapping[str, Any], field: str) -> None:
    expected = value["integrity"][field]
    draft = copy.deepcopy(dict(value))
    draft["integrity"][field] = "0" * 64
    if _json_sha256(draft) != expected:
        raise ContractValidationError("self_hash", f"$.integrity.{field}", "checkpoint self-hash drift")


def _json_sha256(value: Any) -> str:
    try:
        encoded = json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ContractValidationError("canonical_json", "$", "checkpoint contains non-canonical or non-finite data") from exc
    return hashlib.sha256(encoded).hexdigest()


def _write_immutable_json(path: Path, value: Mapping[str, Any]) -> None:
    encoded = _json_bytes(value)
    if path.exists():
        if path.read_bytes() != encoded:
            raise ContractValidationError("immutable_artifact", str(path), "existing checkpoint bytes differ")
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    _write_bytes_atomic(path, encoded)


def _write_json_atomic(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    _write_bytes_atomic(path, _json_bytes(value))


def _write_bytes_atomic(path: Path, encoded: bytes) -> None:
    temporary = path.with_name(path.name + f".tmp-{os.getpid()}")
    try:
        with temporary.open("xb") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _json_bytes(value: Mapping[str, Any]) -> bytes:
    try:
        return (json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ContractValidationError("canonical_json", "$", "checkpoint contains non-canonical or non-finite data") from exc


def _load_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=_reject_duplicates, parse_constant=_reject_nonfinite)
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
        raise ContractValidationError("json", str(path), "checkpoint JSON cannot be read") from exc
    return require_mapping(value, path=str(path))


def _reject_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate key: {key}")
        result[key] = value
    return result


def _reject_nonfinite(value: str) -> None:
    raise ValueError(f"non-finite value: {value}")


def _identifier(value: Any, path: str) -> str:
    result = require_string(value, path=path)
    if _SAFE_IDENTIFIER.fullmatch(result) is None:
        raise ContractValidationError("identifier", path, "unsupported identifier")
    return result


def _nullable_identifier(value: Any, path: str) -> str | None:
    result = require_nullable_string(value, path=path)
    return None if result is None else _identifier(result, path)


def _exception_reason(exc: Exception) -> str:
    code = getattr(exc, "code", None)
    candidate = code if isinstance(code, str) and code else exc.__class__.__name__.lower()
    candidate = re.sub(r"[^A-Za-z0-9._-]+", "_", candidate).strip("_") or "unknown_failure"
    return candidate[:255]


def _outcome_reason(outcome: Mapping[str, Any]) -> str:
    semantic_error = outcome.get("semantic_error")
    if isinstance(semantic_error, Mapping) and isinstance(semantic_error.get("code"), str):
        candidate = re.sub(
            r"[^A-Za-z0-9._-]+", "_", semantic_error["code"]
        ).strip("_")
        return candidate[:255] or "semantic_rejected"
    status = outcome.get("status")
    return _identifier(status if isinstance(status, str) else "semantic_rejected", "$.outcome.status")


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _pid_is_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        process_query_limited_information = 0x1000
        still_active = 259
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(
            process_query_limited_information,
            False,
            pid,
        )
        if not handle:
            return False
        try:
            exit_code = ctypes.c_ulong()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
                return False
            return exit_code.value == still_active
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True
