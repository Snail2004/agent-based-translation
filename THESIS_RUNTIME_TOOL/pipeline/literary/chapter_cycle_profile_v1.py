from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
from typing import Any, Mapping

from pipeline.agents.provider_profile import ProviderProfile
from pipeline.literary.chapter_cycle_resilience_v1 import (
    ResiliencePolicy,
)


PROFILE_SCHEMA_VERSION = "literary_chapter_cycle_profile_v1"
LEGACY_REQUIRED_ROLE_KEYS = {
    "b0",
    "b0_contract_fallback",
    "identity_auditor",
    "local_auditor",
    "stable_claim_auditor",
}
LEGACY_REQUIRED_STAGE_KEYS = {
    "b0",
    "identity_auditor",
    "local_auditor",
    "stable_claim_auditor",
}
CHAPTER_LOOP_REQUIRED_ROLE_KEYS = {
    "b0_contract_fallback",
    "b0_summary",
    "b1_enrich",
    "b1_scan",
    "b2",
    "b3_auditor",
    "b3_temporal",
    "identity_auditor",
    "local_auditor",
    "speaker_recovery",
}
CHAPTER_LOOP_REQUIRED_STAGE_KEYS = (
    CHAPTER_LOOP_REQUIRED_ROLE_KEYS - {"b0_contract_fallback"}
)
STAGE_GRAPH_IDS = {"legacy_builder_v3", "literary_chapter_loop_v1"}

_LOCKED_SAFETY = {
    "integrity_failure_action": "pause_no_retry",
    "unknown_failure_action": "pause",
    "semantic_pending_action": "persist_without_authority_and_continue",
    "auditor_smaller_model_fallback_allowed": False,
    "chapter_skip_allowed": False,
    "gold_in_runtime_request_allowed": False,
}


class ChapterCycleProfileError(ValueError):
    """Raised when the Console-facing literary profile is unsafe or malformed."""


@dataclass(frozen=True)
class StageRuntimeLimits:
    prompt_token_cap: int
    max_output_tokens: int
    temperature: float
    seed: int
    reasoning_effort: str
    max_calls_per_chapter: int


@dataclass(frozen=True)
class LiteraryChapterCycleProfile:
    source_path: Path
    profile_id: str
    stage_graph_id: str
    provider_profile_name: str
    role_bindings: Mapping[str, str]
    stage_limits: Mapping[str, StageRuntimeLimits]
    resilience: Mapping[str, Any]
    orchestration: Mapping[str, Any]
    semantic_leads: Mapping[str, Any]
    reporting: Mapping[str, Any]
    locked_safety: Mapping[str, Any]

    def provider_profile_path(self) -> Path:
        return self.source_path.parent / self.provider_profile_name

    def to_resilience_policy(
        self,
        *,
        provider_profile: ProviderProfile,
    ) -> ResiliencePolicy:
        fallback_role_id = self.role_bindings["b0_contract_fallback"]
        fallback_role = provider_profile.roles.get(fallback_role_id)
        if fallback_role is None:
            raise ChapterCycleProfileError(
                "provider profile lacks the configured B0 fallback role"
            )
        return ResiliencePolicy(
            max_transport_retries_per_request=int(
                self.resilience["transport_retries_per_request"]
            ),
            max_contract_repairs=int(
                self.resilience["contract_repairs_per_response"]
            ),
            b0_contract_fallback_enabled=bool(
                self.resilience["b0_contract_fallback_enabled"]
            ),
            b0_fallback_model_id=fallback_role.model_id,
        )


def load_chapter_cycle_profile(path: Path) -> LiteraryChapterCycleProfile:
    source = Path(path).resolve()
    try:
        payload = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError) as exc:
        raise ChapterCycleProfileError(
            f"cannot load literary chapter-cycle profile: {source}"
        ) from exc
    if not isinstance(payload, dict):
        raise ChapterCycleProfileError("chapter-cycle profile must be an object")
    expected_profile_keys = {
            "schema_version",
            "profile_id",
            "provider_profile",
            "role_bindings",
            "stage_limits",
            "resilience",
            "orchestration",
            "semantic_leads",
            "reporting",
            "locked_safety",
    }
    if frozenset(payload) not in {
        frozenset(expected_profile_keys),
        frozenset({*expected_profile_keys, "stage_graph_id"}),
    }:
        raise ChapterCycleProfileError("chapter-cycle profile has a foreign key set")
    if payload["schema_version"] != PROFILE_SCHEMA_VERSION:
        raise ChapterCycleProfileError("foreign chapter-cycle profile schema")
    profile_id = _required_string(payload["profile_id"], "profile_id")
    stage_graph_id = _required_string(
        payload.get("stage_graph_id") or "legacy_builder_v3",
        "stage_graph_id",
    )
    if stage_graph_id not in STAGE_GRAPH_IDS:
        raise ChapterCycleProfileError("chapter-cycle stage graph is unknown")
    provider_profile_name = _contained_file_name(
        payload["provider_profile"],
        "provider_profile",
    )

    role_bindings = _string_map(payload["role_bindings"], "role_bindings")
    expected_roles = (
        CHAPTER_LOOP_REQUIRED_ROLE_KEYS
        if stage_graph_id == "literary_chapter_loop_v1"
        else LEGACY_REQUIRED_ROLE_KEYS
    )
    if set(role_bindings) != expected_roles:
        raise ChapterCycleProfileError("role_bindings key set is not closed")

    stage_payload = _mapping(payload["stage_limits"], "stage_limits")
    expected_stages = (
        CHAPTER_LOOP_REQUIRED_STAGE_KEYS
        if stage_graph_id == "literary_chapter_loop_v1"
        else LEGACY_REQUIRED_STAGE_KEYS
    )
    if set(stage_payload) != expected_stages:
        raise ChapterCycleProfileError("stage_limits key set is not closed")
    stage_limits = {
        stage_id: _stage_limits(value, stage_id)
        for stage_id, value in stage_payload.items()
    }

    resilience = _mapping(payload["resilience"], "resilience")
    _exact_keys(
        resilience,
        {
            "transport_retries_per_request",
            "contract_repairs_per_response",
            "b0_contract_fallback_enabled",
        },
        "resilience",
    )
    _bounded_int(
        resilience["transport_retries_per_request"],
        "transport_retries_per_request",
        minimum=0,
        maximum=5,
    )
    _bounded_int(
        resilience["contract_repairs_per_response"],
        "contract_repairs_per_response",
        minimum=0,
        maximum=1,
    )
    _required_bool(
        resilience["b0_contract_fallback_enabled"],
        "b0_contract_fallback_enabled",
    )

    orchestration = _mapping(payload["orchestration"], "orchestration")
    _exact_keys(
        orchestration,
        {
            "checkpoint_after_each_chapter",
            "allow_chapter_skip",
            "production_publish_enabled",
            "max_api_calls_per_chapter",
            "max_api_calls_per_run",
            "default_stop_after_chapter_count",
        },
        "orchestration",
    )
    if orchestration["checkpoint_after_each_chapter"] is not True:
        raise ChapterCycleProfileError("chapter checkpointing cannot be disabled")
    if orchestration["allow_chapter_skip"] is not False:
        raise ChapterCycleProfileError("chapter skipping cannot be enabled")
    if orchestration["production_publish_enabled"] is not False:
        raise ChapterCycleProfileError(
            "MVP profile cannot enable production publication"
        )
    _bounded_int(
        orchestration["max_api_calls_per_chapter"],
        "max_api_calls_per_chapter",
        minimum=1,
        maximum=100,
    )
    _bounded_int(
        orchestration["max_api_calls_per_run"],
        "max_api_calls_per_run",
        minimum=1,
        maximum=1000,
    )
    _bounded_int(
        orchestration["default_stop_after_chapter_count"],
        "default_stop_after_chapter_count",
        minimum=1,
        maximum=100,
    )
    if (
        int(orchestration["max_api_calls_per_run"])
        < int(orchestration["max_api_calls_per_chapter"])
    ):
        raise ChapterCycleProfileError(
            "run API cap cannot be below the per-chapter cap"
        )

    semantic_leads = _mapping(payload["semantic_leads"], "semantic_leads")
    _exact_keys(
        semantic_leads,
        {
            "max_leads_per_chapter",
            "max_identity_components_per_chapter",
            "overflow_action",
        },
        "semantic_leads",
    )
    _bounded_int(
        semantic_leads["max_leads_per_chapter"],
        "max_leads_per_chapter",
        minimum=0,
        maximum=64,
    )
    _bounded_int(
        semantic_leads["max_identity_components_per_chapter"],
        "max_identity_components_per_chapter",
        minimum=0,
        maximum=32,
    )
    if semantic_leads["overflow_action"] != "defer_without_authority":
        raise ChapterCycleProfileError("semantic lead overflow must defer safely")

    reporting = _mapping(payload["reporting"], "reporting")
    _exact_keys(
        reporting,
        {
            "persist_raw_attempts",
            "persist_usage_when_reported",
            "redact_credentials",
        },
        "reporting",
    )
    if reporting["persist_raw_attempts"] is not True:
        raise ChapterCycleProfileError("raw attempts must remain auditable")
    _required_bool(
        reporting["persist_usage_when_reported"],
        "persist_usage_when_reported",
    )
    if reporting["redact_credentials"] is not True:
        raise ChapterCycleProfileError("credential redaction cannot be disabled")

    locked_safety = _mapping(payload["locked_safety"], "locked_safety")
    if locked_safety != _LOCKED_SAFETY:
        raise ChapterCycleProfileError("locked safety contract was changed")

    return LiteraryChapterCycleProfile(
        source_path=source,
        profile_id=profile_id,
        stage_graph_id=stage_graph_id,
        provider_profile_name=provider_profile_name,
        role_bindings=role_bindings,
        stage_limits=stage_limits,
        resilience=dict(resilience),
        orchestration=dict(orchestration),
        semantic_leads=dict(semantic_leads),
        reporting=dict(reporting),
        locked_safety=dict(locked_safety),
    )


def verify_profile_roles(
    profile: LiteraryChapterCycleProfile,
    *,
    provider_profile: ProviderProfile,
) -> None:
    for binding_name, role_id in profile.role_bindings.items():
        if role_id not in provider_profile.roles:
            raise ChapterCycleProfileError(
                f"provider profile lacks role for {binding_name}"
            )


def _stage_limits(value: Any, stage_id: str) -> StageRuntimeLimits:
    row = _mapping(value, f"stage_limits.{stage_id}")
    _exact_keys(
        row,
        {
            "prompt_token_cap",
            "max_output_tokens",
            "temperature",
            "seed",
            "reasoning_effort",
            "max_calls_per_chapter",
        },
        f"stage_limits.{stage_id}",
    )
    prompt_cap = _bounded_int(
        row["prompt_token_cap"],
        f"{stage_id}.prompt_token_cap",
        minimum=1_000,
        maximum=1_000_000,
    )
    output_cap = _bounded_int(
        row["max_output_tokens"],
        f"{stage_id}.max_output_tokens",
        minimum=256,
        maximum=65_536,
    )
    temperature = row["temperature"]
    if not isinstance(temperature, (int, float)) or isinstance(temperature, bool):
        raise ChapterCycleProfileError(f"{stage_id}.temperature must be numeric")
    if not 0.0 <= float(temperature) <= 2.0:
        raise ChapterCycleProfileError(f"{stage_id}.temperature is out of bounds")
    seed = _bounded_int(
        row["seed"],
        f"{stage_id}.seed",
        minimum=0,
        maximum=2_147_483_647,
    )
    reasoning_effort = _required_string(
        row["reasoning_effort"],
        f"{stage_id}.reasoning_effort",
    )
    max_calls = _bounded_int(
        row["max_calls_per_chapter"],
        f"{stage_id}.max_calls_per_chapter",
        minimum=0,
        maximum=32,
    )
    return StageRuntimeLimits(
        prompt_token_cap=prompt_cap,
        max_output_tokens=output_cap,
        temperature=float(temperature),
        seed=seed,
        reasoning_effort=reasoning_effort,
        max_calls_per_chapter=max_calls,
    )


def _exact_keys(value: Mapping[str, Any], expected: set[str], label: str) -> None:
    if set(value) != expected:
        raise ChapterCycleProfileError(f"{label} has a foreign key set")


def _mapping(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ChapterCycleProfileError(f"{label} must be an object")
    return dict(value)


def _string_map(value: Any, label: str) -> dict[str, str]:
    row = _mapping(value, label)
    return {
        str(key): _required_string(item, f"{label}.{key}")
        for key, item in row.items()
    }


def _required_string(value: Any, label: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise ChapterCycleProfileError(f"{label} is empty")
    return text


def _contained_file_name(value: Any, label: str) -> str:
    text = _required_string(value, label)
    path = Path(text)
    if path.is_absolute() or len(path.parts) != 1 or path.name != text:
        raise ChapterCycleProfileError(f"{label} must be a neighboring file name")
    return text


def _bounded_int(
    value: Any,
    label: str,
    *,
    minimum: int,
    maximum: int,
) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise ChapterCycleProfileError(f"{label} must be an integer")
    if not minimum <= value <= maximum:
        raise ChapterCycleProfileError(f"{label} is out of bounds")
    return value


def _required_bool(value: Any, label: str) -> bool:
    if not isinstance(value, bool):
        raise ChapterCycleProfileError(f"{label} must be bool")
    return value


__all__ = [
    "ChapterCycleProfileError",
    "LiteraryChapterCycleProfile",
    "PROFILE_SCHEMA_VERSION",
    "StageRuntimeLimits",
    "load_chapter_cycle_profile",
    "verify_profile_roles",
]
