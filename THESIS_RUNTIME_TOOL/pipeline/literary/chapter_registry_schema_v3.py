from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Mapping

from pipeline.literary.checkpoint import canonical_hash


REGISTRY_SCHEMA_VERSION = "chapter_registry_v3"
ORIENTATION_SCHEMA_VERSION = "chapter_orientation_v3"
DELTA_SCHEMA_VERSION = "stable_registry_delta_v3"
AUDIT_SCHEMA_VERSION = "chapter_registry_audit_v2"
CANDIDATE_POLICY_VERSION = "registry_candidate_selection_v5_active_only"
ALIAS_SCOPE_POLICY_VERSION = "global_alias_scope_v2"
B2_RESCAN_POLICY_VERSION = "b2_candidate_rescan_v2_support_blocks"
VALIDATOR_VERSION = "chapter_registry_validator_v3"

PROMPT_IDS = {
    "b0": "literary_chapter_orient_v3",
    "b1": "literary_stable_registry_delta_v3_1",
    "auditor": "literary_registry_exception_audit_v2",
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
NAME_CLASSES = frozenset({"proper_name", "stable_nickname", "title_plus_name"})
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
CHECKLIST_CLASSES = frozenset(
    {
        "stable_named_referent",
        "important_unnamed_referent",
        "translation_sensitive_term",
    }
)
MODEL_TICKET_TYPES = frozenset(
    {
        "same_name_collision",
        "possible_alias",
        "important_unnamed_referent",
        "kind_conflict",
        "profile_conflict",
        "importance_review",
        "surface_class_review",
        "glossary_collision",
    }
)
CODE_TICKET_TYPES = frozenset(
    {
        "candidate_overflow",
        "unlocatable_surface",
        "missing_salient_surface",
        "alias_scope_review",
    }
)
TICKET_TYPES = MODEL_TICKET_TYPES | CODE_TICKET_TYPES
AUDIT_ACTIONS = frozenset(
    {
        "confirm_distinct_entity",
        "merge_as_alias",
        "create_unnamed_entity",
        "promote_global_alias",
        "confirm_distinct_glossary",
        "merge_glossary",
        "revise_profile",
        "defer_to_b2",
        "reject_noise",
        "remain_pending",
    }
)


class RegistryV3Error(RuntimeError):
    """Base error for the additive chapter-registry v3 path."""


class RegistryContractError(RegistryV3Error):
    """Raised when a request, response, or state violates the v3 contract."""


class RegistryStaleRevisionError(RegistryContractError):
    """Raised when a sequential B1 response targets an old working revision."""


class RegistryBudgetError(RegistryV3Error):
    """Raised before execution when a content-addressed cap is exceeded."""


class RegistryStoreError(RegistryV3Error):
    """Raised when a persisted generation fails integrity checks."""


class RegistryStaleParentError(RegistryStoreError):
    """Raised when a generation loses the compare-and-swap race."""


@dataclass(frozen=True)
class RunConfigV3:
    b0_model_id: str
    b0_reasoning_effort: str
    b0_temperature: float
    b0_seed: int
    b0_output_cap: int
    b1_model_id: str
    b1_reasoning_effort: str
    b1_temperature: float
    b1_seed: int
    b1_output_cap: int
    auditor_model_id: str
    auditor_reasoning_effort: str
    auditor_temperature: float
    auditor_seed: int
    auditor_output_cap: int
    b1_window_target_tokens: int
    b1_window_max_blocks: int
    context_only_tail_k: int
    recency_k: int
    candidate_card_count_cap: int
    candidate_card_token_cap: int
    candidate_packet_count_cap: int
    targeted_recall_call_cap: int
    ticket_component_cap: int
    auditor_call_cap: int
    auditor_input_token_cap: int
    auditor_output_token_cap: int
    ticket_share_warning: float
    ticket_share_halt: float
    component_share_warning: float
    component_share_halt: float
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
        positive = (
            "b0_output_cap",
            "b1_output_cap",
            "auditor_output_cap",
            "b1_window_target_tokens",
            "b1_window_max_blocks",
            "candidate_card_count_cap",
            "candidate_card_token_cap",
            "candidate_packet_count_cap",
            "targeted_recall_call_cap",
            "ticket_component_cap",
            "auditor_call_cap",
            "auditor_input_token_cap",
            "auditor_output_token_cap",
            "b0_input_cap",
            "b1_input_cap",
        )
        for name in positive:
            if isinstance(getattr(self, name), bool) or int(getattr(self, name)) <= 0:
                raise RegistryContractError(f"{name} must be a positive integer")
        if self.context_only_tail_k < 0 or self.recency_k < 0:
            raise RegistryContractError("tail and recency K must be non-negative")
        for role in ("b0", "b1", "auditor"):
            if not str(getattr(self, f"{role}_model_id")).strip():
                raise RegistryContractError(f"{role}_model_id must be non-empty")
            if float(getattr(self, f"{role}_temperature")) < 0:
                raise RegistryContractError(f"{role}_temperature must be non-negative")
            if not isinstance(getattr(self, f"{role}_seed"), int):
                raise RegistryContractError(f"{role}_seed must be an integer")
        pairs = (
            (self.ticket_share_warning, self.ticket_share_halt, "ticket share"),
            (self.component_share_warning, self.component_share_halt, "component share"),
        )
        for warning, halt, label in pairs:
            if not 0 <= float(warning) <= float(halt) <= 1:
                raise RegistryContractError(f"{label} thresholds must satisfy 0 <= warning <= halt <= 1")
        expected_roles = {"b0", "b1", "auditor"}
        if set(self.pricing_usd_per_million) != expected_roles:
            raise RegistryContractError("pricing must exact-cover b0/b1/auditor")
        if set(self.role_quota_gate_ids) != expected_roles:
            raise RegistryContractError("role_quota_gate_ids must exact-cover b0/b1/auditor")
        for role, prices in self.pricing_usd_per_million.items():
            if set(prices) != {"input", "cached_input", "output"}:
                raise RegistryContractError(f"{role} pricing shape is invalid")
            if any(value is not None and float(value) < 0 for value in prices.values()):
                raise RegistryContractError(f"{role} prices must be non-negative or null")
        expected_gate_fields = {
            "quota_bucket_id",
            "model_id",
            "rpm",
            "tpm",
            "rpd",
            "internal_utc_day_token_cap",
        }
        role_models = {
            "b0": self.b0_model_id,
            "b1": self.b1_model_id,
            "auditor": self.auditor_model_id,
        }
        for gate_id, raw in self.quota_gates.items():
            if not str(gate_id).strip() or set(raw) != expected_gate_fields:
                raise RegistryContractError("quota gate id/shape is invalid")
            bucket = str(raw["quota_bucket_id"]).strip()
            lowered = bucket.casefold()
            if not bucket or lowered.startswith("sk-") or lowered.startswith("aiza") or lowered.startswith("aq.a"):
                raise RegistryContractError("quota_bucket_id must be an opaque non-secret label")
            for limit_name in ("rpm", "tpm", "rpd"):
                value = raw[limit_name]
                if value is not None and (isinstance(value, bool) or not isinstance(value, int) or value <= 0):
                    raise RegistryContractError(f"{gate_id} {limit_name} must be positive or null")
            internal = raw["internal_utc_day_token_cap"]
            if isinstance(internal, bool) or not isinstance(internal, int) or internal <= 0:
                raise RegistryContractError("internal UTC-day cap must be positive")
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


@dataclass(frozen=True)
class RenderedRegistryRequestV3:
    role: str
    prompt_id: str
    prompt_sha256: str
    response_schema_hash: str
    chapter_id: str
    window_id: str | None
    parent_working_revision_hash: str | None
    sections: Mapping[str, Any]
    messages: tuple[Mapping[str, str], ...]
    request_fingerprint: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class PreparedRegistryGenerationV3:
    state_lineage_id: str
    generation_id: str
    parent_generation_id: str | None
    chapter_id: str
    source_manifest_hash: str
    payload: Mapping[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return dict(self.payload)


def response_json_schema(role: str) -> dict[str, Any]:
    """Return the full provider-neutral response schema for structured output."""

    string = {"type": "string", "minLength": 1}
    nullable_string = {"anyOf": [string, {"type": "null"}]}
    string_array = {"type": "array", "items": string, "uniqueItems": True}
    nullable_kind = {
        "anyOf": [
            {"type": "string", "enum": sorted(REFERENT_KINDS)},
            {"type": "null"},
        ]
    }
    if role == "b0":
        return {
            "type": "object",
            "additionalProperties": False,
            "required": ["gist", "narrator_hypotheses", "salient_registry_checklist"],
            "properties": {
                "gist": string,
                "narrator_hypotheses": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["surface", "note", "block_ids"],
                        "properties": {
                            "surface": nullable_string,
                            "note": string,
                            "block_ids": string_array,
                        },
                    },
                },
                "salient_registry_checklist": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["surface", "block_id", "checklist_class", "importance_note"],
                        "properties": {
                            "surface": string,
                            "block_id": string,
                            "checklist_class": {"type": "string", "enum": sorted(CHECKLIST_CLASSES)},
                            "importance_note": string,
                        },
                    },
                },
            },
        }
    if role == "b1":
        return {
            "type": "object",
            "additionalProperties": False,
            "required": ["new_entities", "new_glossary_items", "tickets"],
            "properties": {
                "new_entities": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["surface", "name_class", "referent_kind_claim", "short_description", "source_block_ids"],
                        "properties": {
                            "surface": string,
                            "name_class": {"type": "string", "enum": sorted(NAME_CLASSES)},
                            "referent_kind_claim": {"type": "string", "enum": sorted(REFERENT_KINDS)},
                            "short_description": string,
                            "source_block_ids": string_array,
                        },
                    },
                },
                "new_glossary_items": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["surface", "category_claim", "short_description", "source_block_ids"],
                        "properties": {
                            "surface": string,
                            "category_claim": {"type": "string", "enum": sorted(GLOSSARY_CATEGORIES)},
                            "short_description": string,
                            "source_block_ids": string_array,
                        },
                    },
                },
                "tickets": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["ticket_type", "surface", "source_block_ids", "candidate_entity_ids", "candidate_glossary_ids", "referent_kind_claim", "proposed_short_description", "reason"],
                        "properties": {
                            "ticket_type": {"type": "string", "enum": sorted(MODEL_TICKET_TYPES)},
                            "surface": nullable_string,
                            "source_block_ids": string_array,
                            "candidate_entity_ids": string_array,
                            "candidate_glossary_ids": string_array,
                            "referent_kind_claim": nullable_kind,
                            "proposed_short_description": nullable_string,
                            "reason": string,
                        },
                    },
                },
            },
        }
    if role == "auditor":
        return {
            "type": "object",
            "additionalProperties": False,
            "required": ["ticket_dispositions"],
            "properties": {
                "ticket_dispositions": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["ticket_id", "action", "source_entity_id", "target_entity_id", "source_glossary_id", "target_glossary_id", "resolved_referent_kind", "revised_identity_summary", "name_class", "resolution_note"],
                        "properties": {
                            "ticket_id": string,
                            "action": {"type": "string", "enum": sorted(AUDIT_ACTIONS)},
                            "source_entity_id": nullable_string,
                            "target_entity_id": nullable_string,
                            "source_glossary_id": nullable_string,
                            "target_glossary_id": nullable_string,
                            "resolved_referent_kind": nullable_kind,
                            "revised_identity_summary": nullable_string,
                            "name_class": {
                                "anyOf": [
                                    {"type": "string", "enum": sorted(NAME_CLASSES)},
                                    {"type": "null"},
                                ]
                            },
                            "resolution_note": string,
                        },
                    },
                }
            },
        }
    raise RegistryContractError(f"unknown registry role: {role}")


__all__ = [
    "ALIAS_SCOPE_POLICY_VERSION",
    "AUDIT_ACTIONS",
    "AUDIT_SCHEMA_VERSION",
    "B2_RESCAN_POLICY_VERSION",
    "CANDIDATE_POLICY_VERSION",
    "CHECKLIST_CLASSES",
    "CODE_TICKET_TYPES",
    "DELTA_SCHEMA_VERSION",
    "GLOSSARY_CATEGORIES",
    "MODEL_TICKET_TYPES",
    "NAME_CLASSES",
    "ORIENTATION_SCHEMA_VERSION",
    "PROMPT_IDS",
    "PreparedRegistryGenerationV3",
    "REFERENT_KINDS",
    "REGISTRY_SCHEMA_VERSION",
    "RegistryBudgetError",
    "RegistryContractError",
    "RegistryStaleParentError",
    "RegistryStaleRevisionError",
    "RegistryStoreError",
    "RenderedRegistryRequestV3",
    "RunConfigV3",
    "TICKET_TYPES",
    "VALIDATOR_VERSION",
    "response_json_schema",
]
