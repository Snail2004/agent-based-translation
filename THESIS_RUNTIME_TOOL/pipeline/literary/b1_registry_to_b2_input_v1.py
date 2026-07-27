"""Mechanical projection from audited B1 chapter registries to B2 input.

The adapter preserves candidate identity, claim status, provenance, and source
text.  It never merges identities, promotes book authority, or reinterprets
source language.
"""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence
import json
import unicodedata

from pipeline.literary.b1_chapter_registry_writer_v1 import (
    verify_b1_chapter_registry_v1,
)
from pipeline.literary.b1_cross_chapter_decision_ledger_v1 import (
    PROJECTION_SCHEMA_VERSION,
)
from pipeline.literary.b2_context_v1 import (
    B2ContextError,
    load_real_b1_run_input_v1,
)
from pipeline.literary.checkpoint import canonical_hash, canonical_json


ADAPTER_SCHEMA_VERSION = "literary_b1_registry_to_b2_input_v1"
PREFIX_SCHEMA_VERSION = "literary_b2_registry_prefix_projection_v1"
DOCUMENT_MANIFEST_SCHEMA_VERSION = "literary_b2_source_document_manifest_v1"
PACKAGE_FILENAME = "b2_registry_input.json"

CHAPTER_CONFIRMED_SCOPE = "chapter_confirmed_prefix"
CANDIDATE_ONLY_SCOPE = "candidate_only"


class B1RegistryToB2InputError(B2ContextError):
    pass


def build_b2_registry_input_package_v1(
    *,
    document: Mapping[str, Any],
    chapter_registries: Sequence[Mapping[str, Any]],
    current_git_head: str,
    reconciled_projection: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a self-contained, content-addressed B2 input package.

    ``reconciled_projection`` carries what the cross-chapter Auditor has already
    settled.  Without it the adapter behaves exactly as before - every shared
    surface is an open question - which is correct only while no hearing has
    been answered.  With it, a merged referent reaches B2 as one confirmed
    card, a case ruled distinct stops being flagged, and an unfinished hearing
    stays demoted with its reason attached.
    """

    chapters = _document_chapters(document)
    if not chapter_registries:
        raise B1RegistryToB2InputError("at least one B1 registry is required")
    git_head = _required_string(current_git_head, "current_git_head")

    chapter_manifest = [
        {
            "chapter_id": _required_string(row.get("chapter_id"), "chapter_id"),
            "source_chapter_hash": _source_chapter_hash(row),
        }
        for row in chapters
    ]
    document_manifest = {
        "schema_version": DOCUMENT_MANIFEST_SCHEMA_VERSION,
        "document_id": str(
            document.get("document_id")
            or document.get("id")
            or document.get("title")
            or "document"
        ),
        "chapters": chapter_manifest,
    }
    source_document_sha256 = canonical_hash(document_manifest)

    registry_rows: list[dict[str, Any]] = []
    for raw_registry in chapter_registries:
        registry = deepcopy(dict(raw_registry))
        _validate_registry_entity_ids_unique(registry)
        verify_b1_chapter_registry_v1(registry)
        registry_rows.append(registry)
    selected_ids = [
        _required_string(row.get("chapter_id"), "registry chapter_id")
        for row in registry_rows
    ]
    expected_ids = [row["chapter_id"] for row in chapter_manifest[: len(selected_ids)]]
    if selected_ids != expected_ids:
        raise B1RegistryToB2InputError(
            "B1 registries must exact-cover a contiguous document prefix"
        )

    chapters_by_id = {row["chapter_id"]: row for row in chapters}
    _validate_reconciled_projection_binding_v1(
        reconciled_projection,
        document_id=document_manifest["document_id"],
        registries=registry_rows,
        chapters=[chapters_by_id[chapter_id] for chapter_id in selected_ids],
    )
    cumulative: list[dict[str, Any]] = []
    package_chapters: list[dict[str, Any]] = []
    registry_manifest: list[dict[str, Any]] = []
    for ordinal, registry in enumerate(registry_rows, 1):
        chapter_id = registry["chapter_id"]
        chapter = chapters_by_id[chapter_id]
        if registry.get("lineage", {}).get("source_chapter_hash") != _source_chapter_hash(
            chapter
        ):
            raise B1RegistryToB2InputError(
                f"B1 registry source chapter differs: {chapter_id}"
            )
        cumulative.append(registry)
        prefix = _build_prefix_projection(
            cumulative, reconciled_projection=reconciled_projection
        )
        registry_manifest.append(
            {
                "chapter_id": chapter_id,
                "registry_hash": registry["registry_hash"],
                "registry_artifact_sha256": canonical_hash(registry),
            }
        )
        chapter_body = {
            "chapter_id": chapter_id,
            "chapter_ordinal": ordinal,
            "chapter": deepcopy(chapter),
            "source_chapter_hash": _source_chapter_hash(chapter),
            "source_registry": deepcopy(registry),
            "source_registry_hash": registry["registry_hash"],
            "prefix_bundle": prefix,
            "prefix_bundle_hash": prefix["prefix_bundle_hash"],
        }
        package_chapters.append(
            {**chapter_body, "chapter_report_hash": canonical_hash(chapter_body)}
        )

    body = {
        "schema_version": ADAPTER_SCHEMA_VERSION,
        "adapter_policy": {
            "identity_merge_performed": False,
            "semantic_claim_inference_performed": False,
            "chapter_authority_preserved": True,
            "identity_authority_granted": False,
            "book_authority_granted": False,
        },
        "source_document_manifest": document_manifest,
        "source_document_sha256": source_document_sha256,
        "source_run_git_head": git_head,
        "ordered_chapter_ids": selected_ids,
        "source_registry_manifest": registry_manifest,
        # The projection is stored so verification recomputes the same prefix.
        # Without it a package built from settled decisions could not be
        # re-verified, and the decisions would silently stop applying.
        "reconciled_projection": (
            deepcopy(dict(reconciled_projection)) if reconciled_projection else None
        ),
        "chapters": package_chapters,
        "historical_artifact_mutated": False,
        "database_mutation_performed": False,
        "production_publish_performed": False,
    }
    package = {**body, "package_hash": canonical_hash(body)}
    verify_b2_registry_input_package_v1(package)
    return package


def verify_b2_registry_input_package_v1(
    package: Mapping[str, Any],
) -> dict[str, Any]:
    if package.get("schema_version") != ADAPTER_SCHEMA_VERSION:
        raise B1RegistryToB2InputError("foreign B1-to-B2 package schema")
    expected_hash = _required_string(package.get("package_hash"), "package_hash")
    body = deepcopy(dict(package))
    body.pop("package_hash", None)
    if canonical_hash(body) != expected_hash:
        raise B1RegistryToB2InputError("B1-to-B2 package hash mismatch")
    policy = package.get("adapter_policy")
    if not isinstance(policy, Mapping) or policy != {
        "identity_merge_performed": False,
        "semantic_claim_inference_performed": False,
        "chapter_authority_preserved": True,
        "identity_authority_granted": False,
        "book_authority_granted": False,
    }:
        raise B1RegistryToB2InputError("B1-to-B2 adapter authority policy differs")
    if package.get("database_mutation_performed") is not False:
        raise B1RegistryToB2InputError("B1-to-B2 package claims database mutation")
    if package.get("production_publish_performed") is not False:
        raise B1RegistryToB2InputError("B1-to-B2 package claims publication")

    manifest = package.get("source_document_manifest")
    if not isinstance(manifest, Mapping):
        raise B1RegistryToB2InputError("source document manifest is absent")
    if manifest.get("schema_version") != DOCUMENT_MANIFEST_SCHEMA_VERSION:
        raise B1RegistryToB2InputError("foreign source document manifest")
    if canonical_hash(manifest) != package.get("source_document_sha256"):
        raise B1RegistryToB2InputError("source document manifest hash mismatch")
    manifest_rows = _mapping_sequence(manifest.get("chapters"), "manifest chapters")
    manifest_ids = [
        _required_string(row.get("chapter_id"), "manifest chapter_id")
        for row in manifest_rows
    ]
    rows = _mapping_sequence(package.get("chapters"), "package chapters")
    ordered_ids = [
        _required_string(value, "ordered_chapter_id")
        for value in package.get("ordered_chapter_ids") or []
    ]
    if not rows or ordered_ids != manifest_ids[: len(rows)]:
        raise B1RegistryToB2InputError("package chapters are not a source prefix")
    if [row.get("chapter_id") for row in rows] != ordered_ids:
        raise B1RegistryToB2InputError("package chapter order differs")

    binding_registries: list[Mapping[str, Any]] = []
    binding_chapters: list[Mapping[str, Any]] = []
    for row in rows:
        chapter = row.get("chapter")
        registry = row.get("source_registry")
        if not isinstance(chapter, Mapping) or not isinstance(registry, Mapping):
            raise B1RegistryToB2InputError("package chapter source is malformed")
        binding_chapters.append(chapter)
        binding_registries.append(registry)
    _validate_reconciled_projection_binding_v1(
        package.get("reconciled_projection"),
        document_id=_required_string(manifest.get("document_id"), "document_id"),
        registries=binding_registries,
        chapters=binding_chapters,
    )

    cumulative: list[dict[str, Any]] = []
    expected_registry_manifest: list[dict[str, Any]] = []
    for ordinal, row in enumerate(rows, 1):
        chapter = row.get("chapter")
        registry = row.get("source_registry")
        if not isinstance(chapter, Mapping) or not isinstance(registry, Mapping):
            raise B1RegistryToB2InputError("package chapter source is malformed")
        _validate_registry_entity_ids_unique(registry)
        verify_b1_chapter_registry_v1(registry)
        chapter_id = ordered_ids[ordinal - 1]
        source_chapter_hash = _source_chapter_hash(chapter)
        if (
            row.get("chapter_ordinal") != ordinal
            or row.get("source_chapter_hash") != source_chapter_hash
            or registry.get("chapter_id") != chapter_id
            or registry.get("lineage", {}).get("source_chapter_hash")
            != source_chapter_hash
        ):
            raise B1RegistryToB2InputError("package chapter lineage differs")
        if manifest_rows[ordinal - 1].get("source_chapter_hash") != source_chapter_hash:
            raise B1RegistryToB2InputError("package chapter differs from manifest")
        cumulative.append(deepcopy(dict(registry)))
        expected_prefix = _build_prefix_projection(
            cumulative, reconciled_projection=package.get("reconciled_projection")
        )
        if row.get("prefix_bundle") != expected_prefix:
            raise B1RegistryToB2InputError("B2 prefix projection differs")
        if row.get("prefix_bundle_hash") != expected_prefix["prefix_bundle_hash"]:
            raise B1RegistryToB2InputError("B2 prefix projection hash differs")
        chapter_body = {
            key: deepcopy(value)
            for key, value in row.items()
            if key != "chapter_report_hash"
        }
        if canonical_hash(chapter_body) != row.get("chapter_report_hash"):
            raise B1RegistryToB2InputError("package chapter report hash differs")
        expected_registry_manifest.append(
            {
                "chapter_id": chapter_id,
                "registry_hash": registry["registry_hash"],
                "registry_artifact_sha256": canonical_hash(registry),
            }
        )
    if package.get("source_registry_manifest") != expected_registry_manifest:
        raise B1RegistryToB2InputError("source registry manifest differs")
    return deepcopy(dict(package))


def write_b2_registry_input_package_v1(
    *, output_root: Path, package: Mapping[str, Any]
) -> Path:
    verified = verify_b2_registry_input_package_v1(package)
    root = Path(output_root).resolve()
    if root.exists():
        raise B1RegistryToB2InputError("B1-to-B2 output root already exists")
    root.mkdir(parents=True, exist_ok=False)
    target = root / PACKAGE_FILENAME
    target.write_text(
        json.dumps(verified, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return target


def load_b2_registry_input_package_v1(
    run_root: Path, *, current_git_head: str | None = None
) -> dict[str, Any]:
    root = Path(run_root).resolve()
    path = root / PACKAGE_FILENAME
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, TypeError, ValueError) as exc:
        raise B1RegistryToB2InputError("cannot read B1-to-B2 input package") from exc
    if not isinstance(raw, Mapping):
        raise B1RegistryToB2InputError("B1-to-B2 input package must be an object")
    package = verify_b2_registry_input_package_v1(raw)
    source_head = _required_string(
        package.get("source_run_git_head"), "source_run_git_head"
    )
    normalized_current = str(current_git_head or "").strip() or None
    blockers: list[str] = []
    if normalized_current is None:
        blockers.append("current_git_head_not_declared")
    elif normalized_current != source_head:
        blockers.append("source_run_head_differs_from_current_head")
    chapters = [
        {
            "chapter_id": row["chapter_id"],
            "chapter_ordinal": row["chapter_ordinal"],
            "chapter": deepcopy(row["chapter"]),
            "chapter_report_path": str(path),
            "chapter_report_hash": row["chapter_report_hash"],
            "prefix_path": str(path),
            "prefix_bundle": deepcopy(row["prefix_bundle"]),
            "prefix_bundle_hash": row["prefix_bundle_hash"],
        }
        for row in package["chapters"]
    ]
    summary_hash = canonical_hash(package["source_registry_manifest"])
    body = {
        "schema_version": "literary_b2_verified_input_v1",
        "source_run_root": str(root),
        "source_plan_hash": package["package_hash"],
        "source_summary_hash": summary_hash,
        "source_document_path": None,
        "source_document_sha256": package["source_document_sha256"],
        "source_run_git_heads": [source_head],
        "source_run_git_head": source_head,
        "current_git_head": normalized_current,
        "certification_eligible": not blockers,
        "certification_blockers": blockers,
        "ordered_chapter_ids": list(package["ordered_chapter_ids"]),
        "sealed_chapter_ids": list(package["ordered_chapter_ids"]),
        "chapters": chapters,
        "source_adapter_schema_version": ADAPTER_SCHEMA_VERSION,
        "source_adapter_package_hash": package["package_hash"],
        "historical_artifact_mutated": False,
    }
    return {**body, "input_hash": canonical_hash(body)}


def load_b2_source_input_v1(
    run_root: Path, *, current_git_head: str | None = None
) -> dict[str, Any]:
    """Load either the new registry package or the immutable legacy B1 run."""

    root = Path(run_root).resolve()
    if (root / PACKAGE_FILENAME).is_file():
        return load_b2_registry_input_package_v1(
            root, current_git_head=current_git_head
        )
    return load_real_b1_run_input_v1(root, current_git_head=current_git_head)


REOPEN_RULE = "cite a source block outside evidence_block_ids"


def _validate_reconciled_projection_binding_v1(
    projection: Mapping[str, Any] | None,
    *,
    document_id: str,
    registries: Sequence[Mapping[str, Any]],
    chapters: Sequence[Mapping[str, Any]],
) -> None:
    """Bind a reconciled view to the exact source prefix before applying it."""

    if projection is None:
        return
    if not isinstance(projection, Mapping):
        raise B1RegistryToB2InputError("reconciled projection is malformed")
    body = deepcopy(dict(projection))
    observed_hash = _required_string(
        body.pop("projection_hash", None), "projection_hash"
    )
    if body.get("schema_version") != PROJECTION_SCHEMA_VERSION:
        raise B1RegistryToB2InputError("foreign reconciled projection schema")
    if canonical_hash(body) != observed_hash:
        raise B1RegistryToB2InputError("reconciled projection hash mismatch")
    if projection.get("identity_authority_granted") is not False:
        raise B1RegistryToB2InputError(
            "reconciled projection grants identity authority"
        )
    if _required_string(projection.get("book_id"), "projection book_id") != document_id:
        raise B1RegistryToB2InputError(
            "reconciled projection belongs to a different document"
        )

    expected_registry_hashes = [
        _required_string(row.get("registry_hash"), "registry_hash")
        for row in registries
    ]
    source_registry_hashes = projection.get("source_registry_hashes")
    if (
        not isinstance(source_registry_hashes, list)
        or any(not isinstance(value, str) or not value for value in source_registry_hashes)
        or len(source_registry_hashes) != len(set(source_registry_hashes))
        or source_registry_hashes != expected_registry_hashes
    ):
        raise B1RegistryToB2InputError(
            "reconciled projection source registry hashes differ"
        )

    known_blocks = {
        _required_string(block.get("block_id"), "projection source block_id")
        for chapter in chapters
        for block in _mapping_sequence(chapter.get("blocks"), "projection source blocks")
    }
    known_card_ids = {
        _required_string(card.get("entity_id"), "projection source entity_id")
        for registry in registries
        for card in _mapping_sequence(registry.get("cards"), "projection source cards")
    }
    _validate_projection_references_v1(
        projection,
        known_blocks=known_blocks,
        known_card_ids=known_card_ids,
    )


def _validate_projection_references_v1(
    value: Any,
    *,
    known_blocks: set[str],
    known_card_ids: set[str],
    current_key: str | None = None,
) -> None:
    card_list_fields = {
        "member_card_ids",
        "card_ids",
        "candidate_set",
        "excluded_prior_card_ids",
    }
    card_scalar_fields = {"card_id", "prior_card_id", "effective_entity_id"}
    if current_key == "evidence_block_ids":
        if not isinstance(value, list) or any(
            not isinstance(item, str) or not item for item in value
        ):
            raise B1RegistryToB2InputError(
                "reconciled projection evidence_block_ids are malformed"
            )
        if len(value) != len(set(value)) or not set(value).issubset(known_blocks):
            raise B1RegistryToB2InputError(
                "reconciled projection cites a foreign evidence block"
            )
        return
    if current_key in card_list_fields:
        if not isinstance(value, list) or any(
            not isinstance(item, str) or not item for item in value
        ):
            raise B1RegistryToB2InputError(
                "reconciled projection card references are malformed"
            )
        if len(value) != len(set(value)) or not set(value).issubset(known_card_ids):
            raise B1RegistryToB2InputError(
                "reconciled projection cites a foreign registry card"
            )
        return
    if current_key in card_scalar_fields:
        if not isinstance(value, str) or value not in known_card_ids:
            raise B1RegistryToB2InputError(
                "reconciled projection cites a foreign registry card"
            )
        return
    if isinstance(value, Mapping):
        for key, child in value.items():
            _validate_projection_references_v1(
                child,
                known_blocks=known_blocks,
                known_card_ids=known_card_ids,
                current_key=str(key),
            )
    elif isinstance(value, list):
        for child in value:
            _validate_projection_references_v1(
                child,
                known_blocks=known_blocks,
                known_card_ids=known_card_ids,
                current_key=current_key,
            )


def _all_settled_together(
    card_ids: Iterable[str],
    resolution: Mapping[str, Mapping[str, Any]],
    merged_groups: Mapping[str, Sequence[str]],
) -> bool:
    """True when every card sharing this surface was ruled on as one case.

    Merged members answer to the same group; cards ruled ``settled_distinct``
    answer to each other.  Anything else - one settled card beside an unseen
    one, or a pending case - is still an open question and keeps its flag.
    """

    ids = sorted(set(card_ids))
    states = {resolution.get(card_id, {}).get("state") for card_id in ids}
    if states == {"settled_merged"}:
        return any(
            set(ids) <= set(members) for members in merged_groups.values()
        )
    if states == {"settled_distinct"}:
        return all(
            set(ids) - {card_id}
            <= set(resolution[card_id].get("distinct_from") or [])
            for card_id in ids
        )
    return False


def _resolution_index(
    projection: Mapping[str, Any] | None,
) -> tuple[dict[str, dict[str, Any]], dict[str, list[str]]]:
    """Index what the cross-chapter Auditor has already settled, by card id.

    Returns a resolution per card and, for merged groups, the members that now
    stand for one referent.  Every entry carries the exact blocks the verdict
    was based on: that set is what a later builder must reach outside of to
    reopen the case, so a settled question stops costing a hearing every
    chapter while a genuine new finding still gets one.
    """

    if not isinstance(projection, Mapping):
        return {}, {}
    resolution: dict[str, dict[str, Any]] = {}
    groups: dict[str, list[str]] = {}

    for entity in projection.get("effective_entities") or []:
        members = [m for m in entity.get("member_card_ids") or [] if isinstance(m, str)]
        refs = [r for r in entity.get("decision_refs") or [] if isinstance(r, str)]
        if len(members) < 2 or not refs:
            continue
        groups[_required_string(entity.get("effective_entity_id"), "effective id")] = members
        evidence = [b for b in entity.get("evidence_block_ids") or [] if isinstance(b, str)]
        for member in members:
            resolution[member] = {
                "state": "settled_merged",
                "ledger_entry_ids": sorted(refs),
                "evidence_block_ids": evidence,
                "member_card_ids": sorted(members),
                "reopen_rule": REOPEN_RULE,
            }

    for row in projection.get("resolved_distinct_cases") or []:
        if not isinstance(row, Mapping):
            continue
        entry_id = row.get("entry_id")
        evidence = [b for b in row.get("evidence_block_ids") or [] if isinstance(b, str)]
        for card_id in row.get("card_ids") or []:
            if not isinstance(card_id, str) or card_id in resolution:
                continue
            resolution[card_id] = {
                "state": "settled_distinct",
                "ledger_entry_ids": [entry_id] if entry_id else [],
                "evidence_block_ids": evidence,
                "distinct_from": sorted(
                    c for c in row.get("card_ids") or [] if c != card_id
                ),
                "decided_at_chapter": row.get("chapter_id"),
                "reason": row.get("reason"),
                "reopen_rule": REOPEN_RULE,
            }

    for row in projection.get("pending_cases") or []:
        if not isinstance(row, Mapping):
            continue
        entry_id = row.get("entry_id")
        evidence = [b for b in row.get("evidence_block_ids") or [] if isinstance(b, str)]
        for card_id in row.get("card_ids") or []:
            if not isinstance(card_id, str) or card_id in resolution:
                continue
            resolution[card_id] = {
                "state": "pending_evidence",
                "ledger_entry_ids": [entry_id] if entry_id else [],
                "evidence_block_ids": evidence,
                "decided_at_chapter": row.get("chapter_id"),
                "missing": row.get("reason"),
                "reopen_rule": REOPEN_RULE,
            }
    return resolution, groups


def _build_prefix_projection(
    registries: Sequence[Mapping[str, Any]],
    *,
    reconciled_projection: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    confirmed: dict[str, dict[str, Any]] = {}
    candidates: dict[str, dict[str, Any]] = {}
    snapshot_history: dict[str, list[tuple[str, dict[str, Any]]]] = {}
    covered_ids: list[str] = []
    for registry in registries:
        chapter_id = _required_string(registry.get("chapter_id"), "chapter_id")
        covered_ids.append(chapter_id)
        full_by_id = {
            _required_string(row.get("entity_id"), "entity_id"): row
            for row in _mapping_sequence(registry.get("cards"), "registry cards")
        }
        projection = registry.get("prior_cards_projection")
        if not isinstance(projection, Mapping):
            raise B1RegistryToB2InputError("registry prior-card projection is absent")
        for prior in _mapping_sequence(projection.get("cards"), "prior cards"):
            card_id = _required_string(prior.get("prior_card_id"), "prior_card_id")
            full = full_by_id.get(card_id)
            if full is None:
                raise B1RegistryToB2InputError("prior card cites a foreign entity")
            card = _project_card_for_b2(prior=prior, full=full, registry=registry)
            history = snapshot_history.setdefault(card_id, [])
            history.append((chapter_id, deepcopy(card)))
            card = _collapse_cross_chapter_snapshots(history)
            if card["authority_scope"] == CHAPTER_CONFIRMED_SCOPE:
                confirmed[card_id] = card
                candidates.pop(card_id, None)
            else:
                candidates[card_id] = card
                confirmed.pop(card_id, None)

    resolution, merged_groups = _resolution_index(reconciled_projection)
    # A settled verdict travels on the card so B2 and B3 can tell an answered
    # question from an open one, and know what to cite if they want it reopened.
    for card_id, card in {**confirmed, **candidates}.items():
        settled = resolution.get(card_id)
        if settled is None:
            continue
        card["identity_resolution"] = deepcopy(settled)
        if settled["state"] == "pending_evidence":
            # An unfinished hearing is not a fact. Relabelling is not enough:
            # the card must actually leave the set B2 is allowed to rely on.
            card["authority_scope"] = CANDIDATE_ONLY_SCOPE
            confirmed.pop(card_id, None)
            candidates[card_id] = card
            if not any(
                row.get("disputed_field") == "identity_membership"
                for row in card["disputed_claims"]
            ):
                card["disputed_claims"].append(
                    _dispute(
                        field="identity_membership",
                        value=None,
                        registry_hash=None,
                        reason="cross_chapter_hearing_awaiting_evidence",
                    )
                )
    # Deliberately no promotion here.  A card sits in candidates for a reason
    # of its own - a provisional claim, say - and settling its identity says
    # nothing about that reason. Promoting on an identity verdict would hand it
    # an authority no one granted. A card whose only problem was the surface
    # collision simply stops being demoted below, which is enough.
    all_cards = {**confirmed, **candidates}
    owners: dict[str, set[str]] = {}
    display: dict[str, str] = {}
    for card_id, card in all_cards.items():
        for surface in card["stable_surfaces"]:
            key = _surface_key(surface)
            if key:
                owners.setdefault(key, set()).add(card_id)
                display.setdefault(key, surface)
    uncertainties: list[dict[str, Any]] = []
    for surface_key, card_ids in sorted(owners.items()):
        if len(card_ids) < 2:
            continue
        # A shared surface among cards the Auditor already ruled on is not an
        # open question. Flagging it again would make B2 doubt a settled answer
        # and would put the same case back in the queue every chapter.
        if _all_settled_together(card_ids, resolution, merged_groups):
            continue
        uncertainty_body = {
            "surface_key": surface_key,
            "source_surface": display[surface_key],
            "prior_card_ids": sorted(card_ids),
            "chapter_ids": sorted(
                {
                    ref["chapter_id"]
                    for card_id in card_ids
                    for ref in all_cards[card_id]["provenance_refs"]
                }
            ),
            "status": "pending_identity_review",
            "authority_effect": CANDIDATE_ONLY_SCOPE,
            "reason_code": "surface_collision_requires_semantic_review",
        }
        uncertainty = {
            "uncertainty_id": "b2prefixunc1_" + canonical_hash(uncertainty_body)[:20],
            **uncertainty_body,
        }
        uncertainties.append(uncertainty)
        for card_id in card_ids:
            card = confirmed.pop(card_id, None)
            if card is None:
                card = candidates[card_id]
            moved = deepcopy(card)
            moved["authority_scope"] = CANDIDATE_ONLY_SCOPE
            if not any(
                row.get("disputed_field") == "identity_membership"
                for row in moved["disputed_claims"]
            ):
                moved["disputed_claims"].append(
                    _dispute(
                        field="identity_membership",
                        value=None,
                        registry_hash=None,
                        reason="surface_collision_requires_semantic_review",
                    )
                )
            candidates[card_id] = moved

    body = {
        "schema_version": PREFIX_SCHEMA_VERSION,
        "coverage_through_chapter_id": covered_ids[-1],
        "covered_chapter_ids": covered_ids,
        "b0_context_cards": sorted(
            confirmed.values(), key=lambda row: row["prior_card_id"]
        ),
        "candidate_only_context_cards": sorted(
            candidates.values(), key=lambda row: row["prior_card_id"]
        ),
        "prefix_identity_uncertainties": uncertainties,
        "claim_transition_coverage": "not_materialized_by_b1_registry_adapter_v1",
        "identity_merge_performed": False,
        "book_authority_granted": False,
    }
    return {**body, "prefix_bundle_hash": canonical_hash(body)}


def _project_card_for_b2(
    *, prior: Mapping[str, Any], full: Mapping[str, Any], registry: Mapping[str, Any]
) -> dict[str, Any]:
    effective: dict[str, Any] = {}
    non_authoritative_context: dict[str, Any] = {
        "identity_summary": prior.get("identity_summary"),
        "presence_basis": prior.get("presence_basis"),
    }
    context_statuses: dict[str, str] = {
        "identity_summary": str(
            (full.get("identity_summary") or {}).get("semantic_status")
            or "provisional"
        ),
        "presence_basis": "observed_context",
    }
    disputes: list[dict[str, Any]] = []
    referent_kind = full.get("referent_kind")
    if isinstance(referent_kind, Mapping) and referent_kind.get("effective") is True:
        effective["referent_kind"] = deepcopy(referent_kind.get("value"))
    else:
        non_authoritative_context["referent_kind"] = prior.get("referent_kind")
        context_statuses["referent_kind"] = str(
            (referent_kind or {}).get("semantic_status") or "provisional"
        )
    effective_values: dict[str, list[Any]] = {}
    for claim in _mapping_sequence(full.get("claims"), "card claims"):
        field = _required_string(claim.get("field"), "claim field")
        if claim.get("effective") is True:
            values = effective_values.setdefault(field, [])
            if not any(canonical_json(value) == canonical_json(claim.get("value")) for value in values):
                values.append(deepcopy(claim.get("value")))
        else:
            disputes.append(
                _dispute(
                    field=field,
                    value=claim.get("value"),
                    registry_hash=registry.get("registry_hash"),
                    reason="chapter_provisional_claim",
                )
            )
    for field, values in sorted(effective_values.items()):
        if len(values) == 1:
            effective[field] = values[0]
            if field == "gender":
                effective["referential_gender"] = values[0]
        else:
            effective.pop(field, None)
            disputes.append(
                _dispute(
                    field=field,
                    value=values,
                    registry_hash=registry.get("registry_hash"),
                    reason="conflicting_effective_claims",
                )
            )

    provisional = (
        prior.get("claim_state") != "confirmed"
        or full.get("chapter_authority") is not True
        or full.get("record_class")
        in {"important_unnamed_referent", "unresolved_named_reference"}
    )
    if provisional and not any(
        row.get("disputed_field") == "identity_membership" for row in disputes
    ):
        disputes.append(
            _dispute(
                field="identity_membership",
                value=None,
                registry_hash=registry.get("registry_hash"),
                reason="chapter_identity_not_confirmed",
            )
        )
    return {
        "prior_card_id": _required_string(prior.get("prior_card_id"), "prior_card_id"),
        "canonical_surface": _required_string(
            prior.get("canonical_surface"), "canonical_surface"
        ),
        "stable_surfaces": _string_list(prior.get("stable_surfaces"), "stable surfaces"),
        "effective_claims": effective,
        "non_authoritative_context_claims": non_authoritative_context,
        "non_authoritative_context_statuses": context_statuses,
        "authority_scope": (
            CANDIDATE_ONLY_SCOPE if provisional else CHAPTER_CONFIRMED_SCOPE
        ),
        "first_supported_block_id": prior.get("first_supported_block_id"),
        "provenance_refs": deepcopy(list(prior.get("provenance_refs") or [])),
        "disputed_claims": disputes,
        "source_registry_hash": registry.get("registry_hash"),
        "identity_authority": False,
        "book_authority": False,
    }


def _collapse_cross_chapter_snapshots(
    history: Sequence[tuple[str, Mapping[str, Any]]],
) -> dict[str, Any]:
    """Keep one current card while preserving every chapter-bound snapshot.

    Reusing a persistent id is already an upstream identity decision. This
    helper does not merge different ids or reconcile claims: the latest
    snapshot remains the current view, while stable surfaces and provenance
    are mechanically retained so earlier chapter evidence is not discarded.
    """

    if not history:
        raise B1RegistryToB2InputError("entity snapshot history is empty")
    current = deepcopy(dict(history[-1][1]))
    stable_surfaces: list[str] = []
    seen_surfaces: set[str] = set()
    provenance_refs: list[dict[str, Any]] = []
    seen_provenance: set[str] = set()
    first_supported_block_id: Any = None
    snapshots: list[dict[str, Any]] = []
    seen_chapters: set[str] = set()

    for chapter_id, raw_card in history:
        normalized_chapter = _required_string(chapter_id, "snapshot chapter_id")
        if normalized_chapter in seen_chapters:
            raise B1RegistryToB2InputError(
                "entity id repeats within one chapter snapshot history"
            )
        seen_chapters.add(normalized_chapter)
        card = deepcopy(dict(raw_card))
        for surface in card.get("stable_surfaces") or []:
            normalized_surface = _surface_key(surface)
            if normalized_surface and normalized_surface not in seen_surfaces:
                stable_surfaces.append(str(surface))
                seen_surfaces.add(normalized_surface)
        for raw_ref in card.get("provenance_refs") or []:
            if not isinstance(raw_ref, Mapping):
                raise B1RegistryToB2InputError(
                    "entity snapshot provenance row is malformed"
                )
            ref = deepcopy(dict(raw_ref))
            ref_key = canonical_json(ref)
            if ref_key not in seen_provenance:
                provenance_refs.append(ref)
                seen_provenance.add(ref_key)
        if first_supported_block_id is None and card.get(
            "first_supported_block_id"
        ) not in (None, ""):
            first_supported_block_id = card["first_supported_block_id"]
        snapshots.append({"chapter_id": normalized_chapter, "card": card})

    current["stable_surfaces"] = stable_surfaces
    current["provenance_refs"] = provenance_refs
    current["first_supported_block_id"] = first_supported_block_id
    # Keep the byte shape of valid one-chapter historical packages unchanged.
    # No successful multi-chapter package existed before persistent-id reuse.
    if len(snapshots) > 1:
        current["chapter_snapshots"] = snapshots
    return current


def _validate_registry_entity_ids_unique(registry: Mapping[str, Any]) -> None:
    """Reject duplicate ids inside one chapter, not across chapter snapshots."""

    seen: set[str] = set()
    for raw_card in _mapping_sequence(registry.get("cards"), "registry cards"):
        entity_id = _required_string(raw_card.get("entity_id"), "entity_id")
        if entity_id in seen:
            raise B1RegistryToB2InputError(
                "registry repeats an entity id within one chapter"
            )
        seen.add(entity_id)


def _dispute(
    *, field: str, value: Any, registry_hash: Any, reason: str
) -> dict[str, Any]:
    return {
        "disputed_field": field,
        "historical_value": deepcopy(value),
        "status": "pending",
        "pending_reason_codes": [reason],
        "evidence_manifest_hashes": (
            [registry_hash] if isinstance(registry_hash, str) else []
        ),
        "hearing_count": 0,
        "automatic_hearing_limit": 2,
        "same_evidence_reopen_forbidden": True,
        "next_review_trigger": "new_source_evidence_or_identity_review",
        "revision_ids": [],
    }


def _source_chapter_hash(chapter: Mapping[str, Any]) -> str:
    chapter_id = _required_string(chapter.get("chapter_id"), "chapter_id")
    blocks = _mapping_sequence(chapter.get("blocks"), "chapter blocks")
    projected: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, block in enumerate(blocks):
        block_id = _required_string(block.get("block_id"), "block_id")
        if block_id in seen:
            raise B1RegistryToB2InputError("chapter block ids are duplicated")
        seen.add(block_id)
        order = block.get("order_index")
        if not isinstance(order, int):
            order = index
        projected.append(
            {
                "block_id": block_id,
                "order_index": order,
                "text": str(block.get("clean_text") or block.get("text") or ""),
            }
        )
    return canonical_hash({"chapter_id": chapter_id, "blocks": projected})


def _document_chapters(document: Mapping[str, Any]) -> list[dict[str, Any]]:
    rows = _mapping_sequence(document.get("chapters"), "document chapters")
    if not rows:
        raise B1RegistryToB2InputError("document has no chapters")
    result = [deepcopy(dict(row)) for row in rows]
    ids = [_required_string(row.get("chapter_id"), "chapter_id") for row in result]
    if len(ids) != len(set(ids)):
        raise B1RegistryToB2InputError("document chapter ids are duplicated")
    return result


def _mapping_sequence(value: Any, label: str) -> list[Mapping[str, Any]]:
    if not isinstance(value, list):
        raise B1RegistryToB2InputError(f"{label} must be an array")
    if any(not isinstance(row, Mapping) for row in value):
        raise B1RegistryToB2InputError(f"{label} contains a malformed row")
    return list(value)


def _string_list(value: Any, label: str) -> list[str]:
    if not isinstance(value, list) or not value:
        raise B1RegistryToB2InputError(f"{label} must be a non-empty array")
    rows = [_required_string(row, label) for row in value]
    if len({_surface_key(row) for row in rows}) != len(rows):
        raise B1RegistryToB2InputError(f"{label} contains duplicates")
    return rows


def _surface_key(value: Any) -> str:
    return " ".join(unicodedata.normalize("NFKC", str(value)).casefold().split())


def _required_string(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise B1RegistryToB2InputError(f"{label} must be a non-empty string")
    return value


__all__ = [
    "ADAPTER_SCHEMA_VERSION",
    "B1RegistryToB2InputError",
    "PACKAGE_FILENAME",
    "build_b2_registry_input_package_v1",
    "load_b2_registry_input_package_v1",
    "load_b2_source_input_v1",
    "verify_b2_registry_input_package_v1",
    "write_b2_registry_input_package_v1",
]
