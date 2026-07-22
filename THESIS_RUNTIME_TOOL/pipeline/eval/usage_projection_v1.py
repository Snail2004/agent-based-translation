from __future__ import annotations

import copy
from dataclasses import dataclass
import json
import os
from pathlib import Path
from typing import Any, Mapping, Sequence
import uuid

from pipeline.eval.common_input_v1 import CommonEvaluationInputV1
from pipeline.eval.contracts_v1 import (
    CanonicalPolicy,
    ContractValidationError,
    canonical_sha256,
    canonicalize,
    require_commit,
    require_enum,
    require_exact_keys,
    require_int,
    require_list,
    require_mapping,
    require_nullable_int,
    require_nullable_number,
    require_nullable_string,
    require_relative_path,
    require_rfc3339,
    require_sha256,
    require_string,
    require_unique,
    seal_payload,
    validate_producer,
    verify_payload_hash,
)
from pipeline.eval.execution_runner_v1 import (
    validate_evaluation_execution_artifact,
    validate_evaluation_execution_binding,
)
from pipeline.eval.llm_profiles_v1 import (
    PJ_JUDGE_ROLE_ID,
    SF_BT_BACK_TRANSLATOR_ROLE_ID,
    SF_BT_SEMANTIC_JUDGE_ROLE_ID,
)
from pipeline.eval.local_sf_qe_v1 import validate_local_sf_qe_evidence_v1
from pipeline.eval.offline_orchestrator_v1 import (
    build_evaluation_plan,
    validate_evaluation_run_config,
)
from pipeline.llm_backend import SharedLlmAttemptLedger
from pipeline.llm_backend.contracts_v1 import canonical_sha256 as shared_canonical_sha256
from pipeline.llm_backend.resolver_v1 import validate_llm_run_records


__all__ = [
    "EVALUATION_USAGE_SCHEMA_ID",
    "EVALUATION_USAGE_SCHEMA_VERSION",
    "EvaluationUsageProjectionV1",
    "load_evaluation_usage_artifact_v1",
    "persist_evaluation_usage_artifact_v1",
    "project_evaluation_usage_v1",
    "seal_evaluation_usage_artifact_v1",
    "validate_evaluation_usage_artifact_v1",
]


EVALUATION_USAGE_SCHEMA_ID = "EvaluationUsageArtifactV1"
EVALUATION_USAGE_SCHEMA_VERSION = "1.0.0"
_SELF_HASH_PATH = ("integrity", "artifact_sha256")
_POLICY = CanonicalPolicy(
    set_like_paths=frozenset({("source_records",), ("usage", "source_artifact_ids")}),
    semantic_sequence_paths=frozenset(
        {("stage_facts",), ("usage", "by_stage"), ("usage", "notes")}
    ),
)
_USAGE_NUMERIC_FIELDS = (
    "request_count",
    "successful_request_count",
    "failed_request_count",
    "input_tokens",
    "cached_input_tokens",
    "output_tokens",
    "reasoning_tokens",
    "thought_tokens",
    "total_tokens",
)
_ROLE_STAGE = {
    SF_BT_BACK_TRANSLATOR_ROLE_ID: ("sf_bt.back_translation", "sf_bt"),
    SF_BT_SEMANTIC_JUDGE_ROLE_ID: ("sf_bt.semantic_judge", "sf_bt"),
    PJ_JUDGE_ROLE_ID: ("pj.judge", "pj"),
}


@dataclass(frozen=True, slots=True)
class EvaluationUsageProjectionV1:
    artifact: dict[str, Any]
    stage_facts: tuple[dict[str, Any], ...]
    usage: dict[str, Any]
    artifact_descriptor: dict[str, str]


@dataclass(frozen=True, slots=True)
class EvaluationUsagePersistResultV1:
    path: Path
    artifact: dict[str, Any]
    reused: bool


def project_evaluation_usage_v1(
    common_input: CommonEvaluationInputV1,
    config_payload: Mapping[str, Any],
    execution_payload: Mapping[str, Any],
    *,
    created_at: str,
    producer_code_commit: str,
    evaluation_logical_run_id: str,
    evaluation_attempt_run_id: str,
    local_sf_qe_evidence: Mapping[str, Any] | None = None,
    local_sf_qe_relative_path: str | None = None,
    shared_ledger: SharedLlmAttemptLedger | None = None,
    shared_ledger_relative_path: str | None = None,
) -> EvaluationUsageProjectionV1:
    config = validate_evaluation_run_config(config_payload)
    execution = validate_evaluation_execution_artifact(execution_payload)
    plan = build_evaluation_plan(common_input, config)
    validate_evaluation_execution_binding(execution, common_input, plan)
    timestamp = require_rfc3339(created_at, path="$.created_at")
    commit = require_commit(producer_code_commit, path="$.producer_code_commit")
    logical_run_id = require_string(
        evaluation_logical_run_id, path="$.evaluation_logical_run_id"
    )
    attempt_run_id = require_string(
        evaluation_attempt_run_id, path="$.evaluation_attempt_run_id"
    )
    method_ids = [row["method_id"] for row in config["methods"]]

    stage_rows: list[dict[str, Any]] = []
    usage_rows: list[dict[str, Any]] = []
    source_records: list[dict[str, str]] = []
    notes: list[str] = []

    if "sf_qe" in method_ids:
        if local_sf_qe_evidence is None or local_sf_qe_relative_path is None:
            stage_rows.append(_unavailable_stage("sf_qe.local_scorer", "sf_qe"))
            usage_rows.append(_unavailable_usage_stage("sf_qe.local_scorer"))
            notes.append("Local SF-QE metering was not supplied.")
        else:
            local = validate_local_sf_qe_evidence_v1(local_sf_qe_evidence)
            _validate_local_binding(local, execution, plan)
            relative = require_relative_path(
                local_sf_qe_relative_path, path="$.local_sf_qe_relative_path"
            )
            stage_rows.append(_local_stage_fact(local, attempt_run_id))
            usage_rows.append(_local_usage_stage(local))
            source_records.append(
                {
                    "kind": "local_sf_qe_evidence",
                    "record_id": local["artifact_id"],
                    "record_sha256": local["integrity"]["artifact_sha256"],
                    "relative_path": relative,
                }
            )

    shared_records = _load_shared_records(
        shared_ledger,
        run_id=logical_run_id,
        attempt_run_id=attempt_run_id,
    )
    if shared_ledger is not None:
        if shared_ledger_relative_path is None:
            raise ContractValidationError(
                "usage_ledger_path",
                "$.shared_ledger_relative_path",
                "a supplied shared ledger needs its persisted relative path",
            )
        shared_relative = require_relative_path(
            shared_ledger_relative_path, path="$.shared_ledger_relative_path"
        )
    else:
        shared_relative = None

    unknown_roles = sorted(
        {row["role_id"] for row in shared_records["seals"]} - set(_ROLE_STAGE)
    )
    if unknown_roles:
        raise ContractValidationError(
            "usage_role",
            "$.shared_ledger",
            "selected Evaluation attempt contains unsupported roles: "
            + ", ".join(unknown_roles),
        )
    extra_roles = sorted(
        {
            row["role_id"]
            for row in shared_records["seals"]
            if _ROLE_STAGE[row["role_id"]][1] not in method_ids
        }
    )
    if extra_roles:
        raise ContractValidationError(
            "usage_method_binding",
            "$.shared_ledger",
            "attempt ledger contains roles absent from the sealed config: "
            + ", ".join(extra_roles),
        )

    for role_id, (stage_id, method_id) in _ROLE_STAGE.items():
        if method_id not in method_ids:
            continue
        seals = [row for row in shared_records["seals"] if row["role_id"] == role_id]
        if not seals:
            if method_id == "pj" and _all_method_jobs_succeeded(execution, method_id):
                stage_rows.append(_not_applicable_stage(stage_id, method_id))
                usage_rows.append(_zero_usage_stage(stage_id))
            else:
                stage_rows.append(_unavailable_stage(stage_id, method_id))
                usage_rows.append(_unavailable_usage_stage(stage_id))
            continue
        stage_usage, stage_sources = _project_shared_stage(
            stage_id=stage_id,
            method_id=method_id,
            seals=seals,
            shared_records=shared_records,
            execution=execution,
            attempt_run_id=attempt_run_id,
            relative_path=shared_relative,
        )
        stage_rows.append(stage_usage[0])
        usage_rows.append(stage_usage[1])
        source_records.extend(stage_sources)

    if not stage_rows:
        raise ContractValidationError(
            "usage_stages", "$.methods", "evaluation config has no supported scoring stages"
        )
    usage = _compose_usage(usage_rows, notes=notes)
    source_records = _deduplicate_source_records(source_records)
    draft = {
        "schema_id": EVALUATION_USAGE_SCHEMA_ID,
        "schema_version": EVALUATION_USAGE_SCHEMA_VERSION,
        "artifact_id": "pending",
        "created_at": timestamp,
        "producer": {
            "workstream": "evaluation",
            "component": "usage_projection_v1",
            "component_version": "1.0.0",
            "code_commit": commit,
        },
        "binding": {
            "project_id": plan.project_id,
            "document_id": plan.document_id,
            "config_id": plan.config_id,
            "config_sha256": plan.config_sha256,
            "input_set_sha256": plan.input_set_sha256,
            "plan_id": plan.plan_id,
            "plan_sha256": plan.plan_sha256,
            "execution_id": execution["execution_id"],
            "execution_sha256": execution["integrity"]["artifact_sha256"],
            "evaluation_logical_run_id": logical_run_id,
            "evaluation_attempt_run_id": attempt_run_id,
        },
        "stage_facts": stage_rows,
        "usage": {**usage, "source_artifact_ids": []},
        "source_records": source_records,
        "integrity": {"artifact_sha256": "0" * 64},
    }
    draft["artifact_id"] = "evaluation-usage-" + _usage_identity_digest(draft)[:24]
    sealed = seal_evaluation_usage_artifact_v1(draft)
    validated = validate_evaluation_usage_artifact_v1(sealed)
    artifact_id = validated["artifact_id"]
    report_usage = copy.deepcopy(validated["usage"])
    report_usage["source_artifact_ids"] = [artifact_id]
    relative_path = f"usage/{validated['integrity']['artifact_sha256']}.json"
    return EvaluationUsageProjectionV1(
        artifact=validated,
        stage_facts=tuple(copy.deepcopy(validated["stage_facts"])),
        usage=report_usage,
        artifact_descriptor={
            "artifact_id": artifact_id,
            "relative_path": relative_path,
            "sha256": validated["integrity"]["artifact_sha256"],
        },
    )


def seal_evaluation_usage_artifact_v1(payload: Mapping[str, Any]) -> dict[str, Any]:
    return seal_payload(payload, policy=_POLICY, hash_path=_SELF_HASH_PATH)


def validate_evaluation_usage_artifact_v1(
    payload: Mapping[str, Any],
) -> dict[str, Any]:
    root = require_mapping(payload, path="$")
    require_exact_keys(
        root,
        required={
            "schema_id",
            "schema_version",
            "artifact_id",
            "created_at",
            "producer",
            "binding",
            "stage_facts",
            "usage",
            "source_records",
            "integrity",
        },
        path="$",
    )
    if root["schema_id"] != EVALUATION_USAGE_SCHEMA_ID:
        raise ContractValidationError("schema_id", "$.schema_id", "foreign usage schema")
    if root["schema_version"] != EVALUATION_USAGE_SCHEMA_VERSION:
        raise ContractValidationError(
            "schema_version", "$.schema_version", "foreign usage schema version"
        )
    normalized = {
        "schema_id": EVALUATION_USAGE_SCHEMA_ID,
        "schema_version": EVALUATION_USAGE_SCHEMA_VERSION,
        "artifact_id": require_string(root["artifact_id"], path="$.artifact_id"),
        "created_at": require_rfc3339(root["created_at"], path="$.created_at"),
        "producer": validate_producer(
            root["producer"], path="$.producer", workstream="evaluation"
        ),
        "binding": _validate_binding(root["binding"]),
        "stage_facts": _validate_stage_facts(root["stage_facts"]),
        "usage": _validate_usage(root["usage"]),
        "source_records": _validate_source_records(root["source_records"]),
        "integrity": _validate_integrity(root["integrity"]),
    }
    stage_ids = [row["stage_id"] for row in normalized["stage_facts"]]
    usage_stage_ids = [row["stage_id"] for row in normalized["usage"]["by_stage"]]
    if stage_ids != usage_stage_ids:
        raise ContractValidationError(
            "usage_stage_exact_cover", "$.usage.by_stage", "usage and stage facts differ"
        )
    expected_artifact_id = "evaluation-usage-" + _usage_identity_digest(normalized)[:24]
    if normalized["artifact_id"] != expected_artifact_id:
        raise ContractValidationError(
            "artifact_id", "$.artifact_id", "usage artifact ID differs from execution"
        )
    if normalized["usage"]["source_artifact_ids"]:
        raise ContractValidationError(
            "internal_usage_source",
            "$.usage.source_artifact_ids",
            "persisted usage artifact cannot recursively reference itself",
        )
    if not verify_payload_hash(normalized, policy=_POLICY, hash_path=_SELF_HASH_PATH):
        raise ContractValidationError(
            "artifact_hash", "$.integrity.artifact_sha256", "usage self-hash mismatch"
        )
    canonical = canonicalize(normalized, policy=_POLICY)
    if not isinstance(canonical, dict):
        raise AssertionError("canonical usage artifact must remain an object")
    return canonical


def persist_evaluation_usage_artifact_v1(
    *, output_root: Path, artifact_payload: Mapping[str, Any]
) -> EvaluationUsagePersistResultV1:
    artifact = validate_evaluation_usage_artifact_v1(artifact_payload)
    root = Path(output_root).resolve()
    root.mkdir(parents=True, exist_ok=True)
    path = _contained_path(
        root, f"usage/{artifact['integrity']['artifact_sha256']}.json"
    )
    reused = not _publish_bytes_create_only(path, _canonical_json_bytes(artifact))
    return EvaluationUsagePersistResultV1(path=path, artifact=artifact, reused=reused)


def load_evaluation_usage_artifact_v1(path: Path) -> dict[str, Any]:
    return validate_evaluation_usage_artifact_v1(_load_json_object(Path(path)))


def _load_shared_records(
    ledger: SharedLlmAttemptLedger | None, *, run_id: str, attempt_run_id: str
) -> dict[str, list[dict[str, Any]]]:
    result = {kind: [] for kind in ("seals", "usage", "errors", "cache", "receipts", "producers")}
    if ledger is None:
        return result
    all_seals = ledger.list_records("seal")
    consumers = [
        row
        for row in all_seals
        if row["workstream"] == "evaluation"
        and row["run_id"] == run_id
        and row["attempt_run_id"] == attempt_run_id
    ]
    consumer_hashes = {row["seal_sha256"] for row in consumers}
    usage = [row for row in ledger.list_records("usage") if row["seal_sha256"] in consumer_hashes]
    errors = [row for row in ledger.list_records("error") if row["seal_sha256"] in consumer_hashes]
    cache = [row for row in ledger.list_records("cache") if row["seal_sha256"] in consumer_hashes]
    receipts = ledger.list_records("artifact_receipt")
    producers = [row for row in all_seals if row["seal_sha256"] not in consumer_hashes]
    for seal in consumers:
        validate_llm_run_records(
            seal=seal,
            usage_rows=[row for row in usage if row["seal_sha256"] == seal["seal_sha256"]],
            error_rows=[row for row in errors if row["seal_sha256"] == seal["seal_sha256"]],
            cache_observations=[row for row in cache if row["seal_sha256"] == seal["seal_sha256"]],
            producer_seals=producers,
            reusable_artifact_receipts=receipts,
            certify_limits=True,
        )
    result.update(
        {
            "seals": consumers,
            "usage": usage,
            "errors": errors,
            "cache": cache,
            "receipts": receipts,
            "producers": producers,
        }
    )
    return result


def _project_shared_stage(
    *,
    stage_id: str,
    method_id: str,
    seals: Sequence[Mapping[str, Any]],
    shared_records: Mapping[str, Sequence[Mapping[str, Any]]],
    execution: Mapping[str, Any],
    attempt_run_id: str,
    relative_path: str | None,
) -> tuple[tuple[dict[str, Any], dict[str, Any]], list[dict[str, str]]]:
    seal_hashes = {row["seal_sha256"] for row in seals}
    usage = [row for row in shared_records["usage"] if row["seal_sha256"] in seal_hashes]
    errors = [row for row in shared_records["errors"] if row["seal_sha256"] in seal_hashes]
    cache = [row for row in shared_records["cache"] if row["seal_sha256"] in seal_hashes]
    cache_hits = [row for row in cache if row["lookup_status"] == "hit" and row["provider_call_avoided"]]
    report_usage = _shared_usage_stage(stage_id, seals, usage, cache_hits)
    method_status = _method_status(execution, method_id)
    if usage:
        started_at = min(row["started_at_utc"] for row in usage)
        ended_at = max(row["finished_at_utc"] for row in usage)
        duration_ms = sum(row["latency_ms"] for row in usage)
    else:
        started_at = ended_at = None
        duration_ms = None
    failed = sum(row["outcome"] != "succeeded" for row in usage)
    if method_status == "failed" or (failed and failed == len(usage)):
        status = "failed"
    elif method_status == "partial" or failed:
        status = "partial"
    else:
        status = "complete"
    stage_fact = {
        "stage_id": stage_id,
        "method_id": method_id,
        "status": status,
        "started_at": started_at,
        "ended_at": ended_at,
        "duration_ms": duration_ms,
        "attempt_run_id": attempt_run_id,
        "error_code": "shared_attempt_failed" if failed else None,
    }
    sources: list[dict[str, str]] = []
    if relative_path is not None:
        for kind, rows, id_field in (
            ("shared_seal", seals, "seal_sha256"),
            ("shared_usage", usage, "attempt_usage_id"),
            ("shared_error", errors, "error_id"),
            ("shared_cache", cache, "observation_id"),
        ):
            for row in rows:
                sources.append(
                    {
                        "kind": kind,
                        "record_id": str(row[id_field]),
                        "record_sha256": shared_canonical_sha256(row),
                        "relative_path": relative_path,
                    }
                )
    return (stage_fact, report_usage), sources


def _shared_usage_stage(
    stage_id: str,
    seals: Sequence[Mapping[str, Any]],
    usage: Sequence[Mapping[str, Any]],
    cache_hits: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    if not usage and cache_hits:
        first = seals[0]
        return {
            "stage_id": stage_id,
            "provider": _unique_or_none([row["primary"]["source"]["source_id"] for row in seals]),
            "model_id": _unique_or_none([row["primary"]["target"]["requested_model_id"] for row in seals]),
            "quota_bucket_id": _unique_or_none(
                [row["primary"]["source"]["physical_quota_bucket_id"] for row in seals]
            ),
            "credential_family": None,
            "accounting_basis": "proxy_reported",
            "status": "available",
            **_known_zero_totals(),
        }
    if not usage:
        return _unavailable_usage_stage(stage_id)
    requested_models = [row["requested_model_id"] for row in usage]
    observed_models = [row["observed_model_id"] or row["requested_model_id"] for row in usage]
    providers = [row["source_id"] for row in usage]
    buckets = [row["physical_quota_bucket_id"] for row in usage]
    numeric_sources = {
        "request_count": [1 for _ in usage],
        "successful_request_count": [1 if row["outcome"] == "succeeded" else 0 for row in usage],
        "failed_request_count": [1 if row["outcome"] != "succeeded" else 0 for row in usage],
        "input_tokens": [row["prompt_tokens"] for row in usage],
        "cached_input_tokens": [row["cached_input_tokens"] for row in usage],
        "output_tokens": [row["completion_tokens"] for row in usage],
        "reasoning_tokens": [row["reasoning_tokens"] for row in usage],
        "thought_tokens": [None for _ in usage],
        "total_tokens": [row["total_tokens"] for row in usage],
    }
    totals = {field: _sum_if_known(values) for field, values in numeric_sources.items()}
    costs = [row["cost_usd"] for row in usage]
    cost = _sum_if_known(costs)
    complete = all(totals[field] is not None for field in _USAGE_NUMERIC_FIELDS if field != "thought_tokens") and cost is not None
    accounting = (
        "provider_reported"
        if all(row["provider_usage_sha256"] is not None for row in usage)
        else "proxy_reported"
    )
    return {
        "stage_id": stage_id,
        "provider": _unique_or_none(providers),
        "model_id": _unique_or_none(observed_models if observed_models else requested_models),
        "quota_bucket_id": _unique_or_none(buckets),
        "credential_family": None,
        "accounting_basis": accounting,
        "status": "available" if complete else "partial",
        **totals,
        "cost_usd": cost,
        "currency": "USD" if cost is not None else None,
    }


def _local_stage_fact(local: Mapping[str, Any], attempt_run_id: str) -> dict[str, Any]:
    meter = local["metering"]
    return {
        "stage_id": "sf_qe.local_scorer",
        "method_id": "sf_qe",
        "status": "complete",
        "started_at": meter["started_at"],
        "ended_at": meter["ended_at"],
        "duration_ms": meter["duration_ms"],
        "attempt_run_id": attempt_run_id,
        "error_code": None,
    }


def _local_usage_stage(local: Mapping[str, Any]) -> dict[str, Any]:
    calls = local["metering"]["batch_call_count"]
    return {
        "stage_id": "sf_qe.local_scorer",
        "provider": "local",
        "model_id": local["model"]["model_id"],
        "quota_bucket_id": None,
        "credential_family": None,
        "accounting_basis": "local_metered",
        "status": "partial",
        "request_count": calls,
        "successful_request_count": calls,
        "failed_request_count": 0,
        "input_tokens": None,
        "cached_input_tokens": None,
        "output_tokens": None,
        "reasoning_tokens": None,
        "thought_tokens": None,
        "total_tokens": None,
        "cost_usd": None,
        "currency": None,
    }


def _compose_usage(rows: Sequence[Mapping[str, Any]], *, notes: Sequence[str]) -> dict[str, Any]:
    active = [row for row in rows if row["status"] != "not_applicable"]
    statuses = {row["status"] for row in active}
    if not active:
        status = "not_applicable"
    elif statuses <= {"available"}:
        status = "available"
    elif statuses <= {"unavailable"}:
        status = "unavailable"
    else:
        status = "partial"
    bases = {row["accounting_basis"] for row in active if row["accounting_basis"] != "unavailable"}
    basis = next(iter(bases)) if len(bases) == 1 else ("mixed" if bases else "unavailable")
    totals = {
        field: _sum_if_known([row[field] for row in active])
        for field in _USAGE_NUMERIC_FIELDS
    }
    costs = [row["cost_usd"] for row in active]
    cost = _sum_if_known(costs)
    unknown_attempts = sum(
        (row["request_count"] or 0)
        if row["status"] in {"partial", "unavailable"}
        else 0
        for row in active
    )
    if status in {"unavailable", "not_applicable"}:
        totals = {field: None for field in _USAGE_NUMERIC_FIELDS}
        cost = None
    if status == "available" and unknown_attempts:
        status = "partial"
    return {
        "status": status,
        "accounting_basis": basis,
        "totals": {
            **totals,
            "cost_usd": cost,
            "currency": "USD" if cost is not None else None,
        },
        "unknown_attempt_count": unknown_attempts,
        "by_stage": [copy.deepcopy(dict(row)) for row in rows],
        "notes": list(notes),
    }


def _validate_local_binding(
    local: Mapping[str, Any], execution: Mapping[str, Any], plan: Any
) -> None:
    expected = {
        "project_id": plan.project_id,
        "document_id": plan.document_id,
        "config_id": plan.config_id,
        "config_sha256": plan.config_sha256,
        "input_set_sha256": plan.input_set_sha256,
        "plan_id": plan.plan_id,
        "plan_sha256": plan.plan_sha256,
    }
    for field, value in expected.items():
        if local["binding"][field] != value:
            raise ContractValidationError(
                "local_sf_qe_binding", "$.local_sf_qe_evidence", f"foreign {field}"
            )
    packet_hashes = {
        row["packet_sha256"]
        for row in execution["jobs"]
        if row["method_id"] == "sf_qe" and row["packet_sha256"] is not None
    }
    if packet_hashes != {row["packet_sha256"] for row in local["rows"]}:
        raise ContractValidationError(
            "local_sf_qe_exact_cover",
            "$.local_sf_qe_evidence.rows",
            "local SF-QE evidence does not exact-cover executed packet hashes",
        )


def _method_status(execution: Mapping[str, Any], method_id: str) -> str:
    statuses = {
        row["status"] for row in execution["aggregates"] if row["method_id"] == method_id
    }
    if not statuses or statuses == {"failed"}:
        return "failed"
    if statuses == {"available"}:
        return "available"
    return "partial"


def _all_method_jobs_succeeded(execution: Mapping[str, Any], method_id: str) -> bool:
    rows = [row for row in execution["jobs"] if row["method_id"] == method_id]
    return bool(rows) and all(row["status"] == "succeeded" for row in rows)


def _unavailable_stage(stage_id: str, method_id: str) -> dict[str, Any]:
    return {
        "stage_id": stage_id,
        "method_id": method_id,
        "status": "not_run",
        "started_at": None,
        "ended_at": None,
        "duration_ms": None,
        "attempt_run_id": None,
        "error_code": "usage_evidence_unavailable",
    }


def _not_applicable_stage(stage_id: str, method_id: str) -> dict[str, Any]:
    return {
        "stage_id": stage_id,
        "method_id": method_id,
        "status": "not_applicable",
        "started_at": None,
        "ended_at": None,
        "duration_ms": None,
        "attempt_run_id": None,
        "error_code": None,
    }


def _unavailable_usage_stage(stage_id: str) -> dict[str, Any]:
    return {
        "stage_id": stage_id,
        "provider": None,
        "model_id": None,
        "quota_bucket_id": None,
        "credential_family": None,
        "accounting_basis": "unavailable",
        "status": "unavailable",
        **{field: None for field in _USAGE_NUMERIC_FIELDS},
        "cost_usd": None,
        "currency": None,
    }


def _zero_usage_stage(stage_id: str) -> dict[str, Any]:
    return {
        "stage_id": stage_id,
        "provider": None,
        "model_id": None,
        "quota_bucket_id": None,
        "credential_family": None,
        "accounting_basis": "unavailable",
        "status": "not_applicable",
        **{field: None for field in _USAGE_NUMERIC_FIELDS},
        "cost_usd": None,
        "currency": None,
    }


def _known_zero_totals() -> dict[str, Any]:
    return {
        **{field: 0 for field in _USAGE_NUMERIC_FIELDS},
        "cost_usd": 0.0,
        "currency": "USD",
    }


def _sum_if_known(values: Sequence[Any]) -> int | float | None:
    if not values or any(value is None for value in values):
        return None
    return sum(values)


def _unique_or_none(values: Sequence[str]) -> str | None:
    unique = set(values)
    return next(iter(unique)) if len(unique) == 1 else None


def _deduplicate_source_records(rows: Sequence[Mapping[str, str]]) -> list[dict[str, str]]:
    result: dict[tuple[str, str], dict[str, str]] = {}
    for row in rows:
        key = (row["kind"], row["record_id"])
        candidate = dict(row)
        prior = result.get(key)
        if prior is not None and prior != candidate:
            raise ContractValidationError(
                "source_record_conflict", "$.source_records", "record identity has different facts"
            )
        result[key] = candidate
    return list(result.values())


def _validate_binding(value: Any) -> dict[str, str]:
    path = "$.binding"
    row = require_mapping(value, path=path)
    fields = {
        "project_id",
        "document_id",
        "config_id",
        "config_sha256",
        "input_set_sha256",
        "plan_id",
        "plan_sha256",
        "execution_id",
        "execution_sha256",
        "evaluation_logical_run_id",
        "evaluation_attempt_run_id",
    }
    require_exact_keys(row, required=fields, path=path)
    result = {
        field: require_string(row[field], path=f"{path}.{field}") for field in fields
    }
    for field in ("config_sha256", "input_set_sha256", "plan_sha256", "execution_sha256"):
        result[field] = require_sha256(row[field], path=f"{path}.{field}")
    return result


def _validate_stage_facts(value: Any) -> list[dict[str, Any]]:
    rows = require_list(value, path="$.stage_facts")
    result = []
    for index, raw in enumerate(rows):
        path = f"$.stage_facts[{index}]"
        row = require_mapping(raw, path=path)
        fields = {
            "stage_id", "method_id", "status", "started_at", "ended_at",
            "duration_ms", "attempt_run_id", "error_code",
        }
        require_exact_keys(row, required=fields, path=path)
        started = None if row["started_at"] is None else require_rfc3339(row["started_at"], path=f"{path}.started_at")
        ended = None if row["ended_at"] is None else require_rfc3339(row["ended_at"], path=f"{path}.ended_at")
        duration = require_nullable_int(row["duration_ms"], path=f"{path}.duration_ms", minimum=0)
        if any(item is None for item in (started, ended, duration)) and any(item is not None for item in (started, ended, duration)):
            raise ContractValidationError("stage_time", path, "stage time facts must be all null or all present")
        result.append(
            {
                "stage_id": require_string(row["stage_id"], path=f"{path}.stage_id"),
                "method_id": require_enum(row["method_id"], {"sf_qe", "sf_bt", "pj"}, path=f"{path}.method_id"),
                "status": require_enum(row["status"], {"complete", "partial", "failed", "not_run", "not_applicable"}, path=f"{path}.status"),
                "started_at": started,
                "ended_at": ended,
                "duration_ms": duration,
                "attempt_run_id": require_nullable_string(row["attempt_run_id"], path=f"{path}.attempt_run_id"),
                "error_code": require_nullable_string(row["error_code"], path=f"{path}.error_code"),
            }
        )
    require_unique([row["stage_id"] for row in result], path="$.stage_facts.stage_id")
    return result


def _validate_usage(value: Any) -> dict[str, Any]:
    path = "$.usage"
    row = require_mapping(value, path=path)
    require_exact_keys(row, required={"status", "accounting_basis", "totals", "unknown_attempt_count", "by_stage", "notes", "source_artifact_ids"}, path=path)
    result = {
        "status": require_enum(row["status"], {"available", "partial", "unavailable", "not_applicable"}, path=f"{path}.status"),
        "accounting_basis": require_enum(row["accounting_basis"], {"provider_reported", "proxy_reported", "local_metered", "mixed", "unavailable"}, path=f"{path}.accounting_basis"),
        "totals": _validate_totals(row["totals"], path=f"{path}.totals"),
        "unknown_attempt_count": require_int(row["unknown_attempt_count"], path=f"{path}.unknown_attempt_count", minimum=0),
        "by_stage": _validate_usage_stages(row["by_stage"]),
        "notes": [require_string(item, path=f"{path}.notes[{index}]") for index, item in enumerate(require_list(row["notes"], path=f"{path}.notes"))],
        "source_artifact_ids": [require_string(item, path=f"{path}.source_artifact_ids[{index}]") for index, item in enumerate(require_list(row["source_artifact_ids"], path=f"{path}.source_artifact_ids"))],
    }
    if result["status"] in {"unavailable", "not_applicable"}:
        if result["accounting_basis"] != "unavailable" or any(
            result["totals"][field] is not None
            for field in (*_USAGE_NUMERIC_FIELDS, "cost_usd", "currency")
        ):
            raise ContractValidationError(
                "usage_unknown", path, "unavailable usage must keep all totals null"
            )
    elif result["accounting_basis"] == "unavailable":
        raise ContractValidationError(
            "usage_basis", f"{path}.accounting_basis", "reported usage needs a basis"
        )
    if result["status"] == "available" and result["unknown_attempt_count"]:
        raise ContractValidationError(
            "usage_status", path, "available usage cannot contain unknown attempts"
        )
    return result


def _validate_usage_stages(value: Any) -> list[dict[str, Any]]:
    result = []
    for index, raw in enumerate(require_list(value, path="$.usage.by_stage")):
        path = f"$.usage.by_stage[{index}]"
        row = require_mapping(raw, path=path)
        fields = {"stage_id", "provider", "model_id", "quota_bucket_id", "credential_family", "accounting_basis", "status", *_USAGE_NUMERIC_FIELDS, "cost_usd", "currency"}
        require_exact_keys(row, required=fields, path=path)
        totals = _validate_totals({field: row[field] for field in (*_USAGE_NUMERIC_FIELDS, "cost_usd", "currency")}, path=path)
        normalized = {
                "stage_id": require_string(row["stage_id"], path=f"{path}.stage_id"),
                "provider": require_nullable_string(row["provider"], path=f"{path}.provider"),
                "model_id": require_nullable_string(row["model_id"], path=f"{path}.model_id"),
                "quota_bucket_id": require_nullable_string(row["quota_bucket_id"], path=f"{path}.quota_bucket_id"),
                "credential_family": require_nullable_string(row["credential_family"], path=f"{path}.credential_family"),
                "accounting_basis": require_enum(row["accounting_basis"], {"provider_reported", "proxy_reported", "local_metered", "mixed", "unavailable"}, path=f"{path}.accounting_basis"),
                "status": require_enum(row["status"], {"available", "partial", "unavailable", "not_applicable"}, path=f"{path}.status"),
                **totals,
            }
        if normalized["status"] in {"unavailable", "not_applicable"}:
            if normalized["accounting_basis"] != "unavailable" or any(
                normalized[field] is not None
                for field in (*_USAGE_NUMERIC_FIELDS, "cost_usd", "currency")
            ):
                raise ContractValidationError(
                    "usage_unknown", path, "unavailable stage usage must remain null"
                )
        elif normalized["accounting_basis"] == "unavailable":
            raise ContractValidationError(
                "usage_basis", path, "reported stage usage needs an accounting basis"
            )
        result.append(normalized)
    require_unique([row["stage_id"] for row in result], path="$.usage.by_stage.stage_id")
    return result


def _validate_totals(value: Any, *, path: str) -> dict[str, Any]:
    row = require_mapping(value, path=path)
    fields = {*_USAGE_NUMERIC_FIELDS, "cost_usd", "currency"}
    require_exact_keys(row, required=fields, path=path)
    result = {field: require_nullable_int(row[field], path=f"{path}.{field}", minimum=0) for field in _USAGE_NUMERIC_FIELDS}
    result["cost_usd"] = require_nullable_number(row["cost_usd"], path=f"{path}.cost_usd", minimum=0)
    result["currency"] = require_nullable_string(row["currency"], path=f"{path}.currency")
    if (result["cost_usd"] is None) != (result["currency"] is None):
        raise ContractValidationError("usage_currency", path, "cost and currency must both be known or null")
    return result


def _validate_source_records(value: Any) -> list[dict[str, str]]:
    result = []
    for index, raw in enumerate(require_list(value, path="$.source_records")):
        path = f"$.source_records[{index}]"
        row = require_mapping(raw, path=path)
        require_exact_keys(row, required={"kind", "record_id", "record_sha256", "relative_path"}, path=path)
        result.append(
            {
                "kind": require_enum(row["kind"], {"local_sf_qe_evidence", "shared_seal", "shared_usage", "shared_error", "shared_cache"}, path=f"{path}.kind"),
                "record_id": require_string(row["record_id"], path=f"{path}.record_id"),
                "record_sha256": require_sha256(row["record_sha256"], path=f"{path}.record_sha256"),
                "relative_path": require_relative_path(row["relative_path"], path=f"{path}.relative_path"),
            }
        )
    require_unique([(row["kind"], row["record_id"]) for row in result], path="$.source_records")
    return result


def _validate_integrity(value: Any) -> dict[str, str]:
    row = require_mapping(value, path="$.integrity")
    require_exact_keys(row, required={"artifact_sha256"}, path="$.integrity")
    return {"artifact_sha256": require_sha256(row["artifact_sha256"], path="$.integrity.artifact_sha256")}


def _usage_identity_digest(value: Mapping[str, Any]) -> str:
    material = copy.deepcopy(dict(value))
    material.pop("schema_id", None)
    material.pop("schema_version", None)
    material.pop("artifact_id", None)
    material.pop("created_at", None)
    material.pop("producer", None)
    material.pop("integrity", None)
    return canonical_sha256(material, policy=_POLICY)


def _contained_path(root: Path, relative_path: str) -> Path:
    candidate = (root / Path(*relative_path.split("/"))).resolve()
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise ContractValidationError("path_escape", str(candidate), "path escapes output root") from exc
    return candidate


def _canonical_json_bytes(payload: Mapping[str, Any]) -> bytes:
    try:
        return (json.dumps(payload, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ContractValidationError("json_encoding", "$", "usage artifact must be finite JSON") from exc


def _publish_bytes_create_only(path: Path, encoded: bytes) -> bool:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if path.read_bytes() == encoded:
            return False
        raise ContractValidationError("immutable_conflict", str(path), "refusing to overwrite usage artifact")
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("xb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(temporary, path)
        except FileExistsError:
            if path.read_bytes() == encoded:
                return False
            raise ContractValidationError("immutable_conflict", str(path), "concurrent usage artifact differs")
        except OSError as exc:
            raise ContractValidationError("atomic_publish", str(path), "cannot atomically publish usage artifact") from exc
        return True
    finally:
        if temporary.exists():
            temporary.unlink()


def _load_json_object(path: Path) -> dict[str, Any]:
    try:
        with path.open("r", encoding="utf-8", newline="") as handle:
            value = json.load(handle, object_pairs_hook=_reject_duplicate_keys, parse_constant=_reject_non_finite)
    except FileNotFoundError as exc:
        raise ContractValidationError("missing_artifact", str(path), "usage artifact is missing") from exc
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise ContractValidationError("artifact_json", str(path), "usage artifact is not strict JSON") from exc
    if not isinstance(value, dict):
        raise ContractValidationError("artifact_shape", str(path), "usage artifact root must be an object")
    return value


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _reject_non_finite(value: str) -> None:
    raise ValueError(f"non-finite JSON constant: {value}")
