from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
from unittest.mock import patch

import pytest

from pipeline.llm_backend import (
    ContentAddressedArtifactStore,
    ContractValidationError,
    MappingCredentialProvider,
    PhysicalQuotaScheduler,
    RawTransportResponse,
    SharedLlmAttemptLedger,
    SharedLlmCapabilityProbe,
    TransportCallError,
    canonical_json,
    canonical_sha256,
    credential_commitment,
    resolve_llm_run_seal,
)
from pipeline.literary import openai_b2_json_object_capability_probe_v1 as probe_module
from pipeline.literary.b2_contract_v1 import B2ContractError
from pipeline.literary.openai_b2_json_object_capability_probe_v1 import (
    PROBE_NAMES,
    RUNTIME_PROFILE_SHA256,
    SHARED_CORE_REVISION,
    LiteraryOpenAiB2CapabilityProbeError,
    build_clean_implementation_binding_v1,
    build_literary_openai_b2_probe_plan_v1,
    empty_b2_probe_response_v1,
    execute_literary_openai_b2_probe_once_v1,
    implementation_sha256_v1,
    load_literary_openai_b2_probe_profile_v1,
    validate_literary_openai_b2_probe_payload_v1,
)
from pipeline.literary.shared_llm_profiles_v1 import (
    build_literary_pipeline_profile,
)
from pipeline.literary.shared_runtime_profile_v2 import (
    load_literary_shared_runtime_profile_v2,
)


SECRET = "literary-openai-b2-json-object-offline-secret"
CONSUMER_REVISION = "a" * 40
ISSUED_AT = "2026-07-20T00:00:00Z"


def _implementation_binding() -> dict[str, str]:
    return {
        "shared_core_revision": SHARED_CORE_REVISION,
        "consumer_revision": CONSUMER_REVISION,
        "consumer_implementation_sha256": implementation_sha256_v1(),
    }


def _plan(probe_name: str, *, suffix: str = "001"):
    def clean_git(_root, *args):
        if args[0] == "status":
            return ""
        if args[:2] == ("rev-parse", "HEAD"):
            return CONSUMER_REVISION
        raise AssertionError(args)

    with patch.object(probe_module, "_git_text", clean_git):
        return build_literary_openai_b2_probe_plan_v1(
            probe_name=probe_name,
            probe_run_id=f"literary_openai_b2_{probe_name}_probe_{suffix}",
            credential_commitment_sha256=credential_commitment(SECRET),
            issued_at_utc=ISSUED_AT,
        )


def _provider_body(plan, *, model="gpt-5.4-2026-03-05", content=None):
    content = content or canonical_json(
        empty_b2_probe_response_v1(
            plan.probe_name,
            request=plan.request,
        )
    )
    return canonical_json(
        {
            "id": f"b2-{plan.probe_name}-probe-request",
            "model": model,
            "choices": [
                {"finish_reason": "stop", "message": {"content": content}}
            ],
            "usage": {
                "prompt_tokens": 1200,
                "completion_tokens": 40,
                "total_tokens": 1240,
                "prompt_tokens_details": {"cached_tokens": 0},
                "completion_tokens_details": {"reasoning_tokens": 0},
            },
        }
    ).encode("utf-8")


class _Clock:
    def __init__(self) -> None:
        start = datetime(2026, 7, 20, tzinfo=timezone.utc)
        self.values = [start + timedelta(seconds=index) for index in range(8)]

    def __call__(self):
        return self.values.pop(0)


class _Sender:
    def __init__(self, plan, *, model=None, content=None, fail=False) -> None:
        self.plan = plan
        self.model = model
        self.content = content
        self.fail = fail
        self.calls = 0

    def send(self, request):
        self.calls += 1
        assert request.url == "https://api.openai.com/v1/chat/completions"
        assert request.headers_for_transport()["authorization"] == (
            f"Bearer {SECRET}"
        )
        assert request.source_id == "openai_official_row2_v1"
        if self.fail:
            raise TransportCallError(
                code="http_503",
                status_code=503,
                safe_message="provider returned HTTP 503",
            )
        return RawTransportResponse(
            status_code=200,
            headers={"x-request-id": f"b2-{self.plan.probe_name}-probe-request"},
            body=_provider_body(
                self.plan,
                model=self.model or "gpt-5.4-2026-03-05",
                content=self.content,
            ),
            request_id=f"b2-{self.plan.probe_name}-probe-request",
        )


def _probe(tmp_path: Path, sender: _Sender):
    ledger = SharedLlmAttemptLedger(tmp_path / "probe_ledger.sqlite3")
    probe = SharedLlmCapabilityProbe(
        credential_provider=MappingCredentialProvider(
            {"credential.openai_row2": SECRET}
        ),
        scheduler=PhysicalQuotaScheduler(tmp_path / "locks"),
        ledger=ledger,
        artifact_store=ContentAddressedArtifactStore(tmp_path / "artifacts"),
        sender=sender,
        implementation_binding=_implementation_binding(),
        clock=_Clock(),
    )
    return probe, ledger


def _unknown_evidence(plan) -> dict:
    source = plan.source
    intent = plan.seal["capability_intent"]
    return {
        "schema_version": "capability_evidence_v1",
        "capability_id": intent["capability_id"],
        "capability_revision": intent["capability_revision"],
        "source_id": source["source_id"],
        "source_revision": source["source_revision"],
        "adapter_id": source["adapter_id"],
        "protocol": source["protocol"],
        "route_id": source["route_id"],
        "base_url": source["base_url"],
        "requested_model_id": intent["requested_model_id"],
        "observed_model_id": None,
        "capability_kind": intent["capability_kind"],
        "schema_dialect": intent["schema_dialect"],
        "schema_sha256": intent["schema_sha256"],
        "local_validator_id": intent["local_validator_id"],
        "local_validator_sha256": intent["local_validator_sha256"],
        "probe_id": f"unqualified_{plan.probe_name}",
        "evidence_sha256": "c" * 64,
        "observed_at_utc": ISSUED_AT,
        "verdict": "unknown",
    }


def _normal_seal(*, plan, evidence):
    runtime = load_literary_shared_runtime_profile_v2(
        probe_module.RUNTIME_PROFILE_PATH
    )
    role_id = plan.seal["role_id"]
    intent = plan.seal["capability_intent"]
    profile = build_literary_pipeline_profile(
        preset=runtime.role_presets[role_id],
        api_source=plan.source,
        capability=evidence,
        prompt_ref={
            "id": f"{role_id}.capability_probe_prompt",
            "revision": "v1",
            "sha256": canonical_sha256(plan.request_body["messages"]),
        },
        response_schema_ref={
            "id": f"{role_id}.response_schema",
            "revision": "runtime_v1",
            "sha256": canonical_sha256(plan.response_schema),
        },
        validator_ref={
            "id": intent["local_validator_id"],
            "revision": "v2" if plan.probe_name == "frame" else "v3",
            "sha256": intent["local_validator_sha256"],
        },
        semantic_extension_ref={
            "id": f"{role_id}.apply_v1",
            "schema_version": "literary_semantic_authority_v1",
            "sha256": "b" * 64,
        },
        structured_output=runtime.shared_structured_output_for(role_id),
        profile_id=runtime.profile_id,
        profile_revision=runtime.profile_revision,
    )
    return resolve_llm_run_seal(
        profile=profile,
        api_sources=[plan.source],
        capability_evidence=[evidence],
        role_id=role_id,
        run_id=f"literary_b2_{plan.probe_name}_consumer_run",
        attempt_run_id=f"literary_b2_{plan.probe_name}_consumer_attempt",
        stage_id=f"literary_b2_{plan.probe_name}",
        input_bindings=[
            {
                "name": "transport_request_body",
                "sha256": canonical_sha256(plan.request_body),
            }
        ],
    )


def test_profile_and_plans_bind_json_object_without_native_schema() -> None:
    profile = load_literary_openai_b2_probe_profile_v1()
    assert profile["runtime_profile"]["profile_sha256"] == RUNTIME_PROFILE_SHA256
    assert [row["probe_name"] for row in profile["capability_intents"]] == list(
        PROBE_NAMES
    )
    for probe_name in PROBE_NAMES:
        plan = _plan(probe_name)
        assert plan.runtime_profile_sha256 == RUNTIME_PROFILE_SHA256
        assert plan.source["base_url"] == "https://api.openai.com/v1"
        assert plan.source["physical_quota_bucket_id"] == "openai-row2"
        assert plan.request_body["response_format"] == {"type": "json_object"}
        assert "json_schema" not in plan.request_body["response_format"]
        assert canonical_sha256(plan.response_schema) == (
            plan.seal["capability_intent"]["schema_sha256"]
        )
        serialized = canonical_json(plan.request_body)
        assert "literary.json_only_output_instruction/v2" in serialized
        assert plan.request_body["max_completion_tokens"] == 1024
        assert plan.request_body["seed"] == 20260720
        assert canonical_json(plan.response_schema) in plan.request_body[
            "messages"
        ][-2]["content"]
        assert "Wuthering" not in serialized
        assert SECRET not in serialized


def test_clean_consumer_binding_requires_clean_exact_head(monkeypatch) -> None:
    def dirty_git(_root, *args):
        if args[0] == "status":
            return " M THESIS_RUNTIME_TOOL/pipeline/literary/example.py"
        raise AssertionError(args)

    monkeypatch.setattr(probe_module, "_git_text", dirty_git)
    with pytest.raises(
        LiteraryOpenAiB2CapabilityProbeError, match="clean tracked"
    ):
        build_clean_implementation_binding_v1()


@pytest.mark.parametrize("probe_name", PROBE_NAMES)
def test_exact_payload_qualifies_and_normal_resolver_accepts(
    tmp_path: Path, probe_name: str
) -> None:
    plan = _plan(probe_name)
    with pytest.raises(ContractValidationError, match="not qualified"):
        _normal_seal(plan=plan, evidence=_unknown_evidence(plan))
    sender = _Sender(plan)
    probe, ledger = _probe(tmp_path / probe_name, sender)
    result = execute_literary_openai_b2_probe_once_v1(probe=probe, plan=plan)
    assert result["status"] == "qualified"
    assert sender.calls == 1
    assert ledger.count("capability_probe_seal") == 1
    assert ledger.count("capability_probe_receipt") == 1
    assert ledger.count("capability_evidence") == 1
    assert ledger.count("usage") == 0
    assert ledger.count("cache") == 0
    normal = _normal_seal(plan=plan, evidence=result["capability_evidence"])
    assert normal["primary"]["capability"]["verdict"] == "qualified"
    assert not any(
        token in path.name.casefold()
        for path in (tmp_path / probe_name).rglob("*")
        for token in ("registry", "checkpoint", "b2_output", "publish")
    )


def test_cross_role_evidence_cannot_resolve() -> None:
    frame = _plan("frame")
    interaction = _plan("interaction")
    evidence = _unknown_evidence(frame)
    evidence["verdict"] = "qualified"
    evidence["observed_model_id"] = "gpt-5.4-2026-03-05"
    with pytest.raises(ContractValidationError):
        _normal_seal(plan=interaction, evidence=evidence)


@pytest.mark.parametrize(
    ("content", "model", "fail", "expected_code"),
    [
        ("not json", None, False, "response_json_invalid"),
        ('{"speaker_turns":[]}', None, False, "local_validator_rejected"),
        (None, "foreign-model", False, "observed_model_mismatch"),
        (None, None, True, "http_503"),
    ],
)
def test_invalid_json_semantics_model_and_http_stay_failed(
    tmp_path: Path, content, model, fail, expected_code
) -> None:
    plan = _plan("interaction", suffix=expected_code)
    sender = _Sender(plan, content=content, model=model, fail=fail)
    probe, ledger = _probe(tmp_path / expected_code, sender)
    result = execute_literary_openai_b2_probe_once_v1(probe=probe, plan=plan)
    assert result["status"] == "failed"
    assert result["receipt"]["failure"]["code"] == expected_code
    assert result["capability_evidence"]["verdict"] == "failed"
    assert sender.calls == 1
    assert ledger.count("usage") == 0
    assert ledger.count("cache") == 0


def test_same_probe_and_capability_cannot_call_twice(tmp_path: Path) -> None:
    plan = _plan("frame")
    sender = _Sender(plan)
    probe, _ledger = _probe(tmp_path, sender)
    assert execute_literary_openai_b2_probe_once_v1(
        probe=probe, plan=plan
    )["status"] == "qualified"
    with pytest.raises(ContractValidationError, match="already reserved"):
        execute_literary_openai_b2_probe_once_v1(probe=probe, plan=plan)
    assert sender.calls == 1


def test_request_and_profile_tamper_fail_before_transport(tmp_path: Path) -> None:
    plan = _plan("frame")
    tampered_request = deepcopy(dict(plan.request_body))
    tampered_request["response_format"] = {
        "type": "json_schema",
        "json_schema": {"schema": plan.response_schema},
    }
    sender = _Sender(plan)
    probe, _ledger = _probe(tmp_path, sender)
    with pytest.raises(ContractValidationError):
        probe.execute_once(
            seal=plan.seal,
            request_body=tampered_request,
            local_validator=lambda payload: payload,
            local_validator_id=plan.seal["capability_intent"][
                "local_validator_id"
            ],
            local_validator_sha256=plan.seal["capability_intent"][
                "local_validator_sha256"
            ],
        )
    assert sender.calls == 0

    profile = load_literary_openai_b2_probe_profile_v1()
    profile["capability_intents"][0]["schema_sha256"] = "f" * 64
    path = tmp_path / "tampered_profile.json"
    path.write_text(json.dumps(profile), encoding="utf-8")
    with pytest.raises(
        LiteraryOpenAiB2CapabilityProbeError, match="closed contract"
    ):
        load_literary_openai_b2_probe_profile_v1(path)


@pytest.mark.parametrize(
    ("probe_name", "field"),
    [
        ("frame", "chapter_id"),
        ("interaction", "chapter_id"),
        ("interaction", "window_id"),
    ],
)
def test_local_validator_rejects_foreign_runtime_identity(
    probe_name: str, field: str
) -> None:
    plan = _plan(probe_name)
    payload = empty_b2_probe_response_v1(probe_name, request=plan.request)
    payload[field] = f"foreign_{field}"
    with pytest.raises(B2ContractError):
        validate_literary_openai_b2_probe_payload_v1(
            probe_name=probe_name,
            request=plan.request,
            payload=payload,
        )
