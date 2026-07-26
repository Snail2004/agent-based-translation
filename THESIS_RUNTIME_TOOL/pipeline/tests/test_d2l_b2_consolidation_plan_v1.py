from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from pipeline.prepass import d2l_b2_consolidation_plan_v1 as plan
from pipeline.prepass.d2l_b2_consolidation_contract_v1 import validate_output


def _row(
    candidate_id: str,
    surface: str,
    target: str | None,
    block_id: str,
    *,
    decision: str = "admit",
    alternatives: tuple[str, ...] = (),
) -> dict[str, object]:
    admitted = decision == "admit"
    targets = []
    if target is not None:
        targets.append({"target_vi": target, "applicability": None})
    targets.extend(
        {"target_vi": value, "applicability": "when context requires it"}
        for value in alternatives
    )
    return {
        "candidate_id": candidate_id,
        "chapter_id": "chapter_alpha",
        "surfaces": [surface],
        "decision": decision,
        "canonical_source": surface if admitted else None,
        "target_proposals": targets if admitted else [],
        "directive": "translate" if admitted else None,
        "evidence_block_ids": [block_id],
        "evidence_complete": True,
        "decision_rationale": "Source-grounded synthetic rationale.",
        "lineage": {
            "packet_id": "source_packet",
            "manifest_sha256": "A" * 64,
            "source_request_sha256": "B" * 64,
            "validation_sha256": "C" * 64,
        },
    }


def _index(rows: list[dict[str, object]], *, long_text: bool = False) -> dict:
    block_ids = sorted(
        {
            block_id
            for row in rows
            for block_id in row["evidence_block_ids"]
        }
    )
    value = {
        "index_version": plan.INDEX_VERSION,
        "chapter_ids": ["chapter_alpha"],
        "source_lineage": [
            {"manifest_sha256": "A" * 64, "valid_packet_ids": ["source_packet"]}
        ],
        "decisions": sorted(rows, key=plan._candidate_sort_key),
        "source_blocks": [
            {
                "block_id": block_id,
                "text": ("technical context " * 4000) if long_text else f"Text for {block_id}.",
            }
            for block_id in block_ids
        ],
    }
    value["counts"] = plan._index_counts(value["decisions"])
    value["index_sha256"] = plan._sha256_json(value)
    return value


def _valid_keep_separate_response(packet: dict) -> dict:
    decisions = []
    for component in packet["components"]:
        entries = []
        for member in component["members"]:
            entries.append(
                {
                    "member_candidate_ids": [member["candidate_id"]],
                    "canonical_source": member["canonical_source"],
                    "canonical_target_vi": member["target_proposals"][0]["target_vi"],
                    "alternative_targets": [],
                    "directive": member["directive"],
                    "evidence_block_ids": [member["evidence_block_ids"][0]],
                    "rationale": "The supplied contexts support a separate entry.",
                }
            )
        decisions.append(
            {
                "component_id": component["component_id"],
                "action": "keep_separate",
                "resolved_entries": entries,
                "pending_reason": None,
            }
        )
    return {"packet_id": packet["packet_id"], "decisions": decisions}


def _component_sources(component: dict) -> set[str]:
    return {str(row["canonical_source"]) for row in component["members"]}


def test_retrieval_signals_create_review_components_without_merge_authority() -> None:
    index = _index(
        [
            _row("cand_gradient", "gradient", "độ dốc", "b001"),
            _row("cand_gradients", "gradients", "độ dốc", "b002"),
            _row("cand_tensor", "tensor", "ten-xơ", "b003"),
            _row("cand_tensor_processing", "tensor processing", "xử lý ten-xơ", "b004"),
            _row("cand_sample", "sample", "mẫu", "b005"),
            _row("cand_instance", "instance", "mẫu", "b006"),
        ]
    )

    component_plan = plan.build_component_plan(index)

    source_sets = [_component_sources(row) for row in component_plan["components"]]
    assert {"gradient", "gradients"} in source_sets
    assert {"tensor", "tensor processing"} in source_sets
    assert {"sample", "instance"} in source_sets
    assert all("action" not in component for component in component_plan["components"])
    assert all(
        "canonical_target_vi" not in component
        for component in component_plan["components"]
    )


def test_multiple_targets_create_singleton_review_component() -> None:
    index = _index(
        [
            _row(
                "cand_vector",
                "vector",
                "véc-tơ",
                "b001",
                alternatives=("vector",),
            )
        ]
    )

    component_plan = plan.build_component_plan(index)

    assert component_plan["counts"]["components"] == 1
    assert component_plan["components"][0]["reason_codes"] == ["multiple_targets"]
    assert component_plan["counts"]["provisional_clean"] == 0


def test_ledgers_exact_cover_admit_review_and_reject() -> None:
    index = _index(
        [
            _row("cand_clean", "optimizer", "bộ tối ưu", "b001"),
            _row("cand_review", "order tensor", None, "b002", decision="review"),
            _row("cand_reject", "do the next thing", None, "b003", decision="reject"),
        ]
    )

    component_plan = plan.build_component_plan(index)

    assert component_plan["counts"] == {
        "components": 0,
        "component_members": 0,
        "provisional_clean": 1,
        "pending_admission": 1,
        "rejected": 1,
        "exact_cover": 3,
    }
    assert component_plan["provisional_clean"][0]["status"] == "provisional_clean"
    assert component_plan["pending_admission"][0]["status"] == "pending_admission"
    assert component_plan["rejected_ledger"][0]["status"] == "rejected"


def test_packetizer_batches_independent_components_and_respects_caps() -> None:
    index = _index(
        [
            _row("cand_a", "alpha", "một", "b001", alternatives=("an-pha",)),
            _row("cand_b", "beta", "hai", "b002", alternatives=("bê-ta",)),
        ]
    )
    component_plan = plan.build_component_plan(index)

    packets, dry = plan.packetize_components(
        plan=component_plan,
        index=index,
        caps=plan.ConsolidationCaps(),
    )

    assert len(packets) == 1
    assert len(packets[0]["components"]) == 2
    assert dry["no_api_called"] is True
    assert dry["totals"]["component_count"] == 2
    assert dry["packets"][0]["prompt_tokens_est"] <= 6000


def test_packetizer_fails_closed_when_atomic_component_exceeds_cap() -> None:
    index = _index(
        [
            _row(
                "cand_large",
                "large term",
                "thuật ngữ lớn",
                "b001",
                alternatives=("cách gọi dài",),
            )
        ],
        long_text=True,
    )
    component_plan = plan.build_component_plan(index)

    with pytest.raises(plan.ConsolidationPlanError, match="cannot fit"):
        plan.packetize_components(
            plan=component_plan,
            index=index,
            caps=plan.ConsolidationCaps(prompt_token_cap=500),
        )


def test_packetizer_can_excerpt_only_an_oversized_atomic_component() -> None:
    index = _index(
        [
            _row(
                "cand_large",
                "large term",
                "thuật ngữ lớn",
                "b001",
                alternatives=("cách gọi dài",),
            )
        ],
        long_text=True,
    )
    index["source_blocks"][0]["text"] = (
        ("unrelated technical context " * 500)
        + "large term appears in decisive context "
        + ("supporting technical context " * 500)
    )
    index["index_sha256"] = plan._sha256_json(
        {key: value for key, value in index.items() if key != "index_sha256"}
    )
    component_plan = plan.build_component_plan(index)

    packets, dry = plan.packetize_components(
        plan=component_plan,
        index=index,
        caps=plan.ConsolidationCaps(prompt_token_cap=1500),
        oversized_component_min_excerpt_chars=128,
    )

    assert len(packets) == 1
    assert "large term" in packets[0]["source_blocks"][0]["text"]
    assert len(packets[0]["source_blocks"][0]["text"]) < len(
        index["source_blocks"][0]["text"]
    )
    assert dry["packets"][0]["prompt_tokens_est"] <= 1500
    assert dry["totals"]["component_count"] == 1
    assert dry["totals"]["member_count"] == 1


def test_partition_oversized_component_prefers_strong_variant_edges() -> None:
    index = _index(
        [
            _row("cand_layer", "layer", "lớp", "b001"),
            _row("cand_layers", "layers", "lớp", "b002"),
            _row("cand_hidden", "hidden layer", "lớp ẩn", "b003"),
            _row("cand_custom", "custom layer", "lớp tùy chỉnh", "b004"),
            _row("cand_output", "output layer", "lớp đầu ra", "b005"),
        ]
    )
    component_plan = plan.build_component_plan(index)
    assert len(component_plan["components"]) == 1

    partitioned = plan.partition_oversized_components(
        plan=component_plan,
        index=index,
        caps=plan.ConsolidationCaps(
            max_components=6,
            max_members=2,
            max_unique_blocks=4,
            prompt_token_cap=6000,
        ),
        min_excerpt_chars=128,
    )
    member_sets = [
        {member["candidate_id"] for member in component["members"]}
        for component in partitioned["components"]
    ]

    assert {"cand_layer", "cand_layers"} in member_sets
    assert set().union(*member_sets) == {
        "cand_layer",
        "cand_layers",
        "cand_hidden",
        "cand_custom",
        "cand_output",
    }
    assert sum(len(member_ids) for member_ids in member_sets) == 5
    packets, dry = plan.packetize_components(
        plan=partitioned,
        index=index,
        caps=plan.ConsolidationCaps(
            max_components=6,
            max_members=2,
            max_unique_blocks=4,
            prompt_token_cap=6000,
        ),
        oversized_component_min_excerpt_chars=128,
    )
    assert packets
    assert dry["totals"]["member_count"] == 5
    assert dry["totals"]["component_count"] == len(partitioned["components"])


def test_writer_is_deterministic_and_retains_all_member_evidence() -> None:
    index = _index(
        [
            _row("cand_gradient", "gradient", "độ dốc", "b001"),
            _row("cand_gradients", "gradients", "độ dốc", "b002"),
        ]
    )
    component_plan = plan.build_component_plan(index)
    packets, _dry = plan.packetize_components(
        plan=component_plan,
        index=index,
        caps=plan.ConsolidationCaps(),
    )
    validations = []
    for packet in packets:
        response = _valid_keep_separate_response(packet)
        validation = validate_output(response, packet=packet)
        assert validation.errors == ()
        validations.append((packet, validation))

    first = plan.build_draft_package(
        index=index,
        plan=component_plan,
        packet_validations=validations,
    )
    second = plan.build_draft_package(
        index=index,
        plan=component_plan,
        packet_validations=list(reversed(validations)),
    )

    assert first == second
    assert first["production_published"] is False
    assert {tuple(row["evidence_block_ids"]) for row in first["audited_entries"]} == {
        ("b001",),
        ("b002",),
    }


def test_writer_rejects_missing_component_decisions() -> None:
    index = _index(
        [
            _row("cand_a", "alpha", "một", "b001", alternatives=("an-pha",)),
            _row("cand_b", "beta", "hai", "b002", alternatives=("bê-ta",)),
        ]
    )
    component_plan = plan.build_component_plan(index)
    packets, _dry = plan.packetize_components(
        plan=component_plan,
        index=index,
        caps=plan.ConsolidationCaps(),
    )
    response = _valid_keep_separate_response(packets[0])
    response["decisions"].pop()
    validation = validate_output(response, packet=packets[0])
    assert validation.errors

    with pytest.raises(plan.ConsolidationPlanError, match="invalid"):
        plan.build_draft_package(
            index=index,
            plan=component_plan,
            packet_validations=[(packets[0], validation)],
        )


def test_writer_preserves_pending_component_without_draft_entry() -> None:
    index = _index(
        [
            _row("cand_a", "alpha", "một", "b001"),
            _row("cand_as", "alphas", "một", "b002"),
        ]
    )
    component_plan = plan.build_component_plan(index)
    packets, _dry = plan.packetize_components(
        plan=component_plan,
        index=index,
        caps=plan.ConsolidationCaps(),
    )
    packet = packets[0]
    response = {
        "packet_id": packet["packet_id"],
        "decisions": [
            {
                "component_id": packet["components"][0]["component_id"],
                "action": "pending",
                "resolved_entries": [],
                "pending_reason": "The supplied contexts do not disambiguate the rows.",
            }
        ],
    }
    validation = validate_output(response, packet=packet)
    assert validation.errors == ()

    draft = plan.build_draft_package(
        index=index,
        plan=component_plan,
        packet_validations=[(packet, validation)],
    )

    assert draft["audited_entries"] == []
    assert len(draft["pending_components"]) == 1


def test_writer_canonicalizes_set_like_member_and_evidence_order() -> None:
    index = _index(
        [
            _row("cand_gradient", "gradient", "độ dốc", "b001"),
            _row("cand_gradients", "gradients", "độ dốc", "b002"),
        ]
    )
    component_plan = plan.build_component_plan(index)
    packets, _dry = plan.packetize_components(
        plan=component_plan,
        index=index,
        caps=plan.ConsolidationCaps(),
    )
    packet = packets[0]
    component = packet["components"][0]
    member_ids = [row["candidate_id"] for row in component["members"]]
    evidence = [
        block_id
        for row in component["members"]
        for block_id in row["evidence_block_ids"]
    ]

    def response(ids: list[str], evidence_ids: list[str]) -> dict:
        return {
            "packet_id": packet["packet_id"],
            "decisions": [
                {
                    "component_id": component["component_id"],
                    "action": "merge_all",
                    "resolved_entries": [
                        {
                            "member_candidate_ids": ids,
                            "canonical_source": "gradient",
                            "canonical_target_vi": "độ dốc",
                            "alternative_targets": [],
                            "directive": "translate",
                            "evidence_block_ids": evidence_ids,
                            "rationale": "The supplied contexts support one lexical entry.",
                        }
                    ],
                    "pending_reason": None,
                }
            ],
        }

    first_validation = validate_output(response(member_ids, evidence), packet=packet)
    second_validation = validate_output(
        response(list(reversed(member_ids)), list(reversed(evidence))), packet=packet
    )
    assert first_validation.errors == second_validation.errors == ()

    first = plan.build_draft_package(
        index=index,
        plan=component_plan,
        packet_validations=[(packet, first_validation)],
    )
    second = plan.build_draft_package(
        index=index,
        plan=component_plan,
        packet_validations=[(packet, second_validation)],
    )

    assert first == second


def test_write_plan_artifacts_is_write_or_verify(tmp_path: Path) -> None:
    index = _index(
        [_row("cand_a", "alpha", "một", "b001", alternatives=("an-pha",))]
    )
    component_plan = plan.build_component_plan(index)
    packets, dry = plan.packetize_components(
        plan=component_plan,
        index=index,
        caps=plan.ConsolidationCaps(),
    )

    plan.write_plan_artifacts(
        out_dir=tmp_path,
        index=index,
        plan=component_plan,
        packets=packets,
        dry_render=dry,
    )
    plan.write_plan_artifacts(
        out_dir=tmp_path,
        index=index,
        plan=component_plan,
        packets=packets,
        dry_render=dry,
    )
    request_path = tmp_path / "packets" / packets[0]["packet_id"] / "request.json"
    request = json.loads(request_path.read_text(encoding="utf-8"))
    assert request["packet"] == packets[0]

    request_path.write_text("{}\n", encoding="utf-8")
    with pytest.raises(plan.ConsolidationPlanError, match="overwrite"):
        plan.write_plan_artifacts(
            out_dir=tmp_path,
            index=index,
            plan=component_plan,
            packets=packets,
            dry_render=dry,
        )


def test_real_42_decision_lineage_is_deterministic_under_source_reorder() -> None:
    tool_root = Path(__file__).resolve().parents[2]
    data = tool_root / "data"
    requests_root = (
        data
        / "prepass"
        / "d2l_hardening_v1"
        / "b2_packet_plan_v2_preliminaries"
        / "packets"
    )
    if not requests_root.is_dir():
        pytest.skip("historical live evidence is not distributed with production code")
    sources = [
        plan.EvidenceSource(
            data
            / "reports"
            / "d2l_hardening_v1"
            / "b2_canary_shopaikey_v2"
            / "manifest.json",
            data
            / "prepass"
            / "d2l_hardening_v1"
            / "b2_canary_shopaikey_v2"
            / "packets",
        ),
        plan.EvidenceSource(
            data
            / "reports"
            / "d2l_hardening_v1"
            / "b2_partial_completion_shopaikey_v2"
            / "manifest.json",
            data
            / "prepass"
            / "d2l_hardening_v1"
            / "b2_partial_completion_shopaikey_v2"
            / "packets",
        ),
    ]

    first = plan.load_b2_evidence(
        sources=sources, requests_root=requests_root, expected_count=42
    )
    second = plan.load_b2_evidence(
        sources=list(reversed(sources)), requests_root=requests_root, expected_count=42
    )

    assert first == second
    assert first["counts"] == {"total": 42, "admit": 34, "review": 1, "reject": 7}
    component_plan = plan.build_component_plan(first)
    assert component_plan["counts"]["exact_cover"] == 42
    packets, dry = plan.packetize_components(
        plan=component_plan,
        index=first,
        caps=plan.ConsolidationCaps(),
    )
    assert packets
    assert dry["no_api_called"] is True
    assert all(row["prompt_tokens_est"] <= 6000 for row in dry["packets"])


def test_index_hash_tamper_is_rejected() -> None:
    index = _index([_row("cand_a", "alpha", "một", "b001")])
    tampered = copy.deepcopy(index)
    tampered["decisions"][0]["surfaces"] = ["changed"]

    with pytest.raises(plan.ConsolidationPlanError, match="hash mismatch"):
        plan.build_component_plan(tampered)
