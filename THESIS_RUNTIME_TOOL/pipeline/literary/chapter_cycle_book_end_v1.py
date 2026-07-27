"""Whole-book closure for the chapter-prefix literary entity cycle."""

from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
from typing import Any, Mapping, Sequence

from pipeline.literary.book_entity_claim_overlay_v1 import (
    apply_stable_claim_overlay_to_global_registry_v1,
    build_book_entity_stable_claim_overlay_v1,
    verify_book_entity_stable_claim_overlay_v1,
)
from pipeline.literary.book_entity_registry_v1 import (
    build_book_entity_index_v1,
    build_global_entity_registry_v1,
    render_cross_chapter_request_v1,
    verify_book_entity_index_v1,
    verify_cross_chapter_decision_v1,
    verify_global_entity_registry_v1,
)
from pipeline.literary.chapter_cycle_review_v1 import (
    finalize_chapter_cycle_review_ledger_v1,
    verify_chapter_cycle_review_ledger_v1,
)
from pipeline.literary.chapter_prefix_prior_v1 import (
    verify_chapter_prefix_prior_bundle_v1,
)
from pipeline.literary.checkpoint import canonical_hash
from pipeline.literary.review_case_ledger_v1 import (
    finalize_review_case_ledger_v1,
    verify_review_case_ledger_v1,
)


HANDOFF_SCHEMA_VERSION = "chapter_cycle_book_end_handoff_v1"
HANDOFF_VALIDATOR_VERSION = "chapter_cycle_book_end_validator_v1"
SEALED_SCHEMA_VERSION = "chapter_cycle_sealed_registry_v1"
REQUEST_SET_SCHEMA_VERSION = "chapter_cycle_book_end_request_set_v1"


class ChapterCycleBookEndError(RuntimeError):
    pass


def _clone(value: Any) -> Any:
    return deepcopy(value)


def _required_string(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ChapterCycleBookEndError(f"{label} must be a non-empty string")
    return value


def _document_chapter_ids(document: Mapping[str, Any]) -> list[str]:
    chapters = document.get("chapters")
    if not isinstance(chapters, list) or not chapters:
        raise ChapterCycleBookEndError("document has no chapters")
    result = [_required_string(row.get("chapter_id"), "chapter_id") for row in chapters]
    if len(result) != len(set(result)):
        raise ChapterCycleBookEndError("document repeats a chapter id")
    return result


def build_chapter_cycle_book_end_handoff_v1(
    *,
    document: Mapping[str, Any],
    audited_inventories: Mapping[str, Mapping[str, Any]],
    final_prefix_bundle: Mapping[str, Any],
    review_ledger: Mapping[str, Any],
    review_case_ledger: Mapping[str, Any] | None = None,
    chapter_orientations: Mapping[str, Mapping[str, Any]] | None = None,
    max_component_candidates: int = 64,
    max_component_source_blocks: int = 96,
    max_review_blocks_per_candidate: int = 4,
) -> dict[str, Any]:
    """Prepare a complete, non-publishing book-end Identity handoff."""

    chapter_ids = _document_chapter_ids(document)
    if set(audited_inventories) != set(chapter_ids):
        raise ChapterCycleBookEndError(
            "book-end handoff requires an audited inventory for every chapter"
        )
    prefix = verify_chapter_prefix_prior_bundle_v1(
        final_prefix_bundle, document=document
    )
    if prefix["covered_chapter_ids"] != chapter_ids:
        raise ChapterCycleBookEndError("book-end prefix does not exact-cover the document")
    ledger = verify_chapter_cycle_review_ledger_v1(review_ledger)
    if ledger["state_lineage_id"] != prefix["state_lineage_id"]:
        raise ChapterCycleBookEndError("review ledger and prefix cross state lineages")
    index = build_book_entity_index_v1(
        document=document,
        audited_inventories=audited_inventories,
        chapter_orientations=chapter_orientations,
        max_component_candidates=max_component_candidates,
        max_component_source_blocks=max_component_source_blocks,
        max_review_blocks_per_candidate=max_review_blocks_per_candidate,
    )
    overlay = build_book_entity_stable_claim_overlay_v1(
        index=index,
        prefix_bundle=prefix,
        document=document,
    )
    finalized_ledger = finalize_chapter_cycle_review_ledger_v1(ledger)
    finalized_case_ledger = (
        finalize_review_case_ledger_v1(review_case_ledger)
        if review_case_ledger is not None
        else None
    )
    if (
        finalized_case_ledger is not None
        and finalized_case_ledger["state_lineage_id"] != prefix["state_lineage_id"]
    ):
        raise ChapterCycleBookEndError(
            "review-case ledger and prefix cross state lineages"
        )
    component_rows = [
        {
            "component_id": row["component_id"],
            "candidate_count": len(row["candidate_refs"]),
            "chapter_ids": _clone(row["chapter_ids"]),
            "overflow": row["overflow"],
            "route": "pending_without_model_call" if row["overflow"] else "identity_auditor",
        }
        for row in index["components"]
    ]
    body = {
        "schema_version": HANDOFF_SCHEMA_VERSION,
        "validator_version": HANDOFF_VALIDATOR_VERSION,
        "state_lineage_id": index["state_lineage_id"],
        "book_source_manifest_hash": index["book_source_manifest_hash"],
        "book_index": index,
        "stable_claim_overlay": overlay,
        "review_ledger": finalized_ledger,
        "review_case_ledger": finalized_case_ledger,
        "component_manifest": sorted(component_rows, key=lambda row: row["component_id"]),
        "required_identity_component_ids": sorted(
            row["component_id"] for row in index["components"] if not row["overflow"]
        ),
        "overflow_component_ids": sorted(
            row["component_id"] for row in index["components"] if row["overflow"]
        ),
        "b2_ready": False,
        "production_publish_performed": False,
    }
    return {**body, "handoff_hash": canonical_hash(body)}


def verify_chapter_cycle_book_end_handoff_v1(
    handoff: Mapping[str, Any],
    *,
    document: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    if handoff.get("schema_version") != HANDOFF_SCHEMA_VERSION:
        raise ChapterCycleBookEndError("foreign book-end handoff schema")
    if handoff.get("validator_version") != HANDOFF_VALIDATOR_VERSION:
        raise ChapterCycleBookEndError("book-end handoff validator mismatch")
    body = dict(handoff)
    observed = _required_string(body.pop("handoff_hash", None), "handoff_hash")
    if canonical_hash(body) != observed:
        raise ChapterCycleBookEndError("book-end handoff hash mismatch")
    if handoff.get("b2_ready") is not False:
        raise ChapterCycleBookEndError("unsealed handoff claims B2 readiness")
    if handoff.get("production_publish_performed") is not False:
        raise ChapterCycleBookEndError("book-end handoff claims publication")
    index = verify_book_entity_index_v1(handoff.get("book_index"), document=document)
    overlay = verify_book_entity_stable_claim_overlay_v1(
        handoff.get("stable_claim_overlay"), index=index
    )
    ledger = verify_chapter_cycle_review_ledger_v1(handoff.get("review_ledger"))
    case_ledger = (
        verify_review_case_ledger_v1(handoff.get("review_case_ledger"))
        if handoff.get("review_case_ledger") is not None
        else None
    )
    if not ledger.get("book_end_finalized"):
        raise ChapterCycleBookEndError("book-end review ledger is not finalized")
    if not (
        index["state_lineage_id"]
        == overlay["state_lineage_id"]
        == ledger["state_lineage_id"]
        == handoff.get("state_lineage_id")
    ):
        raise ChapterCycleBookEndError("book-end handoff crosses state lineages")
    if case_ledger is not None and (
        case_ledger["state_lineage_id"] != handoff.get("state_lineage_id")
        or not case_ledger.get("book_end_finalized")
    ):
        raise ChapterCycleBookEndError("book-end review-case ledger is stale")
    expected_components = sorted(
        row["component_id"] for row in index["components"] if not row["overflow"]
    )
    if handoff.get("required_identity_component_ids") != expected_components:
        raise ChapterCycleBookEndError("book-end component manifest is stale")
    return _clone(dict(handoff))


def render_chapter_cycle_book_end_requests_v1(
    *,
    handoff: Mapping[str, Any],
    document: Mapping[str, Any],
    design_doc: Path,
) -> dict[str, Any]:
    """Render every non-overflow Identity component without calling a model."""

    verified = verify_chapter_cycle_book_end_handoff_v1(
        handoff, document=document
    )
    index = verified["book_index"]
    requests: list[dict[str, Any]] = []
    for component_id in verified["required_identity_component_ids"]:
        rendered = render_cross_chapter_request_v1(
            index=index,
            component_id=component_id,
            document=document,
            design_doc=Path(design_doc),
        )
        request_body = {
            "component_id": rendered.component_id,
            "request_fingerprint": rendered.request_fingerprint,
            "messages": [dict(row) for row in rendered.messages],
            "response_schema": rendered.response_schema,
            "semantic_payload": rendered.semantic_payload,
        }
        requests.append({**request_body, "request_hash": canonical_hash(request_body)})
    body = {
        "schema_version": REQUEST_SET_SCHEMA_VERSION,
        "state_lineage_id": verified["state_lineage_id"],
        "handoff_hash": verified["handoff_hash"],
        "book_index_hash": index["book_index_hash"],
        "requests": sorted(requests, key=lambda row: row["component_id"]),
        "overflow_component_ids": _clone(verified["overflow_component_ids"]),
        "api_calls_performed": 0,
        "production_publish_performed": False,
    }
    return {**body, "request_set_hash": canonical_hash(body)}


def verify_chapter_cycle_book_end_request_set_v1(
    request_set: Mapping[str, Any],
    *,
    handoff: Mapping[str, Any],
) -> dict[str, Any]:
    if request_set.get("schema_version") != REQUEST_SET_SCHEMA_VERSION:
        raise ChapterCycleBookEndError("foreign book-end request-set schema")
    body = dict(request_set)
    observed = _required_string(
        body.pop("request_set_hash", None), "request_set_hash"
    )
    if canonical_hash(body) != observed:
        raise ChapterCycleBookEndError("book-end request-set hash mismatch")
    verified = verify_chapter_cycle_book_end_handoff_v1(handoff)
    if request_set.get("handoff_hash") != verified["handoff_hash"]:
        raise ChapterCycleBookEndError("request set targets a foreign handoff")
    if request_set.get("book_index_hash") != verified["book_index"]["book_index_hash"]:
        raise ChapterCycleBookEndError("request set targets a foreign book index")
    if request_set.get("api_calls_performed") != 0:
        raise ChapterCycleBookEndError("dry request set claims API calls")
    if request_set.get("production_publish_performed") is not False:
        raise ChapterCycleBookEndError("request set claims publication")
    requests = request_set.get("requests")
    if not isinstance(requests, list):
        raise ChapterCycleBookEndError("book-end requests must be a list")
    component_ids: list[str] = []
    for row in requests:
        if not isinstance(row, Mapping):
            raise ChapterCycleBookEndError("book-end request must be an object")
        request_body = {
            key: _clone(value) for key, value in row.items() if key != "request_hash"
        }
        if row.get("request_hash") != canonical_hash(request_body):
            raise ChapterCycleBookEndError("book-end request hash mismatch")
        component_ids.append(_required_string(row.get("component_id"), "component_id"))
        messages = row.get("messages")
        semantic_payload = row.get("semantic_payload")
        if (
            not isinstance(messages, list)
            or not messages
            or not isinstance(messages[-1], Mapping)
            or not isinstance(semantic_payload, Mapping)
        ):
            raise ChapterCycleBookEndError("book-end request payload is malformed")
        try:
            rendered_payload = json.loads(str(messages[-1].get("content") or ""))
        except (TypeError, ValueError) as exc:
            raise ChapterCycleBookEndError(
                "book-end user message is not valid JSON"
            ) from exc
        if canonical_hash(semantic_payload) != canonical_hash(rendered_payload):
            raise ChapterCycleBookEndError("request payload differs from rendered user bytes")
    if sorted(component_ids) != verified["required_identity_component_ids"]:
        raise ChapterCycleBookEndError("request set does not exact-cover components")
    return _clone(dict(request_set))


def _closed_review_item_ids(
    *,
    ledger: Mapping[str, Any],
    overlay: Mapping[str, Any],
    snapshot: Mapping[str, Any],
) -> list[str]:
    prior_to_ref = {
        row["prior_card_id"]: row["candidate_ref"]
        for row in overlay["candidate_claim_rows"]
    }
    nonpending_refs = {
        ref
        for table in ("book_confirmed_entities", "chapter_local_entities")
        for entity in snapshot[table]
        for ref in entity.get("member_candidate_refs") or []
    }
    closed_refs = {row["candidate_ref"] for row in snapshot["closed_candidates"]}
    pending_by_entity_field = {
        (row["entity_id"], row["field"])
        for row in snapshot.get("pending_stable_claims") or []
    }
    entity_by_ref = {
        ref: entity
        for table in (
            "book_confirmed_entities",
            "chapter_local_entities",
            "pending_entities",
        )
        for entity in snapshot[table]
        for ref in entity.get("member_candidate_refs") or []
    }
    closed: list[str] = []
    for item in ledger["review_items"]:
        refs = [
            prior_to_ref[card_id]
            for card_id in item["subject_prior_card_ids"]
            if card_id in prior_to_ref
        ]
        if len(refs) != len(item["subject_prior_card_ids"]):
            continue
        if item["route"] == "book_identity_auditor":
            if all(ref in nonpending_refs or ref in closed_refs for ref in refs):
                closed.append(item["review_item_id"])
        elif item["route"] == "stable_claim_rehearing" and item.get("disputed_field"):
            if all(
                ref in entity_by_ref
                and (
                    entity_by_ref[ref]["entity_id"],
                    item["disputed_field"],
                )
                not in pending_by_entity_field
                for ref in refs
            ):
                closed.append(item["review_item_id"])
    return sorted(closed)


def seal_chapter_cycle_global_registry_v1(
    *,
    handoff: Mapping[str, Any],
    decisions: Sequence[Mapping[str, Any]],
    document: Mapping[str, Any],
) -> dict[str, Any]:
    """Seal the effective registry after exact-cover Identity decisions."""

    verified = verify_chapter_cycle_book_end_handoff_v1(
        handoff, document=document
    )
    index = verified["book_index"]
    normalized_decisions = [
        verify_cross_chapter_decision_v1(row, index=index) for row in decisions
    ]
    observed_components = sorted(row["component_id"] for row in normalized_decisions)
    if observed_components != verified["required_identity_component_ids"]:
        raise ChapterCycleBookEndError(
            "identity decisions do not exact-cover required book-end components"
        )
    base_snapshot = build_global_entity_registry_v1(
        index=index,
        decisions=normalized_decisions,
        document=document,
    )
    effective_snapshot = apply_stable_claim_overlay_to_global_registry_v1(
        snapshot=base_snapshot,
        index=index,
        overlay=verified["stable_claim_overlay"],
    )
    closed_ids = _closed_review_item_ids(
        ledger=verified["review_ledger"],
        overlay=verified["stable_claim_overlay"],
        snapshot=effective_snapshot,
    )
    final_ledger = finalize_chapter_cycle_review_ledger_v1(
        verified["review_ledger"], closed_review_item_ids=closed_ids
    )
    body = {
        "schema_version": SEALED_SCHEMA_VERSION,
        "state_lineage_id": verified["state_lineage_id"],
        "handoff_hash": verified["handoff_hash"],
        "book_index_hash": index["book_index_hash"],
        "stable_claim_overlay_hash": verified["stable_claim_overlay"]["overlay_hash"],
        "identity_decision_hashes": sorted(
            row["decision_hash"] for row in normalized_decisions
        ),
        "global_registry_snapshot": effective_snapshot,
        "final_review_ledger": final_ledger,
        "final_review_case_ledger": verified.get("review_case_ledger"),
        "b2_ready": True,
        "production_publish_performed": False,
    }
    return {**body, "sealed_registry_hash": canonical_hash(body)}


def verify_sealed_chapter_cycle_global_registry_v1(
    artifact: Mapping[str, Any],
) -> dict[str, Any]:
    if artifact.get("schema_version") != SEALED_SCHEMA_VERSION:
        raise ChapterCycleBookEndError("foreign sealed chapter-cycle registry schema")
    body = dict(artifact)
    observed = _required_string(
        body.pop("sealed_registry_hash", None), "sealed_registry_hash"
    )
    if canonical_hash(body) != observed:
        raise ChapterCycleBookEndError("sealed chapter-cycle registry hash mismatch")
    if artifact.get("b2_ready") is not True:
        raise ChapterCycleBookEndError("sealed chapter-cycle registry blocks B2")
    if artifact.get("production_publish_performed") is not False:
        raise ChapterCycleBookEndError("sealed artifact claims publication")
    snapshot = verify_global_entity_registry_v1(
        artifact.get("global_registry_snapshot")
    )
    ledger = verify_chapter_cycle_review_ledger_v1(
        artifact.get("final_review_ledger")
    )
    case_ledger = (
        verify_review_case_ledger_v1(artifact.get("final_review_case_ledger"))
        if artifact.get("final_review_case_ledger") is not None
        else None
    )
    if snapshot["state_lineage_id"] != artifact.get("state_lineage_id"):
        raise ChapterCycleBookEndError("sealed snapshot crosses state lineage")
    if ledger["state_lineage_id"] != artifact.get("state_lineage_id"):
        raise ChapterCycleBookEndError("sealed review ledger crosses state lineage")
    if case_ledger is not None and case_ledger["state_lineage_id"] != artifact.get(
        "state_lineage_id"
    ):
        raise ChapterCycleBookEndError(
            "sealed review-case ledger crosses state lineage"
        )
    return _clone(dict(artifact))


__all__ = [
    "ChapterCycleBookEndError",
    "HANDOFF_SCHEMA_VERSION",
    "HANDOFF_VALIDATOR_VERSION",
    "REQUEST_SET_SCHEMA_VERSION",
    "SEALED_SCHEMA_VERSION",
    "build_chapter_cycle_book_end_handoff_v1",
    "render_chapter_cycle_book_end_requests_v1",
    "seal_chapter_cycle_global_registry_v1",
    "verify_chapter_cycle_book_end_handoff_v1",
    "verify_chapter_cycle_book_end_request_set_v1",
    "verify_sealed_chapter_cycle_global_registry_v1",
]
