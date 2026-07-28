from __future__ import annotations

import re
import unicodedata
from typing import Any, Mapping, Sequence

from pipeline.eval.terminology_evidence.context_substitution.v2.contracts.provenance import (
    source_provenance_from_context,
)
from pipeline.eval.terminology_evidence.context_substitution.v2.contracts.common import (
    REQUIRED_SAME_SENSE_CONTEXT_TYPES,
)


_TYPE_ORDER = {
    "definition": 0,
    "typical_usage": 1,
    "domain_collocation": 2,
    "syntactic_variation": 3,
    "same_sense_difficult": 4,
    "unknown": 5,
}
def context_identity(context: Mapping[str, Any]) -> str:
    context_id = context.get("context_id")
    if isinstance(context_id, str) and context_id:
        return context_id
    return str(context["block_id"])


def candidate_profile(
    term: Mapping[str, Any], target: Mapping[str, Any]
) -> dict[str, Any]:
    return {
        "candidate_id": target["candidate_target_id"],
        "source_term": term["source_term"],
        "candidate_translation": target["target_vi"],
        "sense_id": term["sense_id"],
        "scope_id": term["scope_id"],
        "sense_contract": dict(term["sense_contract"]),
        "part_of_speech": term["part_of_speech"],
        "source_occurrences": list(term["source_occurrences"]),
        "candidate_generation": dict(target["candidate_generation"]),
    }


def selector_term_profile(term: Mapping[str, Any]) -> dict[str, Any]:
    """Return source-only selector input; candidate wording must not influence it."""

    return {
        "term_id": term["term_id"],
        "source_term": term["source_term"],
        "sense_id": term["sense_id"],
        "scope_id": term["scope_id"],
        "sense_contract": dict(term["sense_contract"]),
        "part_of_speech": term["part_of_speech"],
        "source_occurrences": list(term["source_occurrences"]),
    }


def selector_context_payload(context: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "context_id": context_identity(context),
        "chapter_id": context["chapter_id"],
        "block_type": context["block_type"],
        "source_text": context["source_text"],
        "source_sha256": context["source_text_sha256"],
        "source_provenance": source_provenance_from_context(context),
    }


def lexical_similarity(left: str, right: str) -> float:
    left_tokens = _lexical_tokens(left)
    right_tokens = _lexical_tokens(right)
    union = left_tokens | right_tokens
    if not union:
        return 1.0 if left == right else 0.0
    return len(left_tokens & right_tokens) / len(union)


def select_classified_contexts(
    *,
    contexts: Sequence[Mapping[str, Any]],
    annotations: Sequence[Mapping[str, Any]],
    max_same_sense: int = 5,
    max_contrastive: int = 2,
    similarity_threshold: float = 0.82,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    """Select a deterministic diverse subset after the model only classifies."""

    by_id = {context_identity(context): dict(context) for context in contexts}
    annotated = [
        {**by_id[annotation["context_id"]], "_annotation": dict(annotation)}
        for annotation in annotations
    ]
    same_sense = [
        row
        for row in annotated
        if row["_annotation"]["sense_relation"] == "SAME_SENSE"
        and row["_annotation"]["judgeability"] == "JUDGEABLE"
    ]
    contrastive = [
        row
        for row in annotated
        if row["_annotation"]["sense_relation"] == "CONTRASTIVE"
        and row["_annotation"]["judgeability"] == "JUDGEABLE"
    ]
    same_sense.sort(
        key=lambda row: (
            _TYPE_ORDER.get(row["_annotation"]["context_type"], 99),
            *_context_quality_key(row),
        )
    )
    selected = _select_required_type_coverage(
        same_sense,
        maximum=max_same_sense,
        similarity_threshold=similarity_threshold,
    )
    selected_ids = {context_identity(row) for row in selected}
    remaining = [
        row for row in same_sense if context_identity(row) not in selected_ids
    ]
    replacements = _select_diverse(
        remaining,
        maximum=len(remaining),
        similarity_threshold=similarity_threshold,
        fill_to_maximum=True,
        prior_rows=selected,
    )
    contrastive.sort(key=_context_quality_key)
    selected_contrastive = _select_diverse(
        contrastive,
        maximum=max_contrastive,
        similarity_threshold=similarity_threshold,
        fill_to_maximum=False,
    )
    return selected, replacements, selected_contrastive


def missing_required_context_types(
    selected: Sequence[Mapping[str, Any]],
) -> list[str]:
    selected_types = {
        str(row["_annotation"]["context_type"]) for row in selected
    }
    return [
        context_type
        for context_type in REQUIRED_SAME_SENSE_CONTEXT_TYPES
        if context_type not in selected_types
    ]


def _select_required_type_coverage(
    rows: Sequence[dict[str, Any]],
    *,
    maximum: int,
    similarity_threshold: float,
) -> list[dict[str, Any]]:
    selected: list[dict[str, Any]] = []
    for context_type in REQUIRED_SAME_SENSE_CONTEXT_TYPES:
        if len(selected) >= maximum:
            break
        match = next(
            (
                row
                for row in rows
                if row["_annotation"]["context_type"] == context_type
                and context_identity(row)
                not in {context_identity(prior) for prior in selected}
            ),
            None,
        )
        if match is not None:
            selected.append(match)
    if len(selected) >= maximum:
        return selected
    selected_ids = {context_identity(row) for row in selected}
    remaining = [
        row for row in rows if context_identity(row) not in selected_ids
    ]
    selected.extend(
        _select_diverse(
            remaining,
            maximum=maximum - len(selected),
            similarity_threshold=similarity_threshold,
            fill_to_maximum=True,
            prior_rows=selected,
        )
    )
    return selected


def _select_diverse(
    rows: Sequence[dict[str, Any]],
    *,
    maximum: int,
    similarity_threshold: float,
    fill_to_maximum: bool = True,
    prior_rows: Sequence[Mapping[str, Any]] = (),
) -> list[dict[str, Any]]:
    selected: list[dict[str, Any]] = []
    for row in rows:
        if any(
            lexical_similarity(row["source_text"], prior["source_text"])
            > similarity_threshold
            for prior in [*prior_rows, *selected]
        ):
            continue
        selected.append(row)
        if len(selected) == maximum:
            return selected
    if fill_to_maximum:
        selected_ids = {context_identity(row) for row in selected}
        for row in rows:
            if context_identity(row) in selected_ids:
                continue
            selected.append(row)
            if len(selected) == maximum:
                break
    return selected


def _lexical_tokens(value: str) -> frozenset[str]:
    return frozenset(
        token.casefold()
        for token in re.findall(
            r"[^\W_]+", unicodedata.normalize("NFC", value)
        )
        if len(token) > 1
    )


def _context_quality_key(
    context: Mapping[str, Any],
) -> tuple[int, int, str]:
    text = context["source_text"].strip()
    complete = int(
        len(text) >= 35
        and text[-1:] in {".", "?", "!", ":", ";"}
        and context["block_type"].casefold() not in {"title", "heading"}
    )
    return (-complete, -min(len(text), 2_000), context_identity(context))


