"""Bounded cross-chapter review for stable prior-card claim conflicts."""

from __future__ import annotations

from collections import defaultdict
from copy import deepcopy
from dataclasses import dataclass
import json
import math
from pathlib import Path
import re
import unicodedata
from typing import Any, Iterable, Mapping, Sequence

from pipeline.literary.b0_entity_inventory_experiment import (
    REFERENTIAL_GENDERS,
    REFERENT_KINDS,
)
from pipeline.literary.b0_entity_prior_challenge_experiment import (
    EXPERIMENT_SCHEMA_VERSION,
    ISSUE_TO_FIELD,
    REFERENT_CONTINUITIES,
    build_prior_packets,
    validate_prior_cards,
)
from pipeline.literary.book_entity_claim_auditor_prompts_v1 import (
    PROMPT_ID,
    PROMPT_SHA256,
    load_book_entity_claim_prompt_v1,
)
from pipeline.literary.book_source_lineage import (
    build_book_source_manifest,
    chapter_source_hash,
    state_lineage_id_for_manifest,
    verify_book_source_manifest,
)
from pipeline.literary.checkpoint import canonical_hash, canonical_json


CLAIM_INDEX_SCHEMA_VERSION = "book_entity_claim_ticket_index_v2"
CLAIM_DECISION_SCHEMA_VERSION = "cross_chapter_prior_claim_decision_v2"
CLAIM_LEDGER_SCHEMA_VERSION = "prior_claim_revision_ledger_v3"
CLAIM_PROJECTION_SCHEMA_VERSION = "prior_claim_projection_v3"
CLAIM_VALIDATOR_VERSION = "book_entity_claim_auditor_validator_v3"
DEFAULT_MAX_TICKETS_PER_COMPONENT = 4
DEFAULT_MAX_INVOLVED_CHAPTERS = 3
DEFAULT_MAX_SOURCE_BLOCKS = 24
DEFAULT_MAX_BRIDGE_BLOCKS = 8
DEFAULT_NEIGHBOR_RADIUS = 1
DEFAULT_MAX_GIST_UTF8_BYTES = 6000
CLAIM_ACTIONS = frozenset(
    {"retain_prior", "revise_claim", "pending", "refer_identity_conflict"}
)
PENDING_REASON_CODES = frozenset(
    {
        "conflicting_evidence",
        "evidence_not_attributable",
        "insufficient_context",
        "model_uncertain",
    }
)
MAX_AUTOMATIC_HEARINGS = 2
CLAIM_ISSUES = frozenset(
    {"kind_conflict", "gender_conflict", "unsupported_stable_claim"}
)
IDENTITY_ISSUES = frozenset(
    {"identity_collision", "alias_target_conflict", "alias_scope_conflict"}
)


class BookEntityClaimAuditorError(RuntimeError):
    """Base error for the cross-chapter prior-claim boundary."""


class BookEntityClaimContractError(BookEntityClaimAuditorError):
    """Raised when source, ticket, response, or ledger violates the contract."""


@dataclass(frozen=True)
class RenderedPriorClaimRequestV1:
    component_id: str
    request_fingerprint: str
    messages: tuple[dict[str, str], ...]
    response_schema: dict[str, Any]
    semantic_payload: dict[str, Any]


def _clone(value: Any) -> Any:
    return deepcopy(value)


def _required_string(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise BookEntityClaimContractError(f"{label} must be a non-empty string")
    return value


def _exact_keys(value: Mapping[str, Any], expected: set[str], label: str) -> None:
    actual = set(value)
    if actual != expected:
        raise BookEntityClaimContractError(
            f"{label} field set differs; missing={sorted(expected-actual)}, "
            f"foreign={sorted(actual-expected)}"
        )


def _string_list(
    value: Any,
    label: str,
    *,
    allow_empty: bool = False,
) -> list[str]:
    if not isinstance(value, list) or (not value and not allow_empty):
        qualifier = "possibly empty" if allow_empty else "non-empty"
        raise BookEntityClaimContractError(f"{label} must be a {qualifier} list")
    rows = [_required_string(item, label) for item in value]
    if len(rows) != len(set(rows)):
        raise BookEntityClaimContractError(f"{label} contains duplicates")
    return rows


def _hash_string(value: Any, label: str) -> str:
    result = _required_string(value, label)
    if not re.fullmatch(r"[0-9a-f]{64}", result):
        raise BookEntityClaimContractError(f"{label} must be a lowercase SHA-256")
    return result


def _block_text(block: Mapping[str, Any]) -> str:
    return unicodedata.normalize(
        "NFC",
        str(
            block.get("clean_text")
            or block.get("source_text")
            or block.get("text")
            or ""
        ),
    )


def _contains_surface(text: str, surface: str) -> bool:
    normalized_text = unicodedata.normalize("NFKC", text).casefold()
    normalized_surface = unicodedata.normalize("NFKC", surface).casefold()
    if not normalized_surface:
        return False
    start_guard = r"(?<!\w)" if normalized_surface[0].isalnum() else ""
    end_guard = r"(?!\w)" if normalized_surface[-1].isalnum() else ""
    return (
        re.search(
            start_guard + re.escape(normalized_surface) + end_guard,
            normalized_text,
        )
        is not None
    )


def _document_catalog(document: Mapping[str, Any]) -> dict[str, Any]:
    chapters = document.get("chapters")
    if not isinstance(chapters, list) or not chapters:
        raise BookEntityClaimContractError("document must contain chapters")
    chapter_by_id: dict[str, dict[str, Any]] = {}
    block_by_id: dict[str, dict[str, Any]] = {}
    chapter_block_ids: dict[str, list[str]] = {}
    book_order = 0
    for chapter_position, raw_chapter in enumerate(chapters):
        if not isinstance(raw_chapter, Mapping):
            raise BookEntityClaimContractError("document chapter must be an object")
        chapter_id = _required_string(raw_chapter.get("chapter_id"), "chapter_id")
        if chapter_id in chapter_by_id:
            raise BookEntityClaimContractError("document contains duplicate chapter ids")
        raw_blocks = raw_chapter.get("blocks")
        if not isinstance(raw_blocks, list) or not raw_blocks:
            raise BookEntityClaimContractError("document chapter must contain blocks")
        rows: list[dict[str, Any]] = []
        for raw_block in raw_blocks:
            if not isinstance(raw_block, Mapping):
                raise BookEntityClaimContractError("document block must be an object")
            block_id = _required_string(raw_block.get("block_id"), "block_id")
            rows.append(
                {
                    "block_id": block_id,
                    "chapter_id": chapter_id,
                    "order_index": int(raw_block.get("order_index") or 0),
                    "block_type": str(raw_block.get("block_type") or "paragraph"),
                    "text": _block_text(raw_block),
                }
            )
        rows.sort(key=lambda row: (row["order_index"], row["block_id"]))
        if len({row["block_id"] for row in rows}) != len(rows):
            raise BookEntityClaimContractError("chapter contains duplicate block ids")
        ids: list[str] = []
        for chapter_block_position, row in enumerate(rows):
            block_id = row["block_id"]
            if block_id in block_by_id:
                raise BookEntityClaimContractError("book contains duplicate block ids")
            view = {
                **row,
                "chapter_position": chapter_position,
                "chapter_block_position": chapter_block_position,
                "book_order_index": book_order,
            }
            book_order += 1
            block_by_id[block_id] = view
            ids.append(block_id)
        chapter_block_ids[chapter_id] = ids
        chapter_by_id[chapter_id] = dict(raw_chapter)
    return {
        "chapter_by_id": chapter_by_id,
        "block_by_id": block_by_id,
        "chapter_block_ids": chapter_block_ids,
    }


def _verified_artifact(
    artifact: Mapping[str, Any],
    *,
    chapter: Mapping[str, Any],
    prior_cards: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    if not isinstance(artifact, Mapping):
        raise BookEntityClaimContractError("challenge artifact must be an object")
    body = dict(artifact)
    observed = _hash_string(
        body.pop("prior_challenge_artifact_hash", None),
        "prior challenge artifact hash",
    )
    if canonical_hash(body) != observed:
        raise BookEntityClaimContractError("prior challenge artifact hash mismatch")
    if artifact.get("schema_version") != EXPERIMENT_SCHEMA_VERSION:
        raise BookEntityClaimContractError("foreign prior challenge artifact schema")
    chapter_id = _required_string(artifact.get("chapter_id"), "artifact chapter_id")
    if chapter_id != chapter.get("chapter_id"):
        raise BookEntityClaimContractError("challenge artifact targets a foreign chapter")
    _, expected_manifest_hash = build_prior_packets(
        chapter=chapter,
        prior_cards=prior_cards,
    )
    if artifact.get("prior_manifest_hash") != expected_manifest_hash:
        raise BookEntityClaimContractError("challenge artifact prior manifest mismatch")
    dispositions = artifact.get("prior_card_dispositions")
    tickets = artifact.get("prior_conflict_tickets")
    if not isinstance(dispositions, list) or not isinstance(tickets, list):
        raise BookEntityClaimContractError("challenge artifact lacks disposition tables")
    expected_tickets: list[dict[str, Any]] = []
    for row in dispositions:
        if not isinstance(row, Mapping):
            raise BookEntityClaimContractError("challenge disposition must be an object")
        if row.get("verdict") == "challenge":
            expected_tickets.append(
                {
                    "prior_card_id": row.get("prior_card_id"),
                    "issue_code": row.get("issue_code"),
                    "disputed_field": row.get("disputed_field"),
                    "referent_continuity": row.get("referent_continuity"),
                    "source_block_ids": row.get("source_block_ids"),
                    "reason": row.get("reason"),
                }
            )
    sort_key = lambda row: (
        str(row.get("prior_card_id")),
        str(row.get("issue_code")),
        str(row.get("disputed_field")),
    )
    if canonical_json(sorted(expected_tickets, key=sort_key)) != canonical_json(
        sorted(tickets, key=sort_key)
    ):
        raise BookEntityClaimContractError(
            "challenge ticket table differs from challenged dispositions"
        )
    return _clone(dict(artifact))


def _prior_claim_value(card: Mapping[str, Any], field: str) -> Any:
    source_key = {
        "referent_kind": "referent_kind",
        "referential_gender": "referential_gender",
        "identity_summary": "identity_summary",
    }.get(field)
    return _clone(card.get(source_key)) if source_key else None


def _add_neighbors(
    roles: dict[str, set[str]],
    *,
    block_id: str,
    role: str,
    catalog: Mapping[str, Any],
    radius: int,
) -> None:
    block = catalog["block_by_id"][block_id]
    ids = catalog["chapter_block_ids"][block["chapter_id"]]
    position = block["chapter_block_position"]
    for offset in range(1, radius + 1):
        for index in (position - offset, position + offset):
            if 0 <= index < len(ids):
                roles[ids[index]].add(role)


def _add_side_closure(
    roles: dict[str, set[str]],
    unresolved: list[str],
    *,
    direct_block_id: str,
    prefix: str,
    stable_surfaces: Sequence[str],
    catalog: Mapping[str, Any],
    max_bridge_blocks: int,
    neighbor_radius: int,
) -> None:
    block_by_id = catalog["block_by_id"]
    if direct_block_id not in block_by_id:
        raise BookEntityClaimContractError(
            f"ticket cites foreign source block {direct_block_id}"
        )
    direct = block_by_id[direct_block_id]
    roles[direct_block_id].add(f"{prefix}_direct")
    ids = catalog["chapter_block_ids"][direct["chapter_id"]]
    direct_position = direct["chapter_block_position"]
    if any(_contains_surface(direct["text"], surface) for surface in stable_surfaces):
        anchor_id = direct_block_id
    else:
        anchors = [
            block_by_id[block_id]
            for block_id in ids
            if any(
                _contains_surface(block_by_id[block_id]["text"], surface)
                for surface in stable_surfaces
            )
        ]
        if not anchors:
            unresolved.append(f"{prefix}_anchor_missing:{direct_block_id}")
            _add_neighbors(
                roles,
                block_id=direct_block_id,
                role=f"{prefix}_neighbor",
                catalog=catalog,
                radius=neighbor_radius,
            )
            return
        anchor = min(
            anchors,
            key=lambda row: (
                abs(row["chapter_block_position"] - direct_position),
                row["chapter_block_position"],
                row["block_id"],
            ),
        )
        anchor_id = anchor["block_id"]
        lower = min(anchor["chapter_block_position"], direct_position)
        upper = max(anchor["chapter_block_position"], direct_position)
        bridge_ids = ids[lower + 1 : upper]
        if len(bridge_ids) > max_bridge_blocks:
            unresolved.append(f"{prefix}_bridge_cap:{direct_block_id}")
            _add_neighbors(
                roles,
                block_id=direct_block_id,
                role=f"{prefix}_neighbor",
                catalog=catalog,
                radius=neighbor_radius,
            )
            return
        for block_id in bridge_ids:
            roles[block_id].add(f"{prefix}_bridge")
    roles[anchor_id].add(f"{prefix}_anchor")
    for block_id in {direct_block_id, anchor_id}:
        _add_neighbors(
            roles,
            block_id=block_id,
            role=f"{prefix}_neighbor",
            catalog=catalog,
            radius=neighbor_radius,
        )


def _evidence_closure(
    *,
    card: Mapping[str, Any],
    current_block_ids: Sequence[str],
    catalog: Mapping[str, Any],
    max_bridge_blocks: int,
    neighbor_radius: int,
) -> dict[str, Any]:
    roles: dict[str, set[str]] = defaultdict(set)
    unresolved: list[str] = []
    for ref in card["provenance_refs"]:
        _add_side_closure(
            roles,
            unresolved,
            direct_block_id=ref["block_id"],
            prefix="prior",
            stable_surfaces=card["stable_surfaces"],
            catalog=catalog,
            max_bridge_blocks=max_bridge_blocks,
            neighbor_radius=neighbor_radius,
        )
    for block_id in current_block_ids:
        _add_side_closure(
            roles,
            unresolved,
            direct_block_id=block_id,
            prefix="current",
            stable_surfaces=card["stable_surfaces"],
            catalog=catalog,
            max_bridge_blocks=max_bridge_blocks,
            neighbor_radius=neighbor_radius,
        )
    ordered = sorted(
        roles,
        key=lambda block_id: catalog["block_by_id"][block_id]["book_order_index"],
    )
    return {
        "state": "insufficient" if unresolved else "ready",
        "unresolved_reasons": sorted(set(unresolved)),
        "source_blocks": [
            {
                **_clone(catalog["block_by_id"][block_id]),
                "evidence_roles": sorted(roles[block_id]),
            }
            for block_id in ordered
        ],
    }


class _UnionFind:
    def __init__(self, values: Iterable[str]) -> None:
        self.parent = {value: value for value in values}

    def find(self, value: str) -> str:
        parent = self.parent[value]
        if parent != value:
            self.parent[value] = self.find(parent)
        return self.parent[value]

    def union(self, left: str, right: str) -> None:
        left_root = self.find(left)
        right_root = self.find(right)
        if left_root != right_root:
            self.parent[max(left_root, right_root)] = min(left_root, right_root)


def _validated_gists(
    chapter_gists: Mapping[str, Any] | None,
    *,
    catalog: Mapping[str, Any],
    max_utf8_bytes: int,
) -> list[dict[str, str]]:
    if chapter_gists is None:
        return []
    if not isinstance(chapter_gists, Mapping):
        raise BookEntityClaimContractError("chapter_gists must be a mapping")
    rows: list[dict[str, str]] = []
    for chapter_id, value in chapter_gists.items():
        if chapter_id not in catalog["chapter_by_id"]:
            raise BookEntityClaimContractError("chapter gist targets a foreign chapter")
        gist = _required_string(value, "chapter gist")
        if len(gist.encode("utf-8")) > max_utf8_bytes:
            raise BookEntityClaimContractError("chapter gist exceeds byte cap")
        rows.append({"chapter_id": str(chapter_id), "gist": gist})
    rows.sort(
        key=lambda row: list(catalog["chapter_by_id"]).index(row["chapter_id"])
    )
    return rows


def build_prior_claim_ticket_index_v1(
    *,
    document: Mapping[str, Any],
    prior_cards: Sequence[Mapping[str, Any]],
    challenge_artifacts: Sequence[Mapping[str, Any]],
    registry_generation_hash: str,
    chapter_gists: Mapping[str, Any] | None = None,
    max_tickets_per_component: int = DEFAULT_MAX_TICKETS_PER_COMPONENT,
    max_involved_chapters: int = DEFAULT_MAX_INVOLVED_CHAPTERS,
    max_source_blocks: int = DEFAULT_MAX_SOURCE_BLOCKS,
    max_bridge_blocks: int = DEFAULT_MAX_BRIDGE_BLOCKS,
    neighbor_radius: int = DEFAULT_NEIGHBOR_RADIUS,
    max_gist_utf8_bytes: int = DEFAULT_MAX_GIST_UTF8_BYTES,
) -> dict[str, Any]:
    bounds = (
        max_tickets_per_component,
        max_involved_chapters,
        max_source_blocks,
        max_bridge_blocks,
    )
    if any(value < 1 for value in bounds) or neighbor_radius < 0:
        raise BookEntityClaimContractError("claim component bounds are invalid")
    generation_hash = _hash_string(
        registry_generation_hash, "registry_generation_hash"
    )
    catalog = _document_catalog(document)
    source_manifest = build_book_source_manifest(document)
    lineage_id = state_lineage_id_for_manifest(source_manifest)
    cards = validate_prior_cards(prior_cards)
    card_by_id = {row["prior_card_id"]: row for row in cards}
    if len(card_by_id) != len(cards):
        raise BookEntityClaimContractError("prior cards contain duplicate ids")
    gists = _validated_gists(
        chapter_gists,
        catalog=catalog,
        max_utf8_bytes=max_gist_utf8_bytes,
    )

    tickets_by_id: dict[str, dict[str, Any]] = {}
    uncertainties_by_id: dict[str, dict[str, Any]] = {}
    for raw_artifact in challenge_artifacts:
        chapter_id = _required_string(raw_artifact.get("chapter_id"), "chapter_id")
        chapter = catalog["chapter_by_id"].get(chapter_id)
        if chapter is None:
            raise BookEntityClaimContractError("artifact chapter is absent from document")
        raw_dispositions = raw_artifact.get("prior_card_dispositions")
        if not isinstance(raw_dispositions, list):
            raise BookEntityClaimContractError(
                "challenge artifact prior-card dispositions must be a list"
            )
        artifact_card_ids = sorted(
            {
                _required_string(row.get("prior_card_id"), "disposition prior_card_id")
                for row in raw_dispositions
                if isinstance(row, Mapping)
            }
        )
        if len(artifact_card_ids) != len(raw_dispositions):
            raise BookEntityClaimContractError(
                "artifact dispositions contain duplicate or malformed prior-card ids"
            )
        try:
            artifact_cards = [card_by_id[card_id] for card_id in artifact_card_ids]
        except KeyError as exc:
            raise BookEntityClaimContractError(
                "artifact disposition targets a foreign prior card"
            ) from exc
        artifact = _verified_artifact(
            raw_artifact,
            chapter=chapter,
            prior_cards=artifact_cards,
        )
        artifact_hash = str(artifact["prior_challenge_artifact_hash"])
        request_fingerprint = _hash_string(
            artifact.get("request_fingerprint"), "origin request fingerprint"
        )
        context_manifest_body = {
            "schema_version": "prior_claim_origin_context_v1",
            "chapter_id": chapter_id,
            "chapter_source_hash": chapter_source_hash(chapter),
            "prior_manifest_hash": artifact["prior_manifest_hash"],
            "request_fingerprint": request_fingerprint,
            "challenge_artifact_hash": artifact_hash,
        }
        context_manifest_hash = canonical_hash(context_manifest_body)
        for disposition in artifact["prior_card_dispositions"]:
            if disposition["verdict"] != "uncertain":
                continue
            prior_card_id = _required_string(
                disposition.get("prior_card_id"), "uncertain prior_card_id"
            )
            card = card_by_id.get(prior_card_id)
            if card is None:
                raise BookEntityClaimContractError(
                    "uncertain disposition targets a foreign prior card"
                )
            current_ids = _string_list(
                disposition.get("source_block_ids"),
                "uncertain current source block ids",
            )
            closure = _evidence_closure(
                card=card,
                current_block_ids=current_ids,
                catalog=catalog,
                max_bridge_blocks=max_bridge_blocks,
                neighbor_radius=neighbor_radius,
            )
            uncertainty_body = {
                "state_lineage_id": lineage_id,
                "book_source_manifest_hash": source_manifest["manifest_hash"],
                "registry_generation_hash": generation_hash,
                "prior_card_id": prior_card_id,
                "prior_card_hash": canonical_hash(card),
                "challenge_artifact_hash": artifact_hash,
                "origin_request_fingerprint": request_fingerprint,
                "origin_context_manifest_hash": context_manifest_hash,
                "current_source_block_ids": current_ids,
                "reason": _required_string(
                    disposition.get("reason"), "uncertain disposition reason"
                ),
                "lifecycle_state": "open",
                "authority_effect": "candidate_only",
                "evidence_state": closure["state"],
                "evidence_unresolved_reasons": closure["unresolved_reasons"],
            }
            uncertainty_id = "bunc1_" + canonical_hash(uncertainty_body)[:20]
            uncertainty = {
                "uncertainty_id": uncertainty_id,
                **uncertainty_body,
                "evidence_source_blocks": closure["source_blocks"],
            }
            prior = uncertainties_by_id.get(uncertainty_id)
            if prior is not None and canonical_json(prior) != canonical_json(uncertainty):
                raise BookEntityClaimContractError(
                    "uncertainty id collision with unequal bytes"
                )
            uncertainties_by_id[uncertainty_id] = uncertainty
        for raw_ticket in artifact["prior_conflict_tickets"]:
            if not isinstance(raw_ticket, Mapping):
                raise BookEntityClaimContractError("challenge ticket must be an object")
            prior_card_id = _required_string(
                raw_ticket.get("prior_card_id"), "ticket prior_card_id"
            )
            card = card_by_id.get(prior_card_id)
            if card is None:
                raise BookEntityClaimContractError("ticket targets a foreign prior card")
            issue_code = _required_string(raw_ticket.get("issue_code"), "issue_code")
            if issue_code not in ISSUE_TO_FIELD:
                raise BookEntityClaimContractError("ticket issue is outside closed enum")
            disputed_field = _required_string(
                raw_ticket.get("disputed_field"), "disputed_field"
            )
            referent_continuity = _required_string(
                raw_ticket.get("referent_continuity"), "referent_continuity"
            )
            if referent_continuity not in REFERENT_CONTINUITIES - {"uncertain"}:
                raise BookEntityClaimContractError(
                    "challenge ticket has invalid referent continuity"
                )
            if referent_continuity == "same_referent":
                if issue_code not in CLAIM_ISSUES or ISSUE_TO_FIELD[issue_code] != disputed_field:
                    raise BookEntityClaimContractError(
                        "same-referent ticket issue and field disagree"
                    )
            elif issue_code not in IDENTITY_ISSUES:
                raise BookEntityClaimContractError(
                    "possible-collision ticket lacks an identity issue"
                )
            current_ids = _string_list(
                raw_ticket.get("source_block_ids"), "current challenge block ids"
            )
            for block_id in current_ids:
                block = catalog["block_by_id"].get(block_id)
                if block is None or block["chapter_id"] != chapter_id:
                    raise BookEntityClaimContractError(
                        "ticket cites a foreign current source block"
                    )
            route = (
                "claim_auditor"
                if referent_continuity == "same_referent"
                and disputed_field in {
                    "referent_kind",
                    "referential_gender",
                    "identity_summary",
                }
                else "identity_auditor"
            )
            card_hash = canonical_hash(card)
            ticket_body = {
                "state_lineage_id": lineage_id,
                "book_source_manifest_hash": source_manifest["manifest_hash"],
                "registry_generation_hash": generation_hash,
                "prior_card_id": prior_card_id,
                "challenged_prior_card_hash": card_hash,
                "challenge_artifact_hash": artifact_hash,
                "origin_request_fingerprint": request_fingerprint,
                "origin_context_manifest_hash": context_manifest_hash,
                "issue_code": issue_code,
                "disputed_field": disputed_field,
                "referent_continuity": referent_continuity,
                "prior_claim_value": _prior_claim_value(card, disputed_field),
                "prior_provenance_refs": _clone(card["provenance_refs"]),
                "current_challenge_block_ids": current_ids,
                "reason": _required_string(raw_ticket.get("reason"), "ticket reason"),
                "route": route,
            }
            ticket_id = "bclaimtk1_" + canonical_hash(ticket_body)[:20]
            closure = _evidence_closure(
                card=card,
                current_block_ids=current_ids,
                catalog=catalog,
                max_bridge_blocks=max_bridge_blocks,
                neighbor_radius=neighbor_radius,
            )
            ticket = {
                "ticket_id": ticket_id,
                **ticket_body,
                "evidence_state": closure["state"],
                "evidence_unresolved_reasons": closure["unresolved_reasons"],
                "evidence_source_blocks": closure["source_blocks"],
            }
            prior = tickets_by_id.get(ticket_id)
            if prior is not None and canonical_json(prior) != canonical_json(ticket):
                raise BookEntityClaimContractError("ticket id collision with unequal bytes")
            tickets_by_id[ticket_id] = ticket

    tickets = sorted(tickets_by_id.values(), key=lambda row: row["ticket_id"])
    uncertainties = sorted(
        uncertainties_by_id.values(), key=lambda row: row["uncertainty_id"]
    )
    ready_claims = [
        row
        for row in tickets
        if row["route"] == "claim_auditor" and row["evidence_state"] == "ready"
    ]
    preflight_pending = [
        row["ticket_id"]
        for row in tickets
        if row["route"] == "claim_auditor" and row["evidence_state"] != "ready"
    ]
    identity_referrals = [
        {
            "ticket_id": row["ticket_id"],
            "prior_card_id": row["prior_card_id"],
            "issue_code": row["issue_code"],
            "disputed_field": row["disputed_field"],
            "evidence_source_block_ids": [
                block["block_id"] for block in row["evidence_source_blocks"]
            ],
            "route": "identity_auditor",
        }
        for row in tickets
        if row["route"] == "identity_auditor"
    ]

    uf = _UnionFind(row["ticket_id"] for row in ready_claims)
    for left_index, left in enumerate(ready_claims):
        left_blocks = {
            row["block_id"] for row in left["evidence_source_blocks"]
        }
        for right in ready_claims[left_index + 1 :]:
            right_blocks = {
                row["block_id"] for row in right["evidence_source_blocks"]
            }
            if (
                left["prior_card_id"] == right["prior_card_id"]
                or left_blocks.intersection(right_blocks)
            ):
                uf.union(left["ticket_id"], right["ticket_id"])
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in ready_claims:
        grouped[uf.find(row["ticket_id"])].append(row)

    components: list[dict[str, Any]] = []
    for rows in grouped.values():
        rows.sort(key=lambda row: row["ticket_id"])
        source_by_id: dict[str, dict[str, Any]] = {}
        for ticket in rows:
            for block in ticket["evidence_source_blocks"]:
                source = source_by_id.setdefault(
                    block["block_id"],
                    {
                        key: _clone(value)
                        for key, value in block.items()
                        if key != "evidence_roles"
                    }
                    | {"evidence_roles": set(), "ticket_ids": set()},
                )
                source["evidence_roles"].update(block["evidence_roles"])
                source["ticket_ids"].add(ticket["ticket_id"])
        source_blocks = [
            {
                **{
                    key: value
                    for key, value in row.items()
                    if key not in {"evidence_roles", "ticket_ids"}
                },
                "evidence_roles": sorted(row["evidence_roles"]),
                "ticket_ids": sorted(row["ticket_ids"]),
            }
            for row in sorted(
                source_by_id.values(), key=lambda value: value["book_order_index"]
            )
        ]
        chapter_ids = sorted(
            {row["chapter_id"] for row in source_blocks},
            key=lambda chapter_id: list(catalog["chapter_by_id"]).index(chapter_id),
        )
        overflow_reasons: list[str] = []
        if len(rows) > max_tickets_per_component:
            overflow_reasons.append("ticket_count_cap")
        if len(chapter_ids) > max_involved_chapters:
            overflow_reasons.append("chapter_count_cap")
        if len(source_blocks) > max_source_blocks:
            overflow_reasons.append("source_block_count_cap")
        component_body = {
            "ticket_ids": [row["ticket_id"] for row in rows],
            "prior_card_ids": sorted({row["prior_card_id"] for row in rows}),
            "chapter_ids": chapter_ids,
            "source_block_manifest_hash": canonical_hash(
                [
                    {
                        "block_id": row["block_id"],
                        "roles": row["evidence_roles"],
                        "ticket_ids": row["ticket_ids"],
                    }
                    for row in source_blocks
                ]
            ),
        }
        component_id = "bclaimcomp1_" + canonical_hash(component_body)[:20]
        components.append(
            {
                "component_id": component_id,
                **component_body,
                "source_blocks": source_blocks,
                "overflow": bool(overflow_reasons),
                "overflow_reasons": overflow_reasons,
            }
        )
    components.sort(key=lambda row: row["component_id"])

    body = {
        "schema_version": CLAIM_INDEX_SCHEMA_VERSION,
        "validator_version": CLAIM_VALIDATOR_VERSION,
        "state_lineage_id": lineage_id,
        "book_source_manifest": source_manifest,
        "book_source_manifest_hash": source_manifest["manifest_hash"],
        "registry_generation_hash": generation_hash,
        "prior_cards": cards,
        "chapter_gists": gists,
        "ticket_rows": tickets,
        "uncertainty_rows": uncertainties,
        "claim_components": components,
        "preflight_pending_ticket_ids": sorted(preflight_pending),
        "identity_referrals": sorted(
            identity_referrals, key=lambda row: row["ticket_id"]
        ),
        "component_bounds": {
            "max_tickets_per_component": max_tickets_per_component,
            "max_involved_chapters": max_involved_chapters,
            "max_source_blocks": max_source_blocks,
            "max_bridge_blocks": max_bridge_blocks,
            "neighbor_radius": neighbor_radius,
            "max_gist_utf8_bytes": max_gist_utf8_bytes,
        },
        "complete_ticket_coverage": True,
        "complete_uncertainty_coverage": True,
        "semantic_halt_required": False,
    }
    return {**body, "ticket_index_hash": canonical_hash(body)}


def verify_prior_claim_ticket_index_v1(
    index: Mapping[str, Any],
    *,
    document: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    if index.get("schema_version") != CLAIM_INDEX_SCHEMA_VERSION:
        raise BookEntityClaimContractError("foreign claim ticket index schema")
    if index.get("validator_version") != CLAIM_VALIDATOR_VERSION:
        raise BookEntityClaimContractError("claim ticket index validator mismatch")
    body = dict(index)
    observed = _hash_string(body.pop("ticket_index_hash", None), "ticket index hash")
    if canonical_hash(body) != observed:
        raise BookEntityClaimContractError("claim ticket index hash mismatch")
    if index.get("complete_ticket_coverage") is not True:
        raise BookEntityClaimContractError("partial claim ticket index cannot be used")
    if index.get("complete_uncertainty_coverage") is not True:
        raise BookEntityClaimContractError(
            "partial uncertainty index cannot be used"
        )
    uncertainty_rows = index.get("uncertainty_rows")
    if not isinstance(uncertainty_rows, list):
        raise BookEntityClaimContractError("uncertainty rows must be a list")
    uncertainty_ids = [
        _required_string(row.get("uncertainty_id"), "uncertainty_id")
        for row in uncertainty_rows
        if isinstance(row, Mapping)
    ]
    if len(uncertainty_ids) != len(uncertainty_rows) or len(
        uncertainty_ids
    ) != len(set(uncertainty_ids)):
        raise BookEntityClaimContractError(
            "uncertainty rows contain malformed or duplicate ids"
        )
    card_ids = {row["prior_card_id"] for row in index.get("prior_cards") or []}
    for row in uncertainty_rows:
        if row.get("prior_card_id") not in card_ids:
            raise BookEntityClaimContractError(
                "uncertainty row targets a foreign prior card"
            )
        if row.get("lifecycle_state") != "open":
            raise BookEntityClaimContractError(
                "uncertainty row has a foreign lifecycle state"
            )
        if row.get("authority_effect") != "candidate_only":
            raise BookEntityClaimContractError(
                "uncertainty row has unsafe authority"
            )
    if index.get("semantic_halt_required") is not False:
        raise BookEntityClaimContractError("semantic ticket index incorrectly requests halt")
    if document is not None:
        verify_book_source_manifest(document, index["book_source_manifest"])
    return _clone(dict(index))


def prior_claim_response_schema_v1() -> dict[str, Any]:
    action = {
        "type": "object",
        "additionalProperties": False,
        "required": [
            "ticket_id",
            "action",
            "revised_value",
            "source_block_ids",
            "pending_reason_code",
            "resolution_note",
        ],
        "properties": {
            "ticket_id": {"type": "string", "minLength": 1},
            "action": {"type": "string", "enum": sorted(CLAIM_ACTIONS)},
            "revised_value": {
                "anyOf": [{"type": "string"}, {"type": "null"}]
            },
            "source_block_ids": {
                "type": "array",
                "items": {"type": "string", "minLength": 1},
                "minItems": 1,
                "uniqueItems": True,
            },
            "pending_reason_code": {
                "anyOf": [
                    {"type": "string", "enum": sorted(PENDING_REASON_CODES)},
                    {"type": "null"},
                ]
            },
            "resolution_note": {"type": "string", "minLength": 1},
        },
    }
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["component_id", "ticket_actions"],
        "properties": {
            "component_id": {"type": "string", "minLength": 1},
            "ticket_actions": {"type": "array", "items": action},
        },
    }


def render_prior_claim_request_v1(
    *,
    index: Mapping[str, Any],
    component_id: str,
    document: Mapping[str, Any],
    design_doc: Path,
) -> RenderedPriorClaimRequestV1:
    verified = verify_prior_claim_ticket_index_v1(index, document=document)
    component = next(
        (
            row
            for row in verified["claim_components"]
            if row["component_id"] == component_id
        ),
        None,
    )
    if component is None:
        raise BookEntityClaimContractError("unknown claim component")
    if component["overflow"]:
        raise BookEntityClaimContractError("overflow claim component cannot be rendered")
    ticket_by_id = {row["ticket_id"]: row for row in verified["ticket_rows"]}
    card_by_id = {row["prior_card_id"]: row for row in verified["prior_cards"]}
    gist_by_chapter = {
        row["chapter_id"]: row["gist"] for row in verified["chapter_gists"]
    }
    tickets = [ticket_by_id[ticket_id] for ticket_id in component["ticket_ids"]]
    payload = {
        "contract_version": CLAIM_VALIDATOR_VERSION,
        "ticket_index_hash": verified["ticket_index_hash"],
        "registry_generation_hash": verified["registry_generation_hash"],
        "component_id": component_id,
        "tickets": [
            {
                key: _clone(value)
                for key, value in row.items()
                if key not in {"evidence_source_blocks"}
            }
            for row in tickets
        ],
        "prior_cards": [
            card_by_id[prior_card_id] for prior_card_id in component["prior_card_ids"]
        ],
        "chapter_gists": [
            {"chapter_id": chapter_id, "gist": gist_by_chapter[chapter_id]}
            for chapter_id in component["chapter_ids"]
            if chapter_id in gist_by_chapter
        ],
        "source_blocks": _clone(component["source_blocks"]),
        "allowed_revised_values": {
            "referent_kind": sorted(REFERENT_KINDS),
            "referential_gender": sorted(REFERENTIAL_GENDERS),
            "identity_summary": [None],
        },
    }
    prompt = load_book_entity_claim_prompt_v1(design_doc)
    schema = prior_claim_response_schema_v1()
    schema["properties"]["component_id"]["enum"] = [component_id]
    schema["properties"]["ticket_actions"]["items"]["properties"]["ticket_id"][
        "enum"
    ] = list(component["ticket_ids"])
    user_content = canonical_json(payload)
    fingerprint = canonical_hash(
        {
            "prompt_id": PROMPT_ID,
            "prompt_sha256": PROMPT_SHA256,
            "semantic_payload": payload,
            "response_schema": schema,
        }
    )
    return RenderedPriorClaimRequestV1(
        component_id=component_id,
        request_fingerprint=fingerprint,
        messages=(
            {"role": "system", "content": prompt},
            {"role": "user", "content": user_content},
        ),
        response_schema=schema,
        semantic_payload=payload,
    )


def _validate_revised_value(
    *,
    ticket: Mapping[str, Any],
    action: str,
    value: Any,
) -> Any:
    if action != "revise_claim":
        if value is not None:
            raise BookEntityClaimContractError(
                "only revise_claim may carry a revised value"
            )
        return None
    field = ticket["disputed_field"]
    old_value = ticket["prior_claim_value"]
    if field == "referent_kind":
        if value not in REFERENT_KINDS:
            raise BookEntityClaimContractError("foreign referent-kind revision")
    elif field == "referential_gender":
        if value is not None and value not in REFERENTIAL_GENDERS:
            raise BookEntityClaimContractError("foreign gender revision")
    elif field == "identity_summary":
        if value is not None:
            raise BookEntityClaimContractError(
                "V1 cannot author a replacement identity summary"
            )
    else:
        raise BookEntityClaimContractError("claim revision targets a foreign field")
    if canonical_json(value) == canonical_json(old_value):
        raise BookEntityClaimContractError("claim revision does not change the value")
    return _clone(value)


def validate_prior_claim_response_v1(
    response: Mapping[str, Any],
    *,
    index: Mapping[str, Any],
    request_fingerprint: str,
) -> dict[str, Any]:
    verified = verify_prior_claim_ticket_index_v1(index)
    if not isinstance(response, Mapping):
        raise BookEntityClaimContractError("prior-claim response must be an object")
    _exact_keys(response, {"component_id", "ticket_actions"}, "prior-claim response")
    component_id = _required_string(response.get("component_id"), "component_id")
    component = next(
        (
            row
            for row in verified["claim_components"]
            if row["component_id"] == component_id
        ),
        None,
    )
    if component is None or component["overflow"]:
        raise BookEntityClaimContractError(
            "response owns an unknown or overflow claim component"
        )
    ticket_by_id = {
        row["ticket_id"]: row
        for row in verified["ticket_rows"]
        if row["ticket_id"] in set(component["ticket_ids"])
    }
    block_order = {
        row["block_id"]: row["book_order_index"]
        for row in component["source_blocks"]
    }
    raw_actions = response.get("ticket_actions")
    if not isinstance(raw_actions, list):
        raise BookEntityClaimContractError("ticket_actions must be a list")
    normalized: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in raw_actions:
        if not isinstance(raw, Mapping):
            raise BookEntityClaimContractError("ticket action must be an object")
        _exact_keys(
            raw,
            {
                "ticket_id",
                "action",
                "revised_value",
                "source_block_ids",
                "pending_reason_code",
                "resolution_note",
            },
            "ticket action",
        )
        ticket_id = _required_string(raw.get("ticket_id"), "ticket_id")
        if ticket_id not in ticket_by_id or ticket_id in seen:
            raise BookEntityClaimContractError(
                "ticket actions do not exact-cover the component"
            )
        seen.add(ticket_id)
        ticket = ticket_by_id[ticket_id]
        action = _required_string(raw.get("action"), "claim action")
        if action not in CLAIM_ACTIONS:
            raise BookEntityClaimContractError("claim action outside closed enum")
        revised_value = _validate_revised_value(
            ticket=ticket,
            action=action,
            value=raw.get("revised_value"),
        )
        evidence = _string_list(raw.get("source_block_ids"), "claim evidence")
        allowed_blocks = {
            row["block_id"] for row in ticket["evidence_source_blocks"]
        }
        if not set(evidence) <= allowed_blocks:
            raise BookEntityClaimContractError("claim evidence cites foreign blocks")
        roles = {
            row["block_id"]: set(row["evidence_roles"])
            for row in ticket["evidence_source_blocks"]
        }
        selected_roles = set().union(*(roles[block_id] for block_id in evidence))
        if action != "pending" and not (
            any(role.startswith("prior_") for role in selected_roles)
            and any(role.startswith("current_") for role in selected_roles)
        ):
            raise BookEntityClaimContractError(
                "resolved claim action must cite prior and current evidence"
            )
        pending_reason_code = raw.get("pending_reason_code")
        if action == "pending":
            if pending_reason_code not in PENDING_REASON_CODES:
                raise BookEntityClaimContractError(
                    "pending claim action lacks a closed pending reason"
                )
        elif pending_reason_code is not None:
            raise BookEntityClaimContractError(
                "resolved/referral claim action cannot carry a pending reason"
            )
        normalized.append(
            {
                "ticket_id": ticket_id,
                "action": action,
                "revised_value": revised_value,
                "source_block_ids": sorted(evidence, key=block_order.__getitem__),
                "pending_reason_code": pending_reason_code,
                "resolution_note": _required_string(
                    raw.get("resolution_note"), "resolution_note"
                ),
            }
        )
    if seen != set(component["ticket_ids"]):
        raise BookEntityClaimContractError(
            "ticket actions must exact-cover the component"
        )
    projected_values: dict[tuple[str, str], set[str]] = defaultdict(set)
    unresolved_keys: set[tuple[str, str]] = set()
    for row in normalized:
        ticket = ticket_by_id[row["ticket_id"]]
        key = (ticket["prior_card_id"], ticket["disputed_field"])
        if row["action"] in {"pending", "refer_identity_conflict"}:
            unresolved_keys.add(key)
        else:
            value = (
                ticket["prior_claim_value"]
                if row["action"] == "retain_prior"
                else row["revised_value"]
            )
            projected_values[key].add(canonical_json(value))
    if any(len(values) > 1 for values in projected_values.values()):
        raise BookEntityClaimContractError(
            "same prior-card field receives conflicting projected values"
        )
    body = {
        "schema_version": CLAIM_DECISION_SCHEMA_VERSION,
        "validator_version": CLAIM_VALIDATOR_VERSION,
        "ticket_index_hash": verified["ticket_index_hash"],
        "registry_generation_hash": verified["registry_generation_hash"],
        "component_id": component_id,
        "request_fingerprint": _hash_string(
            request_fingerprint, "request fingerprint"
        ),
        "ticket_actions": sorted(normalized, key=lambda row: row["ticket_id"]),
        "unresolved_claim_keys": [
            {"prior_card_id": key[0], "disputed_field": key[1]}
            for key in sorted(unresolved_keys)
        ],
    }
    return {**body, "decision_hash": canonical_hash(body)}


def verify_prior_claim_decision_v1(
    decision: Mapping[str, Any],
    *,
    index: Mapping[str, Any],
) -> dict[str, Any]:
    body = dict(decision)
    observed = _hash_string(body.pop("decision_hash", None), "decision hash")
    if canonical_hash(body) != observed:
        raise BookEntityClaimContractError("prior-claim decision hash mismatch")
    if decision.get("schema_version") != CLAIM_DECISION_SCHEMA_VERSION:
        raise BookEntityClaimContractError("foreign prior-claim decision schema")
    if decision.get("validator_version") != CLAIM_VALIDATOR_VERSION:
        raise BookEntityClaimContractError("prior-claim decision validator mismatch")
    normalized = validate_prior_claim_response_v1(
        {
            "component_id": decision.get("component_id"),
            "ticket_actions": _clone(decision.get("ticket_actions")),
        },
        index=index,
        request_fingerprint=_required_string(
            decision.get("request_fingerprint"), "request fingerprint"
        ),
    )
    if canonical_json(normalized) != canonical_json(decision):
        raise BookEntityClaimContractError(
            "decision artifact is not canonical validator output"
        )
    return normalized


def build_prior_claim_revision_ledger_v1(
    *,
    index: Mapping[str, Any],
    decisions: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    verified = verify_prior_claim_ticket_index_v1(index)
    decision_by_component: dict[str, dict[str, Any]] = {}
    for raw in decisions:
        decision = verify_prior_claim_decision_v1(raw, index=verified)
        component_id = decision["component_id"]
        if component_id in decision_by_component:
            raise BookEntityClaimContractError("duplicate claim component decision")
        decision_by_component[component_id] = decision
    required_components = {
        row["component_id"]
        for row in verified["claim_components"]
        if not row["overflow"]
    }
    if set(decision_by_component) != required_components:
        raise BookEntityClaimContractError(
            "decisions do not exact-cover non-overflow claim components"
        )
    ticket_by_id = {row["ticket_id"]: row for row in verified["ticket_rows"]}
    action_by_ticket: dict[str, tuple[dict[str, Any], str]] = {}
    for component_id, decision in decision_by_component.items():
        for action in decision["ticket_actions"]:
            action_by_ticket[action["ticket_id"]] = (action, decision["decision_hash"])
    revisions: list[dict[str, Any]] = []
    overflow_ticket_ids = {
        ticket_id
        for component in verified["claim_components"]
        if component["overflow"]
        for ticket_id in component["ticket_ids"]
    }
    identity_ids = {row["ticket_id"] for row in verified["identity_referrals"]}
    preflight_pending = set(verified["preflight_pending_ticket_ids"])
    for ticket in verified["ticket_rows"]:
        ticket_id = ticket["ticket_id"]
        decision_hash: str | None = None
        selected_source_ids: list[str] = []
        new_value: Any = None
        pending_reason_code: str | None = None
        if ticket_id in identity_ids:
            status = "identity_referral"
            action_name = "refer_identity_conflict"
        elif ticket_id in preflight_pending:
            status = "pending_preflight"
            action_name = "pending"
            pending_reason_code = "insufficient_context"
        elif ticket_id in overflow_ticket_ids:
            status = "pending_overflow"
            action_name = "pending"
            pending_reason_code = "insufficient_context"
        else:
            action, decision_hash = action_by_ticket[ticket_id]
            action_name = action["action"]
            selected_source_ids = action["source_block_ids"]
            pending_reason_code = action.get("pending_reason_code")
            if action_name == "retain_prior":
                status = "retained"
                new_value = _clone(ticket["prior_claim_value"])
            elif action_name == "revise_claim":
                status = "revised"
                new_value = _clone(action["revised_value"])
            elif action_name == "refer_identity_conflict":
                status = "identity_referral"
            else:
                status = "pending"
        order = {
            row["block_id"]: row["book_order_index"]
            for row in ticket["evidence_source_blocks"]
        }
        effective_from = (
            min(selected_source_ids, key=order.__getitem__)
            if status == "revised" and selected_source_ids
            else (
                next(
                    (
                        ref["block_id"]
                        for ref in ticket["prior_provenance_refs"]
                        if ref["block_id"]
                        == next(
                            card["first_supported_block_id"]
                            for card in verified["prior_cards"]
                            if card["prior_card_id"] == ticket["prior_card_id"]
                        )
                    ),
                    None,
                )
                if status == "retained"
                else None
            )
        )
        revision_body = {
            "ticket_id": ticket_id,
            "prior_card_id": ticket["prior_card_id"],
            "challenged_prior_card_hash": ticket["challenged_prior_card_hash"],
            "issue_code": ticket["issue_code"],
            "disputed_field": ticket["disputed_field"],
            "old_value": _clone(ticket["prior_claim_value"]),
            "new_value": new_value,
            "action": action_name,
            "status": status,
            "selected_source_block_ids": selected_source_ids,
            "prior_provenance_refs": _clone(ticket["prior_provenance_refs"]),
            "current_challenge_block_ids": _clone(
                ticket["current_challenge_block_ids"]
            ),
            "effective_from_block_id": effective_from,
            "decision_hash": decision_hash,
            "pending_reason_code": pending_reason_code,
            "claim_case_key": canonical_hash(
                {
                    "state_lineage_id": verified["state_lineage_id"],
                    "prior_card_id": ticket["prior_card_id"],
                    "disputed_field": ticket["disputed_field"],
                }
            ),
            "evidence_manifest_hash": canonical_hash(
                {
                    "prior_card_hash": ticket["challenged_prior_card_hash"],
                    "current_challenge_block_ids": ticket[
                        "current_challenge_block_ids"
                    ],
                    "evidence_source_block_ids": [
                        block["block_id"]
                        for block in ticket["evidence_source_blocks"]
                    ],
                }
            ),
            "registry_generation_hash": verified["registry_generation_hash"],
            "state_lineage_id": verified["state_lineage_id"],
        }
        revisions.append(
            {
                "revision_id": "bclaimrev1_" + canonical_hash(revision_body)[:20],
                **revision_body,
            }
        )
    revisions.sort(key=lambda row: row["revision_id"])
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in revisions:
        grouped[(row["prior_card_id"], row["disputed_field"])].append(row)
    active: list[dict[str, Any]] = []
    pending: list[dict[str, Any]] = []
    for key, rows in sorted(grouped.items()):
        if any(
            row["status"]
            in {"pending", "pending_preflight", "pending_overflow", "identity_referral"}
            for row in rows
        ):
            hearing_count = len(
                {
                    row["evidence_manifest_hash"]
                    for row in rows
                    if row["decision_hash"] is not None
                }
            )
            pending_reasons = sorted(
                {
                    row["pending_reason_code"]
                    for row in rows
                    if row["pending_reason_code"] is not None
                }
            )
            identity_pending = any(
                row["status"] == "identity_referral" for row in rows
            )
            if identity_pending:
                next_trigger = "identity_resolution"
            elif hearing_count >= MAX_AUTOMATIC_HEARINGS:
                next_trigger = "book_end_or_human"
            elif set(pending_reasons).intersection(
                {"insufficient_context", "evidence_not_attributable"}
            ):
                next_trigger = "expanded_or_new_evidence"
            else:
                next_trigger = "new_evidence_or_book_end"
            pending.append(
                {
                    "prior_card_id": key[0],
                    "disputed_field": key[1],
                    "revision_ids": sorted(row["revision_id"] for row in rows),
                    "status": "pending",
                    "pending_reason_codes": pending_reasons,
                    "evidence_manifest_hashes": sorted(
                        {row["evidence_manifest_hash"] for row in rows}
                    ),
                    "hearing_count": hearing_count,
                    "automatic_hearing_limit": MAX_AUTOMATIC_HEARINGS,
                    "same_evidence_reopen_forbidden": True,
                    "next_review_trigger": next_trigger,
                }
            )
            continue
        values = {canonical_json(row["new_value"]) for row in rows}
        if len(values) != 1:
            raise BookEntityClaimContractError(
                "resolved revisions disagree for one prior-card field"
            )
        value = rows[0]["new_value"]
        effective_ids = [
            row["effective_from_block_id"]
            for row in rows
            if row["effective_from_block_id"] is not None
        ]
        order = {
            block["block_id"]: block["book_order_index"]
            for ticket in verified["ticket_rows"]
            for block in ticket["evidence_source_blocks"]
        }
        active.append(
            {
                "prior_card_id": key[0],
                "disputed_field": key[1],
                "projected_value": _clone(value),
                "effective_from_block_id": (
                    min(effective_ids, key=order.__getitem__) if effective_ids else None
                ),
                "revision_ids": sorted(row["revision_id"] for row in rows),
                "status": "active",
            }
        )
    body = {
        "schema_version": CLAIM_LEDGER_SCHEMA_VERSION,
        "validator_version": CLAIM_VALIDATOR_VERSION,
        "state_lineage_id": verified["state_lineage_id"],
        "book_source_manifest_hash": verified["book_source_manifest_hash"],
        "registry_generation_hash": verified["registry_generation_hash"],
        "ticket_index_hash": verified["ticket_index_hash"],
        "decision_hashes": sorted(
            decision["decision_hash"] for decision in decision_by_component.values()
        ),
        "claim_revision_rows": revisions,
        "active_claim_projection": active,
        "pending_claims": pending,
        "identity_referrals": _clone(verified["identity_referrals"]),
        "pending_identity_reviews": [
            {
                "uncertainty_id": row["uncertainty_id"],
                "prior_card_id": row["prior_card_id"],
                "status": "pending",
                "authority_effect": row["authority_effect"],
                "current_source_block_ids": _clone(row["current_source_block_ids"]),
                "reason": row["reason"],
                "evidence_state": row["evidence_state"],
            }
            for row in verified["uncertainty_rows"]
        ],
    }
    return {**body, "claim_ledger_hash": canonical_hash(body)}


def verify_prior_claim_revision_ledger_v1(ledger: Mapping[str, Any]) -> dict[str, Any]:
    if ledger.get("schema_version") != CLAIM_LEDGER_SCHEMA_VERSION:
        raise BookEntityClaimContractError("foreign prior-claim ledger schema")
    if ledger.get("validator_version") != CLAIM_VALIDATOR_VERSION:
        raise BookEntityClaimContractError("prior-claim ledger validator mismatch")
    body = dict(ledger)
    observed = _hash_string(body.pop("claim_ledger_hash", None), "claim ledger hash")
    if canonical_hash(body) != observed:
        raise BookEntityClaimContractError("prior-claim ledger hash mismatch")
    for field in (
        "claim_revision_rows",
        "active_claim_projection",
        "pending_claims",
        "identity_referrals",
        "pending_identity_reviews",
    ):
        if not isinstance(ledger.get(field), list):
            raise BookEntityClaimContractError(f"{field} must be a list")
    revision_by_id: dict[str, Mapping[str, Any]] = {}
    for row in ledger["claim_revision_rows"]:
        if not isinstance(row, Mapping):
            raise BookEntityClaimContractError("claim revision must be an object")
        _exact_keys(
            row,
            {
                "revision_id",
                "ticket_id",
                "prior_card_id",
                "challenged_prior_card_hash",
                "issue_code",
                "disputed_field",
                "old_value",
                "new_value",
                "action",
                "status",
                "selected_source_block_ids",
                "prior_provenance_refs",
                "current_challenge_block_ids",
                "effective_from_block_id",
                "decision_hash",
                "pending_reason_code",
                "claim_case_key",
                "evidence_manifest_hash",
                "registry_generation_hash",
                "state_lineage_id",
            },
            "claim revision",
        )
        revision_id = _required_string(row.get("revision_id"), "revision_id")
        if revision_id in revision_by_id:
            raise BookEntityClaimContractError("duplicate claim revision id")
        revision_body = dict(row)
        revision_body.pop("revision_id")
        if revision_id != "bclaimrev1_" + canonical_hash(revision_body)[:20]:
            raise BookEntityClaimContractError("claim revision id is not content-addressed")
        if row.get("state_lineage_id") != ledger.get("state_lineage_id"):
            raise BookEntityClaimContractError("claim revision lineage mismatch")
        if row.get("registry_generation_hash") != ledger.get(
            "registry_generation_hash"
        ):
            raise BookEntityClaimContractError("claim revision generation mismatch")
        expected_case_key = canonical_hash(
            {
                "state_lineage_id": ledger["state_lineage_id"],
                "prior_card_id": row.get("prior_card_id"),
                "disputed_field": row.get("disputed_field"),
            }
        )
        if row.get("claim_case_key") != expected_case_key:
            raise BookEntityClaimContractError("claim revision case key mismatch")
        _hash_string(row.get("evidence_manifest_hash"), "evidence manifest hash")
        status = row.get("status")
        reason = row.get("pending_reason_code")
        if status in {"pending", "pending_preflight", "pending_overflow"}:
            if reason not in PENDING_REASON_CODES:
                raise BookEntityClaimContractError(
                    "pending claim revision lacks a closed reason"
                )
        elif reason is not None:
            raise BookEntityClaimContractError(
                "non-pending claim revision carries a pending reason"
            )
        revision_by_id[revision_id] = row
    active_keys: set[tuple[str, str]] = set()
    for row in ledger["active_claim_projection"]:
        if not isinstance(row, Mapping):
            raise BookEntityClaimContractError("active claim projection must be an object")
        _exact_keys(
            row,
            {
                "prior_card_id",
                "disputed_field",
                "projected_value",
                "effective_from_block_id",
                "revision_ids",
                "status",
            },
            "active claim projection",
        )
        if row.get("status") != "active":
            raise BookEntityClaimContractError("active claim projection is not active")
        key = (
            _required_string(row.get("prior_card_id"), "active prior_card_id"),
            _required_string(row.get("disputed_field"), "active disputed_field"),
        )
        if key in active_keys:
            raise BookEntityClaimContractError("duplicate active claim projection")
        active_keys.add(key)
        revision_ids = _string_list(row.get("revision_ids"), "active revision_ids")
        if not set(revision_ids).issubset(revision_by_id):
            raise BookEntityClaimContractError("active projection cites foreign revision")
    pending_keys: set[tuple[str, str]] = set()
    for row in ledger["pending_claims"]:
        if not isinstance(row, Mapping):
            raise BookEntityClaimContractError("pending claim must be an object")
        _exact_keys(
            row,
            {
                "prior_card_id",
                "disputed_field",
                "revision_ids",
                "status",
                "pending_reason_codes",
                "evidence_manifest_hashes",
                "hearing_count",
                "automatic_hearing_limit",
                "same_evidence_reopen_forbidden",
                "next_review_trigger",
            },
            "pending claim",
        )
        key = (
            _required_string(row.get("prior_card_id"), "pending prior_card_id"),
            _required_string(row.get("disputed_field"), "pending disputed_field"),
        )
        if key in pending_keys or key in active_keys:
            raise BookEntityClaimContractError("claim field has conflicting ledger states")
        pending_keys.add(key)
        if row.get("status") != "pending":
            raise BookEntityClaimContractError("pending claim state is not pending")
        revision_ids = _string_list(row.get("revision_ids"), "pending revision_ids")
        if not set(revision_ids).issubset(revision_by_id):
            raise BookEntityClaimContractError("pending claim cites foreign revision")
        reasons = _string_list(
            row.get("pending_reason_codes"),
            "pending_reason_codes",
            allow_empty=True,
        )
        if not set(reasons).issubset(PENDING_REASON_CODES):
            raise BookEntityClaimContractError("pending claim has a foreign reason")
        evidence_hashes = _string_list(
            row.get("evidence_manifest_hashes"), "evidence_manifest_hashes"
        )
        for evidence_hash in evidence_hashes:
            _hash_string(evidence_hash, "pending evidence manifest hash")
        hearing_count = row.get("hearing_count")
        if not isinstance(hearing_count, int) or hearing_count < 0:
            raise BookEntityClaimContractError("pending hearing_count is invalid")
        if row.get("automatic_hearing_limit") != MAX_AUTOMATIC_HEARINGS:
            raise BookEntityClaimContractError("pending hearing limit drifted")
        if row.get("same_evidence_reopen_forbidden") is not True:
            raise BookEntityClaimContractError("same-evidence reopening is not forbidden")
        if row.get("next_review_trigger") not in {
            "identity_resolution",
            "book_end_or_human",
            "expanded_or_new_evidence",
            "new_evidence_or_book_end",
        }:
            raise BookEntityClaimContractError("pending next-review trigger is invalid")
    identity_referral_ids: set[str] = set()
    for row in ledger["identity_referrals"]:
        if not isinstance(row, Mapping):
            raise BookEntityClaimContractError("identity referral must be an object")
        _exact_keys(
            row,
            {
                "ticket_id",
                "prior_card_id",
                "issue_code",
                "disputed_field",
                "evidence_source_block_ids",
                "route",
            },
            "identity referral",
        )
        ticket_id = _required_string(row.get("ticket_id"), "identity ticket_id")
        if ticket_id in identity_referral_ids:
            raise BookEntityClaimContractError("duplicate identity referral")
        identity_referral_ids.add(ticket_id)
        _required_string(row.get("prior_card_id"), "identity prior_card_id")
        if row.get("issue_code") not in IDENTITY_ISSUES:
            raise BookEntityClaimContractError("identity referral has a foreign issue")
        _required_string(row.get("disputed_field"), "identity disputed_field")
        _string_list(
            row.get("evidence_source_block_ids"),
            "identity evidence_source_block_ids",
        )
        if row.get("route") != "identity_auditor":
            raise BookEntityClaimContractError("identity referral has an unsafe route")
    uncertainty_ids: set[str] = set()
    for row in ledger["pending_identity_reviews"]:
        if not isinstance(row, Mapping):
            raise BookEntityClaimContractError(
                "pending identity review must be an object"
            )
        _exact_keys(
            row,
            {
                "uncertainty_id",
                "prior_card_id",
                "status",
                "authority_effect",
                "current_source_block_ids",
                "reason",
                "evidence_state",
            },
            "pending identity review",
        )
        uncertainty_id = _required_string(row.get("uncertainty_id"), "uncertainty_id")
        if uncertainty_id in uncertainty_ids:
            raise BookEntityClaimContractError("duplicate pending uncertainty id")
        uncertainty_ids.add(uncertainty_id)
        _required_string(row.get("prior_card_id"), "pending prior_card_id")
        if row.get("status") != "pending":
            raise BookEntityClaimContractError("identity uncertainty is not pending")
        if row.get("authority_effect") != "candidate_only":
            raise BookEntityClaimContractError("identity uncertainty has unsafe authority")
        _string_list(
            row.get("current_source_block_ids"),
            "pending current_source_block_ids",
        )
        _required_string(row.get("reason"), "pending identity reason")
        if row.get("evidence_state") not in {"ready", "insufficient"}:
            raise BookEntityClaimContractError("identity uncertainty has invalid evidence state")
    return _clone(dict(ledger))


def classify_pending_claim_reopen_v1(
    *,
    pending_claim: Mapping[str, Any],
    evidence_manifest_hash: str,
    trigger: str,
) -> dict[str, Any]:
    """Classify a reopen request without granting semantic authority."""

    if not isinstance(pending_claim, Mapping) or pending_claim.get("status") != "pending":
        raise BookEntityClaimContractError("reopen target is not a pending claim")
    observed_evidence = _hash_string(
        evidence_manifest_hash, "reopen evidence manifest hash"
    )
    prior_evidence = set(
        _string_list(
            pending_claim.get("evidence_manifest_hashes"),
            "pending evidence_manifest_hashes",
        )
    )
    hearing_count = pending_claim.get("hearing_count")
    if not isinstance(hearing_count, int) or hearing_count < 0:
        raise BookEntityClaimContractError("pending hearing_count is invalid")
    if trigger not in {"new_evidence", "expanded_evidence", "book_end", "human"}:
        raise BookEntityClaimContractError("foreign pending reopen trigger")
    if trigger in {"new_evidence", "expanded_evidence"}:
        if observed_evidence in prior_evidence:
            allowed = False
            route = "blocked_same_evidence"
        elif hearing_count >= MAX_AUTOMATIC_HEARINGS:
            allowed = False
            route = "defer_book_end_or_human"
        else:
            allowed = True
            route = "automatic_hearing"
    elif trigger == "book_end":
        allowed = True
        route = "book_end_hearing"
    else:
        allowed = True
        route = "human_hearing"
    body = {
        "schema_version": "pending_claim_reopen_classification_v1",
        "prior_card_id": _required_string(
            pending_claim.get("prior_card_id"), "pending prior_card_id"
        ),
        "disputed_field": _required_string(
            pending_claim.get("disputed_field"), "pending disputed_field"
        ),
        "evidence_manifest_hash": observed_evidence,
        "trigger": trigger,
        "allowed": allowed,
        "route": route,
        "hearing_count_before": hearing_count,
        "automatic_hearing_limit": MAX_AUTOMATIC_HEARINGS,
    }
    return {**body, "classification_hash": canonical_hash(body)}


def build_prior_claim_projection_v1(
    *,
    prior_cards: Sequence[Mapping[str, Any]],
    ledger: Mapping[str, Any],
) -> dict[str, Any]:
    cards = validate_prior_cards(prior_cards)
    verified_ledger = verify_prior_claim_revision_ledger_v1(ledger)
    card_by_id = {row["prior_card_id"]: row for row in cards}
    states_by_card: dict[str, list[dict[str, Any]]] = defaultdict(list)
    disputed_by_card: dict[str, list[dict[str, Any]]] = defaultdict(list)
    identity_pending_from_ledger: dict[str, list[dict[str, Any]]] = defaultdict(list)
    effective_by_card: dict[str, dict[str, Any]] = {
        card_id: {
            "referent_kind": card["referent_kind"],
            "referential_gender": card["referential_gender"],
            "identity_summary": card["identity_summary"],
        }
        for card_id, card in card_by_id.items()
    }
    for row in verified_ledger["active_claim_projection"]:
        card_id = row["prior_card_id"]
        if card_id not in card_by_id:
            raise BookEntityClaimContractError("ledger projects a foreign prior card")
        effective_by_card[card_id][row["disputed_field"]] = _clone(
            row["projected_value"]
        )
        states_by_card[card_id].append(_clone(row))
    for row in verified_ledger["pending_claims"]:
        card_id = row["prior_card_id"]
        if card_id not in card_by_id:
            raise BookEntityClaimContractError("ledger pends a foreign prior card")
        disputed_field = row["disputed_field"]
        if disputed_field not in effective_by_card[card_id]:
            if disputed_field not in {
                "identity_membership",
                "alias_target",
                "alias_scope",
            }:
                raise BookEntityClaimContractError("ledger pends a foreign stable field")
            states_by_card[card_id].append(_clone(row))
            identity_pending_from_ledger[card_id].append(_clone(row))
            continue
        historical_value = _clone(effective_by_card[card_id][disputed_field])
        effective_by_card[card_id][disputed_field] = None
        states_by_card[card_id].append(_clone(row))
        disputed_by_card[card_id].append(
            {
                "disputed_field": disputed_field,
                "historical_value": historical_value,
                "status": "pending",
                "pending_reason_codes": _clone(row["pending_reason_codes"]),
                "evidence_manifest_hashes": _clone(
                    row["evidence_manifest_hashes"]
                ),
                "hearing_count": row["hearing_count"],
                "automatic_hearing_limit": row["automatic_hearing_limit"],
                "same_evidence_reopen_forbidden": row[
                    "same_evidence_reopen_forbidden"
                ],
                "next_review_trigger": row["next_review_trigger"],
                "revision_ids": _clone(row["revision_ids"]),
            }
        )
    uncertainty_by_card: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in verified_ledger["pending_identity_reviews"]:
        card_id = row["prior_card_id"]
        if card_id not in card_by_id:
            raise BookEntityClaimContractError(
                "identity review pends a foreign prior card"
            )
        normalized = {
            "state_kind": "identity_uncertainty",
            "disputed_field": "identity_membership",
            **_clone(row),
        }
        states_by_card[card_id].append(normalized)
        first_identity_uncertainty = not uncertainty_by_card[card_id]
        uncertainty_by_card[card_id].append(normalized)
        if first_identity_uncertainty:
            disputed_by_card[card_id].append(
                {
                    "disputed_field": "identity_membership",
                    "historical_value": None,
                    "status": "pending",
                    "pending_reason_codes": ["conflicting_evidence"],
                    "evidence_manifest_hashes": [],
                    "hearing_count": 0,
                    "automatic_hearing_limit": MAX_AUTOMATIC_HEARINGS,
                    "same_evidence_reopen_forbidden": True,
                    "next_review_trigger": "identity_resolution",
                    "revision_ids": [],
                    "uncertainty_id": row["uncertainty_id"],
                }
            )
    for card_id, rows in identity_pending_from_ledger.items():
        if uncertainty_by_card.get(card_id):
            continue
        uncertainty_by_card[card_id].append(
            {
                "state_kind": "identity_uncertainty",
                "disputed_field": "identity_membership",
                "status": "pending",
            }
        )
        disputed_by_card[card_id].append(
            {
                "disputed_field": "identity_membership",
                "historical_value": None,
                "status": "pending",
                "pending_reason_codes": sorted(
                    {
                        reason
                        for row in rows
                        for reason in row["pending_reason_codes"]
                    }
                ),
                "evidence_manifest_hashes": sorted(
                    {
                        evidence_hash
                        for row in rows
                        for evidence_hash in row["evidence_manifest_hashes"]
                    }
                ),
                "hearing_count": max(row["hearing_count"] for row in rows),
                "automatic_hearing_limit": MAX_AUTOMATIC_HEARINGS,
                "same_evidence_reopen_forbidden": True,
                "next_review_trigger": "identity_resolution",
                "revision_ids": sorted(
                    {
                        revision_id
                        for row in rows
                        for revision_id in row["revision_ids"]
                    }
                ),
            }
        )
    referrals_by_card: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in verified_ledger["identity_referrals"]:
        card_id = row["prior_card_id"]
        if card_id not in card_by_id:
            raise BookEntityClaimContractError(
                "identity referral targets a foreign prior card"
            )
        normalized = {
            "state_kind": "identity_referral",
            "disputed_field": "identity_membership",
            "source_disputed_field": row["disputed_field"],
            "ticket_id": row["ticket_id"],
            "issue_code": row["issue_code"],
            "status": "pending",
            "authority_effect": "candidate_only",
            "evidence_source_block_ids": _clone(
                row["evidence_source_block_ids"]
            ),
            "route": row["route"],
        }
        states_by_card[card_id].append(normalized)
        uncertainty_by_card[card_id].append(normalized)
        referrals_by_card[card_id].append(_clone(row))
    for card_id, rows in referrals_by_card.items():
        evidence_hashes = sorted(canonical_hash(row) for row in rows)
        existing = next(
            (
                row
                for row in disputed_by_card[card_id]
                if row.get("disputed_field") == "identity_membership"
            ),
            None,
        )
        if existing is None:
            uncertainty_body = {
                "prior_card_id": card_id,
                "ticket_ids": sorted(row["ticket_id"] for row in rows),
                "evidence_manifest_hashes": evidence_hashes,
            }
            disputed_by_card[card_id].append(
                {
                    "disputed_field": "identity_membership",
                    "historical_value": None,
                    "status": "pending",
                    "pending_reason_codes": ["conflicting_evidence"],
                    "evidence_manifest_hashes": evidence_hashes,
                    "hearing_count": 0,
                    "automatic_hearing_limit": MAX_AUTOMATIC_HEARINGS,
                    "same_evidence_reopen_forbidden": True,
                    "next_review_trigger": "identity_resolution",
                    "revision_ids": [],
                    "uncertainty_id": "bunc1_"
                    + canonical_hash(uncertainty_body)[:20],
                }
            )
        else:
            existing["pending_reason_codes"] = sorted(
                set(existing.get("pending_reason_codes") or []).union(
                    {"conflicting_evidence"}
                )
            )
            existing["evidence_manifest_hashes"] = sorted(
                set(existing.get("evidence_manifest_hashes") or []).union(
                    evidence_hashes
                )
            )
            if "uncertainty_id" not in existing:
                existing["uncertainty_id"] = "bunc1_" + canonical_hash(
                    {
                        "prior_card_id": card_id,
                        "ticket_ids": sorted(row["ticket_id"] for row in rows),
                        "evidence_manifest_hashes": evidence_hashes,
                    }
                )[:20]
    projected_cards = [
        {
            "prior_card_id": card_id,
            "source_prior_card_hash": canonical_hash(card_by_id[card_id]),
            "original_prior_card": _clone(card_by_id[card_id]),
            "effective_claims": effective_by_card[card_id],
            "disputed_claims": sorted(
                disputed_by_card[card_id],
                key=lambda row: (
                    row["disputed_field"],
                    row.get("uncertainty_id", ""),
                ),
            ),
            "authority_state": (
                "candidate_only"
                if uncertainty_by_card.get(card_id)
                else (
                    "partial_pending"
                    if disputed_by_card.get(card_id)
                    else "prior_authority_preserved"
                )
            ),
            "claim_states": sorted(
                states_by_card[card_id],
                key=lambda row: (
                    row["disputed_field"],
                    row.get("uncertainty_id", ""),
                    row.get("status", ""),
                ),
            ),
        }
        for card_id in sorted(card_by_id)
    ]
    body = {
        "schema_version": CLAIM_PROJECTION_SCHEMA_VERSION,
        "validator_version": CLAIM_VALIDATOR_VERSION,
        "state_lineage_id": verified_ledger["state_lineage_id"],
        "registry_generation_hash": verified_ledger["registry_generation_hash"],
        "claim_ledger_hash": verified_ledger["claim_ledger_hash"],
        "projected_prior_cards": projected_cards,
    }
    projection = {**body, "projection_hash": canonical_hash(body)}
    return verify_prior_claim_projection_v1(projection)


def verify_prior_claim_projection_v1(
    projection: Mapping[str, Any],
) -> dict[str, Any]:
    if projection.get("schema_version") != CLAIM_PROJECTION_SCHEMA_VERSION:
        raise BookEntityClaimContractError("foreign prior-claim projection schema")
    if projection.get("validator_version") != CLAIM_VALIDATOR_VERSION:
        raise BookEntityClaimContractError("prior-claim projection validator mismatch")
    body = dict(projection)
    observed = _hash_string(body.pop("projection_hash", None), "projection hash")
    if canonical_hash(body) != observed:
        raise BookEntityClaimContractError("prior-claim projection hash mismatch")
    rows = projection.get("projected_prior_cards")
    if not isinstance(rows, list):
        raise BookEntityClaimContractError("projected prior cards must be a list")
    seen: set[str] = set()
    for row in rows:
        if not isinstance(row, Mapping):
            raise BookEntityClaimContractError("projected prior card must be an object")
        _exact_keys(
            row,
            {
                "prior_card_id",
                "source_prior_card_hash",
                "original_prior_card",
                "effective_claims",
                "disputed_claims",
                "authority_state",
                "claim_states",
            },
            "projected prior card",
        )
        card_id = _required_string(row.get("prior_card_id"), "prior_card_id")
        if card_id in seen:
            raise BookEntityClaimContractError("duplicate projected prior card")
        seen.add(card_id)
        original = row.get("original_prior_card")
        if not isinstance(original, Mapping):
            raise BookEntityClaimContractError("original prior card is malformed")
        if row.get("source_prior_card_hash") != canonical_hash(original):
            raise BookEntityClaimContractError("source prior card hash mismatch")
        effective = row.get("effective_claims")
        if not isinstance(effective, Mapping) or set(effective) != {
            "referent_kind",
            "referential_gender",
            "identity_summary",
        }:
            raise BookEntityClaimContractError("effective claims are malformed")
        disputes = row.get("disputed_claims")
        if not isinstance(disputes, list):
            raise BookEntityClaimContractError("disputed claims must be a list")
        dispute_fields: list[str] = []
        for dispute in disputes:
            if not isinstance(dispute, Mapping):
                raise BookEntityClaimContractError("disputed claim must be an object")
            field = _required_string(
                dispute.get("disputed_field"), "disputed claim field"
            )
            if field not in {
                "referent_kind",
                "referential_gender",
                "identity_summary",
                "identity_membership",
            }:
                raise BookEntityClaimContractError("disputed claim field is foreign")
            dispute_fields.append(field)
            if dispute.get("status") != "pending":
                raise BookEntityClaimContractError("disputed claim is not pending")
            if field != "identity_membership" and effective[field] is not None:
                raise BookEntityClaimContractError(
                    "pending field remains authoritative in effective claims"
                )
        if len(dispute_fields) != len(set(dispute_fields)):
            raise BookEntityClaimContractError("duplicate disputed claim field")
        authority_state = row.get("authority_state")
        expected_authority = (
            "candidate_only"
            if "identity_membership" in dispute_fields
            else ("partial_pending" if dispute_fields else "prior_authority_preserved")
        )
        if authority_state != expected_authority:
            raise BookEntityClaimContractError("projection authority state is inconsistent")
        if not isinstance(row.get("claim_states"), list):
            raise BookEntityClaimContractError("claim_states must be a list")
    return _clone(dict(projection))


def dry_render_prior_claim_requests_v1(
    *,
    index: Mapping[str, Any],
    document: Mapping[str, Any],
    design_doc: Path,
) -> dict[str, Any]:
    verified = verify_prior_claim_ticket_index_v1(index, document=document)
    rows: list[dict[str, Any]] = []
    for component in verified["claim_components"]:
        if component["overflow"]:
            rows.append(
                {
                    "component_id": component["component_id"],
                    "rendered": False,
                    "overflow_reasons": component["overflow_reasons"],
                    "ticket_count": len(component["ticket_ids"]),
                    "chapter_count": len(component["chapter_ids"]),
                    "source_block_count": len(component["source_blocks"]),
                    "message_utf8_bytes": 0,
                    "response_schema_utf8_bytes": 0,
                    "total_contract_utf8_bytes": 0,
                    "estimated_tokens": 0,
                }
            )
            continue
        rendered = render_prior_claim_request_v1(
            index=verified,
            component_id=component["component_id"],
            document=document,
            design_doc=design_doc,
        )
        message_bytes = sum(
            len(message["content"].encode("utf-8")) for message in rendered.messages
        )
        schema_bytes = len(canonical_json(rendered.response_schema).encode("utf-8"))
        byte_count = message_bytes + schema_bytes
        rows.append(
            {
                "component_id": component["component_id"],
                "rendered": True,
                "overflow_reasons": [],
                "ticket_count": len(component["ticket_ids"]),
                "chapter_count": len(component["chapter_ids"]),
                "source_block_count": len(component["source_blocks"]),
                "message_utf8_bytes": message_bytes,
                "response_schema_utf8_bytes": schema_bytes,
                "total_contract_utf8_bytes": byte_count,
                "estimated_tokens": math.ceil(byte_count / 4),
                "request_fingerprint": rendered.request_fingerprint,
            }
        )
    body = {
        "schema_version": "prior_claim_dry_render_report_v1",
        "ticket_index_hash": verified["ticket_index_hash"],
        "components": rows,
        "rendered_component_count": sum(row["rendered"] for row in rows),
        "overflow_component_count": sum(not row["rendered"] for row in rows),
        "preflight_pending_ticket_count": len(
            verified["preflight_pending_ticket_ids"]
        ),
        "identity_referral_count": len(verified["identity_referrals"]),
        "pending_identity_review_count": len(verified["uncertainty_rows"]),
        "estimated_total_tokens": sum(row["estimated_tokens"] for row in rows),
        "token_estimator": "ceil((message_utf8_bytes+response_schema_utf8_bytes)/4)",
    }
    return {**body, "report_hash": canonical_hash(body)}


__all__ = [
    "CLAIM_ACTIONS",
    "CLAIM_DECISION_SCHEMA_VERSION",
    "CLAIM_INDEX_SCHEMA_VERSION",
    "CLAIM_LEDGER_SCHEMA_VERSION",
    "CLAIM_PROJECTION_SCHEMA_VERSION",
    "CLAIM_VALIDATOR_VERSION",
    "BookEntityClaimAuditorError",
    "BookEntityClaimContractError",
    "RenderedPriorClaimRequestV1",
    "build_prior_claim_projection_v1",
    "build_prior_claim_revision_ledger_v1",
    "build_prior_claim_ticket_index_v1",
    "classify_pending_claim_reopen_v1",
    "dry_render_prior_claim_requests_v1",
    "prior_claim_response_schema_v1",
    "render_prior_claim_request_v1",
    "validate_prior_claim_response_v1",
    "verify_prior_claim_decision_v1",
    "verify_prior_claim_projection_v1",
    "verify_prior_claim_revision_ledger_v1",
    "verify_prior_claim_ticket_index_v1",
]
