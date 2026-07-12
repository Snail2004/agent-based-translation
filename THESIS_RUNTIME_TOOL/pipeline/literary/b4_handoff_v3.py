"""Deterministic, prompt-free Builder-v3 to B4 ground-evidence handoff."""

from __future__ import annotations

from hashlib import sha256
import json
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence
import unicodedata

from pipeline.literary.builder_v3_pipeline import (
    DEFAULT_SUMMARY_K,
    DEFAULT_TAIL_K,
    DEFAULT_WINDOW_MAX_BLOCKS,
    DEFAULT_WINDOW_TARGET_TOKENS,
    EXECUTION_MODE_SYNTHETIC,
    KNOWLEDGE_MODE,
    REQUEST_CONTRACT_HASHES,
)
from pipeline.literary.checkpoint import CheckpointError, canonical_hash, canonical_json
from pipeline.literary.checkpoint_v3 import (
    BUILDER_SCHEMA_V3,
    M1_CHECKPOINT_SCHEMA_VERSION_V3,
    M1_GROUND_STATE_VERSION_V3,
    M2_CHECKPOINT_SCHEMA_VERSION_V3,
    M2_DIGEST_STATE_VERSION_V3,
    contract_versions,
    read_current_checkpoint,
    read_state_from_checkpoint,
)
from pipeline.literary.source_anchor import nfc_block_string


VERIFIED_INPUTS_SCHEMA_VERSION = "literary_builder_v3_verified_inputs_v1"
BUNDLE_SCHEMA_VERSION = "literary_b4_input_bundle_v1"
HANDOFF_CONTRACT_VERSION = "literary_b4_handoff_contract_v1"

_OCCURRENCE_KINDS = {"mention", "endpoint"}
_ENDPOINT_BUCKETS = {
    "eligible": "person_occurrences",
    "route_out": "non_person_occurrences",
    "discourse_only": "discourse_only",
    "deferred": "deferred",
    "invalid": "invalid_flagged",
}
_MENTION_BUCKETS = {
    "person": "person_occurrences",
    "animal": "non_person_occurrences",
    "nonhuman_character": "non_person_occurrences",
    "place": "non_person_occurrences",
    "group_reference": "non_person_occurrences",
    "object": "non_person_occurrences",
    "unknown": "deferred",
}
_REF_KIND_ORDER = {
    "block": 0,
    "mention": 1,
    "endpoint": 2,
    "turn": 3,
    "event": 4,
    "address_occurrence": 5,
    "frame_segment": 6,
}
_ENDPOINT_ROLES = {"speaker", "addressee", "actor", "target"}
_CHANNELS = (
    "cast_claim_inputs",
    "glossary_inputs",
    "dialogue_turn_inputs",
    "relation_event_inputs",
    "phase_observation_inputs",
    "state_change_inputs",
    "unresolved_thread_inputs",
    "translator_fact_inputs",
    "motif_inputs",
    "rolling_summary_inputs",
    "frame_claim_inputs",
    "frame_leaf_index",
)
_FORBIDDEN_AUTHORITY_KEYS = {
    "entity_id",
    "candidate_entity_ids",
    "hint_entity_id",
    "reuse_entity_id",
    "binding_id",
    "base_binding",
    "decision_id",
    "resolution_status",
    "overlay",
    "disclosure",
    "phase_label",
    "address_policy",
}


class B4HandoffError(RuntimeError):
    """Fail-closed Step-4 contract violation."""


def _clone(value: Any) -> Any:
    return json.loads(canonical_json(value))


def _ordered_blocks(chapter: Mapping[str, Any]) -> list[dict[str, Any]]:
    rows = [dict(row) for row in chapter.get("blocks") or [] if row.get("block_id")]
    rows.sort(key=lambda row: (int(row.get("order_index") or 0), str(row["block_id"])))
    if len({str(row["block_id"]) for row in rows}) != len(rows):
        raise B4HandoffError(f"duplicate source block id: {chapter.get('chapter_id')}")
    return rows


def _block_view(block: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "block_id": str(block.get("block_id") or ""),
        "order_index": int(block.get("order_index") or 0),
        "block_type": str(block.get("block_type") or ""),
        "text": nfc_block_string(block),
    }


def _source_hash(chapter: Mapping[str, Any]) -> str:
    return canonical_hash([_block_view(row) for row in _ordered_blocks(chapter)])


def _m1_semantic_projection(state: Mapping[str, Any]) -> dict[str, Any]:
    projection = _clone(state)
    projection.pop("request_manifest", None)
    projection.pop("semantic_state_hash", None)
    return projection


def _m2_semantic_projection(state: Mapping[str, Any]) -> dict[str, Any]:
    projection = _clone(state)
    projection.pop("request_manifest", None)
    projection.pop("semantic_state_hash", None)
    projection.pop("input_m1v3_checkpoint_hash", None)
    for row in projection.get("prior_summary_provenance") or []:
        row.pop("source_m2v3_checkpoint_hash", None)
    return projection


def _validate_state(
    state: Mapping[str, Any],
    *,
    stage: str,
    chapter_id: str,
    m1_checkpoint: Mapping[str, Any] | None = None,
) -> None:
    expected_schema = (
        M1_GROUND_STATE_VERSION_V3 if stage == "m1v3" else M2_DIGEST_STATE_VERSION_V3
    )
    if state.get("schema_version") != expected_schema or state.get("chapter_id") != chapter_id:
        raise CheckpointError(f"restored {stage} state identity mismatch: {chapter_id}")
    expected_contract = contract_versions() if stage == "m1v3" else None
    if state.get("contract_versions") != expected_contract:
        raise CheckpointError(f"restored {stage} state contract mismatch: {chapter_id}")
    projection = (
        _m1_semantic_projection(state) if stage == "m1v3" else _m2_semantic_projection(state)
    )
    if canonical_hash(projection) != state.get("semantic_state_hash"):
        raise CheckpointError(f"restored {stage} semantic_state_hash mismatch: {chapter_id}")
    if stage == "m2v3":
        if m1_checkpoint is None:
            raise CheckpointError(f"M2 validation lacks M1 checkpoint: {chapter_id}")
        if state.get("input_m1v3_identity_hash") != m1_checkpoint.get(
            "checkpoint_identity_hash"
        ):
            raise CheckpointError(f"M2 state has wrong M1 identity: {chapter_id}")
        if state.get("input_m1v3_checkpoint_hash") != m1_checkpoint.get("checkpoint_hash"):
            raise CheckpointError(f"M2 state has wrong operational M1 checkpoint: {chapter_id}")


def _validate_prior_summary_provenance(
    state: Mapping[str, Any],
    *,
    prior_chapters: Sequence[Mapping[str, Any]],
    summary_k: int,
) -> None:
    expected = list(prior_chapters[max(0, len(prior_chapters) - summary_k) :])
    rows = state.get("prior_summary_provenance") or []
    expected_ids = [str(row.get("chapter_id") or "") for row in expected]
    actual_ids = [str(row.get("chapter_id") or "") for row in rows]
    if actual_ids != expected_ids:
        raise CheckpointError(
            f"M2 prior-summary chapter lineage mismatch: {state.get('chapter_id')}"
        )
    for actual, prior in zip(rows, expected):
        if actual.get("source_m2v3_identity_hash") != prior.get("m2v3_identity_hash"):
            raise CheckpointError(
                f"M2 prior-summary identity lineage mismatch: {state.get('chapter_id')}"
            )


def _expected_checkpoint(
    *,
    stage: str,
    chapter: Mapping[str, Any],
    chapter_index: int,
    chapter_prefix: Sequence[str],
    parent_identity_hash: str | None,
    execution_mode: str,
    window_target_tokens: int,
    window_max_blocks: int,
    tail_k: int,
    summary_k: int,
    input_m1v3_identity_hash: str | None = None,
) -> dict[str, Any]:
    expected = {
        "stage": stage,
        "chapter_id": str(chapter.get("chapter_id") or ""),
        "schema_version": (
            M1_CHECKPOINT_SCHEMA_VERSION_V3
            if stage == "m1v3"
            else M2_CHECKPOINT_SCHEMA_VERSION_V3
        ),
        "builder_schema": BUILDER_SCHEMA_V3,
        "absolute_chapter_index": chapter_index,
        "chapter_sequence_prefix": list(chapter_prefix),
        "source_hash": _source_hash(chapter),
        "knowledge_mode": KNOWLEDGE_MODE,
        "execution_mode": execution_mode,
        "contract_versions": contract_versions(),
        "request_contract_hashes": dict(REQUEST_CONTRACT_HASHES),
        "window_target_tokens": window_target_tokens,
        "window_max_blocks": window_max_blocks,
        "tail_k": tail_k,
        "summary_k": 0 if stage == "m1v3" else summary_k,
        "parent_checkpoint_identity_hash": parent_identity_hash,
    }
    if stage == "m2v3":
        expected["input_m1v3_identity_hash"] = input_m1v3_identity_hash
    return expected


def load_verified_builder_v3_inputs(
    document: Mapping[str, Any],
    chapters: Sequence[str],
    *,
    m1v3_dir: Path,
    m2v3_dir: Path,
    knowledge_mode: str = KNOWLEDGE_MODE,
    execution_mode: str = EXECUTION_MODE_SYNTHETIC,
    window_target_tokens: int = DEFAULT_WINDOW_TARGET_TOKENS,
    window_max_blocks: int = DEFAULT_WINDOW_MAX_BLOCKS,
    tail_k: int = DEFAULT_TAIL_K,
    summary_k: int = DEFAULT_SUMMARY_K,
) -> dict[str, Any]:
    """Load an exact Builder-v3 prefix after validating both checkpoint chains."""

    if knowledge_mode != KNOWLEDGE_MODE:
        raise ValueError("Step-4 supports whole_book_frozen only")
    if execution_mode != EXECUTION_MODE_SYNTHETIC:
        raise ValueError("Step-4 currently supports synthetic checkpoints only")
    whole = [dict(row) for row in document.get("chapters") or []]
    whole_ids = [str(row.get("chapter_id") or "") for row in whole]
    if not whole or not chapters or any(not value for value in whole_ids):
        raise ValueError("document and selected chapter prefix must be non-empty")
    selected_ids = [str(value) for value in chapters]
    if len(set(whole_ids)) != len(whole_ids) or selected_ids != whole_ids[: len(selected_ids)]:
        raise ValueError("Step-4 chapters must equal the exact document prefix")

    config_identity = {
        "handoff_contract_version": HANDOFF_CONTRACT_VERSION,
        "knowledge_mode": knowledge_mode,
        "execution_mode": execution_mode,
        "window_target_tokens": int(window_target_tokens),
        "window_max_blocks": int(window_max_blocks),
        "tail_k": int(tail_k),
        "summary_k": int(summary_k),
        "contract_versions": contract_versions(),
        "request_contract_hashes": dict(REQUEST_CONTRACT_HASHES),
    }
    verified: list[dict[str, Any]] = []
    m1_parent: str | None = None
    m2_parent: str | None = None
    for index, chapter in enumerate(whole[: len(selected_ids)]):
        chapter_id = selected_ids[index]
        prefix = selected_ids[: index + 1]
        m1 = read_current_checkpoint(
            out_dir=Path(m1v3_dir),
            stage="m1v3",
            chapter_id=chapter_id,
            expected=_expected_checkpoint(
                stage="m1v3",
                chapter=chapter,
                chapter_index=index,
                chapter_prefix=prefix,
                parent_identity_hash=m1_parent,
                execution_mode=execution_mode,
                window_target_tokens=window_target_tokens,
                window_max_blocks=window_max_blocks,
                tail_k=tail_k,
                summary_k=summary_k,
            ),
        )
        if m1 is None:
            raise CheckpointError(f"missing required M1V3 checkpoint: {chapter_id}")
        m1_state = read_state_from_checkpoint(m1, out_dir=Path(m1v3_dir))
        _validate_state(m1_state, stage="m1v3", chapter_id=chapter_id)

        m2 = read_current_checkpoint(
            out_dir=Path(m2v3_dir),
            stage="m2v3",
            chapter_id=chapter_id,
            expected=_expected_checkpoint(
                stage="m2v3",
                chapter=chapter,
                chapter_index=index,
                chapter_prefix=prefix,
                parent_identity_hash=m2_parent,
                execution_mode=execution_mode,
                window_target_tokens=window_target_tokens,
                window_max_blocks=window_max_blocks,
                tail_k=tail_k,
                summary_k=summary_k,
                input_m1v3_identity_hash=str(m1["checkpoint_identity_hash"]),
            ),
        )
        if m2 is None:
            raise CheckpointError(f"missing required M2V3 checkpoint: {chapter_id}")
        m2_state = read_state_from_checkpoint(m2, out_dir=Path(m2v3_dir))
        _validate_state(
            m2_state,
            stage="m2v3",
            chapter_id=chapter_id,
            m1_checkpoint=m1,
        )
        _validate_prior_summary_provenance(
            m2_state,
            prior_chapters=verified,
            summary_k=summary_k,
        )
        verified.append(
            {
                "chapter_id": chapter_id,
                "absolute_chapter_index": index,
                "source_blocks": [_block_view(row) for row in _ordered_blocks(chapter)],
                "m1v3_identity_hash": str(m1["checkpoint_identity_hash"]),
                "m2v3_identity_hash": str(m2["checkpoint_identity_hash"]),
                "m1_state": _clone(m1_state),
                "m2_state": _clone(m2_state),
            }
        )
        m1_parent = str(m1["checkpoint_identity_hash"])
        m2_parent = str(m2["checkpoint_identity_hash"])

    return _clone(
        {
            "schema_version": VERIFIED_INPUTS_SCHEMA_VERSION,
            "knowledge_mode": knowledge_mode,
            "execution_mode": execution_mode,
            "selected_chapters": selected_ids,
            "knowledge_cutoff_scope": selected_ids[-1],
            "scope_complete_book": selected_ids == whole_ids,
            "config_identity": config_identity,
            "chapters": verified,
        }
    )


def _anchor_tuple(anchor: Mapping[str, Any]) -> tuple[str, int, int]:
    block_id = str(anchor.get("block_id") or "")
    start = anchor.get("char_start")
    end = anchor.get("char_end")
    if not block_id or not isinstance(start, int) or not isinstance(end, int) or start < 0 or end <= start:
        raise B4HandoffError("occurrence has an invalid SourceAnchor")
    return block_id, start, end


def _unique_evidence_span(
    text: str, evidence_quote: str, anchor_start: int, anchor_end: int
) -> tuple[int, int]:
    quote = unicodedata.normalize("NFC", str(evidence_quote))
    if not quote:
        raise B4HandoffError("occurrence evidence_quote is empty")
    starts: list[int] = []
    cursor = 0
    while cursor <= len(text) - len(quote):
        found = text.find(quote, cursor)
        if found < 0:
            break
        starts.append(found)
        cursor = found + 1
    containing = [
        (start, start + len(quote))
        for start in starts
        if start <= anchor_start and anchor_end <= start + len(quote)
    ]
    if len(containing) != 1:
        raise B4HandoffError(
            f"evidence quote must have one containing span, found {len(containing)}"
        )
    return containing[0]


def _scene_context(
    *,
    block_id: str,
    source_blocks: Sequence[Mapping[str, Any]],
    scenes: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    block_map = {str(row.get("block_id") or ""): dict(row) for row in source_blocks}
    if block_id not in block_map:
        raise B4HandoffError(f"occurrence block is absent from source: {block_id}")
    coverage = [
        str(row.get("block_id") or "")
        for row in source_blocks
        if str(row.get("block_type") or "") in {"paragraph", "dialogue"}
    ]
    matches: list[tuple[list[str], list[str]]] = []
    for scene in scenes:
        raw_range = scene.get("block_range")
        if not isinstance(raw_range, list) or len(raw_range) != 2:
            continue
        try:
            start = coverage.index(str(raw_range[0]))
            end = coverage.index(str(raw_range[1]))
        except ValueError:
            continue
        if start <= end:
            ids = coverage[start : end + 1]
            if block_id in ids:
                matches.append(([str(raw_range[0]), str(raw_range[1])], ids))
    if len(matches) == 1:
        scene_range, ids = matches[0]
        return {
            "active_block": _clone(block_map[block_id]),
            "scene_block_candidates": [_clone(block_map[value]) for value in ids],
            "scene_range": scene_range,
            "source": "b0_scene_partition",
        }
    return {
        "active_block": _clone(block_map[block_id]),
        "scene_block_candidates": [_clone(block_map[block_id])],
        "scene_range": [block_id, block_id],
        "source": "active_block_fallback",
    }


def _build_source_block_catalog(inputs: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Forward every selected NFC source block exactly once for Step-5 calls."""

    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for chapter in inputs.get("chapters") or []:
        chapter_id = str(chapter.get("chapter_id") or "")
        for block in chapter.get("source_blocks") or []:
            block_id = str(block.get("block_id") or "")
            text = str(block.get("text") or "")
            if not block_id or block_id in seen:
                raise B4HandoffError(
                    f"source block catalog has duplicate/empty block id: {block_id!r}"
                )
            if unicodedata.normalize("NFC", text) != text:
                raise B4HandoffError(f"source block catalog text is not NFC: {block_id}")
            seen.add(block_id)
            rows.append(
                {
                    "chapter_id": chapter_id,
                    "block_id": block_id,
                    "order_index": int(block.get("order_index") or 0),
                    "block_type": str(block.get("block_type") or ""),
                    "text": text,
                }
            )
    return _clone(rows)


def _summary_lineage(inputs: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Preserve deterministic K-summary provenance without operational hashes/prose."""

    result: list[dict[str, Any]] = []
    for chapter in inputs.get("chapters") or []:
        prior: list[dict[str, Any]] = []
        for raw in (chapter.get("m2_state") or {}).get("prior_summary_provenance") or []:
            row = {
                "chapter_id": str(raw.get("chapter_id") or ""),
                "source_m2v3_identity_hash": str(
                    raw.get("source_m2v3_identity_hash") or ""
                ),
                "input_max_order": raw.get("input_max_order"),
            }
            if (
                not row["chapter_id"]
                or not row["source_m2v3_identity_hash"]
                or not isinstance(row["input_max_order"], int)
            ):
                raise B4HandoffError(
                    f"prior-summary audit provenance is malformed: {chapter.get('chapter_id')}"
                )
            prior.append(row)
        result.append(
            {
                "chapter_id": str(chapter.get("chapter_id") or ""),
                "prior_summaries": prior,
            }
        )
    return _clone(result)


def _validate_block_resolution(
    catalog: Sequence[Mapping[str, Any]],
    occurrence_cards: Sequence[Mapping[str, Any]],
    ground_evidence: Mapping[str, Any],
) -> None:
    """Require every block reference/context row to resolve to catalog text."""

    block_map: dict[str, dict[str, Any]] = {}
    for raw in catalog:
        row = _clone(raw)
        block_id = str(row.get("block_id") or "")
        if not block_id or block_id in block_map:
            raise B4HandoffError(
                f"source block catalog is not uniquely addressable: {block_id!r}"
            )
        block_map[block_id] = row

    for card in occurrence_cards:
        block_id = str(card.get("block_id") or "")
        universe = card.get("context_universe") or {}
        context_rows = [
            universe.get("active_block"),
            *(universe.get("scene_block_candidates") or []),
        ]
        if block_id not in block_map:
            raise B4HandoffError(f"occurrence card block is absent from catalog: {block_id}")
        for raw_context in context_rows:
            if not isinstance(raw_context, Mapping):
                raise B4HandoffError("occurrence context contains a non-object block")
            context_id = str(raw_context.get("block_id") or "")
            expected = block_map.get(context_id)
            comparable = (
                {
                    "block_id": expected["block_id"],
                    "order_index": expected["order_index"],
                    "block_type": expected["block_type"],
                    "text": expected["text"],
                }
                if expected is not None
                else None
            )
            if comparable != raw_context:
                raise B4HandoffError(
                    f"occurrence context disagrees with source catalog: {context_id}"
                )

    for channel, rows in ground_evidence.items():
        if channel == "dedupe_counts":
            continue
        if not isinstance(rows, list):
            raise B4HandoffError(f"ground channel is not a list: {channel}")
        for row in rows:
            for ref in row.get("evidence_refs") or []:
                if (
                    ref.get("ref_kind") == "block"
                    and str(ref.get("ref_id") or "") not in block_map
                ):
                    raise B4HandoffError(
                        f"ground block reference is absent from catalog: {ref.get('ref_id')}"
                    )


def _register_owner(
    owners: dict[str, list[dict[str, Any]]], identifier: str, row: Mapping[str, Any]
) -> None:
    if not identifier:
        raise B4HandoffError("owner occurrence id is empty")
    owners.setdefault(identifier, []).append(_clone(row))


def _owner_rows(chapter: Mapping[str, Any]) -> dict[str, list[dict[str, Any]]]:
    state = chapter["m1_state"]
    owners: dict[str, list[dict[str, Any]]] = {}
    for window in state.get("b1_by_window") or []:
        window_id = str(window.get("window_id") or "")
        for mention in (window.get("payload") or {}).get("character_mentions") or []:
            mention_id = str(mention.get("mention_id") or "")
            _register_owner(
                owners,
                mention_id,
                {
                    "occurrence_kind": "mention",
                    "window_id": window_id,
                    "owner_stage": "b1",
                    "owner_id": mention_id,
                    "owner_role": None,
                    "payload": mention,
                },
            )
    for window in state.get("b2_by_window") or []:
        window_id = str(window.get("window_id") or "")
        payload = window.get("payload") or {}
        for turn in payload.get("speaker_turns") or []:
            turn_id = str(turn.get("turn_id") or "")
            for role in ("speaker", "addressee"):
                endpoint = turn.get(role)
                if not isinstance(endpoint, Mapping):
                    continue
                _register_owner(
                    owners,
                    str(endpoint.get("endpoint_id") or ""),
                    {
                        "occurrence_kind": "endpoint",
                        "window_id": window_id,
                        "owner_stage": "b2",
                        "owner_id": turn_id,
                        "owner_role": role,
                        "payload": endpoint,
                    },
                )
        for event in payload.get("relation_events") or []:
            event_id = str(event.get("event_id") or "")
            for role in ("actor", "target"):
                endpoint = event.get(role)
                if not isinstance(endpoint, Mapping):
                    continue
                _register_owner(
                    owners,
                    str(endpoint.get("endpoint_id") or ""),
                    {
                        "occurrence_kind": "endpoint",
                        "window_id": window_id,
                        "owner_stage": "b2",
                        "owner_id": event_id,
                        "owner_role": role,
                        "payload": endpoint,
                    },
                )
    return owners


def _reference_index(chapter: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    rows = chapter["m1_state"].get("reference_index") or []
    mapped: dict[str, dict[str, Any]] = {}
    for raw in rows:
        row = _clone(raw)
        identifier = str(row.get("id") or "")
        if not identifier or identifier in mapped:
            raise B4HandoffError(f"duplicate/empty M1 reference index id: {identifier!r}")
        mapped[identifier] = row
    return mapped


def _assert_roster_owner_match(
    roster: Mapping[str, Any], owner: Mapping[str, Any], reference: Mapping[str, Any]
) -> None:
    payload = owner["payload"]
    anchor = payload.get("anchor") or {}
    expected = {
        "occurrence_kind": owner["occurrence_kind"],
        "surface": str(payload.get("surface") or ""),
        "referent_kind_claim": str(payload.get("referent_kind_claim") or ""),
        "reference_scope": (
            None
            if owner["occurrence_kind"] == "mention"
            else str(payload.get("reference_scope") or "")
        ),
        "block_id": str(anchor.get("block_id") or payload.get("block_id") or ""),
        "anchor": _clone(anchor),
    }
    for key, value in expected.items():
        if roster.get(key) != value:
            raise B4HandoffError(f"occurrence roster disagrees with owner for {roster.get('id')}: {key}")
    expected_kind = owner["occurrence_kind"]
    if reference.get("kind") != expected_kind or reference.get("window_id") != owner["window_id"]:
        raise B4HandoffError(f"reference index disagrees with owner for {roster.get('id')}")
    if reference.get("anchor") != expected["anchor"]:
        raise B4HandoffError(f"reference index anchor disagrees for {roster.get('id')}")


def build_occurrence_cards(inputs: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Join every compact occurrence to exactly one normalized B1/B2 owner."""

    cards: list[dict[str, Any]] = []
    for chapter in inputs.get("chapters") or []:
        chapter_id = str(chapter.get("chapter_id") or "")
        source_blocks = chapter.get("source_blocks") or []
        block_map = {str(row.get("block_id") or ""): row for row in source_blocks}
        order = {str(row.get("block_id") or ""): int(row.get("order_index") or 0) for row in source_blocks}
        owners = _owner_rows(chapter)
        references = _reference_index(chapter)
        roster_rows = chapter["m2_state"].get("occurrence_roster") or []
        roster: dict[str, dict[str, Any]] = {}
        for raw in roster_rows:
            row = _clone(raw)
            identifier = str(row.get("id") or "")
            if not identifier or identifier in roster:
                raise B4HandoffError(f"duplicate/empty occurrence roster id: {identifier!r}")
            roster[identifier] = row
        if set(roster) != set(owners):
            raise B4HandoffError(
                f"occurrence roster/owner exact-cover mismatch: missing={sorted(set(owners)-set(roster))}, "
                f"extra={sorted(set(roster)-set(owners))}"
            )
        if set(roster) != {
            identifier
            for identifier, row in references.items()
            if row.get("kind") in _OCCURRENCE_KINDS
        }:
            raise B4HandoffError("occurrence roster/reference-index exact-cover mismatch")

        scenes = (chapter["m1_state"].get("b0_payload") or {}).get("scenes_party_size") or []
        for identifier, roster_row in roster.items():
            matches = owners.get(identifier) or []
            if len(matches) != 1:
                raise B4HandoffError(f"occurrence must have exactly one owner: {identifier}")
            owner = matches[0]
            reference = references.get(identifier)
            if reference is None:
                raise B4HandoffError(f"occurrence lacks reference-index row: {identifier}")
            _assert_roster_owner_match(roster_row, owner, reference)
            payload = owner["payload"]
            anchor = _clone(payload.get("anchor") or {})
            block_id, anchor_start, anchor_end = _anchor_tuple(anchor)
            if block_id not in block_map:
                raise B4HandoffError(f"occurrence source block is missing: {identifier}")
            evidence = str(
                payload.get("evidence_quote")
                if owner["occurrence_kind"] == "mention"
                else payload.get("resolution_evidence")
                or ""
            )
            text = str(block_map[block_id].get("text") or "")
            quote_start, quote_end = _unique_evidence_span(
                text, evidence, anchor_start, anchor_end
            )
            common = {
                "occurrence_id": identifier,
                "occurrence_kind": owner["occurrence_kind"],
                "surface": str(payload.get("surface") or ""),
                "referent_kind_claim": str(payload.get("referent_kind_claim") or ""),
                "chapter_id": chapter_id,
                "window_id": owner["window_id"],
                "block_id": block_id,
                "block_order": order[block_id],
                "anchor": anchor,
                "evidence_quote": unicodedata.normalize("NFC", evidence),
                "evidence_span": {"char_start": quote_start, "char_end": quote_end},
                "source_ref": {
                    "owner_stage": owner["owner_stage"],
                    "owner_id": owner["owner_id"],
                    "owner_role": owner["owner_role"],
                },
                "context_universe": _scene_context(
                    block_id=block_id, source_blocks=source_blocks, scenes=scenes
                ),
            }
            if owner["occurrence_kind"] == "mention":
                card = {
                    **common,
                    "reference_scope": None,
                    "mention_type": str(payload.get("mention_type") or ""),
                }
            else:
                card = {
                    **common,
                    "reference_scope": str(payload.get("reference_scope") or ""),
                    "mention_ref": (
                        str(payload.get("mention_ref"))
                        if payload.get("mention_ref") is not None
                        else None
                    ),
                    "attribution_method": str(payload.get("attribution_method") or ""),
                    "runtime_eligibility": str(payload.get("runtime_eligibility") or ""),
                    "resolution_evidence": str(payload.get("resolution_evidence") or ""),
                    "owner_id": owner["owner_id"],
                    "owner_role": owner["owner_role"],
                }
            cards.append(card)
    cards.sort(
        key=lambda row: (
            inputs["selected_chapters"].index(row["chapter_id"]),
            row["block_order"],
            int(row["anchor"]["char_start"]),
            row["occurrence_kind"],
            row["occurrence_id"],
        )
    )
    return _clone(cards)


def build_occurrence_routing_view(
    inputs: Mapping[str, Any], occurrence_cards: Sequence[Mapping[str, Any]]
) -> dict[str, Any]:
    """Route occurrences without re-deriving endpoint two-axis semantics."""

    buckets: dict[str, list[dict[str, Any]]] = {
        "person_occurrences": [],
        "non_person_occurrences": [],
        "discourse_only": [],
        "deferred": [],
        "invalid_flagged": [],
    }
    seen: set[str] = set()
    for raw in occurrence_cards:
        card = _clone(raw)
        identifier = str(card.get("occurrence_id") or "")
        if not identifier or identifier in seen:
            raise B4HandoffError(f"routing input has duplicate/empty occurrence: {identifier!r}")
        seen.add(identifier)
        kind = str(card.get("occurrence_kind") or "")
        if kind == "mention":
            claim = str(card.get("referent_kind_claim") or "")
            bucket = _MENTION_BUCKETS.get(claim)
            if bucket is None:
                raise B4HandoffError(f"mention has foreign referent_kind_claim: {identifier}")
        elif kind == "endpoint":
            eligibility = str(card.get("runtime_eligibility") or "")
            bucket = _ENDPOINT_BUCKETS.get(eligibility)
            if bucket is None:
                raise B4HandoffError(f"endpoint has missing/foreign runtime_eligibility: {identifier}")
        else:
            raise B4HandoffError(f"routing input has foreign occurrence_kind: {kind}")
        buckets[bucket].append(card)
    total = sum(len(rows) for rows in buckets.values())
    if total != len(occurrence_cards) or len(seen) != len(occurrence_cards):
        raise B4HandoffError("occurrence routing is not an exact cover")
    return _clone(
        {
            **buckets,
            "counts": {
                "total": total,
                "person": len(buckets["person_occurrences"]),
                "non_person": len(buckets["non_person_occurrences"]),
                "discourse_only": len(buckets["discourse_only"]),
                "deferred": len(buckets["deferred"]),
                "invalid_flagged": len(buckets["invalid_flagged"]),
            },
        }
    )


def _ref(ref_kind: str, ref_id: str, role: str | None = None) -> dict[str, Any]:
    if ref_kind not in _REF_KIND_ORDER or not ref_id:
        raise B4HandoffError(f"invalid evidence reference: {ref_kind}:{ref_id!r}")
    if role is not None and (ref_kind != "endpoint" or role not in _ENDPOINT_ROLES):
        raise B4HandoffError(f"invalid evidence reference role: {ref_kind}:{role}")
    return {"ref_kind": ref_kind, "ref_id": str(ref_id), "role": role}


def _inclusive_block_ids(
    raw_range: Any, source_blocks: Sequence[Mapping[str, Any]]
) -> list[str]:
    if not isinstance(raw_range, list) or len(raw_range) != 2:
        raise B4HandoffError("source block range must have two ids")
    ids = [str(row.get("block_id") or "") for row in source_blocks]
    try:
        start = ids.index(str(raw_range[0]))
        end = ids.index(str(raw_range[1]))
    except ValueError as exc:
        raise B4HandoffError(f"source block range is foreign: {raw_range}") from exc
    if start > end:
        raise B4HandoffError(f"source block range is reversed: {raw_range}")
    return ids[start : end + 1]


def _reference_positions(
    chapter: Mapping[str, Any], cards: Sequence[Mapping[str, Any]]
) -> tuple[dict[tuple[str, str], tuple[int, int]], dict[str, str]]:
    positions: dict[tuple[str, str], tuple[int, int]] = {}
    occurrence_kinds: dict[str, str] = {}
    source_blocks = chapter["source_blocks"]
    order = {str(row["block_id"]): int(row["order_index"]) for row in source_blocks}
    for block_id, block_order in order.items():
        positions[("block", block_id)] = (block_order, 0)
    for card in cards:
        if card.get("chapter_id") != chapter.get("chapter_id"):
            continue
        identifier = str(card["occurrence_id"])
        kind = str(card["occurrence_kind"])
        occurrence_kinds[identifier] = kind
        positions[(kind, identifier)] = (
            int(card["block_order"]),
            int((card.get("anchor") or {}).get("char_start") or 0),
        )
    for raw in chapter["m1_state"].get("reference_index") or []:
        kind = str(raw.get("kind") or "")
        if kind not in {"turn", "event", "address_occurrence"}:
            continue
        anchor = raw.get("anchor") or {}
        block_id = str(anchor.get("block_id") or "")
        positions[(kind, str(raw.get("id") or ""))] = (
            order.get(block_id, 10**9),
            int(anchor.get("char_start") or 0),
        )
    for raw in chapter["m2_state"].get("digest_reference_index") or []:
        if raw.get("kind") != "frame_segment":
            continue
        interval = raw.get("source_interval") or {}
        start = interval.get("start") or {}
        positions[("frame_segment", str(raw.get("id") or ""))] = (
            int(start.get("block_order") or 0),
            int(start.get("char_offset") or 0),
        )
    return positions, occurrence_kinds


def _normalize_refs(
    refs: Iterable[Mapping[str, Any]],
    positions: Mapping[tuple[str, str], tuple[int, int]],
) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str | None]] = set()
    for raw in refs:
        row = _ref(
            str(raw.get("ref_kind") or ""),
            str(raw.get("ref_id") or ""),
            str(raw.get("role")) if raw.get("role") is not None else None,
        )
        key = (row["ref_kind"], row["ref_id"], row["role"])
        if key in seen:
            continue
        seen.add(key)
        if (row["ref_kind"], row["ref_id"]) not in positions:
            raise B4HandoffError(f"evidence reference is absent from source indexes: {key}")
        normalized.append(row)
    normalized.sort(
        key=lambda row: (
            positions[(row["ref_kind"], row["ref_id"])],
            _REF_KIND_ORDER[row["ref_kind"]],
            row["ref_id"],
            row["role"] or "",
        )
    )
    return normalized


def _ground_item_id(
    *, kind: str, chapter_id: str, evidence_refs: Sequence[Mapping[str, Any]], payload: Any
) -> str:
    body = {
        "kind": kind,
        "chapter_id": chapter_id,
        "evidence_refs": _clone(evidence_refs),
        "payload": _clone(payload),
    }
    digest = sha256(canonical_json(body).encode("utf-8")).hexdigest()[:20]
    return f"g_{kind}_{chapter_id}_{digest}"


class _GroundCollector:
    def __init__(self) -> None:
        self.rows: dict[str, list[tuple[tuple[Any, ...], dict[str, Any]]]] = {
            channel: [] for channel in _CHANNELS
        }
        self.by_id: dict[str, str] = {}
        self.equal_duplicates = 0

    def add(
        self,
        *,
        channel: str,
        kind: str,
        chapter_id: str,
        chapter_index: int,
        source_ordinal: int,
        evidence_refs: Sequence[Mapping[str, Any]],
        payload: Any,
        source_identity: str,
        positions: Mapping[tuple[str, str], tuple[int, int]],
    ) -> None:
        if channel not in self.rows:
            raise B4HandoffError(f"unknown ground-evidence channel: {channel}")
        refs = _normalize_refs(evidence_refs, positions)
        body = {
            "kind": kind,
            "chapter_id": chapter_id,
            "evidence_refs": refs,
            "payload": _clone(payload),
        }
        identifier = _ground_item_id(**body)
        canonical_body = canonical_json(body)
        if identifier in self.by_id:
            if self.by_id[identifier] != canonical_body:
                raise B4HandoffError(f"ground_item_id collision with unequal payload: {identifier}")
            self.equal_duplicates += 1
            return
        self.by_id[identifier] = canonical_body
        first_position = min(
            (positions[(row["ref_kind"], row["ref_id"])] for row in refs),
            default=(10**9, 10**9),
        )
        row = {
            "ground_item_id": identifier,
            **body,
            "source_checkpoint_identity_hash": source_identity,
        }
        self.rows[channel].append(
            ((chapter_index, first_position, source_ordinal, identifier), row)
        )

    def finish(self) -> dict[str, Any]:
        return {
            **{
                channel: [_clone(row) for _key, row in sorted(rows, key=lambda item: item[0])]
                for channel, rows in self.rows.items()
            },
            "dedupe_counts": {"equal_ground_items": self.equal_duplicates},
        }


def _occurrence_ref(
    identifier: str, occurrence_kinds: Mapping[str, str]
) -> dict[str, Any]:
    kind = occurrence_kinds.get(str(identifier))
    if kind not in _OCCURRENCE_KINDS:
        raise B4HandoffError(f"foreign occurrence evidence ref: {identifier}")
    return _ref(kind, str(identifier))


def _block_refs(block_ids: Iterable[Any]) -> list[dict[str, Any]]:
    return [_ref("block", str(value)) for value in block_ids]


def _endpoint_refs(
    first: str, second: str, first_role: str, second_role: str
) -> list[dict[str, Any]]:
    return [
        _ref("endpoint", first, first_role),
        _ref("endpoint", second, second_role),
    ]


def _iter_window_payloads(
    rows: Sequence[Mapping[str, Any]], key: str
) -> Iterable[tuple[int, Mapping[str, Any], str]]:
    ordinal = 0
    for window in rows:
        window_id = str(window.get("window_id") or "")
        for value in (window.get("payload") or {}).get(key) or []:
            yield ordinal, value, window_id
            ordinal += 1


def _validate_frame_leaf_index(
    digest: Mapping[str, Any], source_blocks: Sequence[Mapping[str, Any]]
) -> None:
    """Defensively bind validator-derived leaf spans to the preserved frame tree."""

    frames: dict[str, Mapping[str, Any]] = {}
    for frame in digest.get("narration_frame_segments") or []:
        key = str(frame.get("local_segment_key") or "")
        if not key or key in frames:
            raise B4HandoffError(f"frame leaf index has missing/duplicate frame key: {key!r}")
        frames[key] = frame
    raw_spans = digest.get("deepest_active_leaf_spans")
    raw_by_block = digest.get("deepest_active_leaf_by_block")
    if not isinstance(raw_spans, list) or not isinstance(raw_by_block, Mapping):
        raise B4HandoffError("frame leaf index must contain typed spans and by-block map")

    block_map = {str(row.get("block_id") or ""): row for row in source_blocks}
    spans_by_block: dict[str, list[tuple[int, int, str]]] = {}
    for raw in raw_spans:
        if not isinstance(raw, Mapping):
            raise B4HandoffError("frame leaf span must be an object")
        block_id = str(raw.get("block_id") or "")
        key = str(raw.get("segment_key") or "")
        start = raw.get("char_start")
        end = raw.get("char_end")
        if (
            block_id not in block_map
            or key not in frames
            or not isinstance(start, int)
            or not isinstance(end, int)
            or start < 0
            or end <= start
            or end > len(str(block_map[block_id].get("text") or ""))
        ):
            raise B4HandoffError(f"frame leaf span is malformed or foreign: {raw}")
        if block_id not in _inclusive_block_ids(frames[key].get("block_range"), source_blocks):
            raise B4HandoffError(f"frame leaf span lies outside its frame: {block_id}:{key}")
        spans_by_block.setdefault(block_id, []).append((start, end, key))

    expected_by_block: dict[str, str] = {}
    for block_id, spans in spans_by_block.items():
        ordered = sorted(spans)
        cursor = 0
        for start, end, _key in ordered:
            if start != cursor:
                raise B4HandoffError(f"frame leaf spans leave a gap/overlap in {block_id}")
            cursor = end
        if cursor != len(str(block_map[block_id].get("text") or "")):
            raise B4HandoffError(f"frame leaf spans do not cover full block: {block_id}")
        keys = {key for _start, _end, key in ordered}
        if len(keys) == 1:
            expected_by_block[block_id] = next(iter(keys))

    coverage = {
        block_id
        for block_id, row in block_map.items()
        if str(row.get("block_type") or "") in {"paragraph", "dialogue"}
    }
    if set(spans_by_block) != coverage:
        raise B4HandoffError("frame leaf spans do not exact-cover narrative blocks")
    normalized_by_block = {str(key): str(value) for key, value in raw_by_block.items()}
    if normalized_by_block != expected_by_block:
        raise B4HandoffError("frame deepest-active by-block map disagrees with leaf spans")

def build_complete_ground_evidence(
    inputs: Mapping[str, Any], occurrence_cards: Sequence[Mapping[str, Any]]
) -> dict[str, Any]:
    """Assemble every B4-consumed Builder channel without identity decisions."""

    collector = _GroundCollector()
    for chapter in inputs.get("chapters") or []:
        chapter_id = str(chapter.get("chapter_id") or "")
        chapter_index = int(chapter.get("absolute_chapter_index") or 0)
        m1_identity = str(chapter.get("m1v3_identity_hash") or "")
        m2_identity = str(chapter.get("m2v3_identity_hash") or "")
        m1_state = chapter["m1_state"]
        m2_state = chapter["m2_state"]
        digest = m2_state.get("digest_payload") or {}
        source_blocks = chapter["source_blocks"]
        positions, occurrence_kinds = _reference_positions(chapter, occurrence_cards)

        for ordinal, claim in enumerate((m1_state.get("b0_payload") or {}).get("cast_claims") or []):
            payload = {**_clone(claim), "trust": "untrusted"}
            collector.add(
                channel="cast_claim_inputs",
                kind="cast_claim",
                chapter_id=chapter_id,
                chapter_index=chapter_index,
                source_ordinal=ordinal,
                evidence_refs=_block_refs(claim.get("source_block_ids") or []),
                payload=payload,
                source_identity=m1_identity,
                positions=positions,
            )

        for ordinal, glossary, _window_id in _iter_window_payloads(
            m1_state.get("b1_by_window") or [], "glossary_candidates"
        ):
            collector.add(
                channel="glossary_inputs",
                kind="glossary",
                chapter_id=chapter_id,
                chapter_index=chapter_index,
                source_ordinal=ordinal,
                evidence_refs=_block_refs(glossary.get("block_ids") or []),
                payload=glossary,
                source_identity=m1_identity,
                positions=positions,
            )

        event_by_id: dict[str, dict[str, Any]] = {}
        for ordinal, turn, _window_id in _iter_window_payloads(
            m1_state.get("b2_by_window") or [], "speaker_turns"
        ):
            turn_id = str(turn.get("turn_id") or "")
            speaker = turn.get("speaker") or {}
            addressee = turn.get("addressee")
            refs = [
                _ref("turn", turn_id),
                _ref("block", str(turn.get("block_id") or "")),
                _ref("endpoint", str(speaker.get("endpoint_id") or ""), "speaker"),
            ]
            if isinstance(addressee, Mapping):
                refs.append(
                    _ref(
                        "endpoint",
                        str(addressee.get("endpoint_id") or ""),
                        "addressee",
                    )
                )
            refs.extend(
                _ref("address_occurrence", str(term.get("address_occurrence_id") or ""))
                for term in turn.get("address_terms") or []
            )
            collector.add(
                channel="dialogue_turn_inputs",
                kind="dialogue_turn",
                chapter_id=chapter_id,
                chapter_index=chapter_index,
                source_ordinal=ordinal,
                evidence_refs=refs,
                payload=turn,
                source_identity=m1_identity,
                positions=positions,
            )

        for ordinal, event, _window_id in _iter_window_payloads(
            m1_state.get("b2_by_window") or [], "relation_events"
        ):
            event_id = str(event.get("event_id") or "")
            if not event_id or event_id in event_by_id:
                raise B4HandoffError(f"duplicate/empty relation event id: {event_id!r}")
            event_by_id[event_id] = _clone(event)
            actor = event.get("actor") or {}
            target = event.get("target") or {}
            refs = [
                _ref("event", event_id),
                _ref("block", str(event.get("block_id") or "")),
                *_endpoint_refs(
                    str(actor.get("endpoint_id") or ""),
                    str(target.get("endpoint_id") or ""),
                    "actor",
                    "target",
                ),
            ]
            collector.add(
                channel="relation_event_inputs",
                kind="relation_event",
                chapter_id=chapter_id,
                chapter_index=chapter_index,
                source_ordinal=ordinal,
                evidence_refs=refs,
                payload=event,
                source_identity=m1_identity,
                positions=positions,
            )

        for ordinal, observation in enumerate(digest.get("relation_observations") or []):
            event_id = str(observation.get("event_id") or "")
            event = event_by_id.get(event_id)
            if event is None:
                raise B4HandoffError(f"phase observation references foreign event: {event_id}")
            expected_refs = [
                str((event.get("actor") or {}).get("endpoint_id") or ""),
                str((event.get("target") or {}).get("endpoint_id") or ""),
            ]
            if observation.get("endpoint_refs") != expected_refs or observation.get(
                "block_id"
            ) != event.get("block_id"):
                raise B4HandoffError(f"phase observation/event topology mismatch: {event_id}")
            collector.add(
                channel="phase_observation_inputs",
                kind="phase_observation",
                chapter_id=chapter_id,
                chapter_index=chapter_index,
                source_ordinal=ordinal,
                evidence_refs=[
                    _ref("event", event_id),
                    _ref("block", str(observation.get("block_id") or "")),
                    *_endpoint_refs(*expected_refs, "actor", "target"),
                ],
                payload=observation,
                source_identity=m2_identity,
                positions=positions,
            )

        for ordinal, state in enumerate(digest.get("character_state_changes") or []):
            trigger = str(state.get("trigger_ref") or "")
            trigger_ref = (
                _ref("event", trigger)
                if ("event", trigger) in positions
                else _ref("block", trigger)
            )
            collector.add(
                channel="state_change_inputs",
                kind="state_change",
                chapter_id=chapter_id,
                chapter_index=chapter_index,
                source_ordinal=ordinal,
                evidence_refs=[
                    _occurrence_ref(str(state.get("subject_ref") or ""), occurrence_kinds),
                    trigger_ref,
                ],
                payload=state,
                source_identity=m2_identity,
                positions=positions,
            )

        for ordinal, thread in enumerate(digest.get("unresolved_threads") or []):
            refs = [_ref("block", str(thread.get("opened_block") or ""))]
            refs.extend(
                _occurrence_ref(str(value), occurrence_kinds)
                for value in thread.get("subject_refs") or []
            )
            collector.add(
                channel="unresolved_thread_inputs",
                kind="unresolved_thread",
                chapter_id=chapter_id,
                chapter_index=chapter_index,
                source_ordinal=ordinal,
                evidence_refs=refs,
                payload=thread,
                source_identity=m2_identity,
                positions=positions,
            )

        for ordinal, fact in enumerate(digest.get("translator_relevant_facts") or []):
            refs = _block_refs(fact.get("block_evidence") or [])
            if fact.get("subject_ref") is not None:
                refs.append(
                    _occurrence_ref(str(fact.get("subject_ref") or ""), occurrence_kinds)
                )
            refs.extend(_ref("event", str(value)) for value in fact.get("event_ids") or [])
            collector.add(
                channel="translator_fact_inputs",
                kind="translator_fact",
                chapter_id=chapter_id,
                chapter_index=chapter_index,
                source_ordinal=ordinal,
                evidence_refs=refs,
                payload=fact,
                source_identity=m2_identity,
                positions=positions,
            )

        for ordinal, motif in enumerate(digest.get("motifs") or []):
            refs = _block_refs(motif.get("block_ids") or [])
            refs.extend(
                _occurrence_ref(str(value), occurrence_kinds)
                for value in motif.get("subject_refs") or []
            )
            collector.add(
                channel="motif_inputs",
                kind="motif",
                chapter_id=chapter_id,
                chapter_index=chapter_index,
                source_ordinal=ordinal,
                evidence_refs=refs,
                payload=motif,
                source_identity=m2_identity,
                positions=positions,
            )

        collector.add(
            channel="rolling_summary_inputs",
            kind="rolling_summary",
            chapter_id=chapter_id,
            chapter_index=chapter_index,
            source_ordinal=0,
            evidence_refs=[],
            payload={
                "chapter_id": chapter_id,
                "chapter_rolling_summary": str(digest.get("chapter_rolling_summary") or ""),
            },
            source_identity=m2_identity,
            positions=positions,
        )

        for ordinal, frame in enumerate(digest.get("narration_frame_segments") or []):
            segment_id = str(frame.get("segment_id") or "")
            block_ids = _inclusive_block_ids(frame.get("block_range"), source_blocks)
            refs = [_ref("frame_segment", segment_id), *_block_refs(block_ids)]
            if frame.get("narrator_ref") is not None:
                refs.append(
                    _occurrence_ref(str(frame.get("narrator_ref") or ""), occurrence_kinds)
                )
            collector.add(
                channel="frame_claim_inputs",
                kind="frame_claim",
                chapter_id=chapter_id,
                chapter_index=chapter_index,
                source_ordinal=ordinal,
                evidence_refs=refs,
                payload=frame,
                source_identity=m2_identity,
                positions=positions,
            )

        _validate_frame_leaf_index(digest, source_blocks)
        leaf_payload = {
            "deepest_active_leaf_spans": _clone(digest.get("deepest_active_leaf_spans") or []),
            "deepest_active_leaf_by_block": _clone(
                digest.get("deepest_active_leaf_by_block") or {}
            ),
        }
        leaf_refs: list[dict[str, Any]] = []
        for span in leaf_payload["deepest_active_leaf_spans"]:
            leaf_refs.append(_ref("block", str(span.get("block_id") or "")))
            leaf_refs.append(
                _ref(
                    "frame_segment",
                    f"seg_{chapter_id}_{str(span.get('segment_key') or '')}",
                )
            )
        collector.add(
            channel="frame_leaf_index",
            kind="frame_leaf_index",
            chapter_id=chapter_id,
            chapter_index=chapter_index,
            source_ordinal=0,
            evidence_refs=leaf_refs,
            payload=leaf_payload,
            source_identity=m2_identity,
            positions=positions,
        )

    result = collector.finish()
    if set(result) - {"dedupe_counts"} != set(_CHANNELS):
        raise B4HandoffError("ground-evidence channel set is incomplete")
    return _clone(result)


def _contains_entity_identifier(value: Any) -> bool:
    if isinstance(value, str):
        return value.startswith("ent_")
    if isinstance(value, Mapping):
        return any(_contains_entity_identifier(item) for item in value.values())
    if isinstance(value, (list, tuple)):
        return any(_contains_entity_identifier(item) for item in value)
    return False


def _assert_no_authority_smuggling(value: Any) -> None:
    if isinstance(value, Mapping):
        for raw_key, item in value.items():
            key = str(raw_key)
            if key in _FORBIDDEN_AUTHORITY_KEYS:
                raise B4HandoffError(f"forbidden identity/decision field in Step-4 bundle: {key}")
            if (
                key == "id"
                or key.endswith(("_id", "_ids", "_ref", "_refs"))
            ) and _contains_entity_identifier(item):
                raise B4HandoffError(
                    f"forbidden entity identifier in Step-4 identifier field: {key}"
                )
            _assert_no_authority_smuggling(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            _assert_no_authority_smuggling(item)


def assemble_b4_input_bundle(
    document: Mapping[str, Any],
    chapters: Sequence[str],
    *,
    m1v3_dir: Path,
    m2v3_dir: Path,
    knowledge_mode: str = KNOWLEDGE_MODE,
    execution_mode: str = EXECUTION_MODE_SYNTHETIC,
    window_target_tokens: int = DEFAULT_WINDOW_TARGET_TOKENS,
    window_max_blocks: int = DEFAULT_WINDOW_MAX_BLOCKS,
    tail_k: int = DEFAULT_TAIL_K,
    summary_k: int = DEFAULT_SUMMARY_K,
) -> dict[str, Any]:
    """Build the complete internal B4 input bundle without persisting state."""

    inputs = load_verified_builder_v3_inputs(
        document,
        chapters,
        m1v3_dir=Path(m1v3_dir),
        m2v3_dir=Path(m2v3_dir),
        knowledge_mode=knowledge_mode,
        execution_mode=execution_mode,
        window_target_tokens=window_target_tokens,
        window_max_blocks=window_max_blocks,
        tail_k=tail_k,
        summary_k=summary_k,
    )
    cards = build_occurrence_cards(inputs)
    routing = build_occurrence_routing_view(inputs, cards)
    ground = build_complete_ground_evidence(inputs, cards)
    source_block_catalog = _build_source_block_catalog(inputs)
    summary_lineage = _summary_lineage(inputs)
    _validate_block_resolution(source_block_catalog, cards, ground)
    provenance = [
        {
            "chapter_id": str(row["chapter_id"]),
            "m1v3_identity_hash": str(row["m1v3_identity_hash"]),
            "m2v3_identity_hash": str(row["m2v3_identity_hash"]),
        }
        for row in inputs["chapters"]
    ]
    input_contract = _clone(inputs["config_identity"])
    input_identity_body = {
        "schema_version": BUNDLE_SCHEMA_VERSION,
        "handoff_contract_version": HANDOFF_CONTRACT_VERSION,
        "selected_chapters": list(inputs["selected_chapters"]),
        "knowledge_cutoff_scope": inputs["knowledge_cutoff_scope"],
        "scope_complete_book": bool(inputs["scope_complete_book"]),
        "input_contract": input_contract,
        "provenance": provenance,
    }
    bundle: dict[str, Any] = {
        "schema_version": BUNDLE_SCHEMA_VERSION,
        "handoff_contract_version": HANDOFF_CONTRACT_VERSION,
        "knowledge_mode": inputs["knowledge_mode"],
        "execution_mode": inputs["execution_mode"],
        "selected_chapters": list(inputs["selected_chapters"]),
        "knowledge_cutoff_scope": inputs["knowledge_cutoff_scope"],
        "scope_complete_book": bool(inputs["scope_complete_book"]),
        "input_contract": input_contract,
        "source_block_catalog": source_block_catalog,
        "summary_lineage": summary_lineage,
        "occurrence_cards": cards,
        "occurrence_routing": routing,
        "ground_evidence": ground,
        "provenance": provenance,
        "input_identity_manifest_hash": canonical_hash(input_identity_body),
    }
    _assert_no_authority_smuggling(bundle)
    bundle["bundle_manifest_hash"] = canonical_hash(bundle)
    return _clone(bundle)


__all__ = [
    "B4HandoffError",
    "BUNDLE_SCHEMA_VERSION",
    "HANDOFF_CONTRACT_VERSION",
    "VERIFIED_INPUTS_SCHEMA_VERSION",
    "assemble_b4_input_bundle",
    "build_complete_ground_evidence",
    "build_occurrence_cards",
    "build_occurrence_routing_view",
    "load_verified_builder_v3_inputs",
]
