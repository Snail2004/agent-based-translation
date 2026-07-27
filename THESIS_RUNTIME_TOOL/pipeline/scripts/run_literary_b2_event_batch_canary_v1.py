"""Measure or run one sealed GPT-5.4 EventReviewBatchV1 canary."""

from __future__ import annotations

import argparse
from copy import deepcopy
from datetime import datetime, timezone
import json
from pathlib import Path
import subprocess
from typing import Any, Mapping, Sequence

from pipeline.agents.llm_client import estimate_prompt_tokens
from pipeline.agents.provider_profile import (
    load_provider_profile,
    resolve_role_credential,
)
from pipeline.literary.b2_event_batch_v1 import (
    batch_request_payload_v1,
    render_event_review_batch_request_v1,
    validate_event_review_batch_response_v1,
)
from pipeline.literary.b2_recovery_v1 import (
    B2RecoveryContractError,
    render_event_review_request_v2,
    verify_b2_recovery_index_v1,
)
from pipeline.literary.checkpoint import canonical_hash, canonical_json, file_sha256
from pipeline.literary.openai_compatible_structured_call_v1 import (
    call_openai_compatible_structured_v1,
)
from pipeline.literary.structured_output_policy_v1 import (
    load_literary_structured_output_policy,
    openai_response_format,
    resolve_structured_output_contract,
    validate_structured_payload,
)
from pipeline.scripts.run_chapter_registry_v4_real import FROZEN_DB_SHA256


RUN_SEAL_SCHEMA_VERSION = "literary_b2_event_batch_canary_seal_v1_1"
REPORT_SCHEMA_VERSION = "literary_b2_event_batch_canary_report_v1_1"
ROLE_ID = "literary_local_conflict_auditor"
SCHEMA_NAME = "literary_b2_event_review_batch_v1_1"
PROMPT_TOKEN_CAP = 20_000
OUTPUT_TOKEN_CAP = 12_000
HARD_VISIBLE_TOKEN_CAP = 32_000


class EventBatchCanaryError(RuntimeError):
    pass


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _read_object(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise EventBatchCanaryError(f"cannot read {label}: {path}") from exc
    if not isinstance(value, dict):
        raise EventBatchCanaryError(f"{label} must be an object")
    return value


def _write_new_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        raise EventBatchCanaryError(f"refusing to overwrite artifact: {path}")
    path.write_text(canonical_json(payload) + "\n", encoding="utf-8")


def _git_head(root: Path) -> str:
    completed = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


def _usage_value(usage: Mapping[str, Any], key: str) -> int:
    value = usage.get(key, 0)
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise EventBatchCanaryError(f"provider usage {key} is invalid")
    return value


def _component_ids(index: Mapping[str, Any]) -> list[str]:
    ids = [
        str(row["component_id"])
        for row in index.get("event_components") or []
        if not row.get("overflow")
    ]
    if len(ids) != 2:
        raise EventBatchCanaryError(
            "event batch canary is sealed to exactly two non-overflow components"
        )
    return ids


def _baseline_dirs(root: Path) -> list[Path]:
    result = sorted(path for path in root.iterdir() if path.is_dir())
    if len(result) != 2:
        raise EventBatchCanaryError("baseline event root must contain two components")
    return result


def _baseline_responses(root: Path) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for directory in _baseline_dirs(root):
        raw = _read_object(directory / "raw_result.json", "baseline raw result")
        component_id = str(raw.get("component_id") or "")
        parsed = raw.get("parsed_json")
        if not component_id or not isinstance(parsed, Mapping):
            raise EventBatchCanaryError("baseline event response is incomplete")
        if component_id in result:
            raise EventBatchCanaryError("baseline repeats an event component")
        result[component_id] = deepcopy(dict(parsed))
    return result


def _baseline_prompt_tokens(root: Path) -> int:
    total = 0
    for directory in _baseline_dirs(root):
        raw = _read_object(directory / "raw_result.json", "baseline raw result")
        usage = raw.get("usage")
        if not isinstance(usage, Mapping):
            raise EventBatchCanaryError("baseline event usage is missing")
        total += _usage_value(usage, "prompt_tokens")
    return total


def _ordinary_pairwise_authority(action: Mapping[str, Any]) -> bool:
    if str(action.get("action") or "") not in {"keep", "revise", "split"}:
        return False
    return any(
        assessment.get("actuality") == "occurred"
        and assessment.get("endpoint_status") == "resolved"
        and assessment.get("directionality") in {"one_way", "reciprocal"}
        for assessment in action.get("effective_event_assessments") or []
        if isinstance(assessment, Mapping)
    )


def _baseline_actions(root: Path) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for directory in _baseline_dirs(root):
        decision = _read_object(directory / "decision.json", "baseline decision")
        for action in decision.get("event_actions") or []:
            case_id = str(action.get("case_id") or "")
            action_name = str(action.get("action") or "")
            if not case_id or not action_name or case_id in result:
                raise EventBatchCanaryError("baseline event actions are malformed")
            result[case_id] = {
                "action": action_name,
                "ordinary_pairwise_authority": _ordinary_pairwise_authority(
                    action
                ),
            }
    return result


def _wrapped_response(
    *, request: Any, responses: Mapping[str, Mapping[str, Any]]
) -> dict[str, Any]:
    ids = list(request.semantic_payload["component_ids"])
    if set(ids) != set(responses):
        raise EventBatchCanaryError("baseline response component set drifted")
    return {
        "schema_version": "literary_b2_event_review_batch_response_v1_1",
        "chapter_id": request.semantic_payload["chapter_id"],
        "batch_id": request.component_id,
        "component_results": [
            {
                "component_id": component_id,
                "case_channels": [
                    {
                        "case_id": str(case["case_id"]),
                        "observation_channel": "mixed_or_uncertain",
                    }
                    for component in request.semantic_payload["components"]
                    if component["component_id"] == component_id
                    for case in component["event_cases"]
                ],
                "result": deepcopy(dict(responses[component_id])),
            }
            for component_id in ids
        ],
    }


def _comparison(
    *,
    decisions: Sequence[Mapping[str, Any]],
    baseline: Mapping[str, Mapping[str, Any]],
    authority_hold_case_ids: Sequence[str],
) -> dict[str, Any]:
    observed = {
        str(action["case_id"]): {
            "action": str(action["action"]),
            "ordinary_pairwise_authority": _ordinary_pairwise_authority(action),
        }
        for decision in decisions
        for action in decision["event_actions"]
    }
    shared = sorted(set(observed).intersection(baseline))
    changed = [
        {
            "case_id": case_id,
            "baseline_action": baseline[case_id]["action"],
            "canary_action": observed[case_id]["action"],
            "baseline_pairwise_authority": baseline[case_id][
                "ordinary_pairwise_authority"
            ],
            "canary_pairwise_authority": observed[case_id][
                "ordinary_pairwise_authority"
            ],
        }
        for case_id in shared
        if baseline[case_id]["action"] != observed[case_id]["action"]
    ]
    authority_increases = [
        {
            "case_id": case_id,
            "baseline_action": baseline[case_id]["action"],
            "canary_action": observed[case_id]["action"],
        }
        for case_id in shared
        if not baseline[case_id]["ordinary_pairwise_authority"]
        and observed[case_id]["ordinary_pairwise_authority"]
    ]
    held = set(authority_hold_case_ids)
    unheld_authority_increases = [
        row for row in authority_increases if row["case_id"] not in held
    ]
    return {
        "baseline_case_count": len(baseline),
        "canary_case_count": len(observed),
        "shared_case_count": len(shared),
        "same_action_count": len(shared) - len(changed),
        "changed_action_count": len(changed),
        "authority_increase_count": len(authority_increases),
        "unheld_authority_increase_count": len(unheld_authority_increases),
        "changed_actions": changed,
        "authority_increases": authority_increases,
        "unheld_authority_increases": unheld_authority_increases,
    }


def _estimates(
    *,
    index: Mapping[str, Any],
    artifact: Mapping[str, Any],
    ledger: Mapping[str, Any],
    component_ids: Sequence[str],
    request: Any,
) -> dict[str, Any]:
    singles = [
        render_event_review_request_v2(
            index=index,
            component_id=component_id,
            chapter_artifact=artifact,
            registry_ledger=ledger,
        )
        for component_id in component_ids
    ]

    def estimate(rendered: Any, schema_name: str) -> int:
        return estimate_prompt_tokens(
            [dict(row) for row in rendered.messages],
            response_format={
                "type": "json_schema",
                "json_schema": {
                    "name": schema_name,
                    "strict": True,
                    "schema": dict(rendered.response_schema),
                },
            },
        )

    single_estimates = [
        estimate(single, "literary_b2_event_review_v2") for single in singles
    ]
    batch_estimate = estimate(request, SCHEMA_NAME)
    card_rows_before = sum(
        len(single.semantic_payload.get("candidate_cards") or [])
        for single in singles
    )
    card_rows_after = len(request.semantic_payload["shared_candidate_cards"])
    return {
        "single_prompt_estimates": single_estimates,
        "single_prompt_estimate_total": sum(single_estimates),
        "batch_prompt_estimate": batch_estimate,
        "batch_to_single_estimate_ratio": (
            batch_estimate / sum(single_estimates)
        ),
        "candidate_card_rows_before": card_rows_before,
        "candidate_card_rows_after_union": card_rows_after,
        "candidate_card_rows_removed": card_rows_before - card_rows_after,
    }


def run(
    *,
    mode: str,
    repo_root: Path,
    recovery_index_path: Path,
    chapter_artifact_path: Path,
    registry_ledger_path: Path,
    baseline_event_root: Path,
    output_root: Path,
    provider_profile_path: Path | None,
    structured_output_policy_path: Path | None,
    credential_root: Path | None,
    frozen_db: Path,
) -> dict[str, Any]:
    if mode not in {"offline", "live"}:
        raise EventBatchCanaryError("mode must be offline or live")
    repo = repo_root.resolve()
    output = output_root.resolve()
    if output.exists():
        raise EventBatchCanaryError("output root already exists")
    frozen = frozen_db.resolve()
    if file_sha256(frozen).upper() != FROZEN_DB_SHA256:
        raise EventBatchCanaryError("frozen database hash changed before run")

    index_path = recovery_index_path.resolve()
    artifact_path = chapter_artifact_path.resolve()
    ledger_path = registry_ledger_path.resolve()
    baseline_root = baseline_event_root.resolve()
    index = verify_b2_recovery_index_v1(
        _read_object(index_path, "B2 recovery index")
    )
    artifact = _read_object(artifact_path, "B2 chapter artifact")
    ledger = _read_object(ledger_path, "registry recovery ledger")
    component_ids = _component_ids(index)
    request = render_event_review_batch_request_v1(
        index=index,
        component_ids=component_ids,
        chapter_artifact=artifact,
        registry_ledger=ledger,
    )
    estimates = _estimates(
        index=index,
        artifact=artifact,
        ledger=ledger,
        component_ids=component_ids,
        request=request,
    )
    actual_baseline_prompt = _baseline_prompt_tokens(baseline_root)
    output.mkdir(parents=True)
    _write_new_json(output / "request.json", batch_request_payload_v1(request))

    source_facts = {
        "git_head": _git_head(repo),
        "source_recovery_index": str(index_path),
        "source_recovery_index_sha256": file_sha256(index_path),
        "source_chapter_artifact": str(artifact_path),
        "source_chapter_artifact_sha256": file_sha256(artifact_path),
        "source_registry_ledger": str(ledger_path),
        "source_registry_ledger_sha256": file_sha256(ledger_path),
        "recovery_index_hash": index["recovery_index_hash"],
        "chapter_id": index["chapter_id"],
        "batch_id": request.component_id,
        "component_ids": component_ids,
        "request_fingerprint": request.request_fingerprint,
        "baseline_actual_prompt_tokens": actual_baseline_prompt,
        "estimates": estimates,
        "frozen_db_sha256_before": FROZEN_DB_SHA256,
    }

    if mode == "offline":
        wrapped = _wrapped_response(
            request=request,
            responses=_baseline_responses(baseline_root),
        )
        decision = validate_event_review_batch_response_v1(
            wrapped,
            index=index,
            component_ids=component_ids,
            chapter_artifact=artifact,
            registry_ledger=ledger,
            request_fingerprint=request.request_fingerprint,
        )
        _write_new_json(output / "baseline_wrapped_response.json", wrapped)
        _write_new_json(output / "offline_batch_decision.json", decision)
        report_body = {
            "schema_version": REPORT_SCHEMA_VERSION,
            "status": "complete",
            "mode": "offline_zero_api",
            **source_facts,
            "component_count": len(decision["component_decisions"]),
            "case_count": sum(
                len(row["event_actions"])
                for row in decision["component_decisions"]
            ),
            "provider_calls": 0,
            "gold_or_oracle_loaded": False,
            "source_artifact_mutated": False,
            "production_publish_performed": False,
            "frozen_db_sha256_after": file_sha256(frozen).upper(),
            "completed_at": _now(),
        }
        report = {**report_body, "report_hash": canonical_hash(report_body)}
        _write_new_json(output / "report.json", report)
        return report

    if (
        provider_profile_path is None
        or structured_output_policy_path is None
        or credential_root is None
    ):
        raise EventBatchCanaryError("live mode requires provider inputs")
    profile_path = provider_profile_path.resolve()
    policy_path = structured_output_policy_path.resolve()
    provider = load_provider_profile(profile_path)
    if set(provider.credentials) != {"openai-row2"}:
        raise EventBatchCanaryError("live canary is not sealed to OpenAI Key 2")
    role = provider.roles.get(ROLE_ID)
    if role is None or role.bucket_order != ("openai-row2",):
        raise EventBatchCanaryError("live canary role has fallback or route drift")
    if role.provider != "openai" or role.model_id != "gpt-5.4":
        raise EventBatchCanaryError("live canary model differs from GPT-5.4")
    credential = resolve_role_credential(
        provider,
        role_id=ROLE_ID,
        credential_root=credential_root.resolve(),
    )
    policy = load_literary_structured_output_policy(policy_path)
    contract = resolve_structured_output_contract(
        policy,
        role_id=ROLE_ID,
        provider=role.provider,
        base_url=credential.base_url,
        model_id=role.model_id,
        canonical_schema=request.response_schema,
    )
    if not contract.native_enforcement:
        raise EventBatchCanaryError("event batch requires native Structured Output")
    response_format = openai_response_format(contract, schema_name=SCHEMA_NAME)
    transport_prompt_estimate = estimate_prompt_tokens(
        [dict(row) for row in request.messages],
        response_format=response_format,
    )
    if transport_prompt_estimate > PROMPT_TOKEN_CAP:
        raise EventBatchCanaryError("event batch prompt exceeds the sealed 20k cap")

    seal_body = {
        "schema_version": RUN_SEAL_SCHEMA_VERSION,
        "status": "sealed_before_api",
        **source_facts,
        "provider_profile_id": provider.profile_id,
        "provider_profile_sha256": file_sha256(profile_path),
        "structured_output_policy_id": policy.policy_id,
        "structured_output_policy_sha256": file_sha256(policy_path),
        "stage_binding": {
            "provider_role_id": ROLE_ID,
            "provider": role.provider,
            "model_id": role.model_id,
            "quota_bucket_id": credential.quota_bucket_id,
            "credential_revision": credential.credential_revision,
            "credential_commitment": credential.commitment,
            "structured_output_contract": contract.to_payload(),
        },
        "generation": {
            "temperature": 1.0,
            "seed": 20260719,
            "reasoning_effort": "none",
            "verbosity": "low",
        },
        "limits": {
            "max_calls": 1,
            "max_retries": 0,
            "prompt_token_cap": PROMPT_TOKEN_CAP,
            "output_token_cap": OUTPUT_TOKEN_CAP,
            "hard_visible_token_cap": HARD_VISIBLE_TOKEN_CAP,
            "estimated_prompt_tokens": transport_prompt_estimate,
        },
        "safety": {
            "provider_fallback_allowed": False,
            "credential_rotation_allowed": False,
            "source_artifact_mutation_allowed": False,
            "book_global_identity_mutation_allowed": False,
            "production_publish_enabled": False,
            "baseline_output_injected_into_prompt": False,
            "gold_or_oracle_loaded": False,
        },
        "output_root": str(output),
        "sealed_at": _now(),
    }
    seal = {**seal_body, "seal_hash": canonical_hash(seal_body)}
    _write_new_json(output / "run_seal.json", seal)

    result = call_openai_compatible_structured_v1(
        credential=credential,
        model_id=role.model_id,
        messages=request.messages,
        contract=contract,
        schema_name=SCHEMA_NAME,
        cache_path=output
        / "cache"
        / credential.quota_bucket_id
        / "event_batch.sqlite3",
        tag=f"b2_event_batch:{request.component_id}",
        prompt_token_cap=PROMPT_TOKEN_CAP,
        max_output_tokens=OUTPUT_TOKEN_CAP,
        temperature=1.0,
        seed=20260719,
        reasoning_effort="none",
        verbosity="low",
    )
    raw = {
        "schema_version": "literary_b2_event_batch_raw_result_v1",
        "batch_id": request.component_id,
        "component_ids": component_ids,
        "request_fingerprint": request.request_fingerprint,
        "provider": role.provider,
        "model": result.model,
        "quota_bucket_id": credential.quota_bucket_id,
        "credential_revision": credential.credential_revision,
        "credential_commitment": credential.commitment,
        "structured_output_contract": contract.to_payload(),
        "response_text": result.response_text,
        "parsed_json": (
            dict(result.parsed_json)
            if isinstance(result.parsed_json, Mapping)
            else None
        ),
        "json_error": result.json_error,
        "usage": dict(result.usage),
        "latency_ms": result.latency_ms,
        "cost_usd": None,
        "cost_provenance": "unknown_unpriced_adapter",
        "from_cache": result.from_cache,
        "cache_key": result.cache_key,
        "completed_at": _now(),
    }
    _write_new_json(output / "raw_result.json", raw)
    if result.parsed_json is None:
        raise EventBatchCanaryError(
            f"provider did not return valid JSON: {result.json_error}"
        )
    validate_structured_payload(
        result.parsed_json,
        canonical_schema=request.response_schema,
    )
    decision = validate_event_review_batch_response_v1(
        result.parsed_json,
        index=index,
        component_ids=component_ids,
        chapter_artifact=artifact,
        registry_ledger=ledger,
        request_fingerprint=request.request_fingerprint,
    )
    _write_new_json(output / "batch_decision.json", decision)
    for component in decision["component_decisions"]:
        _write_new_json(
            output / "components" / component["component_id"] / "decision.json",
            component,
        )

    usage = dict(result.usage)
    visible_tokens = _usage_value(usage, "prompt_tokens") + _usage_value(
        usage, "completion_tokens"
    )
    if visible_tokens > HARD_VISIBLE_TOKEN_CAP:
        raise EventBatchCanaryError("event batch exceeded the visible-token cap")
    comparison = _comparison(
        decisions=decision["component_decisions"],
        baseline=_baseline_actions(baseline_root),
        authority_hold_case_ids=[
            row["case_id"] for row in decision["relation_authority_holds"]
        ],
    )
    action_names = [
        str(action["action"])
        for component in decision["component_decisions"]
        for action in component["event_actions"]
    ]
    frozen_after = file_sha256(frozen).upper()
    if frozen_after != FROZEN_DB_SHA256:
        raise EventBatchCanaryError("frozen database changed during canary")
    report_body = {
        "schema_version": REPORT_SCHEMA_VERSION,
        "status": (
            "complete"
            if comparison["unheld_authority_increase_count"] == 0
            else "complete_requires_semantic_review"
        ),
        "mode": "live_one_call",
        **source_facts,
        "seal_hash": seal["seal_hash"],
        "component_count": len(decision["component_decisions"]),
        "case_count": len(action_names),
        "action_counts": {
            action: action_names.count(action) for action in sorted(set(action_names))
        },
        "observation_channel_counts": {
            channel: sum(
                1
                for row in decision["case_channels"]
                if row["observation_channel"] == channel
            )
            for channel in sorted(
                {row["observation_channel"] for row in decision["case_channels"]}
            )
        },
        "relation_authority_hold_count": len(
            decision["relation_authority_holds"]
        ),
        "relation_authority_holds": decision["relation_authority_holds"],
        "usage": usage,
        "visible_tokens": visible_tokens,
        "transport_prompt_estimate": transport_prompt_estimate,
        "provider_calls": 1,
        "retry_performed": False,
        "fallback_performed": False,
        "authority_published": False,
        "comparison_to_prior_run": comparison,
        "gold_or_oracle_loaded": False,
        "source_artifact_mutated": False,
        "production_publish_performed": False,
        "frozen_db_sha256_after": frozen_after,
        "completed_at": _now(),
    }
    report = {**report_body, "report_hash": canonical_hash(report_body)}
    _write_new_json(output / "report.json", report)
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("offline", "live"), required=True)
    parser.add_argument("--repo-root", type=Path, required=True)
    parser.add_argument("--recovery-index", type=Path, required=True)
    parser.add_argument("--chapter-artifact", type=Path, required=True)
    parser.add_argument("--registry-ledger", type=Path, required=True)
    parser.add_argument("--baseline-event-root", type=Path, required=True)
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument("--provider-profile", type=Path)
    parser.add_argument("--structured-output-policy", type=Path)
    parser.add_argument("--credential-root", type=Path)
    parser.add_argument("--frozen-db", type=Path, required=True)
    args = parser.parse_args()
    report = run(
        mode=args.mode,
        repo_root=args.repo_root,
        recovery_index_path=args.recovery_index,
        chapter_artifact_path=args.chapter_artifact,
        registry_ledger_path=args.registry_ledger,
        baseline_event_root=args.baseline_event_root,
        output_root=args.output_root,
        provider_profile_path=args.provider_profile,
        structured_output_policy_path=args.structured_output_policy,
        credential_root=args.credential_root,
        frozen_db=args.frozen_db,
    )
    print(canonical_json(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
