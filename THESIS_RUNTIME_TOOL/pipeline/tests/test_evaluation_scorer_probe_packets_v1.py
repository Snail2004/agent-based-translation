from __future__ import annotations

from pipeline.eval.scorer_probe_fixtures_v1 import (
    load_default_scorer_probe_fixture_set,
    scorer_probe_fixture_sha256,
)
from pipeline.eval.scorer_probe_packets_v1 import (
    build_sf_bt_probe_semantic_packet_v1,
    build_sf_bt_probe_stage1_packet_v1,
)
from pipeline.eval.scorer_prompts_v3 import (
    render_sf_bt_reverse_prompt_v3,
    render_sf_bt_semantic_prompt_v3,
)
from pipeline.eval.sf_bt_stage_contracts_v1 import build_sf_back_translation_result


NOW = "2026-07-20T10:00:00Z"
COMMIT = "a" * 40


def test_p2_packets_use_existing_prompt_contracts_without_oracle_metadata() -> None:
    fixture = load_default_scorer_probe_fixture_set()
    fixture_sha256 = scorer_probe_fixture_sha256(fixture)
    case = next(
        row
        for row in fixture["sf_bt_context_ablation"]
        if row["stratum"] == "P2_omission_control"
    )
    packet = build_sf_bt_probe_stage1_packet_v1(
        case,
        fixture_sha256=fixture_sha256,
        created_at=NOW,
        producer_code_commit=COMMIT,
    )
    no_context = render_sf_bt_reverse_prompt_v3(
        packet, context_profile="no_context"
    )
    bounded = render_sf_bt_reverse_prompt_v3(
        packet, context_profile="bounded_neighbors"
    )
    assert case["target_active_vi"] in no_context.rendered_prompt
    assert case["target_active_vi"] in bounded.rendered_prompt
    assert case["author_note"] not in no_context.rendered_prompt
    assert case["author_note"] not in bounded.rendered_prompt
    assert case["planted_marker"] not in no_context.rendered_prompt
    assert case["planted_marker"] not in bounded.rendered_prompt

    raw = '{"back_translation":"The active Vietnamese sentence."}'
    result = build_sf_back_translation_result(
        packet,
        attempt_id="attempt-usage-fixture",
        attempt_index=1,
        created_at=NOW,
        producer_code_commit=COMMIT,
        context_profile="bounded_neighbors",
        rendered_prompt_sha256=bounded.rendered_prompt_sha256,
        model_profile={
            "provider_id": "fixture-provider",
            "model_id": "fixture-model",
            "model_version": "fixture-model",
            "model_family": "fixture-model",
            "profile_sha256": "1" * 64,
        },
        completion_status="complete",
        finish_reason="stop",
        raw_response_text=raw,
    )
    semantic_packet = build_sf_bt_probe_semantic_packet_v1(
        case,
        packet,
        result,
        context_profile="bounded_neighbors",
        created_at=NOW,
        producer_code_commit=COMMIT,
    )
    semantic_prompt = render_sf_bt_semantic_prompt_v3(semantic_packet).rendered_prompt
    assert case["source_active_en"] in semantic_prompt
    assert "The active Vietnamese sentence." in semantic_prompt
    assert case["author_note"] not in semantic_prompt
    assert "planted_marker" not in semantic_prompt


def test_context_profiles_keep_same_semantic_presentation_order() -> None:
    fixture = load_default_scorer_probe_fixture_set()
    fixture_sha256 = scorer_probe_fixture_sha256(fixture)
    case = next(
        row
        for row in fixture["sf_bt_context_ablation"]
        if row["stratum"] == "P2_omission_control"
    )
    packet = build_sf_bt_probe_stage1_packet_v1(
        case,
        fixture_sha256=fixture_sha256,
        created_at=NOW,
        producer_code_commit=COMMIT,
    )
    source_slots = []
    for profile in ("no_context", "bounded_neighbors"):
        prompt = render_sf_bt_reverse_prompt_v3(packet, context_profile=profile)
        result = build_sf_back_translation_result(
            packet,
            attempt_id=f"attempt-{profile}",
            attempt_index=1,
            created_at=NOW,
            producer_code_commit=COMMIT,
            context_profile=profile,
            rendered_prompt_sha256=prompt.rendered_prompt_sha256,
            model_profile={
                "provider_id": "fixture-provider",
                "model_id": "fixture-model",
                "model_version": "fixture-model",
                "model_family": "fixture-model",
                "profile_sha256": "1" * 64,
            },
            completion_status="complete",
            finish_reason="stop",
            raw_response_text='{"back_translation":"Same back translation."}',
        )
        semantic = build_sf_bt_probe_semantic_packet_v1(
            case,
            packet,
            result,
            context_profile=profile,
            created_at=NOW,
            producer_code_commit=COMMIT,
        )
        source_slots.append(semantic["binding"]["source_slot_id"])
    assert source_slots[0] == source_slots[1]
