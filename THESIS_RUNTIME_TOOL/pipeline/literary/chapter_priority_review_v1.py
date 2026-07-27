"""Non-authoritative review leads derived from B0 chapter priorities.

The priority list is a scheduling hint, not an identity decision.  This module
preserves mechanically valid priority rows that lost their candidate, detects
same-surface candidate collisions, and carries recurring non-active items to a
bounded book-end review queue.  It never creates, merges, promotes, or rejects
an entity.
"""

from __future__ import annotations

from copy import deepcopy
import re
from typing import Any, Mapping, Sequence
import unicodedata

from pipeline.literary.chapter_prefix_prior_v1 import (
    verify_chapter_prefix_prior_bundle_v1,
)
from pipeline.literary.checkpoint import canonical_hash


INDEX_SCHEMA_VERSION = "chapter_priority_review_index_v1"
INDEX_VALIDATOR_VERSION = "chapter_priority_review_validator_v1"
DEFAULT_MAX_PRIORITY_RANK = 8
DEFAULT_MIN_RANKED_CHAPTERS = 2
DEFAULT_MAX_REVIEW_LEADS = 32

ENTITY_ITEM_CLASSES = frozenset(
    {"new_entity", "prior_entity", "candidate_only_entity", "unresolved"}
)
TRIGGER_KINDS = frozenset(
    {
        "duplicate_priority_surface",
        "priority_orphan",
        "priority_reference_without_context_card",
        "recurring_priority_without_active_authority",
    }
)
REVIEW_ROUTES = frozenset({"book_identity_auditor", "book_end_recall_review"})
LEAD_STATES = frozenset({"book_end_pending", "deferred_without_authority"})


class ChapterPriorityReviewError(ValueError):
    """Raised when a priority-review index is malformed or stale."""


def _clone(value: Any) -> Any:
    return deepcopy(value)


def _required_string(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ChapterPriorityReviewError(f"{label} must be a non-empty string")
    return value


def _surface_key(value: Any) -> str:
    text = unicodedata.normalize("NFKC", str(value)).casefold()
    text = re.sub(r"\s+", " ", text).strip()
    return text.strip(" \t\r\n.,;:!?\"'()[]{}")


def _block_text(block: Mapping[str, Any]) -> str:
    return unicodedata.normalize(
        "NFC",
        str(
            block.get("clean_text")
            or block.get("source_text")
            or block.get("text")
            or ""
        ),
    )


def _contains_surface(text: str, surface: str) -> bool:
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


def _sorted_strings(value: Any, label: str, *, allow_empty: bool = True) -> list[str]:
    if not isinstance(value, list) or (not allow_empty and not value):
        raise ChapterPriorityReviewError(f"{label} must be a list")
    rows = [_required_string(row, label) for row in value]
    if rows != sorted(set(rows)):
        raise ChapterPriorityReviewError(f"{label} must be sorted and unique")
    return rows


def _unique_strings(value: Any, label: str, *, allow_empty: bool = True) -> list[str]:
    if not isinstance(value, list) or (not allow_empty and not value):
        raise ChapterPriorityReviewError(f"{label} must be a list")
    rows = [_required_string(row, label) for row in value]
    if len(rows) != len(set(rows)):
        raise ChapterPriorityReviewError(f"{label} must be unique")
    return rows


def _bounded_int(value: Any, label: str, *, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ChapterPriorityReviewError(f"{label} must be an integer")
    if value < minimum or value > maximum:
        raise ChapterPriorityReviewError(f"{label} is outside the allowed range")
    return value


def _chapter_blocks(document: Mapping[str, Any]) -> dict[str, dict[str, Mapping[str, Any]]]:
    chapters = document.get("chapters")
    if not isinstance(chapters, list):
        raise ChapterPriorityReviewError("document has no chapter list")
    result: dict[str, dict[str, Mapping[str, Any]]] = {}
    for chapter in chapters:
        if not isinstance(chapter, Mapping):
            raise ChapterPriorityReviewError("document chapter is malformed")
        chapter_id = _required_string(chapter.get("chapter_id"), "chapter_id")
        blocks = chapter.get("blocks")
        if not isinstance(blocks, list):
            raise ChapterPriorityReviewError("chapter has no block list")
        block_map: dict[str, Mapping[str, Any]] = {}
        for block in blocks:
            if not isinstance(block, Mapping):
                raise ChapterPriorityReviewError("chapter block is malformed")
            block_id = _required_string(block.get("block_id"), "block_id")
            if block_id in block_map:
                raise ChapterPriorityReviewError("chapter repeats a block id")
            block_map[block_id] = block
        result[chapter_id] = block_map
    return result


def _artifact_validation_report(artifact: Mapping[str, Any]) -> Mapping[str, Any]:
    report = artifact.get("validation_report") or artifact.get(
        "source_validation_report"
    )
    return report if isinstance(report, Mapping) else {}


def _occurrence_identity(row: Mapping[str, Any]) -> dict[str, Any]:
    return {
        key: _clone(row[key])
        for key in (
            "chapter_id",
            "rank",
            "surface",
            "surface_key",
            "item_class",
            "source_block_id",
            "source_kind",
            "resolved_ref_ids",
            "resolved_prior_card_ids",
            "unresolved_ref_ids",
            "resolution_state",
            "source_artifact_hash",
        )
    }


def _make_occurrence(**values: Any) -> dict[str, Any]:
    body = dict(values)
    return {
        "occurrence_id": "priocc1_" + canonical_hash(body)[:20],
        **body,
        "authority_effect": "none",
    }


def _priority_rows(
    *,
    chapter_id: str,
    artifact: Mapping[str, Any],
    block_by_id: Mapping[str, Mapping[str, Any]],
    source_to_prior: Mapping[str, str],
    known_prior_ids: set[str],
    active_prior_ids: set[str],
    candidate_prior_ids: set[str],
) -> list[dict[str, Any]]:
    artifact_hash = canonical_hash(artifact)
    occurrences: list[dict[str, Any]] = []

    for raw in artifact.get("chapter_priority_order") or []:
        if not isinstance(raw, Mapping):
            raise ChapterPriorityReviewError("accepted priority row is malformed")
        item_class = _required_string(raw.get("item_class"), "priority item_class")
        if item_class not in ENTITY_ITEM_CLASSES:
            continue
        rank = _bounded_int(raw.get("rank"), "priority rank", minimum=1, maximum=15)
        surface = _required_string(raw.get("surface"), "priority surface")
        block_id = _required_string(raw.get("source_block_id"), "source_block_id")
        block = block_by_id.get(block_id)
        if block is None or not _contains_surface(_block_text(block), surface):
            raise ChapterPriorityReviewError("accepted priority address is stale")
        resolved_refs = sorted(
            {
                _required_string(value, "resolved_ref")
                for value in raw.get("resolved_refs") or []
            }
        )
        prior_ids = sorted(
            {
                source_to_prior.get(ref, ref)
                for ref in resolved_refs
                if source_to_prior.get(ref, ref) in known_prior_ids
            }
        )
        unresolved_refs = sorted(
            ref
            for ref in resolved_refs
            if source_to_prior.get(ref, ref) not in known_prior_ids
        )
        active = set(prior_ids) & active_prior_ids
        candidate = set(prior_ids) & candidate_prior_ids
        if unresolved_refs and not prior_ids:
            state = "reference_without_context_card"
        elif len(prior_ids) > 1:
            state = "multi_candidate"
        elif active and candidate:
            state = "mixed_authority"
        elif active:
            state = "active"
        elif candidate:
            state = "candidate_only"
        elif prior_ids:
            state = "inactive"
        else:
            state = "reference_without_context_card"
        occurrences.append(
            _make_occurrence(
                chapter_id=chapter_id,
                rank=rank,
                surface=surface,
                surface_key=_surface_key(surface),
                item_class=item_class,
                source_block_id=block_id,
                source_kind="accepted_priority",
                resolved_ref_ids=resolved_refs,
                resolved_prior_card_ids=prior_ids,
                unresolved_ref_ids=unresolved_refs,
                resolution_state=state,
                source_artifact_hash=artifact_hash,
            )
        )

    report = _artifact_validation_report(artifact)
    for issue in report.get("priority_issues") or []:
        if not isinstance(issue, Mapping) or issue.get("reason") != (
            "priority row does not reference an emitted or supplied item"
        ):
            continue
        raw = issue.get("raw_row")
        if not isinstance(raw, Mapping):
            raise ChapterPriorityReviewError("priority orphan row is malformed")
        item_class = _required_string(raw.get("item_class"), "orphan item_class")
        if item_class not in ENTITY_ITEM_CLASSES:
            continue
        rank = _bounded_int(issue.get("raw_rank"), "orphan rank", minimum=1, maximum=15)
        surface = _required_string(raw.get("surface"), "orphan surface")
        block_id = _required_string(raw.get("source_block_id"), "orphan block_id")
        block = block_by_id.get(block_id)
        if block is None or not _contains_surface(_block_text(block), surface):
            raise ChapterPriorityReviewError("priority orphan address is stale")
        occurrences.append(
            _make_occurrence(
                chapter_id=chapter_id,
                rank=rank,
                surface=surface,
                surface_key=_surface_key(surface),
                item_class=item_class,
                source_block_id=block_id,
                source_kind="priority_orphan",
                resolved_ref_ids=[],
                resolved_prior_card_ids=[],
                unresolved_ref_ids=[],
                resolution_state="orphan",
                source_artifact_hash=artifact_hash,
            )
        )
    return occurrences


def _group_occurrences(
    occurrences: Sequence[Mapping[str, Any]],
    *,
    active_prior_ids: set[str],
    candidate_prior_ids: set[str],
    max_priority_rank: int,
    min_ranked_chapters: int,
) -> list[dict[str, Any]]:
    by_surface: dict[str, list[Mapping[str, Any]]] = {}
    for row in occurrences:
        by_surface.setdefault(str(row["surface_key"]), []).append(row)
    groups: list[dict[str, Any]] = []
    for surface_key, rows in sorted(by_surface.items()):
        occurrence_ids = sorted(str(row["occurrence_id"]) for row in rows)
        prior_ids = sorted(
            {
                prior_id
                for row in rows
                for prior_id in row["resolved_prior_card_ids"]
            }
        )
        chapter_ids = sorted({str(row["chapter_id"]) for row in rows})
        best_rank = min(int(row["rank"]) for row in rows)
        orphan_count = sum(row["resolution_state"] == "orphan" for row in rows)
        unresolved_count = sum(
            row["resolution_state"] == "reference_without_context_card"
            for row in rows
        )
        signals: set[str] = set()
        if len(prior_ids) > 1:
            signals.add("duplicate_priority_surface")
        if orphan_count:
            signals.add("priority_orphan")
        if unresolved_count:
            signals.add("priority_reference_without_context_card")
        active_ids = sorted(set(prior_ids) & active_prior_ids)
        candidate_ids = sorted(set(prior_ids) & candidate_prior_ids)
        if (
            len(chapter_ids) >= min_ranked_chapters
            and best_rank <= max_priority_rank
            and not active_ids
        ):
            signals.add("recurring_priority_without_active_authority")
        group_identity = {
            "surface_key": surface_key,
            "occurrence_ids": occurrence_ids,
        }
        groups.append(
            {
                "group_id": "prisurf1_" + canonical_hash(group_identity)[:20],
                "surface_key": surface_key,
                "observed_surfaces": sorted({str(row["surface"]) for row in rows}),
                "best_rank": best_rank,
                "chapter_ids": chapter_ids,
                "ranked_chapter_count": len(chapter_ids),
                "occurrence_ids": occurrence_ids,
                "subject_prior_card_ids": prior_ids,
                "active_prior_card_ids": active_ids,
                "candidate_only_prior_card_ids": candidate_ids,
                "orphan_occurrence_count": orphan_count,
                "unresolved_reference_count": unresolved_count,
                "signal_kinds": sorted(signals),
                "authority_effect": "none",
            }
        )
    return groups


def _review_leads(
    groups: Sequence[Mapping[str, Any]],
    occurrences: Sequence[Mapping[str, Any]],
    *,
    max_review_leads: int,
) -> list[dict[str, Any]]:
    occurrence_by_id = {str(row["occurrence_id"]): row for row in occurrences}
    candidates: list[dict[str, Any]] = []
    for group in groups:
        triggers = list(group["signal_kinds"])
        if not triggers:
            continue
        subject_ids = list(group["subject_prior_card_ids"])
        route = (
            "book_identity_auditor"
            if subject_ids
            and any(
                trigger
                in {
                    "duplicate_priority_surface",
                    "recurring_priority_without_active_authority",
                }
                for trigger in triggers
            )
            else "book_end_recall_review"
        )
        source_blocks = sorted(
            {
                str(occurrence_by_id[occurrence_id]["source_block_id"])
                for occurrence_id in group["occurrence_ids"]
            }
        )
        identity = {
            "group_id": group["group_id"],
            "trigger_kinds": triggers,
            "subject_prior_card_ids": subject_ids,
            "source_block_ids": source_blocks,
        }
        candidates.append(
            {
                "lead_id": "prilead1_" + canonical_hash(identity)[:20],
                "group_id": group["group_id"],
                "surface_key": group["surface_key"],
                "trigger_kinds": triggers,
                "best_rank": group["best_rank"],
                "chapter_ids": list(group["chapter_ids"]),
                "source_block_ids": source_blocks,
                "subject_prior_card_ids": subject_ids,
                "route": route,
                "lifecycle_state": "book_end_pending",
                "authority_effect": "none",
            }
        )
    candidates.sort(
        key=lambda row: (
            int(row["best_rank"]),
            -len(row["chapter_ids"]),
            str(row["surface_key"]),
            str(row["lead_id"]),
        )
    )
    for offset, row in enumerate(candidates):
        if offset >= max_review_leads:
            row["lifecycle_state"] = "deferred_without_authority"
    return sorted(candidates, key=lambda row: row["lead_id"])


def build_chapter_priority_review_index_v1(
    *,
    document: Mapping[str, Any],
    priority_artifacts: Mapping[str, Mapping[str, Any]],
    final_prefix_bundle: Mapping[str, Any],
    max_priority_rank: int = DEFAULT_MAX_PRIORITY_RANK,
    min_ranked_chapters: int = DEFAULT_MIN_RANKED_CHAPTERS,
    max_review_leads: int = DEFAULT_MAX_REVIEW_LEADS,
) -> dict[str, Any]:
    """Build a bounded review index without mutating registry authority."""

    rank_cap = _bounded_int(
        max_priority_rank, "max_priority_rank", minimum=1, maximum=15
    )
    chapter_floor = _bounded_int(
        min_ranked_chapters, "min_ranked_chapters", minimum=1, maximum=1000
    )
    lead_cap = _bounded_int(
        max_review_leads, "max_review_leads", minimum=0, maximum=10000
    )
    blocks = _chapter_blocks(document)
    prefix = verify_chapter_prefix_prior_bundle_v1(
        final_prefix_bundle, document=document
    )
    covered = list(prefix["covered_chapter_ids"])
    if set(priority_artifacts) != set(covered):
        raise ChapterPriorityReviewError(
            "priority artifacts must exact-cover the prefix chapters"
        )

    source_to_prior = {
        str(row["source_candidate_id"]): str(row["prior_card_id"])
        for row in prefix["source_entity_manifest"]
    }
    active_ids = {
        str(row["prior_card_id"]) for row in prefix["b0_context_cards"]
    }
    candidate_ids = {
        str(row["prior_card_id"])
        for row in prefix["candidate_only_context_cards"]
    }
    known_prior_ids = {
        str(row["prior_card_id"]) for row in prefix["source_entity_manifest"]
    }
    occurrences: list[dict[str, Any]] = []
    source_artifacts: list[dict[str, str]] = []
    for chapter_id in covered:
        artifact = priority_artifacts[chapter_id]
        if not isinstance(artifact, Mapping):
            raise ChapterPriorityReviewError("priority artifact is malformed")
        if artifact.get("chapter_id") != chapter_id:
            raise ChapterPriorityReviewError("priority artifact chapter is stale")
        source_artifacts.append(
            {
                "chapter_id": chapter_id,
                "source_artifact_hash": canonical_hash(artifact),
            }
        )
        occurrences.extend(
            _priority_rows(
                chapter_id=chapter_id,
                artifact=artifact,
                block_by_id=blocks[chapter_id],
                source_to_prior=source_to_prior,
                known_prior_ids=known_prior_ids,
                active_prior_ids=active_ids,
                candidate_prior_ids=candidate_ids,
            )
        )
    occurrences.sort(
        key=lambda row: (
            covered.index(str(row["chapter_id"])),
            int(row["rank"]),
            str(row["occurrence_id"]),
        )
    )
    groups = _group_occurrences(
        occurrences,
        active_prior_ids=active_ids,
        candidate_prior_ids=candidate_ids,
        max_priority_rank=rank_cap,
        min_ranked_chapters=chapter_floor,
    )
    leads = _review_leads(groups, occurrences, max_review_leads=lead_cap)
    counts = {
        "priority_occurrence_count": len(occurrences),
        "priority_surface_group_count": len(groups),
        "priority_orphan_occurrence_count": sum(
            row["resolution_state"] == "orphan" for row in occurrences
        ),
        "duplicate_priority_surface_group_count": sum(
            "duplicate_priority_surface" in row["signal_kinds"] for row in groups
        ),
        "recurring_nonactive_group_count": sum(
            "recurring_priority_without_active_authority" in row["signal_kinds"]
            for row in groups
        ),
        "book_identity_auditor_lead_count": sum(
            row["route"] == "book_identity_auditor" for row in leads
        ),
        "book_end_recall_review_lead_count": sum(
            row["route"] == "book_end_recall_review" for row in leads
        ),
        "deferred_lead_count": sum(
            row["lifecycle_state"] == "deferred_without_authority" for row in leads
        ),
    }
    body = {
        "schema_version": INDEX_SCHEMA_VERSION,
        "validator_version": INDEX_VALIDATOR_VERSION,
        "state_lineage_id": prefix["state_lineage_id"],
        "coverage_through_chapter_id": prefix["coverage_through_chapter_id"],
        "chapter_ids": covered,
        "source_artifacts": source_artifacts,
        "bounds": {
            "max_priority_rank": rank_cap,
            "min_ranked_chapters": chapter_floor,
            "max_review_leads": lead_cap,
        },
        "occurrences": occurrences,
        "surface_groups": groups,
        "review_leads": leads,
        "counts": counts,
        "production_publish_performed": False,
    }
    result = {**body, "priority_review_index_hash": canonical_hash(body)}
    return verify_chapter_priority_review_index_v1(result, document=document)


def verify_chapter_priority_review_index_v1(
    index: Mapping[str, Any],
    *,
    document: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    if index.get("schema_version") != INDEX_SCHEMA_VERSION:
        raise ChapterPriorityReviewError("foreign priority-review schema")
    if index.get("validator_version") != INDEX_VALIDATOR_VERSION:
        raise ChapterPriorityReviewError("priority-review validator mismatch")
    body = dict(index)
    observed_hash = _required_string(
        body.pop("priority_review_index_hash", None), "priority_review_index_hash"
    )
    if canonical_hash(body) != observed_hash:
        raise ChapterPriorityReviewError("priority-review index hash mismatch")
    if index.get("production_publish_performed") is not False:
        raise ChapterPriorityReviewError("priority-review index claims publication")
    chapter_ids = _unique_strings(
        index.get("chapter_ids"), "chapter_ids", allow_empty=False
    )
    if document is not None:
        known_chapters = set(_chapter_blocks(document))
        if not set(chapter_ids) <= known_chapters:
            raise ChapterPriorityReviewError("priority-review index cites a foreign chapter")
    bounds = index.get("bounds")
    if not isinstance(bounds, Mapping) or set(bounds) != {
        "max_priority_rank",
        "min_ranked_chapters",
        "max_review_leads",
    }:
        raise ChapterPriorityReviewError("priority-review bounds are malformed")
    _bounded_int(bounds["max_priority_rank"], "max_priority_rank", minimum=1, maximum=15)
    _bounded_int(
        bounds["min_ranked_chapters"],
        "min_ranked_chapters",
        minimum=1,
        maximum=1000,
    )
    _bounded_int(bounds["max_review_leads"], "max_review_leads", minimum=0, maximum=10000)

    occurrences = index.get("occurrences")
    if not isinstance(occurrences, list):
        raise ChapterPriorityReviewError("priority occurrences must be a list")
    occurrence_ids: set[str] = set()
    for row in occurrences:
        if not isinstance(row, Mapping):
            raise ChapterPriorityReviewError("priority occurrence is malformed")
        if row.get("authority_effect") != "none":
            raise ChapterPriorityReviewError("priority occurrence grants authority")
        occurrence_id = _required_string(row.get("occurrence_id"), "occurrence_id")
        expected = "priocc1_" + canonical_hash(_occurrence_identity(row))[:20]
        if occurrence_id != expected or occurrence_id in occurrence_ids:
            raise ChapterPriorityReviewError("priority occurrence id is stale or repeated")
        occurrence_ids.add(occurrence_id)
        _sorted_strings(row.get("resolved_ref_ids"), "resolved_ref_ids")
        _sorted_strings(
            row.get("resolved_prior_card_ids"), "resolved_prior_card_ids"
        )
        _sorted_strings(row.get("unresolved_ref_ids"), "unresolved_ref_ids")

    groups = index.get("surface_groups")
    if not isinstance(groups, list):
        raise ChapterPriorityReviewError("priority surface groups must be a list")
    group_ids: set[str] = set()
    for row in groups:
        if not isinstance(row, Mapping) or row.get("authority_effect") != "none":
            raise ChapterPriorityReviewError("priority surface group is malformed")
        group_id = _required_string(row.get("group_id"), "group_id")
        occurrence_rows = _sorted_strings(row.get("occurrence_ids"), "occurrence_ids")
        if not set(occurrence_rows) <= occurrence_ids:
            raise ChapterPriorityReviewError("priority surface group cites a foreign occurrence")
        expected = "prisurf1_" + canonical_hash(
            {"surface_key": row.get("surface_key"), "occurrence_ids": occurrence_rows}
        )[:20]
        if group_id != expected or group_id in group_ids:
            raise ChapterPriorityReviewError("priority surface group id is stale or repeated")
        group_ids.add(group_id)
        signals = _sorted_strings(row.get("signal_kinds"), "signal_kinds")
        if not set(signals) <= TRIGGER_KINDS:
            raise ChapterPriorityReviewError("priority surface group has a foreign signal")
        if row.get("ranked_chapter_count") != len(row.get("chapter_ids") or []):
            raise ChapterPriorityReviewError("priority chapter count is stale")

    leads = index.get("review_leads")
    if not isinstance(leads, list):
        raise ChapterPriorityReviewError("priority review leads must be a list")
    lead_ids: set[str] = set()
    for row in leads:
        if not isinstance(row, Mapping) or row.get("authority_effect") != "none":
            raise ChapterPriorityReviewError("priority review lead is malformed")
        lead_id = _required_string(row.get("lead_id"), "lead_id")
        if lead_id in lead_ids:
            raise ChapterPriorityReviewError("priority review lead repeats an id")
        lead_ids.add(lead_id)
        if row.get("group_id") not in group_ids:
            raise ChapterPriorityReviewError("priority review lead cites a foreign group")
        triggers = _sorted_strings(row.get("trigger_kinds"), "trigger_kinds", allow_empty=False)
        if not set(triggers) <= TRIGGER_KINDS:
            raise ChapterPriorityReviewError("priority review lead has a foreign trigger")
        if row.get("route") not in REVIEW_ROUTES:
            raise ChapterPriorityReviewError("priority review lead has a foreign route")
        if row.get("lifecycle_state") not in LEAD_STATES:
            raise ChapterPriorityReviewError("priority review lead has a foreign lifecycle")
        identity = {
            "group_id": row.get("group_id"),
            "trigger_kinds": triggers,
            "subject_prior_card_ids": row.get("subject_prior_card_ids"),
            "source_block_ids": row.get("source_block_ids"),
        }
        if lead_id != "prilead1_" + canonical_hash(identity)[:20]:
            raise ChapterPriorityReviewError("priority review lead id is stale")
    return _clone(dict(index))


__all__ = [
    "ChapterPriorityReviewError",
    "DEFAULT_MAX_PRIORITY_RANK",
    "DEFAULT_MAX_REVIEW_LEADS",
    "DEFAULT_MIN_RANKED_CHAPTERS",
    "INDEX_SCHEMA_VERSION",
    "INDEX_VALIDATOR_VERSION",
    "build_chapter_priority_review_index_v1",
    "verify_chapter_priority_review_index_v1",
]
