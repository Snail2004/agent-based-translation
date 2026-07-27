from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass, field
import hashlib
import json
import os
from pathlib import Path
import re
import unicodedata
from typing import Any, Callable, Iterable, Mapping, Sequence

from pipeline.literary.builder_pilot import (
    RESPONSE_FORMAT_JSON,
    load_system_prompt_from_design,
)
from pipeline.literary.checkpoint import (
    CheckpointLock,
    canonical_hash,
    canonical_json,
    write_checkpoint_atomic,
)
from pipeline.literary.chapter_registry_schema_v2 import (
    ALIAS_SCOPE_POLICY_VERSION,
    ALIAS_TYPES,
    AUDIT_LISTS,
    AUDIT_SCHEMA_VERSION,
    B2_RESCAN_POLICY_VERSION,
    CANDIDATE_POLICY_VERSION,
    CLEAN_POLICY_VERSION,
    DELTA_LISTS,
    DELTA_SCHEMA_VERSION,
    GLOSSARY_CATEGORIES,
    MENTION_TYPES,
    ORIENTATION_SCHEMA_VERSION,
    PROMPT_IDS,
    PreparedRegistryGenerationV2,
    REFERENT_KINDS,
    REGISTRY_SCHEMA_VERSION,
    RegistryBudgetError,
    RegistryContractError,
    RegistryStaleParentError,
    RegistryStaleRevisionError,
    RegistryStoreError,
    RenderedRegistryRequestV2,
    RunConfigV2,
    TICKET_TYPES,
)


VALIDATOR_VERSION = "chapter_registry_validator_v2_2"
_SURFACE_COMMIT_OUTCOMES = frozenset(
    {"global_alias_candidate", "downscope_local", "pending_scope_review"}
)
_PRONOUNS = frozenset(
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
_DESCRIPTOR_PREFIXES = (
    "the ",
    "a ",
    "an ",
    "this ",
    "that ",
    "these ",
    "those ",
    "my ",
    "your ",
    "his ",
    "her ",
    "our ",
    "their ",
)


def estimate_registry_prompt_tokens(messages: Sequence[Mapping[str, Any]]) -> int:
    request = {"messages": list(messages), "response_format": RESPONSE_FORMAT_JSON}
    return max(1, len(canonical_json(request)) // 4)


def _clone(value: Any) -> Any:
    return json.loads(canonical_json(value))


def _nfc(value: Any) -> str:
    return unicodedata.normalize("NFC", str(value or ""))


def _required_str(value: Any, label: str) -> str:
    text = _nfc(value).strip()
    if not text:
        raise RegistryContractError(f"{label} must be a non-empty string")
    return text


def _require_exact_keys(value: Mapping[str, Any], expected: set[str], label: str) -> None:
    actual = set(value)
    if actual != expected:
        raise RegistryContractError(
            f"{label} fields mismatch: expected {sorted(expected)}, got {sorted(actual)}"
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
    if len({str(row["block_id"]) for row in rows}) != len(rows):
        raise RegistryContractError("chapter contains duplicate block ids")
    return rows


def chapter_source_manifest_hash(chapter: Mapping[str, Any]) -> str:
    return canonical_hash(
        [
            {
                "block_id": str(row.get("block_id") or ""),
                "order_index": int(row.get("order_index") or 0),
                "block_type": str(row.get("block_type") or ""),
                "text": _block_text(row),
            }
            for row in _chapter_blocks(chapter)
        ]
    )


def _normalized_literal(value: Any) -> str:
    text = _nfc(value).casefold()
    text = re.sub(r"[^\w]+", " ", text, flags=re.UNICODE)
    return " ".join(text.split())


def _is_pronoun(surface: str) -> bool:
    return _normalized_literal(surface) in _PRONOUNS


def _looks_descriptor(surface: str) -> bool:
    folded = _nfc(surface).strip().casefold()
    return any(folded.startswith(prefix) for prefix in _DESCRIPTOR_PREFIXES)


def _all_exact_spans(text: str, surface: str) -> list[dict[str, int]]:
    spans: list[dict[str, int]] = []
    start = 0
    while True:
        index = text.find(surface, start)
        if index < 0:
            break
        spans.append({"char_start": index, "char_end": index + len(surface)})
        start = index + max(1, len(surface))
    return spans


def _validate_run_config_contract(run_config: RunConfigV2) -> None:
    if run_config.validator_version != VALIDATOR_VERSION:
        raise RegistryContractError(
            f"validator contract mismatch: expected {VALIDATOR_VERSION}"
        )
    expected_policies = {
        "candidate_selection": CANDIDATE_POLICY_VERSION,
        "clean_commit": CLEAN_POLICY_VERSION,
        "b2_rescan": B2_RESCAN_POLICY_VERSION,
        "alias_scope": ALIAS_SCOPE_POLICY_VERSION,
    }
    for policy_name, policy_version in expected_policies.items():
        if run_config.policy_versions.get(policy_name) != policy_version:
            raise RegistryContractError(
                f"policy contract mismatch for {policy_name}: expected {policy_version}"
            )
    if dict(run_config.prompt_versions) != PROMPT_IDS:
        raise RegistryContractError("prompt version contract mismatch")


def _source_block_catalog(chapter: Mapping[str, Any]) -> dict[str, str]:
    return {str(row["block_id"]): _block_text(row) for row in _chapter_blocks(chapter)}


def _sentence_initial(text: str, char_start: int) -> bool:
    prefix = text[:char_start].rstrip()
    opening = "\"'([{\u2018\u201c"
    while prefix and prefix[-1] in opening:
        prefix = prefix[:-1].rstrip()
    return not prefix or prefix[-1] in ".!?"


def _name_form_position(text: str, char_start: int) -> bool:
    """Return whether an occurrence follows another visibly cased name-form token."""

    prefix = text[:char_start].rstrip()
    match = re.search(r"([^\W\d_][^\W_]*)[.\-']?$", prefix, flags=re.UNICODE)
    if match is None:
        return False
    token = match.group(1)
    return any(char.isupper() for char in token)


def _located_surface_spans(
    *, surface: str, support_block_ids: Sequence[str], source_catalog: Mapping[str, str]
) -> list[dict[str, Any]]:
    spans: list[dict[str, Any]] = []
    for block_id in support_block_ids:
        text = source_catalog.get(str(block_id))
        if text is None:
            continue
        for span in _all_exact_spans(text, surface):
            spans.append(
                {
                    "block_id": str(block_id),
                    **span,
                    "sentence_initial": _sentence_initial(text, span["char_start"]),
                    "name_form_position": _name_form_position(text, span["char_start"]),
                }
            )
    return spans


def route_surface_for_commit(
    *,
    surface: str,
    alias_type: str | None,
    target_entity_id: str | None,
    support_block_ids: Sequence[str],
    located_source_spans: Sequence[Mapping[str, Any]],
    source_status: str,
    source_origin: str,
) -> dict[str, Any]:
    """Mechanically route one surface without deciding its linguistic meaning."""

    checked_surface = _required_str(surface, "surface commit surface")
    checked_support = _require_string_list(
        list(support_block_ids), "surface commit support blocks"
    )
    checked_status = _require_enum(
        source_status, {"provisional", "confirmed", "pending"}, "surface source status"
    )
    checked_origin = _required_str(source_origin, "surface source origin")
    if alias_type is not None:
        checked_alias_type: str | None = _require_enum(
            alias_type, ALIAS_TYPES, "surface alias_type"
        )
    else:
        checked_alias_type = None

    spans = sorted(
        [
            {
                "block_id": _required_str(row.get("block_id"), "surface span block_id"),
                "char_start": int(row.get("char_start")),
                "char_end": int(row.get("char_end")),
                "sentence_initial": bool(row.get("sentence_initial")),
                "name_form_position": bool(row.get("name_form_position")),
            }
            for row in located_source_spans
        ],
        key=lambda row: (row["block_id"], row["char_start"], row["char_end"]),
    )
    cased_positions = [index for index, char in enumerate(checked_surface) if char.isalpha()]
    uppercase_positions = [index for index, char in enumerate(checked_surface) if char.isupper()]
    first_cased = cased_positions[0] if cased_positions else None
    internal_uppercase = any(index != first_cased for index in uppercase_positions)
    noninitial_occurrence = any(not row["sentence_initial"] for row in spans)
    cased_name_form = any(row["name_form_position"] for row in spans)
    proper_name_signal = bool(uppercase_positions) and (
        internal_uppercase or noninitial_occurrence or cased_name_form
    )
    sentence_initial_only = bool(uppercase_positions) and bool(spans) and not proper_name_signal

    if not target_entity_id or not spans:
        outcome = "pending_scope_review"
        reason_code = "missing_target_or_exact_source_span"
    elif checked_alias_type is None:
        outcome = "downscope_local"
        reason_code = "identity_merge_does_not_establish_alias_type"
    elif not proper_name_signal:
        outcome = "downscope_local"
        reason_code = (
            "sentence_initial_name_signal_is_ambiguous"
            if sentence_initial_only
            else "no_independent_proper_name_signal"
        )
    else:
        outcome = "global_alias_candidate"
        reason_code = "orthographically_eligible_for_semantic_alias_decision"

    if outcome not in _SURFACE_COMMIT_OUTCOMES:
        raise RegistryContractError("surface commit gate produced an invalid outcome")
    result = {
        "surface": checked_surface,
        "alias_type": checked_alias_type,
        "target_entity_id": (str(target_entity_id) if target_entity_id else None),
        "support_block_ids": checked_support,
        "source_status": checked_status,
        "source_origin": checked_origin,
        "proper_name_signal": proper_name_signal,
        "sentence_initial_only": sentence_initial_only,
        "located_source_spans": spans,
        "outcome": outcome,
        "reason_code": reason_code,
    }
    result["plan_hash"] = canonical_hash(result)
    return result


def _bounded_literal_search(
    text: str, surface: str, *, ignore_case: bool
) -> re.Match[str] | None:
    if not surface:
        return None
    prefix = r"(?<!\w)" if re.match(r"\w", surface[0], flags=re.UNICODE) else ""
    suffix = r"(?!\w)" if re.match(r"\w", surface[-1], flags=re.UNICODE) else ""
    flags = re.IGNORECASE | re.UNICODE if ignore_case else re.UNICODE
    return re.search(prefix + re.escape(surface) + suffix, text, flags=flags)


def _literal_match(text: str, registry_surface: str) -> tuple[str, str, str] | None:
    exact_match = _bounded_literal_search(text, registry_surface, ignore_case=False)
    if exact_match is not None:
        return "exact", "verbatim literal", exact_match.group(0)
    folded_match = _bounded_literal_search(text, registry_surface, ignore_case=True)
    if folded_match is not None:
        return (
            "casefold",
            "case-insensitive literal",
            folded_match.group(0),
        )
    normalized_surface = _normalized_literal(registry_surface)
    normalized_text = _normalized_literal(text)
    normalized_match = _bounded_literal_search(
        normalized_text, normalized_surface, ignore_case=False
    )
    if normalized_match is not None:
        return (
            "normalized",
            "NFC/whitespace/punctuation-normalized literal",
            normalized_match.group(0),
        )
    tokens = normalized_surface.split()
    for token in tokens:
        if len(token) < 3:
            continue
        source_match = re.search(
            rf"(?<!\w){re.escape(token)}(?!\w)", text, flags=re.IGNORECASE | re.UNICODE
        )
        if source_match is not None:
            return (
                "token_containment",
                "token-boundary containment",
                source_match.group(0),
            )
    return None


def _literal_channel(text: str, registry_surface: str) -> tuple[str, str] | None:
    match = _literal_match(text, registry_surface)
    if match is None:
        return None
    return match[0], match[1]


def empty_registry_snapshot_v2(state_lineage_id: str) -> dict[str, Any]:
    lineage = _required_str(state_lineage_id, "state_lineage_id")
    body = {
        "builder_identity_mode": REGISTRY_SCHEMA_VERSION,
        "schema_version": REGISTRY_SCHEMA_VERSION,
        "state_lineage_id": lineage,
        "generation_id": None,
        "entities": [],
        "aliases": [],
        "glossary_items": [],
        "local_bindings": [],
        "tickets": [],
    }
    body["snapshot_hash"] = canonical_hash(body)
    return body


def _snapshot_revision(snapshot: Mapping[str, Any], chapter_id: str, applied: Sequence[str]) -> str:
    return "work2_" + canonical_hash(
        {
            "chapter_id": chapter_id,
            "parent_snapshot_hash": snapshot.get("snapshot_hash"),
            "entities": snapshot.get("entities") or [],
            "aliases": snapshot.get("aliases") or [],
            "glossary_items": snapshot.get("glossary_items") or [],
            "local_bindings": snapshot.get("local_bindings") or [],
            "tickets": snapshot.get("tickets") or [],
            "applied_request_fingerprints": list(applied),
        }
    )[:24]


def _messages(prompt_text: str, payload: Mapping[str, Any]) -> tuple[dict[str, str], ...]:
    return (
        {"role": "system", "content": prompt_text},
        {"role": "user", "content": canonical_json(payload)},
    )


def _render_request(
    *,
    role: str,
    prompt_id: str,
    prompt_text: str,
    chapter_id: str,
    window_id: str | None,
    parent_working_revision_hash: str | None,
    sections: Mapping[str, Any],
    run_config_hash: str,
    model_contract: Mapping[str, Any],
    source_manifest: Mapping[str, Any],
) -> RenderedRegistryRequestV2:
    prompt_sha = hashlib.sha256(prompt_text.encode("utf-8")).hexdigest()
    payload = {
        "schema_version": REGISTRY_SCHEMA_VERSION,
        "role": role,
        "chapter_id": chapter_id,
        "window_id": window_id,
        "working_registry_revision_hash": parent_working_revision_hash,
        "allowlisted_sections": _clone(sections),
    }
    messages = _messages(prompt_text, payload)
    fingerprint = canonical_hash(
        {
            "role": role,
            "chapter_id": chapter_id,
            "window_id": window_id,
            "prompt_id": prompt_id,
            "prompt_sha256": prompt_sha,
            "model_contract": _clone(model_contract),
            "source_manifest": _clone(source_manifest),
            "parent_working_revision_hash": parent_working_revision_hash,
            "sections_hash": canonical_hash(sections),
            "output_schema_version": {
                "b0": ORIENTATION_SCHEMA_VERSION,
                "b1": DELTA_SCHEMA_VERSION,
                "auditor": AUDIT_SCHEMA_VERSION,
            }[role],
            "run_config_hash": run_config_hash,
        }
    )
    return RenderedRegistryRequestV2(
        role=role,
        prompt_id=prompt_id,
        prompt_sha256=prompt_sha,
        chapter_id=chapter_id,
        window_id=window_id,
        parent_working_revision_hash=parent_working_revision_hash,
        sections=_clone(sections),
        messages=messages,
        request_fingerprint=fingerprint,
    )


def render_b0_request(
    *,
    chapter: Mapping[str, Any],
    design_doc: Path,
    run_config: RunConfigV2,
) -> RenderedRegistryRequestV2:
    _validate_run_config_contract(run_config)
    chapter_id = _required_str(chapter.get("chapter_id"), "chapter_id")
    blocks = [_block_view(row) for row in _chapter_blocks(chapter)]
    prompt = load_system_prompt_from_design(design_doc, PROMPT_IDS["b0"])
    request = _render_request(
        role="b0",
        prompt_id=PROMPT_IDS["b0"],
        prompt_text=prompt,
        chapter_id=chapter_id,
        window_id=None,
        parent_working_revision_hash=None,
        sections={"source_blocks": blocks},
        run_config_hash=run_config.config_hash,
        model_contract={
            "model_id": run_config.b0_model_id,
            "reasoning_effort": run_config.b0_reasoning_effort,
            "temperature": run_config.b0_temperature,
            "seed": run_config.b0_seed,
            "verbosity": run_config.b0_verbosity,
            "max_output_tokens": run_config.b0_output_cap,
        },
        source_manifest={
            "chapter_source_manifest_hash": chapter_source_manifest_hash(chapter),
            "block_ids": [row["block_id"] for row in blocks],
        },
    )
    token_count = estimate_registry_prompt_tokens(request.messages)
    if token_count > run_config.b0_input_cap:
        raise RegistryBudgetError(
            f"B0 input {token_count} exceeds cap {run_config.b0_input_cap}; sharding is deferred"
        )
    return request


def validate_orientation_response(
    response: Mapping[str, Any], chapter: Mapping[str, Any]
) -> dict[str, Any]:
    _require_exact_keys(
        response,
        {"gist", "narrator_hypotheses", "salient_surface_checklist"},
        "ChapterOrientationV2",
    )
    gist = _required_str(response.get("gist"), "orientation gist")
    block_catalog = {str(row["block_id"]): _block_text(row) for row in _chapter_blocks(chapter)}
    hypotheses: list[dict[str, Any]] = []
    for raw in _require_list(response.get("narrator_hypotheses"), "narrator_hypotheses"):
        if not isinstance(raw, Mapping):
            raise RegistryContractError("narrator hypothesis must be an object")
        _require_exact_keys(raw, {"surface", "note", "block_ids"}, "narrator hypothesis")
        surface = raw.get("surface")
        if surface is not None:
            surface = _required_str(surface, "narrator surface")
        block_ids = _require_string_list(raw.get("block_ids"), "narrator block_ids")
        if not set(block_ids) <= set(block_catalog):
            raise RegistryContractError("narrator hypothesis cites foreign block")
        hypotheses.append(
            {
                "surface": surface,
                "note": _required_str(raw.get("note"), "narrator note"),
                "block_ids": block_ids,
            }
        )
    checklist: list[dict[str, Any]] = []
    code_audit_rows: list[dict[str, Any]] = []
    for raw in _require_list(response.get("salient_surface_checklist"), "checklist"):
        if not isinstance(raw, Mapping):
            raise RegistryContractError("checklist row must be an object")
        _require_exact_keys(raw, {"surface", "block_id"}, "checklist row")
        surface = _required_str(raw.get("surface"), "checklist surface")
        block_id = _required_str(raw.get("block_id"), "checklist block_id")
        if block_id not in block_catalog:
            raise RegistryContractError("checklist row cites foreign block")
        spans = _all_exact_spans(block_catalog[block_id], surface)
        row = {"surface": surface, "block_id": block_id}
        checklist.append(row)
        if not spans:
            code_audit_rows.append(
                {
                    "ticket_type": "unlocatable_surface",
                    "surface": surface,
                    "block_id": block_id,
                    "note": "B0 checklist surface was not verbatim in its declared block",
                }
            )
    return {
        "gist": gist,
        "narrator_hypotheses": hypotheses,
        "salient_surface_checklist": checklist,
        "code_audit_rows": code_audit_rows,
    }


def _entity_card(row: Mapping[str, Any], aliases: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    return {
        "entity_id": row["entity_id"],
        "canonical_surface": row["canonical_surface"],
        "referent_kind": row["referent_kind"],
        "aliases": sorted(
            str(alias["surface"]) for alias in aliases if alias.get("entity_id") == row["entity_id"]
        ),
        "identity_summary": row["identity_summary"],
        "status": row["status"],
    }


def _glossary_card(row: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "glossary_id": row["glossary_id"],
        "surface": row["surface"],
        "category_claim": row["category_claim"],
        "short_description": row["short_description"],
        "status": row["status"],
    }


def _build_prejoined_candidate_context(
    *,
    selected: Sequence[Mapping[str, Any]],
    links_by_row: Mapping[tuple[str, str], Sequence[Mapping[str, Any]]],
    overflow_sources: set[str],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    selected_by_key = {
        (str(row["registry_row_type"]), str(row["registry_row_id"])): row
        for row in selected
    }
    packets_by_surface: dict[str, dict[str, Any]] = {}
    for key in sorted(links_by_row):
        selected_row = selected_by_key.get(key)
        for raw_link in links_by_row[key]:
            link = dict(raw_link)
            source_surface = _required_str(link.get("source_surface"), "candidate source surface")
            packet = packets_by_surface.setdefault(
                source_surface,
                {
                    "source_block_ids": set(),
                    "candidates": {},
                },
            )
            packet["source_block_ids"].update(
                _require_string_list(link.get("source_block_ids"), "candidate source block ids")
            )
            if selected_row is None:
                continue
            packet["candidates"].setdefault(
                key,
                {
                    "registry_row_type": key[0],
                    "candidate_card": _clone(selected_row["card"]),
                },
            )

    packets: list[dict[str, Any]] = []
    for source_surface in sorted(
        packets_by_surface, key=lambda item: (_normalized_literal(item), item)
    ):
        raw_packet = packets_by_surface[source_surface]
        candidates = []
        for key in sorted(raw_packet["candidates"]):
            candidate = raw_packet["candidates"][key]
            candidates.append(
                {
                    "registry_row_type": candidate["registry_row_type"],
                    "candidate_card": candidate["candidate_card"],
                }
            )
        body = {
            "source_surface": source_surface,
            "source_block_ids": sorted(raw_packet["source_block_ids"]),
            "candidate_overflow": source_surface in overflow_sources,
            "candidates": candidates,
        }
        packets.append(body)

    unmatched_recency_cards = [
        {
            "registry_row_type": str(row["registry_row_type"]),
            "registry_row_id": str(row["registry_row_id"]),
            "candidate_card": _clone(row["card"]),
            "row_hash": str(row["row_hash"]),
        }
        for row in selected
        if not bool(row["matched"])
    ]
    return packets, unmatched_recency_cards


def select_candidate_cards(
    *,
    snapshot: Mapping[str, Any],
    working_revision_hash: str,
    active_blocks: Sequence[Mapping[str, Any]],
    context_only_tail: Sequence[Mapping[str, Any]],
    block_order: Mapping[str, int],
    recency_k: int,
    card_count_cap: int,
    card_token_cap: int,
) -> dict[str, Any]:
    active_views = [_block_view(row) for row in active_blocks]
    tail_views = [_block_view(row, context_only=True) for row in context_only_tail]
    searchable = active_views + tail_views
    aliases = list(snapshot.get("aliases") or [])
    rows: list[dict[str, Any]] = []
    links_by_row: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    matched_keys: set[tuple[str, str]] = set()
    overflow_sources: set[str] = set()

    entity_by_id = {str(row["entity_id"]): row for row in snapshot.get("entities") or []}
    for entity_id, entity in entity_by_id.items():
        surfaces = [str(entity["canonical_surface"])] + [
            str(row["surface"]) for row in aliases if row.get("entity_id") == entity_id
        ]
        for block in searchable:
            for registry_surface in surfaces:
                match = _literal_match(block["text"], registry_surface)
                if match is None:
                    continue
                channel, reason, source_surface = match
                key = ("entity", entity_id)
                matched_keys.add(key)
                links_by_row[key].append(
                    {
                        "source_surface": source_surface,
                        "source_block_ids": [block["block_id"]],
                        "registry_row_type": "entity",
                        "registry_row_id": entity_id,
                        "matched_registry_surface": registry_surface,
                        "channel": channel,
                        "match_reason": reason,
                    }
                )

    glossary_by_id = {
        str(row["glossary_id"]): row for row in snapshot.get("glossary_items") or []
    }
    for glossary_id, item in glossary_by_id.items():
        for block in searchable:
            match = _literal_match(block["text"], str(item["surface"]))
            if match is None:
                continue
            channel, reason, source_surface = match
            key = ("glossary", glossary_id)
            matched_keys.add(key)
            links_by_row[key].append(
                {
                    "source_surface": source_surface,
                    "source_block_ids": [block["block_id"]],
                    "registry_row_type": "glossary",
                    "registry_row_id": glossary_id,
                    "matched_registry_surface": str(item["surface"]),
                    "channel": channel,
                    "match_reason": reason,
                }
            )

    first_active_order = min((int(row["order_index"]) for row in active_views), default=0)
    recency_keys: set[tuple[str, str]] = set()
    if recency_k:
        lower = first_active_order - recency_k
        for entity_id, entity in entity_by_id.items():
            supports = list(entity.get("created_from_block_ids") or []) + list(
                entity.get("support_block_ids") or []
            )
            if any(lower <= int(block_order.get(block_id, -10**9)) < first_active_order for block_id in supports):
                recency_keys.add(("entity", entity_id))

    ordered_keys = sorted(matched_keys) + sorted(recency_keys - matched_keys)
    pre_cap_rows: list[dict[str, Any]] = []
    for row_type, row_id in ordered_keys:
        if row_type == "entity":
            card = _entity_card(entity_by_id[row_id], aliases)
        else:
            card = _glossary_card(glossary_by_id[row_id])
        pre_cap_rows.append(
            {
                "registry_row_type": row_type,
                "registry_row_id": row_id,
                "matched": (row_type, row_id) in matched_keys,
                "card": card,
                "row_hash": canonical_hash({"row_type": row_type, "card": card}),
            }
        )

    selected: list[dict[str, Any]] = []
    excluded: list[dict[str, Any]] = []
    used_tokens = 0
    for row in pre_cap_rows:
        row_tokens = max(1, len(canonical_json(row["card"])) // 4)
        fits = len(selected) < card_count_cap and used_tokens + row_tokens <= card_token_cap
        if fits:
            selected.append(row)
            used_tokens += row_tokens
        else:
            excluded.append(row)
            if row["matched"]:
                for link in links_by_row[(row["registry_row_type"], row["registry_row_id"])]:
                    overflow_sources.add(str(link["source_surface"]))

    selected_keys = {
        (str(row["registry_row_type"]), str(row["registry_row_id"])) for row in selected
    }
    legacy_candidate_links = [
        link
        for key in sorted(selected_keys)
        for link in sorted(
            links_by_row.get(key, []),
            key=lambda item: (
                item["source_block_ids"],
                item["source_surface"],
                item["matched_registry_surface"],
            ),
        )
    ]
    surface_candidate_packets, unmatched_recency_cards = _build_prejoined_candidate_context(
        selected=selected,
        links_by_row=links_by_row,
        overflow_sources=overflow_sources,
    )
    packet_candidate_count = sum(
        len(packet["candidates"]) for packet in surface_candidate_packets
    )
    manifest_body = {
        "policy_version": CANDIDATE_POLICY_VERSION,
        "working_registry_revision_hash": working_revision_hash,
        "pre_cap_universe_hash": canonical_hash(pre_cap_rows),
        "pre_cap_count": len(pre_cap_rows),
        "selected_row_hashes": [row["row_hash"] for row in selected],
        "selected_count": len(selected),
        "selected_token_estimate": used_tokens,
        "card_count_cap": card_count_cap,
        "card_token_cap": card_token_cap,
        "excluded_row_hashes": [row["row_hash"] for row in excluded],
        "excluded_recency_row_hashes": [
            row["row_hash"] for row in excluded if not row["matched"]
        ],
        "overflowed_source_surfaces": sorted(overflow_sources),
        "overflow": bool(excluded),
        "surface_candidate_packet_hashes": [
            canonical_hash(packet) for packet in surface_candidate_packets
        ],
        "surface_candidate_packet_count": len(surface_candidate_packets),
        "packet_candidate_count": packet_candidate_count,
        "unmatched_recency_row_hashes": [
            row["row_hash"] for row in unmatched_recency_cards
        ],
        "legacy_separate_context_bytes": len(
            canonical_json(
                {
                    "entity_candidate_cards": [
                        row["card"]
                        for row in selected
                        if row["registry_row_type"] == "entity"
                    ],
                    "glossary_candidate_cards": [
                        row["card"]
                        for row in selected
                        if row["registry_row_type"] == "glossary"
                    ],
                    "candidate_links": legacy_candidate_links,
                }
            )
        ),
        "prejoined_context_bytes": len(
            canonical_json(
                {
                    "surface_candidate_packets": surface_candidate_packets,
                    "unmatched_recency_cards": unmatched_recency_cards,
                }
            )
        ),
    }
    manifest = {**manifest_body, "manifest_hash": canonical_hash(manifest_body)}
    selected_entity_ids = {
        row["registry_row_id"] for row in selected if row["registry_row_type"] == "entity"
    }
    relevant_tickets = []
    searchable_texts = [row["text"] for row in searchable]
    for ticket in snapshot.get("tickets") or []:
        if ticket.get("status") == "resolved":
            continue
        surface_relevant = bool(ticket.get("surface")) and any(
            _literal_match(text, str(ticket["surface"])) is not None for text in searchable_texts
        )
        entity_relevant = bool(
            set(str(item) for item in ticket.get("related_entity_ids") or [])
            & selected_entity_ids
        )
        if surface_relevant or entity_relevant:
            relevant_tickets.append(_clone(ticket))
    relevant_bindings = [
        _clone(row)
        for row in snapshot.get("local_bindings") or []
        if row.get("status") != "confirmed"
        and str(row.get("block_id")) in {str(block["block_id"]) for block in searchable}
    ]
    return {
        "surface_candidate_packets": surface_candidate_packets,
        "unmatched_recency_cards": unmatched_recency_cards,
        "candidate_selection_manifest": manifest,
        "relevant_open_tickets": sorted(
            relevant_tickets, key=lambda row: str(row.get("ticket_id") or "")
        ),
        "relevant_local_bindings": sorted(
            relevant_bindings, key=lambda row: str(row.get("binding_id") or "")
        ),
    }


def render_b1_request(
    *,
    chapter_id: str,
    window_id: str,
    b0_gist: str,
    active_blocks: Sequence[Mapping[str, Any]],
    context_only_tail: Sequence[Mapping[str, Any]],
    working: "ChapterWorkingRegistryV2",
    block_order: Mapping[str, int],
    design_doc: Path,
    run_config: RunConfigV2,
    targeted_salient_surfaces: Sequence[Mapping[str, str]] | None = None,
) -> RenderedRegistryRequestV2:
    _validate_run_config_contract(run_config)
    selection = select_candidate_cards(
        snapshot=working.snapshot(),
        working_revision_hash=working.revision_hash,
        active_blocks=active_blocks,
        context_only_tail=context_only_tail,
        block_order=block_order,
        recency_k=run_config.recency_k,
        card_count_cap=run_config.candidate_card_count_cap,
        card_token_cap=run_config.candidate_card_token_cap,
    )
    open_tickets = selection.pop("relevant_open_tickets")
    open_local_bindings = selection.pop("relevant_local_bindings")
    sections = {
        "b0_gist": _required_str(b0_gist, "B0 gist"),
        "active_window_blocks": [_block_view(row) for row in active_blocks],
        "context_only_tail": [_block_view(row, context_only=True) for row in context_only_tail],
        "working_registry_revision_hash": working.revision_hash,
        **selection,
        "open_tickets": open_tickets,
        "open_local_bindings": open_local_bindings,
    }
    if targeted_salient_surfaces is not None:
        sections["targeted_salient_surfaces"] = _clone(list(targeted_salient_surfaces))
    prompt = load_system_prompt_from_design(design_doc, PROMPT_IDS["b1"])
    request = _render_request(
        role="b1",
        prompt_id=PROMPT_IDS["b1"],
        prompt_text=prompt,
        chapter_id=chapter_id,
        window_id=window_id,
        parent_working_revision_hash=working.revision_hash,
        sections=sections,
        run_config_hash=run_config.config_hash,
        model_contract={
            "model_id": run_config.b1_model_id,
            "reasoning_effort": run_config.b1_reasoning_effort,
            "temperature": run_config.b1_temperature,
            "seed": run_config.b1_seed,
            "verbosity": run_config.b1_verbosity,
            "max_output_tokens": run_config.b1_output_cap,
        },
        source_manifest={
            "active_block_ids": [str(row["block_id"]) for row in active_blocks],
            "context_only_tail_ids": [str(row["block_id"]) for row in context_only_tail],
            "candidate_manifest_hash": selection["candidate_selection_manifest"]["manifest_hash"],
        },
    )
    token_count = estimate_registry_prompt_tokens(request.messages)
    if token_count > run_config.b1_input_cap:
        raise RegistryBudgetError(
            f"B1 input {token_count} exceeds cap {run_config.b1_input_cap}"
        )
    return request


def _validated_candidate_card(
    row_type: str, row_id: str, value: Any
) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise RegistryContractError("candidate_card must be an object")
    card = dict(value)
    if row_type == "entity":
        _require_exact_keys(
            card,
            {
                "entity_id",
                "canonical_surface",
                "referent_kind",
                "aliases",
                "identity_summary",
                "status",
            },
            "entity candidate card",
        )
        if _required_str(card.get("entity_id"), "candidate entity_id") != row_id:
            raise RegistryContractError("candidate entity id/card mismatch")
        _required_str(card.get("canonical_surface"), "candidate canonical surface")
        _require_enum(card.get("referent_kind"), REFERENT_KINDS, "candidate referent kind")
        _require_string_list(card.get("aliases"), "candidate aliases", allow_empty=True)
        _required_str(card.get("identity_summary"), "candidate identity summary")
        _required_str(card.get("status"), "candidate entity status")
    elif row_type == "glossary":
        _require_exact_keys(
            card,
            {
                "glossary_id",
                "surface",
                "category_claim",
                "short_description",
                "status",
            },
            "glossary candidate card",
        )
        if _required_str(card.get("glossary_id"), "candidate glossary_id") != row_id:
            raise RegistryContractError("candidate glossary id/card mismatch")
        _required_str(card.get("surface"), "candidate glossary surface")
        _require_enum(
            card.get("category_claim"), GLOSSARY_CATEGORIES, "candidate glossary category"
        )
        _required_str(card.get("short_description"), "candidate glossary description")
        _required_str(card.get("status"), "candidate glossary status")
    else:
        raise RegistryContractError(f"unknown candidate row type: {row_type}")
    return _clone(card)


def _decode_prejoined_candidate_context(
    *,
    sections: Mapping[str, Any],
    working_revision_hash: str,
    source_catalog: Mapping[str, str],
) -> tuple[dict[str, Any], dict[str, Any], list[dict[str, Any]], dict[str, Any]]:
    manifest_value = sections.get("candidate_selection_manifest")
    if not isinstance(manifest_value, Mapping):
        raise RegistryContractError("candidate selection manifest must be an object")
    manifest = dict(manifest_value)
    _require_exact_keys(
        manifest,
        {
            "policy_version",
            "working_registry_revision_hash",
            "pre_cap_universe_hash",
            "pre_cap_count",
            "selected_row_hashes",
            "selected_count",
            "selected_token_estimate",
            "card_count_cap",
            "card_token_cap",
            "excluded_row_hashes",
            "excluded_recency_row_hashes",
            "overflowed_source_surfaces",
            "overflow",
            "surface_candidate_packet_hashes",
            "surface_candidate_packet_count",
            "packet_candidate_count",
            "unmatched_recency_row_hashes",
            "legacy_separate_context_bytes",
            "prejoined_context_bytes",
            "manifest_hash",
        },
        "candidate selection manifest",
    )
    if manifest.get("manifest_hash") != canonical_hash(
        {key: value for key, value in manifest.items() if key != "manifest_hash"}
    ):
        raise RegistryContractError("candidate selection manifest hash mismatch")
    if manifest.get("policy_version") != CANDIDATE_POLICY_VERSION:
        raise RegistryContractError("candidate selection policy version mismatch")
    if manifest.get("working_registry_revision_hash") != working_revision_hash:
        raise RegistryContractError("candidate selection manifest revision drift")

    def manifest_int(name: str) -> int:
        value = manifest.get(name)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise RegistryContractError(f"candidate manifest {name} must be a nonnegative integer")
        return value

    for name in (
        "pre_cap_count",
        "selected_count",
        "selected_token_estimate",
        "card_count_cap",
        "card_token_cap",
        "surface_candidate_packet_count",
        "packet_candidate_count",
        "legacy_separate_context_bytes",
        "prejoined_context_bytes",
    ):
        manifest_int(name)
    if not isinstance(manifest.get("overflow"), bool):
        raise RegistryContractError("candidate manifest overflow must be boolean")
    _required_str(manifest.get("pre_cap_universe_hash"), "pre-cap universe hash")
    for name in ("excluded_row_hashes", "excluded_recency_row_hashes"):
        _require_string_list(manifest.get(name), name, allow_empty=True)
    if manifest_int("selected_count") > manifest_int("card_count_cap"):
        raise RegistryContractError("candidate selected count exceeds manifest cap")
    if manifest_int("selected_token_estimate") > manifest_int("card_token_cap"):
        raise RegistryContractError("candidate selected token estimate exceeds manifest cap")

    packet_values = _require_list(
        sections.get("surface_candidate_packets"), "surface_candidate_packets"
    )
    recency_values = _require_list(
        sections.get("unmatched_recency_cards"), "unmatched_recency_cards"
    )
    overflow_sources = _require_string_list(
        manifest.get("overflowed_source_surfaces"),
        "overflowed_source_surfaces",
        allow_empty=True,
    )
    overflow_source_set = set(overflow_sources)
    cards_by_key: dict[tuple[str, str], dict[str, Any]] = {}
    row_hashes_by_key: dict[tuple[str, str], str] = {}
    supplied_entities: dict[str, Any] = {}
    supplied_glossary: dict[str, Any] = {}
    candidate_links: list[dict[str, Any]] = []
    actual_packet_hashes: list[str] = []
    actual_overflow_sources: set[str] = set()
    seen_packet_surfaces: set[str] = set()
    packet_candidate_count = 0

    def add_card(row_type: str, row_id: str, card_value: Any) -> dict[str, Any]:
        card = _validated_candidate_card(row_type, row_id, card_value)
        key = (row_type, row_id)
        prior = cards_by_key.get(key)
        if prior is not None and canonical_hash(prior) != canonical_hash(card):
            raise RegistryContractError("conflicting copies of one candidate card")
        cards_by_key[key] = card
        row_hashes_by_key[key] = canonical_hash({"row_type": row_type, "card": card})
        if row_type == "entity":
            supplied_entities[row_id] = card
        else:
            supplied_glossary[row_id] = card
        return card

    for raw_packet in packet_values:
        if not isinstance(raw_packet, Mapping):
            raise RegistryContractError("surface candidate packet must be an object")
        packet = dict(raw_packet)
        _require_exact_keys(
            packet,
            {
                "source_surface",
                "source_block_ids",
                "candidate_overflow",
                "candidates",
            },
            "surface candidate packet",
        )
        actual_packet_hashes.append(canonical_hash(packet))
        source_surface = _required_str(packet.get("source_surface"), "packet source surface")
        if source_surface in seen_packet_surfaces:
            raise RegistryContractError("duplicate source surface packet")
        seen_packet_surfaces.add(source_surface)
        source_block_ids = _require_string_list(
            packet.get("source_block_ids"), "packet source block ids"
        )
        if not source_block_ids or not set(source_block_ids) <= set(source_catalog):
            raise RegistryContractError("candidate packet cites foreign or empty source blocks")
        for block_id in source_block_ids:
            if _literal_match(source_catalog[block_id], source_surface) is None:
                raise RegistryContractError("candidate packet surface is absent from cited block")
        candidate_overflow = packet.get("candidate_overflow")
        if not isinstance(candidate_overflow, bool):
            raise RegistryContractError("candidate_overflow must be boolean")
        if candidate_overflow:
            actual_overflow_sources.add(source_surface)
        candidates = _require_list(packet.get("candidates"), "packet candidates")
        if not candidates and not candidate_overflow:
            raise RegistryContractError("candidate packet is empty without overflow")
        seen_packet_candidates: set[tuple[str, str]] = set()
        for raw_candidate in candidates:
            if not isinstance(raw_candidate, Mapping):
                raise RegistryContractError("packet candidate must be an object")
            candidate = dict(raw_candidate)
            _require_exact_keys(
                candidate,
                {
                    "registry_row_type",
                    "candidate_card",
                },
                "packet candidate",
            )
            row_type = _require_enum(
                candidate.get("registry_row_type"),
                frozenset({"entity", "glossary"}),
                "candidate row type",
            )
            candidate_card = candidate.get("candidate_card")
            if not isinstance(candidate_card, Mapping):
                raise RegistryContractError("candidate_card must be an object")
            row_id_key = "entity_id" if row_type == "entity" else "glossary_id"
            row_id = _required_str(candidate_card.get(row_id_key), "candidate row id")
            key = (row_type, row_id)
            if key in seen_packet_candidates:
                raise RegistryContractError("candidate is duplicated inside one packet")
            seen_packet_candidates.add(key)
            add_card(row_type, row_id, candidate_card)
            packet_candidate_count += 1
            candidate_links.append(
                {
                    "source_surface": source_surface,
                    "source_block_ids": source_block_ids,
                    "registry_row_type": row_type,
                    "registry_row_id": row_id,
                }
            )

    actual_recency_hashes: list[str] = []
    for raw_recency in recency_values:
        if not isinstance(raw_recency, Mapping):
            raise RegistryContractError("unmatched recency card must be an object")
        recency = dict(raw_recency)
        _require_exact_keys(
            recency,
            {"registry_row_type", "registry_row_id", "candidate_card", "row_hash"},
            "unmatched recency card",
        )
        row_type = _require_enum(
            recency.get("registry_row_type"),
            frozenset({"entity", "glossary"}),
            "recency row type",
        )
        row_id = _required_str(recency.get("registry_row_id"), "recency row id")
        if (row_type, row_id) in cards_by_key:
            raise RegistryContractError("matched candidate repeated as unmatched recency card")
        card = add_card(row_type, row_id, recency.get("candidate_card"))
        row_hash = _required_str(recency.get("row_hash"), "recency row hash")
        if row_hash != canonical_hash({"row_type": row_type, "card": card}):
            raise RegistryContractError("unmatched recency row hash mismatch")
        actual_recency_hashes.append(row_hash)

    expected_packet_hashes = _require_string_list(
        manifest.get("surface_candidate_packet_hashes"),
        "surface candidate packet hashes",
        allow_empty=True,
    )
    if actual_packet_hashes != expected_packet_hashes:
        raise RegistryContractError("candidate packet manifest mismatch")
    if manifest_int("surface_candidate_packet_count") != len(packet_values):
        raise RegistryContractError("candidate packet count mismatch")
    if manifest_int("packet_candidate_count") != packet_candidate_count:
        raise RegistryContractError("packet candidate count mismatch")
    expected_recency_hashes = _require_string_list(
        manifest.get("unmatched_recency_row_hashes"),
        "unmatched recency row hashes",
        allow_empty=True,
    )
    if actual_recency_hashes != expected_recency_hashes:
        raise RegistryContractError("unmatched recency manifest mismatch")
    actual_prejoined_bytes = len(
        canonical_json(
            {
                "surface_candidate_packets": packet_values,
                "unmatched_recency_cards": recency_values,
            }
        )
    )
    if manifest_int("prejoined_context_bytes") != actual_prejoined_bytes:
        raise RegistryContractError("prejoined candidate context byte count mismatch")
    if actual_overflow_sources != overflow_source_set:
        raise RegistryContractError("candidate overflow packet/manifest mismatch")
    selected_row_hashes = _require_string_list(
        manifest.get("selected_row_hashes"), "selected row hashes", allow_empty=True
    )
    if sorted(selected_row_hashes) != sorted(row_hashes_by_key.values()):
        raise RegistryContractError("selected candidate rows do not match manifest")
    if manifest_int("selected_count") != len(cards_by_key):
        raise RegistryContractError("selected candidate count mismatch")
    selected_token_estimate = sum(
        max(1, len(canonical_json(card)) // 4) for card in cards_by_key.values()
    )
    if manifest_int("selected_token_estimate") != selected_token_estimate:
        raise RegistryContractError("selected candidate token estimate mismatch")
    return supplied_entities, supplied_glossary, candidate_links, manifest


def _row_with_revision(payload: Mapping[str, Any]) -> dict[str, Any]:
    row = _clone(payload)
    row.pop("revision_hash", None)
    row["revision_hash"] = canonical_hash(row)
    return row


def _mint_id(prefix: str, decision_key: Mapping[str, Any]) -> str:
    return prefix + canonical_hash(decision_key)[:20]


def _find_table_row(snapshot: Mapping[str, Any], table: str, id_field: str, row_id: str) -> dict[str, Any] | None:
    for row in snapshot.get(table) or []:
        if str(row.get(id_field) or "") == row_id:
            return dict(row)
    return None


@dataclass
class ChapterWorkingRegistryV2:
    state_lineage_id: str
    chapter_id: str
    source_manifest_hash: str
    parent_generation_id: str | None
    parent_snapshot: Mapping[str, Any]
    _state: dict[str, Any] = field(init=False)
    revision_hash: str = field(init=False)
    applied_request_fingerprints: list[str] = field(default_factory=list)
    candidate_manifest_hashes: list[str] = field(default_factory=list)
    targeted_recall_request_fingerprints: list[str] = field(default_factory=list)
    application_records: list[dict[str, Any]] = field(default_factory=list)
    span_manifests: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    _replay: dict[str, tuple[str, dict[str, Any]]] = field(default_factory=dict)
    created_ids: dict[str, set[str]] = field(
        default_factory=lambda: {
            "entities": set(),
            "aliases": set(),
            "glossary_items": set(),
            "local_bindings": set(),
            "tickets": set(),
        }
    )

    def __post_init__(self) -> None:
        self.state_lineage_id = _required_str(self.state_lineage_id, "state_lineage_id")
        self.chapter_id = _required_str(self.chapter_id, "chapter_id")
        snapshot = _clone(self.parent_snapshot)
        if snapshot.get("schema_version") != REGISTRY_SCHEMA_VERSION:
            raise RegistryContractError("v1/foreign registry snapshot cannot seed v2")
        if snapshot.get("state_lineage_id") != self.state_lineage_id:
            raise RegistryContractError("registry snapshot crosses state lineage")
        for table in ("entities", "aliases", "glossary_items", "local_bindings", "tickets"):
            snapshot.setdefault(table, [])
        self._state = snapshot
        self.revision_hash = _snapshot_revision(self._state, self.chapter_id, [])

    @classmethod
    def create(
        cls,
        *,
        state_lineage_id: str,
        chapter_id: str,
        source_manifest_hash: str,
        parent_generation_id: str | None = None,
        parent_snapshot: Mapping[str, Any] | None = None,
    ) -> "ChapterWorkingRegistryV2":
        snapshot = parent_snapshot or empty_registry_snapshot_v2(state_lineage_id)
        if parent_generation_id is None:
            parent_generation_id = snapshot.get("generation_id")
        return cls(
            state_lineage_id=state_lineage_id,
            chapter_id=chapter_id,
            source_manifest_hash=_required_str(source_manifest_hash, "source_manifest_hash"),
            parent_generation_id=(str(parent_generation_id) if parent_generation_id else None),
            parent_snapshot=snapshot,
        )

    def snapshot(self) -> dict[str, Any]:
        result = {
            "builder_identity_mode": REGISTRY_SCHEMA_VERSION,
            "schema_version": REGISTRY_SCHEMA_VERSION,
            "state_lineage_id": self.state_lineage_id,
            "generation_id": self.parent_generation_id,
            "entities": sorted(
                _clone(self._state.get("entities") or []), key=lambda row: str(row["entity_id"])
            ),
            "aliases": sorted(
                _clone(self._state.get("aliases") or []), key=lambda row: str(row["alias_id"])
            ),
            "glossary_items": sorted(
                _clone(self._state.get("glossary_items") or []),
                key=lambda row: str(row["glossary_id"]),
            ),
            "local_bindings": sorted(
                _clone(self._state.get("local_bindings") or []),
                key=lambda row: str(row["binding_id"]),
            ),
            "tickets": sorted(
                _clone(self._state.get("tickets") or []), key=lambda row: str(row["ticket_id"])
            ),
        }
        result["snapshot_hash"] = canonical_hash(result)
        return result

    def _table_index(self, table: str, id_field: str) -> dict[str, dict[str, Any]]:
        return {str(row[id_field]): row for row in self._state.get(table) or []}

    def _append_unique(self, table: str, id_field: str, row: Mapping[str, Any]) -> None:
        index = self._table_index(table, id_field)
        row_id = str(row[id_field])
        if row_id in index:
            if canonical_json(index[row_id]) != canonical_json(row):
                raise RegistryContractError(f"{table} id collision with unequal payload")
            return
        self._state[table].append(_clone(row))
        self.created_ids[table].add(row_id)

    def _decision_key(
        self,
        request_fingerprint: str,
        list_name: str,
        row_index: int,
        row_payload: Mapping[str, Any],
    ) -> dict[str, Any]:
        return {
            "state_lineage_id": self.state_lineage_id,
            "chapter_id": self.chapter_id,
            "validated_request_fingerprint": request_fingerprint,
            "response_list_name": list_name,
            "response_row_index": row_index,
            "canonical_row_payload": _clone(row_payload),
        }

    def _locate_support(
        self,
        *,
        surface: str,
        block_ids: Sequence[str],
        active_catalog: Mapping[str, str],
        tail_catalog: Mapping[str, str],
    ) -> list[dict[str, Any]]:
        if not set(block_ids) <= set(active_catalog) | set(tail_catalog):
            raise RegistryContractError("semantic row cites a foreign support block")
        if not set(block_ids) & set(active_catalog):
            raise RegistryContractError("context-only tail cannot be sole semantic support")
        spans: list[dict[str, Any]] = []
        for block_id in block_ids:
            text = active_catalog.get(block_id, tail_catalog.get(block_id, ""))
            for span in _all_exact_spans(text, surface):
                spans.append({"block_id": block_id, **span})
        return spans

    def _code_ticket(
        self,
        *,
        request_fingerprint: str,
        row_index: int,
        ticket_type: str,
        surface: str | None,
        block_id: str,
        related_entity_ids: Sequence[str],
        note: str,
    ) -> dict[str, Any]:
        payload = {
            "ticket_type": _require_enum(ticket_type, TICKET_TYPES, "ticket_type"),
            "surface": (_required_str(surface, "ticket surface") if surface is not None else None),
            "block_id": _required_str(block_id, "ticket block_id"),
            "related_entity_ids": sorted(set(str(item) for item in related_entity_ids)),
            "note": _required_str(note, "ticket note"),
            "status": "open",
            "opened_by_request_fingerprint": request_fingerprint,
        }
        ticket_id = _mint_id(
            "tick2_",
            self._decision_key(request_fingerprint, "code_tickets", row_index, payload),
        )
        return _row_with_revision({"ticket_id": ticket_id, **payload})

    def apply_delta(
        self,
        request: RenderedRegistryRequestV2,
        response: Mapping[str, Any],
        *,
        targeted_recall: bool = False,
    ) -> dict[str, Any]:
        if request.role != "b1":
            raise RegistryContractError("only B1 requests can apply RegistryDeltaV2")
        response_hash = canonical_hash(response)
        replay = self._replay.get(request.request_fingerprint)
        if replay is not None:
            if replay[0] != response_hash:
                raise RegistryContractError("same request fingerprint replayed with different response")
            return _clone(replay[1])
        if request.parent_working_revision_hash != self.revision_hash:
            raise RegistryStaleRevisionError(
                f"stale B1 response: expected {self.revision_hash}, got "
                f"{request.parent_working_revision_hash}"
            )
        _require_exact_keys(response, set(DELTA_LISTS), "RegistryDeltaV2")
        for name in DELTA_LISTS:
            _require_list(response.get(name), name)

        sections = request.sections
        if sections.get("working_registry_revision_hash") != self.revision_hash:
            raise RegistryContractError("B1 request section revision drift")
        active_catalog = {
            str(row["block_id"]): _nfc(row.get("text"))
            for row in sections.get("active_window_blocks") or []
        }
        tail_catalog = {
            str(row["block_id"]): _nfc(row.get("text"))
            for row in sections.get("context_only_tail") or []
        }
        if not active_catalog:
            raise RegistryContractError("B1 request has no active blocks")
        supplied_entities, supplied_glossary, candidate_links, manifest = (
            _decode_prejoined_candidate_context(
                sections=sections,
                working_revision_hash=self.revision_hash,
                source_catalog={**tail_catalog, **active_catalog},
            )
        )
        overflow_surfaces = {
            _normalized_literal(item) for item in manifest.get("overflowed_source_surfaces") or []
        }
        self.candidate_manifest_hashes.append(str(manifest["manifest_hash"]))

        existing_entities = self._table_index("entities", "entity_id")
        existing_aliases = self._table_index("aliases", "alias_id")
        existing_glossary = self._table_index("glossary_items", "glossary_id")
        existing_global_surfaces: dict[str, set[str]] = defaultdict(set)
        for entity in existing_entities.values():
            existing_global_surfaces[_normalized_literal(entity["canonical_surface"])].add(
                str(entity["entity_id"])
            )
        for alias in existing_aliases.values():
            existing_global_surfaces[_normalized_literal(alias["surface"])].add(
                str(alias["entity_id"])
            )
        existing_glossary_surfaces: dict[str, set[str]] = defaultdict(set)
        for item in existing_glossary.values():
            existing_glossary_surfaces[_normalized_literal(item["surface"])].add(
                str(item["glossary_id"])
            )

        code_tickets: list[dict[str, Any]] = []
        located_artifacts: dict[str, list[dict[str, Any]]] = {}
        applied_ids: dict[str, list[str]] = {name: [] for name in DELTA_LISTS}
        normalization_counts = {
            "missing_initial_aliases_to_empty": 0,
            "repeated_open_ticket_ignored": 0,
        }
        supplied_open_ticket_signatures = {
            (
                str(ticket.get("ticket_type") or ""),
                _normalized_literal(ticket.get("surface")),
                str(ticket.get("block_id") or ""),
                tuple(sorted(str(item) for item in ticket.get("related_entity_ids") or [])),
            )
            for ticket in sections.get("open_tickets") or []
            if isinstance(ticket, Mapping)
        }
        response_ticket_types_by_surface: set[tuple[str, str]] = set()
        for raw in response["tickets"]:
            if isinstance(raw, Mapping):
                signature = (
                    str(raw.get("ticket_type") or ""),
                    _normalized_literal(raw.get("surface")),
                    str(raw.get("block_id") or ""),
                    tuple(sorted(str(item) for item in raw.get("related_entity_ids") or [])),
                )
                if signature in supplied_open_ticket_signatures:
                    continue
                response_ticket_types_by_surface.add(
                    (
                        str(raw.get("ticket_type") or ""),
                        _normalized_literal(raw.get("surface")),
                    )
                )

        for index, raw_value in enumerate(response["new_entities"]):
            if not isinstance(raw_value, Mapping):
                raise RegistryContractError("new entity must be an object")
            raw = dict(raw_value)
            if "initial_aliases" not in raw:
                raw["initial_aliases"] = []
                normalization_counts["missing_initial_aliases_to_empty"] += 1
            _require_exact_keys(
                raw,
                {
                    "surface",
                    "mention_type",
                    "referent_kind_claim",
                    "short_description",
                    "created_from_block_id",
                    "support_block_ids",
                    "initial_aliases",
                },
                "new entity",
            )
            surface = _required_str(raw["surface"], "entity surface")
            if _is_pronoun(surface):
                raise RegistryContractError("pronoun cannot create a global entity")
            mention_type = _require_enum(raw["mention_type"], MENTION_TYPES, "mention_type")
            kind = _require_enum(raw["referent_kind_claim"], REFERENT_KINDS, "referent_kind")
            created_block = _required_str(raw["created_from_block_id"], "created block")
            support_blocks = _require_string_list(raw["support_block_ids"], "entity support blocks")
            if created_block not in support_blocks:
                support_blocks = [created_block, *support_blocks]
            spans = self._locate_support(
                surface=surface,
                block_ids=support_blocks,
                active_catalog=active_catalog,
                tail_catalog=tail_catalog,
            )
            if not spans:
                code_tickets.append(
                    self._code_ticket(
                        request_fingerprint=request.request_fingerprint,
                        row_index=len(code_tickets),
                        ticket_type="unlocatable_surface",
                        surface=surface,
                        block_id=created_block,
                        related_entity_ids=[],
                        note="new entity surface was not verbatim in declared support blocks",
                    )
                )
                continue
            normalized_surface = _normalized_literal(surface)
            if normalized_surface in overflow_surfaces:
                code_tickets.append(
                    self._code_ticket(
                        request_fingerprint=request.request_fingerprint,
                        row_index=len(code_tickets),
                        ticket_type="candidate_overflow",
                        surface=surface,
                        block_id=created_block,
                        related_entity_ids=[],
                        note="candidate universe overflow forbids duplicate entity creation",
                    )
                )
                continue
            decision_payload = {
                "surface": surface,
                "mention_type": mention_type,
                "referent_kind_claim": kind,
                "short_description": _required_str(raw["short_description"], "short description"),
                "created_from_block_id": created_block,
                "support_block_ids": support_blocks,
                "initial_aliases": _clone(_require_list(raw["initial_aliases"], "initial_aliases")),
            }
            entity_id = _mint_id(
                "ent2_",
                self._decision_key(request.request_fingerprint, "new_entities", index, decision_payload),
            )
            entity = _row_with_revision(
                {
                    "entity_id": entity_id,
                    "canonical_surface": surface,
                    "referent_kind": kind,
                    "identity_summary": decision_payload["short_description"],
                    "created_from_block_ids": [created_block],
                    "support_block_ids": support_blocks,
                    "status": "provisional",
                }
            )
            self._append_unique("entities", "entity_id", entity)
            applied_ids["new_entities"].append(entity_id)
            located_artifacts[entity_id] = spans
            collision_ids = sorted(existing_global_surfaces.get(normalized_surface, set()))
            linked_ids = sorted(
                {
                    str(link.get("registry_row_id"))
                    for link in candidate_links
                    if link.get("registry_row_type") == "entity"
                    and _normalized_literal(link.get("source_surface")) == normalized_surface
                }
            )
            ticket_kind = "same_name_collision" if collision_ids else "possible_duplicate"
            if (collision_ids or linked_ids) and (
                ticket_kind,
                normalized_surface,
            ) not in response_ticket_types_by_surface:
                code_tickets.append(
                    self._code_ticket(
                        request_fingerprint=request.request_fingerprint,
                        row_index=len(code_tickets),
                        ticket_type=ticket_kind,
                        surface=surface,
                        block_id=created_block,
                        related_entity_ids=[entity_id, *collision_ids, *linked_ids],
                        note="code detected a canonical/alias candidate collision",
                    )
                )
            existing_global_surfaces[normalized_surface].add(entity_id)

            for alias_index, alias_value in enumerate(decision_payload["initial_aliases"]):
                if not isinstance(alias_value, Mapping):
                    raise RegistryContractError("initial alias must be an object")
                alias_raw = dict(alias_value)
                _require_exact_keys(
                    alias_raw, {"surface", "alias_type", "support_block_ids"}, "initial alias"
                )
                alias_surface = _required_str(alias_raw["surface"], "initial alias surface")
                if _is_pronoun(alias_surface):
                    raise RegistryContractError("pronoun cannot become a global alias")
                alias_type = _require_enum(alias_raw["alias_type"], ALIAS_TYPES, "alias_type")
                alias_support = _require_string_list(
                    alias_raw["support_block_ids"], "initial alias support"
                )
                alias_spans = self._locate_support(
                    surface=alias_surface,
                    block_ids=alias_support,
                    active_catalog=active_catalog,
                    tail_catalog=tail_catalog,
                )
                if not alias_spans:
                    code_tickets.append(
                        self._code_ticket(
                            request_fingerprint=request.request_fingerprint,
                            row_index=len(code_tickets),
                            ticket_type="unlocatable_surface",
                            surface=alias_surface,
                            block_id=alias_support[0],
                            related_entity_ids=[entity_id],
                            note="initial alias was not verbatim in declared support blocks",
                        )
                    )
                    continue
                alias_payload = {
                    "surface": alias_surface,
                    "alias_type": alias_type,
                    "entity_id": entity_id,
                    "support_block_ids": alias_support,
                }
                alias_id = _mint_id(
                    "als2_",
                    self._decision_key(
                        request.request_fingerprint,
                        f"new_entities[{index}].initial_aliases",
                        alias_index,
                        alias_payload,
                    ),
                )
                alias = _row_with_revision(
                    {"alias_id": alias_id, **alias_payload, "status": "provisional"}
                )
                self._append_unique("aliases", "alias_id", alias)
                self.created_ids["aliases"].add(alias_id)
                located_artifacts[alias_id] = alias_spans
                applied_ids["new_aliases"].append(alias_id)
                existing_global_surfaces[_normalized_literal(alias_surface)].add(entity_id)

        for index, raw_value in enumerate(response["new_aliases"]):
            if not isinstance(raw_value, Mapping):
                raise RegistryContractError("new alias must be an object")
            raw = dict(raw_value)
            _require_exact_keys(
                raw, {"surface", "alias_type", "target_entity_id", "support_block_ids"}, "new alias"
            )
            surface = _required_str(raw["surface"], "alias surface")
            if _is_pronoun(surface):
                raise RegistryContractError("pronoun cannot become a global alias")
            target = _required_str(raw["target_entity_id"], "target_entity_id")
            if target not in supplied_entities:
                raise RegistryContractError("new alias targets an unsupplied entity")
            support_blocks = _require_string_list(raw["support_block_ids"], "alias support")
            spans = self._locate_support(
                surface=surface,
                block_ids=support_blocks,
                active_catalog=active_catalog,
                tail_catalog=tail_catalog,
            )
            if not spans:
                code_tickets.append(
                    self._code_ticket(
                        request_fingerprint=request.request_fingerprint,
                        row_index=len(code_tickets),
                        ticket_type="unlocatable_surface",
                        surface=surface,
                        block_id=support_blocks[0],
                        related_entity_ids=[target],
                        note="alias surface was not verbatim in declared support blocks",
                    )
                )
                continue
            if _normalized_literal(surface) in overflow_surfaces:
                code_tickets.append(
                    self._code_ticket(
                        request_fingerprint=request.request_fingerprint,
                        row_index=len(code_tickets),
                        ticket_type="candidate_overflow",
                        surface=surface,
                        block_id=support_blocks[0],
                        related_entity_ids=[target],
                        note="candidate overflow forbids authoritative alias addition",
                    )
                )
                continue
            payload = {
                "surface": surface,
                "alias_type": _require_enum(raw["alias_type"], ALIAS_TYPES, "alias_type"),
                "entity_id": target,
                "support_block_ids": support_blocks,
            }
            alias_id = _mint_id(
                "als2_",
                self._decision_key(request.request_fingerprint, "new_aliases", index, payload),
            )
            alias = _row_with_revision({"alias_id": alias_id, **payload, "status": "provisional"})
            self._append_unique("aliases", "alias_id", alias)
            applied_ids["new_aliases"].append(alias_id)
            located_artifacts[alias_id] = spans
            owners = existing_global_surfaces.get(_normalized_literal(surface), set()) - {target}
            if owners and ("alias_collision", _normalized_literal(surface)) not in response_ticket_types_by_surface:
                code_tickets.append(
                    self._code_ticket(
                        request_fingerprint=request.request_fingerprint,
                        row_index=len(code_tickets),
                        ticket_type="alias_collision",
                        surface=surface,
                        block_id=support_blocks[0],
                        related_entity_ids=[target, *sorted(owners)],
                        note="code detected alias ownership collision",
                    )
                )
            existing_global_surfaces[_normalized_literal(surface)].add(target)

        for index, raw_value in enumerate(response["new_glossary_items"]):
            if not isinstance(raw_value, Mapping):
                raise RegistryContractError("new glossary item must be an object")
            raw = dict(raw_value)
            _require_exact_keys(
                raw,
                {
                    "surface",
                    "category_claim",
                    "short_description",
                    "created_from_block_id",
                    "support_block_ids",
                },
                "new glossary item",
            )
            surface = _required_str(raw["surface"], "glossary surface")
            if _is_pronoun(surface):
                raise RegistryContractError("pronoun cannot become a glossary item")
            created_block = _required_str(raw["created_from_block_id"], "glossary created block")
            support_blocks = _require_string_list(raw["support_block_ids"], "glossary support")
            if created_block not in support_blocks:
                support_blocks = [created_block, *support_blocks]
            spans = self._locate_support(
                surface=surface,
                block_ids=support_blocks,
                active_catalog=active_catalog,
                tail_catalog=tail_catalog,
            )
            if not spans:
                code_tickets.append(
                    self._code_ticket(
                        request_fingerprint=request.request_fingerprint,
                        row_index=len(code_tickets),
                        ticket_type="unlocatable_surface",
                        surface=surface,
                        block_id=created_block,
                        related_entity_ids=[],
                        note="glossary surface was not verbatim in declared support blocks",
                    )
                )
                continue
            normalized_surface = _normalized_literal(surface)
            payload = {
                "surface": surface,
                "category_claim": _require_enum(
                    raw["category_claim"], GLOSSARY_CATEGORIES, "category_claim"
                ),
                "short_description": _required_str(
                    raw["short_description"], "glossary short_description"
                ),
                "created_from_block_ids": [created_block],
                "support_block_ids": support_blocks,
            }
            glossary_id = _mint_id(
                "gls2_",
                self._decision_key(request.request_fingerprint, "new_glossary_items", index, payload),
            )
            item = _row_with_revision(
                {"glossary_id": glossary_id, **payload, "status": "provisional"}
            )
            self._append_unique("glossary_items", "glossary_id", item)
            applied_ids["new_glossary_items"].append(glossary_id)
            located_artifacts[glossary_id] = spans
            collisions = existing_glossary_surfaces.get(normalized_surface, set())
            linked = {
                str(link.get("registry_row_id"))
                for link in candidate_links
                if link.get("registry_row_type") == "glossary"
                and _normalized_literal(link.get("source_surface")) == normalized_surface
            }
            if (collisions or linked) and (
                "glossary_collision",
                normalized_surface,
            ) not in response_ticket_types_by_surface:
                code_tickets.append(
                    self._code_ticket(
                        request_fingerprint=request.request_fingerprint,
                        row_index=len(code_tickets),
                        ticket_type="glossary_collision",
                        surface=surface,
                        block_id=created_block,
                        related_entity_ids=[],
                        note="code detected duplicate/conflicting glossary surface",
                    )
                )
            existing_glossary_surfaces[normalized_surface].add(glossary_id)

        for index, raw_value in enumerate(response["local_bindings"]):
            if not isinstance(raw_value, Mapping):
                raise RegistryContractError("local binding must be an object")
            raw = dict(raw_value)
            _require_exact_keys(
                raw, {"surface", "block_id", "target_entity_id", "support_block_ids"}, "local binding"
            )
            surface = _required_str(raw["surface"], "local binding surface")
            if _is_pronoun(surface):
                raise RegistryContractError("pronoun cannot become a local binding")
            target = _required_str(raw["target_entity_id"], "local binding target")
            if target not in supplied_entities:
                raise RegistryContractError("local binding targets an unsupplied entity")
            block_id = _required_str(raw["block_id"], "local binding block_id")
            if block_id not in active_catalog:
                raise RegistryContractError("local binding scope must be an active block")
            support_blocks = _require_string_list(raw["support_block_ids"], "local binding support")
            if block_id not in support_blocks:
                support_blocks = [block_id, *support_blocks]
            spans = self._locate_support(
                surface=surface,
                block_ids=support_blocks,
                active_catalog=active_catalog,
                tail_catalog=tail_catalog,
            )
            if not spans:
                code_tickets.append(
                    self._code_ticket(
                        request_fingerprint=request.request_fingerprint,
                        row_index=len(code_tickets),
                        ticket_type="unlocatable_surface",
                        surface=surface,
                        block_id=block_id,
                        related_entity_ids=[target],
                        note="local descriptor was not verbatim in declared block",
                    )
                )
                continue
            if _normalized_literal(surface) in overflow_surfaces:
                code_tickets.append(
                    self._code_ticket(
                        request_fingerprint=request.request_fingerprint,
                        row_index=len(code_tickets),
                        ticket_type="candidate_overflow",
                        surface=surface,
                        block_id=block_id,
                        related_entity_ids=[target],
                        note="candidate overflow forbids authoritative local binding",
                    )
                )
                continue
            payload = {
                "surface": surface,
                "block_id": block_id,
                "target_ref": target,
                "status": "proposed",
                "support_block_ids": support_blocks,
            }
            binding_id = _mint_id(
                "bind2_",
                self._decision_key(request.request_fingerprint, "local_bindings", index, payload),
            )
            binding = _row_with_revision({"binding_id": binding_id, **payload})
            self._append_unique("local_bindings", "binding_id", binding)
            applied_ids["local_bindings"].append(binding_id)
            located_artifacts[binding_id] = spans

        for index, raw_value in enumerate(response["tickets"]):
            if not isinstance(raw_value, Mapping):
                raise RegistryContractError("ticket must be an object")
            raw = dict(raw_value)
            _require_exact_keys(
                raw, {"ticket_type", "surface", "block_id", "related_entity_ids", "note"}, "ticket"
            )
            ticket_type = _require_enum(raw["ticket_type"], TICKET_TYPES, "ticket_type")
            block_id = _required_str(raw["block_id"], "ticket block_id")
            related = _require_string_list(
                raw["related_entity_ids"], "ticket related_entity_ids", allow_empty=True
            )
            signature = (
                ticket_type,
                _normalized_literal(raw.get("surface")),
                block_id,
                tuple(sorted(related)),
            )
            if signature in supplied_open_ticket_signatures:
                normalization_counts["repeated_open_ticket_ignored"] += 1
                continue
            if ticket_type in {"unlocatable_surface", "missing_salient_surface"}:
                raise RegistryContractError(f"{ticket_type} is code-owned")
            if block_id not in active_catalog:
                raise RegistryContractError("model-authored ticket must cite an active block")
            if not set(related) <= set(supplied_entities):
                raise RegistryContractError("ticket cites an unsupplied entity id")
            surface = raw.get("surface")
            payload = {
                "ticket_type": ticket_type,
                "surface": (_required_str(surface, "ticket surface") if surface is not None else None),
                "block_id": block_id,
                "related_entity_ids": related,
                "note": _required_str(raw["note"], "ticket note"),
                "status": "open",
                "opened_by_request_fingerprint": request.request_fingerprint,
            }
            ticket_id = _mint_id(
                "tick2_",
                self._decision_key(request.request_fingerprint, "tickets", index, payload),
            )
            ticket = _row_with_revision({"ticket_id": ticket_id, **payload})
            self._append_unique("tickets", "ticket_id", ticket)
            applied_ids["tickets"].append(ticket_id)

        for ticket in code_tickets:
            self._append_unique("tickets", "ticket_id", ticket)
            applied_ids["tickets"].append(str(ticket["ticket_id"]))
        self.span_manifests.update(located_artifacts)
        self.applied_request_fingerprints.append(request.request_fingerprint)
        if targeted_recall:
            self.targeted_recall_request_fingerprints.append(request.request_fingerprint)
        prior_revision = self.revision_hash
        self.revision_hash = _snapshot_revision(
            self.snapshot(), self.chapter_id, self.applied_request_fingerprints
        )
        result = {
            "request_fingerprint": request.request_fingerprint,
            "response_hash": response_hash,
            "parent_working_revision_hash": prior_revision,
            "working_revision_hash": self.revision_hash,
            "applied_ids": applied_ids,
            "code_ticket_ids": [str(row["ticket_id"]) for row in code_tickets],
            "span_manifest_hash": canonical_hash(located_artifacts),
            "normalization_counts": normalization_counts,
        }
        self.application_records.append(_clone(result))
        self._replay[request.request_fingerprint] = (response_hash, _clone(result))
        return result


def schedule_targeted_recall(
    *,
    orientation: Mapping[str, Any],
    working_snapshot: Mapping[str, Any],
    windows: Sequence[Mapping[str, Any]],
    call_cap: int,
) -> list[dict[str, Any]]:
    known_surfaces = {
        _normalized_literal(row.get("canonical_surface"))
        for row in working_snapshot.get("entities") or []
    }
    known_surfaces |= {
        _normalized_literal(row.get("surface")) for row in working_snapshot.get("aliases") or []
    }
    known_surfaces |= {
        _normalized_literal(row.get("surface"))
        for row in working_snapshot.get("glossary_items") or []
    }
    missing = [
        dict(row)
        for row in orientation.get("salient_surface_checklist") or []
        if _normalized_literal(row.get("surface")) not in known_surfaces
    ]
    block_to_window: dict[str, str] = {}
    for window in windows:
        window_id = _required_str(window.get("window_id"), "window_id")
        for block in window.get("blocks") or []:
            block_to_window[_required_str(block.get("block_id"), "window block_id")] = window_id
    grouped: dict[str, list[dict[str, str]]] = defaultdict(list)
    for row in missing:
        block_id = _required_str(row.get("block_id"), "checklist block_id")
        if block_id not in block_to_window:
            raise RegistryContractError("B0 checklist miss is outside the B1 exact-cover windows")
        grouped[block_to_window[block_id]].append(
            {"surface": _required_str(row.get("surface"), "checklist surface"), "block_id": block_id}
        )
    if len(grouped) > call_cap:
        raise RegistryBudgetError(
            f"targeted recall requires {len(grouped)} grouped calls, cap is {call_cap}"
        )
    return [
        {
            "window_id": window_id,
            "missing_surfaces": sorted(
                rows, key=lambda row: (row["block_id"], row["surface"])
            ),
        }
        for window_id, rows in sorted(grouped.items())
    ]


def _row_token(table: str, row_id: str) -> str:
    return f"{table}:{row_id}"


def build_exception_manifest(working: ChapterWorkingRegistryV2) -> dict[str, Any]:
    snapshot = working.snapshot()
    tables = {
        "entities": ("entity_id", {str(row["entity_id"]): row for row in snapshot["entities"]}),
        "aliases": ("alias_id", {str(row["alias_id"]): row for row in snapshot["aliases"]}),
        "glossary_items": (
            "glossary_id",
            {str(row["glossary_id"]): row for row in snapshot["glossary_items"]},
        ),
        "local_bindings": (
            "binding_id",
            {str(row["binding_id"]): row for row in snapshot["local_bindings"]},
        ),
    }
    graph: dict[str, set[str]] = defaultdict(set)
    token_to_row: dict[str, tuple[str, str, dict[str, Any]]] = {}
    surface_tokens: dict[str, set[str]] = defaultdict(set)
    for table, (id_field, index) in tables.items():
        for row_id, row in index.items():
            token = _row_token(table, row_id)
            token_to_row[token] = (table, row_id, row)
            surface = row.get("canonical_surface") if table == "entities" else row.get("surface")
            if surface:
                surface_tokens[_normalized_literal(surface)].add(token)
    for alias_id, alias in tables["aliases"][1].items():
        entity_token = _row_token("entities", str(alias["entity_id"]))
        alias_token = _row_token("aliases", alias_id)
        graph[entity_token].add(alias_token)
        graph[alias_token].add(entity_token)
    for binding_id, binding in tables["local_bindings"][1].items():
        entity_token = _row_token("entities", str(binding["target_ref"]))
        binding_token = _row_token("local_bindings", binding_id)
        graph[entity_token].add(binding_token)
        graph[binding_token].add(entity_token)

    created_tokens = {
        _row_token(table, row_id)
        for table in ("entities", "aliases", "glossary_items", "local_bindings")
        for row_id in working.created_ids[table]
    }
    open_tickets = [
        row
        for row in snapshot["tickets"]
        if row.get("status") == "open" and str(row["ticket_id"]) in working.created_ids["tickets"]
    ]
    ticket_seeds: dict[str, set[str]] = defaultdict(set)
    for ticket in open_tickets:
        ticket_id = str(ticket["ticket_id"])
        for entity_id in ticket.get("related_entity_ids") or []:
            token = _row_token("entities", str(entity_id))
            if token in token_to_row:
                ticket_seeds[ticket_id].add(token)
        if ticket.get("surface"):
            ticket_seeds[ticket_id] |= surface_tokens.get(
                _normalized_literal(ticket.get("surface")), set()
            )

    exception_tokens: set[str] = set()
    token_ticket_ids: dict[str, set[str]] = defaultdict(set)
    for ticket_id, seeds in ticket_seeds.items():
        queue = deque(seeds)
        seen = set(seeds)
        while queue:
            token = queue.popleft()
            token_ticket_ids[token].add(ticket_id)
            for neighbor in graph.get(token, set()):
                if neighbor not in seen:
                    seen.add(neighbor)
                    queue.append(neighbor)
        exception_tokens |= seen & created_tokens

    records: list[dict[str, Any]] = []
    for token in sorted(created_tokens):
        table, row_id, row = token_to_row[token]
        reasons: list[str] = []
        if token in exception_tokens:
            reasons.append("connected_ticket_component")
        if table == "entities" and row.get("referent_kind") == "unknown":
            reasons.append("unknown_kind")
        eligible = not reasons
        inputs = {
            "row_hash": canonical_hash(row),
            "located_span_manifest_hash": canonical_hash(working.span_manifests.get(row_id, [])),
            "ticket_ids": sorted(token_ticket_ids.get(token, set())),
            "reasons": reasons,
        }
        records.append(
            {
                "policy_version": CLEAN_POLICY_VERSION,
                "row_type": table,
                "row_id": row_id,
                "eligible": eligible,
                "inputs": inputs,
                "record_hash": canonical_hash(
                    {
                        "policy_version": CLEAN_POLICY_VERSION,
                        "row_type": table,
                        "row_id": row_id,
                        "eligible": eligible,
                        "inputs": inputs,
                    }
                ),
            }
        )
        if not eligible:
            exception_tokens.add(token)

    # Unknown entities and their connected aliases/bindings must move together.
    queue = deque(exception_tokens)
    while queue:
        token = queue.popleft()
        for neighbor in graph.get(token, set()):
            if neighbor in created_tokens and neighbor not in exception_tokens:
                exception_tokens.add(neighbor)
                queue.append(neighbor)
    if exception_tokens:
        updated: list[dict[str, Any]] = []
        for record in records:
            token = _row_token(str(record["row_type"]), str(record["row_id"]))
            if token in exception_tokens and record["eligible"]:
                inputs = dict(record["inputs"])
                inputs["reasons"] = ["connected_exception_component"]
                updated.append(
                    {
                        **record,
                        "eligible": False,
                        "inputs": inputs,
                        "record_hash": canonical_hash(
                            {
                                "policy_version": CLEAN_POLICY_VERSION,
                                "row_type": record["row_type"],
                                "row_id": record["row_id"],
                                "eligible": False,
                                "inputs": inputs,
                            }
                        ),
                    }
                )
            else:
                updated.append(record)
        records = updated

    component_rows: list[dict[str, Any]] = []
    unassigned = set(exception_tokens)
    while unassigned:
        seed = min(unassigned)
        queue = deque([seed])
        component = {seed}
        while queue:
            token = queue.popleft()
            for neighbor in graph.get(token, set()):
                if neighbor in exception_tokens and neighbor not in component:
                    component.add(neighbor)
                    queue.append(neighbor)
        unassigned -= component
        ticket_ids = sorted(
            {
                ticket_id
                for token in component
                for ticket_id in token_ticket_ids.get(token, set())
            }
        )
        component_body = {
            "row_tokens": sorted(component),
            "ticket_ids": ticket_ids,
        }
        component_rows.append(
            {**component_body, "component_id": "exc2_" + canonical_hash(component_body)[:20]}
        )
    assigned_ticket_ids = {
        ticket_id for component in component_rows for ticket_id in component["ticket_ids"]
    }
    for ticket in sorted(open_tickets, key=lambda row: str(row["ticket_id"])):
        ticket_id = str(ticket["ticket_id"])
        if ticket_id in assigned_ticket_ids:
            continue
        component_body = {"row_tokens": [], "ticket_ids": [ticket_id]}
        component_rows.append(
            {**component_body, "component_id": "exc2_" + canonical_hash(component_body)[:20]}
        )

    exception_ids = {
        "entities": sorted(
            token.split(":", 1)[1] for token in exception_tokens if token.startswith("entities:")
        ),
        "aliases": sorted(
            token.split(":", 1)[1] for token in exception_tokens if token.startswith("aliases:")
        ),
        "glossary_items": sorted(
            token.split(":", 1)[1]
            for token in exception_tokens
            if token.startswith("glossary_items:")
        ),
        "local_bindings": sorted(
            token.split(":", 1)[1]
            for token in exception_tokens
            if token.startswith("local_bindings:")
        ),
        "tickets": sorted(str(row["ticket_id"]) for row in open_tickets),
    }
    body = {
        "working_registry_revision_hash": working.revision_hash,
        "exception_ids": exception_ids,
        "exception_rows": {
            table: [
                _clone(index[row_id])
                for row_id in exception_ids[table]
            ]
            for table, (_id_field, index) in tables.items()
        },
        "tickets": [_clone(row) for row in sorted(open_tickets, key=lambda row: row["ticket_id"])],
        "components": component_rows,
        "clean_commit_eligibility_records": records,
        "clean_counts": {
            table: sum(
                1
                for record in records
                if record["row_type"] == table and record["eligible"]
            )
            for table in ("entities", "aliases", "glossary_items", "local_bindings")
        },
        "clean_content_hash": canonical_hash(
            sorted(record["record_hash"] for record in records if record["eligible"])
        ),
    }
    body["exception_share"] = (
        len(exception_tokens) / len(created_tokens) if created_tokens else 0.0
    )
    body["manifest_hash"] = canonical_hash(body)
    return body


def render_auditor_request(
    *,
    chapter: Mapping[str, Any],
    b0_gist: str,
    working: ChapterWorkingRegistryV2,
    exception_manifest: Mapping[str, Any],
    design_doc: Path,
    run_config: RunConfigV2,
    enforce_input_cap: bool = True,
) -> RenderedRegistryRequestV2 | None:
    _validate_run_config_contract(run_config)
    exception_ids = exception_manifest.get("exception_ids") or {}
    if not any(exception_ids.get(name) for name in exception_ids):
        return None
    if float(exception_manifest.get("exception_share") or 0.0) > run_config.auditor_exception_share_cap:
        raise RegistryBudgetError("Auditor exception share exceeds approved cap")
    block_catalog = {str(row["block_id"]): _block_view(row) for row in _chapter_blocks(chapter)}
    related_block_ids: set[str] = set()
    for rows in (exception_manifest.get("exception_rows") or {}).values():
        for row in rows:
            related_block_ids |= set(row.get("created_from_block_ids") or [])
            related_block_ids |= set(row.get("support_block_ids") or [])
            if row.get("block_id"):
                related_block_ids.add(str(row["block_id"]))
    for ticket in exception_manifest.get("tickets") or []:
        related_block_ids.add(str(ticket["block_id"]))
    sections = {
        "b0_gist": b0_gist,
        "exception_manifest": _clone(exception_manifest),
        "related_source_blocks": [
            block_catalog[block_id] for block_id in sorted(related_block_ids) if block_id in block_catalog
        ],
        "located_span_manifests": {
            row_id: _clone(working.span_manifests.get(row_id, []))
            for ids in exception_ids.values()
            for row_id in ids
            if row_id in working.span_manifests
        },
        "working_registry_revision_hash": working.revision_hash,
        "clean_row_summary": {
            "counts": exception_manifest.get("clean_counts") or {},
            "content_hash": exception_manifest.get("clean_content_hash"),
        },
    }
    prompt = load_system_prompt_from_design(design_doc, PROMPT_IDS["auditor"])
    request = _render_request(
        role="auditor",
        prompt_id=PROMPT_IDS["auditor"],
        prompt_text=prompt,
        chapter_id=working.chapter_id,
        window_id=None,
        parent_working_revision_hash=working.revision_hash,
        sections=sections,
        run_config_hash=run_config.config_hash,
        model_contract={
            "model_id": run_config.auditor_model_id,
            "reasoning_effort": run_config.auditor_reasoning_effort,
            "temperature": run_config.auditor_temperature,
            "seed": run_config.auditor_seed,
            "verbosity": run_config.auditor_verbosity,
            "max_output_tokens": run_config.auditor_output_cap,
        },
        source_manifest={
            "chapter_source_manifest_hash": working.source_manifest_hash,
            "exception_manifest_hash": exception_manifest.get("manifest_hash"),
        },
    )
    token_count = estimate_registry_prompt_tokens(request.messages)
    if enforce_input_cap and token_count > run_config.auditor_input_token_cap:
        raise RegistryBudgetError(
            f"Auditor input {token_count} exceeds cap {run_config.auditor_input_token_cap}"
        )
    return request


def _exception_manifest_subset(
    manifest: Mapping[str, Any], component: Mapping[str, Any]
) -> dict[str, Any]:
    row_tokens = set(str(item) for item in component.get("row_tokens") or [])
    ticket_ids = set(str(item) for item in component.get("ticket_ids") or [])
    ids = {
        "entities": sorted(
            token.split(":", 1)[1] for token in row_tokens if token.startswith("entities:")
        ),
        "aliases": sorted(
            token.split(":", 1)[1] for token in row_tokens if token.startswith("aliases:")
        ),
        "glossary_items": sorted(
            token.split(":", 1)[1]
            for token in row_tokens
            if token.startswith("glossary_items:")
        ),
        "local_bindings": sorted(
            token.split(":", 1)[1]
            for token in row_tokens
            if token.startswith("local_bindings:")
        ),
        "tickets": sorted(ticket_ids),
    }
    rows = manifest.get("exception_rows") or {}
    id_fields = {
        "entities": "entity_id",
        "aliases": "alias_id",
        "glossary_items": "glossary_id",
        "local_bindings": "binding_id",
    }
    subset = {
        "working_registry_revision_hash": manifest["working_registry_revision_hash"],
        "exception_ids": ids,
        "exception_rows": {
            table: [
                _clone(row)
                for row in rows.get(table) or []
                if str(row[id_fields[table]]) in set(ids[table])
            ]
            for table in id_fields
        },
        "tickets": [
            _clone(row)
            for row in manifest.get("tickets") or []
            if str(row["ticket_id"]) in ticket_ids
        ],
        "components": [_clone(component)],
        "clean_commit_eligibility_records": [
            _clone(row)
            for row in manifest.get("clean_commit_eligibility_records") or []
            if _row_token(str(row["row_type"]), str(row["row_id"])) in row_tokens
        ],
        "clean_counts": _clone(manifest.get("clean_counts") or {}),
        "clean_content_hash": manifest.get("clean_content_hash"),
        "exception_share": manifest.get("exception_share", 0.0),
        "parent_manifest_hash": manifest.get("manifest_hash"),
    }
    subset["manifest_hash"] = canonical_hash(subset)
    return subset


def render_auditor_requests(
    *,
    chapter: Mapping[str, Any],
    b0_gist: str,
    working: ChapterWorkingRegistryV2,
    exception_manifest: Mapping[str, Any],
    design_doc: Path,
    run_config: RunConfigV2,
) -> list[RenderedRegistryRequestV2]:
    exception_ids = exception_manifest.get("exception_ids") or {}
    if not any(exception_ids.get(name) for name in exception_ids):
        return []
    components = list(exception_manifest.get("components") or [])
    if len(components) > run_config.auditor_component_cap:
        raise RegistryBudgetError(
            f"Auditor requires {len(components)} components, cap is "
            f"{run_config.auditor_component_cap}"
        )
    full = render_auditor_request(
        chapter=chapter,
        b0_gist=b0_gist,
        working=working,
        exception_manifest=exception_manifest,
        design_doc=design_doc,
        run_config=run_config,
        enforce_input_cap=False,
    )
    if full is None:
        return []
    full_tokens = estimate_registry_prompt_tokens(full.messages)
    if full_tokens <= run_config.auditor_input_token_cap:
        return [full]
    requests: list[RenderedRegistryRequestV2] = []
    for component in components:
        subset = _exception_manifest_subset(exception_manifest, component)
        request = render_auditor_request(
            chapter=chapter,
            b0_gist=b0_gist,
            working=working,
            exception_manifest=subset,
            design_doc=design_doc,
            run_config=run_config,
            enforce_input_cap=True,
        )
        if request is not None:
            requests.append(request)
    return requests


def _exact_disposition_ids(
    rows: Sequence[Mapping[str, Any]],
    *,
    id_field: str,
    expected_ids: set[str],
    label: str,
) -> None:
    actual = [str(row.get(id_field) or "") for row in rows]
    if any(not row_id for row_id in actual):
        raise RegistryContractError(f"{label} has missing id")
    if len(actual) != len(set(actual)):
        raise RegistryContractError(f"{label} has duplicate dispositions")
    if set(actual) != expected_ids:
        raise RegistryContractError(
            f"{label} exact-cover mismatch: expected {sorted(expected_ids)}, got {sorted(actual)}"
        )


def validate_audit_decision(
    response: Mapping[str, Any],
    *,
    exception_manifest: Mapping[str, Any],
    working: ChapterWorkingRegistryV2,
) -> dict[str, Any]:
    _require_exact_keys(response, set(AUDIT_LISTS), "ChapterAuditDecisionV1")
    for name in AUDIT_LISTS:
        _require_list(response.get(name), name)
    expected = exception_manifest.get("exception_ids") or {}
    mapping = {
        "entity_dispositions": ("entity_id", set(expected.get("entities") or [])),
        "alias_dispositions": ("alias_id", set(expected.get("aliases") or [])),
        "glossary_dispositions": ("glossary_id", set(expected.get("glossary_items") or [])),
        "local_binding_dispositions": (
            "binding_id",
            set(expected.get("local_bindings") or []),
        ),
        "ticket_dispositions": ("ticket_id", set(expected.get("tickets") or [])),
    }
    for list_name, (id_field, ids) in mapping.items():
        _exact_disposition_ids(
            response[list_name], id_field=id_field, expected_ids=ids, label=list_name
        )

    snapshot = working.snapshot()
    entities = {str(row["entity_id"]): row for row in snapshot["entities"]}
    tickets = {str(row["ticket_id"]): row for row in exception_manifest.get("tickets") or []}
    validated_entities: list[dict[str, Any]] = []
    for raw_value in response["entity_dispositions"]:
        if not isinstance(raw_value, Mapping):
            raise RegistryContractError("entity disposition must be an object")
        raw = dict(raw_value)
        _require_exact_keys(
            raw,
            {
                "entity_id",
                "action",
                "merge_target_entity_id",
                "revised_identity_summary",
            },
            "entity disposition",
        )
        entity_id = _required_str(raw["entity_id"], "entity disposition id")
        action = _require_enum(
            raw["action"],
            {"confirm", "reject_noise", "merge_provisional", "remain_pending"},
            "entity action",
        )
        entity = entities[entity_id]
        target = raw.get("merge_target_entity_id")
        if action == "merge_provisional":
            target = _required_str(target, "merge_target_entity_id")
            if entity_id not in working.created_ids["entities"]:
                raise RegistryContractError("Auditor cannot merge a prior confirmed entity")
            if target not in entities or target == entity_id:
                raise RegistryContractError("merge target must be a distinct supplied entity")
        elif target is not None:
            raise RegistryContractError("merge_target_entity_id is only valid for merge_provisional")
        if action == "confirm" and entity.get("referent_kind") == "unknown":
            raise RegistryContractError("unknown entity cannot become confirmed")
        revised = raw.get("revised_identity_summary")
        if revised is not None:
            revised = _required_str(revised, "revised_identity_summary")
        validated_entities.append(
            {
                "entity_id": entity_id,
                "action": action,
                "merge_target_entity_id": target,
                "revised_identity_summary": revised,
            }
        )

    def _validate_simple(
        list_name: str,
        id_field: str,
        actions: set[str],
    ) -> list[dict[str, str]]:
        result: list[dict[str, str]] = []
        for raw_value in response[list_name]:
            if not isinstance(raw_value, Mapping):
                raise RegistryContractError(f"{list_name} row must be an object")
            raw = dict(raw_value)
            _require_exact_keys(raw, {id_field, "action"}, list_name)
            result.append(
                {
                    id_field: _required_str(raw[id_field], id_field),
                    "action": _require_enum(raw["action"], actions, f"{list_name} action"),
                }
            )
        return result

    validated_aliases = _validate_simple(
        "alias_dispositions", "alias_id", {"confirm", "reject", "remain_pending"}
    )
    validated_glossary = _validate_simple(
        "glossary_dispositions",
        "glossary_id",
        {"confirm", "reject_noise", "remain_pending"},
    )
    validated_bindings = _validate_simple(
        "local_binding_dispositions",
        "binding_id",
        {"confirm", "reject", "remain_pending"},
    )
    validated_tickets: list[dict[str, str]] = []
    for raw_value in response["ticket_dispositions"]:
        if not isinstance(raw_value, Mapping):
            raise RegistryContractError("ticket disposition must be an object")
        raw = dict(raw_value)
        _require_exact_keys(raw, {"ticket_id", "action", "resolution_note"}, "ticket disposition")
        ticket_id = _required_str(raw["ticket_id"], "ticket_id")
        validated_tickets.append(
            {
                "ticket_id": ticket_id,
                "action": _require_enum(raw["action"], {"resolve", "carry"}, "ticket action"),
                "resolution_note": _required_str(raw["resolution_note"], "resolution_note"),
            }
        )
    profile_revisions: list[dict[str, Any]] = []
    seen_profiles: set[str] = set()
    for raw_value in response["profile_revisions"]:
        if not isinstance(raw_value, Mapping):
            raise RegistryContractError("profile revision must be an object")
        raw = dict(raw_value)
        _require_exact_keys(
            raw, {"entity_id", "revised_identity_summary", "resolved_ticket_ids"}, "profile revision"
        )
        entity_id = _required_str(raw["entity_id"], "profile entity_id")
        if entity_id not in entities or entity_id in seen_profiles:
            raise RegistryContractError("profile revision targets foreign/duplicate entity")
        seen_profiles.add(entity_id)
        resolved_ids = _require_string_list(
            raw["resolved_ticket_ids"], "resolved_ticket_ids"
        )
        if not set(resolved_ids) <= set(tickets):
            raise RegistryContractError("profile revision cites foreign ticket")
        if not any(
            tickets[ticket_id]["ticket_type"]
            in {"profile_description_conflict", "kind_conflict"}
            and entity_id in set(tickets[ticket_id].get("related_entity_ids") or [])
            for ticket_id in resolved_ids
        ):
            raise RegistryContractError("profile revision lacks an owned profile/kind ticket")
        profile_revisions.append(
            {
                "entity_id": entity_id,
                "revised_identity_summary": _required_str(
                    raw["revised_identity_summary"], "profile revised_identity_summary"
                ),
                "resolved_ticket_ids": resolved_ids,
            }
        )
    result = {
        "entity_dispositions": validated_entities,
        "alias_dispositions": validated_aliases,
        "glossary_dispositions": validated_glossary,
        "local_binding_dispositions": validated_bindings,
        "ticket_dispositions": validated_tickets,
        "profile_revisions": profile_revisions,
    }
    result["decision_hash"] = canonical_hash(result)
    return result


def validate_audit_decisions(
    responses: Sequence[Mapping[str, Any]],
    *,
    requests: Sequence[RenderedRegistryRequestV2],
    exception_manifest: Mapping[str, Any],
    working: ChapterWorkingRegistryV2,
) -> dict[str, Any]:
    if len(responses) != len(requests):
        raise RegistryContractError("Auditor response/request count mismatch")
    combined = {name: [] for name in AUDIT_LISTS}
    for response, request in zip(responses, requests, strict=True):
        subset = request.sections.get("exception_manifest") or {}
        validated = validate_audit_decision(
            response, exception_manifest=subset, working=working
        )
        for name in AUDIT_LISTS:
            combined[name].extend(_clone(validated[name]))
    # The final chapter-wide pass prevents split calls from omitting or duplicating ownership.
    validated_global = validate_audit_decision(
        combined, exception_manifest=exception_manifest, working=working
    )
    return {name: _clone(validated_global[name]) for name in AUDIT_LISTS}


def _replace_row(table: list[dict[str, Any]], id_field: str, replacement: Mapping[str, Any]) -> None:
    row_id = str(replacement[id_field])
    for index, row in enumerate(table):
        if str(row[id_field]) == row_id:
            table[index] = _clone(replacement)
            return
    table.append(_clone(replacement))


def _remove_row(table: list[dict[str, Any]], id_field: str, row_id: str) -> None:
    table[:] = [row for row in table if str(row[id_field]) != row_id]


def _changed_rows(
    *,
    parent: Mapping[str, Any],
    final: Mapping[str, Any],
    table: str,
    id_field: str,
) -> list[dict[str, Any]]:
    parent_index = {str(row[id_field]): row for row in parent.get(table) or []}
    return [
        _clone(row)
        for row in final.get(table) or []
        if str(row[id_field]) not in parent_index
        or canonical_json(parent_index[str(row[id_field])]) != canonical_json(row)
    ]


def _local_binding_status(source_status: str) -> str:
    return {
        "confirmed": "confirmed",
        "provisional": "proposed",
        "pending": "pending",
    }[source_status]


def _surface_scope_ticket(
    *,
    working: ChapterWorkingRegistryV2,
    plan: Mapping[str, Any],
    candidate_id: str,
    note: str,
) -> dict[str, Any]:
    support = list(plan.get("support_block_ids") or [])
    block_id = support[0] if support else working.chapter_id
    target = plan.get("target_entity_id")
    payload = {
        "ticket_type": "surface_scope_review",
        "surface": str(plan["surface"]),
        "block_id": str(block_id),
        "related_entity_ids": ([str(target)] if target else []),
        "note": note,
        "status": "open",
        "opened_by_request_fingerprint": f"commit-gate:{plan['plan_hash']}",
    }
    ticket_id = _mint_id(
        "tick2_",
        {
            "state_lineage_id": working.state_lineage_id,
            "chapter_id": working.chapter_id,
            "alias_scope_policy": ALIAS_SCOPE_POLICY_VERSION,
            "candidate_id": candidate_id,
            "ticket_payload": payload,
        },
    )
    return _row_with_revision({"ticket_id": ticket_id, **payload})


def _apply_surface_commit_gate(
    *,
    final: dict[str, Any],
    working: ChapterWorkingRegistryV2,
    source_catalog: Mapping[str, str],
    merge_surface_candidates: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    parent_aliases = {
        str(row["alias_id"]): row for row in working.parent_snapshot.get("aliases") or []
    }
    for alias in final.get("aliases") or []:
        alias_id = str(alias["alias_id"])
        parent_alias = parent_aliases.get(alias_id)
        if parent_alias is not None and canonical_json(parent_alias) == canonical_json(alias):
            continue
        _require_exact_keys(
            alias,
            {
                "alias_id",
                "surface",
                "alias_type",
                "entity_id",
                "support_block_ids",
                "status",
                "revision_hash",
            },
            "commit-time alias candidate",
        )
        candidates.append(
            {
                "candidate_id": f"alias:{alias_id}",
                "source_alias_id": alias_id,
                "surface": alias["surface"],
                "alias_type": alias["alias_type"],
                "target_entity_id": alias["entity_id"],
                "support_block_ids": list(alias.get("support_block_ids") or []),
                "source_status": alias["status"],
                "source_origin": "b1_or_auditor_alias",
            }
        )
    candidates.extend(_clone(list(merge_surface_candidates)))
    candidates.sort(key=lambda row: str(row["candidate_id"]))

    records: list[dict[str, Any]] = []
    for candidate in candidates:
        candidate_id = _required_str(candidate.get("candidate_id"), "surface candidate_id")
        surface = _required_str(candidate.get("surface"), "surface candidate surface")
        support = _require_string_list(
            candidate.get("support_block_ids"), "surface candidate support blocks"
        )
        target_id = str(candidate.get("target_entity_id") or "") or None
        spans = _located_surface_spans(
            surface=surface,
            support_block_ids=support,
            source_catalog=source_catalog,
        )
        if target_id is not None and _find_table_row(
            final, "entities", "entity_id", target_id
        ) is None:
            target_id = None
        plan = route_surface_for_commit(
            surface=surface,
            alias_type=candidate.get("alias_type"),
            target_entity_id=target_id,
            support_block_ids=support,
            located_source_spans=spans,
            source_status=str(candidate["source_status"]),
            source_origin=str(candidate["source_origin"]),
        )
        source_alias_id = candidate.get("source_alias_id")
        emitted_binding_ids: list[str] = []
        emitted_ticket_ids: list[str] = []
        outcome = str(plan["outcome"])

        if outcome == "global_alias_candidate":
            if source_alias_id is None:
                raise RegistryContractError(
                    "merge-derived surface cannot become a global alias without a typed alias row"
                )
        else:
            if source_alias_id is not None:
                _remove_row(final["aliases"], "alias_id", str(source_alias_id))

        if outcome == "downscope_local":
            existing_by_key = {
                (
                    _normalized_literal(row.get("surface")),
                    str(row.get("block_id") or ""),
                    str(row.get("target_ref") or ""),
                ): row
                for row in final.get("local_bindings") or []
            }
            conflicts = {
                (
                    _normalized_literal(row.get("surface")),
                    str(row.get("block_id") or ""),
                ): str(row.get("target_ref") or "")
                for row in final.get("local_bindings") or []
            }
            for block_id in sorted({str(row["block_id"]) for row in spans}):
                surface_block = (_normalized_literal(surface), block_id)
                conflict_target = conflicts.get(surface_block)
                if conflict_target and conflict_target != target_id:
                    outcome = "pending_scope_review"
                    continue
                key = (*surface_block, str(target_id))
                existing = existing_by_key.get(key)
                status = _local_binding_status(str(plan["source_status"]))
                if existing is not None:
                    authority = {"pending": 0, "proposed": 1, "confirmed": 2}
                    if authority[status] > authority[str(existing["status"])]:
                        updated = dict(existing)
                        updated["status"] = status
                        updated = _row_with_revision(updated)
                        _replace_row(final["local_bindings"], "binding_id", updated)
                    emitted_binding_ids.append(str(existing["binding_id"]))
                    continue
                payload = {
                    "surface": surface,
                    "block_id": block_id,
                    "target_ref": str(target_id),
                    "status": status,
                    "support_block_ids": [block_id],
                }
                binding_id = _mint_id(
                    "bind2_",
                    {
                        "state_lineage_id": working.state_lineage_id,
                        "chapter_id": working.chapter_id,
                        "alias_scope_policy": ALIAS_SCOPE_POLICY_VERSION,
                        "candidate_id": candidate_id,
                        "plan_hash": plan["plan_hash"],
                        "binding_payload": payload,
                    },
                )
                binding = _row_with_revision({"binding_id": binding_id, **payload})
                _replace_row(final["local_bindings"], "binding_id", binding)
                emitted_binding_ids.append(binding_id)
                existing_by_key[key] = binding
                conflicts[surface_block] = str(target_id)

        missing_blocks = sorted(set(support) - {str(row["block_id"]) for row in spans})
        needs_ticket = outcome == "pending_scope_review" or bool(missing_blocks)
        if bool(plan.get("sentence_initial_only")):
            needs_ticket = True
        if needs_ticket:
            note = (
                "Commit-time alias scope could not be published globally; "
                f"reason={plan['reason_code']}; missing_exact_blocks={missing_blocks}."
            )
            ticket = _surface_scope_ticket(
                working=working, plan=plan, candidate_id=candidate_id, note=note
            )
            _replace_row(final["tickets"], "ticket_id", ticket)
            emitted_ticket_ids.append(str(ticket["ticket_id"]))

        record = {
            "candidate_id": candidate_id,
            "source_alias_id": (str(source_alias_id) if source_alias_id is not None else None),
            "plan_hash": plan["plan_hash"],
            "surface": surface,
            "target_entity_id": target_id,
            "outcome": outcome,
            "reason_code": plan["reason_code"],
            "proper_name_signal": plan["proper_name_signal"],
            "sentence_initial_only": plan["sentence_initial_only"],
            "located_span_manifest_hash": canonical_hash(spans),
            "emitted_global_alias_id": (
                str(source_alias_id) if outcome == "global_alias_candidate" else None
            ),
            "emitted_local_binding_ids": sorted(set(emitted_binding_ids)),
            "emitted_ticket_ids": sorted(set(emitted_ticket_ids)),
        }
        record["record_hash"] = canonical_hash(record)
        records.append(record)

    recorded_alias_ids = {
        str(row["emitted_global_alias_id"])
        for row in records
        if row.get("emitted_global_alias_id")
    }
    unclassified = sorted(
        str(row["alias_id"])
        for row in final.get("aliases") or []
        if (
            str(row["alias_id"]) not in parent_aliases
            or canonical_json(parent_aliases[str(row["alias_id"])]) != canonical_json(row)
        )
        and str(row["alias_id"]) not in recorded_alias_ids
    )
    if unclassified:
        raise RegistryContractError(
            f"published aliases bypassed the commit-time scope gate: {unclassified}"
        )
    return records


def build_registry_generation(
    *,
    chapter: Mapping[str, Any],
    working: ChapterWorkingRegistryV2,
    b0_request_fingerprint: str,
    exception_manifest: Mapping[str, Any],
    audit_request_fingerprints: Sequence[str],
    audit_decision: Mapping[str, Any] | None,
) -> PreparedRegistryGenerationV2:
    source_catalog = _source_block_catalog(chapter)
    if chapter_source_manifest_hash(chapter) != working.source_manifest_hash:
        raise RegistryContractError("commit source catalog does not match working source manifest")
    expected_exception = exception_manifest.get("exception_ids") or {}
    has_exception = any(expected_exception.get(name) for name in expected_exception)
    if has_exception and audit_decision is None:
        raise RegistryContractError("exception manifest requires an Auditor decision")
    if not has_exception and audit_decision is not None:
        raise RegistryContractError("clean chapter must not receive an Auditor decision")
    if has_exception:
        validated_audit = validate_audit_decision(
            audit_decision or {}, exception_manifest=exception_manifest, working=working
        )
    else:
        validated_audit = None

    final = working.snapshot()
    records = {
        (str(row["row_type"]), str(row["row_id"])): row
        for row in exception_manifest.get("clean_commit_eligibility_records") or []
    }
    table_specs = {
        "entities": ("entity_id", "status"),
        "aliases": ("alias_id", "status"),
        "glossary_items": ("glossary_id", "status"),
        "local_bindings": ("binding_id", "status"),
    }
    for table, (id_field, status_field) in table_specs.items():
        for row in list(final[table]):
            row_id = str(row[id_field])
            if row_id not in working.created_ids[table]:
                continue
            eligibility = records.get((table, row_id))
            if eligibility and eligibility["eligible"]:
                updated = dict(row)
                updated[status_field] = "confirmed"
                _replace_row(final[table], id_field, _row_with_revision(updated))

    merge_surface_candidates: list[dict[str, Any]] = []
    if validated_audit is not None:
        entity_actions = {row["entity_id"]: row for row in validated_audit["entity_dispositions"]}
        merge_targets: dict[str, str] = {}
        for entity_id, disposition in entity_actions.items():
            entity = _find_table_row(final, "entities", "entity_id", entity_id)
            if entity is None:
                raise RegistryContractError("Auditor entity vanished before finalization")
            action = disposition["action"]
            if action == "reject_noise":
                _remove_row(final["entities"], "entity_id", entity_id)
            elif action == "remain_pending":
                updated = dict(entity)
                updated["status"] = "pending"
                if disposition.get("revised_identity_summary"):
                    updated["identity_summary"] = disposition["revised_identity_summary"]
                _replace_row(final["entities"], "entity_id", _row_with_revision(updated))
            elif action == "confirm":
                updated = dict(entity)
                updated["status"] = "confirmed"
                if disposition.get("revised_identity_summary"):
                    updated["identity_summary"] = disposition["revised_identity_summary"]
                _replace_row(final["entities"], "entity_id", _row_with_revision(updated))
            else:
                target_id = str(disposition["merge_target_entity_id"])
                target = _find_table_row(final, "entities", "entity_id", target_id)
                if target is None:
                    raise RegistryContractError("merge target vanished before finalization")
                merged_target = dict(target)
                merged_target["support_block_ids"] = sorted(
                    set(target.get("support_block_ids") or [])
                    | set(entity.get("support_block_ids") or [])
                )
                _replace_row(final["entities"], "entity_id", _row_with_revision(merged_target))
                _remove_row(final["entities"], "entity_id", entity_id)
                merge_targets[entity_id] = target_id
                merge_surface_candidates.append(
                    {
                        "candidate_id": f"merge:{entity_id}:{target_id}",
                        "source_alias_id": None,
                        "surface": entity["canonical_surface"],
                        "alias_type": None,
                        "target_entity_id": target_id,
                        "support_block_ids": list(entity.get("support_block_ids") or []),
                        "source_status": "confirmed",
                        "source_origin": "auditor_merge_canonical_surface",
                    }
                )

        simple_specs = (
            ("aliases", "alias_id", "alias_dispositions", "reject"),
            ("glossary_items", "glossary_id", "glossary_dispositions", "reject_noise"),
            ("local_bindings", "binding_id", "local_binding_dispositions", "reject"),
        )
        for table, id_field, list_name, reject_action in simple_specs:
            for disposition in validated_audit[list_name]:
                row_id = str(disposition[id_field])
                row = _find_table_row(final, table, id_field, row_id)
                if row is None:
                    continue
                action = disposition["action"]
                if action == reject_action:
                    _remove_row(final[table], id_field, row_id)
                    continue
                updated = dict(row)
                if table == "aliases" and str(updated.get("entity_id")) in merge_targets:
                    updated["entity_id"] = merge_targets[str(updated["entity_id"])]
                if table == "local_bindings" and str(updated.get("target_ref")) in merge_targets:
                    updated["target_ref"] = merge_targets[str(updated["target_ref"])]
                updated["status"] = "confirmed" if action == "confirm" else "pending"
                _replace_row(final[table], id_field, _row_with_revision(updated))

        for revision in validated_audit["profile_revisions"]:
            entity = _find_table_row(final, "entities", "entity_id", revision["entity_id"])
            if entity is None:
                raise RegistryContractError("profile revision target vanished")
            updated = dict(entity)
            updated["identity_summary"] = revision["revised_identity_summary"]
            _replace_row(final["entities"], "entity_id", _row_with_revision(updated))

        ticket_actions = {row["ticket_id"]: row for row in validated_audit["ticket_dispositions"]}
        for ticket_id, disposition in ticket_actions.items():
            ticket = _find_table_row(final, "tickets", "ticket_id", ticket_id)
            if ticket is None:
                raise RegistryContractError("ticket vanished before finalization")
            updated = dict(ticket)
            updated["status"] = "resolved" if disposition["action"] == "resolve" else "carried"
            updated["resolution_note"] = disposition["resolution_note"]
            _replace_row(final["tickets"], "ticket_id", _row_with_revision(updated))

    surface_commit_gate_records = _apply_surface_commit_gate(
        final=final,
        working=working,
        source_catalog=source_catalog,
        merge_surface_candidates=merge_surface_candidates,
    )
    parent = working.parent_snapshot
    body = {
        "state_lineage_id": working.state_lineage_id,
        "parent_generation_id": working.parent_generation_id,
        "chapter_id": working.chapter_id,
        "source_manifest_hash": working.source_manifest_hash,
        "source_block_catalog_hash": canonical_hash(source_catalog),
        "validator_version": VALIDATOR_VERSION,
        "policy_versions": {"alias_scope": ALIAS_SCOPE_POLICY_VERSION},
        "b0_request_fingerprint": _required_str(
            b0_request_fingerprint, "b0_request_fingerprint"
        ),
        "b1_request_fingerprints": list(working.applied_request_fingerprints),
        "candidate_selection_manifest_hashes": list(working.candidate_manifest_hashes),
        "targeted_recall_request_fingerprints": list(
            working.targeted_recall_request_fingerprints
        ),
        "auditor_request_fingerprints": list(audit_request_fingerprints),
        "clean_commit_eligibility_records": _clone(
            exception_manifest.get("clean_commit_eligibility_records") or []
        ),
        "surface_commit_gate_records": surface_commit_gate_records,
        "entity_revisions": _changed_rows(
            parent=parent, final=final, table="entities", id_field="entity_id"
        ),
        "alias_revisions": _changed_rows(
            parent=parent, final=final, table="aliases", id_field="alias_id"
        ),
        "glossary_revisions": _changed_rows(
            parent=parent, final=final, table="glossary_items", id_field="glossary_id"
        ),
        "local_binding_revisions": _changed_rows(
            parent=parent, final=final, table="local_bindings", id_field="binding_id"
        ),
        "ticket_revisions": _changed_rows(
            parent=parent, final=final, table="tickets", id_field="ticket_id"
        ),
        "audit_decisions": ([validated_audit] if validated_audit is not None else []),
    }
    commit_payload_hash = canonical_hash(body)
    generation_id = "reggen2_" + canonical_hash(
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
    return PreparedRegistryGenerationV2(
        state_lineage_id=working.state_lineage_id,
        generation_id=generation_id,
        parent_generation_id=working.parent_generation_id,
        chapter_id=working.chapter_id,
        source_manifest_hash=working.source_manifest_hash,
        payload=payload,
    )


class ChapterRegistryStoreV2:
    def __init__(self, root: Path) -> None:
        self.root = Path(root).resolve()

    def _generation_path(self, generation_id: str) -> Path:
        if not re.fullmatch(r"reggen2_[0-9a-f]{20}", generation_id):
            raise RegistryStoreError("unsafe v2 generation id")
        return self.root / "generations" / f"{generation_id}.json"

    def _pointer_path(self, state_lineage_id: str) -> Path:
        key = canonical_hash({"state_lineage_id": state_lineage_id})
        return self.root / "current" / f"{key}.json"

    def _write_generation(self, generation: PreparedRegistryGenerationV2) -> None:
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

    def load_generation(self, generation_id: str) -> dict[str, Any]:
        path = self._generation_path(generation_id)
        if not path.is_file():
            raise RegistryStoreError(f"missing registry generation: {generation_id}")
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise RegistryStoreError("registry generation is not an object")
        expected = {
            "state_lineage_id",
            "generation_id",
            "parent_generation_id",
            "chapter_id",
            "source_manifest_hash",
            "source_block_catalog_hash",
            "validator_version",
            "policy_versions",
            "b0_request_fingerprint",
            "b1_request_fingerprints",
            "candidate_selection_manifest_hashes",
            "targeted_recall_request_fingerprints",
            "auditor_request_fingerprints",
            "clean_commit_eligibility_records",
            "surface_commit_gate_records",
            "entity_revisions",
            "alias_revisions",
            "glossary_revisions",
            "local_binding_revisions",
            "ticket_revisions",
            "audit_decisions",
            "commit_payload_hash",
        }
        if set(payload) != expected:
            raise RegistryStoreError("registry generation shape mismatch")
        if payload.get("validator_version") != VALIDATOR_VERSION:
            raise RegistryStoreError("registry generation validator contract mismatch")
        if payload.get("policy_versions") != {
            "alias_scope": ALIAS_SCOPE_POLICY_VERSION
        }:
            raise RegistryStoreError("registry generation alias-scope policy mismatch")
        body = dict(payload)
        own_generation = str(body.pop("generation_id"))
        own_commit_hash = str(body.pop("commit_payload_hash"))
        if canonical_hash(body) != own_commit_hash:
            raise RegistryStoreError("registry generation commit hash mismatch")
        expected_generation = "reggen2_" + canonical_hash(
            {
                "state_lineage_id": body["state_lineage_id"],
                "parent_generation_id": body["parent_generation_id"],
                "chapter_id": body["chapter_id"],
                "commit_payload_hash": own_commit_hash,
            }
        )[:20]
        if own_generation != generation_id or expected_generation != generation_id:
            raise RegistryStoreError("registry generation id mismatch")
        return payload

    def current_generation_id(self, state_lineage_id: str) -> str | None:
        path = self._pointer_path(state_lineage_id)
        if not path.is_file():
            return None
        pointer = json.loads(path.read_text(encoding="utf-8"))
        if pointer.get("schema_version") != REGISTRY_SCHEMA_VERSION:
            raise RegistryStoreError("v1/foreign pointer cannot be read as v2")
        if pointer.get("state_lineage_id") != state_lineage_id:
            raise RegistryStoreError("registry pointer lineage mismatch")
        generation_id = _required_str(pointer.get("generation_id"), "pointer generation_id")
        generation = self.load_generation(generation_id)
        if generation.get("state_lineage_id") != state_lineage_id:
            raise RegistryStoreError("pointer targets a foreign lineage")
        return generation_id

    def commit(
        self,
        generation: PreparedRegistryGenerationV2,
        *,
        expected_parent: str | None,
        before_pointer_switch: Callable[[], None] | None = None,
    ) -> None:
        if generation.parent_generation_id != expected_parent:
            raise RegistryStaleParentError("generation parent differs from CAS expectation")
        self._write_generation(generation)
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

    def snapshot(
        self, state_lineage_id: str, generation_id: str | None = None
    ) -> dict[str, Any]:
        actual = generation_id or self.current_generation_id(state_lineage_id)
        if actual is None:
            return empty_registry_snapshot_v2(state_lineage_id)
        chain: list[dict[str, Any]] = []
        seen: set[str] = set()
        cursor: str | None = actual
        while cursor is not None:
            if cursor in seen:
                raise RegistryStoreError("registry generation cycle")
            seen.add(cursor)
            generation = self.load_generation(cursor)
            if generation["state_lineage_id"] != state_lineage_id:
                raise RegistryStoreError("generation chain crosses lineage")
            chain.append(generation)
            cursor = generation.get("parent_generation_id")
        indexes: dict[str, dict[str, dict[str, Any]]] = {
            "entities": {},
            "aliases": {},
            "glossary_items": {},
            "local_bindings": {},
            "tickets": {},
        }
        revision_map = {
            "entity_revisions": ("entities", "entity_id"),
            "alias_revisions": ("aliases", "alias_id"),
            "glossary_revisions": ("glossary_items", "glossary_id"),
            "local_binding_revisions": ("local_bindings", "binding_id"),
            "ticket_revisions": ("tickets", "ticket_id"),
        }
        for generation in reversed(chain):
            for revision_field, (table, id_field) in revision_map.items():
                for row in generation.get(revision_field) or []:
                    indexes[table][str(row[id_field])] = _clone(row)
        result = {
            "builder_identity_mode": REGISTRY_SCHEMA_VERSION,
            "schema_version": REGISTRY_SCHEMA_VERSION,
            "state_lineage_id": state_lineage_id,
            "generation_id": actual,
            **{
                table: [index[row_id] for row_id in sorted(index)]
                for table, index in indexes.items()
            },
        }
        result["snapshot_hash"] = canonical_hash(result)
        return result


def build_registry_windows(
    chapter: Mapping[str, Any],
    *,
    target_tokens: int,
    max_blocks: int,
    preceding_tail_k: int,
) -> list[dict[str, Any]]:
    ordered = _chapter_blocks(chapter)
    active = [
        row
        for row in ordered
        if str(row.get("block_type") or "").casefold() not in {"heading", "chapter_heading"}
    ]
    if not active:
        return []
    order_index = {str(row["block_id"]): index for index, row in enumerate(ordered)}
    windows: list[list[dict[str, Any]]] = []
    current: list[dict[str, Any]] = []
    current_tokens = 0
    for block in active:
        tokens = max(1, len(_block_text(block)) // 4)
        if current and (current_tokens + tokens > target_tokens or len(current) >= max_blocks):
            windows.append(current)
            current = []
            current_tokens = 0
        if tokens > target_tokens:
            windows.append([block])
            continue
        current.append(block)
        current_tokens += tokens
    if current:
        windows.append(current)
    chapter_id = _required_str(chapter.get("chapter_id"), "chapter_id")
    result: list[dict[str, Any]] = []
    covered: list[str] = []
    for index, rows in enumerate(windows, 1):
        first_order = order_index[str(rows[0]["block_id"])]
        tail = ordered[max(0, first_order - preceding_tail_k) : first_order]
        window_id = f"w2_{chapter_id}_{index:02d}"
        result.append(
            {
                "window_id": window_id,
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


def build_b2_candidate_manifest(
    *,
    chapter_id: str,
    active_blocks: Sequence[Mapping[str, Any]],
    registry_snapshot: Mapping[str, Any],
    candidate_count_cap: int,
) -> dict[str, Any]:
    generation_id = _required_str(registry_snapshot.get("generation_id"), "registry_generation_id")
    aliases_by_entity: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for alias in registry_snapshot.get("aliases") or []:
        if alias.get("status") == "confirmed":
            aliases_by_entity[str(alias["entity_id"])].append(alias)
    candidates: dict[str, dict[str, Any]] = {}
    links: list[dict[str, Any]] = []
    for entity in registry_snapshot.get("entities") or []:
        if entity.get("status") != "confirmed":
            continue
        entity_id = str(entity["entity_id"])
        surfaces = [str(entity["canonical_surface"])] + [
            str(alias["surface"]) for alias in aliases_by_entity.get(entity_id, [])
        ]
        for block in active_blocks:
            block_id = _required_str(block.get("block_id"), "B2 block_id")
            text = _block_text(block)
            for surface in surfaces:
                match = _literal_channel(text, surface)
                if match is None:
                    continue
                channel, reason = match
                candidates[entity_id] = {
                    "entity_id": entity_id,
                    "row_hash": canonical_hash(entity),
                }
                links.append(
                    {
                        "entity_id": entity_id,
                        "block_id": block_id,
                        "matched_surface": surface,
                        "match_reason": channel,
                        "detail": reason,
                    }
                )
    local_binding_ids: list[str] = []
    active_ids = {str(row["block_id"]) for row in active_blocks}
    for binding in registry_snapshot.get("local_bindings") or []:
        if binding.get("status") != "confirmed" or str(binding.get("block_id")) not in active_ids:
            continue
        entity_id = str(binding["target_ref"])
        entity = next(
            (
                row
                for row in registry_snapshot.get("entities") or []
                if str(row["entity_id"]) == entity_id and row.get("status") == "confirmed"
            ),
            None,
        )
        if entity is None:
            continue
        candidates[entity_id] = {"entity_id": entity_id, "row_hash": canonical_hash(entity)}
        local_binding_ids.append(str(binding["binding_id"]))
        links.append(
            {
                "entity_id": entity_id,
                "block_id": str(binding["block_id"]),
                "matched_surface": str(binding["surface"]),
                "match_reason": "local_binding",
                "detail": "confirmed block-local advisory binding",
            }
        )
    ordered = [candidates[key] for key in sorted(candidates)]
    selected = ordered[:candidate_count_cap]
    excluded = ordered[candidate_count_cap:]
    body = {
        "policy_version": B2_RESCAN_POLICY_VERSION,
        "chapter_id": chapter_id,
        "active_block_ids": sorted(active_ids),
        "registry_generation_id": generation_id,
        "candidate_entity_ids": [row["entity_id"] for row in selected],
        "candidate_row_hashes": [row["row_hash"] for row in selected],
        "match_reasons": sorted(
            {
                row["match_reason"]
                for row in links
                if row["entity_id"] in {item["entity_id"] for item in selected}
            }
        ),
        "candidate_links": sorted(
            [row for row in links if row["entity_id"] in {item["entity_id"] for item in selected}],
            key=lambda row: (
                row["block_id"],
                row["entity_id"],
                row["matched_surface"],
                row["match_reason"],
            ),
        ),
        "local_binding_ids": sorted(local_binding_ids),
        "pre_cap_universe_hash": canonical_hash(ordered),
        "pre_cap_count": len(ordered),
        "selected_count": len(selected),
        "excluded_row_hashes": [row["row_hash"] for row in excluded],
        "overflow": bool(excluded),
    }
    body["manifest_hash"] = canonical_hash(body)
    return body


@dataclass
class SyntheticRegistryExecutorV2:
    responses: dict[str, list[Mapping[str, Any]]] = field(default_factory=dict)
    calls: list[dict[str, Any]] = field(default_factory=list)

    def add(self, role: str, response: Mapping[str, Any]) -> None:
        self.responses.setdefault(role, []).append(_clone(response))

    def execute(self, request: RenderedRegistryRequestV2) -> dict[str, Any]:
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


def run_synthetic_registry_chapter_v2(
    *,
    chapter: Mapping[str, Any],
    state_lineage_id: str,
    parent_snapshot: Mapping[str, Any],
    executor: SyntheticRegistryExecutorV2,
    design_doc: Path,
    run_config: RunConfigV2,
    store: ChapterRegistryStoreV2 | None = None,
) -> dict[str, Any]:
    chapter_id = _required_str(chapter.get("chapter_id"), "chapter_id")
    source_hash = chapter_source_manifest_hash(chapter)
    b0_request = render_b0_request(chapter=chapter, design_doc=design_doc, run_config=run_config)
    orientation = validate_orientation_response(executor.execute(b0_request), chapter)
    working = ChapterWorkingRegistryV2.create(
        state_lineage_id=state_lineage_id,
        chapter_id=chapter_id,
        source_manifest_hash=source_hash,
        parent_generation_id=parent_snapshot.get("generation_id"),
        parent_snapshot=parent_snapshot,
    )
    windows = build_registry_windows(
        chapter,
        target_tokens=run_config.b1_window_target_tokens,
        max_blocks=run_config.b1_window_max_blocks,
        preceding_tail_k=run_config.context_only_tail_k,
    )
    block_order = {str(row["block_id"]): int(row.get("order_index") or 0) for row in _chapter_blocks(chapter)}
    b1_requests: list[RenderedRegistryRequestV2] = []
    for window in windows:
        request = render_b1_request(
            chapter_id=chapter_id,
            window_id=str(window["window_id"]),
            b0_gist=orientation["gist"],
            active_blocks=window["blocks"],
            context_only_tail=window["context_only_tail"],
            working=working,
            block_order=block_order,
            design_doc=design_doc,
            run_config=run_config,
        )
        response = executor.execute(request)
        working.apply_delta(request, response)
        b1_requests.append(request)
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
            b0_gist=orientation["gist"],
            active_blocks=window["blocks"],
            context_only_tail=window["context_only_tail"],
            working=working,
            block_order=block_order,
            design_doc=design_doc,
            run_config=run_config,
            targeted_salient_surfaces=target["missing_surfaces"],
        )
        response = executor.execute(request)
        working.apply_delta(request, response, targeted_recall=True)
        b1_requests.append(request)
    exception_manifest = build_exception_manifest(working)
    auditor_requests = render_auditor_requests(
        chapter=chapter,
        b0_gist=orientation["gist"],
        working=working,
        exception_manifest=exception_manifest,
        design_doc=design_doc,
        run_config=run_config,
    )
    audit_responses = [executor.execute(request) for request in auditor_requests]
    audit_response = (
        validate_audit_decisions(
            audit_responses,
            requests=auditor_requests,
            exception_manifest=exception_manifest,
            working=working,
        )
        if auditor_requests
        else None
    )
    generation = build_registry_generation(
        chapter=chapter,
        working=working,
        b0_request_fingerprint=b0_request.request_fingerprint,
        exception_manifest=exception_manifest,
        audit_request_fingerprints=[request.request_fingerprint for request in auditor_requests],
        audit_decision=audit_response,
    )
    if store is not None:
        store.commit(generation, expected_parent=working.parent_generation_id)
        final_snapshot = store.snapshot(state_lineage_id)
    else:
        final_snapshot = _clone(parent_snapshot)
        revision_map = {
            "entity_revisions": ("entities", "entity_id"),
            "alias_revisions": ("aliases", "alias_id"),
            "glossary_revisions": ("glossary_items", "glossary_id"),
            "local_binding_revisions": ("local_bindings", "binding_id"),
            "ticket_revisions": ("tickets", "ticket_id"),
        }
        for revision_field, (table, id_field) in revision_map.items():
            for row in generation.to_dict().get(revision_field) or []:
                _replace_row(final_snapshot[table], id_field, row)
        final_snapshot["generation_id"] = generation.generation_id
        final_snapshot["snapshot_hash"] = canonical_hash(
            {key: value for key, value in final_snapshot.items() if key != "snapshot_hash"}
        )
    b2_manifests = [
        build_b2_candidate_manifest(
            chapter_id=chapter_id,
            active_blocks=window["blocks"],
            registry_snapshot=final_snapshot,
            candidate_count_cap=run_config.candidate_card_count_cap,
        )
        for window in windows
    ]
    return {
        "b0_request": b0_request.to_dict(),
        "orientation": orientation,
        "b1_request_fingerprints": [request.request_fingerprint for request in b1_requests],
        "targeted_recall_plan": targeted,
        "exception_manifest": exception_manifest,
        "auditor_called": bool(auditor_requests),
        "generation": generation.to_dict(),
        "b2_candidate_manifests": b2_manifests,
        "working_revision_hash": working.revision_hash,
    }


__all__ = [
    "ChapterRegistryStoreV2",
    "ChapterWorkingRegistryV2",
    "SyntheticRegistryExecutorV2",
    "build_b2_candidate_manifest",
    "build_exception_manifest",
    "build_registry_generation",
    "build_registry_windows",
    "chapter_source_manifest_hash",
    "empty_registry_snapshot_v2",
    "estimate_registry_prompt_tokens",
    "render_auditor_request",
    "render_auditor_requests",
    "render_b0_request",
    "render_b1_request",
    "route_surface_for_commit",
    "run_synthetic_registry_chapter_v2",
    "schedule_targeted_recall",
    "select_candidate_cards",
    "validate_audit_decision",
    "validate_audit_decisions",
    "validate_orientation_response",
]
