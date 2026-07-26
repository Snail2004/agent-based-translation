from __future__ import annotations

import copy
import re
from typing import Any, Mapping, Sequence

from pipeline.eval.contracts_v1 import (
    CanonicalPolicy,
    ContractValidationError,
    canonical_sha256,
    canonicalize,
    require_commit,
    require_enum,
    require_exact_keys,
    require_int,
    require_list,
    require_mapping,
    require_nullable_string,
    require_rfc3339,
    require_sha256,
    require_string,
    require_unique,
    seal_payload,
    validate_producer,
    verify_payload_hash,
)
from pipeline.eval.workflow_component_v1 import (
    FLOW_KIND,
    validate_evaluation_component_event_v1,
    validate_evaluation_component_manifest_v1,
    validate_typed_artifact_binding_v1,
)


__all__ = [
    "CONSOLE_PROJECTION_ARTIFACT_KIND",
    "CONSOLE_PROJECTION_SCHEMA_ID",
    "CONSOLE_PROJECTION_SCHEMA_VERSION",
    "build_evaluation_console_projection_v1",
    "console_projection_artifact_ref_v1",
    "normalize_console_diagnostic_bindings_v1",
    "normalize_console_recovery_journal_v1",
    "validate_evaluation_console_projection_chain_v1",
    "validate_evaluation_console_projection_v1",
]


CONSOLE_PROJECTION_SCHEMA_ID = "EvaluationConsoleProjectionV1"
CONSOLE_PROJECTION_SCHEMA_VERSION = "1.0.0"
CONSOLE_PROJECTION_ARTIFACT_KIND = "evaluation_console_projection_v1"
CONSOLE_PROJECTION_COMPONENT = "workflow_console_projection_v1"

_PROJECTION_HASH_PATH = ("integrity", "projection_sha256")
_ROW_HASH_PATH = ("integrity", "row_sha256")
_ZERO_HASH = "0" * 64
_ROW_ID_RE = re.compile(r"^evalconsole_row_[0-9a-f]{32}$")
_ABSOLUTE_WINDOWS_PATH_RE = re.compile(r"^[A-Za-z]:[\\/]")

_PROJECTION_POLICY = CanonicalPolicy(
    set_like_paths=frozenset(
        {
            ("rows", "*", "detail", "retry", "physical_attempt_indexes"),
            ("rows", "*", "detail", "retry", "reason_codes"),
            ("state", "paused_incident_ids"),
            ("state", "open_retry_groups", "*", "physical_attempt_indexes"),
            ("state", "open_retry_groups", "*", "reason_codes"),
        }
    ),
    semantic_sequence_paths=frozenset(
        {
            ("rows",),
            ("rows", "*", "source_event_ids"),
            ("state", "open_retry_groups"),
            ("state", "open_retry_groups", "*", "source_event_ids"),
        }
    ),
)
_ROW_POLICY = CanonicalPolicy(
    set_like_paths=frozenset(
        {
            ("detail", "retry", "physical_attempt_indexes"),
            ("detail", "retry", "reason_codes"),
        }
    ),
    semantic_sequence_paths=frozenset({("source_event_ids",)}),
)
_PREFIX_POLICY = CanonicalPolicy(
    set_like_paths=frozenset(),
    semantic_sequence_paths=frozenset({("records",)}),
)

_PROJECTED_EVENT_TYPES = frozenset(
    {
        "component_started",
        "component_resumed",
        "stage_start",
        "progress",
        "validation_passed",
        "validation_failed",
        "retry_summary",
        "checkpoint",
        "usage_snapshot",
        "stage_done",
        "stage_paused",
        "component_done",
        "component_failed",
    }
)
_SEVERITIES = frozenset({"info", "warning", "error"})
_RETRY_OUTCOMES = frozenset(
    {
        "stage_succeeded",
        "stage_skipped",
        "stage_failed",
        "stage_blocked",
        "component_succeeded",
        "component_halted",
        "component_failed",
    }
)
_LABEL_KEYS = {
    "component_started": "evaluation.component_started",
    "component_resumed": "evaluation.component_resumed",
    "stage_start": "evaluation.stage_started",
    "progress": "evaluation.progress",
    "validation_passed": "evaluation.validation_passed",
    "validation_failed": "evaluation.validation_failed",
    "retry_summary": "evaluation.retry_summary",
    "checkpoint": "evaluation.checkpoint",
    "usage_snapshot": "evaluation.usage_snapshot",
    "stage_done": "evaluation.stage_done",
    "stage_paused": "evaluation.stage_paused",
    "component_done": "evaluation.component_done",
    "component_failed": "evaluation.component_failed",
}
_FORBIDDEN_PUBLIC_TEXT = (
    "api_key=",
    "authorization:",
    "bearer ",
    "password=",
    "secret=",
    "traceback (most recent call last)",
    "file://",
)


def normalize_console_recovery_journal_v1(
    records: Sequence[Mapping[str, Any]],
) -> tuple[dict[str, Any], ...]:
    """Reduce an already validated recovery journal to public prefix facts."""

    normalized: list[dict[str, Any]] = []
    for index, value in enumerate(records, start=1):
        path = f"$.recovery_journal[{index - 1}]"
        row = require_mapping(value, path=path)
        sequence = require_int(row.get("sequence"), path=f"{path}.sequence", minimum=1)
        if sequence != index:
            raise ContractValidationError(
                "console_recovery_sequence",
                f"{path}.sequence",
                "recovery prefix must be contiguous from one",
            )
        integrity = require_mapping(row.get("integrity"), path=f"{path}.integrity")
        normalized.append(
            {
                "sequence": sequence,
                "journal_sha256": require_sha256(
                    integrity.get("journal_sha256"),
                    path=f"{path}.integrity.journal_sha256",
                ),
            }
        )
    return tuple(normalized)


def normalize_console_diagnostic_bindings_v1(
    bindings: Sequence[Mapping[str, Any]],
) -> tuple[dict[str, str], ...]:
    normalized: list[dict[str, str]] = []
    for index, value in enumerate(bindings):
        path = f"$.diagnostic_bindings[{index}]"
        row = require_mapping(value, path=path)
        require_exact_keys(row, required={"incident_id", "sha256"}, path=path)
        normalized.append(
            {
                "incident_id": require_string(
                    row["incident_id"], path=f"{path}.incident_id"
                ),
                "sha256": require_sha256(row["sha256"], path=f"{path}.sha256"),
            }
        )
    require_unique(
        [row["incident_id"] for row in normalized],
        path="$.diagnostic_bindings[*].incident_id",
    )
    return tuple(normalized)


def build_evaluation_console_projection_v1(
    manifest: Mapping[str, Any],
    event: Mapping[str, Any],
    *,
    previous_projection: Mapping[str, Any] | None,
    recovery_journal: Sequence[Mapping[str, Any]],
    diagnostic_bindings: Sequence[Mapping[str, Any]],
    producer_code_commit: str,
) -> dict[str, Any]:
    accepted_manifest = validate_evaluation_component_manifest_v1(manifest)
    accepted_event = validate_evaluation_component_event_v1(event)
    previous = (
        None
        if previous_projection is None
        else validate_evaluation_console_projection_v1(previous_projection)
    )
    for field in ("workflow_run_id", "component_run_id"):
        if accepted_event[field] != accepted_manifest[field]:
            raise ContractValidationError(
                "console_component_binding",
                f"$.event.{field}",
                "event belongs to another Evaluation component",
            )
    expected_sequence = 1 if previous is None else previous["through_component_seq"] + 1
    if accepted_event["component_seq"] != expected_sequence:
        raise ContractValidationError(
            "console_component_sequence",
            "$.event.component_seq",
            "projection must advance exactly one component event",
        )

    journal = normalize_console_recovery_journal_v1(recovery_journal)
    diagnostics = normalize_console_diagnostic_bindings_v1(diagnostic_bindings)
    _require_prefix_extension(previous, journal=journal, diagnostics=diagnostics)

    prior_state = (
        {"open_retry_groups": [], "paused_incident_ids": []}
        if previous is None
        else copy.deepcopy(previous["state"])
    )
    state, new_rows = _advance_console_state(prior_state, accepted_event)
    previous_row_count = 0 if previous is None else previous["cumulative"]["row_count"]
    row_chain_sha256 = (
        _ZERO_HASH
        if previous is None
        else previous["cumulative"]["row_chain_sha256"]
    )
    for row in new_rows:
        row_chain_sha256 = canonical_sha256(
            {
                "previous_row_chain_sha256": row_chain_sha256,
                "row_sha256": row["integrity"]["row_sha256"],
            },
            policy=_PREFIX_POLICY,
        )

    previous_event_prefix_sha256 = (
        _ZERO_HASH
        if previous is None
        else previous["prefixes"]["component_events"]["prefix_sha256"]
    )
    event_prefix_sha256 = canonical_sha256(
        {
            "previous_event_prefix_sha256": previous_event_prefix_sha256,
            "component_seq": accepted_event["component_seq"],
            "event_sha256": accepted_event["integrity"]["event_sha256"],
        },
        policy=_PREFIX_POLICY,
    )
    draft = {
        "schema_id": CONSOLE_PROJECTION_SCHEMA_ID,
        "schema_version": CONSOLE_PROJECTION_SCHEMA_VERSION,
        "workflow_run_id": accepted_event["workflow_run_id"],
        "flow_kind": FLOW_KIND,
        "component_id": "evaluation",
        "component_run_id": accepted_event["component_run_id"],
        "projection_index": accepted_event["component_seq"],
        "through_component_seq": accepted_event["component_seq"],
        "through_component_event_id": accepted_event["event_id"],
        "component_attempt_id": accepted_event["component_attempt_id"],
        "component_attempt_index": accepted_event["component_attempt_index"],
        "created_at": accepted_event["ts"],
        "prefixes": {
            "component_events": {
                "record_count": accepted_event["component_seq"],
                "through_sha256": accepted_event["integrity"]["event_sha256"],
                "prefix_sha256": event_prefix_sha256,
            },
            "recovery_journal": _prefix_record(
                "evaluation_recovery_journal_v1",
                [row["journal_sha256"] for row in journal],
            ),
            "diagnostics": _prefix_record(
                "evaluation_internal_incidents_v1",
                [row["sha256"] for row in diagnostics],
            ),
        },
        "previous_projection_sha256": (
            None if previous is None else previous["integrity"]["projection_sha256"]
        ),
        "rows": new_rows,
        "state": state,
        "cumulative": {
            "row_count": previous_row_count + len(new_rows),
            "row_chain_sha256": row_chain_sha256,
        },
        "producer": {
            "workstream": "evaluation",
            "component": CONSOLE_PROJECTION_COMPONENT,
            "component_version": CONSOLE_PROJECTION_SCHEMA_VERSION,
            "code_commit": require_commit(
                producer_code_commit, path="$.producer_code_commit"
            ),
        },
        "integrity": {"projection_sha256": _ZERO_HASH},
    }
    return validate_evaluation_console_projection_v1(
        seal_payload(
            draft,
            policy=_PROJECTION_POLICY,
            hash_path=_PROJECTION_HASH_PATH,
        )
    )


def validate_evaluation_console_projection_v1(
    value: Mapping[str, Any],
) -> dict[str, Any]:
    row = require_mapping(value, path="$projection")
    require_exact_keys(
        row,
        required={
            "schema_id",
            "schema_version",
            "workflow_run_id",
            "flow_kind",
            "component_id",
            "component_run_id",
            "projection_index",
            "through_component_seq",
            "through_component_event_id",
            "component_attempt_id",
            "component_attempt_index",
            "created_at",
            "prefixes",
            "previous_projection_sha256",
            "rows",
            "state",
            "cumulative",
            "producer",
            "integrity",
        },
        path="$projection",
    )
    projection_index = require_int(
        row["projection_index"], path="$projection.projection_index", minimum=1
    )
    through_component_seq = require_int(
        row["through_component_seq"],
        path="$projection.through_component_seq",
        minimum=1,
    )
    if projection_index != through_component_seq:
        raise ContractValidationError(
            "console_projection_sequence",
            "$projection.projection_index",
            "v1 requires exactly one projection record per component event",
        )
    rows = [
        _validate_console_row(item, path=f"$projection.rows[{index}]")
        for index, item in enumerate(
            require_list(row["rows"], path="$projection.rows")
        )
    ]
    require_unique(
        [item["row_id"] for item in rows], path="$projection.rows[*].row_id"
    )
    for index, projected_row in enumerate(rows):
        if projected_row["source_component_seq_end"] > through_component_seq:
            raise ContractValidationError(
                "console_future_prefix",
                f"$projection.rows[{index}].source_component_seq_end",
                "console row references a future component event",
            )
    prefixes = _validate_prefixes(row["prefixes"])
    if prefixes["component_events"]["record_count"] != through_component_seq:
        raise ContractValidationError(
            "console_event_prefix",
            "$projection.prefixes.component_events.record_count",
            "event prefix count differs from through_component_seq",
        )
    state = _validate_console_state(row["state"], path="$projection.state")
    cumulative = _validate_cumulative(row["cumulative"], path="$projection.cumulative")
    if cumulative["row_count"] < len(rows):
        raise ContractValidationError(
            "console_row_count",
            "$projection.cumulative.row_count",
            "cumulative row count is smaller than this projection delta",
        )
    integrity = require_mapping(row["integrity"], path="$projection.integrity")
    require_exact_keys(
        integrity, required={"projection_sha256"}, path="$projection.integrity"
    )
    normalized = {
        "schema_id": require_enum(
            row["schema_id"],
            {CONSOLE_PROJECTION_SCHEMA_ID},
            path="$projection.schema_id",
        ),
        "schema_version": require_enum(
            row["schema_version"],
            {CONSOLE_PROJECTION_SCHEMA_VERSION},
            path="$projection.schema_version",
        ),
        "workflow_run_id": require_string(
            row["workflow_run_id"], path="$projection.workflow_run_id"
        ),
        "flow_kind": require_enum(
            row["flow_kind"], {FLOW_KIND}, path="$projection.flow_kind"
        ),
        "component_id": require_enum(
            row["component_id"], {"evaluation"}, path="$projection.component_id"
        ),
        "component_run_id": require_string(
            row["component_run_id"], path="$projection.component_run_id"
        ),
        "projection_index": projection_index,
        "through_component_seq": through_component_seq,
        "through_component_event_id": require_string(
            row["through_component_event_id"],
            path="$projection.through_component_event_id",
        ),
        "component_attempt_id": require_string(
            row["component_attempt_id"], path="$projection.component_attempt_id"
        ),
        "component_attempt_index": require_int(
            row["component_attempt_index"],
            path="$projection.component_attempt_index",
            minimum=1,
        ),
        "created_at": require_rfc3339(
            row["created_at"], path="$projection.created_at"
        ),
        "prefixes": prefixes,
        "previous_projection_sha256": (
            None
            if row["previous_projection_sha256"] is None
            else require_sha256(
                row["previous_projection_sha256"],
                path="$projection.previous_projection_sha256",
            )
        ),
        "rows": rows,
        "state": state,
        "cumulative": cumulative,
        "producer": validate_producer(
            row["producer"], path="$projection.producer", workstream="evaluation"
        ),
        "integrity": {
            "projection_sha256": require_sha256(
                integrity["projection_sha256"],
                path="$projection.integrity.projection_sha256",
            )
        },
    }
    if normalized["producer"]["component"] != CONSOLE_PROJECTION_COMPONENT:
        raise ContractValidationError(
            "console_producer_authority",
            "$projection.producer.component",
            "only the Evaluation Console projection producer may seal this artifact",
        )
    _assert_public_safe(normalized, path="$projection")
    if not verify_payload_hash(
        normalized,
        policy=_PROJECTION_POLICY,
        hash_path=_PROJECTION_HASH_PATH,
    ):
        raise ContractValidationError(
            "console_projection_hash",
            "$projection.integrity.projection_sha256",
            "projection hash drift",
        )
    result = canonicalize(normalized, policy=_PROJECTION_POLICY)
    assert isinstance(result, dict)
    return result


def validate_evaluation_console_projection_chain_v1(
    projections: Sequence[Mapping[str, Any]],
    *,
    manifest: Mapping[str, Any],
    events: Sequence[Mapping[str, Any]],
    recovery_journal: Sequence[Mapping[str, Any]],
    diagnostic_bindings: Sequence[Mapping[str, Any]],
) -> tuple[dict[str, Any], ...]:
    accepted_manifest = validate_evaluation_component_manifest_v1(manifest)
    accepted_events = [
        validate_evaluation_component_event_v1(event) for event in events
    ]
    # Keep the sealed journal records available for deterministic rederivation.
    # The normalized prefix is intentionally only a public projection summary;
    # feeding it back into ``build_*`` would discard the nested integrity field.
    raw_journal = tuple(copy.deepcopy(record) for record in recovery_journal)
    journal = normalize_console_recovery_journal_v1(raw_journal)
    diagnostics = normalize_console_diagnostic_bindings_v1(diagnostic_bindings)
    if len(projections) != len(accepted_events):
        raise ContractValidationError(
            "console_projection_exact_cover",
            "$projections",
            "projection chain must contain exactly one record per component event",
        )
    normalized: list[dict[str, Any]] = []
    previous: dict[str, Any] | None = None
    previous_journal_count = 0
    previous_diagnostic_count = 0
    for index, (raw_projection, event) in enumerate(
        zip(projections, accepted_events, strict=True)
    ):
        path = f"$projections[{index}]"
        projection = validate_evaluation_console_projection_v1(raw_projection)
        for field in ("workflow_run_id", "component_run_id"):
            if projection[field] != accepted_manifest[field]:
                raise ContractValidationError(
                    "console_component_binding",
                    f"{path}.{field}",
                    "projection belongs to another Evaluation component",
                )
        journal_count = projection["prefixes"]["recovery_journal"]["record_count"]
        diagnostic_count = projection["prefixes"]["diagnostics"]["record_count"]
        if (
            journal_count < previous_journal_count
            or diagnostic_count < previous_diagnostic_count
        ):
            raise ContractValidationError(
                "console_prefix_regression",
                path,
                "recovery and diagnostic prefixes must be monotonic",
            )
        if journal_count > len(journal) or diagnostic_count > len(diagnostics):
            raise ContractValidationError(
                "console_future_prefix",
                path,
                "projection binds a recovery or diagnostic record not yet present",
            )
        expected = build_evaluation_console_projection_v1(
            accepted_manifest,
            event,
            previous_projection=previous,
            recovery_journal=raw_journal[:journal_count],
            diagnostic_bindings=diagnostics[:diagnostic_count],
            producer_code_commit=projection["producer"]["code_commit"],
        )
        if projection != expected:
            raise ContractValidationError(
                "console_projection_rederivation",
                path,
                "projection differs from deterministic source rederivation",
            )
        normalized.append(projection)
        previous = projection
        previous_journal_count = journal_count
        previous_diagnostic_count = diagnostic_count
    return tuple(copy.deepcopy(normalized))


def console_projection_artifact_ref_v1(
    projection: Mapping[str, Any],
) -> str:
    accepted = validate_evaluation_console_projection_v1(projection)
    return (
        "console_projections/"
        f"{accepted['projection_index']:08d}_"
        f"{accepted['integrity']['projection_sha256']}.json"
    )


def _advance_console_state(
    prior_state: Mapping[str, Any], event: Mapping[str, Any]
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    state = _validate_console_state(prior_state, path="$.prior_state")
    event_type = event["event"]
    rows: list[dict[str, Any]] = []
    if event_type == "retry":
        _record_retry(state, event)
        return state, rows
    if event_type == "stage_done":
        rows.extend(
            _flush_retry_groups(
                state,
                closing_event=event,
                stage_id=event["stage_id"],
            )
        )
    elif event_type in {"component_halted", "component_done", "component_failed"}:
        rows.extend(_flush_retry_groups(state, closing_event=event, stage_id=None))

    if event_type == "component_halted":
        incident_key = event["payload"].get("incident_id") or event["event_id"]
        if incident_key not in state["paused_incident_ids"]:
            rows.append(_build_pause_row(event))
            state["paused_incident_ids"].append(incident_key)
            state["paused_incident_ids"].sort()
    elif event_type != "retry":
        rows.append(_build_passthrough_row(event))
    return _validate_console_state(state, path="$.state"), rows


def _record_retry(state: dict[str, Any], event: Mapping[str, Any]) -> None:
    payload = event["payload"]
    key = (
        event["stage_id"],
        payload["retry_kind"],
        payload["logical_request_id"],
    )
    group = next(
        (
            item
            for item in state["open_retry_groups"]
            if (
                item["stage_id"],
                item["retry_kind"],
                item["logical_request_id"],
            )
            == key
        ),
        None,
    )
    if group is None:
        group = {
            "stage_id": event["stage_id"],
            "agent": event["agent"],
            "retry_kind": payload["retry_kind"],
            "logical_request_id": payload["logical_request_id"],
            "retry_count": 0,
            "physical_attempt_indexes": [],
            "reason_codes": [],
            "first_component_seq": event["component_seq"],
            "last_component_seq": event["component_seq"],
            "source_event_ids": [],
        }
        state["open_retry_groups"].append(group)
    group["retry_count"] += 1
    group["last_component_seq"] = event["component_seq"]
    group["source_event_ids"].append(event["event_id"])
    physical = payload["physical_attempt_index"]
    if physical is not None and physical not in group["physical_attempt_indexes"]:
        group["physical_attempt_indexes"].append(physical)
        group["physical_attempt_indexes"].sort()
    reason_code = payload["reason_code"]
    if reason_code not in group["reason_codes"]:
        group["reason_codes"].append(reason_code)
        group["reason_codes"].sort()


def _flush_retry_groups(
    state: dict[str, Any],
    *,
    closing_event: Mapping[str, Any],
    stage_id: str | None,
) -> list[dict[str, Any]]:
    selected = [
        group
        for group in state["open_retry_groups"]
        if stage_id is None or group["stage_id"] == stage_id
    ]
    state["open_retry_groups"] = [
        group
        for group in state["open_retry_groups"]
        if stage_id is not None and group["stage_id"] != stage_id
    ]
    outcome = _retry_outcome(closing_event)
    return [
        _build_console_row(
            event="retry_summary",
            severity="warning",
            label_key=_LABEL_KEYS["retry_summary"],
            source_event_ids=[*group["source_event_ids"], closing_event["event_id"]],
            source_component_seq_start=group["first_component_seq"],
            source_component_seq_end=closing_event["component_seq"],
            component_attempt_id=closing_event["component_attempt_id"],
            component_attempt_index=closing_event["component_attempt_index"],
            ts=closing_event["ts"],
            stage_id=group["stage_id"],
            agent=group["agent"],
            detail=_empty_detail(
                retry={
                    "retry_kind": group["retry_kind"],
                    "logical_request_id": group["logical_request_id"],
                    "retry_count": group["retry_count"],
                    "physical_attempt_indexes": group[
                        "physical_attempt_indexes"
                    ],
                    "reason_codes": group["reason_codes"],
                    "outcome": outcome,
                }
            ),
        )
        for group in selected
    ]


def _retry_outcome(closing_event: Mapping[str, Any]) -> str:
    event_type = closing_event["event"]
    if event_type == "stage_done":
        return f"stage_{closing_event['payload']['outcome']}"
    if event_type == "component_done":
        return "component_succeeded"
    if event_type == "component_halted":
        return "component_halted"
    if event_type == "component_failed":
        return "component_failed"
    raise ContractValidationError(
        "console_retry_close",
        "$.closing_event.event",
        "retry group closed by an unsupported event",
    )


def _build_pause_row(event: Mapping[str, Any]) -> dict[str, Any]:
    payload = event["payload"]
    stage_id = payload.get("current_stage_id") or "__component__"
    return _build_console_row(
        event="stage_paused",
        severity="warning",
        label_key=_LABEL_KEYS["stage_paused"],
        source_event_ids=[event["event_id"]],
        source_component_seq_start=event["component_seq"],
        source_component_seq_end=event["component_seq"],
        component_attempt_id=event["component_attempt_id"],
        component_attempt_index=event["component_attempt_index"],
        ts=event["ts"],
        stage_id=stage_id,
        agent=event["agent"],
        detail=_empty_detail(
            reason={
                "reason_code": payload["reason_code"],
                "reason_category": payload.get("reason_category"),
                "incident_id": payload.get("incident_id"),
                "resume_available": True,
                "current_stage_id": payload.get("current_stage_id"),
                "current_work_id": payload.get("current_work_id"),
                "checkpoint": payload.get("checkpoint"),
            }
        ),
    )


def _build_passthrough_row(event: Mapping[str, Any]) -> dict[str, Any]:
    event_type = event["event"]
    if event_type not in _PROJECTED_EVENT_TYPES:
        raise ContractValidationError(
            "console_event",
            "$.event.event",
            f"unsupported projected event: {event_type}",
        )
    payload = event["payload"]
    detail = _empty_detail()
    if event_type == "component_resumed":
        detail["resume"] = {
            "resumed_from_attempt_id": payload["resumed_from_attempt_id"],
            "checkpoint": payload["checkpoint"],
        }
    elif event_type == "stage_start":
        detail["progress"] = {
            "completed": 0,
            "total": payload["work_total"],
            "unit": payload["work_unit"],
            "current_work_id": None,
        }
    elif event_type == "progress":
        detail["progress"] = copy.deepcopy(payload)
    elif event_type in {"validation_passed", "validation_failed"}:
        detail["validator"] = {
            "validator_id": payload["validator_id"],
            "status": "passed" if event_type == "validation_passed" else "failed",
            "reason_code": payload.get("reason_code"),
        }
    elif event_type == "checkpoint":
        detail["checkpoint"] = {
            "binding": payload["checkpoint"],
            "work_id": payload["work_id"],
        }
    elif event_type == "usage_snapshot":
        detail["usage_snapshot"] = payload["snapshot"]
    elif event_type in {"stage_done", "component_done"}:
        detail["outcome"] = payload["outcome"]
    elif event_type == "component_failed":
        detail["outcome"] = payload["outcome"]
        detail["reason"] = {
            "reason_code": payload["reason_code"],
            "reason_category": payload.get("reason_category"),
            "incident_id": payload.get("incident_id"),
            "resume_available": False,
            "current_stage_id": payload.get("current_stage_id"),
            "current_work_id": payload.get("current_work_id"),
            "checkpoint": payload.get("checkpoint"),
        }
    return _build_console_row(
        event=event_type,
        severity=event["severity"],
        label_key=_LABEL_KEYS[event_type],
        source_event_ids=[event["event_id"]],
        source_component_seq_start=event["component_seq"],
        source_component_seq_end=event["component_seq"],
        component_attempt_id=event["component_attempt_id"],
        component_attempt_index=event["component_attempt_index"],
        ts=event["ts"],
        stage_id=event["stage_id"],
        agent=event["agent"],
        detail=detail,
    )


def _build_console_row(
    *,
    event: str,
    severity: str,
    label_key: str,
    source_event_ids: Sequence[str],
    source_component_seq_start: int,
    source_component_seq_end: int,
    component_attempt_id: str,
    component_attempt_index: int,
    ts: str,
    stage_id: str,
    agent: str,
    detail: Mapping[str, Any],
) -> dict[str, Any]:
    material = {
        "event": event,
        "severity": severity,
        "label_key": label_key,
        "source_event_ids": list(source_event_ids),
        "source_component_seq_start": source_component_seq_start,
        "source_component_seq_end": source_component_seq_end,
        "component_attempt_id": component_attempt_id,
        "component_attempt_index": component_attempt_index,
        "ts": ts,
        "stage_id": stage_id,
        "agent": agent,
        "detail": copy.deepcopy(dict(detail)),
    }
    row_id = "evalconsole_row_" + canonical_sha256(
        material, policy=_ROW_POLICY
    )[:32]
    draft = {
        "row_id": row_id,
        **material,
        "integrity": {"row_sha256": _ZERO_HASH},
    }
    return _validate_console_row(
        seal_payload(draft, policy=_ROW_POLICY, hash_path=_ROW_HASH_PATH),
        path="$row",
    )


def _validate_console_row(value: Any, *, path: str) -> dict[str, Any]:
    row = require_mapping(value, path=path)
    require_exact_keys(
        row,
        required={
            "row_id",
            "event",
            "severity",
            "label_key",
            "source_event_ids",
            "source_component_seq_start",
            "source_component_seq_end",
            "component_attempt_id",
            "component_attempt_index",
            "ts",
            "stage_id",
            "agent",
            "detail",
            "integrity",
        },
        path=path,
    )
    source_event_ids = [
        require_string(item, path=f"{path}.source_event_ids[{index}]")
        for index, item in enumerate(
            require_list(row["source_event_ids"], path=f"{path}.source_event_ids")
        )
    ]
    if not source_event_ids:
        raise ContractValidationError(
            "console_source_events",
            f"{path}.source_event_ids",
            "console row must bind at least one source event",
        )
    require_unique(source_event_ids, path=f"{path}.source_event_ids")
    start = require_int(
        row["source_component_seq_start"],
        path=f"{path}.source_component_seq_start",
        minimum=1,
    )
    end = require_int(
        row["source_component_seq_end"],
        path=f"{path}.source_component_seq_end",
        minimum=1,
    )
    if end < start:
        raise ContractValidationError(
            "console_source_sequence",
            f"{path}.source_component_seq_end",
            "console row source range is reversed",
        )
    integrity = require_mapping(row["integrity"], path=f"{path}.integrity")
    require_exact_keys(
        integrity, required={"row_sha256"}, path=f"{path}.integrity"
    )
    normalized = {
        "row_id": _require_row_id(row["row_id"], path=f"{path}.row_id"),
        "event": require_enum(
            row["event"], _PROJECTED_EVENT_TYPES, path=f"{path}.event"
        ),
        "severity": require_enum(
            row["severity"], _SEVERITIES, path=f"{path}.severity"
        ),
        "label_key": require_string(row["label_key"], path=f"{path}.label_key"),
        "source_event_ids": source_event_ids,
        "source_component_seq_start": start,
        "source_component_seq_end": end,
        "component_attempt_id": require_string(
            row["component_attempt_id"], path=f"{path}.component_attempt_id"
        ),
        "component_attempt_index": require_int(
            row["component_attempt_index"],
            path=f"{path}.component_attempt_index",
            minimum=1,
        ),
        "ts": require_rfc3339(row["ts"], path=f"{path}.ts"),
        "stage_id": require_string(row["stage_id"], path=f"{path}.stage_id"),
        "agent": require_string(row["agent"], path=f"{path}.agent"),
        "detail": _validate_console_detail(row["detail"], path=f"{path}.detail"),
        "integrity": {
            "row_sha256": require_sha256(
                integrity["row_sha256"], path=f"{path}.integrity.row_sha256"
            )
        },
    }
    expected_row_id = "evalconsole_row_" + canonical_sha256(
        {
            key: value
            for key, value in normalized.items()
            if key not in {"row_id", "integrity"}
        },
        policy=_ROW_POLICY,
    )[:32]
    if normalized["row_id"] != expected_row_id:
        raise ContractValidationError(
            "console_row_id", f"{path}.row_id", "console row identity drift"
        )
    if not verify_payload_hash(
        normalized, policy=_ROW_POLICY, hash_path=_ROW_HASH_PATH
    ):
        raise ContractValidationError(
            "console_row_hash",
            f"{path}.integrity.row_sha256",
            "console row hash drift",
        )
    _assert_public_safe(normalized, path=path)
    result = canonicalize(normalized, policy=_ROW_POLICY)
    assert isinstance(result, dict)
    return result


def _empty_detail(**overrides: Any) -> dict[str, Any]:
    result = {
        "progress": None,
        "validator": None,
        "retry": None,
        "checkpoint": None,
        "usage_snapshot": None,
        "resume": None,
        "reason": None,
        "outcome": None,
    }
    result.update(overrides)
    return result


def _validate_console_detail(value: Any, *, path: str) -> dict[str, Any]:
    row = require_mapping(value, path=path)
    keys = {
        "progress",
        "validator",
        "retry",
        "checkpoint",
        "usage_snapshot",
        "resume",
        "reason",
        "outcome",
    }
    require_exact_keys(row, required=keys, path=path)
    return {
        "progress": _validate_nullable_progress(row["progress"], path=f"{path}.progress"),
        "validator": _validate_nullable_validator(
            row["validator"], path=f"{path}.validator"
        ),
        "retry": _validate_nullable_retry(row["retry"], path=f"{path}.retry"),
        "checkpoint": _validate_nullable_checkpoint(
            row["checkpoint"], path=f"{path}.checkpoint"
        ),
        "usage_snapshot": (
            None
            if row["usage_snapshot"] is None
            else validate_typed_artifact_binding_v1(
                row["usage_snapshot"], path=f"{path}.usage_snapshot"
            )
        ),
        "resume": _validate_nullable_resume(row["resume"], path=f"{path}.resume"),
        "reason": _validate_nullable_reason(row["reason"], path=f"{path}.reason"),
        "outcome": require_nullable_string(row["outcome"], path=f"{path}.outcome"),
    }


def _validate_nullable_progress(value: Any, *, path: str) -> dict[str, Any] | None:
    if value is None:
        return None
    row = require_mapping(value, path=path)
    require_exact_keys(
        row, required={"completed", "total", "unit", "current_work_id"}, path=path
    )
    completed = require_int(row["completed"], path=f"{path}.completed", minimum=0)
    total = require_int(row["total"], path=f"{path}.total", minimum=0)
    if completed > total:
        raise ContractValidationError(
            "console_progress", f"{path}.completed", "completed exceeds total"
        )
    return {
        "completed": completed,
        "total": total,
        "unit": require_string(row["unit"], path=f"{path}.unit"),
        "current_work_id": require_nullable_string(
            row["current_work_id"], path=f"{path}.current_work_id"
        ),
    }


def _validate_nullable_validator(value: Any, *, path: str) -> dict[str, Any] | None:
    if value is None:
        return None
    row = require_mapping(value, path=path)
    require_exact_keys(
        row, required={"validator_id", "status", "reason_code"}, path=path
    )
    return {
        "validator_id": require_string(
            row["validator_id"], path=f"{path}.validator_id"
        ),
        "status": require_enum(
            row["status"], {"passed", "failed"}, path=f"{path}.status"
        ),
        "reason_code": require_nullable_string(
            row["reason_code"], path=f"{path}.reason_code"
        ),
    }


def _validate_nullable_retry(value: Any, *, path: str) -> dict[str, Any] | None:
    if value is None:
        return None
    row = require_mapping(value, path=path)
    require_exact_keys(
        row,
        required={
            "retry_kind",
            "logical_request_id",
            "retry_count",
            "physical_attempt_indexes",
            "reason_codes",
            "outcome",
        },
        path=path,
    )
    physical_attempt_indexes = [
        require_int(item, path=f"{path}.physical_attempt_indexes[{index}]", minimum=1)
        for index, item in enumerate(
            require_list(
                row["physical_attempt_indexes"],
                path=f"{path}.physical_attempt_indexes",
            )
        )
    ]
    reason_codes = [
        require_string(item, path=f"{path}.reason_codes[{index}]")
        for index, item in enumerate(
            require_list(row["reason_codes"], path=f"{path}.reason_codes")
        )
    ]
    require_unique(
        physical_attempt_indexes, path=f"{path}.physical_attempt_indexes"
    )
    require_unique(reason_codes, path=f"{path}.reason_codes")
    if not reason_codes:
        raise ContractValidationError(
            "console_retry_reason", f"{path}.reason_codes", "retry reason is required"
        )
    return {
        "retry_kind": require_enum(
            row["retry_kind"], {"transport", "semantic"}, path=f"{path}.retry_kind"
        ),
        "logical_request_id": require_string(
            row["logical_request_id"], path=f"{path}.logical_request_id"
        ),
        "retry_count": require_int(
            row["retry_count"], path=f"{path}.retry_count", minimum=1
        ),
        "physical_attempt_indexes": sorted(physical_attempt_indexes),
        "reason_codes": sorted(reason_codes),
        "outcome": require_enum(
            row["outcome"], _RETRY_OUTCOMES, path=f"{path}.outcome"
        ),
    }


def _validate_nullable_checkpoint(
    value: Any, *, path: str
) -> dict[str, Any] | None:
    if value is None:
        return None
    row = require_mapping(value, path=path)
    require_exact_keys(row, required={"binding", "work_id"}, path=path)
    return {
        "binding": validate_typed_artifact_binding_v1(
            row["binding"], path=f"{path}.binding"
        ),
        "work_id": require_nullable_string(row["work_id"], path=f"{path}.work_id"),
    }


def _validate_nullable_resume(value: Any, *, path: str) -> dict[str, Any] | None:
    if value is None:
        return None
    row = require_mapping(value, path=path)
    require_exact_keys(
        row, required={"resumed_from_attempt_id", "checkpoint"}, path=path
    )
    return {
        "resumed_from_attempt_id": require_string(
            row["resumed_from_attempt_id"],
            path=f"{path}.resumed_from_attempt_id",
        ),
        "checkpoint": validate_typed_artifact_binding_v1(
            row["checkpoint"], path=f"{path}.checkpoint"
        ),
    }


def _validate_nullable_reason(value: Any, *, path: str) -> dict[str, Any] | None:
    if value is None:
        return None
    row = require_mapping(value, path=path)
    require_exact_keys(
        row,
        required={
            "reason_code",
            "reason_category",
            "incident_id",
            "resume_available",
            "current_stage_id",
            "current_work_id",
            "checkpoint",
        },
        path=path,
    )
    resume_available = row["resume_available"]
    if not isinstance(resume_available, bool):
        raise ContractValidationError(
            "console_resume_available",
            f"{path}.resume_available",
            "expected boolean",
        )
    return {
        "reason_code": require_string(
            row["reason_code"], path=f"{path}.reason_code"
        ),
        "reason_category": require_nullable_string(
            row["reason_category"], path=f"{path}.reason_category"
        ),
        "incident_id": require_nullable_string(
            row["incident_id"], path=f"{path}.incident_id"
        ),
        "resume_available": resume_available,
        "current_stage_id": require_nullable_string(
            row["current_stage_id"], path=f"{path}.current_stage_id"
        ),
        "current_work_id": require_nullable_string(
            row["current_work_id"], path=f"{path}.current_work_id"
        ),
        "checkpoint": (
            None
            if row["checkpoint"] is None
            else validate_typed_artifact_binding_v1(
                row["checkpoint"], path=f"{path}.checkpoint"
            )
        ),
    }


def _validate_console_state(value: Any, *, path: str) -> dict[str, Any]:
    row = require_mapping(value, path=path)
    require_exact_keys(
        row, required={"open_retry_groups", "paused_incident_ids"}, path=path
    )
    groups = [
        _validate_retry_group(item, path=f"{path}.open_retry_groups[{index}]")
        for index, item in enumerate(
            require_list(
                row["open_retry_groups"], path=f"{path}.open_retry_groups"
            )
        )
    ]
    group_keys = [
        (group["stage_id"], group["retry_kind"], group["logical_request_id"])
        for group in groups
    ]
    require_unique(group_keys, path=f"{path}.open_retry_groups")
    paused = [
        require_string(item, path=f"{path}.paused_incident_ids[{index}]")
        for index, item in enumerate(
            require_list(
                row["paused_incident_ids"], path=f"{path}.paused_incident_ids"
            )
        )
    ]
    require_unique(paused, path=f"{path}.paused_incident_ids")
    return {
        "open_retry_groups": groups,
        "paused_incident_ids": sorted(paused),
    }


def _validate_retry_group(value: Any, *, path: str) -> dict[str, Any]:
    row = require_mapping(value, path=path)
    require_exact_keys(
        row,
        required={
            "stage_id",
            "agent",
            "retry_kind",
            "logical_request_id",
            "retry_count",
            "physical_attempt_indexes",
            "reason_codes",
            "first_component_seq",
            "last_component_seq",
            "source_event_ids",
        },
        path=path,
    )
    retry = _validate_nullable_retry(
        {
            "retry_kind": row["retry_kind"],
            "logical_request_id": row["logical_request_id"],
            "retry_count": row["retry_count"],
            "physical_attempt_indexes": row["physical_attempt_indexes"],
            "reason_codes": row["reason_codes"],
            "outcome": "component_halted",
        },
        path=path,
    )
    assert retry is not None
    first = require_int(
        row["first_component_seq"], path=f"{path}.first_component_seq", minimum=1
    )
    last = require_int(
        row["last_component_seq"], path=f"{path}.last_component_seq", minimum=first
    )
    source_event_ids = [
        require_string(item, path=f"{path}.source_event_ids[{index}]")
        for index, item in enumerate(
            require_list(row["source_event_ids"], path=f"{path}.source_event_ids")
        )
    ]
    require_unique(source_event_ids, path=f"{path}.source_event_ids")
    if len(source_event_ids) != retry["retry_count"]:
        raise ContractValidationError(
            "console_retry_count",
            f"{path}.source_event_ids",
            "retry count differs from source retry events",
        )
    return {
        "stage_id": require_string(row["stage_id"], path=f"{path}.stage_id"),
        "agent": require_string(row["agent"], path=f"{path}.agent"),
        "retry_kind": retry["retry_kind"],
        "logical_request_id": retry["logical_request_id"],
        "retry_count": retry["retry_count"],
        "physical_attempt_indexes": retry["physical_attempt_indexes"],
        "reason_codes": retry["reason_codes"],
        "first_component_seq": first,
        "last_component_seq": last,
        "source_event_ids": source_event_ids,
    }


def _validate_cumulative(value: Any, *, path: str) -> dict[str, Any]:
    row = require_mapping(value, path=path)
    require_exact_keys(row, required={"row_count", "row_chain_sha256"}, path=path)
    return {
        "row_count": require_int(
            row["row_count"], path=f"{path}.row_count", minimum=0
        ),
        "row_chain_sha256": require_sha256(
            row["row_chain_sha256"], path=f"{path}.row_chain_sha256"
        ),
    }


def _validate_prefixes(value: Any) -> dict[str, dict[str, Any]]:
    row = require_mapping(value, path="$projection.prefixes")
    require_exact_keys(
        row,
        required={"component_events", "recovery_journal", "diagnostics"},
        path="$projection.prefixes",
    )
    return {
        name: _validate_prefix_record(
            row[name], path=f"$projection.prefixes.{name}"
        )
        for name in ("component_events", "recovery_journal", "diagnostics")
    }


def _prefix_record(kind: str, hashes: Sequence[str]) -> dict[str, Any]:
    normalized_hashes = [
        require_sha256(value, path="$.prefix.records[*]") for value in hashes
    ]
    return {
        "record_count": len(normalized_hashes),
        "through_sha256": None if not normalized_hashes else normalized_hashes[-1],
        "prefix_sha256": canonical_sha256(
            {"kind": kind, "records": normalized_hashes}, policy=_PREFIX_POLICY
        ),
    }


def _validate_prefix_record(value: Any, *, path: str) -> dict[str, Any]:
    row = require_mapping(value, path=path)
    require_exact_keys(
        row,
        required={"record_count", "through_sha256", "prefix_sha256"},
        path=path,
    )
    count = require_int(row["record_count"], path=f"{path}.record_count", minimum=0)
    through = (
        None
        if row["through_sha256"] is None
        else require_sha256(row["through_sha256"], path=f"{path}.through_sha256")
    )
    if (count == 0) != (through is None):
        raise ContractValidationError(
            "console_prefix_through",
            f"{path}.through_sha256",
            "empty prefix must use null through hash",
        )
    return {
        "record_count": count,
        "through_sha256": through,
        "prefix_sha256": require_sha256(
            row["prefix_sha256"], path=f"{path}.prefix_sha256"
        ),
    }


def _require_prefix_extension(
    previous: Mapping[str, Any] | None,
    *,
    journal: Sequence[Mapping[str, Any]],
    diagnostics: Sequence[Mapping[str, Any]],
) -> None:
    if previous is None:
        return
    comparisons = (
        (
            "recovery_journal",
            [row["journal_sha256"] for row in journal],
            "evaluation_recovery_journal_v1",
        ),
        (
            "diagnostics",
            [row["sha256"] for row in diagnostics],
            "evaluation_internal_incidents_v1",
        ),
    )
    for name, hashes, kind in comparisons:
        previous_prefix = previous["prefixes"][name]
        previous_count = previous_prefix["record_count"]
        if len(hashes) < previous_count:
            raise ContractValidationError(
                "console_prefix_regression",
                f"$.{name}",
                "prefix cannot remove previously bound records",
            )
        if _prefix_record(kind, hashes[:previous_count]) != previous_prefix:
            raise ContractValidationError(
                "console_prefix_drift",
                f"$.{name}",
                "previously bound prefix changed",
            )


def _require_row_id(value: Any, *, path: str) -> str:
    row_id = require_string(value, path=path)
    if _ROW_ID_RE.fullmatch(row_id) is None:
        raise ContractValidationError(
            "console_row_id", path, "invalid console row identity"
        )
    return row_id


def _assert_public_safe(value: Any, *, path: str) -> None:
    if isinstance(value, str):
        folded = value.casefold()
        if (
            _ABSOLUTE_WINDOWS_PATH_RE.match(value)
            or value.startswith("\\\\")
            or any(token in folded for token in _FORBIDDEN_PUBLIC_TEXT)
        ):
            raise ContractValidationError(
                "console_public_leak",
                path,
                "Console projection contains diagnostic, path, or secret-like text",
            )
        return
    if isinstance(value, Mapping):
        for key, item in value.items():
            _assert_public_safe(item, path=f"{path}.{key}")
        return
    if isinstance(value, list):
        for index, item in enumerate(value):
            _assert_public_safe(item, path=f"{path}[{index}]")
