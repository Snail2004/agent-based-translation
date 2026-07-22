from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
import subprocess

import pytest

from pipeline.eval import cometkiwi_worker_v1
from pipeline.eval.cometkiwi_subprocess_v1 import (
    COMETKIWI_BATCH_RESPONSE_SCHEMA_ID,
    COMETKIWI_RUNTIME_DESCRIPTION_SCHEMA_ID,
    CometKiwiSubprocessPredictorV1,
    validate_cometkiwi_batch_request_v1,
    validate_cometkiwi_batch_response_v1,
)
from pipeline.eval.contracts_v1 import ContractValidationError


def _files(tmp_path: Path) -> tuple[Path, Path, str]:
    python = tmp_path / "python.exe"
    checkpoint = tmp_path / "model.ckpt"
    python.write_bytes(b"fixture-python")
    checkpoint.write_bytes(b"fixture-checkpoint")
    digest = hashlib.sha256(checkpoint.read_bytes()).hexdigest()
    return python, checkpoint, digest


def test_predictor_sends_only_source_mt_rows_and_returns_finite_scores(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    python, checkpoint, _ = _files(tmp_path)
    observed: dict = {}

    def fake_run(command, **kwargs):
        observed["command"] = command
        observed["kwargs"] = kwargs
        return subprocess.CompletedProcess(
            command,
            0,
            stdout=json.dumps(
                {
                    "schema_id": COMETKIWI_BATCH_RESPONSE_SCHEMA_ID,
                    "scores": [0.7, 0.8],
                }
            ),
            stderr="",
        )

    monkeypatch.setattr(subprocess, "run", fake_run)
    predictor = CometKiwiSubprocessPredictorV1(
        python_executable=python,
        checkpoint_path=checkpoint,
        timeout_seconds=30,
    )
    rows = [{"src": "Source one", "mt": "Dich mot"}, {"src": "Source two", "mt": "Dich hai"}]
    before = copy.deepcopy(rows)

    assert predictor(rows, 4) == [0.7, 0.8]
    assert rows == before
    request = json.loads(observed["kwargs"]["input"])
    assert request == {"schema_id": "CometKiwiBatchRequestV1", "rows": rows}
    rendered = observed["kwargs"]["input"]
    assert "gold" not in rendered and "arm_id" not in rendered and "score" not in rendered
    assert observed["command"][:3] == [str(python.resolve()), "-m", "pipeline.eval.cometkiwi_worker_v1"]
    assert "shell" not in observed["kwargs"]
    assert observed["kwargs"]["env"]["HF_HUB_OFFLINE"] == "1"
    assert observed["kwargs"]["env"]["TRANSFORMERS_OFFLINE"] == "1"
    assert observed["kwargs"]["env"]["HF_DATASETS_OFFLINE"] == "1"
    assert observed["kwargs"]["env"]["WANDB_MODE"] == "disabled"


def test_predictor_can_shard_rows_across_bounded_fresh_workers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    python, checkpoint, _ = _files(tmp_path)
    observed_requests: list[dict] = []

    def fake_run(command, **kwargs):
        request = json.loads(kwargs["input"])
        observed_requests.append(request)
        return subprocess.CompletedProcess(
            command,
            0,
            stdout=json.dumps(
                {
                    "schema_id": COMETKIWI_BATCH_RESPONSE_SCHEMA_ID,
                    "scores": [0.5 + 0.1 * len(observed_requests)],
                }
            ),
            stderr="",
        )

    monkeypatch.setattr(subprocess, "run", fake_run)
    predictor = CometKiwiSubprocessPredictorV1(
        python_executable=python,
        checkpoint_path=checkpoint,
        timeout_seconds=30,
        max_rows_per_worker=1,
    )
    rows = [
        {"src": "Source one", "mt": "Dich mot"},
        {"src": "Source two", "mt": "Dich hai"},
        {"src": "Source three", "mt": "Dich ba"},
    ]

    assert predictor(rows, 1) == pytest.approx([0.6, 0.7, 0.8])
    assert [request["rows"] for request in observed_requests] == [
        [row] for row in rows
    ]


def test_runtime_description_binds_exact_checkpoint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    python, checkpoint, digest = _files(tmp_path)

    def fake_run(command, **kwargs):
        return subprocess.CompletedProcess(
            command,
            0,
            stdout=json.dumps(
                {
                    "schema_id": COMETKIWI_RUNTIME_DESCRIPTION_SCHEMA_ID,
                    "package_name": "unbabel-comet",
                    "package_version": "2.2.7",
                    "python_version": "3.11.9",
                    "device": "cpu",
                    "checkpoint_sha256": digest,
                }
            ),
            stderr="",
        )

    monkeypatch.setattr(subprocess, "run", fake_run)
    predictor = CometKiwiSubprocessPredictorV1(
        python_executable=python, checkpoint_path=checkpoint
    )

    description = predictor.describe_runtime()
    assert description["checkpoint_sha256"] == digest
    assert description["package_version"] == "2.2.7"


def test_checkpoint_symlink_keeps_snapshot_path_for_comet_sidecars(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    python = tmp_path / "python.exe"
    python.write_bytes(b"fixture-python")
    blob = tmp_path / "blobs" / "checkpoint-blob"
    blob.parent.mkdir()
    blob.write_bytes(b"fixture-checkpoint")
    checkpoint = tmp_path / "snapshots" / "revision" / "model.ckpt"
    checkpoint.parent.mkdir(parents=True)
    try:
        checkpoint.symlink_to(blob)
    except OSError as exc:
        pytest.skip(f"filesystem cannot create a file symlink: {exc}")
    digest = hashlib.sha256(blob.read_bytes()).hexdigest()
    observed: dict = {}

    def fake_run(command, **kwargs):
        observed["command"] = command
        return subprocess.CompletedProcess(
            command,
            0,
            stdout=json.dumps(
                {
                    "schema_id": COMETKIWI_RUNTIME_DESCRIPTION_SCHEMA_ID,
                    "package_name": "unbabel-comet",
                    "package_version": "2.2.7",
                    "python_version": "3.11.9",
                    "device": "cpu",
                    "checkpoint_sha256": digest,
                }
            ),
            stderr="",
        )

    monkeypatch.setattr(subprocess, "run", fake_run)
    predictor = CometKiwiSubprocessPredictorV1(
        python_executable=python,
        checkpoint_path=checkpoint,
    )
    predictor.describe_runtime()

    logical_checkpoint = Path(checkpoint.absolute())
    assert logical_checkpoint != blob.resolve()
    assert observed["command"][4] == str(logical_checkpoint)
    assert cometkiwi_worker_v1._logical_checkpoint_path(
        str(logical_checkpoint)
    ) == logical_checkpoint


def test_missing_checkpoint_fails_before_subprocess(tmp_path: Path) -> None:
    python = tmp_path / "python.exe"
    python.write_bytes(b"fixture-python")
    with pytest.raises(ContractValidationError, match="required file is missing"):
        CometKiwiSubprocessPredictorV1(
            python_executable=python,
            checkpoint_path=tmp_path / "missing.ckpt",
        )


def test_worker_failure_is_safe_and_does_not_echo_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    python, checkpoint, _ = _files(tmp_path)

    def fake_run(command, **kwargs):
        return subprocess.CompletedProcess(
            command, 2, stdout="", stderr="private source text"
        )

    monkeypatch.setattr(subprocess, "run", fake_run)
    predictor = CometKiwiSubprocessPredictorV1(
        python_executable=python, checkpoint_path=checkpoint
    )
    with pytest.raises(ContractValidationError) as exc_info:
        predictor([{"src": "private source text", "mt": "private target"}], 1)

    assert exc_info.value.code == "cometkiwi_worker_failure"
    assert "private" not in str(exc_info.value)


@pytest.mark.parametrize(
    "payload",
    [
        {"schema_id": COMETKIWI_BATCH_RESPONSE_SCHEMA_ID, "scores": [float("nan")]},
        {"schema_id": COMETKIWI_BATCH_RESPONSE_SCHEMA_ID, "scores": [1.1]},
        {"schema_id": COMETKIWI_BATCH_RESPONSE_SCHEMA_ID, "scores": []},
        {"schema_id": COMETKIWI_BATCH_RESPONSE_SCHEMA_ID, "scores": [0.5], "winner": "S1"},
    ],
)
def test_response_contract_rejects_nonfinite_range_count_and_unknown_keys(payload):
    with pytest.raises(ContractValidationError):
        validate_cometkiwi_batch_response_v1(payload, expected_count=1)


def test_request_contract_is_closed_and_nonempty() -> None:
    with pytest.raises(ContractValidationError):
        validate_cometkiwi_batch_request_v1(
            {
                "schema_id": "CometKiwiBatchRequestV1",
                "rows": [{"src": "", "mt": "target"}],
            }
        )
