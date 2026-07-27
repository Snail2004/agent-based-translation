"""Offline bridge from the sealed cross-chapter hearing queue to Auditor requests.

The bridge consumes ``cross_chapter_hearing_queue.json`` exactly once, partitions
components by their recorded ``review_route``, and renders one bounded prepared
request per ready ``identity_auditor`` / ``stable_claim_auditor`` component.  It
never adjudicates a hearing, never infers a route from prose, never grants
authority, and performs zero provider calls.  Waiting components and the
``temporal_auditor`` / ``glossary_auditor`` / ``pending_unassigned`` routes stay
visible as unconsumed work; they are not dropped and not rendered.
"""

from __future__ import annotations

from copy import deepcopy
import hashlib
from pathlib import Path
from typing import Any, Mapping, Sequence

from pipeline.literary.b1_chapter_registry_writer_v1 import (
    verify_b1_cross_chapter_hearing_queue_v1,
)
from pipeline.literary.builder_pilot import load_system_prompt_from_design
from pipeline.literary.checkpoint import canonical_hash, canonical_json


BRIDGE_SCHEMA_VERSION = "literary_b1_cross_chapter_audit_bridge_v1"
REQUEST_SCHEMA_VERSION = "literary_cross_chapter_hearing_request_v1_2"

IDENTITY_PROMPT_ID = "literary_cross_chapter_identity_hearing_v1_2"
STABLE_CLAIM_PROMPT_ID = "literary_cross_chapter_stable_claim_hearing_v1_2"

RENDERED_ROUTES = frozenset({"identity_auditor", "stable_claim_auditor"})
UNCONSUMED_ROUTES = ("temporal_auditor", "glossary_auditor", "pending_unassigned")

IDENTITY_LINKAGE_VERDICTS = ("confirmed_distinct", "merge_referents", "insufficient_evidence")
ALIAS_REFERRAL_VERDICTS = ("alias_confirmed", "alias_rejected_distinct", "insufficient_evidence")
SPURIOUS_REFERRAL_VERDICTS = ("dismiss_observation", "keep_observation", "insufficient_evidence")
STABLE_CLAIM_VERDICTS = (
    "uphold_existing",
    "correction",
    "in_story_change",
    "reveal_only",
    "split_referent",
    "insufficient_evidence",
)

_COMPONENT_ID_PREFIX = "b1xhear_"


class B1CrossChapterAuditBridgeError(ValueError):
    pass


# ---------------------------------------------------------------------------
# verification helpers
# ---------------------------------------------------------------------------


def verify_hearing_queue_binding_v1(
    queue: Mapping[str, Any],
    *,
    expected_registry_hash: str | None = None,
) -> None:
    """Fail closed on schema, hash, tamper, or stale-registry problems."""

    verify_b1_cross_chapter_hearing_queue_v1(queue)
    if expected_registry_hash is not None:
        observed = queue.get("registry_hash")
        if observed != expected_registry_hash:
            raise B1CrossChapterAuditBridgeError(
                "hearing queue is bound to a stale or foreign chapter registry"
            )
    for component in _components(queue):
        component_id = _required_string(component.get("component_id"), "component_id")
        body = deepcopy(dict(component))
        body.pop("component_id", None)
        expected = _COMPONENT_ID_PREFIX + canonical_hash(body)[:20]
        if component_id != expected:
            raise B1CrossChapterAuditBridgeError(
                "hearing component id does not match its sealed body"
            )


def partition_hearing_queue_v1(queue: Mapping[str, Any]) -> dict[str, Any]:
    """Exact-cover partition by recorded route and lifecycle; no prose inference."""

    ready_identity: list[dict[str, Any]] = []
    ready_stable: list[dict[str, Any]] = []
    waiting: list[dict[str, Any]] = []
    unconsumed: dict[str, list[str]] = {route: [] for route in UNCONSUMED_ROUTES}
    seen: set[str] = set()
    for component in _components(queue):
        component_id = _required_string(component.get("component_id"), "component_id")
        if component_id in seen:
            raise B1CrossChapterAuditBridgeError(
                "hearing queue covers one component twice"
            )
        seen.add(component_id)
        route = _required_string(component.get("review_route"), "review_route")
        state = _required_string(component.get("lifecycle_state"), "lifecycle_state")
        if state != "ready_for_hearing":
            waiting.append(deepcopy(dict(component)))
            continue
        if route == "identity_auditor":
            ready_identity.append(deepcopy(dict(component)))
        elif route == "stable_claim_auditor":
            ready_stable.append(deepcopy(dict(component)))
        elif route in unconsumed:
            unconsumed[route].append(component_id)
        else:  # pragma: no cover - the queue verifier already rejects this
            raise B1CrossChapterAuditBridgeError("cross-chapter route is unknown")
    covered = (
        len(ready_identity)
        + len(ready_stable)
        + len(waiting)
        + sum(len(rows) for rows in unconsumed.values())
    )
    if covered != len(seen):
        raise B1CrossChapterAuditBridgeError("hearing partition lost a component")
    return {
        "ready_identity": ready_identity,
        "ready_stable_claim": ready_stable,
        "waiting": waiting,
        "unconsumed_ready": unconsumed,
        "covered_component_ids": sorted(seen),
    }


# ---------------------------------------------------------------------------
# request rendering
# ---------------------------------------------------------------------------


def allowed_verdicts_for_component_v1(component: Mapping[str, Any]) -> tuple[str, ...]:
    question_type = _required_string(component.get("question_type"), "question_type")
    if question_type == "identity_linkage":
        return IDENTITY_LINKAGE_VERDICTS
    if question_type == "roster_recognition":
        # The question is whether a differently-named surface refers to a known
        # entity, so the alias verdicts apply; rejecting is an ordinary outcome,
        # not a failure of the channel.
        return ALIAS_REFERRAL_VERDICTS
    if question_type == "stable_claim":
        return STABLE_CLAIM_VERDICTS
    if question_type == "local_cross_chapter_referral":
        kind = _required_string(component.get("component_kind"), "component_kind")
        if kind == "alias_proposal":
            return ALIAS_REFERRAL_VERDICTS
        if kind == "spurious_challenge":
            return SPURIOUS_REFERRAL_VERDICTS
        return IDENTITY_LINKAGE_VERDICTS
    raise B1CrossChapterAuditBridgeError("hearing question type is unknown")


def render_identity_hearing_request_v1(
    component: Mapping[str, Any],
    *,
    queue: Mapping[str, Any],
    source_blocks: Mapping[str, str],
    design_doc: Path | str,
    model_contract: Mapping[str, Any],
    expand_prior_evidence: bool = False,
) -> dict[str, Any]:
    _require_route(component, "identity_auditor")
    prompt = load_system_prompt_from_design(Path(design_doc), IDENTITY_PROMPT_ID)
    candidate_snapshots = _prior_candidate_snapshots(component)
    cited = _cited_block_ids_identity(component)
    evidence, expansion = _resolve_hearing_blocks_v1(
        cited_block_ids=cited,
        prior_anchor_block_ids=_prior_anchor_block_ids(candidate_snapshots),
        source_blocks=source_blocks,
        registry_surfaces=queue.get("registry_roster_surfaces") or [],
        expand_prior_evidence=expand_prior_evidence,
    )
    sections = {
        "component_id": component["component_id"],
        "question_type": component["question_type"],
        "allowed_verdicts": list(allowed_verdicts_for_component_v1(component)),
        "chapter_id": queue.get("chapter_id"),
        "prior_card_ids": _prior_candidate_ids(component),
        "prior_candidate_snapshots": candidate_snapshots,
        "current_entity_ids": deepcopy(
            component.get("current_entity_ids")
            or ([component.get("current_entity_id")] if component.get("current_entity_id") else [])
        ),
        "current_card_snapshots": deepcopy(
            component.get("current_card_snapshots")
            or ([component.get("current_card_snapshot")] if component.get("current_card_snapshot") else [])
        ),
        "current_dossier_snapshots": deepcopy(
            component.get("current_dossier_snapshots")
            or ([component.get("current_dossier_snapshot")] if component.get("current_dossier_snapshot") else [])
        ),
        "candidate_contexts": deepcopy(component.get("candidate_contexts") or []),
        # Recognition proposals ride with the card they name, whether they
        # opened this hearing or joined one already open on it, so the Auditor
        # rules on the whole card once instead of piecemeal.
        "roster_recognition_proposals": deepcopy(
            component.get("roster_recognition_proposals") or []
        ),
        "referral": (
            {
                "component_kind": component.get("component_kind"),
                "original_proposal": deepcopy(component.get("original_proposal")),
                "resolution_note": component.get("resolution_note"),
            }
            if component.get("question_type") == "local_cross_chapter_referral"
            else None
        ),
        "source_blocks": evidence,
        "prior_evidence_expansion": expansion,
        "authority_policy": {
            "identity_authority_granted": False,
            "claim_authority_granted": False,
            "decision_scope": "single_component_proposal_for_code_application",
        },
    }
    return _prepared_request(
        role="cross_chapter_identity_hearing",
        prompt_id=IDENTITY_PROMPT_ID,
        prompt=prompt,
        component=component,
        queue=queue,
        sections=sections,
        response_schema=identity_hearing_response_schema_v1(),
        model_contract=model_contract,
    )


def render_stable_claim_hearing_request_v1(
    component: Mapping[str, Any],
    *,
    queue: Mapping[str, Any],
    source_blocks: Mapping[str, str],
    design_doc: Path | str,
    model_contract: Mapping[str, Any],
    expand_prior_evidence: bool = False,
) -> dict[str, Any]:
    _require_route(component, "stable_claim_auditor")
    prompt = load_system_prompt_from_design(Path(design_doc), STABLE_CLAIM_PROMPT_ID)
    cited = _cited_block_ids_stable(component)
    prior_snapshot = component.get("prior_card_snapshot")
    prior_snapshots = [prior_snapshot] if isinstance(prior_snapshot, Mapping) else []
    evidence, expansion = _resolve_hearing_blocks_v1(
        cited_block_ids=cited,
        prior_anchor_block_ids=_prior_anchor_block_ids(prior_snapshots),
        source_blocks=source_blocks,
        registry_surfaces=queue.get("registry_roster_surfaces") or [],
        expand_prior_evidence=expand_prior_evidence,
    )
    sections = {
        "component_id": component["component_id"],
        "question_type": component["question_type"],
        "allowed_verdicts": list(STABLE_CLAIM_VERDICTS),
        "chapter_id": queue.get("chapter_id"),
        "prior_card_id": component.get("prior_card_id"),
        "field": component.get("field"),
        "existing_value": component.get("existing_value"),
        "observed_value": component.get("observed_value"),
        "conflict_reason": component.get("reason"),
        "prior_card_snapshot": deepcopy(component.get("prior_card_snapshot")),
        "current_card_snapshot": deepcopy(component.get("current_card_snapshot")),
        "current_dossier_snapshot": deepcopy(component.get("current_dossier_snapshot")),
        "source_blocks": evidence,
        "prior_evidence_expansion": expansion,
        "authority_policy": {
            "identity_authority_granted": False,
            "claim_authority_granted": False,
            "decision_scope": "single_component_proposal_for_code_application",
        },
    }
    return _prepared_request(
        role="cross_chapter_stable_claim_hearing",
        prompt_id=STABLE_CLAIM_PROMPT_ID,
        prompt=prompt,
        component=component,
        queue=queue,
        sections=sections,
        response_schema=stable_claim_hearing_response_schema_v1(),
        model_contract=model_contract,
    )


# ---------------------------------------------------------------------------
# response schemas and validators (for the later consumer; no call happens here)
# ---------------------------------------------------------------------------


def identity_hearing_response_schema_v1() -> dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": False,
        "required": [
            "component_id",
            "verdict",
            "evidence",
            "reason",
            "resolution_condition",
        ],
        "properties": {
            "component_id": {"type": "string", "minLength": 1},
            "verdict": {"type": "string", "minLength": 1},
            "merge_target_prior_card_id": {"type": ["string", "null"]},
            "excluded_prior_card_ids": {
                "type": "array",
                "uniqueItems": True,
                "items": {"type": "string", "minLength": 1},
            },
            "field_adjudications": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["field", "resolution"],
                    "properties": {
                        "field": {"type": "string", "minLength": 1},
                        "resolution": {"type": "string", "minLength": 1},
                        "value": {"type": ["string", "null"]},
                        "basis": {"type": ["string", "null"]},
                    },
                },
            },
            "evidence": {
                "type": "array",
                "minItems": 1,
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["block_id", "quote"],
                    "properties": {
                        "block_id": {"type": "string", "minLength": 1},
                        "quote": {"type": "string", "minLength": 1},
                        "supports_excluded_prior_card_ids": {
                            "type": "array",
                            "uniqueItems": True,
                            "items": {"type": "string", "minLength": 1},
                        },
                    },
                },
            },
            "reason": {"type": "string", "minLength": 1},
            "resolution_condition": {"type": ["string", "null"]},
        },
    }


def stable_claim_hearing_response_schema_v1() -> dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": False,
        "required": [
            "component_id",
            "verdict",
            "evidence",
            "reason",
            "resolution_condition",
        ],
        "properties": {
            "component_id": {"type": "string", "minLength": 1},
            "verdict": {"type": "string", "minLength": 1},
            "effective_from_block_id": {"type": ["string", "null"]},
            "revealed_at_block_id": {"type": ["string", "null"]},
            "corrected_value": {"type": ["string", "null"]},
            "evidence": {
                "type": "array",
                "minItems": 1,
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["block_id", "quote"],
                    "properties": {
                        "block_id": {"type": "string", "minLength": 1},
                        "quote": {"type": "string", "minLength": 1},
                    },
                },
            },
            "reason": {"type": "string", "minLength": 1},
            "resolution_condition": {"type": ["string", "null"]},
        },
    }


def validate_identity_hearing_response_v1(
    response: Mapping[str, Any],
    *,
    component: Mapping[str, Any],
    supplied_block_ids: Sequence[str],
) -> dict[str, Any]:
    allowed = set(allowed_verdicts_for_component_v1(component))
    row = _exact_response_keys(
        response,
        required={
            "component_id",
            "verdict",
            "evidence",
            "reason",
            "resolution_condition",
        },
        optional={
            "merge_target_prior_card_id",
            "excluded_prior_card_ids",
            "field_adjudications",
        },
    )
    _require_component_echo(row, component)
    verdict = _required_string(row.get("verdict"), "hearing verdict")
    if verdict not in allowed:
        raise B1CrossChapterAuditBridgeError(
            "hearing verdict is outside the allowed set for this component"
        )
    candidate_ids = set(_prior_candidate_ids(component))
    if not candidate_ids:
        raise B1CrossChapterAuditBridgeError(
            "identity hearing component supplies no prior candidates"
        )
    # Both verdicts that bind two records into one must name exactly one of the
    # candidates considered in this hearing.
    if verdict in {"merge_referents", "alias_confirmed"}:
        target = row.get("merge_target_prior_card_id")
        if target not in candidate_ids:
            raise B1CrossChapterAuditBridgeError(
                "merge verdict must name one supplied prior card candidate"
            )
    elif row.get("merge_target_prior_card_id") not in (None, ""):
        raise B1CrossChapterAuditBridgeError(
            "merge target is only legal on a merge verdict"
        )
    excluded = _validate_excluded_prior_candidates(
        row,
        verdict=verdict,
        candidate_ids=candidate_ids,
    )
    _validate_evidence_rows(
        row,
        verdict=verdict,
        supplied_block_ids=supplied_block_ids,
    )
    resolution_condition = _validate_resolution_condition(row, verdict)
    return {
        "component_id": row["component_id"],
        "verdict": verdict,
        "merge_target_prior_card_id": row.get("merge_target_prior_card_id"),
        "excluded_prior_card_ids": excluded,
        "field_adjudications": deepcopy(row.get("field_adjudications") or []),
        "evidence": deepcopy(row.get("evidence") or []),
        "reason": _required_string(row.get("reason"), "hearing reason"),
        "resolution_condition": resolution_condition,
        "identity_authority_granted": False,
        "claim_authority_granted": False,
    }


def validate_stable_claim_hearing_response_v1(
    response: Mapping[str, Any],
    *,
    component: Mapping[str, Any],
    supplied_block_ids: Sequence[str],
) -> dict[str, Any]:
    row = _exact_response_keys(
        response,
        required={
            "component_id",
            "verdict",
            "evidence",
            "reason",
            "resolution_condition",
        },
        optional={"effective_from_block_id", "revealed_at_block_id", "corrected_value"},
    )
    _require_component_echo(row, component)
    verdict = _required_string(row.get("verdict"), "hearing verdict")
    if verdict not in STABLE_CLAIM_VERDICTS:
        raise B1CrossChapterAuditBridgeError(
            "stable-claim verdict is outside the closed set"
        )
    supplied = set(supplied_block_ids)
    anchor_rules = {
        "in_story_change": "effective_from_block_id",
        "reveal_only": "revealed_at_block_id",
    }
    for rule_verdict, anchor_field in anchor_rules.items():
        anchor = row.get(anchor_field)
        if verdict == rule_verdict:
            if not isinstance(anchor, str) or anchor not in supplied:
                raise B1CrossChapterAuditBridgeError(
                    f"{rule_verdict} requires {anchor_field} from the supplied blocks"
                )
        elif anchor not in (None, ""):
            raise B1CrossChapterAuditBridgeError(
                f"{anchor_field} is only legal on a {rule_verdict} verdict"
            )
    _validate_evidence_rows(
        row,
        verdict=verdict,
        supplied_block_ids=supplied_block_ids,
    )
    resolution_condition = _validate_resolution_condition(row, verdict)
    return {
        "component_id": row["component_id"],
        "verdict": verdict,
        "effective_from_block_id": row.get("effective_from_block_id"),
        "revealed_at_block_id": row.get("revealed_at_block_id"),
        "corrected_value": row.get("corrected_value"),
        "evidence": deepcopy(row.get("evidence") or []),
        "reason": _required_string(row.get("reason"), "hearing reason"),
        "resolution_condition": resolution_condition,
        "identity_authority_granted": False,
        "claim_authority_granted": False,
    }


# ---------------------------------------------------------------------------
# dry run
# ---------------------------------------------------------------------------


def build_cross_chapter_audit_dry_run_v1(
    *,
    queue: Mapping[str, Any],
    source_blocks: Mapping[str, str],
    design_doc: Path | str,
    model_contract: Mapping[str, Any],
    expected_registry_hash: str | None = None,
    expand_prior_evidence: bool = False,
) -> dict[str, Any]:
    verify_hearing_queue_binding_v1(
        queue, expected_registry_hash=expected_registry_hash
    )
    partition = partition_hearing_queue_v1(queue)
    prepared: list[dict[str, Any]] = []
    for component in partition["ready_identity"]:
        prepared.append(
            render_identity_hearing_request_v1(
                component,
                queue=queue,
                source_blocks=source_blocks,
                design_doc=design_doc,
                model_contract=model_contract,
                expand_prior_evidence=expand_prior_evidence,
            )
        )
    for component in partition["ready_stable_claim"]:
        prepared.append(
            render_stable_claim_hearing_request_v1(
                component,
                queue=queue,
                source_blocks=source_blocks,
                design_doc=design_doc,
                model_contract=model_contract,
                expand_prior_evidence=expand_prior_evidence,
            )
        )
    prepared.sort(key=lambda row: row["component_id"])
    body = {
        "schema_version": BRIDGE_SCHEMA_VERSION,
        "chapter_id": queue.get("chapter_id"),
        "queue_hash": queue.get("queue_hash"),
        "registry_hash": queue.get("registry_hash"),
        "prepared_requests": prepared,
        "waiting_components": [
            {
                "component_id": row["component_id"],
                "review_route": row["review_route"],
                "question_type": row.get("question_type"),
                "lifecycle_state": row["lifecycle_state"],
            }
            for row in partition["waiting"]
        ],
        "unconsumed_routes": partition["unconsumed_ready"],
        "coverage": {
            "component_count": len(partition["covered_component_ids"]),
            "prepared_count": len(prepared),
            "waiting_count": len(partition["waiting"]),
            "unconsumed_ready_count": sum(
                len(rows) for rows in partition["unconsumed_ready"].values()
            ),
            "covered_component_ids": partition["covered_component_ids"],
        },
        "provider_calls": 0,
        "prior_evidence_expansion_enabled": bool(expand_prior_evidence),
        "identity_authority_granted": False,
        "claim_authority_granted": False,
        "registry_mutation_performed": False,
        "production_publish_performed": False,
    }
    return {**body, "report_hash": canonical_hash(body)}


# ---------------------------------------------------------------------------
# internals
# ---------------------------------------------------------------------------


def _prepared_request(
    *,
    role: str,
    prompt_id: str,
    prompt: str,
    component: Mapping[str, Any],
    queue: Mapping[str, Any],
    sections: Mapping[str, Any],
    response_schema: Mapping[str, Any],
    model_contract: Mapping[str, Any],
) -> dict[str, Any]:
    prompt_sha = hashlib.sha256(prompt.encode("utf-8")).hexdigest()
    payload = {
        "schema_version": REQUEST_SCHEMA_VERSION,
        "role": role,
        "chapter_id": queue.get("chapter_id"),
        "allowlisted_sections": sections,
    }
    schema_hash = canonical_hash(dict(response_schema))
    fingerprint = canonical_hash(
        {
            "request_schema_version": REQUEST_SCHEMA_VERSION,
            "component_id": component["component_id"],
            "queue_hash": queue.get("queue_hash"),
            "prompt_id": prompt_id,
            "prompt_sha256": prompt_sha,
            "response_schema_hash": schema_hash,
            "model_contract": deepcopy(dict(model_contract)),
            "sections_hash": canonical_hash(dict(sections)),
        }
    )
    return {
        "schema_version": REQUEST_SCHEMA_VERSION,
        "role": role,
        "component_id": component["component_id"],
        "review_route": component["review_route"],
        "question_type": component.get("question_type"),
        "queue_hash": queue.get("queue_hash"),
        "registry_hash": queue.get("registry_hash"),
        "prompt_id": prompt_id,
        "prompt_sha256": prompt_sha,
        "response_schema": deepcopy(dict(response_schema)),
        "response_schema_hash": schema_hash,
        "model_contract": deepcopy(dict(model_contract)),
        "sections": deepcopy(dict(sections)),
        "messages": [
            {"role": "system", "content": prompt},
            {"role": "user", "content": canonical_json(payload)},
        ],
        "request_fingerprint": fingerprint,
        "provider_calls": 0,
        "identity_authority_granted": False,
        "claim_authority_granted": False,
    }


def _cited_block_ids_identity(component: Mapping[str, Any]) -> list[str]:
    cited: set[str] = set(
        _string_values(component.get("source_block_ids"), "source_block_ids")
    )
    for prior in _prior_candidate_snapshots(component):
        for block_id in prior.get("support_block_ids") or []:
            if isinstance(block_id, str) and block_id:
                cited.add(block_id)
        for ref in prior.get("provenance_refs") or []:
            if isinstance(ref, Mapping) and isinstance(ref.get("block_id"), str):
                cited.add(ref["block_id"])
    if not cited:
        raise B1CrossChapterAuditBridgeError(
            "identity hearing component cites no source blocks"
        )
    return sorted(cited)


def _prior_candidate_ids(component: Mapping[str, Any]) -> list[str]:
    plural = component.get("prior_card_ids")
    if isinstance(plural, list):
        return sorted(set(_string_values(plural, "prior_card_ids")))
    singular = component.get("prior_card_id")
    return [_required_string(singular, "prior_card_id")] if singular else []


def _prior_candidate_snapshots(
    component: Mapping[str, Any],
) -> list[dict[str, Any]]:
    plural = component.get("prior_candidate_snapshots")
    if isinstance(plural, list):
        rows = [
            deepcopy(dict(row))
            for row in plural
            if isinstance(row, Mapping)
        ]
    else:
        singular = component.get("prior_card_snapshot")
        rows = [deepcopy(dict(singular))] if isinstance(singular, Mapping) else []
    ids = _prior_candidate_ids(component)
    observed = [
        _required_string(row.get("prior_card_id"), "prior_card_id") for row in rows
    ]
    if ids and rows and sorted(observed) != ids:
        raise B1CrossChapterAuditBridgeError(
            "prior candidate ids differ from their supplied snapshots"
        )
    return sorted(rows, key=lambda row: row["prior_card_id"])


def _prior_anchor_block_ids(
    snapshots: Sequence[Mapping[str, Any]],
) -> list[str]:
    anchors: set[str] = set()
    for prior in snapshots:
        anchors.update(
            block_id
            for block_id in prior.get("support_block_ids") or []
            if isinstance(block_id, str) and block_id
        )
        anchors.update(
            ref["block_id"]
            for ref in prior.get("provenance_refs") or []
            if isinstance(ref, Mapping)
            and isinstance(ref.get("block_id"), str)
            and ref.get("block_id")
        )
    return sorted(anchors)


def _cited_block_ids_stable(component: Mapping[str, Any]) -> list[str]:
    cited = set(_string_values(component.get("source_block_ids"), "source_block_ids"))
    prior = component.get("prior_card_snapshot")
    if isinstance(prior, Mapping):
        for ref in prior.get("provenance_refs") or []:
            if isinstance(ref, Mapping) and isinstance(ref.get("block_id"), str):
                cited.add(ref["block_id"])
    if not cited:
        raise B1CrossChapterAuditBridgeError(
            "stable-claim hearing component cites no source blocks"
        )
    return sorted(cited)


def _resolve_hearing_blocks_v1(
    *,
    cited_block_ids: Sequence[str],
    prior_anchor_block_ids: Sequence[str],
    source_blocks: Mapping[str, str],
    registry_surfaces: Sequence[str],
    expand_prior_evidence: bool,
) -> tuple[list[dict[str, str]], dict[str, Any]]:
    base = set(cited_block_ids)
    anchors = [block_id for block_id in prior_anchor_block_ids if block_id in source_blocks]
    missing = sorted(set(prior_anchor_block_ids) - set(anchors))
    context: set[str] = set()
    anchor_not_found = False
    max_distance_used = 0
    if expand_prior_evidence:
        ordered_by_chapter: dict[str, list[str]] = {}
        for block_id in source_blocks:
            chapter_key, _position = _block_order_key(block_id)
            ordered_by_chapter.setdefault(chapter_key, []).append(block_id)
        for chapter_ids in ordered_by_chapter.values():
            chapter_ids.sort(key=lambda block_id: _block_order_key(block_id)[1])
        normalized_surfaces = [
            surface.casefold()
            for surface in registry_surfaces
            if isinstance(surface, str) and surface.strip()
        ]
        for anchor in anchors:
            chapter_key, _position = _block_order_key(anchor)
            chapter_ids = ordered_by_chapter.get(chapter_key) or []
            if anchor not in chapter_ids:
                continue
            index = chapter_ids.index(anchor)
            selected_distance = 2
            base_window = _contiguous_window(chapter_ids, index=index, distance=2)
            if normalized_surfaces and not _window_contains_registry_surface(
                base_window, source_blocks=source_blocks, surfaces=normalized_surfaces
            ):
                found = False
                for distance in (3, 4):
                    candidate = _contiguous_window(
                        chapter_ids, index=index, distance=distance
                    )
                    if _window_contains_registry_surface(
                        candidate,
                        source_blocks=source_blocks,
                        surfaces=normalized_surfaces,
                    ):
                        base_window = candidate
                        selected_distance = distance
                        found = True
                        break
                if not found:
                    anchor_not_found = True
            max_distance_used = max(max_distance_used, selected_distance)
            context.update(base_window)
        context -= base
    all_ids = sorted(base | context, key=_block_order_key)
    rows: list[dict[str, str]] = []
    for block_id in all_ids:
        text = source_blocks.get(block_id)
        if not isinstance(text, str) or not text.strip():
            raise B1CrossChapterAuditBridgeError(
                f"hearing evidence block cannot be resolved: {block_id}"
            )
        rows.append(
            {
                "block_id": block_id,
                "text": text,
                "role": "context" if block_id in context else "card_evidence",
            }
        )
    return rows, {
        "enabled": bool(expand_prior_evidence),
        "prior_anchor_block_ids": sorted(prior_anchor_block_ids),
        "context_block_ids": sorted(context, key=_block_order_key),
        "max_distance_used": max_distance_used,
        "anchor_not_found": anchor_not_found or bool(missing),
        "missing_anchor_block_ids": missing,
        "trimmed": False,
    }


def _block_order_key(block_id: str) -> tuple[str, int]:
    head, separator, tail = block_id.rpartition("_b")
    if separator and tail.isdigit():
        return head, int(tail)
    return block_id, 0


def _contiguous_window(
    chapter_ids: Sequence[str], *, index: int, distance: int
) -> list[str]:
    return list(
        chapter_ids[max(0, index - distance) : min(len(chapter_ids), index + distance + 1)]
    )


def _window_contains_registry_surface(
    block_ids: Sequence[str],
    *,
    source_blocks: Mapping[str, str],
    surfaces: Sequence[str],
) -> bool:
    return any(
        surface in source_blocks[block_id].casefold()
        for block_id in block_ids
        for surface in surfaces
    )


def _validate_evidence_rows(
    row: Mapping[str, Any],
    *,
    verdict: str,
    supplied_block_ids: Sequence[str],
) -> None:
    evidence = row.get("evidence")
    if not isinstance(evidence, list):
        raise B1CrossChapterAuditBridgeError("hearing evidence must be a list")
    supplied = set(supplied_block_ids)
    for item in evidence:
        if not isinstance(item, Mapping):
            raise B1CrossChapterAuditBridgeError("hearing evidence row must be an object")
        block_id = _required_string(item.get("block_id"), "evidence block_id")
        if block_id not in supplied:
            raise B1CrossChapterAuditBridgeError(
                "hearing evidence cites a block outside the supplied packet"
            )
        _required_string(item.get("quote"), "evidence quote")
        support_ids = item.get("supports_excluded_prior_card_ids")
        if support_ids is not None:
            _string_values(
                support_ids,
                "supports_excluded_prior_card_ids",
            )
    if not evidence:
        raise B1CrossChapterAuditBridgeError(
            "every hearing verdict requires at least one evidence quote"
        )


def _validate_excluded_prior_candidates(
    row: Mapping[str, Any],
    *,
    verdict: str,
    candidate_ids: set[str],
) -> list[str]:
    raw = row.get("excluded_prior_card_ids")
    excluded = (
        sorted(set(_string_values(raw, "excluded_prior_card_ids")))
        if raw is not None
        else []
    )
    if verdict != "insufficient_evidence":
        if excluded:
            raise B1CrossChapterAuditBridgeError(
                "excluded prior candidates are only legal on insufficient_evidence"
            )
        return []
    if not set(excluded) <= candidate_ids:
        raise B1CrossChapterAuditBridgeError(
            "excluded prior candidate is outside the supplied candidate set"
        )
    if excluded and set(excluded) == candidate_ids:
        raise B1CrossChapterAuditBridgeError(
            "excluding every candidate requires a distinct verdict"
        )
    evidence = row.get("evidence") or []
    supported = {
        candidate_id
        for item in evidence
        if isinstance(item, Mapping)
        for candidate_id in item.get("supports_excluded_prior_card_ids") or []
        if isinstance(candidate_id, str)
    }
    if supported - set(excluded):
        raise B1CrossChapterAuditBridgeError(
            "exclusion evidence names a candidate not declared excluded"
        )
    if set(excluded) - supported:
        raise B1CrossChapterAuditBridgeError(
            "every excluded prior candidate requires a supporting evidence row"
        )
    return excluded


def _validate_resolution_condition(
    row: Mapping[str, Any], verdict: str
) -> str | None:
    condition = row.get("resolution_condition")
    if verdict == "insufficient_evidence":
        return _required_string(
            condition,
            "insufficient-evidence resolution_condition",
        )
    if condition is not None:
        raise B1CrossChapterAuditBridgeError(
            "resolution_condition is only legal on an insufficient_evidence verdict"
        )
    return None


def _require_component_echo(
    row: Mapping[str, Any], component: Mapping[str, Any]
) -> None:
    if row.get("component_id") != component.get("component_id"):
        raise B1CrossChapterAuditBridgeError(
            "hearing response does not echo the supplied component id"
        )


def _require_route(component: Mapping[str, Any], route: str) -> None:
    if component.get("review_route") != route:
        raise B1CrossChapterAuditBridgeError(
            f"component is not routed to {route}"
        )
    if component.get("lifecycle_state") != "ready_for_hearing":
        raise B1CrossChapterAuditBridgeError(
            "only ready_for_hearing components may be rendered"
        )


def _exact_response_keys(
    response: Mapping[str, Any],
    *,
    required: set[str],
    optional: set[str],
) -> dict[str, Any]:
    if not isinstance(response, Mapping):
        raise B1CrossChapterAuditBridgeError("hearing response must be an object")
    keys = set(response.keys())
    missing = required - keys
    unknown = keys - required - optional
    if missing:
        raise B1CrossChapterAuditBridgeError(
            f"hearing response is missing keys: {sorted(missing)}"
        )
    if unknown:
        raise B1CrossChapterAuditBridgeError(
            f"hearing response carries unknown keys: {sorted(unknown)}"
        )
    return dict(deepcopy(dict(response)))


def _components(queue: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    raw = queue.get("components")
    if not isinstance(raw, list):
        raise B1CrossChapterAuditBridgeError("hearing queue components must be a list")
    rows: list[Mapping[str, Any]] = []
    for row in raw:
        if not isinstance(row, Mapping):
            raise B1CrossChapterAuditBridgeError(
                "hearing queue contains a non-object component"
            )
        rows.append(row)
    return rows


def _string_values(value: Any, label: str) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise B1CrossChapterAuditBridgeError(f"{label} must be a list")
    rows: list[str] = []
    for item in value:
        if not isinstance(item, str) or not item:
            raise B1CrossChapterAuditBridgeError(f"{label} entries must be strings")
        rows.append(item)
    return rows


def _required_string(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise B1CrossChapterAuditBridgeError(f"{label} must be a non-empty string")
    return value


__all__ = [
    "BRIDGE_SCHEMA_VERSION",
    "REQUEST_SCHEMA_VERSION",
    "IDENTITY_PROMPT_ID",
    "STABLE_CLAIM_PROMPT_ID",
    "IDENTITY_LINKAGE_VERDICTS",
    "ALIAS_REFERRAL_VERDICTS",
    "SPURIOUS_REFERRAL_VERDICTS",
    "STABLE_CLAIM_VERDICTS",
    "B1CrossChapterAuditBridgeError",
    "verify_hearing_queue_binding_v1",
    "partition_hearing_queue_v1",
    "allowed_verdicts_for_component_v1",
    "render_identity_hearing_request_v1",
    "render_stable_claim_hearing_request_v1",
    "identity_hearing_response_schema_v1",
    "stable_claim_hearing_response_schema_v1",
    "validate_identity_hearing_response_v1",
    "validate_stable_claim_hearing_response_v1",
    "build_cross_chapter_audit_dry_run_v1",
]
