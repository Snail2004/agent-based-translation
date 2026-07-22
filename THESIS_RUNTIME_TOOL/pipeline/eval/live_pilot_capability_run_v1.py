from __future__ import annotations

from copy import deepcopy
from datetime import datetime
import hashlib
import json
from pathlib import Path
import re
from typing import Any, Callable, Mapping, Sequence

from pipeline.eval.contracts_v1 import (
    ContractValidationError,
    require_enum,
    require_exact_keys,
    require_list,
    require_mapping,
    require_rfc3339,
    require_sha256,
    require_string,
    require_unique,
)
from pipeline.eval.live_pilot_capability_probe_v1 import (
    EvaluationCapabilityProbePlanV1,
    build_evaluation_capability_probe_plan_v1,
    execute_evaluation_capability_probe_once_v1,
)
from pipeline.eval.llm_profiles_v1 import (
    EVALUATION_LLM_ROLE_IDS,
    PJ_JUDGE_ROLE_ID,
    SF_BT_BACK_TRANSLATOR_ROLE_ID,
    SF_BT_SEMANTIC_JUDGE_ROLE_ID,
    evaluation_role_contract_v1,
)
from pipeline.llm_backend import (
    ContentAddressedArtifactStore,
    PhysicalQuotaScheduler,
    SharedLlmAttemptLedger,
    SharedLlmCapabilityProbe,
    canonical_json,
    canonical_sha256,
    validate_api_source,
    validate_capability_evidence,
    validate_capability_probe_bundle,
    validate_capability_probe_request_body,
)
from pipeline.llm_backend.credentials_v1 import CredentialProvider
from pipeline.llm_backend.transport_v1 import TransportSender


__all__ = [
    "EVALUATION_CAPABILITY_PROBE_RUN_SCHEMA_VERSION",
    "EVALUATION_CAPABILITY_PROBE_ROLE_ORDER",
    "capabilities_by_role_from_probe_run_v1",
    "run_evaluation_capability_probes_v1",
    "seal_evaluation_capability_probe_run_summary_v1",
    "validate_evaluation_capability_probe_run_summary_v1",
]


EVALUATION_CAPABILITY_PROBE_RUN_SCHEMA_VERSION = (
    "evaluation_live_pilot_capability_probe_run_v1"
)
EVALUATION_CAPABILITY_PROBE_ROLE_ORDER = (
    SF_BT_BACK_TRANSLATOR_ROLE_ID,
    SF_BT_SEMANTIC_JUDGE_ROLE_ID,
    PJ_JUDGE_ROLE_ID,
)
_ZERO_SHA256 = "0" * 64
_SAFE_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,190}$")
_GIT_OID_RE = re.compile(r"^[0-9a-f]{40}$")


def run_evaluation_capability_probes_v1(
    *,
    source: Mapping[str, Any],
    requested_models_by_role: Mapping[str, str],
    accepted_observed_models_by_role: Mapping[str, Sequence[str]],
    credential_provider: CredentialProvider,
    output_root: Path,
    probe_run_prefix: str,
    issued_at_utc: str,
    implementation_binding: Mapping[str, str],
    sender: TransportSender,
    clock: Callable[[], datetime] | None = None,
    plan_builder: Callable[..., EvaluationCapabilityProbePlanV1] = (
        build_evaluation_capability_probe_plan_v1
    ),
) -> dict[str, Any]:
    """Run at most one capability call per Evaluation role, fail closed in order."""

    normalized_source = validate_api_source(source)
    models = _validate_role_models(requested_models_by_role)
    accepted_models = _validate_accepted_models(
        accepted_observed_models_by_role, models
    )
    prefix = _require_safe_id(
        probe_run_prefix, path="$.probe_run_prefix", maximum=96
    )
    issued_at = require_rfc3339(issued_at_utc, path="$.issued_at_utc")
    binding = _validate_implementation_binding(implementation_binding)
    root = Path(output_root).resolve()
    _prepare_output_root(root)

    state_root = root / "_state"
    probe = SharedLlmCapabilityProbe(
        credential_provider=credential_provider,
        scheduler=PhysicalQuotaScheduler(state_root / "quota_leases"),
        ledger=SharedLlmAttemptLedger(state_root / "attempt_ledger.sqlite3"),
        artifact_store=ContentAddressedArtifactStore(state_root / "raw_responses"),
        sender=sender,
        implementation_binding=binding,
        clock=clock,
    )

    attempted: list[dict[str, Any]] = []
    halt: dict[str, str] | None = None
    for index, role_id in enumerate(EVALUATION_CAPABILITY_PROBE_ROLE_ORDER, start=1):
        role_slug = role_id.replace(".", "_")
        probe_run_id = f"{prefix}_{index:02d}_{role_slug}"
        plan = plan_builder(
            role_id=role_id,
            source=normalized_source,
            requested_model_id=models[role_id],
            accepted_observed_model_ids=accepted_models[role_id],
            probe_run_id=probe_run_id,
            issued_at_utc=issued_at,
            implementation_binding=binding,
        )
        role_root = root / "roles" / role_slug
        role_root.mkdir(parents=True, exist_ok=False)
        request_path = role_root / "request_body.json"
        seal_path = role_root / "probe_seal.json"
        result_path = role_root / "probe_result.json"
        _write_json(request_path, plan.request_body)
        _write_json(seal_path, plan.seal)

        result = execute_evaluation_capability_probe_once_v1(
            probe=probe,
            plan=plan,
        )
        _write_json(result_path, result)
        attempted.append(
            _attempted_role_row(
                root=root,
                plan=plan,
                result=result,
                request_path=request_path,
                seal_path=seal_path,
                result_path=result_path,
            )
        )
        if result["status"] != "qualified":
            failure = result["receipt"]["failure"]
            halt = {
                "role_id": role_id,
                "failure_code": failure["code"] if failure else "probe_failed",
            }
            break

    summary = seal_evaluation_capability_probe_run_summary_v1(
        {
            "schema_version": EVALUATION_CAPABILITY_PROBE_RUN_SCHEMA_VERSION,
            "probe_run_prefix": prefix,
            "issued_at_utc": issued_at,
            "source": normalized_source,
            "implementation_binding": binding,
            "requested_roles": [
                {
                    "role_id": role_id,
                    "requested_model_id": models[role_id],
                    "accepted_observed_model_ids": accepted_models[role_id],
                }
                for role_id in EVALUATION_CAPABILITY_PROBE_ROLE_ORDER
            ],
            "attempted_roles": attempted,
            "status": "qualified" if halt is None else "failed",
            "halt": halt,
            "integrity": {"summary_sha256": _ZERO_SHA256},
        }
    )
    _write_json(root / "run_summary.json", summary)
    return deepcopy(summary)


def seal_evaluation_capability_probe_run_summary_v1(
    payload: Mapping[str, Any],
) -> dict[str, Any]:
    row = deepcopy(dict(payload))
    row["integrity"] = {"summary_sha256": _ZERO_SHA256}
    row["integrity"]["summary_sha256"] = canonical_sha256(row)
    return validate_evaluation_capability_probe_run_summary_v1(row)


def validate_evaluation_capability_probe_run_summary_v1(
    payload: Mapping[str, Any],
) -> dict[str, Any]:
    root = require_mapping(payload, path="$")
    require_exact_keys(
        root,
        required={
            "schema_version",
            "probe_run_prefix",
            "issued_at_utc",
            "source",
            "implementation_binding",
            "requested_roles",
            "attempted_roles",
            "status",
            "halt",
            "integrity",
        },
        path="$",
    )
    source = validate_api_source(root["source"])
    binding = _validate_implementation_binding(root["implementation_binding"])
    requested = _validate_requested_role_rows(root["requested_roles"])
    attempted = _validate_attempted_role_rows(root["attempted_roles"], requested)
    status = require_enum(
        root["status"], {"qualified", "failed"}, path="$.status"
    )
    halt = _validate_halt(root["halt"])
    integrity = _validate_integrity(root["integrity"])
    normalized = {
        "schema_version": require_enum(
            root["schema_version"],
            {EVALUATION_CAPABILITY_PROBE_RUN_SCHEMA_VERSION},
            path="$.schema_version",
        ),
        "probe_run_prefix": _require_safe_id(
            root["probe_run_prefix"], path="$.probe_run_prefix", maximum=96
        ),
        "issued_at_utc": require_rfc3339(
            root["issued_at_utc"], path="$.issued_at_utc"
        ),
        "source": source,
        "implementation_binding": binding,
        "requested_roles": requested,
        "attempted_roles": attempted,
        "status": status,
        "halt": halt,
        "integrity": integrity,
    }
    _require_run_consistency(normalized)
    hash_payload = deepcopy(normalized)
    hash_payload["integrity"]["summary_sha256"] = _ZERO_SHA256
    if canonical_sha256(hash_payload) != integrity["summary_sha256"]:
        raise ContractValidationError(
            "summary_hash",
            "$.integrity.summary_sha256",
            "probe run summary hash differs from canonical content",
        )
    return deepcopy(normalized)


def capabilities_by_role_from_probe_run_v1(
    payload: Mapping[str, Any],
    *,
    output_root: Path,
) -> dict[str, dict[str, Any]]:
    summary = validate_evaluation_capability_probe_run_summary_v1(payload)
    if summary["status"] != "qualified":
        raise ContractValidationError(
            "probe_run_unqualified",
            "$.status",
            "failed or incomplete probe runs cannot authorize live scoring",
        )
    root = Path(output_root).resolve()
    capabilities: dict[str, dict[str, Any]] = {}
    for row in summary["attempted_roles"]:
        request_body = _read_bound_json(
            root, row["artifacts"]["request_body"]
        )
        seal = _read_bound_json(root, row["artifacts"]["probe_seal"])
        result = _read_bound_json(root, row["artifacts"]["probe_result"])
        if set(result) != {
            "status",
            "provider_called",
            "probe_seal_sha256",
            "receipt",
            "capability_evidence",
        }:
            raise ContractValidationError(
                "probe_result_shape",
                f"$.attempted_roles.{row['role_id']}",
                "stored probe result has missing or extra fields",
            )
        validate_capability_probe_request_body(
            probe_seal=seal, request_body=request_body
        )
        bundle = validate_capability_probe_bundle(
            seal=seal,
            receipt=result["receipt"],
            capability_evidence=result["capability_evidence"],
        )
        contract = evaluation_role_contract_v1(row["role_id"])
        intent = seal["capability_intent"]
        if (
            seal["role_id"] != row["role_id"]
            or seal["implementation_binding"] != summary["implementation_binding"]
            or seal["source_binding"]["record"] != summary["source"]
            or result["status"] != row["status"]
            or result["probe_seal_sha256"] != row["probe_seal_sha256"]
            or result["receipt"]["receipt_sha256"] != row["receipt_sha256"]
            or result["capability_evidence"] != row["capability_evidence"]
            or intent["schema_sha256"] != contract["response_schema"]["sha256"]
            or intent["local_validator_id"] != contract["validator"]["id"]
            or intent["local_validator_sha256"] != contract["validator"]["sha256"]
        ):
            raise ContractValidationError(
                "probe_bundle_binding",
                f"$.attempted_roles.{row['role_id']}",
                "stored probe bundle differs from the sealed Evaluation role",
            )
        if bundle["capability_evidence"]["verdict"] != "qualified":
            raise ContractValidationError(
                "probe_bundle_unqualified",
                f"$.attempted_roles.{row['role_id']}",
                "stored probe bundle is not qualified",
            )
        capabilities[row["role_id"]] = deepcopy(bundle["capability_evidence"])
    return capabilities


def _attempted_role_row(
    *,
    root: Path,
    plan: EvaluationCapabilityProbePlanV1,
    result: Mapping[str, Any],
    request_path: Path,
    seal_path: Path,
    result_path: Path,
) -> dict[str, Any]:
    return {
        "role_id": plan.role_id,
        "requested_model_id": plan.seal["capability_intent"][
            "requested_model_id"
        ],
        "probe_run_id": plan.seal["probe_run_id"],
        "status": result["status"],
        "probe_seal_sha256": plan.seal["seal_sha256"],
        "receipt_sha256": result["receipt"]["receipt_sha256"],
        "capability_evidence": deepcopy(result["capability_evidence"]),
        "artifacts": {
            "request_body": _artifact_ref(root, request_path),
            "probe_seal": _artifact_ref(root, seal_path),
            "probe_result": _artifact_ref(root, result_path),
        },
    }


def _validate_role_models(value: Mapping[str, str]) -> dict[str, str]:
    row = require_mapping(value, path="$.requested_models_by_role")
    if set(row) != EVALUATION_LLM_ROLE_IDS:
        raise ContractValidationError(
            "role_cover",
            "$.requested_models_by_role",
            "requested models must cover exactly the three Evaluation LLM roles",
        )
    return {
        role_id: require_string(
            row[role_id], path=f"$.requested_models_by_role.{role_id}", maximum=256
        )
        for role_id in EVALUATION_CAPABILITY_PROBE_ROLE_ORDER
    }


def _validate_accepted_models(
    value: Mapping[str, Sequence[str]], requested: Mapping[str, str]
) -> dict[str, list[str]]:
    row = require_mapping(value, path="$.accepted_observed_models_by_role")
    if set(row) != EVALUATION_LLM_ROLE_IDS:
        raise ContractValidationError(
            "role_cover",
            "$.accepted_observed_models_by_role",
            "accepted observed models must cover exactly the Evaluation roles",
        )
    result: dict[str, list[str]] = {}
    for role_id in EVALUATION_CAPABILITY_PROBE_ROLE_ORDER:
        values = row[role_id]
        if isinstance(values, (str, bytes)) or not isinstance(values, Sequence):
            raise ContractValidationError(
                "type",
                f"$.accepted_observed_models_by_role.{role_id}",
                "expected a sequence",
            )
        normalized = [
            require_string(
                item,
                path=f"$.accepted_observed_models_by_role.{role_id}[]",
                maximum=256,
            )
            for item in values
        ]
        if normalized != sorted(set(normalized)) or requested[role_id] not in normalized:
            raise ContractValidationError(
                "accepted_models",
                f"$.accepted_observed_models_by_role.{role_id}",
                "accepted models must be sorted, unique and include the request",
            )
        result[role_id] = normalized
    return result


def _validate_requested_role_rows(value: Any) -> list[dict[str, Any]]:
    rows = require_list(value, path="$.requested_roles")
    result: list[dict[str, Any]] = []
    for index, raw in enumerate(rows):
        path = f"$.requested_roles[{index}]"
        row = require_mapping(raw, path=path)
        require_exact_keys(
            row,
            required={
                "role_id",
                "requested_model_id",
                "accepted_observed_model_ids",
            },
            path=path,
        )
        role_id = require_enum(
            row["role_id"], EVALUATION_LLM_ROLE_IDS, path=f"{path}.role_id"
        )
        models = row["accepted_observed_model_ids"]
        if isinstance(models, (str, bytes)) or not isinstance(models, list):
            raise ContractValidationError(
                "type", f"{path}.accepted_observed_model_ids", "expected an array"
            )
        accepted = [
            require_string(item, path=f"{path}.accepted_observed_model_ids[]")
            for item in models
        ]
        requested = require_string(
            row["requested_model_id"], path=f"{path}.requested_model_id"
        )
        if accepted != sorted(set(accepted)) or requested not in accepted:
            raise ContractValidationError(
                "accepted_models",
                f"{path}.accepted_observed_model_ids",
                "accepted models must be sorted, unique and include the request",
            )
        result.append(
            {
                "role_id": role_id,
                "requested_model_id": requested,
                "accepted_observed_model_ids": accepted,
            }
        )
    if [row["role_id"] for row in result] != list(
        EVALUATION_CAPABILITY_PROBE_ROLE_ORDER
    ):
        raise ContractValidationError(
            "role_order",
            "$.requested_roles",
            "requested roles must use the closed Evaluation execution order",
        )
    return result


def _validate_attempted_role_rows(
    value: Any, requested: Sequence[Mapping[str, Any]]
) -> list[dict[str, Any]]:
    rows = require_list(value, path="$.attempted_roles")
    result: list[dict[str, Any]] = []
    for index, raw in enumerate(rows):
        path = f"$.attempted_roles[{index}]"
        row = require_mapping(raw, path=path)
        require_exact_keys(
            row,
            required={
                "role_id",
                "requested_model_id",
                "probe_run_id",
                "status",
                "probe_seal_sha256",
                "receipt_sha256",
                "capability_evidence",
                "artifacts",
            },
            path=path,
        )
        evidence = validate_capability_evidence(row["capability_evidence"])
        artifacts = _validate_artifacts(row["artifacts"], path=f"{path}.artifacts")
        normalized = {
            "role_id": require_enum(
                row["role_id"], EVALUATION_LLM_ROLE_IDS, path=f"{path}.role_id"
            ),
            "requested_model_id": require_string(
                row["requested_model_id"], path=f"{path}.requested_model_id"
            ),
            "probe_run_id": _require_safe_id(
                row["probe_run_id"], path=f"{path}.probe_run_id"
            ),
            "status": require_enum(
                row["status"], {"qualified", "failed"}, path=f"{path}.status"
            ),
            "probe_seal_sha256": require_sha256(
                row["probe_seal_sha256"], path=f"{path}.probe_seal_sha256"
            ),
            "receipt_sha256": require_sha256(
                row["receipt_sha256"], path=f"{path}.receipt_sha256"
            ),
            "capability_evidence": evidence,
            "artifacts": artifacts,
        }
        expected = requested[index] if index < len(requested) else None
        if expected is None or (
            normalized["role_id"] != expected["role_id"]
            or normalized["requested_model_id"] != expected["requested_model_id"]
        ):
            raise ContractValidationError(
                "attempt_order",
                path,
                "attempted roles must be a prefix of requested roles",
            )
        if (
            evidence["requested_model_id"] != normalized["requested_model_id"]
            or evidence["probe_id"] != normalized["probe_run_id"]
            or evidence["evidence_sha256"] != normalized["receipt_sha256"]
            or evidence["verdict"] != normalized["status"]
        ):
            raise ContractValidationError(
                "attempt_binding", path, "attempt evidence differs from its row"
            )
        result.append(normalized)
    return result


def _validate_artifacts(value: Any, *, path: str) -> dict[str, dict[str, str]]:
    row = require_mapping(value, path=path)
    require_exact_keys(
        row,
        required={"request_body", "probe_seal", "probe_result"},
        path=path,
    )
    return {
        name: _validate_artifact_ref(row[name], path=f"{path}.{name}")
        for name in ("request_body", "probe_seal", "probe_result")
    }


def _validate_artifact_ref(value: Any, *, path: str) -> dict[str, str]:
    row = require_mapping(value, path=path)
    require_exact_keys(row, required={"path", "sha256"}, path=path)
    relative = require_string(row["path"], path=f"{path}.path")
    candidate = Path(relative)
    if candidate.is_absolute() or ".." in candidate.parts or candidate.as_posix() != relative:
        raise ContractValidationError(
            "artifact_path", f"{path}.path", "artifact path must be normalized and relative"
        )
    return {
        "path": relative,
        "sha256": require_sha256(row["sha256"], path=f"{path}.sha256"),
    }


def _validate_halt(value: Any) -> dict[str, str] | None:
    if value is None:
        return None
    row = require_mapping(value, path="$.halt")
    require_exact_keys(row, required={"role_id", "failure_code"}, path="$.halt")
    return {
        "role_id": require_enum(
            row["role_id"], EVALUATION_LLM_ROLE_IDS, path="$.halt.role_id"
        ),
        "failure_code": require_string(
            row["failure_code"], path="$.halt.failure_code"
        ),
    }


def _validate_integrity(value: Any) -> dict[str, str]:
    row = require_mapping(value, path="$.integrity")
    require_exact_keys(row, required={"summary_sha256"}, path="$.integrity")
    return {
        "summary_sha256": require_sha256(
            row["summary_sha256"], path="$.integrity.summary_sha256"
        )
    }


def _validate_implementation_binding(value: Mapping[str, str]) -> dict[str, str]:
    row = require_mapping(value, path="$.implementation_binding")
    require_exact_keys(
        row,
        required={
            "shared_core_revision",
            "consumer_revision",
            "consumer_implementation_sha256",
        },
        path="$.implementation_binding",
    )
    result = {
        "shared_core_revision": require_string(
            row["shared_core_revision"],
            path="$.implementation_binding.shared_core_revision",
        ),
        "consumer_revision": require_string(
            row["consumer_revision"], path="$.implementation_binding.consumer_revision"
        ),
        "consumer_implementation_sha256": require_sha256(
            row["consumer_implementation_sha256"],
            path="$.implementation_binding.consumer_implementation_sha256",
        ),
    }
    if _GIT_OID_RE.fullmatch(result["shared_core_revision"]) is None:
        raise ContractValidationError(
            "shared_core_revision",
            "$.implementation_binding.shared_core_revision",
            "shared core revision must be a lowercase Git object ID",
        )
    if _GIT_OID_RE.fullmatch(result["consumer_revision"]) is None:
        raise ContractValidationError(
            "consumer_revision",
            "$.implementation_binding.consumer_revision",
            "consumer revision must be a lowercase Git object ID",
        )
    return result


def _require_run_consistency(summary: Mapping[str, Any]) -> None:
    attempted = summary["attempted_roles"]
    failed = [row for row in attempted if row["status"] == "failed"]
    if failed and failed != attempted[-1:]:
        raise ContractValidationError(
            "fail_closed_order",
            "$.attempted_roles",
            "only the final attempted role may fail",
        )
    if summary["status"] == "qualified":
        if len(attempted) != len(EVALUATION_CAPABILITY_PROBE_ROLE_ORDER):
            raise ContractValidationError(
                "incomplete_probe", "$.attempted_roles", "qualified run is incomplete"
            )
        if failed or summary["halt"] is not None:
            raise ContractValidationError(
                "qualified_halt", "$.halt", "qualified run cannot contain a halt"
            )
    else:
        if not failed or summary["halt"] is None:
            raise ContractValidationError(
                "missing_halt", "$.halt", "failed run requires failed evidence and halt"
            )
        if summary["halt"]["role_id"] != failed[0]["role_id"]:
            raise ContractValidationError(
                "halt_binding", "$.halt.role_id", "halt differs from failed role"
            )
    source = summary["source"]
    for row in attempted:
        evidence = row["capability_evidence"]
        for field in (
            "source_id",
            "source_revision",
            "adapter_id",
            "protocol",
            "route_id",
            "base_url",
        ):
            if evidence[field] != source[field]:
                raise ContractValidationError(
                    "source_binding",
                    f"$.attempted_roles.{row['role_id']}",
                    "capability evidence belongs to another source",
                )


def _prepare_output_root(root: Path) -> None:
    if root.exists() and any(root.iterdir()):
        raise ContractValidationError(
            "output_root_not_empty",
            "$.output_root",
            "capability probe output root must be absent or empty",
        )
    root.mkdir(parents=True, exist_ok=True)


def _artifact_ref(root: Path, path: Path) -> dict[str, str]:
    relative = path.relative_to(root).as_posix()
    return {"path": relative, "sha256": _sha256_file(path)}


def _write_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = (canonical_json(payload) + "\n").encode("utf-8")
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_bytes(encoded)
    temporary.replace(path)


def _read_bound_json(root: Path, reference: Mapping[str, str]) -> dict[str, Any]:
    path = (root / reference["path"]).resolve()
    try:
        path.relative_to(root)
    except ValueError as exc:
        raise ContractValidationError(
            "artifact_containment",
            "$.attempted_roles.artifacts",
            "probe artifact resolves outside its run root",
        ) from exc
    if not path.is_file() or _sha256_file(path) != reference["sha256"]:
        raise ContractValidationError(
            "artifact_hash",
            "$.attempted_roles.artifacts",
            "probe artifact is missing or differs from its recorded hash",
        )
    try:
        parsed = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ContractValidationError(
            "artifact_json",
            "$.attempted_roles.artifacts",
            "probe artifact is not one UTF-8 JSON object",
        ) from exc
    if not isinstance(parsed, dict):
        raise ContractValidationError(
            "artifact_json",
            "$.attempted_roles.artifacts",
            "probe artifact must contain one JSON object",
        )
    return parsed


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _require_safe_id(value: Any, *, path: str, maximum: int = 191) -> str:
    result = require_string(value, path=path, maximum=maximum)
    if _SAFE_ID_RE.fullmatch(result) is None:
        raise ContractValidationError(
            "safe_id",
            path,
            "identifier must be normalized lowercase ASCII using only letters, digits, dot, dash or underscore",
        )
    return result
