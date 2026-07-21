from __future__ import annotations

import copy
from datetime import datetime, timedelta, timezone
import inspect
import json
from pathlib import Path
from typing import Any

import pytest

import pipeline.ingest.draft_structure_gateway as gateway_module
from pipeline.ingest.canonical_source_package import canonical_json_sha256
from pipeline.ingest.draft_structure import (
    DRAFT_PROJECT_STATE_VERSION,
    DraftStructureError,
    build_draft_structure_report,
)
from pipeline.ingest.draft_structure_gateway import (
    BoundaryRepairRunIdentity,
    DISABLED_UNBOUND_ROLE_IDS,
    DraftStructureGatewayError,
    SharedBackendStructureExecutor,
    build_boundary_repair_profile,
    build_boundary_repair_run_seal,
    load_boundary_repair_preset,
    run_shared_backend_boundary_repair,
)
from pipeline.ingest.draft_structure_llm import (
    BOUNDARY_REPAIR_SCHEMA_DIALECT_V1,
    BOUNDARY_REPAIR_SCHEMA_DIALECT_V2,
    RESPONSE_VERSION,
    boundary_repair_contract_identities,
    build_structure_context_packs,
    render_structure_prompt,
    run_structure_assistant,
)
from pipeline.ingest.unified_source_normalizer import (
    normalize_source,
    write_unified_normalization,
)
from pipeline.llm_backend import (
    ApplicationResponseCache,
    ContentAddressedArtifactStore,
    ContractValidationError,
    MappingCredentialProvider,
    PhysicalQuotaScheduler,
    RawTransportResponse,
    SharedLlmAttemptLedger,
    SharedLlmBackend,
    TransportCallError,
    UncertifiedAttemptError,
    canonical_json,
    canonical_sha256,
    credential_commitment,
)


FIXTURE_ROOT = Path(__file__).parent / "fixtures" / "canonical_source_rich_v1"
SECRET = "test-only-shared-backend-secret"
IMPLEMENTATION_COMMIT = "a" * 40


def _load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _package(tmp_path: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    result = normalize_source(FIXTURE_ROOT / "source.html", doc_id="gateway_canary")
    package_root = tmp_path / "package"
    write_unified_normalization(result, package_root)
    document = _load_json(package_root / "document.json")
    report = build_draft_structure_report(
        document,
        _load_json(package_root / "structure_manifest.json"),
        _load_json(package_root / "asset_manifest.json"),
        _load_json(package_root / "admitted_projection_v1.json"),
        {
            "schema_version": DRAFT_PROJECT_STATE_VERSION,
            "doc_id": document["doc_id"],
            "lifecycle": "draft",
            "pipeline_run_count": 0,
        },
        package_root=package_root,
    )
    flagged = copy.deepcopy(report)
    target = flagged["units"][1]
    target["issue_codes"] = ["unit_low_confidence"]
    issue_payload = {
        "code": "unit_low_confidence",
        "scope": "unit",
        "target_id": target["unit_id"],
        "evidence": [f"chapter_id:{target['chapter_id']}"],
    }
    flagged["issues"] = [
        {
            "issue_id": f"amb_{canonical_json_sha256(issue_payload)[:20]}",
            **issue_payload,
        }
    ]
    flagged["integrity"] = {
        "unit_count": len(flagged["units"]),
        "issue_count": len(flagged["issues"]),
        "payload_sha256": canonical_json_sha256(
            {key: value for key, value in flagged.items() if key != "integrity"}
        ),
    }
    return document, flagged


def _source() -> dict[str, Any]:
    return {
        "schema_version": "api_source_v1",
        "source_id": "inputnorm_third_party_test_v1",
        "source_revision": "source_revision_v1",
        "source_class": "remote_api",
        "adapter_id": "openai_chat_completions_v1",
        "protocol": "openai_chat_completions",
        "route_id": "chat_completions",
        "endpoint_class": "remote",
        "base_url": "https://provider.invalid/v1",
        "credential_ref": "credential.inputnorm_gateway_v1",
        "credential_commitment": credential_commitment(SECRET),
        "physical_quota_bucket_id": "inputnorm-test-bucket-v1",
        "enabled": True,
    }

def _capability(
    *,
    source: dict[str, Any] | None = None,
    model: str = "gpt-5.4-mini",
    observed_model: str | None = None,
    schema_dialect: str = BOUNDARY_REPAIR_SCHEMA_DIALECT_V2,
) -> dict[str, Any]:
    active_source = source or _source()
    identities = boundary_repair_contract_identities(schema_dialect)
    return {
        "schema_version": "capability_evidence_v1",
        "capability_id": "inputnorm_gateway_json_object_v1",
        "capability_revision": "probe_20260719_v1",
        "source_id": active_source["source_id"],
        "source_revision": active_source["source_revision"],
        "adapter_id": active_source["adapter_id"],
        "protocol": active_source["protocol"],
        "route_id": active_source["route_id"],
        "base_url": active_source["base_url"],
        "requested_model_id": model,
        "observed_model_id": observed_model or model,
        "capability_kind": "json_object",
        "schema_dialect": schema_dialect,
        "schema_sha256": identities["response_schema"]["sha256"],
        "local_validator_id": identities["validator"]["id"],
        "local_validator_sha256": identities["validator"]["sha256"],
        "probe_id": "inputnorm_json_object_probe_v1",
        "evidence_sha256": "b" * 64,
        "observed_at_utc": "2026-07-19T00:00:00Z",
        "verdict": "qualified",
    }


def _run_identity(suffix: str = "one") -> BoundaryRepairRunIdentity:
    return BoundaryRepairRunIdentity(
        run_id=f"inputnorm_run_{suffix}",
        attempt_run_id=f"inputnorm_attempt_{suffix}",
        stage_id="boundary_repair_stage_v1",
        logical_request_id="boundary_repair_request_v1",
        implementation_commit=IMPLEMENTATION_COMMIT,
    )


def _response(
    report: dict[str, Any],
    pack: dict[str, Any],
    *,
    one_update: bool = False,
) -> dict[str, Any]:
    focus = list(pack["focus_unit_ids"])
    actions: list[dict[str, Any]] = []
    abstain = focus
    if one_update:
        actions.append(
            {
                "action_type": "update_unit",
                "unit_id": focus[0],
                "new_title": None,
                "classification": "review",
            }
        )
        abstain = focus[1:]
    return {
        "schema_version": RESPONSE_VERSION,
        "report_sha256": report["integrity"]["payload_sha256"],
        "context_pack_sha256": pack["integrity"]["payload_sha256"],
        "actions": actions,
        "abstentions": [
            {"unit_id": unit_id, "reason": "no_change"} for unit_id in abstain
        ],
    }


def _active_pack(
    report: dict[str, Any], document: dict[str, Any]
) -> dict[str, Any]:
    return build_structure_context_packs(
        report,
        document,
        response_contract_dialect=BOUNDARY_REPAIR_SCHEMA_DIALECT_V2,
    )[0]


def _provider_response(
    content: str,
    *,
    model: str = "gpt-5.4-mini",
    prompt_tokens: int = 100,
    completion_tokens: int = 50,
) -> bytes:
    return canonical_json(
        {
            "id": "inputnorm-provider-request-1",
            "model": model,
            "choices": [
                {
                    "finish_reason": "stop",
                    "message": {"content": content},
                }
            ],
            "usage": {
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
                "total_tokens": prompt_tokens + completion_tokens,
                "prompt_tokens_details": {"cached_tokens": 0},
                "completion_tokens_details": {"reasoning_tokens": 0},
            },
        }
    ).encode("utf-8")


class _Clock:
    def __init__(self) -> None:
        self.current = datetime(2026, 7, 19, tzinfo=timezone.utc)

    def __call__(self) -> datetime:
        value = self.current
        self.current += timedelta(seconds=1)
        return value


class _SuccessSender:
    def __init__(self, response_bytes: bytes) -> None:
        self.response_bytes = response_bytes
        self.calls: list[Any] = []

    def send(self, request: Any) -> RawTransportResponse:
        self.calls.append(request)
        return RawTransportResponse(
            status_code=200,
            headers={"x-request-id": "inputnorm-provider-request-1"},
            body=self.response_bytes,
            request_id="inputnorm-provider-request-1",
        )


class _FailureSender:
    def __init__(self, status: int) -> None:
        self.status = status
        self.calls = 0

    def send(self, _request: Any) -> RawTransportResponse:
        self.calls += 1
        raise TransportCallError(
            code=f"http_{self.status}",
            status_code=self.status,
            safe_message=f"provider returned HTTP {self.status}",
        )


class _SemanticExecutor:
    def __init__(self, response: dict[str, Any]) -> None:
        self.response = copy.deepcopy(response)

    def complete(
        self,
        _prompt: str,
        *,
        context_pack: dict[str, Any],
    ) -> dict[str, Any]:
        assert context_pack["integrity"]["payload_sha256"] == self.response[
            "context_pack_sha256"
        ]
        return copy.deepcopy(self.response)


def _backend(tmp_path: Path, sender: Any) -> tuple[
    SharedLlmBackend,
    SharedLlmAttemptLedger,
    ApplicationResponseCache,
]:
    artifact_store = ContentAddressedArtifactStore(tmp_path / "artifacts")
    cache = ApplicationResponseCache(
        index_path=tmp_path / "response_cache.sqlite3",
        artifact_store=artifact_store,
    )
    ledger = SharedLlmAttemptLedger(tmp_path / "attempt_ledger.sqlite3")
    backend = SharedLlmBackend(
        credential_provider=MappingCredentialProvider(
            {"credential.inputnorm_gateway_v1": SECRET}
        ),
        scheduler=PhysicalQuotaScheduler(tmp_path / "quota_locks"),
        ledger=ledger,
        response_cache=cache,
        sender=sender,
        clock=_Clock(),
    )
    return backend, ledger, cache


def test_contract_identities_and_recommended_preset_are_stable() -> None:
    legacy_identities = boundary_repair_contract_identities(
        BOUNDARY_REPAIR_SCHEMA_DIALECT_V1
    )
    active_identities = boundary_repair_contract_identities(
        BOUNDARY_REPAIR_SCHEMA_DIALECT_V2
    )
    assert legacy_identities == boundary_repair_contract_identities(
        BOUNDARY_REPAIR_SCHEMA_DIALECT_V1
    )
    assert active_identities == boundary_repair_contract_identities(
        BOUNDARY_REPAIR_SCHEMA_DIALECT_V2
    )
    assert {
        active_identities["prompt"]["revision"],
        active_identities["response_schema"]["revision"],
        active_identities["validator"]["revision"],
        active_identities["semantic_extension"]["schema_version"],
    } == {"v2"}
    assert active_identities["prompt"]["sha256"] != (
        legacy_identities["prompt"]["sha256"]
    )
    assert active_identities["response_schema"]["sha256"] != (
        legacy_identities["response_schema"]["sha256"]
    )

    preset = load_boundary_repair_preset()
    assert preset["requested_model_id"] == "gpt-5.4-mini"
    assert preset["preset_id"].endswith(".recommended_v3")
    assert preset["preset_revision"] == "recommended_v3"
    assert preset["structured_output"] == {
        "mode": "prompt_validated",
        "schema_dialect": BOUNDARY_REPAIR_SCHEMA_DIALECT_V2,
    }
    assert preset["limits"] == {
        "max_calls": 1,
        "max_prompt_tokens": 12000,
        "max_completion_tokens": 8000,
        "max_total_tokens": 20000,
        "max_cost_usd": None,
        "request_timeout_ms": 180000,
    }
    assert tuple(preset["disabled_unbound_role_ids"]) == DISABLED_UNBOUND_ROLE_IDS

    profile_root = Path(gateway_module.__file__).with_name("profiles")
    legacy = load_boundary_repair_preset(
        profile_root / "draft_structure_boundary_repair_recommended_v1.json"
    )
    previous = load_boundary_repair_preset(
        profile_root / "draft_structure_boundary_repair_recommended_v2.json"
    )
    gpt55 = load_boundary_repair_preset(
        profile_root / "draft_structure_boundary_repair_recommended_v4.json"
    )
    assert legacy["preset_revision"] == "recommended_v1"
    assert legacy["preflight"]["max_prompt_utf8_bytes"] == 12000
    assert legacy["structured_output"]["schema_dialect"] == (
        BOUNDARY_REPAIR_SCHEMA_DIALECT_V1
    )
    assert previous["preset_revision"] == "recommended_v2"
    assert previous["preflight"]["max_prompt_utf8_bytes"] == 24000
    assert previous["structured_output"]["schema_dialect"] == (
        BOUNDARY_REPAIR_SCHEMA_DIALECT_V1
    )
    assert gpt55["preset_revision"] == "recommended_v4"
    assert gpt55["requested_model_id"] == "gpt-5.5"
    assert gpt55["structured_output"]["schema_dialect"] == (
        BOUNDARY_REPAIR_SCHEMA_DIALECT_V2
    )
    for field in (
        "profile_id",
        "generation",
        "transport_retry",
        "semantic_retry",
        "limits",
        "structured_output",
        "namespaces",
        "preflight",
        "disabled_unbound_role_ids",
    ):
        assert gpt55[field] == preset[field]

def test_prompt_byte_cap_is_independent_from_token_cap(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    document, report = _package(tmp_path)
    pack = _active_pack(report, document)
    preset = load_boundary_repair_preset()
    assert preset["limits"]["max_prompt_tokens"] == 12000
    assert preset["preflight"]["max_prompt_utf8_bytes"] == 24000

    monkeypatch.setattr(
        gateway_module,
        "render_structure_prompt",
        lambda _pack, **_kwargs: "x" * 16890,
    )
    invocation = build_boundary_repair_run_seal(
        report,
        pack,
        api_source=_source(),
        capability_evidence=_capability(),
        run_identity=_run_identity("byte_cap_accept"),
        preset=preset,
    )
    assert invocation["prompt_utf8_bytes"] == 16890

    monkeypatch.setattr(
        gateway_module,
        "render_structure_prompt",
        lambda _pack, **_kwargs: "x" * 24001,
    )
    with pytest.raises(
        DraftStructureGatewayError,
        match="prompt UTF-8 byte preflight exceeds",
    ):
        build_boundary_repair_run_seal(
            report,
            pack,
            api_source=_source(),
            capability_evidence=_capability(),
            run_identity=_run_identity("byte_cap_reject"),
            preset=preset,
        )


def test_profile_is_source_capability_bound_and_has_one_active_role() -> None:
    source = _source()
    capability = _capability(source=source)
    profile = build_boundary_repair_profile(
        api_source=source,
        capability_evidence=capability,
    )
    assert [row["role_id"] for row in profile["role_bindings"]] == [
        "input_normalization.structure_draft.boundary_repair"
    ]
    role = profile["role_bindings"][0]
    assert role["primary"]["source_record_sha256"] == canonical_sha256(source)
    assert role["primary"]["capability_record_sha256"] == canonical_sha256(
        capability
    )
    assert role["fallback_plan"] == {"enabled": False, "steps": []}
    assert role["transport_retry"]["max_retries"] == 0
    assert role["semantic_retry"]["max_retries"] == 0
    assert role["structured_output"]["mode"] == "prompt_validated"


@pytest.mark.parametrize(
    "tamper",
    [
        lambda preset: preset["transport_retry"].update(
            {
                "max_retries": 1,
                "backoff_policy": "fixed",
                "initial_delay_ms": 100,
                "max_delay_ms": 100,
                "retryable_codes": ["timeout"],
            }
        ),
        lambda preset: preset["limits"].update({"max_calls": 2}),
        lambda preset: preset["disabled_unbound_role_ids"].pop(),
    ],
)
def test_runtime_preset_tamper_fails_closed(tamper: Any) -> None:
    preset = load_boundary_repair_preset()
    tamper(preset)
    with pytest.raises(DraftStructureGatewayError):
        build_boundary_repair_profile(
            api_source=_source(),
            capability_evidence=_capability(),
            preset=preset,
        )


def test_capability_drift_fails_before_seal() -> None:
    capability = _capability(model="gpt-5.4")
    with pytest.raises(DraftStructureGatewayError, match="requested_model_id"):
        build_boundary_repair_profile(
            api_source=_source(),
            capability_evidence=capability,
        )
    capability = _capability()
    capability["local_validator_sha256"] = "0" * 64
    with pytest.raises(DraftStructureGatewayError, match="local_validator_sha256"):
        build_boundary_repair_profile(
            api_source=_source(),
            capability_evidence=capability,
        )


def test_shared_adapter_writes_proposal_only_artifacts_and_preserves_parity(
    tmp_path: Path,
) -> None:
    document, report = _package(tmp_path)
    before_document = copy.deepcopy(document)
    before_report = copy.deepcopy(report)
    pack = _active_pack(report, document)
    response = _response(report, pack, one_update=True)
    expected = run_structure_assistant(
        _SemanticExecutor(response),
        report,
        document,
        model_identifier="gpt-5.4-mini",
        response_contract_dialect=BOUNDARY_REPAIR_SCHEMA_DIALECT_V2,
    )
    sender = _SuccessSender(_provider_response(json.dumps(response)))
    backend, ledger, _cache = _backend(tmp_path / "backend", sender)

    result = run_shared_backend_boundary_repair(
        report,
        document,
        tmp_path / "semantic_outputs",
        backend=backend,
        api_source=_source(),
        capability_evidence=_capability(),
        run_identity=_run_identity(),
    )

    assert len(sender.calls) == 1
    wire = json.loads(sender.calls[0].body.decode("utf-8"))
    assert wire == {
        "max_completion_tokens": 8000,
        "messages": [
            {
                "role": "user",
                "content": render_structure_prompt(
                    pack,
                    response_contract_dialect=BOUNDARY_REPAIR_SCHEMA_DIALECT_V2,
                ),
            }
        ],
        "model": "gpt-5.4-mini",
        "response_format": {"type": "json_object"},
    }
    assert result["manifest"]["provider_called"] is True
    assert result["manifest"]["canonical_effect"] == "none"
    assert result["manifest"]["human_review_required"] is True
    assert result["result"] == expected
    action = result["result"]["correction_plan"]["actions"][0]
    assert action["status"] == "review_required"
    assert action["reason"] == "non_human_requires_review"
    assert result["result"]["correction_plan"]["proposer"] == {
        "kind": "llm",
        "identifier": "gpt-5.4-mini",
    }
    assert document == before_document
    assert report == before_report
    output_root = Path(result["output_root"])
    assert {path.name for path in output_root.iterdir()} == {
        "correction_plan.json",
        "request.json",
        "resolved_run_seal.json",
        "response_raw.json",
        "result_manifest.json",
    }
    persisted = "\n".join(
        path.read_text(encoding="utf-8")
        for path in output_root.iterdir()
        if path.is_file()
    )
    assert SECRET not in persisted
    usage = ledger.list_records("usage")
    assert len(usage) == 1
    assert usage[0]["prompt_tokens"] == 100
    assert usage[0]["completion_tokens"] == 50
    assert usage[0]["finish_reason"] == "stop"
    assert usage[0]["cost_usd"] is None
    assert usage[0]["cost_status"] == "unknown"
    assert usage[0]["cost_provenance"] == {
        "kind": "unavailable",
        "reference_id": None,
        "reference_sha256": None,
    }




def test_application_response_cache_is_disabled_by_adapter(
    tmp_path: Path,
) -> None:
    document, report = _package(tmp_path)
    pack = _active_pack(report, document)
    sender = _SuccessSender(
        _provider_response(json.dumps(_response(report, pack)))
    )
    backend, ledger, cache = _backend(tmp_path / "backend", sender)

    result = run_shared_backend_boundary_repair(
        report,
        document,
        tmp_path / "semantic_outputs",
        backend=backend,
        api_source=_source(),
        capability_evidence=_capability(),
        run_identity=_run_identity(),
    )

    assert len(sender.calls) == 1
    assert result["backend_result"]["status"] == "provider_succeeded"
    assert result["backend_result"]["cache_observation"] is None
    assert result["manifest"]["application_response_cache"] == {
        "read": False,
        "write": False,
    }
    assert ledger.list_records("cache") == []
    assert list(cache.artifact_store.root.iterdir()) != []
    assert cache.lookup(
        consumer_seal=result["seal"],
        logical_request_id=_run_identity().logical_request_id,
    ) is None




@pytest.mark.parametrize("status", [401, 402, 429])
def test_auth_and_quota_failures_are_one_attempt_without_fallback(
    tmp_path: Path,
    status: int,
) -> None:
    document, report = _package(tmp_path)
    sender = _FailureSender(status)
    backend, ledger, _cache = _backend(tmp_path / "backend", sender)
    output_parent = tmp_path / "semantic_outputs"
    with pytest.raises(TransportCallError, match=str(status)):
        run_shared_backend_boundary_repair(
            report,
            document,
            output_parent,
            backend=backend,
            api_source=_source(),
            capability_evidence=_capability(),
            run_identity=_run_identity(),
        )
    assert sender.calls == 1
    usage = ledger.list_records("usage")
    errors = ledger.list_records("error")
    assert len(usage) == len(errors) == 1
    assert usage[0]["outcome"] == "failed_after_request"
    assert errors[0]["retry_disposition"] == "do_not_retry"
    failure_files = list(output_parent.rglob("failure.json"))
    assert len(failure_files) == 1
    assert _load_json(failure_files[0])["mandatory_stop"] is True


def test_semantic_response_drift_fails_after_usage_is_persisted(
    tmp_path: Path,
) -> None:
    document, report = _package(tmp_path)
    sender = _SuccessSender(_provider_response("not-json"))
    backend, ledger, _cache = _backend(tmp_path / "backend", sender)
    output_parent = tmp_path / "semantic_outputs"
    with pytest.raises(
        (DraftStructureGatewayError, DraftStructureError),
        match="not one strict JSON value",
    ):
        run_shared_backend_boundary_repair(
            report,
            document,
            output_parent,
            backend=backend,
            api_source=_source(),
            capability_evidence=_capability(),
            run_identity=_run_identity(),
        )
    assert len(sender.calls) == 1
    assert len(ledger.list_records("usage")) == 1
    assert len(list(output_parent.rglob("failure.json"))) == 1


def test_provider_model_drift_is_rejected_by_shared_contract(
    tmp_path: Path,
) -> None:
    document, report = _package(tmp_path)
    sender = _SuccessSender(
        _provider_response("{}", model="gpt-5.4-mini-2026-03-17")
    )
    backend, ledger, _cache = _backend(tmp_path / "backend", sender)
    output_parent = tmp_path / "semantic_outputs"
    with pytest.raises(
        ContractValidationError,
        match="usage observed model differs from capability evidence",
    ):
        run_shared_backend_boundary_repair(
            report,
            document,
            output_parent,
            backend=backend,
            api_source=_source(),
            capability_evidence=_capability(),
            run_identity=_run_identity(),
        )
    assert len(sender.calls) == 1
    assert ledger.list_records("usage") == []
    failure_files = list(output_parent.rglob("failure.json"))
    assert len(failure_files) == 1
    assert _load_json(failure_files[0])["mandatory_stop"] is True


def test_requested_alias_accepts_explicit_pinned_observed_snapshot(
    tmp_path: Path,
) -> None:
    document, report = _package(tmp_path)
    pack = _active_pack(report, document)
    observed_model = "gpt-5.4-mini-2026-03-17"
    sender = _SuccessSender(
        _provider_response(
            json.dumps(_response(report, pack)),
            model=observed_model,
        )
    )
    backend, ledger, _cache = _backend(tmp_path / "backend", sender)

    result = run_shared_backend_boundary_repair(
        report,
        document,
        tmp_path / "semantic_outputs",
        backend=backend,
        api_source=_source(),
        capability_evidence=_capability(observed_model=observed_model),
        run_identity=_run_identity("observed_snapshot"),
    )

    assert len(sender.calls) == 1
    assert result["backend_result"]["status"] == "provider_succeeded"
    usage = ledger.list_records("usage")
    assert len(usage) == 1
    assert usage[0]["requested_model_id"] == "gpt-5.4-mini"
    assert usage[0]["observed_model_id"] == observed_model


def test_completion_usage_above_pipeline_cap_fails_without_cache_publish(
    tmp_path: Path,
) -> None:
    document, report = _package(tmp_path)
    pack = _active_pack(report, document)
    sender = _SuccessSender(
        _provider_response(
            json.dumps(_response(report, pack)),
            completion_tokens=8001,
        )
    )
    backend, ledger, cache = _backend(tmp_path / "backend", sender)
    output_parent = tmp_path / "semantic_outputs"
    with pytest.raises(UncertifiedAttemptError, match="cannot be certified"):
        run_shared_backend_boundary_repair(
            report,
            document,
            output_parent,
            backend=backend,
            api_source=_source(),
            capability_evidence=_capability(),
            run_identity=_run_identity(),
        )
    assert len(sender.calls) == 1
    usage = ledger.list_records("usage")
    assert len(usage) == 1
    assert usage[0]["completion_tokens"] == 8001
    seal_files = list(output_parent.rglob("resolved_run_seal.json"))
    assert len(seal_files) == 1
    assert (
        cache.lookup(
            consumer_seal=_load_json(seal_files[0]),
            logical_request_id=_run_identity().logical_request_id,
        )
        is None
    )
    assert list(cache.artifact_store.root.iterdir()) != []
    failure_files = list(output_parent.rglob("failure.json"))
    assert len(failure_files) == 1
    assert _load_json(failure_files[0])["mandatory_stop"] is True



def test_seal_and_output_identity_reject_implicit_resume(tmp_path: Path) -> None:
    document, report = _package(tmp_path)
    pack = _active_pack(report, document)
    first_identity = _run_identity("resume_one")
    second_identity = BoundaryRepairRunIdentity(
        run_id="inputnorm_run_resume_two",
        attempt_run_id="inputnorm_attempt_resume_two",
        stage_id=first_identity.stage_id,
        logical_request_id="boundary_repair_request_resume_two",
        implementation_commit=first_identity.implementation_commit,
    )
    first = build_boundary_repair_run_seal(
        report,
        pack,
        api_source=_source(),
        capability_evidence=_capability(),
        run_identity=first_identity,
    )
    second = build_boundary_repair_run_seal(
        report,
        pack,
        api_source=_source(),
        capability_evidence=_capability(),
        run_identity=second_identity,
    )
    assert first["seal"]["input_bindings_sha256"] == (
        second["seal"]["input_bindings_sha256"]
    )
    assert first["seal"]["seal_sha256"] != second["seal"]["seal_sha256"]
    assert first["seal"]["output_root_id"] != second["seal"]["output_root_id"]

    response = _response(report, pack)
    sender = _SuccessSender(_provider_response(json.dumps(response)))
    backend, _ledger, _cache = _backend(tmp_path / "backend", sender)
    run_shared_backend_boundary_repair(
        report,
        document,
        tmp_path / "outputs",
        backend=backend,
        api_source=_source(),
        capability_evidence=_capability(),
        run_identity=first_identity,
    )
    with pytest.raises(DraftStructureGatewayError, match="must be new or empty"):
        run_shared_backend_boundary_repair(
            report,
            document,
            tmp_path / "outputs",
            backend=backend,
            api_source=_source(),
            capability_evidence=_capability(),
            run_identity=first_identity,
        )
    assert len(sender.calls) == 1

def test_executor_rejects_prompt_and_context_tamper_before_backend(
    tmp_path: Path,
) -> None:
    document, report = _package(tmp_path)
    pack = _active_pack(report, document)
    sender = _SuccessSender(
        _provider_response(json.dumps(_response(report, pack)))
    )
    backend, _ledger, _cache = _backend(tmp_path / "backend", sender)
    invocation = build_boundary_repair_run_seal(
        report,
        pack,
        api_source=_source(),
        capability_evidence=_capability(),
        run_identity=_run_identity(),
    )
    executor = SharedBackendStructureExecutor(
        backend=backend,
        invocation=invocation,
        logical_request_id=_run_identity().logical_request_id,
    )
    with pytest.raises(DraftStructureGatewayError, match="prompt differs"):
        executor.complete("tampered", context_pack=pack)
    tampered_pack = copy.deepcopy(pack)
    tampered_pack["integrity"]["payload_sha256"] = "0" * 64
    with pytest.raises(DraftStructureGatewayError, match="context pack differs"):
        executor.complete(
            render_structure_prompt(
                pack,
                response_contract_dialect=BOUNDARY_REPAIR_SCHEMA_DIALECT_V2,
            ),
            context_pack=tampered_pack,
        )
    assert sender.calls == []


def test_migrated_module_has_no_direct_route_or_credential_transport() -> None:
    source = inspect.getsource(gateway_module)
    for forbidden in (
        "urllib",
        "load_runtime_bearer",
        "Bearer ",
        "http://localhost:8317",
        "gpt-5.4-mini",
        "OPENAI_API_KEY",
    ):
        assert forbidden not in source
