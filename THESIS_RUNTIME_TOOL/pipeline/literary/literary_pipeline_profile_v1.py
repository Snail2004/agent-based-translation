"""Versioned top-level profile for the unified Literary pipeline facade."""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
from typing import Any, Mapping

from pipeline.literary.checkpoint import canonical_hash, file_sha256
from pipeline.literary.structured_output_policy_v1 import (
    LiteraryStructuredOutputPolicy,
    load_literary_structured_output_policy,
)


PROFILE_SCHEMA_VERSION = "literary_pipeline_profile_v1"
PROFILE_SCHEMA_VERSION_V2 = "literary_pipeline_profile_v2"
USAGE_BASELINE_SCHEMA_VERSION = "literary_openai_usage_baseline_v1"
PUBLIC_STAGE_IDS = (
    "b1",
    "local_auditor",
    "stable_claim_auditor",
    "identity_surface_auditor",
    "b2",
)


class LiteraryPipelineProfileError(ValueError):
    pass


@dataclass(frozen=True)
class PublicStageBinding:
    public_stage_id: str
    enabled: bool
    implementation_role: str | None
    implementation_stage_names: tuple[str, ...]


@dataclass(frozen=True)
class LiteraryOpenAIUsageBaseline:
    baseline_id: str
    quota_bucket_id: str
    credential_revision: str
    provider_counter_baselines: Mapping[str, int]
    counter_unit: str
    reset_period: str | None
    hard_quota: int | None
    remaining_quota_must_not_be_inferred: bool
    source_path: Path
    source_sha256: str


@dataclass(frozen=True)
class LiteraryPipelineProfile:
    profile_id: str
    chapter_cycle_profile_path: Path
    design_doc_path: Path
    usage_baseline: LiteraryOpenAIUsageBaseline
    public_stages: Mapping[str, PublicStageBinding]
    chapter_selection: Mapping[str, bool]
    console_controls: Mapping[str, bool]
    structured_output_policy: LiteraryStructuredOutputPolicy | None
    production_publish_enabled: bool
    profile_hash: str
    source_path: Path
    source_sha256: str

    def public_stage_name(self, implementation_stage_name: str) -> str:
        for binding in self.public_stages.values():
            if implementation_stage_name in binding.implementation_stage_names:
                return binding.public_stage_id
        return implementation_stage_name

    def seal_payload(self) -> dict[str, Any]:
        payload = {
            "pipeline_profile_path": str(self.source_path),
            "pipeline_profile_sha256": self.source_sha256,
            "pipeline_profile_id": self.profile_id,
            "pipeline_profile_hash": self.profile_hash,
            "design_doc_path": str(self.design_doc_path),
            "design_doc_sha256": file_sha256(self.design_doc_path),
            "usage_baseline_path": str(self.usage_baseline.source_path),
            "usage_baseline_sha256": self.usage_baseline.source_sha256,
            "usage_baseline_id": self.usage_baseline.baseline_id,
            "public_stage_contract": {
                stage_id: {
                    "enabled": row.enabled,
                    "implementation_role": row.implementation_role,
                    "implementation_stage_names": list(
                        row.implementation_stage_names
                    ),
                }
                for stage_id, row in self.public_stages.items()
            },
            "public_stage_aliases": {
                implementation_name: stage_id
                for stage_id, row in self.public_stages.items()
                for implementation_name in row.implementation_stage_names
            },
            "future_b2_enabled": self.public_stages["b2"].enabled,
        }
        if self.structured_output_policy is not None:
            payload.update(
                {
                    "structured_output_policy_path": str(
                        self.structured_output_policy.source_path
                    ),
                    "structured_output_policy_sha256": (
                        self.structured_output_policy.source_sha256
                    ),
                    "structured_output_policy_id": (
                        self.structured_output_policy.policy_id
                    ),
                    "structured_output_policy_hash": (
                        self.structured_output_policy.policy_hash
                    ),
                }
            )
        return payload


def _load_json(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError) as exc:
        raise LiteraryPipelineProfileError(f"cannot load {label}: {path}") from exc
    if not isinstance(value, dict):
        raise LiteraryPipelineProfileError(f"{label} must be an object")
    return value


def _exact_keys(value: Mapping[str, Any], expected: set[str], label: str) -> None:
    if set(value) != expected:
        raise LiteraryPipelineProfileError(f"{label} has a foreign key set")


def _required_string(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise LiteraryPipelineProfileError(f"{label} must be a non-empty string")
    return value


def _required_bool(value: Any, label: str) -> bool:
    if not isinstance(value, bool):
        raise LiteraryPipelineProfileError(f"{label} must be bool")
    return value


def _mapping(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise LiteraryPipelineProfileError(f"{label} must be an object")
    return dict(value)


def _resolve_profile_file(
    *, source_path: Path, relative_value: Any, label: str, repository_root: Path
) -> Path:
    raw = _required_string(relative_value, label)
    relative = Path(raw)
    if relative.is_absolute():
        raise LiteraryPipelineProfileError(f"{label} must be relative")
    resolved = (source_path.parent / relative).resolve()
    try:
        resolved.relative_to(repository_root.resolve())
    except ValueError as exc:
        raise LiteraryPipelineProfileError(f"{label} escapes repository root") from exc
    if not resolved.is_file():
        raise LiteraryPipelineProfileError(f"{label} is absent: {resolved}")
    return resolved


def load_openai_usage_baseline(path: Path) -> LiteraryOpenAIUsageBaseline:
    source = Path(path).resolve()
    raw = _load_json(source, "OpenAI usage baseline")
    _exact_keys(
        raw,
        {
            "schema_version",
            "baseline_id",
            "quota_bucket_id",
            "credential_revision",
            "provider_counter_baselines",
            "counter_unit",
            "reset_period",
            "hard_quota",
            "remaining_quota_must_not_be_inferred",
        },
        "OpenAI usage baseline",
    )
    if raw["schema_version"] != USAGE_BASELINE_SCHEMA_VERSION:
        raise LiteraryPipelineProfileError("foreign OpenAI usage baseline schema")
    counters = _mapping(
        raw["provider_counter_baselines"], "provider_counter_baselines"
    )
    if not counters or any(
        not isinstance(value, int) or isinstance(value, bool) or value < 0
        for value in counters.values()
    ):
        raise LiteraryPipelineProfileError("usage baseline counters are invalid")
    reset_period = raw["reset_period"]
    hard_quota = raw["hard_quota"]
    if reset_period is not None and not isinstance(reset_period, str):
        raise LiteraryPipelineProfileError("reset_period must be null or string")
    if hard_quota is not None and (
        not isinstance(hard_quota, int)
        or isinstance(hard_quota, bool)
        or hard_quota < 0
    ):
        raise LiteraryPipelineProfileError("hard_quota must be null or integer")
    if raw["remaining_quota_must_not_be_inferred"] is not True:
        raise LiteraryPipelineProfileError(
            "usage baseline must forbid remaining-quota inference"
        )
    return LiteraryOpenAIUsageBaseline(
        baseline_id=_required_string(raw["baseline_id"], "baseline_id"),
        quota_bucket_id=_required_string(
            raw["quota_bucket_id"], "quota_bucket_id"
        ),
        credential_revision=_required_string(
            raw["credential_revision"], "credential_revision"
        ),
        provider_counter_baselines={
            str(key): int(value) for key, value in counters.items()
        },
        counter_unit=_required_string(raw["counter_unit"], "counter_unit"),
        reset_period=reset_period,
        hard_quota=hard_quota,
        remaining_quota_must_not_be_inferred=True,
        source_path=source,
        source_sha256=file_sha256(source),
    )


def load_literary_pipeline_profile(path: Path) -> LiteraryPipelineProfile:
    source = Path(path).resolve()
    raw = _load_json(source, "Literary pipeline profile")
    schema_version = raw.get("schema_version")
    if schema_version not in {PROFILE_SCHEMA_VERSION, PROFILE_SCHEMA_VERSION_V2}:
        raise LiteraryPipelineProfileError("foreign Literary pipeline profile schema")
    expected_keys = {
        "schema_version",
        "profile_id",
        "chapter_cycle_profile",
        "design_doc",
        "usage_baseline",
        "public_stage_contract",
        "chapter_selection",
        "console_controls",
        "production_publish_enabled",
    }
    if schema_version == PROFILE_SCHEMA_VERSION_V2:
        expected_keys.add("structured_output_policy")
    _exact_keys(raw, expected_keys, "Literary pipeline profile")
    repository_root = source.parents[3]
    chapter_cycle_path = _resolve_profile_file(
        source_path=source,
        relative_value=raw["chapter_cycle_profile"],
        label="chapter_cycle_profile",
        repository_root=repository_root,
    )
    design_doc_path = _resolve_profile_file(
        source_path=source,
        relative_value=raw["design_doc"],
        label="design_doc",
        repository_root=repository_root,
    )
    usage_path = _resolve_profile_file(
        source_path=source,
        relative_value=raw["usage_baseline"],
        label="usage_baseline",
        repository_root=repository_root,
    )
    structured_output_policy = None
    if schema_version == PROFILE_SCHEMA_VERSION_V2:
        structured_output_policy_path = _resolve_profile_file(
            source_path=source,
            relative_value=raw["structured_output_policy"],
            label="structured_output_policy",
            repository_root=repository_root,
        )
        structured_output_policy = load_literary_structured_output_policy(
            structured_output_policy_path
        )
    raw_stages = _mapping(raw["public_stage_contract"], "public_stage_contract")
    if tuple(raw_stages) != PUBLIC_STAGE_IDS:
        raise LiteraryPipelineProfileError(
            "public stage contract order or field set drifted"
        )
    stages: dict[str, PublicStageBinding] = {}
    implementation_names: set[str] = set()
    for stage_id in PUBLIC_STAGE_IDS:
        row = _mapping(raw_stages[stage_id], f"public_stage_contract.{stage_id}")
        _exact_keys(
            row,
            {"enabled", "implementation_role", "implementation_stage_names"},
            f"public_stage_contract.{stage_id}",
        )
        enabled = _required_bool(row["enabled"], f"{stage_id}.enabled")
        role = row["implementation_role"]
        if role is not None:
            role = _required_string(role, f"{stage_id}.implementation_role")
        names = row["implementation_stage_names"]
        if (
            not isinstance(names, list)
            or any(not isinstance(name, str) or not name for name in names)
            or len(names) != len(set(names))
        ):
            raise LiteraryPipelineProfileError(
                f"{stage_id}.implementation_stage_names is invalid"
            )
        if enabled and (role is None or not names):
            raise LiteraryPipelineProfileError(
                f"enabled public stage {stage_id} lacks implementation"
            )
        if not enabled and (role is not None or names):
            raise LiteraryPipelineProfileError(
                f"disabled public stage {stage_id} must have no implementation"
            )
        overlap = implementation_names.intersection(names)
        if overlap:
            raise LiteraryPipelineProfileError(
                f"implementation stage is publicly double-classified: {sorted(overlap)}"
            )
        implementation_names.update(names)
        stages[stage_id] = PublicStageBinding(
            public_stage_id=stage_id,
            enabled=enabled,
            implementation_role=role,
            implementation_stage_names=tuple(names),
        )
    if stages["b1"].implementation_stage_names != ("b0", "b0_prior"):
        raise LiteraryPipelineProfileError("public B1 must alias b0 and b0_prior")
    if stages["b2"].enabled:
        raise LiteraryPipelineProfileError("B2 is not implemented in profile V1")

    chapter_selection = _mapping(raw["chapter_selection"], "chapter_selection")
    _exact_keys(
        chapter_selection,
        {
            "requires_contiguous_order",
            "requires_prefix_or_checkpoint_when_not_starting_at_first_chapter",
            "standalone_mode_is_non_authoritative",
        },
        "chapter_selection",
    )
    if any(_required_bool(value, f"chapter_selection.{key}") is not True for key, value in chapter_selection.items()):
        raise LiteraryPipelineProfileError("chapter selection safety cannot be weakened")

    console_controls = _mapping(raw["console_controls"], "console_controls")
    expected_console_controls = {
        "expose_model_selection",
        "expose_provider_selection",
        "expose_credential_handle_selection",
        "expose_token_caps",
        "expose_retry_caps",
        "expose_stop_after_chapter_count",
        "secret_values_in_profile_allowed",
    }
    if schema_version == PROFILE_SCHEMA_VERSION_V2:
        expected_console_controls.add("expose_structured_output_mode")
    _exact_keys(console_controls, expected_console_controls, "console_controls")
    parsed_controls = {
        key: _required_bool(value, f"console_controls.{key}")
        for key, value in console_controls.items()
    }
    if parsed_controls["secret_values_in_profile_allowed"]:
        raise LiteraryPipelineProfileError("pipeline profile cannot contain secrets")
    if raw["production_publish_enabled"] is not False:
        raise LiteraryPipelineProfileError("production publication is locked off")

    return LiteraryPipelineProfile(
        profile_id=_required_string(raw["profile_id"], "profile_id"),
        chapter_cycle_profile_path=chapter_cycle_path,
        design_doc_path=design_doc_path,
        usage_baseline=load_openai_usage_baseline(usage_path),
        public_stages=stages,
        chapter_selection={key: bool(value) for key, value in chapter_selection.items()},
        console_controls=parsed_controls,
        structured_output_policy=structured_output_policy,
        production_publish_enabled=False,
        profile_hash=canonical_hash(raw),
        source_path=source,
        source_sha256=file_sha256(source),
    )


def public_stage_plan(
    profile: LiteraryPipelineProfile, stage_plan: list[Mapping[str, Any]]
) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for row in stage_plan:
        projected = dict(row)
        projected["implementation_stage_name"] = row["stage_name"]
        projected["public_stage_name"] = profile.public_stage_name(
            str(row["stage_name"])
        )
        result.append(projected)
    return result


__all__ = [
    "LiteraryOpenAIUsageBaseline",
    "LiteraryPipelineProfile",
    "LiteraryPipelineProfileError",
    "PROFILE_SCHEMA_VERSION",
    "PROFILE_SCHEMA_VERSION_V2",
    "PUBLIC_STAGE_IDS",
    "PublicStageBinding",
    "load_literary_pipeline_profile",
    "load_openai_usage_baseline",
    "public_stage_plan",
]
