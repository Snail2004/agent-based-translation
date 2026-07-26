from __future__ import annotations

import json
from pathlib import Path

import pytest

import pipeline.prepass.d2l_translation_component_runner_v1 as runner_module
from pipeline.prepass.d2l_resume_checkpoint_receipt_v1 import (
    D2LResumeCheckpointReceiptError,
    RECEIPT_REF,
    VALIDATION_MODE,
    validate_resume_checkpoint_receipt,
)
from pipeline.prepass.d2l_translation_component_runner_v1 import (
    ComponentPlan,
    D2LTranslationComponentRunner,
)
from pipeline.tests.test_d2l_translation_component_runner_v1 import (
    _plan,
    _write_payloads,
)


def _paused_component(tmp_path: Path) -> tuple[Path, ComponentPlan]:
    root = tmp_path / "component"
    _write_payloads(root, attempt_id=2)
    plan = ComponentPlan.from_mapping(_plan(attempt_id=2))
    D2LTranslationComponentRunner(
        plan,
        root,
        stop_after_stage="candidate_index",
    ).run()
    return root, plan


def test_pause_writes_valid_resume_checkpoint_receipt(
    tmp_path: Path,
) -> None:
    root, _plan_row = _paused_component(tmp_path)

    validation = validate_resume_checkpoint_receipt(root)

    assert (root / RECEIPT_REF).is_file()
    assert validation["validation_mode"] == VALIDATION_MODE
    assert validation["component_attempt_id"] == 1
    assert validation["terminal_event"] is None
    assert validation["checkpoint_reference_count"] == 1
    assert validation["artifact_count"] == 0
    assert validation["event_writer_summary"] == {
        "last_component_seq": validation["event_count"],
        "last_component_attempt_id": 1,
        "terminal_event": None,
    }


def test_resume_uses_checkpoint_receipt_without_full_prefix_validation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, plan = _paused_component(tmp_path)
    original = runner_module.validate_translation_component_package
    paused_full_validation_calls = 0

    def reject_paused_full_validation(*args, **kwargs):
        nonlocal paused_full_validation_calls
        manifest = json.loads(
            (root / "component_manifest.json").read_text(encoding="utf-8")
        )
        if manifest["status"] == "paused":
            paused_full_validation_calls += 1
            raise AssertionError("paused prefix was fully rescanned")
        return original(*args, **kwargs)

    monkeypatch.setattr(
        runner_module,
        "validate_translation_component_package",
        reject_paused_full_validation,
    )

    result = D2LTranslationComponentRunner(plan, root).run(resume=True)

    assert result["terminal_event"] == "run_done"
    assert result["component_attempt_id"] == 2
    assert paused_full_validation_calls == 0


def test_receipt_rejects_event_tail_drift(tmp_path: Path) -> None:
    root, _plan_row = _paused_component(tmp_path)
    with (root / "events.jsonl").open("a", encoding="utf-8") as handle:
        handle.write("{}\n")

    with pytest.raises(
        D2LResumeCheckpointReceiptError,
        match="events filesystem identity drift",
    ):
        validate_resume_checkpoint_receipt(root)
