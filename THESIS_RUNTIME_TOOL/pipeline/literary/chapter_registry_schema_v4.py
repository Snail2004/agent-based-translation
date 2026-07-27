from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Mapping

from pipeline.literary.checkpoint import canonical_hash
from pipeline.literary.chapter_registry_prompts_v4 import PROMPT_IDS


REGISTRY_SCHEMA_VERSION = "chapter_registry_v4"
ORIENTATION_SCHEMA_VERSION = "chapter_orientation_v4_1"
DELTA_SCHEMA_VERSION = "stable_registry_delta_v4"
AUDIT_SCHEMA_VERSION = "chapter_registry_audit_v4"
ATTENTION_LEDGER_VERSION = "b0_advisory_attention_v1"
CANDIDATE_POLICY_VERSION = "registry_candidate_selection_v5_prejoined"
ALIAS_SCOPE_POLICY_VERSION = "global_alias_scope_v2"
B2_RESCAN_POLICY_VERSION = "b2_candidate_rescan_v3_local_support"
VALIDATOR_VERSION = "chapter_registry_validator_v4_1_b0"

NARRATIVE_CONTEXT_MODES = frozenset(
    {
        "first_person",
        "third_person_external",
        "second_person",
        "mixed_or_nested",
        "documentary_or_epistolary",
        "uncertain",
    }
)

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
REFERENTIAL_GENDERS = frozenset({"masculine", "feminine", "neutral", "mixed"})
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
SURFACE_UPDATE_KINDS = frozenset({"global_name_alias", "block_local_reference"})
MODEL_TICKET_TYPES = frozenset(
    {
        "same_name_collision",
        "possible_alias",
        "important_unnamed_referent",
        "kind_conflict",
        "profile_conflict",
        "profile_enrichment",
        "surface_class_review",
        "glossary_collision",
    }
)
CODE_TICKET_TYPES = frozenset(
    {
        "candidate_overflow",
        "unlocatable_surface",
        "alias_scope_review",
        "invalid_source_support",
        "ambiguous_new_subject",
    }
)
TICKET_TYPES = MODEL_TICKET_TYPES | CODE_TICKET_TYPES
AUDIT_ACTIONS = frozenset(
    {
        "confirm_distinct_entity",
        "merge_as_alias",
        "create_unnamed_entity",
        "promote_global_alias",
        "confirm_block_local_reference",
        "confirm_distinct_glossary",
        "merge_glossary",
        "revise_profile",
        "defer_to_b2",
        "reject_noise",
        "remain_pending",
    }
)
AUDIT_ALLOWED_ACTIONS: Mapping[str, frozenset[str]] = {
    "same_name_collision": frozenset(
        {"confirm_distinct_entity", "merge_as_alias", "remain_pending"}
    ),
    "possible_alias": frozenset(
        {"confirm_distinct_entity", "merge_as_alias", "remain_pending"}
    ),
    "important_unnamed_referent": frozenset(
        {"create_unnamed_entity", "defer_to_b2", "reject_noise", "remain_pending"}
    ),
    "kind_conflict": frozenset(
        {"revise_profile", "confirm_distinct_entity", "remain_pending"}
    ),
    "profile_conflict": frozenset(
        {"revise_profile", "confirm_distinct_entity", "remain_pending"}
    ),
    "profile_enrichment": frozenset({"revise_profile", "reject_noise", "remain_pending"}),
    "surface_class_review": frozenset(
        {
            "promote_global_alias",
            "confirm_block_local_reference",
            "defer_to_b2",
            "reject_noise",
            "remain_pending",
        }
    ),
    "glossary_collision": frozenset(
        {"confirm_distinct_glossary", "merge_glossary", "reject_noise", "remain_pending"}
    ),
    "candidate_overflow": frozenset({"remain_pending"}),
    "unlocatable_surface": frozenset({"reject_noise", "remain_pending"}),
    "alias_scope_review": frozenset({"defer_to_b2", "reject_noise", "remain_pending"}),
    "invalid_source_support": frozenset({"reject_noise", "remain_pending"}),
    "ambiguous_new_subject": frozenset({"remain_pending"}),
}


class RegistryV4Error(RuntimeError):
    """Base error for the additive chapter-registry v4 path."""


class RegistryContractError(RegistryV4Error):
    """Raised when a request, response, or state violates the v4 contract."""


class RegistryStaleRevisionError(RegistryContractError):
    """Raised when a sequential response targets an old working revision."""


class RegistryBudgetError(RegistryV4Error):
    """Raised before execution when a content-addressed cap is exceeded."""


class RegistryStoreError(RegistryV4Error):
    """Raised when a persisted generation fails integrity checks."""


class RegistryStaleParentError(RegistryStoreError):
    """Raised when a generation loses the compare-and-swap race."""


@dataclass(frozen=True)
class RunConfigV4:
    b0_model_id: str
    b0_reasoning_effort: str
    b0_temperature: float
    b0_seed: int
    b0_output_token_cap: int
    b1_model_id: str
    b1_reasoning_effort: str
    b1_temperature: float
    b1_seed: int
    b1_output_token_cap: int
    auditor_model_id: str
    auditor_reasoning_effort: str
    auditor_temperature: float
    auditor_seed: int
    auditor_output_token_cap: int
    b0_attention_context_mode: str
    b0_input_token_cap: int
    b1_input_token_cap: int
    active_window_source_token_target: int
    active_window_max_blocks: int
    preceding_tail_block_cap: int
    attention_packet_cap_per_window: int
    known_surface_packet_cap_per_window: int
    candidate_cards_total_cap_per_window: int
    candidate_context_token_cap: int
    recency_neighbor_distance_blocks: int
    candidate_overflow_policy: str
    auditor_tickets_per_component_cap: int
    auditor_calls_per_chapter_cap: int
    auditor_neighbor_blocks_each_side: int
    auditor_input_token_cap: int
    provider_quota_policy_hash: str
    prompt_versions: Mapping[str, str]
    schema_versions: Mapping[str, str]
    validator_version: str
    policy_versions: Mapping[str, str]

    def __post_init__(self) -> None:
        positive = (
            "b0_output_token_cap",
            "b1_output_token_cap",
            "auditor_output_token_cap",
            "b0_input_token_cap",
            "b1_input_token_cap",
            "active_window_source_token_target",
            "active_window_max_blocks",
            "attention_packet_cap_per_window",
            "known_surface_packet_cap_per_window",
            "candidate_cards_total_cap_per_window",
            "candidate_context_token_cap",
            "auditor_tickets_per_component_cap",
            "auditor_calls_per_chapter_cap",
            "auditor_input_token_cap",
        )
        for name in positive:
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise RegistryContractError(f"{name} must be a positive integer")
        non_negative = (
            "preceding_tail_block_cap",
            "recency_neighbor_distance_blocks",
            "auditor_neighbor_blocks_each_side",
        )
        for name in non_negative:
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise RegistryContractError(f"{name} must be a non-negative integer")
        if self.b0_attention_context_mode not in {"advisory_active_window", "off"}:
            raise RegistryContractError("unsupported B0 attention context mode")
        if self.candidate_overflow_policy not in {"halt", "ticket"}:
            raise RegistryContractError("unsupported candidate overflow policy")
        for role in ("b0", "b1", "auditor"):
            if not str(getattr(self, f"{role}_model_id")).strip():
                raise RegistryContractError(f"{role}_model_id must be non-empty")
            if float(getattr(self, f"{role}_temperature")) < 0:
                raise RegistryContractError(f"{role}_temperature must be non-negative")
            if not isinstance(getattr(self, f"{role}_seed"), int):
                raise RegistryContractError(f"{role}_seed must be an integer")
        if not str(self.provider_quota_policy_hash).strip():
            raise RegistryContractError("provider_quota_policy_hash must be non-empty")

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @property
    def config_hash(self) -> str:
        return canonical_hash(self.to_dict())


@dataclass(frozen=True)
class RenderedRegistryRequestV4:
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
class PreparedRegistryGenerationV4:
    state_lineage_id: str
    generation_id: str
    parent_generation_id: str | None
    chapter_id: str
    source_manifest_hash: str
    payload: Mapping[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return dict(self.payload)


def _nullable(schema: Mapping[str, Any]) -> dict[str, Any]:
    return {"anyOf": [dict(schema), {"type": "null"}]}


def response_json_schema(role: str) -> dict[str, Any]:
    """Return the complete provider-neutral structured-output schema."""

    string = {"type": "string", "minLength": 1}
    nullable_string = _nullable(string)
    string_array = {"type": "array", "items": string, "uniqueItems": True}
    name_class = {"type": "string", "enum": sorted(NAME_CLASSES)}
    nullable_name_class = _nullable(name_class)
    kind = {"type": "string", "enum": sorted(REFERENT_KINDS)}
    nullable_kind = _nullable(kind)
    gender = {"type": "string", "enum": sorted(REFERENTIAL_GENDERS)}
    nullable_gender = _nullable(gender)
    gender_claim = {
        "type": "object",
        "additionalProperties": False,
        "required": ["value", "support_block_ids"],
        "properties": {"value": gender, "support_block_ids": string_array},
    }
    surface_update_base = {
        "type": "object",
        "additionalProperties": False,
        "required": ["update_kind", "surface", "name_class", "source_block_ids", "reason"],
        "properties": {
            "update_kind": {"type": "string", "enum": sorted(SURFACE_UPDATE_KINDS)},
            "surface": string,
            "name_class": nullable_name_class,
            "source_block_ids": string_array,
            "reason": string,
        },
    }
    if role == "b0":
        return {
            "type": "object",
            "additionalProperties": False,
            "required": ["orientation_draft", "narrative_context", "attention_items"],
            "properties": {
                "orientation_draft": string,
                "narrative_context": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["mode", "note", "support_block_ids"],
                    "properties": {
                        "mode": {
                            "type": "string",
                            "enum": sorted(NARRATIVE_CONTEXT_MODES),
                        },
                        "note": string,
                        "support_block_ids": {
                            "type": "array",
                            "items": string,
                            "minItems": 1,
                            "maxItems": 4,
                            "uniqueItems": True,
                        },
                    },
                },
                "attention_items": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["surface", "source_block_ids", "why_noticed"],
                        "properties": {
                            "surface": string,
                            "source_block_ids": string_array,
                            "why_noticed": string,
                        },
                    },
                },
            },
        }
    if role == "b1":
        top_update = {
            **surface_update_base,
            "required": [
                "update_kind",
                "surface",
                "target_entity_id",
                "name_class",
                "source_block_ids",
                "reason",
            ],
            "properties": {
                **surface_update_base["properties"],
                "target_entity_id": string,
            },
        }
        return {
            "type": "object",
            "additionalProperties": False,
            "required": ["new_entities", "new_glossary_items", "surface_updates", "tickets"],
            "properties": {
                "new_entities": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "additionalProperties": False,
                        "required": [
                            "surface",
                            "name_class",
                            "referent_kind_claim",
                            "identity_summary",
                            "source_block_ids",
                            "initial_surface_updates",
                        ],
                        "properties": {
                            "surface": string,
                            "name_class": nullable_name_class,
                            "referent_kind_claim": kind,
                            "identity_summary": string,
                            "referential_gender_claim": _nullable(gender_claim),
                            "source_block_ids": string_array,
                            "initial_surface_updates": {
                                "type": "array",
                                "items": surface_update_base,
                            },
                        },
                    },
                },
                "new_glossary_items": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "additionalProperties": False,
                        "required": [
                            "surface",
                            "category_claim",
                            "short_description",
                            "source_block_ids",
                        ],
                        "properties": {
                            "surface": string,
                            "category_claim": {
                                "type": "string",
                                "enum": sorted(GLOSSARY_CATEGORIES),
                            },
                            "short_description": string,
                            "source_block_ids": string_array,
                        },
                    },
                },
                "surface_updates": {"type": "array", "items": top_update},
                "tickets": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "additionalProperties": False,
                        "required": [
                            "ticket_type",
                            "surface",
                            "source_block_ids",
                            "candidate_entity_ids",
                            "candidate_glossary_ids",
                            "referent_kind_claim",
                            "proposed_identity_summary",
                            "proposed_referential_gender",
                            "reason",
                        ],
                        "properties": {
                            "ticket_type": {
                                "type": "string",
                                "enum": sorted(MODEL_TICKET_TYPES),
                            },
                            "surface": nullable_string,
                            "source_block_ids": string_array,
                            "candidate_entity_ids": string_array,
                            "candidate_glossary_ids": string_array,
                            "referent_kind_claim": nullable_kind,
                            "proposed_identity_summary": nullable_string,
                            "proposed_referential_gender": nullable_gender,
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
            "required": ["ticket_dispositions", "profile_revisions"],
            "properties": {
                "ticket_dispositions": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "additionalProperties": False,
                        "required": [
                            "ticket_id",
                            "action",
                            "source_entity_id",
                            "target_entity_id",
                            "source_glossary_id",
                            "target_glossary_id",
                            "resolved_referent_kind",
                            "name_class",
                            "valid_block_ids",
                            "resolution_note",
                        ],
                        "properties": {
                            "ticket_id": string,
                            "action": {"type": "string", "enum": sorted(AUDIT_ACTIONS)},
                            "source_entity_id": nullable_string,
                            "target_entity_id": nullable_string,
                            "source_glossary_id": nullable_string,
                            "target_glossary_id": nullable_string,
                            "resolved_referent_kind": nullable_kind,
                            "name_class": nullable_name_class,
                            "valid_block_ids": string_array,
                            "resolution_note": string,
                        },
                    },
                },
                "profile_revisions": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "additionalProperties": False,
                        "required": [
                            "target_entity_id",
                            "source_ticket_ids",
                            "referent_kind_update",
                            "identity_summary_update",
                            "referential_gender_update",
                            "resolution_note",
                        ],
                        "properties": {
                            "target_entity_id": string,
                            "source_ticket_ids": string_array,
                            "referent_kind_update": nullable_kind,
                            "identity_summary_update": nullable_string,
                            "referential_gender_update": nullable_gender,
                            "resolution_note": string,
                        },
                    },
                },
            },
        }
    raise RegistryContractError(f"unknown registry v4 role: {role}")


__all__ = [
    "ALIAS_SCOPE_POLICY_VERSION",
    "ATTENTION_LEDGER_VERSION",
    "AUDIT_ACTIONS",
    "AUDIT_ALLOWED_ACTIONS",
    "AUDIT_SCHEMA_VERSION",
    "B2_RESCAN_POLICY_VERSION",
    "CANDIDATE_POLICY_VERSION",
    "CODE_TICKET_TYPES",
    "DELTA_SCHEMA_VERSION",
    "GLOSSARY_CATEGORIES",
    "MODEL_TICKET_TYPES",
    "NAME_CLASSES",
    "ORIENTATION_SCHEMA_VERSION",
    "NARRATIVE_CONTEXT_MODES",
    "PROMPT_IDS",
    "PreparedRegistryGenerationV4",
    "REFERENTIAL_GENDERS",
    "REFERENT_KINDS",
    "REGISTRY_SCHEMA_VERSION",
    "RegistryBudgetError",
    "RegistryContractError",
    "RegistryStaleParentError",
    "RegistryStaleRevisionError",
    "RegistryStoreError",
    "RenderedRegistryRequestV4",
    "RunConfigV4",
    "SURFACE_UPDATE_KINDS",
    "TICKET_TYPES",
    "VALIDATOR_VERSION",
    "response_json_schema",
]
