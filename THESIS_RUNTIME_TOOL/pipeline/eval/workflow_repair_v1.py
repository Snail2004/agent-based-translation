"""Closed, append-only contracts for selective Evaluation repair.

Phase B repairs an operational code failure inside an already halted
Evaluation component.  The module deliberately carries no scoring logic and
does not discover files.  A caller must provide the exact affected work set
and the exact artifact bindings that were accepted before the repair.
"""

from __future__ import annotations

import copy
import json
import re
from pathlib import Path
from typing import Any, Mapping, Sequence

from pipeline.eval.contracts_v1 import (
    CanonicalPolicy,
    ContractValidationError,
    canonical_sha256,
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
    verify_payload_hash,
)
from pipeline.eval.workflow_component_v1 import validate_typed_artifact_binding_v1

__all__ = [
    "build_evaluation_repair_plan_v1",
    "build_evaluation_repair_receipt_v1",
    "load_evaluation_repair_plan_v1",
    "load_evaluation_repair_receipt_v1",
    "repair_plan_path_v1",
    "repair_receipt_path_v1",
    "validate_evaluation_repair_plan_v1",
    "validate_evaluation_repair_receipt_v1",
    "write_evaluation_repair_plan_v1",
    "write_evaluation_repair_receipt_v1",
]


SCHEMA_VERSION = "1.0.0"
REPAIR_PLAN_SCHEMA_ID = "EvaluationWorkflowRepairPlanV1"
REPAIR_RECEIPT_SCHEMA_ID = "EvaluationWorkflowRepairReceiptV1"
_PLAN_HASH_PATH = ("integrity", "plan_sha256")
_RECEIPT_HASH_PATH = ("integrity", "receipt_sha256")
_ZERO_HASH = "0" * 64
_ATTEMPT_ID_RE = re.compile(r"^evalcomp_attempt_[0-9]{4}$")
_REPAIR_ID_RE = re.compile(r"^evalrepair_[0-9a-f]{32}$")
_POLICY = CanonicalPolicy(
    set_like_paths=frozenset(),
    semantic_sequence_paths=frozenset(
        {
            ("affected_work_ids",),
            ("supersede_work_ids",),
            ("superseded_work_ids",),
            ("rerun_work_ids",),
            ("prior_accepted_artifacts",),
            ("unaffected_accepted_artifacts",),
            ("repaired_results",),
            ("current_accepted_artifacts",),
            ("semantic_bindings", "arm_ids"),
            ("semantic_bindings", "input_bindings"),
        }
    ),
)
_SEMANTIC_KEYS = (
    "input_set_sha256",
    "settings_sha256",
    "evaluation_profile_sha256",
    "stage_plan_sha256",
    "sampling_sha256",
    "semantic_contract_sha256",
)


def _json_bytes(value: Mapping[str, Any]) -> bytes:
    return (
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
        + b"\n"
    )


def _write_immutable_json(path: Path, value: Mapping[str, Any]) -> None:
    payload = _json_bytes(value)
    if path.exists():
        if path.is_symlink() or not path.is_file() or path.read_bytes() != payload:
            raise ContractValidationError(
                "repair_immutable_conflict",
                str(path),
                "repair artifact bytes changed",
            )
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    try:
        temporary.write_bytes(payload)
        temporary.replace(path)
    finally:
        if temporary.exists():
            temporary.unlink()


def _load_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ContractValidationError(
            "repair_json", str(path), "repair artifact is not readable JSON"
        ) from exc
    return require_mapping(value, path=str(path))


def _attempt_id(index: int) -> str:
    return f"evalcomp_attempt_{index:04d}"


def _validate_attempt_id(value: Any, *, path: str) -> str:
    result = require_string(value, path=path)
    if _ATTEMPT_ID_RE.fullmatch(result) is None:
        raise ContractValidationError(
            "repair_attempt_id", path, "invalid Evaluation component attempt ID"
        )
    return result


def _validate_repair_id(value: Any, *, path: str) -> str:
    result = require_string(value, path=path)
    if _REPAIR_ID_RE.fullmatch(result) is None:
        raise ContractValidationError(
            "repair_id", path, "invalid Evaluation repair ID"
        )
    return result


def _validate_semantic_bindings(value: Any, *, path: str) -> dict[str, Any]:
    row = require_mapping(value, path=path)
    require_exact_keys(row, required=set(_SEMANTIC_KEYS), path=path)
    result: dict[str, Any] = {}
    for key in _SEMANTIC_KEYS:
        item = row[key]
        if key in {"arm_ids", "input_bindings"}:
            if not isinstance(item, list):
                raise ContractValidationError(
                    "repair_semantic_binding", f"{path}.{key}", "expected sealed sequence"
                )
            result[key] = copy.deepcopy(item)
        else:
            result[key] = require_sha256(item, path=f"{path}.{key}")
    return result


def _validate_artifact(value: Any, *, path: str) -> dict[str, str]:
    return validate_typed_artifact_binding_v1(value, path=path)


def _validate_artifact_rows(value: Any, *, path: str) -> list[dict[str, Any]]:
    rows = require_list(value, path=path)
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, raw in enumerate(rows):
        item_path = f"{path}[{index}]"
        row = require_mapping(raw, path=item_path)
        require_exact_keys(row, required={"work_id", "artifact"}, path=item_path)
        work_id = require_string(row["work_id"], path=f"{item_path}.work_id")
        if work_id in seen:
            raise ContractValidationError(
                "repair_duplicate_work", item_path, "work ID is repeated"
            )
        seen.add(work_id)
        result.append(
            {
                "work_id": work_id,
                "artifact": _validate_artifact(
                    row["artifact"], path=f"{item_path}.artifact"
                ),
            }
        )
    return result


def _validate_repaired_results(value: Any, *, path: str) -> list[dict[str, Any]]:
    rows = require_list(value, path=path)
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, raw in enumerate(rows):
        item_path = f"{path}[{index}]"
        row = require_mapping(raw, path=item_path)
        require_exact_keys(
            row,
            required={
                "work_id",
                "previous_artifact",
                "result_artifact",
                "report_artifact",
                "execution_artifact",
            },
            path=item_path,
        )
        work_id = require_string(row["work_id"], path=f"{item_path}.work_id")
        if work_id in seen:
            raise ContractValidationError(
                "repair_duplicate_work", item_path, "repaired work ID is repeated"
            )
        seen.add(work_id)
        result.append(
            {
                "work_id": work_id,
                "previous_artifact": (
                    None
                    if row["previous_artifact"] is None
                    else _validate_artifact(
                        row["previous_artifact"],
                        path=f"{item_path}.previous_artifact",
                    )
                ),
                "result_artifact": _validate_artifact(
                    row["result_artifact"], path=f"{item_path}.result_artifact"
                ),
                "report_artifact": _validate_artifact(
                    row["report_artifact"], path=f"{item_path}.report_artifact"
                ),
                "execution_artifact": _validate_artifact(
                    row["execution_artifact"], path=f"{item_path}.execution_artifact"
                ),
            }
        )
    return result


def _validate_plan_shape(value: Mapping[str, Any]) -> dict[str, Any]:
    row = require_mapping(value, path="$repair_plan")
    require_exact_keys(
        row,
        required={
            "schema_id",
            "schema_version",
            "repair_id",
            "workflow_run_id",
            "component_run_id",
            "source_component_attempt_id",
            "source_component_attempt_index",
            "target_component_attempt_id",
            "target_component_attempt_index",
            "assignment_sha256",
            "semantic_bindings",
            "reason_code",
            "authorized_by",
            "authorization_id",
            "authorized_at",
            "source_code_commit",
            "repair_code_commit",
            "affected_work_ids",
            "supersede_work_ids",
            "rerun_work_ids",
            "pre_repair_ledger_sha256",
            "pre_repair_checkpoint_sha256",
            "prior_accepted_artifacts",
            "unaffected_accepted_artifacts",
            "integrity",
        },
        path="$repair_plan",
    )
    integrity = require_mapping(row["integrity"], path="$repair_plan.integrity")
    require_exact_keys(integrity, required={"plan_sha256"}, path="$repair_plan.integrity")
    source_index = require_int(
        row["source_component_attempt_index"],
        path="$.repair_plan.source_component_attempt_index",
        minimum=1,
    )
    target_index = require_int(
        row["target_component_attempt_index"],
        path="$.repair_plan.target_component_attempt_index",
        minimum=1,
    )
    if target_index != source_index + 1:
        raise ContractValidationError(
            "repair_attempt_sequence",
            "$.repair_plan.target_component_attempt_index",
            "repair must advance exactly one component attempt",
        )
    source_id = _validate_attempt_id(
        row["source_component_attempt_id"],
        path="$.repair_plan.source_component_attempt_id",
    )
    target_id = _validate_attempt_id(
        row["target_component_attempt_id"],
        path="$.repair_plan.target_component_attempt_id",
    )
    if source_id != _attempt_id(source_index) or target_id != _attempt_id(target_index):
        raise ContractValidationError(
            "repair_attempt_binding",
            "$.repair_plan",
            "attempt ID and index disagree",
        )
    affected = [
        require_string(item, path="$.repair_plan.affected_work_ids[*]")
        for item in require_list(row["affected_work_ids"], path="$.repair_plan.affected_work_ids")
    ]
    supersede = [
        require_string(item, path="$.repair_plan.supersede_work_ids[*]")
        for item in require_list(row["supersede_work_ids"], path="$.repair_plan.supersede_work_ids")
    ]
    rerun = [
        require_string(item, path="$.repair_plan.rerun_work_ids[*]")
        for item in require_list(row["rerun_work_ids"], path="$.repair_plan.rerun_work_ids")
    ]
    for ids, name in ((affected, "affected"), (supersede, "supersede"), (rerun, "rerun")):
        require_unique(ids, path=f"$.repair_plan.{name}_work_ids[*]")
    if not affected:
        raise ContractValidationError(
            "repair_empty_set", "$.repair_plan.affected_work_ids", "repair needs at least one affected work"
        )
    if set(rerun) != set(affected) or not set(supersede).issubset(set(affected)):
        raise ContractValidationError(
            "repair_work_partition",
            "$.repair_plan",
            "supersede/rerun sets do not exactly describe the affected set",
        )
    prior = _validate_artifact_rows(
        row["prior_accepted_artifacts"], path="$.repair_plan.prior_accepted_artifacts"
    )
    unaffected = _validate_artifact_rows(
        row["unaffected_accepted_artifacts"],
        path="$.repair_plan.unaffected_accepted_artifacts",
    )
    prior_ids = {item["work_id"] for item in prior}
    if not set(supersede).issubset(prior_ids):
        raise ContractValidationError(
            "repair_prior_binding",
            "$.repair_plan.prior_accepted_artifacts",
            "every superseded work needs its prior accepted artifact",
        )
    if not set(item["work_id"] for item in unaffected).issubset(prior_ids):
        raise ContractValidationError(
            "repair_unaffected_binding",
            "$.repair_plan.unaffected_accepted_artifacts",
            "unaffected artifact is not in the prior accepted set",
        )
    normalized = {
        "schema_id": require_enum(
            row["schema_id"], {REPAIR_PLAN_SCHEMA_ID}, path="$.repair_plan.schema_id"
        ),
        "schema_version": require_enum(
            row["schema_version"], {SCHEMA_VERSION}, path="$.repair_plan.schema_version"
        ),
        "repair_id": _validate_repair_id(
            row["repair_id"], path="$.repair_plan.repair_id"
        ),
        "workflow_run_id": require_string(
            row["workflow_run_id"], path="$.repair_plan.workflow_run_id"
        ),
        "component_run_id": require_string(
            row["component_run_id"], path="$.repair_plan.component_run_id"
        ),
        "source_component_attempt_id": source_id,
        "source_component_attempt_index": source_index,
        "target_component_attempt_id": target_id,
        "target_component_attempt_index": target_index,
        "assignment_sha256": require_sha256(
            row["assignment_sha256"], path="$.repair_plan.assignment_sha256"
        ),
        "semantic_bindings": _validate_semantic_bindings(
            row["semantic_bindings"], path="$.repair_plan.semantic_bindings"
        ),
        "reason_code": require_string(row["reason_code"], path="$.repair_plan.reason_code"),
        "authorized_by": require_string(
            row["authorized_by"], path="$.repair_plan.authorized_by"
        ),
        "authorization_id": require_string(
            row["authorization_id"], path="$.repair_plan.authorization_id"
        ),
        "authorized_at": require_rfc3339(
            row["authorized_at"], path="$.repair_plan.authorized_at"
        ),
        "source_code_commit": require_commit(
            row["source_code_commit"], path="$.repair_plan.source_code_commit"
        ),
        "repair_code_commit": require_commit(
            row["repair_code_commit"], path="$.repair_plan.repair_code_commit"
        ),
        "affected_work_ids": affected,
        "supersede_work_ids": supersede,
        "rerun_work_ids": rerun,
        "pre_repair_ledger_sha256": require_sha256(
            row["pre_repair_ledger_sha256"],
            path="$.repair_plan.pre_repair_ledger_sha256",
        ),
        "pre_repair_checkpoint_sha256": require_nullable_string(
            row["pre_repair_checkpoint_sha256"],
            path="$.repair_plan.pre_repair_checkpoint_sha256",
        ),
        "prior_accepted_artifacts": prior,
        "unaffected_accepted_artifacts": unaffected,
        "integrity": {
            "plan_sha256": require_sha256(
                integrity["plan_sha256"],
                path="$.repair_plan.integrity.plan_sha256",
            )
        },
    }
    if normalized["pre_repair_checkpoint_sha256"] is not None:
        normalized["pre_repair_checkpoint_sha256"] = require_sha256(
            normalized["pre_repair_checkpoint_sha256"],
            path="$.repair_plan.pre_repair_checkpoint_sha256",
        )
    if not verify_payload_hash(normalized, policy=_POLICY, hash_path=_PLAN_HASH_PATH):
        raise ContractValidationError(
            "repair_plan_hash",
            "$.repair_plan.integrity.plan_sha256",
            "repair plan hash drift",
        )
    material = copy.deepcopy(normalized)
    material["repair_id"] = ""
    material["integrity"]["plan_sha256"] = _ZERO_HASH
    expected_id = "evalrepair_" + canonical_sha256(
        material, policy=_POLICY
    )[:32]
    if normalized["repair_id"] != expected_id:
        raise ContractValidationError(
            "repair_id",
            "$.repair_plan.repair_id",
            "repair identity drift",
        )
    return normalized


def build_evaluation_repair_plan_v1(
    *,
    assignment: Mapping[str, Any],
    ledger: Mapping[str, Any],
    source_component_attempt_id: str,
    source_component_attempt_index: int,
    assignment_sha256: str,
    pre_repair_checkpoint_sha256: str | None,
    reason_code: str,
    authorized_by: str,
    authorization_id: str,
    authorized_at: str,
    source_code_commit: str,
    repair_code_commit: str,
    affected_work_ids: Sequence[str],
) -> dict[str, Any]:
    accepted_assignment = require_mapping(assignment, path="$.assignment")
    accepted_ledger = require_mapping(ledger, path="$.ledger")
    source_index = require_int(
        source_component_attempt_index,
        path="$.source_component_attempt_index",
        minimum=1,
    )
    source_id = _validate_attempt_id(
        source_component_attempt_id, path="$.source_component_attempt_id"
    )
    if source_id != _attempt_id(source_index):
        raise ContractValidationError(
            "repair_attempt_binding",
            "$.source_component_attempt_id",
            "attempt ID and index disagree",
        )
    work_rows = {
        require_string(row["work_id"], path="$.ledger.works[*].work_id"): row
        for row in require_list(accepted_ledger.get("works"), path="$.ledger.works")
    }
    affected = [
        require_string(item, path="$.affected_work_ids[*]")
        for item in affected_work_ids
    ]
    require_unique(affected, path="$.affected_work_ids[*]")
    if not affected:
        raise ContractValidationError(
            "repair_empty_set", "$.affected_work_ids", "repair needs at least one affected work"
        )
    missing = [item for item in affected if item not in work_rows]
    if missing:
        raise ContractValidationError(
            "repair_work_missing", "$.affected_work_ids", f"unknown work IDs: {missing!r}"
        )
    supersede = [
        item for item in affected if work_rows[item]["state"] == "accepted"
    ]
    rerunnable_states = {"pending", "in_progress", "halted", "retryable_rejected"}
    if any(work_rows[item]["state"] not in {"accepted", *rerunnable_states} for item in affected):
        raise ContractValidationError(
            "repair_work_state",
            "$.affected_work_ids",
            "affected work is already superseded or terminally rejected",
        )
    prior = [
        {"work_id": row["work_id"], "artifact": copy.deepcopy(row["accepted_artifact"])}
        for row in work_rows.values()
        if row["state"] == "accepted" and row["accepted_artifact"] is not None
    ]
    unaffected = [row for row in prior if row["work_id"] not in set(affected)]
    assignment_bindings = _validate_semantic_bindings(
        accepted_assignment["semantic_bindings"],
        path="$.assignment.semantic_bindings",
    )
    base = {
        "schema_id": REPAIR_PLAN_SCHEMA_ID,
        "schema_version": SCHEMA_VERSION,
        "repair_id": "",
        "workflow_run_id": require_string(
            accepted_assignment["workflow_run_id"], path="$.assignment.workflow_run_id"
        ),
        "component_run_id": require_string(
            accepted_assignment["component_run_id"], path="$.assignment.component_run_id"
        ),
        "source_component_attempt_id": source_id,
        "source_component_attempt_index": source_index,
        "target_component_attempt_id": _attempt_id(source_index + 1),
        "target_component_attempt_index": source_index + 1,
        "assignment_sha256": require_sha256(
            assignment_sha256, path="$.assignment_sha256"
        ),
        "semantic_bindings": assignment_bindings,
        "reason_code": require_string(reason_code, path="$.reason_code"),
        "authorized_by": require_string(authorized_by, path="$.authorized_by"),
        "authorization_id": require_string(
            authorization_id, path="$.authorization_id"
        ),
        "authorized_at": require_rfc3339(authorized_at, path="$.authorized_at"),
        "source_code_commit": require_commit(
            source_code_commit, path="$.source_code_commit"
        ),
        "repair_code_commit": require_commit(
            repair_code_commit, path="$.repair_code_commit"
        ),
        "affected_work_ids": affected,
        "supersede_work_ids": supersede,
        "rerun_work_ids": list(affected),
        "pre_repair_ledger_sha256": require_sha256(
            accepted_ledger["integrity"]["ledger_sha256"],
            path="$.ledger.integrity.ledger_sha256",
        ),
        "pre_repair_checkpoint_sha256": (
            None
            if pre_repair_checkpoint_sha256 is None
            else require_sha256(
                pre_repair_checkpoint_sha256,
                path="$.pre_repair_checkpoint_sha256",
            )
        ),
        "prior_accepted_artifacts": prior,
        "unaffected_accepted_artifacts": unaffected,
        "integrity": {"plan_sha256": _ZERO_HASH},
    }
    material = copy.deepcopy(base)
    material["repair_id"] = ""
    material["integrity"]["plan_sha256"] = _ZERO_HASH
    base["repair_id"] = "evalrepair_" + canonical_sha256(
        material, policy=_POLICY
    )[:32]
    return _validate_plan_shape(
        seal_payload(base, policy=_POLICY, hash_path=_PLAN_HASH_PATH)
    )


def validate_evaluation_repair_plan_v1(
    value: Mapping[str, Any],
    *,
    assignment: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    normalized = _validate_plan_shape(value)
    if assignment is not None:
        accepted = require_mapping(assignment, path="$.assignment")
        if normalized["workflow_run_id"] != accepted["workflow_run_id"] or normalized[
            "component_run_id"
        ] != accepted["component_run_id"]:
            raise ContractValidationError(
                "repair_assignment_binding",
                "$.repair_plan",
                "repair plan belongs to another component",
            )
        if normalized["assignment_sha256"] != accepted["integrity"]["assignment_sha256"]:
            raise ContractValidationError(
                "repair_assignment_binding",
                "$.repair_plan.assignment_sha256",
                "repair plan binds a foreign assignment",
            )
        if normalized["semantic_bindings"] != accepted["semantic_bindings"]:
            raise ContractValidationError(
                "repair_semantic_binding",
                "$.repair_plan.semantic_bindings",
                "repair plan changes sealed semantic inputs",
            )
    return copy.deepcopy(normalized)


def _validate_receipt_shape(value: Mapping[str, Any]) -> dict[str, Any]:
    row = require_mapping(value, path="$repair_receipt")
    require_exact_keys(
        row,
        required={
            "schema_id",
            "schema_version",
            "repair_id",
            "plan_sha256",
            "workflow_run_id",
            "component_run_id",
            "component_attempt_id",
            "component_attempt_index",
            "repair_code_commit",
            "superseded_work_ids",
            "rerun_work_ids",
            "repaired_results",
            "unaffected_accepted_artifacts",
            "current_accepted_artifacts",
            "completed_at",
            "integrity",
        },
        path="$repair_receipt",
    )
    integrity = require_mapping(row["integrity"], path="$repair_receipt.integrity")
    require_exact_keys(
        integrity, required={"receipt_sha256"}, path="$repair_receipt.integrity"
    )
    index = require_int(
        row["component_attempt_index"],
        path="$.repair_receipt.component_attempt_index",
        minimum=1,
    )
    attempt_id = _validate_attempt_id(
        row["component_attempt_id"],
        path="$.repair_receipt.component_attempt_id",
    )
    if attempt_id != _attempt_id(index):
        raise ContractValidationError(
            "repair_attempt_binding",
            "$.repair_receipt.component_attempt_id",
            "attempt ID and index disagree",
        )
    superseded = [
        require_string(item, path="$.repair_receipt.superseded_work_ids[*]")
        for item in require_list(
            row["superseded_work_ids"], path="$.repair_receipt.superseded_work_ids"
        )
    ]
    rerun = [
        require_string(item, path="$.repair_receipt.rerun_work_ids[*]")
        for item in require_list(
            row["rerun_work_ids"], path="$.repair_receipt.rerun_work_ids"
        )
    ]
    require_unique(superseded, path="$.repair_receipt.superseded_work_ids[*]")
    require_unique(rerun, path="$.repair_receipt.rerun_work_ids[*]")
    result_rows = _validate_repaired_results(
        row["repaired_results"], path="$.repair_receipt.repaired_results"
    )
    result_ids = [item["work_id"] for item in result_rows]
    if set(result_ids) != set(rerun):
        raise ContractValidationError(
            "repair_result_cover",
            "$.repair_receipt.repaired_results",
            "repair results do not exactly cover rerun work IDs",
        )
    unaffected = _validate_artifact_rows(
        row["unaffected_accepted_artifacts"],
        path="$.repair_receipt.unaffected_accepted_artifacts",
    )
    current = _validate_artifact_rows(
        row["current_accepted_artifacts"],
        path="$.repair_receipt.current_accepted_artifacts",
    )
    current_ids = [item["work_id"] for item in current]
    unaffected_ids = {item["work_id"] for item in unaffected}
    if set(current_ids) != unaffected_ids | set(rerun):
        raise ContractValidationError(
            "repair_current_cover",
            "$.repair_receipt.current_accepted_artifacts",
            "current accepted set must equal unaffected plus rerun work",
        )
    normalized = {
        "schema_id": require_enum(
            row["schema_id"],
            {REPAIR_RECEIPT_SCHEMA_ID},
            path="$.repair_receipt.schema_id",
        ),
        "schema_version": require_enum(
            row["schema_version"],
            {SCHEMA_VERSION},
            path="$.repair_receipt.schema_version",
        ),
        "repair_id": _validate_repair_id(
            row["repair_id"], path="$.repair_receipt.repair_id"
        ),
        "plan_sha256": require_sha256(
            row["plan_sha256"], path="$.repair_receipt.plan_sha256"
        ),
        "workflow_run_id": require_string(
            row["workflow_run_id"], path="$.repair_receipt.workflow_run_id"
        ),
        "component_run_id": require_string(
            row["component_run_id"], path="$.repair_receipt.component_run_id"
        ),
        "component_attempt_id": attempt_id,
        "component_attempt_index": index,
        "repair_code_commit": require_commit(
            row["repair_code_commit"], path="$.repair_receipt.repair_code_commit"
        ),
        "superseded_work_ids": superseded,
        "rerun_work_ids": rerun,
        "repaired_results": result_rows,
        "unaffected_accepted_artifacts": unaffected,
        "current_accepted_artifacts": current,
        "completed_at": require_rfc3339(
            row["completed_at"], path="$.repair_receipt.completed_at"
        ),
        "integrity": {
            "receipt_sha256": require_sha256(
                integrity["receipt_sha256"],
                path="$.repair_receipt.integrity.receipt_sha256",
            )
        },
    }
    if not verify_payload_hash(
        normalized, policy=_POLICY, hash_path=_RECEIPT_HASH_PATH
    ):
        raise ContractValidationError(
            "repair_receipt_hash",
            "$.repair_receipt.integrity.receipt_sha256",
            "repair receipt hash drift",
        )
    return normalized


def build_evaluation_repair_receipt_v1(
    *,
    plan: Mapping[str, Any],
    component_attempt_id: str,
    component_attempt_index: int,
    repaired_results: Sequence[Mapping[str, Any]],
    current_accepted_artifacts: Sequence[Mapping[str, Any]],
    completed_at: str,
) -> dict[str, Any]:
    accepted_plan = _validate_plan_shape(plan)
    result_rows = _validate_repaired_results(
        repaired_results, path="$.repaired_results"
    )
    current_rows = _validate_artifact_rows(
        current_accepted_artifacts, path="$.current_accepted_artifacts"
    )
    if {row["work_id"] for row in result_rows} != set(
        accepted_plan["rerun_work_ids"]
    ):
        raise ContractValidationError(
            "repair_result_cover",
            "$.repaired_results",
            "results do not cover the sealed affected set",
        )
    prior_by_id = {
        row["work_id"]: row["artifact"]
        for row in accepted_plan["prior_accepted_artifacts"]
    }
    unaffected = [
        {"work_id": row["work_id"], "artifact": copy.deepcopy(row["artifact"])}
        for row in accepted_plan["unaffected_accepted_artifacts"]
    ]
    for row in unaffected:
        current = next(
            (item for item in current_rows if item["work_id"] == row["work_id"]),
            None,
        )
        if current is None or current["artifact"] != row["artifact"]:
            raise ContractValidationError(
                "repair_unaffected_drift",
                "$.current_accepted_artifacts",
                "unaffected accepted artifact changed",
            )
    result_by_id = {row["work_id"]: row for row in result_rows}
    current_by_id = {row["work_id"]: row["artifact"] for row in current_rows}
    for work_id in accepted_plan["rerun_work_ids"]:
        result = result_by_id[work_id]
        expected_previous = (
            prior_by_id[work_id]
            if work_id in accepted_plan["supersede_work_ids"]
            else None
        )
        if result["previous_artifact"] != expected_previous:
            raise ContractValidationError(
                "repair_prior_binding",
                "$.repaired_results",
                "repaired result does not echo its exact prior accepted artifact",
            )
        if current_by_id.get(work_id) != result["result_artifact"]:
            raise ContractValidationError(
                "repair_current_binding",
                "$.current_accepted_artifacts",
                "current accepted artifact differs from repaired result",
            )
    target_index = require_int(
        component_attempt_index, path="$.component_attempt_index", minimum=1
    )
    target_id = _validate_attempt_id(
        component_attempt_id, path="$.component_attempt_id"
    )
    if target_index < accepted_plan["target_component_attempt_index"]:
        raise ContractValidationError(
            "repair_attempt_binding",
            "$.component_attempt_id",
            "receipt predates the sealed initial repair attempt",
        )
    base = {
        "schema_id": REPAIR_RECEIPT_SCHEMA_ID,
        "schema_version": SCHEMA_VERSION,
        "repair_id": accepted_plan["repair_id"],
        "plan_sha256": accepted_plan["integrity"]["plan_sha256"],
        "workflow_run_id": accepted_plan["workflow_run_id"],
        "component_run_id": accepted_plan["component_run_id"],
        "component_attempt_id": target_id,
        "component_attempt_index": target_index,
        "repair_code_commit": accepted_plan["repair_code_commit"],
        "superseded_work_ids": list(accepted_plan["supersede_work_ids"]),
        "rerun_work_ids": list(accepted_plan["rerun_work_ids"]),
        "repaired_results": result_rows,
        "unaffected_accepted_artifacts": unaffected,
        "current_accepted_artifacts": current_rows,
        "completed_at": require_rfc3339(completed_at, path="$.completed_at"),
        "integrity": {"receipt_sha256": _ZERO_HASH},
    }
    return _validate_receipt_shape(
        seal_payload(base, policy=_POLICY, hash_path=_RECEIPT_HASH_PATH)
    )


def validate_evaluation_repair_receipt_v1(
    value: Mapping[str, Any],
    *,
    plan: Mapping[str, Any],
) -> dict[str, Any]:
    normalized = _validate_receipt_shape(value)
    accepted_plan = _validate_plan_shape(plan)
    if normalized["repair_id"] != accepted_plan["repair_id"] or normalized[
        "plan_sha256"
    ] != accepted_plan["integrity"]["plan_sha256"]:
        raise ContractValidationError(
            "repair_plan_binding",
            "$.repair_receipt",
            "receipt binds another repair plan",
        )
    if normalized["workflow_run_id"] != accepted_plan["workflow_run_id"] or normalized[
        "component_run_id"
    ] != accepted_plan["component_run_id"]:
        raise ContractValidationError(
            "repair_component_binding",
            "$.repair_receipt",
            "receipt belongs to another component",
        )
    if set(normalized["superseded_work_ids"]) != set(
        accepted_plan["supersede_work_ids"]
    ) or set(normalized["rerun_work_ids"]) != set(
        accepted_plan["rerun_work_ids"]
    ):
        raise ContractValidationError(
            "repair_work_binding",
            "$.repair_receipt",
            "receipt work set differs from plan",
        )
    if (
        normalized["component_attempt_index"]
        < accepted_plan["target_component_attempt_index"]
    ):
        raise ContractValidationError(
            "repair_attempt_binding",
            "$.repair_receipt.component_attempt_index",
            "receipt predates the sealed initial repair attempt",
        )
    expected_unaffected = accepted_plan["unaffected_accepted_artifacts"]
    if normalized["unaffected_accepted_artifacts"] != expected_unaffected:
        raise ContractValidationError(
            "repair_unaffected_drift",
            "$.repair_receipt.unaffected_accepted_artifacts",
            "receipt does not echo the plan's unaffected accepted artifacts",
        )
    prior_by_id = {
        row["work_id"]: row["artifact"]
        for row in accepted_plan["prior_accepted_artifacts"]
    }
    current_by_id = {
        row["work_id"]: row["artifact"]
        for row in normalized["current_accepted_artifacts"]
    }
    for result in normalized["repaired_results"]:
        work_id = result["work_id"]
        expected_previous = (
            prior_by_id[work_id]
            if work_id in accepted_plan["supersede_work_ids"]
            else None
        )
        if result["previous_artifact"] != expected_previous:
            raise ContractValidationError(
                "repair_prior_binding",
                "$.repair_receipt.repaired_results",
                "receipt changed the prior accepted artifact binding",
            )
        if current_by_id.get(work_id) != result["result_artifact"]:
            raise ContractValidationError(
                "repair_current_binding",
                "$.repair_receipt.current_accepted_artifacts",
                "receipt current artifact differs from repaired result",
            )
    return copy.deepcopy(normalized)


def repair_plan_path_v1(root: Path, repair_id: str) -> Path:
    repair = _validate_repair_id(repair_id, path="$.repair_id")
    return root.resolve() / "recovery" / "repairs" / repair / "plan.json"


def repair_receipt_path_v1(root: Path, repair_id: str) -> Path:
    repair = _validate_repair_id(repair_id, path="$.repair_id")
    return root.resolve() / "recovery" / "repairs" / repair / "receipt.json"


def write_evaluation_repair_plan_v1(
    root: Path, plan: Mapping[str, Any]
) -> Path:
    accepted = _validate_plan_shape(plan)
    path = repair_plan_path_v1(root, accepted["repair_id"])
    _write_immutable_json(path, accepted)
    return path


def write_evaluation_repair_receipt_v1(
    root: Path, receipt: Mapping[str, Any], *, plan: Mapping[str, Any]
) -> Path:
    accepted = validate_evaluation_repair_receipt_v1(receipt, plan=plan)
    path = repair_receipt_path_v1(root, accepted["repair_id"])
    _write_immutable_json(path, accepted)
    return path


def load_evaluation_repair_plan_v1(
    root: Path, repair_id: str, *, assignment: Mapping[str, Any] | None = None
) -> dict[str, Any]:
    path = repair_plan_path_v1(root, repair_id)
    return validate_evaluation_repair_plan_v1(_load_json(path), assignment=assignment)


def load_evaluation_repair_receipt_v1(
    root: Path, repair_id: str, *, plan: Mapping[str, Any]
) -> dict[str, Any]:
    path = repair_receipt_path_v1(root, repair_id)
    return validate_evaluation_repair_receipt_v1(_load_json(path), plan=plan)
