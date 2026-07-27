"""Fail-closed Shared Runtime consumer for B4 Address Anchor and Translator."""

from __future__ import annotations

from copy import deepcopy
import json
import math
from pathlib import Path
from typing import Any, Callable, Mapping

from pipeline.llm_backend import (
    ApplicationResponseCache,
    ContentAddressedArtifactStore,
    MappingCredentialProvider,
    PhysicalQuotaScheduler,
    SharedLlmAttemptLedger,
    SharedLlmBackend,
    UrllibTransportSender,
    canonical_json,
    canonical_sha256,
    validate_capability_evidence,
)
from pipeline.literary.b4_address_anchor_v1 import (
    ROLE_ID as ADDRESS_ROLE_ID,
    build_address_anchor_artifact_v1,
    make_address_anchor_semantic_validator_v1,
    render_address_anchor_request_v1,
)
from pipeline.literary.b4_translator_v1 import (
    RESPONSE_SCHEMA_VERSION as TRANSLATOR_RESPONSE_SCHEMA_VERSION,
    ROLE_ID as TRANSLATOR_ROLE_ID,
    build_translation_window_artifact_v1,
    render_translation_window_request_v1,
    validate_translation_window_response_v1,
)
from pipeline.literary.chapter_cycle_shared_runtime_v1 import (
    LiterarySharedRunnerBindingsV1,
    capability_binding_key,
)
from pipeline.literary.checkpoint import canonical_hash
from pipeline.literary.modelapi_b4_capability_probe_v1 import (
    RUNTIME_PROFILE_PATH,
    RUNTIME_ROLE_IDS,
    validator_ref_v1,
)
from pipeline.literary.shared_llm_adapter_v1 import (
    LiterarySharedLlmAttemptAdapter,
    render_literary_request_body,
    resolve_transport_response_schema,
)
from pipeline.literary.shared_runtime_profile_v2 import (
    load_literary_shared_runtime_profile_v2,
)


class B4LiveModelApiError(RuntimeError):
    pass


_MODELAPI_PROMPT_BYTES_PER_TOKEN_FLOOR_V1 = 2.38
_MODELAPI_PROMPT_TOKENIZER_V3 = "o200k_base"
_MODELAPI_PROMPT_INITIAL_SAFETY_MULTIPLIER_V1 = 1.25
_MODELAPI_PROMPT_OBSERVED_RATIO_MARGIN_V1 = 0.05
_MODELAPI_PROMPT_CALIBRATION_SCHEMA_V1 = (
    "literary_b4_transport_prompt_calibration_v1"
)


def run_address_anchor_live_v1(
    *,
    anchor_input: Mapping[str, Any],
    style_profile: str,
    style_profile_version: str,
    measured_arm: bool,
    capability_evidence: Mapping[str, Any],
    output_root: Path,
    shared_root: Path,
    scheduler_root: Path,
    secret: str,
    credential_commitment_sha256: str,
    run_id: str,
    attempt_run_id: str,
    current_git_head: str,
    sender: Any | None = None,
) -> dict[str, Any]:
    output = _fresh_output(output_root)
    rendered = render_address_anchor_request_v1(
        anchor_input=anchor_input,
        style_profile=style_profile,
        style_profile_version=style_profile_version,
        measured_arm=measured_arm,
    )
    request = {
        "messages": [dict(row) for row in rendered.messages],
        "response_schema": deepcopy(rendered.response_schema),
        "request_fingerprint": rendered.request_fingerprint,
    }
    _write(output / "request.json", request)
    _write(output / "packet.json", rendered.packet)
    _write(output / "anchor_input.json", rendered.anchor_input)
    (output / "style_profile.txt").write_text(style_profile, encoding="utf-8")

    bindings, source = _bindings(
        role_id=ADDRESS_ROLE_ID,
        response_schema=rendered.response_schema,
        capability_evidence=capability_evidence,
        shared_root=shared_root,
        scheduler_root=scheduler_root,
        secret=secret,
        credential_commitment_sha256=credential_commitment_sha256,
        run_id=run_id,
        attempt_run_id=attempt_run_id,
        sender=sender,
    )
    preflight = _transport_preflight_v1(
        bindings=bindings,
        role_id=ADDRESS_ROLE_ID,
        request=request,
        schema_name="literary_b4_address_anchor_response_v2",
        calibration_path=_calibration_path_v1(shared_root),
    )
    _write(output / "transport_preflight.json", preflight)
    _enforce_transport_preflight_v1(preflight)
    stage_id = "literary_b4_address_anchor"
    logical_request_id = (
        "literary_b4_anchor_"
        + canonical_hash(
            {
                "run_id": run_id,
                "chapter_id": rendered.anchor_input["chapter_id"],
                "request_fingerprint": rendered.request_fingerprint,
            }
        )[:24]
    )
    semantic_validator = _calibrating_semantic_validator_v1(
        bindings=bindings,
        semantic_validator=make_address_anchor_semantic_validator_v1(
            rendered=rendered
        ),
        preflight=preflight,
        calibration_path=_calibration_path_v1(shared_root),
        output=output,
        role_id=ADDRESS_ROLE_ID,
        stage_id=stage_id,
        logical_request_id=logical_request_id,
        attempt_run_id=attempt_run_id,
    )
    try:
        result = bindings.execute_accepted_request(
            role_id=ADDRESS_ROLE_ID,
            stage_id=stage_id,
            logical_request_id=logical_request_id,
            request=request,
            schema_name="literary_b4_address_anchor_response_v2",
            semantic_validator=semantic_validator,
            validator_ref=validator_ref_v1(ADDRESS_ROLE_ID),
            application_contract_id="literary.b4.address_anchor.application",
            application_contract_revision="v1",
            output_dir=output,
            additional_input_bindings=(
                {
                    "name": "b4_anchor_input",
                    "sha256": rendered.anchor_input["artifact_hash"],
                },
                {
                    "name": "b4_style_profile",
                    "sha256": canonical_sha256(style_profile),
                },
            ),
        )
    except Exception:
        _persist_receipt_calibration_v1(
            bindings=bindings,
            preflight=preflight,
            calibration_path=_calibration_path_v1(shared_root),
            output=output,
            role_id=ADDRESS_ROLE_ID,
            stage_id=stage_id,
            logical_request_id=logical_request_id,
            attempt_run_id=attempt_run_id,
            required=False,
        )
        raise
    if result.status != "semantic_accepted" or result.provider_called is not True:
        raise B4LiveModelApiError("Address Anchor did not complete one live call")
    receipt = _read(output / "shared_attempt_receipt.json")
    receipt_check = _persist_receipt_calibration_v1(
        bindings=bindings,
        preflight=preflight,
        calibration_path=_calibration_path_v1(shared_root),
        output=output,
        role_id=ADDRESS_ROLE_ID,
        stage_id=stage_id,
        logical_request_id=logical_request_id,
        attempt_run_id=attempt_run_id,
        required=True,
    )
    artifact = build_address_anchor_artifact_v1(
        rendered=rendered,
        validated_response=result.semantic_payload,
        provider_receipt=receipt,
        provider_called=True,
    )
    _write(output / "provider_response.json", result.response_payload)
    _write(output / "model_response.json", _model_json(result.response_text))
    _write(output / "validated_response.json", result.semantic_payload)
    _write(output / "address_anchor.json", artifact)
    _write(output / "run_seal.json", result.seal)
    report_body = {
        "schema_version": "literary_b4_address_anchor_live_report_v1",
        "status": "complete",
        "chapter_id": artifact["chapter_id"],
        "git_head": current_git_head,
        "source_id": source["source_id"],
        "requested_model_id": "gpt-5.4",
        "style_profile_version": style_profile_version,
        "measured_arm": measured_arm,
        "reference_based_scoring_allowed": False,
        "pair_count": len(artifact["pair_decisions"]),
        "review_issues": deepcopy(artifact["review_issues"]),
        "normalization_observation_count": len(
            artifact["normalization_observations"]
        ),
        "normalization_observations": deepcopy(
            artifact["normalization_observations"]
        ),
        "usage": dict(result.usage) if result.usage is not None else None,
        "transport_preflight": deepcopy(preflight),
        "transport_preflight_receipt_check": deepcopy(receipt_check),
        "provider_called": True,
        "provider_retries": 0,
        "fallback_performed": False,
        "semantic_record_mutation_performed": False,
        "artifact_hash": artifact["artifact_hash"],
    }
    report = {**report_body, "report_hash": canonical_hash(report_body)}
    _write(output / "live_report.json", report)
    return report


def run_translation_window_live_v1(
    *,
    translator_pack_bytes: bytes,
    address_anchor_bytes: bytes,
    window_slice_bytes: bytes,
    chapter: Mapping[str, Any],
    accepted_tail_translations: Mapping[str, str],
    style_profile: str,
    style_profile_version: str,
    measured_arm: bool,
    capability_evidence: Mapping[str, Any],
    output_root: Path,
    shared_root: Path,
    scheduler_root: Path,
    secret: str,
    credential_commitment_sha256: str,
    run_id: str,
    attempt_run_id: str,
    current_git_head: str,
    sender: Any | None = None,
) -> dict[str, Any]:
    output = _fresh_output(output_root)
    rendered = render_translation_window_request_v1(
        style_profile=style_profile,
        style_profile_version=style_profile_version,
        measured_arm=measured_arm,
        translator_pack_bytes=translator_pack_bytes,
        address_anchor_bytes=address_anchor_bytes,
        window_slice_bytes=window_slice_bytes,
        chapter=chapter,
        accepted_tail_translations=accepted_tail_translations,
    )
    request = {
        "messages": [dict(row) for row in rendered.messages],
        "response_schema": deepcopy(rendered.response_schema),
        "request_fingerprint": rendered.request_fingerprint,
    }
    _write(output / "request.json", request)
    _write(output / "translation_request_pack.json", rendered.model_input_pack)
    (output / "translator_pack.json").write_bytes(translator_pack_bytes)
    (output / "address_anchor.json").write_bytes(address_anchor_bytes)
    (output / "window_slice.json").write_bytes(window_slice_bytes)
    _write(output / "chapter.json", chapter)
    _write(output / "accepted_tail.json", accepted_tail_translations)
    (output / "style_profile.txt").write_text(style_profile, encoding="utf-8")

    bindings, source = _bindings(
        role_id=TRANSLATOR_ROLE_ID,
        response_schema=rendered.response_schema,
        capability_evidence=capability_evidence,
        shared_root=shared_root,
        scheduler_root=scheduler_root,
        secret=secret,
        credential_commitment_sha256=credential_commitment_sha256,
        run_id=run_id,
        attempt_run_id=attempt_run_id,
        sender=sender,
    )
    preflight = _transport_preflight_v1(
        bindings=bindings,
        role_id=TRANSLATOR_ROLE_ID,
        request=request,
        schema_name=TRANSLATOR_RESPONSE_SCHEMA_VERSION,
        calibration_path=_calibration_path_v1(shared_root),
    )
    _write(output / "transport_preflight.json", preflight)
    _enforce_transport_preflight_v1(preflight)

    def validate(payload: Mapping[str, Any]) -> Mapping[str, Any]:
        return validate_translation_window_response_v1(
            rendered=rendered,
            response=payload,
        )

    stage_id = "literary_b4_translator"
    logical_request_id = (
        "literary_b4_translate_"
        + canonical_hash(
            {
                "run_id": run_id,
                "window_id": rendered.window_slice["window_id"],
                "request_fingerprint": rendered.request_fingerprint,
            }
        )[:24]
    )
    semantic_validator = _calibrating_semantic_validator_v1(
        bindings=bindings,
        semantic_validator=validate,
        preflight=preflight,
        calibration_path=_calibration_path_v1(shared_root),
        output=output,
        role_id=TRANSLATOR_ROLE_ID,
        stage_id=stage_id,
        logical_request_id=logical_request_id,
        attempt_run_id=attempt_run_id,
    )
    try:
        result = bindings.execute_accepted_request(
            role_id=TRANSLATOR_ROLE_ID,
            stage_id=stage_id,
            logical_request_id=logical_request_id,
            request=request,
            schema_name=TRANSLATOR_RESPONSE_SCHEMA_VERSION,
            semantic_validator=semantic_validator,
            validator_ref=validator_ref_v1(TRANSLATOR_ROLE_ID),
            application_contract_id="literary.b4.translator.application",
            application_contract_revision="v1",
            output_dir=output,
            additional_input_bindings=(
                {
                    "name": "b4_translator_pack",
                    "sha256": rendered.translator_pack["artifact_hash"],
                },
                {
                    "name": "b4_address_anchor",
                    "sha256": rendered.address_anchor["artifact_hash"],
                },
                {
                    "name": "b4_window_slice",
                    "sha256": rendered.window_slice["artifact_hash"],
                },
                {
                    "name": "b4_stable_prefix",
                    "sha256": rendered.stable_prefix_sha256,
                },
                {
                    "name": "b4_translation_request_pack",
                    "sha256": rendered.model_input_pack["pack_hash"],
                },
            ),
        )
    except Exception:
        _persist_receipt_calibration_v1(
            bindings=bindings,
            preflight=preflight,
            calibration_path=_calibration_path_v1(shared_root),
            output=output,
            role_id=TRANSLATOR_ROLE_ID,
            stage_id=stage_id,
            logical_request_id=logical_request_id,
            attempt_run_id=attempt_run_id,
            required=False,
        )
        raise
    if result.status != "semantic_accepted" or result.provider_called is not True:
        raise B4LiveModelApiError("Translator did not complete one live call")
    receipt = _read(output / "shared_attempt_receipt.json")
    receipt_check = _persist_receipt_calibration_v1(
        bindings=bindings,
        preflight=preflight,
        calibration_path=_calibration_path_v1(shared_root),
        output=output,
        role_id=TRANSLATOR_ROLE_ID,
        stage_id=stage_id,
        logical_request_id=logical_request_id,
        attempt_run_id=attempt_run_id,
        required=True,
    )
    artifact = build_translation_window_artifact_v1(
        validated_response=result.semantic_payload,
        provider_receipt=receipt,
        provider_called=True,
    )
    _write(output / "provider_response.json", result.response_payload)
    _write(output / "model_response.json", _model_json(result.response_text))
    _write(output / "validated_response.json", result.semantic_payload)
    _write(output / "translation_window.json", artifact)
    _write(output / "run_seal.json", result.seal)
    report_body = {
        "schema_version": "literary_b4_translation_window_live_report_v1",
        "status": "complete",
        "chapter_id": artifact["chapter_id"],
        "window_id": artifact["window_id"],
        "window_order": artifact["window_order"],
        "git_head": current_git_head,
        "source_id": source["source_id"],
        "requested_model_id": "gpt-5.4",
        "style_profile_version": style_profile_version,
        "measured_arm": measured_arm,
        "reference_based_scoring_allowed": False,
        "stable_prefix_sha256": artifact["stable_prefix_sha256"],
        "translated_block_count": len(artifact["blocks"]),
        "translator_output_contract": artifact["translator_output_contract"],
        "address_metadata_collected": artifact["address_metadata_collected"],
        "usage": dict(result.usage) if result.usage is not None else None,
        "transport_preflight": deepcopy(preflight),
        "transport_preflight_receipt_check": deepcopy(receipt_check),
        "provider_called": True,
        "provider_retries": 0,
        "fallback_performed": False,
        "semantic_record_mutation_performed": False,
        "artifact_hash": artifact["artifact_hash"],
    }
    report = {**report_body, "report_hash": canonical_hash(report_body)}
    _write(output / "live_report.json", report)
    return report


def estimate_b4_transport_request_v1(
    *,
    role_id: str,
    request: Mapping[str, Any],
    schema_name: str,
    capability_evidence: Mapping[str, Any],
    calibration_path: Path,
) -> dict[str, Any]:
    """Render and budget the exact production envelope without a provider call."""

    evidence = validate_capability_evidence(capability_evidence)
    if evidence.get("verdict") != "qualified":
        raise B4LiveModelApiError(f"B4 capability is not qualified: {role_id}")
    runtime = load_literary_shared_runtime_profile_v2(
        RUNTIME_PROFILE_PATH,
        expected_role_ids=RUNTIME_ROLE_IDS,
    )
    try:
        source_alias = runtime.role_bindings[role_id].source_alias
    except KeyError as exc:
        raise B4LiveModelApiError(
            f"B4 runtime profile lacks role: {role_id}"
        ) from exc
    source_binding = dict(runtime.source_binding_for(role_id))
    source = _source_record(source_binding, "0" * 64)
    bindings = LiterarySharedRunnerBindingsV1(
        adapter=None,  # type: ignore[arg-type]
        api_source=None,
        capabilities={
            capability_binding_key(role_id, request["response_schema"]): evidence
        },
        run_id="b4_offline_transport_estimate",
        attempt_run_id="b4_offline_transport_estimate_a1",
        structured_output=None,
        runtime_profile=runtime,
        api_sources_by_alias={source_alias: source},
    )
    return _transport_preflight_v1(
        bindings=bindings,
        role_id=role_id,
        request=request,
        schema_name=schema_name,
        calibration_path=calibration_path,
    )


def _calibrating_semantic_validator_v1(
    *,
    bindings: LiterarySharedRunnerBindingsV1,
    semantic_validator: Callable[
        [Mapping[str, Any]], Mapping[str, Any]
    ],
    preflight: Mapping[str, Any],
    calibration_path: Path,
    output: Path,
    role_id: str,
    stage_id: str,
    logical_request_id: str,
    attempt_run_id: str,
) -> Callable[[Mapping[str, Any]], Mapping[str, Any]]:
    def validate(payload: Mapping[str, Any]) -> Mapping[str, Any]:
        _persist_receipt_calibration_v1(
            bindings=bindings,
            preflight=preflight,
            calibration_path=calibration_path,
            output=output,
            role_id=role_id,
            stage_id=stage_id,
            logical_request_id=logical_request_id,
            attempt_run_id=attempt_run_id,
            required=True,
        )
        return semantic_validator(payload)

    return validate


def _persist_receipt_calibration_v1(
    *,
    bindings: LiterarySharedRunnerBindingsV1,
    preflight: Mapping[str, Any],
    calibration_path: Path,
    output: Path,
    role_id: str,
    stage_id: str,
    logical_request_id: str,
    attempt_run_id: str,
    required: bool,
) -> dict[str, Any] | None:
    receipt_check_path = output / "transport_preflight_receipt_check.json"
    if receipt_check_path.exists():
        receipt_check = _read(receipt_check_path)
        _enforce_receipt_check_v1(receipt_check)
        return receipt_check

    ledger = bindings.adapter.backend.ledger
    seals = [
        row
        for row in ledger.list_records("seal")
        if row.get("role_id") == role_id
        and row.get("stage_id") == stage_id
        and row.get("attempt_run_id") == attempt_run_id
    ]
    if not seals:
        if required:
            raise B4LiveModelApiError(
                "B4 provider receipt is absent before semantic validation"
            )
        return None
    if len(seals) != 1:
        raise B4LiveModelApiError(
            "B4 provider receipt seal is ambiguous"
        )
    seal_sha256 = seals[0]["seal_sha256"]
    usage_rows = [
        row
        for row in ledger.list_records("usage")
        if row.get("seal_sha256") == seal_sha256
        and row.get("logical_request_id") == logical_request_id
        and row.get("semantic_attempt_index") == 1
        and row.get("transport_retry_ordinal") == 0
    ]
    if not usage_rows:
        if required:
            raise B4LiveModelApiError(
                "B4 provider usage is absent before semantic validation"
            )
        return None
    if len(usage_rows) != 1:
        raise B4LiveModelApiError(
            "B4 provider usage is ambiguous before semantic validation"
        )
    receipt_check = _receipt_check_v1(
        preflight,
        usage_rows[0],
        calibration_path=calibration_path,
    )
    _write(receipt_check_path, receipt_check)
    _enforce_receipt_check_v1(receipt_check)
    return receipt_check


def _transport_preflight_v1(
    *,
    bindings: LiterarySharedRunnerBindingsV1,
    role_id: str,
    request: Mapping[str, Any],
    schema_name: str,
    calibration_path: Path,
) -> dict[str, Any]:
    messages = request.get("messages")
    response_schema = request.get("response_schema")
    if not isinstance(messages, list) or not isinstance(response_schema, Mapping):
        raise B4LiveModelApiError("B4 preflight request is malformed")
    preset = bindings.role_preset_for(role_id)
    source = bindings.api_source_for(role_id)
    envelope = bindings.output_envelope_for(role_id)
    structured_output = bindings.structured_output_for(role_id)
    capability = bindings.capability_for(
        role_id=role_id,
        response_schema=response_schema,
    )
    transport_schema, _omissions = resolve_transport_response_schema(
        response_schema=response_schema,
        protocol=str(source["protocol"]),
        output_envelope=envelope,
    )
    request_body = render_literary_request_body(
        preset=preset,
        protocol=str(source["protocol"]),
        capability=capability,
        messages=messages,
        response_schema=transport_schema,
        instruction_schema=response_schema,
        schema_name=schema_name,
        structured_output=structured_output,
        output_envelope=envelope,
        base_url=source.get("base_url"),
    )
    wire_json = canonical_json(request_body)
    wire_bytes = len(wire_json.encode("utf-8"))
    estimate = _estimate_modelapi_prompt_tokens_v3(wire_json)
    estimated_tokens = estimate["estimated_prompt_tokens"]
    calibration = _load_prompt_calibration_v1(calibration_path)
    role_calibration = calibration["roles"].get(role_id, {})
    calibrated_bounds = _calibrated_prompt_upper_bounds_v1(
        estimated_tokens=estimated_tokens,
        role_calibration=role_calibration,
    )
    safety_multiplier = calibrated_bounds["safety_multiplier"]
    conservative_upper_bound = calibrated_bounds["conservative_upper_bound"]
    raw_cap = preset.generation.get("max_input_tokens")
    if (
        not isinstance(raw_cap, int)
        or isinstance(raw_cap, bool)
        or raw_cap <= 0
    ):
        raise B4LiveModelApiError("B4 role has no valid max_input_tokens")
    body = {
        "schema_version": "literary_b4_transport_preflight_v3",
        "role_id": role_id,
        "source_id": source["source_id"],
        "requested_model_id": preset.requested_model_id,
        "transport_request_sha256": canonical_sha256(request_body),
        "transport_request_utf8_bytes": wire_bytes,
        "estimate_method": estimate["estimate_method"],
        "tokenizer_name": estimate["tokenizer_name"],
        "tokenized_transport_tokens": estimate["tokenized_transport_tokens"],
        "fallback_bytes_per_token_floor": (
            estimate["fallback_bytes_per_token_floor"]
        ),
        "estimated_prompt_tokens": estimated_tokens,
        "safety_multiplier": round(safety_multiplier, 6),
        "initial_safety_multiplier": (
            _MODELAPI_PROMPT_INITIAL_SAFETY_MULTIPLIER_V1
        ),
        "observed_ratio_margin": (
            _MODELAPI_PROMPT_OBSERVED_RATIO_MARGIN_V1
        ),
        "max_observed_actual_to_estimate_ratio": role_calibration.get(
            "max_observed_actual_to_estimate_ratio"
        ),
        "max_observed_additive_error_tokens": calibrated_bounds[
            "max_observed_additive_error_tokens"
        ],
        "multiplicative_upper_bound": calibrated_bounds[
            "multiplicative_upper_bound"
        ],
        "additive_upper_bound": calibrated_bounds["additive_upper_bound"],
        "calibration_sample_count": len(role_calibration.get("samples") or []),
        "calibration_artifact_hash": calibration["calibration_hash"],
        "conservative_prompt_token_upper_bound": conservative_upper_bound,
        "max_input_tokens": raw_cap,
        "fits": conservative_upper_bound <= raw_cap,
    }
    return {**body, "preflight_hash": canonical_hash(body)}


def _calibrated_prompt_upper_bounds_v1(
    *,
    estimated_tokens: int,
    role_calibration: Mapping[str, Any],
) -> dict[str, int | float | None]:
    safety_multiplier = float(
        role_calibration.get(
            "safety_multiplier",
            _MODELAPI_PROMPT_INITIAL_SAFETY_MULTIPLIER_V1,
        )
    )
    multiplicative_upper_bound = math.ceil(
        estimated_tokens * safety_multiplier
    )
    max_additive_error = role_calibration.get(
        "max_observed_additive_error_tokens"
    )
    if (
        not isinstance(max_additive_error, int)
        or isinstance(max_additive_error, bool)
        or max_additive_error < 0
    ):
        observed_errors = [
            int(row["provider_prompt_tokens"])
            - int(row["estimated_prompt_tokens"])
            for row in role_calibration.get("samples") or []
            if (
                isinstance(row, Mapping)
                and isinstance(row.get("provider_prompt_tokens"), int)
                and not isinstance(row.get("provider_prompt_tokens"), bool)
                and isinstance(row.get("estimated_prompt_tokens"), int)
                and not isinstance(row.get("estimated_prompt_tokens"), bool)
            )
        ]
        max_additive_error = (
            max(0, *observed_errors) if observed_errors else None
        )
    additive_upper_bound = (
        estimated_tokens
        if max_additive_error is None
        else (
            estimated_tokens
            + max_additive_error
            + math.ceil(
                estimated_tokens
                * _MODELAPI_PROMPT_OBSERVED_RATIO_MARGIN_V1
            )
        )
    )
    return {
        "safety_multiplier": safety_multiplier,
        "max_observed_additive_error_tokens": max_additive_error,
        "multiplicative_upper_bound": multiplicative_upper_bound,
        "additive_upper_bound": additive_upper_bound,
        "conservative_upper_bound": max(
            multiplicative_upper_bound,
            additive_upper_bound,
        ),
    }


def _estimate_modelapi_prompt_tokens_v3(wire_json: str) -> dict[str, Any]:
    """Tokenize the exact serialized request before receipt calibration."""

    try:
        import tiktoken

        tokenized = len(
            tiktoken.get_encoding(_MODELAPI_PROMPT_TOKENIZER_V3).encode(wire_json)
        )
    except (ImportError, KeyError):
        estimated = math.ceil(
            len(wire_json.encode("utf-8"))
            / _MODELAPI_PROMPT_BYTES_PER_TOKEN_FLOOR_V1
        )
        return {
            "estimate_method": "modelapi_v1_utf8_floor_calibrated_v3",
            "tokenizer_name": None,
            "tokenized_transport_tokens": None,
            "fallback_bytes_per_token_floor": (
                _MODELAPI_PROMPT_BYTES_PER_TOKEN_FLOOR_V1
            ),
            "estimated_prompt_tokens": estimated,
        }

    return {
        "estimate_method": "modelapi_v1_o200k_receipt_calibrated_v3",
        "tokenizer_name": _MODELAPI_PROMPT_TOKENIZER_V3,
        "tokenized_transport_tokens": tokenized,
        "fallback_bytes_per_token_floor": (
            _MODELAPI_PROMPT_BYTES_PER_TOKEN_FLOOR_V1
        ),
        "estimated_prompt_tokens": tokenized,
    }


def _enforce_transport_preflight_v1(preflight: Mapping[str, Any]) -> None:
    if preflight.get("fits") is not True:
        raise B4LiveModelApiError(
            "B4 transport preflight exceeds max_input_tokens: "
            f"estimated={preflight.get('estimated_prompt_tokens')}, "
            "conservative_upper_bound="
            f"{preflight.get('conservative_prompt_token_upper_bound')}, "
            f"cap={preflight.get('max_input_tokens')}"
        )


def _receipt_check_v1(
    preflight: Mapping[str, Any],
    usage: Mapping[str, Any] | None,
    *,
    calibration_path: Path,
) -> dict[str, Any]:
    prompt_tokens = usage.get("prompt_tokens") if usage is not None else None
    if not isinstance(prompt_tokens, int) or isinstance(prompt_tokens, bool):
        prompt_tokens = None
    estimate = int(preflight["estimated_prompt_tokens"])
    upper_bound = int(preflight["conservative_prompt_token_upper_bound"])
    ratio = (
        round(prompt_tokens / estimate, 8)
        if prompt_tokens is not None and estimate > 0
        else None
    )
    persisted = False
    calibration_hash = None
    next_multiplier = None
    next_additive_error = None
    if prompt_tokens is not None:
        calibration = _record_prompt_observation_v1(
            calibration_path=calibration_path,
            role_id=str(preflight["role_id"]),
            transport_request_sha256=str(
                preflight["transport_request_sha256"]
            ),
            estimated_prompt_tokens=estimate,
            provider_prompt_tokens=prompt_tokens,
            attempt_usage_id=(
                str(usage["attempt_usage_id"])
                if usage is not None and usage.get("attempt_usage_id")
                else None
            ),
        )
        role_calibration = calibration["roles"][str(preflight["role_id"])]
        persisted = True
        calibration_hash = calibration["calibration_hash"]
        next_multiplier = role_calibration["safety_multiplier"]
        next_additive_error = role_calibration[
            "max_observed_additive_error_tokens"
        ]
    error_percent = (
        round(abs(estimate - prompt_tokens) * 100 / prompt_tokens, 4)
        if prompt_tokens
        else None
    )
    body = {
        "schema_version": "literary_b4_transport_preflight_receipt_check_v1",
        "transport_preflight_hash": preflight["preflight_hash"],
        "estimated_prompt_tokens": estimate,
        "provider_prompt_tokens": prompt_tokens,
        "actual_to_estimate_ratio": ratio,
        "multiplied_upper_bound": upper_bound,
        "within_multiplied_bound": (
            prompt_tokens <= upper_bound
            if prompt_tokens is not None
            else None
        ),
        "calibration_observation_persisted": persisted,
        "updated_calibration_hash": calibration_hash,
        "next_safety_multiplier": next_multiplier,
        "next_max_observed_additive_error_tokens": next_additive_error,
        "absolute_error_tokens": (
            abs(estimate - prompt_tokens) if prompt_tokens is not None else None
        ),
        "absolute_error_percent": error_percent,
        "within_two_percent": (
            error_percent <= 2.0 if error_percent is not None else None
        ),
    }
    return {**body, "receipt_check_hash": canonical_hash(body)}


def _enforce_receipt_check_v1(receipt_check: Mapping[str, Any]) -> None:
    if receipt_check.get("within_multiplied_bound") is False:
        raise B4LiveModelApiError(
            "B4 provider prompt_tokens exceeded the calibrated preflight bound: "
            f"actual={receipt_check.get('provider_prompt_tokens')}, "
            f"bound={receipt_check.get('multiplied_upper_bound')}, "
            "observation persisted for the next preflight"
        )


def _calibration_path_v1(shared_root: Path) -> Path:
    return (
        Path(shared_root).resolve()
        / "transport_prompt_calibration_v1.json"
    )


def _load_prompt_calibration_v1(path: Path) -> dict[str, Any]:
    target = Path(path).resolve()
    if not target.exists():
        body = {
            "schema_version": _MODELAPI_PROMPT_CALIBRATION_SCHEMA_V1,
            "roles": {},
        }
        return {**body, "calibration_hash": canonical_hash(body)}
    value = _read(target)
    observed_hash = value.pop("calibration_hash", None)
    if (
        value.get("schema_version")
        != _MODELAPI_PROMPT_CALIBRATION_SCHEMA_V1
        or not isinstance(value.get("roles"), dict)
        or observed_hash != canonical_hash(value)
    ):
        raise B4LiveModelApiError(
            "B4 transport prompt calibration is malformed"
        )
    return {**value, "calibration_hash": observed_hash}


def _record_prompt_observation_v1(
    *,
    calibration_path: Path,
    role_id: str,
    transport_request_sha256: str,
    estimated_prompt_tokens: int,
    provider_prompt_tokens: int,
    attempt_usage_id: str | None,
) -> dict[str, Any]:
    if estimated_prompt_tokens <= 0 or provider_prompt_tokens <= 0:
        raise B4LiveModelApiError(
            "B4 prompt calibration observation is malformed"
        )
    calibration = _load_prompt_calibration_v1(calibration_path)
    body = {
        "schema_version": calibration["schema_version"],
        "roles": deepcopy(calibration["roles"]),
    }
    role = deepcopy(body["roles"].get(role_id) or {"samples": []})
    samples = list(role.get("samples") or [])
    sample_id = attempt_usage_id or canonical_hash(
        {
            "role_id": role_id,
            "transport_request_sha256": transport_request_sha256,
            "estimated_prompt_tokens": estimated_prompt_tokens,
            "provider_prompt_tokens": provider_prompt_tokens,
        }
    )
    if not any(row.get("sample_id") == sample_id for row in samples):
        samples.append(
            {
                "sample_id": sample_id,
                "transport_request_sha256": transport_request_sha256,
                "estimated_prompt_tokens": estimated_prompt_tokens,
                "provider_prompt_tokens": provider_prompt_tokens,
                "actual_to_estimate_ratio": round(
                    provider_prompt_tokens / estimated_prompt_tokens,
                    8,
                ),
            }
        )
    samples.sort(key=lambda row: str(row["sample_id"]))
    max_ratio = max(
        float(row["actual_to_estimate_ratio"]) for row in samples
    )
    max_additive_error = max(
        max(
            0,
            int(row["provider_prompt_tokens"])
            - int(row["estimated_prompt_tokens"]),
        )
        for row in samples
    )
    role = {
        "samples": samples,
        "max_observed_actual_to_estimate_ratio": round(max_ratio, 8),
        "max_observed_additive_error_tokens": max_additive_error,
        "safety_multiplier": round(
            max(
                _MODELAPI_PROMPT_INITIAL_SAFETY_MULTIPLIER_V1,
                max_ratio + _MODELAPI_PROMPT_OBSERVED_RATIO_MARGIN_V1,
            ),
            6,
        ),
    }
    body["roles"][role_id] = role
    sealed = {**body, "calibration_hash": canonical_hash(body)}
    target = Path(calibration_path).resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(target.suffix + ".tmp")
    _write(temporary, sealed)
    temporary.replace(target)
    return sealed


def _bindings(
    *,
    role_id: str,
    response_schema: Mapping[str, Any],
    capability_evidence: Mapping[str, Any],
    shared_root: Path,
    scheduler_root: Path,
    secret: str,
    credential_commitment_sha256: str,
    run_id: str,
    attempt_run_id: str,
    sender: Any | None,
) -> tuple[LiterarySharedRunnerBindingsV1, dict[str, Any]]:
    evidence = validate_capability_evidence(capability_evidence)
    if evidence.get("verdict") != "qualified":
        raise B4LiveModelApiError(f"B4 capability is not qualified: {role_id}")
    runtime = load_literary_shared_runtime_profile_v2(
        RUNTIME_PROFILE_PATH,
        expected_role_ids=RUNTIME_ROLE_IDS,
    )
    source_binding = dict(runtime.source_binding_for(role_id))
    source = _source_record(source_binding, credential_commitment_sha256)
    root = Path(shared_root).resolve()
    root.mkdir(parents=True, exist_ok=True)
    store = ContentAddressedArtifactStore(root / "artifacts")
    backend = SharedLlmBackend(
        credential_provider=MappingCredentialProvider(
            {source["credential_ref"]: secret}
        ),
        scheduler=PhysicalQuotaScheduler(scheduler_root),
        ledger=SharedLlmAttemptLedger(root / "attempt_ledger.sqlite3"),
        response_cache=ApplicationResponseCache(
            index_path=root / "response_cache.sqlite3",
            artifact_store=store,
        ),
        sender=sender or UrllibTransportSender(),
    )
    bindings = LiterarySharedRunnerBindingsV1(
        adapter=LiterarySharedLlmAttemptAdapter(backend=backend),
        api_source=None,
        capabilities={capability_binding_key(role_id, response_schema): evidence},
        run_id=run_id,
        attempt_run_id=attempt_run_id,
        structured_output=None,
        runtime_profile=runtime,
        api_sources_by_alias={source_binding["source_alias"]: source},
    )
    return bindings, source


def _source_record(binding: Mapping[str, Any], commitment: str) -> dict[str, Any]:
    if len(commitment) != 64 or any(c not in "0123456789abcdef" for c in commitment):
        raise B4LiveModelApiError("credential commitment is malformed")
    return {
        "schema_version": "api_source_v1",
        "source_id": binding["source_id"],
        "source_revision": binding["source_revision"],
        "source_class": binding["source_class"],
        "adapter_id": binding["adapter_id"],
        "protocol": binding["protocol"],
        "route_id": binding["route_id"],
        "endpoint_class": binding["endpoint_class"],
        "base_url": binding["base_url"],
        "credential_ref": binding["credential_ref"],
        "credential_commitment": commitment,
        "physical_quota_bucket_id": binding["physical_quota_bucket_id"],
        "enabled": True,
    }


def _fresh_output(path: Path) -> Path:
    output = Path(path).resolve()
    if output.exists():
        raise B4LiveModelApiError(f"output directory already exists: {output}")
    output.mkdir(parents=True)
    return output


def _model_json(text: str) -> dict[str, Any]:
    try:
        value = json.loads(text)
    except (TypeError, ValueError) as exc:
        raise B4LiveModelApiError("provider model content is not JSON") from exc
    if not isinstance(value, Mapping):
        raise B4LiveModelApiError("provider model content is not an object")
    return dict(value)


def _read(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    except (OSError, TypeError, ValueError) as exc:
        raise B4LiveModelApiError(f"cannot read JSON: {path}") from exc
    if not isinstance(value, Mapping):
        raise B4LiveModelApiError(f"JSON object required: {path}")
    return dict(value)


def _write(path: Path, value: Mapping[str, Any]) -> None:
    Path(path).write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


__all__ = [
    "B4LiveModelApiError",
    "estimate_b4_transport_request_v1",
    "run_address_anchor_live_v1",
    "run_translation_window_live_v1",
]
