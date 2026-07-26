from __future__ import annotations

import copy
import json
from dataclasses import replace
from pathlib import Path

import pytest

from pipeline.eval.contracts_v1 import ContractValidationError
from pipeline.eval.workflow_component_writer_v1 import (
    EvaluationWorkflowComponentWriterV1,
    benchmark_workflow_stages_v1,
)
from pipeline.eval.workflow_console_projection_v1 import (
    validate_evaluation_console_projection_chain_v1,
    validate_evaluation_console_projection_v1,
)
from pipeline.tests.test_evaluation_workflow_component_writer_v1 import _context


def _writer(
    root: Path,
    *,
    component_run_id: str = "evalcomp_console_fixture_001",
    allow_create: bool = True,
) -> EvaluationWorkflowComponentWriterV1:
    context = replace(_context(), component_run_id=component_run_id)
    return EvaluationWorkflowComponentWriterV1(
        root,
        context,
        generated_at="2026-07-26T00:00:00Z",
        producer_code_commit="c" * 40,
        stages=benchmark_workflow_stages_v1(("d2l_preliminaries",)),
        allow_create=allow_create,
    )


def _projection_bytes(root: Path) -> tuple[bytes, ...]:
    return tuple(
        path.read_bytes()
        for path in sorted((root / "console_projections").glob("*.json"))
    )


def _projection_rows(package: dict) -> list[dict]:
    return [
        row
        for projection in package["console_projections"]
        for row in projection["rows"]
    ]


def test_retry_events_are_collapsed_into_one_producer_sealed_summary(
    tmp_path: Path,
) -> None:
    root = tmp_path / "component"
    writer = _writer(root)
    writer.start_stage("preflight", work_total=1, work_unit="gate")
    for physical_attempt_index, reason_code in (
        (1, "provider_timeout"),
        (2, "provider_timeout"),
    ):
        writer.append_event(
            "retry",
            stage_id="preflight",
            agent="evaluation_preflight",
            severity="warning",
            payload={
                "retry_kind": "transport",
                "logical_request_id": "evaluation.preflight.fixture",
                "physical_attempt_index": physical_attempt_index,
                "reason_code": reason_code,
            },
        )
    writer.complete_stage("preflight")

    package = writer.validate_package()
    rows = _projection_rows(package)
    retry_rows = [row for row in rows if row["event"] == "retry_summary"]

    assert len(package["console_projections"]) == len(package["events"])
    assert sum(event["event"] == "retry" for event in package["events"]) == 2
    assert len(retry_rows) == 1
    assert retry_rows[0]["severity"] == "warning"
    assert retry_rows[0]["detail"]["retry"] == {
        "retry_kind": "transport",
        "logical_request_id": "evaluation.preflight.fixture",
        "retry_count": 2,
        "physical_attempt_indexes": [1, 2],
        "reason_codes": ["provider_timeout"],
        "outcome": "stage_succeeded",
    }
    assert not any(row["event"] == "retry" for row in rows)

    before = _projection_bytes(root)
    reopened = _writer(root, allow_create=False)
    assert reopened.validate_package()["console_projections"] == package[
        "console_projections"
    ]
    assert _projection_bytes(root) == before


def test_internal_incident_emits_one_redacted_amber_pause(
    tmp_path: Path,
) -> None:
    root = tmp_path / "component"
    writer = _writer(root)
    writer.start_stage("preflight", work_total=1, work_unit="gate")
    incident_id = writer.record_internal_incident(
        RuntimeError(
            "authorization: Bearer plaintext-token "
            r"C:\private\workspace\scorer.py"
        ),
        category="operational",
        reason_code="runner_internal_interruption",
        stage_id="preflight",
        work_id=None,
    )
    assert incident_id is not None
    writer.halt(
        reason_code="runner_internal_interruption",
        reason_category="operational",
        incident_id=incident_id,
        current_stage_id="preflight",
        current_work_id="evaluation.preflight.fixture",
    )

    package = writer.validate_package()
    rows = _projection_rows(package)
    pauses = [row for row in rows if row["event"] == "stage_paused"]
    public_bytes = json.dumps(
        package["console_projections"],
        ensure_ascii=False,
        sort_keys=True,
    ).casefold()

    assert len(pauses) == 1
    assert pauses[0]["severity"] == "warning"
    assert pauses[0]["detail"]["reason"]["incident_id"] == incident_id
    assert pauses[0]["detail"]["reason"]["resume_available"] is True
    assert not any(row["event"] == "component_halted" for row in rows)
    assert package["console_projections"][-1]["prefixes"]["diagnostics"][
        "record_count"
    ] == 1
    assert "plaintext-token" not in public_bytes
    assert "private\\workspace" not in public_bytes
    assert "runtimeerror" not in public_bytes


def test_terminal_integrity_failure_emits_one_red_row(
    tmp_path: Path,
) -> None:
    writer = _writer(tmp_path / "component")
    writer.start_stage("preflight", work_total=1, work_unit="gate")
    writer.failed(
        reason_code="accepted_lineage_unverifiable",
        reason_category="integrity",
        incident_id="inc_terminal_integrity",
        current_stage_id="preflight",
        current_work_id="evaluation.preflight.fixture",
    )

    package = writer.validate_package(require_terminal=True)
    rows = _projection_rows(package)
    failures = [row for row in rows if row["event"] == "component_failed"]

    assert len(failures) == 1
    assert failures[0]["severity"] == "error"
    assert failures[0]["detail"]["reason"]["resume_available"] is False
    assert failures[0]["detail"]["reason"]["reason_category"] == "integrity"
    assert not any(row["event"] == "stage_paused" for row in rows)


def test_projection_chain_rejects_tamper_foreign_component_and_future_prefix(
    tmp_path: Path,
) -> None:
    root = tmp_path / "component"
    writer = _writer(root)
    writer.start_stage("preflight", work_total=1, work_unit="gate")
    writer.complete_stage("preflight")
    package = writer.validate_package()
    projections = package["console_projections"]
    events = package["events"]
    recovery_journal = package["recovery"]["journal"]

    tampered = copy.deepcopy(projections[-1])
    tampered["rows"][0]["label_key"] = "evaluation.tampered"
    with pytest.raises(
        ContractValidationError,
        match="console_(row_id|row_hash|projection_hash)",
    ):
        validate_evaluation_console_projection_v1(tampered)

    foreign = _writer(
        tmp_path / "foreign",
        component_run_id="evalcomp_console_foreign_001",
    )
    with pytest.raises(ContractValidationError, match="console_component_binding"):
        validate_evaluation_console_projection_chain_v1(
            projections,
            manifest=foreign.manifest,
            events=events,
            recovery_journal=recovery_journal,
            diagnostic_bindings=(),
        )

    maximum_bound_records = max(
        projection["prefixes"]["recovery_journal"]["record_count"]
        for projection in projections
    )
    assert maximum_bound_records > 0
    with pytest.raises(ContractValidationError, match="console_future_prefix"):
        validate_evaluation_console_projection_chain_v1(
            projections,
            manifest=package["manifest"],
            events=events,
            recovery_journal=recovery_journal[: maximum_bound_records - 1],
            diagnostic_bindings=(),
        )


def test_projection_package_rejects_physical_artifact_tamper(
    tmp_path: Path,
) -> None:
    root = tmp_path / "component"
    writer = _writer(root)
    writer.start_stage("preflight", work_total=1, work_unit="gate")
    writer.complete_stage("preflight")
    package = writer.validate_package()
    final_ref = package["artifact_index"]["artifacts"][-1]["artifact"][
        "artifact_ref"
    ]
    projection_path = root / final_ref
    projection_path.write_bytes(projection_path.read_bytes() + b" ")

    with pytest.raises(ContractValidationError, match="artifact"):
        writer.validate_package()
