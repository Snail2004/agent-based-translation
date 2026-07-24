"""Live semantic stages for one sealed D2L project campaign.

The component runner owns ordering, durable artifact publication, checkpointing,
and replay.  This module owns only pipeline-specific semantic execution.  Code
constructs bounded packets and validates model output; it never invents a term,
translation, merge, or audit decision.
"""

from __future__ import annotations

from collections import Counter
from copy import deepcopy
from dataclasses import asdict
from datetime import UTC, datetime
from hashlib import sha256
import json
from pathlib import Path
import re
import sqlite3
from types import SimpleNamespace
from typing import Any, Callable, Mapping, Protocol, Sequence

from pipeline.eval.common_input_v1 import (
    seal_translation_artifact,
    validate_translation_artifact,
)
from pipeline.prepass import d2l_b2_consistency_contract_v3_7 as b2_contract
from pipeline.prepass import d2l_b2_consolidation_contract_v1 as consolidation_contract
from pipeline.prepass import d2l_b2_multi_target_contract_v1 as multi_target_contract
from pipeline.prepass.d2l_b2_consolidation_plan_v1 import (
    ConsolidationCaps,
    _candidate_sort_key,
    _index_counts,
    _sha256_json,
    build_component_plan as build_morphology_plan,
    build_draft_package,
    packetize_components,
)
from pipeline.prepass.d2l_b2_multi_target_plan_v1 import (
    MultiTargetCaps,
    build_multi_target_plan,
    packetize_multi_target_items,
)
from pipeline.prepass.d2l_b2_packet_plan_v2 import (
    PacketCaps,
    build_candidate_index,
    build_packet_plan,
    canonical_sha256,
)
from pipeline.prepass.d2l_b2_target_collision_apply_v1 import (
    apply_target_collision_audit,
)
from pipeline.prepass.d2l_b2_target_collision_plan_v1 import (
    _current_entry_from_audit,
    _current_entry_from_singleton,
    build_post_morphology_index,
    build_target_collision_plan,
)
from pipeline.prepass.d2l_candidate_discovery_v2 import (
    RESPONSE_FORMAT as DISCOVERY_RESPONSE_FORMAT,
    VALIDATOR_VERSION as DISCOVERY_VALIDATOR_VERSION,
    parse_discovery_json,
    render_discovery_messages,
    validate_discovery_output,
)
from pipeline.prepass.d2l_component_stage_receipt_v1 import (
    D2LStageObservationJournalWriter,
    build_stage_receipt,
    read_observation_journal,
)
from pipeline.prepass.d2l_console_replay_contract_v1 import (
    SCORING_FRAGMENT_SCHEMA,
    build_component_usage_snapshot,
    build_scoring_handoff_fragment,
    build_stage_plan,
    canonical_sha256 as replay_sha256,
    file_sha256,
    validate_scoring_handoff_fragment,
)
from pipeline.prepass.d2l_terminology_memory_delta_v1 import commit_glossary_draft
from pipeline.translate import d2l_latex_markup_line_protected_spans_v4 as spans_v4
from pipeline.translate import d2l_latex_markup_line_protected_spans_v5 as spans_v5
from pipeline.translate import d2l_translation_quality_auditor_v3 as quality_contract
from pipeline.translate import d2l_translation_semantic_repair_v1 as repair_contract
from pipeline.translate.d2l_translation_integrity_v1 import inspect_translations
from pipeline.translate.d2l_translation_quality_observation_v1 import (
    build_quality_observation,
)
from pipeline.translate.runner import load_window_attempt_state, translate_windows
from pipeline.translate.windower import Window


LIVE_EXECUTOR_VERSION = "d2l_project_live_executor_v1_1_transport_retry"
LIVE_PROFILE_ID = "technical_d2l_v1_campaign_v2_3_protected_dual_arm"
LIVE_SCOPE_ID = "d2l_selected_campaign_scope_v1"
_COST_STATUSES = {"provider_actual", "pinned_tariff", "unknown"}
_STAGE_UNITS = {
    row["stage_id"]: row["progress"]["unit"] for row in build_stage_plan()
}


class D2LProjectLiveExecutorError(RuntimeError):
    """Raised when a live stage cannot safely publish semantic output."""


class ProjectTransport(Protocol):
    def build_client(
        self,
        role_id: str,
        *,
        component_attempt_id: int = 1,
        transport_attempt_index: int = 1,
    ) -> Any: ...


def _utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _sealed(body: Mapping[str, Any]) -> dict[str, Any]:
    payload = deepcopy(dict(body))
    payload["integrity"] = {"payload_sha256": replay_sha256(payload)}
    return payload


def _load_json(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise D2LProjectLiveExecutorError(f"cannot load {label}: {path}") from exc
    if not isinstance(value, dict):
        raise D2LProjectLiveExecutorError(f"{label} must be a JSON object")
    return value


def _dataclass_json(value: Any) -> dict[str, Any]:
    return json.loads(json.dumps(asdict(value), ensure_ascii=False))


def _role(config: Mapping[str, Any], role_id: str) -> dict[str, Any]:
    row = next(
        (item for item in config["semantic_roles"] if item["role_id"] == role_id),
        None,
    )
    if row is None:
        raise D2LProjectLiveExecutorError(f"campaign lacks semantic role: {role_id}")
    return dict(row)


def _source_text(row: Mapping[str, Any]) -> str:
    value = row.get("clean_text") or row.get("source_text")
    if not isinstance(value, str) or not value:
        raise D2LProjectLiveExecutorError(
            f"selected block has no source text: {row.get('block_id')}"
        )
    return value


def _windows(
    *,
    campaign: Mapping[str, Any],
    rows: Sequence[Mapping[str, Any]],
    family: str,
) -> list[dict[str, Any]]:
    by_id = {str(row["block_id"]): row for row in rows}
    result: list[dict[str, Any]] = []
    seen: list[str] = []
    for raw in campaign["universe"]["window_estimates"][family]["windows"]:
        block_ids = [str(value) for value in raw["block_ids"]]
        if any(block_id not in by_id for block_id in block_ids):
            raise D2LProjectLiveExecutorError(f"{family} window cites a foreign block")
        seen.extend(block_ids)
        chapter_ids = {str(by_id[block_id]["chapter_id"]) for block_id in block_ids}
        result.append(
            {
                "window_id": str(raw["window_id"]),
                "chapter_id": (
                    next(iter(chapter_ids))
                    if len(chapter_ids) == 1
                    else LIVE_SCOPE_ID
                ),
                "block_ids": block_ids,
                "source_blocks": [
                    [block_id, _source_text(by_id[block_id])] for block_id in block_ids
                ],
                "estimated_source_tokens": int(raw["estimated_source_tokens"]),
            }
        )
    expected_channels = (
        {"semantic_text"}
        if family == "b1"
        else {"semantic_text", "structured_translate"}
    )
    expected = [
        str(row["block_id"])
        for row in rows
        if row["channel"] in expected_channels
    ]
    if seen != expected:
        raise D2LProjectLiveExecutorError(f"{family} windows do not exact-cover source")
    return result


def _source_manifest(
    *, campaign: Mapping[str, Any], rows: Sequence[Mapping[str, Any]], family: str
) -> dict[str, Any]:
    windows = _windows(campaign=campaign, rows=rows, family=family)
    body = {
        "schema_version": "d2l_campaign_source_manifest_v1",
        "chapter_id": LIVE_SCOPE_ID,
        "selected_chapter_ids": list(campaign["config"]["selected_chapter_ids"]),
        "source_binding_sha256": campaign["config"]["source_binding_sha256"],
        "window_family": family,
        "windows": [
            {
                **window,
                "window_order": index,
            }
            for index, window in enumerate(windows)
        ],
    }
    body["manifest_sha256"] = canonical_sha256(body)
    return body


def _result_usage(result: Any) -> dict[str, Any]:
    usage = result.usage
    prompt = int(getattr(usage, "prompt_tokens", 0) or 0)
    completion = int(getattr(usage, "completion_tokens", 0) or 0)
    cached = int(getattr(usage, "cached_tokens", 0) or 0)
    reasoning = int(getattr(usage, "reasoning_tokens", 0) or 0)
    raw_status = str(getattr(result, "cost_status", "unknown") or "unknown")
    cost_status = raw_status if raw_status in _COST_STATUSES else "unknown"
    raw_cost = getattr(result, "cost_usd", None)
    cost_usd = None if cost_status == "unknown" else raw_cost
    return {
        "logical_request_id": str(result.logical_request_id),
        "physical_attempt_index": int(result.physical_attempt_index),
        "provider_id": str(result.provider_id),
        "model_id": str(result.model),
        "source_id": str(result.source_id),
        "masked_quota_bucket": str(result.masked_quota_bucket),
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "cached_input_tokens": cached,
        "reasoning_tokens": reasoning,
        "total_tokens": prompt + completion,
        "latency_ms": int(result.latency_ms),
        "finish_reason": result.finish_reason,
        "cost_usd": cost_usd,
        "currency": None if cost_usd is None else "USD",
        "cost_status": cost_status,
        "cache_status": str(result.cache_status),
        "cache_mechanism": str(result.cache_mechanism),
    }


class _StageObservations:
    def __init__(
        self,
        *,
        campaign: Mapping[str, Any],
        component_root: Path,
        component_attempt_id: int,
        stage_id: str,
        agent: str,
        work_kind: str,
        work_id: str,
    ) -> None:
        self.stage_id = stage_id
        self.agent = agent
        self.work_kind = work_kind
        self.work_id = work_id
        self.rows: list[dict[str, Any]] = []
        self.usage_rows: list[dict[str, Any]] = []
        self.provider_called_rows: list[bool] = []
        self.transport_failure_rows: list[dict[str, Any]] = []
        config = campaign["config"]
        self.workflow_run_id = str(config["workflow_run_id"])
        self.component_run_id = str(config["component_run_id"])
        self.component_attempt_id = component_attempt_id
        self.journal_path = component_root / "runtime/component_observations.jsonl"
        existing = read_observation_journal(self.journal_path)
        self.usage_snapshots = [
            dict(entry["observation"]["payload"])
            for entry in existing
            if entry["observation"]["event"] == "usage_snapshot"
        ]
        self.journal = D2LStageObservationJournalWriter(
            path=self.journal_path,
            workflow_run_id=self.workflow_run_id,
            component_run_id=self.component_run_id,
            component_attempt_id=component_attempt_id,
            stage_id=stage_id,
            producer=agent,
            work_id=work_id,
        )

    def _append(self, event: str, payload: Mapping[str, Any], severity: str = "info") -> None:
        observation = {
            "event": event,
            "agent": self.agent,
            "severity": severity,
            "ts": _utc_now(),
            "payload": dict(payload),
        }
        self.journal.append(observation)
        self.rows.append(observation)

    def progress(self, *, completed: int, total: int) -> None:
        self._append(
            "work_progress",
            {
                "work_kind": self.work_kind,
                "work_id": self.work_id,
                "progress": {
                    "completed": completed,
                    "total": total,
                    "unit": self.work_kind,
                },
            },
        )

    def response(self, *, result: Any, work_id: str) -> None:
        usage = _result_usage(result)
        self._append(
            "request_sent",
            {
                "logical_request_id": usage["logical_request_id"],
                "physical_attempt_index": usage["physical_attempt_index"],
                "work_kind": self.work_kind,
                "work_id": work_id,
                "provider_id": usage["provider_id"],
                "model_id": usage["model_id"],
                "source_id": usage["source_id"],
                "masked_quota_bucket": usage["masked_quota_bucket"],
            },
        )
        self._append("response_received", {"usage": usage})
        self.usage_rows.append(usage)
        provider_called_claim = getattr(result, "provider_called", None)
        provider_called = (
            not bool(getattr(result, "from_cache", False))
            if provider_called_claim is None
            else bool(provider_called_claim)
        )
        self.provider_called_rows.append(provider_called)
        attempt_usage_id = getattr(result, "attempt_usage_id", None)
        cache_observation_id = getattr(result, "cache_observation_id", None)
        if provider_called and attempt_usage_id is None:
            attempt_usage_id = "compat_attempt_" + replay_sha256(
                {
                    "logical_request_id": usage["logical_request_id"],
                    "physical_attempt_index": usage["physical_attempt_index"],
                    "stage_id": self.stage_id,
                }
            )[:32].lower()
        if not provider_called and cache_observation_id is None:
            cache_observation_id = "compat_cache_" + replay_sha256(
                {
                    "logical_request_id": usage["logical_request_id"],
                    "cache_key": str(getattr(result, "cache_key", "")),
                    "stage_id": self.stage_id,
                }
            )[:32].lower()
        accepted_usage = {
            "identity_kind": (
                "provider_attempt" if provider_called else "cache_observation"
            ),
            "attempt_usage_id": (
                None if attempt_usage_id is None else str(attempt_usage_id)
            ),
            "cache_observation_id": (
                None
                if cache_observation_id is None
                else str(cache_observation_id)
            ),
            "logical_request_id": usage["logical_request_id"],
            "semantic_attempt_index": int(
                getattr(result, "semantic_attempt_index", 1)
            ),
            "transport_retry_ordinal": int(
                getattr(result, "transport_retry_ordinal", 0)
            ),
            "physical_attempt_index": (
                usage["physical_attempt_index"] if provider_called else None
            ),
            "provider_called": provider_called,
            "source_revision": str(
                getattr(result, "source_revision", None) or "legacy_source_revision"
            ),
            "usage": usage,
        }
        snapshot = build_component_usage_snapshot(
            previous_snapshots=self.usage_snapshots,
            workflow_run_id=self.workflow_run_id,
            component_run_id=self.component_run_id,
            component_attempt_id=self.component_attempt_id,
            stage_id=self.stage_id,
            work_id=work_id,
            accepted_usage=accepted_usage,
        )
        self._append("usage_snapshot", snapshot)
        self.usage_snapshots.append(snapshot)
        retry_summary = getattr(result, "transport_retry_summary", None)
        if retry_summary is not None:
            self._append_retry_summary(
                retry_summary,
                work_id=work_id,
            )

    def transport_observer(
        self,
        *,
        work_id: str,
    ) -> Callable[[str, Mapping[str, Any]], None]:
        def observe(event: str, payload: Mapping[str, Any]) -> None:
            row = dict(payload)
            if event == "attempt_failed":
                self._append(
                    "request_sent",
                    {
                        "logical_request_id": row["logical_request_id"],
                        "physical_attempt_index": row["physical_attempt_index"],
                        "work_kind": self.work_kind,
                        "work_id": work_id,
                        "provider_id": row["provider_id"],
                        "model_id": row["model_id"],
                        "source_id": row["source_id"],
                        "masked_quota_bucket": row["masked_quota_bucket"],
                    },
                )
                failure = {
                    **row,
                    "work_kind": self.work_kind,
                    "work_id": work_id,
                }
                self._append(
                    "transport_attempt_failed",
                    failure,
                    "warning",
                )
                self.transport_failure_rows.append(failure)
                return
            if event == "retry_scheduled":
                self._append(
                    "retry",
                    {
                        "retry_kind": "transport",
                        "index": row["retry_index"],
                        "max": row["retry_max"],
                        "reason_code": row["reason_code"],
                        "logical_request_id": row["logical_request_id"],
                        "work_kind": self.work_kind,
                        "work_id": work_id,
                    },
                    "warning",
                )
                return
            if event == "retry_summary":
                self._append_retry_summary(row, work_id=work_id)
                return
            raise D2LProjectLiveExecutorError(
                f"unsupported transport observation: {event}"
            )

        return observe

    def _append_retry_summary(
        self,
        payload: Mapping[str, Any],
        *,
        work_id: str,
    ) -> None:
        self._append(
            "retry_summary",
            {
                "logical_request_id": payload["logical_request_id"],
                "retry_kind": "transport",
                "retry_count": payload["retry_count"],
                "outcome": payload["outcome"],
                "work_id": work_id,
                "reason_codes": list(payload["reason_codes"]),
            },
            "info" if payload["outcome"] == "recovered" else "warning",
        )

    def validation(
        self,
        *,
        passed: bool,
        validator_id: str,
        subject_ref: str,
        reason_codes: Sequence[str],
        retryable: bool,
    ) -> None:
        self._append(
            "validation_passed" if passed else "validation_failed",
            {
                "validator_id": validator_id,
                "subject_ref": subject_ref,
                "reason_codes": list(reason_codes),
                "retryable": retryable,
            },
            "info" if passed else "warning",
        )

    def retry(
        self,
        *,
        result: Any,
        work_id: str,
        index: int,
        maximum: int,
        reason_code: str,
    ) -> None:
        self._append(
            "retry",
            {
                "retry_kind": "semantic",
                "index": index,
                "max": maximum,
                "reason_code": reason_code,
                "logical_request_id": str(result.logical_request_id),
                "work_kind": self.work_kind,
                "work_id": work_id,
            },
            "warning",
        )

    def receipt(
        self,
        *,
        campaign: Mapping[str, Any],
        component_attempt_id: int,
        producer: str,
        work_id: str,
    ) -> dict[str, Any]:
        prompt = sum(row["prompt_tokens"] for row in self.usage_rows)
        completion = sum(row["completion_tokens"] for row in self.usage_rows)
        cached = sum(row["cached_input_tokens"] for row in self.usage_rows)
        reasoning = sum(row["reasoning_tokens"] for row in self.usage_rows)
        provider_usage = [
            row
            for row, provider_called in zip(
                self.usage_rows, self.provider_called_rows, strict=True
            )
            if provider_called
        ]
        known_costs = [
            row["cost_usd"] for row in provider_usage if row["cost_usd"] is not None
        ]
        cost_statuses = {
            row["cost_status"] for row in provider_usage if row["cost_usd"] is not None
        }
        all_known = (
            len(known_costs) == len(provider_usage)
            and bool(provider_usage)
            and len(cost_statuses) == 1
            and not self.transport_failure_rows
        )
        logical_request_ids = {
            row["logical_request_id"] for row in self.usage_rows
        } | {
            row["logical_request_id"] for row in self.transport_failure_rows
        }
        cache_counters = Counter(
            row["cache_status"] for row in self.usage_rows
        )
        if self.transport_failure_rows:
            cache_counters["unknown"] += len(self.transport_failure_rows)
        self._append(
            "cost_snapshot",
            {
                "scope": self.stage_id,
                "logical_request_count": len(logical_request_ids),
                "physical_attempt_count": (
                    len(provider_usage) + len(self.transport_failure_rows)
                ),
                "prompt_tokens": prompt,
                "completion_tokens": completion,
                "cached_input_tokens": cached,
                "reasoning_tokens": reasoning,
                "total_tokens": prompt + completion,
                "cost_usd": sum(known_costs) if all_known else None,
                "currency": "USD" if all_known else None,
                "cost_status": next(iter(cost_statuses)) if all_known else "unknown",
                "cache_counters": dict(sorted(cache_counters.items())),
            },
        )
        config = campaign["config"]
        return build_stage_receipt(
            workflow_run_id=str(config["workflow_run_id"]),
            component_run_id=str(config["component_run_id"]),
            component_attempt_id=component_attempt_id,
            stage_id=self.stage_id,
            producer=producer,
            work_id=work_id,
            observations=self.rows,
        )


def _semantic_call(
    *,
    client: Any,
    messages: list[dict[str, str]],
    response_format: Mapping[str, Any],
    tag: str,
    validator_id: str,
    parse: Callable[[Any], dict[str, Any]],
    validate: Callable[[dict[str, Any]], Any],
    observations: _StageObservations,
    retry_cap: int,
) -> tuple[Any, Any]:
    correction: str | None = None
    for semantic_attempt in range(1, retry_cap + 2):
        attempt_messages = list(messages)
        if correction is not None:
            attempt_messages.append(
                {
                    "role": "user",
                    "content": (
                        "Your previous response failed the local response contract. "
                        "Return a complete JSON object only. Do not add commentary. "
                        f"Failure category: {correction}."
                    ),
                }
            )
        call_kwargs: dict[str, Any] = {
            "response_format": dict(response_format),
            "tag": f"{tag}.semantic_{semantic_attempt}",
            "bypass_cache": semantic_attempt > 1,
            # The correction changes the sealed request body and therefore
            # creates a new logical request. Its local attempt sequence starts
            # at one; retry lineage is carried by the observation below.
            "semantic_attempt_index": 1,
        }
        if getattr(client, "uses_shared_backend", False):
            call_kwargs["transport_observer"] = observations.transport_observer(
                work_id=tag
            )
        result = client.call(attempt_messages, **call_kwargs)
        observations.response(result=result, work_id=tag)
        errors: list[str] = []
        validation = None
        try:
            parsed = parse(result.parsed_json if result.parsed_json is not None else result.text)
            validation = validate(parsed)
            errors.extend(str(value) for value in getattr(validation, "errors", ()))
        except Exception as exc:
            errors.append(type(exc).__name__)
        passed = not errors and validation is not None
        observations.validation(
            passed=passed,
            validator_id=validator_id,
            subject_ref=tag,
            reason_codes=["exact_local_validation"] if passed else sorted(set(errors)),
            retryable=not passed and semantic_attempt <= retry_cap,
        )
        if passed:
            return result, validation
        if semantic_attempt <= retry_cap:
            observations.retry(
                result=result,
                work_id=tag,
                index=semantic_attempt,
                maximum=retry_cap,
                reason_code="local_validation_failed",
            )
            correction = "local_validation_failed"
            continue
        raise D2LProjectLiveExecutorError(
            f"semantic response failed local validation for {tag}: {errors}"
        )
    raise AssertionError("semantic retry loop did not terminate")


def _b1_stage(
    *,
    campaign: Mapping[str, Any],
    rows: Sequence[Mapping[str, Any]],
    transport: ProjectTransport,
    component_attempt_id: int,
    observations: _StageObservations,
) -> tuple[dict[str, Any], dict[str, Any]]:
    config = campaign["config"]
    role = _role(config, "d2l.candidate_discovery")
    client = transport.build_client(
        role["role_id"], component_attempt_id=component_attempt_id
    )
    source = _source_manifest(campaign=campaign, rows=rows, family="b1")
    aggregate_rows: dict[str, dict[str, set[str]]] = {}
    calls: list[dict[str, Any]] = []
    accepted_observations = 0
    rejected_rows = 0
    warnings = 0
    observations.progress(completed=0, total=len(source["windows"]))
    for call_index, window in enumerate(source["windows"], start=1):
        messages = render_discovery_messages(
            chapter_id=LIVE_SCOPE_ID,
            window_id=window["window_id"],
            source_blocks=window["source_blocks"],
        )
        _, validation = _semantic_call(
            client=client,
            messages=messages,
            response_format=DISCOVERY_RESPONSE_FORMAT,
            tag=f"b1_{window['window_id']}",
            validator_id=DISCOVERY_VALIDATOR_VERSION,
            parse=parse_discovery_json,
            validate=lambda parsed, window=window: validate_discovery_output(
                parsed,
                chapter_id=LIVE_SCOPE_ID,
                window_id=window["window_id"],
                source_blocks=window["source_blocks"],
                source_lineage_id=source["manifest_sha256"],
            ),
            observations=observations,
            retry_cap=int(role["semantic_retry_cap"]),
        )
        normalized = _dataclass_json(validation)
        accepted_observations += len(normalized["observations"])
        rejected_rows += int(normalized["rejected_rows"])
        warnings += len(normalized["warnings"])
        proposals: list[dict[str, Any]] = []
        for row in normalized["observations"]:
            surface = str(row["source_surface"])
            grouped = aggregate_rows.setdefault(
                surface, {"source_block_ids": set(), "window_ids": set()}
            )
            grouped["source_block_ids"].update(row["source_block_ids"])
            grouped["window_ids"].add(window["window_id"])
            proposals.append(
                {
                    "candidate_order": len(proposals) + 1,
                    "observation_id": row["observation_id"],
                    "source_surface": surface,
                    "source_block_ids": list(row["source_block_ids"]),
                    "lifecycle": "proposed",
                    "semantic_authority": "none",
                }
            )
        calls.append(
            {
                "logical_call_id": f"b1_candidate_call_{call_index:06d}",
                "logical_call_index": call_index,
                "chapter_id": window["chapter_id"],
                "window_id": window["window_id"],
                "validation_status": (
                    "valid_with_warnings" if normalized["warnings"] else "valid"
                ),
                "candidate_count": len(proposals),
                "source_block_ids": list(window["block_ids"]),
                "candidates": proposals,
            }
        )
        observations.progress(completed=call_index, total=len(source["windows"]))
    aggregate = {
        "schema_version": "d2l_candidate_discovery_live_v1",
        "source_manifest": source,
        "source_manifest_sha256": source["manifest_sha256"],
        "campaign_config_sha256": config["integrity"]["payload_sha256"],
        "summary": {
            "status": "completed",
            "windows": len(source["windows"]),
            "accepted_window_observations": accepted_observations,
            "unique_surfaces": len(aggregate_rows),
            "repeated_cross_window_observations": (
                accepted_observations - len(aggregate_rows)
            ),
            "rejected_rows": rejected_rows,
            "warnings": warnings,
        },
        "candidates": [
            {
                "source_surface": surface,
                "source_block_ids": sorted(value["source_block_ids"]),
                "window_ids": sorted(value["window_ids"]),
            }
            for surface, value in sorted(
                aggregate_rows.items(), key=lambda item: (item[0].casefold(), item[0])
            )
        ],
        "gold_available_to_runtime": False,
        "semantic_output_authority": "provisional_only",
    }
    aggregate["aggregate_sha256"] = canonical_sha256(aggregate)
    timeline = {
        "schema": "d2l_candidate_proposal_timeline_v1",
        "stage_id": "b1_candidate_discovery",
        "lifecycle": "provisional",
        "semantic_authority": "none",
        "source_manifest_sha256": source["manifest_sha256"],
        "campaign_manifest_sha256": config["integrity"]["payload_sha256"],
        "candidate_aggregate_sha256": aggregate["aggregate_sha256"],
        "summary": {
            "logical_calls": len(calls),
            "accepted_calls": len(calls),
            "calls_with_warnings": sum(
                row["validation_status"] == "valid_with_warnings" for row in calls
            ),
            "provisional_proposals": accepted_observations,
            "unique_surfaces": len(aggregate_rows),
        },
        "calls": calls,
    }
    timeline["timeline_sha256"] = canonical_sha256(timeline)
    return _sealed(aggregate), timeline


def _candidate_index_stage(
    *, campaign: Mapping[str, Any], rows: Sequence[Mapping[str, Any]], component_root: Path
) -> dict[str, Any]:
    aggregate = _load_json(
        component_root / "artifacts/b1_candidate_discovery/candidates.json",
        "B1 candidate aggregate",
    )
    source = aggregate.get("source_manifest")
    if not isinstance(source, Mapping):
        raise D2LProjectLiveExecutorError("B1 aggregate lacks source manifest")
    if not aggregate.get("candidates"):
        raise D2LProjectLiveExecutorError("B1 produced no candidate surfaces")
    index = build_candidate_index(aggregate, source)
    return _sealed(index)


def _b2_index(
    *,
    candidate_index: Mapping[str, Any],
    source: Mapping[str, Any],
    validations: Sequence[tuple[Mapping[str, Any], Any]],
) -> dict[str, Any]:
    candidate_map = {
        str(row["candidate_id"]): row for row in candidate_index["candidates"]
    }
    source_blocks = {
        str(raw[0]): str(raw[1])
        for window in source["windows"]
        for raw in window["source_blocks"]
    }
    decision_rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for packet, validation in validations:
        for decision in validation.decisions:
            if decision.candidate_id in seen:
                raise D2LProjectLiveExecutorError("B2 candidate was decided twice")
            seen.add(decision.candidate_id)
            candidate = candidate_map[decision.candidate_id]
            target_proposals: list[dict[str, Any]] = []
            if decision.decision == "admit":
                target_proposals.append(
                    {
                        "target_vi": decision.primary_target_vi,
                        "applicability": decision.primary_use,
                    }
                )
                target_proposals.extend(
                    {
                        "target_vi": row.target_vi,
                        "applicability": row.use_when,
                    }
                    for row in decision.alternates
                )
            decision_rows.append(
                {
                    "candidate_id": decision.candidate_id,
                    "chapter_id": LIVE_SCOPE_ID,
                    "surfaces": list(candidate["surfaces"]),
                    "decision": decision.decision,
                    "canonical_source": decision.canonical_source,
                    "target_proposals": target_proposals,
                    "directive": decision.directive,
                    "evidence_block_ids": list(decision.evidence_block_ids),
                    "evidence_complete": bool(candidate["evidence_complete"]),
                    "decision_rationale": decision.rationale,
                    "lineage": {
                        "packet_id": packet["packet_id"],
                        "candidate_index_sha256": candidate_index[
                            "candidate_index_sha256"
                        ],
                        "validator_version": b2_contract.VALIDATOR_VERSION,
                    },
                }
            )
    if seen != set(candidate_map):
        raise D2LProjectLiveExecutorError("B2 decisions do not exact-cover candidates")
    payload = {
        "index_version": "d2l_b2_consolidation_index_v1",
        "chapter_ids": [LIVE_SCOPE_ID],
        "source_lineage": [
            {
                "candidate_index_sha256": candidate_index[
                    "candidate_index_sha256"
                ],
                "packet_ids": [str(packet["packet_id"]) for packet, _ in validations],
            }
        ],
        "decisions": sorted(decision_rows, key=_candidate_sort_key),
        "source_blocks": [
            {"block_id": block_id, "text": text}
            for block_id, text in source_blocks.items()
        ],
    }
    payload["counts"] = _index_counts(payload["decisions"])
    payload["index_sha256"] = _sha256_json(payload)
    return payload


def _b2_stage(
    *,
    campaign: Mapping[str, Any],
    component_root: Path,
    transport: ProjectTransport,
    component_attempt_id: int,
    observations: _StageObservations,
) -> dict[str, Any]:
    config = campaign["config"]
    role = _role(config, "d2l.b2.admission")
    candidate_index = _load_json(
        component_root / "artifacts/candidate_index/index.json", "candidate index"
    )
    source = _load_json(
        component_root / "artifacts/b1_candidate_discovery/candidates.json",
        "B1 aggregate",
    )["source_manifest"]
    packet_plan, packets = build_packet_plan(
        candidate_index,
        source,
        caps=PacketCaps(prompt_token_cap=int(role["generation"]["max_input_tokens"])),
    )
    client = transport.build_client(
        role["role_id"], component_attempt_id=component_attempt_id
    )
    validations: list[tuple[Mapping[str, Any], Any]] = []
    packet_records: list[dict[str, Any]] = []
    observations.progress(completed=0, total=len(packet_plan["packets"]))
    for packet_index, packet_row in enumerate(packet_plan["packets"], start=1):
        packet_bundle = packets[str(packet_row["packet_id"])]
        packet = packet_bundle["packet"]
        messages = packet_bundle["messages"]
        _, validation = _semantic_call(
            client=client,
            messages=messages,
            response_format=b2_contract.RESPONSE_FORMAT,
            tag=f"b2_{packet['packet_id']}",
            validator_id=b2_contract.VALIDATOR_VERSION,
            parse=b2_contract.parse_response_json,
            validate=lambda parsed, packet=packet: b2_contract.validate_output(
                parsed, packet=packet
            ),
            observations=observations,
            retry_cap=int(role["semantic_retry_cap"]),
        )
        validations.append((packet, validation))
        packet_records.append(
            {
                "packet_id": packet["packet_id"],
                "candidate_ids": [row["candidate_id"] for row in packet["candidates"]],
                "validation": _dataclass_json(validation),
                "model_packet_sha256": b2_contract.model_packet_sha256(packet),
            }
        )
        observations.progress(
            completed=packet_index,
            total=len(packet_plan["packets"]),
        )
    consolidation_index = _b2_index(
        candidate_index=candidate_index,
        source=source,
        validations=validations,
    )
    return _sealed(
        {
            "schema_version": "d2l_b2_admission_live_v1",
            "candidate_index_sha256": candidate_index["candidate_index_sha256"],
            "packet_plan": packet_plan,
            "packets": packet_records,
            "consolidation_index": consolidation_index,
            "decision_counts": dict(
                sorted(
                    Counter(
                        row["decision"] for row in consolidation_index["decisions"]
                    ).items()
                )
            ),
            "semantic_output_authority": "validated_b2_draft",
            "gold_available_to_runtime": False,
        }
    )


def _augment_morphology_plan(plan: Mapping[str, Any]) -> dict[str, Any]:
    payload = deepcopy(dict(plan))
    payload.pop("plan_sha256", None)
    payload["resolved_reuse"] = []
    payload["deferred_candidate_ids"] = [
        str(row["candidate_id"]) for row in payload["provisional_clean"]
    ]
    payload["source_full_plan_sha256"] = None
    payload["plan_sha256"] = _sha256_json(payload)
    return payload


def _post_morphology_with_pending(
    *, source_index: Mapping[str, Any], plan: Mapping[str, Any], draft: Mapping[str, Any]
) -> dict[str, Any]:
    source_rows = {
        str(row["candidate_id"]): deepcopy(row)
        for row in source_index["decisions"]
    }
    admitted = {
        candidate_id: row
        for candidate_id, row in source_rows.items()
        if row["decision"] == "admit"
    }
    pending_members = {
        str(candidate_id)
        for component in draft["pending_components"]
        for candidate_id in component["member_candidate_ids"]
    }
    current: list[dict[str, Any]] = []
    covered: set[str] = set()
    for entry in draft["audited_entries"]:
        member_ids = {str(value) for value in entry["member_candidate_ids"]}
        covered.update(member_ids)
        current.append(
            _current_entry_from_audit(
                entry=entry,
                members=admitted,
                source_index_sha256=str(source_index["index_sha256"]),
                authority_kind="stage1_live_audit",
                authority_hash=str(draft["draft_sha256"]),
            )
        )
    for candidate_id in plan["deferred_candidate_ids"]:
        covered.add(str(candidate_id))
        current.append(
            _current_entry_from_singleton(
                row=admitted[str(candidate_id)],
                source_index_sha256=str(source_index["index_sha256"]),
            )
        )
    for candidate_id in sorted(pending_members):
        covered.add(candidate_id)
        row = deepcopy(admitted[candidate_id])
        row["decision"] = "review"
        row["decision_rationale"] = "morphology_auditor_pending"
        row["lineage"] = {
            **dict(row.get("lineage") or {}),
            "authority_kind": "stage1_pending",
            "authority_hash": draft["draft_sha256"],
        }
        current.append(row)
    if covered != set(admitted):
        raise D2LProjectLiveExecutorError(
            "morphology output does not exact-cover admitted candidates"
        )
    current.extend(
        deepcopy(row)
        for row in source_rows.values()
        if row["decision"] != "admit"
    )
    payload = {
        "index_version": "d2l_b2_post_morphology_entry_index_v1",
        "chapter_ids": list(source_index["chapter_ids"]),
        "source_index_sha256": source_index["index_sha256"],
        "source_stage1_plan_sha256": plan["plan_sha256"],
        "source_stage1_draft_sha256": draft["draft_sha256"],
        "source_full_plan_sha256": None,
        "source_lineage": deepcopy(source_index["source_lineage"]),
        "decisions": sorted(current, key=_candidate_sort_key),
        "source_blocks": deepcopy(source_index["source_blocks"]),
        "production_publish_allowed": False,
    }
    payload["counts"] = {
        **_index_counts(payload["decisions"]),
        "source_admitted_candidates": len(admitted),
        "morphology_pending_candidates": len(pending_members),
        "source_candidate_exact_cover": len(covered),
    }
    payload["index_sha256"] = _sha256_json(payload)
    return payload


def _morphology_stage(
    *,
    campaign: Mapping[str, Any],
    component_root: Path,
    transport: ProjectTransport,
    component_attempt_id: int,
    observations: _StageObservations,
) -> dict[str, Any]:
    b2 = _load_json(
        component_root / "artifacts/b2_admission_translation/decisions.json",
        "B2 decisions",
    )
    source_index = b2.get("consolidation_index")
    if not isinstance(source_index, Mapping):
        raise D2LProjectLiveExecutorError("B2 artifact lacks consolidation index")
    plan = _augment_morphology_plan(build_morphology_plan(source_index))
    packets, dry = packetize_components(
        plan=plan,
        index=source_index,
        caps=ConsolidationCaps(),
    )
    role = _role(campaign["config"], "d2l.b2.morphology")
    validations: list[tuple[Mapping[str, Any], Any]] = []
    packet_records: list[dict[str, Any]] = []
    total_components = len(plan["components"])
    completed_components = 0
    observations.progress(completed=0, total=total_components)
    if packets:
        client = transport.build_client(
            role["role_id"], component_attempt_id=component_attempt_id
        )
        for packet in packets:
            _, validation = _semantic_call(
                client=client,
                messages=consolidation_contract.render_messages(packet),
                response_format=consolidation_contract.RESPONSE_FORMAT,
                tag=f"morphology_{packet['packet_id']}",
                validator_id=consolidation_contract.VALIDATOR_VERSION,
                parse=consolidation_contract.parse_response_json,
                validate=lambda parsed, packet=packet: consolidation_contract.validate_output(
                    parsed, packet=packet
                ),
                observations=observations,
                retry_cap=int(role["semantic_retry_cap"]),
            )
            validations.append((packet, validation))
            packet_records.append(
                {
                    "packet_id": packet["packet_id"],
                    "validation": _dataclass_json(validation),
                }
            )
            completed_components += len(packet["components"])
            observations.progress(
                completed=completed_components,
                total=total_components,
            )
    draft = build_draft_package(
        index=source_index,
        plan=plan,
        packet_validations=validations,
    )
    post_index = (
        _post_morphology_with_pending(
            source_index=source_index, plan=plan, draft=draft
        )
        if draft["pending_components"]
        else build_post_morphology_index(
            source_index=source_index,
            stage1_plan=plan,
            stage1_draft=draft,
        )
    )
    return _sealed(
        {
            "schema_version": "d2l_morphology_decisions_live_v1",
            "component_plan": plan,
            "dry_packet_summary": dry,
            "packets": packet_records,
            "draft": draft,
            "post_morphology_index": post_index,
            "component_count": len(plan["components"]),
            "pending_component_count": len(draft["pending_components"]),
            "semantic_output_authority": "validated_auditor_draft",
        }
    )


def _resolved_stage2_plan(
    *, current_index: Mapping[str, Any], prior_plan: Mapping[str, Any]
) -> dict[str, Any]:
    admitted = [row for row in current_index["decisions"] if row["decision"] == "admit"]
    clean: list[dict[str, Any]] = []
    multi: list[dict[str, Any]] = []
    for row in admitted:
        projection = {
            "candidate_id": row["candidate_id"],
            "status": "provisional_clean",
            "chapter_id": row["chapter_id"],
            "canonical_source": row["canonical_source"],
            "surfaces": row["surfaces"],
            "target_proposals": row["target_proposals"],
            "directive": row["directive"],
            "evidence_block_ids": row["evidence_block_ids"],
            "lineage": row["lineage"],
        }
        target_keys = {
            str(target["target_vi"]).strip().casefold()
            for target in row["target_proposals"]
        }
        if len(target_keys) > 1:
            multi.append({**projection, "status": "deferred_multi_target"})
        else:
            clean.append(projection)
    review = [row for row in current_index["decisions"] if row["decision"] == "review"]
    rejected = [row for row in current_index["decisions"] if row["decision"] == "reject"]
    payload = {
        "plan_version": "d2l_b2_target_collision_resolved_plan_v1",
        "selection_scope": "stage2_exact_target_collision_resolved",
        "selection_rule": "apply only validated decisions; pending remains nonpublished",
        "stage_status": "complete_no_review_required",
        "auditor_required": False,
        "production_publish_allowed": False,
        "source_index_sha256": current_index["index_sha256"],
        "source_prior_stage2_plan_sha256": prior_plan["plan_sha256"],
        "source_stage2_draft_sha256": None,
        "components": [],
        "provisional_clean": sorted(clean, key=_candidate_sort_key),
        "multi_target_deferred": sorted(multi, key=_candidate_sort_key),
        "pending_admission": [
            {
                "candidate_id": row["candidate_id"],
                "status": "pending_admission",
                "chapter_id": row["chapter_id"],
                "surfaces": row["surfaces"],
                "evidence_block_ids": row["evidence_block_ids"],
                "decision_rationale": row["decision_rationale"],
                "lineage": row["lineage"],
            }
            for row in review
        ],
        "rejected_ledger": [
            {
                "candidate_id": row["candidate_id"],
                "status": "rejected",
                "chapter_id": row["chapter_id"],
                "surfaces": row["surfaces"],
                "evidence_block_ids": row["evidence_block_ids"],
                "decision_rationale": row["decision_rationale"],
                "lineage": row["lineage"],
            }
            for row in rejected
        ],
        "later_stage_frontier": {
            "multi_target": {
                "status": "ready",
                "requires": "sealed_stage2_zero_component_plan",
                "rule": "review only entries carrying multiple target proposals",
            }
        },
        "counts": {
            "current_admitted_entries": len(admitted),
            "target_collision_components": 0,
            "single_target_clean_entries": len(clean),
            "multi_target_deferred_entries": len(multi),
            "pending_admission": len(review),
            "rejected": len(rejected),
        },
    }
    payload["plan_sha256"] = _sha256_json(payload)
    return payload


def _post_target_with_pending(
    *,
    current_index: Mapping[str, Any],
    plan: Mapping[str, Any],
    draft: Mapping[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    source_rows = {
        str(row["candidate_id"]): deepcopy(row)
        for row in current_index["decisions"]
    }
    admitted = {key: row for key, row in source_rows.items() if row["decision"] == "admit"}
    queued = {
        str(member["candidate_id"])
        for component in plan["components"]
        for member in component["members"]
    }
    pending = {
        str(candidate_id)
        for component in draft["pending_components"]
        for candidate_id in component["member_candidate_ids"]
    }
    resolved: list[dict[str, Any]] = []
    covered: set[str] = set()
    for entry in draft["audited_entries"]:
        current = _current_entry_from_audit(
            entry=entry,
            members=admitted,
            source_index_sha256=str(current_index["index_sha256"]),
            authority_kind="stage2_target_collision_audit",
            authority_hash=str(draft["draft_sha256"]),
        )
        member_ids = [str(value) for value in current["source_member_candidate_ids"]]
        root_ids = sorted(
            {
                str(value)
                for member_id in member_ids
                for value in (
                    admitted[member_id].get("source_member_candidate_ids")
                    or [member_id]
                )
            }
        )
        current["source_member_candidate_ids"] = root_ids
        current["lineage"] = {
            "authority_kind": "stage2_target_collision_audit",
            "authority_hash": draft["draft_sha256"],
            "source_index_sha256": current_index["index_sha256"],
            "stage2_member_entry_ids": member_ids,
            "source_member_candidate_ids": root_ids,
        }
        covered.update(member_ids)
        resolved.append(current)
    for candidate_id in sorted(pending):
        covered.add(candidate_id)
        row = deepcopy(admitted[candidate_id])
        row["decision"] = "review"
        row["decision_rationale"] = "target_collision_auditor_pending"
        resolved.append(row)
    carried = set(admitted) - queued
    covered.update(carried)
    resolved.extend(deepcopy(admitted[value]) for value in carried)
    if covered != set(admitted):
        raise D2LProjectLiveExecutorError(
            "target-collision output does not exact-cover current entries"
        )
    resolved.extend(
        deepcopy(row)
        for row in source_rows.values()
        if row["decision"] != "admit"
    )
    payload = {
        "index_version": "d2l_b2_post_target_collision_entry_index_v1",
        "chapter_ids": list(current_index["chapter_ids"]),
        "source_index_sha256": current_index["index_sha256"],
        "source_stage2_plan_sha256": plan["plan_sha256"],
        "source_stage2_draft_sha256": draft["draft_sha256"],
        "source_lineage": deepcopy(current_index["source_lineage"]),
        "decisions": sorted(resolved, key=_candidate_sort_key),
        "source_blocks": deepcopy(current_index["source_blocks"]),
        "production_publish_allowed": False,
    }
    payload["counts"] = _index_counts(payload["decisions"])
    payload["index_sha256"] = _sha256_json(payload)
    return payload, _resolved_stage2_plan(current_index=payload, prior_plan=plan)


def _target_collision_stage(
    *,
    campaign: Mapping[str, Any],
    component_root: Path,
    transport: ProjectTransport,
    component_attempt_id: int,
    observations: _StageObservations,
) -> dict[str, Any]:
    morphology = _load_json(
        component_root / "artifacts/auditor_morphology/decisions.json",
        "morphology decisions",
    )
    current_index = morphology["post_morphology_index"]
    plan = build_target_collision_plan(current_index)
    packets, dry = packetize_components(
        plan=plan,
        index=current_index,
        caps=ConsolidationCaps(),
    )
    role = _role(campaign["config"], "d2l.b2.target_collision")
    validations: list[tuple[Mapping[str, Any], Any]] = []
    records: list[dict[str, Any]] = []
    total_components = len(plan["components"])
    completed_components = 0
    observations.progress(completed=0, total=total_components)
    if packets:
        client = transport.build_client(
            role["role_id"], component_attempt_id=component_attempt_id
        )
        for packet in packets:
            _, validation = _semantic_call(
                client=client,
                messages=consolidation_contract.render_messages(packet),
                response_format=consolidation_contract.RESPONSE_FORMAT,
                tag=f"target_collision_{packet['packet_id']}",
                validator_id=consolidation_contract.VALIDATOR_VERSION,
                parse=consolidation_contract.parse_response_json,
                validate=lambda parsed, packet=packet: consolidation_contract.validate_output(
                    parsed, packet=packet
                ),
                observations=observations,
                retry_cap=int(role["semantic_retry_cap"]),
            )
            validations.append((packet, validation))
            records.append(
                {"packet_id": packet["packet_id"], "validation": _dataclass_json(validation)}
            )
            completed_components += len(packet["components"])
            observations.progress(
                completed=completed_components,
                total=total_components,
            )
    if packets:
        draft = build_draft_package(
            index=current_index, plan=plan, packet_validations=validations
        )
        if draft["pending_components"]:
            resolved_index, resolved_plan = _post_target_with_pending(
                current_index=current_index, plan=plan, draft=draft
            )
        else:
            resolved_index, resolved_plan = apply_target_collision_audit(
                current_index=current_index,
                stage2_plan=plan,
                stage2_draft=draft,
            )
    else:
        draft = None
        resolved_index = current_index
        resolved_plan = plan
    return _sealed(
        {
            "schema_version": "d2l_target_collision_decisions_live_v1",
            "component_plan": plan,
            "dry_packet_summary": dry,
            "packets": records,
            "draft": draft,
            "resolved_index": resolved_index,
            "resolved_stage2_plan": resolved_plan,
            "component_count": len(plan["components"]),
            "pending_component_count": (
                0 if draft is None else len(draft["pending_components"])
            ),
            "semantic_output_authority": "validated_auditor_draft",
        }
    )


def _multi_target_draft(
    *,
    plan: Mapping[str, Any],
    validations: Sequence[tuple[Mapping[str, Any], Any]],
) -> dict[str, Any]:
    review_by_id = {str(row["candidate_id"]): row for row in plan["review_items"]}
    audited: list[dict[str, Any]] = []
    pending: list[dict[str, Any]] = []
    seen: set[str] = set()
    for packet, validation in validations:
        for decision in validation.decisions:
            if decision.candidate_id in seen:
                raise D2LProjectLiveExecutorError("multi-target item was decided twice")
            seen.add(decision.candidate_id)
            source = review_by_id[decision.candidate_id]
            if decision.action == "pending":
                pending.append(
                    {
                        "candidate_id": decision.candidate_id,
                        "status": "pending_multi_target",
                        "canonical_source": source["canonical_source"],
                        "target_proposals": source["target_proposals"],
                        "evidence_block_ids": source["evidence_block_ids"],
                        "auditor_cited_evidence_block_ids": list(
                            decision.evidence_block_ids
                        ),
                        "pending_reason": decision.pending_reason,
                    }
                )
                continue
            canonical = next(
                row
                for row in decision.target_dispositions
                if row.disposition == "canonical"
            )
            audited.append(
                {
                    "candidate_id": decision.candidate_id,
                    "status": "audited_draft",
                    "canonical_source": source["canonical_source"],
                    "surfaces": source["surfaces"],
                    "canonical_target_vi": canonical.target_vi,
                    "alternative_targets": [
                        {
                            "target_vi": row.target_vi,
                            "applicability": row.applicability,
                        }
                        for row in decision.target_dispositions
                        if row.disposition == "alternative"
                    ],
                    "rejected_target_proposals": [
                        row.target_vi
                        for row in decision.target_dispositions
                        if row.disposition == "reject"
                    ],
                    "directive": source["directive"],
                    "evidence_block_ids": source["evidence_block_ids"],
                    "auditor_cited_evidence_block_ids": list(
                        decision.evidence_block_ids
                    ),
                    "rationale": decision.rationale,
                }
            )
    if seen != set(review_by_id):
        raise D2LProjectLiveExecutorError(
            "multi-target decisions do not exact-cover review items"
        )
    draft = {
        "draft_version": "d2l_b2_multi_target_audited_draft_v1",
        "production_published": False,
        "source_plan_sha256": plan["plan_sha256"],
        "audited_entries": sorted(audited, key=lambda row: row["candidate_id"]),
        "pending_entries": sorted(pending, key=lambda row: row["candidate_id"]),
        "provisional_clean": deepcopy(plan["provisional_clean"]),
        "pending_admission": deepcopy(plan["pending_admission"]),
        "rejected_ledger": deepcopy(plan["rejected_ledger"]),
    }
    draft["counts"] = {
        "audited_entries": len(audited),
        "pending_multi_target": len(pending),
        "provisional_clean": len(plan["provisional_clean"]),
        "pending_admission": len(plan["pending_admission"]),
        "rejected": len(plan["rejected_ledger"]),
    }
    draft["draft_sha256"] = _sha256_json(draft)
    return draft


def _multi_target_stage(
    *,
    campaign: Mapping[str, Any],
    component_root: Path,
    transport: ProjectTransport,
    component_attempt_id: int,
    observations: _StageObservations,
) -> dict[str, Any]:
    target = _load_json(
        component_root / "artifacts/auditor_target_collision/decisions.json",
        "target-collision decisions",
    )
    current_index = target["resolved_index"]
    stage2_plan = target["resolved_stage2_plan"]
    plan = build_multi_target_plan(
        current_index=current_index, stage2_plan=stage2_plan
    )
    packets, dry = packetize_multi_target_items(
        plan=plan, index=current_index, caps=MultiTargetCaps()
    )
    role = _role(campaign["config"], "d2l.b2.multi_target")
    validations: list[tuple[Mapping[str, Any], Any]] = []
    records: list[dict[str, Any]] = []
    total_components = len(plan["review_items"])
    completed_components = 0
    observations.progress(completed=0, total=total_components)
    if packets:
        client = transport.build_client(
            role["role_id"], component_attempt_id=component_attempt_id
        )
        for packet in packets:
            _, validation = _semantic_call(
                client=client,
                messages=multi_target_contract.render_messages(packet),
                response_format=multi_target_contract.RESPONSE_FORMAT,
                tag=f"multi_target_{packet['packet_id']}",
                validator_id=multi_target_contract.VALIDATOR_VERSION,
                parse=multi_target_contract.parse_response_json,
                validate=lambda parsed, packet=packet: multi_target_contract.validate_output(
                    parsed, packet=packet
                ),
                observations=observations,
                retry_cap=int(role["semantic_retry_cap"]),
            )
            validations.append((packet, validation))
            records.append(
                {"packet_id": packet["packet_id"], "validation": _dataclass_json(validation)}
            )
            completed_components += len(packet["review_items"])
            observations.progress(
                completed=completed_components,
                total=total_components,
            )
    draft = _multi_target_draft(plan=plan, validations=validations)
    return _sealed(
        {
            "schema_version": "d2l_multi_target_decisions_live_v1",
            "multi_target_plan": plan,
            "dry_packet_summary": dry,
            "packets": records,
            "glossary_draft": draft,
            "component_count": len(plan["review_items"]),
            "pending_entry_count": len(draft["pending_entries"]),
            "semantic_output_authority": "validated_auditor_draft",
        }
    )


def _glossary_ready_entry(
    *,
    row: Mapping[str, Any],
    source_row: Mapping[str, Any],
    audited: bool,
) -> dict[str, Any]:
    proposals = list(source_row.get("target_proposals") or [])
    if audited:
        canonical_target = str(row["canonical_target_vi"])
        alternatives = [dict(value) for value in row["alternative_targets"]]
        rejected = list(row["rejected_target_proposals"])
        rationale = str(row["rationale"])
        cited = list(row["auditor_cited_evidence_block_ids"])
        authority_kind = "stage3_multi_target_audit"
    else:
        if len(proposals) != 1:
            raise D2LProjectLiveExecutorError(
                "a provisional-clean glossary row must have one target"
            )
        canonical_target = str(proposals[0]["target_vi"])
        alternatives = []
        rejected = []
        rationale = str(
            source_row.get("decision_rationale")
            or "Validated single-target terminology decision."
        )
        cited = []
        authority_kind = "deterministic_single_target"
    canonical_applicability = next(
        (
            value.get("applicability")
            for value in proposals
            if str(value.get("target_vi")) == canonical_target
        ),
        None,
    )
    lineage = dict(source_row.get("lineage") or {})
    authority_sha256 = str(
        row.get("draft_sha256")
        or lineage.get("authority_hash")
        or lineage.get("source_index_sha256")
        or ""
    )
    if not re.fullmatch(r"[0-9A-Fa-f]{64}", authority_sha256):
        raise D2LProjectLiveExecutorError(
            "glossary entry lacks a valid semantic authority hash"
        )
    member_ids = list(
        source_row.get("source_member_candidate_ids")
        or lineage.get("source_member_candidate_ids")
        or [source_row["candidate_id"]]
    )
    return {
        "entry_id": str(source_row["candidate_id"]),
        "canonical_source": str(source_row["canonical_source"]),
        "canonical_target_vi": canonical_target,
        "alternative_targets": alternatives,
        "surfaces": list(source_row["surfaces"]),
        "chapter_id": str(source_row["chapter_id"]),
        "status": "ready_draft",
        "directive": str(source_row["directive"]),
        "canonical_applicability": canonical_applicability,
        "evidence_block_ids": list(source_row["evidence_block_ids"]),
        "evidence_complete": bool(source_row.get("evidence_complete", False)),
        "source_member_candidate_ids": member_ids,
        "decision_rationale": rationale,
        "pending_target_proposals": [],
        "rejected_target_proposals": rejected,
        "resolution": {
            "authority_kind": authority_kind,
            "authority_sha256": authority_sha256.upper(),
            "packet_id": row.get("packet_id"),
            "auditor_rationale": rationale if audited else None,
            "auditor_cited_evidence_block_ids": cited,
            "pending_reason": None,
        },
        "source_lineage": lineage,
    }


def _final_glossary_draft(
    *, campaign: Mapping[str, Any], multi: Mapping[str, Any], target: Mapping[str, Any]
) -> dict[str, Any]:
    plan = multi["multi_target_plan"]
    audit = multi["glossary_draft"]
    current_rows = {
        str(row["candidate_id"]): row for row in target["resolved_index"]["decisions"]
    }
    ready: list[dict[str, Any]] = []
    for row in plan["provisional_clean"]:
        candidate_id = str(row["candidate_id"])
        ready.append(
            _glossary_ready_entry(
                row=row,
                source_row=current_rows[candidate_id],
                audited=False,
            )
        )
    for row in audit["audited_entries"]:
        candidate_id = str(row["candidate_id"])
        ready.append(
            _glossary_ready_entry(
                row=row,
                source_row=current_rows[candidate_id],
                audited=True,
            )
        )
    pending = [
        *deepcopy(list(plan.get("pending_admission") or [])),
        *deepcopy(list(audit.get("pending_entries") or [])),
    ]
    draft = {
        "draft_version": "d2l_b2_glossary_draft_v2",
        "chapter_ids": list(campaign["config"]["selected_chapter_ids"]),
        "source_index_sha256": target["resolved_index"]["index_sha256"],
        "source_multi_target_draft_sha256": audit["draft_sha256"],
        "source_multi_target_plan_sha256": plan["plan_sha256"],
        "source_run_manifest_sha256": campaign["config"]["integrity"]["payload_sha256"],
        "source_stage2_plan_sha256": target["resolved_stage2_plan"]["plan_sha256"],
        "ready_entries": sorted(
            ready,
            key=lambda value: (
                str(value["canonical_source"]).casefold(),
                str(value["entry_id"]),
            ),
        ),
        "pending_entries": pending,
        "production_published": False,
        "counts": {
            "ready_entries": len(ready),
            "pending_entries": len(pending),
            "admitted_exact_cover": len(ready) + len(pending),
            "deterministic_single_target_entries": len(plan["provisional_clean"]),
            "multi_target_audited_entries": len(audit["audited_entries"]),
            "current_admitted_entries": len(ready) + len(pending),
        },
    }
    draft["draft_sha256"] = canonical_sha256(draft)
    return draft


def _install_committed_glossary(
    *, db: sqlite3.Connection, glossary: Mapping[str, Any]
) -> None:
    document_rows = db.execute("SELECT doc_id FROM documents ORDER BY doc_id").fetchall()
    if len(document_rows) != 1:
        raise D2LProjectLiveExecutorError("work database must contain one document")
    doc_id = str(document_rows[0]["doc_id"])
    for record in glossary["records"]:
        value = record["value"]
        alternatives = [
            str(row["target_vi"]) for row in value["alternative_targets"]
        ]
        evidence = list(value["evidence_block_ids"])
        source = str(value["canonical_source"])
        occurrence_count = 0
        for block_id in evidence:
            block = db.execute(
                "SELECT text FROM blocks WHERE block_id = ? AND doc_id = ?",
                (block_id, doc_id),
            ).fetchone()
            if block is None:
                raise D2LProjectLiveExecutorError(
                    f"glossary evidence block is absent from work DB: {block_id}"
                )
            occurrence_count += str(block["text"] or "").casefold().count(
                source.casefold()
            )
        expected = {
            "doc_id": doc_id,
            "source_term": source,
            "target_term": str(value["canonical_target_vi"]),
            "do_not_translate": int(value["directive"] == "preserve"),
            "allowed_variants_json": json.dumps(
                alternatives, ensure_ascii=False, sort_keys=True
            ),
            "evidence_span_ids_json": json.dumps(
                evidence, ensure_ascii=False, sort_keys=True
            ),
            "status": "confirmed",
            "occurrences_count": occurrence_count,
            "last_block_id": evidence[-1] if evidence else None,
        }
        existing = db.execute(
            """
            SELECT doc_id, source_term, target_term, do_not_translate,
                   allowed_variants_json, evidence_span_ids_json, status,
                   occurrences_count, last_block_id
            FROM glossary_entries WHERE glossary_id = ?
            """,
            (record["record_id"],),
        ).fetchone()
        if existing is not None:
            if dict(existing) != expected:
                raise D2LProjectLiveExecutorError(
                    f"work DB glossary collision: {record['record_id']}"
                )
            continue
        db.execute(
            """
            INSERT INTO glossary_entries (
              glossary_id, doc_id, source_term, target_term, term_type, scope,
              chapter_id, do_not_translate, case_sensitive,
              allowed_variants_json, evidence_span_ids_json, status,
              occurrences_count, last_block_id
            ) VALUES (?, ?, ?, ?, 'term', 'global', NULL, ?, 0, ?, ?, ?, ?, ?)
            """,
            (
                record["record_id"],
                expected["doc_id"],
                expected["source_term"],
                expected["target_term"],
                expected["do_not_translate"],
                expected["allowed_variants_json"],
                expected["evidence_span_ids_json"],
                expected["status"],
                expected["occurrences_count"],
                expected["last_block_id"],
            ),
        )
    db.commit()


def _glossary_stage(
    *,
    campaign: Mapping[str, Any],
    component_root: Path,
    work_db: Path,
    component_attempt_id: int,
) -> tuple[dict[str, Any], dict[str, Any]]:
    multi = _load_json(
        component_root / "artifacts/auditor_multi_target/decisions.json",
        "multi-target decisions",
    )
    target = _load_json(
        component_root / "artifacts/auditor_target_collision/decisions.json",
        "target-collision decisions",
    )
    draft = _final_glossary_draft(campaign=campaign, multi=multi, target=target)
    config = campaign["config"]
    commit_root = component_root / "state/glossary_commit"
    committed = commit_glossary_draft(
        draft=draft,
        output_root=commit_root,
        workflow_run_id=str(config["workflow_run_id"]),
        component_run_id=str(config["component_run_id"]),
        component_attempt_id=component_attempt_id,
        stage_id="glossary_seal",
        source_refs=["art_auditor_multi_target"],
        created_at=_utc_now(),
    )
    db = sqlite3.connect(work_db)
    db.row_factory = sqlite3.Row
    try:
        _install_committed_glossary(db=db, glossary=committed["sealed_glossary"])
    finally:
        db.close()
    return committed["sealed_glossary"], committed["memory_delta_batch"]


class _RecordingClient:
    def __init__(self, client: Any, observations: _StageObservations) -> None:
        self._client = client
        self._observations = observations
        self.results: list[tuple[str, Any]] = []

    def __getattr__(self, name: str) -> Any:
        return getattr(self._client, name)

    def call(self, *args: Any, **kwargs: Any) -> Any:
        tag = str(kwargs.get("tag") or "translator_request")
        # Translator corrections change the request body. Shared Backend binds
        # logical identity to those bytes, so each corrected request starts its
        # own semantic-attempt sequence at one.
        kwargs["semantic_attempt_index"] = 1
        if getattr(self._client, "uses_shared_backend", False):
            kwargs["transport_observer"] = self._observations.transport_observer(
                work_id=tag
            )
        result = self._client.call(*args, **kwargs)
        self._observations.response(result=result, work_id=tag)
        self.results.append((tag, result))
        return result


def _translator_windows(
    *, campaign: Mapping[str, Any], rows: Sequence[Mapping[str, Any]]
) -> list[Window]:
    return [
        Window(
            window_id=str(row["window_id"]),
            block_ids=list(row["block_ids"]),
            est_src_tokens=int(row["estimated_source_tokens"]),
        )
        for row in _windows(campaign=campaign, rows=rows, family="translator")
    ]


def _evaluation_source_binding(project: Any) -> dict[str, Any]:
    inputs = project.projection.get("inputs")
    policy = project.projection.get("policy")
    integrity = project.projection.get("integrity")
    if not isinstance(inputs, Mapping) or not isinstance(policy, Mapping):
        raise D2LProjectLiveExecutorError("admitted projection lacks source identities")
    if not isinstance(integrity, Mapping):
        raise D2LProjectLiveExecutorError("admitted projection lacks integrity")
    policy_sha = policy.get("policy_sha256") or canonical_sha256(policy)
    return {
        "binding_kind": "canonical_source_package_v1",
        "project_id": project.manifest["project_id"],
        "document_id": project.manifest["document_doc_id"],
        "document": {
            "schema_version": str(inputs["document"]["schema_version"]),
            "sha256": str(inputs["document"]["sha256"]).lower(),
        },
        "structure": {
            "schema_version": str(inputs["structure"]["schema_version"]),
            "sha256": str(inputs["structure"]["sha256"]).lower(),
        },
        "asset_manifest": {
            "schema_version": str(inputs["asset_manifest"]["schema_version"]),
            "sha256": str(inputs["asset_manifest"]["sha256"]).lower(),
        },
        "admitted_projection": {
            "schema_version": str(project.projection["schema_version"]),
            "payload_sha256": str(integrity["payload_sha256"]).lower(),
        },
        "admission_policy": {
            "policy_id": str(policy["policy_id"]),
            "policy_version": str(policy["policy_version"]),
            "policy_sha256": str(policy_sha).lower(),
        },
    }


def _live_translation_artifact(
    *,
    campaign: Mapping[str, Any],
    project: Any,
    rows: Sequence[Mapping[str, Any]],
    db: sqlite3.Connection,
    arm_id: str,
    experiment_id: str,
    component_attempt_id: int,
) -> dict[str, Any]:
    translated_rows = {
        str(row["block_id"]): str(row["output_text"])
        for row in db.execute(
            """
            SELECT block_id, output_text FROM translation_runs
            WHERE experiment_id = ? AND config = ? AND stage = 'draft'
            """,
            (experiment_id, arm_id.upper()),
        ).fetchall()
    }
    translations: list[dict[str, Any]] = []
    for row in rows:
        block_id = str(row["block_id"])
        channel = str(row["channel"])
        if channel in {"semantic_text", "structured_translate"}:
            target = translated_rows.get(block_id)
            status = "translated" if target is not None else "failed"
            error = None if target is not None else "translator_window_failed"
        elif channel == "preserve_only":
            status = "preserved"
            target = str(row["source_text"])
            error = None
        elif channel == "review_required":
            status = "review_held"
            target = None
            error = None
        else:
            raise D2LProjectLiveExecutorError(f"unsupported channel: {channel}")
        translations.append(
            {
                "block_id": block_id,
                "status": status,
                "target_text": target,
                "error_code": error,
            }
        )
    counts = Counter(row["status"] for row in translations)
    config = campaign["config"]
    role = _role(config, f"d2l.translator.{arm_id}")
    payload = {
        "schema_id": "TranslationArtifactV1",
        "schema_version": "1.0.0",
        "artifact_id": f"{config['component_run_id']}_{arm_id}_a{component_attempt_id:04d}",
        "created_at": _utc_now(),
        "producer": {
            "workstream": "d2l",
            "component": "d2l_project_live_executor",
            "component_version": LIVE_EXECUTOR_VERSION,
            "code_commit": config["code_revision"],
        },
        "source_binding": _evaluation_source_binding(project),
        "run_identity": {
            "logical_run_id": config["component_run_id"],
            "attempt_run_id": f"{config['component_run_id']}_a{component_attempt_id}_{arm_id}",
            "arm_id": arm_id,
            "profile_id": LIVE_PROFILE_ID,
            "profile_config_sha256": str(role["semantic_role_sha256"]).lower(),
            "source_language": "en",
            "target_language": "vi",
        },
        "translations": translations,
        "coverage": {
            "source_block_count": len(translations),
            "eligible_count": counts["translated"] + counts["missing"] + counts["failed"],
            "translated_count": counts["translated"],
            "preserved_count": counts["preserved"],
            "excluded_count": counts["excluded"],
            "review_held_count": counts["review_held"],
            "missing_count": counts["missing"],
            "failed_count": counts["failed"],
        },
        "integrity": {"artifact_sha256": "0" * 64},
    }
    sealed = seal_translation_artifact(payload)
    validate_translation_artifact(sealed)
    return sealed


def _translator_stage(
    *,
    campaign: Mapping[str, Any],
    project: Any,
    rows: Sequence[Mapping[str, Any]],
    work_db: Path,
    transport: ProjectTransport,
    component_attempt_id: int,
    observations: _StageObservations,
) -> tuple[dict[str, Any], dict[str, Any]]:
    db = sqlite3.connect(work_db)
    db.row_factory = sqlite3.Row
    windows = _translator_windows(campaign=campaign, rows=rows)
    artifacts: dict[str, dict[str, Any]] = {}
    total_work = len(rows) * 2
    observations.progress(completed=0, total=total_work)
    try:
        for arm_index, arm_id in enumerate(("s0", "s1"), start=1):
            role = _role(campaign["config"], f"d2l.translator.{arm_id}")
            client = _RecordingClient(
                transport.build_client(
                    role["role_id"], component_attempt_id=component_attempt_id
                ),
                observations,
            )
            experiment_id = (
                f"{campaign['config']['component_run_id']}_{arm_id}_"
                f"a{component_attempt_id:04d}"
            )
            extra = dict(role.get("extra_policy") or {})
            report = translate_windows(
                db,
                windows,
                client,
                experiment_id=experiment_id,
                config=arm_id,
                context_budget_tokens=1500,
                profile_name="technical_d2l_v1",
                protected_spans_policy=extra.get("protected_spans_policy"),
                translation_output_policy=extra.get("translation_output_policy"),
                response_envelope_policy=extra.get("response_envelope_policy"),
            )
            for window_report in report.reports:
                observations.validation(
                    passed=window_report.status in {"translated", "skipped"},
                    validator_id=str(role["validator_id"]),
                    subject_ref=f"{arm_id}:{window_report.window_id}",
                    reason_codes=(
                        ["exact_local_validation"]
                        if window_report.status in {"translated", "skipped"}
                        else list(window_report.errors or ["translation_failed"])
                    ),
                    retryable=False,
                )
            artifacts[arm_id] = _live_translation_artifact(
                campaign=campaign,
                project=project,
                rows=rows,
                db=db,
                arm_id=arm_id,
                experiment_id=experiment_id,
                component_attempt_id=component_attempt_id,
            )
            observations.progress(
                completed=arm_index * len(rows),
                total=total_work,
            )
    finally:
        db.close()
    return artifacts["s0"], artifacts["s1"]


def _mechanical_quality(
    *,
    block_id: str,
    source: str,
    target: str,
    allow_unchanged: bool = False,
) -> tuple[bool, list[str]]:
    findings = inspect_translations(
        [
            {
                "block_id": block_id,
                "block_type": "prose",
                "source_text": source,
                "clean_text": source,
            }
        ],
        {block_id: target},
    )
    reasons = sorted(
        {
            row.issue_type
            for row in findings
            if row.severity == "major"
            and not (
                allow_unchanged
                and row.issue_type
                in {"target_equals_source", "untranslated_heading"}
            )
        }
    )
    return not reasons, sorted(set(reasons))


def _exact_evidence(value: str, *, limit: int = 160) -> str:
    if len(value) <= limit:
        return value
    return value[:limit]


def _translator_experiment_id(
    campaign: Mapping[str, Any],
    *,
    arm_id: str,
    component_attempt_id: int,
) -> str:
    return (
        f"{campaign['config']['component_run_id']}_{arm_id}_"
        f"a{component_attempt_id:04d}"
    )


def _translation_artifact_component_attempt_id(
    artifact: Mapping[str, Any],
    *,
    arm_id: str,
) -> int:
    artifact_id = str(artifact.get("artifact_id") or "")
    match = re.fullmatch(r".+_" + re.escape(arm_id) + r"_a(\d{4})", artifact_id)
    if match is None:
        raise D2LProjectLiveExecutorError(
            f"{arm_id} draft artifact does not expose component attempt lineage"
        )
    return int(match.group(1))


def _semantic_repair_window(
    *,
    campaign: Mapping[str, Any],
    arm_id: str,
    original_window_id: str,
    original_block_ids: Sequence[str],
    output_block_ids: Sequence[str],
    findings_by_block: Mapping[str, list[dict[str, str]]],
    source_by_id: Mapping[str, Mapping[str, Any]],
    current_translations: Mapping[str, Mapping[str, Any]],
    attempt_state: Mapping[str, Any],
    transport: ProjectTransport,
    component_attempt_id: int,
    observations: _StageObservations,
) -> tuple[dict[str, str], dict[str, Any]]:
    role = _role(
        campaign["config"], f"d2l.translator.{arm_id}.semantic_repair"
    )
    client = transport.build_client(
        role["role_id"], component_attempt_id=component_attempt_id
    )
    ordered_context_ids = [str(value) for value in original_block_ids]
    ordered_output_ids = [str(value) for value in output_block_ids]
    active_findings = [
        finding
        for block_id in ordered_output_ids
        for finding in findings_by_block[block_id]
    ]
    tag = f"semantic_repair_{arm_id}_{original_window_id}"
    try:
        plan = repair_contract.build_plan(
            window_id=original_window_id,
            arm_id=arm_id,
            source_blocks=[
                source_by_id[block_id] for block_id in ordered_context_ids
            ],
            current_translations=current_translations,
            output_block_ids=ordered_output_ids,
            active_semantic_findings=active_findings,
            resolved_integrity_history=list(attempt_state["retry_history"]),
            original_context_pack=attempt_state["context_pack"],
        )
        _, validation = _semantic_call(
            client=client,
            messages=repair_contract.render_messages(plan),
            response_format=repair_contract.RESPONSE_SCHEMA,
            tag=tag,
            validator_id=repair_contract.LOCAL_VALIDATOR_ID,
            parse=repair_contract.parse_response,
            validate=lambda parsed: repair_contract.validate_and_restore(
                parsed, plan
            ),
            observations=observations,
            retry_cap=0,
        )
    except (
        D2LProjectLiveExecutorError,
        repair_contract.SemanticRepairContractError,
    ) as exc:
        return {}, {
            "status": "repair_failed",
            "repair_request_id": tag,
            "block_ids": ordered_output_ids,
            "calls": 0
            if isinstance(exc, repair_contract.SemanticRepairContractError)
            else 1,
            "errors": [str(exc)],
            "mechanical_retry_consumed": bool(attempt_state["retry_consumed"]),
        }
    updates = {
        str(block_id): str(value)
        for block_id, value in validation["updates"].items()
    }
    return updates, {
        "status": "repair_applied_unverified_semantically",
        "repair_request_id": tag,
        "block_ids": ordered_output_ids,
        "calls": 1,
        "errors": [],
        "mechanical_retry_consumed": bool(attempt_state["retry_consumed"]),
        "original_context_pack_sha256": plan.packet["translator_context"][
            "context_pack_sha256"
        ],
        "resolved_integrity_history_count": len(
            plan.packet["resolved_integrity_history"]
        ),
    }


def _final_translation_artifact(
    *,
    draft: Mapping[str, Any],
    updates: Mapping[str, str],
    campaign: Mapping[str, Any],
    arm_id: str,
    component_attempt_id: int,
) -> dict[str, Any]:
    payload = deepcopy(dict(draft))
    payload["artifact_id"] = (
        f"{campaign['config']['component_run_id']}_{arm_id}_quality_final_"
        f"a{component_attempt_id:04d}"
    )
    payload["created_at"] = _utc_now()
    payload["run_identity"] = {
        **dict(payload["run_identity"]),
        "attempt_run_id": (
            f"{campaign['config']['component_run_id']}_a"
            f"{component_attempt_id}_{arm_id}_quality_final"
        ),
    }
    translations: list[dict[str, Any]] = []
    for raw in payload["translations"]:
        row = dict(raw)
        block_id = str(row["block_id"])
        if block_id in updates:
            row["status"] = "translated"
            row["target_text"] = str(updates[block_id])
            row["error_code"] = None
        translations.append(row)
    payload["translations"] = translations
    counts = Counter(row["status"] for row in translations)
    payload["coverage"] = {
        "source_block_count": len(translations),
        "eligible_count": counts["translated"] + counts["missing"] + counts["failed"],
        "translated_count": counts["translated"],
        "preserved_count": counts["preserved"],
        "excluded_count": counts["excluded"],
        "review_held_count": counts["review_held"],
        "missing_count": counts["missing"],
        "failed_count": counts["failed"],
    }
    payload["integrity"] = {"artifact_sha256": "0" * 64}
    sealed = seal_translation_artifact(payload)
    validate_translation_artifact(sealed)
    return sealed


def _quality_stage(
    *,
    campaign: Mapping[str, Any],
    rows: Sequence[Mapping[str, Any]],
    component_root: Path,
    work_db: Path,
    transport: ProjectTransport,
    component_attempt_id: int,
    observations: _StageObservations,
) -> tuple[
    dict[str, Any],
    dict[str, Any],
    dict[str, Any],
    dict[str, Any],
]:
    role = _role(campaign["config"], "d2l.translator.quality_auditor")
    client = transport.build_client(
        role["role_id"], component_attempt_id=component_attempt_id
    )
    row_map = {str(row["block_id"]): dict(row) for row in rows}
    windows = _windows(campaign=campaign, rows=rows, family="translator")
    db = sqlite3.connect(work_db)
    db.row_factory = sqlite3.Row
    arm_reports: dict[str, Any] = {}
    state_rows: list[dict[str, Any]] = []
    final_artifacts: dict[str, dict[str, Any]] = {}
    total_work = len(windows) * 2
    completed_work = 0
    observations.progress(completed=0, total=total_work)
    try:
        for arm_id in ("s0", "s1"):
            draft_artifact = _load_json(
                component_root / f"artifacts/translator/{arm_id}.json",
                f"{arm_id} translation artifact",
            )
            translations = {
                str(row["block_id"]): dict(row)
                for row in draft_artifact["translations"]
            }
            attempt_state = load_window_attempt_state(
                db,
                experiment_id=_translator_experiment_id(
                    campaign,
                    arm_id=arm_id,
                    component_attempt_id=_translation_artifact_component_attempt_id(
                        draft_artifact,
                        arm_id=arm_id,
                    ),
                ),
            )
            audited_ids: list[str] = []
            findings: list[dict[str, str]] = []
            repair_rows: list[dict[str, Any]] = []
            repair_updates: dict[str, str] = {}
            llm_packets = 0
            deterministic_issue_blocks = 0
            protected_finding_suppressed_count = 0
            for window in windows:
                safe_blocks: list[dict[str, Any]] = []
                protected_segments_by_block: dict[str, list[str]] = {}
                for block_id in window["block_ids"]:
                    source_row = row_map[block_id]
                    translated = translations[block_id]
                    audited_ids.append(block_id)
                    source = _source_text(source_row)
                    target = translated.get("target_text")
                    if translated["status"] != "translated" or not isinstance(target, str) or not target:
                        deterministic_issue_blocks += 1
                        findings.append(
                            {
                                "block_id": block_id,
                                "issue_type": "meaning_omission",
                                "severity": "major",
                                "source_evidence": _exact_evidence(source),
                                "target_evidence": "",
                                "reason": "Translator did not produce a valid persisted target for this block.",
                            }
                        )
                        continue
                    safe, reasons = _mechanical_quality(
                        block_id=block_id,
                        source=source,
                        target=target,
                        allow_unchanged=(
                            block_id
                            in spans_v5.fixed_only_block_ids(
                                spans_v5.protect_blocks([source_row])
                            )
                        ),
                    )
                    if not safe:
                        deterministic_issue_blocks += 1
                        findings.append(
                            {
                                "block_id": block_id,
                                "issue_type": "semantic_other",
                                "severity": "major",
                                "source_evidence": _exact_evidence(source),
                                "target_evidence": _exact_evidence(target),
                                "reason": "Deterministic protected-content gate: " + ", ".join(reasons),
                            }
                        )
                        continue
                    safe_blocks.append(
                        {
                            "block_id": block_id,
                            "block_type": str(source_row.get("block_type") or "prose"),
                            "source_full_text": source,
                            "target_full_text": target,
                        }
                    )
                    source_plan = spans_v4.protect_blocks([source_row])
                    protected_segments_by_block.update(
                        spans_v4.fixed_source_segments(source_plan)
                    )
                if not safe_blocks:
                    completed_work += 1
                    observations.progress(
                        completed=completed_work,
                        total=total_work,
                    )
                    continue
                packet = quality_contract.build_packet(
                    window_id=f"quality_{arm_id}_{window['window_id']}",
                    blocks=safe_blocks,
                    integrity_receipt={
                        "policy_id": quality_contract.INTEGRITY_POLICY_ID,
                        "full_text_restored": True,
                        "source_target_math_byte_exact": True,
                        "source_target_structure_order_equal": True,
                        "forbidden_control_characters_absent": True,
                        "protected_content_read_only": True,
                    },
                )
                _, validation = _semantic_call(
                    client=client,
                    messages=quality_contract.render_messages(packet),
                    response_format=quality_contract.RESPONSE_SCHEMA,
                    tag=f"quality_{arm_id}_{window['window_id']}",
                    validator_id=quality_contract.LOCAL_VALIDATOR_ID,
                    parse=lambda value: (
                        dict(value)
                        if isinstance(value, Mapping)
                        else dict(quality_contract.parse_response(str(value)))
                    ),
                    validate=lambda parsed, packet=packet: quality_contract.validate_response(
                        parsed, packet
                    ),
                    observations=observations,
                    retry_cap=int(role["semantic_retry_cap"]),
                )
                llm_packets += 1
                validated_findings = [
                    dict(value) for value in validation["findings"]
                ]
                window_findings, suppressed_findings = (
                    quality_contract.filter_protected_content_findings(
                        validated_findings,
                        blocks=packet["blocks"],
                        protected_segments_by_block=protected_segments_by_block,
                    )
                )
                protected_finding_suppressed_count += len(suppressed_findings)
                findings.extend(window_findings)
                major_by_block: dict[str, list[dict[str, str]]] = {}
                for finding in window_findings:
                    if finding["severity"] != "major":
                        continue
                    major_by_block.setdefault(
                        str(finding["block_id"]), []
                    ).append(finding)
                state = attempt_state.get(str(window["window_id"]))
                if state is None:
                    raise D2LProjectLiveExecutorError(
                        "quality stage lacks Translator attempt state for "
                        + str(window["window_id"])
                    )
                if major_by_block:
                    updates, repair_state = _semantic_repair_window(
                        campaign=campaign,
                        arm_id=arm_id,
                        original_window_id=str(window["window_id"]),
                        original_block_ids=list(window["block_ids"]),
                        output_block_ids=[
                            block_id
                            for block_id in window["block_ids"]
                            if block_id in major_by_block
                        ],
                        findings_by_block=major_by_block,
                        source_by_id=row_map,
                        current_translations=translations,
                        attempt_state=state,
                        transport=transport,
                        component_attempt_id=component_attempt_id,
                        observations=observations,
                    )
                    repair_updates.update(updates)
                    repair_rows.append(
                        {
                            "window_id": str(window["window_id"]),
                            "initial_attempt_count": int(state["attempt_count"]),
                            "major_finding_count": sum(
                                len(value) for value in major_by_block.values()
                            ),
                            **repair_state,
                        }
                    )
                completed_work += 1
                observations.progress(
                    completed=completed_work,
                    total=total_work,
                )
            report = build_quality_observation(
                audited_block_ids=audited_ids,
                findings=findings,
                source_translation_artifact_refs=[f"art_translation_{arm_id}"],
            )
            arm_reports[arm_id] = report
            state_rows.append(
                {
                    "arm_id": arm_id,
                    "audited_block_count": len(audited_ids),
                    "llm_packet_count": llm_packets,
                    "deterministic_issue_block_count": deterministic_issue_blocks,
                    "finding_count": report["counts"]["findings"],
                    "protected_finding_suppressed_count": (
                        protected_finding_suppressed_count
                    ),
                    "semantic_repair_attempt_count": sum(
                        row["calls"] for row in repair_rows
                    ),
                    "semantic_repair_applied_count": sum(
                        row["status"] == "repair_applied_unverified_semantically"
                        for row in repair_rows
                    ),
                    "mechanical_retry_before_semantic_repair_count": sum(
                        bool(row.get("mechanical_retry_consumed"))
                        for row in repair_rows
                    ),
                    "semantic_repair_failed_count": sum(
                        row["status"] == "repair_failed" for row in repair_rows
                    ),
                    "repairs": repair_rows,
                    "continue_to_scoring": True,
                }
            )
            final_artifacts[arm_id] = _final_translation_artifact(
                draft=draft_artifact,
                updates=repair_updates,
                campaign=campaign,
                arm_id=arm_id,
                component_attempt_id=component_attempt_id,
            )
    finally:
        db.close()
    observations_payload = _sealed(
        {
            "schema_version": "d2l_translation_quality_observations_live_v2",
            "policy_id": "d2l_translation_quality_repair_once_nonblocking_v2",
            "arms": arm_reports,
            "source_translation_artifact_refs": [
                "art_translation_s0",
                "art_translation_s1",
            ],
            "final_translation_artifact_refs": [
                "art_translation_s0_final",
                "art_translation_s1_final",
            ],
            "glossary_visibility": "none",
            "semantic_output_authority": "validated_findings_only",
        }
    )
    total_findings = sum(int(row["finding_count"]) for row in state_rows)
    applied_repairs = sum(
        int(row["semantic_repair_applied_count"]) for row in state_rows
    )
    state = _sealed(
        {
            "schema_version": "d2l_translation_quality_state_live_v2",
            "policy_id": "d2l_translation_quality_repair_once_nonblocking_v2",
            "arms": state_rows,
            "finding_count": total_findings,
            "semantic_repair_applied_count": applied_repairs,
            "blocking": False,
            "continue_to_scoring": True,
            "status": (
                "completed_after_semantic_repair_unverified"
                if applied_repairs
                else (
                    "completed_with_findings"
                    if total_findings
                    else "completed_clean"
                )
            ),
        }
    )
    return (
        observations_payload,
        state,
        final_artifacts["s0"],
        final_artifacts["s1"],
    )


def _artifact_binding(
    component_root: Path,
    *,
    artifact_ref: str,
    artifact_kind: str,
    schema_version: str,
    relative_path: str,
) -> dict[str, str]:
    return {
        "artifact_ref": artifact_ref,
        "artifact_kind": artifact_kind,
        "schema_version": schema_version,
        "sha256": file_sha256(component_root / relative_path),
        "sha256_kind": "physical",
    }


def _scoring_handoff_stage(
    *,
    campaign: Mapping[str, Any],
    rows: Sequence[Mapping[str, Any]],
    component_root: Path,
    component_attempt_id: int,
) -> dict[str, Any]:
    config = campaign["config"]
    eligible_ids = [
        str(row["block_id"])
        for row in rows
        if row["channel"] != "review_required"
    ]
    translated_count = sum(
        row["channel"] in {"semantic_text", "structured_translate"} for row in rows
    )
    preserved_count = sum(row["channel"] == "preserve_only" for row in rows)
    translation_inputs: list[dict[str, Any]] = []
    for arm_id in ("s0", "s1"):
        artifact = _load_json(
            component_root / f"artifacts/quality/{arm_id}_final.json",
            f"{arm_id} final translation artifact",
        )
        missing = int(artifact["coverage"]["missing_count"])
        failed = int(artifact["coverage"]["failed_count"])
        coverage = {
            "admitted_block_count": len(eligible_ids),
            "translated_block_count": translated_count - failed - missing,
            "preserved_block_count": preserved_count,
            "missing_block_count": missing,
            "failed_block_count": failed,
            "ordered_block_ids_sha256": canonical_sha256(eligible_ids),
            "status": "exact_cover",
        }
        role = _role(config, f"d2l.translator.{arm_id}")
        translation_inputs.append(
            {
                "arm_id": arm_id,
                "artifact": _artifact_binding(
                    component_root,
                    artifact_ref=f"art_translation_{arm_id}_final",
                    artifact_kind="translation_artifact",
                    schema_version="TranslationArtifactV1",
                    relative_path=f"artifacts/quality/{arm_id}_final.json",
                ),
                "producer_component_run_id": config["component_run_id"],
                "producer_component_attempt_id": component_attempt_id,
                "profile_id": LIVE_PROFILE_ID,
                "profile_sha256": role["semantic_role_sha256"],
                "config_sha256": config["integrity"]["payload_sha256"],
                "selected_chapter_ids": list(config["selected_chapter_ids"]),
                "coverage": coverage,
                "source_binding_sha256": canonical_sha256(config["source_binding"]),
            }
        )
    fragment = build_scoring_handoff_fragment(
        workflow_run_id=config["workflow_run_id"],
        translation_component_run_id=config["component_run_id"],
        translation_component_attempt_id=component_attempt_id,
        reserved_evaluation_component_run_id=f"eval_{config['workflow_run_id']}",
        artifact_ref="art_scoring_handoff_fragment",
        source_binding=config["source_binding"],
        translation_inputs=translation_inputs,
        glossary_binding=_artifact_binding(
            component_root,
            artifact_ref="art_glossary",
            artifact_kind="glossary",
            schema_version="d2l_sealed_glossary_v1",
            relative_path="artifacts/glossary_seal/glossary.json",
        ),
        context_memory_binding=None,
        selected_chapter_ids=config["selected_chapter_ids"],
        admitted_universe={
            "ordered_block_ids_sha256": canonical_sha256(eligible_ids),
            "block_count": len(eligible_ids),
            "status": "exact_cover",
        },
        producer_lineage={
            "git_commit": config["code_revision"],
            "pipeline_version": config["pipeline_version"],
            "config_sha256": config["integrity"]["payload_sha256"],
            "code_sha256": file_sha256(Path(__file__)),
        },
        created_at=_utc_now(),
    )
    validate_scoring_handoff_fragment(fragment)
    return fragment


_SEMANTIC_STAGES = {
    "b1_candidate_discovery",
    "b2_admission_translation",
    "auditor_morphology",
    "auditor_target_collision",
    "auditor_multi_target",
    "translator",
    "translation_quality_audit",
}


def execute_live_stage(
    *,
    campaign: Mapping[str, Any],
    project: Any,
    rows: Sequence[Mapping[str, Any]],
    stage_id: str,
    component_root: Path,
    work_db: Path,
    transport: ProjectTransport | None,
    component_attempt_id: int,
    producer: str,
    work_id: str,
) -> dict[str, dict[str, Any]]:
    if stage_id in _SEMANTIC_STAGES and transport is None:
        raise D2LProjectLiveExecutorError(
            f"semantic stage requires an injected transport: {stage_id}"
        )
    observations = _StageObservations(
        campaign=campaign,
        component_root=component_root,
        component_attempt_id=component_attempt_id,
        stage_id=stage_id,
        agent=producer,
        work_kind=_STAGE_UNITS[stage_id],
        work_id=work_id,
    )
    payloads: dict[str, dict[str, Any]]
    if stage_id == "preflight":
        payloads = {
            "art_preflight": _sealed(
                {
                    "schema_version": "d2l_campaign_stage_preflight_live_v1",
                    "execution_mode": "live_sealed",
                    "semantic_output_authority": False,
                    "selected_block_count": len(rows),
                    "selected_chapter_ids": list(campaign["config"]["selected_chapter_ids"]),
                    "channel_counts": dict(campaign["universe"]["channel_counts"]),
                    "campaign_config_sha256": campaign["config"]["integrity"]["payload_sha256"],
                    "campaign_seal_sha256": campaign["seal"]["integrity"]["payload_sha256"],
                    "status": "live_ready_sealed",
                }
            )
        }
    elif stage_id == "b1_candidate_discovery":
        assert transport is not None
        discovery, timeline = _b1_stage(
            campaign=campaign,
            rows=rows,
            transport=transport,
            component_attempt_id=component_attempt_id,
            observations=observations,
        )
        payloads = {
            "art_b1_candidate_discovery": discovery,
            "art_b1_proposal_timeline": timeline,
        }
    elif stage_id == "candidate_index":
        payloads = {
            "art_candidate_index": _candidate_index_stage(
                campaign=campaign, rows=rows, component_root=component_root
            )
        }
    elif stage_id == "b2_admission_translation":
        assert transport is not None
        payloads = {
            "art_b2_admission": _b2_stage(
                campaign=campaign,
                component_root=component_root,
                transport=transport,
                component_attempt_id=component_attempt_id,
                observations=observations,
            )
        }
    elif stage_id == "auditor_morphology":
        assert transport is not None
        payloads = {
            "art_auditor_morphology": _morphology_stage(
                campaign=campaign,
                component_root=component_root,
                transport=transport,
                component_attempt_id=component_attempt_id,
                observations=observations,
            )
        }
    elif stage_id == "auditor_target_collision":
        assert transport is not None
        payloads = {
            "art_auditor_target_collision": _target_collision_stage(
                campaign=campaign,
                component_root=component_root,
                transport=transport,
                component_attempt_id=component_attempt_id,
                observations=observations,
            )
        }
    elif stage_id == "auditor_multi_target":
        assert transport is not None
        payloads = {
            "art_auditor_multi_target": _multi_target_stage(
                campaign=campaign,
                component_root=component_root,
                transport=transport,
                component_attempt_id=component_attempt_id,
                observations=observations,
            )
        }
    elif stage_id == "glossary_seal":
        glossary, delta = _glossary_stage(
            campaign=campaign,
            component_root=component_root,
            work_db=work_db,
            component_attempt_id=component_attempt_id,
        )
        payloads = {"art_glossary": glossary, "art_glossary_memory_delta": delta}
    elif stage_id == "translator":
        assert transport is not None
        s0, s1 = _translator_stage(
            campaign=campaign,
            project=project,
            rows=rows,
            work_db=work_db,
            transport=transport,
            component_attempt_id=component_attempt_id,
            observations=observations,
        )
        payloads = {"art_translation_s0": s0, "art_translation_s1": s1}
    elif stage_id == "translation_quality_audit":
        assert transport is not None
        quality, state, s0_final, s1_final = _quality_stage(
            campaign=campaign,
            rows=rows,
            component_root=component_root,
            work_db=work_db,
            transport=transport,
            component_attempt_id=component_attempt_id,
            observations=observations,
        )
        payloads = {
            "art_translation_quality_observations": quality,
            "art_translation_quality_state": state,
            "art_translation_s0_final": s0_final,
            "art_translation_s1_final": s1_final,
        }
    elif stage_id == "scoring_handoff_fragment":
        payloads = {
            "art_scoring_handoff_fragment": _scoring_handoff_stage(
                campaign=campaign,
                rows=rows,
                component_root=component_root,
                component_attempt_id=component_attempt_id,
            )
        }
    else:
        raise D2LProjectLiveExecutorError(f"unsupported live stage: {stage_id}")
    if stage_id in _SEMANTIC_STAGES:
        payloads[f"art_{stage_id}_receipt"] = observations.receipt(
            campaign=campaign,
            component_attempt_id=component_attempt_id,
            producer=producer,
            work_id=work_id,
        )
    return payloads


__all__ = [
    "D2LProjectLiveExecutorError",
    "LIVE_EXECUTOR_VERSION",
    "LIVE_PROFILE_ID",
    "ProjectTransport",
    "execute_live_stage",
]
