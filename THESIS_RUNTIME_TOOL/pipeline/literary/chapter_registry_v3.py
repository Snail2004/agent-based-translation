from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
import hashlib
import json
import os
from pathlib import Path
import re
import unicodedata
from typing import Any, Callable, Iterable, Mapping, Sequence

from pipeline.literary.builder_pilot import load_system_prompt_from_design
from pipeline.literary.checkpoint import (
    CheckpointLock,
    canonical_hash,
    canonical_json,
    write_checkpoint_atomic,
)
from pipeline.literary.chapter_registry_schema_v3 import (
    ALIAS_SCOPE_POLICY_VERSION,
    AUDIT_ACTIONS,
    AUDIT_SCHEMA_VERSION,
    B2_RESCAN_POLICY_VERSION,
    CANDIDATE_POLICY_VERSION,
    CHECKLIST_CLASSES,
    CODE_TICKET_TYPES,
    DELTA_SCHEMA_VERSION,
    GLOSSARY_CATEGORIES,
    MODEL_TICKET_TYPES,
    NAME_CLASSES,
    ORIENTATION_SCHEMA_VERSION,
    PROMPT_IDS,
    PreparedRegistryGenerationV3,
    REFERENT_KINDS,
    REGISTRY_SCHEMA_VERSION,
    RegistryBudgetError,
    RegistryContractError,
    RegistryStaleParentError,
    RegistryStaleRevisionError,
    RegistryStoreError,
    RenderedRegistryRequestV3,
    RunConfigV3,
    TICKET_TYPES,
    VALIDATOR_VERSION,
    response_json_schema,
)


_ALIAS_GATE_OUTCOMES = frozenset(
    {"eligible_global_alias", "defer_to_b2", "pending_scope_review"}
)
_CONTEXTUAL_NORMALIZED = frozenset(
    {
        "i",
        "me",
        "my",
        "mine",
        "myself",
        "you",
        "your",
        "yours",
        "yourself",
        "he",
        "him",
        "his",
        "himself",
        "she",
        "her",
        "hers",
        "herself",
        "it",
        "its",
        "itself",
        "we",
        "us",
        "our",
        "ours",
        "ourselves",
        "they",
        "them",
        "their",
        "theirs",
        "themselves",
    }
)
_AUDIT_ALLOWED_ACTIONS: Mapping[str, frozenset[str]] = {
    "same_name_collision": frozenset(
        {"confirm_distinct_entity", "merge_as_alias", "remain_pending"}
    ),
    "possible_alias": frozenset(
        {"confirm_distinct_entity", "merge_as_alias", "remain_pending"}
    ),
    "important_unnamed_referent": frozenset(
        {"create_unnamed_entity", "reject_noise", "remain_pending"}
    ),
    "kind_conflict": frozenset(
        {"revise_profile", "confirm_distinct_entity", "remain_pending"}
    ),
    "profile_conflict": frozenset(
        {"revise_profile", "confirm_distinct_entity", "remain_pending"}
    ),
    "importance_review": frozenset(
        {
            "confirm_distinct_entity",
            "confirm_distinct_glossary",
            "reject_noise",
            "remain_pending",
        }
    ),
    "surface_class_review": frozenset(
        {"promote_global_alias", "defer_to_b2", "reject_noise", "remain_pending"}
    ),
    "glossary_collision": frozenset(
        {"confirm_distinct_glossary", "merge_glossary", "reject_noise", "remain_pending"}
    ),
    "candidate_overflow": frozenset({"remain_pending"}),
    "unlocatable_surface": frozenset({"reject_noise", "remain_pending"}),
    "missing_salient_surface": frozenset({"remain_pending"}),
    "alias_scope_review": frozenset(
        {"confirm_distinct_entity", "defer_to_b2", "reject_noise", "remain_pending"}
    ),
}


def _clone(value: Any) -> Any:
    return json.loads(canonical_json(value))


def _nfc(value: Any) -> str:
    return unicodedata.normalize("NFC", str(value or ""))


def _required_str(value: Any, label: str) -> str:
    text = _nfc(value).strip()
    if not text:
        raise RegistryContractError(f"{label} must be a non-empty string")
    return text


def _optional_str(value: Any, label: str) -> str | None:
    return None if value is None else _required_str(value, label)


def _require_exact_keys(value: Mapping[str, Any], expected: set[str], label: str) -> None:
    if set(value) != expected:
        raise RegistryContractError(
            f"{label} fields mismatch: expected {sorted(expected)}, got {sorted(value)}"
        )


def _require_list(value: Any, label: str) -> list[Any]:
    if not isinstance(value, list):
        raise RegistryContractError(f"{label} must be a list")
    return value


def _require_enum(value: Any, allowed: Iterable[str], label: str) -> str:
    text = _required_str(value, label)
    if text not in set(allowed):
        raise RegistryContractError(f"{label} has unsupported value: {text}")
    return text


def _require_string_list(value: Any, label: str, *, allow_empty: bool = False) -> list[str]:
    rows = _require_list(value, label)
    result = [_required_str(item, label) for item in rows]
    if not allow_empty and not result:
        raise RegistryContractError(f"{label} must not be empty")
    if len(result) != len(set(result)):
        raise RegistryContractError(f"{label} contains duplicates")
    return result


def _block_text(block: Mapping[str, Any]) -> str:
    return _nfc(block.get("clean_text") or block.get("source_text") or block.get("text") or "")


def _block_view(block: Mapping[str, Any], *, context_only: bool = False) -> dict[str, Any]:
    row = {
        "block_id": _required_str(block.get("block_id"), "block_id"),
        "order_index": int(block.get("order_index") or 0),
        "block_type": str(block.get("block_type") or ""),
        "text": _block_text(block),
    }
    if context_only:
        row["context_only"] = True
        row["direction"] = "previous"
    return row


def _chapter_blocks(chapter: Mapping[str, Any]) -> list[dict[str, Any]]:
    rows = [dict(row) for row in chapter.get("blocks") or [] if row.get("block_id")]
    rows.sort(key=lambda row: (int(row.get("order_index") or 0), str(row["block_id"])))
    if len(rows) != len({str(row["block_id"]) for row in rows}):
        raise RegistryContractError("chapter contains duplicate block ids")
    return rows


def chapter_source_manifest_hash(chapter: Mapping[str, Any]) -> str:
    return canonical_hash([_block_view(row) for row in _chapter_blocks(chapter)])


def _normalized_literal(value: Any) -> str:
    text = _nfc(value).casefold()
    text = re.sub(r"[^\w]+", " ", text, flags=re.UNICODE)
    return " ".join(text.split())


def _all_exact_spans(text: str, surface: str) -> list[dict[str, int]]:
    spans: list[dict[str, int]] = []
    start = 0
    while True:
        at = text.find(surface, start)
        if at < 0:
            return spans
        spans.append({"char_start": at, "char_end": at + len(surface)})
        start = at + max(1, len(surface))


def _bounded_search(text: str, surface: str, *, ignore_case: bool) -> re.Match[str] | None:
    if not surface:
        return None
    prefix = r"(?<!\w)" if re.match(r"\w", surface[0], flags=re.UNICODE) else ""
    suffix = r"(?!\w)" if re.match(r"\w", surface[-1], flags=re.UNICODE) else ""
    flags = re.IGNORECASE | re.UNICODE if ignore_case else re.UNICODE
    return re.search(prefix + re.escape(surface) + suffix, text, flags=flags)


def _match_known_surface(text: str, known_surface: str) -> tuple[str, str] | None:
    exact = _bounded_search(text, known_surface, ignore_case=False)
    if exact is not None:
        return "exact", exact.group(0)
    folded = _bounded_search(text, known_surface, ignore_case=True)
    if folded is not None:
        return "normalized", folded.group(0)
    known_tokens = [token for token in _normalized_literal(known_surface).split() if len(token) >= 3]
    if not known_tokens:
        return None
    title_tokens = {"mr", "mrs", "ms", "miss", "sir", "lady", "lord", "dr"}
    non_title = [token for token in known_tokens if token not in title_tokens]
    for token in non_title:
        match = re.search(rf"(?<!\w){re.escape(token)}(?!\w)", text, flags=re.I | re.UNICODE)
        if match is not None:
            kind = "title_surname" if len(non_title) < len(known_tokens) else "token_overlap"
            return kind, match.group(0)
    return None


def estimate_registry_prompt_tokens(messages: Sequence[Mapping[str, Any]]) -> int:
    return max(1, len(canonical_json({"messages": list(messages)})) // 4)


def _validate_run_config_contract(run_config: RunConfigV3) -> None:
    if run_config.validator_version != VALIDATOR_VERSION:
        raise RegistryContractError("validator contract mismatch")
    if dict(run_config.prompt_versions) != PROMPT_IDS:
        raise RegistryContractError("prompt version contract mismatch")
    expected_schemas = {
        "registry": REGISTRY_SCHEMA_VERSION,
        "b0": ORIENTATION_SCHEMA_VERSION,
        "b1": DELTA_SCHEMA_VERSION,
        "auditor": AUDIT_SCHEMA_VERSION,
    }
    if dict(run_config.schema_versions) != expected_schemas:
        raise RegistryContractError("schema version contract mismatch")
    expected_policies = {
        "candidate_selection": CANDIDATE_POLICY_VERSION,
        "alias_scope": ALIAS_SCOPE_POLICY_VERSION,
        "b2_rescan": B2_RESCAN_POLICY_VERSION,
    }
    if dict(run_config.policy_versions) != expected_policies:
        raise RegistryContractError("policy version contract mismatch")


def empty_registry_snapshot_v3(state_lineage_id: str) -> dict[str, Any]:
    body = {
        "builder_identity_mode": REGISTRY_SCHEMA_VERSION,
        "schema_version": REGISTRY_SCHEMA_VERSION,
        "state_lineage_id": _required_str(state_lineage_id, "state_lineage_id"),
        "generation_id": None,
        "entities": [],
        "aliases": [],
        "glossary_items": [],
        "tickets": [],
    }
    body["snapshot_hash"] = canonical_hash(body)
    return body


def _snapshot_revision(snapshot: Mapping[str, Any], chapter_id: str, applied: Sequence[str]) -> str:
    return "work3_" + canonical_hash(
        {
            "chapter_id": chapter_id,
            "parent_snapshot_hash": snapshot.get("snapshot_hash"),
            "entities": snapshot.get("entities") or [],
            "aliases": snapshot.get("aliases") or [],
            "glossary_items": snapshot.get("glossary_items") or [],
            "tickets": snapshot.get("tickets") or [],
            "applied_request_fingerprints": list(applied),
        }
    )[:24]


def _render_request(
    *,
    role: str,
    chapter_id: str,
    window_id: str | None,
    parent_working_revision_hash: str | None,
    sections: Mapping[str, Any],
    design_doc: Path,
    run_config: RunConfigV3,
) -> RenderedRegistryRequestV3:
    _validate_run_config_contract(run_config)
    prompt_id = PROMPT_IDS[role]
    prompt = load_system_prompt_from_design(design_doc, prompt_id)
    prompt_sha = hashlib.sha256(prompt.encode("utf-8")).hexdigest()
    schema = response_json_schema(role)
    schema_hash = canonical_hash(schema)
    payload = {
        "schema_version": REGISTRY_SCHEMA_VERSION,
        "role": role,
        "chapter_id": chapter_id,
        "window_id": window_id,
        "working_registry_revision_hash": parent_working_revision_hash,
        "allowlisted_sections": _clone(sections),
    }
    messages = (
        {"role": "system", "content": prompt},
        {"role": "user", "content": canonical_json(payload)},
    )
    model_contract = {
        "model_id": getattr(run_config, f"{role}_model_id"),
        "reasoning_effort": getattr(run_config, f"{role}_reasoning_effort"),
        "temperature": getattr(run_config, f"{role}_temperature"),
        "seed": getattr(run_config, f"{role}_seed"),
        "max_output_tokens": getattr(run_config, f"{role}_output_cap"),
    }
    fingerprint = canonical_hash(
        {
            "role": role,
            "chapter_id": chapter_id,
            "window_id": window_id,
            "prompt_id": prompt_id,
            "prompt_sha256": prompt_sha,
            "response_schema_hash": schema_hash,
            "model_contract": model_contract,
            "parent_working_revision_hash": parent_working_revision_hash,
            "sections_hash": canonical_hash(sections),
            "run_config_hash": run_config.config_hash,
        }
    )
    return RenderedRegistryRequestV3(
        role=role,
        prompt_id=prompt_id,
        prompt_sha256=prompt_sha,
        response_schema_hash=schema_hash,
        chapter_id=chapter_id,
        window_id=window_id,
        parent_working_revision_hash=parent_working_revision_hash,
        sections=_clone(sections),
        messages=messages,
        request_fingerprint=fingerprint,
    )


def render_b0_request(
    *, chapter: Mapping[str, Any], design_doc: Path, run_config: RunConfigV3
) -> RenderedRegistryRequestV3:
    chapter_id = _required_str(chapter.get("chapter_id"), "chapter_id")
    request = _render_request(
        role="b0",
        chapter_id=chapter_id,
        window_id=None,
        parent_working_revision_hash=None,
        sections={"source_blocks": [_block_view(row) for row in _chapter_blocks(chapter)]},
        design_doc=design_doc,
        run_config=run_config,
    )
    tokens = estimate_registry_prompt_tokens(request.messages)
    if tokens > run_config.b0_input_cap:
        raise RegistryBudgetError(f"B0 input {tokens} exceeds cap {run_config.b0_input_cap}")
    return request


def validate_orientation_response(
    response: Mapping[str, Any], chapter: Mapping[str, Any]
) -> dict[str, Any]:
    _require_exact_keys(
        response,
        {"gist", "narrator_hypotheses", "salient_registry_checklist"},
        "ChapterOrientationV3",
    )
    catalog = {str(row["block_id"]): _block_text(row) for row in _chapter_blocks(chapter)}
    hypotheses: list[dict[str, Any]] = []
    for raw in _require_list(response.get("narrator_hypotheses"), "narrator_hypotheses"):
        if not isinstance(raw, Mapping):
            raise RegistryContractError("narrator hypothesis must be an object")
        _require_exact_keys(raw, {"surface", "note", "block_ids"}, "narrator hypothesis")
        block_ids = _require_string_list(raw.get("block_ids"), "narrator block_ids")
        if not set(block_ids) <= set(catalog):
            raise RegistryContractError("narrator hypothesis cites foreign block")
        hypotheses.append(
            {
                "surface": _optional_str(raw.get("surface"), "narrator surface"),
                "note": _required_str(raw.get("note"), "narrator note"),
                "block_ids": block_ids,
            }
        )
    checklist: list[dict[str, Any]] = []
    code_tickets: list[dict[str, Any]] = []
    chapter_id = _required_str(chapter.get("chapter_id"), "chapter_id")
    for raw in _require_list(response.get("salient_registry_checklist"), "checklist"):
        if not isinstance(raw, Mapping):
            raise RegistryContractError("checklist row must be an object")
        _require_exact_keys(
            raw,
            {"surface", "block_id", "checklist_class", "importance_note"},
            "checklist row",
        )
        surface = _required_str(raw.get("surface"), "checklist surface")
        block_id = _required_str(raw.get("block_id"), "checklist block_id")
        if block_id not in catalog:
            raise RegistryContractError("checklist row cites foreign block")
        row = {
            "checklist_id": "check3_"
            + canonical_hash(
                {
                    "chapter_id": chapter_id,
                    "surface": surface,
                    "block_id": block_id,
                    "checklist_class": raw.get("checklist_class"),
                }
            )[:20],
            "surface": surface,
            "block_id": block_id,
            "checklist_class": _require_enum(
                raw.get("checklist_class"), CHECKLIST_CLASSES, "checklist class"
            ),
            "importance_note": _required_str(raw.get("importance_note"), "importance note"),
        }
        checklist.append(row)
        if not _all_exact_spans(catalog[block_id], surface):
            code_tickets.append(
                {
                    "ticket_type": "unlocatable_surface",
                    "surface": surface,
                    "source_block_ids": [block_id],
                    "reason": "B0 checklist surface is absent from its declared block",
                }
            )
    return {
        "gist": _required_str(response.get("gist"), "orientation gist"),
        "narrator_hypotheses": hypotheses,
        "salient_registry_checklist": checklist,
        "code_ticket_proposals": code_tickets,
    }


def build_registry_windows(
    chapter: Mapping[str, Any], *, target_tokens: int, max_blocks: int, preceding_tail_k: int
) -> list[dict[str, Any]]:
    ordered = _chapter_blocks(chapter)
    active = [
        row
        for row in ordered
        if str(row.get("block_type") or "").casefold() not in {"heading", "chapter_heading"}
    ]
    if not active:
        return []
    windows: list[list[dict[str, Any]]] = []
    current: list[dict[str, Any]] = []
    tokens = 0
    for block in active:
        block_tokens = max(1, len(_block_text(block)) // 4)
        if current and (tokens + block_tokens > target_tokens or len(current) >= max_blocks):
            windows.append(current)
            current = []
            tokens = 0
        if block_tokens > target_tokens:
            windows.append([block])
            continue
        current.append(block)
        tokens += block_tokens
    if current:
        windows.append(current)
    order_by_id = {str(row["block_id"]): index for index, row in enumerate(ordered)}
    chapter_id = _required_str(chapter.get("chapter_id"), "chapter_id")
    result: list[dict[str, Any]] = []
    covered: list[str] = []
    for index, rows in enumerate(windows, 1):
        first = order_by_id[str(rows[0]["block_id"])]
        tail = ordered[max(0, first - preceding_tail_k) : first]
        result.append(
            {
                "window_id": f"w3_{chapter_id}_{index:02d}",
                "chapter_id": chapter_id,
                "blocks": _clone(rows),
                "context_only_tail": _clone(tail),
                "estimated_source_tokens": sum(max(1, len(_block_text(row)) // 4) for row in rows),
            }
        )
        covered.extend(str(row["block_id"]) for row in rows)
    expected = [str(row["block_id"]) for row in active]
    if covered != expected or len(covered) != len(set(covered)):
        raise RegistryContractError("B1 windows do not exact-cover non-heading blocks")
    return result


def _entity_card(entity: Mapping[str, Any], aliases: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    names = [str(entity["canonical_surface"])] + [
        str(row["surface"])
        for row in aliases
        if row.get("entity_id") == entity.get("entity_id") and row.get("status") in {"confirmed", "pending"}
    ]
    return {
        "entity_id": str(entity["entity_id"]),
        "canonical_surface": str(entity["canonical_surface"]),
        "referent_kind": str(entity["referent_kind"]),
        "name_forms": sorted(set(names), key=lambda value: (_normalized_literal(value), value)),
        "identity_summary": str(entity["identity_summary"]),
        "status": str(entity["status"]),
    }


def _glossary_card(item: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "glossary_id": str(item["glossary_id"]),
        "surface": str(item["surface"]),
        "category_claim": str(item["category_claim"]),
        "short_description": str(item["short_description"]),
        "status": str(item["status"]),
    }


def select_candidate_packets(
    *,
    snapshot: Mapping[str, Any],
    working_revision_hash: str,
    active_blocks: Sequence[Mapping[str, Any]],
    context_only_tail: Sequence[Mapping[str, Any]],
    block_order: Mapping[str, int],
    recency_k: int,
    card_count_cap: int,
    card_token_cap: int,
    packet_count_cap: int,
) -> dict[str, Any]:
    searchable = [_block_view(row) for row in active_blocks]
    context_tail_ids = [str(row["block_id"]) for row in context_only_tail]
    aliases = list(snapshot.get("aliases") or [])
    entities = {str(row["entity_id"]): dict(row) for row in snapshot.get("entities") or []}
    glossary = {
        str(row["glossary_id"]): dict(row) for row in snapshot.get("glossary_items") or []
    }
    cards: dict[tuple[str, str], dict[str, Any]] = {}
    links: dict[str, dict[str, Any]] = {}

    def add_match(
        *, source_surface: str, block_id: str, match_kind: str, row_type: str, row_id: str, card: dict[str, Any]
    ) -> None:
        normalized = _normalized_literal(source_surface)
        if not normalized:
            return
        packet = links.setdefault(
            normalized,
            {
                "source_surfaces": set(),
                "source_block_ids": set(),
                "match_kinds": set(),
                "candidate_keys": set(),
            },
        )
        packet["source_surfaces"].add(source_surface)
        packet["source_block_ids"].add(block_id)
        packet["match_kinds"].add(match_kind)
        packet["candidate_keys"].add((row_type, row_id))
        cards[(row_type, row_id)] = card

    for entity_id, entity in entities.items():
        card = _entity_card(entity, aliases)
        for surface in card["name_forms"]:
            for block in searchable:
                match = _match_known_surface(block["text"], surface)
                if match is not None:
                    add_match(
                        source_surface=match[1],
                        block_id=block["block_id"],
                        match_kind=match[0],
                        row_type="entity",
                        row_id=entity_id,
                        card=card,
                    )
    for glossary_id, item in glossary.items():
        card = _glossary_card(item)
        for block in searchable:
            match = _match_known_surface(block["text"], card["surface"])
            if match is not None:
                add_match(
                    source_surface=match[1],
                    block_id=block["block_id"],
                    match_kind=match[0],
                    row_type="glossary",
                    row_id=glossary_id,
                    card=card,
                )

    first_order = min((int(row["order_index"]) for row in searchable if not row.get("context_only")), default=0)
    recency_keys: list[tuple[str, str]] = []
    if recency_k:
        lower = first_order - recency_k
        for entity_id, entity in entities.items():
            support = list(entity.get("created_from_block_ids") or []) + list(entity.get("support_block_ids") or [])
            if any(lower <= int(block_order.get(block_id, -10**9)) < first_order for block_id in support):
                recency_keys.append(("entity", entity_id))

    matched_keys = {key for row in links.values() for key in row["candidate_keys"]}
    ordered_keys = sorted(matched_keys) + sorted(set(recency_keys) - matched_keys)
    selected: set[tuple[str, str]] = set()
    used_tokens = 0
    for key in ordered_keys:
        card = cards.get(key)
        if card is None:
            card = _entity_card(entities[key[1]], aliases)
            cards[key] = card
        row_tokens = max(1, len(canonical_json(card)) // 4)
        if len(selected) >= card_count_cap or used_tokens + row_tokens > card_token_cap:
            continue
        selected.add(key)
        used_tokens += row_tokens

    packet_rows: list[dict[str, Any]] = []
    overflow_norms: set[str] = set()
    for normalized in sorted(links):
        body = links[normalized]
        candidate_keys = sorted(body["candidate_keys"])
        included = [key for key in candidate_keys if key in selected]
        overflow = len(included) != len(candidate_keys)
        if overflow:
            overflow_norms.add(normalized)
        source_surface = sorted(body["source_surfaces"], key=lambda value: (len(value), value))[0]
        packet_rows.append(
            {
                "source_surface": source_surface,
                "source_block_ids": sorted(body["source_block_ids"], key=lambda value: (block_order.get(value, 10**9), value)),
                "match_kinds": sorted(body["match_kinds"]),
                "candidate_entities": [cards[key] for key in included if key[0] == "entity"],
                "candidate_glossary_items": [cards[key] for key in included if key[0] == "glossary"],
                "candidate_overflow": overflow,
            }
        )
    if len(packet_rows) > packet_count_cap:
        for row in packet_rows[packet_count_cap:]:
            overflow_norms.add(_normalized_literal(row["source_surface"]))
        packet_rows = packet_rows[:packet_count_cap]

    unmatched_recency = [
        {"registry_row_type": key[0], "candidate_card": cards[key], "row_hash": canonical_hash(cards[key])}
        for key in sorted(selected - matched_keys)
    ]
    manifest_body = {
        "policy_version": CANDIDATE_POLICY_VERSION,
        "lexical_match_scope": "active_blocks_only",
        "context_only_tail_block_ids": context_tail_ids,
        "working_registry_revision_hash": working_revision_hash,
        "selected_card_hashes": sorted(canonical_hash(cards[key]) for key in selected),
        "selected_count": len(selected),
        "selected_token_estimate": used_tokens,
        "card_count_cap": card_count_cap,
        "card_token_cap": card_token_cap,
        "packet_count_cap": packet_count_cap,
        "packet_hashes": [canonical_hash(row) for row in packet_rows],
        "packet_count": len(packet_rows),
        "overflowed_normalized_surfaces": sorted(overflow_norms),
        "prejoined_context_bytes": len(
            canonical_json(
                {"surface_candidate_packets": packet_rows, "unmatched_recency_cards": unmatched_recency}
            )
        ),
    }
    manifest = {**manifest_body, "manifest_hash": canonical_hash(manifest_body)}
    relevant_tickets = [
        _clone(ticket)
        for ticket in snapshot.get("tickets") or []
        if ticket.get("status") != "resolved"
        and any(
            ticket.get("surface")
            and _match_known_surface(block["text"], str(ticket["surface"])) is not None
            for block in searchable
        )
    ]
    return {
        "surface_candidate_packets": packet_rows,
        "unmatched_recency_cards": unmatched_recency,
        "relevant_open_tickets": sorted(relevant_tickets, key=lambda row: str(row["ticket_id"])),
        "candidate_selection_manifest": manifest,
    }


def render_b1_request(
    *,
    chapter_id: str,
    window_id: str,
    orientation: Mapping[str, Any],
    active_blocks: Sequence[Mapping[str, Any]],
    context_only_tail: Sequence[Mapping[str, Any]],
    working: "ChapterWorkingRegistryV3",
    block_order: Mapping[str, int],
    design_doc: Path,
    run_config: RunConfigV3,
    targeted_checklist_rows: Sequence[Mapping[str, Any]] | None = None,
) -> RenderedRegistryRequestV3:
    selection = select_candidate_packets(
        snapshot=working.snapshot(),
        working_revision_hash=working.revision_hash,
        active_blocks=active_blocks,
        context_only_tail=context_only_tail,
        block_order=block_order,
        recency_k=run_config.recency_k,
        card_count_cap=run_config.candidate_card_count_cap,
        card_token_cap=run_config.candidate_card_token_cap,
        packet_count_cap=run_config.candidate_packet_count_cap,
    )
    active_ids = {str(row["block_id"]) for row in active_blocks}
    checklist = [
        _clone(row)
        for row in orientation.get("salient_registry_checklist") or []
        if str(row.get("block_id")) in active_ids
    ]
    if targeted_checklist_rows is not None:
        checklist = _clone(list(targeted_checklist_rows))
        if not checklist:
            raise RegistryContractError("targeted B1 request requires at least one checklist row")
        if any(str(row.get("block_id")) not in active_ids for row in checklist):
            raise RegistryContractError("targeted checklist row falls outside active window")
    sections: dict[str, Any] = {
        "b0_gist": _required_str(orientation.get("gist"), "B0 gist"),
        "b0_checklist_rows_for_active_blocks": checklist,
        "active_window_blocks": [_block_view(row) for row in active_blocks],
        "context_only_preceding_tail": [_block_view(row, context_only=True) for row in context_only_tail],
        "working_registry_revision_hash": working.revision_hash,
        **selection,
        "cap_manifest": {
            "candidate_card_count_cap": run_config.candidate_card_count_cap,
            "candidate_card_token_cap": run_config.candidate_card_token_cap,
            "candidate_packet_count_cap": run_config.candidate_packet_count_cap,
        },
    }
    if targeted_checklist_rows is not None:
        sections["targeted_checklist_rows"] = _clone(list(targeted_checklist_rows))
    request = _render_request(
        role="b1",
        chapter_id=chapter_id,
        window_id=window_id,
        parent_working_revision_hash=working.revision_hash,
        sections=sections,
        design_doc=design_doc,
        run_config=run_config,
    )
    tokens = estimate_registry_prompt_tokens(request.messages)
    if tokens > run_config.b1_input_cap:
        raise RegistryBudgetError(f"B1 input {tokens} exceeds cap {run_config.b1_input_cap}")
    return request


def _row_with_revision(payload: Mapping[str, Any]) -> dict[str, Any]:
    row = _clone(payload)
    row.pop("revision_hash", None)
    row["revision_hash"] = canonical_hash(row)
    return row


def _mint_id(prefix: str, key: Mapping[str, Any]) -> str:
    return prefix + canonical_hash(key)[:20]


def _decode_candidate_universe(
    request: RenderedRegistryRequestV3,
) -> tuple[set[str], set[str], set[str]]:
    manifest = request.sections.get("candidate_selection_manifest")
    if not isinstance(manifest, Mapping):
        raise RegistryContractError("candidate selection manifest must be an object")
    body = dict(manifest)
    own_hash = body.pop("manifest_hash", None)
    if own_hash != canonical_hash(body):
        raise RegistryContractError("candidate selection manifest hash mismatch")
    if body.get("policy_version") != CANDIDATE_POLICY_VERSION:
        raise RegistryContractError("candidate selection policy mismatch")
    if body.get("working_registry_revision_hash") != request.parent_working_revision_hash:
        raise RegistryContractError("candidate selection revision drift")
    packets = _require_list(
        request.sections.get("surface_candidate_packets"), "surface_candidate_packets"
    )
    if body.get("packet_hashes") != [canonical_hash(row) for row in packets]:
        raise RegistryContractError("candidate packet hashes mismatch")
    if body.get("packet_count") != len(packets):
        raise RegistryContractError("candidate packet count mismatch")
    entity_ids: set[str] = set()
    glossary_ids: set[str] = set()
    overflow: set[str] = set()
    seen: set[str] = set()
    for raw in packets:
        if not isinstance(raw, Mapping):
            raise RegistryContractError("candidate packet must be an object")
        _require_exact_keys(
            raw,
            {
                "source_surface",
                "source_block_ids",
                "match_kinds",
                "candidate_entities",
                "candidate_glossary_items",
                "candidate_overflow",
            },
            "candidate packet",
        )
        surface = _required_str(raw.get("source_surface"), "candidate source surface")
        normalized = _normalized_literal(surface)
        if normalized in seen:
            raise RegistryContractError("duplicate normalized source-surface packet")
        seen.add(normalized)
        _require_string_list(raw.get("source_block_ids"), "candidate source blocks")
        kinds = _require_string_list(raw.get("match_kinds"), "candidate match kinds")
        if not set(kinds) <= {"exact", "normalized", "title_surname", "token_overlap"}:
            raise RegistryContractError("candidate packet has unsupported match kind")
        if not isinstance(raw.get("candidate_overflow"), bool):
            raise RegistryContractError("candidate_overflow must be boolean")
        if raw["candidate_overflow"]:
            overflow.add(normalized)
        for entity in _require_list(raw.get("candidate_entities"), "candidate entities"):
            if not isinstance(entity, Mapping):
                raise RegistryContractError("candidate entity must be an object")
            _require_exact_keys(
                entity,
                {
                    "entity_id",
                    "canonical_surface",
                    "referent_kind",
                    "name_forms",
                    "identity_summary",
                    "status",
                },
                "candidate entity",
            )
            entity_ids.add(_required_str(entity.get("entity_id"), "candidate entity id"))
            _require_enum(entity.get("referent_kind"), REFERENT_KINDS, "candidate kind")
            _require_string_list(entity.get("name_forms"), "candidate name forms")
        for item in _require_list(raw.get("candidate_glossary_items"), "candidate glossary"):
            if not isinstance(item, Mapping):
                raise RegistryContractError("candidate glossary item must be an object")
            glossary_ids.add(_required_str(item.get("glossary_id"), "candidate glossary id"))
    return entity_ids, glossary_ids, overflow


def _located_support(
    *, surface: str, block_ids: Sequence[str], active_catalog: Mapping[str, str]
) -> list[dict[str, Any]]:
    if not set(block_ids) <= set(active_catalog):
        raise RegistryContractError("semantic row cites a foreign or context-only block")
    spans: list[dict[str, Any]] = []
    for block_id in block_ids:
        located = _all_exact_spans(active_catalog[block_id], surface)
        if not located:
            return []
        spans.extend({"block_id": block_id, **row} for row in located)
    return spans


@dataclass
class ChapterWorkingRegistryV3:
    state_lineage_id: str
    chapter_id: str
    source_manifest_hash: str
    parent_generation_id: str | None
    parent_snapshot: Mapping[str, Any]
    _state: dict[str, Any] = field(init=False)
    revision_hash: str = field(init=False)
    applied_request_fingerprints: list[str] = field(default_factory=list)
    targeted_recall_request_fingerprints: list[str] = field(default_factory=list)
    candidate_manifest_hashes: list[str] = field(default_factory=list)
    auditor_request_fingerprints: list[str] = field(default_factory=list)
    application_records: list[dict[str, Any]] = field(default_factory=list)
    alias_gate_records: list[dict[str, Any]] = field(default_factory=list)
    clean_entity_ids: set[str] = field(default_factory=set)
    clean_glossary_ids: set[str] = field(default_factory=set)
    _replay: dict[str, tuple[str, dict[str, Any]]] = field(default_factory=dict)
    _created_ids: dict[str, set[str]] = field(
        default_factory=lambda: {
            "entities": set(),
            "aliases": set(),
            "glossary_items": set(),
            "tickets": set(),
        }
    )

    def __post_init__(self) -> None:
        self.state_lineage_id = _required_str(self.state_lineage_id, "state_lineage_id")
        self.chapter_id = _required_str(self.chapter_id, "chapter_id")
        snapshot = _clone(self.parent_snapshot)
        if snapshot.get("schema_version") != REGISTRY_SCHEMA_VERSION:
            raise RegistryContractError("v2/foreign registry snapshot cannot seed v3")
        if snapshot.get("state_lineage_id") != self.state_lineage_id:
            raise RegistryContractError("registry snapshot crosses state lineage")
        for table in ("entities", "aliases", "glossary_items", "tickets"):
            snapshot.setdefault(table, [])
        self._state = snapshot
        self.revision_hash = _snapshot_revision(snapshot, self.chapter_id, [])

    @classmethod
    def create(
        cls,
        *,
        state_lineage_id: str,
        chapter_id: str,
        source_manifest_hash: str,
        parent_generation_id: str | None = None,
        parent_snapshot: Mapping[str, Any] | None = None,
    ) -> "ChapterWorkingRegistryV3":
        snapshot = parent_snapshot or empty_registry_snapshot_v3(state_lineage_id)
        return cls(
            state_lineage_id=state_lineage_id,
            chapter_id=chapter_id,
            source_manifest_hash=_required_str(source_manifest_hash, "source_manifest_hash"),
            parent_generation_id=(
                str(parent_generation_id)
                if parent_generation_id is not None
                else snapshot.get("generation_id")
            ),
            parent_snapshot=snapshot,
        )

    def snapshot(self) -> dict[str, Any]:
        result = {
            "builder_identity_mode": REGISTRY_SCHEMA_VERSION,
            "schema_version": REGISTRY_SCHEMA_VERSION,
            "state_lineage_id": self.state_lineage_id,
            "generation_id": self.parent_generation_id,
            "entities": sorted(_clone(self._state["entities"]), key=lambda row: row["entity_id"]),
            "aliases": sorted(_clone(self._state["aliases"]), key=lambda row: row["alias_id"]),
            "glossary_items": sorted(
                _clone(self._state["glossary_items"]), key=lambda row: row["glossary_id"]
            ),
            "tickets": sorted(_clone(self._state["tickets"]), key=lambda row: row["ticket_id"]),
        }
        result["snapshot_hash"] = canonical_hash(result)
        return result

    def _index(self, table: str, id_field: str) -> dict[str, dict[str, Any]]:
        return {str(row[id_field]): row for row in self._state[table]}

    def _append_unique(self, table: str, id_field: str, row: Mapping[str, Any]) -> None:
        row_id = str(row[id_field])
        prior = self._index(table, id_field).get(row_id)
        if prior is not None:
            if canonical_json(prior) != canonical_json(row):
                raise RegistryContractError(f"{table} id collision with unequal payload")
            return
        self._state[table].append(_clone(row))
        self._created_ids[table].add(row_id)

    def _decision_key(
        self, request_fingerprint: str, list_name: str, row_index: int, payload: Mapping[str, Any]
    ) -> dict[str, Any]:
        return {
            "state_lineage_id": self.state_lineage_id,
            "chapter_id": self.chapter_id,
            "validated_request_fingerprint": request_fingerprint,
            "response_list_name": list_name,
            "response_row_index": row_index,
            "canonical_row_payload": _clone(payload),
        }

    def _code_ticket(
        self,
        *,
        request_fingerprint: str,
        row_index: int,
        ticket_type: str,
        surface: str | None,
        source_block_ids: Sequence[str],
        reason: str,
        candidate_entity_ids: Sequence[str] = (),
        subject_entity_ids: Sequence[str] = (),
    ) -> dict[str, Any]:
        payload = {
            "ticket_type": _require_enum(ticket_type, CODE_TICKET_TYPES, "code ticket type"),
            "surface": _optional_str(surface, "code ticket surface"),
            "source_block_ids": sorted(set(str(item) for item in source_block_ids)),
            "subject_entity_ids": sorted(set(str(item) for item in subject_entity_ids)),
            "subject_glossary_ids": [],
            "candidate_entity_ids": sorted(set(str(item) for item in candidate_entity_ids)),
            "candidate_glossary_ids": [],
            "referent_kind_claim": None,
            "proposed_short_description": None,
            "reason": _required_str(reason, "code ticket reason"),
            "status": "open",
            "opened_by_request_fingerprint": request_fingerprint,
            "resolution_action": None,
            "resolution_note": None,
        }
        ticket_id = _mint_id(
            "tick3_", self._decision_key(request_fingerprint, "code_tickets", row_index, payload)
        )
        return _row_with_revision({"ticket_id": ticket_id, **payload})

    def ingest_code_ticket_proposals(
        self, proposals: Sequence[Mapping[str, Any]], *, source_fingerprint: str
    ) -> None:
        for index, proposal in enumerate(proposals):
            ticket = self._code_ticket(
                request_fingerprint=source_fingerprint,
                row_index=index,
                ticket_type=str(proposal["ticket_type"]),
                surface=proposal.get("surface"),
                source_block_ids=proposal.get("source_block_ids") or [],
                reason=str(proposal["reason"]),
            )
            self._append_unique("tickets", "ticket_id", ticket)
        self.revision_hash = _snapshot_revision(
            self.snapshot(), self.chapter_id, self.applied_request_fingerprints
        )

    def apply_delta(
        self,
        request: RenderedRegistryRequestV3,
        response: Mapping[str, Any],
        *,
        targeted_recall: bool = False,
    ) -> dict[str, Any]:
        if request.role != "b1":
            raise RegistryContractError("only B1 requests can apply StableRegistryDeltaV3")
        response_hash = canonical_hash(response)
        replay = self._replay.get(request.request_fingerprint)
        if replay is not None:
            if replay[0] != response_hash:
                raise RegistryContractError("same request replayed with a different response")
            return _clone(replay[1])
        if request.parent_working_revision_hash != self.revision_hash:
            raise RegistryStaleRevisionError("B1 request targets a stale working revision")
        _require_exact_keys(
            response, {"new_entities", "new_glossary_items", "tickets"}, "StableRegistryDeltaV3"
        )
        active_blocks = _require_list(
            request.sections.get("active_window_blocks"), "active_window_blocks"
        )
        active_catalog = {str(row["block_id"]): str(row["text"]) for row in active_blocks}
        active_order = {str(row["block_id"]): index for index, row in enumerate(active_blocks)}
        supplied_entities, supplied_glossary, overflow = _decode_candidate_universe(request)
        candidate_manifest = request.sections["candidate_selection_manifest"]
        self.candidate_manifest_hashes.append(str(candidate_manifest["manifest_hash"]))

        staged_entities: list[dict[str, Any]] = []
        staged_glossary: list[dict[str, Any]] = []
        code_tickets: list[dict[str, Any]] = []
        pending_name_gate_reviews: list[tuple[dict[str, Any], dict[str, Any], int]] = []
        raw_entity_rows = _require_list(response.get("new_entities"), "new_entities")
        raw_glossary_rows = _require_list(
            response.get("new_glossary_items"), "new_glossary_items"
        )
        raw_tickets = _require_list(response.get("tickets"), "tickets")

        targeted_rows = request.sections.get("targeted_checklist_rows")
        if targeted_recall:
            if not isinstance(targeted_rows, list) or not targeted_rows:
                raise RegistryContractError("targeted recall response lacks a target manifest")
            target_keys = {
                (_normalized_literal(row.get("surface")), str(row.get("block_id"))): str(
                    row.get("checklist_class")
                )
                for row in targeted_rows
            }

            def require_target_scope(raw: Mapping[str, Any], *, row_type: str) -> None:
                block_ids = _require_string_list(
                    raw.get("source_block_ids"), f"targeted {row_type} source blocks"
                )
                surface = raw.get("surface")
                if surface is None:
                    raise RegistryContractError(
                        "targeted recall output must copy its target surface"
                    )
                normalized = _normalized_literal(surface)
                if not all((normalized, block_id) in target_keys for block_id in block_ids):
                    raise RegistryContractError(
                        f"targeted recall emitted out-of-scope {row_type}"
                    )

            for raw in raw_entity_rows:
                if not isinstance(raw, Mapping):
                    raise RegistryContractError("new entity must be an object")
                require_target_scope(raw, row_type="entity")
            for raw in raw_glossary_rows:
                if not isinstance(raw, Mapping):
                    raise RegistryContractError("new glossary item must be an object")
                require_target_scope(raw, row_type="glossary")
            for raw in raw_tickets:
                if not isinstance(raw, Mapping):
                    raise RegistryContractError("ticket must be an object")
                ticket_type = str(raw.get("ticket_type") or "")
                require_target_scope(raw, row_type="ticket")
                if ticket_type == "important_unnamed_referent":
                    matching_classes = {
                        checklist_class
                        for (surface_key, block_id), checklist_class in target_keys.items()
                        if block_id in (raw.get("source_block_ids") or [])
                        and (
                            raw.get("surface") is None
                            or surface_key == _normalized_literal(raw.get("surface"))
                        )
                    }
                    if matching_classes != {"important_unnamed_referent"}:
                        raise RegistryContractError(
                            "important unnamed ticket cannot answer a named or term target"
                        )

        entity_surfaces: set[str] = set()
        glossary_surfaces: set[str] = set()
        for index, raw in enumerate(raw_entity_rows):
            if not isinstance(raw, Mapping):
                raise RegistryContractError("new entity must be an object")
            _require_exact_keys(
                raw,
                {
                    "surface",
                    "name_class",
                    "referent_kind_claim",
                    "short_description",
                    "source_block_ids",
                },
                "new entity",
            )
            surface = _required_str(raw.get("surface"), "entity surface")
            normalized = _normalized_literal(surface)
            entity_surfaces.add(normalized)
            block_ids = sorted(
                _require_string_list(raw.get("source_block_ids"), "entity source blocks"),
                key=lambda value: (active_order.get(value, 10**9), value),
            )
            if normalized in _CONTEXTUAL_NORMALIZED:
                code_tickets.append(
                    self._code_ticket(
                        request_fingerprint=request.request_fingerprint,
                        row_index=9000 + index,
                        ticket_type="alias_scope_review",
                        surface=surface,
                        source_block_ids=block_ids,
                        reason="bare contextual reference cannot become a stable B1 entity",
                    )
                )
                continue
            located = _located_support(
                surface=surface, block_ids=block_ids, active_catalog=active_catalog
            )
            if normalized in overflow:
                code_tickets.append(
                    self._code_ticket(
                        request_fingerprint=request.request_fingerprint,
                        row_index=10000 + index,
                        ticket_type="candidate_overflow",
                        surface=surface,
                        source_block_ids=block_ids,
                        reason="candidate packet overflow forbids authoritative registry mutation",
                    )
                )
                continue
            if not located:
                code_tickets.append(
                    self._code_ticket(
                        request_fingerprint=request.request_fingerprint,
                        row_index=11000 + index,
                        ticket_type="unlocatable_surface",
                        surface=surface,
                        source_block_ids=block_ids,
                        reason="entity surface was absent from one or more declared active blocks",
                    )
                )
                continue
            payload = {
                "canonical_surface": surface,
                "name_class": _require_enum(raw.get("name_class"), NAME_CLASSES, "name_class"),
                "referent_kind": _require_enum(
                    raw.get("referent_kind_claim"), REFERENT_KINDS, "referent kind"
                ),
                "identity_summary": _required_str(
                    raw.get("short_description"), "entity short_description"
                ),
                "created_from_block_ids": [block_ids[0]],
                "support_block_ids": block_ids,
                "status": "provisional",
            }
            entity_id = _mint_id(
                "ent3_", self._decision_key(request.request_fingerprint, "new_entities", index, payload)
            )
            staged_entities.append(
                _row_with_revision({"entity_id": entity_id, **payload, "located_spans": located})
            )
            name_gate = route_alias_for_commit(
                surface=surface,
                name_class=str(payload["name_class"]),
                target_entity_id=entity_id,
                source_block_ids=block_ids,
                source_catalog=active_catalog,
                source_decision_lineage={
                    "request_fingerprint": request.request_fingerprint,
                    "response_list_name": "new_entities",
                    "response_row_index": index,
                    "purpose": "clean_row_name_shape",
                },
            )
            if name_gate["outcome"] != "eligible_global_alias":
                pending_name_gate_reviews.append((staged_entities[-1], name_gate, index))

        for index, raw in enumerate(raw_glossary_rows):
            if not isinstance(raw, Mapping):
                raise RegistryContractError("new glossary item must be an object")
            _require_exact_keys(
                raw, {"surface", "category_claim", "short_description", "source_block_ids"}, "new glossary"
            )
            surface = _required_str(raw.get("surface"), "glossary surface")
            normalized = _normalized_literal(surface)
            glossary_surfaces.add(normalized)
            block_ids = sorted(
                _require_string_list(raw.get("source_block_ids"), "glossary source blocks"),
                key=lambda value: (active_order.get(value, 10**9), value),
            )
            located = _located_support(
                surface=surface, block_ids=block_ids, active_catalog=active_catalog
            )
            if normalized in overflow:
                code_tickets.append(
                    self._code_ticket(
                        request_fingerprint=request.request_fingerprint,
                        row_index=12000 + index,
                        ticket_type="candidate_overflow",
                        surface=surface,
                        source_block_ids=block_ids,
                        reason="candidate packet overflow forbids authoritative glossary mutation",
                    )
                )
                continue
            if not located:
                code_tickets.append(
                    self._code_ticket(
                        request_fingerprint=request.request_fingerprint,
                        row_index=13000 + index,
                        ticket_type="unlocatable_surface",
                        surface=surface,
                        source_block_ids=block_ids,
                        reason="glossary surface was absent from one or more declared active blocks",
                    )
                )
                continue
            payload = {
                "surface": surface,
                "category_claim": _require_enum(
                    raw.get("category_claim"), GLOSSARY_CATEGORIES, "glossary category"
                ),
                "short_description": _required_str(
                    raw.get("short_description"), "glossary short_description"
                ),
                "created_from_block_ids": [block_ids[0]],
                "support_block_ids": block_ids,
                "status": "provisional",
            }
            glossary_id = _mint_id(
                "gls3_",
                self._decision_key(request.request_fingerprint, "new_glossary_items", index, payload),
            )
            staged_glossary.append(
                _row_with_revision({"glossary_id": glossary_id, **payload, "located_spans": located})
            )
        if entity_surfaces & glossary_surfaces:
            raise RegistryContractError("one response emits the same surface as entity and glossary")

        new_entity_by_surface: dict[str, list[str]] = defaultdict(list)
        for row in staged_entities:
            new_entity_by_surface[_normalized_literal(row["canonical_surface"])].append(row["entity_id"])
        new_glossary_by_surface: dict[str, list[str]] = defaultdict(list)
        for row in staged_glossary:
            new_glossary_by_surface[_normalized_literal(row["surface"])].append(row["glossary_id"])

        staged_tickets: list[dict[str, Any]] = []
        for index, raw in enumerate(raw_tickets):
            if not isinstance(raw, Mapping):
                raise RegistryContractError("ticket must be an object")
            _require_exact_keys(
                raw,
                {
                    "ticket_type",
                    "surface",
                    "source_block_ids",
                    "candidate_entity_ids",
                    "candidate_glossary_ids",
                    "referent_kind_claim",
                    "proposed_short_description",
                    "reason",
                },
                "model ticket",
            )
            ticket_type = _require_enum(
                raw.get("ticket_type"), MODEL_TICKET_TYPES, "model ticket type"
            )
            surface = _optional_str(raw.get("surface"), "ticket surface")
            block_ids = sorted(
                _require_string_list(raw.get("source_block_ids"), "ticket source blocks"),
                key=lambda value: (active_order.get(value, 10**9), value),
            )
            if not set(block_ids) <= set(active_catalog):
                raise RegistryContractError("ticket cites a foreign or context-only block")
            candidate_entity_ids = _require_string_list(
                raw.get("candidate_entity_ids"), "ticket candidate entity ids", allow_empty=True
            )
            candidate_glossary_ids = _require_string_list(
                raw.get("candidate_glossary_ids"), "ticket candidate glossary ids", allow_empty=True
            )
            if not set(candidate_entity_ids) <= supplied_entities:
                raise RegistryContractError("ticket cites foreign candidate entity")
            if not set(candidate_glossary_ids) <= supplied_glossary:
                raise RegistryContractError("ticket cites foreign candidate glossary item")
            if surface is not None and not _located_support(
                surface=surface, block_ids=block_ids, active_catalog=active_catalog
            ):
                raise RegistryContractError("model ticket surface is absent from its active blocks")
            kind = raw.get("referent_kind_claim")
            kind = None if kind is None else _require_enum(kind, REFERENT_KINDS, "ticket kind")
            description = _optional_str(
                raw.get("proposed_short_description"), "ticket proposed description"
            )
            if ticket_type == "important_unnamed_referent" and (kind is None or description is None):
                raise RegistryContractError("important unnamed ticket requires kind and description")
            if ticket_type == "important_unnamed_referent" and surface is None:
                raise RegistryContractError(
                    "important unnamed ticket requires its descriptor evidence surface"
                )
            normalized = _normalized_literal(surface) if surface else ""
            payload = {
                "ticket_type": ticket_type,
                "surface": surface,
                "source_block_ids": block_ids,
                "subject_entity_ids": sorted(new_entity_by_surface.get(normalized, [])),
                "subject_glossary_ids": sorted(new_glossary_by_surface.get(normalized, [])),
                "candidate_entity_ids": candidate_entity_ids,
                "candidate_glossary_ids": candidate_glossary_ids,
                "referent_kind_claim": kind,
                "proposed_short_description": description,
                "reason": _required_str(raw.get("reason"), "ticket reason"),
                "status": "open",
                "opened_by_request_fingerprint": request.request_fingerprint,
                "resolution_action": None,
                "resolution_note": None,
            }
            ticket_id = _mint_id(
                "tick3_", self._decision_key(request.request_fingerprint, "tickets", index, payload)
            )
            staged_tickets.append(_row_with_revision({"ticket_id": ticket_id, **payload}))

        packets = request.sections.get("surface_candidate_packets") or []

        def overlapping_candidate_ids(
            *, surface: str, block_ids: Sequence[str], row_key: str
        ) -> set[str]:
            proposed_tokens = set(_normalized_literal(surface).split())
            result: set[str] = set()
            for packet in packets:
                packet_blocks = set(packet.get("source_block_ids") or [])
                packet_tokens = set(_normalized_literal(packet.get("source_surface")).split())
                if not packet_blocks.intersection(block_ids):
                    continue
                if not proposed_tokens or not packet_tokens or not proposed_tokens.intersection(packet_tokens):
                    continue
                result.update(str(row[row_key]) for row in packet.get(
                    "candidate_entities" if row_key == "entity_id" else "candidate_glossary_items"
                ) or [])
            return result

        for entity in staged_entities:
            candidate_ids = overlapping_candidate_ids(
                surface=str(entity["canonical_surface"]),
                block_ids=entity["support_block_ids"],
                row_key="entity_id",
            )
            if not candidate_ids:
                continue
            attached = [
                ticket
                for ticket in staged_tickets
                if entity["entity_id"] in ticket["subject_entity_ids"]
                and set(ticket["candidate_entity_ids"]).intersection(candidate_ids)
                and ticket["ticket_type"]
                in {"same_name_collision", "possible_alias", "kind_conflict", "profile_conflict"}
            ]
            if not attached:
                raise RegistryContractError(
                    "new entity overlaps supplied candidates but lacks an identity-review ticket"
                )
        for item in staged_glossary:
            candidate_ids = overlapping_candidate_ids(
                surface=str(item["surface"]),
                block_ids=item["support_block_ids"],
                row_key="glossary_id",
            )
            attached = [
                ticket
                for ticket in staged_tickets
                if item["glossary_id"] in ticket["subject_glossary_ids"]
                and set(ticket["candidate_glossary_ids"]).intersection(candidate_ids)
                and ticket["ticket_type"] == "glossary_collision"
            ]
            if candidate_ids and not attached:
                raise RegistryContractError(
                    "new glossary item overlaps supplied candidates but lacks a collision ticket"
                )

        model_ticketed_entities = {
            entity_id for ticket in staged_tickets for entity_id in ticket["subject_entity_ids"]
        }
        for entity, name_gate, index in pending_name_gate_reviews:
            if entity["entity_id"] in model_ticketed_entities:
                continue
            code_tickets.append(
                self._code_ticket(
                    request_fingerprint=request.request_fingerprint,
                    row_index=9500 + index,
                    ticket_type="alias_scope_review",
                    surface=str(entity["canonical_surface"]),
                    source_block_ids=entity["support_block_ids"],
                    subject_entity_ids=[entity["entity_id"]],
                    reason=(
                        "new stable-name surface requires review before clean publication: "
                        + str(name_gate["reason_code"])
                    ),
                )
            )

        ticketed_entities = {
            entity_id
            for ticket in staged_tickets + code_tickets
            for entity_id in ticket["subject_entity_ids"]
        }
        ticketed_glossary = {
            glossary_id for ticket in staged_tickets for glossary_id in ticket["subject_glossary_ids"]
        }
        for row in staged_entities:
            if row["referent_kind"] == "unknown" and row["entity_id"] not in ticketed_entities:
                raise RegistryContractError("unknown entity must be attached to a review ticket")
        for row in staged_entities:
            if row["entity_id"] not in ticketed_entities:
                self.clean_entity_ids.add(row["entity_id"])
        for row in staged_glossary:
            if row["glossary_id"] not in ticketed_glossary:
                self.clean_glossary_ids.add(row["glossary_id"])

        for row in staged_entities:
            self._append_unique("entities", "entity_id", row)
        for row in staged_glossary:
            self._append_unique("glossary_items", "glossary_id", row)
        for row in staged_tickets + code_tickets:
            self._append_unique("tickets", "ticket_id", row)

        record = {
            "request_fingerprint": request.request_fingerprint,
            "response_hash": response_hash,
            "entity_ids": [row["entity_id"] for row in staged_entities],
            "glossary_ids": [row["glossary_id"] for row in staged_glossary],
            "ticket_ids": [row["ticket_id"] for row in staged_tickets + code_tickets],
            "targeted_recall": bool(targeted_recall),
        }
        self.applied_request_fingerprints.append(request.request_fingerprint)
        if targeted_recall:
            self.targeted_recall_request_fingerprints.append(request.request_fingerprint)
        self.application_records.append(_clone(record))
        self.revision_hash = _snapshot_revision(
            self.snapshot(), self.chapter_id, self.applied_request_fingerprints
        )
        self._replay[request.request_fingerprint] = (response_hash, _clone(record))
        return _clone(record)


def checklist_coverage(
    orientation: Mapping[str, Any], working_snapshot: Mapping[str, Any]
) -> dict[str, Any]:
    entities = list(working_snapshot.get("entities") or [])
    glossary = list(working_snapshot.get("glossary_items") or [])
    tickets = list(working_snapshot.get("tickets") or [])
    covered: list[str] = []
    missing: list[dict[str, Any]] = []
    for row in orientation.get("salient_registry_checklist") or []:
        normalized = _normalized_literal(row["surface"])
        block_id = str(row["block_id"])
        checklist_class = str(row["checklist_class"])
        if checklist_class == "important_unnamed_referent":
            hit = any(
                ticket.get("ticket_type") == "important_unnamed_referent"
                and _normalized_literal(ticket.get("surface")) == normalized
                and block_id in (ticket.get("source_block_ids") or [])
                for ticket in tickets
                if ticket.get("status") != "resolved"
            ) or any(
                entity.get("name_class") is None
                and block_id in (entity.get("created_from_block_ids") or [])
                and _normalized_literal(entity.get("canonical_surface")) == normalized
                for entity in entities
            )
        else:
            hit = any(
                _normalized_literal(entity.get("canonical_surface")) == normalized
                and block_id in (entity.get("support_block_ids") or [])
                for entity in entities
            ) or any(
                _normalized_literal(item.get("surface")) == normalized
                and block_id in (item.get("support_block_ids") or [])
                for item in glossary
            ) or any(
                _normalized_literal(ticket.get("surface")) == normalized
                and block_id in (ticket.get("source_block_ids") or [])
                for ticket in tickets
                if ticket.get("status") != "resolved"
            )
        if hit:
            covered.append(str(row["checklist_id"]))
        else:
            missing.append(_clone(row))
    body = {
        "covered_checklist_ids": sorted(covered),
        "missing_rows": missing,
        "covered_count": len(covered),
        "missing_count": len(missing),
    }
    return {**body, "coverage_hash": canonical_hash(body)}


def schedule_targeted_recall(
    *,
    orientation: Mapping[str, Any],
    working_snapshot: Mapping[str, Any],
    windows: Sequence[Mapping[str, Any]],
    call_cap: int,
) -> list[dict[str, Any]]:
    coverage = checklist_coverage(orientation, working_snapshot)
    block_to_window: dict[str, str] = {}
    for window in windows:
        for block in window.get("blocks") or []:
            block_to_window[str(block["block_id"])] = str(window["window_id"])
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in coverage["missing_rows"]:
        window_id = block_to_window.get(str(row["block_id"]))
        if window_id is None:
            raise RegistryContractError("missing checklist row is outside B1 window coverage")
        grouped[window_id].append(_clone(row))
    if len(grouped) > call_cap:
        raise RegistryBudgetError(
            f"targeted recall requires {len(grouped)} calls, above cap {call_cap}"
        )
    return [
        {"window_id": window_id, "missing_checklist_rows": grouped[window_id]}
        for window_id in sorted(grouped)
    ]


def build_exception_components(working: ChapterWorkingRegistryV3) -> dict[str, Any]:
    open_tickets = [
        row for row in working.snapshot()["tickets"] if row.get("status") == "open"
    ]
    if not open_tickets:
        return {"components": [], "ticket_count": 0, "component_count": 0}
    neighbors: dict[str, set[str]] = {str(row["ticket_id"]): set() for row in open_tickets}
    signatures: dict[str, set[str]] = {}
    for ticket in open_tickets:
        ticket_id = str(ticket["ticket_id"])
        signatures[ticket_id] = {
            *("entity:" + str(value) for value in ticket.get("subject_entity_ids") or []),
            *("entity:" + str(value) for value in ticket.get("candidate_entity_ids") or []),
            *("glossary:" + str(value) for value in ticket.get("subject_glossary_ids") or []),
            *("glossary:" + str(value) for value in ticket.get("candidate_glossary_ids") or []),
            *("surface:" + _normalized_literal(ticket["surface"]) for _ in [0] if ticket.get("surface")),
        }
    ids = sorted(signatures)
    for index, left in enumerate(ids):
        for right in ids[index + 1 :]:
            if signatures[left] & signatures[right]:
                neighbors[left].add(right)
                neighbors[right].add(left)
    ticket_by_id = {str(row["ticket_id"]): row for row in open_tickets}
    components: list[dict[str, Any]] = []
    seen: set[str] = set()
    for root in ids:
        if root in seen:
            continue
        stack = [root]
        owned: list[str] = []
        while stack:
            current = stack.pop()
            if current in seen:
                continue
            seen.add(current)
            owned.append(current)
            stack.extend(sorted(neighbors[current] - seen, reverse=True))
        tickets = [ticket_by_id[ticket_id] for ticket_id in sorted(owned)]
        body = {
            "component_id": "component3_" + canonical_hash(sorted(owned))[:20],
            "ticket_ids": sorted(owned),
            "tickets": tickets,
            "subject_entity_ids": sorted(
                {value for row in tickets for value in row.get("subject_entity_ids") or []}
            ),
            "candidate_entity_ids": sorted(
                {value for row in tickets for value in row.get("candidate_entity_ids") or []}
            ),
            "subject_glossary_ids": sorted(
                {value for row in tickets for value in row.get("subject_glossary_ids") or []}
            ),
            "candidate_glossary_ids": sorted(
                {value for row in tickets for value in row.get("candidate_glossary_ids") or []}
            ),
            "source_block_ids": sorted(
                {value for row in tickets for value in row.get("source_block_ids") or []}
            ),
        }
        body["component_hash"] = canonical_hash(body)
        components.append(body)
    components.sort(key=lambda row: row["component_id"])
    return {
        "components": components,
        "ticket_count": len(open_tickets),
        "component_count": len(components),
    }


def enforce_exception_budget(
    *,
    working: ChapterWorkingRegistryV3,
    b1_call_count: int,
    run_config: RunConfigV3,
) -> dict[str, Any]:
    """Halt before Auditor calls when live exception growth exceeds the locked envelope."""

    if b1_call_count <= 0:
        raise RegistryContractError("exception budget requires at least one B1 call")
    component_manifest = build_exception_components(working)
    ticket_share = component_manifest["ticket_count"] / b1_call_count
    component_share = component_manifest["component_count"] / b1_call_count
    result = {
        "b1_call_count": b1_call_count,
        "ticket_count": component_manifest["ticket_count"],
        "component_count": component_manifest["component_count"],
        "ticket_share": ticket_share,
        "component_share": component_share,
        "ticket_warning": ticket_share > run_config.ticket_share_warning,
        "component_warning": component_share > run_config.component_share_warning,
    }
    if ticket_share > run_config.ticket_share_halt:
        raise RegistryBudgetError(
            f"ticket share {ticket_share:.3f} exceeds halt threshold "
            f"{run_config.ticket_share_halt:.3f}"
        )
    if component_share > run_config.component_share_halt:
        raise RegistryBudgetError(
            f"component share {component_share:.3f} exceeds halt threshold "
            f"{run_config.component_share_halt:.3f}"
        )
    return result


def render_auditor_requests(
    *,
    chapter: Mapping[str, Any],
    orientation: Mapping[str, Any],
    working: ChapterWorkingRegistryV3,
    design_doc: Path,
    run_config: RunConfigV3,
) -> list[RenderedRegistryRequestV3]:
    component_manifest = build_exception_components(working)
    components = component_manifest["components"]
    if len(components) > run_config.ticket_component_cap:
        raise RegistryBudgetError("ticket component cap exceeded")
    if len(components) > run_config.auditor_call_cap:
        raise RegistryBudgetError("Auditor call cap exceeded")
    snapshot = working.snapshot()
    entities = {str(row["entity_id"]): row for row in snapshot["entities"]}
    glossary = {str(row["glossary_id"]): row for row in snapshot["glossary_items"]}
    source_catalog = {str(row["block_id"]): _block_view(row) for row in _chapter_blocks(chapter)}
    roster = [
        {
            "entity_id": row["entity_id"],
            "canonical_surface": row["canonical_surface"],
            "referent_kind": row["referent_kind"],
            "status": row["status"],
        }
        for row in snapshot["entities"]
    ]
    requests: list[RenderedRegistryRequestV3] = []
    for component in components:
        entity_ids = set(component["subject_entity_ids"]) | set(component["candidate_entity_ids"])
        glossary_ids = set(component["subject_glossary_ids"]) | set(
            component["candidate_glossary_ids"]
        )
        sections = {
            "b0_orientation": _clone(orientation),
            "compact_chapter_roster": roster,
            "ticket_component": _clone(component),
            "referenced_entities": [entities[row_id] for row_id in sorted(entity_ids) if row_id in entities],
            "referenced_glossary_items": [
                glossary[row_id] for row_id in sorted(glossary_ids) if row_id in glossary
            ],
            "source_blocks": [
                source_catalog[block_id]
                for block_id in component["source_block_ids"]
                if block_id in source_catalog
            ],
            "working_registry_revision_hash": working.revision_hash,
            "action_manifest": {
                ticket_type: sorted(_AUDIT_ALLOWED_ACTIONS[ticket_type])
                for ticket_type in sorted(
                    {str(row["ticket_type"]) for row in component["tickets"]}
                )
            },
            "cap_manifest": {
                "auditor_input_token_cap": run_config.auditor_input_token_cap,
                "auditor_output_token_cap": run_config.auditor_output_token_cap,
            },
        }
        request = _render_request(
            role="auditor",
            chapter_id=working.chapter_id,
            window_id=str(component["component_id"]),
            parent_working_revision_hash=working.revision_hash,
            sections=sections,
            design_doc=design_doc,
            run_config=run_config,
        )
        tokens = estimate_registry_prompt_tokens(request.messages)
        if tokens > run_config.auditor_input_token_cap:
            raise RegistryBudgetError(
                f"Auditor component input {tokens} exceeds cap {run_config.auditor_input_token_cap}"
            )
        requests.append(request)
    return requests


def validate_audit_response(
    request: RenderedRegistryRequestV3, response: Mapping[str, Any]
) -> dict[str, Any]:
    if request.role != "auditor":
        raise RegistryContractError("audit response requires an Auditor request")
    _require_exact_keys(response, {"ticket_dispositions"}, "ChapterRegistryAuditV2")
    component = request.sections.get("ticket_component")
    if not isinstance(component, Mapping):
        raise RegistryContractError("Auditor request lacks ticket component")
    ticket_by_id = {str(row["ticket_id"]): row for row in component["tickets"]}
    subject_entities = set(component["subject_entity_ids"])
    candidate_entities = set(component["candidate_entity_ids"])
    subject_glossary = set(component["subject_glossary_ids"])
    candidate_glossary = set(component["candidate_glossary_ids"])
    dispositions: list[dict[str, Any]] = []
    for raw in _require_list(response.get("ticket_dispositions"), "ticket dispositions"):
        if not isinstance(raw, Mapping):
            raise RegistryContractError("ticket disposition must be an object")
        _require_exact_keys(
            raw,
            {
                "ticket_id",
                "action",
                "source_entity_id",
                "target_entity_id",
                "source_glossary_id",
                "target_glossary_id",
                "resolved_referent_kind",
                "revised_identity_summary",
                "name_class",
                "resolution_note",
            },
            "ticket disposition",
        )
        ticket_id = _required_str(raw.get("ticket_id"), "ticket_id")
        ticket = ticket_by_id.get(ticket_id)
        if ticket is None:
            raise RegistryContractError("Auditor disposition cites foreign ticket")
        action = _require_enum(raw.get("action"), AUDIT_ACTIONS, "audit action")
        if action not in _AUDIT_ALLOWED_ACTIONS[str(ticket["ticket_type"])]:
            raise RegistryContractError("Auditor action is invalid for ticket type")
        source_entity_id = _optional_str(raw.get("source_entity_id"), "source_entity_id")
        target_entity_id = _optional_str(raw.get("target_entity_id"), "target_entity_id")
        source_glossary_id = _optional_str(raw.get("source_glossary_id"), "source_glossary_id")
        target_glossary_id = _optional_str(raw.get("target_glossary_id"), "target_glossary_id")
        all_entities = subject_entities | candidate_entities
        all_glossary = subject_glossary | candidate_glossary
        if source_entity_id is not None and source_entity_id not in subject_entities:
            raise RegistryContractError("source entity is not an owned provisional subject")
        if target_entity_id is not None and target_entity_id not in all_entities:
            raise RegistryContractError("target entity is not supplied")
        if source_glossary_id is not None and source_glossary_id not in subject_glossary:
            raise RegistryContractError("source glossary is not an owned provisional subject")
        if target_glossary_id is not None and target_glossary_id not in all_glossary:
            raise RegistryContractError("target glossary is not supplied")
        kind = raw.get("resolved_referent_kind")
        kind = None if kind is None else _require_enum(kind, REFERENT_KINDS, "resolved kind")
        summary = _optional_str(raw.get("revised_identity_summary"), "revised summary")
        name_class = raw.get("name_class")
        name_class = None if name_class is None else _require_enum(name_class, NAME_CLASSES, "name class")
        if action == "merge_as_alias":
            if source_entity_id is None or target_entity_id is None or source_entity_id == target_entity_id or name_class is None:
                raise RegistryContractError("merge_as_alias requires distinct source/target and name_class")
        elif action == "create_unnamed_entity":
            if any(value is not None for value in (source_entity_id, target_entity_id, source_glossary_id, target_glossary_id)):
                raise RegistryContractError("create_unnamed_entity cannot target supplied rows")
            if kind in {None, "unknown"} or summary is None:
                raise RegistryContractError("create_unnamed_entity requires non-unknown kind and summary")
        elif action == "promote_global_alias":
            if target_entity_id is None or name_class is None:
                raise RegistryContractError("promote_global_alias requires target and name_class")
        elif action == "confirm_distinct_entity":
            if target_entity_id is None or target_entity_id not in subject_entities:
                raise RegistryContractError("confirm_distinct_entity requires owned provisional target")
        elif action == "confirm_distinct_glossary":
            if target_glossary_id is None or target_glossary_id not in subject_glossary:
                raise RegistryContractError("confirm_distinct_glossary requires owned provisional target")
        elif action == "merge_glossary":
            if source_glossary_id is None or target_glossary_id is None or source_glossary_id == target_glossary_id:
                raise RegistryContractError("merge_glossary requires distinct source/target")
        elif action == "revise_profile":
            if target_entity_id is None or summary is None:
                raise RegistryContractError("revise_profile requires target and summary")
        dispositions.append(
            {
                "ticket_id": ticket_id,
                "action": action,
                "source_entity_id": source_entity_id,
                "target_entity_id": target_entity_id,
                "source_glossary_id": source_glossary_id,
                "target_glossary_id": target_glossary_id,
                "resolved_referent_kind": kind,
                "revised_identity_summary": summary,
                "name_class": name_class,
                "resolution_note": _required_str(raw.get("resolution_note"), "resolution_note"),
            }
        )
    ids = [row["ticket_id"] for row in dispositions]
    if len(ids) != len(set(ids)) or set(ids) != set(ticket_by_id):
        raise RegistryContractError("Auditor dispositions must exact-cover owned tickets")
    return {"ticket_dispositions": dispositions, "response_hash": canonical_hash(response)}


def _sentence_initial(text: str, char_start: int) -> bool:
    prefix = text[:char_start].rstrip()
    while prefix and prefix[-1] in "\"'([{\u2018\u201c":
        prefix = prefix[:-1].rstrip()
    return not prefix or prefix[-1] in ".!?"


def route_alias_for_commit(
    *,
    surface: str,
    name_class: str,
    target_entity_id: str,
    source_block_ids: Sequence[str],
    source_catalog: Mapping[str, str],
    source_decision_lineage: Mapping[str, Any],
) -> dict[str, Any]:
    """Apply one conservative mechanical gate without deciding co-reference."""

    checked_surface = _required_str(surface, "alias surface")
    checked_class = _require_enum(name_class, NAME_CLASSES, "alias name_class")
    checked_target = _required_str(target_entity_id, "alias target_entity_id")
    checked_blocks = _require_string_list(list(source_block_ids), "alias source blocks")
    if not set(checked_blocks) <= set(source_catalog):
        raise RegistryContractError("alias gate cites a foreign source block")
    occurrences: list[dict[str, Any]] = []
    for block_id in checked_blocks:
        text = source_catalog[block_id]
        for span in _all_exact_spans(text, checked_surface):
            occurrences.append(
                {
                    "block_id": block_id,
                    **span,
                    "sentence_initial": _sentence_initial(text, span["char_start"]),
                }
            )
    cased = [index for index, char in enumerate(checked_surface) if char.isalpha()]
    uppercase = [index for index, char in enumerate(checked_surface) if char.isupper()]
    first_cased = cased[0] if cased else None
    internal_uppercase = any(index != first_cased for index in uppercase)
    noninitial_occurrence = any(not row["sentence_initial"] for row in occurrences)
    proper_name_signal = bool(uppercase) and (internal_uppercase or noninitial_occurrence)
    sentence_initial_only = bool(uppercase) and bool(occurrences) and not proper_name_signal
    normalized = _normalized_literal(checked_surface)
    clearly_contextual = normalized in _CONTEXTUAL_NORMALIZED
    if not occurrences:
        outcome = "pending_scope_review"
        reason = "surface_not_located_in_decision_lineage"
    elif proper_name_signal:
        outcome = "eligible_global_alias"
        reason = "independent_orthographic_name_signal"
    elif clearly_contextual:
        outcome = "defer_to_b2"
        reason = "contextual_reference_has_no_stable_name_signal"
    else:
        outcome = "pending_scope_review"
        reason = (
            "sentence_initial_capitalization_is_insufficient"
            if sentence_initial_only
            else "no_independent_stable_name_signal"
        )
    if outcome not in _ALIAS_GATE_OUTCOMES:
        raise RegistryContractError("alias gate produced an invalid outcome")
    record = {
        "policy_version": ALIAS_SCOPE_POLICY_VERSION,
        "surface": checked_surface,
        "name_class": checked_class,
        "target_entity_id": checked_target,
        "source_block_ids": checked_blocks,
        "exact_source_occurrences": occurrences,
        "proper_name_signal": proper_name_signal,
        "sentence_initial_only": sentence_initial_only,
        "source_decision_lineage": _clone(source_decision_lineage),
        "outcome": outcome,
        "reason_code": reason,
    }
    record["gate_record_hash"] = canonical_hash(record)
    return record


def _replace_row(
    working: ChapterWorkingRegistryV3,
    table: str,
    id_field: str,
    row_id: str,
    changes: Mapping[str, Any],
) -> None:
    for index, row in enumerate(working._state[table]):
        if str(row[id_field]) == row_id:
            payload = {**row, **_clone(changes)}
            payload.pop("revision_hash", None)
            working._state[table][index] = _row_with_revision(payload)
            return
    raise RegistryContractError(f"missing {table} row: {row_id}")


def _remove_row(
    working: ChapterWorkingRegistryV3, table: str, id_field: str, row_id: str
) -> dict[str, Any]:
    for index, row in enumerate(working._state[table]):
        if str(row[id_field]) == row_id:
            return working._state[table].pop(index)
    raise RegistryContractError(f"missing {table} row: {row_id}")


def _alias_scope_ticket(
    *,
    working: ChapterWorkingRegistryV3,
    source_fingerprint: str,
    row_index: int,
    surface: str,
    source_block_ids: Sequence[str],
    target_entity_id: str,
    gate_record: Mapping[str, Any],
) -> dict[str, Any]:
    ticket = working._code_ticket(
        request_fingerprint=source_fingerprint,
        row_index=row_index,
        ticket_type="alias_scope_review",
        surface=surface,
        source_block_ids=source_block_ids,
        candidate_entity_ids=[target_entity_id],
        reason=f"alias gate routed {gate_record['outcome']}: {gate_record['reason_code']}",
    )
    payload = dict(ticket)
    payload.pop("revision_hash", None)
    payload["status"] = "carried"
    return _row_with_revision(payload)


def apply_audit_responses(
    *,
    working: ChapterWorkingRegistryV3,
    request_response_pairs: Sequence[
        tuple[RenderedRegistryRequestV3, Mapping[str, Any]]
    ],
    source_catalog: Mapping[str, str],
) -> list[dict[str, Any]]:
    if not request_response_pairs:
        for entity_id in sorted(working.clean_entity_ids):
            _replace_row(working, "entities", "entity_id", entity_id, {"status": "confirmed"})
        for glossary_id in sorted(working.clean_glossary_ids):
            _replace_row(
                working, "glossary_items", "glossary_id", glossary_id, {"status": "confirmed"}
            )
        working.revision_hash = _snapshot_revision(
            working.snapshot(), working.chapter_id, working.applied_request_fingerprints
        )
        return []
    expected_parent = working.revision_hash
    seen_tickets: set[str] = set()
    validated: list[tuple[RenderedRegistryRequestV3, dict[str, Any]]] = []
    for request, response in request_response_pairs:
        if request.parent_working_revision_hash != expected_parent:
            raise RegistryStaleRevisionError("Auditor request targets a stale working revision")
        audit = validate_audit_response(request, response)
        ticket_ids = {row["ticket_id"] for row in audit["ticket_dispositions"]}
        if seen_tickets & ticket_ids:
            raise RegistryContractError("Auditor requests overlap ticket ownership")
        seen_tickets |= ticket_ids
        validated.append((request, audit))
    expected_open = {
        str(row["ticket_id"])
        for row in working._state["tickets"]
        if row.get("status") == "open"
    }
    if seen_tickets != expected_open:
        raise RegistryContractError("Auditor batch must exact-cover all open tickets")

    outputs: list[dict[str, Any]] = []
    for request, audit in validated:
        for index, disposition in enumerate(audit["ticket_dispositions"]):
            ticket_id = disposition["ticket_id"]
            ticket = working._index("tickets", "ticket_id")[ticket_id]
            action = disposition["action"]
            if action == "confirm_distinct_entity":
                _replace_row(
                    working,
                    "entities",
                    "entity_id",
                    disposition["target_entity_id"],
                    {"status": "confirmed"},
                )
            elif action == "merge_as_alias":
                source_id = str(disposition["source_entity_id"])
                target_id = str(disposition["target_entity_id"])
                source = working._index("entities", "entity_id").get(source_id)
                target = working._index("entities", "entity_id").get(target_id)
                if source is None or target is None or source.get("status") == "confirmed":
                    raise RegistryContractError("merge source/target is missing or source is immutable")
                support = sorted(
                    set(target.get("support_block_ids") or [])
                    | set(source.get("support_block_ids") or [])
                )
                _replace_row(
                    working,
                    "entities",
                    "entity_id",
                    target_id,
                    {"support_block_ids": support},
                )
                gate = route_alias_for_commit(
                    surface=str(source["canonical_surface"]),
                    name_class=str(disposition["name_class"]),
                    target_entity_id=target_id,
                    source_block_ids=source["support_block_ids"],
                    source_catalog=source_catalog,
                    source_decision_lineage={
                        "request_fingerprint": request.request_fingerprint,
                        "ticket_id": ticket_id,
                        "action": action,
                    },
                )
                working.alias_gate_records.append(gate)
                if gate["outcome"] == "eligible_global_alias":
                    alias_payload = {
                        "surface": source["canonical_surface"],
                        "name_class": disposition["name_class"],
                        "entity_id": target_id,
                        "support_block_ids": source["support_block_ids"],
                        "status": "confirmed",
                        "gate_outcome": gate["outcome"],
                        "gate_record_hash": gate["gate_record_hash"],
                    }
                    alias_id = _mint_id(
                        "als3_",
                        {
                            "state_lineage_id": working.state_lineage_id,
                            "chapter_id": working.chapter_id,
                            "source_ticket_id": ticket_id,
                            "payload": alias_payload,
                        },
                    )
                    working._append_unique(
                        "aliases", "alias_id", _row_with_revision({"alias_id": alias_id, **alias_payload})
                    )
                else:
                    working._append_unique(
                        "tickets",
                        "ticket_id",
                        _alias_scope_ticket(
                            working=working,
                            source_fingerprint=request.request_fingerprint,
                            row_index=50000 + index,
                            surface=str(source["canonical_surface"]),
                            source_block_ids=source["support_block_ids"],
                            target_entity_id=target_id,
                            gate_record=gate,
                        ),
                    )
                _remove_row(working, "entities", "entity_id", source_id)
            elif action == "create_unnamed_entity":
                surface = _required_str(ticket.get("surface"), "unnamed creation surface")
                entity_payload = {
                    "canonical_surface": surface,
                    "name_class": None,
                    "referent_kind": disposition["resolved_referent_kind"],
                    "identity_summary": disposition["revised_identity_summary"],
                    "created_from_block_ids": list(ticket["source_block_ids"]),
                    "support_block_ids": list(ticket["source_block_ids"]),
                    "status": "confirmed",
                    "located_spans": [
                        {"block_id": block_id, **span}
                        for block_id in ticket["source_block_ids"]
                        for span in _all_exact_spans(source_catalog[block_id], surface)
                    ],
                }
                entity_id = _mint_id(
                    "ent3_",
                    {
                        "state_lineage_id": working.state_lineage_id,
                        "chapter_id": working.chapter_id,
                        "source_ticket_id": ticket_id,
                        "payload": entity_payload,
                    },
                )
                working._append_unique(
                    "entities",
                    "entity_id",
                    _row_with_revision({"entity_id": entity_id, **entity_payload}),
                )
            elif action == "promote_global_alias":
                surface = _required_str(ticket.get("surface"), "promoted alias surface")
                target_id = str(disposition["target_entity_id"])
                gate = route_alias_for_commit(
                    surface=surface,
                    name_class=str(disposition["name_class"]),
                    target_entity_id=target_id,
                    source_block_ids=ticket["source_block_ids"],
                    source_catalog=source_catalog,
                    source_decision_lineage={
                        "request_fingerprint": request.request_fingerprint,
                        "ticket_id": ticket_id,
                        "action": action,
                    },
                )
                working.alias_gate_records.append(gate)
                if gate["outcome"] == "eligible_global_alias":
                    payload = {
                        "surface": surface,
                        "name_class": disposition["name_class"],
                        "entity_id": target_id,
                        "support_block_ids": list(ticket["source_block_ids"]),
                        "status": "confirmed",
                        "gate_outcome": gate["outcome"],
                        "gate_record_hash": gate["gate_record_hash"],
                    }
                    alias_id = _mint_id(
                        "als3_",
                        {
                            "state_lineage_id": working.state_lineage_id,
                            "chapter_id": working.chapter_id,
                            "source_ticket_id": ticket_id,
                            "payload": payload,
                        },
                    )
                    working._append_unique(
                        "aliases", "alias_id", _row_with_revision({"alias_id": alias_id, **payload})
                    )
                else:
                    working._append_unique(
                        "tickets",
                        "ticket_id",
                        _alias_scope_ticket(
                            working=working,
                            source_fingerprint=request.request_fingerprint,
                            row_index=60000 + index,
                            surface=surface,
                            source_block_ids=ticket["source_block_ids"],
                            target_entity_id=target_id,
                            gate_record=gate,
                        ),
                    )
            elif action == "confirm_distinct_glossary":
                _replace_row(
                    working,
                    "glossary_items",
                    "glossary_id",
                    disposition["target_glossary_id"],
                    {"status": "confirmed"},
                )
            elif action == "merge_glossary":
                source_id = str(disposition["source_glossary_id"])
                target_id = str(disposition["target_glossary_id"])
                source = working._index("glossary_items", "glossary_id").get(source_id)
                target = working._index("glossary_items", "glossary_id").get(target_id)
                if source is None or target is None:
                    raise RegistryContractError("glossary merge source/target missing")
                _replace_row(
                    working,
                    "glossary_items",
                    "glossary_id",
                    target_id,
                    {
                        "support_block_ids": sorted(
                            set(target["support_block_ids"]) | set(source["support_block_ids"])
                        )
                    },
                )
                _remove_row(working, "glossary_items", "glossary_id", source_id)
            elif action == "revise_profile":
                changes: dict[str, Any] = {
                    "identity_summary": disposition["revised_identity_summary"]
                }
                if disposition["resolved_referent_kind"] is not None:
                    changes["referent_kind"] = disposition["resolved_referent_kind"]
                _replace_row(
                    working,
                    "entities",
                    "entity_id",
                    disposition["target_entity_id"],
                    changes,
                )
            elif action == "reject_noise":
                for entity_id in ticket.get("subject_entity_ids") or []:
                    if entity_id in working._index("entities", "entity_id"):
                        _remove_row(working, "entities", "entity_id", str(entity_id))
                for glossary_id in ticket.get("subject_glossary_ids") or []:
                    if glossary_id in working._index("glossary_items", "glossary_id"):
                        _remove_row(working, "glossary_items", "glossary_id", str(glossary_id))
            elif action == "remain_pending":
                for entity_id in ticket.get("subject_entity_ids") or []:
                    _replace_row(
                        working, "entities", "entity_id", str(entity_id), {"status": "pending"}
                    )
                for glossary_id in ticket.get("subject_glossary_ids") or []:
                    _replace_row(
                        working,
                        "glossary_items",
                        "glossary_id",
                        str(glossary_id),
                        {"status": "pending"},
                    )
            elif action == "defer_to_b2":
                if ticket.get("ticket_type") == "alias_scope_review":
                    for entity_id in ticket.get("subject_entity_ids") or []:
                        if entity_id in working._index("entities", "entity_id"):
                            _remove_row(working, "entities", "entity_id", str(entity_id))
            else:
                raise RegistryContractError(f"unimplemented audit action: {action}")

            ticket_changes = {
                "status": "carried" if action == "remain_pending" else "resolved",
                "resolution_action": action,
                "resolution_note": disposition["resolution_note"],
            }
            _replace_row(working, "tickets", "ticket_id", ticket_id, ticket_changes)
            outputs.append(_clone(disposition))
        working.auditor_request_fingerprints.append(request.request_fingerprint)

    for entity_id in sorted(working.clean_entity_ids):
        if entity_id in working._index("entities", "entity_id"):
            _replace_row(working, "entities", "entity_id", entity_id, {"status": "confirmed"})
    for glossary_id in sorted(working.clean_glossary_ids):
        if glossary_id in working._index("glossary_items", "glossary_id"):
            _replace_row(
                working, "glossary_items", "glossary_id", glossary_id, {"status": "confirmed"}
            )
    for row in list(working._state["entities"]):
        if row.get("status") == "provisional":
            _replace_row(working, "entities", "entity_id", row["entity_id"], {"status": "pending"})
    for row in list(working._state["glossary_items"]):
        if row.get("status") == "provisional":
            _replace_row(
                working,
                "glossary_items",
                "glossary_id",
                row["glossary_id"],
                {"status": "pending"},
            )
    working.revision_hash = _snapshot_revision(
        working.snapshot(), working.chapter_id, working.applied_request_fingerprints
    )
    return outputs


def _strip_runtime_only(row: Mapping[str, Any]) -> dict[str, Any]:
    result = _clone(row)
    result.pop("located_spans", None)
    return result


def build_registry_generation(
    *,
    working: ChapterWorkingRegistryV3,
    orientation: Mapping[str, Any],
    b0_request: RenderedRegistryRequestV3,
    source_catalog: Mapping[str, str],
    run_config: RunConfigV3,
    audit_decisions: Sequence[Mapping[str, Any]],
) -> PreparedRegistryGenerationV3:
    _validate_run_config_contract(run_config)
    aliases = working.snapshot()["aliases"]
    gate_hashes = {str(row["gate_record_hash"]) for row in working.alias_gate_records}
    for alias in aliases:
        if alias.get("gate_outcome") != "eligible_global_alias":
            raise RegistryContractError("published alias lacks eligible gate outcome")
        if str(alias.get("gate_record_hash")) not in gate_hashes:
            raise RegistryContractError("published alias lacks registered gate record")
    for table in ("entities", "glossary_items"):
        if any(row.get("status") == "provisional" for row in working.snapshot()[table]):
            raise RegistryContractError("provisional rows cannot be published")
    snapshot = working.snapshot()
    published_snapshot = {
        **{key: value for key, value in snapshot.items() if key != "snapshot_hash"},
        "generation_id": None,
        "entities": [_strip_runtime_only(row) for row in snapshot["entities"]],
        "aliases": [_strip_runtime_only(row) for row in snapshot["aliases"]],
        "glossary_items": [_strip_runtime_only(row) for row in snapshot["glossary_items"]],
        "tickets": [_strip_runtime_only(row) for row in snapshot["tickets"]],
    }
    published_snapshot["snapshot_hash"] = canonical_hash(published_snapshot)
    body = {
        "schema_version": REGISTRY_SCHEMA_VERSION,
        "state_lineage_id": working.state_lineage_id,
        "parent_generation_id": working.parent_generation_id,
        "chapter_id": working.chapter_id,
        "source_manifest_hash": working.source_manifest_hash,
        "source_block_catalog_hash": canonical_hash(source_catalog),
        "validator_version": VALIDATOR_VERSION,
        "prompt_versions": dict(PROMPT_IDS),
        "schema_versions": dict(run_config.schema_versions),
        "policy_versions": dict(run_config.policy_versions),
        "run_config_hash": run_config.config_hash,
        "b0_request_fingerprint": b0_request.request_fingerprint,
        "b1_request_fingerprints": list(working.applied_request_fingerprints),
        "candidate_selection_manifest_hashes": list(working.candidate_manifest_hashes),
        "targeted_recall_request_fingerprints": list(
            working.targeted_recall_request_fingerprints
        ),
        "auditor_request_fingerprints": list(working.auditor_request_fingerprints),
        "orientation_hash": canonical_hash(orientation),
        "alias_gate_records": _clone(working.alias_gate_records),
        "audit_decisions": _clone(list(audit_decisions)),
        "snapshot": published_snapshot,
    }
    commit_payload_hash = canonical_hash(body)
    generation_id = "reggen3_" + canonical_hash(
        {
            "state_lineage_id": working.state_lineage_id,
            "parent_generation_id": working.parent_generation_id,
            "chapter_id": working.chapter_id,
            "commit_payload_hash": commit_payload_hash,
        }
    )[:20]
    payload = {
        **body,
        "generation_id": generation_id,
        "commit_payload_hash": commit_payload_hash,
    }
    return PreparedRegistryGenerationV3(
        state_lineage_id=working.state_lineage_id,
        generation_id=generation_id,
        parent_generation_id=working.parent_generation_id,
        chapter_id=working.chapter_id,
        source_manifest_hash=working.source_manifest_hash,
        payload=payload,
    )


class ChapterRegistryStoreV3:
    def __init__(self, root: Path) -> None:
        self.root = Path(root).resolve()

    def _generation_path(self, generation_id: str) -> Path:
        if not re.fullmatch(r"reggen3_[0-9a-f]{20}", generation_id):
            raise RegistryStoreError("unsafe v3 generation id")
        return self.root / "generations" / f"{generation_id}.json"

    def _pointer_path(self, state_lineage_id: str) -> Path:
        return self.root / "current" / f"{canonical_hash({'state_lineage_id': state_lineage_id})}.json"

    def load_generation(self, generation_id: str) -> dict[str, Any]:
        path = self._generation_path(generation_id)
        if not path.is_file():
            raise RegistryStoreError(f"missing registry generation: {generation_id}")
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict) or payload.get("schema_version") != REGISTRY_SCHEMA_VERSION:
            raise RegistryStoreError("foreign generation cannot be loaded as v3")
        if payload.get("validator_version") != VALIDATOR_VERSION:
            raise RegistryStoreError("generation validator contract mismatch")
        own_hash = str(payload.get("commit_payload_hash") or "")
        body = {
            key: value
            for key, value in payload.items()
            if key not in {"generation_id", "commit_payload_hash"}
        }
        if canonical_hash(body) != own_hash:
            raise RegistryStoreError("generation commit payload hash mismatch")
        if payload.get("generation_id") != generation_id:
            raise RegistryStoreError("generation id/path mismatch")
        expected = "reggen3_" + canonical_hash(
            {
                "state_lineage_id": payload["state_lineage_id"],
                "parent_generation_id": payload["parent_generation_id"],
                "chapter_id": payload["chapter_id"],
                "commit_payload_hash": own_hash,
            }
        )[:20]
        if expected != generation_id:
            raise RegistryStoreError("generation identity mismatch")
        return payload

    def current_generation_id(self, state_lineage_id: str) -> str | None:
        path = self._pointer_path(state_lineage_id)
        if not path.is_file():
            return None
        pointer = json.loads(path.read_text(encoding="utf-8"))
        if pointer.get("schema_version") != REGISTRY_SCHEMA_VERSION:
            raise RegistryStoreError("v2/foreign pointer cannot be read as v3")
        if pointer.get("state_lineage_id") != state_lineage_id:
            raise RegistryStoreError("registry pointer lineage mismatch")
        generation_id = _required_str(pointer.get("generation_id"), "generation_id")
        generation = self.load_generation(generation_id)
        if generation.get("state_lineage_id") != state_lineage_id:
            raise RegistryStoreError("pointer targets foreign lineage")
        return generation_id

    def snapshot(
        self, state_lineage_id: str, generation_id: str | None = None
    ) -> dict[str, Any]:
        actual = generation_id or self.current_generation_id(state_lineage_id)
        if actual is None:
            return empty_registry_snapshot_v3(state_lineage_id)
        generation = self.load_generation(actual)
        snapshot = _clone(generation["snapshot"])
        if snapshot.get("state_lineage_id") != state_lineage_id:
            raise RegistryStoreError("snapshot crosses state lineage")
        snapshot["generation_id"] = actual
        snapshot["snapshot_hash"] = canonical_hash(
            {key: value for key, value in snapshot.items() if key != "snapshot_hash"}
        )
        return snapshot

    def commit(
        self,
        generation: PreparedRegistryGenerationV3,
        *,
        expected_parent: str | None,
        before_pointer_switch: Callable[[], None] | None = None,
    ) -> None:
        if generation.parent_generation_id != expected_parent:
            raise RegistryStaleParentError("generation parent differs from CAS expectation")
        path = self._generation_path(generation.generation_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        encoded = (canonical_json(generation.to_dict()) + "\n").encode("utf-8")
        try:
            with path.open("xb") as handle:
                handle.write(encoded)
                handle.flush()
                os.fsync(handle.fileno())
        except FileExistsError:
            if path.read_bytes() != encoded:
                raise RegistryStoreError("generation id collision with unequal bytes")
        lock_root = self.root / "lineage_locks" / canonical_hash(
            {"state_lineage_id": generation.state_lineage_id}
        )
        with CheckpointLock(lock_root):
            current = self.current_generation_id(generation.state_lineage_id)
            if current != expected_parent:
                raise RegistryStaleParentError(
                    f"stale registry parent: expected {expected_parent}, current {current}"
                )
            if before_pointer_switch is not None:
                before_pointer_switch()
            write_checkpoint_atomic(
                self._pointer_path(generation.state_lineage_id),
                {
                    "schema_version": REGISTRY_SCHEMA_VERSION,
                    "state_lineage_id": generation.state_lineage_id,
                    "generation_id": generation.generation_id,
                },
            )


def build_b2_candidate_manifest(
    *,
    chapter_id: str,
    active_blocks: Sequence[Mapping[str, Any]],
    registry_snapshot: Mapping[str, Any],
    candidate_count_cap: int,
) -> dict[str, Any]:
    if registry_snapshot.get("schema_version") != REGISTRY_SCHEMA_VERSION:
        raise RegistryContractError("B2 preview requires a v3 registry snapshot")
    active_ids = {str(row["block_id"]) for row in active_blocks}
    aliases_by_entity: dict[str, list[str]] = defaultdict(list)
    for alias in registry_snapshot.get("aliases") or []:
        if alias.get("status") == "confirmed":
            aliases_by_entity[str(alias["entity_id"])].append(str(alias["surface"]))
    entities = {
        str(row["entity_id"]): row
        for row in registry_snapshot.get("entities") or []
        if row.get("status") == "confirmed"
    }
    links: list[dict[str, Any]] = []
    for entity_id, entity in entities.items():
        for block in active_blocks:
            block_id = str(block["block_id"])
            text = _block_text(block)
            stable_surfaces = aliases_by_entity[entity_id]
            if entity.get("name_class") is not None:
                stable_surfaces = [str(entity["canonical_surface"])] + stable_surfaces
            for surface in stable_surfaces:
                match = _match_known_surface(text, surface)
                if match is not None:
                    links.append(
                        {
                            "entity_id": entity_id,
                            "block_id": block_id,
                            "candidate_source": "surface_match",
                            "matched_surfaces": [match[1]],
                            "support_block_ids": [],
                            "source_ticket_ids": [],
                        }
                    )
            if block_id in set(entity.get("support_block_ids") or []):
                links.append(
                    {
                        "entity_id": entity_id,
                        "block_id": block_id,
                        "candidate_source": "support_block",
                        "matched_surfaces": [],
                        "support_block_ids": [block_id],
                        "source_ticket_ids": [],
                    }
                )
    for ticket in registry_snapshot.get("tickets") or []:
        if ticket.get("resolution_action") != "defer_to_b2":
            continue
        for block_id in set(ticket.get("source_block_ids") or []) & active_ids:
            for entity_id in ticket.get("candidate_entity_ids") or []:
                if entity_id in entities:
                    links.append(
                        {
                            "entity_id": entity_id,
                            "block_id": block_id,
                            "candidate_source": "deferred_ticket",
                            "matched_surfaces": [],
                            "support_block_ids": [],
                            "source_ticket_ids": [ticket["ticket_id"]],
                        }
                    )
    dedup: dict[str, dict[str, Any]] = {}
    for link in links:
        key = canonical_hash(link)
        dedup[key] = link
    ordered_links = sorted(
        dedup.values(),
        key=lambda row: (row["block_id"], row["entity_id"], row["candidate_source"]),
    )
    candidate_ids = sorted({row["entity_id"] for row in ordered_links})
    selected_ids = candidate_ids[:candidate_count_cap]
    excluded_ids = candidate_ids[candidate_count_cap:]
    selected_set = set(selected_ids)
    body = {
        "policy_version": B2_RESCAN_POLICY_VERSION,
        "chapter_id": chapter_id,
        "active_block_ids": sorted(active_ids),
        "registry_generation_id": registry_snapshot.get("generation_id"),
        "candidate_entity_ids": selected_ids,
        "candidate_links": [row for row in ordered_links if row["entity_id"] in selected_set],
        "pre_cap_count": len(candidate_ids),
        "selected_count": len(selected_ids),
        "excluded_entity_ids": excluded_ids,
        "overflow": bool(excluded_ids),
        "authoritative_bindings": [],
    }
    body["manifest_hash"] = canonical_hash(body)
    return body


@dataclass
class SyntheticRegistryExecutorV3:
    responses: dict[str, list[Mapping[str, Any]]] = field(default_factory=dict)
    calls: list[dict[str, Any]] = field(default_factory=list)

    def add(self, role: str, response: Mapping[str, Any]) -> None:
        self.responses.setdefault(role, []).append(_clone(response))

    def execute(self, request: RenderedRegistryRequestV3) -> dict[str, Any]:
        queue = self.responses.get(request.role) or []
        if not queue:
            raise RegistryContractError(f"no synthetic response queued for {request.role}")
        response = dict(queue.pop(0))
        self.calls.append(
            {
                "role": request.role,
                "request_fingerprint": request.request_fingerprint,
                "response_hash": canonical_hash(response),
            }
        )
        return response


def run_synthetic_registry_chapter_v3(
    *,
    chapter: Mapping[str, Any],
    state_lineage_id: str,
    parent_snapshot: Mapping[str, Any],
    executor: SyntheticRegistryExecutorV3,
    design_doc: Path,
    run_config: RunConfigV3,
    store: ChapterRegistryStoreV3 | None = None,
) -> dict[str, Any]:
    chapter_id = _required_str(chapter.get("chapter_id"), "chapter_id")
    source_hash = chapter_source_manifest_hash(chapter)
    source_catalog = {str(row["block_id"]): _block_text(row) for row in _chapter_blocks(chapter)}
    b0_request = render_b0_request(chapter=chapter, design_doc=design_doc, run_config=run_config)
    orientation = validate_orientation_response(executor.execute(b0_request), chapter)
    working = ChapterWorkingRegistryV3.create(
        state_lineage_id=state_lineage_id,
        chapter_id=chapter_id,
        source_manifest_hash=source_hash,
        parent_generation_id=parent_snapshot.get("generation_id"),
        parent_snapshot=parent_snapshot,
    )
    working.ingest_code_ticket_proposals(
        orientation["code_ticket_proposals"], source_fingerprint=b0_request.request_fingerprint
    )
    windows = build_registry_windows(
        chapter,
        target_tokens=run_config.b1_window_target_tokens,
        max_blocks=run_config.b1_window_max_blocks,
        preceding_tail_k=run_config.context_only_tail_k,
    )
    block_order = {
        str(row["block_id"]): int(row.get("order_index") or 0)
        for row in _chapter_blocks(chapter)
    }
    requests: list[RenderedRegistryRequestV3] = [b0_request]
    for window in windows:
        request = render_b1_request(
            chapter_id=chapter_id,
            window_id=str(window["window_id"]),
            orientation=orientation,
            active_blocks=window["blocks"],
            context_only_tail=window["context_only_tail"],
            working=working,
            block_order=block_order,
            design_doc=design_doc,
            run_config=run_config,
        )
        working.apply_delta(request, executor.execute(request))
        requests.append(request)
    targeted = schedule_targeted_recall(
        orientation=orientation,
        working_snapshot=working.snapshot(),
        windows=windows,
        call_cap=run_config.targeted_recall_call_cap,
    )
    windows_by_id = {str(row["window_id"]): row for row in windows}
    for target in targeted:
        window = windows_by_id[str(target["window_id"])]
        request = render_b1_request(
            chapter_id=chapter_id,
            window_id=f"{window['window_id']}:targeted",
            orientation=orientation,
            active_blocks=window["blocks"],
            context_only_tail=window["context_only_tail"],
            working=working,
            block_order=block_order,
            design_doc=design_doc,
            run_config=run_config,
            targeted_checklist_rows=target["missing_checklist_rows"],
        )
        working.apply_delta(request, executor.execute(request), targeted_recall=True)
        requests.append(request)
    post_target_coverage = checklist_coverage(orientation, working.snapshot())
    if post_target_coverage["missing_count"]:
        raise RegistryContractError(
            "targeted recall completed with uncovered checklist rows: "
            + ",".join(
                str(row["checklist_id"]) for row in post_target_coverage["missing_rows"]
            )
        )
    exception_budget = enforce_exception_budget(
        working=working,
        b1_call_count=len(windows) + len(targeted),
        run_config=run_config,
    )
    auditor_requests = render_auditor_requests(
        chapter=chapter,
        orientation=orientation,
        working=working,
        design_doc=design_doc,
        run_config=run_config,
    )
    audit_pairs = [(request, executor.execute(request)) for request in auditor_requests]
    requests.extend(auditor_requests)
    audits = apply_audit_responses(
        working=working, request_response_pairs=audit_pairs, source_catalog=source_catalog
    )
    generation = build_registry_generation(
        working=working,
        orientation=orientation,
        b0_request=b0_request,
        source_catalog=source_catalog,
        run_config=run_config,
        audit_decisions=audits,
    )
    if store is not None:
        store.commit(generation, expected_parent=working.parent_generation_id)
        final_snapshot = store.snapshot(state_lineage_id, generation.generation_id)
    else:
        final_snapshot = _clone(generation.payload["snapshot"])
        final_snapshot["generation_id"] = generation.generation_id
        final_snapshot["snapshot_hash"] = canonical_hash(
            {key: value for key, value in final_snapshot.items() if key != "snapshot_hash"}
        )
    b2_preview = build_b2_candidate_manifest(
        chapter_id=chapter_id,
        active_blocks=[row for window in windows for row in window["blocks"]],
        registry_snapshot=final_snapshot,
        candidate_count_cap=run_config.candidate_card_count_cap,
    )
    return {
        "orientation": orientation,
        "windows": windows,
        "requests": [request.to_dict() for request in requests],
        "targeted_recall": targeted,
        "post_target_coverage": post_target_coverage,
        "exception_budget": exception_budget,
        "audits": audits,
        "generation": generation.to_dict(),
        "snapshot": final_snapshot,
        "b2_candidate_preview": b2_preview,
    }


__all__ = [
    "ChapterRegistryStoreV3",
    "ChapterWorkingRegistryV3",
    "SyntheticRegistryExecutorV3",
    "apply_audit_responses",
    "build_b2_candidate_manifest",
    "build_exception_components",
    "enforce_exception_budget",
    "build_registry_generation",
    "build_registry_windows",
    "chapter_source_manifest_hash",
    "checklist_coverage",
    "empty_registry_snapshot_v3",
    "estimate_registry_prompt_tokens",
    "render_auditor_requests",
    "render_b0_request",
    "render_b1_request",
    "route_alias_for_commit",
    "run_synthetic_registry_chapter_v3",
    "schedule_targeted_recall",
    "select_candidate_packets",
    "validate_audit_response",
    "validate_orientation_response",
]
