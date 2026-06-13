from __future__ import annotations

import csv
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence


@dataclass(frozen=True)
class HumanRating:
    scope_id: str
    comparison: str
    human_winner: str | None = None
    human_score: float | None = None


def spearman(judge_scores: Sequence[float], human_scores: Sequence[float]) -> float:
    """Spearman rank correlation with average ranks for ties."""

    if len(judge_scores) != len(human_scores):
        raise ValueError("judge_scores and human_scores must have the same length")
    if len(judge_scores) < 2:
        return 0.0
    judge_ranks = _average_ranks([float(item) for item in judge_scores])
    human_ranks = _average_ranks([float(item) for item in human_scores])
    return _pearson(judge_ranks, human_ranks)


def pairwise_agreement(
    judge_verdicts: Sequence[str],
    human_verdicts: Sequence[str],
) -> float:
    if len(judge_verdicts) != len(human_verdicts):
        raise ValueError("judge_verdicts and human_verdicts must have the same length")
    if not judge_verdicts:
        return 0.0
    matches = 0
    for judge, human in zip(judge_verdicts, human_verdicts):
        if _normalize_winner(judge) == _normalize_winner(human):
            matches += 1
    return matches / len(judge_verdicts)


def load_human_ratings(csv_path: str | Path) -> list[HumanRating]:
    path = Path(csv_path)
    if not path.exists():
        return []
    rows: list[HumanRating] = []
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        for row in reader:
            score_text = str(row.get("human_score") or "").strip()
            rows.append(
                HumanRating(
                    scope_id=str(row.get("scope_id") or row.get("block_id") or ""),
                    comparison=str(row.get("comparison") or ""),
                    human_winner=_optional_winner(row.get("human_winner")),
                    human_score=float(score_text) if score_text else None,
                )
            )
    return rows


def calibration_summary(
    *,
    judge_scores: Sequence[float] = (),
    human_scores: Sequence[float] = (),
    judge_verdicts: Sequence[str] = (),
    human_verdicts: Sequence[str] = (),
) -> dict[str, float | bool | int | None]:
    has_scores = len(judge_scores) >= 2 and len(judge_scores) == len(human_scores)
    has_verdicts = len(judge_verdicts) > 0 and len(judge_verdicts) == len(human_verdicts)
    rho = spearman(judge_scores, human_scores) if has_scores else None
    agreement = pairwise_agreement(judge_verdicts, human_verdicts) if has_verdicts else None
    return {
        "calibrated": bool(has_scores or has_verdicts),
        "spearman_rho": rho,
        "pairwise_agreement": agreement,
        "n_scores": len(judge_scores) if has_scores else 0,
        "n_pairwise": len(judge_verdicts) if has_verdicts else 0,
    }


def _average_ranks(values: list[float]) -> list[float]:
    indexed = sorted(enumerate(values), key=lambda item: item[1])
    ranks = [0.0] * len(values)
    position = 0
    while position < len(indexed):
        end = position + 1
        while end < len(indexed) and indexed[end][1] == indexed[position][1]:
            end += 1
        average_rank = (position + 1 + end) / 2
        for index in range(position, end):
            ranks[indexed[index][0]] = average_rank
        position = end
    return ranks


def _pearson(left: list[float], right: list[float]) -> float:
    mean_left = sum(left) / len(left)
    mean_right = sum(right) / len(right)
    numerator = sum((x - mean_left) * (y - mean_right) for x, y in zip(left, right))
    denom_left = math.sqrt(sum((x - mean_left) ** 2 for x in left))
    denom_right = math.sqrt(sum((y - mean_right) ** 2 for y in right))
    denominator = denom_left * denom_right
    if denominator == 0:
        return 0.0
    return numerator / denominator


def _optional_winner(value: object) -> str | None:
    text = str(value or "").strip()
    return _normalize_winner(text) if text else None


def _normalize_winner(value: object) -> str:
    text = str(value or "").casefold().strip()
    if text in {"a", "s0", "left", "1", "ban 1", "bản 1"}:
        return "a"
    if text in {"b", "s1", "right", "2", "ban 2", "bản 2"}:
        return "b"
    return "tie"
