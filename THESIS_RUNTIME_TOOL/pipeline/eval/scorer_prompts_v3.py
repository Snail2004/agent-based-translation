from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from dataclasses import dataclass
from typing import Any, Mapping

from pipeline.eval.contracts_v1 import (
    ContractValidationError,
    require_enum,
    require_exact_keys,
    require_int,
    require_list,
    require_mapping,
    require_string,
    require_unique,
)
from pipeline.eval.scorer_input_packets_v1 import validate_scorer_input_packet
from pipeline.eval.sf_bt_stage_contracts_v1 import (
    SF_BT_REVERSE_CANDIDATE_ID,
    SF_BT_REVERSE_PROMPT_SHA256,
    validate_sf_bt_semantic_judge_packet,
)


__all__ = [
    "CONTEXT_NOT_AVAILABLE",
    "PJ_COMMON_CANDIDATE_ID",
    "PJ_COMMON_PROMPT_SHA256",
    "PJPromptPresentationsV3",
    "RenderedPromptV3",
    "SF_BT_SEMANTIC_CANDIDATE_ID",
    "SF_BT_SEMANTIC_PROMPT_SHA256",
    "parse_pj_response_v2",
    "parse_sf_bt_semantic_response_v3",
    "prepare_pj_prompt_presentations_v3",
    "render_sf_bt_reverse_prompt_v3",
    "render_sf_bt_semantic_passages_v3",
    "render_sf_bt_semantic_prompt_v3",
]


CONTEXT_NOT_AVAILABLE = "[CONTEXT BLOCK NOT AVAILABLE]"
SF_BT_SEMANTIC_CANDIDATE_ID = "sf_bt_semantic_judge_v3_candidate"
SF_BT_SEMANTIC_PROMPT_SHA256 = (
    "64d4f9c9fb63b190afc4ff54f6fd1ab9ced26d47f7653d9227003ab63b689753"
)
PJ_COMMON_CANDIDATE_ID = "pj_common_v2_candidate"
PJ_COMMON_PROMPT_SHA256 = (
    "faaa90b02253a24ce1143b84dd6840533c7b4349c95c64486fd12ec6673511d8"
)

_SF_BT_REVERSE_TEMPLATE = """You are an independent Vietnamese-to-English back-translator used for translation evaluation.

Translate only the block marked ACTIVE into English.
Use PRECEDING and FOLLOWING blocks only to resolve references, terminology, and continuity.

Requirements:
- Preserve every fact, claim, number, negation, entity, and logical relation that the ACTIVE block itself expresses.
- Use PRECEDING and FOLLOWING only to choose correct English wording for references and terminology that the ACTIVE block already contains. Never import a fact, claim, number, or entity that appears only in PRECEDING or FOLLOWING. If the ACTIVE block omits something, your English must omit it too.
- A pronoun or other referring expression in ACTIVE may be rendered with its context-resolved antecedent when needed for unambiguous English. This resolves an ACTIVE reference; it does not authorize adding an entity that ACTIVE never refers to.
- Preserve Markdown, inline code, URLs, and LaTeX math exactly where possible.
- Do not summarize, shorten, expand, explain, criticize, or improve the content.
- If the ACTIVE block is unclear or appears incorrect, translate it as faithfully as possible; do not guess a better version.
- Do not translate the context blocks as additional output.
- Return only the JSON object described below.

Return JSON exactly:
{"back_translation":"English translation of the ACTIVE block"}

VIETNAMESE BLOCK SEQUENCE
{target_block_sequence}"""

_SF_BT_SEMANTIC_TEMPLATE = """You compare two English passages.
Judge how closely they match IN MEANING. The passage labels and order are arbitrary.

Ignore differences in style, word choice, sentence order, formatting, and phrasing when meaning is unchanged.
Judge only facts, claims, numbers, logical relations, negations, and coverage.
Coverage counts in both directions: content present in only one passage is a mismatch.
Length by itself is not evidence of better or worse meaning preservation.

Score bands:
100 = same meaning; differences are purely stylistic
75 = minor drift; one small detail differs or became vague
50 = noticeable drift; a fact, number, or relation differs
25 = substantial mismatch; key claims differ or content is absent
0 = different or contradictory content

Choose exactly one band value: 0, 25, 50, 75, or 100. Do not output any other number.

Flags, all that apply or an empty list:
semantic_mismatch | numeric_mismatch | negation_mismatch | coverage_mismatch | untranslated_residue | format_only

Return JSON exactly:
{"score":0,"flags":[],"note":"one short English sentence"}

PASSAGE A
{passage_a}

PASSAGE B
{passage_b}"""

_PJ_COMMON_TEMPLATE = """You are a strict, impartial evaluator of Vietnamese translations of an English source.

You receive one English block sequence and two Vietnamese candidate sequences labeled Candidate 1 and Candidate 2. Their labels and order are arbitrary and reveal nothing about the systems that produced them.

Judge only the ACTIVE block. Use PRECEDING and FOLLOWING blocks to understand continuity, references, register, and tone; do not score those context blocks as additional items.
A context row may read [CONTEXT BLOCK NOT AVAILABLE]. Never count an unavailable context row for or against a candidate.

Return only JSON with exactly these keys:
{"overall_verdict":"candidate_1|candidate_2|tie","style_verdict":"candidate_1|candidate_2|tie","tags":[],"note":"one short English sentence"}

Definitions:
1. overall_verdict: the better translation overall, considering meaning, completeness, terminology, grammar, naturalness, tone/voice, and formatting. Use tie when differences are immaterial or evidence is insufficient.
2. style_verdict: the better Vietnamese prose, considering grammar, fluency, naturalness, register, tone/voice, and ordinary word choice. Ignore differences that are only technical terminology or source-faithfulness. If only meaning or terminology differs, style_verdict must be tie.
3. tags: zero to three decisive categories from this closed list, most important first:
grammar | naturalness | word_choice | terminology | meaning | omission_addition | formatting | tone_voice
4. note: at most 25 English words identifying the decisive difference, or "no meaningful difference".

Rules:
- Do not reward literal English-like wording when natural Vietnamese preserves the meaning.
- Longer output is not better output. Judge fidelity and natural Vietnamese, not length.
- Do not assume either candidate is a human, model, baseline, or memory-assisted output, and do not try to identify its producer.
- Prefer the candidate that preserves code, math, URLs, numbers, names, and document structure when the source requires them.
- The ACTIVE block may be a heading, caption, fragment, dialogue line, or prose paragraph. Judge it according to its actual function.
- Never invent a preference. Tie is a valid result.

ENGLISH SOURCE SEQUENCE
{source_block_sequence}

CANDIDATE 1 SEQUENCE
{candidate_1_block_sequence}

CANDIDATE 2 SEQUENCE
{candidate_2_block_sequence}"""

_SF_BT_FLAGS = frozenset(
    {
        "semantic_mismatch",
        "numeric_mismatch",
        "negation_mismatch",
        "coverage_mismatch",
        "untranslated_residue",
        "format_only",
    }
)
_PJ_TAGS = frozenset(
    {
        "grammar",
        "naturalness",
        "word_choice",
        "terminology",
        "meaning",
        "omission_addition",
        "formatting",
        "tone_voice",
    }
)
_PJ_VERDICTS = frozenset({"candidate_1", "candidate_2", "tie"})
_CONTEXT_PROFILES = frozenset({"no_context", "bounded_neighbors"})
_TEMPLATE_FIELD_RE = re.compile(r"\{([a-z][a-z0-9_]*)\}")


@dataclass(frozen=True, slots=True)
class RenderedPromptV3:
    candidate_id: str
    prompt_sha256: str
    rendered_prompt: str
    rendered_prompt_sha256: str


@dataclass(frozen=True, slots=True)
class PJPromptPresentationsV3:
    mechanical_equal: bool
    active_equal_context_diff: bool
    canonical: RenderedPromptV3 | None
    reversed: RenderedPromptV3 | None


def render_sf_bt_reverse_prompt_v3(
    packet: Mapping[str, Any], *, context_profile: str
) -> RenderedPromptV3:
    validated = validate_scorer_input_packet(packet)
    if (
        validated["binding"]["method_id"] != "sf_bt"
        or validated["stage"] != "back_translation"
    ):
        raise ContractValidationError(
            "prompt_method", "$", "SF-BT reverse prompt requires a stage-1 packet"
        )
    require_enum(context_profile, _CONTEXT_PROFILES, path="$.context_profile")
    rows = validated["candidates"][0]["blocks"]
    if context_profile == "no_context":
        rows = [row for row in rows if row["role"] == "active"]
    sequence = _render_block_sequence(rows)
    rendered = _render_template(
        _SF_BT_REVERSE_TEMPLATE, {"target_block_sequence": sequence}
    )
    _assert_prompt_does_not_expose_metadata(rendered, validated)
    return _rendered_prompt(
        candidate_id=SF_BT_REVERSE_CANDIDATE_ID,
        prompt_sha256=SF_BT_REVERSE_PROMPT_SHA256,
        rendered=rendered,
    )


def render_sf_bt_semantic_prompt_v3(
    packet: Mapping[str, Any],
) -> RenderedPromptV3:
    validated = validate_sf_bt_semantic_judge_packet(packet)
    passages = {row["slot_id"]: row["text"] for row in validated["passages"]}
    rendered_prompt = render_sf_bt_semantic_passages_v3(
        passage_a=passages["passage_a"],
        passage_b=passages["passage_b"],
    )
    _assert_prompt_does_not_expose_metadata(
        rendered_prompt.rendered_prompt, validated
    )
    return rendered_prompt


def render_sf_bt_semantic_passages_v3(
    *, passage_a: str, passage_b: str
) -> RenderedPromptV3:
    """Render the unchanged judge prompt for an honestly bound passage pair."""

    normalized_a = require_string(passage_a, path="$.passage_a")
    normalized_b = require_string(passage_b, path="$.passage_b")
    rendered = _render_template(
        _SF_BT_SEMANTIC_TEMPLATE,
        {
            "passage_a": normalized_a,
            "passage_b": normalized_b,
        },
    )
    return _rendered_prompt(
        candidate_id=SF_BT_SEMANTIC_CANDIDATE_ID,
        prompt_sha256=SF_BT_SEMANTIC_PROMPT_SHA256,
        rendered=rendered,
    )


def prepare_pj_prompt_presentations_v3(
    packet: Mapping[str, Any],
) -> PJPromptPresentationsV3:
    validated = validate_scorer_input_packet(packet)
    if (
        validated["binding"]["method_id"] != "pj"
        or validated["stage"] != "pairwise_judgment"
    ):
        raise ContractValidationError(
            "prompt_method", "$", "PJ prompt requires a pairwise-judgment packet"
        )
    source_rows = validated["source"]["blocks"]
    candidate_rows = _symmetrize_pj_context(
        validated["candidates"][0]["blocks"],
        validated["candidates"][1]["blocks"],
    )
    source_sequence = _render_block_sequence(source_rows)
    candidate_1_sequence = _render_block_sequence(candidate_rows[0])
    candidate_2_sequence = _render_block_sequence(candidate_rows[1])
    normalized_1 = _mechanical_normalize(candidate_1_sequence)
    normalized_2 = _mechanical_normalize(candidate_2_sequence)
    active_1 = next(row for row in candidate_rows[0] if row["role"] == "active")
    active_2 = next(row for row in candidate_rows[1] if row["role"] == "active")
    active_equal = _mechanical_normalize(active_1["text"]) == _mechanical_normalize(
        active_2["text"]
    )
    mechanical_equal = normalized_1 == normalized_2
    if mechanical_equal:
        return PJPromptPresentationsV3(
            mechanical_equal=True,
            active_equal_context_diff=False,
            canonical=None,
            reversed=None,
        )

    canonical_text = _render_pj_template(
        source_sequence, candidate_1_sequence, candidate_2_sequence
    )
    reverse_text = _render_pj_template(
        source_sequence, candidate_2_sequence, candidate_1_sequence
    )
    _assert_prompt_does_not_expose_metadata(canonical_text, validated)
    _assert_prompt_does_not_expose_metadata(reverse_text, validated)
    return PJPromptPresentationsV3(
        mechanical_equal=False,
        active_equal_context_diff=active_equal,
        canonical=_rendered_prompt(
            candidate_id=PJ_COMMON_CANDIDATE_ID,
            prompt_sha256=PJ_COMMON_PROMPT_SHA256,
            rendered=canonical_text,
        ),
        reversed=_rendered_prompt(
            candidate_id=PJ_COMMON_CANDIDATE_ID,
            prompt_sha256=PJ_COMMON_PROMPT_SHA256,
            rendered=reverse_text,
        ),
    )


def parse_sf_bt_semantic_response_v3(raw_response_text: str) -> dict[str, Any]:
    row = _parse_closed_json_object(raw_response_text, path="$.raw_response")
    require_exact_keys(row, required={"score", "flags", "note"}, path="$.raw_response")
    score = require_int(row["score"], path="$.raw_response.score", minimum=0)
    if score not in {0, 25, 50, 75, 100}:
        raise ContractValidationError(
            "score_band",
            "$.raw_response.score",
            "score must be one of 0, 25, 50, 75, or 100",
        )
    flags = _validate_string_list(
        row["flags"],
        allowed=_SF_BT_FLAGS,
        maximum=6,
        path="$.raw_response.flags",
    )
    note = require_string(
        row["note"], path="$.raw_response.note", maximum=240
    )
    return {"score": score, "flags": flags, "note": note}


def parse_pj_response_v2(raw_response_text: str) -> dict[str, Any]:
    row = _parse_closed_json_object(raw_response_text, path="$.raw_response")
    require_exact_keys(
        row,
        required={"overall_verdict", "style_verdict", "tags", "note"},
        path="$.raw_response",
    )
    tags = _validate_string_list(
        row["tags"],
        allowed=_PJ_TAGS,
        maximum=3,
        path="$.raw_response.tags",
    )
    note = require_string(row["note"], path="$.raw_response.note", maximum=240)
    if len(note.split()) > 25:
        raise ContractValidationError(
            "note_length", "$.raw_response.note", "note exceeds 25 words"
        )
    return {
        "overall_verdict": require_enum(
            row["overall_verdict"],
            _PJ_VERDICTS,
            path="$.raw_response.overall_verdict",
        ),
        "style_verdict": require_enum(
            row["style_verdict"],
            _PJ_VERDICTS,
            path="$.raw_response.style_verdict",
        ),
        "tags": tags,
        "note": note,
    }


def _symmetrize_pj_context(
    first: list[dict[str, Any]],
    second: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    left = [dict(row) for row in first]
    right = [dict(row) for row in second]
    for index, (left_row, right_row) in enumerate(zip(left, right, strict=True)):
        if left_row["block_id"] != right_row["block_id"]:
            raise ContractValidationError(
                "context_alignment",
                f"$.candidates[*].blocks[{index}]",
                "candidate context rows must align before rendering",
            )
        if left_row["role"] == "active":
            continue
        if left_row["text"] is None or right_row["text"] is None:
            left_row["text"] = CONTEXT_NOT_AVAILABLE
            right_row["text"] = CONTEXT_NOT_AVAILABLE
    return left, right


def _render_block_sequence(rows: list[dict[str, Any]]) -> str:
    rendered: list[str] = []
    for row in rows:
        text = row["text"]
        if text is None:
            text = CONTEXT_NOT_AVAILABLE
        rendered.append(
            f"[{row['role'].upper()} block_id={row['block_id']} "
            f"block_type={row['block_type']}]\n{text}"
        )
    return "\n\n".join(rendered)


def _render_pj_template(source: str, first: str, second: str) -> str:
    return _render_template(
        _PJ_COMMON_TEMPLATE,
        {
            "source_block_sequence": source,
            "candidate_1_block_sequence": first,
            "candidate_2_block_sequence": second,
        },
    )


def _render_template(template: str, values: Mapping[str, str]) -> str:
    fields = _TEMPLATE_FIELD_RE.findall(template)
    if set(fields) != set(values) or len(fields) != len(values):
        raise RuntimeError("prompt template fields do not match renderer values")
    return _TEMPLATE_FIELD_RE.sub(lambda match: values[match.group(1)], template)


def _rendered_prompt(
    *, candidate_id: str, prompt_sha256: str, rendered: str
) -> RenderedPromptV3:
    return RenderedPromptV3(
        candidate_id=candidate_id,
        prompt_sha256=prompt_sha256,
        rendered_prompt=rendered,
        rendered_prompt_sha256=hashlib.sha256(rendered.encode("utf-8")).hexdigest(),
    )


def _mechanical_normalize(value: str) -> str:
    normalized = unicodedata.normalize(
        "NFC", value.replace("\r\n", "\n").replace("\r", "\n")
    )
    return "\n".join(line.rstrip() for line in normalized.split("\n"))


def _assert_prompt_does_not_expose_metadata(
    rendered: str, packet: Mapping[str, Any]
) -> None:
    binding = packet.get("binding", {})
    forbidden_values = {
        packet.get("packet_id"),
        packet.get("integrity", {}).get("packet_sha256"),
        packet.get("producer", {}).get("component"),
        packet.get("producer", {}).get("code_commit"),
    }
    for field in (
        "plan_id",
        "plan_sha256",
        "config_sha256",
        "input_set_sha256",
        "job_id",
        "unit_id",
        "stage1_packet_id",
        "stage1_packet_sha256",
        "stage1_result_id",
        "stage1_result_sha256",
        "source_text_sha256",
        "back_translation_sha256",
    ):
        forbidden_values.add(binding.get(field))
    for value in forbidden_values:
        if isinstance(value, str) and len(value) >= 8 and value in rendered:
            raise ContractValidationError(
                "prompt_identifier_leak",
                "$.rendered_prompt",
                "model-visible prompt contains hidden packet metadata",
            )


def _parse_closed_json_object(raw: str, *, path: str) -> Mapping[str, Any]:
    if not isinstance(raw, str):
        raise ContractValidationError("type", path, "raw response must be text")
    try:
        parsed = json.loads(
            raw,
            object_pairs_hook=_reject_duplicate_json_keys,
            parse_constant=_reject_json_constant,
        )
    except (json.JSONDecodeError, ValueError) as exc:
        raise ContractValidationError(
            "response_json", path, "response is not strict JSON"
        ) from exc
    return require_mapping(parsed, path=path)


def _validate_string_list(
    value: Any, *, allowed: frozenset[str], maximum: int, path: str
) -> list[str]:
    rows = require_list(value, path=path)
    if len(rows) > maximum:
        raise ContractValidationError(
            "array_too_long", path, f"array may contain at most {maximum} values"
        )
    result = [
        require_enum(item, allowed, path=f"{path}[{index}]")
        for index, item in enumerate(rows)
    ]
    require_unique(result, path=path)
    return result


def _reject_duplicate_json_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _reject_json_constant(value: str) -> Any:
    raise ValueError(f"non-finite JSON constant: {value}")


def _assert_template_hash(template: str, expected: str) -> None:
    actual = hashlib.sha256(template.encode("utf-8")).hexdigest()
    if actual != expected:
        raise RuntimeError(f"prompt template hash drift: expected {expected}, got {actual}")


_assert_template_hash(_SF_BT_REVERSE_TEMPLATE, SF_BT_REVERSE_PROMPT_SHA256)
_assert_template_hash(_SF_BT_SEMANTIC_TEMPLATE, SF_BT_SEMANTIC_PROMPT_SHA256)
_assert_template_hash(_PJ_COMMON_TEMPLATE, PJ_COMMON_PROMPT_SHA256)
