from __future__ import annotations

import copy
from dataclasses import dataclass
from hashlib import sha256
import json
import math
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
from pipeline.eval.method_executors_v1 import (
    SharedEvaluationRoleCallV1,
    build_evaluation_semantic_contract_v1,
)
from pipeline.eval.scorer_probe_fixtures_v1 import (
    scorer_probe_fixture_sha256,
    validate_scorer_probe_fixture_set,
)
from pipeline.eval.scorer_probe_packets_v1 import (
    build_sf_bt_probe_semantic_packet_v1,
    build_sf_bt_probe_stage1_packet_v1,
)
from pipeline.eval.scorer_prompts_v3 import (
    render_sf_bt_reverse_prompt_v3,
    render_sf_bt_semantic_prompt_v3,
)
from pipeline.eval.sf_bt_stage_contracts_v1 import (
    build_sf_back_translation_result,
    validate_sf_back_translation_result,
)
from pipeline.eval.llm_profiles_v1 import (
    SF_BT_BACK_TRANSLATOR_ROLE_ID,
    SF_BT_SEMANTIC_JUDGE_ROLE_ID,
)
from pipeline.llm_backend import canonical_json as shared_canonical_json
from pipeline.llm_backend import canonical_sha256 as shared_canonical_sha256
from pipeline.llm_backend import derive_llm_attempt_identity
from pipeline.llm_backend import validate_api_source
from pipeline.llm_backend import validate_capability_evidence
from pipeline.llm_backend import validate_pipeline_profile
from pipeline.llm_backend.transport_v1 import TransportCallError


__all__ = [
    "EvaluationSfBtP2ProbeRunResultV1",
    "run_evaluation_sf_bt_p2_probe_v1",
    "validate_evaluation_sf_bt_p2_checkpoint_v1",
    "validate_evaluation_sf_bt_p2_result_v1",
]


_MANIFEST_SCHEMA_ID = "EvaluationSfBtP2ProbeManifestV1"
_CHECKPOINT_SCHEMA_ID = "EvaluationSfBtP2ProbeCheckpointV1"
_RESULT_SCHEMA_ID = "EvaluationSfBtP2ProbeResultV1"
_ATTEMPT_SCHEMA_ID = "EvaluationSfBtP2ProbeAttemptV1"
_SEMANTIC_CONTRACT_SCHEMA_ID = "EvaluationSfBtP2SemanticContractV1"
_ATTEMPT_BINDING_SCHEMA_ID = "EvaluationSfBtP2AttemptBindingV1"
_SCHEMA_VERSION = "1.0.0"
_P2_STRATUM = "P2_omission_control"
_CONTEXT_PROFILES = ("no_context", "bounded_neighbors")
_STAGES = ("back_translation", "semantic_judge")
_RETRYABLE_NEW_ATTEMPT_CODES = frozenset({"http_408", "http_503", "timeout"})
_POLICY = CanonicalPolicy(
    set_like_paths=frozenset(),
    semantic_sequence_paths=frozenset(
        {
            ("binding", "case_ids"),
            ("binding", "context_profiles"),
            ("rows",),
            ("metrics",),
            ("semantic_output", "flags"),
            ("rows", "*", "flags"),
        }
    ),
)
_HASH_PATHS = {
    _MANIFEST_SCHEMA_ID: ("integrity", "manifest_sha256"),
    _CHECKPOINT_SCHEMA_ID: ("integrity", "checkpoint_sha256"),
    _RESULT_SCHEMA_ID: ("integrity", "result_sha256"),
    _ATTEMPT_SCHEMA_ID: ("integrity", "attempt_sha256"),
}


class EvaluationProbeRoleRunnerV1(Protocol):
    @property
    def execution_binding(self) -> Mapping[str, str]: ...

    @property
    def semantic_contract(self) -> Mapping[str, Any]: ...

    @property
    def attempt_runtime_binding(self) -> Mapping[str, Any]: ...

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
class EvaluationSfBtP2ProbeRunResultV1:
    output_root: Path
    manifest_path: Path
    result_path: Path | None
    result: dict[str, Any] | None
    reused_checkpoint_count: int
    created_checkpoint_count: int
    used_attempt_run_ids: tuple[str, ...]


def run_evaluation_sf_bt_p2_probe_v1(
    fixture_payload: Mapping[str, Any],
    role_runners: Sequence[EvaluationProbeRoleRunnerV1],
    output_root: Path,
    *,
    created_at: str,
    producer_code_commit: str,
) -> EvaluationSfBtP2ProbeRunResultV1:
    """Run or resume the approved 40-call P2 probe.

    Each supplied role runner represents one explicitly sealed Evaluation
    attempt. Only an HTTP 408/503 or timeout may advance to the second runner.
    HTTP 429 and semantic rejection halt without an automatic retry.
    """

    fixture = validate_scorer_probe_fixture_set(fixture_payload)
    fixture_hash = scorer_probe_fixture_sha256(fixture)
    cases = [
        row
        for row in fixture["sf_bt_context_ablation"]
        if row["stratum"] == _P2_STRATUM
    ]
    if len(cases) != 10:
        raise ContractValidationError(
            "p2_exact_cover", "$.sf_bt_context_ablation", "expected exactly 10 P2 rows"
        )
    require_unique([row["case_id"] for row in cases], path="$.p2.case_ids")
    runners = _validate_role_runners(role_runners)
    timestamp = require_rfc3339(created_at, path="$.created_at")
    commit = require_commit(producer_code_commit, path="$.producer_code_commit")
    root = Path(output_root).resolve()
    root.mkdir(parents=True, exist_ok=True)
    _require_root_shape(root)
    _persist_bytes_create_or_equal(
        root / ".gitattributes", b"*.json text eol=lf\n*.md text eol=lf\n"
    )
    semantic_contract = _load_or_create_semantic_contract(
        root,
        runners,
        created_at=timestamp,
        producer_code_commit=commit,
    )
    manifest_path = root / "manifest.json"
    manifest = _load_or_create_manifest(
        manifest_path,
        fixture=fixture,
        fixture_sha256=fixture_hash,
        cases=cases,
        binding=runners[0].execution_binding,
        created_at=timestamp,
        producer_code_commit=commit,
    )
    _require_manifest_fixture_binding(
        manifest,
        fixture=fixture,
        fixture_sha256=fixture_hash,
        cases=cases,
    )
    _require_runner_binding(manifest, semantic_contract, runners)

    result_path = root / "result.json"
    if result_path.exists():
        result = validate_evaluation_sf_bt_p2_result_v1(
            _load_json_object(result_path)
        )
        _validate_result_binding(result, manifest)
        return EvaluationSfBtP2ProbeRunResultV1(
            output_root=root,
            manifest_path=manifest_path,
            result_path=result_path,
            result=result,
            reused_checkpoint_count=40,
            created_checkpoint_count=0,
            used_attempt_run_ids=(),
        )

    checkpoints = _load_checkpoints(
        root / "checkpoints", manifest, semantic_contract, root=root
    )
    reused_count = len(checkpoints)
    created_count = 0
    runner_index = 0
    used_attempt_ids: list[str] = []
    _start_attempt(root, runners[runner_index], timestamp, commit)
    used_attempt_ids.append(
        str(runners[runner_index].execution_binding["evaluation_attempt_run_id"])
    )

    for case in cases:
        stage1_packet = build_sf_bt_probe_stage1_packet_v1(
            case,
            fixture_sha256=fixture_hash,
            created_at=manifest["created_at"],
            producer_code_commit=manifest["producer"]["code_commit"],
        )
        for context_profile in _CONTEXT_PROFILES:
            stage1_key = _checkpoint_key(
                case["case_id"], context_profile, "back_translation"
            )
            if stage1_key not in checkpoints:
                while True:
                    try:
                        checkpoint = _execute_stage1(
                            runners[runner_index],
                            manifest,
                            case,
                            stage1_packet,
                            context_profile=context_profile,
                            created_at=timestamp,
                            producer_code_commit=commit,
                        )
                        break
                    except TransportCallError as exc:
                        _halt_attempt(root, runners[runner_index], timestamp, commit, exc)
                        if (
                            exc.code not in _RETRYABLE_NEW_ATTEMPT_CODES
                            or runner_index + 1 >= len(runners)
                        ):
                            raise
                        runner_index += 1
                        _start_attempt(root, runners[runner_index], timestamp, commit)
                        used_attempt_ids.append(
                            str(
                                runners[runner_index].execution_binding[
                                    "evaluation_attempt_run_id"
                                ]
                            )
                        )
                    except ContractValidationError as exc:
                        _halt_semantic_attempt(
                            root, runners[runner_index], timestamp, commit, exc
                        )
                        raise
                _persist_checkpoint(root, checkpoint)
                checkpoints[stage1_key] = checkpoint
                created_count += 1
            stage1_checkpoint = checkpoints[stage1_key]
            _require_stage1_checkpoint_binding(
                stage1_checkpoint,
                case=case,
                packet=stage1_packet,
                context_profile=context_profile,
            )

            stage2_key = _checkpoint_key(
                case["case_id"], context_profile, "semantic_judge"
            )
            if stage2_key in checkpoints:
                _require_stage2_checkpoint_binding(
                    checkpoints[stage2_key],
                    case=case,
                    stage1_packet=stage1_packet,
                    stage1_checkpoint=stage1_checkpoint,
                    context_profile=context_profile,
                    created_at=manifest["created_at"],
                )
                continue
            while True:
                try:
                    checkpoint = _execute_stage2(
                        runners[runner_index],
                        manifest,
                        case,
                        stage1_packet,
                        stage1_checkpoint,
                        context_profile=context_profile,
                        created_at=timestamp,
                        producer_code_commit=commit,
                    )
                    break
                except TransportCallError as exc:
                    _halt_attempt(root, runners[runner_index], timestamp, commit, exc)
                    if (
                        exc.code not in _RETRYABLE_NEW_ATTEMPT_CODES
                        or runner_index + 1 >= len(runners)
                    ):
                        raise
                    runner_index += 1
                    _start_attempt(root, runners[runner_index], timestamp, commit)
                    used_attempt_ids.append(
                        str(
                            runners[runner_index].execution_binding[
                                "evaluation_attempt_run_id"
                            ]
                        )
                    )
                except ContractValidationError as exc:
                    _halt_semantic_attempt(
                        root, runners[runner_index], timestamp, commit, exc
                    )
                    raise
            _persist_checkpoint(root, checkpoint)
            checkpoints[stage2_key] = checkpoint
            created_count += 1

    if len(checkpoints) != 40:
        raise ContractValidationError(
            "p2_exact_cover", "$.checkpoints", "result requires 40 accepted checkpoints"
        )
    _complete_attempt(root, runners[runner_index], timestamp, commit)
    result = _build_result(manifest, cases, checkpoints, timestamp, commit)
    _persist_create_or_equal(result_path, result)
    return EvaluationSfBtP2ProbeRunResultV1(
        output_root=root,
        manifest_path=manifest_path,
        result_path=result_path,
        result=result,
        reused_checkpoint_count=reused_count,
        created_checkpoint_count=created_count,
        used_attempt_run_ids=tuple(used_attempt_ids),
    )


def validate_evaluation_sf_bt_p2_checkpoint_v1(
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
            "stage1_result",
            "integrity",
        },
        path="$",
    )
    binding = _validate_checkpoint_binding(root["binding"])
    stage = binding["stage"]
    semantic = _validate_semantic_output(root["semantic_output"], stage=stage)
    stage1_result = root["stage1_result"]
    if stage == "back_translation":
        if not isinstance(stage1_result, Mapping):
            raise ContractValidationError(
                "stage1_result", "$.stage1_result", "stage 1 requires its sealed result"
            )
        normalized_stage1 = validate_sf_back_translation_result(stage1_result)
        if normalized_stage1["output"] != semantic:
            raise ContractValidationError(
                "stage1_output", "$.semantic_output", "semantic output differs from result"
            )
    else:
        if stage1_result is not None:
            raise ContractValidationError(
                "stage1_result", "$.stage1_result", "stage 2 may not embed stage 1"
            )
        normalized_stage1 = None
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
        "binding": binding,
        "call_evidence": _validate_call_evidence(root["call_evidence"]),
        "semantic_output": semantic,
        "stage1_result": normalized_stage1,
        "integrity": _validate_integrity(
            root["integrity"], "checkpoint_sha256", "$.integrity"
        ),
    }
    _verify_self_hash(normalized, _CHECKPOINT_SCHEMA_ID)
    return _canonical(normalized)


def validate_evaluation_sf_bt_p2_result_v1(
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
            "coverage",
            "metrics",
            "interpretation",
            "rows",
            "integrity",
        },
        path="$",
    )
    binding = _validate_result_root_binding(root["binding"])
    coverage = _validate_coverage(root["coverage"])
    metrics = _validate_metrics(root["metrics"])
    rows = _validate_result_rows(root["rows"])
    if coverage != {
        "case_count": 10,
        "context_profile_count": 2,
        "expected_stage_checkpoint_count": 40,
        "accepted_stage_checkpoint_count": 40,
    }:
        raise ContractValidationError(
            "result_exact_cover", "$.coverage", "P2 result must publish exact 40/40 cover"
        )
    if len(rows) != 20:
        raise ContractValidationError(
            "result_exact_cover", "$.rows", "P2 result requires 20 case-profile rows"
        )
    expected_identities = {
        f"{case_id}\x1f{profile}"
        for case_id in binding["case_ids"]
        for profile in _CONTEXT_PROFILES
    }
    observed_identities = {
        f"{row['case_id']}\x1f{row['context_profile']}" for row in rows
    }
    if observed_identities != expected_identities:
        raise ContractValidationError(
            "result_exact_cover", "$.rows", "result rows differ from manifest cartesian cover"
        )
    expected_metrics = _derive_metrics_from_rows(rows)
    if metrics != expected_metrics:
        raise ContractValidationError(
            "metric_binding", "$.metrics", "metrics differ from result rows"
        )
    normalized = {
        "schema_id": require_enum(
            root["schema_id"], {_RESULT_SCHEMA_ID}, path="$.schema_id"
        ),
        "schema_version": require_enum(
            root["schema_version"], {_SCHEMA_VERSION}, path="$.schema_version"
        ),
        "created_at": require_rfc3339(root["created_at"], path="$.created_at"),
        "producer": validate_producer(
            root["producer"], path="$.producer", workstream="evaluation"
        ),
        "binding": binding,
        "coverage": coverage,
        "metrics": metrics,
        "interpretation": require_enum(
            root["interpretation"],
            {
                "not_blind_to_planted_omission",
                "insensitive_to_planted_omission",
            },
            path="$.interpretation",
        ),
        "rows": rows,
        "integrity": _validate_integrity(
            root["integrity"], "result_sha256", "$.integrity"
        ),
    }
    expected_interpretation = (
        "not_blind_to_planted_omission"
        if all(row["omission_detection_rate"] >= 0.9 for row in metrics)
        else "insensitive_to_planted_omission"
    )
    if normalized["interpretation"] != expected_interpretation:
        raise ContractValidationError(
            "interpretation", "$.interpretation", "interpretation differs from metrics"
        )
    _verify_self_hash(normalized, _RESULT_SCHEMA_ID)
    return _canonical(normalized)


def _execute_stage1(
    runner: EvaluationProbeRoleRunnerV1,
    manifest: Mapping[str, Any],
    case: Mapping[str, Any],
    packet: Mapping[str, Any],
    *,
    context_profile: str,
    created_at: str,
    producer_code_commit: str,
) -> dict[str, Any]:
    packet_hash = packet["integrity"]["packet_sha256"]
    prompt = render_sf_bt_reverse_prompt_v3(packet, context_profile=context_profile)
    request_id = f"sfbt_p2_{case['case_id'].casefold()}_{context_profile}_reverse"
    call = runner.execute(
        role_id=SF_BT_BACK_TRANSLATOR_ROLE_ID,
        scorer_input_packet_sha256=packet_hash,
        rendered_prompt=prompt,
        stage_id=request_id,
        logical_request_id=request_id,
    )
    _require_accepted(call, role_id=SF_BT_BACK_TRANSLATOR_ROLE_ID)
    attempt_id, attempt_index = _attempt_reference(call)
    raw_response = call.outcome.get("response_text")
    if not isinstance(raw_response, str):
        raise ContractValidationError(
            "accepted_response", "$.outcome.response_text", "accepted response text is absent"
        )
    stage1_result = build_sf_back_translation_result(
        packet,
        attempt_id=attempt_id,
        attempt_index=attempt_index,
        created_at=created_at,
        producer_code_commit=producer_code_commit,
        context_profile=context_profile,
        rendered_prompt_sha256=prompt.rendered_prompt_sha256,
        model_profile=_model_profile(call),
        completion_status="complete",
        finish_reason="stop",
        raw_response_text=raw_response,
    )
    return _build_checkpoint(
        manifest,
        runner,
        call,
        case_id=case["case_id"],
        context_profile=context_profile,
        stage="back_translation",
        stage_input_sha256=prompt.rendered_prompt_sha256,
        semantic_output=stage1_result["output"],
        stage1_result=stage1_result,
        created_at=created_at,
        producer_code_commit=producer_code_commit,
    )


def _execute_stage2(
    runner: EvaluationProbeRoleRunnerV1,
    manifest: Mapping[str, Any],
    case: Mapping[str, Any],
    stage1_packet: Mapping[str, Any],
    stage1_checkpoint: Mapping[str, Any],
    *,
    context_profile: str,
    created_at: str,
    producer_code_commit: str,
) -> dict[str, Any]:
    stage1_result = stage1_checkpoint["stage1_result"]
    semantic_packet = build_sf_bt_probe_semantic_packet_v1(
        case,
        stage1_packet,
        stage1_result,
        context_profile=context_profile,
        created_at=created_at,
        producer_code_commit=producer_code_commit,
    )
    packet_hash = semantic_packet["integrity"]["packet_sha256"]
    prompt = render_sf_bt_semantic_prompt_v3(semantic_packet)
    request_id = f"sfbt_p2_{case['case_id'].casefold()}_{context_profile}_semantic"
    call = runner.execute(
        role_id=SF_BT_SEMANTIC_JUDGE_ROLE_ID,
        scorer_input_packet_sha256=packet_hash,
        rendered_prompt=prompt,
        stage_id=request_id,
        logical_request_id=request_id,
        extra_bindings=(
            {
                "name": "sf_bt_stage1_result",
                "sha256": stage1_result["integrity"]["result_sha256"],
            },
        ),
    )
    _require_accepted(call, role_id=SF_BT_SEMANTIC_JUDGE_ROLE_ID)
    return _build_checkpoint(
        manifest,
        runner,
        call,
        case_id=case["case_id"],
        context_profile=context_profile,
        stage="semantic_judge",
        stage_input_sha256=prompt.rendered_prompt_sha256,
        semantic_output=call.outcome["semantic_output"],
        stage1_result=None,
        created_at=created_at,
        producer_code_commit=producer_code_commit,
    )


def _build_checkpoint(
    manifest: Mapping[str, Any],
    runner: EvaluationProbeRoleRunnerV1,
    call: SharedEvaluationRoleCallV1,
    *,
    case_id: str,
    context_profile: str,
    stage: str,
    stage_input_sha256: str,
    semantic_output: Mapping[str, Any],
    stage1_result: Mapping[str, Any] | None,
    created_at: str,
    producer_code_commit: str,
) -> dict[str, Any]:
    outcome = call.outcome
    usage = outcome.get("usage")
    cache = outcome.get("cache_observation")
    runner_profile_sha256 = runner.execution_binding["evaluation_profile_sha256"]
    if call.seal["profile"]["sha256"] != runner_profile_sha256:
        raise ContractValidationError(
            "checkpoint_profile",
            "$.call.seal.profile.sha256",
            "accepted call profile differs from its attempt runner",
        )
    payload = {
        "schema_id": _CHECKPOINT_SCHEMA_ID,
        "schema_version": _SCHEMA_VERSION,
        "created_at": created_at,
        "producer": _producer(producer_code_commit, "scorer_probe_live_runner_v1"),
        "binding": {
            "manifest_sha256": manifest["integrity"]["manifest_sha256"],
            "fixture_sha256": manifest["binding"]["fixture_sha256"],
            "logical_run_id": manifest["binding"]["logical_run_id"],
            "profile_sha256": runner_profile_sha256,
            "case_id": case_id,
            "context_profile": context_profile,
            "stage": stage,
            "stage_input_sha256": stage_input_sha256,
            "attempt_run_id": runner.execution_binding["evaluation_attempt_run_id"],
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
        "semantic_output": copy.deepcopy(dict(semantic_output)),
        "stage1_result": (
            None if stage1_result is None else copy.deepcopy(dict(stage1_result))
        ),
        "integrity": {"checkpoint_sha256": "0" * 64},
    }
    return validate_evaluation_sf_bt_p2_checkpoint_v1(
        _seal(payload, _CHECKPOINT_SCHEMA_ID)
    )


def _build_result(
    manifest: Mapping[str, Any],
    cases: Sequence[Mapping[str, Any]],
    checkpoints: Mapping[str, Mapping[str, Any]],
    created_at: str,
    producer_code_commit: str,
) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    metrics: list[dict[str, Any]] = []
    for profile in _CONTEXT_PROFILES:
        detected = 0
        coverage = 0
        for case in cases:
            stage1 = checkpoints[
                _checkpoint_key(case["case_id"], profile, "back_translation")
            ]
            stage2 = checkpoints[
                _checkpoint_key(case["case_id"], profile, "semantic_judge")
            ]
            output = stage2["semantic_output"]
            is_detected = output["score"] < 100
            has_coverage = "coverage_mismatch" in output["flags"]
            detected += int(is_detected)
            coverage += int(has_coverage)
            rows.append(
                {
                    "case_id": case["case_id"],
                    "context_profile": profile,
                    "score": output["score"],
                    "flags": list(output["flags"]),
                    "omission_detected": is_detected,
                    "back_translation_checkpoint_sha256": stage1["integrity"][
                        "checkpoint_sha256"
                    ],
                    "semantic_checkpoint_sha256": stage2["integrity"][
                        "checkpoint_sha256"
                    ],
                }
            )
        metrics.append(
            {
                "context_profile": profile,
                "case_count": 10,
                "omission_detected_count": detected,
                "omission_detection_rate": detected / 10,
                "coverage_mismatch_count": coverage,
                "coverage_mismatch_rate": coverage / 10,
            }
        )
    interpretation = (
        "not_blind_to_planted_omission"
        if all(row["omission_detection_rate"] >= 0.9 for row in metrics)
        else "insensitive_to_planted_omission"
    )
    payload = {
        "schema_id": _RESULT_SCHEMA_ID,
        "schema_version": _SCHEMA_VERSION,
        "created_at": created_at,
        "producer": _producer(producer_code_commit, "scorer_probe_live_runner_v1"),
        "binding": {
            **manifest["binding"],
            "manifest_sha256": manifest["integrity"]["manifest_sha256"],
        },
        "coverage": {
            "case_count": 10,
            "context_profile_count": 2,
            "expected_stage_checkpoint_count": 40,
            "accepted_stage_checkpoint_count": 40,
        },
        "metrics": metrics,
        "interpretation": interpretation,
        "rows": rows,
        "integrity": {"result_sha256": "0" * 64},
    }
    return validate_evaluation_sf_bt_p2_result_v1(_seal(payload, _RESULT_SCHEMA_ID))


def _load_or_create_manifest(
    path: Path,
    *,
    fixture: Mapping[str, Any],
    fixture_sha256: str,
    cases: Sequence[Mapping[str, Any]],
    binding: Mapping[str, str],
    created_at: str,
    producer_code_commit: str,
) -> dict[str, Any]:
    if path.exists():
        return _validate_manifest(_load_json_object(path))
    payload = {
        "schema_id": _MANIFEST_SCHEMA_ID,
        "schema_version": _SCHEMA_VERSION,
        "created_at": created_at,
        "producer": _producer(producer_code_commit, "scorer_probe_live_runner_v1"),
        "binding": {
            "fixture_set_id": fixture["fixture_set_id"],
            "fixture_sha256": fixture_sha256,
            "stratum": _P2_STRATUM,
            "case_ids": [row["case_id"] for row in cases],
            "context_profiles": list(_CONTEXT_PROFILES),
            "logical_run_id": binding["evaluation_logical_run_id"],
            "profile_id": binding["evaluation_profile_id"],
            "profile_sha256": binding["evaluation_profile_sha256"],
        },
        "workload": {
            "case_count": 10,
            "context_profile_count": 2,
            "stages_per_case_profile": 2,
            "expected_accepted_checkpoint_count": 40,
            "max_additional_attempts_for_408_503_timeout": 1,
            "retry_http_429": False,
            "semantic_retry": False,
        },
        "integrity": {"manifest_sha256": "0" * 64},
    }
    manifest = _validate_manifest(_seal(payload, _MANIFEST_SCHEMA_ID))
    _persist_create_or_equal(path, manifest)
    return manifest


def _require_manifest_fixture_binding(
    manifest: Mapping[str, Any],
    *,
    fixture: Mapping[str, Any],
    fixture_sha256: str,
    cases: Sequence[Mapping[str, Any]],
) -> None:
    expected = {
        "fixture_set_id": fixture["fixture_set_id"],
        "fixture_sha256": fixture_sha256,
        "stratum": _P2_STRATUM,
        "case_ids": [row["case_id"] for row in cases],
        "context_profiles": list(_CONTEXT_PROFILES),
    }
    if any(manifest["binding"][key] != value for key, value in expected.items()):
        raise ContractValidationError(
            "fixture_binding",
            "$.manifest.binding",
            "persisted probe manifest differs from the supplied approved fixture",
        )


def _validate_manifest(payload: Mapping[str, Any]) -> dict[str, Any]:
    root = require_mapping(payload, path="$")
    require_exact_keys(
        root,
        required={
            "schema_id",
            "schema_version",
            "created_at",
            "producer",
            "binding",
            "workload",
            "integrity",
        },
        path="$",
    )
    binding_raw = require_mapping(root["binding"], path="$.binding")
    require_exact_keys(
        binding_raw,
        required={
            "fixture_set_id",
            "fixture_sha256",
            "stratum",
            "case_ids",
            "context_profiles",
            "logical_run_id",
            "profile_id",
            "profile_sha256",
        },
        path="$.binding",
    )
    case_ids = _string_list(binding_raw["case_ids"], "$.binding.case_ids")
    profiles = _string_list(
        binding_raw["context_profiles"], "$.binding.context_profiles"
    )
    if len(case_ids) != 10 or profiles != list(_CONTEXT_PROFILES):
        raise ContractValidationError(
            "manifest_workload", "$.binding", "manifest does not bind exact P2 workload"
        )
    workload = require_mapping(root["workload"], path="$.workload")
    expected_workload = {
        "case_count": 10,
        "context_profile_count": 2,
        "stages_per_case_profile": 2,
        "expected_accepted_checkpoint_count": 40,
        "max_additional_attempts_for_408_503_timeout": 1,
        "retry_http_429": False,
        "semantic_retry": False,
    }
    if dict(workload) != expected_workload:
        raise ContractValidationError(
            "manifest_workload", "$.workload", "workload or retry policy differs"
        )
    normalized = {
        "schema_id": require_enum(
            root["schema_id"], {_MANIFEST_SCHEMA_ID}, path="$.schema_id"
        ),
        "schema_version": require_enum(
            root["schema_version"], {_SCHEMA_VERSION}, path="$.schema_version"
        ),
        "created_at": require_rfc3339(root["created_at"], path="$.created_at"),
        "producer": validate_producer(
            root["producer"], path="$.producer", workstream="evaluation"
        ),
        "binding": {
            "fixture_set_id": require_string(
                binding_raw["fixture_set_id"], path="$.binding.fixture_set_id"
            ),
            "fixture_sha256": require_sha256(
                binding_raw["fixture_sha256"], path="$.binding.fixture_sha256"
            ),
            "stratum": require_enum(
                binding_raw["stratum"], {_P2_STRATUM}, path="$.binding.stratum"
            ),
            "case_ids": case_ids,
            "context_profiles": profiles,
            "logical_run_id": require_string(
                binding_raw["logical_run_id"], path="$.binding.logical_run_id"
            ),
            "profile_id": require_string(
                binding_raw["profile_id"], path="$.binding.profile_id"
            ),
            "profile_sha256": require_sha256(
                binding_raw["profile_sha256"], path="$.binding.profile_sha256"
            ),
        },
        "workload": expected_workload,
        "integrity": _validate_integrity(
            root["integrity"], "manifest_sha256", "$.integrity"
        ),
    }
    _verify_self_hash(normalized, _MANIFEST_SCHEMA_ID)
    return _canonical(normalized)


def _load_or_create_semantic_contract(
    root: Path,
    runners: Sequence[EvaluationProbeRoleRunnerV1],
    *,
    created_at: str,
    producer_code_commit: str,
) -> dict[str, Any]:
    path = root / "semantic_contract.json"
    runner_contract = copy.deepcopy(dict(runners[0].semantic_contract))
    runner_hash = shared_canonical_sha256(runner_contract)
    if path.exists():
        artifact = _validate_semantic_contract_artifact(_load_json_object(path))
    else:
        historical_binding = root / "runtime_binding.json"
        if historical_binding.exists():
            runtime = _validate_historical_runtime_binding(
                _load_json_object(historical_binding), path=str(historical_binding)
            )
            initial_contract = build_evaluation_semantic_contract_v1(
                runtime["profile"],
                [runtime["api_source"]],
                runtime["capabilities"],
            )
        else:
            initial_contract = runner_contract
        initial_hash = shared_canonical_sha256(initial_contract)
        if initial_hash != runner_hash or initial_contract != runner_contract:
            raise ContractValidationError(
                "semantic_contract_drift",
                "$.role_runners[0]",
                "resume runner changes model, prompt, schema, settings, or provider route",
            )
        payload = {
            "schema_id": _SEMANTIC_CONTRACT_SCHEMA_ID,
            "schema_version": _SCHEMA_VERSION,
            "created_at": created_at,
            "producer": _producer(
                producer_code_commit, "scorer_probe_live_runner_v1"
            ),
            "logical_run_id": runners[0].execution_binding[
                "evaluation_logical_run_id"
            ],
            "semantic_contract": initial_contract,
            "semantic_contract_sha256": initial_hash,
            "integrity": {"artifact_sha256": "0" * 64},
        }
        artifact = _validate_semantic_contract_artifact(
            _seal_opaque_artifact(payload)
        )
        _persist_create_or_equal(path, artifact)
    if (
        artifact["logical_run_id"]
        != runners[0].execution_binding["evaluation_logical_run_id"]
        or artifact["semantic_contract_sha256"] != runner_hash
        or artifact["semantic_contract"] != runner_contract
    ):
        raise ContractValidationError(
            "semantic_contract_drift",
            str(path),
            "resume runner changes the sealed semantic contract",
        )
    return artifact


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
    contract = copy.deepcopy(
        dict(require_mapping(root["semantic_contract"], path="$.semantic_contract"))
    )
    contract_hash = require_sha256(
        root["semantic_contract_sha256"], path="$.semantic_contract_sha256"
    )
    if shared_canonical_sha256(contract) != contract_hash:
        raise ContractValidationError(
            "semantic_contract_hash",
            "$.semantic_contract_sha256",
            "semantic contract hash differs from its bytes",
        )
    normalized = {
        "schema_id": require_enum(
            root["schema_id"], {_SEMANTIC_CONTRACT_SCHEMA_ID}, path="$.schema_id"
        ),
        "schema_version": require_enum(
            root["schema_version"], {_SCHEMA_VERSION}, path="$.schema_version"
        ),
        "created_at": require_rfc3339(root["created_at"], path="$.created_at"),
        "producer": validate_producer(
            root["producer"], path="$.producer", workstream="evaluation"
        ),
        "logical_run_id": require_string(
            root["logical_run_id"], path="$.logical_run_id"
        ),
        "semantic_contract": contract,
        "semantic_contract_sha256": contract_hash,
        "integrity": _validate_integrity(
            root["integrity"], "artifact_sha256", "$.integrity"
        ),
    }
    _verify_opaque_artifact_hash(normalized)
    return _canonical_opaque(normalized)


def _validate_historical_runtime_binding(
    payload: Mapping[str, Any], *, path: str
) -> dict[str, Any]:
    root = require_mapping(payload, path=path)
    require_exact_keys(
        root,
        required={
            "schema_id",
            "schema_version",
            "api_source",
            "capabilities",
            "profile",
            "execution_policy",
            "integrity",
        },
        path=path,
    )
    require_enum(
        root["schema_id"], {"EvaluationSfBtP2RuntimeBindingV1"}, path=f"{path}.schema_id"
    )
    require_enum(root["schema_version"], {_SCHEMA_VERSION}, path=f"{path}.schema_version")
    source = validate_api_source(root["api_source"])
    capability_values = require_list(root["capabilities"], path=f"{path}.capabilities")
    capabilities = [validate_capability_evidence(row) for row in capability_values]
    if len(capabilities) != 2:
        raise ContractValidationError(
            "runtime_binding",
            f"{path}.capabilities",
            "P2 runtime binding requires exactly two role capabilities",
        )
    profile = validate_pipeline_profile(root["profile"])
    role_ids = [row["role_id"] for row in profile["role_bindings"]]
    if set(role_ids) != {
        SF_BT_BACK_TRANSLATOR_ROLE_ID,
        SF_BT_SEMANTIC_JUDGE_ROLE_ID,
    } or len(role_ids) != 2:
        raise ContractValidationError(
            "runtime_binding",
            f"{path}.profile.role_bindings",
            "P2 runtime binding requires exactly the two SF-BT roles",
        )
    policy = copy.deepcopy(
        dict(require_mapping(root["execution_policy"], path=f"{path}.execution_policy"))
    )
    require_exact_keys(
        policy,
        required={
            "expected_accepted_call_count",
            "max_failed_retryable_call_count",
            "max_physical_call_count",
            "minimum_call_interval_seconds",
            "http_429_action",
            "semantic_retry",
            "provider_fallback",
        },
        path=f"{path}.execution_policy",
    )
    integrity = require_mapping(root["integrity"], path=f"{path}.integrity")
    require_exact_keys(
        integrity,
        required={"api_source_sha256", "capability_sha256s", "profile_sha256"},
        path=f"{path}.integrity",
    )
    capability_hashes = _string_list(
        integrity["capability_sha256s"], f"{path}.integrity.capability_sha256s"
    )
    expected_capability_hashes = [
        shared_canonical_sha256(row) for row in capabilities
    ]
    if (
        require_sha256(
            integrity["api_source_sha256"], path=f"{path}.integrity.api_source_sha256"
        )
        != shared_canonical_sha256(source)
        or capability_hashes != expected_capability_hashes
        or require_sha256(
            integrity["profile_sha256"], path=f"{path}.integrity.profile_sha256"
        )
        != shared_canonical_sha256(profile)
    ):
        raise ContractValidationError(
            "runtime_binding_hash",
            f"{path}.integrity",
            "historical runtime binding hashes differ from persisted records",
        )
    build_evaluation_semantic_contract_v1(profile, [source], capabilities)
    return {
        "schema_id": "EvaluationSfBtP2RuntimeBindingV1",
        "schema_version": _SCHEMA_VERSION,
        "api_source": source,
        "capabilities": capabilities,
        "profile": profile,
        "execution_policy": policy,
        "integrity": {
            "api_source_sha256": shared_canonical_sha256(source),
            "capability_sha256s": expected_capability_hashes,
            "profile_sha256": shared_canonical_sha256(profile),
        },
    }


def _validate_role_runners(
    runners: Sequence[EvaluationProbeRoleRunnerV1],
) -> tuple[EvaluationProbeRoleRunnerV1, ...]:
    if not isinstance(runners, Sequence) or isinstance(runners, (str, bytes)):
        raise ContractValidationError("type", "$.role_runners", "expected a sequence")
    rows = tuple(runners)
    if not 1 <= len(rows) <= 2:
        raise ContractValidationError(
            "attempt_cap", "$.role_runners", "expected one primary and at most one recovery"
        )
    bindings = [dict(row.execution_binding) for row in rows]
    required = {
        "evaluation_logical_run_id",
        "evaluation_attempt_run_id",
        "evaluation_profile_id",
        "evaluation_profile_sha256",
    }
    for index, binding in enumerate(bindings):
        require_exact_keys(binding, required=required, path=f"$.role_runners[{index}]")
        require_sha256(
            binding["evaluation_profile_sha256"],
            path=f"$.role_runners[{index}].evaluation_profile_sha256",
        )
    first = bindings[0]
    semantic_hashes = [
        shared_canonical_sha256(dict(row.semantic_contract)) for row in rows
    ]
    if any(
        binding[key] != first[key]
        for binding in bindings[1:]
        for key in ("evaluation_logical_run_id",)
    ) or any(value != semantic_hashes[0] for value in semantic_hashes[1:]):
        raise ContractValidationError(
            "attempt_binding",
            "$.role_runners",
            "recovery runner changes the logical run or semantic contract",
        )
    attempt_ids = [binding["evaluation_attempt_run_id"] for binding in bindings]
    require_unique(attempt_ids, path="$.role_runners[*].evaluation_attempt_run_id")
    return rows


def _require_runner_binding(
    manifest: Mapping[str, Any],
    semantic_contract: Mapping[str, Any],
    runners: Sequence[EvaluationProbeRoleRunnerV1],
) -> None:
    binding = manifest["binding"]
    for runner in runners:
        observed = runner.execution_binding
        expected = {
            "evaluation_logical_run_id": binding["logical_run_id"],
        }
        if any(observed[key] != value for key, value in expected.items()):
            raise ContractValidationError(
                "resume_binding",
                "$.role_runner",
                "runner differs from the probe run or sealed semantic contract",
            )
        if (
            shared_canonical_sha256(dict(runner.semantic_contract))
            != semantic_contract["semantic_contract_sha256"]
        ):
            raise ContractValidationError(
                "semantic_contract_drift",
                "$.role_runner.semantic_contract",
                "runner changes the sealed semantic contract",
            )


def _load_checkpoints(
    directory: Path,
    manifest: Mapping[str, Any],
    semantic_contract: Mapping[str, Any],
    *,
    root: Path,
) -> dict[str, dict[str, Any]]:
    directory.mkdir(parents=True, exist_ok=True)
    rows: dict[str, dict[str, Any]] = {}
    expected_cases = set(manifest["binding"]["case_ids"])
    for path in sorted(directory.glob("*.json")):
        checkpoint = validate_evaluation_sf_bt_p2_checkpoint_v1(
            _load_json_object(path)
        )
        binding = checkpoint["binding"]
        if (
            binding["manifest_sha256"] != manifest["integrity"]["manifest_sha256"]
            or binding["fixture_sha256"] != manifest["binding"]["fixture_sha256"]
            or binding["logical_run_id"] != manifest["binding"]["logical_run_id"]
            or binding["case_id"] not in expected_cases
        ):
            raise ContractValidationError(
                "checkpoint_binding", str(path), "checkpoint is foreign to this probe"
            )
        if binding["profile_sha256"] != manifest["binding"]["profile_sha256"]:
            _require_checkpoint_attempt_binding(
                root,
                binding,
                semantic_contract_sha256=semantic_contract[
                    "semantic_contract_sha256"
                ],
            )
        key = _checkpoint_key(
            binding["case_id"], binding["context_profile"], binding["stage"]
        )
        if key in rows:
            raise ContractValidationError(
                "checkpoint_duplicate", str(path), "duplicate semantic checkpoint"
            )
        expected_name = _checkpoint_filename(key)
        if path.name != expected_name:
            raise ContractValidationError(
                "checkpoint_path", str(path), "checkpoint filename differs from identity"
            )
        rows[key] = checkpoint
    return rows


def _persist_checkpoint(root: Path, checkpoint: Mapping[str, Any]) -> None:
    binding = checkpoint["binding"]
    key = _checkpoint_key(
        binding["case_id"], binding["context_profile"], binding["stage"]
    )
    _persist_create_or_equal(root / "checkpoints" / _checkpoint_filename(key), checkpoint)


def _start_attempt(
    root: Path,
    runner: EvaluationProbeRoleRunnerV1,
    created_at: str,
    producer_code_commit: str,
) -> None:
    attempt_id = str(runner.execution_binding["evaluation_attempt_run_id"])
    directory = _attempt_directory(root, attempt_id)
    if directory.exists():
        raise ContractValidationError(
            "attempt_reuse", str(directory), "attempt ID already has persisted evidence"
        )
    directory.mkdir(parents=True, exist_ok=False)
    payload = _attempt_payload(
        runner,
        status="started",
        created_at=created_at,
        producer_code_commit=producer_code_commit,
        error=None,
    )
    _persist_create_or_equal(directory / "attempt.json", payload)
    _persist_create_or_equal(
        directory / "execution_binding.json",
        _attempt_binding_payload(
            runner,
            created_at=created_at,
            producer_code_commit=producer_code_commit,
        ),
    )


def _complete_attempt(
    root: Path,
    runner: EvaluationProbeRoleRunnerV1,
    created_at: str,
    producer_code_commit: str,
) -> None:
    payload = _attempt_payload(
        runner,
        status="complete",
        created_at=created_at,
        producer_code_commit=producer_code_commit,
        error=None,
    )
    _persist_create_or_equal(
        _attempt_directory(
            root, str(runner.execution_binding["evaluation_attempt_run_id"])
        )
        / "complete.json",
        payload,
    )


def _halt_attempt(
    root: Path,
    runner: EvaluationProbeRoleRunnerV1,
    created_at: str,
    producer_code_commit: str,
    error: TransportCallError,
) -> None:
    retry_after = None
    if error.response is not None:
        retry_after = error.response.headers.get("retry-after")
    payload = _attempt_payload(
        runner,
        status="halted",
        created_at=created_at,
        producer_code_commit=producer_code_commit,
        error={
            "code": error.code,
            "status_code": error.status_code,
            "retry_after": retry_after,
            "safe_message": error.safe_message,
        },
    )
    _persist_create_or_equal(
        _attempt_directory(
            root, str(runner.execution_binding["evaluation_attempt_run_id"])
        )
        / "halt.json",
        payload,
    )


def _halt_semantic_attempt(
    root: Path,
    runner: EvaluationProbeRoleRunnerV1,
    created_at: str,
    producer_code_commit: str,
    error: ContractValidationError,
) -> None:
    payload = _attempt_payload(
        runner,
        status="halted",
        created_at=created_at,
        producer_code_commit=producer_code_commit,
        error={
            "code": error.code,
            "status_code": None,
            "retry_after": None,
            "safe_message": str(error),
        },
    )
    _persist_create_or_equal(
        _attempt_directory(
            root, str(runner.execution_binding["evaluation_attempt_run_id"])
        )
        / "halt.json",
        payload,
    )


def _attempt_payload(
    runner: EvaluationProbeRoleRunnerV1,
    *,
    status: str,
    created_at: str,
    producer_code_commit: str,
    error: Mapping[str, Any] | None,
) -> dict[str, Any]:
    binding = runner.execution_binding
    payload = {
        "schema_id": _ATTEMPT_SCHEMA_ID,
        "schema_version": _SCHEMA_VERSION,
        "created_at": created_at,
        "producer": _producer(producer_code_commit, "scorer_probe_live_runner_v1"),
        "binding": {
            "logical_run_id": binding["evaluation_logical_run_id"],
            "attempt_run_id": binding["evaluation_attempt_run_id"],
            "profile_id": binding["evaluation_profile_id"],
            "profile_sha256": binding["evaluation_profile_sha256"],
        },
        "status": status,
        "error": None if error is None else dict(error),
        "integrity": {"attempt_sha256": "0" * 64},
    }
    return _seal(payload, _ATTEMPT_SCHEMA_ID)


def _attempt_binding_payload(
    runner: EvaluationProbeRoleRunnerV1,
    *,
    created_at: str,
    producer_code_commit: str,
) -> dict[str, Any]:
    binding = runner.execution_binding
    runtime_binding = copy.deepcopy(dict(runner.attempt_runtime_binding))
    payload = {
        "schema_id": _ATTEMPT_BINDING_SCHEMA_ID,
        "schema_version": _SCHEMA_VERSION,
        "created_at": created_at,
        "producer": _producer(producer_code_commit, "scorer_probe_live_runner_v1"),
        "logical_run_id": binding["evaluation_logical_run_id"],
        "attempt_run_id": binding["evaluation_attempt_run_id"],
        "profile_id": binding["evaluation_profile_id"],
        "profile_sha256": binding["evaluation_profile_sha256"],
        "semantic_contract_sha256": runtime_binding[
            "semantic_contract_sha256"
        ],
        "attempt_binding_sha256": runtime_binding["integrity"][
            "attempt_binding_sha256"
        ],
        "runtime_binding": runtime_binding,
        "integrity": {"artifact_sha256": "0" * 64},
    }
    return _validate_attempt_binding_artifact(
        _seal_opaque_artifact(payload)
    )


def _validate_attempt_binding_artifact(payload: Mapping[str, Any]) -> dict[str, Any]:
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
            "attempt_binding_sha256",
            "runtime_binding",
            "integrity",
        },
        path="$",
    )
    runtime = copy.deepcopy(
        dict(require_mapping(root["runtime_binding"], path="$.runtime_binding"))
    )
    require_exact_keys(
        runtime,
        required={
            "schema_id",
            "schema_version",
            "semantic_contract_sha256",
            "profile",
            "api_sources",
            "capabilities",
            "integrity",
        },
        path="$.runtime_binding",
    )
    if runtime["schema_id"] != "EvaluationAttemptRuntimeBindingV1":
        raise ContractValidationError(
            "attempt_binding", "$.runtime_binding.schema_id", "unknown runtime binding"
        )
    require_enum(
        runtime["schema_version"], {_SCHEMA_VERSION}, path="$.runtime_binding.schema_version"
    )
    profile = require_mapping(runtime["profile"], path="$.runtime_binding.profile")
    sources = require_list(runtime["api_sources"], path="$.runtime_binding.api_sources")
    capabilities = require_list(
        runtime["capabilities"], path="$.runtime_binding.capabilities"
    )
    semantic = build_evaluation_semantic_contract_v1(
        profile, sources, capabilities
    )
    semantic_hash = shared_canonical_sha256(semantic)
    runtime_integrity = require_mapping(
        runtime["integrity"], path="$.runtime_binding.integrity"
    )
    require_exact_keys(
        runtime_integrity,
        required={"attempt_binding_sha256"},
        path="$.runtime_binding.integrity",
    )
    material = {
        "profile": copy.deepcopy(dict(profile)),
        "api_sources": copy.deepcopy(list(sources)),
        "capabilities": copy.deepcopy(list(capabilities)),
    }
    expected_attempt_hash = shared_canonical_sha256(material)
    declared_attempt_hash = require_sha256(
        root["attempt_binding_sha256"], path="$.attempt_binding_sha256"
    )
    if (
        require_sha256(
            runtime["semantic_contract_sha256"],
            path="$.runtime_binding.semantic_contract_sha256",
        )
        != semantic_hash
        or require_sha256(
            runtime_integrity["attempt_binding_sha256"],
            path="$.runtime_binding.integrity.attempt_binding_sha256",
        )
        != expected_attempt_hash
        or declared_attempt_hash != expected_attempt_hash
        or require_sha256(
            root["semantic_contract_sha256"], path="$.semantic_contract_sha256"
        )
        != semantic_hash
        or require_sha256(root["profile_sha256"], path="$.profile_sha256")
        != shared_canonical_sha256(profile)
        or require_string(root["profile_id"], path="$.profile_id")
        != profile["profile_id"]
    ):
        raise ContractValidationError(
            "attempt_binding",
            "$",
            "attempt execution binding hashes or profile identity are inconsistent",
        )
    normalized = {
        "schema_id": require_enum(
            root["schema_id"], {_ATTEMPT_BINDING_SCHEMA_ID}, path="$.schema_id"
        ),
        "schema_version": require_enum(
            root["schema_version"], {_SCHEMA_VERSION}, path="$.schema_version"
        ),
        "created_at": require_rfc3339(root["created_at"], path="$.created_at"),
        "producer": validate_producer(
            root["producer"], path="$.producer", workstream="evaluation"
        ),
        "logical_run_id": require_string(
            root["logical_run_id"], path="$.logical_run_id"
        ),
        "attempt_run_id": require_string(
            root["attempt_run_id"], path="$.attempt_run_id"
        ),
        "profile_id": profile["profile_id"],
        "profile_sha256": shared_canonical_sha256(profile),
        "semantic_contract_sha256": semantic_hash,
        "attempt_binding_sha256": expected_attempt_hash,
        "runtime_binding": runtime,
        "integrity": _validate_integrity(
            root["integrity"], "artifact_sha256", "$.integrity"
        ),
    }
    _verify_opaque_artifact_hash(normalized)
    return _canonical_opaque(normalized)


def _require_checkpoint_attempt_binding(
    root: Path,
    checkpoint_binding: Mapping[str, Any],
    *,
    semantic_contract_sha256: str,
) -> None:
    path = (
        _attempt_directory(root, str(checkpoint_binding["attempt_run_id"]))
        / "execution_binding.json"
    )
    if not path.exists():
        raise ContractValidationError(
            "checkpoint_execution_binding",
            str(path),
            "checkpoint from another profile lacks attempt-scoped execution evidence",
        )
    attempt = _validate_attempt_binding_artifact(_load_json_object(path))
    if (
        attempt["logical_run_id"] != checkpoint_binding["logical_run_id"]
        or attempt["attempt_run_id"] != checkpoint_binding["attempt_run_id"]
        or attempt["profile_sha256"] != checkpoint_binding["profile_sha256"]
        or attempt["semantic_contract_sha256"] != semantic_contract_sha256
    ):
        raise ContractValidationError(
            "checkpoint_execution_binding",
            str(path),
            "checkpoint does not match its attempt-scoped execution evidence",
        )


def _require_accepted(call: SharedEvaluationRoleCallV1, *, role_id: str) -> None:
    if call.seal.get("role_id") != role_id:
        raise ContractValidationError(
            "role_id", "$.call.seal.role_id", "role runner returned another role"
        )
    if call.outcome.get("status") != "accepted":
        error = call.outcome.get("semantic_error")
        code = error.get("code") if isinstance(error, Mapping) else "rejected"
        raise ContractValidationError(
            "semantic_rejection",
            "$.call.outcome",
            f"{role_id} response failed local validation: {code}",
        )


def _require_stage1_checkpoint_binding(
    checkpoint: Mapping[str, Any],
    *,
    case: Mapping[str, Any],
    packet: Mapping[str, Any],
    context_profile: str,
) -> None:
    prompt = render_sf_bt_reverse_prompt_v3(packet, context_profile=context_profile)
    binding = checkpoint["binding"]
    if (
        binding["case_id"] != case["case_id"]
        or binding["context_profile"] != context_profile
        or binding["stage"] != "back_translation"
        or binding["stage_input_sha256"] != prompt.rendered_prompt_sha256
    ):
        raise ContractValidationError(
            "checkpoint_prompt_binding",
            "$.checkpoint.binding",
            "stage-1 checkpoint differs from its reconstructed prompt",
        )
    result = checkpoint["stage1_result"]
    if (
        result["binding"]["stage1_packet_sha256"]
        != packet["integrity"]["packet_sha256"]
        or result["prompt"]["context_profile"] != context_profile
        or result["prompt"]["rendered_prompt_sha256"]
        != prompt.rendered_prompt_sha256
    ):
        raise ContractValidationError(
            "checkpoint_stage1_binding",
            "$.checkpoint.stage1_result",
            "stage-1 result differs from packet or context profile",
        )


def _require_stage2_checkpoint_binding(
    checkpoint: Mapping[str, Any],
    *,
    case: Mapping[str, Any],
    stage1_packet: Mapping[str, Any],
    stage1_checkpoint: Mapping[str, Any],
    context_profile: str,
    created_at: str,
) -> None:
    semantic_packet = build_sf_bt_probe_semantic_packet_v1(
        case,
        stage1_packet,
        stage1_checkpoint["stage1_result"],
        context_profile=context_profile,
        created_at=created_at,
        producer_code_commit=checkpoint["producer"]["code_commit"],
    )
    prompt = render_sf_bt_semantic_prompt_v3(semantic_packet)
    binding = checkpoint["binding"]
    if (
        binding["case_id"] != case["case_id"]
        or binding["context_profile"] != context_profile
        or binding["stage"] != "semantic_judge"
        or binding["stage_input_sha256"] != prompt.rendered_prompt_sha256
    ):
        raise ContractValidationError(
            "checkpoint_prompt_binding",
            "$.checkpoint.binding",
            "stage-2 checkpoint differs from its reconstructed prompt",
        )


def _attempt_reference(call: SharedEvaluationRoleCallV1) -> tuple[str, int]:
    usage = call.outcome.get("usage")
    if isinstance(usage, Mapping):
        return str(usage["attempt_usage_id"]), int(usage["physical_attempt_index"])
    observation = call.outcome.get("cache_observation")
    if isinstance(observation, Mapping) and observation.get("lookup_status") == "hit":
        lineage = derive_llm_attempt_identity(
            seal=call.seal,
            logical_request_id=call.outcome["logical_request_id"],
            semantic_attempt_index=1,
            transport_retry_ordinal=0,
        )
        return str(lineage["attempt_usage_id"]), 1
    raise ContractValidationError(
        "attempt_provenance", "$.call.outcome", "accepted call lacks attempt evidence"
    )


def _model_profile(call: SharedEvaluationRoleCallV1) -> dict[str, str]:
    source = call.seal["primary"]["source"]
    target = call.seal["role_binding"]["record"]["primary"]
    requested = target["requested_model_id"]
    return {
        "provider_id": source["source_id"],
        "model_id": requested,
        "model_version": requested,
        "model_family": requested,
        "profile_sha256": call.seal["profile"]["sha256"],
    }


def _validate_checkpoint_binding(value: Any) -> dict[str, Any]:
    row = require_mapping(value, path="$.binding")
    required = {
        "manifest_sha256",
        "fixture_sha256",
        "logical_run_id",
        "profile_sha256",
        "case_id",
        "context_profile",
        "stage",
        "stage_input_sha256",
        "attempt_run_id",
        "role_id",
    }
    require_exact_keys(row, required=required, path="$.binding")
    stage = require_enum(row["stage"], _STAGES, path="$.binding.stage")
    role = require_enum(
        row["role_id"],
        {SF_BT_BACK_TRANSLATOR_ROLE_ID, SF_BT_SEMANTIC_JUDGE_ROLE_ID},
        path="$.binding.role_id",
    )
    expected_role = (
        SF_BT_BACK_TRANSLATOR_ROLE_ID
        if stage == "back_translation"
        else SF_BT_SEMANTIC_JUDGE_ROLE_ID
    )
    if role != expected_role:
        raise ContractValidationError(
            "stage_role", "$.binding.role_id", "role does not match checkpoint stage"
        )
    return {
        "manifest_sha256": require_sha256(
            row["manifest_sha256"], path="$.binding.manifest_sha256"
        ),
        "fixture_sha256": require_sha256(
            row["fixture_sha256"], path="$.binding.fixture_sha256"
        ),
        "logical_run_id": require_string(
            row["logical_run_id"], path="$.binding.logical_run_id"
        ),
        "profile_sha256": require_sha256(
            row["profile_sha256"], path="$.binding.profile_sha256"
        ),
        "case_id": require_string(row["case_id"], path="$.binding.case_id"),
        "context_profile": require_enum(
            row["context_profile"], _CONTEXT_PROFILES, path="$.binding.context_profile"
        ),
        "stage": stage,
        "stage_input_sha256": require_sha256(
            row["stage_input_sha256"], path="$.binding.stage_input_sha256"
        ),
        "attempt_run_id": require_string(
            row["attempt_run_id"], path="$.binding.attempt_run_id"
        ),
        "role_id": role,
    }


def _validate_call_evidence(value: Any) -> dict[str, Any]:
    row = require_mapping(value, path="$.call_evidence")
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
        path="$.call_evidence",
    )
    provider_called = row["provider_called"]
    if not isinstance(provider_called, bool):
        raise ContractValidationError(
            "type", "$.call_evidence.provider_called", "expected boolean"
        )
    backend_status = require_enum(
        row["backend_status"],
        {"provider_succeeded", "cache_hit"},
        path="$.call_evidence.backend_status",
    )
    usage = require_nullable_string(
        row["attempt_usage_id"], path="$.call_evidence.attempt_usage_id"
    )
    cache = require_nullable_string(
        row["cache_observation_id"], path="$.call_evidence.cache_observation_id"
    )
    if backend_status == "provider_succeeded" and (not provider_called or usage is None):
        raise ContractValidationError(
            "call_evidence", "$.call_evidence", "provider success lacks usage evidence"
        )
    if backend_status == "cache_hit" and (provider_called or cache is None):
        raise ContractValidationError(
            "call_evidence", "$.call_evidence", "cache hit evidence is inconsistent"
        )
    return {
        "seal_sha256": require_sha256(
            row["seal_sha256"], path="$.call_evidence.seal_sha256"
        ),
        "backend_status": backend_status,
        "provider_called": provider_called,
        "response_artifact_sha256": require_sha256(
            row["response_artifact_sha256"],
            path="$.call_evidence.response_artifact_sha256",
        ),
        "attempt_usage_id": usage,
        "cache_observation_id": cache,
    }


def _validate_semantic_output(value: Any, *, stage: str) -> dict[str, Any]:
    row = require_mapping(value, path="$.semantic_output")
    if stage == "back_translation":
        require_exact_keys(row, required={"back_translation"}, path="$.semantic_output")
        return {
            "back_translation": require_string(
                row["back_translation"], path="$.semantic_output.back_translation"
            )
        }
    require_exact_keys(
        row, required={"score", "flags", "note"}, path="$.semantic_output"
    )
    score = require_int(row["score"], path="$.semantic_output.score", minimum=0)
    if score not in {0, 25, 50, 75, 100}:
        raise ContractValidationError(
            "score_band", "$.semantic_output.score", "score is outside closed bands"
        )
    return {
        "score": score,
        "flags": _string_list(row["flags"], "$.semantic_output.flags"),
        "note": require_string(row["note"], path="$.semantic_output.note", maximum=240),
    }


def _validate_result_root_binding(value: Any) -> dict[str, Any]:
    row = require_mapping(value, path="$.binding")
    required = {
        "fixture_set_id",
        "fixture_sha256",
        "stratum",
        "case_ids",
        "context_profiles",
        "logical_run_id",
        "profile_id",
        "profile_sha256",
        "manifest_sha256",
    }
    require_exact_keys(row, required=required, path="$.binding")
    return {
        "fixture_set_id": require_string(
            row["fixture_set_id"], path="$.binding.fixture_set_id"
        ),
        "fixture_sha256": require_sha256(
            row["fixture_sha256"], path="$.binding.fixture_sha256"
        ),
        "stratum": require_enum(row["stratum"], {_P2_STRATUM}, path="$.binding.stratum"),
        "case_ids": _string_list(row["case_ids"], "$.binding.case_ids"),
        "context_profiles": _string_list(
            row["context_profiles"], "$.binding.context_profiles"
        ),
        "logical_run_id": require_string(
            row["logical_run_id"], path="$.binding.logical_run_id"
        ),
        "profile_id": require_string(row["profile_id"], path="$.binding.profile_id"),
        "profile_sha256": require_sha256(
            row["profile_sha256"], path="$.binding.profile_sha256"
        ),
        "manifest_sha256": require_sha256(
            row["manifest_sha256"], path="$.binding.manifest_sha256"
        ),
    }


def _validate_coverage(value: Any) -> dict[str, int]:
    row = require_mapping(value, path="$.coverage")
    required = {
        "case_count",
        "context_profile_count",
        "expected_stage_checkpoint_count",
        "accepted_stage_checkpoint_count",
    }
    require_exact_keys(row, required=required, path="$.coverage")
    return {
        key: require_int(row[key], path=f"$.coverage.{key}", minimum=0)
        for key in required
    }


def _validate_metrics(value: Any) -> list[dict[str, Any]]:
    raw = require_list(value, path="$.metrics")
    rows: list[dict[str, Any]] = []
    for index, item in enumerate(raw):
        path = f"$.metrics[{index}]"
        row = require_mapping(item, path=path)
        required = {
            "context_profile",
            "case_count",
            "omission_detected_count",
            "omission_detection_rate",
            "coverage_mismatch_count",
            "coverage_mismatch_rate",
        }
        require_exact_keys(row, required=required, path=path)
        case_count = require_int(row["case_count"], path=f"{path}.case_count", minimum=1)
        detected = require_int(
            row["omission_detected_count"], path=f"{path}.omission_detected_count", minimum=0
        )
        coverage = require_int(
            row["coverage_mismatch_count"], path=f"{path}.coverage_mismatch_count", minimum=0
        )
        detection_rate = _finite_rate(
            row["omission_detection_rate"], f"{path}.omission_detection_rate"
        )
        coverage_rate = _finite_rate(
            row["coverage_mismatch_rate"], f"{path}.coverage_mismatch_rate"
        )
        if detected > case_count or coverage > case_count:
            raise ContractValidationError("metric_count", path, "metric count exceeds case count")
        if detection_rate != detected / case_count or coverage_rate != coverage / case_count:
            raise ContractValidationError("metric_rate", path, "rate differs from exact count")
        rows.append(
            {
                "context_profile": require_enum(
                    row["context_profile"], _CONTEXT_PROFILES, path=f"{path}.context_profile"
                ),
                "case_count": case_count,
                "omission_detected_count": detected,
                "omission_detection_rate": detection_rate,
                "coverage_mismatch_count": coverage,
                "coverage_mismatch_rate": coverage_rate,
            }
        )
    if [row["context_profile"] for row in rows] != list(_CONTEXT_PROFILES):
        raise ContractValidationError("metric_profiles", "$.metrics", "profile order differs")
    return rows


def _validate_result_rows(value: Any) -> list[dict[str, Any]]:
    raw = require_list(value, path="$.rows")
    rows: list[dict[str, Any]] = []
    identities: list[str] = []
    for index, item in enumerate(raw):
        path = f"$.rows[{index}]"
        row = require_mapping(item, path=path)
        required = {
            "case_id",
            "context_profile",
            "score",
            "flags",
            "omission_detected",
            "back_translation_checkpoint_sha256",
            "semantic_checkpoint_sha256",
        }
        require_exact_keys(row, required=required, path=path)
        score = require_int(row["score"], path=f"{path}.score", minimum=0)
        if score not in {0, 25, 50, 75, 100}:
            raise ContractValidationError("score_band", f"{path}.score", "invalid score")
        omitted = row["omission_detected"]
        if not isinstance(omitted, bool) or omitted != (score < 100):
            raise ContractValidationError(
                "omission_detected", f"{path}.omission_detected", "flag differs from score"
            )
        case_id = require_string(row["case_id"], path=f"{path}.case_id")
        profile = require_enum(
            row["context_profile"], _CONTEXT_PROFILES, path=f"{path}.context_profile"
        )
        identities.append(f"{case_id}\x1f{profile}")
        rows.append(
            {
                "case_id": case_id,
                "context_profile": profile,
                "score": score,
                "flags": _string_list(row["flags"], f"{path}.flags"),
                "omission_detected": omitted,
                "back_translation_checkpoint_sha256": require_sha256(
                    row["back_translation_checkpoint_sha256"],
                    path=f"{path}.back_translation_checkpoint_sha256",
                ),
                "semantic_checkpoint_sha256": require_sha256(
                    row["semantic_checkpoint_sha256"],
                    path=f"{path}.semantic_checkpoint_sha256",
                ),
            }
        )
    require_unique(identities, path="$.rows")
    return rows


def _derive_metrics_from_rows(rows: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    metrics: list[dict[str, Any]] = []
    for profile in _CONTEXT_PROFILES:
        selected = [row for row in rows if row["context_profile"] == profile]
        count = len(selected)
        detected = sum(bool(row["omission_detected"]) for row in selected)
        coverage = sum("coverage_mismatch" in row["flags"] for row in selected)
        metrics.append(
            {
                "context_profile": profile,
                "case_count": count,
                "omission_detected_count": detected,
                "omission_detection_rate": detected / count if count else 0.0,
                "coverage_mismatch_count": coverage,
                "coverage_mismatch_rate": coverage / count if count else 0.0,
            }
        )
    return metrics


def _validate_result_binding(result: Mapping[str, Any], manifest: Mapping[str, Any]) -> None:
    expected = {**manifest["binding"], "manifest_sha256": manifest["integrity"]["manifest_sha256"]}
    if result["binding"] != expected:
        raise ContractValidationError("result_binding", "$.binding", "result differs from manifest")


def _validate_integrity(value: Any, field: str, path: str) -> dict[str, str]:
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


def _seal(payload: Mapping[str, Any], schema_id: str) -> dict[str, Any]:
    return seal_payload(payload, policy=_POLICY, hash_path=_HASH_PATHS[schema_id])


def _seal_opaque_artifact(payload: Mapping[str, Any]) -> dict[str, Any]:
    sealed = copy.deepcopy(dict(payload))
    integrity = dict(require_mapping(sealed["integrity"], path="$.integrity"))
    require_exact_keys(
        integrity, required={"artifact_sha256"}, path="$.integrity"
    )
    integrity["artifact_sha256"] = "0" * 64
    sealed["integrity"] = integrity
    integrity["artifact_sha256"] = shared_canonical_sha256(sealed)
    return sealed


def _verify_opaque_artifact_hash(payload: Mapping[str, Any]) -> None:
    observed = payload["integrity"]["artifact_sha256"]
    candidate = copy.deepcopy(dict(payload))
    candidate["integrity"]["artifact_sha256"] = "0" * 64
    if shared_canonical_sha256(candidate) != observed:
        raise ContractValidationError(
            "self_hash", "$.integrity.artifact_sha256", "self-hash mismatch"
        )


def _verify_self_hash(payload: Mapping[str, Any], schema_id: str) -> None:
    if not verify_payload_hash(payload, policy=_POLICY, hash_path=_HASH_PATHS[schema_id]):
        raise ContractValidationError("self_hash", "$.integrity", "self-hash mismatch")


def _canonical(payload: Mapping[str, Any]) -> dict[str, Any]:
    row = canonicalize(payload, policy=_POLICY)
    if not isinstance(row, dict):
        raise AssertionError("canonical contract must remain an object")
    return row


def _canonical_opaque(payload: Mapping[str, Any]) -> dict[str, Any]:
    value = json.loads(shared_canonical_json(payload))
    if not isinstance(value, dict):
        raise AssertionError("opaque canonical contract must remain an object")
    return value


def _checkpoint_key(case_id: str, context_profile: str, stage: str) -> str:
    return f"{case_id}\x1f{context_profile}\x1f{stage}"


def _checkpoint_filename(key: str) -> str:
    return sha256(key.encode("utf-8")).hexdigest() + ".json"


def _attempt_directory(root: Path, attempt_id: str) -> Path:
    return root / "attempts" / sha256(attempt_id.encode("utf-8")).hexdigest()[:32]


def _string_list(value: Any, path: str) -> list[str]:
    raw = require_list(value, path=path)
    rows = [require_string(item, path=f"{path}[{index}]") for index, item in enumerate(raw)]
    require_unique(rows, path=path)
    return rows


def _finite_rate(value: Any, path: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ContractValidationError("type", path, "expected finite rate")
    result = float(value)
    if not math.isfinite(result) or result < 0 or result > 1:
        raise ContractValidationError("range", path, "rate must be in [0, 1]")
    return result


def _require_root_shape(root: Path) -> None:
    allowed = {
        ".gitattributes",
        "manifest.json",
        "result.json",
        "runtime_binding.json",
        "semantic_contract.json",
        "RUN_HALTED.md",
        "checkpoints",
        "attempts",
        "_state",
    }
    foreign = sorted(path.name for path in root.iterdir() if path.name not in allowed)
    if foreign:
        raise ContractValidationError("foreign_artifact", str(root), f"foreign entries: {foreign}")


def _persist_create_or_equal(path: Path, payload: Mapping[str, Any]) -> None:
    _persist_bytes_create_or_equal(path, (shared_canonical_json(payload) + "\n").encode("utf-8"))


def _persist_bytes_create_or_equal(path: Path, rendered: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if path.read_bytes() != rendered:
            raise ContractValidationError("immutable_artifact", str(path), "persisted bytes differ")
        return
    descriptor, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(rendered)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)


def _load_json_object(path: Path) -> dict[str, Any]:
    try:
        with path.open("r", encoding="utf-8") as handle:
            value = json.load(
                handle,
                object_pairs_hook=_reject_duplicate_keys,
                parse_constant=_reject_non_finite,
            )
    except (OSError, json.JSONDecodeError) as exc:
        raise ContractValidationError("artifact_json", str(path), "artifact is unreadable") from exc
    if not isinstance(value, dict):
        raise ContractValidationError("artifact_json", str(path), "artifact root must be object")
    return value


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    row: dict[str, Any] = {}
    for key, value in pairs:
        if key in row:
            raise ValueError(f"duplicate key: {key}")
        row[key] = value
    return row


def _reject_non_finite(value: str) -> None:
    raise ValueError(f"non-finite JSON constant: {value}")
