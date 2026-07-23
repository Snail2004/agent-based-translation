"""Decision-focused model packet for D2L Builder 2 admission."""

from __future__ import annotations

import json
from dataclasses import replace
from hashlib import sha256
from typing import Any, Mapping, Sequence

from pipeline.prepass import d2l_b2_consistency_contract_v3 as v3
from pipeline.prepass import d2l_b2_consistency_contract_v3_6 as v3_6
from pipeline.prepass.d2l_b2_packet_plan_v2 import canonical_sha256


PROMPT_VERSION = "d2l_b2_consistency_admission_v3_7"
RESPONSE_SCHEMA_VERSION = v3_6.RESPONSE_SCHEMA_VERSION
VALIDATOR_VERSION = "d2l_b2_consistency_admission_validator_v3_7"
VALIDATOR_REVISION = "v3_7"
MODEL_INPUT_PROJECTION_VERSION = "d2l_b2_model_packet_projection_v1"
RESPONSE_FORMAT = v3_6.RESPONSE_FORMAT
parse_response_json = v3_6.parse_response_json
schema_sha256 = v3_6.schema_sha256
user_payload_sha256 = v3_6.user_payload_sha256

MODEL_CANDIDATE_FIELDS = (
    "candidate_id",
    "surfaces",
    "evidence_block_ids",
    "evidence_complete",
    "support_block_count",
    "window_count",
)

_VERSION_MARKER_OLD = "Prompt version: d2l_b2_consistency_admission_v3_6."
_VERSION_MARKER_NEW = "Prompt version: d2l_b2_consistency_admission_v3_7."
_INPUT_MARKER = "Different candidate IDs remain\ndistinct."
_INPUT_RULE = _INPUT_MARKER + (
    " Candidate rows contain only decision-relevant fields.\n"
    "The evidence_block_ids list is the complete allow-list for citations.\n"
    "support_block_count and window_count describe recurrence only; they do not\n"
    "authorize any additional evidence ID. Broader provenance is retained by\n"
    "code outside this model-facing packet."
)

if v3_6.SYSTEM_PROMPT.count(_VERSION_MARKER_OLD) != 1:
    raise RuntimeError("V3.6 prompt version marker drifted")
if v3_6.SYSTEM_PROMPT.count(_INPUT_MARKER) != 1:
    raise RuntimeError("V3.6 input marker drifted")

SYSTEM_PROMPT = (
    v3_6.SYSTEM_PROMPT.replace(_VERSION_MARKER_OLD, _VERSION_MARKER_NEW)
    .replace(_INPUT_MARKER, _INPUT_RULE)
)


def prompt_sha256() -> str:
    return sha256(SYSTEM_PROMPT.encode("utf-8")).hexdigest().upper()


def project_model_packet(packet: Mapping[str, Any]) -> dict[str, Any]:
    v3._validate_packet(packet)
    candidates: list[dict[str, Any]] = []
    for row in packet["candidates"]:
        candidates.append({key: row[key] for key in MODEL_CANDIDATE_FIELDS})
    return {
        "packet_id": packet["packet_id"],
        "chapter_id": packet["chapter_id"],
        "candidates": candidates,
    }


def model_packet_sha256(packet: Mapping[str, Any]) -> str:
    return canonical_sha256(project_model_packet(packet))


def render_messages(packet: Mapping[str, Any]) -> list[dict[str, str]]:
    projected = project_model_packet(packet)
    rendered_blocks = "\n".join(
        f"[{row['block_id']}] {row['text']}" for row in packet["source_blocks"]
    )
    user = (
        "CANDIDATE_PACKET_JSON\n"
        + json.dumps(
            projected,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n\nENGLISH_SOURCE_BLOCKS\n"
        + rendered_blocks
    )
    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": user},
    ]


def validate_output(
    parsed: Mapping[str, Any], *, packet: Mapping[str, Any]
) -> v3.B2V3Validation:
    validation = v3_6.validate_output(parsed, packet=packet)
    allowed = {
        str(row["candidate_id"]): set(row["evidence_block_ids"])
        for row in packet["candidates"]
    }
    extra_errors: list[str] = []
    decisions = parsed.get("decisions")
    if isinstance(decisions, Sequence) and not isinstance(
        decisions, (str, bytes)
    ):
        for index, row in enumerate(decisions):
            if not isinstance(row, Mapping):
                continue
            candidate_id = row.get("candidate_id")
            candidate_allowed = allowed.get(str(candidate_id))
            if candidate_allowed is None:
                continue
            _check_evidence_subset(
                row.get("evidence_block_ids"),
                allowed=candidate_allowed,
                label=f"decisions[{index}].evidence_block_ids",
                errors=extra_errors,
            )
            alternates = row.get("alternates")
            if isinstance(alternates, list):
                for alternate_index, alternate in enumerate(alternates):
                    if not isinstance(alternate, Mapping):
                        continue
                    _check_evidence_subset(
                        alternate.get("evidence_block_ids"),
                        allowed=candidate_allowed,
                        label=(
                            f"decisions[{index}].alternates[{alternate_index}]"
                            ".evidence_block_ids"
                        ),
                        errors=extra_errors,
                    )
    if not extra_errors:
        return validation
    combined = list(validation.errors)
    for error in extra_errors:
        if error not in combined:
            combined.append(error)
    return replace(validation, errors=tuple(combined))


def _check_evidence_subset(
    value: Any,
    *,
    allowed: set[str],
    label: str,
    errors: list[str],
) -> None:
    if not isinstance(value, list) or not all(
        isinstance(item, str) for item in value
    ):
        return
    if not set(value).issubset(allowed):
        errors.append(
            f"{label} must use only the supplied candidate evidence_block_ids"
        )
