from __future__ import annotations

from dataclasses import dataclass
import hashlib
import itertools
import json
from statistics import fmean
from typing import Any, Mapping, Sequence

from pipeline.eval.common_input_v1 import CommonEvaluationInputV1
from pipeline.eval.contracts_v1 import ContractValidationError
from pipeline.eval.two_wave_coverage_v1 import validate_two_wave_sample_coverage_v1
from pipeline.eval.two_wave_sampling_v1 import (
    BENCHMARK_ARM_IDS_V1,
    build_two_wave_work_plan_v1,
    validate_two_wave_sampling_manifest_v1,
)
from pipeline.llm_backend.contracts_v1 import canonical_sha256


__all__ = [
    "MTQ5_CRITERIA_V1",
    "PreparedMtq5ItemV1",
    "aggregate_mtq5_results_v1",
    "build_mtq5_packet_evidence_v1",
    "parse_mtq5_response_v1",
    "prepare_mtq5_items_v1",
]


MTQ5_METHOD_ID = "pj"
MTQ5_METHOD_VERSION = "mtq5_v1_0"
MTQ5_ROLE_ID = "evaluation.d2l.mtq5.judge.cluster.v1"
MTQ5_CRITERIA_V1 = (
    "adequacy",
    "fluency",
    "terminology_consistency",
    "coherence",
    "structural_integrity",
    "overall",
)
_ORIENTATIONS = ("canonical", "reversed")
_ISSUE_SIDES = frozenset({"1", "2", "b"})
_ISSUE_CRITERIA = frozenset({"A", "F", "T", "C", "S"})
_ISSUE_CODES = frozenset(
    {
        "mistranslation",
        "omission",
        "addition",
        "numeric_error",
        "negation_error",
        "terminology_error",
        "grammar_error",
        "unnatural_wording",
        "coherence_break",
        "structure_loss",
        "formatting_error",
    }
)

_PROMPT_HEADER = """You are an impartial judge of two Vietnamese translations of the same five-block English passage.

Score each candidate independently against the English source before comparing them. Do not reward literal wording by itself. Do not reward elegant Vietnamese that changes, omits, or invents meaning.

Use these six values in this exact order:
A = adequacy and semantic faithfulness
F = Vietnamese fluency and grammatical naturalness
T = technical terminology correctness and consistency across the five blocks
C = local coherence and logical continuity across the five blocks
S = structural integrity: completeness, equations, markup, emphasis, numbers, and block boundaries
O = holistic translation quality, judged separately rather than calculated from A/F/T/C/S

Use integer bands only:
5 = no material defect
4 = minor defect with meaning and usability preserved
3 = noticeable defect but the passage remains broadly usable
2 = major defect affecting important meaning or usability
1 = severe failure, contradiction, extensive omission, or unusable output

Return one compact JSON object only, with exactly this shape:
{"c1":[A,F,T,C,S,O],"c2":[A,F,T,C,S,O],"issues":[[block_no,"1|2|b","A|F|T|C|S","issue_code"]],"note":"at most 25 English words"}

Rules:
- c1 and c2 must each contain exactly six integers from 1 through 5.
- block_no is 1 through 5. "b" means the issue affects both candidates.
- issue_code must be one of: mistranslation, omission, addition, numeric_error, negation_error, terminology_error, grammar_error, unnatural_wording, coherence_break, structure_loss, formatting_error.
- Report at most eight material issues. Use [] when there is no material issue.
- Do not add fields, markdown, explanations, candidate names, or copied passages.
"""


@dataclass(frozen=True, slots=True)
class PreparedMtq5ItemV1:
    item_id: str
    cluster_id: str
    chapter_id: str
    pair_id: str
    orientation: str
    slot_to_arm: tuple[tuple[int, str], tuple[int, str]]
    block_ids: tuple[str, ...]
    packet_sha256: str
    rendered_prompt: str
    rendered_prompt_sha256: str


def prepare_mtq5_items_v1(
    chapter_inputs: Mapping[str, CommonEvaluationInputV1],
    sampling_manifest: Mapping[str, Any],
    sample_coverage: Mapping[str, Any],
    *,
    active_wave: str,
    incremental_only: bool = False,
) -> tuple[PreparedMtq5ItemV1, ...]:
    if not isinstance(incremental_only, bool):
        raise ContractValidationError(
            "type", "$.incremental_only", "incremental_only must be boolean"
        )
    manifest = validate_two_wave_sampling_manifest_v1(sampling_manifest)
    coverage = validate_two_wave_sample_coverage_v1(
        sample_coverage,
        sampling_manifest=manifest,
        chapter_inputs=chapter_inputs,
    )
    if active_wave != coverage["active_wave"]:
        raise ContractValidationError(
            "wave_binding",
            "$.active_wave",
            "MTQ-5 active wave differs from sample coverage",
        )
    if coverage["coverage_status"] != "ready":
        raise ContractValidationError(
            "sample_coverage",
            "$.sample_coverage.coverage_status",
            "MTQ-5 cannot prepare packets until every frozen arm/block is available",
        )

    blocks_by_id = {
        block.block_id: block
        for common_input in chapter_inputs.values()
        for block in common_input.blocks
    }
    translations = {
        (row.arm_id, row.block_id): row
        for common_input in chapter_inputs.values()
        for row in common_input.translations
    }
    clusters_by_id = {
        cluster["cluster_id"]: cluster for cluster in manifest["clusters"]
    }
    work_plan = build_two_wave_work_plan_v1(manifest, active_wave=active_wave)
    selected_cluster_ids = (
        work_plan["incremental_cluster_ids"]
        if incremental_only
        else work_plan["active_cluster_ids"]
    )
    manifest_sha256 = manifest["integrity"]["manifest_sha256"]
    coverage_sha256 = coverage["integrity"]["coverage_sha256"]

    items: list[PreparedMtq5ItemV1] = []
    for cluster_id in selected_cluster_ids:
        cluster = clusters_by_id[cluster_id]
        block_ids = tuple(cluster["block_ids"])
        for first_arm, second_arm in itertools.combinations(
            BENCHMARK_ARM_IDS_V1, 2
        ):
            pair_id = f"{first_arm}__{second_arm}"
            for orientation in _ORIENTATIONS:
                slot_to_arm = (
                    ((1, first_arm), (2, second_arm))
                    if orientation == "canonical"
                    else ((1, second_arm), (2, first_arm))
                )
                blocks = [
                    {
                        "block_no": index,
                        "block_id": block_id,
                        "source_text": blocks_by_id[block_id].source_text,
                        "candidate_1": _target_text(
                            translations, slot_to_arm[0][1], block_id
                        ),
                        "candidate_2": _target_text(
                            translations, slot_to_arm[1][1], block_id
                        ),
                    }
                    for index, block_id in enumerate(block_ids, start=1)
                ]
                packet = {
                    "method_id": MTQ5_METHOD_ID,
                    "method_version": MTQ5_METHOD_VERSION,
                    "sampling_manifest_sha256": manifest_sha256,
                    "sample_coverage_sha256": coverage_sha256,
                    "active_wave": active_wave,
                    "cluster_id": cluster_id,
                    "chapter_id": cluster["chapter_id"],
                    "pair_id": pair_id,
                    "orientation": orientation,
                    "slot_to_arm": [
                        {"slot": slot, "arm_id": arm_id}
                        for slot, arm_id in slot_to_arm
                    ],
                    "blocks": blocks,
                }
                packet_sha256 = canonical_sha256(packet)
                prompt = _render_prompt(blocks)
                prompt_sha256 = hashlib.sha256(prompt.encode("utf-8")).hexdigest()
                items.append(
                    PreparedMtq5ItemV1(
                        item_id=(
                            f"mtq5_{cluster_id}_{pair_id}_{orientation}_"
                            f"{packet_sha256[:16]}"
                        ),
                        cluster_id=cluster_id,
                        chapter_id=cluster["chapter_id"],
                        pair_id=pair_id,
                        orientation=orientation,
                        slot_to_arm=slot_to_arm,
                        block_ids=block_ids,
                        packet_sha256=packet_sha256,
                        rendered_prompt=prompt,
                        rendered_prompt_sha256=prompt_sha256,
                    )
                )
    expected_count = (
        len(selected_cluster_ids)
        * len(tuple(itertools.combinations(BENCHMARK_ARM_IDS_V1, 2)))
        * len(_ORIENTATIONS)
    )
    if len(items) != expected_count or len({item.item_id for item in items}) != len(
        items
    ):
        raise ContractValidationError(
            "item_exact_cover",
            "$.prepared_items",
            "MTQ-5 packet preparation did not exact-cover cluster/pair/orientation work",
        )
    return tuple(items)


def parse_mtq5_response_v1(raw_response_text: str) -> dict[str, Any]:
    try:
        payload = json.loads(raw_response_text)
    except (TypeError, json.JSONDecodeError) as exc:
        raise ContractValidationError(
            "json",
            "$.provider_response",
            "MTQ-5 response must be valid JSON",
        ) from exc
    if not isinstance(payload, Mapping):
        raise ContractValidationError(
            "type", "$.provider_response", "MTQ-5 response root must be an object"
        )
    root = dict(payload)
    _require_exact_keys(root, {"c1", "c2", "issues", "note"}, "$.provider_response")
    candidate_1 = _validate_score_vector(root["c1"], "$.provider_response.c1")
    candidate_2 = _validate_score_vector(root["c2"], "$.provider_response.c2")
    issues = _validate_issues(root["issues"], "$.provider_response.issues")
    note = root["note"]
    if not isinstance(note, str):
        raise ContractValidationError(
            "type", "$.provider_response.note", "note must be a string"
        )
    if "\n" in note or "\r" in note or len(note) > 180 or len(note.split()) > 25:
        raise ContractValidationError(
            "note",
            "$.provider_response.note",
            "note must be one compact line of at most 25 words",
        )
    return {"c1": candidate_1, "c2": candidate_2, "issues": issues, "note": note}


def aggregate_mtq5_results_v1(
    *,
    sampling_manifest: Mapping[str, Any],
    sample_coverage: Mapping[str, Any],
    prepared_items: Sequence[PreparedMtq5ItemV1],
    outputs: Mapping[str, Mapping[str, Any] | str],
    created_at: str,
) -> dict[str, Any]:
    manifest = validate_two_wave_sampling_manifest_v1(sampling_manifest)
    coverage = validate_two_wave_sample_coverage_v1(
        sample_coverage, sampling_manifest=manifest
    )
    if coverage["coverage_status"] != "ready":
        raise ContractValidationError(
            "sample_coverage",
            "$.sample_coverage.coverage_status",
            "MTQ-5 cannot aggregate a blocked sample",
        )
    item_map = {item.item_id: item for item in prepared_items}
    if len(item_map) != len(prepared_items):
        raise ContractValidationError(
            "duplicate_item", "$.prepared_items", "item IDs must be unique"
        )
    if set(outputs) != set(item_map):
        raise ContractValidationError(
            "exact_cover",
            "$.outputs",
            "MTQ-5 outputs must exact-cover every prepared orientation",
        )

    parsed_rows: dict[tuple[str, str], list[dict[str, Any]]] = {}
    arm_vectors: dict[str, list[list[int]]] = {
        arm_id: [] for arm_id in BENCHMARK_ARM_IDS_V1
    }
    for item in prepared_items:
        raw = outputs[item.item_id]
        parsed = parse_mtq5_response_v1(
            raw if isinstance(raw, str) else json.dumps(raw, ensure_ascii=True)
        )
        slot_scores = {1: parsed["c1"], 2: parsed["c2"]}
        mapped_scores = {
            arm_id: slot_scores[slot] for slot, arm_id in item.slot_to_arm
        }
        for arm_id, scores in mapped_scores.items():
            arm_vectors[arm_id].append(scores)
        slot_mapping = dict(item.slot_to_arm)
        mapped_issues = []
        for block_no, side, criterion, code in parsed["issues"]:
            affected_arms = (
                [slot_mapping[1]]
                if side == "1"
                else [slot_mapping[2]]
                if side == "2"
                else [slot_mapping[1], slot_mapping[2]]
            )
            mapped_issues.append(
                {
                    "block_id": item.block_ids[block_no - 1],
                    "affected_arm_ids": affected_arms,
                    "criterion": criterion,
                    "issue_code": code,
                }
            )
        parsed_rows.setdefault((item.cluster_id, item.pair_id), []).append(
            {
                "item_id": item.item_id,
                "orientation": item.orientation,
                "slot_to_arm": [
                    {"slot": slot, "arm_id": arm_id}
                    for slot, arm_id in item.slot_to_arm
                ],
                "scores": {
                    arm_id: _score_row(scores)
                    for arm_id, scores in mapped_scores.items()
                },
                "issues": mapped_issues,
                "note": parsed["note"],
            }
        )

    cluster_pair_rows: list[dict[str, Any]] = []
    warning_count = 0
    cluster_by_id = {
        cluster["cluster_id"]: cluster for cluster in manifest["clusters"]
    }
    for cluster_id in manifest["waves"][coverage["active_wave"]]["cluster_ids"]:
        for first_arm, second_arm in itertools.combinations(
            BENCHMARK_ARM_IDS_V1, 2
        ):
            pair_id = f"{first_arm}__{second_arm}"
            rows = parsed_rows.get((cluster_id, pair_id), [])
            if len(rows) != 2 or {
                row["orientation"] for row in rows
            } != set(_ORIENTATIONS):
                raise ContractValidationError(
                    "orientation_cover",
                    f"$.pairs.{cluster_id}.{pair_id}",
                    "each cluster pair needs canonical and reversed orientations",
                )
            rows.sort(key=lambda row: _ORIENTATIONS.index(row["orientation"]))
            means = {
                arm_id: _mean_score_rows(
                    [row["scores"][arm_id] for row in rows]
                )
                for arm_id in (first_arm, second_arm)
            }
            deltas = {
                arm_id: {
                    criterion: abs(
                        rows[0]["scores"][arm_id][criterion]
                        - rows[1]["scores"][arm_id][criterion]
                    )
                    for criterion in MTQ5_CRITERIA_V1
                }
                for arm_id in (first_arm, second_arm)
            }
            orientation_winners = [
                _winner(
                    first_arm,
                    row["scores"][first_arm]["overall"],
                    second_arm,
                    row["scores"][second_arm]["overall"],
                )
                for row in rows
            ]
            max_delta = max(
                delta for arm_delta in deltas.values() for delta in arm_delta.values()
            )
            winner_flip = (
                orientation_winners[0] != "tie"
                and orientation_winners[1] != "tie"
                and orientation_winners[0] != orientation_winners[1]
            )
            warning = max_delta > 1 or winner_flip
            warning_count += int(warning)
            cluster_pair_rows.append(
                {
                    "cluster_id": cluster_id,
                    "chapter_id": cluster_by_id[cluster_id]["chapter_id"],
                    "pair_id": pair_id,
                    "arm_ids": [first_arm, second_arm],
                    "orientation_rows": rows,
                    "mean_scores": means,
                    "overall_delta_arm_1_minus_arm_2": (
                        means[first_arm]["overall"] - means[second_arm]["overall"]
                    ),
                    "overall_winner": _winner(
                        first_arm,
                        means[first_arm]["overall"],
                        second_arm,
                        means[second_arm]["overall"],
                    ),
                    "orientation_check": {
                        "max_same_arm_band_delta": max_delta,
                        "overall_winners_by_orientation": orientation_winners,
                        "winner_flip": winner_flip,
                        "warning": warning,
                        "per_arm_deltas": deltas,
                    },
                }
            )

    body = {
        "schema_id": "EvaluationMtq5ReportV1",
        "schema_version": "1.0.0",
        "method_id": MTQ5_METHOD_ID,
        "method_version": MTQ5_METHOD_VERSION,
        "authority": "measurement_only",
        "created_at": created_at,
        "sampling_manifest_sha256": manifest["integrity"]["manifest_sha256"],
        "sample_coverage_sha256": coverage["integrity"]["coverage_sha256"],
        "scope": {
            "active_wave": coverage["active_wave"],
            "cluster_count": manifest["waves"][coverage["active_wave"]][
                "cluster_count"
            ],
            "block_count": manifest["waves"][coverage["active_wave"]]["block_count"],
            "arm_ids": list(BENCHMARK_ARM_IDS_V1),
            "pair_count": len(
                tuple(itertools.combinations(BENCHMARK_ARM_IDS_V1, 2))
            ),
            "orientations_per_pair": 2,
            "logical_judgment_count": len(prepared_items),
        },
        "scale": {
            "minimum": 1,
            "maximum": 5,
            "criteria": list(MTQ5_CRITERIA_V1),
            "overall_policy": "separately_judged_not_arithmetic_mean",
        },
        "arm_summaries": [
            {
                "arm_id": arm_id,
                "judgment_count": len(arm_vectors[arm_id]),
                "mean_scores": _mean_vectors(arm_vectors[arm_id]),
            }
            for arm_id in BENCHMARK_ARM_IDS_V1
        ],
        "cluster_pair_rows": cluster_pair_rows,
        "orientation_warning_count": warning_count,
        "interpretation": [
            "Criterion means are descriptive diagnostics, not a synthetic headline score.",
            "Overall is the judge's separate holistic band.",
            "Provider call count is determined by the sealed batching profile, not this logical work report.",
        ],
    }
    return {**body, "report_sha256": canonical_sha256(body)}


def build_mtq5_packet_evidence_v1(
    *,
    sampling_manifest_sha256: str,
    sample_coverage_sha256: str,
    prepared_items: Sequence[PreparedMtq5ItemV1],
) -> dict[str, Any]:
    body = {
        "schema_id": "EvaluationMtq5PacketEvidenceV1",
        "schema_version": "1.0.0",
        "method_id": MTQ5_METHOD_ID,
        "method_version": MTQ5_METHOD_VERSION,
        "sampling_manifest_sha256": sampling_manifest_sha256,
        "sample_coverage_sha256": sample_coverage_sha256,
        "items": [
            {
                "item_id": item.item_id,
                "cluster_id": item.cluster_id,
                "chapter_id": item.chapter_id,
                "pair_id": item.pair_id,
                "orientation": item.orientation,
                "slot_to_arm": [
                    {"slot": slot, "arm_id": arm_id}
                    for slot, arm_id in item.slot_to_arm
                ],
                "block_ids": list(item.block_ids),
                "packet_sha256": item.packet_sha256,
                "rendered_prompt_sha256": item.rendered_prompt_sha256,
            }
            for item in prepared_items
        ],
    }
    return {**body, "packets_sha256": canonical_sha256(body)}


def _render_prompt(blocks: Sequence[Mapping[str, Any]]) -> str:
    rendered = [_PROMPT_HEADER.rstrip()]
    for block in blocks:
        rendered.extend(
            (
                "",
                f"BEGIN BLOCK {block['block_no']}",
                "ENGLISH SOURCE:",
                str(block["source_text"]),
                "VIETNAMESE CANDIDATE 1:",
                str(block["candidate_1"]),
                "VIETNAMESE CANDIDATE 2:",
                str(block["candidate_2"]),
                f"END BLOCK {block['block_no']}",
            )
        )
    return "\n".join(rendered)


def _target_text(
    translation_map: Mapping[tuple[str, str], Any],
    arm_id: str,
    block_id: str,
) -> str:
    row = translation_map.get((arm_id, block_id))
    if (
        row is None
        or row.status != "translated"
        or not isinstance(row.target_text, str)
        or not row.target_text
    ):
        raise ContractValidationError(
            "translation_cover",
            f"$.translations.{arm_id}.{block_id}",
            "MTQ-5 requires a translated target for every frozen arm and block",
        )
    return row.target_text


def _validate_score_vector(value: Any, path: str) -> list[int]:
    if not isinstance(value, list) or len(value) != 6:
        raise ContractValidationError(
            "score_vector", path, "score vector must contain exactly six integers"
        )
    result = []
    for index, score in enumerate(value):
        if (
            isinstance(score, bool)
            or not isinstance(score, int)
            or score < 1
            or score > 5
        ):
            raise ContractValidationError(
                "score_band",
                f"{path}[{index}]",
                "score must be an integer from 1 through 5",
            )
        result.append(score)
    return result


def _validate_issues(value: Any, path: str) -> list[list[Any]]:
    if not isinstance(value, list) or len(value) > 8:
        raise ContractValidationError(
            "issues", path, "issues must be an array of at most eight rows"
        )
    result: list[list[Any]] = []
    seen: set[tuple[Any, ...]] = set()
    for index, item in enumerate(value):
        row_path = f"{path}[{index}]"
        if not isinstance(item, list) or len(item) != 4:
            raise ContractValidationError(
                "issue_row", row_path, "issue row must contain exactly four values"
            )
        block_no, side, criterion, issue_code = item
        if isinstance(block_no, bool) or not isinstance(block_no, int) or not 1 <= block_no <= 5:
            raise ContractValidationError(
                "issue_block", f"{row_path}[0]", "block number must be 1 through 5"
            )
        if side not in _ISSUE_SIDES:
            raise ContractValidationError(
                "issue_side", f"{row_path}[1]", "invalid issue side"
            )
        if criterion not in _ISSUE_CRITERIA:
            raise ContractValidationError(
                "issue_criterion", f"{row_path}[2]", "invalid issue criterion"
            )
        if issue_code not in _ISSUE_CODES:
            raise ContractValidationError(
                "issue_code", f"{row_path}[3]", "invalid issue code"
            )
        normalized = (block_no, side, criterion, issue_code)
        if normalized in seen:
            raise ContractValidationError(
                "duplicate_issue", row_path, "duplicate issue row"
            )
        seen.add(normalized)
        result.append(list(normalized))
    return result


def _score_row(vector: Sequence[int]) -> dict[str, int]:
    return dict(zip(MTQ5_CRITERIA_V1, vector, strict=True))


def _mean_score_rows(rows: Sequence[Mapping[str, int]]) -> dict[str, float]:
    return {
        criterion: fmean(float(row[criterion]) for row in rows)
        for criterion in MTQ5_CRITERIA_V1
    }


def _mean_vectors(vectors: Sequence[Sequence[int]]) -> dict[str, float]:
    if not vectors:
        raise ContractValidationError(
            "empty_scores", "$.outputs", "every arm must receive judgments"
        )
    return {
        criterion: fmean(float(vector[index]) for vector in vectors)
        for index, criterion in enumerate(MTQ5_CRITERIA_V1)
    }


def _winner(first_arm: str, first: float, second_arm: str, second: float) -> str:
    if first > second:
        return first_arm
    if second > first:
        return second_arm
    return "tie"


def _require_exact_keys(
    value: Mapping[str, Any], required: set[str], path: str
) -> None:
    observed = set(value)
    if observed != required:
        missing = sorted(required - observed)
        unknown = sorted(observed - required)
        raise ContractValidationError(
            "keys", path, f"missing={missing!r}; unknown={unknown!r}"
        )
