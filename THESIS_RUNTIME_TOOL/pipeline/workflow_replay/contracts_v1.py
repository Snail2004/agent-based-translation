from __future__ import annotations

import copy
import hashlib
import json
import math
import re
import unicodedata
from datetime import datetime
from pathlib import PurePosixPath
from typing import Any, Iterable, Mapping, Sequence


FLOW_KIND_V1 = "translation_evaluation_publication"
COMPONENT_IDS_V1 = ("translation", "evaluation", "publication")
ARM_IDS_V1 = ("s0", "s1", "community", "google_nmt", "llm_lc")
SOURCE_BINDING_ROLES_V1 = (
    "document",
    "structure_manifest",
    "asset_manifest",
    "admitted_projection",
    "normalization_receipt",
    "package_seal",
)
SCHEMA_VERSION_V1 = "1.0.0"

_SHA256_RE = re.compile(r"^[0-9a-fA-F]{64}$")
_CANONICAL_HASH_KIND_RE = re.compile(
    r"^canonical:[A-Za-z][A-Za-z0-9_.-]*@[0-9]+\.[0-9]+\.[0-9]+$"
)
_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,191}$")
_FORBIDDEN_PAYLOAD_KEYS = frozenset(
    {
        "api_key",
        "authorization",
        "bearer",
        "bearer_token",
        "credential",
        "credentials",
        "gold",
        "gold_translation",
        "human_reference",
        "human_translation",
        "prompt",
        "raw_prompt",
        "raw_request",
        "raw_response",
        "reference_translation",
        "request_body",
        "response",
        "response_body",
        "secret",
    }
)


class WorkflowReplayContractError(ValueError):
    """Raised when neutral workflow evidence is not mechanically valid."""

    def __init__(self, code: str, path: str, message: str) -> None:
        self.code = code
        self.path = path
        super().__init__(f"{code} at {path}: {message}")


def canonical_json_bytes(value: Any) -> bytes:
    normalized = _normalize_json(value, path="$")
    return json.dumps(
        normalized,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def canonical_sha256(value: Any) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def physical_sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def seal_payload(value: Mapping[str, Any], *, hash_key: str) -> dict[str, Any]:
    payload = copy.deepcopy(dict(value))
    payload.pop(hash_key, None)
    payload[hash_key] = canonical_sha256(payload)
    return payload


def verify_sealed_payload(value: Mapping[str, Any], *, hash_key: str) -> bool:
    digest = value.get(hash_key)
    if not isinstance(digest, str) or _SHA256_RE.fullmatch(digest) is None:
        return False
    payload = copy.deepcopy(dict(value))
    payload.pop(hash_key, None)
    return canonical_sha256(payload) == digest.lower()


def validate_typed_artifact_binding_v1(value: Any, *, path: str) -> dict[str, str]:
    row = _mapping(value, path=path)
    _exact_keys(
        row,
        required={
            "artifact_ref",
            "artifact_kind",
            "schema_version",
            "sha256",
            "sha256_kind",
        },
        path=path,
    )
    sha_kind = _string(row["sha256_kind"], path=f"{path}.sha256_kind")
    if sha_kind != "physical" and _CANONICAL_HASH_KIND_RE.fullmatch(sha_kind) is None:
        raise WorkflowReplayContractError(
            "sha256_kind",
            f"{path}.sha256_kind",
            "expected physical or canonical:<contract>@<semver>",
        )
    return {
        "artifact_ref": _relative_ref(row["artifact_ref"], path=f"{path}.artifact_ref"),
        "artifact_kind": _string(row["artifact_kind"], path=f"{path}.artifact_kind"),
        "schema_version": _string(row["schema_version"], path=f"{path}.schema_version"),
        "sha256": _sha256(row["sha256"], path=f"{path}.sha256"),
        "sha256_kind": sha_kind,
    }


def validate_source_package_bindings_v1(value: Any) -> list[dict[str, Any]]:
    rows = _list(value, path="$.source_package_bindings")
    if len(rows) != len(SOURCE_BINDING_ROLES_V1):
        raise WorkflowReplayContractError(
            "source_binding_exact_cover",
            "$.source_package_bindings",
            "expected exactly six canonical source-package bindings",
        )
    normalized: list[dict[str, Any]] = []
    for index, expected_role in enumerate(SOURCE_BINDING_ROLES_V1):
        path = f"$.source_package_bindings[{index}]"
        row = _mapping(rows[index], path=path)
        _exact_keys(row, required={"role", "binding"}, path=path)
        role = _string(row["role"], path=f"{path}.role")
        if role != expected_role:
            raise WorkflowReplayContractError(
                "source_binding_order",
                f"{path}.role",
                f"expected {expected_role}",
            )
        normalized.append(
            {
                "role": role,
                "binding": validate_typed_artifact_binding_v1(
                    row["binding"], path=f"{path}.binding"
                ),
            }
        )
    return normalized


def validate_workflow_event_v1(value: Any) -> dict[str, Any]:
    row = _mapping(value, path="$event")
    _exact_keys(
        row,
        required={
            "schema_id",
            "schema_version",
            "event_id",
            "workflow_run_id",
            "flow_kind",
            "seq",
            "accepted_at",
            "component",
            "stage_id",
            "agent",
            "event",
            "severity",
            "payload",
            "integrity",
        },
        path="$event",
    )
    seq = _integer(row["seq"], path="$event.seq", minimum=1)
    if row["event_id"] != f"workflow_event_{seq:08d}":
        raise WorkflowReplayContractError(
            "event_identity", "$event.event_id", "event ID must be derived from parent seq"
        )
    component = _mapping(row["component"], path="$event.component")
    _exact_keys(
        component,
        required={
            "component_id",
            "component_run_id",
            "component_attempt_id",
            "component_attempt_index",
            "component_seq",
            "source_event_id",
            "source_event_sha256",
            "source_event_sha256_kind",
            "validator_id",
            "validator_revision",
        },
        path="$event.component",
    )
    attempt_id, attempt_index = _validate_attempt_pair(
        component["component_attempt_id"],
        component["component_attempt_index"],
        path="$event.component",
    )
    integrity = _mapping(row["integrity"], path="$event.integrity")
    _exact_keys(
        integrity,
        required={"previous_event_sha256", "event_sha256"},
        path="$event.integrity",
    )
    normalized = {
        "schema_id": _enum(row["schema_id"], {"WorkflowEventV1"}, path="$event.schema_id"),
        "schema_version": _enum(
            row["schema_version"], {SCHEMA_VERSION_V1}, path="$event.schema_version"
        ),
        "event_id": _identifier(row["event_id"], path="$event.event_id"),
        "workflow_run_id": _identifier(
            row["workflow_run_id"], path="$event.workflow_run_id"
        ),
        "flow_kind": _enum(row["flow_kind"], {FLOW_KIND_V1}, path="$event.flow_kind"),
        "seq": seq,
        "accepted_at": _rfc3339(row["accepted_at"], path="$event.accepted_at"),
        "component": {
            "component_id": _enum(
                component["component_id"], set(COMPONENT_IDS_V1), path="$event.component.component_id"
            ),
            "component_run_id": _identifier(
                component["component_run_id"], path="$event.component.component_run_id"
            ),
            "component_attempt_id": attempt_id,
            "component_attempt_index": attempt_index,
            "component_seq": _integer(
                component["component_seq"], path="$event.component.component_seq", minimum=1
            ),
            "source_event_id": _identifier(
                component["source_event_id"], path="$event.component.source_event_id"
            ),
            "source_event_sha256": _sha256(
                component["source_event_sha256"], path="$event.component.source_event_sha256"
            ),
            "source_event_sha256_kind": _enum(
                component["source_event_sha256_kind"],
                {"physical"},
                path="$event.component.source_event_sha256_kind",
            ),
            "validator_id": _identifier(
                component["validator_id"], path="$event.component.validator_id"
            ),
            "validator_revision": _identifier(
                component["validator_revision"], path="$event.component.validator_revision"
            ),
        },
        "stage_id": (
            None if row["stage_id"] is None else _identifier(row["stage_id"], path="$event.stage_id")
        ),
        "agent": _identifier(row["agent"], path="$event.agent"),
        "event": _identifier(row["event"], path="$event.event"),
        "severity": _enum(
            row["severity"], {"info", "warning", "error"}, path="$event.severity"
        ),
        "payload": validate_parent_event_public_payload(row["payload"]),
        "integrity": {
            "previous_event_sha256": (
                None
                if integrity["previous_event_sha256"] is None
                else _sha256(
                    integrity["previous_event_sha256"],
                    path="$event.integrity.previous_event_sha256",
                )
            ),
            "event_sha256": _sha256(
                integrity["event_sha256"], path="$event.integrity.event_sha256"
            ),
        },
    }
    if _payload_hash_without_nested(
        normalized, ("integrity", "event_sha256")
    ) != normalized["integrity"]["event_sha256"]:
        raise WorkflowReplayContractError(
            "event_hash", "$event.integrity.event_sha256", "parent event hash drift"
        )
    return normalized


def validate_workflow_artifact_index_v1(value: Any) -> dict[str, Any]:
    row = _mapping(value, path="$artifact_index")
    _exact_keys(
        row,
        required={
            "schema_id",
            "schema_version",
            "workflow_run_id",
            "flow_kind",
            "artifacts",
            "integrity",
        },
        path="$artifact_index",
    )
    artifacts = [
        _validate_parent_artifact(item, path=f"$artifact_index.artifacts[{index}]")
        for index, item in enumerate(_list(row["artifacts"], path="$artifact_index.artifacts"))
    ]
    refs = [item["binding"]["artifact_ref"] for item in artifacts]
    if len(refs) != len(set(refs)):
        raise WorkflowReplayContractError(
            "duplicate_artifact_ref", "$artifact_index.artifacts", "artifact refs repeat"
        )
    ref_set = set(refs)
    for index, item in enumerate(artifacts):
        unknown = sorted(set(item["parent_artifact_refs"]) - ref_set)
        if unknown:
            raise WorkflowReplayContractError(
                "artifact_parent",
                f"$artifact_index.artifacts[{index}].parent_artifact_refs",
                "unknown parents: " + ", ".join(unknown),
            )
    _reject_artifact_parent_cycles(
        {
            item["binding"]["artifact_ref"]: item["parent_artifact_refs"]
            for item in artifacts
        },
        path="$artifact_index.artifacts",
    )
    integrity = _mapping(row["integrity"], path="$artifact_index.integrity")
    _exact_keys(
        integrity, required={"artifact_index_sha256"}, path="$artifact_index.integrity"
    )
    normalized = {
        "schema_id": _enum(
            row["schema_id"], {"WorkflowArtifactIndexV1"}, path="$artifact_index.schema_id"
        ),
        "schema_version": _enum(
            row["schema_version"], {SCHEMA_VERSION_V1}, path="$artifact_index.schema_version"
        ),
        "workflow_run_id": _identifier(
            row["workflow_run_id"], path="$artifact_index.workflow_run_id"
        ),
        "flow_kind": _enum(
            row["flow_kind"], {FLOW_KIND_V1}, path="$artifact_index.flow_kind"
        ),
        "artifacts": artifacts,
        "integrity": {
            "artifact_index_sha256": _sha256(
                integrity["artifact_index_sha256"],
                path="$artifact_index.integrity.artifact_index_sha256",
            )
        },
    }
    if _payload_hash_without_nested(
        normalized, ("integrity", "artifact_index_sha256")
    ) != normalized["integrity"]["artifact_index_sha256"]:
        raise WorkflowReplayContractError(
            "artifact_index_hash",
            "$artifact_index.integrity.artifact_index_sha256",
            "artifact index hash drift",
        )
    return normalized


def validate_workflow_manifest_v1(value: Any) -> dict[str, Any]:
    row = _mapping(value, path="$manifest")
    _exact_keys(
        row,
        required={
            "schema_id",
            "schema_version",
            "workflow_run_id",
            "flow_kind",
            "job_id",
            "source_package_bindings",
            "status",
            "started_at",
            "updated_at",
            "active_stage_id",
            "components",
            "stages",
            "resume",
            "reconstructed",
            "timing_authority",
            "latest_event_seq",
            "artifact_index_sha256",
            "integrity",
        },
        path="$manifest",
    )
    components = [
        _validate_parent_component(item, path=f"$manifest.components[{index}]")
        for index, item in enumerate(_list(row["components"], path="$manifest.components"))
    ]
    expected_component_order = [
        component_id
        for component_id in COMPONENT_IDS_V1
        if component_id in {item["component_id"] for item in components}
    ]
    if [item["component_id"] for item in components] != expected_component_order:
        raise WorkflowReplayContractError(
            "component_order", "$manifest.components", "components are not in canonical order"
        )
    stages = [
        _validate_parent_stage(item, path=f"$manifest.stages[{index}]", order=index + 1)
        for index, item in enumerate(_list(row["stages"], path="$manifest.stages"))
    ]
    stage_ids = [item["stage_id"] for item in stages]
    if len(stage_ids) != len(set(stage_ids)):
        raise WorkflowReplayContractError("duplicate_stage", "$manifest.stages", "stage IDs repeat")
    resume = _mapping(row["resume"], path="$manifest.resume")
    _exact_keys(resume, required={"available", "component_id"}, path="$manifest.resume")
    available = _exact_bool(resume["available"], path="$manifest.resume.available")
    resume_component = (
        None
        if resume["component_id"] is None
        else _enum(
            resume["component_id"], set(COMPONENT_IDS_V1), path="$manifest.resume.component_id"
        )
    )
    if available != (resume_component is not None):
        raise WorkflowReplayContractError(
            "resume_state", "$manifest.resume", "available and component ID disagree"
        )
    reconstructed = _exact_bool(row["reconstructed"], path="$manifest.reconstructed")
    timing = _enum(
        row["timing_authority"],
        {"recorded", "logical_order_only"},
        path="$manifest.timing_authority",
    )
    if reconstructed != (timing == "logical_order_only"):
        raise WorkflowReplayContractError(
            "timing_authority",
            "$manifest.timing_authority",
            "reconstruction must use logical-order-only timing",
        )
    integrity = _mapping(row["integrity"], path="$manifest.integrity")
    _exact_keys(integrity, required={"manifest_sha256"}, path="$manifest.integrity")
    normalized = {
        "schema_id": _enum(
            row["schema_id"], {"WorkflowManifestV1"}, path="$manifest.schema_id"
        ),
        "schema_version": _enum(
            row["schema_version"], {SCHEMA_VERSION_V1}, path="$manifest.schema_version"
        ),
        "workflow_run_id": _identifier(
            row["workflow_run_id"], path="$manifest.workflow_run_id"
        ),
        "flow_kind": _enum(row["flow_kind"], {FLOW_KIND_V1}, path="$manifest.flow_kind"),
        "job_id": _identifier(row["job_id"], path="$manifest.job_id"),
        "source_package_bindings": validate_source_package_bindings_v1(
            row["source_package_bindings"]
        ),
        "status": _enum(
            row["status"], {"pending", "running", "paused", "failed", "succeeded"}, path="$manifest.status"
        ),
        "started_at": _rfc3339(row["started_at"], path="$manifest.started_at"),
        "updated_at": _rfc3339(row["updated_at"], path="$manifest.updated_at"),
        "active_stage_id": (
            None
            if row["active_stage_id"] is None
            else _identifier(row["active_stage_id"], path="$manifest.active_stage_id")
        ),
        "components": components,
        "stages": stages,
        "resume": {"available": available, "component_id": resume_component},
        "reconstructed": reconstructed,
        "timing_authority": timing,
        "latest_event_seq": _integer(
            row["latest_event_seq"], path="$manifest.latest_event_seq", minimum=0
        ),
        "artifact_index_sha256": _sha256(
            row["artifact_index_sha256"], path="$manifest.artifact_index_sha256"
        ),
        "integrity": {
            "manifest_sha256": _sha256(
                integrity["manifest_sha256"], path="$manifest.integrity.manifest_sha256"
            )
        },
    }
    if normalized["active_stage_id"] is not None and normalized["active_stage_id"] not in stage_ids:
        raise WorkflowReplayContractError(
            "active_stage", "$manifest.active_stage_id", "active stage is undeclared"
        )
    if _payload_hash_without_nested(
        normalized, ("integrity", "manifest_sha256")
    ) != normalized["integrity"]["manifest_sha256"]:
        raise WorkflowReplayContractError(
            "manifest_hash", "$manifest.integrity.manifest_sha256", "manifest hash drift"
        )
    return normalized


def scoring_input_set_sha256_v1(translation_inputs: Sequence[Mapping[str, Any]]) -> str:
    normalized = [
        _validate_translation_input(row, path=f"$.translation_inputs[{index}]")
        for index, row in enumerate(translation_inputs)
    ]
    return canonical_sha256({"translation_inputs": normalized})


def build_scoring_handoff_v1(
    *,
    workflow_run_id: str,
    handoff_id: str,
    created_at: str,
    producer_code_commit: str,
    source_package_bindings: Sequence[Mapping[str, Any]],
    optional_bindings: Mapping[str, Mapping[str, Any] | None],
    translation_inputs: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    payload = {
        "schema_id": "ScoringHandoffV1",
        "schema_version": SCHEMA_VERSION_V1,
        "workflow_run_id": workflow_run_id,
        "flow_kind": FLOW_KIND_V1,
        "handoff_id": handoff_id,
        "created_at": created_at,
        "producer": {
            "workstream": "coordination",
            "component": "neutral_workflow_relay_v1",
            "component_version": SCHEMA_VERSION_V1,
            "code_commit": producer_code_commit,
        },
        "source_package_bindings": list(source_package_bindings),
        "optional_bindings": dict(optional_bindings),
        "translation_inputs": list(translation_inputs),
        "input_set_sha256": scoring_input_set_sha256_v1(translation_inputs),
        "integrity": {"handoff_sha256": "0" * 64},
    }
    normalized = _validate_scoring_handoff_shape(payload, check_hash=False)
    normalized["integrity"]["handoff_sha256"] = _payload_hash_without_nested(
        normalized, ("integrity", "handoff_sha256")
    )
    return validate_scoring_handoff_v1(normalized)


def normalize_d2l_scoring_fragment_v1(
    value: Mapping[str, Any],
    *,
    artifact_ref_map: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Project an already D2L-validated S0/S1 fragment into relay input rows."""

    row = _mapping(value, path="$d2l_fragment")
    required = {
        "schema",
        "fragment_sha256",
        "artifact_ref",
        "workflow_run_id",
        "flow_kind",
        "component_id",
        "translation_component_run_id",
        "translation_component_attempt_id",
        "reserved_evaluation_component_run_id",
        "source_binding",
        "source_binding_sha256",
        "translation_inputs",
        "glossary_binding",
        "context_memory_binding",
        "admitted_projection_binding",
        "selected_chapter_ids",
        "admitted_universe",
        "producer_lineage",
        "status",
        "created_at",
    }
    _exact_keys(row, required=required, path="$d2l_fragment")
    if _enum(row["schema"], {"scoring_handoff_fragment_v1"}, path="$d2l_fragment.schema") != "scoring_handoff_fragment_v1":
        raise AssertionError("unreachable")
    declared_hash = _sha256(row["fragment_sha256"], path="$d2l_fragment.fragment_sha256")
    unhashed = copy.deepcopy(dict(row))
    unhashed.pop("fragment_sha256")
    if canonical_sha256(unhashed) != declared_hash:
        raise WorkflowReplayContractError(
            "fragment_hash", "$d2l_fragment.fragment_sha256", "D2L fragment hash drift"
        )
    if row["component_id"] != "translation" or row["status"] != "translation_component_ready":
        raise WorkflowReplayContractError(
            "fragment_authority", "$d2l_fragment", "expected a ready D2L translation fragment"
        )
    source = _mapping(row["source_binding"], path="$d2l_fragment.source_binding")
    _exact_keys(source, required={"schema", *SOURCE_BINDING_ROLES_V1}, path="$d2l_fragment.source_binding")
    _enum(source["schema"], {"canonical_source_binding_v1"}, path="$d2l_fragment.source_binding.schema")
    source_bindings = [
        {
            "role": role,
            "binding": validate_typed_artifact_binding_v1(
                source[role], path=f"$d2l_fragment.source_binding.{role}"
            ),
        }
        for role in SOURCE_BINDING_ROLES_V1
    ]
    admitted = source_bindings[SOURCE_BINDING_ROLES_V1.index("admitted_projection")]["binding"]
    declared_admitted = validate_typed_artifact_binding_v1(
        row["admitted_projection_binding"],
        path="$d2l_fragment.admitted_projection_binding",
    )
    if admitted != declared_admitted:
        raise WorkflowReplayContractError(
            "source_binding_drift",
            "$d2l_fragment.admitted_projection_binding",
            "admitted projection binding differs from source package",
        )
    raw_inputs = _list(row["translation_inputs"], path="$d2l_fragment.translation_inputs")
    if [item.get("arm_id") if isinstance(item, Mapping) else None for item in raw_inputs] != ["s0", "s1"]:
        raise WorkflowReplayContractError(
            "fragment_authority",
            "$d2l_fragment.translation_inputs",
            "D2L may claim exactly ordered S0 and S1 only",
        )
    run_id = _identifier(
        row["translation_component_run_id"],
        path="$d2l_fragment.translation_component_run_id",
    )
    ref_map = dict(artifact_ref_map or {})
    normalized_inputs: list[dict[str, Any]] = []
    for index, raw in enumerate(raw_inputs):
        path = f"$d2l_fragment.translation_inputs[{index}]"
        item = _mapping(raw, path=path)
        coverage = _mapping(item.get("coverage"), path=f"{path}.coverage")
        expected = _integer(
            coverage.get("admitted_block_count"),
            path=f"{path}.coverage.admitted_block_count",
            minimum=1,
        )
        translated = _integer(
            coverage.get("translated_block_count"),
            path=f"{path}.coverage.translated_block_count",
            minimum=0,
        )
        preserved = _integer(
            coverage.get("preserved_block_count"),
            path=f"{path}.coverage.preserved_block_count",
            minimum=0,
        )
        missing = _integer(
            coverage.get("missing_block_count"),
            path=f"{path}.coverage.missing_block_count",
            minimum=0,
        )
        failed = _integer(
            coverage.get("failed_block_count"),
            path=f"{path}.coverage.failed_block_count",
            minimum=0,
        )
        if translated + preserved + missing + failed != expected:
            raise WorkflowReplayContractError(
                "coverage_accounting", f"{path}.coverage", "D2L coverage does not exact-cover"
            )
        artifact = validate_typed_artifact_binding_v1(
            item.get("artifact"), path=f"{path}.artifact"
        )
        if ref_map and artifact["artifact_ref"] not in ref_map:
            raise WorkflowReplayContractError(
                "artifact_ref_map",
                f"{path}.artifact.artifact_ref",
                "D2L artifact is absent from the relay import map",
            )
        if artifact["artifact_ref"] in ref_map:
            artifact["artifact_ref"] = _relative_ref(
                ref_map[artifact["artifact_ref"]], path=f"{path}.artifact_ref_map"
            )
        normalized_inputs.append(
            {
                "arm_id": item["arm_id"],
                "translation_artifact": artifact,
                "producer": {
                    "component_id": "translation",
                    "component_run_id": run_id,
                },
                "coverage": {
                    "expected_block_count": expected,
                    "block_universe_sha256": _sha256(
                        coverage.get("ordered_block_ids_sha256"),
                        path=f"{path}.coverage.ordered_block_ids_sha256",
                    ),
                    "translated_block_count": translated,
                    "preserved_block_count": preserved,
                    "excluded_block_count": 0,
                    "review_held_block_count": 0,
                    "missing_block_count": missing,
                    "failed_block_count": failed,
                },
                "source_binding": admitted,
            }
        )
    optional = {
        "glossary": _map_optional_d2l_binding(
            row["glossary_binding"],
            path="$d2l_fragment.glossary_binding",
            ref_map=ref_map,
        ),
        "context": _map_optional_d2l_binding(
            row["context_memory_binding"],
            path="$d2l_fragment.context_memory_binding",
            ref_map=ref_map,
        ),
        "projection": None,
    }
    return {
        "workflow_run_id": _identifier(
            row["workflow_run_id"], path="$d2l_fragment.workflow_run_id"
        ),
        "source_package_bindings": source_bindings,
        "optional_bindings": optional,
        "translation_inputs": normalized_inputs,
    }


def _map_optional_d2l_binding(
    value: Any,
    *,
    path: str,
    ref_map: Mapping[str, str],
) -> dict[str, str] | None:
    if value is None:
        return None
    binding = validate_typed_artifact_binding_v1(value, path=path)
    if ref_map and binding["artifact_ref"] not in ref_map:
        raise WorkflowReplayContractError(
            "artifact_ref_map",
            f"{path}.artifact_ref",
            "D2L optional artifact is absent from the relay import map",
        )
    if binding["artifact_ref"] in ref_map:
        binding["artifact_ref"] = _relative_ref(
            ref_map[binding["artifact_ref"]], path=f"{path}.artifact_ref_map"
        )
    return binding


def validate_scoring_handoff_v1(value: Any) -> dict[str, Any]:
    return _validate_scoring_handoff_shape(value, check_hash=True)


def validate_scoring_receipt_v1(
    value: Any,
    *,
    handoff: Mapping[str, Any],
) -> dict[str, Any]:
    accepted_handoff = validate_scoring_handoff_v1(handoff)
    row = _mapping(value, path="$receipt")
    _exact_keys(
        row,
        required={
            "schema_id",
            "schema_version",
            "workflow_run_id",
            "flow_kind",
            "evaluation_component_run_id",
            "evaluation_component_attempt_id",
            "scoring_handoff",
            "accepted_translation_inputs",
            "accepted_input_set_sha256",
            "accepted_at",
            "status",
            "rejection_code",
            "producer",
            "integrity",
        },
        path="$receipt",
    )
    handoff_binding = validate_typed_artifact_binding_v1(
        row["scoring_handoff"], path="$receipt.scoring_handoff"
    )
    expected_handoff_binding = {
        "artifact_ref": "handoffs/scoring_handoff.json",
        "artifact_kind": "scoring_handoff_v1",
        "schema_version": SCHEMA_VERSION_V1,
        "sha256": accepted_handoff["integrity"]["handoff_sha256"],
        "sha256_kind": "canonical:ScoringHandoffV1@1.0.0",
    }
    if handoff_binding != expected_handoff_binding:
        raise WorkflowReplayContractError(
            "handoff_binding",
            "$receipt.scoring_handoff",
            "receipt must bind the exact parent scoring handoff",
        )
    raw_inputs = _list(
        row["accepted_translation_inputs"], path="$receipt.accepted_translation_inputs"
    )
    inputs = [
        _validate_translation_input(item, path=f"$receipt.accepted_translation_inputs[{i}]")
        for i, item in enumerate(raw_inputs)
    ]
    if inputs != accepted_handoff["translation_inputs"]:
        raise WorkflowReplayContractError(
            "handoff_echo",
            "$receipt.accepted_translation_inputs",
            "receipt must echo all accepted handoff rows exactly",
        )
    input_hash = _sha256(
        row["accepted_input_set_sha256"], path="$receipt.accepted_input_set_sha256"
    )
    if input_hash != accepted_handoff["input_set_sha256"]:
        raise WorkflowReplayContractError(
            "handoff_echo",
            "$receipt.accepted_input_set_sha256",
            "receipt must echo the exact handoff input-set hash",
        )
    integrity = _mapping(row["integrity"], path="$receipt.integrity")
    _exact_keys(integrity, required={"receipt_sha256"}, path="$receipt.integrity")
    normalized = {
        "schema_id": _enum(
            row["schema_id"], {"ScoringReceiptV1"}, path="$receipt.schema_id"
        ),
        "schema_version": _enum(
            row["schema_version"], {SCHEMA_VERSION_V1}, path="$receipt.schema_version"
        ),
        "workflow_run_id": _string(
            row["workflow_run_id"], path="$receipt.workflow_run_id"
        ),
        "flow_kind": _enum(
            row["flow_kind"], {FLOW_KIND_V1}, path="$receipt.flow_kind"
        ),
        "evaluation_component_run_id": _identifier(
            row["evaluation_component_run_id"], path="$receipt.evaluation_component_run_id"
        ),
        "evaluation_component_attempt_id": _identifier(
            row["evaluation_component_attempt_id"],
            path="$receipt.evaluation_component_attempt_id",
        ),
        "scoring_handoff": handoff_binding,
        "accepted_translation_inputs": inputs,
        "accepted_input_set_sha256": input_hash,
        "accepted_at": _rfc3339(row["accepted_at"], path="$receipt.accepted_at"),
        "status": _enum(
            row["status"], {"accepted", "rejected"}, path="$receipt.status"
        ),
        "rejection_code": (
            None
            if row["rejection_code"] is None
            else _identifier(row["rejection_code"], path="$receipt.rejection_code")
        ),
        "producer": _validate_evaluation_producer(row["producer"]),
        "integrity": {
            "receipt_sha256": _sha256(
                integrity["receipt_sha256"], path="$receipt.integrity.receipt_sha256"
            )
        },
    }
    if normalized["workflow_run_id"] != accepted_handoff["workflow_run_id"]:
        raise WorkflowReplayContractError(
            "workflow_identity",
            "$receipt.workflow_run_id",
            "receipt belongs to another workflow",
        )
    if (normalized["status"] == "accepted") != (normalized["rejection_code"] is None):
        raise WorkflowReplayContractError(
            "receipt_decision",
            "$receipt.rejection_code",
            "accepted requires null; rejected requires a reason code",
        )
    if _payload_hash_without_nested(
        normalized, ("integrity", "receipt_sha256")
    ) != normalized["integrity"]["receipt_sha256"]:
        raise WorkflowReplayContractError(
            "receipt_hash", "$receipt.integrity.receipt_sha256", "receipt hash drift"
        )
    return normalized


def validate_parent_event_public_payload(value: Any, *, maximum_bytes: int = 65536) -> Any:
    _assert_no_private_payload(value, path="$.payload")
    _assert_truthful_unknown_cost(value, path="$.payload")
    normalized = _normalize_json(value, path="$.payload")
    if len(canonical_json_bytes(normalized)) > maximum_bytes:
        raise WorkflowReplayContractError(
            "payload_too_large", "$.payload", f"payload exceeds {maximum_bytes} bytes"
        )
    return normalized


def _validate_scoring_handoff_shape(value: Any, *, check_hash: bool) -> dict[str, Any]:
    row = _mapping(value, path="$handoff")
    _exact_keys(
        row,
        required={
            "schema_id",
            "schema_version",
            "workflow_run_id",
            "flow_kind",
            "handoff_id",
            "created_at",
            "producer",
            "source_package_bindings",
            "optional_bindings",
            "translation_inputs",
            "input_set_sha256",
            "integrity",
        },
        path="$handoff",
    )
    source_bindings = validate_source_package_bindings_v1(row["source_package_bindings"])
    optional = _mapping(row["optional_bindings"], path="$handoff.optional_bindings")
    _exact_keys(
        optional, required={"glossary", "context", "projection"}, path="$handoff.optional_bindings"
    )
    normalized_optional = {
        name: (
            None
            if optional[name] is None
            else validate_typed_artifact_binding_v1(
                optional[name], path=f"$handoff.optional_bindings.{name}"
            )
        )
        for name in ("glossary", "context", "projection")
    }
    raw_inputs = _list(row["translation_inputs"], path="$handoff.translation_inputs")
    inputs = [
        _validate_translation_input(item, path=f"$handoff.translation_inputs[{index}]")
        for index, item in enumerate(raw_inputs)
    ]
    if tuple(item["arm_id"] for item in inputs) != ARM_IDS_V1:
        raise WorkflowReplayContractError(
            "translation_input_order",
            "$handoff.translation_inputs",
            "expected exact ordered arms s0, s1, community, google_nmt, llm_lc",
        )
    for index, item in enumerate(inputs):
        producer_component = item["producer"]["component_id"]
        if item["arm_id"] in {"s0", "s1"} and producer_component != "translation":
            raise WorkflowReplayContractError(
                "producer_authority",
                f"$handoff.translation_inputs[{index}].producer.component_id",
                "S0 and S1 must be authored by the translation component",
            )
        if item["arm_id"] not in {"s0", "s1"} and producer_component == "translation":
            raise WorkflowReplayContractError(
                "producer_authority",
                f"$handoff.translation_inputs[{index}].producer.component_id",
                "translation component cannot claim foreign benchmark arms",
            )
    refs = [item["translation_artifact"]["artifact_ref"] for item in inputs]
    if len(refs) != len(set(refs)):
        raise WorkflowReplayContractError(
            "duplicate_artifact_ref",
            "$handoff.translation_inputs",
            "translation artifact refs must be unique",
        )
    admitted = source_bindings[SOURCE_BINDING_ROLES_V1.index("admitted_projection")][
        "binding"
    ]
    universe = (
        inputs[0]["coverage"]["block_universe_sha256"],
        inputs[0]["coverage"]["expected_block_count"],
    )
    for index, item in enumerate(inputs):
        if item["source_binding"] != admitted:
            raise WorkflowReplayContractError(
                "source_binding_drift",
                f"$handoff.translation_inputs[{index}].source_binding",
                "arm source binding must equal admitted projection",
            )
        if (
            item["coverage"]["block_universe_sha256"],
            item["coverage"]["expected_block_count"],
        ) != universe:
            raise WorkflowReplayContractError(
                "coverage_universe_drift",
                f"$handoff.translation_inputs[{index}].coverage",
                "all arms must cover the same admitted block universe",
            )
    producer = _mapping(row["producer"], path="$handoff.producer")
    _exact_keys(
        producer,
        required={"workstream", "component", "component_version", "code_commit"},
        path="$handoff.producer",
    )
    normalized_producer = {
        "workstream": _enum(
            producer["workstream"], {"coordination"}, path="$handoff.producer.workstream"
        ),
        "component": _enum(
            producer["component"],
            {"neutral_workflow_relay_v1"},
            path="$handoff.producer.component",
        ),
        "component_version": _enum(
            producer["component_version"],
            {SCHEMA_VERSION_V1},
            path="$handoff.producer.component_version",
        ),
        "code_commit": _commit(producer["code_commit"], path="$handoff.producer.code_commit"),
    }
    integrity = _mapping(row["integrity"], path="$handoff.integrity")
    _exact_keys(integrity, required={"handoff_sha256"}, path="$handoff.integrity")
    normalized = {
        "schema_id": _enum(row["schema_id"], {"ScoringHandoffV1"}, path="$handoff.schema_id"),
        "schema_version": _enum(
            row["schema_version"], {SCHEMA_VERSION_V1}, path="$handoff.schema_version"
        ),
        "workflow_run_id": _identifier(
            row["workflow_run_id"], path="$handoff.workflow_run_id"
        ),
        "flow_kind": _enum(row["flow_kind"], {FLOW_KIND_V1}, path="$handoff.flow_kind"),
        "handoff_id": _identifier(row["handoff_id"], path="$handoff.handoff_id"),
        "created_at": _rfc3339(row["created_at"], path="$handoff.created_at"),
        "producer": normalized_producer,
        "source_package_bindings": source_bindings,
        "optional_bindings": normalized_optional,
        "translation_inputs": inputs,
        "input_set_sha256": _sha256(
            row["input_set_sha256"], path="$handoff.input_set_sha256"
        ),
        "integrity": {
            "handoff_sha256": _sha256(
                integrity["handoff_sha256"], path="$handoff.integrity.handoff_sha256"
            )
        },
    }
    expected_set = scoring_input_set_sha256_v1(inputs)
    if normalized["input_set_sha256"] != expected_set:
        raise WorkflowReplayContractError(
            "input_set_hash", "$handoff.input_set_sha256", "translation input set hash drift"
        )
    if check_hash and _payload_hash_without_nested(
        normalized, ("integrity", "handoff_sha256")
    ) != normalized["integrity"]["handoff_sha256"]:
        raise WorkflowReplayContractError(
            "handoff_hash", "$handoff.integrity.handoff_sha256", "handoff hash drift"
        )
    return normalized


def _validate_evaluation_producer(value: Any) -> dict[str, str]:
    row = _mapping(value, path="$receipt.producer")
    _exact_keys(
        row,
        required={"workstream", "component", "component_version", "code_commit"},
        path="$receipt.producer",
    )
    return {
        "workstream": _enum(
            row["workstream"], {"evaluation"}, path="$receipt.producer.workstream"
        ),
        "component": _enum(
            row["component"],
            {"workflow_component_v1"},
            path="$receipt.producer.component",
        ),
        "component_version": _enum(
            row["component_version"],
            {SCHEMA_VERSION_V1},
            path="$receipt.producer.component_version",
        ),
        "code_commit": _commit(row["code_commit"], path="$receipt.producer.code_commit"),
    }


def _validate_translation_input(value: Any, *, path: str) -> dict[str, Any]:
    row = _mapping(value, path=path)
    _exact_keys(
        row,
        required={"arm_id", "translation_artifact", "producer", "coverage", "source_binding"},
        path=path,
    )
    producer = _mapping(row["producer"], path=f"{path}.producer")
    _exact_keys(producer, required={"component_id", "component_run_id"}, path=f"{path}.producer")
    component_id = _identifier(producer["component_id"], path=f"{path}.producer.component_id")
    if component_id in {"evaluation", "neutral_relay"}:
        raise WorkflowReplayContractError(
            "producer_authority",
            f"{path}.producer.component_id",
            "Evaluation and relay cannot author translation inputs",
        )
    coverage = _validate_coverage(row["coverage"], path=f"{path}.coverage")
    return {
        "arm_id": _enum(row["arm_id"], set(ARM_IDS_V1), path=f"{path}.arm_id"),
        "translation_artifact": validate_typed_artifact_binding_v1(
            row["translation_artifact"], path=f"{path}.translation_artifact"
        ),
        "producer": {
            "component_id": component_id,
            "component_run_id": _identifier(
                producer["component_run_id"], path=f"{path}.producer.component_run_id"
            ),
        },
        "coverage": coverage,
        "source_binding": validate_typed_artifact_binding_v1(
            row["source_binding"], path=f"{path}.source_binding"
        ),
    }


def _validate_parent_artifact(value: Any, *, path: str) -> dict[str, Any]:
    row = _mapping(value, path=path)
    _exact_keys(
        row,
        required={
            "binding",
            "component_artifact_ref",
            "imported_physical_sha256",
            "producer",
            "parent_artifact_refs",
            "created_event_id",
        },
        path=path,
    )
    producer = _mapping(row["producer"], path=f"{path}.producer")
    _exact_keys(
        producer,
        required={
            "component_id",
            "component_run_id",
            "component_attempt_id",
            "component_attempt_index",
            "stage_id",
        },
        path=f"{path}.producer",
    )
    if producer["component_attempt_id"] is None or producer["component_attempt_index"] is None:
        if producer["component_attempt_id"] is not None or producer["component_attempt_index"] is not None:
            raise WorkflowReplayContractError(
                "attempt_identity", f"{path}.producer", "attempt ID and index must both be null or present"
            )
        attempt_id = None
        attempt_index = None
    else:
        attempt_id, attempt_index = _validate_attempt_pair(
            producer["component_attempt_id"],
            producer["component_attempt_index"],
            path=f"{path}.producer",
        )
    parents = [
        _relative_ref(item, path=f"{path}.parent_artifact_refs[{index}]")
        for index, item in enumerate(
            _list(row["parent_artifact_refs"], path=f"{path}.parent_artifact_refs")
        )
    ]
    if len(parents) != len(set(parents)):
        raise WorkflowReplayContractError(
            "artifact_parent", f"{path}.parent_artifact_refs", "parent refs repeat"
        )
    binding = validate_typed_artifact_binding_v1(row["binding"], path=f"{path}.binding")
    if binding["artifact_ref"] in parents:
        raise WorkflowReplayContractError(
            "artifact_parent", f"{path}.parent_artifact_refs", "artifact cannot parent itself"
        )
    return {
        "binding": binding,
        "component_artifact_ref": _relative_ref(
            row["component_artifact_ref"], path=f"{path}.component_artifact_ref"
        ),
        "imported_physical_sha256": _sha256(
            row["imported_physical_sha256"], path=f"{path}.imported_physical_sha256"
        ),
        "producer": {
            "component_id": _identifier(
                producer["component_id"], path=f"{path}.producer.component_id"
            ),
            "component_run_id": _identifier(
                producer["component_run_id"], path=f"{path}.producer.component_run_id"
            ),
            "component_attempt_id": attempt_id,
            "component_attempt_index": attempt_index,
            "stage_id": _identifier(
                producer["stage_id"], path=f"{path}.producer.stage_id"
            ),
        },
        "parent_artifact_refs": parents,
        "created_event_id": (
            None
            if row["created_event_id"] is None
            else _identifier(row["created_event_id"], path=f"{path}.created_event_id")
        ),
    }


def _validate_parent_component(value: Any, *, path: str) -> dict[str, Any]:
    row = _mapping(value, path=path)
    _exact_keys(
        row,
        required={
            "component_id",
            "component_run_id",
            "component_attempt_id",
            "component_attempt_index",
            "status",
            "manifest",
            "last_component_seq",
            "terminal",
            "validator",
        },
        path=path,
    )
    attempt_id, attempt_index = _validate_attempt_pair(
        row["component_attempt_id"], row["component_attempt_index"], path=path
    )
    status = _enum(
        row["status"], {"pending", "running", "paused", "failed", "succeeded"}, path=f"{path}.status"
    )
    terminal = _exact_bool(row["terminal"], path=f"{path}.terminal")
    if terminal != (status in {"failed", "succeeded"}):
        raise WorkflowReplayContractError(
            "terminal_status", f"{path}.terminal", "terminal flag and status disagree"
        )
    validator = _mapping(row["validator"], path=f"{path}.validator")
    _exact_keys(
        validator,
        required={"validator_id", "validator_revision", "validation_receipt_sha256"},
        path=f"{path}.validator",
    )
    return {
        "component_id": _enum(
            row["component_id"], set(COMPONENT_IDS_V1), path=f"{path}.component_id"
        ),
        "component_run_id": _identifier(
            row["component_run_id"], path=f"{path}.component_run_id"
        ),
        "component_attempt_id": attempt_id,
        "component_attempt_index": attempt_index,
        "status": status,
        "manifest": validate_typed_artifact_binding_v1(row["manifest"], path=f"{path}.manifest"),
        "last_component_seq": _integer(
            row["last_component_seq"], path=f"{path}.last_component_seq", minimum=0
        ),
        "terminal": terminal,
        "validator": {
            "validator_id": _identifier(
                validator["validator_id"], path=f"{path}.validator.validator_id"
            ),
            "validator_revision": _identifier(
                validator["validator_revision"], path=f"{path}.validator.validator_revision"
            ),
            "validation_receipt_sha256": _sha256(
                validator["validation_receipt_sha256"],
                path=f"{path}.validator.validation_receipt_sha256",
            ),
        },
    }


def _validate_parent_stage(value: Any, *, path: str, order: int) -> dict[str, Any]:
    row = _mapping(value, path=path)
    _exact_keys(
        row,
        required={
            "stage_id",
            "component_id",
            "local_stage_id",
            "order",
            "label",
            "producer",
            "status",
            "progress",
            "current_work_id",
            "artifact_refs",
        },
        path=path,
    )
    observed_order = _integer(row["order"], path=f"{path}.order", minimum=1)
    if observed_order != order:
        raise WorkflowReplayContractError(
            "stage_order", f"{path}.order", "stage orders must be contiguous"
        )
    component_id = _enum(
        row["component_id"], set(COMPONENT_IDS_V1), path=f"{path}.component_id"
    )
    stage_id = _identifier(row["stage_id"], path=f"{path}.stage_id")
    if not stage_id.startswith(component_id + "."):
        raise WorkflowReplayContractError(
            "stage_namespace", f"{path}.stage_id", "stage is not component-namespaced"
        )
    progress = row["progress"]
    if progress is not None:
        progress = validate_parent_event_public_payload(progress, maximum_bytes=4096)
    refs = [
        _relative_ref(item, path=f"{path}.artifact_refs[{index}]")
        for index, item in enumerate(_list(row["artifact_refs"], path=f"{path}.artifact_refs"))
    ]
    if len(refs) != len(set(refs)):
        raise WorkflowReplayContractError(
            "duplicate_artifact_ref", f"{path}.artifact_refs", "stage artifact refs repeat"
        )
    return {
        "stage_id": stage_id,
        "component_id": component_id,
        "local_stage_id": _identifier(
            row["local_stage_id"], path=f"{path}.local_stage_id"
        ),
        "order": observed_order,
        "label": _string(row["label"], path=f"{path}.label"),
        "producer": _identifier(row["producer"], path=f"{path}.producer"),
        "status": _enum(
            row["status"], {"pending", "running", "paused", "failed", "succeeded"}, path=f"{path}.status"
        ),
        "progress": progress,
        "current_work_id": (
            None
            if row["current_work_id"] is None
            else _identifier(row["current_work_id"], path=f"{path}.current_work_id")
        ),
        "artifact_refs": refs,
    }


def _validate_attempt_pair(value: Any, index_value: Any, *, path: str) -> tuple[str | int, int]:
    index = _integer(index_value, path=f"{path}.component_attempt_index", minimum=1)
    if isinstance(value, bool):
        raise WorkflowReplayContractError(
            "attempt_identity", f"{path}.component_attempt_id", "bool is not an attempt ID"
        )
    if isinstance(value, int):
        if value != index:
            raise WorkflowReplayContractError(
                "attempt_identity", path, "numeric attempt ID must equal attempt index"
            )
        return value, index
    attempt_id = _identifier(value, path=f"{path}.component_attempt_id")
    return attempt_id, index


def _reject_artifact_parent_cycles(
    graph: Mapping[str, Sequence[str]], *, path: str
) -> None:
    visited: set[str] = set()
    active: set[str] = set()

    def visit(node: str) -> None:
        if node in active:
            raise WorkflowReplayContractError(
                "artifact_parent_cycle", path, "artifact parent graph contains a cycle"
            )
        if node in visited:
            return
        active.add(node)
        for parent in graph[node]:
            visit(parent)
        active.remove(node)
        visited.add(node)

    for artifact_ref in graph:
        visit(artifact_ref)


def _validate_coverage(value: Any, *, path: str) -> dict[str, Any]:
    row = _mapping(value, path=path)
    count_fields = (
        "translated_block_count",
        "preserved_block_count",
        "excluded_block_count",
        "review_held_block_count",
        "missing_block_count",
        "failed_block_count",
    )
    _exact_keys(
        row,
        required={"expected_block_count", "block_universe_sha256", *count_fields},
        path=path,
    )
    expected = _integer(row["expected_block_count"], path=f"{path}.expected_block_count", minimum=1)
    counts = {
        name: _integer(row[name], path=f"{path}.{name}", minimum=0) for name in count_fields
    }
    if sum(counts.values()) != expected:
        raise WorkflowReplayContractError(
            "coverage_accounting", path, "coverage statuses must exact-cover admitted blocks"
        )
    return {
        "expected_block_count": expected,
        "block_universe_sha256": _sha256(
            row["block_universe_sha256"], path=f"{path}.block_universe_sha256"
        ),
        **counts,
    }


def _payload_hash_without_nested(value: Mapping[str, Any], path: tuple[str, str]) -> str:
    payload = copy.deepcopy(dict(value))
    parent = payload.get(path[0])
    if not isinstance(parent, dict):
        raise WorkflowReplayContractError("type", f"$.{path[0]}", "expected an object")
    parent.pop(path[1], None)
    return canonical_sha256(payload)


def _normalize_json(value: Any, *, path: str) -> Any:
    if value is None or isinstance(value, (bool, int, str)):
        return unicodedata.normalize("NFC", value) if isinstance(value, str) else value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise WorkflowReplayContractError("non_finite_number", path, "number must be finite")
        return value
    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for key in sorted(value):
            if not isinstance(key, str):
                raise WorkflowReplayContractError("non_string_key", path, "object keys must be strings")
            normalized_key = unicodedata.normalize("NFC", key)
            result[normalized_key] = _normalize_json(value[key], path=f"{path}.{normalized_key}")
        return result
    if isinstance(value, (list, tuple)):
        return [_normalize_json(item, path=f"{path}[{index}]") for index, item in enumerate(value)]
    raise WorkflowReplayContractError("type", path, "value is not JSON-compatible")


def _assert_no_private_payload(value: Any, *, path: str) -> None:
    if isinstance(value, Mapping):
        for key, child in value.items():
            if not isinstance(key, str):
                raise WorkflowReplayContractError("non_string_key", path, "object keys must be strings")
            normalized = re.sub(r"[^a-z0-9]+", "_", key.casefold()).strip("_")
            if normalized in _FORBIDDEN_PAYLOAD_KEYS or normalized.startswith("raw_prompt") or normalized.startswith("raw_response"):
                raise WorkflowReplayContractError(
                    "private_parent_payload",
                    f"{path}.{key}",
                    "raw content, secrets, credentials, and reference authority are forbidden",
                )
            _assert_no_private_payload(child, path=f"{path}.{key}")
    elif isinstance(value, (list, tuple)):
        for index, child in enumerate(value):
            _assert_no_private_payload(child, path=f"{path}[{index}]")


def _assert_truthful_unknown_cost(value: Any, *, path: str) -> None:
    if isinstance(value, Mapping):
        if value.get("cost_status") == "unknown":
            for key in ("cost", "cost_usd", "provider_cost_usd"):
                if key in value and value[key] is not None:
                    raise WorkflowReplayContractError(
                        "unknown_cost",
                        f"{path}.{key}",
                        "unknown cost must remain null",
                    )
        for key, child in value.items():
            _assert_truthful_unknown_cost(child, path=f"{path}.{key}")
    elif isinstance(value, (list, tuple)):
        for index, child in enumerate(value):
            _assert_truthful_unknown_cost(child, path=f"{path}[{index}]")


def _mapping(value: Any, *, path: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise WorkflowReplayContractError("type", path, "expected an object")
    return value


def _list(value: Any, *, path: str) -> list[Any]:
    if not isinstance(value, list):
        raise WorkflowReplayContractError("type", path, "expected an array")
    return value


def _exact_keys(
    value: Mapping[str, Any], *, required: Iterable[str], optional: Iterable[str] = (), path: str
) -> None:
    required_set = frozenset(required)
    allowed = required_set | frozenset(optional)
    actual = frozenset(value.keys())
    missing = sorted(required_set - actual)
    unknown = sorted(actual - allowed)
    if missing:
        raise WorkflowReplayContractError("missing_keys", path, f"missing: {', '.join(missing)}")
    if unknown:
        raise WorkflowReplayContractError("unknown_keys", path, f"unknown: {', '.join(unknown)}")


def _string(value: Any, *, path: str) -> str:
    if not isinstance(value, str):
        raise WorkflowReplayContractError("type", path, "expected a string")
    result = unicodedata.normalize("NFC", value)
    if not result.strip():
        raise WorkflowReplayContractError("empty_string", path, "string must not be empty")
    return result


def _enum(value: Any, allowed: set[str], *, path: str) -> str:
    result = _string(value, path=path)
    if result not in allowed:
        raise WorkflowReplayContractError("enum", path, f"expected one of: {', '.join(sorted(allowed))}")
    return result


def _identifier(value: Any, *, path: str) -> str:
    result = _string(value, path=path)
    if _ID_RE.fullmatch(result) is None:
        raise WorkflowReplayContractError("identifier", path, "invalid identifier syntax")
    return result


def _sha256(value: Any, *, path: str) -> str:
    result = _string(value, path=path)
    if _SHA256_RE.fullmatch(result) is None:
        raise WorkflowReplayContractError("sha256", path, "expected a 64-character SHA-256")
    return result.lower()


def _commit(value: Any, *, path: str) -> str:
    result = _string(value, path=path).lower()
    if re.fullmatch(r"[0-9a-f]{40}", result) is None:
        raise WorkflowReplayContractError("commit", path, "expected a full Git commit identifier")
    return result


def _integer(value: Any, *, path: str, minimum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise WorkflowReplayContractError("integer", path, f"expected integer >= {minimum}")
    return value


def _exact_bool(value: Any, *, path: str) -> bool:
    if type(value) is not bool:
        raise WorkflowReplayContractError("boolean", path, "expected exact bool")
    return value


def _relative_ref(value: Any, *, path: str) -> str:
    result = _string(value, path=path)
    if "\\" in result or re.match(r"^[A-Za-z]:", result):
        raise WorkflowReplayContractError("relative_path", path, "expected a portable relative path")
    parsed = PurePosixPath(result)
    if parsed.is_absolute() or any(part in {"", ".", ".."} for part in parsed.parts):
        raise WorkflowReplayContractError("relative_path", path, "unsafe relative path")
    return parsed.as_posix()


def _rfc3339(value: Any, *, path: str) -> str:
    result = _string(value, path=path)
    try:
        datetime.fromisoformat(result.replace("Z", "+00:00"))
    except ValueError as exc:
        raise WorkflowReplayContractError("timestamp", path, "expected RFC3339 timestamp") from exc
    return result


__all__ = [
    "ARM_IDS_V1",
    "COMPONENT_IDS_V1",
    "FLOW_KIND_V1",
    "SCHEMA_VERSION_V1",
    "SOURCE_BINDING_ROLES_V1",
    "WorkflowReplayContractError",
    "build_scoring_handoff_v1",
    "canonical_json_bytes",
    "canonical_sha256",
    "normalize_d2l_scoring_fragment_v1",
    "physical_sha256",
    "scoring_input_set_sha256_v1",
    "seal_payload",
    "validate_parent_event_public_payload",
    "validate_scoring_handoff_v1",
    "validate_scoring_receipt_v1",
    "validate_source_package_bindings_v1",
    "validate_typed_artifact_binding_v1",
    "validate_workflow_artifact_index_v1",
    "validate_workflow_event_v1",
    "validate_workflow_manifest_v1",
    "verify_sealed_payload",
]
