"""Measure bounded B2 call-graph compression without calling a provider."""

from __future__ import annotations

import argparse
from copy import deepcopy
from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Any, Mapping, Sequence

from pipeline.agents.llm_client import estimate_prompt_tokens
from pipeline.literary.b2_recovery_batch_v1 import (
    batch_request_payload_v1,
    render_registry_recovery_batch_request_v1,
    validate_registry_recovery_batch_response_v1,
)
from pipeline.literary.b2_recovery_v1 import (
    render_registry_recovery_request_v1,
    verify_b2_recovery_index_v1,
)
from pipeline.literary.checkpoint import canonical_hash, canonical_json, file_sha256


REPORT_SCHEMA_VERSION = "literary_b2_callgraph_exp1_report_v1"


class B2CallgraphExp1Error(RuntimeError):
    pass


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _read_object(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise B2CallgraphExp1Error(f"cannot read {label}: {path}") from exc
    if not isinstance(value, dict):
        raise B2CallgraphExp1Error(f"{label} must be an object")
    return value


def _write_new_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        raise B2CallgraphExp1Error(f"refusing to overwrite artifact: {path}")
    path.write_text(canonical_json(payload) + "\n", encoding="utf-8")


def _openai_prompt_estimate(
    messages: Sequence[Mapping[str, Any]],
    schema: Mapping[str, Any],
    *,
    schema_name: str,
) -> int:
    return estimate_prompt_tokens(
        [dict(row) for row in messages],
        response_format={
            "type": "json_schema",
            "json_schema": {
                "name": schema_name,
                "strict": True,
                "schema": dict(schema),
            },
        },
    )


def _usage_prompt_tokens(path: Path) -> int:
    raw = _read_object(path, "baseline raw result")
    usage = raw.get("usage")
    if not isinstance(usage, Mapping):
        raise B2CallgraphExp1Error("baseline raw result lacks usage")
    value = usage.get("prompt_tokens")
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise B2CallgraphExp1Error("baseline prompt token count is invalid")
    return value


def _baseline_response_by_component(
    component_dirs: Sequence[Path],
) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for directory in component_dirs:
        raw = _read_object(directory / "raw_result.json", "baseline raw result")
        component_id = str(raw.get("component_id") or "")
        response = raw.get("parsed_json")
        if not component_id or not isinstance(response, Mapping):
            raise B2CallgraphExp1Error(
                "baseline recovery result lacks component or parsed JSON"
            )
        if component_id in result:
            raise B2CallgraphExp1Error(
                "baseline recovery repeats a component"
            )
        result[component_id] = deepcopy(dict(response))
    return result


def _replay_batch(
    *,
    index: Mapping[str, Any],
    request: Any,
    responses: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any]:
    component_ids = list(request.semantic_payload["component_ids"])
    payload = {
        "schema_version": "literary_b2_registry_recovery_batch_response_v1_1",
        "chapter_id": index["chapter_id"],
        "batch_id": request.component_id,
        "component_results": [
            {
                "component_id": component_id,
                "result": deepcopy(dict(responses[component_id])),
            }
            for component_id in component_ids
        ],
    }
    decision = validate_registry_recovery_batch_response_v1(
        payload,
        index=index,
        component_ids=component_ids,
        request_fingerprint=request.request_fingerprint,
    )
    return {
        "batch_id": request.component_id,
        "component_ids": component_ids,
        "component_count": len(decision["component_decisions"]),
        "ticket_count": sum(
            len(row["ticket_actions"])
            for row in decision["component_decisions"]
        ),
        "batch_decision_hash": decision["batch_decision_hash"],
        "status": "valid",
    }


def _prefix_layout_probe(requests: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    if not requests:
        raise B2CallgraphExp1Error("B2 prefix probe needs interaction requests")
    systems: list[str] = []
    payloads: list[dict[str, Any]] = []
    wire_texts: list[str] = []
    for request in requests:
        messages = request.get("messages")
        if not isinstance(messages, list) or len(messages) != 2:
            raise B2CallgraphExp1Error("interaction request message shape drifted")
        system = str(messages[0].get("content") or "")
        user = str(messages[1].get("content") or "")
        try:
            payload = json.loads(user)
        except ValueError as exc:
            raise B2CallgraphExp1Error(
                "interaction request user payload is not JSON"
            ) from exc
        if not isinstance(payload, dict):
            raise B2CallgraphExp1Error("interaction user payload must be an object")
        systems.append(system)
        payloads.append(payload)
        wire_texts.append(system + "\n" + user)
    if len(set(systems)) != 1:
        raise B2CallgraphExp1Error("interaction system prompts are not identical")

    static_rows: list[dict[str, Any]] = []
    for payload in payloads:
        frame = payload.get("frame_context")
        packet = payload.get("candidate_packets")
        if not isinstance(frame, Mapping) or not isinstance(packet, Mapping):
            raise B2CallgraphExp1Error("interaction context shape drifted")
        static_rows.append(
            {
                "request_kind": payload.get("request_kind"),
                "chapter_id": payload.get("chapter_id"),
                "frame_context_status": payload.get("frame_context_status"),
                "frame_static": {
                    "schema_version": frame.get("schema_version"),
                    "chapter_orientation": deepcopy(
                        frame.get("chapter_orientation")
                    ),
                    "frame_artifact_hash": frame.get("frame_artifact_hash"),
                },
                "candidate_context_static": {
                    "schema_version": packet.get("schema_version"),
                    "chapter_id": packet.get("chapter_id"),
                    "prefix_bundle_hash": packet.get("prefix_bundle_hash"),
                    "claim_transition_coverage": packet.get(
                        "claim_transition_coverage"
                    ),
                },
            }
        )
    static_hashes = [canonical_hash(row) for row in static_rows]
    if len(set(static_hashes)) != 1:
        raise B2CallgraphExp1Error(
            "proposed B2 static prefix is not byte-stable across windows"
        )
    current_common_chars = _longest_common_prefix_length(wire_texts)
    proposed_prefix = systems[0] + "\n" + canonical_json(static_rows[0])
    return {
        "window_count": len(requests),
        "system_prompt_sha256": file_sha256_from_text(systems[0]),
        "current_common_prefix_chars": current_common_chars,
        "current_common_prefix_token_estimate": max(
            1, current_common_chars // 4
        ),
        "proposed_static_context_hash": static_hashes[0],
        "proposed_static_context_chars": len(canonical_json(static_rows[0])),
        "proposed_common_prefix_chars": len(proposed_prefix),
        "proposed_common_prefix_token_estimate": max(
            1, len(proposed_prefix) // 4
        ),
        "runtime_contract_changed": False,
        "adoption_status": "measured_only_not_adopted",
        "reason": (
            "Adoption would change the two-message B2 request contract; "
            "the probe measures cache potential without changing semantics."
        ),
    }


def file_sha256_from_text(value: str) -> str:
    import hashlib

    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _longest_common_prefix_length(values: Sequence[str]) -> int:
    if not values:
        return 0
    shortest = min(len(value) for value in values)
    for index in range(shortest):
        character = values[0][index]
        if any(value[index] != character for value in values[1:]):
            return index
    return shortest


def run(*, context_run_root: Path, output_root: Path) -> dict[str, Any]:
    root = context_run_root.resolve()
    output = output_root.resolve()
    if output.exists():
        raise B2CallgraphExp1Error("output root already exists")
    recovery_root = root / "b2_recovery" / "ch002" / "attempt_001"
    index_path = recovery_root / "recovery_index.json"
    index = verify_b2_recovery_index_v1(
        _read_object(index_path, "chapter-2 recovery index")
    )
    component_ids = [
        row["component_id"]
        for row in index["registry_components"]
        if not row["overflow"]
    ]
    if len(component_ids) != 4:
        raise B2CallgraphExp1Error(
            "EXP-1 is sealed to the measured four-component chapter-2 index"
        )
    singles = [
        render_registry_recovery_request_v1(
            index=index,
            component_id=component_id,
        )
        for component_id in component_ids
    ]
    batch4 = render_registry_recovery_batch_request_v1(
        index=index,
        component_ids=component_ids,
    )
    batch2 = [
        render_registry_recovery_batch_request_v1(
            index=index,
            component_ids=component_ids[start : start + 2],
        )
        for start in range(0, len(component_ids), 2)
    ]
    single_estimates = [
        _openai_prompt_estimate(
            request.messages,
            request.response_schema,
            schema_name="literary_b2_registry_recovery_v1",
        )
        for request in singles
    ]
    batch4_estimate = _openai_prompt_estimate(
        batch4.messages,
        batch4.response_schema,
        schema_name="literary_b2_registry_recovery_batch_v1",
    )
    batch2_estimates = [
        _openai_prompt_estimate(
            request.messages,
            request.response_schema,
            schema_name="literary_b2_registry_recovery_batch_v1",
        )
        for request in batch2
    ]
    baseline_dirs = sorted(
        path
        for path in (recovery_root / "registry_recovery").iterdir()
        if path.is_dir()
    )
    actual_prompt_tokens = sum(
        _usage_prompt_tokens(path / "raw_result.json")
        for path in baseline_dirs
    )
    baseline_responses = _baseline_response_by_component(baseline_dirs)
    offline_replays = [
        _replay_batch(
            index=index,
            request=batch4,
            responses=baseline_responses,
        ),
        *[
            _replay_batch(
                index=index,
                request=request,
                responses=baseline_responses,
            )
            for request in batch2
        ],
    ]
    repeated_card_count = sum(
        len(request.semantic_payload["candidate_cards"])
        for request in singles
    )
    union_card_count = len(
        batch4.semantic_payload["shared_candidate_cards"]
    )

    interaction_dir = root / "b2" / "ch002" / "attempt_001" / "interactions"
    interaction_requests = [
        _read_object(path, "B2 interaction request")
        for path in sorted(interaction_dir.glob("*/request.json"))
    ]
    prefix_probe = _prefix_layout_probe(interaction_requests)
    baseline_estimate = sum(single_estimates)
    batch4_ratio = batch4_estimate / baseline_estimate
    batch2_ratio = sum(batch2_estimates) / baseline_estimate
    chosen_shape = (
        "4_in_1"
        if batch4_estimate <= 20_000 and batch4_ratio < batch2_ratio
        else "2_plus_2"
    )
    report_body = {
        "schema_version": REPORT_SCHEMA_VERSION,
        "mode": "offline_zero_api",
        "status": "complete",
        "source_context_run_root": str(root),
        "source_recovery_index": str(index_path),
        "source_recovery_index_sha256": file_sha256(index_path),
        "recovery_index_hash": index["recovery_index_hash"],
        "chapter_id": index["chapter_id"],
        "component_ids": component_ids,
        "registry_recovery": {
            "baseline_calls": 4,
            "baseline_actual_prompt_tokens": actual_prompt_tokens,
            "baseline_render_estimates": single_estimates,
            "baseline_render_estimate_total": baseline_estimate,
            "batch_4_in_1_prompt_estimate": batch4_estimate,
            "batch_4_in_1_ratio": batch4_ratio,
            "batch_2_plus_2_prompt_estimates": batch2_estimates,
            "batch_2_plus_2_prompt_estimate_total": sum(batch2_estimates),
            "batch_2_plus_2_ratio": batch2_ratio,
            "candidate_card_rows_before": repeated_card_count,
            "candidate_card_rows_after_union": union_card_count,
            "candidate_card_rows_removed": repeated_card_count
            - union_card_count,
            "selected_canary_shape": chosen_shape,
            "four_in_one_gate": {
                "prompt_estimate_lte_20000": batch4_estimate <= 20_000,
                "ratio_lt_0_90": batch4_ratio < 0.90,
            },
            "two_plus_two_gate": {
                "ratio_lt_0_90": batch2_ratio < 0.90,
                "each_batch_lte_20000": all(
                    value <= 20_000 for value in batch2_estimates
                ),
            },
            "offline_baseline_response_replays": offline_replays,
        },
        "b2_interaction_prefix": prefix_probe,
        "provider_call_performed": False,
        "gold_or_oracle_loaded": False,
        "source_artifact_mutated": False,
        "production_publish_performed": False,
        "completed_at": _now(),
    }
    report = {**report_body, "report_hash": canonical_hash(report_body)}
    output.mkdir(parents=True)
    _write_new_json(output / "report.json", report)
    _write_new_json(output / "batch_4_in_1_request.json", batch_request_payload_v1(batch4))
    for ordinal, request in enumerate(batch2, 1):
        _write_new_json(
            output / f"batch_2_plus_2_{ordinal:02d}_request.json",
            batch_request_payload_v1(request),
        )
    return report


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--context-run-root", type=Path, required=True)
    parser.add_argument("--output-root", type=Path, required=True)
    args = parser.parse_args()
    print(
        json.dumps(
            run(
                context_run_root=args.context_run_root,
                output_root=args.output_root,
            ),
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
