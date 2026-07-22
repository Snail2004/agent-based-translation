from __future__ import annotations

from collections import Counter
import hashlib
import json
from typing import Any, Mapping

from pipeline.eval.common_input_v1 import CommonEvaluationInputV1
from pipeline.eval.contracts_v1 import (
    CanonicalPolicy,
    ContractValidationError,
    assert_no_forbidden_runtime_data,
    canonicalize,
    require_enum,
    require_exact_keys,
    require_int,
    require_list,
    require_mapping,
    require_nullable_int,
    require_nullable_string,
    require_rfc3339,
    require_sha256,
    require_string,
    require_unique,
    seal_payload,
    validate_producer,
    verify_payload_hash,
)
from pipeline.eval.llm_profiles_v1 import (
    PJ_JUDGE_ROLE_ID,
    SF_BT_BACK_TRANSLATOR_ROLE_ID,
    SF_BT_SEMANTIC_JUDGE_ROLE_ID,
    evaluation_response_schema_v1,
    evaluation_role_budget_v1,
)
from pipeline.eval.offline_orchestrator_v1 import (
    EvaluationPlanV1,
    build_evaluation_plan,
    validate_evaluation_run_config,
)
from pipeline.eval.scorer_input_packets_v1 import build_scorer_input_packet
from pipeline.eval.scorer_prompts_v3 import (
    PJ_COMMON_CANDIDATE_ID,
    PJ_COMMON_PROMPT_SHA256,
    SF_BT_SEMANTIC_CANDIDATE_ID,
    SF_BT_SEMANTIC_PROMPT_SHA256,
    RenderedPromptV3,
    prepare_pj_prompt_presentations_v3,
    render_sf_bt_reverse_prompt_v3,
)


__all__ = [
    "EVALUATION_LIVE_PILOT_PREFLIGHT_SCHEMA_ID",
    "EVALUATION_LIVE_PILOT_PREFLIGHT_SCHEMA_VERSION",
    "EVALUATION_LIVE_PILOT_CANARY_SCHEMA_VERSION",
    "build_evaluation_live_pilot_canary_preflight",
    "build_evaluation_live_pilot_preflight",
    "seal_evaluation_live_pilot_preflight",
    "validate_evaluation_live_pilot_preflight",
    "validate_evaluation_live_pilot_preflight_binding",
]


EVALUATION_LIVE_PILOT_PREFLIGHT_SCHEMA_ID = "EvaluationLivePilotPreflightV1"
EVALUATION_LIVE_PILOT_PREFLIGHT_SCHEMA_VERSION = "1.0.0"
EVALUATION_LIVE_PILOT_CANARY_SCHEMA_VERSION = "1.1.0"
PREFLIGHT_SELF_HASH_PATH = ("integrity", "preflight_sha256")
SELECTION_ALGORITHM = "source_length_quartile_hash_v1"
CANARY_SELECTION_ALGORITHM = "source_length_tertile_hash_v1"
TOKEN_ESTIMATOR = "utf8_json_bytes_div4_v1"

PREFLIGHT_POLICY = CanonicalPolicy(
    set_like_paths=frozenset({("binding", "selected_arm_ids")}),
    semantic_sequence_paths=frozenset(
        {
            ("selection", "selected_units"),
            ("jobs",),
            ("jobs", "*", "prompts"),
        }
    ),
)

_METHOD_IDS = frozenset({"sf_qe", "sf_bt", "pj"})
_ROLE_IDS = frozenset(
    {
        SF_BT_BACK_TRANSLATOR_ROLE_ID,
        SF_BT_SEMANTIC_JUDGE_ROLE_ID,
        PJ_JUDGE_ROLE_ID,
    }
)
_STAGES = frozenset(
    {
        "sf_bt.back_translation",
        "sf_bt.semantic_judge",
        "pj.canonical",
        "pj.reversed",
    }
)


def build_evaluation_live_pilot_preflight(
    common_input: CommonEvaluationInputV1,
    config_payload: Mapping[str, Any],
    *,
    created_at: str,
    producer_code_commit: str,
    selection_seed: str,
    requested_unit_count: int = 8,
) -> dict[str, Any]:
    """Build a score-free live workload without calling a model or provider."""

    return _build_evaluation_live_pilot_preflight(
        common_input,
        config_payload,
        created_at=created_at,
        producer_code_commit=producer_code_commit,
        selection_seed=selection_seed,
        requested_unit_count=requested_unit_count,
        schema_version=EVALUATION_LIVE_PILOT_PREFLIGHT_SCHEMA_VERSION,
        producer_component="live_pilot_preflight_v1",
        producer_component_version="1.0.0",
        selection_algorithm=SELECTION_ALGORITHM,
        stratum_count=4,
        minimum_unit_count=4,
        maximum_unit_count=32,
    )


def build_evaluation_live_pilot_canary_preflight(
    common_input: CommonEvaluationInputV1,
    config_payload: Mapping[str, Any],
    *,
    created_at: str,
    producer_code_commit: str,
    selection_seed: str,
) -> dict[str, Any]:
    """Build the explicit three-unit quota-safe calibration workload."""

    return _build_evaluation_live_pilot_preflight(
        common_input,
        config_payload,
        created_at=created_at,
        producer_code_commit=producer_code_commit,
        selection_seed=selection_seed,
        requested_unit_count=3,
        schema_version=EVALUATION_LIVE_PILOT_CANARY_SCHEMA_VERSION,
        producer_component="live_pilot_canary_preflight_v1",
        producer_component_version="1.0.0",
        selection_algorithm=CANARY_SELECTION_ALGORITHM,
        stratum_count=3,
        minimum_unit_count=3,
        maximum_unit_count=3,
    )


def _build_evaluation_live_pilot_preflight(
    common_input: CommonEvaluationInputV1,
    config_payload: Mapping[str, Any],
    *,
    created_at: str,
    producer_code_commit: str,
    selection_seed: str,
    requested_unit_count: int,
    schema_version: str,
    producer_component: str,
    producer_component_version: str,
    selection_algorithm: str,
    stratum_count: int,
    minimum_unit_count: int,
    maximum_unit_count: int,
) -> dict[str, Any]:

    timestamp = require_rfc3339(created_at, path="$.created_at")
    code_commit = _require_commit(producer_code_commit, path="$.producer_code_commit")
    seed = require_string(selection_seed, path="$.selection_seed")
    requested = _require_bounded_int(
        requested_unit_count,
        path="$.requested_unit_count",
        minimum=minimum_unit_count,
        maximum=maximum_unit_count,
    )
    config = validate_evaluation_run_config(config_payload)
    plan = build_evaluation_plan(common_input, config)
    _require_supported_plan(plan, config)

    selected_units, available_count = _select_units(
        common_input,
        plan,
        requested_count=requested,
        seed=seed,
        stratum_count=stratum_count,
    )
    selected_unit_ids = {row["unit_id"] for row in selected_units}
    jobs: list[dict[str, Any]] = []
    for job in plan.jobs:
        if job.unit_id not in selected_unit_ids:
            continue
        if job.status != "ready":
            raise ContractValidationError(
                "pilot_job_not_ready",
                "$.plan.jobs",
                "selected pilot units must contain only ready jobs",
            )
        packet = build_scorer_input_packet(
            common_input,
            plan,
            job.job_id,
            created_at=timestamp,
            producer_code_commit=code_commit,
        )
        jobs.append(_build_job_row(job, packet))

    workload = _derive_workload(jobs, selected_unit_count=len(selected_units))
    sealed = seal_evaluation_live_pilot_preflight(
        {
            "schema_id": EVALUATION_LIVE_PILOT_PREFLIGHT_SCHEMA_ID,
            "schema_version": schema_version,
            "created_at": timestamp,
            "producer": {
                "workstream": "evaluation",
                "component": producer_component,
                "component_version": producer_component_version,
                "code_commit": code_commit,
            },
            "binding": {
                "project_id": plan.project_id,
                "document_id": plan.document_id,
                "config_id": plan.config_id,
                "config_sha256": plan.config_sha256,
                "input_set_sha256": plan.input_set_sha256,
                "plan_id": plan.plan_id,
                "plan_sha256": plan.plan_sha256,
                "selected_arm_ids": list(plan.selected_arm_ids),
            },
            "selection": {
                "algorithm": selection_algorithm,
                "seed": seed,
                "requested_unit_count": requested,
                "available_unit_count": available_count,
                "selected_units": selected_units,
            },
            "workload": workload,
            "jobs": jobs,
            "integrity": {"preflight_sha256": "0" * 64},
        }
    )
    return validate_evaluation_live_pilot_preflight(sealed)


def seal_evaluation_live_pilot_preflight(
    payload: Mapping[str, Any],
) -> dict[str, Any]:
    return seal_payload(
        payload,
        policy=PREFLIGHT_POLICY,
        hash_path=PREFLIGHT_SELF_HASH_PATH,
    )


def validate_evaluation_live_pilot_preflight(
    payload: Mapping[str, Any],
) -> dict[str, Any]:
    assert_no_forbidden_runtime_data(payload)
    root = require_mapping(payload, path="$")
    require_exact_keys(
        root,
        required={
            "schema_id",
            "schema_version",
            "created_at",
            "producer",
            "binding",
            "selection",
            "workload",
            "jobs",
            "integrity",
        },
        path="$",
    )
    schema_version = require_enum(
        root["schema_version"],
        {
            EVALUATION_LIVE_PILOT_PREFLIGHT_SCHEMA_VERSION,
            EVALUATION_LIVE_PILOT_CANARY_SCHEMA_VERSION,
        },
        path="$.schema_version",
    )
    normalized = {
        "schema_id": require_enum(
            root["schema_id"],
            {EVALUATION_LIVE_PILOT_PREFLIGHT_SCHEMA_ID},
            path="$.schema_id",
        ),
        "schema_version": schema_version,
        "created_at": require_rfc3339(root["created_at"], path="$.created_at"),
        "producer": validate_producer(
            root["producer"], path="$.producer", workstream="evaluation"
        ),
        "binding": _validate_binding(root["binding"]),
        "selection": _validate_selection(
            root["selection"], schema_version=schema_version
        ),
        "workload": _validate_workload(root["workload"]),
        "jobs": _validate_jobs(root["jobs"]),
        "integrity": _validate_integrity(root["integrity"]),
    }
    _validate_internal_consistency(normalized)
    if not verify_payload_hash(
        normalized,
        policy=PREFLIGHT_POLICY,
        hash_path=PREFLIGHT_SELF_HASH_PATH,
    ):
        raise ContractValidationError(
            "preflight_hash",
            "$.integrity.preflight_sha256",
            "preflight self-hash does not match canonical content",
        )
    canonical = canonicalize(normalized, policy=PREFLIGHT_POLICY)
    if not isinstance(canonical, dict):
        raise AssertionError("canonical pilot preflight must remain an object")
    return canonical


def validate_evaluation_live_pilot_preflight_binding(
    payload: Mapping[str, Any],
    common_input: CommonEvaluationInputV1,
    config_payload: Mapping[str, Any],
) -> dict[str, Any]:
    validated = validate_evaluation_live_pilot_preflight(payload)
    if (
        validated["schema_version"]
        == EVALUATION_LIVE_PILOT_CANARY_SCHEMA_VERSION
    ):
        expected = build_evaluation_live_pilot_canary_preflight(
            common_input,
            config_payload,
            created_at=validated["created_at"],
            producer_code_commit=validated["producer"]["code_commit"],
            selection_seed=validated["selection"]["seed"],
        )
    else:
        expected = build_evaluation_live_pilot_preflight(
            common_input,
            config_payload,
            created_at=validated["created_at"],
            producer_code_commit=validated["producer"]["code_commit"],
            selection_seed=validated["selection"]["seed"],
            requested_unit_count=validated["selection"]["requested_unit_count"],
        )
    if validated != expected:
        raise ContractValidationError(
            "preflight_binding",
            "$",
            "preflight does not match its exact input, config and selection rule",
        )
    return validated


def _require_supported_plan(
    plan: EvaluationPlanV1, config: Mapping[str, Any]
) -> None:
    if len(plan.selected_arm_ids) != 2:
        raise ContractValidationError(
            "pilot_arm_count", "$.selected_arm_ids", "live pilot requires exactly two arms"
        )
    method_ids = {row["method_id"] for row in config["methods"]}
    if method_ids != _METHOD_IDS:
        raise ContractValidationError(
            "pilot_methods",
            "$.methods",
            "live pilot requires exactly sf_qe, sf_bt and pj",
        )
    if len(config["comparison_pairs"]) != 1:
        raise ContractValidationError(
            "pilot_pair_count",
            "$.comparison_pairs",
            "live pilot requires exactly one comparison pair",
        )
    if config["retry_policy"]["max_transport_attempts"] != 1:
        raise ContractValidationError(
            "pilot_transport_attempts",
            "$.retry_policy.max_transport_attempts",
            "live pilot permits exactly one sealed physical attempt per call",
        )


def _select_units(
    common_input: CommonEvaluationInputV1,
    plan: EvaluationPlanV1,
    *,
    requested_count: int,
    seed: str,
    stratum_count: int,
) -> tuple[list[dict[str, Any]], int]:
    block_index = {row.block_id: row for row in common_input.blocks}
    jobs_by_unit: dict[str, list[Any]] = {}
    for job in plan.jobs:
        jobs_by_unit.setdefault(job.unit_id, []).append(job)

    candidates: list[tuple[Any, int]] = []
    for unit in plan.units:
        jobs = jobs_by_unit.get(unit.unit_id, [])
        counts = Counter(job.method_id for job in jobs if job.status == "ready")
        blocked = any(job.status != "ready" for job in jobs)
        if blocked or counts != Counter({"sf_qe": 2, "sf_bt": 2, "pj": 1}):
            continue
        block = block_index[unit.block_id]
        candidates.append((unit, len(block.source_text)))
    if len(candidates) < requested_count:
        raise ContractValidationError(
            "pilot_unit_availability",
            "$.plan.units",
            f"requested {requested_count} units but only {len(candidates)} are fully ready",
        )

    by_length = sorted(
        candidates,
        key=lambda item: (item[1], item[0].order_index, item[0].block_id),
    )
    strata: dict[int, list[tuple[Any, int]]] = {
        index: [] for index in range(stratum_count)
    }
    total = len(by_length)
    for rank, item in enumerate(by_length):
        stratum = min(stratum_count - 1, (rank * stratum_count) // total)
        strata[stratum].append(item)

    base, remainder = divmod(requested_count, stratum_count)
    picked: list[tuple[Any, int, int]] = []
    for stratum in range(stratum_count):
        quota = base + (1 if stratum < remainder else 0)
        ranked = sorted(
            strata[stratum],
            key=lambda item: _digest(seed, plan.plan_sha256, item[0].unit_id),
        )
        if len(ranked) < quota:
            raise ContractValidationError(
                "pilot_stratum_availability",
                "$.plan.units",
                f"length stratum {stratum} cannot supply {quota} pilot units",
            )
        picked.extend((unit, chars, stratum) for unit, chars in ranked[:quota])

    picked.sort(key=lambda item: (item[0].order_index, item[0].block_id))
    return (
        [
            {
                "unit_id": unit.unit_id,
                "block_id": unit.block_id,
                "order_index": unit.order_index,
                "length_stratum": stratum,
                "source_char_count": chars,
            }
            for unit, chars, stratum in picked
        ],
        len(candidates),
    )


def _build_job_row(job: Any, packet: Mapping[str, Any]) -> dict[str, Any]:
    prompts: list[dict[str, Any]] = []
    mechanical_equal = False
    if job.method_id == "sf_bt":
        rendered = render_sf_bt_reverse_prompt_v3(
            packet, context_profile="bounded_neighbors"
        )
        prompts.append(
            _rendered_prompt_row(
                stage_id="sf_bt.back_translation",
                role_id=SF_BT_BACK_TRANSLATOR_ROLE_ID,
                rendered=rendered,
            )
        )
        prompts.append(
            _deferred_prompt_row(
                stage_id="sf_bt.semantic_judge",
                role_id=SF_BT_SEMANTIC_JUDGE_ROLE_ID,
                candidate_id=SF_BT_SEMANTIC_CANDIDATE_ID,
                prompt_sha256=SF_BT_SEMANTIC_PROMPT_SHA256,
            )
        )
    elif job.method_id == "pj":
        presentations = prepare_pj_prompt_presentations_v3(packet)
        mechanical_equal = presentations.mechanical_equal
        if not mechanical_equal:
            if presentations.canonical is None or presentations.reversed is None:
                raise AssertionError("non-equal PJ packet must render two presentations")
            prompts.extend(
                [
                    _rendered_prompt_row(
                        stage_id="pj.canonical",
                        role_id=PJ_JUDGE_ROLE_ID,
                        rendered=presentations.canonical,
                    ),
                    _rendered_prompt_row(
                        stage_id="pj.reversed",
                        role_id=PJ_JUDGE_ROLE_ID,
                        rendered=presentations.reversed,
                    ),
                ]
            )
    return {
        "job_id": job.job_id,
        "unit_id": job.unit_id,
        "method_id": job.method_id,
        "presentation_arm_count": len(job.presentation_arm_ids),
        "packet_sha256": packet["integrity"]["packet_sha256"],
        "mechanical_equal": mechanical_equal,
        "api_call_count": len(prompts),
        "prompts": prompts,
    }


def _rendered_prompt_row(
    *, stage_id: str, role_id: str, rendered: RenderedPromptV3
) -> dict[str, Any]:
    budget = evaluation_role_budget_v1(role_id)
    return {
        "stage_id": stage_id,
        "role_id": role_id,
        "render_state": "rendered",
        "candidate_id": rendered.candidate_id,
        "prompt_sha256": rendered.prompt_sha256,
        "rendered_prompt_sha256": rendered.rendered_prompt_sha256,
        "estimated_prompt_tokens": _estimate_prompt_tokens(
            rendered.rendered_prompt,
            evaluation_response_schema_v1(role_id),
        ),
        "max_input_tokens": budget["generation"]["max_input_tokens"],
        "max_output_tokens": budget["generation"]["max_output_tokens"],
    }


def _deferred_prompt_row(
    *, stage_id: str, role_id: str, candidate_id: str, prompt_sha256: str
) -> dict[str, Any]:
    budget = evaluation_role_budget_v1(role_id)
    return {
        "stage_id": stage_id,
        "role_id": role_id,
        "render_state": "deferred_until_upstream_output",
        "candidate_id": candidate_id,
        "prompt_sha256": prompt_sha256,
        "rendered_prompt_sha256": None,
        "estimated_prompt_tokens": None,
        "max_input_tokens": budget["generation"]["max_input_tokens"],
        "max_output_tokens": budget["generation"]["max_output_tokens"],
    }


def _derive_workload(
    jobs: list[dict[str, Any]], *, selected_unit_count: int
) -> dict[str, Any]:
    method_counts = Counter(row["method_id"] for row in jobs)
    prompt_rows = [prompt for row in jobs for prompt in row["prompts"]]
    role_counts = Counter(row["role_id"] for row in prompt_rows)
    rendered = [row for row in prompt_rows if row["render_state"] == "rendered"]
    deferred = [
        row
        for row in prompt_rows
        if row["render_state"] == "deferred_until_upstream_output"
    ]
    reserved_prompt = sum(row["max_input_tokens"] for row in prompt_rows)
    reserved_completion = sum(row["max_output_tokens"] for row in prompt_rows)
    return {
        "selected_unit_count": selected_unit_count,
        "selected_plan_job_count": len(jobs),
        "method_job_counts": {
            "sf_qe": method_counts["sf_qe"],
            "sf_bt": method_counts["sf_bt"],
            "pj": method_counts["pj"],
        },
        "physical_call_counts": {
            "sf_qe_local_rows": method_counts["sf_qe"],
            "sf_bt_back_translation": role_counts[SF_BT_BACK_TRANSLATOR_ROLE_ID],
            "sf_bt_semantic_judge": role_counts[SF_BT_SEMANTIC_JUDGE_ROLE_ID],
            "pj_judge": role_counts[PJ_JUDGE_ROLE_ID],
            "total_api_calls": len(prompt_rows),
            "qualification_probe_call_cap": 3,
        },
        "token_envelope": {
            "estimator": TOKEN_ESTIMATOR,
            "rendered_prompt_count": len(rendered),
            "deferred_prompt_count": len(deferred),
            "estimated_rendered_prompt_tokens": sum(
                row["estimated_prompt_tokens"] for row in rendered
            ),
            "reserved_max_prompt_tokens": reserved_prompt,
            "reserved_max_completion_tokens": reserved_completion,
            "reserved_max_total_tokens": reserved_prompt + reserved_completion,
            "cost_cap_usd": None,
        },
    }


def _estimate_prompt_tokens(prompt: str, response_schema: Mapping[str, Any]) -> int:
    encoded = json.dumps(
        {"prompt": prompt, "response_schema": response_schema},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return max(1, (len(encoded) + 3) // 4)


def _validate_binding(value: Any) -> dict[str, Any]:
    path = "$.binding"
    row = require_mapping(value, path=path)
    require_exact_keys(
        row,
        required={
            "project_id",
            "document_id",
            "config_id",
            "config_sha256",
            "input_set_sha256",
            "plan_id",
            "plan_sha256",
            "selected_arm_ids",
        },
        path=path,
    )
    arms = [
        require_string(item, path=f"{path}.selected_arm_ids[{index}]")
        for index, item in enumerate(require_list(row["selected_arm_ids"], path=f"{path}.selected_arm_ids"))
    ]
    require_unique(arms, path=f"{path}.selected_arm_ids")
    if len(arms) != 2:
        raise ContractValidationError(
            "pilot_arm_count", f"{path}.selected_arm_ids", "pilot requires two arms"
        )
    return {
        "project_id": require_string(row["project_id"], path=f"{path}.project_id"),
        "document_id": require_string(row["document_id"], path=f"{path}.document_id"),
        "config_id": require_string(row["config_id"], path=f"{path}.config_id"),
        "config_sha256": require_sha256(row["config_sha256"], path=f"{path}.config_sha256"),
        "input_set_sha256": require_sha256(row["input_set_sha256"], path=f"{path}.input_set_sha256"),
        "plan_id": require_string(row["plan_id"], path=f"{path}.plan_id"),
        "plan_sha256": require_sha256(row["plan_sha256"], path=f"{path}.plan_sha256"),
        "selected_arm_ids": arms,
    }


def _validate_selection(
    value: Any, *, schema_version: str
) -> dict[str, Any]:
    path = "$.selection"
    if schema_version == EVALUATION_LIVE_PILOT_CANARY_SCHEMA_VERSION:
        selection_algorithm = CANARY_SELECTION_ALGORITHM
        minimum_unit_count = 3
        maximum_unit_count = 3
        maximum_stratum = 2
    else:
        selection_algorithm = SELECTION_ALGORITHM
        minimum_unit_count = 4
        maximum_unit_count = 32
        maximum_stratum = 3
    row = require_mapping(value, path=path)
    require_exact_keys(
        row,
        required={
            "algorithm",
            "seed",
            "requested_unit_count",
            "available_unit_count",
            "selected_units",
        },
        path=path,
    )
    selected: list[dict[str, Any]] = []
    for index, item in enumerate(require_list(row["selected_units"], path=f"{path}.selected_units")):
        item_path = f"{path}.selected_units[{index}]"
        unit = require_mapping(item, path=item_path)
        require_exact_keys(
            unit,
            required={
                "unit_id",
                "block_id",
                "order_index",
                "length_stratum",
                "source_char_count",
            },
            path=item_path,
        )
        selected.append(
            {
                "unit_id": require_string(unit["unit_id"], path=f"{item_path}.unit_id"),
                "block_id": require_string(unit["block_id"], path=f"{item_path}.block_id"),
                "order_index": require_int(unit["order_index"], path=f"{item_path}.order_index", minimum=0),
                "length_stratum": _require_bounded_int(
                    unit["length_stratum"],
                    path=f"{item_path}.length_stratum",
                    minimum=0,
                    maximum=maximum_stratum,
                ),
                "source_char_count": require_int(unit["source_char_count"], path=f"{item_path}.source_char_count", minimum=0),
            }
        )
    require_unique([row["unit_id"] for row in selected], path=f"{path}.selected_units.unit_id")
    require_unique([row["block_id"] for row in selected], path=f"{path}.selected_units.block_id")
    requested = _require_bounded_int(
        row["requested_unit_count"],
        path=f"{path}.requested_unit_count",
        minimum=minimum_unit_count,
        maximum=maximum_unit_count,
    )
    available = require_int(row["available_unit_count"], path=f"{path}.available_unit_count", minimum=requested)
    if len(selected) != requested:
        raise ContractValidationError(
            "pilot_selection_count",
            f"{path}.selected_units",
            "selected unit count differs from the request",
        )
    if [row["order_index"] for row in selected] != sorted(row["order_index"] for row in selected):
        raise ContractValidationError(
            "pilot_selection_order",
            f"{path}.selected_units",
            "selected units must preserve canonical source order",
        )
    return {
        "algorithm": require_enum(
            row["algorithm"], {selection_algorithm}, path=f"{path}.algorithm"
        ),
        "seed": require_string(row["seed"], path=f"{path}.seed"),
        "requested_unit_count": requested,
        "available_unit_count": available,
        "selected_units": selected,
    }


def _validate_workload(value: Any) -> dict[str, Any]:
    path = "$.workload"
    row = require_mapping(value, path=path)
    require_exact_keys(
        row,
        required={
            "selected_unit_count",
            "selected_plan_job_count",
            "method_job_counts",
            "physical_call_counts",
            "token_envelope",
        },
        path=path,
    )
    methods = _validate_count_map(
        row["method_job_counts"],
        keys={"sf_qe", "sf_bt", "pj"},
        path=f"{path}.method_job_counts",
    )
    calls = _validate_count_map(
        row["physical_call_counts"],
        keys={
            "sf_qe_local_rows",
            "sf_bt_back_translation",
            "sf_bt_semantic_judge",
            "pj_judge",
            "total_api_calls",
            "qualification_probe_call_cap",
        },
        path=f"{path}.physical_call_counts",
    )
    envelope_path = f"{path}.token_envelope"
    envelope = require_mapping(row["token_envelope"], path=envelope_path)
    require_exact_keys(
        envelope,
        required={
            "estimator",
            "rendered_prompt_count",
            "deferred_prompt_count",
            "estimated_rendered_prompt_tokens",
            "reserved_max_prompt_tokens",
            "reserved_max_completion_tokens",
            "reserved_max_total_tokens",
            "cost_cap_usd",
        },
        path=envelope_path,
    )
    if envelope["cost_cap_usd"] is not None:
        raise ContractValidationError(
            "cost_authority",
            f"{envelope_path}.cost_cap_usd",
            "preflight cannot invent a tariff-derived cost cap",
        )
    return {
        "selected_unit_count": require_int(row["selected_unit_count"], path=f"{path}.selected_unit_count", minimum=1),
        "selected_plan_job_count": require_int(row["selected_plan_job_count"], path=f"{path}.selected_plan_job_count", minimum=1),
        "method_job_counts": methods,
        "physical_call_counts": calls,
        "token_envelope": {
            "estimator": require_enum(envelope["estimator"], {TOKEN_ESTIMATOR}, path=f"{envelope_path}.estimator"),
            "rendered_prompt_count": require_int(envelope["rendered_prompt_count"], path=f"{envelope_path}.rendered_prompt_count", minimum=0),
            "deferred_prompt_count": require_int(envelope["deferred_prompt_count"], path=f"{envelope_path}.deferred_prompt_count", minimum=0),
            "estimated_rendered_prompt_tokens": require_int(envelope["estimated_rendered_prompt_tokens"], path=f"{envelope_path}.estimated_rendered_prompt_tokens", minimum=0),
            "reserved_max_prompt_tokens": require_int(envelope["reserved_max_prompt_tokens"], path=f"{envelope_path}.reserved_max_prompt_tokens", minimum=0),
            "reserved_max_completion_tokens": require_int(envelope["reserved_max_completion_tokens"], path=f"{envelope_path}.reserved_max_completion_tokens", minimum=0),
            "reserved_max_total_tokens": require_int(envelope["reserved_max_total_tokens"], path=f"{envelope_path}.reserved_max_total_tokens", minimum=0),
            "cost_cap_usd": None,
        },
    }


def _validate_count_map(value: Any, *, keys: set[str], path: str) -> dict[str, int]:
    row = require_mapping(value, path=path)
    require_exact_keys(row, required=keys, path=path)
    return {
        key: require_int(row[key], path=f"{path}.{key}", minimum=0)
        for key in sorted(keys)
    }


def _validate_jobs(value: Any) -> list[dict[str, Any]]:
    path = "$.jobs"
    result: list[dict[str, Any]] = []
    for index, item in enumerate(require_list(value, path=path)):
        item_path = f"{path}[{index}]"
        row = require_mapping(item, path=item_path)
        require_exact_keys(
            row,
            required={
                "job_id",
                "unit_id",
                "method_id",
                "presentation_arm_count",
                "packet_sha256",
                "mechanical_equal",
                "api_call_count",
                "prompts",
            },
            path=item_path,
        )
        mechanical_equal = row["mechanical_equal"]
        if not isinstance(mechanical_equal, bool):
            raise ContractValidationError(
                "type", f"{item_path}.mechanical_equal", "expected a boolean"
            )
        prompts = _validate_prompts(row["prompts"], path=f"{item_path}.prompts")
        result.append(
            {
                "job_id": require_string(row["job_id"], path=f"{item_path}.job_id"),
                "unit_id": require_string(row["unit_id"], path=f"{item_path}.unit_id"),
                "method_id": require_enum(row["method_id"], _METHOD_IDS, path=f"{item_path}.method_id"),
                "presentation_arm_count": _require_bounded_int(
                    row["presentation_arm_count"],
                    path=f"{item_path}.presentation_arm_count",
                    minimum=1,
                    maximum=2,
                ),
                "packet_sha256": require_sha256(row["packet_sha256"], path=f"{item_path}.packet_sha256"),
                "mechanical_equal": mechanical_equal,
                "api_call_count": _require_bounded_int(
                    row["api_call_count"],
                    path=f"{item_path}.api_call_count",
                    minimum=0,
                    maximum=2,
                ),
                "prompts": prompts,
            }
        )
    require_unique([row["job_id"] for row in result], path=f"{path}.job_id")
    return result


def _validate_prompts(value: Any, *, path: str) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for index, item in enumerate(require_list(value, path=path)):
        item_path = f"{path}[{index}]"
        row = require_mapping(item, path=item_path)
        require_exact_keys(
            row,
            required={
                "stage_id",
                "role_id",
                "render_state",
                "candidate_id",
                "prompt_sha256",
                "rendered_prompt_sha256",
                "estimated_prompt_tokens",
                "max_input_tokens",
                "max_output_tokens",
            },
            path=item_path,
        )
        state = require_enum(
            row["render_state"],
            {"rendered", "deferred_until_upstream_output"},
            path=f"{item_path}.render_state",
        )
        rendered_hash = require_nullable_string(
            row["rendered_prompt_sha256"], path=f"{item_path}.rendered_prompt_sha256"
        )
        if rendered_hash is not None:
            rendered_hash = require_sha256(rendered_hash, path=f"{item_path}.rendered_prompt_sha256")
        estimated = require_nullable_int(
            row["estimated_prompt_tokens"], path=f"{item_path}.estimated_prompt_tokens", minimum=1
        )
        if state == "rendered" and (rendered_hash is None or estimated is None):
            raise ContractValidationError(
                "rendered_prompt_evidence",
                item_path,
                "rendered prompts require a hash and token estimate",
            )
        if state != "rendered" and (rendered_hash is not None or estimated is not None):
            raise ContractValidationError(
                "deferred_prompt_evidence",
                item_path,
                "deferred prompts cannot claim rendered evidence",
            )
        role_id = require_enum(row["role_id"], _ROLE_IDS, path=f"{item_path}.role_id")
        budget = evaluation_role_budget_v1(role_id)
        result.append(
            {
                "stage_id": require_enum(row["stage_id"], _STAGES, path=f"{item_path}.stage_id"),
                "role_id": role_id,
                "render_state": state,
                "candidate_id": require_string(row["candidate_id"], path=f"{item_path}.candidate_id"),
                "prompt_sha256": require_sha256(row["prompt_sha256"], path=f"{item_path}.prompt_sha256"),
                "rendered_prompt_sha256": rendered_hash,
                "estimated_prompt_tokens": estimated,
                "max_input_tokens": _require_exact_int(
                    row["max_input_tokens"],
                    expected=budget["generation"]["max_input_tokens"],
                    path=f"{item_path}.max_input_tokens",
                ),
                "max_output_tokens": _require_exact_int(
                    row["max_output_tokens"],
                    expected=budget["generation"]["max_output_tokens"],
                    path=f"{item_path}.max_output_tokens",
                ),
            }
        )
    return result


def _validate_integrity(value: Any) -> dict[str, str]:
    path = "$.integrity"
    row = require_mapping(value, path=path)
    require_exact_keys(row, required={"preflight_sha256"}, path=path)
    return {
        "preflight_sha256": require_sha256(
            row["preflight_sha256"], path=f"{path}.preflight_sha256"
        )
    }


def _validate_internal_consistency(payload: Mapping[str, Any]) -> None:
    selection = payload["selection"]
    workload = payload["workload"]
    jobs = payload["jobs"]
    if workload["selected_unit_count"] != len(selection["selected_units"]):
        raise ContractValidationError(
            "workload_unit_count", "$.workload.selected_unit_count", "count differs from selection"
        )
    if workload["selected_plan_job_count"] != len(jobs):
        raise ContractValidationError(
            "workload_job_count", "$.workload.selected_plan_job_count", "count differs from jobs"
        )
    selected_ids = {row["unit_id"] for row in selection["selected_units"]}
    if {row["unit_id"] for row in jobs} != selected_ids:
        raise ContractValidationError(
            "workload_unit_cover", "$.jobs", "jobs must cover exactly the selected units"
        )
    expected = _derive_workload(jobs, selected_unit_count=len(selected_ids))
    if workload != expected:
        raise ContractValidationError(
            "workload_recompute", "$.workload", "workload does not match job prompt evidence"
        )
    per_unit = Counter(row["unit_id"] for row in jobs)
    if any(count != 5 for count in per_unit.values()):
        raise ContractValidationError(
            "workload_job_cover", "$.jobs", "each selected unit requires five planned jobs"
        )
    for row in jobs:
        prompt_count = len(row["prompts"])
        if row["api_call_count"] != prompt_count:
            raise ContractValidationError(
                "job_call_count", "$.jobs", "job API call count differs from prompts"
            )
        if row["method_id"] == "sf_qe" and (prompt_count or row["mechanical_equal"]):
            raise ContractValidationError(
                "sf_qe_execution_shape", "$.jobs", "SF-QE must remain local and non-pairwise"
            )
        if row["method_id"] == "sf_bt" and prompt_count != 2:
            raise ContractValidationError(
                "sf_bt_execution_shape", "$.jobs", "SF-BT requires two sealed semantic calls"
            )
        if row["method_id"] == "pj":
            expected_count = 0 if row["mechanical_equal"] else 2
            if prompt_count != expected_count:
                raise ContractValidationError(
                    "pj_execution_shape", "$.jobs", "PJ must be code-only equal or two-order judged"
                )


def _require_commit(value: Any, *, path: str) -> str:
    row = require_string(value, path=path)
    if len(row) != 40 or any(character not in "0123456789abcdef" for character in row):
        raise ContractValidationError("commit", path, "expected a lowercase 40-character commit")
    return row


def _require_bounded_int(
    value: Any,
    *,
    path: str,
    minimum: int,
    maximum: int,
) -> int:
    result = require_int(value, path=path, minimum=minimum)
    if result > maximum:
        raise ContractValidationError(
            "range", path, f"must be <= {maximum}"
        )
    return result


def _require_exact_int(value: Any, *, expected: int, path: str) -> int:
    result = require_int(value, path=path)
    if result != expected:
        raise ContractValidationError(
            "value", path, f"expected exactly {expected}"
        )
    return result


def _digest(*parts: str) -> str:
    return hashlib.sha256("\x1f".join(parts).encode("utf-8")).hexdigest()
