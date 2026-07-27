"""Run one sealed GPT-5.4 RegistryRecoveryBatchV1 canary."""

from __future__ import annotations

import argparse
from dataclasses import asdict
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
from pipeline.literary.b2_recovery_batch_v1 import (
    batch_request_payload_v1,
    render_registry_recovery_batch_request_v1,
    validate_registry_recovery_batch_response_v1,
)
from pipeline.literary.b2_recovery_v1 import (
    B2RecoveryContractError,
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


RUN_SEAL_SCHEMA_VERSION = "literary_b2_registry_batch_canary_seal_v1"
REPORT_SCHEMA_VERSION = "literary_b2_registry_batch_canary_report_v1"
ROLE_ID = "literary_local_conflict_auditor"
SCHEMA_NAME = "literary_b2_registry_recovery_batch_v1"
PROMPT_TOKEN_CAP = 20_000
OUTPUT_TOKEN_CAP = 12_000
HARD_VISIBLE_TOKEN_CAP = 32_000


class RegistryBatchCanaryError(RuntimeError):
    pass


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _read_object(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise RegistryBatchCanaryError(f"cannot read {label}: {path}") from exc
    if not isinstance(value, dict):
        raise RegistryBatchCanaryError(f"{label} must be an object")
    return value


def _write_new_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        raise RegistryBatchCanaryError(f"refusing to overwrite artifact: {path}")
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
        raise RegistryBatchCanaryError(f"provider usage {key} is invalid")
    return value


def _baseline_actions(
    baseline_root: Path | None,
    component_ids: Sequence[str],
) -> dict[str, str]:
    if baseline_root is None:
        return {}
    selected = set(component_ids)
    result: dict[str, str] = {}
    for path in sorted(baseline_root.glob("*/decision.json")):
        decision = _read_object(path, "baseline component decision")
        if decision.get("component_id") not in selected:
            continue
        for action in decision.get("ticket_actions") or []:
            ticket_id = str(action.get("ticket_id") or "")
            action_name = str(action.get("action") or "")
            if not ticket_id or not action_name or ticket_id in result:
                raise RegistryBatchCanaryError(
                    "baseline component decisions are malformed or repeated"
                )
            result[ticket_id] = action_name
    return result


def _comparison(
    *,
    decisions: Sequence[Mapping[str, Any]],
    baseline: Mapping[str, str],
) -> dict[str, Any]:
    observed = {
        str(action["ticket_id"]): str(action["action"])
        for decision in decisions
        for action in decision["ticket_actions"]
    }
    if not baseline:
        return {
            "baseline_available": False,
            "ticket_count": len(observed),
        }
    shared = sorted(set(observed).intersection(baseline))
    changed = [
        {
            "ticket_id": ticket_id,
            "baseline_action": baseline[ticket_id],
            "canary_action": observed[ticket_id],
        }
        for ticket_id in shared
        if baseline[ticket_id] != observed[ticket_id]
    ]
    return {
        "baseline_available": True,
        "baseline_ticket_count": len(baseline),
        "canary_ticket_count": len(observed),
        "shared_ticket_count": len(shared),
        "same_action_count": len(shared) - len(changed),
        "changed_action_count": len(changed),
        "changed_actions": changed,
    }


def run(
    *,
    repo_root: Path,
    recovery_index_path: Path,
    output_root: Path,
    provider_profile_path: Path,
    structured_output_policy_path: Path,
    credential_root: Path,
    frozen_db: Path,
    component_ids: Sequence[str],
    baseline_registry_root: Path | None,
) -> dict[str, Any]:
    repo = repo_root.resolve()
    index_path = recovery_index_path.resolve()
    output = output_root.resolve()
    profile_path = provider_profile_path.resolve()
    policy_path = structured_output_policy_path.resolve()
    frozen = frozen_db.resolve()
    if output.exists():
        raise RegistryBatchCanaryError("output root already exists")
    if file_sha256(frozen).upper() != FROZEN_DB_SHA256:
        raise RegistryBatchCanaryError("frozen database hash changed before canary")
    index = verify_b2_recovery_index_v1(
        _read_object(index_path, "B2 recovery index")
    )
    request = render_registry_recovery_batch_request_v1(
        index=index,
        component_ids=component_ids,
    )
    provider = load_provider_profile(profile_path)
    if set(provider.credentials) != {"openai-row2"}:
        raise RegistryBatchCanaryError(
            "canary provider profile is not sealed to OpenAI Key 2"
        )
    role = provider.roles.get(ROLE_ID)
    if role is None or role.bucket_order != ("openai-row2",):
        raise RegistryBatchCanaryError("canary role has fallback or route drift")
    if role.provider != "openai" or role.model_id != "gpt-5.4":
        raise RegistryBatchCanaryError("canary model differs from GPT-5.4")
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
        raise RegistryBatchCanaryError(
            "batch canary requires native Structured Output"
        )
    response_format = openai_response_format(
        contract,
        schema_name=SCHEMA_NAME,
    )
    prompt_estimate = estimate_prompt_tokens(
        [dict(row) for row in request.messages],
        response_format=response_format,
    )
    if prompt_estimate > PROMPT_TOKEN_CAP:
        raise RegistryBatchCanaryError(
            "batch prompt estimate exceeds the sealed 20k cap"
        )
    seal_body = {
        "schema_version": RUN_SEAL_SCHEMA_VERSION,
        "status": "sealed_before_api",
        "git_head": _git_head(repo),
        "source_recovery_index": str(index_path),
        "source_recovery_index_sha256": file_sha256(index_path),
        "recovery_index_hash": index["recovery_index_hash"],
        "chapter_id": index["chapter_id"],
        "batch_id": request.component_id,
        "component_ids": list(component_ids),
        "request_fingerprint": request.request_fingerprint,
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
            "estimated_prompt_tokens": prompt_estimate,
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
        "frozen_db_sha256_before": FROZEN_DB_SHA256,
        "output_root": str(output),
        "sealed_at": _now(),
    }
    seal = {**seal_body, "seal_hash": canonical_hash(seal_body)}
    output.mkdir(parents=True)
    _write_new_json(output / "run_seal.json", seal)
    _write_new_json(output / "request.json", batch_request_payload_v1(request))
    try:
        result = call_openai_compatible_structured_v1(
            credential=credential,
            model_id=role.model_id,
            messages=request.messages,
            contract=contract,
            schema_name=SCHEMA_NAME,
            cache_path=output
            / "cache"
            / credential.quota_bucket_id
            / "registry_batch.sqlite3",
            tag=f"b2_registry_batch:{request.component_id}",
            prompt_token_cap=PROMPT_TOKEN_CAP,
            max_output_tokens=OUTPUT_TOKEN_CAP,
            temperature=1.0,
            seed=20260719,
            reasoning_effort="none",
            verbosity="low",
        )
        raw = {
            "schema_version": "literary_b2_registry_batch_raw_result_v1",
            "batch_id": request.component_id,
            "component_ids": list(component_ids),
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
            raise RegistryBatchCanaryError(
                f"provider did not return valid JSON: {result.json_error}"
            )
        validate_structured_payload(
            result.parsed_json,
            canonical_schema=request.response_schema,
        )
        decision = validate_registry_recovery_batch_response_v1(
            result.parsed_json,
            index=index,
            component_ids=component_ids,
            request_fingerprint=request.request_fingerprint,
        )
        _write_new_json(output / "batch_decision.json", decision)
        for component in decision["component_decisions"]:
            _write_new_json(
                output
                / "components"
                / component["component_id"]
                / "decision.json",
                component,
            )
        usage = dict(result.usage)
        visible_tokens = (
            _usage_value(usage, "prompt_tokens")
            + _usage_value(usage, "completion_tokens")
        )
        if visible_tokens > HARD_VISIBLE_TOKEN_CAP:
            raise RegistryBatchCanaryError(
                "provider usage exceeded the sealed visible-token cap"
            )
        action_names = [
            str(action["action"])
            for component in decision["component_decisions"]
            for action in component["ticket_actions"]
        ]
        comparison = _comparison(
            decisions=decision["component_decisions"],
            baseline=_baseline_actions(
                baseline_registry_root.resolve()
                if baseline_registry_root is not None
                else None,
                component_ids,
            ),
        )
        frozen_after = file_sha256(frozen).upper()
        if frozen_after != FROZEN_DB_SHA256:
            raise RegistryBatchCanaryError(
                "frozen database changed during canary"
            )
        report_body = {
            "schema_version": REPORT_SCHEMA_VERSION,
            "status": "complete",
            "seal_hash": seal["seal_hash"],
            "batch_id": request.component_id,
            "component_ids": list(component_ids),
            "component_count": len(decision["component_decisions"]),
            "ticket_count": len(action_names),
            "action_counts": {
                action: action_names.count(action)
                for action in sorted(set(action_names))
            },
            "usage": usage,
            "visible_tokens": visible_tokens,
            "estimated_prompt_tokens": prompt_estimate,
            "provider_calls": 1,
            "retry_performed": False,
            "fallback_performed": False,
            "authority_published": False,
            "comparison_to_prior_run": comparison,
            "gold_or_oracle_loaded": False,
            "source_artifact_mutated": False,
            "book_global_identity_mutation_performed": False,
            "production_publish_performed": False,
            "frozen_db_sha256_after": frozen_after,
            "completed_at": _now(),
        }
        report = {**report_body, "report_hash": canonical_hash(report_body)}
        _write_new_json(output / "report.json", report)
        return report
    except Exception as exc:
        failure_body = {
            "schema_version": REPORT_SCHEMA_VERSION,
            "status": "halted_fail_closed",
            "seal_hash": seal["seal_hash"],
            "batch_id": request.component_id,
            "component_ids": list(component_ids),
            "error_type": type(exc).__name__,
            "error": str(exc),
            "provider_fallback_performed": False,
            "retry_performed": False,
            "authority_published": False,
            "source_artifact_mutated": False,
            "production_publish_performed": False,
            "frozen_db_sha256_after": file_sha256(frozen).upper(),
            "completed_at": _now(),
        }
        failure = {**failure_body, "report_hash": canonical_hash(failure_body)}
        if not (output / "report.json").exists():
            _write_new_json(output / "report.json", failure)
        raise


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo-root", type=Path, required=True)
    parser.add_argument("--recovery-index", type=Path, required=True)
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument("--provider-profile", type=Path, required=True)
    parser.add_argument("--structured-output-policy", type=Path, required=True)
    parser.add_argument("--credential-root", type=Path, required=True)
    parser.add_argument("--frozen-db", type=Path, required=True)
    parser.add_argument("--component-id", action="append", required=True)
    parser.add_argument("--baseline-registry-root", type=Path)
    args = parser.parse_args()
    report = run(
        repo_root=args.repo_root,
        recovery_index_path=args.recovery_index,
        output_root=args.output_root,
        provider_profile_path=args.provider_profile,
        structured_output_policy_path=args.structured_output_policy,
        credential_root=args.credential_root,
        frozen_db=args.frozen_db,
        component_ids=args.component_id,
        baseline_registry_root=args.baseline_registry_root,
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
