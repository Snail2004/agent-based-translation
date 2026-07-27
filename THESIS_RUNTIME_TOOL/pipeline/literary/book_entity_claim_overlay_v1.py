"""Project chapter-prefix stable-claim authority onto resolved book entities.

Identity is resolved by the whole-book Identity/Surface Auditor first.  This
module then applies only the effective stable fields from the chapter cycle.
Historical and disputed values remain in their source artifacts; a pending
field is represented as null authority rather than silently restoring history.
"""

from __future__ import annotations

from copy import deepcopy
from typing import Any, Mapping, Sequence

from pipeline.literary.book_entity_registry_v1 import (
    BookEntityContractError,
    verify_book_entity_index_v1,
    verify_global_entity_registry_v1,
)
from pipeline.literary.chapter_prefix_prior_v1 import (
    CLAIM_FIELDS,
    verify_chapter_prefix_prior_bundle_v1,
)
from pipeline.literary.checkpoint import canonical_hash, canonical_json


OVERLAY_SCHEMA_VERSION = "book_entity_stable_claim_overlay_v1"
OVERLAY_VALIDATOR_VERSION = "book_entity_stable_claim_overlay_validator_v1"
PROFILE_FIELDS = {
    "referent_kind": "referent_kind_claim",
    "referential_gender": "referential_gender_claim",
    "identity_summary": "identity_summary_draft",
}
IDENTITY_FIELDS = frozenset({"identity_membership", "alias_target", "alias_scope"})


class BookEntityClaimOverlayError(BookEntityContractError):
    """Raised when prefix claim authority cannot be projected safely."""


def _clone(value: Any) -> Any:
    return deepcopy(value)


def _required_string(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise BookEntityClaimOverlayError(f"{label} must be a non-empty string")
    return value


def _hash_string(value: Any, label: str) -> str:
    result = _required_string(value, label)
    if len(result) != 64 or any(char not in "0123456789abcdef" for char in result):
        raise BookEntityClaimOverlayError(f"{label} must be a lowercase sha256")
    return result


def _document_block_chapters(document: Mapping[str, Any]) -> tuple[list[str], dict[str, str]]:
    chapters = document.get("chapters")
    if not isinstance(chapters, list) or not chapters:
        raise BookEntityClaimOverlayError("document has no chapters")
    chapter_order: list[str] = []
    block_chapter: dict[str, str] = {}
    for chapter in chapters:
        if not isinstance(chapter, Mapping):
            raise BookEntityClaimOverlayError("document chapter must be an object")
        chapter_id = _required_string(chapter.get("chapter_id"), "chapter_id")
        chapter_order.append(chapter_id)
        for block in chapter.get("blocks") or []:
            if not isinstance(block, Mapping):
                raise BookEntityClaimOverlayError("document block must be an object")
            block_id = _required_string(block.get("block_id"), "block_id")
            if block_id in block_chapter:
                raise BookEntityClaimOverlayError("document repeats a block id")
            block_chapter[block_id] = chapter_id
    if len(chapter_order) != len(set(chapter_order)):
        raise BookEntityClaimOverlayError("document repeats a chapter id")
    return chapter_order, block_chapter


def build_book_entity_stable_claim_overlay_v1(
    *,
    index: Mapping[str, Any],
    prefix_bundle: Mapping[str, Any],
    document: Mapping[str, Any],
) -> dict[str, Any]:
    """Build an exact, non-authoritative candidate-ref claim overlay."""

    verified_index = verify_book_entity_index_v1(index, document=document)
    prefix = verify_chapter_prefix_prior_bundle_v1(prefix_bundle, document=document)
    chapter_order, block_chapter = _document_block_chapters(document)
    if prefix["covered_chapter_ids"] != chapter_order:
        raise BookEntityClaimOverlayError(
            "stable-claim overlay requires exact whole-book prefix coverage"
        )
    if prefix["coverage_through_chapter_id"] != chapter_order[-1]:
        raise BookEntityClaimOverlayError("prefix does not end at the book boundary")
    if prefix["state_lineage_id"] != verified_index["state_lineage_id"]:
        raise BookEntityClaimOverlayError("prefix and book index cross state lineages")
    if prefix["book_source_manifest_hash"] != verified_index["book_source_manifest_hash"]:
        raise BookEntityClaimOverlayError("prefix and book index use different source manifests")

    candidate_by_local = {
        (row["chapter_id"], row["local_candidate_id"]): row
        for row in verified_index["candidate_rows"]
    }
    card_by_id = {
        row["prior_card_id"]: row
        for row in [
            *prefix["b0_context_cards"],
            *prefix["candidate_only_context_cards"],
        ]
    }
    overlay_rows: list[dict[str, Any]] = []
    seen_candidate_refs: set[str] = set()
    for source in prefix["source_entity_manifest"]:
        block_ids = list(source.get("all_source_block_ids") or [])
        if not block_ids:
            raise BookEntityClaimOverlayError("prefix source row has no block provenance")
        chapter_ids = {block_chapter.get(str(block_id)) for block_id in block_ids}
        if None in chapter_ids or len(chapter_ids) != 1:
            raise BookEntityClaimOverlayError("prefix source row crosses chapter boundaries")
        chapter_id = next(iter(chapter_ids))
        candidate = candidate_by_local.get(
            (chapter_id, _required_string(source.get("source_candidate_id"), "source_candidate_id"))
        )
        if candidate is None:
            if source.get("local_state") == "unresolved":
                continue
            raise BookEntityClaimOverlayError("prefix source entity is absent from book index")
        candidate_ref = candidate["candidate_ref"]
        if candidate_ref in seen_candidate_refs:
            raise BookEntityClaimOverlayError("prefix maps one candidate more than once")
        seen_candidate_refs.add(candidate_ref)
        if source.get("source_row_hash") != candidate["source_row_hash"]:
            raise BookEntityClaimOverlayError("prefix and book index source row hashes differ")
        prior_card_id = _required_string(source.get("prior_card_id"), "prior_card_id")
        card = card_by_id.get(prior_card_id)
        if card is None:
            raise BookEntityClaimOverlayError("prefix source row has no current context card")
        disputes = [
            _clone(dict(row))
            for row in card.get("disputed_claims") or []
            if isinstance(row, Mapping)
        ]
        pending_fields = sorted(
            {
                str(row.get("disputed_field"))
                for row in disputes
                if row.get("status") == "pending"
                and row.get("disputed_field") in set(CLAIM_FIELDS)
            }
        )
        identity_pending = any(
            row.get("status") == "pending"
            and row.get("disputed_field") in IDENTITY_FIELDS
            for row in disputes
        )
        effective = _clone(card.get("effective_claims"))
        if not isinstance(effective, Mapping) or set(effective) != set(CLAIM_FIELDS):
            raise BookEntityClaimOverlayError("prefix effective claims are malformed")
        if any(effective[field] is not None for field in pending_fields):
            raise BookEntityClaimOverlayError("pending prefix field still carries authority")
        row_body = {
            "candidate_ref": candidate_ref,
            "chapter_id": chapter_id,
            "local_candidate_id": candidate["local_candidate_id"],
            "prior_card_id": prior_card_id,
            "source_row_hash": candidate["source_row_hash"],
            "effective_claims": effective,
            "pending_stable_fields": pending_fields,
            "identity_pending_before_book_audit": identity_pending,
            "disputed_claims": disputes,
        }
        overlay_rows.append({**row_body, "candidate_claim_hash": canonical_hash(row_body)})

    expected_refs = set(candidate["candidate_ref"] for candidate in verified_index["candidate_rows"])
    if seen_candidate_refs != expected_refs:
        raise BookEntityClaimOverlayError("prefix overlay does not exact-cover book candidates")
    body = {
        "schema_version": OVERLAY_SCHEMA_VERSION,
        "validator_version": OVERLAY_VALIDATOR_VERSION,
        "state_lineage_id": verified_index["state_lineage_id"],
        "book_index_hash": verified_index["book_index_hash"],
        "prefix_bundle_hash": prefix["prefix_bundle_hash"],
        "candidate_claim_rows": sorted(overlay_rows, key=lambda row: row["candidate_ref"]),
        "authority_effect": "field_projection_after_identity",
        "production_publish_performed": False,
    }
    return {**body, "overlay_hash": canonical_hash(body)}


def verify_book_entity_stable_claim_overlay_v1(
    overlay: Mapping[str, Any],
    *,
    index: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    if overlay.get("schema_version") != OVERLAY_SCHEMA_VERSION:
        raise BookEntityClaimOverlayError("foreign stable-claim overlay schema")
    if overlay.get("validator_version") != OVERLAY_VALIDATOR_VERSION:
        raise BookEntityClaimOverlayError("stable-claim overlay validator mismatch")
    body = dict(overlay)
    observed = _hash_string(body.pop("overlay_hash", None), "overlay_hash")
    if canonical_hash(body) != observed:
        raise BookEntityClaimOverlayError("stable-claim overlay hash mismatch")
    if overlay.get("authority_effect") != "field_projection_after_identity":
        raise BookEntityClaimOverlayError("stable-claim overlay claims foreign authority")
    if overlay.get("production_publish_performed") is not False:
        raise BookEntityClaimOverlayError("stable-claim overlay claims publication")
    rows = overlay.get("candidate_claim_rows")
    if not isinstance(rows, list):
        raise BookEntityClaimOverlayError("candidate claim rows must be a list")
    refs: set[str] = set()
    for row in rows:
        if not isinstance(row, Mapping):
            raise BookEntityClaimOverlayError("candidate claim row must be an object")
        candidate_ref = _required_string(row.get("candidate_ref"), "candidate_ref")
        if candidate_ref in refs:
            raise BookEntityClaimOverlayError("stable-claim overlay repeats a candidate")
        refs.add(candidate_ref)
        row_body = {key: _clone(value) for key, value in row.items() if key != "candidate_claim_hash"}
        if row.get("candidate_claim_hash") != canonical_hash(row_body):
            raise BookEntityClaimOverlayError("candidate claim row hash mismatch")
        effective = row.get("effective_claims")
        if not isinstance(effective, Mapping) or set(effective) != set(CLAIM_FIELDS):
            raise BookEntityClaimOverlayError("candidate effective claims are malformed")
        pending_fields = row.get("pending_stable_fields")
        if not isinstance(pending_fields, list) or not set(pending_fields) <= set(CLAIM_FIELDS):
            raise BookEntityClaimOverlayError("pending stable fields are malformed")
        if len(pending_fields) != len(set(pending_fields)):
            raise BookEntityClaimOverlayError("pending stable fields contain duplicates")
        if any(effective[field] is not None for field in pending_fields):
            raise BookEntityClaimOverlayError("pending stable field retained a value")
    if index is not None:
        verified_index = verify_book_entity_index_v1(index)
        if overlay.get("state_lineage_id") != verified_index["state_lineage_id"]:
            raise BookEntityClaimOverlayError("overlay and index cross state lineages")
        if overlay.get("book_index_hash") != verified_index["book_index_hash"]:
            raise BookEntityClaimOverlayError("overlay targets a foreign book index")
        expected = {row["candidate_ref"] for row in verified_index["candidate_rows"]}
        if refs != expected:
            raise BookEntityClaimOverlayError("overlay does not exact-cover index candidates")
    return _clone(dict(overlay))


def _field_resolution(
    *,
    field: str,
    member_rows: Sequence[Mapping[str, Any]],
    root_row: Mapping[str, Any],
) -> tuple[Any, str, list[str]]:
    considered = [root_row] if field == "identity_summary" else list(member_rows)
    pending_refs = sorted(
        row["candidate_ref"]
        for row in considered
        if field in set(row["pending_stable_fields"])
    )
    if pending_refs:
        return None, "pending_source_dispute", pending_refs
    non_null = {
        canonical_json(row["effective_claims"][field]): row["effective_claims"][field]
        for row in considered
        if row["effective_claims"][field] is not None
    }
    if len(non_null) > 1:
        return None, "pending_cross_member_conflict", sorted(
            row["candidate_ref"] for row in considered
        )
    if not non_null:
        return None, "not_asserted", []
    return _clone(next(iter(non_null.values()))), "effective", []


def apply_stable_claim_overlay_to_global_registry_v1(
    *,
    snapshot: Mapping[str, Any],
    index: Mapping[str, Any],
    overlay: Mapping[str, Any],
) -> dict[str, Any]:
    """Apply effective fields after identity decisions without rewriting history."""

    base = verify_global_entity_registry_v1(snapshot)
    verified_index = verify_book_entity_index_v1(index)
    verified_overlay = verify_book_entity_stable_claim_overlay_v1(overlay, index=verified_index)
    if base["state_lineage_id"] != verified_overlay["state_lineage_id"]:
        raise BookEntityClaimOverlayError("registry and overlay cross state lineages")
    if base["book_index_hash"] != verified_overlay["book_index_hash"]:
        raise BookEntityClaimOverlayError("registry and overlay target different indexes")
    by_ref = {row["candidate_ref"]: row for row in verified_overlay["candidate_claim_rows"]}
    pending_rows: list[dict[str, Any]] = []
    body = {key: _clone(value) for key, value in base.items() if key != "snapshot_hash"}
    for table in ("book_confirmed_entities", "chapter_local_entities", "pending_entities"):
        updated_entities: list[dict[str, Any]] = []
        for source_entity in body[table]:
            entity = _clone(source_entity)
            members = list(entity.get("member_candidate_refs") or [])
            profile = entity.get("canonical_profile")
            if not members or not isinstance(profile, Mapping):
                updated_entities.append(entity)
                continue
            try:
                member_rows = [by_ref[ref] for ref in members]
                root_row = by_ref[entity["root_candidate_ref"]]
            except KeyError as exc:
                raise BookEntityClaimOverlayError(
                    "resolved entity references a candidate absent from claim overlay"
                ) from exc
            states: list[dict[str, Any]] = []
            updated_profile = _clone(profile)
            for field in CLAIM_FIELDS:
                value, status, evidence_refs = _field_resolution(
                    field=field,
                    member_rows=member_rows,
                    root_row=root_row,
                )
                updated_profile[PROFILE_FIELDS[field]] = value
                state = {
                    "field": field,
                    "status": status,
                    "candidate_refs": evidence_refs,
                }
                states.append(state)
                if status.startswith("pending_"):
                    pending_body = {
                        "entity_id": entity["entity_id"],
                        "field": field,
                        "status": status,
                        "candidate_refs": evidence_refs,
                        "overlay_hash": verified_overlay["overlay_hash"],
                    }
                    pending_rows.append(
                        {
                            "pending_stable_claim_id": "bkpendclaim1_"
                            + canonical_hash(pending_body)[:20],
                            **pending_body,
                        }
                    )
            updated_profile["effective_claim_states"] = sorted(
                states, key=lambda row: row["field"]
            )
            entity["canonical_profile"] = updated_profile
            entity["stable_claim_overlay_hash"] = verified_overlay["overlay_hash"]
            updated_entities.append(entity)
        body[table] = updated_entities
    body["stable_claim_overlay_hash"] = verified_overlay["overlay_hash"]
    body["pending_stable_claims"] = sorted(
        pending_rows,
        key=lambda row: (row["entity_id"], row["field"]),
    )
    body["has_open_uncertainty"] = bool(
        body.get("pending_entities")
        or body.get("pending_source_repairs")
        or body.get("validation_quarantines")
        or body["pending_stable_claims"]
    )
    result = {**body, "snapshot_hash": canonical_hash(body)}
    return verify_global_entity_registry_v1(result)


__all__ = [
    "BookEntityClaimOverlayError",
    "OVERLAY_SCHEMA_VERSION",
    "OVERLAY_VALIDATOR_VERSION",
    "apply_stable_claim_overlay_to_global_registry_v1",
    "build_book_entity_stable_claim_overlay_v1",
    "verify_book_entity_stable_claim_overlay_v1",
]
