"""Bounded mechanical identity-suspicion leads for the literary chapter cycle.

This module never decides co-reference. It turns a closed set of mechanically
observable cross-chapter patterns into content-addressed review leads. A
singleton recurrence remains visible as a waiting lead. It avoids downscoping
only when the current Builder artifact explicitly confirms same-referent
continuity; otherwise the original fail-closed behavior remains in force.
"""

from __future__ import annotations

from copy import deepcopy
import re
from typing import Any, Mapping, Sequence
import unicodedata

from pipeline.literary.chapter_prefix_prior_v1 import (
    CANDIDATE_ONLY_SCOPE,
    verify_chapter_prefix_prior_bundle_v1,
)
from pipeline.literary.b0_entity_prior_challenge_experiment import (
    EXPERIMENT_SCHEMA_VERSION as PRIOR_CHALLENGE_SCHEMA_VERSION,
)
from pipeline.literary.checkpoint import canonical_hash


LEAD_SCHEMA_VERSION = "semantic_candidate_lead_v1"
LEAD_INDEX_SCHEMA_VERSION = "semantic_candidate_lead_index_v1"
LEAD_INDEX_VALIDATOR_VERSION = "semantic_candidate_lead_validator_v1"
OCCURRENCE_BRIDGE_SCHEMA_VERSION = "semantic_identity_occurrence_bridge_v1"
OCCURRENCE_BRIDGE_VALIDATOR_VERSION = (
    "semantic_identity_occurrence_bridge_validator_v1"
)
CREATED_AT_STAGE = "t_lite_mechanical_lead_builder"
DEFAULT_MAX_LEADS_PER_CHAPTER = 8
DEFAULT_MAX_IDENTITY_COMPONENTS_PER_CHAPTER = 4
DEFAULT_MAX_OCCURRENCE_CARDS_PER_LEAD = 8

TRIGGER_KINDS = frozenset(
    {
        "first_recurrence_weak_card",
        "person_terminal_token_overlap",
        "surface_core_overlap",
        "exact_surface_collision",
    }
)
LIFECYCLE_STATES = frozenset(
    {
        "queued_pairable",
        "waiting_for_pair",
        "duplicate_suppressed",
        "chapter_cap_deferred",
        "book_end_pending",
        "closed_by_identity_ledger",
    }
)
OCCURRENCE_BRIDGE_STATES = frozenset(
    {
        "paired_for_identity_review",
        "unlocatable_surface",
        "component_cap_deferred",
        "occurrence_cap_deferred",
    }
)


class SemanticCandidateLeadError(ValueError):
    """Raised when a T-lite lead artifact is malformed or stale."""


def compatible_prior_card_ids_from_challenge_v1(
    challenge_artifact: Mapping[str, Any],
) -> list[str]:
    """Read explicit same-referent confirmations from a sealed B1 artifact."""

    if challenge_artifact.get("schema_version") != PRIOR_CHALLENGE_SCHEMA_VERSION:
        raise SemanticCandidateLeadError("foreign prior-challenge schema")
    body = _clone(dict(challenge_artifact))
    observed_hash = _required_string(
        body.pop("prior_challenge_artifact_hash", None),
        "prior_challenge_artifact_hash",
    )
    if canonical_hash(body) != observed_hash:
        raise SemanticCandidateLeadError("prior-challenge artifact hash mismatch")
    dispositions = challenge_artifact.get("prior_card_dispositions")
    if not isinstance(dispositions, list):
        raise SemanticCandidateLeadError("prior-challenge dispositions are malformed")
    confirmed: list[str] = []
    seen: set[str] = set()
    for row in dispositions:
        if not isinstance(row, Mapping):
            raise SemanticCandidateLeadError("prior-challenge disposition is malformed")
        card_id = _required_string(row.get("prior_card_id"), "prior_card_id")
        if card_id in seen:
            raise SemanticCandidateLeadError("prior-challenge disposition repeats a card")
        seen.add(card_id)
        if (
            row.get("verdict") == "compatible"
            and row.get("referent_continuity") == "same_referent"
        ):
            confirmed.append(card_id)
    return sorted(confirmed)


def _clone(value: Any) -> Any:
    return deepcopy(value)


def _required_string(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise SemanticCandidateLeadError(f"{label} must be a non-empty string")
    return value


def _string_list(
    value: Any,
    label: str,
    *,
    allow_empty: bool = False,
) -> list[str]:
    if not isinstance(value, list) or (not value and not allow_empty):
        qualifier = "possibly empty" if allow_empty else "non-empty"
        raise SemanticCandidateLeadError(f"{label} must be a {qualifier} list")
    rows = [_required_string(item, label) for item in value]
    if rows != sorted(set(rows)):
        raise SemanticCandidateLeadError(f"{label} must be sorted and unique")
    return rows


def _hash_list(value: Any, label: str) -> list[str]:
    rows = _string_list(value, label)
    for row in rows:
        if len(row) != 64 or any(char not in "0123456789abcdef" for char in row):
            raise SemanticCandidateLeadError(f"{label} contains a non-sha256 value")
    return rows


def _surface_key(value: Any) -> str:
    normalized = unicodedata.normalize("NFKC", str(value or "")).casefold()
    return " ".join(normalized.split())


def _surface_tokens(value: Any) -> tuple[str, ...]:
    normalized = _surface_key(value)
    return tuple(re.findall(r"[\w]+(?:['\u2019-][\w]+)*", normalized, flags=re.UNICODE))


def _strict_contiguous_core(left: str, right: str) -> str | None:
    left_tokens = _surface_tokens(left)
    right_tokens = _surface_tokens(right)
    if not left_tokens or not right_tokens or left_tokens == right_tokens:
        return None
    shorter, longer = sorted((left_tokens, right_tokens), key=lambda row: (len(row), row))
    if len(shorter) >= len(longer):
        return None
    for offset in range(0, len(longer) - len(shorter) + 1):
        if longer[offset : offset + len(shorter)] == shorter:
            return " ".join(shorter)
    return None


def _block_text(block: Mapping[str, Any]) -> str:
    return str(block.get("clean_text") or block.get("source_text") or "")


def _document_catalog(document: Mapping[str, Any]) -> dict[str, Any]:
    chapters = document.get("chapters")
    if not isinstance(chapters, list) or not chapters:
        raise SemanticCandidateLeadError("document has no chapters")
    chapter_order: list[str] = []
    chapter_blocks: dict[str, list[Mapping[str, Any]]] = {}
    block_chapter: dict[str, str] = {}
    block_by_id: dict[str, Mapping[str, Any]] = {}
    for raw_chapter in chapters:
        if not isinstance(raw_chapter, Mapping):
            raise SemanticCandidateLeadError("document chapter must be an object")
        chapter_id = _required_string(raw_chapter.get("chapter_id"), "chapter_id")
        if chapter_id in chapter_blocks:
            raise SemanticCandidateLeadError("document repeats a chapter id")
        raw_blocks = raw_chapter.get("blocks")
        if not isinstance(raw_blocks, list) or not raw_blocks:
            raise SemanticCandidateLeadError("document chapter has no blocks")
        blocks = sorted(
            raw_blocks,
            key=lambda row: (
                int(row.get("order_index") or 0),
                str(row.get("block_id") or ""),
            ),
        )
        for block in blocks:
            if not isinstance(block, Mapping):
                raise SemanticCandidateLeadError("document block must be an object")
            block_id = _required_string(block.get("block_id"), "block_id")
            if block_id in block_by_id:
                raise SemanticCandidateLeadError("document repeats a block id")
            block_by_id[block_id] = block
            block_chapter[block_id] = chapter_id
        chapter_order.append(chapter_id)
        chapter_blocks[chapter_id] = blocks
    return {
        "chapter_order": chapter_order,
        "chapter_blocks": chapter_blocks,
        "block_chapter": block_chapter,
        "block_by_id": block_by_id,
    }


def _card_chapters(card: Mapping[str, Any]) -> list[str]:
    return sorted(
        {
            str(row.get("chapter_id"))
            for row in card.get("provenance_refs") or []
            if isinstance(row, Mapping) and str(row.get("chapter_id") or "").strip()
        }
    )


def _card_block_ids(card: Mapping[str, Any]) -> list[str]:
    return sorted(
        {
            str(row.get("block_id"))
            for row in card.get("provenance_refs") or []
            if isinstance(row, Mapping) and str(row.get("block_id") or "").strip()
        }
    )


def _card_kind(card: Mapping[str, Any]) -> str | None:
    claims = card.get("effective_claims")
    if not isinstance(claims, Mapping):
        return None
    value = claims.get("referent_kind")
    return str(value) if isinstance(value, str) and value else None


def _card_gender(card: Mapping[str, Any]) -> str | None:
    claims = card.get("effective_claims")
    if not isinstance(claims, Mapping):
        return None
    value = claims.get("referential_gender")
    return str(value) if isinstance(value, str) and value else None


def _stable_surface_keys(card: Mapping[str, Any]) -> dict[str, str]:
    result: dict[str, str] = {}
    for surface in card.get("stable_surfaces") or []:
        key = _surface_key(surface)
        if key:
            result.setdefault(key, str(surface))
    return result


def _surface_in_block(surface: str, block: Mapping[str, Any]) -> bool:
    needle = _surface_tokens(surface)
    haystack = _surface_tokens(_block_text(block))
    if not needle or len(needle) > len(haystack):
        return False
    return any(
        haystack[offset : offset + len(needle)] == needle
        for offset in range(0, len(haystack) - len(needle) + 1)
    )


def _exact_surface_from_block(surface_key: str, block: Mapping[str, Any]) -> str | None:
    """Return source bytes for one normalized token match, without inferring identity."""

    target = _surface_tokens(surface_key)
    if not target:
        return None
    text = _block_text(block)
    tokens = list(
        re.finditer(
            r"[\w]+(?:['\u2019-][\w]+)*",
            text,
            flags=re.UNICODE,
        )
    )
    normalized = [_surface_key(match.group(0)) for match in tokens]
    for offset in range(0, len(tokens) - len(target) + 1):
        if tuple(normalized[offset : offset + len(target)]) != target:
            continue
        exact = text[tokens[offset].start() : tokens[offset + len(target) - 1].end()]
        if _surface_key(exact) == surface_key:
            return exact
    return None


def _block_hash(block: Mapping[str, Any]) -> str:
    return canonical_hash(
        {
            "block_id": str(block.get("block_id") or ""),
            "order_index": int(block.get("order_index") or 0),
            "block_type": str(block.get("block_type") or ""),
            "text": _block_text(block),
        }
    )


def _manifest_states(prefix: Mapping[str, Any]) -> dict[str, str]:
    return {
        str(row.get("prior_card_id")): str(row.get("local_state") or "")
        for row in prefix.get("source_entity_manifest") or []
        if isinstance(row, Mapping) and str(row.get("prior_card_id") or "").strip()
    }


def _is_stable_candidate(card_id: str, manifest_states: Mapping[str, str]) -> bool:
    # Unresolved rows intentionally carry descriptors rather than stable names.
    # Keeping them out is a structural check, not a lexical word list.
    return manifest_states.get(card_id) != "unresolved"


def _lead_evidence_body(
    *,
    trigger_kind: str,
    surface_keys: Sequence[str],
    prior_card_ids: Sequence[str],
    current_candidate_card_ids: Sequence[str],
    chapter_ids: Sequence[str],
    source_block_ids: Sequence[str],
    source_artifact_hashes: Sequence[str],
) -> dict[str, Any]:
    return {
        "trigger_kind": trigger_kind,
        "surface_keys": sorted(set(surface_keys)),
        "prior_card_ids": sorted(set(prior_card_ids)),
        "current_candidate_card_ids": sorted(set(current_candidate_card_ids)),
        "chapter_ids": sorted(set(chapter_ids)),
        "source_block_ids": sorted(set(source_block_ids)),
        "source_artifact_hashes": sorted(set(source_artifact_hashes)),
    }


def _lead_identity_body(lead: Mapping[str, Any]) -> dict[str, Any]:
    return {
        key: _clone(value)
        for key, value in lead.items()
        if key
        in {
            "schema_version",
            "state_lineage_id",
            "trigger_kind",
            "surface_keys",
            "prior_card_ids",
            "current_candidate_card_ids",
            "chapter_ids",
            "source_block_ids",
            "source_artifact_hashes",
            "evidence_manifest_hash",
            "created_at_stage",
        }
    }


def _lead(
    *,
    lineage_id: str,
    trigger_kind: str,
    surface_keys: Sequence[str],
    prior_card_ids: Sequence[str],
    current_candidate_card_ids: Sequence[str],
    chapter_ids: Sequence[str],
    source_block_ids: Sequence[str],
    source_artifact_hashes: Sequence[str],
) -> dict[str, Any]:
    evidence_body = _lead_evidence_body(
        trigger_kind=trigger_kind,
        surface_keys=surface_keys,
        prior_card_ids=prior_card_ids,
        current_candidate_card_ids=current_candidate_card_ids,
        chapter_ids=chapter_ids,
        source_block_ids=source_block_ids,
        source_artifact_hashes=source_artifact_hashes,
    )
    body = {
        "schema_version": LEAD_SCHEMA_VERSION,
        "state_lineage_id": lineage_id,
        **evidence_body,
        "evidence_manifest_hash": canonical_hash(evidence_body),
        "lifecycle_state": "waiting_for_pair",
        "authority_effect": "none",
        "created_at_stage": CREATED_AT_STAGE,
    }
    return {
        "lead_id": "semlead1_" + canonical_hash(_lead_identity_body(body))[:20],
        **body,
    }


def _watch_item(
    *,
    lineage_id: str,
    prior_card_ids: Sequence[str],
    current_card_ids: Sequence[str],
    chapter_ids: Sequence[str],
) -> dict[str, Any]:
    body = {
        "watch_kind": "nonlexical_unnamed_identity_not_evaluated",
        "state_lineage_id": lineage_id,
        "prior_card_ids": sorted(set(prior_card_ids)),
        "current_candidate_card_ids": sorted(set(current_card_ids)),
        "chapter_ids": sorted(set(chapter_ids)),
        "authority_effect": "none",
        "resolution_state": "unsupported_by_t_lite",
    }
    return {"watch_id": "semwatch1_" + canonical_hash(body)[:20], **body}


def _queued_component_count(leads: Sequence[Mapping[str, Any]]) -> int:
    parent: dict[str, str] = {}

    def find(card_id: str) -> str:
        parent.setdefault(card_id, card_id)
        if parent[card_id] != card_id:
            parent[card_id] = find(parent[card_id])
        return parent[card_id]

    def union(left: str, right: str) -> None:
        left_root = find(left)
        right_root = find(right)
        if left_root == right_root:
            return
        smaller, larger = sorted((left_root, right_root))
        parent[larger] = smaller

    for row in leads:
        if row.get("lifecycle_state") != "queued_pairable":
            continue
        owners = sorted(
            set(
                [
                    *(row.get("prior_card_ids") or []),
                    *(row.get("current_candidate_card_ids") or []),
                ]
            )
        )
        for owner in owners:
            find(owner)
        for owner in owners[1:]:
            union(owners[0], owner)
    return len({find(card_id) for card_id in parent})


def build_semantic_candidate_lead_index_v1(
    *,
    document: Mapping[str, Any],
    prefix_bundle: Mapping[str, Any],
    current_chapter_id: str,
    previous_lead_index: Mapping[str, Any] | None = None,
    max_leads_per_chapter: int = DEFAULT_MAX_LEADS_PER_CHAPTER,
    max_identity_components_per_chapter: int = DEFAULT_MAX_IDENTITY_COMPONENTS_PER_CHAPTER,
) -> dict[str, Any]:
    """Build bounded T-lite leads from cards and source bytes already on disk."""

    if not isinstance(max_leads_per_chapter, int) or not 0 <= max_leads_per_chapter <= 64:
        raise SemanticCandidateLeadError("max lead count is outside the closed bound")
    if (
        not isinstance(max_identity_components_per_chapter, int)
        or not 0 <= max_identity_components_per_chapter <= 32
    ):
        raise SemanticCandidateLeadError("max component count is outside the closed bound")
    prefix = verify_chapter_prefix_prior_bundle_v1(prefix_bundle, document=document)
    chapter_id = _required_string(current_chapter_id, "current_chapter_id")
    if prefix.get("coverage_through_chapter_id") != chapter_id:
        raise SemanticCandidateLeadError("T-lite chapter is stale against prefix coverage")
    catalog = _document_catalog(document)
    if chapter_id not in catalog["chapter_blocks"]:
        raise SemanticCandidateLeadError("T-lite chapter is foreign to the document")
    chapter_position = catalog["chapter_order"].index(chapter_id)
    if chapter_position == 0:
        raise SemanticCandidateLeadError("T-lite requires at least one prior chapter")

    previous_ids: set[str] = set()
    previous_first_recurrence_cards: set[str] = set()
    if previous_lead_index is not None:
        previous = verify_semantic_candidate_lead_index_v1(previous_lead_index)
        if previous["state_lineage_id"] != prefix["state_lineage_id"]:
            raise SemanticCandidateLeadError("previous T-lite index crosses lineage")
        previous_ids = {row["lead_id"] for row in previous["leads"]}
        previous_first_recurrence_cards = {
            card_id
            for row in previous["leads"]
            if row["trigger_kind"] == "first_recurrence_weak_card"
            for card_id in row["prior_card_ids"]
        }

    cards = {
        row["prior_card_id"]: _clone(row)
        for row in [
            *(prefix.get("b0_context_cards") or []),
            *(prefix.get("candidate_only_context_cards") or []),
        ]
    }
    manifest_states = _manifest_states(prefix)
    prior_cards: dict[str, dict[str, Any]] = {}
    current_cards: dict[str, dict[str, Any]] = {}
    for card_id, card in cards.items():
        chapters = _card_chapters(card)
        if chapter_id in chapters:
            current_cards[card_id] = card
        elif any(
            source_chapter in catalog["chapter_order"]
            and catalog["chapter_order"].index(source_chapter) < chapter_position
            for source_chapter in chapters
        ):
            prior_cards[card_id] = card

    current_blocks = catalog["chapter_blocks"][chapter_id]
    candidates: list[dict[str, Any]] = []
    pair_covered: set[tuple[str, str]] = set()
    lexically_accounted_prior: set[str] = set()

    # Exact collisions take precedence over weaker lexical triggers.
    exact_groups: dict[tuple[str, str], dict[str, list[str]]] = {}
    for card_id, card in prior_cards.items():
        if not _is_stable_candidate(card_id, manifest_states):
            continue
        kind = _card_kind(card)
        if kind is None:
            continue
        for key in _stable_surface_keys(card):
            exact_groups.setdefault((key, kind), {"prior": [], "current": []})[
                "prior"
            ].append(card_id)
    for card_id, card in current_cards.items():
        if not _is_stable_candidate(card_id, manifest_states):
            continue
        kind = _card_kind(card)
        if kind is None:
            continue
        for key in _stable_surface_keys(card):
            exact_groups.setdefault((key, kind), {"prior": [], "current": []})[
                "current"
            ].append(card_id)
    for (key, _kind), groups in sorted(exact_groups.items()):
        prior_ids = sorted(set(groups["prior"]))
        current_ids = sorted(set(groups["current"]))
        if not prior_ids or not current_ids:
            continue
        for prior_id in prior_ids:
            for current_id in current_ids:
                pair_covered.add((prior_id, current_id))
                lexically_accounted_prior.add(prior_id)
        owner_ids = [*prior_ids, *current_ids]
        block_ids = sorted(
            {
                block_id
                for card_id in owner_ids
                for block_id in _card_block_ids(cards[card_id])
            }
        )
        artifact_hashes = sorted(
            {
                *[cards[card_id]["context_card_hash"] for card_id in owner_ids],
                *[_block_hash(catalog["block_by_id"][block_id]) for block_id in block_ids],
            }
        )
        candidates.append(
            _lead(
                lineage_id=prefix["state_lineage_id"],
                trigger_kind="exact_surface_collision",
                surface_keys=[key],
                prior_card_ids=prior_ids,
                current_candidate_card_ids=current_ids,
                chapter_ids=[
                    *[chapter for card_id in prior_ids for chapter in _card_chapters(cards[card_id])],
                    chapter_id,
                ],
                source_block_ids=block_ids,
                source_artifact_hashes=artifact_hashes,
            )
        )

    # Strict contiguous token cores cover title/article-qualified lexical variants
    # without embedding a language-specific title or article word list in code.
    for prior_id, prior_card in sorted(prior_cards.items()):
        if not _is_stable_candidate(prior_id, manifest_states):
            continue
        for current_id, current_card in sorted(current_cards.items()):
            if (prior_id, current_id) in pair_covered:
                continue
            if not _is_stable_candidate(current_id, manifest_states):
                continue
            prior_surfaces = _stable_surface_keys(prior_card)
            current_surfaces = _stable_surface_keys(current_card)
            has_lexical_overlap = bool(
                set(prior_surfaces).intersection(current_surfaces)
            ) or any(
                _strict_contiguous_core(left, right)
                for left in prior_surfaces
                for right in current_surfaces
            )
            if has_lexical_overlap:
                lexically_accounted_prior.add(prior_id)
            if _card_kind(prior_card) != _card_kind(current_card):
                continue
            cores = sorted(
                {
                    core
                    for left in prior_surfaces
                    for right in current_surfaces
                    if (core := _strict_contiguous_core(left, right)) is not None
                }
            )
            if not cores:
                continue
            pair_covered.add((prior_id, current_id))
            lexically_accounted_prior.add(prior_id)
            block_ids = sorted(set(_card_block_ids(prior_card) + _card_block_ids(current_card)))
            artifact_hashes = sorted(
                {
                    prior_card["context_card_hash"],
                    current_card["context_card_hash"],
                    *[_block_hash(catalog["block_by_id"][block_id]) for block_id in block_ids],
                }
            )
            candidates.append(
                _lead(
                    lineage_id=prefix["state_lineage_id"],
                    trigger_kind="surface_core_overlap",
                    surface_keys=cores,
                    prior_card_ids=[prior_id],
                    current_candidate_card_ids=[current_id],
                    chapter_ids=[*_card_chapters(prior_card), chapter_id],
                    source_block_ids=block_ids,
                    source_artifact_hashes=artifact_hashes,
                )
            )

    # A shared terminal token among multi-token person names is only a retrieval
    # signal. A matching effective gender keeps family-name components bounded;
    # disputed or absent gender never participates in this mechanical hint.
    terminal_groups: dict[tuple[str, str], dict[str, set[str]]] = {}
    for side, table in (("prior", prior_cards), ("current", current_cards)):
        for card_id, card in sorted(table.items()):
            if not _is_stable_candidate(card_id, manifest_states):
                continue
            if _card_kind(card) != "person":
                continue
            gender = _card_gender(card)
            if gender is None:
                continue
            for surface in _stable_surface_keys(card):
                tokens = _surface_tokens(surface)
                if len(tokens) < 2:
                    continue
                terminal_groups.setdefault(
                    (tokens[-1], gender), {"prior": set(), "current": set()}
                )[side].add(card_id)
    covered_prior_ids = {prior_id for prior_id, _current_id in pair_covered}
    covered_current_ids = {current_id for _prior_id, current_id in pair_covered}
    for (terminal, _gender), groups in sorted(terminal_groups.items()):
        prior_ids = sorted(groups["prior"])
        current_ids = sorted(groups["current"])
        if not prior_ids or not current_ids:
            continue
        if set(prior_ids).issubset(covered_prior_ids) and set(
            current_ids
        ).issubset(covered_current_ids):
            continue
        owner_ids = [*prior_ids, *current_ids]
        for prior_id in prior_ids:
            for current_id in current_ids:
                pair_covered.add((prior_id, current_id))
        lexically_accounted_prior.update(prior_ids)
        block_ids = sorted(
            {
                block_id
                for card_id in owner_ids
                for block_id in _card_block_ids(cards[card_id])
            }
        )
        artifact_hashes = sorted(
            {
                *[cards[card_id]["context_card_hash"] for card_id in owner_ids],
                *[
                    _block_hash(catalog["block_by_id"][block_id])
                    for block_id in block_ids
                ],
            }
        )
        candidates.append(
            _lead(
                lineage_id=prefix["state_lineage_id"],
                trigger_kind="person_terminal_token_overlap",
                surface_keys=[terminal],
                prior_card_ids=prior_ids,
                current_candidate_card_ids=current_ids,
                chapter_ids=[
                    *[
                        source_chapter
                        for card_id in prior_ids
                        for source_chapter in _card_chapters(cards[card_id])
                    ],
                    chapter_id,
                ],
                source_block_ids=block_ids,
                source_artifact_hashes=artifact_hashes,
            )
        )

    # A later literal recurrence of a weak card is visible even when B0 did not
    # create a second card. If a current card exists, the pairable triggers above
    # own the case and avoid duplicate lead inflation.
    for prior_id, prior_card in sorted(prior_cards.items()):
        if not _is_stable_candidate(prior_id, manifest_states):
            continue
        provenance_blocks = _card_block_ids(prior_card)
        weak = (
            prior_card.get("authority_scope") == CANDIDATE_ONLY_SCOPE
            or (len(provenance_blocks) == 1 and len(_card_chapters(prior_card)) == 1)
        )
        if not weak or prior_id in lexically_accounted_prior:
            continue
        hit_keys: list[str] = []
        hit_blocks: set[str] = set()
        for key, surface in _stable_surface_keys(prior_card).items():
            blocks = [
                block
                for block in current_blocks
                if _surface_in_block(surface, block)
            ]
            if blocks:
                hit_keys.append(key)
                hit_blocks.update(str(block["block_id"]) for block in blocks)
        if not hit_blocks:
            continue
        block_ids = sorted(set([*provenance_blocks, *hit_blocks]))
        artifact_hashes = sorted(
            {
                prior_card["context_card_hash"],
                *[_block_hash(catalog["block_by_id"][block_id]) for block_id in block_ids],
            }
        )
        recurrence_lead = _lead(
            lineage_id=prefix["state_lineage_id"],
            trigger_kind="first_recurrence_weak_card",
            surface_keys=hit_keys,
            prior_card_ids=[prior_id],
            current_candidate_card_ids=[],
            chapter_ids=[*_card_chapters(prior_card), chapter_id],
            source_block_ids=block_ids,
            source_artifact_hashes=artifact_hashes,
        )
        if (
            prior_id in previous_first_recurrence_cards
            and recurrence_lead["lead_id"] not in previous_ids
        ):
            continue
        candidates.append(recurrence_lead)

    # Deduplicate identical evidence before assigning bounded lifecycle states.
    candidates_by_id = {row["lead_id"]: row for row in candidates}
    ordered = sorted(
        candidates_by_id.values(),
        key=lambda row: (
            row["trigger_kind"],
            tuple(row["prior_card_ids"]),
            tuple(row["current_candidate_card_ids"]),
            tuple(row["surface_keys"]),
            row["lead_id"],
        ),
    )
    # Compute the actual connected components before applying the component cap.
    # Several leads touching the same cards consume one Auditor component, not
    # one component per lead.
    parent: dict[str, str] = {}

    def find(card_id: str) -> str:
        parent.setdefault(card_id, card_id)
        if parent[card_id] != card_id:
            parent[card_id] = find(parent[card_id])
        return parent[card_id]

    def union(left: str, right: str) -> None:
        left_root = find(left)
        right_root = find(right)
        if left_root == right_root:
            return
        smaller, larger = sorted((left_root, right_root))
        parent[larger] = smaller

    for offset, row in enumerate(ordered):
        owners = sorted(set([*row["prior_card_ids"], *row["current_candidate_card_ids"]]))
        if (
            offset >= max_leads_per_chapter
            or row["lead_id"] in previous_ids
            or len(owners) < 2
        ):
            continue
        for owner in owners:
            find(owner)
        for owner in owners[1:]:
            union(owners[0], owner)
    pairable_roots = sorted({find(card_id) for card_id in parent})
    allowed_roots = set(pairable_roots[:max_identity_components_per_chapter])

    leads: list[dict[str, Any]] = []
    for offset, source in enumerate(ordered):
        row = _clone(source)
        owner_count = len(row["prior_card_ids"]) + len(row["current_candidate_card_ids"])
        if row["lead_id"] in previous_ids:
            lifecycle = "duplicate_suppressed"
        elif offset >= max_leads_per_chapter:
            lifecycle = "chapter_cap_deferred"
        elif owner_count >= 2:
            owner_root = find(
                sorted(
                    set([*row["prior_card_ids"], *row["current_candidate_card_ids"]])
                )[0]
            )
            if owner_root in allowed_roots:
                lifecycle = "queued_pairable"
            else:
                lifecycle = "chapter_cap_deferred"
        else:
            lifecycle = "waiting_for_pair"
        row["lifecycle_state"] = lifecycle
        leads.append(row)

    # Non-lexical unresolved animal/nonhuman pairs remain an explicit watch item,
    # never a false positive lead.
    watches: dict[str, dict[str, Any]] = {}
    for prior_id, prior_card in sorted(prior_cards.items()):
        if manifest_states.get(prior_id) != "unresolved":
            continue
        if _card_kind(prior_card) not in {"animal", "nonhuman_character"}:
            continue
        prior_keys = set(_stable_surface_keys(prior_card))
        for current_id, current_card in sorted(current_cards.items()):
            if manifest_states.get(current_id) != "unresolved":
                continue
            if _card_kind(current_card) != _card_kind(prior_card):
                continue
            current_keys = set(_stable_surface_keys(current_card))
            if prior_keys.intersection(current_keys):
                continue
            if any(
                _strict_contiguous_core(left, right)
                for left in prior_keys
                for right in current_keys
            ):
                continue
            watch = _watch_item(
                lineage_id=prefix["state_lineage_id"],
                prior_card_ids=[prior_id],
                current_card_ids=[current_id],
                chapter_ids=[*_card_chapters(prior_card), chapter_id],
            )
            watches[watch["watch_id"]] = watch

    counts = {
        "lead_count": len(leads),
        "queued_pairable_count": sum(row["lifecycle_state"] == "queued_pairable" for row in leads),
        "waiting_for_pair_count": sum(row["lifecycle_state"] == "waiting_for_pair" for row in leads),
        "duplicate_suppressed_count": sum(row["lifecycle_state"] == "duplicate_suppressed" for row in leads),
        "chapter_cap_deferred_count": sum(row["lifecycle_state"] == "chapter_cap_deferred" for row in leads),
        "queued_component_count": _queued_component_count(leads),
        "unsupported_watch_count": len(watches),
    }
    body = {
        "schema_version": LEAD_INDEX_SCHEMA_VERSION,
        "validator_version": LEAD_INDEX_VALIDATOR_VERSION,
        "state_lineage_id": prefix["state_lineage_id"],
        "coverage_through_chapter_id": chapter_id,
        "prefix_bundle_hash": prefix["prefix_bundle_hash"],
        "bounds": {
            "max_leads_per_chapter": max_leads_per_chapter,
            "max_identity_components_per_chapter": max_identity_components_per_chapter,
        },
        "leads": leads,
        "unsupported_watch_items": sorted(watches.values(), key=lambda row: row["watch_id"]),
        "counts": counts,
        "production_publish_performed": False,
    }
    result = {**body, "lead_index_hash": canonical_hash(body)}
    return verify_semantic_candidate_lead_index_v1(result)


def build_semantic_candidate_lead_index_from_profile_v1(
    *,
    document: Mapping[str, Any],
    prefix_bundle: Mapping[str, Any],
    current_chapter_id: str,
    chapter_cycle_profile: Any,
    previous_lead_index: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Use the validated Console-facing profile as the runtime cap source."""

    limits = getattr(chapter_cycle_profile, "semantic_leads", None)
    if not isinstance(limits, Mapping):
        raise SemanticCandidateLeadError("chapter-cycle profile lacks semantic lead limits")
    if limits.get("overflow_action") != "defer_without_authority":
        raise SemanticCandidateLeadError("chapter-cycle profile has an unsafe overflow action")
    return build_semantic_candidate_lead_index_v1(
        document=document,
        prefix_bundle=prefix_bundle,
        current_chapter_id=current_chapter_id,
        previous_lead_index=previous_lead_index,
        max_leads_per_chapter=int(limits.get("max_leads_per_chapter", -1)),
        max_identity_components_per_chapter=int(
            limits.get("max_identity_components_per_chapter", -1)
        ),
    )


def verify_semantic_candidate_lead_index_v1(
    index: Mapping[str, Any],
) -> dict[str, Any]:
    if index.get("schema_version") != LEAD_INDEX_SCHEMA_VERSION:
        raise SemanticCandidateLeadError("foreign T-lite lead index schema")
    if index.get("validator_version") != LEAD_INDEX_VALIDATOR_VERSION:
        raise SemanticCandidateLeadError("T-lite lead validator mismatch")
    body = dict(index)
    observed = _required_string(body.pop("lead_index_hash", None), "lead_index_hash")
    if canonical_hash(body) != observed:
        raise SemanticCandidateLeadError("T-lite lead index hash mismatch")
    if index.get("production_publish_performed") is not False:
        raise SemanticCandidateLeadError("T-lite lead index claims publication")
    lineage = _required_string(index.get("state_lineage_id"), "state_lineage_id")
    _required_string(index.get("coverage_through_chapter_id"), "coverage chapter")
    _required_string(index.get("prefix_bundle_hash"), "prefix_bundle_hash")
    bounds = index.get("bounds")
    if not isinstance(bounds, Mapping) or set(bounds) != {
        "max_leads_per_chapter",
        "max_identity_components_per_chapter",
    }:
        raise SemanticCandidateLeadError("T-lite bounds are malformed")
    if not all(isinstance(value, int) and value >= 0 for value in bounds.values()):
        raise SemanticCandidateLeadError("T-lite bounds must be non-negative integers")
    leads = index.get("leads")
    if not isinstance(leads, list):
        raise SemanticCandidateLeadError("T-lite leads must be a list")
    expected_order = sorted(
        leads,
        key=lambda row: (
            str(row.get("trigger_kind") or ""),
            tuple(row.get("prior_card_ids") or []),
            tuple(row.get("current_candidate_card_ids") or []),
            tuple(row.get("surface_keys") or []),
            str(row.get("lead_id") or ""),
        ),
    )
    if leads != expected_order:
        raise SemanticCandidateLeadError("T-lite leads are not canonically ordered")
    seen: set[str] = set()
    for row in leads:
        if not isinstance(row, Mapping):
            raise SemanticCandidateLeadError("T-lite lead must be an object")
        if set(row) != {
            "lead_id",
            "schema_version",
            "state_lineage_id",
            "trigger_kind",
            "surface_keys",
            "prior_card_ids",
            "current_candidate_card_ids",
            "chapter_ids",
            "source_block_ids",
            "source_artifact_hashes",
            "evidence_manifest_hash",
            "lifecycle_state",
            "authority_effect",
            "created_at_stage",
        }:
            raise SemanticCandidateLeadError("T-lite lead fields are not closed")
        lead_id = _required_string(row.get("lead_id"), "lead_id")
        if lead_id in seen:
            raise SemanticCandidateLeadError("T-lite lead index repeats a lead")
        seen.add(lead_id)
        if row.get("schema_version") != LEAD_SCHEMA_VERSION:
            raise SemanticCandidateLeadError("foreign T-lite lead schema")
        if row.get("state_lineage_id") != lineage:
            raise SemanticCandidateLeadError("T-lite lead crosses lineage")
        if row.get("trigger_kind") not in TRIGGER_KINDS:
            raise SemanticCandidateLeadError("T-lite lead has a foreign trigger")
        surfaces = _string_list(row.get("surface_keys"), "surface_keys")
        prior_ids = _string_list(row.get("prior_card_ids"), "prior_card_ids")
        current_ids = _string_list(
            row.get("current_candidate_card_ids"),
            "current_candidate_card_ids",
            allow_empty=True,
        )
        if set(prior_ids).intersection(current_ids):
            raise SemanticCandidateLeadError("T-lite lead repeats a card across roles")
        chapters = _string_list(row.get("chapter_ids"), "chapter_ids")
        if len(chapters) < 2:
            raise SemanticCandidateLeadError("T-lite lead is not cross-chapter")
        blocks = _string_list(row.get("source_block_ids"), "source_block_ids")
        artifacts = _hash_list(row.get("source_artifact_hashes"), "source artifacts")
        evidence = _lead_evidence_body(
            trigger_kind=str(row["trigger_kind"]),
            surface_keys=surfaces,
            prior_card_ids=prior_ids,
            current_candidate_card_ids=current_ids,
            chapter_ids=chapters,
            source_block_ids=blocks,
            source_artifact_hashes=artifacts,
        )
        if row.get("evidence_manifest_hash") != canonical_hash(evidence):
            raise SemanticCandidateLeadError("T-lite evidence manifest hash mismatch")
        if row.get("lifecycle_state") not in LIFECYCLE_STATES:
            raise SemanticCandidateLeadError("T-lite lead has a foreign lifecycle")
        if row.get("authority_effect") != "none":
            raise SemanticCandidateLeadError("T-lite lead grants authority")
        if row.get("created_at_stage") != CREATED_AT_STAGE:
            raise SemanticCandidateLeadError("T-lite lead has a foreign stage")
        expected_id = "semlead1_" + canonical_hash(_lead_identity_body(row))[:20]
        if lead_id != expected_id:
            raise SemanticCandidateLeadError("T-lite lead id is stale")
        owner_count = len(prior_ids) + len(current_ids)
        if row["trigger_kind"] == "first_recurrence_weak_card" and (
            len(prior_ids) != 1 or current_ids
        ):
            raise SemanticCandidateLeadError("first-recurrence lead shape is invalid")
        if row["trigger_kind"] in {
            "surface_core_overlap",
            "exact_surface_collision",
        } and (not prior_ids or not current_ids):
            raise SemanticCandidateLeadError("pairable lexical lead shape is invalid")
        if row["lifecycle_state"] == "queued_pairable" and owner_count < 2:
            raise SemanticCandidateLeadError("pairable T-lite lead lacks a pair")
        if row["lifecycle_state"] == "waiting_for_pair" and owner_count != 1:
            raise SemanticCandidateLeadError("singleton T-lite lead is not a singleton")
    watches = index.get("unsupported_watch_items")
    if not isinstance(watches, list):
        raise SemanticCandidateLeadError("unsupported watch items must be a list")
    watch_ids: set[str] = set()
    for row in watches:
        if not isinstance(row, Mapping):
            raise SemanticCandidateLeadError("unsupported watch item must be an object")
        watch_id = _required_string(row.get("watch_id"), "watch_id")
        if watch_id in watch_ids:
            raise SemanticCandidateLeadError("unsupported watch item repeats an id")
        watch_ids.add(watch_id)
        watch_body = {key: _clone(value) for key, value in row.items() if key != "watch_id"}
        if watch_id != "semwatch1_" + canonical_hash(watch_body)[:20]:
            raise SemanticCandidateLeadError("unsupported watch id is stale")
        if row.get("watch_kind") != "nonlexical_unnamed_identity_not_evaluated":
            raise SemanticCandidateLeadError("unsupported watch kind is foreign")
        if row.get("authority_effect") != "none":
            raise SemanticCandidateLeadError("unsupported watch grants authority")
        if row.get("resolution_state") != "unsupported_by_t_lite":
            raise SemanticCandidateLeadError("unsupported watch claims resolution")
    expected_counts = {
        "lead_count": len(leads),
        "queued_pairable_count": sum(row["lifecycle_state"] == "queued_pairable" for row in leads),
        "waiting_for_pair_count": sum(row["lifecycle_state"] == "waiting_for_pair" for row in leads),
        "duplicate_suppressed_count": sum(row["lifecycle_state"] == "duplicate_suppressed" for row in leads),
        "chapter_cap_deferred_count": sum(row["lifecycle_state"] == "chapter_cap_deferred" for row in leads),
        "queued_component_count": _queued_component_count(leads),
        "unsupported_watch_count": len(watches),
    }
    if index.get("counts") != expected_counts:
        raise SemanticCandidateLeadError("T-lite counts are stale")
    processed_count = sum(
        row["lifecycle_state"] in {"queued_pairable", "waiting_for_pair"}
        for row in leads
    )
    if processed_count > int(bounds["max_leads_per_chapter"]):
        raise SemanticCandidateLeadError("T-lite processed lead count exceeds its cap")
    if expected_counts["queued_component_count"] > int(
        bounds["max_identity_components_per_chapter"]
    ):
        raise SemanticCandidateLeadError("T-lite Identity component count exceeds its cap")
    return _clone(dict(index))


def apply_semantic_candidate_leads_to_prefix_v1(
    *,
    prefix_bundle: Mapping[str, Any],
    lead_index: Mapping[str, Any],
    continuity_confirmed_prior_card_ids: Sequence[str] = (),
) -> dict[str, Any]:
    """Project leads while honoring explicit current-chapter continuity."""

    prefix = verify_chapter_prefix_prior_bundle_v1(prefix_bundle)
    index = verify_semantic_candidate_lead_index_v1(lead_index)
    if index["state_lineage_id"] != prefix["state_lineage_id"]:
        raise SemanticCandidateLeadError("T-lite projection crosses lineage")
    if index["prefix_bundle_hash"] != prefix["prefix_bundle_hash"]:
        raise SemanticCandidateLeadError("T-lite projection targets a stale prefix")
    active = {
        row["prior_card_id"]: _clone(row) for row in prefix["b0_context_cards"]
    }
    candidates = {
        row["prior_card_id"]: _clone(row)
        for row in prefix["candidate_only_context_cards"]
    }
    all_card_ids = set(active).union(candidates)
    continuity_confirmed = {
        _required_string(value, "continuity_confirmed_prior_card_id")
        for value in continuity_confirmed_prior_card_ids
    }
    if len(continuity_confirmed) != len(continuity_confirmed_prior_card_ids):
        raise SemanticCandidateLeadError("continuity confirmations repeat a card")
    if not continuity_confirmed.issubset(all_card_ids):
        raise SemanticCandidateLeadError("continuity confirmation cites a foreign card")
    uncertainties = {
        row["uncertainty_id"]: _clone(row)
        for row in prefix["prefix_identity_uncertainties"]
    }
    existing_pairs = {
        (
            str(row.get("surface_key") or ""),
            tuple(sorted(row.get("prior_card_ids") or [])),
        )
        for row in uncertainties.values()
    }
    reason_codes = {
        "first_recurrence_weak_card": "semantic_candidate_first_recurrence_weak_card",
        "person_terminal_token_overlap": (
            "semantic_candidate_person_terminal_token_overlap"
        ),
        "surface_core_overlap": "semantic_candidate_surface_core_overlap",
        "exact_surface_collision": "semantic_candidate_exact_surface_collision",
    }
    for lead in index["leads"]:
        if lead["lifecycle_state"] == "duplicate_suppressed":
            continue
        owner_ids = sorted(
            set([*lead["prior_card_ids"], *lead["current_candidate_card_ids"]])
        )
        # A weak singleton remains non-authoritative even when the current B1
        # call reports same-referent continuity. That observation is retained
        # in the sealed challenge artifact, but one B1 judgment cannot prove
        # that a sparse historical/name-only card and a later occurrence are
        # the same referent. Downscope until a pairable candidate or later
        # bounded review can establish identity.
        if not set(owner_ids).issubset(all_card_ids):
            raise SemanticCandidateLeadError("T-lite lead references a foreign prefix card")
        surface_key = lead["surface_keys"][0]
        if (
            lead["trigger_kind"] == "exact_surface_collision"
            and (surface_key, tuple(owner_ids)) in existing_pairs
        ):
            continue
        uncertainty_body = {
            "surface_key": surface_key,
            "prior_card_ids": owner_ids,
            "chapter_ids": list(lead["chapter_ids"]),
            "status": "pending_identity_review",
            "authority_effect": "candidate_only",
            "reason_code": reason_codes[lead["trigger_kind"]],
            "source_block_ids": list(lead["source_block_ids"]),
            "evidence_manifest_hash": lead["evidence_manifest_hash"],
            "semantic_candidate_lead_id": lead["lead_id"],
            "review_deferred": lead["lifecycle_state"] == "chapter_cap_deferred",
        }
        uncertainty = {
            "uncertainty_id": "prefixunc1_" + canonical_hash(uncertainty_body)[:20],
            **uncertainty_body,
        }
        uncertainties.setdefault(uncertainty["uncertainty_id"], uncertainty)
        for card_id in owner_ids:
            source = active.pop(card_id, None)
            if source is None:
                source = candidates[card_id]
            body = {
                key: _clone(value)
                for key, value in source.items()
                if key != "context_card_hash"
            }
            body["authority_scope"] = CANDIDATE_ONLY_SCOPE
            disputes = list(body.get("disputed_claims") or [])
            if not any(
                row.get("uncertainty_id") == uncertainty["uncertainty_id"]
                for row in disputes
                if isinstance(row, Mapping)
            ):
                disputes.append(
                    {
                        "disputed_field": "identity_membership",
                        "historical_value": None,
                        "status": "pending",
                        "pending_reason_codes": ["semantic_candidate_lead"],
                        "evidence_manifest_hashes": [lead["evidence_manifest_hash"]],
                        "hearing_count": 0,
                        "automatic_hearing_limit": 2,
                        "same_evidence_reopen_forbidden": True,
                        "next_review_trigger": "identity_resolution",
                        "revision_ids": [],
                        "uncertainty_id": uncertainty["uncertainty_id"],
                        "semantic_candidate_lead_id": lead["lead_id"],
                    }
                )
            body["disputed_claims"] = disputes
            candidates[card_id] = {
                **body,
                "context_card_hash": canonical_hash(body),
            }
    body = {
        key: _clone(value)
        for key, value in prefix.items()
        if key
        not in {
            "prefix_bundle_hash",
            "b0_context_cards",
            "candidate_only_context_cards",
            "prefix_identity_uncertainties",
        }
    }
    body["b0_context_cards"] = sorted(active.values(), key=lambda row: row["prior_card_id"])
    body["candidate_only_context_cards"] = sorted(
        candidates.values(), key=lambda row: row["prior_card_id"]
    )
    body["prefix_identity_uncertainties"] = sorted(
        uncertainties.values(), key=lambda row: row["uncertainty_id"]
    )
    body["production_publish_performed"] = False
    result = {**body, "prefix_bundle_hash": canonical_hash(body)}
    return verify_chapter_prefix_prior_bundle_v1(result)


def verify_semantic_identity_occurrence_bridge_v1(
    report: Mapping[str, Any],
) -> dict[str, Any]:
    if set(report) != {
        "schema_version",
        "validator_version",
        "state_lineage_id",
        "coverage_through_chapter_id",
        "input_prefix_bundle_hash",
        "output_prefix_bundle_hash",
        "lead_index_hash",
        "max_occurrence_cards_per_lead",
        "rows",
        "counts",
        "production_publish_performed",
        "bridge_hash",
    }:
        raise SemanticCandidateLeadError("occurrence-bridge fields are not closed")
    if report.get("schema_version") != OCCURRENCE_BRIDGE_SCHEMA_VERSION:
        raise SemanticCandidateLeadError("foreign occurrence-bridge schema")
    if report.get("validator_version") != OCCURRENCE_BRIDGE_VALIDATOR_VERSION:
        raise SemanticCandidateLeadError("occurrence-bridge validator mismatch")
    body = _clone(dict(report))
    observed = _required_string(body.pop("bridge_hash", None), "bridge_hash")
    if canonical_hash(body) != observed:
        raise SemanticCandidateLeadError("occurrence-bridge hash mismatch")
    for label in (
        "state_lineage_id",
        "input_prefix_bundle_hash",
        "output_prefix_bundle_hash",
        "lead_index_hash",
    ):
        value = _required_string(report.get(label), label)
        if len(value) != 64 or any(char not in "0123456789abcdef" for char in value):
            raise SemanticCandidateLeadError(f"{label} is not a sha256")
    if report.get("production_publish_performed") is not False:
        raise SemanticCandidateLeadError("occurrence bridge claims publication")
    _required_string(
        report.get("coverage_through_chapter_id"),
        "coverage_through_chapter_id",
    )
    cap = report.get("max_occurrence_cards_per_lead")
    if not isinstance(cap, int) or cap < 1:
        raise SemanticCandidateLeadError("occurrence bridge cap is invalid")
    rows = report.get("rows")
    if not isinstance(rows, list):
        raise SemanticCandidateLeadError("occurrence bridge rows are malformed")
    seen: set[str] = set()
    for row in rows:
        if not isinstance(row, Mapping) or set(row) != {
            "lead_id",
            "surface_key",
            "source_block_ids",
            "occurrence_candidate_card_ids",
            "lifecycle_state",
            "authority_effect",
        }:
            raise SemanticCandidateLeadError("occurrence bridge row fields are not closed")
        lead_id = _required_string(row.get("lead_id"), "lead_id")
        if lead_id in seen:
            raise SemanticCandidateLeadError("occurrence bridge repeats a lead")
        seen.add(lead_id)
        _required_string(row.get("surface_key"), "surface_key")
        _string_list(
            row.get("source_block_ids"),
            "source_block_ids",
            allow_empty=True,
        )
        card_ids = _string_list(
            row.get("occurrence_candidate_card_ids"),
            "occurrence_candidate_card_ids",
            allow_empty=True,
        )
        state = row.get("lifecycle_state")
        if state not in OCCURRENCE_BRIDGE_STATES:
            raise SemanticCandidateLeadError("occurrence bridge row has a foreign state")
        if state == "paired_for_identity_review" and not card_ids:
            raise SemanticCandidateLeadError("paired occurrence bridge row has no card")
        if state != "paired_for_identity_review" and card_ids:
            raise SemanticCandidateLeadError("deferred occurrence bridge row minted a card")
        if row.get("authority_effect") != "none":
            raise SemanticCandidateLeadError("occurrence bridge grants authority")
    expected_counts = {
        "waiting_input_count": len(rows),
        "paired_count": sum(
            row["lifecycle_state"] == "paired_for_identity_review" for row in rows
        ),
        "unlocatable_count": sum(
            row["lifecycle_state"] == "unlocatable_surface" for row in rows
        ),
        "component_cap_deferred_count": sum(
            row["lifecycle_state"] == "component_cap_deferred" for row in rows
        ),
        "occurrence_cap_deferred_count": sum(
            row["lifecycle_state"] == "occurrence_cap_deferred" for row in rows
        ),
        "occurrence_card_count": sum(
            len(row["occurrence_candidate_card_ids"]) for row in rows
        ),
        "unresolved_count": sum(
            row["lifecycle_state"] != "paired_for_identity_review" for row in rows
        ),
    }
    if report.get("counts") != expected_counts:
        raise SemanticCandidateLeadError("occurrence bridge counts are stale")
    return _clone(dict(report))


def materialize_waiting_identity_occurrences_v1(
    *,
    document: Mapping[str, Any],
    prefix_bundle: Mapping[str, Any],
    lead_index: Mapping[str, Any],
    max_occurrence_cards_per_lead: int = DEFAULT_MAX_OCCURRENCE_CARDS_PER_LEAD,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Give a singleton lead pairable evidence without deciding co-reference.

    Each exact current-chapter block match becomes its own candidate-only card.
    No kind, gender, summary, identity, or surface authority is inferred.
    """

    if max_occurrence_cards_per_lead < 1:
        raise SemanticCandidateLeadError("occurrence-card cap must be positive")
    prefix = verify_chapter_prefix_prior_bundle_v1(prefix_bundle, document=document)
    index = verify_semantic_candidate_lead_index_v1(lead_index)
    if index["state_lineage_id"] != prefix["state_lineage_id"]:
        raise SemanticCandidateLeadError("occurrence bridge crosses lineage")
    if index["coverage_through_chapter_id"] != prefix["coverage_through_chapter_id"]:
        raise SemanticCandidateLeadError("occurrence bridge coverage is stale")

    catalog = _document_catalog(document)
    chapter_id = prefix["coverage_through_chapter_id"]
    active = {
        row["prior_card_id"]: _clone(row) for row in prefix["b0_context_cards"]
    }
    candidates = {
        row["prior_card_id"]: _clone(row)
        for row in prefix["candidate_only_context_cards"]
    }
    uncertainties = {
        row["uncertainty_id"]: _clone(row)
        for row in prefix["prefix_identity_uncertainties"]
    }
    source_rows = {
        row["prior_card_id"]: _clone(row)
        for row in prefix["source_entity_manifest"]
        if isinstance(row, Mapping) and row.get("prior_card_id")
    }
    available_components = max(
        0,
        int(index["bounds"]["max_identity_components_per_chapter"])
        - int(index["counts"]["queued_component_count"]),
    )
    report_rows: list[dict[str, Any]] = []

    for lead in index["leads"]:
        if lead["lifecycle_state"] != "waiting_for_pair":
            continue
        matching_uncertainties = [
            row
            for row in uncertainties.values()
            if row.get("semantic_candidate_lead_id") == lead["lead_id"]
        ]
        if len(matching_uncertainties) != 1:
            raise SemanticCandidateLeadError(
                "waiting lead lacks one projected identity uncertainty"
            )
        old_uncertainty = matching_uncertainties[0]
        surface_key = lead["surface_keys"][0]
        current_blocks = [
            block
            for block in catalog["chapter_blocks"][chapter_id]
            if block["block_id"] in set(lead["source_block_ids"])
        ]
        exact_matches = [
            (block, exact)
            for block in current_blocks
            if (exact := _exact_surface_from_block(surface_key, block)) is not None
        ]
        if available_components <= 0:
            lifecycle = "component_cap_deferred"
        elif not exact_matches:
            lifecycle = "unlocatable_surface"
        elif len(exact_matches) > max_occurrence_cards_per_lead:
            lifecycle = "occurrence_cap_deferred"
        else:
            lifecycle = "paired_for_identity_review"

        occurrence_ids: list[str] = []
        if lifecycle == "paired_for_identity_review":
            occurrence_specs: list[tuple[dict[str, Any], dict[str, Any]]] = []
            for block, exact_surface in exact_matches:
                source_identity = {
                    "state_lineage_id": prefix["state_lineage_id"],
                    "semantic_candidate_lead_id": lead["lead_id"],
                    "chapter_id": chapter_id,
                    "block_id": block["block_id"],
                    "surface": exact_surface,
                }
                source_candidate_id = "semocc1_" + canonical_hash(source_identity)[:20]
                card_id = "pcard1_" + canonical_hash(
                    {**source_identity, "source_candidate_id": source_candidate_id}
                )[:20]
                occurrence_ids.append(card_id)
                occurrence_specs.append(
                    (
                        source_identity,
                        {
                            "prior_card_id": card_id,
                            "canonical_surface": exact_surface,
                            "stable_surfaces": [exact_surface],
                            "effective_claims": {
                                "referent_kind": None,
                                "referential_gender": None,
                                "identity_summary": None,
                            },
                            "authority_scope": CANDIDATE_ONLY_SCOPE,
                            "first_supported_block_id": block["block_id"],
                            "provenance_refs": [
                                {
                                    "chapter_id": chapter_id,
                                    "block_id": block["block_id"],
                                }
                            ],
                            "source_candidate_id": source_candidate_id,
                        },
                    )
                )
            owner_ids = sorted(
                set([*old_uncertainty["prior_card_ids"], *occurrence_ids])
            )
            evidence_hash = canonical_hash(
                {
                    "semantic_candidate_lead_evidence_manifest_hash": lead[
                        "evidence_manifest_hash"
                    ],
                    "occurrence_source_rows": occurrence_specs,
                }
            )
            uncertainty_body = {
                "surface_key": surface_key,
                "prior_card_ids": owner_ids,
                "chapter_ids": list(lead["chapter_ids"]),
                "status": "pending_identity_review",
                "authority_effect": "candidate_only",
                "reason_code": "semantic_candidate_first_recurrence_weak_card",
                "source_block_ids": list(lead["source_block_ids"]),
                "evidence_manifest_hash": evidence_hash,
                "semantic_candidate_lead_id": lead["lead_id"],
                "review_deferred": False,
            }
            uncertainty = {
                "uncertainty_id": "prefixunc1_"
                + canonical_hash(uncertainty_body)[:20],
                **uncertainty_body,
            }
            dispute = {
                "disputed_field": "identity_membership",
                "historical_value": None,
                "status": "pending",
                "pending_reason_codes": ["semantic_candidate_lead"],
                "evidence_manifest_hashes": [evidence_hash],
                "hearing_count": 0,
                "automatic_hearing_limit": 2,
                "same_evidence_reopen_forbidden": True,
                "next_review_trigger": "identity_resolution",
                "revision_ids": [],
                "uncertainty_id": uncertainty["uncertainty_id"],
                "semantic_candidate_lead_id": lead["lead_id"],
            }
            for source_identity, card_base in occurrence_specs:
                card_body = {**card_base, "disputed_claims": [_clone(dispute)]}
                card = {
                    **card_body,
                    "context_card_hash": canonical_hash(card_body),
                }
                prior = candidates.get(card["prior_card_id"])
                if prior is not None and prior != card:
                    raise SemanticCandidateLeadError(
                        "occurrence card id collision with unequal bytes"
                    )
                candidates[card["prior_card_id"]] = card
                manifest_body = {
                    "prior_card_id": card["prior_card_id"],
                    "source_candidate_id": card["source_candidate_id"],
                    "local_state": "unresolved",
                    "authority_scope": CANDIDATE_ONLY_SCOPE,
                    "source_row_hash": canonical_hash(source_identity),
                    "all_source_block_ids": [source_identity["block_id"]],
                }
                prior_manifest = source_rows.get(card["prior_card_id"])
                if prior_manifest is not None and prior_manifest != manifest_body:
                    raise SemanticCandidateLeadError(
                        "occurrence source row collides with unequal bytes"
                    )
                source_rows[card["prior_card_id"]] = manifest_body
            for card_id in owner_ids:
                source = active.pop(card_id, None)
                if source is None:
                    source = candidates.get(card_id)
                if source is None:
                    raise SemanticCandidateLeadError(
                        "occurrence bridge references a foreign prefix card"
                    )
                card_body = {
                    key: _clone(value)
                    for key, value in source.items()
                    if key != "context_card_hash"
                }
                card_body["authority_scope"] = CANDIDATE_ONLY_SCOPE
                card_body["disputed_claims"] = [
                    row
                    for row in card_body.get("disputed_claims") or []
                    if not (
                        isinstance(row, Mapping)
                        and (
                            row.get("semantic_candidate_lead_id") == lead["lead_id"]
                            or row.get("uncertainty_id")
                            == old_uncertainty["uncertainty_id"]
                        )
                    )
                ]
                card_body["disputed_claims"].append(_clone(dispute))
                candidates[card_id] = {
                    **card_body,
                    "context_card_hash": canonical_hash(card_body),
                }
            uncertainties.pop(old_uncertainty["uncertainty_id"])
            uncertainties[uncertainty["uncertainty_id"]] = uncertainty
            available_components -= 1

        report_rows.append(
            {
                "lead_id": lead["lead_id"],
                "surface_key": surface_key,
                "source_block_ids": sorted(
                    block["block_id"] for block, _ in exact_matches
                ),
                "occurrence_candidate_card_ids": sorted(occurrence_ids),
                "lifecycle_state": lifecycle,
                "authority_effect": "none",
            }
        )

    prefix_body = {
        key: _clone(value)
        for key, value in prefix.items()
        if key
        not in {
            "prefix_bundle_hash",
            "b0_context_cards",
            "candidate_only_context_cards",
            "source_entity_manifest",
            "prefix_identity_uncertainties",
        }
    }
    prefix_body["b0_context_cards"] = sorted(
        active.values(), key=lambda row: row["prior_card_id"]
    )
    prefix_body["candidate_only_context_cards"] = sorted(
        candidates.values(), key=lambda row: row["prior_card_id"]
    )
    prefix_body["source_entity_manifest"] = sorted(
        source_rows.values(), key=lambda row: row["prior_card_id"]
    )
    prefix_body["prefix_identity_uncertainties"] = sorted(
        uncertainties.values(), key=lambda row: row["uncertainty_id"]
    )
    prefix_body["production_publish_performed"] = False
    output_prefix = {
        **prefix_body,
        "prefix_bundle_hash": canonical_hash(prefix_body),
    }
    output_prefix = verify_chapter_prefix_prior_bundle_v1(
        output_prefix,
        document=document,
    )

    counts = {
        "waiting_input_count": len(report_rows),
        "paired_count": sum(
            row["lifecycle_state"] == "paired_for_identity_review"
            for row in report_rows
        ),
        "unlocatable_count": sum(
            row["lifecycle_state"] == "unlocatable_surface" for row in report_rows
        ),
        "component_cap_deferred_count": sum(
            row["lifecycle_state"] == "component_cap_deferred"
            for row in report_rows
        ),
        "occurrence_cap_deferred_count": sum(
            row["lifecycle_state"] == "occurrence_cap_deferred"
            for row in report_rows
        ),
        "occurrence_card_count": sum(
            len(row["occurrence_candidate_card_ids"]) for row in report_rows
        ),
        "unresolved_count": sum(
            row["lifecycle_state"] != "paired_for_identity_review"
            for row in report_rows
        ),
    }
    report_body = {
        "schema_version": OCCURRENCE_BRIDGE_SCHEMA_VERSION,
        "validator_version": OCCURRENCE_BRIDGE_VALIDATOR_VERSION,
        "state_lineage_id": prefix["state_lineage_id"],
        "coverage_through_chapter_id": chapter_id,
        "input_prefix_bundle_hash": prefix["prefix_bundle_hash"],
        "output_prefix_bundle_hash": output_prefix["prefix_bundle_hash"],
        "lead_index_hash": index["lead_index_hash"],
        "max_occurrence_cards_per_lead": max_occurrence_cards_per_lead,
        "rows": sorted(report_rows, key=lambda row: row["lead_id"]),
        "counts": counts,
        "production_publish_performed": False,
    }
    report = {**report_body, "bridge_hash": canonical_hash(report_body)}
    return output_prefix, verify_semantic_identity_occurrence_bridge_v1(report)


__all__ = [
    "CREATED_AT_STAGE",
    "DEFAULT_MAX_IDENTITY_COMPONENTS_PER_CHAPTER",
    "DEFAULT_MAX_LEADS_PER_CHAPTER",
    "DEFAULT_MAX_OCCURRENCE_CARDS_PER_LEAD",
    "LEAD_INDEX_SCHEMA_VERSION",
    "LEAD_INDEX_VALIDATOR_VERSION",
    "LEAD_SCHEMA_VERSION",
    "OCCURRENCE_BRIDGE_SCHEMA_VERSION",
    "OCCURRENCE_BRIDGE_VALIDATOR_VERSION",
    "SemanticCandidateLeadError",
    "apply_semantic_candidate_leads_to_prefix_v1",
    "build_semantic_candidate_lead_index_v1",
    "build_semantic_candidate_lead_index_from_profile_v1",
    "compatible_prior_card_ids_from_challenge_v1",
    "materialize_waiting_identity_occurrences_v1",
    "verify_semantic_identity_occurrence_bridge_v1",
    "verify_semantic_candidate_lead_index_v1",
]
