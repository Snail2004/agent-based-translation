from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

import pipeline.eval.workflow_component_writer_v1 as writer_module
from pipeline.eval.contracts_v1 import ContractValidationError
from pipeline.eval.workflow_component_v1 import (
    build_evaluation_component_manifest_v1,
    build_scoring_receipt_v1,
)
from pipeline.eval.workflow_component_writer_v1 import (
    EvaluationWorkflowRunContextV1,
    validate_evaluation_workflow_component_package_v1,
)
from pipeline.tests.test_evaluation_benchmark_runner_v1 import (
    _Predictor,
    _manifest_and_preflight,
    _runtimes,
    _run,
    _sources,
)
from pipeline.tests.test_evaluation_workflow_component_v1 import _binding, _handoff


def _context() -> EvaluationWorkflowRunContextV1:
    handoff = _handoff()
    return EvaluationWorkflowRunContextV1(
        workflow_run_id=handoff["workflow_run_id"],
        component_run_id="evalcomp_fixture_001",
        scoring_handoff=handoff,
        scoring_handoff_artifact_ref="handoffs/scoring_handoff.json",
        evaluation_profile=_binding("profiles/evaluation_fixture_v1.json", "evaluation_profile_v1"),
    )


def test_runner_writes_replayable_component_package(tmp_path: Path) -> None:
    sources = _sources()
    manifest, preflight, overlays = _manifest_and_preflight(sources)
    root = tmp_path / "benchmark"
    result = _run(
        root,
        manifest,
        preflight,
        overlays,
        _runtimes(root, sources, [_Predictor(0.5) for _ in sources]),
        workflow_context=_context(),
    )

    assert result.workflow_component_root == root
    package = validate_evaluation_workflow_component_package_v1(
        root, _context().scoring_handoff, require_terminal=True
    )
    events = package["events"]
    assert [row["component_seq"] for row in events] == list(range(1, len(events) + 1))
    assert events[0]["event"] == "component_started"
    assert events[-1]["event"] == "component_done"
    assert {row["stage_id"] for row in events if row["event"] == "stage_start"} == {
        "preflight",
        "chapter_d2l_preliminaries",
        "chapter_d2l_linear_networks",
        "chapter_d2l_multilayer_perceptrons",
        "chapter_d2l_deep_learning_computation",
        "chapter_d2l_convolutional_neural_networks",
        "aggregation",
    }
    assert package["receipt"]["accepted_translation_inputs"] == _handoff()[
        "translation_inputs"
    ]
    assert package["receipt"]["accepted_input_set_sha256"] == _handoff()[
        "input_set_sha256"
    ]
    assert (root / "component_manifest.json").is_file()
    assert (root / "events.jsonl").is_file()
    assert (root / "artifact_index.json").is_file()
    assert (root / "scoring_receipt.json").is_file()


def test_interrupted_component_resumes_same_component_and_skips_completed_chapters(
    tmp_path: Path,
) -> None:
    sources = _sources()
    manifest, preflight, overlays = _manifest_and_preflight(sources)
    root = tmp_path / "benchmark"
    predictors = [_Predictor(0.5) for _ in sources]
    runtimes = _runtimes(root, sources, predictors)
    context = _context()
    failed_once = False

    def fail_third(common, config, child_root, **kwargs):
        nonlocal failed_once
        if common.blocks[0].chapter_id == "d2l_multilayer_perceptrons" and not failed_once:
            failed_once = True
            raise RuntimeError("fixture component interruption")
        from pipeline.eval.end_to_end_runner_v1 import run_evaluation_end_to_end_v1

        return run_evaluation_end_to_end_v1(common, config, child_root, **kwargs)

    with pytest.raises(RuntimeError, match="fixture component interruption"):
        _run(
            root,
            manifest,
            preflight,
            overlays,
            runtimes,
            chapter_runner=fail_third,
            workflow_context=context,
        )

    partial = validate_evaluation_workflow_component_package_v1(
        root, context.scoring_handoff
    )
    assert partial["events"][-1]["event"] == "component_halted"
    assert partial["events"][-1]["payload"]["resume_available"] is True
    assert any(row["artifact"]["artifact_kind"] == "evaluation_workflow_checkpoint_v1" for row in partial["artifact_index"]["artifacts"])

    result = _run(
        root,
        manifest,
        preflight,
        overlays,
        runtimes,
        workflow_context=context,
    )
    assert result.status["state"] == "completed"
    assert [predictor.calls for predictor in predictors] == [1, 1, 1, 1, 1]
    package = validate_evaluation_workflow_component_package_v1(
        root, context.scoring_handoff, require_terminal=True
    )
    events = package["events"]
    assert events[-1]["event"] == "component_done"
    assert any(row["event"] == "component_resumed" for row in events)
    attempts = {row["component_attempt_id"] for row in events}
    assert attempts == {"evalcomp_attempt_0001", "evalcomp_attempt_0002"}
    manifests = list((root / "manifest_revisions").glob("*.json"))
    assert len(manifests) == 2
    assert json.loads((root / "component_manifest.json").read_text(encoding="utf-8"))[
        "component_run_id"
    ] == context.component_run_id


def test_component_package_rejects_event_projection_tamper(tmp_path: Path) -> None:
    sources = _sources()
    manifest, preflight, overlays = _manifest_and_preflight(sources)
    root = tmp_path / "benchmark"
    context = _context()
    _run(
        root,
        manifest,
        preflight,
        overlays,
        _runtimes(root, sources, [_Predictor(0.5) for _ in sources]),
        workflow_context=context,
    )
    lines = (root / "events.jsonl").read_text(encoding="utf-8").splitlines()
    tampered = copy.deepcopy(json.loads(lines[1]))
    tampered["severity"] = "warning"
    lines[1] = json.dumps(tampered, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    (root / "events.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")
    with pytest.raises(ContractValidationError, match="projection"):
        validate_evaluation_workflow_component_package_v1(
            root, context.scoring_handoff, require_terminal=True
        )


def test_blocked_preflight_still_emits_terminal_replay_package(tmp_path: Path) -> None:
    sources = _sources()
    manifest, preflight, overlays = _manifest_and_preflight(sources, ready=False)
    root = tmp_path / "benchmark"
    context = _context()
    result = _run(
        root,
        manifest,
        preflight,
        overlays,
        _runtimes(root, sources, [_Predictor(0.5) for _ in sources]),
        workflow_context=context,
    )
    assert result.status["state"] == "blocked"
    package = validate_evaluation_workflow_component_package_v1(
        root, context.scoring_handoff, require_terminal=True
    )
    assert package["events"][-1]["event"] == "component_failed"
    assert package["events"][-1]["payload"]["reason_code"] == "benchmark_preflight_blocked"
    assert package["receipt"]["status"] == "accepted"


def test_replay_context_cannot_be_added_after_legacy_benchmark_started(tmp_path: Path) -> None:
    sources = _sources()
    manifest, preflight, overlays = _manifest_and_preflight(sources)
    root = tmp_path / "benchmark"
    _run(root, manifest, preflight, overlays, _runtimes(root, sources, [_Predictor(0.5) for _ in sources]))
    with pytest.raises(ContractValidationError, match="retrofit|already-started"):
        _run(
            root,
            manifest,
            preflight,
            overlays,
            _runtimes(root, sources, [_Predictor(0.5) for _ in sources]),
            workflow_context=_context(),
        )


def test_resume_recovers_same_attempt_after_crash_before_resume_event(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sources = _sources()
    manifest, preflight, overlays = _manifest_and_preflight(sources)
    root = tmp_path / "benchmark"
    context = _context()
    failed_once = False

    def interrupt_chapter(common, config, child_root, **kwargs):
        nonlocal failed_once
        if common.blocks[0].chapter_id == "d2l_multilayer_perceptrons" and not failed_once:
            failed_once = True
            raise RuntimeError("fixture component interruption")
        from pipeline.eval.end_to_end_runner_v1 import run_evaluation_end_to_end_v1

        return run_evaluation_end_to_end_v1(common, config, child_root, **kwargs)

    with pytest.raises(RuntimeError, match="fixture component interruption"):
        _run(
            root,
            manifest,
            preflight,
            overlays,
            _runtimes(root, sources, [_Predictor(0.5) for _ in sources]),
            chapter_runner=interrupt_chapter,
            workflow_context=context,
        )

    original_write = writer_module._write_immutable_json
    injected = False

    def crash_on_resume_event(path: Path, value: dict) -> None:
        nonlocal injected
        if (
            not injected
            and path.parent.name == "event_records"
            and value.get("event") == "component_resumed"
        ):
            injected = True
            raise OSError("fixture crash before Resume event commit")
        original_write(path, value)

    monkeypatch.setattr(writer_module, "_write_immutable_json", crash_on_resume_event)
    with pytest.raises(OSError, match="fixture crash"):
        _run(
            root,
            manifest,
            preflight,
            overlays,
            _runtimes(root, sources, [_Predictor(0.5) for _ in sources]),
            workflow_context=context,
        )
    current = json.loads((root / "component_manifest.json").read_text(encoding="utf-8"))
    assert current["component_attempt_id"] == "evalcomp_attempt_0001"
    assert (root / ".resume_intent.json").is_file()

    monkeypatch.setattr(writer_module, "_write_immutable_json", original_write)
    result = _run(
        root,
        manifest,
        preflight,
        overlays,
        _runtimes(root, sources, [_Predictor(0.5) for _ in sources]),
        workflow_context=context,
    )
    assert result.status["state"] == "completed"
    package = validate_evaluation_workflow_component_package_v1(
        root, context.scoring_handoff, require_terminal=True
    )
    assert {row["component_attempt_id"] for row in package["events"]} == {
        "evalcomp_attempt_0001",
        "evalcomp_attempt_0002",
    }
    assert not (root / ".resume_intent.json").exists()


def test_package_rejects_self_hashed_manifest_with_foreign_handoff_ref(
    tmp_path: Path,
) -> None:
    sources = _sources()
    manifest, preflight, overlays = _manifest_and_preflight(sources)
    root = tmp_path / "benchmark"
    context = _context()
    _run(
        root,
        manifest,
        preflight,
        overlays,
        _runtimes(root, sources, [_Predictor(0.5) for _ in sources]),
        workflow_context=context,
    )
    component_manifest = json.loads(
        (root / "component_manifest.json").read_text(encoding="utf-8")
    )
    foreign_binding = copy.deepcopy(component_manifest["scoring_handoff"])
    foreign_binding["artifact_ref"] = "foreign/renamed_handoff.json"
    foreign_manifest = build_evaluation_component_manifest_v1(
        workflow_run_id=component_manifest["workflow_run_id"],
        component_run_id=component_manifest["component_run_id"],
        component_attempt_id=component_manifest["component_attempt_id"],
        component_attempt_index=component_manifest["component_attempt_index"],
        manifest_revision=component_manifest["manifest_revision"],
        previous_manifest_sha256=component_manifest["previous_manifest_sha256"],
        created_at=component_manifest["created_at"],
        producer_code_commit=component_manifest["producer"]["code_commit"],
        scoring_handoff=foreign_binding,
        scoring_receipt_ref=component_manifest["scoring_receipt_ref"],
        accepted_input_set_sha256=component_manifest["accepted_input_set_sha256"],
        evaluation_profile=component_manifest["evaluation_profile"],
        stages=component_manifest["stages"],
    )
    latest_revision = max((root / "manifest_revisions").glob("*.json"))
    latest_revision.unlink()
    encoded = json.dumps(
        foreign_manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ) + "\n"
    (root / "component_manifest.json").write_text(encoded, encoding="utf-8", newline="\n")
    replacement = root / "manifest_revisions" / (
        f"{foreign_manifest['manifest_revision']:04d}_"
        f"{foreign_manifest['integrity']['manifest_sha256']}.json"
    )
    replacement.write_text(encoded, encoding="utf-8", newline="\n")

    with pytest.raises(ContractValidationError, match="foreign scoring handoff"):
        validate_evaluation_workflow_component_package_v1(
            root, context.scoring_handoff, require_terminal=True
        )


def test_package_rejects_self_hashed_receipt_with_foreign_handoff_ref(
    tmp_path: Path,
) -> None:
    sources = _sources()
    manifest, preflight, overlays = _manifest_and_preflight(sources)
    root = tmp_path / "benchmark"
    context = _context()
    _run(
        root,
        manifest,
        preflight,
        overlays,
        _runtimes(root, sources, [_Predictor(0.5) for _ in sources]),
        workflow_context=context,
    )
    receipt = json.loads((root / "scoring_receipt.json").read_text(encoding="utf-8"))
    foreign_receipt = build_scoring_receipt_v1(
        context.scoring_handoff,
        handoff_artifact_ref="foreign/renamed_handoff.json",
        evaluation_component_run_id=receipt["evaluation_component_run_id"],
        evaluation_component_attempt_id=receipt["evaluation_component_attempt_id"],
        accepted_at=receipt["accepted_at"],
        producer_code_commit=receipt["producer"]["code_commit"],
        status=receipt["status"],
        rejection_code=receipt["rejection_code"],
    )
    (root / "scoring_receipt.json").write_text(
        json.dumps(
            foreign_receipt, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
        + "\n",
        encoding="utf-8",
        newline="\n",
    )

    with pytest.raises(ContractValidationError, match="foreign scoring handoff"):
        validate_evaluation_workflow_component_package_v1(
            root, context.scoring_handoff, require_terminal=True
        )
