from __future__ import annotations

import copy
import hashlib
import json
import re
from typing import Any, Mapping, Sequence

from pipeline.eval.contracts_v1 import (
    CanonicalPolicy,
    ContractValidationError,
    canonical_sha256,
    canonicalize,
    require_commit,
    require_enum,
    require_exact_keys,
    require_int,
    require_list,
    require_mapping,
    require_nullable_string,
    require_relative_path,
    require_rfc3339,
    require_sha256,
    require_string,
    require_unique,
    seal_payload,
    validate_producer,
    verify_payload_hash,
)


__all__ = [
    "ARM_IDS_V1",
    "build_evaluation_artifact_index_v1",
    "build_evaluation_component_event_v1",
    "build_evaluation_component_manifest_v1",
    "build_scoring_receipt_v1",
    "scoring_input_set_sha256_v1",
    "validate_evaluation_artifact_index_v1",
    "validate_evaluation_component_event_v1",
    "validate_evaluation_component_manifest_v1",
    "validate_evaluation_component_stream_v1",
    "validate_scoring_handoff_v1",
    "validate_scoring_receipt_v1",
    "validate_typed_artifact_binding_v1",
]


SCHEMA_VERSION = "1.0.0"
FLOW_KIND = "translation_evaluation_publication"
SCORING_HANDOFF_SCHEMA_ID = "ScoringHandoffV1"
SCORING_RECEIPT_SCHEMA_ID = "ScoringReceiptV1"
COMPONENT_MANIFEST_SCHEMA_ID = "EvaluationWorkflowComponentManifestV1"
COMPONENT_EVENT_SCHEMA_ID = "EvaluationWorkflowComponentEventV1"
ARTIFACT_INDEX_SCHEMA_ID = "EvaluationWorkflowArtifactIndexV1"
ARM_IDS_V1 = ("s0", "s1", "community", "google_nmt", "llm_lc")
SOURCE_BINDING_ROLES_V1 = (
    "document",
    "structure_manifest",
    "asset_manifest",
    "admitted_projection",
    "normalization_receipt",
    "package_seal",
)

_HANDOFF_HASH_PATH = ("integrity", "handoff_sha256")
_RECEIPT_HASH_PATH = ("integrity", "receipt_sha256")
_MANIFEST_HASH_PATH = ("integrity", "manifest_sha256")
_EVENT_HASH_PATH = ("integrity", "event_sha256")
_ARTIFACT_INDEX_HASH_PATH = ("integrity", "artifact_index_sha256")

_HANDOFF_POLICY = CanonicalPolicy(
    set_like_paths=frozenset(),
    semantic_sequence_paths=frozenset(
        {("source_package_bindings",), ("translation_inputs",)}
    ),
)
_RECEIPT_POLICY = CanonicalPolicy(
    set_like_paths=frozenset(),
    semantic_sequence_paths=frozenset({("accepted_translation_inputs",)}),
)
_MANIFEST_POLICY = CanonicalPolicy(
    set_like_paths=frozenset(),
    semantic_sequence_paths=frozenset({("stages",)}),
)
_EVENT_POLICY = CanonicalPolicy(
    set_like_paths=frozenset(),
    semantic_sequence_paths=frozenset(
        {
            ("detail", "data", "arm_ids"),
            ("detail", "data", "metric_ids"),
        }
    ),
)
_ARTIFACT_INDEX_POLICY = CanonicalPolicy(
    set_like_paths=frozenset({("artifacts", "*", "parent_artifact_refs")}),
    semantic_sequence_paths=frozenset({("artifacts",)}),
)
_INPUT_SET_POLICY = CanonicalPolicy(
    set_like_paths=frozenset(),
    semantic_sequence_paths=frozenset({("translation_inputs",)}),
)

_CANONICAL_HASH_KIND_RE = re.compile(r"^canonical:[A-Za-z][A-Za-z0-9_.-]*@[0-9]+\.[0-9]+\.[0-9]+$")
_EVENT_ID_RE = re.compile(r"^evalevt_[0-9a-f]{32}$")
_ATTEMPT_ID_RE = re.compile(r"^evalcomp_attempt_[0-9]{4}$")

_EVENT_TYPES = frozenset(
    {
        "component_started",
        "component_resumed",
        "stage_start",
        "progress",
        "validation_passed",
        "validation_failed",
        "retry",
        "checkpoint",
        "usage_snapshot",
        "stage_done",
        "component_halted",
        "component_done",
        "component_failed",
    }
)
_SEVERITIES = frozenset({"info", "warning", "error"})
_TERMINAL_EVENTS = frozenset({"component_done", "component_failed"})


def validate_typed_artifact_binding_v1(value: Any, *, path: str) -> dict[str, str]:
    row = require_mapping(value, path=path)
    require_exact_keys(
        row,
        required={"artifact_ref", "artifact_kind", "schema_version", "sha256", "sha256_kind"},
        path=path,
    )
    sha_kind = require_string(row["sha256_kind"], path=f"{path}.sha256_kind")
    if sha_kind != "physical" and _CANONICAL_HASH_KIND_RE.fullmatch(sha_kind) is None:
        raise ContractValidationError(
            "sha256_kind",
            f"{path}.sha256_kind",
            "expected physical or canonical:<contract>@<semver>",
        )
    return {
        "artifact_ref": require_relative_path(row["artifact_ref"], path=f"{path}.artifact_ref"),
        "artifact_kind": require_string(row["artifact_kind"], path=f"{path}.artifact_kind"),
        "schema_version": require_string(row["schema_version"], path=f"{path}.schema_version"),
        "sha256": require_sha256(row["sha256"], path=f"{path}.sha256"),
        "sha256_kind": sha_kind,
    }


def scoring_input_set_sha256_v1(translation_inputs: Sequence[Mapping[str, Any]]) -> str:
    """Recompute the relay-declared input-set hash for validation only."""

    normalized = [
        _validate_translation_input(row, path=f"$.translation_inputs[{index}]")
        for index, row in enumerate(translation_inputs)
    ]
    return canonical_sha256(
        {"translation_inputs": normalized}, policy=_INPUT_SET_POLICY
    )


def validate_scoring_handoff_v1(value: Mapping[str, Any]) -> dict[str, Any]:
    row = require_mapping(value, path="$handoff")
    require_exact_keys(
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
    source_bindings = _validate_source_package_bindings(row["source_package_bindings"])
    optional_bindings = _validate_optional_bindings(row["optional_bindings"])
    raw_inputs = require_list(row["translation_inputs"], path="$handoff.translation_inputs")
    translation_inputs = [
        _validate_translation_input(item, path=f"$handoff.translation_inputs[{index}]")
        for index, item in enumerate(raw_inputs)
    ]
    observed_arms = tuple(item["arm_id"] for item in translation_inputs)
    if observed_arms != ARM_IDS_V1:
        raise ContractValidationError(
            "translation_input_order",
            "$handoff.translation_inputs",
            f"expected exact ordered arms: {', '.join(ARM_IDS_V1)}",
        )
    refs = [item["translation_artifact"]["artifact_ref"] for item in translation_inputs]
    require_unique(refs, path="$handoff.translation_inputs[*].translation_artifact.artifact_ref")
    admitted_projection = source_bindings[SOURCE_BINDING_ROLES_V1.index("admitted_projection")][
        "binding"
    ]
    first_universe = translation_inputs[0]["coverage"]["block_universe_sha256"]
    first_expected = translation_inputs[0]["coverage"]["expected_block_count"]
    for index, item in enumerate(translation_inputs):
        if item["source_binding"] != admitted_projection:
            raise ContractValidationError(
                "source_binding_drift",
                f"$handoff.translation_inputs[{index}].source_binding",
                "arm source binding must equal the sealed admitted projection",
            )
        coverage = item["coverage"]
        if (
            coverage["block_universe_sha256"] != first_universe
            or coverage["expected_block_count"] != first_expected
        ):
            raise ContractValidationError(
                "coverage_universe_drift",
                f"$handoff.translation_inputs[{index}].coverage",
                "all arms must cover the same admitted block universe",
            )
    input_set_sha256 = require_sha256(
        row["input_set_sha256"], path="$handoff.input_set_sha256"
    )
    expected_input_set = canonical_sha256(
        {"translation_inputs": translation_inputs}, policy=_INPUT_SET_POLICY
    )
    if input_set_sha256 != expected_input_set:
        raise ContractValidationError(
            "input_set_hash",
            "$handoff.input_set_sha256",
            "translation input set hash drift",
        )
    normalized = {
        "schema_id": require_enum(
            row["schema_id"], {SCORING_HANDOFF_SCHEMA_ID}, path="$handoff.schema_id"
        ),
        "schema_version": require_enum(
            row["schema_version"], {SCHEMA_VERSION}, path="$handoff.schema_version"
        ),
        "workflow_run_id": require_string(
            row["workflow_run_id"], path="$handoff.workflow_run_id"
        ),
        "flow_kind": require_enum(row["flow_kind"], {FLOW_KIND}, path="$handoff.flow_kind"),
        "handoff_id": require_string(row["handoff_id"], path="$handoff.handoff_id"),
        "created_at": require_rfc3339(row["created_at"], path="$handoff.created_at"),
        "producer": _validate_relay_producer(row["producer"]),
        "source_package_bindings": source_bindings,
        "optional_bindings": optional_bindings,
        "translation_inputs": translation_inputs,
        "input_set_sha256": input_set_sha256,
        "integrity": _validate_single_hash(
            row["integrity"], "handoff_sha256", "$handoff.integrity"
        ),
    }
    if not verify_payload_hash(
        normalized, policy=_HANDOFF_POLICY, hash_path=_HANDOFF_HASH_PATH
    ):
        raise ContractValidationError(
            "handoff_hash", "$handoff.integrity.handoff_sha256", "handoff hash drift"
        )
    result = canonicalize(normalized, policy=_HANDOFF_POLICY)
    assert isinstance(result, dict)
    return result


def build_scoring_receipt_v1(
    handoff: Mapping[str, Any],
    *,
    handoff_artifact_ref: str,
    evaluation_component_run_id: str,
    evaluation_component_attempt_id: str,
    accepted_at: str,
    producer_code_commit: str,
    status: str,
    rejection_code: str | None = None,
) -> dict[str, Any]:
    accepted = validate_scoring_handoff_v1(handoff)
    decision = require_enum(status, {"accepted", "rejected"}, path="$.status")
    rejection = require_nullable_string(rejection_code, path="$.rejection_code")
    if (decision == "accepted" and rejection is not None) or (
        decision == "rejected" and rejection is None
    ):
        raise ContractValidationError(
            "receipt_decision",
            "$.rejection_code",
            "accepted requires null; rejected requires a reason code",
        )
    draft = {
        "schema_id": SCORING_RECEIPT_SCHEMA_ID,
        "schema_version": SCHEMA_VERSION,
        "workflow_run_id": accepted["workflow_run_id"],
        "flow_kind": FLOW_KIND,
        "evaluation_component_run_id": require_string(
            evaluation_component_run_id, path="$.evaluation_component_run_id"
        ),
        "evaluation_component_attempt_id": _require_attempt_id(
            evaluation_component_attempt_id, path="$.evaluation_component_attempt_id"
        ),
        "scoring_handoff": {
            "artifact_ref": require_relative_path(
                handoff_artifact_ref, path="$.handoff_artifact_ref"
            ),
            "artifact_kind": "scoring_handoff_v1",
            "schema_version": SCHEMA_VERSION,
            "sha256": accepted["integrity"]["handoff_sha256"],
            "sha256_kind": f"canonical:{SCORING_HANDOFF_SCHEMA_ID}@{SCHEMA_VERSION}",
        },
        "accepted_translation_inputs": copy.deepcopy(accepted["translation_inputs"]),
        "accepted_input_set_sha256": accepted["input_set_sha256"],
        "accepted_at": require_rfc3339(accepted_at, path="$.accepted_at"),
        "status": decision,
        "rejection_code": rejection,
        "producer": {
            "workstream": "evaluation",
            "component": "workflow_component_v1",
            "component_version": SCHEMA_VERSION,
            "code_commit": require_commit(producer_code_commit, path="$.producer_code_commit"),
        },
        "integrity": {"receipt_sha256": "0" * 64},
    }
    return validate_scoring_receipt_v1(
        seal_payload(draft, policy=_RECEIPT_POLICY, hash_path=_RECEIPT_HASH_PATH),
        handoff=accepted,
    )


def validate_scoring_receipt_v1(
    value: Mapping[str, Any], *, handoff: Mapping[str, Any] | None = None
) -> dict[str, Any]:
    row = require_mapping(value, path="$receipt")
    require_exact_keys(
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
    inputs = [
        _validate_translation_input(item, path=f"$receipt.accepted_translation_inputs[{index}]")
        for index, item in enumerate(
            require_list(
                row["accepted_translation_inputs"],
                path="$receipt.accepted_translation_inputs",
            )
        )
    ]
    if tuple(item["arm_id"] for item in inputs) != ARM_IDS_V1:
        raise ContractValidationError(
            "translation_input_order",
            "$receipt.accepted_translation_inputs",
            "receipt must echo all five arms in canonical order",
        )
    status = require_enum(row["status"], {"accepted", "rejected"}, path="$receipt.status")
    rejection = require_nullable_string(
        row["rejection_code"], path="$receipt.rejection_code"
    )
    if (status == "accepted" and rejection is not None) or (
        status == "rejected" and rejection is None
    ):
        raise ContractValidationError(
            "receipt_decision", "$receipt.rejection_code", "invalid receipt decision"
        )
    normalized = {
        "schema_id": require_enum(
            row["schema_id"], {SCORING_RECEIPT_SCHEMA_ID}, path="$receipt.schema_id"
        ),
        "schema_version": require_enum(
            row["schema_version"], {SCHEMA_VERSION}, path="$receipt.schema_version"
        ),
        "workflow_run_id": require_string(
            row["workflow_run_id"], path="$receipt.workflow_run_id"
        ),
        "flow_kind": require_enum(row["flow_kind"], {FLOW_KIND}, path="$receipt.flow_kind"),
        "evaluation_component_run_id": require_string(
            row["evaluation_component_run_id"],
            path="$receipt.evaluation_component_run_id",
        ),
        "evaluation_component_attempt_id": _require_attempt_id(
            row["evaluation_component_attempt_id"],
            path="$receipt.evaluation_component_attempt_id",
        ),
        "scoring_handoff": validate_typed_artifact_binding_v1(
            row["scoring_handoff"], path="$receipt.scoring_handoff"
        ),
        "accepted_translation_inputs": inputs,
        "accepted_input_set_sha256": require_sha256(
            row["accepted_input_set_sha256"],
            path="$receipt.accepted_input_set_sha256",
        ),
        "accepted_at": require_rfc3339(row["accepted_at"], path="$receipt.accepted_at"),
        "status": status,
        "rejection_code": rejection,
        "producer": validate_producer(
            row["producer"], path="$receipt.producer", workstream="evaluation"
        ),
        "integrity": _validate_single_hash(
            row["integrity"], "receipt_sha256", "$receipt.integrity"
        ),
    }
    if normalized["scoring_handoff"]["artifact_kind"] != "scoring_handoff_v1":
        raise ContractValidationError(
            "handoff_binding",
            "$receipt.scoring_handoff.artifact_kind",
            "receipt must bind scoring_handoff_v1",
        )
    expected_input_set = canonical_sha256(
        {"translation_inputs": inputs}, policy=_INPUT_SET_POLICY
    )
    if normalized["accepted_input_set_sha256"] != expected_input_set:
        raise ContractValidationError(
            "input_set_hash",
            "$receipt.accepted_input_set_sha256",
            "receipt input set hash drift",
        )
    if not verify_payload_hash(
        normalized, policy=_RECEIPT_POLICY, hash_path=_RECEIPT_HASH_PATH
    ):
        raise ContractValidationError(
            "receipt_hash", "$receipt.integrity.receipt_sha256", "receipt hash drift"
        )
    if handoff is not None:
        accepted_handoff = validate_scoring_handoff_v1(handoff)
        if normalized["workflow_run_id"] != accepted_handoff["workflow_run_id"]:
            raise ContractValidationError(
                "workflow_binding", "$receipt.workflow_run_id", "foreign workflow"
            )
        if normalized["scoring_handoff"]["sha256"] != accepted_handoff["integrity"]["handoff_sha256"]:
            raise ContractValidationError(
                "handoff_binding", "$receipt.scoring_handoff.sha256", "foreign handoff"
            )
        if normalized["accepted_translation_inputs"] != accepted_handoff["translation_inputs"]:
            raise ContractValidationError(
                "receipt_echo",
                "$receipt.accepted_translation_inputs",
                "receipt must byte-semantically echo accepted input rows",
            )
        if normalized["accepted_input_set_sha256"] != accepted_handoff["input_set_sha256"]:
            raise ContractValidationError(
                "receipt_echo",
                "$receipt.accepted_input_set_sha256",
                "receipt must echo the relay input-set hash",
            )
    result = canonicalize(normalized, policy=_RECEIPT_POLICY)
    assert isinstance(result, dict)
    return result


def build_evaluation_component_manifest_v1(
    *,
    workflow_run_id: str,
    component_run_id: str,
    component_attempt_id: str,
    component_attempt_index: int,
    manifest_revision: int,
    previous_manifest_sha256: str | None,
    created_at: str,
    producer_code_commit: str,
    scoring_handoff: Mapping[str, Any],
    scoring_receipt_ref: str,
    accepted_input_set_sha256: str,
    evaluation_profile: Mapping[str, Any],
    stages: Sequence[Mapping[str, Any]],
    workflow_settings: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    handoff_binding = validate_typed_artifact_binding_v1(
        scoring_handoff, path="$.scoring_handoff"
    )
    profile_binding = validate_typed_artifact_binding_v1(
        evaluation_profile, path="$.evaluation_profile"
    )
    normalized_stages = [
        _validate_stage(stage, path=f"$.stages[{index}]", expected_ordinal=index)
        for index, stage in enumerate(stages)
    ]
    if not normalized_stages:
        raise ContractValidationError("stage_exact_cover", "$.stages", "at least one stage required")
    require_unique([stage["stage_id"] for stage in normalized_stages], path="$.stages[*].stage_id")
    attempt_index = require_int(
        component_attempt_index, path="$.component_attempt_index", minimum=1
    )
    attempt_id = _require_attempt_id(component_attempt_id, path="$.component_attempt_id")
    _require_attempt_pair(attempt_id, attempt_index, path="$.component_attempt_id")
    revision = require_int(manifest_revision, path="$.manifest_revision", minimum=1)
    previous = _require_nullable_sha(previous_manifest_sha256, "$.previous_manifest_sha256")
    if (revision == 1 and previous is not None) or (revision > 1 and previous is None):
        raise ContractValidationError(
            "manifest_lineage",
            "$.previous_manifest_sha256",
            "revision 1 has no parent; later revisions require one",
        )
    draft = {
        "schema_id": COMPONENT_MANIFEST_SCHEMA_ID,
        "schema_version": SCHEMA_VERSION,
        "workflow_run_id": require_string(workflow_run_id, path="$.workflow_run_id"),
        "flow_kind": FLOW_KIND,
        "component_id": "evaluation",
        "component_run_id": require_string(component_run_id, path="$.component_run_id"),
        "component_attempt_id": attempt_id,
        "component_attempt_index": attempt_index,
        "manifest_revision": revision,
        "previous_manifest_sha256": previous,
        "created_at": require_rfc3339(created_at, path="$.created_at"),
        "producer": {
            "workstream": "evaluation",
            "component": "workflow_component_v1",
            "component_version": SCHEMA_VERSION,
            "code_commit": require_commit(producer_code_commit, path="$.producer_code_commit"),
        },
        "scoring_handoff": handoff_binding,
        "scoring_receipt_ref": require_relative_path(
            scoring_receipt_ref, path="$.scoring_receipt_ref"
        ),
        "accepted_input_set_sha256": require_sha256(
            accepted_input_set_sha256, path="$.accepted_input_set_sha256"
        ),
        "evaluation_profile": profile_binding,
        "stages": normalized_stages,
        "integrity": {"manifest_sha256": "0" * 64},
    }
    if workflow_settings is not None:
        draft["workflow_settings"] = validate_typed_artifact_binding_v1(
            workflow_settings, path="$.workflow_settings"
        )
    return validate_evaluation_component_manifest_v1(
        seal_payload(draft, policy=_MANIFEST_POLICY, hash_path=_MANIFEST_HASH_PATH)
    )


def validate_evaluation_component_manifest_v1(value: Mapping[str, Any]) -> dict[str, Any]:
    row = require_mapping(value, path="$manifest")
    require_exact_keys(
        row,
        required={
            "schema_id",
            "schema_version",
            "workflow_run_id",
            "flow_kind",
            "component_id",
            "component_run_id",
            "component_attempt_id",
            "component_attempt_index",
            "manifest_revision",
            "previous_manifest_sha256",
            "created_at",
            "producer",
            "scoring_handoff",
            "scoring_receipt_ref",
            "accepted_input_set_sha256",
            "evaluation_profile",
            "stages",
            "integrity",
        },
        optional={"workflow_settings"},
        path="$manifest",
    )
    attempt_index = require_int(
        row["component_attempt_index"], path="$manifest.component_attempt_index", minimum=1
    )
    attempt_id = _require_attempt_id(
        row["component_attempt_id"], path="$manifest.component_attempt_id"
    )
    _require_attempt_pair(attempt_id, attempt_index, path="$manifest.component_attempt_id")
    revision = require_int(row["manifest_revision"], path="$manifest.manifest_revision", minimum=1)
    previous = _require_nullable_sha(
        row["previous_manifest_sha256"], "$manifest.previous_manifest_sha256"
    )
    if (revision == 1 and previous is not None) or (revision > 1 and previous is None):
        raise ContractValidationError("manifest_lineage", "$manifest.previous_manifest_sha256", "invalid revision lineage")
    stages = [
        _validate_stage(stage, path=f"$manifest.stages[{index}]", expected_ordinal=index)
        for index, stage in enumerate(require_list(row["stages"], path="$manifest.stages"))
    ]
    if not stages:
        raise ContractValidationError("stage_exact_cover", "$manifest.stages", "at least one stage required")
    require_unique([stage["stage_id"] for stage in stages], path="$manifest.stages[*].stage_id")
    accepted_input_set = row["accepted_input_set_sha256"]
    if accepted_input_set is not None:
        accepted_input_set = require_sha256(
            accepted_input_set, path="$manifest.accepted_input_set_sha256"
        )
    normalized = {
        "schema_id": require_enum(row["schema_id"], {COMPONENT_MANIFEST_SCHEMA_ID}, path="$manifest.schema_id"),
        "schema_version": require_enum(row["schema_version"], {SCHEMA_VERSION}, path="$manifest.schema_version"),
        "workflow_run_id": require_string(row["workflow_run_id"], path="$manifest.workflow_run_id"),
        "flow_kind": require_enum(row["flow_kind"], {FLOW_KIND}, path="$manifest.flow_kind"),
        "component_id": require_enum(row["component_id"], {"evaluation"}, path="$manifest.component_id"),
        "component_run_id": require_string(row["component_run_id"], path="$manifest.component_run_id"),
        "component_attempt_id": attempt_id,
        "component_attempt_index": attempt_index,
        "manifest_revision": revision,
        "previous_manifest_sha256": previous,
        "created_at": require_rfc3339(row["created_at"], path="$manifest.created_at"),
        "producer": validate_producer(row["producer"], path="$manifest.producer", workstream="evaluation"),
        "scoring_handoff": validate_typed_artifact_binding_v1(row["scoring_handoff"], path="$manifest.scoring_handoff"),
        "scoring_receipt_ref": require_relative_path(row["scoring_receipt_ref"], path="$manifest.scoring_receipt_ref"),
        "accepted_input_set_sha256": accepted_input_set,
        "evaluation_profile": validate_typed_artifact_binding_v1(row["evaluation_profile"], path="$manifest.evaluation_profile"),
        "stages": stages,
        "integrity": _validate_single_hash(row["integrity"], "manifest_sha256", "$manifest.integrity"),
    }
    if "workflow_settings" in row:
        normalized["workflow_settings"] = validate_typed_artifact_binding_v1(
            row["workflow_settings"], path="$manifest.workflow_settings"
        )
    if normalized["scoring_handoff"]["artifact_kind"] != "scoring_handoff_v1":
        raise ContractValidationError("handoff_binding", "$manifest.scoring_handoff.artifact_kind", "expected scoring_handoff_v1")
    if not verify_payload_hash(normalized, policy=_MANIFEST_POLICY, hash_path=_MANIFEST_HASH_PATH):
        raise ContractValidationError("manifest_hash", "$manifest.integrity.manifest_sha256", "manifest hash drift")
    result = canonicalize(normalized, policy=_MANIFEST_POLICY)
    assert isinstance(result, dict)
    return result


def build_evaluation_component_event_v1(
    manifest: Mapping[str, Any],
    *,
    component_seq: int,
    component_attempt_id: str,
    component_attempt_index: int,
    ts: str,
    stage_id: str,
    agent: str,
    event: str,
    severity: str,
    payload: Mapping[str, Any],
    previous_event_sha256: str | None,
    detail: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    accepted_manifest = validate_evaluation_component_manifest_v1(manifest)
    draft_without_id = {
        "schema_id": COMPONENT_EVENT_SCHEMA_ID,
        "schema_version": SCHEMA_VERSION,
        "workflow_run_id": accepted_manifest["workflow_run_id"],
        "flow_kind": FLOW_KIND,
        "component_id": "evaluation",
        "component_run_id": accepted_manifest["component_run_id"],
        "component_attempt_id": _require_attempt_id(component_attempt_id, path="$.component_attempt_id"),
        "component_attempt_index": require_int(component_attempt_index, path="$.component_attempt_index", minimum=1),
        "component_seq": require_int(component_seq, path="$.component_seq", minimum=1),
        "ts": require_rfc3339(ts, path="$.ts"),
        "stage_id": require_string(stage_id, path="$.stage_id"),
        "agent": require_string(agent, path="$.agent"),
        "event": require_enum(event, _EVENT_TYPES, path="$.event"),
        "severity": require_enum(severity, _SEVERITIES, path="$.severity"),
        "payload": _validate_event_payload(event, payload, path="$.payload"),
        "previous_event_sha256": _require_nullable_sha(previous_event_sha256, "$.previous_event_sha256"),
        "manifest_sha256": accepted_manifest["integrity"]["manifest_sha256"],
    }
    if detail is not None:
        draft_without_id["detail"] = _validate_evaluation_detail(
            detail, path="$.detail"
        )
    _require_attempt_pair(
        draft_without_id["component_attempt_id"],
        draft_without_id["component_attempt_index"],
        path="$.component_attempt_id",
    )
    event_id = "evalevt_" + canonical_sha256(draft_without_id, policy=_EVENT_POLICY)[:32]
    draft = {
        **draft_without_id,
        "event_id": event_id,
        "integrity": {"event_sha256": "0" * 64},
    }
    return validate_evaluation_component_event_v1(
        seal_payload(draft, policy=_EVENT_POLICY, hash_path=_EVENT_HASH_PATH)
    )


def validate_evaluation_component_event_v1(value: Mapping[str, Any]) -> dict[str, Any]:
    row = require_mapping(value, path="$event")
    require_exact_keys(
        row,
        required={
            "schema_id", "schema_version", "event_id", "workflow_run_id", "flow_kind",
            "component_id", "component_run_id", "component_attempt_id",
            "component_attempt_index", "component_seq", "ts", "stage_id", "agent",
            "event", "severity", "payload", "previous_event_sha256", "manifest_sha256",
            "integrity",
        },
        optional={"detail"},
        path="$event",
    )
    event_type = require_enum(row["event"], _EVENT_TYPES, path="$event.event")
    attempt_index = require_int(row["component_attempt_index"], path="$event.component_attempt_index", minimum=1)
    attempt_id = _require_attempt_id(row["component_attempt_id"], path="$event.component_attempt_id")
    _require_attempt_pair(attempt_id, attempt_index, path="$event.component_attempt_id")
    normalized = {
        "schema_id": require_enum(row["schema_id"], {COMPONENT_EVENT_SCHEMA_ID}, path="$event.schema_id"),
        "schema_version": require_enum(row["schema_version"], {SCHEMA_VERSION}, path="$event.schema_version"),
        "event_id": _require_event_id(row["event_id"], path="$event.event_id"),
        "workflow_run_id": require_string(row["workflow_run_id"], path="$event.workflow_run_id"),
        "flow_kind": require_enum(row["flow_kind"], {FLOW_KIND}, path="$event.flow_kind"),
        "component_id": require_enum(row["component_id"], {"evaluation"}, path="$event.component_id"),
        "component_run_id": require_string(row["component_run_id"], path="$event.component_run_id"),
        "component_attempt_id": attempt_id,
        "component_attempt_index": attempt_index,
        "component_seq": require_int(row["component_seq"], path="$event.component_seq", minimum=1),
        "ts": require_rfc3339(row["ts"], path="$event.ts"),
        "stage_id": require_string(row["stage_id"], path="$event.stage_id"),
        "agent": require_string(row["agent"], path="$event.agent"),
        "event": event_type,
        "severity": require_enum(row["severity"], _SEVERITIES, path="$event.severity"),
        "payload": _validate_event_payload(event_type, row["payload"], path="$event.payload"),
        "previous_event_sha256": _require_nullable_sha(row["previous_event_sha256"], "$event.previous_event_sha256"),
        "manifest_sha256": require_sha256(row["manifest_sha256"], path="$event.manifest_sha256"),
        "integrity": _validate_single_hash(row["integrity"], "event_sha256", "$event.integrity"),
    }
    if "detail" in row:
        normalized["detail"] = _validate_evaluation_detail(
            row["detail"], path="$event.detail"
        )
    id_material = {key: value for key, value in normalized.items() if key not in {"event_id", "integrity"}}
    expected_id = "evalevt_" + canonical_sha256(id_material, policy=_EVENT_POLICY)[:32]
    if normalized["event_id"] != expected_id:
        raise ContractValidationError("event_id", "$event.event_id", "event identity drift")
    if not verify_payload_hash(normalized, policy=_EVENT_POLICY, hash_path=_EVENT_HASH_PATH):
        raise ContractValidationError("event_hash", "$event.integrity.event_sha256", "event hash drift")
    return normalized


def validate_evaluation_component_stream_v1(
    manifest: Mapping[str, Any],
    events: Sequence[Mapping[str, Any]],
    *,
    manifest_revisions: Sequence[Mapping[str, Any]] = (),
) -> tuple[dict[str, Any], ...]:
    accepted_manifest = validate_evaluation_component_manifest_v1(manifest)
    accepted_revisions = [
        validate_evaluation_component_manifest_v1(item) for item in manifest_revisions
    ]
    manifests_by_hash = {
        item["integrity"]["manifest_sha256"]: item
        for item in [*accepted_revisions, accepted_manifest]
    }
    if len(manifests_by_hash) != len(accepted_revisions) + 1:
        raise ContractValidationError(
            "manifest_revision_duplicate",
            "$manifest_revisions",
            "manifest revisions must have unique hashes",
        )
    if not events:
        raise ContractValidationError("event_exact_cover", "$events", "event stream must not be empty")
    stages = {stage["stage_id"]: stage for stage in accepted_manifest["stages"]}
    stage_states = {stage_id: "pending" for stage_id in stages}
    normalized: list[dict[str, Any]] = []
    previous_hash: str | None = None
    current_attempt = 0
    attempt_open = False
    terminal = False
    seen_event_ids: set[str] = set()
    for index, raw in enumerate(events):
        path = f"$events[{index}]"
        event = validate_evaluation_component_event_v1(raw)
        if terminal:
            raise ContractValidationError("terminal_append", path, "terminal component cannot receive events")
        for field in ("workflow_run_id", "component_run_id"):
            if event[field] != accepted_manifest[field]:
                raise ContractValidationError("component_binding", f"{path}.{field}", "foreign component event")
        event_manifest = manifests_by_hash.get(event["manifest_sha256"])
        if event_manifest is None:
            raise ContractValidationError(
                "manifest_binding",
                f"{path}.manifest_sha256",
                "event binds an unknown manifest revision",
            )
        if event_manifest["workflow_run_id"] != accepted_manifest["workflow_run_id"] or event_manifest["component_run_id"] != accepted_manifest["component_run_id"]:
            raise ContractValidationError(
                "manifest_binding",
                f"{path}.manifest_sha256",
                "event binds a foreign component manifest",
            )
        if event["component_seq"] != index + 1:
            raise ContractValidationError("component_sequence", f"{path}.component_seq", "component sequence must be contiguous from one")
        if event["previous_event_sha256"] != previous_hash:
            raise ContractValidationError("event_chain", f"{path}.previous_event_sha256", "event hash chain drift")
        if event["event_id"] in seen_event_ids:
            raise ContractValidationError("event_id_reuse", f"{path}.event_id", "event ID reused")
        seen_event_ids.add(event["event_id"])
        event_type = event["event"]
        attempt_index = event["component_attempt_index"]
        if index == 0:
            if event_type != "component_started" or attempt_index != 1 or event_manifest["component_attempt_index"] != 1:
                raise ContractValidationError("component_start", path, "first event must start attempt 1")
            current_attempt = 1
            attempt_open = True
        elif event_type == "component_resumed":
            if attempt_open or attempt_index != current_attempt + 1 or event_manifest["component_attempt_index"] != attempt_index:
                raise ContractValidationError("component_resume", path, "resume must follow a halt and increment attempt")
            if event["payload"]["resumed_from_attempt_id"] != _attempt_id(current_attempt):
                raise ContractValidationError("component_resume", f"{path}.payload.resumed_from_attempt_id", "resume lineage drift")
            current_attempt = attempt_index
            attempt_open = True
        elif attempt_index != current_attempt or event_manifest["component_attempt_index"] != attempt_index or not attempt_open:
            raise ContractValidationError("component_attempt", path, "event is outside the active component attempt")
        if event_type == "usage_snapshot" and event["stage_id"] == "__component__":
            if event["agent"] != "runner":
                raise ContractValidationError(
                    "stage_binding",
                    f"{path}.agent",
                    "component-level usage snapshot must be emitted by the runner",
                )
        elif event_type not in {"component_started", "component_resumed", "component_halted", "component_done", "component_failed"}:
            stage_id = event["stage_id"]
            if stage_id not in stages or event["agent"] != stages[stage_id]["agent"]:
                raise ContractValidationError("stage_binding", f"{path}.stage_id", "unknown stage or agent")
            state = stage_states[stage_id]
            if event_type == "stage_start":
                if state not in {"pending", "halted"}:
                    raise ContractValidationError("stage_state", path, "stage cannot start from current state")
                stage_states[stage_id] = "running"
            elif event_type in {"progress", "validation_passed", "validation_failed", "retry", "checkpoint", "usage_snapshot"}:
                if state != "running":
                    raise ContractValidationError("stage_state", path, "stage detail requires a running stage")
            elif event_type == "stage_done":
                if state != "running":
                    raise ContractValidationError("stage_state", path, "stage_done requires a running stage")
                stage_states[stage_id] = event["payload"]["outcome"]
        if event_type == "component_halted":
            attempt_open = False
            for stage_id, state in tuple(stage_states.items()):
                if state == "running":
                    stage_states[stage_id] = "halted"
        elif event_type in _TERMINAL_EVENTS:
            attempt_open = False
            terminal = True
        previous_hash = event["integrity"]["event_sha256"]
        normalized.append(event)
    if current_attempt != accepted_manifest["component_attempt_index"]:
        raise ContractValidationError("manifest_attempt", "$manifest.component_attempt_index", "manifest does not name latest component attempt")
    if normalized[-1]["component_attempt_id"] != accepted_manifest["component_attempt_id"]:
        raise ContractValidationError("manifest_attempt", "$manifest.component_attempt_id", "manifest attempt identity drift")
    return tuple(copy.deepcopy(normalized))


def build_evaluation_artifact_index_v1(
    manifest: Mapping[str, Any],
    *,
    generated_at: str,
    artifacts: Sequence[Mapping[str, Any]],
    producer_code_commit: str,
) -> dict[str, Any]:
    accepted_manifest = validate_evaluation_component_manifest_v1(manifest)
    normalized_artifacts = [
        _validate_artifact_row(item, path=f"$.artifacts[{index}]")
        for index, item in enumerate(artifacts)
    ]
    require_unique(
        [item["artifact"]["artifact_ref"] for item in normalized_artifacts],
        path="$.artifacts[*].artifact.artifact_ref",
    )
    stage_ids = {stage["stage_id"] for stage in accepted_manifest["stages"]}
    for index, item in enumerate(normalized_artifacts):
        if item["stage_id"] not in stage_ids:
            raise ContractValidationError("stage_binding", f"$.artifacts[{index}].stage_id", "unknown stage")
    draft = {
        "schema_id": ARTIFACT_INDEX_SCHEMA_ID,
        "schema_version": SCHEMA_VERSION,
        "workflow_run_id": accepted_manifest["workflow_run_id"],
        "flow_kind": FLOW_KIND,
        "component_id": "evaluation",
        "component_run_id": accepted_manifest["component_run_id"],
        "component_attempt_id": accepted_manifest["component_attempt_id"],
        "generated_at": require_rfc3339(generated_at, path="$.generated_at"),
        "manifest_sha256": accepted_manifest["integrity"]["manifest_sha256"],
        "producer": {
            "workstream": "evaluation",
            "component": "workflow_component_v1",
            "component_version": SCHEMA_VERSION,
            "code_commit": require_commit(producer_code_commit, path="$.producer_code_commit"),
        },
        "artifacts": normalized_artifacts,
        "integrity": {"artifact_index_sha256": "0" * 64},
    }
    return validate_evaluation_artifact_index_v1(
        seal_payload(draft, policy=_ARTIFACT_INDEX_POLICY, hash_path=_ARTIFACT_INDEX_HASH_PATH),
        manifest=accepted_manifest,
    )


def validate_evaluation_artifact_index_v1(
    value: Mapping[str, Any], *, manifest: Mapping[str, Any] | None = None
) -> dict[str, Any]:
    row = require_mapping(value, path="$artifact_index")
    require_exact_keys(
        row,
        required={"schema_id", "schema_version", "workflow_run_id", "flow_kind", "component_id", "component_run_id", "component_attempt_id", "generated_at", "manifest_sha256", "producer", "artifacts", "integrity"},
        path="$artifact_index",
    )
    artifacts = [
        _validate_artifact_row(item, path=f"$artifact_index.artifacts[{index}]")
        for index, item in enumerate(require_list(row["artifacts"], path="$artifact_index.artifacts"))
    ]
    refs = [item["artifact"]["artifact_ref"] for item in artifacts]
    require_unique(refs, path="$artifact_index.artifacts[*].artifact.artifact_ref")
    ref_set = set(refs)
    for index, item in enumerate(artifacts):
        for parent in item["parent_artifact_refs"]:
            if parent not in ref_set:
                raise ContractValidationError("artifact_parent", f"$artifact_index.artifacts[{index}].parent_artifact_refs", "unknown parent artifact")
    normalized = {
        "schema_id": require_enum(row["schema_id"], {ARTIFACT_INDEX_SCHEMA_ID}, path="$artifact_index.schema_id"),
        "schema_version": require_enum(row["schema_version"], {SCHEMA_VERSION}, path="$artifact_index.schema_version"),
        "workflow_run_id": require_string(row["workflow_run_id"], path="$artifact_index.workflow_run_id"),
        "flow_kind": require_enum(row["flow_kind"], {FLOW_KIND}, path="$artifact_index.flow_kind"),
        "component_id": require_enum(row["component_id"], {"evaluation"}, path="$artifact_index.component_id"),
        "component_run_id": require_string(row["component_run_id"], path="$artifact_index.component_run_id"),
        "component_attempt_id": _require_attempt_id(row["component_attempt_id"], path="$artifact_index.component_attempt_id"),
        "generated_at": require_rfc3339(row["generated_at"], path="$artifact_index.generated_at"),
        "manifest_sha256": require_sha256(row["manifest_sha256"], path="$artifact_index.manifest_sha256"),
        "producer": validate_producer(row["producer"], path="$artifact_index.producer", workstream="evaluation"),
        "artifacts": artifacts,
        "integrity": _validate_single_hash(row["integrity"], "artifact_index_sha256", "$artifact_index.integrity"),
    }
    if not verify_payload_hash(normalized, policy=_ARTIFACT_INDEX_POLICY, hash_path=_ARTIFACT_INDEX_HASH_PATH):
        raise ContractValidationError("artifact_index_hash", "$artifact_index.integrity.artifact_index_sha256", "artifact index hash drift")
    if manifest is not None:
        accepted_manifest = validate_evaluation_component_manifest_v1(manifest)
        for field in ("workflow_run_id", "component_run_id", "component_attempt_id"):
            if normalized[field] != accepted_manifest[field]:
                raise ContractValidationError("component_binding", f"$artifact_index.{field}", "foreign component index")
        if normalized["manifest_sha256"] != accepted_manifest["integrity"]["manifest_sha256"]:
            raise ContractValidationError("manifest_binding", "$artifact_index.manifest_sha256", "foreign manifest")
    result = canonicalize(normalized, policy=_ARTIFACT_INDEX_POLICY)
    assert isinstance(result, dict)
    return result


def _validate_source_package_bindings(value: Any) -> list[dict[str, Any]]:
    rows = require_list(value, path="$handoff.source_package_bindings")
    if len(rows) != len(SOURCE_BINDING_ROLES_V1):
        raise ContractValidationError(
            "source_binding_exact_cover",
            "$handoff.source_package_bindings",
            "canonical source package requires six typed bindings",
        )
    result = []
    for index, raw in enumerate(rows):
        path = f"$handoff.source_package_bindings[{index}]"
        row = require_mapping(raw, path=path)
        require_exact_keys(row, required={"role", "binding"}, path=path)
        result.append(
            {
                "role": require_enum(
                    row["role"], {SOURCE_BINDING_ROLES_V1[index]}, path=f"{path}.role"
                ),
                "binding": validate_typed_artifact_binding_v1(row["binding"], path=f"{path}.binding"),
            }
        )
    return result


def _validate_optional_bindings(value: Any) -> dict[str, dict[str, str] | None]:
    row = require_mapping(value, path="$handoff.optional_bindings")
    require_exact_keys(row, required={"glossary", "context", "projection"}, path="$handoff.optional_bindings")
    result: dict[str, dict[str, str] | None] = {}
    for name in ("glossary", "context", "projection"):
        item = row[name]
        result[name] = None if item is None else validate_typed_artifact_binding_v1(item, path=f"$handoff.optional_bindings.{name}")
    return result


def _validate_translation_input(value: Any, *, path: str) -> dict[str, Any]:
    row = require_mapping(value, path=path)
    require_exact_keys(row, required={"arm_id", "translation_artifact", "producer", "coverage", "source_binding"}, path=path)
    producer = require_mapping(row["producer"], path=f"{path}.producer")
    require_exact_keys(producer, required={"component_id", "component_run_id"}, path=f"{path}.producer")
    producer_component = require_string(producer["component_id"], path=f"{path}.producer.component_id")
    if producer_component in {"evaluation", "neutral_relay"}:
        raise ContractValidationError("producer_authority", f"{path}.producer.component_id", "Evaluation and relay cannot author translation inputs")
    return {
        "arm_id": require_enum(row["arm_id"], set(ARM_IDS_V1), path=f"{path}.arm_id"),
        "translation_artifact": validate_typed_artifact_binding_v1(row["translation_artifact"], path=f"{path}.translation_artifact"),
        "producer": {
            "component_id": producer_component,
            "component_run_id": require_string(producer["component_run_id"], path=f"{path}.producer.component_run_id"),
        },
        "coverage": _validate_coverage(row["coverage"], path=f"{path}.coverage"),
        "source_binding": validate_typed_artifact_binding_v1(row["source_binding"], path=f"{path}.source_binding"),
    }


def _validate_coverage(value: Any, *, path: str) -> dict[str, Any]:
    row = require_mapping(value, path=path)
    count_fields = (
        "translated_block_count", "preserved_block_count", "excluded_block_count",
        "review_held_block_count", "missing_block_count", "failed_block_count",
    )
    require_exact_keys(row, required={"expected_block_count", "block_universe_sha256", *count_fields}, path=path)
    expected = require_int(row["expected_block_count"], path=f"{path}.expected_block_count", minimum=1)
    counts = {name: require_int(row[name], path=f"{path}.{name}", minimum=0) for name in count_fields}
    if sum(counts.values()) != expected:
        raise ContractValidationError("coverage_accounting", path, "coverage statuses must exactly cover admitted blocks")
    return {
        "expected_block_count": expected,
        "block_universe_sha256": require_sha256(row["block_universe_sha256"], path=f"{path}.block_universe_sha256"),
        **counts,
    }


def _validate_relay_producer(value: Any) -> dict[str, str]:
    producer = validate_producer(value, path="$handoff.producer", workstream="coordination")
    if producer["component"] != "neutral_workflow_relay_v1":
        raise ContractValidationError("producer_authority", "$handoff.producer.component", "only neutral relay may compose the five-arm handoff")
    return producer


def _validate_stage(value: Any, *, path: str, expected_ordinal: int) -> dict[str, Any]:
    row = require_mapping(value, path=path)
    require_exact_keys(row, required={"stage_id", "ordinal", "agent"}, path=path)
    ordinal = require_int(row["ordinal"], path=f"{path}.ordinal", minimum=0)
    if ordinal != expected_ordinal:
        raise ContractValidationError("stage_order", f"{path}.ordinal", "stage ordinal drift")
    return {
        "stage_id": require_string(row["stage_id"], path=f"{path}.stage_id"),
        "ordinal": ordinal,
        "agent": require_string(row["agent"], path=f"{path}.agent"),
    }


def _validate_event_payload(event_type: str, value: Any, *, path: str) -> dict[str, Any]:
    row = require_mapping(value, path=path)
    if event_type == "component_started":
        require_exact_keys(row, required={"stage_count"}, path=path)
        return {"stage_count": require_int(row["stage_count"], path=f"{path}.stage_count", minimum=1)}
    if event_type == "component_resumed":
        require_exact_keys(row, required={"resumed_from_attempt_id", "checkpoint"}, path=path)
        return {
            "resumed_from_attempt_id": _require_attempt_id(row["resumed_from_attempt_id"], path=f"{path}.resumed_from_attempt_id"),
            "checkpoint": validate_typed_artifact_binding_v1(row["checkpoint"], path=f"{path}.checkpoint"),
        }
    if event_type == "stage_start":
        require_exact_keys(row, required={"work_total", "work_unit"}, path=path)
        return {
            "work_total": require_int(row["work_total"], path=f"{path}.work_total", minimum=0),
            "work_unit": require_string(row["work_unit"], path=f"{path}.work_unit"),
        }
    if event_type == "progress":
        require_exact_keys(row, required={"completed", "total", "unit", "current_work_id"}, path=path)
        completed = require_int(row["completed"], path=f"{path}.completed", minimum=0)
        total = require_int(row["total"], path=f"{path}.total", minimum=0)
        if completed > total:
            raise ContractValidationError("progress", f"{path}.completed", "completed cannot exceed total")
        return {
            "completed": completed,
            "total": total,
            "unit": require_string(row["unit"], path=f"{path}.unit"),
            "current_work_id": require_nullable_string(row["current_work_id"], path=f"{path}.current_work_id"),
        }
    if event_type in {"validation_passed", "validation_failed"}:
        required = {"validator_id"} if event_type == "validation_passed" else {"validator_id", "reason_code"}
        require_exact_keys(row, required=required, path=path)
        result = {"validator_id": require_string(row["validator_id"], path=f"{path}.validator_id")}
        if event_type == "validation_failed":
            result["reason_code"] = require_string(row["reason_code"], path=f"{path}.reason_code")
        return result
    if event_type == "retry":
        require_exact_keys(row, required={"retry_kind", "logical_request_id", "physical_attempt_index", "reason_code"}, path=path)
        retry_kind = require_enum(row["retry_kind"], {"transport", "semantic"}, path=f"{path}.retry_kind")
        physical = row["physical_attempt_index"]
        if physical is not None:
            physical = require_int(physical, path=f"{path}.physical_attempt_index", minimum=1)
        if retry_kind == "transport" and physical is None:
            raise ContractValidationError("retry_identity", f"{path}.physical_attempt_index", "transport retry requires a physical attempt index")
        return {
            "retry_kind": retry_kind,
            "logical_request_id": require_string(row["logical_request_id"], path=f"{path}.logical_request_id"),
            "physical_attempt_index": physical,
            "reason_code": require_string(row["reason_code"], path=f"{path}.reason_code"),
        }
    if event_type == "checkpoint":
        require_exact_keys(row, required={"checkpoint", "work_id"}, path=path)
        return {
            "checkpoint": validate_typed_artifact_binding_v1(row["checkpoint"], path=f"{path}.checkpoint"),
            "work_id": require_nullable_string(row["work_id"], path=f"{path}.work_id"),
        }
    if event_type == "usage_snapshot":
        require_exact_keys(row, required={"snapshot"}, path=path)
        return {
            "snapshot": validate_typed_artifact_binding_v1(
                row["snapshot"], path=f"{path}.snapshot"
            )
        }
    if event_type == "stage_done":
        require_exact_keys(row, required={"outcome"}, path=path)
        return {"outcome": require_enum(row["outcome"], {"succeeded", "failed", "blocked", "skipped"}, path=f"{path}.outcome")}
    if event_type == "component_halted":
        require_exact_keys(row, required={"reason_code", "resume_available"}, path=path)
        if row["resume_available"] is not True:
            raise ContractValidationError("resume_available", f"{path}.resume_available", "halt must explicitly allow resume")
        return {"reason_code": require_string(row["reason_code"], path=f"{path}.reason_code"), "resume_available": True}
    if event_type == "component_done":
        require_exact_keys(row, required={"outcome"}, path=path)
        return {"outcome": require_enum(row["outcome"], {"succeeded"}, path=f"{path}.outcome")}
    if event_type == "component_failed":
        require_exact_keys(row, required={"outcome", "reason_code"}, path=path)
        return {
            "outcome": require_enum(row["outcome"], {"failed"}, path=f"{path}.outcome"),
            "reason_code": require_string(row["reason_code"], path=f"{path}.reason_code"),
        }
    raise ContractValidationError("event_type", path, "unsupported event payload")


def _validate_evaluation_detail(value: Any, *, path: str) -> dict[str, Any]:
    row = require_mapping(value, path=path)
    require_exact_keys(row, required={"detail_kind", "data"}, path=path)
    kind = require_enum(
        row["detail_kind"],
        {
            "input_arms",
            "chapter_scorer_progress",
            "metric_result",
            "aggregation_result",
            "verdict",
            "scoring_receipt",
        },
        path=f"{path}.detail_kind",
    )
    data = require_mapping(row["data"], path=f"{path}.data")
    if kind == "input_arms":
        require_exact_keys(data, required={"arm_ids"}, path=f"{path}.data")
        arm_ids = [
            require_string(item, path=f"{path}.data.arm_ids[*]")
            for item in require_list(data["arm_ids"], path=f"{path}.data.arm_ids")
        ]
        positions = {arm_id: index for index, arm_id in enumerate(ARM_IDS_V1)}
        if (
            len(arm_ids) < 2
            or len(arm_ids) != len(set(arm_ids))
            or any(arm_id not in positions for arm_id in arm_ids)
            or tuple(sorted(arm_ids, key=positions.__getitem__)) != tuple(arm_ids)
        ):
            raise ContractValidationError(
                "arm_order",
                f"{path}.data.arm_ids",
                "expected at least two unique registered arms in canonical order",
            )
        normalized_data: dict[str, Any] = {"arm_ids": arm_ids}
    elif kind == "chapter_scorer_progress":
        require_exact_keys(
            data,
            required={"chapter_id", "scorer_id", "completed", "total"},
            path=f"{path}.data",
        )
        completed = require_int(
            data["completed"], path=f"{path}.data.completed", minimum=0
        )
        total = require_int(data["total"], path=f"{path}.data.total", minimum=0)
        if completed > total:
            raise ContractValidationError(
                "progress", f"{path}.data.completed", "completed cannot exceed total"
            )
        normalized_data = {
            "chapter_id": require_string(
                data["chapter_id"], path=f"{path}.data.chapter_id"
            ),
            "scorer_id": require_nullable_string(
                data["scorer_id"], path=f"{path}.data.scorer_id"
            ),
            "completed": completed,
            "total": total,
        }
    elif kind == "metric_result":
        require_exact_keys(
            data,
            required={"chapter_id", "scorer_id", "arm_id", "status", "value"},
            path=f"{path}.data",
        )
        raw_value = data["value"]
        if raw_value is not None:
            if isinstance(raw_value, bool) or not isinstance(raw_value, (int, float)):
                raise ContractValidationError(
                    "metric_value", f"{path}.data.value", "value must be numeric or null"
                )
            raw_value = float(raw_value)
            if raw_value != raw_value or raw_value in {float("inf"), float("-inf")}:
                raise ContractValidationError(
                    "metric_value", f"{path}.data.value", "value must be finite"
                )
        status = require_enum(
            data["status"],
            {"available", "missing", "failed"},
            path=f"{path}.data.status",
        )
        if (status == "available") != (raw_value is not None):
            raise ContractValidationError(
                "metric_value",
                f"{path}.data.value",
                "available requires a value; missing/failed require null",
            )
        normalized_data = {
            "chapter_id": require_nullable_string(
                data["chapter_id"], path=f"{path}.data.chapter_id"
            ),
            "scorer_id": require_string(
                data["scorer_id"], path=f"{path}.data.scorer_id"
            ),
            "arm_id": require_nullable_string(
                data["arm_id"], path=f"{path}.data.arm_id"
            ),
            "status": status,
            "value": raw_value,
        }
    elif kind == "aggregation_result":
        require_exact_keys(
            data, required={"report", "metric_ids"}, path=f"{path}.data"
        )
        normalized_data = {
            "report": validate_typed_artifact_binding_v1(
                data["report"], path=f"{path}.data.report"
            ),
            "metric_ids": [
                require_string(item, path=f"{path}.data.metric_ids[*]")
                for item in require_list(
                    data["metric_ids"], path=f"{path}.data.metric_ids"
                )
            ],
        }
        require_unique(
            normalized_data["metric_ids"], path=f"{path}.data.metric_ids"
        )
    elif kind == "verdict":
        require_exact_keys(
            data, required={"status", "verdict_id", "reason_code"}, path=f"{path}.data"
        )
        normalized_data = {
            "status": require_enum(
                data["status"],
                {"not_defined", "inconclusive", "available"},
                path=f"{path}.data.status",
            ),
            "verdict_id": require_string(
                data["verdict_id"], path=f"{path}.data.verdict_id"
            ),
            "reason_code": require_nullable_string(
                data["reason_code"], path=f"{path}.data.reason_code"
            ),
        }
    else:
        require_exact_keys(data, required={"receipt"}, path=f"{path}.data")
        normalized_data = {
            "receipt": validate_typed_artifact_binding_v1(
                data["receipt"], path=f"{path}.data.receipt"
            )
        }
    return {"detail_kind": kind, "data": normalized_data}


def _validate_artifact_row(value: Any, *, path: str) -> dict[str, Any]:
    row = require_mapping(value, path=path)
    require_exact_keys(row, required={"artifact", "stage_id", "created_by_event_id", "parent_artifact_refs"}, path=path)
    parents = [require_relative_path(item, path=f"{path}.parent_artifact_refs[{index}]") for index, item in enumerate(require_list(row["parent_artifact_refs"], path=f"{path}.parent_artifact_refs"))]
    require_unique(parents, path=f"{path}.parent_artifact_refs")
    artifact = validate_typed_artifact_binding_v1(row["artifact"], path=f"{path}.artifact")
    if artifact["artifact_ref"] in parents:
        raise ContractValidationError("artifact_parent", f"{path}.parent_artifact_refs", "artifact cannot parent itself")
    return {
        "artifact": artifact,
        "stage_id": require_string(row["stage_id"], path=f"{path}.stage_id"),
        "created_by_event_id": _require_event_id(row["created_by_event_id"], path=f"{path}.created_by_event_id"),
        "parent_artifact_refs": parents,
    }


def _validate_single_hash(value: Any, field: str, path: str) -> dict[str, str]:
    row = require_mapping(value, path=path)
    require_exact_keys(row, required={field}, path=path)
    return {field: require_sha256(row[field], path=f"{path}.{field}")}


def _require_nullable_sha(value: Any, path: str) -> str | None:
    return None if value is None else require_sha256(value, path=path)


def _attempt_id(index: int) -> str:
    return f"evalcomp_attempt_{index:04d}"


def _require_attempt_id(value: Any, *, path: str) -> str:
    result = require_string(value, path=path)
    if _ATTEMPT_ID_RE.fullmatch(result) is None:
        raise ContractValidationError("component_attempt_id", path, "expected evalcomp_attempt_NNNN")
    return result


def _require_attempt_pair(attempt_id: str, attempt_index: int, *, path: str) -> None:
    if attempt_id != _attempt_id(attempt_index):
        raise ContractValidationError("component_attempt_id", path, "attempt ID and index disagree")


def _require_event_id(value: Any, *, path: str) -> str:
    result = require_string(value, path=path)
    if _EVENT_ID_RE.fullmatch(result) is None:
        raise ContractValidationError("event_id", path, "invalid deterministic Evaluation event ID")
    return result
