from __future__ import annotations

import copy
import json
import os
from pathlib import Path
from typing import Any, Mapping, Sequence
import uuid

from pipeline.eval.benchmark_v1 import (
    BENCHMARK_ARM_IDS_V1,
    BENCHMARK_CHAPTER_IDS_V1,
    validate_benchmark_manifest_v1,
    validate_benchmark_preflight_v1,
)
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
    aggregate_evaluation_job_rows_v1,
    validate_evaluation_aggregates_v1,
    validate_evaluation_execution_artifact,
)
from pipeline.eval.full_run_report_v1 import validate_full_run_report


__all__ = [
    "BENCHMARK_RUN_REPORT_SCHEMA_ID",
    "compose_benchmark_run_report_v1",
    "persist_benchmark_run_report_v1",
    "validate_benchmark_run_report_v1",
]


BENCHMARK_RUN_REPORT_SCHEMA_ID = "EvaluationBenchmarkRunReportV1"
SCHEMA_VERSION = "1.1.0"
_SELF_HASH_PATH = ("integrity", "report_sha256")
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
_POLICY = CanonicalPolicy(
    set_like_paths=frozenset(
        {
            ("aggregates", "*", "arm_values"),
            ("aggregates", "*", "source_job_ids"),
            ("claim", "reason_codes"),
        }
    ),
    semantic_sequence_paths=frozenset(
        {
            ("identity", "selected_chapter_ids"),
            ("identity", "selected_arm_ids"),
            ("identity", "selected_scorer_ids"),
            ("chapter_runs",),
            ("aggregates",),
            ("aggregates", "*", "comparison_pair_arm_ids"),
            ("aggregates", "*", "caveats"),
        }
    ),
)


def compose_benchmark_run_report_v1(
    benchmark_manifest: Mapping[str, Any],
    benchmark_preflight: Mapping[str, Any],
    chapter_results: Sequence[Mapping[str, Any]],
    *,
    generated_at: str,
    producer_code_commit: str,
    evaluation_logical_run_id: str,
    evaluation_attempt_run_id: str,
    evaluation_profile_id: str,
    policy_profile_id: str | None,
    scoring_contract_sha256: str,
    selected_scorer_ids: Sequence[str],
    workflow_settings_sha256: str | None,
    baseline_arm_id: str | None = None,
    candidate_arm_id: str | None = None,
) -> dict[str, Any]:
    manifest = validate_benchmark_manifest_v1(benchmark_manifest)
    preflight = validate_benchmark_preflight_v1(benchmark_preflight)
    if preflight["benchmark_manifest_sha256"] != manifest["integrity"]["manifest_sha256"]:
        raise ContractValidationError(
            "manifest_binding", "$.benchmark_preflight", "preflight belongs to another benchmark"
        )
    if preflight["status"] != "ready":
        raise ContractValidationError(
            "preflight_blocked",
            "$.benchmark_preflight.status",
            "benchmark scoring requires every selected arm/chapter cell ready",
        )
    selected_chapter_ids = tuple(
        row["chapter_id"] for row in manifest["chapters"]
    )
    selected_arm_ids = tuple(row["arm_id"] for row in manifest["arm_contracts"])
    scorer_ids = [
        require_string(item, path="$.selected_scorer_ids[*]")
        for item in selected_scorer_ids
    ]
    _require_canonical_subset(
        scorer_ids,
        allowed=("sf_qe", "sf_bt", "pj"),
        minimum=1,
        path="$.selected_scorer_ids",
    )
    if [row["chapter_id"] for row in preflight["chapter_checks"]] != list(
        selected_chapter_ids
    ) or [row["arm_id"] for row in preflight["arm_checks"]] != list(
        selected_arm_ids
    ):
        raise ContractValidationError(
            "preflight_scope",
            "$.benchmark_preflight",
            "preflight chapter/arm scope differs from the benchmark manifest",
        )
    baseline, candidate = _comparison_pair(
        baseline_arm_id, candidate_arm_id, selected_arm_ids=selected_arm_ids
    )
    timestamp = require_rfc3339(generated_at, path="$.generated_at")
    commit = require_commit(producer_code_commit, path="$.producer_code_commit")
    normalized_chapters, all_jobs = _normalize_chapter_results(
        chapter_results, manifest=manifest
    )
    aggregates = aggregate_evaluation_job_rows_v1(
        all_jobs,
        selected_arm_ids=selected_arm_ids,
        baseline_arm_id=baseline,
        candidate_arm_id=candidate,
    )
    coverage = {
        "expected_chapter_count": len(selected_chapter_ids),
        "completed_chapter_count": len(normalized_chapters),
        "planned_job_count": sum(row["coverage"]["planned_job_count"] for row in normalized_chapters),
        "blocked_job_count": sum(row["coverage"]["blocked_job_count"] for row in normalized_chapters),
        "succeeded_job_count": sum(row["coverage"]["succeeded_job_count"] for row in normalized_chapters),
        "failed_job_count": sum(row["coverage"]["failed_job_count"] for row in normalized_chapters),
    }
    report_id = "benchmark-report-" + _sha256_text(
        "|".join(
            (
                manifest["integrity"]["manifest_sha256"],
                preflight["integrity"]["preflight_sha256"],
                evaluation_logical_run_id,
                evaluation_attempt_run_id,
                timestamp,
            )
        )
    )[:24]
    draft = {
        "schema_id": BENCHMARK_RUN_REPORT_SCHEMA_ID,
        "schema_version": SCHEMA_VERSION,
        "report_id": report_id,
        "generated_at": timestamp,
        "producer": {
            "workstream": "evaluation",
            "component": "benchmark_aggregate_v1",
            "component_version": SCHEMA_VERSION,
            "code_commit": commit,
        },
        "identity": {
            "benchmark_id": manifest["benchmark_id"],
            "benchmark_manifest_sha256": manifest["integrity"]["manifest_sha256"],
            "benchmark_preflight_sha256": preflight["integrity"]["preflight_sha256"],
            "evaluation_logical_run_id": require_string(
                evaluation_logical_run_id, path="$.evaluation_logical_run_id"
            ),
            "evaluation_attempt_run_id": require_string(
                evaluation_attempt_run_id, path="$.evaluation_attempt_run_id"
            ),
            "evaluation_profile_id": require_string(
                evaluation_profile_id, path="$.evaluation_profile_id"
            ),
            "policy_profile_id": require_nullable_string(
                policy_profile_id, path="$.policy_profile_id"
            ),
            "scoring_contract_sha256": require_sha256(
                scoring_contract_sha256, path="$.scoring_contract_sha256"
            ),
            "selected_chapter_ids": list(selected_chapter_ids),
            "selected_arm_ids": list(selected_arm_ids),
            "selected_scorer_ids": list(scorer_ids),
            "workflow_settings_sha256": (
                None
                if workflow_settings_sha256 is None
                else require_sha256(
                    workflow_settings_sha256,
                    path="$.workflow_settings_sha256",
                )
            ),
            "baseline_arm_id": baseline,
            "candidate_arm_id": candidate,
        },
        "coverage": coverage,
        "chapter_runs": normalized_chapters,
        "aggregates": aggregates,
        "usage": _aggregate_usage(normalized_chapters),
        "claim": {
            "status": "not_defined",
            "verdict": "INCONCLUSIVE",
            "reason_codes": ["no_cross_method_composite"],
        },
        "integrity": {"report_sha256": "0" * 64},
    }
    return validate_benchmark_run_report_v1(
        seal_payload(draft, policy=_POLICY, hash_path=_SELF_HASH_PATH)
    )


def validate_benchmark_run_report_v1(payload: Mapping[str, Any]) -> dict[str, Any]:
    root = require_mapping(payload, path="$")
    require_exact_keys(
        root,
        required={
            "schema_id",
            "schema_version",
            "report_id",
            "generated_at",
            "producer",
            "identity",
            "coverage",
            "chapter_runs",
            "aggregates",
            "usage",
            "claim",
            "integrity",
        },
        path="$",
    )
    identity = _validate_identity(root["identity"])
    normalized = {
        "schema_id": require_enum(root["schema_id"], {BENCHMARK_RUN_REPORT_SCHEMA_ID}, path="$.schema_id"),
        "schema_version": require_enum(root["schema_version"], {SCHEMA_VERSION}, path="$.schema_version"),
        "report_id": require_string(root["report_id"], path="$.report_id"),
        "generated_at": require_rfc3339(root["generated_at"], path="$.generated_at"),
        "producer": _validate_producer(root["producer"]),
        "identity": identity,
        "coverage": _validate_coverage(root["coverage"]),
        "chapter_runs": _validate_chapter_runs(
            root["chapter_runs"],
            expected_chapter_ids=identity["selected_chapter_ids"],
        ),
        "aggregates": validate_evaluation_aggregates_v1(root["aggregates"]),
        "usage": _validate_usage(root["usage"]),
        "claim": _validate_claim(root["claim"]),
        "integrity": _validate_integrity(root["integrity"]),
    }
    _validate_report_semantics(normalized)
    if not verify_payload_hash(normalized, policy=_POLICY, hash_path=_SELF_HASH_PATH):
        raise ContractValidationError(
            "report_hash", "$.integrity.report_sha256", "benchmark report self-hash drift"
        )
    canonical = canonicalize(normalized, policy=_POLICY)
    assert isinstance(canonical, dict)
    return canonical


def persist_benchmark_run_report_v1(
    output_root: Path, report_payload: Mapping[str, Any]
) -> Path:
    report = validate_benchmark_run_report_v1(report_payload)
    root = output_root.resolve()
    root.mkdir(parents=True, exist_ok=True)
    _require_chapter_artifacts(root, report)
    path = root / "reports" / "benchmark_run_report_v1.json"
    encoded = _json_bytes(report)
    if path.exists():
        if path.read_bytes() != encoded:
            raise ContractValidationError(
                "immutable_conflict", str(path), "persisted benchmark report differs"
            )
        return path
    _write_bytes_atomic(path, encoded)
    return path


def _require_chapter_artifacts(root: Path, report: Mapping[str, Any]) -> None:
    for chapter in report["chapter_runs"]:
        chapter_id = chapter["chapter_id"]
        ordinal = chapter["ordinal"]
        expected_prefix = f"chapters/{ordinal:02d}_{chapter_id}/"
        report_relative = chapter["report_relative_path"]
        execution_relative = chapter["execution_relative_path"]
        if not report_relative.startswith(expected_prefix) or not execution_relative.startswith(
            expected_prefix
        ):
            raise ContractValidationError(
                "chapter_artifact_path",
                f"$.chapter_runs[{ordinal}]",
                "chapter artifacts must remain under their sealed chapter root",
            )
        report_path = _contained_file(root, report_relative)
        execution_path = _contained_file(root, execution_relative)
        persisted_report = validate_full_run_report(_load_json(report_path))
        persisted_execution = validate_evaluation_execution_artifact(
            _load_json(execution_path)
        )
        if persisted_report["integrity"]["report_sha256"] != chapter["report_sha256"]:
            raise ContractValidationError(
                "chapter_report_binding",
                str(report_path),
                "chapter report differs from the benchmark report reference",
            )
        if (
            persisted_execution["integrity"]["artifact_sha256"]
            != chapter["execution_sha256"]
        ):
            raise ContractValidationError(
                "chapter_execution_binding",
                str(execution_path),
                "chapter execution differs from the benchmark report reference",
            )
        binding = persisted_execution["binding"]
        expected_binding = {
            "config_sha256": chapter["config_sha256"],
            "input_set_sha256": chapter["input_set_sha256"],
            "plan_sha256": chapter["plan_sha256"],
        }
        if any(binding[key] != value for key, value in expected_binding.items()):
            raise ContractValidationError(
                "chapter_execution_binding",
                str(execution_path),
                "chapter execution identity differs from the aggregate reference",
            )
        if (
            persisted_report["integrity"]["evaluation_config_sha256"]
            != chapter["config_sha256"]
            or persisted_report["identity"]["logical_run_id"]
            != f"{report['identity']['evaluation_logical_run_id']}.{chapter_id}"
            or persisted_report["identity"]["profile_id"]
            != report["identity"]["evaluation_profile_id"]
        ):
            raise ContractValidationError(
                "chapter_report_binding",
                str(report_path),
                "chapter report identity differs from the benchmark request",
            )


def _contained_file(root: Path, relative_path: str) -> Path:
    normalized = require_relative_path(relative_path, path="$.chapter_artifact.relative_path")
    candidate = (root / Path(*normalized.split("/"))).resolve()
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise ContractValidationError(
            "path_escape", str(candidate), "chapter artifact escapes benchmark root"
        ) from exc
    if not candidate.is_file():
        raise ContractValidationError(
            "missing_artifact", str(candidate), "referenced chapter artifact is absent"
        )
    return candidate


def _load_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(
            path.read_text(encoding="utf-8"),
            parse_constant=lambda token: (_ for _ in ()).throw(ValueError(token)),
        )
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
        raise ContractValidationError(
            "artifact_json", str(path), "chapter artifact is not valid finite JSON"
        ) from exc
    if not isinstance(value, dict):
        raise ContractValidationError(
            "artifact_json", str(path), "chapter artifact root must be an object"
        )
    return value


def _normalize_chapter_results(
    values: Sequence[Mapping[str, Any]], *, manifest: Mapping[str, Any]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    rows = require_list(list(values), path="$.chapter_results")
    selected_chapter_ids = tuple(
        row["chapter_id"] for row in manifest["chapters"]
    )
    selected_arm_ids = tuple(row["arm_id"] for row in manifest["arm_contracts"])
    if len(rows) != len(selected_chapter_ids):
        raise ContractValidationError(
            "chapter_exact_cover",
            "$.chapter_results",
            "selected chapter results are required",
        )
    manifest_by_id = {row["chapter_id"]: row for row in manifest["chapters"]}
    normalized: list[dict[str, Any]] = []
    all_jobs: list[dict[str, Any]] = []
    for ordinal, raw in enumerate(rows):
        path = f"$.chapter_results[{ordinal}]"
        row = require_mapping(raw, path=path)
        require_exact_keys(
            row,
            required={
                "chapter_id",
                "report_relative_path",
                "execution_relative_path",
                "report",
                "execution",
                "reused_complete_run",
            },
            path=path,
        )
        chapter_id = require_enum(
            row["chapter_id"],
            {selected_chapter_ids[ordinal]},
            path=f"{path}.chapter_id",
        )
        report = validate_full_run_report(require_mapping(row["report"], path=f"{path}.report"))
        execution = validate_evaluation_execution_artifact(
            require_mapping(row["execution"], path=f"{path}.execution")
        )
        if report["report_state"] != "complete":
            raise ContractValidationError(
                "chapter_incomplete", f"{path}.report.report_state", "chapter report must be complete"
            )
        if tuple(execution["binding"]["selected_arm_ids"]) != selected_arm_ids:
            raise ContractValidationError(
                "arm_binding", f"{path}.execution.binding.selected_arm_ids", "chapter arm set differs"
            )
        if [arm["arm_id"] for arm in report["arms"]] != list(selected_arm_ids):
            raise ContractValidationError(
                "arm_binding", f"{path}.report.arms", "chapter report arm order differs"
            )
        if report["integrity"]["evaluation_config_sha256"] != execution["binding"]["config_sha256"]:
            raise ContractValidationError(
                "config_binding", path, "chapter report and execution use different configs"
            )
        if report["identity"]["project_id"] != "d2l" or report["identity"]["document_id"] != "d2l":
            raise ContractValidationError(
                "source_identity", f"{path}.report.identity", "chapter report is foreign to D2L"
            )
        if execution["binding"]["project_id"] != "d2l" or execution["binding"]["document_id"] != "d2l":
            raise ContractValidationError(
                "source_identity", f"{path}.execution.binding", "chapter execution is foreign to D2L"
            )
        reused = row["reused_complete_run"]
        if not isinstance(reused, bool):
            raise ContractValidationError("type", f"{path}.reused_complete_run", "expected boolean")
        coverage = execution["coverage"]
        normalized.append(
            {
                "chapter_id": chapter_id,
                "ordinal": ordinal,
                "source_read_model_sha256": manifest_by_id[chapter_id]["source_read_model_sha256"],
                "report_relative_path": require_relative_path(
                    row["report_relative_path"], path=f"{path}.report_relative_path"
                ),
                "report_sha256": report["integrity"]["report_sha256"],
                "execution_relative_path": require_relative_path(
                    row["execution_relative_path"], path=f"{path}.execution_relative_path"
                ),
                "execution_sha256": execution["integrity"]["artifact_sha256"],
                "config_sha256": execution["binding"]["config_sha256"],
                "input_set_sha256": execution["binding"]["input_set_sha256"],
                "plan_sha256": execution["binding"]["plan_sha256"],
                "reused_complete_run": reused,
                "coverage": copy.deepcopy(coverage),
                "usage": _chapter_usage_summary(report["usage"]),
            }
        )
        all_jobs.extend(copy.deepcopy(execution["jobs"]))
    require_unique([row["job_id"] for row in all_jobs], path="$.chapter_results.jobs")
    return normalized, all_jobs


def _aggregate_usage(chapters: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    usages = [row["usage"] for row in chapters]
    if all(row["status"] == "not_applicable" for row in usages):
        return {
            "status": "not_applicable",
            "accounting_basis": "unavailable",
            "totals": {**{field: None for field in _USAGE_NUMERIC_FIELDS}, "cost_usd": None, "currency": None},
            "unknown_attempt_count": sum(row["unknown_attempt_count"] for row in usages),
        }
    if all(row["status"] in {"unavailable", "not_applicable"} for row in usages):
        return {
            "status": "unavailable",
            "accounting_basis": "unavailable",
            "totals": {**{field: None for field in _USAGE_NUMERIC_FIELDS}, "cost_usd": None, "currency": None},
            "unknown_attempt_count": sum(row["unknown_attempt_count"] for row in usages),
        }
    totals: dict[str, Any] = {}
    for field in _USAGE_NUMERIC_FIELDS:
        values = [row["totals"][field] for row in usages]
        totals[field] = sum(values) if all(value is not None for value in values) else None
    costs = [row["totals"]["cost_usd"] for row in usages]
    currencies = {row["totals"]["currency"] for row in usages if row["totals"]["currency"] is not None}
    totals["cost_usd"] = sum(costs) if all(value is not None for value in costs) and len(currencies) == 1 else None
    totals["currency"] = next(iter(currencies)) if totals["cost_usd"] is not None else None
    bases = {row["accounting_basis"] for row in usages if row["accounting_basis"] != "unavailable"}
    basis = next(iter(bases)) if len(bases) == 1 else "mixed"
    unknown = sum(row["unknown_attempt_count"] for row in usages)
    status = "available" if all(row["status"] == "available" for row in usages) and unknown == 0 else "partial"
    return {
        "status": status,
        "accounting_basis": basis,
        "totals": totals,
        "unknown_attempt_count": unknown,
    }


def _validate_identity(value: Any) -> dict[str, Any]:
    path = "$.identity"
    row = require_mapping(value, path=path)
    require_exact_keys(
        row,
        required={
            "benchmark_id",
            "benchmark_manifest_sha256",
            "benchmark_preflight_sha256",
            "evaluation_logical_run_id",
            "evaluation_attempt_run_id",
            "evaluation_profile_id",
            "policy_profile_id",
            "scoring_contract_sha256",
            "selected_chapter_ids",
            "selected_arm_ids",
            "selected_scorer_ids",
            "workflow_settings_sha256",
            "baseline_arm_id",
            "candidate_arm_id",
        },
        path=path,
    )
    chapters = [
        require_string(item, path=f"{path}.selected_chapter_ids")
        for item in require_list(
            row["selected_chapter_ids"], path=f"{path}.selected_chapter_ids"
        )
    ]
    _require_canonical_subset(
        chapters,
        allowed=BENCHMARK_CHAPTER_IDS_V1,
        minimum=1,
        path=f"{path}.selected_chapter_ids",
    )
    arms = [require_string(item, path=f"{path}.selected_arm_ids") for item in require_list(row["selected_arm_ids"], path=f"{path}.selected_arm_ids")]
    _require_canonical_subset(
        arms,
        allowed=BENCHMARK_ARM_IDS_V1,
        minimum=2,
        path=f"{path}.selected_arm_ids",
    )
    baseline, candidate = _comparison_pair(
        require_nullable_string(row["baseline_arm_id"], path=f"{path}.baseline_arm_id"),
        require_nullable_string(row["candidate_arm_id"], path=f"{path}.candidate_arm_id"),
        selected_arm_ids=arms,
    )
    scorers = [
        require_string(item, path=f"{path}.selected_scorer_ids")
        for item in require_list(
            row["selected_scorer_ids"], path=f"{path}.selected_scorer_ids"
        )
    ]
    _require_canonical_subset(
        scorers,
        allowed=("sf_qe", "sf_bt", "pj"),
        minimum=1,
        path=f"{path}.selected_scorer_ids",
    )
    return {
        "benchmark_id": require_string(row["benchmark_id"], path=f"{path}.benchmark_id"),
        "benchmark_manifest_sha256": require_sha256(row["benchmark_manifest_sha256"], path=f"{path}.benchmark_manifest_sha256"),
        "benchmark_preflight_sha256": require_sha256(row["benchmark_preflight_sha256"], path=f"{path}.benchmark_preflight_sha256"),
        "evaluation_logical_run_id": require_string(row["evaluation_logical_run_id"], path=f"{path}.evaluation_logical_run_id"),
        "evaluation_attempt_run_id": require_string(row["evaluation_attempt_run_id"], path=f"{path}.evaluation_attempt_run_id"),
        "evaluation_profile_id": require_string(row["evaluation_profile_id"], path=f"{path}.evaluation_profile_id"),
        "policy_profile_id": require_nullable_string(row["policy_profile_id"], path=f"{path}.policy_profile_id"),
        "scoring_contract_sha256": require_sha256(row["scoring_contract_sha256"], path=f"{path}.scoring_contract_sha256"),
        "selected_chapter_ids": chapters,
        "selected_arm_ids": arms,
        "selected_scorer_ids": scorers,
        "workflow_settings_sha256": (
            None
            if row["workflow_settings_sha256"] is None
            else require_sha256(
                row["workflow_settings_sha256"],
                path=f"{path}.workflow_settings_sha256",
            )
        ),
        "baseline_arm_id": baseline,
        "candidate_arm_id": candidate,
    }


def _validate_coverage(value: Any) -> dict[str, int]:
    path = "$.coverage"
    row = require_mapping(value, path=path)
    fields = {
        "expected_chapter_count",
        "completed_chapter_count",
        "planned_job_count",
        "blocked_job_count",
        "succeeded_job_count",
        "failed_job_count",
    }
    require_exact_keys(row, required=fields, path=path)
    return {field: require_int(row[field], path=f"{path}.{field}", minimum=0) for field in fields}


def _validate_chapter_runs(
    value: Any, *, expected_chapter_ids: Sequence[str]
) -> list[dict[str, Any]]:
    rows = require_list(value, path="$.chapter_runs")
    if len(rows) != len(expected_chapter_ids):
        raise ContractValidationError(
            "chapter_exact_cover",
            "$.chapter_runs",
            "benchmark report requires every selected chapter",
        )
    result = []
    for ordinal, raw in enumerate(rows):
        path = f"$.chapter_runs[{ordinal}]"
        row = require_mapping(raw, path=path)
        require_exact_keys(
            row,
            required={
                "chapter_id", "ordinal", "source_read_model_sha256", "report_relative_path",
                "report_sha256", "execution_relative_path", "execution_sha256", "config_sha256",
                "input_set_sha256", "plan_sha256", "reused_complete_run", "coverage", "usage",
            },
            path=path,
        )
        reused = row["reused_complete_run"]
        if not isinstance(reused, bool):
            raise ContractValidationError("type", f"{path}.reused_complete_run", "expected boolean")
        observed_ordinal = require_int(row["ordinal"], path=f"{path}.ordinal", minimum=0)
        if observed_ordinal != ordinal:
            raise ContractValidationError(
                "chapter_order", f"{path}.ordinal", "chapter ordinal differs from source order"
            )
        result.append(
            {
                "chapter_id": require_enum(
                    row["chapter_id"],
                    {expected_chapter_ids[ordinal]},
                    path=f"{path}.chapter_id",
                ),
                "ordinal": observed_ordinal,
                "source_read_model_sha256": require_sha256(row["source_read_model_sha256"], path=f"{path}.source_read_model_sha256"),
                "report_relative_path": require_relative_path(row["report_relative_path"], path=f"{path}.report_relative_path"),
                "report_sha256": require_sha256(row["report_sha256"], path=f"{path}.report_sha256"),
                "execution_relative_path": require_relative_path(row["execution_relative_path"], path=f"{path}.execution_relative_path"),
                "execution_sha256": require_sha256(row["execution_sha256"], path=f"{path}.execution_sha256"),
                "config_sha256": require_sha256(row["config_sha256"], path=f"{path}.config_sha256"),
                "input_set_sha256": require_sha256(row["input_set_sha256"], path=f"{path}.input_set_sha256"),
                "plan_sha256": require_sha256(row["plan_sha256"], path=f"{path}.plan_sha256"),
                "reused_complete_run": reused,
                "coverage": _validate_execution_coverage(row["coverage"], path=f"{path}.coverage"),
                "usage": _validate_chapter_usage(row["usage"], path=f"{path}.usage"),
            }
        )
    return result


def _validate_execution_coverage(value: Any, *, path: str) -> dict[str, int]:
    row = require_mapping(value, path=path)
    fields = {"planned_job_count", "blocked_job_count", "succeeded_job_count", "failed_job_count"}
    require_exact_keys(row, required=fields, path=path)
    result = {field: require_int(row[field], path=f"{path}.{field}", minimum=0) for field in fields}
    if result["planned_job_count"] != result["blocked_job_count"] + result["succeeded_job_count"] + result["failed_job_count"]:
        raise ContractValidationError("coverage", path, "chapter job counts are inconsistent")
    return result


def _validate_chapter_usage(value: Any, *, path: str) -> dict[str, Any]:
    return _validate_usage(value, path=path)


def _validate_usage(value: Any, *, path: str = "$.usage") -> dict[str, Any]:
    row = require_mapping(value, path=path)
    require_exact_keys(row, required={"status", "accounting_basis", "totals", "unknown_attempt_count"}, path=path)
    status = require_enum(row["status"], {"available", "partial", "unavailable", "not_applicable"}, path=f"{path}.status")
    basis = require_enum(row["accounting_basis"], {"provider_reported", "proxy_reported", "local_metered", "mixed", "unavailable"}, path=f"{path}.accounting_basis")
    totals_row = require_mapping(row["totals"], path=f"{path}.totals")
    require_exact_keys(totals_row, required={*_USAGE_NUMERIC_FIELDS, "cost_usd", "currency"}, path=f"{path}.totals")
    totals = {field: _nullable_nonnegative_int(totals_row[field], f"{path}.totals.{field}") for field in _USAGE_NUMERIC_FIELDS}
    totals["cost_usd"] = require_nullable_number(totals_row["cost_usd"], path=f"{path}.totals.cost_usd", minimum=0)
    totals["currency"] = require_nullable_string(totals_row["currency"], path=f"{path}.totals.currency")
    if (totals["cost_usd"] is None) != (totals["currency"] is None):
        raise ContractValidationError("usage_currency", f"{path}.totals", "cost and currency must be present together")
    if status in {"not_applicable", "unavailable"} and (basis != "unavailable" or any(value is not None for value in totals.values())):
        raise ContractValidationError("usage_status", path, "unmeasured usage must remain unknown")
    if status not in {"not_applicable", "unavailable"} and basis == "unavailable":
        raise ContractValidationError("usage_basis", path, "measured usage cannot use unavailable basis")
    return {
        "status": status,
        "accounting_basis": basis,
        "totals": totals,
        "unknown_attempt_count": require_int(row["unknown_attempt_count"], path=f"{path}.unknown_attempt_count", minimum=0),
    }


def _validate_claim(value: Any) -> dict[str, Any]:
    path = "$.claim"
    row = require_mapping(value, path=path)
    require_exact_keys(row, required={"status", "verdict", "reason_codes"}, path=path)
    reasons = [require_string(item, path=f"{path}.reason_codes") for item in require_list(row["reason_codes"], path=f"{path}.reason_codes")]
    require_unique(reasons, path=f"{path}.reason_codes")
    return {
        "status": require_enum(row["status"], {"not_defined"}, path=f"{path}.status"),
        "verdict": require_enum(row["verdict"], {"INCONCLUSIVE"}, path=f"{path}.verdict"),
        "reason_codes": reasons,
    }


def _validate_integrity(value: Any) -> dict[str, str]:
    row = require_mapping(value, path="$.integrity")
    require_exact_keys(row, required={"report_sha256"}, path="$.integrity")
    return {"report_sha256": require_sha256(row["report_sha256"], path="$.integrity.report_sha256")}


def _validate_report_semantics(report: Mapping[str, Any]) -> None:
    coverage = report["coverage"]
    chapters = report["chapter_runs"]
    if (
        coverage["expected_chapter_count"]
        != len(report["identity"]["selected_chapter_ids"])
        or coverage["completed_chapter_count"] != len(chapters)
    ):
        raise ContractValidationError("coverage", "$.coverage", "benchmark chapter counts drift")
    for field in ("planned_job_count", "blocked_job_count", "succeeded_job_count", "failed_job_count"):
        if coverage[field] != sum(row["coverage"][field] for row in chapters):
            raise ContractValidationError("coverage", f"$.coverage.{field}", "benchmark job count drift")
    if coverage["planned_job_count"] != coverage["blocked_job_count"] + coverage["succeeded_job_count"] + coverage["failed_job_count"]:
        raise ContractValidationError("coverage", "$.coverage", "benchmark job counts are inconsistent")
    aggregate_job_rows = [
        job_id for row in report["aggregates"] for job_id in row["source_job_ids"]
    ]
    aggregate_jobs = set(aggregate_job_rows)
    if len(aggregate_job_rows) != len(aggregate_jobs):
        raise ContractValidationError(
            "aggregate_coverage",
            "$.aggregates",
            "one source job cannot contribute to multiple benchmark aggregates",
        )
    if len(aggregate_jobs) != coverage["planned_job_count"]:
        raise ContractValidationError("aggregate_coverage", "$.aggregates", "aggregate source jobs do not exact-cover benchmark jobs")
    aggregate_method_ids = []
    for aggregate in report["aggregates"]:
        method_id = aggregate["method_id"]
        if method_id not in aggregate_method_ids:
            aggregate_method_ids.append(method_id)
    if aggregate_method_ids != report["identity"]["selected_scorer_ids"]:
        raise ContractValidationError(
            "scorer_scope",
            "$.identity.selected_scorer_ids",
            "selected scorer IDs do not match benchmark aggregates",
        )
    if report["claim"]["reason_codes"] != ["no_cross_method_composite"]:
        raise ContractValidationError("claim", "$.claim.reason_codes", "benchmark v1 has no composite claim policy")


def _require_canonical_subset(
    values: Sequence[str],
    *,
    allowed: Sequence[str],
    minimum: int,
    path: str,
) -> None:
    if len(values) < minimum:
        raise ContractValidationError(
            "selection_size", path, f"selection requires at least {minimum} item(s)"
        )
    if len(values) != len(set(values)):
        raise ContractValidationError(
            "selection_duplicate", path, "selection items must be unique"
        )
    positions = {item: index for index, item in enumerate(allowed)}
    if any(item not in positions for item in values):
        raise ContractValidationError(
            "selection_unknown", path, "selection contains an unsupported item"
        )
    if list(sorted(values, key=positions.__getitem__)) != list(values):
        raise ContractValidationError(
            "selection_order", path, "selection must preserve canonical order"
        )


def _chapter_usage_summary(value: Mapping[str, Any]) -> dict[str, Any]:
    row = require_mapping(value, path="$.chapter_report.usage")
    return _validate_usage(
        {
            "status": row["status"],
            "accounting_basis": row["accounting_basis"],
            "totals": copy.deepcopy(row["totals"]),
            "unknown_attempt_count": row["unknown_attempt_count"],
        },
        path="$.chapter_report.usage_summary",
    )


def _validate_producer(value: Any) -> dict[str, str]:
    producer = validate_producer(value, path="$.producer", workstream="evaluation")
    if producer["component"] != "benchmark_aggregate_v1" or producer["component_version"] != SCHEMA_VERSION:
        raise ContractValidationError("producer", "$.producer", "unexpected benchmark report producer")
    return producer


def _comparison_pair(
    baseline: str | None,
    candidate: str | None,
    *,
    selected_arm_ids: Sequence[str],
) -> tuple[str | None, str | None]:
    if (baseline is None) != (candidate is None):
        raise ContractValidationError("comparison_binding", "$", "baseline and candidate must be absent together")
    if baseline is not None:
        if baseline == candidate or baseline not in selected_arm_ids or candidate not in selected_arm_ids:
            raise ContractValidationError("comparison_binding", "$", "comparison arms must be distinct selected arms")
    return baseline, candidate


def _nullable_nonnegative_int(value: Any, path: str) -> int | None:
    if value is None:
        return None
    return require_int(value, path=path, minimum=0)


def _sha256_text(value: str) -> str:
    import hashlib

    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _json_bytes(value: Mapping[str, Any]) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("utf-8") + b"\n"


def _write_bytes_atomic(path: Path, value: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temp.open("xb") as handle:
            handle.write(value)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
    finally:
        if temp.exists():
            temp.unlink()
