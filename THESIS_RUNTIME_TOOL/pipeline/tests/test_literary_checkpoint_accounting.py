from __future__ import annotations

from pipeline.literary.builder_pilot import (
    _chapter_accounting,
    _incremental_accounting_view,
)


def test_incremental_accounting_excludes_replayed_cache_cost() -> None:
    records = [
        {
            "attempts": 1,
            "cache_hits": 1,
            "cost_usd": 0.012,
            "prompt_tokens": 100,
            "cached_tokens": 80,
            "completion_tokens": 20,
            "reasoning_tokens": 0,
            "incremental_cost_usd": 0.0,
            "incremental_prompt_tokens": 0,
            "incremental_cached_tokens": 0,
            "incremental_completion_tokens": 0,
            "incremental_reasoning_tokens": 0,
        },
        {
            "attempts": 1,
            "cache_hits": 0,
            "cost_usd": 0.003,
            "prompt_tokens": 30,
            "cached_tokens": 0,
            "completion_tokens": 10,
            "reasoning_tokens": 0,
            "incremental_cost_usd": 0.003,
            "incremental_prompt_tokens": 30,
            "incremental_cached_tokens": 0,
            "incremental_completion_tokens": 10,
            "incremental_reasoning_tokens": 0,
        },
    ]

    artifact_total = _chapter_accounting(records)
    incremental = _incremental_accounting_view(artifact_total)

    assert artifact_total["cost_usd"] == 0.015
    assert incremental["cost_usd"] == 0.003
    assert incremental["prompt_tokens"] == 30
