from __future__ import annotations

from collections import defaultdict
from copy import deepcopy
import hashlib
from pathlib import Path
import re
from typing import Any, Iterable, Mapping, Sequence

from pipeline.literary.b0_entity_inventory_experiment import (
    GLOSSARY_CATEGORIES,
    _block_text,
    _chapter_blocks,
    _normalized_surface,
)
from pipeline.literary.builder_pilot import load_system_prompt_from_design
from pipeline.literary.checkpoint import canonical_hash, canonical_json
from pipeline.literary.chapter_registry_schema_v4 import RenderedRegistryRequestV4
from pipeline.literary.chapter_registry_v4 import route_alias_for_commit


PROMPT_ID = "literary_entity_conflict_auditor_v2"
CONFLICT_SCHEMA_VERSION = "b0_entity_conflict_auditor_v3"
CANDIDATE_ACTIONS = frozenset({"keep", "merge_into", "keep_pending", "reject"})
SURFACE_ACTIONS = frozenset({"bind_global", "quarantine"})
GLOSSARY_ACTIONS = frozenset(
    {"confirm_chapter", "keep_pending", "reject_dormant"}
)
GLOSSARY_RENDER_POLICIES = frozenset({"advisory_meaning", "none"})
MAX_COMPONENT_SOURCE_BLOCKS = 12
MAX_GLOSSARY_SOURCE_BLOCKS = 24
HONORIFIC_PREFIXES = frozenset(
    {"mr", "mrs", "miss", "ms", "dr", "sir", "lady", "lord", "capt", "captain"}
)


def _required_string(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be a non-empty string")
    return value


def _exact_keys(value: Mapping[str, Any], expected: set[str], label: str) -> None:
    actual = set(value)
    if actual != expected:
        raise ValueError(
            f"{label} keys differ; missing={sorted(expected-actual)}, "
            f"foreign={sorted(actual-expected)}"
        )


def _enum(value: Any, allowed: Iterable[str], label: str) -> str:
    result = _required_string(value, label)
    if result not in allowed:
        raise ValueError(f"{label} is outside the closed enum")
    return result


def _nullable_string(value: Any, label: str) -> str | None:
    return None if value is None else _required_string(value, label)


def _nullable_enum(value: Any, allowed: Iterable[str], label: str) -> str | None:
    if value is None:
        return None
    if isinstance(value, str) and value.strip().casefold() == "null":
        return None
    return _enum(value, allowed, label)


def _string_list(value: Any, label: str, *, allow_empty: bool = False) -> list[str]:
    if not isinstance(value, list) or (not value and not allow_empty):
        raise ValueError(f"{label} must be a {'possibly empty' if allow_empty else 'non-empty'} list")
    rows = [_required_string(item, label) for item in value]
    if len(rows) != len(set(rows)):
        raise ValueError(f"{label} contains duplicates")
    return rows


def _block_view(block: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "block_id": str(block.get("block_id") or ""),
        "order_index": int(block.get("order_index") or 0),
        "text": _block_text(block),
    }


def _surface_claims(row: Mapping[str, Any]) -> list[dict[str, Any]]:
    canonical_location = next(
        (
            item
            for item in row.get("name_locations") or []
            if isinstance(item, Mapping)
            and item.get("surface") == row.get("canonical_surface")
        ),
        {},
    )
    claims = [
        {
            "surface": str(row.get("canonical_surface") or ""),
            "name_class": row.get("canonical_name_class"),
            "source_block_ids": list(
                canonical_location.get("source_block_ids")
                or row.get("source_block_ids")
                or []
            ),
            "surface_match_block_ids": list(
                canonical_location.get("surface_match_block_ids") or []
            ),
            "address_validation_state": canonical_location.get(
                "address_validation_state",
                "valid" if row.get("surface_status") == "located" else "unknown",
            ),
            "address_issues": list(canonical_location.get("address_issues") or []),
            "ownership_state": canonical_location.get(
                "ownership_state", "legacy_unclassified"
            ),
            "is_canonical": True,
        }
    ]
    claims.extend(
        {
            "surface": str(item.get("surface") or ""),
            "name_class": item.get("name_class"),
            "source_block_ids": list(item.get("source_block_ids") or []),
            "surface_match_block_ids": list(
                item.get("surface_match_block_ids") or []
            ),
            "address_validation_state": item.get(
                "address_validation_state", "legacy_unclassified"
            ),
            "address_issues": list(item.get("address_issues") or []),
            "ownership_state": item.get("ownership_state", "legacy_unclassified"),
            "is_canonical": False,
        }
        for item in row.get("alternative_names") or []
        if isinstance(item, Mapping)
    )
    return [claim for claim in claims if claim["surface"]]


def _title_core(surface: str, name_class: Any) -> str | None:
    normalized = _normalized_surface(surface)
    tokens = re.findall(r"[\w'-]+", normalized, flags=re.UNICODE)
    if len(tokens) < 2:
        return None
    first = tokens[0].rstrip(".")
    if name_class != "title_plus_name" and first not in HONORIFIC_PREFIXES:
        return None
    core = " ".join(tokens[1:]).strip()
    return core or None


def _bounded_component_block_ids(
    *,
    candidate_ids: Sequence[str],
    contested_surfaces: Sequence[Mapping[str, Any]],
    entities: Mapping[str, Mapping[str, Any]],
    blocks: Sequence[Mapping[str, Any]],
) -> list[str]:
    block_order = {
        str(row.get("block_id") or ""): offset for offset, row in enumerate(blocks)
    }
    direct: list[str] = []

    def add(block_id: Any) -> None:
        value = str(block_id or "")
        if value in block_order and value not in direct:
            direct.append(value)

    # Preserve both ends of each candidate's cited surface evidence. A reused
    # surface may acquire meaning later in the chapter even when it is not a
    # contested surface shared by multiple candidates.
    for candidate_id in candidate_ids:
        claims = _surface_claims(entities[candidate_id])
        source_ids = sorted(
            {
                str(block_id)
                for claim in claims
                for block_id in claim["source_block_ids"]
                if str(block_id) in block_order
            },
            key=block_order.__getitem__,
        )
        if source_ids:
            add(source_ids[0])
            add(source_ids[-1])

    # A reused surface may change referent across the chapter. Preserve both ends
    # without copying every occurrence into the Auditor request.
    for surface in contested_surfaces:
        source_ids = [
            str(value)
            for value in surface.get("source_block_ids") or []
            if str(value) in block_order
        ]
        if source_ids:
            source_ids.sort(key=block_order.__getitem__)
            add(source_ids[0])
            add(source_ids[-1])

    # Kind and gender are secondary identity evidence. Add at most one witness
    # per claim and only after candidate/surface provenance is represented.
    for candidate_id in candidate_ids:
        candidate = entities[candidate_id]
        for claim_name in ("referent_kind_claim", "referential_gender_claim"):
            claim = candidate.get(claim_name) or {}
            if isinstance(claim, Mapping):
                support = claim.get("support_block_ids") or []
                if support:
                    add(support[0])

    if len(direct) > MAX_COMPONENT_SOURCE_BLOCKS:
        raise ValueError(
            "identity conflict requires more direct evidence than one bounded component; "
            "split the component before rendering"
        )

    selected = list(direct)
    for source_id in list(direct):
        center = block_order[source_id]
        for offset in (center - 1, center + 1):
            if not 0 <= offset < len(blocks):
                continue
            neighbor = str(blocks[offset].get("block_id") or "")
            if neighbor not in selected:
                selected.append(neighbor)
            if len(selected) == MAX_COMPONENT_SOURCE_BLOCKS:
                break
        if len(selected) == MAX_COMPONENT_SOURCE_BLOCKS:
            break

    return sorted(selected, key=block_order.__getitem__)


class _UnionFind:
    def __init__(self, values: Iterable[str]) -> None:
        self.parent = {value: value for value in values}

    def find(self, value: str) -> str:
        parent = self.parent[value]
        if parent != value:
            self.parent[value] = self.find(parent)
        return self.parent[value]

    def union(self, left: str, right: str) -> None:
        left_root = self.find(left)
        right_root = self.find(right)
        if left_root != right_root:
            self.parent[max(left_root, right_root)] = min(left_root, right_root)


def _candidate_card(row: Mapping[str, Any]) -> dict[str, Any]:
    supplied_names = []
    for claim in _surface_claims(row):
        supplied_names.append(
            {
                "surface": claim["surface"],
                "name_class": claim["name_class"],
                "source_block_ids": list(claim["source_block_ids"]),
                "surface_match_block_ids": list(claim["surface_match_block_ids"]),
                "address_validation_state": claim["address_validation_state"],
                "address_issues": list(claim["address_issues"]),
                "ownership_state": claim["ownership_state"],
                "is_canonical": bool(claim["is_canonical"]),
            }
        )
    kind = row.get("referent_kind_claim") or {}
    gender = row.get("referential_gender_claim") or {}
    return {
        "candidate_id": str(row.get("candidate_id") or ""),
        "supplied_names": supplied_names,
        "referent_kind_claim": kind.get("value") if isinstance(kind, Mapping) else None,
        "referential_gender_claim": gender.get("value") if isinstance(gender, Mapping) else None,
        "identity_summary_draft": row.get("identity_summary_draft"),
        "audit_reasons": list(row.get("audit_reasons") or []),
    }


def _glossary_review_manifest(
    *,
    inventory: Mapping[str, Any],
    blocks: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    block_by_id = {str(row.get("block_id") or ""): row for row in blocks}
    block_order = {block_id: offset for offset, block_id in enumerate(block_by_id)}
    candidates: list[dict[str, Any]] = []
    deferred_ids: list[str] = []
    direct_ids: list[str] = []
    seen: set[str] = set()

    for raw in inventory.get("glossary_candidates") or []:
        if not isinstance(raw, Mapping):
            raise ValueError("glossary candidate must be an object")
        candidate_id = _required_string(raw.get("candidate_id"), "glossary candidate_id")
        if candidate_id in seen:
            raise ValueError("glossary candidates repeat an id")
        seen.add(candidate_id)
        cited = [
            str(block_id)
            for block_id in raw.get("source_block_ids") or []
            if str(block_id) in block_by_id
        ]
        matched = [
            str(block_id)
            for block_id in raw.get("surface_match_block_ids") or []
            if str(block_id) in block_by_id
        ]
        evidence_ids: list[str] = []
        for block_id in [*cited, *matched]:
            if block_id not in evidence_ids:
                evidence_ids.append(block_id)
        if not evidence_ids:
            deferred_ids.append(candidate_id)
            continue
        for block_id in evidence_ids:
            if block_id not in direct_ids:
                direct_ids.append(block_id)
        candidates.append(
            {
                "candidate_id": candidate_id,
                "surface": _required_string(raw.get("surface"), "glossary surface"),
                "category_claim": _enum(
                    raw.get("category_claim"), GLOSSARY_CATEGORIES, "glossary category"
                ),
                "local_sense_draft": _required_string(
                    raw.get("short_description"), "glossary short_description"
                ),
                "support_block_ids": cited,
                "surface_match_block_ids": matched,
                "address_validation_state": raw.get("address_validation_state"),
            }
        )

    if len(direct_ids) > MAX_GLOSSARY_SOURCE_BLOCKS:
        raise ValueError("glossary audit exceeds its direct-evidence block cap")
    selected = list(direct_ids)
    for source_id in list(direct_ids):
        center = block_order[source_id]
        for offset in (center - 1, center + 1):
            if not 0 <= offset < len(blocks):
                continue
            neighbor = str(blocks[offset].get("block_id") or "")
            if neighbor not in selected:
                selected.append(neighbor)
            if len(selected) == MAX_GLOSSARY_SOURCE_BLOCKS:
                break
        if len(selected) == MAX_GLOSSARY_SOURCE_BLOCKS:
            break
    selected.sort(key=block_order.__getitem__)
    candidates.sort(key=lambda row: row["candidate_id"])
    return {
        "candidate_cards": candidates,
        "allowed_source_block_ids": selected,
        "source_blocks": [_block_view(block_by_id[block_id]) for block_id in selected],
        "deferred_source_repair_candidate_ids": sorted(deferred_ids),
    }


def build_identity_conflict_manifest(
    inventory: Mapping[str, Any], chapter: Mapping[str, Any]
) -> dict[str, Any]:
    chapter_id = _required_string(chapter.get("chapter_id"), "chapter_id")
    if inventory.get("chapter_id") != chapter_id:
        raise ValueError("inventory and chapter differ")
    blocks = _chapter_blocks(chapter)
    block_by_id = {str(row.get("block_id") or ""): row for row in blocks}
    source_catalog = {
        block_id: _block_text(row) for block_id, row in block_by_id.items()
    }
    entities = {
        str(row.get("candidate_id") or ""): dict(row)
        for row in inventory.get("entity_candidates") or []
        if isinstance(row, Mapping)
    }
    if "" in entities:
        raise ValueError("entity candidate is missing candidate_id")
    deferred_source_repair_ids = {
        candidate_id
        for candidate_id, row in entities.items()
        if row.get("publication_state") == "pending_source_repair"
        or not row.get("source_block_ids")
    }
    active_entities = {
        candidate_id: row
        for candidate_id, row in entities.items()
        if candidate_id not in deferred_source_repair_ids
    }

    claims_by_key: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for candidate_id, row in active_entities.items():
        for claim in _surface_claims(row):
            key = _normalized_surface(claim["surface"])
            if key:
                claims_by_key[key].append({"candidate_id": candidate_id, **claim})

    union = _UnionFind(active_entities)
    issues_by_candidate: dict[str, set[str]] = defaultdict(set)
    contested: dict[str, dict[str, Any]] = {}

    for key, claims in sorted(claims_by_key.items()):
        candidate_ids = sorted({str(claim["candidate_id"]) for claim in claims})
        if len(candidate_ids) > 1:
            for candidate_id in candidate_ids[1:]:
                union.union(candidate_ids[0], candidate_id)
            for candidate_id in candidate_ids:
                issues_by_candidate[candidate_id].add("shared_surface")
            contested[key] = {
                "surface_key": key,
                "observed_surfaces": sorted({str(claim["surface"]) for claim in claims}),
                "claimant_candidate_ids": candidate_ids,
                "issue_codes": ["shared_surface"],
                "source_block_ids": sorted(
                    {block_id for claim in claims for block_id in claim["source_block_ids"]}
                ),
            }

    for candidate_id, row in active_entities.items():
        for claim in _surface_claims(row):
            core = _title_core(str(claim["surface"]), claim.get("name_class"))
            if core is None or core not in claims_by_key:
                continue
            other_ids = sorted(
                {
                    str(other["candidate_id"])
                    for other in claims_by_key[core]
                    if str(other["candidate_id"]) != candidate_id
                }
            )
            if not other_ids:
                continue
            evidence_ids = set(claim["source_block_ids"])
            # Nearby blocks are evidence for a direct collision, not evidence that
            # every candidate mentioned nearby belongs to the same identity case.
            # Connecting by co-location creates transitive chapter-wide components.
            connected_ids = other_ids
            for other_id in connected_ids:
                union.union(candidate_id, other_id)
            for linked_id in [candidate_id, *connected_ids]:
                issues_by_candidate[linked_id].add("title_or_surname_scope")
            key = _normalized_surface(claim["surface"])
            previous = contested.get(key)
            claimant_ids = sorted({candidate_id, *other_ids})
            issue_codes = {"title_or_surname_scope"}
            source_ids = sorted(evidence_ids)
            observed = [str(claim["surface"])]
            if previous:
                claimant_ids = sorted(set(claimant_ids + previous["claimant_candidate_ids"]))
                issue_codes.update(previous["issue_codes"])
                source_ids = sorted(set(source_ids + previous["source_block_ids"]))
                observed = sorted(set(observed + previous["observed_surfaces"]))
            contested[key] = {
                "surface_key": key,
                "observed_surfaces": observed,
                "claimant_candidate_ids": claimant_ids,
                "issue_codes": sorted(issue_codes),
                "source_block_ids": source_ids,
            }

    for candidate_id, row in active_entities.items():
        if row.get("audit_reasons"):
            issues_by_candidate[candidate_id].add("source_audit_flag")
        canonical_class = row.get("canonical_name_class")
        canonical_surface = str(row.get("canonical_surface") or "")
        if canonical_class is not None and canonical_surface:
            canonical_claim = _surface_claims(row)[0]
            gate = route_alias_for_commit(
                surface=canonical_surface,
                name_class=str(canonical_class),
                target_entity_id=candidate_id,
                source_block_ids=list(canonical_claim["source_block_ids"]),
                source_catalog=source_catalog,
                source_decision_lineage={
                    "source_inventory_hash": inventory.get("inventory_hash"),
                    "candidate_id": candidate_id,
                },
            )
            if gate["outcome"] != "eligible_global_alias":
                issues_by_candidate[candidate_id].add("canonical_scope_review")

    grouped: dict[str, set[str]] = defaultdict(set)
    for candidate_id in active_entities:
        if issues_by_candidate.get(candidate_id):
            grouped[union.find(candidate_id)].add(candidate_id)
    for candidate_id in active_entities:
        root = union.find(candidate_id)
        if root in grouped:
            grouped[root].add(candidate_id)

    components: list[dict[str, Any]] = []
    conflict_ids: set[str] = set()
    for candidate_ids in sorted((sorted(values) for values in grouped.values()), key=lambda row: row):
        conflict_ids.update(candidate_ids)
        component_contested = [
            row
            for row in contested.values()
            if set(row["claimant_candidate_ids"]).intersection(candidate_ids)
        ]
        ordered_allowed = _bounded_component_block_ids(
            candidate_ids=candidate_ids,
            contested_surfaces=component_contested,
            entities=active_entities,
            blocks=blocks,
        )
        payload = {
            "candidate_ids": candidate_ids,
            "issue_codes": sorted(
                {issue for candidate_id in candidate_ids for issue in issues_by_candidate[candidate_id]}
            ),
            "candidate_cards": [
                _candidate_card(active_entities[candidate_id])
                for candidate_id in candidate_ids
            ],
            "contested_surfaces": sorted(component_contested, key=lambda row: row["surface_key"]),
            "allowed_source_block_ids": ordered_allowed,
            "source_blocks": [_block_view(block_by_id[block_id]) for block_id in ordered_allowed],
        }
        components.append(
            {"component_id": "idcmp_" + canonical_hash(payload)[:20], **payload}
        )

    glossary_review = _glossary_review_manifest(inventory=inventory, blocks=blocks)
    report = {
        "schema_version": CONFLICT_SCHEMA_VERSION,
        "chapter_id": chapter_id,
        "source_inventory_hash": inventory.get("inventory_hash"),
        "components": components,
        "glossary_review": glossary_review,
        "clean_candidate_ids": sorted(set(active_entities) - conflict_ids),
        "conflict_candidate_ids": sorted(conflict_ids),
        "deferred_source_repair_candidate_ids": sorted(deferred_source_repair_ids),
    }
    return {**report, "manifest_hash": canonical_hash(report)}


def _nullable(enum: Iterable[str] | None = None) -> dict[str, Any]:
    string: dict[str, Any] = {"type": "string", "minLength": 1}
    if enum is not None:
        string["enum"] = sorted(enum)
    return {"anyOf": [string, {"type": "null"}]}


def entity_conflict_response_schema() -> dict[str, Any]:
    string = {"type": "string", "minLength": 1}
    block_ids = {
        "type": "array",
        "items": string,
        "minItems": 1,
        "maxItems": 8,
        "uniqueItems": True,
    }
    candidate_action = {
        "type": "object",
        "additionalProperties": False,
        "required": [
            "candidate_id",
            "action",
            "target_candidate_id",
            "selected_canonical_surface",
            "source_block_ids",
            "resolution_note",
        ],
        "properties": {
            "candidate_id": string,
            "action": {"type": "string", "enum": sorted(CANDIDATE_ACTIONS)},
            "target_candidate_id": _nullable(),
            "selected_canonical_surface": _nullable(),
            "source_block_ids": block_ids,
            "resolution_note": string,
        },
    }
    surface_action = {
        "type": "object",
        "additionalProperties": False,
        "required": [
            "surface_key",
            "action",
            "target_candidate_id",
            "source_block_ids",
            "resolution_note",
        ],
        "properties": {
            "surface_key": string,
            "action": {"type": "string", "enum": sorted(SURFACE_ACTIONS)},
            "target_candidate_id": _nullable(),
            "source_block_ids": block_ids,
            "resolution_note": string,
        },
    }
    component = {
        "type": "object",
        "additionalProperties": False,
        "required": ["component_id", "candidate_actions", "surface_actions"],
        "properties": {
            "component_id": string,
            "candidate_actions": {"type": "array", "items": candidate_action},
            "surface_actions": {"type": "array", "items": surface_action},
        },
    }
    glossary_disposition = {
        "type": "object",
        "additionalProperties": False,
        "required": [
            "candidate_id",
            "action",
            "category_update",
            "local_sense_update",
            "preferred_rendering_vi",
            "render_policy",
            "source_block_ids",
            "resolution_note",
        ],
        "properties": {
            "candidate_id": string,
            "action": {"type": "string", "enum": sorted(GLOSSARY_ACTIONS)},
            "category_update": _nullable(GLOSSARY_CATEGORIES),
            "local_sense_update": _nullable(),
            "preferred_rendering_vi": _nullable(),
            "render_policy": {
                "type": "string",
                "enum": sorted(GLOSSARY_RENDER_POLICIES),
            },
            "source_block_ids": block_ids,
            "resolution_note": string,
        },
    }
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["chapter_id", "component_decisions", "glossary_dispositions"],
        "properties": {
            "chapter_id": string,
            "component_decisions": {"type": "array", "items": component},
            "glossary_dispositions": {
                "type": "array",
                "items": glossary_disposition,
            },
        },
    }


def _model_sections(manifest: Mapping[str, Any]) -> dict[str, Any]:
    source_by_id: dict[str, dict[str, Any]] = {}

    def register_sources(rows: Sequence[Mapping[str, Any]]) -> None:
        for raw in rows:
            block = dict(raw)
            block_id = _required_string(block.get("block_id"), "source block_id")
            previous = source_by_id.get(block_id)
            if previous is not None and previous != block:
                raise ValueError("conflict manifest repeats a source block inconsistently")
            source_by_id[block_id] = block

    components: list[dict[str, Any]] = []
    for raw in manifest["components"]:
        component = deepcopy(dict(raw))
        register_sources(component.pop("source_blocks"))
        components.append(component)

    glossary_review = deepcopy(dict(manifest["glossary_review"]))
    register_sources(glossary_review.pop("source_blocks"))
    ordered_sources = sorted(
        source_by_id.values(),
        key=lambda row: (
            int(row.get("order_index") or 0),
            str(row.get("block_id") or ""),
        ),
    )
    return {
        "source_blocks": ordered_sources,
        "identity_conflict_components": components,
        "glossary_review": glossary_review,
    }


def render_entity_conflict_request(
    *,
    chapter: Mapping[str, Any],
    inventory: Mapping[str, Any],
    design_doc: Path,
    model_id: str = "gpt-5.4",
    reasoning_effort: str = "none",
    temperature: float = 1.0,
    seed: int = 20260715,
    max_output_tokens: int = 4096,
) -> RenderedRegistryRequestV4:
    manifest = build_identity_conflict_manifest(inventory, chapter)
    prompt = load_system_prompt_from_design(Path(design_doc), PROMPT_ID)
    prompt_sha = hashlib.sha256(prompt.encode("utf-8")).hexdigest()
    schema = entity_conflict_response_schema()
    sections = _model_sections(manifest)
    payload = {
        "schema_version": CONFLICT_SCHEMA_VERSION,
        "role": "b0_entity_conflict_auditor",
        "chapter_id": manifest["chapter_id"],
        "source_inventory_hash": manifest["source_inventory_hash"],
        "allowlisted_sections": sections,
    }
    messages = (
        {"role": "system", "content": prompt},
        {"role": "user", "content": canonical_json(payload)},
    )
    model_contract = {
        "model_id": model_id,
        "reasoning_effort": reasoning_effort,
        "temperature": temperature,
        "seed": seed,
        "max_output_tokens": max_output_tokens,
    }
    fingerprint = canonical_hash(
        {
            "schema_version": CONFLICT_SCHEMA_VERSION,
            "manifest_hash": manifest["manifest_hash"],
            "prompt_id": PROMPT_ID,
            "prompt_sha256": prompt_sha,
            "response_schema_hash": canonical_hash(schema),
            "model_contract": model_contract,
            "model_sections_hash": canonical_hash(sections),
        }
    )
    return RenderedRegistryRequestV4(
        role="auditor",
        prompt_id=PROMPT_ID,
        prompt_sha256=prompt_sha,
        response_schema_hash=canonical_hash(schema),
        chapter_id=str(manifest["chapter_id"]),
        window_id=None,
        parent_working_revision_hash=None,
        sections=sections,
        messages=messages,
        request_fingerprint=fingerprint,
    )


def _validate_source_ids(value: Any, allowed: set[str], label: str) -> list[str]:
    rows = _string_list(value, label)
    if not set(rows) <= allowed:
        raise ValueError(f"{label} cites a block outside its component")
    return rows


def _well_formed_source_ids(value: Any) -> list[str] | None:
    if not isinstance(value, list) or not value:
        return None
    if not all(isinstance(row, str) and row.strip() for row in value):
        return None
    rows = [str(row) for row in value]
    return rows if len(rows) == len(set(rows)) else None


def _boundary_fallback_sources(
    *, allowed_order: Sequence[str], direct_sources: Iterable[Any]
) -> list[str]:
    direct = {str(row) for row in direct_sources}
    selected = [block_id for block_id in allowed_order if block_id in direct]
    return selected or list(allowed_order[:1])


def normalize_source_boundary_violations(
    response: Mapping[str, Any],
    *,
    chapter: Mapping[str, Any],
    inventory: Mapping[str, Any],
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Fail closed per row when an Auditor exceeds supplied authority.

    The model's semantic decision is not repaired or reinterpreted. Any affected
    action loses authority and is reduced to the corresponding pending/quarantine
    action using only deterministic source provenance already owned by that row.
    This includes an authoritative ``keep`` that selects a canonical surface not
    supplied by its candidate card. Malformed response shapes remain untouched for
    the strict validator to reject.
    """

    normalized = deepcopy(dict(response))
    manifest = build_identity_conflict_manifest(inventory, chapter)
    components = {row["component_id"]: row for row in manifest["components"]}
    records: list[dict[str, Any]] = []

    raw_components = normalized.get("component_decisions")
    if isinstance(raw_components, list):
        for raw_component in raw_components:
            if not isinstance(raw_component, dict):
                continue
            component_id = raw_component.get("component_id")
            component = components.get(component_id)
            if component is None:
                continue
            allowed_order = list(component["allowed_source_block_ids"])
            allowed = set(allowed_order)
            cards = {
                row["candidate_id"]: row for row in component["candidate_cards"]
            }
            contested = {
                row["surface_key"]: row for row in component["contested_surfaces"]
            }

            candidate_actions = raw_component.get("candidate_actions")
            downscoped_candidate_ids: set[str] = set()
            if isinstance(candidate_actions, list):
                for action in candidate_actions:
                    if not isinstance(action, dict):
                        continue
                    card = cards.get(action.get("candidate_id"))
                    if card is None:
                        continue
                    direct_sources = [
                        block_id
                        for name in card["supplied_names"]
                        for field in ("source_block_ids", "surface_match_block_ids")
                        for block_id in name.get(field) or []
                    ]
                    sources = _well_formed_source_ids(action.get("source_block_ids"))
                    supplied_names = {
                        str(name["surface"]) for name in card["supplied_names"]
                    }
                    selected = action.get("selected_canonical_surface")
                    if (
                        action.get("action") == "keep"
                        and selected not in supplied_names
                    ):
                        retained = (
                            [
                                block_id
                                for block_id in allowed_order
                                if sources is not None and block_id in set(sources)
                            ]
                            or _boundary_fallback_sources(
                                allowed_order=allowed_order,
                                direct_sources=direct_sources,
                            )
                        )
                        action.update(
                            {
                                "action": "keep_pending",
                                "target_candidate_id": None,
                                "selected_canonical_surface": None,
                                "source_block_ids": retained,
                                "resolution_note": (
                                    "Downscoped by code: the response selected a "
                                    "canonical surface outside the supplied candidate "
                                    "card, so no canonical authority is granted."
                                ),
                            }
                        )
                        records.append(
                            {
                                "normalization_kind": (
                                    "candidate_canonical_surface_authority"
                                ),
                                "component_id": component_id,
                                "subject_id": card["candidate_id"],
                                "original_action": "keep",
                                "normalized_action": "keep_pending",
                                "out_of_scope_source_block_ids": sorted(
                                    set(sources or []) - allowed
                                ),
                                "retained_source_block_ids": retained,
                            }
                        )
                        downscoped_candidate_ids.add(card["candidate_id"])
                        continue
                    if sources is None or set(sources) <= allowed:
                        continue
                    fallback = _boundary_fallback_sources(
                        allowed_order=allowed_order, direct_sources=direct_sources
                    )
                    original_action = action.get("action")
                    foreign = sorted(set(sources) - allowed)
                    action.update(
                        {
                            "action": "keep_pending",
                            "target_candidate_id": None,
                            "selected_canonical_surface": None,
                            "source_block_ids": fallback,
                            "resolution_note": (
                                "Downscoped by code: the response cited evidence outside "
                                "this component, so no identity authority is granted."
                            ),
                        }
                    )
                    records.append(
                        {
                            "normalization_kind": "candidate_source_boundary",
                            "component_id": component_id,
                            "subject_id": card["candidate_id"],
                            "original_action": original_action,
                            "normalized_action": "keep_pending",
                            "out_of_scope_source_block_ids": foreign,
                            "retained_source_block_ids": fallback,
                        }
                    )

            surface_actions = raw_component.get("surface_actions")
            if isinstance(surface_actions, list):
                for action in surface_actions:
                    if not isinstance(action, dict):
                        continue
                    sources = _well_formed_source_ids(action.get("source_block_ids"))
                    surface = contested.get(action.get("surface_key"))
                    if surface is None:
                        continue
                    if (
                        action.get("action") == "bind_global"
                        and action.get("target_candidate_id")
                        in downscoped_candidate_ids
                    ):
                        retained = (
                            [
                                block_id
                                for block_id in allowed_order
                                if sources is not None and block_id in set(sources)
                            ]
                            or _boundary_fallback_sources(
                                allowed_order=allowed_order,
                                direct_sources=surface.get("source_block_ids") or [],
                            )
                        )
                        action.update(
                            {
                                "action": "quarantine",
                                "target_candidate_id": None,
                                "source_block_ids": retained,
                                "resolution_note": (
                                    "Downscoped by code: the target candidate has no "
                                    "canonical authority, so this surface cannot be "
                                    "bound globally."
                                ),
                            }
                        )
                        records.append(
                            {
                                "normalization_kind": "surface_target_authority",
                                "component_id": component_id,
                                "subject_id": surface["surface_key"],
                                "original_action": "bind_global",
                                "normalized_action": "quarantine",
                                "out_of_scope_source_block_ids": sorted(
                                    set(sources or []) - allowed
                                ),
                                "retained_source_block_ids": retained,
                            }
                        )
                        continue
                    if sources is None or set(sources) <= allowed:
                        continue
                    fallback = _boundary_fallback_sources(
                        allowed_order=allowed_order,
                        direct_sources=surface.get("source_block_ids") or [],
                    )
                    original_action = action.get("action")
                    foreign = sorted(set(sources) - allowed)
                    action.update(
                        {
                            "action": "quarantine",
                            "target_candidate_id": None,
                            "source_block_ids": fallback,
                            "resolution_note": (
                                "Downscoped by code: the response cited evidence outside "
                                "this component, so no surface authority is granted."
                            ),
                        }
                    )
                    records.append(
                        {
                            "normalization_kind": "surface_source_boundary",
                            "component_id": component_id,
                            "subject_id": surface["surface_key"],
                            "original_action": original_action,
                            "normalized_action": "quarantine",
                            "out_of_scope_source_block_ids": foreign,
                            "retained_source_block_ids": fallback,
                        }
                    )

    glossary = manifest["glossary_review"]
    glossary_allowed_order = list(glossary["allowed_source_block_ids"])
    glossary_allowed = set(glossary_allowed_order)
    glossary_cards = {
        row["candidate_id"]: row for row in glossary["candidate_cards"]
    }
    raw_glossary = normalized.get("glossary_dispositions")
    if isinstance(raw_glossary, list):
        for action in raw_glossary:
            if not isinstance(action, dict):
                continue
            sources = _well_formed_source_ids(action.get("source_block_ids"))
            card = glossary_cards.get(action.get("candidate_id"))
            if sources is None or card is None or set(sources) <= glossary_allowed:
                continue
            fallback = _boundary_fallback_sources(
                allowed_order=glossary_allowed_order,
                direct_sources=[
                    *(card.get("support_block_ids") or []),
                    *(card.get("surface_match_block_ids") or []),
                ],
            )
            original_action = action.get("action")
            foreign = sorted(set(sources) - glossary_allowed)
            action.update(
                {
                    "action": "keep_pending",
                    "category_update": None,
                    "local_sense_update": None,
                    "preferred_rendering_vi": None,
                    "render_policy": "none",
                    "source_block_ids": fallback,
                    "resolution_note": (
                        "Downscoped by code: the response cited evidence outside the "
                        "glossary review boundary, so no rendering authority is granted."
                    ),
                }
            )
            records.append(
                {
                    "normalization_kind": "glossary_source_boundary",
                    "component_id": None,
                    "subject_id": card["candidate_id"],
                    "original_action": original_action,
                    "normalized_action": "keep_pending",
                    "out_of_scope_source_block_ids": foreign,
                    "retained_source_block_ids": fallback,
                }
            )

    return normalized, sorted(
        records,
        key=lambda row: (
            row["normalization_kind"],
            str(row["component_id"] or ""),
            row["subject_id"],
        ),
    )


def validate_and_apply_conflict_response(
    response: Mapping[str, Any],
    *,
    chapter: Mapping[str, Any],
    inventory: Mapping[str, Any],
    request_fingerprint: str,
    source_boundary_normalizations: Sequence[Mapping[str, Any]] = (),
) -> dict[str, Any]:
    if not isinstance(response, Mapping):
        raise ValueError("conflict response must be an object")
    _exact_keys(
        response,
        {"chapter_id", "component_decisions", "glossary_dispositions"},
        "conflict response",
    )
    manifest = build_identity_conflict_manifest(inventory, chapter)
    if response.get("chapter_id") != manifest["chapter_id"]:
        raise ValueError("response chapter differs from conflict manifest")
    glossary_review = manifest["glossary_review"]
    glossary_cards = {
        row["candidate_id"]: row for row in glossary_review["candidate_cards"]
    }
    raw_glossary = response.get("glossary_dispositions")
    if not isinstance(raw_glossary, list):
        raise ValueError("glossary_dispositions must be a list")
    glossary_dispositions: list[dict[str, Any]] = []
    seen_glossary_ids: set[str] = set()
    glossary_allowed_blocks = set(glossary_review["allowed_source_block_ids"])
    normalized_non_authoritative_glossary_count = 0
    for raw in raw_glossary:
        if not isinstance(raw, Mapping):
            raise ValueError("glossary disposition must be an object")
        _exact_keys(
            raw,
            {
                "candidate_id",
                "action",
                "category_update",
                "local_sense_update",
                "preferred_rendering_vi",
                "render_policy",
                "source_block_ids",
                "resolution_note",
            },
            "glossary disposition",
        )
        candidate_id = _required_string(raw.get("candidate_id"), "glossary candidate_id")
        card = glossary_cards.get(candidate_id)
        if card is None or candidate_id in seen_glossary_ids:
            raise ValueError("glossary disposition targets a foreign/duplicate candidate")
        seen_glossary_ids.add(candidate_id)
        action = _enum(raw.get("action"), GLOSSARY_ACTIONS, "glossary action")
        source_block_ids = _validate_source_ids(
            raw.get("source_block_ids"),
            glossary_allowed_blocks,
            "glossary source_block_ids",
        )
        direct_evidence = set(card["support_block_ids"]) | set(
            card["surface_match_block_ids"]
        )
        if not set(source_block_ids).intersection(direct_evidence):
            raise ValueError("glossary disposition cites no candidate evidence block")
        category_update = _nullable_enum(
            raw.get("category_update"),
            GLOSSARY_CATEGORIES,
            "glossary category_update",
        )
        local_sense_update = _nullable_string(
            raw.get("local_sense_update"), "glossary local_sense_update"
        )
        preferred_rendering = _nullable_string(
            raw.get("preferred_rendering_vi"), "preferred_rendering_vi"
        )
        render_policy = _enum(
            raw.get("render_policy"),
            GLOSSARY_RENDER_POLICIES,
            "glossary render_policy",
        )
        if action == "confirm_chapter":
            if render_policy != "advisory_meaning":
                raise ValueError("confirmed glossary must use advisory_meaning")
        elif any(
            value is not None
            for value in (category_update, local_sense_update, preferred_rendering)
        ) or render_policy != "none":
            category_update = None
            local_sense_update = None
            preferred_rendering = None
            render_policy = "none"
            normalized_non_authoritative_glossary_count += 1
        glossary_dispositions.append(
            {
                "candidate_id": candidate_id,
                "action": action,
                "category_update": category_update,
                "local_sense_update": local_sense_update,
                "preferred_rendering_vi": preferred_rendering,
                "render_policy": render_policy,
                "source_block_ids": source_block_ids,
                "resolution_note": _required_string(
                    raw.get("resolution_note"), "glossary resolution_note"
                ),
            }
        )
    if seen_glossary_ids != set(glossary_cards):
        raise ValueError("glossary dispositions must exact-cover review candidates")
    glossary_dispositions.sort(key=lambda row: row["candidate_id"])

    raw_components = response.get("component_decisions")
    if not isinstance(raw_components, list):
        raise ValueError("component_decisions must be a list")
    components_by_id = {row["component_id"]: row for row in manifest["components"]}
    supplied_component_ids = [str(row.get("component_id") or "") for row in raw_components]
    if len(supplied_component_ids) != len(set(supplied_component_ids)):
        raise ValueError("component decisions contain duplicate ids")
    if set(supplied_component_ids) != set(components_by_id):
        raise ValueError("component decisions must exact-cover the manifest")

    normalized_components: list[dict[str, Any]] = []
    action_by_candidate: dict[str, dict[str, Any]] = {}
    surface_action_by_key: dict[str, dict[str, Any]] = {}
    normalized_non_authoritative_surface_count = 0
    for raw_component in raw_components:
        if not isinstance(raw_component, Mapping):
            raise ValueError("component decision must be an object")
        _exact_keys(
            raw_component,
            {"component_id", "candidate_actions", "surface_actions"},
            "component decision",
        )
        component_id = _required_string(raw_component.get("component_id"), "component_id")
        component = components_by_id[component_id]
        candidate_cards = {
            row["candidate_id"]: row for row in component["candidate_cards"]
        }
        allowed_blocks = set(component["allowed_source_block_ids"])
        candidate_rows = raw_component.get("candidate_actions")
        if not isinstance(candidate_rows, list):
            raise ValueError("candidate_actions must be a list")
        candidate_ids = [str(row.get("candidate_id") or "") for row in candidate_rows]
        if len(candidate_ids) != len(set(candidate_ids)) or set(candidate_ids) != set(candidate_cards):
            raise ValueError("candidate actions must exact-cover the component")
        normalized_candidates: list[dict[str, Any]] = []
        for raw in candidate_rows:
            if not isinstance(raw, Mapping):
                raise ValueError("candidate action must be an object")
            _exact_keys(
                raw,
                {
                    "candidate_id",
                    "action",
                    "target_candidate_id",
                    "selected_canonical_surface",
                    "source_block_ids",
                    "resolution_note",
                },
                "candidate action",
            )
            candidate_id = _required_string(raw.get("candidate_id"), "candidate_id")
            action = _enum(raw.get("action"), CANDIDATE_ACTIONS, "candidate action")
            target = _nullable_string(raw.get("target_candidate_id"), "target_candidate_id")
            selected = _nullable_string(
                raw.get("selected_canonical_surface"), "selected_canonical_surface"
            )
            sources = _validate_source_ids(
                raw.get("source_block_ids"), allowed_blocks, "candidate source_block_ids"
            )
            note = _required_string(raw.get("resolution_note"), "resolution_note")
            supplied_names = {
                str(row["surface"]) for row in candidate_cards[candidate_id]["supplied_names"]
            }
            if action == "keep":
                if target is not None or selected not in supplied_names:
                    raise ValueError("keep must select one supplied canonical surface")
            elif action == "merge_into":
                if target not in candidate_cards or target == candidate_id or selected is not None:
                    raise ValueError("merge_into must target another supplied candidate")
            else:
                if target is not None:
                    raise ValueError(f"{action} cannot target another candidate")
                if selected is not None:
                    if selected not in supplied_names:
                        raise ValueError(
                            f"{action} cites a foreign canonical surface"
                        )
                    selected = None
                    normalized_non_authoritative_surface_count += 1
            normalized = {
                "candidate_id": candidate_id,
                "action": action,
                "target_candidate_id": target,
                "selected_canonical_surface": selected,
                "source_block_ids": sources,
                "resolution_note": note,
            }
            normalized_candidates.append(normalized)
            action_by_candidate[candidate_id] = normalized
        for row in normalized_candidates:
            if row["action"] == "merge_into":
                if action_by_candidate[str(row["target_candidate_id"])]["action"] != "keep":
                    raise ValueError("merge target must be kept in the same component")

        contested_by_key = {
            row["surface_key"]: row for row in component["contested_surfaces"]
        }
        surface_rows = raw_component.get("surface_actions")
        if not isinstance(surface_rows, list):
            raise ValueError("surface_actions must be a list")
        surface_keys = [str(row.get("surface_key") or "") for row in surface_rows]
        if len(surface_keys) != len(set(surface_keys)) or set(surface_keys) != set(contested_by_key):
            raise ValueError("surface actions must exact-cover contested surfaces")
        normalized_surfaces: list[dict[str, Any]] = []
        for raw in surface_rows:
            if not isinstance(raw, Mapping):
                raise ValueError("surface action must be an object")
            _exact_keys(
                raw,
                {
                    "surface_key",
                    "action",
                    "target_candidate_id",
                    "source_block_ids",
                    "resolution_note",
                },
                "surface action",
            )
            surface_key = _required_string(raw.get("surface_key"), "surface_key")
            action = _enum(raw.get("action"), SURFACE_ACTIONS, "surface action")
            target = _nullable_string(raw.get("target_candidate_id"), "target_candidate_id")
            sources = _validate_source_ids(
                raw.get("source_block_ids"), allowed_blocks, "surface source_block_ids"
            )
            note = _required_string(raw.get("resolution_note"), "resolution_note")
            if action == "bind_global":
                if target not in candidate_cards or action_by_candidate[target]["action"] != "keep":
                    raise ValueError("global surface binding must target a kept candidate")
            elif target is not None:
                raise ValueError("quarantined surface cannot target a candidate")
            normalized = {
                "surface_key": surface_key,
                "action": action,
                "target_candidate_id": target,
                "source_block_ids": sources,
                "resolution_note": note,
            }
            normalized_surfaces.append(normalized)
            surface_action_by_key[surface_key] = normalized
        # A candidate still needs a canonical display label even when that same
        # surface is unsafe as a unique chapter-wide lookup key. For example, a
        # surname can name one candidate while also occurring inside another
        # candidate's full name. Quarantine controls retrieval authority; it does
        # not erase the candidate's source-backed canonical label.
        normalized_components.append(
            {
                "component_id": component_id,
                "candidate_actions": sorted(
                    normalized_candidates, key=lambda row: row["candidate_id"]
                ),
                "surface_actions": sorted(
                    normalized_surfaces, key=lambda row: row["surface_key"]
                ),
            }
        )

    source_entities = {
        str(row["candidate_id"]): deepcopy(dict(row))
        for row in inventory.get("entity_candidates") or []
    }
    clean_entities: list[dict[str, Any]] = []
    kept_entities: dict[str, dict[str, Any]] = {}
    pending_entities: list[dict[str, Any]] = []
    deferred_source_repairs: list[dict[str, Any]] = []
    closed_entities: list[dict[str, Any]] = []
    for candidate_id in manifest["clean_candidate_ids"]:
        row = source_entities[candidate_id]
        row["publication_state"] = "clean_provisional"
        row["conflict_status"] = "no_detected_identity_conflict"
        clean_entities.append(row)
    for candidate_id in manifest["deferred_source_repair_candidate_ids"]:
        row = source_entities[candidate_id]
        row["publication_state"] = "pending_source_repair"
        row["conflict_status"] = "deferred_until_valid_source_address"
        deferred_source_repairs.append(row)
    for candidate_id in manifest["conflict_candidate_ids"]:
        decision = action_by_candidate[candidate_id]
        row = source_entities[candidate_id]
        row["conflict_auditor_disposition"] = dict(decision)
        if decision["action"] == "keep":
            selected = str(decision["selected_canonical_surface"])
            names = {claim["surface"]: claim for claim in _surface_claims(row)}
            selected_claim = names[selected]
            row["canonical_surface"] = selected
            row["canonical_name_class"] = selected_claim["name_class"]
            row["publication_state"] = "conflict_auditor_provisional"
            row["conflict_status"] = "resolved_keep"
            kept_entities[candidate_id] = row
        elif decision["action"] == "keep_pending":
            row["publication_state"] = "pending_conflict"
            row["conflict_status"] = "unresolved"
            pending_entities.append(row)
        else:
            row["publication_state"] = "not_published"
            row["conflict_status"] = decision["action"]
            closed_entities.append(row)

    quarantined: list[dict[str, Any]] = []
    bound: list[dict[str, Any]] = []
    for surface_key, decision in sorted(surface_action_by_key.items()):
        if decision["action"] == "quarantine":
            quarantined.append(dict(decision))
        else:
            bound.append(dict(decision))
        for candidate_id, row in kept_entities.items():
            if candidate_id == decision.get("target_candidate_id"):
                continue
            row["alternative_names"] = [
                name
                for name in row.get("alternative_names") or []
                if _normalized_surface(name.get("surface") or "") != surface_key
            ]
            row["name_locations"] = [
                name
                for name in row.get("name_locations") or []
                if _normalized_surface(name.get("surface") or "") != surface_key
                or name.get("surface") == row.get("canonical_surface")
            ]

    for candidate_id, decision in action_by_candidate.items():
        if decision["action"] != "merge_into":
            continue
        target_id = str(decision["target_candidate_id"])
        target = kept_entities[target_id]
        source = source_entities[candidate_id]
        existing = {
            _normalized_surface(target["canonical_surface"]),
            *(
                _normalized_surface(row.get("surface") or "")
                for row in target.get("alternative_names") or []
            ),
        }
        quarantined_keys = {row["surface_key"] for row in quarantined}
        for claim in _surface_claims(source):
            key = _normalized_surface(claim["surface"])
            if not key or key in existing or key in quarantined_keys:
                continue
            target.setdefault("alternative_names", []).append(
                {
                    "surface": claim["surface"],
                    "name_class": claim["name_class"],
                    "source_block_ids": list(claim["source_block_ids"]),
                    "surface_match_block_ids": list(
                        claim["surface_match_block_ids"]
                    ),
                    "address_validation_state": claim[
                        "address_validation_state"
                    ],
                    "address_issues": list(claim["address_issues"]),
                    "ownership_state": claim["ownership_state"],
                }
            )
            existing.add(key)
        target["source_block_ids"] = sorted(
            set(target.get("source_block_ids") or [])
            | set(source.get("source_block_ids") or [])
        )

    confirmed_entities = sorted(
        [*clean_entities, *kept_entities.values()], key=lambda row: row["candidate_id"]
    )
    source_glossary = {
        str(row.get("candidate_id") or ""): deepcopy(dict(row))
        for row in inventory.get("glossary_candidates") or []
        if isinstance(row, Mapping)
    }
    if "" in source_glossary or len(source_glossary) != len(
        list(inventory.get("glossary_candidates") or [])
    ):
        raise ValueError("source glossary candidates have malformed ids")
    glossary_decision_by_id = {
        row["candidate_id"]: row for row in glossary_dispositions
    }
    deferred_glossary_ids = set(
        glossary_review["deferred_source_repair_candidate_ids"]
    )
    confirmed_glossary: list[dict[str, Any]] = []
    pending_glossary: list[dict[str, Any]] = []
    dormant_glossary: list[dict[str, Any]] = []
    for candidate_id, source in sorted(source_glossary.items()):
        if candidate_id in deferred_glossary_ids:
            source.update(
                {
                    "lifecycle_state": "pending_evidence",
                    "publication_state": "pending_source_repair",
                    "authority_scope": "candidate_only",
                    "preferred_rendering_vi": None,
                    "render_policy": "none",
                    "glossary_audit": {
                        "action": "keep_pending",
                        "source_block_ids": [],
                        "resolution_note": "source address requires repair before audit",
                        "request_fingerprint": request_fingerprint,
                    },
                }
            )
            pending_glossary.append(source)
            continue
        decision = glossary_decision_by_id[candidate_id]
        if decision["category_update"] is not None:
            source["category_claim"] = decision["category_update"]
        if decision["local_sense_update"] is not None:
            source["short_description"] = decision["local_sense_update"]
        source["preferred_rendering_vi"] = decision["preferred_rendering_vi"]
        source["render_policy"] = decision["render_policy"]
        source["glossary_audit"] = {
            **deepcopy(decision),
            "request_fingerprint": request_fingerprint,
        }
        if decision["action"] == "confirm_chapter":
            source["lifecycle_state"] = "chapter_confirmed"
            source["publication_state"] = "chapter_confirmed"
            source["authority_scope"] = "chapter_confirmed"
            confirmed_glossary.append(source)
        elif decision["action"] == "keep_pending":
            source["lifecycle_state"] = "pending_evidence"
            source["publication_state"] = "pending_evidence"
            source["authority_scope"] = "candidate_only"
            pending_glossary.append(source)
        else:
            source["lifecycle_state"] = "rejected_dormant"
            source["publication_state"] = "rejected_dormant"
            source["authority_scope"] = "dormant"
            dormant_glossary.append(source)

    source_validation_report = inventory.get("validation_report")
    if not isinstance(source_validation_report, Mapping):
        raise ValueError("source inventory validation_report must be an object")
    report = {
        "schema_version": CONFLICT_SCHEMA_VERSION,
        "chapter_id": manifest["chapter_id"],
        "source_inventory_hash": inventory.get("inventory_hash"),
        "request_fingerprint": request_fingerprint,
        "conflict_manifest_hash": manifest["manifest_hash"],
        "entity_candidates": confirmed_entities,
        "pending_entity_candidates": sorted(
            pending_entities, key=lambda row: row["candidate_id"]
        ),
        "closed_entity_candidates": sorted(
            closed_entities, key=lambda row: row["candidate_id"]
        ),
        "deferred_source_repairs": sorted(
            deferred_source_repairs, key=lambda row: row["candidate_id"]
        ),
        "source_validation_report": deepcopy(dict(source_validation_report)),
        "glossary_candidates": confirmed_glossary,
        "pending_glossary_candidates": pending_glossary,
        "dormant_glossary_candidates": dormant_glossary,
        "glossary_dispositions": glossary_dispositions,
        "unresolved_referents": deepcopy(list(inventory.get("unresolved_referents") or [])),
        "chapter_priority_order": deepcopy(
            list(inventory.get("chapter_priority_order") or [])
        ),
        "quarantined_surfaces": quarantined,
        "global_surface_bindings": bound,
        "component_decisions": sorted(
            normalized_components, key=lambda row: row["component_id"]
        ),
        "source_boundary_normalizations": [
            deepcopy(dict(row)) for row in source_boundary_normalizations
        ],
        "conflict_summary": {
            "component_count": len(manifest["components"]),
            "clean_candidate_count": len(clean_entities),
            "kept_conflict_candidate_count": len(kept_entities),
            "pending_candidate_count": len(pending_entities),
            "deferred_source_repair_count": len(deferred_source_repairs),
            "closed_candidate_count": len(closed_entities),
            "quarantined_surface_count": len(quarantined),
            "global_surface_binding_count": len(bound),
            "confirmed_glossary_count": len(confirmed_glossary),
            "pending_glossary_count": len(pending_glossary),
            "dormant_glossary_count": len(dormant_glossary),
            "normalized_non_authoritative_surface_count": (
                normalized_non_authoritative_surface_count
            ),
            "normalized_non_authoritative_glossary_count": (
                normalized_non_authoritative_glossary_count
            ),
            "source_boundary_normalization_count": len(
                source_boundary_normalizations
            ),
        },
        "production_publish_performed": False,
    }
    return {**report, "conflict_audited_inventory_hash": canonical_hash(report)}


__all__ = [
    "CONFLICT_SCHEMA_VERSION",
    "MAX_COMPONENT_SOURCE_BLOCKS",
    "PROMPT_ID",
    "build_identity_conflict_manifest",
    "entity_conflict_response_schema",
    "normalize_source_boundary_violations",
    "render_entity_conflict_request",
    "validate_and_apply_conflict_response",
]
