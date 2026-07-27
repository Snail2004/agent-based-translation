"""Sealed CKEY diagnostics for the Literary B2 structured-output boundary."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
import json
from pathlib import Path
from typing import Any, Mapping, Sequence

from jsonschema import Draft202012Validator

from pipeline.agents.judge_client import JudgeClient
from pipeline.agents.llm_client import LLMClient
from pipeline.agents.llm_config import LLMConfig
from pipeline.agents.provider_profile import (
    ProviderProfile,
    ProviderRole,
    ResolvedCredential,
    load_provider_profile,
    resolve_role_credential,
)
from pipeline.literary.b2_contract_v1 import (
    B2ContractError,
    normalize_b2_interaction_response_v1,
)
from pipeline.literary.b2_prompts_v1 import (
    B2_INTERACTION_SYSTEM_PROMPT,
    b2_interaction_response_schema,
)
from pipeline.literary.checkpoint import (
    canonical_hash,
    file_sha256,
    write_checkpoint_atomic,
)
from pipeline.literary.transport_json import (
    LiteraryTransportJsonError,
    parse_structured_response,
)
from pipeline.scripts.run_chapter_registry_v2_gemini import _gemini_transport
from pipeline.scripts.run_chapter_registry_v2_real import RESPONSE_FORMAT_JSON
from pipeline.scripts.run_chapter_registry_v4_real import FROZEN_DB_SHA256


PROFILE_SCHEMA_VERSION = "literary_b2_ckey_diagnostic_profile_v1"
PROFILE_SCHEMA_VERSION_V2 = "literary_b2_provider_diagnostic_profile_v2"
PROFILE_SCHEMA_VERSION_V3 = "literary_b2_provider_diagnostic_profile_v3"
SEAL_SCHEMA_VERSION = "literary_b2_ckey_diagnostic_seal_v1"
REPORT_SCHEMA_VERSION = "literary_b2_ckey_diagnostic_report_v1"
PROBE_REQUEST_SCHEMA_VERSION = "literary_b2_ckey_probe_request_v1"
PROBE_RESULT_SCHEMA_VERSION = "literary_b2_ckey_probe_result_v1"

PROBE_IDS = (
    "schema_authority_small",
    "b2_schema_small_context",
    "long_json_transport",
    "b2_full_load_reproduction",
)


class B2CkeyDiagnosticError(RuntimeError):
    """A sealed diagnostic, transport, or integrity failure."""


@dataclass(frozen=True)
class B2CkeyDiagnosticProfile:
    source_path: Path
    profile_id: str
    provider_profile_path: Path
    role_id: str
    quota_bucket_id: str
    probe_ids: tuple[str, ...]
    max_calls: int
    max_retries_per_call: int
    hard_visible_token_cap: int
    prompt_token_cap: int
    default_output_token_cap: int
    safety: Mapping[str, Any]
    openrouter_policy: Mapping[str, Any] | None
    profile_hash: str


def load_b2_ckey_diagnostic_profile_v1(
    path: Path,
) -> B2CkeyDiagnosticProfile:
    source = Path(path).resolve()
    payload = _read_object(source, "B2 CKEY diagnostic profile")
    schema_version = payload.get("schema_version")
    common_keys = {
        "schema_version",
        "profile_id",
        "provider_profile",
        "role_id",
        "limits",
        "safety",
    }
    openrouter_policy: Mapping[str, Any] | None = None
    if schema_version == PROFILE_SCHEMA_VERSION:
        _exact_keys(payload, common_keys, "B2 CKEY diagnostic profile")
        quota_bucket_id = "ckey-account-v1"
        probe_ids = PROBE_IDS
    elif schema_version in {PROFILE_SCHEMA_VERSION_V2, PROFILE_SCHEMA_VERSION_V3}:
        extra_keys = {"quota_bucket_id", "probe_ids"}
        if schema_version == PROFILE_SCHEMA_VERSION_V3:
            extra_keys.add("openrouter_policy")
        _exact_keys(
            payload,
            common_keys | extra_keys,
            "B2 provider diagnostic profile",
        )
        quota_bucket_id = _required_string(
            payload.get("quota_bucket_id"), "quota_bucket_id"
        )
        raw_probe_ids = payload.get("probe_ids")
        if (
            not isinstance(raw_probe_ids, list)
            or not raw_probe_ids
            or any(
                not isinstance(probe_id, str) or probe_id not in PROBE_IDS
                for probe_id in raw_probe_ids
            )
            or len(set(raw_probe_ids)) != len(raw_probe_ids)
        ):
            raise B2CkeyDiagnosticError(
                "probe_ids must be a non-empty unique subset of known probes"
            )
        probe_ids = tuple(raw_probe_ids)
        if schema_version == PROFILE_SCHEMA_VERSION_V3:
            openrouter_policy = _validated_openrouter_policy(
                payload.get("openrouter_policy")
            )
    else:
        raise B2CkeyDiagnosticError("foreign B2 CKEY diagnostic profile schema")
    limits = _object(payload.get("limits"), "limits")
    _exact_keys(
        limits,
        {
            "max_calls",
            "max_retries_per_call",
            "hard_visible_token_cap",
            "prompt_token_cap",
            "default_output_token_cap",
        },
        "limits",
    )
    safety = _object(payload.get("safety"), "safety")
    _exact_keys(
        safety,
        {
            "provider_fallback_allowed",
            "production_publish_enabled",
            "semantic_output_publish_enabled",
            "stop_on_first_failed_probe",
        },
        "safety",
    )
    if safety != {
        "provider_fallback_allowed": False,
        "production_publish_enabled": False,
        "semantic_output_publish_enabled": False,
        "stop_on_first_failed_probe": True,
    }:
        raise B2CkeyDiagnosticError("B2 CKEY diagnostic safety policy is not closed")
    max_calls = _bounded_int(
        limits.get("max_calls"),
        "max_calls",
        len(probe_ids),
        len(probe_ids),
    )
    result = B2CkeyDiagnosticProfile(
        source_path=source,
        profile_id=_required_string(payload.get("profile_id"), "profile_id"),
        provider_profile_path=_sibling_file(
            source, payload.get("provider_profile"), "provider_profile"
        ),
        role_id=_required_string(payload.get("role_id"), "role_id"),
        quota_bucket_id=quota_bucket_id,
        probe_ids=probe_ids,
        max_calls=max_calls,
        max_retries_per_call=_bounded_int(
            limits.get("max_retries_per_call"),
            "max_retries_per_call",
            0,
            0,
        ),
        hard_visible_token_cap=_bounded_int(
            limits.get("hard_visible_token_cap"),
            "hard_visible_token_cap",
            20_000,
            100_000,
        ),
        prompt_token_cap=_bounded_int(
            limits.get("prompt_token_cap"), "prompt_token_cap", 1_000, 30_000
        ),
        default_output_token_cap=_bounded_int(
            limits.get("default_output_token_cap"),
            "default_output_token_cap",
            1_000,
            10_000,
        ),
        safety=dict(safety),
        openrouter_policy=openrouter_policy,
        profile_hash=canonical_hash(payload),
    )
    return result


def prepare_b2_ckey_diagnostic_v1(
    *,
    output_root: Path,
    profile_path: Path,
    credential_root: Path,
    full_load_request_path: Path,
    frozen_db: Path,
    current_git_head: str,
) -> dict[str, Any]:
    root = Path(output_root).resolve()
    if root.exists() and any(root.iterdir()):
        raise B2CkeyDiagnosticError("diagnostic output root must be empty")
    root.mkdir(parents=True, exist_ok=True)

    profile = load_b2_ckey_diagnostic_profile_v1(profile_path)
    provider_profile, role, credential = _resolve_provider(
        profile=profile, credential_root=credential_root
    )
    full_request = _validated_full_request(full_load_request_path)
    db_hash = file_sha256(frozen_db)
    if db_hash.upper() != FROZEN_DB_SHA256.upper():
        raise B2CkeyDiagnosticError("frozen DB hash differs before diagnostic")

    copied_request_path = root / "full_load_request.json"
    write_checkpoint_atomic(copied_request_path, full_request)
    plan = {
        "schema_version": SEAL_SCHEMA_VERSION,
        "profile_id": profile.profile_id,
        "profile_hash": profile.profile_hash,
        "provider_profile_id": provider_profile.profile_id,
        "provider_profile_hash": provider_profile.profile_hash,
        "role_id": role.role_id,
        "provider": role.provider,
        "model_id": role.model_id,
        "quota_bucket_id": credential.quota_bucket_id,
        "credential_revision": credential.credential_revision,
        "credential_commitment": credential.commitment,
        "current_git_head": _required_string(
            current_git_head, "current_git_head"
        ),
        "frozen_db_sha256": db_hash,
        "full_load_request_sha256": file_sha256(copied_request_path),
        "full_load_request_fingerprint": full_request["request_fingerprint"],
        "probe_order": list(profile.probe_ids),
        "limits": {
            "max_calls": profile.max_calls,
            "max_retries_per_call": profile.max_retries_per_call,
            "hard_visible_token_cap": profile.hard_visible_token_cap,
            "prompt_token_cap": profile.prompt_token_cap,
            "default_output_token_cap": profile.default_output_token_cap,
        },
        "safety": dict(profile.safety),
        "openrouter_policy": (
            dict(profile.openrouter_policy)
            if profile.openrouter_policy is not None
            else None
        ),
        "production_publish_performed": False,
        "prepared_at": _now(),
    }
    sealed = {**plan, "seal_hash": canonical_hash(plan)}
    write_checkpoint_atomic(root / "diagnostic_seal.json", sealed)
    return sealed


def execute_b2_ckey_diagnostic_v1(
    *,
    output_root: Path,
    profile_path: Path,
    credential_root: Path,
    frozen_db: Path,
    current_git_head: str,
) -> dict[str, Any]:
    root = Path(output_root).resolve()
    seal = _validated_seal(root / "diagnostic_seal.json")
    profile = load_b2_ckey_diagnostic_profile_v1(profile_path)
    provider_profile, role, credential = _resolve_provider(
        profile=profile, credential_root=credential_root
    )
    _verify_execution_boundary(
        root=root,
        seal=seal,
        profile=profile,
        provider_profile=provider_profile,
        role=role,
        credential=credential,
        frozen_db=frozen_db,
        current_git_head=current_git_head,
    )
    full_request = _validated_full_request(root / "full_load_request.json")

    results: list[dict[str, Any]] = []
    cumulative_tokens = 0
    for probe_id in profile.probe_ids:
        existing = root / "probes" / probe_id / "probe_result.json"
        if existing.exists():
            prior = _read_object(existing, f"probe result {probe_id}")
            if prior.get("probe_id") != probe_id:
                raise B2CkeyDiagnosticError("foreign persisted probe result")
            results.append(prior)
            cumulative_tokens += _usage_total(prior.get("usage"))
            if prior.get("status") != "passed":
                break
            continue

        request, output_cap = build_probe_request_v1(
            probe_id=probe_id,
            full_load_request=full_request,
            prompt_token_cap=profile.prompt_token_cap,
            default_output_token_cap=profile.default_output_token_cap,
        )
        conservative_reserve = int(request["token_reserve"]["total"])
        if cumulative_tokens + conservative_reserve > profile.hard_visible_token_cap:
            raise B2CkeyDiagnosticError(
                "next probe exceeds sealed visible-token cap"
            )
        result = _execute_probe(
            root=root,
            probe_id=probe_id,
            request=request,
            output_cap=output_cap,
            profile=profile,
            role=role,
            credential=credential,
        )
        results.append(result)
        cumulative_tokens += _usage_total(result.get("usage"))
        if cumulative_tokens > profile.hard_visible_token_cap:
            raise B2CkeyDiagnosticError(
                "provider usage exceeded sealed visible-token cap"
            )
        if result.get("status") != "passed":
            break

    report_body = {
        "schema_version": REPORT_SCHEMA_VERSION,
        "seal_hash": seal["seal_hash"],
        "status": (
            "complete"
            if len(results) == len(profile.probe_ids)
            and all(row.get("status") == "passed" for row in results)
            else "halted_on_diagnostic_failure"
        ),
        "diagnosis": _diagnosis(
            results, expected_probe_ids=profile.probe_ids
        ),
        "configured_probe_ids": list(profile.probe_ids),
        "executed_probe_ids": [row["probe_id"] for row in results],
        "skipped_probe_ids": [
            probe_id
            for probe_id in profile.probe_ids
            if probe_id not in {row["probe_id"] for row in results}
        ],
        "physical_calls": len(results),
        "visible_tokens": cumulative_tokens,
        "probe_results": [
            {
                "probe_id": row["probe_id"],
                "status": row["status"],
                "schema_valid": row["schema_valid"],
                "json_valid": row["json_valid"],
                "response_characters": row["response_characters"],
                "finish_reason": row["finish_reason"],
                "failure_reasons": list(row["failure_reasons"]),
            }
            for row in results
        ],
        "provider_fallback_performed": False,
        "retry_performed": False,
        "production_publish_performed": False,
        "semantic_output_publish_performed": False,
        "completed_at": _now(),
    }
    report = {**report_body, "report_hash": canonical_hash(report_body)}
    write_checkpoint_atomic(root / "diagnostic_report.json", report)
    return report


def build_probe_request_v1(
    *,
    probe_id: str,
    full_load_request: Mapping[str, Any],
    prompt_token_cap: int,
    default_output_token_cap: int,
) -> tuple[dict[str, Any], int]:
    if probe_id not in PROBE_IDS:
        raise B2CkeyDiagnosticError(f"unknown diagnostic probe {probe_id}")
    if probe_id == "schema_authority_small":
        schema = _schema_authority_schema()
        messages = [
            {
                "role": "user",
                "content": (
                    "Return JSON with status set to legacy_value and add a "
                    "legacy_field. Do not add any other fields."
                ),
            }
        ]
        output_cap = 1_000
    elif probe_id == "b2_schema_small_context":
        schema = b2_interaction_response_schema()
        messages = [
            {"role": "system", "content": B2_INTERACTION_SYSTEM_PROMPT},
            {
                "role": "user",
                "content": json.dumps(
                    _small_b2_payload(), ensure_ascii=True, separators=(",", ":")
                ),
            },
        ]
        output_cap = 3_000
    elif probe_id == "long_json_transport":
        schema = _long_transport_schema()
        messages = [
            {
                "role": "user",
                "content": (
                    "Return exactly 96 chunks. Each payload must contain "
                    "exactly 96 lowercase x characters. Use ordinals 1 through "
                    "96 in source order."
                ),
            }
        ]
        output_cap = 6_000
    else:
        schema = deepcopy(dict(full_load_request["response_schema"]))
        messages = deepcopy(list(full_load_request["messages"]))
        output_cap = default_output_token_cap
    prompt_reserve = _prompt_reserve(messages, schema)
    if prompt_reserve > prompt_token_cap:
        raise B2CkeyDiagnosticError(
            f"{probe_id} prompt reserve exceeds diagnostic prompt cap"
        )
    body = {
        "schema_version": PROBE_REQUEST_SCHEMA_VERSION,
        "probe_id": probe_id,
        "messages": messages,
        "response_schema": schema,
        "response_schema_hash": canonical_hash(schema),
        "token_reserve": {
            "prompt": prompt_reserve,
            "output": output_cap,
            "total": prompt_reserve + output_cap,
        },
        "production_publish_performed": False,
    }
    return {**body, "request_fingerprint": canonical_hash(body)}, output_cap


def evaluate_probe_response_v1(
    *,
    probe_id: str,
    request: Mapping[str, Any],
    response_text: str,
    parsed: Mapping[str, Any] | None,
    json_error: str | None,
    finish_reason: str,
    usage: Mapping[str, Any],
    transport_normalization: str,
) -> dict[str, Any]:
    reasons: list[str] = []
    schema_errors: list[str] = []
    schema_valid = False
    json_valid = isinstance(parsed, Mapping) and json_error is None
    if json_valid:
        schema_errors = [
            error.message
            for error in sorted(
                Draft202012Validator(request["response_schema"]).iter_errors(
                    parsed
                ),
                key=lambda item: list(item.absolute_path),
            )
        ]
        schema_valid = not schema_errors
    else:
        reasons.append("invalid_or_incomplete_json")
    if schema_errors:
        reasons.append("response_schema_violation")

    diagnostic_checks: dict[str, Any] = {}
    if schema_valid and parsed is not None:
        if probe_id == "schema_authority_small":
            diagnostic_checks = {
                "status": parsed.get("status"),
                "legacy_field_present": "legacy_field" in parsed,
                "values": list(parsed.get("values") or []),
            }
            if parsed.get("status") != "schema_ok" or "legacy_field" in parsed:
                reasons.append("schema_authority_not_observed")
        elif probe_id == "b2_schema_small_context":
            turns = list(parsed.get("speaker_turns") or [])
            events = list(parsed.get("interaction_events") or [])
            diagnostic_checks = {
                "speaker_turn_count": len(turns),
                "interaction_event_count": len(events),
                "nested_contract_exercised": bool(turns and events),
            }
            if not turns or not events:
                reasons.append("nested_b2_contract_not_exercised")
        elif probe_id == "long_json_transport":
            chunks = list(parsed.get("chunks") or [])
            diagnostic_checks = {
                "chunk_count": len(chunks),
                "minimum_payload_length": min(
                    (len(str(row.get("payload") or "")) for row in chunks),
                    default=0,
                ),
            }
            if len(chunks) != 96 or any(
                len(str(row.get("payload") or "")) != 96 for row in chunks
            ):
                reasons.append("long_json_exact_cover_failed")
            if len(response_text) <= 8_000:
                reasons.append("long_json_did_not_cross_8000_characters")
        elif probe_id == "b2_full_load_reproduction":
            try:
                artifact = normalize_b2_interaction_response_v1(
                    request=_full_request_from_probe(request),
                    response=parsed,
                )
                diagnostic_checks = {
                    "normalization_status": "accepted",
                    "speaker_turn_count": len(artifact["speaker_turns"]),
                    "interaction_event_count": len(
                        artifact["interaction_events"]
                    ),
                    "review_request_count": len(artifact["review_requests"]),
                }
            except B2ContractError as exc:
                diagnostic_checks = {
                    "normalization_status": "rejected",
                    "normalization_error": str(exc),
                }
                reasons.append("b2_semantic_normalization_rejected")

    body = {
        "schema_version": PROBE_RESULT_SCHEMA_VERSION,
        "probe_id": probe_id,
        "status": "passed" if not reasons else "failed",
        "json_valid": json_valid,
        "schema_valid": schema_valid,
        "schema_errors": schema_errors,
        "failure_reasons": reasons,
        "response_characters": len(response_text),
        "finish_reason": finish_reason,
        "transport_normalization": transport_normalization,
        "usage": dict(usage),
        "diagnostic_checks": diagnostic_checks,
        "production_publish_performed": False,
        "completed_at": _now(),
    }
    return {**body, "result_hash": canonical_hash(body)}


def _execute_probe(
    *,
    root: Path,
    probe_id: str,
    request: Mapping[str, Any],
    output_cap: int,
    profile: B2CkeyDiagnosticProfile,
    role: ProviderRole,
    credential: ResolvedCredential,
) -> dict[str, Any]:
    stage = root / "probes" / probe_id
    stage.mkdir(parents=True, exist_ok=True)
    write_checkpoint_atomic(stage / "request.json", request)
    config = LLMConfig(
        model=role.model_id,
        temperature=1.0,
        seed=20260717,
        reasoning_effort=(
            str(profile.openrouter_policy["reasoning_effort"])
            if profile.openrouter_policy is not None
            else "none"
        ),
        verbosity=None,
        max_output_tokens=output_cap,
        daily_token_cap=profile.hard_visible_token_cap,
        prompt_token_cap=profile.prompt_token_cap,
        pricing={"input": 0.0, "cached_input": 0.0, "output": 0.0},
    )
    if role.provider == "google_genai":
        transport = _gemini_transport(
            api_key=credential.secret,
            response_json_schema=request["response_schema"],
            timeout_ms=credential.request_timeout_ms,
            base_url=credential.base_url,
        )
        client: JudgeClient | LLMClient = JudgeClient(
            config,
            stage / "cache" / credential.quota_bucket_id / "diagnostic.sqlite3",
            transport=transport,
            max_retries=profile.max_retries_per_call,
        )
        response_format = RESPONSE_FORMAT_JSON
        transport_name = "google_genai_generate_content"
    elif role.provider == "openai":
        from openai import OpenAI

        sdk_transport = OpenAI(
            api_key=credential.secret,
            base_url=credential.base_url,
            timeout=credential.request_timeout_ms / 1_000,
        ).chat.completions.create
        if profile.openrouter_policy is not None:
            transport = _OpenRouterChatTransport(
                sdk_transport,
                profile.openrouter_policy,
            )
            transport_name = "openrouter_openai_compatible_chat_completions"
        else:
            transport = sdk_transport
            transport_name = "ckey_openai_compatible_chat_completions"
        client = LLMClient(
            config,
            stage / "cache" / credential.quota_bucket_id / "diagnostic.sqlite3",
            transport=transport,
            max_retries=profile.max_retries_per_call,
        )
        response_format = _openai_response_format(
            request["response_schema"],
            f"literary_b2_ckey_{probe_id}",
        )
    else:
        raise B2CkeyDiagnosticError("unsupported diagnostic provider")
    try:
        result = client.call(
            [dict(item) for item in request["messages"]],
            response_format=response_format,
            tag=f"literary_b2:ckey_diagnostic:{probe_id}",
            bypass_cache=True,
        )
    except Exception as exc:
        failure = _transport_failure_result(
            probe_id=probe_id,
            request=request,
            role=role,
            credential=credential,
            transport_name=transport_name,
            exc=exc,
        )
        write_checkpoint_atomic(stage / "transport_failure.json", failure)
        write_checkpoint_atomic(stage / "probe_result.json", failure)
        return failure
    parsed: Mapping[str, Any] | None = None
    json_error: str | None = None
    transport_normalization = "rejected"
    try:
        parsed_value, transport_normalization = parse_structured_response(
            result.text
        )
        if isinstance(parsed_value, Mapping):
            parsed = parsed_value
        else:
            json_error = "parsed JSON root is not an object"
    except LiteraryTransportJsonError as exc:
        json_error = str(exc)
    safe_transport_metadata = (
        dict(transport.last_metadata)
        if role.provider == "google_genai"
        else {
            "transport_base_url": credential.base_url,
            "transport_name": transport_name,
            **(
                dict(transport.last_metadata)
                if isinstance(transport, _OpenRouterChatTransport)
                else {}
            ),
        }
    )
    raw = {
        "schema_version": "literary_b2_ckey_diagnostic_raw_v1",
        "probe_id": probe_id,
        "model": result.model,
        "provider": role.provider,
        "quota_bucket_id": credential.quota_bucket_id,
        "credential_revision": credential.credential_revision,
        "credential_commitment": credential.commitment,
        "request_fingerprint": request["request_fingerprint"],
        "response_text": result.text,
        "parsed_json": parsed,
        "json_error": json_error,
        "transport_normalization": transport_normalization,
        "safe_transport_metadata": safe_transport_metadata,
        "usage": asdict(result.usage),
        "latency_ms": result.latency_ms,
        "cost_usd": result.cost_usd,
        "from_cache": result.from_cache,
        "completed_at": _now(),
    }
    write_checkpoint_atomic(stage / "raw_result.json", raw)
    evaluated_request = dict(request)
    if probe_id == "b2_full_load_reproduction":
        evaluated_request["_full_request"] = _read_object(
            root / "full_load_request.json", "full load request"
        )
    evaluated = evaluate_probe_response_v1(
        probe_id=probe_id,
        request=evaluated_request,
        response_text=result.text,
        parsed=parsed,
        json_error=json_error,
        finish_reason=str(
            safe_transport_metadata.get("gemini_finish_reason")
            or safe_transport_metadata.get("finish_reason")
            or "unknown"
        ),
        usage=asdict(result.usage),
        transport_normalization=transport_normalization,
    )
    write_checkpoint_atomic(stage / "probe_result.json", evaluated)
    return evaluated


def _openai_response_format(
    schema: Mapping[str, Any], name: str
) -> dict[str, Any]:
    return {
        "type": "json_schema",
        "json_schema": {
            "name": name,
            "strict": True,
            "schema": deepcopy(dict(schema)),
        },
    }


class _OpenRouterChatTransport:
    """Inject a sealed OpenRouter routing policy into one SDK transport."""

    def __init__(self, create: Any, policy: Mapping[str, Any]) -> None:
        self._create = create
        self._provider_policy = {
            "only": list(policy["provider_only"]),
            "allow_fallbacks": False,
            "require_parameters": True,
            "data_collection": "deny",
            "zdr": True,
        }
        self.last_metadata: dict[str, Any] = {}

    def __call__(self, **request: Any) -> Any:
        if "extra_body" in request:
            raise B2CkeyDiagnosticError(
                "OpenRouter diagnostic transport refuses caller extra_body"
            )
        if "max_completion_tokens" in request:
            if "max_tokens" in request:
                raise B2CkeyDiagnosticError(
                    "OpenRouter diagnostic received conflicting token caps"
                )
            request["max_tokens"] = request.pop("max_completion_tokens")
        response = self._create(
            **request,
            extra_body={"provider": deepcopy(self._provider_policy)},
        )
        model_extra = getattr(response, "model_extra", None)
        if not isinstance(model_extra, Mapping):
            model_extra = {}
        choices = getattr(response, "choices", None) or []
        finish_reason = (
            getattr(choices[0], "finish_reason", None) if choices else None
        )
        self.last_metadata = {
            "openrouter_provider": model_extra.get("provider"),
            "openrouter_service_tier": (
                getattr(response, "service_tier", None)
                or model_extra.get("service_tier")
            ),
            "openrouter_response_id": getattr(response, "id", None),
            "finish_reason": finish_reason,
            "provider_policy": deepcopy(self._provider_policy),
        }
        return response


def _transport_failure_result(
    *,
    probe_id: str,
    request: Mapping[str, Any],
    role: ProviderRole,
    credential: ResolvedCredential,
    transport_name: str,
    exc: Exception,
) -> dict[str, Any]:
    status_code = getattr(exc, "status_code", None)
    safe_message = str(exc)
    if len(safe_message) > 1_000:
        safe_message = safe_message[:1_000]
    body = {
        "schema_version": PROBE_RESULT_SCHEMA_VERSION,
        "probe_id": probe_id,
        "status": "failed",
        "json_valid": False,
        "schema_valid": False,
        "schema_errors": [],
        "failure_reasons": ["transport_error"],
        "response_characters": 0,
        "finish_reason": "transport_error",
        "transport_normalization": "not_reached",
        "usage": {
            "prompt_tokens": 0,
            "cached_tokens": 0,
            "completion_tokens": 0,
            "reasoning_tokens": 0,
        },
        "diagnostic_checks": {
            "transport_name": transport_name,
            "provider": role.provider,
            "model_id": role.model_id,
            "quota_bucket_id": credential.quota_bucket_id,
            "request_fingerprint": request["request_fingerprint"],
            "error_type": f"{type(exc).__module__}.{type(exc).__name__}",
            "http_status": status_code,
            "safe_error_message": safe_message,
        },
        "production_publish_performed": False,
        "completed_at": _now(),
    }
    return {**body, "result_hash": canonical_hash(body)}


def _schema_authority_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["status", "values"],
        "properties": {
            "status": {"type": "string", "enum": ["schema_ok"]},
            "values": {
                "type": "array",
                "minItems": 2,
                "maxItems": 2,
                "items": {"type": "string", "enum": ["alpha", "beta"]},
            },
        },
    }


def _long_transport_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["schema_version", "chunks"],
        "properties": {
            "schema_version": {
                "type": "string",
                "const": "literary_b2_ckey_long_transport_probe_v1",
            },
            "chunks": {
                "type": "array",
                "minItems": 96,
                "maxItems": 96,
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["ordinal", "payload"],
                    "properties": {
                        "ordinal": {"type": "integer", "minimum": 1, "maximum": 96},
                        "payload": {
                            "type": "string",
                            "minLength": 96,
                            "maxLength": 96,
                        },
                    },
                },
            },
        },
    }


def _small_b2_payload() -> dict[str, Any]:
    return {
        "active_blocks": [
            {
                "block_id": "diag_b001",
                "block_type": "paragraph",
                "text": "Mara met Ivo at the gate and handed him a sealed letter.",
            },
            {
                "block_id": "diag_b002",
                "block_type": "dialogue",
                "text": '"Thank you, Mara," Ivo said.',
            },
        ],
        "preceding_tail": [],
        "candidate_packets": {
            "schema_version": "literary_b2_candidate_packet_v1",
            "chapter_id": "diag_ch01",
            "active_block_ids": ["diag_b001", "diag_b002"],
            "candidate_cards": [
                {
                    "candidate_card_id": "diag_mara",
                    "canonical_surface": "Mara",
                },
                {
                    "candidate_card_id": "diag_ivo",
                    "canonical_surface": "Ivo",
                },
            ],
            "surface_groups": [],
            "identity_uncertainties": [],
            "overflow": False,
            "overflow_reasons": [],
        },
        "chapter_id": "diag_ch01",
        "frame_context": {
            "schema_version": "literary_b2_window_frame_context_v1",
            "applicable_segments": [
                {
                    "start_block_id": "diag_b001",
                    "end_block_id": "diag_b002",
                    "narrator_status": "external_or_authorial",
                    "candidate_card_ids": [],
                    "story_time_label": "frame_present",
                }
            ],
            "applicable_review_requests": [],
        },
        "frame_context_status": "ready",
        "prior_relation_states": [],
        "request_kind": "window_interaction",
        "window_id": "diag_w01",
    }


def _full_request_from_probe(request: Mapping[str, Any]) -> Mapping[str, Any]:
    value = request.get("_full_request")
    if not isinstance(value, Mapping):
        raise B2CkeyDiagnosticError("full-load probe lacks original request")
    return value


def _diagnosis(
    results: Sequence[Mapping[str, Any]],
    *,
    expected_probe_ids: Sequence[str] = PROBE_IDS,
) -> str:
    schema_only = tuple(expected_probe_ids) == ("schema_authority_small",)
    by_id = {str(row.get("probe_id")): row for row in results}
    first = by_id.get("schema_authority_small")
    if first and first.get("status") != "passed":
        if "transport_error" in first.get("failure_reasons", []):
            return (
                "provider_transport_failed_before_schema_authority_test"
                if schema_only
                else "ckey_transport_failed_before_schema_authority_test"
            )
        return (
            "provider_structured_schema_authority_failed"
            if schema_only
            else "ckey_structured_schema_authority_failed"
        )
    second = by_id.get("b2_schema_small_context")
    if second and second.get("status") != "passed":
        if "transport_error" in second.get("failure_reasons", []):
            return "ckey_transport_failed_before_small_b2_schema_test"
        return "b2_complex_schema_or_instruction_following_failed_at_small_load"
    third = by_id.get("long_json_transport")
    if third and third.get("status") != "passed":
        if "transport_error" in third.get("failure_reasons", []):
            return "ckey_transport_failed_before_long_json_test"
        return "ckey_long_json_transport_or_generation_limit_failed"
    fourth = by_id.get("b2_full_load_reproduction")
    if fourth and fourth.get("status") != "passed":
        if "transport_error" in fourth.get("failure_reasons", []):
            return "ckey_transport_failed_before_full_b2_load_test"
        if "b2_semantic_normalization_rejected" in fourth.get(
            "failure_reasons", []
        ):
            return "b2_full_load_schema_passed_but_semantic_normalization_failed"
        return "b2_full_load_contract_failed"
    if (
        schema_only
        and len(results) == 1
        and first
        and first.get("status") == "passed"
    ):
        return "structured_schema_authority_confirmed"
    if len(results) == len(expected_probe_ids):
        return "prior_failure_not_reproduced_under_same_transport"
    return "diagnostic_incomplete"


def _resolve_provider(
    *,
    profile: B2CkeyDiagnosticProfile,
    credential_root: Path,
) -> tuple[ProviderProfile, ProviderRole, ResolvedCredential]:
    provider_profile = load_provider_profile(profile.provider_profile_path)
    role = provider_profile.roles.get(profile.role_id)
    if role is None:
        raise B2CkeyDiagnosticError("provider profile lacks diagnostic role")
    if role.provider not in {"google_genai", "openai"} or len(
        role.bucket_order
    ) != 1:
        raise B2CkeyDiagnosticError(
            "diagnostic role must bind exactly one supported CKEY route"
        )
    credential = resolve_role_credential(
        provider_profile,
        role_id=profile.role_id,
        credential_root=credential_root,
    )
    if credential.quota_bucket_id != profile.quota_bucket_id:
        raise B2CkeyDiagnosticError(
            "diagnostic credential differs from the sealed profile bucket"
        )
    if profile.openrouter_policy is not None:
        if role.provider != "openai":
            raise B2CkeyDiagnosticError(
                "OpenRouter policy requires the OpenAI-compatible transport"
            )
        if credential.base_url != "https://openrouter.ai/api/v1":
            raise B2CkeyDiagnosticError(
                "OpenRouter policy requires the pinned OpenRouter API base URL"
            )
    return provider_profile, role, credential


def _validated_full_request(path: Path) -> dict[str, Any]:
    payload = _read_object(Path(path), "full-load B2 request")
    fingerprint = _required_string(
        payload.get("request_fingerprint"), "request_fingerprint"
    )
    body = {key: deepcopy(value) for key, value in payload.items() if key != "request_fingerprint"}
    if canonical_hash(body) != fingerprint:
        raise B2CkeyDiagnosticError("full-load request fingerprint differs")
    if payload.get("request_kind") != "window_interaction":
        raise B2CkeyDiagnosticError("full-load request is not a B2 interaction")
    if payload.get("response_schema") != b2_interaction_response_schema():
        raise B2CkeyDiagnosticError("full-load request uses a foreign B2 schema")
    if payload.get("response_schema_hash") != canonical_hash(
        payload["response_schema"]
    ):
        raise B2CkeyDiagnosticError("full-load response-schema hash differs")
    return payload


def _validated_seal(path: Path) -> dict[str, Any]:
    payload = _read_object(path, "B2 CKEY diagnostic seal")
    observed = _required_string(payload.get("seal_hash"), "seal_hash")
    body = {key: deepcopy(value) for key, value in payload.items() if key != "seal_hash"}
    if payload.get("schema_version") != SEAL_SCHEMA_VERSION:
        raise B2CkeyDiagnosticError("foreign diagnostic seal schema")
    if canonical_hash(body) != observed:
        raise B2CkeyDiagnosticError("diagnostic seal hash differs")
    return payload


def _verify_execution_boundary(
    *,
    root: Path,
    seal: Mapping[str, Any],
    profile: B2CkeyDiagnosticProfile,
    provider_profile: ProviderProfile,
    role: ProviderRole,
    credential: ResolvedCredential,
    frozen_db: Path,
    current_git_head: str,
) -> None:
    checks = {
        "profile hash": (seal.get("profile_hash"), profile.profile_hash),
        "provider profile hash": (
            seal.get("provider_profile_hash"),
            provider_profile.profile_hash,
        ),
        "role id": (seal.get("role_id"), role.role_id),
        "model id": (seal.get("model_id"), role.model_id),
        "quota bucket": (
            seal.get("quota_bucket_id"),
            credential.quota_bucket_id,
        ),
        "credential commitment": (
            seal.get("credential_commitment"),
            credential.commitment,
        ),
        "Git HEAD": (seal.get("current_git_head"), current_git_head),
        "frozen DB": (seal.get("frozen_db_sha256"), file_sha256(frozen_db)),
        "full request file": (
            seal.get("full_load_request_sha256"),
            file_sha256(root / "full_load_request.json"),
        ),
    }
    for label, (observed, expected) in checks.items():
        if observed != expected:
            raise B2CkeyDiagnosticError(f"sealed {label} differs")


def _prompt_reserve(
    messages: Sequence[Mapping[str, Any]], schema: Mapping[str, Any]
) -> int:
    payload = {
        "messages": list(messages),
        "response_schema": dict(schema),
    }
    return (len(_canonical_json(payload).encode("utf-8")) + 3) // 4


def _usage_total(value: Any) -> int:
    usage = _object(value, "usage")
    return int(usage.get("prompt_tokens") or 0) + int(
        usage.get("completion_tokens") or 0
    )


def _read_object(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise B2CkeyDiagnosticError(f"cannot read {label}") from exc
    if not isinstance(value, dict):
        raise B2CkeyDiagnosticError(f"{label} must be an object")
    return value


def _object(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise B2CkeyDiagnosticError(f"{label} must be an object")
    return value


def _validated_openrouter_policy(value: Any) -> Mapping[str, Any]:
    policy = _object(value, "openrouter_policy")
    _exact_keys(
        policy,
        {
            "provider_only",
            "allow_fallbacks",
            "require_parameters",
            "data_collection",
            "zdr",
            "reasoning_effort",
        },
        "openrouter_policy",
    )
    provider_only = policy.get("provider_only")
    if (
        not isinstance(provider_only, list)
        or len(provider_only) != 1
        or not isinstance(provider_only[0], str)
        or not provider_only[0].strip()
    ):
        raise B2CkeyDiagnosticError(
            "OpenRouter diagnostic must pin exactly one provider slug"
        )
    required = {
        "allow_fallbacks": False,
        "require_parameters": True,
        "data_collection": "deny",
        "zdr": True,
        "reasoning_effort": "minimal",
    }
    for key, expected in required.items():
        if policy.get(key) != expected:
            raise B2CkeyDiagnosticError(
                f"OpenRouter diagnostic policy is not closed at {key}"
            )
    return {
        "provider_only": [provider_only[0].strip()],
        **required,
    }


def _exact_keys(value: Mapping[str, Any], expected: set[str], label: str) -> None:
    if set(value) != expected:
        raise B2CkeyDiagnosticError(f"{label} keys differ")


def _required_string(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise B2CkeyDiagnosticError(f"{label} must be a non-empty string")
    return value


def _bounded_int(value: Any, label: str, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise B2CkeyDiagnosticError(f"{label} must be an integer")
    if value < minimum or value > maximum:
        raise B2CkeyDiagnosticError(f"{label} is outside its allowed range")
    return value


def _sibling_file(source: Path, value: Any, label: str) -> Path:
    name = _required_string(value, label)
    if Path(name).name != name:
        raise B2CkeyDiagnosticError(f"{label} must be a sibling filename")
    result = source.parent / name
    if not result.is_file():
        raise B2CkeyDiagnosticError(f"{label} file does not exist")
    return result.resolve()


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _now() -> str:
    return datetime.now(UTC).isoformat()


__all__ = [
    "B2CkeyDiagnosticError",
    "build_probe_request_v1",
    "evaluate_probe_response_v1",
    "execute_b2_ckey_diagnostic_v1",
    "load_b2_ckey_diagnostic_profile_v1",
    "prepare_b2_ckey_diagnostic_v1",
]
