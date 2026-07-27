from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path

import pytest

from pipeline.llm_backend import (
    ContentAddressedArtifactStore,
    MappingCredentialProvider,
    PhysicalQuotaScheduler,
    RawTransportResponse,
    SharedLlmAttemptLedger,
    SharedLlmCapabilityProbe,
    canonical_json,
    credential_commitment,
)
from pipeline.literary.b1_cross_chapter_auditor_live_v1 import (
    B1CrossChapterAuditorLiveError,
    CROSS_CHAPTER_MODEL_REF_FIELDS_V1,
    IDENTITY_ROUTE,
    build_live_hearing_plan_v1,
    make_hearing_semantic_validator_v1,
)
from pipeline.literary.b1_cross_chapter_decision_ledger_v1 import (
    append_cross_chapter_decisions_v1,
    empty_decision_ledger_v1,
)
from pipeline.literary.checkpoint import canonical_hash
from pipeline.literary.model_ref_v1 import (
    model_ref_instruction_v1,
    project_id_fields,
    project_model_request_v1,
    resolve_model_response_v1,
)
from pipeline.literary.modelapi_b1_cross_chapter_auditor_capability_probe_v1 import (
    DESIGN_DOC,
    RUNTIME_PROFILE_PATH,
    build_probe_plan_v1,
    execute_probe_once_v1,
    synthetic_persistent_response_v1,
)
from pipeline.literary.shared_runtime_profile_v2 import (
    load_literary_shared_runtime_profile_v2,
)
from pipeline.scripts.run_literary_b1_cross_chapter_auditor_live_v1 import (
    _run_hearings,
)
from pipeline.tests.test_literary_b1_chapter_registry_writer_v1 import _sealed
from pipeline.tests.test_literary_b1_cross_chapter_audit_bridge_v1 import (
    MODEL_CONTRACT,
    SOURCE_BLOCKS,
    _dry_run,
    _identity_component,
    _seal_component,
    _seal_queue,
)


SECRET = "synthetic-cross-chapter-auditor-secret"


class _JsonSender:
    def __init__(self, payload: dict) -> None:
        self.payload = payload
        self.calls = 0
        self.request_bodies: list[dict] = []

    def send(self, request) -> RawTransportResponse:
        self.calls += 1
        self.request_bodies.append(json.loads(request.body.decode("utf-8")))
        body = canonical_json(
            {
                "id": f"cross-chapter-fake-{self.calls}",
                "model": "gpt-5.4",
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {"content": canonical_json(self.payload)},
                    }
                ],
                "usage": {
                    "prompt_tokens": 500,
                    "completion_tokens": 120,
                    "total_tokens": 620,
                    "prompt_tokens_details": {"cached_tokens": 0},
                    "completion_tokens_details": {"reasoning_tokens": 0},
                },
            }
        ).encode("utf-8")
        return RawTransportResponse(
            status_code=200,
            headers={"x-request-id": f"cross-chapter-fake-{self.calls}"},
            body=body,
            request_id=f"cross-chapter-fake-{self.calls}",
        )


def _runtime():
    return load_literary_shared_runtime_profile_v2(
        RUNTIME_PROFILE_PATH,
        expected_role_ids={
            "literary.audit.identity_surface",
            "literary.audit.stable_claim",
        },
    )


def _fixture() -> tuple[dict, dict, list[dict], dict[str, str]]:
    registry = _sealed()
    waiting = _identity_component(
        prior_card_id="b0ent_waiting_card",
        lifecycle_state="waiting_for_enrichment",
        current_dossier_snapshots=[],
    )
    ready = _identity_component()
    queue = _seal_queue(
        [ready, waiting],
        chapter_id=registry["chapter_id"],
        registry_hash=registry["registry_hash"],
    )
    prepared = _dry_run(queue)["prepared_requests"]
    return registry, queue, prepared, deepcopy(SOURCE_BLOCKS)


def _plan(
    registry: dict,
    queue: dict,
    prepared: list[dict],
    blocks: dict[str, str],
    *,
    batch_index: int | None = None,
):
    return build_live_hearing_plan_v1(
        queue=queue,
        registry=registry,
        prepared_requests=prepared,
        source_blocks=blocks,
        design_doc=DESIGN_DOC,
        runtime=_runtime(),
        batch_index=batch_index,
    )


def _persistent_identity_response(component_id: str) -> dict:
    return {
        "component_id": component_id,
        "verdict": "confirmed_distinct",
        "merge_target_prior_card_id": None,
        "field_adjudications": [],
        "evidence": [
            {
                "block_id": "bk_ch01_b012",
                "quote": "Above the gate the carving read 'Rowan Aldercote' with an old date.",
            }
        ],
        "reason": "The written occurrence and the living participant are distinct referents.",
        "resolution_condition": None,
    }


def _model_payload(request: dict, persistent: dict) -> dict:
    projected, ref_map = project_model_request_v1(
        request,
        field_names_by_namespace=CROSS_CHAPTER_MODEL_REF_FIELDS_V1,
        instruction=model_ref_instruction_v1(),
    )
    assert projected["model_reference_mode"] == "classified_request_local_v1"
    return project_id_fields(
        persistent,
        ref_map=ref_map,
        field_names_by_namespace=CROSS_CHAPTER_MODEL_REF_FIELDS_V1,
    )


def _write_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(canonical_json(value) + "\n", encoding="utf-8")


def _capability_root(tmp_path: Path) -> Path:
    plan = build_probe_plan_v1(
        route=IDENTITY_ROUTE,
        probe_run_id="literary_cross_chapter_probe_test",
        credential_commitment_sha256=credential_commitment(SECRET),
        issued_at_utc="2026-07-22T00:00:00Z",
        implementation_binding={
            "shared_core_revision": "1" * 40,
            "consumer_revision": "2" * 40,
            "consumer_implementation_sha256": "3" * 64,
        },
    )
    sender = _JsonSender(
        _model_payload(
            dict(plan.request),
            synthetic_persistent_response_v1(IDENTITY_ROUTE),
        )
    )
    probe = SharedLlmCapabilityProbe(
        credential_provider=MappingCredentialProvider(
            {"credential.modelapi_shared_v1": SECRET}
        ),
        scheduler=PhysicalQuotaScheduler(tmp_path / "probe-locks"),
        ledger=SharedLlmAttemptLedger(tmp_path / "probe-ledger.sqlite3"),
        artifact_store=ContentAddressedArtifactStore(tmp_path / "probe-artifacts"),
        sender=sender,
        implementation_binding=plan.implementation_binding,
    )
    result = execute_probe_once_v1(probe=probe, plan=plan)
    assert result["status"] == "qualified"
    root = tmp_path / "capability"
    _write_json(root / "capability_evidence.json", result["capability_evidence"])
    return root


def _materialize_inputs(tmp_path: Path):
    registry, queue, prepared, blocks = _fixture()
    registry_path = tmp_path / "registry.json"
    queue_path = tmp_path / "queue.json"
    prepared_dir = tmp_path / "prepared"
    chapter_path = tmp_path / "chapter.json"
    _write_json(registry_path, registry)
    _write_json(queue_path, queue)
    for row in prepared:
        _write_json(prepared_dir / f"{row['component_id']}.json", row)
    _write_json(
        chapter_path,
        {
            "chapter_id": registry["chapter_id"],
            "blocks": [
                {"block_id": block_id, "text": text}
                for block_id, text in sorted(blocks.items())
            ],
        },
    )
    return registry, queue, prepared, blocks, registry_path, queue_path, prepared_dir, chapter_path


def test_plan_exactly_covers_ready_and_keeps_waiting_out_of_transport() -> None:
    registry, queue, prepared, blocks = _fixture()
    plan = _plan(registry, queue, prepared, blocks)

    assert len(plan.hearings) == 1
    assert plan.hearings[0].route == IDENTITY_ROUTE
    assert plan.hearings[0].token_preflight.fits_prompt_cap is True
    assert [row["lifecycle_state"] for row in plan.waiting_components] == [
        "waiting_for_enrichment"
    ]


def test_plan_projects_within_chapter_merge_refs_before_transport() -> None:
    registry = _sealed()
    component = _identity_component()
    component_body = deepcopy(component)
    component_body.pop("component_id")
    component_body["current_card_snapshots"][0][
        "within_chapter_identity_merge"
    ] = {
        "representative_source_ref": "scan:b1obs_current02",
        "member_source_refs": [
            "scan:b1obs_current02",
            "scan:b1obs_current03",
        ],
        "source_component_ids": ["b1lac_merge_component"],
        "authority_scope": "chapter_only",
        "identity_authority_granted": False,
        "book_authority_granted": False,
    }
    component = _seal_component(component_body)
    queue = _seal_queue(
        [component],
        chapter_id=registry["chapter_id"],
        registry_hash=registry["registry_hash"],
    )
    prepared = _dry_run(queue)["prepared_requests"]

    plan = _plan(registry, queue, prepared, deepcopy(SOURCE_BLOCKS))

    assert len(plan.hearings) == 1
    assert plan.hearings[0].token_preflight.fits_prompt_cap is True
    projected, _ref_map = project_model_request_v1(
        plan.hearings[0].live_request,
        instruction=model_ref_instruction_v1(),
    )
    payload = json.loads(projected["messages"][1]["content"])
    merge = payload["allowlisted_sections"]["current_card_snapshots"][0][
        "within_chapter_identity_merge"
    ]
    assert merge["representative_source_ref"].startswith("O")
    assert all(row.startswith("O") for row in merge["member_source_refs"])


def test_plan_projects_address_form_counterpart_entity_before_transport() -> None:
    registry = _sealed()
    component = _identity_component()
    component_body = deepcopy(component)
    component_body.pop("component_id")
    component_body["current_card_snapshots"][0]["address_forms_used"] = [
        {
            "counterpart_entity_id": "b0ent_counterpart_person",
            "form": "sir",
            "mode": "to",
            "anchor_block_ids": ["bk_ch01_b012"],
        }
    ]
    component = _seal_component(component_body)
    queue = _seal_queue(
        [component],
        chapter_id=registry["chapter_id"],
        registry_hash=registry["registry_hash"],
    )
    prepared = _dry_run(queue)["prepared_requests"]

    plan = _plan(registry, queue, prepared, deepcopy(SOURCE_BLOCKS))
    projected, _ref_map = project_model_request_v1(
        plan.hearings[0].live_request,
        field_names_by_namespace=CROSS_CHAPTER_MODEL_REF_FIELDS_V1,
        instruction=model_ref_instruction_v1(),
    )
    payload = json.loads(projected["messages"][1]["content"])
    counterpart = payload["allowlisted_sections"]["current_card_snapshots"][0][
        "address_forms_used"
    ][0]["counterpart_entity_id"]

    assert counterpart.startswith("E")
    assert "b0ent_counterpart_person" not in canonical_json(projected["messages"])


def test_hearing_transport_round_trips_merge_target_prior_card_id() -> None:
    plan = build_probe_plan_v1(
        route=IDENTITY_ROUTE,
        probe_run_id="literary_cross_chapter_merge_target_transport_test",
        credential_commitment_sha256=credential_commitment(SECRET),
        issued_at_utc="2026-07-23T00:00:00Z",
        implementation_binding={
            "shared_core_revision": "1" * 40,
            "consumer_revision": "2" * 40,
            "consumer_implementation_sha256": "3" * 64,
        },
    )
    target = plan.component["prior_card_ids"][0]
    persistent = synthetic_persistent_response_v1(IDENTITY_ROUTE)
    persistent.update(
        {
            "verdict": "merge_referents",
            "merge_target_prior_card_id": target,
            "excluded_prior_card_ids": [],
            "resolution_condition": None,
        }
    )
    persistent["evidence"][0]["supports_excluded_prior_card_ids"] = []

    model_payload = _model_payload(dict(plan.request), persistent)
    assert model_payload["merge_target_prior_card_id"].startswith("E")
    resolved = resolve_model_response_v1(
        plan.request,
        model_payload,
        field_names_by_namespace=CROSS_CHAPTER_MODEL_REF_FIELDS_V1,
    )
    assert resolved["merge_target_prior_card_id"] == target


def test_plan_rejects_missing_or_duplicate_prepared_hearing() -> None:
    registry, queue, prepared, blocks = _fixture()
    with pytest.raises(B1CrossChapterAuditorLiveError, match="exactly cover"):
        _plan(registry, queue, [], blocks)
    with pytest.raises(B1CrossChapterAuditorLiveError, match="prepared twice"):
        _plan(registry, queue, [prepared[0], deepcopy(prepared[0])], blocks)


def test_plan_partitions_ready_hearings_by_sealed_role_call_cap() -> None:
    registry = _sealed()
    components = []
    for index in range(5):
        prior_card_id = f"b0ent_prior_card_{index}"
        prior_snapshot = deepcopy(
            _identity_component()["prior_card_snapshot"]
        )
        prior_snapshot["prior_card_id"] = prior_card_id
        components.append(
            _identity_component(
                prior_card_id=prior_card_id,
                prior_card_snapshot=prior_snapshot,
            )
        )
    queue = _seal_queue(
        components,
        chapter_id=registry["chapter_id"],
        registry_hash=registry["registry_hash"],
    )
    prepared = _dry_run(queue)["prepared_requests"]

    with pytest.raises(B1CrossChapterAuditorLiveError, match="2 sealed batches"):
        _plan(registry, queue, prepared, deepcopy(SOURCE_BLOCKS))

    first = _plan(
        registry,
        queue,
        prepared,
        deepcopy(SOURCE_BLOCKS),
        batch_index=1,
    )
    second = _plan(
        registry,
        queue,
        prepared,
        deepcopy(SOURCE_BLOCKS),
        batch_index=2,
    )
    first_ids = {row.component_id for row in first.hearings}
    second_ids = {row.component_id for row in second.hearings}
    expected_ids = {row["component_id"] for row in components}

    assert first.batch_count == second.batch_count == 2
    assert first.batch_index == 1
    assert second.batch_index == 2
    assert len(first_ids) == 4
    assert len(second_ids) == 1
    assert first_ids.isdisjoint(second_ids)
    assert first_ids | second_ids == expected_ids
    assert set(first.deferred_ready_component_ids) == second_ids
    assert set(second.deferred_ready_component_ids) == first_ids


def test_one_prior_card_cannot_have_two_open_hearings() -> None:
    registry, queue, prepared, blocks = _fixture()
    duplicate = deepcopy(queue["components"][0])
    duplicate.pop("component_id")
    duplicate["continuity_case_id"] = "b1cont_second_open_case"
    duplicate = _seal_component(duplicate)
    queue = _seal_queue(
        [queue["components"][0], duplicate],
        chapter_id=registry["chapter_id"],
        registry_hash=registry["registry_hash"],
    )
    prepared = _dry_run(queue)["prepared_requests"]
    with pytest.raises(B1CrossChapterAuditorLiveError, match="more than one open"):
        _plan(registry, queue, prepared, blocks)


def test_live_consumer_calls_only_ready_component_and_ledger_accepts_decision(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (
        registry,
        queue,
        prepared,
        blocks,
        registry_path,
        queue_path,
        prepared_dir,
        chapter_path,
    ) = _materialize_inputs(tmp_path)
    plan = _plan(registry, queue, prepared, blocks)
    response = _persistent_identity_response(plan.hearings[0].component_id)
    sender = _JsonSender(_model_payload(plan.hearings[0].live_request, response))
    capability_root = _capability_root(tmp_path)
    monkeypatch.setenv("LITERARY_CROSS_CHAPTER_TEST_KEY", SECRET)

    report = _run_hearings(
        output_root=tmp_path / "live",
        prepared_dir=prepared_dir,
        queue_path=queue_path,
        registry_path=registry_path,
        chapter_paths=[chapter_path],
        design_doc=DESIGN_DOC,
        capability_roots={
            IDENTITY_ROUTE: capability_root,
            "stable_claim_auditor": None,
        },
        run_id="literary_cross_chapter_test_run",
        attempt_run_id="literary_cross_chapter_test_attempt",
        credential_env="LITERARY_CROSS_CHAPTER_TEST_KEY",
        credential_file=None,
        scheduler_root=tmp_path / "live-locks",
        current_head="4" * 40,
        sender=sender,
    )

    assert sender.calls == 1
    assert report["chapter_loop_complete"] is True
    assert len(report["waiting_components"]) == 1
    decisions = json.loads(
        (tmp_path / "live" / "validated_decisions.json").read_text(encoding="utf-8")
    )
    assert decisions == [response | {
        "excluded_prior_card_ids": [],
        "identity_authority_granted": False,
        "claim_authority_granted": False,
    }]
    ledger = append_cross_chapter_decisions_v1(
        ledger=empty_decision_ledger_v1(book_id="synthetic_book"),
        decisions=decisions,
        queue=queue,
        registry=registry,
    )
    assert len(ledger["entries"]) == 1
    assert ledger["entries"][0]["verdict"] == "confirmed_distinct"


def test_invalid_component_response_is_quarantined_without_authority(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (
        registry,
        queue,
        prepared,
        blocks,
        registry_path,
        queue_path,
        prepared_dir,
        chapter_path,
    ) = _materialize_inputs(tmp_path)
    plan = _plan(registry, queue, prepared, blocks)
    invalid = _persistent_identity_response(plan.hearings[0].component_id)
    invalid["evidence"][0]["block_id"] = "foreign_b999"
    sender = _JsonSender(_model_payload(plan.hearings[0].live_request, invalid))
    capability_root = _capability_root(tmp_path)
    monkeypatch.setenv("LITERARY_CROSS_CHAPTER_TEST_KEY", SECRET)

    report = _run_hearings(
        output_root=tmp_path / "quarantine",
        prepared_dir=prepared_dir,
        queue_path=queue_path,
        registry_path=registry_path,
        chapter_paths=[chapter_path],
        design_doc=DESIGN_DOC,
        capability_roots={
            IDENTITY_ROUTE: capability_root,
            "stable_claim_auditor": None,
        },
        run_id="literary_cross_chapter_quarantine_run",
        attempt_run_id="literary_cross_chapter_quarantine_attempt",
        credential_env="LITERARY_CROSS_CHAPTER_TEST_KEY",
        credential_file=None,
        scheduler_root=tmp_path / "quarantine-locks",
        current_head="5" * 40,
        sender=sender,
    )

    assert sender.calls == 1
    assert report["chapter_loop_complete"] is False
    assert report["status"] == "semantic_rejected_all"
    assert len(report["quarantined_component_ids"]) == 1
    assert json.loads(
        (tmp_path / "quarantine" / "validated_decisions.json").read_text(
            encoding="utf-8"
        )
    ) == []


def test_live_consumer_recovers_validated_component_and_calls_only_missing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = _sealed()
    first = _identity_component()
    second_snapshot = deepcopy(first["prior_card_snapshot"])
    second_snapshot["prior_card_id"] = "b0ent_second_prior_card"
    second = _identity_component(
        prior_card_id="b0ent_second_prior_card",
        prior_card_snapshot=second_snapshot,
    )
    queue = _seal_queue(
        [first, second],
        chapter_id=registry["chapter_id"],
        registry_hash=registry["registry_hash"],
    )
    prepared = _dry_run(queue)["prepared_requests"]
    blocks = deepcopy(SOURCE_BLOCKS)
    registry_path = tmp_path / "registry.json"
    queue_path = tmp_path / "queue.json"
    prepared_dir = tmp_path / "prepared"
    chapter_path = tmp_path / "chapter.json"
    _write_json(registry_path, registry)
    _write_json(queue_path, queue)
    for row in prepared:
        _write_json(prepared_dir / f"{row['component_id']}.json", row)
    _write_json(
        chapter_path,
        {
            "chapter_id": registry["chapter_id"],
            "blocks": [
                {"block_id": block_id, "text": text}
                for block_id, text in sorted(blocks.items())
            ],
        },
    )
    plan = _plan(registry, queue, prepared, blocks)
    recovered_hearing, missing_hearing = plan.hearings
    recovered_decision = make_hearing_semantic_validator_v1(
        component=recovered_hearing.component,
        rendered_request=recovered_hearing.live_request,
    )(_persistent_identity_response(recovered_hearing.component_id))

    recovery_root = tmp_path / "partial"
    component_dir = (
        recovery_root
        / "components"
        / f"001_{recovered_hearing.component_id}"
    )
    _write_json(component_dir / "validated_decision.json", recovered_decision)
    _write_json(
        component_dir / "component_report.json",
        {
            "schema_version": "literary_b1_cross_chapter_component_report_v1",
            "component_id": recovered_hearing.component_id,
            "review_route": recovered_hearing.route,
            "status": "semantic_accepted",
            "decision_sha256": canonical_hash(recovered_decision),
        },
    )
    _write_json(
        recovery_root / "validated_decisions.json",
        [recovered_decision],
    )
    _write_json(
        recovery_root / "run_report.json",
        {
            "schema_version": "literary_b1_cross_chapter_auditor_report_v1",
            "plan_hash": plan.plan_hash,
            "queue_hash": plan.queue_hash,
            "registry_hash": plan.registry_hash,
            "batch_index": plan.batch_index,
            "batch_count": plan.batch_count,
            "accepted_component_ids": [recovered_hearing.component_id],
        },
    )

    missing_response = _persistent_identity_response(
        missing_hearing.component_id
    )
    sender = _JsonSender(
        _model_payload(missing_hearing.live_request, missing_response)
    )
    capability_root = _capability_root(tmp_path)
    monkeypatch.setenv("LITERARY_CROSS_CHAPTER_TEST_KEY", SECRET)

    report = _run_hearings(
        output_root=tmp_path / "resumed",
        prepared_dir=prepared_dir,
        queue_path=queue_path,
        registry_path=registry_path,
        chapter_paths=[chapter_path],
        design_doc=DESIGN_DOC,
        capability_roots={
            IDENTITY_ROUTE: capability_root,
            "stable_claim_auditor": None,
        },
        run_id="literary_cross_chapter_resume_run",
        attempt_run_id="literary_cross_chapter_resume_attempt",
        recovery_root=recovery_root,
        credential_env="LITERARY_CROSS_CHAPTER_TEST_KEY",
        credential_file=None,
        scheduler_root=tmp_path / "resume-locks",
        current_head="6" * 40,
        sender=sender,
    )

    assert sender.calls == 1
    assert report["selected_batch_complete"] is True
    assert report["provider_call_count"] == 1
    assert report["recovered_component_ids"] == [
        recovered_hearing.component_id
    ]
    assert report["recovered_component_count"] == 1
    assert {
        row["component_id"]
        for row in json.loads(
            (tmp_path / "resumed" / "validated_decisions.json").read_text(
                encoding="utf-8"
            )
        )
    } == {recovered_hearing.component_id, missing_hearing.component_id}


def test_route_validator_rejects_foreign_evidence_block() -> None:
    registry, queue, prepared, blocks = _fixture()
    plan = _plan(registry, queue, prepared, blocks)
    hearing = plan.hearings[0]
    response = _persistent_identity_response(hearing.component_id)
    response["evidence"][0]["block_id"] = "foreign_b999"
    validator = make_hearing_semantic_validator_v1(
        component=hearing.component,
        rendered_request=hearing.live_request,
    )
    with pytest.raises(Exception, match="supplied"):
        validator(response)
