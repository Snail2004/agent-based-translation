from __future__ import annotations

import json
from dataclasses import dataclass
from hashlib import sha256
from typing import Any, Mapping, Sequence


PROMPT_VERSION = "d2l_b2_admission_translation_v2"
RESPONSE_SCHEMA_VERSION = "d2l_b2_admission_translation_schema_v2"
VALIDATOR_VERSION = "d2l_b2_admission_translation_validator_v2_1"

SYSTEM_PROMPT = r"""You are Terminology Builder 2 for an autonomous
English-to-Vietnamese technical-book translation pipeline.

Prompt version: d2l_b2_admission_translation_v2.

INPUT
You receive one bounded English source packet and candidate expressions found
by an earlier scanner. Candidate rows are evidence, not admitted glossary
entries. Surface variants grouped in one row differ only under supplied exact
normalization; do not infer equivalence between different candidate IDs.

YOUR ONLY JOB
Return exactly one decision for every supplied candidate_id:
- admit: a reusable technical lexical unit whose translation should be managed;
- reject: ordinary prose, disposable procedure, scenario detail, heading
  wrapper, or an over-wide expression rather than a stable lexical unit;
- review: supplied evidence cannot safely settle admission or translation.

For admit only:
- select canonical_source verbatim from that candidate's supplied surfaces;
- propose one to three concise Vietnamese renderings;
- use translate, preserve, or contextual as the directive;
- describe applicability only when target choice depends on source use.

EVIDENCE RULES
- Read the supplied English source blocks yourself.
- Cite only evidence_block_ids supplied for that candidate.
- A partial evidence manifest means additional occurrences exist outside this
  packet. Do not claim that the packet represents every use.
- Keep rationales short and source-grounded.

FORBIDDEN WORK
- Do not add an omitted candidate or invent source text.
- Do not merge candidate IDs, rank candidates, or assign confidence.
- Do not use an external glossary, community gold, memory pack, expected
  answer, or outside source.
- Do not publish a book-level glossary.
- Do not omit a supplied candidate, even when it is clearly rejectable.

Return JSON only with exactly this shape:
{
  "packet_id": "supplied packet id",
  "decisions": [
    {
      "candidate_id": "supplied candidate id",
      "decision": "admit|reject|review",
      "canonical_source": "supplied candidate surface or null",
      "target_proposals": [
        {"target_vi": "Vietnamese rendering", "applicability": "condition or null"}
      ],
      "directive": "translate|preserve|contextual|null",
      "evidence_block_ids": ["supplied evidence block id"],
      "rationale": "short source-grounded reason"
    }
  ]
}
"""

RESPONSE_FORMAT: dict[str, Any] = {
    "type": "json_schema",
    "json_schema": {
        "name": "d2l_b2_admission_translation_v2",
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
                            "target_proposals",
                            "directive",
                            "evidence_block_ids",
                            "rationale",
                        ],
                        "properties": {
                            "candidate_id": {"type": "string"},
                            "decision": {
                                "type": "string",
                                "enum": ["admit", "reject", "review"],
                            },
                            "canonical_source": {
                                "type": ["string", "null"]
                            },
                            "target_proposals": {
                                "type": "array",
                                "maxItems": 3,
                                "items": {
                                    "type": "object",
                                    "additionalProperties": False,
                                    "required": ["target_vi", "applicability"],
                                    "properties": {
                                        "target_vi": {"type": "string"},
                                        "applicability": {
                                            "type": ["string", "null"]
                                        },
                                    },
                                },
                            },
                            "directive": {
                                "type": ["string", "null"],
                                "enum": [
                                    "translate",
                                    "preserve",
                                    "contextual",
                                    None,
                                ],
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


class B2PacketContractError(ValueError):
    pass


@dataclass(frozen=True)
class TargetProposal:
    target_vi: str
    applicability: str | None


@dataclass(frozen=True)
class B2Decision:
    candidate_id: str
    decision: str
    canonical_source: str | None
    target_proposals: tuple[TargetProposal, ...]
    directive: str | None
    evidence_block_ids: tuple[str, ...]
    rationale: str


@dataclass(frozen=True)
class B2Validation:
    packet_id: str
    decisions: tuple[B2Decision, ...]
    errors: tuple[str, ...]
    missing_candidate_ids: tuple[str, ...]
    duplicate_candidate_ids: tuple[str, ...]
    normalization_warnings: tuple[str, ...]


def prompt_sha256() -> str:
    return sha256(SYSTEM_PROMPT.encode("utf-8")).hexdigest().upper()


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
    users = [str(row.get("content") or "") for row in messages if row.get("role") == "user"]
    if len(users) != 1:
        raise B2PacketContractError("Expected exactly one user message")
    return sha256(users[0].encode("utf-8")).hexdigest().upper()


def parse_response_json(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        parsed = value
    elif isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError as exc:
            raise B2PacketContractError(str(exc)) from exc
    else:
        raise B2PacketContractError("Response is neither JSON text nor an object")
    if not isinstance(parsed, dict):
        raise B2PacketContractError("Response top level must be an object")
    if set(parsed) != {"packet_id", "decisions"}:
        raise B2PacketContractError(
            "Top-level keys must be exactly ['decisions', 'packet_id']"
        )
    if not isinstance(parsed["decisions"], list):
        raise B2PacketContractError("decisions must be a list")
    return dict(parsed)


def validate_output(
    parsed: Mapping[str, Any], *, packet: Mapping[str, Any]
) -> B2Validation:
    _validate_packet(packet)
    expected_packet_id = str(packet["packet_id"])
    candidates = {
        str(row["candidate_id"]): dict(row) for row in packet["candidates"]
    }
    errors: list[str] = []
    if parsed.get("packet_id") != expected_packet_id:
        errors.append("packet_id does not match the supplied request")

    decisions: list[B2Decision] = []
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
        normalized_raw = dict(raw)
        if (
            normalized_raw.get("decision") in {"reject", "review"}
            and normalized_raw.get("canonical_source")
            in candidates[candidate_id]["surfaces"]
            and normalized_raw.get("directive") is None
            and normalized_raw.get("target_proposals") == []
        ):
            normalized_raw["canonical_source"] = None
            normalization_warnings.append(
                f"decisions[{index}] normalized redundant canonical_source "
                f"for {normalized_raw['decision']}"
            )
        try:
            decisions.append(
                _validate_decision(
                    normalized_raw,
                    index=index,
                    candidate=candidates[candidate_id],
                )
            )
        except B2PacketContractError as exc:
            errors.append(str(exc))

    missing = sorted(set(candidates) - seen)
    if missing:
        errors.append("Response does not exact-cover supplied candidate IDs")
    return B2Validation(
        packet_id=expected_packet_id,
        decisions=tuple(decisions),
        errors=tuple(errors),
        missing_candidate_ids=tuple(missing),
        duplicate_candidate_ids=tuple(sorted(duplicates)),
        normalization_warnings=tuple(normalization_warnings),
    )


def _validate_decision(
    raw: Mapping[str, Any], *, index: int, candidate: Mapping[str, Any]
) -> B2Decision:
    required = {
        "candidate_id",
        "decision",
        "canonical_source",
        "target_proposals",
        "directive",
        "evidence_block_ids",
        "rationale",
    }
    if set(raw) != required:
        raise B2PacketContractError(f"decisions[{index}] has invalid keys")
    candidate_id = str(raw["candidate_id"])
    decision = raw.get("decision")
    if decision not in {"admit", "reject", "review"}:
        raise B2PacketContractError(f"decisions[{index}].decision is invalid")
    rationale = raw.get("rationale")
    if not isinstance(rationale, str) or not rationale.strip():
        raise B2PacketContractError(f"decisions[{index}].rationale is invalid")

    evidence = raw.get("evidence_block_ids")
    if not isinstance(evidence, list) or not evidence or not all(
        isinstance(value, str) and value for value in evidence
    ):
        raise B2PacketContractError(
            f"decisions[{index}].evidence_block_ids is invalid"
        )
    if len(evidence) != len(set(evidence)):
        raise B2PacketContractError(
            f"decisions[{index}].evidence_block_ids contains duplicates"
        )
    if not set(evidence).issubset(set(candidate["evidence_block_ids"])):
        raise B2PacketContractError(
            f"decisions[{index}] cites evidence outside the candidate packet"
        )

    canonical = raw.get("canonical_source")
    directive = raw.get("directive")
    proposals_raw = raw.get("target_proposals")
    proposals: list[TargetProposal] = []
    if decision == "admit":
        if canonical not in candidate["surfaces"]:
            raise B2PacketContractError(
                f"decisions[{index}].canonical_source is not a supplied surface"
            )
        if directive not in {"translate", "preserve", "contextual"}:
            raise B2PacketContractError(f"decisions[{index}].directive is invalid")
        if not isinstance(proposals_raw, list) or not 1 <= len(proposals_raw) <= 3:
            raise B2PacketContractError(
                f"decisions[{index}].target_proposals must contain 1..3 rows"
            )
        seen_targets: set[str] = set()
        for proposal_index, proposal in enumerate(proposals_raw):
            if not isinstance(proposal, dict) or set(proposal) != {
                "target_vi",
                "applicability",
            }:
                raise B2PacketContractError(
                    f"decisions[{index}].target_proposals[{proposal_index}] is invalid"
                )
            target = proposal.get("target_vi")
            applicability = proposal.get("applicability")
            if not isinstance(target, str) or not target.strip():
                raise B2PacketContractError(
                    f"decisions[{index}].target_proposals[{proposal_index}].target_vi is invalid"
                )
            target_key = target.strip().casefold()
            if target_key in seen_targets:
                raise B2PacketContractError(
                    f"decisions[{index}].target_proposals repeats a target"
                )
            seen_targets.add(target_key)
            if applicability is not None and (
                not isinstance(applicability, str) or not applicability.strip()
            ):
                raise B2PacketContractError(
                    f"decisions[{index}].target_proposals[{proposal_index}].applicability is invalid"
                )
            proposals.append(
                TargetProposal(
                    target_vi=target.strip(),
                    applicability=(
                        applicability.strip()
                        if isinstance(applicability, str)
                        else None
                    ),
                )
            )
    else:
        if canonical is not None or directive is not None or proposals_raw != []:
            raise B2PacketContractError(
                f"decisions[{index}] {decision} must not carry translation payload"
            )

    return B2Decision(
        candidate_id=candidate_id,
        decision=str(decision),
        canonical_source=(str(canonical) if canonical is not None else None),
        target_proposals=tuple(proposals),
        directive=(str(directive) if directive is not None else None),
        evidence_block_ids=tuple(evidence),
        rationale=rationale.strip(),
    )


def _validate_packet(packet: Mapping[str, Any]) -> None:
    required = {"packet_id", "chapter_id", "candidates", "source_blocks"}
    if set(packet) != required:
        raise B2PacketContractError("Packet has an invalid top-level shape")
    if not isinstance(packet["packet_id"], str) or not packet["packet_id"]:
        raise B2PacketContractError("packet_id is invalid")
    if not isinstance(packet["chapter_id"], str) or not packet["chapter_id"]:
        raise B2PacketContractError("chapter_id is invalid")
    candidates = packet["candidates"]
    source_blocks = packet["source_blocks"]
    if not isinstance(candidates, list) or not candidates:
        raise B2PacketContractError("candidates must be a non-empty list")
    if not isinstance(source_blocks, list) or not source_blocks:
        raise B2PacketContractError("source_blocks must be a non-empty list")

    block_ids: set[str] = set()
    for index, row in enumerate(source_blocks):
        if not isinstance(row, dict) or set(row) != {"block_id", "text"}:
            raise B2PacketContractError(f"source_blocks[{index}] has invalid shape")
        block_id = row.get("block_id")
        text = row.get("text")
        if not isinstance(block_id, str) or not block_id or block_id in block_ids:
            raise B2PacketContractError(f"source_blocks[{index}].block_id is invalid")
        if not isinstance(text, str) or not text:
            raise B2PacketContractError(f"source_blocks[{index}].text is invalid")
        block_ids.add(block_id)

    candidate_ids: set[str] = set()
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
    for index, row in enumerate(candidates):
        if not isinstance(row, dict) or set(row) != allowed_candidate_keys:
            raise B2PacketContractError(f"candidates[{index}] has invalid shape")
        candidate_id = row.get("candidate_id")
        if (
            not isinstance(candidate_id, str)
            or not candidate_id
            or candidate_id in candidate_ids
        ):
            raise B2PacketContractError(f"candidates[{index}].candidate_id is invalid")
        candidate_ids.add(candidate_id)
        surfaces = row.get("surfaces")
        evidence_ids = row.get("evidence_block_ids")
        support_ids = row.get("source_block_ids")
        if not isinstance(surfaces, list) or not surfaces or not all(
            isinstance(value, str) and value for value in surfaces
        ):
            raise B2PacketContractError(f"candidates[{index}].surfaces is invalid")
        if not isinstance(support_ids, list) or not support_ids:
            raise B2PacketContractError(
                f"candidates[{index}].source_block_ids is invalid"
            )
        if not isinstance(evidence_ids, list) or not evidence_ids:
            raise B2PacketContractError(
                f"candidates[{index}].evidence_block_ids is invalid"
            )
        if not set(evidence_ids).issubset(set(support_ids)):
            raise B2PacketContractError(
                f"candidates[{index}] evidence is outside candidate support"
            )
        if not set(evidence_ids).issubset(block_ids):
            raise B2PacketContractError(
                f"candidates[{index}] evidence is absent from packet source"
            )
