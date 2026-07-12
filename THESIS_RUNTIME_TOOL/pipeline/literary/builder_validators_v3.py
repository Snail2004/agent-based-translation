"""Offline validators for Builder v3 normalized payloads.

These validators are deliberately not wired into ``builder_pilot.py`` yet.
They make Builder-v3's data contract executable while preserving the current
pipeline as the comparison baseline for the following migration step.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from copy import deepcopy
from dataclasses import dataclass
import re
from typing import Any, Generic, Iterable, Mapping, Sequence, TypeVar

from pipeline.literary.builder_pilot import ValidationReport
from pipeline.literary.builder_schema_v3 import (
    ATTRIBUTION_METHODS,
    FACT_TYPES,
    FRAME_KINDS,
    FRAME_STATUSES,
    GLOSSARY_CATEGORIES,
    INFERENCE_BASES,
    MENTION_TYPES,
    PHASE_LEAK_EVENT_TYPES,
    REFERENT_KIND_CLAIMS,
    REFERENCE_SCOPES,
    REGISTER_CUES,
    RETIRED_FIELDS,
    SCENE_SHAPES,
    STATE_ATTRIBUTES,
    STORY_TIME_LABELS,
    SURFACE_KINDS,
    THREAD_KINDS,
    TIME_FRAME_HINTS,
    VALENCE_HINTS,
)
from pipeline.literary.source_anchor import (
    SourceAnchor,
    SourceInterval,
    SourcePoint,
    block_order_index,
    locate_anchor,
    mint_mention_ids,
    mint_turn_event_ids,
    nfc_block_string,
    nfc_text,
)


T = TypeVar("T")


@dataclass(frozen=True)
class ValidationResult(Generic[T]):
    """The Builder-v3 validator return contract.

    ``payload`` is always a deep-copied normalized payload.  Consumers can
    inspect it to prove that a fail-closed row was not emitted.
    """

    payload: T
    report: ValidationReport


@dataclass(frozen=True)
class FieldContract:
    kind: str
    required: bool = True
    nullable: bool = False
    allowed: frozenset[str] | None = None


def _field(
    kind: str,
    *,
    required: bool = True,
    nullable: bool = False,
    allowed: set[str] | None = None,
) -> FieldContract:
    return FieldContract(kind, required, nullable, frozenset(allowed) if allowed else None)


SHAPE_CONTRACTS: dict[str, dict[str, FieldContract]] = {
    "ChapterBrief": {
        "chapter_id": _field("str"),
        "cast_claims": _field("list"),
        "setting": _field("mapping"),
        "scenes_party_size": _field("list"),
        "neutral_premise": _field("str"),
    },
    "Setting": {
        "place": _field("str"),
        "time_frame_hint": _field("str", allowed=TIME_FRAME_HINTS),
        "scene_shape": _field("str", allowed=SCENE_SHAPES),
    },
    "CastClaim": {
        "surface": _field("str"),
        "surface_kind": _field("str", allowed=SURFACE_KINDS),
        "referent_kind_claim": _field("str", allowed=REFERENT_KIND_CLAIMS),
        "role_hint": _field("str"),
        "scene_range": _field("pair_str"),
        "source_block_ids": _field("list_str_nonempty"),
        "anchor_text": _field("str"),
        "evidence_quote": _field("str"),
        "occurrence_hint": _field("positive_int", required=False),
    },
    "Scene": {
        "block_range": _field("pair_str"),
        "co_present_count": _field("nonnegative_int"),
        "participants": _field("list_str"),
    },
    "Lexicon": {
        "chapter_id": _field("str"),
        "window_block_ids": _field("list_str_nonempty"),
        "context_only_used": _field("bool"),
        "character_mentions": _field("list"),
        "glossary_candidates": _field("list"),
    },
    "Mention": {
        "surface": _field("str"),
        "mention_type": _field("str", allowed=MENTION_TYPES),
        "referent_kind_claim": _field("str", allowed=REFERENT_KIND_CLAIMS),
        "anchor_text": _field("str"),
        "evidence_quote": _field("str"),
        "occurrence_hint": _field("positive_int", required=False),
        "block_id": _field("str"),
    },
    "Glossary": {
        "source_term": _field("str"),
        "proposed_target_vi": _field("str"),
        "category": _field("str", allowed=GLOSSARY_CATEGORIES),
        "do_not_translate": _field("bool"),
        "block_ids": _field("list_str_nonempty"),
    },
    "Narrative": {
        "chapter_id": _field("str"),
        "window_block_ids": _field("list_str_nonempty"),
        "context_only_used": _field("bool"),
        "speaker_turns": _field("list"),
        "relation_events": _field("list"),
    },
    "Endpoint": {
        "surface": _field("str"),
        "reference_scope": _field("str", allowed=REFERENCE_SCOPES),
        "referent_kind_claim": _field("str", allowed=REFERENT_KIND_CLAIMS),
        "mention_ref": _field("str", nullable=True),
        "attribution_method": _field("str", allowed=ATTRIBUTION_METHODS),
        "anchor_text": _field("str"),
        "evidence_quote": _field("str"),
        "occurrence_hint": _field("positive_int", required=False),
    },
    "Turn": {
        "speaker": _field("mapping"),
        "addressee": _field("mapping", nullable=True),
        "utterance_quote": _field("str"),
        "address_terms": _field("list"),
        "register_cue": _field("str", allowed=REGISTER_CUES),
        "block_id": _field("str"),
    },
    "AddressTerm": {
        "anchor_text": _field("str"),
        "evidence_quote": _field("str"),
        "occurrence_hint": _field("positive_int", required=False),
        "addressee_ref": _field("str", allowed={"speaker", "addressee"}),
    },
    "Event": {
        "actor": _field("mapping"),
        "target": _field("mapping"),
        "event_type": _field("str"),
        "evidence_quote": _field("str"),
        "block_id": _field("str"),
    },
    "Digest": {
        "chapter_id": _field("str"),
        "chapter_rolling_summary": _field("str"),
        "narration_frame_segments": _field("list"),
        "relation_observations": _field("list"),
        "character_state_changes": _field("list"),
        "unresolved_threads": _field("list"),
        "translator_relevant_facts": _field("list"),
        "motifs": _field("list"),
    },
    "FrameSegment": {
        "local_segment_key": _field("str"),
        "parent_local_key": _field("str", nullable=True),
        "narrator_surface": _field("str"),
        "narrator_ref": _field("str", nullable=True),
        "frame_kind": _field("str", allowed=FRAME_KINDS),
        "story_time_label": _field("str", allowed=STORY_TIME_LABELS),
        "block_range": _field("pair_str"),
        "start_boundary": _field("mapping", nullable=True),
        "end_boundary": _field("mapping", nullable=True),
        "status": _field("str", allowed=FRAME_STATUSES),
        "evidence_quote": _field("str"),
    },
    "SourceBoundary": {
        "anchor_text": _field("str"),
        "evidence_quote": _field("str"),
        "occurrence_hint": _field("positive_int", required=False),
    },
    "RelationObservation": {
        "event_id": _field("str"),
        "endpoint_refs": _field("pair_str"),
        "observed_valence_hint": _field("str", allowed=VALENCE_HINTS),
        "block_id": _field("str"),
        "evidence_quote": _field("str"),
        "transition_hint": _field("mapping", required=False),
    },
    "TransitionHint": {
        "trigger_event_id": _field("str"),
        "note": _field("str"),
    },
    "StateChange": {
        "subject_ref": _field("str"),
        "attribute": _field("str", allowed=STATE_ATTRIBUTES),
        "from_value": _field("str"),
        "to_value": _field("str"),
        "trigger_ref": _field("str"),
        "evidence_quote": _field("str"),
    },
    "Thread": {
        "thread_local_id": _field("str"),
        "description": _field("str"),
        "opened_block": _field("str"),
        "kind": _field("str", allowed=THREAD_KINDS),
        "subject_refs": _field("list_str", required=False),
    },
    "Fact": {
        "fact_type": _field("str", allowed=FACT_TYPES),
        "fact": _field("str"),
        "block_evidence": _field("list_str_nonempty"),
        "inference_basis": _field("str", allowed=INFERENCE_BASES),
        "subject_ref": _field("str", required=False),
        "event_ids": _field("list_str", required=False),
    },
    "Motif": {
        "note": _field("str"),
        "block_ids": _field("list_str_nonempty"),
        "subject_refs": _field("list_str", required=False),
    },
}


def _matches_kind(value: Any, kind: str) -> bool:
    if kind == "str":
        return isinstance(value, str)
    if kind == "bool":
        return isinstance(value, bool)
    if kind in {"positive_int", "nonnegative_int"}:
        if not isinstance(value, int) or isinstance(value, bool):
            return False
        return value > 0 if kind == "positive_int" else value >= 0
    if kind == "mapping":
        return isinstance(value, Mapping)
    if kind == "list":
        return isinstance(value, list)
    if kind in {"list_str", "list_str_nonempty"}:
        return (
            isinstance(value, list)
            and (kind != "list_str_nonempty" or bool(value))
            and all(isinstance(item, str) for item in value)
        )
    if kind == "pair_str":
        return isinstance(value, list) and len(value) == 2 and all(
            isinstance(item, str) for item in value
        )
    raise ValueError(f"unknown field-contract kind: {kind}")


def validate_shape_contract(
    shape_name: str,
    value: Any,
    *,
    path: str | None = None,
) -> list[str]:
    """Validate required/type/nullability/enum without mutating the value."""

    label = path or shape_name
    if not isinstance(value, Mapping):
        return [f"{label} must be an object"]
    contract = SHAPE_CONTRACTS[shape_name]
    errors: list[str] = []
    for field_name, field_contract in contract.items():
        if field_name not in value:
            if field_contract.required:
                errors.append(f"{label}.{field_name} is required")
            continue
        field_value = value[field_name]
        if field_value is None:
            if not field_contract.nullable:
                errors.append(f"{label}.{field_name} is not nullable")
            continue
        if not _matches_kind(field_value, field_contract.kind):
            errors.append(f"{label}.{field_name} has invalid type/shape")
            continue
        if field_contract.allowed is not None and str(field_value) not in field_contract.allowed:
            errors.append(f"{label}.{field_name} outside enum: {field_value!r}")
    return errors


def _shape_ok(
    shape_name: str,
    value: Any,
    *,
    path: str,
    errors: list[str],
    counts: Counter[str],
) -> bool:
    shape_errors = validate_shape_contract(shape_name, value, path=path)
    errors.extend(shape_errors)
    if shape_errors:
        counts["fatal_shape_contract"] += len(shape_errors)
        return False
    return True


def _report(
    name: str,
    errors: list[str],
    warnings: list[str],
    counts: Counter[str],
) -> ValidationReport:
    return ValidationReport(
        name=name,
        ok=not errors,
        errors=list(errors),
        warnings=list(warnings),
        counts=dict(counts),
    )


def _clone_and_strip_retired(
    value: Any,
    counts: Counter[str],
) -> Any:
    if isinstance(value, Mapping):
        copied: dict[str, Any] = {}
        for raw_key, raw_value in value.items():
            key = str(raw_key)
            if key in RETIRED_FIELDS:
                counts[f"retired_{key}_stripped"] += 1
                counts["retired_fields_stripped"] += 1
                continue
            copied[key] = _clone_and_strip_retired(raw_value, counts)
        return copied
    if isinstance(value, list):
        return [_clone_and_strip_retired(item, counts) for item in value]
    if isinstance(value, tuple):
        return tuple(_clone_and_strip_retired(item, counts) for item in value)
    return deepcopy(value)


def _blocks_by_id(blocks: Sequence[Mapping[str, Any]]) -> dict[str, Mapping[str, Any]]:
    mapped: dict[str, Mapping[str, Any]] = {}
    for block in blocks:
        block_id = str(block.get("block_id") or "")
        if block_id:
            mapped[block_id] = block
    return mapped


def _coverage_block_ids(blocks: Sequence[Mapping[str, Any]]) -> list[str]:
    return [
        str(block["block_id"])
        for block in blocks
        if str(block.get("block_type") or "") in {"paragraph", "dialogue"}
        and str(block.get("block_id") or "")
    ]


def _range_indices(
    block_range: Any,
    order: Mapping[str, int],
) -> tuple[int, int] | None:
    if not isinstance(block_range, (list, tuple)) or len(block_range) != 2:
        return None
    start, end = str(block_range[0]), str(block_range[1])
    if start not in order or end not in order:
        return None
    indices = (order[start], order[end])
    return indices if indices[0] <= indices[1] else None


def _range_block_ids(block_range: Any, coverage_ids: Sequence[str]) -> list[str]:
    order = {block_id: index for index, block_id in enumerate(coverage_ids)}
    indices = _range_indices(block_range, order)
    if indices is None:
        return []
    return list(coverage_ids[indices[0] : indices[1] + 1])


def _enum(
    value: Any,
    allowed: set[str],
    field: str,
    errors: list[str],
) -> bool:
    if str(value) in allowed:
        return True
    errors.append(f"{field} outside enum: {value!r}")
    return False


def _require_mapping(
    value: Any,
    field: str,
    errors: list[str],
) -> dict[str, Any] | None:
    if not isinstance(value, Mapping):
        errors.append(f"{field} must be an object")
        return None
    return dict(value)


def _require_list(
    value: Any,
    field: str,
    errors: list[str],
) -> list[Any] | None:
    if not isinstance(value, list):
        errors.append(f"{field} must be a list")
        return None
    return value


def _anchor_dict(anchor: SourceAnchor) -> dict[str, int | str]:
    return anchor.to_dict()


def _locate_in_block(
    item: Mapping[str, Any],
    block: Mapping[str, Any],
    *,
    counts: Counter[str],
    warnings: list[str],
    label: str,
) -> dict[str, Any] | None:
    anchor_text = str(item.get("anchor_text") or "")
    evidence_quote = str(item.get("evidence_quote") or "")
    surface = item.get("surface")
    if surface is not None and nfc_text(str(surface)) != nfc_text(anchor_text):
        counts["dropped_surface_anchor_mismatch"] += 1
        warnings.append(f"{label} dropped: surface and anchor_text differ")
        return None
    hint = item.get("occurrence_hint")
    hint_value = int(hint) if isinstance(hint, int) and hint > 0 else None
    located = locate_anchor(
        block,
        anchor_text=anchor_text,
        evidence_quote=evidence_quote,
        occurrence_hint=hint_value,
    )
    if not located.ok:
        counts["fail_closed_locate"] += 1
        counts[f"dropped_{label}"] += 1
        warnings.append(f"{label} dropped: {located.failure_reason}")
        return None
    copied = dict(item)
    copied["anchor"] = _anchor_dict(located.anchor)
    return copied


def _unique_ids(
    rows: Iterable[Mapping[str, Any]],
    field: str,
    errors: list[str],
    counts: Counter[str],
) -> None:
    values = [str(row.get(field) or "") for row in rows]
    nonempty = [value for value in values if value]
    if len(nonempty) != len(set(nonempty)):
        errors.append(f"duplicate {field}")
        counts[f"duplicate_{field}"] += 1


def _endpoint_eligibility(
    endpoint: dict[str, Any],
    *,
    errors: list[str],
    warnings: list[str],
    counts: Counter[str],
    label: str,
) -> None:
    scope = str(endpoint.get("reference_scope") or "")
    kind = str(endpoint.get("referent_kind_claim") or "")
    scope_ok = _enum(scope, REFERENCE_SCOPES, f"{label}.reference_scope", errors)
    kind_ok = _enum(kind, REFERENT_KIND_CLAIMS, f"{label}.referent_kind_claim", errors)
    if not (scope_ok and kind_ok):
        endpoint["runtime_eligibility"] = "route_out"
        return

    if scope in {"narrator", "reader"}:
        if kind in {"person", "unknown"}:
            endpoint["runtime_eligibility"] = "discourse_only"
            counts["flag_discourse_only_endpoint"] += 1
            warnings.append(f"{label} retained as discourse_only")
        else:
            endpoint["runtime_eligibility"] = "invalid"
            counts["flag_invalid_two_axis"] += 1
            warnings.append(f"{label} retained with invalid narrator/reader ontology")
        return

    if scope == "individual":
        if kind == "person":
            endpoint["runtime_eligibility"] = "eligible"
        elif kind in {"animal", "nonhuman_character"}:
            endpoint["runtime_eligibility"] = "route_out"
            counts["route_out_endpoint"] += 1
        elif kind == "unknown":
            endpoint["runtime_eligibility"] = "deferred"
            counts["deferred_endpoint"] += 1
        else:
            endpoint["runtime_eligibility"] = "invalid"
            counts["flag_invalid_two_axis"] += 1
            warnings.append(f"{label} retained with invalid individual ontology")
        return

    if scope == "group":
        if kind == "group_reference":
            endpoint["runtime_eligibility"] = "route_out"
            counts["route_out_endpoint"] += 1
        elif kind == "unknown":
            endpoint["runtime_eligibility"] = "deferred"
            counts["deferred_endpoint"] += 1
        else:
            endpoint["runtime_eligibility"] = "invalid"
            counts["flag_invalid_two_axis"] += 1
            warnings.append(f"{label} retained with invalid group ontology")
        return

    if scope == "unknown":
        endpoint["runtime_eligibility"] = "deferred"
        counts["deferred_endpoint"] += 1
        return

    endpoint["runtime_eligibility"] = "invalid"
    if scope_ok and kind_ok:
        counts["flag_invalid_two_axis"] += 1
        warnings.append(f"{label} retained with invalid two-axis combination")


def _normalize_endpoint(
    endpoint_value: Any,
    *,
    block: Mapping[str, Any],
    mention_ids: set[str],
    errors: list[str],
    warnings: list[str],
    counts: Counter[str],
    label: str,
) -> dict[str, Any] | None:
    endpoint = _require_mapping(endpoint_value, label, errors)
    if endpoint is None:
        return None
    if not _shape_ok(
        "Endpoint",
        endpoint,
        path=label,
        errors=errors,
        counts=counts,
    ):
        counts["dropped_invalid_endpoint"] += 1
        return None
    mention_ref = endpoint.get("mention_ref")
    if mention_ref is not None and str(mention_ref) not in mention_ids:
        counts["dropped_unresolved_mention_ref"] += 1
        warnings.append(f"{label} dropped: mention_ref is outside same-window mentions")
        return None
    if not _enum(
        endpoint.get("attribution_method"),
        ATTRIBUTION_METHODS,
        f"{label}.attribution_method",
        errors,
    ):
        return None
    located = _locate_in_block(
        endpoint,
        block,
        counts=counts,
        warnings=warnings,
        label=label,
    )
    if located is None:
        return None
    _endpoint_eligibility(
        located,
        errors=errors,
        warnings=warnings,
        counts=counts,
        label=label,
    )
    located["resolution_evidence"] = str(located.get("evidence_quote") or "")
    return located


def validate_chapter_brief_v3(
    payload: Mapping[str, Any],
    *,
    blocks: Sequence[Mapping[str, Any]],
) -> ValidationResult[dict[str, Any]]:
    """Validate B0 scene coverage and source-grounded cast claims."""

    counts: Counter[str] = Counter()
    errors: list[str] = []
    warnings: list[str] = []
    normalized = _clone_and_strip_retired(payload, counts)
    if not isinstance(normalized, dict):
        report = _report("chapter_brief_v3", ["payload must be an object"], warnings, counts)
        return ValidationResult({}, report)

    block_map = _blocks_by_id(blocks)
    coverage_ids = _coverage_block_ids(blocks)
    coverage_order = {block_id: index for index, block_id in enumerate(coverage_ids)}
    if not _shape_ok(
        "ChapterBrief",
        normalized,
        path="chapter_brief",
        errors=errors,
        counts=counts,
    ):
        return ValidationResult(
            normalized,
            _report("chapter_brief_v3", errors, warnings, counts),
        )
    chapter_id = str(normalized.get("chapter_id") or "")
    if not chapter_id:
        errors.append("chapter_id is required")

    setting = _require_mapping(normalized.get("setting"), "setting", errors)
    if setting is not None:
        _shape_ok("Setting", setting, path="setting", errors=errors, counts=counts)

    scenes = _require_list(normalized.get("scenes_party_size"), "scenes_party_size", errors)
    covered: Counter[str] = Counter()
    if scenes is not None:
        for index, scene_value in enumerate(scenes):
            scene = _require_mapping(scene_value, f"scenes_party_size[{index}]", errors)
            if scene is None:
                continue
            if not _shape_ok(
                "Scene",
                scene,
                path=f"scenes_party_size[{index}]",
                errors=errors,
                counts=counts,
            ):
                counts["dropped_invalid_scene"] += 1
                continue
            range_ids = _range_block_ids(scene.get("block_range"), coverage_ids)
            if not range_ids:
                errors.append(f"scenes_party_size[{index}].block_range is invalid")
                continue
            for block_id in range_ids:
                covered[block_id] += 1
    for block_id in coverage_ids:
        if covered[block_id] == 0:
            errors.append(f"scene_gap: {block_id}")
            counts["scene_gap"] += 1
        elif covered[block_id] > 1:
            errors.append(f"scene_overlap: {block_id}")
            counts["scene_overlap"] += 1

    claim_rows = _require_list(normalized.get("cast_claims"), "cast_claims", errors)
    kept_claims: list[dict[str, Any]] = []
    if claim_rows is not None:
        for index, claim_value in enumerate(claim_rows):
            claim = _require_mapping(claim_value, f"cast_claims[{index}]", errors)
            if claim is None:
                continue
            if not _shape_ok(
                "CastClaim",
                claim,
                path=f"cast_claims[{index}]",
                errors=errors,
                counts=counts,
            ):
                counts["dropped_invalid_cast_claim"] += 1
                continue
            if nfc_text(str(claim.get("surface") or "")) != nfc_text(
                str(claim.get("anchor_text") or "")
            ):
                errors.append(f"cast_claims[{index}].surface and anchor_text differ")
                counts["dropped_surface_anchor_mismatch"] += 1
                continue
            source_ids = claim.get("source_block_ids")
            if not isinstance(source_ids, list) or not source_ids:
                errors.append(f"cast_claims[{index}].source_block_ids is required")
                continue
            scene_ids = _range_block_ids(claim.get("scene_range"), coverage_ids)
            if not scene_ids:
                errors.append(f"cast_claims[{index}].scene_range is invalid")
                continue
            outside = [str(block_id) for block_id in source_ids if str(block_id) not in scene_ids]
            if outside:
                errors.append(f"cast_claims[{index}].source_block_ids outside scene_range: {outside}")
                counts["cast_claim_source_outside_scene"] += 1
                continue
            candidates: list[tuple[dict[str, Any], int]] = []
            for source_id in source_ids:
                block = block_map.get(str(source_id))
                if block is None:
                    errors.append(f"cast_claims[{index}].source_block_ids unknown: {source_id}")
                    continue
                located = locate_anchor(
                    block,
                    anchor_text=str(claim.get("anchor_text") or ""),
                    evidence_quote=str(claim.get("evidence_quote") or ""),
                    occurrence_hint=(
                        int(claim["occurrence_hint"])
                        if isinstance(claim.get("occurrence_hint"), int)
                        and int(claim["occurrence_hint"]) > 0
                        else None
                    ),
                )
                if located.ok:
                    located_claim = dict(claim)
                    located_claim["anchor"] = _anchor_dict(located.anchor)
                    candidates.append((located_claim, int(block.get("order_index") or 0)))
            if len(candidates) != 1:
                counts["fail_closed_locate"] += 1
                counts["dropped_cast_claim"] += 1
                if candidates:
                    warnings.append("cast_claim dropped: anchor locates in multiple source blocks")
                else:
                    warnings.append("cast_claim dropped: anchor does not locate in a source block")
                continue
            located_claim, _ = candidates[0]
            located_claim["evidence_max_order"] = max(
                int(block_map[str(block_id)].get("order_index") or 0) for block_id in source_ids
            )
            kept_claims.append(located_claim)

    def claim_sort_key(claim: Mapping[str, Any]) -> tuple[int, int, int, str]:
        anchor = SourceAnchor.from_value(claim["anchor"])
        return (
            coverage_order.get(anchor.block_id, 10**9),
            anchor.char_start,
            anchor.char_end,
            str(claim.get("surface") or ""),
        )

    for ordinal, claim in enumerate(sorted(kept_claims, key=claim_sort_key), start=1):
        claim["cast_claim_id"] = f"cc_{chapter_id}_{ordinal:02d}"
    normalized["cast_claims"] = kept_claims
    normalized["input_max_order"] = max(
        (int(block.get("order_index") or 0) for block in blocks),
        default=0,
    )
    counts["cast_claims"] = len(kept_claims)
    return ValidationResult(normalized, _report("chapter_brief_v3", errors, warnings, counts))


def validate_lexicon_v3(
    payload: Mapping[str, Any],
    *,
    blocks: Sequence[Mapping[str, Any]],
) -> ValidationResult[dict[str, Any]]:
    """Validate B1 occurrence rows and mint their block-global ids."""

    counts: Counter[str] = Counter()
    errors: list[str] = []
    warnings: list[str] = []
    normalized = _clone_and_strip_retired(payload, counts)
    if not isinstance(normalized, dict):
        return ValidationResult({}, _report("lexicon_v3", ["payload must be an object"], warnings, counts))
    block_map = _blocks_by_id(blocks)
    if not _shape_ok(
        "Lexicon",
        normalized,
        path="lexicon",
        errors=errors,
        counts=counts,
    ):
        return ValidationResult(normalized, _report("lexicon_v3", errors, warnings, counts))
    window_ids = {str(value) for value in normalized.get("window_block_ids") or []}
    if not window_ids:
        errors.append("window_block_ids is required")
    rows = _require_list(normalized.get("character_mentions"), "character_mentions", errors)
    kept: list[dict[str, Any]] = []
    if rows is not None:
        for index, mention_value in enumerate(rows):
            mention = _require_mapping(mention_value, f"character_mentions[{index}]", errors)
            if mention is None:
                continue
            if not _shape_ok(
                "Mention",
                mention,
                path=f"character_mentions[{index}]",
                errors=errors,
                counts=counts,
            ):
                counts["dropped_invalid_mention"] += 1
                continue
            block_id = str(mention.get("block_id") or "")
            if block_id not in window_ids or block_id not in block_map:
                counts["dropped_mention_outside_window"] += 1
                warnings.append(f"mention dropped: block outside window {block_id}")
                continue
            located = _locate_in_block(
                mention,
                block_map[block_id],
                counts=counts,
                warnings=warnings,
                label="mention",
            )
            if located is not None:
                kept.append(located)
    try:
        kept = mint_mention_ids(kept)
    except ValueError as exc:
        errors.append(str(exc))
    normalized["character_mentions"] = kept
    _unique_ids(kept, "mention_id", errors, counts)

    glossary_rows = _require_list(normalized.get("glossary_candidates"), "glossary_candidates", errors)
    kept_glossary: list[dict[str, Any]] = []
    if glossary_rows is not None:
        for index, glossary_value in enumerate(glossary_rows):
            glossary = _require_mapping(glossary_value, f"glossary_candidates[{index}]", errors)
            if glossary is None:
                continue
            if not _shape_ok(
                "Glossary",
                glossary,
                path=f"glossary_candidates[{index}]",
                errors=errors,
                counts=counts,
            ):
                counts["dropped_invalid_glossary"] += 1
                continue
            kept_glossary.append(glossary)
    normalized["glossary_candidates"] = kept_glossary
    counts["mentions"] = len(kept)
    counts["glossary_candidates"] = len(kept_glossary)
    return ValidationResult(normalized, _report("lexicon_v3", errors, warnings, counts))


def validate_narrative_v3(
    payload: Mapping[str, Any],
    *,
    blocks: Sequence[Mapping[str, Any]],
    mentions: Sequence[Mapping[str, Any]] = (),
) -> ValidationResult[dict[str, Any]]:
    """Validate B2 endpoints, route non-person rows, and mint turn/event ids."""

    counts: Counter[str] = Counter()
    errors: list[str] = []
    warnings: list[str] = []
    normalized = _clone_and_strip_retired(payload, counts)
    if not isinstance(normalized, dict):
        return ValidationResult({}, _report("narrative_v3", ["payload must be an object"], warnings, counts))
    block_map = _blocks_by_id(blocks)
    if not _shape_ok(
        "Narrative",
        normalized,
        path="narrative",
        errors=errors,
        counts=counts,
    ):
        return ValidationResult(normalized, _report("narrative_v3", errors, warnings, counts))
    valid_window_ids = {str(value) for value in normalized.get("window_block_ids") or []}
    mention_ids = {
        str(row.get("mention_id"))
        for row in mentions
        if row.get("mention_id")
        and str(row.get("block_id") or "") in valid_window_ids
    }
    if not valid_window_ids:
        errors.append("window_block_ids is required")

    turn_rows = _require_list(normalized.get("speaker_turns"), "speaker_turns", errors)
    kept_turns: list[dict[str, Any]] = []
    if turn_rows is not None:
        for index, turn_value in enumerate(turn_rows):
            turn = _require_mapping(turn_value, f"speaker_turns[{index}]", errors)
            if turn is None:
                continue
            if not _shape_ok(
                "Turn",
                turn,
                path=f"speaker_turns[{index}]",
                errors=errors,
                counts=counts,
            ):
                counts["dropped_invalid_turn"] += 1
                continue
            block_id = str(turn.get("block_id") or "")
            if block_id not in valid_window_ids or block_id not in block_map:
                counts["dropped_turn_outside_window"] += 1
                continue
            if not _enum(turn.get("register_cue"), REGISTER_CUES, f"turn[{index}].register_cue", errors):
                continue
            speaker = _normalize_endpoint(
                turn.get("speaker"),
                block=block_map[block_id],
                mention_ids=mention_ids,
                errors=errors,
                warnings=warnings,
                counts=counts,
                label="speaker",
            )
            addressee_value = turn.get("addressee")
            addressee = None
            if addressee_value is not None:
                addressee = _normalize_endpoint(
                    addressee_value,
                    block=block_map[block_id],
                    mention_ids=mention_ids,
                    errors=errors,
                    warnings=warnings,
                    counts=counts,
                    label="addressee",
                )
            if speaker is None or (addressee_value is not None and addressee is None):
                counts["dropped_turn"] += 1
                continue
            address_terms: list[dict[str, Any]] = []
            raw_terms = turn.get("address_terms") or []
            if not isinstance(raw_terms, list):
                errors.append(f"turn[{index}].address_terms must be a list")
                continue
            for term_index, term_value in enumerate(raw_terms):
                term = _require_mapping(term_value, f"turn[{index}].address_terms[{term_index}]", errors)
                if term is None:
                    continue
                if not _shape_ok(
                    "AddressTerm",
                    term,
                    path=f"speaker_turns[{index}].address_terms[{term_index}]",
                    errors=errors,
                    counts=counts,
                ):
                    counts["dropped_invalid_address_term"] += 1
                    continue
                if term.get("addressee_ref") == "addressee" and addressee is None:
                    counts["dropped_address_term_without_addressee"] += 1
                    warnings.append("address term dropped because addressee is null")
                    continue
                located_term = _locate_in_block(
                    term,
                    block_map[block_id],
                    counts=counts,
                    warnings=warnings,
                    label="address_term",
                )
                if located_term is not None:
                    address_terms.append(located_term)
            turn["speaker"] = speaker
            turn["addressee"] = addressee
            turn["address_terms"] = address_terms
            kept_turns.append(turn)

    event_rows = _require_list(normalized.get("relation_events"), "relation_events", errors)
    kept_events: list[dict[str, Any]] = []
    if event_rows is not None:
        for index, event_value in enumerate(event_rows):
            event = _require_mapping(event_value, f"relation_events[{index}]", errors)
            if event is None:
                continue
            if not _shape_ok(
                "Event",
                event,
                path=f"relation_events[{index}]",
                errors=errors,
                counts=counts,
            ):
                counts["dropped_invalid_event"] += 1
                continue
            block_id = str(event.get("block_id") or "")
            if block_id not in valid_window_ids or block_id not in block_map:
                counts["dropped_event_outside_window"] += 1
                continue
            event_type = str(event.get("event_type") or "")
            if not re.fullmatch(r"[a-z][a-z0-9_]*", event_type):
                errors.append(f"event[{index}].event_type must be lower_snake_case")
                continue
            if event_type in PHASE_LEAK_EVENT_TYPES:
                errors.append(f"phase_leak: event[{index}].event_type={event_type}")
                counts["phase_leak"] += 1
                continue
            actor = _normalize_endpoint(
                event.get("actor"),
                block=block_map[block_id],
                mention_ids=mention_ids,
                errors=errors,
                warnings=warnings,
                counts=counts,
                label="actor",
            )
            target = _normalize_endpoint(
                event.get("target"),
                block=block_map[block_id],
                mention_ids=mention_ids,
                errors=errors,
                warnings=warnings,
                counts=counts,
                label="target",
            )
            if actor is None or target is None:
                counts["dropped_event"] += 1
                continue
            event["actor"] = actor
            event["target"] = target
            if (
                actor.get("runtime_eligibility") != "eligible"
                or target.get("runtime_eligibility") != "eligible"
            ):
                event["runtime_eligibility"] = "route_out"
                counts["route_out_event"] += 1
            else:
                event["runtime_eligibility"] = "eligible"
            kept_events.append(event)

    try:
        minted_turns, minted_events = mint_turn_event_ids(
            kept_turns,
            kept_events,
            block_order=block_order_index(blocks),
        )
    except ValueError as exc:
        errors.append(str(exc))
        minted_turns, minted_events = kept_turns, kept_events
    normalized["speaker_turns"] = minted_turns
    normalized["relation_events"] = minted_events
    _unique_ids(minted_turns, "turn_id", errors, counts)
    _unique_ids(minted_events, "event_id", errors, counts)
    endpoint_rows = [
        endpoint
        for turn in minted_turns
        for endpoint in (turn.get("speaker"), turn.get("addressee"))
        if isinstance(endpoint, Mapping)
    ] + [
        endpoint
        for event in minted_events
        for endpoint in (event.get("actor"), event.get("target"))
        if isinstance(endpoint, Mapping)
    ]
    _unique_ids(endpoint_rows, "endpoint_id", errors, counts)
    counts["speaker_turns"] = len(minted_turns)
    counts["relation_events"] = len(minted_events)
    return ValidationResult(normalized, _report("narrative_v3", errors, warnings, counts))


def _frame_depths(frames: Mapping[str, Mapping[str, Any]]) -> dict[str, int]:
    depths: dict[str, int] = {}

    def visit(key: str, seen: set[str]) -> int:
        if key in depths:
            return depths[key]
        if key in seen:
            raise ValueError("frame_cycle")
        parent = frames[key].get("parent_local_key")
        if parent is None:
            depths[key] = 0
        else:
            parent_key = str(parent)
            if parent_key not in frames:
                raise KeyError(parent_key)
            depths[key] = visit(parent_key, seen | {key}) + 1
        return depths[key]

    for key in frames:
        visit(key, set())
    return depths


def _frame_source_interval(
    frame: Mapping[str, Any],
    *,
    frame_ids: Sequence[str],
    order: Mapping[str, int],
    block_map: Mapping[str, Mapping[str, Any]],
) -> SourceInterval:
    start_anchor = frame.get("start_anchor")
    end_anchor = frame.get("end_anchor")
    start_offset = (
        SourceAnchor.from_value(start_anchor).char_start if start_anchor is not None else 0
    )
    end_offset = (
        SourceAnchor.from_value(end_anchor).char_end
        if end_anchor is not None
        else len(nfc_block_string(block_map[frame_ids[-1]]))
    )
    return SourceInterval(
        SourcePoint(order[frame_ids[0]], start_offset),
        SourcePoint(order[frame_ids[-1]], end_offset),
    )


def _source_interval_from_payload(frame: Mapping[str, Any]) -> SourceInterval:
    value = frame["source_interval"]
    start = value["start"]
    end = value["end"]
    return SourceInterval(
        SourcePoint(int(start["block_order"]), int(start["char_offset"])),
        SourcePoint(int(end["block_order"]), int(end["char_offset"])),
    )


def _deepest_active_spans(
    frames: Mapping[str, Mapping[str, Any]],
    depths: Mapping[str, int],
    *,
    coverage_ids: Sequence[str],
    order: Mapping[str, int],
    block_map: Mapping[str, Mapping[str, Any]],
    errors: list[str],
    counts: Counter[str],
) -> list[dict[str, Any]]:
    intervals = {key: _source_interval_from_payload(frame) for key, frame in frames.items()}
    spans: list[dict[str, Any]] = []
    for block_id in coverage_ids:
        block_index = order[block_id]
        block_length = len(nfc_block_string(block_map[block_id]))
        boundaries = {0, block_length}
        for interval in intervals.values():
            if interval.start.block_order == block_index:
                boundaries.add(interval.start.char_offset)
            if interval.end.block_order == block_index:
                boundaries.add(interval.end.char_offset)
        ordered_boundaries = sorted(offset for offset in boundaries if 0 <= offset <= block_length)
        for char_start, char_end in zip(ordered_boundaries, ordered_boundaries[1:]):
            if char_start == char_end:
                continue
            point = SourcePoint(block_index, char_start)
            containing = [key for key, interval in intervals.items() if interval.contains_point(point)]
            if not containing:
                errors.append(f"frame_leaf_gap: {block_id}:{char_start}-{char_end}")
                counts["frame_leaf_gap"] += 1
                continue
            highest = max(depths[key] for key in containing)
            deepest = sorted(key for key in containing if depths[key] == highest)
            if len(deepest) != 1:
                errors.append(f"frame_ambiguous_deepest: {block_id}:{char_start}-{char_end}")
                counts["frame_ambiguous_deepest"] += 1
                continue
            segment_key = deepest[0]
            if (
                spans
                and spans[-1]["block_id"] == block_id
                and spans[-1]["segment_key"] == segment_key
                and spans[-1]["char_end"] == char_start
            ):
                spans[-1]["char_end"] = char_end
            else:
                spans.append(
                    {
                        "block_id": block_id,
                        "char_start": char_start,
                        "char_end": char_end,
                        "segment_key": segment_key,
                    }
                )
    return spans


def _is_occurrence_reference(value: Any, allowed: set[str]) -> bool:
    reference = str(value or "")
    return bool(reference) and not reference.startswith("ent_") and reference in allowed


def validate_digest_v3(
    payload: Mapping[str, Any],
    *,
    blocks: Sequence[Mapping[str, Any]],
    mention_ids: Iterable[str] = (),
    endpoint_ids: Iterable[str] = (),
    event_ids: Iterable[str] = (),
    event_endpoint_map: Mapping[str, Mapping[str, Any]] | None = None,
) -> ValidationResult[dict[str, Any]]:
    """Validate B3's occurrence-grounded observations, event topology, and frame tree."""

    counts: Counter[str] = Counter()
    counts["frame_cycle"] = 0
    counts["frame_missing_parent"] = 0
    errors: list[str] = []
    warnings: list[str] = []
    normalized = _clone_and_strip_retired(payload, counts)
    if not isinstance(normalized, dict):
        return ValidationResult({}, _report("digest_v3", ["payload must be an object"], warnings, counts))
    block_map = _blocks_by_id(blocks)
    if not _shape_ok(
        "Digest",
        normalized,
        path="digest",
        errors=errors,
        counts=counts,
    ):
        return ValidationResult(normalized, _report("digest_v3", errors, warnings, counts))
    coverage_ids = _coverage_block_ids(blocks)
    order = {block_id: index for index, block_id in enumerate(coverage_ids)}
    allowed_mentions = {str(value) for value in mention_ids}
    allowed_endpoints = {str(value) for value in endpoint_ids}
    allowed_events = {str(value) for value in event_ids}
    occurrence_refs = allowed_mentions | allowed_endpoints
    source_events = {
        str(event_id): dict(value)
        for event_id, value in (event_endpoint_map or {}).items()
        if str(event_id)
    }
    if set(source_events) != allowed_events:
        errors.append("event_endpoint_map keys must exactly match event_ids")
        counts["fatal_event_endpoint_map_key_mismatch"] += 1
    for event_id, source_event in source_events.items():
        actor_id = str(source_event.get("actor_endpoint_id") or "")
        target_id = str(source_event.get("target_endpoint_id") or "")
        block_id = str(source_event.get("block_id") or "")
        if (
            not actor_id
            or not target_id
            or actor_id not in allowed_endpoints
            or target_id not in allowed_endpoints
            or block_id not in block_map
        ):
            errors.append(f"event_endpoint_map[{event_id}] is malformed or foreign")
            counts["fatal_invalid_event_endpoint_map"] += 1

    frame_rows = _require_list(normalized.get("narration_frame_segments"), "narration_frame_segments", errors)
    kept_frames: list[dict[str, Any]] = []
    if frame_rows is not None:
        for index, frame_value in enumerate(frame_rows):
            frame = _require_mapping(frame_value, f"narration_frame_segments[{index}]", errors)
            if frame is None:
                continue
            if not _shape_ok(
                "FrameSegment",
                frame,
                path=f"narration_frame_segments[{index}]",
                errors=errors,
                counts=counts,
            ):
                counts["dropped_invalid_frame"] += 1
                continue
            key = str(frame.get("local_segment_key") or "")
            if not key:
                errors.append(f"frame[{index}].local_segment_key is required")
                continue
            narrator_ref = frame.get("narrator_ref")
            if narrator_ref is not None and not _is_occurrence_reference(
                narrator_ref, occurrence_refs
            ):
                errors.append(f"frame[{index}].narrator_ref is not occurrence-grounded")
                counts["dropped_invalid_narrator_ref"] += 1
                continue
            _enum(frame.get("frame_kind"), FRAME_KINDS, f"frame[{index}].frame_kind", errors)
            _enum(frame.get("story_time_label"), STORY_TIME_LABELS, f"frame[{index}].story_time_label", errors)
            _enum(frame.get("status"), FRAME_STATUSES, f"frame[{index}].status", errors)
            frame_ids = _range_block_ids(frame.get("block_range"), coverage_ids)
            if not frame_ids:
                errors.append(f"frame[{index}].block_range is invalid")
                continue
            boundary_failed = False
            for side, block_id in (("start", frame_ids[0]), ("end", frame_ids[-1])):
                boundary = frame.get(f"{side}_boundary")
                if boundary is None:
                    frame[f"{side}_anchor"] = None
                    continue
                boundary_map = _require_mapping(boundary, f"frame[{index}].{side}_boundary", errors)
                if boundary_map is None:
                    boundary_failed = True
                    continue
                if not _shape_ok(
                    "SourceBoundary",
                    boundary_map,
                    path=f"narration_frame_segments[{index}].{side}_boundary",
                    errors=errors,
                    counts=counts,
                ):
                    boundary_failed = True
                    continue
                located = locate_anchor(
                    block_map[block_id],
                    anchor_text=str(boundary_map.get("anchor_text") or ""),
                    evidence_quote=str(boundary_map.get("evidence_quote") or ""),
                    occurrence_hint=(
                        int(boundary_map["occurrence_hint"])
                        if isinstance(boundary_map.get("occurrence_hint"), int)
                        and int(boundary_map["occurrence_hint"]) > 0
                        else None
                    ),
                )
                if not located.ok:
                    counts["fail_closed_locate"] += 1
                    counts["dropped_frame_boundary"] += 1
                    errors.append(f"frame[{index}].{side}_boundary unlocatable")
                    boundary_failed = True
                else:
                    frame[f"{side}_anchor"] = _anchor_dict(located.anchor)
                    block_length = len(nfc_block_string(block_map[block_id]))
                    if (
                        (side == "start" and located.anchor.char_start == 0)
                        or (side == "end" and located.anchor.char_end == block_length)
                    ):
                        errors.append(
                            f"frame[{index}].{side}_boundary is not a mid-block boundary"
                        )
                        counts["dropped_non_midblock_boundary"] += 1
                        boundary_failed = True
            if boundary_failed:
                continue
            try:
                source_interval = _frame_source_interval(
                    frame,
                    frame_ids=frame_ids,
                    order=order,
                    block_map=block_map,
                )
            except ValueError as exc:
                errors.append(f"frame[{index}].source_interval invalid: {exc}")
                counts["dropped_invalid_frame_interval"] += 1
                continue
            frame["source_interval"] = source_interval.to_dict()
            frame["segment_id"] = f"seg_{normalized.get('chapter_id')}_{key}"
            frame["version"] = "builder_v3"
            kept_frames.append(frame)

    frame_by_key: dict[str, dict[str, Any]] = {}
    for frame in kept_frames:
        key = str(frame["local_segment_key"])
        if key in frame_by_key:
            errors.append(f"duplicate frame local_segment_key: {key}")
            counts["duplicate_frame_key"] += 1
        else:
            frame_by_key[key] = frame
    invalid_tree_keys: set[str] = set()
    for key, frame in frame_by_key.items():
        parent = frame.get("parent_local_key")
        if parent is not None and not isinstance(parent, str):
            errors.append(f"frame parent_local_key must be string or null: {key}")
            counts["frame_invalid_parent_key"] += 1
            invalid_tree_keys.add(key)
            continue
        if parent is not None and str(parent) not in frame_by_key:
            errors.append(f"frame missing parent: {key}->{parent}")
            counts["frame_missing_parent"] += 1
            invalid_tree_keys.add(key)
    changed = True
    while changed:
        changed = False
        for key, frame in frame_by_key.items():
            parent = frame.get("parent_local_key")
            if key not in invalid_tree_keys and parent in invalid_tree_keys:
                invalid_tree_keys.add(key)
                changed = True
    tree_frames = {
        key: frame for key, frame in frame_by_key.items() if key not in invalid_tree_keys
    }
    cycle_detected = False
    try:
        depths = _frame_depths(tree_frames)
    except ValueError as exc:
        errors.append(str(exc))
        counts["frame_cycle"] += 1
        depths = {}
        cycle_detected = True
    for key, frame in tree_frames.items():
        parent = frame.get("parent_local_key")
        if parent is None or str(parent) not in tree_frames:
            continue
        child_interval = _source_interval_from_payload(frame)
        parent_interval = _source_interval_from_payload(tree_frames[str(parent)])
        if not parent_interval.contains_interval(child_interval):
            errors.append(f"frame child outside parent: {key}")
            counts["frame_child_outside_parent"] += 1

    siblings: dict[str | None, list[tuple[str, SourceInterval]]] = defaultdict(list)
    for key, frame in tree_frames.items():
        raw_parent = frame.get("parent_local_key")
        parent_key = raw_parent if isinstance(raw_parent, str) else None
        siblings[parent_key].append((key, _source_interval_from_payload(frame)))
    for rows in siblings.values():
        sorted_rows = sorted(rows, key=lambda row: (row[1].start, row[1].end, row[0]))
        for (key, interval), (next_key, next_interval) in zip(
            sorted_rows, sorted_rows[1:]
        ):
            if interval.overlaps(next_interval):
                errors.append(f"frame sibling overlap: {key},{next_key}")
                counts["frame_sibling_overlap"] += 1

    deepest_spans: list[dict[str, Any]] = []
    if tree_frames and not cycle_detected:
        deepest_spans = _deepest_active_spans(
            tree_frames,
            depths,
            coverage_ids=coverage_ids,
            order=order,
            block_map=block_map,
            errors=errors,
            counts=counts,
        )
    deepest_active_leaf: dict[str, str] = {}
    spans_by_block: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for span in deepest_spans:
        spans_by_block[str(span["block_id"])].append(span)
    for block_id, spans in spans_by_block.items():
        segment_keys = {str(span["segment_key"]) for span in spans}
        if len(segment_keys) == 1:
            deepest_active_leaf[block_id] = next(iter(segment_keys))
    normalized["narration_frame_segments"] = kept_frames
    normalized["deepest_active_leaf_spans"] = deepest_spans
    normalized["deepest_active_leaf_by_block"] = deepest_active_leaf

    relation_rows = _require_list(normalized.get("relation_observations"), "relation_observations", errors)
    kept_relations: list[dict[str, Any]] = []
    if relation_rows is not None:
        for index, relation_value in enumerate(relation_rows):
            relation = _require_mapping(relation_value, f"relation_observations[{index}]", errors)
            if relation is None:
                continue
            if "pair" in relation:
                relation.pop("pair", None)
                counts["retired_pair_stripped"] += 1
            if not _shape_ok(
                "RelationObservation",
                relation,
                path=f"relation_observations[{index}]",
                errors=errors,
                counts=counts,
            ):
                counts["dropped_invalid_relation_observation"] += 1
                continue
            event_id = str(relation.get("event_id") or "")
            if event_id not in allowed_events:
                errors.append(f"relation_observations[{index}].event_id is not occurrence-grounded")
                continue
            refs = relation.get("endpoint_refs")
            if not isinstance(refs, list) or len(refs) != 2 or any(
                not _is_occurrence_reference(ref, allowed_endpoints) for ref in refs
            ):
                errors.append(f"relation_observations[{index}].endpoint_refs are not occurrence-grounded")
                continue
            block_id = str(relation.get("block_id") or "")
            if block_id not in block_map:
                errors.append(f"relation_observations[{index}].block_id outside chapter")
                continue
            source_event = source_events.get(event_id)
            if source_event is None:
                errors.append(f"relation_observations[{index}].event_id has no topology record")
                counts["fatal_missing_event_topology"] += 1
                continue
            expected_refs = [
                str(source_event.get("actor_endpoint_id") or ""),
                str(source_event.get("target_endpoint_id") or ""),
            ]
            if refs != expected_refs or block_id != str(source_event.get("block_id") or ""):
                errors.append(f"relation_observations[{index}] disagrees with its source event topology")
                counts["fatal_event_topology_mismatch"] += 1
                continue
            transition_hint = relation.get("transition_hint")
            if transition_hint is not None:
                if not _shape_ok(
                    "TransitionHint",
                    transition_hint,
                    path=f"relation_observations[{index}].transition_hint",
                    errors=errors,
                    counts=counts,
                ):
                    counts["dropped_invalid_transition_hint"] += 1
                    continue
                if str(transition_hint.get("trigger_event_id") or "") not in allowed_events:
                    errors.append(
                        f"relation_observations[{index}].transition_hint.trigger_event_id "
                        "is not occurrence-grounded"
                    )
                    continue
            kept_relations.append(relation)
    normalized["relation_observations"] = kept_relations

    state_rows = _require_list(normalized.get("character_state_changes"), "character_state_changes", errors)
    kept_states: list[dict[str, Any]] = []
    if state_rows is not None:
        for index, state_value in enumerate(state_rows):
            state = _require_mapping(state_value, f"character_state_changes[{index}]", errors)
            if state is None:
                continue
            if not _shape_ok(
                "StateChange",
                state,
                path=f"character_state_changes[{index}]",
                errors=errors,
                counts=counts,
            ):
                counts["dropped_invalid_state_change"] += 1
                continue
            if not _is_occurrence_reference(state.get("subject_ref"), occurrence_refs):
                errors.append(f"character_state_changes[{index}].subject_ref is not occurrence-grounded")
                continue
            trigger = str(state.get("trigger_ref") or "")
            if trigger not in allowed_events and trigger not in block_map:
                errors.append(f"character_state_changes[{index}].trigger_ref is not grounded")
                continue
            kept_states.append(state)
    normalized["character_state_changes"] = kept_states

    thread_rows = _require_list(normalized.get("unresolved_threads"), "unresolved_threads", errors)
    kept_threads: list[dict[str, Any]] = []
    if thread_rows is not None:
        for index, thread_value in enumerate(thread_rows):
            thread = _require_mapping(thread_value, f"unresolved_threads[{index}]", errors)
            if thread is None:
                continue
            if not _shape_ok(
                "Thread",
                thread,
                path=f"unresolved_threads[{index}]",
                errors=errors,
                counts=counts,
            ):
                counts["dropped_invalid_thread"] += 1
                continue
            if str(thread.get("opened_block") or "") not in block_map:
                errors.append(f"unresolved_threads[{index}].opened_block outside chapter")
                continue
            refs = thread.get("subject_refs")
            if refs is not None and (
                not isinstance(refs, list)
                or any(not _is_occurrence_reference(ref, occurrence_refs) for ref in refs)
            ):
                errors.append(f"unresolved_threads[{index}].subject_refs are not occurrence-grounded")
                continue
            kept_threads.append(thread)
    normalized["unresolved_threads"] = kept_threads

    fact_rows = _require_list(normalized.get("translator_relevant_facts"), "translator_relevant_facts", errors)
    kept_facts: list[dict[str, Any]] = []
    if fact_rows is not None:
        for index, fact_value in enumerate(fact_rows):
            fact = _require_mapping(fact_value, f"translator_relevant_facts[{index}]", errors)
            if fact is None:
                continue
            if not _shape_ok(
                "Fact",
                fact,
                path=f"translator_relevant_facts[{index}]",
                errors=errors,
                counts=counts,
            ):
                counts["dropped_invalid_fact"] += 1
                continue
            subject_ref = fact.get("subject_ref")
            if subject_ref is not None and not _is_occurrence_reference(subject_ref, occurrence_refs):
                errors.append(f"translator_relevant_facts[{index}].subject_ref is not occurrence-grounded")
                continue
            if any(str(event_id) not in allowed_events for event_id in fact.get("event_ids") or []):
                errors.append(f"translator_relevant_facts[{index}].event_ids are not occurrence-grounded")
                continue
            if any(str(block_id) not in block_map for block_id in fact.get("block_evidence") or []):
                errors.append(f"translator_relevant_facts[{index}].block_evidence outside chapter")
                continue
            kept_facts.append(fact)
    normalized["translator_relevant_facts"] = kept_facts

    motif_rows = _require_list(normalized.get("motifs"), "motifs", errors)
    kept_motifs: list[dict[str, Any]] = []
    if motif_rows is not None:
        for index, motif_value in enumerate(motif_rows):
            motif = _require_mapping(motif_value, f"motifs[{index}]", errors)
            if motif is None:
                continue
            if not _shape_ok(
                "Motif",
                motif,
                path=f"motifs[{index}]",
                errors=errors,
                counts=counts,
            ):
                counts["dropped_invalid_motif"] += 1
                continue
            if any(str(block_id) not in block_map for block_id in motif.get("block_ids") or []):
                errors.append(f"motifs[{index}].block_ids outside chapter")
                continue
            refs = motif.get("subject_refs")
            if refs is not None and any(
                not _is_occurrence_reference(ref, occurrence_refs) for ref in refs
            ):
                errors.append(f"motifs[{index}].subject_refs are not occurrence-grounded")
                continue
            kept_motifs.append(motif)
    normalized["motifs"] = kept_motifs

    counts["frames"] = len(kept_frames)
    counts["relation_observations"] = len(kept_relations)
    counts["character_state_changes"] = len(kept_states)
    counts["unresolved_threads"] = len(kept_threads)
    counts["translator_relevant_facts"] = len(kept_facts)
    counts["motifs"] = len(kept_motifs)
    return ValidationResult(normalized, _report("digest_v3", errors, warnings, counts))


def deepest_active_segment_at(
    normalized_digest: Mapping[str, Any],
    *,
    block_id: str,
    char_offset: int,
) -> str | None:
    """Resolve a validated digest's deepest frame at one source query point."""

    for span in normalized_digest.get("deepest_active_leaf_spans") or []:
        if (
            str(span.get("block_id") or "") == block_id
            and int(span.get("char_start") or 0) <= char_offset < int(span.get("char_end") or 0)
        ):
            return str(span.get("segment_key") or "") or None
    return None


def validate_builder_payload_v3(
    stage: str,
    payload: Mapping[str, Any],
    *,
    blocks: Sequence[Mapping[str, Any]],
    mentions: Sequence[Mapping[str, Any]] = (),
    mention_ids: Iterable[str] = (),
    endpoint_ids: Iterable[str] = (),
    event_ids: Iterable[str] = (),
    event_endpoint_map: Mapping[str, Mapping[str, Any]] | None = None,
) -> ValidationResult[dict[str, Any]]:
    """Small offline dispatcher used by fixtures and later orchestration work."""

    if stage == "B0":
        return validate_chapter_brief_v3(payload, blocks=blocks)
    if stage == "B1":
        return validate_lexicon_v3(payload, blocks=blocks)
    if stage == "B2":
        return validate_narrative_v3(payload, blocks=blocks, mentions=mentions)
    if stage == "B3":
        return validate_digest_v3(
            payload,
            blocks=blocks,
            mention_ids=mention_ids,
            endpoint_ids=endpoint_ids,
            event_ids=event_ids,
            event_endpoint_map=event_endpoint_map,
        )
    raise ValueError(f"unsupported Builder-v3 stage: {stage}")
