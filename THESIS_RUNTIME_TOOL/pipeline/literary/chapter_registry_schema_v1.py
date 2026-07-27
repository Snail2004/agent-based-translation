"""Typed, JSON-safe contracts for the chapter-oriented literary registry.

The records in this module are intentionally semantic-free infrastructure.
Models propose identity operations; code validates, locates, mints identifiers,
stages revisions, and publishes one append-only generation per chapter.
"""

from __future__ import annotations

from dataclasses import dataclass, fields
from typing import Any, Literal, Mapping, TypeAlias

from pipeline.literary.source_anchor import SourceAnchor


ReferentKind: TypeAlias = Literal[
    "person",
    "animal",
    "nonhuman_character",
    "place",
    "group_reference",
    "object",
    "unknown",
]
IdentityReferentKind: TypeAlias = Literal[
    "person", "animal", "nonhuman_character", "unknown"
]
RoutingDisposition: TypeAlias = Literal[
    "identity_registry", "noncharacter_index", "pending_kind"
]
BindingStatus: TypeAlias = Literal[
    "attached", "pending", "noncharacter", "unresolved"
]

REFERENT_KINDS = frozenset(
    {
        "person",
        "animal",
        "nonhuman_character",
        "place",
        "group_reference",
        "object",
        "unknown",
    }
)
IDENTITY_KINDS = frozenset({"person", "animal", "nonhuman_character"})
NONCHARACTER_KINDS = frozenset({"place", "group_reference", "object"})
MENTION_TYPES = frozenset({"name", "nickname", "descriptor"})
IDENTITY_OPERATIONS = frozenset(
    {"reinforce_existing", "add_alias", "propose_new_entity", "pending"}
)
IDENTITY_REASON_CODES = frozenset(
    {
        "direct_name",
        "explicit_renaming",
        "title_or_epithet",
        "pronoun_or_metonymy",
        "no_candidate",
        "ambiguous",
        "kind_conflict",
        "other",
    }
)
PENDING_REASONS = frozenset(
    {
        "no_candidate",
        "ambiguous_candidates",
        "conflicting_kind",
        "insufficient_evidence",
        "reconcile_cap",
    }
)
DECISION_FIELDS = frozenset(
    {"identity_binding", "alias_binding", "referent_kind"}
)
EVIDENCE_CLASSES = frozenset(
    {
        "candidate_entities",
        "entity_history",
        "pending_history",
        "wider_source_context",
    }
)


class RegistryContractError(ValueError):
    """Raised when a chapter-registry payload violates its closed contract."""


def _required(value: str, label: str) -> str:
    normalized = str(value or "").strip()
    if not normalized:
        raise RegistryContractError(f"{label} must be non-empty")
    return normalized


def _json_value(value: Any) -> Any:
    if isinstance(value, JsonRecord):
        return value.to_dict()
    if isinstance(value, SourceAnchor):
        return value.to_dict()
    if isinstance(value, tuple):
        return [_json_value(item) for item in value]
    if isinstance(value, Mapping):
        return {str(key): _json_value(item) for key, item in value.items()}
    return value


class JsonRecord:
    def to_dict(self) -> dict[str, Any]:
        return {
            item.name: _json_value(getattr(self, item.name))
            for item in fields(self)
        }


@dataclass(frozen=True, slots=True, order=True)
class StoryPositionV1(JsonRecord):
    chapter_order: int
    block_order: int
    char_offset: int

    def __post_init__(self) -> None:
        if min(self.chapter_order, self.block_order, self.char_offset) < 0:
            raise RegistryContractError("story positions must be non-negative")


@dataclass(frozen=True, slots=True)
class IdentityProposalV1(JsonRecord):
    operation: Literal[
        "reinforce_existing", "add_alias", "propose_new_entity", "pending"
    ]
    target_entity_id: str | None
    canonical_surface_candidate: str | None
    alias_surface: str | None
    reason_code: str
    binding_evidence_quote: str | None
    binding_anchor_text: str | None
    binding_block_id: str | None
    binding_occurrence_hint: int | None
    retrieval_trace_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.operation not in IDENTITY_OPERATIONS:
            raise RegistryContractError(f"unknown identity operation: {self.operation}")
        if self.reason_code not in IDENTITY_REASON_CODES:
            raise RegistryContractError(
                f"unknown identity reason_code: {self.reason_code}"
            )
        if self.operation in {"reinforce_existing", "add_alias"}:
            _required(self.target_entity_id or "", "target_entity_id")
            _required(self.binding_evidence_quote or "", "binding_evidence_quote")
            _required(self.binding_anchor_text or "", "binding_anchor_text")
            _required(self.binding_block_id or "", "binding_block_id")
        elif self.target_entity_id is not None:
            raise RegistryContractError(
                "new/pending identity proposal cannot carry target_entity_id"
            )
        if self.operation == "add_alias":
            _required(self.alias_surface or "", "alias_surface")
        elif self.alias_surface is not None:
            raise RegistryContractError(
                "only add_alias may carry alias_surface"
            )
        if self.operation in {"pending", "propose_new_entity"} and any(
            value is not None
            for value in (
                self.binding_evidence_quote,
                self.binding_anchor_text,
                self.binding_block_id,
                self.binding_occurrence_hint,
            )
        ):
            raise RegistryContractError(
                "pending/new proposal cannot carry binding evidence fields"
            )
        if self.binding_occurrence_hint is not None and self.binding_occurrence_hint < 1:
            raise RegistryContractError("binding_occurrence_hint must be 1-based")


@dataclass(frozen=True, slots=True)
class ModelContextRequestDraftV1(JsonRecord):
    mention_index: int
    decision_field: Literal["identity_binding", "alias_binding", "referent_kind"]
    surface: str
    reason: str
    block_id: str
    needed_evidence: tuple[
        Literal[
            "candidate_entities",
            "entity_history",
            "pending_history",
            "wider_source_context",
        ],
        ...,
    ]

    def __post_init__(self) -> None:
        if self.mention_index < 0:
            raise RegistryContractError("mention_index must be non-negative")
        if self.decision_field not in DECISION_FIELDS:
            raise RegistryContractError(
                f"unknown context-request decision field: {self.decision_field}"
            )
        _required(self.surface, "context-request surface")
        _required(self.reason, "context-request reason")
        _required(self.block_id, "context-request block_id")
        if not self.needed_evidence:
            raise RegistryContractError("context request needs at least one evidence class")
        if len(set(self.needed_evidence)) != len(self.needed_evidence):
            raise RegistryContractError("context request repeats an evidence class")
        if not set(self.needed_evidence) <= EVIDENCE_CLASSES:
            raise RegistryContractError("context request has an unknown evidence class")


@dataclass(frozen=True, slots=True)
class BrokerContextRequestV1(JsonRecord):
    occurrence_id: str
    decision_field: Literal["identity_binding", "alias_binding", "referent_kind"]
    surface: str
    reason: str
    as_of_position: StoryPositionV1
    needed_evidence: tuple[str, ...]

    def __post_init__(self) -> None:
        _required(self.occurrence_id, "broker occurrence_id")
        if self.decision_field not in DECISION_FIELDS:
            raise RegistryContractError(
                f"unknown broker decision field: {self.decision_field}"
            )
        _required(self.surface, "broker surface")
        _required(self.reason, "broker reason")
        if not self.needed_evidence:
            raise RegistryContractError("broker request needs evidence classes")
        if len(set(self.needed_evidence)) != len(self.needed_evidence):
            raise RegistryContractError("broker request repeats an evidence class")
        if not set(self.needed_evidence) <= EVIDENCE_CLASSES:
            raise RegistryContractError("broker request has an unknown evidence class")


@dataclass(frozen=True, slots=True)
class OccurrenceRecordV1(JsonRecord):
    occurrence_id: str
    chapter_id: str
    surface: str
    mention_type: Literal["name", "nickname", "descriptor"]
    referent_kind_claim: ReferentKind
    anchor: SourceAnchor
    evidence_quote: str
    story_position: StoryPositionV1
    routing_disposition: RoutingDisposition
    binding_status: BindingStatus
    entity_or_pending_ref: str | None
    source_window_ids: tuple[str, ...]
    source_request_fingerprints: tuple[str, ...]

    def __post_init__(self) -> None:
        _required(self.occurrence_id, "occurrence_id")
        _required(self.chapter_id, "occurrence chapter_id")
        _required(self.surface, "occurrence surface")
        _required(self.evidence_quote, "occurrence evidence_quote")
        if self.mention_type not in MENTION_TYPES:
            raise RegistryContractError(f"unknown mention type: {self.mention_type}")
        if self.referent_kind_claim not in REFERENT_KINDS:
            raise RegistryContractError(
                f"unknown referent kind: {self.referent_kind_claim}"
            )
        if self.routing_disposition not in {
            "identity_registry",
            "noncharacter_index",
            "pending_kind",
        }:
            raise RegistryContractError(
                f"unknown routing disposition: {self.routing_disposition}"
            )
        if self.binding_status not in {
            "attached",
            "pending",
            "noncharacter",
            "unresolved",
        }:
            raise RegistryContractError(
                f"unknown occurrence binding status: {self.binding_status}"
            )
        if not self.source_window_ids or not self.source_request_fingerprints:
            raise RegistryContractError("occurrence provenance cannot be empty")
        if self.binding_status in {"attached", "pending"}:
            _required(self.entity_or_pending_ref or "", "occurrence binding ref")
        elif self.entity_or_pending_ref is not None:
            raise RegistryContractError(
                "unresolved/noncharacter occurrence cannot carry a binding ref"
            )
        if self.routing_disposition == "noncharacter_index" and self.binding_status != "noncharacter":
            raise RegistryContractError("noncharacter route must stay noncharacter")
        if self.routing_disposition == "pending_kind" and self.binding_status == "attached":
            raise RegistryContractError("unknown kind cannot attach to an entity")


@dataclass(frozen=True, slots=True)
class AliasBindingV1(JsonRecord):
    surface: str
    covered_occurrence_ids: tuple[str, ...]
    surface_observed_from: StoryPositionV1
    binding_disclosed_from: StoryPositionV1 | None
    world_valid_from: StoryPositionV1 | None
    world_valid_until: StoryPositionV1 | None
    used_by_entity_ids: tuple[str, ...] | None
    decision_revision_hash: str

    def __post_init__(self) -> None:
        _required(self.surface, "alias surface")
        _required(self.decision_revision_hash, "alias decision revision")
        if not self.covered_occurrence_ids:
            raise RegistryContractError("alias must cover at least one occurrence")
        if len(set(self.covered_occurrence_ids)) != len(self.covered_occurrence_ids):
            raise RegistryContractError("alias repeats an occurrence id")
        if (
            self.world_valid_from is not None
            and self.world_valid_until is not None
            and self.world_valid_until <= self.world_valid_from
        ):
            raise RegistryContractError("alias world-valid interval must be half-open")


@dataclass(frozen=True, slots=True)
class EntityRecordV1(JsonRecord):
    entity_id: str
    referent_kind: IdentityReferentKind
    runtime_eligibility: Literal["eligible", "deferred", "invalid"]
    canonical_surface: str
    canonical_surface_evidence_refs: tuple[str, ...]
    aliases: tuple[AliasBindingV1, ...]
    status: Literal["active", "pending_revision", "retired"]
    created_in_scope: str
    current_revision_hash: str

    def __post_init__(self) -> None:
        _required(self.entity_id, "entity_id")
        _required(self.canonical_surface, "canonical_surface")
        _required(self.created_in_scope, "created_in_scope")
        _required(self.current_revision_hash, "current_revision_hash")
        if self.referent_kind not in IDENTITY_KINDS | {"unknown"}:
            raise RegistryContractError(f"invalid entity kind: {self.referent_kind}")
        if self.runtime_eligibility not in {"eligible", "deferred", "invalid"}:
            raise RegistryContractError(
                f"invalid runtime eligibility: {self.runtime_eligibility}"
            )
        if self.status not in {"active", "pending_revision", "retired"}:
            raise RegistryContractError(f"invalid entity status: {self.status}")
        if self.referent_kind == "unknown" and (
            self.status == "active" or self.runtime_eligibility == "eligible"
        ):
            raise RegistryContractError("unknown referent cannot be an active entity")
        if not self.canonical_surface_evidence_refs:
            raise RegistryContractError("canonical surface needs occurrence evidence")
        if len(set(self.canonical_surface_evidence_refs)) != len(
            self.canonical_surface_evidence_refs
        ):
            raise RegistryContractError("canonical evidence refs are not unique")


@dataclass(frozen=True, slots=True)
class PendingReferentV1(JsonRecord):
    pending_id: str
    occurrence_ids: tuple[str, ...]
    candidate_entity_refs: tuple[str, ...]
    reason_code: Literal[
        "no_candidate",
        "ambiguous_candidates",
        "conflicting_kind",
        "insufficient_evidence",
        "reconcile_cap",
    ]
    opened_scope: str
    last_considered_scope: str
    status: Literal["open", "resolved", "superseded"]
    resolution_revision_hash: str | None

    def __post_init__(self) -> None:
        _required(self.pending_id, "pending_id")
        _required(self.opened_scope, "pending opened_scope")
        _required(self.last_considered_scope, "pending last_considered_scope")
        if not self.occurrence_ids:
            raise RegistryContractError("pending referent needs occurrences")
        if len(set(self.occurrence_ids)) != len(self.occurrence_ids):
            raise RegistryContractError("pending referent repeats an occurrence")
        if len(set(self.candidate_entity_refs)) != len(self.candidate_entity_refs):
            raise RegistryContractError("pending candidate refs are not unique")
        if self.reason_code not in PENDING_REASONS:
            raise RegistryContractError(f"unknown pending reason: {self.reason_code}")
        if self.status == "open" and self.resolution_revision_hash is not None:
            raise RegistryContractError("open pending record cannot be resolved")
        if self.status not in {"open", "resolved", "superseded"}:
            raise RegistryContractError(f"unknown pending status: {self.status}")


@dataclass(frozen=True, slots=True)
class PresenceRowV1(JsonRecord):
    entity_or_pending_ref: str
    block_id: str
    occurrence_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        _required(self.entity_or_pending_ref, "presence ref")
        _required(self.block_id, "presence block_id")
        if not self.occurrence_ids:
            raise RegistryContractError("presence row needs occurrences")
        if len(set(self.occurrence_ids)) != len(self.occurrence_ids):
            raise RegistryContractError("presence row repeats an occurrence")


@dataclass(frozen=True, slots=True)
class CandidateSelectionManifestV1(JsonRecord):
    snapshot_hash: str
    as_of_chapter_order: int
    active_block_orders: tuple[int, ...]
    selected_entity_ids: tuple[str, ...]
    selected_pending_ids: tuple[str, ...]
    rows: tuple[Mapping[str, Any], ...]
    total_matches: int
    cap: int
    truncated: bool
    overflow: bool
    selection_universe_hash: str
    manifest_hash: str

    def __post_init__(self) -> None:
        _required(self.snapshot_hash, "candidate snapshot_hash")
        _required(self.selection_universe_hash, "candidate selection_universe_hash")
        _required(self.manifest_hash, "candidate manifest_hash")
        if self.as_of_chapter_order < 0:
            raise RegistryContractError("candidate as-of chapter order must be non-negative")
        if not self.active_block_orders or min(self.active_block_orders) < 0:
            raise RegistryContractError("candidate active block orders are invalid")
        if tuple(sorted(set(self.active_block_orders))) != self.active_block_orders:
            raise RegistryContractError(
                "candidate active block orders must be sorted and unique"
            )
        if self.total_matches < 0 or self.cap < 1:
            raise RegistryContractError("candidate counts/cap are invalid")
        if len(self.rows) > self.cap or len(self.rows) > self.total_matches:
            raise RegistryContractError("candidate selected rows exceed their universe")
        if len(set(self.selected_entity_ids)) != len(self.selected_entity_ids):
            raise RegistryContractError("candidate entity ids are not unique")
        if len(set(self.selected_pending_ids)) != len(self.selected_pending_ids):
            raise RegistryContractError("candidate pending ids are not unique")
        if self.truncated != (self.total_matches > self.cap):
            raise RegistryContractError("candidate truncation flag is inconsistent")
        if self.overflow != self.truncated:
            raise RegistryContractError("candidate overflow must expose truncation")


@dataclass(frozen=True, slots=True)
class RegistryGenerationV1(JsonRecord):
    state_lineage_id: str
    generation_id: str
    parent_generation_id: str | None
    chapter_id: str
    source_manifest_hash: str
    b0_request_fingerprint: str
    b1_request_fingerprints: tuple[str, ...]
    candidate_selection_manifest_hashes: tuple[str, ...]
    reconcile_request_fingerprints: tuple[str, ...]
    entity_revisions: tuple[Mapping[str, Any], ...]
    alias_revisions: tuple[Mapping[str, Any], ...]
    occurrence_records: tuple[Mapping[str, Any], ...]
    presence_rows: tuple[Mapping[str, Any], ...]
    pending_records: tuple[Mapping[str, Any], ...]
    commit_payload_hash: str

    def __post_init__(self) -> None:
        _required(self.state_lineage_id, "generation lineage")
        _required(self.generation_id, "generation_id")
        _required(self.chapter_id, "generation chapter_id")
        _required(self.source_manifest_hash, "source_manifest_hash")
        _required(self.b0_request_fingerprint, "b0 request fingerprint")
        _required(self.commit_payload_hash, "commit_payload_hash")


__all__ = [
    "AliasBindingV1",
    "BindingStatus",
    "BrokerContextRequestV1",
    "CandidateSelectionManifestV1",
    "DECISION_FIELDS",
    "EVIDENCE_CLASSES",
    "EntityRecordV1",
    "IDENTITY_KINDS",
    "IDENTITY_REASON_CODES",
    "IdentityProposalV1",
    "ModelContextRequestDraftV1",
    "NONCHARACTER_KINDS",
    "OccurrenceRecordV1",
    "PendingReferentV1",
    "PresenceRowV1",
    "REFERENT_KINDS",
    "RegistryContractError",
    "RegistryGenerationV1",
    "RoutingDisposition",
    "StoryPositionV1",
]
