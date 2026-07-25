"""Small, append-only recovery ledger for the Evaluation component.

This module deliberately scopes recovery to one Evaluation component package.
It does not inspect the repository, Git status, UI files, or unrelated
pipeline files.  Semantic experiment bindings remain sealed; operational code
revisions are diagnostic lineage only.
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
import traceback
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from pipeline.eval.contracts_v1 import (
    CanonicalPolicy,
    ContractValidationError,
    canonical_sha256,
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

__all__ = [
    "EvaluationFailureClassificationV1",
    "EvaluationWorkflowRecoveryStoreV1",
    "build_evaluation_recovery_assignment_v1",
    "build_evaluation_work_descriptor_v1",
    "classify_evaluation_failure_v1",
    "derive_evaluation_work_id_v1",
    "validate_evaluation_recovery_package_v1",
]


SCHEMA_VERSION = "1.0.0"
ASSIGNMENT_SCHEMA_ID = "EvaluationRecoveryAssignmentV1"
WORK_SCHEMA_ID = "EvaluationRecoveryWorkDescriptorV1"
JOURNAL_SCHEMA_ID = "EvaluationRecoveryJournalRecordV1"
LEDGER_SCHEMA_ID = "EvaluationRecoveryWorkLedgerV1"
CHECKPOINT_SCHEMA_ID = "EvaluationRecoveryCheckpointV1"
INCIDENT_SCHEMA_ID = "EvaluationInternalIncidentV1"

_POLICY = CanonicalPolicy(
    set_like_paths=frozenset(),
    semantic_sequence_paths=frozenset(
        {
            ("arm_ids",),
            ("input_bindings",),
            # Journal payloads are canonicalized relative to the payload root,
            # so the work-start descriptor loses the outer ``payload`` prefix.
            ("descriptor", "arm_ids"),
            ("descriptor", "input_bindings"),
            ("payload", "descriptor", "arm_ids"),
            ("payload", "descriptor", "input_bindings"),
            ("works", "*", "physical_attempt_ids"),
            ("accepted_work_ids",),
            ("pending_work_ids",),
            ("halted_work_ids",),
            ("superseded_work_ids",),
            ("physical_attempt_ids",),
            ("records",),
            ("works",),
        }
    ),
)
_HASH_PATH = ("integrity", "sha256")
_ASSIGNMENT_HASH_PATH = ("integrity", "assignment_sha256")
_WORK_HASH_PATH = ("integrity", "work_sha256")
_JOURNAL_HASH_PATH = ("integrity", "journal_sha256")
_LEDGER_HASH_PATH = ("integrity", "ledger_sha256")
_CHECKPOINT_HASH_PATH = ("integrity", "checkpoint_sha256")
_INCIDENT_HASH_PATH = ("integrity", "incident_sha256")
_ZERO_HASH = "0" * 64

_SEMANTIC_BINDING_KEYS = (
    "input_set_sha256",
    "settings_sha256",
    "evaluation_profile_sha256",
    "stage_plan_sha256",
    "sampling_sha256",
    "semantic_contract_sha256",
)
_FAILURE_CATEGORIES = frozenset(
    {"transport", "semantic", "operational", "integrity", "user"}
)
_WORK_STATES = frozenset(
    {
        "pending",
        "in_progress",
        "accepted",
        "retryable_rejected",
        "halted",
        "superseded",
        "terminal_rejected",
    }
)
_JOURNAL_EVENTS = frozenset(
    {
        "work_started",
        "physical_attempt_sealed",
        "usage_recorded",
        "error_recorded",
        "response_recorded",
        "validation_recorded",
        "artifact_recorded",
        "work_accepted",
        "ledger_updated",
        "checkpoint_recorded",
        "work_halted",
        "work_retryable_rejected",
        "work_superseded",
        "incident_recorded",
        "component_resumed",
    }
)
_BOUNDARIES = frozenset(
    {
        "intent",
        "physical_seal",
        "usage_error",
        "response",
        "validation",
        "artifact",
        "accepted",
        "ledger",
        "checkpoint",
    }
)


class EvaluationRecoveryError(ContractValidationError):
    """A recoverable-package contract error with the normal Evaluation shape."""


@dataclass(frozen=True, slots=True)
class EvaluationFailureClassificationV1:
    category: str
    reason_code: str
    resume_available: bool


def _write_json(path: Path, value: Mapping[str, Any]) -> bytes:
    payload = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("utf-8") + b"\n"
    if path.exists():
        if path.is_symlink() or not path.is_file() or path.read_bytes() != payload:
            raise ContractValidationError(
                "recovery_immutable_conflict", str(path), "recovery bytes changed"
            )
        return payload
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    try:
        temporary.write_bytes(payload)
        temporary.replace(path)
    finally:
        if temporary.exists():
            temporary.unlink()
    return payload


def _write_projection(path: Path, value: Mapping[str, Any]) -> bytes:
    """Write a derived projection; journal/checkpoint records remain immutable."""

    payload = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("utf-8") + b"\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.projection.tmp")
    try:
        temporary.write_bytes(payload)
        temporary.replace(path)
    finally:
        if temporary.exists():
            temporary.unlink()
    return payload


def _load_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ContractValidationError(
            "recovery_json", str(path), "recovery record is not readable JSON"
        ) from exc
    return require_mapping(value, path=str(path))


def _relative_file(root: Path, relative: str) -> Path:
    if not relative or "\\" in relative or ":" in relative:
        raise ContractValidationError("recovery_path", relative, "invalid relative recovery path")
    candidate = (root / relative).resolve()
    resolved_root = root.resolve()
    if not candidate.is_relative_to(resolved_root):
        raise ContractValidationError("recovery_path", relative, "path escapes component root")
    return candidate


def _file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _hash_binding(value: Any, *, path: str) -> str:
    return require_sha256(value, path=path)


def _normalize_hash_map(value: Mapping[str, Any], *, path: str) -> dict[str, str]:
    row = require_mapping(value, path=path)
    require_exact_keys(row, required=set(_SEMANTIC_BINDING_KEYS), path=path)
    return {
        key: _hash_binding(row[key], path=f"{path}.{key}")
        for key in _SEMANTIC_BINDING_KEYS
    }


def build_evaluation_recovery_assignment_v1(
    *,
    workflow_run_id: str,
    component_run_id: str,
    input_set_sha256: str,
    settings_sha256: str,
    evaluation_profile_sha256: str,
    stage_plan_sha256: str,
    sampling_sha256: str,
    semantic_contract_sha256: str,
) -> dict[str, Any]:
    """Seal only semantic experiment identity, never a repository-wide state."""

    bindings = _normalize_hash_map(
        {
            "input_set_sha256": input_set_sha256,
            "settings_sha256": settings_sha256,
            "evaluation_profile_sha256": evaluation_profile_sha256,
            "stage_plan_sha256": stage_plan_sha256,
            "sampling_sha256": sampling_sha256,
            "semantic_contract_sha256": semantic_contract_sha256,
        },
        path="$.semantic_bindings",
    )
    draft = {
        "schema_id": ASSIGNMENT_SCHEMA_ID,
        "schema_version": SCHEMA_VERSION,
        "workflow_run_id": require_string(workflow_run_id, path="$.workflow_run_id"),
        "component_run_id": require_string(component_run_id, path="$.component_run_id"),
        "semantic_bindings": bindings,
        "integrity": {"assignment_sha256": _ZERO_HASH},
    }
    return _validate_assignment(
        seal_payload(draft, policy=_POLICY, hash_path=_ASSIGNMENT_HASH_PATH)
    )


def _validate_assignment(value: Mapping[str, Any]) -> dict[str, Any]:
    row = require_mapping(value, path="$.assignment")
    require_exact_keys(
        row,
        required={"schema_id", "schema_version", "workflow_run_id", "component_run_id", "semantic_bindings", "integrity"},
        path="$.assignment",
    )
    integrity = require_mapping(row["integrity"], path="$.assignment.integrity")
    require_exact_keys(integrity, required={"assignment_sha256"}, path="$.assignment.integrity")
    normalized = {
        "schema_id": require_enum(row["schema_id"], {ASSIGNMENT_SCHEMA_ID}, path="$.assignment.schema_id"),
        "schema_version": require_enum(row["schema_version"], {SCHEMA_VERSION}, path="$.assignment.schema_version"),
        "workflow_run_id": require_string(row["workflow_run_id"], path="$.assignment.workflow_run_id"),
        "component_run_id": require_string(row["component_run_id"], path="$.assignment.component_run_id"),
        "semantic_bindings": _normalize_hash_map(row["semantic_bindings"], path="$.assignment.semantic_bindings"),
        "integrity": {
            "assignment_sha256": require_sha256(
                integrity["assignment_sha256"], path="$.assignment.integrity.assignment_sha256"
            )
        },
    }
    if not verify_payload_hash(normalized, policy=_POLICY, hash_path=_ASSIGNMENT_HASH_PATH):
        raise ContractValidationError("recovery_assignment_hash", "$.assignment", "assignment hash drift")
    return normalized


def derive_evaluation_work_id_v1(descriptor: Mapping[str, Any]) -> str:
    normalized = _validate_work_descriptor(descriptor)
    return _derive_work_id_from_normalized(normalized)


def build_evaluation_work_descriptor_v1(
    *,
    stage_id: str,
    chapter_id: str | None,
    scorer_id: str,
    arm_ids: Sequence[str],
    presentation_id: str,
    orientation: str | None,
    input_bindings: Sequence[Mapping[str, Any]],
    evaluation_profile_sha256: str,
    prompt_sha256: str,
    schema_sha256: str,
    validator_sha256: str,
    model_id: str,
    provider_family: str,
    output_mode: str,
    logical_request_id: str,
) -> dict[str, Any]:
    draft = {
        "schema_id": WORK_SCHEMA_ID,
        "schema_version": SCHEMA_VERSION,
        "work_id": "",
        "stage_id": require_string(stage_id, path="$.stage_id"),
        "chapter_id": require_nullable_string(chapter_id, path="$.chapter_id"),
        "scorer_id": require_string(scorer_id, path="$.scorer_id"),
        "arm_ids": [require_string(item, path="$.arm_ids[*]") for item in arm_ids],
        "presentation_id": require_string(presentation_id, path="$.presentation_id"),
        "orientation": require_nullable_string(orientation, path="$.orientation"),
        "input_bindings": [copy.deepcopy(dict(item)) for item in input_bindings],
        "semantic_contract": {
            "evaluation_profile_sha256": _hash_binding(
                evaluation_profile_sha256, path="$.semantic_contract.evaluation_profile_sha256"
            ),
            "prompt_sha256": _hash_binding(prompt_sha256, path="$.semantic_contract.prompt_sha256"),
            "schema_sha256": _hash_binding(schema_sha256, path="$.semantic_contract.schema_sha256"),
            "validator_sha256": _hash_binding(
                validator_sha256, path="$.semantic_contract.validator_sha256"
            ),
            "model_id": require_string(model_id, path="$.semantic_contract.model_id"),
            "provider_family": require_string(
                provider_family, path="$.semantic_contract.provider_family"
            ),
            "output_mode": require_string(output_mode, path="$.semantic_contract.output_mode"),
        },
        "logical_request_id": require_string(logical_request_id, path="$.logical_request_id"),
        "integrity": {"work_sha256": _ZERO_HASH},
    }
    # The ID is derived without the self-hash and without the placeholder ID.
    draft["work_id"] = _derive_work_id_from_normalized(draft)
    return _validate_work_descriptor(seal_payload(draft, policy=_POLICY, hash_path=_WORK_HASH_PATH))


def _derive_work_id_from_normalized(value: Mapping[str, Any]) -> str:
    material = copy.deepcopy(dict(value))
    material.pop("work_id", None)
    material.pop("integrity", None)
    return "evalwork_" + canonical_sha256(material, policy=_POLICY)[:32]


def _validate_work_descriptor(value: Mapping[str, Any]) -> dict[str, Any]:
    row = require_mapping(value, path="$.work")
    required = {
        "schema_id",
        "schema_version",
        "work_id",
        "stage_id",
        "chapter_id",
        "scorer_id",
        "arm_ids",
        "presentation_id",
        "orientation",
        "input_bindings",
        "semantic_contract",
        "logical_request_id",
        "integrity",
    }
    require_exact_keys(row, required=required, path="$.work")
    contract = require_mapping(row["semantic_contract"], path="$.work.semantic_contract")
    require_exact_keys(
        contract,
        required={
            "evaluation_profile_sha256",
            "prompt_sha256",
            "schema_sha256",
            "validator_sha256",
            "model_id",
            "provider_family",
            "output_mode",
        },
        path="$.work.semantic_contract",
    )
    bindings = require_list(row["input_bindings"], path="$.work.input_bindings")
    integrity = require_mapping(row["integrity"], path="$.work.integrity")
    require_exact_keys(
        integrity, required={"work_sha256"}, path="$.work.integrity"
    )
    arms = [require_string(item, path="$.work.arm_ids[*]") for item in require_list(row["arm_ids"], path="$.work.arm_ids")]
    require_unique(arms, path="$.work.arm_ids")
    normalized = {
        "schema_id": require_enum(row["schema_id"], {WORK_SCHEMA_ID}, path="$.work.schema_id"),
        "schema_version": require_enum(row["schema_version"], {SCHEMA_VERSION}, path="$.work.schema_version"),
        "work_id": require_string(row["work_id"], path="$.work.work_id"),
        "stage_id": require_string(row["stage_id"], path="$.work.stage_id"),
        "chapter_id": require_nullable_string(row["chapter_id"], path="$.work.chapter_id"),
        "scorer_id": require_string(row["scorer_id"], path="$.work.scorer_id"),
        "arm_ids": arms,
        "presentation_id": require_string(row["presentation_id"], path="$.work.presentation_id"),
        "orientation": require_nullable_string(row["orientation"], path="$.work.orientation"),
        "input_bindings": [copy.deepcopy(dict(item)) for item in bindings],
        "semantic_contract": {
            key: (
                _hash_binding(contract[key], path=f"$.work.semantic_contract.{key}")
                if key.endswith("_sha256")
                else require_string(contract[key], path=f"$.work.semantic_contract.{key}")
            )
            for key in contract
        },
        "logical_request_id": require_string(row["logical_request_id"], path="$.work.logical_request_id"),
        "integrity": {
            "work_sha256": require_sha256(
                integrity["work_sha256"],
                path="$.work.integrity.work_sha256",
            )
        },
    }
    if normalized["work_id"] != _derive_work_id_from_normalized(normalized):
        raise ContractValidationError("work_id", "$.work.work_id", "work identity drift")
    if not verify_payload_hash(normalized, policy=_POLICY, hash_path=_WORK_HASH_PATH):
        raise ContractValidationError("work_hash", "$.work.integrity.work_sha256", "work hash drift")
    return normalized


def classify_evaluation_failure_v1(
    error: BaseException,
    *,
    category_hint: str | None = None,
) -> EvaluationFailureClassificationV1:
    """Classify only the user-facing behavior; never hides integrity errors."""

    if category_hint is not None:
        category = require_enum(category_hint, _FAILURE_CATEGORIES, path="$.category_hint")
    elif isinstance(error, ContractValidationError):
        code = getattr(error, "code", "")
        integrity_tokens = (
            "artifact",
            "attempt",
            "binding",
            "checkpoint",
            "component_sequence",
            "conflict",
            "duplicate",
            "event_hash",
            "foreign",
            "hash",
            "integrity",
            "lineage",
            "manifest",
            "projection",
            "recovery_",
            "tamper",
        )
        if code in {
            "input_set_hash",
            "settings_binding",
            "stage_binding",
            "profile_binding",
            "manifest_lineage",
            "event_hash",
            "checkpoint_hash",
            "artifact_conflict",
            "component_binding",
            "component_attempt",
            "component_sequence",
        } or any(token in code.casefold() for token in integrity_tokens):
            category = "integrity"
        elif code in {
            "response_json",
            "response_contract",
            "semantic_validation",
            "score_range",
            "coverage",
            "validation",
        }:
            category = "semantic"
        else:
            category = "operational"
    else:
        name = type(error).__name__.casefold()
        code = str(getattr(error, "code", "")).casefold()
        if any(token in name or token in code for token in ("timeout", "transport", "connection", "rate", "http_408", "http_429")):
            category = "transport"
        else:
            category = "operational"
    reason = str(getattr(error, "code", "") or type(error).__name__).strip()
    reason = re.sub(r"[^A-Za-z0-9_.-]+", "_", reason)[:80] or "evaluation_failure"
    return EvaluationFailureClassificationV1(
        category=category,
        reason_code=reason,
        resume_available=category in {"transport", "semantic", "operational", "user"},
    )


def _safe_message(error: BaseException) -> str:
    raw = str(error).replace("\r", " ").replace("\n", " ")
    raw = re.sub(r"(?i)(api[_ -]?key|token|secret|password|authorization)\s*[:=]\s*\S+", r"\1=<redacted>", raw)
    raw = re.sub(r"(?i)([A-Za-z]:\\|/)[^\s]+", "<path>", raw)
    return raw[:240]


def _incident_id(*, category: str, reason_code: str, work_id: str | None, message_hash: str) -> str:
    material = {
        "category": category,
        "reason_code": reason_code,
        "work_id": work_id,
        "message_hash": message_hash,
    }
    return "inc_" + canonical_sha256(material, policy=_POLICY)[:24]


def _validate_journal_record(value: Mapping[str, Any], *, previous_hash: str | None) -> dict[str, Any]:
    row = require_mapping(value, path="$.journal")
    required = {
        "schema_id",
        "schema_version",
        "sequence",
        "event",
        "workflow_run_id",
        "component_run_id",
        "component_attempt_id",
        "component_attempt_index",
        "work_id",
        "logical_request_id",
        "physical_attempt_id",
        "payload",
        "previous_journal_sha256",
        "created_at",
        "integrity",
    }
    require_exact_keys(row, required=required, path="$.journal")
    integrity = require_mapping(row["integrity"], path="$.journal.integrity")
    require_exact_keys(integrity, required={"journal_sha256"}, path="$.journal.integrity")
    normalized = {
        "schema_id": require_enum(row["schema_id"], {JOURNAL_SCHEMA_ID}, path="$.journal.schema_id"),
        "schema_version": require_enum(row["schema_version"], {SCHEMA_VERSION}, path="$.journal.schema_version"),
        "sequence": require_int(row["sequence"], path="$.journal.sequence", minimum=1),
        "event": require_enum(row["event"], _JOURNAL_EVENTS, path="$.journal.event"),
        "workflow_run_id": require_string(row["workflow_run_id"], path="$.journal.workflow_run_id"),
        "component_run_id": require_string(row["component_run_id"], path="$.journal.component_run_id"),
        "component_attempt_id": require_string(row["component_attempt_id"], path="$.journal.component_attempt_id"),
        "component_attempt_index": require_int(row["component_attempt_index"], path="$.journal.component_attempt_index", minimum=1),
        "work_id": require_nullable_string(row["work_id"], path="$.journal.work_id"),
        "logical_request_id": require_nullable_string(row["logical_request_id"], path="$.journal.logical_request_id"),
        "physical_attempt_id": require_nullable_string(row["physical_attempt_id"], path="$.journal.physical_attempt_id"),
        "payload": copy.deepcopy(dict(require_mapping(row["payload"], path="$.journal.payload"))),
        "previous_journal_sha256": (
            None if row["previous_journal_sha256"] is None else require_sha256(row["previous_journal_sha256"], path="$.journal.previous_journal_sha256")
        ),
        "created_at": require_rfc3339(row["created_at"], path="$.journal.created_at"),
        "integrity": {
            "journal_sha256": require_sha256(integrity["journal_sha256"], path="$.journal.integrity.journal_sha256")
        },
    }
    if normalized["previous_journal_sha256"] != previous_hash:
        raise ContractValidationError("recovery_journal_chain", "$.journal.previous_journal_sha256", "journal chain drift")
    if not verify_payload_hash(normalized, policy=_POLICY, hash_path=_JOURNAL_HASH_PATH):
        raise ContractValidationError("recovery_journal_hash", "$.journal.integrity.journal_sha256", "journal hash drift")
    return normalized


def _validate_ledger(value: Mapping[str, Any], *, assignment: Mapping[str, Any]) -> dict[str, Any]:
    row = require_mapping(value, path="$.ledger")
    required = {
        "schema_id",
        "schema_version",
        "workflow_run_id",
        "component_run_id",
        "journal_sequence",
        "works",
        "accepted_work_ids",
        "pending_work_ids",
        "halted_work_ids",
        "superseded_work_ids",
        "integrity",
    }
    require_exact_keys(row, required=required, path="$.ledger")
    integrity = require_mapping(row["integrity"], path="$.ledger.integrity")
    require_exact_keys(integrity, required={"ledger_sha256"}, path="$.ledger.integrity")
    works = require_list(row["works"], path="$.ledger.works")
    normalized_works: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, item in enumerate(works):
        work = require_mapping(item, path=f"$.ledger.works[{index}]")
        require_exact_keys(
            work,
            required={
                "work_id",
                "stage_id",
                "descriptor_sha256",
                "state",
                "logical_request_id",
                "physical_attempt_ids",
                "accepted_artifact",
                "checkpoint_ref",
                "failure_category",
                "incident_id",
            },
            path=f"$.ledger.works[{index}]",
        )
        work_id = require_string(work["work_id"], path=f"$.ledger.works[{index}].work_id")
        if work_id in seen:
            raise ContractValidationError("recovery_duplicate_work", "$.ledger.works", "work ID repeated")
        seen.add(work_id)
        normalized_works.append(
            {
                "work_id": work_id,
                "stage_id": require_string(work["stage_id"], path=f"$.ledger.works[{index}].stage_id"),
                "descriptor_sha256": require_sha256(work["descriptor_sha256"], path=f"$.ledger.works[{index}].descriptor_sha256"),
                "state": require_enum(work["state"], _WORK_STATES, path=f"$.ledger.works[{index}].state"),
                "logical_request_id": require_string(work["logical_request_id"], path=f"$.ledger.works[{index}].logical_request_id"),
                "physical_attempt_ids": [
                    require_string(item, path=f"$.ledger.works[{index}].physical_attempt_ids[*]")
                    for item in require_list(work["physical_attempt_ids"], path=f"$.ledger.works[{index}].physical_attempt_ids")
                ],
                "accepted_artifact": None if work["accepted_artifact"] is None else copy.deepcopy(dict(require_mapping(work["accepted_artifact"], path=f"$.ledger.works[{index}].accepted_artifact"))),
                "checkpoint_ref": require_nullable_string(work["checkpoint_ref"], path=f"$.ledger.works[{index}].checkpoint_ref"),
                "failure_category": require_nullable_string(work["failure_category"], path=f"$.ledger.works[{index}].failure_category"),
                "incident_id": require_nullable_string(work["incident_id"], path=f"$.ledger.works[{index}].incident_id"),
            }
        )
    def _ids(key: str) -> list[str]:
        values = [require_string(item, path=f"$.ledger.{key}[*]") for item in require_list(row[key], path=f"$.ledger.{key}")]
        require_unique(values, path=f"$.ledger.{key}")
        return values
    normalized = {
        "schema_id": require_enum(row["schema_id"], {LEDGER_SCHEMA_ID}, path="$.ledger.schema_id"),
        "schema_version": require_enum(row["schema_version"], {SCHEMA_VERSION}, path="$.ledger.schema_version"),
        "workflow_run_id": require_string(row["workflow_run_id"], path="$.ledger.workflow_run_id"),
        "component_run_id": require_string(row["component_run_id"], path="$.ledger.component_run_id"),
        "journal_sequence": require_int(row["journal_sequence"], path="$.ledger.journal_sequence", minimum=0),
        "works": normalized_works,
        "accepted_work_ids": _ids("accepted_work_ids"),
        "pending_work_ids": _ids("pending_work_ids"),
        "halted_work_ids": _ids("halted_work_ids"),
        "superseded_work_ids": _ids("superseded_work_ids"),
        "integrity": {
            "ledger_sha256": require_sha256(integrity["ledger_sha256"], path="$.ledger.integrity.ledger_sha256")
        },
    }
    if normalized["workflow_run_id"] != assignment["workflow_run_id"] or normalized["component_run_id"] != assignment["component_run_id"]:
        raise ContractValidationError("recovery_ledger_binding", "$.ledger", "ledger belongs to another component")
    if set(normalized["accepted_work_ids"]) & set(normalized["pending_work_ids"] + normalized["halted_work_ids"] + normalized["superseded_work_ids"]):
        raise ContractValidationError("recovery_ledger_state", "$.ledger", "work appears in multiple terminal buckets")
    if not verify_payload_hash(normalized, policy=_POLICY, hash_path=_LEDGER_HASH_PATH):
        raise ContractValidationError("recovery_ledger_hash", "$.ledger.integrity.ledger_sha256", "ledger hash drift")
    return normalized


def _validate_checkpoint(value: Mapping[str, Any], *, assignment: Mapping[str, Any], previous_hash: str | None) -> dict[str, Any]:
    row = require_mapping(value, path="$.checkpoint")
    required = {
        "schema_id",
        "schema_version",
        "workflow_run_id",
        "component_run_id",
        "component_attempt_id",
        "component_attempt_index",
        "component_seq",
        "semantic_bindings",
        "accepted_work_ids",
        "pending_work_ids",
        "halted_work_ids",
        "superseded_work_ids",
        "artifact_index_sha256",
        "usage_snapshot_sha256",
        "previous_checkpoint_sha256",
        "created_at",
        "integrity",
    }
    require_exact_keys(row, required=required, path="$.checkpoint")
    integrity = require_mapping(row["integrity"], path="$.checkpoint.integrity")
    require_exact_keys(integrity, required={"checkpoint_sha256"}, path="$.checkpoint.integrity")
    bindings = _normalize_hash_map(row["semantic_bindings"], path="$.checkpoint.semantic_bindings")
    expected = assignment["semantic_bindings"]
    if bindings != expected:
        raise ContractValidationError("recovery_checkpoint_binding", "$.checkpoint.semantic_bindings", "checkpoint semantic bindings drift")
    normalized = {
        "schema_id": require_enum(row["schema_id"], {CHECKPOINT_SCHEMA_ID}, path="$.checkpoint.schema_id"),
        "schema_version": require_enum(row["schema_version"], {SCHEMA_VERSION}, path="$.checkpoint.schema_version"),
        "workflow_run_id": require_string(row["workflow_run_id"], path="$.checkpoint.workflow_run_id"),
        "component_run_id": require_string(row["component_run_id"], path="$.checkpoint.component_run_id"),
        "component_attempt_id": require_string(row["component_attempt_id"], path="$.checkpoint.component_attempt_id"),
        "component_attempt_index": require_int(row["component_attempt_index"], path="$.checkpoint.component_attempt_index", minimum=1),
        "component_seq": require_int(row["component_seq"], path="$.checkpoint.component_seq", minimum=0),
        "semantic_bindings": bindings,
        "accepted_work_ids": [require_string(item, path="$.checkpoint.accepted_work_ids[*]") for item in require_list(row["accepted_work_ids"], path="$.checkpoint.accepted_work_ids")],
        "pending_work_ids": [require_string(item, path="$.checkpoint.pending_work_ids[*]") for item in require_list(row["pending_work_ids"], path="$.checkpoint.pending_work_ids")],
        "halted_work_ids": [require_string(item, path="$.checkpoint.halted_work_ids[*]") for item in require_list(row["halted_work_ids"], path="$.checkpoint.halted_work_ids")],
        "superseded_work_ids": [require_string(item, path="$.checkpoint.superseded_work_ids[*]") for item in require_list(row["superseded_work_ids"], path="$.checkpoint.superseded_work_ids")],
        "artifact_index_sha256": require_sha256(row["artifact_index_sha256"], path="$.checkpoint.artifact_index_sha256"),
        "usage_snapshot_sha256": None if row["usage_snapshot_sha256"] is None else require_sha256(row["usage_snapshot_sha256"], path="$.checkpoint.usage_snapshot_sha256"),
        "previous_checkpoint_sha256": None if row["previous_checkpoint_sha256"] is None else require_sha256(row["previous_checkpoint_sha256"], path="$.checkpoint.previous_checkpoint_sha256"),
        "created_at": require_rfc3339(row["created_at"], path="$.checkpoint.created_at"),
        "integrity": {
            "checkpoint_sha256": require_sha256(integrity["checkpoint_sha256"], path="$.checkpoint.integrity.checkpoint_sha256")
        },
    }
    if normalized["previous_checkpoint_sha256"] != previous_hash:
        raise ContractValidationError("recovery_checkpoint_chain", "$.checkpoint.previous_checkpoint_sha256", "checkpoint chain drift")
    if (
        normalized["workflow_run_id"] != assignment["workflow_run_id"]
        or normalized["component_run_id"] != assignment["component_run_id"]
    ):
        raise ContractValidationError(
            "recovery_checkpoint_binding",
            "$.checkpoint",
            "checkpoint belongs to another component",
        )
    if not verify_payload_hash(normalized, policy=_POLICY, hash_path=_CHECKPOINT_HASH_PATH):
        raise ContractValidationError("recovery_checkpoint_hash", "$.checkpoint.integrity.checkpoint_sha256", "checkpoint hash drift")
    return normalized


def _validate_journal_bindings_v1(
    records: Sequence[Mapping[str, Any]], *, assignment: Mapping[str, Any]
) -> None:
    attempt_ids: dict[int, str] = {}
    attempt_indexes: dict[str, int] = {}
    works: dict[str, str] = {}
    physical_attempts: dict[str, str] = {}
    previous_attempt_index: int | None = None
    for record in records:
        if (
            record["workflow_run_id"] != assignment["workflow_run_id"]
            or record["component_run_id"] != assignment["component_run_id"]
        ):
            raise ContractValidationError(
                "recovery_journal_binding",
                "$.journal",
                "journal record belongs to another component",
            )
        attempt_index = record["component_attempt_index"]
        attempt_id = record["component_attempt_id"]
        if previous_attempt_index is not None and not (
            previous_attempt_index <= attempt_index <= previous_attempt_index + 1
        ):
            raise ContractValidationError(
                "recovery_attempt_sequence",
                "$.journal.component_attempt_index",
                "component attempt sequence regressed or skipped",
            )
        if attempt_ids.setdefault(attempt_index, attempt_id) != attempt_id:
            raise ContractValidationError(
                "recovery_attempt_binding",
                "$.journal.component_attempt_id",
                "attempt index was reused with another ID",
            )
        if attempt_indexes.setdefault(attempt_id, attempt_index) != attempt_index:
            raise ContractValidationError(
                "recovery_attempt_binding",
                "$.journal.component_attempt_index",
                "attempt ID was reused with another index",
            )
        previous_attempt_index = attempt_index

        work_id = record["work_id"]
        if record["event"] == "work_started":
            descriptor = _validate_work_descriptor(record["payload"]["descriptor"])
            if work_id in works:
                raise ContractValidationError(
                    "recovery_duplicate_work",
                    "$.journal.work_id",
                    "work_started was repeated",
                )
            if (
                descriptor["work_id"] != work_id
                or descriptor["logical_request_id"] != record["logical_request_id"]
            ):
                raise ContractValidationError(
                    "recovery_work_binding",
                    "$.journal",
                    "work record differs from its descriptor",
                )
            works[work_id] = descriptor["logical_request_id"]
        elif work_id is not None:
            if work_id not in works:
                raise ContractValidationError(
                    "recovery_work_order",
                    "$.journal.work_id",
                    "work event precedes work_started",
                )
            if record["logical_request_id"] != works[work_id]:
                raise ContractValidationError(
                    "recovery_work_binding",
                    "$.journal.logical_request_id",
                    "logical request differs from work descriptor",
                )

        physical_attempt_id = record["physical_attempt_id"]
        if record["event"] == "physical_attempt_sealed":
            if physical_attempt_id is None or work_id is None:
                raise ContractValidationError(
                    "recovery_physical_attempt",
                    "$.journal.physical_attempt_id",
                    "physical seal requires a work and attempt ID",
                )
            existing_work_id = physical_attempts.setdefault(
                physical_attempt_id, work_id
            )
            if existing_work_id != work_id:
                raise ContractValidationError(
                    "recovery_physical_attempt",
                    "$.journal.physical_attempt_id",
                    "physical attempt ID was reused across work",
                )
        elif physical_attempt_id is not None:
            if physical_attempts.get(physical_attempt_id) != work_id:
                raise ContractValidationError(
                    "recovery_physical_attempt",
                    "$.journal.physical_attempt_id",
                    "boundary refers to an unsealed or foreign physical attempt",
                )


def _validate_checkpoint_journal_bindings_v1(
    checkpoints: Sequence[Mapping[str, Any]],
    records: Sequence[Mapping[str, Any]],
    *,
    assignment: Mapping[str, Any],
) -> None:
    positions: list[int] = []
    for checkpoint in checkpoints:
        checkpoint_hash = checkpoint["integrity"]["checkpoint_sha256"]
        expected_ref = f"recovery/checkpoints/{checkpoint_hash}.json"
        matches = [
            (index, record)
            for index, record in enumerate(records)
            if record["event"] == "checkpoint_recorded"
            and record["payload"].get("checkpoint_sha256") == checkpoint_hash
            and record["payload"].get("checkpoint_ref") == expected_ref
        ]
        if len(matches) != 1:
            raise ContractValidationError(
                "recovery_checkpoint_binding",
                "$.checkpoint",
                "checkpoint must have one exact journal binding",
            )
        position, record = matches[0]
        if positions and position <= positions[-1]:
            raise ContractValidationError(
                "recovery_checkpoint_sequence",
                "$.checkpoint",
                "checkpoint journal order differs from checkpoint chain",
            )
        positions.append(position)
        if (
            checkpoint["component_attempt_id"] != record["component_attempt_id"]
            or checkpoint["component_attempt_index"]
            != record["component_attempt_index"]
        ):
            raise ContractValidationError(
                "recovery_checkpoint_binding",
                "$.checkpoint.component_attempt_id",
                "checkpoint attempt differs from its journal record",
            )
        projection = _build_ledger_projection_v1(
            records[:position], assignment=assignment
        )
        for key in (
            "accepted_work_ids",
            "pending_work_ids",
            "halted_work_ids",
            "superseded_work_ids",
        ):
            if checkpoint[key] != projection[key]:
                raise ContractValidationError(
                    "recovery_checkpoint_state",
                    f"$.checkpoint.{key}",
                    "checkpoint state differs from the accepted journal",
                )


def _load_ordered_checkpoints(
    checkpoint_root: Path, *, assignment: Mapping[str, Any]
) -> list[dict[str, Any]]:
    """Reconstruct the checkpoint chain from links, never filename order."""

    paths = sorted(checkpoint_root.glob("*.json"))
    if not paths:
        return []
    raw_rows = [_load_json(path) for path in paths]
    by_hash: dict[str, tuple[Path, dict[str, Any]]] = {}
    for path, row in zip(paths, raw_rows):
        integrity = require_mapping(row.get("integrity"), path=f"{path}.integrity")
        checkpoint_hash = require_sha256(
            integrity.get("checkpoint_sha256"),
            path=f"{path}.integrity.checkpoint_sha256",
        )
        if path.stem != checkpoint_hash or checkpoint_hash in by_hash:
            raise ContractValidationError(
                "recovery_checkpoint_ref",
                str(path),
                "checkpoint filename or identity is duplicated",
            )
        by_hash[checkpoint_hash] = (path, row)
    roots = [
        item
        for item in by_hash.values()
        if item[1].get("previous_checkpoint_sha256") is None
    ]
    if len(roots) != 1:
        raise ContractValidationError(
            "recovery_checkpoint_chain",
            str(checkpoint_root),
            "checkpoint chain must have exactly one root",
        )
    ordered: list[dict[str, Any]] = []
    previous_hash: str | None = None
    remaining = set(by_hash)
    while remaining:
        candidates = [
            (checkpoint_hash, path, row)
            for checkpoint_hash, (path, row) in by_hash.items()
            if checkpoint_hash in remaining
            and row.get("previous_checkpoint_sha256") == previous_hash
        ]
        if len(candidates) != 1:
            raise ContractValidationError(
                "recovery_checkpoint_chain",
                str(checkpoint_root),
                "checkpoint chain is branched, gapped, or cyclic",
            )
        checkpoint_hash, path, row = candidates[0]
        normalized = _validate_checkpoint(
            row, assignment=assignment, previous_hash=previous_hash
        )
        ordered.append(normalized)
        remaining.remove(checkpoint_hash)
        previous_hash = checkpoint_hash
    return ordered


def _build_ledger_projection_v1(
    records: Sequence[Mapping[str, Any]], *, assignment: Mapping[str, Any]
) -> dict[str, Any]:
    works: dict[str, dict[str, Any]] = {}
    for record in records:
        work_id = record["work_id"]
        if work_id is None:
            continue
        if record["event"] == "work_started":
            descriptor = _validate_work_descriptor(record["payload"]["descriptor"])
            works.setdefault(
                work_id,
                {
                    "work_id": work_id,
                    "stage_id": descriptor["stage_id"],
                    "descriptor_sha256": descriptor["integrity"]["work_sha256"],
                    "state": "in_progress",
                    "logical_request_id": descriptor["logical_request_id"],
                    "physical_attempt_ids": [],
                    "accepted_artifact": None,
                    "checkpoint_ref": None,
                    "failure_category": None,
                    "incident_id": None,
                },
            )
        elif work_id not in works:
            raise ContractValidationError(
                "recovery_work_order",
                "$.journal",
                "work event precedes work_started",
            )
        if (
            record["event"] == "physical_attempt_sealed"
            and record["physical_attempt_id"]
            not in works[work_id]["physical_attempt_ids"]
        ):
            works[work_id]["physical_attempt_ids"].append(
                record["physical_attempt_id"]
            )
        elif record["event"] == "artifact_recorded":
            # Boundary telemetry may use this event without carrying the
            # accepted artifact binding. Only typed artifact payloads project.
            if "artifact" in record["payload"]:
                works[work_id]["accepted_artifact"] = copy.deepcopy(
                    dict(
                        require_mapping(
                            record["payload"]["artifact"],
                            path="$.journal.payload.artifact",
                        )
                    )
                )
        elif record["event"] == "work_accepted":
            artifact = require_mapping(
                record["payload"].get("artifact"),
                path="$.journal.payload.artifact",
            )
            works[work_id]["state"] = "accepted"
            works[work_id]["accepted_artifact"] = copy.deepcopy(dict(artifact))
        elif record["event"] == "work_halted":
            works[work_id]["state"] = "halted"
            works[work_id]["failure_category"] = record["payload"]["category"]
            works[work_id]["incident_id"] = record["payload"].get("incident_id")
        elif record["event"] == "work_retryable_rejected":
            works[work_id]["state"] = "retryable_rejected"
        elif record["event"] == "work_superseded":
            works[work_id]["state"] = "superseded"
        elif record["event"] == "checkpoint_recorded":
            works[work_id]["checkpoint_ref"] = record["payload"].get(
                "checkpoint_ref"
            )
    ordered = list(works.values())
    draft = {
        "schema_id": LEDGER_SCHEMA_ID,
        "schema_version": SCHEMA_VERSION,
        "workflow_run_id": assignment["workflow_run_id"],
        "component_run_id": assignment["component_run_id"],
        "journal_sequence": len(records),
        "works": ordered,
        "accepted_work_ids": [
            row["work_id"] for row in ordered if row["state"] == "accepted"
        ],
        "pending_work_ids": [
            row["work_id"] for row in ordered if row["state"] == "in_progress"
        ],
        "halted_work_ids": [
            row["work_id"]
            for row in ordered
            if row["state"] in {"halted", "retryable_rejected"}
        ],
        "superseded_work_ids": [
            row["work_id"] for row in ordered if row["state"] == "superseded"
        ],
        "integrity": {"ledger_sha256": _ZERO_HASH},
    }
    return _validate_ledger(
        seal_payload(draft, policy=_POLICY, hash_path=_LEDGER_HASH_PATH),
        assignment=assignment,
    )


def validate_evaluation_recovery_package_v1(
    root: Path,
    *,
    assignment: Mapping[str, Any],
    require_checkpoint: bool = False,
) -> dict[str, Any]:
    """Validate only the component's recovery subtree; never repository files."""

    recovery_root = root.resolve()
    accepted_assignment = _validate_assignment(assignment)
    persisted_assignment = _validate_assignment(_load_json(recovery_root / "assignment.json"))
    if persisted_assignment != accepted_assignment:
        raise ContractValidationError("recovery_assignment_binding", str(recovery_root), "semantic assignment changed")
    journal_root = recovery_root / "journal_records"
    records: list[dict[str, Any]] = []
    previous: str | None = None
    for index, path in enumerate(sorted(journal_root.glob("*.json")), start=1):
        record = _validate_journal_record(_load_json(path), previous_hash=previous)
        if record["sequence"] != index:
            raise ContractValidationError("recovery_journal_sequence", str(path), "journal sequence gap")
        records.append(record)
        previous = record["integrity"]["journal_sha256"]
    _validate_journal_bindings_v1(records, assignment=accepted_assignment)
    ledger_path = recovery_root / "work_ledger.json"
    if ledger_path.is_file():
        ledger = _validate_ledger(_load_json(ledger_path), assignment=accepted_assignment)
        expected_ledger = _build_ledger_projection_v1(
            records, assignment=accepted_assignment
        )
        if ledger != expected_ledger:
            raise ContractValidationError(
                "recovery_ledger_projection",
                str(ledger_path),
                "ledger differs from the accepted journal",
            )
    else:
        ledger = None
    checkpoint_root = recovery_root / "checkpoints"
    checkpoints = _load_ordered_checkpoints(
        checkpoint_root, assignment=accepted_assignment
    )
    _validate_checkpoint_journal_bindings_v1(
        checkpoints, records, assignment=accepted_assignment
    )
    if require_checkpoint and not checkpoints:
        raise ContractValidationError("recovery_checkpoint_missing", str(recovery_root), "resume requires a checkpoint")
    return {
        "assignment": accepted_assignment,
        "journal": tuple(copy.deepcopy(records)),
        "ledger": None if ledger is None else copy.deepcopy(ledger),
        "checkpoints": tuple(copy.deepcopy(checkpoints)),
    }


class EvaluationWorkflowRecoveryStoreV1:
    """Durable recovery state scoped to one Evaluation component root."""

    def __init__(
        self,
        root: Path,
        *,
        assignment: Mapping[str, Any],
        generated_at: str,
        producer_code_commit: str,
        failure_injector: Callable[[str], None] | None = None,
    ) -> None:
        self.component_root = root.resolve()
        self.recovery_root = self.component_root / "recovery"
        self.generated_at = require_rfc3339(generated_at, path="$.generated_at")
        self.producer_code_commit = require_string(
            producer_code_commit, path="$.producer_code_commit"
        )
        self.assignment = _validate_assignment(assignment)
        self.failure_injector = failure_injector
        self.recovery_root.mkdir(parents=True, exist_ok=True)
        self.journal_root = self.recovery_root / "journal_records"
        self.checkpoint_root = self.recovery_root / "checkpoints"
        self.incident_root = self.recovery_root / "diagnostics"
        self.journal_root.mkdir(parents=True, exist_ok=True)
        self.checkpoint_root.mkdir(parents=True, exist_ok=True)
        self.incident_root.mkdir(parents=True, exist_ok=True)
        assignment_path = self.recovery_root / "assignment.json"
        _write_json(assignment_path, self.assignment)
        self._records: list[dict[str, Any]] = []
        self._ledger: dict[str, Any] = self._empty_ledger()
        self._checkpoints: list[dict[str, Any]] = []
        self._load()

    @property
    def latest_checkpoint(self) -> dict[str, Any] | None:
        return None if not self._checkpoints else copy.deepcopy(self._checkpoints[-1])

    @property
    def ledger(self) -> dict[str, Any]:
        return copy.deepcopy(self._ledger)

    @property
    def records(self) -> tuple[dict[str, Any], ...]:
        return tuple(copy.deepcopy(self._records))

    def validate(self, *, require_checkpoint: bool = False) -> dict[str, Any]:
        return validate_evaluation_recovery_package_v1(
            self.recovery_root, assignment=self.assignment, require_checkpoint=require_checkpoint
        )

    def begin_work(self, descriptor: Mapping[str, Any], *, component_attempt_id: str, component_attempt_index: int) -> str:
        work = _validate_work_descriptor(descriptor)
        existing = self._work(work["work_id"])
        if existing is not None:
            if existing["descriptor_sha256"] != work["integrity"]["work_sha256"]:
                raise ContractValidationError("recovery_work_binding", work["work_id"], "work descriptor changed")
            return work["work_id"]
        self._append(
            "work_started",
            component_attempt_id=component_attempt_id,
            component_attempt_index=component_attempt_index,
            work_id=work["work_id"],
            logical_request_id=work["logical_request_id"],
            payload={"descriptor": work, "descriptor_sha256": work["integrity"]["work_sha256"]},
            boundary="intent",
        )
        return work["work_id"]

    def record_physical_attempt(
        self,
        *,
        work_id: str,
        physical_attempt_id: str,
        component_attempt_id: str,
        component_attempt_index: int,
        seal_binding: Mapping[str, Any],
    ) -> None:
        self._require_work(work_id)
        existing = self._physical_record(physical_attempt_id)
        payload = {"seal_binding": copy.deepcopy(dict(seal_binding))}
        if existing is not None:
            if existing["payload"] != payload or existing["work_id"] != work_id:
                raise ContractValidationError("recovery_physical_attempt", physical_attempt_id, "physical attempt identity reused with different bytes")
            return
        self._append(
            "physical_attempt_sealed",
            component_attempt_id=component_attempt_id,
            component_attempt_index=component_attempt_index,
            work_id=work_id,
            logical_request_id=self._work(work_id)["logical_request_id"],
            physical_attempt_id=physical_attempt_id,
            payload=payload,
            boundary="physical_seal",
        )

    def record_boundary(
        self,
        *,
        event: str,
        work_id: str,
        component_attempt_id: str,
        component_attempt_index: int,
        payload: Mapping[str, Any],
        physical_attempt_id: str | None = None,
        boundary: str | None = None,
    ) -> None:
        event_name = require_enum(event, _JOURNAL_EVENTS, path="$.event")
        self._require_work(work_id)
        if physical_attempt_id is not None and self._physical_record(physical_attempt_id) is None:
            raise ContractValidationError("recovery_physical_attempt", physical_attempt_id, "boundary has no sealed physical attempt")
        key = (event_name, work_id, physical_attempt_id, canonical_sha256(dict(payload), policy=_POLICY))
        if any(
            (row["event"], row["work_id"], row["physical_attempt_id"], canonical_sha256(row["payload"], policy=_POLICY)) == key
            for row in self._records
        ):
            return
        self._append(
            event_name,
            component_attempt_id=component_attempt_id,
            component_attempt_index=component_attempt_index,
            work_id=work_id,
            logical_request_id=self._work(work_id)["logical_request_id"],
            physical_attempt_id=physical_attempt_id,
            payload=payload,
            boundary=boundary,
        )

    def accept_work(
        self,
        *,
        work_id: str,
        component_attempt_id: str,
        component_attempt_index: int,
        artifact_binding: Mapping[str, Any],
        physical_attempt_id: str | None = None,
    ) -> None:
        work = self._require_work(work_id)
        if work["state"] == "accepted":
            if work["accepted_artifact"] != dict(artifact_binding):
                raise ContractValidationError("recovery_accept_conflict", work_id, "accepted artifact changed")
            return
        self._append(
            "artifact_recorded",
            component_attempt_id=component_attempt_id,
            component_attempt_index=component_attempt_index,
            work_id=work_id,
            logical_request_id=work["logical_request_id"],
            physical_attempt_id=physical_attempt_id,
            payload={"artifact": copy.deepcopy(dict(artifact_binding))},
            boundary="artifact",
        )
        self._append(
            "work_accepted",
            component_attempt_id=component_attempt_id,
            component_attempt_index=component_attempt_index,
            work_id=work_id,
            logical_request_id=work["logical_request_id"],
            physical_attempt_id=physical_attempt_id,
            payload={"artifact": copy.deepcopy(dict(artifact_binding))},
            boundary="accepted",
        )

    def mark_halted(
        self,
        *,
        work_id: str | None,
        component_attempt_id: str,
        component_attempt_index: int,
        category: str,
        incident_id: str | None,
        reason_code: str,
    ) -> None:
        category = require_enum(category, _FAILURE_CATEGORIES, path="$.category")
        if work_id is not None:
            self._require_work(work_id)
        self._append(
            "work_halted",
            component_attempt_id=component_attempt_id,
            component_attempt_index=component_attempt_index,
            work_id=work_id,
            logical_request_id=None if work_id is None else self._work(work_id)["logical_request_id"],
            payload={"category": category, "incident_id": incident_id, "reason_code": reason_code},
            boundary=None,
        )

    def record_incident(
        self,
        *,
        error: BaseException,
        category: str,
        reason_code: str,
        component_attempt_id: str,
        component_attempt_index: int,
        stage_id: str | None,
        work_id: str | None,
    ) -> str:
        category = require_enum(category, _FAILURE_CATEGORIES, path="$.category")
        message = _safe_message(error)
        message_hash = hashlib.sha256(message.encode("utf-8")).hexdigest()
        incident_id = _incident_id(
            category=category,
            reason_code=reason_code,
            work_id=work_id,
            message_hash=message_hash,
        )
        incident = {
            "schema_id": INCIDENT_SCHEMA_ID,
            "schema_version": SCHEMA_VERSION,
            "incident_id": incident_id,
            "workflow_run_id": self.assignment["workflow_run_id"],
            "component_run_id": self.assignment["component_run_id"],
            "component_attempt_id": component_attempt_id,
            "component_attempt_index": component_attempt_index,
            "category": category,
            "reason_code": reason_code,
            "stage_id": stage_id,
            "work_id": work_id,
            "exception_class": type(error).__name__[:120],
            "safe_message": message,
            "stack_trace_sha256": hashlib.sha256(
                "".join(traceback.format_exception(type(error), error, error.__traceback__)).encode("utf-8")
            ).hexdigest(),
            "producer_code_commit": self.producer_code_commit,
            "created_at": self.generated_at,
            "integrity": {"incident_sha256": _ZERO_HASH},
        }
        sealed = _validate_incident(
            seal_payload(incident, policy=_POLICY, hash_path=_INCIDENT_HASH_PATH)
        )
        _write_json(self.incident_root / f"{incident_id}.json", sealed)
        self._append(
            "incident_recorded",
            component_attempt_id=component_attempt_id,
            component_attempt_index=component_attempt_index,
            work_id=work_id,
            logical_request_id=None if work_id is None else self._work(work_id)["logical_request_id"],
            payload={"incident_id": incident_id, "category": category, "reason_code": reason_code},
            boundary=None,
        )
        return incident_id

    def write_checkpoint(
        self,
        *,
        component_attempt_id: str,
        component_attempt_index: int,
        component_seq: int,
        artifact_index_sha256: str,
        usage_snapshot_sha256: str | None,
        accepted_work_ids: Sequence[str] | None = None,
        pending_work_ids: Sequence[str] | None = None,
        halted_work_ids: Sequence[str] | None = None,
        superseded_work_ids: Sequence[str] | None = None,
    ) -> dict[str, Any]:
        accepted = list(self._ledger["accepted_work_ids"] if accepted_work_ids is None else accepted_work_ids)
        pending = list(self._ledger["pending_work_ids"] if pending_work_ids is None else pending_work_ids)
        halted = list(self._ledger["halted_work_ids"] if halted_work_ids is None else halted_work_ids)
        superseded = list(self._ledger["superseded_work_ids"] if superseded_work_ids is None else superseded_work_ids)
        if self._checkpoints:
            latest = self._checkpoints[-1]
            same_checkpoint = (
                latest["component_attempt_id"] == component_attempt_id
                and latest["component_attempt_index"] == component_attempt_index
                and latest["component_seq"] == component_seq
                and latest["accepted_work_ids"] == accepted
                and latest["pending_work_ids"] == pending
                and latest["halted_work_ids"] == halted
                and latest["superseded_work_ids"] == superseded
                and latest["artifact_index_sha256"] == artifact_index_sha256
                and latest["usage_snapshot_sha256"] == usage_snapshot_sha256
            )
            if same_checkpoint:
                checkpoint_hash = latest["integrity"]["checkpoint_sha256"]
                if not any(
                    row["event"] == "checkpoint_recorded"
                    and row["payload"].get("checkpoint_sha256") == checkpoint_hash
                    for row in self._records
                ):
                    self._append(
                        "checkpoint_recorded",
                        component_attempt_id=component_attempt_id,
                        component_attempt_index=component_attempt_index,
                        work_id=None,
                        logical_request_id=None,
                        payload={
                            "checkpoint_sha256": checkpoint_hash,
                            "checkpoint_ref": (
                                f"recovery/checkpoints/{checkpoint_hash}.json"
                            ),
                        },
                        boundary="checkpoint",
                    )
                return copy.deepcopy(latest)
        previous = None if not self._checkpoints else self._checkpoints[-1]["integrity"]["checkpoint_sha256"]
        draft = {
            "schema_id": CHECKPOINT_SCHEMA_ID,
            "schema_version": SCHEMA_VERSION,
            "workflow_run_id": self.assignment["workflow_run_id"],
            "component_run_id": self.assignment["component_run_id"],
            "component_attempt_id": require_string(component_attempt_id, path="$.component_attempt_id"),
            "component_attempt_index": require_int(component_attempt_index, path="$.component_attempt_index", minimum=1),
            "component_seq": require_int(component_seq, path="$.component_seq", minimum=0),
            "semantic_bindings": copy.deepcopy(self.assignment["semantic_bindings"]),
            "accepted_work_ids": accepted,
            "pending_work_ids": pending,
            "halted_work_ids": halted,
            "superseded_work_ids": superseded,
            "artifact_index_sha256": require_sha256(artifact_index_sha256, path="$.artifact_index_sha256"),
            "usage_snapshot_sha256": None if usage_snapshot_sha256 is None else require_sha256(usage_snapshot_sha256, path="$.usage_snapshot_sha256"),
            "previous_checkpoint_sha256": previous,
            "created_at": self.generated_at,
            "integrity": {"checkpoint_sha256": _ZERO_HASH},
        }
        checkpoint = _validate_checkpoint(
            seal_payload(draft, policy=_POLICY, hash_path=_CHECKPOINT_HASH_PATH),
            assignment=self.assignment,
            previous_hash=previous,
        )
        path = self.checkpoint_root / f"{checkpoint['integrity']['checkpoint_sha256']}.json"
        _write_json(path, checkpoint)
        if not any(row["integrity"]["checkpoint_sha256"] == checkpoint["integrity"]["checkpoint_sha256"] for row in self._checkpoints):
            self._checkpoints.append(checkpoint)
        self._append(
            "checkpoint_recorded",
            component_attempt_id=component_attempt_id,
            component_attempt_index=component_attempt_index,
            work_id=None,
            logical_request_id=None,
            payload={
                "checkpoint_sha256": checkpoint["integrity"]["checkpoint_sha256"],
                "checkpoint_ref": f"recovery/checkpoints/{path.name}",
            },
            boundary="checkpoint",
        )
        return copy.deepcopy(checkpoint)

    def resume(self, *, component_attempt_id: str, component_attempt_index: int) -> None:
        self.validate(require_checkpoint=True)
        self._append(
            "component_resumed",
            component_attempt_id=component_attempt_id,
            component_attempt_index=component_attempt_index,
            work_id=None,
            logical_request_id=None,
            payload={
                "checkpoint_sha256": self._checkpoints[-1]["integrity"]["checkpoint_sha256"],
                "previous_attempt_index": component_attempt_index - 1,
            },
            boundary=None,
        )

    def _append(
        self,
        event: str,
        *,
        component_attempt_id: str,
        component_attempt_index: int,
        work_id: str | None,
        logical_request_id: str | None,
        physical_attempt_id: str | None = None,
        payload: Mapping[str, Any],
        boundary: str | None,
    ) -> dict[str, Any]:
        event = require_enum(event, _JOURNAL_EVENTS, path="$.event")
        if boundary is not None:
            require_enum(boundary, _BOUNDARIES, path="$.boundary")
        previous = None if not self._records else self._records[-1]["integrity"]["journal_sha256"]
        draft = {
            "schema_id": JOURNAL_SCHEMA_ID,
            "schema_version": SCHEMA_VERSION,
            "sequence": len(self._records) + 1,
            "event": event,
            "workflow_run_id": self.assignment["workflow_run_id"],
            "component_run_id": self.assignment["component_run_id"],
            "component_attempt_id": require_string(component_attempt_id, path="$.component_attempt_id"),
            "component_attempt_index": require_int(component_attempt_index, path="$.component_attempt_index", minimum=1),
            "work_id": None if work_id is None else require_string(work_id, path="$.work_id"),
            "logical_request_id": None if logical_request_id is None else require_string(logical_request_id, path="$.logical_request_id"),
            "physical_attempt_id": None if physical_attempt_id is None else require_string(physical_attempt_id, path="$.physical_attempt_id"),
            "payload": copy.deepcopy(dict(require_mapping(payload, path="$.payload"))),
            "previous_journal_sha256": previous,
            "created_at": self.generated_at,
            "integrity": {"journal_sha256": _ZERO_HASH},
        }
        record = _validate_journal_record(
            seal_payload(draft, policy=_POLICY, hash_path=_JOURNAL_HASH_PATH),
            previous_hash=previous,
        )
        path = self.journal_root / f"{record['sequence']:08d}_{record['integrity']['journal_sha256']}.json"
        _write_json(path, record)
        self._inject(boundary)
        self._records.append(record)
        self._rebuild_ledger()
        self._persist_ledger()
        self._inject("ledger" if boundary == "accepted" else None)
        return copy.deepcopy(record)

    def _inject(self, boundary: str | None) -> None:
        if boundary is not None and self.failure_injector is not None:
            self.failure_injector(boundary)

    def _load(self) -> None:
        previous: str | None = None
        for index, path in enumerate(sorted(self.journal_root.glob("*.json")), start=1):
            record = _validate_journal_record(_load_json(path), previous_hash=previous)
            if record["sequence"] != index:
                raise ContractValidationError("recovery_journal_sequence", str(path), "journal sequence is not contiguous")
            self._records.append(record)
            previous = record["integrity"]["journal_sha256"]
        _validate_journal_bindings_v1(
            self._records, assignment=self.assignment
        )
        self._rebuild_ledger()
        ledger_path = self.recovery_root / "work_ledger.json"
        if ledger_path.is_file():
            persisted = _validate_ledger(_load_json(ledger_path), assignment=self.assignment)
            if persisted != self._ledger:
                # A process can die after the immutable journal record is
                # replaced but before its derived ledger projection is
                # replaced.  Recover only that exact one-record window; an
                # arbitrary projection mismatch remains a tamper/error.
                if (
                    self._records
                    and persisted["journal_sequence"] == len(self._records) - 1
                    and persisted == self._ledger_for_records(self._records[:-1])
                ):
                    self._persist_ledger()
                else:
                    raise ContractValidationError(
                        "recovery_ledger_projection",
                        str(ledger_path),
                        "ledger projection differs from journal",
                    )
        else:
            self._persist_ledger()
        self._checkpoints = _load_ordered_checkpoints(
            self.checkpoint_root, assignment=self.assignment
        )
        _validate_checkpoint_journal_bindings_v1(
            self._checkpoints, self._records, assignment=self.assignment
        )

    def _empty_ledger(self) -> dict[str, Any]:
        return _validate_ledger(
            seal_payload(
                {
                    "schema_id": LEDGER_SCHEMA_ID,
                    "schema_version": SCHEMA_VERSION,
                    "workflow_run_id": self.assignment["workflow_run_id"],
                    "component_run_id": self.assignment["component_run_id"],
                    "journal_sequence": 0,
                    "works": [],
                    "accepted_work_ids": [],
                    "pending_work_ids": [],
                    "halted_work_ids": [],
                    "superseded_work_ids": [],
                    "integrity": {"ledger_sha256": _ZERO_HASH},
                },
                policy=_POLICY,
                hash_path=_LEDGER_HASH_PATH,
            ),
            assignment=self.assignment,
        )

    def _persist_ledger(self) -> None:
        _write_projection(self.recovery_root / "work_ledger.json", self._ledger)

    def _work(self, work_id: str) -> dict[str, Any] | None:
        return next((row for row in self._ledger["works"] if row["work_id"] == work_id), None)

    def _require_work(self, work_id: str) -> dict[str, Any]:
        work = self._work(require_string(work_id, path="$.work_id"))
        if work is None:
            raise ContractValidationError("recovery_work_missing", work_id, "unknown work ID")
        return work

    def _physical_record(self, physical_attempt_id: str) -> dict[str, Any] | None:
        return next(
            (
                row
                for row in self._records
                if row["event"] == "physical_attempt_sealed"
                and row["physical_attempt_id"] == physical_attempt_id
            ),
            None,
        )

    def _ledger_for_records(
        self, records: Sequence[Mapping[str, Any]]
    ) -> dict[str, Any]:
        return _build_ledger_projection_v1(
            records, assignment=self.assignment
        )

    def _rebuild_ledger(self) -> None:
        self._ledger = self._ledger_for_records(self._records)


def _validate_incident(value: Mapping[str, Any]) -> dict[str, Any]:
    row = require_mapping(value, path="$.incident")
    required = {
        "schema_id",
        "schema_version",
        "incident_id",
        "workflow_run_id",
        "component_run_id",
        "component_attempt_id",
        "component_attempt_index",
        "category",
        "reason_code",
        "stage_id",
        "work_id",
        "exception_class",
        "safe_message",
        "stack_trace_sha256",
        "producer_code_commit",
        "created_at",
        "integrity",
    }
    require_exact_keys(row, required=required, path="$.incident")
    integrity = require_mapping(row["integrity"], path="$.incident.integrity")
    require_exact_keys(integrity, required={"incident_sha256"}, path="$.incident.integrity")
    normalized = {
        "schema_id": require_enum(row["schema_id"], {INCIDENT_SCHEMA_ID}, path="$.incident.schema_id"),
        "schema_version": require_enum(row["schema_version"], {SCHEMA_VERSION}, path="$.incident.schema_version"),
        "incident_id": require_string(row["incident_id"], path="$.incident.incident_id"),
        "workflow_run_id": require_string(row["workflow_run_id"], path="$.incident.workflow_run_id"),
        "component_run_id": require_string(row["component_run_id"], path="$.incident.component_run_id"),
        "component_attempt_id": require_string(row["component_attempt_id"], path="$.incident.component_attempt_id"),
        "component_attempt_index": require_int(row["component_attempt_index"], path="$.incident.component_attempt_index", minimum=1),
        "category": require_enum(row["category"], _FAILURE_CATEGORIES, path="$.incident.category"),
        "reason_code": require_string(row["reason_code"], path="$.incident.reason_code"),
        "stage_id": require_nullable_string(row["stage_id"], path="$.incident.stage_id"),
        "work_id": require_nullable_string(row["work_id"], path="$.incident.work_id"),
        "exception_class": require_string(row["exception_class"], path="$.incident.exception_class"),
        "safe_message": require_string(row["safe_message"], path="$.incident.safe_message"),
        "stack_trace_sha256": require_sha256(row["stack_trace_sha256"], path="$.incident.stack_trace_sha256"),
        "producer_code_commit": require_string(row["producer_code_commit"], path="$.incident.producer_code_commit"),
        "created_at": require_rfc3339(row["created_at"], path="$.incident.created_at"),
        "integrity": {
            "incident_sha256": require_sha256(integrity["incident_sha256"], path="$.incident.integrity.incident_sha256")
        },
    }
    if any(token in normalized["safe_message"].casefold() for token in ("api_key=", "authorization:", "secret=", "password=")):
        raise ContractValidationError("incident_secret", "$.incident.safe_message", "incident contains a likely secret")
    if not verify_payload_hash(normalized, policy=_POLICY, hash_path=_INCIDENT_HASH_PATH):
        raise ContractValidationError("incident_hash", "$.incident.integrity.incident_sha256", "incident hash drift")
    return normalized
