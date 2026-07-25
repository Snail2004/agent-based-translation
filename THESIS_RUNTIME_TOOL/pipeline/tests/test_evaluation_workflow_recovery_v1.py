import json
from pathlib import Path

import pytest

import pipeline.eval.workflow_recovery_v1 as recovery_module
from pipeline.eval.contracts_v1 import ContractValidationError
from pipeline.eval.workflow_recovery_v1 import (
    EvaluationWorkflowRecoveryStoreV1,
    build_evaluation_recovery_assignment_v1,
    build_evaluation_work_descriptor_v1,
    classify_evaluation_failure_v1,
)


_HASH = "a" * 64
_CREATED_AT = "2026-07-26T12:00:00Z"
_COMMIT = "b" * 40


def _assignment(**overrides):
    values = {
        "workflow_run_id": "workflow_fixture_001",
        "component_run_id": "evalcomp_fixture_001",
        "input_set_sha256": _HASH,
        "settings_sha256": _HASH,
        "evaluation_profile_sha256": _HASH,
        "stage_plan_sha256": _HASH,
        "sampling_sha256": _HASH,
        "semantic_contract_sha256": _HASH,
    }
    values.update(overrides)
    return build_evaluation_recovery_assignment_v1(**values)


def _descriptor():
    return build_evaluation_work_descriptor_v1(
        stage_id="chapter_fixture",
        chapter_id="fixture",
        scorer_id="sf_bt",
        arm_ids=["s0", "s1"],
        presentation_id="presentation_fixture",
        orientation="forward",
        input_bindings=[{"artifact_ref": "handoff.json", "sha256": _HASH}],
        evaluation_profile_sha256=_HASH,
        prompt_sha256=_HASH,
        schema_sha256=_HASH,
        validator_sha256=_HASH,
        model_id="fixture-model",
        provider_family="fixture",
        output_mode="prompt_validated",
        logical_request_id="logical_fixture",
    )


def _store(root: Path, *, failure_injector=None, assignment=None):
    return EvaluationWorkflowRecoveryStoreV1(
        root,
        assignment=_assignment() if assignment is None else assignment,
        generated_at=_CREATED_AT,
        producer_code_commit=_COMMIT,
        failure_injector=failure_injector,
    )


def _write_all_boundaries(store: EvaluationWorkflowRecoveryStoreV1):
    descriptor = _descriptor()
    work_id = store.begin_work(
        descriptor,
        component_attempt_id="evalcomp_attempt_0001",
        component_attempt_index=1,
    )
    store.record_physical_attempt(
        work_id=work_id,
        physical_attempt_id="physical_fixture_0001",
        component_attempt_id="evalcomp_attempt_0001",
        component_attempt_index=1,
        seal_binding={"seal_sha256": _HASH, "model_id": "fixture-model"},
    )
    for event, boundary in (
        ("usage_recorded", "usage_error"),
        ("error_recorded", "usage_error"),
        ("response_recorded", "response"),
        ("validation_recorded", "validation"),
        ("artifact_recorded", "artifact"),
    ):
        store.record_boundary(
            event=event,
            work_id=work_id,
            component_attempt_id="evalcomp_attempt_0001",
            component_attempt_index=1,
            physical_attempt_id="physical_fixture_0001",
            payload={"event": event, "value": 1},
            boundary=boundary,
        )
    store.accept_work(
        work_id=work_id,
        component_attempt_id="evalcomp_attempt_0001",
        component_attempt_index=1,
        physical_attempt_id="physical_fixture_0001",
        artifact_binding={"artifact_ref": "artifacts/fixture.json", "sha256": _HASH},
    )
    store.write_checkpoint(
        component_attempt_id="evalcomp_attempt_0001",
        component_attempt_index=1,
        component_seq=7,
        artifact_index_sha256=_HASH,
        usage_snapshot_sha256=None,
    )
    return work_id


@pytest.mark.parametrize(
    "target",
    [
        "intent",
        "physical_seal",
        "usage_error",
        "response",
        "validation",
        "artifact",
        "accepted",
        "ledger",
        "checkpoint",
    ],
)
def test_crash_after_each_durable_boundary_reopens_and_replays_once(
    tmp_path: Path, target: str
) -> None:
    fired = False

    def inject(boundary: str) -> None:
        nonlocal fired
        if not fired and boundary == target:
            fired = True
            raise OSError(f"synthetic crash at {boundary}")

    root = tmp_path / target
    store = _store(root, failure_injector=inject)
    with pytest.raises(OSError, match=f"synthetic crash at {target}"):
        _write_all_boundaries(store)

    reopened = _store(root)
    work_id = _write_all_boundaries(reopened)
    reopened.validate(require_checkpoint=True)
    assert reopened.ledger["accepted_work_ids"] == [work_id]
    assert len(reopened.ledger["works"]) == 1
    assert len(
        [
            row
            for row in reopened.records
            if row["event"] == "physical_attempt_sealed"
        ]
    ) == 1
    assert len(reopened.latest_checkpoint and [reopened.latest_checkpoint] or []) == 1


def test_physical_attempt_id_cannot_be_reused_with_changed_seal(tmp_path: Path) -> None:
    store = _store(tmp_path / "component")
    work_id = store.begin_work(
        _descriptor(),
        component_attempt_id="evalcomp_attempt_0001",
        component_attempt_index=1,
    )
    kwargs = {
        "work_id": work_id,
        "physical_attempt_id": "physical_fixture_0001",
        "component_attempt_id": "evalcomp_attempt_0001",
        "component_attempt_index": 1,
    }
    store.record_physical_attempt(
        **kwargs, seal_binding={"seal_sha256": _HASH, "model_id": "one"}
    )
    with pytest.raises(ContractValidationError, match="reused"):
        store.record_physical_attempt(
            **kwargs, seal_binding={"seal_sha256": "c" * 64, "model_id": "two"}
        )


def test_resume_keeps_work_identity_and_advances_component_attempt(tmp_path: Path) -> None:
    root = tmp_path / "component"
    store = _store(root)
    work_id = _write_all_boundaries(store)
    store.mark_halted(
        work_id=work_id,
        component_attempt_id="evalcomp_attempt_0001",
        component_attempt_index=1,
        category="operational",
        incident_id=None,
        reason_code="network_timeout",
    )
    reopened = _store(root)
    reopened.resume(
        component_attempt_id="evalcomp_attempt_0002",
        component_attempt_index=2,
    )
    assert reopened.ledger["works"][0]["work_id"] == work_id
    assert any(row["event"] == "component_resumed" for row in reopened.records)
    reopened.validate(require_checkpoint=True)


def test_incident_is_redacted_and_unrelated_files_do_not_participate(
    tmp_path: Path,
) -> None:
    root = tmp_path / "component"
    unrelated = tmp_path / "unrelated_repo_file.txt"
    unrelated.write_text("this file is outside the component", encoding="utf-8")
    store = _store(root)
    incident_id = store.record_incident(
        error=RuntimeError(
            "authorization=SECRET token=TOPSECRET at C:\\secret\\private.json"
        ),
        category="operational",
        reason_code="runtime_bug",
        component_attempt_id="evalcomp_attempt_0001",
        component_attempt_index=1,
        stage_id=None,
        work_id=None,
    )
    diagnostic = json.loads(
        (root / "recovery" / "diagnostics" / f"{incident_id}.json").read_text(
            encoding="utf-8"
        )
    )
    safe_message = diagnostic["safe_message"]
    assert "SECRET" not in safe_message
    assert "TOPSECRET" not in safe_message
    assert "private.json" not in safe_message
    store.validate()
    assert unrelated.is_file()


def test_assignment_drift_fails_closed(tmp_path: Path) -> None:
    root = tmp_path / "component"
    assignment = _assignment()
    _store(root, assignment=assignment)
    with pytest.raises(ContractValidationError, match="immutable|changed|binding"):
        _store(
            root,
            assignment=_assignment(settings_sha256="c" * 64),
        )


def test_resealed_ledger_projection_drift_is_rejected(tmp_path: Path) -> None:
    root = tmp_path / "component"
    store = _store(root)
    work_id = _write_all_boundaries(store)
    ledger_path = root / "recovery" / "work_ledger.json"
    ledger = json.loads(ledger_path.read_text(encoding="utf-8"))
    ledger["works"][0]["state"] = "halted"
    ledger["works"][0]["failure_category"] = "operational"
    ledger["accepted_work_ids"] = []
    ledger["halted_work_ids"] = [work_id]
    ledger["integrity"]["ledger_sha256"] = "0" * 64
    ledger = recovery_module.seal_payload(
        ledger,
        policy=recovery_module._POLICY,
        hash_path=("integrity", "ledger_sha256"),
    )
    ledger_path.write_text(
        json.dumps(ledger, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        + "\n",
        encoding="utf-8",
        newline="\n",
    )
    with pytest.raises(ContractValidationError, match="projection"):
        store.validate(require_checkpoint=True)


def _rewrite_journal_chain(root: Path, mutate) -> None:
    journal_root = root / "recovery" / "journal_records"
    rows = [
        json.loads(path.read_text(encoding="utf-8"))
        for path in sorted(journal_root.glob("*.json"))
    ]
    for path in journal_root.glob("*.json"):
        path.unlink()
    previous = None
    for index, row in enumerate(rows):
        mutate(row, index)
        row["previous_journal_sha256"] = previous
        row["integrity"]["journal_sha256"] = "0" * 64
        sealed = recovery_module.seal_payload(
            row,
            policy=recovery_module._POLICY,
            hash_path=("integrity", "journal_sha256"),
        )
        digest = sealed["integrity"]["journal_sha256"]
        (journal_root / f"{sealed['sequence']:08d}_{digest}.json").write_text(
            json.dumps(
                sealed,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n",
            encoding="utf-8",
            newline="\n",
        )
        previous = digest


def test_resealed_foreign_journal_binding_is_rejected(tmp_path: Path) -> None:
    root = tmp_path / "component"
    store = _store(root)
    _write_all_boundaries(store)
    _rewrite_journal_chain(
        root,
        lambda row, _index: row.__setitem__(
            "workflow_run_id", "workflow_foreign"
        ),
    )
    with pytest.raises(ContractValidationError, match="another component"):
        store.validate(require_checkpoint=True)


def test_resealed_checkpoint_state_drift_is_rejected(tmp_path: Path) -> None:
    root = tmp_path / "component"
    store = _store(root)
    work_id = _write_all_boundaries(store)
    checkpoint_root = root / "recovery" / "checkpoints"
    checkpoint_path = next(checkpoint_root.glob("*.json"))
    checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8"))
    old_hash = checkpoint["integrity"]["checkpoint_sha256"]
    checkpoint["accepted_work_ids"] = []
    checkpoint["pending_work_ids"] = [work_id]
    checkpoint["integrity"]["checkpoint_sha256"] = "0" * 64
    checkpoint = recovery_module.seal_payload(
        checkpoint,
        policy=recovery_module._POLICY,
        hash_path=("integrity", "checkpoint_sha256"),
    )
    new_hash = checkpoint["integrity"]["checkpoint_sha256"]
    checkpoint_path.unlink()
    (checkpoint_root / f"{new_hash}.json").write_text(
        json.dumps(
            checkpoint,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n",
        encoding="utf-8",
        newline="\n",
    )

    def mutate(row, _index):
        if (
            row["event"] == "checkpoint_recorded"
            and row["payload"].get("checkpoint_sha256") == old_hash
        ):
            row["payload"]["checkpoint_sha256"] = new_hash
            row["payload"]["checkpoint_ref"] = (
                f"recovery/checkpoints/{new_hash}.json"
            )

    _rewrite_journal_chain(root, mutate)
    with pytest.raises(ContractValidationError, match="checkpoint state"):
        store.validate(require_checkpoint=True)


def test_hash_drift_is_classified_as_integrity_failure() -> None:
    classification = classify_evaluation_failure_v1(
        ContractValidationError("artifact_hash", "$.artifact", "drift")
    )
    assert classification.category == "integrity"
    assert classification.resume_available is False
