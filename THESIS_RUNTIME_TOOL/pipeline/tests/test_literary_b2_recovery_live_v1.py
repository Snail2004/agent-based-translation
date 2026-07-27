from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from pipeline.agents.provider_profile import load_provider_profile
from pipeline.scripts.run_literary_b2_recovery_live_v1 import (
    B2RecoveryLiveError,
    _event_review_contract_functions,
    _load_reusable_stage_response,
    _load_profile,
    _tree_hash,
    _visible_tokens,
)


RUNTIME_ROOT = Path(__file__).resolve().parents[2]
PROFILE_PATH = (
    RUNTIME_ROOT
    / "pipeline"
    / "configs"
    / "literary_b2_recovery_local_gateway_canary_v1.json"
)
PROFILE_V2_PATH = (
    RUNTIME_ROOT
    / "pipeline"
    / "configs"
    / "literary_b2_recovery_local_gateway_canary_v2.json"
)
PROFILE_CH2_V2_PATH = (
    RUNTIME_ROOT
    / "pipeline"
    / "configs"
    / "literary_b2_recovery_local_gateway_ch2_v2.json"
)
PROFILE_CH2_V3_PATH = (
    RUNTIME_ROOT
    / "pipeline"
    / "configs"
    / "literary_b2_recovery_local_gateway_ch2_v3.json"
)
LOCAL_GPT54_CH2_PROFILE = (
    RUNTIME_ROOT
    / "pipeline"
    / "configs"
    / "literary_b2_recovery_local_gateway_gpt54_ch2_prompt_v21_v1.json"
)
OPENAI_GPT54_CH1_PROFILE = (
    RUNTIME_ROOT
    / "pipeline"
    / "configs"
    / "literary_b2_recovery_ch1_openai_gpt54_samehead_v1.json"
)
OPENAI_GPT54_CH2_PROFILE = (
    RUNTIME_ROOT
    / "pipeline"
    / "configs"
    / "literary_b2_recovery_ch2_openai_gpt54_samehead_v1.json"
)


def test_recovery_live_profile_is_two_call_fail_closed() -> None:
    profile = _load_profile(PROFILE_PATH)
    assert profile["limits"] == {
        "registry_recovery_calls": 1,
        "event_review_calls": 1,
        "max_total_calls": 2,
        "max_retries_per_call": 0,
        "hard_visible_token_cap": 50000,
    }
    assert profile["safety"] == {
        "provider_fallback_allowed": False,
        "source_artifact_mutation_allowed": False,
        "book_global_identity_mutation_allowed": False,
        "production_publish_enabled": False,
        "stop_after_chapter_id": "wh_ch01",
    }
    assert {
        row["provider_role_id"]
        for row in profile["stage_bindings"].values()
    } == {"literary_local_conflict_auditor"}


def test_recovery_live_profile_rejects_weakened_safety(tmp_path: Path) -> None:
    profile = json.loads(PROFILE_PATH.read_text(encoding="utf-8"))
    weakened = deepcopy(profile)
    weakened["safety"]["provider_fallback_allowed"] = True
    path = tmp_path / "weakened.json"
    path.write_text(json.dumps(weakened), encoding="utf-8")
    with pytest.raises(B2RecoveryLiveError, match="safety policy"):
        _load_profile(path)


def test_recovery_live_profile_can_select_event_authority_v2() -> None:
    profile_v1 = _load_profile(PROFILE_PATH)
    assert _event_review_contract_functions(profile_v1)[0] == "v1"
    profile_v2 = _load_profile(PROFILE_V2_PATH)
    assert profile_v2["schema_version"] == "literary_b2_recovery_live_profile_v2"
    assert profile_v2["stage_bindings"]["event_review"]["schema_name"] == (
        "literary_b2_event_review_v2"
    )
    version, render, validate, build = _event_review_contract_functions(profile_v2)
    assert version == "v2"
    assert render.__name__ == "render_event_review_request_v2"
    assert validate.__name__ == "validate_event_review_response_v2"
    assert build.__name__ == "build_event_revision_ledger_v2"


def test_ch2_recovery_profile_keeps_bounded_v2_contract() -> None:
    profile = _load_profile(PROFILE_CH2_V2_PATH)

    assert profile["schema_version"] == "literary_b2_recovery_live_profile_v2"
    assert profile["safety"]["event_review_contract_version"] == "v2"
    assert profile["safety"]["stop_after_chapter_id"] == "wh_ch02"
    assert profile["limits"]["max_total_calls"] == 2
    assert profile["limits"]["max_retries_per_call"] == 0


def test_ch2_multi_component_profile_seals_measured_call_caps() -> None:
    profile = _load_profile(PROFILE_CH2_V3_PATH)

    assert profile["schema_version"] == "literary_b2_recovery_live_profile_v3"
    assert profile["safety"]["event_review_contract_version"] == "v2"
    assert profile["limits"] == {
        "registry_recovery_calls": 2,
        "event_review_calls": 3,
        "max_total_calls": 5,
        "max_retries_per_call": 0,
        "hard_visible_token_cap": 120000,
    }


def test_local_gpt54_ch2_profile_matches_current_measured_components() -> None:
    profile = _load_profile(LOCAL_GPT54_CH2_PROFILE)

    assert profile["limits"] == {
        "registry_recovery_calls": 3,
        "event_review_calls": 2,
        "max_total_calls": 5,
        "max_retries_per_call": 0,
        "hard_visible_token_cap": 120000,
    }
    assert profile["safety"]["stop_after_chapter_id"] == "wh_ch02"
    assert profile["safety"]["event_review_contract_version"] == "v2"
    provider = load_provider_profile(
        LOCAL_GPT54_CH2_PROFILE.parent / profile["provider_profile"]
    )
    role = provider.roles["literary_local_conflict_auditor"]
    assert role.model_id == "gpt-5.4"
    assert role.bucket_order == ("local-gpt-gateway-v1",)


def test_openai_gpt54_ch1_recovery_profile_is_two_call_fail_closed() -> None:
    profile = _load_profile(OPENAI_GPT54_CH1_PROFILE)

    assert profile["profile_id"] == (
        "literary_b2_recovery_ch1_openai_gpt54_samehead_v1"
    )
    assert profile["provider_profile"] == (
        "literary_provider_profile_openai_gpt54_samehead_v1.json"
    )
    assert profile["limits"]["registry_recovery_calls"] == 1
    assert profile["limits"]["event_review_calls"] == 1
    assert profile["limits"]["max_retries_per_call"] == 0
    assert profile["safety"]["event_review_contract_version"] == "v2"
    assert profile["safety"]["provider_fallback_allowed"] is False
    assert profile["safety"]["production_publish_enabled"] is False

    provider = load_provider_profile(
        OPENAI_GPT54_CH1_PROFILE.parent / profile["provider_profile"]
    )
    bound_roles = {
        binding["provider_role_id"]
        for binding in profile["stage_bindings"].values()
    }
    assert bound_roles == {"literary_local_conflict_auditor"}
    role = provider.roles["literary_local_conflict_auditor"]
    assert role.provider == "openai"
    assert role.bucket_order == ("openai-row1",)


def test_openai_gpt54_ch2_recovery_profile_matches_measured_components() -> None:
    profile = _load_profile(OPENAI_GPT54_CH2_PROFILE)

    assert profile["profile_id"] == (
        "literary_b2_recovery_ch2_openai_gpt54_samehead_v1"
    )
    assert profile["limits"] == {
        "registry_recovery_calls": 4,
        "event_review_calls": 3,
        "max_total_calls": 7,
        "max_retries_per_call": 0,
        "hard_visible_token_cap": 160000,
    }
    assert profile["safety"]["stop_after_chapter_id"] == "wh_ch02"
    assert profile["safety"]["event_review_contract_version"] == "v2"
    assert profile["safety"]["provider_fallback_allowed"] is False
    assert profile["safety"]["production_publish_enabled"] is False


def test_multi_component_profile_rejects_unbounded_or_mismatched_counts(
    tmp_path: Path,
) -> None:
    profile = json.loads(PROFILE_CH2_V3_PATH.read_text(encoding="utf-8"))
    profile["limits"]["event_review_calls"] = 7
    profile["limits"]["max_total_calls"] = 9
    path = tmp_path / "too_many.json"
    path.write_text(json.dumps(profile), encoding="utf-8")
    with pytest.raises(B2RecoveryLiveError, match="call caps"):
        _load_profile(path)


def test_recovery_profile_call_counts_are_caps_not_required_empty_work() -> None:
    profile = _load_profile(PROFILE_V2_PATH)

    assert profile["limits"]["registry_recovery_calls"] == 1
    assert profile["limits"]["event_review_calls"] == 1
    # A stage with no renderable component is skipped rather than paid for.
    assert profile["limits"]["max_total_calls"] == 2


def test_recovery_live_profile_rejects_schema_contract_mismatch(
    tmp_path: Path,
) -> None:
    profile = json.loads(PROFILE_PATH.read_text(encoding="utf-8"))
    mismatched = deepcopy(profile)
    mismatched["safety"]["event_review_contract_version"] = "v2"
    path = tmp_path / "mismatched.json"
    path.write_text(json.dumps(mismatched), encoding="utf-8")
    with pytest.raises(B2RecoveryLiveError, match="schema and event contract"):
        _load_profile(path)


def test_visible_usage_does_not_double_count_reasoning_and_tree_hash_is_content_based(
    tmp_path: Path,
) -> None:
    assert _visible_tokens(
        {
            "prompt_tokens": 10,
            "completion_tokens": 4,
            "reasoning_tokens": 3,
            "cached_tokens": 8,
        }
    ) == 14
    (tmp_path / "a.txt").write_text("alpha", encoding="utf-8")
    before = _tree_hash(tmp_path)
    (tmp_path / "a.txt").write_text("beta", encoding="utf-8")
    assert _tree_hash(tmp_path) != before


def test_reusable_stage_requires_exact_identity_and_records_prior_usage(
    tmp_path: Path,
) -> None:
    resume_root = tmp_path / "prior"
    stage = resume_root / "event_review" / "01_component_a"
    stage.mkdir(parents=True)
    (resume_root / "run_seal.json").write_text(
        json.dumps({"seal_hash": "s" * 64}), encoding="utf-8"
    )
    request = SimpleNamespace(
        request_kind="event_semantic_review",
        component_id="component_a",
        request_fingerprint="f" * 64,
        response_schema={
            "type": "object",
            "additionalProperties": False,
            "required": ["ok"],
            "properties": {"ok": {"type": "boolean"}},
        },
    )
    (stage / "request.json").write_text(
        json.dumps({"request_fingerprint": request.request_fingerprint}),
        encoding="utf-8",
    )
    raw = {
        "request_fingerprint": request.request_fingerprint,
        "request_kind": request.request_kind,
        "component_id": request.component_id,
        "model": "gpt-test",
        "quota_bucket_id": "bucket-test",
        "credential_commitment": "c" * 64,
        "structured_output_contract": {"canonical_schema_hash": "h" * 64},
        "parsed_json": {"ok": True},
        "json_error": None,
        "usage": {
            "prompt_tokens": 4,
            "completion_tokens": 2,
            "reasoning_tokens": 1,
        },
    }
    raw_path = stage / "raw_result.json"
    raw_path.write_text(json.dumps(raw), encoding="utf-8")
    credential = SimpleNamespace(
        quota_bucket_id="bucket-test", commitment="c" * 64
    )
    contract = SimpleNamespace(
        to_payload=lambda: {"canonical_schema_hash": "h" * 64}
    )

    reused = _load_reusable_stage_response(
        resume_root=resume_root,
        collection="event_review",
        ordinal=1,
        request=request,
        multi_component=True,
        model_id="gpt-test",
        credential=credential,
        contract=contract,
    )
    assert reused is not None
    parsed, reference = reused
    assert parsed == {"ok": True}
    assert reference["provider_call_performed"] is False
    assert reference["source_visible_tokens"] == 6

    chained_root = tmp_path / "chained"
    chained_stage = chained_root / "event_review" / "01_component_a"
    chained_stage.mkdir(parents=True)
    (chained_root / "run_seal.json").write_text(
        json.dumps({"seal_hash": "t" * 64}), encoding="utf-8"
    )
    (chained_stage / "request.json").write_text(
        json.dumps({"request_fingerprint": request.request_fingerprint}),
        encoding="utf-8",
    )
    (chained_stage / "reused_result.json").write_text(
        json.dumps(reference), encoding="utf-8"
    )
    chained = _load_reusable_stage_response(
        resume_root=chained_root,
        collection="event_review",
        ordinal=1,
        request=request,
        multi_component=True,
        model_id="gpt-test",
        credential=credential,
        contract=contract,
    )
    assert chained is not None
    chained_parsed, chained_reference = chained
    assert chained_parsed == {"ok": True}
    assert chained_reference["source_run_root"] == str(resume_root.resolve())
    assert chained_reference["source_raw_result"] == str(raw_path.resolve())

    raw["model"] = "foreign-model"
    raw_path.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(B2RecoveryLiveError, match="identity drifted"):
        _load_reusable_stage_response(
            resume_root=resume_root,
            collection="event_review",
            ordinal=1,
            request=request,
            multi_component=True,
            model_id="gpt-test",
            credential=credential,
            contract=contract,
        )
