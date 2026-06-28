from __future__ import annotations

import re
import unicodedata


CONCEPT_KEY_OVERRIDES: dict[str, str] = {
    "least squares": "least squares",
    "ordinary least squares": "ordinary least squares",
    "naive bayes": "naive bayes",
    "naive bayes classifier": "naive bayes classifier",
}

DONT_SINGULARIZE_PHRASES: set[str] = set(CONCEPT_KEY_OVERRIDES)

DONT_SINGULARIZE_TOKENS: set[str] = {
    "analysis",
    "axis",
    "basis",
    "bias",
    "bus",
    "class",
    "corpus",
    "diagnosis",
    "gas",
    "hypothesis",
    "lens",
    "logits",
    "loss",
    "mathematics",
    "news",
    "physics",
    "process",
    "series",
    "species",
    "statistics",
    "status",
    "synthesis",
}

IRREGULAR_PLURALS: dict[str, str] = {
    "analyses": "analysis",
    "axes": "axis",
    "hypotheses": "hypothesis",
    "indices": "index",
    "matrices": "matrix",
    "vertices": "vertex",
}


def normalize_phrase(value: str) -> str:
    normalized = unicodedata.normalize("NFC", str(value or "")).casefold().strip()
    return re.sub(r"\s+", " ", normalized)


def concept_key(phrase: str) -> str:
    normalized = normalize_phrase(phrase)
    if not normalized:
        return ""
    if normalized in CONCEPT_KEY_OVERRIDES:
        return CONCEPT_KEY_OVERRIDES[normalized]
    if normalized in DONT_SINGULARIZE_PHRASES:
        return normalized
    return " ".join(singularize_token(token) for token in normalized.split())


def singularize_token(token: str) -> str:
    value = normalize_phrase(token)
    if not value:
        return ""
    if value in DONT_SINGULARIZE_TOKENS:
        return value
    if value.endswith("ss"):
        return value
    if value in IRREGULAR_PLURALS:
        return IRREGULAR_PLURALS[value]
    if len(value) <= 3:
        return value
    if value.endswith("ies") and len(value) > 4:
        return value[:-3] + "y"
    if re.search(r"(s|x|z|ch|sh)es$", value):
        return value[:-2]
    if value.endswith("s"):
        return value[:-1]
    return value


def merge_reason(source_term: str) -> str:
    normalized = normalize_phrase(source_term)
    key = concept_key(source_term)
    if not normalized or normalized == key:
        return "exact"
    if normalized in IRREGULAR_PLURALS:
        return "irregular_plural"
    if normalized in CONCEPT_KEY_OVERRIDES:
        return "phrase_override"
    return "regular_plural"


def risk_flags(source_terms: list[str], targets: list[str] | None = None) -> list[str]:
    flags: set[str] = set()
    normalized_terms = [normalize_phrase(term) for term in source_terms if normalize_phrase(term)]
    normalized_targets = [normalize_phrase(target) for target in targets or [] if normalize_phrase(target)]
    for term in normalized_terms:
        if term in CONCEPT_KEY_OVERRIDES:
            flags.add("phrase_override")
        for token in term.split():
            if token in DONT_SINGULARIZE_TOKENS:
                flags.add(f"dont_singularize:{token}")
            if token in IRREGULAR_PLURALS:
                flags.add(f"irregular:{token}->{IRREGULAR_PLURALS[token]}")
            if singularize_token(token) != token:
                flags.add("number_variant")
    if len(set(normalized_targets)) > 1:
        flags.add("target_divergence")
    if any(_is_common_short_term(term) for term in normalized_terms):
        flags.add("common_short_source")
    return sorted(flags)


def is_common_short_source(source_term: str) -> bool:
    return _is_common_short_term(normalize_phrase(source_term))


def _is_common_short_term(normalized_term: str) -> bool:
    if not normalized_term or " " in normalized_term:
        return False
    if not normalized_term.isalpha():
        return False
    if normalized_term in DONT_SINGULARIZE_TOKENS:
        return False
    return len(normalized_term) <= 7
