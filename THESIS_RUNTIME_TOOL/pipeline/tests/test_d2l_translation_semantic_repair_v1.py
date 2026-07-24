from __future__ import annotations

from copy import deepcopy

import pytest

from pipeline.llm_backend import canonical_json
from pipeline.translate import d2l_translation_semantic_repair_v1 as contract


def _source_blocks() -> list[dict]:
    return [
        {
            "block_id": "b001",
            "block_type": "paragraph",
            "clean_text": "The surrounding explanation remains unchanged.",
        },
        {
            "block_id": "b002",
            "block_type": "paragraph",
            "clean_text": "The value $x$ must increase, not decrease.",
        },
    ]


def _translations() -> dict[str, dict]:
    return {
        "b001": {
            "block_id": "b001",
            "status": "translated",
            "target_text": "Phần giải thích xung quanh không thay đổi.",
        },
        "b002": {
            "block_id": "b002",
            "status": "translated",
            "target_text": "Giá trị $x$ phải giảm.",
        },
    }


def _finding() -> dict[str, str]:
    return {
        "block_id": "b002",
        "issue_type": "polarity_or_negation_error",
        "severity": "major",
        "source_evidence": "increase, not decrease",
        "target_evidence": "giảm",
        "reason": "The target reverses the required direction.",
    }


def _context_pack() -> dict:
    return {
        "glossary_lines": ["value -> giá trị"],
        "preserve_lines": [],
        "context_sensitive_lines": ["increase -> tăng when direction is positive"],
        "entity_lines": [],
        "address_lines": [],
        "token_estimate": 12,
        "anchors": {"block_ids": ["b001", "b002"]},
        "anchors_count": {},
        "dropped_by_budget": [],
        "repair_queue": [],
        "low_context": False,
        "warnings": [],
    }


def _plan() -> contract.SemanticRepairPlan:
    return contract.build_plan(
        window_id="w001",
        arm_id="s1",
        source_blocks=_source_blocks(),
        current_translations=_translations(),
        output_block_ids=["b002"],
        active_semantic_findings=[_finding()],
        resolved_integrity_history=[
            {
                "source": "deterministic_integrity",
                "scope": "block",
                "block_id": "b002",
                "issue_type": "unexpected_output_script",
                "evidence": "foreign-script-token",
            }
        ],
        original_context_pack=_context_pack(),
    )


def test_repair_plan_keeps_full_window_and_exact_initial_s1_context() -> None:
    plan = _plan()
    packet = plan.packet

    assert [row["block_id"] for row in packet["context_blocks"]] == ["b001", "b002"]
    assert packet["output_block_ids"] == ["b002"]
    assert packet["context_blocks"][0]["editable"] is False
    assert packet["context_blocks"][1]["editable"] is True
    assert "$x$" not in packet["context_blocks"][1]["source_protected_text"]
    assert "$x$" not in packet["context_blocks"][1]["current_target_protected_text"]
    assert "[[MATH_REF_0001]]" in packet["context_blocks"][1][
        "current_target_protected_text"
    ]
    assert packet["translator_context"]["glossary_lines"] == ["value -> giá trị"]
    assert packet["translator_context"]["context_pack_sha256"]
    assert packet["resolved_integrity_history"] == [
        {
            "scope": "block",
            "block_id": "b002",
            "issue_type": "unexpected_output_script",
            "evidence": "foreign-script-token",
            "status": "resolved_do_not_regress",
        }
    ]


def test_repair_plan_hides_latex_inside_auditor_evidence() -> None:
    finding = _finding()
    finding["source_evidence"] = "$x$"
    finding["target_evidence"] = "$x$"
    plan = contract.build_plan(
        window_id="w001",
        arm_id="s1",
        source_blocks=_source_blocks(),
        current_translations=_translations(),
        output_block_ids=["b002"],
        active_semantic_findings=[finding],
        resolved_integrity_history=[],
        original_context_pack=_context_pack(),
    )

    assert "$x$" not in canonical_json(plan.packet)
    assert plan.packet["active_semantic_findings"][0]["source_evidence"] == (
        "[[MATH_REF_0001]]"
    )


def test_repair_response_restores_protected_bytes_and_only_target_block() -> None:
    plan = _plan()
    current = next(
        row["current_target_protected_text"]
        for row in plan.packet["context_blocks"]
        if row["block_id"] == "b002"
    )
    repaired = str(current).replace("phải giảm", "phải tăng, không được giảm")
    result = contract.validate_and_restore(
        {
            "contract_version": contract.RESPONSE_CONTRACT_VERSION,
            "window_id": "w001",
            "repairs": [
                {
                    "block_id": "b002",
                    "repaired_target_protected_text": repaired,
                }
            ],
        },
        plan,
    )

    assert result["output_block_ids"] == ["b002"]
    assert result["updates"] == {
        "b002": "Giá trị $x$ phải tăng, không được giảm."
    }


def test_repair_rejects_placeholder_or_output_scope_drift() -> None:
    plan = _plan()
    current = next(
        row["current_target_protected_text"]
        for row in plan.packet["context_blocks"]
        if row["block_id"] == "b002"
    )
    missing_ref = str(current).replace("[[MATH_REF_0001]]", "")
    with pytest.raises(contract.SemanticRepairContractError):
        contract.validate_and_restore(
            {
                "contract_version": contract.RESPONSE_CONTRACT_VERSION,
                "window_id": "w001",
                "repairs": [
                    {
                        "block_id": "b002",
                        "repaired_target_protected_text": missing_ref,
                    }
                ],
            },
            plan,
        )

    extra = deepcopy(
        {
            "contract_version": contract.RESPONSE_CONTRACT_VERSION,
            "window_id": "w001",
            "repairs": [
                {
                    "block_id": "b002",
                    "repaired_target_protected_text": current,
                }
            ],
        }
    )
    extra["repairs"].append(
        {"block_id": "b001", "repaired_target_protected_text": "Không hợp lệ."}
    )
    with pytest.raises(contract.SemanticRepairContractError):
        contract.validate_and_restore(extra, plan)


def test_s0_repair_cannot_receive_a_glossary_pack() -> None:
    with pytest.raises(contract.SemanticRepairContractError):
        contract.build_plan(
            window_id="w001",
            arm_id="s0",
            source_blocks=_source_blocks(),
            current_translations=_translations(),
            output_block_ids=["b002"],
            active_semantic_findings=[_finding()],
            resolved_integrity_history=[],
            original_context_pack=_context_pack(),
        )


def test_fixed_only_semantic_repair_discards_model_authored_prose() -> None:
    source = r"$$f'(x) = \lim_{h \rightarrow 0} \frac{f(x+h)-f(x)}{h}$$"
    plan = contract.build_plan(
        window_id="w_fixed",
        arm_id="s0",
        source_blocks=[
            {
                "block_id": "b_fixed",
                "block_type": "paragraph",
                "clean_text": source,
            }
        ],
        current_translations={
            "b_fixed": {
                "block_id": "b_fixed",
                "status": "translated",
                "target_text": source,
            }
        },
        output_block_ids=["b_fixed"],
        active_semantic_findings=[
            {
                "block_id": "b_fixed",
                "issue_type": "unsupported_addition",
                "severity": "major",
                "source_evidence": source,
                "target_evidence": source,
                "reason": "The target must not add prose to a fixed-only block.",
            }
        ],
        resolved_integrity_history=[],
        original_context_pack=None,
    )
    current = next(
        row["current_target_protected_text"]
        for row in plan.packet["context_blocks"]
    )

    result = contract.validate_and_restore(
        {
            "contract_version": contract.RESPONSE_CONTRACT_VERSION,
            "window_id": "w_fixed",
            "repairs": [
                {
                    "block_id": "b_fixed",
                    "repaired_target_protected_text": "Ta có " + str(current),
                }
            ],
        },
        plan,
    )

    assert result["updates"] == {"b_fixed": source}
