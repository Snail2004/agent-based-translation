from __future__ import annotations

import copy
from collections import Counter
from dataclasses import dataclass
import json
import os
from pathlib import Path
from typing import Any, Mapping, Sequence
import uuid

from pipeline.eval.common_input_v1 import CommonEvaluationInputV1
from pipeline.eval.contracts_v1 import (
    ContractValidationError,
    canonical_sha256,
    require_commit,
    require_enum,
    require_exact_keys,
    require_list,
    require_mapping,
    require_nullable_int,
    require_nullable_string,
    require_relative_path,
    require_rfc3339,
    require_sha256,
    require_string,
    require_unique,
    validate_method,
)
from pipeline.eval.execution_runner_v1 import (
    validate_evaluation_execution_artifact,
    validate_evaluation_execution_binding,
)
from pipeline.eval.execution_store_v1 import load_evaluation_execution_bundle_v1
from pipeline.eval.full_run_report_v1 import (
    FULL_RUN_CANONICAL_POLICY,
    seal_full_run_report,
    validate_full_run_report,
)
from pipeline.eval.offline_orchestrator_v1 import (
    build_evaluation_plan,
    validate_evaluation_run_config,
)


__all__ = [
    "FullRunReportWriteResultV1",
    "compose_full_run_report_v1",
    "persist_full_run_report_v1",
]


_FIXED_REPORT_PATH = "reports/full_run_report_v1.json"
@dataclass(frozen=True, slots=True)
class FullRunReportWriteResultV1:
    report_path: Path
    report: dict[str, Any]
    reused: bool


def compose_full_run_report_v1(
    common_input: CommonEvaluationInputV1,
    config_payload: Mapping[str, Any],
    execution_payload: Mapping[str, Any],
    *,
    generated_at: str,
    producer_code_commit: str,
    evaluation_logical_run_id: str,
    evaluation_attempt_run_id: str,
    evaluation_profile_id: str,
    policy_profile_id: str | None,
    input_artifact: Mapping[str, Any],
    arm_presentations: Sequence[Mapping[str, Any]],
    method_presentations: Sequence[Mapping[str, Any]],
    stage_facts: Sequence[Mapping[str, Any]],
    usage_payload: Mapping[str, Any],
    usage_artifacts: Sequence[Mapping[str, Any]] = (),
    caveats: Sequence[str] = (),
) -> dict[str, Any]:
    """Compose FullRunReportV1 from sealed execution plus explicit facts.

    Labels, artifact paths and usage are not recoverable from the common
    read-model, so callers must supply them. This function validates their
    joins and derives only metric/report structure from the sealed execution.
    """

    config = validate_evaluation_run_config(config_payload)
    execution = validate_evaluation_execution_artifact(execution_payload)
    plan = build_evaluation_plan(common_input, config)
    validate_evaluation_execution_binding(execution, common_input, plan)
    commit = require_commit(producer_code_commit, path="$.producer_code_commit")
    logical_run_id = require_string(
        evaluation_logical_run_id, path="$.evaluation_logical_run_id"
    )
    attempt_run_id = require_string(
        evaluation_attempt_run_id, path="$.evaluation_attempt_run_id"
    )
    profile_id = require_string(
        evaluation_profile_id, path="$.evaluation_profile_id"
    )
    policy_id = require_nullable_string(
        policy_profile_id, path="$.policy_profile_id"
    )

    input_row = _validate_input_artifact(input_artifact)
    arm_rows, translation_artifacts = _compose_arms(
        common_input, arm_presentations
    )
    method_rows = _validate_method_presentations(
        method_presentations, execution
    )
    usage_rows = _validate_usage_artifact_rows(usage_artifacts)
    usage = copy.deepcopy(dict(require_mapping(usage_payload, path="$.usage")))
    usage_status = require_enum(
        usage.get("status"),
        {"available", "partial", "unavailable", "not_applicable"},
        path="$.usage.status",
    )
    declared_usage_ids = [row["artifact_id"] for row in usage_rows]
    supplied_usage_ids = [
        require_string(value, path=f"$.usage.source_artifact_ids[{index}]")
        for index, value in enumerate(
            require_list(
                usage.get("source_artifact_ids"),
                path="$.usage.source_artifact_ids",
            )
        )
    ]
    if len(supplied_usage_ids) != len(set(supplied_usage_ids)) or set(
        supplied_usage_ids
    ) != set(declared_usage_ids):
        raise ContractValidationError(
            "usage_provenance",
            "$.usage.source_artifact_ids",
            "usage facts must reference the exact declared usage artifact set",
        )
    if usage_status in {"available", "partial"} and not declared_usage_ids:
        raise ContractValidationError(
            "usage_provenance",
            "$.usage.source_artifact_ids",
            "available usage requires at least one persisted usage artifact",
        )

    execution_sha256 = execution["integrity"]["artifact_sha256"]
    execution_artifact_id = f"evaluation-execution-{execution_sha256[:24]}"
    execution_artifact = {
        "artifact_id": execution_artifact_id,
        "kind": "metric_report",
        "requirement": "required",
        "status": "present",
        "relative_path": f"execution/{execution_sha256}.json",
        "sha256": execution_sha256,
        "producer_method_id": "evaluation_execution",
        "error_code": None,
    }
    artifacts = [
        input_row,
        *translation_artifacts,
        execution_artifact,
        *usage_rows,
    ]
    require_unique(
        [row["artifact_id"] for row in artifacts], path="$.artifacts.artifact_id"
    )
    require_unique(
        [row["relative_path"] for row in artifacts],
        path="$.artifacts.relative_path",
    )

    metrics = _compose_metrics(execution, method_rows, execution_artifact_id)
    stages, stage_methods = _validate_stage_facts(
        stage_facts,
        execution=execution,
        execution_artifact_id=execution_artifact_id,
        usage_artifact_ids=declared_usage_ids,
    )
    _require_usage_stage_exact_cover(usage, [row["stage_id"] for row in stages])
    _validate_method_model_evidence(method_rows, stage_methods, usage)
    report_state = _report_state(execution)
    claim = _compose_claim(metrics, arm_count=len(arm_rows))
    report_caveats = [
        require_string(value, path=f"$.caveats[{index}]")
        for index, value in enumerate(caveats)
    ]
    if len(arm_rows) == 1:
        report_caveats.append("A single arm cannot support a comparative claim.")
    else:
        report_caveats.append(
            "The comparative claim policy is not frozen; no winner is published."
        )
    if any(row["status"] != "available" for row in execution["aggregates"]):
        report_caveats.append(
            "At least one metric has incomplete or failed planned-job coverage."
        )

    attempt_ids = _ordered_unique(
        [arm.attempt_run_id for arm in common_input.arms] + [attempt_run_id]
    )
    artifact_set_sha256 = canonical_sha256(
        {"artifacts": artifacts}, policy=FULL_RUN_CANONICAL_POLICY
    )
    report = seal_full_run_report(
        {
            "schema_id": "FullRunReportV1",
            "schema_version": "1.0.0",
            "report_id": f"full-run-report-{execution_sha256[:24]}",
            "generated_at": generated_at,
            "producer": {
                "workstream": "evaluation",
                "component": "full_run_report_writer_v1",
                "component_version": "1.0.0",
                "code_commit": commit,
            },
            "report_method": {
                "method_id": "full_run_report",
                "method_version": "1.0.0",
                "policy_profile_id": policy_id,
            },
            "identity": {
                "project_id": common_input.project_id,
                "logical_run_id": logical_run_id,
                "attempt_run_ids": attempt_ids,
                "document_id": common_input.document_id,
                "profile_id": profile_id,
                "input_manifest_sha256": input_row["sha256"],
            },
            "integrity": {
                "evaluation_config_sha256": config["integrity"]["config_sha256"],
                "artifact_set_sha256": artifact_set_sha256,
                "source_usage_artifact_ids": declared_usage_ids,
                "report_sha256": "0" * 64,
            },
            "report_state": report_state,
            "arms": arm_rows,
            "metrics": metrics,
            "claim": claim,
            "usage": usage,
            "stages": stages,
            "artifacts": artifacts,
            "caveats": report_caveats,
        }
    )
    return validate_full_run_report(report)


def persist_full_run_report_v1(
    *, output_root: Path, report_payload: Mapping[str, Any]
) -> FullRunReportWriteResultV1:
    root = _prepare_root(output_root)
    bundle = load_evaluation_execution_bundle_v1(output_root=root)
    report = validate_full_run_report(report_payload)
    _validate_report_bundle_join(report, bundle.manifest, bundle.config, bundle.execution)
    _require_present_artifact_paths(root, report)
    report_path = _contained_path(root, _FIXED_REPORT_PATH)
    encoded = _canonical_json_bytes(report)
    if report_path.exists():
        if report_path.read_bytes() != encoded:
            raise ContractValidationError(
                "immutable_conflict",
                str(report_path),
                "existing FullRunReportV1 differs from requested canonical bytes",
            )
        return FullRunReportWriteResultV1(report_path, report, True)
    created = _publish_bytes_create_only(report_path, encoded)
    return FullRunReportWriteResultV1(report_path, report, not created)


def _validate_input_artifact(value: Mapping[str, Any]) -> dict[str, Any]:
    path = "$.input_artifact"
    row = require_mapping(value, path=path)
    require_exact_keys(
        row, required={"artifact_id", "relative_path", "sha256"}, path=path
    )
    return {
        "artifact_id": require_string(row["artifact_id"], path=f"{path}.artifact_id"),
        "kind": "evaluation_input",
        "requirement": "required",
        "status": "present",
        "relative_path": require_relative_path(
            row["relative_path"], path=f"{path}.relative_path"
        ),
        "sha256": require_sha256(row["sha256"], path=f"{path}.sha256"),
        "producer_method_id": "input_adapter",
        "error_code": None,
    }


def _compose_arms(
    common_input: CommonEvaluationInputV1,
    values: Sequence[Mapping[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    rows = require_list(list(values), path="$.arm_presentations")
    presentations: dict[str, dict[str, str]] = {}
    for index, value in enumerate(rows):
        path = f"$.arm_presentations[{index}]"
        row = require_mapping(value, path=path)
        require_exact_keys(
            row,
            required={"arm_id", "role", "kind", "label", "relative_path"},
            path=path,
        )
        arm_id = require_string(row["arm_id"], path=f"{path}.arm_id")
        if arm_id in presentations:
            raise ContractValidationError("duplicate", path, "duplicate arm presentation")
        presentations[arm_id] = {
            "arm_id": arm_id,
            "role": require_enum(
                row["role"],
                {"baseline", "candidate", "reference", "external_baseline"},
                path=f"{path}.role",
            ),
            "kind": require_enum(
                row["kind"],
                {"system", "human_reference", "machine_baseline"},
                path=f"{path}.kind",
            ),
            "label": require_string(row["label"], path=f"{path}.label"),
            "relative_path": require_relative_path(
                row["relative_path"], path=f"{path}.relative_path"
            ),
        }
    expected_ids = [arm.arm_id for arm in common_input.arms]
    if set(presentations) != set(expected_ids):
        raise ContractValidationError(
            "arm_exact_cover",
            "$.arm_presentations",
            "arm presentations must exact-cover common input arms",
        )
    arm_rows: list[dict[str, Any]] = []
    artifacts: list[dict[str, Any]] = []
    artifact_kind = {
        "system": "translation",
        "human_reference": "human_reference",
        "machine_baseline": "machine_baseline",
    }
    for arm in common_input.arms:
        presentation = presentations[arm.arm_id]
        arm_rows.append(
            {
                "arm_id": arm.arm_id,
                "role": presentation["role"],
                "kind": presentation["kind"],
                "label": presentation["label"],
                "translation_artifact_id": arm.artifact_id,
                "translation_sha256": arm.artifact_sha256,
            }
        )
        artifacts.append(
            {
                "artifact_id": arm.artifact_id,
                "kind": artifact_kind[presentation["kind"]],
                "requirement": "required",
                "status": "present",
                "relative_path": presentation["relative_path"],
                "sha256": arm.artifact_sha256,
                "producer_method_id": None,
                "error_code": None,
            }
        )
    return arm_rows, artifacts


def _validate_method_presentations(
    values: Sequence[Mapping[str, Any]], execution: Mapping[str, Any]
) -> dict[str, dict[str, Any]]:
    rows = require_list(list(values), path="$.method_presentations")
    result: dict[str, dict[str, Any]] = {}
    for index, value in enumerate(rows):
        path = f"$.method_presentations[{index}]"
        row = require_mapping(value, path=path)
        require_exact_keys(
            row, required={"display_name", "method"}, path=path
        )
        method = validate_method(row["method"], path=f"{path}.method")
        method_id = method["method_id"]
        if method_id in result:
            raise ContractValidationError("duplicate", path, "duplicate method presentation")
        result[method_id] = {
            "display_name": require_string(
                row["display_name"], path=f"{path}.display_name"
            ),
            "method": method,
        }
    expected_versions: dict[str, set[str]] = {}
    for aggregate in execution["aggregates"]:
        expected_versions.setdefault(aggregate["method_id"], set()).add(
            aggregate["method_version"]
        )
    if set(result) != set(expected_versions):
        raise ContractValidationError(
            "method_exact_cover",
            "$.method_presentations",
            "method presentations must exact-cover execution methods",
        )
    for method_id, versions in expected_versions.items():
        if versions != {result[method_id]["method"]["method_version"]}:
            raise ContractValidationError(
                "method_version",
                "$.method_presentations",
                "published method version differs from execution aggregate",
            )
    return result


def _validate_usage_artifact_rows(
    values: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    rows = require_list(list(values), path="$.usage_artifacts")
    result: list[dict[str, Any]] = []
    for index, value in enumerate(rows):
        path = f"$.usage_artifacts[{index}]"
        row = require_mapping(value, path=path)
        require_exact_keys(
            row, required={"artifact_id", "relative_path", "sha256"}, path=path
        )
        result.append(
            {
                "artifact_id": require_string(
                    row["artifact_id"], path=f"{path}.artifact_id"
                ),
                "kind": "usage_ledger",
                "requirement": "required",
                "status": "present",
                "relative_path": require_relative_path(
                    row["relative_path"], path=f"{path}.relative_path"
                ),
                "sha256": require_sha256(row["sha256"], path=f"{path}.sha256"),
                "producer_method_id": None,
                "error_code": None,
            }
        )
    require_unique([row["artifact_id"] for row in result], path="$.usage_artifacts")
    return result


def _compose_metrics(
    execution: Mapping[str, Any],
    methods: Mapping[str, Mapping[str, Any]],
    execution_artifact_id: str,
) -> list[dict[str, Any]]:
    counts = Counter(row["method_id"] for row in execution["aggregates"])
    result: list[dict[str, Any]] = []
    for aggregate in execution["aggregates"]:
        method_id = aggregate["method_id"]
        metric_id = (
            method_id if counts[method_id] == 1 else aggregate["aggregate_id"]
        )
        measured = aggregate["observed_job_count"] > 0
        status = "available" if measured else "failed"
        arm_values = []
        for value in aggregate["arm_values"]:
            arm_values.append(
                {
                    "arm_id": value["arm_id"],
                    "value": value["value"] if measured else None,
                    "numerator": value["numerator"] if measured else None,
                    "denominator": value["denominator"] if measured else None,
                    "interval_low": None,
                    "interval_high": None,
                    "interval_level": None,
                }
            )
        comparison = _compose_metric_comparison(
            aggregate, measured=measured, arm_count=len(execution["binding"]["selected_arm_ids"])
        )
        caveats = list(aggregate["caveats"])
        if aggregate["status"] == "partial":
            caveats.append(
                f"Coverage is partial: {aggregate['observed_job_count']} of "
                f"{aggregate['expected_job_count']} planned jobs produced observations."
            )
        result.append(
            {
                "metric_id": metric_id,
                "display_name": methods[method_id]["display_name"],
                "profile_scope": "common",
                "status": status,
                "unit": aggregate["unit"],
                "direction": "higher_is_better",
                "method": methods[method_id]["method"],
                "arm_values": arm_values,
                "comparison": comparison,
                "source_artifact_ids": [execution_artifact_id],
                "caveats": caveats,
            }
        )
    return result


def _compose_metric_comparison(
    aggregate: Mapping[str, Any], *, measured: bool, arm_count: int
) -> dict[str, Any]:
    source = aggregate["comparison"]
    if not measured:
        if arm_count == 1:
            return {
                "status": "not_applicable",
                "baseline_arm_id": None,
                "candidate_arm_id": None,
                "delta": None,
                "wins": None,
                "ties": None,
                "losses": None,
            }
        return {
            "status": "insufficient",
            "baseline_arm_id": source["baseline_arm_id"],
            "candidate_arm_id": source["candidate_arm_id"],
            "delta": None,
            "wins": None,
            "ties": None,
            "losses": None,
        }
    return {
        "status": source["status"],
        "baseline_arm_id": source["baseline_arm_id"],
        "candidate_arm_id": source["candidate_arm_id"],
        "delta": source["delta"],
        "wins": source["wins"],
        "ties": source["ties"],
        "losses": source["losses"],
    }


def _require_usage_stage_exact_cover(
    usage: Mapping[str, Any], expected_stage_ids: Sequence[str]
) -> None:
    by_stage = require_list(usage.get("by_stage"), path="$.usage.by_stage")
    observed = [
        require_string(
            require_mapping(row, path=f"$.usage.by_stage[{index}]").get("stage_id"),
            path=f"$.usage.by_stage[{index}].stage_id",
        )
        for index, row in enumerate(by_stage)
    ]
    if observed != list(expected_stage_ids):
        raise ContractValidationError(
            "usage_stage_exact_cover",
            "$.usage.by_stage",
            "usage stages must exact-cover persisted stage facts in the same order",
        )


def _validate_stage_facts(
    values: Sequence[Mapping[str, Any]],
    *,
    execution: Mapping[str, Any],
    execution_artifact_id: str,
    usage_artifact_ids: Sequence[str],
) -> tuple[list[dict[str, Any]], dict[str, str]]:
    rows = require_list(list(values), path="$.stage_facts")
    result: list[dict[str, Any]] = []
    stage_methods: dict[str, str] = {}
    expected_methods = {row["method_id"] for row in execution["aggregates"]}
    observed_methods: set[str] = set()
    for index, value in enumerate(rows):
        path = f"$.stage_facts[{index}]"
        row = require_mapping(value, path=path)
        require_exact_keys(
            row,
            required={
                "stage_id",
                "method_id",
                "status",
                "started_at",
                "ended_at",
                "duration_ms",
                "attempt_run_id",
                "error_code",
            },
            path=path,
        )
        stage_id = require_string(row["stage_id"], path=f"{path}.stage_id")
        method_id = require_enum(
            row["method_id"], expected_methods, path=f"{path}.method_id"
        )
        if stage_id in stage_methods:
            raise ContractValidationError("duplicate", path, "duplicate stage fact")
        started_at = _nullable_timestamp(row["started_at"], path=f"{path}.started_at")
        ended_at = _nullable_timestamp(row["ended_at"], path=f"{path}.ended_at")
        duration_ms = require_nullable_int(
            row["duration_ms"], path=f"{path}.duration_ms", minimum=0
        )
        if any(value is None for value in (started_at, ended_at, duration_ms)) and any(
            value is not None for value in (started_at, ended_at, duration_ms)
        ):
            raise ContractValidationError(
                "stage_time",
                path,
                "stage timestamps and duration must be all null or all present",
            )
        stage_methods[stage_id] = method_id
        observed_methods.add(method_id)
        result.append(
            {
                "stage_id": stage_id,
                "status": require_enum(
                    row["status"],
                    {"complete", "partial", "failed", "not_run", "not_applicable"},
                    path=f"{path}.status",
                ),
                "started_at": started_at,
                "ended_at": ended_at,
                "duration_ms": duration_ms,
                "attempt_run_id": require_nullable_string(
                    row["attempt_run_id"], path=f"{path}.attempt_run_id"
                ),
                "artifact_ids": [execution_artifact_id, *usage_artifact_ids],
                "error_code": require_nullable_string(
                    row["error_code"], path=f"{path}.error_code"
                ),
            }
        )
    if observed_methods != expected_methods:
        raise ContractValidationError(
            "stage_method_exact_cover",
            "$.stage_facts",
            "stage facts must cover every execution method at least once",
        )
    return result, stage_methods


def _validate_method_model_evidence(
    methods: Mapping[str, Mapping[str, Any]],
    stage_methods: Mapping[str, str],
    usage: Mapping[str, Any],
) -> None:
    evidence: dict[str, set[str]] = {method_id: set() for method_id in methods}
    for row in usage["by_stage"]:
        model_id = row["model_id"]
        if model_id is not None:
            evidence[stage_methods[row["stage_id"]]].add(model_id)
    for method_id, presentation in methods.items():
        declared = presentation["method"]["model_id"]
        observed = evidence[method_id]
        if declared is not None and observed != {declared}:
            raise ContractValidationError(
                "method_model_evidence",
                "$.method_presentations",
                "published method model must match one exact persisted usage model",
            )
        if len(observed) == 1 and declared != next(iter(observed)):
            raise ContractValidationError(
                "method_model_evidence",
                "$.method_presentations",
                "a unique persisted usage model must be published on the method row",
            )
        if len(observed) > 1 and declared is not None:
            raise ContractValidationError(
                "method_model_evidence",
                "$.method_presentations",
                "multi-model methods cannot publish one misleading model ID",
            )


def _nullable_timestamp(value: Any, *, path: str) -> str | None:
    if value is None:
        return None
    return require_rfc3339(value, path=path)


def _report_state(execution: Mapping[str, Any]) -> str:
    statuses = [row["status"] for row in execution["aggregates"]]
    if statuses and all(status == "available" for status in statuses):
        return "complete"
    if any(status in {"available", "partial"} for status in statuses):
        return "partial"
    return "failed"


def _compose_claim(metrics: Sequence[Mapping[str, Any]], *, arm_count: int) -> dict[str, Any]:
    metric_ids = [row["metric_id"] for row in metrics]
    if arm_count == 1:
        return {
            "status": "not_applicable",
            "verdict": "NOT_APPLICABLE",
            "method_id": "claim_gate",
            "method_version": "1.0.0",
            "reason_codes": ["single_arm"],
            "source_metric_ids": metric_ids,
        }
    return {
        "status": "insufficient",
        "verdict": "INCONCLUSIVE",
        "method_id": "claim_gate",
        "method_version": "1.0.0",
        "reason_codes": ["claim_policy_not_frozen"],
        "source_metric_ids": metric_ids,
    }


def _validate_report_bundle_join(
    report: Mapping[str, Any],
    manifest: Mapping[str, Any],
    config: Mapping[str, Any],
    execution: Mapping[str, Any],
) -> None:
    if report["integrity"]["evaluation_config_sha256"] != config["integrity"]["config_sha256"]:
        raise ContractValidationError(
            "report_bundle_binding",
            "$.integrity.evaluation_config_sha256",
            "report references another evaluation config",
        )
    if report["identity"]["project_id"] != manifest["binding"]["project_id"]:
        raise ContractValidationError(
            "report_bundle_binding", "$.identity.project_id", "foreign report project"
        )
    if report["identity"]["document_id"] != manifest["binding"]["document_id"]:
        raise ContractValidationError(
            "report_bundle_binding", "$.identity.document_id", "foreign report document"
        )
    execution_sha256 = execution["integrity"]["artifact_sha256"]
    expected_path = f"execution/{execution_sha256}.json"
    matches = [
        row
        for row in report["artifacts"]
        if row["sha256"] == execution_sha256
        and row["relative_path"] == expected_path
        and row["kind"] == "metric_report"
    ]
    if len(matches) != 1:
        raise ContractValidationError(
            "report_bundle_binding",
            "$.artifacts",
            "report must reference the exact persisted execution artifact once",
        )


def _require_present_artifact_paths(root: Path, report: Mapping[str, Any]) -> None:
    for index, artifact in enumerate(report["artifacts"]):
        if artifact["status"] != "present":
            continue
        path = _contained_path(root, artifact["relative_path"])
        if not path.is_file():
            raise ContractValidationError(
                "missing_artifact",
                f"$.artifacts[{index}].relative_path",
                "report cannot publish a present artifact whose file is absent",
            )


def _prepare_root(path: Path) -> Path:
    root = Path(path)
    if not root.exists() or not root.is_dir():
        raise ContractValidationError(
            "output_root", str(root), "evaluation run root does not exist"
        )
    return root.resolve()


def _contained_path(root: Path, relative_path: str) -> Path:
    normalized = require_relative_path(relative_path, path="$.relative_path")
    candidate = (root / Path(*normalized.split("/"))).resolve()
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise ContractValidationError(
            "path_escape", str(candidate), "report artifact path escapes run root"
        ) from exc
    return candidate


def _canonical_json_bytes(payload: Mapping[str, Any]) -> bytes:
    try:
        return (
            json.dumps(
                payload,
                ensure_ascii=False,
                allow_nan=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n"
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ContractValidationError(
            "json_encoding", "$", "FullRunReportV1 must be finite JSON"
        ) from exc


def _publish_bytes_create_only(path: Path, encoded: bytes) -> bool:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if path.read_bytes() == encoded:
            return False
        raise ContractValidationError(
            "immutable_conflict", str(path), "refusing to overwrite FullRunReportV1"
        )
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
            raise ContractValidationError(
                "immutable_conflict",
                str(path),
                "concurrent writer published another FullRunReportV1",
            )
        except OSError as exc:
            raise ContractValidationError(
                "atomic_publish",
                str(path),
                "filesystem cannot publish FullRunReportV1 atomically",
            ) from exc
        return True
    finally:
        if temporary.exists():
            temporary.unlink()


def _ordered_unique(values: Sequence[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        if value not in seen:
            result.append(value)
            seen.add(value)
    return result
