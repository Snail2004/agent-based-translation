"""Vietnamese address-anchor contract over one sealed B4 chapter pack."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
import hashlib
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from jsonschema import Draft202012Validator

from pipeline.literary.b4_story_bible_assembler_v1 import (
    ANCHOR_INPUT_SCHEMA_VERSION,
    ANCHOR_OUTPUT_SCHEMA_VERSION,
    B4StoryBibleError,
    validate_address_anchor_output_v1,
)
from pipeline.literary.builder_pilot import load_system_prompt_from_design
from pipeline.literary.checkpoint import canonical_hash, canonical_json


ROLE_ID = "literary.b4.address_anchor"
PROMPT_ID = "literary_b4_address_anchor_v2"
REQUEST_SCHEMA_VERSION = "literary_b4_address_anchor_request_v2"
ARTIFACT_SCHEMA_VERSION = "literary_b4_address_anchor_artifact_v3"

SYSTEM_PROMPT = """You are the Vietnamese Address Anchor for one literary chapter.
Prompt version: literary_b4_address_anchor_v2.

Read only the supplied style profile and address-pair evidence. For each
anchorable pair, choose one chapter-stable pronoun_pair: speaker is how the
speaker refers to self and addressee is the pronoun used for the listener.
List permitted sentence-level vocatives separately in vocative_options. A
vocative such as "ong Heathcliff" or "thua ong" is not a pronoun and may vary
from sentence to sentence.

Add a register_shift only when a supplied register cue changes the pronoun
pair itself, for example toi/ong to tao/may. Do not emit a register_shift that
duplicates the baseline pronoun_pair. The Translator may choose any supplied
vocative per sentence and is not required to reuse the same one.

Do not translate source sentences. Do not create, merge, split, or correct
entities, relations, states, or evidence. Do not infer a form for an omitted
or unanchorable pair. You may return not_anchored when the evidence does not
support a responsible choice. Cite only source block ids supplied on that
pair. Copy the supplied pair_ref into the response pair_id field. Output JSON
only and follow the supplied response shape exactly.
"""


class B4AddressAnchorError(RuntimeError):
    pass


@dataclass(frozen=True)
class RenderedAddressAnchorRequestV1:
    request_fingerprint: str
    messages: tuple[dict[str, str], ...]
    response_schema: dict[str, Any]
    packet: dict[str, Any]
    anchor_input: dict[str, Any]
    style_profile_version: str
    measured_arm: bool
    pair_ref_to_id: dict[str, str]


def load_style_profile_v1(*, design_doc: Path, style_profile_version: str) -> str:
    version = _text(style_profile_version, "style_profile_version")
    try:
        profile = load_system_prompt_from_design(Path(design_doc), version)
    except (OSError, ValueError) as exc:
        raise B4AddressAnchorError(
            f"cannot load pinned style profile: {version}"
        ) from exc
    if f"- Prompt version: {version}." not in profile:
        raise B4AddressAnchorError("style profile version marker differs")
    return profile


def address_anchor_response_schema_v1() -> dict[str, Any]:
    pronoun_pair = {
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
    vocative = {
        "type": "object",
        "additionalProperties": False,
        "required": ["form"],
        "properties": {
            "form": {"type": "string", "minLength": 1, "maxLength": 120},
            "note": {"type": "string", "minLength": 1, "maxLength": 800},
        },
    }
    shift = {
        "type": "object",
        "additionalProperties": False,
        "required": ["register_cue", "pronoun_pair", "rationale"],
        "properties": {
            "register_cue": {"type": "string", "minLength": 1, "maxLength": 80},
            "pronoun_pair": deepcopy(pronoun_pair),
            "rationale": {"type": "string", "minLength": 1, "maxLength": 800},
        },
    }
    return {
        "type": "object",
        "additionalProperties": False,
        "required": [
            "schema_version",
            "chapter_id",
            "anchor_input_artifact_hash",
            "pair_decisions",
        ],
        "properties": {
            "schema_version": {
                "type": "string",
                "const": ANCHOR_OUTPUT_SCHEMA_VERSION,
            },
            "chapter_id": {"type": "string", "minLength": 1, "maxLength": 120},
            "anchor_input_artifact_hash": {
                "type": "string",
                "minLength": 64,
                "maxLength": 64,
            },
            "pair_decisions": {
                "type": "array",
                "maxItems": 128,
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": [
                        "pair_id",
                        "pronoun_pair",
                        "vocative_options",
                        "register_shifts",
                        "evidence_refs",
                        "model_confidence",
                        "not_anchored",
                    ],
                    "properties": {
                        "pair_id": {
                            "type": "string",
                            "pattern": "^P[1-9][0-9]*$",
                        },
                        "pronoun_pair": {
                            "anyOf": [deepcopy(pronoun_pair), {"type": "null"}]
                        },
                        "vocative_options": {
                            "type": "array",
                            "maxItems": 32,
                            "items": vocative,
                        },
                        "register_shifts": {
                            "type": "array",
                            "maxItems": 16,
                            "items": shift,
                        },
                        "evidence_refs": {
                            "type": "array",
                            "maxItems": 64,
                            "uniqueItems": True,
                            "items": {
                                "type": "string",
                                "minLength": 1,
                                "maxLength": 120,
                            },
                        },
                        "model_confidence": {
                            "type": "string",
                            "enum": ["high", "medium", "low"],
                        },
                        "not_anchored": {
                            "anyOf": [
                                {
                                    "type": "object",
                                    "additionalProperties": False,
                                    "required": ["reason"],
                                    "properties": {
                                        "reason": {
                                            "type": "string",
                                            "minLength": 1,
                                            "maxLength": 1200,
                                        }
                                    },
                                },
                                {"type": "null"},
                            ]
                        },
                    },
                },
            },
        },
    }


def render_address_anchor_request_v1(
    *,
    anchor_input: Mapping[str, Any],
    style_profile: str,
    style_profile_version: str,
    measured_arm: bool,
) -> RenderedAddressAnchorRequestV1:
    verified = _verify_anchor_input(anchor_input)
    version = _text(style_profile_version, "style_profile_version")
    profile = _text(style_profile, "style_profile")
    if not isinstance(measured_arm, bool):
        raise B4AddressAnchorError("measured_arm must be boolean")

    pairs = sorted(
        (deepcopy(dict(row)) for row in verified["pairs"]),
        key=lambda row: str(row["pair_id"]),
    )
    pair_ref_to_id = {
        f"P{index}": str(row["pair_id"])
        for index, row in enumerate(pairs, start=1)
    }
    id_to_ref = {value: key for key, value in pair_ref_to_id.items()}
    model_pairs = []
    for source in pairs:
        source["pair_ref"] = id_to_ref.pop(str(source.pop("pair_id")))
        source.pop("speaker_effective_entity_id", None)
        source.pop("addressee_effective_entity_id", None)
        source["speaker_claims"] = _claim_values(source.get("speaker_claims"))
        source["addressee_claims"] = _claim_values(source.get("addressee_claims"))
        source["relations"] = [
            _relation_view(row) for row in source.get("relations") or []
        ]
        model_pairs.append(source)
    packet_body = {
        "schema_version": REQUEST_SCHEMA_VERSION,
        "book_id": verified["book_id"],
        "chapter_id": verified["chapter_id"],
        "style_profile_version": version,
        "measured_arm": measured_arm,
        "anchor_input_artifact_hash": verified["artifact_hash"],
        "pairs": model_pairs,
        "authority_policy": {
            "address_forms_only": True,
            "translation_performed": False,
            "semantic_record_mutation_performed": False,
        },
    }
    packet = {**packet_body, "packet_hash": canonical_hash(packet_body)}
    schema = address_anchor_response_schema_v1()
    messages = (
        {"role": "system", "content": profile},
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": canonical_json(packet)},
    )
    request_body = {
        "prompt_id": PROMPT_ID,
        "prompt_sha256": hashlib.sha256(
            SYSTEM_PROMPT.encode("utf-8")
        ).hexdigest(),
        "style_profile_version": version,
        "style_profile_sha256": hashlib.sha256(profile.encode("utf-8")).hexdigest(),
        "anchor_input_artifact_hash": verified["artifact_hash"],
        "packet_hash": packet["packet_hash"],
        "response_schema_hash": canonical_hash(schema),
        "messages": list(messages),
    }
    return RenderedAddressAnchorRequestV1(
        request_fingerprint=canonical_hash(request_body),
        messages=messages,
        response_schema=schema,
        packet=packet,
        anchor_input=verified,
        style_profile_version=version,
        measured_arm=measured_arm,
        pair_ref_to_id=pair_ref_to_id,
    )


def validate_address_anchor_response_v1(
    *,
    rendered: RenderedAddressAnchorRequestV1,
    response: Mapping[str, Any],
) -> dict[str, Any]:
    raw = deepcopy(dict(response))
    errors = sorted(
        Draft202012Validator(rendered.response_schema).iter_errors(raw),
        key=lambda row: list(row.path),
    )
    if errors:
        raise B4AddressAnchorError(
            f"Address Anchor schema failure: {errors[0].message}"
        )
    resolved = deepcopy(raw)
    for row in resolved["pair_decisions"]:
        pair_ref = row["pair_id"]
        try:
            row["pair_id"] = rendered.pair_ref_to_id[pair_ref]
        except KeyError as exc:
            raise B4AddressAnchorError(
                "Address Anchor returned a foreign pair ref"
            ) from exc
    normalization_observations: list[dict[str, str]] = []
    for row in resolved["pair_decisions"]:
        baseline = row.get("pronoun_pair")
        retained_shifts = []
        for shift in row.get("register_shifts") or []:
            if baseline is not None and shift.get("pronoun_pair") == baseline:
                normalization_observations.append(
                    {
                        "observation_kind": "noop_register_shift_removed",
                        "pair_id": str(row["pair_id"]),
                        "register_cue": str(shift["register_cue"]),
                    }
                )
                continue
            retained_shifts.append(shift)
        row["register_shifts"] = retained_shifts
    try:
        validated = validate_address_anchor_output_v1(
            anchor_input=rendered.anchor_input,
            response=resolved,
        )
    except B4StoryBibleError as exc:
        raise B4AddressAnchorError(str(exc)) from exc
    body = deepcopy(dict(validated))
    body.pop("artifact_hash", None)
    body["schema_version"] = "literary_b4_validated_address_anchor_v3"
    body["normalization_observations"] = sorted(
        normalization_observations,
        key=lambda row: (
            row["pair_id"],
            row["register_cue"],
            row["observation_kind"],
        ),
    )
    return {**body, "artifact_hash": canonical_hash(body)}


def build_address_anchor_artifact_v1(
    *,
    rendered: RenderedAddressAnchorRequestV1,
    validated_response: Mapping[str, Any],
    provider_receipt: Mapping[str, Any] | None,
    provider_called: bool,
) -> dict[str, Any]:
    if not isinstance(provider_called, bool):
        raise B4AddressAnchorError("provider_called must be boolean")
    if provider_called != (provider_receipt is not None):
        raise B4AddressAnchorError("Address Anchor provider receipt differs")
    validated = _verify_hashed(
        validated_response, "artifact_hash", "validated Address Anchor response"
    )
    if (
        validated.get("chapter_id") != rendered.anchor_input["chapter_id"]
        or validated.get("anchor_input_artifact_hash")
        != rendered.anchor_input["artifact_hash"]
    ):
        raise B4AddressAnchorError("validated Address Anchor lineage differs")
    body = {
        "schema_version": ARTIFACT_SCHEMA_VERSION,
        "book_id": rendered.anchor_input["book_id"],
        "chapter_id": rendered.anchor_input["chapter_id"],
        "style_profile_version": rendered.style_profile_version,
        "measured_arm": rendered.measured_arm,
        "story_bible_artifact_hash": rendered.anchor_input[
            "story_bible_artifact_hash"
        ],
        "anchor_input_artifact_hash": rendered.anchor_input["artifact_hash"],
        "request_fingerprint": rendered.request_fingerprint,
        "pair_decisions": deepcopy(validated["pair_decisions"]),
        "review_issues": deepcopy(validated["review_issues"]),
        "normalization_observations": deepcopy(
            validated["normalization_observations"]
        ),
        "provider_called": provider_called,
        "provider_receipt": (
            deepcopy(dict(provider_receipt)) if provider_receipt is not None else None
        ),
        "translation_performed": False,
        "semantic_record_mutation_performed": False,
    }
    return {**body, "artifact_hash": canonical_hash(body)}


def build_empty_address_anchor_artifact_v1(
    *,
    anchor_input: Mapping[str, Any],
    style_profile_version: str,
    measured_arm: bool,
) -> dict[str, Any]:
    rendered = render_address_anchor_request_v1(
        anchor_input=anchor_input,
        style_profile="No address pairs were supplied. "
        f"- Prompt version: {style_profile_version}.",
        style_profile_version=style_profile_version,
        measured_arm=measured_arm,
    )
    if rendered.anchor_input["pairs"]:
        raise B4AddressAnchorError("empty Address Anchor requires zero pairs")
    response = {
        "schema_version": ANCHOR_OUTPUT_SCHEMA_VERSION,
        "chapter_id": rendered.anchor_input["chapter_id"],
        "anchor_input_artifact_hash": rendered.anchor_input["artifact_hash"],
        "pair_decisions": [],
    }
    validated = validate_address_anchor_response_v1(
        rendered=rendered, response=response
    )
    return build_address_anchor_artifact_v1(
        rendered=rendered,
        validated_response=validated,
        provider_receipt=None,
        provider_called=False,
    )


def make_address_anchor_semantic_validator_v1(
    *,
    rendered: RenderedAddressAnchorRequestV1,
) -> Callable[[Mapping[str, Any]], Mapping[str, Any]]:
    def validate(payload: Mapping[str, Any]) -> Mapping[str, Any]:
        return validate_address_anchor_response_v1(
            rendered=rendered,
            response=payload,
        )

    return validate


def verify_address_anchor_artifact_v1(
    artifact: Mapping[str, Any],
) -> dict[str, Any]:
    verified = _verify_hashed(
        artifact, "artifact_hash", "Address Anchor artifact"
    )
    if verified.get("schema_version") != ARTIFACT_SCHEMA_VERSION:
        raise B4AddressAnchorError("unsupported Address Anchor artifact schema")
    if verified.get("translation_performed") is not False:
        raise B4AddressAnchorError("Address Anchor claims translation")
    if verified.get("semantic_record_mutation_performed") is not False:
        raise B4AddressAnchorError("Address Anchor claims semantic mutation")
    if not isinstance(verified.get("measured_arm"), bool):
        raise B4AddressAnchorError("Address Anchor measured_arm is malformed")
    _text(verified.get("style_profile_version"), "style_profile_version")
    decisions = verified.get("pair_decisions")
    if not isinstance(decisions, list):
        raise B4AddressAnchorError("Address Anchor decisions are malformed")
    observations = verified.get("normalization_observations")
    if not isinstance(observations, list) or any(
        not isinstance(row, Mapping)
        or set(row)
        != {"observation_kind", "pair_id", "register_cue"}
        or row.get("observation_kind") != "noop_register_shift_removed"
        for row in observations
    ):
        raise B4AddressAnchorError(
            "Address Anchor normalization observations are malformed"
        )
    return verified


def _verify_anchor_input(value: Mapping[str, Any]) -> dict[str, Any]:
    verified = _verify_hashed(value, "artifact_hash", "Address Anchor input")
    if verified.get("schema_version") != ANCHOR_INPUT_SCHEMA_VERSION:
        raise B4AddressAnchorError("unsupported Address Anchor input schema")
    pairs = verified.get("pairs")
    if not isinstance(pairs, list) or any(
        not isinstance(row, Mapping) for row in pairs
    ):
        raise B4AddressAnchorError("Address Anchor input pairs are malformed")
    pair_ids = [_text(row.get("pair_id"), "pair_id") for row in pairs]
    if len(pair_ids) != len(set(pair_ids)):
        raise B4AddressAnchorError("Address Anchor input repeats a pair")
    return verified


def _claim_values(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        return {}
    result: dict[str, Any] = {}
    for field, raw in value.items():
        if not isinstance(raw, Mapping):
            continue
        if "value" in raw:
            result[str(field)] = deepcopy(raw["value"])
        elif isinstance(raw.get("values"), list):
            result[str(field)] = deepcopy(raw["values"])
    return result


def _relation_view(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise B4AddressAnchorError("Address Anchor relation is malformed")
    return {
        key: deepcopy(value.get(key))
        for key in (
            "relation_family",
            "relation_type",
            "relation_note",
            "semantic_status",
            "structurally_contested",
        )
        if key in value
    }


def _verify_hashed(
    value: Mapping[str, Any], field: str, label: str
) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise B4AddressAnchorError(f"{label} must be an object")
    body = deepcopy(dict(value))
    observed = body.pop(field, None)
    if observed != canonical_hash(body):
        raise B4AddressAnchorError(f"{label} hash mismatch")
    return deepcopy(dict(value))


def _text(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise B4AddressAnchorError(f"{label} must be non-empty text")
    return value.strip()


__all__ = [
    "ARTIFACT_SCHEMA_VERSION",
    "B4AddressAnchorError",
    "PROMPT_ID",
    "REQUEST_SCHEMA_VERSION",
    "ROLE_ID",
    "RenderedAddressAnchorRequestV1",
    "SYSTEM_PROMPT",
    "address_anchor_response_schema_v1",
    "build_address_anchor_artifact_v1",
    "build_empty_address_anchor_artifact_v1",
    "load_style_profile_v1",
    "make_address_anchor_semantic_validator_v1",
    "render_address_anchor_request_v1",
    "validate_address_anchor_response_v1",
    "verify_address_anchor_artifact_v1",
]
