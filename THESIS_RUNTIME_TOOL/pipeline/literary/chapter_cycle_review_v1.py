"""Append-only routing ledger for unresolved chapter-prefix review evidence."""

from __future__ import annotations

from copy import deepcopy
from typing import Any, Mapping, Sequence

from pipeline.literary.book_entity_claim_auditor_v1 import (
    classify_pending_claim_reopen_v1,
    verify_prior_claim_revision_ledger_v1,
)
from pipeline.literary.checkpoint import canonical_hash


REVIEW_LEDGER_SCHEMA_VERSION = "chapter_cycle_review_ledger_v1"
REVIEW_LEDGER_VALIDATOR_VERSION = "chapter_cycle_review_validator_v1"
QUEUE_SCHEMA_VERSION = "two_chapter_candidate_review_queue_v1"
REVIEW_ROUTES = frozenset(
    {"stable_claim_rehearing", "book_identity_auditor", "continuity_evidence"}
)
REVIEW_STATES = frozenset(
    {
        "queued",
        "evidence_only",
        "duplicate_suppressed",
        "book_end_pending",
        "closed",
    }
)


def _review_item_identity_body(row: Mapping[str, Any]) -> dict[str, Any]:
    return {
        key: _clone(value)
        for key, value in row.items()
        if key
        in {
            "state_lineage_id",
            "chapter_id",
            "source_kind",
            "route",
            "subject_prior_card_ids",
            "disputed_field",
            "source_block_ids",
            "evidence_manifest_hash",
            "source_artifact_hash",
        }
    }


class ChapterCycleReviewError(RuntimeError):
    pass


def _clone(value: Any) -> Any:
    return deepcopy(value)


def _required_string(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ChapterCycleReviewError(f"{label} must be a non-empty string")
    return value


def _hash_string(value: Any, label: str) -> str:
    result = _required_string(value, label)
    if len(result) != 64 or any(char not in "0123456789abcdef" for char in result):
        raise ChapterCycleReviewError(f"{label} must be a lowercase sha256")
    return result


def _string_list(value: Any, label: str, *, allow_empty: bool = False) -> list[str]:
    if not isinstance(value, list) or (not value and not allow_empty):
        qualifier = "possibly empty" if allow_empty else "non-empty"
        raise ChapterCycleReviewError(f"{label} must be a {qualifier} list")
    rows = [_required_string(row, label) for row in value]
    if len(rows) != len(set(rows)):
        raise ChapterCycleReviewError(f"{label} contains duplicates")
    return rows


def _verify_queue(queue: Mapping[str, Any]) -> dict[str, Any]:
    if queue.get("schema_version") != QUEUE_SCHEMA_VERSION:
        raise ChapterCycleReviewError("foreign candidate review queue schema")
    body = dict(queue)
    observed = _hash_string(body.pop("queue_hash", None), "queue_hash")
    if canonical_hash(body) != observed:
        raise ChapterCycleReviewError("candidate review queue hash mismatch")
    if queue.get("production_publish_performed") is not False:
        raise ChapterCycleReviewError("candidate review queue claims publication")
    return _clone(dict(queue))


def _review_item(
    *,
    state_lineage_id: str,
    chapter_id: str,
    source_kind: str,
    route: str,
    subject_prior_card_ids: Sequence[str],
    disputed_field: str | None,
    source_block_ids: Sequence[str],
    evidence_manifest_hash: str,
    lifecycle_state: str,
    reason_code: str,
    source_artifact_hash: str,
    reopen_classification: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    if route not in REVIEW_ROUTES or lifecycle_state not in REVIEW_STATES:
        raise ChapterCycleReviewError("review item has a foreign route or state")
    card_ids = sorted(set(subject_prior_card_ids))
    if not card_ids:
        raise ChapterCycleReviewError("review item has no prior-card subject")
    block_ids = sorted(set(source_block_ids))
    body = {
        "state_lineage_id": state_lineage_id,
        "chapter_id": chapter_id,
        "source_kind": source_kind,
        "route": route,
        "subject_prior_card_ids": card_ids,
        "disputed_field": disputed_field,
        "source_block_ids": block_ids,
        "evidence_manifest_hash": evidence_manifest_hash,
        "lifecycle_state": lifecycle_state,
        "authority_effect": "none",
        "reason_code": reason_code,
        "source_artifact_hash": source_artifact_hash,
        "reopen_classification": (
            _clone(dict(reopen_classification))
            if reopen_classification is not None
            else None
        ),
    }
    return {
        "review_item_id": "cycrev1_"
        + canonical_hash(_review_item_identity_body(body))[:20],
        **body,
    }


def _queue_items(
    *,
    queue: Mapping[str, Any],
    state_lineage_id: str,
    chapter_id: str,
) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    queue_hash = _hash_string(queue.get("queue_hash"), "queue_hash")
    for row in queue.get("candidate_claim_evidence_queue") or []:
        if not isinstance(row, Mapping):
            raise ChapterCycleReviewError("candidate claim evidence must be an object")
        pending = row.get("pending_claim_snapshot")
        if not isinstance(pending, Mapping):
            raise ChapterCycleReviewError("candidate claim evidence lacks pending state")
        evidence_hash = _hash_string(
            row.get("evidence_manifest_hash"), "candidate evidence manifest hash"
        )
        classification = classify_pending_claim_reopen_v1(
            pending_claim=pending,
            evidence_manifest_hash=evidence_hash,
            trigger="new_evidence",
        )
        route = "stable_claim_rehearing"
        if classification["allowed"]:
            lifecycle = "queued"
        elif classification["route"] == "blocked_same_evidence":
            lifecycle = "duplicate_suppressed"
        else:
            lifecycle = "book_end_pending"
        items.append(
            _review_item(
                state_lineage_id=state_lineage_id,
                chapter_id=chapter_id,
                source_kind="candidate_observation",
                route=route,
                subject_prior_card_ids=[
                    _required_string(row.get("prior_card_id"), "prior_card_id")
                ],
                disputed_field=_required_string(
                    row.get("disputed_field"), "disputed_field"
                ),
                source_block_ids=_string_list(
                    row.get("source_block_ids"), "source_block_ids"
                ),
                evidence_manifest_hash=evidence_hash,
                lifecycle_state=lifecycle,
                reason_code="new_pending_claim_evidence",
                source_artifact_hash=queue_hash,
                reopen_classification=classification,
            )
        )

    for row in queue.get("candidate_identity_observations") or []:
        if not isinstance(row, Mapping):
            raise ChapterCycleReviewError("identity observation must be an object")
        observation = _required_string(row.get("observation"), "observation")
        if observation not in {"possible_collision", "supports_continuity"}:
            raise ChapterCycleReviewError("identity observation has a foreign action")
        items.append(
            _review_item(
                state_lineage_id=state_lineage_id,
                chapter_id=chapter_id,
                source_kind="candidate_observation",
                route=(
                    "book_identity_auditor"
                    if observation == "possible_collision"
                    else "continuity_evidence"
                ),
                subject_prior_card_ids=[
                    _required_string(row.get("prior_card_id"), "prior_card_id")
                ],
                disputed_field=None,
                source_block_ids=_string_list(
                    row.get("source_block_ids"), "source_block_ids"
                ),
                evidence_manifest_hash=_hash_string(
                    row.get("evidence_manifest_hash"), "identity evidence hash"
                ),
                lifecycle_state=(
                    "queued" if observation == "possible_collision" else "evidence_only"
                ),
                reason_code=observation,
                source_artifact_hash=queue_hash,
            )
        )

    for row in queue.get("identity_referrals") or []:
        if not isinstance(row, Mapping):
            raise ChapterCycleReviewError("identity referral must be an object")
        card_id = _required_string(row.get("prior_card_id"), "prior_card_id")
        evidence_ids = _string_list(
            row.get("evidence_source_block_ids"),
            "identity evidence_source_block_ids",
            allow_empty=True,
        )
        evidence_hash = canonical_hash(
            {
                "queue_hash": queue_hash,
                "ticket_id": row.get("ticket_id"),
                "prior_card_id": card_id,
                "source_block_ids": evidence_ids,
            }
        )
        items.append(
            _review_item(
                state_lineage_id=state_lineage_id,
                chapter_id=chapter_id,
                source_kind="claim_identity_referral",
                route="book_identity_auditor",
                subject_prior_card_ids=[card_id],
                disputed_field=str(row.get("disputed_field") or "identity_membership"),
                source_block_ids=evidence_ids,
                evidence_manifest_hash=evidence_hash,
                lifecycle_state="queued",
                reason_code=str(row.get("issue_code") or "identity_referral"),
                source_artifact_hash=queue_hash,
            )
        )

    for row in queue.get("pending_identity_reviews") or []:
        if not isinstance(row, Mapping):
            raise ChapterCycleReviewError("pending identity review must be an object")
        card_id = _required_string(row.get("prior_card_id"), "prior_card_id")
        block_ids = _string_list(
            row.get("current_source_block_ids"),
            "identity current_source_block_ids",
            allow_empty=True,
        )
        evidence_hash = canonical_hash(
            {
                "queue_hash": queue_hash,
                "uncertainty_id": row.get("uncertainty_id"),
                "prior_card_id": card_id,
                "source_block_ids": block_ids,
            }
        )
        items.append(
            _review_item(
                state_lineage_id=state_lineage_id,
                chapter_id=chapter_id,
                source_kind="claim_identity_uncertainty",
                route="book_identity_auditor",
                subject_prior_card_ids=[card_id],
                disputed_field="identity_membership",
                source_block_ids=block_ids,
                evidence_manifest_hash=evidence_hash,
                lifecycle_state="queued",
                reason_code="pending_identity_review",
                source_artifact_hash=queue_hash,
            )
        )

    for row in queue.get("prefix_identity_uncertainties") or []:
        if not isinstance(row, Mapping):
            raise ChapterCycleReviewError("prefix identity uncertainty must be an object")
        card_ids = _string_list(row.get("prior_card_ids"), "prior_card_ids")
        evidence_hash = canonical_hash(
            {
                "queue_hash": queue_hash,
                "uncertainty_id": row.get("uncertainty_id"),
                "prior_card_ids": sorted(card_ids),
                "chapter_ids": row.get("chapter_ids"),
            }
        )
        items.append(
            _review_item(
                state_lineage_id=state_lineage_id,
                chapter_id=chapter_id,
                source_kind="prefix_identity_uncertainty",
                route="book_identity_auditor",
                subject_prior_card_ids=card_ids,
                disputed_field="identity_membership",
                source_block_ids=[],
                evidence_manifest_hash=evidence_hash,
                lifecycle_state="queued",
                reason_code=str(row.get("reason_code") or "prefix_identity_uncertainty"),
                source_artifact_hash=queue_hash,
            )
        )
    return items


def _claim_ledger_items(
    *,
    claim_revision_ledger: Mapping[str, Any],
    state_lineage_id: str,
    chapter_id: str,
) -> list[dict[str, Any]]:
    ledger = verify_prior_claim_revision_ledger_v1(claim_revision_ledger)
    if ledger["state_lineage_id"] != state_lineage_id:
        raise ChapterCycleReviewError("claim ledger crosses state lineage")
    revisions = {
        row["revision_id"]: row for row in ledger["claim_revision_rows"]
    }
    items: list[dict[str, Any]] = []
    for pending in ledger["pending_claims"]:
        revision_rows = [revisions[row_id] for row_id in pending["revision_ids"]]
        block_ids = sorted(
            {
                block_id
                for row in revision_rows
                for block_id in [
                    *row.get("current_challenge_block_ids", []),
                    *row.get("selected_source_block_ids", []),
                ]
            }
        )
        evidence_hash = canonical_hash(
            {
                "claim_ledger_hash": ledger["claim_ledger_hash"],
                "prior_card_id": pending["prior_card_id"],
                "disputed_field": pending["disputed_field"],
                "revision_ids": pending["revision_ids"],
                "evidence_manifest_hashes": pending["evidence_manifest_hashes"],
            }
        )
        identity_route = pending["next_review_trigger"] == "identity_resolution"
        items.append(
            _review_item(
                state_lineage_id=state_lineage_id,
                chapter_id=chapter_id,
                source_kind="claim_revision_ledger",
                route=(
                    "book_identity_auditor"
                    if identity_route
                    else "stable_claim_rehearing"
                ),
                subject_prior_card_ids=[pending["prior_card_id"]],
                disputed_field=pending["disputed_field"],
                source_block_ids=block_ids,
                evidence_manifest_hash=evidence_hash,
                lifecycle_state=("queued" if identity_route else "book_end_pending"),
                reason_code=pending["next_review_trigger"],
                source_artifact_hash=ledger["claim_ledger_hash"],
            )
        )
    return items


def build_chapter_cycle_review_ledger_v1(
    *,
    state_lineage_id: str,
    chapter_id: str,
    candidate_review_queue: Mapping[str, Any],
    claim_revision_ledger: Mapping[str, Any] | None = None,
    previous_ledger: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    lineage = _hash_string(state_lineage_id, "state_lineage_id")
    chapter = _required_string(chapter_id, "chapter_id")
    queue = _verify_queue(candidate_review_queue)
    previous_items: list[dict[str, Any]] = []
    queue_hashes: list[str] = []
    if previous_ledger is not None:
        previous = verify_chapter_cycle_review_ledger_v1(previous_ledger)
        if previous["state_lineage_id"] != lineage:
            raise ChapterCycleReviewError("review ledger crosses state lineage")
        previous_items = _clone(previous["review_items"])
        queue_hashes = list(previous["observed_queue_hashes"])
    queue_hash = _hash_string(queue.get("queue_hash"), "queue_hash")
    new_items = _queue_items(queue=queue, state_lineage_id=lineage, chapter_id=chapter)
    if claim_revision_ledger is not None:
        new_items.extend(
            _claim_ledger_items(
                claim_revision_ledger=claim_revision_ledger,
                state_lineage_id=lineage,
                chapter_id=chapter,
            )
        )
    by_id = {row["review_item_id"]: row for row in previous_items}
    for item in new_items:
        prior = by_id.get(item["review_item_id"])
        if prior is not None and prior != item:
            raise ChapterCycleReviewError("review item id collision with unequal bytes")
        by_id[item["review_item_id"]] = item
    body = {
        "schema_version": REVIEW_LEDGER_SCHEMA_VERSION,
        "validator_version": REVIEW_LEDGER_VALIDATOR_VERSION,
        "state_lineage_id": lineage,
        "coverage_through_chapter_id": chapter,
        "observed_queue_hashes": sorted(set([*queue_hashes, queue_hash])),
        "review_items": sorted(by_id.values(), key=lambda row: row["review_item_id"]),
        "production_publish_performed": False,
    }
    return {**body, "review_ledger_hash": canonical_hash(body)}


def append_prefix_identity_uncertainties_v1(
    *,
    ledger: Mapping[str, Any],
    prefix_bundle: Mapping[str, Any],
    chapter_id: str,
) -> dict[str, Any]:
    """Append identity uncertainty created by a completed prefix extension.

    The uncertainty bytes, rather than the changing cumulative prefix hash,
    identify each review item. Replaying the same extension is therefore
    idempotent while genuinely expanded evidence creates a new item.
    """

    from pipeline.literary.chapter_prefix_prior_v1 import (
        verify_chapter_prefix_prior_bundle_v1,
    )

    previous = verify_chapter_cycle_review_ledger_v1(ledger)
    prefix = verify_chapter_prefix_prior_bundle_v1(prefix_bundle)
    lineage = _hash_string(prefix.get("state_lineage_id"), "state_lineage_id")
    chapter = _required_string(chapter_id, "chapter_id")
    if previous["state_lineage_id"] != lineage:
        raise ChapterCycleReviewError("prefix uncertainty crosses review lineage")
    if prefix.get("coverage_through_chapter_id") != chapter:
        raise ChapterCycleReviewError("prefix uncertainty chapter is stale")
    by_id = {row["review_item_id"]: _clone(row) for row in previous["review_items"]}
    for uncertainty in prefix.get("prefix_identity_uncertainties") or []:
        if not isinstance(uncertainty, Mapping):
            raise ChapterCycleReviewError("prefix identity uncertainty must be an object")
        card_ids = _string_list(uncertainty.get("prior_card_ids"), "prior_card_ids")
        semantic_candidate = str(uncertainty.get("reason_code") or "").startswith(
            "semantic_candidate_"
        )
        supplied_evidence_hash = (
            uncertainty.get("evidence_manifest_hash") if semantic_candidate else None
        )
        evidence_hash = (
            _hash_string(supplied_evidence_hash, "semantic evidence manifest hash")
            if supplied_evidence_hash is not None
            else canonical_hash(
                {
                    "uncertainty_id": uncertainty.get("uncertainty_id"),
                    "prior_card_ids": sorted(card_ids),
                    "chapter_ids": uncertainty.get("chapter_ids"),
                    "surface_key": uncertainty.get("surface_key"),
                }
            )
        )
        source_block_ids = _string_list(
            (uncertainty.get("source_block_ids") if semantic_candidate else None) or [],
            "prefix uncertainty source blocks",
            allow_empty=True,
        )
        source_hash = canonical_hash(uncertainty)
        item = _review_item(
            state_lineage_id=lineage,
            chapter_id=chapter,
            source_kind="prefix_identity_uncertainty",
            route="book_identity_auditor",
            subject_prior_card_ids=card_ids,
            disputed_field="identity_membership",
            source_block_ids=source_block_ids,
            evidence_manifest_hash=evidence_hash,
            lifecycle_state=(
                "evidence_only"
                if semantic_candidate and uncertainty.get("review_deferred") is True
                else "queued"
            ),
            reason_code=str(
                uncertainty.get("reason_code") or "prefix_identity_uncertainty"
            ),
            source_artifact_hash=source_hash,
        )
        item["review_case_id"] = uncertainty.get("uncertainty_id")
        item["surface_key"] = uncertainty.get("surface_key")
        prior = by_id.get(item["review_item_id"])
        if prior is not None and prior != item:
            raise ChapterCycleReviewError(
                "prefix uncertainty id collision with unequal bytes"
            )
        by_id[item["review_item_id"]] = item
    body = {
        "schema_version": REVIEW_LEDGER_SCHEMA_VERSION,
        "validator_version": REVIEW_LEDGER_VALIDATOR_VERSION,
        "state_lineage_id": lineage,
        "coverage_through_chapter_id": chapter,
        "observed_queue_hashes": sorted(
            set(
                [
                    *previous["observed_queue_hashes"],
                    _hash_string(prefix.get("prefix_bundle_hash"), "prefix_bundle_hash"),
                ]
            )
        ),
        "review_items": sorted(by_id.values(), key=lambda row: row["review_item_id"]),
        "production_publish_performed": False,
    }
    return {**body, "review_ledger_hash": canonical_hash(body)}


def finalize_chapter_cycle_review_ledger_v1(
    ledger: Mapping[str, Any],
    *,
    closed_review_item_ids: Sequence[str] = (),
) -> dict[str, Any]:
    verified = verify_chapter_cycle_review_ledger_v1(ledger)
    closed = set(closed_review_item_ids)
    known = {row["review_item_id"] for row in verified["review_items"]}
    if not closed <= known:
        raise ChapterCycleReviewError("book-end closure cites a foreign review item")
    rows: list[dict[str, Any]] = []
    for source in verified["review_items"]:
        row = _clone(source)
        if row["review_item_id"] in closed:
            row["lifecycle_state"] = "closed"
        elif row["lifecycle_state"] == "queued":
            row["lifecycle_state"] = "book_end_pending"
        rows.append(row)
    body = {
        key: _clone(value)
        for key, value in verified.items()
        if key not in {"review_ledger_hash", "review_items"}
    }
    body["review_items"] = sorted(rows, key=lambda row: row["review_item_id"])
    body["book_end_finalized"] = True
    body["production_publish_performed"] = False
    return {**body, "review_ledger_hash": canonical_hash(body)}


def verify_chapter_cycle_review_ledger_v1(
    ledger: Mapping[str, Any],
) -> dict[str, Any]:
    if ledger.get("schema_version") != REVIEW_LEDGER_SCHEMA_VERSION:
        raise ChapterCycleReviewError("foreign chapter-cycle review ledger schema")
    if ledger.get("validator_version") != REVIEW_LEDGER_VALIDATOR_VERSION:
        raise ChapterCycleReviewError("chapter-cycle review validator mismatch")
    body = dict(ledger)
    observed = _hash_string(body.pop("review_ledger_hash", None), "review_ledger_hash")
    if canonical_hash(body) != observed:
        raise ChapterCycleReviewError("chapter-cycle review ledger hash mismatch")
    _hash_string(ledger.get("state_lineage_id"), "state_lineage_id")
    if ledger.get("production_publish_performed") is not False:
        raise ChapterCycleReviewError("review ledger claims production publication")
    queue_hashes = _string_list(
        ledger.get("observed_queue_hashes"), "observed_queue_hashes"
    )
    for value in queue_hashes:
        _hash_string(value, "observed queue hash")
    rows = ledger.get("review_items")
    if not isinstance(rows, list):
        raise ChapterCycleReviewError("review_items must be a list")
    seen: set[str] = set()
    for row in rows:
        if not isinstance(row, Mapping):
            raise ChapterCycleReviewError("review item must be an object")
        item_id = _required_string(row.get("review_item_id"), "review_item_id")
        if item_id in seen:
            raise ChapterCycleReviewError("review ledger repeats an item id")
        seen.add(item_id)
        if row.get("route") not in REVIEW_ROUTES:
            raise ChapterCycleReviewError("review item has a foreign route")
        if row.get("lifecycle_state") not in REVIEW_STATES:
            raise ChapterCycleReviewError("review item has a foreign lifecycle")
        if row.get("authority_effect") != "none":
            raise ChapterCycleReviewError("review item grants semantic authority")
        _hash_string(row.get("evidence_manifest_hash"), "evidence manifest hash")
        _hash_string(row.get("source_artifact_hash"), "source artifact hash")
        _string_list(row.get("subject_prior_card_ids"), "subject prior-card ids")
        _string_list(row.get("source_block_ids"), "source block ids", allow_empty=True)
        expected = "cycrev1_" + canonical_hash(_review_item_identity_body(row))[:20]
        if item_id != expected:
            raise ChapterCycleReviewError("review item id is stale")
    return _clone(dict(ledger))


__all__ = [
    "ChapterCycleReviewError",
    "REVIEW_LEDGER_SCHEMA_VERSION",
    "REVIEW_LEDGER_VALIDATOR_VERSION",
    "append_prefix_identity_uncertainties_v1",
    "build_chapter_cycle_review_ledger_v1",
    "finalize_chapter_cycle_review_ledger_v1",
    "verify_chapter_cycle_review_ledger_v1",
]
