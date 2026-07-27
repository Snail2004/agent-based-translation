"""Offline fold of verified B3 review overlays into chapter memory."""

from __future__ import annotations

from copy import deepcopy
from typing import Any, Mapping, Sequence

from pipeline.literary.b3_temporal_auditor_v1 import (
    B3TemporalAuditorError,
    verify_b3_temporal_review_overlay_v1,
)
from pipeline.literary.b3_parked_identity_v1 import (
    B3ParkedIdentityError,
    verify_parked_identity_index_v1,
)
from pipeline.literary.b3_parked_identity_v2 import (
    PARKED_IDENTITY_INDEX_SCHEMA_VERSION_V2,
    verify_parked_identity_index_v2,
)
from pipeline.literary.b3_temporal_prefix_v1 import (
    B3_TEMPORAL_CHAPTER_ARTIFACT_SCHEMA_VERSION_V1,
)
from pipeline.literary.b1_cross_chapter_decision_ledger_v1 import (
    PROJECTION_SCHEMA_VERSION,
)
from pipeline.literary.checkpoint import canonical_hash, canonical_json


LEDGER_SCHEMA_VERSION_V1 = "literary_b3_temporal_decision_ledger_v1"
APPLY_REPORT_SCHEMA_VERSION_V1 = "literary_b3_temporal_apply_report_v1"
FRAME_CATALOG_SCHEMA_VERSION_V1 = "literary_b3_narrative_frame_catalog_v1"
_IDENTITY_ADAPTER_LIFECYCLE_STATE = "awaiting_identity_adapter"
_IDENTITY_ADAPTER_PARKED_REASON = (
    "no implemented consumer; see B.3 amendment"
)
_ORIGIN_UNKNOWN_BLOCKER = "onset_not_stated_in_source"
_IDENTITY_NOT_CONFIRMED_REASON = "referent_identity_not_confirmed"
_STABLE_CLAIM_REVIEW_REASON = "stable_claim_domain_requires_review"


class B3TemporalDecisionLedgerError(RuntimeError):
    pass


def fold_b3_temporal_review_overlays_v1(
    *,
    chapter_artifact: Mapping[str, Any],
    overlay_packets: Sequence[tuple[Mapping[str, Any], Mapping[str, Any]]] = (),
    reconciled_identity_projection: Mapping[str, Any] | None = None,
    frame_catalog: Mapping[str, Any] | None = None,
    identity_component_catalogs: Sequence[Mapping[str, Any]] = (),
    consolidation_only: bool = False,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    """Verify and fold disjoint per-case overlays without re-adjudication."""

    base = _verified_chapter_artifact(chapter_artifact)
    if (
        not overlay_packets
        and reconciled_identity_projection is None
        and not consolidation_only
    ):
        raise B3TemporalDecisionLedgerError(
            "at least one B3 review overlay or identity projection is required"
        )

    effective_pending, superseded_identity_rows, identity_projection_hash = (
        _retire_superseded_inherited_identity_cases_v1(
            pending_cases=base.get("pending_cases") or [],
            parked_identity_index=base.get("parked_identity_index"),
            reconciled_identity_projection=reconciled_identity_projection,
        )
        if reconciled_identity_projection is not None
        else (deepcopy(list(base.get("pending_cases") or [])), [], None)
    )

    lifecycle_pending, resolved_cases, identity_lifecycle = (
        apply_b3_identity_review_lifecycle_v1(
            pending_cases=effective_pending,
            resolved_cases=base.get("resolved_cases") or [],
            component_catalogs=identity_component_catalogs,
            close_chapter_id=base["chapter_id"],
        )
    )
    pending_by_id = _keyed_rows(
        lifecycle_pending,
        key="pending_case_id",
        label="B3 pending case",
    )
    effective_by_id = _keyed_rows(
        base.get("effective_state_projection") or [],
        key="state_id",
        label="B3 effective state",
    )
    observation_by_id = _keyed_rows(
        base.get("confirmed_observation_rows") or [],
        key="observation_id",
        label="B3 confirmed observation",
    )

    source_hash = base["artifact_hash"]
    seen_case_ids: set[str] = set()
    resolved_ids: set[str] = set()
    retained_ids: set[str] = set()
    referred_ids: set[str] = set(base.get("referred_identity_case_ids") or [])
    entries: list[dict[str, Any]] = []

    for ordinal, (raw_overlay, raw_packet) in enumerate(overlay_packets, 1):
        try:
            overlay = verify_b3_temporal_review_overlay_v1(
                raw_overlay, packet=raw_packet
            )
        except B3TemporalAuditorError as exc:
            raise B3TemporalDecisionLedgerError(
                f"overlay {ordinal} failed B3 verification: {exc}"
            ) from exc

        if overlay.get("source_b3_artifact_hash") != source_hash:
            raise B3TemporalDecisionLedgerError(
                "G1: B3 review overlay belongs to another base artifact"
            )
        if overlay.get("chapter_id") != base.get("chapter_id"):
            raise B3TemporalDecisionLedgerError(
                "B3 review overlay belongs to another chapter"
            )
        if overlay.get("identity_mutation_performed") is not False:
            raise B3TemporalDecisionLedgerError(
                "G6: B3 review overlay claims identity mutation"
            )

        resolved = _id_set(
            overlay.get("resolved_pending_case_ids"), "resolved pending case"
        )
        retained = _id_set(
            overlay.get("retained_pending_case_ids"), "retained pending case"
        )
        referred = _id_set(
            overlay.get("identity_referral_case_ids"), "identity referral case"
        )
        if resolved & retained:
            raise B3TemporalDecisionLedgerError(
                "G5: one B3 case is both resolved and retained"
            )
        if not referred <= retained:
            raise B3TemporalDecisionLedgerError(
                "identity referral must remain in the retained pending set"
            )
        owned = resolved | retained
        overlap = seen_case_ids & owned
        if overlap:
            raise B3TemporalDecisionLedgerError(
                "G3: B3 review overlays repeat a pending case: "
                + ", ".join(sorted(overlap))
            )
        absent = (owned | referred) - set(pending_by_id)
        if absent:
            raise B3TemporalDecisionLedgerError(
                "G4: B3 review overlay cites a foreign pending case: "
                + ", ".join(sorted(absent))
            )

        for state in overlay.get("confirmed_state_rows") or []:
            _insert_exact_row(
                effective_by_id,
                state,
                key="state_id",
                label="auditor-confirmed B3 state",
            )
        for observation in overlay.get("confirmed_observation_rows") or []:
            _insert_exact_row(
                observation_by_id,
                observation,
                key="observation_id",
                label="auditor-confirmed B3 observation",
            )

        seen_case_ids.update(owned)
        resolved_ids.update(resolved)
        retained_ids.update(retained)
        referred_ids.update(referred)
        entries.append(
            {
                "overlay_hash": overlay["overlay_hash"],
                "packet_hash": overlay["packet_hash"],
                "source_b3_artifact_hash": overlay["source_b3_artifact_hash"],
                "source_b3_tree_hash": overlay["source_b3_tree_hash"],
                "resolved_pending_case_ids": sorted(resolved),
                "retained_pending_case_ids": sorted(retained),
                "identity_referral_case_ids": sorted(referred),
                "confirmed_state_ids": sorted(
                    row["state_id"] for row in overlay.get("confirmed_state_rows") or []
                ),
                "confirmed_observation_ids": sorted(
                    row["observation_id"]
                    for row in overlay.get("confirmed_observation_rows") or []
                ),
            }
        )

    normalized_frame_catalog = merge_b3_narrative_frame_catalogs_v1(
        [value for value in (base.get("frame_catalog"), frame_catalog) if value]
    )
    effective_states, consolidation, consolidation_issues = (
        _consolidate_effective_state_projection_v1(
            list(effective_by_id.values()),
            frame_catalog=normalized_frame_catalog,
        )
    )
    reconciled_body = deepcopy(dict(base))
    reconciled_body.pop("artifact_hash", None)
    reconciled_body["effective_state_projection"] = effective_states
    reconciled_body["pending_cases"] = [
        pending_by_id[key]
        for key in sorted(pending_by_id)
        if key not in resolved_ids
    ]
    reconciled_body["resolved_cases"] = resolved_cases
    if reconciled_identity_projection is not None:
        historical_pending = _keyed_rows(
            base.get("superseded_pending_cases") or [],
            key="pending_case_id",
            label="superseded B3 pending case",
        )
        for row in superseded_identity_rows:
            _insert_exact_row(
                historical_pending,
                row,
                key="pending_case_id",
                label="superseded B3 pending case",
            )
        reconciled_body["superseded_pending_cases"] = [
            historical_pending[key] for key in sorted(historical_pending)
        ]
    reconciled_body["confirmed_observation_rows"] = [
        observation_by_id[key] for key in sorted(observation_by_id)
    ]
    reconciled_body["frame_catalog"] = normalized_frame_catalog
    reconciled_body["referred_identity_case_ids"] = sorted(referred_ids)
    reconciled_body["identity_mutation_performed"] = False
    if consolidation_issues:
        existing_issues = list(reconciled_body.get("review_issues") or [])
        existing_issue_hashes = {canonical_hash(row) for row in existing_issues}
        reconciled_body["review_issues"] = existing_issues + [
            row
            for row in consolidation_issues
            if canonical_hash(row) not in existing_issue_hashes
        ]
    reconciled = {
        **reconciled_body,
        "artifact_hash": canonical_hash(reconciled_body),
    }

    entries.sort(key=lambda row: row["overlay_hash"])
    ledger_body = {
        "schema_version": LEDGER_SCHEMA_VERSION_V1,
        "chapter_id": base["chapter_id"],
        "source_b3_artifact_hash": source_hash,
        "reconciled_b3_artifact_hash": reconciled["artifact_hash"],
        "entries": entries,
        "resolved_pending_case_ids": sorted(resolved_ids),
        "retained_pending_case_ids": sorted(retained_ids),
        "referred_identity_case_ids": sorted(referred_ids),
        "state_consolidation": consolidation,
        "identity_review_lifecycle": identity_lifecycle,
        "identity_mutation_performed": False,
        "provider_calls": 0,
    }
    if identity_projection_hash is not None:
        ledger_body.update(
            {
                "source_identity_projection_hash": identity_projection_hash,
                "superseded_inherited_identity_case_ids": sorted(
                    row["pending_case_id"] for row in superseded_identity_rows
                ),
            }
        )
    ledger = {**ledger_body, "ledger_hash": canonical_hash(ledger_body)}

    report_body = {
        "schema_version": APPLY_REPORT_SCHEMA_VERSION_V1,
        "status": "applied",
        "chapter_id": base["chapter_id"],
        "source_b3_artifact_hash": source_hash,
        "reconciled_b3_artifact_hash": reconciled["artifact_hash"],
        "ledger_hash": ledger["ledger_hash"],
        "overlay_count": len(entries),
        "before": {
            "effective_states": len(base.get("effective_state_projection") or []),
            "pending_cases": len(base.get("pending_cases") or []),
            "resolved_cases": len(base.get("resolved_cases") or []),
        },
        "after": {
            "effective_states": len(reconciled["effective_state_projection"]),
            "pending_cases": len(reconciled["pending_cases"]),
            "resolved_cases": len(reconciled["resolved_cases"]),
        },
        "confirmed_states_added": (
            len(effective_by_id) - len(base.get("effective_state_projection") or [])
        ),
        "confirmed_observations_added": len(
            [
                key
                for key in observation_by_id
                if key
                not in {
                    row.get("observation_id")
                    for row in base.get("confirmed_observation_rows") or []
                }
            ]
        ),
        "resolved_pending_case_ids": sorted(resolved_ids),
        "retained_pending_case_ids": sorted(retained_ids),
        "referred_identity_case_ids": sorted(referred_ids),
        "identity_review_lifecycle": identity_lifecycle,
        "provider_calls": 0,
        "identity_mutation_performed": False,
        "production_publish_performed": False,
    }
    if identity_projection_hash is not None:
        report_body.update(
            {
                "source_identity_projection_hash": identity_projection_hash,
                "superseded_inherited_identity_case_ids": sorted(
                    row["pending_case_id"] for row in superseded_identity_rows
                ),
            }
        )
    report = {**report_body, "report_hash": canonical_hash(report_body)}
    return reconciled, ledger, report


def apply_b3_identity_review_lifecycle_v1(
    *,
    pending_cases: Sequence[Mapping[str, Any]],
    resolved_cases: Sequence[Mapping[str, Any]],
    component_catalogs: Sequence[Mapping[str, Any]],
    close_chapter_id: str,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    """Classify typed identity holds, close onset-only cases, and route the rest."""

    close_chapter = _required_text(close_chapter_id, "identity close chapter_id")
    components = _identity_component_index_v1(component_catalogs)
    resolved_by_id = _keyed_rows(
        [_resolved_case_v1(row) for row in resolved_cases],
        key="pending_case_id",
        label="resolved B3 case",
    )
    pending_by_id = _keyed_rows(
        pending_cases,
        key="pending_case_id",
        label="B3 pending case",
    )
    overlap = set(resolved_by_id) & set(pending_by_id)
    if overlap:
        raise B3TemporalDecisionLedgerError(
            "B3 case is both pending and resolved: "
            + ", ".join(sorted(overlap))
        )

    retained: list[dict[str, Any]] = []
    classifications: list[dict[str, Any]] = []
    for case_id in sorted(pending_by_id):
        case = deepcopy(pending_by_id[case_id])
        if case.get("review_route") != "identity_review":
            retained.append(case)
            continue

        lifecycle_state = case.get("lifecycle_state")
        if lifecycle_state is not None:
            if (
                lifecycle_state != _IDENTITY_ADAPTER_LIFECYCLE_STATE
                or case.get("parked_reason") != _IDENTITY_ADAPTER_PARKED_REASON
            ):
                raise B3TemporalDecisionLedgerError(
                    "identity review case has an unsupported lifecycle marker"
                )
            retained.append(case)
            classifications.append(
                {
                    "pending_case_id": case_id,
                    "component_id": _required_text(
                        case.get("component_id"), "identity component_id"
                    ),
                    "classification": "identity_blocked",
                    "candidate_count": None,
                    "subject_resolved": False,
                    "outcome": "already_parked",
                }
            )
            continue

        component_id = _required_text(
            case.get("component_id"), "identity component_id"
        )
        component = components.get(component_id)
        if component is None:
            raise B3TemporalDecisionLedgerError(
                "identity review case lacks its typed component evidence: "
                + component_id
            )
        classification = _classify_identity_review_case_v1(case, component)
        report_row = {
            "pending_case_id": case_id,
            "component_id": component_id,
            **classification,
        }
        if classification["classification"] == "onset_unknown":
            resolved = deepcopy(case)
            resolved.update(
                {
                    "disposition": "origin_unknown",
                    "authority_status": "resolved_terminal",
                    "unknowable_window": {
                        "from_chapter": _required_text(
                            case.get("chapter_id"), "identity case chapter_id"
                        ),
                        "to_chapter": close_chapter,
                        "blocker": _ORIGIN_UNKNOWN_BLOCKER,
                    },
                }
            )
            resolved = _resolved_case_v1(resolved)
            _insert_exact_row(
                resolved_by_id,
                resolved,
                key="pending_case_id",
                label="resolved B3 case",
                allow_exact=False,
            )
            report_row["outcome"] = "resolved_terminal"
        elif classification["classification"] == "identity_blocked":
            case.update(
                {
                    "lifecycle_state": _IDENTITY_ADAPTER_LIFECYCLE_STATE,
                    "parked_reason": _IDENTITY_ADAPTER_PARKED_REASON,
                }
            )
            retained.append(case)
            report_row["outcome"] = "parked_pending_adapter"
        else:
            review_route = _resolved_identity_review_route_v1(case)
            case["review_route"] = review_route
            retained.append(case)
            report_row["outcome"] = "rerouted"
            report_row["rerouted_review_route"] = review_route
        classifications.append(report_row)

    classification_counts = {
        name: sum(
            row["classification"] == name for row in classifications
        )
        for name in ("identity_blocked", "onset_unknown", "neither")
    }
    lifecycle_report = {
        "classification_counts": classification_counts,
        "classifications": classifications,
        "closed_origin_unknown_case_ids": sorted(
            row["pending_case_id"]
            for row in classifications
            if row["outcome"] == "resolved_terminal"
        ),
        "parked_identity_case_ids": sorted(
            row["pending_case_id"]
            for row in classifications
            if row["outcome"] in {
                "parked_pending_adapter",
                "already_parked",
            }
        ),
        "unchanged_identity_case_ids": sorted(
            row["pending_case_id"]
            for row in classifications
            if row["outcome"] == "unchanged"
        ),
        "rerouted_temporal_case_ids": sorted(
            row["pending_case_id"]
            for row in classifications
            if row.get("rerouted_review_route") == "temporal_review"
        ),
        "rerouted_stable_claim_case_ids": sorted(
            row["pending_case_id"]
            for row in classifications
            if row.get("rerouted_review_route") == "stable_claim_review"
        ),
    }
    return (
        sorted(retained, key=lambda row: row["pending_case_id"]),
        [resolved_by_id[key] for key in sorted(resolved_by_id)],
        lifecycle_report,
    )


def _resolved_identity_review_route_v1(case: Mapping[str, Any]) -> str:
    reason_codes = case.get("reason_codes")
    if not isinstance(reason_codes, list) or not reason_codes or not all(
        isinstance(value, str) and value for value in reason_codes
    ):
        raise B3TemporalDecisionLedgerError(
            "resolved identity review case has malformed reason codes"
        )
    reasons = set(reason_codes)
    if reasons == {_IDENTITY_NOT_CONFIRMED_REASON}:
        return "temporal_review"
    if reasons == {
        _IDENTITY_NOT_CONFIRMED_REASON,
        _STABLE_CLAIM_REVIEW_REASON,
    }:
        return "stable_claim_review"
    raise B3TemporalDecisionLedgerError(
        "resolved identity review case has no specified typed route: "
        + ", ".join(sorted(reasons))
    )


def _classify_identity_review_case_v1(
    case: Mapping[str, Any],
    component: Mapping[str, Any],
) -> dict[str, Any]:
    cards = _mapping_rows(component.get("candidate_cards"), "B3 candidate cards")
    card_ids: set[str] = set()
    cards_by_ref: dict[str, set[str]] = {}
    for card in cards:
        card_id = _required_text(card.get("candidate_card_id"), "candidate_card_id")
        referent_ref = _required_text(card.get("referent_ref"), "candidate referent_ref")
        card_ids.add(card_id)
        cards_by_ref.setdefault(referent_ref, set()).add(card_id)

    action = case.get("proposed_action")
    subject_refs: list[str] = []
    operation = None
    if isinstance(action, Mapping):
        operation = action.get("operation")
        raw_subjects = action.get("subject_referent_refs")
        if isinstance(raw_subjects, list) and all(
            isinstance(value, str) and value for value in raw_subjects
        ):
            subject_refs = list(dict.fromkeys(raw_subjects))

    candidate_ids: set[str] = set()
    subject_resolved = bool(subject_refs)
    if subject_refs:
        for referent_ref in subject_refs:
            matches = cards_by_ref.get(referent_ref, set())
            if len(matches) != 1:
                subject_resolved = False
            candidate_ids.update(matches)
    else:
        candidate_ids.update(
            _typed_ambiguous_candidate_ids_v1(
                component,
                cards_by_ref=cards_by_ref,
                known_card_ids=card_ids,
            )
        )
        subject_resolved = False

    if not subject_resolved or len(candidate_ids) > 1:
        classification = "identity_blocked"
    elif len(candidate_ids) == 1 and operation == "reveal_only":
        classification = "onset_unknown"
    else:
        classification = "neither"
    return {
        "classification": classification,
        "candidate_count": len(candidate_ids),
        "subject_resolved": subject_resolved,
    }


def _typed_ambiguous_candidate_ids_v1(
    component: Mapping[str, Any],
    *,
    cards_by_ref: Mapping[str, set[str]],
    known_card_ids: set[str],
) -> set[str]:
    candidate_ids: set[str] = set()
    for review in _mapping_rows(
        component.get("b2_review_requests") or [],
        "B3 component review requests",
    ):
        if review.get("review_kind") != "event_participant":
            continue
        raw_ids = review.get("candidate_card_ids")
        if isinstance(raw_ids, list):
            candidate_ids.update(
                value
                for value in raw_ids
                if isinstance(value, str) and value in known_card_ids
            )

    for turn in _mapping_rows(
        component.get("speaker_turns") or [],
        "B3 component speaker turns",
    ):
        for endpoint_name in ("speaker", "addressee"):
            endpoint = turn.get(endpoint_name)
            if not isinstance(endpoint, Mapping) or endpoint.get(
                "resolution_status"
            ) != "ambiguous_candidates":
                continue
            for referent_ref in endpoint.get("referent_refs") or []:
                if isinstance(referent_ref, str):
                    candidate_ids.update(cards_by_ref.get(referent_ref, set()))
    return candidate_ids or set(known_card_ids)


def _identity_component_index_v1(
    catalogs: Sequence[Mapping[str, Any]],
) -> dict[str, dict[str, Any]]:
    components: dict[str, dict[str, Any]] = {}
    for catalog in catalogs:
        if not isinstance(catalog, Mapping):
            raise B3TemporalDecisionLedgerError(
                "B3 component catalog must be an object"
            )
        if catalog.get("schema_version") not in {
            "literary_b3_temporal_component_catalog_v1",
            "literary_b3_temporal_component_catalog_v2",
        }:
            raise B3TemporalDecisionLedgerError(
                "foreign B3 temporal component catalog"
            )
        observed_hash = catalog.get("catalog_hash")
        if observed_hash is not None:
            unsigned = dict(catalog)
            unsigned.pop("catalog_hash", None)
            if observed_hash != canonical_hash(unsigned):
                raise B3TemporalDecisionLedgerError(
                    "B3 temporal component catalog hash mismatch"
                )
        for raw_component in _mapping_rows(
            catalog.get("components"), "B3 temporal components"
        ):
            component_id = _required_text(
                raw_component.get("component_id"), "B3 component_id"
            )
            observed_component_hash = raw_component.get("component_hash")
            if observed_component_hash is not None:
                unsigned_component = dict(raw_component)
                unsigned_component.pop("component_hash", None)
                unsigned_component.pop("component_id", None)
                unsigned_component.pop("component_ordinal", None)
                if observed_component_hash != canonical_hash(unsigned_component):
                    raise B3TemporalDecisionLedgerError(
                        "B3 temporal component hash mismatch"
                    )
            _insert_exact_row(
                components,
                raw_component,
                key="component_id",
                label="B3 temporal component",
            )
    return components


def _resolved_case_v1(value: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise B3TemporalDecisionLedgerError("resolved B3 case must be an object")
    row = deepcopy(dict(value))
    _required_text(row.get("pending_case_id"), "resolved pending_case_id")
    _required_text(row.get("chapter_id"), "resolved case chapter_id")
    if row.get("authority_status") != "resolved_terminal":
        raise B3TemporalDecisionLedgerError(
            "resolved B3 case lacks terminal authority status"
        )
    if row.get("disposition") != "origin_unknown":
        raise B3TemporalDecisionLedgerError(
            "resolved B3 case has an unsupported disposition"
        )
    window = row.get("unknowable_window")
    if not isinstance(window, Mapping):
        raise B3TemporalDecisionLedgerError(
            "origin_unknown case lacks an unknowable_window"
        )
    _required_text(window.get("from_chapter"), "unknowable from_chapter")
    _required_text(window.get("to_chapter"), "unknowable to_chapter")
    if window.get("blocker") != _ORIGIN_UNKNOWN_BLOCKER:
        raise B3TemporalDecisionLedgerError(
            "origin_unknown case has a foreign unknowable blocker"
        )
    return row


def merge_b3_narrative_frame_catalogs_v1(
    catalogs: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Project one cumulative, content-addressed narrative-layer catalog."""

    layers: dict[str, tuple[str, str]] = {}
    for catalog in catalogs:
        if not isinstance(catalog, Mapping):
            raise B3TemporalDecisionLedgerError(
                "B3 frame catalog input must be an object"
            )
        if catalog.get("schema_version") == FRAME_CATALOG_SCHEMA_VERSION_V1:
            observed_hash = catalog.get("catalog_hash")
            unsigned = dict(catalog)
            unsigned.pop("catalog_hash", None)
            if observed_hash != canonical_hash(unsigned):
                raise B3TemporalDecisionLedgerError(
                    "B3 narrative frame catalog hash mismatch"
                )
        for frame_id, layer in _narrative_layer_index_v1(catalog).items():
            previous = layers.get(frame_id)
            if previous is not None and previous != layer:
                raise B3TemporalDecisionLedgerError(
                    "frame segment has conflicting narrative layers"
                )
            layers[frame_id] = layer
    body = {
        "schema_version": FRAME_CATALOG_SCHEMA_VERSION_V1,
        "frame_segments": [
            {
                "frame_segment_id": frame_id,
                "narrative_mode": layer[0],
                "narrator_surface": layer[1],
            }
            for frame_id, layer in sorted(layers.items())
        ],
    }
    return {**body, "catalog_hash": canonical_hash(body)}


def _consolidate_effective_state_projection_v1(
    states: Sequence[Mapping[str, Any]],
    *,
    frame_catalog: Mapping[str, Any] | None,
) -> tuple[list[dict[str, Any]], dict[str, Any], list[dict[str, Any]]]:
    """Fold duplicate state observations without crossing narrative layers."""

    layers = _narrative_layer_index_v1(frame_catalog)
    standalone: list[dict[str, Any]] = []
    groups: dict[tuple[str, tuple[str, ...], tuple[str, str]], list[dict[str, Any]]] = {}
    unresolved_issues: list[dict[str, Any]] = []
    semantic_subject_groups: dict[
        tuple[str, tuple[str, ...]], list[tuple[tuple[str, str], dict[str, Any]]]
    ] = {}

    for raw_state in states:
        state = deepcopy(dict(raw_state))
        state_id = _required_text(state.get("state_id"), "state id")
        semantic_key = state.get("semantic_key")
        subject_refs = tuple(sorted(str(value) for value in state.get("subject_referent_refs") or []))
        layer, issue = _state_narrative_layer_v1(state, layers)
        if issue is not None:
            standalone.append(state)
            unresolved_issues.append(
                {
                    "row_type": "state_spans_multiple_narrative_layers",
                    "state_id": state_id,
                    **issue,
                }
            )
            continue
        if not isinstance(semantic_key, str) or not semantic_key:
            standalone.append(state)
            continue
        group_key = (semantic_key, subject_refs, layer)
        groups.setdefault(group_key, []).append(state)

    consolidated: list[dict[str, Any]] = []
    duplicate_group_count = 0
    absorbed_ids: list[str] = []
    for group_key, members in groups.items():
        if len(members) == 1:
            consolidated.append(members[0])
            continue
        duplicate_group_count += 1
        ordered = sorted(
            members,
            key=lambda row: _required_text(row.get("state_id"), "state id"),
        )
        survivor = deepcopy(ordered[0])
        member_ids = [
            _required_text(row.get("state_id"), "state id") for row in ordered
        ]
        observations = [
            {
                "state_value": deepcopy(row.get("state_value")),
                "source_block_ids": deepcopy(list(row.get("source_block_ids") or [])),
                "frame_segment_ids": deepcopy(list(row.get("frame_segment_ids") or [])),
                "observed_at_block_id": deepcopy(row.get("observed_at_block_id")),
                "opened_by_observation_id": deepcopy(
                    row.get("opened_by_observation_id")
                ),
                "source_pending_case_id": deepcopy(
                    row.get("source_pending_case_id")
                ),
            }
            for row in ordered
        ]
        observations.sort(
            key=lambda row: (
                str(row.get("observed_at_block_id") or ""),
                str(row.get("opened_by_observation_id") or ""),
            )
        )
        survivor["observations"] = observations
        survivor["consolidated_state_ids"] = sorted(member_ids[1:])
        survivor["observation_count"] = len(observations)
        survivor["source_block_ids"] = sorted(
            {
                block_id
                for row in ordered
                for block_id in row.get("source_block_ids") or []
            }
        )
        valid_from = [
            row.get("valid_from_block_id")
            for row in ordered
            if row.get("valid_from_block_id") is not None
        ]
        valid_to = [
            row.get("valid_to_block_id")
            for row in ordered
            if row.get("valid_to_block_id") is not None
        ]
        survivor["valid_from_block_id"] = min(valid_from) if valid_from else None
        survivor["valid_to_block_id"] = (
            None
            if any(row.get("valid_to_block_id") is None for row in ordered)
            else max(valid_to)
        )
        absorbed_ids.extend(member_ids[1:])
        consolidated.append(survivor)

    consolidated.extend(standalone)
    consolidated.sort(key=lambda row: _required_text(row.get("state_id"), "state id"))

    # Link equal semantic claims that survived in different narrative layers.
    for row in consolidated:
        semantic_key = row.get("semantic_key")
        subject_refs = tuple(sorted(str(value) for value in row.get("subject_referent_refs") or []))
        layer, issue = _state_narrative_layer_v1(row, layers)
        if (
            issue is not None
            or not isinstance(semantic_key, str)
            or not semantic_key
        ):
            continue
        semantic_subject_groups.setdefault((semantic_key, subject_refs), []).append(
            (layer, row)
        )
    for rows in semantic_subject_groups.values():
        distinct_layers = {layer for layer, _row in rows}
        if len(distinct_layers) < 2:
            continue
        for layer, row in rows:
            peers = [
                (peer, peer_layer)
                for peer_layer, peer in rows
                if peer_layer != layer
            ]
            row["corroborating_state_ids"] = sorted(
                {_required_text(peer.get("state_id"), "state id") for peer, _ in peers}
            )
            row["corroboration_layers"] = [
                {
                    "state_id": _required_text(peer.get("state_id"), "state id"),
                    "narrative_mode": peer_layer[0],
                    "narrator_surface": peer_layer[1],
                }
                for peer, peer_layer in sorted(
                    peers,
                    key=lambda item: _required_text(item[0].get("state_id"), "state id"),
                )
            ]

    before_count = len(states)
    after_count = len(consolidated)
    report = {
        "before_count": before_count,
        "after_count": after_count,
        "duplicate_group_count": duplicate_group_count,
        "absorbed_state_ids": sorted(absorbed_ids),
        "review_issue_count": len(unresolved_issues),
    }
    return consolidated, report, unresolved_issues


def _narrative_layer_index_v1(
    frame_catalog: Mapping[str, Any] | None,
) -> dict[str, tuple[str, str]]:
    if not isinstance(frame_catalog, Mapping):
        return {}
    segments: list[Mapping[str, Any]] = []
    direct = frame_catalog.get("frame_segments")
    if isinstance(direct, list):
        segments.extend(row for row in direct if isinstance(row, Mapping))
    for component in frame_catalog.get("components") or []:
        if not isinstance(component, Mapping):
            continue
        segments.extend(
            row
            for row in component.get("frame_segments") or []
            if isinstance(row, Mapping)
        )
    result: dict[str, tuple[str, str]] = {}
    for segment in segments:
        frame_id = _required_text(segment.get("frame_segment_id"), "frame segment id")
        layer = (
            _required_text(segment.get("narrative_mode"), "narrative mode"),
            _required_text(segment.get("narrator_surface"), "narrator surface"),
        )
        previous = result.get(frame_id)
        if previous is not None and previous != layer:
            raise B3TemporalDecisionLedgerError(
                "frame segment has conflicting narrative layers"
            )
        result[frame_id] = layer
    return result


def _state_narrative_layer_v1(
    state: Mapping[str, Any],
    layers: Mapping[str, tuple[str, str]],
) -> tuple[tuple[str, str] | None, dict[str, Any] | None]:
    frame_ids = [
        _required_text(value, "state frame segment id")
        for value in state.get("frame_segment_ids") or []
    ]
    if not frame_ids:
        return None, {
            "reason_code": "state_frame_layer_unresolved",
            "frame_segment_ids": [],
        }
    resolved = {layers[frame_id] for frame_id in frame_ids if frame_id in layers}
    missing = sorted(set(frame_ids) - set(layers))
    if missing or len(resolved) != 1:
        return None, {
            "reason_code": "state_frame_layer_unresolved"
            if missing
            else "state_spans_multiple_narrative_layers",
            "frame_segment_ids": frame_ids,
            "unresolved_frame_segment_ids": missing,
            "resolved_layers": [
                {
                    "narrative_mode": layer[0],
                    "narrator_surface": layer[1],
                }
                for layer in sorted(resolved)
            ],
        }
    return next(iter(resolved)), None


def verify_b3_temporal_decision_ledger_v1(
    ledger: Mapping[str, Any],
) -> dict[str, Any]:
    row = deepcopy(dict(ledger))
    observed = row.pop("ledger_hash", None)
    if observed != canonical_hash(row):
        raise B3TemporalDecisionLedgerError("B3 temporal decision ledger hash mismatch")
    if ledger.get("schema_version") != LEDGER_SCHEMA_VERSION_V1:
        raise B3TemporalDecisionLedgerError("foreign B3 temporal decision ledger")
    if ledger.get("provider_calls") != 0 or ledger.get(
        "identity_mutation_performed"
    ) is not False:
        raise B3TemporalDecisionLedgerError("B3 temporal decision ledger claims effects")
    return deepcopy(dict(ledger))


def _verified_chapter_artifact(value: Mapping[str, Any]) -> dict[str, Any]:
    row = deepcopy(dict(value))
    observed = row.pop("artifact_hash", None)
    if observed != canonical_hash(row):
        raise B3TemporalDecisionLedgerError("B3 chapter artifact hash mismatch")
    if value.get("schema_version") != B3_TEMPORAL_CHAPTER_ARTIFACT_SCHEMA_VERSION_V1:
        raise B3TemporalDecisionLedgerError("foreign B3 chapter artifact")
    if value.get("identity_mutation_performed") is not False:
        raise B3TemporalDecisionLedgerError("B3 chapter artifact claims identity mutation")
    if value.get("production_publish_performed") is not False:
        raise B3TemporalDecisionLedgerError("B3 chapter artifact claims publication")
    return deepcopy(dict(value))


def _retire_superseded_inherited_identity_cases_v1(
    *,
    pending_cases: Sequence[Mapping[str, Any]],
    parked_identity_index: Any,
    reconciled_identity_projection: Mapping[str, Any],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], str]:
    """Retire inherited holds only when their hearing was explicitly superseded."""

    projection = _verified_identity_projection(reconciled_identity_projection)
    superseded_components = {
        _required_text(row.get("component_id"), "superseded component_id")
        for row in _mapping_rows(
            projection.get("superseded_pending_cases"),
            "superseded identity pending cases",
        )
    }
    if not superseded_components:
        return deepcopy(list(pending_cases)), [], projection["projection_hash"]
    if not isinstance(parked_identity_index, Mapping):
        raise B3TemporalDecisionLedgerError(
            "B3 artifact lacks the current parked identity index"
        )
    try:
        if (
            parked_identity_index.get("schema_version")
            == PARKED_IDENTITY_INDEX_SCHEMA_VERSION_V2
        ):
            parked = verify_parked_identity_index_v2(parked_identity_index)
        else:
            parked = verify_parked_identity_index_v1(parked_identity_index)
    except B3ParkedIdentityError as exc:
        raise B3TemporalDecisionLedgerError(
            "B3 parked identity index failed verification"
        ) from exc
    active_components = {
        _required_text(row.get("hearing_component_id"), "hearing_component_id")
        for row in _mapping_rows(
            parked.get("parked_identities"), "parked identities"
        )
    }

    effective: list[dict[str, Any]] = []
    retired: list[dict[str, Any]] = []
    for raw in pending_cases:
        if not isinstance(raw, Mapping):
            raise B3TemporalDecisionLedgerError("B3 pending case must be an object")
        row = deepcopy(dict(raw))
        if row.get("review_route") != "inherited_identity_block":
            effective.append(row)
            continue
        plural_inherited = row.get("inherited_parked_identities")
        singular_inherited = row.get("inherited_parked_identity")
        if plural_inherited is not None and singular_inherited is not None:
            raise B3TemporalDecisionLedgerError(
                "inherited identity pending case carries both marker shapes"
            )
        inherited_values = plural_inherited
        if inherited_values is None:
            inherited_values = (
                [] if singular_inherited is None else [singular_inherited]
            )
        if (
            not isinstance(inherited_values, list)
            or not inherited_values
            or not all(isinstance(value, Mapping) for value in inherited_values)
        ):
            raise B3TemporalDecisionLedgerError(
                "inherited identity pending case lacks its hearing marker"
            )
        component_ids = {
            _required_text(
                inherited.get("hearing_component_id"),
                "inherited hearing_component_id",
            )
            for inherited in inherited_values
        }
        retiring = component_ids.intersection(superseded_components) - active_components
        if not retiring:
            effective.append(row)
            continue
        remaining = component_ids - retiring
        if remaining:
            effective_row = deepcopy(row)
            effective_row["inherited_parked_identities"] = [
                deepcopy(dict(value))
                for value in inherited_values
                if value.get("hearing_component_id") in remaining
            ]
            effective.append(effective_row)
        superseded_key = (
            "superseded_hearing_component_ids"
            if plural_inherited is not None
            else "superseded_hearing_component_id"
        )
        superseded_value: Any = (
            sorted(retiring)
            if plural_inherited is not None
            else next(iter(retiring))
        )
        row.update(
            {
                superseded_key: superseded_value,
                "superseded_by_identity_projection_hash": projection[
                    "projection_hash"
                ],
                "superseded_reason": (
                    "identity hearing was superseded and is absent from the "
                    "current parked identity index"
                ),
            }
        )
        retired.append(row)
    return (
        sorted(effective, key=lambda row: row["pending_case_id"]),
        sorted(retired, key=lambda row: row["pending_case_id"]),
        projection["projection_hash"],
    )


def _verified_identity_projection(value: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise B3TemporalDecisionLedgerError(
            "reconciled identity projection must be an object"
        )
    row = deepcopy(dict(value))
    observed = row.pop("projection_hash", None)
    if observed != canonical_hash(row):
        raise B3TemporalDecisionLedgerError(
            "reconciled identity projection hash mismatch"
        )
    if value.get("schema_version") != PROJECTION_SCHEMA_VERSION:
        raise B3TemporalDecisionLedgerError(
            "foreign reconciled identity projection"
        )
    if value.get("identity_authority_granted") is not False:
        raise B3TemporalDecisionLedgerError(
            "reconciled identity projection grants identity authority"
        )
    return deepcopy(dict(value))


def _mapping_rows(value: Any, label: str) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        raise B3TemporalDecisionLedgerError(f"{label} must be a list")
    if not all(isinstance(row, Mapping) for row in value):
        raise B3TemporalDecisionLedgerError(f"{label} must contain objects")
    return [deepcopy(dict(row)) for row in value]


def _required_text(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise B3TemporalDecisionLedgerError(f"{label} must be a non-empty string")
    return value.strip()


def _keyed_rows(
    values: Sequence[Mapping[str, Any]], *, key: str, label: str
) -> dict[str, dict[str, Any]]:
    rows: dict[str, dict[str, Any]] = {}
    for value in values:
        if not isinstance(value, Mapping):
            raise B3TemporalDecisionLedgerError(f"{label} must be an object")
        _insert_exact_row(rows, value, key=key, label=label, allow_exact=False)
    return rows


def _insert_exact_row(
    rows: dict[str, dict[str, Any]],
    value: Mapping[str, Any],
    *,
    key: str,
    label: str,
    allow_exact: bool = True,
) -> None:
    row = deepcopy(dict(value))
    row_id = row.get(key)
    if not isinstance(row_id, str) or not row_id:
        raise B3TemporalDecisionLedgerError(f"{label} lacks {key}")
    current = rows.get(row_id)
    if current is None:
        rows[row_id] = row
        return
    if allow_exact and canonical_json(current) == canonical_json(row):
        return
    raise B3TemporalDecisionLedgerError(f"{label} repeats {key}: {row_id}")


def _id_set(value: Any, label: str) -> set[str]:
    if not isinstance(value, list):
        raise B3TemporalDecisionLedgerError(f"{label} ids must be a list")
    rows: set[str] = set()
    for item in value:
        if not isinstance(item, str) or not item or item in rows:
            raise B3TemporalDecisionLedgerError(f"{label} ids are malformed")
        rows.add(item)
    return rows


__all__ = [
    "APPLY_REPORT_SCHEMA_VERSION_V1",
    "B3TemporalDecisionLedgerError",
    "FRAME_CATALOG_SCHEMA_VERSION_V1",
    "LEDGER_SCHEMA_VERSION_V1",
    "apply_b3_identity_review_lifecycle_v1",
    "_retire_superseded_inherited_identity_cases_v1",
    "fold_b3_temporal_review_overlays_v1",
    "merge_b3_narrative_frame_catalogs_v1",
    "verify_b3_temporal_decision_ledger_v1",
]
