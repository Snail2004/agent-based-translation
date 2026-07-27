"""Windowed literary Translator contract over sealed B4 context."""

from __future__ import annotations

from collections import Counter
from copy import deepcopy
from dataclasses import dataclass
import hashlib
import json
import re
from typing import Any, Mapping, Sequence

from jsonschema import Draft202012Validator

from pipeline.literary.b4_address_anchor_v1 import (
    B4AddressAnchorError,
    verify_address_anchor_artifact_v1,
)
from pipeline.literary.b4_story_bible_assembler_v1 import (
    WINDOW_SCHEMA_VERSION,
)
from pipeline.literary.b4_translator_pack_v1 import (
    B4TranslatorPackError,
    translator_pack_prompt_view_v1,
    verify_translator_pack_v1,
)
from pipeline.literary.checkpoint import canonical_hash, canonical_json


ROLE_ID = "literary.b4.translator"
PROMPT_ID = "literary_b4_translator_v8"
RESPONSE_SCHEMA_VERSION = "literary_b4_translation_window_response_v6"
WINDOW_ARTIFACT_SCHEMA_VERSION = "literary_b4_translation_window_artifact_v7"
LEGACY_WINDOW_ARTIFACT_SCHEMA_VERSIONS = {
    "literary_b4_translation_window_artifact_v6",
}
CHAPTER_ARTIFACT_SCHEMA_VERSION = "literary_b4_translation_chapter_v7"
WINDOW_PROMPT_VIEW_SCHEMA_VERSION = "literary_b4_translator_window_prompt_v1"
TRANSLATION_REQUEST_PACK_SCHEMA_VERSION = "literary_b4_translation_request_pack_v1"

SYSTEM_PROMPT = """You are the Vietnamese literary Translator for one bounded window.
Prompt version: literary_b4_translator_v8.

Read the single TRANSLATION_REQUEST_PACK. It contains the style profile,
bounded Story Bible context, Address Anchor, narrative position, current
window, accepted preceding tail, and active source blocks. Use those materials
to translate every active source block into Vietnamese exactly once. The
Address Anchor is guidance for the prose; do not report address-analysis
metadata back to the caller. For an explicitly unanchored pair, choose the form
that best preserves the source without modifying any semantic record.

Treat the active source text as authoritative for what is literally said or
done: polarity and modal force, explicitly expressed participants, action and
object, concrete detail and quantity, uncertainty, irony, repetition, and
emotional valence. Treat the pack as authoritative for resolved identity,
as-of chronology, narrative frame, approved address forms, stable names, and
approved terminology. Context may resolve an ambiguity supported by the
source, but must not override explicit source wording or invent a fact. If
context and explicit source wording appear to conflict, preserve the source
meaning and ambiguity instead of forcing the context into the translation.

Preceding tail blocks and their accepted translations are context only and
must not be emitted. Preserve active block order and copy each active block_id
exactly. Return only schema_version and blocks. Each block contains only
block_id and target_text. Do not report scores, explanations, source text,
turn ids, address forms, deviations, or notes. Output JSON only and follow the
supplied response shape exactly.
"""


class B4TranslatorError(RuntimeError):
    pass


@dataclass(frozen=True)
class RenderedTranslationWindowRequestV1:
    request_fingerprint: str
    stable_prefix_sha256: str
    stable_prefix_messages: tuple[dict[str, str], ...]
    messages: tuple[dict[str, str], ...]
    response_schema: dict[str, Any]
    model_input_pack: dict[str, Any]
    translator_pack: dict[str, Any]
    address_anchor: dict[str, Any]
    window_slice: dict[str, Any]
    chapter: dict[str, Any]
    style_profile_version: str
    measured_arm: bool


def translation_window_response_schema_v1() -> dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["schema_version", "blocks"],
        "properties": {
            "schema_version": {
                "type": "string",
                "const": RESPONSE_SCHEMA_VERSION,
            },
            "blocks": {
                "type": "array",
                "maxItems": 256,
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["block_id", "target_text"],
                    "properties": {
                        "block_id": {
                            "type": "string",
                            "minLength": 1,
                            "maxLength": 120,
                        },
                        "target_text": {"type": "string", "minLength": 1},
                    },
                },
            },
        },
    }


def render_translation_window_request_v1(
    *,
    style_profile: str,
    style_profile_version: str,
    measured_arm: bool,
    translator_pack_bytes: bytes,
    address_anchor_bytes: bytes,
    window_slice_bytes: bytes,
    chapter: Mapping[str, Any],
    accepted_tail_translations: Mapping[str, str],
    allow_planning_only: bool = False,
) -> RenderedTranslationWindowRequestV1:
    profile = _text(style_profile, "style_profile")
    version = _text(style_profile_version, "style_profile_version")
    if not isinstance(measured_arm, bool):
        raise B4TranslatorError("measured_arm must be boolean")
    try:
        translator_pack = verify_translator_pack_v1(
            _json_bytes(translator_pack_bytes, "Translator Pack")
        )
    except B4TranslatorPackError as exc:
        raise B4TranslatorError(str(exc)) from exc
    if not isinstance(allow_planning_only, bool):
        raise B4TranslatorError("allow_planning_only must be boolean")
    if translator_pack.get("planning_only") is True and not allow_planning_only:
        raise B4TranslatorError(
            "planning-only Translator Pack cannot be used for a live request"
        )
    try:
        anchor = verify_address_anchor_artifact_v1(
            _json_bytes(address_anchor_bytes, "Address Anchor")
        )
    except B4AddressAnchorError as exc:
        raise B4TranslatorError(str(exc)) from exc
    window = _sealed_json_bytes(
        window_slice_bytes,
        hash_field="artifact_hash",
        label="window slice",
    )
    if window.get("schema_version") != WINDOW_SCHEMA_VERSION:
        raise B4TranslatorError("unsupported B4 window slice schema")
    chapter_row = deepcopy(dict(chapter))
    chapter_id = _text(chapter_row.get("chapter_id"), "chapter_id")
    if not (
        translator_pack.get("chapter_id")
        == anchor.get("chapter_id")
        == window.get("chapter_id")
        == chapter_id
    ):
        raise B4TranslatorError("Translator inputs belong to different chapters")
    if (
        anchor.get("style_profile_version") != version
        or anchor.get("measured_arm") is not measured_arm
    ):
        raise B4TranslatorError("Translator style profile differs from anchor")
    if (
        anchor.get("story_bible_artifact_hash")
        != translator_pack.get("story_bible_artifact_hash")
    ):
        raise B4TranslatorError("Address Anchor belongs to another Story Bible")
    if (
        translator_pack.get("address_anchor_artifact_hash")
        != anchor.get("artifact_hash")
    ):
        raise B4TranslatorError("Translator Pack belongs to another Address Anchor")
    _validate_window_anchor_pair_scope_v1(
        window=window,
        anchor=anchor,
    )
    anchor_prompt_view = _address_anchor_prompt_view_v1(anchor)
    window_prompt_view = translator_window_prompt_view_v1(
        window=window,
        anchor=anchor,
    )

    blocks = _chapter_blocks(chapter_row)
    block_by_id = {row["block_id"]: row for row in blocks}
    active_ids = _string_list(window.get("active_block_ids"), "active_block_ids")
    tail_ids = _string_list(
        window.get("preceding_tail_block_ids") or [],
        "preceding_tail_block_ids",
    )
    if set(active_ids).intersection(tail_ids):
        raise B4TranslatorError("window active and tail blocks overlap")
    if any(block_id not in block_by_id for block_id in [*active_ids, *tail_ids]):
        raise B4TranslatorError("window cites a source block absent from document")
    tail_map = {
        _text(key, "tail block_id"): _text(value, "tail target_text")
        for key, value in accepted_tail_translations.items()
    }
    if set(tail_map) != set(tail_ids):
        raise B4TranslatorError(
            "accepted tail translations must exact-cover preceding tail blocks"
        )

    stable_context = {
        "style_profile": profile,
        "style_profile_version": version,
        "measured_arm": measured_arm,
        "translator_context": translator_pack_prompt_view_v1(translator_pack),
        "address_anchor": anchor_prompt_view,
        "narrative_position": deepcopy(
            translator_pack.get("narrative_position")
        ),
    }
    window_context = {
        "window_slice": window_prompt_view,
        "preceding_tail": [
            {
                "block_id": block_id,
                "source_text": block_by_id[block_id]["text"],
                "accepted_target_text": tail_map[block_id],
            }
            for block_id in tail_ids
        ],
        "active_source_blocks": [
            {
                "block_id": block_id,
                "source_text": block_by_id[block_id]["text"],
            }
            for block_id in active_ids
        ],
    }
    model_input_body = {
        "schema_version": TRANSLATION_REQUEST_PACK_SCHEMA_VERSION,
        "chapter_id": chapter_id,
        "window_id": window["window_id"],
        "window_order": window["window_order"],
        "stable_context": stable_context,
        "window_context": window_context,
    }
    model_input_pack = {
        **model_input_body,
        "pack_hash": canonical_hash(model_input_body),
    }
    system_message = {"role": "system", "content": SYSTEM_PROMPT}
    messages = (
        system_message,
        {
            "role": "user",
            "content": "[TRANSLATION_REQUEST_PACK]\n"
            + canonical_json(model_input_pack),
        },
    )
    schema = translation_window_response_schema_v1()
    # This hash binds chapter-stable context even though the provider receives
    # one complete request pack per window.
    stable_prefix_sha256 = hashlib.sha256(
        canonical_json(stable_context).encode("utf-8")
    ).hexdigest()
    stable_context_messages = (
        system_message,
        {"role": "user", "content": canonical_json(stable_context)},
    )
    request_body = {
        "prompt_id": PROMPT_ID,
        "prompt_sha256": hashlib.sha256(
            SYSTEM_PROMPT.encode("utf-8")
        ).hexdigest(),
        "style_profile_version": version,
        "style_profile_sha256": hashlib.sha256(profile.encode("utf-8")).hexdigest(),
        "stable_prefix_sha256": stable_prefix_sha256,
        "story_bible_artifact_hash": translator_pack[
            "story_bible_artifact_hash"
        ],
        "translator_pack_artifact_hash": translator_pack["artifact_hash"],
        "address_anchor_artifact_hash": anchor["artifact_hash"],
        "window_slice_artifact_hash": window["artifact_hash"],
        "model_input_pack_hash": model_input_pack["pack_hash"],
        "response_schema_hash": canonical_hash(schema),
        "messages": list(messages),
    }
    return RenderedTranslationWindowRequestV1(
        request_fingerprint=canonical_hash(request_body),
        stable_prefix_sha256=stable_prefix_sha256,
        stable_prefix_messages=stable_context_messages,
        messages=messages,
        response_schema=schema,
        model_input_pack=model_input_pack,
        translator_pack=translator_pack,
        address_anchor=anchor,
        window_slice=window,
        chapter=chapter_row,
        style_profile_version=version,
        measured_arm=measured_arm,
    )


def validate_translation_window_response_v1(
    *,
    rendered: RenderedTranslationWindowRequestV1,
    response: Mapping[str, Any],
) -> dict[str, Any]:
    raw = deepcopy(dict(response))
    errors = sorted(
        Draft202012Validator(rendered.response_schema).iter_errors(raw),
        key=lambda row: list(row.path),
    )
    if errors:
        raise B4TranslatorError(
            f"Translator response schema failure: {errors[0].message}"
        )

    active_ids = _string_list(
        rendered.window_slice.get("active_block_ids"), "active_block_ids"
    )
    returned_ids = [
        _text(row.get("block_id"), "translated block_id")
        for row in raw["blocks"]
    ]
    if returned_ids != active_ids:
        raise B4TranslatorError(
            "Translator blocks must exact-cover active blocks once in order"
        )

    block_by_id = {
        row["block_id"]: row for row in _chapter_blocks(rendered.chapter)
    }
    turns_by_block: dict[str, set[str]] = {}
    seen_turn_ids: set[str] = set()
    for turn in rendered.window_slice.get("speaker_turns") or []:
        if not isinstance(turn, Mapping):
            raise B4TranslatorError("window speaker turn is malformed")
        if turn.get("window_membership") != "active":
            continue
        turn_id = _text(turn.get("speaker_turn_id"), "speaker_turn_id")
        block_id = _text(turn.get("block_id"), "speaker turn block_id")
        if turn_id in seen_turn_ids:
            raise B4TranslatorError("window repeats an active speaker turn")
        if block_id not in active_ids:
            raise B4TranslatorError("active speaker turn cites a foreign block")
        seen_turn_ids.add(turn_id)
        turns_by_block.setdefault(block_id, set()).add(turn_id)

    normalized_rows: list[dict[str, Any]] = []
    for block_id, row in zip(active_ids, raw["blocks"], strict=True):
        target_text = row["target_text"]
        if not isinstance(target_text, str) or not target_text.strip():
            raise B4TranslatorError("Translator target_text is empty")
        normalized_rows.append(
            {
                "block_id": block_id,
                "source_text": block_by_id[block_id]["text"],
                "target_text": target_text,
                "turn_refs": sorted(turns_by_block.get(block_id, set())),
            }
        )

    body = {
        "schema_version": "literary_b4_validated_translation_window_v7",
        "chapter_id": rendered.window_slice["chapter_id"],
        "window_id": rendered.window_slice["window_id"],
        "style_profile_version": rendered.style_profile_version,
        "measured_arm": rendered.measured_arm,
        "story_bible_artifact_hash": rendered.translator_pack[
            "story_bible_artifact_hash"
        ],
        "address_anchor_artifact_hash": rendered.address_anchor["artifact_hash"],
        "window_order": rendered.window_slice["window_order"],
        "window_slice_artifact_hash": rendered.window_slice["artifact_hash"],
        "translator_pack_artifact_hash": rendered.translator_pack[
            "artifact_hash"
        ],
        "request_fingerprint": rendered.request_fingerprint,
        "stable_prefix_sha256": rendered.stable_prefix_sha256,
        "model_input_pack_hash": rendered.model_input_pack["pack_hash"],
        "blocks": normalized_rows,
        "translator_output_contract": "translation_only_v1",
        "address_metadata_collected": False,
        "provider_calls": 0,
    }
    return {**body, "artifact_hash": canonical_hash(body)}


def _validate_translation_window_response_legacy_v1(
    *,
    rendered: RenderedTranslationWindowRequestV1,
    response: Mapping[str, Any],
) -> dict[str, Any]:
    raw = deepcopy(dict(response))
    errors = sorted(
        Draft202012Validator(rendered.response_schema).iter_errors(raw),
        key=lambda row: list(row.path),
    )
    if errors:
        raise B4TranslatorError(
            f"Translator response schema failure: {errors[0].message}"
        )
    expected_echoes = {
        "chapter_id": rendered.window_slice["chapter_id"],
        "window_id": rendered.window_slice["window_id"],
        "style_profile_version": rendered.style_profile_version,
        "measured_arm": rendered.measured_arm,
        "story_bible_artifact_hash": rendered.translator_pack[
            "story_bible_artifact_hash"
        ],
        "address_anchor_artifact_hash": rendered.address_anchor["artifact_hash"],
    }
    for field, expected in expected_echoes.items():
        if raw.get(field) != expected:
            raise B4TranslatorError(f"Translator {field} mismatch")

    block_by_id = {
        row["block_id"]: row for row in _chapter_blocks(rendered.chapter)
    }
    active_ids = _string_list(
        rendered.window_slice.get("active_block_ids"), "active_block_ids"
    )
    tail_ids = set(
        _string_list(
            rendered.window_slice.get("preceding_tail_block_ids") or [],
            "preceding_tail_block_ids",
        )
    )
    rows = raw["blocks"]
    returned_ids = [_text(row.get("block_id"), "translated block_id") for row in rows]
    if len(returned_ids) != len(set(returned_ids)) or set(returned_ids) != set(
        active_ids
    ):
        raise B4TranslatorError(
            "Translator blocks must exact-cover active blocks once"
        )
    if set(returned_ids).intersection(tail_ids):
        raise B4TranslatorError("Translator emitted a tail block")

    turns = {
        str(row["speaker_turn_id"]): row
        for row in rendered.window_slice.get("speaker_turns") or []
        if isinstance(row, Mapping) and row.get("window_membership") == "active"
    }
    turns_by_block: dict[str, set[str]] = {}
    for turn_id, turn in turns.items():
        turns_by_block.setdefault(str(turn["block_id"]), set()).add(turn_id)
    pair_by_turn: dict[str, Mapping[str, Any]] = {}
    pair_by_id: dict[str, Mapping[str, Any]] = {}
    for pair in rendered.window_slice.get("address_pairs") or []:
        if not isinstance(pair, Mapping):
            raise B4TranslatorError("window address pair is malformed")
        pair_id = pair.get("pair_id")
        if pair_id is not None:
            pair_id = _text(pair_id, "window pair_id")
            if pair_id in pair_by_id:
                raise B4TranslatorError("window repeats an address pair")
            pair_by_id[pair_id] = pair
        for turn_id in pair.get("turn_ids") or []:
            if turn_id in pair_by_turn and pair_by_turn[turn_id] != pair:
                raise B4TranslatorError("turn belongs to multiple address pairs")
            pair_by_turn[str(turn_id)] = pair
    anchor_by_pair = {
        str(row["pair_id"]): row
        for row in rendered.address_anchor.get("pair_decisions") or []
        if isinstance(row, Mapping)
    }

    normalized_rows = []
    all_form_turns: set[str] = set()
    all_deviation_turns: set[str] = set()
    pronoun_realization_counts = {"overt": 0, "dropped": 0}
    vocative_used_counts: dict[str | None, int] = {}
    validation_observations: list[dict[str, Any]] = []
    quarantined_address_metadata: list[dict[str, Any]] = []
    for source in rows:
        normalized_source = deepcopy(source)
        normalized_source["address_forms_used"] = []
        normalized_source["anchor_deviations"] = []
        normalized_source["address_metadata_unverified_turn_ids"] = []
        block_id = str(source["block_id"])
        if source["source_text"] != block_by_id[block_id]["text"]:
            raise B4TranslatorError("Translator source_text is not verbatim")
        expected_turns = turns_by_block.get(block_id, set())
        if set(source["turn_refs"]) != expected_turns:
            raise B4TranslatorError("Translator turn_refs differ from B2")
        form_rows = source["address_forms_used"]
        deviation_rows = source["anchor_deviations"]
        form_by_turn: dict[str, Mapping[str, Any]] = {}
        deviation_by_turn: dict[str, Mapping[str, Any]] = {}
        for form_row in form_rows:
            turn_id = str(form_row["turn_id"])
            pair_id = form_row["pair_id"]
            if (
                turn_id not in expected_turns
                or turn_id in form_by_turn
                or turn_id in all_form_turns
                or turn_id not in pair_by_turn
                or pair_by_turn.get(turn_id, {}).get("pair_id") != pair_id
            ):
                raise B4TranslatorError(
                    "Translator address form cites a foreign or repeated turn"
                )
            form_by_turn[turn_id] = form_row
            all_form_turns.add(turn_id)
        for deviation in deviation_rows:
            turn_id = str(deviation["turn_id"])
            pair_id = str(deviation["pair_id"])
            if (
                turn_id not in expected_turns
                or turn_id in deviation_by_turn
                or turn_id in all_deviation_turns
                or turn_id not in pair_by_turn
                or pair_by_turn.get(turn_id, {}).get("pair_id") != pair_id
            ):
                raise B4TranslatorError(
                    "Translator deviation cites a foreign or repeated turn"
                )
            deviation_by_turn[turn_id] = deviation
            all_deviation_turns.add(turn_id)

        for turn_id in sorted(expected_turns):
            pair = pair_by_turn.get(turn_id)
            if pair is None:
                continue
            pair_id = pair.get("pair_id")
            anchor = (
                anchor_by_pair.get(str(pair_id))
                if isinstance(pair_id, str)
                else None
            )
            turn = turns[turn_id]
            used = form_by_turn.get(turn_id)
            deviation = deviation_by_turn.get(turn_id)
            quarantine_reasons: list[str] = []
            if anchor is None or anchor.get("not_anchored") is not None:
                if used is not None and used["from_anchor"] is not False:
                    quarantine_reasons.append(
                        "unanchored_turn_claims_anchored_form"
                    )
                if deviation is not None:
                    quarantine_reasons.append(
                        "unanchored_turn_declares_anchor_deviation"
                    )
            elif deviation is not None and used is None:
                quarantine_reasons.append(
                    "declared_deviation_without_address_form"
                )
            elif deviation is not None and used is not None:
                expected_pair = _expected_pronoun_pair(anchor=anchor, turn=turn)
                if (
                    deviation["anchored_pronoun_pair"] != expected_pair
                    or deviation["used_pronoun_pair"] != used["pronoun_pair"]
                    or deviation["vocative"] != used["vocative_used"]
                ):
                    quarantine_reasons.append(
                        "declared_deviation_misstates_address_metadata"
                    )

            if quarantine_reasons:
                normalized_source[
                    "address_metadata_unverified_turn_ids"
                ].append(turn_id)
                quarantine = {
                    "block_id": block_id,
                    "turn_id": turn_id,
                    "pair_id": pair_id,
                    "reason_codes": quarantine_reasons,
                    "raw_address_form": (
                        deepcopy(used) if used is not None else None
                    ),
                    "raw_anchor_deviation": (
                        deepcopy(deviation) if deviation is not None else None
                    ),
                }
                quarantined_address_metadata.append(quarantine)
                validation_observations.append(
                    {
                        "observation_kind": "address_metadata_unverified",
                        "block_id": block_id,
                        "turn_id": turn_id,
                        "pair_id": pair_id,
                        "reason_codes": quarantine_reasons,
                    }
                )
                continue

            if used is not None:
                normalized_source["address_forms_used"].append(deepcopy(used))
                pronoun_realization = str(used["pronoun_realization"])
                pronoun_realization_counts[pronoun_realization] += 1
                vocative_used = used["vocative_used"]
                vocative_key = (
                    str(vocative_used) if vocative_used is not None else None
                )
                vocative_used_counts[vocative_key] = (
                    vocative_used_counts.get(vocative_key, 0) + 1
                )
                if pronoun_realization == "dropped":
                    validation_observations.append(
                        {
                            "observation_kind": "addressee_pronoun_absent",
                            "block_id": block_id,
                            "turn_id": turn_id,
                            "pair_id": pair_id,
                        }
                    )
                    if vocative_used is None:
                        validation_observations.append(
                            {
                                "observation_kind": "address_marker_absent",
                                "block_id": block_id,
                                "turn_id": turn_id,
                                "pair_id": pair_id,
                            }
                        )
            if deviation is not None:
                normalized_source["anchor_deviations"].append(
                    {
                        **deepcopy(deviation),
                        "rule": "model_declared_anchor_deviation",
                    }
                )

            if anchor is None or anchor.get("not_anchored") is not None:
                continue
            if used is None:
                normalized_source["anchor_deviations"].append(
                    _observed_anchor_deviation_v1(
                        turn_id=turn_id,
                        pair_id=str(pair_id),
                        rule="anchored_turn_omits_address_form",
                        anchored_pronoun_pair=_expected_pronoun_pair(
                            anchor=anchor,
                            turn=turn,
                        ),
                        used_pronoun_pair=None,
                        vocative=None,
                        reason="anchored turn omits its address form",
                    )
                )
                continue
            expected_pair = _expected_pronoun_pair(anchor=anchor, turn=turn)
            used_pair = used["pronoun_pair"]
            matches_anchor = used_pair == expected_pair
            if matches_anchor and used["from_anchor"] is not True:
                normalized_source["anchor_deviations"].append(
                    _observed_anchor_deviation_v1(
                        turn_id=turn_id,
                        pair_id=str(pair_id),
                        rule="anchored_form_not_marked_from_anchor",
                        anchored_pronoun_pair=expected_pair,
                        used_pronoun_pair=used_pair,
                        vocative=used["vocative_used"],
                        reason="anchored pronoun pair is not marked from_anchor",
                    )
                )
            if not matches_anchor and deviation is None:
                normalized_source["anchor_deviations"].append(
                    _observed_anchor_deviation_v1(
                        turn_id=turn_id,
                        pair_id=str(pair_id),
                        rule="pronoun_pair_differs_without_declared_deviation",
                        anchored_pronoun_pair=expected_pair,
                        used_pronoun_pair=used_pair,
                        vocative=used["vocative_used"],
                        reason=(
                            "anchored turn differs from its pronoun pair "
                            "without deviation"
                        ),
                    )
                )
            if not matches_anchor and used["from_anchor"] is not False:
                normalized_source["anchor_deviations"].append(
                    _observed_anchor_deviation_v1(
                        turn_id=turn_id,
                        pair_id=str(pair_id),
                        rule="departed_pronoun_pair_claims_anchor",
                        anchored_pronoun_pair=expected_pair,
                        used_pronoun_pair=used_pair,
                        vocative=used["vocative_used"],
                        reason=(
                            "departed pronoun pair claims it came from the anchor"
                        ),
                    )
                )
            vocative = used["vocative_used"]
            allowed_vocatives = {
                str(option["form"]).casefold()
                for option in anchor.get("vocative_options") or []
                if isinstance(option, Mapping) and option.get("form")
            }
            pronoun_realization = used["pronoun_realization"]
            if pronoun_realization == "overt":
                search_text = _without_other_pair_address_forms(
                    text=source["target_text"],
                    current_pair_id=str(pair_id),
                    anchor_by_pair=anchor_by_pair,
                )
                if expected_pair["addressee"].casefold() not in search_text.casefold():
                    normalized_source["anchor_deviations"].append(
                        _observed_anchor_deviation_v1(
                            turn_id=turn_id,
                            pair_id=str(pair_id),
                            rule="declared_addressee_pronoun_absent_from_target",
                            anchored_pronoun_pair=expected_pair,
                            used_pronoun_pair=used_pair,
                            vocative=vocative,
                            reason=(
                                "target_text omits the declared addressee pronoun"
                            ),
                        )
                    )
            if (
                isinstance(vocative, str)
                and vocative
                and vocative.casefold() not in source["target_text"].casefold()
            ):
                normalized_source["anchor_deviations"].append(
                    _observed_anchor_deviation_v1(
                        turn_id=turn_id,
                        pair_id=str(pair_id),
                        rule="declared_addressee_vocative_absent_from_target",
                        anchored_pronoun_pair=expected_pair,
                        used_pronoun_pair=used_pair,
                        vocative=vocative,
                        reason=(
                            "target_text omits the declared addressee vocative"
                        ),
                    )
                )
            if (
                vocative is not None
                and str(vocative).casefold() not in allowed_vocatives
                and deviation is None
            ):
                synthesized = {
                    "turn_id": turn_id,
                    "pair_id": pair_id,
                    "rule": "vocative_outside_anchor_options",
                    "anchored_pronoun_pair": deepcopy(expected_pair),
                    "used_pronoun_pair": deepcopy(used_pair),
                    "vocative": vocative,
                    "reason": "vocative_outside_anchor_options",
                }
                normalized_source["anchor_deviations"].append(synthesized)
                deviation_by_turn[turn_id] = synthesized
                all_deviation_turns.add(turn_id)
        normalized_rows.append(normalized_source)

    deviation_counts = Counter(
        str(deviation["rule"])
        for row in normalized_rows
        for deviation in row["anchor_deviations"]
    )
    body = {
        "schema_version": "literary_b4_validated_translation_window_v6",
        **expected_echoes,
        "window_order": rendered.window_slice["window_order"],
        "window_slice_artifact_hash": rendered.window_slice["artifact_hash"],
        "translator_pack_artifact_hash": rendered.translator_pack[
            "artifact_hash"
        ],
        "request_fingerprint": rendered.request_fingerprint,
        "stable_prefix_sha256": rendered.stable_prefix_sha256,
        "blocks": sorted(
            normalized_rows, key=lambda row: active_ids.index(row["block_id"])
        ),
        "pronoun_realization_counts": pronoun_realization_counts,
        "vocative_used_counts": _vocative_counts_rows(vocative_used_counts),
        "addressee_pronoun_absent_count": sum(
            row["observation_kind"] == "addressee_pronoun_absent"
            for row in validation_observations
        ),
        "address_marker_absent_count": sum(
            row["observation_kind"] == "address_marker_absent"
            for row in validation_observations
        ),
        "validation_observations": validation_observations,
        "address_metadata_unverified_count": len(
            quarantined_address_metadata
        ),
        "address_metadata_unverified_turn_ids": [
            row["turn_id"] for row in quarantined_address_metadata
        ],
        "quarantined_address_metadata": quarantined_address_metadata,
        "anchor_deviation_count": sum(deviation_counts.values()),
        "anchor_deviation_by_rule": dict(sorted(deviation_counts.items())),
        "provider_calls": 0,
    }
    return {**body, "artifact_hash": canonical_hash(body)}


def build_translation_window_artifact_v1(
    *,
    validated_response: Mapping[str, Any],
    provider_receipt: Mapping[str, Any] | None,
    provider_called: bool,
) -> dict[str, Any]:
    validated = _verify_hashed(
        validated_response, "artifact_hash", "validated Translator response"
    )
    if provider_called != (provider_receipt is not None):
        raise B4TranslatorError("Translator provider receipt differs")
    body = {
        **{
            key: deepcopy(value)
            for key, value in validated.items()
            if key not in {"schema_version", "artifact_hash", "provider_calls"}
        },
        "schema_version": WINDOW_ARTIFACT_SCHEMA_VERSION,
        "provider_called": provider_called,
        "provider_receipt": (
            deepcopy(dict(provider_receipt)) if provider_receipt is not None else None
        ),
        "translation_performed": True,
        "semantic_record_mutation_performed": False,
    }
    return {**body, "artifact_hash": canonical_hash(body)}


def assemble_translation_chapter_v1(
    *,
    translator_pack: Mapping[str, Any],
    address_anchor: Mapping[str, Any],
    window_plan: Mapping[str, Any],
    window_artifacts: Sequence[Mapping[str, Any]],
    chapter: Mapping[str, Any],
) -> dict[str, Any]:
    try:
        pack = verify_translator_pack_v1(translator_pack)
    except B4TranslatorPackError as exc:
        raise B4TranslatorError(str(exc)) from exc
    if pack.get("planning_only") is True:
        raise B4TranslatorError(
            "planning-only Translator Pack cannot assemble a translation"
        )
    try:
        anchor = verify_address_anchor_artifact_v1(address_anchor)
    except B4AddressAnchorError as exc:
        raise B4TranslatorError(str(exc)) from exc
    chapter_row = deepcopy(dict(chapter))
    chapter_id = _text(chapter_row.get("chapter_id"), "chapter_id")
    if pack.get("chapter_id") != chapter_id or anchor.get("chapter_id") != chapter_id:
        raise B4TranslatorError("chapter assembly lineage differs")
    if (
        pack.get("story_bible_artifact_hash")
        != anchor.get("story_bible_artifact_hash")
        or pack.get("address_anchor_artifact_hash") != anchor.get("artifact_hash")
    ):
        raise B4TranslatorError("chapter assembly pack lineage differs")
    windows = window_plan.get("windows")
    if not isinstance(windows, list):
        raise B4TranslatorError("window plan is malformed")
    artifacts = [
        _verify_translation_window_artifact(row) for row in window_artifacts
    ]
    by_window = {str(row["window_id"]): row for row in artifacts}
    expected_window_ids = [str(row["window_id"]) for row in windows]
    if len(by_window) != len(artifacts) or set(by_window) != set(expected_window_ids):
        raise B4TranslatorError("translation windows do not exact-cover plan")
    prefix_hashes = {row["stable_prefix_sha256"] for row in artifacts}
    if len(prefix_hashes) != 1:
        raise B4TranslatorError("translation windows have different stable prefixes")
    if any(
        row["story_bible_artifact_hash"]
        != pack["story_bible_artifact_hash"]
        or row["translator_pack_artifact_hash"] != pack["artifact_hash"]
        or row["address_anchor_artifact_hash"] != anchor["artifact_hash"]
        or row["style_profile_version"] != anchor["style_profile_version"]
        or row["measured_arm"] is not anchor["measured_arm"]
        for row in artifacts
    ):
        raise B4TranslatorError("translation window lineage differs")

    source_order = [
        row["block_id"] for row in _chapter_blocks(chapter_row)
    ]
    source_ids = set(source_order)
    expected_active = [
        str(block_id)
        for window in windows
        for block_id in window.get("active_block_ids") or []
    ]
    if len(expected_active) != len(set(expected_active)):
        raise B4TranslatorError("window plan repeats an active block")
    if any(block_id not in source_ids for block_id in expected_active):
        raise B4TranslatorError("window plan cites a source block absent from document")
    translated = [
        deepcopy(block)
        for window_id in expected_window_ids
        for block in by_window[window_id]["blocks"]
    ]
    translated_ids = [str(row["block_id"]) for row in translated]
    if translated_ids != expected_active:
        raise B4TranslatorError("translated block order differs from window plan")
    if set(translated_ids) != set(expected_active):
        raise B4TranslatorError(
            "chapter translation does not exact-cover active source blocks"
        )
    translated.sort(key=lambda row: source_order.index(str(row["block_id"])))
    metadata_modes = {
        bool(
            row.get(
                "address_metadata_collected",
                row.get("schema_version") in LEGACY_WINDOW_ARTIFACT_SCHEMA_VERSIONS,
            )
        )
        for row in artifacts
    }
    if len(metadata_modes) != 1:
        raise B4TranslatorError(
            "translation windows mix metadata and translation-only contracts"
        )
    address_metadata_collected = next(iter(metadata_modes))
    body = {
        "schema_version": CHAPTER_ARTIFACT_SCHEMA_VERSION,
        "book_id": pack["book_id"],
        "chapter_id": chapter_id,
        "chapter_order": pack["chapter_order"],
        "style_profile_version": anchor["style_profile_version"],
        "measured_arm": anchor["measured_arm"],
        "story_bible_artifact_hash": pack["story_bible_artifact_hash"],
        "translator_pack_artifact_hash": pack["artifact_hash"],
        "address_anchor_artifact_hash": anchor["artifact_hash"],
        "window_plan_hash": window_plan.get("window_plan_hash"),
        "stable_prefix_sha256": next(iter(prefix_hashes)),
        "blocks": translated,
        "window_artifact_hashes": [
            by_window[window_id]["artifact_hash"]
            for window_id in expected_window_ids
        ],
        "provider_receipts": [
            deepcopy(by_window[window_id]["provider_receipt"])
            for window_id in expected_window_ids
            if by_window[window_id].get("provider_receipt") is not None
        ],
        "translator_output_contract": (
            "legacy_translation_with_address_metadata_v1"
            if address_metadata_collected
            else "translation_only_v1"
        ),
        "address_metadata_collected": address_metadata_collected,
        "translation_performed": True,
        "semantic_record_mutation_performed": False,
        "reference_based_scores": None,
    }
    return {**body, "artifact_hash": canonical_hash(body)}


def assert_reference_scoring_allowed_v1(
    translation_artifact: Mapping[str, Any],
) -> None:
    if translation_artifact.get("measured_arm") is not True:
        raise B4TranslatorError(
            "reference-based scoring requires measured_arm=true"
        )


def assert_stable_prefixes_v1(
    requests: Sequence[RenderedTranslationWindowRequestV1],
) -> str:
    if not requests:
        raise B4TranslatorError("at least one Translator request is required")
    hashes = {row.stable_prefix_sha256 for row in requests}
    serialized = {
        canonical_json(list(row.stable_prefix_messages)) for row in requests
    }
    if len(hashes) != 1 or len(serialized) != 1:
        raise B4TranslatorError(
            "Translator chapter windows do not share a byte-identical prefix"
        )
    return next(iter(hashes))


def _validate_window_anchor_pair_scope_v1(
    *,
    window: Mapping[str, Any],
    anchor: Mapping[str, Any],
) -> None:
    anchor_pair_ids: set[str] = set()
    for row in anchor.get("pair_decisions") or []:
        if not isinstance(row, Mapping):
            raise B4TranslatorError("Address Anchor pair decision is malformed")
        pair_id = _text(row.get("pair_id"), "Address Anchor pair_id")
        if pair_id in anchor_pair_ids:
            raise B4TranslatorError("Address Anchor repeats a pair_id")
        anchor_pair_ids.add(pair_id)
    for row in window.get("address_pairs") or []:
        if not isinstance(row, Mapping):
            raise B4TranslatorError("window address pair is malformed")
        pair_id = row.get("pair_id")
        if pair_id is None:
            if row.get("unanchored") is not True:
                raise B4TranslatorError(
                    "window pair with null pair_id must be unanchored"
                )
            continue
        normalized = _text(pair_id, "window pair_id")
        if row.get("unanchored") is True:
            raise B4TranslatorError(
                "window anchored pair cannot be marked unanchored"
            )
        if normalized not in anchor_pair_ids:
            raise B4TranslatorError(
                "window pair_id is absent from the Address Anchor"
            )


def _observed_anchor_deviation_v1(
    *,
    turn_id: str,
    pair_id: str,
    rule: str,
    anchored_pronoun_pair: Mapping[str, Any],
    used_pronoun_pair: Mapping[str, Any] | None,
    vocative: Any,
    reason: str,
) -> dict[str, Any]:
    return {
        "turn_id": turn_id,
        "pair_id": pair_id,
        "rule": rule,
        "anchored_pronoun_pair": deepcopy(dict(anchored_pronoun_pair)),
        "used_pronoun_pair": (
            deepcopy(dict(used_pronoun_pair))
            if isinstance(used_pronoun_pair, Mapping)
            else None
        ),
        "vocative": deepcopy(vocative),
        "reason": reason,
    }


def _address_anchor_prompt_view_v1(
    anchor: Mapping[str, Any],
) -> dict[str, Any]:
    return {
        "schema_version": "literary_b4_address_anchor_prompt_view_v1",
        "chapter_id": anchor["chapter_id"],
        "style_profile_version": anchor["style_profile_version"],
        "measured_arm": anchor["measured_arm"],
        "story_bible_artifact_hash": anchor["story_bible_artifact_hash"],
        "anchor_input_artifact_hash": anchor["anchor_input_artifact_hash"],
        "address_anchor_artifact_hash": anchor["artifact_hash"],
        "pair_decisions": deepcopy(anchor["pair_decisions"]),
        "review_issues": deepcopy(anchor["review_issues"]),
    }


def translator_window_prompt_view_v1(
    *,
    window: Mapping[str, Any],
    anchor: Mapping[str, Any],
) -> dict[str, Any]:
    anchor_by_pair = {
        _text(row.get("pair_id"), "Address Anchor pair_id"): row
        for row in anchor.get("pair_decisions") or []
        if isinstance(row, Mapping)
    }
    turns = []
    for source in window.get("speaker_turns") or []:
        if not isinstance(source, Mapping):
            raise B4TranslatorError("window speaker turn is malformed")
        turns.append(
            {
                "speaker_turn_id": _text(
                    source.get("speaker_turn_id"), "speaker_turn_id"
                ),
                "block_id": _text(source.get("block_id"), "turn block_id"),
                "frame_segment_id": source.get("frame_segment_id"),
                "speaker": {
                    "effective_entity_id": _prompt_endpoint_entity_id_v1(
                        source.get("speaker"), "speaker"
                    )
                },
                "addressee": {
                    "effective_entity_id": _prompt_endpoint_entity_id_v1(
                        source.get("addressee"), "addressee"
                    )
                },
                "address_terms": deepcopy(source.get("address_terms") or []),
                "register_cue": source.get("register_cue"),
                "delivery_tone": source.get("delivery_tone"),
                "utterance_anchor": source.get("utterance_anchor"),
            }
        )

    pairs = []
    for source in window.get("address_pairs") or []:
        if not isinstance(source, Mapping):
            raise B4TranslatorError("window address pair is malformed")
        pair_id = source.get("pair_id")
        decision = anchor_by_pair.get(str(pair_id)) if pair_id is not None else None
        if pair_id is None or (
            isinstance(decision, Mapping) and decision.get("not_anchored") is not None
        ):
            pairs.append(deepcopy(dict(source)))
            continue
        if decision is None:
            raise B4TranslatorError(
                "window pair_id is absent from the Address Anchor"
            )
        pairs.append(
            {
                "pair_id": _text(pair_id, "window pair_id"),
                "speaker_effective_entity_id": source.get(
                    "speaker_effective_entity_id"
                ),
                "addressee_effective_entity_id": source.get(
                    "addressee_effective_entity_id"
                ),
                "turn_ids": deepcopy(source.get("turn_ids") or []),
            }
        )

    return {
        "schema_version": WINDOW_PROMPT_VIEW_SCHEMA_VERSION,
        "source_window_artifact_hash": _text(
            window.get("artifact_hash"), "window artifact_hash"
        ),
        "chapter_id": _text(window.get("chapter_id"), "window chapter_id"),
        "window_id": _text(window.get("window_id"), "window_id"),
        "window_order": window.get("window_order"),
        "active_block_ids": deepcopy(window.get("active_block_ids") or []),
        "preceding_tail_block_ids": deepcopy(
            window.get("preceding_tail_block_ids") or []
        ),
        "speaker_turns": turns,
        "address_pairs": pairs,
    }


def _prompt_endpoint_entity_id_v1(value: Any, label: str) -> str | None:
    if not isinstance(value, Mapping):
        raise B4TranslatorError(f"window {label} endpoint is malformed")
    resolved = value.get("resolved_to_effective_entity")
    entity_ids = value.get("effective_entity_ids") or []
    if not isinstance(entity_ids, list) or any(
        not isinstance(row, str) or not row for row in entity_ids
    ):
        raise B4TranslatorError(
            f"window {label} effective entity ids are malformed"
        )
    if resolved is True:
        if len(entity_ids) != 1:
            raise B4TranslatorError(
                f"resolved window {label} must name exactly one effective entity"
            )
        return entity_ids[0]
    return None


def _expected_pronoun_pair(
    *, anchor: Mapping[str, Any], turn: Mapping[str, Any]
) -> dict[str, str]:
    register = turn.get("register_cue")
    for shift in anchor.get("register_shifts") or []:
        if shift.get("register_cue") == register:
            pair = shift.get("pronoun_pair")
            if not isinstance(pair, Mapping):
                raise B4TranslatorError(
                    "register shift has no pronoun_pair"
                )
            return {
                "speaker": _text(pair.get("speaker"), "speaker pronoun"),
                "addressee": _text(
                    pair.get("addressee"), "addressee pronoun"
                ),
            }
    baseline = anchor.get("pronoun_pair")
    if not isinstance(baseline, Mapping):
        raise B4TranslatorError("anchored pair has no pronoun_pair")
    return {
        "speaker": _text(baseline.get("speaker"), "speaker pronoun"),
        "addressee": _text(
            baseline.get("addressee"), "addressee pronoun"
        ),
    }


def _without_other_pair_address_forms(
    *,
    text: str,
    current_pair_id: str,
    anchor_by_pair: Mapping[str, Mapping[str, Any]],
) -> str:
    current_anchor = anchor_by_pair.get(current_pair_id)
    current_forms = (
        _anchor_address_forms(current_anchor)
        if isinstance(current_anchor, Mapping)
        else set()
    )
    current_form_keys = {form.casefold() for form in current_forms}
    other_forms: set[str] = set()
    for pair_id, anchor in anchor_by_pair.items():
        if pair_id == current_pair_id or anchor.get("not_anchored") is not None:
            continue
        other_forms.update(
            form
            for form in _anchor_address_forms(anchor)
            if form.casefold() not in current_form_keys
        )

    stripped = text
    for form in sorted(
        other_forms,
        key=lambda value: (-len(value), value.casefold()),
    ):
        stripped = re.sub(re.escape(form), "", stripped, flags=re.IGNORECASE)
    return stripped


def _anchor_address_forms(anchor: Mapping[str, Any]) -> set[str]:
    forms: set[str] = set()
    for option in anchor.get("vocative_options") or []:
        if isinstance(option, Mapping) and option.get("form"):
            forms.add(str(option["form"]))
    pronoun_pairs = [anchor.get("pronoun_pair")]
    pronoun_pairs.extend(
        shift.get("pronoun_pair")
        for shift in anchor.get("register_shifts") or []
        if isinstance(shift, Mapping)
    )
    for pair in pronoun_pairs:
        if not isinstance(pair, Mapping):
            continue
        for field in ("speaker", "addressee"):
            value = pair.get(field)
            if isinstance(value, str) and value:
                forms.add(value)
    return forms


def _vocative_counts_rows(
    counts: Mapping[str | None, int],
) -> list[dict[str, Any]]:
    return [
        {"form": form, "count": int(count)}
        for form, count in sorted(
            counts.items(),
            key=lambda item: (
                item[0] is not None,
                str(item[0]).casefold() if item[0] is not None else "",
            ),
        )
    ]


def _pronoun_pair_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["speaker", "addressee"],
        "properties": {
            "speaker": {"type": "string", "minLength": 1, "maxLength": 80},
            "addressee": {
                "type": "string",
                "minLength": 1,
                "maxLength": 80,
            },
        },
    }


def _chapter_blocks(chapter: Mapping[str, Any]) -> list[dict[str, str]]:
    raw = chapter.get("blocks")
    if not isinstance(raw, list) or not raw:
        raise B4TranslatorError("chapter has no source blocks")
    result = []
    seen: set[str] = set()
    for row in raw:
        if not isinstance(row, Mapping):
            raise B4TranslatorError("source block is malformed")
        block_id = _text(row.get("block_id"), "block_id")
        text = row.get("clean_text")
        if not isinstance(text, str) or not text:
            raise B4TranslatorError("source block text is absent")
        if block_id in seen:
            raise B4TranslatorError("chapter repeats a source block")
        seen.add(block_id)
        result.append({"block_id": block_id, "text": text})
    return result


def _verify_translation_window_artifact(
    value: Mapping[str, Any],
) -> dict[str, Any]:
    verified = _verify_hashed(
        value, "artifact_hash", "translation window artifact"
    )
    if verified.get("schema_version") not in {
        WINDOW_ARTIFACT_SCHEMA_VERSION,
        *LEGACY_WINDOW_ARTIFACT_SCHEMA_VERSIONS,
    }:
        raise B4TranslatorError("unsupported translation window artifact schema")
    if verified.get("translation_performed") is not True:
        raise B4TranslatorError("translation window did not perform translation")
    if verified.get("semantic_record_mutation_performed") is not False:
        raise B4TranslatorError("translation window claims semantic mutation")
    return verified


def _sealed_json_bytes(
    raw: bytes, *, hash_field: str, label: str
) -> dict[str, Any]:
    return _verify_hashed(_json_bytes(raw, label), hash_field, label)


def _json_bytes(raw: bytes, label: str) -> dict[str, Any]:
    try:
        value = json.loads(raw.decode("utf-8-sig"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise B4TranslatorError(f"{label} is not UTF-8 JSON") from exc
    if not isinstance(value, dict):
        raise B4TranslatorError(f"{label} must be a JSON object")
    return value


def _verify_hashed(
    value: Mapping[str, Any], field: str, label: str
) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise B4TranslatorError(f"{label} must be an object")
    body = deepcopy(dict(value))
    observed = body.pop(field, None)
    if observed != canonical_hash(body):
        raise B4TranslatorError(f"{label} hash mismatch")
    return deepcopy(dict(value))


def _string_list(value: Any, label: str) -> list[str]:
    if not isinstance(value, list) or any(
        not isinstance(row, str) or not row for row in value
    ):
        raise B4TranslatorError(f"{label} must be a list of strings")
    return list(value)


def _text(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise B4TranslatorError(f"{label} must be non-empty text")
    return value.strip()


__all__ = [
    "B4TranslatorError",
    "CHAPTER_ARTIFACT_SCHEMA_VERSION",
    "PROMPT_ID",
    "RESPONSE_SCHEMA_VERSION",
    "ROLE_ID",
    "RenderedTranslationWindowRequestV1",
    "SYSTEM_PROMPT",
    "WINDOW_ARTIFACT_SCHEMA_VERSION",
    "WINDOW_PROMPT_VIEW_SCHEMA_VERSION",
    "assemble_translation_chapter_v1",
    "assert_reference_scoring_allowed_v1",
    "assert_stable_prefixes_v1",
    "build_translation_window_artifact_v1",
    "render_translation_window_request_v1",
    "translator_window_prompt_view_v1",
    "translation_window_response_schema_v1",
    "validate_translation_window_response_v1",
]
