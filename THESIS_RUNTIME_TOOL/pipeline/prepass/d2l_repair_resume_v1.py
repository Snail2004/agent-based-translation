"""Explicit provenance for a mechanical code repair between component attempts."""

from __future__ import annotations

from copy import deepcopy
import re
from typing import Any, Mapping, Sequence

from pipeline.prepass.d2l_console_replay_contract_v1 import canonical_sha256


SCHEMA_VERSION = "d2l_component_repair_receipt_v2_mechanical_scope"
REPAIR_KIND = "mechanical_runtime_fix"
ATTESTATION = "semantic_contract_unchanged"
REPAIR_SCOPE_POLICY_ID = "d2l_mechanical_repair_paths_v1"
_GIT_RE = re.compile(r"^[0-9a-f]{40}$")
_SHA_RE = re.compile(r"^[0-9A-F]{64}$")
_MECHANICAL_RUNTIME_PATHS = frozenset(
    {
        "THESIS_RUNTIME_TOOL/app/backend/routes/thesis_runs.py",
        "THESIS_RUNTIME_TOOL/app/backend/services/thesis_runs.py",
        "THESIS_RUNTIME_TOOL/app/backend/tests/test_thesis_runs.py",
        (
            "THESIS_RUNTIME_TOOL/pipeline/prepass/"
            "d2l_component_writer_lease_v1.py"
        ),
        (
            "THESIS_RUNTIME_TOOL/pipeline/prepass/"
            "d2l_console_replay_contract_v1.py"
        ),
        "THESIS_RUNTIME_TOOL/pipeline/prepass/d2l_repair_resume_v1.py",
        (
            "THESIS_RUNTIME_TOOL/pipeline/prepass/"
            "d2l_stage_work_journal_v1.py"
        ),
        (
            "THESIS_RUNTIME_TOOL/pipeline/prepass/"
            "d2l_stage_process_guard_v1.py"
        ),
        (
            "THESIS_RUNTIME_TOOL/pipeline/prepass/"
            "d2l_stage_process_tree_v1.py"
        ),
        (
            "THESIS_RUNTIME_TOOL/pipeline/prepass/"
            "d2l_translation_component_runner_v1.py"
        ),
        "THESIS_RUNTIME_TOOL/pipeline/scripts/run_d2l_project_campaign.py",
        "THESIS_RUNTIME_TOOL/pipeline/scripts/run_workflow_orchestrator_v1.py",
    }
)
_MECHANICAL_SUPPORT_PREFIXES = (
    "THESIS_RUNTIME_TOOL/pipeline/tests/",
    "THESIS_RUNTIME_TOOL/tasks/",
)


class D2LRepairResumeError(ValueError):
    """Raised when a repair receipt cannot authorize a same-run Resume."""


def _text(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise D2LRepairResumeError(f"{label} must be a non-empty string")
    return value


def _integer(value: Any, label: str, *, minimum: int = 1) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise D2LRepairResumeError(f"{label} must be an integer >= {minimum}")
    return value


def _sha(value: Any, label: str) -> str:
    digest = _text(value, label).upper()
    if not _SHA_RE.fullmatch(digest):
        raise D2LRepairResumeError(f"{label} must be a SHA-256")
    return digest


def _git(value: Any, label: str) -> str:
    revision = _text(value, label).lower()
    if not _GIT_RE.fullmatch(revision):
        raise D2LRepairResumeError(f"{label} must be a Git commit")
    return revision


def _payload(value: Mapping[str, Any]) -> dict[str, Any]:
    row = deepcopy(dict(value))
    row.pop("integrity", None)
    return row


def validate_mechanical_repair_paths(paths: Sequence[str]) -> list[str]:
    normalized = sorted(set(str(path) for path in paths))
    if not normalized:
        raise D2LRepairResumeError("repair changed_paths cannot be empty")
    for path in normalized:
        if (
            not path
            or "\\" in path
            or path.startswith("/")
            or ".." in path.split("/")
        ):
            raise D2LRepairResumeError(
                "repair changed_paths must be sorted unique relative paths"
            )
        if path in _MECHANICAL_RUNTIME_PATHS:
            continue
        if any(path.startswith(prefix) for prefix in _MECHANICAL_SUPPORT_PREFIXES):
            continue
        raise D2LRepairResumeError(
            f"repair path is outside the closed mechanical scope: {path}"
        )
    return normalized


def validate_repair_receipt(value: Mapping[str, Any]) -> dict[str, Any]:
    row = deepcopy(dict(value))
    expected = {
        "schema_version",
        "workflow_run_id",
        "component_run_id",
        "previous_component_attempt_id",
        "next_component_attempt_id",
        "stage_id",
        "checkpoint_ref",
        "checkpoint_sha256",
        "repair_kind",
        "repair_scope_policy_id",
        "reason_code",
        "operator_attestation",
        "baseline_code_revision",
        "effective_code_revision",
        "semantic_contract_sha256",
        "runner_plan_sha256",
        "git_delta_sha256",
        "changed_paths",
        "created_at",
        "integrity",
    }
    if set(row) != expected:
        raise D2LRepairResumeError("repair receipt keys mismatch")
    if row["schema_version"] != SCHEMA_VERSION:
        raise D2LRepairResumeError("repair receipt schema is invalid")
    for key in (
        "workflow_run_id",
        "component_run_id",
        "stage_id",
        "checkpoint_ref",
        "reason_code",
        "created_at",
    ):
        _text(row[key], f"repair_receipt.{key}")
    previous_attempt = _integer(
        row["previous_component_attempt_id"],
        "repair_receipt.previous_component_attempt_id",
    )
    if (
        _integer(
            row["next_component_attempt_id"],
            "repair_receipt.next_component_attempt_id",
        )
        != previous_attempt + 1
    ):
        raise D2LRepairResumeError(
            "repair receipt attempts must be contiguous"
        )
    if row["repair_kind"] != REPAIR_KIND:
        raise D2LRepairResumeError("repair kind is invalid")
    if row["repair_scope_policy_id"] != REPAIR_SCOPE_POLICY_ID:
        raise D2LRepairResumeError("repair scope policy is invalid")
    if row["operator_attestation"] != ATTESTATION:
        raise D2LRepairResumeError("repair attestation is invalid")
    baseline = _git(
        row["baseline_code_revision"],
        "repair_receipt.baseline_code_revision",
    )
    effective = _git(
        row["effective_code_revision"],
        "repair_receipt.effective_code_revision",
    )
    if baseline == effective:
        raise D2LRepairResumeError("repair must change the code revision")
    for key in (
        "checkpoint_sha256",
        "semantic_contract_sha256",
        "runner_plan_sha256",
        "git_delta_sha256",
    ):
        row[key] = _sha(row[key], f"repair_receipt.{key}")
    changed_paths = row["changed_paths"]
    if (
        not isinstance(changed_paths, list)
        or not changed_paths
        or any(
            not isinstance(path, str)
            or not path
            or "\\" in path
            or path.startswith("/")
            or ".." in path.split("/")
            for path in changed_paths
        )
        or changed_paths != sorted(set(changed_paths))
    ):
        raise D2LRepairResumeError(
            "repair changed_paths must be sorted unique relative paths"
        )
    changed_paths = validate_mechanical_repair_paths(changed_paths)
    integrity = row["integrity"]
    if not isinstance(integrity, Mapping) or set(integrity) != {
        "payload_sha256"
    }:
        raise D2LRepairResumeError("repair receipt integrity is invalid")
    expected_sha = canonical_sha256(_payload(row))
    if _sha(
        integrity["payload_sha256"],
        "repair_receipt.integrity.payload_sha256",
    ) != expected_sha:
        raise D2LRepairResumeError("repair receipt payload hash drift")
    row["baseline_code_revision"] = baseline
    row["effective_code_revision"] = effective
    row["changed_paths"] = list(changed_paths)
    row["integrity"] = {"payload_sha256": expected_sha}
    return row


def build_repair_receipt(
    *,
    workflow_run_id: str,
    component_run_id: str,
    previous_component_attempt_id: int,
    stage_id: str,
    checkpoint_ref: str,
    checkpoint_sha256: str,
    reason_code: str,
    baseline_code_revision: str,
    effective_code_revision: str,
    semantic_contract_sha256: str,
    runner_plan_sha256: str,
    git_delta_sha256: str,
    changed_paths: Sequence[str],
    created_at: str,
) -> dict[str, Any]:
    row = {
        "schema_version": SCHEMA_VERSION,
        "workflow_run_id": workflow_run_id,
        "component_run_id": component_run_id,
        "previous_component_attempt_id": previous_component_attempt_id,
        "next_component_attempt_id": previous_component_attempt_id + 1,
        "stage_id": stage_id,
        "checkpoint_ref": checkpoint_ref,
        "checkpoint_sha256": checkpoint_sha256,
        "repair_kind": REPAIR_KIND,
        "repair_scope_policy_id": REPAIR_SCOPE_POLICY_ID,
        "reason_code": reason_code,
        "operator_attestation": ATTESTATION,
        "baseline_code_revision": baseline_code_revision,
        "effective_code_revision": effective_code_revision,
        "semantic_contract_sha256": semantic_contract_sha256,
        "runner_plan_sha256": runner_plan_sha256,
        "git_delta_sha256": git_delta_sha256,
        "changed_paths": sorted(set(str(path) for path in changed_paths)),
        "created_at": created_at,
    }
    row["integrity"] = {"payload_sha256": canonical_sha256(row)}
    return validate_repair_receipt(row)


__all__ = [
    "ATTESTATION",
    "D2LRepairResumeError",
    "REPAIR_SCOPE_POLICY_ID",
    "REPAIR_KIND",
    "SCHEMA_VERSION",
    "build_repair_receipt",
    "validate_mechanical_repair_paths",
    "validate_repair_receipt",
]
