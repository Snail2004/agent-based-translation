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
from pipeline.literary import openai_b1_capability_probe_v1 as probe_module
from pipeline.literary.openai_b1_capability_probe_v1 import (
    ACCEPTED_OBSERVED_MODEL_IDS,
    B1_CANONICAL_SCHEMA_SHA256,
    B1_OMISSION_SET_SHA256,
    B1_ROLE_ID,
    B1_SCHEMA_SHA256,
    B1_TRANSPORT_SCHEMA_SHA256,
    B1_VALIDATOR_ID,
    B1_VALIDATOR_SHA256,
    LiteraryOpenAiCapabilityProbeError,
    PROBE_PROFILE_REVISION,
    RUNTIME_PROFILE_SHA256,
    SHARED_CORE_REVISION,
    build_clean_implementation_binding_v1,
    build_literary_openai_b1_probe_plan_v1,
    empty_probe_response_v1,
    execute_literary_openai_b1_probe_once_v1,
    implementation_sha256_v1,
    load_literary_openai_b1_probe_profile_v1,
    validate_literary_openai_b1_probe_payload_v1,
)
from pipeline.literary.shared_llm_profiles_v1 import (
    build_literary_pipeline_profile,
    get_literary_shared_role_preset,
)
from pipeline.literary.structured_output_policy_v1 import (
    LiteraryStructuredOutputValidationError,
)


SECRET = "literary-openai-probe-fixture-secret"
CONSUMER_REVISION = "a" * 40
ISSUED_AT = "2026-07-20T00:00:00Z"


def _schema_keywords(value) -> set[str]:
    if isinstance(value, dict):
        return set(value).union(
            *(_schema_keywords(child) for child in value.values())
        )
    if isinstance(value, list):
        return set().union(*(_schema_keywords(child) for child in value))
    return set()


def _implementation_binding() -> dict[str, str]:
    return {
        "shared_core_revision": SHARED_CORE_REVISION,
        "consumer_revision": CONSUMER_REVISION,
        "consumer_implementation_sha256": implementation_sha256_v1(),
    }


def _plan(*, probe_run_id: str = "literary_openai_b1_probe_test_001"):
    def clean_git(_root, *args):
        if args[0] == "status":
            return ""
        if args[:2] == ("rev-parse", "HEAD"):
            return CONSUMER_REVISION
        raise AssertionError(args)

    with patch.object(probe_module, "_git_text", clean_git):
        return build_literary_openai_b1_probe_plan_v1(
            probe_run_id=probe_run_id,
            credential_commitment_sha256=credential_commitment(SECRET),
            issued_at_utc=ISSUED_AT,
        )


def _provider_body(
    *, model: str = "gpt-5.4-2026-03-05", content: str | None = None
) -> bytes:
    content = content or canonical_json(empty_probe_response_v1())
    return canonical_json(
        {
            "id": "literary-openai-probe-request-1",
            "model": model,
            "choices": [
                {"finish_reason": "stop", "message": {"content": content}}
            ],
            "usage": {
                "prompt_tokens": 900,
                "completion_tokens": 30,
                "total_tokens": 930,
                "prompt_tokens_details": {"cached_tokens": 0},
                "completion_tokens_details": {"reasoning_tokens": 0},
            },
        }
    ).encode("utf-8")


class _Clock:
    def __init__(self) -> None:
        start = datetime(2026, 7, 20, tzinfo=timezone.utc)
        self.values = [start + timedelta(seconds=index) for index in range(8)]

    def __call__(self) -> datetime:
        return self.values.pop(0)


class _Sender:
    def __init__(
        self,
        *,
        model: str = "gpt-5.4-2026-03-05",
        content: str | None = None,
        fail: bool = False,
    ) -> None:
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
            headers={"x-request-id": "literary-openai-probe-request-1"},
            body=_provider_body(model=self.model, content=self.content),
            request_id="literary-openai-probe-request-1",
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
        "probe_id": "literary_openai_b1_unqualified_declaration",
        "evidence_sha256": "c" * 64,
        "observed_at_utc": ISSUED_AT,
        "verdict": "unknown",
    }


def _normal_seal(*, source: dict, evidence: dict, plan) -> dict:
    profile = build_literary_pipeline_profile(
        preset=get_literary_shared_role_preset(B1_ROLE_ID),
        api_source=source,
        capability=evidence,
        prompt_ref={
            "id": "literary.b1.entity_inventory.capability_probe_prompt",
            "revision": "v1",
            "sha256": canonical_sha256(plan.request_body["messages"]),
        },
        response_schema_ref={
            "id": "literary.b1.entity_inventory.response_schema",
            "revision": "runtime_v1",
            "sha256": B1_SCHEMA_SHA256,
        },
        validator_ref={
            "id": B1_VALIDATOR_ID,
            "revision": "v1",
            "sha256": B1_VALIDATOR_SHA256,
        },
        semantic_extension_ref={
            "id": "literary.b1.inventory.apply_v1",
            "schema_version": "literary_semantic_authority_v1",
            "sha256": "b" * 64,
        },
        structured_output={
            "mode": "required",
            "schema_dialect": "openai_strict_json_schema_subset_v1",
        },
        profile_id="literary_shared_llm_phase3_v1",
        profile_revision="shared_runtime_recommended_v1",
    )
    return resolve_llm_run_seal(
        profile=profile,
        api_sources=[source],
        capability_evidence=[evidence],
        role_id=B1_ROLE_ID,
        run_id="literary_probe_consumer_run",
        attempt_run_id="literary_probe_consumer_attempt",
        stage_id="literary_b1",
        input_bindings=[
            {
                "name": "transport_request_body",
                "sha256": canonical_sha256(plan.request_body),
            }
        ],
    )


def test_profile_and_plan_bind_official_runtime_contract() -> None:
    profile = load_literary_openai_b1_probe_profile_v1()
    plan = _plan()
    assert profile["shared_core_revision"] == SHARED_CORE_REVISION
    assert profile["runtime_profile"]["profile_sha256"] == (
        RUNTIME_PROFILE_SHA256
    )
    assert profile["profile_revision"] == PROBE_PROFILE_REVISION
    assert profile["capability_intent"]["accepted_observed_model_ids"] == list(
        ACCEPTED_OBSERVED_MODEL_IDS
    )
    assert (
        profile["capability_intent"]["capability_revision"]
        == "b1_transport_eed09132_validator_1f85e47c_openai_row2_v2"
    )
    assert plan.runtime_profile_sha256 == RUNTIME_PROFILE_SHA256
    assert plan.source["source_id"] == "openai_official_row2_v1"
    assert plan.source["source_revision"] == (
        "openai_key2_literary_20260719_v1"
    )
    assert plan.source["base_url"] == "https://api.openai.com/v1"
    assert plan.source["physical_quota_bucket_id"] == "openai-row2"
    assert plan.source["credential_ref"] == "credential.openai_row2"
    assert canonical_sha256(plan.canonical_schema) == (
        B1_CANONICAL_SCHEMA_SHA256
    )
    assert canonical_sha256(plan.response_schema) == B1_SCHEMA_SHA256
    assert B1_SCHEMA_SHA256 == B1_TRANSPORT_SCHEMA_SHA256
    assert canonical_sha256(list(plan.omitted_transport_constraints)) == (
        B1_OMISSION_SET_SHA256
    )
    assert len(plan.omitted_transport_constraints) == 27
    assert {row["keyword"] for row in plan.omitted_transport_constraints} == {
        "minItems",
        "minLength",
        "uniqueItems",
    }
    response_format = plan.request_body["response_format"]
    assert response_format["type"] == "json_schema"
    assert response_format["json_schema"]["strict"] is True
    assert response_format["json_schema"]["schema"] == plan.response_schema
    assert response_format["json_schema"]["schema"] != plan.canonical_schema
    assert not {"minItems", "minLength", "uniqueItems"}.intersection(
        _schema_keywords(plan.response_schema)
    )
    assert {"required", "additionalProperties", "type", "properties"}.issubset(
        _schema_keywords(plan.response_schema)
    )
    serialized = canonical_json(plan.request_body)
    assert "Wuthering" not in serialized
    assert SECRET not in serialized


def test_clean_consumer_binding_requires_clean_exact_head(monkeypatch) -> None:
    def dirty_git(_root, *args):
        if args[0] == "status":
            return " M THESIS_RUNTIME_TOOL/pipeline/literary/example.py"
        raise AssertionError(args)

    monkeypatch.setattr(probe_module, "_git_text", dirty_git)
    with pytest.raises(LiteraryOpenAiCapabilityProbeError, match="clean tracked"):
        build_clean_implementation_binding_v1()

    def clean_git(_root, *args):
        if args[0] == "status":
            return ""
        if args[:2] == ("rev-parse", "HEAD"):
            return CONSUMER_REVISION
        raise AssertionError(args)

    monkeypatch.setattr(probe_module, "_git_text", clean_git)
    assert build_clean_implementation_binding_v1() == _implementation_binding()


def test_unknown_rejected_then_exact_evidence_qualifies_and_resolves(
    tmp_path,
) -> None:
    plan = _plan()
    with pytest.raises(ContractValidationError, match="not qualified"):
        _normal_seal(
            source=dict(plan.source), evidence=_unknown_evidence(plan), plan=plan
        )
    sender = _Sender()
    probe, ledger = _probe(tmp_path, sender)
    result = execute_literary_openai_b1_probe_once_v1(probe=probe, plan=plan)
    assert result["status"] == "qualified"
    assert sender.calls == 1
    assert ledger.count("capability_probe_seal") == 1
    assert ledger.count("capability_probe_receipt") == 1
    assert ledger.count("capability_evidence") == 1
    assert ledger.count("usage") == 0
    assert ledger.count("cache") == 0
    normal = _normal_seal(
        source=dict(plan.source),
        evidence=result["capability_evidence"],
        plan=plan,
    )
    assert normal["primary"]["capability"]["verdict"] == "qualified"
    assert not any(
        token in path.name.casefold()
        for path in tmp_path.rglob("*")
        for token in (
            "registry",
            "checkpoint",
            "b1_output",
            "b2_output",
            "publish",
        )
    )


@pytest.mark.parametrize(
    ("sender", "expected_code"),
    [
        (_Sender(content="not json"), "response_json_invalid"),
        (_Sender(content='{"entity_candidates":[]}'), "local_validator_rejected"),
        (_Sender(model="foreign-model"), "observed_model_mismatch"),
        (_Sender(fail=True), "http_503"),
    ],
)
def test_invalid_json_schema_model_and_http_stay_failed(
    tmp_path, sender: _Sender, expected_code: str
) -> None:
    plan = _plan(probe_run_id=f"literary_openai_probe_{expected_code}")
    probe, ledger = _probe(tmp_path, sender)
    result = execute_literary_openai_b1_probe_once_v1(probe=probe, plan=plan)
    assert result["status"] == "failed"
    assert result["capability_evidence"]["verdict"] == "failed"
    assert result["receipt"]["failure"]["code"] == expected_code
    assert sender.calls == 1
    assert ledger.count("usage") == 0
    assert ledger.count("cache") == 0
    with pytest.raises(ContractValidationError, match="not qualified"):
        _normal_seal(
            source=dict(plan.source),
            evidence=result["capability_evidence"],
            plan=plan,
        )


def test_same_probe_and_capability_revision_cannot_call_again(tmp_path) -> None:
    plan = _plan()
    sender = _Sender()
    probe, _ = _probe(tmp_path, sender)
    assert execute_literary_openai_b1_probe_once_v1(
        probe=probe, plan=plan
    )["status"] == "qualified"
    with pytest.raises(ContractValidationError, match="already reserved"):
        execute_literary_openai_b1_probe_once_v1(probe=probe, plan=plan)
    second_plan = _plan(probe_run_id="literary_openai_b1_probe_test_002")
    with pytest.raises(ContractValidationError, match="terminal probe evidence"):
        execute_literary_openai_b1_probe_once_v1(
            probe=probe, plan=second_plan
        )
    assert sender.calls == 1


def test_qualification_is_exact_to_schema_validator_and_request(tmp_path) -> None:
    plan = _plan()
    sender = _Sender()
    probe, _ = _probe(tmp_path, sender)
    evidence = execute_literary_openai_b1_probe_once_v1(
        probe=probe, plan=plan
    )["capability_evidence"]
    foreign_schema = deepcopy(evidence)
    foreign_schema["schema_sha256"] = "d" * 64
    with pytest.raises(ContractValidationError, match="schema mismatch"):
        _normal_seal(
            source=dict(plan.source), evidence=foreign_schema, plan=plan
        )
    foreign_validator = deepcopy(evidence)
    foreign_validator["local_validator_sha256"] = "e" * 64
    with pytest.raises(ContractValidationError, match="local validator binding"):
        _normal_seal(
            source=dict(plan.source), evidence=foreign_validator, plan=plan
        )
    tampered_request = deepcopy(dict(plan.request_body))
    tampered_request["response_format"]["json_schema"]["strict"] = False
    fresh_sender = _Sender()
    fresh_probe, _ = _probe(tmp_path / "tampered", fresh_sender)
    with pytest.raises(ContractValidationError):
        fresh_probe.execute_once(
            seal=plan.seal,
            request_body=tampered_request,
            local_validator=validate_literary_openai_b1_probe_payload_v1,
            local_validator_id=B1_VALIDATOR_ID,
            local_validator_sha256=B1_VALIDATOR_SHA256,
        )
    assert fresh_sender.calls == 0


def test_canonical_local_validator_retains_omitted_constraints() -> None:
    duplicate = empty_probe_response_v1()
    duplicate["glossary_candidates"] = [
        {
            "surface": "Term",
            "category_claim": "other",
            "short_description": "Stable term",
            "support_block_ids": [
                "literary_openai_probe_b001",
                "literary_openai_probe_b001",
            ],
        }
    ]
    with pytest.raises(
        LiteraryStructuredOutputValidationError, match="non-unique|duplicate"
    ):
        validate_literary_openai_b1_probe_payload_v1(duplicate)

    empty_ids = deepcopy(duplicate)
    empty_ids["glossary_candidates"][0]["support_block_ids"] = []
    with pytest.raises(
        LiteraryStructuredOutputValidationError,
        match=r"non-empty|\[\] should be non-empty",
    ):
        validate_literary_openai_b1_probe_payload_v1(empty_ids)

    empty_surface = deepcopy(duplicate)
    empty_surface["glossary_candidates"][0]["support_block_ids"] = [
        "literary_openai_probe_b001"
    ]
    empty_surface["glossary_candidates"][0]["surface"] = ""
    with pytest.raises(
        LiteraryStructuredOutputValidationError,
        match="non-empty|'' should be non-empty",
    ):
        validate_literary_openai_b1_probe_payload_v1(empty_surface)


def test_canonical_schema_and_projection_drift_fail_closed() -> None:
    canonical = probe_module.entity_inventory_response_schema()
    drifted = deepcopy(canonical)
    drifted["properties"]["glossary_candidates"]["maxItems"] = 99
    with patch.object(
        probe_module, "entity_inventory_response_schema", lambda: drifted
    ), pytest.raises(
        LiteraryOpenAiCapabilityProbeError, match="canonical schema"
    ):
        _plan(probe_run_id="literary_openai_probe_canonical_drift")

    projected, omissions = probe_module.project_transport_schema_v1(canonical)
    with patch.object(
        probe_module,
        "project_transport_schema_v1",
        lambda _schema: (projected, omissions[:-1]),
    ), pytest.raises(
        LiteraryOpenAiCapabilityProbeError, match="omission set"
    ):
        _plan(probe_run_id="literary_openai_probe_omission_drift")


def test_profile_runtime_projection_and_dialect_tamper_fail_closed(
    tmp_path,
) -> None:
    profile = load_literary_openai_b1_probe_profile_v1()
    mutations = (
        ("runtime_profile", "profile_sha256", "f" * 64, "runtime profile"),
        ("capability_intent", "requested_model_id", "gpt-5.4-mini", "intent"),
        (
            "capability_intent",
            "accepted_observed_model_ids",
            ["gpt-5.4"],
            "intent",
        ),
        (
            "capability_intent",
            "schema_dialect",
            "json_schema_2020_12",
            "intent",
        ),
        (
            "capability_intent",
            "schema_sha256",
            B1_CANONICAL_SCHEMA_SHA256,
            "intent",
        ),
        (
            "transport_projection",
            "omitted_constraint_count",
            26,
            "projection",
        ),
    )
    for section, field, value, message in mutations:
        tampered = deepcopy(profile)
        tampered[section][field] = value
        path = tmp_path / f"tampered_{section}_{field}.json"
        path.write_text(json.dumps(tampered), encoding="utf-8")
        with pytest.raises(LiteraryOpenAiCapabilityProbeError, match=message):
            load_literary_openai_b1_probe_profile_v1(path)


def test_full_canonical_schema_substitution_never_reaches_transport(
    tmp_path,
) -> None:
    plan = _plan(probe_run_id="literary_openai_full_schema_substitution")
    tampered = deepcopy(dict(plan.request_body))
    tampered["response_format"]["json_schema"]["schema"] = deepcopy(
        dict(plan.canonical_schema)
    )
    sender = _Sender()
    probe, _ = _probe(tmp_path, sender)
    with pytest.raises(
        ContractValidationError, match="schema differs|request body differs"
    ):
        probe.execute_once(
            seal=plan.seal,
            request_body=tampered,
            local_validator=validate_literary_openai_b1_probe_payload_v1,
            local_validator_id=B1_VALIDATOR_ID,
            local_validator_sha256=B1_VALIDATOR_SHA256,
        )
    assert sender.calls == 0
