"""Case-stable admission contract for D2L Builder 2."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
from hashlib import sha256
from typing import Any, Mapping

from pipeline.prepass import d2l_b2_consistency_contract_v3 as v3
from pipeline.prepass import d2l_b2_consistency_contract_v3_4 as v3_4


PROMPT_VERSION = "d2l_b2_consistency_admission_v3_5"
RESPONSE_SCHEMA_VERSION = v3_4.RESPONSE_SCHEMA_VERSION
VALIDATOR_VERSION = "d2l_b2_consistency_admission_validator_v3_5"
VALIDATOR_REVISION = "v3_5"
RESPONSE_FORMAT = v3_4.RESPONSE_FORMAT
parse_response_json = v3_4.parse_response_json
schema_sha256 = v3_4.schema_sha256
user_payload_sha256 = v3_4.user_payload_sha256


_VERSION_MARKER_OLD = "Prompt version: d2l_b2_consistency_admission_v3_4."
_VERSION_MARKER_NEW = "Prompt version: d2l_b2_consistency_admission_v3_5."
_COPY_RULE = (
    "- Copy canonical_source byte-for-byte from one supplied surface, preserving\n"
    "  capitalization, punctuation, spacing, and symbols. Never normalize it."
)
_COPY_RULE_V3_5 = _COPY_RULE + (
    " Candidate\n"
    "  acronyms are case-sensitive: for example, an uppercase supplied acronym\n"
    "  must remain uppercase."
)

if v3_4.SYSTEM_PROMPT.count(_VERSION_MARKER_OLD) != 1:
    raise RuntimeError("V3.4 prompt version marker drifted")
if v3_4.SYSTEM_PROMPT.count(_COPY_RULE) != 1:
    raise RuntimeError("V3.4 canonical-source rule drifted")

SYSTEM_PROMPT = (
    v3_4.SYSTEM_PROMPT.replace(_VERSION_MARKER_OLD, _VERSION_MARKER_NEW)
    .replace(_COPY_RULE, _COPY_RULE_V3_5)
)


def prompt_sha256() -> str:
    return sha256(SYSTEM_PROMPT.encode("utf-8")).hexdigest().upper()


def render_messages(packet: Mapping[str, Any]) -> list[dict[str, str]]:
    messages = v3_4.render_messages(packet)
    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        *messages[1:],
    ]


def validate_output(
    parsed: Mapping[str, Any], *, packet: Mapping[str, Any]
) -> v3.B2V3Validation:
    repaired = deepcopy(dict(parsed))
    candidates = {
        str(row.get("candidate_id")): tuple(row.get("surfaces") or ())
        for row in packet.get("candidates") or ()
        if isinstance(row, Mapping)
    }
    warnings: list[str] = []
    decisions = repaired.get("decisions")
    if isinstance(decisions, list):
        for row in decisions:
            if not isinstance(row, dict) or row.get("decision") != "admit":
                continue
            candidate_id = row.get("candidate_id")
            canonical = row.get("canonical_source")
            surfaces = candidates.get(str(candidate_id), ())
            if not isinstance(canonical, str) or canonical in surfaces:
                continue
            matches = [
                surface
                for surface in surfaces
                if isinstance(surface, str) and surface.casefold() == canonical.casefold()
            ]
            if len(matches) != 1:
                continue
            restored = matches[0]
            if (
                row.get("directive") == "preserve"
                and row.get("primary_target_vi") == canonical
            ):
                row["primary_target_vi"] = restored
            row["canonical_source"] = restored
            warnings.append(
                f"candidate {candidate_id} canonical_source case was restored byte-exactly"
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
