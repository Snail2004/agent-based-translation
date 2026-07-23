from __future__ import annotations

import json
from pathlib import Path

import pytest

from pipeline.llm_backend import credential_commitment
from pipeline.workflow_replay.contracts_v1 import (
    canonical_json_bytes,
    canonical_sha256,
    physical_sha256,
)
from pipeline.workflow_replay.evaluation_server_runtime_v1 import (
    EvaluationServerRuntimeError,
    build_evaluation_server_runtime_v1,
    validate_evaluation_server_runtime_config_v1,
)


def _write_json(path: Path, value: dict) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(canonical_json_bytes(value) + b"\n")
    return path


def _fixture(tmp_path: Path) -> Path:
    tmp_path.mkdir(parents=True, exist_ok=True)
    python_path = tmp_path / "python.exe"
    checkpoint_path = tmp_path / "model.ckpt"
    credential_path = tmp_path / "credential.txt"
    python_path.write_bytes(b"python")
    checkpoint_path.write_bytes(b"checkpoint")
    credential = "evaluation-secret"
    credential_path.write_text(credential + "\n", encoding="utf-8", newline="\n")
    source = {
        "schema_id": "ApiSourceV1",
        "schema_version": "1.0.0",
        "source_id": "evaluation-source",
        "source_revision": "evaluation-source-v1",
        "source_class": "third_party_proxy",
        "protocol": "google_genai_generate_content",
        "base_url": "https://example.invalid/v1beta",
        "credential_ref": "evaluation.credential.v1",
        "credential_commitment": credential_commitment(credential),
        "request_policy": {
            "timeout_seconds": 30,
            "max_response_bytes": 1000000,
            "max_error_body_bytes": 1000,
        },
    }
    profile = {"schema_id": "EvaluationLlmProfileV1", "profile_id": "eval-v1"}
    capability = {
        "schema_id": "CapabilityEvidenceV1",
        "role_id": "evaluation.pj_judge",
    }
    source_path = _write_json(tmp_path / "source.json", source)
    profile_path = _write_json(tmp_path / "profile.json", profile)
    capability_path = _write_json(tmp_path / "capability.json", capability)
    payload = {
        "schema_id": "EvaluationServerRuntimeConfigV1",
        "schema_version": "1.0.0",
        "cometkiwi": {
            "python_executable": str(python_path.resolve()),
            "checkpoint_path": str(checkpoint_path.resolve()),
            "checkpoint_sha256": physical_sha256(checkpoint_path.read_bytes()),
            "package_name": "unbabel-comet",
            "package_version": "2.2.7",
            "device": "cpu",
            "batch_size": 8,
            "timeout_seconds": 1800,
            "max_rows_per_worker": None,
        },
        "llm": {
            "profile": {
                "path": str(profile_path.resolve()),
                "sha256": physical_sha256(profile_path.read_bytes()),
            },
            "api_sources": [
                {
                    "path": str(source_path.resolve()),
                    "sha256": physical_sha256(source_path.read_bytes()),
                }
            ],
            "capability_evidence": [
                {
                    "path": str(capability_path.resolve()),
                    "sha256": physical_sha256(capability_path.read_bytes()),
                }
            ],
            "credential_ref": "evaluation.credential.v1",
            "credential_file": str(credential_path.resolve()),
            "cache_mode": "read_write",
            "cost_fact": None,
        },
    }
    payload["integrity"] = {"config_sha256": canonical_sha256(payload)}
    return _write_json(tmp_path / "runtime.json", payload)


def test_validates_public_authority_without_reading_secret(
    tmp_path: Path,
) -> None:
    config_path = _fixture(tmp_path)
    secret_path = tmp_path / "credential.txt"
    secret_path.write_bytes(b"\xff")

    loaded = validate_evaluation_server_runtime_config_v1(config_path)

    assert loaded["schema_id"] == "EvaluationServerRuntimeConfigV1"
    assert loaded["cometkiwi"]["checkpoint_sha256"] == physical_sha256(
        (tmp_path / "model.ckpt").read_bytes()
    )


def test_builds_process_local_runtime_and_never_serializes_secret(
    tmp_path: Path,
) -> None:
    config_path = _fixture(tmp_path)

    runtime = build_evaluation_server_runtime_v1(config_path)

    assert runtime.local_sf_qe_checkpoint_sha256 is not None
    assert runtime.credential_provider is not None
    assert (
        runtime.credential_provider.resolve("evaluation.credential.v1")
        == "evaluation-secret"
    )
    assert b"evaluation-secret" not in config_path.read_bytes()


def test_rejects_config_and_artifact_drift(tmp_path: Path) -> None:
    config_path = _fixture(tmp_path)
    value = json.loads(config_path.read_text(encoding="utf-8"))
    value["cometkiwi"]["batch_size"] = 9
    _write_json(config_path, value)

    with pytest.raises(
        EvaluationServerRuntimeError, match="hash is inconsistent"
    ):
        validate_evaluation_server_runtime_config_v1(config_path)

    config_path = _fixture(tmp_path / "artifact")
    (tmp_path / "artifact" / "profile.json").write_bytes(b"{}")
    with pytest.raises(
        EvaluationServerRuntimeError, match="bytes differ"
    ):
        validate_evaluation_server_runtime_config_v1(config_path)


def test_rejects_foreign_credential_reference(tmp_path: Path) -> None:
    config_path = _fixture(tmp_path)
    value = json.loads(config_path.read_text(encoding="utf-8"))
    value["llm"]["credential_ref"] = "foreign.credential"
    value["integrity"]["config_sha256"] = canonical_sha256(
        {key: row for key, row in value.items() if key != "integrity"}
    )
    _write_json(config_path, value)

    with pytest.raises(
        EvaluationServerRuntimeError, match="credential reference"
    ):
        validate_evaluation_server_runtime_config_v1(config_path)
