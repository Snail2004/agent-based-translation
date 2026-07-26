"""Bounded, restart-safe scheduling for standalone Evaluation work.

Workers return already validated scorer-artifact bytes.  Only the scheduler
persists artifacts and receipts, so provider work may overlap without allowing
multiple writers to corrupt checkpoint state.
"""

from __future__ import annotations

import copy
import json
import os
import re
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Mapping, Sequence

from pipeline.eval.contracts_v1 import ContractValidationError
from pipeline.llm_backend import canonical_json, canonical_sha256


PROFILE_SCHEMA_ID = "StandaloneEvaluationConcurrencyProfileV1"
PLAN_SCHEMA_ID = "StandaloneEvaluationTaskPlanV1"
RECEIPT_SCHEMA_ID = "StandaloneEvaluationTaskReceiptV1"
STATUS_SCHEMA_ID = "StandaloneEvaluationSchedulerStatusV1"
SCHEMA_VERSION = "1.0.0"

_IDENTIFIER = re.compile(r"^[a-z0-9][a-z0-9._-]{0,127}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_LANE_AUTHORITIES = frozenset(
    {"local_cpu", "local_code", "physical_quota_bucket"}
)
_OPERATIONS = frozenset(
    {
        "sf_qe",
        "sf_bt_reverse",
        "sf_bt_semantic",
        "mtq5_orientation",
        "tc_occ",
        "ta_occ",
    }
)


@dataclass(frozen=True, slots=True)
class StandaloneEvaluationTaskV1:
    task_id: str
    ordinal: int
    operation_id: str
    lane_id: str
    input_sha256: str
    execution_binding_sha256: str
    dependency_task_ids: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class StandaloneEvaluationRunResultV1:
    state: str
    accepted_task_ids: tuple[str, ...]
    failed_task_ids: tuple[str, ...]
    pending_task_ids: tuple[str, ...]
    plan_sha256: str
    profile_sha256: str
    status_path: Path


class StandaloneTaskFailureV1(RuntimeError):
    """Expected safe failure whose code may be persisted in a receipt."""

    def __init__(self, error_code: str) -> None:
        self.error_code = _identifier(error_code, "$.error_code")
        super().__init__(f"standalone Evaluation task failed: {self.error_code}")


StandaloneTaskExecutorV1 = Callable[
    [StandaloneEvaluationTaskV1, int, Mapping[str, bytes]], bytes
]


def build_standalone_concurrency_profile_v1(
    *,
    profile_id: str,
    assignment_sha256: str,
    max_in_flight: int,
    lanes: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    draft = {
        "schema_id": PROFILE_SCHEMA_ID,
        "schema_version": SCHEMA_VERSION,
        "profile_id": profile_id,
        "assignment_sha256": assignment_sha256,
        "max_in_flight": max_in_flight,
        "lanes": [copy.deepcopy(dict(row)) for row in lanes],
        "integrity": {"profile_sha256": "0" * 64},
    }
    normalized = validate_standalone_concurrency_profile_v1(draft, verify_hash=False)
    normalized["integrity"]["profile_sha256"] = _self_hash(
        normalized, ("integrity", "profile_sha256")
    )
    return validate_standalone_concurrency_profile_v1(normalized)


def validate_standalone_concurrency_profile_v1(
    value: Mapping[str, Any], *, verify_hash: bool = True
) -> dict[str, Any]:
    row = _mapping(value, "$")
    _exact_keys(
        row,
        {
            "schema_id",
            "schema_version",
            "profile_id",
            "assignment_sha256",
            "max_in_flight",
            "lanes",
            "integrity",
        },
        "$",
    )
    if row["schema_id"] != PROFILE_SCHEMA_ID or row["schema_version"] != SCHEMA_VERSION:
        raise ContractValidationError("schema", "$", "unsupported concurrency profile")
    max_in_flight = _positive_int(row["max_in_flight"], "$.max_in_flight")
    lane_rows = _list(row["lanes"], "$.lanes")
    if not lane_rows:
        raise ContractValidationError("empty_array", "$.lanes", "at least one lane is required")
    lanes: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, raw_lane in enumerate(lane_rows):
        path = f"$.lanes[{index}]"
        lane = _mapping(raw_lane, path)
        _exact_keys(lane, {"lane_id", "authority_kind", "worker_limit"}, path)
        lane_id = _identifier(lane["lane_id"], f"{path}.lane_id")
        if lane_id in seen:
            raise ContractValidationError("duplicate", f"{path}.lane_id", "lane is repeated")
        authority = lane["authority_kind"]
        if authority not in _LANE_AUTHORITIES:
            raise ContractValidationError("enum", f"{path}.authority_kind", "unknown lane authority")
        worker_limit = _positive_int(lane["worker_limit"], f"{path}.worker_limit")
        if authority == "physical_quota_bucket" and worker_limit != 1:
            raise ContractValidationError(
                "quota_concurrency",
                f"{path}.worker_limit",
                "one physical quota bucket permits exactly one active call",
            )
        lanes.append(
            {
                "lane_id": lane_id,
                "authority_kind": authority,
                "worker_limit": worker_limit,
            }
        )
        seen.add(lane_id)
    if max_in_flight > sum(item["worker_limit"] for item in lanes):
        raise ContractValidationError(
            "concurrency_cap",
            "$.max_in_flight",
            "global cap exceeds the sum of lane caps",
        )
    integrity = _mapping(row["integrity"], "$.integrity")
    _exact_keys(integrity, {"profile_sha256"}, "$.integrity")
    normalized = {
        "schema_id": PROFILE_SCHEMA_ID,
        "schema_version": SCHEMA_VERSION,
        "profile_id": _identifier(row["profile_id"], "$.profile_id"),
        "assignment_sha256": _sha256(row["assignment_sha256"], "$.assignment_sha256"),
        "max_in_flight": max_in_flight,
        "lanes": lanes,
        "integrity": {
            "profile_sha256": _sha256(
                integrity["profile_sha256"], "$.integrity.profile_sha256"
            )
        },
    }
    if verify_hash and normalized["integrity"]["profile_sha256"] != _self_hash(
        normalized, ("integrity", "profile_sha256")
    ):
        raise ContractValidationError("profile_hash", "$.integrity.profile_sha256", "profile hash drift")
    return normalized


def build_standalone_task_plan_v1(
    *,
    plan_id: str,
    profile: Mapping[str, Any],
    tasks: Sequence[StandaloneEvaluationTaskV1],
) -> dict[str, Any]:
    normalized_profile = validate_standalone_concurrency_profile_v1(profile)
    draft = {
        "schema_id": PLAN_SCHEMA_ID,
        "schema_version": SCHEMA_VERSION,
        "plan_id": plan_id,
        "profile_id": normalized_profile["profile_id"],
        "profile_sha256": normalized_profile["integrity"]["profile_sha256"],
        "tasks": [_task_to_json(row) for row in tasks],
        "integrity": {"plan_sha256": "0" * 64},
    }
    normalized = validate_standalone_task_plan_v1(draft, profile=profile, verify_hash=False)
    normalized["integrity"]["plan_sha256"] = _self_hash(
        normalized, ("integrity", "plan_sha256")
    )
    return validate_standalone_task_plan_v1(normalized, profile=profile)


def validate_standalone_task_plan_v1(
    value: Mapping[str, Any],
    *,
    profile: Mapping[str, Any],
    verify_hash: bool = True,
) -> dict[str, Any]:
    normalized_profile = validate_standalone_concurrency_profile_v1(profile)
    row = _mapping(value, "$")
    _exact_keys(
        row,
        {"schema_id", "schema_version", "plan_id", "profile_id", "profile_sha256", "tasks", "integrity"},
        "$",
    )
    if row["schema_id"] != PLAN_SCHEMA_ID or row["schema_version"] != SCHEMA_VERSION:
        raise ContractValidationError("schema", "$", "unsupported standalone plan")
    if row["profile_id"] != normalized_profile["profile_id"]:
        raise ContractValidationError("profile_binding", "$.profile_id", "plan names another profile")
    if row["profile_sha256"] != normalized_profile["integrity"]["profile_sha256"]:
        raise ContractValidationError("profile_binding", "$.profile_sha256", "plan profile hash drift")
    lane_ids = {item["lane_id"] for item in normalized_profile["lanes"]}
    raw_tasks = _list(row["tasks"], "$.tasks")
    if not raw_tasks:
        raise ContractValidationError("empty_array", "$.tasks", "at least one task is required")
    tasks = [_validate_task(item, index, lane_ids) for index, item in enumerate(raw_tasks)]
    task_ids = [item["task_id"] for item in tasks]
    if len(set(task_ids)) != len(task_ids):
        raise ContractValidationError("duplicate", "$.tasks", "task_id is repeated")
    ordinals = [item["ordinal"] for item in tasks]
    if ordinals != list(range(1, len(tasks) + 1)):
        raise ContractValidationError("task_order", "$.tasks", "task ordinals must be contiguous plan order")
    known: set[str] = set()
    for index, task in enumerate(tasks):
        for dependency in task["dependency_task_ids"]:
            if dependency not in known:
                raise ContractValidationError(
                    "dependency_order",
                    f"$.tasks[{index}].dependency_task_ids",
                    "dependencies must name an earlier task",
                )
        known.add(task["task_id"])
    integrity = _mapping(row["integrity"], "$.integrity")
    _exact_keys(integrity, {"plan_sha256"}, "$.integrity")
    normalized = {
        "schema_id": PLAN_SCHEMA_ID,
        "schema_version": SCHEMA_VERSION,
        "plan_id": _identifier(row["plan_id"], "$.plan_id"),
        "profile_id": normalized_profile["profile_id"],
        "profile_sha256": normalized_profile["integrity"]["profile_sha256"],
        "tasks": tasks,
        "integrity": {"plan_sha256": _sha256(integrity["plan_sha256"], "$.integrity.plan_sha256")},
    }
    if verify_hash and normalized["integrity"]["plan_sha256"] != _self_hash(
        normalized, ("integrity", "plan_sha256")
    ):
        raise ContractValidationError("plan_hash", "$.integrity.plan_sha256", "plan hash drift")
    return normalized


def run_standalone_evaluation_tasks_v1(
    *,
    output_root: Path,
    profile: Mapping[str, Any],
    plan: Mapping[str, Any],
    executor: StandaloneTaskExecutorV1,
    resume: bool,
) -> StandaloneEvaluationRunResultV1:
    """Run ready tasks concurrently and checkpoint each completed attempt.

    The executor must return canonical, locally validated scorer artifact bytes.
    Raw provider responses must stay in the shared backend evidence store.
    """

    normalized_profile = validate_standalone_concurrency_profile_v1(profile)
    normalized_plan = validate_standalone_task_plan_v1(plan, profile=normalized_profile)
    root = Path(output_root).resolve()
    root.mkdir(parents=True, exist_ok=True)
    manifest_path = root / "manifest.json"
    _create_or_equal(manifest_path, _json_bytes(normalized_plan))
    lock_path = root / "scheduler.lock"
    descriptor = _acquire_lock(lock_path)
    try:
        receipts_root = root / "receipts"
        artifacts_root = root / "artifacts"
        receipts_root.mkdir(exist_ok=True)
        artifacts_root.mkdir(exist_ok=True)
        tasks = [_task_from_json(row) for row in normalized_plan["tasks"]]
        accepted, latest_attempt = _audit_receipts(
            receipts_root=receipts_root,
            artifacts_root=artifacts_root,
            plan=normalized_plan,
        )
        if accepted and not resume and len(accepted) != len(tasks):
            raise ContractValidationError(
                "resume_required", str(root), "partial checkpoint exists; pass resume=True"
            )
        task_by_id = {task.task_id: task for task in tasks}
        lane_limits = {row["lane_id"]: row["worker_limit"] for row in normalized_profile["lanes"]}
        lane_active = {lane_id: 0 for lane_id in lane_limits}
        running: dict[Future[bytes], tuple[StandaloneEvaluationTaskV1, int]] = {}
        failed: set[str] = set()
        attempted_this_run: set[str] = set()
        with ThreadPoolExecutor(max_workers=normalized_profile["max_in_flight"]) as pool:
            while True:
                if not failed:
                    for task in tasks:
                        if len(running) >= normalized_profile["max_in_flight"]:
                            break
                        if task.task_id in accepted or task.task_id in attempted_this_run:
                            continue
                        if lane_active[task.lane_id] >= lane_limits[task.lane_id]:
                            continue
                        if any(item not in accepted for item in task.dependency_task_ids):
                            continue
                        attempt_index = latest_attempt.get(task.task_id, 0) + 1
                        dependencies = {
                            item: _read_accepted_artifact(accepted[item], root)
                            for item in task.dependency_task_ids
                        }
                        future = pool.submit(executor, task, attempt_index, dependencies)
                        running[future] = (task, attempt_index)
                        attempted_this_run.add(task.task_id)
                        lane_active[task.lane_id] += 1
                if not running:
                    break
                done, _ = wait(tuple(running), return_when=FIRST_COMPLETED)
                for future in done:
                    task, attempt_index = running.pop(future)
                    lane_active[task.lane_id] -= 1
                    try:
                        artifact_bytes = future.result()
                        if not isinstance(artifact_bytes, bytes) or not artifact_bytes:
                            raise StandaloneTaskFailureV1("empty_validated_artifact")
                    except StandaloneTaskFailureV1 as exc:
                        receipt = _failed_receipt(normalized_plan, task, attempt_index, exc.error_code)
                        failed.add(task.task_id)
                    except Exception:
                        receipt = _failed_receipt(normalized_plan, task, attempt_index, "internal_execution_error")
                        failed.add(task.task_id)
                    else:
                        artifact_relative = PurePosixPath("artifacts", task.task_id, f"attempt_{attempt_index:04d}.json")
                        artifact_path = root.joinpath(*artifact_relative.parts)
                        _create_or_equal(artifact_path, artifact_bytes)
                        receipt = _accepted_receipt(
                            normalized_plan,
                            task,
                            attempt_index,
                            str(artifact_relative),
                            artifact_bytes,
                        )
                    receipt_path = receipts_root / task.task_id / f"attempt_{attempt_index:04d}.json"
                    _create_or_equal(receipt_path, _json_bytes(receipt))
                    latest_attempt[task.task_id] = attempt_index
                    if receipt["status"] == "accepted":
                        accepted[task.task_id] = receipt
                _write_status(root, normalized_plan, normalized_profile, tasks, accepted, failed)
        pending = tuple(task.task_id for task in tasks if task.task_id not in accepted and task.task_id not in failed)
        state = "completed" if len(accepted) == len(tasks) else "halted"
        status_path = _write_status(root, normalized_plan, normalized_profile, tasks, accepted, failed)
        return StandaloneEvaluationRunResultV1(
            state=state,
            accepted_task_ids=tuple(task.task_id for task in tasks if task.task_id in accepted),
            failed_task_ids=tuple(task.task_id for task in tasks if task.task_id in failed),
            pending_task_ids=pending,
            plan_sha256=normalized_plan["integrity"]["plan_sha256"],
            profile_sha256=normalized_profile["integrity"]["profile_sha256"],
            status_path=status_path,
        )
    finally:
        os.close(descriptor)
        lock_path.unlink(missing_ok=True)


def _audit_receipts(
    *, receipts_root: Path, artifacts_root: Path, plan: Mapping[str, Any]
) -> tuple[dict[str, dict[str, Any]], dict[str, int]]:
    accepted: dict[str, dict[str, Any]] = {}
    latest: dict[str, int] = {}
    tasks = {row["task_id"]: row for row in plan["tasks"]}
    for path in sorted(receipts_root.glob("*/attempt_*.json")):
        receipt = _validate_receipt(json.loads(path.read_text(encoding="utf-8")), plan=plan)
        task_id = receipt["task_id"]
        if task_id not in tasks:
            raise ContractValidationError("foreign_task", str(path), "receipt task is outside plan")
        if receipt["task_input_sha256"] != tasks[task_id]["input_sha256"]:
            raise ContractValidationError("input_binding", str(path), "receipt input differs from task")
        expected = latest.get(task_id, 0) + 1
        if receipt["attempt_index"] != expected:
            raise ContractValidationError("attempt_sequence", str(path), "attempt sequence is not contiguous")
        latest[task_id] = expected
        if task_id in accepted:
            raise ContractValidationError("duplicate_acceptance", str(path), "accepted task has a later attempt")
        if receipt["status"] == "accepted":
            artifact_path = _contained_relative_path(artifacts_root.parent, receipt["artifact_ref"])
            if not artifact_path.is_file():
                raise ContractValidationError("artifact_missing", str(path), "accepted artifact is absent")
            payload = artifact_path.read_bytes()
            if len(payload) != receipt["artifact_size"] or _bytes_sha256(payload) != receipt["artifact_sha256"]:
                raise ContractValidationError("artifact_hash", str(artifact_path), "accepted artifact drift")
            accepted[task_id] = receipt
    return accepted, latest


def _write_status(
    root: Path,
    plan: Mapping[str, Any],
    profile: Mapping[str, Any],
    tasks: Sequence[StandaloneEvaluationTaskV1],
    accepted: Mapping[str, Mapping[str, Any]],
    failed: set[str],
) -> Path:
    accepted_ids = [task.task_id for task in tasks if task.task_id in accepted]
    failed_ids = [task.task_id for task in tasks if task.task_id in failed]
    pending_ids = [task.task_id for task in tasks if task.task_id not in accepted and task.task_id not in failed]
    body = {
        "schema_id": STATUS_SCHEMA_ID,
        "schema_version": SCHEMA_VERSION,
        "state": "completed" if len(accepted_ids) == len(tasks) else "halted" if failed_ids else "running",
        "plan_sha256": plan["integrity"]["plan_sha256"],
        "profile_sha256": profile["integrity"]["profile_sha256"],
        "accepted_task_ids": accepted_ids,
        "failed_task_ids": failed_ids,
        "pending_task_ids": pending_ids,
    }
    payload = {**body, "status_sha256": canonical_sha256(body)}
    path = root / "status.json"
    _replace_json(path, payload)
    return path


def _accepted_receipt(
    plan: Mapping[str, Any], task: StandaloneEvaluationTaskV1, attempt_index: int, artifact_ref: str, payload: bytes
) -> dict[str, Any]:
    body = _receipt_base(plan, task, attempt_index, "accepted")
    body.update(
        {
            "artifact_ref": artifact_ref,
            "artifact_sha256": _bytes_sha256(payload),
            "artifact_size": len(payload),
            "error_code": None,
        }
    )
    return {**body, "receipt_sha256": canonical_sha256(body)}


def _failed_receipt(
    plan: Mapping[str, Any], task: StandaloneEvaluationTaskV1, attempt_index: int, error_code: str
) -> dict[str, Any]:
    body = _receipt_base(plan, task, attempt_index, "failed")
    body.update(
        {
            "artifact_ref": None,
            "artifact_sha256": None,
            "artifact_size": None,
            "error_code": _identifier(error_code, "$.error_code"),
        }
    )
    return {**body, "receipt_sha256": canonical_sha256(body)}


def _receipt_base(
    plan: Mapping[str, Any], task: StandaloneEvaluationTaskV1, attempt_index: int, status: str
) -> dict[str, Any]:
    return {
        "schema_id": RECEIPT_SCHEMA_ID,
        "schema_version": SCHEMA_VERSION,
        "plan_sha256": plan["integrity"]["plan_sha256"],
        "profile_sha256": plan["profile_sha256"],
        "task_id": task.task_id,
        "task_input_sha256": task.input_sha256,
        "execution_binding_sha256": task.execution_binding_sha256,
        "attempt_index": attempt_index,
        "status": status,
    }


def _validate_receipt(value: Mapping[str, Any], *, plan: Mapping[str, Any]) -> dict[str, Any]:
    row = _mapping(value, "$")
    required = {
        "schema_id", "schema_version", "plan_sha256", "profile_sha256", "task_id",
        "task_input_sha256", "execution_binding_sha256", "attempt_index", "status",
        "artifact_ref", "artifact_sha256", "artifact_size", "error_code", "receipt_sha256",
    }
    _exact_keys(row, required, "$")
    if row["schema_id"] != RECEIPT_SCHEMA_ID or row["schema_version"] != SCHEMA_VERSION:
        raise ContractValidationError("schema", "$", "unsupported receipt")
    if row["plan_sha256"] != plan["integrity"]["plan_sha256"] or row["profile_sha256"] != plan["profile_sha256"]:
        raise ContractValidationError("receipt_binding", "$", "receipt belongs to another plan/profile")
    status = row["status"]
    if status not in {"accepted", "failed"}:
        raise ContractValidationError("enum", "$.status", "unknown receipt status")
    normalized = {
        "schema_id": RECEIPT_SCHEMA_ID,
        "schema_version": SCHEMA_VERSION,
        "plan_sha256": _sha256(row["plan_sha256"], "$.plan_sha256"),
        "profile_sha256": _sha256(row["profile_sha256"], "$.profile_sha256"),
        "task_id": _identifier(row["task_id"], "$.task_id"),
        "task_input_sha256": _sha256(row["task_input_sha256"], "$.task_input_sha256"),
        "execution_binding_sha256": _sha256(row["execution_binding_sha256"], "$.execution_binding_sha256"),
        "attempt_index": _positive_int(row["attempt_index"], "$.attempt_index"),
        "status": status,
        "artifact_ref": row["artifact_ref"],
        "artifact_sha256": row["artifact_sha256"],
        "artifact_size": row["artifact_size"],
        "error_code": row["error_code"],
        "receipt_sha256": _sha256(row["receipt_sha256"], "$.receipt_sha256"),
    }
    if status == "accepted":
        normalized["artifact_ref"] = _relative_path(row["artifact_ref"], "$.artifact_ref")
        normalized["artifact_sha256"] = _sha256(row["artifact_sha256"], "$.artifact_sha256")
        normalized["artifact_size"] = _positive_int(row["artifact_size"], "$.artifact_size")
        if row["error_code"] is not None:
            raise ContractValidationError("receipt_shape", "$.error_code", "accepted receipt cannot carry error")
    else:
        if any(row[key] is not None for key in ("artifact_ref", "artifact_sha256", "artifact_size")):
            raise ContractValidationError("receipt_shape", "$", "failed receipt cannot carry artifact")
        normalized["error_code"] = _identifier(row["error_code"], "$.error_code")
    body = dict(normalized)
    receipt_sha256 = body.pop("receipt_sha256")
    if canonical_sha256(body) != receipt_sha256:
        raise ContractValidationError("receipt_hash", "$.receipt_sha256", "receipt hash drift")
    normalized["receipt_sha256"] = receipt_sha256
    return normalized


def _validate_task(value: Any, index: int, lane_ids: set[str]) -> dict[str, Any]:
    path = f"$.tasks[{index}]"
    row = _mapping(value, path)
    _exact_keys(
        row,
        {"task_id", "ordinal", "operation_id", "lane_id", "input_sha256", "execution_binding_sha256", "dependency_task_ids"},
        path,
    )
    operation = row["operation_id"]
    if operation not in _OPERATIONS:
        raise ContractValidationError("enum", f"{path}.operation_id", "unknown operation")
    lane_id = _identifier(row["lane_id"], f"{path}.lane_id")
    if lane_id not in lane_ids:
        raise ContractValidationError("lane_reference", f"{path}.lane_id", "task names unknown lane")
    dependencies = [_identifier(item, f"{path}.dependency_task_ids") for item in _list(row["dependency_task_ids"], f"{path}.dependency_task_ids")]
    if len(set(dependencies)) != len(dependencies):
        raise ContractValidationError("duplicate", f"{path}.dependency_task_ids", "dependency is repeated")
    return {
        "task_id": _identifier(row["task_id"], f"{path}.task_id"),
        "ordinal": _positive_int(row["ordinal"], f"{path}.ordinal"),
        "operation_id": operation,
        "lane_id": lane_id,
        "input_sha256": _sha256(row["input_sha256"], f"{path}.input_sha256"),
        "execution_binding_sha256": _sha256(row["execution_binding_sha256"], f"{path}.execution_binding_sha256"),
        "dependency_task_ids": dependencies,
    }


def _task_to_json(task: StandaloneEvaluationTaskV1) -> dict[str, Any]:
    return {
        "task_id": task.task_id,
        "ordinal": task.ordinal,
        "operation_id": task.operation_id,
        "lane_id": task.lane_id,
        "input_sha256": task.input_sha256,
        "execution_binding_sha256": task.execution_binding_sha256,
        "dependency_task_ids": list(task.dependency_task_ids),
    }


def _task_from_json(row: Mapping[str, Any]) -> StandaloneEvaluationTaskV1:
    return StandaloneEvaluationTaskV1(
        task_id=row["task_id"],
        ordinal=row["ordinal"],
        operation_id=row["operation_id"],
        lane_id=row["lane_id"],
        input_sha256=row["input_sha256"],
        execution_binding_sha256=row["execution_binding_sha256"],
        dependency_task_ids=tuple(row["dependency_task_ids"]),
    )


def _read_accepted_artifact(receipt: Mapping[str, Any], root: Path) -> bytes:
    path = _contained_relative_path(root, receipt["artifact_ref"])
    payload = path.read_bytes()
    if _bytes_sha256(payload) != receipt["artifact_sha256"]:
        raise ContractValidationError("artifact_hash", str(path), "dependency artifact drift")
    return payload


def _contained_relative_path(root: Path, relative: str) -> Path:
    child = root.joinpath(*PurePosixPath(relative).parts).resolve()
    try:
        child.relative_to(root.resolve())
    except ValueError as exc:
        raise ContractValidationError("path_escape", relative, "path escapes output root") from exc
    return child


def _relative_path(value: Any, path: str) -> str:
    if not isinstance(value, str) or not value:
        raise ContractValidationError("type", path, "expected relative path")
    parsed = PurePosixPath(value)
    if parsed.is_absolute() or ".." in parsed.parts or "\\" in value:
        raise ContractValidationError("relative_path", path, "unsafe relative path")
    return str(parsed)


def _acquire_lock(path: Path) -> int:
    try:
        return os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError as exc:
        raise ContractValidationError("run_locked", str(path), "another scheduler owns this run") from exc


def _create_or_equal(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        if path.read_bytes() != payload:
            raise ContractValidationError("immutable_drift", str(path), "immutable file has different bytes")
        return
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(payload)
        stream.flush()
        os.fsync(stream.fileno())


def _replace_json(path: Path, payload: Mapping[str, Any]) -> None:
    temp = path.with_suffix(".tmp")
    temp.write_bytes(_json_bytes(payload))
    os.replace(temp, path)


def _json_bytes(value: Mapping[str, Any]) -> bytes:
    return (canonical_json(value) + "\n").encode("utf-8")


def _self_hash(value: Mapping[str, Any], path: tuple[str, str]) -> str:
    detached = copy.deepcopy(dict(value))
    detached[path[0]].pop(path[1], None)
    return canonical_sha256(detached)


def _bytes_sha256(value: bytes) -> str:
    from hashlib import sha256

    return sha256(value).hexdigest()


def _mapping(value: Any, path: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ContractValidationError("type", path, "expected object")
    return value

def _list(value: Any, path: str) -> list[Any]:
    if not isinstance(value, list):
        raise ContractValidationError("type", path, "expected array")
    return value


def _exact_keys(value: Mapping[str, Any], required: set[str], path: str) -> None:
    actual = set(value)
    if actual != required:
        missing = sorted(required - actual)
        unknown = sorted(actual - required)
        raise ContractValidationError("object_keys", path, f"missing={missing}; unknown={unknown}")


def _identifier(value: Any, path: str) -> str:
    if not isinstance(value, str) or _IDENTIFIER.fullmatch(value) is None:
        raise ContractValidationError("identifier", path, "invalid identifier")
    return value


def _sha256(value: Any, path: str) -> str:
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        raise ContractValidationError("sha256", path, "expected lowercase SHA-256")
    return value


def _positive_int(value: Any, path: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ContractValidationError("positive_int", path, "expected positive integer")
    return value
