"""Offline chapter-registry foundation for literary Builder identity.

This module is intentionally additive and 0-API. It renders auditable prompt
requests, validates synthetic responses, performs literal candidate selection,
offers a fake bounded tool broker, stages one chapter against an immutable
parent snapshot, and publishes an append-only generation with CAS.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from hashlib import sha256
import json
import os
from pathlib import Path
import re
from typing import Any, Callable, Mapping, Sequence
import unicodedata

from pipeline.literary.builder_pilot import load_system_prompt_from_design
from pipeline.literary.chapter_registry_schema_v1 import (
    AliasBindingV1,
    BrokerContextRequestV1,
    CandidateSelectionManifestV1,
    DECISION_FIELDS,
    EntityRecordV1,
    EVIDENCE_CLASSES,
    IDENTITY_KINDS,
    IdentityProposalV1,
    ModelContextRequestDraftV1,
    NONCHARACTER_KINDS,
    OccurrenceRecordV1,
    PendingReferentV1,
    PENDING_REASONS,
    PresenceRowV1,
    REFERENT_KINDS,
    RegistryContractError,
    RegistryGenerationV1,
    StoryPositionV1,
)
from pipeline.literary.checkpoint import (
    CheckpointLock,
    canonical_hash,
    canonical_json,
    write_checkpoint_atomic,
)
from pipeline.literary.source_anchor import SourceAnchor, locate_anchor, nfc_block_string


BUILDER_IDENTITY_MODE = "chapter_registry_v1"
DEFAULT_BUILDER_IDENTITY_MODE = "v3_b0less"
REGISTRY_SCHEMA_VERSION = "chapter_registry_schema_v1"
REGISTRY_VALIDATOR_CONTRACT_HASH = canonical_hash(
    {
        "schema": REGISTRY_SCHEMA_VERSION,
        "occurrence_id": "content_addressed_source_anchor_v1",
        "candidate_policy": "literal_as_of_snapshot_candidates_v2",
        "resolver_policy": "model_rejects_candidate_universe_v2",
        "commit": "append_only_chapter_cas_v1",
    }
)
ORIENT_PROMPT_ID = "literary_chapter_orient_v1"
EXTRACT_PROMPT_ID = "literary_registry_extract_v1"
RESOLVE_PROMPT_ID = "literary_registry_resolve_v1"
EXECUTION_MODE_SYNTHETIC = "synthetic"
MAX_TOOL_ROUNDS = 2
ALLOWED_TOOLS = frozenset(
    {
        "find_entity_candidates",
        "get_entity_evidence",
        "get_pending_evidence",
        "get_source_context",
    }
)
_SAFE_ID = re.compile(r"^[A-Za-z0-9_.-]+$")


class RegistryStoreError(RegistryContractError):
    """Raised for malformed/tampered append-only registry artifacts."""


class RegistryStaleParentError(RegistryStoreError):
    """Raised when a chapter commit loses the lineage CAS race."""


def _clone(value: Any) -> Any:
    return json.loads(canonical_json(value))


def _require_exact_keys(value: Mapping[str, Any], expected: set[str], label: str) -> None:
    actual = set(value)
    if actual != expected:
        raise RegistryContractError(
            f"{label} fields mismatch: missing={sorted(expected-actual)}, "
            f"extra={sorted(actual-expected)}"
        )


def _required_str(value: Any, label: str) -> str:
    rendered = str(value or "").strip()
    if not rendered:
        raise RegistryContractError(f"{label} must be non-empty")
    return rendered


def _safe_id(value: str, label: str) -> str:
    if not value or not _SAFE_ID.fullmatch(value):
        raise RegistryStoreError(f"unsafe {label}: {value!r}")
    return value


def _block_wire(block: Mapping[str, Any], order_index: int) -> dict[str, Any]:
    return {
        "block_id": _required_str(block.get("block_id"), "block_id"),
        "order_index": order_index,
        "block_type": str(block.get("block_type") or "paragraph"),
        "text": nfc_block_string(block),
    }


def chapter_block_views(chapter: Mapping[str, Any]) -> list[dict[str, Any]]:
    return [
        _block_wire(block, index)
        for index, block in enumerate(chapter.get("blocks") or [])
    ]


def _block_map(blocks: Sequence[Mapping[str, Any]]) -> dict[str, Mapping[str, Any]]:
    result: dict[str, Mapping[str, Any]] = {}
    for block in blocks:
        block_id = _required_str(block.get("block_id"), "block_id")
        if block_id in result:
            raise RegistryContractError(f"duplicate source block id: {block_id}")
        result[block_id] = block
    return result


def _snapshot_body(snapshot: Mapping[str, Any]) -> dict[str, Any]:
    body = dict(snapshot)
    body.pop("snapshot_hash", None)
    return body


def empty_registry_snapshot(state_lineage_id: str) -> dict[str, Any]:
    body = {
        "builder_identity_mode": BUILDER_IDENTITY_MODE,
        "schema_version": REGISTRY_SCHEMA_VERSION,
        "state_lineage_id": _safe_id(state_lineage_id, "state_lineage_id"),
        "generation_id": None,
        "entities": [],
        "pending_records": [],
        "occurrences": [],
        "presence_rows": [],
    }
    return body | {"snapshot_hash": canonical_hash(body)}


def verify_registry_snapshot(snapshot: Mapping[str, Any]) -> None:
    expected = {
        "builder_identity_mode",
        "schema_version",
        "state_lineage_id",
        "generation_id",
        "entities",
        "pending_records",
        "occurrences",
        "presence_rows",
        "snapshot_hash",
    }
    _require_exact_keys(snapshot, expected, "registry snapshot")
    if snapshot.get("builder_identity_mode") != BUILDER_IDENTITY_MODE:
        raise RegistryContractError("foreign builder identity mode")
    if snapshot.get("schema_version") != REGISTRY_SCHEMA_VERSION:
        raise RegistryContractError("foreign registry schema version")
    own_hash = _required_str(snapshot.get("snapshot_hash"), "snapshot_hash")
    if canonical_hash(_snapshot_body(snapshot)) != own_hash:
        raise RegistryContractError("registry snapshot hash mismatch")
    _validate_registry_snapshot_rows(snapshot)


def load_snapshot_from_handle(handle: Mapping[str, Any]) -> dict[str, Any]:
    """Fail closed instead of silently resuming a v3/B0-less checkpoint."""

    snapshot = _clone(handle)
    verify_registry_snapshot(snapshot)
    return snapshot


@dataclass(frozen=True, slots=True)
class RegistryRequestV1:
    role: str
    chapter_id: str
    request_key: str
    prompt_id: str
    system_prompt: str
    state_lineage_id: str
    parent_snapshot_hash: str
    chapter_order: int
    sections: Mapping[str, Any]
    request_fingerprint: str

    @staticmethod
    def _prompt_sha256(system_prompt: str) -> str:
        return sha256(system_prompt.encode("utf-8")).hexdigest()

    @classmethod
    def create(
        cls,
        *,
        role: str,
        chapter_id: str,
        request_key: str,
        prompt_id: str,
        system_prompt: str,
        state_lineage_id: str,
        parent_snapshot_hash: str,
        chapter_order: int,
        sections: Mapping[str, Any],
    ) -> "RegistryRequestV1":
        body = {
            "builder_identity_mode": BUILDER_IDENTITY_MODE,
            "execution_mode": EXECUTION_MODE_SYNTHETIC,
            "role": role,
            "chapter_id": chapter_id,
            "request_key": request_key,
            "prompt_id": prompt_id,
            "prompt_sha256": cls._prompt_sha256(system_prompt),
            "state_lineage_id": state_lineage_id,
            "parent_snapshot_hash": parent_snapshot_hash,
            "chapter_order": chapter_order,
            "sections": _clone(sections),
        }
        return cls(
            role=role,
            chapter_id=chapter_id,
            request_key=request_key,
            prompt_id=prompt_id,
            system_prompt=system_prompt,
            state_lineage_id=state_lineage_id,
            parent_snapshot_hash=parent_snapshot_hash,
            chapter_order=chapter_order,
            sections=_clone(sections),
            request_fingerprint=canonical_hash(body),
        )

    def body(self) -> dict[str, Any]:
        return {
            "builder_identity_mode": BUILDER_IDENTITY_MODE,
            "execution_mode": EXECUTION_MODE_SYNTHETIC,
            "role": self.role,
            "chapter_id": self.chapter_id,
            "request_key": self.request_key,
            "prompt_id": self.prompt_id,
            "prompt_sha256": self._prompt_sha256(self.system_prompt),
            "state_lineage_id": self.state_lineage_id,
            "parent_snapshot_hash": self.parent_snapshot_hash,
            "chapter_order": self.chapter_order,
            "sections": _clone(self.sections),
        }


class SyntheticRegistryExecutor:
    """Deterministic scripted executor; no provider client or network seam exists."""

    def __init__(
        self,
        scripted: Mapping[
            tuple[str, str],
            Mapping[str, Any] | Callable[[RegistryRequestV1], Mapping[str, Any]],
        ],
    ) -> None:
        self._scripted = dict(scripted)
        self.call_log: list[dict[str, Any]] = []

    def execute(self, request: RegistryRequestV1) -> dict[str, Any]:
        body = request.body()
        if canonical_hash(body) != request.request_fingerprint:
            raise RegistryContractError("synthetic request fingerprint mismatch")
        key = (request.role, request.request_key)
        self.call_log.append(
            {
                "role": request.role,
                "request_key": request.request_key,
                "parent_snapshot_hash": request.parent_snapshot_hash,
                "request_fingerprint": request.request_fingerprint,
                "body": body,
            }
        )
        scripted_key = key if key in self._scripted else (request.role, "*")
        if scripted_key not in self._scripted:
            raise RegistryContractError(f"missing synthetic payload: {key}")
        scripted = self._scripted[scripted_key]
        return _clone(scripted(request) if callable(scripted) else scripted)


def render_orientation_request(
    *,
    design_doc: Path,
    chapter: Mapping[str, Any],
    chapter_order: int,
    snapshot: Mapping[str, Any],
) -> RegistryRequestV1:
    verify_registry_snapshot(snapshot)
    chapter_id = _required_str(chapter.get("chapter_id"), "chapter_id")
    sections = {"chapter_blocks": chapter_block_views(chapter)}
    return RegistryRequestV1.create(
        role="b0_orient",
        chapter_id=chapter_id,
        request_key=f"b0:{chapter_id}",
        prompt_id=ORIENT_PROMPT_ID,
        system_prompt=load_system_prompt_from_design(design_doc, ORIENT_PROMPT_ID),
        state_lineage_id=str(snapshot["state_lineage_id"]),
        parent_snapshot_hash=str(snapshot["snapshot_hash"]),
        chapter_order=chapter_order,
        sections=sections,
    )


def _normalized_literal(value: str) -> str:
    normalized = unicodedata.normalize("NFC", value).casefold()
    normalized = re.sub(r"[^\w]+", " ", normalized, flags=re.UNICODE)
    return " ".join(normalized.split())


def _literal_channel(surface: str, source_text: str) -> str | None:
    if not surface:
        return None

    def contains_token_span(needle: str, haystack: str) -> bool:
        left = r"(?<!\w)" if needle[:1].isalnum() or needle.startswith("_") else ""
        right = r"(?!\w)" if needle[-1:].isalnum() or needle.endswith("_") else ""
        return re.search(left + re.escape(needle) + right, haystack) is not None

    if contains_token_span(surface, source_text):
        return "exact"
    if contains_token_span(surface.casefold(), source_text.casefold()):
        return "casefold"
    needle = _normalized_literal(surface)
    normalized_source = _normalized_literal(source_text)
    if needle and f" {needle} " in f" {normalized_source} ":
        return "normalized_literal"
    return None


def _story_position_tuple(value: Mapping[str, Any]) -> tuple[int, int, int]:
    return (
        int(value.get("chapter_order") or 0),
        int(value.get("block_order") or 0),
        int(value.get("char_offset") or 0),
    )


def _alias_can_apply_to_window(
    alias: Mapping[str, Any],
    *,
    chapter_order: int,
    active_blocks: Sequence[Mapping[str, Any]],
) -> bool:
    valid_from = alias.get("world_valid_from")
    valid_until = alias.get("world_valid_until")
    lower = _story_position_tuple(valid_from) if isinstance(valid_from, Mapping) else None
    upper = _story_position_tuple(valid_until) if isinstance(valid_until, Mapping) else None
    for block in active_blocks:
        block_order = int(block.get("order_index", -1))
        if block_order < 0:
            raise RegistryContractError("candidate block lacks a global order_index")
        text = str(block.get("text") or nfc_block_string(block))
        block_start = (chapter_order, block_order, 0)
        block_end = (chapter_order, block_order, len(text) + 1)
        if (lower is None or lower < block_end) and (
            upper is None or block_start < upper
        ):
            return True
    return False


def _best_literal_channel(
    surface: str, active_blocks: Sequence[Mapping[str, Any]]
) -> str | None:
    rank = {"exact": 0, "casefold": 1, "normalized_literal": 2}
    channels = {
        channel
        for block in active_blocks
        if (
            channel := _literal_channel(
                surface, str(block.get("text") or nfc_block_string(block))
            )
        )
        is not None
    }
    return min(channels, key=rank.__getitem__) if channels else None


def select_candidate_cards(
    *,
    active_blocks: Sequence[Mapping[str, Any]],
    snapshot: Mapping[str, Any],
    chapter_order: int,
    cap: int,
) -> dict[str, Any]:
    """Generate candidates from literal source scans without binding anything."""

    verify_registry_snapshot(snapshot)
    if chapter_order < 0:
        raise RegistryContractError("candidate chapter order must be non-negative")
    if not active_blocks:
        raise RegistryContractError("candidate selection needs active blocks")
    if cap < 1:
        raise RegistryContractError("candidate cap must be positive")
    active_block_orders = tuple(
        sorted({int(block.get("order_index", -1)) for block in active_blocks})
    )
    if not active_block_orders or active_block_orders[0] < 0:
        raise RegistryContractError("candidate blocks need non-negative order_index values")
    occurrences = {
        str(row.get("occurrence_id")): row for row in snapshot.get("occurrences") or []
    }
    matches: list[dict[str, Any]] = []
    cards: dict[tuple[str, str], dict[str, Any]] = {}

    for entity in snapshot.get("entities") or []:
        if entity.get("status") != "active":
            continue
        active_aliases = [
            row
            for row in entity.get("aliases") or []
            if _alias_can_apply_to_window(
                row,
                chapter_order=chapter_order,
                active_blocks=active_blocks,
            )
        ]
        surfaces = [str(entity.get("canonical_surface") or "")]
        surfaces.extend(str(row.get("surface") or "") for row in active_aliases)
        channels = [
            (surface, channel)
            for surface in surfaces
            if (channel := _best_literal_channel(surface, active_blocks)) is not None
        ]
        if not channels:
            continue
        channel_rank = {"exact": 0, "casefold": 1, "normalized_literal": 2}
        surface, channel = sorted(channels, key=lambda item: (channel_rank[item[1]], item[0]))[0]
        entity_id = _required_str(entity.get("entity_id"), "candidate entity_id")
        card = {
            "card_kind": "entity",
            "entity_id": entity_id,
            "referent_kind": entity.get("referent_kind"),
            "canonical_surface": entity.get("canonical_surface"),
            "aliases": [
                {
                    "surface": row.get("surface"),
                    "world_valid_from": row.get("world_valid_from"),
                    "world_valid_until": row.get("world_valid_until"),
                }
                for row in active_aliases
            ],
            "current_revision_hash": entity.get("current_revision_hash"),
        }
        cards[("entity", entity_id)] = card
        matches.append(
            {
                "ref_kind": "entity",
                "ref_id": entity_id,
                "channel": channel,
                "matched_surface": surface,
                "rank": channel_rank[channel],
                "source_row_hash": canonical_hash(entity),
            }
        )

    for pending in snapshot.get("pending_records") or []:
        if pending.get("status") != "open":
            continue
        surfaces = sorted(
            {
                str(occurrences[occurrence_id].get("surface") or "")
                for occurrence_id in pending.get("occurrence_ids") or []
                if occurrence_id in occurrences
            }
        )
        channels = [
            (surface, channel)
            for surface in surfaces
            if (channel := _best_literal_channel(surface, active_blocks)) is not None
        ]
        if not channels:
            continue
        channel_rank = {"exact": 0, "casefold": 1, "normalized_literal": 2}
        surface, channel = sorted(channels, key=lambda item: (channel_rank[item[1]], item[0]))[0]
        pending_id = _required_str(pending.get("pending_id"), "candidate pending_id")
        card = {
            "card_kind": "pending",
            "pending_id": pending_id,
            "surfaces": surfaces,
            "candidate_entity_refs": list(pending.get("candidate_entity_refs") or []),
            "reason_code": pending.get("reason_code"),
        }
        cards[("pending", pending_id)] = card
        matches.append(
            {
                "ref_kind": "pending",
                "ref_id": pending_id,
                "channel": channel,
                "matched_surface": surface,
                "rank": channel_rank[channel],
                "source_row_hash": canonical_hash(pending),
            }
        )

    ordered = sorted(
        matches,
        key=lambda row: (int(row["rank"]), str(row["ref_kind"]), str(row["ref_id"])),
    )
    selected_rows = ordered[:cap]
    selected_entity_ids = tuple(
        str(row["ref_id"]) for row in selected_rows if row["ref_kind"] == "entity"
    )
    selected_pending_ids = tuple(
        str(row["ref_id"]) for row in selected_rows if row["ref_kind"] == "pending"
    )
    selection_universe_hash = canonical_hash(ordered)
    manifest_body = {
        "snapshot_hash": str(snapshot["snapshot_hash"]),
        "as_of_chapter_order": chapter_order,
        "active_block_orders": list(active_block_orders),
        "selected_entity_ids": list(selected_entity_ids),
        "selected_pending_ids": list(selected_pending_ids),
        "rows": selected_rows,
        "total_matches": len(ordered),
        "cap": cap,
        "truncated": len(ordered) > cap,
        "overflow": len(ordered) > cap,
        "selection_universe_hash": selection_universe_hash,
    }
    manifest = CandidateSelectionManifestV1(
        snapshot_hash=str(snapshot["snapshot_hash"]),
        as_of_chapter_order=chapter_order,
        active_block_orders=active_block_orders,
        selected_entity_ids=selected_entity_ids,
        selected_pending_ids=selected_pending_ids,
        rows=tuple(selected_rows),
        total_matches=len(ordered),
        cap=cap,
        truncated=len(ordered) > cap,
        overflow=len(ordered) > cap,
        selection_universe_hash=selection_universe_hash,
        manifest_hash=canonical_hash(manifest_body),
    )
    return {
        "candidate_cards": [cards[("entity", item)] for item in selected_entity_ids],
        "open_pending_cards": [cards[("pending", item)] for item in selected_pending_ids],
        "candidate_selection_manifest": manifest.to_dict(),
    }


def render_extract_request(
    *,
    design_doc: Path,
    chapter_id: str,
    chapter_order: int,
    request_key: str,
    window_id: str,
    active_blocks: Sequence[Mapping[str, Any]],
    context_only_tail: Sequence[Mapping[str, Any]],
    snapshot: Mapping[str, Any],
    candidate_cap: int,
    targeted_recall_evidence: Mapping[str, Any] | None = None,
) -> RegistryRequestV1:
    selection = select_candidate_cards(
        active_blocks=active_blocks,
        snapshot=snapshot,
        chapter_order=chapter_order,
        cap=candidate_cap,
    )
    sections: dict[str, Any] = {
        "window_id": window_id,
        "active_window_blocks": _clone(active_blocks),
        "context_only_tail": _clone(context_only_tail),
        **selection,
    }
    if targeted_recall_evidence is not None:
        evidence = dict(targeted_recall_evidence)
        forbidden = {
            "entity_id",
            "candidate_entity_id",
            "target_entity_id",
            "alias_binding",
            "referent_kind_claim",
        }
        if forbidden & set(evidence):
            raise RegistryContractError("targeted recall evidence contains an identity answer")
        sections["targeted_recall_evidence"] = _clone(evidence)
    request = RegistryRequestV1.create(
        role="b1_targeted" if targeted_recall_evidence is not None else "b1_extract",
        chapter_id=chapter_id,
        request_key=request_key,
        prompt_id=EXTRACT_PROMPT_ID,
        system_prompt=load_system_prompt_from_design(design_doc, EXTRACT_PROMPT_ID),
        state_lineage_id=str(snapshot["state_lineage_id"]),
        parent_snapshot_hash=str(snapshot["snapshot_hash"]),
        chapter_order=chapter_order,
        sections=sections,
    )
    serialized = canonical_json(request.body())
    for forbidden_token in (
        "salient_surface_checklist",
        "narrator_hypotheses",
        "chapter_orientation",
        "b0_cast",
    ):
        if forbidden_token in serialized:
            raise RegistryContractError("B0 semantic content leaked into ordinary B1")
    return request


def _locate_row(
    row: Mapping[str, Any],
    *,
    blocks: Mapping[str, Mapping[str, Any]],
    label: str,
) -> SourceAnchor:
    block_id = _required_str(row.get("block_id"), f"{label} block_id")
    if block_id not in blocks:
        raise RegistryContractError(f"{label} cites foreign block: {block_id}")
    hint_value = row.get("occurrence_hint")
    hint = int(hint_value) if hint_value is not None else None
    coordinate_block = dict(blocks[block_id])
    if not coordinate_block.get("clean_text") and not coordinate_block.get("source_text"):
        coordinate_block["source_text"] = str(coordinate_block.get("text") or "")
    located = locate_anchor(
        coordinate_block,
        anchor_text=_required_str(row.get("anchor_text"), f"{label} anchor_text"),
        evidence_quote=_required_str(
            row.get("evidence_quote"), f"{label} evidence_quote"
        ),
        occurrence_hint=hint,
    )
    if not located.ok or located.anchor is None:
        raise RegistryContractError(
            f"{label} failed closed source location: {located.failure_reason}"
        )
    return located.anchor


def validate_orientation_response(
    response: Mapping[str, Any],
    *,
    request: RegistryRequestV1,
) -> dict[str, Any]:
    expected_top = {
        "chapter_id",
        "gist",
        "setting_notes",
        "narrator_hypotheses",
        "salient_surface_checklist",
    }
    _require_exact_keys(response, expected_top, "chapter orientation")
    if response.get("chapter_id") != request.chapter_id:
        raise RegistryContractError("orientation chapter mismatch")
    blocks = _block_map(request.sections.get("chapter_blocks") or [])
    normalized = _clone(response)
    for field_name in ("setting_notes", "narrator_hypotheses"):
        located_rows: list[dict[str, Any]] = []
        for row in response.get(field_name) or []:
            expected = {
                "claim",
                "surface",
                "anchor_text",
                "evidence_quote",
                "block_id",
                "occurrence_hint",
            }
            _require_exact_keys(row, expected, field_name)
            anchor = _locate_row(row, blocks=blocks, label=field_name)
            located_rows.append(_clone(row) | {"anchor": anchor.to_dict()})
        normalized[field_name] = located_rows
    checklist: list[dict[str, Any]] = []
    for row in response.get("salient_surface_checklist") or []:
        expected = {
            "surface",
            "salience_note",
            "anchor_text",
            "evidence_quote",
            "block_id",
            "occurrence_hint",
        }
        _require_exact_keys(row, expected, "salient checklist")
        if row.get("anchor_text") != row.get("surface"):
            raise RegistryContractError(
                "salient checklist anchor_text must equal surface exactly"
            )
        anchor = _locate_row(row, blocks=blocks, label="salient checklist")
        checklist.append(_clone(row) | {"anchor": anchor.to_dict()})
    normalized["salient_surface_checklist"] = checklist
    normalized["orientation_hash"] = canonical_hash(normalized)
    return normalized


def routing_for_kind(kind: str) -> str:
    if kind in IDENTITY_KINDS:
        return "identity_registry"
    if kind in NONCHARACTER_KINDS:
        return "noncharacter_index"
    if kind == "unknown":
        return "pending_kind"
    raise RegistryContractError(f"unknown referent kind: {kind}")


def mint_occurrence_id(
    *,
    state_lineage_id: str,
    chapter_id: str,
    anchor: SourceAnchor,
    surface: str,
) -> str:
    """Stable under later targeted-recall additions to the same block."""

    body = {
        "state_lineage_id": state_lineage_id,
        "chapter_id": chapter_id,
        "anchor": anchor.to_dict(),
        "surface": unicodedata.normalize("NFC", surface),
    }
    return "occ_" + canonical_hash(body)[:20]


def _identity_proposal(value: Mapping[str, Any]) -> IdentityProposalV1:
    expected = {
        "operation",
        "target_entity_id",
        "canonical_surface_candidate",
        "alias_surface",
        "reason_code",
        "binding_evidence_quote",
        "binding_anchor_text",
        "binding_block_id",
        "binding_occurrence_hint",
        "retrieval_trace_ids",
    }
    _require_exact_keys(value, expected, "identity proposal")
    return IdentityProposalV1(
        operation=str(value.get("operation") or ""),  # type: ignore[arg-type]
        target_entity_id=(
            str(value["target_entity_id"])
            if value.get("target_entity_id") is not None
            else None
        ),
        canonical_surface_candidate=(
            str(value["canonical_surface_candidate"])
            if value.get("canonical_surface_candidate") is not None
            else None
        ),
        alias_surface=(
            str(value["alias_surface"]) if value.get("alias_surface") is not None else None
        ),
        reason_code=str(value.get("reason_code") or ""),
        binding_evidence_quote=(
            str(value["binding_evidence_quote"])
            if value.get("binding_evidence_quote") is not None
            else None
        ),
        binding_anchor_text=(
            str(value["binding_anchor_text"])
            if value.get("binding_anchor_text") is not None
            else None
        ),
        binding_block_id=(
            str(value["binding_block_id"])
            if value.get("binding_block_id") is not None
            else None
        ),
        binding_occurrence_hint=(
            int(value["binding_occurrence_hint"])
            if value.get("binding_occurrence_hint") is not None
            else None
        ),
        retrieval_trace_ids=tuple(str(item) for item in value.get("retrieval_trace_ids") or []),
    )


def validate_extract_response(
    response: Mapping[str, Any],
    *,
    request: RegistryRequestV1,
) -> dict[str, Any]:
    expected_top = {
        "chapter_id",
        "window_block_ids",
        "context_only_used",
        "character_mentions",
        "glossary_candidates",
    }
    _require_exact_keys(response, expected_top, "registry extract response")
    if response.get("chapter_id") != request.chapter_id:
        raise RegistryContractError("extract chapter mismatch")
    if not isinstance(response.get("context_only_used"), bool):
        raise RegistryContractError("context_only_used must be boolean")
    active_blocks = request.sections.get("active_window_blocks") or []
    blocks = _block_map(active_blocks)
    active_ids = list(blocks)
    if list(response.get("window_block_ids") or []) != active_ids:
        raise RegistryContractError("extract window ids do not match rendered active blocks")
    manifest = request.sections.get("candidate_selection_manifest") or {}
    if manifest.get("snapshot_hash") != request.parent_snapshot_hash:
        raise RegistryContractError("candidate manifest snapshot drift")
    expected_manifest_fields = {
        "snapshot_hash",
        "as_of_chapter_order",
        "active_block_orders",
        "selected_entity_ids",
        "selected_pending_ids",
        "rows",
        "total_matches",
        "cap",
        "truncated",
        "overflow",
        "selection_universe_hash",
        "manifest_hash",
    }
    _require_exact_keys(manifest, expected_manifest_fields, "candidate manifest")
    CandidateSelectionManifestV1(
        snapshot_hash=str(manifest.get("snapshot_hash") or ""),
        as_of_chapter_order=int(manifest.get("as_of_chapter_order", -1)),
        active_block_orders=tuple(
            int(item) for item in manifest.get("active_block_orders") or []
        ),
        selected_entity_ids=tuple(
            str(item) for item in manifest.get("selected_entity_ids") or []
        ),
        selected_pending_ids=tuple(
            str(item) for item in manifest.get("selected_pending_ids") or []
        ),
        rows=tuple(manifest.get("rows") or []),
        total_matches=int(manifest.get("total_matches", -1)),
        cap=int(manifest.get("cap", 0)),
        truncated=manifest.get("truncated") is True,
        overflow=manifest.get("overflow") is True,
        selection_universe_hash=str(manifest.get("selection_universe_hash") or ""),
        manifest_hash=str(manifest.get("manifest_hash") or ""),
    )
    manifest_body = {
        key: _clone(value)
        for key, value in manifest.items()
        if key != "manifest_hash"
    }
    if canonical_hash(manifest_body) != manifest.get("manifest_hash"):
        raise RegistryContractError("candidate manifest hash mismatch")
    expected_active_orders = sorted(
        int(row["order_index"]) for row in active_blocks
    )
    if int(manifest.get("as_of_chapter_order")) != request.chapter_order or list(
        manifest.get("active_block_orders") or []
    ) != expected_active_orders:
        raise RegistryContractError("candidate manifest as-of scope drift")
    candidate_ids = {
        str(row.get("entity_id"))
        for row in request.sections.get("candidate_cards") or []
    }
    pending_candidate_ids = {
        str(row.get("pending_id"))
        for row in request.sections.get("open_pending_cards") or []
    }
    if candidate_ids != set(manifest.get("selected_entity_ids") or []):
        raise RegistryContractError("candidate cards disagree with their manifest")
    if pending_candidate_ids != set(manifest.get("selected_pending_ids") or []):
        raise RegistryContractError("pending cards disagree with their manifest")
    block_orders = {str(row["block_id"]): int(row["order_index"]) for row in active_blocks}
    normalized_mentions: list[dict[str, Any]] = []

    for index, raw in enumerate(response.get("character_mentions") or []):
        expected = {
            "surface",
            "mention_type",
            "referent_kind_claim",
            "anchor_text",
            "evidence_quote",
            "block_id",
            "occurrence_hint",
            "decision_status",
            "identity_proposal",
            "context_requests",
        }
        _require_exact_keys(raw, expected, f"character mention {index}")
        if {"mention_id", "occurrence_id", "routing_disposition"} & set(raw):
            raise RegistryContractError("model attempted to emit code-owned mention fields")
        kind = str(raw.get("referent_kind_claim") or "")
        if kind not in REFERENT_KINDS:
            raise RegistryContractError(f"unknown referent kind: {kind}")
        mention_type = str(raw.get("mention_type") or "")
        if mention_type not in {"name", "nickname", "descriptor"}:
            raise RegistryContractError(f"unknown mention type: {mention_type}")
        surface = _required_str(raw.get("surface"), "mention surface")
        if raw.get("anchor_text") != surface:
            raise RegistryContractError("mention anchor_text must equal surface exactly")
        anchor = _locate_row(raw, blocks=blocks, label=f"mention {index}")
        occurrence_id = mint_occurrence_id(
            state_lineage_id=request.state_lineage_id,
            chapter_id=request.chapter_id,
            anchor=anchor,
            surface=surface,
        )
        route = routing_for_kind(kind)
        status = str(raw.get("decision_status") or "")
        if status not in {"decided", "needs_context"}:
            raise RegistryContractError(f"unknown decision_status: {status}")
        raw_proposal = raw.get("identity_proposal")
        proposal = _identity_proposal(raw_proposal) if isinstance(raw_proposal, Mapping) else None
        context_requests: list[ModelContextRequestDraftV1] = []
        for context_value in raw.get("context_requests") or []:
            expected_context = {
                "mention_index",
                "decision_field",
                "surface",
                "reason",
                "block_id",
                "needed_evidence",
            }
            _require_exact_keys(context_value, expected_context, "model context request")
            context_request = ModelContextRequestDraftV1(
                mention_index=int(context_value.get("mention_index", -1)),
                decision_field=str(context_value.get("decision_field") or ""),  # type: ignore[arg-type]
                surface=str(context_value.get("surface") or ""),
                reason=str(context_value.get("reason") or ""),
                block_id=str(context_value.get("block_id") or ""),
                needed_evidence=tuple(
                    str(item) for item in context_value.get("needed_evidence") or []
                ),  # type: ignore[arg-type]
            )
            if context_request.mention_index != index:
                raise RegistryContractError("context request points to a foreign mention index")
            if context_request.surface != surface or context_request.block_id != anchor.block_id:
                raise RegistryContractError("context request does not match its mention")
            context_requests.append(context_request)

        if status == "needs_context":
            if proposal is not None or not context_requests:
                raise RegistryContractError(
                    "needs_context requires null proposal and non-empty requests"
                )
        else:
            if context_requests:
                raise RegistryContractError("decided mention cannot request more context")
            if route == "noncharacter_index":
                if proposal is not None:
                    raise RegistryContractError("noncharacter mention cannot carry identity proposal")
            elif proposal is None:
                raise RegistryContractError("identity/pending mention needs a decided proposal")

        if proposal is not None:
            if proposal.operation == "propose_new_entity":
                raise RegistryContractError("Round-0 cannot propose a new entity")
            if proposal.canonical_surface_candidate is not None:
                raise RegistryContractError("Round-0 cannot rewrite canonical surface")
            if proposal.retrieval_trace_ids:
                raise RegistryContractError("Round-0 cannot cite unseen retrieval traces")
            if proposal.target_entity_id is not None and proposal.target_entity_id not in candidate_ids:
                raise RegistryContractError("identity proposal cites a foreign candidate entity")
            if route == "pending_kind" and proposal.operation != "pending":
                raise RegistryContractError("unknown kind cannot activate an identity operation")
            if manifest.get("overflow") and proposal.operation in {
                "reinforce_existing",
                "add_alias",
            }:
                raise RegistryContractError("overflowed candidates require retrieval or pending")

        if proposal is not None and proposal.operation in {
            "reinforce_existing",
            "add_alias",
        }:
            if (
                proposal.binding_anchor_text != surface
                or proposal.binding_block_id != anchor.block_id
            ):
                raise RegistryContractError(
                    "identity proposal binding evidence does not own its mention"
                )
            if proposal.operation == "add_alias" and proposal.alias_surface != surface:
                raise RegistryContractError(
                    "add_alias surface must equal the fresh mention surface"
                )
            binding_row = {
                "block_id": proposal.binding_block_id,
                "anchor_text": proposal.binding_anchor_text,
                "evidence_quote": proposal.binding_evidence_quote,
                "occurrence_hint": proposal.binding_occurrence_hint,
            }
            binding_anchor = _locate_row(
                binding_row, blocks=blocks, label="identity binding"
            )
            if binding_anchor != anchor:
                raise RegistryContractError(
                    "identity proposal binding evidence locates a different occurrence"
                )

        story_position = StoryPositionV1(
            chapter_order=request.chapter_order,
            block_order=block_orders[anchor.block_id],
            char_offset=anchor.char_start,
        )
        broker_requests = [
            BrokerContextRequestV1(
                occurrence_id=occurrence_id,
                decision_field=item.decision_field,
                surface=item.surface,
                reason=item.reason,
                as_of_position=story_position,
                needed_evidence=item.needed_evidence,
            ).to_dict()
            for item in context_requests
        ]
        normalized_mentions.append(
            {
                "occurrence_id": occurrence_id,
                "chapter_id": request.chapter_id,
                "surface": surface,
                "mention_type": mention_type,
                "referent_kind_claim": kind,
                "anchor": anchor.to_dict(),
                "evidence_quote": str(raw.get("evidence_quote") or ""),
                "story_position": story_position.to_dict(),
                "routing_disposition": route,
                "identity_proposal": proposal.to_dict() if proposal is not None else None,
                "broker_context_requests": broker_requests,
                "source_window_ids": [str(request.sections.get("window_id") or request.request_key)],
                "source_request_fingerprints": [request.request_fingerprint],
            }
        )

    glossary: list[dict[str, Any]] = []
    for row in response.get("glossary_candidates") or []:
        expected = {
            "source_term",
            "proposed_target_vi",
            "category",
            "do_not_translate",
            "block_ids",
        }
        _require_exact_keys(row, expected, "glossary candidate")
        if not set(row.get("block_ids") or []) <= set(active_ids):
            raise RegistryContractError("glossary candidate cites non-active block")
        glossary.append(_clone(row))
    return {
        "mentions": normalized_mentions,
        "glossary_candidates": glossary,
        "request_fingerprint": request.request_fingerprint,
        "candidate_selection_manifest_hash": str(manifest.get("manifest_hash") or ""),
    }


@dataclass(slots=True)
class ChapterWorkingSetV1:
    state_lineage_id: str
    chapter_id: str
    parent_generation_id: str | None
    parent_snapshot_hash: str
    chapter_order: int
    orientation: dict[str, Any] | None = None
    b0_request_fingerprint: str = ""
    staged_occurrences: dict[str, dict[str, Any]] = field(default_factory=dict)
    proposal_variants: dict[str, list[dict[str, Any] | None]] = field(default_factory=dict)
    broker_context_requests: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    b1_request_fingerprints: list[str] = field(default_factory=list)
    candidate_manifest_hashes: list[str] = field(default_factory=list)
    candidate_manifests: dict[str, dict[str, Any]] = field(default_factory=dict)
    candidate_entity_ids_by_occurrence: dict[str, set[str]] = field(
        default_factory=dict
    )
    candidate_pending_ids_by_occurrence: dict[str, set[str]] = field(
        default_factory=dict
    )
    reconcile_request_fingerprints: list[str] = field(default_factory=list)
    glossary_candidates: list[dict[str, Any]] = field(default_factory=list)

    def stage_orientation(
        self, *, request: RegistryRequestV1, validated: Mapping[str, Any]
    ) -> None:
        self._require_parent(request)
        self.orientation = _clone(validated)
        self.b0_request_fingerprint = request.request_fingerprint

    def _require_parent(self, request: RegistryRequestV1) -> None:
        if request.parent_snapshot_hash != self.parent_snapshot_hash:
            raise RegistryContractError("sibling request does not use the frozen parent snapshot")
        if request.state_lineage_id != self.state_lineage_id:
            raise RegistryContractError("working-set lineage mismatch")

    def stage_extract(
        self, *, request: RegistryRequestV1, validated: Mapping[str, Any]
    ) -> None:
        self._require_parent(request)
        self.b1_request_fingerprints.append(str(validated["request_fingerprint"]))
        manifest_hash = str(validated.get("candidate_selection_manifest_hash") or "")
        if manifest_hash:
            self.candidate_manifest_hashes.append(manifest_hash)
            manifest = _clone(request.sections.get("candidate_selection_manifest") or {})
            prior_manifest = self.candidate_manifests.get(manifest_hash)
            if prior_manifest is not None and canonical_json(prior_manifest) != canonical_json(
                manifest
            ):
                raise RegistryContractError("candidate manifest hash collision")
            self.candidate_manifests[manifest_hash] = manifest
        request_entity_ids = {
            str(row.get("entity_id"))
            for row in request.sections.get("candidate_cards") or []
            if row.get("entity_id")
        }
        request_pending_ids = {
            str(row.get("pending_id"))
            for row in request.sections.get("open_pending_cards") or []
            if row.get("pending_id")
        }
        self.glossary_candidates.extend(_clone(validated.get("glossary_candidates") or []))
        for row in validated.get("mentions") or []:
            occurrence_id = str(row["occurrence_id"])
            existing = self.staged_occurrences.get(occurrence_id)
            if existing is None:
                self.staged_occurrences[occurrence_id] = _clone(row)
            else:
                semantic_fields = {
                    "occurrence_id",
                    "chapter_id",
                    "surface",
                    "mention_type",
                    "referent_kind_claim",
                    "anchor",
                    "evidence_quote",
                    "story_position",
                    "routing_disposition",
                }
                if {key: existing[key] for key in semantic_fields} != {
                    key: row[key] for key in semantic_fields
                }:
                    raise RegistryContractError(
                        "same located occurrence has conflicting ground evidence"
                    )
                existing["source_window_ids"] = sorted(
                    set(existing["source_window_ids"]) | set(row["source_window_ids"])
                )
                existing["source_request_fingerprints"] = sorted(
                    set(existing["source_request_fingerprints"])
                    | set(row["source_request_fingerprints"])
                )
            variants = self.proposal_variants.setdefault(occurrence_id, [])
            proposal = _clone(row.get("identity_proposal"))
            if all(canonical_json(item) != canonical_json(proposal) for item in variants):
                variants.append(proposal)
            self.broker_context_requests.setdefault(occurrence_id, []).extend(
                _clone(row.get("broker_context_requests") or [])
            )
            self.candidate_entity_ids_by_occurrence.setdefault(
                occurrence_id, set()
            ).update(request_entity_ids)
            self.candidate_pending_ids_by_occurrence.setdefault(
                occurrence_id, set()
            ).update(request_pending_ids)


def schedule_targeted_recall(
    *,
    working_set: ChapterWorkingSetV1,
    chapter_blocks: Sequence[Mapping[str, Any]],
    design_doc: Path,
    candidate_snapshot: Mapping[str, Any],
    candidate_cap: int,
    context_k: int = 1,
) -> list[RegistryRequestV1]:
    if working_set.orientation is None:
        raise RegistryContractError("targeted recall requires validated B0 orientation")
    views = [_block_wire(block, index) for index, block in enumerate(chapter_blocks)]
    by_id = {str(row["block_id"]): row for row in views}
    index_by_id = {str(row["block_id"]): index for index, row in enumerate(views)}
    retained = {
        (
            str(row["anchor"]["block_id"]),
            int(row["anchor"]["char_start"]),
            int(row["anchor"]["char_end"]),
        )
        for row in working_set.staged_occurrences.values()
    }
    requests: list[RegistryRequestV1] = []
    for checklist_index, item in enumerate(
        working_set.orientation.get("salient_surface_checklist") or []
    ):
        anchor = item.get("anchor") or {}
        key = (
            str(anchor.get("block_id") or ""),
            int(anchor.get("char_start") or 0),
            int(anchor.get("char_end") or 0),
        )
        if key in retained:
            continue
        block_id = key[0]
        if block_id not in by_id:
            raise RegistryContractError("B0 checklist anchor is absent from chapter")
        index = index_by_id[block_id]
        context = views[max(0, index - context_k) : index] + views[
            index + 1 : index + 1 + context_k
        ]
        evidence = {
            "surface": item.get("surface"),
            "anchor_text": item.get("anchor_text"),
            "evidence_quote": item.get("evidence_quote"),
            "block_id": block_id,
            "occurrence_hint": item.get("occurrence_hint"),
        }
        requests.append(
            render_extract_request(
                design_doc=design_doc,
                chapter_id=working_set.chapter_id,
                chapter_order=working_set.chapter_order,
                request_key=f"b1-target:{working_set.chapter_id}:{checklist_index:03d}",
                window_id=f"target_{block_id}_{checklist_index:03d}",
                active_blocks=[by_id[block_id]],
                context_only_tail=context,
                snapshot=candidate_snapshot,
                candidate_cap=candidate_cap,
                targeted_recall_evidence=evidence,
            )
        )
    return requests


class FakeRegistryToolBroker:
    """Allowlisted, bounded fake broker used only by Phase-A tests."""

    def __init__(
        self,
        scripted: Mapping[tuple[str, str], Sequence[Mapping[str, Any]]],
        *,
        result_cap: int,
    ) -> None:
        if result_cap < 1:
            raise RegistryContractError("tool result cap must be positive")
        self._scripted = {
            key: [_clone(row) for row in value] for key, value in scripted.items()
        }
        self.result_cap = result_cap
        self.call_log: list[dict[str, Any]] = []

    def execute(
        self,
        *,
        tool_name: str,
        lookup_key: str,
        arguments: Mapping[str, Any],
        snapshot_hash: str,
        as_of_position: Mapping[str, Any],
        allowed_entity_ids: Sequence[str],
        allowed_pending_ids: Sequence[str],
        round_no: int,
    ) -> dict[str, Any]:
        if tool_name not in ALLOWED_TOOLS:
            raise RegistryContractError(f"non-allowlisted registry tool: {tool_name}")
        if round_no < 1 or round_no > MAX_TOOL_ROUNDS:
            raise RegistryContractError("tool round exceeds bounded loop")
        results = _clone(self._scripted.get((tool_name, lookup_key), []))
        position = StoryPositionV1(
            chapter_order=int(as_of_position.get("chapter_order", -1)),
            block_order=int(as_of_position.get("block_order", -1)),
            char_offset=int(as_of_position.get("char_offset", -1)),
        )
        if tool_name == "find_entity_candidates":
            allowed_entities = set(allowed_entity_ids)
            allowed_pending = set(allowed_pending_ids)
            for result in results:
                if not isinstance(result, Mapping):
                    raise RegistryContractError("candidate tool result must be an object")
                entity_id = result.get("entity_id")
                pending_id = result.get("pending_id")
                if (entity_id is None) == (pending_id is None):
                    raise RegistryContractError(
                        "candidate tool result must identify exactly one registry row"
                    )
                if entity_id is not None and str(entity_id) not in allowed_entities:
                    raise RegistryContractError("candidate tool returned a foreign entity")
                if pending_id is not None and str(pending_id) not in allowed_pending:
                    raise RegistryContractError("candidate tool returned a foreign pending row")
        overflow = len(results) > self.result_cap
        returned = results[: self.result_cap]
        body = {
            "tool_name": tool_name,
            "arguments": _clone(arguments),
            "snapshot_hash": snapshot_hash,
            "as_of_position": position.to_dict(),
            "round_no": round_no,
            "results": returned,
            "total_matches": len(results),
            "truncated": overflow,
            "overflow": overflow,
            "complete_search": not overflow,
        }
        trace = body | {"trace_id": "trace_" + canonical_hash(body)[:20]}
        self.call_log.append(_clone(trace))
        return trace


def render_resolution_request(
    *,
    design_doc: Path,
    working_set: ChapterWorkingSetV1,
    owned_occurrence_ids: Sequence[str],
    candidate_cards: Sequence[Mapping[str, Any]],
    pending_cards: Sequence[Mapping[str, Any]],
    tool_traces: Sequence[Mapping[str, Any]],
    remaining_tool_rounds: int,
) -> RegistryRequestV1:
    owned = [working_set.staged_occurrences[item] for item in owned_occurrence_ids]
    evidence_items = []
    for occurrence in owned:
        body = {
            "occurrence_id": occurrence["occurrence_id"],
            "anchor": occurrence["anchor"],
            "evidence_quote": occurrence["evidence_quote"],
        }
        evidence_items.append(body | {"evidence_id": "evidence_" + canonical_hash(body)[:20]})
    sections = {
        "owned_occurrences": _clone(owned),
        "candidate_cards": _clone(candidate_cards),
        "open_pending_cards": _clone(pending_cards),
        "candidate_selection_manifests": [
            _clone(working_set.candidate_manifests[key])
            for key in sorted(working_set.candidate_manifests)
        ],
        "evidence_items": evidence_items,
        "tool_result_manifests": _clone(tool_traces),
        "remaining_tool_rounds": remaining_tool_rounds,
    }
    request_key = "resolve:" + canonical_hash(
        {"chapter_id": working_set.chapter_id, "owned": sorted(owned_occurrence_ids)}
    )[:20]
    return RegistryRequestV1.create(
        role="b1_resolve",
        chapter_id=working_set.chapter_id,
        request_key=request_key,
        prompt_id=RESOLVE_PROMPT_ID,
        system_prompt=load_system_prompt_from_design(design_doc, RESOLVE_PROMPT_ID),
        state_lineage_id=working_set.state_lineage_id,
        parent_snapshot_hash=working_set.parent_snapshot_hash,
        chapter_order=working_set.chapter_order,
        sections=sections,
    )


def validate_resolution_response(
    response: Mapping[str, Any],
    *,
    request: RegistryRequestV1,
) -> dict[str, Any]:
    expected_top = {
        "chapter_id",
        "request_id",
        "owned_occurrence_ids",
        "existing_attachments",
        "new_partitions",
        "pending",
        "context_requests",
    }
    _require_exact_keys(response, expected_top, "registry resolution")
    if response.get("chapter_id") != request.chapter_id:
        raise RegistryContractError("resolution chapter mismatch")
    if response.get("request_id") != request.request_key:
        raise RegistryContractError("resolution request id mismatch")
    owned_rows = {
        str(row.get("occurrence_id")): row
        for row in request.sections.get("owned_occurrences") or []
    }
    owned = set(owned_rows)
    if set(response.get("owned_occurrence_ids") or []) != owned:
        raise RegistryContractError("resolver owned-occurrence universe drift")
    initial_candidate_ids = {
        str(row.get("entity_id"))
        for row in request.sections.get("candidate_cards") or []
        if row.get("entity_id")
    }
    candidate_ids = set(initial_candidate_ids)
    pending_ids = {
        str(row.get("pending_id"))
        for row in request.sections.get("open_pending_cards") or []
        if row.get("pending_id")
    }
    traces = {
        str(row.get("trace_id")): row
        for row in request.sections.get("tool_result_manifests") or []
    }
    tool_entity_sources: dict[str, set[str]] = {}
    for trace_id, trace in traces.items():
        if trace.get("snapshot_hash") != request.parent_snapshot_hash:
            raise RegistryContractError("resolver tool trace has a foreign snapshot")
        for result in trace.get("results") or []:
            if result.get("entity_id"):
                entity_id = str(result["entity_id"])
                candidate_ids.add(entity_id)
                tool_entity_sources.setdefault(entity_id, set()).add(trace_id)
            if result.get("pending_id"):
                pending_ids.add(str(result["pending_id"]))
    evidence_by_occurrence = {
        str(row.get("occurrence_id")): str(row.get("evidence_id"))
        for row in request.sections.get("evidence_items") or []
    }
    if set(evidence_by_occurrence) != owned or not all(evidence_by_occurrence.values()):
        raise RegistryContractError("resolver evidence universe does not cover owned rows")
    covered: list[str] = []

    def require_owned_evidence(
        occurrence_ids: Sequence[Any], evidence_refs: Sequence[Any], label: str
    ) -> list[str]:
        normalized_ids = [str(item) for item in occurrence_ids]
        if not normalized_ids or not set(normalized_ids) <= owned:
            raise RegistryContractError(f"{label} cites a foreign occurrence")
        refs = [str(item) for item in evidence_refs]
        required_refs = {evidence_by_occurrence[item] for item in normalized_ids}
        if len(refs) != len(set(refs)) or set(refs) != required_refs:
            raise RegistryContractError(
                f"{label} evidence must exact-cover its own occurrences"
            )
        return normalized_ids

    for row in response.get("existing_attachments") or []:
        expected = {
            "target_entity_id",
            "occurrence_ids",
            "operation",
            "alias_surface",
            "reason_code",
            "binding_evidence_refs",
            "retrieval_trace_ids",
        }
        _require_exact_keys(row, expected, "existing attachment")
        target_entity_id = str(row.get("target_entity_id") or "")
        if target_entity_id not in candidate_ids:
            raise RegistryContractError("resolver cites foreign target entity")
        if row.get("operation") not in {"reinforce_existing", "add_alias"}:
            raise RegistryContractError("resolver attachment operation is invalid")
        occurrence_ids = require_owned_evidence(
            row.get("occurrence_ids") or [],
            row.get("binding_evidence_refs") or [],
            "existing attachment",
        )
        trace_ids = {str(item) for item in row.get("retrieval_trace_ids") or []}
        if not trace_ids <= set(traces):
            raise RegistryContractError("resolver cites foreign retrieval trace")
        if target_entity_id not in initial_candidate_ids and not (
            trace_ids & tool_entity_sources.get(target_entity_id, set())
        ):
            raise RegistryContractError(
                "resolver tool-only target lacks its retrieval provenance"
            )
        if row.get("operation") == "add_alias":
            alias_surface = _required_str(row.get("alias_surface"), "resolver alias_surface")
            if {
                str(owned_rows[item].get("surface") or "") for item in occurrence_ids
            } != {alias_surface}:
                raise RegistryContractError(
                    "resolver alias surface is not the attached source surface"
                )
        elif row.get("alias_surface") is not None:
            raise RegistryContractError("reinforce_existing cannot carry alias_surface")
        covered.extend(occurrence_ids)

    for row in response.get("new_partitions") or []:
        expected = {
            "occurrence_ids",
            "referent_kind_claim",
            "canonical_surface_candidate",
            "alias_surfaces",
            "reason_code",
            "binding_evidence_refs",
            "retrieval_trace_ids",
            "rejected_candidate_entity_ids",
            "rejected_pending_ids",
        }
        _require_exact_keys(row, expected, "new partition")
        if row.get("referent_kind_claim") not in IDENTITY_KINDS:
            raise RegistryContractError("new partition has an ineligible kind")
        trace_ids = {str(item) for item in row.get("retrieval_trace_ids") or []}
        if not trace_ids <= set(traces):
            raise RegistryContractError("new partition cites foreign trace")
        complete_search = any(
            traces[trace_id].get("tool_name") == "find_entity_candidates"
            and traces[trace_id].get("complete_search") is True
            and traces[trace_id].get("overflow") is False
            for trace_id in trace_ids
        )
        if not complete_search:
            raise RegistryContractError(
                "new partition lacks a completed candidate search"
            )
        rejected_entities = [
            str(item) for item in row.get("rejected_candidate_entity_ids") or []
        ]
        rejected_pending = [
            str(item) for item in row.get("rejected_pending_ids") or []
        ]
        if len(rejected_entities) != len(set(rejected_entities)) or set(
            rejected_entities
        ) != candidate_ids:
            raise RegistryContractError(
                "new partition must semantically reject the full entity candidate universe"
            )
        if len(rejected_pending) != len(set(rejected_pending)) or set(
            rejected_pending
        ) != pending_ids:
            raise RegistryContractError(
                "new partition must semantically reject the full pending candidate universe"
            )
        occurrence_ids = require_owned_evidence(
            row.get("occurrence_ids") or [],
            row.get("binding_evidence_refs") or [],
            "new partition",
        )
        partition_kind = str(row.get("referent_kind_claim") or "")
        if {
            str(owned_rows[item].get("referent_kind_claim") or "")
            for item in occurrence_ids
        } != {partition_kind}:
            raise RegistryContractError("new partition changes a grounded referent kind")
        observed_surfaces = {
            str(owned_rows[item].get("surface") or "") for item in occurrence_ids
        }
        canonical_surface = _required_str(
            row.get("canonical_surface_candidate"), "canonical surface candidate"
        )
        alias_surfaces = [str(item) for item in row.get("alias_surfaces") or []]
        if canonical_surface not in observed_surfaces or set(alias_surfaces) != observed_surfaces:
            raise RegistryContractError(
                "new partition labels must exact-cover source-observed surfaces"
            )
        covered.extend(occurrence_ids)

    normalized_pending: list[dict[str, Any]] = []
    for row in response.get("pending") or []:
        expected = {
            "occurrence_id",
            "reason_code",
            "evidence_refs",
            "retrieval_trace_ids",
        }
        _require_exact_keys(row, expected, "pending disposition")
        occurrence_id = str(row.get("occurrence_id") or "")
        require_owned_evidence(
            [occurrence_id], row.get("evidence_refs") or [], "pending disposition"
        )
        if row.get("reason_code") not in PENDING_REASONS:
            raise RegistryContractError("pending disposition has an unknown reason")
        if not set(row.get("retrieval_trace_ids") or []) <= set(traces):
            raise RegistryContractError("pending cites foreign retrieval trace")
        normalized_pending.append(
            _clone(row)
            | {"candidate_entity_refs": sorted(candidate_ids)}
        )
        covered.append(occurrence_id)

    remaining_rounds = int(request.sections.get("remaining_tool_rounds") or 0)
    for row in response.get("context_requests") or []:
        expected = {
            "occurrence_id",
            "decision_field",
            "surface",
            "reason",
            "needed_evidence",
        }
        _require_exact_keys(row, expected, "resolver context request")
        if remaining_rounds < 1:
            raise RegistryContractError("resolver requested context after round cap")
        occurrence_id = str(row.get("occurrence_id") or "")
        if occurrence_id not in owned:
            raise RegistryContractError("resolver context request cites foreign occurrence")
        if row.get("decision_field") not in DECISION_FIELDS:
            raise RegistryContractError("resolver context request has unknown decision field")
        if row.get("surface") != owned_rows[occurrence_id].get("surface"):
            raise RegistryContractError("resolver context request changes occurrence surface")
        _required_str(row.get("reason"), "resolver context reason")
        needed = [str(item) for item in row.get("needed_evidence") or []]
        if (
            not needed
            or len(needed) != len(set(needed))
            or not set(needed) <= EVIDENCE_CLASSES
        ):
            raise RegistryContractError("resolver context request has invalid evidence classes")
        covered.append(occurrence_id)

    if len(covered) != len(set(covered)) or set(covered) != owned:
        raise RegistryContractError("resolver dispositions do not exact-cover owned occurrences")
    normalized = _clone(response)
    normalized["pending"] = normalized_pending
    normalized["validated_response_hash"] = canonical_hash(normalized)
    return normalized


def _position(value: Mapping[str, Any]) -> StoryPositionV1:
    return StoryPositionV1(
        chapter_order=int(value.get("chapter_order") or 0),
        block_order=int(value.get("block_order") or 0),
        char_offset=int(value.get("char_offset") or 0),
    )


def _alias_from_dict(value: Mapping[str, Any]) -> AliasBindingV1:
    _require_exact_keys(
        value,
        {
            "surface",
            "covered_occurrence_ids",
            "surface_observed_from",
            "binding_disclosed_from",
            "world_valid_from",
            "world_valid_until",
            "used_by_entity_ids",
            "decision_revision_hash",
        },
        "alias record",
    )
    return AliasBindingV1(
        surface=str(value.get("surface") or ""),
        covered_occurrence_ids=tuple(
            str(item) for item in value.get("covered_occurrence_ids") or []
        ),
        surface_observed_from=_position(value.get("surface_observed_from") or {}),
        binding_disclosed_from=(
            _position(value["binding_disclosed_from"])
            if value.get("binding_disclosed_from") is not None
            else None
        ),
        world_valid_from=(
            _position(value["world_valid_from"])
            if value.get("world_valid_from") is not None
            else None
        ),
        world_valid_until=(
            _position(value["world_valid_until"])
            if value.get("world_valid_until") is not None
            else None
        ),
        used_by_entity_ids=(
            tuple(str(item) for item in value.get("used_by_entity_ids") or [])
            if value.get("used_by_entity_ids") is not None
            else None
        ),
        decision_revision_hash=str(value.get("decision_revision_hash") or ""),
    )


def _entity_from_dict(value: Mapping[str, Any]) -> EntityRecordV1:
    _require_exact_keys(
        value,
        {
            "entity_id",
            "referent_kind",
            "runtime_eligibility",
            "canonical_surface",
            "canonical_surface_evidence_refs",
            "aliases",
            "status",
            "created_in_scope",
            "current_revision_hash",
        },
        "entity record",
    )
    return EntityRecordV1(
        entity_id=str(value.get("entity_id") or ""),
        referent_kind=str(value.get("referent_kind") or ""),  # type: ignore[arg-type]
        runtime_eligibility=str(value.get("runtime_eligibility") or ""),  # type: ignore[arg-type]
        canonical_surface=str(value.get("canonical_surface") or ""),
        canonical_surface_evidence_refs=tuple(
            str(item) for item in value.get("canonical_surface_evidence_refs") or []
        ),
        aliases=tuple(_alias_from_dict(row) for row in value.get("aliases") or []),
        status=str(value.get("status") or ""),  # type: ignore[arg-type]
        created_in_scope=str(value.get("created_in_scope") or ""),
        current_revision_hash=str(value.get("current_revision_hash") or ""),
    )


def _pending_from_dict(value: Mapping[str, Any]) -> PendingReferentV1:
    _require_exact_keys(
        value,
        {
            "pending_id",
            "occurrence_ids",
            "candidate_entity_refs",
            "reason_code",
            "opened_scope",
            "last_considered_scope",
            "status",
            "resolution_revision_hash",
        },
        "pending record",
    )
    return PendingReferentV1(
        pending_id=str(value.get("pending_id") or ""),
        occurrence_ids=tuple(str(item) for item in value.get("occurrence_ids") or []),
        candidate_entity_refs=tuple(
            str(item) for item in value.get("candidate_entity_refs") or []
        ),
        reason_code=str(value.get("reason_code") or ""),  # type: ignore[arg-type]
        opened_scope=str(value.get("opened_scope") or ""),
        last_considered_scope=str(value.get("last_considered_scope") or ""),
        status=str(value.get("status") or ""),  # type: ignore[arg-type]
        resolution_revision_hash=(
            str(value["resolution_revision_hash"])
            if value.get("resolution_revision_hash") is not None
            else None
        ),
    )


def _occurrence_from_dict(value: Mapping[str, Any]) -> OccurrenceRecordV1:
    _require_exact_keys(
        value,
        {
            "occurrence_id",
            "chapter_id",
            "surface",
            "mention_type",
            "referent_kind_claim",
            "anchor",
            "evidence_quote",
            "story_position",
            "routing_disposition",
            "binding_status",
            "entity_or_pending_ref",
            "source_window_ids",
            "source_request_fingerprints",
        },
        "occurrence record",
    )
    return OccurrenceRecordV1(
        occurrence_id=str(value.get("occurrence_id") or ""),
        chapter_id=str(value.get("chapter_id") or ""),
        surface=str(value.get("surface") or ""),
        mention_type=str(value.get("mention_type") or ""),  # type: ignore[arg-type]
        referent_kind_claim=str(value.get("referent_kind_claim") or ""),  # type: ignore[arg-type]
        anchor=SourceAnchor.from_value(value.get("anchor") or {}),
        evidence_quote=str(value.get("evidence_quote") or ""),
        story_position=_position(value.get("story_position") or {}),
        routing_disposition=str(value.get("routing_disposition") or ""),  # type: ignore[arg-type]
        binding_status=str(value.get("binding_status") or ""),  # type: ignore[arg-type]
        entity_or_pending_ref=(
            str(value["entity_or_pending_ref"])
            if value.get("entity_or_pending_ref") is not None
            else None
        ),
        source_window_ids=tuple(
            str(item) for item in value.get("source_window_ids") or []
        ),
        source_request_fingerprints=tuple(
            str(item) for item in value.get("source_request_fingerprints") or []
        ),
    )


def _presence_from_dict(value: Mapping[str, Any]) -> PresenceRowV1:
    _require_exact_keys(
        value,
        {"entity_or_pending_ref", "block_id", "occurrence_ids"},
        "presence row",
    )
    return PresenceRowV1(
        entity_or_pending_ref=str(value.get("entity_or_pending_ref") or ""),
        block_id=str(value.get("block_id") or ""),
        occurrence_ids=tuple(str(item) for item in value.get("occurrence_ids") or []),
    )


def _validate_registry_snapshot_rows(snapshot: Mapping[str, Any]) -> None:
    entity_rows = [_entity_from_dict(row) for row in snapshot.get("entities") or []]
    pending_rows = [
        _pending_from_dict(row) for row in snapshot.get("pending_records") or []
    ]
    occurrence_rows = [
        _occurrence_from_dict(row) for row in snapshot.get("occurrences") or []
    ]
    presence_rows = [
        _presence_from_dict(row) for row in snapshot.get("presence_rows") or []
    ]
    for label, values in (
        ("entity", [row.entity_id for row in entity_rows]),
        ("pending", [row.pending_id for row in pending_rows]),
        ("occurrence", [row.occurrence_id for row in occurrence_rows]),
        (
            "presence",
            [f"{row.entity_or_pending_ref}\x00{row.block_id}" for row in presence_rows],
        ),
    ):
        if len(values) != len(set(values)):
            raise RegistryContractError(f"duplicate {label} row in registry snapshot")


def _seal_entity(record: EntityRecordV1) -> EntityRecordV1:
    body = record.to_dict()
    body["current_revision_hash"] = ""
    return replace(record, current_revision_hash=canonical_hash(body))


def _mint_pending_id(state_lineage_id: str, occurrence_ids: Sequence[str]) -> str:
    return "pend_" + canonical_hash(
        {
            "state_lineage_id": state_lineage_id,
            "sorted_occurrence_ids": sorted(occurrence_ids),
        }
    )[:20]


def _mint_entity_id(state_lineage_id: str, occurrence_ids: Sequence[str]) -> str:
    if not occurrence_ids:
        raise RegistryContractError("cannot mint entity without occurrences")
    return "ent_" + canonical_hash(
        {
            "state_lineage_id": state_lineage_id,
            "earliest_owned_occurrence_id": occurrence_ids[0],
        }
    )[:20]


@dataclass(frozen=True, slots=True)
class PreparedChapterCommitV1:
    state_lineage_id: str
    parent_generation_id: str | None
    chapter_id: str
    entity_revisions: tuple[dict[str, Any], ...]
    alias_revisions: tuple[dict[str, Any], ...]
    occurrence_records: tuple[dict[str, Any], ...]
    presence_rows: tuple[dict[str, Any], ...]
    pending_records: tuple[dict[str, Any], ...]


def finalize_working_set(
    *,
    working_set: ChapterWorkingSetV1,
    parent_snapshot: Mapping[str, Any],
    resolution: Mapping[str, Any] | None,
) -> PreparedChapterCommitV1:
    verify_registry_snapshot(parent_snapshot)
    if parent_snapshot.get("snapshot_hash") != working_set.parent_snapshot_hash:
        raise RegistryContractError("finalization parent snapshot drift")
    parent_entities = {
        str(row.get("entity_id")): _entity_from_dict(row)
        for row in parent_snapshot.get("entities") or []
    }
    attachments: list[dict[str, Any]] = []
    pending_rows: list[dict[str, Any]] = []
    new_partitions: list[dict[str, Any]] = []
    unresolved: set[str] = set()

    for occurrence_id, occurrence in working_set.staged_occurrences.items():
        route = occurrence["routing_disposition"]
        if route == "noncharacter_index":
            continue
        variants = working_set.proposal_variants.get(occurrence_id) or [None]
        non_null = [item for item in variants if item is not None]
        if len(non_null) != 1 or len(variants) != 1:
            unresolved.add(occurrence_id)
            continue
        proposal = non_null[0]
        operation = proposal.get("operation")
        if operation in {"reinforce_existing", "add_alias"}:
            attachments.append(
                {
                    "target_entity_id": proposal["target_entity_id"],
                    "occurrence_ids": [occurrence_id],
                    "operation": operation,
                    "alias_surface": proposal.get("alias_surface"),
                    "reason_code": proposal.get("reason_code"),
                    "binding_evidence_refs": [],
                    "retrieval_trace_ids": [],
                }
            )
        elif operation == "pending":
            pending_rows.append(
                {
                    "occurrence_id": occurrence_id,
                    "reason_code": "insufficient_evidence",
                    "evidence_refs": [],
                    "retrieval_trace_ids": [],
                }
            )
        else:
            unresolved.add(occurrence_id)

    if resolution is not None:
        attachments.extend(_clone(resolution.get("existing_attachments") or []))
        new_partitions.extend(_clone(resolution.get("new_partitions") or []))
        pending_rows.extend(_clone(resolution.get("pending") or []))
        resolver_owned = set(resolution.get("owned_occurrence_ids") or [])
        unresolved -= resolver_owned
        if resolution.get("context_requests"):
            unresolved |= {
                str(row.get("occurrence_id") or "")
                for row in resolution.get("context_requests") or []
            }
    if unresolved:
        raise RegistryContractError(
            f"working set has unresolved dispositions: {sorted(unresolved)}"
        )

    entity_revisions: dict[str, EntityRecordV1] = {}
    binding_by_occurrence: dict[str, tuple[str, str]] = {}

    def add_or_extend_alias(
        entity: EntityRecordV1,
        *,
        surface: str,
        occurrence_ids: Sequence[str],
        decision_hash: str,
    ) -> EntityRecordV1:
        positions = [
            _position(working_set.staged_occurrences[item]["story_position"])
            for item in occurrence_ids
        ]
        aliases = list(entity.aliases)
        matched = False
        for index, alias in enumerate(aliases):
            if alias.surface != surface:
                continue
            aliases[index] = replace(
                alias,
                covered_occurrence_ids=tuple(
                    sorted(set(alias.covered_occurrence_ids) | set(occurrence_ids))
                ),
                surface_observed_from=min(alias.surface_observed_from, min(positions)),
                decision_revision_hash=decision_hash,
            )
            matched = True
            break
        if not matched:
            aliases.append(
                AliasBindingV1(
                    surface=surface,
                    covered_occurrence_ids=tuple(sorted(occurrence_ids)),
                    surface_observed_from=min(positions),
                    binding_disclosed_from=None,
                    world_valid_from=None,
                    world_valid_until=None,
                    used_by_entity_ids=None,
                    decision_revision_hash=decision_hash,
                )
            )
        updated = replace(entity, aliases=tuple(sorted(aliases, key=lambda item: item.surface)))
        if surface == entity.canonical_surface:
            updated = replace(
                updated,
                canonical_surface_evidence_refs=tuple(
                    sorted(
                        set(updated.canonical_surface_evidence_refs) | set(occurrence_ids)
                    )
                ),
            )
        return _seal_entity(updated)

    for attachment in attachments:
        entity_id = str(attachment.get("target_entity_id") or "")
        entity = entity_revisions.get(entity_id) or parent_entities.get(entity_id)
        if entity is None:
            raise RegistryContractError(f"attachment targets unknown entity: {entity_id}")
        occurrence_ids = [str(item) for item in attachment.get("occurrence_ids") or []]
        if not occurrence_ids:
            raise RegistryContractError("attachment has no occurrences")
        for occurrence_id in occurrence_ids:
            occurrence = working_set.staged_occurrences[occurrence_id]
            if occurrence["referent_kind_claim"] != entity.referent_kind:
                raise RegistryContractError("attachment kind conflicts with active entity")
        surface = (
            str(attachment.get("alias_surface") or "")
            if attachment.get("operation") == "add_alias"
            else str(working_set.staged_occurrences[occurrence_ids[0]]["surface"])
        )
        decision_hash = canonical_hash(attachment)
        entity = add_or_extend_alias(
            entity,
            surface=surface,
            occurrence_ids=occurrence_ids,
            decision_hash=decision_hash,
        )
        entity_revisions[entity_id] = entity
        for occurrence_id in occurrence_ids:
            binding_by_occurrence[occurrence_id] = ("attached", entity_id)

    for partition in new_partitions:
        occurrence_ids = sorted(
            (str(item) for item in partition.get("occurrence_ids") or []),
            key=lambda item: (
                _position(working_set.staged_occurrences[item]["story_position"]),
                item,
            ),
        )
        if not occurrence_ids:
            raise RegistryContractError("new partition has no occurrences")
        entity_id = _mint_entity_id(working_set.state_lineage_id, occurrence_ids)
        if entity_id in parent_entities or entity_id in entity_revisions:
            raise RegistryContractError("entity id collision")
        kind = str(partition.get("referent_kind_claim") or "")
        surfaces = [str(item) for item in partition.get("alias_surfaces") or []]
        observed_surfaces = {
            str(working_set.staged_occurrences[item]["surface"])
            for item in occurrence_ids
        }
        if set(surfaces) != observed_surfaces:
            raise RegistryContractError(
                "new partition aliases must exact-cover observed surfaces"
            )
        canonical_surface = _required_str(
            partition.get("canonical_surface_candidate"), "canonical surface candidate"
        )
        if canonical_surface not in observed_surfaces:
            raise RegistryContractError("canonical surface candidate is not source-observed")
        aliases: list[AliasBindingV1] = []
        decision_hash = canonical_hash(partition)
        for surface in sorted(set(surfaces) | {canonical_surface}):
            covered = [
                item
                for item in occurrence_ids
                if working_set.staged_occurrences[item]["surface"] == surface
            ]
            if not covered:
                continue
            aliases.append(
                AliasBindingV1(
                    surface=surface,
                    covered_occurrence_ids=tuple(covered),
                    surface_observed_from=min(
                        _position(working_set.staged_occurrences[item]["story_position"])
                        for item in covered
                    ),
                    binding_disclosed_from=None,
                    world_valid_from=None,
                    world_valid_until=None,
                    used_by_entity_ids=None,
                    decision_revision_hash=decision_hash,
                )
            )
        record = EntityRecordV1(
            entity_id=entity_id,
            referent_kind=kind,  # type: ignore[arg-type]
            runtime_eligibility="eligible",
            canonical_surface=canonical_surface,
            canonical_surface_evidence_refs=tuple(
                item
                for item in occurrence_ids
                if working_set.staged_occurrences[item]["surface"] == canonical_surface
            ),
            aliases=tuple(aliases),
            status="active",
            created_in_scope=working_set.chapter_id,
            current_revision_hash="placeholder",
        )
        record = _seal_entity(record)
        entity_revisions[entity_id] = record
        for occurrence_id in occurrence_ids:
            binding_by_occurrence[occurrence_id] = ("attached", entity_id)

    pending_records: list[PendingReferentV1] = []
    for pending in pending_rows:
        occurrence_id = str(pending.get("occurrence_id") or "")
        if occurrence_id not in working_set.staged_occurrences:
            raise RegistryContractError("pending disposition cites foreign occurrence")
        pending_id = _mint_pending_id(working_set.state_lineage_id, [occurrence_id])
        candidates = tuple(
            sorted(
                set(working_set.candidate_entity_ids_by_occurrence.get(occurrence_id, set()))
                | {
                    str(item)
                    for item in pending.get("candidate_entity_refs") or []
                    if item
                }
            )
        )
        if not set(candidates) <= set(parent_entities):
            raise RegistryContractError("pending disposition retains a foreign candidate")
        reason = str(pending.get("reason_code") or "insufficient_evidence")
        reason_map = {
            "ambiguous": "ambiguous_candidates",
            "kind_conflict": "conflicting_kind",
            "no_candidate": "no_candidate",
        }
        reason = reason_map.get(reason, reason)
        if reason not in {
            "no_candidate",
            "ambiguous_candidates",
            "conflicting_kind",
            "insufficient_evidence",
            "reconcile_cap",
        }:
            reason = "insufficient_evidence"
        pending_record = PendingReferentV1(
            pending_id=pending_id,
            occurrence_ids=(occurrence_id,),
            candidate_entity_refs=candidates,
            reason_code=reason,  # type: ignore[arg-type]
            opened_scope=working_set.chapter_id,
            last_considered_scope=working_set.chapter_id,
            status="open",
            resolution_revision_hash=None,
        )
        pending_records.append(pending_record)
        binding_by_occurrence[occurrence_id] = ("pending", pending_id)

    occurrence_records: list[OccurrenceRecordV1] = []
    for occurrence_id, staged in working_set.staged_occurrences.items():
        route = str(staged["routing_disposition"])
        if route == "noncharacter_index":
            binding_status, binding_ref = "noncharacter", None
        else:
            if occurrence_id not in binding_by_occurrence:
                raise RegistryContractError(
                    f"retained occurrence lacks disposition: {occurrence_id}"
                )
            binding_status, binding_ref = binding_by_occurrence[occurrence_id]
        anchor = SourceAnchor.from_value(staged["anchor"])
        occurrence_records.append(
            OccurrenceRecordV1(
                occurrence_id=occurrence_id,
                chapter_id=working_set.chapter_id,
                surface=str(staged["surface"]),
                mention_type=str(staged["mention_type"]),  # type: ignore[arg-type]
                referent_kind_claim=str(staged["referent_kind_claim"]),  # type: ignore[arg-type]
                anchor=anchor,
                evidence_quote=str(staged["evidence_quote"]),
                story_position=_position(staged["story_position"]),
                routing_disposition=route,  # type: ignore[arg-type]
                binding_status=binding_status,  # type: ignore[arg-type]
                entity_or_pending_ref=binding_ref,
                source_window_ids=tuple(sorted(set(staged["source_window_ids"]))),
                source_request_fingerprints=tuple(
                    sorted(set(staged["source_request_fingerprints"]))
                ),
            )
        )

    presence_groups: dict[tuple[str, str], list[str]] = {}
    for occurrence in occurrence_records:
        if occurrence.entity_or_pending_ref is None:
            continue
        presence_groups.setdefault(
            (occurrence.entity_or_pending_ref, occurrence.anchor.block_id), []
        ).append(occurrence.occurrence_id)
    presence_rows = [
        PresenceRowV1(
            entity_or_pending_ref=ref,
            block_id=block_id,
            occurrence_ids=tuple(sorted(occurrence_ids)),
        )
        for (ref, block_id), occurrence_ids in sorted(presence_groups.items())
    ]
    alias_revisions = [
        alias.to_dict()
        for entity in entity_revisions.values()
        for alias in entity.aliases
    ]
    return PreparedChapterCommitV1(
        state_lineage_id=working_set.state_lineage_id,
        parent_generation_id=working_set.parent_generation_id,
        chapter_id=working_set.chapter_id,
        entity_revisions=tuple(
            entity.to_dict()
            for entity in sorted(entity_revisions.values(), key=lambda row: row.entity_id)
        ),
        alias_revisions=tuple(
            sorted(
                alias_revisions,
                key=lambda row: (str(row["surface"]), canonical_hash(row)),
            )
        ),
        occurrence_records=tuple(
            row.to_dict()
            for row in sorted(occurrence_records, key=lambda item: item.occurrence_id)
        ),
        presence_rows=tuple(row.to_dict() for row in presence_rows),
        pending_records=tuple(
            row.to_dict() for row in sorted(pending_records, key=lambda item: item.pending_id)
        ),
    )


def build_registry_generation(
    *,
    prepared: PreparedChapterCommitV1,
    source_manifest_hash: str,
    b0_request_fingerprint: str,
    b1_request_fingerprints: Sequence[str],
    candidate_manifest_hashes: Sequence[str],
    reconcile_request_fingerprints: Sequence[str],
) -> RegistryGenerationV1:
    body = {
        "state_lineage_id": prepared.state_lineage_id,
        "parent_generation_id": prepared.parent_generation_id,
        "chapter_id": prepared.chapter_id,
        "source_manifest_hash": source_manifest_hash,
        "b0_request_fingerprint": b0_request_fingerprint,
        "b1_request_fingerprints": sorted(set(b1_request_fingerprints)),
        "candidate_selection_manifest_hashes": sorted(set(candidate_manifest_hashes)),
        "reconcile_request_fingerprints": sorted(set(reconcile_request_fingerprints)),
        "entity_revisions": list(prepared.entity_revisions),
        "alias_revisions": list(prepared.alias_revisions),
        "occurrence_records": list(prepared.occurrence_records),
        "presence_rows": list(prepared.presence_rows),
        "pending_records": list(prepared.pending_records),
    }
    commit_payload_hash = canonical_hash(body)
    generation_id = "reggen_" + canonical_hash(
        {
            "state_lineage_id": prepared.state_lineage_id,
            "parent_generation_id": prepared.parent_generation_id,
            "chapter_id": prepared.chapter_id,
            "commit_payload_hash": commit_payload_hash,
        }
    )[:20]
    return RegistryGenerationV1(
        state_lineage_id=prepared.state_lineage_id,
        generation_id=generation_id,
        parent_generation_id=prepared.parent_generation_id,
        chapter_id=prepared.chapter_id,
        source_manifest_hash=source_manifest_hash,
        b0_request_fingerprint=b0_request_fingerprint,
        b1_request_fingerprints=tuple(body["b1_request_fingerprints"]),
        candidate_selection_manifest_hashes=tuple(
            body["candidate_selection_manifest_hashes"]
        ),
        reconcile_request_fingerprints=tuple(body["reconcile_request_fingerprints"]),
        entity_revisions=prepared.entity_revisions,
        alias_revisions=prepared.alias_revisions,
        occurrence_records=prepared.occurrence_records,
        presence_rows=prepared.presence_rows,
        pending_records=prepared.pending_records,
        commit_payload_hash=commit_payload_hash,
    )


class ChapterRegistryStoreV1:
    def __init__(self, root: Path) -> None:
        self.root = Path(root).resolve()

    def _generation_path(self, generation_id: str) -> Path:
        return self.root / "generations" / f"{_safe_id(generation_id, 'generation_id')}.json"

    def _pointer_path(self, state_lineage_id: str) -> Path:
        return self.root / "current" / f"{_safe_id(state_lineage_id, 'lineage')}.json"

    def _write_generation(self, generation: RegistryGenerationV1) -> None:
        path = self._generation_path(generation.generation_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        encoded = (canonical_json(generation.to_dict()) + "\n").encode("utf-8")
        try:
            with path.open("xb") as handle:
                handle.write(encoded)
                handle.flush()
                os.fsync(handle.fileno())
        except FileExistsError:
            if path.read_bytes() != encoded:
                raise RegistryStoreError("generation id collision with different bytes")

    def load_generation(self, generation_id: str) -> dict[str, Any]:
        path = self._generation_path(generation_id)
        if not path.is_file():
            raise RegistryStoreError(f"missing registry generation: {generation_id}")
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise RegistryStoreError("registry generation is not an object")
        expected_fields = {
            "state_lineage_id",
            "generation_id",
            "parent_generation_id",
            "chapter_id",
            "source_manifest_hash",
            "b0_request_fingerprint",
            "b1_request_fingerprints",
            "candidate_selection_manifest_hashes",
            "reconcile_request_fingerprints",
            "entity_revisions",
            "alias_revisions",
            "occurrence_records",
            "presence_rows",
            "pending_records",
            "commit_payload_hash",
        }
        try:
            _require_exact_keys(payload, expected_fields, "registry generation")
        except RegistryContractError as exc:
            raise RegistryStoreError(str(exc)) from exc
        body = dict(payload)
        own_generation_id = str(body.pop("generation_id", ""))
        own_commit_hash = str(body.pop("commit_payload_hash", ""))
        if canonical_hash(body) != own_commit_hash:
            raise RegistryStoreError("registry commit payload hash mismatch")
        expected_generation_id = "reggen_" + canonical_hash(
            {
                "state_lineage_id": body.get("state_lineage_id"),
                "parent_generation_id": body.get("parent_generation_id"),
                "chapter_id": body.get("chapter_id"),
                "commit_payload_hash": own_commit_hash,
            }
        )[:20]
        if own_generation_id != generation_id or expected_generation_id != generation_id:
            raise RegistryStoreError("registry generation id mismatch")
        return payload

    def current_generation_id(self, state_lineage_id: str) -> str | None:
        path = self._pointer_path(state_lineage_id)
        if not path.is_file():
            return None
        pointer = json.loads(path.read_text(encoding="utf-8"))
        if pointer.get("state_lineage_id") != state_lineage_id:
            raise RegistryStoreError("registry pointer lineage mismatch")
        generation_id = _required_str(pointer.get("generation_id"), "pointer generation_id")
        generation = self.load_generation(generation_id)
        if generation.get("state_lineage_id") != state_lineage_id:
            raise RegistryStoreError("pointer targets a foreign lineage")
        return generation_id

    def commit(
        self,
        generation: RegistryGenerationV1,
        *,
        expected_parent: str | None,
        before_pointer_switch: Callable[[], None] | None = None,
    ) -> None:
        if generation.parent_generation_id != expected_parent:
            raise RegistryStaleParentError("generation parent differs from CAS expectation")
        self._write_generation(generation)
        lock_root = self.root / "lineage_locks" / canonical_hash(
            {"state_lineage_id": generation.state_lineage_id}
        )
        with CheckpointLock(lock_root):
            current = self.current_generation_id(generation.state_lineage_id)
            if current != expected_parent:
                raise RegistryStaleParentError(
                    f"stale registry parent: expected {expected_parent}, current {current}"
                )
            if before_pointer_switch is not None:
                before_pointer_switch()
            write_checkpoint_atomic(
                self._pointer_path(generation.state_lineage_id),
                {
                    "state_lineage_id": generation.state_lineage_id,
                    "generation_id": generation.generation_id,
                },
            )

    def snapshot(
        self, state_lineage_id: str, generation_id: str | None = None
    ) -> dict[str, Any]:
        actual = generation_id
        if actual is None:
            actual = self.current_generation_id(state_lineage_id)
        if actual is None:
            return empty_registry_snapshot(state_lineage_id)
        chain: list[dict[str, Any]] = []
        seen: set[str] = set()
        cursor: str | None = actual
        while cursor is not None:
            if cursor in seen:
                raise RegistryStoreError("registry generation cycle")
            seen.add(cursor)
            generation = self.load_generation(cursor)
            if generation.get("state_lineage_id") != state_lineage_id:
                raise RegistryStoreError("generation chain crosses lineage")
            chain.append(generation)
            cursor = (
                str(generation["parent_generation_id"])
                if generation.get("parent_generation_id") is not None
                else None
            )
        entities: dict[str, dict[str, Any]] = {}
        pending: dict[str, dict[str, Any]] = {}
        occurrences: dict[str, dict[str, Any]] = {}
        presence: dict[tuple[str, str], dict[str, Any]] = {}
        for generation in reversed(chain):
            for row in generation.get("entity_revisions") or []:
                entities[str(row["entity_id"])] = _clone(row)
            for row in generation.get("pending_records") or []:
                pending[str(row["pending_id"])] = _clone(row)
            for row in generation.get("occurrence_records") or []:
                occurrence_id = str(row["occurrence_id"])
                prior = occurrences.get(occurrence_id)
                if prior is not None and canonical_json(prior) != canonical_json(row):
                    raise RegistryStoreError("occurrence history was rewritten")
                occurrences[occurrence_id] = _clone(row)
            for row in generation.get("presence_rows") or []:
                key = (str(row["entity_or_pending_ref"]), str(row["block_id"]))
                if key in presence:
                    merged = sorted(
                        set(presence[key]["occurrence_ids"])
                        | set(row.get("occurrence_ids") or [])
                    )
                    presence[key]["occurrence_ids"] = merged
                else:
                    presence[key] = _clone(row)
        body = {
            "builder_identity_mode": BUILDER_IDENTITY_MODE,
            "schema_version": REGISTRY_SCHEMA_VERSION,
            "state_lineage_id": state_lineage_id,
            "generation_id": actual,
            "entities": [entities[key] for key in sorted(entities)],
            "pending_records": [pending[key] for key in sorted(pending)],
            "occurrences": [occurrences[key] for key in sorted(occurrences)],
            "presence_rows": [presence[key] for key in sorted(presence)],
        }
        return body | {"snapshot_hash": canonical_hash(body)}


def _union_cards(
    requests: Sequence[RegistryRequestV1], field_name: str, id_field: str
) -> list[dict[str, Any]]:
    rows: dict[str, dict[str, Any]] = {}
    for request in requests:
        for value in request.sections.get(field_name) or []:
            row_id = str(value.get(id_field) or "")
            if not row_id:
                continue
            prior = rows.get(row_id)
            if prior is not None and canonical_json(prior) != canonical_json(value):
                raise RegistryContractError(f"candidate card drift for {row_id}")
            rows[row_id] = _clone(value)
    return [rows[key] for key in sorted(rows)]


def _merge_resolution_rounds(
    first: Mapping[str, Any], second: Mapping[str, Any]
) -> dict[str, Any]:
    replaced = {
        str(row.get("occurrence_id") or "")
        for row in first.get("context_requests") or []
    }
    if set(second.get("owned_occurrence_ids") or []) != replaced:
        raise RegistryContractError("second resolution round does not own prior requests")
    result = {
        "chapter_id": first["chapter_id"],
        "request_id": first["request_id"],
        "owned_occurrence_ids": list(first["owned_occurrence_ids"]),
        "existing_attachments": list(first.get("existing_attachments") or [])
        + list(second.get("existing_attachments") or []),
        "new_partitions": list(first.get("new_partitions") or [])
        + list(second.get("new_partitions") or []),
        "pending": list(first.get("pending") or []) + list(second.get("pending") or []),
        "context_requests": list(second.get("context_requests") or []),
    }
    if result["context_requests"]:
        raise RegistryContractError("resolution remained open after the second tool round")
    result["validated_response_hash"] = canonical_hash(result)
    return result


def run_synthetic_registry_chapter(
    *,
    builder_identity_mode: str,
    design_doc: Path,
    chapter: Mapping[str, Any],
    chapter_order: int,
    windows: Sequence[Mapping[str, Any]],
    parent_snapshot: Mapping[str, Any],
    executor: SyntheticRegistryExecutor,
    broker: FakeRegistryToolBroker,
    store: ChapterRegistryStoreV1,
    candidate_cap: int,
    tool_round_cap: int = MAX_TOOL_ROUNDS,
    before_pointer_switch: Callable[[], None] | None = None,
) -> dict[str, Any]:
    """Run the complete Phase-A path using scripted responses and fake tools."""

    if builder_identity_mode != BUILDER_IDENTITY_MODE:
        raise RegistryContractError("chapter-registry runner requires its explicit feature flag")
    if tool_round_cap < 1 or tool_round_cap > MAX_TOOL_ROUNDS:
        raise RegistryContractError("tool_round_cap is outside the closed Phase-A range")
    verify_registry_snapshot(parent_snapshot)
    state_lineage_id = str(parent_snapshot["state_lineage_id"])
    parent_generation_id = (
        str(parent_snapshot["generation_id"])
        if parent_snapshot.get("generation_id") is not None
        else None
    )
    if store.current_generation_id(state_lineage_id) != parent_generation_id:
        raise RegistryStaleParentError("provided parent snapshot is not the current pointer")
    chapter_id = _required_str(chapter.get("chapter_id"), "chapter_id")
    working = ChapterWorkingSetV1(
        state_lineage_id=state_lineage_id,
        chapter_id=chapter_id,
        parent_generation_id=parent_generation_id,
        parent_snapshot_hash=str(parent_snapshot["snapshot_hash"]),
        chapter_order=chapter_order,
    )

    orientation_request = render_orientation_request(
        design_doc=design_doc,
        chapter=chapter,
        chapter_order=chapter_order,
        snapshot=parent_snapshot,
    )
    orientation = validate_orientation_response(
        executor.execute(orientation_request), request=orientation_request
    )
    working.stage_orientation(request=orientation_request, validated=orientation)

    ordinary_requests = [
        render_extract_request(
            design_doc=design_doc,
            chapter_id=chapter_id,
            chapter_order=chapter_order,
            request_key=f"b1:{chapter_id}:{str(window['window_id'])}",
            window_id=str(window["window_id"]),
            active_blocks=window.get("active_blocks") or [],
            context_only_tail=window.get("context_only_tail") or [],
            snapshot=parent_snapshot,
            candidate_cap=candidate_cap,
        )
        for window in windows
    ]
    all_extract_requests: list[RegistryRequestV1] = list(ordinary_requests)
    for request in ordinary_requests:
        validated = validate_extract_response(executor.execute(request), request=request)
        working.stage_extract(request=request, validated=validated)

    targeted_requests = schedule_targeted_recall(
        working_set=working,
        chapter_blocks=chapter.get("blocks") or [],
        design_doc=design_doc,
        candidate_snapshot=parent_snapshot,
        candidate_cap=candidate_cap,
    )
    all_extract_requests.extend(targeted_requests)
    for request in targeted_requests:
        validated = validate_extract_response(executor.execute(request), request=request)
        working.stage_extract(request=request, validated=validated)

    unresolved: list[str] = []
    for occurrence_id, occurrence in working.staged_occurrences.items():
        if occurrence["routing_disposition"] == "noncharacter_index":
            continue
        variants = working.proposal_variants.get(occurrence_id) or [None]
        if len(variants) != 1 or variants[0] is None:
            unresolved.append(occurrence_id)

    candidate_cards = _union_cards(all_extract_requests, "candidate_cards", "entity_id")
    pending_cards = _union_cards(all_extract_requests, "open_pending_cards", "pending_id")
    traces: list[dict[str, Any]] = []
    resolution: dict[str, Any] | None = None
    allowed_entity_ids = [
        str(row.get("entity_id"))
        for row in parent_snapshot.get("entities") or []
        if row.get("entity_id")
    ]
    allowed_pending_ids = [
        str(row.get("pending_id"))
        for row in parent_snapshot.get("pending_records") or []
        if row.get("pending_id")
    ]

    def execute_broker_round(
        occurrence_id: str,
        context_requests: Sequence[Mapping[str, Any]],
        *,
        round_no: int,
    ) -> list[dict[str, Any]]:
        occurrence = working.staged_occurrences[occurrence_id]
        requested_evidence = {
            str(item)
            for request_row in context_requests
            for item in request_row.get("needed_evidence") or []
        }
        if not requested_evidence:
            requested_evidence.add("candidate_entities")
        if not requested_evidence <= EVIDENCE_CLASSES:
            raise RegistryContractError("broker dispatch received an unknown evidence class")
        # A completed candidate search is mandatory before a new entity can be proposed.
        requested_evidence.add("candidate_entities")
        decision_fields = {
            str(row.get("decision_field") or "identity_binding")
            for row in context_requests
        }
        if not decision_fields <= DECISION_FIELDS or len(decision_fields) > 1:
            raise RegistryContractError("broker dispatch has inconsistent decision fields")
        decision_field = next(iter(decision_fields), "identity_binding")
        tool_by_evidence = {
            "candidate_entities": "find_entity_candidates",
            "entity_history": "get_entity_evidence",
            "pending_history": "get_pending_evidence",
            "wider_source_context": "get_source_context",
        }
        emitted: list[dict[str, Any]] = []
        for evidence_class in sorted(requested_evidence):
            tool_name = tool_by_evidence[evidence_class]
            arguments: dict[str, Any] = {
                "surface": occurrence["surface"],
                "chapter_id": chapter_id,
                "block_id": occurrence["anchor"]["block_id"],
                "decision_field": decision_field,
            }
            if evidence_class == "entity_history":
                arguments["entity_ids"] = sorted(
                    working.candidate_entity_ids_by_occurrence.get(
                        occurrence_id, set()
                    )
                )
            elif evidence_class == "pending_history":
                arguments["pending_ids"] = sorted(
                    working.candidate_pending_ids_by_occurrence.get(
                        occurrence_id, set()
                    )
                )
            elif evidence_class == "wider_source_context":
                arguments["context_policy"] = "bounded_neighbor_blocks_v1"
            emitted.append(
                broker.execute(
                    tool_name=tool_name,
                    lookup_key=str(occurrence["surface"]),
                    arguments=arguments,
                    snapshot_hash=working.parent_snapshot_hash,
                    as_of_position=occurrence["story_position"],
                    allowed_entity_ids=allowed_entity_ids,
                    allowed_pending_ids=allowed_pending_ids,
                    round_no=round_no,
                )
            )
        return emitted

    if unresolved:
        for occurrence_id in sorted(unresolved):
            traces.extend(
                execute_broker_round(
                    occurrence_id,
                    working.broker_context_requests.get(occurrence_id, []),
                    round_no=1,
                )
            )
        first_request = render_resolution_request(
            design_doc=design_doc,
            working_set=working,
            owned_occurrence_ids=sorted(unresolved),
            candidate_cards=candidate_cards,
            pending_cards=pending_cards,
            tool_traces=traces,
            remaining_tool_rounds=tool_round_cap - 1,
        )
        first = validate_resolution_response(
            executor.execute(first_request), request=first_request
        )
        working.reconcile_request_fingerprints.append(first_request.request_fingerprint)
        resolution = first
        if first.get("context_requests"):
            if tool_round_cap < 2:
                raise RegistryContractError("resolution requested a disabled second round")
            second_owned = [
                str(row["occurrence_id"]) for row in first["context_requests"]
            ]
            context_by_occurrence = {
                str(row["occurrence_id"]): [row]
                for row in first["context_requests"]
            }
            for occurrence_id in second_owned:
                traces.extend(
                    execute_broker_round(
                        occurrence_id,
                        context_by_occurrence[occurrence_id],
                        round_no=2,
                    )
                )
            second_request = render_resolution_request(
                design_doc=design_doc,
                working_set=working,
                owned_occurrence_ids=second_owned,
                candidate_cards=candidate_cards,
                pending_cards=pending_cards,
                tool_traces=traces,
                remaining_tool_rounds=0,
            )
            second = validate_resolution_response(
                executor.execute(second_request), request=second_request
            )
            working.reconcile_request_fingerprints.append(second_request.request_fingerprint)
            resolution = _merge_resolution_rounds(first, second)

    prepared = finalize_working_set(
        working_set=working,
        parent_snapshot=parent_snapshot,
        resolution=resolution,
    )
    generation = build_registry_generation(
        prepared=prepared,
        source_manifest_hash=chapter_source_manifest_hash(chapter),
        b0_request_fingerprint=working.b0_request_fingerprint,
        b1_request_fingerprints=working.b1_request_fingerprints,
        candidate_manifest_hashes=working.candidate_manifest_hashes,
        reconcile_request_fingerprints=working.reconcile_request_fingerprints,
    )
    store.commit(
        generation,
        expected_parent=parent_generation_id,
        before_pointer_switch=before_pointer_switch,
    )
    return {
        "generation": generation.to_dict(),
        "snapshot": store.snapshot(state_lineage_id),
        "orientation": orientation,
        "working_set": working,
        "ordinary_request_count": len(ordinary_requests),
        "targeted_request_count": len(targeted_requests),
        "tool_trace_count": len(traces),
        "executor_call_log": _clone(executor.call_log),
    }


def chapter_source_manifest_hash(chapter: Mapping[str, Any]) -> str:
    return canonical_hash(chapter_block_views(chapter))


__all__ = [
    "ALLOWED_TOOLS",
    "BUILDER_IDENTITY_MODE",
    "ChapterRegistryStoreV1",
    "ChapterWorkingSetV1",
    "DEFAULT_BUILDER_IDENTITY_MODE",
    "EXECUTION_MODE_SYNTHETIC",
    "FakeRegistryToolBroker",
    "PreparedChapterCommitV1",
    "REGISTRY_SCHEMA_VERSION",
    "REGISTRY_VALIDATOR_CONTRACT_HASH",
    "RegistryContractError",
    "RegistryRequestV1",
    "RegistryStaleParentError",
    "RegistryStoreError",
    "SyntheticRegistryExecutor",
    "build_registry_generation",
    "chapter_block_views",
    "chapter_source_manifest_hash",
    "empty_registry_snapshot",
    "finalize_working_set",
    "load_snapshot_from_handle",
    "mint_occurrence_id",
    "render_extract_request",
    "render_orientation_request",
    "render_resolution_request",
    "routing_for_kind",
    "run_synthetic_registry_chapter",
    "schedule_targeted_recall",
    "select_candidate_cards",
    "validate_extract_response",
    "validate_orientation_response",
    "validate_resolution_response",
    "verify_registry_snapshot",
]
