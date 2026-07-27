from __future__ import annotations

from pipeline.scripts.run_b0_entity_conflict_auditor_experiment import (
    _transport_config,
)
from pipeline.scripts.run_cross_chapter_claim_auditor_v1_live import (
    _select_bucket as select_claim_bucket,
)
from pipeline.scripts.run_incremental_identity_auditor_v1_live import (
    _select_bucket as select_identity_bucket,
)


def test_local_auditor_can_be_sealed_to_openai_key1_only() -> None:
    transport = _transport_config(("openai-row1",))
    assert transport.role_quota_gate_ids["auditor"] == (
        "openai-row1-gpt54",
    )
    assert all(
        all(gate_id.startswith("openai-row1-") for gate_id in gate_ids)
        for gate_ids in transport.role_quota_gate_ids.values()
    )
    assert {
        str(gate["quota_bucket_id"]) for gate in transport.quota_gates.values()
    } == {"openai-row1"}


def test_cross_chapter_auditors_do_not_fall_back_to_key2() -> None:
    preflight = {"usage_by_bucket_model": {}, "calls_by_bucket_model": {}}

    assert select_claim_bucket(
        preflight=preflight,
        reserve_tokens=100,
        model_id="gpt-5.4",
        bucket_order=("openai-row1",),
    )[0] == "openai-row1"
    assert select_identity_bucket(
        preflight=preflight,
        reserve_tokens=100,
        bucket_order=("openai-row1",),
    )[0] == "openai-row1"
