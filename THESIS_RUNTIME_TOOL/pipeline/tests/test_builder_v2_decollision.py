from __future__ import annotations

import inspect
import json
import re
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


def test_prompt_v2_adds_owner_hint_and_rejects_shared_signal(tmp_path: Path):
    groups = decollide.build_collision_groups(
        _notebook(),
        _db(tmp_path),
        prompt_version=decollide.PROMPT_VERSION_V2,
    )
    derivative_group = next(group for group in groups if group["shared_canonical"] == "đạo hàm riêng")
    by_id = {member["entry_id"]: member for member in derivative_group["members"]}

    assert derivative_group["owner_hint"] == "partial derivative"
    assert by_id["gradient"]["signals"]["rejects_shared"] is True
    assert by_id["partial derivative"]["signals"]["rejects_shared"] is False


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


def test_gate_applies_only_hard_ledger_candidates_and_holds_variant_proposals(tmp_path: Path):
    notebook = _notebook()
    groups = decollide.build_collision_groups(notebook, _db(tmp_path))
    rows = []
    for group in groups:
        for member in group["members"]:
            if member["entry_id"] == "gradient":
                rows.append(
                    {
                        "entry_id": "gradient",
                        "decision": "resolve_distinct",
                        "chosen_canonical": "đạo hàm",
                        "confidence": "high",
                        "reason": "ledger flags bad shared name",
                    }
                )
            elif member["entry_id"] == "product rule":
                rows.append(
                    {
                        "entry_id": "product rule",
                        "decision": "resolve_distinct",
                        "chosen_canonical": "quy tắc tích",
                        "confidence": "high",
                        "reason": "variant suggests a narrower rule name",
                    }
                )
            else:
                rows.append(
                    {
                        "entry_id": member["entry_id"],
                        "decision": "keep_shared",
                        "chosen_canonical": group["shared_canonical"],
                        "confidence": "high",
                        "reason": "keeps the shared owner name",
                    }
                )

    validated = decollide.validate_decollision_results(rows, groups)
    gated = decollide.gate_decollision_rows(validated, gated=True)
    by_id = {row["entry_id"]: row for row in gated}

    assert by_id["gradient"]["applied_status"] == "applied"
    assert by_id["gradient"]["chosen_candidate_source"] == "conflict_ledger"
    assert by_id["gradient"]["chosen_candidate_type"] == "canonical_target_change"
    assert by_id["product rule"]["applied_status"] == "held_proposal"
    assert by_id["product rule"]["chosen_candidate_source"] == "target_variant"
    assert by_id["product rule"]["chosen_candidate_type"] is None

    updated = decollide.apply_decollision_to_notebook(
        notebook,
        gated,
        prompt_version=decollide.PROMPT_VERSION_V2,
    )
    entries = {entry["concept_key"]: entry for entry in updated["entries"]}
    assert entries["gradient"]["canonical_target_vi"] == "đạo hàm"
    assert entries["product rule"]["canonical_target_vi"] == "quy tắc nhân"
    assert entries["product rule"]["decollision"]["applied_status"] == "held_proposal"
    assert updated["decollision_prompt_version"] == decollide.PROMPT_VERSION_V2


def test_no_new_collision_guard_converts_applied_resolve_to_polysemy(tmp_path: Path):
    notebook = _notebook()
    notebook["entries"].append(_entry("derivative", "đạo hàm", "b_part"))
    groups = decollide.build_collision_groups(notebook, _db(tmp_path))
    rows = []
    for group in groups:
        for member in group["members"]:
            if member["entry_id"] == "gradient":
                rows.append(
                    {
                        "entry_id": "gradient",
                        "decision": "resolve_distinct",
                        "chosen_canonical": "đạo hàm",
                        "confidence": "high",
                        "reason": "ledger flags bad shared name",
                    }
                )
            else:
                rows.append(
                    {
                        "entry_id": member["entry_id"],
                        "decision": "keep_shared",
                        "chosen_canonical": group["shared_canonical"],
                        "confidence": "high",
                        "reason": "keeps the shared owner name",
                    }
                )

    validated = decollide.validate_decollision_results(rows, groups)
    guarded = decollide.gate_decollision_rows(validated, gated=True, notebook=notebook)
    gradient = next(row for row in guarded if row["entry_id"] == "gradient")

    assert gradient["decision"] == "mark_polysemy"
    assert gradient["chosen_canonical"] is None
    assert gradient["applied_status"] == "converted_to_polysemy"
    assert gradient["blocked_chosen_canonical"] == "đạo hàm"
    assert gradient["blocked_collision_entry_ids"] == ["derivative"]

    updated = decollide.apply_decollision_to_notebook(notebook, guarded)
    entries = {entry["concept_key"]: entry for entry in updated["entries"]}
    assert entries["gradient"]["canonical_target_vi"] != "đạo hàm"
    assert entries["gradient"]["inject_as_hard_canonical"] is False
    assert entries["gradient"]["audit"]["audit_label"] == "polysemy_or_context_dependent"


def test_ledger_promotion_uses_three_gates_without_demoting_held_style_proposals():
    notebook = {
        "entries": [
            _entry(
                "gradient",
                "dao ham rieng",
                "b_grad",
                conflicts=[
                    {"type": "bad_existing_target", "proposed_target": "gradient", "evidence_block_ids": ["b_grad"]},
                    {"type": "canonical_target_change", "proposed_target": "dao ham", "evidence_block_ids": ["b_grad"]},
                ],
            ),
            _entry(
                "tensor",
                "tensor",
                "b_grad",
                conflicts=[
                    {"type": "bad_existing_target", "proposed_target": "tenxo", "evidence_block_ids": ["b_grad"]}
                ],
            ),
            _entry(
                "shape",
                "hinh dang",
                "b_grad",
                conflicts=[
                    {"type": "bad_existing_target", "proposed_target": "kich thuoc", "evidence_block_ids": ["b_grad"]}
                ],
                label="polysemy_or_context_dependent",
            ),
            _entry("size", "kich thuoc", "b_grad"),
            _entry(
                "one",
                "so 1",
                "b_grad",
                conflicts=[
                    {"type": "bad_existing_target", "proposed_target": "mot", "evidence_block_ids": ["b_grad"]}
                ],
                label="generic_low_value",
            ),
        ]
    }

    promoted, trail = decollide.promote_ledger_canonical_candidates(notebook)
    entries = {entry["concept_key"]: entry for entry in promoted["entries"]}
    trail_by_id = {row["entry_id"]: row for row in trail}

    assert promoted["ledger_promotion_summary"]["canonical_changed_count"] == 1
    assert entries["gradient"]["canonical_target_vi"] == "gradient"
    assert entries["gradient"]["canonical_corrected_from"] == "dao ham rieng"
    assert entries["gradient"]["inject_as_hard_canonical"] is True
    assert trail_by_id["gradient"]["status"] == "promoted_keep_source"

    assert entries["tensor"]["canonical_target_vi"] == "tensor"
    assert entries["tensor"]["audit"]["injection_action"] == "translate"
    assert entries["tensor"].get("inject_as_hard_canonical") is None
    assert trail_by_id["tensor"]["status"] == "held_translation_proposal"

    assert entries["shape"]["canonical_target_vi"] == "hinh dang"
    assert entries["shape"]["audit"]["audit_label"] == "polysemy_or_context_dependent"
    assert trail_by_id["shape"]["status"] == "blocked_audit_label"

    assert entries["one"]["canonical_target_vi"] == "so 1"
    assert entries["one"]["audit"]["audit_label"] == "generic_low_value"
    assert trail_by_id["one"]["status"] == "blocked_audit_label"


def test_ledger_promotion_matches_any_source_variant_surface():
    entry = _entry(
        "feature",
        "dac trung sai",
        "b_grad",
        conflicts=[
            {"type": "bad_existing_target", "proposed_target": "features", "evidence_block_ids": ["b_grad"]}
        ],
    )
    entry["source_variants"].append({"surface": "features", "evidence_block_ids": ["b_grad"], "occurrence_count": 1})
    promoted, trail = decollide.promote_ledger_canonical_candidates({"entries": [entry]})

    assert promoted["entries"][0]["canonical_target_vi"] == "features"
    assert trail[0]["status"] == "promoted_keep_source"


def test_ledger_promotion_production_function_has_no_fixture_term_literals():
    source = inspect.getsource(decollide.promote_ledger_canonical_candidates)
    for term in ("gradient", "tensor", "shape", "one"):
        assert re.search(rf"\b{re.escape(term)}\b", source) is None


def test_prompt_v2_owner_rule_rejects_resolve_without_owner(tmp_path: Path):
    groups = decollide.build_collision_groups(
        _notebook(),
        _db(tmp_path),
        prompt_version=decollide.PROMPT_VERSION_V2,
    )
    rule_group = next(group for group in groups if group["shared_canonical"] == "quy tắc nhân")
    rows = [
        {
            "entry_id": "product rule",
            "decision": "resolve_distinct",
            "chosen_canonical": "quy tắc tích",
            "confidence": "high",
            "reason": "wrongly moves one likely owner",
        },
        {
            "entry_id": "multiplication rule",
            "decision": "resolve_distinct",
            "chosen_canonical": "quy tắc phép nhân",
            "confidence": "medium",
            "reason": "wrongly moves the other likely owner",
        },
    ]

    with pytest.raises(ValueError, match="no keep_shared owner"):
        decollide.validate_decollision_results(rows, [rule_group], require_owner=True)


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
    assert report["ledger_promotion_summary"]["canonical_changed_count"] == 1
    assert report["groups"] == 1
    assert (out / "ledger_promotion_trail.json").exists()
    assert (out / "notebook_promoted.json").exists()
    assert (out / "collision_groups.json").exists()
    prompts = sorted((out / "prompts").glob("*.txt"))
    assert len(prompts) == 1
    assert "You resolve naming COLLISIONS" in prompts[0].read_text(encoding="utf-8")
