"""Whole-book entity index, bounded adjudication, and immutable registry projection."""

from __future__ import annotations

from collections import defaultdict
from copy import deepcopy
from dataclasses import dataclass
import json
from pathlib import Path
import re
import unicodedata
from typing import Any, Iterable, Mapping, Sequence

from pipeline.literary.book_source_lineage import (
    build_book_source_manifest,
    state_lineage_id_for_manifest,
    verify_book_source_manifest,
)
from pipeline.literary.book_entity_registry_prompts_v1 import (
    PROMPT_ID,
    PROMPT_SHA256,
    load_book_entity_prompt_v1,
)
from pipeline.literary.checkpoint import canonical_hash, canonical_json


BOOK_INDEX_SCHEMA_VERSION = "book_entity_index_v2"
BOOK_REGISTRY_SCHEMA_VERSION = "global_entity_registry_v2"
BOOK_VALIDATOR_VERSION = "book_entity_registry_validator_v2"
DEFAULT_MAX_COMPONENT_CANDIDATES = 64
DEFAULT_MAX_COMPONENT_SOURCE_BLOCKS = 96
DEFAULT_MAX_REVIEW_BLOCKS_PER_CANDIDATE = 4
DEFAULT_MAX_GIST_UTF8_BYTES = 6000
CANDIDATE_ACTIONS = frozenset({"keep", "merge_into", "split", "pending", "reject"})
SURFACE_ACTIONS = frozenset(
    {"promote_book_global", "retain_chapter_scoped", "quarantine"}
)
STABLE_NAME_CLASSES = frozenset({"proper_name", "stable_nickname", "title_plus_name"})
HONORIFIC_PREFIXES = frozenset(
    {"mr", "mrs", "miss", "ms", "dr", "sir", "lady", "lord", "capt", "captain"}
)


class BookEntityRegistryError(RuntimeError):
    """Base error for the whole-book entity boundary."""


class BookEntityContractError(BookEntityRegistryError):
    """Raised when source, response, or snapshot violates the closed contract."""


@dataclass(frozen=True)
class RenderedBookEntityRequestV1:
    component_id: str
    request_fingerprint: str
    messages: tuple[dict[str, str], ...]
    response_schema: dict[str, Any]
    semantic_payload: dict[str, Any]


def _clone(value: Any) -> Any:
    return deepcopy(value)


def _required_string(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise BookEntityContractError(f"{label} must be a non-empty string")
    return value


def _exact_keys(value: Mapping[str, Any], expected: set[str], label: str) -> None:
    actual = set(value)
    if actual != expected:
        raise BookEntityContractError(
            f"{label} field set differs; missing={sorted(expected-actual)}, "
            f"foreign={sorted(actual-expected)}"
        )


def _string_list(value: Any, label: str, *, allow_empty: bool = False) -> list[str]:
    if not isinstance(value, list) or (not value and not allow_empty):
        qualifier = "possibly empty" if allow_empty else "non-empty"
        raise BookEntityContractError(f"{label} must be a {qualifier} list")
    rows = [_required_string(item, label) for item in value]
    if len(rows) != len(set(rows)):
        raise BookEntityContractError(f"{label} contains duplicates")
    return rows


def _normalized_surface(value: str) -> str:
    text = unicodedata.normalize("NFC", value).casefold()
    text = re.sub(r"[^\w'-]+", " ", text, flags=re.UNICODE)
    return " ".join(text.split())


def _title_core(surface: str, name_class: Any) -> str | None:
    normalized = _normalized_surface(surface)
    tokens = re.findall(r"[\w'-]+", normalized, flags=re.UNICODE)
    if not tokens:
        return None
    first = tokens[0].rstrip(".")
    if len(tokens) >= 2 and (
        name_class == "title_plus_name" or first in HONORIFIC_PREFIXES
    ):
        return " ".join(tokens[1:]) or None
    if len(tokens) == 1 and name_class in STABLE_NAME_CLASSES:
        return tokens[0]
    return None


def _block_text(block: Mapping[str, Any]) -> str:
    return str(block.get("clean_text") or block.get("source_text") or "")


def _document_catalog(document: Mapping[str, Any]) -> tuple[
    list[str], dict[str, dict[str, Any]], dict[str, str], dict[str, int]
]:
    chapter_order: list[str] = []
    chapters: dict[str, dict[str, Any]] = {}
    block_chapter: dict[str, str] = {}
    block_order: dict[str, int] = {}
    offset = 0
    for raw_chapter in document.get("chapters") or []:
        if not isinstance(raw_chapter, Mapping):
            raise BookEntityContractError("document chapter must be an object")
        chapter = dict(raw_chapter)
        chapter_id = _required_string(chapter.get("chapter_id"), "document.chapter_id")
        if chapter_id in chapters:
            raise BookEntityContractError("document contains duplicate chapter ids")
        blocks = []
        for raw_block in chapter.get("blocks") or []:
            if not isinstance(raw_block, Mapping):
                raise BookEntityContractError("document block must be an object")
            block_id = _required_string(raw_block.get("block_id"), "document.block_id")
            if block_id in block_chapter:
                raise BookEntityContractError("document contains duplicate block ids")
            text = _block_text(raw_block)
            blocks.append(
                {
                    "block_id": block_id,
                    "order_index": int(raw_block.get("order_index") or len(blocks) + 1),
                    "text": text,
                }
            )
            block_chapter[block_id] = chapter_id
            block_order[block_id] = offset
            offset += 1
        if not blocks:
            raise BookEntityContractError(f"chapter has no blocks: {chapter_id}")
        chapter_order.append(chapter_id)
        chapters[chapter_id] = {"chapter_id": chapter_id, "blocks": blocks}
    if not chapter_order:
        raise BookEntityContractError("document has no chapters")
    return chapter_order, chapters, block_chapter, block_order


def _coerce_chapter_payloads(
    values: Mapping[str, Mapping[str, Any]] | Sequence[Mapping[str, Any]],
    *,
    label: str,
) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    if isinstance(values, Mapping):
        iterator = [(str(key), value) for key, value in values.items()]
    elif isinstance(values, Sequence) and not isinstance(values, (str, bytes)):
        iterator = []
        for value in values:
            if not isinstance(value, Mapping):
                raise BookEntityContractError(f"{label} row must be an object")
            chapter_id = _required_string(value.get("chapter_id"), f"{label}.chapter_id")
            iterator.append((chapter_id, value))
    else:
        raise BookEntityContractError(f"{label} must be chapter keyed or a sequence")
    for key, raw in iterator:
        if not isinstance(raw, Mapping):
            raise BookEntityContractError(f"{label} payload must be an object")
        payload = dict(raw)
        embedded = payload.get("chapter_id")
        if embedded is not None and str(embedded) != key:
            raise BookEntityContractError(f"{label} chapter key disagrees with payload")
        if key in result:
            raise BookEntityContractError(f"duplicate {label} chapter: {key}")
        result[key] = payload
    return result


def _verify_audited_inventory(inventory: Mapping[str, Any], chapter_id: str) -> str:
    if inventory.get("chapter_id") != chapter_id:
        raise BookEntityContractError("audited inventory belongs to a foreign chapter")
    observed = _required_string(
        inventory.get("conflict_audited_inventory_hash"),
        "conflict_audited_inventory_hash",
    )
    body = dict(inventory)
    body.pop("conflict_audited_inventory_hash", None)
    if canonical_hash(body) != observed:
        raise BookEntityContractError("audited inventory hash mismatch")
    _required_string(inventory.get("source_inventory_hash"), "source_inventory_hash")
    _required_string(inventory.get("request_fingerprint"), "request_fingerprint")
    return observed


def _open_validation_report(
    inventory: Mapping[str, Any], chapter_id: str, inventory_hash: str
) -> dict[str, Any] | None:
    raw = inventory.get("source_validation_report")
    if raw is None:
        return None
    if not isinstance(raw, Mapping):
        raise BookEntityContractError("source validation report must be an object")
    issue_fields = (
        "rejected_rows",
        "claim_issues",
        "alternative_name_issues",
        "canonical_name_class_issues",
        "unlocated_surfaces",
    )
    issues: dict[str, list[Any]] = {}
    for field in issue_fields:
        value = raw.get(field) or []
        if not isinstance(value, list):
            raise BookEntityContractError(
                f"source validation report {field} must be a list"
            )
        if value:
            issues[field] = _clone(value)
    if not issues:
        return None
    body = {
        "chapter_id": chapter_id,
        "conflict_audited_inventory_hash": inventory_hash,
        "lifecycle_state": "open_validation_quarantine",
        "authority_state": "not_authoritative",
        "issues": issues,
    }
    return {
        "validation_quarantine_id": "bkvalq_" + canonical_hash(body)[:20],
        **body,
    }


def _orientation_gist(orientation: Mapping[str, Any], chapter_id: str) -> tuple[str, str]:
    embedded = orientation.get("chapter_id")
    if embedded is not None and str(embedded) != chapter_id:
        raise BookEntityContractError("orientation belongs to a foreign chapter")
    gist = orientation.get("orientation_draft", orientation.get("gist"))
    gist = _required_string(gist, "orientation gist")
    observed = orientation.get("orientation_hash")
    body = dict(orientation)
    body.pop("orientation_hash", None)
    computed = canonical_hash(body)
    if observed is not None and str(observed) != computed:
        raise BookEntityContractError("orientation hash mismatch")
    return gist, str(observed or computed)


def _candidate_ref(lineage_id: str, chapter_id: str, local_id: str) -> str:
    return "bkcand_" + canonical_hash(
        {
            "state_lineage_id": lineage_id,
            "chapter_id": chapter_id,
            "local_candidate_id": local_id,
        }
    )[:20]


def _surface_claims(row: Mapping[str, Any]) -> list[dict[str, Any]]:
    locations = [
        dict(item) for item in row.get("name_locations") or [] if isinstance(item, Mapping)
    ]
    canonical = str(row.get("canonical_surface") or "")
    fallback = list(row.get("source_block_ids") or [])
    claims: list[dict[str, Any]] = []
    if canonical:
        canonical_location = next(
            (item for item in locations if item.get("surface") == canonical),
            {},
        )
        canonical_blocks = list(
            canonical_location.get("source_block_ids") or fallback
        )
        canonical_address_state = canonical_location.get(
            "address_validation_state",
            "valid" if row.get("surface_status") == "located" else "unknown",
        )
        claims.append(
            {
                "surface": canonical,
                "surface_key": _normalized_surface(canonical),
                "name_class": row.get("canonical_name_class"),
                "source_block_ids": canonical_blocks,
                "surface_match_block_ids": list(
                    canonical_location.get("surface_match_block_ids") or []
                ),
                "address_validation_state": canonical_address_state,
                "address_issues": list(
                    canonical_location.get("address_issues") or []
                ),
                "ownership_state": canonical_location.get(
                    "ownership_state", "legacy_unclassified"
                ),
                "is_canonical": True,
                "source_located": (
                    canonical_address_state == "valid" and bool(canonical_blocks)
                ),
            }
        )
    for raw in row.get("alternative_names") or []:
        if not isinstance(raw, Mapping):
            continue
        surface = str(raw.get("surface") or "")
        if not surface:
            continue
        claims.append(
            {
                "surface": surface,
                "surface_key": _normalized_surface(surface),
                "name_class": raw.get("name_class"),
                "source_block_ids": list(raw.get("source_block_ids") or []),
                "surface_match_block_ids": list(
                    raw.get("surface_match_block_ids") or []
                ),
                "address_validation_state": raw.get(
                    "address_validation_state", "legacy_unclassified"
                ),
                "address_issues": list(raw.get("address_issues") or []),
                "ownership_state": raw.get(
                    "ownership_state", "legacy_unclassified"
                ),
                "is_canonical": False,
                "source_located": (
                    raw.get("address_validation_state", "valid") == "valid"
                    and bool(raw.get("source_block_ids"))
                ),
            }
        )
    unique: dict[tuple[Any, ...], dict[str, Any]] = {}
    for claim in claims:
        key = (
            claim["surface"],
            claim["name_class"],
            tuple(sorted(set(claim["source_block_ids"]))),
            tuple(sorted(set(claim["surface_match_block_ids"]))),
            claim["address_validation_state"],
            tuple(claim["address_issues"]),
            claim["ownership_state"],
            claim["is_canonical"],
            claim["source_located"],
        )
        unique[key] = claim
    return sorted(
        unique.values(),
        key=lambda item: (
            item["surface_key"],
            not item["is_canonical"],
            str(item["name_class"]),
        ),
    )


def _has_located_canonical(candidate: Mapping[str, Any]) -> bool:
    return any(
        row.get("is_canonical") and row.get("source_located")
        for row in candidate.get("surface_claims") or []
    )


def _claim_value(row: Mapping[str, Any], field: str) -> Any:
    value = row.get(field)
    return value.get("value") if isinstance(value, Mapping) else value


def _stable_claim_payload(row: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "canonical_surface": row.get("canonical_surface"),
        "canonical_name_class": row.get("canonical_name_class"),
        "alternative_names": sorted(
            [
                {
                    "surface": item.get("surface"),
                    "name_class": item.get("name_class"),
                }
                for item in row.get("alternative_names") or []
                if isinstance(item, Mapping)
            ],
            key=lambda item: (str(item["surface"]), str(item["name_class"])),
        ),
        "referent_kind_claim": _claim_value(row, "referent_kind_claim"),
        "referential_gender_claim": _claim_value(row, "referential_gender_claim"),
        "identity_summary_draft": row.get("identity_summary_draft"),
    }


def _claim_support_block_ids(row: Mapping[str, Any]) -> list[str]:
    result: list[str] = []
    for field in ("referent_kind_claim", "referential_gender_claim"):
        claim = row.get(field)
        if not isinstance(claim, Mapping):
            continue
        for block_id in claim.get("support_block_ids") or []:
            value = str(block_id or "")
            if value and value not in result:
                result.append(value)
    return result


def _bounded_review_blocks(
    *,
    source_block_ids: Sequence[str],
    surface_claims: Sequence[Mapping[str, Any]],
    semantic_support_block_ids: Sequence[str],
    block_order: Mapping[str, int],
    cap: int,
) -> list[str]:
    priorities: list[str] = []

    def add(block_id: str) -> None:
        if block_id in block_order and block_id not in priorities:
            priorities.append(block_id)

    ordered_source = sorted(set(source_block_ids), key=block_order.__getitem__)
    if ordered_source:
        add(ordered_source[0])
        add(ordered_source[-1])
    for claim in surface_claims:
        claim_blocks = sorted(
            set(str(value) for value in claim.get("source_block_ids") or []),
            key=block_order.__getitem__,
        )
        if claim_blocks:
            add(claim_blocks[0])
            add(claim_blocks[-1])
    for block_id in semantic_support_block_ids:
        add(block_id)
    for block_id in ordered_source:
        add(block_id)
    return sorted(priorities[:cap], key=block_order.__getitem__)


def _ordered_unique_block_ids(
    values: Iterable[Any], *, block_order: Mapping[str, int], chapter_id: str | None = None,
    block_chapter: Mapping[str, str] | None = None, label: str = "source_block_ids"
) -> list[str]:
    rows: list[str] = []
    for value in values:
        block_id = _required_string(value, label)
        if block_id not in block_order:
            raise BookEntityContractError(f"{label} contains foreign block: {block_id}")
        if chapter_id is not None and block_chapter is not None:
            if block_chapter[block_id] != chapter_id:
                raise BookEntityContractError(f"{label} crosses chapter boundary")
        if block_id not in rows:
            rows.append(block_id)
    return sorted(rows, key=block_order.__getitem__)


class _UnionFind:
    def __init__(self, values: Iterable[str]) -> None:
        self.parent = {value: value for value in values}

    def find(self, value: str) -> str:
        parent = self.parent[value]
        if parent != value:
            self.parent[value] = self.find(parent)
        return self.parent[value]

    def union(self, left: str, right: str) -> None:
        a, b = self.find(left), self.find(right)
        if a != b:
            self.parent[max(a, b)] = min(a, b)


def build_book_entity_index_v1(
    *,
    document: Mapping[str, Any],
    audited_inventories: Mapping[str, Mapping[str, Any]] | Sequence[Mapping[str, Any]],
    chapter_orientations: (
        Mapping[str, Mapping[str, Any]] | Sequence[Mapping[str, Any]] | None
    ) = None,
    max_component_candidates: int = DEFAULT_MAX_COMPONENT_CANDIDATES,
    max_component_source_blocks: int = DEFAULT_MAX_COMPONENT_SOURCE_BLOCKS,
    max_review_blocks_per_candidate: int = DEFAULT_MAX_REVIEW_BLOCKS_PER_CANDIDATE,
    max_gist_utf8_bytes: int = DEFAULT_MAX_GIST_UTF8_BYTES,
) -> dict[str, Any]:
    """Build a complete, deterministic, non-authoritative whole-book index."""

    if (
        max_component_candidates <= 1
        or max_component_source_blocks <= 1
        or max_review_blocks_per_candidate <= 1
    ):
        raise BookEntityContractError("component bounds must exceed one")
    chapter_order, chapters, block_chapter, block_order = _document_catalog(document)
    block_text_by_id = {
        block["block_id"]: block["text"]
        for chapter in chapters.values()
        for block in chapter["blocks"]
    }
    inventories = _coerce_chapter_payloads(audited_inventories, label="audited inventory")
    orientations = (
        _coerce_chapter_payloads(chapter_orientations, label="orientation")
        if chapter_orientations is not None
        else {}
    )
    expected = set(chapter_order)
    if set(inventories) != expected:
        raise BookEntityContractError("audited inventories do not exact-cover document chapters")
    if not set(orientations) <= expected:
        raise BookEntityContractError("orientations contain foreign document chapters")

    source_manifest = build_book_source_manifest(document)
    verify_book_source_manifest(document, source_manifest)
    lineage_id = state_lineage_id_for_manifest(source_manifest)
    input_rows: list[dict[str, Any]] = []
    gists: dict[str, str] = {}
    candidates: list[dict[str, Any]] = []
    closed_candidates: list[dict[str, Any]] = []
    unresolved_candidates: list[dict[str, Any]] = []
    pending_source_repairs: list[dict[str, Any]] = []
    validation_quarantines: list[dict[str, Any]] = []
    chapter_bindings: list[dict[str, Any]] = []
    local_quarantines: list[dict[str, Any]] = []

    for chapter_id in chapter_order:
        inventory = inventories[chapter_id]
        inventory_hash = _verify_audited_inventory(inventory, chapter_id)
        validation_quarantine = _open_validation_report(
            inventory, chapter_id, inventory_hash
        )
        if validation_quarantine is not None:
            validation_quarantines.append(validation_quarantine)
        orientation_hash: str | None = None
        if chapter_id in orientations:
            gist, orientation_hash = _orientation_gist(
                orientations[chapter_id], chapter_id
            )
            if len(gist.encode("utf-8")) > max_gist_utf8_bytes:
                raise BookEntityContractError("orientation gist exceeds the pinned byte bound")
            gists[chapter_id] = gist
        input_rows.append(
            {
                "chapter_id": chapter_id,
                "source_hash": next(
                    row["source_hash"]
                    for row in source_manifest["ordered_chapters"]
                    if row["chapter_id"] == chapter_id
                ),
                "conflict_audited_inventory_hash": inventory_hash,
                "orientation_hash": orientation_hash,
            }
        )
        seen_local: set[str] = set()
        for table, local_state in (
            ("entity_candidates", "confirmed"),
            ("pending_entity_candidates", "pending"),
            ("closed_entity_candidates", "closed"),
        ):
            for raw in inventory.get(table) or []:
                if not isinstance(raw, Mapping):
                    raise BookEntityContractError(f"{table} row must be an object")
                local_id = _required_string(
                    raw.get("candidate_id"), f"{table}.candidate_id"
                )
                if local_id in seen_local:
                    raise BookEntityContractError(
                        "duplicate local candidate id across inventory tables"
                    )
                seen_local.add(local_id)
                row = _clone(dict(raw))
                source_ids = _ordered_unique_block_ids(
                    row.get("source_block_ids") or [],
                    block_order=block_order,
                    chapter_id=chapter_id,
                    block_chapter=block_chapter,
                )
                if not source_ids:
                    raise BookEntityContractError("candidate lacks source provenance")
                for claim in _surface_claims(row):
                    claim_blocks = _ordered_unique_block_ids(
                        claim["source_block_ids"],
                        block_order=block_order,
                        chapter_id=chapter_id,
                        block_chapter=block_chapter,
                        label="surface source_block_ids",
                    )
                    if not set(claim_blocks) <= set(source_ids):
                        raise BookEntityContractError(
                            "surface support must be included in candidate source provenance"
                        )
                    if claim["source_located"]:
                        surface = unicodedata.normalize("NFC", claim["surface"])
                        if not any(
                            surface in unicodedata.normalize("NFC", block_text_by_id[block_id])
                            for block_id in claim_blocks
                        ):
                            raise BookEntityContractError(
                                "located surface is absent from its cited source blocks"
                            )
                candidate = {
                    "candidate_ref": _candidate_ref(lineage_id, chapter_id, local_id),
                    "chapter_id": chapter_id,
                    "local_candidate_id": local_id,
                    "local_state": local_state,
                    "source_block_ids": source_ids,
                    "review_source_block_ids": _bounded_review_blocks(
                        source_block_ids=source_ids,
                        surface_claims=_surface_claims(row),
                        semantic_support_block_ids=_claim_support_block_ids(row),
                        block_order=block_order,
                        cap=max_review_blocks_per_candidate,
                    ),
                    "semantic_support_block_ids": _ordered_unique_block_ids(
                        _claim_support_block_ids(row),
                        block_order=block_order,
                        chapter_id=chapter_id,
                        block_chapter=block_chapter,
                        label="semantic support_block_ids",
                    ),
                    "first_supported_block_id": source_ids[0],
                    "stable_claim": _stable_claim_payload(row),
                    "surface_claims": _surface_claims(row),
                    "source_row_hash": canonical_hash(row),
                    "publication_state": row.get("publication_state"),
                    "audit_reasons": sorted(
                        str(value) for value in row.get("audit_reasons") or []
                    ),
                }
                (closed_candidates if local_state == "closed" else candidates).append(candidate)

        by_local = {
            row["local_candidate_id"]: row
            for row in candidates
            if row["chapter_id"] == chapter_id
        }
        for raw in inventory.get("global_surface_bindings") or []:
            if not isinstance(raw, Mapping):
                raise BookEntityContractError("local surface binding must be an object")
            if raw.get("action") != "bind_global":
                raise BookEntityContractError("foreign local surface binding action")
            local_target = _required_string(raw.get("target_candidate_id"), "binding target")
            target = by_local.get(local_target)
            if target is None:
                raise BookEntityContractError("local surface binding targets a missing candidate")
            source_ids = _ordered_unique_block_ids(
                raw.get("source_block_ids") or [],
                block_order=block_order,
                chapter_id=chapter_id,
                block_chapter=block_chapter,
                label="binding source_block_ids",
            )
            chapter_bindings.append(
                {
                    "surface_key": _normalized_surface(
                        _required_string(raw.get("surface_key"), "binding.surface_key")
                    ),
                    "scope_authority": "chapter_scoped",
                    "chapter_id": chapter_id,
                    "target_candidate_ref": target["candidate_ref"],
                    "source_block_ids": source_ids,
                    "first_supported_block_id": source_ids[0],
                    "source_action": "bind_global",
                    "resolution_note": raw.get("resolution_note"),
                }
            )
        for raw in inventory.get("quarantined_surfaces") or []:
            if not isinstance(raw, Mapping):
                raise BookEntityContractError("local quarantine row must be an object")
            source_ids = _ordered_unique_block_ids(
                raw.get("source_block_ids") or [],
                block_order=block_order,
                chapter_id=chapter_id,
                block_chapter=block_chapter,
                label="quarantine source_block_ids",
            )
            local_quarantines.append(
                {
                    "surface_key": _normalized_surface(
                        _required_string(raw.get("surface_key"), "quarantine.surface_key")
                    ),
                    "chapter_id": chapter_id,
                    "source_block_ids": source_ids,
                    "first_supported_block_id": source_ids[0],
                    "source": "chapter_local_auditor",
                }
            )
        for offset, raw in enumerate(inventory.get("unresolved_referents") or []):
            if not isinstance(raw, Mapping):
                raise BookEntityContractError("unresolved referent must be an object")
            source_ids = _ordered_unique_block_ids(
                raw.get("source_block_ids") or [],
                block_order=block_order,
                chapter_id=chapter_id,
                block_chapter=block_chapter,
                label="unresolved source_block_ids",
            )
            if not source_ids:
                continue
            unresolved_candidates.append(
                {
                    "unresolved_ref": "bkunres_" + canonical_hash(
                        {
                            "state_lineage_id": lineage_id,
                            "chapter_id": chapter_id,
                            "row_hash": canonical_hash(raw),
                            "offset": offset,
                        }
                    )[:20],
                    "chapter_id": chapter_id,
                    "source_block_ids": source_ids,
                    "first_supported_block_id": source_ids[0],
                    "source_row": _clone(dict(raw)),
                }
            )
        for offset, raw in enumerate(inventory.get("deferred_source_repairs") or []):
            if not isinstance(raw, Mapping):
                raise BookEntityContractError("deferred source repair must be an object")
            local_id = _required_string(
                raw.get("candidate_id"), "deferred source repair candidate_id"
            )
            proposed_ids = sorted(
                {
                    str(block_id)
                    for location in raw.get("name_locations") or []
                    if isinstance(location, Mapping)
                    for block_id in location.get("proposed_support_block_ids") or []
                }
            )
            lexical_ids = _ordered_unique_block_ids(
                [
                    block_id
                    for location in raw.get("name_locations") or []
                    if isinstance(location, Mapping)
                    for block_id in location.get("surface_match_block_ids") or []
                ],
                block_order=block_order,
                chapter_id=chapter_id,
                block_chapter=block_chapter,
                label="deferred surface_match_block_ids",
            )
            repair_body = {
                "chapter_id": chapter_id,
                "local_candidate_id": local_id,
                "proposed_support_block_ids": proposed_ids,
                "surface_match_block_ids": lexical_ids,
                "lifecycle_state": "pending_source_repair",
                "authority_state": "not_authoritative",
                "source_row_hash": canonical_hash(raw),
                "source_row": _clone(dict(raw)),
            }
            pending_source_repairs.append(
                {
                    "source_repair_id": "bksrep_" + canonical_hash(
                        {
                            "state_lineage_id": lineage_id,
                            "chapter_id": chapter_id,
                            "local_candidate_id": local_id,
                            "offset": offset,
                            "source_row_hash": repair_body["source_row_hash"],
                        }
                    )[:20],
                    **repair_body,
                }
            )

    candidates.sort(key=lambda row: row["candidate_ref"])
    closed_candidates.sort(key=lambda row: row["candidate_ref"])
    candidate_by_ref = {row["candidate_ref"]: row for row in candidates}
    if len(candidate_by_ref) != len(candidates):
        raise BookEntityContractError("candidate ref collision")

    claim_origins: dict[str, list[dict[str, Any]]] = defaultdict(list)
    claim_payloads: dict[str, dict[str, Any]] = {}
    for row in candidates:
        payload = row["stable_claim"]
        payload_hash = canonical_hash(payload)
        group_id = "bkclaim_" + payload_hash[:20]
        row["claim_group_id"] = group_id
        claim_payloads[group_id] = payload
        claim_origins[group_id].append(
            {
                "candidate_ref": row["candidate_ref"],
                "chapter_id": row["chapter_id"],
                "source_block_ids": row["source_block_ids"],
                "source_row_hash": row["source_row_hash"],
            }
        )
    claim_groups = [
        {
            "claim_group_id": group_id,
            "claim_payload": claim_payloads[group_id],
            "origin_count": len(claim_origins[group_id]),
            "origins": sorted(claim_origins[group_id], key=lambda row: row["candidate_ref"]),
        }
        for group_id in sorted(claim_payloads)
    ]
    for row in candidates:
        row.pop("stable_claim", None)

    surface_rows: dict[str, dict[str, Any]] = {}
    match_buckets: dict[str, set[str]] = defaultdict(set)
    for candidate in candidates:
        for claim in candidate["surface_claims"]:
            if not claim["source_located"]:
                continue
            key = claim["surface_key"]
            if not key:
                continue
            row = surface_rows.setdefault(
                key,
                {
                    "surface_key": key,
                    "surface_variants": set(),
                    "candidate_refs": set(),
                    "observed_chapter_ids": set(),
                    "source_block_ids": set(),
                },
            )
            row["surface_variants"].add(claim["surface"])
            row["candidate_refs"].add(candidate["candidate_ref"])
            row["observed_chapter_ids"].add(candidate["chapter_id"])
            row["source_block_ids"].update(claim["source_block_ids"])
            match_buckets[f"surface:{key}"].add(candidate["candidate_ref"])
            core = _title_core(claim["surface"], claim["name_class"])
            if core:
                match_buckets[f"core:{core}"].add(candidate["candidate_ref"])
    for binding in chapter_bindings:
        key = binding["surface_key"]
        row = surface_rows.setdefault(
            key,
            {
                "surface_key": key,
                "surface_variants": set(),
                "candidate_refs": set(),
                "observed_chapter_ids": set(),
                "source_block_ids": set(),
            },
        )
        row["surface_variants"].add(key)
        row["candidate_refs"].add(binding["target_candidate_ref"])
        row["observed_chapter_ids"].add(binding["chapter_id"])
        row["source_block_ids"].update(binding["source_block_ids"])
        match_buckets[f"surface:{key}"].add(binding["target_candidate_ref"])

    uf = _UnionFind(candidate_by_ref)
    active_match_keys: list[str] = []
    for match_key, refs in sorted(match_buckets.items()):
        chapters_for_key = {candidate_by_ref[ref]["chapter_id"] for ref in refs}
        if len(refs) < 2 or len(chapters_for_key) < 2:
            continue
        active_match_keys.append(match_key)
        ordered = sorted(refs)
        for ref in ordered[1:]:
            uf.union(ordered[0], ref)
    connected: dict[str, list[str]] = defaultdict(list)
    for ref in candidate_by_ref:
        connected[uf.find(ref)].append(ref)

    normalized_surface_index: list[dict[str, Any]] = []
    surface_case_by_key: dict[str, dict[str, Any]] = {}
    for key, raw in sorted(surface_rows.items()):
        source_ids = _ordered_unique_block_ids(raw["source_block_ids"], block_order=block_order)
        row = {
            "surface_key": key,
            "surface_variants": sorted(raw["surface_variants"]),
            "candidate_refs": sorted(raw["candidate_refs"]),
            "observed_chapter_ids": sorted(
                raw["observed_chapter_ids"], key=chapter_order.index
            ),
            "source_block_ids": source_ids,
            "review_source_block_ids": _ordered_unique_block_ids(
                [
                    block_id
                    for chapter_id in sorted(
                        raw["observed_chapter_ids"], key=chapter_order.index
                    )
                    for block_id in (
                        [
                            value
                            for value in source_ids
                            if block_chapter[value] == chapter_id
                        ][:1]
                        + [
                            value
                            for value in source_ids
                            if block_chapter[value] == chapter_id
                        ][-1:]
                    )
                ],
                block_order=block_order,
            ),
            "first_supported_block_id": source_ids[0],
        }
        normalized_surface_index.append(row)
        if len(row["candidate_refs"]) >= 2 and len(row["observed_chapter_ids"]) >= 2:
            case_body = {
                "surface_key": key,
                "candidate_refs": row["candidate_refs"],
                "observed_chapter_ids": row["observed_chapter_ids"],
                "source_block_ids": source_ids,
                "review_source_block_ids": row["review_source_block_ids"],
            }
            surface_case_by_key[key] = {
                **case_body,
                "surface_case_id": "bisurf_" + canonical_hash(case_body)[:20],
                "surface_variants": row["surface_variants"],
            }

    components: list[dict[str, Any]] = []
    all_component_refs: set[str] = set()
    for refs in sorted((sorted(values) for values in connected.values()), key=lambda row: row[0]):
        chapters_in_component = sorted(
            {candidate_by_ref[ref]["chapter_id"] for ref in refs}, key=chapter_order.index
        )
        if len(refs) < 2 or len(chapters_in_component) < 2:
            continue
        all_component_refs.update(refs)
        ref_set = set(refs)
        cases = sorted(
            [
                row
                for row in surface_case_by_key.values()
                if len(ref_set.intersection(row["candidate_refs"])) >= 2
            ],
            key=lambda row: row["surface_case_id"],
        )
        inherited = sorted(
            [row for row in chapter_bindings if row["target_candidate_ref"] in ref_set],
            key=lambda row: (
                chapter_order.index(row["chapter_id"]),
                row["surface_key"],
                row["target_candidate_ref"],
            ),
        )
        provenance_source_ids = _ordered_unique_block_ids(
            [
                block_id
                for ref in refs
                for block_id in candidate_by_ref[ref]["source_block_ids"]
            ]
            + [block_id for case in cases for block_id in case["source_block_ids"]]
            + [block_id for row in inherited for block_id in row["source_block_ids"]],
            block_order=block_order,
        )
        request_source_ids = _ordered_unique_block_ids(
            [
                block_id
                for ref in refs
                for block_id in candidate_by_ref[ref]["review_source_block_ids"]
            ]
            + [
                block_id
                for case in cases
                for block_id in case["review_source_block_ids"]
            ]
            + [
                block_id
                for row in inherited
                for block_id in row["source_block_ids"][:1]
            ],
            block_order=block_order,
        )
        body = {
            "candidate_refs": refs,
            "chapter_ids": chapters_in_component,
            "surface_case_ids": [row["surface_case_id"] for row in cases],
            "claim_group_ids": sorted(
                {candidate_by_ref[ref]["claim_group_id"] for ref in refs}
            ),
        }
        component_id = "bicomp_" + canonical_hash(
            {"state_lineage_id": lineage_id, **body}
        )[:20]
        overflow_reasons: list[str] = []
        if len(refs) > max_component_candidates:
            overflow_reasons.append("candidate_count_cap")
        if len(request_source_ids) > max_component_source_blocks:
            overflow_reasons.append("source_block_count_cap")
        components.append(
            {
                "component_id": component_id,
                **body,
                "contested_surfaces": cases,
                "inherited_chapter_scoped_bindings": inherited,
                "source_block_ids": request_source_ids,
                "provenance_source_block_ids": provenance_source_ids,
                "overflow": bool(overflow_reasons),
                "overflow_reasons": overflow_reasons,
            }
        )
    components.sort(key=lambda row: row["component_id"])

    index_body = {
        "schema_version": BOOK_INDEX_SCHEMA_VERSION,
        "validator_version": BOOK_VALIDATOR_VERSION,
        "state_lineage_id": lineage_id,
        "book_source_manifest": source_manifest,
        "book_source_manifest_hash": source_manifest["manifest_hash"],
        "complete_chapter_coverage": True,
        "ordered_chapter_inputs": input_rows,
        "chapter_gists": [
            {"chapter_id": chapter_id, "gist": gists[chapter_id]}
            for chapter_id in chapter_order
            if chapter_id in gists
        ],
        "missing_optional_gist_chapter_ids": [
            chapter_id for chapter_id in chapter_order if chapter_id not in gists
        ],
        "candidate_rows": candidates,
        "closed_candidate_rows": closed_candidates,
        "unresolved_candidates": unresolved_candidates,
        "pending_source_repairs": sorted(
            pending_source_repairs, key=lambda row: row["source_repair_id"]
        ),
        "validation_quarantines": sorted(
            validation_quarantines,
            key=lambda row: row["validation_quarantine_id"],
        ),
        "claim_groups": claim_groups,
        "surface_candidate_index": normalized_surface_index,
        "chapter_scoped_bindings": chapter_bindings,
        "local_quarantined_surfaces": local_quarantines,
        "components": components,
        "clean_candidate_refs": sorted(set(candidate_by_ref) - all_component_refs),
        "overflow_component_ids": sorted(
            row["component_id"] for row in components if row["overflow"]
        ),
        "component_bounds": {
            "max_component_candidates": max_component_candidates,
            "max_component_source_blocks": max_component_source_blocks,
            "max_review_blocks_per_candidate": max_review_blocks_per_candidate,
            "max_gist_utf8_bytes": max_gist_utf8_bytes,
        },
    }
    return {**index_body, "book_index_hash": canonical_hash(index_body)}


def verify_book_entity_index_v1(
    index: Mapping[str, Any], *, document: Mapping[str, Any] | None = None
) -> dict[str, Any]:
    if index.get("schema_version") != BOOK_INDEX_SCHEMA_VERSION:
        raise BookEntityContractError("foreign book index schema")
    if index.get("validator_version") != BOOK_VALIDATOR_VERSION:
        raise BookEntityContractError("book index validator mismatch")
    body = dict(index)
    observed = _required_string(body.pop("book_index_hash", None), "book_index_hash")
    if canonical_hash(body) != observed:
        raise BookEntityContractError("book index hash mismatch")
    if index.get("complete_chapter_coverage") is not True:
        raise BookEntityContractError("partial book index cannot be used")
    repair_rows = index.get("pending_source_repairs")
    if not isinstance(repair_rows, list):
        raise BookEntityContractError("pending_source_repairs must be a list")
    repair_ids: set[str] = set()
    for row in repair_rows:
        if not isinstance(row, Mapping):
            raise BookEntityContractError("pending source repair must be an object")
        repair_id = _required_string(row.get("source_repair_id"), "source_repair_id")
        if repair_id in repair_ids:
            raise BookEntityContractError("duplicate source repair id")
        repair_ids.add(repair_id)
        if row.get("lifecycle_state") != "pending_source_repair":
            raise BookEntityContractError("source repair has invalid lifecycle")
        if row.get("authority_state") != "not_authoritative":
            raise BookEntityContractError("source repair has unsafe authority")
        source_row = row.get("source_row")
        if not isinstance(source_row, Mapping):
            raise BookEntityContractError("source repair source_row must be an object")
        if canonical_hash(source_row) != row.get("source_row_hash"):
            raise BookEntityContractError("source repair row hash mismatch")
    quarantine_rows = index.get("validation_quarantines")
    if not isinstance(quarantine_rows, list):
        raise BookEntityContractError("validation_quarantines must be a list")
    quarantine_ids: set[str] = set()
    for row in quarantine_rows:
        if not isinstance(row, Mapping):
            raise BookEntityContractError("validation quarantine must be an object")
        quarantine_id = _required_string(
            row.get("validation_quarantine_id"), "validation_quarantine_id"
        )
        if quarantine_id in quarantine_ids:
            raise BookEntityContractError("duplicate validation quarantine id")
        quarantine_ids.add(quarantine_id)
        if row.get("lifecycle_state") != "open_validation_quarantine":
            raise BookEntityContractError("validation quarantine has invalid lifecycle")
        if row.get("authority_state") != "not_authoritative":
            raise BookEntityContractError("validation quarantine has unsafe authority")
        if not isinstance(row.get("issues"), Mapping) or not row["issues"]:
            raise BookEntityContractError("validation quarantine has no issues")
    if document is not None:
        verify_book_source_manifest(document, index["book_source_manifest"])
    return _clone(dict(index))


def cross_chapter_response_schema_v1() -> dict[str, Any]:
    partition = {
        "type": "object",
        "additionalProperties": False,
        "required": ["source_block_ids", "retained_surfaces", "resolution_note"],
        "properties": {
            "source_block_ids": {"type": "array", "items": {"type": "string"}},
            "retained_surfaces": {"type": "array", "items": {"type": "string"}},
            "resolution_note": {"type": "string"},
        },
    }
    candidate = {
        "type": "object",
        "additionalProperties": False,
        "required": [
            "candidate_ref",
            "action",
            "target_candidate_ref",
            "split_partitions",
            "source_block_ids",
            "resolution_note",
        ],
        "properties": {
            "candidate_ref": {"type": "string"},
            "action": {"type": "string", "enum": sorted(CANDIDATE_ACTIONS)},
            "target_candidate_ref": {"type": ["string", "null"]},
            "split_partitions": {"type": "array", "items": partition},
            "source_block_ids": {"type": "array", "items": {"type": "string"}},
            "resolution_note": {"type": "string"},
        },
    }
    surface = {
        "type": "object",
        "additionalProperties": False,
        "required": [
            "surface_case_id",
            "action",
            "target_candidate_ref",
            "source_block_ids",
            "resolution_note",
        ],
        "properties": {
            "surface_case_id": {"type": "string"},
            "action": {"type": "string", "enum": sorted(SURFACE_ACTIONS)},
            "target_candidate_ref": {"type": ["string", "null"]},
            "source_block_ids": {"type": "array", "items": {"type": "string"}},
            "resolution_note": {"type": "string"},
        },
    }
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["component_id", "candidate_actions", "surface_actions"],
        "properties": {
            "component_id": {"type": "string"},
            "candidate_actions": {"type": "array", "items": candidate},
            "surface_actions": {"type": "array", "items": surface},
        },
    }


def render_cross_chapter_request_v1(
    *,
    index: Mapping[str, Any],
    component_id: str,
    document: Mapping[str, Any],
    design_doc: Path,
) -> RenderedBookEntityRequestV1:
    verified = verify_book_entity_index_v1(index, document=document)
    component = next(
        (row for row in verified["components"] if row["component_id"] == component_id),
        None,
    )
    if component is None:
        raise BookEntityContractError("unknown cross-chapter component")
    if component["overflow"]:
        raise BookEntityContractError("overflow component cannot be rendered")
    candidate_by_ref = {row["candidate_ref"]: row for row in verified["candidate_rows"]}
    groups = {
        row["claim_group_id"]: row for row in verified["claim_groups"]
    }
    gist_by_chapter = {row["chapter_id"]: row["gist"] for row in verified["chapter_gists"]}
    chapter_order, chapters, _block_chapter, _block_order = _document_catalog(document)
    block_by_id = {
        block["block_id"]: {"chapter_id": chapter_id, **block}
        for chapter_id in chapter_order
        for block in chapters[chapter_id]["blocks"]
    }
    candidate_provenance = [
        {
            "candidate_ref": ref,
            "chapter_id": candidate_by_ref[ref]["chapter_id"],
            "local_candidate_id": candidate_by_ref[ref]["local_candidate_id"],
            "local_state": candidate_by_ref[ref]["local_state"],
            "claim_group_id": candidate_by_ref[ref]["claim_group_id"],
            "surface_claims": candidate_by_ref[ref]["surface_claims"],
            "source_block_ids": candidate_by_ref[ref]["source_block_ids"],
            "review_source_block_ids": candidate_by_ref[ref]["review_source_block_ids"],
            "split_eligible": set(candidate_by_ref[ref]["source_block_ids"])
            <= set(component["source_block_ids"]),
            "first_supported_block_id": candidate_by_ref[ref]["first_supported_block_id"],
        }
        for ref in component["candidate_refs"]
    ]
    payload = {
        "contract_version": BOOK_VALIDATOR_VERSION,
        "book_index_hash": verified["book_index_hash"],
        "component_id": component_id,
        "candidate_provenance": candidate_provenance,
        "claim_groups": [groups[group_id] for group_id in component["claim_group_ids"]],
        "contested_surfaces": component["contested_surfaces"],
        "inherited_chapter_scoped_bindings": component[
            "inherited_chapter_scoped_bindings"
        ],
        "chapter_gists": [
            {"chapter_id": chapter_id, "gist": gist_by_chapter[chapter_id]}
            for chapter_id in component["chapter_ids"]
            if chapter_id in gist_by_chapter
        ],
        "source_blocks": [block_by_id[block_id] for block_id in component["source_block_ids"]],
    }
    system_prompt = load_book_entity_prompt_v1(design_doc)
    user_content = canonical_json(payload)
    fingerprint = canonical_hash(
        {
            "prompt_id": PROMPT_ID,
            "prompt_sha256": PROMPT_SHA256,
            "semantic_payload": payload,
            "response_schema": cross_chapter_response_schema_v1(),
        }
    )
    return RenderedBookEntityRequestV1(
        component_id=component_id,
        request_fingerprint=fingerprint,
        messages=(
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_content},
        ),
        response_schema=cross_chapter_response_schema_v1(),
        semantic_payload=payload,
    )


def validate_cross_chapter_response_v1(
    response: Mapping[str, Any],
    *,
    index: Mapping[str, Any],
    request_fingerprint: str,
) -> dict[str, Any]:
    verified = verify_book_entity_index_v1(index)
    _exact_keys(
        response,
        {"component_id", "candidate_actions", "surface_actions"},
        "cross-chapter response",
    )
    component_id = _required_string(response.get("component_id"), "component_id")
    component = next(
        (row for row in verified["components"] if row["component_id"] == component_id),
        None,
    )
    if component is None or component["overflow"]:
        raise BookEntityContractError("response owns an unknown or overflow component")
    candidates = {
        row["candidate_ref"]: row
        for row in verified["candidate_rows"]
        if row["candidate_ref"] in set(component["candidate_refs"])
    }
    block_chapter = {
        block_id: row["chapter_id"]
        for row in candidates.values()
        for block_id in row["source_block_ids"]
    }
    for case in component["contested_surfaces"]:
        for block_id in case["source_block_ids"]:
            if block_id not in block_chapter:
                chapter = next(
                    chapter_id
                    for chapter_id in case["observed_chapter_ids"]
                    if any(
                        block_id in row["source_block_ids"]
                        for row in candidates.values()
                        if row["chapter_id"] == chapter_id
                    )
                )
                block_chapter[block_id] = chapter

    raw_candidate_actions = response.get("candidate_actions")
    if not isinstance(raw_candidate_actions, list):
        raise BookEntityContractError("candidate_actions must be a list")
    normalized_candidates: list[dict[str, Any]] = []
    seen_candidates: set[str] = set()
    for raw in raw_candidate_actions:
        if not isinstance(raw, Mapping):
            raise BookEntityContractError("candidate action must be an object")
        _exact_keys(
            raw,
            {
                "candidate_ref",
                "action",
                "target_candidate_ref",
                "split_partitions",
                "source_block_ids",
                "resolution_note",
            },
            "candidate action",
        )
        ref = _required_string(raw.get("candidate_ref"), "candidate_ref")
        if ref not in candidates or ref in seen_candidates:
            raise BookEntityContractError("candidate actions do not exact-cover component")
        seen_candidates.add(ref)
        action = _required_string(raw.get("action"), "candidate action")
        if action not in CANDIDATE_ACTIONS:
            raise BookEntityContractError("candidate action outside closed enum")
        target = raw.get("target_candidate_ref")
        if target is not None:
            target = _required_string(target, "target_candidate_ref")
        evidence = _string_list(raw.get("source_block_ids"), "candidate evidence")
        if not set(evidence) <= set(candidates[ref]["review_source_block_ids"]):
            raise BookEntityContractError("candidate evidence cites foreign source blocks")
        note = _required_string(raw.get("resolution_note"), "candidate resolution_note")
        raw_partitions = raw.get("split_partitions")
        if not isinstance(raw_partitions, list):
            raise BookEntityContractError("split_partitions must be a list")
        partitions: list[dict[str, Any]] = []
        if action == "split":
            if not set(candidates[ref]["source_block_ids"]) <= set(
                component["source_block_ids"]
            ):
                raise BookEntityContractError(
                    "split is forbidden when candidate source text is not fully supplied"
                )
            if target is not None or len(raw_partitions) < 2:
                raise BookEntityContractError("split requires at least two target-free partitions")
            supplied_blocks = set(candidates[ref]["source_block_ids"])
            covered: set[str] = set()
            supplied_surfaces = {
                claim["surface"]: set(claim["source_block_ids"])
                for claim in candidates[ref]["surface_claims"]
            }
            for partition in raw_partitions:
                if not isinstance(partition, Mapping):
                    raise BookEntityContractError("split partition must be an object")
                _exact_keys(
                    partition,
                    {"source_block_ids", "retained_surfaces", "resolution_note"},
                    "split partition",
                )
                blocks = _string_list(
                    partition.get("source_block_ids"), "partition source_block_ids"
                )
                block_set = set(blocks)
                if not block_set <= supplied_blocks or covered.intersection(block_set):
                    raise BookEntityContractError("split partitions overlap or cite foreign blocks")
                covered.update(block_set)
                surfaces = _string_list(
                    partition.get("retained_surfaces"),
                    "retained_surfaces",
                    allow_empty=True,
                )
                for surface in surfaces:
                    if surface not in supplied_surfaces or not supplied_surfaces[
                        surface
                    ].intersection(block_set):
                        raise BookEntityContractError(
                            "split partition invents or misplaces a surface"
                        )
                partitions.append(
                    {
                        "source_block_ids": sorted(blocks),
                        "retained_surfaces": sorted(surfaces),
                        "resolution_note": _required_string(
                            partition.get("resolution_note"), "partition resolution_note"
                        ),
                    }
                )
            if covered != supplied_blocks:
                raise BookEntityContractError("split partitions must exact-cover candidate blocks")
        elif raw_partitions:
            raise BookEntityContractError("non-split action cannot carry split partitions")
        if action == "merge_into":
            if target is None or target == ref or target not in candidates:
                raise BookEntityContractError("merge target must be another component candidate")
        elif target is not None:
            raise BookEntityContractError("target is allowed only for merge_into")
        normalized_candidates.append(
            {
                "candidate_ref": ref,
                "action": action,
                "target_candidate_ref": target,
                "split_partitions": sorted(
                    partitions, key=lambda row: tuple(row["source_block_ids"])
                ),
                "source_block_ids": sorted(evidence),
                "resolution_note": note,
            }
        )
    if seen_candidates != set(component["candidate_refs"]):
        raise BookEntityContractError("candidate actions must exact-cover component")
    kept = {
        row["candidate_ref"] for row in normalized_candidates if row["action"] == "keep"
    }
    for row in normalized_candidates:
        if row["action"] == "merge_into" and row["target_candidate_ref"] not in kept:
            raise BookEntityContractError("merge target must be kept in the same response")

    cases = {row["surface_case_id"]: row for row in component["contested_surfaces"]}
    raw_surface_actions = response.get("surface_actions")
    if not isinstance(raw_surface_actions, list):
        raise BookEntityContractError("surface_actions must be a list")
    normalized_surfaces: list[dict[str, Any]] = []
    seen_surfaces: set[str] = set()
    for raw in raw_surface_actions:
        if not isinstance(raw, Mapping):
            raise BookEntityContractError("surface action must be an object")
        _exact_keys(
            raw,
            {
                "surface_case_id",
                "action",
                "target_candidate_ref",
                "source_block_ids",
                "resolution_note",
            },
            "surface action",
        )
        case_id = _required_string(raw.get("surface_case_id"), "surface_case_id")
        if case_id not in cases or case_id in seen_surfaces:
            raise BookEntityContractError("surface actions do not exact-cover component")
        seen_surfaces.add(case_id)
        case = cases[case_id]
        action = _required_string(raw.get("action"), "surface action")
        if action not in SURFACE_ACTIONS:
            raise BookEntityContractError("surface action outside closed enum")
        target = raw.get("target_candidate_ref")
        if target is not None:
            target = _required_string(target, "surface target_candidate_ref")
        evidence = _string_list(raw.get("source_block_ids"), "surface evidence")
        if not set(evidence) <= set(case["review_source_block_ids"]):
            raise BookEntityContractError("surface evidence cites foreign source blocks")
        if action == "promote_book_global":
            if target not in kept or target not in set(case["candidate_refs"]):
                raise BookEntityContractError("book-global surface must target a kept candidate")
            evidence_chapters = {block_chapter.get(block_id) for block_id in evidence}
            if set(case["observed_chapter_ids"]) - evidence_chapters:
                raise BookEntityContractError(
                    "book-global surface lacks evidence from every observed chapter"
                )
        elif target is not None:
            raise BookEntityContractError("non-global surface action cannot target a candidate")
        normalized_surfaces.append(
            {
                "surface_case_id": case_id,
                "action": action,
                "target_candidate_ref": target,
                "source_block_ids": sorted(evidence),
                "resolution_note": _required_string(
                    raw.get("resolution_note"), "surface resolution_note"
                ),
            }
        )
    if seen_surfaces != set(cases):
        raise BookEntityContractError("surface actions must exact-cover component")
    body = {
        "schema_version": "cross_chapter_entity_decision_v1",
        "validator_version": BOOK_VALIDATOR_VERSION,
        "book_index_hash": verified["book_index_hash"],
        "component_id": component_id,
        "request_fingerprint": _required_string(
            request_fingerprint, "request_fingerprint"
        ),
        "candidate_actions": sorted(
            normalized_candidates, key=lambda row: row["candidate_ref"]
        ),
        "surface_actions": sorted(
            normalized_surfaces, key=lambda row: row["surface_case_id"]
        ),
    }
    return {**body, "decision_hash": canonical_hash(body)}


def verify_cross_chapter_decision_v1(
    decision: Mapping[str, Any], *, index: Mapping[str, Any]
) -> dict[str, Any]:
    verified_index = verify_book_entity_index_v1(index)
    body = dict(decision)
    observed = _required_string(body.pop("decision_hash", None), "decision_hash")
    if canonical_hash(body) != observed:
        raise BookEntityContractError("cross-chapter decision hash mismatch")
    if decision.get("book_index_hash") != verified_index["book_index_hash"]:
        raise BookEntityContractError("cross-chapter decision targets a foreign book index")
    if decision.get("validator_version") != BOOK_VALIDATOR_VERSION:
        raise BookEntityContractError("cross-chapter decision validator mismatch")
    normalized = validate_cross_chapter_response_v1(
        {
            "component_id": decision.get("component_id"),
            "candidate_actions": _clone(decision.get("candidate_actions")),
            "surface_actions": _clone(decision.get("surface_actions")),
        },
        index=verified_index,
        request_fingerprint=_required_string(
            decision.get("request_fingerprint"), "request_fingerprint"
        ),
    )
    if canonical_json(normalized) != canonical_json(decision):
        raise BookEntityContractError("decision artifact is not canonical validator output")
    return normalized


def _entity_id(
    lineage_id: str,
    category: str,
    member_refs: Sequence[str],
    partition_blocks: Sequence[str] | None = None,
) -> str:
    return "bkent1_" + canonical_hash(
        {
            "state_lineage_id": lineage_id,
            "category": category,
            "member_candidate_refs": sorted(member_refs),
            "partition_source_block_ids": sorted(partition_blocks or []),
        }
    )[:20]


def _entity_row(
    *,
    entity_id: str,
    category: str,
    root_ref: str,
    member_refs: Sequence[str],
    candidate_by_ref: Mapping[str, Mapping[str, Any]],
    claim_payloads: Mapping[str, Mapping[str, Any]],
    block_order: Mapping[str, int],
    partition_blocks: Sequence[str] | None = None,
    retained_surfaces: Sequence[str] | None = None,
) -> dict[str, Any]:
    root = candidate_by_ref[root_ref]
    block_filter = set(partition_blocks or [])
    source_ids = sorted(
        {
            block_id
            for ref in member_refs
            for block_id in candidate_by_ref[ref]["source_block_ids"]
            if not block_filter or block_id in block_filter
        },
        key=block_order.__getitem__,
    )
    claim_history = [
        {
            "candidate_ref": ref,
            "chapter_id": candidate_by_ref[ref]["chapter_id"],
            "claim_group_id": candidate_by_ref[ref]["claim_group_id"],
            "claim_payload": _clone(
                claim_payloads[candidate_by_ref[ref]["claim_group_id"]]
            ),
            "source_block_ids": [
                block_id
                for block_id in candidate_by_ref[ref]["source_block_ids"]
                if not block_filter or block_id in block_filter
            ],
        }
        for ref in sorted(member_refs)
    ]
    profile = _clone(claim_payloads[root["claim_group_id"]])
    root_surface_claims = list(root["surface_claims"])
    if retained_surfaces is not None:
        allowed = set(retained_surfaces)
        root_surface_claims = [
            row for row in root_surface_claims if row["surface"] in allowed
        ]
        if profile.get("canonical_surface") not in allowed:
            profile["canonical_surface"] = next(iter(sorted(allowed)), None)
            profile["canonical_name_class"] = None
    canonical_claim = next(
        (
            row
            for row in root_surface_claims
            if row["surface"] == profile.get("canonical_surface")
        ),
        None,
    )
    profile["canonical_surface_first_supported_block_id"] = (
        min(canonical_claim["source_block_ids"], key=block_order.__getitem__)
        if canonical_claim and canonical_claim["source_block_ids"]
        else source_ids[0]
    )
    profile["alternative_names"] = [
        {
            "surface": row["surface"],
            "name_class": row["name_class"],
            "first_supported_block_id": min(
                row["source_block_ids"], key=block_order.__getitem__
            ),
        }
        for row in root_surface_claims
        if not row["is_canonical"] and row["source_block_ids"]
    ]
    surface_history = sorted(
        [
            {
                "candidate_ref": ref,
                "chapter_id": candidate_by_ref[ref]["chapter_id"],
                "surface": claim["surface"],
                "surface_key": claim["surface_key"],
                "name_class": claim["name_class"],
                "is_canonical": claim["is_canonical"],
                "source_block_ids": [
                    block_id
                    for block_id in claim["source_block_ids"]
                    if not block_filter or block_id in block_filter
                ],
                "first_supported_block_id": min(
                    [
                        block_id
                        for block_id in claim["source_block_ids"]
                        if not block_filter or block_id in block_filter
                    ],
                    key=block_order.__getitem__,
                ),
            }
            for ref in sorted(member_refs)
            for claim in candidate_by_ref[ref]["surface_claims"]
            if any(
                not block_filter or block_id in block_filter
                for block_id in claim["source_block_ids"]
            )
        ],
        key=lambda row: (
            block_order[row["first_supported_block_id"]],
            row["surface_key"],
            row["candidate_ref"],
        ),
    )
    return {
        "entity_id": entity_id,
        "registry_category": category,
        "canonical_profile": profile,
        "root_candidate_ref": root_ref,
        "member_candidate_refs": sorted(member_refs),
        "origin_chapter_ids": sorted(
            {candidate_by_ref[ref]["chapter_id"] for ref in member_refs}
        ),
        "source_block_ids": source_ids,
        "first_supported_block_id": source_ids[0],
        "canonical_profile_first_supported_block_id": profile[
            "canonical_surface_first_supported_block_id"
        ],
        "surface_history": surface_history,
        "claim_history": claim_history,
    }


def build_global_entity_registry_v1(
    *,
    index: Mapping[str, Any],
    decisions: Sequence[Mapping[str, Any]],
    document: Mapping[str, Any],
) -> dict[str, Any]:
    verified = verify_book_entity_index_v1(index, document=document)
    chapter_order, _chapters, block_chapter, block_order = _document_catalog(document)
    decision_by_component: dict[str, dict[str, Any]] = {}
    for raw in decisions:
        decision = verify_cross_chapter_decision_v1(raw, index=verified)
        component_id = str(decision["component_id"])
        if component_id in decision_by_component:
            raise BookEntityContractError("duplicate component decision")
        decision_by_component[component_id] = decision
    required_components = {
        row["component_id"] for row in verified["components"] if not row["overflow"]
    }
    if set(decision_by_component) != required_components:
        raise BookEntityContractError("decisions do not exact-cover non-overflow components")

    candidate_by_ref = {row["candidate_ref"]: row for row in verified["candidate_rows"]}
    claim_payloads = {
        row["claim_group_id"]: row["claim_payload"] for row in verified["claim_groups"]
    }
    candidate_to_entities: dict[str, list[str]] = defaultdict(list)
    book_confirmed: list[dict[str, Any]] = []
    chapter_local: list[dict[str, Any]] = []
    pending: list[dict[str, Any]] = []
    closed: list[dict[str, Any]] = [
        {
            "candidate_ref": row["candidate_ref"],
            "chapter_id": row["chapter_id"],
            "local_candidate_id": row["local_candidate_id"],
            "source_block_ids": row["source_block_ids"],
            "first_supported_block_id": row["first_supported_block_id"],
            "closure_source": "chapter_local_auditor",
        }
        for row in verified["closed_candidate_rows"]
    ]

    component_refs = {
        ref for component in verified["components"] for ref in component["candidate_refs"]
    }
    for ref in verified["clean_candidate_refs"]:
        row = candidate_by_ref[ref]
        claim_payload = claim_payloads[row["claim_group_id"]]
        named = (
            claim_payload.get("canonical_name_class") in STABLE_NAME_CLASSES
            and bool(claim_payload.get("canonical_surface"))
            and _has_located_canonical(row)
        )
        if row["local_state"] == "pending":
            category = "pending"
        else:
            category = "book_confirmed" if named else "chapter_local"
        entity_id = _entity_id(verified["state_lineage_id"], category, [ref])
        entity = _entity_row(
            entity_id=entity_id,
            category=category,
            root_ref=ref,
            member_refs=[ref],
            candidate_by_ref=candidate_by_ref,
            claim_payloads=claim_payloads,
            block_order=block_order,
        )
        candidate_to_entities[ref].append(entity_id)
        if category == "book_confirmed":
            book_confirmed.append(entity)
        elif category == "chapter_local":
            chapter_local.append(entity)
        else:
            pending.append(entity)

    for component in verified["components"]:
        refs = component["candidate_refs"]
        if component["overflow"]:
            for ref in refs:
                entity_id = _entity_id(verified["state_lineage_id"], "pending", [ref])
                pending.append(
                    _entity_row(
                        entity_id=entity_id,
                        category="pending",
                        root_ref=ref,
                        member_refs=[ref],
                        candidate_by_ref=candidate_by_ref,
                        claim_payloads=claim_payloads,
                        block_order=block_order,
                    )
                )
                candidate_to_entities[ref].append(entity_id)
            continue
        decision = decision_by_component[component["component_id"]]
        actions = {row["candidate_ref"]: row for row in decision["candidate_actions"]}
        merged_into: dict[str, list[str]] = defaultdict(list)
        for ref, action in actions.items():
            if action["action"] == "merge_into":
                merged_into[action["target_candidate_ref"]].append(ref)
        for ref in refs:
            action = actions[ref]
            kind = action["action"]
            if kind == "merge_into":
                continue
            if kind == "reject":
                row = candidate_by_ref[ref]
                closed.append(
                    {
                        "candidate_ref": ref,
                        "chapter_id": row["chapter_id"],
                        "local_candidate_id": row["local_candidate_id"],
                        "source_block_ids": row["source_block_ids"],
                        "first_supported_block_id": row["first_supported_block_id"],
                        "closure_source": component["component_id"],
                    }
                )
                continue
            if kind == "split":
                for partition in action["split_partitions"]:
                    entity_id = _entity_id(
                        verified["state_lineage_id"],
                        "pending_split",
                        [ref],
                        partition["source_block_ids"],
                    )
                    pending.append(
                        _entity_row(
                            entity_id=entity_id,
                            category="pending",
                            root_ref=ref,
                            member_refs=[ref],
                            candidate_by_ref=candidate_by_ref,
                            claim_payloads=claim_payloads,
                            block_order=block_order,
                            partition_blocks=partition["source_block_ids"],
                            retained_surfaces=partition["retained_surfaces"],
                        )
                    )
                    candidate_to_entities[ref].append(entity_id)
                continue
            members = [ref, *sorted(merged_into.get(ref, []))]
            if kind == "pending" or any(
                candidate_by_ref[member]["local_state"] == "pending" for member in members
            ):
                category = "pending"
            else:
                root_payload = claim_payloads[candidate_by_ref[ref]["claim_group_id"]]
                named = (
                    root_payload.get("canonical_name_class") in STABLE_NAME_CLASSES
                    and bool(root_payload.get("canonical_surface"))
                    and _has_located_canonical(candidate_by_ref[ref])
                )
                category = "book_confirmed" if named else "chapter_local"
            entity_id = _entity_id(verified["state_lineage_id"], category, members)
            entity = _entity_row(
                entity_id=entity_id,
                category=category,
                root_ref=ref,
                member_refs=members,
                candidate_by_ref=candidate_by_ref,
                claim_payloads=claim_payloads,
                block_order=block_order,
            )
            for member in members:
                candidate_to_entities[member].append(entity_id)
            if category == "book_confirmed":
                book_confirmed.append(entity)
            elif category == "chapter_local":
                chapter_local.append(entity)
            else:
                pending.append(entity)

    for unresolved in verified["unresolved_candidates"]:
        entity_id = "bkent1_" + canonical_hash(
            {
                "state_lineage_id": verified["state_lineage_id"],
                "unresolved_ref": unresolved["unresolved_ref"],
            }
        )[:20]
        pending.append(
            {
                "entity_id": entity_id,
                "registry_category": "pending",
                "canonical_profile": None,
                "root_candidate_ref": unresolved["unresolved_ref"],
                "member_candidate_refs": [],
                "origin_chapter_ids": [unresolved["chapter_id"]],
                "source_block_ids": unresolved["source_block_ids"],
                "first_supported_block_id": unresolved["first_supported_block_id"],
                "claim_history": [{"unresolved_source_row": unresolved["source_row"]}],
            }
        )

    surface_decisions: dict[str, tuple[dict[str, Any], dict[str, Any]]] = {}
    for component in verified["components"]:
        if component["overflow"]:
            continue
        decision = decision_by_component[component["component_id"]]
        cases = {row["surface_case_id"]: row for row in component["contested_surfaces"]}
        for action in decision["surface_actions"]:
            surface_decisions[cases[action["surface_case_id"]]["surface_key"]] = (
                cases[action["surface_case_id"]],
                action,
            )

    entity_by_id = {
        row["entity_id"]: row for row in [*book_confirmed, *chapter_local, *pending]
    }
    surface_candidate_index: list[dict[str, Any]] = []
    for row in verified["surface_candidate_index"]:
        entity_ids = sorted(
            {
                entity_id
                for ref in row["candidate_refs"]
                for entity_id in candidate_to_entities.get(ref, [])
            }
        )
        surface_candidate_index.append(
            {
                **_clone(row),
                "entity_ids": entity_ids,
                "authority": "retrieval_only",
            }
        )

    chapter_surface_bindings: list[dict[str, Any]] = []
    for binding in verified["chapter_scoped_bindings"]:
        decision_pair = surface_decisions.get(binding["surface_key"])
        if decision_pair and decision_pair[1]["action"] == "quarantine":
            continue
        target_ids = candidate_to_entities.get(binding["target_candidate_ref"], [])
        for entity_id in target_ids:
            entity_blocks = set(entity_by_id[entity_id]["source_block_ids"])
            evidence = [
                block_id for block_id in binding["source_block_ids"] if block_id in entity_blocks
            ]
            if not evidence:
                continue
            chapter_surface_bindings.append(
                {
                    "surface_key": binding["surface_key"],
                    "scope_authority": "chapter_scoped",
                    "chapter_id": binding["chapter_id"],
                    "entity_id": entity_id,
                    "source_block_ids": evidence,
                    "first_supported_block_id": min(evidence, key=block_order.__getitem__),
                    "source_action": binding["source_action"],
                }
            )

    book_surface_bindings: list[dict[str, Any]] = []
    cross_quarantines: list[dict[str, Any]] = []
    for surface_key, (case, action) in sorted(surface_decisions.items()):
        if action["action"] == "promote_book_global":
            target_ids = candidate_to_entities.get(action["target_candidate_ref"], [])
            if len(target_ids) != 1 or target_ids[0] not in {
                row["entity_id"] for row in book_confirmed
            }:
                raise BookEntityContractError(
                    "book-global binding target is not uniquely confirmed"
                )
            evidence = sorted(action["source_block_ids"], key=block_order.__getitem__)
            book_surface_bindings.append(
                {
                    "surface_key": surface_key,
                    "scope_authority": "book_global",
                    "entity_id": target_ids[0],
                    "source_block_ids": evidence,
                    "first_supported_block_id": evidence[0],
                    "observed_chapter_ids": case["observed_chapter_ids"],
                }
            )
        elif action["action"] == "quarantine":
            evidence = sorted(action["source_block_ids"], key=block_order.__getitem__)
            cross_quarantines.append(
                {
                    "surface_key": surface_key,
                    "scope_authority": "retrieval_only",
                    "source_block_ids": evidence,
                    "first_supported_block_id": evidence[0],
                    "source": "cross_chapter_auditor",
                }
            )

    snapshot_body = {
        "schema_version": BOOK_REGISTRY_SCHEMA_VERSION,
        "validator_version": BOOK_VALIDATOR_VERSION,
        "state_lineage_id": verified["state_lineage_id"],
        "book_source_manifest_hash": verified["book_source_manifest_hash"],
        "book_index_hash": verified["book_index_hash"],
        "b2_ready": True,
        "book_confirmed_entities": sorted(book_confirmed, key=lambda row: row["entity_id"]),
        "chapter_local_entities": sorted(chapter_local, key=lambda row: row["entity_id"]),
        "pending_entities": sorted(pending, key=lambda row: row["entity_id"]),
        "pending_source_repairs": _clone(verified["pending_source_repairs"]),
        "validation_quarantines": _clone(verified["validation_quarantines"]),
        "has_open_uncertainty": bool(
            pending
            or verified["unresolved_candidates"]
            or verified["pending_source_repairs"]
            or verified["validation_quarantines"]
        ),
        "closed_candidates": sorted(
            closed, key=lambda row: (row["chapter_id"], row["candidate_ref"])
        ),
        "surface_candidate_index": sorted(
            surface_candidate_index, key=lambda row: row["surface_key"]
        ),
        "book_surface_bindings": sorted(
            book_surface_bindings, key=lambda row: row["surface_key"]
        ),
        "chapter_surface_bindings": sorted(
            chapter_surface_bindings,
            key=lambda row: (chapter_order.index(row["chapter_id"]), row["surface_key"]),
        ),
        "quarantined_surfaces": sorted(
            [*_clone(verified["local_quarantined_surfaces"]), *cross_quarantines],
            key=lambda row: (row.get("surface_key"), row.get("chapter_id", "")),
        ),
        "chapter_input_provenance": _clone(verified["ordered_chapter_inputs"]),
        "cross_chapter_decision_hashes": sorted(
            decision["decision_hash"] for decision in decision_by_component.values()
        ),
    }
    return {**snapshot_body, "snapshot_hash": canonical_hash(snapshot_body)}


def verify_global_entity_registry_v1(snapshot: Mapping[str, Any]) -> dict[str, Any]:
    if snapshot.get("schema_version") != BOOK_REGISTRY_SCHEMA_VERSION:
        raise BookEntityContractError("foreign global registry schema")
    if snapshot.get("validator_version") != BOOK_VALIDATOR_VERSION:
        raise BookEntityContractError("global registry validator mismatch")
    body = dict(snapshot)
    observed = _required_string(body.pop("snapshot_hash", None), "snapshot_hash")
    if canonical_hash(body) != observed:
        raise BookEntityContractError("global registry snapshot hash mismatch")
    if snapshot.get("b2_ready") is not True:
        raise BookEntityContractError("global registry is not B2-ready")
    if not isinstance(snapshot.get("has_open_uncertainty"), bool):
        raise BookEntityContractError("global registry uncertainty flag is missing")
    for field in ("pending_source_repairs", "validation_quarantines"):
        if not isinstance(snapshot.get(field), list):
            raise BookEntityContractError(f"global registry {field} must be a list")
    overlay_hash = snapshot.get("stable_claim_overlay_hash")
    pending_stable = snapshot.get("pending_stable_claims")
    if overlay_hash is not None:
        if (
            not isinstance(overlay_hash, str)
            or len(overlay_hash) != 64
            or any(char not in "0123456789abcdef" for char in overlay_hash)
        ):
            raise BookEntityContractError("global registry claim overlay hash is malformed")
        if not isinstance(pending_stable, list):
            raise BookEntityContractError(
                "claim-projected registry lacks pending stable claims"
            )
        entity_ids = {
            row.get("entity_id")
            for table in (
                "book_confirmed_entities",
                "chapter_local_entities",
                "pending_entities",
            )
            for row in snapshot.get(table) or []
            if isinstance(row, Mapping)
        }
        seen_pending_ids: set[str] = set()
        for row in pending_stable:
            if not isinstance(row, Mapping):
                raise BookEntityContractError("pending stable claim must be an object")
            pending_id = row.get("pending_stable_claim_id")
            if not isinstance(pending_id, str) or not pending_id:
                raise BookEntityContractError("pending stable claim id is missing")
            if pending_id in seen_pending_ids:
                raise BookEntityContractError("pending stable claim id is duplicated")
            seen_pending_ids.add(pending_id)
            if row.get("entity_id") not in entity_ids:
                raise BookEntityContractError(
                    "pending stable claim targets a foreign entity"
                )
            if row.get("field") not in {
                "referent_kind",
                "referential_gender",
                "identity_summary",
            }:
                raise BookEntityContractError("pending stable claim has a foreign field")
            if row.get("overlay_hash") != overlay_hash:
                raise BookEntityContractError("pending stable claim crosses overlays")
    elif pending_stable is not None:
        raise BookEntityContractError(
            "global registry has pending stable claims without an overlay"
        )
    expected_open = bool(
        snapshot.get("pending_entities")
        or snapshot.get("pending_source_repairs")
        or snapshot.get("validation_quarantines")
        or pending_stable
    )
    if expected_open and snapshot.get("has_open_uncertainty") is not True:
        raise BookEntityContractError("global registry hides open uncertainty")
    return _clone(dict(snapshot))


__all__ = [
    "BOOK_INDEX_SCHEMA_VERSION",
    "BOOK_REGISTRY_SCHEMA_VERSION",
    "BOOK_VALIDATOR_VERSION",
    "BookEntityContractError",
    "BookEntityRegistryError",
    "RenderedBookEntityRequestV1",
    "build_book_entity_index_v1",
    "build_global_entity_registry_v1",
    "cross_chapter_response_schema_v1",
    "render_cross_chapter_request_v1",
    "validate_cross_chapter_response_v1",
    "verify_book_entity_index_v1",
    "verify_cross_chapter_decision_v1",
    "verify_global_entity_registry_v1",
]
