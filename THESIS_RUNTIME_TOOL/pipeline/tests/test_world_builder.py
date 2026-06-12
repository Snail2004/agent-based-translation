from __future__ import annotations

import json

from pipeline.agents.llm_client import LLMClient
from pipeline.agents.llm_config import LLMConfig
from pipeline.prepass.prompt import build_messages
from pipeline.prepass.registry import PrepassRegistry
from pipeline.prepass.runner import run_prepass
from pipeline.prepass.schemas import validate_chapter_output


class FakeTransport:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def __call__(self, **kwargs):
        self.calls.append(kwargs)
        if not self.responses:
            raise AssertionError("FakeTransport has no response left")
        return _response(self.responses.pop(0))


def _config() -> LLMConfig:
    return LLMConfig(
        model="gpt-5.4-mini",
        temperature=0.2,
        seed=20260612,
        reasoning_effort="minimal",
        verbosity="low",
        max_output_tokens=256,
        daily_token_cap=2_400_000,
        pricing={"input": 0.25, "cached_input": 0.025, "output": 2.0},
    )


def _client(tmp_path, responses, transport_out=None) -> LLMClient:
    transport = FakeTransport(responses)
    if transport_out is not None:
        transport_out.append(transport)
    return LLMClient(_config(), tmp_path / "cache.sqlite3", transport=transport)


def _response(text: str | dict):
    content = text if isinstance(text, str) else json.dumps(text, ensure_ascii=False)
    return {
        "choices": [{"message": {"content": content}}],
        "system_fingerprint": "fp_prepass_test",
        "usage": {
            "prompt_tokens": 100,
            "prompt_tokens_details": {"cached_tokens": 0},
            "completion_tokens": 20,
            "completion_tokens_details": {"reasoning_tokens": 3},
        },
    }


def _chapter(chapter_id: str, block_count: int = 2):
    suffix = chapter_id.split("_")[-1]
    return {
        "chapter_id": chapter_id,
        "blocks": [
            {
                "block_id": f"mini_{suffix}_b{i:03d}",
                "clean_text": f"Block {i} text about Jim and the captain.",
                "source_text": f"Block {i} source.",
            }
            for i in range(1, block_count + 1)
        ],
    }


def _document(chapters):
    return {"doc_id": "mini", "chapters": chapters}


def _valid_output(chapter_id: str, *, entity_id: str = "ent_jim_hawkins"):
    suffix = chapter_id.split("_")[-1]
    return {
        "chapter_id": chapter_id,
        "glossary_candidates": [
            {
                "source_term": "Admiral Benbow",
                "proposed_target_vi": "quán Admiral Benbow",
                "do_not_translate": False,
                "category": "place",
                "block_ids": [f"{suffix}_b001"],
            }
        ],
        "entities": [
            {
                "entity_id": entity_id,
                "canonical_source": "Jim Hawkins",
                "aliases_source": ["Jim"],
                "entity_type": "person",
                "proposed_target_vi": "Jim Hawkins",
                "aliases_target_vi": ["Jim"],
            }
        ],
        "relations": [],
        "mention_surfaces": [
            {"entity_id": entity_id, "surfaces": ["Jim Hawkins", "Jim"]}
        ],
        "chapter_summary_vi": "Jim mở đầu câu chuyện tại quán trọ.",
        "motifs": [{"note": "không khí quán trọ ven biển", "block_ids": [f"{suffix}_b001"]}],
    }


def test_prompt_blocks_and_registry():
    chapter = _chapter("mini_ch01")
    messages = build_messages(chapter, "ent_jim | Jim -> Jim")
    combined = "\n".join(message["content"] for message in messages)

    assert "[ch01_b001]" in combined
    assert "ent_jim | Jim -> Jim" in combined
    for forbidden in ["AILAB", "oracle", "handoff"]:
        assert forbidden not in combined


def test_schema_validation_catches():
    valid = _valid_output("mini_ch01")
    valid_block_ids = {"ch01_b001", "ch01_b002"}

    assert (
        validate_chapter_output(
            valid,
            expected_chapter_id="mini_ch01",
            valid_block_ids=valid_block_ids,
        )
        == []
    )

    missing = dict(valid)
    missing.pop("chapter_summary_vi")
    missing_nested = json.loads(json.dumps(valid))
    missing_nested["glossary_candidates"][0].pop("block_ids")
    wrong_type = dict(valid)
    wrong_type["glossary_candidates"] = {}
    unknown = dict(valid)
    unknown["relations"] = [
        {
            "a": "ent_jim_hawkins",
            "b": "ent_unknown",
            "relation": "knows",
            "address_a_to_b_vi": "ông",
            "address_b_to_a_vi": "cậu",
            "state_label": "test",
            "trigger_block_id": None,
            "notes": "",
        }
    ]

    errors = []
    errors.extend(validate_chapter_output(missing))
    errors.extend(validate_chapter_output(missing_nested))
    errors.extend(validate_chapter_output(wrong_type))
    errors.extend(validate_chapter_output(unknown))

    assert any("missing required field: chapter_summary_vi" in item for item in errors)
    assert any("glossary_candidates[0].block_ids is required" in item for item in errors)
    assert any("glossary_candidates must be list" in item for item in errors)
    assert any("references unknown entity_id" in item for item in errors)


def test_runner_two_chapters_merges_registry(tmp_path):
    chapters = [_chapter("mini_ch01"), _chapter("mini_ch02")]
    document_path = tmp_path / "document.json"
    document_path.write_text(
        json.dumps(_document(chapters), ensure_ascii=False),
        encoding="utf-8",
    )
    transports = []
    client = _client(
        tmp_path,
        [_valid_output("mini_ch01"), _valid_output("mini_ch02", entity_id="ent_billy_bones")],
        transports,
    )

    report = run_prepass(document_path, ["ch01", "ch02"], client, tmp_path / "out")

    assert (tmp_path / "out" / "mini_ch01.json").exists()
    assert (tmp_path / "out" / "mini_ch02.json").exists()
    assert (tmp_path / "out" / "run_report.json").exists()
    assert report.json_fail_rate == 0
    chapter2_prompt = "\n".join(
        message["content"] for message in transports[0].calls[1]["messages"]
    )
    assert "ent_jim_hawkins" in chapter2_prompt


def test_reask_then_success(tmp_path):
    chapter = _chapter("mini_ch01")
    document_path = tmp_path / "document.json"
    document_path.write_text(
        json.dumps(_document([chapter]), ensure_ascii=False),
        encoding="utf-8",
    )
    transports = []
    client = _client(tmp_path, ["not-json", _valid_output("mini_ch01")], transports)

    report = run_prepass(document_path, ["ch01"], client, tmp_path / "out")

    assert len(transports[0].calls) == 2
    assert report.json_fail_rate == 0
    assert report.chapters[0].status == "passed"
    reask_messages = transports[0].calls[1]["messages"]
    assert "Output truoc sai:" in reask_messages[-1]["content"]
    assert "JSON parse failed" in reask_messages[-1]["content"]


def test_failed_chapter_continues(tmp_path):
    chapters = [_chapter("mini_ch01"), _chapter("mini_ch02")]
    document_path = tmp_path / "document.json"
    document_path.write_text(
        json.dumps(_document(chapters), ensure_ascii=False),
        encoding="utf-8",
    )
    transports = []
    client = _client(
        tmp_path,
        ["not-json", "still-not-json", _valid_output("mini_ch02")],
        transports,
    )

    report = run_prepass(document_path, ["ch01", "ch02"], client, tmp_path / "out")

    assert report.json_fail_rate == 0.5
    assert report.chapters[0].status == "failed"
    assert report.chapters[1].status == "passed"
    assert not (tmp_path / "out" / "mini_ch01.json").exists()
    assert (tmp_path / "out" / "mini_ch02.json").exists()
    chapter2_prompt = "\n".join(
        message["content"] for message in transports[0].calls[2]["messages"]
    )
    assert "still-not-json" not in chapter2_prompt


def test_registry_compress_cap():
    registry = PrepassRegistry()
    registry.merge(
        {
            "chapter_id": "mini_ch01",
            "glossary_candidates": [
                {
                    "source_term": "Admiral Benbow",
                    "proposed_target_vi": "quán Admiral Benbow",
                    "block_ids": ["b1"],
                }
            ],
            "entities": [
                {
                    "entity_id": f"ent_person_{i:03d}",
                    "canonical_source": f"Person {i}",
                    "aliases_source": [f"P{i}"],
                    "proposed_target_vi": f"Nhân vật {i}",
                }
                for i in range(80)
            ],
            "relations": [
                {
                    "a": "ent_person_000",
                    "b": "ent_person_001",
                    "state_label": "wary",
                    "address_a_to_b_vi": "ông",
                    "address_b_to_a_vi": "cậu",
                }
            ],
        }
    )

    compressed = registry.compress(max_tokens=80)

    assert len(compressed) <= 80 * 4
    assert "Admiral Benbow -> quán Admiral Benbow" in compressed
    assert "ent_person_000 <-> ent_person_001" in compressed
