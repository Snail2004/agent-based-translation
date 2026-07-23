from __future__ import annotations

import json
import unicodedata
from dataclasses import dataclass
from hashlib import sha256
from typing import Any, Mapping, Sequence


PROMPT_VERSION = "d2l_b2_multi_target_audit_v1"
RESPONSE_SCHEMA_VERSION = "d2l_b2_multi_target_schema_v1"
VALIDATOR_VERSION = "d2l_b2_multi_target_validator_v1"

SYSTEM_PROMPT = r"""You are the Multiple-Target Terminology Auditor for an
autonomous English-to-Vietnamese technical-book translation pipeline.

Prompt version: d2l_b2_multi_target_audit_v1.

INPUT
You receive independent terminology entries that Builder 2 already admitted.
Each entry has more than one supplied Vietnamese target proposal and exact
English source blocks. Entries in one packet are batched only to reuse source
context. They are not candidates for merging with one another.

YOUR ONLY JOB
Return exactly one decision for every supplied candidate_id.

For a resolved entry:
- assign exactly one supplied target as canonical;
- retain a supplied target as an alternative only when its source-use condition
  is concise and explicit;
- reject any other supplied target;
- exact-cover every supplied target proposal once;
- cite only supplied evidence block IDs for that entry.

If the supplied blocks cannot safely choose one canonical target, return
pending. A pending decision marks every supplied target pending and explains
what evidence is missing.

TARGET DISPOSITIONS
- canonical: exactly one in a resolved decision; applicability must be null;
- alternative: optional; applicability must state when this target applies;
- reject: the proposal is not retained; applicability must be null;
- pending: used only when the whole entry is pending; applicability must be
  null.

FORBIDDEN WORK
- Do not merge, split, rename, admit, or reject English terminology entries.
- Do not invent or rewrite a Vietnamese target, source surface, candidate ID,
  evidence block, or omitted context.
- Do not infer that entries batched in one packet are related.
- Do not use gold, a community glossary, external memory, expected answers, or
  outside sources.
- Do not publish a glossary or change a Builder 2 decision.
- Do not omit a supplied candidate or target proposal.

Return JSON only with exactly this shape:
{
  "packet_id": "supplied packet id",
  "decisions": [
    {
      "candidate_id": "supplied candidate id",
      "action": "resolve|pending",
      "target_dispositions": [
        {
          "target_vi": "supplied target",
          "disposition": "canonical|alternative|reject|pending",
          "applicability": "condition or null"
        }
      ],
      "evidence_block_ids": ["supplied block id"],
      "rationale": "short source-grounded reason",
      "pending_reason": "reason or null"
    }
  ]
}
"""

RESPONSE_FORMAT: dict[str, Any] = {
    "type": "json_schema",
    "json_schema": {
        "name": PROMPT_VERSION,
        "strict": True,
        "schema": {
            "type": "object",
            "additionalProperties": False,
            "required": ["packet_id", "decisions"],
            "properties": {
                "packet_id": {"type": "string"},
                "decisions": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "additionalProperties": False,
                        "required": [
                            "candidate_id",
                            "action",
                            "target_dispositions",
                            "evidence_block_ids",
                            "rationale",
                            "pending_reason",
                        ],
                        "properties": {
                            "candidate_id": {"type": "string"},
                            "action": {
                                "type": "string",
                                "enum": ["resolve", "pending"],
                            },
                            "target_dispositions": {
                                "type": "array",
                                "minItems": 2,
                                "items": {
                                    "type": "object",
                                    "additionalProperties": False,
                                    "required": [
                                        "target_vi",
                                        "disposition",
                                        "applicability",
                                    ],
                                    "properties": {
                                        "target_vi": {"type": "string"},
                                        "disposition": {
                                            "type": "string",
                                            "enum": [
                                                "canonical",
                                                "alternative",
                                                "reject",
                                                "pending",
                                            ],
                                        },
                                        "applicability": {
                                            "type": ["string", "null"]
                                        },
                                    },
                                },
                            },
                            "evidence_block_ids": {
                                "type": "array",
                                "minItems": 1,
                                "items": {"type": "string"},
                            },
                            "rationale": {"type": "string"},
                            "pending_reason": {"type": ["string", "null"]},
                        },
                    },
                },
            },
        },
    },
}


class MultiTargetContractError(ValueError):
    pass


@dataclass(frozen=True)
class TargetDisposition:
    target_vi: str
    disposition: str
    applicability: str | None


@dataclass(frozen=True)
class MultiTargetDecision:
    candidate_id: str
    action: str
    target_dispositions: tuple[TargetDisposition, ...]
    evidence_block_ids: tuple[str, ...]
    rationale: str
    pending_reason: str | None


@dataclass(frozen=True)
class MultiTargetValidation:
    packet_id: str
    decisions: tuple[MultiTargetDecision, ...]
    errors: tuple[str, ...]
    missing_candidate_ids: tuple[str, ...]
    duplicate_candidate_ids: tuple[str, ...]


def prompt_sha256() -> str:
    return sha256(SYSTEM_PROMPT.encode("utf-8")).hexdigest().upper()


def response_schema_sha256() -> str:
    rendered = json.dumps(
        RESPONSE_FORMAT["json_schema"]["schema"],
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return sha256(rendered.encode("utf-8")).hexdigest().upper()


def render_messages(packet: Mapping[str, Any]) -> list[dict[str, str]]:
    _validate_packet(packet)
    blocks = "\n".join(
        f"[{row['block_id']}] {row['text']}" for row in packet["source_blocks"]
    )
    user = (
        "MULTI_TARGET_PACKET_JSON\n"
        + json.dumps(
            {
                "packet_id": packet["packet_id"],
                "chapter_id": packet["chapter_id"],
                "review_items": packet["review_items"],
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n\nENGLISH_SOURCE_BLOCKS\n"
        + blocks
    )
    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": user},
    ]


def user_payload_sha256(messages: Sequence[Mapping[str, str]]) -> str:
    users = [
        str(row.get("content") or "")
        for row in messages
        if row.get("role") == "user"
    ]
    if len(users) != 1:
        raise MultiTargetContractError("Expected exactly one user message")
    return sha256(users[0].encode("utf-8")).hexdigest().upper()


def parse_response_json(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        parsed = value
    elif isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError as exc:
            raise MultiTargetContractError(str(exc)) from exc
    else:
        raise MultiTargetContractError(
            "Response is neither JSON text nor an object"
        )
    if not isinstance(parsed, dict):
        raise MultiTargetContractError("Response top level must be an object")
    if set(parsed) != {"packet_id", "decisions"}:
        raise MultiTargetContractError(
            "Top-level keys must be exactly ['decisions', 'packet_id']"
        )
    if not isinstance(parsed["decisions"], list):
        raise MultiTargetContractError("decisions must be a list")
    return dict(parsed)


def validate_output(
    parsed: Mapping[str, Any], *, packet: Mapping[str, Any]
) -> MultiTargetValidation:
    _validate_packet(packet)
    packet_id = str(packet["packet_id"])
    items = {
        str(row["candidate_id"]): dict(row) for row in packet["review_items"]
    }
    errors: list[str] = []
    if parsed.get("packet_id") != packet_id:
        errors.append("packet_id does not match the supplied request")

    decisions: list[MultiTargetDecision] = []
    seen: set[str] = set()
    duplicates: set[str] = set()
    for index, raw in enumerate(parsed.get("decisions") or []):
        if not isinstance(raw, dict):
            errors.append(f"decisions[{index}] must be an object")
            continue
        candidate_id = raw.get("candidate_id")
        if not isinstance(candidate_id, str) or candidate_id not in items:
            errors.append(f"decisions[{index}] has an unknown candidate_id")
            continue
        if candidate_id in seen:
            duplicates.add(candidate_id)
            errors.append(f"candidate {candidate_id} is decided more than once")
            continue
        seen.add(candidate_id)
        try:
            decisions.append(
                _validate_decision(raw, index=index, item=items[candidate_id])
            )
        except MultiTargetContractError as exc:
            errors.append(str(exc))

    missing = sorted(set(items) - seen)
    if missing:
        errors.append("Response does not exact-cover supplied candidate IDs")
    return MultiTargetValidation(
        packet_id=packet_id,
        decisions=tuple(decisions),
        errors=tuple(errors),
        missing_candidate_ids=tuple(missing),
        duplicate_candidate_ids=tuple(sorted(duplicates)),
    )


def _validate_decision(
    raw: Mapping[str, Any], *, index: int, item: Mapping[str, Any]
) -> MultiTargetDecision:
    required = {
        "candidate_id",
        "action",
        "target_dispositions",
        "evidence_block_ids",
        "rationale",
        "pending_reason",
    }
    if set(raw) != required:
        raise MultiTargetContractError(f"decisions[{index}] has invalid keys")
    action = raw.get("action")
    if action not in {"resolve", "pending"}:
        raise MultiTargetContractError(f"decisions[{index}].action is invalid")
    rationale = raw.get("rationale")
    if not isinstance(rationale, str) or not rationale.strip():
        raise MultiTargetContractError(
            f"decisions[{index}].rationale is required"
        )
    pending_reason = raw.get("pending_reason")
    if action == "pending":
        if not isinstance(pending_reason, str) or not pending_reason.strip():
            raise MultiTargetContractError(
                f"decisions[{index}].pending_reason is required"
            )
    elif pending_reason is not None:
        raise MultiTargetContractError(
            f"decisions[{index}] resolved action requires null pending_reason"
        )

    dispositions_raw = raw.get("target_dispositions")
    if not isinstance(dispositions_raw, list):
        raise MultiTargetContractError(
            f"decisions[{index}].target_dispositions must be a list"
        )
    supplied_targets = {
        _normalize_text(str(row["target_vi"])): str(row["target_vi"])
        for row in item["target_proposals"]
    }
    seen_targets: set[str] = set()
    dispositions: list[TargetDisposition] = []
    for target_index, row in enumerate(dispositions_raw):
        prefix = f"decisions[{index}].target_dispositions[{target_index}]"
        if not isinstance(row, dict) or set(row) != {
            "target_vi",
            "disposition",
            "applicability",
        }:
            raise MultiTargetContractError(f"{prefix} has invalid keys")
        target = row.get("target_vi")
        if not isinstance(target, str) or _normalize_text(target) not in supplied_targets:
            raise MultiTargetContractError(
                f"{prefix}.target_vi is not a supplied B2 target"
            )
        target_key = _normalize_text(target)
        if target_key in seen_targets:
            raise MultiTargetContractError(
                f"decisions[{index}] repeats a target disposition"
            )
        seen_targets.add(target_key)
        disposition = row.get("disposition")
        if disposition not in {"canonical", "alternative", "reject", "pending"}:
            raise MultiTargetContractError(f"{prefix}.disposition is invalid")
        applicability = row.get("applicability")
        if disposition == "alternative":
            if not isinstance(applicability, str) or not applicability.strip():
                raise MultiTargetContractError(
                    f"{prefix}.applicability is required for an alternative"
                )
            cleaned_applicability: str | None = applicability.strip()
        else:
            if applicability is not None:
                raise MultiTargetContractError(
                    f"{prefix}.applicability must be null"
                )
            cleaned_applicability = None
        dispositions.append(
            TargetDisposition(
                target_vi=supplied_targets[target_key],
                disposition=str(disposition),
                applicability=cleaned_applicability,
            )
        )
    if seen_targets != set(supplied_targets):
        raise MultiTargetContractError(
            f"decisions[{index}] does not exact-cover supplied targets"
        )
    disposition_values = [row.disposition for row in dispositions]
    if action == "pending":
        if set(disposition_values) != {"pending"}:
            raise MultiTargetContractError(
                f"decisions[{index}] pending must mark every target pending"
            )
    elif (
        disposition_values.count("canonical") != 1
        or "pending" in disposition_values
    ):
        raise MultiTargetContractError(
            f"decisions[{index}] resolved action requires one canonical target"
        )

    evidence = raw.get("evidence_block_ids")
    allowed_evidence = {str(value) for value in item["evidence_block_ids"]}
    if (
        not isinstance(evidence, list)
        or not evidence
        or not all(isinstance(value, str) for value in evidence)
        or len(evidence) != len(set(evidence))
        or not set(evidence).issubset(allowed_evidence)
    ):
        raise MultiTargetContractError(
            f"decisions[{index}].evidence_block_ids is invalid"
        )
    return MultiTargetDecision(
        candidate_id=str(raw["candidate_id"]),
        action=str(action),
        target_dispositions=tuple(dispositions),
        evidence_block_ids=tuple(evidence),
        rationale=rationale.strip(),
        pending_reason=(pending_reason.strip() if isinstance(pending_reason, str) else None),
    )


def _validate_packet(packet: Mapping[str, Any]) -> None:
    if set(packet) != {"packet_id", "chapter_id", "review_items", "source_blocks"}:
        raise MultiTargetContractError("Packet has invalid top-level keys")
    if not isinstance(packet.get("packet_id"), str) or not packet["packet_id"]:
        raise MultiTargetContractError("packet_id is invalid")
    if not isinstance(packet.get("chapter_id"), str) or not packet["chapter_id"]:
        raise MultiTargetContractError("chapter_id is invalid")
    items = packet.get("review_items")
    if not isinstance(items, list) or not items:
        raise MultiTargetContractError("review_items must be a non-empty list")
    blocks = packet.get("source_blocks")
    if not isinstance(blocks, list) or not blocks:
        raise MultiTargetContractError("source_blocks must be a non-empty list")
    block_ids: set[str] = set()
    for row in blocks:
        if not isinstance(row, dict) or set(row) != {"block_id", "text"}:
            raise MultiTargetContractError("source_blocks row has invalid keys")
        block_id = row.get("block_id")
        if (
            not isinstance(block_id, str)
            or not block_id
            or block_id in block_ids
            or not isinstance(row.get("text"), str)
        ):
            raise MultiTargetContractError("source_blocks row is invalid")
        block_ids.add(block_id)

    candidate_ids: set[str] = set()
    for item in items:
        required = {
            "candidate_id",
            "canonical_source",
            "surfaces",
            "target_proposals",
            "directive",
            "evidence_block_ids",
            "reason_codes",
        }
        if not isinstance(item, dict) or set(item) != required:
            raise MultiTargetContractError("review item has invalid keys")
        candidate_id = item.get("candidate_id")
        if (
            not isinstance(candidate_id, str)
            or not candidate_id
            or candidate_id in candidate_ids
        ):
            raise MultiTargetContractError("review item candidate_id is invalid")
        candidate_ids.add(candidate_id)
        if not isinstance(item.get("canonical_source"), str):
            raise MultiTargetContractError("review item canonical_source is invalid")
        surfaces = item.get("surfaces")
        if not isinstance(surfaces, list) or not surfaces or not all(
            isinstance(value, str) and value for value in surfaces
        ):
            raise MultiTargetContractError("review item surfaces are invalid")
        proposals = item.get("target_proposals")
        if not isinstance(proposals, list) or len(proposals) < 2:
            raise MultiTargetContractError(
                "review item must have multiple target proposals"
            )
        target_keys: set[str] = set()
        for proposal in proposals:
            if not isinstance(proposal, dict) or set(proposal) != {
                "target_vi",
                "applicability",
            }:
                raise MultiTargetContractError("target proposal has invalid keys")
            target = proposal.get("target_vi")
            applicability = proposal.get("applicability")
            if (
                not isinstance(target, str)
                or not target.strip()
                or _normalize_text(target) in target_keys
                or not (applicability is None or isinstance(applicability, str))
            ):
                raise MultiTargetContractError("target proposal is invalid")
            target_keys.add(_normalize_text(target))
        if len(target_keys) < 2:
            raise MultiTargetContractError(
                "review item target proposals are not distinct"
            )
        if item.get("directive") not in {"translate", "preserve", "contextual"}:
            raise MultiTargetContractError("review item directive is invalid")
        evidence = item.get("evidence_block_ids")
        if (
            not isinstance(evidence, list)
            or not evidence
            or len(evidence) != len(set(evidence))
            or not all(value in block_ids for value in evidence)
        ):
            raise MultiTargetContractError("review item evidence is invalid")
        if item.get("reason_codes") != ["multiple_distinct_target_proposals"]:
            raise MultiTargetContractError("review item reason_codes are invalid")


def _normalize_text(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value).casefold()
    return " ".join(normalized.split())
