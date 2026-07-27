"""Append-only ledger for cross-chapter Auditor decisions, and the reconciled
projection a later chapter reads instead of raw conflicting cards.

Nothing here decides identity.  A decision arrives already validated by the
bridge; this module records it against its exact component, queue, and registry
lineage, and then computes what follows mechanically.

Two rules shape everything:

* A card is never deleted.  A merge unions two records under one effective id
  and keeps both member ids visible, so the merge can be explained later and
  undone by removing the entry rather than by rebuilding the registry.
* A case that was answered stays answered.  ``confirmed_distinct`` is recorded
  so chapter after chapter does not re-open the same question, while
  ``insufficient_evidence`` stays visible as pending with its reason - answered
  and unanswered are different states, and neither is silence.
"""

from __future__ import annotations

from copy import deepcopy
from typing import Any, Iterable, Mapping, Sequence

from pipeline.literary.checkpoint import canonical_hash

LEDGER_SCHEMA_VERSION = "literary_b1_cross_chapter_decision_ledger_v1"
PROJECTION_SCHEMA_VERSION = "literary_b1_reconciled_projection_v1"

ENTRY_ID_PREFIX = "b1dec_"

# Verdicts that close a case, per question family.  Kept in one place so the
# projection cannot silently act on a verdict the bridge never allowed.
MERGE_VERDICTS = frozenset({"merge_referents", "alias_confirmed"})
DISTINCT_VERDICTS = frozenset({"confirmed_distinct", "alias_rejected_distinct"})
PENDING_VERDICTS = frozenset({"insufficient_evidence"})
OBSERVATION_VERDICTS = frozenset({"dismiss_observation", "keep_observation"})
STABLE_CLAIM_VERDICTS = frozenset(
    {
        "uphold_existing",
        "correction",
        "in_story_change",
        "reveal_only",
        "split_referent",
    }
)

KNOWN_VERDICTS = (
    MERGE_VERDICTS
    | DISTINCT_VERDICTS
    | PENDING_VERDICTS
    | OBSERVATION_VERDICTS
    | STABLE_CLAIM_VERDICTS
)


class B1DecisionLedgerError(ValueError):
    pass


# ---------------------------------------------------------------------------
# ledger
# ---------------------------------------------------------------------------


def empty_decision_ledger_v1(*, book_id: str) -> dict[str, Any]:
    """A ledger with no entries is a legal starting state, not a missing file."""

    body = {
        "schema_version": LEDGER_SCHEMA_VERSION,
        "book_id": _required_string(book_id, "book_id"),
        "entries": [],
    }
    return {**body, "ledger_hash": canonical_hash(body)}


def append_cross_chapter_decisions_v1(
    *,
    ledger: Mapping[str, Any],
    decisions: Sequence[Mapping[str, Any]],
    queue: Mapping[str, Any],
    registry: Mapping[str, Any],
) -> dict[str, Any]:
    """Append validated decisions; never rewrite or drop an existing entry.

    Every decision must name a component that exists in the supplied queue, and
    the queue must belong to the supplied registry.  A decision for a component
    already answered is refused: re-answering is a new component in a later
    chapter, not an edit of the record.
    """

    verify_decision_ledger_v1(ledger)
    queue_hash = _required_string(queue.get("queue_hash"), "queue_hash")
    registry_hash = _required_string(registry.get("registry_hash"), "registry_hash")
    if _required_string(queue.get("registry_hash"), "queue registry_hash") != registry_hash:
        raise B1DecisionLedgerError("hearing queue does not belong to this registry")

    components = {}
    for row in queue.get("components") or []:
        if not isinstance(row, Mapping):
            raise B1DecisionLedgerError("hearing queue component is malformed")
        components[_required_string(row.get("component_id"), "component_id")] = row

    entries = [deepcopy(dict(row)) for row in ledger["entries"]]
    answered = {row["component_id"] for row in entries}
    next_index = len(entries)

    for decision in decisions:
        if not isinstance(decision, Mapping):
            raise B1DecisionLedgerError("decision must be an object")
        component_id = _required_string(decision.get("component_id"), "component_id")
        component = components.get(component_id)
        if component is None:
            raise B1DecisionLedgerError(
                "decision cites a component absent from the supplied queue"
            )
        if component_id in answered:
            raise B1DecisionLedgerError(
                "component already has a decision; a later reconsideration is a new"
                " component, never an edit of an existing entry"
            )
        verdict = _required_string(decision.get("verdict"), "verdict")
        if verdict not in KNOWN_VERDICTS:
            raise B1DecisionLedgerError(f"unsupported verdict {verdict!r}")

        evidence = _validated_evidence(decision.get("evidence"))
        if verdict not in PENDING_VERDICTS and not evidence:
            raise B1DecisionLedgerError(
                "a verdict that closes a case must cite at least one evidence row"
            )

        prior_card_ids = _component_prior_card_ids(component)
        merge_target = decision.get("merge_target_prior_card_id")
        if verdict in MERGE_VERDICTS:
            if merge_target not in prior_card_ids:
                raise B1DecisionLedgerError(
                    "merge verdict must echo the prior card candidate supplied in the component"
                )
        elif merge_target is not None:
            raise B1DecisionLedgerError(
                "merge target is only meaningful for a merge verdict"
            )
        excluded_prior_card_ids = _validated_excluded_prior_card_ids(
            decision.get("excluded_prior_card_ids"),
            verdict=verdict,
            candidate_ids=prior_card_ids,
            evidence=evidence,
        )

        current_entity_ids = _component_current_entity_ids(component)
        body = {
            "chapter_id": _required_string(queue.get("chapter_id"), "chapter_id"),
            "component_id": component_id,
            "component_hash": canonical_hash(component),
            "question_type": component.get("question_type"),
            "review_route": component.get("review_route"),
            "prior_card_id": prior_card_ids[0] if len(prior_card_ids) == 1 else None,
            "prior_card_ids": prior_card_ids,
            # Keep the singular field for historical consumers while preserving
            # the complete current-side set that a clustered hearing weighed.
            "current_entity_id": (
                current_entity_ids[0] if len(current_entity_ids) == 1 else None
            ),
            "current_entity_ids": current_entity_ids,
            "verdict": verdict,
            "merge_target_prior_card_id": merge_target,
            "excluded_prior_card_ids": excluded_prior_card_ids,
            "evidence": evidence,
            "reason": _required_string(decision.get("reason"), "reason"),
            # Preserve the Auditor's explicit reopening condition.  Older
            # decisions may omit it; new hearing validators always provide it
            # for insufficient_evidence and null it for decisive verdicts.
            "resolution_condition": _validated_resolution_condition(
                decision.get("resolution_condition"), verdict
            ),
            "field_adjudications": _validated_field_adjudications(
                decision.get("field_adjudications")
            ),
            "queue_hash": queue_hash,
            "registry_hash": registry_hash,
        }
        entries.append(
            {
                "entry_id": ENTRY_ID_PREFIX + canonical_hash(body)[:20],
                "sequence_index": next_index,
                **body,
            }
        )
        answered.add(component_id)
        next_index += 1

    return _sealed_ledger(book_id=ledger["book_id"], entries=entries)


def verify_decision_ledger_v1(ledger: Mapping[str, Any]) -> dict[str, Any]:
    """Recompute the seal and every entry id, so a tampered row cannot pass."""

    if not isinstance(ledger, Mapping):
        raise B1DecisionLedgerError("ledger must be an object")
    if ledger.get("schema_version") != LEDGER_SCHEMA_VERSION:
        raise B1DecisionLedgerError("ledger schema version is unsupported")
    entries = ledger.get("entries")
    if not isinstance(entries, list):
        raise B1DecisionLedgerError("ledger entries must be a list")

    seen_components: set[str] = set()
    for index, row in enumerate(entries):
        if not isinstance(row, Mapping):
            raise B1DecisionLedgerError("ledger entry must be an object")
        if row.get("sequence_index") != index:
            raise B1DecisionLedgerError("ledger entry order was rewritten")
        component_id = _required_string(row.get("component_id"), "component_id")
        if component_id in seen_components:
            raise B1DecisionLedgerError("ledger holds two decisions for one component")
        seen_components.add(component_id)
        _entry_prior_card_ids(row)
        _entry_current_entity_ids(row)
        body = {
            key: value
            for key, value in row.items()
            if key not in {"entry_id", "sequence_index"}
        }
        expected = ENTRY_ID_PREFIX + canonical_hash(body)[:20]
        if row.get("entry_id") != expected:
            raise B1DecisionLedgerError("ledger entry content does not match its id")

    resealed = _sealed_ledger(
        book_id=_required_string(ledger.get("book_id"), "book_id"),
        entries=[deepcopy(dict(row)) for row in entries],
    )
    if resealed["ledger_hash"] != ledger.get("ledger_hash"):
        raise B1DecisionLedgerError("ledger hash does not match its content")
    return resealed


# ---------------------------------------------------------------------------
# reconciled projection
# ---------------------------------------------------------------------------


def project_reconciled_b1_registry_v1(
    *,
    registries: Sequence[Mapping[str, Any]],
    ledger: Mapping[str, Any],
) -> dict[str, Any]:
    """Compute the effective view a later chapter reads.

    Cards come from the sealed chapter registries; the ledger only says which
    of them are the same referent.  A card with no decision about it simply
    stands alone - absence of a hearing is not absence of an entity.
    """

    verify_decision_ledger_v1(ledger)
    cards: dict[str, dict[str, Any]] = {}
    card_anchor_chapter: dict[str, str] = {}
    card_snapshots: dict[str, list[tuple[str, dict[str, Any]]]] = {}
    card_chapters: dict[str, set[str]] = {}
    for registry in registries:
        chapter_id = _required_string(registry.get("chapter_id"), "chapter_id")
        for raw in registry.get("cards") or []:
            if not isinstance(raw, Mapping):
                raise B1DecisionLedgerError("registry card is malformed")
            entity_id = _required_string(raw.get("entity_id"), "entity_id")
            snapshot = deepcopy(dict(raw))
            # A persistent id may legitimately recur as the same referent's
            # chapter snapshot. Identity merges still require an Auditor entry;
            # this path only preserves history already bound to one id.
            card_snapshots.setdefault(entity_id, []).append((chapter_id, snapshot))
            card_chapters.setdefault(entity_id, set()).add(chapter_id)
            prior = cards.get(entity_id)
            prior_chapter = card_anchor_chapter.get(entity_id)
            if prior is None or (chapter_id, canonical_hash(snapshot)) < (
                _required_string(prior_chapter, "card anchor chapter"),
                canonical_hash(prior),
            ):
                cards[entity_id] = snapshot
                card_anchor_chapter[entity_id] = chapter_id

    # Every settled case travels with the blocks its verdict rested on.  That
    # set is the reopen key: a later builder must cite something outside it,
    # which stops a settled question costing a hearing every chapter without
    # ever sealing it shut against a genuine new finding.
    evidence_by_entry = {
        entry["entry_id"]: sorted(
            {row["block_id"] for row in entry.get("evidence") or []}
        )
        for entry in ledger["entries"]
    }

    union = _UnionFind(cards)
    resolved_distinct: list[dict[str, Any]] = []
    pending: list[dict[str, Any]] = []
    claim_adjudications: list[dict[str, Any]] = []
    dismissed_observations: list[dict[str, Any]] = []
    distinct_constraints = _distinct_constraints_v1(ledger["entries"])
    accepted_merges: list[tuple[str, str, str]] = []
    merge_outcomes: dict[str, dict[str, Any]] = {}

    for entry in ledger["entries"]:
        verdict = entry["verdict"]
        if verdict not in MERGE_VERDICTS:
            continue
        target = entry.get("merge_target_prior_card_id") or entry.get("prior_card_id")
        rights = _entry_current_entity_ids(entry)
        if target not in cards or not rights or any(right not in cards for right in rights):
            merge_outcomes[entry["entry_id"]] = {
                "state": "decision_not_applicable_here",
                "reason": "merge cites a card outside the supplied registries",
                "conflicting_entry_ids": [],
            }
            continue
        conflicts = _merge_group_distinct_conflicts_v1(
            union=union,
            member_ids=[target, *rights],
            constraints=distinct_constraints,
        )
        if conflicts:
            merge_outcomes[entry["entry_id"]] = {
                "state": "decision_conflict_unapplied",
                "reason": "merge would join a pair already recorded as distinct",
                "conflicting_entry_ids": sorted(conflicts),
            }
            continue
        for right in rights:
            union.join(target, right)
            accepted_merges.append((entry["entry_id"], target, right))
        merge_outcomes[entry["entry_id"]] = {
            "state": "applied",
            "reason": "merge is compatible with all recorded distinct constraints",
            "conflicting_entry_ids": [],
        }

    for entry in ledger["entries"]:
        verdict = entry["verdict"]
        candidates = _entry_prior_card_ids(entry)
        rights = _entry_current_entity_ids(entry)
        if verdict in MERGE_VERDICTS:
            outcome = merge_outcomes[entry["entry_id"]]
            if outcome["state"] != "applied":
                pending.append(
                    _unapplied(
                        entry,
                        outcome["reason"],
                        state=outcome["state"],
                        conflicting_entry_ids=outcome["conflicting_entry_ids"],
                    )
                )
            target = entry.get("merge_target_prior_card_id")
            for candidate in candidates:
                if candidate != target:
                    for right in rights:
                        resolved_distinct.append(
                            _resolved_distinct_row_v1(
                                entry=entry,
                                left=candidate,
                                right=right,
                                evidence_block_ids=evidence_by_entry[entry["entry_id"]],
                                finding="non_selected_candidate",
                            )
                        )
        elif verdict in DISTINCT_VERDICTS:
            for candidate in candidates:
                for right in rights:
                    resolved_distinct.append(
                        _resolved_distinct_row_v1(
                            entry=entry,
                            left=candidate,
                            right=right,
                            evidence_block_ids=evidence_by_entry[entry["entry_id"]],
                            finding="all_candidates_distinct",
                        )
                    )
        elif verdict in PENDING_VERDICTS:
            excluded = set(entry.get("excluded_prior_card_ids") or [])
            for candidate in candidates:
                for right in rights:
                    if candidate in excluded:
                        resolved_distinct.append(
                            _resolved_distinct_row_v1(
                                entry=entry,
                                left=candidate,
                                right=right,
                                evidence_block_ids=_exclusion_evidence_block_ids_v1(
                                    entry, candidate
                                ),
                                finding="partial_exclusion",
                            )
                        )
                        continue
                    pending.append(
                        {
                            "entry_id": entry["entry_id"],
                            "component_id": entry["component_id"],
                            "chapter_id": entry["chapter_id"],
                            "question_type": entry.get("question_type"),
                            "review_route": entry.get("review_route"),
                            "card_ids": sorted([candidate, right]),
                            "candidate_set": candidates,
                            "current_candidate_set": rights,
                            "excluded_prior_card_ids": sorted(excluded),
                            "state": "evidence_needed",
                            "evidence_block_ids": evidence_by_entry[entry["entry_id"]],
                            "reason": entry["reason"],
                            "resolution_condition": entry.get("resolution_condition"),
                        }
                    )
        elif verdict in OBSERVATION_VERDICTS:
            dismissed_observations.append(
                {
                    "entry_id": entry["entry_id"],
                    "component_id": entry["component_id"],
                    "verdict": verdict,
                    "card_ids": sorted([*candidates, *rights]),
                    "reason": entry["reason"],
                }
            )
        else:  # stable-claim family
            claim_adjudications.append(
                {
                    "entry_id": entry["entry_id"],
                    "component_id": entry["component_id"],
                    "chapter_id": entry["chapter_id"],
                    "card_id": entry.get("prior_card_id"),
                    "verdict": verdict,
                    "field_adjudications": entry.get("field_adjudications") or [],
                    "reason": entry["reason"],
                }
            )

    merge_entries: dict[str, set[str]] = {}
    for entry_id, target, _right in accepted_merges:
        merge_entries.setdefault(union.find(target), set()).add(entry_id)

    groups: dict[str, list[str]] = {}
    for entity_id in cards:
        groups.setdefault(union.find(entity_id), []).append(entity_id)

    effective: list[dict[str, Any]] = []
    for root in sorted(groups):
        members = sorted(groups[root])
        effective.append(
            _effective_entity(
                root=root,
                members=members,
                cards=cards,
                card_snapshots=card_snapshots,
                card_chapters=card_chapters,
                decision_refs=sorted(merge_entries.get(root, set())),
                evidence_block_ids=sorted(
                    {
                        block_id
                        for ref in merge_entries.get(root, set())
                        for block_id in evidence_by_entry.get(ref, [])
                    }
                ),
            )
        )

    pending, superseded_pending, projection_review_issues = (
        _retire_superseded_pending_cases_v1(
            pending_cases=pending,
            resolved_distinct_cases=resolved_distinct,
            ledger_entries=ledger["entries"],
        )
    )
    body = {
        "schema_version": PROJECTION_SCHEMA_VERSION,
        "book_id": ledger["book_id"],
        "source_registry_hashes": [
            _required_string(r.get("registry_hash"), "registry_hash") for r in registries
        ],
        "ledger_hash": ledger["ledger_hash"],
        "effective_entities": effective,
        "resolved_distinct_cases": resolved_distinct,
        "pending_cases": pending,
        "superseded_pending_cases": superseded_pending,
        "review_issues": projection_review_issues,
        "claim_adjudications": claim_adjudications,
        "observation_adjudications": dismissed_observations,
        "metrics": {
            "source_card_count": sum(len(rows) for rows in card_snapshots.values()),
            "source_entity_id_count": len(cards),
            "effective_entity_count": len(effective),
            "merged_group_count": sum(1 for row in effective if len(row["member_card_ids"]) > 1),
            "resolved_distinct_count": len(resolved_distinct),
            "pending_case_count": len(pending),
            "superseded_pending_case_count": len(superseded_pending),
            "review_issue_count": len(projection_review_issues),
            "claim_adjudication_count": len(claim_adjudications),
        },
        "identity_authority_granted": False,
    }
    return {**body, "projection_hash": canonical_hash(body)}


def build_projected_prior_cards_v1(
    *,
    registries: Sequence[Mapping[str, Any]],
    projection: Mapping[str, Any],
) -> list[dict[str, Any]]:
    """Prior cards for the next chapter, built from the reconciled view.

    Same shape the scan runner already consumes, so nothing downstream changes:
    a card that no decision touched comes through exactly as its registry
    projected it.  A merged group arrives as ONE card carrying every member's
    surfaces, provenance, and claims, which is the point - the next chapter can
    then retrieve it by any of its names instead of asking the same question
    again.

    ``record_class`` takes the strongest state present in the group.  Merge is
    the Auditor's finding that these records are one referent, so the best
    knowledge about that referent applies to all of it; keeping the weakest
    would re-open a hearing every chapter for a question already answered.
    Ranking is a fixed schema ladder, not a reading of the text.
    """

    base_history: dict[str, list[tuple[str, dict[str, Any]]]] = {}
    full_history: dict[str, list[tuple[str, dict[str, Any]]]] = {}
    for registry in registries:
        chapter_id = _required_string(registry.get("chapter_id"), "chapter_id")
        projected = registry.get("prior_cards_projection")
        if isinstance(projected, Mapping):
            for raw in projected.get("cards") or []:
                if isinstance(raw, Mapping) and isinstance(raw.get("prior_card_id"), str):
                    base_history.setdefault(raw["prior_card_id"], []).append(
                        (chapter_id, deepcopy(dict(raw)))
                    )
        for raw in registry.get("cards") or []:
            if isinstance(raw, Mapping) and isinstance(raw.get("entity_id"), str):
                full_history.setdefault(raw["entity_id"], []).append(
                    (chapter_id, deepcopy(dict(raw)))
                )

    rows: list[dict[str, Any]] = []
    for entity in projection.get("effective_entities") or []:
        members = [m for m in entity.get("member_card_ids") or [] if m in base_history]
        if not members:
            continue
        anchor_member = members[0]
        anchor = sorted(
            base_history[anchor_member],
            key=lambda row: (row[0], canonical_hash(row[1])),
        )
        anchor = anchor[-1][1]
        surfaces: list[str] = []
        provenance: list[Any] = []
        claims: list[dict[str, Any]] = []
        for member in members:
            for _chapter_id, base in sorted(
                base_history[member], key=lambda row: (row[0], canonical_hash(row[1]))
            ):
                for surface in base.get("stable_surfaces") or []:
                    if surface not in surfaces:
                        surfaces.append(surface)
                for ref in base.get("provenance_refs") or []:
                    if ref not in provenance:
                        provenance.append(deepcopy(ref))
            for _chapter_id, full in sorted(
                full_history.get(member) or [],
                key=lambda row: (row[0], canonical_hash(row[1])),
            ):
                for claim in full.get("claims") or []:
                    if not isinstance(claim, Mapping):
                        continue
                    row = {
                        "field": deepcopy(claim.get("field")),
                        "status": deepcopy(claim.get("status")),
                        "value": deepcopy(claim.get("value")),
                        "basis": deepcopy(claim.get("basis")),
                        "effective": deepcopy(claim.get("effective")),
                        "anchor_block_ids": deepcopy(list(claim.get("anchor_block_ids") or [])),
                        "story_time_note": deepcopy(claim.get("story_time_note")),
                        "validity": deepcopy(claim.get("validity")),
                        "semantic_status": deepcopy(claim.get("semantic_status")),
                    }
                    if row not in claims:
                        claims.append(row)
        claims.sort(key=lambda row: (str(row.get("field") or ""), canonical_hash(row)))
        card = {
            **anchor,
            "prior_card_id": _required_string(
                entity.get("effective_entity_id"), "effective_entity_id"
            ),
            "canonical_surface": entity.get("canonical_surface", anchor.get("canonical_surface")),
            "stable_surfaces": surfaces or list(anchor.get("stable_surfaces") or []),
            "provenance_refs": provenance or list(anchor.get("provenance_refs") or []),
            "record_class": _strongest_record_class(
                [
                    card.get("record_class")
                    for member in members
                    for _chapter_id, card in base_history[member]
                ]
            ),
            "profile_claims": claims,
            "distinguishing_note": _latest_distinguishing_note(
                member=anchor_member,
                full_history=full_history,
            ),
        }
        rows.append(card)
    rows.sort(key=lambda row: row["prior_card_id"])
    return rows


_RECORD_CLASS_RANK = (
    "unresolved_named_reference",
    "important_unnamed_referent",
    "named_entity_candidate",
    "confirmed_entity",
)


def _latest_distinguishing_note(
    *,
    member: str,
    full_history: Mapping[str, Sequence[tuple[str, Mapping[str, Any]]]],
) -> Any:
    snapshots = sorted(
        full_history.get(member) or [],
        key=lambda row: (row[0], canonical_hash(row[1])),
        reverse=True,
    )
    for _chapter_id, card in snapshots:
        if card.get("distinguishing_note") not in (None, ""):
            return deepcopy(card["distinguishing_note"])
    return None


def _strongest_record_class(values: Iterable[Any]) -> Any:
    best = None
    best_rank = -1
    for value in values:
        if value is None:
            continue
        try:
            rank = _RECORD_CLASS_RANK.index(str(value))
        except ValueError:
            # An unknown class is never silently downgraded; it simply cannot
            # win the ladder, and the first one seen still stands as fallback.
            if best is None:
                best = value
            continue
        if rank > best_rank:
            best, best_rank = value, rank
    return best


def reopen_admissibility_v1(
    projection: Mapping[str, Any],
    *,
    card_ids: Iterable[str],
    cited_block_ids: Iterable[str],
) -> dict[str, Any]:
    """Decide whether a settled case may be heard again.

    A settled question must stop costing a hearing every chapter, but it must
    never become unappealable: a builder that finds something genuinely new has
    to be able to bring it. So the gate is mechanical and narrow - the request
    must cite at least one block the verdict did not already rest on.

    A builder reading a later chapter necessarily cites that chapter's blocks,
    so a real new finding always passes. What the gate stops is re-asking the
    same question over the same evidence, which is what produced contradictory
    answers across chapters. No judgment about meaning happens here; whether
    the new block actually changes anything is the Auditor's to decide.

    ``cited_block_ids`` must be non-empty: a request with no evidence at all is
    not admissible against a decision that had some.
    """

    wanted = sorted(set(card_ids))
    cited = {block for block in cited_block_ids if isinstance(block, str) and block}
    for state, rows in (
        ("settled_distinct", projection.get("resolved_distinct_cases") or []),
        ("pending_evidence", projection.get("pending_cases") or []),
    ):
        for row in rows:
            if sorted(set(row.get("card_ids") or [])) != wanted:
                continue
            known = {
                block
                for block in row.get("evidence_block_ids") or []
                if isinstance(block, str)
            }
            fresh = sorted(cited - known)
            # A pending case reopens only when its packet carries something the
            # prior refusal did not already weigh.  Its free-text condition is
            # passed onward for the Auditor; code does not interpret prose.
            admissible = bool(fresh)
            return {
                "prior_state": state,
                "already_decided": True,
                "admissible": admissible,
                "new_block_ids": fresh,
                "known_block_ids": sorted(known),
                "entry_id": row.get("entry_id"),
                "resolution_condition": row.get("resolution_condition"),
                "reason": (
                    "cites evidence outside the earlier verdict"
                    if admissible
                    else "cites only evidence the earlier verdict already weighed"
                ),
            }
    for entity in projection.get("effective_entities") or []:
        members = sorted(set(entity.get("member_card_ids") or []))
        if len(members) < 2 or not set(wanted) <= set(members):
            continue
        known = {
            block
            for block in entity.get("evidence_block_ids") or []
            if isinstance(block, str)
        }
        fresh = sorted(cited - known)
        return {
            "prior_state": "settled_merged",
            "already_decided": True,
            "admissible": bool(fresh),
            "new_block_ids": fresh,
            "known_block_ids": sorted(known),
            "entry_id": (entity.get("decision_refs") or [None])[0],
            "resolution_condition": None,
            "reason": (
                "cites evidence outside the earlier verdict"
                if fresh
                else "cites only evidence the earlier verdict already weighed"
            ),
        }
    return {
        "prior_state": "unreviewed",
        "already_decided": False,
        "admissible": True,
        "new_block_ids": sorted(cited),
        "known_block_ids": [],
        "entry_id": None,
        "resolution_condition": None,
        "reason": "no earlier verdict covers this pair",
    }


# ---------------------------------------------------------------------------
# internals
# ---------------------------------------------------------------------------


class _UnionFind:
    """Merge is transitive: if A is B and B is C, a later chapter sees one
    person, not three fragments waiting for a hearing that will never come."""

    def __init__(self, cards: Mapping[str, Any]) -> None:
        self._parent = {key: key for key in cards}

    def find(self, key: str) -> str:
        root = key
        while self._parent[root] != root:
            root = self._parent[root]
        while self._parent[key] != root:
            self._parent[key], key = root, self._parent[key]
        return root

    def join(self, left: str, right: str) -> str:
        left_root, right_root = self.find(left), self.find(right)
        if left_root == right_root:
            return left_root
        # Deterministic winner: the lexicographically smaller id, so the same
        # ledger always projects the same effective ids regardless of order.
        winner, loser = sorted((left_root, right_root))
        self._parent[loser] = winner
        return winner


def _effective_entity(
    *,
    root: str,
    members: Sequence[str],
    cards: Mapping[str, Mapping[str, Any]],
    card_snapshots: Mapping[str, Sequence[tuple[str, Mapping[str, Any]]]],
    card_chapters: Mapping[str, set[str]],
    decision_refs: Sequence[str],
    evidence_block_ids: Sequence[str] = (),
) -> dict[str, Any]:
    ordered = sorted(
        members,
        key=lambda key: (min(card_chapters.get(key) or {""}), key),
    )
    anchor = cards[ordered[0]]
    surfaces: list[str] = []
    provenance: list[dict[str, Any]] = []
    aliases: list[Any] = []
    for member in ordered:
        for _chapter_id, card in sorted(
            card_snapshots[member], key=lambda row: (row[0], canonical_hash(row[1]))
        ):
            for surface in card.get("stable_surfaces") or []:
                if surface not in surfaces:
                    surfaces.append(surface)
            for ref in card.get("source_refs") or []:
                if ref not in provenance:
                    provenance.append(deepcopy(ref))
            for alias in card.get("aliases") or []:
                if alias not in aliases:
                    aliases.append(deepcopy(alias))
    return {
        "effective_entity_id": root,
        "member_card_ids": list(ordered),
        "canonical_surface": anchor.get("canonical_surface"),
        "referent_kind": anchor.get("referent_kind"),
        "record_class": anchor.get("record_class"),
        "stable_surfaces": surfaces,
        "aliases": aliases,
        "source_refs": provenance,
        "first_seen": deepcopy(anchor.get("first_seen")),
        "member_chapters": sorted(
            {
                chapter_id
                for member in ordered
                for chapter_id in card_chapters[member]
            }
        ),
        "decision_refs": list(decision_refs),
        "evidence_block_ids": list(evidence_block_ids),
        "identity_authority_granted": False,
    }


def _entry_prior_card_ids(entry: Mapping[str, Any]) -> list[str]:
    plural = entry.get("prior_card_ids")
    if isinstance(plural, list):
        return sorted(
            {_required_string(value, "prior_card_ids item") for value in plural}
        )
    singular = entry.get("prior_card_id")
    return [_required_string(singular, "prior_card_id")] if singular else []


def _entry_current_entity_ids(entry: Mapping[str, Any]) -> list[str]:
    plural = entry.get("current_entity_ids")
    singular = entry.get("current_entity_id")
    if plural is not None:
        if not isinstance(plural, list):
            raise B1DecisionLedgerError("current_entity_ids must be a list")
        listed = [
            _required_string(value, "current_entity_ids item") for value in plural
        ]
        if len(set(listed)) != len(listed):
            raise B1DecisionLedgerError("current_entity_ids contains a duplicate")
        resolved = sorted(listed)
        if singular is not None:
            singular_id = _required_string(singular, "current_entity_id")
            if resolved != [singular_id]:
                raise B1DecisionLedgerError(
                    "singular and plural current entity references disagree"
                )
        return resolved
    if singular is None:
        return []
    return [_required_string(singular, "current_entity_id")]


def _distinct_constraints_v1(
    entries: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for entry in entries:
        verdict = entry.get("verdict")
        rights = _entry_current_entity_ids(entry)
        if not rights:
            continue
        candidates = _entry_prior_card_ids(entry)
        if verdict in DISTINCT_VERDICTS:
            distinct = candidates
        elif verdict in MERGE_VERDICTS:
            target = entry.get("merge_target_prior_card_id")
            distinct = [candidate for candidate in candidates if candidate != target]
        elif verdict in PENDING_VERDICTS:
            distinct = [
                candidate
                for candidate in candidates
                if candidate in set(entry.get("excluded_prior_card_ids") or [])
            ]
        else:
            distinct = []
        for candidate in distinct:
            for right in rights:
                if candidate == right:
                    continue
                rows.append(
                    {
                        "card_ids": tuple(sorted((candidate, right))),
                        "entry_id": entry.get("entry_id"),
                    }
                )
    rows.sort(key=lambda row: (row["card_ids"], str(row["entry_id"] or "")))
    return rows


def _merge_group_distinct_conflicts_v1(
    *,
    union: "_UnionFind",
    member_ids: Sequence[str],
    constraints: Sequence[Mapping[str, Any]],
) -> list[str]:
    joined_roots = {union.find(member_id) for member_id in member_ids}
    conflicts: set[str] = set()
    for constraint in constraints:
        first, second = constraint["card_ids"]
        if first not in union._parent or second not in union._parent:
            continue
        first_root = union.find(first)
        second_root = union.find(second)
        if first_root == second_root or (
            first_root in joined_roots
            and second_root in joined_roots
            and first_root != second_root
        ):
            entry_id = constraint.get("entry_id")
            if isinstance(entry_id, str) and entry_id:
                conflicts.add(entry_id)
    return sorted(conflicts)


def _resolved_distinct_row_v1(
    *,
    entry: Mapping[str, Any],
    left: str,
    right: str,
    evidence_block_ids: Sequence[str],
    finding: str,
) -> dict[str, Any]:
    return {
        "entry_id": entry["entry_id"],
        "component_id": entry["component_id"],
        "chapter_id": entry["chapter_id"],
        "card_ids": sorted([left, right]),
        "verdict": entry["verdict"],
        "finding": finding,
        "evidence_block_ids": sorted(set(evidence_block_ids)),
        "reason": entry["reason"],
    }


def _retire_superseded_pending_cases_v1(
    *,
    pending_cases: Sequence[Mapping[str, Any]],
    resolved_distinct_cases: Sequence[Mapping[str, Any]],
    ledger_entries: Sequence[Mapping[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    """Move an older pending pair aside after a later distinct verdict.

    Chapter order comes from the append-only ledger, not lexical chapter-id
    order.  The first entry seen for each chapter fixes that chapter's rank.
    """

    chapter_rank: dict[str, int] = {}
    for raw in ledger_entries:
        chapter_id = _required_string(raw.get("chapter_id"), "chapter_id")
        sequence_index = raw.get("sequence_index")
        if not isinstance(sequence_index, int) or isinstance(sequence_index, bool):
            raise B1DecisionLedgerError("ledger sequence_index is malformed")
        chapter_rank.setdefault(chapter_id, sequence_index)

    resolved_by_cards: dict[frozenset[str], list[dict[str, Any]]] = {}
    for raw in resolved_distinct_cases:
        row = deepcopy(dict(raw))
        if row.get("verdict") != "confirmed_distinct":
            continue
        card_ids = _card_set_for_supersession_v1(
            row.get("card_ids"), "resolved distinct card_ids"
        )
        resolved_by_cards.setdefault(card_ids, []).append(row)
    for rows in resolved_by_cards.values():
        rows.sort(
            key=lambda row: (
                chapter_rank[_required_string(row.get("chapter_id"), "chapter_id")],
                _required_string(row.get("entry_id"), "entry_id"),
                _required_string(row.get("component_id"), "component_id"),
            )
        )

    effective: list[dict[str, Any]] = []
    superseded: list[dict[str, Any]] = []
    review_issues: list[dict[str, Any]] = []
    for raw in pending_cases:
        pending = deepcopy(dict(raw))
        cards = _card_set_for_supersession_v1(
            pending.get("card_ids"), "pending card_ids"
        )
        pending_chapter = _required_string(
            pending.get("chapter_id"), "pending chapter_id"
        )
        pending_rank = chapter_rank[pending_chapter]
        candidates = resolved_by_cards.get(cards, [])
        later = [
            row
            for row in candidates
            if chapter_rank[_required_string(row.get("chapter_id"), "chapter_id")]
            > pending_rank
        ]
        if later:
            settled = later[0]
            pending.update(
                {
                    "superseded_by_entry_id": _required_string(
                        settled.get("entry_id"), "superseding entry_id"
                    ),
                    "superseded_by_component": _required_string(
                        settled.get("component_id"), "superseding component_id"
                    ),
                    "superseded_in_chapter": _required_string(
                        settled.get("chapter_id"), "superseding chapter_id"
                    ),
                    "superseded_reason": "later verdict settled the same card set",
                }
            )
            superseded.append(pending)
            continue

        effective.append(pending)
        for settled in candidates:
            settled_chapter = _required_string(
                settled.get("chapter_id"), "resolved chapter_id"
            )
            if chapter_rank[settled_chapter] > pending_rank:
                continue
            review_issues.append(
                {
                    "issue_code": "pending_resolved_order_conflict",
                    "card_ids": sorted(cards),
                    "pending_entry_id": _required_string(
                        pending.get("entry_id"), "pending entry_id"
                    ),
                    "pending_chapter_id": pending_chapter,
                    "resolved_entry_id": _required_string(
                        settled.get("entry_id"), "resolved entry_id"
                    ),
                    "resolved_chapter_id": settled_chapter,
                }
            )

    return (
        effective,
        superseded,
        sorted(
            review_issues,
            key=lambda row: (
                row["pending_entry_id"],
                row["resolved_entry_id"],
            ),
        ),
    )


def _card_set_for_supersession_v1(value: Any, label: str) -> frozenset[str]:
    if not isinstance(value, list) or len(value) < 2:
        raise B1DecisionLedgerError(f"{label} must contain at least two ids")
    cards = frozenset(_required_string(item, label) for item in value)
    if len(cards) != len(value):
        raise B1DecisionLedgerError(f"{label} contains a duplicate")
    return cards


def _exclusion_evidence_block_ids_v1(
    entry: Mapping[str, Any], candidate_id: str
) -> list[str]:
    return sorted(
        {
            row["block_id"]
            for row in entry.get("evidence") or []
            if candidate_id in (row.get("supports_excluded_prior_card_ids") or [])
        }
    )


def _unapplied(
    entry: Mapping[str, Any],
    reason: str,
    *,
    state: str = "decision_not_applicable_here",
    conflicting_entry_ids: Sequence[str] = (),
) -> dict[str, Any]:
    return {
        "entry_id": entry["entry_id"],
        "component_id": entry["component_id"],
        "chapter_id": entry["chapter_id"],
        "question_type": entry.get("question_type"),
        "review_route": entry.get("review_route"),
        "card_ids": sorted(
            [
                *_entry_prior_card_ids(entry),
                *_entry_current_entity_ids(entry),
            ]
        ),
        "state": state,
        "reason": reason,
        "conflicting_entry_ids": list(conflicting_entry_ids),
    }


def _sealed_ledger(*, book_id: str, entries: list[dict[str, Any]]) -> dict[str, Any]:
    body = {
        "schema_version": LEDGER_SCHEMA_VERSION,
        "book_id": book_id,
        "entries": entries,
    }
    return {**body, "ledger_hash": canonical_hash(body)}


def _validated_evidence(value: Any) -> list[dict[str, Any]]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise B1DecisionLedgerError("evidence must be a list")
    rows: list[dict[str, Any]] = []
    for raw in value:
        if not isinstance(raw, Mapping):
            raise B1DecisionLedgerError("evidence row must be an object")
        row = {
            "block_id": _required_string(raw.get("block_id"), "evidence block_id"),
            "quote": _required_string(raw.get("quote"), "evidence quote"),
        }
        support_ids = raw.get("supports_excluded_prior_card_ids")
        if support_ids is not None:
            if not isinstance(support_ids, list):
                raise B1DecisionLedgerError(
                    "supports_excluded_prior_card_ids must be a list"
                )
            row["supports_excluded_prior_card_ids"] = sorted(
                {
                    _required_string(value, "excluded prior card evidence id")
                    for value in support_ids
                }
            )
        rows.append(row)
    return rows


def _validated_excluded_prior_card_ids(
    value: Any,
    *,
    verdict: str,
    candidate_ids: Sequence[str],
    evidence: Sequence[Mapping[str, Any]],
) -> list[str]:
    if value is None:
        excluded: list[str] = []
    elif isinstance(value, list):
        excluded = sorted(
            {_required_string(row, "excluded_prior_card_ids item") for row in value}
        )
    else:
        raise B1DecisionLedgerError("excluded_prior_card_ids must be a list")
    if verdict not in PENDING_VERDICTS:
        if excluded:
            raise B1DecisionLedgerError(
                "excluded prior candidates are only legal on insufficient_evidence"
            )
        return []
    candidates = set(candidate_ids)
    if not set(excluded) <= candidates:
        raise B1DecisionLedgerError(
            "excluded prior candidate is outside the hearing candidate set"
        )
    if excluded and set(excluded) == candidates:
        raise B1DecisionLedgerError(
            "excluding every prior candidate requires a distinct verdict"
        )
    supported = {
        candidate_id
        for row in evidence
        for candidate_id in row.get("supports_excluded_prior_card_ids") or []
    }
    if supported != set(excluded):
        raise B1DecisionLedgerError(
            "excluded prior candidates and their evidence coverage differ"
        )
    return excluded


def _validated_field_adjudications(value: Any) -> list[dict[str, Any]]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise B1DecisionLedgerError("field_adjudications must be a list")
    rows: list[dict[str, Any]] = []
    for raw in value:
        if not isinstance(raw, Mapping):
            raise B1DecisionLedgerError("field adjudication must be an object")
        rows.append(deepcopy(dict(raw)))
    return rows


def _validated_resolution_condition(value: Any, verdict: str) -> str | None:
    """Keep the optional field compatible with older decision artifacts."""

    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise B1DecisionLedgerError("resolution_condition must be a non-empty string")
    if verdict not in PENDING_VERDICTS:
        raise B1DecisionLedgerError(
            "resolution_condition is only legal on an insufficient_evidence verdict"
        )
    return value


def _component_current_entity_ids(component: Mapping[str, Any]) -> list[str]:
    singular = component.get("current_entity_id")
    plural = component.get("current_entity_ids")
    if plural is not None:
        if not isinstance(plural, list):
            raise B1DecisionLedgerError("current_entity_ids must be a list")
        listed = [
            _required_string(value, "current_entity_ids item") for value in plural
        ]
        if len(set(listed)) != len(listed):
            raise B1DecisionLedgerError("current_entity_ids contains a duplicate")
        resolved = sorted(listed)
        if singular is not None and resolved != [
            _required_string(singular, "current_entity_id")
        ]:
            raise B1DecisionLedgerError(
                "singular and plural current entity references disagree"
            )
        return resolved
    if singular is None:
        return []
    return [_required_string(singular, "current_entity_id")]


def _component_prior_card_ids(component: Mapping[str, Any]) -> list[str]:
    plural = component.get("prior_card_ids")
    if plural is not None:
        if not isinstance(plural, list):
            raise B1DecisionLedgerError("prior_card_ids must be a list")
        ids = sorted(
            {_required_string(value, "prior_card_ids item") for value in plural}
        )
        singular = component.get("prior_card_id")
        if singular is not None and ids != [_required_string(singular, "prior_card_id")]:
            raise B1DecisionLedgerError(
                "singular and plural prior card references disagree"
            )
        return ids
    singular = component.get("prior_card_id")
    return [_required_string(singular, "prior_card_id")] if singular else []


def _required_string(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise B1DecisionLedgerError(f"{label} must be a non-empty string")
    return value.strip()


__all__ = [
    "B1DecisionLedgerError",
    "DISTINCT_VERDICTS",
    "LEDGER_SCHEMA_VERSION",
    "MERGE_VERDICTS",
    "PENDING_VERDICTS",
    "PROJECTION_SCHEMA_VERSION",
    "append_cross_chapter_decisions_v1",
    "build_projected_prior_cards_v1",
    "reopen_admissibility_v1",
    "empty_decision_ledger_v1",
    "project_reconciled_b1_registry_v1",
    "verify_decision_ledger_v1",
]
