from __future__ import annotations

import json
from pathlib import Path

import pytest

from pipeline.literary.chapter_cycle_resilience_v1 import (
    AttemptKind,
    ChapterCycleResilienceError,
    ContractIssue,
    FailureClass,
    IntegrityOrLineageFailure,
    ModelEndpoint,
    ResilientStageHalt,
    RowSourceIssue,
    SemanticStatus,
    StageExecutionSpec,
    StageRequest,
    StageResponse,
    TransportFailure,
    ValidationOutcome,
    WholeResponseContractFailure,
    build_contract_repair_directive,
    execute_resilient_stage,
    route_row_source_issue,
)
from pipeline.literary.checkpoint import canonical_hash


def _endpoint(
    bucket: str = "gemini-row1",
    *,
    provider: str = "google_genai",
    model: str = "gemini-3.5-flash",
) -> ModelEndpoint:
    return ModelEndpoint(
        provider=provider,
        model_id=model,
        quota_bucket_id=bucket,
        credential_revision=f"{bucket}-v1",
    )


def _request() -> StageRequest:
    payload = {
        "messages": [{"role": "user", "content": "book-neutral source"}],
        "response_schema": {"type": "object"},
    }
    return StageRequest(
        request_fingerprint=canonical_hash(payload),
        semantic_payload_hash=canonical_hash({"source": "chapter"}),
        response_schema_hash=canonical_hash(payload["response_schema"]),
        prompt_version="literary_b0_inventory_v1_4",
        payload=payload,
    )


def _spec(
    tmp_path: Path,
    *,
    role: str = "b0",
    primary: tuple[ModelEndpoint, ...] | None = None,
    fallback: tuple[ModelEndpoint, ...] = (),
    pointer: Path | None = None,
) -> StageExecutionSpec:
    return StageExecutionSpec(
        stage_id="ch001_b0",
        chapter_id="wh_ch01",
        stage_role=role,
        request=_request(),
        primary_endpoints=primary or (_endpoint(),),
        b0_fallback_endpoints=fallback,
        output_dir=tmp_path / "run",
        protected_checkpoint_pointer=pointer,
    )


def _accepted(
    _response: StageResponse,
    _plan,
    attempt_dir: Path,
) -> ValidationOutcome:
    artifact = attempt_dir / "accepted.json"
    artifact.write_text('{"accepted":true}\n', encoding="utf-8")
    return ValidationOutcome(
        semantic_status=SemanticStatus.ACCEPTED,
        result_hash=canonical_hash({"accepted": True}),
        artifact_paths=(str(artifact),),
    )


def test_primary_success_writes_attempt_ledger(tmp_path: Path) -> None:
    plans = []

    def invoke(plan, _attempt_dir: Path) -> StageResponse:
        plans.append(plan)
        return StageResponse(
            parsed_payload={"ok": True},
            model_actual=plan.endpoint.model_id,
            usage={"input_tokens": 10, "output_tokens": 2},
        )

    report = execute_resilient_stage(
        spec=_spec(tmp_path),
        invoke=invoke,
        validate=_accepted,
    )

    assert report["status"] == "accepted"
    assert report["attempt_count"] == 1
    assert plans[0].attempt_kind is AttemptKind.PRIMARY
    ledger = json.loads(
        (tmp_path / "run" / "attempt_ledger.json").read_text(encoding="utf-8")
    )
    assert ledger["latest_attempt_number"] == 1
    assert ledger["attempts"][0]["production_publish_performed"] is False
    assert (
        tmp_path
        / "run"
        / "attempts"
        / "001_primary"
        / "stage_response.json"
    ).is_file()


def test_transport_retry_preserves_exact_request_fingerprint(
    tmp_path: Path,
) -> None:
    observed = []

    def invoke(plan, _attempt_dir: Path) -> StageResponse:
        observed.append(plan)
        if len(observed) == 1:
            raise TransportFailure("timeout", rotate_credential=False)
        return StageResponse(parsed_payload={"ok": True})

    report = execute_resilient_stage(
        spec=_spec(tmp_path),
        invoke=invoke,
        validate=_accepted,
    )

    assert report["attempt_count"] == 2
    assert observed[1].attempt_kind is AttemptKind.TRANSPORT_RETRY
    assert (
        observed[0].effective_request_fingerprint
        == observed[1].effective_request_fingerprint
        == _request().request_fingerprint
    )
    assert observed[0].endpoint == observed[1].endpoint


def test_quota_transport_failure_rotates_only_same_model_family(
    tmp_path: Path,
) -> None:
    observed = []
    endpoints = (_endpoint("row1"), _endpoint("row2"))

    def invoke(plan, _attempt_dir: Path) -> StageResponse:
        observed.append(plan)
        if len(observed) == 1:
            raise TransportFailure("quota_exhausted", rotate_credential=True)
        return StageResponse(parsed_payload={"ok": True})

    execute_resilient_stage(
        spec=_spec(tmp_path, primary=endpoints),
        invoke=invoke,
        validate=_accepted,
    )

    assert [row.endpoint.quota_bucket_id for row in observed] == ["row1", "row2"]
    assert {row.endpoint.model_id for row in observed} == {"gemini-3.5-flash"}
    assert {row.endpoint.provider for row in observed} == {"google_genai"}


def test_third_transport_failure_halts_same_stage(tmp_path: Path) -> None:
    def invoke(_plan, _attempt_dir: Path) -> StageResponse:
        raise TransportFailure("provider_5xx")

    with pytest.raises(ResilientStageHalt) as caught:
        execute_resilient_stage(
            spec=_spec(tmp_path),
            invoke=invoke,
            validate=_accepted,
        )

    report = caught.value.report
    assert report["failure_class"] == FailureClass.TRANSPORT.value
    assert report["halt_reason"] == "transport_retry_exhausted"
    assert report["attempt_count"] == 3


def test_console_can_set_zero_transport_retry(tmp_path: Path) -> None:
    calls = 0

    def invoke(_plan, _attempt_dir: Path) -> StageResponse:
        nonlocal calls
        calls += 1
        raise TransportFailure("provider_5xx")

    from pipeline.literary.chapter_cycle_resilience_v1 import ResiliencePolicy

    with pytest.raises(ResilientStageHalt) as caught:
        execute_resilient_stage(
            spec=_spec(tmp_path),
            invoke=invoke,
            validate=_accepted,
            policy=ResiliencePolicy(max_transport_retries_per_request=0),
        )

    assert calls == 1
    assert caught.value.report["attempt_count"] == 1


def test_contract_repair_keeps_semantic_and_schema_hashes(
    tmp_path: Path,
) -> None:
    plans = []
    validations = 0

    def invoke(plan, _attempt_dir: Path) -> StageResponse:
        plans.append(plan)
        return StageResponse(parsed_payload={"ok": True})

    def validate(response, plan, attempt_dir: Path) -> ValidationOutcome:
        nonlocal validations
        validations += 1
        if validations == 1:
            raise WholeResponseContractFailure(
                [ContractIssue("missing_required_field", "$.entity_candidates")]
            )
        return _accepted(response, plan, attempt_dir)

    report = execute_resilient_stage(
        spec=_spec(tmp_path),
        invoke=invoke,
        validate=validate,
    )

    assert report["attempt_count"] == 2
    assert plans[1].attempt_kind is AttemptKind.CONTRACT_REPAIR
    assert plans[1].repair_directive is not None
    assert (
        plans[0].request.semantic_payload_hash
        == plans[1].request.semantic_payload_hash
    )
    assert (
        plans[0].request.response_schema_hash
        == plans[1].request.response_schema_hash
    )
    directive = plans[1].repair_directive.to_dict()
    encoded = json.dumps(directive, sort_keys=True).casefold()
    assert "gold" not in encoded
    assert "oracle" not in encoded
    assert "target_answer" not in encoded


def test_repair_directive_rejects_answer_shaped_issue_tokens() -> None:
    with pytest.raises(ChapterCycleResilienceError, match="answer-shaped"):
        ContractIssue("expected_answer_mismatch", "$.entity_candidates")

    failure = WholeResponseContractFailure(
        [ContractIssue("missing_required_field", "$.entity_candidates")]
    )
    directive = build_contract_repair_directive(failure)
    assert directive.to_dict()["issues"] == [
        {"code": "missing_required_field", "field_path": "$.entity_candidates"}
    ]


def test_b0_uses_gpt54_only_after_repair_failure(tmp_path: Path) -> None:
    plans = []
    validations = 0
    fallback = (
        _endpoint(
            "openai-row1",
            provider="openai",
            model="gpt-5.4",
        ),
    )

    def invoke(plan, _attempt_dir: Path) -> StageResponse:
        plans.append(plan)
        return StageResponse(
            parsed_payload={"ok": True},
            model_actual=plan.endpoint.model_id,
        )

    def validate(response, plan, attempt_dir: Path) -> ValidationOutcome:
        nonlocal validations
        validations += 1
        if validations < 3:
            raise WholeResponseContractFailure(
                [ContractIssue("invalid_closed_enum", "$.entity_candidates[0].kind")]
            )
        return _accepted(response, plan, attempt_dir)

    report = execute_resilient_stage(
        spec=_spec(tmp_path, fallback=fallback),
        invoke=invoke,
        validate=validate,
    )

    assert [row.attempt_kind for row in plans] == [
        AttemptKind.PRIMARY,
        AttemptKind.CONTRACT_REPAIR,
        AttemptKind.B0_MODEL_FALLBACK,
    ]
    assert report["model_actual"] == "gpt-5.4"
    assert report["attempt_count"] == 3


def test_auditor_cannot_configure_smaller_model_fallback(tmp_path: Path) -> None:
    with pytest.raises(ChapterCycleResilienceError, match="cannot configure"):
        _spec(
            tmp_path,
            role="auditor",
            primary=(
                _endpoint(
                    "openai-row1",
                    provider="openai",
                    model="gpt-5.4",
                ),
            ),
            fallback=(_endpoint("mini", provider="openai", model="gpt-5.4-mini"),),
        )


def test_auditor_contract_failure_repairs_once_then_pauses(
    tmp_path: Path,
) -> None:
    plans = []
    primary = (
        _endpoint("openai-row1", provider="openai", model="gpt-5.4"),
        _endpoint("openai-row2", provider="openai", model="gpt-5.4"),
    )

    def invoke(plan, _attempt_dir: Path) -> StageResponse:
        plans.append(plan)
        return StageResponse(parsed_payload={})

    def reject(_response, _plan, _attempt_dir: Path) -> ValidationOutcome:
        raise WholeResponseContractFailure(
            [ContractIssue("missing_required_field", "$.candidate_actions")]
        )

    with pytest.raises(ResilientStageHalt) as caught:
        execute_resilient_stage(
            spec=_spec(tmp_path, role="auditor", primary=primary),
            invoke=invoke,
            validate=reject,
        )

    assert caught.value.report["attempt_count"] == 2
    assert {row.endpoint.model_id for row in plans} == {"gpt-5.4"}
    assert caught.value.report["halt_reason"] == "response_contract_repair_exhausted"


def test_optional_alias_failure_is_downscoped_without_entity_rejection() -> None:
    disposition = route_row_source_issue(
        RowSourceIssue(
            row_id="alias_01",
            row_kind="global_alias",
            field_path="aliases[0]",
            source_block_ids=("bk_ch01_b007",),
            reason_code="surface_not_located",
            load_bearing_for_identity=False,
        )
    )

    assert disposition["row_action"] == "exclude_defective_row"
    assert disposition["resulting_status"] == "row_downscoped"
    assert disposition["authority_effect"] == "none"


def test_load_bearing_source_failure_keeps_entity_pending() -> None:
    disposition = route_row_source_issue(
        RowSourceIssue(
            row_id="ent_01",
            row_kind="entity_candidate",
            field_path="canonical_surface",
            source_block_ids=("bk_ch01_b007",),
            reason_code="canonical_surface_not_located",
            load_bearing_for_identity=True,
        )
    )

    assert disposition["row_action"] == "retain_entity_pending"
    assert disposition["resulting_status"] == "pending"
    assert disposition["authority_effect"] == "none"


def test_integrity_failure_has_zero_retry_and_preserves_pointer(
    tmp_path: Path,
) -> None:
    pointer = tmp_path / "current.json"
    original = '{"generation":7}\n'
    pointer.write_text(original, encoding="utf-8")

    def invoke(_plan, _attempt_dir: Path) -> StageResponse:
        raise IntegrityOrLineageFailure("state_lineage_mismatch")

    with pytest.raises(ResilientStageHalt) as caught:
        execute_resilient_stage(
            spec=_spec(tmp_path, pointer=pointer),
            invoke=invoke,
            validate=_accepted,
        )

    assert caught.value.report["attempt_count"] == 1
    assert (
        caught.value.report["failure_class"]
        == FailureClass.INTEGRITY_OR_LINEAGE.value
    )
    assert pointer.read_text(encoding="utf-8") == original


def test_external_pointer_mutation_is_integrity_failure(tmp_path: Path) -> None:
    pointer = tmp_path / "current.json"
    pointer.write_text('{"generation":7}\n', encoding="utf-8")

    def invoke(_plan, _attempt_dir: Path) -> StageResponse:
        pointer.write_text('{"generation":8}\n', encoding="utf-8")
        return StageResponse(parsed_payload={"ok": True})

    with pytest.raises(ResilientStageHalt) as caught:
        execute_resilient_stage(
            spec=_spec(tmp_path, pointer=pointer),
            invoke=invoke,
            validate=_accepted,
        )

    assert (
        caught.value.report["failure_class"]
        == FailureClass.INTEGRITY_OR_LINEAGE.value
    )
    assert (
        caught.value.report["halt_reason"]
        == "protected_checkpoint_pointer_changed"
    )
    assert caught.value.report["attempt_count"] == 1


def test_unclassified_exception_never_retries(tmp_path: Path) -> None:
    def invoke(_plan, _attempt_dir: Path) -> StageResponse:
        raise ValueError("unexpected adapter bug")

    with pytest.raises(ResilientStageHalt) as caught:
        execute_resilient_stage(
            spec=_spec(tmp_path),
            invoke=invoke,
            validate=_accepted,
        )

    assert caught.value.report["attempt_count"] == 1
    assert caught.value.report["failure_class"] is None
    assert caught.value.report["halt_reason"] == "unclassified_failure"


def test_semantic_pending_is_accepted_without_retry(tmp_path: Path) -> None:
    calls = 0

    def invoke(_plan, _attempt_dir: Path) -> StageResponse:
        nonlocal calls
        calls += 1
        return StageResponse(parsed_payload={"status": "pending"})

    def pending(
        _response: StageResponse,
        _plan,
        _attempt_dir: Path,
    ) -> ValidationOutcome:
        return ValidationOutcome(
            semantic_status=SemanticStatus.PENDING,
            result_hash=canonical_hash({"status": "pending"}),
        )

    report = execute_resilient_stage(
        spec=_spec(tmp_path),
        invoke=invoke,
        validate=pending,
    )

    assert calls == 1
    assert report["status"] == "accepted_semantic_pending"
    ledger = json.loads(
        (tmp_path / "run" / "attempt_ledger.json").read_text(encoding="utf-8")
    )
    assert (
        ledger["attempts"][0]["failure_class"]
        == FailureClass.SEMANTIC_PENDING.value
    )


def test_endpoint_families_cannot_mix_models_or_providers(tmp_path: Path) -> None:
    with pytest.raises(ChapterCycleResilienceError, match="one provider and one model"):
        _spec(
            tmp_path,
            primary=(
                _endpoint("row1"),
                _endpoint("row2", provider="openai", model="gpt-5.4"),
            ),
        )


def test_b0_fallback_model_is_pinned_to_gpt54(tmp_path: Path) -> None:
    fallback = (
        _endpoint("openai-row1", provider="openai", model="gpt-5.4-mini"),
    )

    with pytest.raises(ChapterCycleResilienceError, match="locked fallback model"):
        execute_resilient_stage(
            spec=_spec(tmp_path, fallback=fallback),
            invoke=lambda _plan, _attempt_dir: StageResponse(parsed_payload={}),
            validate=_accepted,
        )


def test_disabled_b0_fallback_rejects_configured_endpoints(
    tmp_path: Path,
) -> None:
    from pipeline.literary.chapter_cycle_resilience_v1 import ResiliencePolicy

    fallback = (
        _endpoint("openai-row1", provider="openai", model="gpt-5.4"),
    )
    with pytest.raises(ChapterCycleResilienceError, match="fallback is disabled"):
        execute_resilient_stage(
            spec=_spec(tmp_path, fallback=fallback),
            invoke=lambda _plan, _attempt_dir: StageResponse(parsed_payload={}),
            validate=_accepted,
            policy=ResiliencePolicy(b0_contract_fallback_enabled=False),
        )
