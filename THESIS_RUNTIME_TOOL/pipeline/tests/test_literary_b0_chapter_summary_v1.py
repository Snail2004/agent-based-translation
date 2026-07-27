from __future__ import annotations

from copy import deepcopy

import pytest

from pipeline.literary.b0_chapter_summary_v1 import (
    B0ChapterSummaryError,
    append_capsule_log_v1,
    build_b0_summary_artifact_v1,
    build_capsule_log_v1,
    collapse_unresolved_cases_v1,
    make_b0_summary_semantic_validator_v1,
    render_b0_summary_request_v1,
    synthetic_b0_context_v1,
    synthetic_b0_response_v1,
    validate_b0_summary_response_v1,
    verify_b0_summary_artifact_v1,
    verify_capsule_log_v1,
    verify_b0_summary_context_v1,
)
from pipeline.literary.checkpoint import canonical_hash


def _parked_index():
    body = {
        "schema_version": "literary_b3_parked_identity_index_v1",
        "source_hearing_root": "synthetic",
        "source_hearing_tree_hash": "a" * 64,
        "source_validated_decisions_sha256": "b" * 64,
        "parked_identities": [
            {
                "hearing_component_id": "b1xhear_hareton",
                "resolution_condition": "A later dated block must settle the linkage.",
                "card_ids": ["b0ent_hareton", "b0ent_earnshaw", "b0ent_inscription"],
            }
        ],
    }
    return {**body, "index_hash": canonical_hash(body)}


def _rehash_packet(packet):
    body = deepcopy(packet)
    body.pop("packet_hash", None)
    return {**body, "packet_hash": canonical_hash(body)}


def _artifact(packet=None, response=None):
    packet = packet or synthetic_b0_context_v1()
    response = response or synthetic_b0_response_v1(packet)
    semantic = validate_b0_summary_response_v1(packet=packet, response=response)
    return build_b0_summary_artifact_v1(
        packet=packet,
        semantic_response=semantic,
        request_fingerprint="a" * 64,
        lineage={
            "b1_registry_hash": "b" * 64,
            "b2_artifact_hash": "c" * 64,
            "b3_artifact_hash": "d" * 64,
            "b3_review_overlay_hash": "e" * 64,
        },
    )


def test_context_exposes_only_chapter_scope_metadata() -> None:
    packet = synthetic_b0_context_v1()
    rendered = render_b0_summary_request_v1(packet)
    assert set(packet["chapter_metadata"]) == {"chapter_id", "chapter_order"}
    assert "book_id" not in packet["chapter_metadata"]
    assert rendered.packet == packet


def test_b3_context_is_optional() -> None:
    packet = synthetic_b0_context_v1(include_b3=False)
    assert packet["b3_context_status"] == "not_supplied"
    assert packet["b3_effective_states"] == []
    response = synthetic_b0_response_v1(packet)
    semantic = validate_b0_summary_response_v1(packet=packet, response=response)
    assert semantic["effective_state_refs"] == []
    assert semantic["capsule"]["state_refs"] == []


def test_b0_reads_only_effective_b2_review_projection() -> None:
    pending = synthetic_b0_context_v1(b2_speaker_review_status="pending")
    assert pending["b2_review_projection_status"] == "speaker_recovery_applied"
    assert [row["case_id"] for row in pending["unresolved_cases"]] == [
        "review_speaker_1"
    ]

    resolved = synthetic_b0_context_v1(b2_speaker_review_status="resolved")
    assert resolved["b2_review_projection_status"] == "speaker_recovery_applied"
    assert resolved["unresolved_cases"] == []


def test_b0_collapses_only_cases_tracing_to_the_same_parked_hearing() -> None:
    rows = collapse_unresolved_cases_v1(
        b1_cases=[
            {
                "continuity_case_id": "b1_case",
                "row_type": "cross_chapter_identity_linkage",
                "prior_card_id": "b0ent_inscription",
                "reason": "chapter-local prose is not used for routing",
            }
        ],
        b2_cases=[
            {
                "review_id": "b2_same_knot",
                "review_kind": "addressee_identity",
                "blocking_kind": "unresolved_entity",
                "candidate_card_ids": [
                    "b0ent_hareton",
                    "b0ent_earnshaw",
                    "b0ent_inscription",
                ],
            },
            {
                "review_id": "b2_other_question",
                "review_kind": "addressee_identity",
                "blocking_kind": "unresolved_entity",
                "competing_card_ids": [
                    "b0ent_hareton",
                    "b0ent_earnshaw",
                    "b0ent_heathcliff",
                ],
            },
        ],
        b3_cases=[
            {
                "pending_case_id": "b3_inherited",
                "review_route": "inherited_identity_block",
                "inherited_parked_identity": {
                    "hearing_component_id": "b1xhear_hareton",
                    "resolution_condition": "A later dated block must settle the linkage.",
                },
            }
        ],
        parked_identity_index=_parked_index(),
    )
    parked = [row for row in rows if row["kind"] == "parked_identity"]
    assert parked == [
        {
            "case_id": "b1xhear_hareton",
            "origin_stage": "identity_hearing",
            "kind": "parked_identity",
            "reason": "A later dated block must settle the linkage.",
            "resolution_condition": "A later dated block must settle the linkage.",
            "collapsed_case_count": 3,
            "authority": "non_authoritative",
        }
    ]
    assert any(row["case_id"] == "b2_other_question" for row in rows)


def test_b0_does_not_collapse_non_identity_review_with_same_card_set() -> None:
    rows = collapse_unresolved_cases_v1(
        b1_cases=[],
        b2_cases=[
            {
                "review_id": "b2_anchor",
                "review_kind": "source_anchor",
                "blocking_kind": "anchor_defect",
                "candidate_card_ids": [
                    "b0ent_hareton",
                    "b0ent_earnshaw",
                    "b0ent_inscription",
                ],
            }
        ],
        b3_cases=[],
        parked_identity_index=_parked_index(),
    )
    assert [row["case_id"] for row in rows] == ["b2_anchor"]


def test_b0_collapses_a_typed_parked_identity_carried_from_a_prior_chapter() -> None:
    condition = "A later source block must settle the prior identity linkage."
    rows = collapse_unresolved_cases_v1(
        b1_cases=[],
        b2_cases=[],
        b3_cases=[
            {
                "pending_case_id": "b3_prior_1",
                "review_route": "inherited_identity_block",
                "inherited_parked_identity": {
                    "hearing_component_id": "b1xhear_prior",
                    "resolution_condition": condition,
                },
            },
            {
                "pending_case_id": "b3_prior_2",
                "review_route": "inherited_identity_block",
                "inherited_parked_identity": {
                    "hearing_component_id": "b1xhear_prior",
                    "resolution_condition": condition,
                },
            },
        ],
        parked_identity_index=_parked_index(),
    )
    assert rows == [
        {
            "case_id": "b1xhear_prior",
            "origin_stage": "identity_hearing",
            "kind": "parked_identity",
            "reason": condition,
            "resolution_condition": condition,
            "collapsed_case_count": 2,
            "authority": "non_authoritative",
        }
    ]


def test_b0_rejects_conflicting_conditions_for_one_carried_hearing() -> None:
    with pytest.raises(B0ChapterSummaryError, match="differs"):
        collapse_unresolved_cases_v1(
            b1_cases=[],
            b2_cases=[],
            b3_cases=[
                {
                    "pending_case_id": "b3_prior_1",
                    "review_route": "inherited_identity_block",
                    "inherited_parked_identity": {
                        "hearing_component_id": "b1xhear_prior",
                        "resolution_condition": "condition one",
                    },
                },
                {
                    "pending_case_id": "b3_prior_2",
                    "review_route": "inherited_identity_block",
                    "inherited_parked_identity": {
                        "hearing_component_id": "b1xhear_prior",
                        "resolution_condition": "condition two",
                    },
                },
            ],
            parked_identity_index=_parked_index(),
        )


def test_b0_rejects_raw_speaker_reviews_without_effective_projection() -> None:
    with pytest.raises(B0ChapterSummaryError, match="effective review projection"):
        synthetic_b0_context_v1(b2_speaker_review_status="missing")


def test_unknown_structured_refs_are_quarantined_while_mentions_remain_text() -> None:
    packet = synthetic_b0_context_v1()
    response = synthetic_b0_response_v1(packet)
    response["narrative_handoff"]["frame_refs"].append("foreign_frame")
    response["narrative_handoff"]["entities_mentioned"].append("Unknown visitor")
    response["narrative_handoff"]["locations_mentioned"].append("Unknown place")
    response["salient_event_refs"].append("foreign_event")
    response["effective_state_refs"].append("foreign_state")
    response["unresolved_case_refs"].append("foreign_case")
    response["capsule"]["event_refs"].append("foreign_capsule_event")

    semantic = validate_b0_summary_response_v1(packet=packet, response=response)

    assert semantic["narrative_handoff"]["frame_refs"] == ["frame_1"]
    assert semantic["salient_event_refs"] == ["event_1"]
    assert semantic["effective_state_refs"] == ["state_1"]
    assert semantic["unresolved_case_refs"] == []
    assert semantic["narrative_handoff"]["entities_mentioned"] == [
        "Mara",
        "North House",
        "Unknown visitor",
    ]
    assert semantic["narrative_handoff"]["locations_mentioned"] == [
        "North House",
        "Unknown place",
    ]
    assert "unknown_refs_quarantined" in semantic["review_issues"]
    assert {row["ref"] for row in semantic["quarantined_refs"]} == {
        "foreign_frame",
        "foreign_event",
        "foreign_state",
        "foreign_case",
        "foreign_capsule_event",
    }


def test_entity_alias_resolves_only_in_structured_entity_refs() -> None:
    packet = synthetic_b0_context_v1()
    packet["id_alias_table"] = [
        {"alias_entity_id": "ent_mara_old", "canonical_entity_id": "ent_mara"}
    ]
    packet = _rehash_packet(packet)
    response = synthetic_b0_response_v1(packet)
    response["narrative_handoff"]["entities_mentioned"] = ["Mara"]
    response["capsule"]["entity_refs"] = ["ent_mara_old"]

    semantic = validate_b0_summary_response_v1(packet=packet, response=response)

    assert semantic["narrative_handoff"]["entities_mentioned"] == ["Mara"]
    assert semantic["capsule"]["entity_refs"] == ["ent_mara"]
    assert semantic["quarantined_refs"] == []


def test_budget_findings_are_visible_but_not_fatal() -> None:
    packet = synthetic_b0_context_v1()
    semantic = validate_b0_summary_response_v1(
        packet=packet, response=synthetic_b0_response_v1(packet)
    )
    assert "chapter_summary_under_target" in semantic["review_issues"]
    assert semantic["authority"] == "orientation_only"


def test_wrong_chapter_echo_is_normalized_without_dropping_summary() -> None:
    packet = synthetic_b0_context_v1()
    response = synthetic_b0_response_v1(packet)
    response["chapter_id"] = "copied_example_chapter"

    semantic = validate_b0_summary_response_v1(packet=packet, response=response)

    assert semantic["chapter_id"] == packet["chapter_metadata"]["chapter_id"]
    assert semantic["chapter_summary"]
    assert semantic["response_normalization_notes"][0]["field"] == "chapter_id"


def test_shared_semantic_validator_seals_summary_and_capsule() -> None:
    packet = synthetic_b0_context_v1()
    rendered = render_b0_summary_request_v1(packet)
    validator = make_b0_summary_semantic_validator_v1(
        packet=packet,
        rendered=rendered,
        lineage={"source_chapter_sha256": "f" * 64},
    )
    artifact = validator(synthetic_b0_response_v1(packet))
    assert verify_b0_summary_artifact_v1(artifact) == artifact
    assert artifact["summary"]["authority"] == "orientation_only"
    assert artifact["capsule"]["authority"] == "orientation_only"


def test_shared_validator_resolves_handoff_transport_labels_to_surfaces() -> None:
    packet = synthetic_b0_context_v1()
    rendered = render_b0_summary_request_v1(packet)
    response = synthetic_b0_response_v1(packet)
    response["narrative_handoff"]["entities_mentioned"] = ["E2", "E1"]
    response["narrative_handoff"]["locations_mentioned"] = ["E1"]
    validator = make_b0_summary_semantic_validator_v1(
        packet=packet,
        rendered=rendered,
        lineage={"source_chapter_sha256": "f" * 64},
    )

    artifact = validator(response)

    assert artifact["summary"]["narrative_handoff"]["entities_mentioned"] == [
        "Mara",
        "North House",
    ]
    assert artifact["summary"]["narrative_handoff"]["locations_mentioned"] == [
        "North House"
    ]
    assert "handoff_transport_labels_resolved" in artifact["summary"]["review_issues"]


def test_shared_validator_rejects_unknown_handoff_transport_label() -> None:
    packet = synthetic_b0_context_v1()
    rendered = render_b0_summary_request_v1(packet)
    response = synthetic_b0_response_v1(packet)
    response["narrative_handoff"]["entities_mentioned"] = ["E99"]
    validator = make_b0_summary_semantic_validator_v1(
        packet=packet,
        rendered=rendered,
        lineage={"source_chapter_sha256": "f" * 64},
    )

    with pytest.raises(B0ChapterSummaryError, match="unknown entity transport label"):
        validator(response)


def test_capsule_log_is_content_addressed_and_rejects_duplicate_chapter() -> None:
    artifact = _artifact()
    log = build_capsule_log_v1([artifact])
    assert log["authority"] == "orientation_only"
    assert log["append_only"] is True
    assert log["capsules"][0]["summary_artifact_hash"] == artifact["artifact_hash"]
    with pytest.raises(B0ChapterSummaryError, match="duplicate chapter"):
        build_capsule_log_v1([artifact, artifact])


def test_capsule_log_appends_the_immediate_prior_sequence() -> None:
    chapter_one = _artifact()
    prior_log = build_capsule_log_v1([chapter_one])
    packet = synthetic_b0_context_v1()
    packet["chapter_metadata"] = {
        "chapter_id": "probe_chapter_2",
        "chapter_order": 2,
    }
    packet = _rehash_packet(packet)
    chapter_two = _artifact(packet=packet)

    combined = append_capsule_log_v1(
        artifact=chapter_two,
        prior_log=prior_log,
    )

    assert verify_capsule_log_v1(combined) == combined
    assert [row["chapter_order"] for row in combined["capsules"]] == [1, 2]
    assert [row["chapter_id"] for row in combined["capsules"]] == [
        "probe_chapter",
        "probe_chapter_2",
    ]
    with pytest.raises(B0ChapterSummaryError, match="requires the prior capsule log"):
        append_capsule_log_v1(artifact=chapter_two, prior_log=None)


def test_context_and_artifact_hash_tamper_fail_closed() -> None:
    packet = synthetic_b0_context_v1()
    packet["chapter_source"][0]["text"] = "tampered"
    with pytest.raises(B0ChapterSummaryError, match="packet hash"):
        verify_b0_summary_context_v1(packet)

    artifact = _artifact()
    artifact["capsule"]["text"] = "tampered"
    with pytest.raises(B0ChapterSummaryError, match="artifact hash"):
        verify_b0_summary_artifact_v1(artifact)


def test_response_schema_rejects_unknown_fields() -> None:
    packet = synthetic_b0_context_v1()
    response = synthetic_b0_response_v1(packet)
    response["invented"] = True
    with pytest.raises(B0ChapterSummaryError, match="schema failure"):
        validate_b0_summary_response_v1(packet=packet, response=response)
