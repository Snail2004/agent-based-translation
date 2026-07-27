"""Closed mechanical bindings for the current Literary chapter loop."""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
from typing import Any, Mapping

from pipeline.literary.checkpoint import canonical_hash, file_sha256


BINDING_SCHEMA_VERSION = "literary_chapter_loop_stage_bindings_v1"
RUNTIME_BINDING_SCHEMA_VERSION = "literary_chapter_loop_runtime_bindings_v1"
STAGE_NAMES = (
    "b1_scan",
    "b1_enrich",
    "b1_local_auditor",
    "b1_registry_writer",
    "xchapter_prepare",
    "xchapter_hearing",
    "identity_apply",
    "b1_to_b2_input",
    "b2_frame_interaction",
    "b2_review_routing",
    "speaker_recovery",
    "b3_temporal",
    "b3_auditor",
    "b3_apply",
    "b0_summary",
    "checkpoint",
)
MODEL_STAGE_NAMES = frozenset(
    {
        "b1_scan",
        "b1_enrich",
        "b1_local_auditor",
        "xchapter_hearing",
        "b2_frame_interaction",
        "speaker_recovery",
        "b3_temporal",
        "b3_auditor",
        "b0_summary",
    }
)


class ChapterLoopBindingError(ValueError):
    pass


@dataclass(frozen=True)
class StageBindingV1:
    stage_name: str
    script: str | None
    command: str | None
    role: str
    api: bool
    conditional: bool
    condition: str | None
    inputs: Mapping[str, str]
    outputs: tuple[str, ...]
    report: str


@dataclass(frozen=True)
class RuntimeStageBindingV1:
    stage_name: str
    runtime_profile: Path | None
    context_profile: Path | None
    capabilities: Mapping[str, Path]
    source_id: str
    model_id: str


@dataclass(frozen=True)
class ChapterLoopRuntimeBindingsV1:
    source_path: Path
    binding_id: str
    stages: Mapping[str, RuntimeStageBindingV1]
    binding_hash: str


def _read_object(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    except (OSError, TypeError, ValueError) as exc:
        raise ChapterLoopBindingError(f"cannot load {label}: {path}") from exc
    if not isinstance(value, dict):
        raise ChapterLoopBindingError(f"{label} must be an object")
    return value


def load_stage_bindings_v1(path: Path) -> dict[str, StageBindingV1]:
    payload = _read_object(Path(path).resolve(), "stage bindings")
    if set(payload) != {"schema_version", "binding_id", "stages"}:
        raise ChapterLoopBindingError("stage binding key set is not closed")
    if payload["schema_version"] != BINDING_SCHEMA_VERSION:
        raise ChapterLoopBindingError("foreign stage binding schema")
    _required_string(payload["binding_id"], "binding_id")
    rows = payload["stages"]
    if not isinstance(rows, list):
        raise ChapterLoopBindingError("stage bindings must be a list")
    result: dict[str, StageBindingV1] = {}
    for row in rows:
        if not isinstance(row, Mapping):
            raise ChapterLoopBindingError("stage binding row must be an object")
        expected = {
            "stage_name",
            "script",
            "command",
            "role",
            "api",
            "conditional",
            "inputs",
            "outputs",
            "report",
        }
        if row.get("conditional") is True:
            expected.add("condition")
        if set(row) != expected:
            raise ChapterLoopBindingError("stage binding row key set is not closed")
        stage_name = _required_string(row["stage_name"], "stage_name")
        if stage_name in result:
            raise ChapterLoopBindingError("stage bindings repeat a stage")
        script = _optional_string(row["script"], "script")
        command = _optional_string(row["command"], "command")
        role = _required_string(row["role"], "role")
        api = _required_bool(row["api"], "api")
        conditional = _required_bool(row["conditional"], "conditional")
        if api != (stage_name in MODEL_STAGE_NAMES):
            raise ChapterLoopBindingError("stage API classification drifted")
        if (script is None) != (stage_name == "checkpoint"):
            raise ChapterLoopBindingError("only checkpoint may lack a script")
        raw_inputs = row["inputs"]
        if not isinstance(raw_inputs, Mapping) or any(
            not isinstance(key, str) or not isinstance(value, str)
            for key, value in raw_inputs.items()
        ):
            raise ChapterLoopBindingError("stage inputs must be a string map")
        raw_outputs = row["outputs"]
        if (
            not isinstance(raw_outputs, list)
            or not raw_outputs
            or any(not isinstance(value, str) or not value for value in raw_outputs)
        ):
            raise ChapterLoopBindingError("stage outputs must be a non-empty string list")
        result[stage_name] = StageBindingV1(
            stage_name=stage_name,
            script=script,
            command=command,
            role=role,
            api=api,
            conditional=conditional,
            condition=(
                _required_string(row["condition"], "condition")
                if conditional
                else None
            ),
            inputs=dict(raw_inputs),
            outputs=tuple(raw_outputs),
            report=_required_string(row["report"], "report"),
        )
    if tuple(result) != STAGE_NAMES:
        raise ChapterLoopBindingError("stage bindings do not match the sealed stage graph")
    return result


def load_runtime_bindings_v1(
    path: Path,
    *,
    expected_stages: frozenset[str] = MODEL_STAGE_NAMES,
) -> ChapterLoopRuntimeBindingsV1:
    source = Path(path).resolve()
    payload = _read_object(source, "runtime bindings")
    if set(payload) != {"schema_version", "binding_id", "stages"}:
        raise ChapterLoopBindingError("runtime binding key set is not closed")
    if payload["schema_version"] != RUNTIME_BINDING_SCHEMA_VERSION:
        raise ChapterLoopBindingError("foreign runtime binding schema")
    binding_id = _required_string(payload["binding_id"], "binding_id")
    raw_stages = payload["stages"]
    if not isinstance(raw_stages, Mapping) or set(raw_stages) != expected_stages:
        raise ChapterLoopBindingError("runtime bindings do not cover model stages exactly")
    stages: dict[str, RuntimeStageBindingV1] = {}
    for stage_name, raw in raw_stages.items():
        if not isinstance(raw, Mapping):
            raise ChapterLoopBindingError("runtime stage binding must be an object")
        if set(raw) != {
            "runtime_profile",
            "context_profile",
            "capabilities",
            "source_id",
            "model_id",
        }:
            raise ChapterLoopBindingError(
                f"runtime binding key set is not closed: {stage_name}"
            )
        capabilities = raw["capabilities"]
        if not isinstance(capabilities, Mapping) or not capabilities:
            raise ChapterLoopBindingError(
                f"runtime binding has no capability: {stage_name}"
            )
        capability_paths = {
            _required_string(name, "capability name"): _existing_path(
                value, source.parent, f"{stage_name} capability"
            )
            for name, value in capabilities.items()
        }
        source_id = _required_string(raw["source_id"], "source_id")
        model_id = _required_string(raw["model_id"], "model_id")
        runtime_profile = _optional_existing_path(
            raw["runtime_profile"], source.parent, f"{stage_name} runtime profile"
        )
        context_profile = _optional_existing_path(
            raw["context_profile"], source.parent, f"{stage_name} context profile"
        )
        _verify_runtime_source(
            stage_name=stage_name,
            runtime_profile=runtime_profile,
            capability_paths=capability_paths,
            expected_source_id=source_id,
            expected_model_id=model_id,
        )
        stages[stage_name] = RuntimeStageBindingV1(
            stage_name=stage_name,
            runtime_profile=runtime_profile,
            context_profile=context_profile,
            capabilities=capability_paths,
            source_id=source_id,
            model_id=model_id,
        )
    body = {
        "binding_id": binding_id,
        "source_sha256": file_sha256(source),
        "stage_sources": {
            stage_name: {
                "source_id": row.source_id,
                "model_id": row.model_id,
                "runtime_profile_sha256": (
                    file_sha256(row.runtime_profile) if row.runtime_profile else None
                ),
                "context_profile_sha256": (
                    file_sha256(row.context_profile) if row.context_profile else None
                ),
                "capabilities": {
                    name: file_sha256(_capability_evidence_path(path))
                    for name, path in sorted(row.capabilities.items())
                },
            }
            for stage_name, row in sorted(stages.items())
        },
    }
    return ChapterLoopRuntimeBindingsV1(
        source_path=source,
        binding_id=binding_id,
        stages=stages,
        binding_hash=canonical_hash(body),
    )


def _verify_runtime_source(
    *,
    stage_name: str,
    runtime_profile: Path | None,
    capability_paths: Mapping[str, Path],
    expected_source_id: str,
    expected_model_id: str,
) -> None:
    if runtime_profile is not None:
        runtime = _read_object(runtime_profile, f"{stage_name} runtime profile")
        source_ids = {
            row.get("source_id")
            for row in runtime.get("sources") or []
            if isinstance(row, Mapping)
        }
        model_ids = {
            row.get("requested_model_id")
            for row in runtime.get("roles") or []
            if isinstance(row, Mapping)
        }
        if source_ids != {expected_source_id}:
            raise ChapterLoopBindingError(
                f"runtime profile source_id differs for {stage_name}"
            )
        if model_ids != {expected_model_id}:
            raise ChapterLoopBindingError(
                f"runtime profile model differs for {stage_name}"
            )
    for capability_name, capability_root in capability_paths.items():
        evidence_path = _capability_evidence_path(capability_root)
        evidence = _read_object(
            evidence_path,
            f"{stage_name}.{capability_name} capability evidence",
        )
        if evidence.get("verdict") != "qualified":
            raise ChapterLoopBindingError(
                f"capability is not qualified: {stage_name}.{capability_name}"
            )
        if evidence.get("source_id") != expected_source_id:
            raise ChapterLoopBindingError(
                f"capability source_id differs for {stage_name}.{capability_name}"
            )
        if (
            evidence.get("requested_model_id") != expected_model_id
            or evidence.get("observed_model_id") != expected_model_id
        ):
            raise ChapterLoopBindingError(
                f"capability model differs for {stage_name}.{capability_name}"
            )


def _capability_evidence_path(path: Path) -> Path:
    return path / "capability_evidence.json" if path.is_dir() else path


def _required_string(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise ChapterLoopBindingError(f"{label} must be a non-empty string")
    return value


def _optional_string(value: Any, label: str) -> str | None:
    if value is None:
        return None
    return _required_string(value, label)


def _required_bool(value: Any, label: str) -> bool:
    if not isinstance(value, bool):
        raise ChapterLoopBindingError(f"{label} must be bool")
    return value


def _existing_path(value: Any, base: Path, label: str) -> Path:
    raw = _required_string(value, label)
    path = Path(raw)
    target = (base / path).resolve() if not path.is_absolute() else path.resolve()
    if not target.exists():
        raise ChapterLoopBindingError(f"{label} is absent: {target}")
    return target


def _optional_existing_path(value: Any, base: Path, label: str) -> Path | None:
    if value is None:
        return None
    return _existing_path(value, base, label)


__all__ = [
    "BINDING_SCHEMA_VERSION",
    "ChapterLoopBindingError",
    "ChapterLoopRuntimeBindingsV1",
    "MODEL_STAGE_NAMES",
    "RUNTIME_BINDING_SCHEMA_VERSION",
    "STAGE_NAMES",
    "StageBindingV1",
    "RuntimeStageBindingV1",
    "load_runtime_bindings_v1",
    "load_stage_bindings_v1",
]
