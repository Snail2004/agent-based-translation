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

from pipeline.literary.checkpoint import (
    CheckpointLock,
    canonical_hash,
    canonical_json,
    write_checkpoint_atomic,
)
from pipeline.literary.chapter_registry_prompts_v4 import load_registry_prompt_v4
from pipeline.literary.chapter_registry_schema_v4 import (
    ALIAS_SCOPE_POLICY_VERSION,
    ATTENTION_LEDGER_VERSION,
    AUDIT_ALLOWED_ACTIONS,
    AUDIT_SCHEMA_VERSION,
    B2_RESCAN_POLICY_VERSION,
    CANDIDATE_POLICY_VERSION,
    CODE_TICKET_TYPES,
    DELTA_SCHEMA_VERSION,
    GLOSSARY_CATEGORIES,
    MODEL_TICKET_TYPES,
    NAME_CLASSES,
    NARRATIVE_CONTEXT_MODES,
    ORIENTATION_SCHEMA_VERSION,
    PROMPT_IDS,
    PreparedRegistryGenerationV4,
    REFERENTIAL_GENDERS,
    REFERENT_KINDS,
    REGISTRY_SCHEMA_VERSION,
    RegistryBudgetError,
    RegistryContractError,
    RegistryStaleParentError,
    RegistryStaleRevisionError,
    RegistryStoreError,
    RenderedRegistryRequestV4,
    RunConfigV4,
    SURFACE_UPDATE_KINDS,
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
_UNSTABLE_PROFILE_MARKERS = frozenset(
    {
        "age",
        "age_band",
        "current_action",
        "current_mood",
        "mood",
        "relation_phase",
        "relation_state",
        "scene_action",
    }
)
_B0_FORBIDDEN_ANSWER_MARKERS = frozenset(
    {
        "candidate_entity_ids",
        "category_claim",
        "entity_id",
        "merge_as_alias",
        "name_class",
        "referent_kind_claim",
        "target_entity_id",
        "ticket_type",
    }
)


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


def _require_mapping(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise RegistryContractError(f"{label} must be an object")
    return value


def _require_exact_keys(value: Mapping[str, Any], expected: set[str], label: str) -> None:
    if set(value) != expected:
        raise RegistryContractError(
            f"{label} fields mismatch: expected {sorted(expected)}, got {sorted(value)}"
        )


def _require_keys(
    value: Mapping[str, Any],
    *,
    required: set[str],
    optional: set[str],
    label: str,
) -> None:
    actual = set(value)
    missing = required - actual
    unexpected = actual - required - optional
    if missing or unexpected:
        raise RegistryContractError(
            f"{label} fields mismatch: missing {sorted(missing)}, "
            f"unexpected {sorted(unexpected)}"
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
    if not rows:
        raise RegistryContractError("chapter must contain source blocks")
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


def _bounded_matches(text: str, surface: str, *, ignore_case: bool) -> list[re.Match[str]]:
    if not surface:
        return []
    prefix = r"(?<!\w)" if re.match(r"\w", surface[0], flags=re.UNICODE) else ""
    suffix = r"(?!\w)" if re.match(r"\w", surface[-1], flags=re.UNICODE) else ""
    flags = re.IGNORECASE | re.UNICODE if ignore_case else re.UNICODE
    return list(re.finditer(prefix + re.escape(surface) + suffix, text, flags=flags))


def _match_surface(text: str, surface: str) -> list[tuple[str, str]]:
    exact = _bounded_matches(text, surface, ignore_case=False)
    if exact:
        return [("exact", row.group(0)) for row in exact]
    folded = _bounded_matches(text, surface, ignore_case=True)
    return [("normalized", row.group(0)) for row in folded]


def _title_base(surface: str, name_class: str | None) -> str | None:
    if name_class != "title_plus_name":
        return None
    parts = surface.split()
    if len(parts) < 2:
        return None
    base = " ".join(parts[1:]).strip()
    return base or None


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


def estimate_registry_prompt_tokens(messages: Sequence[Mapping[str, Any]]) -> int:
    return max(1, len(canonical_json({"messages": list(messages)})) // 4)


def _row_with_revision(payload: Mapping[str, Any]) -> dict[str, Any]:
    row = _clone(payload)
    row.pop("revision_hash", None)
    row["revision_hash"] = canonical_hash(row)
    return row


def _mint_id(prefix: str, key: Mapping[str, Any]) -> str:
    return prefix + canonical_hash(key)[:20]


def _validate_run_config_contract(run_config: RunConfigV4) -> None:
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


def empty_registry_snapshot_v4(state_lineage_id: str) -> dict[str, Any]:
    body = {
        "builder_identity_mode": REGISTRY_SCHEMA_VERSION,
        "schema_version": REGISTRY_SCHEMA_VERSION,
        "state_lineage_id": _required_str(state_lineage_id, "state_lineage_id"),
        "generation_id": None,
        "entities": [],
        "global_aliases": [],
        "block_local_references": [],
        "glossary_items": [],
        "tickets": [],
        "profile_revisions": [],
        "attention_ledger": [],
    }
    body["snapshot_hash"] = canonical_hash(body)
    return body


def _snapshot_revision(snapshot: Mapping[str, Any], chapter_id: str, applied: Sequence[str]) -> str:
    return "work4_" + canonical_hash(
        {
            "chapter_id": chapter_id,
            "parent_snapshot_hash": snapshot.get("snapshot_hash"),
            "entities": snapshot.get("entities") or [],
            "global_aliases": snapshot.get("global_aliases") or [],
            "block_local_references": snapshot.get("block_local_references") or [],
            "glossary_items": snapshot.get("glossary_items") or [],
            "tickets": snapshot.get("tickets") or [],
            "profile_revisions": snapshot.get("profile_revisions") or [],
            "attention_ledger": snapshot.get("attention_ledger") or [],
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
    run_config: RunConfigV4,
) -> RenderedRegistryRequestV4:
    _validate_run_config_contract(run_config)
    prompt = load_registry_prompt_v4(Path(design_doc), role)
    prompt_id = PROMPT_IDS[role]
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
        "max_output_tokens": getattr(run_config, f"{role}_output_token_cap"),
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
    return RenderedRegistryRequestV4(
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
    *, chapter: Mapping[str, Any], design_doc: Path, run_config: RunConfigV4
) -> RenderedRegistryRequestV4:
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
    if tokens > run_config.b0_input_token_cap:
        raise RegistryBudgetError(
            f"B0 input {tokens} exceeds cap {run_config.b0_input_token_cap}"
        )
    return request


def _reject_b0_answer_shaping(text: str, label: str) -> None:
    normalized = _normalized_literal(text).replace(" ", "_")
    hits = sorted(marker for marker in _B0_FORBIDDEN_ANSWER_MARKERS if marker in normalized)
    if hits:
        raise RegistryContractError(f"{label} contains typed registry answer markers: {hits}")


def validate_orientation_response(
    response: Mapping[str, Any],
    chapter: Mapping[str, Any],
    *,
    b0_request_fingerprint: str,
) -> dict[str, Any]:
    _require_exact_keys(
        response,
        {"orientation_draft", "narrative_context", "attention_items"},
        "ChapterOrientationV4_1",
    )
    chapter_id = _required_str(chapter.get("chapter_id"), "chapter_id")
    catalog = {str(row["block_id"]): _block_text(row) for row in _chapter_blocks(chapter)}
    order = {str(row["block_id"]): int(row.get("order_index") or 0) for row in _chapter_blocks(chapter)}
    orientation = _required_str(response.get("orientation_draft"), "orientation_draft")
    if len(orientation.split()) > 220:
        raise RegistryContractError("orientation_draft exceeds 220 words")
    _reject_b0_answer_shaping(orientation, "orientation_draft")

    raw_context = _require_mapping(response.get("narrative_context"), "narrative_context")
    _require_exact_keys(
        raw_context,
        {"mode", "note", "support_block_ids"},
        "narrative_context",
    )
    mode = _required_str(raw_context.get("mode"), "narrative context mode")
    if mode not in NARRATIVE_CONTEXT_MODES:
        raise RegistryContractError("narrative context mode is invalid")
    context_note = _required_str(raw_context.get("note"), "narrative context note")
    if len(context_note) > 240:
        raise RegistryContractError("narrative context note exceeds 240 characters")
    _reject_b0_answer_shaping(context_note, "narrative context note")
    context_blocks = _require_string_list(
        raw_context.get("support_block_ids"), "narrative context support blocks"
    )
    if len(context_blocks) > 4:
        raise RegistryContractError("narrative context exceeds four support blocks")
    if not set(context_blocks) <= set(catalog):
        raise RegistryContractError("narrative context cites foreign block")
    narrative_context = {
        "mode": mode,
        "note": context_note,
        "support_block_ids": sorted(
            context_blocks, key=lambda value: (order[value], value)
        ),
    }

    source_text_hashes = {block_id: canonical_hash(text) for block_id, text in catalog.items()}
    observations: list[dict[str, Any]] = []
    raw_attention = _require_list(response.get("attention_items"), "attention_items")
    dropped_by_reason = {
        "foreign_block": 0,
        "surface_not_located": 0,
        "reason_too_long": 0,
        "typed_answer_marker": 0,
    }
    for raw in raw_attention:
        row = _require_mapping(raw, "attention item")
        _require_exact_keys(row, {"surface", "source_block_ids", "why_noticed"}, "attention item")
        surface = _required_str(row.get("surface"), "attention surface")
        block_ids = _require_string_list(row.get("source_block_ids"), "attention source blocks")
        if not set(block_ids) <= set(catalog):
            dropped_by_reason["foreign_block"] += 1
            continue
        if not _located_support(surface=surface, block_ids=block_ids, active_catalog=catalog):
            dropped_by_reason["surface_not_located"] += 1
            continue
        reason = _required_str(row.get("why_noticed"), "attention why_noticed")
        if len(reason) > 240:
            dropped_by_reason["reason_too_long"] += 1
            continue
        try:
            _reject_b0_answer_shaping(reason, "attention why_noticed")
        except RegistryContractError:
            dropped_by_reason["typed_answer_marker"] += 1
            continue
        ordered_blocks = sorted(block_ids, key=lambda value: (order[value], value))
        manifest_hash = canonical_hash(
            [{"block_id": block_id, "source_text_hash": source_text_hashes[block_id]} for block_id in ordered_blocks]
        )
        key = {
            "attention_ledger_version": ATTENTION_LEDGER_VERSION,
            "chapter_id": chapter_id,
            "normalized_surface": _normalized_literal(surface),
            "source_block_ids": ordered_blocks,
            "source_text_manifest_hash": manifest_hash,
            "b0_request_fingerprint": b0_request_fingerprint,
        }
        observations.append(
            _row_with_revision(
                {
                    "attention_observation_id": _mint_id("att4_", key),
                    "chapter_id": chapter_id,
                    "surface": surface,
                    "normalized_surface": _normalized_literal(surface),
                    "source_block_ids": ordered_blocks,
                    "why_noticed": reason,
                    "source_text_manifest_hash": manifest_hash,
                    "b0_request_fingerprint": _required_str(
                        b0_request_fingerprint, "b0_request_fingerprint"
                    ),
                }
            )
        )
    observations.sort(key=lambda row: (row["normalized_surface"], row["attention_observation_id"]))
    attention_validation_report = {
        "input_count": len(raw_attention),
        "accepted_count": len(observations),
        "dropped_count": sum(dropped_by_reason.values()),
        "dropped_by_reason": dropped_by_reason,
    }
    return {
        "orientation_draft": orientation,
        "narrative_context": narrative_context,
        "attention_ledger": observations,
        "attention_validation_report": attention_validation_report,
        "orientation_hash": canonical_hash(
            {
                "orientation_draft": orientation,
                "narrative_context": narrative_context,
                "attention_ledger": observations,
                "attention_validation_report": attention_validation_report,
            }
        ),
    }


def build_registry_windows(
    chapter: Mapping[str, Any], *, target_tokens: int, max_blocks: int, preceding_tail_k: int
) -> list[dict[str, Any]]:
    if target_tokens <= 0 or max_blocks <= 0 or preceding_tail_k < 0:
        raise RegistryContractError("window controls are invalid")
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
    current_tokens = 0
    for block in active:
        block_tokens = max(1, len(_block_text(block)) // 4)
        if current and (current_tokens + block_tokens > target_tokens or len(current) >= max_blocks):
            windows.append(current)
            current = []
            current_tokens = 0
        if block_tokens > target_tokens:
            windows.append([block])
            continue
        current.append(block)
        current_tokens += block_tokens
    if current:
        windows.append(current)
    chapter_id = _required_str(chapter.get("chapter_id"), "chapter_id")
    order_by_id = {str(row["block_id"]): index for index, row in enumerate(ordered)}
    result: list[dict[str, Any]] = []
    covered: list[str] = []
    for index, rows in enumerate(windows, 1):
        first = order_by_id[str(rows[0]["block_id"])]
        tail = ordered[max(0, first - preceding_tail_k) : first]
        result.append(
            {
                "window_id": f"w4_{chapter_id}_{index:02d}",
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


def build_attention_packets(
    attention_ledger: Sequence[Mapping[str, Any]],
    *,
    active_block_ids: Sequence[str],
    block_order: Mapping[str, int],
    packet_cap: int,
) -> list[dict[str, Any]]:
    if packet_cap <= 0:
        raise RegistryContractError("attention packet cap must be positive")
    active = set(active_block_ids)
    grouped: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for raw in attention_ledger:
        row = _require_mapping(raw, "attention ledger row")
        if not set(row.get("source_block_ids") or []).intersection(active):
            continue
        grouped[_required_str(row.get("normalized_surface"), "normalized attention surface")].append(row)
    packets: list[dict[str, Any]] = []
    for normalized in sorted(grouped):
        rows = sorted(grouped[normalized], key=lambda row: str(row["attention_observation_id"]))
        relevant_blocks = sorted(
            {
                str(block_id)
                for row in rows
                for block_id in row.get("source_block_ids") or []
                if str(block_id) in active
            },
            key=lambda value: (block_order.get(value, 10**9), value),
        )
        observations = [
            {
                "attention_observation_id": str(row["attention_observation_id"]),
                "source_block_ids": sorted(
                    set(str(value) for value in row.get("source_block_ids") or []) & active,
                    key=lambda value: (block_order.get(value, 10**9), value),
                ),
                "why_noticed": str(row["why_noticed"]),
            }
            for row in rows
        ]
        packets.append(
            {
                "surface": str(rows[0]["surface"]),
                "source_block_ids": relevant_blocks,
                "observations": observations,
            }
        )
    if len(packets) > packet_cap:
        raise RegistryBudgetError(
            f"attention packet count {len(packets)} exceeds cap {packet_cap}"
        )
    return packets


def _entity_card(
    entity: Mapping[str, Any],
    aliases: Sequence[Mapping[str, Any]],
    *,
    retrieval_evidence: Mapping[str, Any],
) -> dict[str, Any]:
    stable_names = [
        str(row["surface"])
        for row in aliases
        if row.get("entity_id") == entity.get("entity_id")
        and row.get("status") in {"confirmed", "pending"}
        and row.get("gate_outcome") == "eligible_global_alias"
    ]
    if entity.get("name_class") is not None:
        stable_names.append(str(entity["canonical_surface"]))
    return {
        "entity_id": str(entity["entity_id"]),
        "canonical_surface": str(entity["canonical_surface"]),
        "referent_kind": str(entity["referent_kind"]),
        "stable_name_forms": sorted(
            set(stable_names), key=lambda value: (_normalized_literal(value), value)
        ),
        "identity_summary": str(entity["identity_summary"]),
        "referential_gender": entity.get("referential_gender"),
        "referential_gender_support_block_ids": list(
            entity.get("referential_gender_support_block_ids") or []
        ),
        "created_from_block_ids": list(entity.get("created_from_block_ids") or []),
        "status": str(entity["status"]),
        "retrieval_evidence": {
            "matched_registry_surfaces": sorted(
                set(str(value) for value in retrieval_evidence.get("matched_registry_surfaces") or []),
                key=lambda value: (_normalized_literal(value), value),
            ),
            "match_kinds": sorted(
                set(str(value) for value in retrieval_evidence.get("match_kinds") or [])
            ),
        },
    }


def _glossary_card(
    item: Mapping[str, Any], *, retrieval_evidence: Mapping[str, Any]
) -> dict[str, Any]:
    return {
        "glossary_id": str(item["glossary_id"]),
        "surface": str(item["surface"]),
        "category_claim": str(item["category_claim"]),
        "short_description": str(item["short_description"]),
        "status": str(item["status"]),
        "retrieval_evidence": {
            "matched_registry_surfaces": sorted(
                set(str(value) for value in retrieval_evidence.get("matched_registry_surfaces") or []),
                key=lambda value: (_normalized_literal(value), value),
            ),
            "match_kinds": sorted(
                set(str(value) for value in retrieval_evidence.get("match_kinds") or [])
            ),
        },
    }


def select_candidate_packets(
    *,
    snapshot: Mapping[str, Any],
    working_revision_hash: str,
    active_blocks: Sequence[Mapping[str, Any]],
    context_only_tail: Sequence[Mapping[str, Any]],
    block_order: Mapping[str, int],
    recency_distance: int,
    card_count_cap: int,
    card_token_cap: int,
    packet_count_cap: int,
) -> dict[str, Any]:
    """Build prejoined lexical packets without choosing an identity."""

    if snapshot.get("schema_version") != REGISTRY_SCHEMA_VERSION:
        raise RegistryContractError("candidate selection requires a v4 registry snapshot")
    if min(card_count_cap, card_token_cap, packet_count_cap) <= 0 or recency_distance < 0:
        raise RegistryContractError("candidate-selection controls are invalid")
    searchable = [_block_view(row) for row in active_blocks]
    active_ids = {str(row["block_id"]) for row in searchable}
    tail_ids = [str(row["block_id"]) for row in context_only_tail]
    entities = {
        str(row["entity_id"]): dict(row)
        for row in snapshot.get("entities") or []
        if row.get("status") in {"confirmed", "provisional", "pending"}
    }
    aliases = [
        dict(row)
        for row in snapshot.get("global_aliases") or []
        if row.get("status") in {"confirmed", "pending"}
        and row.get("gate_outcome") == "eligible_global_alias"
    ]
    local_references = [
        dict(row)
        for row in snapshot.get("block_local_references") or []
        if row.get("status") in {"confirmed", "provisional", "pending"}
        and set(str(value) for value in row.get("valid_block_ids") or []).intersection(active_ids)
    ]
    glossary = {
        str(row["glossary_id"]): dict(row)
        for row in snapshot.get("glossary_items") or []
        if row.get("status") in {"confirmed", "provisional", "pending"}
    }
    entity_surfaces: dict[str, list[tuple[str, str]]] = defaultdict(list)
    for entity_id, entity in entities.items():
        if entity.get("name_class") is not None:
            surface = str(entity["canonical_surface"])
            entity_surfaces[entity_id].append((surface, "canonical"))
            base = _title_base(surface, str(entity.get("name_class") or ""))
            if base:
                entity_surfaces[entity_id].append((base, "title_base"))
    for alias in aliases:
        if str(alias.get("entity_id")) in entities:
            surface = str(alias["surface"])
            entity_surfaces[str(alias["entity_id"])].append((surface, "global_alias"))
            base = _title_base(surface, str(alias.get("name_class") or ""))
            if base:
                entity_surfaces[str(alias["entity_id"])].append((base, "title_base"))
    for local in local_references:
        if str(local.get("entity_id")) in entities:
            entity_surfaces[str(local["entity_id"])].append(
                (str(local["surface"]), "block_local_reference")
            )

    packets: dict[str, dict[str, Any]] = {}

    def packet_for(source_surface: str) -> dict[str, Any]:
        normalized = _normalized_literal(source_surface)
        return packets.setdefault(
            normalized,
            {
                "source_surfaces": set(),
                "matched_block_ids": set(),
                "entity_evidence": defaultdict(
                    lambda: {"matched_registry_surfaces": set(), "match_kinds": set()}
                ),
                "glossary_evidence": defaultdict(
                    lambda: {"matched_registry_surfaces": set(), "match_kinds": set()}
                ),
            },
        )

    for entity_id, surfaces in entity_surfaces.items():
        for registry_surface, surface_origin in surfaces:
            for block in searchable:
                if surface_origin == "block_local_reference":
                    allowed = {
                        block_id
                        for row in local_references
                        if row.get("entity_id") == entity_id
                        and row.get("surface") == registry_surface
                        for block_id in row.get("valid_block_ids") or []
                    }
                    if block["block_id"] not in allowed:
                        continue
                matches = _match_surface(block["text"], registry_surface)
                for match_kind, source_surface in matches:
                    effective_kind = "title_base" if surface_origin == "title_base" else match_kind
                    packet = packet_for(source_surface)
                    packet["source_surfaces"].add(source_surface)
                    packet["matched_block_ids"].add(block["block_id"])
                    packet["entity_evidence"][entity_id]["matched_registry_surfaces"].add(
                        registry_surface
                    )
                    packet["entity_evidence"][entity_id]["match_kinds"].add(effective_kind)

    for glossary_id, item in glossary.items():
        registry_surface = str(item["surface"])
        for block in searchable:
            for match_kind, source_surface in _match_surface(block["text"], registry_surface):
                packet = packet_for(source_surface)
                packet["source_surfaces"].add(source_surface)
                packet["matched_block_ids"].add(block["block_id"])
                packet["glossary_evidence"][glossary_id]["matched_registry_surfaces"].add(
                    registry_surface
                )
                packet["glossary_evidence"][glossary_id]["match_kinds"].add(match_kind)

    # Lexical candidates are selected before recency-only cards.
    lexical_keys: list[tuple[str, str]] = []
    for normalized in sorted(packets):
        packet = packets[normalized]
        lexical_keys.extend(("entity", entity_id) for entity_id in sorted(packet["entity_evidence"]))
        lexical_keys.extend(
            ("glossary", glossary_id) for glossary_id in sorted(packet["glossary_evidence"])
        )
    lexical_keys = list(dict.fromkeys(lexical_keys))
    first_order = min((int(row["order_index"]) for row in searchable), default=0)
    recency_keys: list[tuple[str, str]] = []
    if recency_distance:
        lower = first_order - recency_distance
        for entity_id, entity in entities.items():
            supports = list(entity.get("created_from_block_ids") or []) + list(
                entity.get("support_block_ids") or []
            )
            if any(lower <= int(block_order.get(block_id, -10**9)) < first_order for block_id in supports):
                recency_keys.append(("entity", entity_id))
    ordered_keys = lexical_keys + sorted(set(recency_keys) - set(lexical_keys))
    selected: set[tuple[str, str]] = set()
    selected_tokens = 0
    generic_card_cache: dict[tuple[str, str], dict[str, Any]] = {}
    for key in ordered_keys:
        if key[0] == "entity":
            card = _entity_card(
                entities[key[1]],
                aliases,
                retrieval_evidence={"matched_registry_surfaces": [], "match_kinds": []},
            )
        else:
            card = _glossary_card(
                glossary[key[1]],
                retrieval_evidence={"matched_registry_surfaces": [], "match_kinds": []},
            )
        generic_card_cache[key] = card
        row_tokens = max(1, len(canonical_json(card)) // 4)
        if len(selected) >= card_count_cap or selected_tokens + row_tokens > card_token_cap:
            continue
        selected.add(key)
        selected_tokens += row_tokens

    packet_rows: list[dict[str, Any]] = []
    overflowed: set[str] = set()
    for normalized in sorted(packets):
        packet = packets[normalized]
        entity_cards: list[dict[str, Any]] = []
        glossary_cards: list[dict[str, Any]] = []
        for entity_id in sorted(packet["entity_evidence"]):
            if ("entity", entity_id) not in selected:
                overflowed.add(normalized)
                continue
            entity_cards.append(
                _entity_card(
                    entities[entity_id],
                    aliases,
                    retrieval_evidence=packet["entity_evidence"][entity_id],
                )
            )
        for glossary_id in sorted(packet["glossary_evidence"]):
            if ("glossary", glossary_id) not in selected:
                overflowed.add(normalized)
                continue
            glossary_cards.append(
                _glossary_card(
                    glossary[glossary_id],
                    retrieval_evidence=packet["glossary_evidence"][glossary_id],
                )
            )
        source_surface = sorted(
            packet["source_surfaces"], key=lambda value: (_normalized_literal(value), value)
        )[0]
        packet_rows.append(
            {
                "source_surface": source_surface,
                "matched_block_ids": sorted(
                    packet["matched_block_ids"],
                    key=lambda value: (block_order.get(value, 10**9), value),
                ),
                "candidate_entity_cards": entity_cards,
                "candidate_glossary_cards": glossary_cards,
                "candidate_overflow": normalized in overflowed,
            }
        )
    if len(packet_rows) > packet_count_cap:
        for row in packet_rows[packet_count_cap:]:
            overflowed.add(_normalized_literal(row["source_surface"]))
        packet_rows = packet_rows[:packet_count_cap]

    lexical_set = set(lexical_keys)
    recency_rows = []
    for key in sorted(selected - lexical_set):
        if key[0] != "entity":
            continue
        card = _clone(generic_card_cache[key])
        card.pop("retrieval_evidence", None)
        recency_rows.append(
            {
                "candidate_source": "recency_neighbor",
                "entity_card": card,
                "card_hash": canonical_hash(card),
            }
        )

    def rendered_candidate_tokens() -> int:
        return max(
            1,
            len(
                canonical_json(
                    {
                        "known_surface_hits": packet_rows,
                        "recency_neighbor_cards": recency_rows,
                    }
                )
            )
            // 4,
        )

    # The cap applies to bytes actually rendered, including per-card retrieval
    # evidence. Generic preselection estimates alone would undercount this.
    while rendered_candidate_tokens() > card_token_cap:
        if recency_rows:
            recency_rows.pop()
            continue
        removed = False
        for packet_row in reversed(packet_rows):
            for field in ("candidate_glossary_cards", "candidate_entity_cards"):
                if packet_row[field]:
                    packet_row[field].pop()
                    packet_row["candidate_overflow"] = True
                    overflowed.add(_normalized_literal(packet_row["source_surface"]))
                    removed = True
                    break
            if removed:
                break
        if not removed:
            raise RegistryBudgetError(
                "candidate packet metadata exceeds token cap even after removing all cards"
            )

    rendered_cards = [
        card
        for packet_row in packet_rows
        for field in ("candidate_entity_cards", "candidate_glossary_cards")
        for card in packet_row[field]
    ] + [row["entity_card"] for row in recency_rows]
    rendered_card_keys = {
        ("entity", str(card["entity_id"]))
        if "entity_id" in card
        else ("glossary", str(card["glossary_id"]))
        for card in rendered_cards
    }
    actual_context_tokens = rendered_candidate_tokens()
    overflow_records = []
    for normalized in sorted(overflowed):
        packet = packets.get(normalized)
        if packet is None:
            continue
        overflow_records.append(
            {
                "normalized_surface": normalized,
                "source_surface": sorted(
                    packet["source_surfaces"],
                    key=lambda value: (_normalized_literal(value), value),
                )[0],
                "matched_block_ids": sorted(
                    packet["matched_block_ids"],
                    key=lambda value: (block_order.get(value, 10**9), value),
                ),
            }
        )
    body = {
        "policy_version": CANDIDATE_POLICY_VERSION,
        "lexical_match_scope": "active_blocks_only",
        "context_only_tail_block_ids": tail_ids,
        "working_registry_revision_hash": working_revision_hash,
        "selected_card_hashes": sorted(canonical_hash(card) for card in rendered_cards),
        "selected_count": len(rendered_card_keys),
        "selected_token_estimate": actual_context_tokens,
        "card_count_cap": card_count_cap,
        "card_token_cap": card_token_cap,
        "packet_count_cap": packet_count_cap,
        "packet_hashes": [canonical_hash(row) for row in packet_rows],
        "packet_count": len(packet_rows),
        "overflowed_normalized_surfaces": sorted(overflowed),
        "overflow_records": overflow_records,
        "prejoined_context_bytes": len(
            canonical_json(
                {"known_surface_hits": packet_rows, "recency_neighbor_cards": recency_rows}
            )
        ),
    }
    manifest = {**body, "manifest_hash": canonical_hash(body)}
    return {
        "known_surface_hits": packet_rows,
        "recency_neighbor_cards": recency_rows,
        "candidate_selection_manifest": manifest,
    }


def render_b1_request(
    *,
    chapter_id: str,
    window_id: str,
    orientation: Mapping[str, Any],
    active_blocks: Sequence[Mapping[str, Any]],
    context_only_tail: Sequence[Mapping[str, Any]],
    working: "ChapterWorkingRegistryV4",
    block_order: Mapping[str, int],
    design_doc: Path,
    run_config: RunConfigV4,
) -> RenderedRegistryRequestV4:
    selection = select_candidate_packets(
        snapshot=working.snapshot(),
        working_revision_hash=working.revision_hash,
        active_blocks=active_blocks,
        context_only_tail=context_only_tail,
        block_order=block_order,
        recency_distance=run_config.recency_neighbor_distance_blocks,
        card_count_cap=run_config.candidate_cards_total_cap_per_window,
        card_token_cap=run_config.candidate_context_token_cap,
        packet_count_cap=run_config.known_surface_packet_cap_per_window,
    )
    if (
        selection["candidate_selection_manifest"]["overflow_records"]
        and run_config.candidate_overflow_policy == "halt"
    ):
        raise RegistryBudgetError("candidate selection overflowed under halt policy")
    active_ids = [str(row["block_id"]) for row in active_blocks]
    attention = []
    if run_config.b0_attention_context_mode == "advisory_active_window":
        attention = build_attention_packets(
            orientation.get("attention_ledger") or [],
            active_block_ids=active_ids,
            block_order=block_order,
            packet_cap=run_config.attention_packet_cap_per_window,
        )
    sections = {
        "b0_orientation_draft": _required_str(
            orientation.get("orientation_draft"), "B0 orientation draft"
        ),
        "active_window_blocks": [_block_view(row) for row in active_blocks],
        "context_only_preceding_tail": [
            _block_view(row, context_only=True) for row in context_only_tail
        ],
        "advisory_attention_for_active_blocks": attention,
        "known_surface_hits": selection["known_surface_hits"],
        "recency_neighbor_cards": selection["recency_neighbor_cards"],
        "working_registry_revision_hash": working.revision_hash,
        "candidate_selection_manifest": selection["candidate_selection_manifest"],
        "cap_manifest": {
            "attention_packet_cap_per_window": run_config.attention_packet_cap_per_window,
            "known_surface_packet_cap_per_window": run_config.known_surface_packet_cap_per_window,
            "candidate_cards_total_cap_per_window": run_config.candidate_cards_total_cap_per_window,
            "candidate_context_token_cap": run_config.candidate_context_token_cap,
            "recency_neighbor_distance_blocks": run_config.recency_neighbor_distance_blocks,
        },
    }
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
    if tokens > run_config.b1_input_token_cap:
        raise RegistryBudgetError(
            f"B1 input {tokens} exceeds cap {run_config.b1_input_token_cap}"
        )
    return request


def _decode_candidate_universe(
    request: RenderedRegistryRequestV4,
) -> tuple[set[str], set[str], set[str], dict[str, list[str]]]:
    manifest = _require_mapping(
        request.sections.get("candidate_selection_manifest"), "candidate selection manifest"
    )
    body = dict(manifest)
    own_hash = body.pop("manifest_hash", None)
    if own_hash != canonical_hash(body):
        raise RegistryContractError("candidate selection manifest hash mismatch")
    if body.get("policy_version") != CANDIDATE_POLICY_VERSION:
        raise RegistryContractError("candidate selection policy mismatch")
    if body.get("working_registry_revision_hash") != request.parent_working_revision_hash:
        raise RegistryContractError("candidate selection revision drift")
    packets = _require_list(request.sections.get("known_surface_hits"), "known_surface_hits")
    if body.get("packet_hashes") != [canonical_hash(row) for row in packets]:
        raise RegistryContractError("candidate packet hashes mismatch")
    if body.get("packet_count") != len(packets):
        raise RegistryContractError("candidate packet count mismatch")
    entity_ids: set[str] = set()
    glossary_ids: set[str] = set()
    overflow: set[str] = set(body.get("overflowed_normalized_surfaces") or [])
    entity_packet_surfaces: dict[str, list[str]] = defaultdict(list)
    seen: set[str] = set()
    for raw in packets:
        row = _require_mapping(raw, "known surface packet")
        _require_exact_keys(
            row,
            {
                "source_surface",
                "matched_block_ids",
                "candidate_entity_cards",
                "candidate_glossary_cards",
                "candidate_overflow",
            },
            "known surface packet",
        )
        source_surface = _required_str(row.get("source_surface"), "known source surface")
        normalized = _normalized_literal(source_surface)
        if normalized in seen:
            raise RegistryContractError("duplicate normalized known-surface packet")
        seen.add(normalized)
        _require_string_list(row.get("matched_block_ids"), "matched block ids")
        if not isinstance(row.get("candidate_overflow"), bool):
            raise RegistryContractError("candidate_overflow must be boolean")
        if row["candidate_overflow"]:
            overflow.add(normalized)
        for raw_card in _require_list(row.get("candidate_entity_cards"), "entity cards"):
            card = _require_mapping(raw_card, "entity candidate card")
            entity_id = _required_str(card.get("entity_id"), "candidate entity id")
            evidence = _require_mapping(card.get("retrieval_evidence"), "entity retrieval evidence")
            kinds = _require_string_list(
                evidence.get("match_kinds"), "entity retrieval match kinds"
            )
            if not set(kinds) <= {"exact", "normalized", "title_base"}:
                raise RegistryContractError("entity card has unsupported match kind")
            _require_string_list(
                evidence.get("matched_registry_surfaces"), "matched registry surfaces"
            )
            entity_ids.add(entity_id)
            entity_packet_surfaces[entity_id].append(source_surface)
        for raw_card in _require_list(row.get("candidate_glossary_cards"), "glossary cards"):
            card = _require_mapping(raw_card, "glossary candidate card")
            glossary_ids.add(_required_str(card.get("glossary_id"), "candidate glossary id"))
    for raw in _require_list(request.sections.get("recency_neighbor_cards"), "recency cards"):
        row = _require_mapping(raw, "recency card")
        if set(row) != {"candidate_source", "entity_card", "card_hash"}:
            raise RegistryContractError("recency card fields mismatch")
        card = _require_mapping(row.get("entity_card"), "recency entity card")
        if row.get("candidate_source") != "recency_neighbor" or row.get("card_hash") != canonical_hash(card):
            raise RegistryContractError("recency card integrity mismatch")
        entity_ids.add(_required_str(card.get("entity_id"), "recency entity id"))
    return entity_ids, glossary_ids, overflow, entity_packet_surfaces


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
    """Apply a conservative mechanical gate without deciding co-reference."""

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
    clearly_contextual = _normalized_literal(checked_surface) in _CONTEXTUAL_NORMALIZED
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
    if outcome not in _ALIAS_GATE_OUTCOMES:
        raise RegistryContractError("alias gate produced an invalid outcome")
    record["gate_record_hash"] = canonical_hash(record)
    return record


def _reject_unstable_profile_text(text: str, label: str) -> None:
    normalized = _normalized_literal(text).replace(" ", "_")
    hits = sorted(marker for marker in _UNSTABLE_PROFILE_MARKERS if marker in normalized)
    if hits:
        raise RegistryContractError(f"{label} contains out-of-scope profile markers: {hits}")


@dataclass
class ChapterWorkingRegistryV4:
    state_lineage_id: str
    chapter_id: str
    source_manifest_hash: str
    parent_generation_id: str | None
    parent_snapshot: Mapping[str, Any]
    _state: dict[str, Any] = field(init=False)
    revision_hash: str = field(init=False)
    applied_request_fingerprints: list[str] = field(default_factory=list)
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
            "global_aliases": set(),
            "block_local_references": set(),
            "glossary_items": set(),
            "tickets": set(),
            "profile_revisions": set(),
            "attention_ledger": set(),
        }
    )

    def __post_init__(self) -> None:
        self.state_lineage_id = _required_str(self.state_lineage_id, "state_lineage_id")
        self.chapter_id = _required_str(self.chapter_id, "chapter_id")
        self.source_manifest_hash = _required_str(
            self.source_manifest_hash, "source_manifest_hash"
        )
        snapshot = _clone(self.parent_snapshot)
        if snapshot.get("schema_version") != REGISTRY_SCHEMA_VERSION:
            raise RegistryContractError("foreign registry snapshot cannot seed v4")
        if snapshot.get("state_lineage_id") != self.state_lineage_id:
            raise RegistryContractError("registry snapshot crosses state lineage")
        for table in self._created_ids:
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
    ) -> "ChapterWorkingRegistryV4":
        snapshot = parent_snapshot or empty_registry_snapshot_v4(state_lineage_id)
        return cls(
            state_lineage_id=state_lineage_id,
            chapter_id=chapter_id,
            source_manifest_hash=source_manifest_hash,
            parent_generation_id=(
                str(parent_generation_id)
                if parent_generation_id is not None
                else snapshot.get("generation_id")
            ),
            parent_snapshot=snapshot,
        )

    def snapshot(self) -> dict[str, Any]:
        id_fields = {
            "entities": "entity_id",
            "global_aliases": "alias_id",
            "block_local_references": "local_reference_id",
            "glossary_items": "glossary_id",
            "tickets": "ticket_id",
            "profile_revisions": "profile_revision_id",
            "attention_ledger": "attention_observation_id",
        }
        result: dict[str, Any] = {
            "builder_identity_mode": REGISTRY_SCHEMA_VERSION,
            "schema_version": REGISTRY_SCHEMA_VERSION,
            "state_lineage_id": self.state_lineage_id,
            "generation_id": self.parent_generation_id,
        }
        for table, id_field in id_fields.items():
            result[table] = sorted(
                _clone(self._state[table]), key=lambda row: str(row[id_field])
            )
        result["snapshot_hash"] = canonical_hash(result)
        return result

    def install_attention_ledger(self, rows: Sequence[Mapping[str, Any]]) -> None:
        for row in rows:
            checked = _require_mapping(row, "attention ledger row")
            if checked.get("chapter_id") != self.chapter_id:
                raise RegistryContractError("attention ledger crosses chapter")
            self._append_unique(
                "attention_ledger", "attention_observation_id", checked
            )
        self.revision_hash = _snapshot_revision(
            self.snapshot(), self.chapter_id, self.applied_request_fingerprints
        )

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
        self,
        request_fingerprint: str,
        list_name: str,
        row_index: int,
        payload: Mapping[str, Any],
    ) -> dict[str, Any]:
        return {
            "state_lineage_id": self.state_lineage_id,
            "chapter_id": self.chapter_id,
            "validated_request_fingerprint": request_fingerprint,
            "response_list_name": list_name,
            "response_row_index": row_index,
            "canonical_row_payload": _clone(payload),
        }

    def _source_hash(
        self, block_ids: Sequence[str], source_catalog: Mapping[str, str]
    ) -> str:
        return canonical_hash(
            [
                {"block_id": block_id, "source_text_hash": canonical_hash(source_catalog[block_id])}
                for block_id in block_ids
            ]
        )

    def _code_ticket(
        self,
        *,
        request_fingerprint: str,
        row_index: int,
        ticket_type: str,
        surface: str | None,
        source_block_ids: Sequence[str],
        source_catalog: Mapping[str, str],
        reason: str,
        subject_entity_ids: Sequence[str] = (),
        subject_glossary_ids: Sequence[str] = (),
        candidate_entity_ids: Sequence[str] = (),
        candidate_glossary_ids: Sequence[str] = (),
    ) -> dict[str, Any]:
        checked_type = _require_enum(ticket_type, CODE_TICKET_TYPES, "code ticket type")
        payload = {
            "ticket_type": checked_type,
            "surface": surface,
            "source_block_ids": list(source_block_ids),
            "subject_entity_ids": sorted(set(subject_entity_ids)),
            "subject_glossary_ids": sorted(set(subject_glossary_ids)),
            "candidate_entity_ids": sorted(set(candidate_entity_ids)),
            "candidate_glossary_ids": sorted(set(candidate_glossary_ids)),
            "referent_kind_claim": None,
            "proposed_identity_summary": None,
            "proposed_referential_gender": None,
            "reason": reason,
            "status": "open",
            "opened_by_request_fingerprint": request_fingerprint,
            "source_text_manifest_hash": self._source_hash(source_block_ids, source_catalog),
            "resolution_action": None,
            "resolution_note": None,
        }
        ticket_id = _mint_id(
            "tick4_", self._decision_key(request_fingerprint, "code_tickets", row_index, payload)
        )
        return _row_with_revision({"ticket_id": ticket_id, **payload})

    def _stage_surface_update(
        self,
        *,
        raw: Mapping[str, Any],
        target_entity_id: str,
        request: RenderedRegistryRequestV4,
        row_index: int,
        active_catalog: Mapping[str, str],
        active_order: Mapping[str, int],
        overflow: set[str],
        nested: bool,
    ) -> tuple[dict[str, Any] | None, dict[str, Any] | None, dict[str, Any] | None]:
        expected = {"update_kind", "surface", "name_class", "source_block_ids", "reason"}
        if not nested:
            expected.add("target_entity_id")
        _require_exact_keys(raw, expected, "surface update")
        update_kind = _require_enum(
            raw.get("update_kind"), SURFACE_UPDATE_KINDS, "surface update kind"
        )
        surface = _required_str(raw.get("surface"), "surface update surface")
        block_ids = sorted(
            _require_string_list(raw.get("source_block_ids"), "surface update blocks"),
            key=lambda value: (active_order.get(value, 10**9), value),
        )
        located = _located_support(
            surface=surface, block_ids=block_ids, active_catalog=active_catalog
        )
        if not located:
            ticket = self._code_ticket(
                request_fingerprint=request.request_fingerprint,
                row_index=30000 + row_index,
                ticket_type="unlocatable_surface",
                surface=surface,
                source_block_ids=block_ids,
                source_catalog=active_catalog,
                subject_entity_ids=[target_entity_id],
                reason="surface update is absent from one or more declared active blocks",
            )
            return None, None, ticket
        if _normalized_literal(surface) in overflow:
            ticket = self._code_ticket(
                request_fingerprint=request.request_fingerprint,
                row_index=31000 + row_index,
                ticket_type="candidate_overflow",
                surface=surface,
                source_block_ids=block_ids,
                source_catalog=active_catalog,
                subject_entity_ids=[target_entity_id],
                reason="candidate overflow forbids authoritative surface attachment",
            )
            return None, None, ticket
        name_class = raw.get("name_class")
        if update_kind == "global_name_alias":
            checked_class = _require_enum(name_class, NAME_CLASSES, "global alias name_class")
            gate = route_alias_for_commit(
                surface=surface,
                name_class=checked_class,
                target_entity_id=target_entity_id,
                source_block_ids=block_ids,
                source_catalog=active_catalog,
                source_decision_lineage={
                    "request_fingerprint": request.request_fingerprint,
                    "response_list_name": (
                        "initial_surface_updates" if nested else "surface_updates"
                    ),
                    "response_row_index": row_index,
                },
            )
            self.alias_gate_records.append(_clone(gate))
            if gate["outcome"] != "eligible_global_alias":
                ticket = self._code_ticket(
                    request_fingerprint=request.request_fingerprint,
                    row_index=32000 + row_index,
                    ticket_type="alias_scope_review",
                    surface=surface,
                    source_block_ids=block_ids,
                    source_catalog=active_catalog,
                    subject_entity_ids=[target_entity_id],
                    reason="global alias did not pass the unified scope gate",
                )
                return None, None, ticket
            payload = {
                "surface": surface,
                "name_class": checked_class,
                "entity_id": target_entity_id,
                "support_block_ids": block_ids,
                "created_by_request_fingerprint": request.request_fingerprint,
                "source_text_manifest_hash": self._source_hash(block_ids, active_catalog),
                "status": "pending",
                "gate_outcome": gate["outcome"],
                "gate_record_hash": gate["gate_record_hash"],
            }
            alias_id = _mint_id(
                "alias4_",
                {
                    "state_lineage_id": self.state_lineage_id,
                    "entity_id": target_entity_id,
                    "normalized_surface": _normalized_literal(surface),
                    "source_block_ids": block_ids,
                },
            )
            return _row_with_revision({"alias_id": alias_id, **payload}), None, None
        if name_class is not None:
            raise RegistryContractError("block-local reference requires name_class=null")
        payload = {
            "surface": surface,
            "entity_id": target_entity_id,
            "valid_block_ids": block_ids,
            "created_by_request_fingerprint": request.request_fingerprint,
            "source_text_manifest_hash": self._source_hash(block_ids, active_catalog),
            "status": "provisional",
        }
        local_id = _mint_id(
            "local4_",
            {
                "state_lineage_id": self.state_lineage_id,
                "entity_id": target_entity_id,
                "normalized_surface": _normalized_literal(surface),
                "valid_block_ids": block_ids,
            },
        )
        return None, _row_with_revision({"local_reference_id": local_id, **payload}), None

    def apply_b1_response(
        self,
        *,
        request: RenderedRegistryRequestV4,
        response: Mapping[str, Any],
    ) -> dict[str, Any]:
        if request.role != "b1" or request.chapter_id != self.chapter_id:
            raise RegistryContractError("working registry received a foreign B1 request")
        response_hash = canonical_hash(response)
        replay = self._replay.get(request.request_fingerprint)
        if replay is not None:
            if replay[0] != response_hash:
                raise RegistryContractError(
                    "one request fingerprint cannot accept two validated responses"
                )
            return _clone(replay[1])
        if request.parent_working_revision_hash != self.revision_hash:
            raise RegistryStaleRevisionError(
                f"stale B1 parent: expected {self.revision_hash}, got "
                f"{request.parent_working_revision_hash}"
            )
        _require_exact_keys(
            response,
            {"new_entities", "new_glossary_items", "surface_updates", "tickets"},
            "StableRegistryDeltaV4",
        )
        supplied_entities, supplied_glossary, overflow, packet_surfaces = (
            _decode_candidate_universe(request)
        )
        manifest = _require_mapping(
            request.sections.get("candidate_selection_manifest"),
            "candidate selection manifest",
        )
        active_rows = [
            _require_mapping(row, "active source block")
            for row in _require_list(
                request.sections.get("active_window_blocks"), "active_window_blocks"
            )
        ]
        active_catalog = {
            _required_str(row.get("block_id"), "active block id"): _required_str(
                row.get("text"), "active block text"
            )
            for row in active_rows
        }
        active_order = {
            str(row["block_id"]): int(row.get("order_index") or 0) for row in active_rows
        }
        source_order = lambda values: sorted(
            values, key=lambda value: (active_order.get(value, 10**9), value)
        )
        raw_entities = _require_list(response.get("new_entities"), "new_entities")
        raw_glossary = _require_list(
            response.get("new_glossary_items"), "new_glossary_items"
        )
        raw_updates = _require_list(response.get("surface_updates"), "surface_updates")
        raw_tickets = _require_list(response.get("tickets"), "tickets")

        staged_entities: list[dict[str, Any]] = []
        staged_glossary: list[dict[str, Any]] = []
        staged_aliases: list[dict[str, Any]] = []
        staged_locals: list[dict[str, Any]] = []
        code_tickets: list[dict[str, Any]] = []
        entity_surfaces: set[str] = set()
        glossary_surfaces: set[str] = set()
        nested_updates: dict[str, list[Mapping[str, Any]]] = {}

        for index, raw in enumerate(raw_entities):
            row = _require_mapping(raw, "new entity")
            _require_keys(
                row,
                required={
                    "surface",
                    "name_class",
                    "referent_kind_claim",
                    "identity_summary",
                    "source_block_ids",
                    "initial_surface_updates",
                },
                optional={"referential_gender_claim"},
                label="new entity",
            )
            surface = _required_str(row.get("surface"), "entity surface")
            normalized = _normalized_literal(surface)
            entity_surfaces.add(normalized)
            block_ids = source_order(
                _require_string_list(row.get("source_block_ids"), "entity source blocks")
            )
            located = _located_support(
                surface=surface, block_ids=block_ids, active_catalog=active_catalog
            )
            if not located:
                code_tickets.append(
                    self._code_ticket(
                        request_fingerprint=request.request_fingerprint,
                        row_index=10000 + index,
                        ticket_type="unlocatable_surface",
                        surface=surface,
                        source_block_ids=block_ids,
                        source_catalog=active_catalog,
                        reason="entity surface is absent from one or more declared active blocks",
                    )
                )
                continue
            name_class = row.get("name_class")
            name_class = (
                None
                if name_class is None
                else _require_enum(name_class, NAME_CLASSES, "entity name_class")
            )
            kind = _require_enum(
                row.get("referent_kind_claim"), REFERENT_KINDS, "entity referent kind"
            )
            summary = _required_str(row.get("identity_summary"), "identity_summary")
            _reject_unstable_profile_text(summary, "identity_summary")
            gender_claim = row.get("referential_gender_claim")
            gender: str | None = None
            gender_blocks: list[str] = []
            if gender_claim is not None:
                claim = _require_mapping(gender_claim, "referential_gender_claim")
                _require_exact_keys(
                    claim, {"value", "support_block_ids"}, "referential_gender_claim"
                )
                gender = _require_enum(
                    claim.get("value"), REFERENTIAL_GENDERS, "referential gender"
                )
                if kind not in {"person", "animal", "nonhuman_character"}:
                    raise RegistryContractError(
                        "referential gender is unsupported for this referent kind"
                    )
                gender_blocks = source_order(
                    _require_string_list(
                        claim.get("support_block_ids"), "referential gender support blocks"
                    )
                )
                if not set(gender_blocks) <= set(block_ids):
                    raise RegistryContractError(
                        "referential gender support must be exact entity support blocks"
                    )
            payload = {
                "canonical_surface": surface,
                "name_class": name_class,
                "referent_kind": kind,
                "identity_summary": summary,
                "referential_gender": gender,
                "referential_gender_support_block_ids": gender_blocks,
                "created_from_block_ids": [block_ids[0]],
                "support_block_ids": block_ids,
                "latest_profile_revision_id": None,
                "created_by_request_fingerprint": request.request_fingerprint,
                "source_text_manifest_hash": self._source_hash(block_ids, active_catalog),
                "status": "provisional",
            }
            entity_id = _mint_id(
                "ent4_",
                self._decision_key(request.request_fingerprint, "new_entities", index, payload),
            )
            staged_entities.append(
                _row_with_revision(
                    {"entity_id": entity_id, **payload, "located_spans": located}
                )
            )
            nested_updates[entity_id] = [
                _require_mapping(item, "initial surface update")
                for item in _require_list(
                    row.get("initial_surface_updates"), "initial_surface_updates"
                )
            ]
            if normalized in overflow:
                code_tickets.append(
                    self._code_ticket(
                        request_fingerprint=request.request_fingerprint,
                        row_index=11000 + index,
                        ticket_type="candidate_overflow",
                        surface=surface,
                        source_block_ids=block_ids,
                        source_catalog=active_catalog,
                        subject_entity_ids=[entity_id],
                        candidate_entity_ids=supplied_entities,
                        reason="candidate overflow keeps the new entity under review",
                    )
                )

        for index, raw in enumerate(raw_glossary):
            row = _require_mapping(raw, "new glossary item")
            _require_exact_keys(
                row,
                {"surface", "category_claim", "short_description", "source_block_ids"},
                "new glossary item",
            )
            surface = _required_str(row.get("surface"), "glossary surface")
            normalized = _normalized_literal(surface)
            glossary_surfaces.add(normalized)
            block_ids = source_order(
                _require_string_list(row.get("source_block_ids"), "glossary source blocks")
            )
            located = _located_support(
                surface=surface, block_ids=block_ids, active_catalog=active_catalog
            )
            if not located:
                code_tickets.append(
                    self._code_ticket(
                        request_fingerprint=request.request_fingerprint,
                        row_index=12000 + index,
                        ticket_type="unlocatable_surface",
                        surface=surface,
                        source_block_ids=block_ids,
                        source_catalog=active_catalog,
                        reason="glossary surface is absent from one or more declared active blocks",
                    )
                )
                continue
            payload = {
                "surface": surface,
                "category_claim": _require_enum(
                    row.get("category_claim"), GLOSSARY_CATEGORIES, "glossary category"
                ),
                "short_description": _required_str(
                    row.get("short_description"), "glossary short_description"
                ),
                "created_from_block_ids": [block_ids[0]],
                "support_block_ids": block_ids,
                "created_by_request_fingerprint": request.request_fingerprint,
                "source_text_manifest_hash": self._source_hash(block_ids, active_catalog),
                "status": "provisional",
            }
            glossary_id = _mint_id(
                "gls4_",
                self._decision_key(
                    request.request_fingerprint, "new_glossary_items", index, payload
                ),
            )
            staged_glossary.append(
                _row_with_revision(
                    {"glossary_id": glossary_id, **payload, "located_spans": located}
                )
            )
        if entity_surfaces & glossary_surfaces:
            raise RegistryContractError(
                "one response emits the same normalized surface as entity and glossary"
            )

        new_entities_by_surface: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for row in staged_entities:
            new_entities_by_surface[_normalized_literal(row["canonical_surface"])].append(row)
        new_glossary_by_surface: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for row in staged_glossary:
            new_glossary_by_surface[_normalized_literal(row["surface"])].append(row)

        # Multiple same-surface rows stay separate, but none may pass the clean gate
        # merely because code cannot identify which row a later ticket intended.
        for duplicate_index, rows in enumerate(
            group for group in new_entities_by_surface.values() if len(group) > 1
        ):
            duplicate_blocks = source_order(
                sorted({block_id for row in rows for block_id in row["support_block_ids"]})
            )
            code_tickets.append(
                self._code_ticket(
                    request_fingerprint=request.request_fingerprint,
                    row_index=12500 + duplicate_index,
                    ticket_type="ambiguous_new_subject",
                    surface=str(rows[0]["canonical_surface"]),
                    source_block_ids=duplicate_blocks,
                    source_catalog=active_catalog,
                    subject_entity_ids=[str(row["entity_id"]) for row in rows],
                    reason="multiple new same-surface entities require Auditor separation",
                )
            )

        staged_tickets: list[dict[str, Any]] = []
        for index, raw in enumerate(raw_tickets):
            row = _require_mapping(raw, "model ticket")
            _require_exact_keys(
                row,
                {
                    "ticket_type",
                    "surface",
                    "source_block_ids",
                    "candidate_entity_ids",
                    "candidate_glossary_ids",
                    "referent_kind_claim",
                    "proposed_identity_summary",
                    "proposed_referential_gender",
                    "reason",
                },
                "model ticket",
            )
            ticket_type = _require_enum(
                row.get("ticket_type"), MODEL_TICKET_TYPES, "model ticket type"
            )
            surface = _optional_str(row.get("surface"), "ticket surface")
            block_ids = source_order(
                _require_string_list(row.get("source_block_ids"), "ticket source blocks")
            )
            if not set(block_ids) <= set(active_catalog):
                raise RegistryContractError("ticket cites a foreign or context-only block")
            if surface is not None and not _located_support(
                surface=surface, block_ids=block_ids, active_catalog=active_catalog
            ):
                raise RegistryContractError("model ticket surface is absent from active blocks")
            candidate_entities = _require_string_list(
                row.get("candidate_entity_ids"),
                "ticket candidate entity ids",
                allow_empty=True,
            )
            candidate_glossary = _require_string_list(
                row.get("candidate_glossary_ids"),
                "ticket candidate glossary ids",
                allow_empty=True,
            )
            if not set(candidate_entities) <= supplied_entities:
                raise RegistryContractError("ticket cites foreign candidate entity")
            if not set(candidate_glossary) <= supplied_glossary:
                raise RegistryContractError("ticket cites foreign candidate glossary item")
            kind_value = row.get("referent_kind_claim")
            kind = (
                None
                if kind_value is None
                else _require_enum(kind_value, REFERENT_KINDS, "ticket referent kind")
            )
            summary = _optional_str(
                row.get("proposed_identity_summary"), "proposed identity summary"
            )
            if summary is not None:
                _reject_unstable_profile_text(summary, "proposed identity summary")
            gender_value = row.get("proposed_referential_gender")
            gender = (
                None
                if gender_value is None
                else _require_enum(
                    gender_value, REFERENTIAL_GENDERS, "proposed referential gender"
                )
            )
            if ticket_type in {"profile_conflict", "profile_enrichment"}:
                if len(candidate_entities) != 1 or bool(summary) == bool(gender):
                    raise RegistryContractError(
                        "profile ticket requires one candidate and exactly one proposed field"
                    )
            elif ticket_type == "important_unnamed_referent":
                if summary is None or gender is not None:
                    raise RegistryContractError(
                        "important unnamed ticket requires a stable summary and no profile patch"
                    )
            elif summary is not None or gender is not None:
                raise RegistryContractError(
                    "non-profile ticket cannot carry a profile patch"
                )
            if ticket_type == "important_unnamed_referent" and (
                surface is None or kind in {None, "unknown"} or summary is None
            ):
                raise RegistryContractError(
                    "important unnamed ticket requires surface, kind, and stable summary"
                )
            normalized = _normalized_literal(surface) if surface else ""
            entity_subjects = [
                entity
                for entity in new_entities_by_surface.get(normalized, [])
                if set(entity["support_block_ids"]).intersection(block_ids)
            ]
            glossary_subjects = [
                item
                for item in new_glossary_by_surface.get(normalized, [])
                if set(item["support_block_ids"]).intersection(block_ids)
            ]
            ambiguous = len(entity_subjects) > 1 or len(glossary_subjects) > 1
            subject_entity_ids = (
                [str(entity_subjects[0]["entity_id"])] if len(entity_subjects) == 1 else []
            )
            subject_glossary_ids = (
                [str(glossary_subjects[0]["glossary_id"])]
                if len(glossary_subjects) == 1
                else []
            )
            if ambiguous:
                code_tickets.append(
                    self._code_ticket(
                        request_fingerprint=request.request_fingerprint,
                        row_index=13000 + index,
                        ticket_type="ambiguous_new_subject",
                        surface=surface,
                        source_block_ids=block_ids,
                        source_catalog=active_catalog,
                        candidate_entity_ids=candidate_entities,
                        candidate_glossary_ids=candidate_glossary,
                        reason="ticket could attach to multiple new same-surface rows",
                    )
                )
                subject_entity_ids = []
                subject_glossary_ids = []
            payload = {
                "ticket_type": ticket_type,
                "surface": surface,
                "source_block_ids": block_ids,
                "subject_entity_ids": subject_entity_ids,
                "subject_glossary_ids": subject_glossary_ids,
                "candidate_entity_ids": candidate_entities,
                "candidate_glossary_ids": candidate_glossary,
                "referent_kind_claim": kind,
                "proposed_identity_summary": summary,
                "proposed_referential_gender": gender,
                "reason": _required_str(row.get("reason"), "ticket reason"),
                "status": "open",
                "opened_by_request_fingerprint": request.request_fingerprint,
                "source_text_manifest_hash": self._source_hash(block_ids, active_catalog),
                "resolution_action": None,
                "resolution_note": None,
            }
            ticket_id = _mint_id(
                "tick4_",
                self._decision_key(request.request_fingerprint, "tickets", index, payload),
            )
            staged_tickets.append(_row_with_revision({"ticket_id": ticket_id, **payload}))

        for index, raw in enumerate(raw_updates):
            row = _require_mapping(raw, "top-level surface update")
            target = _required_str(row.get("target_entity_id"), "surface update target")
            if target not in supplied_entities:
                raise RegistryContractError(
                    "top-level surface update targets a foreign or unsupplied entity"
                )
            alias, local, ticket = self._stage_surface_update(
                raw=row,
                target_entity_id=target,
                request=request,
                row_index=index,
                active_catalog=active_catalog,
                active_order=active_order,
                overflow=overflow,
                nested=False,
            )
            if alias:
                staged_aliases.append(alias)
            if local:
                staged_locals.append(local)
            if ticket:
                code_tickets.append(ticket)

        for entity in staged_entities:
            for nested_index, raw in enumerate(nested_updates[entity["entity_id"]]):
                alias, local, ticket = self._stage_surface_update(
                    raw=raw,
                    target_entity_id=str(entity["entity_id"]),
                    request=request,
                    row_index=1000 + nested_index,
                    active_catalog=active_catalog,
                    active_order=active_order,
                    overflow=overflow,
                    nested=True,
                )
                if alias:
                    staged_aliases.append(alias)
                if local:
                    staged_locals.append(local)
                if ticket:
                    code_tickets.append(ticket)

        for overflow_index, overflow_record in enumerate(manifest.get("overflow_records") or []):
            normalized = _required_str(
                overflow_record.get("normalized_surface"), "overflow normalized surface"
            )
            if any(
                ticket.get("ticket_type") == "candidate_overflow"
                and _normalized_literal(ticket.get("surface")) == normalized
                for ticket in code_tickets
            ):
                continue
            block_ids = source_order(
                _require_string_list(
                    overflow_record.get("matched_block_ids"), "overflow matched blocks"
                )
            )
            code_tickets.append(
                self._code_ticket(
                    request_fingerprint=request.request_fingerprint,
                    row_index=40000 + overflow_index,
                    ticket_type="candidate_overflow",
                    surface=_required_str(
                        overflow_record.get("source_surface"), "overflow source surface"
                    ),
                    source_block_ids=block_ids,
                    source_catalog=active_catalog,
                    subject_entity_ids=[
                        str(row["entity_id"])
                        for row in staged_entities
                        if _normalized_literal(row["canonical_surface"]) == normalized
                    ],
                    subject_glossary_ids=[
                        str(row["glossary_id"])
                        for row in staged_glossary
                        if _normalized_literal(row["surface"]) == normalized
                    ],
                    candidate_entity_ids=[
                        entity_id
                        for entity_id, surfaces in packet_surfaces.items()
                        if normalized
                        in {_normalized_literal(surface) for surface in surfaces}
                    ],
                    reason="candidate packet/card cap overflow requires Auditor review",
                )
            )

        all_tickets = staged_tickets + code_tickets
        ticketed_entities = {
            str(entity_id)
            for ticket in all_tickets
            for entity_id in ticket.get("subject_entity_ids") or []
        }
        ticketed_glossary = {
            str(glossary_id)
            for ticket in all_tickets
            for glossary_id in ticket.get("subject_glossary_ids") or []
        }
        for entity in staged_entities:
            normalized = _normalized_literal(entity["canonical_surface"])
            overlapping_candidates = {
                candidate_id
                for candidate_id, surfaces in packet_surfaces.items()
                if normalized in {_normalized_literal(surface) for surface in surfaces}
            }
            if overlapping_candidates:
                attached = any(
                    entity["entity_id"] in (ticket.get("subject_entity_ids") or [])
                    and overlapping_candidates.intersection(
                        ticket.get("candidate_entity_ids") or []
                    )
                    and ticket.get("ticket_type")
                    in {"same_name_collision", "possible_alias", "kind_conflict", "profile_conflict"}
                    for ticket in staged_tickets
                )
                if not attached:
                    raise RegistryContractError(
                        "new entity overlaps supplied candidates but lacks an identity-review ticket"
                    )
            if entity["referent_kind"] == "unknown" and entity["entity_id"] not in ticketed_entities:
                raise RegistryContractError("unknown entity must remain attached to a review ticket")
            if entity["entity_id"] not in ticketed_entities:
                self.clean_entity_ids.add(str(entity["entity_id"]))
        for item in staged_glossary:
            if item["glossary_id"] not in ticketed_glossary:
                self.clean_glossary_ids.add(str(item["glossary_id"]))

        for row in staged_entities:
            self._append_unique("entities", "entity_id", row)
        for row in staged_aliases:
            self._append_unique("global_aliases", "alias_id", row)
        for row in staged_locals:
            self._append_unique(
                "block_local_references", "local_reference_id", row
            )
        for row in staged_glossary:
            self._append_unique("glossary_items", "glossary_id", row)
        for row in all_tickets:
            self._append_unique("tickets", "ticket_id", row)

        record = {
            "request_fingerprint": request.request_fingerprint,
            "response_hash": response_hash,
            "entity_ids": [row["entity_id"] for row in staged_entities],
            "glossary_ids": [row["glossary_id"] for row in staged_glossary],
            "alias_ids": [row["alias_id"] for row in staged_aliases],
            "local_reference_ids": [row["local_reference_id"] for row in staged_locals],
            "ticket_ids": [row["ticket_id"] for row in all_tickets],
        }
        self.applied_request_fingerprints.append(request.request_fingerprint)
        self.candidate_manifest_hashes.append(str(manifest["manifest_hash"]))
        self.application_records.append(_clone(record))
        self.revision_hash = _snapshot_revision(
            self.snapshot(), self.chapter_id, self.applied_request_fingerprints
        )
        self._replay[request.request_fingerprint] = (response_hash, _clone(record))
        return _clone(record)


def _replace_row(
    working: ChapterWorkingRegistryV4,
    table: str,
    id_field: str,
    row_id: str,
    changes: Mapping[str, Any],
) -> dict[str, Any]:
    rows = working._state[table]
    for index, row in enumerate(rows):
        if str(row[id_field]) != str(row_id):
            continue
        updated = {**row, **_clone(changes)}
        updated.pop("revision_hash", None)
        rows[index] = _row_with_revision(updated)
        return rows[index]
    raise RegistryContractError(f"missing {table} row: {row_id}")


def _remove_row(
    working: ChapterWorkingRegistryV4, table: str, id_field: str, row_id: str
) -> None:
    before = len(working._state[table])
    working._state[table] = [
        row for row in working._state[table] if str(row[id_field]) != str(row_id)
    ]
    if len(working._state[table]) == before:
        raise RegistryContractError(f"missing {table} row: {row_id}")


def build_exception_components(working: ChapterWorkingRegistryV4) -> dict[str, Any]:
    tickets = [
        _clone(row)
        for row in working.snapshot().get("tickets") or []
        if row.get("status") == "open"
    ]
    if not tickets:
        body = {"components": [], "ticket_count": 0, "component_count": 0}
        return {**body, "manifest_hash": canonical_hash(body)}
    parent = list(range(len(tickets)))

    def find(index: int) -> int:
        while parent[index] != index:
            parent[index] = parent[parent[index]]
            index = parent[index]
        return index

    def union(left: int, right: int) -> None:
        a, b = find(left), find(right)
        if a != b:
            parent[max(a, b)] = min(a, b)

    keys: list[set[str]] = []
    for ticket in tickets:
        ticket_keys = {
            f"entity:{value}"
            for field in ("subject_entity_ids", "candidate_entity_ids")
            for value in ticket.get(field) or []
        }
        ticket_keys |= {
            f"glossary:{value}"
            for field in ("subject_glossary_ids", "candidate_glossary_ids")
            for value in ticket.get(field) or []
        }
        if ticket.get("surface"):
            ticket_keys.add(f"surface:{_normalized_literal(ticket['surface'])}")
        keys.append(ticket_keys)
    for left in range(len(tickets)):
        for right in range(left + 1, len(tickets)):
            if keys[left].intersection(keys[right]):
                union(left, right)
    grouped: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for index, ticket in enumerate(tickets):
        grouped[find(index)].append(ticket)
    components: list[dict[str, Any]] = []
    for rows in grouped.values():
        ordered = sorted(rows, key=lambda row: str(row["ticket_id"]))
        ticket_ids = [str(row["ticket_id"]) for row in ordered]
        components.append(
            {
                "component_id": "comp4_"
                + canonical_hash(
                    {
                        "chapter_id": working.chapter_id,
                        "ticket_ids": ticket_ids,
                        "working_revision_hash": working.revision_hash,
                    }
                )[:20],
                "ticket_ids": ticket_ids,
                "source_block_ids": sorted(
                    {str(value) for row in ordered for value in row.get("source_block_ids") or []}
                ),
                "entity_ids": sorted(
                    {
                        str(value)
                        for row in ordered
                        for field in ("subject_entity_ids", "candidate_entity_ids")
                        for value in row.get(field) or []
                    }
                ),
                "glossary_ids": sorted(
                    {
                        str(value)
                        for row in ordered
                        for field in ("subject_glossary_ids", "candidate_glossary_ids")
                        for value in row.get(field) or []
                    }
                ),
                "profile_target_entity_ids": sorted(
                    {
                        str((row.get("candidate_entity_ids") or [""])[0])
                        for row in ordered
                        if row.get("ticket_type")
                        in {"profile_conflict", "profile_enrichment"}
                        and len(row.get("candidate_entity_ids") or []) == 1
                    }
                    - {""}
                ),
            }
        )
    components.sort(key=lambda row: row["component_id"])
    body = {
        "components": components,
        "ticket_count": len(tickets),
        "component_count": len(components),
    }
    return {**body, "manifest_hash": canonical_hash(body)}


def render_auditor_requests(
    *,
    working: ChapterWorkingRegistryV4,
    orientation: Mapping[str, Any],
    source_catalog: Mapping[str, str],
    block_order: Mapping[str, int],
    design_doc: Path,
    run_config: RunConfigV4,
) -> list[RenderedRegistryRequestV4]:
    component_manifest = build_exception_components(working)
    components = list(component_manifest["components"])
    if len(components) > run_config.auditor_calls_per_chapter_cap:
        raise RegistryBudgetError("Auditor component count exceeds chapter call cap")
    snapshot = working.snapshot()
    tickets_by_id = {str(row["ticket_id"]): row for row in snapshot["tickets"]}
    entities_by_id = {str(row["entity_id"]): row for row in snapshot["entities"]}
    glossary_by_id = {str(row["glossary_id"]): row for row in snapshot["glossary_items"]}
    aliases = list(snapshot["global_aliases"])
    locals_ = list(snapshot["block_local_references"])
    profile_revisions = list(snapshot["profile_revisions"])
    roster = [
        {
            "entity_id": str(row["entity_id"]),
            "canonical_surface": str(row["canonical_surface"]),
            "referent_kind": str(row["referent_kind"]),
            "referential_gender": row.get("referential_gender"),
            "status": str(row["status"]),
        }
        for row in sorted(snapshot["entities"], key=lambda item: str(item["entity_id"]))
    ]
    requests: list[RenderedRegistryRequestV4] = []
    for component in components:
        ticket_ids = list(component["ticket_ids"])
        if len(ticket_ids) > run_config.auditor_tickets_per_component_cap:
            raise RegistryBudgetError("Auditor component exceeds ticket cap")
        source_ids = sorted(
            component["source_block_ids"],
            key=lambda value: (block_order.get(value, 10**9), value),
        )
        if not set(source_ids) <= set(source_catalog):
            raise RegistryContractError("Auditor component cites foreign source block")
        entity_ids = set(component["entity_ids"])
        glossary_ids = set(component["glossary_ids"])
        narrative_context = _require_mapping(
            orientation.get("narrative_context"), "B0 narrative context"
        )
        sections = {
            "b0_orientation_draft": _required_str(
                orientation.get("orientation_draft"), "B0 orientation draft"
            ),
            # Compatibility projection for the unchanged v4 Auditor prompt. This
            # is one chapter-level orientation note, not a narrator/block map.
            "narrator_hypotheses": [
                {
                    "surface": None,
                    "note": _required_str(
                        narrative_context.get("note"), "B0 narrative context note"
                    ),
                    "source_block_ids": _require_string_list(
                        narrative_context.get("support_block_ids"),
                        "B0 narrative context support blocks",
                    ),
                }
            ],
            "compact_chapter_roster": roster,
            "component_manifest": _clone(component),
            "owned_tickets": [_clone(tickets_by_id[ticket_id]) for ticket_id in ticket_ids],
            "owned_entities": [
                _clone(entities_by_id[entity_id])
                for entity_id in sorted(entity_ids)
                if entity_id in entities_by_id
            ],
            "owned_global_aliases": [
                _clone(row) for row in aliases if str(row.get("entity_id")) in entity_ids
            ],
            "owned_block_local_references": [
                _clone(row) for row in locals_ if str(row.get("entity_id")) in entity_ids
            ],
            "owned_glossary_items": [
                _clone(glossary_by_id[glossary_id])
                for glossary_id in sorted(glossary_ids)
                if glossary_id in glossary_by_id
            ],
            "owned_profile_revisions": [
                _clone(row)
                for row in profile_revisions
                if str(row.get("entity_id")) in entity_ids
            ],
            "cited_source_blocks": [
                {
                    "block_id": block_id,
                    "order_index": int(block_order.get(block_id, 0)),
                    "text": _nfc(source_catalog[block_id]),
                }
                for block_id in source_ids
            ],
            "working_registry_revision_hash": working.revision_hash,
            "cap_manifest": {
                "auditor_tickets_per_component_cap": run_config.auditor_tickets_per_component_cap,
                "auditor_neighbor_blocks_each_side": run_config.auditor_neighbor_blocks_each_side,
            },
        }
        if "attention_ledger" in sections or "advisory_attention" in canonical_json(sections):
            raise RegistryContractError("Auditor request must not contain B0 attention inventory")
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
                f"Auditor input {tokens} exceeds cap {run_config.auditor_input_token_cap}"
            )
        requests.append(request)
    return requests


def validate_audit_response(
    request: RenderedRegistryRequestV4, response: Mapping[str, Any]
) -> dict[str, Any]:
    if request.role != "auditor":
        raise RegistryContractError("audit validator received a non-Auditor request")
    _require_exact_keys(
        response,
        {"ticket_dispositions", "profile_revisions"},
        "ChapterRegistryAuditV4",
    )
    owned_tickets = [
        _require_mapping(row, "owned ticket")
        for row in _require_list(request.sections.get("owned_tickets"), "owned_tickets")
    ]
    tickets_by_id = {str(row["ticket_id"]): row for row in owned_tickets}
    entity_rows = {
        str(row["entity_id"]): row
        for row in _require_list(request.sections.get("owned_entities"), "owned_entities")
    }
    glossary_rows = {
        str(row["glossary_id"]): row
        for row in _require_list(
            request.sections.get("owned_glossary_items"), "owned_glossary_items"
        )
    }
    source_blocks = {
        str(row["block_id"])
        for row in _require_list(
            request.sections.get("cited_source_blocks"), "cited_source_blocks"
        )
    }
    dispositions: list[dict[str, Any]] = []
    for raw in _require_list(response.get("ticket_dispositions"), "ticket_dispositions"):
        row = _require_mapping(raw, "ticket disposition")
        _require_exact_keys(
            row,
            {
                "ticket_id",
                "action",
                "source_entity_id",
                "target_entity_id",
                "source_glossary_id",
                "target_glossary_id",
                "resolved_referent_kind",
                "name_class",
                "valid_block_ids",
                "resolution_note",
            },
            "ticket disposition",
        )
        ticket_id = _required_str(row.get("ticket_id"), "ticket_id")
        ticket = tickets_by_id.get(ticket_id)
        if ticket is None:
            raise RegistryContractError("Auditor disposition cites foreign ticket")
        action = _require_enum(
            row.get("action"),
            AUDIT_ALLOWED_ACTIONS[str(ticket["ticket_type"])],
            "Auditor action",
        )
        source_entity = _optional_str(row.get("source_entity_id"), "source entity id")
        target_entity = _optional_str(row.get("target_entity_id"), "target entity id")
        source_glossary = _optional_str(row.get("source_glossary_id"), "source glossary id")
        target_glossary = _optional_str(row.get("target_glossary_id"), "target glossary id")
        for entity_id in (source_entity, target_entity):
            if entity_id is not None and entity_id not in entity_rows:
                raise RegistryContractError("Auditor disposition invents entity id")
        for glossary_id in (source_glossary, target_glossary):
            if glossary_id is not None and glossary_id not in glossary_rows:
                raise RegistryContractError("Auditor disposition invents glossary id")
        kind_value = row.get("resolved_referent_kind")
        kind = (
            None
            if kind_value is None
            else _require_enum(kind_value, REFERENT_KINDS, "resolved referent kind")
        )
        name_value = row.get("name_class")
        name_class = (
            None
            if name_value is None
            else _require_enum(name_value, NAME_CLASSES, "Auditor name_class")
        )
        valid_blocks = _require_string_list(
            row.get("valid_block_ids"), "valid block ids", allow_empty=True
        )
        if not set(valid_blocks) <= source_blocks:
            raise RegistryContractError("Auditor local scope cites foreign block")
        if action == "confirm_distinct_entity" and (
            source_entity is None
            or source_entity not in set(ticket.get("subject_entity_ids") or [])
            or entity_rows[source_entity].get("status") != "provisional"
        ):
            raise RegistryContractError(
                "confirm_distinct_entity requires the ticket's provisional subject"
            )
        if action == "merge_as_alias" and (
            source_entity is None
            or target_entity is None
            or source_entity == target_entity
            or name_class is None
            or source_entity not in set(ticket.get("subject_entity_ids") or [])
            or target_entity not in set(ticket.get("candidate_entity_ids") or [])
            or entity_rows[source_entity].get("status") != "provisional"
        ):
            raise RegistryContractError("merge_as_alias requires distinct entities and name class")
        if action == "create_unnamed_entity" and kind in {None, "unknown"}:
            raise RegistryContractError("create_unnamed_entity requires resolved kind")
        if action == "promote_global_alias" and (
            target_entity is None or name_class is None
            or target_entity not in set(ticket.get("candidate_entity_ids") or [])
        ):
            raise RegistryContractError("promote_global_alias requires target and name class")
        if action == "confirm_block_local_reference" and (
            target_entity is None
            or target_entity not in set(ticket.get("candidate_entity_ids") or [])
            or not valid_blocks
            or not set(valid_blocks) <= set(ticket.get("source_block_ids") or [])
        ):
            raise RegistryContractError("invalid block-local reference disposition")
        if action == "merge_glossary" and (
            source_glossary is None
            or target_glossary is None
            or source_glossary == target_glossary
            or source_glossary not in set(ticket.get("subject_glossary_ids") or [])
            or target_glossary not in set(ticket.get("candidate_glossary_ids") or [])
            or glossary_rows[source_glossary].get("status") != "provisional"
        ):
            raise RegistryContractError("merge_glossary requires distinct glossary rows")
        if action == "confirm_distinct_glossary" and (
            source_glossary is None
            or source_glossary not in set(ticket.get("subject_glossary_ids") or [])
            or glossary_rows[source_glossary].get("status") != "provisional"
        ):
            raise RegistryContractError(
                "confirm_distinct_glossary requires the ticket's provisional subject"
            )
        if action == "revise_profile" and (
            target_entity is None
            or kind is not None
            or ticket.get("candidate_entity_ids") != [target_entity]
        ):
            raise RegistryContractError(
                "revise_profile requires target and expresses kind only in profile revision"
            )
        if action != "create_unnamed_entity" and kind is not None:
            raise RegistryContractError("resolved_referent_kind is invalid for this action")
        if action not in {"merge_as_alias", "promote_global_alias"} and name_class is not None:
            raise RegistryContractError("name_class is invalid for this action")
        if action != "confirm_block_local_reference" and valid_blocks:
            raise RegistryContractError("valid_block_ids must be empty for this action")
        dispositions.append(
            {
                "ticket_id": ticket_id,
                "action": action,
                "source_entity_id": source_entity,
                "target_entity_id": target_entity,
                "source_glossary_id": source_glossary,
                "target_glossary_id": target_glossary,
                "resolved_referent_kind": kind,
                "name_class": name_class,
                "valid_block_ids": valid_blocks,
                "resolution_note": _required_str(
                    row.get("resolution_note"), "resolution_note"
                ),
            }
        )
    disposition_ids = [row["ticket_id"] for row in dispositions]
    if len(disposition_ids) != len(set(disposition_ids)) or set(disposition_ids) != set(
        tickets_by_id
    ):
        raise RegistryContractError("Auditor dispositions must exact-cover owned tickets")

    revisions: list[dict[str, Any]] = []
    seen_targets: set[str] = set()
    covered_revision_tickets: set[str] = set()
    revise_ticket_ids = {
        row["ticket_id"] for row in dispositions if row["action"] == "revise_profile"
    }
    for raw in _require_list(response.get("profile_revisions"), "profile_revisions"):
        row = _require_mapping(raw, "profile revision")
        _require_exact_keys(
            row,
            {
                "target_entity_id",
                "source_ticket_ids",
                "referent_kind_update",
                "identity_summary_update",
                "referential_gender_update",
                "resolution_note",
            },
            "profile revision",
        )
        target = _required_str(row.get("target_entity_id"), "profile target entity")
        if target not in entity_rows or target in seen_targets:
            raise RegistryContractError("profile revision has foreign or duplicate target")
        source_ticket_ids = _require_string_list(
            row.get("source_ticket_ids"), "profile source ticket ids"
        )
        if not set(source_ticket_ids) <= revise_ticket_ids:
            raise RegistryContractError("profile revision cites non-revise ticket")
        for ticket_id in source_ticket_ids:
            ticket = tickets_by_id[ticket_id]
            if ticket.get("candidate_entity_ids") != [target]:
                raise RegistryContractError("profile revision ticket targets another entity")
        kind_value = row.get("referent_kind_update")
        kind = (
            None
            if kind_value is None
            else _require_enum(kind_value, REFERENT_KINDS, "profile kind update")
        )
        summary = _optional_str(
            row.get("identity_summary_update"), "profile identity summary update"
        )
        if summary is not None:
            _reject_unstable_profile_text(summary, "profile identity summary update")
        gender_value = row.get("referential_gender_update")
        gender = (
            None
            if gender_value is None
            else _require_enum(
                gender_value, REFERENTIAL_GENDERS, "profile gender update"
            )
        )
        if kind is None and summary is None and gender is None:
            raise RegistryContractError("profile revision must change at least one field")
        if covered_revision_tickets.intersection(source_ticket_ids):
            raise RegistryContractError("revise_profile ticket appears in multiple revisions")
        seen_targets.add(target)
        covered_revision_tickets.update(source_ticket_ids)
        revisions.append(
            {
                "target_entity_id": target,
                "source_ticket_ids": source_ticket_ids,
                "referent_kind_update": kind,
                "identity_summary_update": summary,
                "referential_gender_update": gender,
                "resolution_note": _required_str(
                    row.get("resolution_note"), "profile resolution_note"
                ),
            }
        )
    if covered_revision_tickets != revise_ticket_ids:
        raise RegistryContractError("profile revisions must exact-cover revise_profile tickets")
    return {
        "ticket_dispositions": dispositions,
        "profile_revisions": revisions,
        "response_hash": canonical_hash(response),
    }


def _audit_alias_row(
    *,
    working: ChapterWorkingRegistryV4,
    request: RenderedRegistryRequestV4,
    surface: str,
    name_class: str,
    target_entity_id: str,
    block_ids: Sequence[str],
    source_catalog: Mapping[str, str],
    purpose: str,
) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    gate = route_alias_for_commit(
        surface=surface,
        name_class=name_class,
        target_entity_id=target_entity_id,
        source_block_ids=block_ids,
        source_catalog=source_catalog,
        source_decision_lineage={
            "auditor_request_fingerprint": request.request_fingerprint,
            "purpose": purpose,
        },
    )
    working.alias_gate_records.append(_clone(gate))
    if gate["outcome"] != "eligible_global_alias":
        return None, gate
    payload = {
        "surface": surface,
        "name_class": name_class,
        "entity_id": target_entity_id,
        "support_block_ids": list(block_ids),
        "created_by_request_fingerprint": request.request_fingerprint,
        "source_text_manifest_hash": working._source_hash(block_ids, source_catalog),
        "status": "confirmed",
        "gate_outcome": gate["outcome"],
        "gate_record_hash": gate["gate_record_hash"],
    }
    alias_id = _mint_id(
        "alias4_",
        {
            "state_lineage_id": working.state_lineage_id,
            "entity_id": target_entity_id,
            "normalized_surface": _normalized_literal(surface),
            "source_block_ids": list(block_ids),
        },
    )
    return _row_with_revision({"alias_id": alias_id, **payload}), gate


def apply_audit_responses(
    *,
    working: ChapterWorkingRegistryV4,
    requests: Sequence[RenderedRegistryRequestV4],
    responses: Sequence[Mapping[str, Any]],
    source_catalog: Mapping[str, str],
) -> list[dict[str, Any]]:
    if len(requests) != len(responses):
        raise RegistryContractError("Auditor request/response count mismatch")
    expected_open_tickets = {
        str(row["ticket_id"])
        for row in working.snapshot().get("tickets") or []
        if row.get("status") == "open"
    }
    outputs: list[dict[str, Any]] = []
    seen_tickets: set[str] = set()
    for request, raw_response in zip(requests, responses):
        if request.parent_working_revision_hash != working.revision_hash:
            raise RegistryStaleRevisionError("Auditor request targets a stale working revision")
        validated = validate_audit_response(request, raw_response)
        ticket_index = working._index("tickets", "ticket_id")
        owned_ticket_ids = {
            str(row["ticket_id"]) for row in request.sections.get("owned_tickets") or []
        }
        if seen_tickets.intersection(owned_ticket_ids):
            raise RegistryContractError("ticket appears in multiple Auditor requests")
        seen_tickets.update(owned_ticket_ids)
        profile_rows_by_target = {
            str(row["target_entity_id"]): row for row in validated["profile_revisions"]
        }
        applied_profile_targets: set[str] = set()

        for disposition in validated["ticket_dispositions"]:
            ticket_id = str(disposition["ticket_id"])
            ticket = ticket_index.get(ticket_id)
            if ticket is None or ticket.get("status") != "open":
                raise RegistryContractError("Auditor disposition targets missing/closed ticket")
            action = str(disposition["action"])
            effective_action = action
            resolution_note = str(disposition["resolution_note"])
            if action == "confirm_distinct_entity":
                _replace_row(
                    working,
                    "entities",
                    "entity_id",
                    str(disposition["source_entity_id"]),
                    {"status": "confirmed"},
                )
            elif action == "merge_as_alias":
                source_id = str(disposition["source_entity_id"])
                target_id = str(disposition["target_entity_id"])
                source = working._index("entities", "entity_id").get(source_id)
                target = working._index("entities", "entity_id").get(target_id)
                if source is None or target is None or source_id not in working._created_ids["entities"]:
                    raise RegistryContractError(
                        "merge_as_alias may retire only an uncommitted provisional source"
                    )
                alias, gate = _audit_alias_row(
                    working=working,
                    request=request,
                    surface=str(source["canonical_surface"]),
                    name_class=str(disposition["name_class"]),
                    target_entity_id=target_id,
                    block_ids=ticket["source_block_ids"],
                    source_catalog=source_catalog,
                    purpose="merge_as_alias",
                )
                if alias is None:
                    effective_action = "remain_pending"
                    resolution_note = (
                        resolution_note + "; alias gate deferred merge: " + gate["reason_code"]
                    )
                    _replace_row(
                        working, "entities", "entity_id", source_id, {"status": "pending"}
                    )
                else:
                    working._append_unique("global_aliases", "alias_id", alias)
                    _replace_row(
                        working,
                        "entities",
                        "entity_id",
                        target_id,
                        {
                            "support_block_ids": sorted(
                                set(target.get("support_block_ids") or [])
                                | set(source.get("support_block_ids") or [])
                            )
                        },
                    )
                    _remove_row(working, "entities", "entity_id", source_id)
            elif action == "create_unnamed_entity":
                block_ids = list(ticket["source_block_ids"])
                payload = {
                    "canonical_surface": _required_str(
                        ticket.get("surface"), "unnamed entity display surface"
                    ),
                    "name_class": None,
                    "referent_kind": str(disposition["resolved_referent_kind"]),
                    "identity_summary": _required_str(
                        ticket.get("proposed_identity_summary"),
                        "unnamed entity identity summary",
                    ),
                    "referential_gender": None,
                    "referential_gender_support_block_ids": [],
                    "created_from_block_ids": [block_ids[0]],
                    "support_block_ids": block_ids,
                    "latest_profile_revision_id": None,
                    "created_by_request_fingerprint": request.request_fingerprint,
                    "source_text_manifest_hash": working._source_hash(block_ids, source_catalog),
                    "status": "confirmed",
                }
                entity_id = _mint_id(
                    "ent4_",
                    {
                        "state_lineage_id": working.state_lineage_id,
                        "chapter_id": working.chapter_id,
                        "ticket_id": ticket_id,
                        "payload": payload,
                    },
                )
                working._append_unique(
                    "entities",
                    "entity_id",
                    _row_with_revision({"entity_id": entity_id, **payload}),
                )
            elif action == "promote_global_alias":
                alias, gate = _audit_alias_row(
                    working=working,
                    request=request,
                    surface=_required_str(ticket.get("surface"), "promoted alias surface"),
                    name_class=str(disposition["name_class"]),
                    target_entity_id=str(disposition["target_entity_id"]),
                    block_ids=ticket["source_block_ids"],
                    source_catalog=source_catalog,
                    purpose="promote_global_alias",
                )
                if alias is None:
                    effective_action = "remain_pending"
                    resolution_note = (
                        resolution_note
                        + "; alias gate kept proposal pending: "
                        + gate["reason_code"]
                    )
                else:
                    working._append_unique("global_aliases", "alias_id", alias)
            elif action == "confirm_block_local_reference":
                block_ids = list(disposition["valid_block_ids"])
                payload = {
                    "surface": _required_str(ticket.get("surface"), "local reference surface"),
                    "entity_id": str(disposition["target_entity_id"]),
                    "valid_block_ids": block_ids,
                    "created_by_request_fingerprint": request.request_fingerprint,
                    "source_text_manifest_hash": working._source_hash(block_ids, source_catalog),
                    "status": "confirmed",
                }
                local_id = _mint_id(
                    "local4_",
                    {
                        "state_lineage_id": working.state_lineage_id,
                        "entity_id": payload["entity_id"],
                        "normalized_surface": _normalized_literal(payload["surface"]),
                        "valid_block_ids": block_ids,
                    },
                )
                working._append_unique(
                    "block_local_references",
                    "local_reference_id",
                    _row_with_revision({"local_reference_id": local_id, **payload}),
                )
            elif action == "confirm_distinct_glossary":
                _replace_row(
                    working,
                    "glossary_items",
                    "glossary_id",
                    str(disposition["source_glossary_id"]),
                    {"status": "confirmed"},
                )
            elif action == "merge_glossary":
                source_id = str(disposition["source_glossary_id"])
                target_id = str(disposition["target_glossary_id"])
                source = working._index("glossary_items", "glossary_id").get(source_id)
                target = working._index("glossary_items", "glossary_id").get(target_id)
                if source is None or target is None or source_id not in working._created_ids["glossary_items"]:
                    raise RegistryContractError(
                        "merge_glossary may retire only an uncommitted provisional source"
                    )
                _replace_row(
                    working,
                    "glossary_items",
                    "glossary_id",
                    target_id,
                    {
                        "support_block_ids": sorted(
                            set(target.get("support_block_ids") or [])
                            | set(source.get("support_block_ids") or [])
                        )
                    },
                )
                _remove_row(working, "glossary_items", "glossary_id", source_id)
            elif action == "revise_profile":
                target = str(disposition["target_entity_id"])
                if target not in profile_rows_by_target:
                    raise RegistryContractError("revise_profile lacks consolidated revision")
                applied_profile_targets.add(target)
            elif action == "reject_noise":
                for entity_id in ticket.get("subject_entity_ids") or []:
                    if entity_id in working._created_ids["entities"] and entity_id in working._index(
                        "entities", "entity_id"
                    ):
                        _remove_row(working, "entities", "entity_id", str(entity_id))
                for glossary_id in ticket.get("subject_glossary_ids") or []:
                    if glossary_id in working._created_ids[
                        "glossary_items"
                    ] and glossary_id in working._index("glossary_items", "glossary_id"):
                        _remove_row(
                            working, "glossary_items", "glossary_id", str(glossary_id)
                        )
            elif action == "remain_pending":
                for entity_id in ticket.get("subject_entity_ids") or []:
                    if entity_id in working._index("entities", "entity_id"):
                        _replace_row(
                            working,
                            "entities",
                            "entity_id",
                            str(entity_id),
                            {"status": "pending"},
                        )
                for glossary_id in ticket.get("subject_glossary_ids") or []:
                    if glossary_id in working._index("glossary_items", "glossary_id"):
                        _replace_row(
                            working,
                            "glossary_items",
                            "glossary_id",
                            str(glossary_id),
                            {"status": "pending"},
                        )
            elif action in {"defer_to_b2", "reject_noise"}:
                pass
            else:
                raise RegistryContractError(f"unimplemented audit action: {action}")
            _replace_row(
                working,
                "tickets",
                "ticket_id",
                ticket_id,
                {
                    "status": "carried" if effective_action == "remain_pending" else "resolved",
                    "resolution_action": effective_action,
                    "resolution_note": resolution_note,
                },
            )
            outputs.append(
                {**_clone(disposition), "effective_action": effective_action}
            )

        for target, revision in profile_rows_by_target.items():
            if target not in applied_profile_targets:
                raise RegistryContractError("profile revision has no revise_profile disposition")
            entity = working._index("entities", "entity_id").get(target)
            if entity is None:
                raise RegistryContractError("profile revision target is missing")
            source_tickets = [ticket_index[ticket_id] for ticket_id in revision["source_ticket_ids"]]
            summary_support = sorted(
                {
                    str(block_id)
                    for ticket in source_tickets
                    if ticket.get("proposed_identity_summary") is not None
                    for block_id in ticket.get("source_block_ids") or []
                }
            )
            gender_support = sorted(
                {
                    str(block_id)
                    for ticket in source_tickets
                    if ticket.get("proposed_referential_gender") is not None
                    for block_id in ticket.get("source_block_ids") or []
                }
            )
            kind_support = sorted(
                {
                    str(block_id)
                    for ticket in source_tickets
                    if ticket.get("referent_kind_claim") is not None
                    for block_id in ticket.get("source_block_ids") or []
                }
            )
            prior_projection = {
                "referent_kind": entity["referent_kind"],
                "identity_summary": entity["identity_summary"],
                "referential_gender": entity.get("referential_gender"),
                "referential_gender_support_block_ids": entity.get(
                    "referential_gender_support_block_ids"
                )
                or [],
                "latest_profile_revision_id": entity.get("latest_profile_revision_id"),
            }
            updated_projection = {
                **prior_projection,
                "referent_kind": revision["referent_kind_update"]
                if revision["referent_kind_update"] is not None
                else entity["referent_kind"],
                "identity_summary": revision["identity_summary_update"]
                if revision["identity_summary_update"] is not None
                else entity["identity_summary"],
                "referential_gender": revision["referential_gender_update"]
                if revision["referential_gender_update"] is not None
                else entity.get("referential_gender"),
                "referential_gender_support_block_ids": gender_support
                if revision["referential_gender_update"] is not None
                else entity.get("referential_gender_support_block_ids")
                or [],
            }
            revision_payload = {
                "entity_id": target,
                "source_ticket_ids": list(revision["source_ticket_ids"]),
                "prior_profile_hash": canonical_hash(prior_projection),
                "referent_kind_update": revision["referent_kind_update"],
                "identity_summary_update": revision["identity_summary_update"],
                "referential_gender_update": revision["referential_gender_update"],
                "identity_summary_support_block_ids": summary_support,
                "referential_gender_support_block_ids": gender_support,
                "result_profile_hash": canonical_hash(updated_projection),
                "created_by_auditor_request_fingerprint": request.request_fingerprint,
                "source_text_manifest_hash": working._source_hash(
                    sorted(set(kind_support + summary_support + gender_support)), source_catalog
                ),
            }
            profile_revision_id = _mint_id(
                "prof4_",
                {
                    "state_lineage_id": working.state_lineage_id,
                    "chapter_id": working.chapter_id,
                    "payload": revision_payload,
                },
            )
            working._append_unique(
                "profile_revisions",
                "profile_revision_id",
                _row_with_revision(
                    {"profile_revision_id": profile_revision_id, **revision_payload}
                ),
            )
            updated_projection["latest_profile_revision_id"] = profile_revision_id
            _replace_row(
                working, "entities", "entity_id", target, updated_projection
            )
        working.auditor_request_fingerprints.append(request.request_fingerprint)

    if seen_tickets != expected_open_tickets:
        raise RegistryContractError(
            "Auditor requests must exact-cover every open chapter ticket"
        )

    for entity_id in sorted(working.clean_entity_ids):
        if entity_id in working._index("entities", "entity_id"):
            _replace_row(
                working, "entities", "entity_id", entity_id, {"status": "confirmed"}
            )
    for glossary_id in sorted(working.clean_glossary_ids):
        if glossary_id in working._index("glossary_items", "glossary_id"):
            _replace_row(
                working,
                "glossary_items",
                "glossary_id",
                glossary_id,
                {"status": "confirmed"},
            )
    for row in list(working._state["entities"]):
        if row.get("status") == "provisional":
            _replace_row(
                working, "entities", "entity_id", str(row["entity_id"]), {"status": "pending"}
            )
    for row in list(working._state["glossary_items"]):
        if row.get("status") == "provisional":
            _replace_row(
                working,
                "glossary_items",
                "glossary_id",
                str(row["glossary_id"]),
                {"status": "pending"},
            )
    for row in list(working._state["global_aliases"]):
        target = working._index("entities", "entity_id").get(str(row["entity_id"]))
        if target and target.get("status") == "confirmed" and row.get("status") == "pending":
            _replace_row(
                working,
                "global_aliases",
                "alias_id",
                str(row["alias_id"]),
                {"status": "confirmed"},
            )
    for row in list(working._state["block_local_references"]):
        target = working._index("entities", "entity_id").get(str(row["entity_id"]))
        if target and target.get("status") == "confirmed" and row.get("status") == "provisional":
            _replace_row(
                working,
                "block_local_references",
                "local_reference_id",
                str(row["local_reference_id"]),
                {"status": "confirmed"},
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
    working: ChapterWorkingRegistryV4,
    orientation: Mapping[str, Any],
    b0_request: RenderedRegistryRequestV4,
    source_catalog: Mapping[str, str],
    run_config: RunConfigV4,
    audit_decisions: Sequence[Mapping[str, Any]],
) -> PreparedRegistryGenerationV4:
    _validate_run_config_contract(run_config)
    snapshot = working.snapshot()
    gate_hashes = {str(row["gate_record_hash"]) for row in working.alias_gate_records}
    for alias in snapshot["global_aliases"]:
        if alias.get("gate_outcome") != "eligible_global_alias":
            raise RegistryContractError("published alias lacks eligible gate outcome")
        if str(alias.get("gate_record_hash")) not in gate_hashes:
            raise RegistryContractError("published alias lacks registered gate record")
    for table in ("entities", "glossary_items", "block_local_references"):
        if any(row.get("status") == "provisional" for row in snapshot[table]):
            raise RegistryContractError("provisional rows cannot be published")
    if any(row.get("status") == "open" for row in snapshot["tickets"]):
        raise RegistryContractError("open tickets cannot be published")
    entity_ids = {str(row["entity_id"]) for row in snapshot["entities"]}
    if any(str(row.get("entity_id")) not in entity_ids for row in snapshot["global_aliases"]):
        raise RegistryContractError("global alias targets a missing entity")
    if any(
        str(row.get("entity_id")) not in entity_ids
        for row in snapshot["block_local_references"]
    ):
        raise RegistryContractError("block-local reference targets a missing entity")
    published_snapshot = {
        **{key: value for key, value in snapshot.items() if key != "snapshot_hash"},
        "generation_id": None,
    }
    for table in (
        "entities",
        "global_aliases",
        "block_local_references",
        "glossary_items",
        "tickets",
        "profile_revisions",
        "attention_ledger",
    ):
        published_snapshot[table] = [_strip_runtime_only(row) for row in snapshot[table]]
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
        "auditor_request_fingerprints": list(working.auditor_request_fingerprints),
        "orientation_hash": canonical_hash(orientation),
        "alias_gate_records": _clone(working.alias_gate_records),
        "audit_decisions": _clone(list(audit_decisions)),
        "snapshot": published_snapshot,
    }
    commit_payload_hash = canonical_hash(body)
    generation_id = "reggen4_" + canonical_hash(
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
    return PreparedRegistryGenerationV4(
        state_lineage_id=working.state_lineage_id,
        generation_id=generation_id,
        parent_generation_id=working.parent_generation_id,
        chapter_id=working.chapter_id,
        source_manifest_hash=working.source_manifest_hash,
        payload=payload,
    )


class ChapterRegistryStoreV4:
    def __init__(self, root: Path) -> None:
        self.root = Path(root).resolve()

    def _generation_path(self, generation_id: str) -> Path:
        if not re.fullmatch(r"reggen4_[0-9a-f]{20}", generation_id):
            raise RegistryStoreError("unsafe v4 generation id")
        return self.root / "generations" / f"{generation_id}.json"

    def _pointer_path(self, state_lineage_id: str) -> Path:
        return self.root / "current" / (
            canonical_hash({"state_lineage_id": state_lineage_id}) + ".json"
        )

    def load_generation(self, generation_id: str) -> dict[str, Any]:
        path = self._generation_path(generation_id)
        if not path.is_file():
            raise RegistryStoreError(f"missing registry generation: {generation_id}")
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict) or payload.get("schema_version") != REGISTRY_SCHEMA_VERSION:
            raise RegistryStoreError("foreign generation cannot be loaded as v4")
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
        expected = "reggen4_" + canonical_hash(
            {
                "state_lineage_id": payload["state_lineage_id"],
                "parent_generation_id": payload["parent_generation_id"],
                "chapter_id": payload["chapter_id"],
                "commit_payload_hash": own_hash,
            }
        )[:20]
        if payload.get("generation_id") != generation_id or expected != generation_id:
            raise RegistryStoreError("generation identity/path mismatch")
        return payload

    def current_generation_id(self, state_lineage_id: str) -> str | None:
        path = self._pointer_path(state_lineage_id)
        if not path.is_file():
            return None
        pointer = json.loads(path.read_text(encoding="utf-8"))
        if pointer.get("schema_version") != REGISTRY_SCHEMA_VERSION:
            raise RegistryStoreError("foreign pointer cannot be read as v4")
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
            return empty_registry_snapshot_v4(state_lineage_id)
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
        generation: PreparedRegistryGenerationV4,
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
        raise RegistryContractError("B2 preview requires a v4 registry snapshot")
    if candidate_count_cap <= 0:
        raise RegistryContractError("B2 candidate cap must be positive")
    active = [_block_view(row) for row in active_blocks]
    active_ids = {str(row["block_id"]) for row in active}
    entities = {
        str(row["entity_id"]): row
        for row in registry_snapshot.get("entities") or []
        if row.get("status") in {"confirmed", "pending"}
    }
    aliases_by_entity: dict[str, list[tuple[str, str | None]]] = defaultdict(list)
    for alias in registry_snapshot.get("global_aliases") or []:
        if alias.get("status") == "confirmed" and alias.get("gate_outcome") == "eligible_global_alias":
            aliases_by_entity[str(alias["entity_id"])].append(
                (str(alias["surface"]), alias.get("name_class"))
            )
    links: list[dict[str, Any]] = []
    for entity_id, entity in entities.items():
        stable_surfaces = list(aliases_by_entity[entity_id])
        if entity.get("name_class") is not None:
            stable_surfaces.append(
                (str(entity["canonical_surface"]), str(entity["name_class"]))
            )
        for block in active:
            block_id = str(block["block_id"])
            for surface, name_class in stable_surfaces:
                match_rows = _match_surface(block["text"], surface)
                base = _title_base(surface, name_class)
                if base:
                    match_rows += [
                        ("title_base", matched)
                        for _, matched in _match_surface(block["text"], base)
                    ]
                for match_kind, matched in match_rows:
                    links.append(
                        {
                            "entity_id": entity_id,
                            "block_id": block_id,
                            "candidate_source": "surface_match",
                            "matched_surfaces": [matched],
                            "match_kinds": [match_kind],
                            "support_block_ids": [],
                            "source_ticket_ids": [],
                            "authoritative": False,
                        }
                    )
            if block_id in set(entity.get("support_block_ids") or []):
                links.append(
                    {
                        "entity_id": entity_id,
                        "block_id": block_id,
                        "candidate_source": "support_block",
                        "matched_surfaces": [],
                        "match_kinds": [],
                        "support_block_ids": [block_id],
                        "source_ticket_ids": [],
                        "authoritative": False,
                    }
                )
    for local in registry_snapshot.get("block_local_references") or []:
        entity_id = str(local.get("entity_id"))
        if entity_id not in entities or local.get("status") not in {"confirmed", "pending"}:
            continue
        for block_id in set(local.get("valid_block_ids") or []) & active_ids:
            links.append(
                {
                    "entity_id": entity_id,
                    "block_id": block_id,
                    "candidate_source": "block_local_reference",
                    "matched_surfaces": [str(local["surface"])],
                    "match_kinds": [],
                    "support_block_ids": [],
                    "source_ticket_ids": [],
                    "authoritative": False,
                }
            )
    for ticket in registry_snapshot.get("tickets") or []:
        if ticket.get("resolution_action") != "defer_to_b2":
            continue
        candidate_ids = set(ticket.get("candidate_entity_ids") or []) | set(
            ticket.get("subject_entity_ids") or []
        )
        for block_id in set(ticket.get("source_block_ids") or []) & active_ids:
            for entity_id in sorted(candidate_ids):
                if entity_id in entities:
                    links.append(
                        {
                            "entity_id": entity_id,
                            "block_id": block_id,
                            "candidate_source": "deferred_ticket",
                            "matched_surfaces": [],
                            "match_kinds": [],
                            "support_block_ids": [],
                            "source_ticket_ids": [str(ticket["ticket_id"])],
                            "authoritative": False,
                        }
                    )
    dedup = {canonical_hash(row): row for row in links}
    ordered_links = sorted(
        dedup.values(),
        key=lambda row: (row["block_id"], row["entity_id"], row["candidate_source"]),
    )
    candidate_ids = sorted({str(row["entity_id"]) for row in ordered_links})
    selected_ids = candidate_ids[:candidate_count_cap]
    selected_set = set(selected_ids)
    body = {
        "policy_version": B2_RESCAN_POLICY_VERSION,
        "chapter_id": chapter_id,
        "active_block_ids": sorted(active_ids),
        "registry_generation_id": registry_snapshot.get("generation_id"),
        "candidate_entity_ids": selected_ids,
        "candidate_cards": [
            {
                "entity_id": entity_id,
                "canonical_surface": str(entities[entity_id]["canonical_surface"]),
                "referent_kind": str(entities[entity_id]["referent_kind"]),
                "identity_summary": str(entities[entity_id]["identity_summary"]),
                "referential_gender": entities[entity_id].get("referential_gender"),
                "status": str(entities[entity_id]["status"]),
            }
            for entity_id in selected_ids
        ],
        "candidate_links": [
            row for row in ordered_links if row["entity_id"] in selected_set
        ],
        "pre_cap_count": len(candidate_ids),
        "selected_count": len(selected_ids),
        "excluded_entity_ids": candidate_ids[candidate_count_cap:],
        "overflow": len(candidate_ids) > candidate_count_cap,
        "authoritative_bindings": [],
    }
    body["manifest_hash"] = canonical_hash(body)
    return body


@dataclass
class SyntheticRegistryExecutorV4:
    responses: dict[str, list[Mapping[str, Any]]] = field(default_factory=dict)
    calls: list[dict[str, Any]] = field(default_factory=list)

    def add(self, role: str, response: Mapping[str, Any]) -> None:
        if role not in PROMPT_IDS:
            raise RegistryContractError(f"unknown synthetic role: {role}")
        self.responses.setdefault(role, []).append(_clone(response))

    def execute(self, request: RenderedRegistryRequestV4) -> dict[str, Any]:
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


__all__ = [
    "ChapterRegistryStoreV4",
    "ChapterWorkingRegistryV4",
    "SyntheticRegistryExecutorV4",
    "apply_audit_responses",
    "build_attention_packets",
    "build_b2_candidate_manifest",
    "build_exception_components",
    "build_registry_generation",
    "build_registry_windows",
    "chapter_source_manifest_hash",
    "empty_registry_snapshot_v4",
    "estimate_registry_prompt_tokens",
    "render_auditor_requests",
    "render_b0_request",
    "render_b1_request",
    "route_alias_for_commit",
    "select_candidate_packets",
    "validate_audit_response",
    "validate_orientation_response",
]
