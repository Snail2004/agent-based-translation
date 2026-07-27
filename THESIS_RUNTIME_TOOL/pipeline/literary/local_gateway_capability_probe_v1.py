"""Sealed Local GPT Gateway probes for the exact Literary B2 schemas."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from datetime import UTC, datetime
import json
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from openai import OpenAI

from pipeline.agents.provider_profile import (
    ProviderProfile,
    ProviderRole,
    ResolvedCredential,
    load_provider_profile,
    resolve_role_credential,
)
from pipeline.literary.b0_entity_conflict_auditor import (
    entity_conflict_response_schema,
)
from pipeline.literary.b0_entity_inventory_experiment import (
    entity_inventory_response_schema,
)
from pipeline.literary.b2_prompts_v1 import b2_frame_response_schema
from pipeline.literary.b2_prompts_v2 import b2_interaction_response_schema_v2
from pipeline.literary.checkpoint import canonical_hash, file_sha256
from pipeline.literary.structured_prompt_reserve_v1 import (
    structured_prompt_reserve_v1,
)
from pipeline.literary.structured_output_policy_v1 import (
    validate_structured_payload,
)
from pipeline.literary.transport_json import parse_structured_response
from pipeline.scripts.run_chapter_registry_v4_real import FROZEN_DB_SHA256


PROFILE_SCHEMA_VERSION = "literary_local_gateway_capability_probe_profile_v1"
SEAL_SCHEMA_VERSION = "literary_local_gateway_capability_probe_seal_v1"
REPORT_SCHEMA_VERSION = "literary_local_gateway_capability_probe_report_v1"


class LocalGatewayCapabilityProbeError(RuntimeError):
    """The sealed capability probe could not complete without drift."""


@dataclass(frozen=True)
class ProbeSpec:
    probe_id: str
    role_id: str
    schema_id: str
    output_token_cap: int


@dataclass(frozen=True)
class ProbeProfile:
    source_path: Path
    profile_id: str
    provider_profile_path: Path
    quota_bucket_id: str
    probes: tuple[ProbeSpec, ...]
    max_calls: int
    max_retries_per_call: int
    hard_visible_token_cap: int
    profile_hash: str


def load_probe_profile_v1(path: Path) -> ProbeProfile:
    source = Path(path).resolve()
    payload = _read_object(source, "Local Gateway probe profile")
    _exact_keys(
        payload,
        {
            "schema_version",
            "profile_id",
            "provider_profile",
            "quota_bucket_id",
            "probes",
            "limits",
            "safety",
        },
        "Local Gateway probe profile",
    )
    if payload.get("schema_version") != PROFILE_SCHEMA_VERSION:
        raise LocalGatewayCapabilityProbeError("foreign probe profile schema")
    raw_probes = payload.get("probes")
    if not isinstance(raw_probes, list) or len(raw_probes) != 4:
        raise LocalGatewayCapabilityProbeError(
            "probe profile needs exactly four probes"
        )
    probes: list[ProbeSpec] = []
    for index, raw in enumerate(raw_probes):
        if not isinstance(raw, Mapping):
            raise LocalGatewayCapabilityProbeError("probe row must be an object")
        _exact_keys(
            raw,
            {"probe_id", "role_id", "schema_id", "output_token_cap"},
            f"probe {index}",
        )
        schema_id = _required_string(raw.get("schema_id"), f"probe {index} schema")
        if schema_id not in {
            "b1_inventory_current",
            "b2_interaction_v2",
            "local_auditor_current",
            "b2_frame_v1",
        }:
            raise LocalGatewayCapabilityProbeError("probe schema is unsupported")
        probes.append(
            ProbeSpec(
                probe_id=_required_string(raw.get("probe_id"), f"probe {index} id"),
                role_id=_required_string(raw.get("role_id"), f"probe {index} role"),
                schema_id=schema_id,
                output_token_cap=_bounded_int(
                    raw.get("output_token_cap"),
                    f"probe {index} output cap",
                    256,
                    4096,
                ),
            )
        )
    if len({row.probe_id for row in probes}) != len(probes):
        raise LocalGatewayCapabilityProbeError("probe ids are duplicated")
    if {row.schema_id for row in probes} != {
        "b1_inventory_current",
        "b2_interaction_v2",
        "local_auditor_current",
        "b2_frame_v1",
    }:
        raise LocalGatewayCapabilityProbeError(
            "probe schemas must cover both premium models and schema families"
        )

    limits = _object(payload.get("limits"), "probe limits")
    _exact_keys(
        limits,
        {"max_calls", "max_retries_per_call", "hard_visible_token_cap"},
        "probe limits",
    )
    safety = _object(payload.get("safety"), "probe safety")
    expected_safety = {
        "provider_fallback_allowed": False,
        "production_publish_enabled": False,
        "semantic_quality_claim_allowed": False,
        "stop_on_first_failure": True,
    }
    if safety != expected_safety:
        raise LocalGatewayCapabilityProbeError("probe safety policy is not closed")
    max_calls = _bounded_int(limits.get("max_calls"), "max_calls", 4, 4)
    max_retries = _bounded_int(
        limits.get("max_retries_per_call"), "max_retries_per_call", 0, 0
    )
    provider_profile = _sibling_file(
        source, payload.get("provider_profile"), "provider_profile"
    )
    return ProbeProfile(
        source_path=source,
        profile_id=_required_string(payload.get("profile_id"), "profile_id"),
        provider_profile_path=provider_profile,
        quota_bucket_id=_required_string(
            payload.get("quota_bucket_id"), "quota_bucket_id"
        ),
        probes=tuple(probes),
        max_calls=max_calls,
        max_retries_per_call=max_retries,
        hard_visible_token_cap=_bounded_int(
            limits.get("hard_visible_token_cap"),
            "hard_visible_token_cap",
            4_000,
            40_000,
        ),
        profile_hash=canonical_hash(payload),
    )


def prepare_local_gateway_probe_v1(
    *,
    output_root: Path,
    profile_path: Path,
    credential_root: Path,
    frozen_db: Path,
    current_git_head: str,
) -> dict[str, Any]:
    root = Path(output_root).resolve()
    if root.exists() and any(root.iterdir()):
        raise LocalGatewayCapabilityProbeError("probe output root must be empty")
    root.mkdir(parents=True, exist_ok=True)
    profile = load_probe_profile_v1(profile_path)
    provider, routes = _resolved_routes(profile, credential_root=credential_root)
    frozen_hash = file_sha256(Path(frozen_db))
    if frozen_hash.upper() != FROZEN_DB_SHA256:
        raise LocalGatewayCapabilityProbeError("frozen database hash drifted")

    probe_rows: list[dict[str, Any]] = []
    total_reserve = 0
    for ordinal, (spec, role, _credential) in enumerate(routes, start=1):
        schema = _schema(spec.schema_id)
        messages = _messages(spec.schema_id)
        reserve = structured_prompt_reserve_v1(
            messages=messages,
            response_schema=schema,
            output_token_cap=spec.output_token_cap,
        ).to_payload()
        total_reserve += int(reserve["total_token_reserve"])
        probe_rows.append(
            {
                "ordinal": ordinal,
                "probe_id": spec.probe_id,
                "role_id": spec.role_id,
                "model_id": role.model_id,
                "schema_id": spec.schema_id,
                "canonical_schema_hash": canonical_hash(schema),
                "schema_keywords": sorted(_schema_keywords(schema)),
                "messages_hash": canonical_hash(messages),
                "output_token_cap": spec.output_token_cap,
                "token_reserve": reserve,
            }
        )
    if total_reserve > profile.hard_visible_token_cap:
        raise LocalGatewayCapabilityProbeError("probe reserve exceeds hard token cap")
    commitment = routes[0][2].commitment
    if any(row[2].commitment != commitment for row in routes):
        raise LocalGatewayCapabilityProbeError(
            "probe routes do not share one credential"
        )

    seal_body = {
        "schema_version": SEAL_SCHEMA_VERSION,
        "status": "prepared_no_api",
        "profile_id": profile.profile_id,
        "profile_hash": profile.profile_hash,
        "provider_profile_id": provider.profile_id,
        "provider_profile_hash": provider.profile_hash,
        "git_head": _required_string(current_git_head, "git_head"),
        "frozen_db_sha256": frozen_hash.upper(),
        "provider": "openai",
        "base_url": "http://localhost:8317/v1",
        "quota_bucket_id": profile.quota_bucket_id,
        "credential_revision": routes[0][2].credential_revision,
        "credential_commitment": commitment,
        "max_calls": profile.max_calls,
        "max_retries_per_call": profile.max_retries_per_call,
        "hard_visible_token_cap": profile.hard_visible_token_cap,
        "total_token_reserve": total_reserve,
        "probes": probe_rows,
        "provider_fallback_allowed": False,
        "production_publish_enabled": False,
        "semantic_quality_claim_allowed": False,
    }
    seal = {**seal_body, "seal_hash": canonical_hash(seal_body)}
    _write_new_json(root / "run_seal.json", seal)
    _write_new_json(
        root / "preflight.json",
        {
            "schema_version": "literary_local_gateway_probe_preflight_v1",
            "status": "ready_for_exactly_four_calls",
            "seal_hash": seal["seal_hash"],
            "total_token_reserve": total_reserve,
            "credential_secret_persisted": False,
        },
    )
    return seal


def execute_local_gateway_probe_v1(
    *,
    output_root: Path,
    profile_path: Path,
    credential_root: Path,
    frozen_db: Path,
    current_git_head: str,
    transport_factory: Callable[[ResolvedCredential], Callable[..., Any]] | None = None,
) -> dict[str, Any]:
    root = Path(output_root).resolve()
    seal = _read_object(root / "run_seal.json", "probe seal")
    profile = load_probe_profile_v1(profile_path)
    provider, routes = _resolved_routes(profile, credential_root=credential_root)
    _verify_seal(
        seal=seal,
        profile=profile,
        provider=provider,
        routes=routes,
        frozen_db=frozen_db,
        current_git_head=current_git_head,
    )
    make_transport = transport_factory or _default_transport
    total_visible = 0
    completed: list[dict[str, Any]] = []
    for ordinal, (spec, role, credential) in enumerate(routes, start=1):
        call_dir = root / "calls" / f"{ordinal:02d}_{spec.probe_id}"
        call_dir.mkdir(parents=True, exist_ok=False)
        schema = _schema(spec.schema_id)
        messages = _messages(spec.schema_id)
        request_record = {
            "schema_version": "literary_local_gateway_probe_request_v1",
            "probe_id": spec.probe_id,
            "model_id": role.model_id,
            "quota_bucket_id": credential.quota_bucket_id,
            "canonical_schema_hash": canonical_hash(schema),
            "messages": messages,
            "response_format": _response_format(schema, spec.probe_id),
            "output_token_cap": spec.output_token_cap,
            "credential_commitment": credential.commitment,
        }
        _write_new_json(call_dir / "request.json", request_record)
        try:
            response = make_transport(credential)(
                model=role.model_id,
                messages=messages,
                response_format=request_record["response_format"],
                max_completion_tokens=spec.output_token_cap,
                temperature=1.0,
            )
            text, model_actual, finish_reason, usage = _response_fields(response)
            if not _model_matches(role.model_id, model_actual):
                raise LocalGatewayCapabilityProbeError(
                    "gateway returned a different model than the sealed route"
                )
            parsed, normalization = parse_structured_response(text)
            validate_structured_payload(parsed, canonical_schema=schema)
            visible = int(usage["total_tokens"])
            total_visible += visible
            if total_visible > profile.hard_visible_token_cap:
                raise LocalGatewayCapabilityProbeError(
                    "observed usage exceeded the hard visible-token cap"
                )
            result = {
                "schema_version": "literary_local_gateway_probe_result_v1",
                "status": "passed_transport_and_local_schema",
                "probe_id": spec.probe_id,
                "model_requested": role.model_id,
                "model_actual": model_actual,
                "quota_bucket_id": credential.quota_bucket_id,
                "credential_commitment": credential.commitment,
                "finish_reason": finish_reason,
                "transport_normalization": normalization,
                "usage": usage,
                "canonical_schema_hash": canonical_hash(schema),
                "parsed_payload_hash": canonical_hash(parsed),
                "semantic_quality_evaluated": False,
                "production_publish_performed": False,
            }
            _write_new_json(
                call_dir / "raw_result.json", {**result, "response_text": text}
            )
            completed.append(result)
        except Exception as exc:
            failure = {
                "schema_version": "literary_local_gateway_probe_failure_v1",
                "status": "halted_fail_closed",
                "probe_id": spec.probe_id,
                "model_requested": role.model_id,
                "quota_bucket_id": credential.quota_bucket_id,
                "error_type": type(exc).__name__,
                "message": _safe_error(exc, secrets=(credential.secret,)),
                "completed_call_count": len(completed),
                "production_publish_performed": False,
            }
            _write_new_json(call_dir / "failure.json", failure)
            _write_new_json(root / "failure.json", failure)
            raise

    report_body = {
        "schema_version": REPORT_SCHEMA_VERSION,
        "status": "passed_exact_model_schema_probe",
        "seal_hash": seal["seal_hash"],
        "git_head": current_git_head,
        "quota_bucket_id": profile.quota_bucket_id,
        "call_count": len(completed),
        "total_visible_tokens": total_visible,
        "results": completed,
        "semantic_quality_evaluated": False,
        "production_publish_performed": False,
        "frozen_db_sha256_after": file_sha256(Path(frozen_db)).upper(),
        "completed_at": datetime.now(UTC).isoformat(),
    }
    report = {**report_body, "report_hash": canonical_hash(report_body)}
    _write_new_json(root / "report.json", report)
    return report


def _resolved_routes(
    profile: ProbeProfile, *, credential_root: Path
) -> tuple[ProviderProfile, list[tuple[ProbeSpec, ProviderRole, ResolvedCredential]]]:
    provider = load_provider_profile(profile.provider_profile_path)
    routes: list[tuple[ProbeSpec, ProviderRole, ResolvedCredential]] = []
    for spec in profile.probes:
        role = provider.roles.get(spec.role_id)
        if (
            role is None
            or role.provider != "openai"
            or role.model_id not in {"gpt-5.4", "gpt-5.5"}
            or role.bucket_order != (profile.quota_bucket_id,)
        ):
            raise LocalGatewayCapabilityProbeError("probe role route is not sealed")
        credential = resolve_role_credential(
            provider,
            role_id=spec.role_id,
            credential_root=Path(credential_root),
        )
        if (
            credential.quota_bucket_id != profile.quota_bucket_id
            or credential.base_url != "http://localhost:8317/v1"
        ):
            raise LocalGatewayCapabilityProbeError("probe credential route drifted")
        routes.append((spec, role, credential))
    return provider, routes


def _verify_seal(
    *,
    seal: Mapping[str, Any],
    profile: ProbeProfile,
    provider: ProviderProfile,
    routes: Sequence[tuple[ProbeSpec, ProviderRole, ResolvedCredential]],
    frozen_db: Path,
    current_git_head: str,
) -> None:
    if seal.get("schema_version") != SEAL_SCHEMA_VERSION:
        raise LocalGatewayCapabilityProbeError("foreign probe seal")
    body = {key: deepcopy(value) for key, value in seal.items() if key != "seal_hash"}
    if seal.get("seal_hash") != canonical_hash(body):
        raise LocalGatewayCapabilityProbeError("probe seal hash drifted")
    expected = {
        "profile_hash": profile.profile_hash,
        "provider_profile_hash": provider.profile_hash,
        "git_head": current_git_head,
        "frozen_db_sha256": file_sha256(Path(frozen_db)).upper(),
        "quota_bucket_id": profile.quota_bucket_id,
        "credential_commitment": routes[0][2].commitment,
        "max_calls": 4,
        "max_retries_per_call": 0,
    }
    for key, value in expected.items():
        if seal.get(key) != value:
            raise LocalGatewayCapabilityProbeError(f"probe seal drifted at {key}")
    if expected["frozen_db_sha256"] != FROZEN_DB_SHA256:
        raise LocalGatewayCapabilityProbeError("frozen database hash drifted")


def _schema(schema_id: str) -> dict[str, Any]:
    if schema_id == "b1_inventory_current":
        return entity_inventory_response_schema()
    if schema_id == "b2_interaction_v2":
        return b2_interaction_response_schema_v2()
    if schema_id == "local_auditor_current":
        return entity_conflict_response_schema()
    if schema_id == "b2_frame_v1":
        return b2_frame_response_schema()
    raise LocalGatewayCapabilityProbeError("foreign probe schema id")


def _messages(schema_id: str) -> list[dict[str, str]]:
    if schema_id == "b1_inventory_current":
        task = (
            "Return a schema-valid transport probe with empty entity, glossary, "
            "unresolved, and chapter-priority lists."
        )
    elif schema_id == "b2_interaction_v2":
        task = (
            "Return a schema-valid transport probe for chapter_id probe_chapter and "
            "window_id probe_window. All three output lists may be empty."
        )
    elif schema_id == "local_auditor_current":
        task = (
            "Return a schema-valid transport probe for chapter_id probe_chapter "
            "with empty component-decision and glossary-disposition lists."
        )
    else:
        task = (
            "Return a schema-valid transport probe for chapter_id probe_chapter. "
            "Use a short non-empty gist, narrative_mode unknown, and empty setting, "
            "frame-start, and review lists."
        )
    return [
        {
            "role": "system",
            "content": (
                "This is a provider capability probe, not a literary inference task. "
                "Return exactly one JSON object matching the supplied JSON Schema."
            ),
        },
        {"role": "user", "content": task},
    ]


def _transport_schema(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {
            str(key): _transport_schema(child)
            for key, child in value.items()
            if key not in {"minItems", "minLength", "uniqueItems"}
        }
    if isinstance(value, list):
        return [_transport_schema(child) for child in value]
    return deepcopy(value)


def _schema_keywords(value: Any) -> set[str]:
    known = {
        "additionalProperties",
        "anyOf",
        "const",
        "enum",
        "items",
        "maxItems",
        "maxLength",
        "minItems",
        "minLength",
        "properties",
        "required",
        "type",
        "uniqueItems",
    }
    result: set[str] = set()
    if isinstance(value, Mapping):
        for key, child in value.items():
            if key in known:
                result.add(str(key))
            result.update(_schema_keywords(child))
    elif isinstance(value, list):
        for child in value:
            result.update(_schema_keywords(child))
    return result


def _response_format(schema: Mapping[str, Any], name: str) -> dict[str, Any]:
    return {
        "type": "json_schema",
        "json_schema": {
            "name": name,
            "strict": True,
            "schema": _transport_schema(schema),
        },
    }


def _default_transport(credential: ResolvedCredential) -> Callable[..., Any]:
    return OpenAI(
        api_key=credential.secret,
        base_url=credential.base_url,
        timeout=credential.request_timeout_ms / 1000,
        max_retries=0,
    ).chat.completions.create


def _response_fields(response: Any) -> tuple[str, str, str, dict[str, int]]:
    try:
        choice = response.choices[0]
        text = choice.message.content
        usage = response.usage
        prompt = int(usage.prompt_tokens or 0)
        completion = int(usage.completion_tokens or 0)
        total = int(usage.total_tokens or (prompt + completion))
        if not isinstance(text, str) or not text.strip():
            raise ValueError("empty structured response")
        return (
            text,
            str(response.model),
            str(choice.finish_reason or "unknown"),
            {
                "prompt_tokens": prompt,
                "completion_tokens": completion,
                "total_tokens": total,
            },
        )
    except (AttributeError, IndexError, TypeError, ValueError) as exc:
        raise LocalGatewayCapabilityProbeError("malformed gateway response") from exc


def _model_matches(requested: str, actual: str) -> bool:
    return actual == requested or actual.startswith(requested + "-")


def _read_object(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError) as exc:
        raise LocalGatewayCapabilityProbeError(f"cannot read {label}") from exc
    if not isinstance(value, dict):
        raise LocalGatewayCapabilityProbeError(f"{label} must be an object")
    return value


def _write_new_json(path: Path, payload: Mapping[str, Any]) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        raise LocalGatewayCapabilityProbeError(f"refusing to overwrite {target.name}")
    target.write_text(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )


def _sibling_file(source: Path, value: Any, label: str) -> Path:
    raw = _required_string(value, label)
    path = Path(raw)
    if path.is_absolute() or ".." in path.parts:
        raise LocalGatewayCapabilityProbeError(f"{label} must be a sibling file")
    result = (source.parent / path).resolve()
    if result.parent != source.parent:
        raise LocalGatewayCapabilityProbeError(f"{label} escapes config directory")
    return result


def _object(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise LocalGatewayCapabilityProbeError(f"{label} must be an object")
    return dict(value)


def _exact_keys(value: Mapping[str, Any], expected: set[str], label: str) -> None:
    if set(value) != expected:
        raise LocalGatewayCapabilityProbeError(f"{label} keys differ")


def _required_string(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise LocalGatewayCapabilityProbeError(f"{label} must be non-empty")
    return value.strip()


def _bounded_int(value: Any, label: str, low: int, high: int) -> int:
    if (
        not isinstance(value, int)
        or isinstance(value, bool)
        or not low <= value <= high
    ):
        raise LocalGatewayCapabilityProbeError(f"{label} is outside its closed range")
    return value


def _safe_error(exc: Exception, *, secrets: Sequence[str] = ()) -> str:
    text = str(exc).replace("\r", " ").replace("\n", " ")
    for secret in secrets:
        if secret:
            text = text.replace(secret, "<redacted>")
    return text[:500]


__all__ = [
    "LocalGatewayCapabilityProbeError",
    "ProbeProfile",
    "execute_local_gateway_probe_v1",
    "load_probe_profile_v1",
    "prepare_local_gateway_probe_v1",
]
