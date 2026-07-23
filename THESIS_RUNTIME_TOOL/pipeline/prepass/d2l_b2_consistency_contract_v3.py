from __future__ import annotations

import json
import re
from dataclasses import dataclass
from hashlib import sha256
from typing import Any, Mapping, Sequence


PROMPT_VERSION = "d2l_b2_consistency_admission_v3"
RESPONSE_SCHEMA_VERSION = "d2l_b2_consistency_admission_schema_v3"
VALIDATOR_VERSION = "d2l_b2_consistency_admission_validator_v3_2"

SYSTEM_PROMPT = r"""You are Terminology Builder 2 for an autonomous
English-to-Vietnamese technical-book translation pipeline.

Prompt version: d2l_b2_consistency_admission_v3.

INPUT
You receive one bounded English source packet and scanner-proposed candidate
expressions. They are evidence, not glossary entries. Grouped surfaces differ
only under supplied exact normalization. Different candidate IDs remain
distinct.

DECISION
Return one row for every candidate_id. Ask whether the expression needs a
persistent book-level translation rule to prevent material inconsistency or
mistranslation, or whether a competent Translator can render it safely from
local sentence context.

- admit: persistent control is affirmatively needed for a stable lexical unit;
- reject: local translation is sufficient, including technical but ordinary
  prose, procedure detail, scenario detail, heading wrappers, and over-wide
  expressions;
- review: supplied evidence cannot safely settle admission or translation.

Technicality, frequency, and subject relevance alone do not justify admit.
Without an affirmative need for persistent control, use reject. Use review only
for real evidence insufficiency.

ADMIT PAYLOAD
- Copy canonical_source from supplied surfaces and give one primary target.
- directive is translate, preserve, or contextual.
- preserve requires primary_target_vi to equal canonical_source exactly.
- translate/preserve require primary_use=null and alternates=[].
- contextual requires primary_use. Add at most two alternates only when source
  blocks prove distinct use classes needing different Vietnamese renderings.
- Each alternate needs a distinct target, a use_when rule naming the source-use
  class and operational condition, and its own supplied evidence IDs. A
  stylistic synonym is not an alternate.

EVIDENCE AND BOUNDARIES
- Read the supplied English blocks; cite only candidate evidence_block_ids.
- Partial evidence means other occurrences exist outside this packet.
- Keep rationales short and source-grounded.
- Never add candidates, merge IDs, rank, assign confidence, or use an external
  glossary, community gold, memory pack, expected answer, or outside source.
- Do not omit any supplied candidate or publish a book-level glossary.

Return JSON only with exactly this shape:
{
  "packet_id": "supplied packet id",
  "decisions": [
    {
      "candidate_id": "supplied candidate id",
      "decision": "admit|reject|review",
      "canonical_source": "supplied candidate surface or null",
      "directive": "translate|preserve|contextual|null",
      "primary_target_vi": "one Vietnamese rendering or null",
      "primary_use": "condition or null",
      "alternates": [
        {
          "target_vi": "distinct Vietnamese rendering",
          "use_when": "source-use class and operational condition",
          "evidence_block_ids": ["supplied evidence block id"]
        }
      ],
      "evidence_block_ids": ["supplied evidence block id"],
      "rationale": "short source-grounded reason"
    }
  ]
}
"""

ALTERNATE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "target_vi",
        "use_when",
        "evidence_block_ids",
    ],
    "properties": {
        "target_vi": {"type": "string"},
        "use_when": {"type": "string"},
        "evidence_block_ids": {
            "type": "array",
            "items": {"type": "string"},
        },
    },
}

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
                            "decision",
                            "canonical_source",
                            "directive",
                            "primary_target_vi",
                            "primary_use",
                            "alternates",
                            "evidence_block_ids",
                            "rationale",
                        ],
                        "properties": {
                            "candidate_id": {"type": "string"},
                            "decision": {
                                "type": "string",
                                "enum": ["admit", "reject", "review"],
                            },
                            "canonical_source": {"type": ["string", "null"]},
                            "directive": {
                                "type": ["string", "null"],
                                "enum": [
                                    "translate",
                                    "preserve",
                                    "contextual",
                                    None,
                                ],
                            },
                            "primary_target_vi": {
                                "type": ["string", "null"]
                            },
                            "primary_use": {
                                "type": ["string", "null"]
                            },
                            "alternates": {
                                "type": "array",
                                "maxItems": 2,
                                "items": ALTERNATE_SCHEMA,
                            },
                            "evidence_block_ids": {
                                "type": "array",
                                "items": {"type": "string"},
                            },
                            "rationale": {"type": "string"},
                        },
                    },
                },
            },
        },
    },
}


class B2V3ContractError(ValueError):
    pass


@dataclass(frozen=True)
class AlternateTarget:
    target_vi: str
    use_when: str
    evidence_block_ids: tuple[str, ...]


@dataclass(frozen=True)
class B2V3Decision:
    candidate_id: str
    decision: str
    canonical_source: str | None
    directive: str | None
    primary_target_vi: str | None
    primary_use: str | None
    alternates: tuple[AlternateTarget, ...]
    evidence_block_ids: tuple[str, ...]
    rationale: str


@dataclass(frozen=True)
class B2V3Validation:
    packet_id: str
    decisions: tuple[B2V3Decision, ...]
    errors: tuple[str, ...]
    missing_candidate_ids: tuple[str, ...]
    duplicate_candidate_ids: tuple[str, ...]
    normalization_warnings: tuple[str, ...]


def prompt_sha256() -> str:
    return sha256(SYSTEM_PROMPT.encode("utf-8")).hexdigest().upper()


def schema_sha256() -> str:
    rendered = json.dumps(
        RESPONSE_FORMAT,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return sha256(rendered.encode("utf-8")).hexdigest().upper()


def render_messages(packet: Mapping[str, Any]) -> list[dict[str, str]]:
    _validate_packet(packet)
    rendered_blocks = "\n".join(
        f"[{row['block_id']}] {row['text']}" for row in packet["source_blocks"]
    )
    user = (
        "CANDIDATE_PACKET_JSON\n"
        + json.dumps(
            {
                "packet_id": packet["packet_id"],
                "chapter_id": packet["chapter_id"],
                "candidates": packet["candidates"],
            },
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


def user_payload_sha256(messages: Sequence[Mapping[str, str]]) -> str:
    users = [
        str(row.get("content") or "")
        for row in messages
        if row.get("role") == "user"
    ]
    if len(users) != 1:
        raise B2V3ContractError("Expected exactly one user message")
    return sha256(users[0].encode("utf-8")).hexdigest().upper()


def parse_response_json(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        parsed = value
    elif isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError as exc:
            raise B2V3ContractError(str(exc)) from exc
    else:
        raise B2V3ContractError("Response is neither JSON text nor an object")
    if not isinstance(parsed, dict):
        raise B2V3ContractError("Response top level must be an object")
    if set(parsed) != {"packet_id", "decisions"}:
        raise B2V3ContractError(
            "Top-level keys must be exactly ['decisions', 'packet_id']"
        )
    if not isinstance(parsed["decisions"], list):
        raise B2V3ContractError("decisions must be a list")
    return dict(parsed)


def validate_output(
    parsed: Mapping[str, Any], *, packet: Mapping[str, Any]
) -> B2V3Validation:
    _validate_packet(packet)
    expected_packet_id = str(packet["packet_id"])
    rendered_blocks = {
        str(row["block_id"]): str(row["text"])
        for row in packet["source_blocks"]
    }
    candidates: dict[str, dict[str, Any]] = {}
    for row in packet["candidates"]:
        candidate = dict(row)
        candidate["_rendered_support_ids"] = sorted(
            block_id
            for block_id, text in rendered_blocks.items()
            if block_id in set(candidate["source_block_ids"])
            or any(
                _surface_occurs_in_text(surface, text)
                for surface in candidate["surfaces"]
            )
        )
        candidates[str(candidate["candidate_id"])] = candidate
    errors: list[str] = []
    if parsed.get("packet_id") != expected_packet_id:
        errors.append("packet_id does not match the supplied request")

    decisions: list[B2V3Decision] = []
    normalization_warnings: list[str] = []
    seen: set[str] = set()
    duplicates: set[str] = set()
    for index, raw in enumerate(parsed.get("decisions") or []):
        if not isinstance(raw, dict):
            errors.append(f"decisions[{index}] must be an object")
            continue
        candidate_id = raw.get("candidate_id")
        if not isinstance(candidate_id, str) or candidate_id not in candidates:
            errors.append(f"decisions[{index}] has an unknown candidate_id")
            continue
        if candidate_id in seen:
            duplicates.add(candidate_id)
            errors.append(f"candidate {candidate_id} is decided more than once")
            continue
        seen.add(candidate_id)
        try:
            decision_warnings: list[str] = []
            validated = _validate_decision(
                raw,
                index=index,
                candidate=candidates[candidate_id],
                normalization_warnings=decision_warnings,
            )
            decisions.append(validated)
            normalization_warnings.extend(decision_warnings)
        except B2V3ContractError as exc:
            errors.append(str(exc))

    missing = sorted(set(candidates) - seen)
    if missing:
        errors.append("Response does not exact-cover supplied candidate IDs")
    return B2V3Validation(
        packet_id=expected_packet_id,
        decisions=tuple(decisions),
        errors=tuple(errors),
        missing_candidate_ids=tuple(missing),
        duplicate_candidate_ids=tuple(sorted(duplicates)),
        normalization_warnings=tuple(normalization_warnings),
    )


def _validate_decision(
    raw: Mapping[str, Any],
    *,
    index: int,
    candidate: Mapping[str, Any],
    normalization_warnings: list[str],
) -> B2V3Decision:
    required = {
        "candidate_id",
        "decision",
        "canonical_source",
        "directive",
        "primary_target_vi",
        "primary_use",
        "alternates",
        "evidence_block_ids",
        "rationale",
    }
    if set(raw) != required:
        raise B2V3ContractError(f"decisions[{index}] has invalid keys")
    decision = raw.get("decision")
    if decision not in {"admit", "reject", "review"}:
        raise B2V3ContractError(f"decisions[{index}].decision is invalid")
    rationale = _required_text(
        raw.get("rationale"),
        label=f"decisions[{index}].rationale",
        maximum=360,
    )
    sampled_evidence = set(candidate["evidence_block_ids"])
    allowed_evidence = set(candidate["_rendered_support_ids"])
    evidence = _evidence_ids(
        raw.get("evidence_block_ids"),
        allowed=allowed_evidence,
        label=f"decisions[{index}].evidence_block_ids",
    )
    _record_extended_evidence_warnings(
        evidence,
        sampled=sampled_evidence,
        label=f"decisions[{index}].evidence_block_ids",
        normalization_warnings=normalization_warnings,
    )

    canonical = raw.get("canonical_source")
    directive = raw.get("directive")
    primary = raw.get("primary_target_vi")
    primary_use = raw.get("primary_use")
    alternates_raw = raw.get("alternates")
    alternates: list[AlternateTarget] = []

    if decision != "admit":
        if any(
            value is not None
            for value in (
                canonical,
                directive,
                primary,
                primary_use,
            )
        ) or alternates_raw != []:
            raise B2V3ContractError(
                f"decisions[{index}] {decision} must not carry translation payload"
            )
    else:
        if canonical not in candidate["surfaces"]:
            raise B2V3ContractError(
                f"decisions[{index}].canonical_source is not a supplied surface"
            )
        if directive not in {"translate", "preserve", "contextual"}:
            raise B2V3ContractError(f"decisions[{index}].directive is invalid")
        primary = _required_text(
            primary,
            label=f"decisions[{index}].primary_target_vi",
            maximum=160,
        )
        if directive == "preserve" and primary != canonical:
            raise B2V3ContractError(
                f"decisions[{index}] preserve target must equal canonical_source"
            )
        if directive in {"translate", "preserve"}:
            if primary_use is not None or alternates_raw != []:
                raise B2V3ContractError(
                    f"decisions[{index}] non-contextual admit cannot carry primary_use or alternates"
                )
        else:
            primary_use = _required_text(
                primary_use,
                label=f"decisions[{index}].primary_use",
                maximum=240,
            )
            alternates = _validate_alternates(
                alternates_raw,
                index=index,
                primary_target=primary,
                allowed_evidence=allowed_evidence,
                sampled_evidence=sampled_evidence,
                normalization_warnings=normalization_warnings,
            )

    return B2V3Decision(
        candidate_id=str(raw["candidate_id"]),
        decision=str(decision),
        canonical_source=(str(canonical) if canonical is not None else None),
        directive=(str(directive) if directive is not None else None),
        primary_target_vi=(str(primary) if primary is not None else None),
        primary_use=(
            str(primary_use)
            if primary_use is not None
            else None
        ),
        alternates=tuple(alternates),
        evidence_block_ids=tuple(evidence),
        rationale=rationale,
    )


def _validate_alternates(
    value: Any,
    *,
    index: int,
    primary_target: str,
    allowed_evidence: set[str],
    sampled_evidence: set[str],
    normalization_warnings: list[str],
) -> list[AlternateTarget]:
    if not isinstance(value, list) or len(value) > 2:
        raise B2V3ContractError(
            f"decisions[{index}].alternates must contain 0..2 rows"
        )
    seen_targets = {primary_target.casefold()}
    rows: list[AlternateTarget] = []
    required = {
        "target_vi",
        "use_when",
        "evidence_block_ids",
    }
    for secondary_index, raw in enumerate(value):
        label = f"decisions[{index}].alternates[{secondary_index}]"
        if not isinstance(raw, dict) or set(raw) != required:
            raise B2V3ContractError(f"{label} has invalid keys")
        target = _required_text(raw.get("target_vi"), label=f"{label}.target_vi", maximum=160)
        target_key = target.casefold()
        use_when = _required_text(
            raw.get("use_when"),
            label=f"{label}.use_when",
            maximum=240,
        )
        evidence = _evidence_ids(
            raw.get("evidence_block_ids"),
            allowed=allowed_evidence,
            label=f"{label}.evidence_block_ids",
        )
        _record_extended_evidence_warnings(
            evidence,
            sampled=sampled_evidence,
            label=f"{label}.evidence_block_ids",
            normalization_warnings=normalization_warnings,
        )
        if target_key in seen_targets:
            normalization_warnings.append(
                f"{label}.target_vi duplicated an earlier target and was dropped"
            )
            continue
        seen_targets.add(target_key)
        rows.append(
            AlternateTarget(
                target_vi=target,
                use_when=use_when,
                evidence_block_ids=tuple(evidence),
            )
        )
    return rows


def _record_extended_evidence_warnings(
    evidence: list[str],
    *,
    sampled: set[str],
    label: str,
    normalization_warnings: list[str],
) -> None:
    for block_id in evidence:
        if block_id not in sampled:
            normalization_warnings.append(
                f"{label} used rendered candidate occurrence {block_id} outside the sampled evidence subset"
            )


def _surface_occurs_in_text(surface: str, text: str) -> bool:
    normalized_surface = " ".join(surface.casefold().split())
    if not normalized_surface:
        return False
    pattern = re.escape(normalized_surface).replace(r"\ ", r"\s+")
    if normalized_surface[0].isalnum():
        pattern = r"(?<!\w)" + pattern
    if normalized_surface[-1].isalnum():
        pattern = pattern + r"(?!\w)"
    normalized_text = text.casefold()
    return re.search(pattern, normalized_text) is not None


def _required_text(value: Any, *, label: str, maximum: int) -> str:
    if not isinstance(value, str) or not value.strip():
        raise B2V3ContractError(f"{label} is invalid")
    rendered = value.strip()
    if len(rendered) > maximum:
        raise B2V3ContractError(f"{label} exceeds {maximum} characters")
    return rendered


def _evidence_ids(value: Any, *, allowed: set[str], label: str) -> list[str]:
    if not isinstance(value, list) or not value or not all(
        isinstance(item, str) and item for item in value
    ):
        raise B2V3ContractError(f"{label} is invalid")
    if len(value) != len(set(value)):
        raise B2V3ContractError(f"{label} contains duplicates")
    if not set(value).issubset(allowed):
        raise B2V3ContractError(f"{label} cites evidence outside the candidate packet")
    return list(value)


def _validate_packet(packet: Mapping[str, Any]) -> None:
    required = {"packet_id", "chapter_id", "candidates", "source_blocks"}
    if set(packet) != required:
        raise B2V3ContractError("Packet has an invalid top-level shape")
    if not isinstance(packet["packet_id"], str) or not packet["packet_id"]:
        raise B2V3ContractError("packet_id is invalid")
    if not isinstance(packet["chapter_id"], str) or not packet["chapter_id"]:
        raise B2V3ContractError("chapter_id is invalid")
    candidates = packet["candidates"]
    source_blocks = packet["source_blocks"]
    if not isinstance(candidates, list) or not candidates:
        raise B2V3ContractError("candidates must be a non-empty list")
    if not isinstance(source_blocks, list) or not source_blocks:
        raise B2V3ContractError("source_blocks must be a non-empty list")

    block_ids: set[str] = set()
    for index, row in enumerate(source_blocks):
        if not isinstance(row, dict) or set(row) != {"block_id", "text"}:
            raise B2V3ContractError(f"source_blocks[{index}] has invalid shape")
        block_id = row.get("block_id")
        text = row.get("text")
        if not isinstance(block_id, str) or not block_id or block_id in block_ids:
            raise B2V3ContractError(f"source_blocks[{index}].block_id is invalid")
        if not isinstance(text, str) or not text:
            raise B2V3ContractError(f"source_blocks[{index}].text is invalid")
        block_ids.add(block_id)

    allowed_candidate_keys = {
        "candidate_id",
        "normalized_surface",
        "surfaces",
        "source_block_ids",
        "window_ids",
        "evidence_block_ids",
        "evidence_complete",
        "support_block_count",
        "window_count",
    }
    candidate_ids: set[str] = set()
    for index, row in enumerate(candidates):
        if not isinstance(row, dict) or set(row) != allowed_candidate_keys:
            raise B2V3ContractError(f"candidates[{index}] has invalid shape")
        candidate_id = row.get("candidate_id")
        if not isinstance(candidate_id, str) or not candidate_id or candidate_id in candidate_ids:
            raise B2V3ContractError(f"candidates[{index}].candidate_id is invalid")
        candidate_ids.add(candidate_id)
        surfaces = row.get("surfaces")
        support = row.get("source_block_ids")
        evidence = row.get("evidence_block_ids")
        if not isinstance(surfaces, list) or not surfaces or not all(
            isinstance(item, str) and item for item in surfaces
        ):
            raise B2V3ContractError(f"candidates[{index}].surfaces is invalid")
        if not isinstance(support, list) or not support:
            raise B2V3ContractError(f"candidates[{index}].source_block_ids is invalid")
        if not isinstance(evidence, list) or not evidence:
            raise B2V3ContractError(f"candidates[{index}].evidence_block_ids is invalid")
        if not set(evidence).issubset(set(support)):
            raise B2V3ContractError(f"candidates[{index}] evidence is outside candidate support")
        if not set(evidence).issubset(block_ids):
            raise B2V3ContractError(f"candidates[{index}] evidence is absent from packet source")
