"""Apply an explicit within-chapter identity split without rewriting history.

The caller names accepted same-referent components to retract.  This module
does not inspect prose or infer identity: it validates the named components,
repartitions the Local Auditor artifact, and verifies that a rebuilt registry
splits every affected source-ref set without losing observations.
"""

from __future__ import annotations

from copy import deepcopy
from typing import Any, Mapping, Sequence

from pipeline.literary.b1_chapter_registry_writer_v1 import (
    verify_b1_chapter_registry_v1,
)
from pipeline.literary.checkpoint import canonical_hash


OVERLAY_SCHEMA_VERSION = "literary_identity_split_correction_overlay_v1"
RECEIPT_SCHEMA_VERSION = "literary_identity_split_correction_receipt_v1"

_PARTITION_BY_ACTION = {
    "accept_proposal": "accepted_components",
    "revise_proposal": "revised_components",
    "reject_proposal": "rejected_components",
    "keep_pending": "pending_components",
    "refer_cross_chapter": "cross_chapter_referrals",
}


class LiteraryIdentitySplitCorrectionError(ValueError):
    pass


def apply_identity_split_to_local_audit_v1(
    *,
    source_registry: Mapping[str, Any],
    source_local_audit: Mapping[str, Any],
    overlay: Mapping[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Return a corrected Local Auditor artifact and normalized overlay."""

    verify_b1_chapter_registry_v1(dict(source_registry))
    _verify_artifact_hash(source_local_audit, "source Local Auditor")
    normalized = normalize_identity_split_overlay_v1(
        overlay=overlay,
        source_registry=source_registry,
        source_local_audit=source_local_audit,
    )
    corrected = deepcopy(dict(source_local_audit))
    decisions = [
        deepcopy(dict(row))
        for row in _mapping_rows(corrected.get("decisions"), "audit decisions")
    ]
    corrections_by_component = {
        row["retract_component_id"]: row
        for row in normalized["corrections"]
    }
    seen: set[str] = set()
    for decision in decisions:
        component_id = _required_string(
            decision.get("component_id"), "decision component_id"
        )
        correction = corrections_by_component.get(component_id)
        if correction is None:
            continue
        seen.add(component_id)
        decision["action"] = "reject_proposal"
        decision["resolution_note"] = correction["correction_note"]
        decision["source_block_ids"] = list(
            correction["evidence_block_ids"]
        )
        decision["revised_relation"] = None
        decision["revised_relation_note"] = None
        decision["revised_target_ref"] = None
    missing = set(corrections_by_component) - seen
    if missing:
        raise LiteraryIdentitySplitCorrectionError(
            "identity split correction component disappeared"
        )

    corrected["decisions"] = decisions
    for action, key in _PARTITION_BY_ACTION.items():
        corrected[key] = [
            deepcopy(row) for row in decisions if row["action"] == action
        ]
    metrics = deepcopy(dict(corrected.get("metrics") or {}))
    metrics["accepted_count"] = sum(
        row["action"] == "accept_proposal" for row in decisions
    )
    metrics["revised_count"] = sum(
        row["action"] == "revise_proposal" for row in decisions
    )
    metrics["rejected_count"] = sum(
        row["action"] == "reject_proposal" for row in decisions
    )
    metrics["pending_count"] = sum(
        row["action"] == "keep_pending" for row in decisions
    )
    metrics["cross_chapter_referral_count"] = sum(
        row["action"] == "refer_cross_chapter" for row in decisions
    )
    corrected["metrics"] = metrics
    corrected.pop("artifact_hash", None)
    corrected["artifact_hash"] = canonical_hash(corrected)
    _verify_artifact_hash(corrected, "corrected Local Auditor")
    return corrected, normalized


def normalize_identity_split_overlay_v1(
    *,
    overlay: Mapping[str, Any],
    source_registry: Mapping[str, Any],
    source_local_audit: Mapping[str, Any],
) -> dict[str, Any]:
    expected = {
        "schema_version",
        "chapter_id",
        "source_registry_hash",
        "source_local_audit_artifact_hash",
        "corrections",
    }
    if set(overlay) != expected:
        raise LiteraryIdentitySplitCorrectionError(
            "identity split overlay fields differ"
        )
    if overlay.get("schema_version") != OVERLAY_SCHEMA_VERSION:
        raise LiteraryIdentitySplitCorrectionError(
            "foreign identity split overlay schema"
        )
    chapter_id = _required_string(overlay.get("chapter_id"), "chapter_id")
    if chapter_id != source_registry.get("chapter_id"):
        raise LiteraryIdentitySplitCorrectionError(
            "identity split correction chapter differs"
        )
    if overlay.get("source_registry_hash") != source_registry.get(
        "registry_hash"
    ):
        raise LiteraryIdentitySplitCorrectionError(
            "identity split source registry hash differs"
        )
    if overlay.get(
        "source_local_audit_artifact_hash"
    ) != source_local_audit.get("artifact_hash"):
        raise LiteraryIdentitySplitCorrectionError(
            "identity split source Local Auditor hash differs"
        )

    cards = {
        _required_string(row.get("entity_id"), "registry entity_id"): row
        for row in _mapping_rows(source_registry.get("cards"), "registry cards")
    }
    decisions = {
        _required_string(row.get("component_id"), "decision component_id"): row
        for row in _mapping_rows(
            source_local_audit.get("decisions"), "audit decisions"
        )
    }
    rows: list[dict[str, Any]] = []
    component_ids: set[str] = set()
    target_ids: set[str] = set()
    for raw in _mapping_rows(overlay.get("corrections"), "corrections"):
        if set(raw) != {
            "target_entity_id",
            "retract_component_id",
            "evidence_block_ids",
            "correction_note",
        }:
            raise LiteraryIdentitySplitCorrectionError(
                "identity split correction row fields differ"
            )
        target_id = _required_string(
            raw.get("target_entity_id"), "target_entity_id"
        )
        component_id = _required_string(
            raw.get("retract_component_id"), "retract_component_id"
        )
        if target_id in target_ids or component_id in component_ids:
            raise LiteraryIdentitySplitCorrectionError(
                "identity split correction target repeats"
            )
        target_ids.add(target_id)
        component_ids.add(component_id)
        card = cards.get(target_id)
        if card is None:
            raise LiteraryIdentitySplitCorrectionError(
                "identity split correction targets an unknown entity"
            )
        merge = card.get("within_chapter_identity_merge")
        if not isinstance(merge, Mapping) or component_id not in set(
            merge.get("source_component_ids") or []
        ):
            raise LiteraryIdentitySplitCorrectionError(
                "identity split component is not part of the target merge"
            )
        decision = decisions.get(component_id)
        if (
            decision is None
            or decision.get("component_kind") != "same_referent_proposal"
            or decision.get("action") != "accept_proposal"
        ):
            raise LiteraryIdentitySplitCorrectionError(
                "identity split target is not an accepted same-referent decision"
            )
        evidence = _string_list(
            raw.get("evidence_block_ids"), "evidence_block_ids"
        )
        if not evidence or not set(evidence).issubset(
            set(decision.get("source_block_ids") or [])
        ):
            raise LiteraryIdentitySplitCorrectionError(
                "identity split evidence exceeds the accepted decision"
            )
        proposal = decision.get("original_proposal")
        if not isinstance(proposal, Mapping):
            raise LiteraryIdentitySplitCorrectionError(
                "identity split decision has no original proposal"
            )
        member_refs = set(merge.get("member_source_refs") or [])
        subject_ref = _required_string(
            decision.get("subject_ref"), "decision subject_ref"
        )
        target_ref = _required_string(
            proposal.get("target_ref"), "proposal target_ref"
        )
        if {subject_ref, target_ref} - member_refs:
            raise LiteraryIdentitySplitCorrectionError(
                "identity split decision endpoints exceed the target merge"
            )
        row_body = {
            "target_entity_id": target_id,
            "retract_component_id": component_id,
            "evidence_block_ids": evidence,
            "correction_note": _required_string(
                raw.get("correction_note"), "correction_note"
            ),
        }
        rows.append(
            {
                **row_body,
                "correction_id": (
                    "litidcorr1_" + canonical_hash(row_body)[:20]
                ),
            }
        )
    if not rows:
        raise LiteraryIdentitySplitCorrectionError(
            "identity split correction overlay is empty"
        )
    rows.sort(key=lambda row: row["correction_id"])
    body = {
        "schema_version": OVERLAY_SCHEMA_VERSION,
        "chapter_id": chapter_id,
        "source_registry_hash": source_registry["registry_hash"],
        "source_local_audit_artifact_hash": source_local_audit[
            "artifact_hash"
        ],
        "corrections": rows,
        "human_semantic_correction_performed": True,
    }
    return {**body, "overlay_hash": canonical_hash(body)}


def attach_identity_split_lineage_v1(
    *,
    corrected_registry: Mapping[str, Any],
    normalized_overlay: Mapping[str, Any],
) -> dict[str, Any]:
    corrected = deepcopy(dict(corrected_registry))
    corrected.pop("registry_hash", None)
    lineage = deepcopy(dict(corrected.get("lineage") or {}))
    hashes = list(lineage.get("identity_split_correction_overlay_hashes") or [])
    overlay_hash = normalized_overlay["overlay_hash"]
    if overlay_hash in hashes:
        raise LiteraryIdentitySplitCorrectionError(
            "identity split overlay was already applied"
        )
    lineage["identity_split_correction_overlay_hashes"] = [
        *hashes,
        overlay_hash,
    ]
    corrected["lineage"] = lineage
    curation = deepcopy(dict(corrected.get("curation_log") or {}))
    curation["identity_split_correction_ids"] = [
        *list(curation.get("identity_split_correction_ids") or []),
        *[
            row["correction_id"]
            for row in normalized_overlay["corrections"]
        ],
    ]
    corrected["curation_log"] = curation
    corrected["human_semantic_correction_performed"] = True
    metrics = deepcopy(dict(corrected.get("metrics") or {}))
    metrics["identity_split_correction_count"] = int(
        metrics.get("identity_split_correction_count") or 0
    ) + len(normalized_overlay["corrections"])
    corrected["metrics"] = metrics
    corrected["registry_hash"] = canonical_hash(corrected)
    verify_b1_chapter_registry_v1(corrected)
    return corrected


def build_identity_split_receipt_v1(
    *,
    source_registry: Mapping[str, Any],
    corrected_registry: Mapping[str, Any],
    source_local_audit: Mapping[str, Any],
    corrected_local_audit: Mapping[str, Any],
    normalized_overlay: Mapping[str, Any],
    queue_hash: str,
) -> dict[str, Any]:
    mappings = _split_mappings(
        source_registry=source_registry,
        corrected_registry=corrected_registry,
        normalized_overlay=normalized_overlay,
    )
    body = {
        "schema_version": RECEIPT_SCHEMA_VERSION,
        "chapter_id": source_registry["chapter_id"],
        "source_registry_hash": source_registry["registry_hash"],
        "corrected_registry_hash": corrected_registry["registry_hash"],
        "source_local_audit_artifact_hash": source_local_audit["artifact_hash"],
        "corrected_local_audit_artifact_hash": corrected_local_audit[
            "artifact_hash"
        ],
        "overlay_hash": normalized_overlay["overlay_hash"],
        "correction_ids": [
            row["correction_id"]
            for row in normalized_overlay["corrections"]
        ],
        "retracted_component_ids": [
            row["retract_component_id"]
            for row in normalized_overlay["corrections"]
        ],
        "split_mappings": mappings,
        "queue_hash": queue_hash,
        "human_semantic_correction_performed": True,
        "provider_calls": 0,
        "production_publish_performed": False,
    }
    return {**body, "receipt_hash": canonical_hash(body)}


def verify_identity_split_bundle_v1(
    *,
    source_registry: Mapping[str, Any],
    corrected_registry: Mapping[str, Any],
    corrected_local_audit: Mapping[str, Any],
    prior_cards: Mapping[str, Any],
    queue: Mapping[str, Any],
    writer_report: Mapping[str, Any],
    normalized_overlay: Mapping[str, Any],
    receipt: Mapping[str, Any],
) -> None:
    verify_b1_chapter_registry_v1(dict(source_registry))
    verify_b1_chapter_registry_v1(dict(corrected_registry))
    _verify_artifact_hash(corrected_local_audit, "corrected Local Auditor")
    _verify_hashed_object(
        normalized_overlay, "overlay_hash", "identity split overlay"
    )
    _verify_hashed_object(receipt, "receipt_hash", "identity split receipt")
    _verify_hashed_object(queue, "queue_hash", "corrected hearing queue")
    _verify_hashed_object(
        writer_report, "report_hash", "corrected writer report"
    )
    if receipt.get("provider_calls") != 0:
        raise LiteraryIdentitySplitCorrectionError(
            "identity split receipt claims a provider call"
        )
    if (
        receipt.get("source_registry_hash") != source_registry.get("registry_hash")
        or receipt.get("corrected_registry_hash")
        != corrected_registry.get("registry_hash")
        or receipt.get("corrected_local_audit_artifact_hash")
        != corrected_local_audit.get("artifact_hash")
        or receipt.get("overlay_hash") != normalized_overlay.get("overlay_hash")
    ):
        raise LiteraryIdentitySplitCorrectionError(
            "identity split bundle lineage differs"
        )
    if (
        queue.get("registry_hash") != corrected_registry.get("registry_hash")
        or queue.get("local_audit_artifact_hash")
        != corrected_local_audit.get("artifact_hash")
        or receipt.get("queue_hash") != queue.get("queue_hash")
        or writer_report.get("registry_hash")
        != corrected_registry.get("registry_hash")
    ):
        raise LiteraryIdentitySplitCorrectionError(
            "identity split queue/report lineage differs"
        )
    if dict(prior_cards) != corrected_registry.get("prior_cards_projection"):
        raise LiteraryIdentitySplitCorrectionError(
            "identity split prior-card projection differs"
        )
    expected_mappings = _split_mappings(
        source_registry=source_registry,
        corrected_registry=corrected_registry,
        normalized_overlay=normalized_overlay,
    )
    if receipt.get("split_mappings") != expected_mappings:
        raise LiteraryIdentitySplitCorrectionError(
            "identity split mapping receipt differs"
        )
    if _all_source_refs(source_registry) != _all_source_refs(corrected_registry):
        raise LiteraryIdentitySplitCorrectionError(
            "identity split correction lost or duplicated source refs"
        )


def _split_mappings(
    *,
    source_registry: Mapping[str, Any],
    corrected_registry: Mapping[str, Any],
    normalized_overlay: Mapping[str, Any],
) -> list[dict[str, Any]]:
    source_cards = {
        row["entity_id"]: row
        for row in _mapping_rows(source_registry.get("cards"), "source cards")
    }
    corrected_cards = _mapping_rows(
        corrected_registry.get("cards"), "corrected cards"
    )
    rows: list[dict[str, Any]] = []
    for correction in normalized_overlay["corrections"]:
        target_id = correction["target_entity_id"]
        source_card = source_cards[target_id]
        merge = source_card.get("within_chapter_identity_merge")
        if not isinstance(merge, Mapping):
            raise LiteraryIdentitySplitCorrectionError(
                "identity split source card has no merge metadata"
            )
        member_refs = set(merge.get("member_source_refs") or [])
        partitions: list[dict[str, Any]] = []
        covered: list[str] = []
        for card in corrected_cards:
            refs = sorted(member_refs & set(card.get("source_refs") or []))
            if not refs:
                continue
            covered.extend(refs)
            partitions.append(
                {
                    "entity_id": card["entity_id"],
                    "canonical_surface": card["canonical_surface"],
                    "member_source_refs": refs,
                    "retains_source_entity_id": card["entity_id"] == target_id,
                }
            )
            corrected_merge = card.get("within_chapter_identity_merge")
            if isinstance(corrected_merge, Mapping) and correction[
                "retract_component_id"
            ] in set(corrected_merge.get("source_component_ids") or []):
                raise LiteraryIdentitySplitCorrectionError(
                    "retracted identity component survived the correction"
                )
        partitions.sort(key=lambda row: row["entity_id"])
        if (
            len(partitions) < 2
            or sorted(covered) != sorted(member_refs)
            or sum(row["retains_source_entity_id"] for row in partitions) != 1
        ):
            raise LiteraryIdentitySplitCorrectionError(
                "identity correction did not produce an exact split"
            )
        rows.append(
            {
                "target_entity_id": target_id,
                "retracted_component_id": correction["retract_component_id"],
                "partitions": partitions,
            }
        )
    rows.sort(key=lambda row: row["target_entity_id"])
    return rows


def _all_source_refs(registry: Mapping[str, Any]) -> list[str]:
    refs = [
        str(ref)
        for card in _mapping_rows(registry.get("cards"), "registry cards")
        for ref in card.get("source_refs") or []
    ]
    if len(refs) != len(set(refs)):
        raise LiteraryIdentitySplitCorrectionError(
            "registry source refs are duplicated"
        )
    return sorted(refs)


def _verify_artifact_hash(value: Mapping[str, Any], label: str) -> None:
    _verify_hashed_object(value, "artifact_hash", label)


def _verify_hashed_object(
    value: Mapping[str, Any], hash_key: str, label: str
) -> None:
    body = dict(value)
    observed = body.pop(hash_key, None)
    if not isinstance(observed, str) or canonical_hash(body) != observed:
        raise LiteraryIdentitySplitCorrectionError(f"{label} hash differs")


def _mapping_rows(value: Any, label: str) -> list[Mapping[str, Any]]:
    if not isinstance(value, list) or not all(
        isinstance(row, Mapping) for row in value
    ):
        raise LiteraryIdentitySplitCorrectionError(
            f"{label} must be a list of objects"
        )
    return list(value)


def _required_string(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise LiteraryIdentitySplitCorrectionError(f"{label} must be non-empty")
    return value


def _string_list(value: Any, label: str) -> list[str]:
    if not isinstance(value, list) or not all(
        isinstance(row, str) and row for row in value
    ):
        raise LiteraryIdentitySplitCorrectionError(
            f"{label} must be a list of strings"
        )
    if len(value) != len(set(value)):
        raise LiteraryIdentitySplitCorrectionError(f"{label} repeats values")
    return list(value)


__all__ = [
    "LiteraryIdentitySplitCorrectionError",
    "apply_identity_split_to_local_audit_v1",
    "attach_identity_split_lineage_v1",
    "build_identity_split_receipt_v1",
    "normalize_identity_split_overlay_v1",
    "verify_identity_split_bundle_v1",
]
