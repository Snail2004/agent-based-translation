from __future__ import annotations

import json
from dataclasses import dataclass
from hashlib import sha256
from typing import Any, Iterable


PROMPT_VERSION = "d2l_candidate_discovery_v2"
VALIDATOR_VERSION = "d2l_candidate_discovery_validator_v2"
RESPONSE_FORMAT = {"type": "json_object"}
RESPONSE_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["chapter_id", "window_id", "candidate_observations"],
    "properties": {
        "chapter_id": {"type": "string"},
        "window_id": {"type": "string"},
        "candidate_observations": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["source_surface", "anchor_block_ids"],
                "properties": {
                    "source_surface": {"type": "string"},
                    "anchor_block_ids": {
                        "type": "array",
                        "minItems": 1,
                        "maxItems": 3,
                        "items": {"type": "string"},
                    },
                },
            },
        },
    },
}

SYSTEM_PROMPT = r"""You are Candidate Scanner 1 for an autonomous English-to-Vietnamese
technical-book translation pipeline.

Prompt version: d2l_candidate_discovery_v2.

YOUR ONLY JOB
Read the supplied English source window and point to expressions that a later stage
should evaluate as glossary candidates. You are a wide net, not a judge.

Include an exact source expression when, in this passage, it carries domain-relevant
meaning and a later stage may need to decide whether it requires consistent or
sense-specific translation.

- A candidate may be any part of speech, not only a noun phrase.
- An ordinary-looking expression remains eligible when it carries domain-relevant
  meaning in the supplied passage.
- When uncertain, include the expression. Missing a real candidate is worse than
  including a doubtful one; later stages decide termhood and translation policy with
  broader evidence.

You make a permissive candidate proposal. You do not make the final term-admission or
translation-policy decision.

STRUCTURAL EXCLUSIONS ONLY
- Do not emit an entire sentence or instruction as one candidate. Expressions inside
  instructions remain eligible.
- Do not emit URLs, cross-reference labels, bare numbers, file paths, or markup and
  LaTeX wrappers.
- If markup wraps an eligible expression, report the exact inner text only when that
  inner text is a contiguous substring of the supplied source.

BOUNDARIES AND SOURCE COPYING
- Copy source_surface verbatim as one contiguous exact substring of the supplied
  English source.
- Emit a complete local expression that directly denotes the concept, object, action,
  quantity, symbol, or domain-specific usage you noticed.
- Do not enumerate internal subspans solely because they occur inside a longer
  expression. A later code stage may generate subspan candidates and corpus-occurrence
  statistics; a later Builder decides whether any subspan is a real lexical unit.
- If two visibly different exact forms occur in the window, report each form as its
  own observation.
- Emit each exact source_surface at most once per window.
- For each observation, list one to three anchor_block_ids where you noticed it. You
  do not need to list every occurrence; code locates every exact occurrence later.

FORBIDDEN WORK
- Do not translate into Vietnamese.
- Do not decide final termhood, importance, priority, policy, or ownership.
- Do not propose a canonical form, definition, confidence, variant, parent, or merge.
- Do not use a glossary, memory pack, external source, reference translation, gold
  list, or expected answer.
- Do not invent text that is absent from the supplied source.

Return JSON only with exactly this shape:
{
  "chapter_id": "supplied chapter id",
  "window_id": "supplied window id",
  "candidate_observations": [
    {
      "source_surface": "verbatim source text",
      "anchor_block_ids": ["supplied block id"]
    }
  ]
}
"""


class DiscoveryContractError(ValueError):
    pass


@dataclass(frozen=True)
class DiscoveryObservation:
    observation_id: str
    source_surface: str
    claimed_anchor_block_ids: tuple[str, ...]
    invalid_claimed_anchor_block_ids: tuple[str, ...]
    source_block_ids: tuple[str, ...]


@dataclass(frozen=True)
class DiscoveryValidation:
    chapter_id: str
    window_id: str
    observations: tuple[DiscoveryObservation, ...]
    errors: tuple[str, ...]
    warnings: tuple[str, ...]
    rejected_rows: int
    duplicate_rows: int
    block_claims_added_by_code: int
    anchor_claims_truncated_by_code: int


def prompt_sha256() -> str:
    return sha256(SYSTEM_PROMPT.encode("utf-8")).hexdigest().upper()


def render_discovery_messages(
    *,
    chapter_id: str,
    window_id: str,
    source_blocks: Iterable[tuple[str, str]],
) -> list[dict[str, str]]:
    blocks = [(str(block_id), str(text)) for block_id, text in source_blocks]
    if not chapter_id or not window_id:
        raise DiscoveryContractError("chapter_id and window_id are required")
    if not blocks:
        raise DiscoveryContractError("At least one source block is required")
    block_ids = [block_id for block_id, _ in blocks]
    if any(not block_id for block_id in block_ids) or len(block_ids) != len(set(block_ids)):
        raise DiscoveryContractError("Source block IDs must be non-empty and unique")
    rendered = "\n".join(f"[{block_id}] {text}" for block_id, text in blocks)
    user = (
        f"CHAPTER_ID\n{chapter_id}\n\n"
        f"WINDOW_ID\n{window_id}\n\n"
        "ENGLISH_SOURCE_WINDOW_WITH_BLOCK_MARKERS\n"
        f"{rendered}"
    )
    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": user},
    ]


def user_payload_sha256(messages: list[dict[str, str]]) -> str:
    user = _message_content(messages, "user")
    return sha256(user.encode("utf-8")).hexdigest().upper()


def parse_discovery_json(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        parsed = value
    elif isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError as exc:
            raise DiscoveryContractError(str(exc)) from exc
    else:
        raise DiscoveryContractError("Discovery response is neither JSON text nor an object")
    if not isinstance(parsed, dict):
        raise DiscoveryContractError("Discovery response top level must be an object")
    required = {"chapter_id", "window_id", "candidate_observations"}
    actual = set(parsed)
    if actual != required:
        raise DiscoveryContractError(
            f"Discovery top-level keys must be exactly {sorted(required)}; got {sorted(actual)}"
        )
    if not isinstance(parsed["candidate_observations"], list):
        raise DiscoveryContractError("candidate_observations must be a list")
    return dict(parsed)


def validate_discovery_output(
    parsed: dict[str, Any],
    *,
    chapter_id: str,
    window_id: str,
    source_blocks: Iterable[tuple[str, str]],
    source_lineage_id: str,
) -> DiscoveryValidation:
    blocks = [(str(block_id), str(text)) for block_id, text in source_blocks]
    block_order = {block_id: index for index, (block_id, _) in enumerate(blocks)}
    block_text = dict(blocks)
    errors: list[str] = []
    warnings: list[str] = []
    rejected_rows = 0
    truncated_anchors = 0
    if parsed.get("chapter_id") != chapter_id:
        errors.append("chapter_id does not match the supplied request")
    if parsed.get("window_id") != window_id:
        errors.append("window_id does not match the supplied request")

    grouped: dict[str, dict[str, set[str]]] = {}
    row_count: dict[str, int] = {}
    for index, row in enumerate(parsed.get("candidate_observations") or []):
        if not isinstance(row, dict):
            warnings.append(f"candidate_observations[{index}] is not an object")
            rejected_rows += 1
            continue
        required = {"source_surface", "anchor_block_ids"}
        actual = set(row)
        if actual != required:
            warnings.append(
                f"candidate_observations[{index}] keys must be exactly {sorted(required)}"
            )
            rejected_rows += 1
            continue
        surface = row.get("source_surface")
        anchors = row.get("anchor_block_ids")
        if not isinstance(surface, str) or not surface or surface != surface.strip():
            warnings.append(
                f"candidate_observations[{index}].source_surface must be non-empty verbatim text"
            )
            rejected_rows += 1
            continue
        if not isinstance(anchors, list) or not anchors or not all(
            isinstance(item, str) and item for item in anchors
        ):
            warnings.append(
                f"candidate_observations[{index}].anchor_block_ids must be a non-empty string list"
            )
            rejected_rows += 1
            continue

        unique_anchors = list(dict.fromkeys(anchors))
        if len(unique_anchors) > 3:
            truncated_anchors += len(unique_anchors) - 3
            warnings.append(
                f"candidate_observations[{index}] supplied more than three anchors; "
                "code ignored the extras"
            )
            unique_anchors = unique_anchors[:3]

        located = {block_id for block_id, text in blocks if surface in text}
        if not located:
            warnings.append(f"candidate_observations[{index}] source surface is unlocatable")
            rejected_rows += 1
            continue
        foreign = sorted({item for item in unique_anchors if item not in block_text})
        invalid_claims = sorted(
            {
                item
                for item in unique_anchors
                if item in block_text and surface not in block_text[item]
            }
        )
        if foreign:
            warnings.append(
                f"candidate_observations[{index}] ignored foreign anchor block IDs: {foreign}"
            )
        if invalid_claims:
            warnings.append(
                f"candidate_observations[{index}] ignored anchors without the exact surface: "
                f"{invalid_claims}"
            )
        group = grouped.setdefault(
            surface, {"valid_claims": set(), "invalid_claims": set()}
        )
        group["valid_claims"].update(item for item in unique_anchors if item in located)
        group["invalid_claims"].update([*foreign, *invalid_claims])
        row_count[surface] = row_count.get(surface, 0) + 1

    observations: list[DiscoveryObservation] = []
    claims_added = 0
    for surface in sorted(grouped, key=lambda item: (item.casefold(), item)):
        group = grouped[surface]
        claimed = tuple(sorted(group["valid_claims"], key=block_order.__getitem__))
        invalid_claimed = tuple(sorted(group["invalid_claims"]))
        located = tuple(block_id for block_id, text in blocks if surface in text)
        claims_added += len(set(located) - set(claimed))
        payload = {
            "source_lineage_id": source_lineage_id,
            "prompt_sha256": prompt_sha256(),
            "chapter_id": chapter_id,
            "window_id": window_id,
            "source_surface": surface,
            "source_block_ids": list(located),
        }
        observation_id = "obs_" + sha256(
            json.dumps(
                payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()[:24]
        observations.append(
            DiscoveryObservation(
                observation_id=observation_id,
                source_surface=surface,
                claimed_anchor_block_ids=claimed,
                invalid_claimed_anchor_block_ids=invalid_claimed,
                source_block_ids=located,
            )
        )
    return DiscoveryValidation(
        chapter_id=chapter_id,
        window_id=window_id,
        observations=tuple(observations),
        errors=tuple(errors),
        warnings=tuple(warnings),
        rejected_rows=rejected_rows,
        duplicate_rows=sum(max(0, count - 1) for count in row_count.values()),
        block_claims_added_by_code=claims_added,
        anchor_claims_truncated_by_code=truncated_anchors,
    )


def _message_content(messages: list[dict[str, str]], role: str) -> str:
    matches = [item.get("content", "") for item in messages if item.get("role") == role]
    if len(matches) != 1:
        raise DiscoveryContractError(f"Expected exactly one {role} message")
    return matches[0]
