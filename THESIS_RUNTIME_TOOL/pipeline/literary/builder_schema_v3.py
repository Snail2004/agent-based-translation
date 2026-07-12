"""Typed, offline data contracts for Builder v3.

The dataclasses describe normalized payloads after the Builder-v3 validators
have located anchors and minted code-owned identifiers.  They deliberately do
not import or alter the live Builder orchestration.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from pipeline.literary.source_anchor import SourceAnchor, SourceInterval


ReferentKindClaim = Literal[
    "person",
    "animal",
    "nonhuman_character",
    "place",
    "group_reference",
    "object",
    "unknown",
]
SurfaceKind = Literal["proper_name", "descriptor"]
MentionType = Literal["name", "nickname", "descriptor"]
ReferenceScope = Literal["individual", "group", "narrator", "reader", "unknown"]
AttributionMethod = Literal[
    "explicit_tag",
    "turn_alternation",
    "nearby_context",
    "narrator_inference",
    "vocative",
]
RegisterCue = Literal["neutral", "intimate", "deferential", "paternal", "hostile", "mocking"]
FrameKind = Literal[
    "primary_narration",
    "embedded_document",
    "letter",
    "diary",
    "dream",
    "vision",
    "tale_told_aloud",
    "quoted_report",
]
StoryTimeLabel = Literal["frame_present", "retrospective_past", "anterior_past"]
FrameStatus = Literal["proposed", "uncertain"]
ObservedValenceHint = Literal["positive", "negative", "mixed", "unclear"]
StateAttribute = Literal["social_status", "alias_or_title", "life_status", "residence"]
ThreadKind = Literal["mystery", "pending_transition", "question"]
FactType = Literal["narrator", "register", "speech_style", "status", "setting"]
InferenceBasis = Literal["stated", "derived"]
RuntimeEligibility = Literal["eligible", "discourse_only", "route_out", "deferred", "invalid"]


@dataclass(frozen=True)
class SourceBoundary:
    anchor_text: str
    evidence_quote: str
    occurrence_hint: int | None = None


@dataclass(frozen=True)
class CastClaim:
    surface: str
    surface_kind: SurfaceKind
    referent_kind_claim: ReferentKindClaim
    role_hint: str
    scene_range: tuple[str, str]
    source_block_ids: tuple[str, ...]
    anchor_text: str
    evidence_quote: str
    occurrence_hint: int | None = None
    cast_claim_id: str | None = None
    anchor: SourceAnchor | None = None
    evidence_max_order: int | None = None


@dataclass(frozen=True)
class Scene:
    block_range: tuple[str, str]
    co_present_count: int
    participants: tuple[str, ...]


@dataclass(frozen=True)
class Setting:
    place: str
    time_frame_hint: Literal["frame_present", "past_recollection", "unclear"]
    scene_shape: Literal[
        "single_scene_one_location", "few_scenes", "many_scenes_or_travel"
    ]


@dataclass(frozen=True)
class ChapterBrief:
    chapter_id: str
    cast_claims: tuple[CastClaim, ...]
    setting: Setting
    scenes_party_size: tuple[Scene, ...]
    neutral_premise: str
    input_max_order: int | None = None


@dataclass(frozen=True)
class Mention:
    surface: str
    mention_type: MentionType
    referent_kind_claim: ReferentKindClaim
    anchor_text: str
    evidence_quote: str
    block_id: str
    occurrence_hint: int | None = None
    mention_id: str | None = None
    anchor: SourceAnchor | None = None


@dataclass(frozen=True)
class Glossary:
    source_term: str
    proposed_target_vi: str
    category: Literal["place", "object", "cultural", "other"]
    do_not_translate: bool
    block_ids: tuple[str, ...]


@dataclass(frozen=True)
class Lexicon:
    chapter_id: str
    window_block_ids: tuple[str, ...]
    context_only_used: bool
    character_mentions: tuple[Mention, ...]
    glossary_candidates: tuple[Glossary, ...]


@dataclass(frozen=True)
class Endpoint:
    surface: str
    reference_scope: ReferenceScope
    referent_kind_claim: ReferentKindClaim
    mention_ref: str | None
    attribution_method: AttributionMethod
    anchor_text: str
    evidence_quote: str
    occurrence_hint: int | None = None
    endpoint_id: str | None = None
    anchor: SourceAnchor | None = None
    resolution_evidence: str | None = None
    runtime_eligibility: RuntimeEligibility | None = None


@dataclass(frozen=True)
class AddressTerm:
    anchor_text: str
    evidence_quote: str
    addressee_ref: Literal["speaker", "addressee"]
    occurrence_hint: int | None = None
    address_occurrence_id: str | None = None
    anchor: SourceAnchor | None = None


@dataclass(frozen=True)
class Turn:
    speaker: Endpoint
    addressee: Endpoint | None
    utterance_quote: str
    address_terms: tuple[AddressTerm, ...]
    register_cue: RegisterCue
    block_id: str
    turn_id: str | None = None
    position_key: tuple[int, int, int] | None = None


@dataclass(frozen=True)
class Event:
    actor: Endpoint
    target: Endpoint
    event_type: str
    evidence_quote: str
    block_id: str
    event_id: str | None = None
    position_key: tuple[int, int, int] | None = None


@dataclass(frozen=True)
class Narrative:
    chapter_id: str
    window_block_ids: tuple[str, ...]
    context_only_used: bool
    speaker_turns: tuple[Turn, ...]
    relation_events: tuple[Event, ...]


@dataclass(frozen=True)
class FrameSegment:
    local_segment_key: str
    parent_local_key: str | None
    narrator_surface: str
    narrator_ref: str | None
    frame_kind: FrameKind
    story_time_label: StoryTimeLabel
    block_range: tuple[str, str]
    start_boundary: SourceBoundary | None
    end_boundary: SourceBoundary | None
    status: FrameStatus
    evidence_quote: str
    start_anchor: SourceAnchor | None = None
    end_anchor: SourceAnchor | None = None
    segment_id: str | None = None
    version: str | None = None
    source_interval: SourceInterval | None = None


@dataclass(frozen=True)
class TransitionHint:
    trigger_event_id: str
    note: str


@dataclass(frozen=True)
class RelationObservation:
    event_id: str
    endpoint_refs: tuple[str, str]
    observed_valence_hint: ObservedValenceHint
    block_id: str
    evidence_quote: str
    transition_hint: TransitionHint | None = None


@dataclass(frozen=True)
class StateChange:
    subject_ref: str
    attribute: StateAttribute
    from_value: str
    to_value: str
    trigger_ref: str
    evidence_quote: str


@dataclass(frozen=True)
class UnresolvedThread:
    thread_local_id: str
    description: str
    opened_block: str
    kind: ThreadKind
    subject_refs: tuple[str, ...] | None = None


@dataclass(frozen=True)
class TranslatorRelevantFact:
    fact_type: FactType
    fact: str
    block_evidence: tuple[str, ...]
    inference_basis: InferenceBasis
    subject_ref: str | None = None
    event_ids: tuple[str, ...] | None = None


@dataclass(frozen=True)
class Motif:
    note: str
    block_ids: tuple[str, ...]
    subject_refs: tuple[str, ...] | None = None


@dataclass(frozen=True)
class Digest:
    chapter_id: str
    chapter_rolling_summary: str
    narration_frame_segments: tuple[FrameSegment, ...]
    relation_observations: tuple[RelationObservation, ...]
    character_state_changes: tuple[StateChange, ...]
    unresolved_threads: tuple[UnresolvedThread, ...]
    translator_relevant_facts: tuple[TranslatorRelevantFact, ...]
    motifs: tuple[Motif, ...]


REFERENT_KIND_CLAIMS = {
    "person",
    "animal",
    "nonhuman_character",
    "place",
    "group_reference",
    "object",
    "unknown",
}
REFERENCE_SCOPES = {"individual", "group", "narrator", "reader", "unknown"}
ATTRIBUTION_METHODS = {
    "explicit_tag",
    "turn_alternation",
    "nearby_context",
    "narrator_inference",
    "vocative",
}
REGISTER_CUES = {"neutral", "intimate", "deferential", "paternal", "hostile", "mocking"}
FRAME_KINDS = {
    "primary_narration",
    "embedded_document",
    "letter",
    "diary",
    "dream",
    "vision",
    "tale_told_aloud",
    "quoted_report",
}
STORY_TIME_LABELS = {"frame_present", "retrospective_past", "anterior_past"}
FRAME_STATUSES = {"proposed", "uncertain"}
VALENCE_HINTS = {"positive", "negative", "mixed", "unclear"}
STATE_ATTRIBUTES = {"social_status", "alias_or_title", "life_status", "residence"}
THREAD_KINDS = {"mystery", "pending_transition", "question"}
FACT_TYPES = {"narrator", "register", "speech_style", "status", "setting"}
INFERENCE_BASES = {"stated", "derived"}
GLOSSARY_CATEGORIES = {"place", "object", "cultural", "other"}
MENTION_TYPES = {"name", "nickname", "descriptor"}
SURFACE_KINDS = {"proper_name", "descriptor"}
TIME_FRAME_HINTS = {"frame_present", "past_recollection", "unclear"}
SCENE_SHAPES = {"single_scene_one_location", "few_scenes", "many_scenes_or_travel"}

RETIRED_FIELDS = {
    "confidence",
    "utterance_gist",
    "termhood",
    "resolution_status",
    "candidate_entity_ids",
}

PHASE_LEAK_EVENT_TYPES = {
    "ally",
    "allied",
    "enemy",
    "enemies",
    "friend",
    "friendly",
    "hostile",
    "hostility",
    "rival",
    "rivalry",
    "strained",
    "estranged",
    "reconciled",
    "dependent",
    "relationship",
    "phase",
}
