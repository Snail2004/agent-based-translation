from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
from typing import Any, Mapping

from pipeline.agents.provider_profile import load_provider_profile
from pipeline.literary.b2_context_v1 import load_b2_phase_a_profile
from pipeline.literary.chapter_cycle_profile_v1 import load_chapter_cycle_profile
from pipeline.literary.checkpoint import canonical_hash, file_sha256
from pipeline.literary.literary_pipeline_profile_v1 import (
    load_literary_pipeline_profile,
)
from pipeline.literary.structured_output_policy_v1 import (
    load_literary_structured_output_policy,
)


PROFILE_SCHEMA_VERSION = "literary_context_pipeline_profile_v1"


class LiteraryContextPipelineProfileError(ValueError):
    """Raised when the B1-through-B2 pipeline profile is unsafe or incomplete."""


@dataclass(frozen=True)
class LiteraryContextPipelineProfile:
    source_path: Path
    profile_id: str
    b1_pipeline_profile_path: Path
    b2_phase_profile_path: Path
    provider_profile_path: Path
    structured_output_policy_path: Path
    role_bindings: Mapping[str, str]
    contract_versions: Mapping[str, str]
    generation: Mapping[str, Any]
    recovery_stage_limits: Mapping[str, Mapping[str, int]]
    limits: Mapping[str, int]
    safety: Mapping[str, Any]
    profile_hash: str
    source_sha256: str


def load_context_pipeline_profile_v1(
    path: Path,
) -> LiteraryContextPipelineProfile:
    source = Path(path).resolve()
    payload = _read_object(source, "context pipeline profile")
    _exact_keys(
        payload,
        {
            "schema_version",
            "profile_id",
            "b1_pipeline_profile",
            "b2_phase_profile",
            "provider_profile",
            "structured_output_policy",
            "role_bindings",
            "contract_versions",
            "generation",
            "recovery_stage_limits",
            "limits",
            "safety",
        },
        "context pipeline profile",
    )
    if payload.get("schema_version") != PROFILE_SCHEMA_VERSION:
        raise LiteraryContextPipelineProfileError(
            "foreign context pipeline profile schema"
        )

    roles = _object(payload.get("role_bindings"), "role_bindings")
    _exact_keys(
        roles,
        {"b2_frame", "b2_interaction", "registry_recovery", "event_review"},
        "role_bindings",
    )
    role_bindings = {
        key: _required_string(value, f"role_bindings.{key}")
        for key, value in roles.items()
    }

    contracts = _object(payload.get("contract_versions"), "contract_versions")
    _exact_keys(
        contracts,
        {"frame", "interaction", "event_review"},
        "contract_versions",
    )
    if contracts != {
        "frame": "v1",
        "interaction": "v2",
        "event_review": "v2",
    }:
        raise LiteraryContextPipelineProfileError(
            "unsupported context-pipeline semantic contracts"
        )

    generation = _object(payload.get("generation"), "generation")
    _exact_keys(
        generation,
        {"temperature", "seed", "reasoning_effort", "verbosity"},
        "generation",
    )
    temperature = generation.get("temperature")
    seed = generation.get("seed")
    if (
        not isinstance(temperature, (int, float))
        or isinstance(temperature, bool)
        or not 0 <= float(temperature) <= 2
        or not isinstance(seed, int)
        or isinstance(seed, bool)
        or generation.get("reasoning_effort") not in {"none", "low", "medium"}
        or generation.get("verbosity") not in {"low", "medium"}
    ):
        raise LiteraryContextPipelineProfileError(
            "context-pipeline generation controls are invalid"
        )

    recovery_stage_limits_raw = _object(
        payload.get("recovery_stage_limits"), "recovery_stage_limits"
    )
    _exact_keys(
        recovery_stage_limits_raw,
        {"registry_recovery", "event_review"},
        "recovery_stage_limits",
    )
    recovery_stage_limits: dict[str, dict[str, int]] = {}
    for stage_id, raw_stage in recovery_stage_limits_raw.items():
        stage = _object(raw_stage, f"recovery_stage_limits.{stage_id}")
        _exact_keys(
            stage,
            {"prompt_token_cap", "max_output_tokens"},
            f"recovery_stage_limits.{stage_id}",
        )
        recovery_stage_limits[stage_id] = {
            "prompt_token_cap": _bounded_int(
                stage.get("prompt_token_cap"),
                f"{stage_id}.prompt_token_cap",
                1000,
                100000,
            ),
            "max_output_tokens": _bounded_int(
                stage.get("max_output_tokens"),
                f"{stage_id}.max_output_tokens",
                1000,
                100000,
            ),
        }

    limits_raw = _object(payload.get("limits"), "limits")
    _exact_keys(
        limits_raw,
        {
            "max_chapters_per_run",
            "b2_interaction_calls_per_chapter_cap",
            "b2_hard_visible_token_cap_per_chapter",
            "recovery_registry_calls_per_chapter_cap",
            "recovery_event_calls_per_chapter_cap",
            "recovery_hard_visible_token_cap_per_chapter",
            "max_b2_attempts_per_chapter",
            "max_recovery_attempts_per_chapter",
        },
        "limits",
    )
    limit_bounds = {
        "max_chapters_per_run": (1, 100),
        "b2_interaction_calls_per_chapter_cap": (1, 6),
        "b2_hard_visible_token_cap_per_chapter": (1, 1_000_000),
        "recovery_registry_calls_per_chapter_cap": (0, 4),
        "recovery_event_calls_per_chapter_cap": (1, 6),
        "recovery_hard_visible_token_cap_per_chapter": (1, 1_000_000),
        "max_b2_attempts_per_chapter": (1, 3),
        "max_recovery_attempts_per_chapter": (1, 3),
    }
    limits = {
        key: _bounded_int(limits_raw.get(key), key, low, high)
        for key, (low, high) in limit_bounds.items()
    }

    safety = _object(payload.get("safety"), "safety")
    _exact_keys(
        safety,
        {
            "provider_fallback_allowed",
            "semantic_pending_action",
            "integrity_failure_action",
            "source_artifact_mutation_allowed",
            "book_global_identity_mutation_allowed",
            "gold_in_runtime_request_allowed",
            "production_publish_enabled",
        },
        "safety",
    )
    locked_safety = {
        "provider_fallback_allowed": False,
        "semantic_pending_action": "persist_without_authority_and_continue",
        "integrity_failure_action": "halt_before_next_call",
        "source_artifact_mutation_allowed": False,
        "book_global_identity_mutation_allowed": False,
        "gold_in_runtime_request_allowed": False,
        "production_publish_enabled": False,
    }
    if safety != locked_safety:
        raise LiteraryContextPipelineProfileError(
            "context-pipeline safety contract was weakened"
        )

    b1_path = _sibling_file(
        source, payload.get("b1_pipeline_profile"), "b1_pipeline_profile"
    )
    b2_path = _sibling_file(
        source, payload.get("b2_phase_profile"), "b2_phase_profile"
    )
    provider_path = _sibling_file(
        source, payload.get("provider_profile"), "provider_profile"
    )
    policy_path = _sibling_file(
        source,
        payload.get("structured_output_policy"),
        "structured_output_policy",
    )

    b1_profile = load_literary_pipeline_profile(b1_path)
    if b1_profile.public_stages["b2"].enabled:
        raise LiteraryContextPipelineProfileError(
            "B1 profile must leave B2 to the outer context runner"
        )
    chapter_cycle = load_chapter_cycle_profile(
        b1_profile.chapter_cycle_profile_path
    )
    if file_sha256(chapter_cycle.provider_profile_path()) != file_sha256(
        provider_path
    ):
        raise LiteraryContextPipelineProfileError(
            "B1 and downstream stages resolve different provider profiles"
        )
    if (
        b1_profile.structured_output_policy is None
        or b1_profile.structured_output_policy.source_sha256
        != file_sha256(policy_path)
    ):
        raise LiteraryContextPipelineProfileError(
            "B1 and downstream stages resolve different structured-output policies"
        )

    _ = load_b2_phase_a_profile(b2_path)
    provider = load_provider_profile(provider_path)
    _ = load_literary_structured_output_policy(policy_path)
    for role_name, role_id in role_bindings.items():
        role = provider.roles.get(role_id)
        if role is None:
            raise LiteraryContextPipelineProfileError(
                f"provider profile lacks {role_name} role: {role_id}"
            )
        if len(role.bucket_order) != 1:
            raise LiteraryContextPipelineProfileError(
                f"{role_name} role contains an unsealed fallback route"
            )

    return LiteraryContextPipelineProfile(
        source_path=source,
        profile_id=_required_string(payload.get("profile_id"), "profile_id"),
        b1_pipeline_profile_path=b1_path,
        b2_phase_profile_path=b2_path,
        provider_profile_path=provider_path,
        structured_output_policy_path=policy_path,
        role_bindings=role_bindings,
        contract_versions={
            key: str(value) for key, value in contracts.items()
        },
        generation=dict(generation),
        recovery_stage_limits=recovery_stage_limits,
        limits=limits,
        safety=dict(safety),
        profile_hash=canonical_hash(payload),
        source_sha256=file_sha256(source),
    )


def _read_object(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError) as exc:
        raise LiteraryContextPipelineProfileError(
            f"cannot load {label}: {path}"
        ) from exc
    if not isinstance(value, dict):
        raise LiteraryContextPipelineProfileError(f"{label} must be an object")
    return value


def _object(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise LiteraryContextPipelineProfileError(f"{label} must be an object")
    return dict(value)


def _exact_keys(value: Mapping[str, Any], keys: set[str], label: str) -> None:
    if set(value) != keys:
        raise LiteraryContextPipelineProfileError(f"{label} keys drifted")


def _required_string(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise LiteraryContextPipelineProfileError(
            f"{label} must be a non-empty string"
        )
    return value.strip()


def _bounded_int(value: Any, label: str, low: int, high: int) -> int:
    if (
        not isinstance(value, int)
        or isinstance(value, bool)
        or not low <= value <= high
    ):
        raise LiteraryContextPipelineProfileError(
            f"{label} must be in [{low}, {high}]"
        )
    return value


def _sibling_file(source: Path, value: Any, label: str) -> Path:
    relative = Path(_required_string(value, label))
    if relative.is_absolute() or len(relative.parts) != 1:
        raise LiteraryContextPipelineProfileError(
            f"{label} must name one sibling file"
        )
    path = (source.parent / relative).resolve()
    if not path.is_file():
        raise LiteraryContextPipelineProfileError(f"{label} is absent: {path}")
    return path
