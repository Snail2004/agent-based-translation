from __future__ import annotations

from pathlib import Path

import pytest

from pipeline.eval.llm_profiles_v1 import SF_BT_SEMANTIC_JUDGE_ROLE_ID
from pipeline.scripts.run_evaluation_sf_bt_band_calibration_v1 import (
    _CallPacer,
    _build_profile,
    _contained_output_root,
    _require_source_credential_binding,
    _require_source_row_binding,
)
from pipeline.scripts.run_evaluation_ckey_capability_probe_v1 import (
    build_ckey_google_compatible_source_v1,
    build_ckey_openai_compatible_source_v1,
    load_selected_credential_row_v1,
)


def _official_google_source() -> dict[str, object]:
    return {
        "schema_version": "api_source_v1",
        "source_id": "google-gemini-free-row2-v1",
        "source_revision": "gemini-free-row2-v1",
        "source_class": "remote_api",
        "adapter_id": "google_genai_rest_v1",
        "protocol": "google_genai_generate_content",
        "route_id": "models_generate_content",
        "endpoint_class": "remote",
        "base_url": "https://generativelanguage.googleapis.com/v1beta",
        "credential_ref": "shared.google.gemini_free.row2",
        "credential_commitment": "1" * 64,
        "physical_quota_bucket_id": "gemini-free-row2-v1",
        "enabled": True,
    }


def _capability(
    *, capability_kind: str, requested_model_id: str
) -> dict[str, str]:
    return {
        "capability_id": "evaluation.sf_bt.semantic_judge.fixture",
        "capability_revision": "fixture_v1",
        "capability_kind": capability_kind,
        "requested_model_id": requested_model_id,
    }


def test_official_google_capability_builds_semantic_only_required_profile() -> None:
    source = _official_google_source()
    capability = _capability(
        capability_kind="native_structured_output",
        requested_model_id="gemini-3.5-flash",
    )
    profile = _build_profile(
        source,
        capability,
        profile_id="evaluation-band-test-profile",
        profile_revision="test-v1",
    )
    assert [row["role_id"] for row in profile["role_bindings"]] == [
        SF_BT_SEMANTIC_JUDGE_ROLE_ID
    ]
    role = profile["role_bindings"][0]
    assert role["structured_output"]["mode"] == "required"
    assert role["primary"]["requested_model_id"] == "gemini-3.5-flash"
    assert role["generation"]["max_input_tokens"] == 4_096
    assert role["limits"]["max_prompt_tokens"] == 4_096
    assert role["limits"]["max_completion_tokens"] == 512
    assert role["limits"]["max_total_tokens"] == 4_608
    assert role["transport_retry"]["max_retries"] == 0
    assert role["semantic_retry"]["max_retries"] == 0
    assert role["fallback_plan"] == {"enabled": False, "steps": []}


def test_physical_row_must_match_capability_source() -> None:
    source = _official_google_source()
    _require_source_row_binding(source, 2)
    with pytest.raises(ValueError, match="differs"):
        _require_source_row_binding(source, 1)


def test_json_object_capability_builds_prompt_validated_profile() -> None:
    capability = _capability(
        capability_kind="json_object",
        requested_model_id="vendor/gemini-3.5-flash",
    )
    source = {
        **_official_google_source(),
        "source_id": "ckey-evaluation-v1",
        "source_revision": "ckey-evaluation-revision-v1",
        "adapter_id": "openai_compatible_chat_v1",
        "protocol": "openai_chat_completions",
        "route_id": "chat_completions",
        "base_url": "https://proxy.example.test/v1",
        "credential_ref": "shared.ckey.account-v1",
        "physical_quota_bucket_id": "ckey-account-v1",
    }
    profile = _build_profile(
        source,
        capability,
        profile_id="evaluation-band-ckey-test-profile",
        profile_revision="test-v1",
    )
    role = profile["role_bindings"][0]
    assert role["structured_output"]["mode"] == "prompt_validated"
    assert role["primary"]["requested_model_id"] == (
        "vendor/gemini-3.5-flash"
    )
    assert role["response_schema"]["id"] == (
        "evaluation_sf_bt_semantic_response_v1"
    )
    assert role["validator"]["id"] == "evaluation_sf_bt_semantic_validator_v3"
    assert role["generation"]["max_input_tokens"] == 8_192
    assert role["generation"]["max_output_tokens"] == 512
    assert role["limits"]["max_prompt_tokens"] == 8_192
    assert role["limits"]["max_completion_tokens"] == 2_048
    assert role["limits"]["max_total_tokens"] == 10_240


def test_third_party_credential_binding_is_explicit() -> None:
    source = {
        "protocol": "openai_chat_completions",
        "credential_ref": "shared.ckey.account-v1",
    }
    _require_source_credential_binding(
        source,
        physical_row=1,
        expected_credential_ref="shared.ckey.account-v1",
    )
    with pytest.raises(ValueError, match="require --expected-credential-ref"):
        _require_source_credential_binding(
            source,
            physical_row=1,
            expected_credential_ref=None,
        )
    with pytest.raises(ValueError, match="differs"):
        _require_source_credential_binding(
            source,
            physical_row=1,
            expected_credential_ref="shared.ckey.foreign",
        )


def test_ckey_source_loader_keeps_plaintext_out_of_source(tmp_path: Path) -> None:
    secret = "fixture-ckey-credential-value-123456789"
    path = tmp_path / "CKEY.txt"
    path.write_text(secret + "\n", encoding="utf-8")
    selected = load_selected_credential_row_v1(
        path, physical_row=1, expected_row_count=1
    )
    source = build_ckey_openai_compatible_source_v1(
        source_id="ckey-evaluation-v1",
        source_revision="ckey-evaluation-revision-v1",
        credential_ref="shared.ckey.account-v1",
        physical_quota_bucket_id="ckey-account-v1",
        credential=selected,
    )
    assert source["base_url"] == "https://api.xah.io/v1"
    assert source["protocol"] == "openai_chat_completions"
    assert source["credential_commitment"] != secret
    assert secret not in repr(source)

    google_source = build_ckey_google_compatible_source_v1(
        source_id="ckey-evaluation-google-v1",
        source_revision="ckey-evaluation-google-revision-v1",
        credential_ref="shared.ckey.account-v1",
        physical_quota_bucket_id="ckey-account-v1",
        credential=selected,
    )
    assert google_source["base_url"] == "https://api.xah.io/v1beta"
    assert google_source["protocol"] == "google_genai_generate_content"
    assert google_source["credential_commitment"] != secret
    assert secret not in repr(google_source)


def test_output_root_is_contained_and_uses_posix_relative_path(tmp_path: Path) -> None:
    assert _contained_output_root(tmp_path, "data/reports/calibration") == (
        tmp_path / "data" / "reports" / "calibration"
    ).resolve()
    for value in ("../escape", "C:/escape", "data\\escape"):
        with pytest.raises(ValueError):
            _contained_output_root(tmp_path, value)


@pytest.mark.parametrize("value", [-1, float("nan"), True])
def test_call_pacer_rejects_invalid_intervals(value: object) -> None:
    with pytest.raises(ValueError):
        _CallPacer(value)  # type: ignore[arg-type]


def test_call_pacer_allows_zero_interval() -> None:
    pacer = _CallPacer(0)
    pacer.wait()
    pacer.wait()
