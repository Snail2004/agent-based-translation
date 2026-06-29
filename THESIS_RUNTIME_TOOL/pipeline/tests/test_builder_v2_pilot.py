from __future__ import annotations

import json
from pathlib import Path

import pytest

from pipeline.agents.llm_client import LLMClient
from pipeline.agents.llm_config import LLMConfig
from pipeline.memory.store_init import init_db
from pipeline.prepass.builder_v2_consolidate import Notebook, apply_builder_output
from pipeline.prepass.runner import PrepassWindow
from pipeline.scripts import builder_v2_pilot as pilot


class FakeTransport:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def __call__(self, **kwargs):
        self.calls.append(kwargs)
        if not self.responses:
            raise AssertionError("No fake response left")
        return self.responses.pop(0)


def _config(**overrides) -> LLMConfig:
    data = {
        "model": "gpt-5.4-mini",
        "temperature": 1.0,
        "seed": 20260612,
        "reasoning_effort": "none",
        "verbosity": "low",
        "max_output_tokens": 512,
        "daily_token_cap": 2_400_000,
        "prompt_token_cap": 6000,
        "pricing": {"input": 0.25, "cached_input": 0.025, "output": 2.0},
    }
    data.update(overrides)
    return LLMConfig(**data)


def _response(obj: dict, *, prompt: int = 20, completion: int = 10) -> dict:
    return {
        "choices": [{"message": {"content": json.dumps(obj, ensure_ascii=False)}}],
        "system_fingerprint": "fp_test",
        "usage": {
            "prompt_tokens": prompt,
            "prompt_tokens_details": {"cached_tokens": 0},
            "completion_tokens": completion,
            "completion_tokens_details": {"reasoning_tokens": 0},
        },
    }


def _bad_response() -> dict:
    return {
        "choices": [{"message": {"content": '{"new_terms": ['}}],
        "system_fingerprint": "fp_test",
        "usage": {
            "prompt_tokens": 5,
            "prompt_tokens_details": {"cached_tokens": 0},
            "completion_tokens": 5,
            "completion_tokens_details": {"reasoning_tokens": 0},
        },
    }


def _four_buckets(**overrides) -> dict:
    data = {
        "chapter_id": "d2l_preliminaries",
        "window_id": "w",
        "new_terms": [],
        "updates_to_existing": [],
        "conflicts": [],
        "seen_existing_terms": [],
    }
    data.update(overrides)
    return data


def _term(source: str, target: str, block_id: str = "b1") -> dict:
    return {
        "source_term": source,
        "canonical_target_vi": target,
        "term_type": "term",
        "do_not_translate": False,
        "evidence_block_ids": [block_id],
        "occurrence_count": 1,
    }


def _window(window_id: str, block_id: str, text: str, order: int) -> PrepassWindow:
    return PrepassWindow(
        window_id=window_id,
        chapter_id="d2l_preliminaries",
        blocks=[
            {
                "block_id": block_id,
                "chapter_id": "d2l_preliminaries",
                "order_index": order,
                "block_type": "prose",
                "clean_text": text,
                "source_text": text,
            }
        ],
        est_src_tokens=max(1, len(text) // 4),
    )


def _chapter() -> dict:
    return {
        "chapter_id": "d2l_preliminaries",
        "blocks": [
            {
                "block_id": "b1",
                "chapter_id": "d2l_preliminaries",
                "order_index": 1,
                "block_type": "prose",
                "clean_text": "A feature appears.",
                "source_text": "A feature appears.",
            },
            {
                "block_id": "b2",
                "chapter_id": "d2l_preliminaries",
                "order_index": 2,
                "block_type": "prose",
                "clean_text": "The features reappear.",
                "source_text": "The features reappear.",
            },
        ],
    }


def _db(tmp_path: Path) -> Path:
    path = tmp_path / "memory.sqlite3"
    conn = init_db(path)
    conn.close()
    return path


def test_live_notebook_pack_has_no_future_leak_and_matches_all_surfaces():
    first = _window("w1", "b1", "A feature appears.", 1)
    second = _window("w2", "b2", "The features reappear.", 2)
    notebook = Notebook()

    first_pack, first_audit = pilot.build_pack_from_notebook(notebook, first)
    assert first_pack["matched_existing_terms"] == []
    assert first_pack["near_number_variants"] == []
    assert first_audit["pack_provenance"] == "builder_v2_notebook"

    apply_builder_output(
        notebook,
        {"new_terms": [_term("feature", "dac trung")]},
        window_id="w1",
        block_types_by_id={"b1": "prose"},
    )
    second_pack, second_audit = pilot.build_pack_from_notebook(notebook, second)

    assert second_pack["near_number_variants"][0]["source_term"] == "feature"
    assert second_pack["near_number_variants"][0]["related_surface_seen"] == "features"
    assert second_audit["included_by_concept_key"] == [
        {"source_term": "feature", "related_surface_seen": "features"}
    ]


def test_run_online_pilot_uses_separate_cache_and_second_run_is_cache_hit(tmp_path: Path):
    windows = [
        _window("w1", "b1", "A feature appears.", 1),
        _window("w2", "b2", "The feature reappears.", 2),
    ]
    config = _config()
    estimate = pilot.Estimate(
        chapter_id="d2l_preliminaries",
        calls=2,
        estimated_prompt_tokens=100,
        estimated_output_tokens_nominal=20,
        estimated_output_tokens_cap=1024,
        estimated_total_tokens_nominal=120,
        estimated_total_tokens_cap=1124,
        estimated_cost_usd_nominal=0.001,
        estimated_cost_usd_cap=0.01,
        pricing=dict(config.pricing),
        model_config=pilot.config_to_dict(config),
    )
    responses = [
        _response(_four_buckets(new_terms=[_term("feature", "dac trung", "b1")])),
        _response(_four_buckets(seen_existing_terms=[{"source_term": "feature", "evidence_block_ids": ["b2"]}])),
    ]
    transport = FakeTransport(responses)
    cache_path = tmp_path / "artifact-cache" / "llm_cache.sqlite3"
    client = LLMClient(config, cache_path, transport=transport)
    db_path = _db(tmp_path)

    first = pilot.run_online_pilot(
        db_path=db_path,
        doc_id="d2l",
        chapter=_chapter(),
        windows=windows,
        client=client,
        config=config,
        out_dir=tmp_path / "first",
        estimate=estimate,
    )
    second = pilot.run_online_pilot(
        db_path=db_path,
        doc_id="d2l",
        chapter=_chapter(),
        windows=windows,
        client=client,
        config=config,
        out_dir=tmp_path / "second",
        estimate=estimate,
    )

    assert cache_path.exists()
    prompt_files = sorted((tmp_path / "first" / "prompts").glob("*.txt"))
    assert len(prompt_files) == 2
    assert "SYSTEM" in prompt_files[0].read_text(encoding="utf-8")
    audit = json.loads((tmp_path / "first" / "per_window_audit.json").read_text(encoding="utf-8"))
    assert audit[0]["prompt_file"].startswith("prompts/")
    assert len(transport.calls) == 2
    assert first["summary"]["cache_misses"] == 2
    assert second["summary"]["cache_hits"] == 2
    assert second["summary"]["cache_misses"] == 0


def test_parse_failure_reask_sets_degraded_without_crashing(tmp_path: Path):
    config = _config()
    transport = FakeTransport([_bad_response(), _bad_response()])
    client = LLMClient(config, tmp_path / "llm_cache.sqlite3", transport=transport)
    estimate = pilot.Estimate(
        chapter_id="d2l_preliminaries",
        calls=1,
        estimated_prompt_tokens=50,
        estimated_output_tokens_nominal=10,
        estimated_output_tokens_cap=512,
        estimated_total_tokens_nominal=60,
        estimated_total_tokens_cap=562,
        estimated_cost_usd_nominal=0.001,
        estimated_cost_usd_cap=0.01,
        pricing=dict(config.pricing),
        model_config=pilot.config_to_dict(config),
    )

    report = pilot.run_online_pilot(
        db_path=_db(tmp_path),
        doc_id="d2l",
        chapter=_chapter(),
        windows=[_window("w1", "b1", "A feature appears.", 1)],
        client=client,
        config=config,
        out_dir=tmp_path / "out",
        estimate=estimate,
    )

    assert report["status"] == "degraded"
    assert report["summary"]["parse_failure_count"] == 2
    assert report["summary"]["skipped_windows"] == 1
    assert len(transport.calls) == 2
    cost_log = json.loads((tmp_path / "out" / "cost_log.json").read_text(encoding="utf-8"))
    assert [item["attempt"] for item in cost_log] == [1, 2]


def test_cost_gate_blocks_when_estimate_cap_exceeds_ceiling():
    config = _config()
    estimate = pilot.Estimate(
        chapter_id="d2l_preliminaries",
        calls=1,
        estimated_prompt_tokens=100,
        estimated_output_tokens_nominal=10,
        estimated_output_tokens_cap=512,
        estimated_total_tokens_nominal=110,
        estimated_total_tokens_cap=612,
        estimated_cost_usd_nominal=0.01,
        estimated_cost_usd_cap=0.20,
        pricing=dict(config.pricing),
        model_config=pilot.config_to_dict(config),
    )

    with pytest.raises(pilot.CostGateExceeded):
        pilot.enforce_cost_gate(estimate, 0.19)


def test_estimate_only_uses_c1_upper_bound_even_with_empty_notebook(tmp_path: Path):
    report = {
        "chapter_id": "d2l_preliminaries",
        "windows": {"total_prompt_tokens_est": 141317},
    }
    report_path = tmp_path / "builder_v2_b_render_report.json"
    report_path.write_text(json.dumps(report), encoding="utf-8")
    windows = [_window("w1", "b1", "A feature appears.", 1), _window("w2", "b2", "More text.", 2)]

    estimate = pilot.estimate_pilot_cost(
        chapter_id="d2l_preliminaries",
        windows=windows,
        config=_config(max_output_tokens=6144),
        c1_render_report_path=report_path,
    )

    assert estimate.estimated_prompt_tokens == 141317
    assert estimate.estimated_output_tokens_nominal == 2 * 1200
    assert estimate.estimated_output_tokens_cap == 2 * 6144
    assert estimate.estimated_cost_usd_nominal > 0
    assert estimate.zero_api is True
