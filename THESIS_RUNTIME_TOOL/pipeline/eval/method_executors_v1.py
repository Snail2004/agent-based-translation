from __future__ import annotations

import copy
import math
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Sequence

from pipeline.eval.common_input_v1 import CommonEvaluationInputV1
from pipeline.eval.contracts_v1 import ContractValidationError, canonical_sha256
from pipeline.eval.llm_adapter_v1 import (
    EVALUATION_LLM_CACHE_MODES,
    build_evaluation_input_bindings_v1,
    build_evaluation_request_body_v1,
    execute_evaluation_llm_attempt_v1,
)
from pipeline.eval.llm_profiles_v1 import (
    PJ_JUDGE_ROLE_ID,
    SF_BT_BACK_TRANSLATOR_ROLE_ID,
    SF_BT_SEMANTIC_JUDGE_ROLE_ID,
)
from pipeline.eval.offline_orchestrator_v1 import (
    EvaluationPlanV1,
    build_evaluation_plan,
    validate_evaluation_run_config,
)
from pipeline.eval.scorer_input_packets_v1 import (
    validate_scorer_input_packet,
    validate_scorer_input_packet_binding,
)
from pipeline.eval.scorer_prompts_v3 import (
    PJPromptPresentationsV3,
    RenderedPromptV3,
    prepare_pj_prompt_presentations_v3,
    render_sf_bt_reverse_prompt_v3,
    render_sf_bt_semantic_prompt_v3,
)
from pipeline.eval.sf_bt_stage_contracts_v1 import (
    build_sf_back_translation_result,
    build_sf_bt_semantic_judge_packet,
)
from pipeline.llm_backend import (
    SharedLlmBackend,
    canonical_sha256 as shared_canonical_sha256,
    derive_llm_attempt_identity,
    resolve_llm_run_seal,
    validate_api_source,
    validate_capability_evidence,
    validate_pipeline_profile,
)


__all__ = [
    "EvaluationMethodExecutorV1",
    "SharedEvaluationRoleCallV1",
    "SharedEvaluationRoleRunnerV1",
    "build_evaluation_semantic_contract_v1",
]


LocalSfQeScorerV1 = Callable[[str, str], float]


@dataclass(frozen=True, slots=True)
class SharedEvaluationRoleCallV1:
    seal: dict[str, Any]
    outcome: dict[str, Any]


class SharedEvaluationRoleRunnerV1:
    """Resolve and execute one sealed Evaluation semantic role attempt.

    This object chooses no model, source, credential, fallback, or retry. Those
    values must already be present in the supplied concrete pipeline profile.
    """

    def __init__(
        self,
        *,
        backend: SharedLlmBackend,
        profile: Mapping[str, Any],
        api_sources: Sequence[Mapping[str, Any]],
        capability_evidence: Sequence[Mapping[str, Any]],
        run_id: str,
        attempt_run_id: str,
        cache_mode: str = "bypass",
        cost_fact: Mapping[str, Any] | None = None,
    ) -> None:
        if cache_mode not in EVALUATION_LLM_CACHE_MODES:
            raise ContractValidationError(
                "cache_mode",
                "$.cache_mode",
                f"cache_mode must be one of {sorted(EVALUATION_LLM_CACHE_MODES)}",
            )
        self._backend = backend
        self._profile = validate_pipeline_profile(profile)
        self._sources = tuple(validate_api_source(row) for row in api_sources)
        self._capabilities = tuple(
            validate_capability_evidence(row) for row in capability_evidence
        )
        self._run_id = run_id
        self._attempt_run_id = attempt_run_id
        self._cache_mode = cache_mode
        self._cost_fact = None if cost_fact is None else copy.deepcopy(dict(cost_fact))

    @property
    def execution_binding(self) -> dict[str, str]:
        return {
            "evaluation_logical_run_id": self._run_id,
            "evaluation_attempt_run_id": self._attempt_run_id,
            "evaluation_profile_id": self._profile["profile_id"],
            "evaluation_profile_sha256": shared_canonical_sha256(self._profile),
        }

    @property
    def cache_mode(self) -> str:
        return self._cache_mode

    @property
    def semantic_contract(self) -> dict[str, Any]:
        """Return the row-independent semantic contract for resumable eval work.

        Physical source identity, credential commitment, and quota bucket are
        deliberately excluded. Model identity, prompts, validators, generation
        settings, retry policy, output mode, and provider route family remain
        load-bearing.
        """

        return build_evaluation_semantic_contract_v1(
            self._profile, self._sources, self._capabilities
        )

    @property
    def attempt_runtime_binding(self) -> dict[str, Any]:
        sources = sorted(
            (copy.deepcopy(row) for row in self._sources),
            key=lambda row: (row["source_id"], row["source_revision"]),
        )
        capabilities = sorted(
            (copy.deepcopy(row) for row in self._capabilities),
            key=lambda row: (row["capability_id"], row["capability_revision"]),
        )
        material = {
            "profile": copy.deepcopy(self._profile),
            "api_sources": sources,
            "capabilities": capabilities,
        }
        return {
            "schema_id": "EvaluationAttemptRuntimeBindingV1",
            "schema_version": "1.0.0",
            "semantic_contract_sha256": shared_canonical_sha256(
                self.semantic_contract
            ),
            **material,
            "integrity": {
                "attempt_binding_sha256": shared_canonical_sha256(material)
            },
        }

    def execute(
        self,
        *,
        role_id: str,
        scorer_input_packet_sha256: str,
        rendered_prompt: RenderedPromptV3,
        stage_id: str,
        logical_request_id: str,
        extra_bindings: Sequence[Mapping[str, str]] = (),
    ) -> SharedEvaluationRoleCallV1:
        role = next(
            (
                row
                for row in self._profile["role_bindings"]
                if row["role_id"] == role_id
            ),
            None,
        )
        if role is None:
            raise ContractValidationError(
                "role_id", "$.role_id", "concrete profile lacks requested role"
            )
        target = role["primary"]
        source = self._find_source(target)
        capability = self._find_capability(target)
        request_body = build_evaluation_request_body_v1(
            profile=self._profile,
            role_id=role_id,
            source=source,
            capability=capability,
            rendered_prompt=rendered_prompt,
        )
        bindings = build_evaluation_input_bindings_v1(
            scorer_input_packet_sha256=scorer_input_packet_sha256,
            rendered_prompt=rendered_prompt,
            request_body=request_body,
            extra_bindings=extra_bindings,
        )
        seal = resolve_llm_run_seal(
            profile=self._profile,
            api_sources=self._sources,
            capability_evidence=self._capabilities,
            role_id=role_id,
            run_id=self._run_id,
            attempt_run_id=self._attempt_run_id,
            stage_id=stage_id,
            input_bindings=bindings,
        )
        outcome = execute_evaluation_llm_attempt_v1(
            backend=self._backend,
            seal=seal,
            logical_request_id=logical_request_id,
            rendered_prompt=rendered_prompt,
            cache_mode=self._cache_mode,
            cost_fact=self._cost_fact,
        )
        return SharedEvaluationRoleCallV1(seal=seal, outcome=outcome)

    def _find_source(self, target: Mapping[str, Any]) -> dict[str, Any]:
        matches = [
            row
            for row in self._sources
            if row["source_id"] == target["source_id"]
            and row["source_revision"] == target["source_revision"]
        ]
        if len(matches) != 1:
            raise ContractValidationError(
                "source_reference",
                "$.profile.role_bindings[*].primary",
                "role target does not resolve to exactly one API source",
            )
        return matches[0]

    def _find_capability(self, target: Mapping[str, Any]) -> dict[str, Any]:
        matches = [
            row
            for row in self._capabilities
            if row["capability_id"] == target["capability_id"]
            and row["capability_revision"] == target["capability_revision"]
        ]
        if len(matches) != 1:
            raise ContractValidationError(
                "capability_reference",
                "$.profile.role_bindings[*].primary",
                "role target does not resolve to exactly one capability record",
            )
        return matches[0]


def _require_target_records(
    target: Mapping[str, Any],
    source: Mapping[str, Any],
    capability: Mapping[str, Any],
) -> None:
    if target["source_record_sha256"] != shared_canonical_sha256(source):
        raise ContractValidationError(
            "source_reference",
            "$.profile.role_bindings[*].primary.source_record_sha256",
            "role target source hash differs from the supplied source record",
        )
    if target["capability_record_sha256"] != shared_canonical_sha256(capability):
        raise ContractValidationError(
            "capability_reference",
            "$.profile.role_bindings[*].primary.capability_record_sha256",
            "role target capability hash differs from the supplied capability record",
        )
    if (
        capability["source_id"] != source["source_id"]
        or capability["source_revision"] != source["source_revision"]
        or capability["requested_model_id"] != target["requested_model_id"]
    ):
        raise ContractValidationError(
            "capability_reference",
            "$.profile.role_bindings[*].primary",
            "role target, source, and capability identity are inconsistent",
        )


def build_evaluation_semantic_contract_v1(
    profile: Mapping[str, Any],
    api_sources: Sequence[Mapping[str, Any]],
    capability_evidence: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Project a concrete Evaluation profile to its immutable semantic contract."""

    normalized_profile = validate_pipeline_profile(profile)
    sources = tuple(validate_api_source(row) for row in api_sources)
    capabilities = tuple(
        validate_capability_evidence(row) for row in capability_evidence
    )
    roles: list[dict[str, Any]] = []
    for role in normalized_profile["role_bindings"]:
        target = role["primary"]
        source_matches = [
            row
            for row in sources
            if row["source_id"] == target["source_id"]
            and row["source_revision"] == target["source_revision"]
        ]
        capability_matches = [
            row
            for row in capabilities
            if row["capability_id"] == target["capability_id"]
            and row["capability_revision"] == target["capability_revision"]
        ]
        if len(source_matches) != 1 or len(capability_matches) != 1:
            raise ContractValidationError(
                "target_reference",
                "$.profile.role_bindings[*].primary",
                "role target does not resolve to exact source and capability records",
            )
        source = source_matches[0]
        capability = capability_matches[0]
        _require_target_records(target, source, capability)
        semantic_role = copy.deepcopy(dict(role))
        semantic_role["primary"] = {
            "requested_model_id": target["requested_model_id"],
            "transport_family": {
                key: source[key]
                for key in (
                    "source_class",
                    "adapter_id",
                    "protocol",
                    "route_id",
                    "endpoint_class",
                    "base_url",
                )
            },
            "capability": {
                key: capability[key]
                for key in (
                    "requested_model_id",
                    "observed_model_id",
                    "capability_kind",
                    "schema_dialect",
                    "schema_sha256",
                    "local_validator_id",
                    "local_validator_sha256",
                    "verdict",
                )
            },
        }
        roles.append(semantic_role)
    return {
        "contract_id": "evaluation_role_semantic_contract_v1",
        "contract_version": "1.0.0",
        "workstream": normalized_profile["workstream"],
        "roles": roles,
    }


class EvaluationMethodExecutorV1:
    """Execute SF-QE, SF-BT, and PJ packets without changing scorer policy."""

    def __init__(
        self,
        *,
        common_input: CommonEvaluationInputV1,
        config_payload: Mapping[str, Any],
        sf_qe_scorer: LocalSfQeScorerV1,
        llm_roles: SharedEvaluationRoleRunnerV1,
        created_at: str,
        producer_code_commit: str,
        sf_bt_context_profile: str = "bounded_neighbors",
    ) -> None:
        self._common_input = common_input
        self._config = validate_evaluation_run_config(config_payload)
        self._plan = build_evaluation_plan(common_input, self._config)
        self._sf_qe_scorer = sf_qe_scorer
        self._llm_roles = llm_roles
        self._created_at = created_at
        self._producer_code_commit = producer_code_commit
        self._sf_bt_context_profile = sf_bt_context_profile

    @property
    def plan(self) -> EvaluationPlanV1:
        return self._plan

    @property
    def execution_binding(self) -> dict[str, Any]:
        binding: dict[str, Any] = copy.deepcopy(self._llm_roles.execution_binding)
        local_binding = getattr(self._sf_qe_scorer, "execution_binding", None)
        if local_binding is not None:
            if not isinstance(local_binding, Mapping):
                raise ContractValidationError(
                    "sf_qe_execution_binding",
                    "$.sf_qe_scorer.execution_binding",
                    "local SF-QE execution binding must be an object",
                )
            binding["local_sf_qe"] = copy.deepcopy(dict(local_binding))
        return binding

    def assert_sf_qe_exact_cover(self) -> None:
        assertion = getattr(self._sf_qe_scorer, "assert_exact_cover", None)
        if not callable(assertion):
            raise ContractValidationError(
                "sf_qe_exact_cover",
                "$.sf_qe_scorer",
                "pilot execution requires a sealed exact-cover SF-QE scorer",
            )
        assertion()

    def begin_sf_qe_execution(self) -> None:
        begin = getattr(self._sf_qe_scorer, "begin_execution", None)
        if not callable(begin):
            raise ContractValidationError(
                "sf_qe_execution_lifecycle",
                "$.sf_qe_scorer",
                "pilot execution requires a restart-safe sealed SF-QE scorer",
            )
        begin()

    def __call__(self, packet: Mapping[str, Any]) -> dict[str, Any]:
        validated = validate_scorer_input_packet_binding(
            packet, self._common_input, self._plan
        )
        method_id = validated["binding"]["method_id"]
        if method_id == "sf_qe":
            return self._execute_sf_qe(validated)
        if method_id == "sf_bt":
            return self._execute_sf_bt(validated)
        if method_id == "pj":
            return self._execute_pj(validated)
        raise ContractValidationError(
            "unsupported_method", "$.binding.method_id", "method executor is closed"
        )

    def _execute_sf_qe(self, packet: Mapping[str, Any]) -> dict[str, Any]:
        source = packet["source"]["blocks"][0]["text"]
        candidate = packet["candidates"][0]["blocks"][0]["text"]
        if not isinstance(source, str) or not isinstance(candidate, str):
            raise ContractValidationError(
                "sf_qe_text", "$", "SF-QE requires source and candidate text"
            )
        score = self._sf_qe_scorer(source, candidate)
        if isinstance(score, bool) or not isinstance(score, (int, float)):
            return _failed("sf_qe_invalid_score_type")
        score = float(score)
        if not math.isfinite(score) or score < 0 or score > 100:
            return _failed("sf_qe_invalid_score_range")
        return _succeeded({"score": score})

    def _execute_sf_bt(self, packet: Mapping[str, Any]) -> dict[str, Any]:
        packet_sha256 = packet["integrity"]["packet_sha256"]
        reverse_prompt = render_sf_bt_reverse_prompt_v3(
            packet, context_profile=self._sf_bt_context_profile
        )
        reverse_call = self._llm_roles.execute(
            role_id=SF_BT_BACK_TRANSLATOR_ROLE_ID,
            scorer_input_packet_sha256=packet_sha256,
            rendered_prompt=reverse_prompt,
            stage_id=f"sf_bt_reverse_{packet_sha256[:24]}",
            logical_request_id=f"sf_bt_reverse_{packet_sha256[:24]}",
        )
        if reverse_call.outcome["status"] != "accepted":
            return _failed(_semantic_error_code("sf_bt_reverse", reverse_call.outcome))

        attempt_id, attempt_index = _attempt_reference(reverse_call)
        raw_response = reverse_call.outcome["response_text"]
        if not isinstance(raw_response, str):
            raise ContractValidationError(
                "accepted_response", "$", "accepted reverse call lacks response text"
            )
        stage1_result = build_sf_back_translation_result(
            packet,
            attempt_id=attempt_id,
            attempt_index=attempt_index,
            created_at=self._created_at,
            producer_code_commit=self._producer_code_commit,
            context_profile=self._sf_bt_context_profile,
            rendered_prompt_sha256=reverse_prompt.rendered_prompt_sha256,
            model_profile=_legacy_model_profile(reverse_call),
            completion_status="complete",
            finish_reason="stop",
            raw_response_text=raw_response,
        )
        source_first = int(packet_sha256[0], 16) % 2 == 0
        presentation_id = "primary_source_first" if source_first else "primary_reverse_first"
        semantic_packet = build_sf_bt_semantic_judge_packet(
            self._common_input,
            self._plan,
            packet,
            stage1_result,
            stage1_raw_response_text=raw_response,
            stage1_context_profile=self._sf_bt_context_profile,
            stage1_rendered_prompt_sha256=reverse_prompt.rendered_prompt_sha256,
            presentation_id=presentation_id,
            source_first=source_first,
            created_at=self._created_at,
            producer_code_commit=self._producer_code_commit,
        )
        semantic_prompt = render_sf_bt_semantic_prompt_v3(semantic_packet)
        semantic_packet_sha256 = semantic_packet["integrity"]["packet_sha256"]
        semantic_call = self._llm_roles.execute(
            role_id=SF_BT_SEMANTIC_JUDGE_ROLE_ID,
            scorer_input_packet_sha256=semantic_packet_sha256,
            rendered_prompt=semantic_prompt,
            stage_id=f"sf_bt_semantic_{semantic_packet_sha256[:24]}",
            logical_request_id=f"sf_bt_semantic_{semantic_packet_sha256[:24]}",
            extra_bindings=(
                {
                    "name": "sf_bt_stage1_result",
                    "sha256": stage1_result["integrity"]["result_sha256"],
                },
            ),
        )
        if semantic_call.outcome["status"] != "accepted":
            return _failed(_semantic_error_code("sf_bt_semantic", semantic_call.outcome))
        return _succeeded(copy.deepcopy(semantic_call.outcome["semantic_output"]))

    def _execute_pj(self, packet: Mapping[str, Any]) -> dict[str, Any]:
        presentations = prepare_pj_prompt_presentations_v3(packet)
        if presentations.mechanical_equal:
            return _succeeded(
                {
                    "overall_verdict": "tie",
                    "style_verdict": "tie",
                    "tags": [],
                    "note": "no meaningful difference",
                }
            )
        canonical = _require_pj_prompt(presentations, "canonical")
        reversed_prompt = _require_pj_prompt(presentations, "reversed")
        packet_sha256 = packet["integrity"]["packet_sha256"]
        first = self._llm_roles.execute(
            role_id=PJ_JUDGE_ROLE_ID,
            scorer_input_packet_sha256=packet_sha256,
            rendered_prompt=canonical,
            stage_id=f"pj_canonical_{packet_sha256[:24]}",
            logical_request_id=f"pj_canonical_{packet_sha256[:24]}",
            extra_bindings=(
                {"name": "pj_presentation", "sha256": canonical.rendered_prompt_sha256},
            ),
        )
        second = self._llm_roles.execute(
            role_id=PJ_JUDGE_ROLE_ID,
            scorer_input_packet_sha256=packet_sha256,
            rendered_prompt=reversed_prompt,
            stage_id=f"pj_reversed_{packet_sha256[:24]}",
            logical_request_id=f"pj_reversed_{packet_sha256[:24]}",
            extra_bindings=(
                {
                    "name": "pj_presentation",
                    "sha256": reversed_prompt.rendered_prompt_sha256,
                },
            ),
        )
        if first.outcome["status"] != "accepted" or second.outcome["status"] != "accepted":
            return _failed("pj_evidence_missing")
        canonical_output = first.outcome["semantic_output"]
        reversed_output = _unmap_reversed_pj(second.outcome["semantic_output"])
        overall = _resolve_two_order_verdict(
            canonical_output["overall_verdict"], reversed_output["overall_verdict"]
        )
        style = _resolve_two_order_verdict(
            canonical_output["style_verdict"], reversed_output["style_verdict"]
        )
        disagreement = (
            overall != canonical_output["overall_verdict"]
            or style != canonical_output["style_verdict"]
            or canonical_output["overall_verdict"] != reversed_output["overall_verdict"]
            or canonical_output["style_verdict"] != reversed_output["style_verdict"]
        )
        note = (
            "Presentation orders disagreed; result conservatively resolved as tie."
            if disagreement
            else canonical_output["note"]
        )
        tags = _bounded_union(canonical_output["tags"], reversed_output["tags"], maximum=3)
        return _succeeded(
            {
                "overall_verdict": overall,
                "style_verdict": style,
                "tags": tags,
                "note": note,
            }
        )


def _attempt_reference(call: SharedEvaluationRoleCallV1) -> tuple[str, int]:
    outcome = call.outcome
    usage = outcome.get("usage")
    if isinstance(usage, Mapping):
        return str(usage["attempt_usage_id"]), int(usage["physical_attempt_index"])
    observation = outcome.get("cache_observation")
    if isinstance(observation, Mapping) and observation.get("lookup_status") == "hit":
        lineage = derive_llm_attempt_identity(
            seal=call.seal,
            logical_request_id=outcome["logical_request_id"],
            semantic_attempt_index=1,
            transport_retry_ordinal=0,
        )
        return str(lineage["attempt_usage_id"]), 1
    raise ContractValidationError(
        "attempt_provenance", "$", "accepted role call lacks attempt or cache evidence"
    )


def _legacy_model_profile(call: SharedEvaluationRoleCallV1) -> dict[str, str]:
    """Project exact shared-seal facts into the pre-shared SF-BT result shape."""

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


def _semantic_error_code(prefix: str, outcome: Mapping[str, Any]) -> str:
    error = outcome.get("semantic_error")
    code = error.get("code") if isinstance(error, Mapping) else None
    return f"{prefix}_{code}" if isinstance(code, str) and code else f"{prefix}_rejected"


def _require_pj_prompt(
    presentations: PJPromptPresentationsV3, field: str
) -> RenderedPromptV3:
    prompt = getattr(presentations, field)
    if prompt is None:
        raise ContractValidationError(
            "pj_presentation", "$", "non-equal PJ packet lacks both presentations"
        )
    return prompt


def _unmap_reversed_pj(output: Mapping[str, Any]) -> dict[str, Any]:
    def unmap(value: str) -> str:
        return {
            "candidate_1": "candidate_2",
            "candidate_2": "candidate_1",
            "tie": "tie",
        }[value]

    return {
        "overall_verdict": unmap(output["overall_verdict"]),
        "style_verdict": unmap(output["style_verdict"]),
        "tags": list(output["tags"]),
        "note": output["note"],
    }


def _resolve_two_order_verdict(first: str, second: str) -> str:
    return first if first == second else "tie"


def _bounded_union(first: Sequence[str], second: Sequence[str], *, maximum: int) -> list[str]:
    result: list[str] = []
    for value in (*first, *second):
        if value not in result:
            result.append(value)
        if len(result) == maximum:
            break
    return result


def _succeeded(output: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "status": "succeeded",
        "semantic_output": copy.deepcopy(dict(output)),
        "error_code": None,
    }


def _failed(error_code: str) -> dict[str, Any]:
    return {"status": "failed", "semantic_output": None, "error_code": error_code}
