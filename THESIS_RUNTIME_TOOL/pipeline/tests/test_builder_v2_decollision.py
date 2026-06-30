from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from pipeline.prepass import builder_v2_decollision as decollide
from pipeline.scripts import builder_v2_c35_decollision as script


def _db(tmp_path: Path) -> Path:
    path = tmp_path / "memory.sqlite3"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE blocks (block_id TEXT PRIMARY KEY, text TEXT, chapter_id TEXT, block_type TEXT)")
    conn.executemany(
        "INSERT INTO blocks VALUES (?,?,?,?)",
        [
            ("b_grad", "The gradient points in the direction of steepest ascent.", "ch1", "prose"),
            ("b_part", "A partial derivative differentiates with respect to one variable.", "ch1", "prose"),
            ("b_prod", "The product rule gives the derivative of a product.", "ch1", "prose"),
            ("b_mult", "The multiplication rule combines conditional probabilities.", "ch1", "prose"),
        ],
    )
    conn.commit()
    conn.close()
    return path


def _entry(
    key: str,
    target: str,
    block_id: str,
    *,
    variants: list[str] | None = None,
    conflicts: list[dict] | None = None,
    label: str = "keep_as_translate_term",
) -> dict:
    return {
        "concept_key": key,
        "canonical_source_term": key,
        "canonical_target_vi": target,
        "occurrences_total": 2,
        "source_variants": [
            {
                "surface": key,
                "evidence_block_ids": [block_id],
                "occurrence_count": 2,
            }
        ],
        "target_variants": [
            {"text": text, "evidence_block_id": block_id, "variant_reason": "test"}
            for text in (variants or [])
        ],
        "conflict_ledger": conflicts or [],
        "audit": {
            "audit_label": label,
            "priority_tier": "high",
            "injection_action": "translate",
            "confidence": "high",
            "reason": "test",
        },
    }


def _notebook() -> dict:
    return {
        "entries": [
            _entry(
                "gradient",
                "đạo hàm riêng",
                "b_grad",
                variants=["đạo hàm theo hướng"],
                conflicts=[
                    {
                        "type": "bad_existing_target",
                        "proposed_target": "gradient",
                        "evidence_block_ids": ["b_grad"],
                    },
                    {
                        "type": "canonical_target_change",
                        "proposed_target": "đạo hàm",
                        "evidence_block_ids": ["b_grad"],
                    },
                ],
            ),
            _entry("partial derivative", "đạo hàm riêng", "b_part"),
            _entry("product rule", "quy tắc nhân", "b_prod", variants=["quy tắc tích"]),
            _entry("multiplication rule", "quy tắc nhân", "b_mult", variants=["quy tắc phép nhân"]),
        ]
    }


def _config(path: Path) -> None:
    path.write_text(
        "\n".join(
            [
                'model: "gpt-5.4-mini"',
                "temperature: 1.0",
                "seed: 20260612",
                'reasoning_effort: "none"',
                'verbosity: "low"',
                "max_output_tokens: 6144",
                "daily_token_cap: 2400000",
                "prompt_token_cap: 6000",
                "pricing:",
                "  input: 0.25",
                "  cached_input: 0.025",
                "  output: 2.00",
                "",
            ]
        ),
        encoding="utf-8",
    )


def test_group_builder_keeps_diacritics_and_candidate_provenance(tmp_path: Path):
    db = _db(tmp_path)
    groups = decollide.build_collision_groups(_notebook(), db)

    assert [group["shared_canonical"] for group in groups] == ["quy tắc nhân", "đạo hàm riêng"]
    gradient = next(
        member
        for group in groups
        for member in group["members"]
        if member["entry_id"] == "gradient"
    )
    assert gradient["candidates"][0] == {
        "text": "gradient",
        "source": "conflict_ledger",
        "type": "bad_existing_target",
    }
    assert {"text": "đạo hàm theo hướng", "source": "target_variant", "type": None} in gradient["candidates"]


def test_validator_rejects_shared_or_sibling_canonical(tmp_path: Path):
    groups = decollide.build_collision_groups(_notebook(), _db(tmp_path))
    expected_count = sum(len(group["members"]) for group in groups)
    rows = [
        {
            "entry_id": member["entry_id"],
            "decision": "keep_shared",
            "chosen_canonical": group["shared_canonical"],
            "confidence": "high",
            "reason": "test reason",
        }
        for group in groups
        for member in group["members"]
    ]
    assert len(rows) == expected_count

    rows[0] = {
        "entry_id": rows[0]["entry_id"],
        "decision": "resolve_distinct",
        "chosen_canonical": groups[0]["shared_canonical"],
        "confidence": "high",
        "reason": "bad shared choice",
    }
    with pytest.raises(ValueError, match="shared canonical"):
        decollide.validate_decollision_results(rows, groups)


def test_apply_polysemy_and_uncertain_do_not_remain_hard_canonical():
    notebook = _notebook()
    rows = [
        {
            "group_id": "g",
            "entry_id": "gradient",
            "decision": "mark_polysemy",
            "chosen_canonical": None,
            "confidence": "medium",
            "reason": "several renderings",
            "shared_canonical": "đạo hàm riêng",
            "candidate_texts": ["gradient"],
        },
        {
            "group_id": "g",
            "entry_id": "partial derivative",
            "decision": "uncertain",
            "chosen_canonical": None,
            "confidence": "low",
            "reason": "thin evidence",
            "shared_canonical": "đạo hàm riêng",
            "candidate_texts": [],
        },
    ]
    updated = decollide.apply_decollision_to_notebook(notebook, rows)
    by_id = {entry["concept_key"]: entry for entry in updated["entries"]}

    assert by_id["gradient"]["inject_as_hard_canonical"] is False
    assert by_id["gradient"]["audit"]["injection_action"] == "context_sensitive_translate"
    assert by_id["partial derivative"]["inject_as_hard_canonical"] is False
    assert by_id["partial derivative"]["audit"]["injection_action"] == "review_only"


def test_estimate_only_archives_full_prompts(tmp_path: Path):
    db = _db(tmp_path)
    notebook_path = tmp_path / "notebook.json"
    notebook_path.write_text(json.dumps(_notebook(), ensure_ascii=False), encoding="utf-8")
    config_path = tmp_path / "llm_prepass.yaml"
    _config(config_path)
    out = tmp_path / "out"

    report = script.run_c35(
        db_path=db,
        notebook_path=notebook_path,
        config_path=config_path,
        out_dir=out,
        estimate_only=True,
    )

    assert report["zero_api"] is True
    assert report["groups"] == 2
    assert (out / "collision_groups.json").exists()
    prompts = sorted((out / "prompts").glob("*.txt"))
    assert len(prompts) == 1
    assert "You resolve naming COLLISIONS" in prompts[0].read_text(encoding="utf-8")
