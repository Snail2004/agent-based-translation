from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass
from typing import Any

from pipeline.agents.judge_client import JudgeClient


METRIC_VERSION = "judge_v1"
JSON_FORMAT = {"type": "json_object"}


@dataclass(frozen=True)
class PairwiseVerdict:
    order: str
    display_winner: str
    mapped_winner: str
    rationale: str
    raw: dict[str, Any]


@dataclass(frozen=True)
class PairwiseResult:
    winner: str
    confidence: str
    rationale: str
    verdicts: list[PairwiseVerdict]

    def to_dict(self) -> dict[str, Any]:
        return {
            "winner": self.winner,
            "confidence": self.confidence,
            "rationale": self.rationale,
            "verdicts": [asdict(item) for item in self.verdicts],
        }


@dataclass(frozen=True)
class GembaResult:
    adequacy: float
    fluency: float
    style_voice: float
    fidelity_no_adddrop: float
    rationale: str
    raw: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def pairwise(
    client: JudgeClient,
    *,
    source: str,
    vi_a: str,
    vi_b: str,
    scope_id: str = "",
) -> PairwiseResult:
    """Run blind pairwise judge twice with swapped display order."""

    first = _pairwise_once(
        client,
        source=source,
        displayed_1=vi_a,
        displayed_2=vi_b,
        map_1="a",
        map_2="b",
        tag=f"pairwise:{scope_id}:ab",
        order="ab",
    )
    second = _pairwise_once(
        client,
        source=source,
        displayed_1=vi_b,
        displayed_2=vi_a,
        map_1="b",
        map_2="a",
        tag=f"pairwise:{scope_id}:ba",
        order="ba",
    )

    winners = [first.mapped_winner, second.mapped_winner]
    if winners[0] == winners[1] and winners[0] in {"a", "b"}:
        winner = winners[0]
        confidence = "high"
    else:
        winner = "tie"
        confidence = "low"

    rationale = " | ".join(
        item.rationale for item in [first, second] if item.rationale
    )
    return PairwiseResult(
        winner=winner,
        confidence=confidence,
        rationale=rationale,
        verdicts=[first, second],
    )


def gemba(
    client: JudgeClient,
    *,
    source: str,
    vi: str,
    scope_id: str = "",
    label: str = "",
) -> GembaResult:
    """Direct quality assessment. Diagnostic only; pairwise remains primary."""

    result = client.call(
        _gemba_messages(source, vi),
        response_format=JSON_FORMAT,
        tag=f"gemba:{scope_id}:{label}",
    )
    data = _json_dict(result.parsed_json)
    return GembaResult(
        adequacy=_score(data.get("adequacy")),
        fluency=_score(data.get("fluency")),
        style_voice=_score(data.get("style_voice")),
        fidelity_no_adddrop=_score(data.get("fidelity_no_adddrop")),
        rationale=str(data.get("rationale") or ""),
        raw=data,
    )


def mattr(text: str, window: int = 50) -> float:
    """Moving-average type-token ratio for quick lexical-diversity diagnostics."""

    tokens = re.findall(r"\w+", text.casefold(), flags=re.UNICODE)
    if not tokens:
        return 0.0
    if window <= 0:
        raise ValueError("window must be positive")
    if len(tokens) <= window:
        return len(set(tokens)) / len(tokens)
    ratios = []
    for start in range(0, len(tokens) - window + 1):
        chunk = tokens[start : start + window]
        ratios.append(len(set(chunk)) / window)
    return sum(ratios) / len(ratios)


def _pairwise_once(
    client: JudgeClient,
    *,
    source: str,
    displayed_1: str,
    displayed_2: str,
    map_1: str,
    map_2: str,
    tag: str,
    order: str,
) -> PairwiseVerdict:
    result = client.call(
        _pairwise_messages(source, displayed_1, displayed_2),
        response_format=JSON_FORMAT,
        tag=tag,
    )
    data = _json_dict(result.parsed_json)
    display_winner = _normalize_display_winner(data.get("winner"))
    mapped = "tie"
    if display_winner == "1":
        mapped = map_1
    elif display_winner == "2":
        mapped = map_2
    return PairwiseVerdict(
        order=order,
        display_winner=display_winner,
        mapped_winner=mapped,
        rationale=str(data.get("rationale") or ""),
        raw=data,
    )


def _pairwise_messages(
    source: str,
    displayed_1: str,
    displayed_2: str,
) -> list[dict[str, str]]:
    system = (
        "You are an independent translation-quality judge. "
        "Compare two Vietnamese translations of the same English source. "
        "Do not infer system names or model identities. "
        "Choose the better translation by meaning preservation, Vietnamese fluency, "
        "style/voice, and avoiding added or omitted content. "
        "Return only JSON with keys winner and rationale. "
        "winner must be one of: 1, 2, tie."
    )
    user = (
        "ENGLISH SOURCE\n"
        f"{source}\n\n"
        "Bản 1\n"
        f"{displayed_1}\n\n"
        "Bản 2\n"
        f"{displayed_2}\n\n"
        'Return JSON: {"winner":"1|2|tie","rationale":"short reason"}'
    )
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


def _gemba_messages(source: str, vi: str) -> list[dict[str, str]]:
    system = (
        "You are an independent translation-quality judge. "
        "Score one Vietnamese translation of an English source. "
        "Use 0-100 integer scores. Return only JSON."
    )
    user = (
        "ENGLISH SOURCE\n"
        f"{source}\n\n"
        "VIETNAMESE TRANSLATION\n"
        f"{vi}\n\n"
        "Score these criteria: adequacy, fluency, style_voice, "
        "fidelity_no_adddrop. fidelity_no_adddrop penalizes hallucination, "
        "unjustified adaptation, and omission. "
        'Return JSON: {"adequacy":0,"fluency":0,"style_voice":0,'
        '"fidelity_no_adddrop":0,"rationale":"short reason"}'
    )
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


def _json_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            if isinstance(parsed, dict):
                return parsed
        except json.JSONDecodeError:
            pass
    return {}


def _normalize_display_winner(value: Any) -> str:
    text = str(value or "").casefold().strip()
    if text in {"1", "ban 1", "bản 1", "version 1", "translation 1"}:
        return "1"
    if text in {"2", "ban 2", "bản 2", "version 2", "translation 2"}:
        return "2"
    return "tie"


def _score(value: Any) -> float:
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return 0.0
    return max(0.0, min(100.0, numeric))
