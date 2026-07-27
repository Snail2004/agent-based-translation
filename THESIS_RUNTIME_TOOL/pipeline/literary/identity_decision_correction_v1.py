"""Apply an explicit human correction to one sealed identity decision.

This module is offline and mechanical. It does not infer identity from prose:
the caller names the ledger entry, expected old verdict, replacement verdict,
reason, and exact source quotations. The source ledger remains immutable.
"""

from __future__ import annotations

from copy import deepcopy
from typing import Any, Mapping, Sequence

from pipeline.literary.b1_cross_chapter_decision_ledger_v1 import (
    DISTINCT_VERDICTS,
    ENTRY_ID_PREFIX,
    MERGE_VERDICTS,
    PENDING_VERDICTS,
    _sealed_ledger,
    build_projected_prior_cards_v1,
    project_reconciled_b1_registry_v1,
    verify_decision_ledger_v1,
)
from pipeline.literary.checkpoint import canonical_hash


OVERLAY_SCHEMA_VERSION = "literary_identity_decision_correction_overlay_v1"
RECEIPT_SCHEMA_VERSION = "literary_identity_decision_correction_receipt_v1"
CORRECTABLE_REPLACEMENT_VERDICTS = DISTINCT_VERDICTS | PENDING_VERDICTS


class LiteraryIdentityDecisionCorrectionError(ValueError):
    pass


def apply_identity_decision_correction_v1(
    *,
    decision_ledger: Mapping[str, Any],
    source_document: Mapping[str, Any],
    overlay: Mapping[str, Any],
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    """Return a corrected ledger, normalized overlay, and sealed receipt."""

    source_ledger = deepcopy(dict(decision_ledger))
    verify_decision_ledger_v1(source_ledger)
    normalized = normalize_identity_decision_correction_overlay_v1(
        overlay=overlay,
        source_ledger=source_ledger,
        source_document=source_document,
    )
    target_entry_id = normalized["target_entry_id"]
    target_index = next(
        (
            index
            for index, row in enumerate(source_ledger["entries"])
            if row.get("entry_id") == target_entry_id
        ),
        None,
    )
    if target_index is None:
        raise LiteraryIdentityDecisionCorrectionError(
            "identity correction targets an unknown ledger entry"
        )
    old_entry = deepcopy(source_ledger["entries"][target_index])
    replacement = normalized["replacement"]
    new_entry = deepcopy(old_entry)
    new_entry["verdict"] = replacement["verdict"]
    new_entry["merge_target_prior_card_id"] = None
    new_entry["excluded_prior_card_ids"] = deepcopy(
        replacement["excluded_prior_card_ids"]
    )
    new_entry["evidence"] = deepcopy(replacement["evidence"])
    new_entry["reason"] = replacement["reason"]
    new_entry["resolution_condition"] = replacement["resolution_condition"]
    body = {
        key: value
        for key, value in new_entry.items()
        if key not in {"entry_id", "sequence_index"}
    }
    new_entry["entry_id"] = ENTRY_ID_PREFIX + canonical_hash(body)[:20]

    entries = [deepcopy(row) for row in source_ledger["entries"]]
    entries[target_index] = new_entry
    corrected = _sealed_ledger(
        book_id=source_ledger["book_id"],
        entries=entries,
    )
    verify_decision_ledger_v1(corrected)

    receipt_body = {
        "schema_version": RECEIPT_SCHEMA_VERSION,
        "book_id": source_ledger["book_id"],
        "source_ledger_hash": source_ledger["ledger_hash"],
        "corrected_ledger_hash": corrected["ledger_hash"],
        "overlay_hash": normalized["overlay_hash"],
        "correction_id": normalized["correction_id"],
        "target_component_id": normalized["target_component_id"],
        "old_entry_id": old_entry["entry_id"],
        "new_entry_id": new_entry["entry_id"],
        "old_verdict": old_entry["verdict"],
        "new_verdict": new_entry["verdict"],
        "evidence_block_ids": sorted(
            {row["block_id"] for row in new_entry["evidence"]}
        ),
        "human_semantic_correction_performed": True,
        "provider_calls": 0,
        "production_publish_performed": False,
    }
    receipt = {
        **receipt_body,
        "receipt_hash": canonical_hash(receipt_body),
    }
    return corrected, normalized, receipt


def normalize_identity_decision_correction_overlay_v1(
    *,
    overlay: Mapping[str, Any],
    source_ledger: Mapping[str, Any],
    source_document: Mapping[str, Any],
) -> dict[str, Any]:
    if not isinstance(overlay, Mapping):
        raise LiteraryIdentityDecisionCorrectionError(
            "identity decision correction overlay must be an object"
        )
    if overlay.get("schema_version") != OVERLAY_SCHEMA_VERSION:
        raise LiteraryIdentityDecisionCorrectionError(
            "identity decision correction overlay schema is unsupported"
        )
    if overlay.get("source_ledger_hash") != source_ledger.get("ledger_hash"):
        raise LiteraryIdentityDecisionCorrectionError(
            "identity correction source ledger hash differs"
        )
    correction_id = _required_string(overlay.get("correction_id"), "correction_id")
    target_entry_id = _required_string(
        overlay.get("target_entry_id"), "target_entry_id"
    )
    target_component_id = _required_string(
        overlay.get("target_component_id"), "target_component_id"
    )
    target = next(
        (
            row
            for row in source_ledger.get("entries") or []
            if isinstance(row, Mapping) and row.get("entry_id") == target_entry_id
        ),
        None,
    )
    if target is None:
        raise LiteraryIdentityDecisionCorrectionError(
            "identity correction targets an unknown ledger entry"
        )
    if target.get("component_id") != target_component_id:
        raise LiteraryIdentityDecisionCorrectionError(
            "identity correction target component differs"
        )
    expected_verdict = _required_string(
        overlay.get("expected_verdict"), "expected_verdict"
    )
    if expected_verdict not in MERGE_VERDICTS:
        raise LiteraryIdentityDecisionCorrectionError(
            "identity correction may only retract a merge verdict"
        )
    if target.get("verdict") != expected_verdict:
        raise LiteraryIdentityDecisionCorrectionError(
            "identity correction expected verdict differs"
        )
    expected_merge_target = _required_string(
        overlay.get("expected_merge_target_prior_card_id"),
        "expected_merge_target_prior_card_id",
    )
    if target.get("merge_target_prior_card_id") != expected_merge_target:
        raise LiteraryIdentityDecisionCorrectionError(
            "identity correction expected merge target differs"
        )
    replacement = _normalize_replacement(
        overlay.get("replacement"),
        target=target,
        source_document=source_document,
    )
    body = {
        "schema_version": OVERLAY_SCHEMA_VERSION,
        "source_ledger_hash": source_ledger["ledger_hash"],
        "correction_id": correction_id,
        "target_entry_id": target_entry_id,
        "target_component_id": target_component_id,
        "expected_verdict": expected_verdict,
        "expected_merge_target_prior_card_id": expected_merge_target,
        "replacement": replacement,
        "human_semantic_correction_performed": True,
    }
    return {**body, "overlay_hash": canonical_hash(body)}


def verify_identity_decision_correction_receipt_v1(
    receipt: Mapping[str, Any],
) -> dict[str, Any]:
    if not isinstance(receipt, Mapping):
        raise LiteraryIdentityDecisionCorrectionError(
            "identity decision correction receipt must be an object"
        )
    if receipt.get("schema_version") != RECEIPT_SCHEMA_VERSION:
        raise LiteraryIdentityDecisionCorrectionError(
            "identity decision correction receipt schema is unsupported"
        )
    body = dict(receipt)
    observed_hash = body.pop("receipt_hash", None)
    if canonical_hash(body) != observed_hash:
        raise LiteraryIdentityDecisionCorrectionError(
            "identity decision correction receipt seal is invalid"
        )
    if (
        receipt.get("provider_calls") != 0
        or receipt.get("production_publish_performed") is not False
        or receipt.get("human_semantic_correction_performed") is not True
    ):
        raise LiteraryIdentityDecisionCorrectionError(
            "identity decision correction receipt authority flags are invalid"
        )
    return deepcopy(dict(receipt))


def verify_identity_decision_correction_bundle_v1(
    *,
    source_ledger: Mapping[str, Any],
    source_document: Mapping[str, Any],
    registries: Sequence[Mapping[str, Any]],
    corrected_ledger: Mapping[str, Any],
    reconciled_projection: Mapping[str, Any],
    prior_cards: Sequence[Mapping[str, Any]],
    normalized_overlay: Mapping[str, Any],
    receipt: Mapping[str, Any],
) -> None:
    verify_decision_ledger_v1(source_ledger)
    verify_decision_ledger_v1(corrected_ledger)
    verify_identity_decision_correction_receipt_v1(receipt)
    rebuilt_ledger, rebuilt_overlay, rebuilt_receipt = (
        apply_identity_decision_correction_v1(
            decision_ledger=source_ledger,
            source_document=source_document,
            overlay=normalized_overlay,
        )
    )
    if rebuilt_overlay != normalized_overlay:
        raise LiteraryIdentityDecisionCorrectionError(
            "identity decision correction overlay differs on replay"
        )
    if rebuilt_ledger != corrected_ledger:
        raise LiteraryIdentityDecisionCorrectionError(
            "corrected decision ledger differs on replay"
        )
    if rebuilt_receipt != receipt:
        raise LiteraryIdentityDecisionCorrectionError(
            "identity decision correction receipt differs on replay"
        )
    rebuilt_projection = project_reconciled_b1_registry_v1(
        registries=registries,
        ledger=corrected_ledger,
    )
    if rebuilt_projection != reconciled_projection:
        raise LiteraryIdentityDecisionCorrectionError(
            "reconciled identity projection differs on replay"
        )
    rebuilt_prior_cards = build_projected_prior_cards_v1(
        registries=registries,
        projection=rebuilt_projection,
    )
    if rebuilt_prior_cards != list(prior_cards):
        raise LiteraryIdentityDecisionCorrectionError(
            "projected prior cards differ on replay"
        )


def _normalize_replacement(
    value: Any,
    *,
    target: Mapping[str, Any],
    source_document: Mapping[str, Any],
) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise LiteraryIdentityDecisionCorrectionError(
            "identity correction replacement must be an object"
        )
    verdict = _required_string(value.get("verdict"), "replacement verdict")
    if verdict not in CORRECTABLE_REPLACEMENT_VERDICTS:
        raise LiteraryIdentityDecisionCorrectionError(
            "identity correction replacement verdict is unsupported"
        )
    if value.get("merge_target_prior_card_id") is not None:
        raise LiteraryIdentityDecisionCorrectionError(
            "identity correction replacement cannot retain a merge target"
        )
    evidence = _normalize_evidence(
        value.get("evidence"),
        source_document=source_document,
    )
    if not evidence:
        raise LiteraryIdentityDecisionCorrectionError(
            "identity correction replacement must cite evidence"
        )
    reason = _required_string(value.get("reason"), "replacement reason")
    resolution_condition = value.get("resolution_condition")
    if verdict in PENDING_VERDICTS:
        resolution_condition = _required_string(
            resolution_condition,
            "replacement resolution_condition",
        )
    elif resolution_condition is not None:
        raise LiteraryIdentityDecisionCorrectionError(
            "decisive identity correction cannot retain a resolution condition"
        )
    excluded = _string_list(
        value.get("excluded_prior_card_ids") or [],
        "replacement excluded_prior_card_ids",
    )
    candidates = set(
        target.get("prior_card_ids")
        or ([target["prior_card_id"]] if target.get("prior_card_id") else [])
    )
    if verdict not in PENDING_VERDICTS and excluded:
        raise LiteraryIdentityDecisionCorrectionError(
            "only a pending correction may exclude prior candidates"
        )
    if not set(excluded) <= candidates:
        raise LiteraryIdentityDecisionCorrectionError(
            "identity correction excludes a foreign prior candidate"
        )
    supported = {
        candidate_id
        for row in evidence
        for candidate_id in row.get("supports_excluded_prior_card_ids") or []
    }
    if supported != set(excluded):
        raise LiteraryIdentityDecisionCorrectionError(
            "identity correction exclusion evidence coverage differs"
        )
    return {
        "verdict": verdict,
        "merge_target_prior_card_id": None,
        "excluded_prior_card_ids": excluded,
        "evidence": evidence,
        "reason": reason,
        "resolution_condition": resolution_condition,
    }


def _normalize_evidence(
    value: Any,
    *,
    source_document: Mapping[str, Any],
) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        raise LiteraryIdentityDecisionCorrectionError(
            "identity correction evidence must be a list"
        )
    block_text = _source_block_text(source_document)
    rows: list[dict[str, Any]] = []
    for raw in value:
        if not isinstance(raw, Mapping):
            raise LiteraryIdentityDecisionCorrectionError(
                "identity correction evidence row must be an object"
            )
        block_id = _required_string(raw.get("block_id"), "evidence block_id")
        quote = _required_string(raw.get("quote"), "evidence quote")
        texts = block_text.get(block_id)
        if texts is None:
            raise LiteraryIdentityDecisionCorrectionError(
                f"identity correction cites a foreign source block: {block_id}"
            )
        if not any(quote in text for text in texts):
            raise LiteraryIdentityDecisionCorrectionError(
                f"identity correction quote is not verbatim for block: {block_id}"
            )
        supports = _string_list(
            raw.get("supports_excluded_prior_card_ids") or [],
            "supports_excluded_prior_card_ids",
        )
        rows.append(
            {
                "block_id": block_id,
                "quote": quote,
                "supports_excluded_prior_card_ids": supports,
            }
        )
    return rows


def _source_block_text(
    source_document: Mapping[str, Any],
) -> dict[str, tuple[str, ...]]:
    chapters = source_document.get("chapters")
    if not isinstance(chapters, list) or not chapters:
        raise LiteraryIdentityDecisionCorrectionError(
            "identity correction source document has no chapters"
        )
    result: dict[str, tuple[str, ...]] = {}
    for chapter in chapters:
        if not isinstance(chapter, Mapping):
            raise LiteraryIdentityDecisionCorrectionError(
                "identity correction source chapter is malformed"
            )
        for block in chapter.get("blocks") or []:
            if not isinstance(block, Mapping):
                raise LiteraryIdentityDecisionCorrectionError(
                    "identity correction source block is malformed"
                )
            block_id = _required_string(block.get("block_id"), "source block_id")
            if block_id in result:
                raise LiteraryIdentityDecisionCorrectionError(
                    "identity correction source document repeats a block id"
                )
            texts = tuple(
                text
                for text in (block.get("clean_text"), block.get("source_text"))
                if isinstance(text, str)
            )
            if not texts:
                raise LiteraryIdentityDecisionCorrectionError(
                    f"identity correction source block has no text: {block_id}"
                )
            result[block_id] = texts
    return result


def _string_list(value: Any, label: str) -> list[str]:
    if not isinstance(value, list):
        raise LiteraryIdentityDecisionCorrectionError(f"{label} must be a list")
    rows = [_required_string(row, f"{label} item") for row in value]
    if len(rows) != len(set(rows)):
        raise LiteraryIdentityDecisionCorrectionError(f"{label} repeats a value")
    return sorted(rows)


def _required_string(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise LiteraryIdentityDecisionCorrectionError(
            f"{label} must be a non-empty string"
        )
    return value


__all__ = [
    "LiteraryIdentityDecisionCorrectionError",
    "apply_identity_decision_correction_v1",
    "normalize_identity_decision_correction_overlay_v1",
    "verify_identity_decision_correction_bundle_v1",
    "verify_identity_decision_correction_receipt_v1",
]
