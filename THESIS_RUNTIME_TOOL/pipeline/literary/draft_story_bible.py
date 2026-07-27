"""Offline-only S5C diagnostic Story Bible assembly.

The module deliberately has no provider client and never publishes production
state.  It turns validated Step-4 occurrences plus recorded model judgments
into a deterministic, append-only diagnostic artifact.
"""

from __future__ import annotations

from collections import defaultdict
from copy import deepcopy
from dataclasses import dataclass
from hashlib import sha256
import json
import math
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence

from pipeline.literary.builder_pilot import load_system_prompt_from_design
from pipeline.literary.checkpoint import canonical_hash, canonical_json
from pipeline.literary.checkpoint_v3 import write_json_exclusive
from pipeline.literary.step5c_slice import (
    build_proposal_request_payload,
    build_retrieval_request_payload,
    render_identity_slice_request,
    split_target_batches,
)


DRAFT_CONTRACT_VERSION = "literary_s5c_diagnostic_draft_v2"
DRAFT_ARTIFACT_SCHEMA_VERSION = "literary_diagnostic_story_bible_v2"
DRAFT_REPORT_PREFIX = "literary_m4f_s5c_draft_bible"
PHASE_PROMPT_ID = "literary_phase_segment_draft_v1"
_EXPECTED_BUNDLE_SCHEMA_VERSION = "literary_b4_input_bundle_v3"
_EXPECTED_HANDOFF_CONTRACT_VERSION = "literary_b4_handoff_contract_v3"

_ENTITY_KINDS = frozenset({"person", "animal", "nonhuman_character", "unknown"})
_NON_ENTITY_KINDS = frozenset({"place", "object", "group_reference"})
_PHASE_LABELS = frozenset(
    {"allied", "friendly", "neutral", "strained", "hostile", "estranged", "dependent", "reconciled"}
)
_EVENT_OUTCOMES = frozenset({"phase_support", "fact_support", "no_change", "blocked"})
_PREDICATES = frozenset(
    {
        "parent_of",
        "child_of",
        "spouse_of",
        "sibling_of",
        "daughter_in_law_of",
        "son_in_law_of",
        "father_in_law_of",
        "mother_in_law_of",
        "grandparent_of",
        "grandchild_of",
        "cousin_of",
        "servant_of",
        "master_of",
        "landlord_of",
        "tenant_of",
        "guest_of",
        "neighbor_of",
        "guardian_of",
        "ward_of",
        "other",
    }
)


class DraftBibleError(ValueError):
    """Raised when an offline draft invariant is violated."""


@dataclass(frozen=True, slots=True)
class IdentityShardPlan:
    shard_id: str
    target_ids: tuple[str, ...]
    prompt_tokens: int
    response_floor_tokens: int
    max_output_tokens: int
    request_fingerprint: str
    payload: dict[str, Any]
    request_body: dict[str, Any]


@dataclass(frozen=True, slots=True)
class PhaseDraftRequest:
    canonical_request_json: str
    request_fingerprint: str

    def body(self) -> dict[str, Any]:
        value = json.loads(self.canonical_request_json)
        if not isinstance(value, dict):
            raise DraftBibleError("phase draft request must be an object")
        return value


def _clone(value: Any) -> Any:
    return json.loads(canonical_json(value))


def _require_mapping(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise DraftBibleError(f"{label} must be an object")
    return value


def _require_list(value: Any, label: str) -> list[Any]:
    if not isinstance(value, list):
        raise DraftBibleError(f"{label} must be a list")
    return value


def _exact_keys(value: Mapping[str, Any], required: set[str], optional: set[str], label: str) -> None:
    keys = set(value)
    if not required <= keys or not keys <= required | optional:
        raise DraftBibleError(
            f"{label} fields mismatch: required={sorted(required)}, optional={sorted(optional)}, got={sorted(keys)}"
        )


def _verify_bundle_identity(bundle: Mapping[str, Any]) -> None:
    body = _clone(dict(bundle))
    expected = str(body.pop("bundle_manifest_hash", ""))
    if not expected or canonical_hash(body) != expected:
        raise DraftBibleError("Step-4 bundle manifest hash mismatch")
    if not str(bundle.get("state_lineage_id") or ""):
        raise DraftBibleError("Step-4 bundle lacks state_lineage_id")
    if bundle.get("schema_version") != _EXPECTED_BUNDLE_SCHEMA_VERSION:
        raise DraftBibleError("Step-4 bundle schema identity mismatch")
    if bundle.get("handoff_contract_version") != _EXPECTED_HANDOFF_CONTRACT_VERSION:
        raise DraftBibleError("Step-4 bundle handoff contract mismatch")
    ground = _require_mapping(bundle.get("ground_evidence"), "ground_evidence")
    if "cast_claim_inputs" in ground:
        raise DraftBibleError("retired cast_claim_inputs channel is forbidden")


def load_phase_draft_prompt(design_doc: Path) -> str:
    prompt = load_system_prompt_from_design(Path(design_doc), PHASE_PROMPT_ID)
    if f"Prompt version: {PHASE_PROMPT_ID}." not in prompt:
        raise DraftBibleError("phase draft prompt marker mismatch")
    required = ("considered_event_ids", "event_dispositions", "inference_basis", "valid_until_block")
    if any(token not in prompt for token in required):
        raise DraftBibleError("phase draft prompt lacks required contract text")
    return prompt


def _card_index(bundle: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    cards: dict[str, dict[str, Any]] = {}
    for raw in _require_list(bundle.get("occurrence_cards"), "occurrence_cards"):
        row = dict(_require_mapping(raw, "occurrence card"))
        occurrence_id = str(row.get("occurrence_id") or "")
        if not occurrence_id or occurrence_id in cards:
            raise DraftBibleError(f"duplicate/empty occurrence card: {occurrence_id!r}")
        cards[occurrence_id] = _clone(row)
    return cards


def _routing_index(bundle: Mapping[str, Any]) -> dict[str, str]:
    routing = _require_mapping(bundle.get("occurrence_routing"), "occurrence_routing")
    result: dict[str, str] = {}
    for bucket in (
        "person_occurrences",
        "non_person_occurrences",
        "discourse_only",
        "deferred",
        "invalid_flagged",
    ):
        for raw in _require_list(routing.get(bucket), f"routing.{bucket}"):
            row = _require_mapping(raw, f"routing.{bucket} row")
            occurrence_id = str(row.get("occurrence_id") or "")
            if not occurrence_id or occurrence_id in result:
                raise DraftBibleError(f"routing is not a unique cover: {occurrence_id!r}")
            result[occurrence_id] = bucket
    return result


def _occurrence_order(card: Mapping[str, Any], chapter_order: Mapping[str, int]) -> tuple[Any, ...]:
    anchor = _require_mapping(card.get("anchor"), "occurrence anchor")
    return (
        chapter_order.get(str(card.get("chapter_id") or ""), 10**9),
        int(card.get("block_order") or 0),
        int(anchor.get("char_start") or 0),
        int(anchor.get("char_end") or 0),
        str(card.get("occurrence_id") or ""),
    )


def build_identity_target_manifest(bundle: Mapping[str, Any]) -> dict[str, Any]:
    """Build the exact owned-target universe without trusting surface semantics."""

    _verify_bundle_identity(bundle)
    cards = _card_index(bundle)
    routing = _routing_index(bundle)
    if set(cards) != set(routing):
        raise DraftBibleError("occurrence cards and routing do not exact-cover each other")
    rows: list[dict[str, Any]] = []
    excluded: list[dict[str, Any]] = []
    for occurrence_id, card in cards.items():
        bucket = routing[occurrence_id]
        kind = str(card.get("referent_kind_claim") or "unknown")
        reason: str | None = None
        if bucket == "person_occurrences":
            reason = "person_occurrence"
        elif bucket == "deferred":
            reason = "deferred_requires_adjudication"
        elif bucket == "non_person_occurrences" and kind in {"animal", "nonhuman_character"}:
            reason = f"character_like_{kind}"
        projection = {
            "occurrence_id": occurrence_id,
            "occurrence_kind": str(card.get("occurrence_kind") or ""),
            "routing_bucket": bucket,
            "referent_kind_claim": kind,
            "chapter_id": str(card.get("chapter_id") or ""),
            "block_id": str(card.get("block_id") or ""),
            "inclusion_reason": reason,
            "source_row_hash": canonical_hash(card),
        }
        (rows if reason else excluded).append(projection)
    chapter_ids = [str(row.get("parent_chapter") or "") for row in bundle.get("unit_manifest") or []]
    chapter_order = {chapter_id: index for index, chapter_id in enumerate(chapter_ids)}
    rows.sort(key=lambda row: _occurrence_order(cards[row["occurrence_id"]], chapter_order))
    excluded.sort(key=lambda row: _occurrence_order(cards[row["occurrence_id"]], chapter_order))
    payload = {
        "contract_version": DRAFT_CONTRACT_VERSION,
        "state_lineage_id": str(bundle.get("state_lineage_id") or ""),
        "owned_targets": rows,
        "excluded_occurrences": excluded,
        "counts": {
            "owned_total": len(rows),
            "owned_mentions": sum(row["occurrence_kind"] == "mention" for row in rows),
            "owned_endpoints": sum(row["occurrence_kind"] == "endpoint" for row in rows),
            "excluded_total": len(excluded),
        },
    }
    return {**payload, "manifest_hash": canonical_hash(payload)}


def _ground_rows(bundle: Mapping[str, Any]) -> list[dict[str, Any]]:
    ground = _require_mapping(bundle.get("ground_evidence"), "ground_evidence")
    rows: list[dict[str, Any]] = []
    for channel, values in ground.items():
        if channel == "ground_manifest_hash":
            continue
        if isinstance(values, list):
            for raw in values:
                row = dict(_require_mapping(raw, f"ground_evidence.{channel} row"))
                row.setdefault("channel", channel)
                rows.append(_clone(row))
    return rows


def _ref_ids(row: Mapping[str, Any]) -> set[str]:
    return {
        str(ref.get("ref_id") or "")
        for ref in row.get("evidence_refs") or []
        if isinstance(ref, Mapping) and str(ref.get("ref_id") or "")
    }


def build_bounded_retrieval_payload(
    bundle: Mapping[str, Any], *, target_ids: Sequence[str], frame: Any = None
) -> dict[str, Any]:
    """Project retrieval input to owned target evidence while keeping the full light roster."""

    payload = build_retrieval_request_payload(bundle, target_occurrence_ids=target_ids, frame=frame)
    target_set = set(target_ids)
    ground = [
        row
        for row in _ground_rows(bundle)
        if _ref_ids(row) & target_set
    ]
    payload["evidence_items"] = ground
    payload["evidence_ref_universe"] = sorted(
        str(row.get("ground_item_id") or "") for row in ground if str(row.get("ground_item_id") or "")
    )
    payload["evidence_ref_universe_hash"] = canonical_hash(payload["evidence_ref_universe"])
    return _clone(payload)


def build_bounded_proposal_payload(
    bundle: Mapping[str, Any],
    *,
    retrieval: Mapping[str, Any],
    target_ids: Sequence[str] | None = None,
    frame: Any = None,
) -> dict[str, Any]:
    payload = build_proposal_request_payload(bundle, retrieval=retrieval, frame=frame)
    selected = set(target_ids or payload.get("target_ids") or [])
    rows = [row for row in payload.get("targets") or [] if str(row.get("target_occurrence_id") or "") in selected]
    if set(str(row.get("target_occurrence_id") or "") for row in rows) != selected:
        raise DraftBibleError("proposal shard target projection is not an exact cover")
    occurrence_ids = {
        str(value)
        for row in rows
        for value in row.get("selection_occurrence_ids") or []
    }
    ground = [
        row
        for row in _ground_rows(bundle)
        if _ref_ids(row) & occurrence_ids
    ]
    payload["targets"] = rows
    payload["target_ids"] = [str(row["target_occurrence_id"]) for row in rows]
    payload["evidence_items"] = ground
    payload["evidence_ref_universe"] = sorted(
        str(row.get("ground_item_id") or "") for row in ground if str(row.get("ground_item_id") or "")
    )
    payload["evidence_ref_universe_hash"] = canonical_hash(payload["evidence_ref_universe"])
    return _clone(payload)


def _json_token_floor(value: Any) -> int:
    return max(1, math.ceil(len(canonical_json(value).encode("utf-8")) / 4))


def _retrieval_response_floor(target_ids: Sequence[str]) -> dict[str, Any]:
    return {
        "targets": [
            {
                "target_occurrence_id": target,
                "candidate_occurrence_ids": [],
                "status": "unknown",
                "evidence_refs": [],
            }
            for target in target_ids
        ]
    }


def select_output_cap(
    response_floor: Mapping[str, Any],
    *,
    allowed_caps: Sequence[int] = (3072, 6144, 12288),
    safety_factor: float = 1.75,
    fixed_overhead: int = 256,
) -> tuple[int, int]:
    floor = _json_token_floor(response_floor)
    required = math.ceil(floor * safety_factor) + fixed_overhead
    for cap in allowed_caps:
        if cap >= required:
            return int(cap), floor
    raise DraftBibleError(f"response lower bound exceeds allowed output caps: floor={floor}, required={required}")


def plan_identity_retrieval_shards(
    bundle: Mapping[str, Any],
    *,
    prompt_text: str,
    provider: str,
    model_config: Mapping[str, Any],
    prompt_token_cap: int,
    estimate_tokens: Callable[[Mapping[str, Any]], int] | None = None,
    frame: Any = None,
) -> list[IdentityShardPlan]:
    manifest = build_identity_target_manifest(bundle)
    target_ids = [row["occurrence_id"] for row in manifest["owned_targets"]]
    if not target_ids:
        raise DraftBibleError("identity target universe is empty")
    estimator = estimate_tokens or (lambda value: _json_token_floor(value))

    def render(ids: Sequence[str]) -> Mapping[str, Any]:
        payload = build_bounded_retrieval_payload(bundle, target_ids=ids, frame=frame)
        request = render_identity_slice_request(
            role="retrieval",
            payload=payload,
            prompt_text=prompt_text,
            provider=provider,
            model_config=model_config,
            upstream_lineage_hashes={
                "state_lineage_id": str(bundle.get("state_lineage_id") or ""),
                "bundle_manifest_hash": str(bundle.get("bundle_manifest_hash") or ""),
            },
        )
        return request.body()

    try:
        batches = split_target_batches(
            target_ids,
            render_payload=render,
            prompt_token_cap=prompt_token_cap,
            estimate_tokens=estimator,
        )
    except Exception as exc:
        raise DraftBibleError(str(exc)) from exc
    plans: list[IdentityShardPlan] = []
    for index, batch in enumerate(batches, start=1):
        payload = build_bounded_retrieval_payload(bundle, target_ids=batch, frame=frame)
        cap, floor = select_output_cap(_retrieval_response_floor(batch))
        capped_config = _clone(dict(model_config))
        capped_config["max_output_tokens"] = cap
        request = render_identity_slice_request(
            role="retrieval",
            payload=payload,
            prompt_text=prompt_text,
            provider=provider,
            model_config=capped_config,
            upstream_lineage_hashes={
                "state_lineage_id": str(bundle.get("state_lineage_id") or ""),
                "bundle_manifest_hash": str(bundle.get("bundle_manifest_hash") or ""),
            },
        )
        plans.append(
            IdentityShardPlan(
                shard_id=f"identity_retrieval_{index:03d}",
                target_ids=tuple(batch),
                prompt_tokens=int(estimator(request.body())),
                response_floor_tokens=floor,
                max_output_tokens=cap,
                request_fingerprint=request.request_fingerprint,
                payload=_clone(payload),
                request_body=request.body(),
            )
        )
    flattened = [target for plan in plans for target in plan.target_ids]
    if flattened != target_ids:
        raise DraftBibleError("identity shard plan is not an ordered exact cover")
    return plans


def plan_identity_proposal_shards(
    bundle: Mapping[str, Any],
    *,
    retrieval: Mapping[str, Any],
    prompt_text: str,
    provider: str,
    model_config: Mapping[str, Any],
    prompt_token_cap: int,
    retrieval_response_hash: str,
    estimate_tokens: Callable[[Mapping[str, Any]], int] | None = None,
    frame: Any = None,
) -> list[IdentityShardPlan]:
    full = build_bounded_proposal_payload(bundle, retrieval=retrieval, frame=frame)
    target_ids = list(full.get("target_ids") or [])
    if not target_ids:
        return []
    estimator = estimate_tokens or (lambda value: _json_token_floor(value))

    def render(ids: Sequence[str]) -> Mapping[str, Any]:
        payload = build_bounded_proposal_payload(bundle, retrieval=retrieval, target_ids=ids, frame=frame)
        return render_identity_slice_request(
            role="proposal",
            payload=payload,
            prompt_text=prompt_text,
            provider=provider,
            model_config=model_config,
            upstream_lineage_hashes={
                "state_lineage_id": str(bundle.get("state_lineage_id") or ""),
                "bundle_manifest_hash": str(bundle.get("bundle_manifest_hash") or ""),
                "retrieval_response_hash": retrieval_response_hash,
            },
        ).body()

    try:
        batches = split_target_batches(
            target_ids,
            render_payload=render,
            prompt_token_cap=prompt_token_cap,
            estimate_tokens=estimator,
        )
    except Exception as exc:
        raise DraftBibleError(str(exc)) from exc
    plans: list[IdentityShardPlan] = []
    for index, batch in enumerate(batches, start=1):
        payload = build_bounded_proposal_payload(bundle, retrieval=retrieval, target_ids=batch, frame=frame)
        floor_response = {
            "proposals": [
                {
                    "target_occurrence_id": target,
                    "status": "unknown",
                    "same_referent_occurrence_ids": [],
                    "different_referent_occurrence_ids": [],
                    "referent_kind": "unknown",
                    "canonical_surface_guess": "",
                    "evidence_refs": [],
                }
                for target in batch
            ]
        }
        cap, floor = select_output_cap(floor_response)
        capped_config = _clone(dict(model_config))
        capped_config["max_output_tokens"] = cap
        request = render_identity_slice_request(
            role="proposal",
            payload=payload,
            prompt_text=prompt_text,
            provider=provider,
            model_config=capped_config,
            upstream_lineage_hashes={
                "state_lineage_id": str(bundle.get("state_lineage_id") or ""),
                "bundle_manifest_hash": str(bundle.get("bundle_manifest_hash") or ""),
                "retrieval_response_hash": retrieval_response_hash,
            },
        )
        plans.append(
            IdentityShardPlan(
                shard_id=f"identity_proposal_{index:03d}",
                target_ids=tuple(batch),
                prompt_tokens=int(estimator(request.body())),
                response_floor_tokens=floor,
                max_output_tokens=cap,
                request_fingerprint=request.request_fingerprint,
                payload=_clone(payload),
                request_body=request.body(),
            )
        )
    if [target for plan in plans for target in plan.target_ids] != target_ids:
        raise DraftBibleError("identity proposal shard plan is not an ordered exact cover")
    return plans


class _UnionFind:
    def __init__(self, values: Iterable[str]) -> None:
        self.parent = {value: value for value in values}

    def find(self, value: str) -> str:
        parent = self.parent[value]
        if parent != value:
            self.parent[value] = self.find(parent)
        return self.parent[value]

    def union(self, left: str, right: str) -> None:
        a, b = self.find(left), self.find(right)
        if a != b:
            self.parent[max(a, b)] = min(a, b)


def reconcile_identity_claims(
    bundle: Mapping[str, Any],
    *,
    proposal_rows: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Turn target-wise signed claims into a deterministic diagnostic partition."""

    manifest = build_identity_target_manifest(bundle)
    target_ids = [row["occurrence_id"] for row in manifest["owned_targets"]]
    target_set = set(target_ids)
    cards = _card_index(bundle)
    by_target: dict[str, dict[str, Any]] = {}
    foreign_by_target: dict[str, set[str]] = defaultdict(set)
    for raw in proposal_rows:
        row = dict(_require_mapping(raw, "identity proposal row"))
        target = str(row.get("target_occurrence_id") or "")
        if target not in target_set or target in by_target:
            raise DraftBibleError(f"identity proposals are duplicate/foreign: {target!r}")
        status = str(row.get("status") or "")
        same = [str(value) for value in _require_list(row.get("same_referent_occurrence_ids"), "same ids")]
        different = [str(value) for value in _require_list(row.get("different_referent_occurrence_ids"), "different ids")]
        if status not in {"proposed", "unknown"}:
            raise DraftBibleError(f"foreign identity proposal status: {status}")
        if len(same) != len(set(same)) or len(different) != len(set(different)) or set(same) & set(different):
            raise DraftBibleError("identity proposal sets are duplicate or overlapping")
        if status == "unknown" and (same or different or row.get("evidence_refs")):
            raise DraftBibleError("unknown identity proposal must be empty")
        if status == "proposed" and same.count(target) != 1:
            raise DraftBibleError("proposed same set must contain target once")
        foreign_by_target[target].update((set(same) | set(different)) - target_set)
        row["same_referent_occurrence_ids"] = same
        row["different_referent_occurrence_ids"] = different
        by_target[target] = _clone(row)
    if set(by_target) != target_set:
        raise DraftBibleError(
            f"identity proposals do not exact-cover targets: missing={sorted(target_set-set(by_target))}"
        )

    union = _UnionFind(target_ids)
    negative: set[tuple[str, str]] = set()
    for target, row in by_target.items():
        if row["status"] != "proposed":
            continue
        for other in row["same_referent_occurrence_ids"]:
            if other in target_set:
                union.union(target, other)
        for other in row["different_referent_occurrence_ids"]:
            if other in target_set:
                negative.add(tuple(sorted((target, other))))

    components: dict[str, set[str]] = defaultdict(set)
    for target in target_ids:
        components[union.find(target)].add(target)
    chapter_ids = [str(row.get("parent_chapter") or "") for row in bundle.get("unit_manifest") or []]
    chapter_order = {chapter_id: index for index, chapter_id in enumerate(chapter_ids)}
    entities: list[dict[str, Any]] = []
    conflicts: list[dict[str, Any]] = []
    unresolved: set[str] = set()
    occurrence_to_entity: dict[str, str] = {}

    for members in sorted(components.values(), key=lambda values: min(values)):
        reasons: set[str] = set()
        if any(by_target[member]["status"] == "unknown" for member in members):
            if len(members) == 1:
                unresolved.update(members)
                continue
            reasons.add("unknown_member_claimed_same")
        if any(left in members and right in members for left, right in negative):
            reasons.add("internal_different_identity_edge")
        if any(foreign_by_target[member] for member in members):
            reasons.add("foreign_or_unowned_occurrence")
        known_kinds = {
            str(by_target[member].get("referent_kind") or "unknown")
            for member in members
            if by_target[member]["status"] == "proposed"
            and str(by_target[member].get("referent_kind") or "unknown") != "unknown"
        }
        if not known_kinds <= _ENTITY_KINDS:
            reasons.add("non_entity_referent_kind")
        if len(known_kinds) > 1:
            reasons.add("incompatible_referent_kinds")
        ordered_members = sorted(members, key=lambda value: _occurrence_order(cards[value], chapter_order))
        if reasons:
            unresolved.update(members)
            conflict_body = {
                "member_occurrence_ids": ordered_members,
                "reasons": sorted(reasons),
                "source_claims": [by_target[member] for member in ordered_members],
            }
            conflicts.append(
                {
                    "conflict_id": f"idconf_{canonical_hash(conflict_body)[:20]}",
                    **_clone(conflict_body),
                }
            )
            continue
        earliest = ordered_members[0]
        entity_id = "entd_" + sha256(
            canonical_json(
                {
                    "state_lineage_id": str(bundle.get("state_lineage_id") or ""),
                    "earliest_owned_occurrence_id": earliest,
                }
            ).encode("utf-8")
        ).hexdigest()[:20]
        kind = next(iter(known_kinds), "unknown")
        surface_groups: dict[str, list[str]] = defaultdict(list)
        for member in ordered_members:
            surface_groups[str(cards[member].get("surface") or "")].append(member)
            occurrence_to_entity[member] = entity_id
        aliases = [
            {
                "surface": surface,
                "covered_occurrence_ids": occurrence_ids,
                "first_order": list(_occurrence_order(cards[occurrence_ids[0]], chapter_order)[:-1]),
            }
            for surface, occurrence_ids in sorted(
                surface_groups.items(),
                key=lambda item: _occurrence_order(cards[item[1][0]], chapter_order),
            )
        ]
        guesses = [
            {
                "value": str(by_target[member].get("canonical_surface_guess") or ""),
                "source_occurrence_id": member,
            }
            for member in ordered_members
            if str(by_target[member].get("canonical_surface_guess") or "")
        ]
        kind_claims = [
            {
                "referent_kind": str(by_target[member].get("referent_kind") or "unknown"),
                "source_occurrence_id": member,
            }
            for member in ordered_members
            if by_target[member]["status"] == "proposed"
        ]
        entities.append(
            {
                "entity_id": entity_id,
                "display_surface": str(cards[earliest].get("surface") or ""),
                "referent_kind": kind,
                "member_occurrence_ids": ordered_members,
                "aliases": aliases,
                "canonical_surface_guesses_advisory": guesses,
                "referent_kind_claims": kind_claims,
                "status": "diagnostic_draft",
            }
        )
    entities.sort(key=lambda row: _occurrence_order(cards[row["member_occurrence_ids"][0]], chapter_order))
    result = {
        "entities": entities,
        "occurrence_to_entity": dict(sorted(occurrence_to_entity.items())),
        "unresolved_occurrence_ids": sorted(unresolved),
        "identity_conflicts": conflicts,
        "coverage": {
            "owned_total": len(target_ids),
            "resolved": len(occurrence_to_entity),
            "unresolved": len(unresolved),
            "conflicted": len({value for row in conflicts for value in row["member_occurrence_ids"]}),
        },
        "target_manifest_hash": manifest["manifest_hash"],
    }
    return {**result, "identity_result_hash": canonical_hash(result)}


def bind_endpoints(
    bundle: Mapping[str, Any], identity: Mapping[str, Any]
) -> dict[str, Any]:
    cards = _card_index(bundle)
    routing = _routing_index(bundle)
    occurrence_to_entity = {
        str(key): str(value) for key, value in _require_mapping(identity.get("occurrence_to_entity"), "occurrence_to_entity").items()
    }
    conflict_ids = {
        str(value)
        for row in identity.get("identity_conflicts") or []
        for value in row.get("member_occurrence_ids") or []
    }
    rows: list[dict[str, Any]] = []
    counts: dict[str, int] = defaultdict(int)
    role_counts: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for occurrence_id, card in cards.items():
        if card.get("occurrence_kind") != "endpoint":
            continue
        bucket = routing[occurrence_id]
        if occurrence_id in occurrence_to_entity:
            disposition, entity_id = "bound", occurrence_to_entity[occurrence_id]
        elif occurrence_id in conflict_ids:
            disposition, entity_id = "conflict", None
        elif bucket == "non_person_occurrences":
            disposition, entity_id = "non_entity", None
        elif bucket == "discourse_only":
            disposition, entity_id = "discourse_only", None
        else:
            disposition, entity_id = "unresolved", None
        role = str(card.get("owner_role") or "unknown")
        counts[disposition] += 1
        role_counts[role][disposition] += 1
        rows.append(
            {
                "endpoint_id": occurrence_id,
                "owner_id": str(card.get("owner_id") or ""),
                "endpoint_role": role,
                "disposition": disposition,
                "entity_id": entity_id,
                "source_occurrence_hash": canonical_hash(card),
            }
        )
    rows.sort(key=lambda row: row["endpoint_id"])
    result = {
        "endpoint_dispositions": rows,
        "coverage": {
            "total": len(rows),
            "by_disposition": dict(sorted(counts.items())),
            "by_role": {key: dict(sorted(value.items())) for key, value in sorted(role_counts.items())},
        },
    }
    return {**result, "endpoint_binding_hash": canonical_hash(result)}


def _ground_channel(bundle: Mapping[str, Any], name: str) -> list[dict[str, Any]]:
    ground = _require_mapping(bundle.get("ground_evidence"), "ground_evidence")
    return [_clone(_require_mapping(row, f"ground_evidence.{name} row")) for row in _require_list(ground.get(name), name)]


def build_final_pair_batches(
    bundle: Mapping[str, Any], endpoint_bindings: Mapping[str, Any]
) -> dict[str, Any]:
    dispositions = {
        str(row.get("endpoint_id") or ""): row
        for row in endpoint_bindings.get("endpoint_dispositions") or []
    }
    observations = {
        str((row.get("payload") or {}).get("event_id") or ""): row
        for row in _ground_channel(bundle, "phase_observation_inputs")
    }
    block_order = {
        str(row.get("block_id") or ""): int(row.get("order_index") or 0)
        for row in bundle.get("source_block_catalog") or []
    }
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    blocked: list[dict[str, Any]] = []
    seen_events: set[str] = set()
    for envelope in _ground_channel(bundle, "relation_event_inputs"):
        event = _require_mapping(envelope.get("payload"), "relation event payload")
        event_id = str(event.get("event_id") or "")
        if not event_id or event_id in seen_events:
            raise DraftBibleError(f"duplicate/empty relation event: {event_id!r}")
        seen_events.add(event_id)
        actor_id = str((_require_mapping(event.get("actor"), "event actor")).get("endpoint_id") or "")
        target_id = str((_require_mapping(event.get("target"), "event target")).get("endpoint_id") or "")
        actor = dispositions.get(actor_id)
        target = dispositions.get(target_id)
        reason: str | None = None
        if actor is None or target is None:
            reason = "endpoint_missing_from_binding_manifest"
        elif actor.get("disposition") != "bound" or target.get("disposition") != "bound":
            reason = f"endpoint_unresolved:{actor.get('disposition')}:{target.get('disposition')}"
        elif actor.get("entity_id") == target.get("entity_id"):
            reason = "self_pair_after_binding"
        if reason:
            blocked.append({"event_id": event_id, "reason": reason, "ground_item_id": envelope.get("ground_item_id")})
            continue
        pair = tuple(sorted((str(actor["entity_id"]), str(target["entity_id"]))))
        grouped[pair].append(
            {
                "event_id": event_id,
                "block_id": str(event.get("block_id") or ""),
                "event_type": str(event.get("event_type") or ""),
                "evidence_quote": str(event.get("evidence_quote") or ""),
                "actor_endpoint_id": actor_id,
                "target_endpoint_id": target_id,
                "actor_entity_id": str(actor["entity_id"]),
                "target_entity_id": str(target["entity_id"]),
                "phase_observation": _clone((observations.get(event_id) or {}).get("payload")),
                "evidence_refs": _clone(envelope.get("evidence_refs") or []),
            }
        )
    batches = []
    for pair, events in sorted(grouped.items()):
        events.sort(key=lambda row: (block_order.get(row["block_id"], 10**9), row["event_id"]))
        batches.append(
            {
                "pair": {"a_entity_id": pair[0], "b_entity_id": pair[1]},
                "events": events,
                "event_ids": [row["event_id"] for row in events],
            }
        )
    result = {
        "pair_batches": batches,
        "blocked_events": sorted(blocked, key=lambda row: row["event_id"]),
        "coverage": {
            "input_events": len(seen_events),
            "routed_events": sum(len(row["events"]) for row in batches),
            "blocked_events": len(blocked),
        },
    }
    if result["coverage"]["routed_events"] + result["coverage"]["blocked_events"] != len(seen_events):
        raise DraftBibleError("relation-event routing lost an event")
    return {**result, "pair_batch_hash": canonical_hash(result)}


def render_phase_draft_request(
    pair_batch: Mapping[str, Any],
    *,
    prompt_text: str,
    provider: str,
    model_config: Mapping[str, Any],
    upstream_lineage_hashes: Mapping[str, str],
    prior_timeline: Sequence[Mapping[str, Any]] = (),
) -> PhaseDraftRequest:
    if f"Prompt version: {PHASE_PROMPT_ID}." not in prompt_text:
        raise DraftBibleError("phase draft request prompt marker mismatch")
    if not provider.strip() or not str(model_config.get("model") or ""):
        raise DraftBibleError("phase draft request lacks provider/model")
    if not upstream_lineage_hashes or any(not str(value) for value in upstream_lineage_hashes.values()):
        raise DraftBibleError("phase draft request lacks complete upstream lineage")
    pair = _clone(_require_mapping(pair_batch.get("pair"), "pair batch pair"))
    events = _clone(_require_list(pair_batch.get("events"), "pair batch events"))
    model_input = {
        "pair": pair,
        "EVENTS": events,
        "PRIOR_TIMELINE": _clone(list(prior_timeline)),
        "response_schema": {
            "required": ["pair", "considered_event_ids", "event_dispositions", "relation_phases", "relation_facts"]
        },
    }
    config = _clone(dict(model_config))
    messages = [
        {"role": "system", "content": prompt_text},
        {"role": "user", "content": canonical_json(model_input)},
    ]
    body = {
        "contract_version": DRAFT_CONTRACT_VERSION,
        "execution_mode": "real_api",
        "proposal_only": True,
        "role": "phase_draft",
        "provider": provider.strip(),
        "prompt_id": PHASE_PROMPT_ID,
        "prompt_sha256": sha256(prompt_text.encode("utf-8")).hexdigest(),
        "model_config": config,
        "model_config_hash": canonical_hash(config),
        "upstream_lineage_hashes": dict(sorted(upstream_lineage_hashes.items())),
        "payload": model_input,
        "payload_hash": canonical_hash(model_input),
        "rendered_messages": messages,
        "response_format": {"type": "json_object"},
    }
    encoded = canonical_json(body)
    return PhaseDraftRequest(encoded, canonical_hash(body))


def plan_phase_draft_calls(
    pair_batches: Mapping[str, Any],
    *,
    prompt_text: str,
    provider: str,
    model_config: Mapping[str, Any],
    upstream_lineage_hashes: Mapping[str, str],
    prompt_token_cap: int,
    estimate_tokens: Callable[[Mapping[str, Any]], int] | None = None,
) -> list[dict[str, Any]]:
    estimator = estimate_tokens or (lambda value: _json_token_floor(value))
    rows: list[dict[str, Any]] = []
    for index, batch in enumerate(pair_batches.get("pair_batches") or [], start=1):
        floor = {
            "pair": _clone(batch["pair"]),
            "considered_event_ids": list(batch["event_ids"]),
            "event_dispositions": [
                {"event_id": event_id, "outcomes": ["no_change"]}
                for event_id in batch["event_ids"]
            ],
            "relation_phases": [],
            "relation_facts": [],
        }
        cap, floor_tokens = select_output_cap(floor)
        capped_config = _clone(dict(model_config))
        capped_config["max_output_tokens"] = cap
        request = render_phase_draft_request(
            batch,
            prompt_text=prompt_text,
            provider=provider,
            model_config=capped_config,
            upstream_lineage_hashes=upstream_lineage_hashes,
        )
        prompt_tokens = int(estimator(request.body()))
        if prompt_tokens > prompt_token_cap:
            raise DraftBibleError(f"single phase pair exceeds prompt cap: {_phase_pair_key(batch)}")
        rows.append(
            {
                "call_id": f"phase_draft_{index:03d}",
                "pair": _clone(batch["pair"]),
                "event_ids": list(batch["event_ids"]),
                "prompt_tokens": prompt_tokens,
                "response_floor_tokens": floor_tokens,
                "max_output_tokens": cap,
                "request_fingerprint": request.request_fingerprint,
                "request_body": request.body(),
            }
        )
    return rows


def _validated_usage(value: Any) -> dict[str, int | float]:
    usage = dict(_require_mapping(value, "phase usage"))
    required = {"prompt_tokens", "completion_tokens", "cached_tokens", "reasoning_tokens", "cost_usd"}
    if set(usage) != required:
        raise DraftBibleError("phase usage fields mismatch")
    for key, raw in usage.items():
        if not isinstance(raw, (int, float)) or raw < 0:
            raise DraftBibleError(f"phase usage is invalid: {key}")
    return usage


def execute_phase_draft_request(
    request: PhaseDraftRequest,
    *,
    pair_batch: Mapping[str, Any],
    bundle: Mapping[str, Any],
    request_llm: Callable[[list[dict[str, Any]], Mapping[str, Any], bool], Mapping[str, Any]],
    out_dir: Path,
    reports_root: Path,
) -> dict[str, Any]:
    """Persist-call-persist-validate with one transport retry and no semantic retry."""

    body = request.body()
    if canonical_hash(body) != request.request_fingerprint:
        raise DraftBibleError("phase request fingerprint mismatch")
    resolved = assert_draft_output_root(out_dir, reports_root=reports_root)
    call_dir = resolved / "phase_calls" / request.request_fingerprint
    call_dir.mkdir(parents=True, exist_ok=True)
    request_path = call_dir / "request.json"
    write_json_exclusive(request_path, body)
    raw: Mapping[str, Any] | None = None
    attempts: list[str] = []
    last_error: Exception | None = None
    for attempt in range(1, 3):
        try:
            raw = request_llm(
                _clone(body["rendered_messages"]),
                {
                    "provider": body["provider"],
                    "model_config": _clone(body["model_config"]),
                    "request_fingerprint": request.request_fingerprint,
                    "response_format": _clone(body["response_format"]),
                },
                attempt == 2,
            )
            if not isinstance(raw, Mapping):
                raise DraftBibleError("phase callback returned a non-object")
            path = call_dir / f"attempt_{attempt:02d}_raw.json"
            write_json_exclusive(path, raw)
            attempts.append(path.relative_to(resolved).as_posix())
            break
        except DraftBibleError:
            raise
        except Exception as exc:
            last_error = exc
            path = call_dir / f"attempt_{attempt:02d}_transport_error.json"
            write_json_exclusive(path, {"error_type": type(exc).__name__, "message": str(exc)})
            attempts.append(path.relative_to(resolved).as_posix())
    if raw is None:
        raise DraftBibleError(f"phase transport failed after one retry: {last_error}")
    _exact_keys(
        raw,
        {"response", "usage", "provider", "model", "cache_key"},
        set(),
        "phase raw result",
    )
    if str(raw.get("provider") or "") != str(body["provider"]):
        raise DraftBibleError("phase provider metadata mismatch")
    if str(raw.get("model") or "") != str(body["model_config"].get("model") or ""):
        raise DraftBibleError("phase model metadata mismatch")
    if not str(raw.get("cache_key") or ""):
        raise DraftBibleError("phase callback omitted cache key")
    usage = _validated_usage(raw.get("usage"))
    validation_path = call_dir / "validation.json"
    try:
        normalized = validate_phase_draft_response(
            _require_mapping(raw.get("response"), "phase response"),
            pair_batch=pair_batch,
            bundle=bundle,
        )
    except DraftBibleError as exc:
        write_json_exclusive(
            validation_path,
            {"ok": False, "error_type": type(exc).__name__, "message": str(exc)},
        )
        raise
    validation = {
        "ok": True,
        "normalized_response_hash": str(normalized["validated_response_hash"]),
        "proposal_only": True,
    }
    write_json_exclusive(validation_path, validation)
    return {
        "request_fingerprint": request.request_fingerprint,
        "normalized_response": normalized,
        "usage": usage,
        "request_path": request_path.relative_to(resolved).as_posix(),
        "attempt_paths": attempts,
        "validation_path": validation_path.relative_to(resolved).as_posix(),
    }


def validate_phase_draft_response(
    response: Mapping[str, Any], *, pair_batch: Mapping[str, Any], bundle: Mapping[str, Any]
) -> dict[str, Any]:
    value = dict(_require_mapping(response, "phase response"))
    _exact_keys(
        value,
        {"pair", "considered_event_ids", "event_dispositions", "relation_phases", "relation_facts"},
        set(),
        "phase response",
    )
    pair = dict(_require_mapping(value["pair"], "phase pair"))
    expected_pair = dict(_require_mapping(pair_batch.get("pair"), "pair batch pair"))
    if pair != expected_pair:
        raise DraftBibleError("phase response pair mismatch")
    events = {
        str(row.get("event_id") or ""): dict(_require_mapping(row, "pair event"))
        for row in _require_list(pair_batch.get("events"), "pair events")
    }
    expected_event_ids = list(pair_batch.get("event_ids") or [])
    considered = [str(value) for value in _require_list(value["considered_event_ids"], "considered_event_ids")]
    if len(considered) != len(set(considered)) or set(considered) != set(expected_event_ids):
        raise DraftBibleError("considered_event_ids must set-equal pair events")
    dispositions: dict[str, dict[str, Any]] = {}
    for raw in _require_list(value["event_dispositions"], "event_dispositions"):
        row = dict(_require_mapping(raw, "event disposition"))
        _exact_keys(row, {"event_id", "outcomes"}, {"reason"}, "event disposition")
        event_id = str(row.get("event_id") or "")
        outcomes = [str(item) for item in _require_list(row.get("outcomes"), "event outcomes")]
        if event_id not in events or event_id in dispositions:
            raise DraftBibleError("event dispositions are duplicate/foreign")
        if not outcomes or len(outcomes) != len(set(outcomes)) or not set(outcomes) <= _EVENT_OUTCOMES:
            raise DraftBibleError("event outcomes are empty/duplicate/foreign")
        if ("blocked" in outcomes or "no_change" in outcomes) and len(outcomes) != 1:
            raise DraftBibleError("blocked/no_change event outcome must be exclusive")
        if outcomes == ["blocked"] and not str(row.get("reason") or "").strip():
            raise DraftBibleError("blocked event disposition requires reason")
        dispositions[event_id] = row
    if set(dispositions) != set(events):
        raise DraftBibleError("event dispositions do not exact-cover pair events")

    block_order = {
        str(row.get("block_id") or ""): int(row.get("order_index") or 0)
        for row in bundle.get("source_block_catalog") or []
    }
    phases: list[dict[str, Any]] = []
    used_phase_events: set[str] = set()
    raw_phases = _require_list(value["relation_phases"], "relation_phases")
    for index, raw in enumerate(raw_phases):
        row = dict(_require_mapping(raw, "relation phase"))
        _exact_keys(
            row,
            {"phase_label", "valid_from_block", "valid_until_block", "status", "trigger_event_id", "trigger_evidence_quote"},
            set(),
            "relation phase",
        )
        start = str(row.get("valid_from_block") or "")
        end = row.get("valid_until_block")
        trigger_id = str(row.get("trigger_event_id") or "")
        if row.get("phase_label") not in _PHASE_LABELS or start not in block_order or trigger_id not in events:
            raise DraftBibleError("phase label/start/trigger is invalid")
        if str(events[trigger_id].get("block_id") or "") != start:
            raise DraftBibleError("phase trigger event must occur at valid_from_block")
        if str(row.get("trigger_evidence_quote") or "") != str(events[trigger_id].get("evidence_quote") or ""):
            raise DraftBibleError("phase trigger quote does not copy event evidence")
        is_last = index == len(raw_phases) - 1
        if is_last:
            if end is not None or row.get("status") != "open":
                raise DraftBibleError("last phase must be open with null end")
        else:
            next_row = _require_mapping(raw_phases[index + 1], "next relation phase")
            if end != next_row.get("valid_from_block") or row.get("status") != "closed":
                raise DraftBibleError("closed phase must end at next phase start")
        if end is not None and (str(end) not in block_order or block_order[str(end)] <= block_order[start]):
            raise DraftBibleError("phase half-open interval is invalid")
        if phases and block_order[start] < block_order[str(phases[-1]["valid_until_block"])]:
            raise DraftBibleError("phase intervals overlap")
        used_phase_events.add(trigger_id)
        phases.append(_clone(row))

    entity_ids = {str(expected_pair["a_entity_id"]), str(expected_pair["b_entity_id"])}
    facts: list[dict[str, Any]] = []
    used_fact_events: set[str] = set()
    for raw in _require_list(value["relation_facts"], "relation_facts"):
        row = dict(_require_mapping(raw, "relation fact"))
        _exact_keys(
            row,
            {"subject_ref", "predicate_code", "object_ref", "source_event_id", "evidence_quote", "inference_basis"},
            {"predicate_note"},
            "relation fact",
        )
        subject, obj = str(row.get("subject_ref") or ""), str(row.get("object_ref") or "")
        event_id = str(row.get("source_event_id") or "")
        if {subject, obj} != entity_ids or subject == obj:
            raise DraftBibleError("relation fact subject/object must be the supplied directed pair")
        if row.get("predicate_code") not in _PREDICATES or row.get("inference_basis") not in {"stated", "derived"}:
            raise DraftBibleError("relation fact predicate/inference_basis is invalid")
        if event_id not in events or str(row.get("evidence_quote") or "") != str(events[event_id].get("evidence_quote") or ""):
            raise DraftBibleError("relation fact evidence does not copy its source event")
        if row.get("predicate_code") == "other" and not str(row.get("predicate_note") or "").strip():
            raise DraftBibleError("other relation fact requires predicate_note")
        used_fact_events.add(event_id)
        facts.append(_clone(row))
    for event_id, row in dispositions.items():
        outcomes = set(row["outcomes"])
        if (event_id in used_phase_events) != ("phase_support" in outcomes):
            raise DraftBibleError("phase_support disposition does not match phase triggers")
        if (event_id in used_fact_events) != ("fact_support" in outcomes):
            raise DraftBibleError("fact_support disposition does not match fact evidence")
    normalized = {
        "pair": expected_pair,
        "considered_event_ids": expected_event_ids,
        "event_dispositions": [dispositions[event_id] for event_id in expected_event_ids],
        "relation_phases": phases,
        "relation_facts": facts,
    }
    return {**normalized, "validated_response_hash": canonical_hash(normalized)}


def _claim_id(kind: str, payload: Mapping[str, Any]) -> str:
    return f"draft_{kind}_{canonical_hash({'kind': kind, 'payload': payload})[:20]}"


def _append_claim(rows: list[dict[str, Any]], kind: str, payload: Mapping[str, Any]) -> None:
    body = _clone(payload)
    supplied = body.pop("item_id", None)
    expected = _claim_id(kind, body)
    if supplied is not None and supplied != expected:
        raise DraftBibleError(f"content-addressed {kind} item id mismatch")
    row = {"item_id": expected, **body}
    for existing in rows:
        if existing.get("item_id") == expected:
            if canonical_json(existing) != canonical_json(row):
                raise DraftBibleError(f"equal {kind} item id has unequal payload")
            return
    rows.append(row)


def _normalize_claim_rows(values: Sequence[Mapping[str, Any]], kind: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for value in values:
        _append_claim(rows, kind, value)
    return rows


def _phase_pair_key(row: Mapping[str, Any]) -> tuple[str, str]:
    pair = row.get("pair") or {}
    return tuple(sorted((str(pair.get("a_entity_id") or ""), str(pair.get("b_entity_id") or ""))))


def _phase_overlap(left: Mapping[str, Any], right: Mapping[str, Any], order: Mapping[str, int]) -> bool:
    if _phase_pair_key(left) != _phase_pair_key(right):
        return False
    left_start = order.get(str(left.get("valid_from_block") or ""), 10**9)
    right_start = order.get(str(right.get("valid_from_block") or ""), 10**9)
    left_end_raw, right_end_raw = left.get("valid_until_block"), right.get("valid_until_block")
    left_end = order.get(str(left_end_raw), 10**9) if left_end_raw is not None else 10**12
    right_end = order.get(str(right_end_raw), 10**9) if right_end_raw is not None else 10**12
    return left_start < right_end and right_start < left_end


def consolidate_diagnostic_draft(
    bundle: Mapping[str, Any],
    *,
    identity: Mapping[str, Any],
    endpoint_bindings: Mapping[str, Any],
    pair_batches: Mapping[str, Any],
    phase_responses: Sequence[Mapping[str, Any]],
    untrusted_frame_proposal: Mapping[str, Any] | Sequence[Mapping[str, Any]] | None = None,
    prior_draft: Mapping[str, Any] | None = None,
    request_lineage: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Append validated claims without selecting semantic winners."""

    _verify_bundle_identity(bundle)
    if prior_draft:
        _artifact_without_hash(prior_draft)
    prior = _clone(prior_draft or {})
    phase_rows = _normalize_claim_rows(prior.get("relation_phases") or [], "relation_phase")
    fact_rows = _normalize_claim_rows(prior.get("relation_facts") or [], "relation_fact")
    pair_dispositions = _normalize_claim_rows(prior.get("pair_dispositions") or [], "pair_disposition")
    relation_conflicts = [dict(row) for row in prior.get("relation_conflicts") or []]
    batches = {
        _phase_pair_key(batch): batch for batch in pair_batches.get("pair_batches") or []
    }
    responses: dict[tuple[str, str], dict[str, Any]] = {}
    for response in phase_responses:
        key = _phase_pair_key(response)
        if key not in batches or key in responses:
            raise DraftBibleError(f"phase response is duplicate or has no input pair: {key}")
        responses[key] = dict(response)
    for key, batch in batches.items():
        if key not in responses:
            _append_claim(
                pair_dispositions,
                "pair_disposition",
                {
                    "pair": _clone(batch["pair"]),
                    "disposition": "model_omitted_pair",
                    "event_ids": list(batch["event_ids"]),
                },
            )
            continue
        response = responses[key]
        _append_claim(
            pair_dispositions,
            "pair_disposition",
            {
                "pair": _clone(response["pair"]),
                "disposition": "addressed",
                "event_ids": list(response["considered_event_ids"]),
                "event_dispositions": _clone(response["event_dispositions"]),
            },
        )
        for phase in response["relation_phases"]:
            payload = {
                **_clone(phase),
                "pair": _clone(response["pair"]),
                "source_event_ids": [str(phase["trigger_event_id"])],
                "claim_status": "single_model_proposal",
            }
            _append_claim(phase_rows, "relation_phase", payload)
        event_by_id = {row["event_id"]: row for row in batch["events"]}
        for fact in response["relation_facts"]:
            event = event_by_id[str(fact["source_event_id"])]
            payload = {
                **_clone(fact),
                "pair": _clone(response["pair"]),
                "valid_from_block": str(event["block_id"]),
                "claim_status": "single_model_proposal",
                "runtime_eligible": False,
            }
            _append_claim(fact_rows, "relation_fact", payload)

    block_order = {
        str(row.get("block_id") or ""): int(row.get("order_index") or 0)
        for row in bundle.get("source_block_catalog") or []
    }
    for index, left in enumerate(phase_rows):
        for right in phase_rows[index + 1 :]:
            if left.get("item_id") != right.get("item_id") and _phase_overlap(left, right, block_order):
                body = {
                    "kind": "overlapping_phase_proposals",
                    "pair": _clone(left.get("pair") or {}),
                    "claim_item_ids": sorted([str(left.get("item_id")), str(right.get("item_id"))]),
                }
                conflict = {"conflict_id": f"relconf_{canonical_hash(body)[:20]}", **body}
                if conflict not in relation_conflicts:
                    relation_conflicts.append(conflict)

    occurrence_to_entity = identity.get("occurrence_to_entity") or {}
    mapped_states: list[dict[str, Any]] = []
    blocked_states: list[dict[str, Any]] = []
    for row in _ground_channel(bundle, "state_change_inputs"):
        payload = _require_mapping(row.get("payload"), "state change payload")
        subject_ref = str(payload.get("subject_ref") or "")
        entity_id = occurrence_to_entity.get(subject_ref)
        target = mapped_states if entity_id else blocked_states
        target.append(
            {
                **_clone(row),
                "entity_id": entity_id,
                "mapping_status": "mapped" if entity_id else "unresolved_subject",
            }
        )

    if untrusted_frame_proposal is None:
        frames: list[dict[str, Any]] = []
    elif isinstance(untrusted_frame_proposal, Mapping):
        frames = [_clone(untrusted_frame_proposal)]
    elif isinstance(untrusted_frame_proposal, Sequence) and not isinstance(untrusted_frame_proposal, (str, bytes)):
        frames = [_clone(_require_mapping(row, "untrusted frame proposal")) for row in untrusted_frame_proposal]
    else:
        raise DraftBibleError("untrusted frame proposal must be an object or list")

    def reject_authority(value: Any) -> None:
        if isinstance(value, Mapping):
            if set(value) & {"active", "verified", "corroborated"}:
                raise DraftBibleError("single-model frame proposal attempted to claim authority")
            for nested in value.values():
                reject_authority(nested)
        elif isinstance(value, list):
            for nested in value:
                reject_authority(nested)

    for frame in frames:
        if frame.get("trust") not in {"unknown", "untrusted"}:
            raise DraftBibleError("single-model frame proposal has foreign trust")
        reject_authority(frame)
    target_manifest = build_identity_target_manifest(bundle)
    artifact = {
        "schema_version": DRAFT_ARTIFACT_SCHEMA_VERSION,
        "contract_version": DRAFT_CONTRACT_VERSION,
        "artifact_status": "diagnostic_draft",
        "runtime_consumable": False,
        "knowledge_mode": "whole_book_frozen",
        "omitted_layers": ["authority_checker", "active_overlay", "disclosure_filtered_view", "address_policy"],
        "state_lineage_id": str(bundle.get("state_lineage_id") or ""),
        "bundle_manifest_hash": str(bundle.get("bundle_manifest_hash") or ""),
        "selected_chapters": list(bundle.get("selected_chapters") or []),
        "source_block_catalog": _clone(bundle.get("source_block_catalog") or []),
        "occurrences": _clone(bundle.get("occurrence_cards") or []),
        "identity_target_manifest": target_manifest,
        "non_entity_referents": _clone(target_manifest["excluded_occurrences"]),
        "entities": _clone(identity.get("entities") or []),
        "occurrence_to_entity": _clone(occurrence_to_entity),
        "unresolved_occurrence_ids": list(identity.get("unresolved_occurrence_ids") or []),
        "identity_conflicts": _clone(identity.get("identity_conflicts") or []),
        "endpoint_dispositions": _clone(endpoint_bindings.get("endpoint_dispositions") or []),
        "relation_events": _ground_channel(bundle, "relation_event_inputs"),
        "phase_observations": _ground_channel(bundle, "phase_observation_inputs"),
        "blocked_events": _clone(pair_batches.get("blocked_events") or []),
        "relation_phases": phase_rows,
        "relation_facts": fact_rows,
        "pair_dispositions": pair_dispositions,
        "relation_conflicts": relation_conflicts,
        "entity_state_change_claims": mapped_states,
        "blocked_state_change_claims": blocked_states,
        "observed_address_evidence": _ground_channel(bundle, "dialogue_turn_inputs"),
        "glossary": _ground_channel(bundle, "glossary_inputs"),
        "translator_facts": _ground_channel(bundle, "translator_fact_inputs"),
        "motifs": _ground_channel(bundle, "motif_inputs"),
        "unresolved_threads": _ground_channel(bundle, "unresolved_thread_inputs"),
        "rolling_summaries": _ground_channel(bundle, "rolling_summary_inputs"),
        "frame_claim_inputs": _ground_channel(bundle, "frame_claim_inputs"),
        "untrusted_frame_proposals": frames,
        "runtime_frame_view": {"status": "unknown", "segments": []},
        "request_lineage": _clone(request_lineage or {}),
        "coverage": {
            "identity": _clone(identity.get("coverage") or {}),
            "endpoints": _clone(endpoint_bindings.get("coverage") or {}),
            "events": _clone(pair_batches.get("coverage") or {}),
        },
        "validator_contract_hashes": {
            "draft_reconciler": canonical_hash({"contract_version": DRAFT_CONTRACT_VERSION}),
            "phase_draft": canonical_hash(
                {
                    "prompt_id": PHASE_PROMPT_ID,
                    "phase_labels": sorted(_PHASE_LABELS),
                    "event_outcomes": sorted(_EVENT_OUTCOMES),
                    "predicates": sorted(_PREDICATES),
                }
            ),
        },
        "validation_artifact_hashes": {
            "identity": str(identity.get("identity_result_hash") or ""),
            "endpoints": str(endpoint_bindings.get("endpoint_binding_hash") or ""),
            "pair_batches": str(pair_batches.get("pair_batch_hash") or ""),
        },
    }
    artifact["artifact_hash"] = canonical_hash(artifact)
    return _clone(artifact)


def _artifact_without_hash(artifact: Mapping[str, Any]) -> dict[str, Any]:
    value = _clone(artifact)
    expected = str(value.pop("artifact_hash", ""))
    if not expected or canonical_hash(value) != expected:
        raise DraftBibleError("diagnostic draft artifact hash mismatch")
    return value


def build_chapter_prefix_snapshots(
    artifact: Mapping[str, Any], bundle: Mapping[str, Any]
) -> list[dict[str, Any]]:
    _artifact_without_hash(artifact)
    chapters = list(artifact.get("selected_chapters") or [])
    cards = _card_index(bundle)
    source_catalog = list(bundle.get("source_block_catalog") or [])
    block_chapter = {
        str(row.get("block_id") or ""): str(row.get("chapter_id") or "")
        for row in source_catalog
    }
    event_chapter = {
        str((row.get("payload") or {}).get("event_id") or ""): str(row.get("chapter_id") or "")
        for row in artifact.get("relation_events") or []
    }
    snapshots: list[dict[str, Any]] = []
    for cutoff_index, chapter_id in enumerate(chapters):
        allowed = set(chapters[: cutoff_index + 1])
        allowed_occurrences = {
            occurrence_id for occurrence_id, card in cards.items() if str(card.get("chapter_id") or "") in allowed
        }
        entities: list[dict[str, Any]] = []
        for entity in artifact.get("entities") or []:
            members = [value for value in entity.get("member_occurrence_ids") or [] if value in allowed_occurrences]
            if not members:
                continue
            row = _clone(entity)
            row["member_occurrence_ids"] = members
            row["aliases"] = [
                {
                    **_clone(alias),
                    "covered_occurrence_ids": [
                        value for value in alias.get("covered_occurrence_ids") or [] if value in allowed_occurrences
                    ],
                }
                for alias in row.get("aliases") or []
                if any(value in allowed_occurrences for value in alias.get("covered_occurrence_ids") or [])
            ]
            row["canonical_surface_guesses_advisory"] = [
                value
                for value in row.get("canonical_surface_guesses_advisory") or []
                if str(value.get("source_occurrence_id") or "") in allowed_occurrences
            ]
            row["referent_kind_claims"] = [
                value
                for value in row.get("referent_kind_claims") or []
                if str(value.get("source_occurrence_id") or "") in allowed_occurrences
            ]
            prefix_kinds = {
                str(value.get("referent_kind") or "unknown")
                for value in row["referent_kind_claims"]
                if str(value.get("referent_kind") or "unknown") != "unknown"
            }
            row["referent_kind"] = next(iter(prefix_kinds), "unknown") if len(prefix_kinds) <= 1 else "unknown"
            entities.append(row)
        snapshot = _clone(artifact)
        snapshot["scope_chapter_id"] = chapter_id
        snapshot["selected_chapters"] = chapters[: cutoff_index + 1]
        snapshot["entities"] = entities
        snapshot["occurrence_to_entity"] = {
            key: value for key, value in artifact.get("occurrence_to_entity", {}).items() if key in allowed_occurrences
        }
        snapshot["unresolved_occurrence_ids"] = [
            value for value in artifact.get("unresolved_occurrence_ids") or [] if value in allowed_occurrences
        ]
        snapshot["source_block_catalog"] = [
            row for row in source_catalog if str(row.get("chapter_id") or "") in allowed
        ]
        snapshot["occurrences"] = [
            row for row in artifact.get("occurrences") or [] if str(row.get("chapter_id") or "") in allowed
        ]
        snapshot["endpoint_dispositions"] = [
            row for row in artifact.get("endpoint_dispositions") or [] if str(row.get("endpoint_id") or "") in allowed_occurrences
        ]
        snapshot["identity_conflicts"] = []
        for conflict in artifact.get("identity_conflicts") or []:
            members = [value for value in conflict.get("member_occurrence_ids") or [] if value in allowed_occurrences]
            if members:
                row = _clone(conflict)
                row["member_occurrence_ids"] = members
                snapshot["identity_conflicts"].append(row)
        snapshot["relation_phases"] = []
        for phase in artifact.get("relation_phases") or []:
            start = str(phase.get("valid_from_block") or "")
            if block_chapter.get(start) not in allowed:
                continue
            row = _clone(phase)
            end = row.get("valid_until_block")
            if end is not None and block_chapter.get(str(end)) not in allowed:
                row["valid_until_block"] = None
                row["status"] = "open_within_scope"
            snapshot["relation_phases"].append(row)
        snapshot["relation_facts"] = [
            row
            for row in artifact.get("relation_facts") or []
            if event_chapter.get(str(row.get("source_event_id") or "")) in allowed
        ]
        snapshot["blocked_events"] = [
            row
            for row in artifact.get("blocked_events") or []
            if event_chapter.get(str(row.get("event_id") or "")) in allowed
        ]
        snapshot["pair_dispositions"] = []
        for disposition in artifact.get("pair_dispositions") or []:
            kept_ids = [
                event_id
                for event_id in disposition.get("event_ids") or []
                if event_chapter.get(str(event_id)) in allowed
            ]
            if not kept_ids:
                continue
            row = _clone(disposition)
            row["event_ids"] = kept_ids
            if "event_dispositions" in row:
                row["event_dispositions"] = [
                    value
                    for value in row["event_dispositions"]
                    if str(value.get("event_id") or "") in set(kept_ids)
                ]
            snapshot["pair_dispositions"].append(row)
        retained_phase_ids = {str(row.get("item_id") or "") for row in snapshot["relation_phases"]}
        snapshot["relation_conflicts"] = [
            row
            for row in artifact.get("relation_conflicts") or []
            if set(str(value) for value in row.get("claim_item_ids") or []) & retained_phase_ids
        ]
        for key in (
            "relation_events",
            "phase_observations",
            "observed_address_evidence",
            "glossary",
            "translator_facts",
            "motifs",
            "unresolved_threads",
            "rolling_summaries",
            "frame_claim_inputs",
            "entity_state_change_claims",
            "blocked_state_change_claims",
        ):
            snapshot[key] = [row for row in artifact.get(key) or [] if str(row.get("chapter_id") or "") in allowed]
        snapshot["coverage"] = {
            "identity": {
                "owned_total": len(
                    [row for row in artifact["identity_target_manifest"]["owned_targets"] if row["occurrence_id"] in allowed_occurrences]
                ),
                "resolved": len(snapshot["occurrence_to_entity"]),
                "unresolved": len(snapshot["unresolved_occurrence_ids"]),
            },
            "endpoints": {"total": len(snapshot["endpoint_dispositions"])},
            "events": {
                "input_events": len(snapshot["relation_events"]),
                "blocked_events": len(snapshot["blocked_events"]),
            },
        }
        snapshot["identity_target_manifest"] = {
            **_clone(artifact["identity_target_manifest"]),
            "owned_targets": [
                row for row in artifact["identity_target_manifest"]["owned_targets"] if row["occurrence_id"] in allowed_occurrences
            ],
            "excluded_occurrences": [
                row for row in artifact["identity_target_manifest"]["excluded_occurrences"] if row["occurrence_id"] in allowed_occurrences
            ],
        }
        snapshot["non_entity_referents"] = [
            row for row in artifact.get("non_entity_referents") or [] if row.get("occurrence_id") in allowed_occurrences
        ]
        snapshot["untrusted_frame_proposals"] = [
            row
            for row in artifact.get("untrusted_frame_proposals") or []
            if not row.get("unit_id") or str(row.get("unit_id")) in allowed
        ]
        snapshot.pop("artifact_hash", None)
        snapshot["artifact_hash"] = canonical_hash(snapshot)
        snapshots.append(snapshot)
    return snapshots


def assert_draft_output_root(out_dir: Path, *, reports_root: Path) -> Path:
    root = Path(reports_root).resolve()
    resolved = Path(out_dir).resolve()
    expected_parent = (root / DRAFT_REPORT_PREFIX).resolve()
    try:
        resolved.relative_to(expected_parent)
    except ValueError as exc:
        raise DraftBibleError("draft output must remain under the dedicated report root") from exc
    if resolved == expected_parent:
        raise DraftBibleError("draft output requires an explicit run_id directory")
    return resolved


def write_draft_artifacts(
    artifact: Mapping[str, Any],
    *,
    bundle: Mapping[str, Any],
    out_dir: Path,
    reports_root: Path,
    eyeball_manifest: Mapping[str, Any] | None = None,
    preflight_report: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    resolved = assert_draft_output_root(out_dir, reports_root=reports_root)
    resolved.mkdir(parents=True, exist_ok=True)
    write_json_exclusive(resolved / "book_diagnostic_draft.json", artifact)
    snapshots = build_chapter_prefix_snapshots(artifact, bundle)
    chapter_paths: list[str] = []
    for snapshot in snapshots:
        filename = f"{snapshot['scope_chapter_id']}_diagnostic_draft.json"
        write_json_exclusive(resolved / filename, snapshot)
        chapter_paths.append(filename)
    if eyeball_manifest is not None:
        write_json_exclusive(resolved / "eyeball_manifest.json", eyeball_manifest)
    if preflight_report is not None:
        write_json_exclusive(resolved / "preflight_report.json", preflight_report)
    manifest = {
        "schema_version": "literary_s5c_draft_run_manifest_v1",
        "artifact_status": "diagnostic_draft",
        "runtime_consumable": False,
        "book_artifact": "book_diagnostic_draft.json",
        "chapter_artifacts": chapter_paths,
        "artifact_hash": str(artifact.get("artifact_hash") or ""),
        "eyeball_manifest_hash": str((eyeball_manifest or {}).get("manifest_hash") or ""),
        "preflight_report_hash": str((preflight_report or {}).get("report_hash") or ""),
    }
    manifest["manifest_hash"] = canonical_hash(manifest)
    write_json_exclusive(resolved / "run_manifest.json", manifest)
    return _clone(manifest)


def build_preflight_report(call_rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    bucket_totals: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for raw in call_rows:
        row = dict(_require_mapping(raw, "preflight call row"))
        _exact_keys(
            row,
            {"call_id", "stage", "quota_bucket_id", "completion_reserve"},
            {"prompt_tokens_exact_now", "deterministic_prompt_upper"},
            "preflight call row",
        )
        exact = row.get("prompt_tokens_exact_now")
        upper = row.get("deterministic_prompt_upper")
        reserve = row.get("completion_reserve")
        if exact is None and upper is None:
            raise DraftBibleError("preflight row needs exact-now or deterministic upper prompt tokens")
        if any(value is not None and (not isinstance(value, int) or value < 0) for value in (exact, upper, reserve)):
            raise DraftBibleError("preflight token fields must be non-negative integers")
        if exact is not None and upper is not None and exact > upper:
            raise DraftBibleError("exact prompt tokens exceed deterministic upper bound")
        prompt_debit = int(exact if exact is not None else upper)
        normalized = {
            **row,
            "estimate_class": "exact_now" if exact is not None else "deterministic_upper",
            "prompt_debit": prompt_debit,
            "reserved_total": prompt_debit + int(reserve),
        }
        rows.append(normalized)
        totals = bucket_totals[str(row["quota_bucket_id"])]
        totals["prompt_debit"] += prompt_debit
        totals["completion_reserve"] += int(reserve)
        totals["reserved_total"] += prompt_debit + int(reserve)
        totals["calls"] += 1
    body = {
        "schema_version": "literary_s5c_draft_preflight_v1",
        "zero_api": True,
        "calls": rows,
        "bucket_totals": {key: dict(sorted(value.items())) for key, value in sorted(bucket_totals.items())},
    }
    return {**body, "report_hash": canonical_hash(body)}


def gate_preflight_budget(
    report: Mapping[str, Any],
    *,
    used_today_by_bucket: Mapping[str, int],
    daily_cap_by_bucket: Mapping[str, int],
) -> dict[str, Any]:
    body = _clone(dict(report))
    expected = str(body.pop("report_hash", ""))
    if not expected or canonical_hash(body) != expected:
        raise DraftBibleError("preflight report hash mismatch")
    decisions: list[dict[str, Any]] = []
    for bucket, totals in sorted(_require_mapping(report.get("bucket_totals"), "bucket_totals").items()):
        used = int(used_today_by_bucket.get(bucket, 0))
        cap = daily_cap_by_bucket.get(bucket)
        if cap is None or not isinstance(cap, int) or cap <= 0 or used < 0:
            raise DraftBibleError(f"quota bucket lacks a valid UTC-day cap: {bucket}")
        reserved = int(_require_mapping(totals, "bucket total").get("reserved_total") or 0)
        allowed = used + reserved <= cap
        decisions.append(
            {
                "quota_bucket_id": bucket,
                "used_today": used,
                "reserved_total": reserved,
                "daily_cap": cap,
                "allowed": allowed,
            }
        )
        if not allowed:
            raise DraftBibleError(
                f"quota gate rejected bucket {bucket}: used={used}, reserve={reserved}, cap={cap}"
            )
    result = {
        "schema_version": "literary_s5c_draft_budget_gate_v1",
        "report_hash": expected,
        "utc_day_accounting": "prompt_plus_completion",
        "decisions": decisions,
        "allowed": True,
    }
    return {**result, "gate_hash": canonical_hash(result)}


def build_eyeball_manifest(defect_reconciliation_path: Path) -> dict[str, Any]:
    path = Path(defect_reconciliation_path)
    text = path.read_text(encoding="utf-8")
    rows: list[dict[str, Any]] = []
    active_section: str | None = None
    for line_number, line in enumerate(text.splitlines(), start=1):
        stripped = line.strip()
        if stripped.startswith("## "):
            active_section = stripped[3:4] if stripped[3:4] in {"A", "B", "C"} else None
            continue
        if active_section is None or not stripped.startswith("|"):
            continue
        cells = [cell.strip() for cell in stripped.strip("|").split("|")]
        if not cells or all(set(cell) <= {"-", ":", " "} for cell in cells):
            continue
        if cells[0].casefold() in {"theme", "claude#", "sol#"}:
            continue
        folded = stripped.casefold()
        rows.append(
            {
                "row_id": f"e9row_{sha256(stripped.encode('utf-8')).hexdigest()[:20]}",
                "source_section": active_section,
                "source_line": line_number,
                "source_row": stripped,
                "expected_class": "uncertain" if any(token in folded for token in ("uncertain", "ambiguous")) else "from_committed_reconciliation",
            }
        )
    if not rows:
        raise DraftBibleError("E9 defect reconciliation produced an empty eyeball manifest")
    body = {
        "schema_version": "literary_s5c_eyeball_manifest_v1",
        "source_path": path.as_posix(),
        "source_sha256": sha256(text.encode("utf-8")).hexdigest(),
        "review_protocol": "independent_then_reconcile",
        "allowed_outcomes": ["fixed", "persists", "not_exercised", "not_auditable"],
        "rows": rows,
    }
    return {**body, "manifest_hash": canonical_hash(body)}


def run_recorded_draft_apply(
    bundle: Mapping[str, Any],
    *,
    proposal_rows: Sequence[Mapping[str, Any]],
    phase_response_rows: Sequence[Mapping[str, Any]],
    out_dir: Path,
    reports_root: Path,
    defect_reconciliation_path: Path,
    untrusted_frame_proposal: Mapping[str, Any] | Sequence[Mapping[str, Any]] | None = None,
    prior_draft: Mapping[str, Any] | None = None,
    preflight_report: Mapping[str, Any] | None = None,
    request_lineage: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Exercise the complete draft apply path without a key or network call."""

    identity = reconcile_identity_claims(bundle, proposal_rows=proposal_rows)
    endpoints = bind_endpoints(bundle, identity)
    pairs = build_final_pair_batches(bundle, endpoints)
    batch_by_key = {_phase_pair_key(row): row for row in pairs["pair_batches"]}
    validated_responses: list[dict[str, Any]] = []
    for raw in phase_response_rows:
        key = _phase_pair_key(raw)
        batch = batch_by_key.get(key)
        if batch is None:
            raise DraftBibleError(f"recorded phase response has no pair batch: {key}")
        validated_responses.append(validate_phase_draft_response(raw, pair_batch=batch, bundle=bundle))
    effective_lineage = _clone(
        request_lineage
        or {
            "execution_mode": "recorded_response",
            "identity_proposal_rows_hash": canonical_hash(list(proposal_rows)),
            "phase_response_rows_hash": canonical_hash(list(phase_response_rows)),
        }
    )
    artifact = consolidate_diagnostic_draft(
        bundle,
        identity=identity,
        endpoint_bindings=endpoints,
        pair_batches=pairs,
        phase_responses=validated_responses,
        untrusted_frame_proposal=untrusted_frame_proposal,
        prior_draft=prior_draft,
        request_lineage=effective_lineage,
    )
    eyeball = build_eyeball_manifest(defect_reconciliation_path)
    run_manifest = write_draft_artifacts(
        artifact,
        bundle=bundle,
        out_dir=out_dir,
        reports_root=reports_root,
        eyeball_manifest=eyeball,
        preflight_report=preflight_report,
    )
    return {
        "artifact": artifact,
        "identity": identity,
        "endpoint_bindings": endpoints,
        "pair_batches": pairs,
        "eyeball_manifest": eyeball,
        "run_manifest": run_manifest,
    }


__all__ = [
    "DRAFT_ARTIFACT_SCHEMA_VERSION",
    "DRAFT_CONTRACT_VERSION",
    "DRAFT_REPORT_PREFIX",
    "DraftBibleError",
    "IdentityShardPlan",
    "PhaseDraftRequest",
    "PHASE_PROMPT_ID",
    "assert_draft_output_root",
    "bind_endpoints",
    "build_bounded_retrieval_payload",
    "build_bounded_proposal_payload",
    "build_chapter_prefix_snapshots",
    "build_eyeball_manifest",
    "build_final_pair_batches",
    "build_identity_target_manifest",
    "build_preflight_report",
    "consolidate_diagnostic_draft",
    "execute_phase_draft_request",
    "gate_preflight_budget",
    "load_phase_draft_prompt",
    "plan_identity_retrieval_shards",
    "plan_identity_proposal_shards",
    "plan_phase_draft_calls",
    "reconcile_identity_claims",
    "run_recorded_draft_apply",
    "render_phase_draft_request",
    "select_output_cap",
    "validate_phase_draft_response",
    "write_draft_artifacts",
]
