"""Validated, Console-consumable Literary values for Shared LLM Backend runs."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
import json
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping

from pipeline.llm_backend import canonical_sha256
from pipeline.literary.shared_llm_profiles_v1 import LiterarySharedRolePreset


PROFILE_SCHEMA_VERSION = "literary_shared_llm_runtime_profile_v1"
DEFAULT_PROFILE_PATH = (
    Path(__file__).resolve().parents[1]
    / "configs"
    / "literary_shared_llm_runtime_recommended_v1.json"
)
EXPECTED_ROLE_IDS = frozenset(
    {
        "literary.b1.entity_inventory",
        "literary.audit.local_conflict",
        "literary.audit.stable_claim",
        "literary.audit.identity_surface",
        "literary.b2.frame",
        "literary.b2.interaction",
        "literary.b2.registry_recovery",
        "literary.b2.event_review",
    }
)


class LiterarySharedRuntimeProfileError(ValueError):
    pass


@dataclass(frozen=True)
class LiterarySharedRuntimeProfileV1:
    source_path: Path
    profile_id: str
    profile_revision: str
    backend_mode: str
    source_policy: Mapping[str, Any]
    structured_output: Mapping[str, Any]
    role_presets: Mapping[str, LiterarySharedRolePreset]
    safety: Mapping[str, Any]
    profile_sha256: str

    def public_payload(self) -> dict[str, Any]:
        return {
            "schema_version": PROFILE_SCHEMA_VERSION,
            "profile_id": self.profile_id,
            "profile_revision": self.profile_revision,
            "backend_mode": self.backend_mode,
            "source_policy": deepcopy(dict(self.source_policy)),
            "structured_output": deepcopy(dict(self.structured_output)),
            "roles": [
                literary_role_preset_payload_v1(self.role_presets[role_id])
                for role_id in sorted(self.role_presets)
            ],
            "safety": deepcopy(dict(self.safety)),
            "profile_sha256": self.profile_sha256,
        }


def load_literary_shared_runtime_profile_v1(
    path: Path = DEFAULT_PROFILE_PATH,
) -> LiterarySharedRuntimeProfileV1:
    source = Path(path).resolve()
    try:
        payload = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError) as exc:
        raise LiterarySharedRuntimeProfileError(
            f"cannot load Literary shared runtime profile: {source}"
        ) from exc
    _exact_keys(
        payload,
        {
            "schema_version",
            "profile_id",
            "profile_revision",
            "backend_mode",
            "source_policy",
            "structured_output",
            "roles",
            "safety",
        },
        "runtime profile",
    )
    if payload["schema_version"] != PROFILE_SCHEMA_VERSION:
        raise LiterarySharedRuntimeProfileError("foreign runtime profile schema")
    if payload["backend_mode"] != "shared_v1":
        raise LiterarySharedRuntimeProfileError("runtime profile must use shared_v1")

    source_policy = _object(payload["source_policy"], "source_policy")
    _exact_keys(
        source_policy,
        {
            "selection_mode",
            "recommended_source_id",
            "recommended_source_revision",
            "fallback_enabled",
        },
        "source_policy",
    )
    _text(source_policy["recommended_source_id"], "source id")
    _text(source_policy["recommended_source_revision"], "source revision")
    if (
        source_policy["selection_mode"] != "host_resolved_exact_source"
        or source_policy["fallback_enabled"] is not False
    ):
        raise LiterarySharedRuntimeProfileError("source policy is not fail-closed")

    structured = _object(payload["structured_output"], "structured_output")
    _exact_keys(structured, {"mode", "schema_dialect"}, "structured_output")
    if structured != {
        "mode": "required",
        "schema_dialect": "json_schema_2020_12",
    }:
        raise LiterarySharedRuntimeProfileError(
            "recommended runtime requires native JSON Schema"
        )

    rows = payload["roles"]
    if not isinstance(rows, list):
        raise LiterarySharedRuntimeProfileError("roles must be a list")
    presets: dict[str, LiterarySharedRolePreset] = {}
    namespaces: set[str] = set()
    for index, raw in enumerate(rows, 1):
        preset = parse_literary_role_preset_v1(raw, index=index)
        if preset.role_id in presets:
            raise LiterarySharedRuntimeProfileError("role_id is duplicated")
        for namespace in preset.namespaces.values():
            if namespace in namespaces:
                raise LiterarySharedRuntimeProfileError(
                    "runtime namespaces must be unique across roles"
                )
            namespaces.add(namespace)
        presets[preset.role_id] = preset
    if set(presets) != EXPECTED_ROLE_IDS:
        raise LiterarySharedRuntimeProfileError(
            "runtime profile does not exact-cover active Literary roles"
        )

    safety = _object(payload["safety"], "safety")
    expected_safety = {
        "provider_fallback_allowed": False,
        "application_response_cache_enabled": False,
        "production_publish_enabled": False,
    }
    if safety != expected_safety:
        raise LiterarySharedRuntimeProfileError("runtime safety policy drifted")

    profile_id = _text(payload["profile_id"], "profile_id")
    profile_revision = _text(payload["profile_revision"], "profile_revision")
    normalized_body = {
        "schema_version": PROFILE_SCHEMA_VERSION,
        "profile_id": profile_id,
        "profile_revision": profile_revision,
        "backend_mode": "shared_v1",
        "source_policy": deepcopy(dict(source_policy)),
        "structured_output": deepcopy(dict(structured)),
        "roles": [
            literary_role_preset_payload_v1(presets[role_id])
            for role_id in sorted(presets)
        ],
        "safety": deepcopy(dict(safety)),
    }
    return LiterarySharedRuntimeProfileV1(
        source_path=source,
        profile_id=profile_id,
        profile_revision=profile_revision,
        backend_mode="shared_v1",
        source_policy=MappingProxyType(dict(source_policy)),
        structured_output=MappingProxyType(dict(structured)),
        role_presets=MappingProxyType(presets),
        safety=MappingProxyType(dict(safety)),
        profile_sha256=canonical_sha256(normalized_body),
    )


def parse_literary_role_preset_v1(
    raw: Any, *, index: int
) -> LiterarySharedRolePreset:
    row = _object(raw, f"role {index}")
    _exact_keys(
        row,
        {
            "role_id",
            "preset_id",
            "preset_revision",
            "legacy_role_ids",
            "requested_model_id",
            "generation",
            "limits",
            "transport_retry",
            "semantic_retry",
            "namespaces",
        },
        f"role {index}",
    )
    role_id = _text(row["role_id"], f"role {index} id")
    legacy = row["legacy_role_ids"]
    if not isinstance(legacy, list) or not all(
        isinstance(value, str) and value.strip() for value in legacy
    ):
        raise LiterarySharedRuntimeProfileError("legacy_role_ids are malformed")

    generation = _object(row["generation"], f"{role_id}.generation")
    required_generation_fields = {
        "context_window_tokens",
        "max_input_tokens",
        "max_output_tokens",
        "temperature",
        "top_p",
        "seed",
        "reasoning_effort",
        "verbosity",
    }
    memory_generation_fields = {
        "memory_token_budget",
        "memory_dormancy_chapters",
    }
    if (
        not required_generation_fields <= set(generation)
        or not set(generation)
        <= required_generation_fields | memory_generation_fields
    ):
        raise LiterarySharedRuntimeProfileError(
            f"{role_id}.generation fields differ"
        )
    if set(generation) & memory_generation_fields and role_id != "literary.b1.scan":
        raise LiterarySharedRuntimeProfileError(
            "memory budget fields are reserved for literary.b1.scan"
        )
    _positive_int(generation["max_input_tokens"], "max_input_tokens")
    _positive_int(generation["max_output_tokens"], "max_output_tokens")
    if generation["context_window_tokens"] is not None:
        _positive_int(generation["context_window_tokens"], "context_window_tokens")
    if not isinstance(generation["temperature"], (int, float)) or isinstance(
        generation["temperature"], bool
    ) or not 0 <= generation["temperature"] <= 2:
        raise LiterarySharedRuntimeProfileError("temperature is outside 0..2")
    if not isinstance(generation["top_p"], (int, float)) or isinstance(
        generation["top_p"], bool
    ) or not 0 < generation["top_p"] <= 1:
        raise LiterarySharedRuntimeProfileError("top_p is outside 0..1")
    if generation["seed"] is not None and (
        not isinstance(generation["seed"], int)
        or isinstance(generation["seed"], bool)
    ):
        raise LiterarySharedRuntimeProfileError("seed must be an integer or null")
    if generation["reasoning_effort"] not in {
        "none",
        "minimal",
        "low",
        "medium",
        "high",
        "xhigh",
        "max",
    }:
        raise LiterarySharedRuntimeProfileError("reasoning_effort is invalid")
    if generation["verbosity"] not in {"low", "medium", "high"}:
        raise LiterarySharedRuntimeProfileError("verbosity is invalid")
    if "memory_token_budget" in generation:
        _positive_int(generation["memory_token_budget"], "memory_token_budget")
    if "memory_dormancy_chapters" in generation and (
        not isinstance(generation["memory_dormancy_chapters"], int)
        or isinstance(generation["memory_dormancy_chapters"], bool)
        or generation["memory_dormancy_chapters"] < 0
    ):
        raise LiterarySharedRuntimeProfileError(
            "memory_dormancy_chapters must be a nonnegative integer"
        )

    limits = _object(row["limits"], f"{role_id}.limits")
    _exact_keys(
        limits,
        {
            "max_calls",
            "max_prompt_tokens",
            "max_completion_tokens",
            "max_total_tokens",
            "max_cost_usd",
            "request_timeout_ms",
        },
        f"{role_id}.limits",
    )
    for field in (
        "max_calls",
        "max_prompt_tokens",
        "max_completion_tokens",
        "max_total_tokens",
        "request_timeout_ms",
    ):
        _positive_int(limits[field], f"{role_id}.{field}")
    if limits["max_prompt_tokens"] < (
        generation["max_input_tokens"] * limits["max_calls"]
    ) or limits["max_completion_tokens"] < (
        generation["max_output_tokens"] * limits["max_calls"]
    ) or limits["max_total_tokens"] < (
        limits["max_prompt_tokens"] + limits["max_completion_tokens"]
    ):
        raise LiterarySharedRuntimeProfileError(
            f"{role_id} aggregate limits do not dominate per-call limits"
        )
    if limits["max_cost_usd"] is not None and (
        not isinstance(limits["max_cost_usd"], (int, float))
        or isinstance(limits["max_cost_usd"], bool)
        or limits["max_cost_usd"] < 0
    ):
        raise LiterarySharedRuntimeProfileError("max_cost_usd is invalid")

    transport = _object(row["transport_retry"], f"{role_id}.transport_retry")
    semantic = _object(row["semantic_retry"], f"{role_id}.semantic_retry")
    if transport != {
        "max_retries": 0,
        "backoff_policy": "none",
        "initial_delay_ms": 0,
        "max_delay_ms": 0,
        "retryable_codes": [],
    } or semantic != {"max_retries": 0, "retryable_categories": []}:
        raise LiterarySharedRuntimeProfileError(
            "recommended runtime does not permit hidden retry"
        )

    namespace = _object(row["namespaces"], f"{role_id}.namespaces")
    _exact_keys(namespace, {"output", "checkpoint", "cache"}, "namespaces")
    if not all(_text(value, "namespace") for value in namespace.values()):
        raise LiterarySharedRuntimeProfileError("namespace is empty")
    return LiterarySharedRolePreset(
        role_id=role_id,
        preset_id=_text(row["preset_id"], f"{role_id}.preset_id"),
        preset_revision=_text(
            row["preset_revision"], f"{role_id}.preset_revision"
        ),
        legacy_role_ids=tuple(value.strip() for value in legacy),
        requested_model_id=_text(
            row["requested_model_id"], f"{role_id}.requested_model_id"
        ),
        generation=MappingProxyType(deepcopy(dict(generation))),
        limits=MappingProxyType(deepcopy(dict(limits))),
        transport_retry=MappingProxyType(deepcopy(dict(transport))),
        semantic_retry=MappingProxyType(deepcopy(dict(semantic))),
        namespaces=MappingProxyType(deepcopy(dict(namespace))),
    )


def literary_role_preset_payload_v1(
    preset: LiterarySharedRolePreset,
) -> dict[str, Any]:
    return {
        "role_id": preset.role_id,
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


def _object(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise LiterarySharedRuntimeProfileError(f"{label} must be an object")
    return dict(value)


def _exact_keys(value: Any, keys: set[str], label: str) -> None:
    if not isinstance(value, Mapping) or set(value) != keys:
        raise LiterarySharedRuntimeProfileError(f"{label} keys differ")


def _text(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise LiterarySharedRuntimeProfileError(f"{label} is empty")
    return value.strip()


def _positive_int(value: Any, label: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise LiterarySharedRuntimeProfileError(f"{label} must be positive")
    return value


__all__ = [
    "DEFAULT_PROFILE_PATH",
    "EXPECTED_ROLE_IDS",
    "LiterarySharedRuntimeProfileError",
    "LiterarySharedRuntimeProfileV1",
    "PROFILE_SCHEMA_VERSION",
    "literary_role_preset_payload_v1",
    "load_literary_shared_runtime_profile_v1",
    "parse_literary_role_preset_v1",
]
