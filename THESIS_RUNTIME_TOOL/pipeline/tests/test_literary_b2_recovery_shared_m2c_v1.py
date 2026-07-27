from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
from typing import Any, Mapping

import pytest

from pipeline.literary.b2_event_batch_v1 import (
    render_event_review_batch_request_v1,
    validate_event_review_batch_response_v1,
)
from pipeline.literary.b2_recovery_batch_v1 import (
    REGISTRY_RECOVERY_BATCH_RESPONSE_SCHEMA_VERSION_V1,
    render_registry_recovery_batch_request_v1,
    validate_registry_recovery_batch_response_v1,
)
from pipeline.literary.b2_recovery_shared_runner_v1 import (
    B2RecoverySharedRunnerError,
    EVENT_ROLE_ID,
    REGISTRY_ROLE_ID,
    SHARED_RECOVERY_SEAL_SCHEMA_VERSION,
    _preregister_event_requests,
    _request_payload,
    _validated_resume_context,
)
from pipeline.literary.b2_recovery_v1 import (
    B2RecoveryContractError,
    build_b2_recovery_index_v1,
    build_registry_recovery_ledger_v1,
    render_event_review_request_v2,
    validate_event_review_response_v2,
)
from pipeline.literary.chapter_cycle_shared_runtime_v1 import (
    BACKEND_MODE_SHARED_V1,
    LiterarySharedRunnerBindingsV1,
    build_literary_code_ref_v1,
    capability_binding_key,
)
from pipeline.literary.checkpoint import canonical_hash, file_sha256
from pipeline.literary.model_ref_v1 import (
    MODEL_REF_FIELDS_V1,
    project_id_fields,
    project_model_request_v1,
    project_model_response_schema_v1,
)
from pipeline.literary.model_ref_transport_v1 import bind_model_ref_validator_v1
from pipeline.literary.shared_llm_adapter_v1 import LiterarySharedLlmAttemptAdapter
from pipeline.literary.structured_output_policy_v1 import validate_structured_payload
from pipeline.llm_backend import (
    ApplicationResponseCache,
    ContentAddressedArtifactStore,
    MappingCredentialProvider,
    PhysicalQuotaScheduler,
    RawTransportResponse,
    SharedLlmAttemptLedger,
    SharedLlmBackend,
    canonical_json,
    canonical_sha256,
    credential_commitment,
)
from pipeline.scripts.run_chapter_registry_v4_real import FROZEN_DB_SHA256
from pipeline.scripts.run_literary_b2_recovery_live_v1 import (
    B2RecoveryLiveError,
    _tree_hash,
    run,
)
from pipeline.tests.test_literary_b2_recovery_v1 import _fixture


REPO_ROOT = Path(__file__).resolve().parents[3]
SECRET = "synthetic-literary-m2c-secret"


def _local_ref_wire_schema(schema: Mapping[str, Any]) -> dict[str, Any]:
    return project_model_response_schema_v1(schema)


def _write_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(canonical_json(payload), encoding="utf-8")


def _profile(tmp_path: Path) -> Path:
    payload = {
        "schema_version": "literary_b2_recovery_live_profile_v3",
        "profile_id": "literary_b2_recovery_shared_m2c_test_v1",
        "provider_profile": "unused-in-shared-mode.json",
        "structured_output_policy": "unused-in-shared-mode.json",
        "stage_bindings": {
            "registry_recovery": {
                "provider_role_id": "literary_local_conflict_auditor",
                "schema_name": "literary_b2_registry_recovery_v1",
                "prompt_token_cap": 12000,
                "max_output_tokens": 8000,
            },
            "event_review": {
                "provider_role_id": "literary_local_conflict_auditor",
                "schema_name": "literary_b2_event_review_v2",
                "prompt_token_cap": 20000,
                "max_output_tokens": 12000,
            },
        },
        "generation": {
            "temperature": 1.0,
            "seed": 20260720,
            "reasoning_effort": "none",
            "verbosity": "low",
        },
        "limits": {
            "registry_recovery_calls": 4,
            "event_review_calls": 6,
            "max_total_calls": 10,
            "max_retries_per_call": 0,
            "hard_visible_token_cap": 160000,
        },
        "safety": {
            "provider_fallback_allowed": False,
            "source_artifact_mutation_allowed": False,
            "book_global_identity_mutation_allowed": False,
            "production_publish_enabled": False,
            "stop_after_chapter_id": "ch1",
            "event_review_contract_version": "v2",
        },
    }
    path = tmp_path / "recovery_profile.json"
    _write_json(path, payload)
    return path


def _source_root(
    tmp_path: Path, *, event_count: int | None = None
) -> tuple[Path, dict[str, Any], list[dict[str, Any]]]:
    artifact, requests = _fixture()
    if event_count is not None:
        request = deepcopy(requests[0])
        payload = json.loads(request["messages"][1]["content"])
        blocks = []
        events = []
        base_event = deepcopy(artifact["interaction_events"][0])
        for ordinal in range(event_count):
            block_id = f"ch1_batch_b{ordinal:02d}"
            text = f"Robin set a bowl before the hound number {ordinal}."
            blocks.append(
                {"block_id": block_id, "block_type": "paragraph", "text": text}
            )
            event = deepcopy(base_event)
            event["interaction_event_id"] = f"event_shared_{ordinal:02d}"
            event["block_id"] = block_id
            event["event_anchor"] = text
            event["action_summary"] = "Robin gives a bowl to the hound."
            event["source_spans"] = [{"char_start": 0, "char_end": len(text)}]
            events.append(event)
        packet = dict(payload["candidate_packets"])
        packet_body = {
            **packet,
            "active_block_ids": [row["block_id"] for row in blocks],
        }
        packet_body.pop("packet_hash")
        packet = {**packet_body, "packet_hash": canonical_hash(packet_body)}
        payload["active_blocks"] = blocks
        payload["candidate_packets"] = packet
        request_body = {
            **request,
            "messages": [
                request["messages"][0],
                {"role": "user", "content": canonical_json(payload)},
            ],
            "context_hashes": {
                **request["context_hashes"],
                "candidate_packet_hash": packet["packet_hash"],
                "window_hash": canonical_hash(blocks),
            },
        }
        request_body.pop("request_fingerprint")
        request = {
            **request_body,
            "request_fingerprint": canonical_hash(request_body),
        }
        requests = [request]
        body = {**artifact, "interaction_events": events}
        body.pop("artifact_hash")
        artifact = {**body, "artifact_hash": canonical_hash(body)}
    root = tmp_path / "b2_source"
    _write_json(root / "chapter_b2_artifact.json", artifact)
    for request in requests:
        _write_json(
            root / "interactions" / str(request["window_id"]) / "request.json",
            request,
        )
    return root, artifact, requests


def _registry_response(index: Mapping[str, Any]) -> tuple[dict[str, Any], Any]:
    component_ids = [
        str(row["component_id"])
        for row in index["registry_components"]
        if not row["overflow"]
    ]
    rendered = render_registry_recovery_batch_request_v1(
        index=index, component_ids=component_ids
    )
    tickets = {row["ticket_id"]: row for row in index["registry_gap_tickets"]}
    components = {row["component_id"]: row for row in index["registry_components"]}
    results = []
    for component_id in component_ids:
        actions = []
        for ticket_id in components[component_id]["ticket_ids"]:
            ticket = tickets[ticket_id]
            actions.append(
                {
                    "ticket_id": ticket_id,
                    "action": "keep_pending",
                    "target_candidate_card_id": None,
                    "narrowed_candidate_card_ids": [],
                    "provisional_group_key": None,
                    "canonical_surface": None,
                    "referent_kind": None,
                    "identity_summary": None,
                    "source_block_ids": list(ticket["source_block_ids"]),
                    "pending_reason": "The bounded chapter evidence does not settle identity.",
                    "resolution_note": "Retain the endpoint without identity authority.",
                }
            )
        results.append(
            {
                "component_id": component_id,
                "result": {
                    "schema_version": "literary_b2_registry_recovery_response_v1",
                    "chapter_id": index["chapter_id"],
                    "component_id": component_id,
                    "ticket_actions": actions,
                },
            }
        )
    return (
        {
            "schema_version": REGISTRY_RECOVERY_BATCH_RESPONSE_SCHEMA_VERSION_V1,
            "chapter_id": index["chapter_id"],
            "batch_id": rendered.component_id,
            "component_results": results,
        },
        rendered,
    )


def _event_response(
    *,
    index: Mapping[str, Any],
    artifact: Mapping[str, Any],
    registry_ledger: Mapping[str, Any],
    component_ids: list[str] | None = None,
) -> tuple[dict[str, Any], Any]:
    component_ids = component_ids or [
        str(row["component_id"])
        for row in index["event_components"]
        if not row["overflow"]
    ]
    cases = {row["case_id"]: row for row in index["event_review_cases"]}
    components = {row["component_id"]: row for row in index["event_components"]}
    if len(component_ids) == 1:
        component_id = component_ids[0]
        rendered = render_event_review_request_v2(
            index=index,
            component_id=component_id,
            chapter_artifact=artifact,
            registry_ledger=registry_ledger,
        )
        return (
            {
                "schema_version": "literary_b2_event_review_response_v2",
                "chapter_id": index["chapter_id"],
                "component_id": component_id,
                "event_actions": [
                    {
                        "case_id": case_id,
                        "action": "pending",
                        "replacement_events": [],
                        "effective_event_assessments": [],
                        "source_block_ids": list(cases[case_id]["source_block_ids"]),
                        "pending_reason": "Identity recovery remains unresolved.",
                        "resolution_note": "Hold event authority for later review.",
                    }
                    for case_id in components[component_id]["case_ids"]
                ],
            },
            rendered,
        )
    rendered = render_event_review_batch_request_v1(
        index=index,
        component_ids=component_ids,
        chapter_artifact=artifact,
        registry_ledger=registry_ledger,
    )
    results = []
    for component_id in component_ids:
        case_ids = list(components[component_id]["case_ids"])
        results.append(
            {
                "component_id": component_id,
                "case_channels": [
                    {
                        "case_id": case_id,
                        "observation_channel": "non_speech_observation",
                    }
                    for case_id in case_ids
                ],
                "result": {
                    "schema_version": "literary_b2_event_review_response_v2",
                    "chapter_id": index["chapter_id"],
                    "component_id": component_id,
                    "event_actions": [
                        {
                            "case_id": case_id,
                            "action": "pending",
                            "replacement_events": [],
                            "effective_event_assessments": [],
                            "source_block_ids": list(cases[case_id]["source_block_ids"]),
                            "pending_reason": "Identity recovery remains unresolved.",
                            "resolution_note": "Hold event authority for later review.",
                        }
                        for case_id in case_ids
                    ],
                },
            }
        )
    return (
        {
            "schema_version": "literary_b2_event_review_batch_response_v1_1",
            "chapter_id": index["chapter_id"],
            "batch_id": rendered.component_id,
            "component_results": results,
        },
        rendered,
    )


class _QueuedSender:
    def __init__(self, responses: list[dict[str, Any]]) -> None:
        self.responses = list(responses)
        self.calls = 0

    def send(self, request: Any) -> RawTransportResponse:
        body = json.loads(request.body.decode("utf-8"))
        assert body["model"] == "gpt-5.4"
        assert body["response_format"]["type"] == "json_schema"
        semantic = self.responses[self.calls]
        self.calls += 1
        response = {
            "id": f"fake-b2-m2c-{self.calls}",
            "model": "gpt-5.4",
            "choices": [
                {
                    "finish_reason": "stop",
                    "message": {"content": canonical_json(semantic)},
                }
            ],
            "usage": {
                "prompt_tokens": 300,
                "completion_tokens": 100,
                "total_tokens": 400,
                "prompt_tokens_details": {"cached_tokens": 0},
                "completion_tokens_details": {"reasoning_tokens": 0},
            },
        }
        return RawTransportResponse(
            status_code=200,
            headers={"x-request-id": f"fake-b2-m2c-{self.calls}"},
            body=canonical_json(response).encode("utf-8"),
            request_id=f"fake-b2-m2c-{self.calls}",
        )


def _model_local_response(
    rendered_request: Any, response: Mapping[str, Any]
) -> dict[str, Any]:
    _projected, ref_map = project_model_request_v1(
        _request_payload(rendered_request)
    )
    return project_id_fields(
        response,
        ref_map=ref_map,
        field_names_by_namespace=MODEL_REF_FIELDS_V1,
    )


def _source() -> dict[str, Any]:
    return {
        "schema_version": "api_source_v1",
        "source_id": "literary_m2c_fake_v1",
        "source_revision": "fake_transport_v1",
        "source_class": "remote_api",
        "adapter_id": "openai_python_v1",
        "protocol": "openai_chat_completions",
        "route_id": "chat_completions_create",
        "endpoint_class": "remote",
        "base_url": "https://provider.invalid/v1",
        "credential_ref": "credential.literary_m2c_fake_v1",
        "credential_commitment": credential_commitment(SECRET),
        "physical_quota_bucket_id": "literary-m2c-fake-v1",
        "enabled": True,
    }


def _capability(
    *, role_id: str, schema: Mapping[str, Any], validator_ref: Mapping[str, str]
) -> dict[str, Any]:
    return {
        "schema_version": "capability_evidence_v1",
        "capability_id": f"{role_id}.fake_so_v1",
        "capability_revision": "fake_transport_v1",
        "source_id": "literary_m2c_fake_v1",
        "source_revision": "fake_transport_v1",
        "adapter_id": "openai_python_v1",
        "protocol": "openai_chat_completions",
        "route_id": "chat_completions_create",
        "base_url": "https://provider.invalid/v1",
        "requested_model_id": "gpt-5.4",
        "observed_model_id": "gpt-5.4",
        "capability_kind": "native_structured_output",
        "schema_dialect": "json_schema_2020_12",
        "schema_sha256": canonical_sha256(_local_ref_wire_schema(schema)),
        "local_validator_id": validator_ref["id"],
        "local_validator_sha256": validator_ref["sha256"],
        "probe_id": "literary_m2c_fake_probe_v1",
        "evidence_sha256": "e" * 64,
        "observed_at_utc": "2026-07-20T00:00:00Z",
        "verdict": "qualified",
    }


def _runtime(
    tmp_path: Path,
    *,
    registry_schema: Mapping[str, Any],
    event_schemas: list[Mapping[str, Any]],
    event_mode: str,
    sender: _QueuedSender,
) -> LiterarySharedRunnerBindingsV1:
    registry_ref = build_literary_code_ref_v1(
        identifier="literary.b2.registry_recovery.validator",
        revision="batch_v1",
        callables=(
            validate_structured_payload,
            validate_registry_recovery_batch_response_v1,
        ),
    )
    event_ref = build_literary_code_ref_v1(
        identifier="literary.b2.event_review.validator",
        revision="single_v2" if event_mode == "single" else "batch_v1",
        callables=(
            validate_structured_payload,
            (
                validate_event_review_response_v2
                if event_mode == "single"
                else validate_event_review_batch_response_v1
            ),
        ),
    )
    source = _source()
    capabilities = {}
    for role_id, schema, validator_ref in [
        (REGISTRY_ROLE_ID, registry_schema, registry_ref),
        *(
            (EVENT_ROLE_ID, event_schema, event_ref)
            for event_schema in event_schemas
        ),
    ]:
        capabilities[capability_binding_key(role_id, schema)] = _capability(
            role_id=role_id,
            schema=schema,
            validator_ref=bind_model_ref_validator_v1(validator_ref),
        )
    store = ContentAddressedArtifactStore(tmp_path / "objects")
    backend = SharedLlmBackend(
        credential_provider=MappingCredentialProvider(
            {"credential.literary_m2c_fake_v1": SECRET}
        ),
        scheduler=PhysicalQuotaScheduler(tmp_path / "quota"),
        ledger=SharedLlmAttemptLedger(tmp_path / "attempts.sqlite3"),
        response_cache=ApplicationResponseCache(
            index_path=tmp_path / "cache.sqlite3", artifact_store=store
        ),
        sender=sender,
    )
    return LiterarySharedRunnerBindingsV1(
        adapter=LiterarySharedLlmAttemptAdapter(backend=backend),
        api_source=source,
        capabilities=capabilities,
        run_id="literary_m2c_fake_run",
        attempt_run_id="literary_m2c_fake_attempt",
        structured_output={
            "mode": "required",
            "schema_dialect": "json_schema_2020_12",
        },
    )


def test_shared_recovery_batches_both_roles_and_keeps_pending_non_authoritative(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source_root, artifact, requests = _source_root(tmp_path)
    index = build_b2_recovery_index_v1(
        chapter_artifact=artifact, interaction_requests=requests
    )
    registry_response, registry_request = _registry_response(index)
    registry_decision = validate_registry_recovery_batch_response_v1(
        registry_response,
        index=index,
        component_ids=[
            row["component_id"]
            for row in index["registry_components"]
            if not row["overflow"]
        ],
        request_fingerprint=registry_request.request_fingerprint,
    )
    registry_ledger = build_registry_recovery_ledger_v1(
        index=index, decisions=registry_decision["component_decisions"]
    )
    event_response, event_request = _event_response(
        index=index, artifact=artifact, registry_ledger=registry_ledger
    )
    event_component_ids = [
        row["component_id"]
        for row in index["event_components"]
        if not row["overflow"]
    ]
    assert len(event_component_ids) == 1
    base_event_request = render_event_review_request_v2(
        index=index,
        component_id=event_component_ids[0],
        chapter_artifact=artifact,
        registry_ledger=None,
    )
    sender = _QueuedSender(
        [
            _model_local_response(registry_request, registry_response),
            _model_local_response(event_request, event_response),
        ]
    )
    runtime = _runtime(
        tmp_path / "shared",
        registry_schema=registry_request.response_schema,
        event_schemas=[
            base_event_request.response_schema,
            event_request.response_schema,
        ],
        event_mode=(
            "single"
            if len([row for row in index["event_components"] if not row["overflow"]])
            == 1
            else "batch"
        ),
        sender=sender,
    )
    frozen = tmp_path / "frozen.sqlite3"
    frozen.write_bytes(b"synthetic frozen fixture")
    real_hash = file_sha256

    def fake_file_sha256(path: Path) -> str:
        return (
            FROZEN_DB_SHA256
            if Path(path).resolve() == frozen.resolve()
            else real_hash(path)
        )

    monkeypatch.setattr(
        "pipeline.literary.b2_recovery_shared_runner_v1.file_sha256",
        fake_file_sha256,
    )

    def forbidden(*_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("legacy recovery transport was selected")

    monkeypatch.setattr(
        "pipeline.scripts.run_literary_b2_recovery_live_v1._run_legacy",
        forbidden,
    )
    output = tmp_path / "recovery"
    report = run(
        repo_root=REPO_ROOT,
        b2_root=source_root,
        output_root=output,
        profile_path=_profile(tmp_path),
        credential_root=None,
        frozen_db=frozen,
        backend_mode=BACKEND_MODE_SHARED_V1,
        shared_runtime=runtime,
    )

    assert sender.calls == 2
    assert report["backend_mode"] == BACKEND_MODE_SHARED_V1
    assert report["provider_calls"] == 2
    assert report["pending_registry_ticket_count"] > 0
    assert report["pending_event_case_count"] > 0
    assert report["recovered_candidate_card_count"] == 0
    assert report["retry_performed"] is False
    assert report["fallback_performed"] is False
    assert (output / "registry_recovery_batch" / "shared_attempt_receipt.json").is_file()
    assert (
        output / "event_review_single_001" / "shared_attempt_receipt.json"
    ).is_file()
    assert event_request.response_schema == json.loads(
        (output / "event_review_single_001" / "request.json").read_text(
            encoding="utf-8"
        )
    )["response_schema"]


def test_recovery_backend_gate_rejects_mixing_and_shared_resume(
    tmp_path: Path,
) -> None:
    with pytest.raises(B2RecoveryLiveError, match="shared runtime"):
        run(
            repo_root=REPO_ROOT,
            b2_root=tmp_path,
            output_root=tmp_path / "out1",
            profile_path=tmp_path / "profile.json",
            credential_root=None,
            frozen_db=tmp_path / "frozen.sqlite3",
            backend_mode="shared_v1",
            shared_runtime=None,
        )
    with pytest.raises(B2RecoveryLiveError, match="credential root"):
        run(
            repo_root=REPO_ROOT,
            b2_root=tmp_path,
            output_root=tmp_path / "out2",
            profile_path=tmp_path / "profile.json",
            credential_root=tmp_path,
            frozen_db=tmp_path / "frozen.sqlite3",
            backend_mode="shared_v1",
            shared_runtime=object(),  # type: ignore[arg-type]
        )


def test_shared_recovery_uses_event_batch_for_multiple_components(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source_root, artifact, requests = _source_root(tmp_path, event_count=13)
    index = build_b2_recovery_index_v1(
        chapter_artifact=artifact, interaction_requests=requests
    )
    assert len([row for row in index["event_components"] if not row["overflow"]]) > 1
    registry_response, registry_request = _registry_response(index)
    registry_decision = validate_registry_recovery_batch_response_v1(
        registry_response,
        index=index,
        component_ids=[
            row["component_id"]
            for row in index["registry_components"]
            if not row["overflow"]
        ],
        request_fingerprint=registry_request.request_fingerprint,
    )
    registry_ledger = build_registry_recovery_ledger_v1(
        index=index, decisions=registry_decision["component_decisions"]
    )
    component_ids = [
        row["component_id"]
        for row in index["event_components"]
        if not row["overflow"]
    ]
    assert len(component_ids) == 2
    event_response, event_request = _event_response(
        index=index,
        artifact=artifact,
        registry_ledger=registry_ledger,
        component_ids=component_ids,
    )
    base_event_request = render_event_review_batch_request_v1(
        index=index,
        component_ids=component_ids,
        chapter_artifact=artifact,
        registry_ledger=None,
    )
    sender = _QueuedSender(
        [
            _model_local_response(registry_request, registry_response),
            _model_local_response(event_request, event_response),
        ]
    )
    runtime = _runtime(
        tmp_path / "shared",
        registry_schema=registry_request.response_schema,
        event_schemas=[
            base_event_request.response_schema,
            event_request.response_schema,
        ],
        event_mode="batch",
        sender=sender,
    )
    frozen = tmp_path / "frozen.sqlite3"
    frozen.write_bytes(b"synthetic frozen fixture")
    real_hash = file_sha256

    def fake_file_sha256(path: Path) -> str:
        return (
            FROZEN_DB_SHA256
            if Path(path).resolve() == frozen.resolve()
            else real_hash(path)
        )

    monkeypatch.setattr(
        "pipeline.literary.b2_recovery_shared_runner_v1.file_sha256",
        fake_file_sha256,
    )
    output = tmp_path / "recovery"
    report = run(
        repo_root=REPO_ROOT,
        b2_root=source_root,
        output_root=output,
        profile_path=_profile(tmp_path),
        credential_root=None,
        frozen_db=frozen,
        backend_mode=BACKEND_MODE_SHARED_V1,
        shared_runtime=runtime,
    )

    assert sender.calls == 2
    assert report["event_request_modes"] == ["batch"]
    assert (
        output / "event_review_batch_001" / "shared_attempt_receipt.json"
    ).is_file()


def test_event_preregistration_splits_an_incompatible_pair_without_dropping_ids(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[list[str]] = []

    def fake_render(**kwargs: Any) -> tuple[object, str]:
        ids = list(kwargs["component_ids"])
        calls.append(ids)
        if ids == ["c3", "c4"]:
            raise B2RecoveryContractError("synthetic incompatible source blocks")
        return object(), "single" if len(ids) == 1 else "batch"

    monkeypatch.setattr(
        "pipeline.literary.b2_recovery_shared_runner_v1._render_event_request",
        fake_render,
    )
    groups, requests = _preregister_event_requests(
        index={},
        component_ids=["c1", "c2", "c3", "c4", "c5"],
        chapter_artifact={},
    )

    assert groups == [["c1", "c2"], ["c3"], ["c4", "c5"]]
    assert len(requests) == 3
    assert calls == [["c1", "c2"], ["c3", "c4"], ["c3"], ["c4", "c5"]]


def test_shared_recovery_resume_requires_exact_failed_seal_identity(
    tmp_path: Path,
) -> None:
    source_root, artifact, requests = _source_root(tmp_path)
    index = build_b2_recovery_index_v1(
        chapter_artifact=artifact, interaction_requests=requests
    )
    profile_path = _profile(tmp_path)
    profile = json.loads(profile_path.read_text(encoding="utf-8"))

    class Runtime:
        def __init__(self, marker: str) -> None:
            self.marker = marker

        def identity_payload(self) -> dict[str, Any]:
            return {"backend": "shared_v1", "attempt": self.marker}

    runtime = Runtime("a")
    seal_body = {
        "schema_version": SHARED_RECOVERY_SEAL_SCHEMA_VERSION,
        "backend_mode": BACKEND_MODE_SHARED_V1,
        "source_b2_root": str(source_root.resolve()),
        "source_tree_hash": _tree_hash(source_root),
        "source_b2_artifact_hash": index["source_b2_artifact_hash"],
        "recovery_index_hash": index["recovery_index_hash"],
        "profile_id": profile["profile_id"],
        "profile_sha256": file_sha256(profile_path),
        "shared_runtime_identity": runtime.identity_payload(),
    }
    prior = tmp_path / "prior"
    _write_json(
        prior / "run_seal.json",
        {**seal_body, "seal_hash": canonical_hash(seal_body)},
    )
    _write_json(
        prior / "run_failure.json",
        {
            "schema_version": "literary_b2_recovery_shared_failure_v1",
            "run_seal_hash": canonical_hash(seal_body),
            "retry_performed": False,
            "fallback_performed": False,
        },
    )
    context = _validated_resume_context(
        resume_root=prior,
        source=source_root.resolve(),
        source_tree_hash=_tree_hash(source_root),
        index=index,
        profile_path=profile_path,
        profile=profile,
        shared_runtime=runtime,  # type: ignore[arg-type]
    )
    assert context is not None
    assert context["stage_reuse_performed"] is False
    assert context["semantic_requests_replayed_as_new"] is True

    with pytest.raises(
        B2RecoverySharedRunnerError, match="differs from the sealed run"
    ):
        _validated_resume_context(
            resume_root=prior,
            source=source_root.resolve(),
            source_tree_hash=_tree_hash(source_root),
            index=index,
            profile_path=profile_path,
            profile=profile,
            shared_runtime=Runtime("b"),  # type: ignore[arg-type]
        )

    _write_json(
        prior / "run_failure.json",
        {
            "schema_version": "literary_b2_recovery_shared_failure_v1",
            "run_seal_hash": "0" * 64,
            "retry_performed": False,
            "fallback_performed": False,
        },
    )
    with pytest.raises(
        B2RecoverySharedRunnerError, match="differs from the sealed run"
    ):
        _validated_resume_context(
            resume_root=prior,
            source=source_root.resolve(),
            source_tree_hash=_tree_hash(source_root),
            index=index,
            profile_path=profile_path,
            profile=profile,
            shared_runtime=runtime,  # type: ignore[arg-type]
        )
