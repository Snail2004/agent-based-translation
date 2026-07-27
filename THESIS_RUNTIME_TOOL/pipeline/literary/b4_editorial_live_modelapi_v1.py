"""Fail-closed ModelAPI consumer for B4 Editorial Review."""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from typing import Any, Mapping

from pipeline.llm_backend import (
    ApplicationResponseCache,
    ContentAddressedArtifactStore,
    MappingCredentialProvider,
    PhysicalQuotaScheduler,
    SharedLlmAttemptLedger,
    SharedLlmBackend,
    UrllibTransportSender,
    validate_capability_evidence,
)
from pipeline.literary.b4_editorial_review_v1 import (
    RESPONSE_SCHEMA_VERSION,
    ROLE_ID,
    build_editorial_review_artifact_v1,
    render_editorial_review_request_v1,
    validate_editorial_review_response_v1,
)
from pipeline.literary.b4_live_modelapi_v1 import (
    _calibrating_semantic_validator_v1,
    _calibration_path_v1,
    _enforce_transport_preflight_v1,
    _fresh_output,
    _model_json,
    _persist_receipt_calibration_v1,
    _source_record,
    _transport_preflight_v1,
    _write,
)
from pipeline.literary.chapter_cycle_shared_runtime_v1 import (
    LiterarySharedRunnerBindingsV1,
    capability_binding_key,
)
from pipeline.literary.checkpoint import canonical_hash
from pipeline.literary.modelapi_b4_editorial_capability_probe_v1 import (
    RUNTIME_PROFILE_PATH,
    validator_ref_v1,
)
from pipeline.literary.shared_llm_adapter_v1 import (
    LiterarySharedLlmAttemptAdapter,
)
from pipeline.literary.shared_runtime_profile_v2 import (
    load_literary_shared_runtime_profile_v2,
)


EDITORIAL_RUNTIME_ROLE_IDS = frozenset({ROLE_ID})


class B4EditorialLiveModelApiError(RuntimeError):
    pass


def run_editorial_review_live_v1(
    *,
    review_packet: Mapping[str, Any],
    style_profile: str,
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
    rendered = render_editorial_review_request_v1(
        review_packet=review_packet,
        style_profile=style_profile,
    )
    request = {
        "messages": [dict(row) for row in rendered.messages],
        "response_schema": deepcopy(rendered.response_schema),
        "request_fingerprint": rendered.request_fingerprint,
    }
    _write(output / "request.json", request)
    _write(output / "editorial_review_packet.json", rendered.packet)
    (output / "style_profile.txt").write_text(
        style_profile,
        encoding="utf-8",
    )

    bindings, source = _bindings(
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
        role_id=ROLE_ID,
        request=request,
        schema_name=RESPONSE_SCHEMA_VERSION,
        calibration_path=_calibration_path_v1(shared_root),
    )
    _write(output / "transport_preflight.json", preflight)
    _enforce_transport_preflight_v1(preflight)

    def validate(payload: Mapping[str, Any]) -> Mapping[str, Any]:
        return validate_editorial_review_response_v1(
            rendered=rendered,
            response=payload,
        )

    stage_id = "literary_b4_editorial_review"
    logical_request_id = (
        "literary_b4_editorial_"
        + canonical_hash(
            {
                "run_id": run_id,
                "chapter_id": rendered.packet["chapter_id"],
                "batch_index": rendered.packet["batch_index"],
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
        role_id=ROLE_ID,
        stage_id=stage_id,
        logical_request_id=logical_request_id,
        attempt_run_id=attempt_run_id,
    )
    try:
        result = bindings.execute_accepted_request(
            role_id=ROLE_ID,
            stage_id=stage_id,
            logical_request_id=logical_request_id,
            request=request,
            schema_name=RESPONSE_SCHEMA_VERSION,
            semantic_validator=semantic_validator,
            validator_ref=validator_ref_v1(),
            application_contract_id="literary.b4.editorial_review.application",
            application_contract_revision="v1",
            output_dir=output,
            additional_input_bindings=(
                {
                    "name": "b4_editorial_review_packet",
                    "sha256": rendered.packet["artifact_hash"],
                },
                {
                    "name": "b4_source_translation",
                    "sha256": rendered.packet[
                        "source_translation_artifact_hash"
                    ],
                },
                {
                    "name": "b4_translator_pack",
                    "sha256": rendered.packet[
                        "translator_pack_artifact_hash"
                    ],
                },
                {
                    "name": "b4_translation_lint",
                    "sha256": rendered.packet[
                        "lint_report_artifact_hash"
                    ],
                },
            ),
        )
    except Exception:
        _persist_receipt_calibration_v1(
            bindings=bindings,
            preflight=preflight,
            calibration_path=_calibration_path_v1(shared_root),
            output=output,
            role_id=ROLE_ID,
            stage_id=stage_id,
            logical_request_id=logical_request_id,
            attempt_run_id=attempt_run_id,
            required=False,
        )
        raise
    if result.status != "semantic_accepted" or result.provider_called is not True:
        raise B4EditorialLiveModelApiError(
            "Editorial Review did not complete one live call"
        )
    receipt = _read_json(output / "shared_attempt_receipt.json")
    receipt_check = _persist_receipt_calibration_v1(
        bindings=bindings,
        preflight=preflight,
        calibration_path=_calibration_path_v1(shared_root),
        output=output,
        role_id=ROLE_ID,
        stage_id=stage_id,
        logical_request_id=logical_request_id,
        attempt_run_id=attempt_run_id,
        required=True,
    )
    artifact = build_editorial_review_artifact_v1(
        rendered=rendered,
        validated_response=result.semantic_payload,
        provider_receipt=receipt,
        provider_called=True,
    )
    _write(output / "provider_response.json", result.response_payload)
    _write(output / "model_response.json", _model_json(result.response_text))
    _write(output / "validated_response.json", result.semantic_payload)
    _write(output / "editorial_review.json", artifact)
    _write(output / "run_seal.json", result.seal)
    report_body = {
        "schema_version": "literary_b4_editorial_review_live_report_v1",
        "status": "complete",
        "chapter_id": artifact["chapter_id"],
        "batch_index": artifact["batch_index"],
        "batch_count": artifact["batch_count"],
        "git_head": current_git_head,
        "source_id": source["source_id"],
        "requested_model_id": "gpt-5.4",
        "style_profile_version": artifact["style_profile_version"],
        "candidate_block_count": len(artifact["candidate_block_ids"]),
        "action_counts": deepcopy(artifact["action_counts"]),
        "severity_counts": deepcopy(artifact["severity_counts"]),
        "usage": dict(result.usage) if result.usage is not None else None,
        "transport_preflight": deepcopy(preflight),
        "transport_preflight_receipt_check": deepcopy(receipt_check),
        "provider_called": True,
        "provider_retries": 0,
        "fallback_performed": False,
        "translation_text_mutation_performed": False,
        "semantic_record_mutation_performed": False,
        "artifact_hash": artifact["artifact_hash"],
    }
    report = {**report_body, "report_hash": canonical_hash(report_body)}
    _write(output / "live_report.json", report)
    return report


def _bindings(
    *,
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
        raise B4EditorialLiveModelApiError(
            "Editorial Review capability is not qualified"
        )
    runtime = load_literary_shared_runtime_profile_v2(
        RUNTIME_PROFILE_PATH,
        expected_role_ids=EDITORIAL_RUNTIME_ROLE_IDS,
    )
    source_binding = dict(runtime.source_binding_for(ROLE_ID))
    source = _source_record(
        source_binding,
        credential_commitment_sha256,
    )
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
        capabilities={
            capability_binding_key(ROLE_ID, response_schema): evidence
        },
        run_id=run_id,
        attempt_run_id=attempt_run_id,
        structured_output=None,
        runtime_profile=runtime,
        api_sources_by_alias={source_binding["source_alias"]: source},
    )
    return bindings, source


def _read_json(path: Path) -> dict[str, Any]:
    import json

    try:
        value = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    except (OSError, TypeError, ValueError) as exc:
        raise B4EditorialLiveModelApiError(
            f"cannot read JSON: {path}"
        ) from exc
    if not isinstance(value, Mapping):
        raise B4EditorialLiveModelApiError(
            f"JSON object required: {path}"
        )
    return dict(value)


__all__ = [
    "B4EditorialLiveModelApiError",
    "EDITORIAL_RUNTIME_ROLE_IDS",
    "run_editorial_review_live_v1",
]
