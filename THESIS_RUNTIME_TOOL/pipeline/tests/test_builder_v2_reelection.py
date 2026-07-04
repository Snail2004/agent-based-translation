from __future__ import annotations

from collections import Counter

from pipeline.scripts.builder_v2_reelection import (
    _english_matches_source,
    _target_string_equals_source,
    _winning_challenger,
    build_watchlist,
    estimate_calls,
)


def test_polysemy_with_competing_variant_enters_watchlist() -> None:
    entries = [
        {
            "concept_key": "population",
            "canonical_source_term": "population",
            "canonical_target_vi": "quần thể",
            "source_variants": [
                {"surface": "population", "occurrence_count": 2, "evidence_block_ids": ["b1", "b2"]}
            ],
            "target_variants": [{"text": "tổng thể"}],
            "audit": {
                "audit_label": "polysemy_or_context_dependent",
                "injection_action": "context_sensitive_translate",
            },
        }
    ]

    watchlist = build_watchlist(entries)

    assert [item["source_term"] for item in watchlist] == ["population"]
    assert watchlist[0]["watchlist_reasons"] == ["audit_polysemy"]
    assert [item["text"] for item in watchlist[0]["competitors"]] == ["tổng thể"]


def test_collision_soft_fallback_with_competing_variant_enters_watchlist() -> None:
    entries = [
        {
            "concept_key": "normalization",
            "canonical_source_term": "normalization",
            "canonical_target_vi": "chuẩn hóa",
            "source_variants": [{"surface": "normalization", "occurrence_count": 2}],
            "target_variants": [],
            "audit": {
                "audit_label": "keep_as_translate_term",
                "injection_action": "translate",
            },
        },
        {
            "concept_key": "regularization",
            "canonical_source_term": "regularization",
            "canonical_target_vi": "chuẩn hóa",
            "source_variants": [{"surface": "regularization", "occurrence_count": 3}],
            "target_variants": [{"text": "điều chuẩn"}],
            "audit": {
                "audit_label": "keep_as_translate_term",
                "injection_action": "translate",
            },
        },
    ]

    watchlist = build_watchlist(entries)
    regularization = next(item for item in watchlist if item["source_term"] == "regularization")

    assert "collision_soft_fallback" in regularization["watchlist_reasons"]
    assert regularization["collision_soft_fallback"]["target_key"] == "chuẩn hóa"
    assert [item["text"] for item in regularization["competitors"]] == ["điều chuẩn"]


def test_competing_variant_without_collision_or_polysemy_is_not_watchlisted() -> None:
    entries = [
        {
            "concept_key": "tensor",
            "canonical_source_term": "tensor",
            "canonical_target_vi": "tensor",
            "source_variants": [{"surface": "tensor", "occurrence_count": 4}],
            "target_variants": [{"text": "tenxơ"}],
            "audit": {
                "audit_label": "keep_as_translate_term",
                "injection_action": "translate",
            },
        }
    ]

    assert build_watchlist(entries) == []


def test_call_estimate_counts_backtranslations_and_context_vote_cap() -> None:
    watchlist = [
        {
            "source_term": "regularization",
            "backtranslation_calls": 3,
            "estimated_context_vote_calls_cap": 30,
        },
        {
            "source_term": "population",
            "backtranslation_calls": 2,
            "estimated_context_vote_calls_cap": 4,
        },
    ]

    assert estimate_calls(watchlist) == {
        "backtranslation_calls": 5,
        "context_vote_calls_cap": 34,
        "total_cap": 39,
    }


def test_round2_english_match_is_exact_or_plural_only_without_containment() -> None:
    assert _english_matches_source("populations", "population")
    assert _english_matches_source("classes", "class")
    assert not _english_matches_source("regularization term", "regularization")
    assert not _english_matches_source("feature variable", "feature")


def test_round2_source_string_candidate_cannot_win_backtranslation() -> None:
    assert _target_string_equals_source("class", "class")
    assert not _target_string_equals_source("rows", "row")
    assert not _target_string_equals_source("lớp", "class")


def test_round2_challenger_needs_two_votes_and_more_than_incumbent() -> None:
    assert _winning_challenger(Counter({"điều chuẩn": 2, "chuẩn hóa": 1}), "chuẩn hóa") == "điều chuẩn"
    assert _winning_challenger(Counter({"điều chuẩn": 1, "chuẩn hóa": 0}), "chuẩn hóa") == ""
    assert _winning_challenger(Counter({"điều chuẩn": 2, "chuẩn hóa": 2}), "chuẩn hóa") == ""
    assert _winning_challenger(Counter({"điều chuẩn": 2, "chỉnh quy": 2, "chuẩn hóa": 0}), "chuẩn hóa") == ""
