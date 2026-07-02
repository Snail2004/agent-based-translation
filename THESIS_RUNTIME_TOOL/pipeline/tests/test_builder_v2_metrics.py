from __future__ import annotations

from pipeline.scripts.builder_v2_metrics import _tier_gold_misses


def test_tier_gold_misses_word_boundary_containment() -> None:
    entries = [
        {
            "canonical_source_term": "multilayer perceptron",
            "source_variants": [{"surface": "multilayer perceptrons"}],
        },
        {"canonical_source_term": "overfit", "source_variants": []},
        {"canonical_source_term": "bias vector", "source_variants": []},
    ]
    metric = {
        "matched_terms": 6,
        "recall": 0.6,
        "missing_terms": [
            {"source_term": "perceptron", "gold_target": "perceptron"},
            {"source_term": "fit", "gold_target": "khớp"},
            {"source_term": "vector", "gold_target": "vector"},
            {"source_term": "batch size", "gold_target": "kích thước batch"},
        ],
    }
    tiered = _tier_gold_misses(metric, entries)
    covered = {item["source_term"] for item in tiered["phrase_covered"]}
    # word-boundary: perceptron/vector are inside longer phrases; `fit` is NOT
    # inside the single token `overfit`; batch size has no trace at all.
    assert covered == {"perceptron", "vector"}
    assert tiered["absent_terms"] == ["fit", "batch size"]
    assert tiered["recall_strict"] == 0.6
    assert tiered["recall_with_phrase_covered"] == 0.8
