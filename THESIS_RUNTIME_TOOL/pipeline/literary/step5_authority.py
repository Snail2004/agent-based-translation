from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction
from typing import Literal, Mapping, TypeAlias

from pipeline.literary.step5_types import (
    AgreementRecord,
    AuthorityIndependencePolicy,
    BlindingValidationRecord,
    DecisionKind,
    DecisionQuestion,
    DecisionRevision,
    DecisionState,
    HumanAuthorityRoute,
    HumanDecisionRecord,
    IndependenceClass,
    MetricRatio,
    MetricThreshold,
    ModelAuthorityRoute,
    QualificationManifest,
    Step5ContractError,
    content_address,
    verify_content_address,
)


class AuthorityError(Step5ContractError):
    """Raised when authority evidence is incomplete, forged, or stale."""


def classify_independence(
    adjudicator: ModelAuthorityRoute,
    checker: ModelAuthorityRoute,
    policy: AuthorityIndependencePolicy,
) -> IndependenceClass:
    verify_content_address(policy)
    if adjudicator.role != "adjudicator" or checker.role != "checker":
        raise AuthorityError("authority routes have incompatible roles")
    if (
        adjudicator.model_identity.model_family_lineage_id
        == checker.model_identity.model_family_lineage_id
    ):
        return "same_model_lineage"
    return "distinct_model_lineage"


def _threshold_passes(metric: MetricRatio, threshold: MetricThreshold) -> bool:
    if metric.metric_id != threshold.metric_id:
        raise AuthorityError("metric and threshold ids do not match")
    measured = Fraction(metric.numerator, metric.denominator)
    required = Fraction(
        threshold.threshold_numerator, threshold.threshold_denominator
    )
    return {
        ">=": measured >= required,
        ">": measured > required,
        "<=": measured <= required,
        "<": measured < required,
        "==": measured == required,
    }[threshold.comparator]


def recompute_qualified_result(manifest: QualificationManifest) -> bool:
    metrics = {item.metric_id: item for item in manifest.measured_metrics}
    if len(metrics) != len(manifest.measured_metrics):
        raise AuthorityError("qualification metric ids must be unique")
    thresholds = {item.metric_id: item for item in manifest.thresholds}
    if len(thresholds) != len(manifest.thresholds) or not thresholds:
        raise AuthorityError("qualification threshold ids must be unique and non-empty")
    if set(metrics) != set(thresholds):
        raise AuthorityError("qualification metrics and thresholds are not an exact cover")
    return all(_threshold_passes(metrics[key], thresholds[key]) for key in thresholds)


def validate_qualification_manifest(
    manifest: QualificationManifest,
    *,
    policy: AuthorityIndependencePolicy,
) -> None:
    verify_content_address(manifest)
    verify_content_address(policy)
    if manifest.independence_policy_hash != policy.policy_hash:
        raise AuthorityError("qualification manifest independence policy mismatch")
    if manifest.qualification_policy_hash != policy.qualification_policy_hash:
        raise AuthorityError("qualification manifest qualification policy mismatch")
    if not manifest.qualify_group_ids:
        raise AuthorityError("qualification manifest has no qualification groups")
    if recompute_qualified_result(manifest) != manifest.qualified_result:
        raise AuthorityError("caller-supplied qualified_result contradicts metrics")


def qualified_for(
    route: ModelAuthorityRoute,
    manifests: Mapping[str, QualificationManifest],
    *,
    policy: AuthorityIndependencePolicy,
) -> frozenset[DecisionKind]:
    seen: set[DecisionKind] = set()
    result: set[DecisionKind] = set()
    for binding in route.qualification_bindings:
        if binding.decision_kind in seen:
            raise AuthorityError("duplicate qualification binding for one decision kind")
        seen.add(binding.decision_kind)
        manifest = manifests.get(binding.qualification_manifest_hash)
        if manifest is None:
            raise AuthorityError("qualification binding references an unknown manifest")
        validate_qualification_manifest(manifest, policy=policy)
        if manifest.model_identity != route.model_identity:
            raise AuthorityError("qualification manifest model identity mismatch")
        if manifest.decision_kind != binding.decision_kind:
            raise AuthorityError("qualification binding decision kind mismatch")
        if manifest.qualified_result:
            result.add(binding.decision_kind)
    return frozenset(result)


def _validate_agreement(
    agreement: AgreementRecord,
    *,
    adjudicator: ModelAuthorityRoute,
    checker: ModelAuthorityRoute,
    blinding_records: Mapping[str, BlindingValidationRecord],
) -> None:
    if agreement.adjudicator_route_id != adjudicator.authority_route_id:
        raise AuthorityError("agreement adjudicator route mismatch")
    if agreement.checker_route_id != checker.authority_route_id:
        raise AuthorityError("agreement checker route mismatch")
    if agreement.request_fingerprint_a == agreement.request_fingerprint_b:
        raise AuthorityError("adjudicator and checker request fingerprints must differ")
    if agreement.canonical_signature_hash_a != agreement.canonical_signature_hash_b:
        raise AuthorityError("authority signatures do not agree")
    blind = blinding_records.get(agreement.blinding_validation_record_hash)
    if blind is None or content_address(blind) != agreement.blinding_validation_record_hash:
        raise AuthorityError("agreement lacks a content-addressed blinding record")
    if validate_blinding(blind) != "valid":
        raise AuthorityError("checker blinding validation is invalid")
    if blind.checker_request_fingerprint != agreement.request_fingerprint_b:
        raise AuthorityError("blinding record checker fingerprint mismatch")


def validate_blinding(
    record: BlindingValidationRecord,
) -> Literal["valid", "invalid"]:
    """Mechanically prove that checker sections exclude adjudicator output."""

    manifest = record.checker_input_section_manifest
    verify_content_address(manifest)
    if (
        record.checker_input_section_manifest_hash
        != manifest.checker_input_section_manifest_hash
    ):
        raise AuthorityError("blinding record section-manifest hash mismatch")
    if record.checker_request_fingerprint != manifest.checker_request_fingerprint:
        raise AuthorityError("blinding record request fingerprint mismatch")
    target = record.adjudicator_response_artifact_hash
    contaminated = any(
        target == section.section_content_hash
        or target in section.source_artifact_hashes
        for section in manifest.checker_input_sections
    )
    return "invalid" if contaminated else "valid"


def promote(
    *,
    adjudicator_route: ModelAuthorityRoute,
    checker_route: ModelAuthorityRoute | HumanAuthorityRoute | None,
    agreement_records: tuple[AgreementRecord, ...],
    decision_kind: DecisionKind,
    decision_question: DecisionQuestion,
    canonical_signature_hash_value: str,
    proposal_record_id_value: str,
    adjudicator_response_artifact_hash: str,
    authority_policy: AuthorityIndependencePolicy,
    qualification_manifests: Mapping[str, QualificationManifest],
    blinding_records: Mapping[str, BlindingValidationRecord],
    persisted_agreement_hashes: frozenset[str] = frozenset(),
    human_records: Mapping[str, HumanDecisionRecord] | None = None,
    entity_or_item_revision_hash: str | None = None,
) -> DecisionState:
    verify_content_address(authority_policy)
    if adjudicator_route.role != "adjudicator":
        raise AuthorityError("the primary authority route must be an adjudicator")
    if checker_route is None:
        if agreement_records:
            raise AuthorityError("agreement evidence exists without a checker")
        return "proposed"

    if isinstance(checker_route, HumanAuthorityRoute):
        if agreement_records:
            raise AuthorityError("human promotion does not consume model agreement records")
        record = (human_records or {}).get(checker_route.human_decision_record_hash)
        if record is None or content_address(record) != checker_route.human_decision_record_hash:
            raise AuthorityError("human route lacks a content-addressed decision record")
        if record.verdict != "approve" or record.decision_kind != decision_kind:
            raise AuthorityError("human record does not approve this decision kind")
        if record.decision_question_hash != decision_question.canonical_hash():
            raise AuthorityError("human record question mismatch")
        if record.canonical_signature_hash != canonical_signature_hash_value:
            raise AuthorityError("human record signature mismatch")
        if (
            not entity_or_item_revision_hash
            or record.entity_or_item_revision_hash != entity_or_item_revision_hash
        ):
            raise AuthorityError("human record revision mismatch")
        return "active"

    if checker_route.role != "checker":
        raise AuthorityError("the secondary model route must be a checker")
    if len(agreement_records) != 1:
        raise AuthorityError("model promotion requires exactly one agreement record")
    agreement = agreement_records[0]
    if content_address(agreement) not in persisted_agreement_hashes:
        raise AuthorityError("agreement record was not persisted content-addressed")
    if agreement.proposal_record_id != proposal_record_id_value:
        raise AuthorityError("agreement proposal record mismatch")
    _validate_agreement(
        agreement,
        adjudicator=adjudicator_route,
        checker=checker_route,
        blinding_records=blinding_records,
    )
    if agreement.question != decision_question:
        raise AuthorityError("agreement decision question mismatch")
    if agreement.canonical_signature_hash_a != canonical_signature_hash_value:
        raise AuthorityError("agreement signature does not match the promoted decision")
    blind = blinding_records[agreement.blinding_validation_record_hash]
    if blind.adjudicator_response_artifact_hash != adjudicator_response_artifact_hash:
        raise AuthorityError("blinding record adjudicator artifact mismatch")
    independence = classify_independence(
        adjudicator_route, checker_route, authority_policy
    )
    if independence == "same_model_lineage":
        return "corroborated"
    if decision_kind not in qualified_for(
        checker_route, qualification_manifests, policy=authority_policy
    ):
        return "corroborated"
    return "active"


def stale_qualification_decisions(
    decisions: tuple[DecisionRevision, ...],
    *,
    current_binding_manifest_hashes: Mapping[str, frozenset[str]],
) -> tuple[str, ...]:
    stale: list[str] = []
    for decision in decisions:
        if decision.state != "active":
            continue
        decision_key = decision.decision_id or decision.proposal_record_id
        if decision.qualification_manifest_hashes != current_binding_manifest_hashes.get(
            decision_key, frozenset()
        ):
            stale.append(decision_key)
    return tuple(sorted(stale))


@dataclass(frozen=True, slots=True, kw_only=True)
class RetainUnchangedRow:
    row_id: str
    active_revision_hash: str


@dataclass(frozen=True, slots=True, kw_only=True)
class BindOccurrenceActive:
    occurrence_id: str
    active_binding_revision_hash: str


@dataclass(frozen=True, slots=True, kw_only=True)
class RemapReferenceActive:
    reference_id: str
    source_ref: str
    target_ref: str
    active_witness_revision_hash: str
    collision: bool = False


FastPathOp: TypeAlias = RetainUnchangedRow | BindOccurrenceActive | RemapReferenceActive


def fast_path_allowed(op: object) -> bool:
    if isinstance(op, RetainUnchangedRow):
        if not op.row_id or not op.active_revision_hash:
            raise AuthorityError("retain-history fast path is incomplete")
        return True
    if isinstance(op, BindOccurrenceActive):
        if not op.occurrence_id or not op.active_binding_revision_hash:
            raise AuthorityError("active occurrence binding fast path is incomplete")
        return True
    if isinstance(op, RemapReferenceActive):
        if (
            not op.reference_id
            or not op.source_ref
            or not op.target_ref
            or not op.active_witness_revision_hash
            or op.collision
            or op.source_ref == op.target_ref
        ):
            raise AuthorityError("reference remap is not one-to-one and collision-free")
        return True
    raise AuthorityError("operation is outside the closed fast-path union")


__all__ = [
    "AuthorityError",
    "BindOccurrenceActive",
    "FastPathOp",
    "RemapReferenceActive",
    "RetainUnchangedRow",
    "classify_independence",
    "fast_path_allowed",
    "promote",
    "qualified_for",
    "recompute_qualified_result",
    "stale_qualification_decisions",
    "validate_blinding",
    "validate_qualification_manifest",
]
