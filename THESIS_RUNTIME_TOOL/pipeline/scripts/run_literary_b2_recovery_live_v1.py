"""Run the bounded post-B2 recovery canary with no retry or provider fallback."""

from __future__ import annotations

import argparse
from dataclasses import asdict
from datetime import datetime, timezone
import json
from pathlib import Path
import subprocess
from typing import TYPE_CHECKING, Any, Mapping

if TYPE_CHECKING:
    from pipeline.literary.chapter_cycle_shared_runtime_v1 import (
        LiterarySharedRunnerBindingsV1,
    )

from pipeline.agents.llm_client import estimate_prompt_tokens
from pipeline.agents.provider_profile import (
    ProviderProfile,
    ResolvedCredential,
    load_provider_profile,
    resolve_role_credential,
)
from pipeline.literary.b2_recovery_v1 import (
    RenderedB2RecoveryRequestV1,
    build_b2_recovery_index_v1,
    build_effective_b2_projection_v1,
    build_effective_b2_projection_v2,
    build_event_revision_ledger_v1,
    build_event_revision_ledger_v2,
    build_registry_recovery_ledger_v1,
    render_event_review_request_v1,
    render_event_review_request_v2,
    render_registry_recovery_request_v1,
    validate_event_review_response_v1,
    validate_event_review_response_v2,
    validate_registry_recovery_response_v1,
)
from pipeline.literary.checkpoint import canonical_hash, canonical_json, file_sha256
from pipeline.literary.openai_compatible_structured_call_v1 import (
    OpenAICompatibleStructuredResult,
    call_openai_compatible_structured_v1,
)
from pipeline.literary.structured_output_policy_v1 import (
    LiteraryStructuredOutputPolicy,
    StructuredOutputContract,
    load_literary_structured_output_policy,
    openai_response_format,
    resolve_structured_output_contract,
    validate_structured_payload,
)
from pipeline.scripts.run_chapter_registry_v4_real import FROZEN_DB_SHA256


RUN_SEAL_SCHEMA_VERSION = "literary_b2_recovery_live_seal_v1"
LIVE_REPORT_SCHEMA_VERSION = "literary_b2_recovery_live_report_v1"
RUN_SEAL_SCHEMA_VERSION_V2 = "literary_b2_recovery_live_seal_v2"
LIVE_REPORT_SCHEMA_VERSION_V2 = "literary_b2_recovery_live_report_v2"
RUN_SEAL_SCHEMA_VERSION_V3 = "literary_b2_recovery_live_seal_v3"
LIVE_REPORT_SCHEMA_VERSION_V3 = "literary_b2_recovery_live_report_v3"
RUN_SEAL_SCHEMA_VERSION_V4 = "literary_b2_recovery_live_seal_v4"
LIVE_REPORT_SCHEMA_VERSION_V4 = "literary_b2_recovery_live_report_v4"


class B2RecoveryLiveError(RuntimeError):
    """Raised when the sealed recovery canary cannot continue safely."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _read_object(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise B2RecoveryLiveError(f"cannot read {label}: {path}") from exc
    if not isinstance(value, dict):
        raise B2RecoveryLiveError(f"{label} must be a JSON object")
    return value


def _write_new_json(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        raise B2RecoveryLiveError(f"refusing to overwrite artifact: {path}")
    path.write_text(canonical_json(value) + "\n", encoding="utf-8")


def _tree_hash(root: Path) -> str:
    if not root.is_dir():
        raise B2RecoveryLiveError(f"source root does not exist: {root}")
    rows = [
        {
            "path": path.relative_to(root).as_posix(),
            "sha256": file_sha256(path),
        }
        for path in sorted(row for row in root.rglob("*") if row.is_file())
    ]
    if not rows:
        raise B2RecoveryLiveError("source root contains no files")
    return canonical_hash(rows)


def _git_head(root: Path) -> str:
    completed = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


def _request_payload(request: RenderedB2RecoveryRequestV1) -> dict[str, Any]:
    payload = asdict(request)
    payload["messages"] = list(payload["messages"])
    return payload


def _load_profile(path: Path) -> dict[str, Any]:
    raw = _read_object(path, "B2 recovery live profile")
    expected = {
        "schema_version",
        "profile_id",
        "provider_profile",
        "structured_output_policy",
        "stage_bindings",
        "generation",
        "limits",
        "safety",
    }
    if set(raw) != expected:
        raise B2RecoveryLiveError("B2 recovery profile keys drifted")
    profile_schema = raw["schema_version"]
    if profile_schema not in {
        "literary_b2_recovery_live_profile_v1",
        "literary_b2_recovery_live_profile_v2",
        "literary_b2_recovery_live_profile_v3",
    }:
        raise B2RecoveryLiveError("foreign B2 recovery profile schema")
    limits = raw["limits"]
    if profile_schema == "literary_b2_recovery_live_profile_v3":
        registry_calls = limits.get("registry_recovery_calls")
        event_calls = limits.get("event_review_calls")
        max_calls = limits.get("max_total_calls")
        if (
            not isinstance(registry_calls, int)
            or isinstance(registry_calls, bool)
            or not 0 <= registry_calls <= 4
            or not isinstance(event_calls, int)
            or isinstance(event_calls, bool)
            or not 1 <= event_calls <= 6
            or max_calls != registry_calls + event_calls
            or limits.get("max_retries_per_call") != 0
        ):
            raise B2RecoveryLiveError(
                "multi-component recovery call caps are invalid"
            )
    elif (
        limits.get("registry_recovery_calls") != 1
        or limits.get("event_review_calls") != 1
        or limits.get("max_total_calls") != 2
        or limits.get("max_retries_per_call") != 0
    ):
        raise B2RecoveryLiveError("recovery canary must remain two calls with no retry")
    safety = raw["safety"]
    if (
        safety.get("provider_fallback_allowed") is not False
        or safety.get("source_artifact_mutation_allowed") is not False
        or safety.get("book_global_identity_mutation_allowed") is not False
        or safety.get("production_publish_enabled") is not False
    ):
        raise B2RecoveryLiveError("recovery canary safety policy was weakened")
    contract_version = str(
        safety.get("event_review_contract_version") or "v1"
    )
    expected_contract = (
        "v2"
        if profile_schema
        in {
            "literary_b2_recovery_live_profile_v2",
            "literary_b2_recovery_live_profile_v3",
        }
        else "v1"
    )
    if contract_version != expected_contract:
        raise B2RecoveryLiveError(
            "recovery profile schema and event contract version disagree"
        )
    return raw


def _event_review_contract_functions(
    profile: Mapping[str, Any],
) -> tuple[str, Any, Any, Any]:
    version = str(
        (profile.get("safety") or {}).get("event_review_contract_version") or "v1"
    )
    if version == "v1":
        return (
            version,
            render_event_review_request_v1,
            validate_event_review_response_v1,
            build_event_revision_ledger_v1,
        )
    if version == "v2":
        return (
            version,
            render_event_review_request_v2,
            validate_event_review_response_v2,
            build_event_revision_ledger_v2,
        )
    raise B2RecoveryLiveError("foreign event review contract version")


def _role_and_credential(
    *,
    provider: ProviderProfile,
    role_id: str,
    credential_root: Path,
) -> tuple[Any, ResolvedCredential]:
    role = provider.roles.get(role_id)
    if role is None:
        raise B2RecoveryLiveError(f"provider profile lacks role {role_id}")
    credential = resolve_role_credential(
        provider,
        role_id=role_id,
        credential_root=credential_root,
    )
    if role.bucket_order != (credential.quota_bucket_id,):
        raise B2RecoveryLiveError(f"role {role_id} has a fallback bucket")
    if role.provider != "openai" or credential.provider != "openai":
        raise B2RecoveryLiveError("recovery canary requires the sealed OpenAI route")
    return role, credential


def _contract(
    *,
    policy: LiteraryStructuredOutputPolicy,
    role_id: str,
    role: Any,
    credential: ResolvedCredential,
    schema: Mapping[str, Any],
) -> StructuredOutputContract:
    contract = resolve_structured_output_contract(
        policy,
        role_id=role_id,
        provider=role.provider,
        base_url=credential.base_url,
        model_id=role.model_id,
        canonical_schema=schema,
    )
    if not contract.native_enforcement:
        raise B2RecoveryLiveError("recovery canary requires native structured output")
    return contract


def _request_reserve(
    request: RenderedB2RecoveryRequestV1,
    *,
    contract: StructuredOutputContract,
    schema_name: str,
    max_output_tokens: int,
) -> int:
    return estimate_prompt_tokens(
        [dict(row) for row in request.messages],
        openai_response_format(contract, schema_name=schema_name),
    ) + max_output_tokens


def _visible_tokens(usage: Mapping[str, Any]) -> int:
    # OpenAI-compatible usage reports reasoning_tokens as a completion-token
    # detail, so adding it again would double-count the same output tokens.
    return int(usage.get("prompt_tokens") or 0) + int(
        usage.get("completion_tokens") or 0
    )


def _stage_raw(
    *,
    stage_dir: Path,
    request: RenderedB2RecoveryRequestV1,
    result: OpenAICompatibleStructuredResult,
    credential: ResolvedCredential,
    contract: StructuredOutputContract,
) -> dict[str, Any]:
    raw = {
        "schema_version": "literary_b2_recovery_raw_result_v1",
        "request_kind": request.request_kind,
        "component_id": request.component_id,
        "request_fingerprint": request.request_fingerprint,
        "model": result.model,
        "quota_bucket_id": credential.quota_bucket_id,
        "credential_revision": credential.credential_revision,
        "credential_commitment": credential.commitment,
        "response_text": result.response_text,
        "parsed_json": result.parsed_json,
        "json_error": result.json_error,
        "usage": dict(result.usage),
        "latency_ms": result.latency_ms,
        "cost_usd": result.cost_usd,
        "from_cache": result.from_cache,
        "cache_key": result.cache_key,
        "structured_output_contract": contract.to_payload(),
        "completed_at": _now(),
    }
    _write_new_json(stage_dir / "raw_result.json", raw)
    return raw


def _execute_stage(
    *,
    stage_dir: Path,
    request: RenderedB2RecoveryRequestV1,
    role: Any,
    credential: ResolvedCredential,
    contract: StructuredOutputContract,
    binding: Mapping[str, Any],
    generation: Mapping[str, Any],
    seal_hash: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    _write_new_json(stage_dir / "request.json", _request_payload(request))
    _write_new_json(
        stage_dir / "stage_started.json",
        {
            "schema_version": "literary_b2_recovery_stage_started_v1",
            "request_kind": request.request_kind,
            "component_id": request.component_id,
            "request_fingerprint": request.request_fingerprint,
            "run_seal_hash": seal_hash,
            "model_id": role.model_id,
            "quota_bucket_id": credential.quota_bucket_id,
            "credential_commitment": credential.commitment,
            "started_at": _now(),
        },
    )
    try:
        result = call_openai_compatible_structured_v1(
            credential=credential,
            model_id=role.model_id,
            messages=request.messages,
            contract=contract,
            schema_name=str(binding["schema_name"]),
            cache_path=stage_dir / "cache" / "response.sqlite3",
            tag=f"literary_b2_recovery:{request.request_kind}:{request.component_id}",
            prompt_token_cap=int(binding["prompt_token_cap"]),
            max_output_tokens=int(binding["max_output_tokens"]),
            temperature=float(generation["temperature"]),
            seed=int(generation["seed"]),
            reasoning_effort=str(generation["reasoning_effort"]),
            verbosity=str(generation["verbosity"]),
        )
        raw = _stage_raw(
            stage_dir=stage_dir,
            request=request,
            result=result,
            credential=credential,
            contract=contract,
        )
        if not isinstance(result.parsed_json, Mapping):
            raise B2RecoveryLiveError(
                f"{request.request_kind} returned invalid JSON: {result.json_error}"
            )
        validate_structured_payload(
            result.parsed_json,
            canonical_schema=request.response_schema,
        )
        return dict(result.parsed_json), raw
    except Exception as exc:
        _write_new_json(
            stage_dir / "stage_failure.json",
            {
                "schema_version": "literary_b2_recovery_stage_failure_v1",
                "request_kind": request.request_kind,
                "component_id": request.component_id,
                "error_type": type(exc).__name__,
                "error": str(exc),
                "provider_call_attempted": True,
                "retry_performed": False,
                "fallback_performed": False,
                "failed_at": _now(),
            },
        )
        raise


def _component_stage_dir(
    root: Path,
    *,
    collection: str,
    ordinal: int,
    component_id: str,
    multi_component: bool,
) -> Path:
    if multi_component:
        return root / collection / f"{ordinal:02d}_{component_id}"
    return root / collection


def _load_reusable_stage_response(
    *,
    resume_root: Path | None,
    collection: str,
    ordinal: int,
    request: RenderedB2RecoveryRequestV1,
    multi_component: bool,
    model_id: str,
    credential: ResolvedCredential,
    contract: StructuredOutputContract,
) -> tuple[dict[str, Any], dict[str, Any]] | None:
    if resume_root is None:
        return None
    prior_stage = _component_stage_dir(
        resume_root,
        collection=collection,
        ordinal=ordinal,
        component_id=request.component_id,
        multi_component=multi_component,
    )
    raw_path = prior_stage / "raw_result.json"
    request_path = prior_stage / "request.json"
    reused_path = prior_stage / "reused_result.json"
    if (
        not raw_path.exists()
        and not request_path.exists()
        and not reused_path.exists()
    ):
        return None
    if not request_path.is_file() or not (
        raw_path.is_file() or reused_path.is_file()
    ):
        raise B2RecoveryLiveError(
            f"resume stage is incomplete: {prior_stage}"
        )
    prior_request = _read_object(request_path, "reusable stage request")
    source_run_root = resume_root
    if not raw_path.is_file():
        reused = _read_object(reused_path, "reused stage reference")
        if (
            reused.get("provider_call_performed") is not False
            or reused.get("request_kind") != request.request_kind
            or reused.get("component_id") != request.component_id
            or reused.get("request_fingerprint") != request.request_fingerprint
        ):
            raise B2RecoveryLiveError(
                f"reused stage identity drifted: {prior_stage}"
            )
        source_run_root_value = reused.get("source_run_root")
        source_raw_result_value = reused.get("source_raw_result")
        if not isinstance(source_run_root_value, str) or not isinstance(
            source_raw_result_value, str
        ):
            raise B2RecoveryLiveError(
                f"reused stage omits its source path: {prior_stage}"
            )
        source_run_root = Path(source_run_root_value).resolve()
        raw_path = Path(source_raw_result_value).resolve()
        if (
            not source_run_root.is_dir()
            or not raw_path.is_file()
            or not raw_path.is_relative_to(source_run_root)
            or file_sha256(raw_path) != reused.get("source_raw_result_sha256")
        ):
            raise B2RecoveryLiveError(
                f"reused stage source is missing or changed: {prior_stage}"
            )
        source_seal = _read_object(
            source_run_root / "run_seal.json", "reused source run seal"
        )
        if source_seal.get("seal_hash") != reused.get("source_run_seal_hash"):
            raise B2RecoveryLiveError(
                f"reused stage source seal drifted: {prior_stage}"
            )
    raw = _read_object(raw_path, "reusable stage raw result")
    expected_contract = contract.to_payload()
    observed_contract = raw.get("structured_output_contract") or {}
    if (
        prior_request.get("request_fingerprint")
        != request.request_fingerprint
        or raw.get("request_fingerprint") != request.request_fingerprint
        or raw.get("request_kind") != request.request_kind
        or raw.get("component_id") != request.component_id
        or raw.get("model") != model_id
        or raw.get("quota_bucket_id") != credential.quota_bucket_id
        or raw.get("credential_commitment") != credential.commitment
        or observed_contract.get("canonical_schema_hash")
        != expected_contract.get("canonical_schema_hash")
    ):
        raise B2RecoveryLiveError(
            f"resume stage identity drifted: {prior_stage}"
        )
    parsed = raw.get("parsed_json")
    if raw.get("json_error") is not None or not isinstance(parsed, Mapping):
        raise B2RecoveryLiveError(
            f"resume stage lacks valid structured JSON: {prior_stage}"
        )
    validate_structured_payload(
        parsed,
        canonical_schema=request.response_schema,
    )
    reference = {
        "schema_version": "literary_b2_recovery_reused_stage_v1",
        "request_kind": request.request_kind,
        "component_id": request.component_id,
        "request_fingerprint": request.request_fingerprint,
        "source_run_root": str(source_run_root),
        "source_raw_result": str(raw_path),
        "source_raw_result_sha256": file_sha256(raw_path),
        "source_run_seal_hash": _read_object(
            source_run_root / "run_seal.json", "resume run seal"
        ).get("seal_hash"),
        "provider_call_performed": False,
        "source_usage": dict(raw.get("usage") or {}),
        "source_visible_tokens": _visible_tokens(raw.get("usage") or {}),
        "revalidated_under_git_head": None,
        "reused_at": _now(),
    }
    return dict(parsed), reference


def _validate_resume_root(
    resume_root: Path | None,
    *,
    source_tree_hash: str,
    index: Mapping[str, Any],
    profile: Mapping[str, Any],
    profile_path: Path,
) -> dict[str, Any] | None:
    if resume_root is None:
        return None
    if not resume_root.is_dir():
        raise B2RecoveryLiveError(f"resume root does not exist: {resume_root}")
    seal = _read_object(resume_root / "run_seal.json", "resume run seal")
    prior_index = _read_object(
        resume_root / "recovery_index.json", "resume recovery index"
    )
    if (
        seal.get("source_tree_hash") != source_tree_hash
        or seal.get("source_b2_artifact_hash")
        != index["source_b2_artifact_hash"]
        or seal.get("recovery_index_hash") != index["recovery_index_hash"]
        or prior_index.get("recovery_index_hash")
        != index["recovery_index_hash"]
        or seal.get("profile_id") != profile["profile_id"]
        or seal.get("profile_sha256") != file_sha256(profile_path)
    ):
        raise B2RecoveryLiveError("resume root does not match the sealed run input")
    return {
        "resume_root": str(resume_root),
        "resume_tree_hash": _tree_hash(resume_root),
        "resume_run_seal_hash": seal.get("seal_hash"),
        "resume_failed_run_retained": (resume_root / "run_failure.json").is_file(),
    }


def _run_legacy(
    *,
    repo_root: Path,
    b2_root: Path,
    output_root: Path,
    profile_path: Path,
    credential_root: Path,
    frozen_db: Path,
    resume_from_root: Path | None = None,
) -> dict[str, Any]:
    repo = repo_root.resolve()
    source = b2_root.resolve()
    output = output_root.resolve()
    resume_source = (
        resume_from_root.resolve() if resume_from_root is not None else None
    )
    if output.exists():
        raise B2RecoveryLiveError(f"output root already exists: {output}")
    if resume_source is not None and output == resume_source:
        raise B2RecoveryLiveError("resume output must be a new immutable root")
    frozen_before = file_sha256(frozen_db).upper()
    if frozen_before != FROZEN_DB_SHA256:
        raise B2RecoveryLiveError("frozen DB differs from the accepted baseline")

    profile = _load_profile(profile_path)
    (
        event_review_contract_version,
        render_event_review_request,
        validate_event_review_response,
        build_event_revision_ledger,
    ) = _event_review_contract_functions(profile)
    config_root = profile_path.resolve().parent
    provider_path = config_root / str(profile["provider_profile"])
    policy_path = config_root / str(profile["structured_output_policy"])
    provider = load_provider_profile(provider_path)
    policy = load_literary_structured_output_policy(policy_path)
    bindings = profile["stage_bindings"]
    registry_binding = bindings["registry_recovery"]
    event_binding = bindings["event_review"]
    registry_role_id = str(registry_binding["provider_role_id"])
    event_role_id = str(event_binding["provider_role_id"])
    registry_role, registry_credential = _role_and_credential(
        provider=provider,
        role_id=registry_role_id,
        credential_root=credential_root,
    )
    event_role, event_credential = _role_and_credential(
        provider=provider,
        role_id=event_role_id,
        credential_root=credential_root,
    )
    if (
        registry_credential.quota_bucket_id
        != event_credential.quota_bucket_id
    ):
        raise B2RecoveryLiveError("recovery stages are not pinned to one bucket")
    if registry_credential.commitment != event_credential.commitment:
        raise B2RecoveryLiveError("recovery credentials drifted between stages")

    source_tree_hash = _tree_hash(source)
    chapter_artifact = _read_object(
        source / "chapter_b2_artifact.json", "chapter B2 artifact"
    )
    interaction_paths = sorted((source / "interactions").glob("*/request.json"))
    if not interaction_paths:
        raise B2RecoveryLiveError("B2 source has no interaction requests")
    interaction_requests = [
        _read_object(path, f"interaction request {path.parent.name}")
        for path in interaction_paths
    ]
    index = build_b2_recovery_index_v1(
        chapter_artifact=chapter_artifact,
        interaction_requests=interaction_requests,
    )
    resume_context = _validate_resume_root(
        resume_source,
        source_tree_hash=source_tree_hash,
        index=index,
        profile=profile,
        profile_path=profile_path,
    )
    if index["chapter_id"] != profile["safety"]["stop_after_chapter_id"]:
        raise B2RecoveryLiveError("source chapter differs from the sealed stop chapter")
    registry_components = [
        row for row in index["registry_components"] if not row["overflow"]
    ]
    event_components = [
        row for row in index["event_components"] if not row["overflow"]
    ]
    multi_component = (
        profile["schema_version"] == "literary_b2_recovery_live_profile_v3"
    )
    if multi_component:
        if len(registry_components) > int(
            profile["limits"]["registry_recovery_calls"]
        ) or not 1 <= len(event_components) <= int(
            profile["limits"]["event_review_calls"]
        ):
            raise B2RecoveryLiveError(
                "recovery components exceed the sealed multi-component caps"
            )
    elif len(registry_components) > 1 or len(event_components) != 1:
        raise B2RecoveryLiveError(
            "this canary permits at most one registry and exactly one event component"
        )
    registry_requests = [
        render_registry_recovery_request_v1(
            index=index,
            component_id=component["component_id"],
        )
        for component in registry_components
    ]
    base_event_requests = [
        render_event_review_request(
            index=index,
            component_id=component["component_id"],
            chapter_artifact=chapter_artifact,
            registry_ledger=None,
        )
        for component in event_components
    ]
    registry_contracts = [
        _contract(
            policy=policy,
            role_id=registry_role_id,
            role=registry_role,
            credential=registry_credential,
            schema=request.response_schema,
        )
        for request in registry_requests
    ]
    base_event_contracts = [
        _contract(
            policy=policy,
            role_id=event_role_id,
            role=event_role,
            credential=event_credential,
            schema=request.response_schema,
        )
        for request in base_event_requests
    ]
    reserve = sum(
        _request_reserve(
            request,
            contract=contract,
            schema_name=str(registry_binding["schema_name"]),
            max_output_tokens=int(registry_binding["max_output_tokens"]),
        )
        for request, contract in zip(
            registry_requests, registry_contracts, strict=True
        )
    ) + sum(
        _request_reserve(
            request,
            contract=contract,
            schema_name=str(event_binding["schema_name"]),
            max_output_tokens=int(event_binding["max_output_tokens"]),
        )
        for request, contract in zip(
            base_event_requests, base_event_contracts, strict=True
        )
    )
    hard_cap = int(profile["limits"]["hard_visible_token_cap"])
    if reserve > hard_cap:
        raise B2RecoveryLiveError(
            f"conservative reserve {reserve} exceeds hard cap {hard_cap}"
        )

    if multi_component:
        component_ids_payload: dict[str, Any] = {
            "registry_recovery": [row.component_id for row in registry_requests],
            "event_review": [row.component_id for row in base_event_requests],
        }
        request_fingerprints_payload: dict[str, Any] = {
            "registry_recovery": [
                row.request_fingerprint for row in registry_requests
            ],
            "event_review_before_registry_overlay": [
                row.request_fingerprint for row in base_event_requests
            ],
        }
        registry_contract_payload: dict[str, Any] = {
            "structured_output_contracts": [
                {
                    "component_id": request.component_id,
                    "contract": contract.to_payload(),
                }
                for request, contract in zip(
                    registry_requests, registry_contracts, strict=True
                )
            ],
            "call_count": len(registry_requests),
        }
        event_contract_payload: dict[str, Any] = {
            "structured_output_contracts": [
                {
                    "component_id": request.component_id,
                    "contract": contract.to_payload(),
                }
                for request, contract in zip(
                    base_event_requests, base_event_contracts, strict=True
                )
            ],
            "call_count": len(base_event_requests),
        }
    else:
        registry_request = registry_requests[0] if registry_requests else None
        registry_contract = registry_contracts[0] if registry_contracts else None
        base_event_request = base_event_requests[0]
        base_event_contract = base_event_contracts[0]
        component_ids_payload = {
            "registry_recovery": (
                registry_request.component_id
                if registry_request is not None
                else None
            ),
            "event_review": base_event_request.component_id,
        }
        request_fingerprints_payload = {
            "registry_recovery": (
                registry_request.request_fingerprint
                if registry_request is not None
                else None
            ),
            "event_review_before_registry_overlay": (
                base_event_request.request_fingerprint
            ),
        }
        registry_contract_payload = {
            "structured_output_contract": (
                registry_contract.to_payload()
                if registry_contract is not None
                else None
            ),
            "call_required": registry_request is not None,
        }
        event_contract_payload = {
            "structured_output_contract": base_event_contract.to_payload(),
        }

    seal_body = {
        "schema_version": (
            RUN_SEAL_SCHEMA_VERSION_V4
            if resume_context is not None
            else RUN_SEAL_SCHEMA_VERSION_V3
            if multi_component
            else (
                RUN_SEAL_SCHEMA_VERSION_V2
                if event_review_contract_version == "v2"
                else RUN_SEAL_SCHEMA_VERSION
            )
        ),
        "status": "sealed_before_api",
        "git_head": _git_head(repo),
        "profile_id": profile["profile_id"],
        "profile_path": str(profile_path.resolve()),
        "profile_sha256": file_sha256(profile_path),
        "provider_profile_id": provider.profile_id,
        "provider_profile_sha256": file_sha256(provider_path),
        "structured_output_policy_id": policy.policy_id,
        "structured_output_policy_sha256": file_sha256(policy_path),
        "output_root": str(output),
        "source_b2_root": str(source),
        "source_tree_hash": source_tree_hash,
        "source_b2_artifact_hash": index["source_b2_artifact_hash"],
        "recovery_index_hash": index["recovery_index_hash"],
        "chapter_id": index["chapter_id"],
        "component_ids": component_ids_payload,
        "base_request_fingerprints": request_fingerprints_payload,
        "stage_bindings": {
            "registry_recovery": {
                "provider_role_id": registry_role_id,
                "model_id": registry_role.model_id,
                "quota_bucket_id": registry_credential.quota_bucket_id,
                "credential_revision": registry_credential.credential_revision,
                "credential_commitment": registry_credential.commitment,
                **registry_contract_payload,
            },
            "event_review": {
                "provider_role_id": event_role_id,
                "model_id": event_role.model_id,
                "quota_bucket_id": event_credential.quota_bucket_id,
                "credential_revision": event_credential.credential_revision,
                "credential_commitment": event_credential.commitment,
                **event_contract_payload,
            },
        },
        "limits": {
            **profile["limits"],
            "conservative_total_token_reserve": reserve,
        },
        "safety": dict(profile["safety"]),
        "frozen_db_sha256_before": frozen_before,
        "gold_or_oracle_loaded": False,
        "source_artifact_mutated": False,
        "production_publish_performed": False,
        "resume": resume_context,
        "resume_policy": (
            "revalidate_exact_raw_then_call_missing_components"
            if resume_context is not None
            else "disabled"
        ),
        "sealed_at": _now(),
    }
    seal = {**seal_body, "seal_hash": canonical_hash(seal_body)}
    output.mkdir(parents=True)
    _write_new_json(output / "run_seal.json", seal)
    _write_new_json(output / "recovery_index.json", index)

    usage_rows: list[dict[str, Any]] = []
    reused_stage_rows: list[dict[str, Any]] = []
    try:
        registry_decisions: list[dict[str, Any]] = []
        for ordinal, (registry_request, registry_contract) in enumerate(
            zip(registry_requests, registry_contracts, strict=True), 1
        ):
            registry_stage_dir = _component_stage_dir(
                output,
                collection="registry_recovery",
                ordinal=ordinal,
                component_id=registry_request.component_id,
                multi_component=multi_component,
            )
            reusable = _load_reusable_stage_response(
                resume_root=resume_source,
                collection="registry_recovery",
                ordinal=ordinal,
                request=registry_request,
                multi_component=multi_component,
                model_id=registry_role.model_id,
                credential=registry_credential,
                contract=registry_contract,
            )
            if reusable is not None:
                registry_response, reuse_reference = reusable
                reuse_reference["revalidated_under_git_head"] = seal["git_head"]
                _write_new_json(
                    registry_stage_dir / "request.json",
                    _request_payload(registry_request),
                )
                _write_new_json(
                    registry_stage_dir / "reused_result.json", reuse_reference
                )
                reused_stage_rows.append(reuse_reference)
            else:
                registry_response, registry_raw = _execute_stage(
                    stage_dir=registry_stage_dir,
                    request=registry_request,
                    role=registry_role,
                    credential=registry_credential,
                    contract=registry_contract,
                    binding=registry_binding,
                    generation=profile["generation"],
                    seal_hash=seal["seal_hash"],
                )
                usage_rows.append(
                    {
                        "stage": (
                            f"registry_recovery:{registry_request.component_id}"
                            if multi_component
                            else "registry_recovery"
                        ),
                        "model": registry_raw["model"],
                        "usage": registry_raw["usage"],
                    }
                )
            registry_decision = validate_registry_recovery_response_v1(
                registry_response,
                index=index,
                component_id=registry_request.component_id,
                request_fingerprint=registry_request.request_fingerprint,
            )
            _write_new_json(
                registry_stage_dir / "decision.json",
                registry_decision,
            )
            registry_decisions.append(registry_decision)
        if not registry_requests:
            _write_new_json(
                output / "registry_recovery" / "skipped.json",
                {
                    "schema_version": "literary_b2_registry_recovery_skip_v1",
                    "reason": "no_registry_gap_tickets",
                    "recovery_index_hash": index["recovery_index_hash"],
                    "provider_call_performed": False,
                },
            )
        registry_ledger = build_registry_recovery_ledger_v1(
            index=index,
            decisions=registry_decisions,
        )
        _write_new_json(output / "registry_recovery_ledger.json", registry_ledger)

        event_requests = [
            render_event_review_request(
                index=index,
                component_id=component["component_id"],
                chapter_artifact=chapter_artifact,
                registry_ledger=registry_ledger,
            )
            for component in event_components
        ]
        event_contracts = [
            _contract(
                policy=policy,
                role_id=event_role_id,
                role=event_role,
                credential=event_credential,
                schema=request.response_schema,
            )
            for request in event_requests
        ]
        for event_contract, base_event_contract in zip(
            event_contracts, base_event_contracts, strict=True
        ):
            if (
                event_contract.canonical_schema_hash
                != base_event_contract.canonical_schema_hash
            ):
                raise B2RecoveryLiveError(
                    "event response schema drifted after registry overlay"
                )
        actual_so_far = sum(_visible_tokens(row["usage"]) for row in usage_rows)
        event_reserves = [
            _request_reserve(
                request,
                contract=contract,
                schema_name=str(event_binding["schema_name"]),
                max_output_tokens=int(event_binding["max_output_tokens"]),
            )
            for request, contract in zip(
                event_requests, event_contracts, strict=True
            )
        ]
        if actual_so_far + sum(event_reserves) > hard_cap:
            raise B2RecoveryLiveError(
                "event calls would exceed the sealed token cap"
            )

        event_decisions: list[dict[str, Any]] = []
        for ordinal, (event_request, event_contract, next_reserve) in enumerate(
            zip(event_requests, event_contracts, event_reserves, strict=True), 1
        ):
            event_stage_dir = _component_stage_dir(
                output,
                collection="event_review",
                ordinal=ordinal,
                component_id=event_request.component_id,
                multi_component=multi_component,
            )
            actual_so_far = sum(
                _visible_tokens(row["usage"]) for row in usage_rows
            )
            remaining_reserve = sum(event_reserves[ordinal - 1 :])
            if actual_so_far + remaining_reserve > hard_cap:
                raise B2RecoveryLiveError(
                    "remaining event calls would exceed the sealed token cap"
                )
            event_stage_seal_body = {
                "schema_version": (
                    "literary_b2_recovery_event_stage_seal_v3"
                    if multi_component
                    else (
                        "literary_b2_recovery_event_stage_seal_v2"
                        if event_review_contract_version == "v2"
                        else "literary_b2_recovery_event_stage_seal_v1"
                    )
                ),
                "run_seal_hash": seal["seal_hash"],
                "registry_recovery_ledger_hash": registry_ledger[
                    "registry_recovery_ledger_hash"
                ],
                "request_fingerprint": event_request.request_fingerprint,
                "structured_output_contract": event_contract.to_payload(),
                "remaining_conservative_reserve": (
                    remaining_reserve if multi_component else next_reserve
                ),
                "sealed_at": _now(),
            }
            if multi_component:
                event_stage_seal_body.update(
                    {
                        "component_id": event_request.component_id,
                        "request_conservative_reserve": next_reserve,
                    }
                )
            event_stage_seal = {
                **event_stage_seal_body,
                "event_stage_seal_hash": canonical_hash(event_stage_seal_body),
            }
            _write_new_json(
                (
                    event_stage_dir / "stage_seal.json"
                    if multi_component
                    else output / "event_stage_seal.json"
                ),
                event_stage_seal,
            )

            reusable = _load_reusable_stage_response(
                resume_root=resume_source,
                collection="event_review",
                ordinal=ordinal,
                request=event_request,
                multi_component=multi_component,
                model_id=event_role.model_id,
                credential=event_credential,
                contract=event_contract,
            )
            if reusable is not None:
                event_response, reuse_reference = reusable
                reuse_reference["revalidated_under_git_head"] = seal["git_head"]
                _write_new_json(
                    event_stage_dir / "request.json",
                    _request_payload(event_request),
                )
                _write_new_json(
                    event_stage_dir / "reused_result.json", reuse_reference
                )
                reused_stage_rows.append(reuse_reference)
            else:
                event_response, event_raw = _execute_stage(
                    stage_dir=event_stage_dir,
                    request=event_request,
                    role=event_role,
                    credential=event_credential,
                    contract=event_contract,
                    binding=event_binding,
                    generation=profile["generation"],
                    seal_hash=seal["seal_hash"],
                )
                usage_rows.append(
                    {
                        "stage": (
                            f"event_review:{event_request.component_id}"
                            if multi_component
                            else "event_review"
                        ),
                        "model": event_raw["model"],
                        "usage": event_raw["usage"],
                    }
                )
            event_decision = validate_event_review_response(
                event_response,
                index=index,
                component_id=event_request.component_id,
                chapter_artifact=chapter_artifact,
                registry_ledger=registry_ledger,
                request_fingerprint=event_request.request_fingerprint,
            )
            _write_new_json(event_stage_dir / "decision.json", event_decision)
            event_decisions.append(event_decision)

        event_ledger = build_event_revision_ledger(
            index=index,
            chapter_artifact=chapter_artifact,
            registry_ledger=registry_ledger,
            decisions=event_decisions,
        )
        _write_new_json(output / "event_revision_ledger.json", event_ledger)
        projection_builder = (
            build_effective_b2_projection_v2
            if event_review_contract_version == "v2"
            else build_effective_b2_projection_v1
        )
        projection = projection_builder(
            chapter_artifact=chapter_artifact,
            index=index,
            registry_ledger=registry_ledger,
            event_ledger=event_ledger,
        )
        _write_new_json(output / "effective_b2_projection.json", projection)
    except Exception as exc:
        _write_new_json(
            output / "run_failure.json",
            {
                "schema_version": (
                    "literary_b2_recovery_run_failure_v4"
                    if resume_context is not None
                    else "literary_b2_recovery_run_failure_v3"
                    if multi_component
                    else (
                        "literary_b2_recovery_run_failure_v2"
                        if event_review_contract_version == "v2"
                        else "literary_b2_recovery_run_failure_v1"
                    )
                ),
                "run_seal_hash": seal["seal_hash"],
                "error_type": type(exc).__name__,
                "error": str(exc),
                "completed_provider_calls": len(usage_rows),
                "usage_rows": usage_rows,
                "reused_stage_rows": reused_stage_rows,
                "retry_performed": False,
                "fallback_performed": False,
                "frozen_db_sha256_after": file_sha256(frozen_db).upper(),
                "failed_at": _now(),
            },
        )
        raise

    if _tree_hash(source) != source_tree_hash:
        raise B2RecoveryLiveError("source B2 artifact changed during recovery")
    frozen_after = file_sha256(frozen_db).upper()
    if frozen_after != frozen_before:
        raise B2RecoveryLiveError("frozen DB changed during recovery")
    visible_tokens = sum(_visible_tokens(row["usage"]) for row in usage_rows)
    if visible_tokens > hard_cap:
        raise B2RecoveryLiveError("provider usage exceeded the sealed token cap")

    registry_actions = [
        row["action"] for row in registry_ledger["ticket_resolutions"]
    ]
    event_actions = [row["action"] for row in event_ledger["event_revisions"]]
    report_body = {
        "schema_version": (
            LIVE_REPORT_SCHEMA_VERSION_V4
            if resume_context is not None
            else LIVE_REPORT_SCHEMA_VERSION_V3
            if multi_component
            else (
                LIVE_REPORT_SCHEMA_VERSION_V2
                if event_review_contract_version == "v2"
                else LIVE_REPORT_SCHEMA_VERSION
            )
        ),
        "status": "complete",
        "run_seal_hash": seal["seal_hash"],
        "git_head": seal["git_head"],
        "chapter_id": index["chapter_id"],
        "source_b2_artifact_hash": index["source_b2_artifact_hash"],
        "recovery_index_hash": index["recovery_index_hash"],
        "registry_recovery_ledger_hash": registry_ledger[
            "registry_recovery_ledger_hash"
        ],
        "event_revision_ledger_hash": event_ledger["event_revision_ledger_hash"],
        "effective_projection_hash": projection["effective_projection_hash"],
        "provider_calls": len(usage_rows),
        "usage_rows": usage_rows,
        "visible_tokens": visible_tokens,
        "resume": resume_context,
        "reused_stage_count": len(reused_stage_rows),
        "reused_stage_rows": reused_stage_rows,
        "reused_prior_visible_tokens": sum(
            int(row["source_visible_tokens"]) for row in reused_stage_rows
        ),
        "registry_action_counts": {
            action: registry_actions.count(action)
            for action in sorted(set(registry_actions))
        },
        "event_action_counts": {
            action: event_actions.count(action)
            for action in sorted(set(event_actions))
        },
        "recovered_candidate_card_count": len(
            projection["recovered_candidate_cards"]
        ),
        "effective_event_count": len(projection["interaction_events"]),
        "pending_registry_ticket_count": len(
            projection["pending_registry_tickets"]
        ),
        "pending_event_case_count": len(projection["pending_event_cases"]),
        "registry_recovery_skipped_no_tickets": not registry_requests,
        "retry_performed": False,
        "fallback_performed": False,
        "gold_or_oracle_loaded": False,
        "source_artifact_mutated": False,
        "book_global_identity_mutation_performed": False,
        "relation_phase_inference_performed": False,
        "translation_performed": False,
        "production_publish_performed": False,
        "frozen_db_sha256_after": frozen_after,
        "completed_at": _now(),
    }
    report = {**report_body, "report_hash": canonical_hash(report_body)}
    _write_new_json(output / "live_report.json", report)
    return report


def run(
    *,
    repo_root: Path,
    b2_root: Path,
    output_root: Path,
    profile_path: Path,
    credential_root: Path | None,
    frozen_db: Path,
    resume_from_root: Path | None = None,
    backend_mode: str = "legacy",
    shared_runtime: "LiterarySharedRunnerBindingsV1 | None" = None,
) -> dict[str, Any]:
    """Select exactly one recovery backend without transport fallback."""

    if backend_mode == "legacy":
        if shared_runtime is not None:
            raise B2RecoveryLiveError(
                "legacy recovery cannot receive a shared runtime"
            )
        if credential_root is None:
            raise B2RecoveryLiveError(
                "legacy recovery requires its explicit credential root"
            )
        return _run_legacy(
            repo_root=repo_root,
            b2_root=b2_root,
            output_root=output_root,
            profile_path=profile_path,
            credential_root=credential_root,
            frozen_db=frozen_db,
            resume_from_root=resume_from_root,
        )
    if backend_mode == "shared_v1":
        if credential_root is not None:
            raise B2RecoveryLiveError(
                "shared recovery cannot receive a legacy credential root"
            )
        if shared_runtime is None:
            raise B2RecoveryLiveError(
                "shared recovery requires an injected shared runtime"
            )
        from pipeline.literary.b2_recovery_shared_runner_v1 import (
            run_b2_recovery_shared_v1,
        )

        return run_b2_recovery_shared_v1(
            repo_root=repo_root,
            b2_root=b2_root,
            output_root=output_root,
            profile_path=profile_path,
            frozen_db=frozen_db,
            shared_runtime=shared_runtime,
            resume_from_root=resume_from_root,
        )
    raise B2RecoveryLiveError(
        "backend_mode must be exactly legacy or shared_v1"
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo-root", type=Path, required=True)
    parser.add_argument("--b2-root", type=Path, required=True)
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument("--profile", type=Path, required=True)
    parser.add_argument("--credential-root", type=Path, required=True)
    parser.add_argument("--frozen-db", type=Path, required=True)
    parser.add_argument("--resume-from-root", type=Path)
    args = parser.parse_args()
    report = run(
        repo_root=args.repo_root,
        b2_root=args.b2_root,
        output_root=args.output_root,
        profile_path=args.profile,
        credential_root=args.credential_root,
        frozen_db=args.frozen_db,
        resume_from_root=args.resume_from_root,
    )
    print(canonical_json(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
