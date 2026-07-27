"""Counterfactual B0 experiment with bounded prior cards and conflict tickets."""

from __future__ import annotations

from copy import deepcopy
from hashlib import sha256
from pathlib import Path
import re
import unicodedata
from typing import Any, Iterable, Mapping, Sequence

from pipeline.literary.b0_entity_inventory_experiment import (
    GLOSSARY_CATEGORIES,
    NAME_CLASSES,
    REFERENTIAL_GENDERS,
    REFERENT_KINDS,
    entity_inventory_response_schema,
    validate_entity_inventory_response,
)
from pipeline.literary.b0_chapter_priority_v1 import (
    make_priority_target,
    priority_schema,
    validate_priority_order,
)
from pipeline.literary.builder_pilot import load_system_prompt_from_design
from pipeline.literary.chapter_registry_schema_v4 import RenderedRegistryRequestV4
from pipeline.literary.checkpoint import canonical_hash, canonical_json


PROMPT_ID = "literary_chapter_entity_inventory_prior_challenge_exp_v5_2"
PROMPT_SHA256 = "f6d5de3ef37115f572ef763331946beec301a58645616f503c47b4816b1fdac6"
PROMPT_UTF8_BYTES = 8041
EXPERIMENT_SCHEMA_VERSION = "b0_entity_prior_challenge_exp_v6"
REVIEW_CASE_PACKET_SCHEMA_VERSION = "literary_relevant_review_case_packet_v1"
PRIOR_PACKET_SCHEMA_VERSION = "b0_prior_packet_v3"
CANDIDATE_ONLY_PACKET_SCHEMA_VERSION = "b0_candidate_only_packet_v2"
GLOSSARY_PACKET_SCHEMA_VERSION = "b0_prior_glossary_packet_v2"
CORRUPTION_MANIFEST_SCHEMA_VERSION = "b0_prior_corruption_manifest_v1"
CORRUPTION_SET_MANIFEST_SCHEMA_VERSION = "b0_prior_corruption_manifest_set_v1"
AUTHORITY_SCOPE = "test_verified_global_as_of_prior_scope"
PRIOR_AUTHORITY_SCOPES = frozenset(
    {AUTHORITY_SCOPE, "chapter_confirmed_prefix", "book_confirmed"}
)
MAX_PRIOR_CARDS = 8
MAX_PRIOR_PROVENANCE_REFS = 8
MAX_CANDIDATE_ONLY_CARDS = 8
MAX_GLOSSARY_CARDS = 12
MAX_MODEL_SURFACE_HIT_BLOCK_IDS = 8
MAX_MODEL_REVIEW_CASE_SOURCE_BLOCK_IDS = 8
MAX_PERSISTED_REVIEW_CASE_SOURCE_BLOCK_IDS = 3
GLOSSARY_LIFECYCLE_STATES = frozenset(
    {"chapter_confirmed", "pending_evidence", "rejected_dormant"}
)
PRIORITY_ITEM_CLASSES = frozenset(
    {
        "prior_entity",
        "candidate_only_entity",
        "prior_glossary",
        "new_entity",
        "new_glossary",
        "unresolved",
    }
)
CANDIDATE_CLAIM_EVIDENCE_FIELDS = frozenset(
    {"referent_kind", "referential_gender", "identity_summary"}
)

ISSUE_TO_FIELD = {
    "kind_conflict": "referent_kind",
    "gender_conflict": "referential_gender",
    "identity_collision": "identity_membership",
    "alias_target_conflict": "alias_target",
    "alias_scope_conflict": "alias_scope",
    "unsupported_stable_claim": "identity_summary",
}
REFERENT_CONTINUITIES = frozenset(
    {"same_referent", "possible_collision", "uncertain"}
)
IDENTITY_ISSUES = frozenset(
    {"identity_collision", "alias_target_conflict", "alias_scope_conflict"}
)
MUTABLE_TEST_FIELDS = frozenset(
    {
        "canonical_surface",
        "stable_surfaces",
        "referent_kind",
        "referential_gender",
        "identity_summary",
    }
)


class B0PriorChallengeError(ValueError):
    """Raised when the counterfactual request or response violates its contract."""


def _required_string(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise B0PriorChallengeError(f"{label} must be a non-empty string")
    return value


def _exact_keys(value: Mapping[str, Any], expected: set[str], label: str) -> None:
    actual = set(value)
    if actual != expected:
        raise B0PriorChallengeError(
            f"{label} field set differs; missing={sorted(expected-actual)}, "
            f"foreign={sorted(actual-expected)}"
        )


def _enum(value: Any, allowed: Iterable[str], label: str) -> str:
    result = _required_string(value, label)
    if result not in allowed:
        raise B0PriorChallengeError(f"{label} has unsupported value {result!r}")
    return result


def _string_list(
    value: Any,
    label: str,
    *,
    minimum: int = 0,
    maximum: int | None = None,
) -> list[str]:
    if not isinstance(value, list):
        raise B0PriorChallengeError(f"{label} must be a list")
    if len(value) < minimum or (maximum is not None and len(value) > maximum):
        raise B0PriorChallengeError(f"{label} violates cardinality bounds")
    rows = [_required_string(item, label) for item in value]
    if len(rows) != len(set(rows)):
        raise B0PriorChallengeError(f"{label} contains duplicates")
    return rows


def _nfc(value: Any) -> str:
    return unicodedata.normalize("NFC", str(value))


def _normalized_surface(value: Any) -> str:
    text = unicodedata.normalize("NFKC", str(value)).casefold()
    text = re.sub(r"\s+", " ", text).strip()
    return text.strip(" \t\r\n.,;:!?\"'()[]{}")


def _block_text(block: Mapping[str, Any]) -> str:
    return _nfc(
        block.get("clean_text") or block.get("source_text") or block.get("text") or ""
    )


def _contains_surface(text: str, surface: str) -> bool:
    start_guard = r"(?<!\w)" if surface[0].isalnum() else ""
    end_guard = r"(?!\w)" if surface[-1].isalnum() else ""
    return (
        re.search(
            start_guard + re.escape(surface) + end_guard,
            text,
            flags=re.IGNORECASE | re.UNICODE,
        )
        is not None
    )


def _source_blocks(chapter: Mapping[str, Any]) -> list[dict[str, Any]]:
    raw = chapter.get("blocks")
    if not isinstance(raw, list) or not raw:
        raise B0PriorChallengeError("chapter must contain source blocks")
    rows: list[dict[str, Any]] = []
    for block in raw:
        if not isinstance(block, Mapping):
            raise B0PriorChallengeError("chapter contains a non-object block")
        block_id = _required_string(block.get("block_id"), "block_id")
        rows.append(
            {
                "block_id": block_id,
                "block_type": str(block.get("block_type") or "paragraph"),
                "order_index": int(block.get("order_index") or 0),
                "text": _block_text(block),
            }
        )
    rows.sort(key=lambda row: (row["order_index"], row["block_id"]))
    if len({row["block_id"] for row in rows}) != len(rows):
        raise B0PriorChallengeError("chapter contains duplicate block ids")
    return rows


def _source_blocks_model_view(
    chapter: Mapping[str, Any],
) -> list[dict[str, str]]:
    """Keep source text and addresses; list order already carries author order."""

    return [
        {
            "block_id": row["block_id"],
            "text": row["text"],
        }
        for row in _source_blocks(chapter)
    ]


def _bounded_model_block_ids(block_ids: Sequence[str]) -> tuple[list[str], bool]:
    """Select ordered representative addresses without changing code-side presence."""

    rows = list(block_ids)
    if len(rows) <= MAX_MODEL_SURFACE_HIT_BLOCK_IDS:
        return rows, False
    head_count = MAX_MODEL_SURFACE_HIT_BLOCK_IDS // 2
    tail_count = MAX_MODEL_SURFACE_HIT_BLOCK_IDS - head_count
    return rows[:head_count] + rows[-tail_count:], True


def _surface_hit_model_view(hit: Mapping[str, Any]) -> dict[str, Any]:
    block_ids = _string_list(
        hit.get("current_block_ids"),
        "current_block_ids",
        minimum=1,
    )
    selected, truncated = _bounded_model_block_ids(block_ids)
    result = {
        "surface": _required_string(hit.get("surface"), "surface"),
        "current_block_ids": selected,
    }
    if truncated:
        result["current_hit_count"] = len(block_ids)
        result["current_block_ids_truncated"] = True
    return result


def _surface_packet_model_view(
    packet: Mapping[str, Any], *, card_key: str
) -> dict[str, Any]:
    return {
        card_key: deepcopy(packet[card_key]),
        "current_surface_hits": [
            _surface_hit_model_view(hit) for hit in packet["current_surface_hits"]
        ],
    }


def _review_case_manifest_model_view(manifest: Mapping[str, Any]) -> dict[str, Any]:
    packets: list[dict[str, Any]] = []
    for raw in manifest["packets"]:
        row = deepcopy(dict(raw))
        row.pop("packet_hash", None)
        block_ids = _string_list(
            row.get("current_surface_hit_block_ids"),
            "current_surface_hit_block_ids",
            minimum=1,
        )
        selected, truncated = _bounded_model_block_ids(block_ids)
        row["current_surface_hit_block_ids"] = selected
        if truncated:
            row["current_surface_hit_count"] = len(block_ids)
            row["current_surface_hit_block_ids_truncated"] = True
        packets.append(row)
    return {
        "schema_version": manifest["schema_version"],
        "chapter_id": manifest["chapter_id"],
        "review_case_ledger_hash": manifest["review_case_ledger_hash"],
        "packets": packets,
        "overflow_count": manifest["overflow_count"],
    }


def _validate_prior_card(value: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise B0PriorChallengeError("prior card must be an object")
    _exact_keys(
        value,
        {
            "prior_card_id",
            "canonical_surface",
            "stable_surfaces",
            "referent_kind",
            "referential_gender",
            "identity_summary",
            "authority_scope",
            "first_supported_block_id",
            "provenance_refs",
        },
        "prior card",
    )
    prior_card_id = _required_string(value.get("prior_card_id"), "prior_card_id")
    canonical_surface = _required_string(
        value.get("canonical_surface"), "canonical_surface"
    )
    stable_surfaces = _string_list(
        value.get("stable_surfaces"), "stable_surfaces", minimum=1
    )
    normalized = [_normalized_surface(surface) for surface in stable_surfaces]
    if not all(normalized) or len(normalized) != len(set(normalized)):
        raise B0PriorChallengeError("stable_surfaces collide after normalization")
    if _normalized_surface(canonical_surface) not in normalized:
        raise B0PriorChallengeError("canonical_surface must occur in stable_surfaces")
    referent_kind = _enum(value.get("referent_kind"), REFERENT_KINDS, "referent_kind")
    gender = value.get("referential_gender")
    if gender is not None:
        gender = _enum(gender, REFERENTIAL_GENDERS, "referential_gender")
    authority = _required_string(value.get("authority_scope"), "authority_scope")
    if authority not in PRIOR_AUTHORITY_SCOPES:
        raise B0PriorChallengeError("prior card authority_scope mismatch")
    raw_refs = value.get("provenance_refs")
    if not isinstance(raw_refs, list) or not 1 <= len(raw_refs) <= MAX_PRIOR_PROVENANCE_REFS:
        raise B0PriorChallengeError("provenance_refs violates cardinality bounds")
    refs: list[dict[str, str]] = []
    for raw_ref in raw_refs:
        if not isinstance(raw_ref, Mapping):
            raise B0PriorChallengeError("provenance ref must be an object")
        _exact_keys(raw_ref, {"chapter_id", "block_id"}, "provenance ref")
        refs.append(
            {
                "chapter_id": _required_string(raw_ref.get("chapter_id"), "chapter_id"),
                "block_id": _required_string(raw_ref.get("block_id"), "block_id"),
            }
        )
    refs.sort(key=lambda row: (row["chapter_id"], row["block_id"]))
    if len({(row["chapter_id"], row["block_id"]) for row in refs}) != len(refs):
        raise B0PriorChallengeError("provenance_refs contains duplicates")
    first_supported_block_id = _required_string(
        value.get("first_supported_block_id"), "first_supported_block_id"
    )
    if first_supported_block_id not in {row["block_id"] for row in refs}:
        raise B0PriorChallengeError("first_supported_block_id is absent from provenance_refs")
    return {
        "prior_card_id": prior_card_id,
        "canonical_surface": canonical_surface,
        "stable_surfaces": stable_surfaces,
        "referent_kind": referent_kind,
        "referential_gender": gender,
        "identity_summary": _required_string(
            value.get("identity_summary"), "identity_summary"
        ),
        "authority_scope": authority,
        "first_supported_block_id": first_supported_block_id,
        "provenance_refs": refs,
    }


def validate_prior_cards(
    prior_cards: Sequence[Mapping[str, Any]] | None,
    *,
    maximum: int | None = MAX_PRIOR_CARDS,
) -> list[dict[str, Any]]:
    if prior_cards is None:
        return []
    if isinstance(prior_cards, (str, bytes)) or not isinstance(prior_cards, Sequence):
        raise B0PriorChallengeError("prior_cards must be a sequence")
    if maximum is not None and len(prior_cards) > maximum:
        raise B0PriorChallengeError("prior_cards exceeds the bounded cap")
    rows = [_validate_prior_card(card) for card in prior_cards]
    rows.sort(key=lambda row: row["prior_card_id"])
    if len({row["prior_card_id"] for row in rows}) != len(rows):
        raise B0PriorChallengeError("prior_cards contains duplicate ids")
    return rows


def _prior_card_model_view(card: Mapping[str, Any]) -> dict[str, Any]:
    return {
        key: deepcopy(card[key])
        for key in (
            "prior_card_id",
            "canonical_surface",
            "stable_surfaces",
            "referent_kind",
            "referential_gender",
            "identity_summary",
        )
    }


def _candidate_only_card_model_view(card: Mapping[str, Any]) -> dict[str, Any]:
    disputes = [
        {
            key: deepcopy(dispute.get(key))
            for key in (
                "disputed_field",
                "historical_value",
                "status",
                "pending_reason_codes",
            )
        }
        for dispute in card["disputed_claims"]
        if isinstance(dispute, Mapping)
    ]
    return {
        "prior_card_id": card["prior_card_id"],
        "canonical_surface": card["canonical_surface"],
        "stable_surfaces": deepcopy(card["stable_surfaces"]),
        "effective_claims": deepcopy(card["effective_claims"]),
        "disputed_claims": disputes,
    }


def _glossary_card_model_view(card: Mapping[str, Any]) -> dict[str, Any]:
    return {
        key: deepcopy(card[key])
        for key in (
            "glossary_card_id",
            "surface",
            "stable_surfaces",
            "category_claim",
            "local_sense",
            "preferred_rendering_vi",
            "render_policy",
            "lifecycle_state",
        )
    }


def select_prior_cards_for_chapter(
    *,
    chapter: Mapping[str, Any],
    prior_cards: Sequence[Mapping[str, Any]] | None,
) -> list[dict[str, Any]]:
    if prior_cards is None:
        return []
    if isinstance(prior_cards, (str, bytes)) or not isinstance(prior_cards, Sequence):
        raise B0PriorChallengeError("prior_cards must be a sequence")
    blocks = _source_blocks(chapter)
    rows = [_validate_prior_card(card) for card in prior_cards]
    selected = [
        card
        for card in rows
        if any(
            _contains_surface(block["text"], surface)
            for surface in card["stable_surfaces"]
            for block in blocks
        )
    ]
    selected.sort(key=lambda row: row["prior_card_id"])
    if len(selected) > MAX_PRIOR_CARDS:
        raise B0PriorChallengeError("current chapter prior-card matches exceed cap")
    return selected


def _validate_candidate_only_context_card(raw: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(raw, Mapping):
        raise B0PriorChallengeError("candidate-only card must be an object")
    _exact_keys(
        raw,
        {
            "prior_card_id",
            "canonical_surface",
            "stable_surfaces",
            "effective_claims",
            "disputed_claims",
            "authority_scope",
            "first_supported_block_id",
            "provenance_refs",
            "source_candidate_id",
            "context_card_hash",
        },
        "candidate-only card",
    )
    body = dict(raw)
    observed = _required_string(body.pop("context_card_hash", None), "context_card_hash")
    if canonical_hash(body) != observed:
        raise B0PriorChallengeError("candidate-only card hash mismatch")
    if raw.get("authority_scope") != "candidate_only":
        raise B0PriorChallengeError("candidate-only card has unsafe authority")
    surfaces = _string_list(
        raw.get("stable_surfaces"), "candidate stable_surfaces", minimum=1
    )
    if _normalized_surface(raw.get("canonical_surface")) not in {
        _normalized_surface(surface) for surface in surfaces
    }:
        raise B0PriorChallengeError(
            "candidate canonical surface is absent from stable_surfaces"
        )
    claims = raw.get("effective_claims")
    if not isinstance(claims, Mapping) or set(claims) != {
        "referent_kind",
        "referential_gender",
        "identity_summary",
    }:
        raise B0PriorChallengeError("candidate effective_claims is malformed")
    kind = claims.get("referent_kind")
    if kind is not None and kind not in REFERENT_KINDS:
        raise B0PriorChallengeError("candidate referent_kind is invalid")
    gender = claims.get("referential_gender")
    if gender is not None and gender not in REFERENTIAL_GENDERS:
        raise B0PriorChallengeError("candidate referential_gender is invalid")
    disputes = raw.get("disputed_claims")
    if not isinstance(disputes, list):
        raise B0PriorChallengeError("candidate disputed_claims must be a list")
    return deepcopy(dict(raw))


def validate_candidate_only_context_cards(
    cards: Sequence[Mapping[str, Any]] | None,
) -> list[dict[str, Any]]:
    if cards is None:
        return []
    if isinstance(cards, (str, bytes)) or not isinstance(cards, Sequence):
        raise B0PriorChallengeError("candidate-only cards must be a sequence")
    if len(cards) > MAX_CANDIDATE_ONLY_CARDS:
        raise B0PriorChallengeError("candidate-only cards exceed the bounded cap")
    rows = [_validate_candidate_only_context_card(raw) for raw in cards]
    rows.sort(key=lambda row: row["prior_card_id"])
    if len({row["prior_card_id"] for row in rows}) != len(rows):
        raise B0PriorChallengeError("candidate-only cards contain duplicate ids")
    return rows


def build_candidate_only_packets(
    *,
    chapter: Mapping[str, Any],
    candidate_only_cards: Sequence[Mapping[str, Any]] | None,
) -> tuple[list[dict[str, Any]], str]:
    blocks = _source_blocks(chapter)
    if candidate_only_cards is None:
        cards: list[dict[str, Any]] = []
    elif isinstance(candidate_only_cards, (str, bytes)) or not isinstance(
        candidate_only_cards, Sequence
    ):
        raise B0PriorChallengeError("candidate-only cards must be a sequence")
    else:
        cards = [
            _validate_candidate_only_context_card(raw)
            for raw in candidate_only_cards
        ]
    cards.sort(key=lambda row: row["prior_card_id"])
    if len({row["prior_card_id"] for row in cards}) != len(cards):
        raise B0PriorChallengeError("candidate-only cards contain duplicate ids")
    packets: list[dict[str, Any]] = []
    for card in cards:
        hits: list[dict[str, Any]] = []
        for surface in card["stable_surfaces"]:
            block_ids = [
                row["block_id"]
                for row in blocks
                if _contains_surface(row["text"], surface)
            ]
            if block_ids:
                hits.append({"surface": surface, "current_block_ids": block_ids})
        if hits:
            packets.append(
                {
                    "candidate_only_card": _candidate_only_card_model_view(card),
                    "current_surface_hits": hits,
                }
            )
    if len(packets) > MAX_CANDIDATE_ONLY_CARDS:
        raise B0PriorChallengeError(
            "current chapter candidate-only matches exceed the bounded cap"
        )
    body = {
        "schema_version": CANDIDATE_ONLY_PACKET_SCHEMA_VERSION,
        "chapter_id": _required_string(chapter.get("chapter_id"), "chapter_id"),
        "packets": packets,
    }
    return packets, canonical_hash(body)


def _validate_glossary_context_card(raw: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(raw, Mapping):
        raise B0PriorChallengeError("glossary context card must be an object")
    expected = {
        "glossary_card_id",
        "surface",
        "stable_surfaces",
        "category_claim",
        "local_sense",
        "preferred_rendering_vi",
        "render_policy",
        "lifecycle_state",
        "authority_scope",
        "first_supported_block_id",
        "provenance_refs",
        "source_candidate_id",
        "cross_chapter_dispositions",
        "hearing_count",
        "same_evidence_reopen_forbidden",
        "glossary_card_hash",
    }
    _exact_keys(raw, expected, "glossary context card")
    body = dict(raw)
    observed = _required_string(body.pop("glossary_card_hash", None), "glossary_card_hash")
    if canonical_hash(body) != observed:
        raise B0PriorChallengeError("glossary context card hash mismatch")
    lifecycle = _enum(
        raw.get("lifecycle_state"),
        GLOSSARY_LIFECYCLE_STATES,
        "glossary lifecycle_state",
    )
    expected_authority = {
        "chapter_confirmed": "chapter_confirmed_prefix",
        "pending_evidence": "candidate_only",
        "rejected_dormant": "dormant",
    }[lifecycle]
    if raw.get("authority_scope") != expected_authority:
        raise B0PriorChallengeError("glossary lifecycle and authority disagree")
    surfaces = _string_list(
        raw.get("stable_surfaces"), "glossary stable_surfaces", minimum=1, maximum=4
    )
    surface = _required_string(raw.get("surface"), "glossary surface")
    if _normalized_surface(surface) not in {
        _normalized_surface(value) for value in surfaces
    }:
        raise B0PriorChallengeError("glossary surface is absent from stable_surfaces")
    _enum(raw.get("category_claim"), GLOSSARY_CATEGORIES, "glossary category_claim")
    _required_string(raw.get("local_sense"), "glossary local_sense")
    if raw.get("preferred_rendering_vi") is not None:
        _required_string(raw.get("preferred_rendering_vi"), "preferred_rendering_vi")
    render_policy = raw.get("render_policy")
    if render_policy not in {"advisory_meaning", "none"}:
        raise B0PriorChallengeError("glossary render_policy is invalid")
    if lifecycle == "chapter_confirmed":
        if render_policy != "advisory_meaning":
            raise B0PriorChallengeError(
                "confirmed glossary must use advisory_meaning"
            )
    elif raw.get("preferred_rendering_vi") is not None or render_policy != "none":
        raise B0PriorChallengeError(
            "non-confirmed glossary cannot carry rendering guidance"
        )
    refs = raw.get("provenance_refs")
    if not isinstance(refs, list) or not refs:
        raise B0PriorChallengeError("glossary provenance_refs must be non-empty")
    return deepcopy(dict(raw))


def build_glossary_packets(
    *,
    chapter: Mapping[str, Any],
    glossary_cards: Sequence[Mapping[str, Any]] | None,
) -> tuple[list[dict[str, Any]], str]:
    blocks = _source_blocks(chapter)
    if glossary_cards is None:
        cards: list[dict[str, Any]] = []
    elif isinstance(glossary_cards, (str, bytes)) or not isinstance(
        glossary_cards, Sequence
    ):
        raise B0PriorChallengeError("glossary cards must be a sequence")
    else:
        cards = [_validate_glossary_context_card(raw) for raw in glossary_cards]
    cards.sort(key=lambda row: row["glossary_card_id"])
    if len({row["glossary_card_id"] for row in cards}) != len(cards):
        raise B0PriorChallengeError("glossary cards contain duplicate ids")
    packets: list[dict[str, Any]] = []
    for card in cards:
        hits: list[dict[str, Any]] = []
        for surface in card["stable_surfaces"]:
            block_ids = [
                row["block_id"]
                for row in blocks
                if _contains_surface(row["text"], surface)
            ]
            if block_ids:
                hits.append({"surface": surface, "current_block_ids": block_ids})
        if hits:
            packets.append(
                {
                    "glossary_card": _glossary_card_model_view(card),
                    "current_surface_hits": hits,
                }
            )
    if len(packets) > MAX_GLOSSARY_CARDS:
        raise B0PriorChallengeError(
            "current chapter glossary matches exceed the bounded cap"
        )
    body = {
        "schema_version": GLOSSARY_PACKET_SCHEMA_VERSION,
        "chapter_id": _required_string(chapter.get("chapter_id"), "chapter_id"),
        "packets": packets,
    }
    return packets, canonical_hash(body)


def build_prior_packets(
    *,
    chapter: Mapping[str, Any],
    prior_cards: Sequence[Mapping[str, Any]] | None,
) -> tuple[list[dict[str, Any]], str]:
    blocks = _source_blocks(chapter)
    cards = validate_prior_cards(prior_cards)
    packets: list[dict[str, Any]] = []
    for card in cards:
        hits: list[dict[str, Any]] = []
        for surface in card["stable_surfaces"]:
            block_ids = [
                row["block_id"]
                for row in blocks
                if _contains_surface(row["text"], surface)
            ]
            if block_ids:
                hits.append({"surface": surface, "current_block_ids": block_ids})
        if not hits:
            raise B0PriorChallengeError(
                f"prior card {card['prior_card_id']} has no exact current surface hit"
            )
        packets.append(
            {
                "prior_card": _prior_card_model_view(card),
                "current_surface_hits": hits,
            }
        )
    body = {
        "schema_version": PRIOR_PACKET_SCHEMA_VERSION,
        "chapter_id": _required_string(chapter.get("chapter_id"), "chapter_id"),
        "packets": packets,
    }
    return packets, canonical_hash(body)


def _review_case_packet_input(
    *,
    chapter_id: str,
    relevant_review_cases: Mapping[str, Any] | None,
) -> tuple[dict[str, Any], str]:
    if relevant_review_cases is None:
        body = {
            "schema_version": REVIEW_CASE_PACKET_SCHEMA_VERSION,
            "chapter_id": chapter_id,
            "review_case_ledger_hash": None,
            "packets": [],
            "overflow_count": 0,
        }
        return body, canonical_hash(body)
    if relevant_review_cases.get("schema_version") != REVIEW_CASE_PACKET_SCHEMA_VERSION:
        raise B0PriorChallengeError("foreign relevant review-case packet schema")
    verified = deepcopy(dict(relevant_review_cases))
    observed = _required_string(
        verified.pop("review_case_manifest_hash", None),
        "review_case_manifest_hash",
    )
    if canonical_hash(verified) != observed:
        raise B0PriorChallengeError("relevant review-case packet hash mismatch")
    if verified.get("chapter_id") != chapter_id:
        raise B0PriorChallengeError("relevant review-case packet targets another chapter")
    rows = verified.get("packets")
    if not isinstance(rows, list):
        raise B0PriorChallengeError("relevant review-case packets must be a list")
    seen: set[str] = set()
    for row in rows:
        if not isinstance(row, Mapping):
            raise B0PriorChallengeError("relevant review-case row must be an object")
        case_id = _required_string(row.get("review_case_id"), "review_case_id")
        if case_id in seen:
            raise B0PriorChallengeError("relevant review-case packet repeats a case")
        seen.add(case_id)
        row_body = dict(row)
        packet_hash = _required_string(row_body.pop("packet_hash", None), "packet_hash")
        if canonical_hash(row_body) != packet_hash:
            raise B0PriorChallengeError("relevant review-case row hash mismatch")
        _string_list(
            row.get("current_surface_hit_block_ids"),
            "current_surface_hit_block_ids",
            minimum=1,
        )
    verified["review_case_manifest_hash"] = observed
    return deepcopy(verified), verified["review_case_manifest_hash"]


def prior_challenge_response_schema() -> dict[str, Any]:
    base = deepcopy(entity_inventory_response_schema())
    block_ids = {
        "type": "array",
        "items": {"type": "string", "minLength": 1},
        "minItems": 1,
        "maxItems": 3,
        "uniqueItems": True,
    }
    review_case_block_ids = {
        **block_ids,
        "maxItems": MAX_MODEL_REVIEW_CASE_SOURCE_BLOCK_IDS,
    }
    return {
        "type": "object",
        "additionalProperties": False,
        "required": [
            "new_entity_candidates",
            "new_glossary_candidates",
            "unresolved_referents",
            "prior_enrichment_requests",
            "prior_card_dispositions",
            "candidate_only_observations",
            "review_case_observations",
            "prior_glossary_dispositions",
            "chapter_priority_order",
        ],
        "properties": {
            "new_entity_candidates": base["properties"]["entity_candidates"],
            "new_glossary_candidates": base["properties"]["glossary_candidates"],
            "unresolved_referents": base["properties"]["unresolved_referents"],
            "prior_enrichment_requests": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": [
                        "prior_card_id",
                        "surface",
                        "name_class",
                        "source_block_ids",
                        "reason",
                    ],
                    "properties": {
                        "prior_card_id": {"type": "string", "minLength": 1},
                        "surface": {"type": "string", "minLength": 1},
                        "name_class": {
                            "type": "string",
                            "enum": sorted(NAME_CLASSES),
                        },
                        "source_block_ids": block_ids,
                        "reason": {"type": "string", "minLength": 1},
                    },
                },
            },
            "prior_card_dispositions": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": [
                        "prior_card_id",
                        "verdict",
                        "referent_continuity",
                        "issue_code",
                        "disputed_field",
                        "source_block_ids",
                        "reason",
                    ],
                    "properties": {
                        "prior_card_id": {"type": "string", "minLength": 1},
                        "verdict": {
                            "type": "string",
                            "enum": ["challenge", "compatible", "uncertain"],
                        },
                        "referent_continuity": {
                            "type": "string",
                            "enum": sorted(REFERENT_CONTINUITIES),
                        },
                        "issue_code": {
                            "anyOf": [
                                {"type": "string", "enum": sorted(ISSUE_TO_FIELD)},
                                {"type": "null"},
                            ],
                        },
                        "disputed_field": {
                            "anyOf": [
                                {
                                    "type": "string",
                                    "enum": sorted(set(ISSUE_TO_FIELD.values())),
                                },
                                {"type": "null"},
                            ],
                        },
                        "source_block_ids": block_ids,
                        "reason": {
                            "anyOf": [
                                {"type": "string", "minLength": 1},
                                {"type": "null"},
                            ],
                        },
                    },
                },
            },
            "candidate_only_observations": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": [
                        "prior_card_id",
                        "observation",
                        "disputed_field",
                        "source_block_ids",
                        "reason",
                    ],
                    "properties": {
                        "prior_card_id": {"type": "string", "minLength": 1},
                        "observation": {
                            "type": "string",
                            "enum": [
                                "new_claim_evidence",
                                "possible_collision",
                                "supports_continuity",
                            ],
                        },
                        "disputed_field": {
                            "anyOf": [
                                {
                                    "type": "string",
                                    "enum": sorted(CANDIDATE_CLAIM_EVIDENCE_FIELDS),
                                },
                                {"type": "null"},
                            ]
                        },
                        "source_block_ids": block_ids,
                        "reason": {"type": "string", "minLength": 1},
                    },
                },
            },
            "review_case_observations": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": [
                        "review_case_id",
                        "observation",
                        "source_block_ids",
                        "reason",
                    ],
                    "properties": {
                        "review_case_id": {"type": "string", "minLength": 1},
                        "observation": {
                            "type": "string",
                            "enum": [
                                "supports",
                                "conflicts",
                                "not_same_referent",
                                "ambiguous",
                                "no_new_evidence",
                            ],
                        },
                        "source_block_ids": review_case_block_ids,
                        "reason": {"type": "string", "minLength": 1},
                    },
                },
            },
            "prior_glossary_dispositions": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": [
                        "glossary_card_id",
                        "verdict",
                        "source_block_ids",
                        "reason",
                    ],
                    "properties": {
                        "glossary_card_id": {"type": "string", "minLength": 1},
                        "verdict": {
                            "type": "string",
                            "enum": ["compatible", "challenge", "uncertain"],
                        },
                        "source_block_ids": block_ids,
                        "reason": {
                            "anyOf": [
                                {"type": "string", "minLength": 1},
                                {"type": "null"},
                            ]
                        },
                    },
                },
            },
            "chapter_priority_order": priority_schema(
                item_classes=PRIORITY_ITEM_CLASSES
            ),
        },
    }


def _load_prompt(design_doc: Path) -> str:
    prompt = load_system_prompt_from_design(Path(design_doc), PROMPT_ID)
    encoded = prompt.encode("utf-8")
    if len(encoded) != PROMPT_UTF8_BYTES or sha256(encoded).hexdigest() != PROMPT_SHA256:
        raise B0PriorChallengeError("prior-challenge prompt bytes differ from review")
    return prompt


def render_prior_challenge_request(
    *,
    chapter: Mapping[str, Any],
    prior_cards: Sequence[Mapping[str, Any]] | None,
    candidate_only_cards: Sequence[Mapping[str, Any]] | None = None,
    glossary_cards: Sequence[Mapping[str, Any]] | None = None,
    relevant_review_cases: Mapping[str, Any] | None = None,
    design_doc: Path,
    model_id: str = "gpt-5.4",
    reasoning_effort: str = "none",
    temperature: float = 1.0,
    seed: int = 20260715,
    max_output_tokens: int = 4096,
) -> RenderedRegistryRequestV4:
    chapter_id = _required_string(chapter.get("chapter_id"), "chapter_id")
    prompt = _load_prompt(design_doc)
    packets, prior_manifest_hash = build_prior_packets(
        chapter=chapter, prior_cards=prior_cards
    )
    candidate_packets, candidate_manifest_hash = build_candidate_only_packets(
        chapter=chapter, candidate_only_cards=candidate_only_cards
    )
    glossary_packets, glossary_manifest_hash = build_glossary_packets(
        chapter=chapter, glossary_cards=glossary_cards
    )
    review_case_manifest, review_case_manifest_hash = _review_case_packet_input(
        chapter_id=chapter_id,
        relevant_review_cases=relevant_review_cases,
    )
    model_sections = {
        "source_blocks": _source_blocks_model_view(chapter),
        "supplied_prior_packets": [
            _surface_packet_model_view(row, card_key="prior_card") for row in packets
        ],
        "candidate_only_surface_hits": [
            _surface_packet_model_view(row, card_key="candidate_only_card")
            for row in candidate_packets
        ],
        "supplied_glossary_packets": [
            _surface_packet_model_view(row, card_key="glossary_card")
            for row in glossary_packets
        ],
        "relevant_review_cases": _review_case_manifest_model_view(
            review_case_manifest
        ),
    }
    sections = {
        **model_sections,
        "prior_manifest_hash": prior_manifest_hash,
        "candidate_only_manifest_hash": candidate_manifest_hash,
        "glossary_manifest_hash": glossary_manifest_hash,
        "review_case_manifest_hash": review_case_manifest_hash,
    }
    payload = {
        "schema_version": EXPERIMENT_SCHEMA_VERSION,
        "role": "b0_entity_prior_challenge_experiment",
        "chapter_id": chapter_id,
        "allowlisted_sections": model_sections,
    }
    schema = prior_challenge_response_schema()
    model_contract = {
        "model_id": model_id,
        "reasoning_effort": reasoning_effort,
        "temperature": temperature,
        "seed": seed,
        "max_output_tokens": max_output_tokens,
    }
    fingerprint = canonical_hash(
        {
            "schema_version": EXPERIMENT_SCHEMA_VERSION,
            "chapter_id": chapter_id,
            "prompt_id": PROMPT_ID,
            "prompt_sha256": PROMPT_SHA256,
            "response_schema_hash": canonical_hash(schema),
            "model_contract": model_contract,
            "sections_hash": canonical_hash(sections),
        }
    )
    return RenderedRegistryRequestV4(
        role="b0_prior_challenge",
        prompt_id=PROMPT_ID,
        prompt_sha256=PROMPT_SHA256,
        response_schema_hash=canonical_hash(schema),
        chapter_id=chapter_id,
        window_id=None,
        parent_working_revision_hash=None,
        sections=sections,
        messages=(
            {"role": "system", "content": prompt},
            {"role": "user", "content": canonical_json(payload)},
        ),
        request_fingerprint=fingerprint,
    )


def _current_block_ids(chapter: Mapping[str, Any]) -> set[str]:
    return {row["block_id"] for row in _source_blocks(chapter)}


def _validated_support_ids(
    value: Any, *, chapter: Mapping[str, Any], label: str
) -> list[str]:
    rows = _string_list(value, label, minimum=1, maximum=3)
    foreign = sorted(set(rows) - _current_block_ids(chapter))
    if foreign:
        raise B0PriorChallengeError(f"{label} cites foreign blocks {foreign}")
    return rows


def _normalized_compatible_support_ids(
    value: Any,
    *,
    chapter: Mapping[str, Any],
    label: str,
    hit_block_ids: set[str],
) -> tuple[list[str], int]:
    rows = _string_list(value, label, minimum=1)
    foreign = sorted(set(rows) - _current_block_ids(chapter))
    if foreign:
        raise B0PriorChallengeError(f"{label} cites foreign blocks {foreign}")
    order_by_id = {
        row["block_id"]: index for index, row in enumerate(_source_blocks(chapter))
    }
    hit_rows = sorted(
        (block_id for block_id in rows if block_id in hit_block_ids),
        key=order_by_id.__getitem__,
    )
    if not hit_rows:
        available_hits = sorted(
            hit_block_ids,
            key=lambda block_id: (
                min(
                    abs(order_by_id[block_id] - order_by_id[cited_block_id])
                    for cited_block_id in rows
                ),
                order_by_id[block_id],
            ),
        )
        if not available_hits:
            raise B0PriorChallengeError(
                f"{label} has no code-derived surface hit"
            )
        # Compatibility is the model judgment; locating its nearest literal
        # witness is deterministic bookkeeping over the packet code supplied.
        replacement_count = min(2, max(1, len(rows)))
        return available_hits[:replacement_count], len(rows)
    contextual_rows = sorted(
        (block_id for block_id in rows if block_id not in hit_block_ids),
        key=order_by_id.__getitem__,
    )
    normalized = (hit_rows + contextual_rows)[:2]
    return normalized, len(rows) - len(normalized)


def _normalized_candidate_observation_support_ids(
    value: Any,
    *,
    chapter: Mapping[str, Any],
    label: str,
    hit_block_ids: set[str],
) -> tuple[list[str], int, int]:
    """Close non-authoritative evidence over one deterministic literal hit."""

    rows = _validated_support_ids(value, chapter=chapter, label=label)
    if set(rows).intersection(hit_block_ids):
        return rows, 0, 0
    order_by_id = {
        row["block_id"]: index for index, row in enumerate(_source_blocks(chapter))
    }
    available_hits = sorted(
        hit_block_ids,
        key=lambda block_id: (
            min(
                abs(order_by_id[block_id] - order_by_id[cited_block_id])
                for cited_block_id in rows
            ),
            order_by_id[block_id],
        ),
    )
    if not available_hits:
        raise B0PriorChallengeError(f"{label} has no code-derived surface hit")
    selected_hit = available_hits[0]
    contextual_rows = sorted(
        rows,
        key=lambda block_id: (
            abs(order_by_id[block_id] - order_by_id[selected_hit]),
            order_by_id[block_id],
        ),
    )[:2]
    normalized = sorted(
        {selected_hit, *contextual_rows},
        key=order_by_id.__getitem__,
    )
    return normalized, 1, len(rows) - len(contextual_rows)


def _normalized_review_case_support_ids(
    value: Any,
    *,
    chapter: Mapping[str, Any],
    label: str,
    hit_block_ids: set[str],
) -> tuple[list[str], int]:
    """Bound valid review evidence without making a semantic selection."""

    rows = _string_list(
        value,
        label,
        minimum=1,
        maximum=MAX_MODEL_REVIEW_CASE_SOURCE_BLOCK_IDS,
    )
    foreign = sorted(set(rows) - _current_block_ids(chapter))
    if foreign:
        raise B0PriorChallengeError(f"{label} cites foreign blocks {foreign}")
    order_by_id = {
        row["block_id"]: index for index, row in enumerate(_source_blocks(chapter))
    }
    ordered = sorted(rows, key=order_by_id.__getitem__)
    ordered_hits = [block_id for block_id in ordered if block_id in hit_block_ids]
    if not ordered_hits:
        raise B0PriorChallengeError(
            "review-case observation cites no supplied surface-hit block"
        )
    if len(ordered) <= MAX_PERSISTED_REVIEW_CASE_SOURCE_BLOCK_IDS:
        return ordered, 0

    # Preserve the cited span and at least one code-supplied literal hit. The
    # raw provider response retains every cited id for later audit.
    selected = {ordered[0], ordered[-1], ordered_hits[0]}
    if len(selected) < MAX_PERSISTED_REVIEW_CASE_SOURCE_BLOCK_IDS:
        selected.add(ordered[len(ordered) // 2])
    if len(selected) < MAX_PERSISTED_REVIEW_CASE_SOURCE_BLOCK_IDS:
        selected.update(ordered[:MAX_PERSISTED_REVIEW_CASE_SOURCE_BLOCK_IDS])
    normalized = sorted(selected, key=order_by_id.__getitem__)[
        :MAX_PERSISTED_REVIEW_CASE_SOURCE_BLOCK_IDS
    ]
    return normalized, len(ordered) - len(normalized)


def validate_prior_challenge_response(
    response: Mapping[str, Any],
    *,
    chapter: Mapping[str, Any],
    prior_cards: Sequence[Mapping[str, Any]] | None,
    candidate_only_cards: Sequence[Mapping[str, Any]] | None = None,
    glossary_cards: Sequence[Mapping[str, Any]] | None = None,
    relevant_review_cases: Mapping[str, Any] | None = None,
    request_fingerprint: str,
) -> dict[str, Any]:
    if not isinstance(response, Mapping):
        raise B0PriorChallengeError("prior-challenge response must be an object")
    _exact_keys(
        response,
        {
            "new_entity_candidates",
            "new_glossary_candidates",
            "unresolved_referents",
            "prior_enrichment_requests",
            "prior_card_dispositions",
            "candidate_only_observations",
            "review_case_observations",
            "prior_glossary_dispositions",
            "chapter_priority_order",
        },
        "prior-challenge response",
    )
    packets, prior_manifest_hash = build_prior_packets(
        chapter=chapter, prior_cards=prior_cards
    )
    candidate_packets, candidate_manifest_hash = build_candidate_only_packets(
        chapter=chapter, candidate_only_cards=candidate_only_cards
    )
    glossary_packets, glossary_manifest_hash = build_glossary_packets(
        chapter=chapter, glossary_cards=glossary_cards
    )
    review_case_manifest, review_case_manifest_hash = _review_case_packet_input(
        chapter_id=_required_string(chapter.get("chapter_id"), "chapter_id"),
        relevant_review_cases=relevant_review_cases,
    )
    review_case_packets = review_case_manifest["packets"]
    cards = {row["prior_card"]["prior_card_id"]: row["prior_card"] for row in packets}
    base_inventory = validate_entity_inventory_response(
        {
            "entity_candidates": response.get("new_entity_candidates"),
            "glossary_candidates": response.get("new_glossary_candidates"),
            "unresolved_referents": response.get("unresolved_referents"),
            "chapter_priority_order": [
                deepcopy(row)
                for row in response.get("chapter_priority_order") or []
                if isinstance(row, Mapping)
                and row.get("item_class")
                in {"new_entity", "new_glossary", "unresolved"}
            ],
        },
        chapter,
        request_fingerprint=request_fingerprint,
    )

    raw_enrichments = response.get("prior_enrichment_requests")
    if not isinstance(raw_enrichments, list):
        raise B0PriorChallengeError("prior_enrichment_requests must be a list")
    enrichments: list[dict[str, Any]] = []
    seen_enrichment: set[tuple[str, str]] = set()
    block_text = {row["block_id"]: row["text"] for row in _source_blocks(chapter)}
    for raw in raw_enrichments:
        if not isinstance(raw, Mapping):
            raise B0PriorChallengeError("prior enrichment must be an object")
        _exact_keys(
            raw,
            {"prior_card_id", "surface", "name_class", "source_block_ids", "reason"},
            "prior enrichment",
        )
        prior_card_id = _required_string(raw.get("prior_card_id"), "prior_card_id")
        if prior_card_id not in cards:
            raise B0PriorChallengeError("prior enrichment cites a foreign prior card")
        surface = _required_string(raw.get("surface"), "enrichment surface")
        if _normalized_surface(surface) in {
            _normalized_surface(item) for item in cards[prior_card_id]["stable_surfaces"]
        }:
            raise B0PriorChallengeError("prior enrichment repeats an existing stable surface")
        source_block_ids = _validated_support_ids(
            raw.get("source_block_ids"), chapter=chapter, label="enrichment source_block_ids"
        )
        if not all(surface in block_text[block_id] for block_id in source_block_ids):
            raise B0PriorChallengeError("enrichment surface is absent from a cited block")
        key = (prior_card_id, _normalized_surface(surface))
        if key in seen_enrichment:
            raise B0PriorChallengeError("duplicate prior enrichment")
        seen_enrichment.add(key)
        enrichments.append(
            {
                "prior_card_id": prior_card_id,
                "surface": surface,
                "name_class": _enum(raw.get("name_class"), NAME_CLASSES, "name_class"),
                "source_block_ids": source_block_ids,
                "reason": _required_string(raw.get("reason"), "enrichment reason"),
            }
        )

    raw_dispositions = response.get("prior_card_dispositions")
    if not isinstance(raw_dispositions, list):
        raise B0PriorChallengeError("prior_card_dispositions must be a list")
    dispositions: list[dict[str, Any]] = []
    seen_dispositions: set[str] = set()
    normalized_compatible_entity_disposition_count = 0
    omitted_compatible_entity_source_block_count = 0
    normalized_missing_entity_disposition_reason_count = 0
    hit_blocks_by_card = {
        row["prior_card"]["prior_card_id"]: {
            block_id
            for hit in row["current_surface_hits"]
            for block_id in hit["current_block_ids"]
        }
        for row in packets
    }
    for raw in raw_dispositions:
        if not isinstance(raw, Mapping):
            raise B0PriorChallengeError("prior card disposition must be an object")
        _exact_keys(
            raw,
            {
                "prior_card_id",
                "verdict",
                "referent_continuity",
                "issue_code",
                "disputed_field",
                "source_block_ids",
                "reason",
            },
            "prior card disposition",
        )
        prior_card_id = _required_string(raw.get("prior_card_id"), "prior_card_id")
        if prior_card_id not in cards:
            raise B0PriorChallengeError("disposition cites a foreign prior card")
        if prior_card_id in seen_dispositions:
            raise B0PriorChallengeError("duplicate prior card disposition")
        seen_dispositions.add(prior_card_id)
        verdict = _enum(
            raw.get("verdict"), {"compatible", "challenge", "uncertain"}, "verdict"
        )
        referent_continuity = _enum(
            raw.get("referent_continuity"),
            REFERENT_CONTINUITIES,
            "referent_continuity",
        )
        if verdict == "compatible":
            source_block_ids, omitted_count = _normalized_compatible_support_ids(
                raw.get("source_block_ids"),
                chapter=chapter,
                label="disposition source_block_ids",
                hit_block_ids=hit_blocks_by_card[prior_card_id],
            )
            if omitted_count:
                normalized_compatible_entity_disposition_count += 1
                omitted_compatible_entity_source_block_count += omitted_count
        else:
            source_block_ids = _validated_support_ids(
                raw.get("source_block_ids"),
                chapter=chapter,
                label="disposition source_block_ids",
            )
        if not set(source_block_ids).intersection(hit_blocks_by_card[prior_card_id]):
            raise B0PriorChallengeError(
                "disposition does not cite a block containing its retrieved surface"
            )
        issue_code = raw.get("issue_code")
        disputed_field = raw.get("disputed_field")
        reason = raw.get("reason")
        if verdict == "challenge":
            issue_code = _enum(issue_code, ISSUE_TO_FIELD, "issue_code")
            disputed_field = _required_string(disputed_field, "disputed_field")
            if referent_continuity == "uncertain":
                raise B0PriorChallengeError(
                    "challenge disposition cannot use uncertain continuity"
                )
            if referent_continuity == "same_referent":
                if issue_code in IDENTITY_ISSUES:
                    raise B0PriorChallengeError(
                        "same-referent challenge cannot use an identity issue label"
                    )
                if ISSUE_TO_FIELD[issue_code] != disputed_field:
                    raise B0PriorChallengeError(
                        "same-referent challenge issue and field disagree"
                    )
            elif issue_code not in IDENTITY_ISSUES:
                raise B0PriorChallengeError(
                    "possible-collision challenge requires an identity issue label"
                )
            if reason is None:
                reason = "model_omitted_challenge_reason"
                normalized_missing_entity_disposition_reason_count += 1
            else:
                reason = _required_string(reason, "challenge reason")
        elif verdict == "compatible":
            if referent_continuity != "same_referent":
                raise B0PriorChallengeError(
                    "compatible disposition must use same_referent continuity"
                )
            if issue_code is not None or disputed_field is not None or reason is not None:
                raise B0PriorChallengeError(
                    "compatible disposition must omit issue, field, and reason"
                )
        else:
            if referent_continuity != "uncertain":
                raise B0PriorChallengeError(
                    "uncertain disposition must use uncertain continuity"
                )
            if issue_code is not None or disputed_field is not None:
                raise B0PriorChallengeError("uncertain disposition cannot declare a conflict")
            if reason is None:
                reason = "model_omitted_uncertainty_reason"
                normalized_missing_entity_disposition_reason_count += 1
            else:
                reason = _required_string(reason, "uncertain reason")
        dispositions.append(
            {
                "prior_card_id": prior_card_id,
                "verdict": verdict,
                "referent_continuity": referent_continuity,
                "issue_code": issue_code,
                "disputed_field": disputed_field,
                "source_block_ids": source_block_ids,
                "reason": reason,
            }
        )

    if seen_dispositions != set(cards):
        missing = sorted(set(cards) - seen_dispositions)
        foreign = sorted(seen_dispositions - set(cards))
        raise B0PriorChallengeError(
            f"prior dispositions do not exact-cover packets; missing={missing}, foreign={foreign}"
        )

    enrichments.sort(key=lambda row: (row["prior_card_id"], row["surface"]))
    dispositions.sort(key=lambda row: row["prior_card_id"])
    disposition_by_card = {row["prior_card_id"]: row for row in dispositions}
    for enrichment in enrichments:
        if disposition_by_card[enrichment["prior_card_id"]]["verdict"] != "compatible":
            raise B0PriorChallengeError(
                "prior enrichment requires a compatible card disposition"
            )
    tickets = [
        {
            "prior_card_id": row["prior_card_id"],
            "issue_code": row["issue_code"],
            "disputed_field": row["disputed_field"],
            "referent_continuity": row["referent_continuity"],
            "source_block_ids": row["source_block_ids"],
            "reason": row["reason"],
        }
        for row in dispositions
        if row["verdict"] == "challenge"
    ]
    candidate_packet_by_id = {
        row["candidate_only_card"]["prior_card_id"]: row
        for row in candidate_packets
    }
    raw_candidate_observations = response.get("candidate_only_observations")
    if not isinstance(raw_candidate_observations, list):
        raise B0PriorChallengeError("candidate_only_observations must be a list")
    candidate_observations: list[dict[str, Any]] = []
    seen_candidate_observations: set[str] = set()
    normalized_inapplicable_candidate_disputed_field_count = 0
    normalized_candidate_observation_missing_hit_count = 0
    added_candidate_observation_hit_block_count = 0
    omitted_candidate_observation_context_block_count = 0
    downgraded_unowned_candidate_claim_evidence_count = 0
    for raw in raw_candidate_observations:
        if not isinstance(raw, Mapping):
            raise B0PriorChallengeError("candidate-only observation must be an object")
        _exact_keys(
            raw,
            {
                "prior_card_id",
                "observation",
                "disputed_field",
                "source_block_ids",
                "reason",
            },
            "candidate-only observation",
        )
        prior_card_id = _required_string(raw.get("prior_card_id"), "prior_card_id")
        packet = candidate_packet_by_id.get(prior_card_id)
        if packet is None or prior_card_id in seen_candidate_observations:
            raise B0PriorChallengeError(
                "candidate-only observation targets a foreign/duplicate card"
            )
        seen_candidate_observations.add(prior_card_id)
        hit_block_ids = {
            block_id
            for hit in packet["current_surface_hits"]
            for block_id in hit["current_block_ids"]
        }
        (
            source_block_ids,
            added_hit_count,
            omitted_context_count,
        ) = _normalized_candidate_observation_support_ids(
            raw.get("source_block_ids"),
            chapter=chapter,
            label="candidate observation source_block_ids",
            hit_block_ids=hit_block_ids,
        )
        if added_hit_count:
            normalized_candidate_observation_missing_hit_count += 1
            added_candidate_observation_hit_block_count += added_hit_count
            omitted_candidate_observation_context_block_count += omitted_context_count
        observation = _enum(
            raw.get("observation"),
            {"new_claim_evidence", "possible_collision", "supports_continuity"},
            "candidate observation",
        )
        disputed_field = raw.get("disputed_field")
        if observation == "new_claim_evidence":
            disputed_field = _enum(
                disputed_field,
                CANDIDATE_CLAIM_EVIDENCE_FIELDS,
                "candidate disputed field",
            )
            card_disputed_fields = {
                row.get("disputed_field")
                for row in packet["candidate_only_card"]["disputed_claims"]
                if isinstance(row, Mapping)
            }
            if disputed_field not in card_disputed_fields:
                stable_disputed_fields = (
                    card_disputed_fields & CANDIDATE_CLAIM_EVIDENCE_FIELDS
                )
                if stable_disputed_fields:
                    raise B0PriorChallengeError(
                        "candidate claim evidence targets no supplied disputed field"
                    )
                # Identity-routing disputes cannot receive a stable claim
                # update. Retain the evidence as non-authoritative continuity
                # for the identity review path.
                observation = "supports_continuity"
                disputed_field = None
                downgraded_unowned_candidate_claim_evidence_count += 1
        elif disputed_field is not None:
            # Continuity/collision is an identity-routing observation, not a
            # claim-facet update. Preserve its evidence and route while
            # mechanically clearing a structurally inapplicable oversupply.
            disputed_field = None
            normalized_inapplicable_candidate_disputed_field_count += 1
        candidate_observations.append(
            {
                "prior_card_id": prior_card_id,
                "observation": observation,
                "disputed_field": disputed_field,
                "source_block_ids": source_block_ids,
                "reason": _required_string(raw.get("reason"), "candidate reason"),
            }
        )
    candidate_observations.sort(key=lambda row: row["prior_card_id"])

    review_case_by_id = {
        row["review_case_id"]: row for row in review_case_packets
    }
    raw_review_observations = response.get("review_case_observations")
    if not isinstance(raw_review_observations, list):
        raise B0PriorChallengeError("review_case_observations must be a list")
    review_case_observations: list[dict[str, Any]] = []
    seen_review_cases: set[str] = set()
    normalized_review_case_observation_count = 0
    omitted_review_case_source_block_count = 0
    for raw in raw_review_observations:
        if not isinstance(raw, Mapping):
            raise B0PriorChallengeError("review-case observation must be an object")
        _exact_keys(
            raw,
            {"review_case_id", "observation", "source_block_ids", "reason"},
            "review-case observation",
        )
        case_id = _required_string(raw.get("review_case_id"), "review_case_id")
        packet = review_case_by_id.get(case_id)
        if packet is None or case_id in seen_review_cases:
            raise B0PriorChallengeError(
                "review-case observation targets a foreign/duplicate case"
            )
        seen_review_cases.add(case_id)
        source_block_ids, omitted_source_block_count = (
            _normalized_review_case_support_ids(
                raw.get("source_block_ids"),
                chapter=chapter,
                label="review-case source_block_ids",
                hit_block_ids=set(packet["current_surface_hit_block_ids"]),
            )
        )
        if omitted_source_block_count:
            normalized_review_case_observation_count += 1
            omitted_review_case_source_block_count += omitted_source_block_count
        allowed_hits = set(packet["current_surface_hit_block_ids"])
        if not set(source_block_ids).intersection(allowed_hits):
            raise B0PriorChallengeError(
                "review-case observation cites no supplied surface-hit block"
            )
        observation = _enum(
            raw.get("observation"),
            {
                "supports",
                "conflicts",
                "not_same_referent",
                "ambiguous",
                "no_new_evidence",
            },
            "review-case observation",
        )
        reason = _required_string(raw.get("reason"), "review-case reason")
        if len(reason) > 800:
            raise B0PriorChallengeError("review-case reason is too long")
        review_case_observations.append(
            {
                "review_case_id": case_id,
                "observation": observation,
                "source_block_ids": source_block_ids,
                "reason": reason,
            }
        )
    review_case_observations.sort(key=lambda row: row["review_case_id"])

    glossary_packet_by_id = {
        row["glossary_card"]["glossary_card_id"]: row for row in glossary_packets
    }
    raw_glossary_dispositions = response.get("prior_glossary_dispositions")
    if not isinstance(raw_glossary_dispositions, list):
        raise B0PriorChallengeError("prior_glossary_dispositions must be a list")
    glossary_dispositions: list[dict[str, Any]] = []
    seen_glossary_dispositions: set[str] = set()
    normalized_compatible_glossary_disposition_count = 0
    omitted_compatible_glossary_source_block_count = 0
    normalized_missing_glossary_disposition_reason_count = 0
    for raw in raw_glossary_dispositions:
        if not isinstance(raw, Mapping):
            raise B0PriorChallengeError("prior glossary disposition must be an object")
        _exact_keys(
            raw,
            {"glossary_card_id", "verdict", "source_block_ids", "reason"},
            "prior glossary disposition",
        )
        glossary_card_id = _required_string(
            raw.get("glossary_card_id"), "glossary_card_id"
        )
        packet = glossary_packet_by_id.get(glossary_card_id)
        if packet is None or glossary_card_id in seen_glossary_dispositions:
            raise B0PriorChallengeError(
                "prior glossary disposition targets a foreign/duplicate card"
            )
        seen_glossary_dispositions.add(glossary_card_id)
        verdict = _enum(
            raw.get("verdict"), {"compatible", "challenge", "uncertain"}, "verdict"
        )
        hit_blocks = {
            block_id
            for hit in packet["current_surface_hits"]
            for block_id in hit["current_block_ids"]
        }
        if verdict == "compatible":
            source_block_ids, omitted_count = _normalized_compatible_support_ids(
                raw.get("source_block_ids"),
                chapter=chapter,
                label="prior glossary source_block_ids",
                hit_block_ids=hit_blocks,
            )
            if omitted_count:
                normalized_compatible_glossary_disposition_count += 1
                omitted_compatible_glossary_source_block_count += omitted_count
        else:
            source_block_ids = _validated_support_ids(
                raw.get("source_block_ids"),
                chapter=chapter,
                label="prior glossary source_block_ids",
            )
        if not set(source_block_ids).intersection(hit_blocks):
            raise B0PriorChallengeError(
                "prior glossary disposition cites no retrieved surface block"
            )
        reason = raw.get("reason")
        if verdict == "compatible":
            if reason is not None:
                raise B0PriorChallengeError(
                    "compatible glossary disposition must use null reason"
                )
        else:
            if reason is None:
                reason = "model_omitted_glossary_disposition_reason"
                normalized_missing_glossary_disposition_reason_count += 1
            else:
                reason = _required_string(
                    reason, "prior glossary disposition reason"
                )
        glossary_dispositions.append(
            {
                "glossary_card_id": glossary_card_id,
                "verdict": verdict,
                "source_block_ids": source_block_ids,
                "reason": reason,
            }
        )
    if seen_glossary_dispositions != set(glossary_packet_by_id):
        raise B0PriorChallengeError(
            "prior glossary dispositions must exact-cover supplied packets"
        )
    glossary_dispositions.sort(key=lambda row: row["glossary_card_id"])

    priority_targets: list[dict[str, Any]] = []
    for packet in packets:
        for hit in packet["current_surface_hits"]:
            priority_targets.append(
                make_priority_target(
                    item_class="prior_entity",
                    ref_id=packet["prior_card"]["prior_card_id"],
                    surface=hit["surface"],
                    block_ids=hit["current_block_ids"],
                )
            )
    for packet in candidate_packets:
        for hit in packet["current_surface_hits"]:
            priority_targets.append(
                make_priority_target(
                    item_class="candidate_only_entity",
                    ref_id=packet["candidate_only_card"]["prior_card_id"],
                    surface=hit["surface"],
                    block_ids=hit["current_block_ids"],
                )
            )
    for packet in glossary_packets:
        for hit in packet["current_surface_hits"]:
            priority_targets.append(
                make_priority_target(
                    item_class="prior_glossary",
                    ref_id=packet["glossary_card"]["glossary_card_id"],
                    surface=hit["surface"],
                    block_ids=hit["current_block_ids"],
                )
            )
    for row in base_inventory["entity_candidates"]:
        for location in row.get("name_locations") or []:
            if isinstance(location, Mapping):
                priority_targets.append(
                    make_priority_target(
                        item_class="new_entity",
                        ref_id=row["candidate_id"],
                        surface=str(location.get("surface") or ""),
                        block_ids=list(location.get("surface_match_block_ids") or []),
                    )
                )
    for item_class, table in (
        ("new_glossary", "glossary_candidates"),
        ("unresolved", "unresolved_referents"),
    ):
        for row in base_inventory[table]:
            priority_targets.append(
                make_priority_target(
                    item_class=item_class,
                    ref_id=row["candidate_id"],
                    surface=row["surface"],
                    block_ids=list(row.get("surface_match_block_ids") or []),
                )
            )
    chapter_priority_order, priority_issues = validate_priority_order(
        response.get("chapter_priority_order"),
        chapter_blocks=_source_blocks(chapter),
        targets=priority_targets,
        allowed_item_classes=PRIORITY_ITEM_CLASSES,
    )

    body = {
        "schema_version": EXPERIMENT_SCHEMA_VERSION,
        "chapter_id": _required_string(chapter.get("chapter_id"), "chapter_id"),
        "request_fingerprint": _required_string(
            request_fingerprint, "request_fingerprint"
        ),
        "prior_manifest_hash": prior_manifest_hash,
        "candidate_only_manifest_hash": candidate_manifest_hash,
        "glossary_manifest_hash": glossary_manifest_hash,
        "review_case_manifest_hash": review_case_manifest_hash,
        "code_derived_prior_presence": [
            {
                "prior_card_id": row["prior_card"]["prior_card_id"],
                "current_surface_hits": row["current_surface_hits"],
            }
            for row in packets
        ],
        "code_derived_glossary_presence": [
            {
                "glossary_card_id": row["glossary_card"]["glossary_card_id"],
                "current_surface_hits": row["current_surface_hits"],
            }
            for row in glossary_packets
        ],
        "delta_inventory": base_inventory,
        "prior_enrichment_requests": enrichments,
        "prior_card_dispositions": dispositions,
        "prior_conflict_tickets": tickets,
        "candidate_only_observations": candidate_observations,
        "review_case_observations": review_case_observations,
        "prior_glossary_dispositions": glossary_dispositions,
        "chapter_priority_order": chapter_priority_order,
        "validation_report": {
            "prior_packet_count": len(packets),
            "compatible_count": sum(
                row["verdict"] == "compatible" for row in dispositions
            ),
            "challenge_count": sum(row["verdict"] == "challenge" for row in dispositions),
            "uncertain_count": sum(row["verdict"] == "uncertain" for row in dispositions),
            "enrichment_count": len(enrichments),
            "candidate_only_packet_count": len(candidate_packets),
            "candidate_only_observation_count": len(candidate_observations),
            "supplied_review_case_count": len(review_case_packets),
            "review_case_observation_count": len(review_case_observations),
            "normalized_review_case_observation_count": (
                normalized_review_case_observation_count
            ),
            "omitted_review_case_source_block_count": (
                omitted_review_case_source_block_count
            ),
            "normalized_inapplicable_candidate_disputed_field_count": (
                normalized_inapplicable_candidate_disputed_field_count
            ),
            "normalized_candidate_observation_missing_hit_count": (
                normalized_candidate_observation_missing_hit_count
            ),
            "added_candidate_observation_hit_block_count": (
                added_candidate_observation_hit_block_count
            ),
            "omitted_candidate_observation_context_block_count": (
                omitted_candidate_observation_context_block_count
            ),
            "downgraded_unowned_candidate_claim_evidence_count": (
                downgraded_unowned_candidate_claim_evidence_count
            ),
            "prior_glossary_packet_count": len(glossary_packets),
            "prior_glossary_compatible_count": sum(
                row["verdict"] == "compatible" for row in glossary_dispositions
            ),
            "prior_glossary_challenge_count": sum(
                row["verdict"] == "challenge" for row in glossary_dispositions
            ),
            "prior_glossary_uncertain_count": sum(
                row["verdict"] == "uncertain" for row in glossary_dispositions
            ),
            "normalized_compatible_entity_disposition_count": (
                normalized_compatible_entity_disposition_count
            ),
            "omitted_compatible_entity_source_block_count": (
                omitted_compatible_entity_source_block_count
            ),
            "normalized_missing_entity_disposition_reason_count": (
                normalized_missing_entity_disposition_reason_count
            ),
            "normalized_compatible_glossary_disposition_count": (
                normalized_compatible_glossary_disposition_count
            ),
            "omitted_compatible_glossary_source_block_count": (
                omitted_compatible_glossary_source_block_count
            ),
            "normalized_missing_glossary_disposition_reason_count": (
                normalized_missing_glossary_disposition_reason_count
            ),
            "accepted_priority_count": len(chapter_priority_order),
            "priority_issue_count": len(priority_issues),
            "priority_issues": priority_issues,
        },
    }
    return {**body, "prior_challenge_artifact_hash": canonical_hash(body)}


def build_hidden_corruption_manifest(
    *,
    mutation_id: str,
    correct_prior_cards: Sequence[Mapping[str, Any]],
    supplied_prior_cards: Sequence[Mapping[str, Any]],
    expected_issue_code: str,
) -> dict[str, Any]:
    correct = validate_prior_cards(correct_prior_cards)
    supplied = validate_prior_cards(supplied_prior_cards)
    correct_by_id = {row["prior_card_id"]: row for row in correct}
    supplied_by_id = {row["prior_card_id"]: row for row in supplied}
    if set(correct_by_id) != set(supplied_by_id):
        raise B0PriorChallengeError("corruption arm must preserve the prior card id set")
    changed_ids = [
        card_id
        for card_id in sorted(correct_by_id)
        if canonical_json(correct_by_id[card_id]) != canonical_json(supplied_by_id[card_id])
    ]
    if len(changed_ids) != 1:
        raise B0PriorChallengeError("corruption arm must change exactly one prior card")
    changed_id = changed_ids[0]
    changed_fields = sorted(
        field
        for field in correct_by_id[changed_id]
        if canonical_json(correct_by_id[changed_id][field])
        != canonical_json(supplied_by_id[changed_id][field])
    )
    if not changed_fields or not set(changed_fields) <= MUTABLE_TEST_FIELDS:
        raise B0PriorChallengeError("corruption arm changes a protected prior field")
    issue_code = _enum(expected_issue_code, ISSUE_TO_FIELD, "expected_issue_code")
    body = {
        "schema_version": CORRUPTION_MANIFEST_SCHEMA_VERSION,
        "mutation_id": _required_string(mutation_id, "mutation_id"),
        "correct_prior_cards_hash": canonical_hash(correct),
        "supplied_prior_cards_hash": canonical_hash(supplied),
        "changed_prior_card_id": changed_id,
        "changed_card_fields": changed_fields,
        "expected_issue_code": issue_code,
        "expected_disputed_field": ISSUE_TO_FIELD[issue_code],
        "hidden_from_model": True,
    }
    return {**body, "corruption_manifest_hash": canonical_hash(body)}


def build_hidden_corruption_set_manifest(
    *,
    mutation_id: str,
    correct_prior_cards: Sequence[Mapping[str, Any]],
    supplied_prior_cards: Sequence[Mapping[str, Any]],
    expected_outcomes: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    correct = validate_prior_cards(correct_prior_cards)
    supplied = validate_prior_cards(supplied_prior_cards)
    correct_by_id = {row["prior_card_id"]: row for row in correct}
    supplied_by_id = {row["prior_card_id"]: row for row in supplied}
    if set(correct_by_id) != set(supplied_by_id):
        raise B0PriorChallengeError("corruption set must preserve the prior card id set")
    changed_cards: list[dict[str, Any]] = []
    for card_id in sorted(correct_by_id):
        changed_fields = sorted(
            field
            for field in correct_by_id[card_id]
            if canonical_json(correct_by_id[card_id][field])
            != canonical_json(supplied_by_id[card_id][field])
        )
        if not changed_fields:
            continue
        if not set(changed_fields) <= MUTABLE_TEST_FIELDS:
            raise B0PriorChallengeError("corruption set changes a protected prior field")
        changed_cards.append(
            {"prior_card_id": card_id, "changed_card_fields": changed_fields}
        )
    if not changed_cards:
        raise B0PriorChallengeError("corruption set must change at least one prior card")

    outcomes: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for raw in expected_outcomes:
        if not isinstance(raw, Mapping):
            raise B0PriorChallengeError("expected outcome must be an object")
        _exact_keys(
            raw,
            {"prior_card_id", "verdict", "issue_code"},
            "expected outcome",
        )
        card_id = _required_string(raw.get("prior_card_id"), "expected prior_card_id")
        if card_id not in supplied_by_id or card_id in seen_ids:
            raise B0PriorChallengeError("expected outcome has foreign or duplicate card id")
        verdict = _enum(raw.get("verdict"), {"challenge", "uncertain"}, "verdict")
        issue = raw.get("issue_code")
        if verdict == "challenge":
            issue = _enum(issue, ISSUE_TO_FIELD, "expected issue_code")
        elif issue is not None:
            raise B0PriorChallengeError("uncertain expected outcome must use null issue_code")
        outcomes.append(
            {
                "prior_card_id": card_id,
                "verdict": verdict,
                "issue_code": issue,
            }
        )
        seen_ids.add(card_id)
    if not outcomes:
        raise B0PriorChallengeError("corruption set needs expected outcomes")
    outcomes.sort(key=lambda row: row["prior_card_id"])
    body = {
        "schema_version": CORRUPTION_SET_MANIFEST_SCHEMA_VERSION,
        "mutation_id": _required_string(mutation_id, "mutation_id"),
        "correct_prior_cards_hash": canonical_hash(correct),
        "supplied_prior_cards_hash": canonical_hash(supplied),
        "changed_prior_cards": changed_cards,
        "expected_outcomes": outcomes,
        "hidden_from_model": True,
    }
    return {**body, "corruption_manifest_hash": canonical_hash(body)}


def evaluate_hidden_corruption(
    artifact: Mapping[str, Any], corruption_manifest: Mapping[str, Any]
) -> dict[str, Any]:
    artifact_body = dict(artifact)
    artifact_hash = artifact_body.pop("prior_challenge_artifact_hash", None)
    if canonical_hash(artifact_body) != artifact_hash:
        raise B0PriorChallengeError("prior-challenge artifact hash mismatch")
    manifest_body = dict(corruption_manifest)
    observed_hash = manifest_body.pop("corruption_manifest_hash", None)
    if canonical_hash(manifest_body) != observed_hash:
        raise B0PriorChallengeError("corruption manifest hash mismatch")
    if corruption_manifest.get("hidden_from_model") is not True:
        raise B0PriorChallengeError("corruption manifest was not sealed as hidden")
    if (
        corruption_manifest.get("schema_version")
        == CORRUPTION_SET_MANIFEST_SCHEMA_VERSION
    ):
        observed_dispositions = {
            str(row.get("prior_card_id")): {
                "verdict": str(row.get("verdict")),
                "issue_code": row.get("issue_code"),
            }
            for row in artifact.get("prior_card_dispositions") or []
        }
        expected_rows = [
            {
                "prior_card_id": str(row["prior_card_id"]),
                "verdict": str(row["verdict"]),
                "issue_code": row.get("issue_code"),
            }
            for row in corruption_manifest.get("expected_outcomes") or []
        ]
        matched: list[str] = []
        missing: list[dict[str, Any]] = []
        for row in expected_rows:
            observed = observed_dispositions.get(row["prior_card_id"])
            expected_value = {
                "verdict": row["verdict"],
                "issue_code": row["issue_code"],
            }
            if observed == expected_value:
                matched.append(row["prior_card_id"])
            else:
                missing.append({**row, "observed": observed})
        expected_ids = {row["prior_card_id"] for row in expected_rows}
        unexpected_noncompatible = sorted(
            card_id
            for card_id, row in observed_dispositions.items()
            if card_id not in expected_ids and row["verdict"] != "compatible"
        )
        expected_challenges = {
            (row["prior_card_id"], str(row["issue_code"]))
            for row in expected_rows
            if row["verdict"] == "challenge"
        }
        observed_tickets = {
            (str(row.get("prior_card_id")), str(row.get("issue_code")))
            for row in artifact.get("prior_conflict_tickets") or []
        }
        body = {
            "schema_version": "b0_prior_corruption_set_evaluation_v1",
            "mutation_id": str(corruption_manifest["mutation_id"]),
            "expected_outcome_count": len(expected_rows),
            "matched_outcome_count": len(matched),
            "matched_prior_card_ids": sorted(matched),
            "missing_or_mismatched_outcomes": missing,
            "unexpected_noncompatible_prior_card_ids": unexpected_noncompatible,
            "expected_ticket_detected": expected_challenges <= observed_tickets,
            "unrelated_ticket_count": len(observed_tickets - expected_challenges),
            "observed_ticket_count": len(observed_tickets),
            "all_expected_outcomes_detected": not missing,
            "artifact_hash": str(artifact.get("prior_challenge_artifact_hash") or ""),
            "corruption_manifest_hash": str(
                corruption_manifest["corruption_manifest_hash"]
            ),
        }
        return {**body, "evaluation_hash": canonical_hash(body)}

    expected = (
        str(corruption_manifest["changed_prior_card_id"]),
        str(corruption_manifest["expected_issue_code"]),
    )
    observed = [
        (str(row.get("prior_card_id")), str(row.get("issue_code")))
        for row in artifact.get("prior_conflict_tickets") or []
    ]
    body = {
        "schema_version": "b0_prior_corruption_evaluation_v1",
        "mutation_id": str(corruption_manifest["mutation_id"]),
        "expected_ticket_detected": expected in observed,
        "unrelated_ticket_count": sum(row != expected for row in observed),
        "observed_ticket_count": len(observed),
        "artifact_hash": str(artifact.get("prior_challenge_artifact_hash") or ""),
        "corruption_manifest_hash": str(corruption_manifest["corruption_manifest_hash"]),
    }
    return {**body, "evaluation_hash": canonical_hash(body)}


__all__ = [
    "AUTHORITY_SCOPE",
    "B0PriorChallengeError",
    "CORRUPTION_MANIFEST_SCHEMA_VERSION",
    "CORRUPTION_SET_MANIFEST_SCHEMA_VERSION",
    "EXPERIMENT_SCHEMA_VERSION",
    "ISSUE_TO_FIELD",
    "MAX_PRIOR_CARDS",
    "PROMPT_ID",
    "PROMPT_SHA256",
    "PROMPT_UTF8_BYTES",
    "build_hidden_corruption_manifest",
    "build_hidden_corruption_set_manifest",
    "build_candidate_only_packets",
    "build_glossary_packets",
    "build_prior_packets",
    "evaluate_hidden_corruption",
    "prior_challenge_response_schema",
    "render_prior_challenge_request",
    "select_prior_cards_for_chapter",
    "validate_candidate_only_context_cards",
    "validate_prior_cards",
    "validate_prior_challenge_response",
]
