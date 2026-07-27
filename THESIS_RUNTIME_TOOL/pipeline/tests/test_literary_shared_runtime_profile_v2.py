from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path

import pytest

from pipeline.llm_backend import canonical_sha256
from pipeline.literary.shared_runtime_profile_v1 import EXPECTED_ROLE_IDS
from pipeline.literary.shared_runtime_profile import (
    DEFAULT_RECOMMENDED_PROFILE_PATH,
    load_literary_shared_runtime_profile,
)
from pipeline.literary.shared_runtime_profile_v2 import (
    DEFAULT_PROFILE_V2_PATH,
    LiterarySharedRuntimeProfileV2Error,
    load_literary_shared_runtime_profile_v2,
    validate_runtime_source_against_binding_v2,
)


B2_SLIM_PROFILE_PATH = (
    Path(__file__).resolve().parents[1]
    / "configs"
    / "literary_shared_llm_runtime_openai_b2_slim_v3.json"
)


def _payload() -> dict:
    return json.loads(DEFAULT_PROFILE_V2_PATH.read_text(encoding="utf-8"))


def _write(tmp_path: Path, payload: dict) -> Path:
    path = tmp_path / "profile.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _modelapi_source_row() -> dict:
    return {
        "source_alias": "modelapi_prompt_json",
        "source_id": "modelapi_shared_v1",
        "source_revision": "modelapi_profile_v1",
        "authority_class": "third_party",
        "source_class": "remote_api",
        "adapter_id": "openai_python_v1",
        "protocol": "openai_chat_completions",
        "route_id": "chat_completions_create",
        "endpoint_class": "remote",
        "base_url": "https://modelapi.vn/v1",
        "credential_ref": "credential.modelapi_shared_v1",
        "physical_quota_bucket_id": "modelapi-shared-v1",
        "selection_mode": "host_resolved_exact_source",
        "fallback_enabled": False,
    }


def test_b2_slim_profile_selects_prompt_validated_without_changing_b1() -> None:
    profile = load_literary_shared_runtime_profile_v2(B2_SLIM_PROFILE_PATH)

    assert profile.shared_structured_output_for(
        "literary.b1.entity_inventory"
    )["mode"] == "required"
    for role_id in ("literary.b2.frame", "literary.b2.interaction"):
        assert profile.output_envelope_for(role_id)["mode"] == "json_object"
        assert profile.shared_structured_output_for(role_id) == {
            "mode": "prompt_validated",
            "schema_dialect": "json_schema_2020_12",
        }


def test_v2_profile_exact_covers_roles_and_binds_official_source_per_role() -> None:
    profile = load_literary_shared_runtime_profile_v2()
    assert set(profile.role_bindings) == EXPECTED_ROLE_IDS
    assert profile.backend_mode == "shared_v1"
    for role_id in EXPECTED_ROLE_IDS:
        source = profile.source_binding_for(role_id)
        assert source["authority_class"] == "direct_official_openai"
        assert source["base_url"] == "https://api.openai.com/v1"
        assert profile.output_envelope_for(role_id)["mode"] == "native_schema"
        assert profile.shared_structured_output_for(role_id) == {
            "mode": "required",
            "schema_dialect": "openai_strict_json_schema_subset_v1",
        }
    public = profile.public_payload()
    body = {key: value for key, value in public.items() if key != "profile_sha256"}
    assert canonical_sha256(body) == profile.profile_sha256
    serialized = json.dumps(public, sort_keys=True)
    assert "OPENAI-KEY" not in serialized
    assert "sk-" not in serialized


def test_version_dispatcher_defaults_to_recommended_v2_profile() -> None:
    profile = load_literary_shared_runtime_profile()
    assert DEFAULT_RECOMMENDED_PROFILE_PATH == DEFAULT_PROFILE_V2_PATH
    assert profile.profile_id == "literary_shared_llm_openai_official_v2"


def test_one_role_can_change_source_model_and_envelope_without_mutating_b1(
    tmp_path: Path,
) -> None:
    baseline = load_literary_shared_runtime_profile_v2()
    payload = _payload()
    payload["profile_revision"] = "b2_interaction_modelapi_prompt_v1"
    payload["sources"].append(_modelapi_source_row())
    target = next(
        row
        for row in payload["roles"]
        if row["role_id"] == "literary.b2.interaction"
    )
    target["source_alias"] = "modelapi_prompt_json"
    target["requested_model_id"] = "gpt-5.4-mini"
    target["preset_id"] = "literary.b2.interaction.gpt54mini_prompt_v1"
    target["preset_revision"] = "v1"
    target["output_envelope"] = {
        "mode": "prompt_json",
        "schema_dialect": None,
        "instruction_id": "literary.json_only_output_instruction",
        "instruction_revision": "v2",
    }
    changed = load_literary_shared_runtime_profile_v2(_write(tmp_path, payload))

    assert changed.profile_sha256 != baseline.profile_sha256
    assert (
        changed.role_bindings["literary.b1.entity_inventory"].preset
        == baseline.role_bindings["literary.b1.entity_inventory"].preset
    )
    assert changed.source_binding_for("literary.b1.entity_inventory") == (
        baseline.source_binding_for("literary.b1.entity_inventory")
    )
    assert (
        changed.role_bindings["literary.b2.interaction"].preset.requested_model_id
        == "gpt-5.4-mini"
    )
    assert changed.shared_structured_output_for("literary.b2.interaction") == {
        "mode": "disabled",
        "schema_dialect": None,
    }


def test_third_party_source_cannot_claim_native_schema(tmp_path: Path) -> None:
    payload = _payload()
    payload["sources"].append(_modelapi_source_row())
    target = next(
        row
        for row in payload["roles"]
        if row["role_id"] == "literary.b2.frame"
    )
    target["source_alias"] = "modelapi_prompt_json"
    with pytest.raises(
        LiterarySharedRuntimeProfileV2Error,
        match="third-party source cannot claim native",
    ):
        load_literary_shared_runtime_profile_v2(_write(tmp_path, payload))


def test_native_schema_dialect_must_match_official_source(tmp_path: Path) -> None:
    payload = _payload()
    target = next(
        row
        for row in payload["roles"]
        if row["role_id"] == "literary.b2.frame"
    )
    target["output_envelope"]["schema_dialect"] = (
        "gemini_response_json_schema_subset_v1"
    )
    with pytest.raises(
        LiterarySharedRuntimeProfileV2Error,
        match="dialect differs from source authority",
    ):
        load_literary_shared_runtime_profile_v2(_write(tmp_path, payload))


def test_json_object_declares_local_validator_not_native_schema(
    tmp_path: Path,
) -> None:
    payload = _payload()
    target = next(
        row
        for row in payload["roles"]
        if row["role_id"] == "literary.b2.frame"
    )
    target["output_envelope"] = {
        "mode": "json_object",
        "schema_dialect": "openai_strict_json_schema_subset_v1",
        "instruction_id": "literary.json_only_output_instruction",
        "instruction_revision": "v2",
    }
    with pytest.raises(
        LiterarySharedRuntimeProfileV2Error,
        match="local-validation schema authority",
    ):
        load_literary_shared_runtime_profile_v2(_write(tmp_path, payload))


def test_json_object_uses_shared_prompt_validated_mode(tmp_path: Path) -> None:
    payload = _payload()
    target = next(
        row
        for row in payload["roles"]
        if row["role_id"] == "literary.b2.frame"
    )
    target["output_envelope"] = {
        "mode": "json_object",
        "schema_dialect": "json_schema_2020_12",
        "instruction_id": "literary.json_only_output_instruction",
        "instruction_revision": "v2",
    }
    profile = load_literary_shared_runtime_profile_v2(_write(tmp_path, payload))

    assert profile.output_envelope_for("literary.b2.frame")["mode"] == (
        "json_object"
    )
    assert profile.shared_structured_output_for("literary.b2.frame") == {
        "mode": "prompt_validated",
        "schema_dialect": "json_schema_2020_12",
    }


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("source_id", "foreign"),
        ("source_revision", "foreign"),
        ("base_url", "https://proxy.invalid/v1"),
        ("credential_ref", "credential.foreign"),
        ("physical_quota_bucket_id", "foreign-row"),
    ],
)
def test_runtime_source_must_exactly_match_profile_binding(
    field: str, value: str
) -> None:
    profile = load_literary_shared_runtime_profile_v2()
    binding = profile.source_binding_for("literary.b1.entity_inventory")
    source = {
        "schema_version": "api_source_v1",
        "source_id": binding["source_id"],
        "source_revision": binding["source_revision"],
        "source_class": binding["source_class"],
        "adapter_id": binding["adapter_id"],
        "protocol": binding["protocol"],
        "route_id": binding["route_id"],
        "endpoint_class": binding["endpoint_class"],
        "base_url": binding["base_url"],
        "credential_ref": binding["credential_ref"],
        "credential_commitment": "a" * 64,
        "physical_quota_bucket_id": binding["physical_quota_bucket_id"],
        "enabled": True,
    }
    source[field] = value
    with pytest.raises(LiterarySharedRuntimeProfileV2Error, match=field):
        validate_runtime_source_against_binding_v2(
            source=source, binding=binding
        )


def test_profile_rejects_native_schema_on_official_lookalike(tmp_path: Path) -> None:
    payload = _payload()
    payload["sources"][0]["base_url"] = "https://api.openai.com.example/v1"
    with pytest.raises(
        LiterarySharedRuntimeProfileV2Error, match="not direct"
    ):
        load_literary_shared_runtime_profile_v2(_write(tmp_path, payload))


def test_profile_edit_does_not_mutate_loaded_baseline(tmp_path: Path) -> None:
    payload = _payload()
    before = deepcopy(payload)
    load_literary_shared_runtime_profile_v2(_write(tmp_path, payload))
    assert payload == before
