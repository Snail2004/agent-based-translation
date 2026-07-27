"""One-chapter, one-call Shared Backend canary for Literary B3 V2."""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Any, Mapping

from pipeline.literary.b3_temporal_context_v1 import (
    B3TemporalProfileV1,
    load_b2_temporal_input_v1,
)
from pipeline.literary.b3_temporal_context_v2 import (
    build_b3_temporal_live_bundle_v2,
)
from pipeline.literary.b3_temporal_contract_v2 import (
    normalize_b3_temporal_response_v2,
    validate_b3_temporal_request_v2,
)
from pipeline.literary.chapter_cycle_shared_runtime_v1 import (
    BACKEND_MODE_SHARED_V1,
    LiterarySharedRunnerBindingsV1,
)
from pipeline.literary.checkpoint import (
    canonical_hash,
    canonical_json,
    file_sha256,
    resolve_existing_canonical_path,
)
from pipeline.literary.model_ref_v1 import MODEL_REF_MODE_CLASSIFIED_V1
from pipeline.literary.openai_b3_json_object_capability_probe_v1 import (
    ROLE_ID,
    b3_validator_ref_v2,
)


B3_LIVE_SEAL_SCHEMA_VERSION_V1 = "literary_b3_temporal_live_seal_v1"
B3_LIVE_REPORT_SCHEMA_VERSION_V1 = "literary_b3_temporal_live_report_v1"


class B3TemporalLiveError(RuntimeError):
    pass


def prepare_b3_temporal_canary_v1(
    *,
    b2_run_root: Path,
    output_root: Path,
    profile: B3TemporalProfileV1,
    shared_runtime: LiterarySharedRunnerBindingsV1,
    current_git_head: str,
) -> dict[str, Any]:
    output = Path(output_root).resolve()
    source_root = Path(b2_run_root).resolve()
    if output.exists():
        raise B3TemporalLiveError("B3 live output root must not exist")
    if not source_root.is_dir():
        raise B3TemporalLiveError("B3 live B2 source root is absent")
    temporal_input = load_b2_temporal_input_v1(source_root)
    source_tree_hash = _tree_hash(source_root)
    bundle = build_b3_temporal_live_bundle_v2(
        temporal_input=temporal_input,
        profile=profile,
    )
    if bundle["plan"]["request_count"] != 1:
        raise B3TemporalLiveError(
            "B3 Ch1 canary requires exactly one planned request"
        )
    request = bundle["requests"][0]
    validate_b3_temporal_request_v2(request)
    runtime_identity = shared_runtime.identity_payload()
    if set(shared_runtime.runtime_profile.role_bindings) != {ROLE_ID}:
        raise B3TemporalLiveError("B3 runtime profile must exact-cover only B3")
    source = shared_runtime.api_source_for(ROLE_ID)
    preset = shared_runtime.role_preset_for(ROLE_ID)
    if source.get("physical_quota_bucket_id") != "openai-row2":
        raise B3TemporalLiveError("B3 canary is not sealed to openai-row2")
    if preset.requested_model_id != "gpt-5.4":
        raise B3TemporalLiveError("B3 canary model differs from gpt-5.4")
    if preset.limits["max_calls"] != 1:
        raise B3TemporalLiveError("B3 canary role must permit exactly one call")

    seal_body = {
        "schema_version": B3_LIVE_SEAL_SCHEMA_VERSION_V1,
        "backend_mode": BACKEND_MODE_SHARED_V1,
        "phase": "bounded_chapter_canary",
        "current_git_head": current_git_head,
        "chapter_id": temporal_input["chapter_id"],
        "source_b2_run_root": str(source_root),
        "source_b2_tree_hash": source_tree_hash,
        "source_b2_artifact_hash": temporal_input["source_b2_artifact_hash"],
        "source_prefix_bundle_hash": temporal_input["source_prefix_bundle_hash"],
        "context_profile_id": profile.profile_id,
        "context_profile_hash": profile.profile_hash,
        "context_profile_sha256": profile.profile_sha256,
        "live_plan_hash": bundle["plan"]["plan_hash"],
        "request_fingerprint": request["request_fingerprint"],
        "shared_runtime_identity": runtime_identity,
        "limits": {
            "max_calls": 1,
            "max_prompt_tokens": profile.prompt_tokens_per_request,
            "max_completion_tokens": profile.output_tokens_per_request,
            "max_total_tokens": (
                profile.prompt_tokens_per_request
                + profile.output_tokens_per_request
            ),
            "transport_retries": 0,
            "semantic_retries": 0,
        },
        "safety": {
            "provider_fallback_allowed": False,
            "application_response_cache_enabled": False,
            "gold_or_oracle_allowed": False,
            "production_publish_enabled": False,
            "mandatory_stop_after_chapter": temporal_input["chapter_id"],
        },
        "issued_at_utc": _now(),
    }
    seal = {**seal_body, "seal_hash": canonical_hash(seal_body)}
    output.mkdir(parents=True, exist_ok=False)
    _write_new_json(output / "run_seal.json", seal)
    _write_new_json(output / "live_plan.json", bundle["plan"])
    _write_new_json(
        output / "components.json",
        {
            "schema_version": "literary_b3_temporal_component_catalog_v1",
            "chapter_id": temporal_input["chapter_id"],
            "components": bundle["components"],
        },
    )
    _write_new_json(output / "request.json", request)
    if _tree_hash(source_root) != source_tree_hash:
        raise B3TemporalLiveError("B2 source changed during B3 preparation")
    return seal


def execute_b3_temporal_canary_v1(
    *,
    output_root: Path,
    shared_runtime: LiterarySharedRunnerBindingsV1,
    current_git_head: str,
) -> dict[str, Any]:
    output = Path(output_root).resolve()
    seal = _verified_hashed_object(output / "run_seal.json", "seal_hash")
    if seal.get("current_git_head") != current_git_head:
        raise B3TemporalLiveError("B3 live Git HEAD differs from sealed HEAD")
    if seal.get("shared_runtime_identity") != shared_runtime.identity_payload():
        raise B3TemporalLiveError("B3 shared runtime differs from sealed identity")
    source_root = resolve_existing_canonical_path(seal["source_b2_run_root"])
    if _tree_hash(source_root) != seal["source_b2_tree_hash"]:
        raise B3TemporalLiveError("B3 source B2 tree changed after seal")
    request = _read_object(output / "request.json", "B3 live request")
    validate_b3_temporal_request_v2(request)
    if request.get("request_fingerprint") != seal.get("request_fingerprint"):
        raise B3TemporalLiveError("B3 live request differs from seal")
    if (output / "b3_temporal_artifact.json").exists():
        raise B3TemporalLiveError("B3 canary already completed")

    _write_new_json(
        output / "stage_started.json",
        {
            "schema_version": "literary_b3_temporal_stage_started_v1",
            "seal_hash": seal["seal_hash"],
            "request_fingerprint": request["request_fingerprint"],
            "physical_quota_bucket_id": shared_runtime.api_source_for(ROLE_ID)[
                "physical_quota_bucket_id"
            ],
            "started_at_utc": _now(),
        },
    )

    def validate(response: Mapping[str, Any]) -> Mapping[str, Any]:
        return normalize_b3_temporal_response_v2(
            request=request,
            response=response,
        )

    try:
        result = shared_runtime.execute_accepted_request(
            role_id=ROLE_ID,
            stage_id=f"b3_temporal_{seal['chapter_id']}",
            logical_request_id=(
                f"b3_temporal_{seal['chapter_id']}_{request['request_fingerprint'][:24]}"
            ),
            request=request,
            schema_name="literary_b3_temporal_response_v2",
            semantic_validator=validate,
            validator_ref=b3_validator_ref_v2(),
            application_contract_id="literary.b3.temporal_state.apply_v1",
            application_contract_revision="v1",
            output_dir=output,
            model_reference_mode=MODEL_REF_MODE_CLASSIFIED_V1,
            additional_input_bindings=(
                {"name": "b3_live_run_seal", "sha256": seal["seal_hash"]},
                {
                    "name": "b3_source_b2_artifact",
                    "sha256": seal["source_b2_artifact_hash"],
                },
                {
                    "name": "b3_source_prefix_bundle",
                    "sha256": seal["source_prefix_bundle_hash"],
                },
            ),
        )
        usage = _validated_usage(result.usage, seal["limits"])
        artifact = dict(result.semantic_payload)
        _write_new_json(output / "b3_temporal_artifact.json", artifact)
        if _tree_hash(source_root) != seal["source_b2_tree_hash"]:
            raise B3TemporalLiveError("B2 source changed during B3 provider call")
        report_body = {
            "schema_version": B3_LIVE_REPORT_SCHEMA_VERSION_V1,
            "status": "complete_mandatory_stop",
            "seal_hash": seal["seal_hash"],
            "chapter_id": seal["chapter_id"],
            "request_fingerprint": request["request_fingerprint"],
            "provider_called": result.provider_called,
            "provider_artifact_sha256": result.artifact_sha256,
            "artifact_hash": artifact["artifact_hash"],
            "usage": usage,
            "counts": {
                "component_results": len(artifact["component_results"]),
                "new_states": len(artifact["new_state_rows"]),
                "transitions": len(artifact["transition_rows"]),
                "reinforcements": len(artifact["reinforcement_rows"]),
                "historical_observations": len(
                    artifact["historical_observations"]
                ),
                "non_effective_observations": len(
                    artifact["non_effective_observations"]
                ),
                "pending_cases": len(artifact["pending_cases"]),
                "effective_states": len(artifact["effective_state_projection"]),
            },
            "source_artifact_mutated": False,
            "gold_or_oracle_loaded": False,
            "production_publish_performed": False,
            "api_calls_performed": 1,
            "mandatory_stop_observed": True,
            "completed_at_utc": _now(),
        }
        report = {**report_body, "report_hash": canonical_hash(report_body)}
        _write_new_json(output / "live_report.json", report)
        return report
    except Exception as exc:
        failure_path = output / "stage_failure.json"
        if not failure_path.exists():
            _write_new_json(
                failure_path,
                _failure_payload(
                    exc=exc,
                    seal_hash=seal["seal_hash"],
                    request_fingerprint=request["request_fingerprint"],
                ),
            )
        raise


def _validated_usage(
    usage: Mapping[str, Any] | None, limits: Mapping[str, Any]
) -> dict[str, Any]:
    if not isinstance(usage, Mapping):
        raise B3TemporalLiveError("B3 provider usage is unknown")
    prompt = usage.get("prompt_tokens")
    completion = usage.get("completion_tokens")
    total = usage.get("total_tokens")
    if not all(
        isinstance(value, int) and not isinstance(value, bool) and value >= 0
        for value in (prompt, completion, total)
    ):
        raise B3TemporalLiveError("B3 provider usage is incomplete")
    if prompt + completion != total:
        raise B3TemporalLiveError("B3 provider usage is inconsistent")
    comparisons = (
        (prompt, limits["max_prompt_tokens"]),
        (completion, limits["max_completion_tokens"]),
        (total, limits["max_total_tokens"]),
    )
    if any(observed > maximum for observed, maximum in comparisons):
        raise B3TemporalLiveError("B3 provider usage exceeded a sealed cap")
    return deepcopy(dict(usage))


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _failure_payload(
    *, exc: Exception, seal_hash: str, request_fingerprint: str
) -> dict[str, Any]:
    message = str(exc)
    if "sk-" in message or "Bearer " in message:
        message = "credential material was redacted from the B3 failure"
    return {
        "schema_version": "literary_b3_temporal_stage_failure_v1",
        "status": "halted_fail_closed",
        "seal_hash": seal_hash,
        "request_fingerprint": request_fingerprint,
        "error_type": type(exc).__name__,
        "message": message[:1200],
        "production_publish_performed": False,
        "failed_at_utc": _now(),
    }


def _read_object(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise B3TemporalLiveError(f"cannot read {label}: {path}") from exc
    if not isinstance(value, dict):
        raise B3TemporalLiveError(f"{label} must be an object")
    return value


def _verified_hashed_object(path: Path, field: str) -> dict[str, Any]:
    row = _read_object(path, path.name)
    expected = row.get(field)
    unsigned = dict(row)
    unsigned.pop(field, None)
    if expected != canonical_hash(unsigned):
        raise B3TemporalLiveError(f"{path.name} hash mismatch")
    return row


def _write_new_json(path: Path, payload: Mapping[str, Any]) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        raise B3TemporalLiveError(f"refusing to overwrite artifact: {target}")
    target.write_text(canonical_json(payload) + "\n", encoding="utf-8")


def _tree_hash(root: Path) -> str:
    rows = [
        {"path": path.relative_to(root).as_posix(), "sha256": file_sha256(path)}
        for path in sorted(Path(root).rglob("*"))
        if path.is_file()
    ]
    return canonical_hash(rows)


__all__ = [
    "B3_LIVE_REPORT_SCHEMA_VERSION_V1",
    "B3_LIVE_SEAL_SCHEMA_VERSION_V1",
    "B3TemporalLiveError",
    "execute_b3_temporal_canary_v1",
    "prepare_b3_temporal_canary_v1",
]
