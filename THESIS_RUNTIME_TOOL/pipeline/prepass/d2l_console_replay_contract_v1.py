"""D2L translation-component observability contract for Workflow Replay V1.

This module owns only the D2L translation component package.  It deliberately
does not create the parent workflow manifest, the parent event stream, a global
sequence, or the final five-arm scoring handoff.  Those are neutral Workflow
Relay authorities defined by coordination decision DEC-057.
"""

from __future__ import annotations

import json
import os
import re
from collections import Counter
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from typing import Any, Mapping, Sequence


COMPONENT_MANIFEST_SCHEMA = "d2l_translation_component_manifest_v1"
COMPONENT_EVENT_SCHEMA = "d2l_translation_component_event_v1"
ARTIFACT_INDEX_SCHEMA = "d2l_translation_artifact_index_v1"
CHECKPOINT_SCHEMA = "d2l_translation_checkpoint_v1"
SCORING_FRAGMENT_SCHEMA = "scoring_handoff_fragment_v1"
SOURCE_BINDING_SCHEMA = "canonical_source_binding_v1"

COMPONENT_ID = "translation"
FLOW_KIND = "terminology_translation"

STAGE_IDS = (
    "preflight",
    "b1_candidate_discovery",
    "candidate_index",
    "b2_admission_translation",
    "auditor_morphology",
    "auditor_target_collision",
    "auditor_multi_target",
    "glossary_seal",
    "translator",
    "translation_quality_audit",
    "scoring_handoff_fragment",
)

EVENT_NAMES = (
    "run_start",
    "run_resumed",
    "stage_start",
    "work_started",
    "request_sent",
    "response_received",
    "validation_passed",
    "validation_failed",
    "retry",
    "checkpoint",
    "artifact_created",
    "stage_done",
    "cost_snapshot",
    "run_done",
    "run_failed",
)

RUN_LEVEL_EVENTS = {"run_start", "run_resumed", "run_done", "run_failed"}
STAGE_STATUSES = {
    "pending",
    "running",
    "paused",
    "succeeded",
    "failed",
    "skipped",
    "reused",
    "cancelled",
}
COMPONENT_STATUSES = {"planned", "running", "paused", "succeeded", "failed", "cancelled"}
TERMINAL_COMPONENT_STATUSES = {"succeeded", "failed", "cancelled"}
OUTCOMES = {"succeeded", "failed", "skipped", "reused", "cancelled"}
SEVERITIES = {"info", "warning", "error"}
COST_STATUSES = {"provider_actual", "pinned_tariff", "unknown"}
CACHE_STATUSES = {"hit", "miss", "bypass", "unknown"}
CACHE_MECHANISMS = {
    "none",
    "provider_prompt_cache",
    "provider_implicit_cache",
    "local_exact_cache",
    "unknown",
}
TIMING_AUTHORITIES = {"recorded", "logical_order_only"}
SHA256_KINDS = {"physical", "canonical:d2l_canonical_json_v1"}

_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,190}$")
_SHA_RE = re.compile(r"^[0-9A-Fa-f]{64}$")
_GIT_RE = re.compile(r"^[0-9A-Fa-f]{40}$")
_FORBIDDEN_EVENT_KEYS = {
    "raw_prompt",
    "raw_response",
    "prompt_text",
    "response_text",
    "api_key",
    "secret",
    "gold",
    "oracle",
    "reference_text",
}

_IMMUTABLE_MANIFEST_KEYS = (
    "workflow_run_id",
    "flow_kind",
    "pipeline_id",
    "pipeline_version",
    "component_id",
    "component_run_id",
    "selected_chapter_ids",
    "source_binding",
    "config_sha256",
    "code_revision",
    "event_log_ref",
    "artifact_index_ref",
    "reconstructed",
    "timing_authority",
    "lineage",
)


class D2LConsoleContractError(ValueError):
    """Raised when a D2L component package is unsafe to relay."""


def canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def canonical_sha256(value: Any) -> str:
    return sha256(canonical_json_bytes(value)).hexdigest().upper()


def file_sha256(path: str | Path) -> str:
    return sha256(Path(path).read_bytes()).hexdigest().upper()


def _require_mapping(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise D2LConsoleContractError(f"{label} must be an object")
    return value


def _require_list(value: Any, label: str) -> list[Any]:
    if not isinstance(value, list):
        raise D2LConsoleContractError(f"{label} must be an array")
    return value


def _require_string(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise D2LConsoleContractError(f"{label} must be a non-empty string")
    return value


def _require_id(value: Any, label: str) -> str:
    value = _require_string(value, label)
    if not _ID_RE.fullmatch(value):
        raise D2LConsoleContractError(f"{label} has invalid identifier syntax")
    return value


def _require_sha(value: Any, label: str) -> str:
    value = _require_string(value, label)
    if not _SHA_RE.fullmatch(value):
        raise D2LConsoleContractError(f"{label} must be a SHA-256")
    return value.upper()


def _require_int(value: Any, label: str, *, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise D2LConsoleContractError(f"{label} must be an integer >= {minimum}")
    return value


def _require_bool(value: Any, label: str) -> bool:
    if not isinstance(value, bool):
        raise D2LConsoleContractError(f"{label} must be boolean")
    return value


def _require_exact_keys(
    value: Mapping[str, Any],
    required: set[str],
    label: str,
    *,
    optional: set[str] | None = None,
) -> None:
    missing = sorted(required - set(value))
    if missing:
        raise D2LConsoleContractError(f"{label} missing keys: {', '.join(missing)}")
    unknown = sorted(set(value) - required - (optional or set()))
    if unknown:
        raise D2LConsoleContractError(f"{label} has unknown keys: {', '.join(unknown)}")


def _validate_relative_ref(value: Any, label: str) -> str:
    value = _require_string(value, label)
    path = Path(value)
    if path.is_absolute() or ".." in path.parts:
        raise D2LConsoleContractError(f"{label} must be package-relative")
    return path.as_posix()


def _validate_timestamp(value: Any, *, allow_null: bool, label: str) -> None:
    if value is None:
        if not allow_null:
            raise D2LConsoleContractError(f"{label} cannot be null")
        return
    value = _require_string(value, label)
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise D2LConsoleContractError(f"{label} must be RFC3339") from exc
    if parsed.tzinfo is None or parsed.utcoffset() != UTC.utcoffset(parsed):
        raise D2LConsoleContractError(f"{label} must be UTC")


def _validate_string_list(value: Any, label: str, *, nonempty: bool = True) -> list[str]:
    rows = _require_list(value, label)
    if nonempty and not rows:
        raise D2LConsoleContractError(f"{label} cannot be empty")
    if any(not isinstance(item, str) or not item for item in rows):
        raise D2LConsoleContractError(f"{label} must contain non-empty strings")
    if len(rows) != len(set(rows)):
        raise D2LConsoleContractError(f"{label} contains duplicates")
    return list(rows)


def _validate_progress(value: Any, label: str) -> dict[str, Any]:
    row = dict(_require_mapping(value, label))
    _require_exact_keys(row, {"completed", "total", "unit"}, label)
    completed = _require_int(row["completed"], f"{label}.completed")
    if row["total"] is not None:
        total = _require_int(row["total"], f"{label}.total")
        if completed > total:
            raise D2LConsoleContractError(f"{label}.completed exceeds total")
    _require_string(row["unit"], f"{label}.unit")
    return row


def validate_typed_binding(
    value: Any,
    label: str,
    *,
    allow_none: bool = False,
) -> dict[str, Any] | None:
    if value is None:
        if allow_none:
            return None
        raise D2LConsoleContractError(f"{label} cannot be null")
    row = dict(_require_mapping(value, label))
    _require_exact_keys(
        row,
        {"artifact_ref", "artifact_kind", "schema_version", "sha256", "sha256_kind"},
        label,
    )
    _require_id(row["artifact_ref"], f"{label}.artifact_ref")
    _require_string(row["artifact_kind"], f"{label}.artifact_kind")
    _require_string(row["schema_version"], f"{label}.schema_version")
    row["sha256"] = _require_sha(row["sha256"], f"{label}.sha256")
    if row["sha256_kind"] not in SHA256_KINDS:
        raise D2LConsoleContractError(f"{label}.sha256_kind is unsupported")
    return row


def validate_source_binding(value: Any, label: str = "source_binding") -> dict[str, Any]:
    row = dict(_require_mapping(value, label))
    _require_exact_keys(
        row,
        {
            "schema",
            "document",
            "structure_manifest",
            "asset_manifest",
            "admitted_projection",
            "normalization_receipt",
            "package_seal",
        },
        label,
    )
    if row["schema"] != SOURCE_BINDING_SCHEMA:
        raise D2LConsoleContractError(f"{label}.schema is invalid")
    for key in (
        "document",
        "structure_manifest",
        "asset_manifest",
        "admitted_projection",
        "normalization_receipt",
        "package_seal",
    ):
        validate_typed_binding(row[key], f"{label}.{key}")
    return row


def _validate_resume(value: Any, label: str = "manifest.resume") -> dict[str, Any]:
    row = dict(_require_mapping(value, label))
    _require_exact_keys(
        row,
        {
            "resume_available",
            "checkpoint_ref",
            "checkpoint_sha256",
            "stage_id",
            "work_id",
            "paused_reason",
        },
        label,
    )
    available = _require_bool(row["resume_available"], f"{label}.resume_available")
    detail = (
        row["checkpoint_ref"],
        row["checkpoint_sha256"],
        row["stage_id"],
        row["work_id"],
        row["paused_reason"],
    )
    if available:
        _validate_relative_ref(row["checkpoint_ref"], f"{label}.checkpoint_ref")
        _require_sha(row["checkpoint_sha256"], f"{label}.checkpoint_sha256")
        _require_id(row["stage_id"], f"{label}.stage_id")
        _require_id(row["work_id"], f"{label}.work_id")
        _require_string(row["paused_reason"], f"{label}.paused_reason")
    elif any(item is not None for item in detail):
        raise D2LConsoleContractError(f"{label} detail must be null when resume is unavailable")
    return row


def validate_stages(value: Any) -> tuple[dict[str, Any], ...]:
    rows = _require_list(value, "manifest.stages")
    if not rows:
        raise D2LConsoleContractError("manifest.stages cannot be empty")
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    orders: list[int] = []
    for index, raw in enumerate(rows):
        label = f"manifest.stages[{index}]"
        row = dict(_require_mapping(raw, label))
        _require_exact_keys(
            row,
            {
                "stage_id",
                "order",
                "label",
                "producer",
                "component_id",
                "status",
                "started_at",
                "ended_at",
                "progress",
                "current_work_id",
                "artifact_refs",
            },
            label,
        )
        stage_id = _require_id(row["stage_id"], f"{label}.stage_id")
        if stage_id in seen:
            raise D2LConsoleContractError(f"duplicate stage_id: {stage_id}")
        seen.add(stage_id)
        orders.append(_require_int(row["order"], f"{label}.order", minimum=1))
        _require_string(row["label"], f"{label}.label")
        _require_string(row["producer"], f"{label}.producer")
        if row["component_id"] != COMPONENT_ID:
            raise D2LConsoleContractError(f"{label}.component_id must be translation")
        if row["status"] not in STAGE_STATUSES:
            raise D2LConsoleContractError(f"{label}.status is invalid")
        _validate_timestamp(row["started_at"], allow_null=True, label=f"{label}.started_at")
        _validate_timestamp(row["ended_at"], allow_null=True, label=f"{label}.ended_at")
        _validate_progress(row["progress"], f"{label}.progress")
        if row["current_work_id"] is not None:
            _require_id(row["current_work_id"], f"{label}.current_work_id")
        refs = _validate_string_list(row["artifact_refs"], f"{label}.artifact_refs", nonempty=False)
        for ref in refs:
            _require_id(ref, f"{label}.artifact_refs[]")
        result.append(row)
    if sorted(orders) != list(range(1, len(rows) + 1)):
        raise D2LConsoleContractError("stage order must be contiguous from 1")
    return tuple(result)


def validate_component_manifest(value: Mapping[str, Any]) -> dict[str, Any]:
    row = dict(_require_mapping(value, "manifest"))
    _require_exact_keys(
        row,
        {
            "schema",
            "workflow_run_id",
            "flow_kind",
            "pipeline_id",
            "pipeline_version",
            "component_id",
            "component_run_id",
            "component_attempt_id",
            "status",
            "started_at",
            "updated_at",
            "active_stage_id",
            "selected_chapter_ids",
            "source_binding",
            "config_sha256",
            "code_revision",
            "stages",
            "event_log_ref",
            "artifact_index_ref",
            "scoring_handoff_fragment_ref",
            "resume",
            "reconstructed",
            "timing_authority",
            "lineage",
        },
        "manifest",
    )
    if row["schema"] != COMPONENT_MANIFEST_SCHEMA:
        raise D2LConsoleContractError("manifest.schema is invalid")
    _require_id(row["workflow_run_id"], "manifest.workflow_run_id")
    if row["flow_kind"] != FLOW_KIND:
        raise D2LConsoleContractError("manifest.flow_kind is invalid")
    _require_id(row["pipeline_id"], "manifest.pipeline_id")
    _require_id(row["pipeline_version"], "manifest.pipeline_version")
    if row["component_id"] != COMPONENT_ID:
        raise D2LConsoleContractError("manifest.component_id must be translation")
    _require_id(row["component_run_id"], "manifest.component_run_id")
    _require_int(row["component_attempt_id"], "manifest.component_attempt_id", minimum=1)
    if row["status"] not in COMPONENT_STATUSES:
        raise D2LConsoleContractError("manifest.status is invalid")
    allow_null_time = bool(row["reconstructed"])
    _validate_timestamp(row["started_at"], allow_null=allow_null_time, label="manifest.started_at")
    _validate_timestamp(row["updated_at"], allow_null=allow_null_time, label="manifest.updated_at")
    chapters = _validate_string_list(row["selected_chapter_ids"], "manifest.selected_chapter_ids")
    validate_source_binding(row["source_binding"], "manifest.source_binding")
    _require_sha(row["config_sha256"], "manifest.config_sha256")
    code_revision = _require_string(row["code_revision"], "manifest.code_revision")
    if not _GIT_RE.fullmatch(code_revision):
        raise D2LConsoleContractError("manifest.code_revision must be a full Git commit")
    stages = validate_stages(row["stages"])
    stage_ids = {stage["stage_id"] for stage in stages}
    if tuple(stage["stage_id"] for stage in stages) != STAGE_IDS:
        raise D2LConsoleContractError("manifest stage schedule is not D2L Translation V1")
    if row["active_stage_id"] is not None and row["active_stage_id"] not in stage_ids:
        raise D2LConsoleContractError("manifest.active_stage_id is unknown")
    if row["status"] in {"running", "paused"} and row["active_stage_id"] is None:
        raise D2LConsoleContractError("active component requires active_stage_id")
    if row["status"] in TERMINAL_COMPONENT_STATUSES and row["active_stage_id"] is not None:
        raise D2LConsoleContractError("terminal component cannot have active_stage_id")
    _validate_relative_ref(row["event_log_ref"], "manifest.event_log_ref")
    _validate_relative_ref(row["artifact_index_ref"], "manifest.artifact_index_ref")
    if row["scoring_handoff_fragment_ref"] is not None:
        _validate_relative_ref(
            row["scoring_handoff_fragment_ref"],
            "manifest.scoring_handoff_fragment_ref",
        )
    if row["status"] == "succeeded" and row["scoring_handoff_fragment_ref"] is None:
        raise D2LConsoleContractError("successful component requires scoring fragment")
    resume = _validate_resume(row["resume"])
    if row["status"] == "paused" and not resume["resume_available"]:
        raise D2LConsoleContractError("paused component must publish a resumable checkpoint")
    _require_bool(row["reconstructed"], "manifest.reconstructed")
    if row["timing_authority"] not in TIMING_AUTHORITIES:
        raise D2LConsoleContractError("manifest.timing_authority is invalid")
    if row["reconstructed"] != (row["timing_authority"] == "logical_order_only"):
        raise D2LConsoleContractError("reconstructed and timing_authority disagree")
    lineage = dict(_require_mapping(row["lineage"], "manifest.lineage"))
    _require_exact_keys(lineage, {"kind", "parent_component_run_id"}, "manifest.lineage")
    if lineage["kind"] not in {"origin", "rerun", "reconstructed"}:
        raise D2LConsoleContractError("manifest.lineage.kind is invalid")
    if lineage["kind"] == "rerun":
        _require_id(lineage["parent_component_run_id"], "manifest.lineage.parent_component_run_id")
    elif lineage["parent_component_run_id"] is not None:
        raise D2LConsoleContractError("only rerun lineage may cite a parent component run")
    if (lineage["kind"] == "reconstructed") != row["reconstructed"]:
        raise D2LConsoleContractError("manifest lineage and reconstructed flag disagree")
    if not chapters:
        raise D2LConsoleContractError("manifest must select at least one chapter")
    return row


def component_manifest_sha256(value: Mapping[str, Any]) -> str:
    return canonical_sha256(validate_component_manifest(value))


def _validate_usage(value: Any, label: str) -> dict[str, Any]:
    row = dict(_require_mapping(value, label))
    _require_exact_keys(
        row,
        {
            "logical_request_id",
            "physical_attempt_index",
            "provider_id",
            "model_id",
            "source_id",
            "masked_quota_bucket",
            "prompt_tokens",
            "completion_tokens",
            "cached_input_tokens",
            "reasoning_tokens",
            "total_tokens",
            "latency_ms",
            "finish_reason",
            "cost_usd",
            "currency",
            "cost_status",
            "cache_status",
            "cache_mechanism",
        },
        label,
    )
    for key in (
        "logical_request_id",
        "provider_id",
        "model_id",
        "source_id",
        "masked_quota_bucket",
    ):
        _require_string(row[key], f"{label}.{key}")
    _require_int(row["physical_attempt_index"], f"{label}.physical_attempt_index", minimum=1)
    for key in (
        "prompt_tokens",
        "completion_tokens",
        "cached_input_tokens",
        "reasoning_tokens",
        "total_tokens",
        "latency_ms",
    ):
        _require_int(row[key], f"{label}.{key}")
    if row["finish_reason"] is not None:
        _require_string(row["finish_reason"], f"{label}.finish_reason")
    if row["cost_usd"] is not None and (
        isinstance(row["cost_usd"], bool)
        or not isinstance(row["cost_usd"], (int, float))
        or row["cost_usd"] < 0
    ):
        raise D2LConsoleContractError(f"{label}.cost_usd must be non-negative or null")
    if row["currency"] is not None:
        _require_string(row["currency"], f"{label}.currency")
    if row["cost_status"] not in COST_STATUSES:
        raise D2LConsoleContractError(f"{label}.cost_status is invalid")
    if row["cost_status"] == "unknown" and row["cost_usd"] is not None:
        raise D2LConsoleContractError("unknown cost must remain null")
    if row["cache_status"] not in CACHE_STATUSES:
        raise D2LConsoleContractError(f"{label}.cache_status is invalid")
    if row["cache_mechanism"] not in CACHE_MECHANISMS:
        raise D2LConsoleContractError(f"{label}.cache_mechanism is invalid")
    return row


def _reject_forbidden_event_keys(value: Any, label: str = "event.payload") -> None:
    if isinstance(value, Mapping):
        for key, child in value.items():
            if str(key).lower() in _FORBIDDEN_EVENT_KEYS:
                raise D2LConsoleContractError(f"{label} contains forbidden key: {key}")
            _reject_forbidden_event_keys(child, f"{label}.{key}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _reject_forbidden_event_keys(child, f"{label}[{index}]")


def _validate_event_payload(event_name: str, value: Any) -> dict[str, Any]:
    row = dict(_require_mapping(value, f"{event_name}.payload"))
    required = {
        "run_start": {"manifest_ref", "manifest_sha256", "selected_chapter_ids"},
        "run_resumed": {
            "previous_component_attempt_id",
            "checkpoint_ref",
            "checkpoint_sha256",
            "reason_code",
        },
        "stage_start": {"progress", "current_work_id"},
        "work_started": {"work_kind", "work_id", "progress"},
        "request_sent": {
            "logical_request_id",
            "physical_attempt_index",
            "work_kind",
            "work_id",
            "provider_id",
            "model_id",
            "source_id",
            "masked_quota_bucket",
        },
        "response_received": {"usage"},
        "validation_passed": {"validator_id", "subject_ref", "reason_codes", "retryable"},
        "validation_failed": {"validator_id", "subject_ref", "reason_codes", "retryable"},
        "retry": {
            "retry_kind",
            "index",
            "max",
            "reason_code",
            "logical_request_id",
            "work_kind",
            "work_id",
        },
        "checkpoint": {
            "checkpoint_ref",
            "checkpoint_sha256",
            "stage_id",
            "work_id",
            "resume_available",
            "paused_reason",
        },
        "artifact_created": {
            "artifact_ref",
            "artifact_kind",
            "schema_version",
            "sha256",
            "sha256_kind",
            "parent_artifact_refs",
        },
        "stage_done": {"outcome", "reason_code", "progress"},
        "cost_snapshot": {
            "scope",
            "logical_request_count",
            "physical_attempt_count",
            "prompt_tokens",
            "completion_tokens",
            "cached_input_tokens",
            "reasoning_tokens",
            "total_tokens",
            "cost_usd",
            "currency",
            "cost_status",
            "cache_counters",
        },
        "run_done": {
            "artifact_index_ref",
            "artifact_index_sha256",
            "scoring_handoff_fragment_ref",
            "scoring_handoff_fragment_sha256",
            "outcome",
        },
        "run_failed": {
            "failed_stage_id",
            "error_code",
            "message",
            "retryable",
            "checkpoint_ref",
            "checkpoint_sha256",
        },
    }
    _require_exact_keys(row, required[event_name], f"{event_name}.payload")
    _reject_forbidden_event_keys(row)
    if event_name == "run_start":
        _validate_relative_ref(row["manifest_ref"], "run_start.manifest_ref")
        _require_sha(row["manifest_sha256"], "run_start.manifest_sha256")
        _validate_string_list(row["selected_chapter_ids"], "run_start.selected_chapter_ids")
    elif event_name == "run_resumed":
        _require_int(row["previous_component_attempt_id"], "run_resumed.previous_component_attempt_id", minimum=1)
        _validate_relative_ref(row["checkpoint_ref"], "run_resumed.checkpoint_ref")
        _require_sha(row["checkpoint_sha256"], "run_resumed.checkpoint_sha256")
        _require_string(row["reason_code"], "run_resumed.reason_code")
    elif event_name == "stage_start":
        _validate_progress(row["progress"], "stage_start.progress")
        if row["current_work_id"] is not None:
            _require_id(row["current_work_id"], "stage_start.current_work_id")
    elif event_name == "work_started":
        _require_string(row["work_kind"], "work_started.work_kind")
        _require_id(row["work_id"], "work_started.work_id")
        _validate_progress(row["progress"], "work_started.progress")
    elif event_name == "request_sent":
        for key in (
            "logical_request_id",
            "work_kind",
            "work_id",
            "provider_id",
            "model_id",
            "source_id",
            "masked_quota_bucket",
        ):
            _require_string(row[key], f"request_sent.{key}")
        _require_int(row["physical_attempt_index"], "request_sent.physical_attempt_index", minimum=1)
    elif event_name == "response_received":
        _validate_usage(row["usage"], "response_received.usage")
    elif event_name in {"validation_passed", "validation_failed"}:
        _require_string(row["validator_id"], f"{event_name}.validator_id")
        _require_string(row["subject_ref"], f"{event_name}.subject_ref")
        reasons = _require_list(row["reason_codes"], f"{event_name}.reason_codes")
        if any(not isinstance(reason, str) or not reason for reason in reasons):
            raise D2LConsoleContractError(f"{event_name}.reason_codes must contain strings")
        _require_bool(row["retryable"], f"{event_name}.retryable")
    elif event_name == "retry":
        for key in ("retry_kind", "reason_code", "logical_request_id", "work_kind", "work_id"):
            _require_string(row[key], f"retry.{key}")
        index = _require_int(row["index"], "retry.index", minimum=1)
        maximum = _require_int(row["max"], "retry.max", minimum=1)
        if index > maximum:
            raise D2LConsoleContractError("retry.index exceeds retry.max")
    elif event_name == "checkpoint":
        _validate_relative_ref(row["checkpoint_ref"], "checkpoint.checkpoint_ref")
        _require_sha(row["checkpoint_sha256"], "checkpoint.checkpoint_sha256")
        _require_id(row["stage_id"], "checkpoint.stage_id")
        _require_id(row["work_id"], "checkpoint.work_id")
        available = _require_bool(row["resume_available"], "checkpoint.resume_available")
        if available:
            _require_string(row["paused_reason"], "checkpoint.paused_reason")
        elif row["paused_reason"] is not None:
            raise D2LConsoleContractError("non-resumable checkpoint cannot have paused_reason")
    elif event_name == "artifact_created":
        _require_id(row["artifact_ref"], "artifact_created.artifact_ref")
        _require_string(row["artifact_kind"], "artifact_created.artifact_kind")
        _require_string(row["schema_version"], "artifact_created.schema_version")
        _require_sha(row["sha256"], "artifact_created.sha256")
        if row["sha256_kind"] not in SHA256_KINDS:
            raise D2LConsoleContractError("artifact_created.sha256_kind is invalid")
        refs = _validate_string_list(
            row["parent_artifact_refs"],
            "artifact_created.parent_artifact_refs",
            nonempty=False,
        )
        for ref in refs:
            _require_id(ref, "artifact_created.parent_artifact_refs[]")
    elif event_name == "stage_done":
        if row["outcome"] not in OUTCOMES:
            raise D2LConsoleContractError("stage_done.outcome is invalid")
        _require_string(row["reason_code"], "stage_done.reason_code")
        _validate_progress(row["progress"], "stage_done.progress")
    elif event_name == "cost_snapshot":
        _require_string(row["scope"], "cost_snapshot.scope")
        for key in (
            "logical_request_count",
            "physical_attempt_count",
            "prompt_tokens",
            "completion_tokens",
            "cached_input_tokens",
            "reasoning_tokens",
            "total_tokens",
        ):
            _require_int(row[key], f"cost_snapshot.{key}")
        if row["cost_usd"] is not None and (
            isinstance(row["cost_usd"], bool)
            or not isinstance(row["cost_usd"], (int, float))
            or row["cost_usd"] < 0
        ):
            raise D2LConsoleContractError("cost_snapshot.cost_usd is invalid")
        if row["currency"] is not None:
            _require_string(row["currency"], "cost_snapshot.currency")
        if row["cost_status"] not in COST_STATUSES:
            raise D2LConsoleContractError("cost_snapshot.cost_status is invalid")
        if row["cost_status"] == "unknown" and row["cost_usd"] is not None:
            raise D2LConsoleContractError("unknown cost must remain null")
        counters = _require_mapping(row["cache_counters"], "cost_snapshot.cache_counters")
        for key, count in counters.items():
            _require_string(key, "cost_snapshot.cache_counters key")
            _require_int(count, f"cost_snapshot.cache_counters.{key}")
    elif event_name == "run_done":
        _validate_relative_ref(row["artifact_index_ref"], "run_done.artifact_index_ref")
        _require_sha(row["artifact_index_sha256"], "run_done.artifact_index_sha256")
        _validate_relative_ref(
            row["scoring_handoff_fragment_ref"],
            "run_done.scoring_handoff_fragment_ref",
        )
        _require_sha(
            row["scoring_handoff_fragment_sha256"],
            "run_done.scoring_handoff_fragment_sha256",
        )
        if row["outcome"] != "succeeded":
            raise D2LConsoleContractError("run_done outcome must be succeeded")
    elif event_name == "run_failed":
        if row["failed_stage_id"] is not None:
            _require_id(row["failed_stage_id"], "run_failed.failed_stage_id")
        _require_string(row["error_code"], "run_failed.error_code")
        _require_string(row["message"], "run_failed.message")
        _require_bool(row["retryable"], "run_failed.retryable")
        paired = (row["checkpoint_ref"], row["checkpoint_sha256"])
        if any(item is None for item in paired) and any(item is not None for item in paired):
            raise D2LConsoleContractError("run_failed checkpoint ref/hash must be paired")
        if row["checkpoint_ref"] is not None:
            _validate_relative_ref(row["checkpoint_ref"], "run_failed.checkpoint_ref")
            _require_sha(row["checkpoint_sha256"], "run_failed.checkpoint_sha256")
    return row


def validate_component_event(
    value: Mapping[str, Any],
    *,
    manifest: Mapping[str, Any],
    expected_component_seq: int,
) -> dict[str, Any]:
    manifest_row = validate_component_manifest(manifest)
    row = dict(_require_mapping(value, "event"))
    _require_exact_keys(
        row,
        {
            "schema",
            "event_id",
            "workflow_run_id",
            "flow_kind",
            "component_id",
            "component_run_id",
            "component_attempt_id",
            "component_seq",
            "ts",
            "stage_id",
            "agent",
            "event",
            "severity",
            "payload",
        },
        "event",
    )
    if row["schema"] != COMPONENT_EVENT_SCHEMA:
        raise D2LConsoleContractError("event.schema is invalid")
    _require_id(row["event_id"], "event.event_id")
    expected_event_id = f"evt_{manifest_row['component_run_id']}_{expected_component_seq:08d}"
    if row["event_id"] != expected_event_id:
        raise D2LConsoleContractError("event_id is not deterministic for component_seq")
    for key in ("workflow_run_id", "flow_kind", "component_id", "component_run_id"):
        if row[key] != manifest_row[key]:
            raise D2LConsoleContractError(f"event.{key} does not match manifest")
    _require_int(row["component_attempt_id"], "event.component_attempt_id", minimum=1)
    sequence = _require_int(row["component_seq"], "event.component_seq", minimum=1)
    if sequence != expected_component_seq:
        raise D2LConsoleContractError(
            f"component_seq gap: expected {expected_component_seq}, got {sequence}"
        )
    _validate_timestamp(
        row["ts"],
        allow_null=manifest_row["timing_authority"] == "logical_order_only",
        label="event.ts",
    )
    event_name = _require_string(row["event"], "event.event")
    if event_name not in EVENT_NAMES:
        raise D2LConsoleContractError(f"unsupported event: {event_name}")
    _require_string(row["agent"], "event.agent")
    if row["severity"] not in SEVERITIES:
        raise D2LConsoleContractError("event.severity is invalid")
    if event_name in RUN_LEVEL_EVENTS or event_name == "cost_snapshot":
        if row["stage_id"] is not None:
            raise D2LConsoleContractError(f"{event_name} requires stage_id=null when component-scoped")
    else:
        stage_id = _require_id(row["stage_id"], "event.stage_id")
        if stage_id not in {stage["stage_id"] for stage in manifest_row["stages"]}:
            raise D2LConsoleContractError("event.stage_id is unknown")
    payload = _validate_event_payload(event_name, row["payload"])
    if event_name == "run_start":
        if payload["selected_chapter_ids"] != manifest_row["selected_chapter_ids"]:
            raise D2LConsoleContractError("run_start chapter scope does not match manifest")
    if event_name == "checkpoint" and payload["stage_id"] != row["stage_id"]:
        raise D2LConsoleContractError("checkpoint stage does not match event stage")
    return row


def validate_component_event_stream(
    path: str | Path,
    *,
    manifest: Mapping[str, Any],
    require_terminal: bool = True,
) -> dict[str, Any]:
    manifest_row = validate_component_manifest(manifest)
    event_path = Path(path)
    if not event_path.is_file():
        raise D2LConsoleContractError("component event log is missing")
    counts: Counter[str] = Counter()
    seen_ids: set[str] = set()
    last_attempt = 0
    last_event: str | None = None
    last_seq = 0
    awaiting_resume = False
    terminal_seen = False
    checkpoint_events: dict[tuple[str, str], int] = {}
    with event_path.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            if not line.strip():
                raise D2LConsoleContractError(f"blank event line at {line_number}")
            try:
                raw = json.loads(line)
            except json.JSONDecodeError as exc:
                raise D2LConsoleContractError(f"invalid JSON at event line {line_number}") from exc
            if terminal_seen:
                raise D2LConsoleContractError("event appears after terminal component event")
            row = validate_component_event(
                raw,
                manifest=manifest_row,
                expected_component_seq=last_seq + 1,
            )
            if row["event_id"] in seen_ids:
                raise D2LConsoleContractError("duplicate event_id")
            event_name = row["event"]
            attempt = row["component_attempt_id"]
            if last_seq == 0:
                if event_name != "run_start" or attempt != 1:
                    raise D2LConsoleContractError("component stream must start with attempt-1 run_start")
            else:
                if event_name == "run_start":
                    raise D2LConsoleContractError("run_start may appear only once")
                if attempt < last_attempt or attempt > last_attempt + 1:
                    raise D2LConsoleContractError("component attempt progression is invalid")
                if attempt == last_attempt + 1:
                    awaiting_resume = True
                if awaiting_resume and event_name != "run_resumed":
                    raise D2LConsoleContractError("new component attempt must start with run_resumed")
                if event_name == "run_resumed":
                    if not awaiting_resume:
                        raise D2LConsoleContractError("run_resumed did not start a new component attempt")
                    if row["payload"]["previous_component_attempt_id"] != last_attempt:
                        raise D2LConsoleContractError("run_resumed previous attempt is incorrect")
                    checkpoint_key = (
                        row["payload"]["checkpoint_ref"],
                        row["payload"]["checkpoint_sha256"].upper(),
                    )
                    if checkpoint_events.get(checkpoint_key) != last_attempt:
                        raise D2LConsoleContractError(
                            "run_resumed checkpoint does not belong to the previous attempt"
                        )
                    awaiting_resume = False
            if event_name == "checkpoint":
                checkpoint_key = (
                    row["payload"]["checkpoint_ref"],
                    row["payload"]["checkpoint_sha256"].upper(),
                )
                if checkpoint_key in checkpoint_events:
                    raise D2LConsoleContractError("duplicate checkpoint lineage")
                checkpoint_events[checkpoint_key] = attempt
            if event_name in {"run_done", "run_failed"}:
                terminal_seen = True
            seen_ids.add(row["event_id"])
            counts[event_name] += 1
            last_seq = row["component_seq"]
            last_attempt = attempt
            last_event = event_name
    if last_seq == 0:
        raise D2LConsoleContractError("component event log cannot be empty")
    if terminal_seen:
        if last_attempt != manifest_row["component_attempt_id"]:
            raise D2LConsoleContractError("terminal stream attempt does not match manifest")
    elif last_attempt != manifest_row["component_attempt_id"]:
        raise D2LConsoleContractError("nonterminal stream attempt does not match manifest")
    terminal_count = counts["run_done"] + counts["run_failed"]
    if terminal_count > 1 or (require_terminal and terminal_count != 1):
        raise D2LConsoleContractError("component stream terminal event count is invalid")
    return {
        "schema": "d2l_translation_component_event_summary_v1",
        "workflow_run_id": manifest_row["workflow_run_id"],
        "component_run_id": manifest_row["component_run_id"],
        "component_attempt_id": last_attempt,
        "event_count": last_seq,
        "last_component_seq": last_seq,
        "terminal_event": last_event if last_event in {"run_done", "run_failed"} else None,
        "event_counts": dict(sorted(counts.items())),
    }


class D2LTranslationComponentEventWriter:
    """Append-only component writer; it never assigns parent workflow seq."""

    def __init__(
        self,
        path: str | Path,
        *,
        manifest: Mapping[str, Any],
        component_attempt_id: int,
    ) -> None:
        self.path = Path(path)
        self.manifest = validate_component_manifest(manifest)
        self.component_attempt_id = _require_int(
            component_attempt_id,
            "component_attempt_id",
            minimum=1,
        )
        if self.component_attempt_id != self.manifest["component_attempt_id"]:
            raise D2LConsoleContractError("writer attempt does not match current manifest")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._terminal = False
        self._requires_resume = False
        self._seq = self._load_existing()

    def _load_existing(self) -> int:
        if not self.path.exists() or self.path.stat().st_size == 0:
            if self.component_attempt_id != 1:
                raise D2LConsoleContractError("new component stream must start at attempt 1")
            return 0
        try:
            existing_lines = self.path.read_text(encoding="utf-8").splitlines()
            last_raw = json.loads(existing_lines[-1])
            if not isinstance(last_raw, Mapping):
                raise D2LConsoleContractError("existing final event must be an object")
            prefix_attempt = _require_int(
                last_raw.get("component_attempt_id"),
                "existing event component_attempt_id",
                minimum=1,
            )
        except (IndexError, json.JSONDecodeError) as exc:
            raise D2LConsoleContractError("existing component event stream is not valid JSON") from exc
        prefix_manifest = dict(self.manifest)
        prefix_manifest["component_attempt_id"] = prefix_attempt
        summary = validate_component_event_stream(
            self.path,
            manifest=prefix_manifest,
            require_terminal=False,
        )
        if summary["terminal_event"] is not None:
            self._terminal = True
            return int(summary["last_component_seq"])
        last_attempt = int(summary["component_attempt_id"])
        if self.component_attempt_id == last_attempt:
            raise D2LConsoleContractError(
                "opening an existing nonterminal stream requires a new component attempt"
            )
        if self.component_attempt_id != last_attempt + 1:
            raise D2LConsoleContractError("resume must increment component_attempt_id by one")
        if self.manifest["reconstructed"]:
            raise D2LConsoleContractError("reconstructed component cannot resume")
        self._requires_resume = True
        return int(summary["last_component_seq"])

    @property
    def component_seq(self) -> int:
        return self._seq

    @property
    def next_event_id(self) -> str:
        return f"evt_{self.manifest['component_run_id']}_{self._seq + 1:08d}"

    def emit(
        self,
        event: str,
        *,
        stage_id: str | None,
        agent: str,
        payload: Mapping[str, Any],
        severity: str = "info",
        ts: str | None = None,
    ) -> dict[str, Any]:
        if self._terminal:
            raise D2LConsoleContractError("cannot append after terminal event")
        if self._seq == 0 and event != "run_start":
            raise D2LConsoleContractError("first component event must be run_start")
        if self._requires_resume and event != "run_resumed":
            raise D2LConsoleContractError("resumed attempt must start with run_resumed")
        next_seq = self._seq + 1
        row = {
            "schema": COMPONENT_EVENT_SCHEMA,
            "event_id": self.next_event_id,
            "workflow_run_id": self.manifest["workflow_run_id"],
            "flow_kind": FLOW_KIND,
            "component_id": COMPONENT_ID,
            "component_run_id": self.manifest["component_run_id"],
            "component_attempt_id": self.component_attempt_id,
            "component_seq": next_seq,
            "ts": ts
            if ts is not None
            else (
                None
                if self.manifest["timing_authority"] == "logical_order_only"
                else datetime.now(UTC).isoformat().replace("+00:00", "Z")
            ),
            "stage_id": stage_id,
            "agent": agent,
            "event": event,
            "severity": severity,
            "payload": dict(payload),
        }
        validate_component_event(row, manifest=self.manifest, expected_component_seq=next_seq)
        encoded = (json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8")
        with self.path.open("ab") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        self._seq = next_seq
        self._requires_resume = False
        if event in {"run_done", "run_failed"}:
            self._terminal = True
        return row


def validate_artifact_index(
    value: Mapping[str, Any],
    *,
    manifest: Mapping[str, Any],
    artifact_root: str | Path | None = None,
) -> dict[str, Any]:
    manifest_row = validate_component_manifest(manifest)
    row = dict(_require_mapping(value, "artifact_index"))
    _require_exact_keys(
        row,
        {
            "schema",
            "workflow_run_id",
            "flow_kind",
            "component_id",
            "component_run_id",
            "component_attempt_id",
            "artifacts",
        },
        "artifact_index",
    )
    if row["schema"] != ARTIFACT_INDEX_SCHEMA:
        raise D2LConsoleContractError("artifact_index.schema is invalid")
    for key in (
        "workflow_run_id",
        "flow_kind",
        "component_id",
        "component_run_id",
        "component_attempt_id",
    ):
        if row[key] != manifest_row[key]:
            raise D2LConsoleContractError(f"artifact_index.{key} does not match manifest")
    artifacts = _require_list(row["artifacts"], "artifact_index.artifacts")
    refs: set[str] = set()
    for index, raw in enumerate(artifacts):
        label = f"artifact_index.artifacts[{index}]"
        artifact = dict(_require_mapping(raw, label))
        _require_exact_keys(
            artifact,
            {
                "workflow_run_id",
                "flow_kind",
                "component_id",
                "component_run_id",
                "component_attempt_id",
                "artifact_ref",
                "artifact_kind",
                "schema_version",
                "sha256",
                "sha256_kind",
                "producer_stage_id",
                "parent_artifact_refs",
                "created_event_id",
                "relative_path",
                "availability",
                "metadata",
            },
            label,
        )
        for key in ("workflow_run_id", "flow_kind", "component_id", "component_run_id"):
            if artifact[key] != manifest_row[key]:
                raise D2LConsoleContractError(f"{label}.{key} does not match manifest")
        producing_attempt = _require_int(
            artifact["component_attempt_id"],
            f"{label}.component_attempt_id",
            minimum=1,
        )
        if producing_attempt > manifest_row["component_attempt_id"]:
            raise D2LConsoleContractError(f"{label} belongs to a future component attempt")
        ref = _require_id(artifact["artifact_ref"], f"{label}.artifact_ref")
        if ref in refs:
            raise D2LConsoleContractError("duplicate artifact_ref")
        refs.add(ref)
        _require_string(artifact["artifact_kind"], f"{label}.artifact_kind")
        _require_string(artifact["schema_version"], f"{label}.schema_version")
        declared_sha = _require_sha(artifact["sha256"], f"{label}.sha256")
        if artifact["sha256_kind"] not in SHA256_KINDS:
            raise D2LConsoleContractError(f"{label}.sha256_kind is invalid")
        if artifact["producer_stage_id"] not in STAGE_IDS:
            raise D2LConsoleContractError(f"{label}.producer_stage_id is invalid")
        parent_refs = _validate_string_list(
            artifact["parent_artifact_refs"],
            f"{label}.parent_artifact_refs",
            nonempty=False,
        )
        for parent_ref in parent_refs:
            _require_id(parent_ref, f"{label}.parent_artifact_refs[]")
        _require_id(artifact["created_event_id"], f"{label}.created_event_id")
        relative_path = _validate_relative_ref(artifact["relative_path"], f"{label}.relative_path")
        if artifact["availability"] not in {"available", "unavailable"}:
            raise D2LConsoleContractError(f"{label}.availability is invalid")
        _require_mapping(artifact["metadata"], f"{label}.metadata")
        if artifact_root is not None and artifact["availability"] == "available":
            root = Path(artifact_root).resolve()
            candidate = (root / relative_path).resolve()
            if root not in candidate.parents:
                raise D2LConsoleContractError("artifact path escapes component root")
            if not candidate.is_file():
                raise D2LConsoleContractError(f"artifact is missing: {relative_path}")
            if artifact["sha256_kind"] == "physical":
                actual_sha = file_sha256(candidate)
            else:
                try:
                    actual_sha = canonical_sha256(json.loads(candidate.read_text(encoding="utf-8")))
                except json.JSONDecodeError as exc:
                    raise D2LConsoleContractError("canonical JSON artifact is invalid") from exc
            if actual_sha != declared_sha:
                raise D2LConsoleContractError(f"artifact hash drift: {relative_path}")
    for artifact in artifacts:
        for parent_ref in artifact["parent_artifact_refs"]:
            if parent_ref not in refs:
                raise D2LConsoleContractError("artifact parent ref is unknown")
    return row


def validate_checkpoint(
    value: Mapping[str, Any],
    *,
    manifest: Mapping[str, Any],
) -> dict[str, Any]:
    manifest_row = validate_component_manifest(manifest)
    row = dict(_require_mapping(value, "checkpoint"))
    _require_exact_keys(
        row,
        {
            "schema",
            "workflow_run_id",
            "flow_kind",
            "component_id",
            "component_run_id",
            "component_attempt_id",
            "checkpoint_ref",
            "stage_id",
            "work_id",
            "resume_available",
            "paused_reason",
            "created_at",
            "state",
            "state_sha256",
        },
        "checkpoint",
    )
    if row["schema"] != CHECKPOINT_SCHEMA:
        raise D2LConsoleContractError("checkpoint.schema is invalid")
    for key in ("workflow_run_id", "flow_kind", "component_id", "component_run_id"):
        if row[key] != manifest_row[key]:
            raise D2LConsoleContractError(f"checkpoint.{key} does not match manifest")
    attempt = _require_int(row["component_attempt_id"], "checkpoint.component_attempt_id", minimum=1)
    if attempt > manifest_row["component_attempt_id"]:
        raise D2LConsoleContractError("checkpoint belongs to a future component attempt")
    _validate_relative_ref(row["checkpoint_ref"], "checkpoint.checkpoint_ref")
    if row["stage_id"] not in STAGE_IDS:
        raise D2LConsoleContractError("checkpoint.stage_id is invalid")
    _require_id(row["work_id"], "checkpoint.work_id")
    available = _require_bool(row["resume_available"], "checkpoint.resume_available")
    if available:
        _require_string(row["paused_reason"], "checkpoint.paused_reason")
    elif row["paused_reason"] is not None:
        raise D2LConsoleContractError("non-resumable checkpoint cannot have paused_reason")
    _validate_timestamp(row["created_at"], allow_null=False, label="checkpoint.created_at")
    state = _require_mapping(row["state"], "checkpoint.state")
    if canonical_sha256(state) != _require_sha(row["state_sha256"], "checkpoint.state_sha256"):
        raise D2LConsoleContractError("checkpoint state hash drift")
    return row


def build_checkpoint(
    *,
    manifest: Mapping[str, Any],
    checkpoint_ref: str,
    stage_id: str,
    work_id: str,
    resume_available: bool,
    paused_reason: str | None,
    created_at: str,
    state: Mapping[str, Any],
) -> dict[str, Any]:
    manifest_row = validate_component_manifest(manifest)
    checkpoint = {
        "schema": CHECKPOINT_SCHEMA,
        "workflow_run_id": manifest_row["workflow_run_id"],
        "flow_kind": FLOW_KIND,
        "component_id": COMPONENT_ID,
        "component_run_id": manifest_row["component_run_id"],
        "component_attempt_id": manifest_row["component_attempt_id"],
        "checkpoint_ref": checkpoint_ref,
        "stage_id": stage_id,
        "work_id": work_id,
        "resume_available": resume_available,
        "paused_reason": paused_reason,
        "created_at": created_at,
        "state": dict(state),
        "state_sha256": canonical_sha256(state),
    }
    validate_checkpoint(checkpoint, manifest=manifest_row)
    return checkpoint


def _validate_coverage(value: Any, label: str) -> dict[str, Any]:
    row = dict(_require_mapping(value, label))
    _require_exact_keys(
        row,
        {
            "admitted_block_count",
            "translated_block_count",
            "preserved_block_count",
            "missing_block_count",
            "failed_block_count",
            "ordered_block_ids_sha256",
            "status",
        },
        label,
    )
    counts = {
        key: _require_int(row[key], f"{label}.{key}")
        for key in (
            "admitted_block_count",
            "translated_block_count",
            "preserved_block_count",
            "missing_block_count",
            "failed_block_count",
        )
    }
    covered = (
        counts["translated_block_count"]
        + counts["preserved_block_count"]
        + counts["missing_block_count"]
        + counts["failed_block_count"]
    )
    if covered != counts["admitted_block_count"]:
        raise D2LConsoleContractError(f"{label} does not exact-cover admitted blocks")
    _require_sha(row["ordered_block_ids_sha256"], f"{label}.ordered_block_ids_sha256")
    if row["status"] != "exact_cover":
        raise D2LConsoleContractError(f"{label}.status must be exact_cover")
    if counts["missing_block_count"] or counts["failed_block_count"]:
        raise D2LConsoleContractError(f"{label} cannot be scoring-ready with missing/failed blocks")
    return row


def scoring_fragment_sha256(value: Mapping[str, Any]) -> str:
    payload = dict(value)
    payload.pop("fragment_sha256", None)
    return canonical_sha256(payload)


def validate_scoring_handoff_fragment(value: Mapping[str, Any]) -> dict[str, Any]:
    row = dict(_require_mapping(value, "scoring_fragment"))
    _require_exact_keys(
        row,
        {
            "schema",
            "fragment_sha256",
            "artifact_ref",
            "workflow_run_id",
            "flow_kind",
            "component_id",
            "translation_component_run_id",
            "translation_component_attempt_id",
            "reserved_evaluation_component_run_id",
            "source_binding",
            "source_binding_sha256",
            "translation_inputs",
            "glossary_binding",
            "context_memory_binding",
            "admitted_projection_binding",
            "selected_chapter_ids",
            "admitted_universe",
            "producer_lineage",
            "status",
            "created_at",
        },
        "scoring_fragment",
    )
    if row["schema"] != SCORING_FRAGMENT_SCHEMA:
        raise D2LConsoleContractError("scoring fragment schema is invalid")
    declared_fragment_sha = _require_sha(row["fragment_sha256"], "scoring_fragment.fragment_sha256")
    if scoring_fragment_sha256(row) != declared_fragment_sha:
        raise D2LConsoleContractError("scoring fragment self-hash drift")
    _require_id(row["artifact_ref"], "scoring_fragment.artifact_ref")
    _require_id(row["workflow_run_id"], "scoring_fragment.workflow_run_id")
    if row["flow_kind"] != FLOW_KIND or row["component_id"] != COMPONENT_ID:
        raise D2LConsoleContractError("scoring fragment flow/component identity is invalid")
    _require_id(row["translation_component_run_id"], "scoring_fragment.translation_component_run_id")
    _require_int(
        row["translation_component_attempt_id"],
        "scoring_fragment.translation_component_attempt_id",
        minimum=1,
    )
    _require_id(
        row["reserved_evaluation_component_run_id"],
        "scoring_fragment.reserved_evaluation_component_run_id",
    )
    source_binding = validate_source_binding(row["source_binding"], "scoring_fragment.source_binding")
    source_binding_sha = _require_sha(
        row["source_binding_sha256"],
        "scoring_fragment.source_binding_sha256",
    )
    if canonical_sha256(source_binding) != source_binding_sha:
        raise D2LConsoleContractError("source binding hash drift")
    chapters = _validate_string_list(row["selected_chapter_ids"], "scoring_fragment.selected_chapter_ids")
    admitted = dict(_require_mapping(row["admitted_universe"], "scoring_fragment.admitted_universe"))
    _require_exact_keys(
        admitted,
        {"ordered_block_ids_sha256", "block_count", "status"},
        "scoring_fragment.admitted_universe",
    )
    universe_sha = _require_sha(
        admitted["ordered_block_ids_sha256"],
        "scoring_fragment.admitted_universe.ordered_block_ids_sha256",
    )
    universe_count = _require_int(
        admitted["block_count"],
        "scoring_fragment.admitted_universe.block_count",
    )
    if admitted["status"] != "exact_cover":
        raise D2LConsoleContractError("admitted universe must be exact_cover")
    inputs = _require_list(row["translation_inputs"], "scoring_fragment.translation_inputs")
    if [item.get("arm_id") if isinstance(item, Mapping) else None for item in inputs] != ["s0", "s1"]:
        raise D2LConsoleContractError("D2L fragment must contain exactly ordered s0 and s1 inputs")
    for index, raw in enumerate(inputs):
        label = f"scoring_fragment.translation_inputs[{index}]"
        item = dict(_require_mapping(raw, label))
        _require_exact_keys(
            item,
            {
                "arm_id",
                "artifact",
                "producer_component_run_id",
                "producer_component_attempt_id",
                "profile_id",
                "profile_sha256",
                "config_sha256",
                "selected_chapter_ids",
                "coverage",
                "source_binding_sha256",
            },
            label,
        )
        artifact = validate_typed_binding(item["artifact"], f"{label}.artifact")
        if artifact["artifact_kind"] != "translation_artifact":
            raise D2LConsoleContractError(f"{label}.artifact must be a translation artifact")
        if item["producer_component_run_id"] != row["translation_component_run_id"]:
            raise D2LConsoleContractError(f"{label} producer run mismatch")
        producer_attempt = _require_int(
            item["producer_component_attempt_id"],
            f"{label}.producer_component_attempt_id",
            minimum=1,
        )
        if producer_attempt > row["translation_component_attempt_id"]:
            raise D2LConsoleContractError(f"{label} belongs to a future producer attempt")
        _require_id(item["profile_id"], f"{label}.profile_id")
        _require_sha(item["profile_sha256"], f"{label}.profile_sha256")
        _require_sha(item["config_sha256"], f"{label}.config_sha256")
        if item["selected_chapter_ids"] != chapters:
            raise D2LConsoleContractError(f"{label} chapter scope mismatch")
        coverage = _validate_coverage(item["coverage"], f"{label}.coverage")
        if coverage["ordered_block_ids_sha256"] != universe_sha:
            raise D2LConsoleContractError(f"{label} ordered universe hash mismatch")
        if coverage["admitted_block_count"] != universe_count:
            raise D2LConsoleContractError(f"{label} admitted universe count mismatch")
        if item["source_binding_sha256"] != source_binding_sha:
            raise D2LConsoleContractError(f"{label} source binding hash mismatch")
    validate_typed_binding(row["glossary_binding"], "scoring_fragment.glossary_binding", allow_none=True)
    validate_typed_binding(
        row["context_memory_binding"],
        "scoring_fragment.context_memory_binding",
        allow_none=True,
    )
    admitted_binding = validate_typed_binding(
        row["admitted_projection_binding"],
        "scoring_fragment.admitted_projection_binding",
    )
    if admitted_binding != source_binding["admitted_projection"]:
        raise D2LConsoleContractError("admitted projection binding disagrees with source binding")
    lineage = dict(_require_mapping(row["producer_lineage"], "scoring_fragment.producer_lineage"))
    _require_exact_keys(
        lineage,
        {"git_commit", "pipeline_version", "config_sha256", "code_sha256"},
        "scoring_fragment.producer_lineage",
    )
    if not _GIT_RE.fullmatch(_require_string(lineage["git_commit"], "producer_lineage.git_commit")):
        raise D2LConsoleContractError("producer_lineage.git_commit must be a full Git commit")
    _require_id(lineage["pipeline_version"], "producer_lineage.pipeline_version")
    _require_sha(lineage["config_sha256"], "producer_lineage.config_sha256")
    _require_sha(lineage["code_sha256"], "producer_lineage.code_sha256")
    if row["status"] != "translation_component_ready":
        raise D2LConsoleContractError("scoring fragment status is invalid")
    _validate_timestamp(row["created_at"], allow_null=False, label="scoring_fragment.created_at")
    return row


def build_scoring_handoff_fragment(
    *,
    workflow_run_id: str,
    translation_component_run_id: str,
    translation_component_attempt_id: int,
    reserved_evaluation_component_run_id: str,
    artifact_ref: str,
    source_binding: Mapping[str, Any],
    translation_inputs: Sequence[Mapping[str, Any]],
    glossary_binding: Mapping[str, Any] | None,
    context_memory_binding: Mapping[str, Any] | None,
    selected_chapter_ids: Sequence[str],
    admitted_universe: Mapping[str, Any],
    producer_lineage: Mapping[str, Any],
    created_at: str,
) -> dict[str, Any]:
    source = dict(source_binding)
    fragment: dict[str, Any] = {
        "schema": SCORING_FRAGMENT_SCHEMA,
        "fragment_sha256": "0" * 64,
        "artifact_ref": artifact_ref,
        "workflow_run_id": workflow_run_id,
        "flow_kind": FLOW_KIND,
        "component_id": COMPONENT_ID,
        "translation_component_run_id": translation_component_run_id,
        "translation_component_attempt_id": translation_component_attempt_id,
        "reserved_evaluation_component_run_id": reserved_evaluation_component_run_id,
        "source_binding": source,
        "source_binding_sha256": canonical_sha256(source),
        "translation_inputs": [dict(item) for item in translation_inputs],
        "glossary_binding": None if glossary_binding is None else dict(glossary_binding),
        "context_memory_binding": None
        if context_memory_binding is None
        else dict(context_memory_binding),
        "admitted_projection_binding": dict(source["admitted_projection"]),
        "selected_chapter_ids": list(selected_chapter_ids),
        "admitted_universe": dict(admitted_universe),
        "producer_lineage": dict(producer_lineage),
        "status": "translation_component_ready",
        "created_at": created_at,
    }
    fragment["fragment_sha256"] = scoring_fragment_sha256(fragment)
    validate_scoring_handoff_fragment(fragment)
    return fragment


def build_stage_plan() -> list[dict[str, Any]]:
    units = {
        "preflight": "checks",
        "b1_candidate_discovery": "windows",
        "candidate_index": "candidates",
        "b2_admission_translation": "packets",
        "auditor_morphology": "components",
        "auditor_target_collision": "components",
        "auditor_multi_target": "components",
        "glossary_seal": "entries",
        "translator": "blocks",
        "translation_quality_audit": "windows",
        "scoring_handoff_fragment": "artifacts",
    }
    labels = {
        "preflight": "Preflight",
        "b1_candidate_discovery": "B1 Candidate Discovery",
        "candidate_index": "Candidate Index",
        "b2_admission_translation": "B2 Admission and Translation",
        "auditor_morphology": "Morphology Auditor",
        "auditor_target_collision": "Target Collision Auditor",
        "auditor_multi_target": "Multi-target Auditor",
        "glossary_seal": "Glossary Seal",
        "translator": "Translator",
        "translation_quality_audit": "Translation Quality Audit",
        "scoring_handoff_fragment": "Scoring Handoff Fragment",
    }
    return [
        {
            "stage_id": stage_id,
            "order": order,
            "label": labels[stage_id],
            "producer": stage_id,
            "component_id": COMPONENT_ID,
            "status": "pending",
            "started_at": None,
            "ended_at": None,
            "progress": {"completed": 0, "total": None, "unit": units[stage_id]},
            "current_work_id": None,
            "artifact_refs": [],
        }
        for order, stage_id in enumerate(STAGE_IDS, start=1)
    ]


def build_component_manifest(
    *,
    workflow_run_id: str,
    component_run_id: str,
    component_attempt_id: int,
    pipeline_id: str,
    pipeline_version: str,
    source_binding: Mapping[str, Any],
    config_sha256: str,
    code_revision: str,
    selected_chapter_ids: Sequence[str],
    started_at: str | None,
    updated_at: str | None,
    status: str = "planned",
    active_stage_id: str | None = None,
    stages: Sequence[Mapping[str, Any]] | None = None,
    scoring_handoff_fragment_ref: str | None = None,
    resume: Mapping[str, Any] | None = None,
    reconstructed: bool = False,
    lineage_kind: str = "origin",
    parent_component_run_id: str | None = None,
) -> dict[str, Any]:
    manifest = {
        "schema": COMPONENT_MANIFEST_SCHEMA,
        "workflow_run_id": workflow_run_id,
        "flow_kind": FLOW_KIND,
        "pipeline_id": pipeline_id,
        "pipeline_version": pipeline_version,
        "component_id": COMPONENT_ID,
        "component_run_id": component_run_id,
        "component_attempt_id": component_attempt_id,
        "status": status,
        "started_at": started_at,
        "updated_at": updated_at,
        "active_stage_id": active_stage_id,
        "selected_chapter_ids": list(selected_chapter_ids),
        "source_binding": dict(source_binding),
        "config_sha256": config_sha256,
        "code_revision": code_revision,
        "stages": [dict(stage) for stage in (stages or build_stage_plan())],
        "event_log_ref": "events.jsonl",
        "artifact_index_ref": "artifact_index.json",
        "scoring_handoff_fragment_ref": scoring_handoff_fragment_ref,
        "resume": dict(
            resume
            or {
                "resume_available": False,
                "checkpoint_ref": None,
                "checkpoint_sha256": None,
                "stage_id": None,
                "work_id": None,
                "paused_reason": None,
            }
        ),
        "reconstructed": reconstructed,
        "timing_authority": "logical_order_only" if reconstructed else "recorded",
        "lineage": {
            "kind": lineage_kind,
            "parent_component_run_id": parent_component_run_id,
        },
    }
    validate_component_manifest(manifest)
    return manifest


def _load_json(path: Path, label: str) -> Mapping[str, Any]:
    try:
        return _require_mapping(json.loads(path.read_text(encoding="utf-8")), label)
    except FileNotFoundError as exc:
        raise D2LConsoleContractError(f"{label} is missing") from exc
    except json.JSONDecodeError as exc:
        raise D2LConsoleContractError(f"{label} is not valid JSON") from exc


def _resolve_ref(root: Path, value: Any, label: str) -> Path:
    relative = _validate_relative_ref(value, label)
    candidate = (root / relative).resolve()
    if root not in candidate.parents:
        raise D2LConsoleContractError(f"{label} escapes component root")
    return candidate


def write_component_manifest_snapshot(
    root: str | Path,
    manifest: Mapping[str, Any],
) -> dict[str, str]:
    """Persist an immutable revision, then atomically advance the current snapshot."""

    package_root = Path(root)
    row = validate_component_manifest(manifest)
    encoded = canonical_json_bytes(row)
    revision_sha = sha256(encoded).hexdigest().upper()
    relative_ref = f"manifest_revisions/{revision_sha}.json"
    revision_path = package_root / relative_ref
    if revision_path.exists() and revision_path.read_bytes() != encoded:
        raise D2LConsoleContractError("manifest revision ID was reused with unequal bytes")
    if not revision_path.exists():
        revision_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = revision_path.with_name(revision_path.name + ".tmp")
        temporary.write_bytes(encoded)
        os.replace(temporary, revision_path)
    write_json(package_root / "component_manifest.json", row)
    return {"manifest_ref": relative_ref, "manifest_sha256": revision_sha}


def _require_binding_matches_index(
    binding: Mapping[str, Any],
    *,
    artifacts_by_ref: Mapping[str, Mapping[str, Any]],
    label: str,
    producer_attempt_id: int | None = None,
) -> None:
    indexed = artifacts_by_ref.get(str(binding["artifact_ref"]))
    if indexed is None:
        raise D2LConsoleContractError(f"{label} is absent from the component artifact index")
    if indexed["availability"] != "available":
        raise D2LConsoleContractError(f"{label} is not available for handoff")
    for key in ("artifact_kind", "schema_version", "sha256", "sha256_kind"):
        if binding[key] != indexed[key]:
            raise D2LConsoleContractError(f"{label}.{key} disagrees with the component artifact index")
    if producer_attempt_id is not None and indexed["component_attempt_id"] != producer_attempt_id:
        raise D2LConsoleContractError(f"{label} producer attempt disagrees with the artifact index")


def validate_translation_component_package(
    root: str | Path,
    *,
    require_terminal: bool = True,
) -> dict[str, Any]:
    package_root = Path(root).resolve()
    if (package_root / "workflow_manifest.json").exists():
        raise D2LConsoleContractError("D2L component must not publish workflow_manifest.json")
    if (package_root / "scoring_handoff.json").exists():
        raise D2LConsoleContractError("D2L component must not publish final scoring_handoff.json")
    manifest_path = package_root / "component_manifest.json"
    manifest = validate_component_manifest(_load_json(manifest_path, "component_manifest"))
    current_manifest_sha = file_sha256(manifest_path)
    current_revision_path = package_root / "manifest_revisions" / f"{current_manifest_sha}.json"
    if not current_revision_path.is_file() or current_revision_path.read_bytes() != manifest_path.read_bytes():
        raise D2LConsoleContractError("current component manifest has no immutable matching revision")
    event_path = _resolve_ref(package_root, manifest["event_log_ref"], "manifest.event_log_ref")
    event_summary = validate_component_event_stream(
        event_path,
        manifest=manifest,
        require_terminal=require_terminal,
    )
    index_path = _resolve_ref(package_root, manifest["artifact_index_ref"], "manifest.artifact_index_ref")
    artifact_index = validate_artifact_index(
        _load_json(index_path, "artifact_index"),
        manifest=manifest,
        artifact_root=package_root,
    )
    artifacts_by_ref = {item["artifact_ref"]: item for item in artifact_index["artifacts"]}
    events_by_id: dict[str, Mapping[str, Any]] = {}
    stage_start_counts: Counter[str] = Counter()
    stage_done_events: dict[str, Mapping[str, Any]] = {}
    checkpoints = 0
    with event_path.open("r", encoding="utf-8") as handle:
        for line in handle:
            event = json.loads(line)
            events_by_id[event["event_id"]] = event
            payload = event["payload"]
            if event["event"] == "run_start":
                revision_path = _resolve_ref(
                    package_root,
                    payload["manifest_ref"],
                    "run_start.manifest_ref",
                )
                if not revision_path.is_file() or file_sha256(revision_path) != payload["manifest_sha256"]:
                    raise D2LConsoleContractError("run_start manifest revision hash drift")
                revision = validate_component_manifest(_load_json(revision_path, "manifest_revision"))
                if revision["component_attempt_id"] != 1:
                    raise D2LConsoleContractError("run_start manifest revision must be component attempt 1")
                for key in _IMMUTABLE_MANIFEST_KEYS:
                    if revision[key] != manifest[key]:
                        raise D2LConsoleContractError(
                            f"run_start immutable manifest field drifted: {key}"
                        )
                revision_stages = [
                    (
                        row["stage_id"],
                        row["order"],
                        row["label"],
                        row["producer"],
                        row["component_id"],
                        row["progress"]["unit"],
                    )
                    for row in revision["stages"]
                ]
                current_stages = [
                    (
                        row["stage_id"],
                        row["order"],
                        row["label"],
                        row["producer"],
                        row["component_id"],
                        row["progress"]["unit"],
                    )
                    for row in manifest["stages"]
                ]
                if revision_stages != current_stages:
                    raise D2LConsoleContractError("component stage definition drifted after run_start")
            elif event["event"] == "stage_start":
                stage_start_counts[str(event["stage_id"])] += 1
            elif event["event"] == "stage_done":
                stage_id = str(event["stage_id"])
                if stage_id in stage_done_events:
                    raise D2LConsoleContractError("stage has more than one stage_done event")
                stage_done_events[stage_id] = event
            elif event["event"] == "artifact_created":
                artifact = artifacts_by_ref.get(payload["artifact_ref"])
                if artifact is None:
                    raise D2LConsoleContractError("artifact_created refers to an unknown artifact")
                if artifact["created_event_id"] != event["event_id"]:
                    raise D2LConsoleContractError("artifact created_event_id mismatch")
                if artifact["component_attempt_id"] != event["component_attempt_id"]:
                    raise D2LConsoleContractError("artifact producing attempt mismatch")
                if artifact["producer_stage_id"] != event["stage_id"]:
                    raise D2LConsoleContractError("artifact producer stage mismatch")
                for event_key, artifact_key in (
                    ("artifact_kind", "artifact_kind"),
                    ("schema_version", "schema_version"),
                    ("sha256", "sha256"),
                    ("sha256_kind", "sha256_kind"),
                    ("parent_artifact_refs", "parent_artifact_refs"),
                ):
                    if payload[event_key] != artifact[artifact_key]:
                        raise D2LConsoleContractError(f"artifact_created {event_key} mismatch")
            elif event["event"] in {"checkpoint", "run_resumed"}:
                checkpoint_path = _resolve_ref(
                    package_root,
                    payload["checkpoint_ref"],
                    f"{event['event']}.checkpoint_ref",
                )
                if not checkpoint_path.is_file() or file_sha256(checkpoint_path) != payload["checkpoint_sha256"]:
                    raise D2LConsoleContractError("checkpoint physical hash drift")
                checkpoint = validate_checkpoint(
                    _load_json(checkpoint_path, "checkpoint"),
                    manifest=manifest,
                )
                expected_attempt = (
                    event["payload"]["previous_component_attempt_id"]
                    if event["event"] == "run_resumed"
                    else event["component_attempt_id"]
                )
                if checkpoint["component_attempt_id"] != expected_attempt:
                    raise D2LConsoleContractError("checkpoint producing attempt mismatch")
                if event["event"] == "checkpoint":
                    for key in ("checkpoint_ref", "stage_id", "work_id", "resume_available", "paused_reason"):
                        if checkpoint[key] != payload[key]:
                            raise D2LConsoleContractError(f"checkpoint event {key} mismatch")
                checkpoints += 1
    for artifact in artifact_index["artifacts"]:
        if artifact["created_event_id"] not in events_by_id:
            raise D2LConsoleContractError("artifact index cites an unknown creation event")
    scoring_fragment_sha: str | None = None
    if manifest["scoring_handoff_fragment_ref"] is not None:
        fragment_path = _resolve_ref(
            package_root,
            manifest["scoring_handoff_fragment_ref"],
            "manifest.scoring_handoff_fragment_ref",
        )
        fragment = validate_scoring_handoff_fragment(_load_json(fragment_path, "scoring_fragment"))
        scoring_fragment_sha = file_sha256(fragment_path)
        if fragment["workflow_run_id"] != manifest["workflow_run_id"]:
            raise D2LConsoleContractError("scoring fragment workflow identity mismatch")
        if fragment["translation_component_run_id"] != manifest["component_run_id"]:
            raise D2LConsoleContractError("scoring fragment component run mismatch")
        if fragment["translation_component_attempt_id"] != manifest["component_attempt_id"]:
            raise D2LConsoleContractError("scoring fragment component attempt mismatch")
        if fragment["source_binding"] != manifest["source_binding"]:
            raise D2LConsoleContractError("scoring fragment source binding mismatch")
        if fragment["selected_chapter_ids"] != manifest["selected_chapter_ids"]:
            raise D2LConsoleContractError("scoring fragment chapter scope mismatch")
        for input_row in fragment["translation_inputs"]:
            _require_binding_matches_index(
                input_row["artifact"],
                artifacts_by_ref=artifacts_by_ref,
                label=f"translation input {input_row['arm_id']}",
                producer_attempt_id=input_row["producer_component_attempt_id"],
            )
        for binding_name in ("glossary_binding", "context_memory_binding"):
            binding = fragment[binding_name]
            if binding is not None:
                _require_binding_matches_index(
                    binding,
                    artifacts_by_ref=artifacts_by_ref,
                    label=binding_name,
                )
        indexed = artifacts_by_ref.get(fragment["artifact_ref"])
        if indexed is None or indexed["sha256_kind"] != "physical" or indexed["sha256"] != scoring_fragment_sha:
            raise D2LConsoleContractError("scoring fragment is not physically bound in artifact index")
    if event_summary["terminal_event"] == "run_done":
        if manifest["status"] != "succeeded":
            raise D2LConsoleContractError("run_done requires succeeded manifest")
        terminal = events_by_id[f"evt_{manifest['component_run_id']}_{event_summary['last_component_seq']:08d}"]
        if terminal["payload"]["artifact_index_sha256"] != file_sha256(index_path):
            raise D2LConsoleContractError("run_done artifact-index hash drift")
        if terminal["payload"]["scoring_handoff_fragment_sha256"] != scoring_fragment_sha:
            raise D2LConsoleContractError("run_done scoring-fragment hash drift")
        expected_outcomes = {
            "succeeded": "succeeded",
            "skipped": "skipped",
            "reused": "reused",
        }
        for stage in manifest["stages"]:
            stage_id = stage["stage_id"]
            if stage["status"] not in expected_outcomes:
                raise D2LConsoleContractError("run_done requires every stage to be complete")
            if stage_start_counts[stage_id] != 1 or stage_id not in stage_done_events:
                raise D2LConsoleContractError("run_done requires one start and one done event per stage")
            if stage_done_events[stage_id]["payload"]["outcome"] != expected_outcomes[stage["status"]]:
                raise D2LConsoleContractError("stage manifest/event outcome mismatch")
            indexed_refs = {
                row["artifact_ref"]
                for row in artifact_index["artifacts"]
                if row["producer_stage_id"] == stage_id
            }
            if set(stage["artifact_refs"]) != indexed_refs:
                raise D2LConsoleContractError("stage artifact refs disagree with artifact index")
    elif event_summary["terminal_event"] == "run_failed":
        if manifest["status"] not in {"failed", "cancelled"}:
            raise D2LConsoleContractError("run_failed requires failed or cancelled manifest")
    return {
        "schema": "d2l_translation_component_package_validation_v1",
        "workflow_run_id": manifest["workflow_run_id"],
        "component_run_id": manifest["component_run_id"],
        "component_attempt_id": manifest["component_attempt_id"],
        "event_count": event_summary["event_count"],
        "artifact_count": len(artifact_index["artifacts"]),
        "checkpoint_reference_count": checkpoints,
        "terminal_event": event_summary["terminal_event"],
        "component_manifest_sha256": file_sha256(manifest_path),
        "artifact_index_sha256": file_sha256(index_path),
        "scoring_handoff_fragment_sha256": scoring_fragment_sha,
    }


def write_json(path: str | Path, value: Any) -> None:
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(destination.name + ".tmp")
    temporary.write_bytes(canonical_json_bytes(value))
    os.replace(temporary, destination)


__all__ = [
    "ARTIFACT_INDEX_SCHEMA",
    "CHECKPOINT_SCHEMA",
    "COMPONENT_EVENT_SCHEMA",
    "COMPONENT_ID",
    "COMPONENT_MANIFEST_SCHEMA",
    "D2LConsoleContractError",
    "D2LTranslationComponentEventWriter",
    "FLOW_KIND",
    "SCORING_FRAGMENT_SCHEMA",
    "SOURCE_BINDING_SCHEMA",
    "STAGE_IDS",
    "build_checkpoint",
    "build_component_manifest",
    "build_scoring_handoff_fragment",
    "build_stage_plan",
    "canonical_json_bytes",
    "canonical_sha256",
    "component_manifest_sha256",
    "file_sha256",
    "scoring_fragment_sha256",
    "validate_artifact_index",
    "validate_checkpoint",
    "validate_component_event",
    "validate_component_event_stream",
    "validate_component_manifest",
    "validate_scoring_handoff_fragment",
    "validate_source_binding",
    "validate_translation_component_package",
    "validate_typed_binding",
    "write_component_manifest_snapshot",
    "write_json",
]
