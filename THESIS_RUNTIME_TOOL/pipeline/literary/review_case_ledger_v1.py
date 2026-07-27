"""Append-only lifecycle for unresolved Literary registry questions.

The ledger is deliberately non-authoritative.  Code may retrieve evidence,
deduplicate it, route a case, enforce hearing limits, or lower authority.  Only
an Auditor decision may grant block/chapter scope or nominate a book-level
candidate.
"""

from __future__ import annotations

from copy import deepcopy
import re
from typing import Any, Mapping, Sequence
import unicodedata

from pipeline.literary.chapter_cycle_review_v1 import (
    verify_chapter_cycle_review_ledger_v1,
)
from pipeline.literary.chapter_prefix_prior_v1 import (
    verify_chapter_prefix_prior_bundle_v1,
)
from pipeline.literary.chapter_registry_v4 import route_alias_for_commit
from pipeline.literary.checkpoint import canonical_hash


LEDGER_SCHEMA_VERSION = "literary_review_case_ledger_v1"
LEDGER_VALIDATOR_VERSION = "literary_review_case_ledger_validator_v1"
PACKET_SCHEMA_VERSION = "literary_relevant_review_case_packet_v1"
DEFAULT_MAX_RELEVANT_CASES = 16
DEFAULT_AUTOMATIC_HEARING_LIMIT = 2

CASE_TYPES = frozenset(
    {
        "alias_scope",
        "prior_enrichment",
        "entity_identity",
        "stable_claim",
        "glossary_sense",
        "unresolved_referent",
        "importance_recall",
        "source_repair",
        "validation_repair",
    }
)
CASE_STATUSES = frozenset(
    {
        "collecting_evidence",
        "ready_for_review",
        "block_scoped",
        "chapter_scoped",
        "book_candidate",
        "book_confirmed",
        "dismissed_dormant",
        "technical_repair",
        "book_end_pending",
        "hard_rejected",
        "closed",
    }
)
NEXT_ACTORS = frozenset(
    {
        "b1",
        "code_repair",
        "local_auditor",
        "stable_claim_auditor",
        "identity_auditor",
        "book_end_auditor",
        "none",
    }
)
AUTHORITY_EFFECTS = frozenset(
    {
        "none",
        "retrieval_only",
        "block_scoped",
        "chapter_scoped",
        "book_candidate",
        "book_global",
    }
)
REOPEN_TRIGGERS = frozenset(
    {
        "surface_recurrence",
        "new_material_evidence",
        "identity_resolution",
        "technical_retry",
        "book_end",
        "manual_only",
        "none",
    }
)
OBSERVATIONS = frozenset(
    {
        "supports",
        "conflicts",
        "not_same_referent",
        "ambiguous",
        "no_new_evidence",
        "gate_deferred",
        "prior_enrichment_proposed",
        "pending_inventory_row",
        "dormant_inventory_row",
        "priority_review_lead",
        "legacy_review_lead",
        "technical_failure",
    }
)
SURFACE_SCOPE_ACTIONS = frozenset(
    {
        "confirm_block_scope",
        "confirm_chapter_scope",
        "nominate_book_candidate",
        "keep_pending",
        "dismiss_dormant",
    }
)
TERMINAL_STATUSES = frozenset({"book_confirmed", "hard_rejected", "closed"})
DECISION_ACTIONS = frozenset(
    {
        *SURFACE_SCOPE_ACTIONS,
        "resolved_distinct",
        "provisional_link",
    }
)


class ReviewCaseLedgerError(RuntimeError):
    """Raised when review-case lineage, evidence, or lifecycle is malformed."""


def _clone(value: Any) -> Any:
    return deepcopy(value)


def _required_string(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ReviewCaseLedgerError(f"{label} must be a non-empty string")
    return value


def _hash_string(value: Any, label: str) -> str:
    result = _required_string(value, label)
    if len(result) != 64 or any(char not in "0123456789abcdef" for char in result):
        raise ReviewCaseLedgerError(f"{label} must be a lowercase sha256")
    return result


def _verified_artifact_hash(
    artifact: Mapping[str, Any], *, hash_field: str, label: str
) -> str:
    observed = _hash_string(artifact.get(hash_field), hash_field)
    body = dict(artifact)
    body.pop(hash_field, None)
    if canonical_hash(body) != observed:
        raise ReviewCaseLedgerError(f"{label} hash mismatch")
    return observed


def _string_list(
    value: Any,
    label: str,
    *,
    allow_empty: bool = False,
    sorted_unique: bool = False,
) -> list[str]:
    if not isinstance(value, list) or (not allow_empty and not value):
        raise ReviewCaseLedgerError(f"{label} must be a string list")
    rows = [_required_string(row, label) for row in value]
    if len(rows) != len(set(rows)):
        raise ReviewCaseLedgerError(f"{label} contains duplicates")
    if sorted_unique and rows != sorted(rows):
        raise ReviewCaseLedgerError(f"{label} must be sorted")
    return rows


def _surface_key(value: Any) -> str:
    text = unicodedata.normalize("NFKC", str(value)).casefold()
    text = re.sub(r"\s+", " ", text).strip()
    return text.strip(" \t\r\n.,;:!?\"'()[]{}")


def _block_text(block: Mapping[str, Any]) -> str:
    for key in ("clean_text", "source_text", "text", "content", "raw_text"):
        value = block.get(key)
        if isinstance(value, str) and value.strip():
            return unicodedata.normalize("NFC", value)
    return ""


def _contains_surface(text: str, surface: str) -> bool:
    if not surface:
        return False
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


def _chapter(
    document: Mapping[str, Any], chapter_id: str
) -> Mapping[str, Any]:
    rows = [
        row
        for row in document.get("chapters") or []
        if isinstance(row, Mapping) and row.get("chapter_id") == chapter_id
    ]
    if len(rows) != 1:
        raise ReviewCaseLedgerError("review case chapter is absent or duplicated")
    return rows[0]


def _source_catalog(
    document: Mapping[str, Any], chapter_id: str
) -> tuple[dict[str, str], dict[str, int]]:
    chapter = _chapter(document, chapter_id)
    ordered = sorted(
        chapter.get("blocks") or [],
        key=lambda row: (
            int(row.get("order_index") or 0),
            str(row.get("block_id") or ""),
        ),
    )
    catalog: dict[str, str] = {}
    order: dict[str, int] = {}
    for offset, block in enumerate(ordered):
        if not isinstance(block, Mapping):
            raise ReviewCaseLedgerError("review case source block is malformed")
        block_id = _required_string(block.get("block_id"), "block_id")
        if block_id in catalog:
            raise ReviewCaseLedgerError("review case chapter repeats a block id")
        catalog[block_id] = _block_text(block)
        order[block_id] = offset
    return catalog, order


def _case_identity_body(row: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "state_lineage_id": row["state_lineage_id"],
        "case_type": row["case_type"],
        "subject_refs": _clone(row["subject_refs"]),
        "surface_key": row["surface_key"],
        "disputed_field": row.get("disputed_field"),
    }


def _evidence_row(
    *,
    chapter_id: str,
    source_block_ids: Sequence[str],
    observation: str,
    reason_code: str,
    source_artifact_hash: str,
    source_kind: str,
) -> dict[str, Any]:
    if observation not in OBSERVATIONS:
        raise ReviewCaseLedgerError("review evidence has a foreign observation")
    body = {
        "chapter_id": _required_string(chapter_id, "evidence chapter_id"),
        "source_block_ids": sorted(set(source_block_ids)),
        "observation": observation,
        "reason_code": _required_string(reason_code, "evidence reason_code"),
        "source_artifact_hash": _hash_string(
            source_artifact_hash, "evidence source artifact hash"
        ),
        "source_kind": _required_string(source_kind, "evidence source_kind"),
    }
    # Re-running a model over the same immutable source blocks is not new
    # evidence. Keep the producing artifact for provenance, but deduplicate
    # reopening by the source coordinates themselves.
    content_body = {
        "chapter_id": body["chapter_id"],
        "source_block_ids": body["source_block_ids"],
    }
    enriched = {**body, "evidence_content_hash": canonical_hash(content_body)}
    return {**enriched, "evidence_hash": canonical_hash(enriched)}


def _deadline(limit: int = DEFAULT_AUTOMATIC_HEARING_LIMIT) -> dict[str, Any]:
    return {
        "automatic_hearing_limit": int(limit),
        "same_evidence_reopen_forbidden": True,
        "unresolved_after_limit": "book_end_pending",
    }


def _lifecycle_defaults(
    *,
    status: str,
    case_type: str,
) -> tuple[str, str, str, list[str]]:
    if status == "ready_for_review":
        actor = {
            "stable_claim": "stable_claim_auditor",
            "glossary_sense": "book_end_auditor",
            "importance_recall": "book_end_auditor",
        }.get(case_type, "identity_auditor")
        return actor, "new_material_evidence", "none", [
            "bounded_source_evidence"
        ]
    if status == "technical_repair":
        return "code_repair", "technical_retry", "none", [
            "valid_source_address"
        ]
    if status == "book_end_pending":
        return "book_end_auditor", "book_end", "none", [
            "whole_book_closure"
        ]
    if status == "book_candidate":
        return "b1", "surface_recurrence", "book_candidate", [
            "whole_book_scope_evidence"
        ]
    if status == "block_scoped":
        return "b1", "surface_recurrence", "block_scoped", [
            "cross_block_scope_evidence"
        ]
    if status == "chapter_scoped":
        return "b1", "surface_recurrence", "chapter_scoped", [
            "cross_chapter_scope_evidence"
        ]
    if status == "dismissed_dormant":
        return "b1", "new_material_evidence", "retrieval_only", [
            "materially_new_evidence"
        ]
    if status in TERMINAL_STATUSES:
        authority = "book_global" if status == "book_confirmed" else "none"
        return "none", "none", authority, []
    return "b1", "surface_recurrence", "retrieval_only", [
        "materially_new_evidence"
    ]


def _validate_lifecycle_tuple(
    row: Mapping[str, Any], *, book_end_finalized: bool
) -> None:
    status = row["status"]
    actor = row["next_actor"]
    trigger = row["reopen_trigger"]
    authority = row["authority_effect"]
    case_type = row["case_type"]

    if status == "collecting_evidence":
        allowed = (
            actor == "b1"
            and trigger in {"surface_recurrence", "new_material_evidence"}
            and authority == "retrieval_only"
        )
    elif status == "ready_for_review":
        expected_actor = {
            "stable_claim": "stable_claim_auditor",
            "glossary_sense": "book_end_auditor",
            "importance_recall": "book_end_auditor",
        }.get(case_type, "identity_auditor")
        allowed = (
            actor == expected_actor
            and trigger == "new_material_evidence"
            and authority == "none"
        )
    elif status in {"block_scoped", "chapter_scoped"}:
        expected_authority = status
        allowed = authority == expected_authority and (
            (
                not book_end_finalized
                and actor == "b1"
                and trigger == "surface_recurrence"
            )
            or (
                book_end_finalized
                and actor == "none"
                and trigger == "none"
            )
        )
    elif status == "book_candidate":
        allowed = authority == "book_candidate" and (
            (
                not book_end_finalized
                and actor == "b1"
                and trigger == "surface_recurrence"
            )
            or (
                book_end_finalized
                and actor == "book_end_auditor"
                and trigger == "book_end"
            )
        )
    elif status == "book_confirmed":
        allowed = actor == "none" and trigger == "none" and authority == "book_global"
    elif status == "dismissed_dormant":
        allowed = (
            actor == "b1"
            and trigger == "new_material_evidence"
            and authority == "retrieval_only"
        )
    elif status == "technical_repair":
        allowed = (
            actor == "code_repair"
            and trigger == "technical_retry"
            and authority == "none"
        )
    elif status == "book_end_pending":
        allowed = (
            actor == "book_end_auditor"
            and trigger == "book_end"
            and authority == "none"
        )
    elif status in {"hard_rejected", "closed"}:
        allowed = actor == "none" and trigger == "none" and authority == "none"
    else:  # pragma: no cover - guarded by the closed status enum.
        allowed = False
    if not allowed:
        raise ReviewCaseLedgerError(
            "review-case status, owner, trigger, and authority disagree"
        )


def _new_case(
    *,
    state_lineage_id: str,
    case_type: str,
    subject_refs: Sequence[str],
    surface: str,
    disputed_field: str | None,
    status: str,
    evidence: Mapping[str, Any],
    evidence_needed: Sequence[str] | None = None,
) -> dict[str, Any]:
    if case_type not in CASE_TYPES or status not in CASE_STATUSES:
        raise ReviewCaseLedgerError("review case has a foreign type or status")
    refs = sorted(set(subject_refs))
    if not refs:
        raise ReviewCaseLedgerError("review case has no subject")
    checked_surface = _required_string(surface, "review case surface")
    actor, trigger, authority, default_needed = _lifecycle_defaults(
        status=status, case_type=case_type
    )
    needed = sorted(set(evidence_needed or default_needed))
    identity = {
        "state_lineage_id": _hash_string(
            state_lineage_id, "state_lineage_id"
        ),
        "case_type": case_type,
        "subject_refs": refs,
        "surface_key": _surface_key(checked_surface),
        "disputed_field": disputed_field,
    }
    return {
        "review_case_id": "litcase1_" + canonical_hash(identity)[:20],
        **identity,
        "surface": checked_surface,
        "status": status,
        "authority_effect": authority,
        "next_actor": actor,
        "reopen_trigger": trigger,
        "evidence_needed": needed,
        "hearing_count": 0,
        "deadline": _deadline(),
        "evidence_history": [_clone(dict(evidence))],
        "decision_history": [],
        "last_seen_chapter_id": evidence["chapter_id"],
        "last_heard_chapter_id": None,
    }


def _append_evidence(
    case: Mapping[str, Any], evidence: Mapping[str, Any]
) -> dict[str, Any]:
    row = _clone(dict(case))
    existing = {
        _required_string(
            item.get("evidence_content_hash"), "evidence_content_hash"
        )
        for item in row["evidence_history"]
    }
    if evidence["evidence_content_hash"] not in existing:
        row["evidence_history"].append(_clone(dict(evidence)))
    row["last_seen_chapter_id"] = evidence["chapter_id"]
    return row


def _put_case(
    cases: dict[str, dict[str, Any]], incoming: Mapping[str, Any]
) -> None:
    case_id = _required_string(incoming.get("review_case_id"), "review_case_id")
    prior = cases.get(case_id)
    if prior is None:
        cases[case_id] = _clone(dict(incoming))
        return
    if _case_identity_body(prior) != _case_identity_body(incoming):
        raise ReviewCaseLedgerError("review case id collision with unequal identity")
    merged = prior
    for evidence in incoming["evidence_history"]:
        merged = _append_evidence(merged, evidence)
    if (
        merged["status"] not in TERMINAL_STATUSES
        and incoming["status"] == "ready_for_review"
    ):
        merged["status"] = "ready_for_review"
        actor, trigger, authority, needed = _lifecycle_defaults(
            status="ready_for_review", case_type=merged["case_type"]
        )
        merged["next_actor"] = actor
        merged["reopen_trigger"] = trigger
        merged["authority_effect"] = authority
        merged["evidence_needed"] = needed
    cases[case_id] = merged


def _prior_cards(prefix: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    rows = [
        *(prefix.get("b0_context_cards") or []),
        *(prefix.get("candidate_only_context_cards") or []),
    ]
    return {
        _required_string(row.get("prior_card_id"), "prior_card_id"): _clone(
            dict(row)
        )
        for row in rows
        if isinstance(row, Mapping)
    }


def _source_to_prior(prefix: Mapping[str, Any]) -> dict[str, str]:
    return {
        _required_string(row.get("source_candidate_id"), "source_candidate_id"):
        _required_string(row.get("prior_card_id"), "prior_card_id")
        for row in prefix.get("source_entity_manifest") or []
        if isinstance(row, Mapping)
    }


def _source_to_glossary(prefix: Mapping[str, Any]) -> dict[str, str]:
    return {
        _required_string(row.get("source_candidate_id"), "source_candidate_id"):
        _required_string(row.get("glossary_card_id"), "glossary_card_id")
        for row in prefix.get("source_glossary_manifest") or []
        if isinstance(row, Mapping)
    }


def _candidate_rows(
    inventory: Mapping[str, Any],
) -> list[tuple[str, Mapping[str, Any]]]:
    result: list[tuple[str, Mapping[str, Any]]] = []
    for table, local_state in (
        ("entity_candidates", "confirmed"),
        ("pending_entity_candidates", "pending"),
        ("closed_entity_candidates", "closed"),
    ):
        rows = inventory.get(table) or []
        if not isinstance(rows, list):
            raise ReviewCaseLedgerError(f"{table} must be a list")
        for row in rows:
            if not isinstance(row, Mapping):
                raise ReviewCaseLedgerError(f"{table} row must be an object")
            result.append((local_state, row))
    return result


def _case_from_alias(
    *,
    lineage: str,
    prior_card_id: str,
    row: Mapping[str, Any],
    alias: Mapping[str, Any],
    source_catalog: Mapping[str, str],
    chapter_id: str,
    inventory_hash: str,
) -> dict[str, Any] | None:
    surface = _required_string(alias.get("surface"), "alternative surface")
    name_class = _required_string(alias.get("name_class"), "alternative name_class")
    source_ids = [
        _required_string(value, "alternative source block")
        for value in (
            alias.get("surface_match_block_ids")
            or alias.get("source_block_ids")
            or []
        )
    ]
    if not source_ids:
        return None
    gate = route_alias_for_commit(
        surface=surface,
        name_class=name_class,
        target_entity_id=prior_card_id,
        source_block_ids=source_ids,
        source_catalog=source_catalog,
        source_decision_lineage={
            "source_inventory_hash": inventory_hash,
            "source_candidate_id": row.get("candidate_id"),
            "purpose": "review_case_discovery",
        },
    )
    if gate["outcome"] == "eligible_global_alias":
        return None
    evidence = _evidence_row(
        chapter_id=chapter_id,
        source_block_ids=source_ids,
        observation="gate_deferred",
        reason_code=gate["reason_code"],
        source_artifact_hash=inventory_hash,
        source_kind="alias_commit_gate",
    )
    return _new_case(
        state_lineage_id=lineage,
        case_type="alias_scope",
        subject_refs=[prior_card_id],
        surface=surface,
        disputed_field="alias_scope",
        status="collecting_evidence",
        evidence=evidence,
        evidence_needed=[
            (
                "referent_attribution"
                if gate["outcome"] == "defer_to_b2"
                else "scope_disambiguation"
            )
        ],
    )


def _inventory_cases(
    *,
    document: Mapping[str, Any],
    chapter_id: str,
    prefix: Mapping[str, Any],
    inventory: Mapping[str, Any],
) -> list[dict[str, Any]]:
    lineage = prefix["state_lineage_id"]
    inventory_hash = _hash_string(
        inventory.get("conflict_audited_inventory_hash"),
        "conflict audited inventory hash",
    )
    source_catalog, _order = _source_catalog(document, chapter_id)
    source_to_prior = _source_to_prior(prefix)
    source_to_glossary = _source_to_glossary(prefix)
    result: list[dict[str, Any]] = []

    for local_state, row in _candidate_rows(inventory):
        candidate_id = _required_string(row.get("candidate_id"), "candidate_id")
        prior_card_id = source_to_prior.get(candidate_id)
        if prior_card_id is None:
            continue
        for alias in row.get("alternative_names") or []:
            if not isinstance(alias, Mapping):
                raise ReviewCaseLedgerError("alternative name must be an object")
            case = _case_from_alias(
                lineage=lineage,
                prior_card_id=prior_card_id,
                row=row,
                alias=alias,
                source_catalog=source_catalog,
                chapter_id=chapter_id,
                inventory_hash=inventory_hash,
            )
            if case is not None:
                result.append(case)
        if local_state not in {"pending", "closed"}:
            continue
        surface = _required_string(row.get("canonical_surface"), "canonical_surface")
        source_ids = [
            _required_string(value, "candidate source block")
            for value in (
                row.get("source_block_ids")
                or row.get("surface_match_block_ids")
                or []
            )
        ]
        evidence = _evidence_row(
            chapter_id=chapter_id,
            source_block_ids=source_ids,
            observation=(
                "pending_inventory_row"
                if local_state == "pending"
                else "dormant_inventory_row"
            ),
            reason_code=(
                "local_auditor_kept_pending"
                if local_state == "pending"
                else "local_auditor_closed_candidate"
            ),
            source_artifact_hash=inventory_hash,
            source_kind="local_audited_inventory",
        )
        result.append(
            _new_case(
                state_lineage_id=lineage,
                case_type="entity_identity",
                subject_refs=[prior_card_id],
                surface=surface,
                disputed_field="identity_membership",
                status=(
                    "collecting_evidence"
                    if local_state == "pending"
                    else "dismissed_dormant"
                ),
                evidence=evidence,
            )
        )

    for offset, row in enumerate(inventory.get("unresolved_referents") or []):
        if not isinstance(row, Mapping):
            raise ReviewCaseLedgerError("unresolved referent must be an object")
        source_id = str(row.get("candidate_id") or f"unresolved_{chapter_id}_{offset}")
        prior_card_id = source_to_prior.get(source_id, source_id)
        source_ids = [
            _required_string(value, "unresolved source block")
            for value in (
                row.get("source_block_ids")
                or row.get("surface_match_block_ids")
                or []
            )
        ]
        evidence = _evidence_row(
            chapter_id=chapter_id,
            source_block_ids=source_ids,
            observation="pending_inventory_row",
            reason_code=str(row.get("issue") or "unresolved_referent"),
            source_artifact_hash=inventory_hash,
            source_kind="local_unresolved_referent",
        )
        result.append(
            _new_case(
                state_lineage_id=lineage,
                case_type="unresolved_referent",
                subject_refs=[prior_card_id],
                surface=_required_string(row.get("surface"), "unresolved surface"),
                disputed_field="identity_membership",
                status="collecting_evidence",
                evidence=evidence,
                evidence_needed=["explicit_naming_or_referent_attribution"],
            )
        )

    for table, status in (
        ("pending_glossary_candidates", "collecting_evidence"),
        ("dormant_glossary_candidates", "dismissed_dormant"),
    ):
        rows = inventory.get(table) or []
        if not isinstance(rows, list):
            raise ReviewCaseLedgerError(f"{table} must be a list")
        for row in rows:
            if not isinstance(row, Mapping):
                raise ReviewCaseLedgerError(f"{table} row must be an object")
            source_id = _required_string(row.get("candidate_id"), "glossary candidate_id")
            subject = source_to_glossary.get(source_id, source_id)
            source_ids = [
                _required_string(value, "glossary source block")
                for value in (
                    row.get("source_block_ids")
                    or row.get("surface_match_block_ids")
                    or []
                )
            ]
            evidence = _evidence_row(
                chapter_id=chapter_id,
                source_block_ids=source_ids,
                observation=(
                    "pending_inventory_row"
                    if status == "collecting_evidence"
                    else "dormant_inventory_row"
                ),
                reason_code=(
                    "glossary_pending_evidence"
                    if status == "collecting_evidence"
                    else "glossary_rejected_dormant"
                ),
                source_artifact_hash=inventory_hash,
                source_kind="local_glossary_audit",
            )
            result.append(
                _new_case(
                    state_lineage_id=lineage,
                    case_type="glossary_sense",
                    subject_refs=[subject],
                    surface=_required_string(row.get("surface"), "glossary surface"),
                    disputed_field="local_sense",
                    status=status,
                    evidence=evidence,
                    evidence_needed=["distinct_translation_sensitive_local_sense"],
                )
            )

    for row in inventory.get("deferred_source_repairs") or []:
        if not isinstance(row, Mapping):
            raise ReviewCaseLedgerError("deferred source repair must be an object")
        source_id = _required_string(row.get("candidate_id"), "repair candidate_id")
        subject = source_to_prior.get(source_id) or source_to_glossary.get(source_id) or source_id
        source_ids = [
            str(value)
            for value in (
                row.get("surface_match_block_ids")
                or row.get("source_block_ids")
                or []
            )
            if isinstance(value, str) and value
        ]
        evidence = _evidence_row(
            chapter_id=chapter_id,
            source_block_ids=source_ids,
            observation="technical_failure",
            reason_code="pending_source_repair",
            source_artifact_hash=inventory_hash,
            source_kind="source_address_validator",
        )
        result.append(
            _new_case(
                state_lineage_id=lineage,
                case_type="source_repair",
                subject_refs=[subject],
                surface=str(
                    row.get("canonical_surface")
                    or row.get("surface")
                    or source_id
                ),
                disputed_field="source_address",
                status="technical_repair",
                evidence=evidence,
            )
        )
    return result


def _challenge_cases(
    *,
    chapter_id: str,
    prefix: Mapping[str, Any],
    challenge: Mapping[str, Any],
) -> list[dict[str, Any]]:
    lineage = prefix["state_lineage_id"]
    artifact_hash = _hash_string(
        challenge.get("prior_challenge_artifact_hash"),
        "prior challenge artifact hash",
    )
    result: list[dict[str, Any]] = []
    for row in challenge.get("prior_enrichment_requests") or []:
        if not isinstance(row, Mapping):
            raise ReviewCaseLedgerError("prior enrichment must be an object")
        evidence = _evidence_row(
            chapter_id=chapter_id,
            source_block_ids=_string_list(
                row.get("source_block_ids"), "enrichment source blocks"
            ),
            observation="prior_enrichment_proposed",
            reason_code="b1_proposed_stable_surface",
            source_artifact_hash=artifact_hash,
            source_kind="b1_prior_enrichment",
        )
        result.append(
            _new_case(
                state_lineage_id=lineage,
                case_type="prior_enrichment",
                subject_refs=[
                    _required_string(row.get("prior_card_id"), "prior_card_id")
                ],
                surface=_required_string(row.get("surface"), "enrichment surface"),
                disputed_field="alias_scope",
                status="ready_for_review",
                evidence=evidence,
                evidence_needed=["bounded_source_evidence"],
            )
        )
    return result


def _legacy_review_cases(
    *,
    prefix: Mapping[str, Any],
    review_ledger: Mapping[str, Any],
) -> list[dict[str, Any]]:
    lineage = prefix["state_lineage_id"]
    cards = _prior_cards(prefix)
    verified = verify_chapter_cycle_review_ledger_v1(review_ledger)
    result: list[dict[str, Any]] = []
    for row in verified["review_items"]:
        if row["lifecycle_state"] == "closed":
            continue
        subject_ids = list(row["subject_prior_card_ids"])
        surface = str(row.get("surface_key") or "")
        if not surface:
            surface = next(
                (
                    str(cards[card_id]["canonical_surface"])
                    for card_id in subject_ids
                    if card_id in cards
                ),
                subject_ids[0],
            )
        case_type = (
            "stable_claim"
            if row["route"] == "stable_claim_rehearing"
            else "entity_identity"
        )
        status = (
            "ready_for_review"
            if row["lifecycle_state"] == "queued"
            else "book_end_pending"
            if row["lifecycle_state"] == "book_end_pending"
            else "collecting_evidence"
        )
        evidence = _evidence_row(
            chapter_id=row["chapter_id"],
            source_block_ids=row["source_block_ids"],
            observation="legacy_review_lead",
            reason_code=row["reason_code"],
            source_artifact_hash=row["source_artifact_hash"],
            source_kind=row["source_kind"],
        )
        result.append(
            _new_case(
                state_lineage_id=lineage,
                case_type=case_type,
                subject_refs=subject_ids,
                surface=surface,
                disputed_field=row.get("disputed_field"),
                status=status,
                evidence=evidence,
            )
        )
    return result


def _priority_cases(
    *,
    prefix: Mapping[str, Any],
    priority_review_index: Mapping[str, Any],
) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    source_hash = _hash_string(
        priority_review_index.get("priority_review_index_hash"),
        "priority review index hash",
    )
    for row in priority_review_index.get("review_leads") or []:
        if not isinstance(row, Mapping):
            raise ReviewCaseLedgerError("priority review lead must be an object")
        chapter_ids = _string_list(
            row.get("chapter_ids"),
            "priority review chapter_ids",
        )
        subjects = [
            str(value)
            for value in row.get("subject_prior_card_ids") or []
            if isinstance(value, str) and value
        ]
        if not subjects:
            subjects = [_required_string(row.get("lead_id"), "priority lead_id")]
        evidence = _evidence_row(
            chapter_id=chapter_ids[-1],
            source_block_ids=row.get("source_block_ids") or [],
            observation="priority_review_lead",
            reason_code="+".join(row.get("trigger_kinds") or ["priority_review"]),
            source_artifact_hash=source_hash,
            source_kind="chapter_priority_review",
        )
        result.append(
            _new_case(
                state_lineage_id=prefix["state_lineage_id"],
                case_type="importance_recall",
                subject_refs=subjects,
                surface=str(row.get("surface_key") or row.get("lead_id")),
                disputed_field="identity_membership",
                status="book_end_pending",
                evidence=evidence,
                evidence_needed=["whole_book_priority_recurrence"],
            )
        )
    return result


def _apply_review_observations(
    *,
    cases: dict[str, dict[str, Any]],
    chapter_id: str,
    challenge: Mapping[str, Any],
) -> None:
    artifact_hash = _hash_string(
        challenge.get("prior_challenge_artifact_hash"),
        "prior challenge artifact hash",
    )
    seen: set[str] = set()
    for row in challenge.get("review_case_observations") or []:
        if not isinstance(row, Mapping):
            raise ReviewCaseLedgerError("review case observation must be an object")
        case_id = _required_string(row.get("review_case_id"), "review_case_id")
        if case_id in seen or case_id not in cases:
            raise ReviewCaseLedgerError(
                "review case observation targets a foreign or duplicate case"
            )
        seen.add(case_id)
        observation = _required_string(row.get("observation"), "observation")
        if observation not in {
            "supports",
            "conflicts",
            "not_same_referent",
            "ambiguous",
            "no_new_evidence",
        }:
            raise ReviewCaseLedgerError("review observation is foreign")
        evidence = _evidence_row(
            chapter_id=chapter_id,
            source_block_ids=_string_list(
                row.get("source_block_ids"), "review observation source blocks"
            ),
            observation=observation,
            reason_code=_required_string(row.get("reason"), "review observation reason"),
            source_artifact_hash=artifact_hash,
            source_kind="b1_review_case_observation",
        )
        prior_hashes = {
            item["evidence_content_hash"]
            for item in cases[case_id]["evidence_history"]
        }
        cases[case_id] = _append_evidence(cases[case_id], evidence)
        if evidence["evidence_content_hash"] in prior_hashes:
            continue
        if observation in {"supports", "conflicts", "not_same_referent"}:
            cases[case_id]["status"] = "ready_for_review"
            actor, trigger, authority, needed = _lifecycle_defaults(
                status="ready_for_review", case_type=cases[case_id]["case_type"]
            )
            cases[case_id]["next_actor"] = actor
            cases[case_id]["reopen_trigger"] = trigger
            cases[case_id]["authority_effect"] = authority
            cases[case_id]["evidence_needed"] = needed
        elif observation == "ambiguous":
            cases[case_id]["status"] = "collecting_evidence"
            cases[case_id]["next_actor"] = "b1"
            cases[case_id]["reopen_trigger"] = "new_material_evidence"
            cases[case_id]["authority_effect"] = "retrieval_only"


def build_review_case_ledger_v1(
    *,
    document: Mapping[str, Any],
    chapter_id: str,
    prefix_bundle: Mapping[str, Any],
    audited_inventory: Mapping[str, Any],
    previous_ledger: Mapping[str, Any] | None = None,
    prior_challenge_artifact: Mapping[str, Any] | None = None,
    chapter_review_ledger: Mapping[str, Any] | None = None,
    priority_review_index: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Extend the review-case ledger with one completed chapter."""

    prefix = verify_chapter_prefix_prior_bundle_v1(
        prefix_bundle, document=document
    )
    if prefix["coverage_through_chapter_id"] != chapter_id:
        raise ReviewCaseLedgerError("review-case prefix coverage is stale")
    inventory_hash = _verified_artifact_hash(
        audited_inventory,
        hash_field="conflict_audited_inventory_hash",
        label="conflict audited inventory",
    )
    cases: dict[str, dict[str, Any]] = {}
    observed_artifacts: list[str] = []
    if previous_ledger is not None:
        previous = verify_review_case_ledger_v1(previous_ledger)
        if previous["state_lineage_id"] != prefix["state_lineage_id"]:
            raise ReviewCaseLedgerError("review-case ledger crosses state lineage")
        cases = {
            row["review_case_id"]: _clone(row)
            for row in previous["review_cases"]
        }
        observed_artifacts = list(previous["observed_artifact_hashes"])
    for case in _inventory_cases(
        document=document,
        chapter_id=chapter_id,
        prefix=prefix,
        inventory=audited_inventory,
    ):
        _put_case(cases, case)
    observed_artifacts.append(inventory_hash)

    if prior_challenge_artifact is not None:
        challenge_hash = _verified_artifact_hash(
            prior_challenge_artifact,
            hash_field="prior_challenge_artifact_hash",
            label="prior challenge artifact",
        )
        for case in _challenge_cases(
            chapter_id=chapter_id,
            prefix=prefix,
            challenge=prior_challenge_artifact,
        ):
            _put_case(cases, case)
        _apply_review_observations(
            cases=cases,
            chapter_id=chapter_id,
            challenge=prior_challenge_artifact,
        )
        observed_artifacts.append(challenge_hash)
    if chapter_review_ledger is not None:
        for case in _legacy_review_cases(
            prefix=prefix, review_ledger=chapter_review_ledger
        ):
            _put_case(cases, case)
        observed_artifacts.append(
            _hash_string(
                chapter_review_ledger.get("review_ledger_hash"),
                "chapter review ledger hash",
            )
        )
    if priority_review_index is not None:
        priority_hash = _verified_artifact_hash(
            priority_review_index,
            hash_field="priority_review_index_hash",
            label="priority review index",
        )
        for case in _priority_cases(
            prefix=prefix, priority_review_index=priority_review_index
        ):
            _put_case(cases, case)
        observed_artifacts.append(priority_hash)

    body = {
        "schema_version": LEDGER_SCHEMA_VERSION,
        "validator_version": LEDGER_VALIDATOR_VERSION,
        "state_lineage_id": prefix["state_lineage_id"],
        "coverage_through_chapter_id": chapter_id,
        "observed_artifact_hashes": sorted(set(observed_artifacts)),
        "review_cases": sorted(cases.values(), key=lambda row: row["review_case_id"]),
        "production_publish_performed": False,
    }
    return verify_review_case_ledger_v1(
        {**body, "review_case_ledger_hash": canonical_hash(body)}
    )


def _compact_card(card: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "prior_card_id": card["prior_card_id"],
        "canonical_surface": card["canonical_surface"],
        "stable_surfaces": _clone(card.get("stable_surfaces") or []),
        "effective_claims": _clone(card.get("effective_claims") or {}),
        "authority_scope": card.get("authority_scope"),
        "first_supported_block_id": card.get("first_supported_block_id"),
    }


def select_relevant_review_cases_v1(
    *,
    ledger: Mapping[str, Any],
    chapter: Mapping[str, Any],
    prefix_bundle: Mapping[str, Any],
    max_cases: int = DEFAULT_MAX_RELEVANT_CASES,
) -> dict[str, Any]:
    """Select only B1-owned cases whose surface occurs in the current chapter."""

    verified = verify_review_case_ledger_v1(ledger)
    prefix = verify_chapter_prefix_prior_bundle_v1(prefix_bundle)
    if verified["state_lineage_id"] != prefix["state_lineage_id"]:
        raise ReviewCaseLedgerError("review-case selection crosses state lineage")
    if max_cases < 0:
        raise ReviewCaseLedgerError("review-case selection cap is negative")
    chapter_id = _required_string(chapter.get("chapter_id"), "chapter_id")
    blocks = sorted(
        chapter.get("blocks") or [],
        key=lambda row: (
            int(row.get("order_index") or 0),
            str(row.get("block_id") or ""),
        ),
    )
    cards = _prior_cards(prefix)
    packets: list[dict[str, Any]] = []
    for case in verified["review_cases"]:
        if case["next_actor"] != "b1" or case["status"] in TERMINAL_STATUSES:
            continue
        surface = case["surface"]
        hit_ids = [
            _required_string(block.get("block_id"), "block_id")
            for block in blocks
            if isinstance(block, Mapping)
            and _contains_surface(_block_text(block), surface)
        ]
        if not hit_ids:
            continue
        subject_cards = [
            _compact_card(cards[card_id])
            for card_id in case["subject_refs"]
            if card_id in cards
        ]
        body = {
            "review_case_id": case["review_case_id"],
            "case_type": case["case_type"],
            "surface": surface,
            "status": case["status"],
            "authority_effect": case["authority_effect"],
            "disputed_field": case.get("disputed_field"),
            "evidence_needed": _clone(case["evidence_needed"]),
            "hearing_count": case["hearing_count"],
            "automatic_hearing_limit": case["deadline"][
                "automatic_hearing_limit"
            ],
            "current_surface_hit_block_ids": hit_ids,
            "subject_cards": subject_cards,
        }
        packets.append({**body, "packet_hash": canonical_hash(body)})
    packets.sort(key=lambda row: row["review_case_id"])
    overflow = max(0, len(packets) - max_cases)
    selected = packets[:max_cases]
    manifest_body = {
        "schema_version": PACKET_SCHEMA_VERSION,
        "chapter_id": chapter_id,
        "review_case_ledger_hash": verified["review_case_ledger_hash"],
        "packets": selected,
        "overflow_count": overflow,
    }
    return {
        **manifest_body,
        "review_case_manifest_hash": canonical_hash(manifest_body),
    }


def verify_relevant_review_case_packet_v1(
    packet: Mapping[str, Any],
    *,
    expected_chapter_id: str | None = None,
) -> dict[str, Any]:
    if packet.get("schema_version") != PACKET_SCHEMA_VERSION:
        raise ReviewCaseLedgerError("foreign relevant review-case packet schema")
    body = dict(packet)
    observed = _hash_string(
        body.pop("review_case_manifest_hash", None),
        "review_case_manifest_hash",
    )
    if canonical_hash(body) != observed:
        raise ReviewCaseLedgerError("relevant review-case packet hash mismatch")
    chapter_id = _required_string(packet.get("chapter_id"), "chapter_id")
    if expected_chapter_id is not None and chapter_id != expected_chapter_id:
        raise ReviewCaseLedgerError("relevant review-case packet targets another chapter")
    _hash_string(
        packet.get("review_case_ledger_hash"),
        "review_case_ledger_hash",
    )
    overflow = packet.get("overflow_count")
    if not isinstance(overflow, int) or overflow < 0:
        raise ReviewCaseLedgerError("review-case packet overflow count is invalid")
    rows = packet.get("packets")
    if not isinstance(rows, list):
        raise ReviewCaseLedgerError("review-case packets must be a list")
    seen: set[str] = set()
    for row in rows:
        if not isinstance(row, Mapping):
            raise ReviewCaseLedgerError("relevant review-case row must be an object")
        row_body = dict(row)
        packet_hash = _hash_string(
            row_body.pop("packet_hash", None), "review-case packet hash"
        )
        if canonical_hash(row_body) != packet_hash:
            raise ReviewCaseLedgerError("relevant review-case row hash mismatch")
        case_id = _required_string(row.get("review_case_id"), "review_case_id")
        if case_id in seen:
            raise ReviewCaseLedgerError("relevant review-case packet repeats a case")
        seen.add(case_id)
        if row.get("case_type") not in CASE_TYPES:
            raise ReviewCaseLedgerError("relevant review-case type is foreign")
        if row.get("status") not in CASE_STATUSES:
            raise ReviewCaseLedgerError("relevant review-case status is foreign")
        if row.get("authority_effect") not in AUTHORITY_EFFECTS:
            raise ReviewCaseLedgerError("relevant review-case authority is foreign")
        _required_string(row.get("surface"), "review-case surface")
        _string_list(
            row.get("current_surface_hit_block_ids"),
            "current_surface_hit_block_ids",
            sorted_unique=False,
        )
        _string_list(
            row.get("evidence_needed"),
            "review-case evidence_needed",
            allow_empty=False,
            sorted_unique=True,
        )
        cards = row.get("subject_cards")
        if not isinstance(cards, list):
            raise ReviewCaseLedgerError("review-case subject cards must be a list")
        if not isinstance(row.get("hearing_count"), int) or row["hearing_count"] < 0:
            raise ReviewCaseLedgerError("review-case hearing count is invalid")
        if (
            not isinstance(row.get("automatic_hearing_limit"), int)
            or row["automatic_hearing_limit"] < 1
        ):
            raise ReviewCaseLedgerError("review-case hearing limit is invalid")
    return _clone(dict(packet))


def project_ready_cases_to_chapter_review_ledger_v1(
    *,
    case_ledger: Mapping[str, Any],
    chapter_review_ledger: Mapping[str, Any],
) -> dict[str, Any]:
    """Project ready Identity/Surface cases into the existing bounded Auditor lane."""

    cases = verify_review_case_ledger_v1(case_ledger)
    review = verify_chapter_cycle_review_ledger_v1(chapter_review_ledger)
    if cases["state_lineage_id"] != review["state_lineage_id"]:
        raise ReviewCaseLedgerError("review-case projection crosses state lineage")
    by_id = {
        row["review_item_id"]: _clone(row) for row in review["review_items"]
    }
    for case in cases["review_cases"]:
        if (
            case["status"] != "ready_for_review"
            or case["next_actor"] != "identity_auditor"
        ):
            continue
        evidence = case["evidence_history"][-1]
        source_artifact_hash = canonical_hash(
            {
                "review_case_id": case["review_case_id"],
                "evidence_hash": evidence["evidence_hash"],
            }
        )
        row = {
            "state_lineage_id": cases["state_lineage_id"],
            "chapter_id": evidence["chapter_id"],
            "source_kind": "review_case_ledger",
            "route": "book_identity_auditor",
            "subject_prior_card_ids": list(case["subject_refs"]),
            "disputed_field": case["disputed_field"],
            "source_block_ids": list(evidence["source_block_ids"]),
            "evidence_manifest_hash": evidence["evidence_hash"],
            "lifecycle_state": "queued",
            "authority_effect": "none",
            "reason_code": f"review_case_{case['case_type']}_ready",
            "source_artifact_hash": source_artifact_hash,
            "reopen_classification": {
                "route": "new_material_evidence",
                "reason": evidence["observation"],
            },
            "review_case_id": case["review_case_id"],
            "surface_key": case["surface_key"],
            "surface": case["surface"],
        }
        identity = {
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
        item = {
            "review_item_id": "cycrev1_" + canonical_hash(identity)[:20],
            **row,
        }
        prior = by_id.get(item["review_item_id"])
        if prior is not None and prior != item:
            raise ReviewCaseLedgerError(
                "projected review item id collides with unequal bytes"
            )
        by_id[item["review_item_id"]] = item
    body = {
        key: _clone(value)
        for key, value in review.items()
        if key not in {"review_ledger_hash", "review_items"}
    }
    body["review_items"] = sorted(by_id.values(), key=lambda row: row["review_item_id"])
    result = {**body, "review_ledger_hash": canonical_hash(body)}
    return verify_chapter_cycle_review_ledger_v1(result)


def apply_identity_surface_decisions_to_review_cases_v1(
    *,
    case_ledger: Mapping[str, Any],
    chapter_review_ledger: Mapping[str, Any],
    identity_ledger: Mapping[str, Any],
) -> dict[str, Any]:
    """Apply only typed surface-scope decisions to the unified lifecycle."""

    from pipeline.literary.incremental_identity_auditor_v1 import (
        verify_incremental_identity_ledger_v1,
    )

    cases = verify_review_case_ledger_v1(case_ledger)
    review = verify_chapter_cycle_review_ledger_v1(chapter_review_ledger)
    identity = verify_incremental_identity_ledger_v1(identity_ledger)
    if not (
        cases["state_lineage_id"]
        == review["state_lineage_id"]
        == identity["state_lineage_id"]
    ):
        raise ReviewCaseLedgerError("surface decision crosses state lineage")
    review_to_case = {
        row["review_item_id"]: row.get("review_case_id")
        for row in review["review_items"]
        if row.get("review_case_id")
    }
    rows = {
        row["review_case_id"]: _clone(row) for row in cases["review_cases"]
    }
    for decision in identity["decision_history"]:
        for action in decision.get("surface_scope_actions") or []:
            review_id = action["review_item_id"]
            case_id = review_to_case.get(review_id)
            if case_id is None or case_id not in rows:
                continue
            case = rows[case_id]
            decision_entry = {
                "decision_hash": decision["decision_hash"],
                "review_item_id": review_id,
                "hearing_number": decision["hearing_number"],
                "action": action["action"],
                "target_prior_card_id": action.get("target_prior_card_id"),
                "valid_block_ids": _clone(action.get("valid_block_ids") or []),
                "source_block_ids": _clone(action.get("source_block_ids") or []),
                "evidence_needed": action.get("evidence_needed"),
                "resolution_note": action["resolution_note"],
            }
            entry_hash = canonical_hash(decision_entry)
            if any(
                row.get("decision_entry_hash") == entry_hash
                for row in case["decision_history"]
            ):
                continue
            case["decision_history"].append(
                {**decision_entry, "decision_entry_hash": entry_hash}
            )
            case["hearing_count"] += 1
            case["last_heard_chapter_id"] = identity[
                "coverage_through_chapter_id"
            ]
            action_name = action["action"]
            if action_name == "confirm_block_scope":
                case["status"] = "block_scoped"
            elif action_name == "confirm_chapter_scope":
                case["status"] = "chapter_scoped"
            elif action_name == "nominate_book_candidate":
                case["status"] = "book_candidate"
            elif action_name == "dismiss_dormant":
                case["status"] = "dismissed_dormant"
            elif action_name == "keep_pending":
                case["status"] = (
                    "book_end_pending"
                    if case["hearing_count"]
                    >= case["deadline"]["automatic_hearing_limit"]
                    else "collecting_evidence"
                )
            else:
                raise ReviewCaseLedgerError("surface decision action is foreign")
            actor, trigger, authority, needed = _lifecycle_defaults(
                status=case["status"], case_type=case["case_type"]
            )
            case["next_actor"] = actor
            case["reopen_trigger"] = trigger
            case["authority_effect"] = authority
            case["evidence_needed"] = (
                [action["evidence_needed"]]
                if action_name == "keep_pending" and action.get("evidence_needed")
                else needed
            )
            rows[case_id] = case
    state_by_review_id = {
        review_id: state
        for state in identity["component_states"]
        for review_id in state.get("review_item_ids") or []
    }
    decision_by_hash = {
        row["decision_hash"]: row for row in identity["decision_history"]
    }
    for review_id, case_id in review_to_case.items():
        if case_id not in rows:
            continue
        case = rows[case_id]
        if case.get("disputed_field") in {"alias_scope", "alias_target"}:
            continue
        state = state_by_review_id.get(review_id)
        if state is None:
            continue
        decision = decision_by_hash[state["latest_decision_hash"]]
        entry_body = {
            "decision_hash": decision["decision_hash"],
            "review_item_id": review_id,
            "hearing_number": decision["hearing_number"],
            "action": "keep_pending" if state["status"] == "pending" else state["status"],
            "target_prior_card_id": None,
            "valid_block_ids": [],
            "source_block_ids": sorted(
                {
                    block_id
                    for action in decision["candidate_actions"]
                    for block_id in action["source_block_ids"]
                }
            ),
            "evidence_needed": (
                "identity_resolution" if state["status"] == "pending" else None
            ),
            "resolution_note": "Identity hearing applied to the review-case lifecycle.",
        }
        entry_hash = canonical_hash(entry_body)
        if any(
            row.get("decision_entry_hash") == entry_hash
            for row in case["decision_history"]
        ):
            continue
        case["decision_history"].append(
            {**entry_body, "decision_entry_hash": entry_hash}
        )
        case["hearing_count"] += 1
        case["last_heard_chapter_id"] = identity["coverage_through_chapter_id"]
        if state["status"] == "pending":
            case["status"] = (
                "book_end_pending"
                if case["hearing_count"]
                >= case["deadline"]["automatic_hearing_limit"]
                else "collecting_evidence"
            )
            actor, trigger, authority, _needed = _lifecycle_defaults(
                status=case["status"], case_type=case["case_type"]
            )
            case["next_actor"] = actor
            case["reopen_trigger"] = trigger
            case["authority_effect"] = authority
            case["evidence_needed"] = ["identity_resolution"]
        else:
            case["status"] = "closed"
            case["next_actor"] = "none"
            case["reopen_trigger"] = "none"
            case["authority_effect"] = "none"
            case["evidence_needed"] = []
        rows[case_id] = case
    body = {
        key: _clone(value)
        for key, value in cases.items()
        if key not in {"review_case_ledger_hash", "review_cases"}
    }
    body["coverage_through_chapter_id"] = identity["coverage_through_chapter_id"]
    body["review_cases"] = sorted(rows.values(), key=lambda row: row["review_case_id"])
    return verify_review_case_ledger_v1(
        {**body, "review_case_ledger_hash": canonical_hash(body)}
    )


def finalize_review_case_ledger_v1(
    ledger: Mapping[str, Any],
) -> dict[str, Any]:
    """Move every still-unresolved semantic case to explicit book-end ownership."""

    verified = verify_review_case_ledger_v1(ledger)
    rows: list[dict[str, Any]] = []
    for source in verified["review_cases"]:
        row = _clone(source)
        if row["status"] == "book_candidate":
            row["next_actor"] = "book_end_auditor"
            row["reopen_trigger"] = "book_end"
            row["evidence_needed"] = ["whole_book_scope_evidence"]
        elif row["status"] in {"block_scoped", "chapter_scoped"}:
            row["next_actor"] = "none"
            row["reopen_trigger"] = "none"
            row["evidence_needed"] = []
        elif row["status"] == "dismissed_dormant":
            row["status"] = "closed"
            row["next_actor"] = "none"
            row["reopen_trigger"] = "none"
            row["authority_effect"] = "none"
            row["evidence_needed"] = []
        elif row["status"] not in TERMINAL_STATUSES and row["status"] not in {
            "block_scoped",
            "chapter_scoped",
        }:
            row["status"] = "book_end_pending"
            actor, trigger, authority, needed = _lifecycle_defaults(
                status="book_end_pending", case_type=row["case_type"]
            )
            row["next_actor"] = actor
            row["reopen_trigger"] = trigger
            row["authority_effect"] = authority
            row["evidence_needed"] = needed
        rows.append(row)
    body = {
        key: _clone(value)
        for key, value in verified.items()
        if key not in {"review_case_ledger_hash", "review_cases"}
    }
    body["review_cases"] = sorted(rows, key=lambda row: row["review_case_id"])
    body["book_end_finalized"] = True
    body["production_publish_performed"] = False
    return verify_review_case_ledger_v1(
        {**body, "review_case_ledger_hash": canonical_hash(body)}
    )


def verify_review_case_ledger_v1(
    ledger: Mapping[str, Any],
) -> dict[str, Any]:
    if ledger.get("schema_version") != LEDGER_SCHEMA_VERSION:
        raise ReviewCaseLedgerError("foreign review-case ledger schema")
    if ledger.get("validator_version") != LEDGER_VALIDATOR_VERSION:
        raise ReviewCaseLedgerError("review-case validator mismatch")
    body = dict(ledger)
    observed = _hash_string(
        body.pop("review_case_ledger_hash", None), "review_case_ledger_hash"
    )
    if canonical_hash(body) != observed:
        raise ReviewCaseLedgerError("review-case ledger hash mismatch")
    _hash_string(ledger.get("state_lineage_id"), "state_lineage_id")
    if ledger.get("production_publish_performed") is not False:
        raise ReviewCaseLedgerError("review-case ledger claims publication")
    allowed_ledger_keys = {
        "schema_version",
        "validator_version",
        "state_lineage_id",
        "coverage_through_chapter_id",
        "observed_artifact_hashes",
        "review_cases",
        "production_publish_performed",
        "review_case_ledger_hash",
        "book_end_finalized",
    }
    if not set(ledger).issubset(allowed_ledger_keys):
        raise ReviewCaseLedgerError("review-case ledger contains foreign fields")
    book_end_finalized = ledger.get("book_end_finalized", False)
    if not isinstance(book_end_finalized, bool):
        raise ReviewCaseLedgerError("book-end finalization flag is malformed")
    _required_string(
        ledger.get("coverage_through_chapter_id"),
        "coverage_through_chapter_id",
    )
    _string_list(
        ledger.get("observed_artifact_hashes"),
        "observed_artifact_hashes",
        allow_empty=True,
        sorted_unique=True,
    )
    rows = ledger.get("review_cases")
    if not isinstance(rows, list):
        raise ReviewCaseLedgerError("review_cases must be a list")
    seen: set[str] = set()
    expected_case_keys = {
        "review_case_id",
        "state_lineage_id",
        "case_type",
        "subject_refs",
        "surface_key",
        "disputed_field",
        "surface",
        "status",
        "authority_effect",
        "next_actor",
        "reopen_trigger",
        "evidence_needed",
        "hearing_count",
        "deadline",
        "evidence_history",
        "decision_history",
        "last_seen_chapter_id",
        "last_heard_chapter_id",
    }
    for row in rows:
        if not isinstance(row, Mapping):
            raise ReviewCaseLedgerError("review case must be an object")
        if set(row) != expected_case_keys:
            raise ReviewCaseLedgerError("review case shape is not closed")
        case_id = _required_string(row.get("review_case_id"), "review_case_id")
        if case_id in seen:
            raise ReviewCaseLedgerError("review-case ledger repeats a case id")
        seen.add(case_id)
        if row.get("case_type") not in CASE_TYPES:
            raise ReviewCaseLedgerError("review case has a foreign type")
        if row.get("status") not in CASE_STATUSES:
            raise ReviewCaseLedgerError("review case has a foreign status")
        if row.get("next_actor") not in NEXT_ACTORS:
            raise ReviewCaseLedgerError("review case has a foreign next actor")
        if row.get("authority_effect") not in AUTHORITY_EFFECTS:
            raise ReviewCaseLedgerError("review case has a foreign authority effect")
        if row.get("reopen_trigger") not in REOPEN_TRIGGERS:
            raise ReviewCaseLedgerError("review case has a foreign reopen trigger")
        _string_list(row.get("subject_refs"), "subject_refs", sorted_unique=True)
        _required_string(row.get("surface"), "review case surface")
        _required_string(row.get("surface_key"), "review case surface_key")
        if row["surface_key"] != _surface_key(row["surface"]):
            raise ReviewCaseLedgerError("review case surface key is stale")
        if row.get("disputed_field") is not None:
            _required_string(row.get("disputed_field"), "disputed_field")
        _string_list(
            row.get("evidence_needed"), "evidence_needed", allow_empty=True,
            sorted_unique=True,
        )
        resolved_at_book_end = book_end_finalized and row["status"] in {
            "block_scoped",
            "chapter_scoped",
        }
        if row["status"] in TERMINAL_STATUSES or resolved_at_book_end:
            if row["next_actor"] != "none" or row["reopen_trigger"] != "none":
                raise ReviewCaseLedgerError("terminal review case still has an owner")
        elif row["next_actor"] == "none" or not row["evidence_needed"]:
            raise ReviewCaseLedgerError(
                "nonterminal review case lacks owner or evidence target"
            )
        _validate_lifecycle_tuple(
            row,
            book_end_finalized=book_end_finalized,
        )
        deadline = row.get("deadline")
        if not isinstance(deadline, Mapping) or set(deadline) != {
            "automatic_hearing_limit",
            "same_evidence_reopen_forbidden",
            "unresolved_after_limit",
        }:
            raise ReviewCaseLedgerError("review case deadline is malformed")
        if (
            not isinstance(deadline["automatic_hearing_limit"], int)
            or deadline["automatic_hearing_limit"] < 1
            or deadline["same_evidence_reopen_forbidden"] is not True
            or deadline["unresolved_after_limit"] != "book_end_pending"
        ):
            raise ReviewCaseLedgerError("review case deadline contract is invalid")
        if (
            not isinstance(row.get("hearing_count"), int)
            or row["hearing_count"] < 0
            or row["hearing_count"] > deadline["automatic_hearing_limit"]
        ):
            raise ReviewCaseLedgerError("review hearing count is invalid")
        evidence_rows = row.get("evidence_history")
        decisions = row.get("decision_history")
        if not isinstance(evidence_rows, list) or not evidence_rows:
            raise ReviewCaseLedgerError("review case lacks evidence history")
        if not isinstance(decisions, list):
            raise ReviewCaseLedgerError("review case decision history is malformed")
        expected_evidence_keys = {
            "chapter_id",
            "source_block_ids",
            "observation",
            "reason_code",
            "source_artifact_hash",
            "source_kind",
            "evidence_content_hash",
            "evidence_hash",
        }
        evidence_hashes: set[str] = set()
        evidence_content_hashes: set[str] = set()
        for evidence in evidence_rows:
            if not isinstance(evidence, Mapping):
                raise ReviewCaseLedgerError("review evidence must be an object")
            if set(evidence) != expected_evidence_keys:
                raise ReviewCaseLedgerError("review evidence shape is not closed")
            evidence_hash = _hash_string(
                evidence.get("evidence_hash"), "evidence_hash"
            )
            if evidence_hash in evidence_hashes:
                raise ReviewCaseLedgerError("review case repeats evidence")
            evidence_hashes.add(evidence_hash)
            content_hash = _hash_string(
                evidence.get("evidence_content_hash"),
                "evidence_content_hash",
            )
            if content_hash in evidence_content_hashes:
                raise ReviewCaseLedgerError(
                    "review case repeats immutable source evidence"
                )
            evidence_content_hashes.add(content_hash)
            if content_hash != canonical_hash(
                {
                    "chapter_id": evidence.get("chapter_id"),
                    "source_block_ids": evidence.get("source_block_ids"),
                }
            ):
                raise ReviewCaseLedgerError("review evidence content hash mismatch")
            evidence_body = {
                key: _clone(value)
                for key, value in evidence.items()
                if key != "evidence_hash"
            }
            if canonical_hash(evidence_body) != evidence_hash:
                raise ReviewCaseLedgerError("review evidence hash mismatch")
            _hash_string(
                evidence.get("source_artifact_hash"),
                "evidence source artifact hash",
            )
            _string_list(
                evidence.get("source_block_ids"),
                "evidence source_block_ids",
                allow_empty=True,
                sorted_unique=True,
            )
            if evidence.get("observation") not in OBSERVATIONS:
                raise ReviewCaseLedgerError("review evidence observation is foreign")
            _required_string(evidence.get("chapter_id"), "evidence chapter_id")
            _required_string(evidence.get("reason_code"), "evidence reason_code")
            _required_string(evidence.get("source_kind"), "evidence source_kind")
        if row["last_seen_chapter_id"] != evidence_rows[-1]["chapter_id"]:
            raise ReviewCaseLedgerError("review case last-seen chapter is stale")

        decision_hashes: set[str] = set()
        expected_decision_keys = {
            "decision_hash",
            "review_item_id",
            "hearing_number",
            "action",
            "target_prior_card_id",
            "valid_block_ids",
            "source_block_ids",
            "evidence_needed",
            "resolution_note",
            "decision_entry_hash",
        }
        for decision in decisions:
            if not isinstance(decision, Mapping) or set(decision) != expected_decision_keys:
                raise ReviewCaseLedgerError("review decision shape is not closed")
            entry_hash = _hash_string(
                decision.get("decision_entry_hash"),
                "decision_entry_hash",
            )
            if entry_hash in decision_hashes:
                raise ReviewCaseLedgerError("review case repeats a decision")
            decision_hashes.add(entry_hash)
            decision_body = {
                key: _clone(value)
                for key, value in decision.items()
                if key != "decision_entry_hash"
            }
            if canonical_hash(decision_body) != entry_hash:
                raise ReviewCaseLedgerError("review decision entry hash mismatch")
            _hash_string(decision.get("decision_hash"), "decision_hash")
            _required_string(decision.get("review_item_id"), "review_item_id")
            if (
                not isinstance(decision.get("hearing_number"), int)
                or decision["hearing_number"] < 1
            ):
                raise ReviewCaseLedgerError("review decision hearing is invalid")
            if decision.get("action") not in DECISION_ACTIONS:
                raise ReviewCaseLedgerError("review decision action is foreign")
            if decision.get("target_prior_card_id") is not None:
                _required_string(
                    decision.get("target_prior_card_id"),
                    "target_prior_card_id",
                )
            _string_list(
                decision.get("valid_block_ids"),
                "decision valid_block_ids",
                allow_empty=True,
                sorted_unique=True,
            )
            _string_list(
                decision.get("source_block_ids"),
                "decision source_block_ids",
                allow_empty=True,
                sorted_unique=True,
            )
            if decision.get("evidence_needed") is not None:
                _required_string(decision.get("evidence_needed"), "evidence_needed")
            _required_string(decision.get("resolution_note"), "resolution_note")
        if row["hearing_count"] != len(decisions):
            raise ReviewCaseLedgerError("review hearing count and history disagree")
        if decisions:
            _required_string(
                row.get("last_heard_chapter_id"),
                "last_heard_chapter_id",
            )
        elif row.get("last_heard_chapter_id") is not None:
            raise ReviewCaseLedgerError("unheard review case has a hearing chapter")
        expected = "litcase1_" + canonical_hash(_case_identity_body(row))[:20]
        if case_id != expected:
            raise ReviewCaseLedgerError("review case id is stale")
    return _clone(dict(ledger))


__all__ = [
    "AUTHORITY_EFFECTS",
    "CASE_STATUSES",
    "CASE_TYPES",
    "DEFAULT_AUTOMATIC_HEARING_LIMIT",
    "DEFAULT_MAX_RELEVANT_CASES",
    "LEDGER_SCHEMA_VERSION",
    "LEDGER_VALIDATOR_VERSION",
    "NEXT_ACTORS",
    "PACKET_SCHEMA_VERSION",
    "ReviewCaseLedgerError",
    "apply_identity_surface_decisions_to_review_cases_v1",
    "build_review_case_ledger_v1",
    "finalize_review_case_ledger_v1",
    "project_ready_cases_to_chapter_review_ledger_v1",
    "select_relevant_review_cases_v1",
    "verify_relevant_review_case_packet_v1",
    "verify_review_case_ledger_v1",
]
