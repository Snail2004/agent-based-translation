from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path

import pytest

from pipeline.literary.b2_ckey_diagnostic_v1 import (
    B2CkeyDiagnosticError,
    build_probe_request_v1,
    evaluate_probe_response_v1,
    load_b2_ckey_diagnostic_profile_v1,
)
from pipeline.literary import b2_ckey_diagnostic_v1
from pipeline.literary.b2_prompts_v1 import (
    B2_INTERACTION_PROMPT_ID,
    B2_INTERACTION_SYSTEM_PROMPT,
    b2_interaction_response_schema,
)
from pipeline.literary.checkpoint import canonical_hash


RUNTIME_ROOT = Path(__file__).resolve().parents[2]
PROFILE = (
    RUNTIME_ROOT
    / "pipeline"
    / "configs"
    / "literary_b2_ckey_diagnostic_profile_v1.json"
)
TRANHIEU_PROFILE = (
    RUNTIME_ROOT
    / "pipeline"
    / "configs"
    / "literary_b2_ckey_diagnostic_tranhieu_profile_v1.json"
)
TRANHIEU_OPENAI_PROFILE = (
    RUNTIME_ROOT
    / "pipeline"
    / "configs"
    / "literary_b2_ckey_diagnostic_tranhieu_openai_profile_v1.json"
)
GOOGLE_OFFICIAL_SCHEMA_PROFILE = (
    RUNTIME_ROOT
    / "pipeline"
    / "configs"
    / "literary_b2_google_official_schema_probe_profile_v1.json"
)
OPENROUTER_SCHEMA_PROFILE = (
    RUNTIME_ROOT
    / "pipeline"
    / "configs"
    / "literary_b2_openrouter_schema_probe_profile_v1.json"
)
CKEY_SCHEMA_SUBSET_PROFILE = (
    RUNTIME_ROOT
    / "pipeline"
    / "configs"
    / "literary_b2_ckey_schema_subset_probe_profile_v1.json"
)
GOOGLE_OFFICIAL_REMAINING_PROFILES = {
    "gemini-free-row1-v1": (
        "literary_b2_google_official_row1_schema_probe_profile_v1.json",
        "literary_b2_interaction_row1",
        1,
    ),
    "gemini-free-row2-v1": (
        "literary_b2_google_official_row2_schema_probe_profile_v1.json",
        "literary_b2_interaction_row2",
        2,
    ),
    "gemini-free-row3-v1": (
        "literary_b2_google_official_row3_schema_probe_profile_v1.json",
        "literary_b2_interaction_row3",
        3,
    ),
    "gemini-free-row5-v2": (
        "literary_b2_google_official_row5_schema_probe_profile_v1.json",
        "literary_b2_interaction_row5",
        5,
    ),
}


def _full_request() -> dict:
    schema = b2_interaction_response_schema()
    body = {
        "schema_version": "literary_b2_request_v1",
        "request_kind": "window_interaction",
        "prompt_id": B2_INTERACTION_PROMPT_ID,
        "prompt_sha256": canonical_hash(B2_INTERACTION_SYSTEM_PROMPT),
        "chapter_id": "fixture_ch01",
        "window_id": "fixture_w01",
        "messages": [
            {"role": "system", "content": B2_INTERACTION_SYSTEM_PROMPT},
            {"role": "user", "content": "{}"},
        ],
        "response_schema": schema,
        "response_schema_hash": canonical_hash(schema),
        "token_reserve": {},
        "configured_prompt_cap": 18_000,
        "dependency_status": "ready",
        "api_eligible": True,
        "api_ineligible_reasons": [],
        "context_hashes": {},
        "production_publish_performed": False,
    }
    return {**body, "request_fingerprint": canonical_hash(body)}


def test_profile_is_closed_and_four_call_bounded() -> None:
    profile = load_b2_ckey_diagnostic_profile_v1(PROFILE)
    assert profile.max_calls == 4
    assert profile.max_retries_per_call == 0
    assert profile.safety["provider_fallback_allowed"] is False
    assert profile.safety["production_publish_enabled"] is False


def test_google_official_profile_is_one_call_and_one_bucket() -> None:
    profile = load_b2_ckey_diagnostic_profile_v1(
        GOOGLE_OFFICIAL_SCHEMA_PROFILE
    )
    assert profile.max_calls == 1
    assert profile.probe_ids == ("schema_authority_small",)
    assert profile.quota_bucket_id == "gemini-free-row4-v1"
    provider = json.loads(
        profile.provider_profile_path.read_text(encoding="utf-8")
    )
    assert set(provider["credentials"]) == {"gemini-free-row4-v1"}
    credential = provider["credentials"]["gemini-free-row4-v1"]
    assert credential["provider"] == "google_genai"
    assert credential["base_url"] is None
    role = provider["roles"]["literary_b2_interaction"]
    assert role["model_id"] == "gemini-3.5-flash"
    assert role["bucket_order"] == ["gemini-free-row4-v1"]


def test_openrouter_profile_is_one_call_and_fail_closed() -> None:
    profile = load_b2_ckey_diagnostic_profile_v1(
        OPENROUTER_SCHEMA_PROFILE
    )
    assert profile.max_calls == 1
    assert profile.probe_ids == ("schema_authority_small",)
    assert profile.quota_bucket_id == "openrouter-literary-v1"
    assert profile.openrouter_policy == {
        "provider_only": ["google-vertex"],
        "allow_fallbacks": False,
        "require_parameters": True,
        "data_collection": "deny",
        "zdr": True,
        "reasoning_effort": "minimal",
    }
    provider = json.loads(
        profile.provider_profile_path.read_text(encoding="utf-8")
    )
    credential = provider["credentials"]["openrouter-literary-v1"]
    assert credential["provider"] == "openai"
    assert credential["base_url"] == "https://openrouter.ai/api/v1"
    role = provider["roles"]["literary_b2_interaction"]
    assert role["model_id"] == "google/gemini-3.5-flash"
    assert role["bucket_order"] == ["openrouter-literary-v1"]


def test_ckey_subset_profile_is_one_call_and_one_bucket() -> None:
    profile = load_b2_ckey_diagnostic_profile_v1(
        CKEY_SCHEMA_SUBSET_PROFILE
    )
    assert profile.max_calls == 1
    assert profile.probe_ids == ("schema_authority_small",)
    assert profile.quota_bucket_id == "ckey-account-v1"
    assert profile.openrouter_policy is None
    provider = json.loads(
        profile.provider_profile_path.read_text(encoding="utf-8")
    )
    credential = provider["credentials"]["ckey-account-v1"]
    assert credential["provider"] == "google_genai"
    assert credential["base_url"] == "https://api.xah.io"
    role = provider["roles"]["literary_b2_interaction"]
    assert role["model_id"] == "vuduythanh2023/gemini-3.5-flash"
    assert role["bucket_order"] == ["ckey-account-v1"]


def test_openrouter_profile_rejects_provider_fallback(tmp_path: Path) -> None:
    payload = json.loads(
        OPENROUTER_SCHEMA_PROFILE.read_text(encoding="utf-8")
    )
    source_provider = (
        OPENROUTER_SCHEMA_PROFILE.parent / payload["provider_profile"]
    )
    copied_provider = tmp_path / source_provider.name
    copied_provider.write_bytes(source_provider.read_bytes())
    payload["provider_profile"] = copied_provider.name
    payload["openrouter_policy"]["allow_fallbacks"] = True
    path = tmp_path / "profile.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(B2CkeyDiagnosticError):
        load_b2_ckey_diagnostic_profile_v1(path)


def test_openrouter_transport_injects_sealed_policy() -> None:
    captured: dict = {}

    class _Choice:
        finish_reason = "stop"

    class _Response:
        id = "generation-test"
        choices = [_Choice()]
        service_tier = "default"
        model_extra = {"provider": "Google Vertex"}

    def create(**request):
        captured.update(request)
        return _Response()

    transport = b2_ckey_diagnostic_v1._OpenRouterChatTransport(
        create,
        {
            "provider_only": ["google-vertex"],
            "allow_fallbacks": False,
            "require_parameters": True,
            "data_collection": "deny",
            "zdr": True,
            "reasoning_effort": "minimal",
        },
    )
    result = transport(
        model="google/gemini-3.5-flash",
        messages=[],
        max_completion_tokens=1000,
    )
    assert isinstance(result, _Response)
    assert captured["max_tokens"] == 1000
    assert "max_completion_tokens" not in captured
    assert captured["extra_body"] == {
        "provider": {
            "only": ["google-vertex"],
            "allow_fallbacks": False,
            "require_parameters": True,
            "data_collection": "deny",
            "zdr": True,
        }
    }
    assert transport.last_metadata["openrouter_provider"] == "Google Vertex"
    assert transport.last_metadata["finish_reason"] == "stop"


def test_provider_profile_rejects_probe_count_mismatch(tmp_path: Path) -> None:
    payload = json.loads(
        GOOGLE_OFFICIAL_SCHEMA_PROFILE.read_text(encoding="utf-8")
    )
    source_provider = (
        GOOGLE_OFFICIAL_SCHEMA_PROFILE.parent
        / payload["provider_profile"]
    )
    copied_provider = tmp_path / source_provider.name
    copied_provider.write_bytes(source_provider.read_bytes())
    payload["provider_profile"] = copied_provider.name
    payload["limits"]["max_calls"] = 2
    path = tmp_path / "profile.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(B2CkeyDiagnosticError):
        load_b2_ckey_diagnostic_profile_v1(path)


@pytest.mark.parametrize(
    ("bucket_id", "profile_name", "role_id", "key_row"),
    [
        (bucket_id, *values)
        for bucket_id, values in GOOGLE_OFFICIAL_REMAINING_PROFILES.items()
    ],
)
def test_remaining_google_profiles_pin_one_physical_bucket(
    bucket_id: str,
    profile_name: str,
    role_id: str,
    key_row: int,
) -> None:
    profile = load_b2_ckey_diagnostic_profile_v1(
        RUNTIME_ROOT / "pipeline" / "configs" / profile_name
    )
    assert profile.quota_bucket_id == bucket_id
    assert profile.probe_ids == ("schema_authority_small",)
    assert profile.max_calls == 1
    provider = json.loads(
        profile.provider_profile_path.read_text(encoding="utf-8")
    )
    credential = provider["credentials"][bucket_id]
    assert credential["provider"] == "google_genai"
    assert credential["nonempty_line"] == key_row
    assert credential["base_url"] is None
    role = provider["roles"][role_id]
    assert role["model_id"] == "gemini-3.5-flash"
    assert role["bucket_order"] == [bucket_id]


def test_tranhieu_profile_is_isolated_to_literary_ckey() -> None:
    profile = load_b2_ckey_diagnostic_profile_v1(TRANHIEU_PROFILE)
    provider = json.loads(
        profile.provider_profile_path.read_text(encoding="utf-8")
    )
    assert set(provider["credentials"]) == {"ckey-account-v1"}
    role = provider["roles"]["literary_b2_interaction"]
    assert role["provider"] == "google_genai"
    assert role["model_id"] == "tranhieu13102003/gemini-3.5-flash"
    assert role["bucket_order"] == ["ckey-account-v1"]


def test_tranhieu_openai_profile_uses_ckey_v1_route_only() -> None:
    profile = load_b2_ckey_diagnostic_profile_v1(TRANHIEU_OPENAI_PROFILE)
    provider = json.loads(
        profile.provider_profile_path.read_text(encoding="utf-8")
    )
    assert set(provider["credentials"]) == {"ckey-account-v1"}
    credential = provider["credentials"]["ckey-account-v1"]
    assert credential["provider"] == "openai"
    assert credential["base_url"] == "https://api.xah.io/v1"
    role = provider["roles"]["literary_b2_interaction"]
    assert role["model_id"] == "tranhieu13102003/gemini-3.5-flash"
    assert role["bucket_order"] == ["ckey-account-v1"]


def test_profile_rejects_provider_fallback(tmp_path: Path) -> None:
    payload = json.loads(PROFILE.read_text(encoding="utf-8"))
    payload["safety"]["provider_fallback_allowed"] = True
    source_provider = PROFILE.parent / payload["provider_profile"]
    copied_provider = tmp_path / source_provider.name
    copied_provider.write_bytes(source_provider.read_bytes())
    payload["provider_profile"] = copied_provider.name
    path = tmp_path / "profile.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(B2CkeyDiagnosticError):
        load_b2_ckey_diagnostic_profile_v1(path)


def test_small_b2_probe_uses_real_b2_schema() -> None:
    request, cap = build_probe_request_v1(
        probe_id="b2_schema_small_context",
        full_load_request=_full_request(),
        prompt_token_cap=18_000,
        default_output_token_cap=8_000,
    )
    assert request["response_schema"] == b2_interaction_response_schema()
    assert cap == 3_000
    payload = json.loads(request["messages"][1]["content"])
    assert len(payload["active_blocks"]) == 2
    assert request["request_fingerprint"] == canonical_hash(
        {
            key: value
            for key, value in request.items()
            if key != "request_fingerprint"
        }
    )


def test_schema_authority_probe_rejects_prompt_requested_legacy_shape() -> None:
    request, _ = build_probe_request_v1(
        probe_id="schema_authority_small",
        full_load_request=_full_request(),
        prompt_token_cap=18_000,
        default_output_token_cap=8_000,
    )
    legacy = {"status": "legacy_value", "legacy_field": True}
    result = evaluate_probe_response_v1(
        probe_id="schema_authority_small",
        request=request,
        response_text=json.dumps(legacy),
        parsed=legacy,
        json_error=None,
        finish_reason="STOP",
        usage={"prompt_tokens": 10, "completion_tokens": 10},
        transport_normalization="strict_json",
    )
    assert result["status"] == "failed"
    assert "response_schema_violation" in result["failure_reasons"]


def test_schema_authority_probe_uses_only_supported_gemini_constraints() -> None:
    request, _ = build_probe_request_v1(
        probe_id="schema_authority_small",
        full_load_request=_full_request(),
        prompt_token_cap=18_000,
        default_output_token_cap=8_000,
    )
    schema = request["response_schema"]
    assert schema["properties"]["status"]["enum"] == ["schema_ok"]
    assert "const" not in json.dumps(schema)
    assert "uniqueItems" not in json.dumps(schema)


def test_schema_authority_probe_accepts_schema_shape() -> None:
    request, _ = build_probe_request_v1(
        probe_id="schema_authority_small",
        full_load_request=_full_request(),
        prompt_token_cap=18_000,
        default_output_token_cap=8_000,
    )
    response = {"status": "schema_ok", "values": ["alpha", "beta"]}
    result = evaluate_probe_response_v1(
        probe_id="schema_authority_small",
        request=request,
        response_text=json.dumps(response),
        parsed=response,
        json_error=None,
        finish_reason="STOP",
        usage={"prompt_tokens": 10, "completion_tokens": 10},
        transport_normalization="strict_json",
    )
    assert result["status"] == "passed"
    assert result["schema_valid"] is True


def test_long_probe_requires_more_than_8000_characters() -> None:
    request, _ = build_probe_request_v1(
        probe_id="long_json_transport",
        full_load_request=_full_request(),
        prompt_token_cap=18_000,
        default_output_token_cap=8_000,
    )
    response = {
        "schema_version": "literary_b2_ckey_long_transport_probe_v1",
        "chunks": [
            {"ordinal": index, "payload": "x" * 96}
            for index in range(1, 97)
        ],
    }
    text = json.dumps(response, separators=(",", ":"))
    assert len(text) > 8_000
    result = evaluate_probe_response_v1(
        probe_id="long_json_transport",
        request=request,
        response_text=text,
        parsed=response,
        json_error=None,
        finish_reason="STOP",
        usage={"prompt_tokens": 10, "completion_tokens": 4_000},
        transport_normalization="strict_json",
    )
    assert result["status"] == "passed"
    assert result["diagnostic_checks"]["chunk_count"] == 96


def test_invalid_json_halts_probe() -> None:
    request, _ = build_probe_request_v1(
        probe_id="schema_authority_small",
        full_load_request=_full_request(),
        prompt_token_cap=18_000,
        default_output_token_cap=8_000,
    )
    result = evaluate_probe_response_v1(
        probe_id="schema_authority_small",
        request=request,
        response_text='{"status":',
        parsed=None,
        json_error="truncated JSON",
        finish_reason="STOP",
        usage={"prompt_tokens": 10, "completion_tokens": 2},
        transport_normalization="rejected",
    )
    assert result["status"] == "failed"
    assert result["failure_reasons"] == ["invalid_or_incomplete_json"]


def test_diagnosis_distinguishes_transport_failure_from_schema_failure() -> None:
    transport = [
        {
            "probe_id": "schema_authority_small",
            "status": "failed",
            "failure_reasons": ["transport_error"],
        }
    ]
    schema = [
        {
            "probe_id": "schema_authority_small",
            "status": "failed",
            "failure_reasons": ["response_schema_violation"],
        }
    ]
    assert (
        b2_ckey_diagnostic_v1._diagnosis(transport)
        == "ckey_transport_failed_before_schema_authority_test"
    )
    assert (
        b2_ckey_diagnostic_v1._diagnosis(schema)
        == "ckey_structured_schema_authority_failed"
    )
    assert (
        b2_ckey_diagnostic_v1._diagnosis(
            transport, expected_probe_ids=("schema_authority_small",)
        )
        == "provider_transport_failed_before_schema_authority_test"
    )
    assert (
        b2_ckey_diagnostic_v1._diagnosis(
            schema, expected_probe_ids=("schema_authority_small",)
        )
        == "provider_structured_schema_authority_failed"
    )


def test_full_load_probe_preserves_original_messages_and_schema() -> None:
    full = _full_request()
    request, cap = build_probe_request_v1(
        probe_id="b2_full_load_reproduction",
        full_load_request=full,
        prompt_token_cap=18_000,
        default_output_token_cap=8_000,
    )
    assert request["messages"] == full["messages"]
    assert request["response_schema"] == full["response_schema"]
    assert cap == 8_000
    modified = deepcopy(request)
    modified["messages"][0]["content"] = "changed"
    assert modified["messages"] != full["messages"]
