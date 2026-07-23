from __future__ import annotations

import copy

import pytest

from pipeline.eval.contracts_v1 import (
    CanonicalPolicy,
    ContractValidationError,
    seal_payload,
)
from pipeline.eval.evaluation_component_usage_v1 import (
    EvaluationComponentUsageTrackerV1,
    validate_evaluation_component_usage_snapshot_chain_v1,
    validate_evaluation_component_usage_snapshot_v1,
)
from pipeline.llm_backend.contracts_v1 import (
    ContractValidationError as SharedContractValidationError,
)


STAGES = ("preflight", "chapter_d2l_preliminaries", "aggregation")
EXECUTION_TARGET = {
    "source_id": "google-official",
    "source_revision": "row1-v3",
    "physical_quota_bucket_id": "gemini-free-row1",
    "requested_model_id": "gemini-3.5-flash",
    "observed_model_id": "gemini-3.5-flash",
}
_POLICY = CanonicalPolicy(
    set_like_paths=frozenset(),
    semantic_sequence_paths=frozenset(
        {
            ("accepted_usage_ids",),
            ("accepted_cache_observation_ids",),
            ("stage_totals",),
        }
    ),
)


def _usage(
    record_id: str = "usage_001",
    *,
    prompt_tokens: int | None = 10,
    completion_tokens: int | None = 5,
    cost_usd: float | None = None,
    cost_status: str = "unknown",
) -> dict:
    total = (
        None
        if prompt_tokens is None or completion_tokens is None
        else prompt_tokens + completion_tokens
    )
    return {
        "schema_version": "llm_attempt_usage_v1",
        "attempt_usage_id": record_id,
        "seal_sha256": "1" * 64,
        "logical_request_id": "logical_001",
        "logical_request_sha256": "2" * 64,
        "semantic_attempt_index": 1,
        "transport_retry_ordinal": 0,
        "physical_attempt_index": 1,
        "request_id": "provider-request-001",
        "source_id": "google-official",
        "source_revision": "row1-v3",
        "physical_quota_bucket_id": "gemini-free-row1",
        "requested_model_id": "gemini-3.5-flash",
        "observed_model_id": "gemini-3.5-flash",
        "started_at_utc": "2026-07-23T00:00:00.000Z",
        "finished_at_utc": "2026-07-23T00:00:00.100Z",
        "latency_ms": 100,
        "outcome": "succeeded",
        "finish_reason": "stop",
        "prompt_tokens": prompt_tokens,
        "cached_input_tokens": 0 if prompt_tokens is not None else None,
        "completion_tokens": completion_tokens,
        "reasoning_tokens": 0 if completion_tokens is not None else None,
        "total_tokens": total,
        "cost_usd": cost_usd,
        "cost_status": cost_status,
        "cost_provenance": (
            {
                "kind": "unavailable",
                "reference_id": None,
                "reference_sha256": None,
            }
            if cost_status == "unknown"
            else {
                "kind": "pricing_manifest",
                "reference_id": "pricing-v1",
                "reference_sha256": "3" * 64,
            }
        ),
        "provider_usage_sha256": "4" * 64,
        "error_id": None,
    }


def _cache(record_id: str = "cache_001") -> dict:
    return {
        "schema_version": "cache_observation_v1",
        "observation_id": record_id,
        "seal_sha256": "1" * 64,
        "logical_request_id": "logical_001",
        "logical_request_sha256": "2" * 64,
        "attempt_usage_id": None,
        "cache_kind": "none",
        "cache_namespace": "evaluation.cache.fixture",
        "cache_key_sha256": None,
        "lookup_status": "not_checked",
        "provider_call_avoided": False,
        "provider_cached_input_tokens": None,
        "reused_artifact_sha256": None,
        "producer_seal_sha256": None,
        "producer_input_bindings_sha256": None,
        "producer_artifact_receipt_sha256": None,
        "observed_at_utc": "2026-07-23T00:00:00.101Z",
    }


def _tracker(snapshots=()) -> EvaluationComponentUsageTrackerV1:
    return EvaluationComponentUsageTrackerV1(
        workflow_run_id="workflow_fixture_001",
        component_run_id="evaluation_fixture_001",
        stage_ids=STAGES,
        snapshots=snapshots,
    )


def _accept_usage(
    tracker: EvaluationComponentUsageTrackerV1,
    usage: dict,
    *,
    component_attempt_id: str = "evalcomp_attempt_0001",
    component_attempt_index: int = 1,
):
    return tracker.accept_usage(
        usage,
        stage_id="chapter_d2l_preliminaries",
        role_id="evaluation.sf_bt.semantic_judge",
        source_ledger_ref="chapters/preliminaries/shared_llm_attempts.sqlite",
        execution_target=EXECUTION_TARGET,
        component_attempt_id=component_attempt_id,
        component_attempt_index=component_attempt_index,
        accepted_through_component_seq=len(tracker.snapshots) + 2,
        current_work_id=usage["logical_request_id"],
        generated_at="2026-07-23T00:00:01Z",
    )


def test_usage_and_cache_snapshots_preserve_null_cost_and_cumulative_totals() -> None:
    tracker = _tracker()
    first = _accept_usage(tracker, _usage())
    assert first is not None
    assert first["component_totals"]["physical_attempt_count"] == 1
    assert first["component_totals"]["total_tokens"] == 15
    assert first["component_totals"]["cost_usd"] is None
    assert first["component_totals"]["cost_status"] == "partial_unknown"

    second = tracker.accept_cache_observation(
        _cache(),
        stage_id="chapter_d2l_preliminaries",
        role_id="evaluation.sf_bt.semantic_judge",
        source_ledger_ref="chapters/preliminaries/shared_llm_attempts.sqlite",
        execution_target=EXECUTION_TARGET,
        component_attempt_id="evalcomp_attempt_0001",
        component_attempt_index=1,
        accepted_through_component_seq=3,
        current_work_id="logical_001",
        generated_at="2026-07-23T00:00:02Z",
    )
    assert second is not None
    assert second["component_totals"]["cache_observation_count"] == 1
    assert second["component_totals"]["cost_usd"] is None
    assert second["accepted_usage_ids"] == ["usage_001"]
    assert second["accepted_cache_observation_ids"] == ["cache_001"]


def test_resume_reuses_identical_record_without_double_count() -> None:
    tracker = _tracker()
    _accept_usage(tracker, _usage())
    resumed = _tracker(tracker.snapshots)
    assert _accept_usage(resumed, _usage()) is None
    assert len(resumed.snapshots) == 1
    assert resumed.latest["component_totals"]["physical_attempt_count"] == 1

    next_snapshot = _accept_usage(
        resumed,
        _usage("usage_002"),
        component_attempt_id="evalcomp_attempt_0002",
        component_attempt_index=2,
    )
    assert next_snapshot is not None
    assert next_snapshot["component_attempt_id"] == "evalcomp_attempt_0002"
    assert next_snapshot["component_attempt_index"] == 2
    assert next_snapshot["component_totals"]["physical_attempt_count"] == 2


def test_resume_rejects_same_id_with_different_bytes() -> None:
    tracker = _tracker()
    _accept_usage(tracker, _usage())
    resumed = _tracker(tracker.snapshots)
    changed = _usage(prompt_tokens=11, completion_tokens=5)
    with pytest.raises(ContractValidationError, match="changed bytes"):
        _accept_usage(resumed, changed)


def test_final_snapshot_keeps_totals_and_rejects_later_usage() -> None:
    tracker = _tracker()
    _accept_usage(tracker, _usage())
    final = tracker.finalize(
        stage_id="aggregation",
        component_attempt_id="evalcomp_attempt_0001",
        component_attempt_index=1,
        accepted_through_component_seq=3,
        generated_at="2026-07-23T00:00:03Z",
    )
    assert final is not None
    assert final["current_record"]["kind"] == "final"
    assert final["component_totals"] == tracker.snapshots[-2]["component_totals"]
    assert tracker.finalize(
        stage_id="aggregation",
        component_attempt_id="evalcomp_attempt_0001",
        component_attempt_index=1,
        accepted_through_component_seq=4,
        generated_at="2026-07-23T00:00:04Z",
    ) is None
    with pytest.raises(ContractValidationError, match="cannot follow"):
        _accept_usage(tracker, _usage("usage_002"))


def test_resealed_total_tamper_is_rejected() -> None:
    tracker = _tracker()
    snapshot = _accept_usage(tracker, _usage())
    assert snapshot is not None
    tampered = copy.deepcopy(snapshot)
    tampered["component_totals"]["prompt_tokens"] = 999
    tampered["integrity"]["usage_snapshot_sha256"] = "0" * 64
    tampered = seal_payload(
        tampered,
        policy=_POLICY,
        hash_path=("integrity", "usage_snapshot_sha256"),
    )
    with pytest.raises(ContractValidationError, match="component totals"):
        validate_evaluation_component_usage_snapshot_v1(
            tampered, stage_ids=STAGES
        )


def test_snapshot_chain_rejects_gap_and_foreign_component() -> None:
    tracker = _tracker()
    _accept_usage(tracker, _usage())
    tracker.accept_cache_observation(
        _cache(),
        stage_id="chapter_d2l_preliminaries",
        role_id="evaluation.sf_bt.semantic_judge",
        source_ledger_ref="chapters/preliminaries/shared_llm_attempts.sqlite",
        execution_target=EXECUTION_TARGET,
        component_attempt_id="evalcomp_attempt_0001",
        component_attempt_index=1,
        accepted_through_component_seq=3,
        current_work_id="logical_001",
        generated_at="2026-07-23T00:00:02Z",
    )
    snapshots = list(tracker.snapshots)
    with pytest.raises(ContractValidationError):
        validate_evaluation_component_usage_snapshot_chain_v1(
            snapshots[1:],
            workflow_run_id="workflow_fixture_001",
            component_run_id="evaluation_fixture_001",
            stage_ids=STAGES,
        )
    with pytest.raises(ContractValidationError, match="foreign component"):
        validate_evaluation_component_usage_snapshot_chain_v1(
            snapshots,
            workflow_run_id="workflow_fixture_001",
            component_run_id="other_component",
            stage_ids=STAGES,
        )


def test_nonfinite_cost_is_rejected_before_snapshot() -> None:
    tracker = _tracker()
    with pytest.raises(SharedContractValidationError, match="nonfinite"):
        _accept_usage(
            tracker,
            _usage(cost_usd=float("nan"), cost_status="calculated"),
        )


def test_unknown_attempt_usage_keeps_uncertifiable_token_totals_null() -> None:
    tracker = _tracker()
    _accept_usage(tracker, _usage())
    snapshot = _accept_usage(
        tracker,
        _usage(
            "usage_unknown",
            prompt_tokens=None,
            completion_tokens=None,
        ),
    )
    assert snapshot is not None
    totals = snapshot["component_totals"]
    assert totals["physical_attempt_count"] == 2
    assert totals["unknown_attempt_count"] == 1
    assert totals["prompt_tokens"] is None
    assert totals["completion_tokens"] is None
    assert totals["reasoning_tokens"] is None
    assert totals["total_tokens"] is None
