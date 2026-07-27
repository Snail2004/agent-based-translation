from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Mapping

from pipeline.literary.checkpoint import canonical_hash


REGISTRY_SCHEMA_VERSION = "chapter_registry_v2"
ORIENTATION_SCHEMA_VERSION = "chapter_orientation_v2"
DELTA_SCHEMA_VERSION = "registry_delta_v2"
AUDIT_SCHEMA_VERSION = "chapter_audit_decision_v1"
CANDIDATE_POLICY_VERSION = "registry_candidate_selection_v3_prejoined"
CLEAN_POLICY_VERSION = "clean_commit_eligibility_v1"
B2_RESCAN_POLICY_VERSION = "b2_candidate_rescan_v1"
ALIAS_SCOPE_POLICY_VERSION = "global_alias_scope_v1"

PROMPT_IDS = {
    "b0": "literary_chapter_orient_v2",
    "b1": "literary_registry_delta_v2_2",
    "auditor": "literary_registry_audit_v1_1",
}

REFERENT_KINDS = frozenset(
    {
        "person",
        "animal",
        "nonhuman_character",
        "group_reference",
        "place",
        "object",
        "unknown",
    }
)
MENTION_TYPES = frozenset({"name", "nickname", "title"})
ALIAS_TYPES = MENTION_TYPES
GLOSSARY_CATEGORIES = frozenset(
    {
        "cultural_term",
        "technical_term",
        "place_name",
        "object_name",
        "institution_name",
        "other",
    }
)
TICKET_TYPES = frozenset(
    {
        "profile_description_conflict",
        "same_name_collision",
        "alias_collision",
        "kind_conflict",
        "ambiguous_local_binding",
        "unregistered_local_referent",
        "importance_review",
        "possible_duplicate",
        "glossary_collision",
        "unlocatable_surface",
        "candidate_overflow",
        "missing_salient_surface",
        "surface_scope_review",
    }
)

DELTA_LISTS = (
    "new_entities",
    "new_aliases",
    "new_glossary_items",
    "local_bindings",
    "tickets",
)
AUDIT_LISTS = (
    "entity_dispositions",
    "alias_dispositions",
    "glossary_dispositions",
    "local_binding_dispositions",
    "ticket_dispositions",
    "profile_revisions",
)


class RegistryV2Error(RuntimeError):
    """Base error for the additive chapter-registry v2 path."""


class RegistryContractError(RegistryV2Error):
    """Raised when a request or model artifact violates the v2 contract."""


class RegistryStaleRevisionError(RegistryContractError):
    """Raised when a sequential B1 response targets an old working revision."""


class RegistryStoreError(RegistryV2Error):
    """Raised when a persisted v2 generation fails integrity checks."""


class RegistryStaleParentError(RegistryStoreError):
    """Raised when a chapter generation loses the compare-and-swap race."""


class RegistryBudgetError(RegistryV2Error):
    """Raised before execution when a locked cap cannot be honored."""


@dataclass(frozen=True)
class RunConfigV2:
    b0_model_id: str
    b0_reasoning_effort: str
    b0_temperature: float
    b0_seed: int
    b0_verbosity: str | None
    b0_output_cap: int
    b1_model_id: str
    b1_reasoning_effort: str
    b1_temperature: float
    b1_seed: int
    b1_verbosity: str | None
    b1_output_cap: int
    auditor_model_id: str
    auditor_reasoning_effort: str
    auditor_temperature: float
    auditor_seed: int
    auditor_verbosity: str | None
    auditor_output_cap: int
    b1_window_target_tokens: int
    b1_window_max_blocks: int
    context_only_tail_k: int
    recency_k: int
    candidate_card_count_cap: int
    candidate_card_token_cap: int
    targeted_recall_call_cap: int
    auditor_component_cap: int
    auditor_input_token_cap: int
    auditor_exception_share_cap: float
    b0_input_cap: int
    b1_input_cap: int
    pricing_usd_per_million: Mapping[str, Mapping[str, float | None]]
    quota_gates: Mapping[str, Mapping[str, Any]]
    role_quota_gate_ids: Mapping[str, tuple[str, ...]]
    prompt_versions: Mapping[str, str]
    schema_versions: Mapping[str, str]
    validator_version: str
    policy_versions: Mapping[str, str]

    def __post_init__(self) -> None:
        positive_ints = (
            "b0_output_cap",
            "b1_output_cap",
            "auditor_output_cap",
            "b1_window_target_tokens",
            "b1_window_max_blocks",
            "candidate_card_count_cap",
            "candidate_card_token_cap",
            "targeted_recall_call_cap",
            "auditor_component_cap",
            "auditor_input_token_cap",
            "b0_input_cap",
            "b1_input_cap",
        )
        for field_name in positive_ints:
            if int(getattr(self, field_name)) <= 0:
                raise RegistryContractError(f"{field_name} must be positive")
        if self.context_only_tail_k < 0 or self.recency_k < 0:
            raise RegistryContractError("tail and recency K must be non-negative")
        for role in ("b0", "b1", "auditor"):
            temperature = float(getattr(self, f"{role}_temperature"))
            seed = getattr(self, f"{role}_seed")
            verbosity = getattr(self, f"{role}_verbosity")
            if temperature < 0:
                raise RegistryContractError(f"{role}_temperature must be non-negative")
            if not isinstance(seed, int):
                raise RegistryContractError(f"{role}_seed must be an integer")
            if verbosity is not None and not str(verbosity).strip():
                raise RegistryContractError(f"{role}_verbosity must be non-empty or null")
        if not 0.0 <= float(self.auditor_exception_share_cap) <= 1.0:
            raise RegistryContractError("auditor_exception_share_cap must be in [0, 1]")
        expected_roles = {"b0", "b1", "auditor"}
        if set(self.pricing_usd_per_million) != expected_roles:
            raise RegistryContractError("pricing must exact-cover b0/b1/auditor")
        for role, pricing in self.pricing_usd_per_million.items():
            if set(pricing) != {"input", "cached_input", "output"}:
                raise RegistryContractError(f"{role} pricing shape is invalid")
            for price_name, value in pricing.items():
                if value is not None and float(value) < 0:
                    raise RegistryContractError(
                        f"{role} {price_name} price must be non-negative or null"
                    )
        if set(self.role_quota_gate_ids) != expected_roles:
            raise RegistryContractError("role_quota_gate_ids must exact-cover b0/b1/auditor")
        role_models = {
            "b0": self.b0_model_id,
            "b1": self.b1_model_id,
            "auditor": self.auditor_model_id,
        }
        expected_gate_fields = {
            "quota_bucket_id",
            "model_id",
            "rpm",
            "tpm",
            "rpd",
            "internal_utc_day_token_cap",
        }
        for gate_id, raw_gate in self.quota_gates.items():
            gate_name = str(gate_id).strip()
            if not gate_name or set(raw_gate) != expected_gate_fields:
                raise RegistryContractError("quota gate id/shape is invalid")
            bucket = str(raw_gate["quota_bucket_id"]).strip()
            lowered = bucket.casefold()
            if (
                not bucket
                or lowered.startswith("sk" + "-")
                or lowered.startswith("aiza")
                or lowered.startswith("aq.a")
            ):
                raise RegistryContractError("quota_bucket_id must be an opaque non-secret label")
            if not str(raw_gate["model_id"]).strip():
                raise RegistryContractError("quota gate model_id must be non-empty")
            for limit_name in ("rpm", "tpm", "rpd"):
                limit = raw_gate[limit_name]
                if limit is not None and (not isinstance(limit, int) or limit <= 0):
                    raise RegistryContractError(
                        f"quota gate {gate_name} {limit_name} must be positive or null"
                    )
            internal = raw_gate["internal_utc_day_token_cap"]
            if not isinstance(internal, int) or internal <= 0:
                raise RegistryContractError("quota gate internal UTC-day cap must be positive")
        for role, gate_ids in self.role_quota_gate_ids.items():
            if not gate_ids:
                raise RegistryContractError(f"{role} must have at least one quota gate")
            for gate_id in gate_ids:
                if gate_id not in self.quota_gates:
                    raise RegistryContractError(f"{role} references unknown quota gate {gate_id}")
                if self.quota_gates[gate_id]["model_id"] != role_models[role]:
                    raise RegistryContractError(f"{role} quota gate model mismatch")

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @property
    def config_hash(self) -> str:
        return canonical_hash(self.to_dict())

    @property
    def unknown_provider_limit_gate_ids(self) -> tuple[str, ...]:
        return tuple(
            sorted(
                gate_id
                for gate_id, gate in self.quota_gates.items()
                if any(gate[name] is None for name in ("rpm", "tpm", "rpd"))
            )
        )


@dataclass(frozen=True)
class RenderedRegistryRequestV2:
    role: str
    prompt_id: str
    prompt_sha256: str
    chapter_id: str
    window_id: str | None
    parent_working_revision_hash: str | None
    sections: Mapping[str, Any]
    messages: tuple[Mapping[str, str], ...]
    request_fingerprint: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class PreparedRegistryGenerationV2:
    state_lineage_id: str
    generation_id: str
    parent_generation_id: str | None
    chapter_id: str
    source_manifest_hash: str
    payload: Mapping[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return dict(self.payload)


__all__ = [
    "ALIAS_TYPES",
    "ALIAS_SCOPE_POLICY_VERSION",
    "AUDIT_LISTS",
    "AUDIT_SCHEMA_VERSION",
    "B2_RESCAN_POLICY_VERSION",
    "CANDIDATE_POLICY_VERSION",
    "CLEAN_POLICY_VERSION",
    "DELTA_LISTS",
    "DELTA_SCHEMA_VERSION",
    "GLOSSARY_CATEGORIES",
    "MENTION_TYPES",
    "ORIENTATION_SCHEMA_VERSION",
    "PROMPT_IDS",
    "PreparedRegistryGenerationV2",
    "REFERENT_KINDS",
    "REGISTRY_SCHEMA_VERSION",
    "RegistryBudgetError",
    "RegistryContractError",
    "RegistryStaleParentError",
    "RegistryStaleRevisionError",
    "RegistryStoreError",
    "RenderedRegistryRequestV2",
    "RunConfigV2",
    "TICKET_TYPES",
]
