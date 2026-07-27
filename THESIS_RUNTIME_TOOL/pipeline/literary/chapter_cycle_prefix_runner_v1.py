"""Deterministic N-chapter prefix assembly for the literary entity cycle."""

from __future__ import annotations

from copy import deepcopy
from typing import Any, Mapping, Sequence

from pipeline.literary.chapter_prefix_prior_v1 import (
    apply_claim_projection_to_prefix_bundle_v1,
    build_chapter_prefix_prior_bundle_v1,
    extend_chapter_prefix_prior_bundle_v1,
    verify_chapter_prefix_prior_bundle_v1,
)
from pipeline.literary.checkpoint import canonical_hash


ASSEMBLY_SCHEMA_VERSION = "chapter_cycle_prefix_assembly_v1"
ASSEMBLY_VALIDATOR_VERSION = "chapter_cycle_prefix_assembly_validator_v1"


class ChapterCyclePrefixRunnerError(RuntimeError):
    pass


def _clone(value: Any) -> Any:
    return deepcopy(value)


def _required_string(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ChapterCyclePrefixRunnerError(f"{label} must be a non-empty string")
    return value


def _document_chapter_ids(document: Mapping[str, Any]) -> list[str]:
    chapters = document.get("chapters")
    if not isinstance(chapters, list) or not chapters:
        raise ChapterCyclePrefixRunnerError("document has no chapters")
    result = [_required_string(row.get("chapter_id"), "chapter_id") for row in chapters]
    if len(result) != len(set(result)):
        raise ChapterCyclePrefixRunnerError("document repeats a chapter id")
    return result


def assemble_chapter_cycle_prefix_v1(
    *,
    document: Mapping[str, Any],
    audited_inventories: Mapping[str, Mapping[str, Any]],
    ordered_chapter_ids: Sequence[str] | None = None,
    claim_projections_before_chapter: Mapping[str, Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """Replay a contiguous book prefix from immutable chapter artifacts.

    A projection keyed by chapter N is applied to the existing prefix before
    chapter N is appended.  The function performs no model call and no publish.
    """

    document_order = _document_chapter_ids(document)
    selected = list(ordered_chapter_ids or document_order)
    if not selected or selected != document_order[: len(selected)]:
        raise ChapterCyclePrefixRunnerError(
            "chapter-cycle assembly must be a contiguous prefix from chapter one"
        )
    if len(selected) != len(set(selected)):
        raise ChapterCyclePrefixRunnerError("chapter-cycle assembly repeats a chapter")
    if set(audited_inventories) != set(selected):
        raise ChapterCyclePrefixRunnerError(
            "audited inventories do not exact-cover the selected chapter prefix"
        )
    projections = dict(claim_projections_before_chapter or {})
    if set(projections) - set(selected[1:]):
        raise ChapterCyclePrefixRunnerError(
            "claim projection targets the first or a foreign chapter"
        )

    first = selected[0]
    prefix = build_chapter_prefix_prior_bundle_v1(
        document=document,
        audited_inventory=audited_inventories[first],
        coverage_through_chapter_id=first,
    )
    receipts: list[dict[str, Any]] = [
        {
            "chapter_id": first,
            "action": "initialize_prefix",
            "input_inventory_hash": audited_inventories[first][
                "conflict_audited_inventory_hash"
            ],
            "claim_projection_hash": None,
            "result_prefix_hash": prefix["prefix_bundle_hash"],
        }
    ]
    for chapter_id in selected[1:]:
        projection = projections.get(chapter_id)
        projection_hash = None
        if projection is not None:
            prefix = apply_claim_projection_to_prefix_bundle_v1(
                bundle=prefix,
                projection=projection,
            )
            projection_hash = _required_string(
                projection.get("projection_hash"), "projection_hash"
            )
        prefix = extend_chapter_prefix_prior_bundle_v1(
            bundle=prefix,
            document=document,
            audited_inventory=audited_inventories[chapter_id],
            next_chapter_id=chapter_id,
        )
        receipts.append(
            {
                "chapter_id": chapter_id,
                "action": "project_then_extend",
                "input_inventory_hash": audited_inventories[chapter_id][
                    "conflict_audited_inventory_hash"
                ],
                "claim_projection_hash": projection_hash,
                "result_prefix_hash": prefix["prefix_bundle_hash"],
            }
        )
    verified_prefix = verify_chapter_prefix_prior_bundle_v1(prefix, document=document)
    full_coverage = selected == document_order
    body = {
        "schema_version": ASSEMBLY_SCHEMA_VERSION,
        "validator_version": ASSEMBLY_VALIDATOR_VERSION,
        "state_lineage_id": verified_prefix["state_lineage_id"],
        "ordered_chapter_ids": selected,
        "transition_receipts": receipts,
        "final_prefix_bundle": verified_prefix,
        "full_book_coverage": full_coverage,
        "book_end_handoff_allowed": full_coverage,
        "b2_ready": False,
        "production_publish_performed": False,
    }
    return {**body, "assembly_hash": canonical_hash(body)}


def verify_chapter_cycle_prefix_assembly_v1(
    assembly: Mapping[str, Any],
    *,
    document: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    if assembly.get("schema_version") != ASSEMBLY_SCHEMA_VERSION:
        raise ChapterCyclePrefixRunnerError("foreign chapter-cycle assembly schema")
    if assembly.get("validator_version") != ASSEMBLY_VALIDATOR_VERSION:
        raise ChapterCyclePrefixRunnerError("chapter-cycle assembly validator mismatch")
    body = dict(assembly)
    observed = _required_string(body.pop("assembly_hash", None), "assembly_hash")
    if canonical_hash(body) != observed:
        raise ChapterCyclePrefixRunnerError("chapter-cycle assembly hash mismatch")
    if assembly.get("b2_ready") is not False:
        raise ChapterCyclePrefixRunnerError("prefix assembly claims B2 readiness")
    if assembly.get("production_publish_performed") is not False:
        raise ChapterCyclePrefixRunnerError("prefix assembly claims publication")
    prefix = verify_chapter_prefix_prior_bundle_v1(
        assembly.get("final_prefix_bundle"), document=document
    )
    chapter_ids = assembly.get("ordered_chapter_ids")
    if not isinstance(chapter_ids, list) or chapter_ids != prefix["covered_chapter_ids"]:
        raise ChapterCyclePrefixRunnerError("assembly coverage differs from final prefix")
    receipts = assembly.get("transition_receipts")
    if not isinstance(receipts, list) or len(receipts) != len(chapter_ids):
        raise ChapterCyclePrefixRunnerError("assembly transition receipts are incomplete")
    if [row.get("chapter_id") for row in receipts] != chapter_ids:
        raise ChapterCyclePrefixRunnerError("assembly transition order drifted")
    if receipts[-1].get("result_prefix_hash") != prefix["prefix_bundle_hash"]:
        raise ChapterCyclePrefixRunnerError("assembly final receipt is stale")
    if document is not None:
        document_order = _document_chapter_ids(document)
        full = chapter_ids == document_order
        if assembly.get("full_book_coverage") is not full:
            raise ChapterCyclePrefixRunnerError("assembly full-coverage flag is stale")
        if assembly.get("book_end_handoff_allowed") is not full:
            raise ChapterCyclePrefixRunnerError("assembly book-end gate is stale")
    return _clone(dict(assembly))


__all__ = [
    "ASSEMBLY_SCHEMA_VERSION",
    "ASSEMBLY_VALIDATOR_VERSION",
    "ChapterCyclePrefixRunnerError",
    "assemble_chapter_cycle_prefix_v1",
    "verify_chapter_cycle_prefix_assembly_v1",
]
