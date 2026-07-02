from __future__ import annotations

from pipeline.agents.llm_config import LLMConfig
from pipeline.agents.llm_client import LLMUsage
from pipeline.scripts.run_experiment_cascade import (
    _merge_scope_term,
    _source_surfaces_from_row,
    _target_forms_from_row,
    _run_t3_for_config,
    estimate_t3_for_report,
)


class _Result:
    parsed_json = {
        "occurrence_id": "S0:block:term:0",
        "found": True,
        "target_quote": "nguoi dung",
        "left_context": "",
        "confidence": "high",
    }
    usage = LLMUsage(prompt_tokens=100, completion_tokens=20)
    from_cache = False
    cost_usd = 0.001
    cache_key = "cache-key"


class _Client:
    def call(self, messages, *, response_format, tag):  # noqa: ANN001
        assert "accepted_forms" not in messages[1]["content"]
        assert response_format["json_schema"]["name"] == "occurrence_location"
        assert tag.startswith("BUILDER-V2:35.10:")
        return _Result()


def _decision(resolved_by: str) -> dict:
    return {
        "occ_id": "S0:block:term:0",
        "config": "S0",
        "block_id": "block",
        "chapter_id": "chapter",
        "source_term": "user",
        "term_id": "term",
        "source_start": 0,
        "source_end": 4,
        "source_surface": "user",
        "source_sentence_idx": 0,
        "source_sentence": "The user clicks.",
        "source_text": "The user clicks.",
        "target_text": "nguoi dung nhap.",
        "resolved_by": resolved_by,
        "decision": "ambiguous",
        "accepted_forms": ["nguoi dung"],
        "t1": {"ranges": [[0, 16]]},
        "candidates": [],
    }


def test_t3_estimate_counts_only_residuals() -> None:
    report = {"decisions": [_decision("t2_credit"), _decision("t3_stub")]}
    config = LLMConfig(
        model="gpt-test-001",
        max_output_tokens=10,
        pricing={"input": 1.0, "cached_input": 0.1, "output": 2.0},
    )
    estimate = estimate_t3_for_report(report, config)
    assert estimate["calls"] == 1
    assert estimate["prompt_tokens_estimate"] > 0
    assert estimate["output_tokens_cap"] == 10
    assert estimate["cost_cap_usd"] > 0


def test_t3_run_keeps_model_quote_as_t3_overlay_not_t2_span() -> None:
    updated, stats = _run_t3_for_config(
        {"decisions": [_decision("t3_stub")]},
        _Client(),
    )
    item = updated["decisions"][0]
    assert item["resolved_by"] == "t3_llm"
    assert item["target_quote"] == "nguoi dung"
    assert item.get("target_start") is None
    assert item.get("target_end") is None
    assert item["t3_code_score"]["adherence_label"] == "adherent"
    assert stats["fresh_calls"] == 1
    assert stats["validation_errors"] == {}


def test_notebook_scope_expands_source_variants_and_preserve_forms() -> None:
    row = {
        "source_term": "Tensor",
        "target_term": "tensor",
        "source_surfaces": ["Tensor", "tensors", "Tensor"],
        "target_variants": ["tenxơ", "tensor"],
    }
    assert _source_surfaces_from_row(row) == ["Tensor", "tensors"]
    assert _target_forms_from_row(row, include_source_for_preserve=True) == [
        "tensor",
        "tenxơ",
        "tensors",
    ]
    terms = {}
    _merge_scope_term(terms, source="Tensor", forms=["tensor"], origin="notebook")
    _merge_scope_term(terms, source="tensor", forms=["tenxơ"], origin="gold")
    assert list(terms) == ["tensor"]
    assert terms["tensor"]["forms"] == ["tensor", "tenxơ"]
