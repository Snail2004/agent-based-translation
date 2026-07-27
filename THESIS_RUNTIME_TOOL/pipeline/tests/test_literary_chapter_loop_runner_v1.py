from __future__ import annotations

import argparse
from copy import deepcopy
import json
from pathlib import Path
from typing import Any

import pytest

from pipeline.literary.b2_live_canary_v1 import load_b2_canary_profile_v1
from pipeline.literary.chapter_cycle_orchestrator_v1 import (
    ApiCallPermit,
    ChapterCycleStage,
    ChapterCycleStagePause,
    StageExecutionResult,
    advance_chapter_cycle_stage_v1,
    initialize_chapter_cycle_run_v1,
    load_chapter_cycle_plan_v1,
    load_chapter_cycle_state_v1,
)
from pipeline.literary.chapter_loop_bindings_v1 import (
    ChapterLoopBindingError,
    MODEL_STAGE_NAMES,
    load_runtime_bindings_v1,
    load_stage_bindings_v1,
)
from pipeline.literary.chapter_loop_current_executor_v1 import (
    LiteraryChapterLoopExecutorError,
    LiteraryChapterLoopExecutorV1,
    _approved_retry_command_change,
    _existing_stage_output_is_complete_v1,
    _recoverable_xchapter_component_count_v1,
    _sealed_b3_request_count_v1,
    _speaker_recovery_expected_calls_v1,
    _serviceable_cases,
    materialize_b2_canary_profile_v1,
    write_chapter_bridge_files_v1,
)
from pipeline.literary.chapter_loop_observability_v1 import (
    LiteraryChapterLoopHistoryV1,
    extract_usage_v1,
)
from pipeline.literary.chapter_source_document_v1 import (
    load_literary_source_document_v1,
)
from pipeline.literary.checkpoint import canonical_hash, file_sha256
from pipeline.scripts import run_literary_chapter_loop_v1 as chapter_loop_runner


RUNTIME_ROOT = Path(__file__).resolve().parents[2]
CONFIG_ROOT = RUNTIME_ROOT / "pipeline" / "configs"
PROFILE = CONFIG_ROOT / "literary_chapter_loop_profile_v1.json"
STAGE_BINDINGS = (
    CONFIG_ROOT / "literary_chapter_loop_stage_bindings_v1.json"
)
B2_LOOP_TEMPLATE = (
    CONFIG_ROOT
    / "literary_b2_chapter_loop_modelapi_gpt54_canary_template_v1.json"
)


def test_b2_loop_profile_materializes_per_chapter_without_mutating_template(
    tmp_path: Path,
) -> None:
    before = B2_LOOP_TEMPLATE.read_bytes()
    ch1_path = materialize_b2_canary_profile_v1(
        template_path=B2_LOOP_TEMPLATE,
        output_path=tmp_path / "ch001.json",
        chapter_id="book_ch01",
        prior_frame_candidate_carry_required=False,
        interaction_call_count=2,
    )
    ch2_path = materialize_b2_canary_profile_v1(
        template_path=B2_LOOP_TEMPLATE,
        output_path=tmp_path / "ch002.json",
        chapter_id="book_ch02",
        prior_frame_candidate_carry_required=True,
        interaction_call_count=4,
    )

    ch1 = load_b2_canary_profile_v1(ch1_path)
    ch2 = load_b2_canary_profile_v1(ch2_path)
    assert ch1.chapter_id == "book_ch01"
    assert ch1.prior_frame_candidate_carry_required is False
    assert ch2.chapter_id == "book_ch02"
    assert ch2.prior_frame_candidate_carry_required is True
    assert ch1.interaction_calls == 2
    assert ch1.max_total_calls == 3
    assert ch2.interaction_calls == 4
    assert ch2.max_total_calls == 5
    assert ch1.b2_profile_path == ch2.b2_profile_path
    assert B2_LOOP_TEMPLATE.read_bytes() == before


def test_b2_loop_profile_can_refresh_only_its_measured_call_limits(
    tmp_path: Path,
) -> None:
    output = materialize_b2_canary_profile_v1(
        template_path=B2_LOOP_TEMPLATE,
        output_path=tmp_path / "ch002.json",
        chapter_id="book_ch02",
        prior_frame_candidate_carry_required=True,
        interaction_call_count=2,
    )

    refreshed = materialize_b2_canary_profile_v1(
        template_path=B2_LOOP_TEMPLATE,
        output_path=output,
        chapter_id="book_ch02",
        prior_frame_candidate_carry_required=True,
        interaction_call_count=4,
    )

    profile = load_b2_canary_profile_v1(refreshed)
    assert profile.interaction_calls == 4
    assert profile.max_total_calls == 5


def test_b2_loop_profile_rejects_materialized_drift(tmp_path: Path) -> None:
    output = materialize_b2_canary_profile_v1(
        template_path=B2_LOOP_TEMPLATE,
        output_path=tmp_path / "ch001.json",
        chapter_id="book_ch01",
        prior_frame_candidate_carry_required=False,
    )
    payload = json.loads(output.read_text(encoding="utf-8"))
    payload["chapter_id"] = "foreign_chapter"
    _write_json(output, payload)

    with pytest.raises(ChapterCycleStagePause, match="profile drifted"):
        materialize_b2_canary_profile_v1(
            template_path=B2_LOOP_TEMPLATE,
            output_path=output,
            chapter_id="book_ch01",
            prior_frame_candidate_carry_required=False,
        )


def test_b2_loop_profile_content_addresses_same_named_dependencies(
    tmp_path: Path,
) -> None:
    first_root = tmp_path / "first"
    second_root = tmp_path / "second"
    output_root = tmp_path / "materialized"
    source_profile = load_b2_canary_profile_v1(B2_LOOP_TEMPLATE)
    for root in (first_root, second_root):
        root.mkdir()
        (root / B2_LOOP_TEMPLATE.name).write_bytes(B2_LOOP_TEMPLATE.read_bytes())
        for source in (
            source_profile.b2_profile_path,
            source_profile.provider_profile_path,
            source_profile.structured_output_policy_path,
        ):
            if source is not None:
                (root / source.name).write_bytes(source.read_bytes())

    second_phase_path = second_root / source_profile.b2_profile_path.name
    second_phase = json.loads(second_phase_path.read_text(encoding="utf-8"))
    second_phase["profile_id"] = "same_name_higher_capacity"
    second_phase["context_caps"]["frame_candidate_card_cap"] = 160
    _write_json(second_phase_path, second_phase)

    first = load_b2_canary_profile_v1(
        materialize_b2_canary_profile_v1(
            template_path=first_root / B2_LOOP_TEMPLATE.name,
            output_path=output_root / "ch001.json",
            chapter_id="book_ch01",
            prior_frame_candidate_carry_required=False,
        )
    )
    second = load_b2_canary_profile_v1(
        materialize_b2_canary_profile_v1(
            template_path=second_root / B2_LOOP_TEMPLATE.name,
            output_path=output_root / "ch002.json",
            chapter_id="book_ch02",
            prior_frame_candidate_carry_required=True,
        )
    )

    assert first.b2_profile_path != second.b2_profile_path
    assert first.b2_profile_path.exists()
    assert second.b2_profile_path.exists()
    assert first.b2_profile_path.name.startswith(
        f"{source_profile.b2_profile_path.stem}_"
    )
    assert second.b2_profile_path.name.startswith(
        f"{source_profile.b2_profile_path.stem}_"
    )


def _document(chapter_count: int) -> dict[str, Any]:
    return {
        "document_id": "generic-literary-fixture",
        "chapters": [
            {
                "chapter_id": f"book_ch{ordinal:02d}",
                "blocks": [
                    {
                        "block_id": f"book_ch{ordinal:02d}_b001",
                        "order_index": 1,
                        "block_type": "paragraph",
                        "clean_text": f"Chapter {ordinal} source.",
                    }
                ],
            }
            for ordinal in range(1, chapter_count + 1)
        ],
    }


def _write_json(path: Path, value: Any) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return path


def _write_json_with_bom(path: Path, value: Any) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8-sig",
    )
    return path


def _runtime_profile(
    tmp_path: Path,
    *,
    source_id: str = "modelapi-fixture-v1",
    model_id: str = "gpt-fixture",
    name: str = "runtime_profile.json",
) -> Path:
    return _write_json(
        tmp_path / name,
        {
            "sources": [{"source_id": source_id}],
            "roles": [{"requested_model_id": model_id}],
        },
    )


def _runtime_binding(
    tmp_path: Path,
    *,
    source_id: str = "modelapi-fixture-v1",
    b1_enrich_runtime_profile: Path | None = None,
) -> Path:
    evidence = _write_json(
        tmp_path / "capability_evidence.json",
        {
            "verdict": "qualified",
            "source_id": source_id,
            "requested_model_id": "gpt-fixture",
            "observed_model_id": "gpt-fixture",
        },
    )
    capability_names = {
        "b1_scan": ("default",),
        "b1_enrich": ("default",),
        "b1_local_auditor": ("default",),
        "xchapter_hearing": ("identity", "stable_claim"),
        "b2_frame_interaction": ("frame", "interaction"),
        "speaker_recovery": ("default",),
        "b3_temporal": ("default",),
        "b3_auditor": ("default",),
        "b0_summary": ("default",),
    }
    assert set(capability_names) == MODEL_STAGE_NAMES
    return _write_json(
        tmp_path / "runtime_bindings.json",
        {
            "schema_version": "literary_chapter_loop_runtime_bindings_v1",
            "binding_id": "fixture-runtime-binding",
            "stages": {
                stage: {
                    "runtime_profile": (
                        str(b1_enrich_runtime_profile)
                        if stage == "b1_enrich"
                        and b1_enrich_runtime_profile is not None
                        else None
                    ),
                    "context_profile": None,
                    "capabilities": {
                        name: str(evidence) for name in names
                    },
                    "source_id": source_id,
                    "model_id": "gpt-fixture",
                }
                for stage, names in capability_names.items()
            },
        },
    )


def _initialized_executor(
    tmp_path: Path, *, chapter_count: int
) -> tuple[Path, LiteraryChapterLoopExecutorV1, LiteraryChapterLoopHistoryV1]:
    document = _document(chapter_count)
    document_path = _write_json(tmp_path / "document.json", document)
    frozen_db = tmp_path / "memory.sqlite3"
    frozen_db.write_bytes(b"sealed-offline-fixture")
    run_root = tmp_path / "run"
    initialize_chapter_cycle_run_v1(
        run_root=run_root,
        document_path=document_path,
        profile_path=PROFILE,
        frozen_db_path=frozen_db,
        ordered_chapter_ids=[
            f"book_ch{ordinal:02d}" for ordinal in range(1, chapter_count + 1)
        ],
        stop_after_chapter_count=min(chapter_count, 10),
    )
    write_chapter_bridge_files_v1(
        run_root=run_root,
        document=document,
        ordered_chapter_ids=[
            f"book_ch{ordinal:02d}" for ordinal in range(1, chapter_count + 1)
        ],
    )
    plan = load_chapter_cycle_plan_v1(run_root)
    history = LiteraryChapterLoopHistoryV1(run_root=run_root, run_id="fixture_run")
    history.initialize(
        plan_hash=plan["plan_hash"],
        selected_chapter_ids=plan["ordered_chapter_ids"],
        code_revision="fixture-head",
        binding_hash="fixture-binding",
        runtime_binding_hash="fixture-runtime",
    )
    executor = LiteraryChapterLoopExecutorV1(
        run_root=run_root,
        plan=plan,
        stage_bindings=load_stage_bindings_v1(STAGE_BINDINGS),
        runtime_bindings=load_runtime_bindings_v1(_runtime_binding(tmp_path)),
        credential_file=None,
        scheduler_root=None,
        history=history,
    )
    return run_root, executor, history


def _write_stage_outputs(
    root: Path, *, outputs: tuple[str, ...], marker: str
) -> Path:
    for name in outputs:
        path = root / name
        if name in {"prepared_requests", "overlays"}:
            path.mkdir(parents=True, exist_ok=True)
            _write_json(path / "fixture.json", {"marker": marker})
        else:
            _write_json(path, {"marker": marker, "output": name})
    return root


def _write_accepted_stage_fixture(
    *,
    run_root: Path,
    executor: LiteraryChapterLoopExecutorV1,
    ordinal: int,
    stage_name: str,
) -> Path:
    stage_id = f"ch{ordinal:03d}_{stage_name}"
    binding = executor.bindings[stage_name]
    source_root = _write_stage_outputs(
        run_root / "artifacts" / "chapters" / f"ch{ordinal:03d}" / stage_name,
        outputs=binding.outputs,
        marker=f"source:{stage_id}",
    )
    result_path = _write_json(
        run_root / "stages" / stage_id / "stage_result.json",
        {
            "stage_descriptor": {
                "stage_id": stage_id,
                "stage_name": stage_name,
            },
            "status": "accepted",
            "payload": {"output_root": str(source_root)},
        },
    )
    _write_json(
        run_root / "receipts" / f"{stage_id}.json",
        {
            "stage_id": stage_id,
            "status": "accepted",
            "artifact_path": result_path.relative_to(run_root).as_posix(),
            "artifact_sha256": file_sha256(result_path),
        },
    )
    return source_root


def test_stage_binding_table_covers_the_current_graph_and_real_scripts() -> None:
    bindings = load_stage_bindings_v1(STAGE_BINDINGS)

    assert len(bindings) == 16
    assert tuple(bindings)[0] == "b1_scan"
    assert tuple(bindings)[-1] == "checkpoint"
    for binding in bindings.values():
        if binding.script is not None:
            assert (RUNTIME_ROOT / binding.script).is_file()


def test_model_cli_commands_scope_credentials_to_the_parser_that_owns_them(
    tmp_path: Path,
) -> None:
    _, executor, _ = _initialized_executor(tmp_path, chapter_count=1)
    credential = tmp_path / "gateway.txt"
    credential.write_text("fixture-secret", encoding="utf-8")
    scheduler = tmp_path / "scheduler"
    scheduler.mkdir()
    executor.credential_file = credential
    executor.scheduler_root = scheduler

    scan_stage = _stage(
        next(
            row
            for row in executor.plan["stage_plan"]
            if row["stage_name"] == "b1_scan"
        )
    )
    scan_command = executor.build_command(
        scan_stage,
        {
            "capability_root": tmp_path / "scan-capability",
            "document": executor.document_path,
            "prior_cards": None,
            "previous_summary_root": None,
        },
        tmp_path / "scan-output",
        1,
    )
    assert scan_command[2] == "canary"
    assert scan_command.index("--credential-file") > scan_command.index("canary")

    b2_stage = _stage(
        next(
            row
            for row in executor.plan["stage_plan"]
            if row["stage_name"] == "b2_frame_interaction"
        )
    )
    b2_command = executor.build_command(
        b2_stage,
        {
            "source_run_root": tmp_path / "b2-input",
            "frame_capability_root": tmp_path / "frame-capability",
            "interaction_capability_root": tmp_path / "interaction-capability",
            "canary_profile": tmp_path / "b2-canary.json",
            "frozen_db": executor.frozen_db,
            "prior_b2_root": None,
        },
        tmp_path / "b2-output",
        1,
    )
    assert b2_command.index("--credential-file") < b2_command.index("run")

    routing_stage = _stage(
        next(
            row
            for row in executor.plan["stage_plan"]
            if row["stage_name"] == "b2_review_routing"
        )
    )
    routing_command = executor.build_command(
        routing_stage,
        {
            "b2_root": tmp_path / "b2-output",
            "b2_input_root": tmp_path / "b2-input",
            "registry_root": tmp_path / "registry",
            "local_audit_root": tmp_path / "local-audit",
            "hearing_queue_root": tmp_path / "hearing-queue",
            "decided_cross_component_ids": ["hearing_a", "hearing_b"],
        },
        tmp_path / "routing-output",
        0,
    )
    assert routing_command.count("--decided-cross-component-id") == 2
    first_decided = routing_command.index("--decided-cross-component-id")
    assert routing_command[first_decided + 1] == "hearing_a"
    assert routing_command[-2:] == ["--decided-cross-component-id", "hearing_b"]


def test_b3_apply_command_carries_prior_component_catalogs(tmp_path: Path) -> None:
    _, executor, _ = _initialized_executor(tmp_path, chapter_count=2)
    stage = _stage(
        next(
            row
            for row in executor.plan["stage_plan"]
            if row["stage_id"] == "ch002_b3_apply"
        )
    )
    first_catalog = tmp_path / "ch001" / "component_catalog.json"
    second_catalog = tmp_path / "ch001-revision" / "component_catalog.json"

    command = executor.build_command(
        stage,
        {
            "b3_root": tmp_path / "ch002-b3",
            "overlays": [],
            "prior_component_catalogs": [first_catalog, second_catalog],
            "reconciled_projection": tmp_path / "identity-projection.json",
        },
        tmp_path / "ch002-b3-apply",
        0,
    )

    catalog_indexes = [
        index
        for index, value in enumerate(command)
        if value == "--component-catalog"
    ]
    assert [command[index + 1] for index in catalog_indexes] == [
        str(first_catalog),
        str(second_catalog),
    ]


def test_dry_run_is_generic_for_ten_chapters_and_calls_no_provider(
    tmp_path: Path,
) -> None:
    _, executor, _ = _initialized_executor(tmp_path, chapter_count=10)

    report = executor.dry_run_plan()

    assert report["provider_calls"] == 0
    assert report["chapters"] == [
        f"book_ch{ordinal:02d}" for ordinal in range(1, 11)
    ]
    assert report["totals"]["stage_count"] == 160
    assert report["stages"][0]["stage_name"] == "b1_scan"
    assert report["stages"][15]["stage_name"] == "checkpoint"
    assert report["stages"][16]["chapter_id"] == "book_ch02"
    assert all(
        row["runtime_binding"]["source_match"] is True
        for row in report["stages"]
        if row["api"]
    )


def test_windows_bom_inputs_load_without_changing_the_contract(
    tmp_path: Path,
) -> None:
    document_path = _write_json_with_bom(
        tmp_path / "document.json", _document(1)
    )
    frozen_db = tmp_path / "memory.sqlite3"
    frozen_db.write_bytes(b"sealed-offline-fixture")
    run_root = tmp_path / "run"

    initialize_chapter_cycle_run_v1(
        run_root=run_root,
        document_path=document_path,
        profile_path=PROFILE,
        frozen_db_path=frozen_db,
        ordered_chapter_ids=["book_ch01"],
        stop_after_chapter_count=1,
    )
    assert load_literary_source_document_v1(document_path)["document_id"] == (
        "generic-literary-fixture"
    )

    runtime_path = _runtime_binding(tmp_path)
    runtime_payload = json.loads(runtime_path.read_text(encoding="utf-8"))
    _write_json_with_bom(runtime_path, runtime_payload)
    assert load_runtime_bindings_v1(runtime_path).binding_id == (
        "fixture-runtime-binding"
    )


def test_missing_producer_receipt_halts_instead_of_guessing_a_path(
    tmp_path: Path,
) -> None:
    _, executor, _ = _initialized_executor(tmp_path, chapter_count=1)
    enrich = executor.plan["stage_plan"][1]
    stage = _stage(enrich)

    with pytest.raises(ChapterCycleStagePause, match="lacks required input"):
        executor.resolve_inputs(stage, strict=True)


def test_incomplete_stage_output_is_archived_outside_replay_before_retry(
    tmp_path: Path,
) -> None:
    run_root, executor, _ = _initialized_executor(tmp_path, chapter_count=1)
    stage = _stage(executor.plan["stage_plan"][1])
    output_root = executor.stage_output_root(stage)
    _write_json(output_root / "request.json", {"attempt": 1})
    shared_root = output_root.with_name(f"{output_root.name}-shared")
    _write_json(shared_root / "transport.json", {"attempt": 1})

    archive = executor._archive_incomplete_stage_output(stage, output_root)

    assert not output_root.exists()
    assert archive.parent == (
        run_root / "operational_attempts" / "ch001_b1_enrich"
    )
    assert json.loads(
        (archive / "output" / "request.json").read_text(encoding="utf-8")
    ) == {
        "attempt": 1
    }
    assert json.loads(
        (archive / "shared_output" / "transport.json").read_text(
            encoding="utf-8"
        )
    ) == {"attempt": 1}
    metadata = json.loads(
        (archive / "incomplete_attempt.json").read_text(encoding="utf-8")
    )
    assert metadata["stage_id"] == "ch001_b1_enrich"
    assert metadata["replay_visible"] is False


def test_xchapter_remaining_call_count_uses_only_bound_validated_components(
    tmp_path: Path,
) -> None:
    root = tmp_path / "xchapter"
    accepted = ["b1xhear_first", "b1xhear_second"]
    decisions = [{"component_id": component_id} for component_id in accepted]
    _write_json(
        root / "run_report.json",
        {
            "schema_version": "literary_b1_cross_chapter_auditor_report_v1",
            "accepted_component_ids": accepted,
        },
    )
    _write_json(root / "validated_decisions.json", decisions)
    for index, component_id in enumerate(accepted, start=1):
        component = root / "components" / f"{index:03d}_{component_id}"
        _write_json(component / "validated_decision.json", decisions[index - 1])
        _write_json(component / "component_report.json", {"ok": True})

    assert _recoverable_xchapter_component_count_v1(
        root,
        ready_count=5,
    ) == 2

    (root / "components" / "002_b1xhear_second" / "component_report.json").unlink()
    assert _recoverable_xchapter_component_count_v1(
        root,
        ready_count=5,
    ) == 0


def test_runtime_capability_override_is_scoped_and_hash_bound(
    tmp_path: Path,
) -> None:
    runtime = load_runtime_bindings_v1(_runtime_binding(tmp_path))
    capability = tmp_path / "b3_auditor_capability"
    _write_json(
        capability / "capability_evidence.json",
        {"verdict": "qualified"},
    )

    overrides = chapter_loop_runner._parse_capability_overrides(
        [f"b3_auditor.default={capability}"],
        runtime_bindings=runtime,
    )
    rebound = chapter_loop_runner._runtime_bindings_with_capability_overrides(
        runtime,
        overrides,
    )

    assert rebound.stages["b3_auditor"].capabilities["default"] == (
        capability.resolve()
    )
    assert rebound.stages["b3_temporal"].capabilities == (
        runtime.stages["b3_temporal"].capabilities
    )
    assert chapter_loop_runner._validated_capability_overrides(
        overrides,
        runtime_bindings=runtime,
    ) == overrides

    _write_json(
        capability / "capability_evidence.json",
        {"verdict": "tampered"},
    )
    with pytest.raises(SystemExit, match="override drifted"):
        chapter_loop_runner._validated_capability_overrides(
            overrides,
            runtime_bindings=runtime,
        )


def test_runtime_capability_override_rejects_unknown_selector(
    tmp_path: Path,
) -> None:
    runtime = load_runtime_bindings_v1(_runtime_binding(tmp_path))
    capability = tmp_path / "capability"
    _write_json(
        capability / "capability_evidence.json",
        {"verdict": "qualified"},
    )

    with pytest.raises(SystemExit, match="unknown capability override"):
        chapter_loop_runner._parse_capability_overrides(
            [f"b3_auditor.foreign={capability}"],
            runtime_bindings=runtime,
        )


def test_runtime_profile_override_is_scoped_and_hash_bound(
    tmp_path: Path,
) -> None:
    original = _runtime_profile(tmp_path, name="original.json")
    replacement = _runtime_profile(tmp_path, name="replacement.json")
    runtime = load_runtime_bindings_v1(
        _runtime_binding(
            tmp_path,
            b1_enrich_runtime_profile=original,
        )
    )

    overrides = chapter_loop_runner._parse_runtime_profile_overrides(
        [f"b1_enrich={replacement}"],
        runtime_bindings=runtime,
    )
    rebound = chapter_loop_runner._runtime_bindings_with_profile_overrides(
        runtime,
        overrides,
    )

    assert rebound.stages["b1_enrich"].runtime_profile == replacement.resolve()
    assert rebound.stages["b1_scan"] == runtime.stages["b1_scan"]
    assert rebound.binding_hash == runtime.binding_hash
    assert chapter_loop_runner._validated_runtime_profile_overrides(
        overrides,
        runtime_bindings=runtime,
    ) == overrides

    _write_json(
        replacement,
        {
            "sources": [{"source_id": "modelapi-fixture-v1"}],
            "roles": [{"requested_model_id": "gpt-fixture"}],
            "tampered": True,
        },
    )
    with pytest.raises(SystemExit, match="override drifted"):
        chapter_loop_runner._validated_runtime_profile_overrides(
            overrides,
            runtime_bindings=runtime,
        )


def test_runtime_profile_override_rejects_source_or_model_drift(
    tmp_path: Path,
) -> None:
    original = _runtime_profile(tmp_path, name="original.json")
    runtime = load_runtime_bindings_v1(
        _runtime_binding(
            tmp_path,
            b1_enrich_runtime_profile=original,
        )
    )
    foreign_source = _runtime_profile(
        tmp_path,
        source_id="foreign-source",
        name="foreign_source.json",
    )
    foreign_model = _runtime_profile(
        tmp_path,
        model_id="foreign-model",
        name="foreign_model.json",
    )

    with pytest.raises(SystemExit, match="source_id differs"):
        chapter_loop_runner._parse_runtime_profile_overrides(
            [f"b1_enrich={foreign_source}"],
            runtime_bindings=runtime,
        )
    with pytest.raises(SystemExit, match="model differs"):
        chapter_loop_runner._parse_runtime_profile_overrides(
            [f"b1_enrich={foreign_model}"],
            runtime_bindings=runtime,
        )


def test_runtime_profile_override_rejects_non_capacity_drift(
    tmp_path: Path,
) -> None:
    original = _runtime_profile(tmp_path, name="original.json")
    runtime = load_runtime_bindings_v1(
        _runtime_binding(
            tmp_path,
            b1_enrich_runtime_profile=original,
        )
    )
    changed = _write_json(
        tmp_path / "changed.json",
        {
            "sources": [{"source_id": "modelapi-fixture-v1"}],
            "roles": [
                {
                    "requested_model_id": "gpt-fixture",
                    "generation": {"temperature": 0},
                }
            ],
        },
    )

    with pytest.raises(SystemExit, match="non-capacity fields"):
        chapter_loop_runner._parse_runtime_profile_overrides(
            [f"b1_enrich={changed}"],
            runtime_bindings=runtime,
        )


def test_runtime_profile_override_allows_only_upward_memory_budget(
    tmp_path: Path,
) -> None:
    original = _write_json(
        tmp_path / "original.json",
        {
            "sources": [{"source_id": "modelapi-fixture-v1"}],
            "roles": [
                {
                    "requested_model_id": "gpt-fixture",
                    "generation": {"memory_token_budget": 12_000},
                }
            ],
        },
    )
    runtime = load_runtime_bindings_v1(
        _runtime_binding(
            tmp_path,
            b1_enrich_runtime_profile=original,
        )
    )
    raised = _write_json(
        tmp_path / "raised.json",
        {
            "sources": [{"source_id": "modelapi-fixture-v1"}],
            "roles": [
                {
                    "requested_model_id": "gpt-fixture",
                    "generation": {"memory_token_budget": 20_000},
                }
            ],
        },
    )
    lowered = _write_json(
        tmp_path / "lowered.json",
        {
            "sources": [{"source_id": "modelapi-fixture-v1"}],
            "roles": [
                {
                    "requested_model_id": "gpt-fixture",
                    "generation": {"memory_token_budget": 8_000},
                }
            ],
        },
    )

    parsed = chapter_loop_runner._parse_runtime_profile_overrides(
        [f"b1_enrich={raised}"],
        runtime_bindings=runtime,
    )
    assert parsed["b1_enrich"]["path"] == str(raised.resolve())
    with pytest.raises(SystemExit, match="capacity is not upward"):
        chapter_loop_runner._parse_runtime_profile_overrides(
            [f"b1_enrich={lowered}"],
            runtime_bindings=runtime,
        )


def test_capacity_override_raises_only_call_budget_and_preserves_plan_shape(
    tmp_path: Path,
) -> None:
    run_root, executor, _ = _initialized_executor(tmp_path, chapter_count=1)
    plan = load_chapter_cycle_plan_v1(run_root)
    current = chapter_loop_runner._validated_capacity_overrides(
        None,
        plan=plan,
    )
    raised = chapter_loop_runner._requested_capacity_overrides(
        [
            "b2=32",
            "b3_temporal=16",
            "local_auditor=64",
        ],
        max_api_calls_per_chapter=128,
        max_api_calls_per_run=4096,
        plan=plan,
        current=current,
    )
    assert raised is not None
    effective = chapter_loop_runner._plan_with_capacity_overrides(
        plan,
        raised,
    )
    assert effective["plan_hash"] == plan["plan_hash"]
    assert effective["stage_plan"] == plan["stage_plan"]
    assert effective["logical_call_caps_by_role"]["b2"] == 32
    assert effective["logical_call_caps_by_role"]["b3_temporal"] == 16
    assert effective["logical_call_caps_by_role"]["local_auditor"] == 64
    assert effective["max_api_calls_per_chapter"] == 128
    assert effective["max_api_calls_per_run"] == 4096
    assert executor.plan["stage_plan"] == plan["stage_plan"]


def test_capacity_override_cannot_lower_an_existing_cap(
    tmp_path: Path,
) -> None:
    run_root, _, _ = _initialized_executor(tmp_path, chapter_count=1)
    plan = load_chapter_cycle_plan_v1(run_root)
    current = chapter_loop_runner._validated_capacity_overrides(
        None,
        plan=plan,
    )
    with pytest.raises(SystemExit, match="can only increase"):
        chapter_loop_runner._requested_capacity_overrides(
            ["b2=1"],
            max_api_calls_per_chapter=None,
            max_api_calls_per_run=None,
            plan=plan,
            current=current,
        )


def test_b3_context_override_changes_only_request_ceiling(
    tmp_path: Path,
) -> None:
    original = CONFIG_ROOT / "literary_b3_temporal_phase_a_v1.json"
    replacement = json.loads(original.read_text(encoding="utf-8"))
    replacement["profile_id"] = "fixture_b3_capacity"
    replacement["batching"]["max_requests_per_chapter"] = 16
    replacement["token_caps"]["prompt_tokens_per_request"] = 24_000
    replacement_path = _write_json(tmp_path / "b3_context.json", replacement)
    runtime = load_runtime_bindings_v1(
        _runtime_binding(
            tmp_path,
        )
    )
    # The fixture binding has no context profile; bind the real role shape so
    # this test exercises the same scoped override used by the live runner.
    stage = runtime.stages["b3_temporal"]
    from pipeline.literary.chapter_loop_bindings_v1 import RuntimeStageBindingV1

    stages = dict(runtime.stages)
    stages["b3_temporal"] = RuntimeStageBindingV1(
        stage_name=stage.stage_name,
        runtime_profile=stage.runtime_profile,
        context_profile=original,
        capabilities=stage.capabilities,
        source_id=stage.source_id,
        model_id=stage.model_id,
    )
    runtime = type(runtime)(
        source_path=runtime.source_path,
        binding_id=runtime.binding_id,
        stages=stages,
        binding_hash=runtime.binding_hash,
    )
    parsed = chapter_loop_runner._parse_context_profile_overrides(
        [f"b3_temporal={replacement_path}"],
        runtime_bindings=runtime,
    )
    rebound = chapter_loop_runner._runtime_bindings_with_context_profile_overrides(
        runtime,
        parsed,
    )
    assert rebound.stages["b3_temporal"].context_profile == replacement_path.resolve()
    assert (
        chapter_loop_runner._context_profile_capacity(replacement)
        == 16
    )
    assert (
        chapter_loop_runner._context_profile_capacity(
            json.loads(original.read_text(encoding="utf-8"))
        )
        == 4
    )
    assert chapter_loop_runner._context_profile_prompt_capacity(replacement) == 24_000


def test_b3_context_override_cannot_lower_prompt_ceiling(tmp_path: Path) -> None:
    original = CONFIG_ROOT / "literary_b3_temporal_phase_a_v1.json"
    replacement = json.loads(original.read_text(encoding="utf-8"))
    replacement["token_caps"]["prompt_tokens_per_request"] = 19_999
    replacement_path = _write_json(tmp_path / "b3_lower_prompt.json", replacement)
    runtime = load_runtime_bindings_v1(_runtime_binding(tmp_path))
    stage = runtime.stages["b3_temporal"]
    from pipeline.literary.chapter_loop_bindings_v1 import RuntimeStageBindingV1

    stages = dict(runtime.stages)
    stages["b3_temporal"] = RuntimeStageBindingV1(
        stage_name=stage.stage_name,
        runtime_profile=stage.runtime_profile,
        context_profile=original,
        capabilities=stage.capabilities,
        source_id=stage.source_id,
        model_id=stage.model_id,
    )
    runtime = type(runtime)(
        source_path=runtime.source_path,
        binding_id=runtime.binding_id,
        stages=stages,
        binding_hash=runtime.binding_hash,
    )
    with pytest.raises(SystemExit, match="lowers prompt ceiling"):
        chapter_loop_runner._parse_context_profile_overrides(
            [f"b3_temporal={replacement_path}"],
            runtime_bindings=runtime,
        )


def test_b2_context_override_changes_only_prompt_ceiling(
    tmp_path: Path,
) -> None:
    original = (
        CONFIG_ROOT
        / "literary_b2_chapter_loop_modelapi_gpt54_canary_template_v1.json"
    )
    replacement = (
        CONFIG_ROOT
        / "literary_b2_chapter_loop_modelapi_gpt54_canary_template_v3_320k_capacity.json"
    )
    runtime = load_runtime_bindings_v1(_runtime_binding(tmp_path))
    stage = runtime.stages["b2_frame_interaction"]
    from pipeline.literary.chapter_loop_bindings_v1 import RuntimeStageBindingV1

    stages = dict(runtime.stages)
    stages["b2_frame_interaction"] = RuntimeStageBindingV1(
        stage_name=stage.stage_name,
        runtime_profile=stage.runtime_profile,
        context_profile=original,
        capabilities=stage.capabilities,
        source_id=stage.source_id,
        model_id=stage.model_id,
    )
    runtime = type(runtime)(
        source_path=runtime.source_path,
        binding_id=runtime.binding_id,
        stages=stages,
        binding_hash=runtime.binding_hash,
    )
    parsed = chapter_loop_runner._parse_context_profile_overrides(
        [f"b2_frame_interaction={replacement}"],
        runtime_bindings=runtime,
    )
    rebound = chapter_loop_runner._runtime_bindings_with_context_profile_overrides(
        runtime,
        parsed,
    )
    assert (
        rebound.stages["b2_frame_interaction"].context_profile
        == replacement.resolve()
    )


def test_b2_context_override_can_raise_candidate_ceiling(tmp_path: Path) -> None:
    original = (
        CONFIG_ROOT
        / "literary_b2_chapter_loop_modelapi_gpt54_canary_template_v3_320k_capacity.json"
    )
    replacement = (
        CONFIG_ROOT
        / "literary_b2_chapter_loop_modelapi_gpt54_canary_template_v4_320k_capacity_160_128.json"
    )
    runtime = load_runtime_bindings_v1(_runtime_binding(tmp_path))
    stage = runtime.stages["b2_frame_interaction"]
    from pipeline.literary.chapter_loop_bindings_v1 import RuntimeStageBindingV1

    stages = dict(runtime.stages)
    stages["b2_frame_interaction"] = RuntimeStageBindingV1(
        stage_name=stage.stage_name,
        runtime_profile=stage.runtime_profile,
        context_profile=original,
        capabilities=stage.capabilities,
        source_id=stage.source_id,
        model_id=stage.model_id,
    )
    runtime = type(runtime)(
        source_path=runtime.source_path,
        binding_id=runtime.binding_id,
        stages=stages,
        binding_hash=runtime.binding_hash,
    )

    parsed = chapter_loop_runner._parse_context_profile_overrides(
        [f"b2_frame_interaction={replacement}"],
        runtime_bindings=runtime,
    )

    assert parsed["b2_frame_interaction"]["path"] == str(replacement.resolve())


def test_b2_context_override_cannot_lower_candidate_ceiling(
    tmp_path: Path,
) -> None:
    original = (
        CONFIG_ROOT
        / "literary_b2_chapter_loop_modelapi_gpt54_canary_template_v3_320k_capacity.json"
    )
    replacement_root = tmp_path / "replacement"
    replacement_root.mkdir()
    original_canary = json.loads(original.read_text(encoding="utf-8"))
    original_phase_path = original.parent / original_canary["b2_profile"]
    replacement_phase = json.loads(
        original_phase_path.read_text(encoding="utf-8")
    )
    replacement_phase["context_caps"]["frame_candidate_card_cap"] = 95
    _write_json(replacement_root / original_phase_path.name, replacement_phase)
    for name in (
        original_canary["provider_profile"],
        original_canary["structured_output_policy"],
    ):
        (replacement_root / name).write_bytes((original.parent / name).read_bytes())
    replacement = _write_json(
        replacement_root / original.name,
        original_canary,
    )
    runtime = load_runtime_bindings_v1(_runtime_binding(tmp_path))
    stage = runtime.stages["b2_frame_interaction"]
    from pipeline.literary.chapter_loop_bindings_v1 import RuntimeStageBindingV1

    stages = dict(runtime.stages)
    stages["b2_frame_interaction"] = RuntimeStageBindingV1(
        stage_name=stage.stage_name,
        runtime_profile=stage.runtime_profile,
        context_profile=original,
        capabilities=stage.capabilities,
        source_id=stage.source_id,
        model_id=stage.model_id,
    )
    runtime = type(runtime)(
        source_path=runtime.source_path,
        binding_id=runtime.binding_id,
        stages=stages,
        binding_hash=runtime.binding_hash,
    )

    with pytest.raises(SystemExit, match="lowers candidate ceiling"):
        chapter_loop_runner._parse_context_profile_overrides(
            [f"b2_frame_interaction={replacement}"],
            runtime_bindings=runtime,
        )


def test_b2_context_override_rejects_lower_aggregate_token_ceiling(
    tmp_path: Path,
) -> None:
    original = (
        CONFIG_ROOT
        / "literary_b2_chapter_loop_modelapi_gpt54_canary_template_v1.json"
    )
    replacement = json.loads(original.read_text(encoding="utf-8"))
    replacement["profile_id"] = "fixture_b2_lower_aggregate_cap"
    replacement["limits"]["hard_visible_token_cap"] = 80000
    replacement_path = _write_json(tmp_path / "b2_context.json", replacement)
    runtime = load_runtime_bindings_v1(_runtime_binding(tmp_path))
    stage = runtime.stages["b2_frame_interaction"]
    from pipeline.literary.chapter_loop_bindings_v1 import RuntimeStageBindingV1

    stages = dict(runtime.stages)
    stages["b2_frame_interaction"] = RuntimeStageBindingV1(
        stage_name=stage.stage_name,
        runtime_profile=stage.runtime_profile,
        context_profile=original,
        capabilities=stage.capabilities,
        source_id=stage.source_id,
        model_id=stage.model_id,
    )
    runtime = type(runtime)(
        source_path=runtime.source_path,
        binding_id=runtime.binding_id,
        stages=stages,
        binding_hash=runtime.binding_hash,
    )
    with pytest.raises(SystemExit, match="lowers aggregate token ceiling"):
        chapter_loop_runner._parse_context_profile_overrides(
            [f"b2_frame_interaction={replacement_path}"],
            runtime_bindings=runtime,
        )


def test_b2_context_override_rejects_non_capacity_drift(
    tmp_path: Path,
) -> None:
    original = (
        CONFIG_ROOT
        / "literary_b2_chapter_loop_modelapi_gpt54_canary_template_v1.json"
    )
    replacement = json.loads(original.read_text(encoding="utf-8"))
    replacement["profile_id"] = "fixture_b2_non_capacity_drift"
    replacement["limits"]["interaction_calls"] = 3
    replacement_path = _write_json(tmp_path / "b2_context.json", replacement)
    runtime = load_runtime_bindings_v1(_runtime_binding(tmp_path))
    stage = runtime.stages["b2_frame_interaction"]
    from pipeline.literary.chapter_loop_bindings_v1 import RuntimeStageBindingV1

    stages = dict(runtime.stages)
    stages["b2_frame_interaction"] = RuntimeStageBindingV1(
        stage_name=stage.stage_name,
        runtime_profile=stage.runtime_profile,
        context_profile=original,
        capabilities=stage.capabilities,
        source_id=stage.source_id,
        model_id=stage.model_id,
    )
    runtime = type(runtime)(
        source_path=runtime.source_path,
        binding_id=runtime.binding_id,
        stages=stages,
        binding_hash=runtime.binding_hash,
    )
    with pytest.raises(SystemExit, match="changes non-capacity fields"):
        chapter_loop_runner._parse_context_profile_overrides(
            [f"b2_frame_interaction={replacement_path}"],
            runtime_bindings=runtime,
        )


def test_b2_failed_stage_can_retry_with_capacity_profile_revision() -> None:
    before = {
        "schema_version": "literary_chapter_loop_command_v1",
        "stage_id": "ch006_b2_frame_interaction",
        "chapter_id": "book_ch06",
        "command": [
            "python",
            "runner.py",
            "--canary-profile",
            "old.json",
            "--source-run-root",
            "source",
        ],
        "resolved_inputs": {
            "canary_profile": "old.json",
            "source_run_root": "source",
        },
        "expected_calls": 3,
        "retry_allowed": False,
        "fallback_allowed": False,
        "command_hash": "old",
    }
    after = json.loads(json.dumps(before))
    after["command"][3] = "new.json"
    after["resolved_inputs"]["canary_profile"] = "new.json"
    after["command_hash"] = "new"

    assert _approved_retry_command_change(before, after) is True

    after["resolved_inputs"]["source_run_root"] = "foreign"
    assert _approved_retry_command_change(before, after) is False


def test_b3_failed_stage_can_retry_with_capacity_profiles_only() -> None:
    before = {
        "schema_version": "literary_chapter_loop_command_v1",
        "stage_id": "ch007_b3_temporal",
        "chapter_id": "book_ch07",
        "command": [
            "python",
            "runner.py",
            "--context-profile",
            "context-20k.json",
            "--runtime-profile",
            "runtime-20k.json",
            "--b2-root",
            "b2-stable",
            "--max-calls",
            "5",
            "--prior-b3-root",
            "prior-stable",
        ],
        "resolved_inputs": {
            "context_profile": "context-20k.json",
            "runtime_profile": "runtime-20k.json",
            "b2_root": "b2-stable",
            "prior_b3_roots": ["prior-stable"],
        },
        "expected_calls": 5,
        "retry_allowed": False,
        "fallback_allowed": False,
        "command_hash": "old",
    }
    after = deepcopy(before)
    after["command"][3] = "context-24k.json"
    after["command"][5] = "runtime-24k.json"
    after["resolved_inputs"]["context_profile"] = "context-24k.json"
    after["resolved_inputs"]["runtime_profile"] = "runtime-24k.json"
    after["command"][after["command"].index("--max-calls") + 1] = "4"
    after["expected_calls"] = 4
    after["command_hash"] = "new"

    assert _approved_retry_command_change(before, after) is True

    changed_input = deepcopy(after)
    changed_input["resolved_inputs"]["b2_root"] = "foreign"
    assert _approved_retry_command_change(before, changed_input) is False


def test_b3_resume_reuses_sealed_request_count(tmp_path: Path) -> None:
    plan_body = {
        "schema_version": "literary_b3_temporal_live_plan_v6",
        "chapter_id": "book_ch07",
        "request_count": 5,
        "batch_membership": [
            {"batch_ordinal": ordinal, "component_ids": [f"c{ordinal}"]}
            for ordinal in range(1, 6)
        ],
    }
    plan = {**plan_body, "plan_hash": canonical_hash(plan_body)}
    _write_json(tmp_path / "live_plan.json", plan)
    seal_body = {
        "schema_version": "literary_b3_temporal_chapter_run_seal_v1",
        "chapter_id": "book_ch07",
        "live_plan_hash": plan["plan_hash"],
    }
    seal = {**seal_body, "seal_hash": canonical_hash(seal_body)}
    _write_json(tmp_path / "run_seal.json", seal)

    assert _sealed_b3_request_count_v1(
        output_root=tmp_path,
        chapter_id="book_ch07",
    ) == 5

    plan["request_count"] = 4
    _write_json(tmp_path / "live_plan.json", plan)
    with pytest.raises(
        LiteraryChapterLoopExecutorError,
        match="live plan seal is invalid",
    ):
        _sealed_b3_request_count_v1(
            output_root=tmp_path,
            chapter_id="book_ch07",
        )


def test_b1_enrich_16k_runtime_profile_matches_chapter_loop_limits() -> None:
    profile_path = (
        CONFIG_ROOT
        / "literary_shared_llm_runtime_modelapi_b1_enrich_v6_32k_16k.json"
    )
    payload = json.loads(profile_path.read_text(encoding="utf-8"))
    role = payload["roles"][0]
    generation = role["generation"]
    limits = role["limits"]

    assert role["role_id"] == "literary.b1.enrich"
    assert role["requested_model_id"] == "gpt-5.4"
    assert generation["max_input_tokens"] == 32000
    assert generation["max_output_tokens"] == 16384
    assert limits["max_prompt_tokens"] == 32000
    assert limits["max_completion_tokens"] == 16384
    assert limits["max_total_tokens"] == 48384


def test_b2_frame_capacity_profiles_preserve_full_chapter_context() -> None:
    phase = json.loads(
        (
            CONFIG_ROOT
            / "literary_b2_slim_phase_a_profile_v5_ch6_capacity.json"
        ).read_text(encoding="utf-8")
    )
    runtime = json.loads(
        (
            CONFIG_ROOT
            / "literary_shared_llm_runtime_modelapi_b2_long_chapter_v4_64k_frame.json"
        ).read_text(encoding="utf-8")
    )
    roles = {row["role_id"]: row for row in runtime["roles"]}

    assert phase["token_caps"]["frame_prompt_tokens"] == 64000
    assert phase["context_caps"]["frame_candidate_card_cap"] == 96
    assert roles["literary.b2.frame"]["generation"]["max_input_tokens"] == 64000
    assert roles["literary.b2.frame"]["limits"]["max_prompt_tokens"] == 64000
    assert roles["literary.b2.interaction"]["generation"]["max_input_tokens"] == 25000


def test_retry_command_may_change_only_an_approved_runtime_binding() -> None:
    before = {
        "schema_version": "literary_chapter_loop_command_v1",
        "stage_id": "ch002_b3_auditor",
        "chapter_id": "book_ch02",
        "command": [
            "python",
            "audit.py",
            "--capability-root",
            "old",
            "--b3-root",
            "stable",
        ],
        "resolved_inputs": {
            "capability_root": "old",
            "b3_root": "stable",
        },
        "expected_calls": 4,
        "retry_allowed": False,
        "fallback_allowed": False,
        "command_hash": "old_hash",
    }
    after = deepcopy(before)
    after["command"][3] = "new"
    after["resolved_inputs"]["capability_root"] = "new"
    after["command_hash"] = "new_hash"

    assert _approved_retry_command_change(before, after) is True

    before["command"][2:2] = ["--runtime-profile", "old-profile"]
    before["resolved_inputs"]["runtime_profile"] = "old-profile"
    after = deepcopy(before)
    after["command"][3] = "new-profile"
    after["resolved_inputs"]["runtime_profile"] = "new-profile"
    after["command_hash"] = "new_profile_hash"

    assert _approved_retry_command_change(before, after) is True

    after["resolved_inputs"]["b3_root"] = "foreign"
    assert _approved_retry_command_change(before, after) is False

    after = deepcopy(before)
    after["expected_calls"] = 3
    after["command_hash"] = "recounted_hash"
    assert _approved_retry_command_change(before, after) is True

    non_auditor_before = deepcopy(before)
    non_auditor_before["stage_id"] = "ch002_b1_enrich"
    non_auditor_after = deepcopy(non_auditor_before)
    non_auditor_after["expected_calls"] = 3
    non_auditor_after["command_hash"] = "recounted_hash"
    assert _approved_retry_command_change(non_auditor_before, non_auditor_after) is False

    hearing_before = deepcopy(before)
    hearing_before["stage_id"] = "ch012_xchapter_hearing"
    hearing_after = deepcopy(hearing_before)
    hearing_after["expected_calls"] = 2
    hearing_after["command_hash"] = "recovered_component_count_hash"
    assert _approved_retry_command_change(hearing_before, hearing_after) is True


def test_partial_cross_chapter_report_is_not_reused_as_complete(
    tmp_path: Path,
) -> None:
    report_path = tmp_path / "run_report.json"
    partial = {
        "schema_version": "literary_b1_cross_chapter_auditor_report_v1",
        "ready_hearings_complete": False,
        "chapter_loop_complete": False,
        "quarantined_component_ids": ["hearing_bad"],
    }
    _write_json(report_path, partial)

    assert _existing_stage_output_is_complete_v1(
        stage_name="xchapter_hearing",
        report_path=report_path,
    ) is False

    complete = {
        **partial,
        "ready_hearings_complete": True,
        "chapter_loop_complete": True,
        "quarantined_component_ids": [],
    }
    report_path.write_text(
        json.dumps(complete, ensure_ascii=False, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    assert _existing_stage_output_is_complete_v1(
        stage_name="xchapter_hearing",
        report_path=report_path,
    ) is True


def test_retry_command_accepts_decided_hearing_wiring_for_offline_b2_routing() -> None:
    before = {
        "schema_version": "literary_chapter_loop_command_v1",
        "stage_id": "ch004_b2_review_routing",
        "chapter_id": "book_ch04",
        "command": ["python", "route.py", "--b2-root", "stable"],
        "resolved_inputs": {"b2_root": "stable"},
        "expected_calls": 0,
        "retry_allowed": False,
        "fallback_allowed": False,
        "command_hash": "old_hash",
    }
    after = deepcopy(before)
    after["command"].extend(
        ["--decided-cross-component-id", "hearing_a"]
    )
    after["resolved_inputs"].update(
        {
            "decision_ledger": "ledger.json",
            "decided_cross_component_ids": ["hearing_a"],
        }
    )
    after["command_hash"] = "new_hash"

    assert _approved_retry_command_change(before, after) is True

    after["resolved_inputs"]["b2_root"] = "foreign"
    assert _approved_retry_command_change(before, after) is False


def test_retry_command_accepts_only_prior_catalog_wiring_for_b3_apply() -> None:
    before_body = {
        "schema_version": "literary_chapter_loop_command_v1",
        "stage_id": "ch005_b3_apply",
        "chapter_id": "chapter_v",
        "command": [
            "python",
            "apply.py",
            "--b3-root",
            "b3",
            "--out-dir",
            "out",
            "--reconciled-projection",
            "projection.json",
        ],
        "resolved_inputs": {
            "b3_root": "b3",
            "reconciled_projection": "projection.json",
        },
        "expected_calls": 0,
        "retry_allowed": False,
        "fallback_allowed": False,
    }
    after_body = deepcopy(before_body)
    after_body["command"] = [
        *after_body["command"],
        "--component-catalog",
        "prior/component_catalog.json",
    ]
    after_body["resolved_inputs"] = {
        **after_body["resolved_inputs"],
        "prior_component_catalogs": ["prior/component_catalog.json"],
    }
    before = {**before_body, "command_hash": canonical_hash(before_body)}
    after = {**after_body, "command_hash": canonical_hash(after_body)}

    assert _approved_retry_command_change(before, after) is True

    changed_projection = deepcopy(after)
    changed_projection["command"][
        changed_projection["command"].index("--reconciled-projection") + 1
    ] = "foreign-projection.json"
    assert _approved_retry_command_change(before, changed_projection) is False


def test_b3_auditor_does_not_readjudicate_carried_prior_cases() -> None:
    artifact = {
        "chapter_id": "book_ch03",
        "pending_cases": [
            {
                "pending_case_id": "case_current",
                "chapter_id": "book_ch03",
                "review_route": "stable_claim_review",
            },
            {
                "pending_case_id": "case_carried",
                "chapter_id": "book_ch02",
                "review_route": "stable_claim_review",
            },
            {
                "pending_case_id": "case_identity",
                "chapter_id": "book_ch03",
                "review_route": "identity_review",
            },
        ],
    }

    assert [row["pending_case_id"] for row in _serviceable_cases(artifact)] == [
        "case_current"
    ]


def test_conditional_zero_work_stage_writes_an_explicit_skipped_receipt(
    tmp_path: Path,
) -> None:
    run_root, _, _ = _initialized_executor(tmp_path, chapter_count=1)

    def executor(
        stage: ChapterCycleStage, permit: ApiCallPermit
    ) -> StageExecutionResult:
        if stage.stage_name == "xchapter_hearing":
            return StageExecutionResult(
                status="skipped",
                payload={"skip_reason": "no_ready_cross_chapter_component"},
                call_disposition="not_required",
            )
        if stage.requires_api:
            permit.reserve("fixture")
            return StageExecutionResult(
                status="accepted",
                payload={"fixture": True},
                call_disposition="called",
                request_fingerprint="0" * 64,
                model_actual="gpt-fixture",
                resilience_report_hash="1" * 64,
                attempt_count=1,
            )
        return StageExecutionResult(
            status="accepted",
            payload={"fixture": True},
            call_disposition="code_only",
        )

    for _ in range(6):
        advance_chapter_cycle_stage_v1(run_root=run_root, executor=executor)

    state = load_chapter_cycle_state_v1(run_root)
    receipt = state["stage_receipts"][-1]
    assert receipt["stage_id"] == "ch001_xchapter_hearing"
    assert receipt["status"] == "skipped"
    result = json.loads(
        (run_root / receipt["artifact_path"]).read_text(encoding="utf-8")
    )
    assert result["payload"]["skip_reason"] == "no_ready_cross_chapter_component"
    assert result["attempt_count"] == 0


def test_identity_apply_does_not_require_decisions_when_hearing_was_skipped(
    tmp_path: Path,
) -> None:
    run_root, executor, _ = _initialized_executor(tmp_path, chapter_count=1)
    registry_root = tmp_path / "registry"
    _write_json(registry_root / "chapter_registry.json", {"fixture": True})
    _write_json(
        registry_root / "cross_chapter_hearing_queue.json", {"fixture": True}
    )
    registry_result = _write_json(
        run_root / "stage_results" / "registry.json",
        {"payload": {"output_root": str(registry_root)}},
    )
    _write_json(
        run_root / "receipts" / "ch001_b1_registry_writer.json",
        {
            "status": "accepted",
            "artifact_path": registry_result.relative_to(run_root).as_posix(),
        },
    )
    _write_json(
        run_root / "receipts" / "ch001_xchapter_hearing.json",
        {"status": "skipped", "artifact_path": "unused.json"},
    )
    stage = _stage(
        next(
            row
            for row in executor.plan["stage_plan"]
            if row["stage_name"] == "identity_apply"
        )
    )

    resolved = executor.resolve_inputs(stage, strict=True)

    assert resolved["decisions"] is None
    assert executor._condition(stage, resolved) == (
        False,
        "cross_chapter_hearing_skipped",
    )


def test_runtime_binding_rejects_capability_from_another_source(
    tmp_path: Path,
) -> None:
    path = _runtime_binding(tmp_path, source_id="expected-source")
    payload = json.loads(path.read_text(encoding="utf-8"))
    evidence_path = Path(
        payload["stages"]["b1_scan"]["capabilities"]["default"]
    )
    evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
    evidence["source_id"] = "foreign-source"
    _write_json(evidence_path, evidence)

    with pytest.raises(ChapterLoopBindingError, match="source_id differs"):
        load_runtime_bindings_v1(path)


def test_runtime_binding_hashes_capability_root_evidence(tmp_path: Path) -> None:
    path = _runtime_binding(tmp_path)
    payload = json.loads(path.read_text(encoding="utf-8"))
    evidence_source = Path(
        payload["stages"]["b1_scan"]["capabilities"]["default"]
    )
    capability_root = tmp_path / "b1_scan_capability"
    capability_root.mkdir()
    (capability_root / "capability_evidence.json").write_bytes(
        evidence_source.read_bytes()
    )
    payload["stages"]["b1_scan"]["capabilities"]["default"] = str(
        capability_root
    )
    _write_json(path, payload)

    loaded = load_runtime_bindings_v1(path)

    assert loaded.stages["b1_scan"].capabilities["default"] == capability_root


def test_console_history_indexes_artifact_delta_and_usage(
    tmp_path: Path,
) -> None:
    run_root, _, history = _initialized_executor(tmp_path, chapter_count=1)
    stage_root = run_root / "artifacts" / "chapters" / "ch001" / "b1_scan"
    _write_json(
        stage_root / "b1_scan_artifact.json",
        {
            "schema_version": "fixture_scan_v1",
            "entity_observations": [
                {"observation_id": "obs1", "status": "observed"},
                {"observation_id": "obs2", "status": "observed"},
            ],
            "review_issues": [{"id": "issue1", "status": "quarantined"}],
        },
    )
    report = _write_json(
        stage_root / "canary_report.json",
        {
            "usage": {
                "input_tokens": 100,
                "output_tokens": 20,
                "cached_input_tokens": 60,
            }
        },
    )

    result = history.record_stage(
        stage_id="ch001_b1_scan",
        stage_name="b1_scan",
        chapter_id="book_ch01",
        stage_root=stage_root,
        output_names=("b1_scan_artifact.json", "canary_report.json"),
        parent_artifact_refs=(),
        report_path=report,
        status="accepted",
    )

    assert result["semantic_delta"]["collection_counts"] == {
        "entity_observations": 2,
        "review_issues": 1,
    }
    assert result["usage"]["cached_input_tokens"] == 60
    index = json.loads((run_root / "artifact_index.json").read_text(encoding="utf-8"))
    assert len(index["artifacts"]) == 3
    assert index["artifacts"][-1]["artifact_kind"] == "semantic_delta"
    events = (run_root / "events.jsonl").read_text(encoding="utf-8")
    assert '"event_type": "artifact_created"' in events
    assert '"event_type": "usage_snapshot"' in events
    assert '"component_id": "translation"' in events
    assert '"component_seq":' in events
    assert '"seq":' not in events
    assert extract_usage_v1(report)["input_tokens"] == 100


def test_console_history_indexes_an_offline_stage_revision(
    tmp_path: Path,
) -> None:
    run_root, _, history = _initialized_executor(tmp_path, chapter_count=1)
    stage_root = run_root / "artifacts" / "chapters" / "ch001" / "b1_registry_writer"
    _write_json(
        stage_root / "chapter_registry.json",
        {"schema_version": "registry_v1", "cards": [], "relation_edges": []},
    )
    original = history.record_stage(
        stage_id="ch001_b1_registry_writer",
        stage_name="b1_registry_writer",
        chapter_id="book_ch01",
        stage_root=stage_root,
        output_names=("chapter_registry.json",),
        parent_artifact_refs=(),
        report_path=None,
        status="accepted",
    )
    revision_root = (
        run_root
        / "corrections"
        / "chapters"
        / "ch001"
        / "relation_correction"
    )
    _write_json(
        revision_root / "chapter_registry.json",
        {
            "schema_version": "registry_v1",
            "cards": [],
            "relation_edges": [{"relation_edge_id": "corrected"}],
        },
    )
    _write_json(
        revision_root / "relation_correction_receipt.json",
        {"schema_version": "correction_receipt_v1"},
    )

    revised = history.record_stage_revision(
        stage_id="ch001_b1_registry_writer",
        stage_name="b1_registry_writer",
        chapter_id="book_ch01",
        revision_name="relation_correction",
        revision_root=revision_root,
        output_names=(
            "chapter_registry.json",
            "relation_correction_receipt.json",
        ),
        parent_artifact_refs=(
            original["artifacts"][0]["artifact_ref"],
        ),
        revision_metadata={"effective_revision": True},
    )

    assert revised["already_indexed"] is False
    assert revised["semantic_delta"]["collection_counts"] == {
        "cards": 0,
        "relation_edges": 1,
    }
    assert all(
        "/relation_correction/" in row["artifact_ref"]
        for row in revised["artifacts"]
    )
    events = [
        json.loads(row)
        for row in (run_root / "events.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    assert sum(row["event"] == "stage_done" for row in events) == 1
    index = json.loads(
        (run_root / "artifact_index.json").read_text(encoding="utf-8")
    )
    indexed_paths = {row["relative_path"] for row in index["artifacts"]}
    assert revision_root.joinpath("semantic_delta.json").relative_to(
        run_root
    ).as_posix() in indexed_paths


def test_registry_receipt_prefers_a_verified_relation_correction(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_root, executor, _ = _initialized_executor(tmp_path, chapter_count=2)
    source_root = (
        run_root / "artifacts" / "chapters" / "ch001" / "b1_registry_writer"
    )
    for name, value in {
        "chapter_registry.json": {"registry_hash": "source"},
        "prior_cards.json": {"cards": ["source"]},
        "cross_chapter_hearing_queue.json": {"queue": "source"},
        "writer_report.json": {"report": "source"},
    }.items():
        _write_json(source_root / name, value)
    result = _write_json(
        run_root / "stage_results" / "registry.json",
        {"payload": {"output_root": str(source_root)}},
    )
    _write_json(
        run_root / "receipts" / "ch001_b1_registry_writer.json",
        {
            "status": "accepted",
            "artifact_path": result.relative_to(run_root).as_posix(),
        },
    )
    correction_root = (
        run_root
        / "corrections"
        / "chapters"
        / "ch001"
        / "relation_correction"
    )
    for name, value in {
        "chapter_registry.json": {"registry_hash": "corrected"},
        "prior_cards.json": {"cards": ["corrected"]},
        "relation_correction_overlay.json": {"overlay_hash": "fixture"},
        "relation_correction_receipt.json": {"receipt_hash": "fixture"},
        "cross_chapter_hearing_queue.json": {"queue": "source"},
        "writer_report.json": {"report": "source"},
    }.items():
        _write_json(correction_root / name, value)
    monkeypatch.setattr(
        "pipeline.literary.chapter_loop_current_executor_v1."
        "verify_relation_correction_bundle_v1",
        lambda **_kwargs: None,
    )

    assert executor._receipt_selector(
        ordinal=1,
        stage_name="b1_registry_writer",
        selector="prior_cards.json",
        strict=True,
    ) == correction_root / "prior_cards.json"
    assert executor._receipt_selector(
        ordinal=1,
        stage_name="b1_registry_writer",
        selector="root",
        strict=True,
    ) == correction_root
    assert executor._receipt_selector(
        ordinal=1,
        stage_name="b1_registry_writer",
        selector="cross_chapter_hearing_queue.json",
        strict=True,
    ) == source_root / "cross_chapter_hearing_queue.json"

    _write_json(
        correction_root / "cross_chapter_hearing_queue.json",
        {"queue": "tampered"},
    )
    with pytest.raises(
        LiteraryChapterLoopExecutorError,
        match="passthrough differs",
    ):
        executor._receipt_selector(
            ordinal=1,
            stage_name="b1_registry_writer",
            selector="root",
            strict=True,
        )


def test_receipt_selector_is_unchanged_without_effective_root_manifest(
    tmp_path: Path,
) -> None:
    run_root, executor, _ = _initialized_executor(tmp_path, chapter_count=2)
    source_root = _write_accepted_stage_fixture(
        run_root=run_root,
        executor=executor,
        ordinal=1,
        stage_name="b2_frame_interaction",
    )

    assert executor._receipt_selector(
        ordinal=1,
        stage_name="b2_frame_interaction",
        selector="root",
        strict=True,
    ) == source_root
    assert not (run_root / "corrections" / "effective_stage_roots.json").exists()


def test_effective_roots_drive_chapter_four_prior_and_prefix_selectors(
    tmp_path: Path,
) -> None:
    run_root, executor, _ = _initialized_executor(tmp_path, chapter_count=4)
    stage_names = (
        "b1_registry_writer",
        "identity_apply",
        "b2_frame_interaction",
        "b3_apply",
        "b0_summary",
    )
    corrected: dict[str, Path] = {}
    for stage_name in stage_names:
        _write_accepted_stage_fixture(
            run_root=run_root,
            executor=executor,
            ordinal=3,
            stage_name=stage_name,
        )
        stage_id = f"ch003_{stage_name}"
        corrected[stage_id] = _write_stage_outputs(
            run_root
            / "corrections"
            / "chapters"
            / "ch003"
            / f"{stage_name}_revision",
            outputs=executor.bindings[stage_name].outputs,
            marker=f"corrected:{stage_id}",
        )

    manifest = executor.bind_effective_stage_roots(corrected)

    assert len(manifest["overrides"]) == len(stage_names)
    assert executor._receipt_selector(
        ordinal=3,
        stage_name="identity_apply",
        selector="prior_cards.json",
        strict=True,
    ) == corrected["ch003_identity_apply"] / "prior_cards.json"
    chapter_four = _stage(
        next(
            row
            for row in executor.plan["stage_plan"]
            if row["stage_id"] == "ch004_b1_scan"
        )
    )
    assert executor._resolve_arm(
        stage=chapter_four,
        arm="previous.b0_summary:root",
        strict=True,
    ) == corrected["ch003_b0_summary"]
    assert executor._resolve_arm(
        stage=chapter_four,
        arm="previous.b2_frame_interaction:root",
        strict=True,
    ) == corrected["ch003_b2_frame_interaction"]
    assert executor._resolve_arm(
        stage=chapter_four,
        arm="all_previous.b3_apply:root",
        strict=True,
    ) == [corrected["ch003_b3_apply"]]
    assert executor._resolve_arm(
        stage=chapter_four,
        arm="all.b1_registry_writer:root",
        strict=True,
    ) == [corrected["ch003_b1_registry_writer"]]
    chapter_three = _stage(
        next(
            row
            for row in executor.plan["stage_plan"]
            if row["stage_id"] == "ch003_b0_summary"
        )
    )
    assert executor.stage_output_root_for(
        chapter_three,
        "b3_apply",
    ) == corrected["ch003_b3_apply"]


def test_effective_root_binding_rejects_foreign_or_incomplete_roots(
    tmp_path: Path,
) -> None:
    run_root, executor, _ = _initialized_executor(tmp_path, chapter_count=1)
    _write_accepted_stage_fixture(
        run_root=run_root,
        executor=executor,
        ordinal=1,
        stage_name="b0_summary",
    )
    foreign = _write_stage_outputs(
        tmp_path / "foreign",
        outputs=executor.bindings["b0_summary"].outputs,
        marker="foreign",
    )
    with pytest.raises(
        LiteraryChapterLoopExecutorError,
        match="outside the component",
    ):
        executor.bind_effective_stage_roots({"ch001_b0_summary": foreign})

    incomplete = run_root / "corrections" / "incomplete"
    incomplete.mkdir(parents=True)
    with pytest.raises(
        LiteraryChapterLoopExecutorError,
        match="omits outputs",
    ):
        executor.bind_effective_stage_roots({"ch001_b0_summary": incomplete})


def test_effective_root_binding_detects_tamper_and_stage_mismatch(
    tmp_path: Path,
) -> None:
    run_root, executor, _ = _initialized_executor(tmp_path, chapter_count=1)
    _write_accepted_stage_fixture(
        run_root=run_root,
        executor=executor,
        ordinal=1,
        stage_name="b0_summary",
    )
    corrected = _write_stage_outputs(
        run_root / "corrections" / "b0_revision",
        outputs=executor.bindings["b0_summary"].outputs,
        marker="corrected",
    )
    executor.bind_effective_stage_roots({"ch001_b0_summary": corrected})
    _write_json(corrected / "capsule_log.json", {"marker": "tampered"})
    with pytest.raises(
        LiteraryChapterLoopExecutorError,
        match="fingerprint differs",
    ):
        executor._receipt_selector(
            ordinal=1,
            stage_name="b0_summary",
            selector="root",
            strict=True,
        )

    _write_stage_outputs(
        corrected,
        outputs=executor.bindings["b0_summary"].outputs,
        marker="corrected",
    )
    executor.bind_effective_stage_roots(
        {"ch001_b0_summary": corrected},
        replace_existing=True,
    )
    manifest_path = run_root / "corrections" / "effective_stage_roots.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["overrides"][0]["stage_name"] = "b3_apply"
    body = dict(manifest)
    body.pop("manifest_hash")
    manifest["manifest_hash"] = canonical_hash(body)
    _write_json(manifest_path, manifest)
    with pytest.raises(
        LiteraryChapterLoopExecutorError,
        match="identity differs",
    ):
        executor._receipt_selector(
            ordinal=1,
            stage_name="b0_summary",
            selector="root",
            strict=True,
        )


def test_live_resume_records_revision_metadata_and_keeps_accepted_receipt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    document_path = _write_json(tmp_path / "document.json", _document(1))
    frozen_db = tmp_path / "memory.sqlite3"
    frozen_db.write_bytes(b"sealed-offline-fixture")
    run_root = tmp_path / "run"
    initial_revision = "a" * 40
    resumed_revision = "b" * 40
    monkeypatch.setattr(
        chapter_loop_runner, "_clean_head", lambda: initial_revision
    )
    chapter_loop_runner._initialize(
        argparse.Namespace(
            document=document_path,
            run_root=run_root,
            run_id="revision_resume_fixture",
            frozen_db=frozen_db,
            runtime_bindings=_runtime_binding(tmp_path),
            project_binding=None,
            profile=PROFILE,
            stage_bindings=STAGE_BINDINGS,
            stop_after_chapter_count=1,
            chapter_id=None,
            chapter_range=None,
            all_chapters=True,
        )
    )

    def accepted_executor(
        stage: ChapterCycleStage, permit: ApiCallPermit
    ) -> StageExecutionResult:
        permit.reserve("fixture")
        return StageExecutionResult(
            status="accepted",
            payload={"fixture": True},
            call_disposition="called",
            request_fingerprint="0" * 64,
            model_actual="gpt-fixture",
            resilience_report_hash="1" * 64,
            attempt_count=1,
        )

    advance_chapter_cycle_stage_v1(
        run_root=run_root, executor=accepted_executor
    )
    receipt_path = run_root / "receipts" / "ch001_b1_scan.json"
    accepted_receipt = receipt_path.read_bytes()
    monkeypatch.setattr(chapter_loop_runner, "_head", lambda: resumed_revision)
    monkeypatch.setattr(
        chapter_loop_runner, "_require_clean_tracked_worktree", lambda: None
    )

    context = chapter_loop_runner._load_context(run_root, for_live=True)
    second_context = chapter_loop_runner._load_context(run_root, for_live=True)

    assert context["session"]["code_revision"] == initial_revision
    assert context["session"]["active_code_revision"] == resumed_revision
    assert context["session"]["code_revision_history"] == [
        initial_revision,
        resumed_revision,
    ]
    assert second_context["session"] == context["session"]
    assert receipt_path.read_bytes() == accepted_receipt
    state = load_chapter_cycle_state_v1(run_root)
    assert state["current_stage"] == "ch001_b1_enrich"
    assert len(state["stage_receipts"]) == 1
    manifest = json.loads(
        (run_root / "component_manifest.json").read_text(encoding="utf-8")
    )
    assert manifest["active_code_revision"] == resumed_revision
    assert manifest["code_revision_history"] == [
        initial_revision,
        resumed_revision,
    ]
    events = [
        json.loads(line)
        for line in (run_root / "events.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
        if line.strip()
    ]
    assert not any(row["event"] == "code_transition" for row in events)


def _stage(row: dict[str, Any]) -> ChapterCycleStage:
    return ChapterCycleStage(
        stage_id=row["stage_id"],
        chapter_id=row["chapter_id"],
        chapter_ordinal=row["chapter_ordinal"],
        stage_name=row["stage_name"],
        stage_role=row["stage_role"],
        requires_api=row["requires_api"],
        is_chapter_checkpoint=row["is_chapter_checkpoint"],
        stage_descriptor_hash=row["stage_descriptor_hash"],
    )


def test_speaker_recovery_expected_calls_follow_batches_and_existing_report(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    b2_root = tmp_path / "b2"
    output_root = tmp_path / "speaker-recovery"
    monkeypatch.setattr(
        "pipeline.literary.chapter_loop_current_executor_v1."
        "load_b2_slim_speaker_source_v1",
        lambda _root: ({"artifact_hash": "b2-fixture"}, [{"window_id": "w1"}]),
    )
    monkeypatch.setattr(
        "pipeline.literary.chapter_loop_current_executor_v1."
        "build_b2_slim_speaker_recovery_index_v1",
        lambda **_kwargs: {
            "registry_components": [
                {"component_id": f"component-{index}", "overflow": False}
                for index in range(5)
            ]
        },
    )

    assert _speaker_recovery_expected_calls_v1(
        b2_root=b2_root,
        output_root=output_root,
    ) == 2

    _write_json(
        output_root / "canary_report.json",
        {
            "schema_version": "literary_b2_speaker_recovery_canary_report_v1",
            "status": "semantic_accepted",
            "provider_calls": 2,
            "batch_count": 2,
        },
    )
    monkeypatch.setattr(
        "pipeline.literary.chapter_loop_current_executor_v1."
        "load_b2_slim_speaker_source_v1",
        lambda _root: pytest.fail("completed output must not rebuild the index"),
    )
    assert _speaker_recovery_expected_calls_v1(
        b2_root=b2_root,
        output_root=output_root,
    ) == 2


def test_speaker_recovery_retry_accepts_only_a_recounted_batch_total() -> None:
    before = {
        "schema_version": "literary_chapter_loop_command_v1",
        "stage_id": "ch017_speaker_recovery",
        "chapter_id": "book_ch17",
        "command": ["python", "speaker_recovery.py", "--b2-root", "stable"],
        "resolved_inputs": {"b2_root": "stable"},
        "expected_calls": 1,
        "retry_allowed": False,
        "fallback_allowed": False,
        "command_hash": "old",
    }
    after = deepcopy(before)
    after["expected_calls"] = 2
    after["command_hash"] = "recounted"

    assert _approved_retry_command_change(before, after) is True

    after["resolved_inputs"]["b2_root"] = "foreign"
    assert _approved_retry_command_change(before, after) is False
