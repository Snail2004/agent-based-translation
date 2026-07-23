from __future__ import annotations

from copy import deepcopy

import pytest

from pipeline.prepass.d2l_component_stage_receipt_v1 import (
    D2LStageReceiptError,
    build_stage_receipt,
    validate_stage_receipt,
)
from pipeline.prepass.d2l_console_replay_contract_v1 import (
    build_component_manifest,
    build_stage_plan,
)


def _manifest() -> dict[str, object]:
    source_specs = {
        "document": ("source_document", "document_v1"),
        "structure_manifest": ("structure_manifest", "structure_manifest_v1"),
        "asset_manifest": ("asset_manifest", "asset_manifest_v1"),
        "admitted_projection": ("admitted_projection", "admitted_projection_v1"),
        "normalization_receipt": ("normalization_receipt", "normalization_receipt_v1"),
        "package_seal": ("source_package_seal", "source_package_seal_v1"),
    }
    return build_component_manifest(
        workflow_run_id="wf_receipt_test",
        component_run_id="tr_receipt_test",
        component_attempt_id=1,
        pipeline_id="d2l_terminology",
        pipeline_version="test_v1",
        source_binding={
            "schema": "canonical_source_binding_v1",
            **{
                key: {
                    "artifact_ref": f"src_{key}",
                    "artifact_kind": artifact_kind,
                    "schema_version": schema_version,
                    "sha256": "A" * 64,
                    "sha256_kind": "physical",
                }
                for key, (artifact_kind, schema_version) in source_specs.items()
            },
        },
        config_sha256="B" * 64,
        code_revision="C" * 40,
        selected_chapter_ids=["d2l_multilayer_perceptrons"],
        started_at="2026-07-22T00:00:00Z",
        updated_at="2026-07-22T00:00:00Z",
        stages=build_stage_plan(),
    )


def _request_observation() -> dict[str, object]:
    return {
        "event": "request_sent",
        "agent": "b2",
        "severity": "info",
        "ts": "2026-07-22T00:00:01Z",
        "payload": {
            "logical_request_id": "req_1",
            "physical_attempt_index": 1,
            "work_kind": "packet",
            "work_id": "packet_1",
            "provider_id": "provider",
            "model_id": "model",
            "source_id": "source",
            "masked_quota_bucket": "bucket-***",
        },
    }


def _receipt(observations: list[dict[str, object]] | None = None) -> dict[str, object]:
    return build_stage_receipt(
        workflow_run_id="wf_receipt_test",
        component_run_id="tr_receipt_test",
        component_attempt_id=1,
        stage_id="b2_admission_translation",
        producer="b2",
        work_id="packet_1",
        observations=observations or [_request_observation()],
    )


def _validate(receipt: dict[str, object]) -> dict[str, object]:
    return validate_stage_receipt(
        receipt,
        manifest=_manifest(),
        stage_id="b2_admission_translation",
        producer="b2",
        work_id="packet_1",
        start_component_seq=4,
    )


def test_stage_receipt_validates_closed_observation_batch() -> None:
    validated = _validate(_receipt())

    assert validated["receipt_sha256"] == _receipt()["receipt_sha256"]
    assert len(validated["observations"]) == 1


def test_stage_receipt_rejects_hash_drift() -> None:
    receipt = _receipt()
    receipt["observations"][0]["payload"]["provider_id"] = "changed"

    with pytest.raises(D2LStageReceiptError, match="hash drift"):
        _validate(receipt)


def test_stage_receipt_rejects_forbidden_semantic_payload() -> None:
    receipt = _receipt()
    receipt["observations"][0]["payload"]["raw_prompt"] = "hidden"

    with pytest.raises(D2LStageReceiptError, match="forbidden key"):
        _validate(receipt)


def test_stage_receipt_rejects_response_without_request() -> None:
    response = {
        "event": "response_received",
        "agent": "b2",
        "severity": "info",
        "ts": "2026-07-22T00:00:02Z",
        "payload": {
            "usage": {
                "logical_request_id": "req_1",
                "physical_attempt_index": 1,
                "provider_id": "provider",
                "model_id": "model",
                "source_id": "source",
                "masked_quota_bucket": "bucket-***",
                "prompt_tokens": 1,
                "completion_tokens": 1,
                "cached_input_tokens": 0,
                "reasoning_tokens": 0,
                "total_tokens": 2,
                "latency_ms": 10,
                "finish_reason": "stop",
                "cost_usd": None,
                "currency": None,
                "cost_status": "unknown",
                "cache_status": "miss",
                "cache_mechanism": "none",
            }
        },
    }

    with pytest.raises(D2LStageReceiptError, match="no preceding request_sent"):
        _validate(_receipt([response]))


def test_stage_receipt_rejects_duplicate_physical_request() -> None:
    request = _request_observation()
    receipt = _receipt([request, deepcopy(request)])

    with pytest.raises(D2LStageReceiptError, match="duplicate request_sent"):
        _validate(receipt)


def test_stage_receipt_accepts_component_scoped_cost_snapshot() -> None:
    receipt = _receipt(
        [
            {
                "event": "cost_snapshot",
                "agent": "b2",
                "severity": "info",
                "ts": "2026-07-22T00:00:03Z",
                "payload": {
                    "scope": "stage:b2_admission_translation",
                    "logical_request_count": 1,
                    "physical_attempt_count": 1,
                    "prompt_tokens": 10,
                    "completion_tokens": 2,
                    "cached_input_tokens": 0,
                    "reasoning_tokens": 0,
                    "total_tokens": 12,
                    "cost_usd": None,
                    "currency": None,
                    "cost_status": "unknown",
                    "cache_counters": {"miss": 1},
                },
            }
        ]
    )

    assert _validate(receipt)["observations"][0]["event"] == "cost_snapshot"
