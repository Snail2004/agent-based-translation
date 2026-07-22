from __future__ import annotations

import copy
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import tempfile
from typing import Any, Mapping, Protocol, Sequence

from pipeline.eval.contracts_v1 import (
    CanonicalPolicy,
    ContractValidationError,
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
from pipeline.eval.llm_profiles_v1 import SF_BT_SEMANTIC_JUDGE_ROLE_ID
from pipeline.eval.method_executors_v1 import SharedEvaluationRoleCallV1
from pipeline.eval.scorer_prompts_v3 import (
    SF_BT_SEMANTIC_CANDIDATE_ID,
    SF_BT_SEMANTIC_PROMPT_SHA256,
    parse_sf_bt_semantic_response_v3,
    render_sf_bt_semantic_passages_v3,
)
from pipeline.eval.sf_bt_band_calibration_packet_v1 import (
    build_sf_bt_band_calibration_packet_v1,
    validate_sf_bt_band_calibration_packet_binding_v1,
)
from pipeline.eval.sf_bt_band_calibration_v1 import (
    SF_BT_BAND_CALIBRATION_SCORES,
    analyze_sf_bt_band_calibration,
    sf_bt_band_calibration_fixture_sha256,
    validate_sf_bt_band_calibration_fixture,
)
from pipeline.llm_backend import canonical_sha256 as shared_canonical_sha256
from pipeline.llm_backend import (
    validate_api_source,
    validate_capability_evidence,
    validate_pipeline_profile,
)


__all__ = [
    "EvaluationSfBtBandCalibrationRunResultV1",
    "build_sf_bt_band_calibration_plan_v1",
    "run_evaluation_sf_bt_band_calibration_v1",
    "validate_sf_bt_band_calibration_checkpoint_v1",
    "validate_sf_bt_band_calibration_plan_v1",
    "validate_sf_bt_band_calibration_result_v1",
]


_PLAN_SCHEMA_ID = "EvaluationSfBtBandCalibrationPlanV1"
_CHECKPOINT_SCHEMA_ID = "EvaluationSfBtBandCalibrationCheckpointV1"
_RESULT_SCHEMA_ID = "EvaluationSfBtBandCalibrationResultV1"
_SEMANTIC_CONTRACT_SCHEMA_ID = "EvaluationSfBtBandSemanticContractV1"
_ATTEMPT_SCHEMA_ID = "EvaluationSfBtBandAttemptBindingV1"
_SCHEMA_VERSION = "1.0.0"
_REPEAT_COUNT = 2
_ORIENTATION_CASE_COUNT = 5
_EXPECTED_CALL_COUNT = 35

_PLAN_POLICY = CanonicalPolicy(
    set_like_paths=frozenset(),
    semantic_sequence_paths=frozenset(
        {
            ("binding", "case_ids"),
            ("binding", "orientation_case_ids"),
            ("calls",),
        }
    ),
)
_CHECKPOINT_POLICY = CanonicalPolicy(
    set_like_paths=frozenset(),
    semantic_sequence_paths=frozenset({("semantic_output", "flags")}),
)
_RESULT_POLICY = CanonicalPolicy(
    set_like_paths=frozenset(),
    semantic_sequence_paths=frozenset(
        {
            ("binding", "case_ids"),
            ("binding", "orientation_case_ids"),
            ("binding", "attempt_run_ids"),
            ("round_analyses",),
            ("round_analyses", "*", "analysis", "judge_contract", "allowed_scores"),
            ("round_analyses", "*", "analysis", "per_expected_band"),
            ("round_analyses", "*", "analysis", "confusion_matrix"),
            ("round_analyses", "*", "analysis", "predicted_distribution"),
            ("round_analyses", "*", "analysis", "case_results"),
            (
                "round_analyses",
                "*",
                "analysis",
                "case_results",
                "*",
                "flags",
            ),
            ("repeatability", "absolute_delta_distribution"),
            ("repeatability", "rows"),
            ("orientation_screen", "rows"),
            ("observations",),
            ("observations", "*", "flags"),
        }
    ),
)


class EvaluationBandCalibrationRoleRunnerV1(Protocol):
    @property
    def execution_binding(self) -> Mapping[str, str]: ...

    @property
    def semantic_contract(self) -> Mapping[str, Any]: ...

    @property
    def attempt_runtime_binding(self) -> Mapping[str, Any]: ...

    @property
    def cache_mode(self) -> str: ...

    def execute(
        self,
        *,
        role_id: str,
        scorer_input_packet_sha256: str,
        rendered_prompt,
        stage_id: str,
        logical_request_id: str,
        extra_bindings: Sequence[Mapping[str, str]] = (),
    ) -> SharedEvaluationRoleCallV1: ...


@dataclass(frozen=True, slots=True)
class EvaluationSfBtBandCalibrationRunResultV1:
    output_root: Path
    plan_path: Path
    result_path: Path | None
    result: dict[str, Any] | None
    reused_checkpoint_count: int
    created_checkpoint_count: int
    remaining_call_count: int
    attempt_run_id: str | None


def run_evaluation_sf_bt_band_calibration_v1(
    fixture_payload: Mapping[str, Any],
    role_runner: EvaluationBandCalibrationRoleRunnerV1,
    output_root: Path,
    *,
    created_at: str,
    producer_code_commit: str,
    max_new_calls: int | None = None,
) -> EvaluationSfBtBandCalibrationRunResultV1:
    """Run or resume one sealed 35-call calibration experiment.

    A later invocation may supply another physical API row only when the
    logical run and row-independent semantic contract are byte-equivalent.
    The runner never rotates credentials or retries a failed semantic call.
    """

    fixture = validate_sf_bt_band_calibration_fixture(fixture_payload)
    if fixture["review_status"] != "approved_independent_semantic_review":
        raise ContractValidationError(
            "fixture_review",
            "$.review_status",
            "live calibration requires the independently approved fixture",
        )
    runner = _validate_role_runner(role_runner)
    timestamp = require_rfc3339(created_at, path="$.created_at")
    commit = require_commit(producer_code_commit, path="$.producer_code_commit")
    invocation_call_cap = _normalize_invocation_call_cap(max_new_calls)
    root = Path(output_root).resolve()
    root.mkdir(parents=True, exist_ok=True)
    _persist_bytes_create_or_equal(
        root / ".gitattributes", b"*.json text eol=lf\n*.md text eol=lf\n"
    )

    semantic_contract = _load_or_create_semantic_contract(
        root,
        runner,
        created_at=timestamp,
        producer_code_commit=commit,
    )
    plan_path = root / "plan.json"
    plan = _load_or_create_plan(
        plan_path,
        fixture,
        runner,
        semantic_contract,
        created_at=timestamp,
        producer_code_commit=commit,
    )
    checkpoints = _load_checkpoints(
        root,
        fixture,
        plan,
        semantic_contract,
    )
    reused_count = len(checkpoints)
    result_path = root / "result.json"
    if result_path.exists():
        result = validate_sf_bt_band_calibration_result_v1(
            _load_json_object(result_path)
        )
        expected = _build_result(fixture, plan, checkpoints)
        if result != expected:
            raise ContractValidationError(
                "result_binding",
                str(result_path),
                "persisted result differs from its exact fixture and checkpoints",
            )
        return EvaluationSfBtBandCalibrationRunResultV1(
            output_root=root,
            plan_path=plan_path,
            result_path=result_path,
            result=result,
            reused_checkpoint_count=_EXPECTED_CALL_COUNT,
            created_checkpoint_count=0,
            remaining_call_count=0,
            attempt_run_id=None,
        )

    if len(checkpoints) == _EXPECTED_CALL_COUNT:
        result = _build_result(fixture, plan, checkpoints)
        _persist_create_or_equal(result_path, result, policy=_RESULT_POLICY)
        return EvaluationSfBtBandCalibrationRunResultV1(
            output_root=root,
            plan_path=plan_path,
            result_path=result_path,
            result=result,
            reused_checkpoint_count=reused_count,
            created_checkpoint_count=0,
            remaining_call_count=0,
            attempt_run_id=None,
        )

    attempt = _persist_attempt_binding(
        root,
        runner,
        semantic_contract,
        created_at=timestamp,
        producer_code_commit=commit,
        max_new_calls=invocation_call_cap,
    )
    created_count = 0
    for call_spec in plan["calls"]:
        call_id = call_spec["call_id"]
        if call_id in checkpoints:
            continue
        if created_count >= invocation_call_cap:
            break
        checkpoint = _execute_call(
            fixture,
            plan,
            call_spec,
            runner,
            created_at=plan["created_at"],
            producer_code_commit=plan["producer"]["code_commit"],
        )
        _persist_checkpoint(root, checkpoint)
        checkpoints[call_id] = checkpoint
        created_count += 1

    remaining_count = _EXPECTED_CALL_COUNT - len(checkpoints)
    if remaining_count:
        return EvaluationSfBtBandCalibrationRunResultV1(
            output_root=root,
            plan_path=plan_path,
            result_path=None,
            result=None,
            reused_checkpoint_count=reused_count,
            created_checkpoint_count=created_count,
            remaining_call_count=remaining_count,
            attempt_run_id=attempt["attempt_run_id"],
        )

    if len(checkpoints) != _EXPECTED_CALL_COUNT:
        raise ContractValidationError(
            "checkpoint_exact_cover",
            str(root / "checkpoints"),
            "calibration stopped without exact checkpoint coverage",
        )
    result = _build_result(fixture, plan, checkpoints)
    _persist_create_or_equal(result_path, result, policy=_RESULT_POLICY)
    return EvaluationSfBtBandCalibrationRunResultV1(
        output_root=root,
        plan_path=plan_path,
        result_path=result_path,
        result=result,
        reused_checkpoint_count=reused_count,
        created_checkpoint_count=created_count,
        remaining_call_count=0,
        attempt_run_id=attempt["attempt_run_id"],
    )


def build_sf_bt_band_calibration_plan_v1(
    fixture_payload: Mapping[str, Any],
    *,
    logical_run_id: str,
    semantic_contract_sha256: str,
    created_at: str,
    producer_code_commit: str,
) -> dict[str, Any]:
    fixture = validate_sf_bt_band_calibration_fixture(fixture_payload)
    if fixture["review_status"] != "approved_independent_semantic_review":
        raise ContractValidationError(
            "fixture_review", "$.review_status", "plan requires approved fixture"
        )
    run_id = require_string(logical_run_id, path="$.logical_run_id")
    contract_hash = require_sha256(
        semantic_contract_sha256, path="$.semantic_contract_sha256"
    )
    timestamp = require_rfc3339(created_at, path="$.created_at")
    commit = require_commit(producer_code_commit, path="$.producer_code_commit")
    fixture_hash = sf_bt_band_calibration_fixture_sha256(fixture)
    case_ids = [row["case_id"] for row in fixture["cases"]]
    orientation_case_ids = _select_orientation_cases(fixture["cases"])
    calls = _build_call_specs(case_ids, orientation_case_ids)
    payload = {
        "schema_id": _PLAN_SCHEMA_ID,
        "schema_version": _SCHEMA_VERSION,
        "plan_id": "sfbt-band-plan-" + _digest(fixture_hash, run_id)[:24],
        "created_at": timestamp,
        "producer": _producer(commit, "sf_bt_band_calibration_live_runner_v1"),
        "binding": {
            "fixture_set_id": fixture["fixture_set_id"],
            "fixture_sha256": fixture_hash,
            "logical_run_id": run_id,
            "semantic_contract_sha256": contract_hash,
            "role_id": SF_BT_SEMANTIC_JUDGE_ROLE_ID,
            "prompt_candidate_id": SF_BT_SEMANTIC_CANDIDATE_ID,
            "prompt_sha256": SF_BT_SEMANTIC_PROMPT_SHA256,
            "case_ids": case_ids,
            "orientation_case_ids": orientation_case_ids,
        },
        "execution_policy": {
            "primary_repeat_count": _REPEAT_COUNT,
            "orientation_case_count": _ORIENTATION_CASE_COUNT,
            "expected_accepted_call_count": _EXPECTED_CALL_COUNT,
            "cache_mode": "bypass",
            "automatic_retry": False,
            "provider_fallback": False,
        },
        "calls": calls,
        "integrity": {"plan_sha256": "0" * 64},
    }
    return validate_sf_bt_band_calibration_plan_v1(
        seal_payload(
            payload,
            policy=_PLAN_POLICY,
            hash_path=("integrity", "plan_sha256"),
        )
    )


def validate_sf_bt_band_calibration_plan_v1(
    payload: Mapping[str, Any],
) -> dict[str, Any]:
    root = require_mapping(payload, path="$")
    require_exact_keys(
        root,
        required={
            "schema_id",
            "schema_version",
            "plan_id",
            "created_at",
            "producer",
            "binding",
            "execution_policy",
            "calls",
            "integrity",
        },
        path="$",
    )
    normalized = {
        "schema_id": require_enum(
            root["schema_id"], {_PLAN_SCHEMA_ID}, path="$.schema_id"
        ),
        "schema_version": require_enum(
            root["schema_version"], {_SCHEMA_VERSION}, path="$.schema_version"
        ),
        "plan_id": require_string(root["plan_id"], path="$.plan_id"),
        "created_at": require_rfc3339(root["created_at"], path="$.created_at"),
        "producer": validate_producer(
            root["producer"], path="$.producer", workstream="evaluation"
        ),
        "binding": _validate_plan_binding(root["binding"]),
        "execution_policy": _validate_execution_policy(root["execution_policy"]),
        "calls": _validate_call_specs(root["calls"]),
        "integrity": _validate_integrity(
            root["integrity"], field="plan_sha256", path="$.integrity"
        ),
    }
    _validate_plan_shape(normalized)
    if not verify_payload_hash(
        normalized,
        policy=_PLAN_POLICY,
        hash_path=("integrity", "plan_sha256"),
    ):
        raise ContractValidationError(
            "plan_hash", "$.integrity.plan_sha256", "plan self-hash does not match"
        )
    return _canonical(normalized, _PLAN_POLICY)


def validate_sf_bt_band_calibration_checkpoint_v1(
    payload: Mapping[str, Any],
) -> dict[str, Any]:
    root = require_mapping(payload, path="$")
    require_exact_keys(
        root,
        required={
            "schema_id",
            "schema_version",
            "created_at",
            "producer",
            "binding",
            "call_evidence",
            "semantic_output",
            "integrity",
        },
        path="$",
    )
    normalized = {
        "schema_id": require_enum(
            root["schema_id"], {_CHECKPOINT_SCHEMA_ID}, path="$.schema_id"
        ),
        "schema_version": require_enum(
            root["schema_version"], {_SCHEMA_VERSION}, path="$.schema_version"
        ),
        "created_at": require_rfc3339(root["created_at"], path="$.created_at"),
        "producer": validate_producer(
            root["producer"], path="$.producer", workstream="evaluation"
        ),
        "binding": _validate_checkpoint_binding(root["binding"]),
        "call_evidence": _validate_call_evidence(root["call_evidence"]),
        "semantic_output": _validate_semantic_output(root["semantic_output"]),
        "integrity": _validate_integrity(
            root["integrity"], field="checkpoint_sha256", path="$.integrity"
        ),
    }
    if not verify_payload_hash(
        normalized,
        policy=_CHECKPOINT_POLICY,
        hash_path=("integrity", "checkpoint_sha256"),
    ):
        raise ContractValidationError(
            "checkpoint_hash",
            "$.integrity.checkpoint_sha256",
            "checkpoint self-hash does not match",
        )
    return _canonical(normalized, _CHECKPOINT_POLICY)


def validate_sf_bt_band_calibration_result_v1(
    payload: Mapping[str, Any],
) -> dict[str, Any]:
    root = require_mapping(payload, path="$")
    require_exact_keys(
        root,
        required={
            "schema_id",
            "schema_version",
            "created_at",
            "producer",
            "binding",
            "interpretation",
            "round_analyses",
            "repeatability",
            "orientation_screen",
            "observations",
            "integrity",
        },
        path="$",
    )
    normalized = copy.deepcopy(dict(root))
    require_enum(normalized["schema_id"], {_RESULT_SCHEMA_ID}, path="$.schema_id")
    require_enum(
        normalized["schema_version"], {_SCHEMA_VERSION}, path="$.schema_version"
    )
    require_rfc3339(normalized["created_at"], path="$.created_at")
    validate_producer(normalized["producer"], path="$.producer", workstream="evaluation")
    require_enum(
        normalized["interpretation"],
        {"measurement_only_not_a_calibration_pass"},
        path="$.interpretation",
    )
    _validate_integrity(
        normalized["integrity"], field="result_sha256", path="$.integrity"
    )
    if not verify_payload_hash(
        normalized,
        policy=_RESULT_POLICY,
        hash_path=("integrity", "result_sha256"),
    ):
        raise ContractValidationError(
            "result_hash", "$.integrity.result_sha256", "result self-hash does not match"
        )
    return _canonical(normalized, _RESULT_POLICY)


def _validate_role_runner(
    runner: EvaluationBandCalibrationRoleRunnerV1,
) -> EvaluationBandCalibrationRoleRunnerV1:
    binding = require_mapping(runner.execution_binding, path="$.role_runner.binding")
    require_exact_keys(
        binding,
        required={
            "evaluation_logical_run_id",
            "evaluation_attempt_run_id",
            "evaluation_profile_id",
            "evaluation_profile_sha256",
        },
        path="$.role_runner.binding",
    )
    require_sha256(
        binding["evaluation_profile_sha256"],
        path="$.role_runner.binding.evaluation_profile_sha256",
    )
    if runner.cache_mode != "bypass":
        raise ContractValidationError(
            "cache_mode",
            "$.role_runner.cache_mode",
            "repeatability calibration requires cache bypass",
        )
    contract = require_mapping(
        runner.semantic_contract, path="$.role_runner.semantic_contract"
    )
    roles = require_list(contract.get("roles"), path="$.role_runner.semantic_contract.roles")
    if [row.get("role_id") for row in roles if isinstance(row, Mapping)] != [
        SF_BT_SEMANTIC_JUDGE_ROLE_ID
    ]:
        raise ContractValidationError(
            "semantic_roles",
            "$.role_runner.semantic_contract.roles",
            "calibration profile must contain only the SF-BT semantic judge role",
        )
    return runner


def _load_or_create_semantic_contract(
    root: Path,
    runner: EvaluationBandCalibrationRoleRunnerV1,
    *,
    created_at: str,
    producer_code_commit: str,
) -> dict[str, Any]:
    path = root / "semantic_contract.json"
    contract = copy.deepcopy(dict(runner.semantic_contract))
    contract_hash = shared_canonical_sha256(contract)
    run_id = str(runner.execution_binding["evaluation_logical_run_id"])
    if path.exists():
        artifact = _validate_semantic_contract_artifact(_load_json_object(path))
    else:
        artifact = _seal_opaque(
            {
                "schema_id": _SEMANTIC_CONTRACT_SCHEMA_ID,
                "schema_version": _SCHEMA_VERSION,
                "created_at": created_at,
                "producer": _producer(
                    producer_code_commit, "sf_bt_band_calibration_live_runner_v1"
                ),
                "logical_run_id": run_id,
                "semantic_contract": contract,
                "semantic_contract_sha256": contract_hash,
                "integrity": {"artifact_sha256": "0" * 64},
            }
        )
        artifact = _validate_semantic_contract_artifact(artifact)
        _persist_opaque_create_or_equal(path, artifact)
    if (
        artifact["logical_run_id"] != run_id
        or artifact["semantic_contract_sha256"] != contract_hash
        or artifact["semantic_contract"] != contract
    ):
        raise ContractValidationError(
            "semantic_contract_drift",
            str(path),
            "resume changes model, prompt, schema, generation settings, or route family",
        )
    return artifact


def _load_or_create_plan(
    path: Path,
    fixture: Mapping[str, Any],
    runner: EvaluationBandCalibrationRoleRunnerV1,
    semantic_contract: Mapping[str, Any],
    *,
    created_at: str,
    producer_code_commit: str,
) -> dict[str, Any]:
    if path.exists():
        plan = validate_sf_bt_band_calibration_plan_v1(_load_json_object(path))
        expected = build_sf_bt_band_calibration_plan_v1(
            fixture,
            logical_run_id=str(
                runner.execution_binding["evaluation_logical_run_id"]
            ),
            semantic_contract_sha256=semantic_contract["semantic_contract_sha256"],
            created_at=plan["created_at"],
            producer_code_commit=plan["producer"]["code_commit"],
        )
        if plan != expected:
            raise ContractValidationError(
                "plan_binding", str(path), "persisted plan differs from current inputs"
            )
        return plan
    plan = build_sf_bt_band_calibration_plan_v1(
        fixture,
        logical_run_id=str(runner.execution_binding["evaluation_logical_run_id"]),
        semantic_contract_sha256=semantic_contract["semantic_contract_sha256"],
        created_at=created_at,
        producer_code_commit=producer_code_commit,
    )
    _persist_create_or_equal(path, plan, policy=_PLAN_POLICY)
    return plan


def _persist_attempt_binding(
    root: Path,
    runner: EvaluationBandCalibrationRoleRunnerV1,
    semantic_contract: Mapping[str, Any],
    *,
    created_at: str,
    producer_code_commit: str,
    max_new_calls: int,
) -> dict[str, Any]:
    execution = runner.execution_binding
    runtime_binding = _validate_attempt_runtime_binding(runner.attempt_runtime_binding)
    invocation_policy = _build_invocation_policy(
        max_new_calls, runtime_binding["profile"]
    )
    if (
        runtime_binding["semantic_contract_sha256"]
        != semantic_contract["semantic_contract_sha256"]
        or shared_canonical_sha256(runtime_binding["profile"])
        != execution["evaluation_profile_sha256"]
    ):
        raise ContractValidationError(
            "attempt_binding",
            "$.role_runner.attempt_runtime_binding",
            "attempt runtime binding differs from execution or semantic contract",
        )
    artifact = _seal_opaque(
        {
            "schema_id": _ATTEMPT_SCHEMA_ID,
            "schema_version": _SCHEMA_VERSION,
            "created_at": created_at,
            "producer": _producer(
                producer_code_commit, "sf_bt_band_calibration_live_runner_v1"
            ),
            "logical_run_id": execution["evaluation_logical_run_id"],
            "attempt_run_id": execution["evaluation_attempt_run_id"],
            "profile_id": execution["evaluation_profile_id"],
            "profile_sha256": execution["evaluation_profile_sha256"],
            "semantic_contract_sha256": semantic_contract[
                "semantic_contract_sha256"
            ],
            "invocation_policy": invocation_policy,
            "runtime_binding": runtime_binding,
            "integrity": {"artifact_sha256": "0" * 64},
        }
    )
    artifact = _validate_attempt_artifact(artifact)
    path = root / "attempts" / f"{artifact['attempt_run_id']}.json"
    _persist_opaque_create_or_equal(path, artifact)
    return artifact


def _execute_call(
    fixture: Mapping[str, Any],
    plan: Mapping[str, Any],
    call_spec: Mapping[str, Any],
    runner: EvaluationBandCalibrationRoleRunnerV1,
    *,
    created_at: str,
    producer_code_commit: str,
) -> dict[str, Any]:
    packet = build_sf_bt_band_calibration_packet_v1(
        fixture,
        case_id=call_spec["case_id"],
        orientation=call_spec["orientation"],
        created_at=created_at,
        producer_code_commit=producer_code_commit,
    )
    validate_sf_bt_band_calibration_packet_binding_v1(packet, fixture)
    passages = {row["slot_id"]: row["text"] for row in packet["passages"]}
    prompt = render_sf_bt_semantic_passages_v3(
        passage_a=passages["passage_a"], passage_b=passages["passage_b"]
    )
    _assert_oracle_not_in_prompt(fixture, packet, prompt.rendered_prompt)
    request_id = "sfbt_band_" + call_spec["call_id"]
    call = runner.execute(
        role_id=SF_BT_SEMANTIC_JUDGE_ROLE_ID,
        scorer_input_packet_sha256=packet["integrity"]["packet_sha256"],
        rendered_prompt=prompt,
        stage_id=request_id,
        logical_request_id=request_id,
        extra_bindings=(
            {
                "name": "sf_bt_band_fixture",
                "sha256": plan["binding"]["fixture_sha256"],
            },
            {
                "name": "sf_bt_band_plan",
                "sha256": plan["integrity"]["plan_sha256"],
            },
        ),
    )
    _require_accepted(call)
    return _build_checkpoint(
        plan,
        call_spec,
        packet,
        prompt.rendered_prompt_sha256,
        runner,
        call,
        created_at=created_at,
        producer_code_commit=producer_code_commit,
    )


def _build_checkpoint(
    plan: Mapping[str, Any],
    call_spec: Mapping[str, Any],
    packet: Mapping[str, Any],
    rendered_prompt_sha256: str,
    runner: EvaluationBandCalibrationRoleRunnerV1,
    call: SharedEvaluationRoleCallV1,
    *,
    created_at: str,
    producer_code_commit: str,
) -> dict[str, Any]:
    outcome = call.outcome
    usage = outcome.get("usage")
    cache = outcome.get("cache_observation")
    payload = {
        "schema_id": _CHECKPOINT_SCHEMA_ID,
        "schema_version": _SCHEMA_VERSION,
        "created_at": created_at,
        "producer": _producer(
            producer_code_commit, "sf_bt_band_calibration_live_runner_v1"
        ),
        "binding": {
            "plan_sha256": plan["integrity"]["plan_sha256"],
            "fixture_sha256": plan["binding"]["fixture_sha256"],
            "logical_run_id": plan["binding"]["logical_run_id"],
            "semantic_contract_sha256": plan["binding"][
                "semantic_contract_sha256"
            ],
            "call_id": call_spec["call_id"],
            "case_id": call_spec["case_id"],
            "observation_kind": call_spec["observation_kind"],
            "replicate_index": call_spec["replicate_index"],
            "orientation": call_spec["orientation"],
            "presentation_id": packet["binding"]["presentation_id"],
            "packet_sha256": packet["integrity"]["packet_sha256"],
            "rendered_prompt_sha256": rendered_prompt_sha256,
            "attempt_run_id": runner.execution_binding[
                "evaluation_attempt_run_id"
            ],
            "profile_sha256": runner.execution_binding[
                "evaluation_profile_sha256"
            ],
            "role_id": call.seal["role_id"],
        },
        "call_evidence": {
            "seal_sha256": call.seal["seal_sha256"],
            "backend_status": outcome["backend_status"],
            "provider_called": bool(outcome["provider_called"]),
            "response_artifact_sha256": outcome["response_artifact_sha256"],
            "attempt_usage_id": (
                usage.get("attempt_usage_id") if isinstance(usage, Mapping) else None
            ),
            "cache_observation_id": (
                cache.get("observation_id") if isinstance(cache, Mapping) else None
            ),
        },
        "semantic_output": copy.deepcopy(dict(outcome["semantic_output"])),
        "integrity": {"checkpoint_sha256": "0" * 64},
    }
    return validate_sf_bt_band_calibration_checkpoint_v1(
        seal_payload(
            payload,
            policy=_CHECKPOINT_POLICY,
            hash_path=("integrity", "checkpoint_sha256"),
        )
    )


def _build_result(
    fixture: Mapping[str, Any],
    plan: Mapping[str, Any],
    checkpoints: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any]:
    if set(checkpoints) != {row["call_id"] for row in plan["calls"]}:
        raise ContractValidationError(
            "checkpoint_exact_cover",
            "$.checkpoints",
            "result requires exact planned checkpoint coverage",
        )
    by_call = {row["call_id"]: checkpoints[row["call_id"]] for row in plan["calls"]}
    responses_by_round: dict[int, dict[str, str]] = {1: {}, 2: {}}
    observations = []
    attempt_run_ids: list[str] = []
    expected_by_case = {row["case_id"]: row["expected_score"] for row in fixture["cases"]}
    for call_spec in plan["calls"]:
        checkpoint = by_call[call_spec["call_id"]]
        output = checkpoint["semantic_output"]
        if call_spec["observation_kind"] == "primary_repeat":
            responses_by_round[call_spec["replicate_index"]][call_spec["case_id"]] = (
                json.dumps(output, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
            )
        attempt_id = checkpoint["binding"]["attempt_run_id"]
        if attempt_id not in attempt_run_ids:
            attempt_run_ids.append(attempt_id)
        observations.append(
            {
                **copy.deepcopy(dict(call_spec)),
                "presentation_id": checkpoint["binding"]["presentation_id"],
                "expected_score": expected_by_case[call_spec["case_id"]],
                "predicted_score": output["score"],
                "flags": copy.deepcopy(list(output["flags"])),
                "note": output["note"],
                "attempt_run_id": attempt_id,
                "attempt_usage_id": checkpoint["call_evidence"]["attempt_usage_id"],
                "checkpoint_sha256": checkpoint["integrity"][
                    "checkpoint_sha256"
                ],
            }
        )
    round_analyses = [
        {
            "replicate_index": index,
            "analysis": analyze_sf_bt_band_calibration(
                fixture, responses_by_round[index]
            ),
        }
        for index in (1, 2)
    ]
    repeatability = _build_repeatability(plan, by_call)
    orientation_screen = _build_orientation_screen(plan, by_call)
    payload = {
        "schema_id": _RESULT_SCHEMA_ID,
        "schema_version": _SCHEMA_VERSION,
        "created_at": plan["created_at"],
        "producer": _producer(
            plan["producer"]["code_commit"],
            "sf_bt_band_calibration_live_runner_v1",
        ),
        "binding": {
            "plan_id": plan["plan_id"],
            "plan_sha256": plan["integrity"]["plan_sha256"],
            "fixture_set_id": plan["binding"]["fixture_set_id"],
            "fixture_sha256": plan["binding"]["fixture_sha256"],
            "logical_run_id": plan["binding"]["logical_run_id"],
            "semantic_contract_sha256": plan["binding"][
                "semantic_contract_sha256"
            ],
            "case_ids": copy.deepcopy(plan["binding"]["case_ids"]),
            "orientation_case_ids": copy.deepcopy(
                plan["binding"]["orientation_case_ids"]
            ),
            "attempt_run_ids": attempt_run_ids,
            "checkpoint_count": len(checkpoints),
        },
        "interpretation": "measurement_only_not_a_calibration_pass",
        "round_analyses": round_analyses,
        "repeatability": repeatability,
        "orientation_screen": orientation_screen,
        "observations": observations,
        "integrity": {"result_sha256": "0" * 64},
    }
    return validate_sf_bt_band_calibration_result_v1(
        seal_payload(
            payload,
            policy=_RESULT_POLICY,
            hash_path=("integrity", "result_sha256"),
        )
    )


def _build_repeatability(
    plan: Mapping[str, Any], checkpoints: Mapping[str, Mapping[str, Any]]
) -> dict[str, Any]:
    rows = []
    for case_id in plan["binding"]["case_ids"]:
        first = checkpoints[f"{case_id}__repeat_1"]["semantic_output"]["score"]
        second = checkpoints[f"{case_id}__repeat_2"]["semantic_output"]["score"]
        delta = abs(first - second)
        rows.append(
            {
                "case_id": case_id,
                "repeat_1_score": first,
                "repeat_2_score": second,
                "absolute_point_delta": delta,
                "exact_agreement": first == second,
                "within_one_band": delta <= 25,
            }
        )
    exact = sum(row["exact_agreement"] for row in rows)
    within = sum(row["within_one_band"] for row in rows)
    return {
        "case_count": len(rows),
        "exact_agreement_count": exact,
        "exact_agreement_rate": exact / len(rows),
        "within_one_band_count": within,
        "within_one_band_rate": within / len(rows),
        "mean_absolute_point_delta": sum(
            row["absolute_point_delta"] for row in rows
        )
        / len(rows),
        "maximum_absolute_point_delta": max(
            row["absolute_point_delta"] for row in rows
        ),
        "absolute_delta_distribution": [
            {
                "absolute_point_delta": delta,
                "count": sum(row["absolute_point_delta"] == delta for row in rows),
            }
            for delta in SF_BT_BAND_CALIBRATION_SCORES
        ],
        "rows": rows,
    }


def _build_orientation_screen(
    plan: Mapping[str, Any], checkpoints: Mapping[str, Mapping[str, Any]]
) -> dict[str, Any]:
    rows = []
    for case_id in plan["binding"]["orientation_case_ids"]:
        first = checkpoints[f"{case_id}__repeat_1"]["semantic_output"]["score"]
        second = checkpoints[f"{case_id}__repeat_2"]["semantic_output"]["score"]
        reversed_score = checkpoints[f"{case_id}__orientation_reverse"][
            "semantic_output"
        ]["score"]
        delta = abs(first - reversed_score)
        low, high = sorted((first, second))
        rows.append(
            {
                "case_id": case_id,
                "canonical_repeat_1_score": first,
                "canonical_repeat_2_score": second,
                "reversed_score": reversed_score,
                "absolute_delta_vs_repeat_1": delta,
                "exact_vs_repeat_1": reversed_score == first,
                "within_one_band_vs_repeat_1": delta <= 25,
                "outside_canonical_repeat_range": not low <= reversed_score <= high,
            }
        )
    exact = sum(row["exact_vs_repeat_1"] for row in rows)
    within = sum(row["within_one_band_vs_repeat_1"] for row in rows)
    outside = sum(row["outside_canonical_repeat_range"] for row in rows)
    return {
        "interpretation": (
            "screen_only_orientation_and_stochastic_variance_are_not_identified_separately"
        ),
        "case_count": len(rows),
        "exact_vs_repeat_1_count": exact,
        "exact_vs_repeat_1_rate": exact / len(rows),
        "within_one_band_vs_repeat_1_count": within,
        "within_one_band_vs_repeat_1_rate": within / len(rows),
        "outside_canonical_repeat_range_count": outside,
        "rows": rows,
    }


def _load_checkpoints(
    root: Path,
    fixture: Mapping[str, Any],
    plan: Mapping[str, Any],
    semantic_contract: Mapping[str, Any],
) -> dict[str, dict[str, Any]]:
    directory = root / "checkpoints"
    directory.mkdir(parents=True, exist_ok=True)
    expected_specs = {row["call_id"]: row for row in plan["calls"]}
    rows: dict[str, dict[str, Any]] = {}
    for path in sorted(directory.glob("*.json")):
        checkpoint = validate_sf_bt_band_calibration_checkpoint_v1(
            _load_json_object(path)
        )
        call_id = checkpoint["binding"]["call_id"]
        if path.stem != call_id or call_id not in expected_specs or call_id in rows:
            raise ContractValidationError(
                "checkpoint_identity",
                str(path),
                "checkpoint filename or call identity is foreign or duplicated",
            )
        _require_checkpoint_binding(
            root,
            checkpoint,
            expected_specs[call_id],
            fixture,
            plan,
            semantic_contract,
        )
        rows[call_id] = checkpoint
    return rows


def _require_checkpoint_binding(
    root: Path,
    checkpoint: Mapping[str, Any],
    call_spec: Mapping[str, Any],
    fixture: Mapping[str, Any],
    plan: Mapping[str, Any],
    semantic_contract: Mapping[str, Any],
) -> None:
    packet = build_sf_bt_band_calibration_packet_v1(
        fixture,
        case_id=call_spec["case_id"],
        orientation=call_spec["orientation"],
        created_at=plan["created_at"],
        producer_code_commit=plan["producer"]["code_commit"],
    )
    passages = {row["slot_id"]: row["text"] for row in packet["passages"]}
    prompt = render_sf_bt_semantic_passages_v3(
        passage_a=passages["passage_a"], passage_b=passages["passage_b"]
    )
    binding = checkpoint["binding"]
    expected = {
        "plan_sha256": plan["integrity"]["plan_sha256"],
        "fixture_sha256": plan["binding"]["fixture_sha256"],
        "logical_run_id": plan["binding"]["logical_run_id"],
        "semantic_contract_sha256": semantic_contract["semantic_contract_sha256"],
        "call_id": call_spec["call_id"],
        "case_id": call_spec["case_id"],
        "observation_kind": call_spec["observation_kind"],
        "replicate_index": call_spec["replicate_index"],
        "orientation": call_spec["orientation"],
        "presentation_id": packet["binding"]["presentation_id"],
        "packet_sha256": packet["integrity"]["packet_sha256"],
        "rendered_prompt_sha256": prompt.rendered_prompt_sha256,
        "role_id": SF_BT_SEMANTIC_JUDGE_ROLE_ID,
    }
    if any(binding[key] != value for key, value in expected.items()):
        raise ContractValidationError(
            "checkpoint_binding",
            "$.checkpoint.binding",
            "checkpoint differs from reconstructed plan, packet, or prompt",
        )
    attempt_path = root / "attempts" / f"{binding['attempt_run_id']}.json"
    if not attempt_path.exists():
        raise ContractValidationError(
            "attempt_reference", str(attempt_path), "checkpoint attempt artifact is absent"
        )
    attempt = _validate_attempt_artifact(_load_json_object(attempt_path))
    if (
        attempt["logical_run_id"] != binding["logical_run_id"]
        or attempt["attempt_run_id"] != binding["attempt_run_id"]
        or attempt["profile_sha256"] != binding["profile_sha256"]
        or attempt["semantic_contract_sha256"]
        != binding["semantic_contract_sha256"]
    ):
        raise ContractValidationError(
            "checkpoint_attempt_binding",
            str(attempt_path),
            "checkpoint differs from attempt-scoped execution evidence",
        )


def _validate_semantic_contract_artifact(payload: Mapping[str, Any]) -> dict[str, Any]:
    root = require_mapping(payload, path="$")
    require_exact_keys(
        root,
        required={
            "schema_id",
            "schema_version",
            "created_at",
            "producer",
            "logical_run_id",
            "semantic_contract",
            "semantic_contract_sha256",
            "integrity",
        },
        path="$",
    )
    normalized = copy.deepcopy(dict(root))
    require_enum(root["schema_id"], {_SEMANTIC_CONTRACT_SCHEMA_ID}, path="$.schema_id")
    require_enum(root["schema_version"], {_SCHEMA_VERSION}, path="$.schema_version")
    require_rfc3339(root["created_at"], path="$.created_at")
    validate_producer(root["producer"], path="$.producer", workstream="evaluation")
    require_string(root["logical_run_id"], path="$.logical_run_id")
    contract = require_mapping(root["semantic_contract"], path="$.semantic_contract")
    contract_hash = require_sha256(
        root["semantic_contract_sha256"], path="$.semantic_contract_sha256"
    )
    if shared_canonical_sha256(contract) != contract_hash:
        raise ContractValidationError(
            "semantic_contract_hash",
            "$.semantic_contract_sha256",
            "semantic contract hash differs from its bytes",
        )
    _verify_opaque(normalized)
    return normalized


def _validate_attempt_artifact(payload: Mapping[str, Any]) -> dict[str, Any]:
    root = require_mapping(payload, path="$")
    require_exact_keys(
        root,
        required={
            "schema_id",
            "schema_version",
            "created_at",
            "producer",
            "logical_run_id",
            "attempt_run_id",
            "profile_id",
            "profile_sha256",
            "semantic_contract_sha256",
            "invocation_policy",
            "runtime_binding",
            "integrity",
        },
        path="$",
    )
    normalized = copy.deepcopy(dict(root))
    require_enum(root["schema_id"], {_ATTEMPT_SCHEMA_ID}, path="$.schema_id")
    require_enum(root["schema_version"], {_SCHEMA_VERSION}, path="$.schema_version")
    require_rfc3339(root["created_at"], path="$.created_at")
    validate_producer(root["producer"], path="$.producer", workstream="evaluation")
    require_string(root["logical_run_id"], path="$.logical_run_id")
    require_string(root["attempt_run_id"], path="$.attempt_run_id")
    require_string(root["profile_id"], path="$.profile_id")
    profile_hash = require_sha256(root["profile_sha256"], path="$.profile_sha256")
    semantic_hash = require_sha256(
        root["semantic_contract_sha256"], path="$.semantic_contract_sha256"
    )
    runtime = _validate_attempt_runtime_binding(root["runtime_binding"])
    invocation_policy = _validate_invocation_policy(root["invocation_policy"])
    expected_policy = _build_invocation_policy(
        invocation_policy["max_new_calls"], runtime["profile"]
    )
    if (
        shared_canonical_sha256(runtime["profile"]) != profile_hash
        or runtime["semantic_contract_sha256"] != semantic_hash
    ):
        raise ContractValidationError(
            "attempt_runtime_binding",
            "$.runtime_binding",
            "runtime binding differs from attempt profile or semantic contract",
        )
    if invocation_policy != expected_policy:
        raise ContractValidationError(
            "attempt_invocation_policy",
            "$.invocation_policy",
            "invocation token caps differ from the sealed role profile",
        )
    normalized["invocation_policy"] = invocation_policy
    normalized["runtime_binding"] = runtime
    _verify_opaque(normalized)
    return normalized


def _validate_attempt_runtime_binding(value: Any) -> dict[str, Any]:
    path = "$.runtime_binding"
    root = require_mapping(value, path=path)
    require_exact_keys(
        root,
        required={
            "schema_id",
            "schema_version",
            "semantic_contract_sha256",
            "profile",
            "api_sources",
            "capabilities",
            "integrity",
        },
        path=path,
    )
    require_enum(
        root["schema_id"], {"EvaluationAttemptRuntimeBindingV1"}, path=f"{path}.schema_id"
    )
    require_enum(root["schema_version"], {"1.0.0"}, path=f"{path}.schema_version")
    semantic_hash = require_sha256(
        root["semantic_contract_sha256"], path=f"{path}.semantic_contract_sha256"
    )
    profile = validate_pipeline_profile(root["profile"])
    sources = [
        validate_api_source(row)
        for row in require_list(root["api_sources"], path=f"{path}.api_sources")
    ]
    capabilities = [
        validate_capability_evidence(row)
        for row in require_list(root["capabilities"], path=f"{path}.capabilities")
    ]
    integrity = require_mapping(root["integrity"], path=f"{path}.integrity")
    require_exact_keys(
        integrity, required={"attempt_binding_sha256"}, path=f"{path}.integrity"
    )
    recorded = require_sha256(
        integrity["attempt_binding_sha256"],
        path=f"{path}.integrity.attempt_binding_sha256",
    )
    material = {
        "profile": profile,
        "api_sources": sources,
        "capabilities": capabilities,
    }
    if shared_canonical_sha256(material) != recorded:
        raise ContractValidationError(
            "attempt_binding_hash",
            f"{path}.integrity.attempt_binding_sha256",
            "attempt runtime binding hash differs from its records",
        )
    return {
        "schema_id": "EvaluationAttemptRuntimeBindingV1",
        "schema_version": "1.0.0",
        "semantic_contract_sha256": semantic_hash,
        **material,
        "integrity": {"attempt_binding_sha256": recorded},
    }


def _normalize_invocation_call_cap(value: int | None) -> int:
    if value is None:
        return _EXPECTED_CALL_COUNT
    cap = require_int(value, path="$.max_new_calls", minimum=1)
    if cap > _EXPECTED_CALL_COUNT:
        raise ContractValidationError(
            "range",
            "$.max_new_calls",
            f"must be <= {_EXPECTED_CALL_COUNT}",
        )
    return cap


def _build_invocation_policy(
    max_new_calls: int, profile: Mapping[str, Any]
) -> dict[str, int]:
    roles = [
        row
        for row in profile["role_bindings"]
        if row["role_id"] == SF_BT_SEMANTIC_JUDGE_ROLE_ID
    ]
    if len(roles) != 1:
        raise ContractValidationError(
            "semantic_roles",
            "$.profile.role_bindings",
            "calibration profile must contain exactly one semantic judge role",
        )
    limits = roles[0]["limits"]
    prompt_cap = int(limits["max_prompt_tokens"])
    completion_cap = int(limits["max_completion_tokens"])
    total_cap = int(limits["max_total_tokens"])
    return {
        "max_new_calls": max_new_calls,
        "per_call_max_prompt_tokens": prompt_cap,
        "per_call_max_completion_tokens": completion_cap,
        "per_call_max_total_tokens": total_cap,
        "aggregate_max_prompt_tokens": max_new_calls * prompt_cap,
        "aggregate_max_completion_tokens": max_new_calls * completion_cap,
        "aggregate_max_total_tokens": max_new_calls * total_cap,
    }


def _validate_invocation_policy(value: Any) -> dict[str, int]:
    path = "$.invocation_policy"
    row = require_mapping(value, path=path)
    fields = {
        "max_new_calls",
        "per_call_max_prompt_tokens",
        "per_call_max_completion_tokens",
        "per_call_max_total_tokens",
        "aggregate_max_prompt_tokens",
        "aggregate_max_completion_tokens",
        "aggregate_max_total_tokens",
    }
    require_exact_keys(row, required=fields, path=path)
    normalized = {
        field: require_int(row[field], path=f"{path}.{field}", minimum=1)
        for field in fields
    }
    normalized["max_new_calls"] = _normalize_invocation_call_cap(
        row["max_new_calls"]
    )
    if (
        normalized["aggregate_max_prompt_tokens"]
        != normalized["max_new_calls"]
        * normalized["per_call_max_prompt_tokens"]
        or normalized["aggregate_max_completion_tokens"]
        != normalized["max_new_calls"]
        * normalized["per_call_max_completion_tokens"]
        or normalized["aggregate_max_total_tokens"]
        != normalized["max_new_calls"]
        * normalized["per_call_max_total_tokens"]
    ):
        raise ContractValidationError(
            "invocation_token_caps",
            path,
            "aggregate token caps must equal per-call caps times max_new_calls",
        )
    return normalized


def _validate_plan_binding(value: Any) -> dict[str, Any]:
    path = "$.binding"
    row = require_mapping(value, path=path)
    require_exact_keys(
        row,
        required={
            "fixture_set_id",
            "fixture_sha256",
            "logical_run_id",
            "semantic_contract_sha256",
            "role_id",
            "prompt_candidate_id",
            "prompt_sha256",
            "case_ids",
            "orientation_case_ids",
        },
        path=path,
    )
    case_ids = _string_list(row["case_ids"], f"{path}.case_ids")
    orientation_ids = _string_list(
        row["orientation_case_ids"], f"{path}.orientation_case_ids"
    )
    require_unique(case_ids, path=f"{path}.case_ids")
    require_unique(orientation_ids, path=f"{path}.orientation_case_ids")
    return {
        "fixture_set_id": require_string(
            row["fixture_set_id"], path=f"{path}.fixture_set_id"
        ),
        "fixture_sha256": require_sha256(
            row["fixture_sha256"], path=f"{path}.fixture_sha256"
        ),
        "logical_run_id": require_string(
            row["logical_run_id"], path=f"{path}.logical_run_id"
        ),
        "semantic_contract_sha256": require_sha256(
            row["semantic_contract_sha256"],
            path=f"{path}.semantic_contract_sha256",
        ),
        "role_id": require_enum(
            row["role_id"], {SF_BT_SEMANTIC_JUDGE_ROLE_ID}, path=f"{path}.role_id"
        ),
        "prompt_candidate_id": require_enum(
            row["prompt_candidate_id"],
            {SF_BT_SEMANTIC_CANDIDATE_ID},
            path=f"{path}.prompt_candidate_id",
        ),
        "prompt_sha256": require_enum(
            row["prompt_sha256"],
            {SF_BT_SEMANTIC_PROMPT_SHA256},
            path=f"{path}.prompt_sha256",
        ),
        "case_ids": case_ids,
        "orientation_case_ids": orientation_ids,
    }


def _validate_execution_policy(value: Any) -> dict[str, Any]:
    path = "$.execution_policy"
    row = require_mapping(value, path=path)
    require_exact_keys(
        row,
        required={
            "primary_repeat_count",
            "orientation_case_count",
            "expected_accepted_call_count",
            "cache_mode",
            "automatic_retry",
            "provider_fallback",
        },
        path=path,
    )
    return {
        "primary_repeat_count": require_int(
            row["primary_repeat_count"], path=f"{path}.primary_repeat_count", minimum=1
        ),
        "orientation_case_count": require_int(
            row["orientation_case_count"],
            path=f"{path}.orientation_case_count",
            minimum=0,
        ),
        "expected_accepted_call_count": require_int(
            row["expected_accepted_call_count"],
            path=f"{path}.expected_accepted_call_count",
            minimum=1,
        ),
        "cache_mode": require_enum(
            row["cache_mode"], {"bypass"}, path=f"{path}.cache_mode"
        ),
        "automatic_retry": _require_false(
            row["automatic_retry"], path=f"{path}.automatic_retry"
        ),
        "provider_fallback": _require_false(
            row["provider_fallback"], path=f"{path}.provider_fallback"
        ),
    }


def _validate_call_specs(value: Any) -> list[dict[str, Any]]:
    path = "$.calls"
    rows = require_list(value, path=path)
    result = []
    for index, value in enumerate(rows):
        row_path = f"{path}[{index}]"
        row = require_mapping(value, path=row_path)
        require_exact_keys(
            row,
            required={
                "call_id",
                "case_id",
                "observation_kind",
                "replicate_index",
                "orientation",
            },
            path=row_path,
        )
        result.append(
            {
                "call_id": require_string(row["call_id"], path=f"{row_path}.call_id"),
                "case_id": require_string(row["case_id"], path=f"{row_path}.case_id"),
                "observation_kind": require_enum(
                    row["observation_kind"],
                    {"primary_repeat", "orientation_screen"},
                    path=f"{row_path}.observation_kind",
                ),
                "replicate_index": require_int(
                    row["replicate_index"],
                    path=f"{row_path}.replicate_index",
                    minimum=1,
                ),
                "orientation": require_enum(
                    row["orientation"],
                    {"canonical", "reversed"},
                    path=f"{row_path}.orientation",
                ),
            }
        )
    require_unique([row["call_id"] for row in result], path=f"{path}.call_id")
    return result


def _validate_plan_shape(plan: Mapping[str, Any]) -> None:
    policy = plan["execution_policy"]
    binding = plan["binding"]
    expected = _build_call_specs(
        binding["case_ids"], binding["orientation_case_ids"]
    )
    if (
        policy["primary_repeat_count"] != _REPEAT_COUNT
        or policy["orientation_case_count"] != _ORIENTATION_CASE_COUNT
        or policy["expected_accepted_call_count"] != _EXPECTED_CALL_COUNT
        or len(binding["case_ids"]) != 15
        or len(binding["orientation_case_ids"]) != _ORIENTATION_CASE_COUNT
        or plan["calls"] != expected
    ):
        raise ContractValidationError(
            "plan_shape", "$", "calibration plan differs from the fixed 35-call design"
        )


def _validate_checkpoint_binding(value: Any) -> dict[str, Any]:
    path = "$.binding"
    row = require_mapping(value, path=path)
    required = {
        "plan_sha256",
        "fixture_sha256",
        "logical_run_id",
        "semantic_contract_sha256",
        "call_id",
        "case_id",
        "observation_kind",
        "replicate_index",
        "orientation",
        "presentation_id",
        "packet_sha256",
        "rendered_prompt_sha256",
        "attempt_run_id",
        "profile_sha256",
        "role_id",
    }
    require_exact_keys(row, required=required, path=path)
    return {
        "plan_sha256": require_sha256(row["plan_sha256"], path=f"{path}.plan_sha256"),
        "fixture_sha256": require_sha256(
            row["fixture_sha256"], path=f"{path}.fixture_sha256"
        ),
        "logical_run_id": require_string(
            row["logical_run_id"], path=f"{path}.logical_run_id"
        ),
        "semantic_contract_sha256": require_sha256(
            row["semantic_contract_sha256"],
            path=f"{path}.semantic_contract_sha256",
        ),
        "call_id": require_string(row["call_id"], path=f"{path}.call_id"),
        "case_id": require_string(row["case_id"], path=f"{path}.case_id"),
        "observation_kind": require_enum(
            row["observation_kind"],
            {"primary_repeat", "orientation_screen"},
            path=f"{path}.observation_kind",
        ),
        "replicate_index": require_int(
            row["replicate_index"], path=f"{path}.replicate_index", minimum=1
        ),
        "orientation": require_enum(
            row["orientation"], {"canonical", "reversed"}, path=f"{path}.orientation"
        ),
        "presentation_id": require_enum(
            row["presentation_id"],
            {"calibration_reference_first", "calibration_candidate_first"},
            path=f"{path}.presentation_id",
        ),
        "packet_sha256": require_sha256(
            row["packet_sha256"], path=f"{path}.packet_sha256"
        ),
        "rendered_prompt_sha256": require_sha256(
            row["rendered_prompt_sha256"], path=f"{path}.rendered_prompt_sha256"
        ),
        "attempt_run_id": require_string(
            row["attempt_run_id"], path=f"{path}.attempt_run_id"
        ),
        "profile_sha256": require_sha256(
            row["profile_sha256"], path=f"{path}.profile_sha256"
        ),
        "role_id": require_enum(
            row["role_id"], {SF_BT_SEMANTIC_JUDGE_ROLE_ID}, path=f"{path}.role_id"
        ),
    }


def _validate_call_evidence(value: Any) -> dict[str, Any]:
    path = "$.call_evidence"
    row = require_mapping(value, path=path)
    require_exact_keys(
        row,
        required={
            "seal_sha256",
            "backend_status",
            "provider_called",
            "response_artifact_sha256",
            "attempt_usage_id",
            "cache_observation_id",
        },
        path=path,
    )
    if row["provider_called"] is not True:
        raise ContractValidationError(
            "provider_called",
            f"{path}.provider_called",
            "cache-bypassed calibration checkpoint requires a provider call",
        )
    return {
        "seal_sha256": require_sha256(
            row["seal_sha256"], path=f"{path}.seal_sha256"
        ),
        "backend_status": require_enum(
            row["backend_status"],
            {"provider_succeeded"},
            path=f"{path}.backend_status",
        ),
        "provider_called": True,
        "response_artifact_sha256": require_sha256(
            row["response_artifact_sha256"],
            path=f"{path}.response_artifact_sha256",
        ),
        "attempt_usage_id": require_nullable_string(
            row["attempt_usage_id"], path=f"{path}.attempt_usage_id"
        ),
        "cache_observation_id": require_nullable_string(
            row["cache_observation_id"], path=f"{path}.cache_observation_id"
        ),
    }


def _validate_semantic_output(value: Any) -> dict[str, Any]:
    row = require_mapping(value, path="$.semantic_output")
    rendered = json.dumps(row, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    return parse_sf_bt_semantic_response_v3(rendered)


def _select_orientation_cases(cases: Sequence[Mapping[str, Any]]) -> list[str]:
    result = []
    for score in (100, 75, 50, 25, 0):
        candidates = [row["case_id"] for row in cases if row["expected_score"] == score]
        if len(candidates) != 3:
            raise ContractValidationError(
                "orientation_selection",
                "$.cases",
                "orientation selection requires three fixture cases per band",
            )
        result.append(min(candidates, key=lambda value: _digest("orientation_v1", value)))
    return result


def _build_call_specs(
    case_ids: Sequence[str], orientation_case_ids: Sequence[str]
) -> list[dict[str, Any]]:
    calls = [
        {
            "call_id": f"{case_id}__repeat_{repeat_index}",
            "case_id": case_id,
            "observation_kind": "primary_repeat",
            "replicate_index": repeat_index,
            "orientation": "canonical",
        }
        for repeat_index in (1, 2)
        for case_id in case_ids
    ]
    calls.extend(
        {
            "call_id": f"{case_id}__orientation_reverse",
            "case_id": case_id,
            "observation_kind": "orientation_screen",
            "replicate_index": 1,
            "orientation": "reversed",
        }
        for case_id in orientation_case_ids
    )
    return calls


def _assert_oracle_not_in_prompt(
    fixture: Mapping[str, Any], packet: Mapping[str, Any], prompt: str
) -> None:
    case = next(
        row for row in fixture["cases"] if row["case_id"] == packet["binding"]["case_id"]
    )
    forbidden = (
        case["case_id"],
        case["expected_primary_reason"],
        case["author_note"],
        packet["binding"]["presentation_id"],
    )
    if any(value in prompt for value in forbidden):
        raise ContractValidationError(
            "oracle_leak", "$.rendered_prompt", "fixture metadata leaked into model prompt"
        )


def _require_accepted(call: SharedEvaluationRoleCallV1) -> None:
    if call.seal.get("role_id") != SF_BT_SEMANTIC_JUDGE_ROLE_ID:
        raise ContractValidationError(
            "role_id", "$.call.seal.role_id", "role runner returned another role"
        )
    if call.outcome.get("status") != "accepted":
        error = call.outcome.get("semantic_error")
        code = error.get("code") if isinstance(error, Mapping) else "rejected"
        raise ContractValidationError(
            "semantic_rejection",
            "$.call.outcome",
            f"semantic judge failed local validation: {code}",
        )


def _persist_checkpoint(root: Path, checkpoint: Mapping[str, Any]) -> None:
    path = root / "checkpoints" / f"{checkpoint['binding']['call_id']}.json"
    _persist_create_or_equal(path, checkpoint, policy=_CHECKPOINT_POLICY)


def _validate_integrity(value: Any, *, field: str, path: str) -> dict[str, str]:
    row = require_mapping(value, path=path)
    require_exact_keys(row, required={field}, path=path)
    return {field: require_sha256(row[field], path=f"{path}.{field}")}


def _producer(commit: str, component: str) -> dict[str, str]:
    return {
        "workstream": "evaluation",
        "component": component,
        "component_version": "1.0.0",
        "code_commit": commit,
    }


def _string_list(value: Any, path: str) -> list[str]:
    rows = require_list(value, path=path)
    return [require_string(row, path=f"{path}[{index}]") for index, row in enumerate(rows)]


def _require_false(value: Any, *, path: str) -> bool:
    if value is not False:
        raise ContractValidationError("value", path, "expected false")
    return False


def _canonical(value: Mapping[str, Any], policy: CanonicalPolicy) -> dict[str, Any]:
    normalized = canonicalize(value, policy=policy)
    if not isinstance(normalized, dict):
        raise AssertionError("canonical artifact must remain an object")
    return normalized


def _seal_opaque(payload: Mapping[str, Any]) -> dict[str, Any]:
    sealed = copy.deepcopy(dict(payload))
    sealed["integrity"] = {
        "artifact_sha256": shared_canonical_sha256(
            {key: value for key, value in sealed.items() if key != "integrity"}
        )
    }
    return sealed


def _verify_opaque(payload: Mapping[str, Any]) -> None:
    integrity = require_mapping(payload.get("integrity"), path="$.integrity")
    require_exact_keys(
        integrity, required={"artifact_sha256"}, path="$.integrity"
    )
    recorded = require_sha256(
        integrity["artifact_sha256"], path="$.integrity.artifact_sha256"
    )
    expected = shared_canonical_sha256(
        {key: value for key, value in payload.items() if key != "integrity"}
    )
    if recorded != expected:
        raise ContractValidationError(
            "artifact_hash", "$.integrity.artifact_sha256", "artifact hash differs"
        )


def _persist_create_or_equal(
    path: Path, payload: Mapping[str, Any], *, policy: CanonicalPolicy
) -> None:
    rendered = (
        json.dumps(
            _canonical(payload, policy),
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n"
    ).encode("utf-8")
    _persist_bytes_create_or_equal(path, rendered)


def _persist_opaque_create_or_equal(path: Path, payload: Mapping[str, Any]) -> None:
    rendered = (
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")
    _persist_bytes_create_or_equal(path, rendered)


def _persist_bytes_create_or_equal(path: Path, rendered: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if path.read_bytes() != rendered:
            raise ContractValidationError(
                "immutable_artifact", str(path), "existing artifact bytes differ"
            )
        return
    handle = tempfile.NamedTemporaryFile(
        mode="wb", delete=False, dir=path.parent, prefix=path.name + ".", suffix=".tmp"
    )
    temporary = Path(handle.name)
    try:
        with handle:
            handle.write(rendered)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _load_json_object(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(
            path.read_text(encoding="utf-8"),
            object_pairs_hook=_reject_duplicate_json_keys,
            parse_constant=_reject_json_constant,
        )
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        raise ContractValidationError(
            "artifact_file", str(path), "artifact is unreadable or malformed"
        ) from exc
    if not isinstance(value, dict):
        raise ContractValidationError("type", str(path), "artifact must be an object")
    return value


def _reject_duplicate_json_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"non-finite JSON constant is forbidden: {value}")


def _digest(*parts: str) -> str:
    return hashlib.sha256("\x1f".join(parts).encode("utf-8")).hexdigest()
