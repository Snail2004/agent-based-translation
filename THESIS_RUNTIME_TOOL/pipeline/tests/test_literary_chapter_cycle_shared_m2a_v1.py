from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Mapping

import pytest

from pipeline.literary.b0_entity_inventory_experiment import (
    entity_inventory_response_schema,
    validate_entity_inventory_response,
)
from pipeline.literary.chapter_cycle_live_executor_v1 import (
    ChapterCycleLiveExecutorError,
    ChapterCycleLiveExecutorV1,
)
from pipeline.literary.chapter_cycle_orchestrator_v1 import (
    ApiCallPermit,
    current_chapter_cycle_stage_v1,
    initialize_chapter_cycle_run_v1,
    load_chapter_cycle_plan_v1,
    ChapterCycleStage,
)
from pipeline.literary.chapter_cycle_shared_runtime_v1 import (
    BACKEND_MODE_SHARED_V1,
    LiterarySharedRunnerBindingsV1,
    build_literary_code_ref_v1,
    capability_binding_key,
)
from pipeline.literary.model_ref_transport_v1 import bind_model_ref_validator_v1
from pipeline.literary.shared_llm_adapter_v1 import LiterarySharedLlmAttemptAdapter
from pipeline.literary.shared_llm_profiles_v1 import ROLE_PRESETS
from pipeline.literary.shared_runtime_profile_v1 import (
    load_literary_shared_runtime_profile_v1,
)
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


RUNTIME_ROOT = Path(__file__).resolve().parents[2]
PIPELINE_PROFILE = (
    RUNTIME_ROOT / "pipeline" / "configs" / "literary_pipeline_profile_v2.json"
)
SECRET = "synthetic-literary-m2a-secret"


def _fake_local_normalize(
    raw: Mapping[str, Any], **_kwargs: Any
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    return dict(raw), []


def _fake_local_apply(raw: Mapping[str, Any], **_kwargs: Any) -> dict[str, Any]:
    return {
        "schema_version": "fake_local_audited_inventory_v1",
        "component_decisions": list(raw.get("component_decisions") or []),
        "conflict_audited_inventory_hash": "1" * 64,
    }


def _fake_claim_validator(
    raw: Mapping[str, Any], **_kwargs: Any
) -> dict[str, Any]:
    return dict(raw)


def _fake_identity_validator(
    raw: Mapping[str, Any], **_kwargs: Any
) -> dict[str, Any]:
    return dict(raw)


class _Permit:
    def __init__(self) -> None:
        self.logical_ids: list[str] = []

    def reserve(self, logical_id: str) -> None:
        self.logical_ids.append(logical_id)

    def attempt_count(self) -> int:
        return len(self.logical_ids)


class _RecordingSharedRuntime:
    api_source = {"protocol": "openai_chat_completions"}

    def __init__(self, raw_by_role: Mapping[str, Mapping[str, Any]]) -> None:
        self.raw_by_role = dict(raw_by_role)
        self.roles: list[str] = []

    def role_preset_for(self, role_id: str):
        return ROLE_PRESETS[role_id]

    def api_source_for(self, _role_id: str) -> Mapping[str, Any]:
        return self.api_source

    def execute_accepted_request(self, **kwargs: Any) -> SimpleNamespace:
        role_id = kwargs["role_id"]
        self.roles.append(role_id)
        semantic = kwargs["semantic_validator"](self.raw_by_role[role_id])
        output = Path(kwargs["output_dir"])
        output.mkdir(parents=True, exist_ok=True)
        seal = {
            "seal_sha256": "2" * 64,
            "primary": {"target": {"requested_model_id": "gpt-5.4"}},
        }
        receipt = {
            "schema_version": "literary_shared_attempt_receipt_v1",
            "backend_mode": "shared_v1",
            "role_id": role_id,
            "stage_id": kwargs["stage_id"],
            "logical_request_id": kwargs["logical_request_id"],
            "semantic_attempt_index": 1,
            "transport_retry_ordinal": 0,
            "request_fingerprint": kwargs["request"]["request_fingerprint"],
            "request_sha256": canonical_sha256(kwargs["request"]),
            "semantic_output_sha256": canonical_sha256(semantic),
            "provider_artifact_sha256": "3" * 64,
            "seal": seal,
            "seal_sha256": seal["seal_sha256"],
            "usage": None,
            "cache_observation": None,
            "application_response_cache": "disabled",
            "semantic_status": "semantic_accepted",
            "production_publish_performed": False,
        }
        receipt["receipt_sha256"] = canonical_sha256(receipt)
        (output / "shared_attempt_receipt.json").write_text(
            canonical_json(receipt), encoding="utf-8"
        )
        return SimpleNamespace(semantic_payload=semantic)


class _B1Sender:
    def __init__(self) -> None:
        self.calls = 0

    def send(self, request) -> RawTransportResponse:
        self.calls += 1
        body = json.loads(request.body.decode("utf-8"))
        assert body["model"] == "gpt-5.4"
        assert body["response_format"]["type"] == "json_schema"
        semantic = {
            "entity_candidates": [],
            "glossary_candidates": [],
            "unresolved_referents": [],
            "chapter_priority_order": [],
        }
        response = {
            "id": "fake-b1-m2a",
            "model": "gpt-5.4",
            "choices": [
                {
                    "finish_reason": "stop",
                    "message": {"content": canonical_json(semantic)},
                }
            ],
            "usage": {
                "prompt_tokens": 300,
                "completion_tokens": 80,
                "total_tokens": 380,
                "prompt_tokens_details": {"cached_tokens": 0},
                "completion_tokens_details": {"reasoning_tokens": 0},
            },
        }
        return RawTransportResponse(
            status_code=200,
            headers={"x-request-id": "fake-b1-m2a"},
            body=canonical_json(response).encode("utf-8"),
            request_id="fake-b1-m2a",
        )


def _write_document(tmp_path: Path) -> Path:
    payload = {
        "document_id": "literary-m2a-fixture",
        "chapters": [
            {
                "chapter_id": "fixture_ch01",
                "blocks": [
                    {
                        "block_id": "fixture_ch01_b001",
                        "order_index": 1,
                        "block_type": "paragraph",
                        "clean_text": "Mr. Vale enters the house.",
                    }
                ],
            }
        ],
    }
    path = tmp_path / "document.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _source() -> dict[str, Any]:
    return {
        "schema_version": "api_source_v1",
        "source_id": "literary_m2a_fake_v1",
        "source_revision": "fake_transport_v1",
        "source_class": "remote_api",
        "adapter_id": "openai_python_v1",
        "protocol": "openai_chat_completions",
        "route_id": "chat_completions_create",
        "endpoint_class": "remote",
        "base_url": "https://provider.invalid/v1",
        "credential_ref": "credential.literary_m2a_fake_v1",
        "credential_commitment": credential_commitment(SECRET),
        "physical_quota_bucket_id": "literary-m2a-fake-v1",
        "enabled": True,
    }


def _capability(schema: dict[str, Any], validator_sha256: str) -> dict[str, Any]:
    return {
        "schema_version": "capability_evidence_v1",
        "capability_id": "literary.b1.entity_inventory.fake_so_v1",
        "capability_revision": "fake_transport_v1",
        "source_id": "literary_m2a_fake_v1",
        "source_revision": "fake_transport_v1",
        "adapter_id": "openai_python_v1",
        "protocol": "openai_chat_completions",
        "route_id": "chat_completions_create",
        "base_url": "https://provider.invalid/v1",
        "requested_model_id": "gpt-5.4",
        "observed_model_id": "gpt-5.4",
        "capability_kind": "native_structured_output",
        "schema_dialect": "json_schema_2020_12",
        "schema_sha256": canonical_sha256(schema),
        "local_validator_id": "literary.b1.entity_inventory.validator",
        "local_validator_sha256": validator_sha256,
        "probe_id": "literary_m2a_fake_probe_v1",
        "evidence_sha256": "e" * 64,
        "observed_at_utc": "2026-07-20T00:00:00Z",
        "verdict": "qualified",
    }


def _runtime(
    tmp_path: Path,
    sender: _B1Sender,
    *,
    use_console_profile: bool = False,
) -> LiterarySharedRunnerBindingsV1:
    schema = entity_inventory_response_schema()
    validator_ref = build_literary_code_ref_v1(
        identifier="literary.b1.entity_inventory.validator",
        revision="v1",
        callables=(validate_structured_payload, validate_entity_inventory_response),
    )
    source = _source()
    capability_ref = bind_model_ref_validator_v1(validator_ref)
    capability = _capability(schema, capability_ref["sha256"])
    store = ContentAddressedArtifactStore(tmp_path / "objects")
    backend = SharedLlmBackend(
        credential_provider=MappingCredentialProvider(
            {"credential.literary_m2a_fake_v1": SECRET}
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
        capabilities={
            capability_binding_key("literary.b1.entity_inventory", schema): capability
        },
        run_id="literary_m2a_fake_run",
        attempt_run_id="literary_m2a_fake_attempt",
        structured_output={
            "mode": "required",
            "schema_dialect": "json_schema_2020_12",
        },
        runtime_profile=(
            load_literary_shared_runtime_profile_v1()
            if use_console_profile
            else None
        ),
    )


def test_console_profile_hash_enters_shared_runtime_identity(tmp_path: Path) -> None:
    runtime = _runtime(
        tmp_path,
        _B1Sender(),
        use_console_profile=True,
    )
    profile = load_literary_shared_runtime_profile_v1()
    identity = runtime.identity_payload()
    assert identity["pipeline_profile_id"] == profile.profile_id
    assert identity["pipeline_profile_revision"] == profile.profile_revision
    assert identity["pipeline_profile_sha256"] == profile.profile_sha256


def _initialized_run(tmp_path: Path) -> tuple[Path, dict[str, Any]]:
    document = _write_document(tmp_path)
    frozen = tmp_path / "frozen.sqlite3"
    frozen.write_bytes(b"literary-m2a-frozen-fixture")
    run_root = tmp_path / "run"
    from pipeline.literary.literary_pipeline_profile_v1 import (
        load_literary_pipeline_profile,
    )

    profile = load_literary_pipeline_profile(PIPELINE_PROFILE)
    initialize_chapter_cycle_run_v1(
        run_root=run_root,
        document_path=document,
        profile_path=profile.chapter_cycle_profile_path,
        frozen_db_path=frozen,
        ordered_chapter_ids=["fixture_ch01"],
        stop_after_chapter_count=1,
        pipeline_profile_path=PIPELINE_PROFILE,
    )
    return run_root, load_chapter_cycle_plan_v1(run_root)


def test_shared_mode_requires_shared_runtime_and_forbids_legacy_credentials(
    tmp_path: Path,
) -> None:
    run_root, plan = _initialized_run(tmp_path)
    with pytest.raises(ChapterCycleLiveExecutorError, match="requires an injected"):
        ChapterCycleLiveExecutorV1(
            run_root=run_root,
            plan=plan,
            credential_root=None,
            backend_mode=BACKEND_MODE_SHARED_V1,
        )
    sender = _B1Sender()
    with pytest.raises(ChapterCycleLiveExecutorError, match="legacy credential root"):
        ChapterCycleLiveExecutorV1(
            run_root=run_root,
            plan=plan,
            credential_root=tmp_path,
            backend_mode=BACKEND_MODE_SHARED_V1,
            shared_runtime=_runtime(tmp_path / "shared", sender),
        )
    assert sender.calls == 0


def test_b1_stage_uses_one_shared_attempt_and_never_calls_legacy_transport(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_root, plan = _initialized_run(tmp_path)
    sender = _B1Sender()
    runtime = _runtime(
        tmp_path / "shared", sender, use_console_profile=True
    )

    def forbidden(*_args, **_kwargs):
        raise AssertionError("legacy credential or transport path was selected")

    monkeypatch.setattr(
        "pipeline.literary.chapter_cycle_live_executor_v1.resolve_role_credential",
        forbidden,
    )
    monkeypatch.setattr(
        "pipeline.literary.chapter_cycle_live_executor_v1.run_b1_live",
        forbidden,
    )
    executor = ChapterCycleLiveExecutorV1(
        run_root=run_root,
        plan=plan,
        credential_root=None,
        backend_mode=BACKEND_MODE_SHARED_V1,
        shared_runtime=runtime,
    )
    stage = current_chapter_cycle_stage_v1(run_root)
    assert stage is not None and stage.stage_name == "b0"
    result = executor(
        stage,
        ApiCallPermit(run_root=run_root, plan=plan, stage=stage),
    )

    assert sender.calls == 1
    assert result.status == "accepted"
    assert result.call_disposition == "called"
    assert result.model_actual == "gpt-5.4"
    assert result.payload["backend_mode"] == "shared_v1"
    live = run_root / "stages" / "ch001_b0" / "live"
    inventory = json.loads((live / "inventory.json").read_text(encoding="utf-8"))
    receipt = json.loads(
        (live / "shared_attempt_receipt.json").read_text(encoding="utf-8")
    )
    assert inventory["entity_candidates"] == []
    assert receipt["backend_mode"] == "shared_v1"
    assert receipt["seal"]["role_id"] == "literary.b1.entity_inventory"
    assert receipt["seal"]["profile"]["record"]["profile_revision"] == (
        "shared_runtime_recommended_v1"
    )
    assert receipt["cache_observation"] is None
    assert (live / "shared_execution_report.json").is_file()
    assert not (live / "experiment_report.json").exists()


def test_local_stable_and_identity_runner_branches_route_only_to_shared_mode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_root, plan = _initialized_run(tmp_path)
    runtime = _RecordingSharedRuntime(
        {
            "literary.audit.local_conflict": {
                "chapter_id": "fixture_ch01",
                "component_decisions": [],
                "glossary_dispositions": [],
            },
            "literary.audit.stable_claim": {
                "component_id": "claim_component_1",
                "ticket_actions": [],
            },
            "literary.audit.identity_surface": {
                "component_id": "identity_component_1",
                "candidate_actions": [],
                "surface_scope_actions": [],
            },
        }
    )
    executor = ChapterCycleLiveExecutorV1(
        run_root=run_root,
        plan=plan,
        credential_root=None,
        backend_mode=BACKEND_MODE_SHARED_V1,
        shared_runtime=runtime,  # type: ignore[arg-type]
    )

    def forbidden(*_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("legacy transport path was selected")

    for name in (
        "resolve_role_credential",
        "resolve_role_credentials",
        "run_local_auditor_live",
        "run_claim_component_live",
        "run_identity_component_live",
    ):
        monkeypatch.setattr(
            f"pipeline.literary.chapter_cycle_live_executor_v1.{name}", forbidden
        )

    local_stage = ChapterCycleStage(
        stage_id="ch001_local_auditor",
        chapter_id="fixture_ch01",
        chapter_ordinal=1,
        stage_name="local_auditor",
        stage_role="local_auditor",
        requires_api=True,
        is_chapter_checkpoint=False,
        stage_descriptor_hash="4" * 64,
    )
    local_paths = executor._paths(local_stage)
    local_paths["b1_inventory"].parent.mkdir(parents=True, exist_ok=True)
    local_paths["b1_inventory"].write_text(
        canonical_json(
            {
                "schema_version": "fake_inventory_v1",
                "inventory_hash": "5" * 64,
                "entity_candidates": [],
                "glossary_candidates": [],
                "unresolved_referents": [],
            }
        ),
        encoding="utf-8",
    )
    local_paths["dry"].mkdir(parents=True, exist_ok=True)
    (local_paths["dry"] / "dry_report.json").write_text(
        canonical_json({"envelope_hash": "6" * 64}), encoding="utf-8"
    )
    (local_paths["dry"] / "request.json").write_text(
        canonical_json(
            {
                "messages": [{"role": "user", "content": "local"}],
                "request_fingerprint": "7" * 64,
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "pipeline.literary.chapter_cycle_live_executor_v1.build_identity_conflict_manifest",
        lambda *_args, **_kwargs: {
            "components": [{"component_id": "local_component_1"}],
            "glossary_review": {"candidate_cards": []},
        },
    )
    monkeypatch.setattr(
        "pipeline.literary.chapter_cycle_live_executor_v1.normalize_source_boundary_violations",
        _fake_local_normalize,
    )
    monkeypatch.setattr(
        "pipeline.literary.chapter_cycle_live_executor_v1.validate_and_apply_conflict_response",
        _fake_local_apply,
    )
    local_result = executor._execute_local_auditor(local_stage, _Permit())  # type: ignore[arg-type]
    assert local_result.status == "accepted"

    claim_stage = ChapterCycleStage(
        stage_id="ch002_stable_claim_components",
        chapter_id="fixture_ch01",
        chapter_ordinal=2,
        stage_name="stable_claim_components",
        stage_role="stable_claim_auditor",
        requires_api=True,
        is_chapter_checkpoint=False,
        stage_descriptor_hash="8" * 64,
    )
    claim_paths = executor._paths(claim_stage)
    claim_index = {
        "claim_components": [
            {"component_id": "claim_component_1", "overflow": False}
        ]
    }
    claim_request = {
        "messages": [{"role": "user", "content": "claim"}],
        "response_schema": {
            "type": "object",
            "properties": {
                "component_id": {"type": "string"},
                "ticket_actions": {"type": "array"},
            },
            "required": ["component_id", "ticket_actions"],
            "additionalProperties": False,
        },
        "request_fingerprint": "9" * 64,
    }
    (claim_paths["claim_prepared"] / "components" / "claim_component_1").mkdir(
        parents=True, exist_ok=True
    )
    (claim_paths["claim_prepared"] / "ticket_index.json").write_text(
        canonical_json(claim_index), encoding="utf-8"
    )
    (
        claim_paths["claim_prepared"]
        / "components"
        / "claim_component_1"
        / "request.json"
    ).write_text(canonical_json(claim_request), encoding="utf-8")
    monkeypatch.setattr(
        "pipeline.literary.chapter_cycle_live_executor_v1.verify_prior_claim_ticket_index_v1",
        lambda _row: claim_index,
    )
    monkeypatch.setattr(
        "pipeline.literary.chapter_cycle_live_executor_v1.validate_prior_claim_response_v1",
        _fake_claim_validator,
    )
    claim_result = executor._execute_claim_components(claim_stage, _Permit())  # type: ignore[arg-type]
    assert claim_result.status == "accepted"

    identity_stage = ChapterCycleStage(
        stage_id="ch002_identity_components",
        chapter_id="fixture_ch01",
        chapter_ordinal=2,
        stage_name="identity_components",
        stage_role="identity_auditor",
        requires_api=True,
        is_chapter_checkpoint=False,
        stage_descriptor_hash="a" * 64,
    )
    identity_paths = executor._paths(identity_stage)
    identity_index = {
        "components": [
            {
                "component_id": "identity_component_1",
                "overflow": False,
                "trigger_state": "ready",
            }
        ]
    }
    identity_request = {
        "messages": [{"role": "user", "content": "identity"}],
        "response_schema": {
            "type": "object",
            "properties": {
                "component_id": {"type": "string"},
                "candidate_actions": {"type": "array"},
                "surface_scope_actions": {"type": "array"},
            },
            "required": [
                "component_id",
                "candidate_actions",
                "surface_scope_actions",
            ],
            "additionalProperties": False,
        },
        "request_fingerprint": "b" * 64,
    }
    (
        identity_paths["identity_prepared"]
        / "components"
        / "identity_component_1"
    ).mkdir(parents=True, exist_ok=True)
    (identity_paths["identity_prepared"] / "identity_index.json").write_text(
        canonical_json(identity_index), encoding="utf-8"
    )
    (
        identity_paths["identity_prepared"]
        / "components"
        / "identity_component_1"
        / "request.json"
    ).write_text(canonical_json(identity_request), encoding="utf-8")
    monkeypatch.setattr(
        "pipeline.literary.chapter_cycle_live_executor_v1.verify_incremental_identity_index_v1",
        lambda _row: identity_index,
    )
    monkeypatch.setattr(
        "pipeline.literary.chapter_cycle_live_executor_v1.validate_incremental_identity_response_v1",
        _fake_identity_validator,
    )
    monkeypatch.setattr(
        "pipeline.literary.chapter_cycle_live_executor_v1.normalize_surface_scope_action_coverage_v1",
        lambda raw, **_kwargs: (
            dict(raw),
            [
                {
                    "normalization_kind": "non_surface_action_ignored",
                    "component_id": "identity_component_1",
                    "review_item_id": "identity_review_1",
                    "disputed_field": "identity_membership",
                    "original_action": "keep_pending",
                    "normalized_action": None,
                }
            ],
        ),
    )
    identity_result = executor._execute_identity_components(  # type: ignore[arg-type]
        identity_stage, _Permit()
    )
    assert identity_result.status == "accepted"
    normalizations = json.loads(
        (
            identity_paths["identity_component_live"]
            / "identity_component_1"
            / "surface_scope_normalizations.json"
        ).read_text(encoding="utf-8")
    )
    assert normalizations["normalization_count"] == 1
    assert normalizations["normalizations"][0]["disputed_field"] == (
        "identity_membership"
    )

    assert runtime.roles == [
        "literary.audit.local_conflict",
        "literary.audit.stable_claim",
        "literary.audit.identity_surface",
    ]
