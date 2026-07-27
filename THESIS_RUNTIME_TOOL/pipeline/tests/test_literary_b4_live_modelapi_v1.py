from __future__ import annotations

import json
from pathlib import Path

import pytest

from pipeline.llm_backend import (
    ContentAddressedArtifactStore,
    MappingCredentialProvider,
    PhysicalQuotaScheduler,
    RawTransportResponse,
    SharedLlmAttemptLedger,
    SharedLlmCapabilityProbe,
    canonical_json,
    credential_commitment,
)
from pipeline.literary.b4_live_modelapi_v1 import (
    B4LiveModelApiError,
    _calibrated_prompt_upper_bounds_v1,
    _estimate_modelapi_prompt_tokens_v3,
    _record_prompt_observation_v1,
    run_address_anchor_live_v1,
    run_translation_window_live_v1,
)
from pipeline.literary.checkpoint import canonical_json as literary_json
from pipeline.literary.modelapi_b4_capability_probe_v1 import (
    ADDRESS_ROLE_ID,
    RUNTIME_PROFILE_PATH,
    RUNTIME_ROLE_IDS,
    TRANSLATOR_ROLE_ID,
    build_probe_plan_v1,
    execute_probe_once_v1,
    synthetic_probe_rendered_v1,
    synthetic_probe_response_v1,
)
from pipeline.literary.shared_llm_adapter_v1 import (
    LiterarySharedLlmAdapterError,
)
from pipeline.literary.shared_runtime_profile_v2 import (
    load_literary_shared_runtime_profile_v2,
)


SECRET = "synthetic-b4-live-secret"


class _Sender:
    def __init__(self, payload: dict, *, cached_tokens: int = 0) -> None:
        self.payload = payload
        self.cached_tokens = cached_tokens
        self.calls = 0

    def send(self, _request):
        self.calls += 1
        body = canonical_json(
            {
                "id": f"synthetic-b4-{self.calls}",
                "model": "gpt-5.4",
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {"content": canonical_json(self.payload)},
                    }
                ],
                "usage": {
                    "prompt_tokens": 900,
                    "completion_tokens": 180,
                    "total_tokens": 1080,
                    "prompt_tokens_details": {
                        "cached_tokens": self.cached_tokens
                    },
                },
            }
        ).encode("utf-8")
        return RawTransportResponse(
            status_code=200,
            headers={},
            body=body,
            request_id=f"synthetic-b4-{self.calls}",
        )


def _binding() -> dict[str, str]:
    return {
        "shared_core_revision": "1" * 40,
        "consumer_revision": "2" * 40,
        "consumer_implementation_sha256": "3" * 64,
    }


def _qualified_evidence(tmp_path: Path, role_id: str) -> dict:
    plan = build_probe_plan_v1(
        role_id=role_id,
        probe_run_id=f"probe_{role_id.replace('.', '_')}",
        credential_commitment_sha256=credential_commitment(SECRET),
        issued_at_utc="2026-07-26T00:00:00Z",
        implementation_binding=_binding(),
    )
    sender = _Sender(synthetic_probe_response_v1(role_id, plan.rendered))
    probe = SharedLlmCapabilityProbe(
        credential_provider=MappingCredentialProvider(
            {plan.source["credential_ref"]: SECRET}
        ),
        scheduler=PhysicalQuotaScheduler(tmp_path / "locks"),
        ledger=SharedLlmAttemptLedger(tmp_path / "ledger.sqlite3"),
        artifact_store=ContentAddressedArtifactStore(tmp_path / "artifacts"),
        sender=sender,
        implementation_binding=plan.implementation_binding,
    )
    result = execute_probe_once_v1(probe=probe, plan=plan)
    assert result["status"] == "qualified"
    assert sender.calls == 1
    return result["capability_evidence"]


def test_b4_runtime_registers_exact_two_fail_closed_roles() -> None:
    runtime = load_literary_shared_runtime_profile_v2(
        RUNTIME_PROFILE_PATH,
        expected_role_ids=RUNTIME_ROLE_IDS,
    )
    assert set(runtime.role_bindings) == {
        ADDRESS_ROLE_ID,
        TRANSLATOR_ROLE_ID,
    }
    assert runtime.role_presets[ADDRESS_ROLE_ID].limits["max_calls"] == 2
    assert runtime.role_presets[TRANSLATOR_ROLE_ID].limits["max_calls"] == 6
    assert all(
        runtime.output_envelope_for(role_id)["mode"] == "json_object"
        for role_id in RUNTIME_ROLE_IDS
    )
    assert all(
        runtime.role_presets[role_id].transport_retry["max_retries"] == 0
        for role_id in RUNTIME_ROLE_IDS
    )


def test_b4_address_probe_and_live_call_persist_usage(tmp_path: Path) -> None:
    evidence = _qualified_evidence(tmp_path / "probe", ADDRESS_ROLE_ID)
    rendered = synthetic_probe_rendered_v1(ADDRESS_ROLE_ID)
    sender = _Sender(
        synthetic_probe_response_v1(ADDRESS_ROLE_ID, rendered),
        cached_tokens=320,
    )
    output = tmp_path / "live"
    report = run_address_anchor_live_v1(
        anchor_input=rendered.anchor_input,
        style_profile=(
            "Use restrained Vietnamese for this test.\n"
            "- Prompt version: literary_style_profile_probe_v1."
        ),
        style_profile_version="literary_style_profile_probe_v1",
        measured_arm=False,
        capability_evidence=evidence,
        output_root=output,
        shared_root=tmp_path / "shared",
        scheduler_root=tmp_path / "locks-live",
        secret=SECRET,
        credential_commitment_sha256=credential_commitment(SECRET),
        run_id="b4_address_live_test",
        attempt_run_id="b4_address_live_test_a1",
        current_git_head="4" * 40,
        sender=sender,
    )
    assert sender.calls == 1
    assert report["usage"]["cached_input_tokens"] == 320
    assert report["reference_based_scoring_allowed"] is False
    assert (output / "shared_attempt_receipt.json").is_file()
    artifact = json.loads((output / "address_anchor.json").read_text("utf-8"))
    assert artifact["provider_called"] is True


def test_rejected_address_persists_calibration_before_semantic_validation(
    tmp_path: Path,
) -> None:
    evidence = _qualified_evidence(tmp_path / "probe", ADDRESS_ROLE_ID)
    rendered = synthetic_probe_rendered_v1(ADDRESS_ROLE_ID)
    response = synthetic_probe_response_v1(ADDRESS_ROLE_ID, rendered)
    response["pair_decisions"][0]["evidence_refs"] = ["foreign_block"]
    sender = _Sender(response)
    output = tmp_path / "live"
    shared_root = tmp_path / "shared"

    with pytest.raises(
        LiterarySharedLlmAdapterError,
        match="cites a foreign source block",
    ):
        run_address_anchor_live_v1(
            anchor_input=rendered.anchor_input,
            style_profile=(
                "Use restrained Vietnamese for this test.\n"
                "- Prompt version: literary_style_profile_probe_v1."
            ),
            style_profile_version="literary_style_profile_probe_v1",
            measured_arm=False,
            capability_evidence=evidence,
            output_root=output,
            shared_root=shared_root,
            scheduler_root=tmp_path / "locks-live",
            secret=SECRET,
            credential_commitment_sha256=credential_commitment(SECRET),
            run_id="b4_address_rejection_calibration_test",
            attempt_run_id="b4_address_rejection_calibration_test_a1",
            current_git_head="4" * 40,
            sender=sender,
        )

    assert sender.calls == 1
    receipt_check = json.loads(
        (output / "transport_preflight_receipt_check.json").read_text("utf-8")
    )
    assert receipt_check["provider_prompt_tokens"] == 900
    assert receipt_check["calibration_observation_persisted"] is True
    calibration = json.loads(
        (shared_root / "transport_prompt_calibration_v1.json").read_text("utf-8")
    )
    samples = calibration["roles"][ADDRESS_ROLE_ID]["samples"]
    assert len(samples) == 1
    assert samples[0]["provider_prompt_tokens"] == 900
    assert (output / "semantic_rejection.json").is_file()


def test_b4_translator_probe_and_live_call_persist_cache_usage(tmp_path: Path) -> None:
    evidence = _qualified_evidence(tmp_path / "probe", TRANSLATOR_ROLE_ID)
    rendered = synthetic_probe_rendered_v1(TRANSLATOR_ROLE_ID)
    sender = _Sender(
        synthetic_probe_response_v1(TRANSLATOR_ROLE_ID, rendered),
        cached_tokens=700,
    )
    report = run_translation_window_live_v1(
        translator_pack_bytes=(
            literary_json(rendered.translator_pack) + "\n"
        ).encode("utf-8"),
        address_anchor_bytes=(literary_json(rendered.address_anchor) + "\n").encode("utf-8"),
        window_slice_bytes=(literary_json(rendered.window_slice) + "\n").encode("utf-8"),
        chapter=rendered.chapter,
        accepted_tail_translations={},
        style_profile=(
            "Use restrained Vietnamese for this test.\n"
            "- Prompt version: literary_style_profile_probe_v1."
        ),
        style_profile_version="literary_style_profile_probe_v1",
        measured_arm=False,
        capability_evidence=evidence,
        output_root=tmp_path / "live",
        shared_root=tmp_path / "shared",
        scheduler_root=tmp_path / "locks-live",
        secret=SECRET,
        credential_commitment_sha256=credential_commitment(SECRET),
        run_id="b4_translator_live_test",
        attempt_run_id="b4_translator_live_test_a1",
        current_git_head="5" * 40,
        sender=sender,
    )
    assert sender.calls == 1
    assert report["usage"]["cached_input_tokens"] == 700
    assert report["translated_block_count"] == 1
    assert report["provider_retries"] == 0
    assert report["transport_preflight"]["fits"] is True
    assert (tmp_path / "live" / "translation_window.json").is_file()


def test_t10_translator_over_cap_halts_before_transport(tmp_path: Path) -> None:
    evidence = _qualified_evidence(tmp_path / "probe", TRANSLATOR_ROLE_ID)
    rendered = synthetic_probe_rendered_v1(TRANSLATOR_ROLE_ID)
    sender = _Sender(
        synthetic_probe_response_v1(TRANSLATOR_ROLE_ID, rendered)
    )
    with pytest.raises(
        B4LiveModelApiError,
        match="exceeds max_input_tokens",
    ):
        run_translation_window_live_v1(
            translator_pack_bytes=(
                literary_json(rendered.translator_pack) + "\n"
            ).encode("utf-8"),
            address_anchor_bytes=(
                literary_json(rendered.address_anchor) + "\n"
            ).encode("utf-8"),
            window_slice_bytes=(
                literary_json(rendered.window_slice) + "\n"
            ).encode("utf-8"),
            chapter=rendered.chapter,
            accepted_tail_translations={},
            style_profile=" ".join(
                f"unique_style_token_{index:06d}" for index in range(20_000)
            ),
            style_profile_version="literary_style_profile_probe_v1",
            measured_arm=False,
            capability_evidence=evidence,
            output_root=tmp_path / "live",
            shared_root=tmp_path / "shared",
            scheduler_root=tmp_path / "locks-live",
            secret=SECRET,
            credential_commitment_sha256=credential_commitment(SECRET),
            run_id="b4_translator_preflight_test",
            attempt_run_id="b4_translator_preflight_test_a1",
            current_git_head="6" * 40,
            sender=sender,
        )
    assert sender.calls == 0
    preflight = json.loads(
        (tmp_path / "live" / "transport_preflight.json").read_text("utf-8")
    )
    assert preflight["fits"] is False
    assert (
        preflight["conservative_prompt_token_upper_bound"]
        >= preflight["estimated_prompt_tokens"]
    )


def test_modelapi_estimator_uses_o200k_base() -> None:
    estimate = _estimate_modelapi_prompt_tokens_v3(
        '{"english":"the house","vietnamese":"ngôi nhà"}'
    )
    assert estimate["tokenizer_name"] == "o200k_base"
    assert estimate["estimated_prompt_tokens"] == estimate[
        "tokenized_transport_tokens"
    ]


def test_t23_persisted_receipt_widens_the_next_preflight(
    tmp_path: Path,
) -> None:
    shared_root = tmp_path / "shared"
    calibration_path = shared_root / "transport_prompt_calibration_v1.json"
    _record_prompt_observation_v1(
        calibration_path=calibration_path,
        role_id=TRANSLATOR_ROLE_ID,
        transport_request_sha256="a" * 64,
        estimated_prompt_tokens=1_000,
        provider_prompt_tokens=1_400,
        attempt_usage_id="observed_overrun",
    )
    evidence = _qualified_evidence(tmp_path / "probe", TRANSLATOR_ROLE_ID)
    rendered = synthetic_probe_rendered_v1(TRANSLATOR_ROLE_ID)
    sender = _Sender(
        synthetic_probe_response_v1(TRANSLATOR_ROLE_ID, rendered)
    )
    report = run_translation_window_live_v1(
        translator_pack_bytes=(
            literary_json(rendered.translator_pack) + "\n"
        ).encode("utf-8"),
        address_anchor_bytes=(
            literary_json(rendered.address_anchor) + "\n"
        ).encode("utf-8"),
        window_slice_bytes=(
            literary_json(rendered.window_slice) + "\n"
        ).encode("utf-8"),
        chapter=rendered.chapter,
        accepted_tail_translations={},
        style_profile=(
            "Use restrained Vietnamese for this test.\n"
            "- Prompt version: literary_style_profile_probe_v1."
        ),
        style_profile_version="literary_style_profile_probe_v1",
        measured_arm=False,
        capability_evidence=evidence,
        output_root=tmp_path / "live",
        shared_root=shared_root,
        scheduler_root=tmp_path / "locks-live",
        secret=SECRET,
        credential_commitment_sha256=credential_commitment(SECRET),
        run_id="b4_translator_calibration_test",
        attempt_run_id="b4_translator_calibration_test_a1",
        current_git_head="7" * 40,
        sender=sender,
    )
    assert report["transport_preflight"]["safety_multiplier"] == 1.45
    assert report["transport_preflight"]["calibration_sample_count"] == 1


def test_smaller_request_keeps_observed_additive_route_overhead() -> None:
    bounds = _calibrated_prompt_upper_bounds_v1(
        estimated_tokens=7_828,
        role_calibration={
            "safety_multiplier": 1.358495,
            "max_observed_additive_error_tokens": 3_606,
            "samples": [],
        },
    )

    assert bounds["multiplicative_upper_bound"] == 10_635
    assert bounds["additive_upper_bound"] == 11_826
    assert bounds["conservative_upper_bound"] == 11_826
    assert bounds["conservative_upper_bound"] >= 11_524


def test_removing_additive_route_overhead_reproduces_w2_overrun() -> None:
    bounds = _calibrated_prompt_upper_bounds_v1(
        estimated_tokens=7_828,
        role_calibration={
            "safety_multiplier": 1.358495,
            "samples": [],
        },
    )

    assert bounds["conservative_upper_bound"] == 10_635
    assert bounds["conservative_upper_bound"] < 11_524
