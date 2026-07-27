"""Pipeline-owned Literary role presets for the neutral Shared LLM Backend.

The records in this module contain semantic-role values only. They do not load
credentials, resolve a source, select fallback, or perform a physical request.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Mapping

from pipeline.llm_backend import canonical_sha256, validate_pipeline_profile


PROFILE_ID = "literary_shared_llm_phase3_v1"
PROFILE_REVISION = "phase3_bridge_v1"
ROLE_MANIFEST_SCHEMA_VERSION = "literary_shared_llm_role_manifest_v1"
_BACKEND_GENERATION_FIELDS = frozenset(
    {
        "context_window_tokens",
        "max_input_tokens",
        "max_output_tokens",
        "temperature",
        "top_p",
        "seed",
        "reasoning_effort",
        "verbosity",
    }
)
_PIPELINE_LOCAL_GENERATION_FIELDS = frozenset(
    {"memory_token_budget", "memory_dormancy_chapters"}
)


@dataclass(frozen=True)
class LiterarySharedRolePreset:
    role_id: str
    preset_id: str
    preset_revision: str
    legacy_role_ids: tuple[str, ...]
    requested_model_id: str
    generation: Mapping[str, Any]
    limits: Mapping[str, Any]
    transport_retry: Mapping[str, Any]
    semantic_retry: Mapping[str, Any]
    namespaces: Mapping[str, str]


def _generation(*, max_input_tokens: int, max_output_tokens: int) -> Mapping[str, Any]:
    return MappingProxyType(
        {
            "context_window_tokens": None,
            "max_input_tokens": max_input_tokens,
            "max_output_tokens": max_output_tokens,
            "temperature": 1.0,
            "top_p": 1.0,
            "seed": 20260719,
            "reasoning_effort": "none",
            "verbosity": "low",
        }
    )


_NO_TRANSPORT_RETRY = MappingProxyType(
    {
        "max_retries": 0,
        "backoff_policy": "none",
        "initial_delay_ms": 0,
        "max_delay_ms": 0,
        "retryable_codes": [],
    }
)
_NO_SEMANTIC_RETRY = MappingProxyType(
    {"max_retries": 0, "retryable_categories": []}
)


def _preset(
    role_id: str,
    *,
    legacy_role_ids: tuple[str, ...],
    max_input_tokens: int,
    max_output_tokens: int,
    max_calls: int,
) -> LiterarySharedRolePreset:
    generation = _generation(
        max_input_tokens=max_input_tokens,
        max_output_tokens=max_output_tokens,
    )
    return LiterarySharedRolePreset(
        role_id=role_id,
        preset_id=f"{role_id}.gpt54_recommended_v1",
        preset_revision="v1",
        legacy_role_ids=legacy_role_ids,
        requested_model_id="gpt-5.4",
        generation=generation,
        limits=MappingProxyType(
            {
                "max_calls": max_calls,
                "max_prompt_tokens": max_input_tokens * max_calls,
                "max_completion_tokens": max_output_tokens * max_calls,
                "max_total_tokens": (max_input_tokens + max_output_tokens)
                * max_calls,
                "max_cost_usd": None,
                "request_timeout_ms": 300_000,
            }
        ),
        transport_retry=_NO_TRANSPORT_RETRY,
        semantic_retry=_NO_SEMANTIC_RETRY,
        namespaces=MappingProxyType(
            {
                "output": f"{role_id}.output",
                "checkpoint": f"{role_id}.checkpoint",
                "cache": f"{role_id}.cache",
            }
        ),
    )


ROLE_PRESETS = MappingProxyType(
    {
        "literary.b1.entity_inventory": _preset(
            "literary.b1.entity_inventory",
            legacy_role_ids=("literary_b0", "literary_b0_contract_fallback"),
            max_input_tokens=20_000,
            max_output_tokens=4_096,
            max_calls=1,
        ),
        "literary.audit.local_conflict": _preset(
            "literary.audit.local_conflict",
            legacy_role_ids=("literary_local_conflict_auditor",),
            max_input_tokens=10_000,
            max_output_tokens=4_096,
            max_calls=1,
        ),
        "literary.audit.stable_claim": _preset(
            "literary.audit.stable_claim",
            legacy_role_ids=("literary_stable_claim_auditor",),
            max_input_tokens=10_000,
            max_output_tokens=4_096,
            max_calls=4,
        ),
        "literary.audit.identity_surface": _preset(
            "literary.audit.identity_surface",
            legacy_role_ids=("literary_incremental_identity_auditor",),
            max_input_tokens=10_000,
            max_output_tokens=4_096,
            max_calls=4,
        ),
        "literary.b2.frame": _preset(
            "literary.b2.frame",
            legacy_role_ids=("literary_b2_frame",),
            max_input_tokens=20_000,
            max_output_tokens=2_500,
            max_calls=1,
        ),
        "literary.b2.interaction": _preset(
            "literary.b2.interaction",
            legacy_role_ids=("literary_b2_interaction",),
            max_input_tokens=16_000,
            max_output_tokens=6_000,
            max_calls=6,
        ),
        "literary.b2.registry_recovery": _preset(
            "literary.b2.registry_recovery",
            legacy_role_ids=("literary_local_conflict_auditor",),
            max_input_tokens=12_000,
            max_output_tokens=8_000,
            max_calls=4,
        ),
        "literary.b2.event_review": _preset(
            "literary.b2.event_review",
            legacy_role_ids=("literary_local_conflict_auditor",),
            max_input_tokens=20_000,
            max_output_tokens=12_000,
            max_calls=6,
        ),
    }
)


def get_literary_shared_role_preset(role_id: str) -> LiterarySharedRolePreset:
    try:
        return ROLE_PRESETS[role_id]
    except KeyError as exc:
        raise KeyError(f"unknown Literary shared-LLM role: {role_id}") from exc


def build_literary_pipeline_profile(
    *,
    preset: LiterarySharedRolePreset,
    api_source: Mapping[str, Any],
    capability: Mapping[str, Any],
    prompt_ref: Mapping[str, Any],
    response_schema_ref: Mapping[str, Any],
    validator_ref: Mapping[str, Any],
    semantic_extension_ref: Mapping[str, Any],
    structured_output: Mapping[str, Any],
    profile_id: str = PROFILE_ID,
    profile_revision: str = PROFILE_REVISION,
) -> dict[str, Any]:
    """Bind one Literary semantic role to exact shared source evidence."""

    role = {
        "workstream": "literary",
        "role_id": preset.role_id,
        "preset_id": preset.preset_id,
        "preset_revision": preset.preset_revision,
        "primary": {
            "source_id": api_source["source_id"],
            "source_revision": api_source["source_revision"],
            "source_record_sha256": canonical_sha256(api_source),
            "requested_model_id": preset.requested_model_id,
            "capability_id": capability["capability_id"],
            "capability_revision": capability["capability_revision"],
            "capability_record_sha256": canonical_sha256(capability),
        },
        "fallback_plan": {"enabled": False, "steps": []},
        "generation": _backend_generation(preset.generation),
        "transport_retry": deepcopy(dict(preset.transport_retry)),
        "semantic_retry": deepcopy(dict(preset.semantic_retry)),
        "limits": deepcopy(dict(preset.limits)),
        "structured_output": deepcopy(dict(structured_output)),
        "namespaces": deepcopy(dict(preset.namespaces)),
        "prompt": deepcopy(dict(prompt_ref)),
        "response_schema": deepcopy(dict(response_schema_ref)),
        "validator": deepcopy(dict(validator_ref)),
        "semantic_extension": deepcopy(dict(semantic_extension_ref)),
    }
    return validate_pipeline_profile(
        {
            "schema_version": "pipeline_profile_v1",
            "profile_id": profile_id,
            "profile_revision": profile_revision,
            "workstream": "literary",
            "role_bindings": [role],
        }
    )


def _backend_generation(generation: Mapping[str, Any]) -> dict[str, Any]:
    raw = dict(generation)
    unknown = set(raw) - _BACKEND_GENERATION_FIELDS - _PIPELINE_LOCAL_GENERATION_FIELDS
    if unknown:
        raise ValueError(
            "Literary generation contains fields unknown to both pipeline and backend"
        )
    return {
        key: deepcopy(raw[key])
        for key in _BACKEND_GENERATION_FIELDS
        if key in raw
    }


def role_manifest() -> dict[str, Any]:
    rows = []
    for role_id, preset in sorted(ROLE_PRESETS.items()):
        rows.append(
            {
                "role_id": role_id,
                "preset_id": preset.preset_id,
                "preset_revision": preset.preset_revision,
                "legacy_role_ids": list(preset.legacy_role_ids),
                "requested_model_id": preset.requested_model_id,
                "generation": deepcopy(dict(preset.generation)),
                "limits": deepcopy(dict(preset.limits)),
                "transport_retry": deepcopy(dict(preset.transport_retry)),
                "semantic_retry": deepcopy(dict(preset.semantic_retry)),
                "namespaces": deepcopy(dict(preset.namespaces)),
            }
        )
    body = {"schema_version": ROLE_MANIFEST_SCHEMA_VERSION, "roles": rows}
    return {**body, "manifest_sha256": canonical_sha256(body)}


__all__ = [
    "LiterarySharedRolePreset",
    "PROFILE_ID",
    "PROFILE_REVISION",
    "ROLE_MANIFEST_SCHEMA_VERSION",
    "ROLE_PRESETS",
    "build_literary_pipeline_profile",
    "get_literary_shared_role_preset",
    "role_manifest",
]
