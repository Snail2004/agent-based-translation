from __future__ import annotations

import argparse
from datetime import UTC, datetime
from pathlib import Path
import time
from typing import Any, Callable, Mapping

from pipeline.agents.judge_client import JudgeClient
from pipeline.agents.llm_client import LLMResult
from pipeline.agents.llm_config import LLMConfig
from pipeline.literary.chapter_registry_schema_v2 import (
    ALIAS_TYPES,
    GLOSSARY_CATEGORIES,
    MENTION_TYPES,
    REFERENT_KINDS,
    TICKET_TYPES,
    RegistryBudgetError,
    RunConfigV2,
)
from pipeline.literary.checkpoint import canonical_hash
from pipeline.literary.chapter_registry_v2 import estimate_registry_prompt_tokens
from pipeline.scripts.run_chapter_registry_v2_real import (
    CHAPTER_IDS,
    DEFAULT_DESIGN_DOC,
    DEFAULT_DOCUMENT,
    DEFAULT_FROZEN_DB,
    ExecutedRegistryCall,
    PhaseCError,
    RESPONSE_FORMAT_JSON,
    _load_config,
    _read_json,
    _record_halt_report,
    _usage_rows_from_existing_calls,
    run_phase_c,
)


REPO_ROOT = Path(__file__).resolve().parents[3]
RUNTIME_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_KEYS_FILE = REPO_ROOT / "GEMINI-KEY-FREE.txt"
DEFAULT_CONFIG = (
    RUNTIME_ROOT
    / "data"
    / "reports"
    / "literary_m4f_b1_prejoined_context_gemini_dry_20260714"
    / "run_config_gemini.json"
)
BUCKET_IDS = (
    "gemini-free-row1-v1",
    "gemini-free-row2-v1",
    "gemini-free-row3-v1",
    "gemini-free-row4-v1",
    "gemini-free-row5-v2",
)


def _string_schema(*, enum: set[str] | frozenset[str] | None = None) -> dict[str, Any]:
    schema: dict[str, Any] = {"type": "string"}
    if enum is not None:
        schema["enum"] = sorted(enum)
    return schema


def _nullable_string_schema() -> dict[str, Any]:
    return {"anyOf": [{"type": "string"}, {"type": "null"}]}


def _array_schema(items: Mapping[str, Any]) -> dict[str, Any]:
    return {"type": "array", "items": dict(items)}


def _object_schema(
    properties: Mapping[str, Mapping[str, Any]],
    *,
    optional: set[str] | frozenset[str] = frozenset(),
) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {key: dict(value) for key, value in properties.items()},
        "required": sorted(set(properties) - set(optional)),
        "additionalProperties": False,
    }


_STRING_LIST_SCHEMA = _array_schema(_string_schema())

_B0_RESPONSE_SCHEMA = _object_schema(
    {
        "gist": _string_schema(),
        "narrator_hypotheses": _array_schema(
            _object_schema(
                {
                    "surface": _nullable_string_schema(),
                    "note": _string_schema(),
                    "block_ids": _STRING_LIST_SCHEMA,
                }
            )
        ),
        "salient_surface_checklist": _array_schema(
            _object_schema(
                {
                    "surface": _string_schema(),
                    "block_id": _string_schema(),
                }
            )
        ),
    }
)

_ALIAS_ROW_SCHEMA = _object_schema(
    {
        "surface": _string_schema(),
        "alias_type": _string_schema(enum=ALIAS_TYPES),
        "support_block_ids": _STRING_LIST_SCHEMA,
    }
)

_B1_RESPONSE_SCHEMA = _object_schema(
    {
        "new_entities": _array_schema(
            _object_schema(
                {
                    "surface": _string_schema(),
                    "mention_type": _string_schema(enum=MENTION_TYPES),
                    "referent_kind_claim": _string_schema(enum=REFERENT_KINDS),
                    "short_description": _string_schema(),
                    "created_from_block_id": _string_schema(),
                    "support_block_ids": _STRING_LIST_SCHEMA,
                    "initial_aliases": _array_schema(_ALIAS_ROW_SCHEMA),
                },
                optional={"initial_aliases"},
            )
        ),
        "new_aliases": _array_schema(
            _object_schema(
                {
                    "surface": _string_schema(),
                    "alias_type": _string_schema(enum=ALIAS_TYPES),
                    "target_entity_id": _string_schema(),
                    "support_block_ids": _STRING_LIST_SCHEMA,
                }
            )
        ),
        "new_glossary_items": _array_schema(
            _object_schema(
                {
                    "surface": _string_schema(),
                    "category_claim": _string_schema(enum=GLOSSARY_CATEGORIES),
                    "short_description": _string_schema(),
                    "created_from_block_id": _string_schema(),
                    "support_block_ids": _STRING_LIST_SCHEMA,
                }
            )
        ),
        "local_bindings": _array_schema(
            _object_schema(
                {
                    "surface": _string_schema(),
                    "block_id": _string_schema(),
                    "target_entity_id": _string_schema(),
                    "support_block_ids": _STRING_LIST_SCHEMA,
                }
            )
        ),
        "tickets": _array_schema(
            _object_schema(
                {
                    "ticket_type": _string_schema(enum=TICKET_TYPES),
                    "surface": _nullable_string_schema(),
                    "block_id": _string_schema(),
                    "related_entity_ids": _STRING_LIST_SCHEMA,
                    "note": _string_schema(),
                }
            )
        ),
    }
)

_AUDITOR_RESPONSE_SCHEMA = _object_schema(
    {
        "entity_dispositions": _array_schema(
            _object_schema(
                {
                    "entity_id": _string_schema(),
                    "action": _string_schema(
                        enum={"confirm", "reject_noise", "merge_provisional", "remain_pending"}
                    ),
                    "merge_target_entity_id": _nullable_string_schema(),
                    "revised_identity_summary": _nullable_string_schema(),
                }
            )
        ),
        "alias_dispositions": _array_schema(
            _object_schema(
                {
                    "alias_id": _string_schema(),
                    "action": _string_schema(enum={"confirm", "reject", "remain_pending"}),
                }
            )
        ),
        "glossary_dispositions": _array_schema(
            _object_schema(
                {
                    "glossary_id": _string_schema(),
                    "action": _string_schema(
                        enum={"confirm", "reject_noise", "remain_pending"}
                    ),
                }
            )
        ),
        "local_binding_dispositions": _array_schema(
            _object_schema(
                {
                    "binding_id": _string_schema(),
                    "action": _string_schema(enum={"confirm", "reject", "remain_pending"}),
                }
            )
        ),
        "ticket_dispositions": _array_schema(
            _object_schema(
                {
                    "ticket_id": _string_schema(),
                    "action": _string_schema(enum={"resolve", "carry"}),
                    "resolution_note": _string_schema(),
                }
            )
        ),
        "profile_revisions": _array_schema(
            _object_schema(
                {
                    "entity_id": _string_schema(),
                    "revised_identity_summary": _string_schema(),
                    "resolved_ticket_ids": _STRING_LIST_SCHEMA,
                }
            )
        ),
    }
)

GEMINI_RESPONSE_SCHEMAS: Mapping[str, Mapping[str, Any]] = {
    "b0": _B0_RESPONSE_SCHEMA,
    "b1": _B1_RESPONSE_SCHEMA,
    "auditor": _AUDITOR_RESPONSE_SCHEMA,
}


def _now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()


def load_gemini_credentials(path: Path) -> tuple[dict[str, str], dict[str, str]]:
    values = [row.strip() for row in Path(path).read_text(encoding="utf-8").splitlines() if row.strip()]
    if len(values) != len(BUCKET_IDS):
        raise PhaseCError(f"Gemini key file must contain exactly {len(BUCKET_IDS)} non-empty rows")
    credentials: dict[str, str] = {}
    commitments: dict[str, str] = {}
    for bucket_id, value in zip(BUCKET_IDS, values, strict=True):
        if not value.startswith(("AIza", "AQ.A")) or len(value) < 35:
            raise PhaseCError(f"Gemini credential row for {bucket_id} is malformed")
        credentials[bucket_id] = value
        commitments[bucket_id] = canonical_hash({"credential": value})
    return credentials, commitments


def _gemini_transport(
    *,
    api_key: str,
    response_json_schema: Mapping[str, Any] | None,
    timeout_ms: int = 120_000,
    base_url: str | None = None,
) -> Callable[..., Any]:
    from google import genai
    from google.genai import types

    client = genai.Client(
        api_key=api_key,
        http_options=(
            types.HttpOptions(baseUrl=base_url, timeout=timeout_ms)
            if base_url
            else types.HttpOptions(timeout=timeout_ms)
        ),
    )

    def transport(**kwargs: Any) -> Any:
        system_parts: list[str] = []
        contents: list[str] = []
        for message in kwargs["messages"]:
            role = str(message.get("role", "user"))
            content = str(message.get("content", ""))
            if role == "system":
                system_parts.append(content)
            else:
                contents.append(content)
        response_format = kwargs.get("response_format") or {}
        wants_json = "json" in str(response_format.get("type", "")).casefold()
        config = types.GenerateContentConfig(
            temperature=float(kwargs.get("temperature", 1.0)),
            seed=int(kwargs.get("seed", 0)),
            max_output_tokens=int(kwargs.get("max_output_tokens", 2048)),
            response_mime_type="application/json" if wants_json else "text/plain",
            response_json_schema=(
                dict(response_json_schema)
                if wants_json and response_json_schema is not None
                else None
            ),
            system_instruction="\n\n".join(system_parts) or None,
            thinking_config=types.ThinkingConfig(thinking_budget=0),
        )
        response = client.models.generate_content(
            model=str(kwargs["model"]),
            contents="\n\n".join(contents),
            config=config,
        )
        candidates = getattr(response, "candidates", None) or []
        finish_reason = getattr(candidates[0], "finish_reason", None) if candidates else None
        transport.last_metadata = {
            "gemini_finish_reason": str(finish_reason or "unknown"),
            "transport_base_url": base_url or "google_official",
            "native_response_schema_attached": (
                wants_json and response_json_schema is not None
            ),
        }
        return response

    transport.last_metadata = {}
    return transport


class GeminiRegistryExecutor:
    def __init__(
        self,
        *,
        run_config: RunConfigV2,
        run_root: Path,
        credentials: Mapping[str, str],
        commitments: Mapping[str, str],
        prior_usage_by_bucket: Mapping[str, int] | None = None,
        prior_calls_by_gate: Mapping[str, int] | None = None,
        client_factory: Callable[..., JudgeClient] | None = None,
    ) -> None:
        configured_buckets = {
            str(gate["quota_bucket_id"]) for gate in run_config.quota_gates.values()
        }
        if configured_buckets != set(credentials) or configured_buckets != set(commitments):
            raise PhaseCError("Gemini credentials must exact-cover configured physical buckets")
        self.config = run_config
        self.run_root = Path(run_root)
        self._credentials = dict(credentials)
        self._commitments = dict(commitments)
        self._usage = {str(key): int(value) for key, value in (prior_usage_by_bucket or {}).items()}
        self._calls = {str(key): int(value) for key, value in (prior_calls_by_gate or {}).items()}
        self._last_call: dict[str, float] = {}
        self._next_gate = {"b0": 0, "b1": 0, "auditor": 0}
        self._clients: dict[tuple[str, str], JudgeClient] = {}
        self._transports: dict[tuple[str, str], Callable[..., Any]] = {}
        self._client_factory = client_factory or JudgeClient

    @property
    def public_manifest(self) -> Mapping[str, Any]:
        return {
            "executor": "google_genai_generate_content_v3",
            "credential_commitments": dict(sorted(self._commitments.items())),
            "thinking_budget": 0,
            "response_mime_type": "application/json",
            "response_json_schema_sha256_by_role": {
                role: canonical_hash(schema)
                for role, schema in sorted(GEMINI_RESPONSE_SCHEMAS.items())
            },
            "operational_quota_policy": {
                "quota_source": "Google AI Studio limits captured by model and physical key",
                "per_gate": {
                    gate_id: {
                        key: gate[key]
                        for key in ("quota_bucket_id", "model_id", "rpm", "tpm", "rpd", "internal_utc_day_token_cap")
                    }
                    for gate_id, gate in sorted(self.config.quota_gates.items())
                },
            },
        }

    def _role_contract(self, role: str) -> dict[str, Any]:
        return {
            "model": getattr(self.config, f"{role}_model_id"),
            "temperature": getattr(self.config, f"{role}_temperature"),
            "seed": getattr(self.config, f"{role}_seed"),
            "max_output_tokens": getattr(self.config, f"{role}_output_cap"),
            "prompt_token_cap": (
                self.config.auditor_input_token_cap
                if role == "auditor"
                else getattr(self.config, f"{role}_input_cap")
            ),
        }

    def _client(self, role: str, gate_id: str) -> JudgeClient:
        key = (role, gate_id)
        cached = self._clients.get(key)
        if cached is not None:
            return cached
        gate = self.config.quota_gates[gate_id]
        bucket_id = str(gate["quota_bucket_id"])
        contract = self._role_contract(role)
        pricing = self.config.pricing_usd_per_million[role]
        config = LLMConfig(
            model=str(contract["model"]),
            temperature=float(contract["temperature"]),
            seed=int(contract["seed"]),
            reasoning_effort="none",
            verbosity=None,
            max_output_tokens=int(contract["max_output_tokens"]),
            daily_token_cap=int(gate["internal_utc_day_token_cap"]),
            prompt_token_cap=int(contract["prompt_token_cap"]),
            pricing={
                name: float(value) if value is not None else 0.0
                for name, value in pricing.items()
            },
        )
        transport = _gemini_transport(
            api_key=self._credentials[bucket_id],
            response_json_schema=GEMINI_RESPONSE_SCHEMAS[role],
        )
        client = self._client_factory(
            config,
            self.run_root / "cache" / bucket_id / f"{role}.sqlite3",
            transport=transport,
            max_retries=0,
        )
        self._transports[key] = transport
        self._clients[key] = client
        return client

    def _ordered_gate_ids(self, role: str) -> list[str]:
        gate_ids = list(self.config.role_quota_gate_ids[role])
        start = self._next_gate[role] % len(gate_ids)
        return gate_ids[start:] + gate_ids[:start]

    def execute(self, request: Any) -> ExecutedRegistryCall:
        role = str(request.role)
        if role not in {"b0", "b1", "auditor"}:
            raise PhaseCError(f"unsupported registry role: {role}")
        contract = self._role_contract(role)
        prompt_tokens = estimate_registry_prompt_tokens(request.messages)
        if prompt_tokens > int(contract["prompt_token_cap"]):
            raise RegistryBudgetError(f"{role} prompt exceeds configured input cap")
        output_cap = int(contract["max_output_tokens"])
        selected_gate: str | None = None
        for gate_id in self._ordered_gate_ids(role):
            gate = self.config.quota_gates[gate_id]
            bucket_id = str(gate["quota_bucket_id"])
            projected = self._usage.get(bucket_id, 0) + prompt_tokens + output_cap
            if projected <= int(gate["internal_utc_day_token_cap"]) and self._calls.get(gate_id, 0) < int(gate["rpd"]):
                selected_gate = gate_id
                break
        if selected_gate is None:
            raise RegistryBudgetError(f"no Gemini quota bucket can reserve {role} request")
        gate = self.config.quota_gates[selected_gate]
        bucket_id = str(gate["quota_bucket_id"])
        interval = 60.0 / int(gate["rpm"])
        previous = self._last_call.get(selected_gate)
        if previous is not None:
            remaining = interval - (time.monotonic() - previous)
            if remaining > 0:
                time.sleep(remaining)
        judged = self._client(role, selected_gate).call(
            [dict(row) for row in request.messages],
            response_format=RESPONSE_FORMAT_JSON,
            tag=f"registry_v2:{role}:{request.chapter_id}:{request.window_id or 'chapter'}",
        )
        transport = self._transports[(role, selected_gate)]
        transport_metadata = (
            {}
            if judged.from_cache
            else dict(getattr(transport, "last_metadata", {}) or {})
        )
        self._last_call[selected_gate] = time.monotonic()
        usage = judged.usage
        if not judged.from_cache:
            actual = usage.prompt_tokens + usage.completion_tokens + usage.reasoning_tokens
            self._usage[bucket_id] = self._usage.get(bucket_id, 0) + actual
            self._calls[selected_gate] = self._calls.get(selected_gate, 0) + 1
        role_gate_ids = list(self.config.role_quota_gate_ids[role])
        self._next_gate[role] = (role_gate_ids.index(selected_gate) + 1) % len(role_gate_ids)
        result = LLMResult(
            text=judged.text,
            parsed_json=judged.parsed_json,
            json_error=judged.json_error,
            model=judged.model,
            system_fingerprint=None,
            usage=usage,
            cost_usd=judged.cost_usd,
            latency_ms=judged.latency_ms,
            from_cache=judged.from_cache,
            cache_key=judged.cache_key,
        )
        return ExecutedRegistryCall(
            result=result,
            quota_gate_id=selected_gate,
            quota_bucket_id=bucket_id,
            credential_commitment=self._commitments[bucket_id],
            safe_response_headers=transport_metadata,
            completed_at=_now(),
        )


def prior_gemini_quota_state(run_root: Path) -> tuple[dict[str, int], dict[str, int]]:
    usage_by_bucket: dict[str, int] = {}
    calls_by_gate: dict[str, int] = {}
    for path in sorted((Path(run_root) / "calls").glob("*/attempt_01/raw_result.json")):
        raw = _read_json(path)
        if raw.get("from_cache"):
            continue
        usage = raw.get("usage") or {}
        bucket_id = str(raw.get("quota_bucket_id") or "")
        gate_id = str(raw.get("quota_gate_id") or "")
        tokens = sum(int(usage.get(name) or 0) for name in ("prompt_tokens", "completion_tokens", "reasoning_tokens"))
        usage_by_bucket[bucket_id] = usage_by_bucket.get(bucket_id, 0) + tokens
        calls_by_gate[gate_id] = calls_by_gate.get(gate_id, 0) + 1
    return usage_by_bucket, calls_by_gate


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run Gemini M4f B0/B1 registry v2 canary")
    parser.add_argument("--document", type=Path, default=DEFAULT_DOCUMENT)
    parser.add_argument("--design-doc", type=Path, default=DEFAULT_DESIGN_DOC)
    parser.add_argument("--run-config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--keys-file", type=Path, default=DEFAULT_KEYS_FILE)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--frozen-db", type=Path, default=DEFAULT_FROZEN_DB)
    parser.add_argument("--through", choices=CHAPTER_IDS, required=True)
    parser.add_argument("--confirm-config-hash", required=True)
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--execute", action="store_true")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    if not args.execute:
        raise SystemExit("Refusing real Gemini API execution without --execute")
    config, _ = _load_config(args.run_config)
    if args.confirm_config_hash != config.config_hash:
        raise SystemExit("--confirm-config-hash does not match the content-addressed RunConfig")
    credentials, commitments = load_gemini_credentials(args.keys_file)
    prior_usage, prior_calls = (
        prior_gemini_quota_state(args.output_dir) if args.resume else ({}, {})
    )
    executor = GeminiRegistryExecutor(
        run_config=config,
        run_root=args.output_dir,
        credentials=credentials,
        commitments=commitments,
        prior_usage_by_bucket=prior_usage,
        prior_calls_by_gate=prior_calls,
    )
    try:
        summary = run_phase_c(
            document_path=args.document,
            design_doc=args.design_doc,
            config_path=args.run_config,
            output_dir=args.output_dir,
            frozen_db=args.frozen_db,
            through_chapter=args.through,
            executor=executor,
            resume=args.resume,
        )
    except Exception as exc:
        _record_halt_report(args.output_dir, exc, args.frozen_db)
        raise
    from pipeline.literary.checkpoint import canonical_json

    print(canonical_json(summary))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
