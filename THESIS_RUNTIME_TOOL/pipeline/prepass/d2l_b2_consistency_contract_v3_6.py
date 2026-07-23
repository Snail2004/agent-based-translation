"""Candidate-normalization-stable admission contract for D2L Builder 2."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
from hashlib import sha256
from typing import Any, Mapping

from pipeline.prepass import d2l_b2_consistency_contract_v3 as v3
from pipeline.prepass import d2l_b2_consistency_contract_v3_4 as v3_4
from pipeline.prepass import d2l_b2_consistency_contract_v3_5 as v3_5
from pipeline.prepass.concept_key import normalize_phrase


PROMPT_VERSION = "d2l_b2_consistency_admission_v3_6"
RESPONSE_SCHEMA_VERSION = v3_5.RESPONSE_SCHEMA_VERSION
VALIDATOR_VERSION = "d2l_b2_consistency_admission_validator_v3_6"
VALIDATOR_REVISION = "v3_6"
RESPONSE_FORMAT = v3_5.RESPONSE_FORMAT
parse_response_json = v3_5.parse_response_json
schema_sha256 = v3_5.schema_sha256
user_payload_sha256 = v3_5.user_payload_sha256


_VERSION_MARKER_OLD = "Prompt version: d2l_b2_consistency_admission_v3_5."
_VERSION_MARKER_NEW = "Prompt version: d2l_b2_consistency_admission_v3_6."
_ACRONYM_RULE = (
    "  acronyms are case-sensitive: for example, an uppercase supplied acronym\n"
    "  must remain uppercase."
)
_SURFACE_RULE = _ACRONYM_RULE + (
    " When several supplied surfaces differ only by the\n"
    "  candidate normalization policy, copy one complete supplied surface; never\n"
    "  synthesize a new capitalization or spacing variant."
)

if v3_5.SYSTEM_PROMPT.count(_VERSION_MARKER_OLD) != 1:
    raise RuntimeError("V3.5 prompt version marker drifted")
if v3_5.SYSTEM_PROMPT.count(_ACRONYM_RULE) != 1:
    raise RuntimeError("V3.5 acronym rule drifted")

SYSTEM_PROMPT = (
    v3_5.SYSTEM_PROMPT.replace(_VERSION_MARKER_OLD, _VERSION_MARKER_NEW)
    .replace(_ACRONYM_RULE, _SURFACE_RULE)
)


def prompt_sha256() -> str:
    return sha256(SYSTEM_PROMPT.encode("utf-8")).hexdigest().upper()


def render_messages(packet: Mapping[str, Any]) -> list[dict[str, str]]:
    messages = v3_5.render_messages(packet)
    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        *messages[1:],
    ]


def validate_output(
    parsed: Mapping[str, Any], *, packet: Mapping[str, Any]
) -> v3.B2V3Validation:
    repaired = deepcopy(dict(parsed))
    candidates = {
        str(row.get("candidate_id")): dict(row)
        for row in packet.get("candidates") or ()
        if isinstance(row, Mapping)
    }
    warnings: list[str] = []
    decisions = repaired.get("decisions")
    if isinstance(decisions, list):
        for row in decisions:
            if not isinstance(row, dict) or row.get("decision") != "admit":
                continue
            candidate_id = str(row.get("candidate_id"))
            canonical = row.get("canonical_source")
            candidate = candidates.get(candidate_id)
            if not isinstance(canonical, str) or not candidate:
                continue
            surfaces = tuple(candidate.get("surfaces") or ())
            if canonical in surfaces:
                continue
            if normalize_phrase(canonical) != candidate.get("normalized_surface"):
                continue
            if not surfaces or not isinstance(surfaces[0], str):
                continue
            restored = surfaces[0]
            if (
                row.get("directive") == "preserve"
                and row.get("primary_target_vi") == canonical
            ):
                row["primary_target_vi"] = restored
            row["canonical_source"] = restored
            warnings.append(
                f"candidate {candidate_id} canonical_source was restored by normalize_phrase_exact_v1"
            )

    validation = v3_4.validate_output(repaired, packet=packet)
    if not warnings:
        return validation
    return replace(
        validation,
        normalization_warnings=(
            *validation.normalization_warnings,
            *warnings,
        ),
    )
