from __future__ import annotations

import copy
import inspect
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from pipeline.eval.contracts_v1 import ContractValidationError
from pipeline.eval.llm_profiles_v1 import (
    EVALUATION_LLM_ROLE_IDS,
    build_evaluation_llm_profile_v1,
)
from pipeline.eval.evaluation_workflow_settings_v1 import (
    EVALUATION_CHAPTER_IDS_V1,
)
from pipeline.eval.workflow_runtime_bundle_v1 import (
    WorkflowScoringBaselineTemplateSourcesV1,
    materialize_workflow_scoring_runtime_bundle_v1,
)
from pipeline.eval.workflow_runtime_factory_v1 import (
    EvaluationServerRuntimeConfigV1,
    build_evaluation_executor_runtime_v1,
    build_evaluation_runtime_object_registry_v1,
    prepare_evaluation_production_runtime_v1,
    prepare_evaluation_runtime_bundle_v1,
    register_evaluation_baseline_template_v1,
)
from pipeline.llm_backend import (
    MappingCredentialProvider,
    canonical_sha256,
    credential_commitment,
)
from pipeline.tests.test_evaluation_benchmark_runner_v1 import _Predictor
from pipeline.tests.test_evaluation_llm_adapter_v1 import (
    _capability,
    _source,
    _target,
)
from pipeline.tests.test_evaluation_method_executors_v1 import _SemanticSender
from pipeline.tests.test_evaluation_workflow_runtime_bundle_v1 import (
    CHAPTER_ID,
    COMMIT,
    COMPONENT_RUN_ID,
    JOB_ID,
    NOW,
    SOURCE_BINDING_SHA256,
    WORKFLOW_RUN_ID,
    _fixture,
)


def _baseline_registration(
    tmp_path: Path,
    *,
    selected_scorer_ids: tuple[str, ...] = ("sf_qe",),
):
    fixture = _fixture(
        tmp_path / "fixture",
        canonical=True,
        selected_scorer_ids=selected_scorer_ids,
    )
    external_rows = fixture["handoff"]["translation_inputs"][2:]
    external_paths = {
        row["translation_artifact"]["artifact_ref"]: fixture[
            "artifact_sources"
        ].handoff_artifacts[row["translation_artifact"]["artifact_ref"]]
        for row in external_rows
    }
    registered = register_evaluation_baseline_template_v1(
        tmp_path / "job",
        job_id=JOB_ID,
        source_binding_sha256=SOURCE_BINDING_SHA256,
        supported_chapter_ids=EVALUATION_CHAPTER_IDS_V1,
        template_id="five-arm-template-fixture-v1",
        created_at=NOW,
        producer_code_commit=COMMIT,
        settings_option_id=fixture["registration"].settings_option_id,
        registered_option_sha256=fixture[
            "registration"
        ].registered_option_sha256,
        evaluation_profile_id=fixture["registration"].evaluation_profile_id,
        evaluation_profile_ref=fixture["registration"].evaluation_profile_ref,
        policy_profile_id=fixture["registration"].policy_profile_id,
        policy_profile_ref=fixture["registration"].policy_profile_ref,
        shared_selection_ref=fixture["registration"].shared_selection_ref,
        settings_authority=fixture["registration"].settings_authority,
        artifact_sources=WorkflowScoringBaselineTemplateSourcesV1(
            external_translation_inputs=external_rows,
            external_translation_artifacts=external_paths,
            authority_artifacts=fixture[
                "artifact_sources"
            ].authority_artifacts,
        ),
        caveats=("Fixture-only accepted external baselines.",),
    )
    return fixture, registered


def test_registers_missing_workflow_runtime_without_scanning_and_reuses_exactly(
    tmp_path: Path,
) -> None:
    fixture, first = _baseline_registration(tmp_path)

    assert first.workflow_runtime_path.name == "workflow_runtime_v1.json"
    runtime = json.loads(first.workflow_runtime_path.read_text("utf-8"))
    assert runtime["baseline_bundle"]["arm_ids"] == [
        "community",
        "google_nmt",
        "llm_lc",
    ]
    assert runtime["supported_chapter_ids"] == list(EVALUATION_CHAPTER_IDS_V1)
    first_bytes = first.workflow_runtime_path.read_bytes()

    external_rows = fixture["handoff"]["translation_inputs"][2:]
    second = register_evaluation_baseline_template_v1(
        tmp_path / "job",
        job_id=JOB_ID,
        source_binding_sha256=SOURCE_BINDING_SHA256,
        supported_chapter_ids=EVALUATION_CHAPTER_IDS_V1,
        template_id="five-arm-template-fixture-v1",
        created_at=NOW,
        producer_code_commit=COMMIT,
        settings_option_id=fixture["registration"].settings_option_id,
        registered_option_sha256=fixture[
            "registration"
        ].registered_option_sha256,
        evaluation_profile_id=fixture["registration"].evaluation_profile_id,
        evaluation_profile_ref=fixture["registration"].evaluation_profile_ref,
        policy_profile_id=None,
        policy_profile_ref=None,
        shared_selection_ref=fixture["registration"].shared_selection_ref,
        settings_authority=fixture["registration"].settings_authority,
        artifact_sources=WorkflowScoringBaselineTemplateSourcesV1(
            external_translation_inputs=external_rows,
            external_translation_artifacts={
                row["translation_artifact"]["artifact_ref"]: fixture[
                    "artifact_sources"
                ].handoff_artifacts[row["translation_artifact"]["artifact_ref"]]
                for row in external_rows
            },
            authority_artifacts=fixture[
                "artifact_sources"
            ].authority_artifacts,
        ),
        caveats=("Fixture-only accepted external baselines.",),
    )
    assert second.workflow_runtime_path.read_bytes() == first_bytes


def test_registration_rejects_foreign_or_partial_baseline_bytes(
    tmp_path: Path,
) -> None:
    fixture = _fixture(tmp_path / "fixture", canonical=True)
    external_rows = fixture["handoff"]["translation_inputs"][2:]
    external_paths = {
        row["translation_artifact"]["artifact_ref"]: fixture[
            "artifact_sources"
        ].handoff_artifacts[row["translation_artifact"]["artifact_ref"]]
        for row in external_rows
    }
    llm_ref = external_rows[2]["translation_artifact"]["artifact_ref"]
    partial = tmp_path / "technical_book_vi_partial.md"
    partial.write_text("[[B0001]]\npartial evidence only\n", encoding="utf-8")
    external_paths[llm_ref] = partial

    with pytest.raises(ContractValidationError, match="artifact_hash"):
        register_evaluation_baseline_template_v1(
            tmp_path / "job",
            job_id=JOB_ID,
            source_binding_sha256=SOURCE_BINDING_SHA256,
            supported_chapter_ids=EVALUATION_CHAPTER_IDS_V1,
            template_id="five-arm-template-fixture-v1",
            created_at=NOW,
            producer_code_commit=COMMIT,
            settings_option_id=fixture["registration"].settings_option_id,
            registered_option_sha256=fixture[
                "registration"
            ].registered_option_sha256,
            evaluation_profile_id=fixture[
                "registration"
            ].evaluation_profile_id,
            evaluation_profile_ref=fixture[
                "registration"
            ].evaluation_profile_ref,
            policy_profile_id=None,
            policy_profile_ref=None,
            shared_selection_ref=fixture[
                "registration"
            ].shared_selection_ref,
            settings_authority=fixture["registration"].settings_authority,
            artifact_sources=WorkflowScoringBaselineTemplateSourcesV1(
                external_translation_inputs=external_rows,
                external_translation_artifacts=external_paths,
                authority_artifacts=fixture[
                    "artifact_sources"
                ].authority_artifacts,
            ),
        )


def test_prepares_run_specific_bundle_and_resume_reuses_exact_settings(
    tmp_path: Path,
) -> None:
    fixture, _registered = _baseline_registration(tmp_path)
    prepared = prepare_evaluation_runtime_bundle_v1(
        job_root=tmp_path / "job",
        expected_job_id=JOB_ID,
        expected_source_binding_sha256=SOURCE_BINDING_SHA256,
        selected_chapter_ids=(CHAPTER_ID,),
        scoring_handoff_path=fixture["artifact_sources"].scoring_handoff,
        locked_selection=fixture["selection"],
        workflow_run_id=WORKFLOW_RUN_ID,
        component_run_id=COMPONENT_RUN_ID,
        evaluation_output_root=fixture["output_root"],
        runtime_bundle_root=tmp_path / "run-bundle",
        generated_at=NOW,
        producer_code_commit=COMMIT,
        evaluation_logical_run_id="evaluation_runtime_fixture",
        evaluation_attempt_run_id="evaluation_runtime_fixture_attempt",
        artifact_sources=fixture["artifact_sources"],
        arm_presentations=fixture["arm_presentations"],
        method_presentations=fixture["method_presentations"],
        chapter_runtime_bindings=fixture["chapter_runtime_bindings"],
    )
    first_bytes = prepared.bundle_path.read_bytes()
    resumed = prepare_evaluation_runtime_bundle_v1(
        job_root=tmp_path / "job",
        expected_job_id=JOB_ID,
        expected_source_binding_sha256=SOURCE_BINDING_SHA256,
        selected_chapter_ids=(CHAPTER_ID,),
        scoring_handoff_path=fixture["artifact_sources"].scoring_handoff,
        locked_selection=fixture["selection"],
        workflow_run_id=WORKFLOW_RUN_ID,
        component_run_id=COMPONENT_RUN_ID,
        evaluation_output_root=fixture["output_root"],
        runtime_bundle_root=tmp_path / "run-bundle",
        generated_at=NOW,
        producer_code_commit=COMMIT,
        evaluation_logical_run_id="evaluation_runtime_fixture",
        evaluation_attempt_run_id="evaluation_runtime_fixture_attempt",
        artifact_sources=fixture["artifact_sources"],
        arm_presentations=fixture["arm_presentations"],
        method_presentations=fixture["method_presentations"],
        chapter_runtime_bindings=fixture["chapter_runtime_bindings"],
    )
    assert resumed.bundle_path.read_bytes() == first_bytes
    assert (
        resumed.loaded_runtime.workflow_settings["settings_sha256"]
        == prepared.loaded_runtime.workflow_settings["settings_sha256"]
    )


def _producer_handoff_paths(fixture: dict) -> dict[str, Path]:
    handoff = fixture["handoff"]
    refs = [
        row["binding"]["artifact_ref"]
        for row in handoff["source_package_bindings"]
    ]
    refs.extend(
        row["translation_artifact"]["artifact_ref"]
        for row in handoff["translation_inputs"]
        if row["arm_id"] in {"s0", "s1"}
    )
    return {
        ref: fixture["artifact_sources"].handoff_artifacts[ref]
        for ref in refs
    }


def test_production_prepare_builds_all_evaluation_owned_inputs_and_executor(
    tmp_path: Path,
) -> None:
    fixture, _registered = _baseline_registration(
        tmp_path,
        selected_scorer_ids=("sf_qe", "sf_bt", "pj"),
    )
    runtime, sender, secret = _server_runtime()

    result = prepare_evaluation_production_runtime_v1(
        job_root=tmp_path / "job",
        expected_job_id=JOB_ID,
        expected_source_binding_sha256=SOURCE_BINDING_SHA256,
        scoring_handoff_path=fixture["artifact_sources"].scoring_handoff,
        producer_handoff_artifacts=_producer_handoff_paths(fixture),
        locked_selection=fixture["selection"],
        workflow_run_id=WORKFLOW_RUN_ID,
        component_run_id=COMPONENT_RUN_ID,
        evaluation_output_root=fixture["output_root"],
        runtime_bundle_root=tmp_path / "production-bundle",
        generated_at=NOW,
        producer_code_commit=COMMIT,
        evaluation_logical_run_id="evaluation_runtime_fixture",
        evaluation_attempt_run_id="evaluation_runtime_fixture_attempt",
        server_runtime=runtime,
        caveats=("Production preparation fixture.",),
    )

    assert result.prepared_bundle.loaded_runtime.workflow_settings[
        "settings_sha256"
    ] == fixture["settings"]["settings_sha256"]
    assert result.executor_runtime.workflow_settings[
        "settings_sha256"
    ] == fixture["settings"]["settings_sha256"]
    assert sender.calls == 0
    config = json.loads(
        (
            result.prepared_inputs_root
            / "chapter_configs"
            / f"{CHAPTER_ID}.json"
        ).read_text("utf-8")
    )
    assert {row["method_id"] for row in config["methods"]} == {
        "sf_qe",
        "sf_bt",
        "pj",
    }
    assert len(config["comparison_pairs"]) == 10
    assert len(
        list((result.prepared_inputs_root / "overlays").rglob("*.json"))
    ) == 5
    all_written = b"".join(
        path.read_bytes()
        for path in fixture["output_root"].rglob("*")
        if path.is_file()
    )
    assert secret.encode("utf-8") not in all_written
    signature = inspect.signature(prepare_evaluation_production_runtime_v1)
    assert "artifact_sources" not in signature.parameters
    assert "benchmark_manifest" not in signature.parameters
    assert "chapter_configs" not in signature.parameters


def test_production_prepare_resume_is_byte_stable_and_reuses_runtime_identity(
    tmp_path: Path,
) -> None:
    fixture, _registered = _baseline_registration(
        tmp_path,
        selected_scorer_ids=("sf_qe", "sf_bt", "pj"),
    )
    runtime, sender, _secret = _server_runtime()
    kwargs = {
        "job_root": tmp_path / "job",
        "expected_job_id": JOB_ID,
        "expected_source_binding_sha256": SOURCE_BINDING_SHA256,
        "scoring_handoff_path": fixture["artifact_sources"].scoring_handoff,
        "producer_handoff_artifacts": _producer_handoff_paths(fixture),
        "locked_selection": fixture["selection"],
        "workflow_run_id": WORKFLOW_RUN_ID,
        "component_run_id": COMPONENT_RUN_ID,
        "evaluation_output_root": fixture["output_root"],
        "runtime_bundle_root": tmp_path / "production-bundle",
        "generated_at": NOW,
        "producer_code_commit": COMMIT,
        "evaluation_logical_run_id": "evaluation_runtime_fixture",
        "evaluation_attempt_run_id": "evaluation_runtime_fixture_attempt",
        "server_runtime": runtime,
    }
    first = prepare_evaluation_production_runtime_v1(**kwargs)
    bundle_bytes = first.prepared_bundle.bundle_path.read_bytes()
    identity_bytes = (
        first.executor_runtime.runtime_registry.runtime_identity_path.read_bytes()
    )

    second = prepare_evaluation_production_runtime_v1(**kwargs)

    assert second.prepared_bundle.bundle_path.read_bytes() == bundle_bytes
    assert (
        second.executor_runtime.runtime_registry.runtime_identity_path.read_bytes()
        == identity_bytes
    )
    assert sender.calls == 0


def test_production_prepare_rejects_missing_or_foreign_producer_path_before_calls(
    tmp_path: Path,
) -> None:
    fixture, _registered = _baseline_registration(
        tmp_path,
        selected_scorer_ids=("sf_qe", "sf_bt", "pj"),
    )
    runtime, sender, _secret = _server_runtime()
    producer_paths = _producer_handoff_paths(fixture)
    producer_paths.pop("source/package_seal.json")

    with pytest.raises(ContractValidationError, match="source_map_exact_cover"):
        prepare_evaluation_production_runtime_v1(
            job_root=tmp_path / "job",
            expected_job_id=JOB_ID,
            expected_source_binding_sha256=SOURCE_BINDING_SHA256,
            scoring_handoff_path=fixture["artifact_sources"].scoring_handoff,
            producer_handoff_artifacts=producer_paths,
            locked_selection=fixture["selection"],
            workflow_run_id=WORKFLOW_RUN_ID,
            component_run_id=COMPONENT_RUN_ID,
            evaluation_output_root=fixture["output_root"],
            runtime_bundle_root=tmp_path / "production-bundle",
            generated_at=NOW,
            producer_code_commit=COMMIT,
            evaluation_logical_run_id="evaluation_runtime_fixture",
            evaluation_attempt_run_id="evaluation_runtime_fixture_attempt",
            server_runtime=runtime,
        )
    assert sender.calls == 0


def _server_runtime() -> tuple[EvaluationServerRuntimeConfigV1, _SemanticSender, str]:
    secret = "evaluation-test-secret"
    source = _source("google_genai_generate_content")
    source["credential_commitment"] = credential_commitment(secret)
    capabilities = [
        _capability(role_id, source, native=False)
        for role_id in sorted(EVALUATION_LLM_ROLE_IDS)
    ]
    by_role = {
        role_id: capability
        for role_id, capability in zip(
            sorted(EVALUATION_LLM_ROLE_IDS), capabilities, strict=True
        )
    }
    profile = build_evaluation_llm_profile_v1(
        primary_targets={
            role_id: _target(source, by_role[role_id])
            for role_id in sorted(EVALUATION_LLM_ROLE_IDS)
        },
        structured_output_mode="prompt_validated",
    )
    sender = _SemanticSender()
    return (
        EvaluationServerRuntimeConfigV1(
            local_sf_qe_predictor=_Predictor(0.5),
            local_sf_qe_checkpoint_sha256="7" * 64,
            local_sf_qe_package_name="unbabel-comet",
            local_sf_qe_package_version="2.2.7",
            local_sf_qe_device="cpu",
            local_sf_qe_batch_size=8,
            llm_profile=profile,
            api_sources=(source,),
            capability_evidence=capabilities,
            credential_provider=MappingCredentialProvider(
                {source["credential_ref"]: secret}
            ),
            sender=sender,
            cache_mode="read_write",
            clock=lambda: datetime(2026, 7, 24, tzinfo=timezone.utc),
            monotonic=lambda: 1.0,
        ),
        sender,
        secret,
    )


def test_builds_local_and_shared_runtime_registry_without_serializing_secret(
    tmp_path: Path,
) -> None:
    fixture = _fixture(
        tmp_path / "fixture",
        canonical=True,
        selected_scorer_ids=("sf_qe", "sf_bt", "pj"),
    )
    bundle_path = materialize_workflow_scoring_runtime_bundle_v1(
        tmp_path / "bundle",
        registration=fixture["registration"],
        artifact_sources=fixture["artifact_sources"],
        arm_presentations=fixture["arm_presentations"],
        method_presentations=fixture["method_presentations"],
        chapter_runtime_bindings=fixture["chapter_runtime_bindings"],
    )
    runtime, sender, secret = _server_runtime()

    first = build_evaluation_runtime_object_registry_v1(
        bundle_path,
        evaluation_output_root=fixture["output_root"],
        server_runtime=runtime,
    )
    assert set(first.registry.local_sf_qe_runtimes) == {
        "local_sf_qe.fixture.v1"
    }
    assert set(first.registry.llm_role_runners) == {"llm_roles.fixture.v1"}
    assert set(first.registry.shared_ledgers) == {"shared_ledger.fixture.v1"}
    assert sender.calls == 0
    assert secret.encode("utf-8") not in first.runtime_identity_path.read_bytes()
    ledger = first.registry.shared_ledgers["shared_ledger.fixture.v1"]
    assert ledger.path == (
        fixture["output_root"]
        / "chapters"
        / f"00_{CHAPTER_ID}"
        / "usage"
        / "attempt_ledger.sqlite3"
    ).resolve()

    second = build_evaluation_runtime_object_registry_v1(
        bundle_path,
        evaluation_output_root=fixture["output_root"],
        server_runtime=runtime,
    )
    assert second.runtime_identity == first.runtime_identity
    assert second.registry.shared_ledgers[
        "shared_ledger.fixture.v1"
    ].path == ledger.path
    assert sender.calls == 0

    built_executor = build_evaluation_executor_runtime_v1(
        bundle_path,
        evaluation_output_root=fixture["output_root"],
        server_runtime=runtime,
    )
    assert (
        built_executor.workflow_settings["settings_sha256"]
        == fixture["settings"]["settings_sha256"]
    )
    assert built_executor.scoring_handoff == fixture["handoff"]
    assert sender.calls == 0


def test_runtime_resume_rejects_profile_identity_drift_before_provider_call(
    tmp_path: Path,
) -> None:
    fixture = _fixture(
        tmp_path / "fixture",
        canonical=True,
        selected_scorer_ids=("sf_qe", "sf_bt", "pj"),
    )
    bundle_path = materialize_workflow_scoring_runtime_bundle_v1(
        tmp_path / "bundle",
        registration=fixture["registration"],
        artifact_sources=fixture["artifact_sources"],
        arm_presentations=fixture["arm_presentations"],
        method_presentations=fixture["method_presentations"],
        chapter_runtime_bindings=fixture["chapter_runtime_bindings"],
    )
    runtime, sender, _secret = _server_runtime()
    build_evaluation_runtime_object_registry_v1(
        bundle_path,
        evaluation_output_root=fixture["output_root"],
        server_runtime=runtime,
    )
    drifted_profile = copy.deepcopy(runtime.llm_profile)
    assert drifted_profile is not None
    drifted_profile["profile_revision"] = "resume-drift-v2"
    drifted = EvaluationServerRuntimeConfigV1(
        local_sf_qe_predictor=runtime.local_sf_qe_predictor,
        local_sf_qe_checkpoint_sha256=runtime.local_sf_qe_checkpoint_sha256,
        local_sf_qe_package_name=runtime.local_sf_qe_package_name,
        local_sf_qe_package_version=runtime.local_sf_qe_package_version,
        local_sf_qe_device=runtime.local_sf_qe_device,
        local_sf_qe_batch_size=runtime.local_sf_qe_batch_size,
        llm_profile=drifted_profile,
        api_sources=runtime.api_sources,
        capability_evidence=runtime.capability_evidence,
        credential_provider=runtime.credential_provider,
        sender=runtime.sender,
        cache_mode=runtime.cache_mode,
        clock=runtime.clock,
        monotonic=runtime.monotonic,
    )

    with pytest.raises(ContractValidationError, match="artifact_collision"):
        build_evaluation_runtime_object_registry_v1(
            bundle_path,
            evaluation_output_root=fixture["output_root"],
            server_runtime=drifted,
        )
    assert sender.calls == 0


def test_runtime_rejects_unsealed_source_or_capability_expansion(
    tmp_path: Path,
) -> None:
    fixture = _fixture(
        tmp_path / "fixture",
        canonical=True,
        selected_scorer_ids=("sf_qe", "sf_bt", "pj"),
    )
    bundle_path = materialize_workflow_scoring_runtime_bundle_v1(
        tmp_path / "bundle",
        registration=fixture["registration"],
        artifact_sources=fixture["artifact_sources"],
        arm_presentations=fixture["arm_presentations"],
        method_presentations=fixture["method_presentations"],
        chapter_runtime_bindings=fixture["chapter_runtime_bindings"],
    )
    runtime, sender, _secret = _server_runtime()
    extra_source = copy.deepcopy(runtime.api_sources[0])
    extra_source["source_id"] = "foreign-source"
    extra_source["source_revision"] = "foreign-v1"
    expanded = EvaluationServerRuntimeConfigV1(
        local_sf_qe_predictor=runtime.local_sf_qe_predictor,
        local_sf_qe_checkpoint_sha256=runtime.local_sf_qe_checkpoint_sha256,
        local_sf_qe_package_name=runtime.local_sf_qe_package_name,
        local_sf_qe_package_version=runtime.local_sf_qe_package_version,
        local_sf_qe_device=runtime.local_sf_qe_device,
        local_sf_qe_batch_size=runtime.local_sf_qe_batch_size,
        llm_profile=runtime.llm_profile,
        api_sources=(*runtime.api_sources, extra_source),
        capability_evidence=runtime.capability_evidence,
        credential_provider=runtime.credential_provider,
        sender=runtime.sender,
        cache_mode=runtime.cache_mode,
    )

    with pytest.raises(
        ContractValidationError, match="runtime_source_exact_cover"
    ):
        build_evaluation_runtime_object_registry_v1(
            bundle_path,
            evaluation_output_root=fixture["output_root"],
            server_runtime=expanded,
        )
    assert sender.calls == 0
