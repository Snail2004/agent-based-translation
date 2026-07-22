from __future__ import annotations

import copy
from dataclasses import dataclass
from datetime import datetime
import json
import math
import os
from pathlib import Path, PurePosixPath
import tempfile
from typing import Any, Callable, Mapping

from pipeline.eval.common_input_v1 import CommonEvaluationInputV1
from pipeline.eval.contracts_v1 import ContractValidationError
from pipeline.eval.live_pilot_profile_v1 import (
    build_evaluation_live_pilot_profile_v1,
)
from pipeline.eval.live_pilot_runner_v1 import (
    execute_evaluation_live_pilot_v1,
    validate_evaluation_live_pilot_execution_binding,
)
from pipeline.eval.live_pilot_sf_qe_v1 import (
    PilotCometKiwiPredictorV1,
    prepare_evaluation_live_pilot_sf_qe_v1,
    validate_pilot_local_sf_qe_binding_v1,
)
from pipeline.eval.method_executors_v1 import (
    EvaluationMethodExecutorV1,
    SharedEvaluationRoleRunnerV1,
)
from pipeline.llm_backend import (
    ApplicationResponseCache,
    ContentAddressedArtifactStore,
    PhysicalQuotaScheduler,
    SharedLlmAttemptLedger,
    SharedLlmBackend,
    canonical_json,
    canonical_sha256,
    resolve_source_credential,
)
from pipeline.llm_backend.credentials_v1 import CredentialProvider
from pipeline.llm_backend.transport_v1 import TransportSender


__all__ = [
    "EvaluationLivePilotRunResultV1",
    "run_evaluation_live_pilot_execution_v1",
]


_PROFILE_NAME = "profile.json"
_LOCAL_SF_QE_NAME = "local_sf_qe_binding.json"
_EXECUTION_NAME = "execution.json"
_HALT_NAME = "halt.json"
_STATE_NAME = "_state"
_ALLOWED_ROOT_NAMES = frozenset(
    {
        _PROFILE_NAME,
        _LOCAL_SF_QE_NAME,
        _EXECUTION_NAME,
        _HALT_NAME,
        _STATE_NAME,
        ".gitattributes",
        "RUN_HALTED.md",
        "RUN_SUCCESS.md",
    }
)


@dataclass(frozen=True, slots=True)
class EvaluationLivePilotRunResultV1:
    output_root: Path
    profile_path: Path
    local_sf_qe_path: Path
    execution_path: Path
    profile: dict[str, Any]
    local_sf_qe_binding: dict[str, Any]
    execution: dict[str, Any]
    reused_complete_run: bool


def run_evaluation_live_pilot_execution_v1(
    common_input: CommonEvaluationInputV1,
    config_payload: Mapping[str, Any],
    preflight_payload: Mapping[str, Any],
    *,
    api_source: Mapping[str, Any],
    capabilities_by_role: Mapping[str, Mapping[str, Any]],
    credential_provider: CredentialProvider,
    sender: TransportSender,
    sf_qe_predictor: PilotCometKiwiPredictorV1,
    output_base_root: Path,
    output_root_relative: str,
    created_at: str,
    producer_code_commit: str,
    profile_id: str,
    profile_revision: str,
    evaluation_logical_run_id: str,
    evaluation_attempt_run_id: str,
    sf_qe_batch_size: int = 8,
    clock: Callable[[], datetime] | None = None,
    structured_output_mode: str = "preferred",
) -> EvaluationLivePilotRunResultV1:
    """Run or exactly replay one sealed versioned live pilot.

    This orchestrator chooses no provider, model, capability, credential row,
    prompt, schema, or scorer policy. Every such value is supplied through
    already validated records. The shared backend remains the sole owner of a
    physical provider attempt and its durable usage/cache evidence.
    """

    root = _prepare_output_root(
        _resolve_output_root(output_base_root, output_root_relative)
    )
    _persist_bytes_create_or_equal(
        root / ".gitattributes", b"*.json text eol=lf\n*.md text eol=lf\n"
    )
    profile_path = root / _PROFILE_NAME
    local_sf_qe_path = root / _LOCAL_SF_QE_NAME
    execution_path = root / _EXECUTION_NAME
    halt_path = root / _HALT_NAME

    bundle = build_evaluation_live_pilot_profile_v1(
        common_input,
        config_payload,
        preflight_payload,
        api_source,
        capabilities_by_role,
        created_at=created_at,
        producer_code_commit=producer_code_commit,
        profile_id=profile_id,
        profile_revision=profile_revision,
        evaluation_logical_run_id=evaluation_logical_run_id,
        evaluation_attempt_run_id=evaluation_attempt_run_id,
        output_root_relative=output_root_relative,
        cache_mode="read_write",
        structured_output_mode=structured_output_mode,
    )
    _persist_create_or_equal(profile_path, bundle.artifact)

    if halt_path.exists():
        if not local_sf_qe_path.is_file():
            raise ContractValidationError(
                "pilot_partial_artifact",
                str(local_sf_qe_path),
                "halted pilot lacks its bound local SF-QE artifact",
            )
        halted_local_binding = validate_pilot_local_sf_qe_binding_v1(
            _load_json_object(local_sf_qe_path)
        )
        _validate_halt_marker_binding(
            _load_json_object(halt_path),
            profile_artifact=bundle.artifact,
            local_sf_qe_binding=halted_local_binding,
            preflight_payload=preflight_payload,
            evaluation_logical_run_id=evaluation_logical_run_id,
            evaluation_attempt_run_id=evaluation_attempt_run_id,
            output_root_relative=output_root_relative,
        )
        raise ContractValidationError(
            "pilot_halted_root",
            str(halt_path),
            "pilot root is terminally halted and cannot retry its sealed attempt",
        )

    if execution_path.exists():
        if not local_sf_qe_path.is_file():
            raise ContractValidationError(
                "pilot_partial_artifact",
                str(local_sf_qe_path),
                "completed execution lacks its bound local SF-QE artifact",
            )
        local_binding = validate_pilot_local_sf_qe_binding_v1(
            _load_json_object(local_sf_qe_path)
        )
        execution = validate_evaluation_live_pilot_execution_binding(
            _load_json_object(execution_path),
            common_input,
            config_payload,
            preflight_payload,
            evaluation_logical_run_id=evaluation_logical_run_id,
            evaluation_attempt_run_id=evaluation_attempt_run_id,
            evaluation_profile_id=bundle.profile["profile_id"],
            evaluation_profile_sha256=bundle.artifact["binding"][
                "profile_sha256"
            ],
            local_sf_qe_binding=local_binding,
        )
        return EvaluationLivePilotRunResultV1(
            output_root=root,
            profile_path=profile_path,
            local_sf_qe_path=local_sf_qe_path,
            execution_path=execution_path,
            profile=copy.deepcopy(bundle.artifact),
            local_sf_qe_binding=copy.deepcopy(local_binding),
            execution=copy.deepcopy(execution),
            reused_complete_run=True,
        )

    resolve_source_credential(
        source=bundle.api_source,
        provider=credential_provider,
    )
    prepared_sf_qe = prepare_evaluation_live_pilot_sf_qe_v1(
        common_input,
        config_payload,
        preflight_payload,
        sf_qe_predictor,
        batch_size=sf_qe_batch_size,
    )
    local_binding = prepared_sf_qe.execution_binding
    _persist_create_or_equal(local_sf_qe_path, local_binding)

    state_root = root / _STATE_NAME
    artifact_store = ContentAddressedArtifactStore(state_root / "raw_responses")
    response_cache = ApplicationResponseCache(
        index_path=state_root / "response_cache.sqlite3",
        artifact_store=artifact_store,
    )
    ledger = SharedLlmAttemptLedger(state_root / "attempt_ledger.sqlite3")
    backend = SharedLlmBackend(
        credential_provider=credential_provider,
        scheduler=PhysicalQuotaScheduler(state_root / "quota_leases"),
        ledger=ledger,
        response_cache=response_cache,
        sender=sender,
        clock=clock,
    )
    roles = SharedEvaluationRoleRunnerV1(
        backend=backend,
        profile=bundle.profile,
        api_sources=[bundle.api_source],
        capability_evidence=bundle.capabilities,
        run_id=evaluation_logical_run_id,
        attempt_run_id=evaluation_attempt_run_id,
        cache_mode="read_write",
    )
    executor = EvaluationMethodExecutorV1(
        common_input=common_input,
        config_payload=config_payload,
        sf_qe_scorer=prepared_sf_qe,
        llm_roles=roles,
        created_at=created_at,
        producer_code_commit=producer_code_commit,
    )
    try:
        execution = execute_evaluation_live_pilot_v1(
            common_input,
            config_payload,
            preflight_payload,
            executor,
            created_at=created_at,
            runner_code_commit=producer_code_commit,
        )
    except Exception as exc:
        halt_marker = _build_halt_marker(
            ledger=ledger,
            profile_artifact=bundle.artifact,
            local_sf_qe_binding=local_binding,
            preflight_payload=preflight_payload,
            evaluation_logical_run_id=evaluation_logical_run_id,
            evaluation_attempt_run_id=evaluation_attempt_run_id,
            output_root_relative=output_root_relative,
            exception=exc,
        )
        _persist_create_or_equal(halt_path, halt_marker)
        raise
    _persist_create_or_equal(execution_path, execution)
    return EvaluationLivePilotRunResultV1(
        output_root=root,
        profile_path=profile_path,
        local_sf_qe_path=local_sf_qe_path,
        execution_path=execution_path,
        profile=copy.deepcopy(bundle.artifact),
        local_sf_qe_binding=copy.deepcopy(local_binding),
        execution=copy.deepcopy(execution),
        reused_complete_run=False,
    )


def _prepare_output_root(path: Path) -> Path:
    root = Path(path).resolve()
    if root.exists() and not root.is_dir():
        raise ContractValidationError(
            "output_root", str(root), "pilot output root is not a directory"
        )
    root.mkdir(parents=True, exist_ok=True)
    names = {item.name for item in root.iterdir()}
    unknown = sorted(names - _ALLOWED_ROOT_NAMES)
    if unknown:
        raise ContractValidationError(
            "output_root_contents",
            str(root),
            f"pilot output root contains foreign entries: {unknown}",
        )
    if _PROFILE_NAME not in names and names - {".gitattributes"}:
        raise ContractValidationError(
            "pilot_partial_artifact",
            str(root),
            "pilot state exists without its sealed profile",
        )
    if _EXECUTION_NAME in names and _LOCAL_SF_QE_NAME not in names:
        raise ContractValidationError(
            "pilot_partial_artifact",
            str(root),
            "pilot execution exists without local SF-QE binding",
        )
    if _HALT_NAME in names and _EXECUTION_NAME in names:
        raise ContractValidationError(
            "pilot_terminal_state",
            str(root),
            "pilot root cannot contain both halted and completed execution states",
        )
    return root


def _build_halt_marker(
    *,
    ledger: SharedLlmAttemptLedger,
    profile_artifact: Mapping[str, Any],
    local_sf_qe_binding: Mapping[str, Any],
    preflight_payload: Mapping[str, Any],
    evaluation_logical_run_id: str,
    evaluation_attempt_run_id: str,
    output_root_relative: str,
    exception: Exception,
) -> dict[str, Any]:
    usage_rows = ledger.list_records("usage")
    error_rows = ledger.list_records("error")
    succeeded_rows = [row for row in usage_rows if row["outcome"] == "succeeded"]
    failed_rows = [row for row in usage_rows if row["outcome"] != "succeeded"]
    expected_calls = int(
        preflight_payload["workload"]["physical_call_counts"]["total_api_calls"]
    )
    recorded_calls = len(usage_rows)
    terminal_error = _terminal_error_summary(
        usage_rows=usage_rows,
        failed_rows=failed_rows,
        error_rows=error_rows,
        exception=exception,
    )
    halted_at = terminal_error["occurred_at"]
    marker = {
        "schema_id": "EvaluationLivePilotHaltV1",
        "schema_version": "1.0.0",
        "status": "halted",
        "halted_at": halted_at,
        "binding": {
            "logical_run_id": evaluation_logical_run_id,
            "attempt_run_id": evaluation_attempt_run_id,
            "profile_id": profile_artifact["profile"]["profile_id"],
            "profile_sha256": profile_artifact["binding"]["profile_sha256"],
            "preflight_sha256": preflight_payload["integrity"]["preflight_sha256"],
            "local_sf_qe_score_set_sha256": local_sf_qe_binding[
                "score_set_sha256"
            ],
            "output_root_relative": output_root_relative,
        },
        "progress": {
            "expected_physical_call_count": expected_calls,
            "recorded_physical_attempt_count": recorded_calls,
            "succeeded_physical_attempt_count": len(succeeded_rows),
            "failed_physical_attempt_count": len(failed_rows),
            "unattempted_physical_call_count": max(expected_calls - recorded_calls, 0),
            "unfinished_physical_call_count": max(
                expected_calls - len(succeeded_rows), 0
            ),
            "known_prompt_tokens": sum(
                row["prompt_tokens"] or 0 for row in succeeded_rows
            ),
            "known_completion_tokens": sum(
                row["completion_tokens"] or 0 for row in succeeded_rows
            ),
            "known_total_tokens": sum(
                row["total_tokens"] or 0 for row in succeeded_rows
            ),
            "execution_published": False,
            "publishable": False,
        },
        "terminal_error": terminal_error,
        "integrity": {"halt_sha256": "0" * 64},
    }
    marker["integrity"]["halt_sha256"] = canonical_sha256(marker)
    return marker


def _terminal_error_summary(
    *,
    usage_rows: list[dict[str, Any]],
    failed_rows: list[dict[str, Any]],
    error_rows: list[dict[str, Any]],
    exception: Exception,
) -> dict[str, Any]:
    error_by_id = {row["error_id"]: row for row in error_rows}
    failed_with_time = sorted(
        failed_rows, key=lambda row: (row["finished_at_utc"], row["attempt_usage_id"])
    )
    if failed_with_time:
        failed = failed_with_time[-1]
        error = error_by_id.get(failed["error_id"])
        if error is not None:
            return {
                "source": "shared_attempt_ledger",
                "occurred_at": error["occurred_at_utc"],
                "category": error["category"],
                "code": error["code"],
                "retry_class": error["retry_class"],
                "retry_disposition": error["retry_disposition"],
                "safe_message": error["safe_message"],
                "source_health_effect": error["source_health_effect"],
                "attempt_usage_id": failed["attempt_usage_id"],
                "logical_request_id": failed["logical_request_id"],
            }

    occurred_at = max(
        (row["finished_at_utc"] for row in usage_rows),
        default="1970-01-01T00:00:00Z",
    )
    code = (
        exception.code
        if isinstance(exception, ContractValidationError)
        else type(exception).__name__
    )
    return {
        "source": "local_execution",
        "occurred_at": occurred_at,
        "category": "semantic_or_local",
        "code": code,
        "retry_class": "not_classified",
        "retry_disposition": "do_not_retry",
        "safe_message": "pilot execution failed after local SF-QE preparation",
        "source_health_effect": "none",
        "attempt_usage_id": None,
        "logical_request_id": None,
    }


def _validate_halt_marker_binding(
    value: Mapping[str, Any],
    *,
    profile_artifact: Mapping[str, Any],
    local_sf_qe_binding: Mapping[str, Any],
    preflight_payload: Mapping[str, Any],
    evaluation_logical_run_id: str,
    evaluation_attempt_run_id: str,
    output_root_relative: str,
) -> None:
    required = {
        "schema_id",
        "schema_version",
        "status",
        "halted_at",
        "binding",
        "progress",
        "terminal_error",
        "integrity",
    }
    if set(value) != required:
        raise ContractValidationError(
            "closed_schema", "$.halt", "halt marker fields differ"
        )
    if (
        value["schema_id"] != "EvaluationLivePilotHaltV1"
        or value["schema_version"] != "1.0.0"
        or value["status"] != "halted"
    ):
        raise ContractValidationError(
            "halt_schema", "$.halt", "unsupported pilot halt marker"
        )
    integrity = value.get("integrity")
    if not isinstance(integrity, Mapping) or set(integrity) != {"halt_sha256"}:
        raise ContractValidationError(
            "halt_integrity", "$.halt.integrity", "halt integrity fields differ"
        )
    recorded_hash = integrity.get("halt_sha256")
    unhashed = copy.deepcopy(dict(value))
    unhashed["integrity"]["halt_sha256"] = "0" * 64
    if recorded_hash != canonical_sha256(unhashed):
        raise ContractValidationError(
            "halt_integrity", "$.halt.integrity.halt_sha256", "halt hash differs"
        )
    binding = value.get("binding")
    if not isinstance(binding, Mapping):
        raise ContractValidationError(
            "halt_binding", "$.halt.binding", "halt binding must be an object"
        )
    expected_binding = {
        "logical_run_id": evaluation_logical_run_id,
        "attempt_run_id": evaluation_attempt_run_id,
        "profile_id": profile_artifact["profile"]["profile_id"],
        "profile_sha256": profile_artifact["binding"]["profile_sha256"],
        "preflight_sha256": preflight_payload["integrity"]["preflight_sha256"],
        "local_sf_qe_score_set_sha256": local_sf_qe_binding["score_set_sha256"],
        "output_root_relative": output_root_relative,
    }
    if binding != expected_binding:
        raise ContractValidationError(
            "halt_binding", "$.halt.binding", "halt marker binds a different run"
        )
    progress = value.get("progress")
    progress_fields = {
        "expected_physical_call_count",
        "recorded_physical_attempt_count",
        "succeeded_physical_attempt_count",
        "failed_physical_attempt_count",
        "unattempted_physical_call_count",
        "unfinished_physical_call_count",
        "known_prompt_tokens",
        "known_completion_tokens",
        "known_total_tokens",
        "execution_published",
        "publishable",
    }
    if not isinstance(progress, Mapping) or set(progress) != progress_fields:
        raise ContractValidationError(
            "halt_progress", "$.halt.progress", "halt progress fields differ"
        )
    count_fields = progress_fields - {"execution_published", "publishable"}
    if any(
        isinstance(progress[field], bool)
        or not isinstance(progress[field], int)
        or progress[field] < 0
        for field in count_fields
    ):
        raise ContractValidationError(
            "halt_progress", "$.halt.progress", "halt progress counts are invalid"
        )
    if (
        progress["execution_published"] is not False
        or progress["publishable"] is not False
    ):
        raise ContractValidationError(
            "halt_authority", "$.halt.progress", "halted pilot cannot be publishable"
        )
    if progress["recorded_physical_attempt_count"] != (
        progress["succeeded_physical_attempt_count"]
        + progress["failed_physical_attempt_count"]
    ):
        raise ContractValidationError(
            "halt_progress", "$.halt.progress", "halt attempt counts do not reconcile"
        )
    terminal_error = value.get("terminal_error")
    terminal_fields = {
        "source",
        "occurred_at",
        "category",
        "code",
        "retry_class",
        "retry_disposition",
        "safe_message",
        "source_health_effect",
        "attempt_usage_id",
        "logical_request_id",
    }
    if not isinstance(terminal_error, Mapping) or set(terminal_error) != terminal_fields:
        raise ContractValidationError(
            "halt_error", "$.halt.terminal_error", "terminal error fields differ"
        )


def _resolve_output_root(base: Path, relative_value: str) -> Path:
    if not isinstance(relative_value, str) or not relative_value:
        raise ContractValidationError(
            "output_root", "$.output_root_relative", "expected a relative POSIX path"
        )
    if "\\" in relative_value:
        raise ContractValidationError(
            "output_root", "$.output_root_relative", "expected a relative POSIX path"
        )
    relative = PurePosixPath(relative_value)
    if relative.is_absolute() or relative_value in {".", ".."} or ".." in relative.parts:
        raise ContractValidationError(
            "output_root", "$.output_root_relative", "output root escapes its base"
        )
    resolved_base = Path(base).resolve()
    root = resolved_base.joinpath(*relative.parts).resolve()
    try:
        root.relative_to(resolved_base)
    except ValueError as exc:
        raise ContractValidationError(
            "output_root", "$.output_root_relative", "output root escapes its base"
        ) from exc
    return root


def _persist_create_or_equal(path: Path, payload: Mapping[str, Any]) -> None:
    rendered = (canonical_json(payload) + "\n").encode("utf-8")
    _persist_bytes_create_or_equal(path, rendered)


def _persist_bytes_create_or_equal(path: Path, rendered: bytes) -> None:
    if path.exists():
        if not path.is_file() or path.read_bytes() != rendered:
            raise ContractValidationError(
                "artifact_collision",
                str(path),
                "existing pilot artifact differs from the sealed request",
            )
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(rendered)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.rename(temporary, path)
        except FileExistsError:
            if path.read_bytes() != rendered:
                raise ContractValidationError(
                    "artifact_collision",
                    str(path),
                    "concurrent pilot artifact differs from the sealed request",
                )
    finally:
        temporary.unlink(missing_ok=True)


def _load_json_object(path: Path) -> dict[str, Any]:
    try:
        with Path(path).open("r", encoding="utf-8") as handle:
            value = json.load(
                handle,
                object_pairs_hook=_reject_duplicate_keys,
                parse_constant=_reject_non_finite,
            )
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ContractValidationError(
            "json_artifact", str(path), "pilot artifact is not strict UTF-8 JSON"
        ) from exc
    if not isinstance(value, dict):
        raise ContractValidationError(
            "json_artifact", str(path), "pilot artifact root must be an object"
        )
    return value


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ContractValidationError(
                "duplicate_key", "$", f"duplicate JSON key: {key}"
            )
        result[key] = value
    return result


def _reject_non_finite(value: str) -> None:
    try:
        number = float(value)
    except ValueError as exc:
        raise ContractValidationError(
            "nonfinite", "$", "invalid JSON numeric constant"
        ) from exc
    if not math.isfinite(number):
        raise ContractValidationError(
            "nonfinite", "$", "non-finite JSON values are forbidden"
        )
    raise ContractValidationError("json_number", "$", "invalid JSON number")
