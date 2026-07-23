from __future__ import annotations

import copy
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from pipeline.eval.contracts_v1 import (
    CanonicalPolicy,
    ContractValidationError,
    canonical_sha256,
    canonicalize,
    require_enum,
    require_exact_keys,
    require_int,
    require_list,
    require_mapping,
    require_nullable_string,
    require_relative_path,
    require_rfc3339,
    require_sha256,
    require_string,
    require_unique,
    seal_payload,
    verify_payload_hash,
)
from pipeline.llm_backend.contracts_v1 import (
    validate_cache_observation,
    validate_llm_attempt_usage,
)


__all__ = [
    "EvaluationComponentUsageTrackerV1",
    "build_evaluation_component_usage_snapshot_v1",
    "validate_evaluation_component_usage_snapshot_chain_v1",
    "validate_evaluation_component_usage_snapshot_v1",
]


SCHEMA_ID = "EvaluationComponentUsageSnapshotV1"
SCHEMA_VERSION = "1.0.0"
_HASH_PATH = ("integrity", "usage_snapshot_sha256")
_POLICY = CanonicalPolicy(
    set_like_paths=frozenset(),
    semantic_sequence_paths=frozenset(
        {
            ("accepted_usage_ids",),
            ("accepted_cache_observation_ids",),
            ("stage_totals",),
        }
    ),
)
_RECORD_CHAIN_POLICY = CanonicalPolicy(
    set_like_paths=frozenset(), semantic_sequence_paths=frozenset()
)
_ZERO_CHAIN_SHA256 = canonical_sha256(
    {"schema_id": "EvaluationAcceptedLlmRecordChainV1", "records": 0},
    policy=_RECORD_CHAIN_POLICY,
)
_TOTAL_FIELDS = (
    "physical_attempt_count",
    "succeeded_attempt_count",
    "failed_attempt_count",
    "cache_observation_count",
    "provider_calls_avoided",
    "prompt_tokens",
    "cached_input_tokens",
    "completion_tokens",
    "reasoning_tokens",
    "total_tokens",
    "cost_usd",
    "cost_status",
    "unknown_attempt_count",
)


@dataclass(frozen=True, slots=True)
class _AcceptedEvidenceV1:
    kind: str
    stage_id: str
    role_id: str
    source_ledger_ref: str
    execution_target: Mapping[str, Any]
    record: Mapping[str, Any]


class EvaluationComponentUsageTrackerV1:
    """Build immutable cumulative snapshots without re-counting Resume evidence."""

    def __init__(
        self,
        *,
        workflow_run_id: str,
        component_run_id: str,
        stage_ids: Sequence[str],
        snapshots: Sequence[Mapping[str, Any]] = (),
    ) -> None:
        self.workflow_run_id = require_string(
            workflow_run_id, path="$.workflow_run_id"
        )
        self.component_run_id = require_string(
            component_run_id, path="$.component_run_id"
        )
        self.stage_ids = tuple(
            require_string(item, path="$.stage_ids[*]") for item in stage_ids
        )
        require_unique(self.stage_ids, path="$.stage_ids")
        if not self.stage_ids:
            raise ContractValidationError(
                "usage_stage_plan", "$.stage_ids", "at least one stage is required"
            )
        self._snapshots = list(
            validate_evaluation_component_usage_snapshot_chain_v1(
                snapshots,
                workflow_run_id=self.workflow_run_id,
                component_run_id=self.component_run_id,
                stage_ids=self.stage_ids,
            )
        )
        self._evidence_by_id: dict[tuple[str, str], dict[str, Any]] = {}
        for snapshot in self._snapshots:
            current = snapshot["current_record"]
            if current["kind"] == "final":
                continue
            record_id = _record_id(current)
            key = (current["kind"], record_id)
            prior = self._evidence_by_id.get(key)
            if prior is not None and prior != current:
                raise ContractValidationError(
                    "usage_record_conflict",
                    "$snapshots",
                    "accepted usage/cache ID changed bytes",
                )
            self._evidence_by_id[key] = copy.deepcopy(current)

    @property
    def snapshots(self) -> tuple[dict[str, Any], ...]:
        return tuple(copy.deepcopy(self._snapshots))

    @property
    def latest(self) -> dict[str, Any] | None:
        return None if not self._snapshots else copy.deepcopy(self._snapshots[-1])

    def accept_usage(
        self,
        usage: Mapping[str, Any],
        *,
        stage_id: str,
        role_id: str,
        source_ledger_ref: str,
        execution_target: Mapping[str, Any],
        component_attempt_id: str,
        component_attempt_index: int,
        accepted_through_component_seq: int,
        current_work_id: str | None,
        generated_at: str,
    ) -> dict[str, Any] | None:
        return self._accept(
            _AcceptedEvidenceV1(
                kind="usage",
                stage_id=stage_id,
                role_id=role_id,
                source_ledger_ref=source_ledger_ref,
                execution_target=_normalize_execution_target(
                    execution_target, path="$.execution_target"
                ),
                record=validate_llm_attempt_usage(usage),
            ),
            component_attempt_id=component_attempt_id,
            component_attempt_index=component_attempt_index,
            accepted_through_component_seq=accepted_through_component_seq,
            current_work_id=current_work_id,
            generated_at=generated_at,
        )

    def accept_cache_observation(
        self,
        observation: Mapping[str, Any],
        *,
        stage_id: str,
        role_id: str,
        source_ledger_ref: str,
        execution_target: Mapping[str, Any],
        component_attempt_id: str,
        component_attempt_index: int,
        accepted_through_component_seq: int,
        current_work_id: str | None,
        generated_at: str,
    ) -> dict[str, Any] | None:
        return self._accept(
            _AcceptedEvidenceV1(
                kind="cache",
                stage_id=stage_id,
                role_id=role_id,
                source_ledger_ref=source_ledger_ref,
                execution_target=_normalize_execution_target(
                    execution_target, path="$.execution_target"
                ),
                record=validate_cache_observation(observation),
            ),
            component_attempt_id=component_attempt_id,
            component_attempt_index=component_attempt_index,
            accepted_through_component_seq=accepted_through_component_seq,
            current_work_id=current_work_id,
            generated_at=generated_at,
        )

    def finalize(
        self,
        *,
        stage_id: str,
        component_attempt_id: str,
        component_attempt_index: int,
        accepted_through_component_seq: int,
        generated_at: str,
    ) -> dict[str, Any] | None:
        if self._snapshots and self._snapshots[-1]["current_record"]["kind"] == "final":
            return None
        snapshot = build_evaluation_component_usage_snapshot_v1(
            workflow_run_id=self.workflow_run_id,
            component_run_id=self.component_run_id,
            component_attempt_id=component_attempt_id,
            component_attempt_index=component_attempt_index,
            snapshot_index=len(self._snapshots) + 1,
            stage_ids=self.stage_ids,
            stage_id=stage_id,
            current_work_id=None,
            accepted_through_component_seq=accepted_through_component_seq,
            current_record={"kind": "final", "evidence": None},
            previous_snapshot=self._snapshots[-1] if self._snapshots else None,
            generated_at=generated_at,
        )
        self._snapshots.append(snapshot)
        return copy.deepcopy(snapshot)

    def _accept(
        self,
        evidence: _AcceptedEvidenceV1,
        *,
        component_attempt_id: str,
        component_attempt_index: int,
        accepted_through_component_seq: int,
        current_work_id: str | None,
        generated_at: str,
    ) -> dict[str, Any] | None:
        if self._snapshots and self._snapshots[-1]["current_record"]["kind"] == "final":
            raise ContractValidationError(
                "usage_terminal",
                "$usage",
                "provider/cache evidence cannot follow the final usage snapshot",
            )
        stage_id = require_enum(
            evidence.stage_id, set(self.stage_ids), path="$.stage_id"
        )
        current = _normalize_current_record(
            {
                "kind": evidence.kind,
                "evidence": {
                    "stage_id": stage_id,
                    "role_id": evidence.role_id,
                    "source_ledger_ref": evidence.source_ledger_ref,
                    "execution_target": evidence.execution_target,
                    (
                        "attempt_usage"
                        if evidence.kind == "usage"
                        else "cache_observation"
                    ): evidence.record,
                },
            },
            path="$.current_record",
        )
        key = (current["kind"], _record_id(current))
        prior = self._evidence_by_id.get(key)
        if prior is not None:
            if prior != current:
                raise ContractValidationError(
                    "usage_record_conflict",
                    "$usage",
                    "accepted usage/cache ID changed bytes on Resume",
                )
            return None
        snapshot = build_evaluation_component_usage_snapshot_v1(
            workflow_run_id=self.workflow_run_id,
            component_run_id=self.component_run_id,
            component_attempt_id=component_attempt_id,
            component_attempt_index=component_attempt_index,
            snapshot_index=len(self._snapshots) + 1,
            stage_ids=self.stage_ids,
            stage_id=stage_id,
            current_work_id=current_work_id,
            accepted_through_component_seq=accepted_through_component_seq,
            current_record=current,
            previous_snapshot=self._snapshots[-1] if self._snapshots else None,
            generated_at=generated_at,
        )
        self._snapshots.append(snapshot)
        self._evidence_by_id[key] = current
        return copy.deepcopy(snapshot)


def build_evaluation_component_usage_snapshot_v1(
    *,
    workflow_run_id: str,
    component_run_id: str,
    component_attempt_id: str,
    component_attempt_index: int,
    snapshot_index: int,
    stage_ids: Sequence[str],
    stage_id: str,
    current_work_id: str | None,
    accepted_through_component_seq: int,
    current_record: Mapping[str, Any],
    previous_snapshot: Mapping[str, Any] | None,
    generated_at: str,
) -> dict[str, Any]:
    ordered_stages = tuple(
        require_string(item, path="$.stage_ids[*]") for item in stage_ids
    )
    require_unique(ordered_stages, path="$.stage_ids")
    current = _normalize_current_record(current_record, path="$.current_record")
    previous = (
        None
        if previous_snapshot is None
        else _validate_evaluation_component_usage_snapshot_v1(
            previous_snapshot,
            stage_ids=ordered_stages,
            detached_predecessor=True,
        )
    )
    usage_ids = [] if previous is None else list(previous["accepted_usage_ids"])
    cache_ids = (
        [] if previous is None else list(previous["accepted_cache_observation_ids"])
    )
    if current["kind"] == "usage":
        usage_ids.append(current["evidence"]["attempt_usage"]["attempt_usage_id"])
    elif current["kind"] == "cache":
        cache_ids.append(
            current["evidence"]["cache_observation"]["observation_id"]
        )
    stage_totals = _next_stage_totals(
        ordered_stages, previous=previous, current=current
    )
    component_totals = _sum_stage_totals(stage_totals)
    previous_hash = (
        None
        if previous is None
        else previous["integrity"]["usage_snapshot_sha256"]
    )
    prior_record_chain = (
        _ZERO_CHAIN_SHA256
        if previous is None
        else previous["accepted_record_chain_sha256"]
    )
    record_chain = _next_record_chain(prior_record_chain, current)
    draft = {
        "schema_id": SCHEMA_ID,
        "schema_version": SCHEMA_VERSION,
        "workflow_run_id": require_string(
            workflow_run_id, path="$.workflow_run_id"
        ),
        "component_run_id": require_string(
            component_run_id, path="$.component_run_id"
        ),
        "component_attempt_id": require_string(
            component_attempt_id, path="$.component_attempt_id"
        ),
        "component_attempt_index": require_int(
            component_attempt_index,
            path="$.component_attempt_index",
            minimum=1,
        ),
        "snapshot_index": require_int(
            snapshot_index, path="$.snapshot_index", minimum=1
        ),
        "stage_id": require_enum(stage_id, set(ordered_stages), path="$.stage_id"),
        "current_work_id": require_nullable_string(
            current_work_id, path="$.current_work_id"
        ),
        "accepted_through_component_seq": require_int(
            accepted_through_component_seq,
            path="$.accepted_through_component_seq",
            minimum=1,
        ),
        "current_record": current,
        "accepted_usage_ids": usage_ids,
        "accepted_cache_observation_ids": cache_ids,
        "stage_totals": stage_totals,
        "component_totals": component_totals,
        "accepted_record_chain_sha256": record_chain,
        "previous_usage_snapshot_sha256": previous_hash,
        "generated_at": require_rfc3339(generated_at, path="$.generated_at"),
        "integrity": {"usage_snapshot_sha256": "0" * 64},
    }
    return validate_evaluation_component_usage_snapshot_v1(
        seal_payload(draft, policy=_POLICY, hash_path=_HASH_PATH),
        previous_snapshot=previous,
        stage_ids=ordered_stages,
    )


def validate_evaluation_component_usage_snapshot_v1(
    value: Mapping[str, Any],
    *,
    previous_snapshot: Mapping[str, Any] | None = None,
    stage_ids: Sequence[str] | None = None,
) -> dict[str, Any]:
    return _validate_evaluation_component_usage_snapshot_v1(
        value,
        previous_snapshot=previous_snapshot,
        stage_ids=stage_ids,
        detached_predecessor=False,
    )


def _validate_evaluation_component_usage_snapshot_v1(
    value: Mapping[str, Any],
    *,
    previous_snapshot: Mapping[str, Any] | None = None,
    stage_ids: Sequence[str] | None = None,
    detached_predecessor: bool,
) -> dict[str, Any]:
    row = require_mapping(value, path="$usage_snapshot")
    require_exact_keys(
        row,
        required={
            "schema_id",
            "schema_version",
            "workflow_run_id",
            "component_run_id",
            "component_attempt_id",
            "component_attempt_index",
            "snapshot_index",
            "stage_id",
            "current_work_id",
            "accepted_through_component_seq",
            "current_record",
            "accepted_usage_ids",
            "accepted_cache_observation_ids",
            "stage_totals",
            "component_totals",
            "accepted_record_chain_sha256",
            "previous_usage_snapshot_sha256",
            "generated_at",
            "integrity",
        },
        path="$usage_snapshot",
    )
    totals = [
        _validate_totals(item, path=f"$usage_snapshot.stage_totals[{index}]")
        for index, item in enumerate(
            require_list(row["stage_totals"], path="$usage_snapshot.stage_totals")
        )
    ]
    observed_stage_ids = tuple(item["stage_id"] for item in totals)
    if stage_ids is not None:
        expected_stage_ids = tuple(
            require_string(item, path="$.stage_ids[*]") for item in stage_ids
        )
        if observed_stage_ids != expected_stage_ids:
            raise ContractValidationError(
                "usage_stage_plan",
                "$usage_snapshot.stage_totals",
                "stage totals do not preserve the declared stage order",
            )
    if len(observed_stage_ids) != len(set(observed_stage_ids)):
        raise ContractValidationError(
            "usage_stage_plan",
            "$usage_snapshot.stage_totals",
            "stage totals repeat a stage",
        )
    current = _normalize_current_record(
        row["current_record"], path="$usage_snapshot.current_record"
    )
    normalized = {
        "schema_id": require_enum(
            row["schema_id"], {SCHEMA_ID}, path="$usage_snapshot.schema_id"
        ),
        "schema_version": require_enum(
            row["schema_version"],
            {SCHEMA_VERSION},
            path="$usage_snapshot.schema_version",
        ),
        "workflow_run_id": require_string(
            row["workflow_run_id"], path="$usage_snapshot.workflow_run_id"
        ),
        "component_run_id": require_string(
            row["component_run_id"], path="$usage_snapshot.component_run_id"
        ),
        "component_attempt_id": require_string(
            row["component_attempt_id"],
            path="$usage_snapshot.component_attempt_id",
        ),
        "component_attempt_index": require_int(
            row["component_attempt_index"],
            path="$usage_snapshot.component_attempt_index",
            minimum=1,
        ),
        "snapshot_index": require_int(
            row["snapshot_index"], path="$usage_snapshot.snapshot_index", minimum=1
        ),
        "stage_id": require_enum(
            row["stage_id"],
            set(observed_stage_ids),
            path="$usage_snapshot.stage_id",
        ),
        "current_work_id": require_nullable_string(
            row["current_work_id"], path="$usage_snapshot.current_work_id"
        ),
        "accepted_through_component_seq": require_int(
            row["accepted_through_component_seq"],
            path="$usage_snapshot.accepted_through_component_seq",
            minimum=1,
        ),
        "current_record": current,
        "accepted_usage_ids": [
            require_string(item, path="$usage_snapshot.accepted_usage_ids[*]")
            for item in require_list(
                row["accepted_usage_ids"],
                path="$usage_snapshot.accepted_usage_ids",
            )
        ],
        "accepted_cache_observation_ids": [
            require_string(
                item, path="$usage_snapshot.accepted_cache_observation_ids[*]"
            )
            for item in require_list(
                row["accepted_cache_observation_ids"],
                path="$usage_snapshot.accepted_cache_observation_ids",
            )
        ],
        "stage_totals": totals,
        "component_totals": _validate_component_totals(
            row["component_totals"], path="$usage_snapshot.component_totals"
        ),
        "accepted_record_chain_sha256": require_sha256(
            row["accepted_record_chain_sha256"],
            path="$usage_snapshot.accepted_record_chain_sha256",
        ),
        "previous_usage_snapshot_sha256": (
            None
            if row["previous_usage_snapshot_sha256"] is None
            else require_sha256(
                row["previous_usage_snapshot_sha256"],
                path="$usage_snapshot.previous_usage_snapshot_sha256",
            )
        ),
        "generated_at": require_rfc3339(
            row["generated_at"], path="$usage_snapshot.generated_at"
        ),
        "integrity": _validate_integrity(row["integrity"]),
    }
    require_unique(
        normalized["accepted_usage_ids"],
        path="$usage_snapshot.accepted_usage_ids",
    )
    require_unique(
        normalized["accepted_cache_observation_ids"],
        path="$usage_snapshot.accepted_cache_observation_ids",
    )
    if normalized["component_totals"] != _sum_stage_totals(totals):
        raise ContractValidationError(
            "usage_totals",
            "$usage_snapshot.component_totals",
            "component totals do not equal stage totals",
        )
    if previous_snapshot is not None:
        previous = _validate_evaluation_component_usage_snapshot_v1(
            previous_snapshot,
            stage_ids=observed_stage_ids,
            detached_predecessor=True,
        )
        _validate_increment(previous, normalized)
    elif not detached_predecessor:
        _validate_first_snapshot(normalized)
    if not verify_payload_hash(
        normalized, policy=_POLICY, hash_path=_HASH_PATH
    ):
        raise ContractValidationError(
            "usage_snapshot_hash",
            "$usage_snapshot.integrity.usage_snapshot_sha256",
            "usage snapshot hash drift",
        )
    result = canonicalize(normalized, policy=_POLICY)
    assert isinstance(result, dict)
    return result


def validate_evaluation_component_usage_snapshot_chain_v1(
    values: Sequence[Mapping[str, Any]],
    *,
    workflow_run_id: str,
    component_run_id: str,
    stage_ids: Sequence[str],
) -> tuple[dict[str, Any], ...]:
    normalized: list[dict[str, Any]] = []
    previous = None
    terminal = False
    for index, raw in enumerate(values):
        current = validate_evaluation_component_usage_snapshot_v1(
            raw, previous_snapshot=previous, stage_ids=stage_ids
        )
        if current["workflow_run_id"] != workflow_run_id:
            raise ContractValidationError(
                "usage_workflow_binding",
                f"$snapshots[{index}].workflow_run_id",
                "foreign workflow usage snapshot",
            )
        if current["component_run_id"] != component_run_id:
            raise ContractValidationError(
                "usage_component_binding",
                f"$snapshots[{index}].component_run_id",
                "foreign component usage snapshot",
            )
        if terminal:
            raise ContractValidationError(
                "usage_terminal",
                f"$snapshots[{index}]",
                "final usage snapshot must be last",
            )
        terminal = current["current_record"]["kind"] == "final"
        normalized.append(current)
        previous = current
    return tuple(copy.deepcopy(normalized))


def _normalize_current_record(value: Any, *, path: str) -> dict[str, Any]:
    row = require_mapping(value, path=path)
    require_exact_keys(row, required={"kind", "evidence"}, path=path)
    kind = require_enum(row["kind"], {"usage", "cache", "final"}, path=f"{path}.kind")
    if kind == "final":
        if row["evidence"] is not None:
            raise ContractValidationError(
                "usage_final", f"{path}.evidence", "final snapshot has no current evidence"
            )
        return {"kind": "final", "evidence": None}
    evidence = require_mapping(row["evidence"], path=f"{path}.evidence")
    record_key = "attempt_usage" if kind == "usage" else "cache_observation"
    require_exact_keys(
        evidence,
        required={
            "stage_id",
            "role_id",
            "source_ledger_ref",
            "execution_target",
            record_key,
        },
        path=f"{path}.evidence",
    )
    record = (
        validate_llm_attempt_usage(evidence[record_key])
        if kind == "usage"
        else validate_cache_observation(evidence[record_key])
    )
    execution_target = _normalize_execution_target(
        evidence["execution_target"], path=f"{path}.evidence.execution_target"
    )
    if kind == "usage":
        for field in (
            "source_id",
            "source_revision",
            "physical_quota_bucket_id",
            "requested_model_id",
            "observed_model_id",
        ):
            if record[field] != execution_target[field]:
                raise ContractValidationError(
                    "usage_execution_target",
                    f"{path}.evidence.execution_target.{field}",
                    "execution target differs from accepted usage",
                )
    return {
        "kind": kind,
        "evidence": {
            "stage_id": require_string(
                evidence["stage_id"], path=f"{path}.evidence.stage_id"
            ),
            "role_id": require_string(
                evidence["role_id"], path=f"{path}.evidence.role_id"
            ),
            "source_ledger_ref": require_relative_path(
                evidence["source_ledger_ref"],
                path=f"{path}.evidence.source_ledger_ref",
            ),
            "execution_target": execution_target,
            record_key: record,
        },
    }


def _normalize_execution_target(value: Any, *, path: str) -> dict[str, Any]:
    row = require_mapping(value, path=path)
    require_exact_keys(
        row,
        required={
            "source_id",
            "source_revision",
            "physical_quota_bucket_id",
            "requested_model_id",
            "observed_model_id",
        },
        path=path,
    )
    return {
        "source_id": require_string(row["source_id"], path=f"{path}.source_id"),
        "source_revision": require_string(
            row["source_revision"], path=f"{path}.source_revision"
        ),
        "physical_quota_bucket_id": require_string(
            row["physical_quota_bucket_id"],
            path=f"{path}.physical_quota_bucket_id",
        ),
        "requested_model_id": require_string(
            row["requested_model_id"], path=f"{path}.requested_model_id"
        ),
        "observed_model_id": require_nullable_string(
            row["observed_model_id"], path=f"{path}.observed_model_id"
        ),
    }


def _record_id(current: Mapping[str, Any]) -> str:
    if current["kind"] == "usage":
        return current["evidence"]["attempt_usage"]["attempt_usage_id"]
    if current["kind"] == "cache":
        return current["evidence"]["cache_observation"]["observation_id"]
    raise ContractValidationError("usage_record", "$current", "final has no record ID")


def _empty_totals(stage_id: str | None = None) -> dict[str, Any]:
    result = {
        "physical_attempt_count": 0,
        "succeeded_attempt_count": 0,
        "failed_attempt_count": 0,
        "cache_observation_count": 0,
        "provider_calls_avoided": 0,
        "prompt_tokens": 0,
        "cached_input_tokens": 0,
        "completion_tokens": 0,
        "reasoning_tokens": 0,
        "total_tokens": 0,
        "cost_usd": 0.0,
        "cost_status": "not_applicable",
        "unknown_attempt_count": 0,
    }
    return result if stage_id is None else {"stage_id": stage_id, **result}


def _next_stage_totals(
    stage_ids: Sequence[str],
    *,
    previous: Mapping[str, Any] | None,
    current: Mapping[str, Any],
) -> list[dict[str, Any]]:
    rows = (
        [_empty_totals(stage_id) for stage_id in stage_ids]
        if previous is None
        else copy.deepcopy(previous["stage_totals"])
    )
    if current["kind"] == "final":
        return rows
    stage_id = current["evidence"]["stage_id"]
    target = next(row for row in rows if row["stage_id"] == stage_id)
    if current["kind"] == "usage":
        usage = current["evidence"]["attempt_usage"]
        target["physical_attempt_count"] += 1
        if usage["outcome"] == "succeeded":
            target["succeeded_attempt_count"] += 1
        else:
            target["failed_attempt_count"] += 1
        if any(
            usage[field] is None
            for field in (
                "prompt_tokens",
                "cached_input_tokens",
                "completion_tokens",
                "total_tokens",
            )
        ):
            target["unknown_attempt_count"] += 1
        for field in (
            "prompt_tokens",
            "cached_input_tokens",
            "completion_tokens",
            "reasoning_tokens",
            "total_tokens",
        ):
            target[field] = _nullable_sum(target[field], usage[field])
        target["cost_usd"], target["cost_status"] = _next_cost(
            target["cost_usd"], target["cost_status"], usage
        )
    else:
        observation = current["evidence"]["cache_observation"]
        target["cache_observation_count"] += 1
        if observation["provider_call_avoided"]:
            target["provider_calls_avoided"] += 1
    return rows


def _nullable_sum(left: int | None, right: int | None) -> int | None:
    return None if left is None or right is None else left + right


def _next_cost(
    prior_cost: float | None,
    prior_status: str,
    usage: Mapping[str, Any],
) -> tuple[float | None, str]:
    if prior_status == "partial_unknown" or usage["cost_status"] == "unknown":
        return None, "partial_unknown"
    prior = 0.0 if prior_status == "not_applicable" else prior_cost
    assert prior is not None and usage["cost_usd"] is not None
    return float(prior) + float(usage["cost_usd"]), "known"


def _sum_stage_totals(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    result = _empty_totals()
    for row in rows:
        for field in (
            "physical_attempt_count",
            "succeeded_attempt_count",
            "failed_attempt_count",
            "cache_observation_count",
            "provider_calls_avoided",
            "unknown_attempt_count",
        ):
            result[field] += row[field]
        for field in (
            "prompt_tokens",
            "cached_input_tokens",
            "completion_tokens",
            "reasoning_tokens",
            "total_tokens",
        ):
            result[field] = _nullable_sum(result[field], row[field])
        if result["cost_status"] == "partial_unknown" or row["cost_status"] == "partial_unknown":
            result["cost_usd"] = None
            result["cost_status"] = "partial_unknown"
        elif row["cost_status"] == "known":
            prior = 0.0 if result["cost_status"] == "not_applicable" else result["cost_usd"]
            assert prior is not None and row["cost_usd"] is not None
            result["cost_usd"] = float(prior) + float(row["cost_usd"])
            result["cost_status"] = "known"
    return result


def _validate_totals(value: Any, *, path: str) -> dict[str, Any]:
    row = require_mapping(value, path=path)
    require_exact_keys(row, required={"stage_id", *_TOTAL_FIELDS}, path=path)
    return {
        "stage_id": require_string(row["stage_id"], path=f"{path}.stage_id"),
        **_validate_total_values(row, path=path),
    }


def _validate_component_totals(value: Any, *, path: str) -> dict[str, Any]:
    row = require_mapping(value, path=path)
    require_exact_keys(row, required=set(_TOTAL_FIELDS), path=path)
    return _validate_total_values(row, path=path)


def _validate_total_values(value: Mapping[str, Any], *, path: str) -> dict[str, Any]:
    result = {
        field: require_int(value[field], path=f"{path}.{field}", minimum=0)
        for field in (
            "physical_attempt_count",
            "succeeded_attempt_count",
            "failed_attempt_count",
            "cache_observation_count",
            "provider_calls_avoided",
            "unknown_attempt_count",
        )
    }
    for field in (
        "prompt_tokens",
        "cached_input_tokens",
        "completion_tokens",
        "reasoning_tokens",
        "total_tokens",
    ):
        result[field] = (
            None
            if value[field] is None
            else require_int(value[field], path=f"{path}.{field}", minimum=0)
        )
    status = require_enum(
        value["cost_status"],
        {"not_applicable", "known", "partial_unknown"},
        path=f"{path}.cost_status",
    )
    cost = value["cost_usd"]
    if cost is not None:
        if isinstance(cost, bool) or not isinstance(cost, (int, float)):
            raise ContractValidationError(
                "usage_cost", f"{path}.cost_usd", "cost must be numeric or null"
            )
        cost = float(cost)
        if cost < 0 or cost != cost or cost in {float("inf"), float("-inf")}:
            raise ContractValidationError(
                "usage_cost", f"{path}.cost_usd", "cost must be finite and non-negative"
            )
    if status == "partial_unknown" and cost is not None:
        raise ContractValidationError(
            "usage_cost", f"{path}.cost_usd", "unknown cost must remain null"
        )
    if status in {"not_applicable", "known"} and cost is None:
        raise ContractValidationError(
            "usage_cost", f"{path}.cost_usd", "known/no-call cost requires a value"
        )
    if status == "not_applicable" and cost != 0.0:
        raise ContractValidationError(
            "usage_cost", f"{path}.cost_usd", "no-call cost must be exactly zero"
        )
    if result["succeeded_attempt_count"] + result["failed_attempt_count"] != result[
        "physical_attempt_count"
    ]:
        raise ContractValidationError(
            "usage_totals", path, "attempt outcomes do not cover physical attempts"
        )
    if result["provider_calls_avoided"] > result["cache_observation_count"]:
        raise ContractValidationError(
            "usage_totals", path, "avoided calls exceed cache observations"
        )
    return {
        **result,
        "cost_usd": cost,
        "cost_status": status,
    }


def _validate_integrity(value: Any) -> dict[str, str]:
    row = require_mapping(value, path="$usage_snapshot.integrity")
    require_exact_keys(
        row,
        required={"usage_snapshot_sha256"},
        path="$usage_snapshot.integrity",
    )
    return {
        "usage_snapshot_sha256": require_sha256(
            row["usage_snapshot_sha256"],
            path="$usage_snapshot.integrity.usage_snapshot_sha256",
        )
    }


def _validate_first_snapshot(snapshot: Mapping[str, Any]) -> None:
    if snapshot["snapshot_index"] != 1:
        raise ContractValidationError(
            "usage_snapshot_index",
            "$usage_snapshot.snapshot_index",
            "first snapshot index must be one",
        )
    if snapshot["previous_usage_snapshot_sha256"] is not None:
        raise ContractValidationError(
            "usage_snapshot_chain",
            "$usage_snapshot.previous_usage_snapshot_sha256",
            "first snapshot has no predecessor",
        )
    empty = [_empty_totals(row["stage_id"]) for row in snapshot["stage_totals"]]
    expected = _next_stage_totals(
        tuple(row["stage_id"] for row in empty),
        previous=None,
        current=snapshot["current_record"],
    )
    if snapshot["stage_totals"] != expected:
        raise ContractValidationError(
            "usage_totals", "$usage_snapshot.stage_totals", "first totals drift"
        )
    if snapshot["accepted_record_chain_sha256"] != _next_record_chain(
        _ZERO_CHAIN_SHA256, snapshot["current_record"]
    ):
        raise ContractValidationError(
            "usage_record_chain",
            "$usage_snapshot.accepted_record_chain_sha256",
            "first accepted-record chain drift",
        )
    _validate_id_increment(None, snapshot)


def _validate_increment(
    previous: Mapping[str, Any], current: Mapping[str, Any]
) -> None:
    if current["workflow_run_id"] != previous["workflow_run_id"]:
        raise ContractValidationError(
            "usage_workflow_binding",
            "$usage_snapshot.workflow_run_id",
            "workflow changed between snapshots",
        )
    if current["component_run_id"] != previous["component_run_id"]:
        raise ContractValidationError(
            "usage_component_binding",
            "$usage_snapshot.component_run_id",
            "component changed between snapshots",
        )
    if current["snapshot_index"] != previous["snapshot_index"] + 1:
        raise ContractValidationError(
            "usage_snapshot_index",
            "$usage_snapshot.snapshot_index",
            "snapshot index must be contiguous",
        )
    if current["accepted_through_component_seq"] <= previous[
        "accepted_through_component_seq"
    ]:
        raise ContractValidationError(
            "usage_component_sequence",
            "$usage_snapshot.accepted_through_component_seq",
            "usage snapshots must advance the component sequence",
        )
    if current["previous_usage_snapshot_sha256"] != previous["integrity"][
        "usage_snapshot_sha256"
    ]:
        raise ContractValidationError(
            "usage_snapshot_chain",
            "$usage_snapshot.previous_usage_snapshot_sha256",
            "usage snapshot hash chain drift",
        )
    expected_totals = _next_stage_totals(
        tuple(row["stage_id"] for row in previous["stage_totals"]),
        previous=previous,
        current=current["current_record"],
    )
    if current["stage_totals"] != expected_totals:
        raise ContractValidationError(
            "usage_totals", "$usage_snapshot.stage_totals", "cumulative totals drift"
        )
    if current["accepted_record_chain_sha256"] != _next_record_chain(
        previous["accepted_record_chain_sha256"], current["current_record"]
    ):
        raise ContractValidationError(
            "usage_record_chain",
            "$usage_snapshot.accepted_record_chain_sha256",
            "accepted-record chain drift",
        )
    _validate_id_increment(previous, current)


def _validate_id_increment(
    previous: Mapping[str, Any] | None, current: Mapping[str, Any]
) -> None:
    prior_usage = [] if previous is None else previous["accepted_usage_ids"]
    prior_cache = (
        [] if previous is None else previous["accepted_cache_observation_ids"]
    )
    expected_usage = list(prior_usage)
    expected_cache = list(prior_cache)
    record = current["current_record"]
    if record["kind"] == "usage":
        expected_usage.append(record["evidence"]["attempt_usage"]["attempt_usage_id"])
    elif record["kind"] == "cache":
        expected_cache.append(
            record["evidence"]["cache_observation"]["observation_id"]
        )
    if current["accepted_usage_ids"] != expected_usage:
        raise ContractValidationError(
            "usage_id_chain",
            "$usage_snapshot.accepted_usage_ids",
            "usage ID chain drift",
        )
    if current["accepted_cache_observation_ids"] != expected_cache:
        raise ContractValidationError(
            "usage_id_chain",
            "$usage_snapshot.accepted_cache_observation_ids",
            "cache observation ID chain drift",
        )


def _next_record_chain(previous_sha256: str, current: Mapping[str, Any]) -> str:
    if current["kind"] == "final":
        return previous_sha256
    return canonical_sha256(
        {
            "schema_id": "EvaluationAcceptedLlmRecordChainV1",
            "previous_record_chain_sha256": previous_sha256,
            "current_record": current,
        },
        policy=_RECORD_CHAIN_POLICY,
    )
