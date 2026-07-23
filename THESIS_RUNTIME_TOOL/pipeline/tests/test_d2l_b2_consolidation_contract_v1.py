from __future__ import annotations

import copy

import pytest

from pipeline.prepass.d2l_b2_consolidation_contract_v1 import (
    ConsolidationContractError,
    parse_response_json,
    prompt_sha256,
    render_messages,
    response_schema_sha256,
    validate_output,
)


def _member(
    candidate_id: str,
    surface: str,
    target: str,
    block_id: str,
    *,
    alternatives: tuple[str, ...] = (),
) -> dict:
    return {
        "candidate_id": candidate_id,
        "canonical_source": surface,
        "surfaces": [surface, surface.title()],
        "target_proposals": [
            {"target_vi": value, "applicability": None}
            for value in (target, *alternatives)
        ],
        "directive": "translate",
        "evidence_block_ids": [block_id],
        "evidence_complete": True,
        "decision_rationale": "The source uses a reusable technical unit.",
    }


def _packet() -> dict:
    return {
        "packet_id": "cpkt_fixture",
        "chapter_id": "chapter_fixture",
        "components": [
            {
                "component_id": "cmp_fixture",
                "reason_codes": [
                    "source_form_variant",
                    "multiple_targets",
                ],
                "members": [
                    _member(
                        "cand_a",
                        "vector unit",
                        "don vi vecto",
                        "b1",
                        alternatives=("phan tu vecto",),
                    ),
                    _member(
                        "cand_b",
                        "vector units",
                        "don vi vecto",
                        "b2",
                    ),
                    _member(
                        "cand_c",
                        "vector unit operation",
                        "phep toan don vi vecto",
                        "b3",
                    ),
                ],
                "edges": [
                    {
                        "left_candidate_id": "cand_a",
                        "right_candidate_id": "cand_b",
                        "signals": ["source_form_variant", "shared_target"],
                    },
                    {
                        "left_candidate_id": "cand_a",
                        "right_candidate_id": "cand_c",
                        "signals": ["source_token_containment"],
                    },
                ],
                "source_block_ids": ["b1", "b2", "b3"],
            }
        ],
        "source_blocks": [
            {"block_id": "b1", "text": "A vector unit is defined here."},
            {"block_id": "b2", "text": "Several vector units are compared."},
            {
                "block_id": "b3",
                "text": "A vector unit operation is a separate procedure.",
            },
        ],
    }


def _valid_partition() -> dict:
    return {
        "packet_id": "cpkt_fixture",
        "decisions": [
            {
                "component_id": "cmp_fixture",
                "action": "partition",
                "resolved_entries": [
                    {
                        "member_candidate_ids": ["cand_a", "cand_b"],
                        "canonical_source": "vector unit",
                        "canonical_target_vi": "don vi vecto",
                        "alternative_targets": [
                            {
                                "target_vi": "phan tu vecto",
                                "applicability": "Only for the element sense.",
                            }
                        ],
                        "directive": "translate",
                        "evidence_block_ids": ["b1", "b2"],
                        "rationale": "The two rows differ only in number.",
                    },
                    {
                        "member_candidate_ids": ["cand_c"],
                        "canonical_source": "vector unit operation",
                        "canonical_target_vi": "phep toan don vi vecto",
                        "alternative_targets": [],
                        "directive": "translate",
                        "evidence_block_ids": ["b3"],
                        "rationale": "The longer expression names a distinct unit.",
                    },
                ],
                "pending_reason": None,
            }
        ],
    }


def test_prompt_and_schema_are_stable_and_book_neutral() -> None:
    assert len(prompt_sha256()) == 64
    assert len(response_schema_sha256()) == 64
    rendered = render_messages(_packet())
    assert rendered == render_messages(_packet())
    assert "vector unit" not in rendered[0]["content"]
    assert "community gold" not in rendered[0]["content"].casefold()


def test_partition_exact_cover_is_valid() -> None:
    validation = validate_output(_valid_partition(), packet=_packet())
    assert validation.errors == ()
    assert validation.missing_component_ids == ()
    assert validation.decisions[0].action == "partition"
    assert validation.normalization_warnings == ()
    assert [
        entry.member_candidate_ids
        for entry in validation.decisions[0].resolved_entries
    ] == [("cand_a", "cand_b"), ("cand_c",)]


def test_one_group_exact_cover_partition_normalizes_to_merge_all() -> None:
    payload = _valid_partition()
    entry = payload["decisions"][0]["resolved_entries"][0]
    entry["member_candidate_ids"] = ["cand_a", "cand_b", "cand_c"]
    entry["evidence_block_ids"] = ["b1", "b2", "b3"]
    payload["decisions"][0]["resolved_entries"] = [entry]

    validation = validate_output(payload, packet=_packet())

    assert validation.errors == ()
    assert validation.decisions[0].action == "merge_all"
    assert len(validation.normalization_warnings) == 1
    assert "one-group exact-cover partition" in validation.normalization_warnings[0]


def test_all_singleton_exact_cover_partition_normalizes_to_keep_separate() -> None:
    payload = _valid_partition()
    merged_entry = payload["decisions"][0]["resolved_entries"][0]
    split_entries = []
    for candidate_id, surface, target, block_id in (
        ("cand_a", "vector unit", "don vi vecto", "b1"),
        ("cand_b", "vector units", "don vi vecto", "b2"),
    ):
        entry = dict(merged_entry)
        entry["member_candidate_ids"] = [candidate_id]
        entry["canonical_source"] = surface
        entry["canonical_target_vi"] = target
        entry["alternative_targets"] = []
        entry["evidence_block_ids"] = [block_id]
        split_entries.append(entry)
    split_entries.append(payload["decisions"][0]["resolved_entries"][1])
    payload["decisions"][0]["resolved_entries"] = split_entries

    validation = validate_output(payload, packet=_packet())

    assert validation.errors == ()
    assert validation.decisions[0].action == "keep_separate"
    assert len(validation.normalization_warnings) == 1
    assert "all-singleton exact-cover partition" in (
        validation.normalization_warnings[0]
    )


def test_pending_requires_reason_and_zero_entries() -> None:
    payload = {
        "packet_id": "cpkt_fixture",
        "decisions": [
            {
                "component_id": "cmp_fixture",
                "action": "pending",
                "resolved_entries": [],
                "pending_reason": "The supplied blocks do not settle identity.",
            }
        ],
    }
    assert validate_output(payload, packet=_packet()).errors == ()
    payload["decisions"][0]["pending_reason"] = None
    assert "pending_reason is required" in validate_output(
        payload, packet=_packet()
    ).errors[0]


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("canonical_source", "invented source", "not a supplied member surface"),
        ("canonical_target_vi", "invented target", "not a supplied B2 target"),
        ("directive", "preserve", "not supplied by assigned members"),
        ("evidence_block_ids", ["outside"], "outside assigned members"),
    ],
)
def test_resolved_entry_rejects_invented_values(
    field: str, value: object, message: str
) -> None:
    payload = _valid_partition()
    payload["decisions"][0]["resolved_entries"][0][field] = value
    validation = validate_output(payload, packet=_packet())
    assert message in validation.errors[0]


def test_alternative_target_requires_explicit_applicability() -> None:
    payload = _valid_partition()
    payload["decisions"][0]["resolved_entries"][0]["alternative_targets"][0][
        "applicability"
    ] = ""
    validation = validate_output(payload, packet=_packet())
    assert "applicability is required" in validation.errors[0]


def test_duplicate_or_missing_members_fail_partition() -> None:
    duplicate = _valid_partition()
    duplicate["decisions"][0]["resolved_entries"][1][
        "member_candidate_ids"
    ] = ["cand_a"]
    duplicate["decisions"][0]["resolved_entries"][1].update(
        {
            "canonical_source": "vector unit",
            "canonical_target_vi": "don vi vecto",
            "evidence_block_ids": ["b1"],
        }
    )
    assert "assigns a candidate more than once" in validate_output(
        duplicate, packet=_packet()
    ).errors[0]

    missing = _valid_partition()
    missing["decisions"][0]["resolved_entries"].pop()
    assert "does not exact-partition" in validate_output(
        missing, packet=_packet()
    ).errors[0]


def test_action_cardinality_is_closed() -> None:
    merge = _valid_partition()
    merge["decisions"][0]["action"] = "merge_all"
    assert "merge_all has invalid cardinality" in validate_output(
        merge, packet=_packet()
    ).errors[0]

    separate = _valid_partition()
    separate["decisions"][0]["action"] = "keep_separate"
    assert "keep_separate has invalid cardinality" in validate_output(
        separate, packet=_packet()
    ).errors[0]


def test_component_exact_cover_rejects_unknown_duplicate_and_missing() -> None:
    missing = {"packet_id": "cpkt_fixture", "decisions": []}
    assert validate_output(missing, packet=_packet()).missing_component_ids == (
        "cmp_fixture",
    )

    unknown = _valid_partition()
    unknown["decisions"][0]["component_id"] = "unknown"
    assert "unknown component_id" in validate_output(
        unknown, packet=_packet()
    ).errors[0]

    duplicate = _valid_partition()
    duplicate["decisions"].append(copy.deepcopy(duplicate["decisions"][0]))
    assert validate_output(
        duplicate, packet=_packet()
    ).duplicate_component_ids == ("cmp_fixture",)


def test_packet_rejects_cross_component_candidate_reuse() -> None:
    packet = _packet()
    second = copy.deepcopy(packet["components"][0])
    second["component_id"] = "cmp_second"
    packet["components"].append(second)
    with pytest.raises(ConsolidationContractError, match="candidate_id is invalid"):
        render_messages(packet)


def test_parse_response_json_rejects_unknown_top_level_fields() -> None:
    payload = _valid_partition()
    payload["extra"] = True
    with pytest.raises(ConsolidationContractError, match="Top-level keys"):
        parse_response_json(payload)
