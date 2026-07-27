"""Deterministic as-of story-bible assembly for the current literary lineage."""

from __future__ import annotations

from collections import Counter, defaultdict
from copy import deepcopy
from dataclasses import dataclass
import json
import math
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from pipeline.literary.checkpoint import canonical_hash, canonical_json, file_sha256


SCHEMA_VERSION = "literary_b4_story_bible_v3"
WINDOW_SCHEMA_VERSION = "literary_b4_window_slice_v1"
UI_SCHEMA_VERSION = "literary_b4_ui_story_graph_v2"
EVIDENCE_INDEX_SCHEMA_VERSION = "literary_b4_evidence_index_v1"
ANCHOR_INPUT_SCHEMA_VERSION = "literary_b4_address_anchor_input_v1"
ANCHOR_OUTPUT_SCHEMA_VERSION = "literary_b4_address_anchor_output_v2"
REPORT_SCHEMA_VERSION = "literary_b4_assembly_report_v3"
MANIFEST_SCHEMA_VERSION = "literary_b4_input_manifest_v1"
PROFILE_SCHEMA_VERSION = "literary_b4_story_bible_profile_v1"

_HASH_FIELDS = (
    "registry_hash",
    "projection_hash",
    "artifact_hash",
    "catalog_hash",
    "capsule_log_hash",
    "window_plan_hash",
)
_CLAIM_FIELDS = ("gender", "life_stage", "role_or_occupation")
_PROHIBITED_B4_DECISION_FIELDS = {
    "baseline_form",
    "register_variants",
    "pronoun_pair",
    "vocative_options",
    "register_shifts",
    "target_pronoun",
    "target_address_form",
    "translated_sentence",
    "translated_text",
    "translation",
}
_RECORD_CLASS_ORDER = {
    "confirmed_entity": 0,
    "named_entity_candidate": 0,
    "unresolved_named_reference": 1,
    "important_unnamed_referent": 2,
}


class B4StoryBibleError(RuntimeError):
    """Raised when B4 cannot assemble a complete, lineage-safe pack."""


@dataclass(frozen=True)
class LoadedInput:
    source_id: str
    path: Path
    payload: dict[str, Any]
    sha256: str
    declared_hash_field: str | None
    declared_hash: str | None


@dataclass(frozen=True)
class B4Assembly:
    stable: dict[str, Any]
    window_slices: tuple[dict[str, Any], ...]
    ui_view: dict[str, Any]
    evidence_index: dict[str, Any]
    address_anchor_input: dict[str, Any]
    report: dict[str, Any]


def _required_string(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise B4StoryBibleError(f"{label} must be a non-empty string")
    return value


def _required_int(value: Any, label: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise B4StoryBibleError(f"{label} must be a positive integer")
    return value


def _list_of_dicts(value: Any, label: str) -> list[dict[str, Any]]:
    if not isinstance(value, list) or any(not isinstance(row, dict) for row in value):
        raise B4StoryBibleError(f"{label} must be a list of objects")
    return value


def _list_of_strings(value: Any, label: str) -> list[str]:
    if not isinstance(value, list) or any(
        not isinstance(row, str) or not row for row in value
    ):
        raise B4StoryBibleError(f"{label} must be a list of non-empty strings")
    return value


def _read_json_object(path: Path, label: str) -> dict[str, Any]:
    if not path.is_file():
        raise B4StoryBibleError(f"required {label} is missing: {path}")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise B4StoryBibleError(f"cannot read {label}: {path}") from exc
    if not isinstance(value, dict):
        raise B4StoryBibleError(f"{label} must be a JSON object: {path}")
    return value


def _verify_declared_hash(payload: Mapping[str, Any], label: str) -> tuple[str, str] | None:
    present = [field for field in _HASH_FIELDS if field in payload]
    if len(present) > 1:
        raise B4StoryBibleError(f"{label} declares multiple canonical hash fields")
    if not present:
        return None
    field = present[0]
    expected = _required_string(payload[field], f"{label} {field}")
    body = dict(payload)
    body.pop(field, None)
    if canonical_hash(body) != expected:
        raise B4StoryBibleError(f"{label} {field} mismatch")
    return field, expected


def _resolve_manifest_path(base: Path, raw: Any, label: str) -> Path:
    value = _required_string(raw, label)
    path = Path(value)
    if not path.is_absolute():
        path = base / path
    return path.resolve()


def _load_input(
    *,
    source_id: str,
    path: Path,
    label: str,
) -> LoadedInput:
    payload = _read_json_object(path, label)
    declared = _verify_declared_hash(payload, label)
    return LoadedInput(
        source_id=source_id,
        path=path,
        payload=payload,
        sha256=file_sha256(path),
        declared_hash_field=declared[0] if declared else None,
        declared_hash=declared[1] if declared else None,
    )


def load_b4_input_manifest_v1(path: Path) -> dict[str, Any]:
    manifest_path = Path(path).resolve()
    manifest = _read_json_object(manifest_path, "B4 input manifest")
    if manifest.get("schema_version") != MANIFEST_SCHEMA_VERSION:
        raise B4StoryBibleError("unsupported B4 input manifest schema")
    manifest["_manifest_path"] = str(manifest_path)
    return manifest


def load_b4_profile_v1(path: Path | None) -> dict[str, Any]:
    if path is None:
        return {
            "schema_version": PROFILE_SCHEMA_VERSION,
            "b4_token_budget": None,
            "memory_dormancy_chapters": 3,
        }
    profile = _read_json_object(Path(path).resolve(), "B4 profile")
    if profile.get("schema_version") != PROFILE_SCHEMA_VERSION:
        raise B4StoryBibleError("unsupported B4 profile schema")
    budget = profile.get("b4_token_budget")
    if budget is not None:
        _required_int(budget, "b4_token_budget")
    _required_int(
        profile.get("memory_dormancy_chapters"),
        "memory_dormancy_chapters",
    )
    return profile


def _chapter_order_map(manifest: Mapping[str, Any]) -> tuple[dict[str, int], list[dict[str, Any]]]:
    rows = _list_of_dicts(manifest.get("chapters"), "manifest chapters")
    if not rows:
        raise B4StoryBibleError("manifest chapters cannot be empty")
    order_by_id: dict[str, int] = {}
    for row in rows:
        chapter_id = _required_string(row.get("chapter_id"), "manifest chapter_id")
        order = _required_int(row.get("chapter_order"), "manifest chapter_order")
        if chapter_id in order_by_id or order in order_by_id.values():
            raise B4StoryBibleError("manifest repeats a chapter id or order")
        order_by_id[chapter_id] = order
    expected_orders = list(range(1, len(rows) + 1))
    if sorted(order_by_id.values()) != expected_orders:
        raise B4StoryBibleError("manifest chapters must be the complete 1..N prefix")
    rows = sorted(rows, key=lambda row: order_by_id[str(row["chapter_id"])])
    target_id = _required_string(
        manifest.get("target_chapter_id"), "target_chapter_id"
    )
    target_order = _required_int(
        manifest.get("target_chapter_order"), "target_chapter_order"
    )
    if order_by_id.get(target_id) != target_order or target_order != len(rows):
        raise B4StoryBibleError("target chapter must be the final manifest chapter")
    return order_by_id, rows


def _assert_as_of_chapter(
    chapter_id: str,
    *,
    order_by_id: Mapping[str, int],
    target_order: int,
    label: str,
) -> None:
    if chapter_id not in order_by_id:
        raise B4StoryBibleError(f"{label} cites a chapter outside the input prefix")
    if order_by_id[chapter_id] > target_order:
        raise B4StoryBibleError(
            f"{label} leaks future chapter {chapter_id} into as-of chapter "
            f"{target_order}"
        )


def _assert_artifact_chapter(
    payload: Mapping[str, Any],
    expected: str,
    label: str,
) -> None:
    if payload.get("chapter_id") != expected:
        raise B4StoryBibleError(f"{label} chapter_id mismatch")


def _assert_declared_chapters_as_of(
    value: Any,
    *,
    order_by_id: Mapping[str, int],
    target_order: int,
    label: str,
    path: str = "",
) -> None:
    if isinstance(value, dict):
        chapter_id = value.get("chapter_id")
        if isinstance(chapter_id, str) and chapter_id:
            _assert_as_of_chapter(
                chapter_id,
                order_by_id=order_by_id,
                target_order=target_order,
                label=f"{label} {path or 'root'}",
            )
        member_chapters = value.get("member_chapters")
        if isinstance(member_chapters, list):
            for member in member_chapters:
                if isinstance(member, str) and member:
                    _assert_as_of_chapter(
                        member,
                        order_by_id=order_by_id,
                        target_order=target_order,
                        label=f"{label} {path}.member_chapters",
                    )
        for key, item in value.items():
            child = f"{path}.{key}" if path else str(key)
            _assert_declared_chapters_as_of(
                item,
                order_by_id=order_by_id,
                target_order=target_order,
                label=label,
                path=child,
            )
    elif isinstance(value, list):
        for index, item in enumerate(value):
            _assert_declared_chapters_as_of(
                item,
                order_by_id=order_by_id,
                target_order=target_order,
                label=label,
                path=f"{path}[{index}]",
            )


def _lineage_row(item: LoadedInput) -> dict[str, Any]:
    row: dict[str, Any] = {
        "source_id": item.source_id,
        "sha256": item.sha256,
        "size_bytes": item.path.stat().st_size,
    }
    if item.declared_hash_field:
        row["declared_hash_field"] = item.declared_hash_field
        row["declared_hash"] = item.declared_hash
    return row


def _seal(body: Mapping[str, Any], field: str = "artifact_hash") -> dict[str, Any]:
    value = deepcopy(dict(body))
    value.pop(field, None)
    value[field] = canonical_hash(value)
    return value


def _evidence_ref(kind: str, key: Mapping[str, Any]) -> str:
    return "b4evid1_" + canonical_hash(
        {"evidence_kind": kind, "key": dict(key)}
    )[:24]


def _register_evidence(
    entries: dict[str, dict[str, Any]],
    *,
    kind: str,
    key: Mapping[str, Any],
    source_rows: Sequence[Mapping[str, Any]],
) -> str:
    if not source_rows:
        raise B4StoryBibleError("evidence reference has no source row")
    ref = _evidence_ref(kind, key)
    entry = {
        "evidence_ref": ref,
        "evidence_kind": kind,
        "source_rows": sorted(
            (deepcopy(dict(row)) for row in source_rows),
            key=canonical_json,
        ),
    }
    previous = entries.setdefault(ref, entry)
    if canonical_json(previous) != canonical_json(entry):
        raise B4StoryBibleError("evidence reference maps to conflicting source rows")
    return ref


def _source_payload_hash(payload: Mapping[str, Any]) -> str:
    for field in _HASH_FIELDS:
        value = payload.get(field)
        if isinstance(value, str) and value:
            return value
    raise B4StoryBibleError("evidence source has no canonical artifact hash")


def _resolve_evidence_locator(
    locator: Mapping[str, Any],
    *,
    source_payloads: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any]:
    source_id = _required_string(locator.get("source_id"), "evidence source_id")
    source = source_payloads.get(source_id)
    if source is None:
        raise B4StoryBibleError(f"evidence source is unavailable: {source_id}")
    if locator.get("source_artifact_hash") != _source_payload_hash(source):
        raise B4StoryBibleError("evidence source artifact hash mismatch")
    collection = locator.get("collection")
    row_index = locator.get("row_index")
    if not isinstance(row_index, int) or isinstance(row_index, bool) or row_index < 0:
        raise B4StoryBibleError("evidence row_index is malformed")
    if collection == "card_claims":
        parent_id = _required_string(locator.get("parent_id"), "evidence parent_id")
        cards = [
            row
            for row in _list_of_dicts(source.get("cards"), "evidence registry cards")
            if row.get("entity_id") == parent_id
        ]
        if len(cards) != 1:
            raise B4StoryBibleError("evidence claim parent does not resolve uniquely")
        rows = _list_of_dicts(cards[0].get("claims") or [], "evidence card claims")
    elif collection in {
        "relation_edges",
        "speaker_turns",
        "effective_state_projection",
        "glossary_entries",
    }:
        rows = _list_of_dicts(
            source.get(collection) or [], f"evidence {collection}"
        )
        id_field = {
            "relation_edges": "relation_edge_id",
            "speaker_turns": "speaker_turn_id",
            "effective_state_projection": "state_id",
            "glossary_entries": None,
        }[str(collection)]
        if id_field and locator.get("row_id") != (
            rows[row_index].get(id_field) if row_index < len(rows) else None
        ):
            raise B4StoryBibleError(f"evidence {collection} id mismatch")
    else:
        raise B4StoryBibleError("unsupported evidence source collection")
    if row_index >= len(rows):
        raise B4StoryBibleError("evidence row_index is outside its source collection")
    row = rows[row_index]
    if locator.get("row_hash") != canonical_hash(row):
        raise B4StoryBibleError("evidence source row hash mismatch")
    return deepcopy(row)


def _evidence_entry_by_ref(
    evidence_index: Mapping[str, Any],
) -> dict[str, Mapping[str, Any]]:
    expected = evidence_index.get("artifact_hash")
    body = dict(evidence_index)
    body.pop("artifact_hash", None)
    if (
        evidence_index.get("schema_version") != EVIDENCE_INDEX_SCHEMA_VERSION
        or not isinstance(expected, str)
        or canonical_hash(body) != expected
    ):
        raise B4StoryBibleError("B4 evidence index is malformed")
    entries = _list_of_dicts(evidence_index.get("entries"), "evidence index entries")
    result: dict[str, Mapping[str, Any]] = {}
    for entry in entries:
        ref = _required_string(entry.get("evidence_ref"), "evidence_ref")
        if ref in result:
            raise B4StoryBibleError("evidence_ref repeats in evidence index")
        result[ref] = entry
    return result


def _resolve_evidence_ref_from_sources(
    evidence_ref: str,
    *,
    evidence_index: Mapping[str, Any],
    source_payloads: Mapping[str, Mapping[str, Any]],
) -> tuple[dict[str, Any], ...]:
    entry = _evidence_entry_by_ref(evidence_index).get(evidence_ref)
    if entry is None:
        raise B4StoryBibleError(f"evidence_ref is not indexed: {evidence_ref}")
    rows = tuple(
        _resolve_evidence_locator(locator, source_payloads=source_payloads)
        for locator in _list_of_dicts(
            entry.get("source_rows"), "evidence source_rows"
        )
    )
    if not rows:
        raise B4StoryBibleError("evidence_ref resolves to no source row")
    return rows


def _evidence_refs_in(value: Any) -> set[str]:
    refs: set[str] = set()
    if isinstance(value, dict):
        for key, item in value.items():
            if key == "evidence_ref":
                refs.add(_required_string(item, "evidence_ref"))
            else:
                refs.update(_evidence_refs_in(item))
    elif isinstance(value, list):
        for item in value:
            refs.update(_evidence_refs_in(item))
    return refs


def _build_evidence_index(
    *,
    book_id: str,
    chapter_id: str,
    chapter_order: int,
    input_manifest_hash: str,
    entries: Mapping[str, Mapping[str, Any]],
    sources: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    return _seal(
        {
            "schema_version": EVIDENCE_INDEX_SCHEMA_VERSION,
            "book_id": book_id,
            "chapter_id": chapter_id,
            "chapter_order": chapter_order,
            "input_manifest_hash": input_manifest_hash,
            "sources": deepcopy(list(sources)),
            "entries": sorted(
                (deepcopy(dict(row)) for row in entries.values()),
                key=lambda row: str(row["evidence_ref"]),
            ),
            "provider_calls": 0,
        }
    )


def resolve_b4_evidence_ref_v1(
    *,
    evidence_ref: str,
    evidence_index: Mapping[str, Any],
    manifest: Mapping[str, Any],
) -> tuple[dict[str, Any], ...]:
    """Resolve one compact B4 evidence key against exact manifest-bound sources."""
    _required_string(evidence_ref, "evidence_ref")
    _, chapter_rows = _chapter_order_map(manifest)
    manifest_path = Path(
        str(manifest.get("_manifest_path") or Path.cwd() / "manifest.json")
    ).resolve()
    base = manifest_path.parent
    source_payloads: dict[str, Mapping[str, Any]] = {}
    for chapter in chapter_rows:
        chapter_id = str(chapter["chapter_id"])
        for prefix, field in (
            ("registry", "registry_path"),
            ("interaction", "interaction_path"),
            ("temporal", "temporal_path"),
        ):
            loaded = _load_input(
                source_id=f"{prefix}:{chapter_id}",
                path=_resolve_manifest_path(base, chapter.get(field), field),
                label=f"{chapter_id} B4 evidence {prefix}",
            )
            source_payloads[loaded.source_id] = loaded.payload
    if evidence_index.get("book_id") != manifest.get("book_id"):
        raise B4StoryBibleError("evidence index book_id mismatch")
    if evidence_index.get("chapter_id") != manifest.get("target_chapter_id"):
        raise B4StoryBibleError("evidence index chapter_id mismatch")
    return _resolve_evidence_ref_from_sources(
        evidence_ref,
        evidence_index=evidence_index,
        source_payloads=source_payloads,
    )


def _referent_kind_value(value: Any) -> str:
    if isinstance(value, dict):
        value = value.get("value")
    if isinstance(value, str) and value:
        return value
    return "unknown"


def _aliases(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    rows: set[str] = set()
    for item in value:
        if isinstance(item, str) and item:
            rows.add(item)
        elif isinstance(item, dict):
            surface = item.get("surface")
            if isinstance(surface, str) and surface:
                rows.add(surface)
    return sorted(rows)


def _first_chapter_projection(
    registry: Mapping[str, Any],
    *,
    chapter_id: str,
) -> dict[str, Any]:
    entities: list[dict[str, Any]] = []
    for card in _list_of_dicts(registry.get("cards"), "registry cards"):
        entity_id = _required_string(card.get("entity_id"), "registry entity_id")
        first_seen = card.get("first_seen")
        if not isinstance(first_seen, dict) or first_seen.get("chapter_id") != chapter_id:
            raise B4StoryBibleError("first-chapter card has invalid first_seen")
        entities.append(
            {
                "effective_entity_id": entity_id,
                "canonical_surface": _required_string(
                    card.get("canonical_surface"), "card canonical_surface"
                ),
                "stable_surfaces": sorted(
                    set(
                        _list_of_strings(
                            card.get("stable_surfaces") or [],
                            "card stable_surfaces",
                        )
                    )
                ),
                "aliases": _aliases(card.get("aliases")),
                "referent_kind": deepcopy(card.get("referent_kind")),
                "record_class": _required_string(
                    card.get("record_class"), "card record_class"
                ),
                "member_card_ids": [entity_id],
                "member_chapters": [chapter_id],
                "first_seen": deepcopy(first_seen),
                "decision_refs": [],
                "source_refs": deepcopy(card.get("source_refs") or []),
            }
        )
    body = {
        "schema_version": "literary_b4_first_chapter_identity_projection_v1",
        "book_id": None,
        "effective_entities": sorted(
            entities, key=lambda row: str(row["effective_entity_id"])
        ),
        "pending_cases": [],
        "resolved_distinct_cases": [],
        "source_registry_hashes": [registry["registry_hash"]],
        "identity_authority_granted": False,
    }
    return _seal(body, "projection_hash")


def _claim_rows_by_card(
    registries: Sequence[Mapping[str, Any]],
    *,
    order_by_id: Mapping[str, int],
    target_order: int,
) -> dict[str, list[dict[str, Any]]]:
    rows: dict[str, list[dict[str, Any]]] = defaultdict(list)
    seen: dict[tuple[str, str, str, str], dict[str, Any]] = {}
    for registry in registries:
        registry_chapter = _required_string(
            registry.get("chapter_id"), "registry chapter_id"
        )
        source_id = f"registry:{registry_chapter}"
        source_artifact_hash = _source_payload_hash(registry)
        for card in _list_of_dicts(registry.get("cards"), "registry cards"):
            card_id = _required_string(card.get("entity_id"), "registry entity_id")
            for claim_index, claim in enumerate(
                _list_of_dicts(card.get("claims") or [], "card claims")
            ):
                field = claim.get("field")
                if field not in _CLAIM_FIELDS or claim.get("status") != "supported":
                    continue
                provenance = claim.get("provenance")
                chapter_id = (
                    provenance.get("chapter_id")
                    if isinstance(provenance, dict)
                    else registry_chapter
                )
                chapter_id = _required_string(
                    chapter_id, "supported claim chapter_id"
                )
                _assert_as_of_chapter(
                    chapter_id,
                    order_by_id=order_by_id,
                    target_order=target_order,
                    label="supported claim",
                )
                value = claim.get("value")
                if value is None:
                    raise B4StoryBibleError("supported claim has no value")
                signature = (
                    card_id,
                    str(field),
                    canonical_json(value),
                    chapter_id,
                )
                locator = {
                    "source_id": source_id,
                    "source_artifact_hash": source_artifact_hash,
                    "collection": "card_claims",
                    "parent_id": card_id,
                    "row_index": claim_index,
                    "row_hash": canonical_hash(claim),
                }
                existing = seen.get(signature)
                if existing is not None:
                    if locator not in existing["_evidence_locators"]:
                        existing["_evidence_locators"].append(locator)
                    continue
                row = {
                    "value": deepcopy(value),
                    "chapter_id": chapter_id,
                    "established_in_chapter": chapter_id,
                    "_field": field,
                    "_evidence_locators": [locator],
                }
                seen[signature] = row
                rows[card_id].append(row)
    return rows


def _build_entities(
    projection: Mapping[str, Any],
    registries: Sequence[Mapping[str, Any]],
    *,
    evidence_entries: dict[str, dict[str, Any]],
    order_by_id: Mapping[str, int],
    target_order: int,
) -> tuple[list[dict[str, Any]], dict[str, str], dict[str, dict[str, Any]]]:
    claims_by_card = _claim_rows_by_card(
        registries,
        order_by_id=order_by_id,
        target_order=target_order,
    )
    card_to_effective: dict[str, str] = {}
    entity_by_id: dict[str, dict[str, Any]] = {}
    entities: list[dict[str, Any]] = []
    for source in _list_of_dicts(
        projection.get("effective_entities"), "effective_entities"
    ):
        effective_id = _required_string(
            source.get("effective_entity_id"), "effective_entity_id"
        )
        member_card_ids = _list_of_strings(
            source.get("member_card_ids"), "member_card_ids"
        )
        member_chapters = _list_of_strings(
            source.get("member_chapters"), "member_chapters"
        )
        for chapter_id in member_chapters:
            _assert_as_of_chapter(
                chapter_id,
                order_by_id=order_by_id,
                target_order=target_order,
                label="effective entity member",
            )
        first_seen = source.get("first_seen")
        if not isinstance(first_seen, dict):
            raise B4StoryBibleError("effective entity first_seen is malformed")
        established = _required_string(
            first_seen.get("chapter_id"), "effective entity first_seen chapter_id"
        )
        _assert_as_of_chapter(
            established,
            order_by_id=order_by_id,
            target_order=target_order,
            label="effective entity",
        )
        for card_id in member_card_ids:
            previous = card_to_effective.setdefault(card_id, effective_id)
            if previous != effective_id:
                raise B4StoryBibleError("one card maps to multiple effective entities")
        grouped_claims: dict[str, dict[str, Any]] = {}
        for field in _CLAIM_FIELDS:
            field_rows: dict[tuple[str, str], dict[str, Any]] = {}
            for card_id in member_card_ids:
                for row in claims_by_card.get(card_id, []):
                    if row["_field"] != field:
                        continue
                    key = (canonical_json(row["value"]), row["chapter_id"])
                    existing = field_rows.get(key)
                    if existing is None:
                        existing = {
                            "value": deepcopy(row["value"]),
                            "chapter_id": row["chapter_id"],
                            "_evidence_locators": [],
                        }
                        field_rows[key] = existing
                    for locator in row["_evidence_locators"]:
                        if locator not in existing["_evidence_locators"]:
                            existing["_evidence_locators"].append(deepcopy(locator))
            ordered = sorted(
                field_rows.values(),
                key=lambda row: (
                    order_by_id[row["chapter_id"]],
                    canonical_json(row["value"]),
                ),
            )
            if not ordered:
                continue
            values_by_key: dict[str, Any] = {}
            evidence_locators: list[dict[str, Any]] = []
            for claim_row in ordered:
                values_by_key.setdefault(
                    canonical_json(claim_row["value"]),
                    deepcopy(claim_row["value"]),
                )
                for locator in claim_row["_evidence_locators"]:
                    if locator not in evidence_locators:
                        evidence_locators.append(deepcopy(locator))
            values = list(values_by_key.values())
            ref = _register_evidence(
                evidence_entries,
                kind="claim",
                key={
                    "effective_entity_id": effective_id,
                    "field": field,
                },
                source_rows=evidence_locators,
            )
            grouped_claims[field] = (
                {
                    "value": values[0],
                    "evidence_ref": ref,
                }
                if len(values) == 1
                else {
                    "values": values,
                    "claim_conflict": True,
                    "evidence_ref": ref,
                }
            )
        first_seen_block = _required_string(
            first_seen.get("block_id"), "effective entity first_seen block_id"
        )
        row = {
            "effective_entity_id": effective_id,
            "canonical_surface": _required_string(
                source.get("canonical_surface"), "effective canonical_surface"
            ),
            "stable_surfaces": sorted(
                set(
                    _list_of_strings(
                        source.get("stable_surfaces") or [],
                        "effective stable_surfaces",
                    )
                )
            ),
            "aliases": _aliases(source.get("aliases")),
            "referent_kind": _referent_kind_value(source.get("referent_kind")),
            "record_class": _required_string(
                source.get("record_class"), "effective record_class"
            ),
            "member_card_ids": sorted(set(member_card_ids)),
            "member_chapters": sorted(
                set(member_chapters), key=lambda chapter: order_by_id[chapter]
            ),
            "first_seen": first_seen_block,
            "claims": grouped_claims,
            "established_in_chapter": established,
        }
        entities.append(row)
        entity_by_id[effective_id] = row
    entities.sort(key=lambda row: str(row["effective_entity_id"]))
    return entities, card_to_effective, entity_by_id


def _build_relations(
    registries: Sequence[Mapping[str, Any]],
    *,
    card_to_effective: Mapping[str, str],
    evidence_entries: dict[str, dict[str, Any]],
    order_by_id: Mapping[str, int],
    target_order: int,
) -> list[dict[str, Any]]:
    relations: list[dict[str, Any]] = []
    for registry in registries:
        registry_chapter = _required_string(
            registry.get("chapter_id"), "registry chapter_id"
        )
        source_id = f"registry:{registry_chapter}"
        source_artifact_hash = _source_payload_hash(registry)
        for edge_index, edge in enumerate(
            _list_of_dicts(
                registry.get("relation_edges") or [], "registry relation_edges"
            )
        ):
            chapter_id = _required_string(
                edge.get("chapter_id") or registry_chapter,
                "relation chapter_id",
            )
            _assert_as_of_chapter(
                chapter_id,
                order_by_id=order_by_id,
                target_order=target_order,
                label="relation edge",
            )
            source_card = _required_string(
                edge.get("source_entity_id"), "relation source_entity_id"
            )
            target_card = _required_string(
                edge.get("target_entity_id"), "relation target_entity_id"
            )
            if source_card not in card_to_effective or target_card not in card_to_effective:
                raise B4StoryBibleError(
                    "relation endpoint is absent from effective identity projection"
                )
            relation_edge_id = _required_string(
                edge.get("relation_edge_id"), "relation_edge_id"
            )
            evidence_ref = _register_evidence(
                evidence_entries,
                kind="relation",
                key={"relation_edge_id": relation_edge_id},
                source_rows=[
                    {
                        "source_id": source_id,
                        "source_artifact_hash": source_artifact_hash,
                        "collection": "relation_edges",
                        "row_id": relation_edge_id,
                        "row_index": edge_index,
                        "row_hash": canonical_hash(edge),
                    }
                ],
            )
            relations.append(
                {
                    "relation_edge_id": relation_edge_id,
                    "relation": _required_string(
                        edge.get("relation"), "relation"
                    ),
                    "relation_family": edge.get("relation_family"),
                    "source_effective_entity_id": card_to_effective[source_card],
                    "target_effective_entity_id": card_to_effective[target_card],
                    "relation_note": edge.get("relation_note"),
                    "evidence_ref": evidence_ref,
                    "chapter_id": chapter_id,
                    "semantic_status": edge.get("semantic_status"),
                    "structurally_contested": bool(
                        edge.get("structurally_contested")
                    ),
                    "contested_group_id": edge.get("contested_group_id"),
                    "contested_rule": edge.get("contested_rule"),
                    "effective": bool(edge.get("effective", False)),
                    "established_in_chapter": chapter_id,
                }
            )
    return sorted(
        relations,
        key=lambda row: (
            order_by_id[row["chapter_id"]],
            str(row["relation_edge_id"]),
        ),
    )


def _build_referent_catalog(
    component_catalogs: Sequence[Mapping[str, Any]],
    *,
    card_to_effective: Mapping[str, str],
) -> dict[str, dict[str, Any]]:
    catalog: dict[str, dict[str, Any]] = {}
    for component_catalog in component_catalogs:
        chapter_id = _required_string(
            component_catalog.get("chapter_id"),
            "B3 component catalog chapter_id",
        )
        for component in _list_of_dicts(
            component_catalog.get("components"), "B3 components"
        ):
            for card in _list_of_dicts(
                component.get("candidate_cards") or [], "B3 candidate_cards"
            ):
                ref = _required_string(card.get("referent_ref"), "B3 referent_ref")
                card_id = _required_string(
                    card.get("candidate_card_id"), "B3 candidate_card_id"
                )
                row = {
                    "referent_ref": ref,
                    "candidate_card_id": card_id,
                    "effective_entity_id": card_to_effective.get(card_id),
                    "canonical_surface": _required_string(
                        card.get("canonical_surface"), "B3 canonical_surface"
                    ),
                    "observed_surfaces": [
                        {
                            "surface": _required_string(
                                card.get("canonical_surface"),
                                "B3 canonical_surface",
                            ),
                            "chapter_id": chapter_id,
                        }
                    ],
                }
                previous = catalog.get(ref)
                if previous is None:
                    catalog[ref] = row
                    continue
                if (
                    previous["candidate_card_id"] != card_id
                    or previous["effective_entity_id"] != row["effective_entity_id"]
                ):
                    raise B4StoryBibleError(
                        "B3 referent_ref maps to conflicting candidate cards"
                    )
                surface_row = row["observed_surfaces"][0]
                if surface_row not in previous["observed_surfaces"]:
                    previous["observed_surfaces"].append(surface_row)
                previous["observed_surfaces"].sort(
                    key=lambda item: (str(item["chapter_id"]), str(item["surface"]))
                )
                # The same stable referent may be presented under a later surface.
                # Keep the latest as display text and retain every supplied surface.
                previous["canonical_surface"] = row["canonical_surface"]
    return catalog


def _state_establishment_index(
    temporal_history: Sequence[Mapping[str, Any]],
) -> tuple[dict[str, str], dict[str, str], dict[str, str]]:
    by_state: dict[str, str] = {}
    by_observation: dict[str, str] = {}
    by_case: dict[str, str] = {}

    def bind(target: dict[str, str], key: Any, chapter_id: str, label: str) -> None:
        if not isinstance(key, str) or not key:
            return
        previous = target.setdefault(key, chapter_id)
        if previous != chapter_id:
            raise B4StoryBibleError(f"{label} is attributed to multiple chapters")

    for artifact in temporal_history:
        chapter_id = _required_string(
            artifact.get("chapter_id"), "temporal history chapter_id"
        )
        for row in _list_of_dicts(
            artifact.get("new_state_rows") or [], "new_state_rows"
        ):
            bind(by_state, row.get("state_id"), chapter_id, "state_id")
        for field in (
            "confirmed_observation_rows",
            "historical_observations",
            "non_effective_observations",
        ):
            for row in _list_of_dicts(artifact.get(field) or [], field):
                source_chapter = _required_string(
                    row.get("chapter_id") or chapter_id,
                    f"{field} chapter_id",
                )
                bind(
                    by_observation,
                    row.get("observation_id"),
                    source_chapter,
                    "observation_id",
                )
                bind(
                    by_case,
                    row.get("pending_case_id"),
                    source_chapter,
                    "pending_case_id",
                )
        for field in ("pending_cases", "resolved_cases"):
            for row in _list_of_dicts(artifact.get(field) or [], field):
                source_chapter = _required_string(
                    row.get("chapter_id") or chapter_id,
                    f"{field} chapter_id",
                )
                bind(
                    by_case,
                    row.get("pending_case_id"),
                    source_chapter,
                    "pending_case_id",
                )
    return by_state, by_observation, by_case


def _referent_rows(
    refs: Sequence[str],
    *,
    referent_catalog: Mapping[str, Mapping[str, Any]],
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for ref in refs:
        source = referent_catalog.get(ref)
        if source is None:
            rows.append(
                {
                    "referent_ref": ref,
                    "candidate_card_id": None,
                    "effective_entity_id": None,
                    "canonical_surface": None,
                    "catalog_status": "not_supplied_to_current_b3_prefix",
                }
            )
        else:
            rows.append({**deepcopy(dict(source)), "catalog_status": "mapped"})
    return rows


def _build_states(
    target_temporal: Mapping[str, Any],
    temporal_history: Sequence[Mapping[str, Any]],
    *,
    referent_catalog: Mapping[str, Mapping[str, Any]],
    evidence_entries: dict[str, dict[str, Any]],
    order_by_id: Mapping[str, int],
    target_order: int,
) -> list[dict[str, Any]]:
    by_state, by_observation, by_case = _state_establishment_index(temporal_history)
    fields = (
        "state_id",
        "semantic_key",
        "state_domain",
        "state_value",
        "valid_from_block_id",
        "valid_to_block_id",
        "lifecycle_status",
        "authority_status",
    )
    states: list[dict[str, Any]] = []
    temporal_chapter = _required_string(
        target_temporal.get("chapter_id"), "target temporal chapter_id"
    )
    chapter_ids = tuple(
        sorted(order_by_id, key=lambda chapter_id: len(chapter_id), reverse=True)
    )

    def chapter_from_source_blocks(source: Mapping[str, Any]) -> str | None:
        block_ids: set[str] = set()
        for field in ("observed_at_block_id", "valid_from_block_id"):
            value = source.get(field)
            if isinstance(value, str) and value:
                block_ids.add(value)
        block_ids.update(
            _list_of_strings(
                source.get("source_block_ids") or [],
                "state source_block_ids",
            )
        )
        matched = {
            chapter_id
            for block_id in block_ids
            for chapter_id in chapter_ids
            if block_id.startswith(f"{chapter_id}_b")
        }
        if len(matched) > 1:
            raise B4StoryBibleError(
                "temporal state source blocks span multiple chapters"
            )
        return next(iter(matched)) if matched else None

    source_artifact_hash = _source_payload_hash(target_temporal)
    for state_index, source in enumerate(
        _list_of_dicts(
            target_temporal.get("effective_state_projection"),
            "effective_state_projection",
        )
    ):
        state_id = _required_string(source.get("state_id"), "state_id")
        chapters = {
            value
            for value in (
                by_state.get(state_id),
                by_observation.get(source.get("opened_by_observation_id")),
                by_case.get(source.get("source_pending_case_id")),
            )
            if value
        }
        establishment_basis = "temporal_decision_lineage"
        if not chapters:
            derived = chapter_from_source_blocks(source)
            if derived is not None:
                chapters.add(derived)
                establishment_basis = "source_block_chapter"
        if len(chapters) != 1:
            raise B4StoryBibleError(
                f"state {state_id} lacks one recorded source-decision chapter"
            )
        established = next(iter(chapters))
        _assert_as_of_chapter(
            established,
            order_by_id=order_by_id,
            target_order=target_order,
            label="temporal state",
        )
        row = {field: deepcopy(source.get(field)) for field in fields if field in source}
        subject_refs = _list_of_strings(
            source.get("subject_referent_refs") or [],
            "state subject_referent_refs",
        )
        counterpart_refs = _list_of_strings(
            source.get("counterpart_referent_refs") or [],
            "state counterpart_referent_refs",
        )
        for output_field, refs in (
            ("subject_referents", subject_refs),
            ("counterpart_referents", counterpart_refs),
        ):
            row[output_field] = [
                {
                    "effective_entity_id": item.get("effective_entity_id"),
                    "surface": item.get("canonical_surface"),
                    "resolution_status": item.get("catalog_status"),
                }
                for item in _referent_rows(refs, referent_catalog=referent_catalog)
            ]
        row["evidence_ref"] = _register_evidence(
            evidence_entries,
            kind="temporal_state",
            key={"state_id": state_id},
            source_rows=[
                {
                    "source_id": f"temporal:{temporal_chapter}",
                    "source_artifact_hash": source_artifact_hash,
                    "collection": "effective_state_projection",
                    "row_id": state_id,
                    "row_index": state_index,
                    "row_hash": canonical_hash(source),
                }
            ],
        )
        row["established_in_chapter"] = established
        row["establishment_basis"] = establishment_basis
        states.append(row)
    return sorted(states, key=lambda row: str(row["state_id"]))


def _frame_by_block(interaction: Mapping[str, Any]) -> dict[str, str]:
    result: dict[str, str] = {}
    for frame in _list_of_dicts(
        interaction.get("frame_segments"), "frame_segments"
    ):
        frame_id = _required_string(
            frame.get("frame_segment_id"), "frame_segment_id"
        )
        for block_id in _list_of_strings(
            frame.get("covered_block_ids") or [],
            "frame covered_block_ids",
        ):
            previous = result.setdefault(block_id, frame_id)
            if previous != frame_id:
                raise B4StoryBibleError("one block belongs to multiple B2 frames")
    return result


def _recovery_overlay_index(
    recovery: Mapping[str, Any] | None,
    *,
    interaction_hash: str,
) -> dict[tuple[str, str], dict[str, Any]]:
    if recovery is None:
        return {}
    if recovery.get("source_b2_artifact_hash") != interaction_hash:
        raise B4StoryBibleError("speaker recovery source B2 hash mismatch")
    index: dict[tuple[str, str], dict[str, Any]] = {}
    for field, role in (
        ("speaker_overlays", "speaker"),
        ("addressee_overlays", "addressee"),
    ):
        for overlay in _list_of_dicts(recovery.get(field) or [], field):
            turn_id = _required_string(
                overlay.get("speaker_turn_id"), "overlay speaker_turn_id"
            )
            if overlay.get("endpoint_role") != role:
                raise B4StoryBibleError("speaker recovery endpoint role mismatch")
            endpoint = overlay.get("effective_endpoint")
            if endpoint is None and overlay.get("action") == "keep_pending":
                endpoint = overlay.get("original_endpoint")
            if not isinstance(endpoint, dict):
                raise B4StoryBibleError("speaker recovery effective_endpoint malformed")
            key = (turn_id, role)
            if key in index:
                raise B4StoryBibleError("speaker recovery repeats a turn endpoint")
            index[key] = deepcopy(endpoint)
    return index


def _normalize_endpoint(
    endpoint: Mapping[str, Any],
    *,
    card_to_effective: Mapping[str, str],
) -> dict[str, Any]:
    surface = endpoint.get("surface")
    if not isinstance(surface, str):
        surface = ""
    status = _required_string(
        endpoint.get("resolution_status"), "endpoint resolution_status"
    )
    card_ids = _list_of_strings(
        endpoint.get("candidate_card_ids") or [],
        "endpoint candidate_card_ids",
    )
    effective_ids = sorted(
        {card_to_effective[card_id] for card_id in card_ids if card_id in card_to_effective}
    )
    fully_resolved = (
        status == "resolved_candidate"
        and len(card_ids) == 1
        and len(effective_ids) == 1
    )
    return {
        "surface": surface,
        "resolution_status": status,
        "candidate_card_ids": card_ids,
        "effective_entity_ids": effective_ids,
        "resolved_to_effective_entity": fully_resolved,
        "unresolved": not fully_resolved,
    }


def _collect_turns(
    chapter_rows: Sequence[dict[str, Any]],
    *,
    card_to_effective: Mapping[str, str],
    order_by_id: Mapping[str, int],
) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]]]:
    turns: list[dict[str, Any]] = []
    evidence_by_turn_id: dict[str, dict[str, Any]] = {}
    seen_ids: set[str] = set()
    for chapter in chapter_rows:
        chapter_id = chapter["chapter_id"]
        interaction = chapter["interaction"].payload
        frame_by_block = _frame_by_block(interaction)
        overlays = _recovery_overlay_index(
            chapter.get("recovery").payload if chapter.get("recovery") else None,
            interaction_hash=interaction["artifact_hash"],
        )
        source_id = f"interaction:{chapter_id}"
        source_artifact_hash = _source_payload_hash(interaction)
        for turn_index, source in enumerate(
            _list_of_dicts(interaction.get("speaker_turns"), "speaker_turns")
        ):
            turn_id = _required_string(
                source.get("speaker_turn_id"), "speaker_turn_id"
            )
            if turn_id in seen_ids:
                raise B4StoryBibleError("speaker_turn_id repeats across chapters")
            seen_ids.add(turn_id)
            evidence_by_turn_id[turn_id] = {
                "source_id": source_id,
                "source_artifact_hash": source_artifact_hash,
                "collection": "speaker_turns",
                "row_id": turn_id,
                "row_index": turn_index,
                "row_hash": canonical_hash(source),
            }
            block_id = _required_string(source.get("block_id"), "turn block_id")
            frame_id = frame_by_block.get(block_id)
            if frame_id is None:
                raise B4StoryBibleError("speaker turn block is outside B2 frame coverage")
            speaker_source = overlays.get((turn_id, "speaker"), source.get("speaker"))
            addressee_source = overlays.get(
                (turn_id, "addressee"), source.get("addressee")
            )
            if not isinstance(speaker_source, dict) or not isinstance(
                addressee_source, dict
            ):
                raise B4StoryBibleError("speaker turn endpoint is malformed")
            turns.append(
                {
                    "speaker_turn_id": turn_id,
                    "block_id": block_id,
                    "utterance_anchor": _required_string(
                        source.get("utterance_anchor"), "utterance_anchor"
                    ),
                    "frame_segment_id": frame_id,
                    "speaker": _normalize_endpoint(
                        speaker_source, card_to_effective=card_to_effective
                    ),
                    "addressee": _normalize_endpoint(
                        addressee_source, card_to_effective=card_to_effective
                    ),
                    "address_terms": deepcopy(source.get("address_terms") or []),
                    "register_cue": source.get("register_cue"),
                    "register_cue_raw": source.get("register_cue_raw"),
                    "delivery_tone": source.get("delivery_tone"),
                    "chapter_id": chapter_id,
                    "chapter_order": order_by_id[chapter_id],
                    "established_in_chapter": chapter_id,
                }
            )
    return (
        sorted(
            turns,
            key=lambda row: (
                row["chapter_order"],
                row["block_id"],
                row["speaker_turn_id"],
            ),
        ),
        evidence_by_turn_id,
    )


def _endpoint_key(endpoint: Mapping[str, Any]) -> tuple[str, str]:
    if endpoint.get("resolved_to_effective_entity"):
        return "entity", str(endpoint["effective_entity_ids"][0])
    return "raw", str(endpoint.get("surface") or "")


def _resolved_address_pair_id(
    speaker_effective_entity_id: str | None,
    addressee_effective_entity_id: str | None,
) -> str | None:
    if not speaker_effective_entity_id or not addressee_effective_entity_id:
        return None
    return canonical_hash(
        {
            "speaker_effective_entity_id": speaker_effective_entity_id,
            "addressee_effective_entity_id": addressee_effective_entity_id,
        }
    )[:24]


def _count_rows(values: Iterable[Any], key_name: str) -> list[dict[str, Any]]:
    counter = Counter(value for value in values if value not in (None, ""))
    return [
        {key_name: key, "count": count}
        for key, count in sorted(counter.items(), key=lambda item: str(item[0]))
    ]


def _pending_identity_card_ids(projection: Mapping[str, Any]) -> set[str]:
    result: set[str] = set()
    for row in _list_of_dicts(
        projection.get("pending_cases") or [], "identity pending_cases"
    ):
        for field in ("card_ids", "candidate_set", "current_candidate_set"):
            value = row.get(field)
            if isinstance(value, list):
                result.update(item for item in value if isinstance(item, str))
    return result


def _entity_missing_claims(
    effective_id: str | None,
    *,
    entity_by_id: Mapping[str, Mapping[str, Any]],
) -> list[str]:
    if not effective_id or effective_id not in entity_by_id:
        return ["gender", "life_stage"]
    claims = entity_by_id[effective_id].get("claims")
    if not isinstance(claims, dict):
        return ["gender", "life_stage"]
    return [
        field
        for field in ("gender", "life_stage")
        if not isinstance(claims.get(field), dict)
        or (
            "value" not in claims[field]
            and not claims[field].get("values")
        )
    ]


def _relation_lookup(
    relations: Sequence[Mapping[str, Any]],
) -> dict[frozenset[str], list[Mapping[str, Any]]]:
    result: dict[frozenset[str], list[Mapping[str, Any]]] = defaultdict(list)
    for row in relations:
        result[
            frozenset(
                (
                    str(row["source_effective_entity_id"]),
                    str(row["target_effective_entity_id"]),
                )
            )
        ].append(row)
    return result


def _build_address_pairs(
    turns: Sequence[Mapping[str, Any]],
    *,
    relations: Sequence[Mapping[str, Any]],
    entity_by_id: Mapping[str, Mapping[str, Any]],
    pending_identity_cards: set[str],
    order_by_id: Mapping[str, int],
) -> list[dict[str, Any]]:
    relation_by_pair = _relation_lookup(relations)
    groups: dict[
        tuple[tuple[str, str], tuple[str, str]], list[Mapping[str, Any]]
    ] = defaultdict(list)
    for turn in turns:
        groups[
            (_endpoint_key(turn["speaker"]), _endpoint_key(turn["addressee"]))
        ].append(turn)
    output: list[dict[str, Any]] = []
    for (speaker_key, addressee_key), group in groups.items():
        speaker_effective = (
            speaker_key[1] if speaker_key[0] == "entity" else None
        )
        addressee_effective = (
            addressee_key[1] if addressee_key[0] == "entity" else None
        )
        speaker_resolved = speaker_effective is not None
        addressee_resolved = addressee_effective is not None
        terms: dict[str, dict[str, Any]] = {}
        for turn in group:
            for term in turn.get("address_terms") or []:
                if not isinstance(term, str) or not term:
                    continue
                row = terms.setdefault(
                    term,
                    {
                        "term": term,
                        "count": 0,
                        "chapters": set(),
                        "example_anchor": turn["utterance_anchor"],
                        "established_in_chapter": turn["chapter_id"],
                    },
                )
                row["count"] += 1
                row["chapters"].add(turn["chapter_id"])
        observed_terms = []
        for row in terms.values():
            clean = dict(row)
            clean["chapters"] = sorted(
                row["chapters"], key=lambda chapter: order_by_id[chapter]
            )
            observed_terms.append(clean)
        observed_terms.sort(key=lambda row: row["term"])
        relation_rows = (
            relation_by_pair.get(
                frozenset((speaker_effective, addressee_effective)), []
            )
            if speaker_effective and addressee_effective
            else []
        )
        speaker_cards = {
            card_id
            for turn in group
            for card_id in turn["speaker"]["candidate_card_ids"]
        }
        addressee_cards = {
            card_id
            for turn in group
            for card_id in turn["addressee"]["candidate_card_ids"]
        }
        established = min(
            (turn["chapter_id"] for turn in group),
            key=lambda chapter: order_by_id[chapter],
        )
        pair_id = _resolved_address_pair_id(
            speaker_effective,
            addressee_effective,
        )
        output.append(
            {
                "pair_id": pair_id,
                "unanchored": pair_id is None,
                "speaker_effective_entity_id": speaker_effective,
                "addressee_effective_entity_id": addressee_effective,
                "speaker_surface": group[0]["speaker"]["surface"],
                "addressee_surface": group[0]["addressee"]["surface"],
                "speaker_resolved": speaker_resolved,
                "addressee_resolved": addressee_resolved,
                "observed_terms": observed_terms,
                "registers": _count_rows(
                    (turn.get("register_cue") for turn in group),
                    "register_cue",
                ),
                "tones": _count_rows(
                    (turn.get("delivery_tone") for turn in group),
                    "delivery_tone",
                ),
                "turn_count": len(group),
                "vocative_count": sum(
                    len(turn.get("address_terms") or []) for turn in group
                ),
                "relation_present": bool(relation_rows),
                "relation_contested": any(
                    bool(row.get("structurally_contested"))
                    for row in relation_rows
                ),
                "relation_edge_ids": sorted(
                    str(row["relation_edge_id"]) for row in relation_rows
                ),
                "missing_claims": {
                    "speaker": _entity_missing_claims(
                        speaker_effective, entity_by_id=entity_by_id
                    ),
                    "addressee": _entity_missing_claims(
                        addressee_effective, entity_by_id=entity_by_id
                    ),
                },
                "pending_identity": bool(
                    (speaker_cards | addressee_cards) & pending_identity_cards
                ),
                "anchorable": (
                    speaker_resolved and addressee_resolved
                ),
                "example_anchor": group[0]["utterance_anchor"],
                "turn_ids": sorted(str(turn["speaker_turn_id"]) for turn in group),
                "source_block_ids": sorted(
                    {str(turn["block_id"]) for turn in group}
                ),
                "chapters": sorted(
                    {str(turn["chapter_id"]) for turn in group},
                    key=lambda chapter: order_by_id[chapter],
                ),
                "established_in_chapter": established,
            }
        )
    return sorted(
        output,
        key=lambda row: (
            str(row["speaker_effective_entity_id"] or row["speaker_surface"]),
            str(row["addressee_effective_entity_id"] or row["addressee_surface"]),
        ),
    )


def _build_idiolect(
    turns: Sequence[Mapping[str, Any]],
    registries: Sequence[Mapping[str, Any]],
    *,
    evidence_entries: dict[str, dict[str, Any]],
    order_by_id: Mapping[str, int],
) -> list[dict[str, Any]]:
    speaker_turns: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for turn in turns:
        speaker = turn["speaker"]
        if speaker.get("resolved_to_effective_entity"):
            speaker_turns[str(speaker["effective_entity_ids"][0])].append(turn)
    glossary: list[dict[str, Any]] = []
    for registry in registries:
        chapter_id = str(registry["chapter_id"])
        source_artifact_hash = _source_payload_hash(registry)
        for entry_index, entry in enumerate(
            _list_of_dicts(
                registry.get("glossary_entries") or [], "glossary_entries"
            )
        ):
            blocks = set(
                _list_of_strings(
                    entry.get("source_block_ids") or [],
                    "glossary source_block_ids",
                )
            )
            glossary.append(
                {
                    "surface": _required_string(
                        entry.get("surface"), "glossary surface"
                    ),
                    "contextual_sense": entry.get("contextual_sense"),
                    "chapter_id": chapter_id,
                    "source_block_ids": blocks,
                    "_evidence_locator": {
                        "source_id": f"registry:{chapter_id}",
                        "source_artifact_hash": source_artifact_hash,
                        "collection": "glossary_entries",
                        "row_index": entry_index,
                        "row_hash": canonical_hash(entry),
                    },
                }
            )
    output: list[dict[str, Any]] = []
    for effective_id, rows in speaker_turns.items():
        blocks = {str(turn["block_id"]) for turn in rows}
        terms: dict[tuple[str, str], dict[str, Any]] = {}
        for entry in glossary:
            if not blocks.intersection(entry["source_block_ids"]):
                continue
            key = (
                entry["surface"],
                canonical_json(entry["contextual_sense"]),
            )
            row = terms.setdefault(
                key,
                {
                    "surface": entry["surface"],
                    "contextual_sense": deepcopy(entry["contextual_sense"]),
                        "chapters": set(),
                        "source_block_ids": set(),
                        "_evidence_locators": [],
                        "established_in_chapter": entry["chapter_id"],
                    },
                )
            row["chapters"].add(entry["chapter_id"])
            row["source_block_ids"].update(entry["source_block_ids"])
            locator = entry["_evidence_locator"]
            if locator not in row["_evidence_locators"]:
                row["_evidence_locators"].append(deepcopy(locator))
            if order_by_id[entry["chapter_id"]] < order_by_id[
                row["established_in_chapter"]
            ]:
                row["established_in_chapter"] = entry["chapter_id"]
        term_rows = []
        for row in terms.values():
            clean = dict(row)
            clean["chapters"] = sorted(
                row["chapters"], key=lambda chapter: order_by_id[chapter]
            )
            clean.pop("source_block_ids", None)
            locators = clean.pop("_evidence_locators")
            clean["evidence_ref"] = _register_evidence(
                evidence_entries,
                kind="glossary_term",
                key={
                    "effective_entity_id": effective_id,
                    "surface": clean["surface"],
                    "contextual_sense": clean["contextual_sense"],
                },
                source_rows=locators,
            )
            term_rows.append(clean)
        established = min(
            (str(turn["chapter_id"]) for turn in rows),
            key=lambda chapter: order_by_id[chapter],
        )
        output.append(
            {
                "effective_entity_id": effective_id,
                "glossary_terms_in_own_speech": sorted(
                    term_rows, key=lambda row: (row["surface"], row["chapters"])
                ),
                "register_distribution": _count_rows(
                    (turn.get("register_cue") for turn in rows),
                    "register_cue",
                ),
                "tone_distribution": _count_rows(
                    (turn.get("delivery_tone") for turn in rows),
                    "delivery_tone",
                ),
                "turn_count": len(rows),
                "established_in_chapter": established,
            }
        )
    return sorted(output, key=lambda row: row["effective_entity_id"])


def _build_narrative_position(
    *,
    capsule_log: Mapping[str, Any],
    target_interaction: Mapping[str, Any],
    prior_summary: Mapping[str, Any] | None,
    card_to_effective: Mapping[str, str],
    order_by_id: Mapping[str, int],
    target_order: int,
) -> dict[str, Any]:
    capsules: list[dict[str, Any]] = []
    for source in _list_of_dicts(capsule_log.get("capsules"), "capsules"):
        chapter_id = _required_string(source.get("chapter_id"), "capsule chapter_id")
        chapter_order = _required_int(
            source.get("chapter_order"), "capsule chapter_order"
        )
        _assert_as_of_chapter(
            chapter_id,
            order_by_id=order_by_id,
            target_order=target_order,
            label="narrative capsule",
        )
        if order_by_id[chapter_id] != chapter_order:
            raise B4StoryBibleError("capsule chapter order mismatch")
        capsules.append(
            {
                "capsule_id": source.get("capsule_id"),
                "chapter_id": chapter_id,
                "chapter_order": chapter_order,
                "text": source.get("text"),
                "entity_refs": deepcopy(source.get("entity_refs") or []),
                "event_refs": deepcopy(source.get("event_refs") or []),
                "state_refs": deepcopy(source.get("state_refs") or []),
                "established_in_chapter": chapter_id,
            }
        )
    frames: list[dict[str, Any]] = []
    target_id = _required_string(
        target_interaction.get("chapter_id"), "target interaction chapter_id"
    )
    for source in _list_of_dicts(
        target_interaction.get("frame_segments"), "target frame_segments"
    ):
        card_ids = _list_of_strings(
            source.get("candidate_card_ids") or [],
            "frame candidate_card_ids",
        )
        frames.append(
            {
                "frame_segment_id": _required_string(
                    source.get("frame_segment_id"), "frame_segment_id"
                ),
                "narrative_mode": _required_string(
                    source.get("narrative_mode"), "narrative_mode"
                ),
                "narrator_surface": source.get("narrator_surface"),
                "narrator_status": source.get("narrator_status"),
                "narrator_card_ids": card_ids,
                "narrator_effective_entity_ids": sorted(
                    {
                        card_to_effective[card_id]
                        for card_id in card_ids
                        if card_id in card_to_effective
                    }
                ),
                "start_block_id": source.get("start_block_id"),
                "end_block_id": source.get("end_block_id"),
                "established_in_chapter": target_id,
            }
        )
    handoff = None
    if prior_summary is not None:
        prior_chapter = _required_string(
            prior_summary.get("chapter_id"), "prior B0 chapter_id"
        )
        _assert_as_of_chapter(
            prior_chapter,
            order_by_id=order_by_id,
            target_order=target_order,
            label="prior narrative handoff",
        )
        summary = prior_summary.get("summary")
        if not isinstance(summary, dict) or not isinstance(
            summary.get("narrative_handoff"), dict
        ):
            raise B4StoryBibleError("prior B0 narrative_handoff is missing")
        handoff = {
            **deepcopy(summary["narrative_handoff"]),
            "established_in_chapter": prior_chapter,
        }
    return {
        "capsules": sorted(capsules, key=lambda row: row["chapter_order"]),
        "frames": sorted(frames, key=lambda row: str(row["start_block_id"])),
        "handoff": handoff,
    }


def _build_open_questions(
    *,
    projection: Mapping[str, Any],
    target_temporal: Mapping[str, Any],
    relations: Sequence[Mapping[str, Any]],
    turns: Sequence[Mapping[str, Any]],
    turn_evidence_by_id: Mapping[str, Mapping[str, Any]],
    evidence_entries: dict[str, dict[str, Any]],
    order_by_id: Mapping[str, int],
    target_order: int,
    target_chapter_id: str,
    target_card_ids: set[str],
) -> tuple[dict[str, list[dict[str, Any]]], dict[str, Any]]:
    identity_by_component: dict[str, dict[str, Any]] = {}
    identity_source_count = 0
    for source in _list_of_dicts(
        projection.get("pending_cases") or [], "identity pending_cases"
    ):
        identity_source_count += 1
        chapter_id = _required_string(
            source.get("chapter_id"), "pending identity chapter_id"
        )
        _assert_as_of_chapter(
            chapter_id,
            order_by_id=order_by_id,
            target_order=target_order,
            label="pending identity case",
        )
        component_id = _required_string(
            source.get("component_id"), "identity component_id"
        )
        metadata = {
            "question_type": source.get("question_type"),
            "reason": source.get("reason"),
            "resolution_condition": source.get("resolution_condition"),
            "chapter_id": chapter_id,
            "review_route": source.get("review_route"),
            "state": source.get("state"),
            "established_in_chapter": chapter_id,
        }
        previous = identity_by_component.get(component_id)
        if previous is None:
            identity_by_component[component_id] = {
                "component_id": component_id,
                "card_ids": set(
                    _list_of_strings(
                        source.get("card_ids") or [],
                        "identity card_ids",
                    )
                ),
                **metadata,
                "source_row_count": 1,
            }
            continue
        if any(previous.get(key) != value for key, value in metadata.items()):
            raise B4StoryBibleError(
                "identity pending rows for one component disagree"
            )
        previous["card_ids"].update(
            _list_of_strings(
                source.get("card_ids") or [],
                "identity card_ids",
            )
        )
        previous["source_row_count"] += 1
    identity_source_components = len(identity_by_component)
    identity = []
    for row in identity_by_component.values():
        normalized = dict(row)
        normalized["card_ids"] = sorted(row["card_ids"])
        if row["card_ids"].intersection(target_card_ids):
            identity.append(normalized)

    active_temporal_routes = {"temporal_review", "stable_claim_review"}
    parked_temporal_routes = {"identity_review", "inherited_identity_block"}
    temporal_route_counts: Counter[str] = Counter()
    temporal_source_count = 0
    pending_states: list[dict[str, Any]] = []
    for source in _list_of_dicts(
        target_temporal.get("pending_cases") or [], "B3 pending_cases"
    ):
        temporal_source_count += 1
        chapter_id = _required_string(
            source.get("chapter_id"), "pending state chapter_id"
        )
        _assert_as_of_chapter(
            chapter_id,
            order_by_id=order_by_id,
            target_order=target_order,
            label="pending temporal case",
        )
        review_route = _required_string(
            source.get("review_route"), "pending state review_route"
        )
        temporal_route_counts[review_route] += 1
        if review_route in parked_temporal_routes:
            continue
        if review_route not in active_temporal_routes:
            raise B4StoryBibleError(
                f"unsupported B3 pending review route: {review_route}"
            )
        if chapter_id != target_chapter_id:
            continue
        pending_states.append(
            {
                "pending_case_id": _required_string(
                    source.get("pending_case_id"), "pending_case_id"
                ),
                "review_route": review_route,
                "reason": source.get("reason"),
                "reason_codes": deepcopy(source.get("reason_codes") or []),
                "chapter_id": chapter_id,
                "lifecycle_state": source.get("lifecycle_state"),
                "parked_reason": source.get("parked_reason"),
                "established_in_chapter": chapter_id,
            }
        )
    terminal_route_counts: Counter[str] = Counter()
    terminal_source_count = 0
    for source in _list_of_dicts(
        target_temporal.get("resolved_cases") or [], "B3 resolved_cases"
    ):
        if source.get("disposition") != "origin_unknown":
            continue
        terminal_source_count += 1
        chapter_id = _required_string(
            source.get("chapter_id"), "origin_unknown chapter_id"
        )
        _assert_as_of_chapter(
            chapter_id,
            order_by_id=order_by_id,
            target_order=target_order,
            label="origin_unknown case",
        )
        window = source.get("unknowable_window")
        if not isinstance(window, dict):
            raise B4StoryBibleError("origin_unknown case lacks unknowable_window")
        terminal_route_counts[
            str(source.get("review_route") or "unspecified")
        ] += 1
    contested_groups: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for relation in relations:
        if relation.get("structurally_contested"):
            group_id = _required_string(
                relation.get("contested_group_id"), "contested_group_id"
            )
            contested_groups[group_id].append(relation)
    contested: list[dict[str, Any]] = []
    for group_id, rows in contested_groups.items():
        established = min(
            (str(row["chapter_id"]) for row in rows),
            key=lambda chapter: order_by_id[chapter],
        )
        contested.append(
            {
                "contested_group_id": group_id,
                "relation_edge_ids": sorted(
                    str(row["relation_edge_id"]) for row in rows
                ),
                "contested_rule": rows[0].get("contested_rule"),
                "chapters": sorted(
                    {str(row["chapter_id"]) for row in rows},
                    key=lambda chapter: order_by_id[chapter],
                ),
                "established_in_chapter": established,
            }
        )
    unresolved_groups: dict[
        tuple[tuple[str, str], tuple[str, str]],
        list[Mapping[str, Any]],
    ] = defaultdict(list)
    for turn in turns:
        if turn["chapter_id"] != target_chapter_id:
            continue
        if not turn["speaker"]["unresolved"] and not turn["addressee"]["unresolved"]:
            continue
        unresolved_groups[
            (_endpoint_key(turn["speaker"]), _endpoint_key(turn["addressee"]))
        ].append(turn)
    unresolved_address: list[dict[str, Any]] = []
    for (speaker_key, addressee_key), group in unresolved_groups.items():
        example = group[0]
        unresolved_sides = sorted(
            {
                side
                for turn in group
                for side in ("speaker", "addressee")
                if turn[side]["unresolved"]
            }
        )
        status_counts = Counter(
            (side, str(turn[side]["resolution_status"]))
            for turn in group
            for side in ("speaker", "addressee")
            if turn[side]["unresolved"]
        )
        status_rows = [
            {
                "side": side,
                "status": status,
                "turn_count": count,
            }
            for (side, status), count in sorted(status_counts.items())
        ]
        turn_id = str(example["speaker_turn_id"])
        locator = turn_evidence_by_id.get(turn_id)
        if locator is None:
            raise B4StoryBibleError("unresolved address example lacks source evidence")
        evidence_ref = _register_evidence(
            evidence_entries,
            kind="speaker_turn",
            key={"speaker_turn_id": turn_id},
            source_rows=[locator],
        )
        speaker_effective = (
            speaker_key[1] if speaker_key[0] == "entity" else None
        )
        addressee_effective = (
            addressee_key[1] if addressee_key[0] == "entity" else None
        )
        established = min(
            (str(turn["chapter_id"]) for turn in group),
            key=lambda chapter: order_by_id[chapter],
        )
        row = {
            "speaker_surface": example["speaker"]["surface"],
            "speaker_effective_entity_id": speaker_effective,
            "addressee_surface": example["addressee"]["surface"],
            "addressee_effective_entity_id": addressee_effective,
            "unresolved_side": (
                unresolved_sides[0] if len(unresolved_sides) == 1 else None
            ),
            "resolution_status": (
                status_rows[0]["status"] if len(status_rows) == 1 else None
            ),
            "turn_count": len(group),
            "chapters": sorted(
                {str(turn["chapter_id"]) for turn in group},
                key=lambda chapter: order_by_id[chapter],
            ),
            "example_anchor": example["utterance_anchor"],
            "evidence_ref": evidence_ref,
            "established_in_chapter": established,
        }
        if len(unresolved_sides) > 1:
            row["unresolved_sides"] = unresolved_sides
        if len(status_rows) > 1:
            row["resolution_statuses"] = status_rows
        unresolved_address.append(row)
    open_questions = {
        "pending_identity_cases": sorted(
            identity, key=lambda row: str(row["component_id"])
        ),
        "pending_states": sorted(
            pending_states, key=lambda row: str(row["pending_case_id"])
        ),
        "unknowable_windows": [],
        "contested_relations": sorted(
            contested, key=lambda row: str(row["contested_group_id"])
        ),
        "unresolved_address": sorted(
            unresolved_address,
            key=lambda row: (
                order_by_id[row["established_in_chapter"]],
                str(
                    row["speaker_effective_entity_id"]
                    or row["speaker_surface"]
                ),
                str(
                    row["addressee_effective_entity_id"]
                    or row["addressee_surface"]
                ),
            ),
        ),
    }
    projection_report = {
        "schema_version": "literary_b4_open_question_projection_v1",
        "identity": {
            "source_rows": identity_source_count,
            "source_components": identity_source_components,
            "selected_components": len(identity),
            "grouped_rows": identity_source_count - identity_source_components,
            "omitted_not_target_relevant_components": (
                identity_source_components - len(identity)
            ),
        },
        "temporal": {
            "source_rows": temporal_source_count,
            "selected_actionable_rows": len(pending_states),
            "parked_identity_rows": sum(
                temporal_route_counts[route] for route in parked_temporal_routes
            ),
            "omitted_historical_actionable_rows": (
                sum(
                    temporal_route_counts[route]
                    for route in active_temporal_routes
                )
                - len(pending_states)
            ),
            "route_counts": dict(sorted(temporal_route_counts.items())),
        },
        "terminal_origin_unknown": {
            "source_rows": terminal_source_count,
            "selected_active_rows": 0,
            "route_counts": dict(sorted(terminal_route_counts.items())),
        },
        "unresolved_address": {
            "scope": "target_chapter_only",
            "target_chapter_id": target_chapter_id,
            "selected_groups": len(unresolved_address),
        },
    }
    return open_questions, projection_report


def _estimated_tokens(value: Any) -> int:
    return max(1, math.ceil(len(canonical_json(value).encode("utf-8")) / 4))


def _entity_priority(
    entity: Mapping[str, Any],
    *,
    order_by_id: Mapping[str, int],
    target_order: int,
    dormancy_chapters: int,
) -> tuple[int, int, int, str]:
    member_orders = [
        order_by_id[chapter] for chapter in entity.get("member_chapters") or []
    ]
    if not member_orders:
        raise B4StoryBibleError("effective entity has no member chapter")
    member_count = len(set(member_orders))
    dormancy = target_order - max(member_orders)
    if member_count >= 2 or dormancy < dormancy_chapters:
        tier = 1
    elif _referent_kind_value(entity.get("referent_kind")) in {
        "person",
        "nonhuman_character",
        "unknown",
    }:
        tier = 2
    else:
        tier = 3
    record_rank = _RECORD_CLASS_ORDER.get(str(entity.get("record_class")), 9)
    return tier, dormancy, record_rank, str(entity["effective_entity_id"])


def _apply_budget(
    stable_body: dict[str, Any],
    *,
    token_budget: int | None,
    dormancy_chapters: int,
    order_by_id: Mapping[str, int],
    target_order: int,
    protected_entity_ids: set[str] | None = None,
    current_speaker_entity_ids: set[str] | None = None,
) -> tuple[dict[str, Any], list[dict[str, Any]], dict[str, Any]]:
    full = deepcopy(stable_body)
    full_tokens = _estimated_tokens(full)
    if token_budget is None:
        memory_budget = {
            "token_budget": None,
            "full_estimated_tokens": full_tokens,
            "selected_estimated_tokens": 0,
            "omitted_count": 0,
            "omission_counts": {},
            "omissions_hash": canonical_hash([]),
        }
        full["memory_budget"] = memory_budget
        memory_budget["selected_estimated_tokens"] = _estimated_tokens(full)
        return (
            full,
            [],
            memory_budget,
        )
    entities = full["entities"]
    relations = full["relations"]
    idiolect = full["idiolect"]
    current_speakers = set(current_speaker_entity_ids or set())
    priority = {
        str(entity["effective_entity_id"]): _entity_priority(
            entity,
            order_by_id=order_by_id,
            target_order=target_order,
            dormancy_chapters=dormancy_chapters,
        )
        for entity in entities
    }
    protected: set[str] = set(protected_entity_ids or set()) | {
        str(row["source_effective_entity_id"])
        for row in relations
        if row.get("structurally_contested")
    } | {
        str(row["target_effective_entity_id"])
        for row in relations
        if row.get("structurally_contested")
    }
    card_to_effective = {
        card_id: str(entity["effective_entity_id"])
        for entity in entities
        for card_id in entity.get("member_card_ids") or []
    }
    for case in full["open_questions"]["pending_identity_cases"]:
        protected.update(
            card_to_effective[card_id]
            for card_id in case.get("card_ids") or []
            if card_id in card_to_effective
        )
    selected = {
        entity_id
        for entity_id, row_priority in priority.items()
        if row_priority[0] == 1
    } | protected

    def project(ids: set[str]) -> dict[str, Any]:
        value = deepcopy(full)
        value["entities"] = [
            row for row in entities if row["effective_entity_id"] in ids
        ]
        value["relations"] = [
            row
            for row in relations
            if row["structurally_contested"]
            or (
                row["source_effective_entity_id"] in ids
                and row["target_effective_entity_id"] in ids
            )
        ]
        value["idiolect"] = [
            row
            for row in idiolect
            if row["effective_entity_id"] in ids
            and row["effective_entity_id"] in current_speakers
        ]
        return value

    required_pack = project(selected)
    if _estimated_tokens(required_pack) > token_budget:
        raise B4StoryBibleError(
            "B4 token budget cannot hold tier-1 and protected rows"
        )
    optional = sorted(
        (entity_id for entity_id in priority if entity_id not in selected),
        key=lambda entity_id: priority[entity_id],
    )
    admitted_optional: list[str] = []
    for entity_id in optional:
        candidate_ids = selected | {entity_id}
        if _estimated_tokens(project(candidate_ids)) <= token_budget:
            selected = candidate_ids
            admitted_optional.append(entity_id)

    def build_omissions(
        chosen: Mapping[str, Any],
        selected_ids: set[str],
    ) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        omitted_entities = {
            entity_id for entity_id in priority if entity_id not in selected_ids
        }
        for entity_id in sorted(
            omitted_entities, key=lambda value: priority[value]
        ):
            rows.append(
                {
                    "row_id": entity_id,
                    "row_kind": "entity",
                    "tier": priority[entity_id][0],
                    "reason": "b4_token_budget",
                }
            )
        chosen_relation_ids = {
            str(row["relation_edge_id"]) for row in chosen["relations"]
        }
        for row in relations:
            if str(row["relation_edge_id"]) in chosen_relation_ids:
                continue
            endpoint_tiers = (
                priority[str(row["source_effective_entity_id"])][0],
                priority[str(row["target_effective_entity_id"])][0],
            )
            rows.append(
                {
                    "row_id": row["relation_edge_id"],
                    "row_kind": "relation",
                    "tier": max(endpoint_tiers),
                    "reason": "endpoint_omitted_by_b4_token_budget",
                }
            )
        chosen_idiolect_ids = {
            str(row["effective_entity_id"]) for row in chosen["idiolect"]
        }
        for row in idiolect:
            entity_id = str(row["effective_entity_id"])
            if entity_id in chosen_idiolect_ids:
                continue
            rows.append(
                {
                    "row_id": entity_id,
                    "row_kind": "idiolect",
                    "tier": priority.get(entity_id, (3,))[0],
                    "reason": (
                        "not_target_chapter_speaker"
                        if entity_id not in current_speakers
                        else "entity_omitted_by_b4_token_budget"
                    ),
                }
            )
        return rows

    def attach_budget_summary(
        chosen: dict[str, Any],
        omissions: Sequence[Mapping[str, Any]],
    ) -> dict[str, Any]:
        counts = Counter(
            f"{row['row_kind']}:{row['reason']}" for row in omissions
        )
        summary = {
            "token_budget": token_budget,
            "full_estimated_tokens": full_tokens,
            "selected_estimated_tokens": 0,
            "omitted_count": len(omissions),
            "omission_counts": dict(sorted(counts.items())),
            "omissions_hash": canonical_hash(list(omissions)),
        }
        value = deepcopy(chosen)
        value["memory_budget"] = summary
        for _ in range(4):
            estimate = _estimated_tokens(value)
            if summary["selected_estimated_tokens"] == estimate:
                break
            summary["selected_estimated_tokens"] = estimate
        return value

    chosen = project(selected)
    omissions = build_omissions(chosen, selected)
    chosen = attach_budget_summary(chosen, omissions)
    while _estimated_tokens(chosen) > token_budget and admitted_optional:
        selected.remove(admitted_optional.pop())
        chosen = project(selected)
        omissions = build_omissions(chosen, selected)
        chosen = attach_budget_summary(chosen, omissions)
    selected_tokens = _estimated_tokens(chosen)
    if selected_tokens > token_budget:
        raise B4StoryBibleError(
            "B4 token budget cannot hold tier-1 and protected rows including audit metadata"
        )
    budget_report = deepcopy(chosen["memory_budget"])
    return (
        chosen,
        omissions,
        budget_report,
    )


def _build_window_slices(
    *,
    target_chapter_id: str,
    book_id: str,
    window_plan: Mapping[str, Any],
    target_turns: Sequence[Mapping[str, Any]],
    relations: Sequence[Mapping[str, Any]],
    entity_by_id: Mapping[str, Mapping[str, Any]],
    pending_identity_cards: set[str],
    order_by_id: Mapping[str, int],
    lineage_hash: str,
    source_b2_hash: str,
    source_recovery_hash: str | None,
) -> tuple[dict[str, Any], ...]:
    if window_plan.get("chapter_id") != target_chapter_id:
        raise B4StoryBibleError("window plan chapter_id mismatch")
    active_owner: dict[str, str] = {}
    windows = _list_of_dicts(window_plan.get("windows"), "window_plan windows")
    for window in windows:
        window_id = _required_string(window.get("window_id"), "window_id")
        for block_id in _list_of_strings(
            window.get("active_block_ids"), "active_block_ids"
        ):
            previous = active_owner.setdefault(block_id, window_id)
            if previous != window_id:
                raise B4StoryBibleError("window active block repeats")
    turn_ids = {str(turn["speaker_turn_id"]) for turn in target_turns}
    active_turn_ids: set[str] = set()
    slices: list[dict[str, Any]] = []
    for index, window in enumerate(windows, start=1):
        window_id = str(window["window_id"])
        active_blocks = set(window["active_block_ids"])
        tail_blocks = set(
            _list_of_strings(
                window.get("preceding_tail_block_ids") or [],
                "preceding_tail_block_ids",
            )
        )
        rows = []
        for source in target_turns:
            block_id = str(source["block_id"])
            membership = (
                "active"
                if block_id in active_blocks
                else "tail"
                if block_id in tail_blocks
                else None
            )
            if membership is None:
                continue
            row = deepcopy(dict(source))
            row["window_membership"] = membership
            rows.append(row)
            if membership == "active":
                turn_id = str(source["speaker_turn_id"])
                if turn_id in active_turn_ids:
                    raise B4StoryBibleError(
                        "speaker turn appears active in more than one window"
                    )
                active_turn_ids.add(turn_id)
        pairs = _build_address_pairs(
            rows,
            relations=relations,
            entity_by_id=entity_by_id,
            pending_identity_cards=pending_identity_cards,
            order_by_id=order_by_id,
        )
        body = {
            "schema_version": WINDOW_SCHEMA_VERSION,
            "book_id": book_id,
            "chapter_id": target_chapter_id,
            "window_id": window_id,
            "window_order": index,
            "window_plan_hash": window_plan["window_plan_hash"],
            "active_block_ids": deepcopy(window["active_block_ids"]),
            "preceding_tail_block_ids": deepcopy(
                window.get("preceding_tail_block_ids") or []
            ),
            "estimated_active_source_tokens": window.get(
                "estimated_active_source_tokens"
            ),
            "speaker_turns": rows,
            "address_pairs": pairs,
            "lineage": {
                "story_bible_lineage_hash": lineage_hash,
                "source_b2_artifact_hash": source_b2_hash,
                "source_speaker_recovery_artifact_hash": source_recovery_hash,
            },
            "provider_calls": 0,
        }
        slices.append(_seal(body))
    if active_turn_ids != turn_ids:
        missing = sorted(turn_ids - active_turn_ids)
        raise B4StoryBibleError(
            f"window plan does not actively cover every speaker turn: {missing}"
        )
    return tuple(slices)


def _plain_claim_values(claims: Any) -> dict[str, Any]:
    if not isinstance(claims, dict):
        return {}
    output: dict[str, Any] = {}
    for field, row in claims.items():
        if not isinstance(row, dict):
            continue
        if "value" in row:
            output[str(field)] = deepcopy(row["value"])
        elif isinstance(row.get("values"), list):
            output[str(field)] = deepcopy(row["values"])
    return output


def _build_ui_view(
    *,
    stable: Mapping[str, Any],
) -> dict[str, Any]:
    nodes = [
        {
            "node_id": row["effective_entity_id"],
            "label": row["canonical_surface"],
            "kind": row["referent_kind"],
            "record_class": row["record_class"],
            "first": row["first_seen"],
            "member_chapters": deepcopy(row["member_chapters"]),
            "surface_forms": sorted(
                {
                    str(surface)
                    for surface in (
                        [row["canonical_surface"]]
                        + list(row.get("stable_surfaces") or [])
                        + list(row.get("aliases") or [])
                    )
                    if surface
                }
            ),
            "claims": _plain_claim_values(row["claims"]),
        }
        for row in stable["entities"]
    ]
    node_ids = {str(row["node_id"]) for row in nodes}
    edges = []
    for row in stable["relations"]:
        source_id = str(row["source_effective_entity_id"])
        target_id = str(row["target_effective_entity_id"])
        if source_id not in node_ids or target_id not in node_ids:
            raise B4StoryBibleError("UI relation edge would be orphaned")
        edges.append(
            {
                "edge_id": row["relation_edge_id"],
                "source_node_id": source_id,
                "target_node_id": target_id,
                "relation": row["relation"],
                "relation_family": row["relation_family"],
                "chapter_id": row["chapter_id"],
                "effective": row["effective"],
                "structurally_contested": row["structurally_contested"],
                "contested_group_id": row["contested_group_id"],
            }
        )
    card_to_effective = {
        card_id: str(row["effective_entity_id"])
        for row in stable["entities"]
        for card_id in row.get("member_card_ids") or []
    }
    pending = []
    relation_endpoints_by_group: dict[str, set[str]] = defaultdict(set)
    for edge in edges:
        group_id = edge.get("contested_group_id")
        if group_id:
            relation_endpoints_by_group[str(group_id)].update(
                (str(edge["source_node_id"]), str(edge["target_node_id"]))
            )
    for kind, rows in stable["open_questions"].items():
        for row in rows:
            effective_ids = {
                card_to_effective[card_id]
                for card_id in row.get("card_ids") or []
                if card_id in card_to_effective
            }
            for field in (
                "speaker_effective_entity_id",
                "addressee_effective_entity_id",
            ):
                value = row.get(field)
                if isinstance(value, str) and value in node_ids:
                    effective_ids.add(value)
            contested_group_id = row.get("contested_group_id")
            if contested_group_id:
                effective_ids.update(
                    relation_endpoints_by_group.get(str(contested_group_id), set())
                )
            pending.append(
                {
                    "pending_kind": kind,
                    "pending_id": (
                        row.get("component_id")
                        or row.get("pending_case_id")
                        or row.get("contested_group_id")
                        or row.get("evidence_ref")
                    ),
                    "effective_entity_ids": sorted(effective_ids),
                    "established_in_chapter": row["established_in_chapter"],
                }
            )
    body = {
        "schema_version": UI_SCHEMA_VERSION,
        "book_id": stable["book_id"],
        "chapter_id": stable["chapter_id"],
        "chapter_order": stable["chapter_order"],
        "story_bible_artifact_hash": stable["artifact_hash"],
        "nodes": sorted(nodes, key=lambda row: str(row["node_id"])),
        "edges": sorted(edges, key=lambda row: str(row["edge_id"])),
        "pending": sorted(
            pending,
            key=lambda row: (str(row["pending_kind"]), str(row["pending_id"])),
        ),
        "provider_calls": 0,
    }
    return _seal(body)


def _build_anchor_input(
    *,
    stable: Mapping[str, Any],
    all_pairs: Sequence[Mapping[str, Any]],
    target_chapter_id: str,
    entity_by_id: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any]:
    current_pairs = [
        row
        for row in all_pairs
        if target_chapter_id in row.get("chapters", []) and row.get("anchorable")
    ]
    relation_by_id = {
        str(row["relation_edge_id"]): row for row in stable["relations"]
    }
    rows = []
    for pair in current_pairs:
        speaker_id = str(pair["speaker_effective_entity_id"])
        addressee_id = str(pair["addressee_effective_entity_id"])
        rows.append(
            {
                "pair_id": _required_string(pair.get("pair_id"), "pair_id"),
                "speaker_effective_entity_id": speaker_id,
                "addressee_effective_entity_id": addressee_id,
                "speaker_surface": entity_by_id[speaker_id]["canonical_surface"],
                "addressee_surface": entity_by_id[addressee_id][
                    "canonical_surface"
                ],
                "observed_terms": deepcopy(pair["observed_terms"]),
                "registers": deepcopy(pair["registers"]),
                "tones": deepcopy(pair["tones"]),
                "turn_count": pair["turn_count"],
                "example_anchor": pair["example_anchor"],
                "source_block_ids": deepcopy(pair["source_block_ids"]),
                "relations": [
                    deepcopy(relation_by_id[edge_id])
                    for edge_id in pair["relation_edge_ids"]
                    if edge_id in relation_by_id
                ],
                "speaker_claims": deepcopy(entity_by_id[speaker_id]["claims"]),
                "addressee_claims": deepcopy(entity_by_id[addressee_id]["claims"]),
                "evidence_completeness": {
                    key: deepcopy(pair[key])
                    for key in (
                        "speaker_resolved",
                        "addressee_resolved",
                        "turn_count",
                        "vocative_count",
                        "relation_present",
                        "relation_contested",
                        "missing_claims",
                        "pending_identity",
                        "anchorable",
                    )
                },
            }
        )
    body = {
        "schema_version": ANCHOR_INPUT_SCHEMA_VERSION,
        "book_id": stable["book_id"],
        "chapter_id": target_chapter_id,
        "story_bible_artifact_hash": stable["artifact_hash"],
        "pairs": sorted(rows, key=lambda row: row["pair_id"]),
        "provider_calls": 0,
    }
    return _seal(body)


def validate_address_anchor_output_v1(
    *,
    anchor_input: Mapping[str, Any],
    response: Mapping[str, Any],
) -> dict[str, Any]:
    if response.get("schema_version") != ANCHOR_OUTPUT_SCHEMA_VERSION:
        raise B4StoryBibleError("unsupported Address Anchor output schema")
    if response.get("chapter_id") != anchor_input.get("chapter_id"):
        raise B4StoryBibleError("Address Anchor chapter_id mismatch")
    if response.get("anchor_input_artifact_hash") != anchor_input.get(
        "artifact_hash"
    ):
        raise B4StoryBibleError("Address Anchor input hash mismatch")
    allowed = {
        str(row["pair_id"]): row
        for row in _list_of_dicts(anchor_input.get("pairs"), "anchor input pairs")
    }
    decisions = _list_of_dicts(response.get("pair_decisions"), "pair_decisions")
    seen: set[str] = set()
    validated: list[dict[str, Any]] = []
    issues: list[dict[str, Any]] = []
    forbidden_translation_fields = {
        "translated_sentence",
        "translation",
        "target_text",
        "translated_text",
    }
    decision_keys = {
        "pair_id",
        "pronoun_pair",
        "vocative_options",
        "register_shifts",
        "evidence_refs",
        "model_confidence",
        "not_anchored",
    }
    for source in decisions:
        if forbidden_translation_fields.intersection(source):
            raise B4StoryBibleError("Address Anchor output contains translated text")
        if set(source) != decision_keys:
            raise B4StoryBibleError("Address Anchor decision keys differ")
        pair_id = _required_string(source.get("pair_id"), "anchor pair_id")
        if pair_id not in allowed or pair_id in seen:
            raise B4StoryBibleError("Address Anchor pair is foreign or repeated")
        seen.add(pair_id)
        not_anchored = source.get("not_anchored")
        pronoun_pair = source.get("pronoun_pair")
        if not_anchored is not None:
            if not isinstance(not_anchored, dict) or not isinstance(
                not_anchored.get("reason"), str
            ) or set(not_anchored) != {"reason"} or not not_anchored["reason"].strip():
                raise B4StoryBibleError("not_anchored reason is required")
            if (
                pronoun_pair is not None
                or source.get("vocative_options")
                or source.get("register_shifts")
            ):
                raise B4StoryBibleError(
                    "not_anchored pair cannot receive address anchors"
                )
        else:
            pronoun_pair = _validate_pronoun_pair(
                pronoun_pair, "pronoun_pair"
            )
        confidence = source.get("model_confidence")
        if confidence not in {"high", "medium", "low"}:
            raise B4StoryBibleError("invalid Address Anchor model_confidence")

        vocative_options = _list_of_dicts(
            source.get("vocative_options"), "vocative_options"
        )
        normalized_vocatives: list[dict[str, Any]] = []
        seen_vocatives: set[str] = set()
        for option in vocative_options:
            if set(option) not in ({"form"}, {"form", "note"}):
                raise B4StoryBibleError(
                    "Address Anchor vocative option keys differ"
                )
            form = _required_string(option.get("form"), "vocative form")
            normalized_form = form.casefold()
            if normalized_form in seen_vocatives:
                raise B4StoryBibleError(
                    "Address Anchor repeats a vocative option"
                )
            seen_vocatives.add(normalized_form)
            normalized = {"form": form}
            if "note" in option:
                normalized["note"] = _required_string(
                    option.get("note"), "vocative note"
                )
            normalized_vocatives.append(normalized)

        shifts = _list_of_dicts(
            source.get("register_shifts"), "register_shifts"
        )
        allowed_registers = {
            str(row["register_cue"])
            for row in allowed[pair_id].get("registers") or []
            if isinstance(row, Mapping) and row.get("register_cue")
        }
        seen_registers: set[str] = set()
        normalized_shifts: list[dict[str, Any]] = []
        for shift in shifts:
            if set(shift) != {
                "register_cue",
                "pronoun_pair",
                "rationale",
            }:
                raise B4StoryBibleError(
                    "Address Anchor register shift keys differ"
                )
            register = _required_string(
                shift.get("register_cue"), "register_cue"
            )
            if register not in allowed_registers or register in seen_registers:
                raise B4StoryBibleError(
                    "Address Anchor register shift is foreign or repeated"
                )
            seen_registers.add(register)
            shifted_pair = _validate_pronoun_pair(
                shift.get("pronoun_pair"), "register shift pronoun_pair"
            )
            if pronoun_pair is not None and shifted_pair == pronoun_pair:
                raise B4StoryBibleError(
                    "Address Anchor register shift repeats baseline pronoun_pair"
                )
            normalized_shifts.append(
                {
                    "register_cue": register,
                    "pronoun_pair": shifted_pair,
                    "rationale": _required_string(
                        shift.get("rationale"), "register shift rationale"
                    ),
                }
            )
        evidence_refs = _list_of_strings(
            source.get("evidence_refs") or [], "anchor evidence_refs"
        )
        allowed_evidence = set(
            _list_of_strings(
                allowed[pair_id].get("source_block_ids"),
                "anchor source_block_ids",
            )
        )
        if any(value not in allowed_evidence for value in evidence_refs):
            raise B4StoryBibleError("Address Anchor cites a foreign source block")
        if not_anchored is None and not evidence_refs:
            raise B4StoryBibleError("anchored pair requires direct evidence")
        row = deepcopy(source)
        row["evidence_refs"] = evidence_refs
        row["vocative_options"] = normalized_vocatives
        row["register_shifts"] = normalized_shifts
        if pronoun_pair is not None:
            row["pronoun_pair"] = pronoun_pair
        if not_anchored is not None:
            row["not_anchored"] = {"reason": not_anchored["reason"].strip()}
        validated.append(row)
        completeness = allowed[pair_id]["evidence_completeness"]
        known_gap = (
            not completeness.get("speaker_resolved")
            or not completeness.get("addressee_resolved")
            or bool(completeness.get("missing_claims", {}).get("speaker"))
            or bool(completeness.get("missing_claims", {}).get("addressee"))
        )
        if confidence == "high" and known_gap:
            issues.append(
                {
                    "issue_kind": "anchor_confidence_exceeds_evidence",
                    "pair_id": pair_id,
                }
            )
    if seen != set(allowed):
        raise B4StoryBibleError("Address Anchor decisions do not exact-cover pairs")
    body = {
        "schema_version": "literary_b4_validated_address_anchor_v2",
        "chapter_id": anchor_input["chapter_id"],
        "anchor_input_artifact_hash": anchor_input["artifact_hash"],
        "pair_decisions": sorted(validated, key=lambda row: str(row["pair_id"])),
        "review_issues": sorted(
            issues, key=lambda row: (row["issue_kind"], row["pair_id"])
        ),
        "provider_calls": 0,
    }
    return _seal(body)


def _validate_pronoun_pair(value: Any, label: str) -> dict[str, str]:
    if not isinstance(value, Mapping) or set(value) != {
        "speaker",
        "addressee",
    }:
        raise B4StoryBibleError(f"{label} must contain two pronouns")
    return {
        "speaker": _required_string(
            value.get("speaker"), f"{label} speaker"
        ),
        "addressee": _required_string(
            value.get("addressee"), f"{label} addressee"
        ),
    }


def _contains_prohibited_b4_decision_field(value: Any) -> str | None:
    if isinstance(value, dict):
        matches = sorted(set(value) & _PROHIBITED_B4_DECISION_FIELDS)
        if matches:
            return matches[0]
        for item in value.values():
            found = _contains_prohibited_b4_decision_field(item)
            if found:
                return found
    elif isinstance(value, list):
        for item in value:
            found = _contains_prohibited_b4_decision_field(item)
            if found:
                return found
    return None


def verify_b4_story_bible_v1(
    artifact: Mapping[str, Any],
    *,
    order_by_id: Mapping[str, int],
    evidence_index: Mapping[str, Any],
) -> None:
    if artifact.get("schema_version") != SCHEMA_VERSION:
        raise B4StoryBibleError("unsupported story bible schema")
    expected = artifact.get("artifact_hash")
    body = dict(artifact)
    body.pop("artifact_hash", None)
    if not isinstance(expected, str) or canonical_hash(body) != expected:
        raise B4StoryBibleError("story bible artifact hash mismatch")
    indexed = _evidence_entry_by_ref(evidence_index)
    lineage = artifact.get("lineage")
    if (
        not isinstance(lineage, dict)
        or lineage.get("evidence_index_hash") != evidence_index.get("artifact_hash")
    ):
        raise B4StoryBibleError("story bible evidence index hash mismatch")
    missing_refs = sorted(_evidence_refs_in(artifact) - set(indexed))
    if missing_refs:
        raise B4StoryBibleError(
            f"story bible contains unresolved evidence_ref: {missing_refs[0]}"
        )
    target_order = _required_int(artifact.get("chapter_order"), "chapter_order")
    required_established = []
    required_established.extend(artifact.get("entities") or [])
    required_established.extend(artifact.get("relations") or [])
    required_established.extend(artifact.get("states") or [])
    required_established.extend(artifact.get("idiolect") or [])
    narrative = artifact.get("narrative_position")
    if not isinstance(narrative, dict):
        raise B4StoryBibleError("narrative_position is missing")
    required_established.extend(narrative.get("capsules") or [])
    required_established.extend(narrative.get("frames") or [])
    if narrative.get("handoff") is not None:
        required_established.append(narrative["handoff"])
    open_questions = artifact.get("open_questions")
    if not isinstance(open_questions, dict):
        raise B4StoryBibleError("open_questions is missing")
    for rows in open_questions.values():
        required_established.extend(rows)
    for row in required_established:
        if not isinstance(row, dict):
            raise B4StoryBibleError("story bible row is malformed")
        chapter_id = _required_string(
            row.get("established_in_chapter"), "established_in_chapter"
        )
        _assert_as_of_chapter(
            chapter_id,
            order_by_id=order_by_id,
            target_order=target_order,
            label="story bible row",
        )
    relation_groups = {
        str(row["contested_group_id"])
        for row in artifact.get("relations") or []
        if row.get("structurally_contested")
    }
    open_groups = {
        str(row["contested_group_id"])
        for row in open_questions.get("contested_relations") or []
    }
    if relation_groups != open_groups:
        raise B4StoryBibleError(
            "contested relations are not mirrored in open_questions"
        )
    forbidden = _contains_prohibited_b4_decision_field(
        {
            "entities": artifact.get("entities"),
            "relations": artifact.get("relations"),
            "states": artifact.get("states"),
            "idiolect": artifact.get("idiolect"),
            "narrative_position": artifact.get("narrative_position"),
            "open_questions": artifact.get("open_questions"),
        }
    )
    if forbidden:
        raise B4StoryBibleError(
            f"B4 story bible contains a translation/address decision field: {forbidden}"
        )


def assemble_b4_story_bible_v1(
    *,
    manifest: Mapping[str, Any],
    profile: Mapping[str, Any] | None = None,
) -> B4Assembly:
    if manifest.get("schema_version") != MANIFEST_SCHEMA_VERSION:
        raise B4StoryBibleError("unsupported B4 input manifest schema")
    profile = dict(profile or load_b4_profile_v1(None))
    if profile.get("schema_version") != PROFILE_SCHEMA_VERSION:
        raise B4StoryBibleError("unsupported B4 profile schema")
    order_by_id, raw_chapters = _chapter_order_map(manifest)
    target_id = str(manifest["target_chapter_id"])
    target_order = int(manifest["target_chapter_order"])
    book_id = _required_string(manifest.get("book_id"), "book_id")
    manifest_path = Path(
        str(manifest.get("_manifest_path") or Path.cwd() / "manifest.json")
    ).resolve()
    base = manifest_path.parent
    loaded: list[LoadedInput] = []
    chapters: list[dict[str, Any]] = []
    for row in raw_chapters:
        chapter_id = str(row["chapter_id"])
        chapter_order = int(row["chapter_order"])
        registry = _load_input(
            source_id=f"registry:{chapter_id}",
            path=_resolve_manifest_path(
                base, row.get("registry_path"), "registry_path"
            ),
            label=f"{chapter_id} registry",
        )
        interaction = _load_input(
            source_id=f"interaction:{chapter_id}",
            path=_resolve_manifest_path(
                base, row.get("interaction_path"), "interaction_path"
            ),
            label=f"{chapter_id} interaction",
        )
        temporal = _load_input(
            source_id=f"temporal:{chapter_id}",
            path=_resolve_manifest_path(
                base, row.get("temporal_path"), "temporal_path"
            ),
            label=f"{chapter_id} temporal",
        )
        component_catalog = _load_input(
            source_id=f"b3_component_catalog:{chapter_id}",
            path=_resolve_manifest_path(
                base,
                row.get("component_catalog_path"),
                "component_catalog_path",
            ),
            label=f"{chapter_id} B3 component catalog",
        )
        summary = _load_input(
            source_id=f"b0_summary:{chapter_id}",
            path=_resolve_manifest_path(
                base, row.get("summary_path"), "summary_path"
            ),
            label=f"{chapter_id} B0 summary",
        )
        recovery = None
        if row.get("recovery_path"):
            recovery = _load_input(
                source_id=f"speaker_recovery:{chapter_id}",
                path=_resolve_manifest_path(
                    base, row.get("recovery_path"), "recovery_path"
                ),
                label=f"{chapter_id} speaker recovery",
            )
        for item, label in (
            (registry, "registry"),
            (interaction, "interaction"),
            (temporal, "temporal"),
            (summary, "summary"),
        ):
            _assert_artifact_chapter(item.payload, chapter_id, f"{chapter_id} {label}")
        if component_catalog.payload.get("chapter_id") != chapter_id:
            raise B4StoryBibleError("B3 component catalog chapter_id mismatch")
        if recovery and recovery.payload.get("chapter_id") != chapter_id:
            raise B4StoryBibleError("speaker recovery chapter_id mismatch")
        chapter = {
            "chapter_id": chapter_id,
            "chapter_order": chapter_order,
            "registry": registry,
            "interaction": interaction,
            "recovery": recovery,
            "temporal": temporal,
            "component_catalog": component_catalog,
            "summary": summary,
        }
        chapters.append(chapter)
        loaded.extend(
            item
            for item in (
                registry,
                interaction,
                recovery,
                temporal,
                component_catalog,
                summary,
            )
            if item is not None
        )
    target = chapters[-1]
    capsule = _load_input(
        source_id=f"capsule_log:{target_id}",
        path=_resolve_manifest_path(
            base, manifest.get("capsule_log_path"), "capsule_log_path"
        ),
        label=f"{target_id} capsule log",
    )
    window_plan = _load_input(
        source_id=f"window_plan:{target_id}",
        path=_resolve_manifest_path(
            base, manifest.get("window_plan_path"), "window_plan_path"
        ),
        label=f"{target_id} window plan",
    )
    loaded.extend((capsule, window_plan))
    if target_order == 1:
        if manifest.get("identity_projection_path"):
            raise B4StoryBibleError(
                "chapter 1 must use its own registry identity, not a later projection"
            )
        projection = _first_chapter_projection(
            target["registry"].payload, chapter_id=target_id
        )
        identity_mode = "first_chapter_registry_identity"
    else:
        identity_input = _load_input(
            source_id=f"identity_projection:{target_id}",
            path=_resolve_manifest_path(
                base,
                manifest.get("identity_projection_path"),
                "identity_projection_path",
            ),
            label=f"{target_id} identity projection",
        )
        loaded.append(identity_input)
        projection = identity_input.payload
        identity_mode = "reconciled_projection"
    _assert_declared_chapters_as_of(
        projection,
        order_by_id=order_by_id,
        target_order=target_order,
        label="identity projection",
    )
    registries = [chapter["registry"].payload for chapter in chapters]
    temporal_history = [chapter["temporal"].payload for chapter in chapters]
    component_catalogs = [
        chapter["component_catalog"].payload for chapter in chapters
    ]
    evidence_entries: dict[str, dict[str, Any]] = {}
    entities, card_to_effective, entity_by_id = _build_entities(
        projection,
        registries,
        evidence_entries=evidence_entries,
        order_by_id=order_by_id,
        target_order=target_order,
    )
    relations = _build_relations(
        registries,
        card_to_effective=card_to_effective,
        evidence_entries=evidence_entries,
        order_by_id=order_by_id,
        target_order=target_order,
    )
    referent_catalog = _build_referent_catalog(
        component_catalogs, card_to_effective=card_to_effective
    )
    states = _build_states(
        target["temporal"].payload,
        temporal_history,
        referent_catalog=referent_catalog,
        evidence_entries=evidence_entries,
        order_by_id=order_by_id,
        target_order=target_order,
    )
    turns, turn_evidence_by_id = _collect_turns(
        chapters,
        card_to_effective=card_to_effective,
        order_by_id=order_by_id,
    )
    target_card_ids = {
        _required_string(card.get("entity_id"), "target registry entity_id")
        for card in _list_of_dicts(
            target["registry"].payload.get("cards"), "target registry cards"
        )
    }
    for turn in turns:
        if turn["chapter_id"] != target_id:
            continue
        for endpoint in (turn["speaker"], turn["addressee"]):
            target_card_ids.update(endpoint.get("candidate_card_ids") or [])
    for frame in _list_of_dicts(
        target["interaction"].payload.get("frame_segments"),
        "target frame_segments",
    ):
        target_card_ids.update(
            _list_of_strings(
                frame.get("candidate_card_ids") or [],
                "target frame candidate_card_ids",
            )
        )
    pending_identity_cards = _pending_identity_card_ids(projection)
    all_pairs = _build_address_pairs(
        turns,
        relations=relations,
        entity_by_id=entity_by_id,
        pending_identity_cards=pending_identity_cards,
        order_by_id=order_by_id,
    )
    idiolect = _build_idiolect(
        turns,
        registries,
        evidence_entries=evidence_entries,
        order_by_id=order_by_id,
    )
    prior_summary = chapters[-2]["summary"].payload if target_order > 1 else None
    narrative = _build_narrative_position(
        capsule_log=capsule.payload,
        target_interaction=target["interaction"].payload,
        prior_summary=prior_summary,
        card_to_effective=card_to_effective,
        order_by_id=order_by_id,
        target_order=target_order,
    )
    open_questions, open_question_projection = _build_open_questions(
        projection=projection,
        target_temporal=target["temporal"].payload,
        relations=relations,
        turns=turns,
        turn_evidence_by_id=turn_evidence_by_id,
        evidence_entries=evidence_entries,
        order_by_id=order_by_id,
        target_order=target_order,
        target_chapter_id=target_id,
        target_card_ids=target_card_ids,
    )
    input_manifest_hash = canonical_hash(
        {
            key: value
            for key, value in manifest.items()
            if key != "_manifest_path"
        }
    )
    lineage_sources = sorted(
        (_lineage_row(item) for item in loaded),
        key=lambda row: row["source_id"],
    )
    evidence_index = _build_evidence_index(
        book_id=book_id,
        chapter_id=target_id,
        chapter_order=target_order,
        input_manifest_hash=input_manifest_hash,
        entries=evidence_entries,
        sources=lineage_sources,
    )
    source_payloads = {item.source_id: item.payload for item in loaded}
    for ref in sorted(evidence_entries):
        _resolve_evidence_ref_from_sources(
            ref,
            evidence_index=evidence_index,
            source_payloads=source_payloads,
        )
    full_lineage = {
        "identity_source_mode": identity_mode,
        "input_manifest_hash": input_manifest_hash,
        "evidence_index_hash": evidence_index["artifact_hash"],
        "sources": lineage_sources,
    }
    lineage = {
        "identity_source_mode": identity_mode,
        "input_manifest_hash": input_manifest_hash,
        "evidence_index_hash": evidence_index["artifact_hash"],
        "lineage_hash": canonical_hash(full_lineage),
    }
    stable_body = {
        "schema_version": SCHEMA_VERSION,
        "book_id": book_id,
        "chapter_id": target_id,
        "chapter_order": target_order,
        "entities": entities,
        "relations": relations,
        "states": states,
        "idiolect": idiolect,
        "narrative_position": narrative,
        "open_questions": open_questions,
        "open_question_projection": open_question_projection,
        "lineage": lineage,
        "provider_calls": 0,
    }
    budgeted, omissions, budget_report = _apply_budget(
        stable_body,
        token_budget=profile.get("b4_token_budget"),
        dormancy_chapters=_required_int(
            profile.get("memory_dormancy_chapters"),
            "memory_dormancy_chapters",
        ),
        order_by_id=order_by_id,
        target_order=target_order,
        protected_entity_ids={
            str(endpoint["effective_entity_ids"][0])
            for turn in turns
            if turn["chapter_id"] == target_id
            for endpoint in (turn["speaker"], turn["addressee"])
            if endpoint.get("resolved_to_effective_entity")
        },
        current_speaker_entity_ids={
            str(turn["speaker"]["effective_entity_ids"][0])
            for turn in turns
            if turn["chapter_id"] == target_id
            and turn["speaker"].get("resolved_to_effective_entity")
        },
    )
    stable = _seal(budgeted)
    verify_b4_story_bible_v1(
        stable,
        order_by_id=order_by_id,
        evidence_index=evidence_index,
    )
    selected_entity_by_id = {
        str(row["effective_entity_id"]): row for row in stable["entities"]
    }
    target_turns = [
        turn for turn in turns if turn["chapter_id"] == target_id
    ]
    slices = _build_window_slices(
        target_chapter_id=target_id,
        book_id=book_id,
        window_plan=window_plan.payload,
        target_turns=target_turns,
        relations=stable["relations"],
        entity_by_id=selected_entity_by_id,
        pending_identity_cards=pending_identity_cards,
        order_by_id=order_by_id,
        lineage_hash=lineage["lineage_hash"],
        source_b2_hash=target["interaction"].payload["artifact_hash"],
        source_recovery_hash=(
            target["recovery"].payload["artifact_hash"]
            if target.get("recovery")
            else None
        ),
    )
    ui_view = _build_ui_view(stable=stable)
    anchor_input = _build_anchor_input(
        stable=stable,
        all_pairs=all_pairs,
        target_chapter_id=target_id,
        entity_by_id=selected_entity_by_id,
    )
    glossary_count = sum(
        len(row["glossary_terms_in_own_speech"]) for row in stable["idiolect"]
    )
    report_body = {
        "schema_version": REPORT_SCHEMA_VERSION,
        "book_id": book_id,
        "chapter_id": target_id,
        "chapter_order": target_order,
        "story_bible_artifact_hash": stable["artifact_hash"],
        "ui_artifact_hash": ui_view["artifact_hash"],
        "evidence_index_artifact_hash": evidence_index["artifact_hash"],
        "address_anchor_input_artifact_hash": anchor_input["artifact_hash"],
        "window_artifact_hashes": [
            row["artifact_hash"] for row in slices
        ],
        "metrics": {
            "estimated_story_bible_tokens": _estimated_tokens(stable),
            "section_estimated_tokens": {
                key: _estimated_tokens(stable[key])
                for key in (
                    "entities",
                    "relations",
                    "states",
                    "idiolect",
                    "narrative_position",
                    "open_questions",
                    "open_question_projection",
                    "lineage",
                    "memory_budget",
                )
            },
            "story_bible_bytes": len(
                (json.dumps(stable, ensure_ascii=False, indent=2) + "\n").encode(
                    "utf-8"
                )
            ),
            "ui_graph_bytes": len(
                (canonical_json(ui_view) + "\n").encode("utf-8")
            ),
            "evidence_index_entries": len(evidence_index["entries"]),
            "entities": len(stable["entities"]),
            "relations": len(stable["relations"]),
            "states": len(stable["states"]),
            "address_pairs": len(all_pairs),
            "anchorable_pairs": len(anchor_input["pairs"]),
            "glossary_terms_attributed": glossary_count,
            "open_questions": {
                key: len(value) for key, value in stable["open_questions"].items()
            },
            "windows": len(slices),
            "omissions": len(omissions),
        },
        "memory_budget_omissions": omissions,
        "provider_calls": 0,
    }
    report = _seal(report_body, "report_hash")
    return B4Assembly(
        stable=stable,
        window_slices=slices,
        ui_view=ui_view,
        evidence_index=evidence_index,
        address_anchor_input=anchor_input,
        report=report,
    )


def write_b4_assembly_v1(assembly: B4Assembly, *, out_dir: Path) -> list[Path]:
    root = Path(out_dir)
    if root.exists():
        raise B4StoryBibleError(f"output directory already exists: {root}")
    root.mkdir(parents=True, exist_ok=False)
    chapter_id = str(assembly.stable["chapter_id"])
    suffix = chapter_id.rsplit("_", 1)[-1]
    outputs: list[tuple[Path, Mapping[str, Any]]] = [
        (root / f"story_bible_as_of_{suffix}.json", assembly.stable),
        (root / f"story_graph_as_of_{suffix}.json", assembly.ui_view),
        (
            root / f"evidence_index_as_of_{suffix}.json",
            assembly.evidence_index,
        ),
        (
            root / f"address_anchor_input_{suffix}.json",
            assembly.address_anchor_input,
        ),
        (root / "assembly_report.json", assembly.report),
    ]
    for window in assembly.window_slices:
        window_order = int(window["window_order"])
        outputs.append(
            (
                root / f"window_slice_{suffix}_w{window_order:02d}.json",
                window,
            )
        )
    written: list[Path] = []
    for path, payload in outputs:
        serialized = (
            canonical_json(payload)
            if payload is assembly.ui_view
            else json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True)
        )
        path.write_text(
            serialized + "\n",
            encoding="utf-8",
        )
        written.append(path)
    return written


__all__ = [
    "ANCHOR_INPUT_SCHEMA_VERSION",
    "ANCHOR_OUTPUT_SCHEMA_VERSION",
    "EVIDENCE_INDEX_SCHEMA_VERSION",
    "B4Assembly",
    "B4StoryBibleError",
    "MANIFEST_SCHEMA_VERSION",
    "PROFILE_SCHEMA_VERSION",
    "SCHEMA_VERSION",
    "assemble_b4_story_bible_v1",
    "load_b4_input_manifest_v1",
    "load_b4_profile_v1",
    "resolve_b4_evidence_ref_v1",
    "validate_address_anchor_output_v1",
    "verify_b4_story_bible_v1",
    "write_b4_assembly_v1",
]
