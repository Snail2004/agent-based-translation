from __future__ import annotations

import json
import sys
from collections import Counter
from hashlib import sha256
from pathlib import Path

import pytest

from pipeline.prepass.d2l_console_replay_contract_v1 import (
    STAGE_IDS,
    build_scoring_handoff_fragment,
    build_stage_plan,
    canonical_sha256,
    file_sha256,
    validate_translation_component_package,
)
from pipeline.prepass.d2l_component_stage_receipt_v1 import (
    STAGE_RECEIPT_SCHEMA,
    build_stage_receipt,
)
from pipeline.prepass.d2l_translation_component_runner_v1 import (
    ComponentPlan,
    ComponentRunnerError,
    D2LTranslationComponentRunner,
    RUNNER_SCHEMA,
)


GIT_COMMIT = "1" * 40
CONFIG_SHA = "2" * 64
CODE_SHA = "3" * 64
PROFILE_SHA = "4" * 64
CHAPTERS = ["d2l_multilayer_perceptrons"]


def _event_counts(root: Path) -> Counter[str]:
    return Counter(
        json.loads(line)["event"]
        for line in (root / "events.jsonl").read_text(encoding="utf-8").splitlines()
    )


def _binding(ref: str, kind: str, schema: str, digest: str) -> dict[str, str]:
    return {
        "artifact_ref": ref,
        "artifact_kind": kind,
        "schema_version": schema,
        "sha256": digest,
        "sha256_kind": "physical",
    }


def _source_binding() -> dict[str, object]:
    def item(ref: str, kind: str, schema: str) -> dict[str, str]:
        digest = sha256(ref.encode("utf-8")).hexdigest().upper()
        return _binding(ref, kind, schema, digest)

    return {
        "schema": "canonical_source_binding_v1",
        "document": item("src_document", "source_document", "document_v1"),
        "structure_manifest": item(
            "src_structure", "structure_manifest", "structure_manifest_v1"
        ),
        "asset_manifest": item("src_assets", "asset_manifest", "asset_manifest_v1"),
        "admitted_projection": item(
            "src_projection", "admitted_projection", "admitted_projection_v1"
        ),
        "normalization_receipt": item(
            "src_receipt", "normalization_receipt", "normalization_receipt_v1"
        ),
        "package_seal": item(
            "src_package_seal", "source_package_seal", "source_package_seal_v1"
        ),
    }


def _write_payloads(root: Path, *, attempt_id: int) -> None:
    artifacts = root / "artifacts"
    artifacts.mkdir(parents=True, exist_ok=True)
    s0_path = artifacts / "translation_s0.json"
    s1_path = artifacts / "translation_s1.json"
    glossary_path = artifacts / "glossary.json"
    s0_path.write_text('{"arm":"s0","blocks":["b001"]}\n', encoding="utf-8")
    s1_path.write_text('{"arm":"s1","blocks":["b001"]}\n', encoding="utf-8")
    glossary_path.write_text('{"entries":[]}\n', encoding="utf-8")
    source = _source_binding()
    universe_sha = sha256(b"b001").hexdigest().upper()
    inputs = []
    for arm_id, path in (("s0", s0_path), ("s1", s1_path)):
        inputs.append(
            {
                "arm_id": arm_id,
                "artifact": _binding(
                    f"art_translation_{arm_id}",
                    "translation_artifact",
                    "TranslationArtifactV1",
                    file_sha256(path),
                ),
                "producer_component_run_id": "tr_component_test_v1",
                "producer_component_attempt_id": attempt_id,
                "profile_id": f"d2l_{arm_id}_v1",
                "profile_sha256": PROFILE_SHA,
                "config_sha256": CONFIG_SHA,
                "selected_chapter_ids": CHAPTERS,
                "coverage": {
                    "admitted_block_count": 1,
                    "translated_block_count": 1,
                    "preserved_block_count": 0,
                    "missing_block_count": 0,
                    "failed_block_count": 0,
                    "ordered_block_ids_sha256": universe_sha,
                    "status": "exact_cover",
                },
                "source_binding_sha256": canonical_sha256(source),
            }
        )
    fragment = build_scoring_handoff_fragment(
        workflow_run_id="wf_component_test_v1",
        translation_component_run_id="tr_component_test_v1",
        translation_component_attempt_id=attempt_id,
        reserved_evaluation_component_run_id="ev_component_test_v1",
        artifact_ref="art_scoring_handoff_fragment",
        source_binding=source,
        translation_inputs=inputs,
        glossary_binding=_binding(
            "art_glossary",
            "glossary",
            "D2LGlossaryV1",
            file_sha256(glossary_path),
        ),
        context_memory_binding=None,
        selected_chapter_ids=CHAPTERS,
        admitted_universe={
            "ordered_block_ids_sha256": universe_sha,
            "block_count": 1,
            "status": "exact_cover",
        },
        producer_lineage={
            "git_commit": GIT_COMMIT,
            "pipeline_version": "d2l_translation_component_runner_v1",
            "config_sha256": CONFIG_SHA,
            "code_sha256": CODE_SHA,
        },
        created_at="2026-07-22T00:00:00Z",
    )
    (root / "scoring_handoff_fragment.json").write_text(
        json.dumps(fragment, ensure_ascii=False, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _artifact_spec(
    ref: str,
    kind: str,
    schema: str,
    path: str,
) -> dict[str, object]:
    return {
        "artifact_ref": ref,
        "artifact_kind": kind,
        "schema_version": schema,
        "relative_path": path,
        "parent_artifact_refs": [],
        "metadata": {},
    }


def _plan(*, attempt_id: int) -> dict[str, object]:
    units = {row["stage_id"]: row["progress"]["unit"] for row in build_stage_plan()}
    stages = []
    for stage_id in STAGE_IDS:
        specs: list[dict[str, object]] = []
        if stage_id == "glossary_seal":
            specs.append(
                _artifact_spec(
                    "art_glossary", "glossary", "D2LGlossaryV1", "artifacts/glossary.json"
                )
            )
        elif stage_id == "translator":
            specs.extend(
                [
                    _artifact_spec(
                        "art_translation_s0",
                        "translation_artifact",
                        "TranslationArtifactV1",
                        "artifacts/translation_s0.json",
                    ),
                    _artifact_spec(
                        "art_translation_s1",
                        "translation_artifact",
                        "TranslationArtifactV1",
                        "artifacts/translation_s1.json",
                    ),
                ]
            )
        elif stage_id == "scoring_handoff_fragment":
            specs.append(
                _artifact_spec(
                    "art_scoring_handoff_fragment",
                    "scoring_handoff_fragment",
                    "scoring_handoff_fragment_v1",
                    "scoring_handoff_fragment.json",
                )
            )
        stages.append(
            {
                "stage_id": stage_id,
                "producer": stage_id,
                "command": [sys.executable, "-c", "pass"],
                "cwd": None,
                "artifact_specs": specs,
                "total": 1,
                "unit": units[stage_id],
                "work_id": f"work_{stage_id}",
                "mode": "execute",
                "timeout_seconds": 30,
                "receipt_ref": None,
            }
        )
    return {
        "schema": RUNNER_SCHEMA,
        "workflow_run_id": "wf_component_test_v1",
        "component_run_id": "tr_component_test_v1",
        "pipeline_id": "d2l_terminology",
        "pipeline_version": "d2l_translation_component_runner_v1",
        "source_binding": _source_binding(),
        "config_sha256": CONFIG_SHA,
        "code_revision": GIT_COMMIT,
        "selected_chapter_ids": CHAPTERS,
        "stages": stages,
        "scoring_handoff_fragment_ref": "scoring_handoff_fragment.json",
    }


def test_runner_emits_terminal_component_package(tmp_path: Path) -> None:
    root = tmp_path / "component"
    _write_payloads(root, attempt_id=1)
    result = D2LTranslationComponentRunner(_plan(attempt_id=1), root).run()

    assert result["terminal_event"] == "run_done"
    assert result["artifact_count"] == 4
    counts = _event_counts(root)
    assert "cost_snapshot" not in counts
    assert "response_received" not in counts
    assert not (root / "workflow_manifest.json").exists()
    assert not (root / "scoring_handoff.json").exists()
    manifest = json.loads((root / "component_manifest.json").read_text(encoding="utf-8"))
    assert manifest["status"] == "succeeded"
    assert all(stage["status"] == "succeeded" for stage in manifest["stages"])
    assert validate_translation_component_package(root)["component_attempt_id"] == 1


def test_runner_pause_resume_increments_component_attempt(tmp_path: Path) -> None:
    root = tmp_path / "component"
    _write_payloads(root, attempt_id=2)
    plan = ComponentPlan.from_mapping(_plan(attempt_id=2))

    paused = D2LTranslationComponentRunner(
        plan,
        root,
        stop_after_stage="candidate_index",
    ).run()
    assert paused["terminal_event"] is None
    manifest1 = json.loads((root / "component_manifest.json").read_text(encoding="utf-8"))
    assert manifest1["status"] == "paused"
    assert manifest1["component_attempt_id"] == 1
    assert manifest1["resume"]["resume_available"] is True

    resumed = D2LTranslationComponentRunner(plan, root).run(resume=True)
    assert resumed["terminal_event"] == "run_done"
    assert resumed["component_attempt_id"] == 2
    events = [
        json.loads(line)
        for line in (root / "events.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert sum(row["event"] == "run_resumed" for row in events) == 1
    assert [row["component_attempt_id"] for row in events if row["event"] == "run_resumed"] == [2]


def test_runner_honors_pause_marker_only_at_stage_boundary(tmp_path: Path) -> None:
    root = tmp_path / "component"
    pause_file = tmp_path / "PAUSE"
    _write_payloads(root, attempt_id=2)
    plan = ComponentPlan.from_mapping(_plan(attempt_id=2))

    # The marker is created before execution to model an App pause request.
    # The current stage is still allowed to finish; the next stage is the
    # checkpoint boundary.
    pause_file.write_text("paused_by_user\n", encoding="utf-8")
    paused = D2LTranslationComponentRunner(
        plan,
        root,
        pause_file=pause_file,
    ).run()

    assert paused["terminal_event"] is None
    manifest = json.loads((root / "component_manifest.json").read_text(encoding="utf-8"))
    assert manifest["status"] == "paused"
    assert manifest["active_stage_id"] == "b1_candidate_discovery"
    assert manifest["resume"]["paused_reason"] == "user_requested_pause"


def test_runner_rejects_plan_with_forbidden_semantic_evidence() -> None:
    plan = _plan(attempt_id=1)
    plan["gold"] = {"term": "answer"}

    with pytest.raises(ComponentRunnerError, match="forbidden key"):
        ComponentPlan.from_mapping(plan)


def test_runner_rejects_duplicate_artifact_refs_before_execution() -> None:
    plan = _plan(attempt_id=1)
    plan["stages"][0]["artifact_specs"] = [
        _artifact_spec(
            "art_glossary",
            "preflight_receipt",
            "preflight_receipt_v1",
            "artifacts/preflight.json",
        )
    ]

    with pytest.raises(ComponentRunnerError, match="declared more than once"):
        ComponentPlan.from_mapping(plan)


def test_runner_imports_validated_child_stage_receipt(tmp_path: Path) -> None:
    root = tmp_path / "component"
    _write_payloads(root, attempt_id=1)
    receipt_ref = "artifacts/preflight_receipt.json"
    receipt = build_stage_receipt(
        workflow_run_id="wf_component_test_v1",
        component_run_id="tr_component_test_v1",
        component_attempt_id=1,
        stage_id="preflight",
        producer="preflight",
        work_id="work_preflight",
        observations=[
            {
                "event": "request_sent",
                "agent": "preflight",
                "severity": "info",
                "ts": "2026-07-22T00:00:01Z",
                "payload": {
                    "logical_request_id": "req_preflight_1",
                    "physical_attempt_index": 1,
                    "work_kind": "check",
                    "work_id": "work_preflight",
                    "provider_id": "fake_provider",
                    "model_id": "fake_model",
                    "source_id": "fake_source",
                    "masked_quota_bucket": "fake-bucket-***",
                },
            },
            {
                "event": "response_received",
                "agent": "preflight",
                "severity": "info",
                "ts": "2026-07-22T00:00:02Z",
                "payload": {
                    "usage": {
                        "logical_request_id": "req_preflight_1",
                        "physical_attempt_index": 1,
                        "provider_id": "fake_provider",
                        "model_id": "fake_model",
                        "source_id": "fake_source",
                        "masked_quota_bucket": "fake-bucket-***",
                        "prompt_tokens": 10,
                        "completion_tokens": 2,
                        "cached_input_tokens": 0,
                        "reasoning_tokens": 0,
                        "total_tokens": 12,
                        "latency_ms": 20,
                        "finish_reason": "stop",
                        "cost_usd": None,
                        "currency": None,
                        "cost_status": "unknown",
                        "cache_status": "miss",
                        "cache_mechanism": "none",
                    }
                },
            },
        ],
    )
    (root / receipt_ref).write_text(
        json.dumps(receipt, ensure_ascii=False, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    plan = _plan(attempt_id=1)
    plan["stages"][0]["receipt_ref"] = receipt_ref
    plan["stages"][0]["artifact_specs"] = [
        _artifact_spec(
            "art_preflight_receipt",
            "d2l_stage_event_receipt",
            STAGE_RECEIPT_SCHEMA,
            receipt_ref,
        )
    ]

    result = D2LTranslationComponentRunner(plan, root).run()

    counts = _event_counts(root)
    assert result["terminal_event"] == "run_done"
    assert counts["request_sent"] == 1
    assert counts["response_received"] == 1
    assert result["artifact_count"] == 5


def test_runner_does_not_resume_after_plan_drift(tmp_path: Path) -> None:
    root = tmp_path / "component"
    _write_payloads(root, attempt_id=2)
    D2LTranslationComponentRunner(
        _plan(attempt_id=2), root, stop_after_stage="candidate_index"
    ).run()
    drifted = _plan(attempt_id=2)
    drifted["config_sha256"] = "F" * 64

    with pytest.raises(ComponentRunnerError, match="sealed plan"):
        D2LTranslationComponentRunner(drifted, root).run(resume=True)


def test_runner_rejects_argv_drift_before_write_or_execution(tmp_path: Path) -> None:
    root = tmp_path / "component"
    marker = tmp_path / "replacement-command-ran.txt"
    _write_payloads(root, attempt_id=2)
    D2LTranslationComponentRunner(
        _plan(attempt_id=2), root, stop_after_stage="candidate_index"
    ).run()
    before = {
        name: (root / name).read_bytes()
        for name in ("component_manifest.json", "artifact_index.json", "events.jsonl")
    }
    drifted = _plan(attempt_id=2)
    drifted["stages"][3]["command"] = [
        sys.executable,
        "-c",
        f"from pathlib import Path; Path({str(marker)!r}).write_text('ran')",
    ]

    with pytest.raises(ComponentRunnerError, match="runner plan hash mismatch"):
        D2LTranslationComponentRunner(drifted, root).run(resume=True)

    assert not marker.exists()
    assert before == {
        name: (root / name).read_bytes()
        for name in ("component_manifest.json", "artifact_index.json", "events.jsonl")
    }


def test_runner_rejects_material_noncommand_drift_before_write(tmp_path: Path) -> None:
    root = tmp_path / "component"
    _write_payloads(root, attempt_id=2)
    D2LTranslationComponentRunner(
        _plan(attempt_id=2), root, stop_after_stage="candidate_index"
    ).run()
    before = {
        name: (root / name).read_bytes()
        for name in ("component_manifest.json", "artifact_index.json", "events.jsonl")
    }
    drifted = _plan(attempt_id=2)
    drifted["stages"][3]["timeout_seconds"] = 31

    with pytest.raises(ComponentRunnerError, match="runner plan hash mismatch"):
        D2LTranslationComponentRunner(drifted, root).run(resume=True)

    assert before == {
        name: (root / name).read_bytes()
        for name in ("component_manifest.json", "artifact_index.json", "events.jsonl")
    }


def test_runner_records_terminal_failure_without_claiming_success(tmp_path: Path) -> None:
    root = tmp_path / "component"
    _write_payloads(root, attempt_id=1)
    plan = _plan(attempt_id=1)
    plan["stages"][3]["command"] = [sys.executable, "-c", "raise SystemExit(7)"]

    with pytest.raises(ComponentRunnerError, match="exit code 7"):
        D2LTranslationComponentRunner(plan, root).run()

    result = validate_translation_component_package(root)
    assert result["terminal_event"] == "run_failed"
    manifest = json.loads((root / "component_manifest.json").read_text(encoding="utf-8"))
    assert manifest["status"] == "failed"
    assert manifest["scoring_handoff_fragment_ref"] is None
    failed = next(
        stage for stage in manifest["stages"] if stage["stage_id"] == "b2_admission_translation"
    )
    assert failed["status"] == "failed"
    assert failed["ended_at"] is not None
    assert failed["current_work_id"] is None
    counts = _event_counts(root)
    assert counts["validation_failed"] == 1
    assert counts["stage_done"] == 4
