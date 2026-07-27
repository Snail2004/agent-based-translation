from __future__ import annotations

from dataclasses import dataclass, field, fields, replace
from typing import Any, ClassVar, Literal, Mapping, TypeAlias

from pipeline.literary.checkpoint import canonical_hash, canonical_json


DecisionState: TypeAlias = Literal[
    "proposed",
    "corroborated",
    "active",
    "quarantine_proposed",
    "blocked_for_runtime",
    "pending_resolution",
    "rejected",
]
DecisionKind: TypeAlias = Literal[
    "identity", "frame", "phase", "address", "disclosure", "explicit_fact"
]
RouteRole: TypeAlias = Literal["adjudicator", "checker"]
IndependenceClass: TypeAlias = Literal[
    "same_model_lineage", "distinct_model_lineage"
]

DECISION_KINDS = frozenset(
    {"identity", "frame", "phase", "address", "disclosure", "explicit_fact"}
)
DECISION_STATES = frozenset(
    {
        "proposed",
        "corroborated",
        "active",
        "quarantine_proposed",
        "blocked_for_runtime",
        "pending_resolution",
        "rejected",
    }
)


class Step5ContractError(ValueError):
    """Raised when an S5A typed or canonical contract is violated."""


def _require_nonempty(*values: str, label: str) -> None:
    if not all(values):
        raise Step5ContractError(f"{label} fields must be non-empty")


def _validate_kind(kind: str) -> None:
    if kind not in DECISION_KINDS:
        raise Step5ContractError(f"unknown decision kind: {kind}")


def _validate_range(value: tuple[str, str] | tuple[()]) -> None:
    if len(value) not in {0, 2} or (value and not all(value)):
        raise Step5ContractError("valid_range must be empty or a non-empty pair")


SET_LIKE_FIELDS = frozenset(
    {
        "member_ids",
        "covered_occurrence_ids",
        "evidence_refs",
        "qualified_for",
        "qualification_bindings",
        "qualify_group_ids",
        "qualification_manifest_hashes",
        "authority_evidence_record_hashes",
        "seed_occurrence_ids",
        "support_sets",
        "support_alternatives",
        "overlay_record_refs",
        "quarantine_record_refs",
        "retained_row_ids",
        "qualification_bindings_current",
        "source_event_ids",
        "endpoint_bindings",
        "active_overlay_records",
        "member_row_ids",
        "qualify_groups",
        "dev_eval_groups",
        "held_out_groups",
        "entries",
        "decision_revisions",
        "quarantine_records",
        "measured_metrics",
        "thresholds",
        "qualification_manifest_hashes_current",
        "source_artifact_hashes",
    }
)
SEQUENCE_LIKE_FIELDS = frozenset(
    {
        "valid_range",
        "generation_lineage",
        "invalidations",
        "invalidation_refs",
        "ordered_chapters",
        "unit_manifest",
        "timeline_rows",
        "event_order",
        "audit_append_order",
        "checker_input_sections",
    }
)
MAPPING_LIKE_FIELDS = frozenset(
    {
        "finite_caps",
        "remote_totals",
        "local_compute_totals",
    }
)
FIELD_ORDERING = {
    **{name: "set" for name in SET_LIKE_FIELDS},
    **{name: "sequence" for name in SEQUENCE_LIKE_FIELDS},
    **{name: "mapping" for name in MAPPING_LIKE_FIELDS},
}


def validate_field_ordering_classes(
    *,
    set_like: frozenset[str] = SET_LIKE_FIELDS,
    sequence_like: frozenset[str] = SEQUENCE_LIKE_FIELDS,
    mapping_like: frozenset[str] = MAPPING_LIKE_FIELDS,
) -> None:
    overlaps = (set_like & sequence_like) | (set_like & mapping_like) | (
        sequence_like & mapping_like
    )
    if overlaps:
        raise Step5ContractError(
            f"collection fields have multiple ordering classes: {sorted(overlaps)}"
        )


validate_field_ordering_classes()


def _canonical_value(value: Any, *, field_name: str) -> Any:
    if isinstance(value, CanonicalRecord):
        return value.to_canonical_payload()
    if isinstance(value, Mapping):
        if FIELD_ORDERING.get(field_name) != "mapping":
            raise Step5ContractError(
                f"unknown collection ordering class for field: {field_name}"
            )
        return {
            str(key): _canonical_value(item, field_name=str(key))
            if isinstance(item, (CanonicalRecord, Mapping, tuple, list, frozenset, set))
            else item
            for key, item in value.items()
        }
    if isinstance(value, (tuple, list, frozenset, set)):
        ordering = FIELD_ORDERING.get(field_name)
        if ordering is None:
            raise Step5ContractError(
                f"unknown collection ordering class for field: {field_name}"
            )
        rendered = [
            _canonical_value(item, field_name=field_name)
            if isinstance(item, (CanonicalRecord, Mapping, tuple, list, frozenset, set))
            else item
            for item in value
        ]
        if ordering == "set":
            def set_key(item: Any) -> str:
                if not isinstance(item, Mapping):
                    return canonical_json(item)
                if field_name in {"support_sets", "support_alternatives"}:
                    return str(item.get("support_set_id") or "")
                if field_name == "qualification_bindings":
                    return canonical_json(
                        [
                            item.get("decision_kind"),
                            item.get("qualification_manifest_hash"),
                        ]
                    )
                if field_name == "entries":
                    return str(item.get("call_plan_entry_id") or "")
                if field_name in {"measured_metrics", "thresholds"}:
                    return str(item.get("metric_id") or "")
                if field_name == "decision_revisions":
                    return str(item.get("decision_id") or item.get("proposal_record_id") or "")
                if field_name == "quarantine_records":
                    return str(item.get("record_id") or "")
                return canonical_json(item)

            rendered.sort(key=set_key)
        return rendered
    return value


class CanonicalRecord:
    """Frozen record with an explicit JSON-safe semantic projection."""

    self_hash_field: ClassVar[str | None] = None

    def to_canonical_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {}
        for item in fields(self):
            if item.metadata.get("canonical_exclude"):
                continue
            payload[item.name] = _canonical_value(
                getattr(self, item.name), field_name=item.name
            )
        return payload

    def canonical_hash(self) -> str:
        return canonical_hash(self.to_canonical_payload())


def content_address(record: CanonicalRecord) -> str:
    return record.canonical_hash()


def with_content_address(record: CanonicalRecord) -> CanonicalRecord:
    field_name = record.self_hash_field
    if not field_name:
        raise Step5ContractError(
            f"record has no content-address field: {type(record).__name__}"
        )
    return replace(record, **{field_name: content_address(record)})


def verify_content_address(record: CanonicalRecord) -> None:
    field_name = record.self_hash_field
    if not field_name or getattr(record, field_name) != content_address(record):
        raise Step5ContractError(
            f"content-address mismatch: {type(record).__name__}"
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class ModelIdentity(CanonicalRecord):
    provider: str
    model_family_lineage_id: str
    model_id: str
    weights_version: str

    def __post_init__(self) -> None:
        if not all(
            (self.provider, self.model_family_lineage_id, self.model_id, self.weights_version)
        ):
            raise Step5ContractError("model identity fields must be non-empty")


@dataclass(frozen=True, slots=True, kw_only=True)
class QualificationBinding(CanonicalRecord):
    decision_kind: DecisionKind
    qualification_manifest_hash: str

    def __post_init__(self) -> None:
        _validate_kind(self.decision_kind)
        _require_nonempty(self.qualification_manifest_hash, label="qualification binding")


@dataclass(frozen=True, slots=True, kw_only=True)
class ModelAuthorityRoute(CanonicalRecord):
    authority_route_id: str
    role: RouteRole
    model_identity: ModelIdentity
    qualification_bindings: tuple[QualificationBinding, ...] = ()

    def __post_init__(self) -> None:
        _require_nonempty(self.authority_route_id, label="model authority route")
        if self.role not in {"adjudicator", "checker"}:
            raise Step5ContractError(f"unknown authority route role: {self.role}")


@dataclass(frozen=True, slots=True, kw_only=True)
class HumanAuthorityRoute(CanonicalRecord):
    authority_route_id: str
    human_decision_record_hash: str

    def __post_init__(self) -> None:
        _require_nonempty(
            self.authority_route_id,
            self.human_decision_record_hash,
            label="human authority route",
        )


AuthorityRoute: TypeAlias = ModelAuthorityRoute | HumanAuthorityRoute


@dataclass(frozen=True, slots=True, kw_only=True)
class HumanDecisionRecord(CanonicalRecord):
    reviewer_id: str
    recorded_at_audit: str = field(metadata={"canonical_exclude": True})
    decision_kind: DecisionKind
    decision_question_hash: str
    canonical_signature_hash: str
    verdict: Literal["approve", "reject"]
    evidence_refs: frozenset[str]
    entity_or_item_revision_hash: str

    def __post_init__(self) -> None:
        _validate_kind(self.decision_kind)
        _require_nonempty(
            self.reviewer_id,
            self.decision_question_hash,
            self.canonical_signature_hash,
            self.entity_or_item_revision_hash,
            label="human decision",
        )
        if self.verdict not in {"approve", "reject"}:
            raise Step5ContractError(f"unknown human verdict: {self.verdict}")


@dataclass(frozen=True, slots=True, kw_only=True)
class AuthorityIndependencePolicy(CanonicalRecord):
    policy_hash: str = field(default="", metadata={"canonical_exclude": True})
    policy_version: str
    qualification_policy_hash: str

    self_hash_field: ClassVar[str] = "policy_hash"

    def __post_init__(self) -> None:
        _require_nonempty(
            self.policy_version,
            self.qualification_policy_hash,
            label="authority policy",
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class ExistingCandidateSignature(CanonicalRecord):
    candidate_handle: str
    covered_occurrence_ids: frozenset[str]
    referent_kind: str
    valid_range: tuple[str, str] | tuple[()] = ()
    frame_ref: str | None = None

    def __post_init__(self) -> None:
        _require_nonempty(self.candidate_handle, self.referent_kind, label="existing signature")
        if not self.covered_occurrence_ids:
            raise Step5ContractError("existing signature occurrence cover is empty")
        _validate_range(self.valid_range)


@dataclass(frozen=True, slots=True, kw_only=True)
class NewEntitySignature(CanonicalRecord):
    covered_occurrence_ids: frozenset[str]
    referent_kind: str
    canonical_surface_guess: str | None = None
    valid_range: tuple[str, str] | tuple[()] = ()
    frame_ref: str | None = None

    def __post_init__(self) -> None:
        _require_nonempty(self.referent_kind, label="new-entity signature")
        if not self.covered_occurrence_ids:
            raise Step5ContractError("new-entity signature occurrence cover is empty")
        _validate_range(self.valid_range)


SemanticSignature: TypeAlias = ExistingCandidateSignature | NewEntitySignature


@dataclass(frozen=True, slots=True, kw_only=True)
class MetricRatio(CanonicalRecord):
    metric_id: str
    numerator: int
    denominator: int

    def __post_init__(self) -> None:
        if self.denominator <= 0 or self.numerator < 0:
            raise Step5ContractError("metric ratios require non-negative/positive integers")


@dataclass(frozen=True, slots=True, kw_only=True)
class MetricThreshold(CanonicalRecord):
    metric_id: str
    comparator: Literal[">=", ">", "<=", "<", "=="]
    threshold_numerator: int
    threshold_denominator: int

    def __post_init__(self) -> None:
        if self.threshold_denominator <= 0 or self.threshold_numerator < 0:
            raise Step5ContractError("threshold ratios require non-negative/positive integers")


@dataclass(frozen=True, slots=True, kw_only=True)
class QualificationManifest(CanonicalRecord):
    qualification_manifest_hash: str = field(
        default="", metadata={"canonical_exclude": True}
    )
    model_identity: ModelIdentity
    decision_kind: DecisionKind
    oracle_split_manifest_hash: str
    qualify_group_ids: frozenset[str]
    independence_policy_hash: str
    qualification_policy_hash: str
    metric_schema_version: str
    measured_metrics: tuple[MetricRatio, ...]
    thresholds: tuple[MetricThreshold, ...]
    qualified_result: bool

    self_hash_field: ClassVar[str] = "qualification_manifest_hash"

    def __post_init__(self) -> None:
        _validate_kind(self.decision_kind)
        _require_nonempty(
            self.oracle_split_manifest_hash,
            self.independence_policy_hash,
            self.qualification_policy_hash,
            self.metric_schema_version,
            label="qualification manifest",
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class DecisionQuestion(CanonicalRecord):
    semantic_question_hash: str
    selection_universe_hash: str

    def __post_init__(self) -> None:
        _require_nonempty(
            self.semantic_question_hash,
            self.selection_universe_hash,
            label="decision question",
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class CheckerInputSection(CanonicalRecord):
    section_id: str
    section_content_hash: str
    source_artifact_hashes: frozenset[str]

    def __post_init__(self) -> None:
        _require_nonempty(
            self.section_id,
            self.section_content_hash,
            label="checker input section",
        )


def build_checker_input_section(
    *,
    section_id: str,
    rendered_content: str,
    source_artifact_hashes: frozenset[str],
) -> CheckerInputSection:
    return CheckerInputSection(
        section_id=section_id,
        section_content_hash=canonical_hash(
            {"rendered_content": rendered_content}
        ),
        source_artifact_hashes=source_artifact_hashes,
    )


@dataclass(frozen=True, slots=True, kw_only=True)
class CheckerInputSectionManifest(CanonicalRecord):
    checker_input_section_manifest_hash: str = field(
        default="", metadata={"canonical_exclude": True}
    )
    checker_request_fingerprint: str
    checker_input_sections: tuple[CheckerInputSection, ...]

    self_hash_field: ClassVar[str] = "checker_input_section_manifest_hash"

    def __post_init__(self) -> None:
        _require_nonempty(
            self.checker_request_fingerprint,
            label="checker input-section manifest",
        )
        if not self.checker_input_sections:
            raise Step5ContractError("checker input-section manifest cannot be empty")
        section_ids = [section.section_id for section in self.checker_input_sections]
        if len(section_ids) != len(set(section_ids)):
            raise Step5ContractError("checker input section ids must be unique")


def build_checker_input_section_manifest(
    *,
    checker_request_fingerprint: str,
    checker_input_sections: tuple[CheckerInputSection, ...],
) -> CheckerInputSectionManifest:
    draft = CheckerInputSectionManifest(
        checker_request_fingerprint=checker_request_fingerprint,
        checker_input_sections=checker_input_sections,
    )
    return replace(
        draft,
        checker_input_section_manifest_hash=draft.canonical_hash(),
    )


@dataclass(frozen=True, slots=True, kw_only=True)
class BlindingValidationRecord(CanonicalRecord):
    checker_request_fingerprint: str
    checker_input_section_manifest_hash: str
    checker_input_section_manifest: CheckerInputSectionManifest
    adjudicator_response_artifact_hash: str
    validator_contract_hash: str

    def __post_init__(self) -> None:
        _require_nonempty(
            self.checker_request_fingerprint,
            self.checker_input_section_manifest_hash,
            self.adjudicator_response_artifact_hash,
            self.validator_contract_hash,
            label="blinding validation",
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class AgreementRecord(CanonicalRecord):
    proposal_record_id: str
    question: DecisionQuestion
    request_fingerprint_a: str
    request_fingerprint_b: str
    canonical_signature_hash_a: str
    canonical_signature_hash_b: str
    blinding_validation_record_hash: str
    adjudicator_route_id: str
    checker_route_id: str

    def __post_init__(self) -> None:
        _require_nonempty(
            self.proposal_record_id,
            self.request_fingerprint_a,
            self.request_fingerprint_b,
            self.canonical_signature_hash_a,
            self.canonical_signature_hash_b,
            self.blinding_validation_record_hash,
            self.adjudicator_route_id,
            self.checker_route_id,
            label="agreement record",
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class SupportSet(CanonicalRecord):
    support_set_id: str
    member_ids: frozenset[str]

    def __post_init__(self) -> None:
        if not self.support_set_id or not self.member_ids:
            raise Step5ContractError("support sets must be named and non-empty")


@dataclass(frozen=True, slots=True, kw_only=True)
class DecisionRevision(CanonicalRecord):
    decision_id: str | None
    proposal_record_id: str
    decision_kind: DecisionKind
    state: DecisionState
    question_hash: str
    canonical_signature_hash: str
    support_sets: tuple[SupportSet, ...]
    decided_at_scope: str
    qualification_manifest_hashes: frozenset[str]
    authority_evidence_record_hashes: frozenset[str]
    authority_policy_hash: str
    validator_contract_hash: str

    def __post_init__(self) -> None:
        _validate_kind(self.decision_kind)
        if self.state not in DECISION_STATES:
            raise Step5ContractError(f"unknown decision state: {self.state}")
        _require_nonempty(
            self.proposal_record_id,
            self.question_hash,
            self.canonical_signature_hash,
            self.decided_at_scope,
            self.authority_policy_hash,
            self.validator_contract_hash,
            label="decision revision",
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class QuarantineProposedRecord(CanonicalRecord):
    record_id: str
    proposal_record_id: str
    seed_occurrence_ids: frozenset[str]
    evidence_refs: frozenset[str]
    state: Literal["quarantine_proposed"] = "quarantine_proposed"
    decided_at_scope: str

    def __post_init__(self) -> None:
        _require_nonempty(
            self.record_id,
            self.proposal_record_id,
            self.decided_at_scope,
            label="quarantine proposal",
        )
        if not self.seed_occurrence_ids or not self.evidence_refs:
            raise Step5ContractError("quarantine proposal requires seeds and evidence")
        if self.state != "quarantine_proposed":
            raise Step5ContractError("quarantine proposal has an invalid state")


@dataclass(frozen=True, slots=True, kw_only=True)
class SafetySeedChangeSet(CanonicalRecord):
    changeset_hash: str = field(default="", metadata={"canonical_exclude": True})
    state_lineage_id: str
    source_scope_id: str
    parent_generation_hash: str | None
    bundle_manifest_hash: str
    validator_contract_hash: str
    quarantine_records: tuple[QuarantineProposedRecord, ...]
    materialized_view_hash: str
    estimated_apply_cost: int

    self_hash_field: ClassVar[str] = "changeset_hash"

    def __post_init__(self) -> None:
        if self.estimated_apply_cost < 0:
            raise Step5ContractError("estimated apply cost cannot be negative")
        _require_nonempty(
            self.state_lineage_id,
            self.source_scope_id,
            self.bundle_manifest_hash,
            self.validator_contract_hash,
            self.materialized_view_hash,
            label="safety-seed changeset",
        )
        if not self.quarantine_records:
            raise Step5ContractError("safety-seed changeset cannot be empty")
        record_ids = [record.record_id for record in self.quarantine_records]
        if len(record_ids) != len(set(record_ids)):
            raise Step5ContractError("quarantine record ids must be unique")


@dataclass(frozen=True, slots=True, kw_only=True)
class FullScopeChangeSet(CanonicalRecord):
    changeset_hash: str = field(default="", metadata={"canonical_exclude": True})
    state_lineage_id: str
    source_scope_id: str
    parent_generation_hash: str | None
    bundle_manifest_hash: str
    validator_contract_hash: str
    decision_revisions: tuple[DecisionRevision, ...]
    overlay_record_refs: frozenset[str]
    quarantine_record_refs: frozenset[str]
    invalidation_refs: tuple[str, ...]
    retained_row_ids: frozenset[str]
    materialized_view_hash: str
    estimated_apply_cost: int

    self_hash_field: ClassVar[str] = "changeset_hash"

    def __post_init__(self) -> None:
        if self.estimated_apply_cost < 0:
            raise Step5ContractError("estimated apply cost cannot be negative")
        _require_nonempty(
            self.state_lineage_id,
            self.source_scope_id,
            self.bundle_manifest_hash,
            self.validator_contract_hash,
            self.materialized_view_hash,
            label="full changeset",
        )
        revision_ids = [
            revision.decision_id or revision.proposal_record_id
            for revision in self.decision_revisions
        ]
        if len(revision_ids) != len(set(revision_ids)):
            raise Step5ContractError("decision revision ids must be unique per changeset")


ScopeChangeSet: TypeAlias = SafetySeedChangeSet | FullScopeChangeSet


@dataclass(frozen=True, slots=True, kw_only=True)
class Generation(CanonicalRecord):
    generation_hash: str = field(default="", metadata={"canonical_exclude": True})
    parent_generation_hash: str | None
    state_lineage_id: str
    kind: Literal["full", "safety_seed"]
    generation_schema_version: str
    changeset_hash: str
    support_index_hash: str
    authority_policy_hash: str
    qualification_policy_hash: str
    validator_contract_hash: str
    semantic_state_hash: str
    materialized_view_hash: str
    created_at_audit: str | None = field(
        default=None, metadata={"canonical_exclude": True}
    )

    self_hash_field: ClassVar[str] = "generation_hash"

    def __post_init__(self) -> None:
        if self.kind not in {"full", "safety_seed"}:
            raise Step5ContractError(f"unknown generation kind: {self.kind}")
        _require_nonempty(
            self.state_lineage_id,
            self.generation_schema_version,
            self.changeset_hash,
            self.support_index_hash,
            self.authority_policy_hash,
            self.qualification_policy_hash,
            self.validator_contract_hash,
            self.semantic_state_hash,
            self.materialized_view_hash,
            label="generation",
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class SemanticInputs(CanonicalRecord):
    endpoint_bindings: frozenset[str]
    source_event_ids: frozenset[str]
    frame_version: str
    active_overlay_records: frozenset[str]
    disclosure_view_hash: str
    quarantine_set_hash: str
    context_policy_version: str


def semantic_input_hash(inputs: SemanticInputs) -> str:
    return inputs.canonical_hash()


def allocate_candidate_handles(candidate_ids: tuple[str, ...]) -> tuple[tuple[str, str], ...]:
    if len(candidate_ids) != len(set(candidate_ids)):
        raise Step5ContractError("candidate ids must be unique")
    width = max(2, len(str(len(candidate_ids))))
    return tuple(
        (f"cand_{index:0{width}d}", candidate_id)
        for index, candidate_id in enumerate(candidate_ids, start=1)
    )


def shuffle_candidate_handles(
    handles: tuple[tuple[str, str], ...], *, checker_fingerprint: str
) -> tuple[tuple[str, str], ...]:
    if not checker_fingerprint:
        raise Step5ContractError("checker fingerprint is required")
    candidate_ids = [row[1] for row in handles]
    if len(candidate_ids) != len(set(candidate_ids)):
        raise Step5ContractError("candidate handle map contains duplicate ids")
    shuffled = sorted(
        candidate_ids,
        key=lambda candidate_id: canonical_hash(
            {
                "checker_fingerprint": checker_fingerprint,
                "candidate_id": candidate_id,
            }
        ),
    )
    width = max(2, len(str(len(shuffled))))
    return tuple(
        (f"cand_{index:0{width}d}", candidate_id)
        for index, candidate_id in enumerate(shuffled, start=1)
    )


def normalize_signature(
    signature: SemanticSignature, *, handle_map: Mapping[str, str]
) -> dict[str, Any]:
    payload = signature.to_canonical_payload()
    if isinstance(signature, ExistingCandidateSignature):
        candidate_id = handle_map.get(signature.candidate_handle)
        if not candidate_id:
            raise Step5ContractError("signature references an unknown candidate handle")
        payload.pop("candidate_handle")
        payload["candidate_id"] = candidate_id
        payload["signature_kind"] = "existing_candidate"
    else:
        payload["signature_kind"] = "new_entity"
    return payload


def canonical_signature_hash(
    signature: SemanticSignature, *, handle_map: Mapping[str, str]
) -> str:
    return canonical_hash(normalize_signature(signature, handle_map=handle_map))


def proposal_record_id(
    *, request_fingerprint: str, canonical_signature_hash_value: str
) -> str:
    if not request_fingerprint or not canonical_signature_hash_value:
        raise Step5ContractError("proposal id inputs must be non-empty")
    return "prop_" + canonical_hash(
        {
            "request_fingerprint": request_fingerprint,
            "canonical_signature_hash": canonical_signature_hash_value,
        }
    )[:24]


__all__ = [
    "AgreementRecord",
    "AuthorityIndependencePolicy",
    "AuthorityRoute",
    "BlindingValidationRecord",
    "CanonicalRecord",
    "CheckerInputSection",
    "CheckerInputSectionManifest",
    "DecisionKind",
    "DecisionQuestion",
    "DecisionRevision",
    "DecisionState",
    "ExistingCandidateSignature",
    "FIELD_ORDERING",
    "FullScopeChangeSet",
    "Generation",
    "HumanAuthorityRoute",
    "HumanDecisionRecord",
    "IndependenceClass",
    "MetricRatio",
    "MetricThreshold",
    "ModelAuthorityRoute",
    "ModelIdentity",
    "NewEntitySignature",
    "QualificationBinding",
    "QualificationManifest",
    "QuarantineProposedRecord",
    "RouteRole",
    "SafetySeedChangeSet",
    "ScopeChangeSet",
    "SemanticInputs",
    "Step5ContractError",
    "SupportSet",
    "allocate_candidate_handles",
    "build_checker_input_section",
    "build_checker_input_section_manifest",
    "canonical_signature_hash",
    "content_address",
    "normalize_signature",
    "proposal_record_id",
    "semantic_input_hash",
    "shuffle_candidate_handles",
    "validate_field_ordering_classes",
    "verify_content_address",
    "with_content_address",
]
