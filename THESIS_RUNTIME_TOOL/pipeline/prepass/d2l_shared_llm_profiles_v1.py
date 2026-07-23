"""Pipeline-owned D2L role presets for the shared LLM backend.

This module contains semantic-role defaults only. It does not load credentials,
select a fallback, call a provider, or claim that a new adapter is qualified.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Mapping

from pipeline.llm_backend import canonical_sha256, validate_pipeline_profile


PROFILE_VERSION = "d2l_shared_llm_role_manifest_v1"
PROFILE_ID = "d2l_shared_llm_phase3_v1"
PROFILE_REVISION = "phase3_v1"


@dataclass(frozen=True)
class D2LRolePreset:
    role_id: str
    preset_id: str
    preset_revision: str
    lifecycle: str
    source_choice: str
    requested_model_id: str
    generation: Mapping[str, Any]
    transport_retry: Mapping[str, Any]
    semantic_retry: Mapping[str, Any]
    namespaces: Mapping[str, str]


def _generation(
    *,
    max_input_tokens: int,
    max_output_tokens: int,
    temperature: float,
    seed: int | None,
    reasoning_effort: str,
    verbosity: str,
) -> Mapping[str, Any]:
    return MappingProxyType(
        {
            "context_window_tokens": None,
            "max_input_tokens": max_input_tokens,
            "max_output_tokens": max_output_tokens,
            "temperature": temperature,
            "top_p": 1.0,
            "seed": seed,
            "reasoning_effort": reasoning_effort,
            "verbosity": verbosity,
        }
    )


_ZERO_TRANSPORT_RETRY = MappingProxyType(
    {
        "max_retries": 0,
        "backoff_policy": "none",
        "initial_delay_ms": 0,
        "max_delay_ms": 0,
        "retryable_codes": [],
    }
)
_CANDIDATE_TRANSPORT_RETRY = MappingProxyType(
    {
        "max_retries": 2,
        "backoff_policy": "exponential",
        "initial_delay_ms": 1000,
        "max_delay_ms": 4000,
        "retryable_codes": [
            "connection",
            "rate_limit",
            "server_unavailable",
            "timeout",
        ],
    }
)
_ZERO_SEMANTIC_RETRY = MappingProxyType(
    {"max_retries": 0, "retryable_categories": []}
)
_TRANSLATOR_SEMANTIC_RETRY = MappingProxyType(
    {"max_retries": 1, "retryable_categories": ["pipeline_semantic"]}
)


def _namespaces(role_id: str) -> Mapping[str, str]:
    return MappingProxyType(
        {
            "output": f"{role_id}.output",
            "checkpoint": f"{role_id}.checkpoint",
            "cache": f"{role_id}.cache",
        }
    )


def _preset(
    role_id: str,
    *,
    suffix: str,
    lifecycle: str,
    source_choice: str,
    model: str,
    generation: Mapping[str, Any],
    semantic_retry: Mapping[str, Any] = _ZERO_SEMANTIC_RETRY,
    transport_retry: Mapping[str, Any] = _ZERO_TRANSPORT_RETRY,
) -> D2LRolePreset:
    return D2LRolePreset(
        role_id=role_id,
        preset_id=f"{role_id}.{suffix}",
        preset_revision="v1",
        lifecycle=lifecycle,
        source_choice=source_choice,
        requested_model_id=model,
        generation=generation,
        transport_retry=transport_retry,
        semantic_retry=semantic_retry,
        namespaces=_namespaces(role_id),
    )


ROLE_PRESETS = MappingProxyType(
    {
        "d2l.candidate_discovery": _preset(
            "d2l.candidate_discovery",
            suffix="shopaikey_gemini35_flash_v2",
            lifecycle="active",
            source_choice="shopaikey_gemini_proxy_v1",
            model="gemini-3.5-flash",
            generation=_generation(
                max_input_tokens=6000,
                max_output_tokens=6144,
                temperature=1.0,
                seed=20260612,
                reasoning_effort="none",
                verbosity="low",
            ),
            transport_retry=_CANDIDATE_TRANSPORT_RETRY,
        ),
        "d2l.b2.admission": _preset(
            "d2l.b2.admission",
            suffix="local_gateway_gpt55_v3_2",
            lifecycle="active",
            source_choice="local_gpt_gateway_v1",
            model="gpt-5.5",
            generation=_generation(
                max_input_tokens=6250,
                max_output_tokens=4096,
                temperature=1.0,
                seed=20260718,
                reasoning_effort="none",
                verbosity="low",
            ),
        ),
        "d2l.b2.morphology": _preset(
            "d2l.b2.morphology",
            suffix="local_gateway_gpt55_v1_1",
            lifecycle="active",
            source_choice="local_gpt_gateway_v1",
            model="gpt-5.5",
            generation=_generation(
                max_input_tokens=6000,
                max_output_tokens=4096,
                temperature=1.0,
                seed=20260719,
                reasoning_effort="none",
                verbosity="low",
            ),
        ),
        "d2l.b2.target_collision": _preset(
            "d2l.b2.target_collision",
            suffix="local_gateway_gpt55_v1",
            lifecycle="active",
            source_choice="local_gpt_gateway_v1",
            model="gpt-5.5",
            generation=_generation(
                max_input_tokens=6000,
                max_output_tokens=4096,
                temperature=1.0,
                seed=20260719,
                reasoning_effort="none",
                verbosity="low",
            ),
        ),
        "d2l.b2.multi_target": _preset(
            "d2l.b2.multi_target",
            suffix="local_gateway_gpt55_v1",
            lifecycle="active",
            source_choice="local_gpt_gateway_v1",
            model="gpt-5.5",
            generation=_generation(
                max_input_tokens=6250,
                max_output_tokens=4096,
                temperature=1.0,
                seed=20260718,
                reasoning_effort="none",
                verbosity="low",
            ),
        ),
        "d2l.translator.s0": _preset(
            "d2l.translator.s0",
            suffix="openai_key2_gpt54_mini_v1",
            lifecycle="active",
            source_choice="openai_key2_v1",
            model="gpt-5.4-mini",
            generation=_generation(
                max_input_tokens=6000,
                max_output_tokens=4096,
                temperature=0.3,
                seed=20260612,
                reasoning_effort="none",
                verbosity="low",
            ),
            semantic_retry=_TRANSLATOR_SEMANTIC_RETRY,
        ),
        "d2l.translator.s1": _preset(
            "d2l.translator.s1",
            suffix="openai_key2_gpt54_mini_v1",
            lifecycle="active",
            source_choice="openai_key2_v1",
            model="gpt-5.4-mini",
            generation=_generation(
                max_input_tokens=6000,
                max_output_tokens=4096,
                temperature=0.3,
                seed=20260612,
                reasoning_effort="none",
                verbosity="low",
            ),
            semantic_retry=_TRANSLATOR_SEMANTIC_RETRY,
        ),
        "d2l.legacy.builder_v2": _preset(
            "d2l.legacy.builder_v2",
            suffix="parity_v1",
            lifecycle="legacy_parity",
            source_choice="openai_key2_v1",
            model="gpt-5.4-mini",
            generation=_generation(
                max_input_tokens=6000,
                max_output_tokens=4096,
                temperature=0.3,
                seed=20260612,
                reasoning_effort="none",
                verbosity="low",
            ),
        ),
        "d2l.legacy.term_auditor": _preset(
            "d2l.legacy.term_auditor",
            suffix="parity_v1",
            lifecycle="legacy_parity",
            source_choice="openai_key2_v1",
            model="gpt-5.4-mini",
            generation=_generation(
                max_input_tokens=6000,
                max_output_tokens=4096,
                temperature=0.3,
                seed=20260612,
                reasoning_effort="none",
                verbosity="low",
            ),
        ),
        "d2l.legacy.target_decollision": _preset(
            "d2l.legacy.target_decollision",
            suffix="parity_v1",
            lifecycle="legacy_parity",
            source_choice="openai_key2_v1",
            model="gpt-5.4-mini",
            generation=_generation(
                max_input_tokens=6000,
                max_output_tokens=4096,
                temperature=0.3,
                seed=20260612,
                reasoning_effort="none",
                verbosity="low",
            ),
        ),
        "d2l.legacy.canonical_reelection": _preset(
            "d2l.legacy.canonical_reelection",
            suffix="parity_v1",
            lifecycle="legacy_parity",
            source_choice="openai_key2_v1",
            model="gpt-5.4-mini",
            generation=_generation(
                max_input_tokens=6000,
                max_output_tokens=4096,
                temperature=0.3,
                seed=20260612,
                reasoning_effort="none",
                verbosity="low",
            ),
        ),
    }
)


def get_role_preset(role_id: str) -> D2LRolePreset:
    try:
        return ROLE_PRESETS[role_id]
    except KeyError as exc:
        raise KeyError(f"Unknown D2L shared-LLM role: {role_id}") from exc


def build_pipeline_profile(
    *,
    preset: D2LRolePreset,
    api_source: Mapping[str, Any],
    capability: Mapping[str, Any],
    prompt_ref: Mapping[str, Any],
    response_schema_ref: Mapping[str, Any] | None,
    validator_ref: Mapping[str, Any],
    semantic_extension_ref: Mapping[str, Any],
    structured_output: Mapping[str, Any],
    limits: Mapping[str, Any],
) -> dict[str, Any]:
    source_hash = canonical_sha256(api_source)
    capability_hash = canonical_sha256(capability)
    role = {
        "workstream": "d2l",
        "role_id": preset.role_id,
        "preset_id": preset.preset_id,
        "preset_revision": preset.preset_revision,
        "primary": {
            "source_id": api_source["source_id"],
            "source_revision": api_source["source_revision"],
            "source_record_sha256": source_hash,
            "requested_model_id": preset.requested_model_id,
            "capability_id": capability["capability_id"],
            "capability_revision": capability["capability_revision"],
            "capability_record_sha256": capability_hash,
        },
        "fallback_plan": {"enabled": False, "steps": []},
        "generation": deepcopy(dict(preset.generation)),
        "transport_retry": deepcopy(dict(preset.transport_retry)),
        "semantic_retry": deepcopy(dict(preset.semantic_retry)),
        "limits": deepcopy(dict(limits)),
        "structured_output": deepcopy(dict(structured_output)),
        "namespaces": deepcopy(dict(preset.namespaces)),
        "prompt": deepcopy(dict(prompt_ref)),
        "response_schema": (
            None if response_schema_ref is None else deepcopy(dict(response_schema_ref))
        ),
        "validator": deepcopy(dict(validator_ref)),
        "semantic_extension": deepcopy(dict(semantic_extension_ref)),
    }
    profile = {
        "schema_version": "pipeline_profile_v1",
        "profile_id": PROFILE_ID,
        "profile_revision": PROFILE_REVISION,
        "workstream": "d2l",
        "role_bindings": [role],
    }
    return validate_pipeline_profile(profile)


def role_manifest() -> dict[str, Any]:
    rows = []
    for role_id, preset in sorted(ROLE_PRESETS.items()):
        rows.append(
            {
                "role_id": role_id,
                "preset_id": preset.preset_id,
                "preset_revision": preset.preset_revision,
                "lifecycle": preset.lifecycle,
                "source_choice": preset.source_choice,
                "requested_model_id": preset.requested_model_id,
                "generation": deepcopy(dict(preset.generation)),
                "transport_retry": deepcopy(dict(preset.transport_retry)),
                "semantic_retry": deepcopy(dict(preset.semantic_retry)),
                "namespaces": deepcopy(dict(preset.namespaces)),
            }
        )
    body = {"schema_version": PROFILE_VERSION, "roles": rows}
    return {**body, "manifest_sha256": canonical_sha256(body)}


__all__ = [
    "D2LRolePreset",
    "PROFILE_ID",
    "PROFILE_REVISION",
    "PROFILE_VERSION",
    "ROLE_PRESETS",
    "build_pipeline_profile",
    "get_role_preset",
    "role_manifest",
]
