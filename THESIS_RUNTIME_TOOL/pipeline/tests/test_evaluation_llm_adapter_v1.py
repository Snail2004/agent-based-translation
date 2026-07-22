from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path

import pytest

from pipeline.eval.contracts_v1 import ContractValidationError as EvaluationError
from pipeline.eval.llm_adapter_v1 import (
    build_evaluation_input_bindings_v1,
    build_evaluation_request_body_v1,
    execute_evaluation_llm_attempt_v1,
)
from pipeline.eval.llm_profiles_v1 import (
    EVALUATION_LLM_ROLE_IDS,
    PJ_JUDGE_ROLE_ID,
    SF_BT_BACK_TRANSLATOR_ROLE_ID,
    SF_BT_SEMANTIC_JUDGE_ROLE_ID,
    build_evaluation_llm_profile_v1,
    evaluation_role_contract_v1,
)
from pipeline.eval.scorer_prompts_v3 import RenderedPromptV3
from pipeline.llm_backend import (
    ApplicationResponseCache,
    ContentAddressedArtifactStore,
    ContractValidationError as SharedContractError,
    MappingCredentialProvider,
    PhysicalQuotaScheduler,
    RawTransportResponse,
    SharedLlmAttemptLedger,
    SharedLlmBackend,
    TransportCallError,
    canonical_json,
    canonical_sha256,
    resolve_llm_run_seal,
)


SHA_A = "a" * 64

_ROLE_OUTPUTS = {
    SF_BT_BACK_TRANSLATOR_ROLE_ID: {
        "back_translation": "The model converges."
    },
    SF_BT_SEMANTIC_JUDGE_ROLE_ID: {
        "score": 100,
        "flags": [],
        "note": "same meaning",
    },
    PJ_JUDGE_ROLE_ID: {
        "overall_verdict": "candidate_1",
        "style_verdict": "tie",
        "tags": ["meaning"],
        "note": "candidate one preserves the source claim",
    },
}


class _Clock:
    def __init__(self) -> None:
        self.current = datetime(2026, 7, 19, tzinfo=timezone.utc)

    def __call__(self) -> datetime:
        value = self.current
        self.current += timedelta(milliseconds=100)
        return value


class _LocalSuccessSender:
    def __init__(
        self,
        output: dict,
        *,
        finish_reason: str = "stop",
        usage: dict | None = None,
    ) -> None:
        self.output = output
        self.finish_reason = finish_reason
        self.usage = usage or {
            "prompt_tokens": 40,
            "cached_input_tokens": 0,
            "completion_tokens": 12,
            "reasoning_tokens": 0,
            "total_tokens": 52,
        }
        self.calls = 0
        self.request_bodies: list[dict] = []

    def send(self, request):
        self.calls += 1
        self.request_bodies.append(json.loads(request.body.decode("utf-8")))
        body = canonical_json(
            {
                "model": "evaluation-fixture-model",
                "finish_reason": self.finish_reason,
                "usage": self.usage,
                "output_text": canonical_json(self.output),
            }
        ).encode("utf-8")
        return RawTransportResponse(
            status_code=200,
            headers={},
            body=body,
            request_id=f"fixture-request-{self.calls}",
        )


class _TransportFailureSender:
    def __init__(self) -> None:
        self.calls = 0

    def send(self, request):
        self.calls += 1
        raise TransportCallError(
            code="http_500",
            status_code=500,
            safe_message="provider returned HTTP 500",
        )


class _NonfiniteUsageSender:
    def __init__(self) -> None:
        self.calls = 0

    def send(self, request):
        self.calls += 1
        body = json.dumps(
            {
                "model": "evaluation-fixture-model",
                "finish_reason": "stop",
                "usage": {
                    "prompt_tokens": float("nan"),
                    "cached_input_tokens": 0,
                    "completion_tokens": 1,
                    "reasoning_tokens": 0,
                    "total_tokens": 1,
                },
                "output_text": "{}",
            },
            allow_nan=True,
        ).encode("utf-8")
        return RawTransportResponse(status_code=200, headers={}, body=body)


def _source(protocol: str = "local_in_process") -> dict:
    if protocol == "local_in_process":
        return {
            "schema_version": "api_source_v1",
            "source_id": "evaluation_fixture_source_v1",
            "source_revision": "fixture_v1",
            "source_class": "local_in_process",
            "adapter_id": "evaluation_fixture_adapter_v1",
            "protocol": protocol,
            "route_id": "fixture_callback",
            "endpoint_class": "in_process",
            "base_url": None,
            "credential_ref": None,
            "credential_commitment": None,
            "physical_quota_bucket_id": "evaluation-fixture-local-v1",
            "enabled": True,
        }
    source_id = f"evaluation_{protocol}_fixture_v1"
    return {
        "schema_version": "api_source_v1",
        "source_id": source_id,
        "source_revision": "fixture_v1",
        "source_class": "remote_api",
        "adapter_id": f"{protocol}_fixture_adapter_v1",
        "protocol": protocol,
        "route_id": "fixture_generate",
        "endpoint_class": "remote",
        "base_url": "https://provider.invalid/v1",
        "credential_ref": f"credential.{source_id}",
        "credential_commitment": SHA_A,
        "physical_quota_bucket_id": f"{source_id}-bucket",
        "enabled": True,
    }


def _capability(role_id: str, source: dict, *, native: bool = True) -> dict:
    contract = evaluation_role_contract_v1(role_id)
    model_id = "evaluation-fixture-model"
    return {
        "schema_version": "capability_evidence_v1",
        "capability_id": (
            role_id.replace(".", "_") + "_fixture_capability_v1"
        ),
        "capability_revision": "fixture_v1",
        "source_id": source["source_id"],
        "source_revision": source["source_revision"],
        "adapter_id": source["adapter_id"],
        "protocol": source["protocol"],
        "route_id": source["route_id"],
        "base_url": source["base_url"],
        "requested_model_id": model_id,
        "observed_model_id": model_id,
        "capability_kind": (
            "native_structured_output" if native else "json_object"
        ),
        "schema_dialect": "json_schema_2020_12",
        "schema_sha256": contract["response_schema"]["sha256"],
        "local_validator_id": contract["validator"]["id"],
        "local_validator_sha256": contract["validator"]["sha256"],
        "verdict": "qualified",
        "probe_id": role_id.replace(".", "_") + "_fixture_probe_v1",
        "evidence_sha256": "e" * 64,
        "observed_at_utc": "2026-07-19T00:00:00Z",
    }


def _target(source: dict, capability: dict) -> dict:
    return {
        "source_id": source["source_id"],
        "source_revision": source["source_revision"],
        "source_record_sha256": canonical_sha256(source),
        "requested_model_id": capability["requested_model_id"],
        "capability_id": capability["capability_id"],
        "capability_revision": capability["capability_revision"],
        "capability_record_sha256": canonical_sha256(capability),
    }


def _rendered(role_id: str, text: str = "fixture prompt") -> RenderedPromptV3:
    prompt = evaluation_role_contract_v1(role_id)["prompt"]
    return RenderedPromptV3(
        candidate_id=prompt["id"],
        prompt_sha256=prompt["sha256"],
        rendered_prompt=text,
        rendered_prompt_sha256=canonical_text_sha256(text),
    )


def canonical_text_sha256(value: str) -> str:
    from hashlib import sha256

    return sha256(value.encode("utf-8")).hexdigest()


def _case(role_id: str) -> tuple[dict, dict, dict, RenderedPromptV3, dict]:
    source = _source()
    capability = _capability(role_id, source)
    profile = build_evaluation_llm_profile_v1(
        primary_targets={role_id: _target(source, capability)}
    )
    rendered = _rendered(role_id)
    body = build_evaluation_request_body_v1(
        profile=profile,
        role_id=role_id,
        source=source,
        capability=capability,
        rendered_prompt=rendered,
    )
    bindings = build_evaluation_input_bindings_v1(
        scorer_input_packet_sha256="b" * 64,
        rendered_prompt=rendered,
        request_body=body,
    )
    seal = resolve_llm_run_seal(
        profile=profile,
        api_sources=[source],
        capability_evidence=[capability],
        role_id=role_id,
        run_id="evaluation_fixture_run",
        attempt_run_id="evaluation_fixture_attempt",
        stage_id=role_id.replace(".", "_"),
        input_bindings=bindings,
    )
    return profile, source, capability, rendered, seal


def _backend(tmp_path: Path, sender):
    store = ContentAddressedArtifactStore(tmp_path / "objects")
    cache = ApplicationResponseCache(
        index_path=tmp_path / "response_cache.sqlite3",
        artifact_store=store,
    )
    ledger = SharedLlmAttemptLedger(tmp_path / "attempt_ledger.sqlite3")
    backend = SharedLlmBackend(
        credential_provider=MappingCredentialProvider({}),
        scheduler=PhysicalQuotaScheduler(tmp_path / "quota_locks"),
        ledger=ledger,
        response_cache=cache,
        sender=sender,
        clock=_Clock(),
    )
    return backend, ledger


def test_profile_manifest_contains_only_provider_backed_semantic_roles() -> None:
    targets = {}
    for role_id in EVALUATION_LLM_ROLE_IDS:
        source = _source()
        capability = _capability(role_id, source)
        targets[role_id] = _target(source, capability)
    profile = build_evaluation_llm_profile_v1(primary_targets=targets)
    assert {row["role_id"] for row in profile["role_bindings"]} == set(
        EVALUATION_LLM_ROLE_IDS
    )
    assert all(row["fallback_plan"] == {"enabled": False, "steps": []} for row in profile["role_bindings"])
    assert all(row["transport_retry"]["max_retries"] == 0 for row in profile["role_bindings"])
    assert all(row["semantic_retry"]["max_retries"] == 0 for row in profile["role_bindings"])
    assert "evaluation.sf_qe.scorer" not in {
        row["role_id"] for row in profile["role_bindings"]
    }


def test_official_native_profile_can_seal_required_mode_explicitly() -> None:
    targets = {}
    for role_id in EVALUATION_LLM_ROLE_IDS:
        source = _source("google_genai_generate_content")
        capability = _capability(role_id, source)
        targets[role_id] = _target(source, capability)

    profile = build_evaluation_llm_profile_v1(
        primary_targets=targets,
        profile_id="evaluation-native-required-fixture-v1",
        profile_revision="v1",
        structured_output_mode="required",
    )

    assert all(
        row["structured_output"]
        == {"mode": "required", "schema_dialect": "json_schema_2020_12"}
        for row in profile["role_bindings"]
    )
    assert all(
        row["preset_revision"] == "v2-required"
        for row in profile["role_bindings"]
    )


def test_profile_rejects_unknown_structured_output_mode() -> None:
    role_id = SF_BT_BACK_TRANSLATOR_ROLE_ID
    source = _source()
    capability = _capability(role_id, source)

    with pytest.raises(SharedContractError, match="structured-output mode"):
        build_evaluation_llm_profile_v1(
            primary_targets={role_id: _target(source, capability)},
            structured_output_mode="native_schema",
        )


def test_profile_rejects_code_only_or_unknown_role() -> None:
    with pytest.raises(SharedContractError, match="unsupported roles"):
        build_evaluation_llm_profile_v1(
            primary_targets={"evaluation.sf_qe.scorer": {}}
        )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("source_id", "human_reference_source_v1"),
        ("capability_id", "result_callback_capability_v1"),
        ("requested_model_id", "oracle_model_v1"),
    ],
)
def test_profile_rejects_reference_authority_in_target_identifiers(
    field: str, value: str
) -> None:
    role_id = SF_BT_BACK_TRANSLATOR_ROLE_ID
    source = _source()
    capability = _capability(role_id, source)
    target = _target(source, capability)
    target[field] = value
    with pytest.raises(SharedContractError, match="runtime authority token"):
        build_evaluation_llm_profile_v1(primary_targets={role_id: target})


@pytest.mark.parametrize(
    "protocol",
    [
        "local_in_process",
        "openai_chat_completions",
        "openai_responses",
        "google_genai_generate_content",
    ],
)
def test_request_body_is_protocol_specific_but_source_and_secret_free(
    protocol: str,
) -> None:
    role_id = SF_BT_SEMANTIC_JUDGE_ROLE_ID
    source = _source(protocol)
    capability = _capability(role_id, source)
    profile = build_evaluation_llm_profile_v1(
        primary_targets={role_id: _target(source, capability)}
    )
    first = build_evaluation_request_body_v1(
        profile=profile,
        role_id=role_id,
        source=source,
        capability=capability,
        rendered_prompt=_rendered(role_id),
    )
    second = build_evaluation_request_body_v1(
        profile=profile,
        role_id=role_id,
        source=source,
        capability=capability,
        rendered_prompt=_rendered(role_id),
    )
    assert first == second
    rendered = canonical_json(first)
    assert "fixture prompt" in rendered
    assert source["source_id"] not in rendered
    assert "credential" not in rendered
    assert "base_url" not in rendered


def test_prompt_validated_request_adds_only_the_json_output_envelope() -> None:
    role_id = SF_BT_SEMANTIC_JUDGE_ROLE_ID
    source = _source("google_genai_generate_content")
    capability = _capability(role_id, source, native=False)
    profile = build_evaluation_llm_profile_v1(
        primary_targets={role_id: _target(source, capability)},
        structured_output_mode="prompt_validated",
    )
    rendered_prompt = _rendered(role_id)
    body = build_evaluation_request_body_v1(
        profile=profile,
        role_id=role_id,
        source=source,
        capability=capability,
        rendered_prompt=rendered_prompt,
    )
    prompt = body["contents"][0]["parts"][0]["text"]
    assert prompt.startswith(rendered_prompt.rendered_prompt)
    assert "Do not use Markdown code fences" in prompt
    assert body["generationConfig"]["responseMimeType"] == "application/json"
    assert "responseJsonSchema" not in body["generationConfig"]
    role = profile["role_bindings"][0]
    assert role["generation"]["max_output_tokens"] == 512
    assert role["limits"]["max_completion_tokens"] == 4_096
    assert profile["role_bindings"][0]["preset_revision"] == (
        "v3-prompt-validated"
    )


def test_required_native_request_does_not_add_prompt_validated_envelope() -> None:
    role_id = SF_BT_SEMANTIC_JUDGE_ROLE_ID
    source = _source("google_genai_generate_content")
    capability = _capability(role_id, source)
    profile = build_evaluation_llm_profile_v1(
        primary_targets={role_id: _target(source, capability)},
        structured_output_mode="required",
    )
    body = build_evaluation_request_body_v1(
        profile=profile,
        role_id=role_id,
        source=source,
        capability=capability,
        rendered_prompt=_rendered(role_id),
    )
    prompt = body["contents"][0]["parts"][0]["text"]
    assert "Do not use Markdown code fences" not in prompt
    assert "responseJsonSchema" in body["generationConfig"]
    role = profile["role_bindings"][0]
    assert role["generation"]["max_output_tokens"] == 512
    assert role["limits"]["max_completion_tokens"] == 512


@pytest.mark.parametrize("role_id", sorted(EVALUATION_LLM_ROLE_IDS))
def test_adapter_accepts_all_three_semantic_role_outputs(
    role_id: str, tmp_path: Path
) -> None:
    _, _, _, rendered, seal = _case(role_id)
    sender = _LocalSuccessSender(_ROLE_OUTPUTS[role_id])
    backend, ledger = _backend(tmp_path, sender)
    outcome = execute_evaluation_llm_attempt_v1(
        backend=backend,
        seal=seal,
        logical_request_id="logical_request_one",
        rendered_prompt=rendered,
        cache_mode="bypass",
    )
    assert outcome["status"] == "accepted"
    assert outcome["semantic_output"] == _ROLE_OUTPUTS[role_id]
    assert outcome["provider_called"] is True
    assert sender.calls == 1
    assert ledger.count("usage") == 1
    assert ledger.count("error") == 0


def test_cache_hit_reuses_exact_accepted_response_without_second_call(
    tmp_path: Path,
) -> None:
    role_id = SF_BT_BACK_TRANSLATOR_ROLE_ID
    _, _, _, rendered, seal = _case(role_id)
    sender = _LocalSuccessSender(_ROLE_OUTPUTS[role_id])
    backend, ledger = _backend(tmp_path, sender)
    first = execute_evaluation_llm_attempt_v1(
        backend=backend,
        seal=seal,
        logical_request_id="cacheable_request",
        rendered_prompt=rendered,
        cache_mode="read_write",
    )
    second = execute_evaluation_llm_attempt_v1(
        backend=backend,
        seal=seal,
        logical_request_id="cacheable_request",
        rendered_prompt=rendered,
        cache_mode="read_write",
    )
    assert first["status"] == second["status"] == "accepted"
    assert second["backend_status"] == "cache_hit"
    assert second["provider_called"] is False
    assert sender.calls == 1
    assert ledger.count("usage") == 1
    assert ledger.count("cache") == 2


def test_duplicate_physical_attempt_fails_when_cache_is_bypassed(
    tmp_path: Path,
) -> None:
    role_id = PJ_JUDGE_ROLE_ID
    _, _, _, rendered, seal = _case(role_id)
    sender = _LocalSuccessSender(_ROLE_OUTPUTS[role_id])
    backend, _ = _backend(tmp_path, sender)
    execute_evaluation_llm_attempt_v1(
        backend=backend,
        seal=seal,
        logical_request_id="duplicate_request",
        rendered_prompt=rendered,
        cache_mode="bypass",
    )
    with pytest.raises(SharedContractError, match="already exists"):
        execute_evaluation_llm_attempt_v1(
            backend=backend,
            seal=seal,
            logical_request_id="duplicate_request",
            rendered_prompt=rendered,
            cache_mode="bypass",
        )
    assert sender.calls == 1


def test_transport_failure_is_recorded_once_and_not_retried(
    tmp_path: Path,
) -> None:
    role_id = SF_BT_SEMANTIC_JUDGE_ROLE_ID
    _, _, _, rendered, seal = _case(role_id)
    sender = _TransportFailureSender()
    backend, ledger = _backend(tmp_path, sender)
    with pytest.raises(TransportCallError, match="HTTP 500"):
        execute_evaluation_llm_attempt_v1(
            backend=backend,
            seal=seal,
            logical_request_id="failed_request",
            rendered_prompt=rendered,
            cache_mode="bypass",
        )
    assert sender.calls == 1
    assert ledger.count("usage") == 1
    assert ledger.count("error") == 1


def test_semantic_failure_returns_rejection_and_never_retries(
    tmp_path: Path,
) -> None:
    role_id = SF_BT_SEMANTIC_JUDGE_ROLE_ID
    _, _, _, rendered, seal = _case(role_id)
    sender = _LocalSuccessSender({"score": 63, "flags": [], "note": "invalid band"})
    backend, ledger = _backend(tmp_path, sender)
    outcome = execute_evaluation_llm_attempt_v1(
        backend=backend,
        seal=seal,
        logical_request_id="semantic_failure",
        rendered_prompt=rendered,
        cache_mode="bypass",
    )
    assert outcome["status"] == "semantic_rejected"
    assert outcome["semantic_error"]["category"] == "canonical_schema"
    assert outcome["semantic_error"]["retry_requires_new_seal"] is True
    assert sender.calls == 1
    assert ledger.count("usage") == 1


def test_incomplete_finish_reason_is_semantically_rejected(
    tmp_path: Path,
) -> None:
    role_id = PJ_JUDGE_ROLE_ID
    _, _, _, rendered, seal = _case(role_id)
    sender = _LocalSuccessSender(_ROLE_OUTPUTS[role_id], finish_reason="length")
    backend, _ = _backend(tmp_path, sender)
    outcome = execute_evaluation_llm_attempt_v1(
        backend=backend,
        seal=seal,
        logical_request_id="incomplete_response",
        rendered_prompt=rendered,
        cache_mode="bypass",
    )
    assert outcome["status"] == "semantic_rejected"
    assert outcome["semantic_error"]["category"] == "pipeline_semantic"
    assert sender.calls == 1


def test_stale_seal_and_foreign_prompt_fail_before_transport(
    tmp_path: Path,
) -> None:
    role_id = SF_BT_BACK_TRANSLATOR_ROLE_ID
    _, _, _, rendered, seal = _case(role_id)
    sender = _LocalSuccessSender(_ROLE_OUTPUTS[role_id])
    backend, _ = _backend(tmp_path, sender)
    stale = deepcopy(seal)
    stale["stage_id"] = "mutated_stage"
    with pytest.raises(SharedContractError, match="mismatch"):
        execute_evaluation_llm_attempt_v1(
            backend=backend,
            seal=stale,
            logical_request_id="stale_seal",
            rendered_prompt=rendered,
            cache_mode="bypass",
        )
    with pytest.raises(EvaluationError, match="different role"):
        execute_evaluation_llm_attempt_v1(
            backend=backend,
            seal=seal,
            logical_request_id="foreign_prompt",
            rendered_prompt=_rendered(PJ_JUDGE_ROLE_ID),
            cache_mode="bypass",
        )
    assert sender.calls == 0


def test_nonfinite_cost_is_rejected_before_transport(tmp_path: Path) -> None:
    role_id = SF_BT_BACK_TRANSLATOR_ROLE_ID
    _, _, _, rendered, seal = _case(role_id)
    sender = _LocalSuccessSender(_ROLE_OUTPUTS[role_id])
    backend, _ = _backend(tmp_path, sender)
    with pytest.raises(EvaluationError, match="finite canonical JSON"):
        execute_evaluation_llm_attempt_v1(
            backend=backend,
            seal=seal,
            logical_request_id="nonfinite_cost",
            rendered_prompt=rendered,
            cache_mode="bypass",
            cost_fact={
                "cost_usd": float("nan"),
                "cost_status": "calculated",
                "cost_provenance": {
                    "kind": "pricing_manifest",
                    "reference_id": "fixture",
                    "reference_sha256": SHA_A,
                },
            },
        )
    assert sender.calls == 0


def test_nonfinite_provider_usage_is_transport_failure(tmp_path: Path) -> None:
    role_id = SF_BT_BACK_TRANSLATOR_ROLE_ID
    _, _, _, rendered, seal = _case(role_id)
    sender = _NonfiniteUsageSender()
    backend, ledger = _backend(tmp_path, sender)
    with pytest.raises(TransportCallError, match="usage is invalid"):
        execute_evaluation_llm_attempt_v1(
            backend=backend,
            seal=seal,
            logical_request_id="nonfinite_usage",
            rendered_prompt=rendered,
            cache_mode="bypass",
        )
    assert sender.calls == 1
    assert ledger.count("usage") == 1
    assert ledger.count("error") == 1


def test_gold_reference_binding_is_rejected_by_shared_seal() -> None:
    role_id = SF_BT_BACK_TRANSLATOR_ROLE_ID
    profile, source, capability, rendered, _ = _case(role_id)
    body = build_evaluation_request_body_v1(
        profile=profile,
        role_id=role_id,
        source=source,
        capability=capability,
        rendered_prompt=rendered,
    )
    bindings = build_evaluation_input_bindings_v1(
        scorer_input_packet_sha256="b" * 64,
        rendered_prompt=rendered,
        request_body=body,
        extra_bindings=[
            {"name": "human_reference", "sha256": "c" * 64}
        ],
    )
    with pytest.raises(SharedContractError, match="human_reference"):
        resolve_llm_run_seal(
            profile=profile,
            api_sources=[source],
            capability_evidence=[capability],
            role_id=role_id,
            run_id="forbidden_reference_run",
            attempt_run_id="forbidden_reference_attempt",
            stage_id="forbidden_reference_stage",
            input_bindings=bindings,
        )
