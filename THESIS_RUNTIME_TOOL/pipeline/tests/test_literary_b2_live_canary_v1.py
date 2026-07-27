from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path

import pytest

from pipeline.agents.provider_profile import load_provider_profile
from pipeline.literary.b2_context_v1 import (
    build_b2_windows_v1,
    load_b2_phase_a_profile,
    render_b2_frame_request_v1,
    render_b2_interaction_request_v1,
)
from pipeline.literary.b2_contract_v1 import normalize_b2_frame_response_v1
from pipeline.literary.b2_prompts_v1 import b2_frame_response_schema
from pipeline.literary.b2_prompts_v2 import b2_interaction_response_schema_v2
from pipeline.literary.b2_live_canary_v1 import (
    B2LiveCanaryError,
    INTERACTION_SEAL_SCHEMA_VERSION,
    INTERACTION_SEAL_SCHEMA_VERSION_V2,
    RUN_SEAL_SCHEMA_VERSION_V4,
    _finalize_live_report,
    _interaction_stage_report,
    _resolve_canary_roles,
    _request_total_reserve,
    _verify_b2_interaction_sealed_contract,
    authorize_b2_request_for_live_v1,
    build_frame_context_for_window_v1,
    build_prior_frame_candidate_context_v1,
    load_b2_canary_profile_v1,
)
from pipeline.literary.checkpoint import canonical_hash
from pipeline.literary.structured_output_policy_v1 import (
    load_literary_structured_output_policy,
    resolve_structured_output_contract,
)


RUNTIME_ROOT = Path(__file__).resolve().parents[2]
B2_PROFILE = (
    RUNTIME_ROOT / "pipeline" / "configs" / "literary_b2_phase_a_profile_v1.json"
)
CANARY_PROFILE = (
    RUNTIME_ROOT
    / "pipeline"
    / "configs"
    / "literary_b2_ch1_canary_profile_v1.json"
)
STRUCTURED_CANARY_PROFILE = (
    RUNTIME_ROOT
    / "pipeline"
    / "configs"
    / "literary_b2_ch1_canary_profile_v2.json"
)
STRUCTURED_POLICY = (
    RUNTIME_ROOT
    / "pipeline"
    / "configs"
    / "literary_structured_output_policy_v1.json"
)
REFERENCE_CANARY_PROFILE = (
    RUNTIME_ROOT
    / "pipeline"
    / "configs"
    / "literary_b2_ch1_reference_canary_profile_v1.json"
)
PREMIUM_CANARY_PROFILE = (
    RUNTIME_ROOT
    / "pipeline"
    / "configs"
    / "literary_b2_ch1_local_gateway_premium_profile_v1.json"
)
PREMIUM_CH2_CANARY_PROFILE = (
    RUNTIME_ROOT
    / "pipeline"
    / "configs"
    / "literary_b2_ch2_local_gateway_premium_profile_v2.json"
)
OPENAI_GPT54_CH1_CANARY_PROFILE = (
    RUNTIME_ROOT
    / "pipeline"
    / "configs"
    / "literary_b2_ch1_openai_gpt54_samehead_v1.json"
)
OPENAI_GPT54_CH2_CANARY_PROFILE = (
    RUNTIME_ROOT
    / "pipeline"
    / "configs"
    / "literary_b2_ch2_openai_gpt54_samehead_v1.json"
)
OPENAI_SHARED_SLIM_CH2_CANARY_PROFILE = (
    RUNTIME_ROOT
    / "pipeline"
    / "configs"
    / "literary_b2_ch2_openai_shared_slim_canary_v1.json"
)
PREMIUM_POLICY = (
    RUNTIME_ROOT
    / "pipeline"
    / "configs"
    / "literary_structured_output_policy_local_gateway_premium_v1.json"
)
PREMIUM_PROVIDER_PROFILE = (
    RUNTIME_ROOT
    / "pipeline"
    / "configs"
    / "literary_provider_profile_local_gateway_premium_v1.json"
)


def _chapter() -> dict:
    return {
        "chapter_id": "book_ch01",
        "blocks": [
            {
                "block_id": "book_ch01_h001",
                "order_index": 0,
                "block_type": "heading",
                "clean_text": "Chapter One",
            },
            {
                "block_id": "book_ch01_b001",
                "order_index": 1,
                "block_type": "paragraph",
                "clean_text": "Vale greeted Robin at North House.",
            },
            {
                "block_id": "book_ch01_b002",
                "order_index": 2,
                "block_type": "dialogue",
                "clean_text": "\"Robin, come here,\" said Vale.",
            },
        ],
    }


def _prefix() -> dict:
    return {
        "prefix_bundle_hash": "prefix_" + "a" * 57,
        "b0_context_cards": [
            {
                "prior_card_id": "card_vale",
                "canonical_surface": "Vale",
                "stable_surfaces": ["Vale"],
                "authority_scope": "chapter_confirmed",
                "effective_claims": {
                    "referent_kind": "person",
                    "identity_summary": "A named resident.",
                },
                "disputed_claims": [],
                "first_supported_block_id": "book_ch01_b001",
                "provenance_refs": [
                    {
                        "chapter_id": "book_ch01",
                        "block_id": "book_ch01_b001",
                    }
                ],
            }
        ],
        "candidate_only_context_cards": [],
        "prefix_identity_uncertainties": [],
    }


def _frame_response() -> dict:
    return {
        "schema_version": "literary_b2_frame_response_v1",
        "chapter_id": "book_ch01",
        "chapter_orientation": {
            "chapter_gist": "A resident greets a visitor.",
            "narrative_mode": "third_person_external",
            "setting_surfaces": ["North House"],
        },
        "frame_starts": [
            {
                "start_block_id": "book_ch01_b001",
                "narrator_surface": None,
                "narrator_status": "external_or_authorial",
                "candidate_card_ids": [],
                "story_time_label": "frame_present",
                "boundary_reason": "The chapter starts in an external frame.",
            }
        ],
        "review_requests": [],
    }


def test_canary_profile_seals_one_frame_two_interactions_and_no_fallback() -> None:
    profile = load_b2_canary_profile_v1(CANARY_PROFILE)
    assert profile.chapter_id == "wh_ch01"
    assert profile.frame_calls == 1
    assert profile.interaction_calls == 2
    assert profile.exception_calls == 0
    assert profile.max_total_calls == 3
    assert profile.max_retries_per_call == 0
    assert profile.safety["provider_fallback_allowed"] is False
    assert profile.safety["production_publish_enabled"] is False


def test_reference_canary_keeps_same_caps_under_a_distinct_profile() -> None:
    profile = load_b2_canary_profile_v1(REFERENCE_CANARY_PROFILE)
    assert profile.profile_id == "literary_b2_ch1_reference_canary_v1"
    assert profile.frame_calls == 1
    assert profile.interaction_calls == 2
    assert profile.exception_calls == 0
    assert profile.max_total_calls == 3
    assert profile.safety["provider_fallback_allowed"] is False


def test_native_structured_canary_seals_policy_and_no_provider_fallback() -> None:
    profile = load_b2_canary_profile_v1(STRUCTURED_CANARY_PROFILE)

    assert profile.structured_output_policy_path is not None
    assert profile.provider_profile_path.name == "literary_provider_profile_v4.json"
    assert profile.max_retries_per_call == 0
    assert profile.safety["provider_fallback_allowed"] is False


def test_openai_gpt54_samehead_profiles_are_nonpublishing_canaries() -> None:
    ch1 = load_b2_canary_profile_v1(OPENAI_GPT54_CH1_CANARY_PROFILE)
    ch2 = load_b2_canary_profile_v1(OPENAI_GPT54_CH2_CANARY_PROFILE)

    assert ch1.chapter_id == "wh_ch01"
    assert ch2.chapter_id == "wh_ch02"
    assert ch1.safety["source_run_may_be_historical"] is True
    assert ch2.safety["source_run_may_be_historical"] is True
    assert ch1.safety["certification_claim_allowed"] is False
    assert ch2.safety["certification_claim_allowed"] is False
    assert ch1.safety["production_publish_enabled"] is False
    assert ch2.safety["production_publish_enabled"] is False
    assert ch1.max_retries_per_call == 0
    assert ch2.max_retries_per_call == 0


def test_b2_native_schema_overhead_is_included_in_hard_reserve() -> None:
    schema = {
        "type": "object",
        "properties": {
            "rows": {
                "type": "array",
                "items": {
                    "type": "object",
                    "description": "x" * 3500,
                },
            }
        },
    }
    request = {
        "messages": [{"role": "user", "content": "Inspect this chapter."}],
        "response_schema": schema,
        "token_reserve": {
            "prompt_token_reserve": 256,
            "output_token_cap": 512,
            "conservative_total_token_reserve": 768,
        },
    }
    policy = load_literary_structured_output_policy(STRUCTURED_POLICY)
    contract = resolve_structured_output_contract(
        policy,
        role_id="literary_b2_frame",
        provider="openai",
        base_url=None,
        model_id="gpt-5.4",
        canonical_schema=schema,
    )

    assert _request_total_reserve(request) == 768
    assert (
        _request_total_reserve(request, structured_output_contract=contract) > 768
    )


def test_interaction_contract_is_verified_per_window_with_legacy_resume() -> None:
    policy = load_literary_structured_output_policy(PREMIUM_POLICY)
    schema_a = b2_interaction_response_schema_v2()
    schema_a["properties"]["chapter_id"] = {
        "type": "string",
        "enum": ["book_ch01"],
    }
    schema_b = deepcopy(schema_a)
    schema_b["properties"]["window_id"] = {
        "type": "string",
        "enum": ["b2w1_book_ch01_02"],
    }
    contract_a = resolve_structured_output_contract(
        policy,
        role_id="literary_b2_interaction",
        provider="openai",
        base_url="http://localhost:8317/v1",
        model_id="gpt-5.4",
        canonical_schema=schema_a,
    )
    contract_b = resolve_structured_output_contract(
        policy,
        role_id="literary_b2_interaction",
        provider="openai",
        base_url="http://localhost:8317/v1",
        model_id="gpt-5.4",
        canonical_schema=schema_b,
    )
    assert contract_a.canonical_schema_hash != contract_b.canonical_schema_hash

    legacy_seal = {
        "structured_output_contracts": {
            "frame": None,
            "interaction": contract_a.to_payload(),
        }
    }
    _verify_b2_interaction_sealed_contract(
        seal=legacy_seal,
        interaction_seal={"schema_version": INTERACTION_SEAL_SCHEMA_VERSION},
        request_row={},
        contract=contract_a,
    )

    per_window_seal = {"schema_version": INTERACTION_SEAL_SCHEMA_VERSION_V2}
    request_row = {"structured_output_contract": contract_b.to_payload()}
    _verify_b2_interaction_sealed_contract(
        seal={"structured_output_contracts": {"interaction": {}}},
        interaction_seal=per_window_seal,
        request_row=request_row,
        contract=contract_b,
    )
    with pytest.raises(B2LiveCanaryError, match="window seal"):
        _verify_b2_interaction_sealed_contract(
            seal={"structured_output_contracts": {"interaction": {}}},
            interaction_seal=per_window_seal,
            request_row=request_row,
            contract=contract_a,
        )


def test_live_authorization_changes_only_api_metadata_and_fingerprint() -> None:
    profile = load_b2_phase_a_profile(B2_PROFILE)
    request = render_b2_frame_request_v1(
        chapter=_chapter(), prefix_bundle=_prefix(), profile=profile
    )
    authorized = authorize_b2_request_for_live_v1(request)
    assert authorized["api_eligible"] is True
    assert authorized["api_ineligible_reasons"] == []
    assert authorized["messages"] == request["messages"]
    assert authorized["response_schema"] == request["response_schema"]
    assert authorized["prompt_sha256"] == request["prompt_sha256"]
    assert authorized["request_fingerprint"] != request["request_fingerprint"]
    body = deepcopy(authorized)
    observed = body.pop("request_fingerprint")
    assert canonical_hash(body) == observed


def test_live_authorization_rejects_missing_frame_dependency() -> None:
    profile = load_b2_phase_a_profile(B2_PROFILE)
    window = build_b2_windows_v1(_chapter(), profile=profile)[0]
    request = render_b2_interaction_request_v1(
        window=window,
        prefix_bundle=_prefix(),
        profile=profile,
        frame_context=None,
    )
    with pytest.raises(B2LiveCanaryError, match="dependency"):
        authorize_b2_request_for_live_v1(request)


def test_actual_frame_context_is_bounded_to_window_and_renders_ready_request() -> None:
    profile = load_b2_phase_a_profile(B2_PROFILE)
    frame_request = render_b2_frame_request_v1(
        chapter=_chapter(), prefix_bundle=_prefix(), profile=profile
    )
    frame = normalize_b2_frame_response_v1(
        request=frame_request, response=_frame_response()
    )
    window = build_b2_windows_v1(_chapter(), profile=profile)[0]
    context = build_frame_context_for_window_v1(
        frame_artifact=frame, window=window
    )
    assert context["frame_artifact_hash"] == frame["artifact_hash"]
    assert context["applicable_segments"][0]["applicable_block_ids"] == [
        "book_ch01_b001",
        "book_ch01_b002",
    ]
    request = render_b2_interaction_request_v1(
        window=window,
        prefix_bundle=_prefix(),
        profile=profile,
        frame_context=context,
    )
    authorized = authorize_b2_request_for_live_v1(request)
    payload = json.loads(authorized["messages"][1]["content"])
    assert payload["frame_context_status"] == "ready"
    assert (
        payload["frame_context"]["frame_context_hash"]
        == context["frame_context_hash"]
    )


def test_interaction_stage_report_uses_persisted_raw_usage() -> None:
    artifact = {
        "window_id": "b2w1_book_ch01_01",
        "artifact_hash": "a" * 64,
        "speaker_turns": [{}, {}],
        "interaction_events": [{}],
        "review_requests": [],
    }
    raw = {
        "usage": {
            "prompt_tokens": 100,
            "completion_tokens": 25,
            "cached_tokens": 0,
            "reasoning_tokens": 0,
        },
        "quota_bucket_id": "openai-row1",
        "completed_at": "2026-07-17T00:00:00+00:00",
    }
    report = _interaction_stage_report(
        artifact=artifact, raw=raw, status="accepted"
    )
    assert report["usage"] == raw["usage"]
    assert report["speaker_turn_count"] == 2
    assert report["interaction_event_count"] == 1
    assert report["quota_bucket_id"] == "openai-row1"


def test_live_report_status_is_not_hard_coded_to_chapter_one(
    tmp_path: Path,
) -> None:
    seal = {
        "seal_hash": "s" * 64,
        "chapter_id": "book_ch02",
        "limits": {"hard_visible_token_cap": 100, "max_total_calls": 0},
        "certification_blockers": ["exploratory_b2_chapter_canary"],
    }
    artifact = {
        "schema_version": "literary_b2_slim_chapter_artifact_v1",
        "artifact_hash": "a" * 64,
        "frame_segments": [],
        "speaker_turns": [],
        "salient_events": [],
        "review_requests": [],
    }

    report = _finalize_live_report(
        output=tmp_path,
        seal=seal,
        chapter_artifact=artifact,
    )

    assert report["chapter_id"] == "book_ch02"
    assert report["status"] == "complete_exploratory_chapter_canary"
    assert report["certification_blockers"] == [
        "exploratory_b2_chapter_canary"
    ]


def test_premium_canary_selects_v2_interaction_without_replacing_frame_v1() -> None:
    profile = load_b2_canary_profile_v1(PREMIUM_CANARY_PROFILE)
    assert profile.frame_contract_version == "v1"
    assert profile.interaction_contract_version == "v2"
    assert profile.frame_calls == 1
    assert profile.interaction_calls == 2
    provider = load_provider_profile(PREMIUM_PROVIDER_PROFILE)
    frame, interaction = _resolve_canary_roles(provider, profile)
    assert frame.model_id == "gpt-5.5"
    assert interaction.model_id == "gpt-5.4"


def test_premium_ch2_canary_seals_exact_window_count_and_stops_at_ch2() -> None:
    profile = load_b2_canary_profile_v1(PREMIUM_CH2_CANARY_PROFILE)

    assert profile.chapter_id == "wh_ch02"
    assert profile.frame_contract_version == "v1"
    assert profile.interaction_contract_version == "v2"
    assert profile.frame_calls == 1
    assert profile.interaction_calls == 4
    assert profile.max_total_calls == 5
    assert profile.max_retries_per_call == 0
    assert profile.prior_frame_candidate_carry_required is True
    assert profile.safety["stop_after_chapter_id"] == "wh_ch02"
    assert profile.safety["provider_fallback_allowed"] is False
    assert profile.safety["production_publish_enabled"] is False


def test_openai_shared_slim_ch2_canary_seals_v3_and_prior_frame_carry() -> None:
    profile = load_b2_canary_profile_v1(OPENAI_SHARED_SLIM_CH2_CANARY_PROFILE)

    assert profile.chapter_id == "wh_ch02"
    assert profile.frame_contract_version == "v2"
    assert profile.interaction_contract_version == "v3"
    assert profile.frame_calls == 1
    assert profile.interaction_calls == 4
    assert profile.max_total_calls == 5
    assert profile.max_retries_per_call == 0
    assert profile.prior_frame_candidate_carry_required is True
    assert profile.safety["stop_after_chapter_id"] == "wh_ch02"
    assert profile.safety["provider_fallback_allowed"] is False
    assert profile.safety["production_publish_enabled"] is False


@pytest.mark.parametrize(
    "seal_schema_version",
    ["literary_b2_ch1_canary_seal_v1", RUN_SEAL_SCHEMA_VERSION_V4],
)
def test_prior_frame_context_requires_adjacent_source_and_current_card(
    tmp_path: Path, seal_schema_version: str
) -> None:
    prior_root = tmp_path / "prior"
    frame_body = {
        "schema_version": "literary_b2_frame_artifact_v1",
        "chapter_id": "book_ch01",
        "frame_segments": [
            {
                "frame_segment_id": "frame_1",
                "narrator_status": "resolved_candidate",
                "candidate_card_ids": ["card_vale"],
                "normalization_status": "accepted",
            }
        ],
    }
    frame = {**frame_body, "artifact_hash": canonical_hash(frame_body)}
    seal_body = {
        "schema_version": seal_schema_version,
        "chapter_id": "book_ch01",
        "source_document_sha256": "d" * 64,
    }
    seal = {**seal_body, "seal_hash": canonical_hash(seal_body)}
    (prior_root / "frame").mkdir(parents=True)
    (prior_root / "run_seal.json").write_text(
        json.dumps(seal), encoding="utf-8"
    )
    (prior_root / "frame" / "frame_artifact.json").write_text(
        json.dumps(frame), encoding="utf-8"
    )

    context = build_prior_frame_candidate_context_v1(
        prior_b2_root=prior_root,
        current_source_document_sha256="d" * 64,
        chapter_ids=["book_ch01", "book_ch02"],
        target_chapter_id="book_ch02",
        current_prefix_bundle=_prefix(),
    )
    assert context["source_chapter_id"] == "book_ch01"
    assert context["target_chapter_id"] == "book_ch02"
    assert context["candidate_sources"][0]["candidate_card_id"] == "card_vale"

    with pytest.raises(B2LiveCanaryError, match="not the preceding chapter"):
        build_prior_frame_candidate_context_v1(
            prior_b2_root=prior_root,
            current_source_document_sha256="d" * 64,
            chapter_ids=["book_ch01", "book_ch00", "book_ch02"],
            target_chapter_id="book_ch02",
            current_prefix_bundle=_prefix(),
        )


def test_premium_provider_profile_has_one_gateway_bucket_and_no_fallback() -> None:
    profile = load_provider_profile(PREMIUM_PROVIDER_PROFILE)
    assert set(profile.credentials) == {"local-gpt-gateway-v1"}
    assert profile.credentials["local-gpt-gateway-v1"].base_url == (
        "http://localhost:8317/v1"
    )
    assert profile.roles["literary_b0"].model_id == "gpt-5.4"
    assert profile.roles["literary_b2_interaction"].model_id == "gpt-5.4"
    assert profile.roles["literary_b2_frame"].model_id == "gpt-5.5"
    assert profile.roles["literary_local_conflict_auditor"].model_id == "gpt-5.5"
    assert all(
        role.bucket_order == ("local-gpt-gateway-v1",)
        for role in profile.roles.values()
    )


def test_premium_policy_uses_verified_exact_gateway_probe_evidence() -> None:
    policy = load_literary_structured_output_policy(PREMIUM_POLICY)
    interaction = resolve_structured_output_contract(
        policy,
        role_id="literary_b2_interaction",
        provider="openai",
        base_url="http://localhost:8317/v1",
        model_id="gpt-5.4",
        canonical_schema=b2_interaction_response_schema_v2(),
    )
    frame = resolve_structured_output_contract(
        policy,
        role_id="literary_b2_frame",
        provider="openai",
        base_url="http://localhost:8317/v1",
        model_id="gpt-5.5",
        canonical_schema=b2_frame_response_schema(),
    )
    assert interaction.native_enforcement is True
    assert frame.native_enforcement is True
    assert interaction.evidence_id and "e7a62aa08ad96d5e" in interaction.evidence_id
    assert frame.evidence_id and "e7a62aa08ad96d5e" in frame.evidence_id
