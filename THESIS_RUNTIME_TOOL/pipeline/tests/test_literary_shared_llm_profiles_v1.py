from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
import json
from pathlib import Path

import pytest

from pipeline.literary.shared_llm_profiles_v1 import (
    ROLE_PRESETS,
    build_literary_pipeline_profile,
    get_literary_shared_role_preset,
    role_manifest,
)
from pipeline.literary.shared_runtime_profile_v1 import (
    DEFAULT_PROFILE_PATH,
    LiterarySharedRuntimeProfileError,
    load_literary_shared_runtime_profile_v1,
)
from pipeline.llm_backend import canonical_sha256


EXPECTED_ROLES = {
    "literary.b1.entity_inventory",
    "literary.audit.local_conflict",
    "literary.audit.stable_claim",
    "literary.audit.identity_surface",
    "literary.b2.frame",
    "literary.b2.interaction",
    "literary.b2.registry_recovery",
    "literary.b2.event_review",
}


def _source() -> dict:
    return {
        "schema_version": "api_source_v1",
        "source_id": "literary_fake_openai_v1",
        "source_revision": "fake_transport_v1",
        "source_class": "remote_api",
        "adapter_id": "openai_python_v1",
        "protocol": "openai_chat_completions",
        "route_id": "chat_completions_create",
        "endpoint_class": "remote",
        "base_url": "https://provider.invalid/v1",
        "credential_ref": "credential.literary_fake_v1",
        "credential_commitment": "a" * 64,
        "physical_quota_bucket_id": "literary-fake-v1",
        "enabled": True,
    }


def _capability(role_id: str, schema_hash: str, validator_hash: str) -> dict:
    return {
        "schema_version": "capability_evidence_v1",
        "capability_id": f"{role_id}.fake_native_so_v1",
        "capability_revision": "fake_transport_v1",
        "source_id": "literary_fake_openai_v1",
        "source_revision": "fake_transport_v1",
        "adapter_id": "openai_python_v1",
        "protocol": "openai_chat_completions",
        "route_id": "chat_completions_create",
        "base_url": "https://provider.invalid/v1",
        "requested_model_id": "gpt-5.4",
        "observed_model_id": "gpt-5.4",
        "capability_kind": "native_structured_output",
        "schema_dialect": "json_schema_2020_12",
        "schema_sha256": schema_hash,
        "local_validator_id": f"{role_id}.validator_v1",
        "local_validator_sha256": validator_hash,
        "probe_id": f"{role_id}.fake_probe_v1",
        "evidence_sha256": "e" * 64,
        "observed_at_utc": "2026-07-19T00:00:00Z",
        "verdict": "qualified",
    }


def _ref(identifier: str, digest: str) -> dict:
    return {"id": identifier, "revision": "v1", "sha256": digest}


def test_role_manifest_exact_covers_active_literary_roles() -> None:
    assert set(ROLE_PRESETS) == EXPECTED_ROLES
    manifest = role_manifest()
    body = {key: value for key, value in manifest.items() if key != "manifest_sha256"}
    assert manifest["manifest_sha256"] == canonical_sha256(body)
    assert {row["role_id"] for row in manifest["roles"]} == EXPECTED_ROLES


def test_role_presets_preserve_current_caps_and_no_hidden_retry() -> None:
    assert ROLE_PRESETS["literary.b1.entity_inventory"].generation[
        "max_input_tokens"
    ] == 20_000
    assert ROLE_PRESETS["literary.b2.interaction"].generation[
        "max_output_tokens"
    ] == 6_000
    assert ROLE_PRESETS["literary.b2.event_review"].limits["max_calls"] == 6
    for preset in ROLE_PRESETS.values():
        assert preset.transport_retry["max_retries"] == 0
        assert preset.semantic_retry["max_retries"] == 0
        assert len(set(preset.namespaces.values())) == 3


def test_recovery_and_event_roles_no_longer_reuse_local_auditor_namespace() -> None:
    local = ROLE_PRESETS["literary.audit.local_conflict"]
    recovery = ROLE_PRESETS["literary.b2.registry_recovery"]
    event = ROLE_PRESETS["literary.b2.event_review"]
    assert local.role_id != recovery.role_id != event.role_id
    assert not (set(local.namespaces.values()) & set(recovery.namespaces.values()))
    assert not (set(recovery.namespaces.values()) & set(event.namespaces.values()))


@pytest.mark.parametrize("role_id", sorted(EXPECTED_ROLES))
def test_each_role_builds_a_closed_shared_profile(role_id: str) -> None:
    schema_hash = "c" * 64
    validator_hash = "d" * 64
    profile = build_literary_pipeline_profile(
        preset=get_literary_shared_role_preset(role_id),
        api_source=_source(),
        capability=_capability(role_id, schema_hash, validator_hash),
        prompt_ref=_ref(f"{role_id}.prompt_v1", "b" * 64),
        response_schema_ref=_ref(f"{role_id}.schema_v1", schema_hash),
        validator_ref=_ref(f"{role_id}.validator_v1", validator_hash),
        semantic_extension_ref={
            "id": f"{role_id}.authority_v1",
            "schema_version": "literary_semantic_authority_v1",
            "sha256": "f" * 64,
        },
        structured_output={
            "mode": "required",
            "schema_dialect": "json_schema_2020_12",
        },
    )
    role = profile["role_bindings"][0]
    assert role["role_id"] == role_id
    assert role["fallback_plan"] == {"enabled": False, "steps": []}
    assert role["primary"]["source_record_sha256"] == canonical_sha256(_source())


def test_profile_builder_does_not_mutate_source_or_capability() -> None:
    role_id = "literary.b2.frame"
    source = _source()
    capability = _capability(role_id, "c" * 64, "d" * 64)
    before_source = deepcopy(source)
    before_capability = deepcopy(capability)
    build_literary_pipeline_profile(
        preset=ROLE_PRESETS[role_id],
        api_source=source,
        capability=capability,
        prompt_ref=_ref(f"{role_id}.prompt_v1", "b" * 64),
        response_schema_ref=_ref(f"{role_id}.schema_v1", "c" * 64),
        validator_ref=_ref(f"{role_id}.validator_v1", "d" * 64),
        semantic_extension_ref={
            "id": f"{role_id}.authority_v1",
            "schema_version": "literary_semantic_authority_v1",
            "sha256": "f" * 64,
        },
        structured_output={
            "mode": "required",
            "schema_dialect": "json_schema_2020_12",
        },
    )
    assert source == before_source
    assert capability == before_capability


def test_profile_builder_keeps_scan_memory_controls_out_of_backend_generation() -> None:
    role_id = "literary.b1.entity_inventory"
    base = ROLE_PRESETS[role_id]
    preset = replace(
        base,
        generation={
            **dict(base.generation),
            "memory_token_budget": 12_000,
            "memory_dormancy_chapters": 3,
        },
    )

    profile = build_literary_pipeline_profile(
        preset=preset,
        api_source=_source(),
        capability=_capability(role_id, "c" * 64, "d" * 64),
        prompt_ref=_ref(f"{role_id}.prompt_v1", "b" * 64),
        response_schema_ref=_ref(f"{role_id}.schema_v1", "c" * 64),
        validator_ref=_ref(f"{role_id}.validator_v1", "d" * 64),
        semantic_extension_ref={
            "id": f"{role_id}.authority_v1",
            "schema_version": "literary_semantic_authority_v1",
            "sha256": "f" * 64,
        },
        structured_output={
            "mode": "required",
            "schema_dialect": "json_schema_2020_12",
        },
    )

    backend_generation = profile["role_bindings"][0]["generation"]
    assert "memory_token_budget" not in backend_generation
    assert "memory_dormancy_chapters" not in backend_generation
    assert preset.generation["memory_token_budget"] == 12_000


def test_modelapi_declaration_is_discovery_only_and_has_no_retired_fallback() -> None:
    path = (
        Path(__file__).parents[1]
        / "configs"
        / "literary_modelapi_shared_source_declaration_v1.json"
    )
    declaration = json.loads(path.read_text(encoding="utf-8"))
    assert declaration["source_id"] == "modelapi_shared_v1"
    assert declaration["source_revision"] == "modelapi_profile_v1"
    assert declaration["base_url"] == "https://modelapi.vn/v1"
    assert declaration["physical_quota_bucket_id"] == "modelapi-shared-v1"
    assert declaration["capability_state"] == "discovery_only"
    assert declaration["generation_enabled"] is False
    assert declaration["structured_output_qualified"] is False
    assert declaration["fallback"] == {
        "enabled": False,
        "retired_localhost_allowed": False,
        "official_openai_allowed": False,
    }
    assert "credential_commitment" not in declaration


def test_console_runtime_profile_exact_covers_roles_and_current_values() -> None:
    profile = load_literary_shared_runtime_profile_v1()
    assert set(profile.role_presets) == EXPECTED_ROLES
    assert profile.backend_mode == "shared_v1"
    assert profile.source_policy == {
        "selection_mode": "host_resolved_exact_source",
        "recommended_source_id": "modelapi_shared_v1",
        "recommended_source_revision": "modelapi_profile_v1",
        "fallback_enabled": False,
    }
    assert profile.structured_output == {
        "mode": "required",
        "schema_dialect": "json_schema_2020_12",
    }
    for role_id, preset in profile.role_presets.items():
        baseline = ROLE_PRESETS[role_id]
        assert preset.requested_model_id == baseline.requested_model_id
        assert preset.generation == baseline.generation
        assert preset.limits == baseline.limits
        assert preset.transport_retry == baseline.transport_retry
        assert preset.semantic_retry == baseline.semantic_retry
        assert preset.namespaces == baseline.namespaces
    public = profile.public_payload()
    assert public["profile_sha256"] == profile.profile_sha256
    body = {key: value for key, value in public.items() if key != "profile_sha256"}
    assert canonical_sha256(body) == profile.profile_sha256


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (
            lambda row: row["source_policy"].update({"fallback_enabled": True}),
            "source policy",
        ),
        (
            lambda row: row["roles"][0]["transport_retry"].update(
                {"max_retries": 1}
            ),
            "hidden retry",
        ),
        (
            lambda row: row["roles"].pop(),
            "exact-cover",
        ),
        (
            lambda row: row["roles"][0]["limits"].update(
                {"max_total_tokens": 1}
            ),
            "aggregate limits",
        ),
    ],
)
def test_console_runtime_profile_rejects_unsafe_edits(
    tmp_path: Path, mutate, message: str
) -> None:
    payload = json.loads(DEFAULT_PROFILE_PATH.read_text(encoding="utf-8"))
    mutate(payload)
    path = tmp_path / "profile.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(LiterarySharedRuntimeProfileError, match=message):
        load_literary_shared_runtime_profile_v1(path)
