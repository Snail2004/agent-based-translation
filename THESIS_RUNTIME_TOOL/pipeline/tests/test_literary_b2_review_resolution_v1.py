from __future__ import annotations

import pytest

from pipeline.literary.checkpoint import canonical_hash
from pipeline.literary.b2_review_resolution_v1 import (
    ReviewResolutionError,
    build_review_routing_plan_v1,
    build_review_routing_plan_from_artifacts_v1,
    open_cross_identity_cases_v1,
    resolve_route_b_review_v1,
    verify_review_routing_plan_v1,
)


def _cards() -> dict[str, dict]:
    return {
        "card_hareton": {
            "first_seen": {"chapter_id": "wh_ch02"},
            "source_refs": ["scan:hareton_full"],
        },
        "card_earnshaw": {
            "first_seen": {"chapter_id": "wh_ch02"},
            "source_refs": ["scan:earnshaw"],
        },
        "card_old": {
            "first_seen": {"chapter_id": "wh_ch01"},
            "source_refs": ["scan:old"],
        },
    }


def _review(cards: list[str]) -> dict:
    return {
        "review_id": "r1",
        "blocking_kind": "unresolved_entity",
        "competing_card_ids": cards,
    }


def _seal(value: dict, hash_field: str) -> dict:
    return {**value, hash_field: canonical_hash(value)}


def test_within_normalizes_card_ids_to_scan_refs_and_attaches_superset() -> None:
    result = resolve_route_b_review_v1(
        review=_review(["card_hareton", "card_earnshaw"]),
        cards_by_id=_cards(),
        current_chapter_id="wh_ch02",
        open_within_cases=[
            {
                "case_id": "local-1",
                "destination": "WITHIN",
                "member_refs": ["scan:hareton_full", "scan:earnshaw", "scan:extra"],
                "has_verdict": False,
            }
        ],
    )
    assert result["destination"] == "WITHIN"
    assert result["action"] == "attach_existing_case"
    assert result["case_id"] == "local-1"


def test_cross_uses_persistent_card_ids() -> None:
    result = resolve_route_b_review_v1(
        review=_review(["card_hareton", "card_old"]),
        cards_by_id=_cards(),
        current_chapter_id="wh_ch02",
        open_cross_cases=[
            {
                "case_id": "cross-1",
                "destination": "CROSS",
                "member_refs": ["card_hareton", "card_old", "card_other"],
                "has_verdict": False,
            }
        ],
    )
    assert result["destination"] == "CROSS"
    assert result["case_id"] == "cross-1"


def test_cross_review_pointing_at_superseded_case_is_retained_and_marked() -> None:
    queue = {
        "components": [
            {
                "component_id": "cross-old",
                "review_route": "identity_auditor",
                "prior_card_ids": ["card_old"],
                "current_entity_ids": ["card_hareton"],
            }
        ]
    }
    cases = open_cross_identity_cases_v1(
        queue,
        superseded_component_ids=["cross-old"],
    )

    result = resolve_route_b_review_v1(
        review=_review(["card_hareton", "card_old"]),
        cards_by_id=_cards(),
        current_chapter_id="wh_ch02",
        open_cross_cases=cases,
    )

    assert result["action"] == "attached_case_superseded"
    assert result["case_id"] == "cross-old"
    assert result["member_refs"] == ["card_hareton", "card_old"]


def test_superseded_cross_case_overrides_historical_decision() -> None:
    queue = {
        "components": [
            {
                "component_id": "cross-old",
                "review_route": "identity_auditor",
                "prior_card_ids": ["card_old"],
                "current_entity_ids": ["card_hareton"],
            }
        ]
    }

    cases = open_cross_identity_cases_v1(
        queue,
        decided_component_ids=["cross-old"],
        superseded_component_ids=["cross-old"],
    )

    assert cases == [
        {
            "case_id": "cross-old",
            "case_state": "superseded",
            "destination": "CROSS",
            "has_verdict": True,
            "member_refs": ["card_hareton", "card_old"],
        }
    ]


def test_partial_overlap_with_undecided_case_is_held_without_authority() -> None:
    result = resolve_route_b_review_v1(
        review=_review(["card_hareton", "card_earnshaw"]),
        cards_by_id=_cards(),
        current_chapter_id="wh_ch02",
        open_within_cases=[
            {
                "case_id": "local-1",
                "destination": "WITHIN",
                "member_refs": ["scan:hareton_full", "scan:other"],
                "has_verdict": False,
            }
        ],
    )
    assert result == {
        "review_id": "r1",
        "route": "B",
        "blocking_kind": "unresolved_entity",
        "competing_card_ids": ["card_hareton", "card_earnshaw"],
        "action": "hold_partial_overlap",
        "destination": "WITHIN",
        "case_id": "local-1",
        "member_refs": ["scan:earnshaw", "scan:hareton_full"],
        "matched_case_member_refs": ["scan:hareton_full", "scan:other"],
        "identity_authority_granted": False,
    }


def test_partial_overlap_with_decided_case_opens_new_case() -> None:
    result = resolve_route_b_review_v1(
        review=_review(["card_hareton", "card_earnshaw"]),
        cards_by_id=_cards(),
        current_chapter_id="wh_ch02",
        open_within_cases=[
            {
                "case_id": "local-1",
                "destination": "WITHIN",
                "member_refs": ["scan:hareton_full", "scan:other"],
                "has_verdict": True,
            }
        ],
    )
    assert result["action"] == "open_new_case"


def test_partial_overlap_with_undecided_cross_case_is_held_without_authority() -> None:
    result = resolve_route_b_review_v1(
        review=_review(["card_hareton", "card_old"]),
        cards_by_id=_cards(),
        current_chapter_id="wh_ch02",
        open_cross_cases=[
            {
                "case_id": "cross-1",
                "destination": "CROSS",
                "member_refs": ["card_hareton", "card_other"],
                "has_verdict": False,
            }
        ],
    )
    assert result == {
        "review_id": "r1",
        "route": "B",
        "blocking_kind": "unresolved_entity",
        "competing_card_ids": ["card_hareton", "card_old"],
        "action": "hold_partial_overlap",
        "destination": "CROSS",
        "case_id": "cross-1",
        "member_refs": ["card_hareton", "card_old"],
        "matched_case_member_refs": ["card_hareton", "card_other"],
        "identity_authority_granted": False,
    }


def test_decided_partial_overlap_does_not_attach_to_a_later_case() -> None:
    result = resolve_route_b_review_v1(
        review=_review(["card_hareton", "card_earnshaw"]),
        cards_by_id=_cards(),
        current_chapter_id="wh_ch02",
        open_within_cases=[
            {
                "case_id": "decided-overlap",
                "destination": "WITHIN",
                "member_refs": ["scan:hareton_full", "scan:other"],
                "has_verdict": True,
            },
            {
                "case_id": "later-superset",
                "destination": "WITHIN",
                "member_refs": [
                    "scan:hareton_full",
                    "scan:earnshaw",
                    "scan:extra",
                ],
                "has_verdict": False,
            },
        ],
    )
    assert result["action"] == "open_new_case"
    assert result["case_id"] is None


def test_routing_plan_keeps_route_c_model_free_route_d_typed_and_route_e_held() -> None:
    plan = build_review_routing_plan_v1(
        reviews=[
            {
                "review_id": "a",
                "blocking_kind": "scene_ambiguity",
                "competing_card_ids": [],
            },
            {
                "review_id": "b",
                "blocking_kind": "anchor_defect",
                "source_block_ids": ["b1"],
            },
            {
                "review_id": "c",
                "blocking_kind": "timeline_pending",
                "competing_card_ids": [],
            },
            {
                "review_id": "d",
                "blocking_kind": "frame_structure",
                "competing_card_ids": [],
            },
        ],
        cards_by_id=_cards(),
        current_chapter_id="wh_ch02",
    )
    assert [row["review_id"] for row in plan["route_a"]] == ["a"]
    assert plan["route_c"][0]["model_call_performed"] is False
    assert [row["review_id"] for row in plan["route_d"]] == ["c"]
    assert plan["route_e"] == [
        {
            "review_id": "d",
            "route": "E",
            "blocking_kind": "frame_structure",
        }
    ]
    with pytest.raises(ReviewResolutionError, match="non-route-B"):
        resolve_route_b_review_v1(
            review={
                "review_id": "d",
                "blocking_kind": "frame_structure",
                "competing_card_ids": [],
            },
            cards_by_id=_cards(),
            current_chapter_id="wh_ch02",
        )


def test_artifact_plan_attaches_mr_earnshaw_to_open_local_case() -> None:
    registry = _seal({
        "chapter_id": "wh_ch02",
        "cards": [
            {
                "entity_id": "card_hareton",
                "first_seen": {"chapter_id": "wh_ch02"},
                "source_refs": ["scan:hareton_full"],
            },
            {
                "entity_id": "card_earnshaw",
                "first_seen": {"chapter_id": "wh_ch02"},
                "source_refs": ["scan:earnshaw"],
            },
        ],
    }, "registry_hash")
    local_audit = _seal({
        "chapter_id": "wh_ch02",
        "decisions": [
            {
                "component_id": "b1lac_pending",
                "component_kind": "same_referent_proposal",
                "subject_ref": "scan:earnshaw",
                "original_proposal": {"target_ref": "scan:hareton_full"},
                "action": "keep_pending",
            }
        ],
    }, "artifact_hash")
    queue = _seal({
        "chapter_id": "wh_ch02",
        "components": [],
    }, "queue_hash")
    b2 = _seal({
        "chapter_id": "wh_ch02",
        "review_requests": [
            {
                "review_id": "mr_earnshaw_review",
                "blocking_kind": "unresolved_entity",
                "competing_card_ids": ["card_hareton", "card_earnshaw"],
            }
        ],
    }, "artifact_hash")
    plan = build_review_routing_plan_from_artifacts_v1(
        b2_artifact=b2,
        chapter_registry=registry,
        local_audit_artifact=local_audit,
        hearing_queue=queue,
    )
    assert plan["route_b"] == [
        {
            "review_id": "mr_earnshaw_review",
            "route": "B",
            "blocking_kind": "unresolved_entity",
            "competing_card_ids": ["card_hareton", "card_earnshaw"],
            "action": "attach_existing_case",
            "destination": "WITHIN",
            "case_id": "b1lac_pending",
            "member_refs": ["scan:earnshaw", "scan:hareton_full"],
            "matched_case_member_refs": [
                "scan:earnshaw",
                "scan:hareton_full",
            ],
        }
    ]
    assert plan["model_call_performed"] is False


def test_artifact_plan_routes_with_a_prior_card_snapshot_from_the_queue() -> None:
    registry = _seal(
        {
            "chapter_id": "wh_ch02",
            "cards": [
                {
                    "entity_id": "card_hareton",
                    "first_seen": {"chapter_id": "wh_ch02"},
                    "source_refs": ["scan:hareton_full"],
                }
            ],
        },
        "registry_hash",
    )
    local_audit = _seal(
        {"chapter_id": "wh_ch02", "decisions": []},
        "artifact_hash",
    )
    queue = _seal(
        {
            "chapter_id": "wh_ch02",
            "components": [
                {
                    "component_id": "cross-hareton",
                    "review_route": "identity_auditor",
                    "current_entity_ids": ["card_hareton"],
                    "prior_card_ids": ["card_old"],
                    "prior_candidate_snapshots": [
                        {
                            "prior_card_id": "card_old",
                            "canonical_surface": "Hareton Earnshaw",
                            "provenance_refs": [
                                {
                                    "chapter_id": "wh_ch01",
                                    "block_id": "wh_ch01_b012",
                                }
                            ],
                        }
                    ],
                }
            ],
        },
        "queue_hash",
    )
    b2 = _seal(
        {
            "chapter_id": "wh_ch02",
            "review_requests": [
                {
                    "review_id": "hareton_review",
                    "blocking_kind": "unresolved_entity",
                    "competing_card_ids": ["card_hareton", "card_old"],
                }
            ],
        },
        "artifact_hash",
    )

    plan = build_review_routing_plan_from_artifacts_v1(
        b2_artifact=b2,
        chapter_registry=registry,
        local_audit_artifact=local_audit,
        hearing_queue=queue,
    )

    assert plan["route_b"][0]["destination"] == "CROSS"
    assert plan["route_b"][0]["action"] == "attach_existing_case"
    assert plan["route_b"][0]["case_id"] == "cross-hareton"
    assert plan["route_b"][0]["member_refs"] == ["card_hareton", "card_old"]


def test_artifact_plan_routes_with_a_carried_b2_candidate_card() -> None:
    registry = _seal(
        {
            "chapter_id": "wh_ch06",
            "cards": [
                {
                    "entity_id": "card_nelly",
                    "first_seen": {"chapter_id": "wh_ch06"},
                    "source_refs": ["scan:nelly"],
                }
            ],
        },
        "registry_hash",
    )
    local_audit = _seal(
        {"chapter_id": "wh_ch06", "decisions": []},
        "artifact_hash",
    )
    queue = _seal(
        {"chapter_id": "wh_ch06", "components": []},
        "queue_hash",
    )
    b2 = _seal(
        {
            "chapter_id": "wh_ch06",
            "review_requests": [
                {
                    "review_id": "nelly_review",
                    "blocking_kind": "unresolved_entity",
                    "competing_card_ids": ["card_nelly", "card_mrs_dean"],
                }
            ],
        },
        "artifact_hash",
    )

    plan = build_review_routing_plan_from_artifacts_v1(
        b2_artifact=b2,
        chapter_registry=registry,
        local_audit_artifact=local_audit,
        hearing_queue=queue,
        candidate_scope_cards=[
            {
                "effective_entity_id": "card_mrs_dean",
                "first_seen": {"chapter_id": "wh_ch04"},
                "provenance_refs": [
                    {"chapter_id": "wh_ch04", "block_id": "wh_ch04_b001"}
                ],
            }
        ],
    )

    assert plan["route_b"][0]["destination"] == "CROSS"
    assert plan["route_b"][0]["action"] == "open_new_case"
    assert plan["route_b"][0]["member_refs"] == [
        "card_mrs_dean",
        "card_nelly",
    ]


def test_artifact_plan_rejects_a_competing_card_outside_registry_and_queue() -> None:
    with pytest.raises(
        ReviewResolutionError,
        match="route-B competing cards are outside the supplied scope",
    ):
        build_review_routing_plan_from_artifacts_v1(
            b2_artifact=_seal(
                {
                    "chapter_id": "wh_ch02",
                    "review_requests": [
                        {
                            "review_id": "foreign_card_review",
                            "blocking_kind": "unresolved_entity",
                            "competing_card_ids": [
                                "card_hareton",
                                "card_not_supplied",
                            ],
                        }
                    ],
                },
                "artifact_hash",
            ),
            chapter_registry=_seal(
                {
                    "chapter_id": "wh_ch02",
                    "cards": [
                        {
                            "entity_id": "card_hareton",
                            "first_seen": {"chapter_id": "wh_ch02"},
                            "source_refs": ["scan:hareton_full"],
                        }
                    ],
                },
                "registry_hash",
            ),
            local_audit_artifact=_seal(
                {"chapter_id": "wh_ch02", "decisions": []},
                "artifact_hash",
            ),
            hearing_queue=_seal(
                {"chapter_id": "wh_ch02", "components": []},
                "queue_hash",
            ),
        )


def test_artifact_plan_rejects_tampered_source_before_routing() -> None:
    b2 = _seal(
        {
            "chapter_id": "wh_ch02",
            "review_requests": [],
        },
        "artifact_hash",
    )
    b2["review_requests"].append(
        {
            "review_id": "late-tamper",
            "blocking_kind": "scene_ambiguity",
            "competing_card_ids": [],
        }
    )
    with pytest.raises(ReviewResolutionError, match="B2 artifact hash mismatch"):
        build_review_routing_plan_from_artifacts_v1(
            b2_artifact=b2,
            chapter_registry=_seal(
                {"chapter_id": "wh_ch02", "cards": []},
                "registry_hash",
            ),
            local_audit_artifact=_seal(
                {"chapter_id": "wh_ch02", "decisions": []},
                "artifact_hash",
            ),
            hearing_queue=_seal(
                {"chapter_id": "wh_ch02", "components": []},
                "queue_hash",
            ),
        )


def test_artifact_plan_hash_tamper_fails_closed() -> None:
    plan = {
        "schema_version": "literary_b2_review_routing_plan_v1",
        "chapter_id": "wh_ch02",
        "source_b2_artifact_hash": "b2",
        "source_registry_hash": "registry",
        "source_local_audit_artifact_hash": "local",
        "source_hearing_queue_hash": "queue",
        "route_a": [],
        "route_b": [],
        "route_c": [],
        "route_d": [],
        "route_e": [],
        "review_ids": [],
        "model_call_performed": False,
        "identity_authority_granted": False,
        "registry_mutation_performed": False,
        "routing_plan_hash": "wrong",
    }
    with pytest.raises(ReviewResolutionError, match="hash mismatch"):
        verify_review_routing_plan_v1(plan)
