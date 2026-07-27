from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path

import pytest

from pipeline.llm_backend import (
    ApplicationResponseCache,
    ContentAddressedArtifactStore,
    ContractValidationError,
    MappingCredentialProvider,
    PhysicalQuotaScheduler,
    RawTransportResponse,
    SharedLlmAttemptLedger,
    SharedLlmBackend,
    SharedLlmCapabilityProbe,
    TransportCallError,
    canonical_json,
    canonical_sha256,
    credential_commitment,
)
from pipeline.literary import b3_temporal_live_v1 as live_module
from pipeline.literary.b3_temporal_context_v2 import (
    build_b3_temporal_live_bundle_v2,
)
from pipeline.literary.b3_temporal_contract_v1 import B3TemporalContractError
from pipeline.literary.b3_temporal_contract_v2 import (
    normalize_b3_temporal_response_v2,
)
from pipeline.literary.b3_temporal_live_v1 import (
    execute_b3_temporal_canary_v1,
    prepare_b3_temporal_canary_v1,
)
from pipeline.literary.b3_temporal_prompts_v2 import (
    b3_temporal_response_schema_v2,
)
from pipeline.literary.chapter_cycle_shared_runtime_v1 import (
    LiterarySharedRunnerBindingsV1,
    capability_binding_key,
)
from pipeline.literary.openai_b3_json_object_capability_probe_v1 import (
    ROLE_ID,
    RUNTIME_PROFILE_PATH,
    build_literary_openai_b3_probe_plan_v1,
    empty_b3_probe_response_v1,
    execute_literary_openai_b3_probe_once_v1,
    implementation_sha256_v1,
    synthetic_b3_probe_request_v2,
)
from pipeline.literary.model_ref_v1 import project_model_response_schema_v1
from pipeline.literary.shared_llm_adapter_v1 import LiterarySharedLlmAttemptAdapter
from pipeline.literary.shared_runtime_profile_v2 import (
    LiterarySharedRuntimeProfileV2Error,
    load_literary_shared_runtime_profile_v2,
)
from pipeline.scripts.run_literary_b3_temporal_canary_v1 import (
    _now as cli_now,
    _probe_usage,
)
from pipeline.tests.test_literary_b3_temporal_v1 import (
    _base_response,
    _profile,
    _temporal_input,
)
from pipeline.tests.literary_capability_probe_test_support import (
    model_facing_probe_payload_v1,
)


SECRET = "literary-b3-offline-secret"
IMPLEMENTATION_BINDING = {
    "shared_core_revision": "de7c74ba348bffe507aa86933f438b3ed4c5af29",
    "consumer_revision": "a" * 40,
    "consumer_implementation_sha256": implementation_sha256_v1(),
}


class _Clock:
    def __init__(self) -> None:
        start = datetime(2026, 7, 20, tzinfo=timezone.utc)
        self.values = [start + timedelta(seconds=index) for index in range(20)]

    def __call__(self):
        return self.values.pop(0)


class _ProbeSender:
    def __init__(
        self,
        plan,
        *,
        model: str = "gpt-5.4-2026-03-05",
        content: str | None = None,
        fail: bool = False,
    ) -> None:
        self.plan = plan
        self.model = model
        self.content = content
        self.fail = fail
        self.calls = 0

    def send(self, request):
        self.calls += 1
        assert request.url == "https://api.openai.com/v1/chat/completions"
        assert json.loads(request.body)["response_format"] == {"type": "json_object"}
        if self.fail:
            raise TransportCallError(
                code="http_503",
                status_code=503,
                safe_message="provider returned HTTP 503",
            )
        content = self.content
        if content is None:
            content = canonical_json(
                model_facing_probe_payload_v1(
                    self.plan,
                    empty_b3_probe_response_v1(synthetic_b3_probe_request_v2()),
                )
            )
        payload = {
            "id": "b3-probe",
            "model": self.model,
            "choices": [
                {
                    "finish_reason": "stop",
                    "message": {"content": content},
                }
            ],
            "usage": {
                "prompt_tokens": 1000,
                "completion_tokens": 40,
                "total_tokens": 1040,
                "prompt_tokens_details": {"cached_tokens": 0},
                "completion_tokens_details": {"reasoning_tokens": 0},
            },
        }
        return RawTransportResponse(
            status_code=200,
            headers={"x-request-id": "b3-probe"},
            body=canonical_json(payload).encode("utf-8"),
            request_id="b3-probe",
        )


class _CanarySender:
    def __init__(self) -> None:
        self.calls = 0

    def send(self, request):
        self.calls += 1
        body = json.loads(request.body)
        assert body["response_format"] == {"type": "json_object"}
        user = next(
            row for row in reversed(body["messages"]) if row["role"] == "user"
        )
        payload = json.loads(user["content"])
        semantic = {
            "schema_version": "literary_b3_temporal_response_v1",
            "chapter_id": payload["chapter_id"],
            "batch_id": payload["batch_id"],
            "component_results": [
                {
                    "component_id": row["component_id"],
                    "disposition": "no_durable_change",
                    "state_actions": [],
                    "pending_route": "none",
                    "pending_reason": None,
                }
                for row in payload["components"]
            ],
        }
        provider = {
            "id": "b3-canary",
            "model": "gpt-5.4-2026-03-05",
            "choices": [
                {
                    "finish_reason": "stop",
                    "message": {"content": canonical_json(semantic)},
                }
            ],
            "usage": {
                "prompt_tokens": 1500,
                "completion_tokens": 80,
                "total_tokens": 1580,
                "prompt_tokens_details": {"cached_tokens": 0},
                "completion_tokens_details": {"reasoning_tokens": 0},
            },
        }
        return RawTransportResponse(
            status_code=200,
            headers={"x-request-id": "b3-canary"},
            body=canonical_json(provider).encode("utf-8"),
            request_id="b3-canary",
        )


class _CanaryFailureSender:
    def __init__(self) -> None:
        self.calls = 0

    def send(self, _request):
        self.calls += 1
        raise TransportCallError(
            code="http_503",
            status_code=503,
            safe_message="provider returned HTTP 503",
        )


def _probe_plan():
    return build_literary_openai_b3_probe_plan_v1(
        probe_run_id="literary_b3_probe_fixture",
        credential_commitment_sha256=credential_commitment(SECRET),
        issued_at_utc="2026-07-20T00:00:00Z",
        implementation_binding=IMPLEMENTATION_BINDING,
    )


def _qualified_evidence(tmp_path: Path):
    plan = _probe_plan()
    sender = _ProbeSender(plan)
    probe, _ledger = _probe_runtime(tmp_path, sender)
    result = execute_literary_openai_b3_probe_once_v1(probe=probe, plan=plan)
    assert result["status"] == "qualified"
    assert sender.calls == 1
    return result["capability_evidence"]


def _probe_runtime(tmp_path: Path, sender):
    tmp_path.mkdir(parents=True, exist_ok=True)
    ledger = SharedLlmAttemptLedger(tmp_path / "probe.sqlite3")
    probe = SharedLlmCapabilityProbe(
        credential_provider=MappingCredentialProvider(
            {"credential.openai_row2": SECRET}
        ),
        scheduler=PhysicalQuotaScheduler(tmp_path / "probe_quota"),
        ledger=ledger,
        artifact_store=ContentAddressedArtifactStore(tmp_path / "probe_artifacts"),
        sender=sender,
        implementation_binding=IMPLEMENTATION_BINDING,
        clock=_Clock(),
    )
    return probe, ledger


def _runtime(
    tmp_path: Path,
    evidence,
    sender,
    *,
    runtime_profile_path=RUNTIME_PROFILE_PATH,
    capability_schema=None,
    capability_validator_ref=None,
):
    profile = load_literary_shared_runtime_profile_v2(
        runtime_profile_path,
        expected_role_ids={ROLE_ID},
    )
    binding = dict(profile.source_binding_for(ROLE_ID))
    source = {
        "schema_version": "api_source_v1",
        "source_id": binding["source_id"],
        "source_revision": binding["source_revision"],
        "source_class": binding["source_class"],
        "adapter_id": binding["adapter_id"],
        "protocol": binding["protocol"],
        "route_id": binding["route_id"],
        "endpoint_class": binding["endpoint_class"],
        "base_url": binding["base_url"],
        "credential_ref": binding["credential_ref"],
        "credential_commitment": credential_commitment(SECRET),
        "physical_quota_bucket_id": binding["physical_quota_bucket_id"],
        "enabled": True,
    }
    store = ContentAddressedArtifactStore(tmp_path / "run_artifacts")
    backend = SharedLlmBackend(
        credential_provider=MappingCredentialProvider(
            {"credential.openai_row2": SECRET}
        ),
        scheduler=PhysicalQuotaScheduler(tmp_path / "run_quota"),
        ledger=SharedLlmAttemptLedger(tmp_path / "run.sqlite3"),
        response_cache=ApplicationResponseCache(
            index_path=tmp_path / "cache.sqlite3", artifact_store=store
        ),
        sender=sender,
        clock=_Clock(),
    )
    bound_schema = capability_schema or b3_temporal_response_schema_v2()
    bound_evidence = dict(evidence)
    if capability_schema is not None:
        bound_evidence["schema_sha256"] = canonical_sha256(
            project_model_response_schema_v1(bound_schema)
        )
    if capability_validator_ref is not None:
        bound_evidence["local_validator_id"] = capability_validator_ref["id"]
        bound_evidence["local_validator_sha256"] = capability_validator_ref[
            "sha256"
        ]
    return LiterarySharedRunnerBindingsV1(
        adapter=LiterarySharedLlmAttemptAdapter(backend=backend),
        api_source=None,
        capabilities={capability_binding_key(ROLE_ID, bound_schema): bound_evidence},
        run_id="literary_b3_fixture_run",
        attempt_run_id="literary_b3_fixture_attempt",
        structured_output=None,
        runtime_profile=profile,
        api_sources_by_alias={"openai_official_row2": source},
    )


def test_stable_schema_reuses_hash_but_local_validator_closes_ids() -> None:
    first = build_b3_temporal_live_bundle_v2(
        temporal_input=_temporal_input(), profile=_profile()
    )["requests"][0]
    second_input = deepcopy(_temporal_input())
    second_input["chapter_id"] = "book_ch02"
    second_input["input_hash"] = "f" * 64
    second = build_b3_temporal_live_bundle_v2(
        temporal_input=second_input, profile=_profile()
    )["requests"][0]
    assert first["response_schema_hash"] == second["response_schema_hash"]
    assert first["request_fingerprint"] != second["request_fingerprint"]
    response = _base_response(first)
    response["component_results"][0]["component_id"] = "foreign_component"
    with pytest.raises(B3TemporalContractError, match="foreign component"):
        normalize_b3_temporal_response_v2(request=first, response=response)


def test_live_timestamps_use_shared_contract_utc_shape() -> None:
    assert cli_now().endswith("Z")
    assert live_module._now().endswith("Z")


def test_probe_report_projects_flat_shared_receipt_usage() -> None:
    assert _probe_usage(
        {
            "prompt_tokens": 1635,
            "completion_tokens": 78,
            "total_tokens": 1713,
            "cached_input_tokens": 0,
            "reasoning_tokens": 0,
        }
    ) == {
        "prompt_tokens": 1635,
        "completion_tokens": 78,
        "total_tokens": 1713,
        "cached_input_tokens": 0,
        "reasoning_tokens": 0,
    }
    assert _probe_usage({"prompt_tokens": 1}) is None


def test_runtime_profile_subset_is_explicit() -> None:
    profile = load_literary_shared_runtime_profile_v2(
        RUNTIME_PROFILE_PATH,
        expected_role_ids={ROLE_ID},
    )
    assert set(profile.role_bindings) == {ROLE_ID}
    with pytest.raises(LiterarySharedRuntimeProfileV2Error, match="exact-cover"):
        load_literary_shared_runtime_profile_v2(RUNTIME_PROFILE_PATH)


@pytest.mark.parametrize(
    ("content", "model", "fail", "expected_code"),
    [
        ("not json", "gpt-5.4-2026-03-05", False, "response_json_invalid"),
        ("{}", "gpt-5.4-2026-03-05", False, "local_validator_rejected"),
        (None, "foreign-model", False, "observed_model_mismatch"),
        (None, "gpt-5.4-2026-03-05", True, "http_503"),
    ],
)
def test_probe_failures_remain_terminal_and_non_authoritative(
    tmp_path: Path,
    content: str | None,
    model: str,
    fail: bool,
    expected_code: str,
) -> None:
    plan = _probe_plan()
    sender = _ProbeSender(plan, model=model, content=content, fail=fail)
    probe, ledger = _probe_runtime(tmp_path / expected_code, sender)
    result = execute_literary_openai_b3_probe_once_v1(probe=probe, plan=plan)
    assert result["status"] == "failed"
    assert result["receipt"]["failure"]["code"] == expected_code
    assert result["capability_evidence"]["verdict"] == "failed"
    assert sender.calls == 1
    assert ledger.count("usage") == 0
    assert ledger.count("cache") == 0


def test_same_b3_probe_cannot_call_twice(tmp_path: Path) -> None:
    plan = _probe_plan()
    sender = _ProbeSender(plan)
    probe, _ledger = _probe_runtime(tmp_path, sender)
    assert execute_literary_openai_b3_probe_once_v1(
        probe=probe, plan=plan
    )["status"] == "qualified"
    with pytest.raises(ContractValidationError, match="already reserved"):
        execute_literary_openai_b3_probe_once_v1(probe=probe, plan=plan)
    assert sender.calls == 1


def test_probe_then_one_call_canary_through_shared_backend(
    tmp_path: Path, monkeypatch
) -> None:
    evidence = _qualified_evidence(tmp_path)
    sender = _CanarySender()
    runtime = _runtime(tmp_path, evidence, sender)
    source_root = tmp_path / "b2_source"
    source_root.mkdir()
    (source_root / "immutable.json").write_text("{}\n", encoding="utf-8")
    temporal_input = _temporal_input()
    temporal_input["source_b2_artifact_hash"] = "1" * 64
    temporal_input["source_prefix_bundle_hash"] = "2" * 64
    unsigned = dict(temporal_input)
    unsigned.pop("input_hash", None)
    temporal_input["input_hash"] = canonical_sha256(unsigned)
    monkeypatch.setattr(
        live_module,
        "load_b2_temporal_input_v1",
        lambda _root: deepcopy(temporal_input),
    )
    output = tmp_path / "b3_live"
    prepare_b3_temporal_canary_v1(
        b2_run_root=source_root,
        output_root=output,
        profile=_profile(),
        shared_runtime=runtime,
        current_git_head="a" * 40,
    )
    report = execute_b3_temporal_canary_v1(
        output_root=output,
        shared_runtime=runtime,
        current_git_head="a" * 40,
    )
    assert sender.calls == 1
    assert report["status"] == "complete_mandatory_stop"
    assert report["api_calls_performed"] == 1
    assert report["usage"]["total_tokens"] == 1580
    assert (output / "shared_attempt_receipt.json").is_file()
    assert (output / "b3_temporal_artifact.json").is_file()


def test_failed_canary_records_terminal_stage_failure(
    tmp_path: Path, monkeypatch
) -> None:
    evidence = _qualified_evidence(tmp_path / "capability")
    sender = _CanaryFailureSender()
    runtime = _runtime(tmp_path / "runtime", evidence, sender)
    source_root = tmp_path / "b2_source"
    source_root.mkdir()
    (source_root / "immutable.json").write_text("{}\n", encoding="utf-8")
    temporal_input = _temporal_input()
    temporal_input["source_b2_artifact_hash"] = "1" * 64
    temporal_input["source_prefix_bundle_hash"] = "2" * 64
    unsigned = dict(temporal_input)
    unsigned.pop("input_hash", None)
    temporal_input["input_hash"] = canonical_sha256(unsigned)
    monkeypatch.setattr(
        live_module,
        "load_b2_temporal_input_v1",
        lambda _root: deepcopy(temporal_input),
    )
    output = tmp_path / "b3_failed"
    prepare_b3_temporal_canary_v1(
        b2_run_root=source_root,
        output_root=output,
        profile=_profile(),
        shared_runtime=runtime,
        current_git_head="a" * 40,
    )
    with pytest.raises(TransportCallError, match="HTTP 503"):
        execute_b3_temporal_canary_v1(
            output_root=output,
            shared_runtime=runtime,
            current_git_head="a" * 40,
        )
    assert sender.calls == 1
    failure = json.loads((output / "stage_failure.json").read_text(encoding="utf-8"))
    assert failure["status"] == "halted_fail_closed"
    assert failure["error_type"] == "TransportCallError"
    assert not (output / "b3_temporal_artifact.json").exists()
