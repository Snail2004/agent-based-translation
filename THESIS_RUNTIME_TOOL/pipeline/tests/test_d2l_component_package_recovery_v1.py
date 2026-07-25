from __future__ import annotations

import json
import sys
from hashlib import sha256
from pathlib import Path

import pytest

from pipeline.prepass import d2l_component_package_recovery_v1 as recovery
from pipeline.prepass.d2l_component_package_recovery_v1 import (
    D2LComponentPackageRecoveryError,
    D2LComponentPackageRecoveryRequestV1,
    recover_d2l_component_package_v1,
    validate_recovery_receipt_v1,
)
from pipeline.prepass.d2l_component_writer_lease_v1 import (
    D2LComponentWriterLease,
)
from pipeline.prepass.d2l_console_replay_contract_v1 import (
    STAGE_IDS,
    build_stage_plan,
    canonical_json_bytes,
    canonical_sha256,
    file_sha256,
    validate_translation_component_package,
    write_json,
)
from pipeline.prepass.d2l_translation_component_runner_v1 import (
    ComponentPlan,
    D2LTranslationComponentRunner,
    RUNNER_SCHEMA,
)


CONFIG_SHA = "2" * 64
GIT_COMMIT = "1" * 40
SNAPSHOT_SHA = "A" * 64


def _binding(ref: str, kind: str, schema: str) -> dict[str, str]:
    return {
        "artifact_ref": ref,
        "artifact_kind": kind,
        "schema_version": schema,
        "sha256": sha256(ref.encode("utf-8")).hexdigest().upper(),
        "sha256_kind": "physical",
    }


def _source_binding() -> dict[str, object]:
    return {
        "schema": "canonical_source_binding_v1",
        "document": _binding("src_document", "source_document", "document_v1"),
        "structure_manifest": _binding(
            "src_structure",
            "structure_manifest",
            "structure_manifest_v1",
        ),
        "asset_manifest": _binding(
            "src_assets",
            "asset_manifest",
            "asset_manifest_v1",
        ),
        "admitted_projection": _binding(
            "src_projection",
            "admitted_projection",
            "admitted_projection_v1",
        ),
        "normalization_receipt": _binding(
            "src_receipt",
            "normalization_receipt",
            "normalization_receipt_v1",
        ),
        "package_seal": _binding(
            "src_package_seal",
            "source_package_seal",
            "source_package_seal_v1",
        ),
    }


def _plan() -> ComponentPlan:
    units = {
        row["stage_id"]: row["progress"]["unit"]
        for row in build_stage_plan()
    }
    stages = [
        {
            "stage_id": stage_id,
            "producer": stage_id,
            "command": [sys.executable, "-c", "pass"],
            "cwd": None,
            "artifact_specs": [],
            "total": 1,
            "unit": units[stage_id],
            "work_id": f"work_{stage_id}",
            "mode": "execute",
            "timeout_seconds": 30,
            "receipt_ref": None,
        }
        for stage_id in STAGE_IDS
    ]
    return ComponentPlan.from_mapping(
        {
            "schema": RUNNER_SCHEMA,
            "workflow_run_id": "wf_recovery_test_v1",
            "component_run_id": "tr_recovery_test_v1",
            "pipeline_id": "d2l_terminology",
            "pipeline_version": "d2l_translation_component_runner_v1",
            "source_binding": _source_binding(),
            "config_sha256": CONFIG_SHA,
            "code_revision": GIT_COMMIT,
            "selected_chapter_ids": ["d2l_preliminaries"],
            "stages": stages,
            "scoring_handoff_fragment_ref": "scoring_handoff_fragment.json",
        }
    )


def _broken_fixture(tmp_path: Path) -> dict[str, object]:
    root = tmp_path / "component"
    D2LTranslationComponentRunner(
        _plan(),
        root,
        stop_after_stage="preflight",
    ).run()
    validation = validate_translation_component_package(
        root,
        require_terminal=False,
    )
    assert validation["component_attempt_id"] == 1

    authoritative_bytes = (root / "artifact_index.json").read_bytes()
    authoritative_sha = sha256(authoritative_bytes).hexdigest().upper()
    snapshot_dir = tmp_path / "parent" / "snapshots" / SNAPSHOT_SHA
    snapshot_dir.mkdir(parents=True)
    authoritative_path = snapshot_dir / "artifact_index.json"
    authoritative_path.write_bytes(authoritative_bytes)

    broken = json.loads(authoritative_bytes)
    broken["component_attempt_id"] = 2
    write_json(root / "artifact_index.json", broken)
    orphan = {
        "schema": "orphan_writer_probe_v1",
        "component_attempt_id": 2,
    }
    (root / "component_manifest.json.tmp").write_bytes(
        canonical_json_bytes(orphan)
    )
    with pytest.raises(Exception, match="component_attempt_id"):
        validate_translation_component_package(
            root,
            require_terminal=False,
        )

    request = D2LComponentPackageRecoveryRequestV1(
        component_root=root,
        transaction_root=tmp_path / "recovery_transactions",
        authoritative_index_file=authoritative_path,
        authoritative_index_sha256=authoritative_sha,
        parent_snapshot_ref=(
            "components/translation/tr_recovery_test_v1/"
            f"snapshots/{SNAPSHOT_SHA.lower()}/artifact_index.json"
        ),
        parent_snapshot_sha256=SNAPSHOT_SHA,
        parent_import_ordinal=30,
        expected_manifest_sha256=file_sha256(
            root / "component_manifest.json"
        ),
        expected_events_sha256=file_sha256(root / "events.jsonl"),
        expected_broken_index_sha256=file_sha256(
            root / "artifact_index.json"
        ),
        expected_manifest_temp_sha256=file_sha256(
            root / "component_manifest.json.tmp"
        ),
    )
    return {
        "root": root,
        "request": request,
        "authoritative_bytes": authoritative_bytes,
        "authoritative_sha": authoritative_sha,
    }


def test_recovery_preserves_broken_bytes_and_validates_package(
    tmp_path: Path,
) -> None:
    fixture = _broken_fixture(tmp_path)
    root = fixture["root"]
    request = fixture["request"]
    assert isinstance(root, Path)
    assert isinstance(request, D2LComponentPackageRecoveryRequestV1)
    manifest_before = (root / "component_manifest.json").read_bytes()
    events_before = (root / "events.jsonl").read_bytes()

    receipt = recover_d2l_component_package_v1(request)

    assert receipt["provider_call_count"] == 0
    assert receipt["semantic_replay_count"] == 0
    assert receipt["parent_import_ordinal"] == 30
    assert receipt["component_attempt_id"] == 1
    assert validate_recovery_receipt_v1(receipt) == receipt
    assert (root / "component_manifest.json").read_bytes() == manifest_before
    assert (root / "events.jsonl").read_bytes() == events_before
    assert not (root / "component_manifest.json.tmp").exists()
    assert (
        (root / "artifact_index.json").read_bytes()
        == fixture["authoritative_bytes"]
    )
    validation = validate_translation_component_package(
        root,
        require_terminal=False,
    )
    assert canonical_sha256(validation) == receipt[
        "post_package_validation_sha256"
    ]

    transaction_dir = (
        request.transaction_root / receipt["transaction_id"]
    )
    broken_ref = receipt["quarantine"]["broken_artifact_index_ref"]
    temp_ref = receipt["quarantine"]["orphan_manifest_temp_ref"]
    assert file_sha256(transaction_dir / broken_ref) == request.expected_broken_index_sha256
    assert file_sha256(transaction_dir / temp_ref) == request.expected_manifest_temp_sha256
    assert file_sha256(transaction_dir / "receipt.json") == sha256(
        canonical_json_bytes(receipt)
    ).hexdigest().upper()


def test_recovery_is_idempotent_after_commit(tmp_path: Path) -> None:
    fixture = _broken_fixture(tmp_path)
    request = fixture["request"]
    assert isinstance(request, D2LComponentPackageRecoveryRequestV1)
    first = recover_d2l_component_package_v1(request)
    second = recover_d2l_component_package_v1(request)
    assert second == first


def test_recovery_rejects_quarantine_drift_on_idempotent_call(
    tmp_path: Path,
) -> None:
    fixture = _broken_fixture(tmp_path)
    request = fixture["request"]
    assert isinstance(request, D2LComponentPackageRecoveryRequestV1)
    receipt = recover_d2l_component_package_v1(request)
    transaction_dir = request.transaction_root / receipt["transaction_id"]
    broken_ref = receipt["quarantine"]["broken_artifact_index_ref"]
    (transaction_dir / broken_ref).write_bytes(b"{}\n")

    with pytest.raises(
        D2LComponentPackageRecoveryError,
        match="quarantined broken artifact index hash drift",
    ):
        recover_d2l_component_package_v1(request)


def test_recovery_resumes_after_receipt_write_crash(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _broken_fixture(tmp_path)
    request = fixture["request"]
    root = fixture["root"]
    assert isinstance(request, D2LComponentPackageRecoveryRequestV1)
    assert isinstance(root, Path)
    original = recovery._write_absent_or_equal
    failed = False

    def crash_before_receipt(path: Path, value: bytes) -> None:
        nonlocal failed
        if path.name == "receipt.json" and not failed:
            failed = True
            raise OSError("simulated receipt write crash")
        original(path, value)

    monkeypatch.setattr(
        recovery,
        "_write_absent_or_equal",
        crash_before_receipt,
    )
    with pytest.raises(OSError, match="simulated receipt write crash"):
        recover_d2l_component_package_v1(request)
    assert file_sha256(root / "artifact_index.json") == request.authoritative_index_sha256
    assert not (root / "component_manifest.json.tmp").exists()

    monkeypatch.setattr(recovery, "_write_absent_or_equal", original)
    receipt = recover_d2l_component_package_v1(request)
    assert receipt["provider_call_count"] == 0
    validate_translation_component_package(root, require_terminal=False)


def test_recovery_rolls_back_when_owning_validation_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _broken_fixture(tmp_path)
    request = fixture["request"]
    root = fixture["root"]
    assert isinstance(request, D2LComponentPackageRecoveryRequestV1)
    assert isinstance(root, Path)
    original_validator = recovery.validate_translation_component_package
    calls = 0

    def reject_once(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise ValueError("simulated owning validator rejection")
        return original_validator(*args, **kwargs)

    monkeypatch.setattr(
        recovery,
        "validate_translation_component_package",
        reject_once,
    )
    with pytest.raises(ValueError, match="simulated owning validator"):
        recover_d2l_component_package_v1(request)
    assert file_sha256(root / "artifact_index.json") == request.expected_broken_index_sha256
    assert file_sha256(root / "component_manifest.json.tmp") == (
        request.expected_manifest_temp_sha256
    )


def test_recovery_rejects_active_component_writer(tmp_path: Path) -> None:
    fixture = _broken_fixture(tmp_path)
    request = fixture["request"]
    root = fixture["root"]
    assert isinstance(request, D2LComponentPackageRecoveryRequestV1)
    assert isinstance(root, Path)
    with D2LComponentWriterLease(root):
        with pytest.raises(
            D2LComponentPackageRecoveryError,
            match="held by another process",
        ):
            recover_d2l_component_package_v1(request)


def test_recovery_rejects_prestate_and_authority_drift(
    tmp_path: Path,
) -> None:
    fixture = _broken_fixture(tmp_path)
    request = fixture["request"]
    assert isinstance(request, D2LComponentPackageRecoveryRequestV1)

    bad_prestate = D2LComponentPackageRecoveryRequestV1(
        **{
            **request.__dict__,
            "expected_events_sha256": "F" * 64,
        }
    )
    with pytest.raises(
        D2LComponentPackageRecoveryError,
        match="events prestate hash drift",
    ):
        recover_d2l_component_package_v1(bad_prestate)

    request.authoritative_index_file.write_bytes(b"{}\n")
    with pytest.raises(
        D2LComponentPackageRecoveryError,
        match="authoritative index hash drift",
    ):
        recover_d2l_component_package_v1(request)


def test_recovery_rejects_foreign_snapshot_path_before_mutation(
    tmp_path: Path,
) -> None:
    fixture = _broken_fixture(tmp_path)
    request = fixture["request"]
    root = fixture["root"]
    assert isinstance(request, D2LComponentPackageRecoveryRequestV1)
    assert isinstance(root, Path)
    before = (root / "artifact_index.json").read_bytes()
    foreign = D2LComponentPackageRecoveryRequestV1(
        **{
            **request.__dict__,
            "parent_snapshot_sha256": "B" * 64,
        }
    )
    with pytest.raises(
        D2LComponentPackageRecoveryError,
        match="outside the supplied snapshot",
    ):
        recover_d2l_component_package_v1(foreign)
    assert (root / "artifact_index.json").read_bytes() == before
