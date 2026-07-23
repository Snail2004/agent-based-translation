from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any, Mapping, Sequence

from pipeline.eval.cometkiwi_subprocess_v1 import (
    CometKiwiSubprocessPredictorV1,
)
from pipeline.eval.workflow_runtime_factory_v1 import (
    EvaluationServerRuntimeConfigV1,
)
from pipeline.llm_backend import MappingCredentialProvider, UrllibTransportSender
from pipeline.workflow_replay.contracts_v1 import (
    canonical_json_bytes,
    canonical_sha256,
    physical_sha256,
)


SERVER_RUNTIME_SCHEMA_ID = "EvaluationServerRuntimeConfigV1"
SERVER_RUNTIME_SCHEMA_VERSION = "1.0.0"


class EvaluationServerRuntimeError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def validate_evaluation_server_runtime_config_v1(
    path: str | Path,
) -> dict[str, Any]:
    """Validate public runtime authority without reading credential bytes."""

    config_path = _regular_file(path, owner="runtime config")
    encoded = config_path.read_bytes()
    try:
        value = json.loads(encoded.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise EvaluationServerRuntimeError(
            "evaluation_runtime_config_json",
            "Evaluation runtime config is not valid UTF-8 JSON.",
        ) from exc
    if not isinstance(value, Mapping):
        raise EvaluationServerRuntimeError(
            "evaluation_runtime_config_shape",
            "Evaluation runtime config must be an object.",
        )
    if encoded != canonical_json_bytes(value) + b"\n":
        raise EvaluationServerRuntimeError(
            "evaluation_runtime_config_noncanonical",
            "Evaluation runtime config bytes must use canonical JSON plus LF.",
        )
    _exact_keys(
        value,
        {"schema_id", "schema_version", "cometkiwi", "llm", "integrity"},
        owner="runtime config",
    )
    if (
        value["schema_id"] != SERVER_RUNTIME_SCHEMA_ID
        or value["schema_version"] != SERVER_RUNTIME_SCHEMA_VERSION
    ):
        raise EvaluationServerRuntimeError(
            "evaluation_runtime_config_schema",
            "Evaluation runtime config schema is unsupported.",
        )
    integrity = _object(value["integrity"], owner="runtime config integrity")
    _exact_keys(integrity, {"config_sha256"}, owner="runtime config integrity")
    observed_sha = _sha256(
        integrity["config_sha256"], owner="runtime config integrity"
    )
    unhashed = copy.deepcopy(dict(value))
    unhashed.pop("integrity")
    if canonical_sha256(unhashed) != observed_sha:
        raise EvaluationServerRuntimeError(
            "evaluation_runtime_config_hash",
            "Evaluation runtime config hash is inconsistent.",
        )

    comet = _validate_comet_config(value["cometkiwi"])
    llm = _validate_llm_config(value["llm"])
    credential_path = _regular_file(
        llm["credential_file"], owner="Evaluation credential file"
    )
    profile = _artifact_payload(llm["profile"], owner="Evaluation profile")
    sources = [
        _artifact_payload(row, owner=f"API source {index}")
        for index, row in enumerate(llm["api_sources"])
    ]
    capabilities = [
        _artifact_payload(row, owner=f"capability evidence {index}")
        for index, row in enumerate(llm["capability_evidence"])
    ]
    source_refs = {
        row.get("credential_ref")
        for row in sources
        if row.get("credential_ref") is not None
    }
    if source_refs != {llm["credential_ref"]}:
        raise EvaluationServerRuntimeError(
            "evaluation_runtime_credential_binding",
            "API sources must exact-match the registered credential reference.",
        )
    return {
        "schema_id": SERVER_RUNTIME_SCHEMA_ID,
        "schema_version": SERVER_RUNTIME_SCHEMA_VERSION,
        "config_path": str(config_path),
        "config_sha256": observed_sha,
        "cometkiwi": comet,
        "llm": {
            **llm,
            "credential_file": str(credential_path),
            "profile_payload": profile,
            "api_source_payloads": sources,
            "capability_evidence_payloads": capabilities,
        },
    }


def build_evaluation_server_runtime_v1(
    path: str | Path,
) -> EvaluationServerRuntimeConfigV1:
    """Build concrete process-local runtime objects from sealed server config."""

    loaded = validate_evaluation_server_runtime_config_v1(path)
    comet = loaded["cometkiwi"]
    llm = loaded["llm"]
    predictor = CometKiwiSubprocessPredictorV1(
        python_executable=Path(comet["python_executable"]),
        checkpoint_path=Path(comet["checkpoint_path"]),
        timeout_seconds=comet["timeout_seconds"],
        max_rows_per_worker=comet["max_rows_per_worker"],
    )
    if predictor.checkpoint_sha256 != comet["checkpoint_sha256"]:
        raise EvaluationServerRuntimeError(
            "evaluation_runtime_checkpoint_hash",
            "COMET checkpoint bytes differ from the registered hash.",
        )
    secret = _read_single_secret(Path(llm["credential_file"]))
    return EvaluationServerRuntimeConfigV1(
        local_sf_qe_predictor=predictor,
        local_sf_qe_checkpoint_sha256=comet["checkpoint_sha256"],
        local_sf_qe_package_name=comet["package_name"],
        local_sf_qe_package_version=comet["package_version"],
        local_sf_qe_device=comet["device"],
        local_sf_qe_batch_size=comet["batch_size"],
        llm_profile=llm["profile_payload"],
        api_sources=tuple(llm["api_source_payloads"]),
        capability_evidence=tuple(llm["capability_evidence_payloads"]),
        credential_provider=MappingCredentialProvider(
            {llm["credential_ref"]: secret}
        ),
        sender=UrllibTransportSender(),
        cache_mode=llm["cache_mode"],
        cost_fact=copy.deepcopy(llm["cost_fact"]),
    )


def _validate_comet_config(value: Any) -> dict[str, Any]:
    row = _object(value, owner="COMET runtime")
    _exact_keys(
        row,
        {
            "python_executable",
            "checkpoint_path",
            "checkpoint_sha256",
            "package_name",
            "package_version",
            "device",
            "batch_size",
            "timeout_seconds",
            "max_rows_per_worker",
        },
        owner="COMET runtime",
    )
    python_path = _regular_file(
        _string(row["python_executable"], owner="COMET Python"),
        owner="COMET Python",
    )
    checkpoint_path = _regular_file(
        _string(row["checkpoint_path"], owner="COMET checkpoint"),
        owner="COMET checkpoint",
        preserve_symlink=True,
    )
    checkpoint_sha = _sha256(
        row["checkpoint_sha256"], owner="COMET checkpoint"
    )
    if physical_sha256(checkpoint_path.read_bytes()) != checkpoint_sha:
        raise EvaluationServerRuntimeError(
            "evaluation_runtime_checkpoint_hash",
            "COMET checkpoint bytes differ from the registered hash.",
        )
    batch_size = _integer(
        row["batch_size"], owner="COMET batch size", minimum=1, maximum=512
    )
    timeout = _integer(
        row["timeout_seconds"],
        owner="COMET timeout",
        minimum=1,
        maximum=7200,
    )
    max_rows = row["max_rows_per_worker"]
    if max_rows is not None:
        max_rows = _integer(
            max_rows,
            owner="COMET worker row cap",
            minimum=1,
            maximum=10_000,
        )
    return {
        "python_executable": str(python_path),
        "checkpoint_path": str(checkpoint_path),
        "checkpoint_sha256": checkpoint_sha,
        "package_name": _string(
            row["package_name"], owner="COMET package name"
        ),
        "package_version": _string(
            row["package_version"], owner="COMET package version"
        ),
        "device": _string(row["device"], owner="COMET device"),
        "batch_size": batch_size,
        "timeout_seconds": timeout,
        "max_rows_per_worker": max_rows,
    }


def _validate_llm_config(value: Any) -> dict[str, Any]:
    row = _object(value, owner="Evaluation LLM runtime")
    _exact_keys(
        row,
        {
            "profile",
            "api_sources",
            "capability_evidence",
            "credential_ref",
            "credential_file",
            "cache_mode",
            "cost_fact",
        },
        owner="Evaluation LLM runtime",
    )
    sources = _array(row["api_sources"], owner="API sources")
    capabilities = _array(
        row["capability_evidence"], owner="capability evidence"
    )
    if not sources or not capabilities:
        raise EvaluationServerRuntimeError(
            "evaluation_runtime_llm_authority",
            "Evaluation LLM runtime requires source and capability authority.",
        )
    cache_mode = _string(row["cache_mode"], owner="cache mode")
    if cache_mode not in {"disabled", "read_only", "read_write"}:
        raise EvaluationServerRuntimeError(
            "evaluation_runtime_cache_mode",
            "Evaluation cache mode is unsupported.",
        )
    cost_fact = row["cost_fact"]
    if cost_fact is not None and not isinstance(cost_fact, Mapping):
        raise EvaluationServerRuntimeError(
            "evaluation_runtime_cost_fact",
            "Evaluation cost fact must be an object or null.",
        )
    return {
        "profile": _artifact_binding(row["profile"], owner="Evaluation profile"),
        "api_sources": [
            _artifact_binding(item, owner=f"API source {index}")
            for index, item in enumerate(sources)
        ],
        "capability_evidence": [
            _artifact_binding(item, owner=f"capability evidence {index}")
            for index, item in enumerate(capabilities)
        ],
        "credential_ref": _string(
            row["credential_ref"], owner="credential reference"
        ),
        "credential_file": _string(
            row["credential_file"], owner="credential file"
        ),
        "cache_mode": cache_mode,
        "cost_fact": copy.deepcopy(cost_fact),
    }


def _artifact_binding(value: Any, *, owner: str) -> dict[str, str]:
    row = _object(value, owner=owner)
    _exact_keys(row, {"path", "sha256"}, owner=owner)
    return {
        "path": _string(row["path"], owner=f"{owner} path"),
        "sha256": _sha256(row["sha256"], owner=owner),
    }


def _artifact_payload(binding: Mapping[str, str], *, owner: str) -> dict[str, Any]:
    path = _regular_file(binding["path"], owner=owner)
    if physical_sha256(path.read_bytes()) != binding["sha256"]:
        raise EvaluationServerRuntimeError(
            "evaluation_runtime_artifact_hash",
            f"{owner} bytes differ from the registered hash.",
        )
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise EvaluationServerRuntimeError(
            "evaluation_runtime_artifact_json",
            f"{owner} is not valid UTF-8 JSON.",
        ) from exc
    if not isinstance(value, dict):
        raise EvaluationServerRuntimeError(
            "evaluation_runtime_artifact_shape",
            f"{owner} must be a JSON object.",
        )
    return value


def _read_single_secret(path: Path) -> str:
    try:
        value = path.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise EvaluationServerRuntimeError(
            "evaluation_runtime_credential_encoding",
            "Evaluation credential file must be UTF-8.",
        ) from exc
    rows = [row for row in value.splitlines() if row]
    if len(rows) != 1 or rows[0] != rows[0].strip() or any(
        character.isspace() or ord(character) < 32 for character in rows[0]
    ):
        raise EvaluationServerRuntimeError(
            "evaluation_runtime_credential_shape",
            "Evaluation credential file must contain one nonempty token.",
        )
    return rows[0]


def _regular_file(
    value: str | Path,
    *,
    owner: str,
    preserve_symlink: bool = False,
) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute() or not path.is_file():
        raise EvaluationServerRuntimeError(
            "evaluation_runtime_path",
            f"{owner} must name an existing absolute file.",
        )
    if path.is_symlink() and not preserve_symlink:
        raise EvaluationServerRuntimeError(
            "evaluation_runtime_path",
            f"{owner} must not be a symlink.",
        )
    return path.resolve()


def _object(value: Any, *, owner: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise EvaluationServerRuntimeError(
            "evaluation_runtime_shape", f"{owner} must be an object."
        )
    return value


def _array(value: Any, *, owner: str) -> Sequence[Any]:
    if not isinstance(value, list):
        raise EvaluationServerRuntimeError(
            "evaluation_runtime_shape", f"{owner} must be an array."
        )
    return value


def _exact_keys(
    value: Mapping[str, Any], expected: set[str], *, owner: str
) -> None:
    if set(value) != expected:
        raise EvaluationServerRuntimeError(
            "evaluation_runtime_fields",
            f"{owner} fields differ from the registered contract.",
        )


def _string(value: Any, *, owner: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise EvaluationServerRuntimeError(
            "evaluation_runtime_value", f"{owner} must be a nonempty string."
        )
    return value


def _sha256(value: Any, *, owner: str) -> str:
    text = _string(value, owner=owner).lower()
    if len(text) != 64 or any(character not in "0123456789abcdef" for character in text):
        raise EvaluationServerRuntimeError(
            "evaluation_runtime_sha256", f"{owner} must contain a SHA-256."
        )
    return text


def _integer(
    value: Any,
    *,
    owner: str,
    minimum: int,
    maximum: int,
) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise EvaluationServerRuntimeError(
            "evaluation_runtime_value", f"{owner} must be an integer."
        )
    if value < minimum or value > maximum:
        raise EvaluationServerRuntimeError(
            "evaluation_runtime_value",
            f"{owner} must be within {minimum}..{maximum}.",
        )
    return value


__all__ = [
    "EvaluationServerRuntimeError",
    "SERVER_RUNTIME_SCHEMA_ID",
    "SERVER_RUNTIME_SCHEMA_VERSION",
    "build_evaluation_server_runtime_v1",
    "validate_evaluation_server_runtime_config_v1",
]
