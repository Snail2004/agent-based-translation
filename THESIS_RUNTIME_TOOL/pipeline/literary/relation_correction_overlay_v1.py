"""Apply explicit human relation corrections to a sealed B1 registry.

This module is offline and mechanical. It never infers a relation from prose:
the caller names the edge, action, replacement endpoints, relation, and source
blocks. The original registry remains immutable.
"""

from __future__ import annotations

from copy import deepcopy
from typing import Any, Mapping, Sequence

from pipeline.literary.b1_chapter_registry_writer_v1 import (
    _edge_group_identity,
    _edge_identity,
    _mark_structurally_impossible_kinship_v1,
    _normalize_relation_candidate,
    verify_b1_chapter_registry_v1,
)
from pipeline.literary.b1_enrich_v1 import KINSHIP_RELATIONS, LINK_RELATIONS
from pipeline.literary.checkpoint import canonical_hash


OVERLAY_SCHEMA_VERSION = "literary_relation_correction_overlay_v1"
RECEIPT_SCHEMA_VERSION = "literary_relation_correction_receipt_v1"
CORRECTION_ACTIONS = frozenset({"retract", "replace"})
COMPONENT_KINDS = frozenset({"entity_link", "kinship_link"})


class LiteraryRelationCorrectionError(ValueError):
    pass


def apply_relation_correction_overlay_v1(
    *,
    chapter_registry: Mapping[str, Any],
    chapter: Mapping[str, Any],
    overlay: Mapping[str, Any],
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    """Return a corrected registry, normalized overlay, and apply receipt."""

    source_registry = deepcopy(dict(chapter_registry))
    verify_b1_chapter_registry_v1(source_registry)
    chapter_id, block_ids = _chapter_identity(chapter)
    if source_registry.get("chapter_id") != chapter_id:
        raise LiteraryRelationCorrectionError(
            "chapter and registry identities differ"
        )
    normalized = normalize_relation_correction_overlay_v1(
        overlay=overlay,
        source_registry=source_registry,
        block_ids=block_ids,
    )
    edges = [
        deepcopy(dict(row))
        for row in _mapping_rows(
            source_registry.get("relation_edges"), "registry relation_edges"
        )
    ]
    edge_by_id = {
        _required_string(row.get("relation_edge_id"), "relation edge id"): row
        for row in edges
    }
    known_entities = {
        _required_string(row.get("entity_id"), "registry entity id")
        for row in _mapping_rows(source_registry.get("cards"), "registry cards")
    }
    replacements: list[dict[str, Any]] = []
    replacement_ids: list[str] = []
    retracted_ids: list[str] = []
    for correction in normalized["corrections"]:
        target_id = correction["target_relation_edge_id"]
        target = edge_by_id.get(target_id)
        if target is None:
            raise LiteraryRelationCorrectionError(
                f"correction targets an unknown relation edge: {target_id}"
            )
        if target.get("semantic_status") == "human_retracted":
            raise LiteraryRelationCorrectionError(
                f"relation edge is already human-retracted: {target_id}"
            )
        target["previous_semantic_status"] = target.get("semantic_status")
        target["previous_effective"] = target.get("effective")
        target["semantic_status"] = "human_retracted"
        target["effective"] = False
        target["relation_correction_id"] = correction["correction_id"]
        retracted_ids.append(target_id)

        replacement = correction.get("replacement")
        if replacement is None:
            continue
        replacement_edge = _build_replacement_edge(
            correction=correction,
            replacement=replacement,
            chapter_id=chapter_id,
            known_entities=known_entities,
        )
        replacement_group = canonical_hash(
            _edge_group_identity(replacement_edge)
        )
        for existing in edges:
            if existing["relation_edge_id"] == target_id:
                continue
            if (
                existing.get("semantic_status") != "human_retracted"
                and canonical_hash(_edge_group_identity(existing))
                == replacement_group
            ):
                raise LiteraryRelationCorrectionError(
                    "replacement duplicates an existing active relation; "
                    "use retract instead"
                )
        replacements.append(replacement_edge)
        replacement_ids.append(replacement_edge["relation_edge_id"])

    all_edges = edges + replacements
    inactive: list[dict[str, Any]] = []
    candidates: list[dict[str, Any]] = []
    pre_contest_status: dict[str, str] = {}
    for edge in all_edges:
        status = edge.get("semantic_status")
        if edge.get("effective") is True or status == "structurally_contested":
            clean = deepcopy(edge)
            original_status = (
                clean.get("pre_contest_semantic_status")
                if status == "structurally_contested"
                else status
            )
            if not isinstance(original_status, str) or not original_status:
                original_status = "auditor_reviewed"
            for key in (
                "structurally_contested",
                "contested_group_id",
                "contested_rule",
                "pre_contest_semantic_status",
            ):
                clean.pop(key, None)
            clean["semantic_status"] = original_status
            clean["effective"] = True
            pre_contest_status[clean["relation_edge_id"]] = original_status
            candidates.append(clean)
        else:
            inactive.append(deepcopy(edge))
    candidates, structural_issues = _mark_structurally_impossible_kinship_v1(
        candidates
    )
    for edge in candidates:
        if edge.get("semantic_status") == "structurally_contested":
            edge["pre_contest_semantic_status"] = pre_contest_status[
                edge["relation_edge_id"]
            ]
    corrected_edges = sorted(
        [*inactive, *candidates], key=lambda row: row["relation_edge_id"]
    )

    corrected = deepcopy(source_registry)
    corrected.pop("registry_hash", None)
    corrected["relation_edges"] = corrected_edges
    corrected["pending_reviews"] = _replace_structural_issues(
        corrected.get("pending_reviews"), structural_issues
    )
    corrected["diagnostics"] = _replace_structural_issues(
        corrected.get("diagnostics"), structural_issues
    )
    lineage = deepcopy(dict(corrected.get("lineage") or {}))
    prior_overlay_hashes = list(
        lineage.get("relation_correction_overlay_hashes") or []
    )
    if normalized["overlay_hash"] in prior_overlay_hashes:
        raise LiteraryRelationCorrectionError(
            "relation correction overlay was already applied"
        )
    lineage["relation_correction_overlay_hashes"] = [
        *prior_overlay_hashes,
        normalized["overlay_hash"],
    ]
    corrected["lineage"] = lineage
    curation_log = deepcopy(dict(corrected.get("curation_log") or {}))
    curation_log["relation_correction_ids"] = [
        *list(curation_log.get("relation_correction_ids") or []),
        *[row["correction_id"] for row in normalized["corrections"]],
    ]
    corrected["curation_log"] = curation_log
    corrected["human_semantic_correction_performed"] = True
    metrics = deepcopy(dict(corrected.get("metrics") or {}))
    metrics["relation_edge_count"] = len(corrected_edges)
    metrics["effective_relation_edge_count"] = sum(
        row.get("effective") is True for row in corrected_edges
    )
    metrics["structural_contradiction_count"] = len(structural_issues)
    metrics["relation_correction_count"] = int(
        metrics.get("relation_correction_count") or 0
    ) + len(normalized["corrections"])
    corrected["metrics"] = metrics
    corrected["registry_hash"] = canonical_hash(corrected)
    verify_b1_chapter_registry_v1(corrected)

    receipt_body = {
        "schema_version": RECEIPT_SCHEMA_VERSION,
        "chapter_id": chapter_id,
        "source_registry_hash": source_registry["registry_hash"],
        "corrected_registry_hash": corrected["registry_hash"],
        "overlay_hash": normalized["overlay_hash"],
        "correction_ids": [
            row["correction_id"] for row in normalized["corrections"]
        ],
        "retracted_relation_edge_ids": sorted(retracted_ids),
        "replacement_relation_edge_ids": sorted(replacement_ids),
        "structural_contradiction_count_before": int(
            (source_registry.get("metrics") or {}).get(
                "structural_contradiction_count"
            )
            or 0
        ),
        "structural_contradiction_count_after": len(structural_issues),
        "human_semantic_correction_performed": True,
        "provider_calls": 0,
        "production_publish_performed": False,
    }
    receipt = {
        **receipt_body,
        "receipt_hash": canonical_hash(receipt_body),
    }
    return corrected, normalized, receipt


def normalize_relation_correction_overlay_v1(
    *,
    overlay: Mapping[str, Any],
    source_registry: Mapping[str, Any],
    block_ids: set[str],
) -> dict[str, Any]:
    if set(overlay) != {
        "schema_version",
        "chapter_id",
        "source_registry_hash",
        "corrections",
    }:
        raise LiteraryRelationCorrectionError(
            "relation correction overlay fields differ"
        )
    if overlay.get("schema_version") != OVERLAY_SCHEMA_VERSION:
        raise LiteraryRelationCorrectionError(
            "foreign relation correction overlay schema"
        )
    chapter_id = _required_string(overlay.get("chapter_id"), "chapter_id")
    if chapter_id != source_registry.get("chapter_id"):
        raise LiteraryRelationCorrectionError(
            "relation correction chapter differs"
        )
    if overlay.get("source_registry_hash") != source_registry.get(
        "registry_hash"
    ):
        raise LiteraryRelationCorrectionError(
            "relation correction source registry hash differs"
        )
    raw_rows = _mapping_rows(overlay.get("corrections"), "corrections")
    if not raw_rows:
        raise LiteraryRelationCorrectionError(
            "relation correction overlay is empty"
        )
    rows: list[dict[str, Any]] = []
    target_ids: set[str] = set()
    for raw in raw_rows:
        allowed = {
            "action",
            "target_relation_edge_id",
            "evidence_block_ids",
            "correction_note",
            "replacement",
        }
        if set(raw) - allowed or not {
            "action",
            "target_relation_edge_id",
            "evidence_block_ids",
            "correction_note",
        }.issubset(raw):
            raise LiteraryRelationCorrectionError(
                "relation correction row fields differ"
            )
        action = _required_string(raw.get("action"), "correction action")
        if action not in CORRECTION_ACTIONS:
            raise LiteraryRelationCorrectionError(
                "relation correction action is unknown"
            )
        target_id = _required_string(
            raw.get("target_relation_edge_id"), "target relation edge id"
        )
        if target_id in target_ids:
            raise LiteraryRelationCorrectionError(
                "relation correction target repeats"
            )
        target_ids.add(target_id)
        evidence = _block_list(
            raw.get("evidence_block_ids"),
            block_ids=block_ids,
            label="correction evidence_block_ids",
        )
        note = _required_string(raw.get("correction_note"), "correction note")
        replacement = raw.get("replacement")
        if action == "retract":
            if replacement is not None:
                raise LiteraryRelationCorrectionError(
                    "retract correction cannot carry a replacement"
                )
            normalized_replacement = None
        else:
            normalized_replacement = _normalize_replacement(
                replacement,
                block_ids=block_ids,
                evidence_block_ids=set(evidence),
            )
        row_body = {
            "action": action,
            "target_relation_edge_id": target_id,
            "evidence_block_ids": evidence,
            "correction_note": note,
            "replacement": normalized_replacement,
        }
        rows.append(
            {
                **row_body,
                "correction_id": (
                    "litrelcorr1_" + canonical_hash(row_body)[:20]
                ),
            }
        )
    rows.sort(key=lambda row: row["correction_id"])
    body = {
        "schema_version": OVERLAY_SCHEMA_VERSION,
        "chapter_id": chapter_id,
        "source_registry_hash": source_registry["registry_hash"],
        "corrections": rows,
        "human_semantic_correction_performed": True,
    }
    return {**body, "overlay_hash": canonical_hash(body)}


def verify_relation_correction_receipt_v1(
    receipt: Mapping[str, Any],
) -> None:
    if receipt.get("schema_version") != RECEIPT_SCHEMA_VERSION:
        raise LiteraryRelationCorrectionError(
            "foreign relation correction receipt schema"
        )
    body = dict(receipt)
    observed = body.pop("receipt_hash", None)
    if not isinstance(observed, str) or canonical_hash(body) != observed:
        raise LiteraryRelationCorrectionError(
            "relation correction receipt hash differs"
        )
    if receipt.get("human_semantic_correction_performed") is not True:
        raise LiteraryRelationCorrectionError(
            "relation correction receipt hides human intervention"
        )
    if receipt.get("provider_calls") != 0:
        raise LiteraryRelationCorrectionError(
            "relation correction receipt claims a provider call"
        )


def verify_relation_correction_bundle_v1(
    *,
    source_registry: Mapping[str, Any],
    corrected_registry: Mapping[str, Any],
    prior_cards: Mapping[str, Any],
    normalized_overlay: Mapping[str, Any],
    receipt: Mapping[str, Any],
) -> None:
    """Verify one correction bundle without reinterpreting its prose."""

    verify_b1_chapter_registry_v1(source_registry)
    verify_b1_chapter_registry_v1(corrected_registry)
    verify_relation_correction_receipt_v1(receipt)
    overlay_body = dict(normalized_overlay)
    overlay_hash = overlay_body.pop("overlay_hash", None)
    if (
        normalized_overlay.get("schema_version") != OVERLAY_SCHEMA_VERSION
        or not isinstance(overlay_hash, str)
        or canonical_hash(overlay_body) != overlay_hash
    ):
        raise LiteraryRelationCorrectionError(
            "relation correction overlay hash differs"
        )
    if normalized_overlay.get("human_semantic_correction_performed") is not True:
        raise LiteraryRelationCorrectionError(
            "relation correction overlay hides human intervention"
        )
    source_hash = source_registry.get("registry_hash")
    corrected_hash = corrected_registry.get("registry_hash")
    if (
        normalized_overlay.get("source_registry_hash") != source_hash
        or receipt.get("source_registry_hash") != source_hash
    ):
        raise LiteraryRelationCorrectionError(
            "relation correction bundle source registry differs"
        )
    if receipt.get("corrected_registry_hash") != corrected_hash:
        raise LiteraryRelationCorrectionError(
            "relation correction bundle corrected registry differs"
        )
    if (
        receipt.get("chapter_id") != source_registry.get("chapter_id")
        or corrected_registry.get("chapter_id") != source_registry.get("chapter_id")
        or normalized_overlay.get("chapter_id") != source_registry.get("chapter_id")
    ):
        raise LiteraryRelationCorrectionError(
            "relation correction bundle chapter differs"
        )
    if receipt.get("overlay_hash") != overlay_hash:
        raise LiteraryRelationCorrectionError(
            "relation correction bundle overlay differs"
        )
    correction_ids = [
        row.get("correction_id")
        for row in _mapping_rows(
            normalized_overlay.get("corrections"), "normalized corrections"
        )
    ]
    if receipt.get("correction_ids") != correction_ids:
        raise LiteraryRelationCorrectionError(
            "relation correction bundle correction ids differ"
        )
    if dict(prior_cards) != corrected_registry.get("prior_cards_projection"):
        raise LiteraryRelationCorrectionError(
            "relation correction prior cards differ"
        )
    edges = {
        _required_string(row.get("relation_edge_id"), "relation edge id"): row
        for row in _mapping_rows(
            corrected_registry.get("relation_edges"), "corrected relation_edges"
        )
    }
    for edge_id in receipt.get("retracted_relation_edge_ids") or []:
        edge = edges.get(edge_id)
        if (
            edge is None
            or edge.get("semantic_status") != "human_retracted"
            or edge.get("effective") is not False
        ):
            raise LiteraryRelationCorrectionError(
                "relation correction retracted edge differs"
            )
    for edge_id in receipt.get("replacement_relation_edge_ids") or []:
        edge = edges.get(edge_id)
        if (
            edge is None
            or edge.get("semantic_status") != "human_corrected"
            or edge.get("effective") is not True
        ):
            raise LiteraryRelationCorrectionError(
                "relation correction replacement edge differs"
            )


def _normalize_replacement(
    value: Any,
    *,
    block_ids: set[str],
    evidence_block_ids: set[str],
) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise LiteraryRelationCorrectionError(
            "replace correction requires a replacement object"
        )
    allowed = {
        "component_kind",
        "relation",
        "source_entity_id",
        "target_entity_id",
        "anchor_block_ids",
        "relation_note",
    }
    required = allowed - {"relation_note"}
    if set(value) - allowed or not required.issubset(value):
        raise LiteraryRelationCorrectionError(
            "relation replacement fields differ"
        )
    component_kind = _required_string(
        value.get("component_kind"), "replacement component_kind"
    )
    if component_kind not in COMPONENT_KINDS:
        raise LiteraryRelationCorrectionError(
            "replacement component_kind is unknown"
        )
    relation = _required_string(value.get("relation"), "replacement relation")
    allowed_relations = (
        KINSHIP_RELATIONS
        if component_kind == "kinship_link"
        else LINK_RELATIONS
    )
    if relation not in allowed_relations:
        raise LiteraryRelationCorrectionError(
            "replacement relation is outside the typed vocabulary"
        )
    source = _required_string(
        value.get("source_entity_id"), "replacement source_entity_id"
    )
    target = _required_string(
        value.get("target_entity_id"), "replacement target_entity_id"
    )
    if source == target:
        raise LiteraryRelationCorrectionError(
            "relation replacement is reflexive"
        )
    anchors = _block_list(
        value.get("anchor_block_ids"),
        block_ids=block_ids,
        label="replacement anchor_block_ids",
    )
    if not set(anchors) <= evidence_block_ids:
        raise LiteraryRelationCorrectionError(
            "replacement anchors exceed correction evidence"
        )
    note = value.get("relation_note")
    if note is not None:
        note = _required_string(note, "replacement relation_note")
    open_relation = (
        relation == "other_kin"
        if component_kind == "kinship_link"
        else relation == "other_link"
    )
    if open_relation and note is None:
        raise LiteraryRelationCorrectionError(
            "open relation replacement requires relation_note"
        )
    return {
        "component_kind": component_kind,
        "relation": relation,
        "source_entity_id": source,
        "target_entity_id": target,
        "anchor_block_ids": anchors,
        "relation_note": note,
    }


def _build_replacement_edge(
    *,
    correction: Mapping[str, Any],
    replacement: Mapping[str, Any],
    chapter_id: str,
    known_entities: set[str],
) -> dict[str, Any]:
    source = replacement["source_entity_id"]
    target = replacement["target_entity_id"]
    if source not in known_entities or target not in known_entities:
        raise LiteraryRelationCorrectionError(
            "replacement cites an entity outside the chapter registry"
        )
    relation = replacement["relation"]
    open_relation = relation in {"other_kin", "other_link"}
    candidate = _normalize_relation_candidate(
        component_kind=replacement["component_kind"],
        relation=relation,
        relation_note=replacement.get("relation_note"),
        relation_raw=(
            replacement.get("relation_note") if open_relation else None
        ),
        relation_status=("human_other" if open_relation else "in_vocabulary"),
        source_entity_id=source,
        target_entity_id=target,
        proposal={
            "anchor_block_ids": list(replacement["anchor_block_ids"]),
        },
        decision={
            "component_id": correction["correction_id"],
            "source_block_ids": list(correction["evidence_block_ids"]),
        },
        chapter_id=chapter_id,
    )
    edge = {
        "relation_family": candidate["relation_family"],
        "relation": candidate["relation"],
        "relation_variants": list(candidate["relation_variants"]),
        "source_entity_id": candidate["source_entity_id"],
        "target_entity_id": candidate["target_entity_id"],
        "chapter_id": chapter_id,
        "anchor_block_ids": list(candidate["anchor_block_ids"]),
        "semantic_status": "human_corrected",
        "effective": True,
        "source_component_ids": [correction["correction_id"]],
        "validity_scope": "as_of_chapter",
        "relation_correction_id": correction["correction_id"],
    }
    for key in ("relation_note", "relation_raw", "relation_status"):
        if key in candidate:
            edge[key] = deepcopy(candidate[key])
    edge["relation_edge_id"] = (
        "litrel1_" + canonical_hash(_edge_identity(edge))[:20]
    )
    return edge


def _replace_structural_issues(
    value: Any,
    structural_issues: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    rows = _mapping_rows(value or [], "registry issue rows")
    kept = [
        deepcopy(dict(row))
        for row in rows
        if row.get("reason_code") != "kinship_structurally_impossible"
    ]
    kept.extend(deepcopy(dict(row)) for row in structural_issues)
    return kept


def _chapter_identity(chapter: Mapping[str, Any]) -> tuple[str, set[str]]:
    chapter_id = _required_string(chapter.get("chapter_id"), "chapter_id")
    blocks = _mapping_rows(chapter.get("blocks"), "chapter blocks")
    if not blocks:
        raise LiteraryRelationCorrectionError("chapter blocks are empty")
    block_ids = {
        _required_string(row.get("block_id"), "chapter block_id")
        for row in blocks
    }
    if len(block_ids) != len(blocks):
        raise LiteraryRelationCorrectionError("chapter block IDs repeat")
    return chapter_id, block_ids


def _block_list(
    value: Any,
    *,
    block_ids: set[str],
    label: str,
) -> list[str]:
    if (
        not isinstance(value, list)
        or not value
        or any(not isinstance(row, str) or not row for row in value)
        or len(value) != len(set(value))
    ):
        raise LiteraryRelationCorrectionError(
            f"{label} must be a non-empty unique string list"
        )
    unknown = set(value) - block_ids
    if unknown:
        raise LiteraryRelationCorrectionError(
            f"{label} cites foreign blocks: {sorted(unknown)}"
        )
    return list(value)


def _mapping_rows(value: Any, label: str) -> list[Mapping[str, Any]]:
    if not isinstance(value, list) or any(
        not isinstance(row, Mapping) for row in value
    ):
        raise LiteraryRelationCorrectionError(
            f"{label} must be a list of objects"
        )
    return list(value)


def _required_string(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise LiteraryRelationCorrectionError(
            f"{label} must be a non-empty string"
        )
    return value.strip()


__all__ = [
    "LiteraryRelationCorrectionError",
    "OVERLAY_SCHEMA_VERSION",
    "RECEIPT_SCHEMA_VERSION",
    "apply_relation_correction_overlay_v1",
    "normalize_relation_correction_overlay_v1",
    "verify_relation_correction_bundle_v1",
    "verify_relation_correction_receipt_v1",
]
