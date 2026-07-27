from __future__ import annotations

from copy import deepcopy
import json
import unicodedata
from pathlib import Path

import pytest

from pipeline.literary.b3_temporal_auditor_v1 import (
    build_b3_temporal_review_overlay_v1,
    synthetic_b3_temporal_review_packet_v1,
    synthetic_keep_pending_response_v1,
)
from pipeline.literary.b3_temporal_decision_ledger_v1 import (
    B3TemporalDecisionLedgerError,
    _retire_superseded_inherited_identity_cases_v1,
    _consolidate_effective_state_projection_v1,
    fold_b3_temporal_review_overlays_v1,
    merge_b3_narrative_frame_catalogs_v1,
    verify_b3_temporal_decision_ledger_v1,
)
from pipeline.literary.b3_temporal_prefix_v1 import (
    load_b3_temporal_chapter_artifact_v1,
)
from pipeline.literary.checkpoint import canonical_hash
from pipeline.scripts import (
    run_literary_b3_apply_temporal_review_decisions_v1 as runner,
)


def _sealed(body: dict, field: str) -> dict:
    return {**body, field: canonical_hash(body)}


def test_apply_resolves_nfc_path_to_nfd_filesystem_directory(tmp_path: Path) -> None:
    filesystem_name = unicodedata.normalize("NFD", "Tài liệu")
    canonical_name = unicodedata.normalize("NFC", filesystem_name)
    actual = tmp_path / filesystem_name
    actual.mkdir()

    resolved = runner._resolve_source_root(str(tmp_path / canonical_name))

    assert resolved == actual.resolve()


def _parked_index(*component_ids: str) -> dict:
    body = {
        "schema_version": "literary_b3_parked_identity_index_v1",
        "source_hearing_root": "test",
        "source_hearing_tree_hash": "a" * 64,
        "source_validated_decisions_sha256": "b" * 64,
        "parked_identities": [
            {
                "hearing_component_id": component_id,
                "resolution_condition": "A later source block must resolve it.",
                "card_ids": [f"card_{index}_a", f"card_{index}_b"],
            }
            for index, component_id in enumerate(component_ids)
        ],
    }
    return _sealed(body, "index_hash")


def _identity_projection(*component_ids: str) -> dict:
    body = {
        "schema_version": "literary_b1_reconciled_projection_v1",
        "book_id": "probe_book",
        "identity_authority_granted": False,
        "source_registry_hashes": [],
        "superseded_pending_cases": [
            {
                "component_id": component_id,
                "card_ids": [f"card_{index}_a", f"card_{index}_b"],
            }
            for index, component_id in enumerate(component_ids)
        ],
    }
    return _sealed(body, "projection_hash")


def _inherited_pending(component_id: str, case_id: str) -> dict:
    return {
        "pending_case_id": case_id,
        "review_route": "inherited_identity_block",
        "authority_status": "pending_review",
        "inherited_parked_identity": {
            "hearing_component_id": component_id,
            "resolution_condition": "A later source block must resolve it.",
        },
    }


def _fixtures():
    confirmed_packet = synthetic_b3_temporal_review_packet_v1(
        review_route="stable_claim_review"
    )
    referred_packet = synthetic_b3_temporal_review_packet_v1(
        review_route="temporal_review"
    )
    base_body = {
        "schema_version": "literary_b3_temporal_chapter_artifact_v1",
        "chapter_id": "probe_chapter",
        "effective_state_projection": [
            {
                "state_id": "existing_state",
                "authority_status": "effective",
            }
        ],
        "pending_cases": [
            deepcopy(confirmed_packet["pending_cases"][0]),
            deepcopy(referred_packet["pending_cases"][0]),
        ],
        "frame_catalog": {
            "frame_segments": [
                {
                    "frame_segment_id": "probe_frame",
                    "narrative_mode": "direct_current",
                    "narrator_surface": "Probe narrator",
                }
            ]
        },
        "identity_mutation_performed": False,
        "production_publish_performed": False,
    }
    base = _sealed(base_body, "artifact_hash")

    packets = []
    for source in (confirmed_packet, referred_packet):
        body = deepcopy(source)
        body.pop("packet_hash")
        body["source_b3_artifact_hash"] = base["artifact_hash"]
        packets.append(_sealed(body, "packet_hash"))
    confirmed_packet, referred_packet = packets

    case_id = confirmed_packet["pending_cases"][0]["pending_case_id"]
    confirmed_response = {
        "schema_version": "literary_b3_temporal_review_response_v1",
        "chapter_id": "probe_chapter",
        "case_decisions": [
            {
                "pending_case_id": case_id,
                "disposition": "confirm_state",
                "resolved_action": {
                    "operation": "open_state",
                    "state_domain": "ownership",
                    "subject_referent_refs": ["probe_owner"],
                    "counterpart_referent_refs": ["probe_place"],
                    "state_value": "owns",
                    "event_status": "occurred",
                    "temporal_position": "current_progression",
                    "source_event_ids": ["probe_event"],
                    "source_turn_ids": [],
                    "source_block_ids": ["probe_block"],
                    "frame_segment_ids": ["probe_frame"],
                    "reason": "The supplied statement supports a durable state.",
                },
                "cited_source_block_ids": ["probe_block"],
                "reason": "The claim is grounded.",
                "pending_reason_code": None,
            }
        ],
    }
    confirmed_overlay = build_b3_temporal_review_overlay_v1(
        packet=confirmed_packet, decision=confirmed_response
    )

    referred_response = synthetic_keep_pending_response_v1(referred_packet)
    referred_decision = referred_response["case_decisions"][0]
    referred_decision["disposition"] = "refer_identity"
    referred_decision["pending_reason_code"] = None
    referred_decision["reason"] = "Identity must be resolved before the state."
    referred_overlay = build_b3_temporal_review_overlay_v1(
        packet=referred_packet, decision=referred_response
    )
    return base, [
        (confirmed_overlay, confirmed_packet),
        (referred_overlay, referred_packet),
    ]


def test_fold_confirms_states_resolves_cases_and_retains_identity_referral() -> None:
    base, overlays = _fixtures()

    reconciled, ledger, report = fold_b3_temporal_review_overlays_v1(
        chapter_artifact=base, overlay_packets=overlays
    )

    referred_id = overlays[1][1]["pending_cases"][0]["pending_case_id"]
    assert len(reconciled["effective_state_projection"]) == 2
    assert [row["pending_case_id"] for row in reconciled["pending_cases"]] == [
        referred_id
    ]
    assert reconciled["referred_identity_case_ids"] == [referred_id]
    assert len(reconciled["confirmed_observation_rows"]) == 1
    assert report["before"] == {
        "effective_states": 1,
        "pending_cases": 2,
        "resolved_cases": 0,
    }
    assert report["after"] == {
        "effective_states": 2,
        "pending_cases": 1,
        "resolved_cases": 0,
    }
    assert verify_b3_temporal_decision_ledger_v1(ledger) == ledger


def test_cross_base_overlay_halts_before_fold() -> None:
    base, overlays = _fixtures()
    other_body = deepcopy(base)
    other_body.pop("artifact_hash")
    other_body["source_prefix_bundle_hash"] = "different"
    other = _sealed(other_body, "artifact_hash")

    with pytest.raises(B3TemporalDecisionLedgerError, match="G1"):
        fold_b3_temporal_review_overlays_v1(
            chapter_artifact=other, overlay_packets=[overlays[0]]
        )


def test_repeated_case_across_overlays_halts() -> None:
    base, overlays = _fixtures()

    with pytest.raises(B3TemporalDecisionLedgerError, match="G3"):
        fold_b3_temporal_review_overlays_v1(
            chapter_artifact=base,
            overlay_packets=[overlays[0], overlays[0]],
        )


def test_fold_is_independent_of_overlay_order() -> None:
    base, overlays = _fixtures()

    forward = fold_b3_temporal_review_overlays_v1(
        chapter_artifact=base, overlay_packets=overlays
    )
    reversed_order = fold_b3_temporal_review_overlays_v1(
        chapter_artifact=base, overlay_packets=list(reversed(overlays))
    )

    assert reversed_order == forward


def test_superseded_inherited_identity_hold_is_retired_but_history_is_kept() -> None:
    active = _inherited_pending("hearing_active", "case_active")
    stale = _inherited_pending("hearing_stale", "case_stale")
    temporal = {
        "pending_case_id": "case_temporal",
        "review_route": "temporal_review",
    }

    effective, superseded, projection_hash = (
        _retire_superseded_inherited_identity_cases_v1(
            pending_cases=[active, stale, temporal],
            parked_identity_index=_parked_index("hearing_active"),
            reconciled_identity_projection=_identity_projection("hearing_stale"),
        )
    )

    assert {row["pending_case_id"] for row in effective} == {
        "case_active",
        "case_temporal",
    }
    assert [row["pending_case_id"] for row in superseded] == ["case_stale"]
    assert superseded[0]["superseded_hearing_component_id"] == "hearing_stale"
    assert superseded[0]["authority_status"] == "pending_review"
    assert superseded[0]["superseded_by_identity_projection_hash"] == projection_hash


def test_active_or_not_explicitly_superseded_identity_hold_is_retained() -> None:
    active = _inherited_pending("hearing_active", "case_active")
    unrelated = _inherited_pending("hearing_unrelated", "case_unrelated")

    effective, superseded, _projection_hash = (
        _retire_superseded_inherited_identity_cases_v1(
            pending_cases=[active, unrelated],
            parked_identity_index=_parked_index("hearing_active"),
            reconciled_identity_projection=_identity_projection(
                "hearing_active", "hearing_other"
            ),
        )
    )

    assert {row["pending_case_id"] for row in effective} == {
        "case_active",
        "case_unrelated",
    }
    assert superseded == []


def test_tampered_identity_projection_halts_before_retirement() -> None:
    projection = _identity_projection("hearing_stale")
    projection["superseded_pending_cases"][0]["component_id"] = "tampered"

    with pytest.raises(B3TemporalDecisionLedgerError, match="hash mismatch"):
        _retire_superseded_inherited_identity_cases_v1(
            pending_cases=[_inherited_pending("hearing_stale", "case_stale")],
            parked_identity_index=_parked_index(),
            reconciled_identity_projection=projection,
        )


def _state(
    state_id: str,
    value: str,
    *,
    frame_ids: list[str],
    subject: str = "subject",
    semantic_key: str = "semantic",
    block: str = "b001",
) -> dict:
    return {
        "state_id": state_id,
        "semantic_key": semantic_key,
        "state_value": value,
        "subject_referent_refs": [subject],
        "counterpart_referent_refs": [],
        "frame_segment_ids": frame_ids,
        "source_block_ids": [block],
        "observed_at_block_id": block,
        "opened_by_observation_id": f"obs_{state_id}",
        "source_pending_case_id": None,
        "valid_from_block_id": None,
        "valid_to_block_id": None,
    }


_LAYER_CATALOG = {
    "frame_segments": [
        {
            "frame_segment_id": "frame_current",
            "narrative_mode": "direct_current",
            "narrator_surface": "Lockwood",
        },
        {
            "frame_segment_id": "frame_diary",
            "narrative_mode": "quoted_document",
            "narrator_surface": "Catherine",
        },
    ]
}


def test_state_consolidation_keeps_narrative_layers_separate_and_links_them() -> None:
    states = [
        _state("state_current", "hostile", frame_ids=["frame_current"]),
        _state("state_diary", "hostile", frame_ids=["frame_diary"]),
    ]

    folded, report, issues = _consolidate_effective_state_projection_v1(
        states, frame_catalog=_LAYER_CATALOG
    )

    assert len(folded) == 2
    assert report["duplicate_group_count"] == 0
    assert issues == []
    assert {
        tuple(row["corroborating_state_ids"]) for row in folded
    } == {("state_current",), ("state_diary",)}
    assert {
        row["corroboration_layers"][0]["narrator_surface"] for row in folded
    } == {"Lockwood", "Catherine"}


def test_state_consolidation_preserves_distinct_values_in_observations() -> None:
    states = [
        _state(
            "state_a",
            "hostile and degrading",
            frame_ids=["frame_current"],
            block="b001",
        ),
        _state(
            "state_b",
            "hostile and socially excluding",
            frame_ids=["frame_current"],
            block="b002",
        ),
    ]

    folded, report, issues = _consolidate_effective_state_projection_v1(
        states, frame_catalog=_LAYER_CATALOG
    )

    assert len(folded) == 1
    assert report["absorbed_state_ids"] == ["state_b"]
    assert [row["state_value"] for row in folded[0]["observations"]] == [
        "hostile and degrading",
        "hostile and socially excluding",
    ]
    assert folded[0]["state_value"] == "hostile and degrading"
    assert folded[0]["consolidated_state_ids"] == ["state_b"]
    assert issues == []


def test_state_with_mixed_frame_layers_is_left_standalone_with_issue() -> None:
    state = _state(
        "state_mixed",
        "a claim",
        frame_ids=["frame_current", "frame_diary"],
    )

    folded, report, issues = _consolidate_effective_state_projection_v1(
        [state], frame_catalog=_LAYER_CATALOG
    )

    assert folded == [state]
    assert report["after_count"] == 1
    assert issues[0]["row_type"] == "state_spans_multiple_narrative_layers"
    assert issues[0]["state_id"] == "state_mixed"


def test_absorbed_state_id_remains_resolvable_and_fold_is_idempotent() -> None:
    states = [
        _state("state_a", "first", frame_ids=["frame_current"], block="b001"),
        _state("state_b", "second", frame_ids=["frame_current"], block="b002"),
    ]

    folded, _report, _issues = _consolidate_effective_state_projection_v1(
        states, frame_catalog=_LAYER_CATALOG
    )
    assert "state_b" in folded[0]["consolidated_state_ids"]
    assert next(
        row for row in folded if row["state_id"] == "state_a"
    )["observations"][1]["state_value"] == "second"

    repeated, repeated_report, repeated_issues = (
        _consolidate_effective_state_projection_v1(
            folded, frame_catalog=_LAYER_CATALOG
        )
    )
    assert repeated == folded
    assert repeated_report["duplicate_group_count"] == 0
    assert repeated_issues == []


def test_frame_catalog_merge_is_cumulative_and_rejects_layer_drift() -> None:
    first = merge_b3_narrative_frame_catalogs_v1([_LAYER_CATALOG])
    second = merge_b3_narrative_frame_catalogs_v1(
        [
            first,
            {
                "frame_segments": [
                    {
                        "frame_segment_id": "frame_later",
                        "narrative_mode": "recollected",
                        "narrator_surface": "Nelly",
                    }
                ]
            },
        ]
    )
    assert [row["frame_segment_id"] for row in second["frame_segments"]] == [
        "frame_current",
        "frame_diary",
        "frame_later",
    ]

    conflicting = {
        "frame_segments": [
            {
                "frame_segment_id": "frame_current",
                "narrative_mode": "quoted_document",
                "narrator_surface": "Catherine",
            }
        ]
    }
    with pytest.raises(
        B3TemporalDecisionLedgerError,
        match="conflicting narrative layers",
    ):
        merge_b3_narrative_frame_catalogs_v1([first, conflicting])


def test_consolidation_only_refold_is_byte_stable() -> None:
    base, _overlays = _fixtures()
    body = deepcopy(base)
    body.pop("artifact_hash")
    body["effective_state_projection"] = [
        _state("state_a", "first", frame_ids=["frame_current"], block="b001"),
        _state("state_b", "second", frame_ids=["frame_current"], block="b002"),
    ]
    base = _sealed(body, "artifact_hash")

    first, _ledger, _report = fold_b3_temporal_review_overlays_v1(
        chapter_artifact=base,
        frame_catalog=_LAYER_CATALOG,
        consolidation_only=True,
    )
    repeated, _ledger, _report = fold_b3_temporal_review_overlays_v1(
        chapter_artifact=first,
        frame_catalog=first["frame_catalog"],
        consolidation_only=True,
    )

    assert repeated == first


def _identity_case(
    case_id: str,
    component_id: str,
    *,
    operation: str | None,
    subject_refs: list[str] | None,
    reason_codes: list[str] | None = None,
) -> dict:
    action = None
    if operation is not None:
        action = {
            "operation": operation,
            "subject_referent_refs": subject_refs or [],
        }
    return {
        "pending_case_id": case_id,
        "chapter_id": "probe_chapter",
        "component_id": component_id,
        "review_route": "identity_review",
        "authority_status": "pending_review",
        "reason": "Typed fixture.",
        "reason_codes": (
            reason_codes
            if reason_codes is not None
            else ["referent_identity_not_confirmed"]
        ),
        "proposed_action": action,
    }


def _identity_component(
    component_id: str,
    cards: list[tuple[str, str]],
    *,
    ambiguous_refs: list[str] | None = None,
) -> dict:
    body = {
        "candidate_cards": [
            {
                "candidate_card_id": card_id,
                "referent_ref": referent_ref,
            }
            for card_id, referent_ref in cards
        ],
        "b2_review_requests": [],
        "speaker_turns": (
            [
                {
                    "speaker": {
                        "resolution_status": "resolved_candidate",
                        "referent_refs": [],
                    },
                    "addressee": {
                        "resolution_status": "ambiguous_candidates",
                        "referent_refs": ambiguous_refs,
                    },
                }
            ]
            if ambiguous_refs
            else []
        ),
    }
    return {
        "component_id": component_id,
        **body,
        "component_hash": canonical_hash(body),
    }


def _identity_catalog(components: list[dict]) -> dict:
    body = {
        "schema_version": "literary_b3_temporal_component_catalog_v2",
        "chapter_id": "probe_chapter",
        "components": components,
    }
    return _sealed(body, "catalog_hash")


def test_identity_lifecycle_closes_onset_parks_ambiguity_and_routes_resolved() -> None:
    onset = _identity_case(
        "case_onset",
        "component_onset",
        operation="reveal_only",
        subject_refs=["ref_onset"],
    )
    blocked = _identity_case(
        "case_blocked",
        "component_blocked",
        operation=None,
        subject_refs=None,
    )
    already_parked = _identity_case(
        "case_already_parked",
        "component_already_parked",
        operation=None,
        subject_refs=None,
    )
    already_parked.update(
        {
            "lifecycle_state": "awaiting_identity_adapter",
            "parked_reason": "no implemented consumer; see B.3 amendment",
        }
    )
    neither = _identity_case(
        "case_neither",
        "component_neither",
        operation="open_state",
        subject_refs=["ref_neither"],
    )
    stable = _identity_case(
        "case_stable",
        "component_stable",
        operation="reinforce_state",
        subject_refs=["ref_stable"],
        reason_codes=[
            "referent_identity_not_confirmed",
            "stable_claim_domain_requires_review",
        ],
    )
    base_body = {
        "schema_version": "literary_b3_temporal_chapter_artifact_v1",
        "chapter_id": "probe_chapter",
        "effective_state_projection": [],
        "pending_cases": [onset, blocked, already_parked, neither, stable],
        "identity_mutation_performed": False,
        "production_publish_performed": False,
    }
    catalog = _identity_catalog(
        [
            _identity_component(
                "component_onset", [("card_onset", "ref_onset")]
            ),
            _identity_component(
                "component_blocked",
                [("card_a", "ref_a"), ("card_b", "ref_b")],
                ambiguous_refs=["ref_a", "ref_b"],
            ),
            _identity_component(
                "component_neither", [("card_neither", "ref_neither")]
            ),
            _identity_component(
                "component_stable", [("card_stable", "ref_stable")]
            ),
        ]
    )
    original = deepcopy(base_body)

    reconciled, _ledger, report = fold_b3_temporal_review_overlays_v1(
        chapter_artifact=_sealed(base_body, "artifact_hash"),
        identity_component_catalogs=[catalog],
        consolidation_only=True,
    )

    pending = {
        row["pending_case_id"]: row for row in reconciled["pending_cases"]
    }
    assert set(pending) == {
        "case_already_parked",
        "case_blocked",
        "case_neither",
        "case_stable",
    }
    assert pending["case_blocked"]["lifecycle_state"] == (
        "awaiting_identity_adapter"
    )
    assert pending["case_blocked"]["parked_reason"] == (
        "no implemented consumer; see B.3 amendment"
    )
    assert pending["case_already_parked"] == already_parked
    assert pending["case_neither"]["review_route"] == "temporal_review"
    assert pending["case_stable"]["review_route"] == "stable_claim_review"
    assert base_body == original
    assert [row["pending_case_id"] for row in reconciled["resolved_cases"]] == [
        "case_onset"
    ]
    resolved = reconciled["resolved_cases"][0]
    assert resolved["disposition"] == "origin_unknown"
    assert resolved["authority_status"] == "resolved_terminal"
    assert resolved["unknowable_window"] == {
        "from_chapter": "probe_chapter",
        "to_chapter": "probe_chapter",
        "blocker": "onset_not_stated_in_source",
    }
    assert reconciled["effective_state_projection"] == []
    lifecycle = report["identity_review_lifecycle"]
    assert lifecycle["classification_counts"] == {
        "identity_blocked": 2,
        "onset_unknown": 1,
        "neither": 2,
    }
    assert lifecycle["unchanged_identity_case_ids"] == []
    assert lifecycle["rerouted_temporal_case_ids"] == ["case_neither"]
    assert lifecycle["rerouted_stable_claim_case_ids"] == ["case_stable"]
    assert lifecycle["parked_identity_case_ids"] == [
        "case_already_parked",
        "case_blocked",
    ]


def test_resolved_identity_case_with_unspecified_reason_combination_halts() -> None:
    unsupported = _identity_case(
        "case_unsupported",
        "component_unsupported",
        operation="open_state",
        subject_refs=["ref_unsupported"],
        reason_codes=[
            "referent_identity_not_confirmed",
            "foreign_typed_reason",
        ],
    )
    base_body = {
        "schema_version": "literary_b3_temporal_chapter_artifact_v1",
        "chapter_id": "probe_chapter",
        "effective_state_projection": [],
        "pending_cases": [unsupported],
        "identity_mutation_performed": False,
        "production_publish_performed": False,
    }
    catalog = _identity_catalog(
        [
            _identity_component(
                "component_unsupported",
                [("card_unsupported", "ref_unsupported")],
            )
        ]
    )

    with pytest.raises(
        B3TemporalDecisionLedgerError,
        match="no specified typed route",
    ):
        fold_b3_temporal_review_overlays_v1(
            chapter_artifact=_sealed(base_body, "artifact_hash"),
            identity_component_catalogs=[catalog],
            consolidation_only=True,
        )


def test_origin_unknown_without_unknowable_window_fails_closed() -> None:
    invalid_resolved = {
        "pending_case_id": "case_onset",
        "chapter_id": "probe_chapter",
        "review_route": "identity_review",
        "authority_status": "resolved_terminal",
        "disposition": "origin_unknown",
    }
    base_body = {
        "schema_version": "literary_b3_temporal_chapter_artifact_v1",
        "chapter_id": "probe_chapter",
        "effective_state_projection": [],
        "pending_cases": [],
        "resolved_cases": [invalid_resolved],
        "identity_mutation_performed": False,
        "production_publish_performed": False,
    }

    with pytest.raises(
        B3TemporalDecisionLedgerError, match="unknowable_window"
    ):
        fold_b3_temporal_review_overlays_v1(
            chapter_artifact=_sealed(base_body, "artifact_hash"),
            consolidation_only=True,
        )


def test_unclassified_identity_case_without_component_evidence_halts() -> None:
    base_body = {
        "schema_version": "literary_b3_temporal_chapter_artifact_v1",
        "chapter_id": "probe_chapter",
        "effective_state_projection": [],
        "pending_cases": [
            _identity_case(
                "case_missing",
                "component_missing",
                operation=None,
                subject_refs=None,
            )
        ],
        "identity_mutation_performed": False,
        "production_publish_performed": False,
    }

    with pytest.raises(
        B3TemporalDecisionLedgerError, match="typed component evidence"
    ):
        fold_b3_temporal_review_overlays_v1(
            chapter_artifact=_sealed(base_body, "artifact_hash"),
            consolidation_only=True,
        )


def test_cli_writes_three_immutable_outputs_and_reconciled_root_loads(
    tmp_path: Path,
) -> None:
    base, overlays = _fixtures()
    b3_root = tmp_path / "b3"
    b3_root.mkdir()
    (b3_root / "chapter_temporal_artifact.json").write_text(
        json.dumps(base), encoding="utf-8"
    )
    overlay_roots = []
    for index, (overlay, packet) in enumerate(overlays, 1):
        root = tmp_path / f"overlay-{index}"
        root.mkdir()
        (root / "temporal_review_overlay.json").write_text(
            json.dumps(overlay), encoding="utf-8"
        )
        (root / "review_packet.json").write_text(
            json.dumps(packet), encoding="utf-8"
        )
        overlay_roots.append(root)
    out = tmp_path / "out"

    argv = ["--b3-root", str(b3_root)]
    for root in overlay_roots:
        argv.extend(["--overlay", str(root)])
    argv.extend(["--out-dir", str(out)])
    assert runner.main(argv) == 0

    assert {path.name for path in out.iterdir()} == {
        "apply_report.json",
        "frame_catalog.json",
        "reconciled_temporal_artifact.json",
        "temporal_decision_ledger.json",
    }
    loaded, path = load_b3_temporal_chapter_artifact_v1(out)
    assert path.name == "reconciled_temporal_artifact.json"
    assert loaded["artifact_hash"] == json.loads(
        (out / "apply_report.json").read_text(encoding="utf-8")
    )["reconciled_b3_artifact_hash"]
