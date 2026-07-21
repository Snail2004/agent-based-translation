from __future__ import annotations

import copy
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from pipeline.ingest.canonical_source_package import canonical_json_sha256
from pipeline.ingest.draft_structure import DraftStructureError
from pipeline.ingest.draft_structure_llm import (
    BOUNDARY_REPAIR_SCHEMA_DIALECT_V1,
    BOUNDARY_REPAIR_SCHEMA_DIALECT_V2,
    StructureContextBudget,
    boundary_repair_contract_identities,
    build_structure_context_packs,
    parse_structure_response_json,
    render_structure_prompt,
    run_structure_assistant,
)
from pipeline.llm_backend import (
    SharedLlmBackend,
    canonical_sha256,
    resolve_llm_run_seal,
    validate_api_source,
    validate_capability_evidence,
    validate_pipeline_profile,
)


PRESET_SCHEMA_VERSION = "input_normalization_llm_preset_v1"
RESULT_MANIFEST_VERSION = "draft_structure_shared_backend_result_v2"
ROLE_ID = "input_normalization.structure_draft.boundary_repair"
DISABLED_UNBOUND_ROLE_IDS = (
    "input_normalization.structure_draft.global_boundary_plan",
    "input_normalization.structure_draft.hierarchy_plan",
)
DEFAULT_PRESET_PATH = (
    Path(__file__).with_name("profiles")
    / "draft_structure_boundary_repair_recommended_v3.json"
)
SUPPORTED_PRESET_REVISIONS = frozenset(
    {"recommended_v1", "recommended_v2", "recommended_v3", "recommended_v4"}
)
_SCHEMA_DIALECT_BY_PRESET_REVISION = {
    "recommended_v1": BOUNDARY_REPAIR_SCHEMA_DIALECT_V1,
    "recommended_v2": BOUNDARY_REPAIR_SCHEMA_DIALECT_V1,
    "recommended_v3": BOUNDARY_REPAIR_SCHEMA_DIALECT_V2,
    "recommended_v4": BOUNDARY_REPAIR_SCHEMA_DIALECT_V2,
}

_PRESET_FIELDS = {
    "schema_version",
    "profile_id",
    "role_id",
    "preset_id",
    "preset_revision",
    "requested_model_id",
    "generation",
    "transport_retry",
    "semantic_retry",
    "limits",
    "structured_output",
    "namespaces",
    "preflight",
    "disabled_unbound_role_ids",
}


class DraftStructureGatewayError(DraftStructureError):
    """Fail-closed Input adapter or semantic-response contract violation."""


@dataclass(frozen=True)
class BoundaryRepairRunIdentity:
    run_id: str
    attempt_run_id: str
    stage_id: str
    logical_request_id: str
    implementation_commit: str

    def validate(self) -> None:
        for name in ("run_id", "attempt_run_id", "stage_id", "logical_request_id"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise DraftStructureGatewayError(f"{name} must be a non-empty string")
        value = self.implementation_commit
        if (
            not isinstance(value, str)
            or len(value) != 40
            or any(char not in "0123456789abcdef" for char in value)
        ):
            raise DraftStructureGatewayError(
                "implementation_commit must be a lowercase 40-character Git hash"
            )


def _write_json(path: Path, value: Any) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _sealed(value: dict[str, Any]) -> dict[str, Any]:
    result = copy.deepcopy(value)
    result["integrity"] = {"payload_sha256": canonical_json_sha256(result)}
    return result


def _strict_json_object(raw: bytes, *, owner: str) -> dict[str, Any]:
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise DraftStructureGatewayError(f"{owner} is not UTF-8") from exc

    def reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise DraftStructureGatewayError(
                    f"{owner} repeats JSON key: {key}"
                )
            result[key] = value
        return result

    try:
        parsed = json.loads(text, object_pairs_hook=reject_duplicate_keys)
    except DraftStructureGatewayError:
        raise
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise DraftStructureGatewayError(f"{owner} is not valid JSON") from exc
    if not isinstance(parsed, dict):
        raise DraftStructureGatewayError(f"{owner} must be a JSON object")
    return parsed


def load_boundary_repair_preset(
    path: str | Path | None = None,
) -> dict[str, Any]:
    preset_path = Path(path) if path is not None else DEFAULT_PRESET_PATH
    try:
        raw = json.loads(preset_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise DraftStructureGatewayError(
            "boundary-repair preset is unavailable or malformed"
        ) from exc
    if not isinstance(raw, dict) or set(raw) != _PRESET_FIELDS:
        raise DraftStructureGatewayError(
            "boundary-repair preset has missing or extra fields"
        )
    return _validate_boundary_repair_preset(raw)


def _validate_boundary_repair_preset(
    value: Mapping[str, Any],
) -> dict[str, Any]:
    if set(value) != _PRESET_FIELDS:
        raise DraftStructureGatewayError(
            "boundary-repair preset has missing or extra fields"
        )
    preset = copy.deepcopy(dict(value))
    if preset["schema_version"] != PRESET_SCHEMA_VERSION:
        raise DraftStructureGatewayError("boundary-repair preset schema differs")
    if preset["role_id"] != ROLE_ID:
        raise DraftStructureGatewayError("boundary-repair preset role differs")
    preset_revision = preset["preset_revision"]
    if preset_revision not in SUPPORTED_PRESET_REVISIONS:
        raise DraftStructureGatewayError("boundary-repair preset revision differs")
    if preset["preset_id"] != f"{ROLE_ID}.{preset_revision}":
        raise DraftStructureGatewayError("boundary-repair preset ID differs")
    if tuple(preset["disabled_unbound_role_ids"]) != DISABLED_UNBOUND_ROLE_IDS:
        raise DraftStructureGatewayError(
            "global and hierarchy roles must remain disabled and unbound"
        )
    if preset["transport_retry"] != {
        "max_retries": 0,
        "backoff_policy": "none",
        "initial_delay_ms": 0,
        "max_delay_ms": 0,
        "retryable_codes": [],
    }:
        raise DraftStructureGatewayError("transport retry must remain disabled")
    if preset["semantic_retry"] != {
        "max_retries": 0,
        "retryable_categories": [],
    }:
        raise DraftStructureGatewayError("semantic retry must remain disabled")
    if preset["limits"].get("max_calls") != 1:
        raise DraftStructureGatewayError("boundary repair must remain one-call")
    expected_schema_dialect = _SCHEMA_DIALECT_BY_PRESET_REVISION[preset_revision]
    if preset["structured_output"] != {
        "mode": "prompt_validated",
        "schema_dialect": expected_schema_dialect,
    }:
        raise DraftStructureGatewayError(
            "boundary repair must remain JSON-object plus local validation"
        )
    if not isinstance(preset["requested_model_id"], str) or not preset[
        "requested_model_id"
    ].strip():
        raise DraftStructureGatewayError("requested model is missing from preset")
    max_prompt_bytes = preset["preflight"].get("max_prompt_utf8_bytes")
    if (
        isinstance(max_prompt_bytes, bool)
        or not isinstance(max_prompt_bytes, int)
        or max_prompt_bytes <= 0
    ):
        raise DraftStructureGatewayError(
            "preset preflight max_prompt_utf8_bytes is invalid"
        )
    return preset


def build_boundary_repair_profile(
    *,
    api_source: Mapping[str, Any],
    capability_evidence: Mapping[str, Any],
    preset: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    active_preset = (
        _validate_boundary_repair_preset(preset)
        if preset is not None
        else load_boundary_repair_preset()
    )
    source = validate_api_source(api_source)
    capability = validate_capability_evidence(capability_evidence)
    schema_dialect = active_preset["structured_output"]["schema_dialect"]
    identities = boundary_repair_contract_identities(schema_dialect)
    requested_model = active_preset["requested_model_id"]
    expected_capability = {
        "source_id": source["source_id"],
        "source_revision": source["source_revision"],
        "adapter_id": source["adapter_id"],
        "protocol": source["protocol"],
        "route_id": source["route_id"],
        "base_url": source["base_url"],
        "requested_model_id": requested_model,
        "capability_kind": "json_object",
        "schema_dialect": active_preset["structured_output"]["schema_dialect"],
        "schema_sha256": identities["response_schema"]["sha256"],
        "local_validator_id": identities["validator"]["id"],
        "local_validator_sha256": identities["validator"]["sha256"],
        "verdict": "qualified",
    }
    for field, expected in expected_capability.items():
        if capability.get(field) != expected:
            raise DraftStructureGatewayError(
                f"capability evidence differs at {field}"
            )
    target = {
        "source_id": source["source_id"],
        "source_revision": source["source_revision"],
        "source_record_sha256": canonical_sha256(source),
        "requested_model_id": requested_model,
        "capability_id": capability["capability_id"],
        "capability_revision": capability["capability_revision"],
        "capability_record_sha256": canonical_sha256(capability),
    }
    role = {
        "workstream": "input_normalization",
        "role_id": active_preset["role_id"],
        "preset_id": active_preset["preset_id"],
        "preset_revision": active_preset["preset_revision"],
        "primary": target,
        "fallback_plan": {"enabled": False, "steps": []},
        "generation": copy.deepcopy(active_preset["generation"]),
        "transport_retry": copy.deepcopy(active_preset["transport_retry"]),
        "semantic_retry": copy.deepcopy(active_preset["semantic_retry"]),
        "limits": copy.deepcopy(active_preset["limits"]),
        "structured_output": copy.deepcopy(active_preset["structured_output"]),
        "namespaces": copy.deepcopy(active_preset["namespaces"]),
        "prompt": identities["prompt"],
        "response_schema": identities["response_schema"],
        "validator": identities["validator"],
        "semantic_extension": identities["semantic_extension"],
    }
    revision_material = {
        "profile_id": active_preset["profile_id"],
        "workstream": "input_normalization",
        "role_binding": role,
    }
    profile = {
        "schema_version": "pipeline_profile_v1",
        "profile_id": active_preset["profile_id"],
        "profile_revision": (
            f"resolved_{canonical_sha256(revision_material)[:24]}"
        ),
        "workstream": "input_normalization",
        "role_bindings": [role],
    }
    return validate_pipeline_profile(profile)


def _request_body(prompt: str, role: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "model": role["primary"]["requested_model_id"],
        "messages": [{"role": "user", "content": prompt}],
        "response_format": {"type": "json_object"},
        "max_completion_tokens": role["generation"]["max_output_tokens"],
    }


def build_boundary_repair_run_seal(
    report: Mapping[str, Any],
    context_pack: Mapping[str, Any],
    *,
    api_source: Mapping[str, Any],
    capability_evidence: Mapping[str, Any],
    run_identity: BoundaryRepairRunIdentity,
    preset: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    run_identity.validate()
    active_preset = (
        _validate_boundary_repair_preset(preset)
        if preset is not None
        else load_boundary_repair_preset()
    )
    profile = build_boundary_repair_profile(
        api_source=api_source,
        capability_evidence=capability_evidence,
        preset=active_preset,
    )
    role = profile["role_bindings"][0]
    schema_dialect = active_preset["structured_output"]["schema_dialect"]
    prompt = render_structure_prompt(
        dict(context_pack),
        response_contract_dialect=schema_dialect,
    )
    prompt_utf8_bytes = len(prompt.encode("utf-8"))
    if prompt_utf8_bytes > active_preset["preflight"]["max_prompt_utf8_bytes"]:
        raise DraftStructureGatewayError(
            "prompt UTF-8 byte preflight exceeds the pipeline-owned cap"
        )
    report_integrity = report.get("integrity")
    pack_integrity = context_pack.get("integrity")
    if not isinstance(report_integrity, Mapping) or not isinstance(
        pack_integrity, Mapping
    ):
        raise DraftStructureGatewayError("report or context pack integrity is missing")
    if context_pack.get("report_sha256") != report_integrity.get("payload_sha256"):
        raise DraftStructureGatewayError("context pack is not bound to report")
    request_body = _request_body(prompt, role)
    input_bindings = [
        {
            "name": "transport_request_body",
            "sha256": canonical_sha256(request_body),
        },
        {
            "name": "document",
            "sha256": str(context_pack.get("document_sha256")),
        },
        {
            "name": "draft_structure_report",
            "sha256": str(report_integrity.get("payload_sha256")),
        },
        {
            "name": "context_pack",
            "sha256": str(pack_integrity.get("payload_sha256")),
        },
        {"name": "rendered_prompt", "sha256": canonical_sha256(prompt)},
        {
            "name": "allowed_scope",
            "sha256": canonical_sha256(context_pack.get("allowed_scope")),
        },
        {
            "name": "implementation_commit",
            "sha256": canonical_sha256(run_identity.implementation_commit),
        },
        {
            "name": "pipeline_preset",
            "sha256": canonical_sha256(active_preset),
        },
    ]
    seal = resolve_llm_run_seal(
        profile=profile,
        api_sources=[api_source],
        capability_evidence=[capability_evidence],
        role_id=ROLE_ID,
        run_id=run_identity.run_id,
        attempt_run_id=run_identity.attempt_run_id,
        stage_id=run_identity.stage_id,
        input_bindings=input_bindings,
    )
    return {
        "profile": profile,
        "seal": seal,
        "prompt": prompt,
        "request_body": request_body,
        "prompt_utf8_bytes": prompt_utf8_bytes,
        "preset_sha256": canonical_sha256(active_preset),
    }


def _extract_message_content(
    response: Mapping[str, Any], *, requested_model: str
) -> str:
    observed_model = response.get("model")
    if (
        not isinstance(observed_model, str)
        or (
            observed_model != requested_model
            and not observed_model.startswith(f"{requested_model}-")
        )
    ):
        raise DraftStructureGatewayError("provider returned an unexpected model")
    choices = response.get("choices")
    if not isinstance(choices, list) or len(choices) != 1:
        raise DraftStructureGatewayError("provider must return exactly one choice")
    choice = choices[0]
    if not isinstance(choice, Mapping):
        raise DraftStructureGatewayError("provider choice must be an object")
    message = choice.get("message")
    if not isinstance(message, Mapping):
        raise DraftStructureGatewayError("provider choice message is missing")
    content = message.get("content")
    if not isinstance(content, str) or not content.strip():
        raise DraftStructureGatewayError("provider message content is empty")
    return content


class SharedBackendStructureExecutor:
    def __init__(
        self,
        *,
        backend: SharedLlmBackend,
        invocation: Mapping[str, Any],
        logical_request_id: str,
    ) -> None:
        self.backend = backend
        self.invocation = copy.deepcopy(dict(invocation))
        self.logical_request_id = logical_request_id
        self.call_count = 0
        self.backend_result: dict[str, Any] | None = None
        self.response_payload: dict[str, Any] | None = None
        self.raw_response: str | None = None

    def complete(
        self,
        prompt: str,
        *,
        context_pack: dict[str, Any],
    ) -> dict[str, Any]:
        if self.call_count:
            raise DraftStructureGatewayError("boundary-repair call cap is exhausted")
        expected_prompt = self.invocation["prompt"]
        if canonical_sha256(prompt) != canonical_sha256(expected_prompt):
            raise DraftStructureGatewayError("runtime prompt differs from sealed input")
        expected_pack_hash = next(
            row["sha256"]
            for row in self.invocation["seal"]["input_bindings"]
            if row["name"] == "context_pack"
        )
        if (
            context_pack.get("integrity", {}).get("payload_sha256")
            != expected_pack_hash
        ):
            raise DraftStructureGatewayError(
                "runtime context pack differs from sealed input"
            )
        self.call_count += 1
        result = self.backend.execute_one_attempt(
            seal=self.invocation["seal"],
            logical_request_id=self.logical_request_id,
            semantic_attempt_index=1,
            transport_retry_ordinal=0,
            request_body=self.invocation["request_body"],
            target_index=0,
            allow_response_cache_read=False,
            allow_response_cache_write=False,
            cost_fact=None,
        )
        self.backend_result = copy.deepcopy(result)
        payload = _strict_json_object(
            result["response_bytes"], owner="provider response"
        )
        self.response_payload = copy.deepcopy(payload)
        content = _extract_message_content(
            payload,
            requested_model=self.invocation["profile"]["role_bindings"][0][
                "primary"
            ]["requested_model_id"],
        )
        self.raw_response = content
        return parse_structure_response_json(content)


def run_shared_backend_boundary_repair(
    report: dict[str, Any],
    document: dict[str, Any],
    output_parent: str | Path,
    *,
    backend: SharedLlmBackend,
    api_source: Mapping[str, Any],
    capability_evidence: Mapping[str, Any],
    run_identity: BoundaryRepairRunIdentity,
    context_budget: StructureContextBudget | None = None,
    preset_path: str | Path | None = None,
    include_all_units: bool = False,
    focus_unit_ids: list[str] | None = None,
) -> dict[str, Any]:
    active_budget = context_budget or StructureContextBudget()
    preset = load_boundary_repair_preset(preset_path)
    schema_dialect = preset["structured_output"]["schema_dialect"]
    packs = build_structure_context_packs(
        report,
        document,
        budget=active_budget,
        include_all_units=include_all_units,
        focus_unit_ids=focus_unit_ids,
        response_contract_dialect=schema_dialect,
    )
    if len(packs) != 1:
        raise DraftStructureGatewayError(
            "boundary-repair shared adapter requires exactly one context pack"
        )
    invocation = build_boundary_repair_run_seal(
        report,
        packs[0],
        api_source=api_source,
        capability_evidence=capability_evidence,
        run_identity=run_identity,
        preset=preset,
    )
    output_path = Path(output_parent) / invocation["seal"]["output_root_id"]
    if output_path.exists() and any(output_path.iterdir()):
        raise DraftStructureGatewayError(
            "resolved semantic output root must be new or empty"
        )
    output_path.mkdir(parents=True, exist_ok=True)
    _write_json(output_path / "resolved_run_seal.json", invocation["seal"])
    _write_json(output_path / "request.json", invocation["request_body"])
    executor = SharedBackendStructureExecutor(
        backend=backend,
        invocation=invocation,
        logical_request_id=run_identity.logical_request_id,
    )
    try:
        result = run_structure_assistant(
            executor,
            report,
            document,
            model_identifier=invocation["profile"]["role_bindings"][0]["primary"][
                "requested_model_id"
            ],
            budget=active_budget,
            include_all_units=include_all_units,
            focus_unit_ids=focus_unit_ids,
            response_contract_dialect=schema_dialect,
        )
    except Exception as exc:
        if executor.response_payload is not None:
            _write_json(
                output_path / "response_received.json",
                executor.response_payload,
            )
        if executor.raw_response is not None:
            (output_path / "response_raw.json").write_text(
                executor.raw_response.rstrip() + "\n",
                encoding="utf-8",
            )
        failure = _sealed(
            {
                "schema_version": RESULT_MANIFEST_VERSION,
                "status": "failed_closed",
                "seal_sha256": invocation["seal"]["seal_sha256"],
                "error_type": type(exc).__name__,
                "error_message": str(exc),
                "logical_call_count": executor.call_count,
                "provider_called": (
                    None
                    if executor.backend_result is None
                    else executor.backend_result["provider_called"]
                ),
                "canonical_effect": "none",
                "application_response_cache": {
                    "read": False,
                    "write": False,
                },
                "mandatory_stop": True,
            }
        )
        _write_json(output_path / "failure.json", failure)
        raise
    if executor.backend_result is None or executor.raw_response is None:
        raise DraftStructureGatewayError(
            "shared backend completed without semantic response evidence"
        )
    (output_path / "response_raw.json").write_text(
        executor.raw_response.rstrip() + "\n",
        encoding="utf-8",
    )
    _write_json(output_path / "correction_plan.json", result["correction_plan"])
    backend_result = executor.backend_result
    usage = backend_result.get("usage")
    manifest = _sealed(
        {
            "schema_version": RESULT_MANIFEST_VERSION,
            "status": "proposal_ready_for_human_review",
            "seal_sha256": invocation["seal"]["seal_sha256"],
            "backend_status": backend_result["status"],
            "provider_called": backend_result["provider_called"],
            "response_artifact_sha256": backend_result["artifact_sha256"],
            "attempt_usage_id": (
                None if usage is None else usage["attempt_usage_id"]
            ),
            "correction_plan_sha256": result["correction_plan"]["integrity"][
                "payload_sha256"
            ],
            "logical_call_count": executor.call_count,
            "canonical_effect": "none",
            "application_response_cache": {
                "read": False,
                "write": False,
            },
            "human_review_required": True,
            "mandatory_stop": True,
        }
    )
    _write_json(output_path / "result_manifest.json", manifest)
    return {
        "profile": invocation["profile"],
        "seal": invocation["seal"],
        "result": result,
        "backend_result": backend_result,
        "manifest": manifest,
        "output_root": str(output_path),
    }


__all__ = [
    "BoundaryRepairRunIdentity",
    "DEFAULT_PRESET_PATH",
    "DISABLED_UNBOUND_ROLE_IDS",
    "DraftStructureGatewayError",
    "PRESET_SCHEMA_VERSION",
    "RESULT_MANIFEST_VERSION",
    "ROLE_ID",
    "SharedBackendStructureExecutor",
    "build_boundary_repair_profile",
    "build_boundary_repair_run_seal",
    "load_boundary_repair_preset",
    "run_shared_backend_boundary_repair",
]
