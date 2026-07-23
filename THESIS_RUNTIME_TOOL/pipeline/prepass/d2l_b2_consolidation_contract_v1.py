from __future__ import annotations

import json
from dataclasses import dataclass
from hashlib import sha256
from typing import Any, Mapping, Sequence


PROMPT_VERSION = "d2l_b2_consolidation_audit_v1"
RESPONSE_SCHEMA_VERSION = "d2l_b2_consolidation_schema_v1"
VALIDATOR_VERSION = "d2l_b2_consolidation_validator_v1_2"

SYSTEM_PROMPT = r"""You are the Terminology Consolidation Auditor for an
autonomous English-to-Vietnamese technical-book translation pipeline.

Prompt version: d2l_b2_consolidation_audit_v1.

INPUT
You receive bounded review components built from already validated Builder 2
decisions and their supplied English source blocks. Component membership is a
retrieval hint, not a claim that its members are equivalent. Shared Vietnamese
proposals, similar spelling, inflection, or source containment are never by
themselves authority to merge.

YOUR ONLY JOB
Return exactly one decision for every supplied component_id:
- merge_all: all member candidates are one reusable lexical entry;
- keep_separate: every member is a distinct lexical entry;
- partition: some members merge while other members remain distinct;
- pending: the supplied evidence cannot safely resolve the component.

For every resolved entry:
- exact-cover its assigned member_candidate_ids;
- copy canonical_source from a supplied surface of those members;
- choose canonical_target_vi from their supplied Builder 2 target proposals;
- choose a directive supplied by those members;
- retain an alternative target only when it has a concise, explicit source-use
  condition; otherwise choose one canonical target or return pending;
- cite only supplied evidence block IDs belonging to those members.

ACTION SHAPES
- merge_all: exactly one resolved entry containing every component member;
- keep_separate: exactly one singleton resolved entry per component member;
- partition: two or more resolved entries that exact-partition all members,
  with at least one multi-member entry;
- pending: zero resolved entries and a non-empty pending_reason.

FORBIDDEN WORK
- Do not infer equivalence merely because targets match.
- Do not invent a source surface, Vietnamese target, directive, candidate ID,
  component ID, evidence block, or omitted context.
- Do not use gold, an external glossary, memory, expected answers, or outside
  sources.
- Do not change Builder 2 admission decisions or publish a book-level glossary.
- Do not assign confidence or omit a supplied component.

Return JSON only with exactly this shape:
{
  "packet_id": "supplied packet id",
  "decisions": [
    {
      "component_id": "supplied component id",
      "action": "merge_all|keep_separate|partition|pending",
      "resolved_entries": [
        {
          "member_candidate_ids": ["supplied candidate id"],
          "canonical_source": "supplied member surface",
          "canonical_target_vi": "supplied Builder 2 target",
          "alternative_targets": [
            {"target_vi": "supplied alternative", "applicability": "condition"}
          ],
          "directive": "translate|preserve|contextual",
          "evidence_block_ids": ["supplied member evidence block id"],
          "rationale": "short source-grounded reason"
        }
      ],
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
                            "component_id",
                            "action",
                            "resolved_entries",
                            "pending_reason",
                        ],
                        "properties": {
                            "component_id": {"type": "string"},
                            "action": {
                                "type": "string",
                                "enum": [
                                    "merge_all",
                                    "keep_separate",
                                    "partition",
                                    "pending",
                                ],
                            },
                            "resolved_entries": {
                                "type": "array",
                                "items": {
                                    "type": "object",
                                    "additionalProperties": False,
                                    "required": [
                                        "member_candidate_ids",
                                        "canonical_source",
                                        "canonical_target_vi",
                                        "alternative_targets",
                                        "directive",
                                        "evidence_block_ids",
                                        "rationale",
                                    ],
                                    "properties": {
                                        "member_candidate_ids": {
                                            "type": "array",
                                            "minItems": 1,
                                            "items": {"type": "string"},
                                        },
                                        "canonical_source": {"type": "string"},
                                        "canonical_target_vi": {"type": "string"},
                                        "alternative_targets": {
                                            "type": "array",
                                            "items": {
                                                "type": "object",
                                                "additionalProperties": False,
                                                "required": [
                                                    "target_vi",
                                                    "applicability",
                                                ],
                                                "properties": {
                                                    "target_vi": {
                                                        "type": "string"
                                                    },
                                                    "applicability": {
                                                        "type": "string"
                                                    },
                                                },
                                            },
                                        },
                                        "directive": {
                                            "type": "string",
                                            "enum": [
                                                "translate",
                                                "preserve",
                                                "contextual",
                                            ],
                                        },
                                        "evidence_block_ids": {
                                            "type": "array",
                                            "minItems": 1,
                                            "items": {"type": "string"},
                                        },
                                        "rationale": {"type": "string"},
                                    },
                                },
                            },
                            "pending_reason": {
                                "type": ["string", "null"]
                            },
                        },
                    },
                },
            },
        },
    },
}


class ConsolidationContractError(ValueError):
    pass


@dataclass(frozen=True)
class AlternativeTarget:
    target_vi: str
    applicability: str


@dataclass(frozen=True)
class ResolvedEntry:
    member_candidate_ids: tuple[str, ...]
    canonical_source: str
    canonical_target_vi: str
    alternative_targets: tuple[AlternativeTarget, ...]
    directive: str
    evidence_block_ids: tuple[str, ...]
    rationale: str


@dataclass(frozen=True)
class ComponentDecision:
    component_id: str
    action: str
    resolved_entries: tuple[ResolvedEntry, ...]
    pending_reason: str | None


@dataclass(frozen=True)
class ConsolidationValidation:
    packet_id: str
    decisions: tuple[ComponentDecision, ...]
    errors: tuple[str, ...]
    missing_component_ids: tuple[str, ...]
    duplicate_component_ids: tuple[str, ...]
    normalization_warnings: tuple[str, ...]


def prompt_sha256() -> str:
    return sha256(SYSTEM_PROMPT.encode("utf-8")).hexdigest().upper()


def response_schema_sha256() -> str:
    schema = RESPONSE_FORMAT["json_schema"]["schema"]
    rendered = json.dumps(
        schema,
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
        "CONSOLIDATION_PACKET_JSON\n"
        + json.dumps(
            {
                "packet_id": packet["packet_id"],
                "chapter_id": packet["chapter_id"],
                "components": packet["components"],
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
        raise ConsolidationContractError("Expected exactly one user message")
    return sha256(users[0].encode("utf-8")).hexdigest().upper()


def parse_response_json(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        parsed = value
    elif isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError as exc:
            raise ConsolidationContractError(str(exc)) from exc
    else:
        raise ConsolidationContractError(
            "Response is neither JSON text nor an object"
        )
    if not isinstance(parsed, dict):
        raise ConsolidationContractError("Response top level must be an object")
    if set(parsed) != {"packet_id", "decisions"}:
        raise ConsolidationContractError(
            "Top-level keys must be exactly ['decisions', 'packet_id']"
        )
    if not isinstance(parsed["decisions"], list):
        raise ConsolidationContractError("decisions must be a list")
    return dict(parsed)


def validate_output(
    parsed: Mapping[str, Any], *, packet: Mapping[str, Any]
) -> ConsolidationValidation:
    _validate_packet(packet)
    packet_id = str(packet["packet_id"])
    components = {
        str(row["component_id"]): dict(row) for row in packet["components"]
    }
    errors: list[str] = []
    if parsed.get("packet_id") != packet_id:
        errors.append("packet_id does not match the supplied request")

    decisions: list[ComponentDecision] = []
    normalization_warnings: list[str] = []
    seen: set[str] = set()
    duplicates: set[str] = set()
    for index, raw in enumerate(parsed.get("decisions") or []):
        if not isinstance(raw, dict):
            errors.append(f"decisions[{index}] must be an object")
            continue
        component_id = raw.get("component_id")
        if not isinstance(component_id, str) or component_id not in components:
            errors.append(f"decisions[{index}] has an unknown component_id")
            continue
        if component_id in seen:
            duplicates.add(component_id)
            errors.append(f"component {component_id} is decided more than once")
            continue
        seen.add(component_id)
        try:
            decision, warning = _validate_component_decision(
                    raw,
                    index=index,
                    component=components[component_id],
                )
            decisions.append(decision)
            if warning is not None:
                normalization_warnings.append(warning)
        except ConsolidationContractError as exc:
            errors.append(str(exc))

    missing = sorted(set(components) - seen)
    if missing:
        errors.append("Response does not exact-cover supplied component IDs")
    return ConsolidationValidation(
        packet_id=packet_id,
        decisions=tuple(decisions),
        errors=tuple(errors),
        missing_component_ids=tuple(missing),
        duplicate_component_ids=tuple(sorted(duplicates)),
        normalization_warnings=tuple(normalization_warnings),
    )


def _validate_component_decision(
    raw: Mapping[str, Any], *, index: int, component: Mapping[str, Any]
) -> tuple[ComponentDecision, str | None]:
    required = {
        "component_id",
        "action",
        "resolved_entries",
        "pending_reason",
    }
    if set(raw) != required:
        raise ConsolidationContractError(f"decisions[{index}] has invalid keys")
    action = raw.get("action")
    if action not in {"merge_all", "keep_separate", "partition", "pending"}:
        raise ConsolidationContractError(f"decisions[{index}].action is invalid")
    entries_raw = raw.get("resolved_entries")
    if not isinstance(entries_raw, list):
        raise ConsolidationContractError(
            f"decisions[{index}].resolved_entries must be a list"
        )
    pending_reason = raw.get("pending_reason")
    if action == "pending":
        if entries_raw:
            raise ConsolidationContractError(
                f"decisions[{index}] pending must have zero resolved entries"
            )
        if not isinstance(pending_reason, str) or not pending_reason.strip():
            raise ConsolidationContractError(
                f"decisions[{index}].pending_reason is required"
            )
        return (
            ComponentDecision(
                component_id=str(raw["component_id"]),
                action=str(action),
                resolved_entries=(),
                pending_reason=pending_reason.strip(),
            ),
            None,
        )
    if pending_reason is not None:
        raise ConsolidationContractError(
            f"decisions[{index}] resolved action must have null pending_reason"
        )
    if not entries_raw:
        raise ConsolidationContractError(
            f"decisions[{index}] resolved action requires entries"
        )

    members = {
        str(row["candidate_id"]): dict(row) for row in component["members"]
    }
    entries: list[ResolvedEntry] = []
    assigned: list[str] = []
    for entry_index, entry_raw in enumerate(entries_raw):
        entry = _validate_resolved_entry(
            entry_raw,
            decision_index=index,
            entry_index=entry_index,
            members=members,
            source_block_ids={
                str(value) for value in component["source_block_ids"]
            },
        )
        entries.append(entry)
        assigned.extend(entry.member_candidate_ids)
    if len(assigned) != len(set(assigned)):
        raise ConsolidationContractError(
            f"decisions[{index}] assigns a candidate more than once"
        )
    if set(assigned) != set(members):
        raise ConsolidationContractError(
            f"decisions[{index}] does not exact-partition component members"
        )
    normalized_action = str(action)
    normalization_warning = None
    if action == "partition" and (
        len(entries) == 1
        and len(entries[0].member_candidate_ids) == len(members)
    ):
        normalized_action = "merge_all"
        normalization_warning = (
            f"decisions[{index}] normalized one-group exact-cover partition "
            "to merge_all"
        )
    elif action == "partition" and (
        len(entries) == len(members)
        and all(len(entry.member_candidate_ids) == 1 for entry in entries)
    ):
        normalized_action = "keep_separate"
        normalization_warning = (
            f"decisions[{index}] normalized all-singleton exact-cover "
            "partition to keep_separate"
        )
    if normalized_action == "merge_all" and (
        len(entries) != 1 or len(entries[0].member_candidate_ids) != len(members)
    ):
        raise ConsolidationContractError(
            f"decisions[{index}] merge_all has invalid cardinality"
        )
    if normalized_action == "keep_separate" and (
        len(entries) != len(members)
        or any(len(entry.member_candidate_ids) != 1 for entry in entries)
    ):
        raise ConsolidationContractError(
            f"decisions[{index}] keep_separate has invalid cardinality"
        )
    if normalized_action == "partition" and (
        len(entries) < 2
        or len(entries) >= len(members)
        or not any(len(entry.member_candidate_ids) > 1 for entry in entries)
    ):
        raise ConsolidationContractError(
            f"decisions[{index}] partition has invalid cardinality"
        )
    return (
        ComponentDecision(
            component_id=str(raw["component_id"]),
            action=normalized_action,
            resolved_entries=tuple(entries),
            pending_reason=None,
        ),
        normalization_warning,
    )


def _validate_resolved_entry(
    raw: Any,
    *,
    decision_index: int,
    entry_index: int,
    members: Mapping[str, Mapping[str, Any]],
    source_block_ids: set[str],
) -> ResolvedEntry:
    prefix = f"decisions[{decision_index}].resolved_entries[{entry_index}]"
    required = {
        "member_candidate_ids",
        "canonical_source",
        "canonical_target_vi",
        "alternative_targets",
        "directive",
        "evidence_block_ids",
        "rationale",
    }
    if not isinstance(raw, dict) or set(raw) != required:
        raise ConsolidationContractError(f"{prefix} has invalid keys")
    member_ids = raw.get("member_candidate_ids")
    if not isinstance(member_ids, list) or not member_ids or not all(
        isinstance(value, str) and value in members for value in member_ids
    ):
        raise ConsolidationContractError(f"{prefix}.member_candidate_ids is invalid")
    if len(member_ids) != len(set(member_ids)):
        raise ConsolidationContractError(
            f"{prefix}.member_candidate_ids contains duplicates"
        )
    selected = [members[value] for value in member_ids]
    allowed_surfaces = {
        str(surface) for member in selected for surface in member["surfaces"]
    }
    allowed_targets = {
        str(proposal["target_vi"])
        for member in selected
        for proposal in member["target_proposals"]
    }
    allowed_directives = {str(member["directive"]) for member in selected}
    allowed_evidence = {
        str(value)
        for member in selected
        for value in member["evidence_block_ids"]
    }
    canonical_source = raw.get("canonical_source")
    if canonical_source not in allowed_surfaces:
        raise ConsolidationContractError(
            f"{prefix}.canonical_source is not a supplied member surface"
        )
    canonical_target = raw.get("canonical_target_vi")
    if canonical_target not in allowed_targets:
        raise ConsolidationContractError(
            f"{prefix}.canonical_target_vi is not a supplied B2 target"
        )
    directive = raw.get("directive")
    if directive not in allowed_directives:
        raise ConsolidationContractError(
            f"{prefix}.directive is not supplied by assigned members"
        )
    alternatives_raw = raw.get("alternative_targets")
    if not isinstance(alternatives_raw, list):
        raise ConsolidationContractError(f"{prefix}.alternative_targets is invalid")
    alternatives: list[AlternativeTarget] = []
    seen_targets = {str(canonical_target).strip().casefold()}
    for alternative_index, alternative in enumerate(alternatives_raw):
        alt_prefix = f"{prefix}.alternative_targets[{alternative_index}]"
        if not isinstance(alternative, dict) or set(alternative) != {
            "target_vi",
            "applicability",
        }:
            raise ConsolidationContractError(f"{alt_prefix} has invalid keys")
        target = alternative.get("target_vi")
        applicability = alternative.get("applicability")
        if target not in allowed_targets:
            raise ConsolidationContractError(
                f"{alt_prefix}.target_vi is not a supplied B2 target"
            )
        target_key = str(target).strip().casefold()
        if target_key in seen_targets:
            raise ConsolidationContractError(
                f"{prefix}.alternative_targets repeats a target"
            )
        seen_targets.add(target_key)
        if not isinstance(applicability, str) or not applicability.strip():
            raise ConsolidationContractError(
                f"{alt_prefix}.applicability is required"
            )
        alternatives.append(
            AlternativeTarget(
                target_vi=str(target),
                applicability=applicability.strip(),
            )
        )
    evidence = raw.get("evidence_block_ids")
    if not isinstance(evidence, list) or not evidence or not all(
        isinstance(value, str) and value for value in evidence
    ):
        raise ConsolidationContractError(f"{prefix}.evidence_block_ids is invalid")
    if len(evidence) != len(set(evidence)):
        raise ConsolidationContractError(
            f"{prefix}.evidence_block_ids contains duplicates"
        )
    if not set(evidence).issubset(allowed_evidence & source_block_ids):
        raise ConsolidationContractError(
            f"{prefix} cites evidence outside assigned members"
        )
    rationale = raw.get("rationale")
    if not isinstance(rationale, str) or not rationale.strip():
        raise ConsolidationContractError(f"{prefix}.rationale is invalid")
    return ResolvedEntry(
        member_candidate_ids=tuple(member_ids),
        canonical_source=str(canonical_source),
        canonical_target_vi=str(canonical_target),
        alternative_targets=tuple(alternatives),
        directive=str(directive),
        evidence_block_ids=tuple(evidence),
        rationale=rationale.strip(),
    )


def _validate_packet(packet: Mapping[str, Any]) -> None:
    if set(packet) != {"packet_id", "chapter_id", "components", "source_blocks"}:
        raise ConsolidationContractError("Packet has an invalid top-level shape")
    if not isinstance(packet["packet_id"], str) or not packet["packet_id"]:
        raise ConsolidationContractError("packet_id is invalid")
    if not isinstance(packet["chapter_id"], str) or not packet["chapter_id"]:
        raise ConsolidationContractError("chapter_id is invalid")
    components = packet["components"]
    blocks = packet["source_blocks"]
    if not isinstance(components, list) or not components:
        raise ConsolidationContractError("components must be a non-empty list")
    if not isinstance(blocks, list) or not blocks:
        raise ConsolidationContractError("source_blocks must be a non-empty list")
    block_ids: set[str] = set()
    for index, row in enumerate(blocks):
        if not isinstance(row, dict) or set(row) != {"block_id", "text"}:
            raise ConsolidationContractError(
                f"source_blocks[{index}] has invalid shape"
            )
        block_id = row.get("block_id")
        text = row.get("text")
        if not isinstance(block_id, str) or not block_id or block_id in block_ids:
            raise ConsolidationContractError(
                f"source_blocks[{index}].block_id is invalid"
            )
        if not isinstance(text, str) or not text:
            raise ConsolidationContractError(
                f"source_blocks[{index}].text is invalid"
            )
        block_ids.add(block_id)

    component_ids: set[str] = set()
    candidate_ids: set[str] = set()
    component_keys = {
        "component_id",
        "reason_codes",
        "members",
        "edges",
        "source_block_ids",
    }
    member_keys = {
        "candidate_id",
        "canonical_source",
        "surfaces",
        "target_proposals",
        "directive",
        "evidence_block_ids",
        "evidence_complete",
        "decision_rationale",
    }
    for component_index, component in enumerate(components):
        if not isinstance(component, dict) or set(component) != component_keys:
            raise ConsolidationContractError(
                f"components[{component_index}] has invalid shape"
            )
        component_id = component.get("component_id")
        if (
            not isinstance(component_id, str)
            or not component_id
            or component_id in component_ids
        ):
            raise ConsolidationContractError(
                f"components[{component_index}].component_id is invalid"
            )
        component_ids.add(component_id)
        reason_codes = component.get("reason_codes")
        members = component.get("members")
        edges = component.get("edges")
        source_ids = component.get("source_block_ids")
        if not isinstance(reason_codes, list) or not reason_codes or not all(
            isinstance(value, str) and value for value in reason_codes
        ):
            raise ConsolidationContractError(
                f"components[{component_index}].reason_codes is invalid"
            )
        if not isinstance(members, list) or not members:
            raise ConsolidationContractError(
                f"components[{component_index}].members is invalid"
            )
        if not isinstance(edges, list):
            raise ConsolidationContractError(
                f"components[{component_index}].edges is invalid"
            )
        if not isinstance(source_ids, list) or not source_ids or not set(
            source_ids
        ).issubset(block_ids):
            raise ConsolidationContractError(
                f"components[{component_index}].source_block_ids is invalid"
            )
        local_ids: set[str] = set()
        for member_index, member in enumerate(members):
            if not isinstance(member, dict) or set(member) != member_keys:
                raise ConsolidationContractError(
                    f"components[{component_index}].members[{member_index}] has invalid shape"
                )
            candidate_id = member.get("candidate_id")
            if (
                not isinstance(candidate_id, str)
                or not candidate_id
                or candidate_id in local_ids
                or candidate_id in candidate_ids
            ):
                raise ConsolidationContractError(
                    f"components[{component_index}].members[{member_index}].candidate_id is invalid"
                )
            local_ids.add(candidate_id)
            candidate_ids.add(candidate_id)
            _validate_member(
                member,
                prefix=(
                    f"components[{component_index}].members[{member_index}]"
                ),
                source_ids=set(source_ids),
            )
        edge_keys = {"left_candidate_id", "right_candidate_id", "signals"}
        for edge_index, edge in enumerate(edges):
            if not isinstance(edge, dict) or set(edge) != edge_keys:
                raise ConsolidationContractError(
                    f"components[{component_index}].edges[{edge_index}] has invalid shape"
                )
            if edge.get("left_candidate_id") not in local_ids or edge.get(
                "right_candidate_id"
            ) not in local_ids:
                raise ConsolidationContractError(
                    f"components[{component_index}].edges[{edge_index}] references an unknown member"
                )
            signals = edge.get("signals")
            if not isinstance(signals, list) or not signals or not all(
                isinstance(value, str) and value for value in signals
            ):
                raise ConsolidationContractError(
                    f"components[{component_index}].edges[{edge_index}].signals is invalid"
                )


def _validate_member(
    member: Mapping[str, Any], *, prefix: str, source_ids: set[str]
) -> None:
    canonical = member.get("canonical_source")
    surfaces = member.get("surfaces")
    targets = member.get("target_proposals")
    directive = member.get("directive")
    evidence = member.get("evidence_block_ids")
    rationale = member.get("decision_rationale")
    if not isinstance(canonical, str) or not canonical:
        raise ConsolidationContractError(f"{prefix}.canonical_source is invalid")
    if not isinstance(surfaces, list) or canonical not in surfaces or not all(
        isinstance(value, str) and value for value in surfaces
    ):
        raise ConsolidationContractError(f"{prefix}.surfaces is invalid")
    if not isinstance(targets, list) or not targets:
        raise ConsolidationContractError(f"{prefix}.target_proposals is invalid")
    seen_targets: set[str] = set()
    for target_index, target in enumerate(targets):
        if not isinstance(target, dict) or set(target) != {
            "target_vi",
            "applicability",
        }:
            raise ConsolidationContractError(
                f"{prefix}.target_proposals[{target_index}] has invalid shape"
            )
        value = target.get("target_vi")
        applicability = target.get("applicability")
        if not isinstance(value, str) or not value.strip():
            raise ConsolidationContractError(
                f"{prefix}.target_proposals[{target_index}].target_vi is invalid"
            )
        key = value.strip().casefold()
        if key in seen_targets:
            raise ConsolidationContractError(
                f"{prefix}.target_proposals repeats a target"
            )
        seen_targets.add(key)
        if applicability is not None and (
            not isinstance(applicability, str) or not applicability.strip()
        ):
            raise ConsolidationContractError(
                f"{prefix}.target_proposals[{target_index}].applicability is invalid"
            )
    if directive not in {"translate", "preserve", "contextual"}:
        raise ConsolidationContractError(f"{prefix}.directive is invalid")
    if not isinstance(evidence, list) or not evidence or not set(evidence).issubset(
        source_ids
    ):
        raise ConsolidationContractError(f"{prefix}.evidence_block_ids is invalid")
    if len(evidence) != len(set(evidence)):
        raise ConsolidationContractError(
            f"{prefix}.evidence_block_ids contains duplicates"
        )
    if not isinstance(member.get("evidence_complete"), bool):
        raise ConsolidationContractError(f"{prefix}.evidence_complete is invalid")
    if not isinstance(rationale, str) or not rationale.strip():
        raise ConsolidationContractError(f"{prefix}.decision_rationale is invalid")
