"""Deterministic chapter-prefix cards for the next literary B0 pass.

The bundle keeps full claim cards for audit while exposing a separate,
field-authoritative context view to the next chapter.  It never merges local
candidate identities or promotes chapter evidence to book-wide authority.
"""

from __future__ import annotations

from copy import deepcopy
from typing import Any, Mapping, Sequence
import unicodedata

from pipeline.literary.b0_entity_inventory_experiment import (
    REFERENTIAL_GENDERS,
    REFERENT_KINDS,
)
from pipeline.literary.book_source_lineage import (
    build_book_source_manifest,
    state_lineage_id_for_manifest,
    verify_book_source_manifest,
)
from pipeline.literary.checkpoint import canonical_hash, canonical_json
from pipeline.literary.chapter_registry_v4 import route_alias_for_commit


PREFIX_PRIOR_SCHEMA_VERSION = "chapter_prefix_prior_bundle_v3"
PREFIX_PRIOR_VALIDATOR_VERSION = "chapter_prefix_prior_validator_v3"
CHAPTER_CONFIRMED_SCOPE = "chapter_confirmed_prefix"
CANDIDATE_ONLY_SCOPE = "candidate_only"
DORMANT_SCOPE = "dormant"
STABLE_NAME_CLASSES = frozenset(
    {"proper_name", "stable_nickname", "title_plus_name"}
)
CLAIM_FIELDS = (
    "referent_kind",
    "referential_gender",
    "identity_summary",
)
MAX_CONTEXT_PROVENANCE_REFS = 8


class ChapterPrefixPriorError(ValueError):
    """Raised when a chapter-prefix prior bundle is malformed or stale."""


def _clone(value: Any) -> Any:
    return deepcopy(value)


def _required_string(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ChapterPrefixPriorError(f"{label} must be a non-empty string")
    return value


def _surface_key(value: Any) -> str:
    return " ".join(unicodedata.normalize("NFKC", str(value)).casefold().split())


def _document_catalog(document: Mapping[str, Any]) -> dict[str, Any]:
    chapters = document.get("chapters")
    if not isinstance(chapters, list) or not chapters:
        raise ChapterPrefixPriorError("document has no chapters")
    chapter_by_id: dict[str, Mapping[str, Any]] = {}
    block_chapter: dict[str, str] = {}
    block_order: dict[str, int] = {}
    block_text: dict[str, str] = {}
    chapter_order: list[str] = []
    absolute_order = 0
    for raw_chapter in chapters:
        if not isinstance(raw_chapter, Mapping):
            raise ChapterPrefixPriorError("document chapter must be an object")
        chapter_id = _required_string(raw_chapter.get("chapter_id"), "chapter_id")
        if chapter_id in chapter_by_id:
            raise ChapterPrefixPriorError("document contains duplicate chapter ids")
        blocks = raw_chapter.get("blocks")
        if not isinstance(blocks, list) or not blocks:
            raise ChapterPrefixPriorError(f"chapter has no blocks: {chapter_id}")
        ordered = sorted(
            blocks,
            key=lambda row: (
                int(row.get("order_index") or 0),
                str(row.get("block_id") or ""),
            ),
        )
        for block in ordered:
            if not isinstance(block, Mapping):
                raise ChapterPrefixPriorError("document block must be an object")
            block_id = _required_string(block.get("block_id"), "block_id")
            if block_id in block_chapter:
                raise ChapterPrefixPriorError("document contains duplicate block ids")
            block_chapter[block_id] = chapter_id
            block_order[block_id] = absolute_order
            block_text[block_id] = str(
                block.get("clean_text") or block.get("text") or ""
            )
            absolute_order += 1
        chapter_order.append(chapter_id)
        chapter_by_id[chapter_id] = raw_chapter
    return {
        "chapter_order": chapter_order,
        "chapter_by_id": chapter_by_id,
        "block_chapter": block_chapter,
        "block_order": block_order,
        "block_text": block_text,
    }


def _verify_inventory(inventory: Mapping[str, Any], chapter_id: str) -> str:
    if inventory.get("chapter_id") != chapter_id:
        raise ChapterPrefixPriorError("audited inventory belongs to a foreign chapter")
    observed = _required_string(
        inventory.get("conflict_audited_inventory_hash"),
        "conflict_audited_inventory_hash",
    )
    body = dict(inventory)
    body.pop("conflict_audited_inventory_hash", None)
    if canonical_hash(body) != observed:
        raise ChapterPrefixPriorError("audited inventory hash mismatch")
    _required_string(inventory.get("source_inventory_hash"), "source_inventory_hash")
    _required_string(inventory.get("request_fingerprint"), "request_fingerprint")
    return observed


def _claim_value(row: Mapping[str, Any], field: str) -> Any:
    value = row.get(field)
    return value.get("value") if isinstance(value, Mapping) else value


def _ordered_block_ids(
    values: Sequence[Any],
    *,
    chapter_id: str,
    block_chapter: Mapping[str, str],
    block_order: Mapping[str, int],
    allow_empty: bool = False,
) -> list[str]:
    rows = {str(value or "") for value in values if str(value or "")}
    if not rows and not allow_empty:
        raise ChapterPrefixPriorError("entity lacks source block provenance")
    foreign = sorted(
        block_id
        for block_id in rows
        if block_chapter.get(block_id) != chapter_id
    )
    if foreign:
        raise ChapterPrefixPriorError(
            f"entity cites foreign source blocks: {foreign}"
        )
    return sorted(rows, key=block_order.__getitem__)


def _bounded_provenance(
    source_ids: Sequence[str],
    semantic_ids: Sequence[str],
    *,
    chapter_id: str,
    block_order: Mapping[str, int],
) -> list[dict[str, str]]:
    priorities: list[str] = []

    def add(block_id: str) -> None:
        if block_id and block_id not in priorities:
            priorities.append(block_id)

    ordered_source = sorted(set(source_ids), key=block_order.__getitem__)
    for block_id in sorted(set(semantic_ids), key=block_order.__getitem__):
        add(block_id)
    if ordered_source:
        add(ordered_source[0])
        add(ordered_source[-1])
    for block_id in ordered_source:
        add(block_id)
    return [
        {"chapter_id": chapter_id, "block_id": block_id}
        for block_id in priorities[:MAX_CONTEXT_PROVENANCE_REFS]
    ]


def _is_title_base_alias(
    *,
    canonical_surface: str,
    canonical_name_class: Any,
    alternative_surface: str,
) -> bool:
    if canonical_name_class != "title_plus_name":
        return False
    canonical_tokens = canonical_surface.split()
    alternative_tokens = alternative_surface.split()
    if len(canonical_tokens) != len(alternative_tokens) + 1:
        return False
    if [_surface_key(value) for value in canonical_tokens[1:]] != [
        _surface_key(value) for value in alternative_tokens
    ]:
        return False
    return any(char.isupper() for char in alternative_surface)


def _stable_surfaces(
    row: Mapping[str, Any],
    *,
    candidate_id: str,
    source_catalog: Mapping[str, str],
    source_inventory_hash: str,
) -> list[str]:
    surfaces: list[str] = []

    def add(surface: Any) -> None:
        value = str(surface or "")
        if value and value not in surfaces:
            surfaces.append(value)

    canonical_surface = str(row.get("canonical_surface") or "")
    canonical_name_class = row.get("canonical_name_class")
    if canonical_name_class in STABLE_NAME_CLASSES:
        add(canonical_surface)
    for raw in row.get("alternative_names") or []:
        if not isinstance(raw, Mapping):
            continue
        if raw.get("name_class") not in STABLE_NAME_CLASSES:
            continue
        if raw.get("address_validation_state", "valid") != "valid":
            continue
        surface = str(raw.get("surface") or "")
        source_block_ids = list(
            raw.get("surface_match_block_ids")
            or raw.get("source_block_ids")
            or []
        )
        if not surface or not source_block_ids:
            continue
        gate = route_alias_for_commit(
            surface=surface,
            name_class=str(raw["name_class"]),
            target_entity_id=candidate_id,
            source_block_ids=source_block_ids,
            source_catalog=source_catalog,
            source_decision_lineage={
                "source_inventory_hash": source_inventory_hash,
                "candidate_id": candidate_id,
            },
        )
        if gate["outcome"] == "eligible_global_alias" or _is_title_base_alias(
            canonical_surface=canonical_surface,
            canonical_name_class=canonical_name_class,
            alternative_surface=surface,
        ):
            add(surface)
    return surfaces


def _entity_rows(inventory: Mapping[str, Any]) -> list[tuple[str, Mapping[str, Any]]]:
    rows: list[tuple[str, Mapping[str, Any]]] = []
    seen: set[str] = set()
    for table, state in (
        ("entity_candidates", "confirmed"),
        ("pending_entity_candidates", "pending"),
    ):
        raw_rows = inventory.get(table) or []
        if not isinstance(raw_rows, list):
            raise ChapterPrefixPriorError(f"{table} must be a list")
        for raw in raw_rows:
            if not isinstance(raw, Mapping):
                raise ChapterPrefixPriorError(f"{table} row must be an object")
            candidate_id = _required_string(raw.get("candidate_id"), "candidate_id")
            if candidate_id in seen:
                raise ChapterPrefixPriorError("duplicate candidate id across tables")
            seen.add(candidate_id)
            rows.append((state, raw))
    return rows


def _glossary_rows(
    inventory: Mapping[str, Any],
) -> list[tuple[str, Mapping[str, Any]]]:
    rows: list[tuple[str, Mapping[str, Any]]] = []
    seen: set[str] = set()
    for table, lifecycle in (
        ("glossary_candidates", "chapter_confirmed"),
        ("pending_glossary_candidates", "pending_evidence"),
        ("dormant_glossary_candidates", "rejected_dormant"),
    ):
        raw_rows = inventory.get(table) or []
        if not isinstance(raw_rows, list):
            raise ChapterPrefixPriorError(f"{table} must be a list")
        for raw in raw_rows:
            if not isinstance(raw, Mapping):
                raise ChapterPrefixPriorError(f"{table} row must be an object")
            candidate_id = _required_string(raw.get("candidate_id"), "candidate_id")
            if candidate_id in seen:
                raise ChapterPrefixPriorError("duplicate glossary id across tables")
            seen.add(candidate_id)
            rows.append((lifecycle, raw))
    return rows


def _context_card(
    *,
    prior_card_id: str,
    canonical_surface: str,
    stable_surfaces: Sequence[str],
    effective_claims: Mapping[str, Any],
    authority_scope: str,
    first_supported_block_id: str,
    provenance_refs: Sequence[Mapping[str, str]],
    source_candidate_id: str,
    disputed_claims: Sequence[Mapping[str, Any]] = (),
) -> dict[str, Any]:
    body = {
        "prior_card_id": prior_card_id,
        "canonical_surface": canonical_surface,
        "stable_surfaces": list(stable_surfaces),
        "effective_claims": {
            field: _clone(effective_claims.get(field)) for field in CLAIM_FIELDS
        },
        "disputed_claims": [_clone(dict(row)) for row in disputed_claims],
        "authority_scope": authority_scope,
        "first_supported_block_id": first_supported_block_id,
        "provenance_refs": [_clone(dict(row)) for row in provenance_refs],
        "source_candidate_id": source_candidate_id,
    }
    return {**body, "context_card_hash": canonical_hash(body)}


def _glossary_context_card(
    *,
    glossary_card_id: str,
    surface: str,
    category_claim: str,
    local_sense: str,
    preferred_rendering_vi: str | None,
    render_policy: str,
    lifecycle_state: str,
    authority_scope: str,
    first_supported_block_id: str,
    provenance_refs: Sequence[Mapping[str, str]],
    source_candidate_id: str,
    cross_chapter_dispositions: Sequence[Mapping[str, Any]] = (),
    hearing_count: int = 1,
) -> dict[str, Any]:
    body = {
        "glossary_card_id": glossary_card_id,
        "surface": surface,
        "stable_surfaces": [surface],
        "category_claim": category_claim,
        "local_sense": local_sense,
        "preferred_rendering_vi": preferred_rendering_vi,
        "render_policy": render_policy,
        "lifecycle_state": lifecycle_state,
        "authority_scope": authority_scope,
        "first_supported_block_id": first_supported_block_id,
        "provenance_refs": [_clone(dict(row)) for row in provenance_refs],
        "source_candidate_id": source_candidate_id,
        "cross_chapter_dispositions": [
            _clone(dict(row)) for row in cross_chapter_dispositions
        ],
        "hearing_count": int(hearing_count),
        "same_evidence_reopen_forbidden": True,
    }
    return {**body, "glossary_card_hash": canonical_hash(body)}


def build_chapter_prefix_prior_bundle_v1(
    *,
    document: Mapping[str, Any],
    audited_inventory: Mapping[str, Any],
    coverage_through_chapter_id: str,
) -> dict[str, Any]:
    """Project one audited chapter into history and next-B0 context cards."""

    catalog = _document_catalog(document)
    chapter_id = _required_string(
        coverage_through_chapter_id, "coverage_through_chapter_id"
    )
    if chapter_id not in catalog["chapter_by_id"]:
        raise ChapterPrefixPriorError("coverage chapter is absent from document")
    inventory_hash = _verify_inventory(audited_inventory, chapter_id)
    source_manifest = build_book_source_manifest(document)
    verify_book_source_manifest(document, source_manifest)
    lineage_id = state_lineage_id_for_manifest(source_manifest)
    claim_cards: list[dict[str, Any]] = []
    active_context: list[dict[str, Any]] = []
    candidate_context: list[dict[str, Any]] = []
    source_rows: list[dict[str, Any]] = []
    glossary_context: list[dict[str, Any]] = []
    source_glossary_rows: list[dict[str, Any]] = []

    for local_state, row in _entity_rows(audited_inventory):
        candidate_id = _required_string(row.get("candidate_id"), "candidate_id")
        canonical_surface = _required_string(
            row.get("canonical_surface"), "canonical_surface"
        )
        source_ids = _ordered_block_ids(
            list(row.get("source_block_ids") or []),
            chapter_id=chapter_id,
            block_chapter=catalog["block_chapter"],
            block_order=catalog["block_order"],
        )
        semantic_ids: list[str] = []
        for field in ("referent_kind_claim", "referential_gender_claim"):
            claim = row.get(field)
            if isinstance(claim, Mapping):
                semantic_ids.extend(str(value) for value in claim.get("support_block_ids") or [])
        semantic_ids = _ordered_block_ids(
            semantic_ids,
            chapter_id=chapter_id,
            block_chapter=catalog["block_chapter"],
            block_order=catalog["block_order"],
            allow_empty=True,
        )
        provenance_refs = _bounded_provenance(
            source_ids,
            semantic_ids,
            chapter_id=chapter_id,
            block_order=catalog["block_order"],
        )
        surfaces = _stable_surfaces(
            row,
            candidate_id=candidate_id,
            source_catalog=catalog["block_text"],
            source_inventory_hash=inventory_hash,
        )
        named = bool(surfaces) and canonical_surface in surfaces
        effective_claims = {
            "referent_kind": _claim_value(row, "referent_kind_claim"),
            "referential_gender": _claim_value(row, "referential_gender_claim"),
            "identity_summary": row.get("identity_summary_draft"),
        }
        if effective_claims["referent_kind"] not in REFERENT_KINDS:
            raise ChapterPrefixPriorError("entity has invalid referent kind")
        gender = effective_claims["referential_gender"]
        if gender is not None and gender not in REFERENTIAL_GENDERS:
            raise ChapterPrefixPriorError("entity has invalid referential gender")
        if not isinstance(effective_claims["identity_summary"], str) or not str(
            effective_claims["identity_summary"]
        ).strip():
            raise ChapterPrefixPriorError("entity has no stable identity summary")
        prior_card_id = "pcard1_" + canonical_hash(
            {
                "state_lineage_id": lineage_id,
                "chapter_id": chapter_id,
                "candidate_id": candidate_id,
                "source_row_hash": canonical_hash(row),
            }
        )[:20]
        authority = (
            CHAPTER_CONFIRMED_SCOPE
            if local_state == "confirmed" and named
            else CANDIDATE_ONLY_SCOPE
        )
        context = _context_card(
            prior_card_id=prior_card_id,
            canonical_surface=canonical_surface,
            stable_surfaces=surfaces or [canonical_surface],
            effective_claims=effective_claims,
            authority_scope=authority,
            first_supported_block_id=source_ids[0],
            provenance_refs=provenance_refs,
            source_candidate_id=candidate_id,
        )
        (active_context if authority == CHAPTER_CONFIRMED_SCOPE else candidate_context).append(
            context
        )
        if authority == CHAPTER_CONFIRMED_SCOPE:
            claim_cards.append(
                {
                    "prior_card_id": prior_card_id,
                    "canonical_surface": canonical_surface,
                    "stable_surfaces": surfaces,
                    "referent_kind": effective_claims["referent_kind"],
                    "referential_gender": gender,
                    "identity_summary": effective_claims["identity_summary"],
                    "authority_scope": CHAPTER_CONFIRMED_SCOPE,
                    "first_supported_block_id": source_ids[0],
                    "provenance_refs": provenance_refs,
                }
            )
        source_rows.append(
            {
                "prior_card_id": prior_card_id,
                "source_candidate_id": candidate_id,
                "local_state": local_state,
                "authority_scope": authority,
                "source_row_hash": canonical_hash(row),
                "all_source_block_ids": source_ids,
            }
        )

    for offset, raw in enumerate(audited_inventory.get("unresolved_referents") or []):
        if not isinstance(raw, Mapping):
            raise ChapterPrefixPriorError("unresolved referent must be an object")
        source_values = list(raw.get("source_block_ids") or [])
        if not source_values:
            if (
                raw.get("lifecycle_state") != "dormant_unresolved"
                or raw.get("publication_state") != "not_published"
            ):
                raise ChapterPrefixPriorError("entity lacks source block provenance")
            # A dormant unresolved row has no authority. When the model cited the
            # wrong address, retain it only at literal locations found by code;
            # never treat the model's proposed semantic blocks as validated source.
            source_values = list(raw.get("surface_match_block_ids") or [])
            if not source_values:
                # The audited inventory remains the durable source for the
                # review-case ledger.  A row with no literal location cannot
                # become a prefix card, even as candidate-only context, because
                # that would turn an ungrounded model proposal into retrieval
                # authority for later chapters.
                continue
        source_ids = _ordered_block_ids(
            source_values,
            chapter_id=chapter_id,
            block_chapter=catalog["block_chapter"],
            block_order=catalog["block_order"],
        )
        source_candidate_id = str(raw.get("candidate_id") or f"unresolved_{offset}")
        prior_card_id = "pcard1_" + canonical_hash(
            {
                "state_lineage_id": lineage_id,
                "chapter_id": chapter_id,
                "unresolved_row_hash": canonical_hash(raw),
                "offset": offset,
            }
        )[:20]
        candidate_context.append(
            _context_card(
                prior_card_id=prior_card_id,
                canonical_surface=_required_string(raw.get("surface"), "unresolved surface"),
                stable_surfaces=[str(raw["surface"])],
                effective_claims={
                    "referent_kind": raw.get("referent_kind_claim"),
                    "referential_gender": None,
                    "identity_summary": raw.get("short_description"),
                },
                authority_scope=CANDIDATE_ONLY_SCOPE,
                first_supported_block_id=source_ids[0],
                provenance_refs=_bounded_provenance(
                    source_ids,
                    [],
                    chapter_id=chapter_id,
                    block_order=catalog["block_order"],
                ),
                source_candidate_id=source_candidate_id,
            )
        )
        source_rows.append(
            {
                "prior_card_id": prior_card_id,
                "source_candidate_id": source_candidate_id,
                "local_state": "unresolved",
                "authority_scope": CANDIDATE_ONLY_SCOPE,
                "source_row_hash": canonical_hash(raw),
                "all_source_block_ids": source_ids,
            }
        )

    for lifecycle_state, row in _glossary_rows(audited_inventory):
        candidate_id = _required_string(row.get("candidate_id"), "glossary candidate_id")
        surface = _required_string(row.get("surface"), "glossary surface")
        source_ids = _ordered_block_ids(
            list(row.get("source_block_ids") or []),
            chapter_id=chapter_id,
            block_chapter=catalog["block_chapter"],
            block_order=catalog["block_order"],
            allow_empty=(row.get("publication_state") == "pending_source_repair"),
        )
        if not source_ids:
            source_ids = _ordered_block_ids(
                list(row.get("surface_match_block_ids") or []),
                chapter_id=chapter_id,
                block_chapter=catalog["block_chapter"],
                block_order=catalog["block_order"],
            )
        authority_scope = {
            "chapter_confirmed": CHAPTER_CONFIRMED_SCOPE,
            "pending_evidence": CANDIDATE_ONLY_SCOPE,
            "rejected_dormant": DORMANT_SCOPE,
        }[lifecycle_state]
        glossary_card_id = "gcard1_" + canonical_hash(
            {
                "state_lineage_id": lineage_id,
                "chapter_id": chapter_id,
                "candidate_id": candidate_id,
                "source_row_hash": canonical_hash(row),
            }
        )[:20]
        provenance_refs = _bounded_provenance(
            source_ids,
            [],
            chapter_id=chapter_id,
            block_order=catalog["block_order"],
        )
        glossary_context.append(
            _glossary_context_card(
                glossary_card_id=glossary_card_id,
                surface=surface,
                category_claim=_required_string(
                    row.get("category_claim"), "glossary category_claim"
                ),
                local_sense=_required_string(
                    row.get("short_description"), "glossary short_description"
                ),
                preferred_rendering_vi=(
                    str(row["preferred_rendering_vi"])
                    if row.get("preferred_rendering_vi") is not None
                    else None
                ),
                render_policy=str(row.get("render_policy") or "none"),
                lifecycle_state=lifecycle_state,
                authority_scope=authority_scope,
                first_supported_block_id=source_ids[0],
                provenance_refs=provenance_refs,
                source_candidate_id=candidate_id,
            )
        )
        source_glossary_rows.append(
            {
                "glossary_card_id": glossary_card_id,
                "source_candidate_id": candidate_id,
                "lifecycle_state": lifecycle_state,
                "authority_scope": authority_scope,
                "source_row_hash": canonical_hash(row),
                "all_source_block_ids": source_ids,
            }
        )

    body = {
        "schema_version": PREFIX_PRIOR_SCHEMA_VERSION,
        "validator_version": PREFIX_PRIOR_VALIDATOR_VERSION,
        "state_lineage_id": lineage_id,
        "book_source_manifest_hash": source_manifest["manifest_hash"],
        "coverage_through_chapter_id": chapter_id,
        "covered_chapter_ids": [chapter_id],
        "audited_inventory_provenance": [
            {
                "chapter_id": chapter_id,
                "conflict_audited_inventory_hash": inventory_hash,
                "source_inventory_hash": audited_inventory["source_inventory_hash"],
                "request_fingerprint": audited_inventory["request_fingerprint"],
            }
        ],
        "claim_cards": sorted(claim_cards, key=lambda row: row["prior_card_id"]),
        "b0_context_cards": sorted(active_context, key=lambda row: row["prior_card_id"]),
        "candidate_only_context_cards": sorted(
            candidate_context, key=lambda row: row["prior_card_id"]
        ),
        "source_entity_manifest": sorted(
            source_rows, key=lambda row: row["prior_card_id"]
        ),
        "glossary_context_cards": sorted(
            glossary_context, key=lambda row: row["glossary_card_id"]
        ),
        "source_glossary_manifest": sorted(
            source_glossary_rows, key=lambda row: row["glossary_card_id"]
        ),
        "claim_projection_hashes": [],
        "glossary_projection_hashes": [],
        "prefix_identity_uncertainties": [],
        "production_publish_performed": False,
    }
    return {**body, "prefix_bundle_hash": canonical_hash(body)}


def verify_chapter_prefix_prior_bundle_v1(
    bundle: Mapping[str, Any], *, document: Mapping[str, Any] | None = None
) -> dict[str, Any]:
    if bundle.get("schema_version") != PREFIX_PRIOR_SCHEMA_VERSION:
        raise ChapterPrefixPriorError("foreign prefix-prior schema")
    if bundle.get("validator_version") != PREFIX_PRIOR_VALIDATOR_VERSION:
        raise ChapterPrefixPriorError("prefix-prior validator mismatch")
    body = dict(bundle)
    observed = _required_string(body.pop("prefix_bundle_hash", None), "prefix_bundle_hash")
    if canonical_hash(body) != observed:
        raise ChapterPrefixPriorError("prefix-prior bundle hash mismatch")
    if bundle.get("production_publish_performed") is not False:
        raise ChapterPrefixPriorError("prefix-prior bundle claims production publication")
    if document is not None:
        manifest = build_book_source_manifest(document)
        if manifest["manifest_hash"] != bundle.get("book_source_manifest_hash"):
            raise ChapterPrefixPriorError("prefix-prior source manifest is stale")
        if state_lineage_id_for_manifest(manifest) != bundle.get("state_lineage_id"):
            raise ChapterPrefixPriorError("prefix-prior lineage is stale")
        catalog = _document_catalog(document)
        if bundle.get("coverage_through_chapter_id") not in catalog["chapter_by_id"]:
            raise ChapterPrefixPriorError("prefix-prior coverage is foreign")
    projection_hashes = bundle.get("claim_projection_hashes")
    if not isinstance(projection_hashes, list) or len(projection_hashes) != len(
        set(projection_hashes)
    ):
        raise ChapterPrefixPriorError("claim projection hash history is malformed")
    for projection_hash in projection_hashes:
        if not isinstance(projection_hash, str) or len(projection_hash) != 64:
            raise ChapterPrefixPriorError("claim projection hash is malformed")
    glossary_projection_hashes = bundle.get("glossary_projection_hashes")
    if not isinstance(glossary_projection_hashes, list) or len(
        glossary_projection_hashes
    ) != len(set(glossary_projection_hashes)):
        raise ChapterPrefixPriorError("glossary projection hash history is malformed")
    for projection_hash in glossary_projection_hashes:
        if not isinstance(projection_hash, str) or len(projection_hash) != 64:
            raise ChapterPrefixPriorError("glossary projection hash is malformed")
    claim_cards = bundle.get("claim_cards")
    active_cards = bundle.get("b0_context_cards")
    candidate_cards = bundle.get("candidate_only_context_cards")
    if not all(isinstance(rows, list) for rows in (claim_cards, active_cards, candidate_cards)):
        raise ChapterPrefixPriorError("prefix card collections must be lists")
    glossary_cards = bundle.get("glossary_context_cards")
    glossary_manifest = bundle.get("source_glossary_manifest")
    if not isinstance(glossary_cards, list) or not isinstance(glossary_manifest, list):
        raise ChapterPrefixPriorError("prefix glossary collections must be lists")
    glossary_ids: set[str] = set()
    allowed_glossary_states = {
        "chapter_confirmed": CHAPTER_CONFIRMED_SCOPE,
        "pending_evidence": CANDIDATE_ONLY_SCOPE,
        "rejected_dormant": DORMANT_SCOPE,
    }
    for row in glossary_cards:
        if not isinstance(row, Mapping):
            raise ChapterPrefixPriorError("glossary context card must be an object")
        glossary_id = _required_string(row.get("glossary_card_id"), "glossary_card_id")
        if glossary_id in glossary_ids:
            raise ChapterPrefixPriorError("glossary context cards repeat an id")
        glossary_ids.add(glossary_id)
        lifecycle = row.get("lifecycle_state")
        if lifecycle not in allowed_glossary_states:
            raise ChapterPrefixPriorError("glossary context card has foreign lifecycle")
        if row.get("authority_scope") != allowed_glossary_states[lifecycle]:
            raise ChapterPrefixPriorError("glossary lifecycle and authority disagree")
        if row.get("render_policy") not in {"advisory_meaning", "none"}:
            raise ChapterPrefixPriorError("glossary render policy is invalid")
        if lifecycle != "chapter_confirmed" and (
            row.get("preferred_rendering_vi") is not None
            or row.get("render_policy") != "none"
        ):
            raise ChapterPrefixPriorError("non-confirmed glossary carries rendering authority")
        card_body = dict(row)
        observed_card_hash = _required_string(
            card_body.pop("glossary_card_hash", None), "glossary_card_hash"
        )
        if canonical_hash(card_body) != observed_card_hash:
            raise ChapterPrefixPriorError("glossary context card hash mismatch")
    manifest_ids = {
        _required_string(row.get("glossary_card_id"), "glossary_card_id")
        for row in glossary_manifest
        if isinstance(row, Mapping)
    }
    if len(manifest_ids) != len(glossary_manifest) or manifest_ids != glossary_ids:
        raise ChapterPrefixPriorError("glossary source manifest does not exact-cover cards")
    uncertainties = bundle.get("prefix_identity_uncertainties")
    if not isinstance(uncertainties, list):
        raise ChapterPrefixPriorError("prefix identity uncertainties must be a list")
    active_ids = {
        row.get("prior_card_id") for row in active_cards
    }
    claim_ids = {row.get("prior_card_id") for row in claim_cards}
    candidate_ids = {
        row.get("prior_card_id")
        for row in candidate_cards
    }
    if len(claim_ids) != len(claim_cards):
        raise ChapterPrefixPriorError("prefix claim cards repeat an id")
    if len(active_ids) != len(active_cards) or len(candidate_ids) != len(candidate_cards):
        raise ChapterPrefixPriorError("prefix context cards repeat an id")
    if active_ids.intersection(candidate_ids):
        raise ChapterPrefixPriorError("active and candidate-only cards overlap")
    if not active_ids.issubset(claim_ids):
        raise ChapterPrefixPriorError("active context contains a foreign claim card")
    if not claim_ids.issubset(active_ids.union(candidate_ids)):
        raise ChapterPrefixPriorError(
            "claim history is absent from both active and candidate-only context"
        )
    for row in active_cards:
        if row.get("authority_scope") != CHAPTER_CONFIRMED_SCOPE:
            raise ChapterPrefixPriorError("active context card lacks chapter authority")
        card_body = dict(row)
        card_hash = _required_string(card_body.pop("context_card_hash", None), "context card hash")
        if canonical_hash(card_body) != card_hash:
            raise ChapterPrefixPriorError("active context card hash mismatch")
    for row in candidate_cards:
        if row.get("authority_scope") != CANDIDATE_ONLY_SCOPE:
            raise ChapterPrefixPriorError("candidate-only card has unsafe authority")
        card_body = dict(row)
        card_hash = _required_string(card_body.pop("context_card_hash", None), "context card hash")
        if canonical_hash(card_body) != card_hash:
            raise ChapterPrefixPriorError("candidate-only context card hash mismatch")
    uncertainty_ids: set[str] = set()
    allowed_uncertainty_reasons = {
        "cross_chapter_surface_collision": 2,
        "semantic_candidate_first_recurrence_weak_card": 1,
        "semantic_candidate_person_terminal_token_overlap": 2,
        "semantic_candidate_surface_core_overlap": 2,
        "semantic_candidate_exact_surface_collision": 2,
    }
    context_ids = active_ids.union(candidate_ids)
    for row in uncertainties:
        if not isinstance(row, Mapping):
            raise ChapterPrefixPriorError("prefix identity uncertainty must be an object")
        uncertainty_id = _required_string(row.get("uncertainty_id"), "uncertainty_id")
        if uncertainty_id in uncertainty_ids:
            raise ChapterPrefixPriorError("prefix identity uncertainty repeats an id")
        uncertainty_ids.add(uncertainty_id)
        owner_ids = row.get("prior_card_ids")
        chapter_ids = row.get("chapter_ids")
        if (
            not isinstance(owner_ids, list)
            or len(owner_ids) < allowed_uncertainty_reasons.get(
                str(row.get("reason_code") or ""), 999
            )
            or len(owner_ids) != len(set(owner_ids))
            or not set(owner_ids).issubset(context_ids)
        ):
            raise ChapterPrefixPriorError("prefix identity owners are malformed")
        if (
            not isinstance(chapter_ids, list)
            or len(chapter_ids) < 2
            or len(chapter_ids) != len(set(chapter_ids))
        ):
            raise ChapterPrefixPriorError("prefix identity chapters are malformed")
        if document is not None and not set(chapter_ids).issubset(
            set(catalog["chapter_by_id"])
        ):
            raise ChapterPrefixPriorError("prefix identity uncertainty cites a foreign chapter")
        if row.get("status") != "pending_identity_review":
            raise ChapterPrefixPriorError("prefix identity uncertainty has unsafe status")
        if row.get("authority_effect") != "candidate_only":
            raise ChapterPrefixPriorError("prefix identity uncertainty grants authority")
        if row.get("reason_code") not in allowed_uncertainty_reasons:
            raise ChapterPrefixPriorError("prefix identity uncertainty has a foreign reason")
        if str(row.get("reason_code")).startswith("semantic_candidate_"):
            lead_id = _required_string(
                row.get("semantic_candidate_lead_id"),
                "semantic_candidate_lead_id",
            )
            if not lead_id.startswith("semlead1_"):
                raise ChapterPrefixPriorError("semantic lead id has a foreign prefix")
            evidence_hash = _required_string(
                row.get("evidence_manifest_hash"),
                "semantic evidence_manifest_hash",
            )
            if len(evidence_hash) != 64 or any(
                char not in "0123456789abcdef" for char in evidence_hash
            ):
                raise ChapterPrefixPriorError("semantic evidence hash is malformed")
            source_ids = row.get("source_block_ids")
            if (
                not isinstance(source_ids, list)
                or not source_ids
                or len(source_ids) != len(set(source_ids))
                or not all(isinstance(value, str) and value for value in source_ids)
            ):
                raise ChapterPrefixPriorError("semantic uncertainty sources are malformed")
            if document is not None and not set(source_ids).issubset(
                set(catalog["block_chapter"])
            ):
                raise ChapterPrefixPriorError("semantic uncertainty cites a foreign block")
            if not isinstance(row.get("review_deferred"), bool):
                raise ChapterPrefixPriorError("semantic uncertainty deferral is malformed")
        body = {key: _clone(value) for key, value in row.items() if key != "uncertainty_id"}
        expected_id = "prefixunc1_" + canonical_hash(body)[:20]
        if uncertainty_id != expected_id:
            raise ChapterPrefixPriorError("prefix identity uncertainty id is stale")
    return _clone(dict(bundle))


def apply_claim_projection_to_prefix_bundle_v1(
    *,
    bundle: Mapping[str, Any],
    projection: Mapping[str, Any],
) -> dict[str, Any]:
    """Apply an append-only claim projection to B0 context, not claim history."""

    from pipeline.literary.book_entity_claim_auditor_v1 import (
        verify_prior_claim_projection_v1,
    )
    from pipeline.literary.b0_entity_prior_challenge_experiment import (
        B0PriorChallengeError,
        validate_prior_cards,
    )

    verified = verify_chapter_prefix_prior_bundle_v1(bundle)
    verified_projection = verify_prior_claim_projection_v1(projection)
    if verified_projection.get("state_lineage_id") != verified["state_lineage_id"]:
        raise ChapterPrefixPriorError("claim projection belongs to a foreign lineage")
    projected_rows = verified_projection.get("projected_prior_cards")
    if not isinstance(projected_rows, list):
        raise ChapterPrefixPriorError("claim projection has no projected cards")
    by_id = {row["prior_card_id"]: row for row in verified["b0_context_cards"]}
    candidate_by_id = {
        row["prior_card_id"]: row
        for row in verified["candidate_only_context_cards"]
    }
    claim_by_id = {row["prior_card_id"]: row for row in verified["claim_cards"]}
    try:
        normalized_claim_by_id = {
            row["prior_card_id"]: row
            for row in validate_prior_cards(
                verified["claim_cards"],
                maximum=None,
            )
        }
    except B0PriorChallengeError as exc:
        raise ChapterPrefixPriorError(
            "prefix claim card failed canonical validation"
        ) from exc
    seen: set[str] = set()
    for raw in projected_rows:
        if not isinstance(raw, Mapping):
            raise ChapterPrefixPriorError("projected prior card must be an object")
        prior_card_id = _required_string(raw.get("prior_card_id"), "prior_card_id")
        if prior_card_id not in claim_by_id or prior_card_id in seen:
            raise ChapterPrefixPriorError("claim projection targets a foreign/duplicate card")
        seen.add(prior_card_id)
        if raw.get("source_prior_card_hash") != canonical_hash(
            normalized_claim_by_id[prior_card_id]
        ):
            raise ChapterPrefixPriorError("claim projection card hash mismatch")
        projected_effective = raw.get("effective_claims")
        projected_disputes = raw.get("disputed_claims")
        claim_states = raw.get("claim_states")
        if (
            not isinstance(projected_effective, Mapping)
            or not isinstance(projected_disputes, list)
            or not isinstance(claim_states, list)
        ):
            raise ChapterPrefixPriorError("claim projection lacks effective/disputed claims")
        touched_fields = {
            str(row.get("disputed_field"))
            for row in [*claim_states, *projected_disputes]
            if isinstance(row, Mapping) and row.get("disputed_field")
        }
        if not touched_fields:
            continue
        allowed_fields = {
            "referent_kind",
            "referential_gender",
            "identity_summary",
            "identity_membership",
            "alias_target",
            "alias_scope",
        }
        if not touched_fields <= allowed_fields:
            raise ChapterPrefixPriorError("claim projection touches a foreign field")

        source = by_id.pop(prior_card_id, None)
        if source is None:
            source = candidate_by_id.pop(prior_card_id, None)
        if source is None:
            raise ChapterPrefixPriorError(
                "claim projection target has no current context card"
            )

        effective = _clone(source["effective_claims"])
        for field in touched_fields.intersection(CLAIM_FIELDS):
            effective[field] = _clone(projected_effective.get(field))

        disputes = [
            _clone(row)
            for row in source["disputed_claims"]
            if isinstance(row, Mapping)
            and row.get("disputed_field") not in touched_fields
        ]
        disputes.extend(_clone(row) for row in projected_disputes)
        disputes.sort(
            key=lambda row: (
                str(row.get("disputed_field") or ""),
                str(row.get("uncertainty_id") or ""),
            )
        )
        has_identity_dispute = any(
            row.get("disputed_field")
            in {"identity_membership", "alias_target", "alias_scope"}
            for row in disputes
            if isinstance(row, Mapping)
        )
        authority_scope = (
            CANDIDATE_ONLY_SCOPE
            if has_identity_dispute
            else CHAPTER_CONFIRMED_SCOPE
        )
        updated = _context_card(
            prior_card_id=prior_card_id,
            canonical_surface=source["canonical_surface"],
            stable_surfaces=source["stable_surfaces"],
            effective_claims=effective,
            authority_scope=authority_scope,
            first_supported_block_id=source["first_supported_block_id"],
            provenance_refs=source["provenance_refs"],
            source_candidate_id=source["source_candidate_id"],
            disputed_claims=disputes,
        )
        if updated["authority_scope"] == CANDIDATE_ONLY_SCOPE:
            candidate_by_id[prior_card_id] = updated
        else:
            by_id[prior_card_id] = updated
    body = {
        key: _clone(value)
        for key, value in verified.items()
        if key != "prefix_bundle_hash"
    }
    body["b0_context_cards"] = sorted(by_id.values(), key=lambda row: row["prior_card_id"])
    body["candidate_only_context_cards"] = sorted(
        candidate_by_id.values(), key=lambda row: row["prior_card_id"]
    )
    projection_hash = _required_string(
        verified_projection.get("projection_hash"), "projection_hash"
    )
    body["claim_projection_hashes"] = sorted(
        set(body["claim_projection_hashes"]).union({projection_hash})
    )
    body["production_publish_performed"] = False
    result = {**body, "prefix_bundle_hash": canonical_hash(body)}
    return verify_chapter_prefix_prior_bundle_v1(result)


def apply_glossary_dispositions_to_prefix_bundle_v1(
    *,
    bundle: Mapping[str, Any],
    challenge_artifact: Mapping[str, Any],
) -> dict[str, Any]:
    """Apply current-chapter glossary evidence without granting book authority."""

    verified = verify_chapter_prefix_prior_bundle_v1(bundle)
    challenge_body = dict(challenge_artifact)
    challenge_hash = _required_string(
        challenge_body.pop("prior_challenge_artifact_hash", None),
        "prior_challenge_artifact_hash",
    )
    if canonical_hash(challenge_body) != challenge_hash:
        raise ChapterPrefixPriorError("prior challenge artifact hash mismatch")
    if challenge_hash in verified["glossary_projection_hashes"]:
        return verified
    chapter_id = _required_string(challenge_artifact.get("chapter_id"), "chapter_id")
    dispositions = challenge_artifact.get("prior_glossary_dispositions")
    presence = challenge_artifact.get("code_derived_glossary_presence")
    if not isinstance(dispositions, list) or not isinstance(presence, list):
        raise ChapterPrefixPriorError("challenge lacks glossary evidence collections")
    presence_ids = {
        _required_string(row.get("glossary_card_id"), "glossary_card_id")
        for row in presence
        if isinstance(row, Mapping)
    }
    cards = {
        row["glossary_card_id"]: _clone(row)
        for row in verified["glossary_context_cards"]
    }
    seen: set[str] = set()
    for raw in dispositions:
        if not isinstance(raw, Mapping):
            raise ChapterPrefixPriorError("glossary disposition must be an object")
        card_id = _required_string(raw.get("glossary_card_id"), "glossary_card_id")
        if card_id in seen or card_id not in cards or card_id not in presence_ids:
            raise ChapterPrefixPriorError(
                "glossary disposition targets a foreign/duplicate card"
            )
        seen.add(card_id)
        verdict = raw.get("verdict")
        if verdict not in {"compatible", "challenge", "uncertain"}:
            raise ChapterPrefixPriorError("glossary disposition has foreign verdict")
        source_block_ids = [
            _required_string(block_id, "source_block_id")
            for block_id in raw.get("source_block_ids") or []
        ]
        if not source_block_ids:
            raise ChapterPrefixPriorError("glossary disposition has no evidence")
        source = cards[card_id]
        provenance = [dict(row) for row in source["provenance_refs"]]
        for block_id in source_block_ids:
            ref = {"chapter_id": chapter_id, "block_id": block_id}
            if ref not in provenance:
                provenance.append(ref)
        history = [dict(row) for row in source["cross_chapter_dispositions"]]
        history.append(
            {
                "chapter_id": chapter_id,
                "verdict": verdict,
                "source_block_ids": source_block_ids,
                "reason": raw.get("reason"),
                "challenge_artifact_hash": challenge_hash,
            }
        )
        lifecycle = source["lifecycle_state"]
        if verdict != "compatible" or lifecycle == "rejected_dormant":
            lifecycle = "pending_evidence"
        authority = (
            CHAPTER_CONFIRMED_SCOPE
            if lifecycle == "chapter_confirmed"
            else CANDIDATE_ONLY_SCOPE
        )
        preferred = source["preferred_rendering_vi"]
        render_policy = source["render_policy"]
        if lifecycle != "chapter_confirmed":
            preferred = None
            render_policy = "none"
        cards[card_id] = _glossary_context_card(
            glossary_card_id=card_id,
            surface=source["surface"],
            category_claim=source["category_claim"],
            local_sense=source["local_sense"],
            preferred_rendering_vi=preferred,
            render_policy=render_policy,
            lifecycle_state=lifecycle,
            authority_scope=authority,
            first_supported_block_id=source["first_supported_block_id"],
            provenance_refs=provenance,
            source_candidate_id=source["source_candidate_id"],
            cross_chapter_dispositions=history,
            hearing_count=int(source["hearing_count"]) + 1,
        )
    if seen != presence_ids:
        raise ChapterPrefixPriorError(
            "glossary dispositions do not exact-cover retrieved glossary cards"
        )
    body = {
        key: _clone(value)
        for key, value in verified.items()
        if key != "prefix_bundle_hash"
    }
    body["glossary_context_cards"] = sorted(
        cards.values(), key=lambda row: row["glossary_card_id"]
    )
    body["glossary_projection_hashes"] = sorted(
        set(body["glossary_projection_hashes"]).union({challenge_hash})
    )
    body["production_publish_performed"] = False
    result = {**body, "prefix_bundle_hash": canonical_hash(body)}
    return verify_chapter_prefix_prior_bundle_v1(result)


def extend_chapter_prefix_prior_bundle_v1(
    *,
    bundle: Mapping[str, Any],
    document: Mapping[str, Any],
    audited_inventory: Mapping[str, Any],
    next_chapter_id: str,
) -> dict[str, Any]:
    """Append one audited chapter without deciding cross-chapter identity."""

    prior = verify_chapter_prefix_prior_bundle_v1(bundle, document=document)
    catalog = _document_catalog(document)
    next_id = _required_string(next_chapter_id, "next_chapter_id")
    order = catalog["chapter_order"]
    try:
        expected = order[order.index(prior["coverage_through_chapter_id"]) + 1]
    except (ValueError, IndexError) as exc:
        raise ChapterPrefixPriorError("prefix has no contiguous next chapter") from exc
    if next_id != expected or next_id in prior["covered_chapter_ids"]:
        raise ChapterPrefixPriorError("prefix extension is not the next chapter")
    addition = build_chapter_prefix_prior_bundle_v1(
        document=document,
        audited_inventory=audited_inventory,
        coverage_through_chapter_id=next_id,
    )
    if addition["state_lineage_id"] != prior["state_lineage_id"]:
        raise ChapterPrefixPriorError("prefix extension lineage mismatch")

    claim_cards = {
        row["prior_card_id"]: _clone(row)
        for row in [*prior["claim_cards"], *addition["claim_cards"]]
    }
    if len(claim_cards) != len(prior["claim_cards"]) + len(addition["claim_cards"]):
        raise ChapterPrefixPriorError("prefix extension repeats a prior-card id")
    active = {
        row["prior_card_id"]: _clone(row)
        for row in [*prior["b0_context_cards"], *addition["b0_context_cards"]]
    }
    candidates = {
        row["prior_card_id"]: _clone(row)
        for row in [
            *prior["candidate_only_context_cards"],
            *addition["candidate_only_context_cards"],
        ]
    }
    if set(active).intersection(candidates):
        raise ChapterPrefixPriorError("prefix extension repeats context authority")
    glossary_cards = {
        row["glossary_card_id"]: _clone(row)
        for row in [
            *prior["glossary_context_cards"],
            *addition["glossary_context_cards"],
        ]
    }
    if len(glossary_cards) != len(prior["glossary_context_cards"]) + len(
        addition["glossary_context_cards"]
    ):
        raise ChapterPrefixPriorError("prefix extension repeats a glossary card id")

    surface_owners: dict[str, set[str]] = {}
    card_views = {**candidates, **active}
    for card_id, row in card_views.items():
        for surface in row["stable_surfaces"]:
            key = _surface_key(surface)
            if key:
                surface_owners.setdefault(key, set()).add(card_id)
    uncertainties = [
        _clone(row) for row in prior["prefix_identity_uncertainties"]
    ]
    for surface_key, owner_ids in sorted(surface_owners.items()):
        if len(owner_ids) < 2:
            continue
        owner_chapters = {
            ref["chapter_id"]
            for card_id in owner_ids
            for ref in card_views[card_id]["provenance_refs"]
        }
        if len(owner_chapters) < 2:
            continue
        uncertainty_body = {
            "surface_key": surface_key,
            "prior_card_ids": sorted(owner_ids),
            "chapter_ids": sorted(owner_chapters),
            "status": "pending_identity_review",
            "authority_effect": "candidate_only",
            "reason_code": "cross_chapter_surface_collision",
        }
        uncertainty = {
            "uncertainty_id": "prefixunc1_"
            + canonical_hash(uncertainty_body)[:20],
            **uncertainty_body,
        }
        if uncertainty["uncertainty_id"] not in {
            row["uncertainty_id"] for row in uncertainties
        }:
            uncertainties.append(uncertainty)
        for card_id in owner_ids:
            if card_id not in active:
                continue
            source = active.pop(card_id)
            body = {
                key: _clone(value)
                for key, value in source.items()
                if key != "context_card_hash"
            }
            body["authority_scope"] = CANDIDATE_ONLY_SCOPE
            disputes = list(body["disputed_claims"])
            if not any(
                row.get("disputed_field") == "identity_membership"
                for row in disputes
                if isinstance(row, Mapping)
            ):
                disputes.append(
                    {
                        "disputed_field": "identity_membership",
                        "historical_value": None,
                        "status": "pending",
                        "pending_reason_codes": ["conflicting_evidence"],
                        "evidence_manifest_hashes": [],
                        "hearing_count": 0,
                        "automatic_hearing_limit": 2,
                        "same_evidence_reopen_forbidden": True,
                        "next_review_trigger": "identity_resolution",
                        "revision_ids": [],
                        "uncertainty_id": uncertainty["uncertainty_id"],
                    }
                )
            body["disputed_claims"] = disputes
            candidates[card_id] = {
                **body,
                "context_card_hash": canonical_hash(body),
            }

    source_rows = [
        *(_clone(prior["source_entity_manifest"])),
        *(_clone(addition["source_entity_manifest"])),
    ]
    source_glossary_rows = [
        *(_clone(prior["source_glossary_manifest"])),
        *(_clone(addition["source_glossary_manifest"])),
    ]
    provenance_rows = [
        *(_clone(prior["audited_inventory_provenance"])),
        *(_clone(addition["audited_inventory_provenance"])),
    ]
    body = {
        "schema_version": PREFIX_PRIOR_SCHEMA_VERSION,
        "validator_version": PREFIX_PRIOR_VALIDATOR_VERSION,
        "state_lineage_id": prior["state_lineage_id"],
        "book_source_manifest_hash": prior["book_source_manifest_hash"],
        "coverage_through_chapter_id": next_id,
        "covered_chapter_ids": [*prior["covered_chapter_ids"], next_id],
        "audited_inventory_provenance": provenance_rows,
        "claim_cards": sorted(claim_cards.values(), key=lambda row: row["prior_card_id"]),
        "b0_context_cards": sorted(active.values(), key=lambda row: row["prior_card_id"]),
        "candidate_only_context_cards": sorted(
            candidates.values(), key=lambda row: row["prior_card_id"]
        ),
        "source_entity_manifest": sorted(
            source_rows, key=lambda row: row["prior_card_id"]
        ),
        "glossary_context_cards": sorted(
            glossary_cards.values(), key=lambda row: row["glossary_card_id"]
        ),
        "source_glossary_manifest": sorted(
            source_glossary_rows, key=lambda row: row["glossary_card_id"]
        ),
        "claim_projection_hashes": sorted(
            set(prior["claim_projection_hashes"]).union(
                addition["claim_projection_hashes"]
            )
        ),
        "glossary_projection_hashes": sorted(
            set(prior["glossary_projection_hashes"]).union(
                addition["glossary_projection_hashes"]
            )
        ),
        "prefix_identity_uncertainties": sorted(
            uncertainties, key=lambda row: row["uncertainty_id"]
        ),
        "production_publish_performed": False,
    }
    result = {**body, "prefix_bundle_hash": canonical_hash(body)}
    return verify_chapter_prefix_prior_bundle_v1(result, document=document)


def b0_inputs_from_prefix_bundle_v1(
    bundle: Mapping[str, Any],
) -> dict[str, Any]:
    """Derive bounded B0 inputs without treating pending claims as authority."""

    verified = verify_chapter_prefix_prior_bundle_v1(bundle)
    prior_cards: list[dict[str, Any]] = []
    candidate_cards = [
        _clone(row) for row in verified["candidate_only_context_cards"]
    ]
    for row in verified["b0_context_cards"]:
        effective = row["effective_claims"]
        has_pending = bool(row["disputed_claims"])
        has_required_null = (
            effective.get("referent_kind") is None
            or effective.get("identity_summary") is None
        )
        if has_pending or has_required_null:
            body = {
                key: _clone(value)
                for key, value in row.items()
                if key != "context_card_hash"
            }
            body["authority_scope"] = CANDIDATE_ONLY_SCOPE
            candidate_cards.append(
                {**body, "context_card_hash": canonical_hash(body)}
            )
            continue
        prior_cards.append(
            {
                "prior_card_id": row["prior_card_id"],
                "canonical_surface": row["canonical_surface"],
                "stable_surfaces": _clone(row["stable_surfaces"]),
                "referent_kind": effective["referent_kind"],
                "referential_gender": effective["referential_gender"],
                "identity_summary": effective["identity_summary"],
                "authority_scope": CHAPTER_CONFIRMED_SCOPE,
                "first_supported_block_id": row["first_supported_block_id"],
                "provenance_refs": _clone(row["provenance_refs"]),
            }
        )
    body = {
        "schema_version": "b0_prefix_inputs_v2",
        "prefix_bundle_hash": verified["prefix_bundle_hash"],
        "state_lineage_id": verified["state_lineage_id"],
        "prior_cards": sorted(prior_cards, key=lambda row: row["prior_card_id"]),
        "candidate_only_context_cards": sorted(
            candidate_cards, key=lambda row: row["prior_card_id"]
        ),
        "glossary_context_cards": _clone(verified["glossary_context_cards"]),
    }
    return {**body, "b0_inputs_hash": canonical_hash(body)}


__all__ = [
    "CANDIDATE_ONLY_SCOPE",
    "CHAPTER_CONFIRMED_SCOPE",
    "DORMANT_SCOPE",
    "CLAIM_FIELDS",
    "PREFIX_PRIOR_SCHEMA_VERSION",
    "PREFIX_PRIOR_VALIDATOR_VERSION",
    "ChapterPrefixPriorError",
    "apply_claim_projection_to_prefix_bundle_v1",
    "apply_glossary_dispositions_to_prefix_bundle_v1",
    "b0_inputs_from_prefix_bundle_v1",
    "build_chapter_prefix_prior_bundle_v1",
    "extend_chapter_prefix_prior_bundle_v1",
    "verify_chapter_prefix_prior_bundle_v1",
]
