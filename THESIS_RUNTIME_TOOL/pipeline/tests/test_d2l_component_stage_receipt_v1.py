from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path

import pytest

from pipeline.prepass.d2l_component_stage_receipt_v1 import (
    D2LStageReceiptError,
    D2LStageObservationJournalWriter,
    build_stage_receipt,
    read_observation_journal,
    validate_stage_receipt,
    validate_stage_receipt_against_journal,
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


def _transport_failure_observation() -> dict[str, object]:
    return {
        "event": "transport_attempt_failed",
        "agent": "b2",
        "severity": "warning",
        "ts": "2026-07-22T00:00:02Z",
        "payload": {
            "attempt_usage_id": "usage_1",
            "logical_request_id": "req_1",
            "semantic_attempt_index": 1,
            "transport_retry_ordinal": 0,
            "physical_attempt_index": 1,
            "work_kind": "packet",
            "work_id": "packet_1",
            "provider_id": "provider",
            "model_id": "model",
            "source_id": "source",
            "source_revision": "source_v1",
            "masked_quota_bucket": "bucket-***",
            "latency_ms": 10,
            "prompt_tokens": None,
            "completion_tokens": None,
            "cached_input_tokens": None,
            "reasoning_tokens": None,
            "total_tokens": None,
            "cost_usd": None,
            "cost_status": "unknown",
            "reason_code": "http_500",
            "retry_class": "server_unavailable",
            "retry_disposition": "transport_retry_allowed",
        },
    }


def _response_observation(*, physical_attempt_index: int = 2) -> dict[str, object]:
    return {
        "event": "response_received",
        "agent": "b2",
        "severity": "info",
        "ts": "2026-07-22T00:00:05Z",
        "payload": {
            "usage": {
                "logical_request_id": "req_1",
                "physical_attempt_index": physical_attempt_index,
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


def test_stage_receipt_accepts_transport_retry_recovery_chain() -> None:
    second_request = deepcopy(_request_observation())
    second_request["ts"] = "2026-07-22T00:00:04Z"
    second_request["payload"]["physical_attempt_index"] = 2
    observations = [
        _request_observation(),
        _transport_failure_observation(),
        {
            "event": "retry",
            "agent": "b2",
            "severity": "warning",
            "ts": "2026-07-22T00:00:03Z",
            "payload": {
                "retry_kind": "transport",
                "index": 1,
                "max": 2,
                "reason_code": "server_unavailable",
                "logical_request_id": "req_1",
                "work_kind": "packet",
                "work_id": "packet_1",
            },
        },
        second_request,
        _response_observation(),
        {
            "event": "retry_summary",
            "agent": "b2",
            "severity": "info",
            "ts": "2026-07-22T00:00:06Z",
            "payload": {
                "logical_request_id": "req_1",
                "retry_kind": "transport",
                "retry_count": 1,
                "outcome": "recovered",
                "work_id": "packet_1",
                "reason_codes": ["server_unavailable"],
            },
        },
    ]

    validated = _validate(_receipt(observations))
    assert [row["event"] for row in validated["observations"]] == [
        "request_sent",
        "transport_attempt_failed",
        "retry",
        "request_sent",
        "response_received",
        "retry_summary",
    ]


def test_stage_receipt_rejects_false_recovered_retry_summary() -> None:
    observations = [
        _request_observation(),
        _transport_failure_observation(),
        {
            "event": "retry_summary",
            "agent": "b2",
            "severity": "info",
            "ts": "2026-07-22T00:00:03Z",
            "payload": {
                "logical_request_id": "req_1",
                "retry_kind": "transport",
                "retry_count": 1,
                "outcome": "recovered",
                "work_id": "packet_1",
                "reason_codes": ["server_unavailable"],
            },
        },
    ]

    with pytest.raises(
        D2LStageReceiptError,
        match="no successful response",
    ):
        _validate(_receipt(observations))


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("retry_count", 2, "count does not match failed attempts"),
        ("reason_codes", ["timeout"], "reasons do not match failed attempts"),
    ],
)
def test_stage_receipt_rejects_retry_summary_evidence_drift(
    field: str,
    value: object,
    message: str,
) -> None:
    second_request = deepcopy(_request_observation())
    second_request["payload"]["physical_attempt_index"] = 2
    summary = {
        "event": "retry_summary",
        "agent": "b2",
        "severity": "info",
        "ts": "2026-07-22T00:00:06Z",
        "payload": {
            "logical_request_id": "req_1",
            "retry_kind": "transport",
            "retry_count": 1,
            "outcome": "recovered",
            "work_id": "packet_1",
            "reason_codes": ["server_unavailable"],
        },
    }
    summary["payload"][field] = value
    observations = [
        _request_observation(),
        _transport_failure_observation(),
        {
            "event": "retry",
            "agent": "b2",
            "severity": "warning",
            "ts": "2026-07-22T00:00:03Z",
            "payload": {
                "retry_kind": "transport",
                "index": 1,
                "max": 2,
                "reason_code": "server_unavailable",
                "logical_request_id": "req_1",
                "work_kind": "packet",
                "work_id": "packet_1",
            },
        },
        second_request,
        _response_observation(),
        summary,
    ]

    with pytest.raises(D2LStageReceiptError, match=message):
        _validate(_receipt(observations))


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


def _journal_writer(path: Path) -> D2LStageObservationJournalWriter:
    return D2LStageObservationJournalWriter(
        path=path,
        workflow_run_id="wf_receipt_test",
        component_run_id="tr_receipt_test",
        component_attempt_id=1,
        stage_id="b2_admission_translation",
        producer="b2",
        work_id="packet_1",
    )


def test_observation_journal_is_hash_chained_and_receipt_exact(tmp_path: Path) -> None:
    path = tmp_path / "component_observations.jsonl"
    writer = _journal_writer(path)
    writer.append(_request_observation())
    entries = read_observation_journal(path)
    receipt = _validate(_receipt())

    assert entries[0]["journal_seq"] == 1
    assert entries[0]["previous_entry_sha256"] is None
    validate_stage_receipt_against_journal(
        receipt,
        journal_entries=entries,
    )

    drifted = deepcopy(receipt)
    drifted["observations"][0]["payload"]["model_id"] = "other"
    with pytest.raises(D2LStageReceiptError, match="exactly match"):
        validate_stage_receipt_against_journal(
            drifted,
            journal_entries=entries,
        )


def test_observation_journal_ignores_only_unterminated_live_tail(
    tmp_path: Path,
) -> None:
    path = tmp_path / "component_observations.jsonl"
    _journal_writer(path).append(_request_observation())
    path.write_bytes(path.read_bytes()[:-1])

    assert read_observation_journal(path, allow_incomplete_tail=True) == []
    with pytest.raises(D2LStageReceiptError, match="unterminated"):
        read_observation_journal(path)


def test_observation_journal_rejects_hash_tamper(tmp_path: Path) -> None:
    path = tmp_path / "component_observations.jsonl"
    _journal_writer(path).append(_request_observation())
    row = json.loads(path.read_text(encoding="utf-8"))
    row["observation"]["payload"]["provider_id"] = "tampered"
    path.write_text(
        json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    with pytest.raises(D2LStageReceiptError, match="hash drift"):
        read_observation_journal(path)
