from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping

import pytest

from pipeline.literary.b2_context_v1 import (
    build_b2_windows_v1,
    load_b2_phase_a_profile,
    load_real_b1_run_input_v1,
    render_b2_frame_request_v1,
)
from pipeline.literary.b2_context_v2 import render_b2_interaction_request_v2
from pipeline.literary.b2_context_v3 import (
    render_b2_frame_request_v2,
    render_b2_interaction_request_v3,
)
from pipeline.literary.b2_contract_v1 import normalize_b2_frame_response_v1
from pipeline.literary.b2_contract_v3 import normalize_b2_frame_response_v2
from pipeline.literary.b2_live_canary_v1 import (
    B2LiveCanaryError,
    FROZEN_DB_SHA256,
    authorize_b2_request_for_live_v1,
    build_frame_context_for_window_v1,
    execute_b2_frame_live_v1,
    execute_b2_interactions_live_v1,
    load_b2_canary_profile_v1,
    prepare_b2_ch1_canary_v1,
    _shared_b2_usage_v1,
)
from pipeline.literary.chapter_cycle_shared_runtime_v1 import (
    BACKEND_MODE_LEGACY,
    BACKEND_MODE_SHARED_V1,
    LiterarySharedRunnerBindingsV1,
    build_literary_code_ref_v1,
    capability_binding_key,
)
from pipeline.literary.chapter_prefix_prior_v1 import (
    build_chapter_prefix_prior_bundle_v1,
)
from pipeline.literary.checkpoint import canonical_hash, file_sha256
from pipeline.literary.model_ref_v1 import (
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


RUNTIME_ROOT = Path(__file__).resolve().parents[2]
B2_PROFILE = (
    RUNTIME_ROOT / "pipeline" / "configs" / "literary_b2_phase_a_profile_v1.json"
)
SLIM_CANARY_PROFILE = (
    RUNTIME_ROOT
    / "pipeline"
    / "configs"
    / "literary_b2_ch1_openai_shared_slim_canary_v1.json"
)
SECRET = "synthetic-literary-m2b-secret"


def _local_ref_wire_schema(schema: Mapping[str, Any]) -> dict[str, Any]:
    return project_model_response_schema_v1(schema)


def test_checked_in_slim_canary_profile_is_bounded_and_nonpublishing() -> None:
    profile = load_b2_canary_profile_v1(SLIM_CANARY_PROFILE)

    assert profile.chapter_id == "wh_ch01"
    assert profile.frame_contract_version == "v2"
    assert profile.interaction_contract_version == "v3"
    assert profile.frame_calls == 1
    assert profile.interaction_calls == 2
    assert profile.max_total_calls == 3
    assert profile.max_retries_per_call == 0
    assert profile.safety["production_publish_enabled"] is False


def _write_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(canonical_json(payload), encoding="utf-8")


def _claim(value: str, block_id: str) -> dict[str, Any]:
    return {
        "value": value,
        "basis": "explicit",
        "support_block_ids": [block_id],
        "address_validation_state": "valid",
        "semantic_status": "unreviewed",
    }


def _chapter() -> dict[str, Any]:
    return {
        "chapter_id": "book_ch01",
        "blocks": [
            {
                "block_id": "book_ch01_h001",
                "order_index": 0,
                "block_type": "heading",
                "clean_text": "Chapter One",
            },
            {
                "block_id": "book_ch01_b001",
                "order_index": 1,
                "block_type": "paragraph",
                "clean_text": "Mr. Vale entered North House.",
            },
            {
                "block_id": "book_ch01_b002",
                "order_index": 2,
                "block_type": "dialogue",
                "clean_text": '"Robin, come here," said Mr. Vale.',
            },
        ],
    }


def _audited_inventory() -> dict[str, Any]:
    block_id = "book_ch01_b001"
    entity = {
        "candidate_id": "local_vale_book_ch01",
        "canonical_surface": "Mr. Vale",
        "surface_status": "located",
        "canonical_name_class": "title_plus_name",
        "alternative_names": [],
        "name_locations": [
            {
                "surface": "Mr. Vale",
                "name_class": "title_plus_name",
                "source_block_ids": [block_id],
            }
        ],
        "source_block_ids": [block_id],
        "referent_kind_claim": _claim("person", block_id),
        "referential_gender_claim": _claim("masculine", block_id),
        "identity_summary_draft": "A named visitor associated with the house.",
        "identity_summary_status": "unreviewed",
        "publication_state": "clean_provisional",
        "audit_reasons": [],
        "conflict_status": "no_detected_identity_conflict",
    }
    body = {
        "schema_version": "b0_entity_conflict_auditor_v1",
        "chapter_id": "book_ch01",
        "source_inventory_hash": "inventory_book_ch01",
        "request_fingerprint": "request_book_ch01",
        "conflict_manifest_hash": "manifest_book_ch01",
        "entity_candidates": [entity],
        "pending_entity_candidates": [],
        "closed_entity_candidates": [],
        "glossary_candidates": [],
        "unresolved_referents": [],
        "quarantined_surfaces": [],
        "global_surface_bindings": [],
        "component_decisions": [],
        "conflict_summary": {},
        "source_validation_report": {},
        "production_publish_performed": False,
    }
    return {**body, "conflict_audited_inventory_hash": canonical_hash(body)}


def _fake_b1_run(tmp_path: Path, *, source_head: str) -> Path:
    chapter = _chapter()
    document = {"document_id": "book", "chapters": [chapter]}
    document_path = tmp_path / "document.json"
    _write_json(document_path, document)
    root = tmp_path / "b1"
    prefix = build_chapter_prefix_prior_bundle_v1(
        document=document,
        audited_inventory=_audited_inventory(),
        coverage_through_chapter_id="book_ch01",
    )
    report_path = root / "artifacts" / "chapters" / "ch001" / "chapter_report.json"
    _write_json(report_path.parent / "final_prefix.json", prefix)
    report_body = {
        "chapter_id": "book_ch01",
        "b2_enabled": False,
        "b2_ready": False,
        "prefix_bundle_hash": prefix["prefix_bundle_hash"],
    }
    report = {**report_body, "report_hash": canonical_hash(report_body)}
    _write_json(report_path, report)
    plan_body = {
        "document_path": str(document_path.resolve()),
        "document_sha256": file_sha256(document_path),
        "ordered_chapter_ids": ["book_ch01"],
    }
    plan = {**plan_body, "plan_hash": canonical_hash(plan_body)}
    _write_json(root / "run_plan.json", plan)
    summary_body = {
        "status": "complete",
        "production_publish_performed": False,
        "b2": {"enabled": False},
        "plan_hash": plan["plan_hash"],
        "completed_chapter_ids": ["book_ch01"],
        "chapter_reports": [
            {
                "chapter_id": "book_ch01",
                "path": report_path.relative_to(root).as_posix(),
                "report_hash": report["report_hash"],
            }
        ],
    }
    _write_json(
        root / "run_summary.json",
        {**summary_body, "summary_hash": canonical_hash(summary_body)},
    )
    _write_json(
        root / "stages" / "b1" / "live" / "run_envelope_001.json",
        {"git_head": source_head},
    )
    return root


def _canary_profile(tmp_path: Path, *, slim: bool = False) -> Path:
    config = tmp_path / "config"
    config.mkdir()
    (config / "b2_profile.json").write_bytes(B2_PROFILE.read_bytes())
    _write_json(config / "provider.json", {})
    _write_json(config / "structured_policy.json", {})
    payload = {
        "schema_version": "literary_b2_ch1_canary_profile_v3",
        "profile_id": (
            "literary_b2_m2b_slim_fake_v1"
            if slim
            else "literary_b2_m2b_fake_v1"
        ),
        "b2_profile": "b2_profile.json",
        "provider_profile": "provider.json",
        "structured_output_policy": "structured_policy.json",
        "chapter_id": "book_ch01",
        "role_bindings": {
            "frame": "literary_b2_frame",
            "interaction": "literary_b2_interaction",
        },
        "contract_versions": (
            {"frame": "v2", "interaction": "v3"}
            if slim
            else {"frame": "v1", "interaction": "v2"}
        ),
        "limits": {
            "frame_calls": 1,
            "interaction_calls": 1,
            "exception_calls": 0,
            "max_total_calls": 2,
            "max_retries_per_call": 0,
            "hard_visible_token_cap": 100000,
        },
        "safety": {
            "source_run_may_be_historical": True,
            "certification_claim_allowed": False,
            "semantic_review_action": "persist_and_continue",
            "integrity_failure_action": "halt_before_next_call",
            "provider_fallback_allowed": False,
            "production_publish_enabled": False,
            "stop_after_chapter_id": "book_ch01",
        },
    }
    path = config / "canary.json"
    _write_json(path, payload)
    return path


def _frame_response() -> dict[str, Any]:
    return {
        "schema_version": "literary_b2_frame_response_v1",
        "chapter_id": "book_ch01",
        "chapter_orientation": {
            "chapter_gist": "A visitor enters a house and addresses Robin.",
            "narrative_mode": "third_person_external",
            "setting_surfaces": ["North House"],
        },
        "frame_starts": [
            {
                "start_block_id": "book_ch01_b001",
                "narrator_surface": None,
                "narrator_status": "external_or_authorial",
                "candidate_card_ids": [],
                "story_time_label": "frame_present",
                "boundary_reason": "The chapter opens in an external frame.",
            }
        ],
        "review_requests": [],
    }


def _slim_frame_response() -> dict[str, Any]:
    return {
        "schema_version": "literary_b2_frame_response_v2",
        "chapter_id": "book_ch01",
        "frame_starts": [
            {
                "start_block_id": "book_ch01_b001",
                "narrator_surface": None,
                "narrator_status": "external_or_authorial",
                "candidate_card_ids": [],
                "narrative_mode": "external_narration",
                "boundary_cue_anchor": None,
            }
        ],
        "review_requests": [],
    }


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
            "id": f"fake-b2-m2b-{self.calls}",
            "model": "gpt-5.4",
            "choices": [
                {
                    "finish_reason": "stop",
                    "message": {"content": canonical_json(semantic)},
                }
            ],
            "usage": {
                "prompt_tokens": 200,
                "completion_tokens": 50,
                "total_tokens": 250,
                "prompt_tokens_details": {"cached_tokens": 0},
                "completion_tokens_details": {"reasoning_tokens": 0},
            },
        }
        return RawTransportResponse(
            status_code=200,
            headers={"x-request-id": f"fake-b2-m2b-{self.calls}"},
            body=canonical_json(response).encode("utf-8"),
            request_id=f"fake-b2-m2b-{self.calls}",
        )


def _source() -> dict[str, Any]:
    return {
        "schema_version": "api_source_v1",
        "source_id": "literary_m2b_fake_v1",
        "source_revision": "fake_transport_v1",
        "source_class": "remote_api",
        "adapter_id": "openai_python_v1",
        "protocol": "openai_chat_completions",
        "route_id": "chat_completions_create",
        "endpoint_class": "remote",
        "base_url": "https://provider.invalid/v1",
        "credential_ref": "credential.literary_m2b_fake_v1",
        "credential_commitment": credential_commitment(SECRET),
        "physical_quota_bucket_id": "literary-m2b-fake-v1",
        "enabled": True,
    }


def _capability(
    *, role_id: str, schema: Mapping[str, Any], validator_ref: Mapping[str, str]
) -> dict[str, Any]:
    return {
        "schema_version": "capability_evidence_v1",
        "capability_id": f"{role_id}.fake_so_v1",
        "capability_revision": "fake_transport_v1",
        "source_id": "literary_m2b_fake_v1",
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
        "probe_id": "literary_m2b_fake_probe_v1",
        "evidence_sha256": "e" * 64,
        "observed_at_utc": "2026-07-20T00:00:00Z",
        "verdict": "qualified",
    }


def _runtime(
    tmp_path: Path,
    *,
    frame_schema: Mapping[str, Any],
    interaction_schemas: list[Mapping[str, Any]],
    sender: _QueuedSender,
    frame_contract_version: str = "v1",
    interaction_contract_version: str = "v2",
) -> LiterarySharedRunnerBindingsV1:
    from pipeline.literary.b2_live_canary_v1 import _normalize_interaction_response

    frame_normalizer = (
        normalize_b2_frame_response_v2
        if frame_contract_version == "v2"
        else normalize_b2_frame_response_v1
    )
    frame_ref = build_literary_code_ref_v1(
        identifier="literary.b2.frame.validator",
        revision=frame_contract_version,
        callables=(validate_structured_payload, frame_normalizer),
    )
    interaction_ref = build_literary_code_ref_v1(
        identifier="literary.b2.interaction.validator",
        revision=interaction_contract_version,
        callables=(_normalize_interaction_response,),
    )
    source = _source()
    capabilities: dict[str, Mapping[str, Any]] = {}
    frame_capability = _capability(
        role_id="literary.b2.frame",
        schema=frame_schema,
        validator_ref=bind_model_ref_validator_v1(frame_ref),
    )
    capabilities[
        capability_binding_key("literary.b2.frame", frame_schema)
    ] = frame_capability
    for schema in interaction_schemas:
        capability = _capability(
            role_id="literary.b2.interaction",
            schema=schema,
            validator_ref=bind_model_ref_validator_v1(interaction_ref),
        )
        capabilities[
            capability_binding_key("literary.b2.interaction", schema)
        ] = capability
    store = ContentAddressedArtifactStore(tmp_path / "objects")
    backend = SharedLlmBackend(
        credential_provider=MappingCredentialProvider(
            {"credential.literary_m2b_fake_v1": SECRET}
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
        run_id="literary_m2b_fake_run",
        attempt_run_id="literary_m2b_fake_attempt",
        structured_output={
            "mode": "required",
            "schema_dialect": "json_schema_2020_12",
        },
    )


def test_b2_frame_and_interaction_use_shared_backend_and_reject_mixed_resume(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source_head = "fake-head-m2b"
    b1_root = _fake_b1_run(tmp_path, source_head=source_head)
    canary_path = _canary_profile(tmp_path)
    real_input = load_real_b1_run_input_v1(
        b1_root, current_git_head=source_head
    )
    chapter_row = real_input["chapters"][0]
    chapter = chapter_row["chapter"]
    prefix = chapter_row["prefix_bundle"]
    profile = load_b2_phase_a_profile(canary_path.parent / "b2_profile.json")
    frame_request = authorize_b2_request_for_live_v1(
        render_b2_frame_request_v1(
            chapter=chapter,
            prefix_bundle=prefix,
            profile=profile,
        )
    )
    frame_response = _frame_response()
    frame_artifact = normalize_b2_frame_response_v1(
        request=frame_request, response=frame_response
    )
    windows = build_b2_windows_v1(chapter, profile=profile)
    assert len(windows) == 1
    pending_interaction = render_b2_interaction_request_v2(
        window=windows[0],
        prefix_bundle=prefix,
        profile=profile,
        frame_context=None,
    )
    frame_context = build_frame_context_for_window_v1(
        frame_artifact=frame_artifact, window=windows[0]
    )
    actual_interaction = authorize_b2_request_for_live_v1(
        render_b2_interaction_request_v2(
            window=windows[0],
            prefix_bundle=prefix,
            profile=profile,
            frame_context=frame_context,
        )
    )
    interaction_response = {
        "schema_version": "literary_b2_interaction_response_v2",
        "chapter_id": "book_ch01",
        "window_id": actual_interaction["window_id"],
        "speaker_turns": [],
        "interaction_events": [],
        "review_requests": [],
    }
    sender = _QueuedSender([frame_response, interaction_response])
    runtime = _runtime(
        tmp_path / "shared",
        frame_schema=frame_request["response_schema"],
        interaction_schemas=[
            pending_interaction["response_schema"],
            actual_interaction["response_schema"],
        ],
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
        "pipeline.literary.b2_live_canary_v1.file_sha256", fake_file_sha256
    )

    def forbidden(*_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("legacy B2 credential or transport path was selected")

    monkeypatch.setattr(
        "pipeline.literary.b2_live_canary_v1.resolve_role_credential", forbidden
    )
    monkeypatch.setattr(
        "pipeline.literary.b2_live_canary_v1._call_interaction_model", forbidden
    )
    output = tmp_path / "b2"
    seal = prepare_b2_ch1_canary_v1(
        source_run_root=b1_root,
        output_root=output,
        canary_profile_path=canary_path,
        credential_root=None,
        frozen_db=frozen,
        current_git_head=source_head,
        backend_mode=BACKEND_MODE_SHARED_V1,
        shared_runtime=runtime,
    )
    assert seal["backend_mode"] == BACKEND_MODE_SHARED_V1
    assert seal["schema_version"] == "literary_b2_ch1_canary_seal_v4_shared_backend"

    with pytest.raises(B2LiveCanaryError, match="backend mode differs"):
        execute_b2_frame_live_v1(
            output_root=output,
            credential_root=tmp_path,
            frozen_db=frozen,
            current_git_head=source_head,
            backend_mode=BACKEND_MODE_LEGACY,
        )
    assert sender.calls == 0

    frame = execute_b2_frame_live_v1(
        output_root=output,
        credential_root=None,
        frozen_db=frozen,
        current_git_head=source_head,
        backend_mode=BACKEND_MODE_SHARED_V1,
        shared_runtime=runtime,
    )
    chapter_artifact = execute_b2_interactions_live_v1(
        output_root=output,
        credential_root=None,
        frozen_db=frozen,
        current_git_head=source_head,
        backend_mode=BACKEND_MODE_SHARED_V1,
        shared_runtime=runtime,
    )

    assert sender.calls == 2
    assert frame["chapter_id"] == "book_ch01"
    assert chapter_artifact["active_block_coverage"]["exact_cover"] is True
    assert chapter_artifact["speaker_turns"] == []
    assert chapter_artifact["interaction_events"] == []
    frame_receipt = json.loads(
        (output / "frame" / "shared_attempt_receipt.json").read_text(
            encoding="utf-8"
        )
    )
    interaction_receipts = list(
        (output / "interactions").glob("*/shared_attempt_receipt.json")
    )
    assert frame_receipt["role_id"] == "literary.b2.frame"
    assert len(interaction_receipts) == 1
    assert json.loads(interaction_receipts[0].read_text(encoding="utf-8"))[
        "role_id"
    ] == "literary.b2.interaction"
    assert not list(output.rglob("*.sqlite3"))
    with pytest.raises(B2LiveCanaryError, match="backend mode differs"):
        execute_b2_interactions_live_v1(
            output_root=output,
            credential_root=tmp_path,
            frozen_db=frozen,
            current_git_head=source_head,
            backend_mode=BACKEND_MODE_LEGACY,
        )
    assert sender.calls == 2


def test_b2_slim_frame_and_interaction_complete_through_shared_backend(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source_head = "fake-head-m2b-slim"
    b1_root = _fake_b1_run(tmp_path, source_head=source_head)
    canary_path = _canary_profile(tmp_path, slim=True)
    real_input = load_real_b1_run_input_v1(
        b1_root, current_git_head=source_head
    )
    chapter_row = real_input["chapters"][0]
    chapter = chapter_row["chapter"]
    prefix = chapter_row["prefix_bundle"]
    profile = load_b2_phase_a_profile(canary_path.parent / "b2_profile.json")
    frame_request = authorize_b2_request_for_live_v1(
        render_b2_frame_request_v2(
            chapter=chapter,
            prefix_bundle=prefix,
            profile=profile,
        )
    )
    frame_response = _slim_frame_response()
    frame_artifact = normalize_b2_frame_response_v2(
        request=frame_request, response=frame_response
    )
    windows = build_b2_windows_v1(chapter, profile=profile)
    frame_context = build_frame_context_for_window_v1(
        frame_artifact=frame_artifact, window=windows[0]
    )
    interaction_request = authorize_b2_request_for_live_v1(
        render_b2_interaction_request_v3(
            window=windows[0],
            prefix_bundle=prefix,
            profile=profile,
            frame_context=frame_context,
        )
    )
    interaction_response = {
        "schema_version": "literary_b2_interaction_response_v3",
        "chapter_id": "book_ch01",
        "window_id": interaction_request["window_id"],
        "speaker_turns": [],
        "salient_events": [],
        "review_requests": [],
    }
    sender = _QueuedSender([frame_response, interaction_response])
    runtime = _runtime(
        tmp_path / "shared_slim",
        frame_schema=frame_request["response_schema"],
        interaction_schemas=[interaction_request["response_schema"]],
        sender=sender,
        frame_contract_version="v2",
        interaction_contract_version="v3",
    )
    frozen = tmp_path / "frozen_slim.sqlite3"
    frozen.write_bytes(b"synthetic frozen slim fixture")
    real_hash = file_sha256

    def fake_file_sha256(path: Path) -> str:
        return (
            FROZEN_DB_SHA256
            if Path(path).resolve() == frozen.resolve()
            else real_hash(path)
        )

    monkeypatch.setattr(
        "pipeline.literary.b2_live_canary_v1.file_sha256", fake_file_sha256
    )
    output = tmp_path / "b2_slim"
    prepare_b2_ch1_canary_v1(
        source_run_root=b1_root,
        output_root=output,
        canary_profile_path=canary_path,
        credential_root=None,
        frozen_db=frozen,
        current_git_head=source_head,
        backend_mode=BACKEND_MODE_SHARED_V1,
        shared_runtime=runtime,
    )
    execute_b2_frame_live_v1(
        output_root=output,
        credential_root=None,
        frozen_db=frozen,
        current_git_head=source_head,
        backend_mode=BACKEND_MODE_SHARED_V1,
        shared_runtime=runtime,
    )
    chapter_artifact = execute_b2_interactions_live_v1(
        output_root=output,
        credential_root=None,
        frozen_db=frozen,
        current_git_head=source_head,
        backend_mode=BACKEND_MODE_SHARED_V1,
        shared_runtime=runtime,
    )

    assert sender.calls == 2
    assert chapter_artifact["schema_version"] == (
        "literary_b2_slim_chapter_artifact_v1"
    )
    assert chapter_artifact["speaker_turns"] == []
    assert chapter_artifact["salient_events"] == []
    assert "interaction_events" not in chapter_artifact
    report = json.loads((output / "live_report.json").read_text(encoding="utf-8"))
    assert report["schema_version"] == "literary_b2_slim_canary_report_v1"
    assert report["salient_event_count"] == 0
    assert not list(output.rglob("*.sqlite3"))


def test_shared_b2_unknown_usage_cannot_certify_a_finite_cap(tmp_path: Path) -> None:
    _write_json(
        tmp_path / "shared_attempt_receipt.json",
        {"usage": None},
    )
    with pytest.raises(B2LiveCanaryError, match="usage is unknown"):
        _shared_b2_usage_v1(tmp_path)
