from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict
import json
from pathlib import Path
import subprocess
import sys

import pytest

from pipeline.literary.b0_entity_prior_challenge_experiment import (
    EXPERIMENT_SCHEMA_VERSION,
    ISSUE_TO_FIELD,
    build_prior_packets,
)
from pipeline.literary.book_entity_claim_auditor_v1 import (
    CLAIM_ACTIONS,
    BookEntityClaimContractError,
    build_prior_claim_projection_v1,
    build_prior_claim_revision_ledger_v1,
    build_prior_claim_ticket_index_v1,
    classify_pending_claim_reopen_v1,
    dry_render_prior_claim_requests_v1,
    render_prior_claim_request_v1,
    validate_prior_claim_response_v1,
    verify_prior_claim_decision_v1,
    verify_prior_claim_revision_ledger_v1,
    verify_prior_claim_ticket_index_v1,
)
from pipeline.literary.book_entity_claim_auditor_batch_v1 import (
    render_prior_claim_batch_request_v1,
    validate_prior_claim_batch_response_v1,
)
from pipeline.literary.checkpoint import canonical_hash, canonical_json


RUNTIME_ROOT = Path(__file__).resolve().parents[2]
DESIGN_DOC = RUNTIME_ROOT.parent / "design" / "LITERARY_PROMPT_DESIGN.md"
FIXTURE_ROOT = Path(__file__).resolve().parent / "fixtures"
SOURCE_FIXTURE = FIXTURE_ROOT / "literary_prior_claim_source_v1.json"
ORACLE_FIXTURE = FIXTURE_ROOT / "literary_prior_claim_oracle_v1.json"
REGISTRY_GENERATION_HASH = canonical_hash({"generation": "synthetic_generation_v1"})


def _document() -> dict:
    return json.loads(SOURCE_FIXTURE.read_text(encoding="utf-8"))


def _chapter(chapter_id: str) -> dict:
    return next(
        row for row in _document()["chapters"] if row["chapter_id"] == chapter_id
    )


def _card(
    prior_card_id: str,
    surface: str,
    *,
    kind: str,
    gender: str | None,
    summary: str,
    first_supported_block_id: str,
    provenance_refs: list[tuple[str, str]],
) -> dict:
    return {
        "prior_card_id": prior_card_id,
        "canonical_surface": surface,
        "stable_surfaces": [surface],
        "referent_kind": kind,
        "referential_gender": gender,
        "identity_summary": summary,
        "authority_scope": "test_verified_global_as_of_prior_scope",
        "first_supported_block_id": first_supported_block_id,
        "provenance_refs": [
            {"chapter_id": chapter_id, "block_id": block_id}
            for chapter_id, block_id in provenance_refs
        ],
    }


def _cards() -> list[dict]:
    return [
        _card(
            "prior_rowan",
            "Captain Rowan",
            kind="person",
            gender="masculine",
            summary="The archive officer identified by the stable titled name.",
            first_supported_block_id="syn_ch01_b003",
            provenance_refs=[("syn_ch01", "syn_ch01_b003")],
        ),
        _card(
            "prior_mira",
            "Mira Voss",
            kind="person",
            gender="masculine",
            summary="A named visitor associated with a sealed parcel.",
            first_supported_block_id="syn_ch01_b005",
            provenance_refs=[("syn_ch01", "syn_ch01_b005")],
        ),
        _card(
            "prior_north_gate",
            "North Gate",
            kind="institution",
            gender=None,
            summary="A stable named entry associated with the archive grounds.",
            first_supported_block_id="syn_ch01_b006",
            provenance_refs=[("syn_ch01", "syn_ch01_b006")],
        ),
    ]


def _artifact(
    chapter_id: str,
    cards: list[dict],
    challenges: list[tuple[str, str, list[str], str]],
) -> dict:
    chapter = _chapter(chapter_id)
    _, manifest_hash = build_prior_packets(chapter=chapter, prior_cards=cards)
    challenge_by_id = {row[0]: row for row in challenges}
    dispositions: list[dict] = []
    tickets: list[dict] = []
    for card in sorted(cards, key=lambda row: row["prior_card_id"]):
        prior_card_id = card["prior_card_id"]
        challenge = challenge_by_id.get(prior_card_id)
        if challenge is None:
            surface = card["stable_surfaces"][0]
            supporting = next(
                block["block_id"]
                for block in chapter["blocks"]
                if surface.casefold() in block["clean_text"].casefold()
            )
            disposition = {
                "prior_card_id": prior_card_id,
                "verdict": "compatible",
                "referent_continuity": "same_referent",
                "issue_code": None,
                "disputed_field": None,
                "source_block_ids": [supporting],
                "reason": None,
            }
        else:
            _, issue_code, source_block_ids, reason = challenge
            continuity = (
                "possible_collision"
                if issue_code in {
                    "identity_collision",
                    "alias_target_conflict",
                    "alias_scope_conflict",
                }
                else "same_referent"
            )
            disposition = {
                "prior_card_id": prior_card_id,
                "verdict": "challenge",
                "referent_continuity": continuity,
                "issue_code": issue_code,
                "disputed_field": ISSUE_TO_FIELD[issue_code],
                "source_block_ids": source_block_ids,
                "reason": reason,
            }
            tickets.append(
                {
                    "prior_card_id": prior_card_id,
                    "issue_code": issue_code,
                    "disputed_field": ISSUE_TO_FIELD[issue_code],
                    "referent_continuity": continuity,
                    "source_block_ids": source_block_ids,
                    "reason": reason,
                }
            )
        dispositions.append(disposition)
    body = {
        "schema_version": EXPERIMENT_SCHEMA_VERSION,
        "chapter_id": chapter_id,
        "request_fingerprint": canonical_hash(
            {"chapter_id": chapter_id, "cards": [row["prior_card_id"] for row in cards]}
        ),
        "prior_manifest_hash": manifest_hash,
        "prior_card_dispositions": dispositions,
        "prior_conflict_tickets": tickets,
    }
    return {**body, "prior_challenge_artifact_hash": canonical_hash(body)}


def _artifacts() -> list[dict]:
    cards = {row["prior_card_id"]: row for row in _cards()}
    chapter_two_cards = [
        cards["prior_rowan"],
        cards["prior_mira"],
        cards["prior_north_gate"],
    ]
    chapter_two = _artifact(
        "syn_ch02",
        chapter_two_cards,
        [
            (
                "prior_rowan",
                "gender_conflict",
                ["syn_ch02_b001"],
                "The chapter uses feminine reference for the supplied named officer.",
            ),
            (
                "prior_mira",
                "gender_conflict",
                ["syn_ch02_b003"],
                "The supplied prior gender is not supported by this introduction.",
            ),
            (
                "prior_north_gate",
                "kind_conflict",
                ["syn_ch02_b005"],
                "The named referent is presented as a physical entrance, not an institution.",
            ),
        ],
    )
    chapter_three_rowan = _artifact(
        "syn_ch03",
        [cards["prior_rowan"]],
        [
            (
                "prior_rowan",
                "gender_conflict",
                ["syn_ch03_b001"],
                "The later chapter again uses feminine reference for the same stable name.",
            )
        ],
    )
    chapter_three_identity = _artifact(
        "syn_ch03",
        [cards["prior_mira"]],
        [
            (
                "prior_mira",
                "identity_collision",
                ["syn_ch03_b003"],
                "The stable surface may denote a different referent than the supplied card.",
            )
        ],
    )
    return [chapter_two, chapter_three_rowan, chapter_three_identity]


def _index(**overrides: object) -> dict:
    kwargs = {
        "document": _document(),
        "prior_cards": _cards(),
        "challenge_artifacts": _artifacts(),
        "registry_generation_hash": REGISTRY_GENERATION_HASH,
        "chapter_gists": {
            "syn_ch01": "An archive officer is introduced and stable places are named.",
            "syn_ch02": "Named visitors return and prior stable claims are challenged.",
            "syn_ch03": "A council appearance repeats one stable identity question.",
        },
    }
    kwargs.update(overrides)
    return build_prior_claim_ticket_index_v1(**kwargs)


def _component_for_card(index: dict, prior_card_id: str) -> dict:
    ticket_ids = {
        row["ticket_id"]
        for row in index["ticket_rows"]
        if row["prior_card_id"] == prior_card_id
        and row["route"] == "claim_auditor"
        and row["evidence_state"] == "ready"
    }
    return next(
        row
        for row in index["claim_components"]
        if ticket_ids.intersection(row["ticket_ids"])
    )


def _decision_for_component(index: dict, component: dict, revised_value: str) -> dict:
    ticket_by_id = {row["ticket_id"]: row for row in index["ticket_rows"]}
    actions = []
    for ticket_id in component["ticket_ids"]:
        ticket = ticket_by_id[ticket_id]
        prior_direct = next(
            block["block_id"]
            for block in ticket["evidence_source_blocks"]
            if "prior_direct" in block["evidence_roles"]
        )
        current = next(
            block["block_id"]
            for block in ticket["evidence_source_blocks"]
            if any(role.startswith("current_") for role in block["evidence_roles"])
        )
        actions.append(
            {
                "ticket_id": ticket_id,
                "action": "revise_claim",
                "revised_value": revised_value,
                "source_block_ids": [prior_direct, current],
                "pending_reason_code": None,
                "resolution_note": (
                    "The supplied old and current evidence support the closed revision."
                ),
            }
        )
    rendered = render_prior_claim_request_v1(
        index=index,
        component_id=component["component_id"],
        document=_document(),
        design_doc=DESIGN_DOC,
    )
    return validate_prior_claim_response_v1(
        {"component_id": component["component_id"], "ticket_actions": actions},
        index=index,
        request_fingerprint=rendered.request_fingerprint,
    )


def test_index_enriches_routes_and_exact_covers_all_tickets() -> None:
    index = verify_prior_claim_ticket_index_v1(_index(), document=_document())
    assert len(index["ticket_rows"]) == 5
    assert len(index["identity_referrals"]) == 1
    assert len(index["preflight_pending_ticket_ids"]) == 1
    assert len(index["claim_components"]) == 2
    assert index["semantic_halt_required"] is False
    ticket_ids = {row["ticket_id"] for row in index["ticket_rows"]}
    routed = {
        ticket_id
        for component in index["claim_components"]
        for ticket_id in component["ticket_ids"]
    }
    routed.update(index["preflight_pending_ticket_ids"])
    routed.update(row["ticket_id"] for row in index["identity_referrals"])
    assert routed == ticket_ids
    assert all(len(row["ticket_id"]) == len("bclaimtk1_") + 20 for row in index["ticket_rows"])
    assert all(row["challenged_prior_card_hash"] for row in index["ticket_rows"])
    assert all(row["origin_context_manifest_hash"] for row in index["ticket_rows"])


def test_empty_supplied_prior_manifest_produces_an_empty_claim_index() -> None:
    artifact = _artifact("syn_ch02", [], [])
    index = build_prior_claim_ticket_index_v1(
        document=_document(),
        prior_cards=[],
        challenge_artifacts=[artifact],
        registry_generation_hash=REGISTRY_GENERATION_HASH,
        chapter_gists=None,
    )

    assert index["ticket_rows"] == []
    assert index["claim_components"] == []
    assert index["identity_referrals"] == []
    assert index["preflight_pending_ticket_ids"] == []


def test_render_pins_bookkeeping_ids_without_pinning_semantic_actions() -> None:
    index = _index()
    component = next(row for row in index["claim_components"] if not row["overflow"])
    rendered = render_prior_claim_request_v1(
        index=index,
        component_id=component["component_id"],
        document=_document(),
        design_doc=DESIGN_DOC,
    )

    assert rendered.response_schema["properties"]["component_id"]["enum"] == [
        component["component_id"]
    ]
    action_properties = rendered.response_schema["properties"]["ticket_actions"][
        "items"
    ]["properties"]
    assert action_properties["ticket_id"]["enum"] == component["ticket_ids"]
    assert action_properties["action"]["enum"] == sorted(CLAIM_ACTIONS)
    assert "enum" not in action_properties["revised_value"]


def test_uncertainty_is_persisted_without_an_immediate_auditor_call() -> None:
    card = _cards()[0]
    chapter = _chapter("syn_ch02")
    _, manifest_hash = build_prior_packets(chapter=chapter, prior_cards=[card])
    body = {
        "schema_version": EXPERIMENT_SCHEMA_VERSION,
        "chapter_id": "syn_ch02",
        "request_fingerprint": canonical_hash({"request": "uncertain_rowan"}),
        "prior_manifest_hash": manifest_hash,
        "prior_card_dispositions": [
            {
                "prior_card_id": card["prior_card_id"],
                "verdict": "uncertain",
                "referent_continuity": "uncertain",
                "issue_code": None,
                "disputed_field": None,
                "source_block_ids": ["syn_ch02_b001"],
                "reason": "The current mention is insufficient to establish continuity.",
            }
        ],
        "prior_conflict_tickets": [],
    }
    artifact = {**body, "prior_challenge_artifact_hash": canonical_hash(body)}

    index = build_prior_claim_ticket_index_v1(
        document=_document(),
        prior_cards=[card],
        challenge_artifacts=[artifact],
        registry_generation_hash=REGISTRY_GENERATION_HASH,
        chapter_gists={"syn_ch02": "A named officer appears in an unresolved context."},
    )

    assert index["ticket_rows"] == []
    assert index["claim_components"] == []
    assert index["identity_referrals"] == []
    assert len(index["uncertainty_rows"]) == 1
    assert index["uncertainty_rows"][0]["authority_effect"] == "candidate_only"
    ledger = build_prior_claim_revision_ledger_v1(index=index, decisions=[])
    assert len(ledger["pending_identity_reviews"]) == 1
    projection = build_prior_claim_projection_v1(prior_cards=[card], ledger=ledger)
    projected = projection["projected_prior_cards"][0]
    assert projected["authority_state"] == "candidate_only"
    assert projected["claim_states"][0]["state_kind"] == "identity_uncertainty"


def test_rehashed_ledger_cannot_drop_pending_identity_reviews() -> None:
    card = _cards()[0]
    chapter = _chapter("syn_ch02")
    _, manifest_hash = build_prior_packets(chapter=chapter, prior_cards=[card])
    body = {
        "schema_version": EXPERIMENT_SCHEMA_VERSION,
        "chapter_id": "syn_ch02",
        "request_fingerprint": canonical_hash({"request": "uncertain_drop_probe"}),
        "prior_manifest_hash": manifest_hash,
        "prior_card_dispositions": [
            {
                "prior_card_id": card["prior_card_id"],
                "verdict": "uncertain",
                "referent_continuity": "uncertain",
                "issue_code": None,
                "disputed_field": None,
                "source_block_ids": ["syn_ch02_b001"],
                "reason": "The current mention is insufficient to establish continuity.",
            }
        ],
        "prior_conflict_tickets": [],
    }
    artifact = {**body, "prior_challenge_artifact_hash": canonical_hash(body)}
    index = build_prior_claim_ticket_index_v1(
        document=_document(),
        prior_cards=[card],
        challenge_artifacts=[artifact],
        registry_generation_hash=REGISTRY_GENERATION_HASH,
        chapter_gists={"syn_ch02": "A named officer appears in an unresolved context."},
    )
    ledger = build_prior_claim_revision_ledger_v1(index=index, decisions=[])
    tampered = deepcopy(ledger)
    tampered.pop("pending_identity_reviews")
    tampered_body = dict(tampered)
    tampered_body.pop("claim_ledger_hash", None)
    tampered["claim_ledger_hash"] = canonical_hash(tampered_body)

    with pytest.raises(BookEntityClaimContractError, match="pending_identity_reviews"):
        verify_prior_claim_revision_ledger_v1(tampered)


def test_rehashed_identity_uncertainty_cannot_gain_authority() -> None:
    card = _cards()[0]
    chapter = _chapter("syn_ch02")
    _, manifest_hash = build_prior_packets(chapter=chapter, prior_cards=[card])
    body = {
        "schema_version": EXPERIMENT_SCHEMA_VERSION,
        "chapter_id": "syn_ch02",
        "request_fingerprint": canonical_hash({"request": "uncertain_authority_probe"}),
        "prior_manifest_hash": manifest_hash,
        "prior_card_dispositions": [
            {
                "prior_card_id": card["prior_card_id"],
                "verdict": "uncertain",
                "referent_continuity": "uncertain",
                "issue_code": None,
                "disputed_field": None,
                "source_block_ids": ["syn_ch02_b001"],
                "reason": "The current mention is insufficient to establish continuity.",
            }
        ],
        "prior_conflict_tickets": [],
    }
    artifact = {**body, "prior_challenge_artifact_hash": canonical_hash(body)}
    index = build_prior_claim_ticket_index_v1(
        document=_document(),
        prior_cards=[card],
        challenge_artifacts=[artifact],
        registry_generation_hash=REGISTRY_GENERATION_HASH,
        chapter_gists={"syn_ch02": "A named officer appears in an unresolved context."},
    )
    ledger = build_prior_claim_revision_ledger_v1(index=index, decisions=[])
    tampered = deepcopy(ledger)
    tampered["pending_identity_reviews"][0]["authority_effect"] = "active"
    tampered_body = dict(tampered)
    tampered_body.pop("claim_ledger_hash", None)
    tampered["claim_ledger_hash"] = canonical_hash(tampered_body)

    with pytest.raises(BookEntityClaimContractError, match="unsafe authority"):
        verify_prior_claim_revision_ledger_v1(tampered)


def test_possible_collision_routes_to_identity_even_when_kind_is_disputed() -> None:
    card = _cards()[0]
    chapter = _chapter("syn_ch02")
    _, manifest_hash = build_prior_packets(chapter=chapter, prior_cards=[card])
    ticket = {
        "prior_card_id": card["prior_card_id"],
        "issue_code": "identity_collision",
        "disputed_field": "referent_kind",
        "referent_continuity": "possible_collision",
        "source_block_ids": ["syn_ch02_b001"],
        "reason": "The stable surface may identify another referent of a different kind.",
    }
    body = {
        "schema_version": EXPERIMENT_SCHEMA_VERSION,
        "chapter_id": "syn_ch02",
        "request_fingerprint": canonical_hash({"request": "kind_collision"}),
        "prior_manifest_hash": manifest_hash,
        "prior_card_dispositions": [
            {
                "prior_card_id": card["prior_card_id"],
                "verdict": "challenge",
                **{key: value for key, value in ticket.items() if key != "prior_card_id"},
            }
        ],
        "prior_conflict_tickets": [ticket],
    }
    artifact = {**body, "prior_challenge_artifact_hash": canonical_hash(body)}

    index = build_prior_claim_ticket_index_v1(
        document=_document(),
        prior_cards=[card],
        challenge_artifacts=[artifact],
        registry_generation_hash=REGISTRY_GENERATION_HASH,
        chapter_gists={"syn_ch02": "A stable surface may denote another referent."},
    )

    assert index["claim_components"] == []
    assert len(index["identity_referrals"]) == 1
    assert index["identity_referrals"][0]["disputed_field"] == "referent_kind"
    ledger = build_prior_claim_revision_ledger_v1(index=index, decisions=[])
    projection = build_prior_claim_projection_v1(prior_cards=[card], ledger=ledger)
    projected = projection["projected_prior_cards"][0]
    assert projected["authority_state"] == "candidate_only"
    assert projected["original_prior_card"]["referent_kind"] == "person"
    assert projected["disputed_claims"][0]["disputed_field"] == "identity_membership"
    assert projected["claim_states"][0]["state_kind"] == "identity_referral"
    assert projected["claim_states"][0]["source_disputed_field"] == "referent_kind"


def test_pronoun_support_gets_anchor_bridge_and_direct_roles() -> None:
    index = _index()
    rowan = next(
        row
        for row in index["ticket_rows"]
        if row["prior_card_id"] == "prior_rowan"
        and row["route"] == "claim_auditor"
        and row["issue_code"] == "gender_conflict"
        and row["current_challenge_block_ids"] == ["syn_ch02_b001"]
    )
    roles = {
        row["block_id"]: set(row["evidence_roles"])
        for row in rowan["evidence_source_blocks"]
    }
    assert "prior_anchor" in roles["syn_ch01_b001"]
    assert "prior_bridge" in roles["syn_ch01_b002"]
    assert "prior_direct" in roles["syn_ch01_b003"]
    assert "current_anchor" in roles["syn_ch02_b001"]
    assert "current_neighbor" in roles["syn_ch02_b002"]


def test_missing_prior_anchor_is_preflight_pending_and_never_rendered() -> None:
    index = _index()
    mira = next(
        row
        for row in index["ticket_rows"]
        if row["prior_card_id"] == "prior_mira"
        and row["issue_code"] == "gender_conflict"
    )
    assert mira["evidence_state"] == "insufficient"
    assert any("prior_anchor_missing" in reason for reason in mira["evidence_unresolved_reasons"])
    assert mira["ticket_id"] in index["preflight_pending_ticket_ids"]
    assert all(
        mira["ticket_id"] not in component["ticket_ids"]
        for component in index["claim_components"]
    )


def test_same_card_tickets_group_but_unrelated_same_chapter_stays_separate() -> None:
    index = _index()
    rowan_component = _component_for_card(index, "prior_rowan")
    gate_component = _component_for_card(index, "prior_north_gate")
    assert len(rowan_component["ticket_ids"]) == 2
    assert rowan_component["component_id"] != gate_component["component_id"]
    assert "syn_ch02" in rowan_component["chapter_ids"]
    assert "syn_ch02" in gate_component["chapter_ids"]
    source_ids = [row["block_id"] for row in rowan_component["source_blocks"]]
    assert len(source_ids) == len(set(source_ids))


def test_component_cap_overflow_is_explicit_and_not_truncated() -> None:
    index = _index(max_tickets_per_component=1)
    rowan = _component_for_card(index, "prior_rowan")
    assert rowan["overflow"] is True
    assert rowan["overflow_reasons"] == ["ticket_count_cap"]
    assert len(rowan["ticket_ids"]) == 2
    with pytest.raises(BookEntityClaimContractError, match="overflow"):
        render_prior_claim_request_v1(
            index=index,
            component_id=rowan["component_id"],
            document=_document(),
            design_doc=DESIGN_DOC,
        )


def test_render_is_one_component_bounded_and_excludes_hidden_oracle() -> None:
    index = _index()
    component = _component_for_card(index, "prior_rowan")
    rendered = render_prior_claim_request_v1(
        index=index,
        component_id=component["component_id"],
        document=_document(),
        design_doc=DESIGN_DOC,
    )
    payload = rendered.semantic_payload
    assert payload["component_id"] == component["component_id"]
    assert {row["prior_card_id"] for row in payload["prior_cards"]} == {"prior_rowan"}
    assert len(payload["source_blocks"]) < sum(
        len(chapter["blocks"]) for chapter in _document()["chapters"]
    )
    request_bytes = canonical_json(payload)
    oracle = ORACLE_FIXTURE.read_text(encoding="utf-8")
    assert "hidden_prior_claim_oracle_v1" not in request_bytes
    assert canonical_hash(json.loads(oracle)) not in request_bytes


def test_transport_batch_keeps_unrelated_components_independent() -> None:
    index = _index()
    rowan = _component_for_card(index, "prior_rowan")
    gate = _component_for_card(index, "prior_north_gate")
    requests = [
        asdict(
            render_prior_claim_request_v1(
                index=index,
                component_id=component["component_id"],
                document=_document(),
                design_doc=DESIGN_DOC,
            )
        )
        for component in (rowan, gate)
    ]
    batch = render_prior_claim_batch_request_v1(
        component_requests=requests,
        design_doc=DESIGN_DOC,
    )
    payload = batch.semantic_payload
    assert set(batch.component_ids) == {rowan["component_id"], gate["component_id"]}
    assert len(payload["components"]) == 2
    assert payload["shared_chapter_ids"] == ["syn_ch01", "syn_ch02"]
    assert len(payload["source_blocks"]) == len(
        {block["block_id"] for request in requests for block in request["semantic_payload"]["source_blocks"]}
    )
    assert all(len(block["component_ids"]) >= 1 for block in payload["source_blocks"])
    request_bytes = canonical_json(payload)
    assert "hidden_prior_claim_oracle_v1" not in request_bytes
    assert canonical_hash(json.loads(ORACLE_FIXTURE.read_text(encoding="utf-8"))) not in request_bytes


def test_transport_batch_response_exact_covers_each_component() -> None:
    index = _index()
    rowan = _component_for_card(index, "prior_rowan")
    gate = _component_for_card(index, "prior_north_gate")
    requests = [
        asdict(
            render_prior_claim_request_v1(
                index=index,
                component_id=component["component_id"],
                document=_document(),
                design_doc=DESIGN_DOC,
            )
        )
        for component in (rowan, gate)
    ]
    batch = render_prior_claim_batch_request_v1(
        component_requests=requests,
        design_doc=DESIGN_DOC,
    )
    rowan_actions = _decision_for_component(index, rowan, "feminine")["ticket_actions"]
    gate_actions = _decision_for_component(index, gate, "place")["ticket_actions"]
    response = {
        "batch_id": batch.batch_id,
        "component_results": [
            {"component_id": rowan["component_id"], "ticket_actions": rowan_actions},
            {"component_id": gate["component_id"], "ticket_actions": gate_actions},
        ],
    }
    decision = validate_prior_claim_batch_response_v1(
        response,
        index=index,
        request=batch,
    )
    assert len(decision["component_decisions"]) == 2
    wrong_echo = deepcopy(response)
    wrong_echo["batch_id"] = "copied_example_batch"
    normalized = validate_prior_claim_batch_response_v1(
        wrong_echo,
        index=index,
        request=batch,
    )
    assert normalized["batch_id"] == batch.batch_id
    assert normalized["response_normalization_notes"][0]["field"] == "batch_id"
    with pytest.raises(BookEntityClaimContractError, match="exact-cover"):
        validate_prior_claim_batch_response_v1(
            {"batch_id": batch.batch_id, "component_results": response["component_results"][:1]},
            index=index,
            request=batch,
        )


def test_transport_batch_respects_union_source_block_cap() -> None:
    index = _index()
    components = [
        _component_for_card(index, "prior_rowan"),
        _component_for_card(index, "prior_north_gate"),
    ]
    requests = [
        asdict(
            render_prior_claim_request_v1(
                index=index,
                component_id=component["component_id"],
                document=_document(),
                design_doc=DESIGN_DOC,
            )
        )
        for component in components
    ]
    with pytest.raises(BookEntityClaimContractError, match="source-block count"):
        render_prior_claim_batch_request_v1(
            component_requests=requests,
            design_doc=DESIGN_DOC,
            max_source_blocks=1,
        )


def test_response_revises_closed_values_and_exact_cover_is_enforced() -> None:
    index = _index()
    component = _component_for_card(index, "prior_rowan")
    decision = _decision_for_component(index, component, "feminine")
    assert all(row["revised_value"] == "feminine" for row in decision["ticket_actions"])
    assert verify_prior_claim_decision_v1(decision, index=index) == decision
    rendered = render_prior_claim_request_v1(
        index=index,
        component_id=component["component_id"],
        document=_document(),
        design_doc=DESIGN_DOC,
    )
    with pytest.raises(BookEntityClaimContractError, match="exact-cover"):
        validate_prior_claim_response_v1(
            {"component_id": component["component_id"], "ticket_actions": []},
            index=index,
            request_fingerprint=rendered.request_fingerprint,
        )


def test_foreign_value_and_foreign_evidence_are_rejected() -> None:
    index = _index()
    component = _component_for_card(index, "prior_rowan")
    ticket_id = component["ticket_ids"][0]
    rendered = render_prior_claim_request_v1(
        index=index,
        component_id=component["component_id"],
        document=_document(),
        design_doc=DESIGN_DOC,
    )
    base = {
        "component_id": component["component_id"],
        "ticket_actions": [
            {
                "ticket_id": ticket_id,
                "action": "revise_claim",
                "revised_value": "foreign_gender",
                "source_block_ids": ["syn_ch01_b003", "syn_ch02_b001"],
                "pending_reason_code": None,
                "resolution_note": "Test invalid value.",
            }
        ],
    }
    with pytest.raises(BookEntityClaimContractError, match="foreign gender"):
        validate_prior_claim_response_v1(
            base,
            index=index,
            request_fingerprint=rendered.request_fingerprint,
        )
    complete_actions = []
    for component_ticket_id in component["ticket_ids"]:
        complete_actions.append(
            {
                "ticket_id": component_ticket_id,
                "action": "pending",
                "revised_value": None,
                "source_block_ids": ["syn_ch02_b005"],
                "pending_reason_code": "insufficient_context",
                "resolution_note": "Test foreign evidence.",
            }
        )
    with pytest.raises(BookEntityClaimContractError, match="foreign blocks"):
        validate_prior_claim_response_v1(
            {"component_id": component["component_id"], "ticket_actions": complete_actions},
            index=index,
            request_fingerprint=rendered.request_fingerprint,
        )


def test_retaining_a_correct_prior_claim_is_a_valid_non_revision() -> None:
    index = _index()
    component = _component_for_card(index, "prior_north_gate")
    ticket_id = component["ticket_ids"][0]
    rendered = render_prior_claim_request_v1(
        index=index,
        component_id=component["component_id"],
        document=_document(),
        design_doc=DESIGN_DOC,
    )
    decision = validate_prior_claim_response_v1(
        {
            "component_id": component["component_id"],
            "ticket_actions": [
                {
                    "ticket_id": ticket_id,
                    "action": "retain_prior",
                    "revised_value": None,
                    "source_block_ids": ["syn_ch01_b006", "syn_ch02_b005"],
                    "pending_reason_code": None,
                    "resolution_note": "The bounded evidence supports retaining the prior claim.",
                }
            ],
        },
        index=index,
        request_fingerprint=rendered.request_fingerprint,
    )
    assert decision["ticket_actions"][0]["action"] == "retain_prior"


def test_ledger_and_projection_are_append_only_and_preserve_pending() -> None:
    index = _index()
    rowan_component = _component_for_card(index, "prior_rowan")
    gate_component = _component_for_card(index, "prior_north_gate")
    decisions = [
        _decision_for_component(index, rowan_component, "feminine"),
        _decision_for_component(index, gate_component, "place"),
    ]
    ledger = build_prior_claim_revision_ledger_v1(index=index, decisions=decisions)
    assert {row["status"] for row in ledger["claim_revision_rows"]} >= {
        "revised",
        "pending_preflight",
        "identity_referral",
    }
    active = {
        (row["prior_card_id"], row["disputed_field"]): row["projected_value"]
        for row in ledger["active_claim_projection"]
    }
    assert active[("prior_rowan", "referential_gender")] == "feminine"
    assert active[("prior_north_gate", "referent_kind")] == "place"
    assert any(row["prior_card_id"] == "prior_mira" for row in ledger["pending_claims"])
    before = deepcopy(_cards())
    projection = build_prior_claim_projection_v1(prior_cards=before, ledger=ledger)
    assert before == _cards()
    by_id = {row["prior_card_id"]: row for row in projection["projected_prior_cards"]}
    assert by_id["prior_rowan"]["effective_claims"]["referential_gender"] == "feminine"
    assert by_id["prior_north_gate"]["effective_claims"]["referent_kind"] == "place"
    assert by_id["prior_mira"]["effective_claims"]["referential_gender"] is None
    assert by_id["prior_mira"]["disputed_claims"][0]["status"] == "pending"
    rowan_history = [
        row
        for row in ledger["claim_revision_rows"]
        if row["prior_card_id"] == "prior_rowan"
    ]
    assert all(row["old_value"] == "masculine" for row in rowan_history)


def test_pending_claim_reopen_requires_new_evidence_and_has_a_finite_lifecycle() -> None:
    index = _index()
    decisions = [
        _decision_for_component(index, _component_for_card(index, "prior_rowan"), "feminine"),
        _decision_for_component(index, _component_for_card(index, "prior_north_gate"), "place"),
    ]
    ledger = build_prior_claim_revision_ledger_v1(index=index, decisions=decisions)
    pending = next(
        row for row in ledger["pending_claims"] if row["prior_card_id"] == "prior_mira"
    )
    same_evidence = pending["evidence_manifest_hashes"][0]
    blocked = classify_pending_claim_reopen_v1(
        pending_claim=pending,
        evidence_manifest_hash=same_evidence,
        trigger="new_evidence",
    )
    assert blocked["allowed"] is False
    assert blocked["route"] == "blocked_same_evidence"

    fresh_hash = canonical_hash({"evidence": "later chapter"})
    automatic = classify_pending_claim_reopen_v1(
        pending_claim=pending,
        evidence_manifest_hash=fresh_hash,
        trigger="expanded_evidence",
    )
    assert automatic["allowed"] is True
    assert automatic["route"] == "automatic_hearing"

    exhausted = {**pending, "hearing_count": 2}
    deferred = classify_pending_claim_reopen_v1(
        pending_claim=exhausted,
        evidence_manifest_hash=fresh_hash,
        trigger="new_evidence",
    )
    assert deferred["allowed"] is False
    assert deferred["route"] == "defer_book_end_or_human"
    assert classify_pending_claim_reopen_v1(
        pending_claim=exhausted,
        evidence_manifest_hash=fresh_hash,
        trigger="book_end",
    )["route"] == "book_end_hearing"
    assert classify_pending_claim_reopen_v1(
        pending_claim=exhausted,
        evidence_manifest_hash=fresh_hash,
        trigger="human",
    )["route"] == "human_hearing"


def test_input_order_does_not_change_index_identity() -> None:
    first = _index()
    second = build_prior_claim_ticket_index_v1(
        document=_document(),
        prior_cards=list(reversed(_cards())),
        challenge_artifacts=list(reversed(_artifacts())),
        registry_generation_hash=REGISTRY_GENERATION_HASH,
        chapter_gists={
            "syn_ch03": "A council appearance repeats one stable identity question.",
            "syn_ch02": "Named visitors return and prior stable claims are challenged.",
            "syn_ch01": "An archive officer is introduced and stable places are named.",
        },
    )
    assert first["ticket_index_hash"] == second["ticket_index_hash"]
    assert canonical_json(first) == canonical_json(second)


def test_tampered_artifact_registry_hash_and_foreign_block_fail_closed() -> None:
    bad_artifact = deepcopy(_artifacts()[0])
    bad_artifact["prior_conflict_tickets"][0]["reason"] = "tampered"
    with pytest.raises(BookEntityClaimContractError, match="artifact hash mismatch"):
        build_prior_claim_ticket_index_v1(
            document=_document(),
            prior_cards=_cards(),
            challenge_artifacts=[bad_artifact],
            registry_generation_hash=REGISTRY_GENERATION_HASH,
        )
    with pytest.raises(BookEntityClaimContractError, match="SHA-256"):
        _index(registry_generation_hash="not-a-hash")
    foreign = deepcopy(_artifacts()[0])
    foreign["prior_card_dispositions"][0]["source_block_ids"] = ["foreign_b001"]
    foreign["prior_conflict_tickets"][0]["source_block_ids"] = ["foreign_b001"]
    body = dict(foreign)
    body.pop("prior_challenge_artifact_hash")
    foreign["prior_challenge_artifact_hash"] = canonical_hash(body)
    with pytest.raises(BookEntityClaimContractError, match="foreign current"):
        build_prior_claim_ticket_index_v1(
            document=_document(),
            prior_cards=_cards(),
            challenge_artifacts=[foreign],
            registry_generation_hash=REGISTRY_GENERATION_HASH,
        )


def test_dry_render_reports_caps_without_api_or_semantic_halt() -> None:
    report = dry_render_prior_claim_requests_v1(
        index=_index(),
        document=_document(),
        design_doc=DESIGN_DOC,
    )
    assert report["rendered_component_count"] == 2
    assert report["preflight_pending_ticket_count"] == 1
    assert report["identity_referral_count"] == 1
    assert report["estimated_total_tokens"] > 0
    assert report["token_estimator"] == (
        "ceil((message_utf8_bytes+response_schema_utf8_bytes)/4)"
    )


def test_identity_summary_can_only_be_invalidated_in_sidecar_projection() -> None:
    rowan = next(row for row in _cards() if row["prior_card_id"] == "prior_rowan")
    artifact = _artifact(
        "syn_ch02",
        [rowan],
        [
            (
                "prior_rowan",
                "unsupported_stable_claim",
                ["syn_ch02_b001"],
                "The supplied stable summary contains an unsupported role claim.",
            )
        ],
    )
    index = build_prior_claim_ticket_index_v1(
        document=_document(),
        prior_cards=[rowan],
        challenge_artifacts=[artifact],
        registry_generation_hash=REGISTRY_GENERATION_HASH,
    )
    component = index["claim_components"][0]
    ticket = index["ticket_rows"][0]
    rendered = render_prior_claim_request_v1(
        index=index,
        component_id=component["component_id"],
        document=_document(),
        design_doc=DESIGN_DOC,
    )
    evidence = ["syn_ch01_b003", "syn_ch02_b001"]
    decision = validate_prior_claim_response_v1(
        {
            "component_id": component["component_id"],
            "ticket_actions": [
                {
                    "ticket_id": ticket["ticket_id"],
                    "action": "revise_claim",
                    "revised_value": None,
                    "source_block_ids": evidence,
                    "pending_reason_code": None,
                    "resolution_note": "The supplied summary is not source-supported.",
                }
            ],
        },
        index=index,
        request_fingerprint=rendered.request_fingerprint,
    )
    ledger = build_prior_claim_revision_ledger_v1(index=index, decisions=[decision])
    projection = build_prior_claim_projection_v1(prior_cards=[rowan], ledger=ledger)
    assert projection["projected_prior_cards"][0]["effective_claims"]["identity_summary"] is None
    assert rowan["identity_summary"]
    bad = deepcopy(decision)
    bad["ticket_actions"][0]["revised_value"] = "A model-authored replacement."
    with pytest.raises(BookEntityClaimContractError, match="cannot author"):
        validate_prior_claim_response_v1(
            {
                "component_id": component["component_id"],
                "ticket_actions": bad["ticket_actions"],
            },
            index=index,
            request_fingerprint=rendered.request_fingerprint,
        )


def test_offline_runner_is_resumable_and_waiting_responses_is_nonfatal(
    tmp_path: Path,
) -> None:
    document_path = tmp_path / "document.json"
    cards_path = tmp_path / "cards.json"
    gists_path = tmp_path / "gists.json"
    artifact_paths: list[Path] = []
    document_path.write_text(json.dumps(_document()), encoding="utf-8")
    cards_path.write_text(json.dumps({"prior_cards": _cards()}), encoding="utf-8")
    gists_path.write_text(
        json.dumps(
            {
                "syn_ch01": "An archive officer is introduced.",
                "syn_ch02": "Prior stable claims are challenged.",
                "syn_ch03": "A later appearance repeats one question.",
            }
        ),
        encoding="utf-8",
    )
    for index, artifact in enumerate(_artifacts()):
        path = tmp_path / f"artifact_{index}.json"
        path.write_text(json.dumps(artifact), encoding="utf-8")
        artifact_paths.append(path)
    output_dir = tmp_path / "out"
    script = RUNTIME_ROOT / "pipeline" / "scripts" / "run_cross_chapter_claim_auditor_v1.py"
    command = [
        sys.executable,
        str(script),
        "--document",
        str(document_path),
        "--prior-cards",
        str(cards_path),
        "--registry-generation-hash",
        REGISTRY_GENERATION_HASH,
        "--chapter-gists",
        str(gists_path),
        "--output-dir",
        str(output_dir),
    ]
    for path in artifact_paths:
        command.extend(["--challenge-artifact", str(path)])
    first = subprocess.run(command, cwd=RUNTIME_ROOT, check=False, capture_output=True, text=True)
    assert first.returncode == 0, first.stderr
    second = subprocess.run(command, cwd=RUNTIME_ROOT, check=False, capture_output=True, text=True)
    assert second.returncode == 0, second.stderr
    run_plan = json.loads((output_dir / "run_plan.json").read_text(encoding="utf-8"))
    assert run_plan["semantic_halt_required"] is False
    assert run_plan["all_renderable_components_validated"] is False
    assert any(row["status"] == "awaiting_response" for row in run_plan["components"])
    assert not (output_dir / "claim_revision_ledger.json").exists()
