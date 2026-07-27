from __future__ import annotations

import argparse
from dataclasses import asdict
from datetime import UTC, datetime
import json
from pathlib import Path
from typing import Any, Mapping, Sequence

from pipeline.agents.judge_client import JudgeClient
from pipeline.agents.llm_config import LLMConfig
from pipeline.agents.provider_profile import ResolvedCredential
from pipeline.literary.b0_entity_inventory_auditor_experiment import (
    entity_inventory_auditor_response_schema,
    render_inventory_auditor_request,
    validate_and_apply_auditor_response,
)
from pipeline.literary.b0_entity_inventory_experiment import (
    entity_inventory_response_schema,
    evaluate_inventory_against_gold,
    render_entity_inventory_request,
    validate_entity_inventory_response,
)
from pipeline.literary.checkpoint import canonical_hash, canonical_json, file_sha256
from pipeline.literary.openai_compatible_structured_call_v1 import (
    call_openai_compatible_structured_v1,
)
from pipeline.literary.transport_json import (
    LiteraryTransportJsonError,
    parse_structured_response,
)
from pipeline.literary.structured_prompt_reserve_v1 import (
    structured_prompt_reserve_v1,
)
from pipeline.literary.structured_output_policy_v1 import (
    StructuredOutputContract,
    gemini_response_json_schema,
    validate_structured_payload,
)
from pipeline.scripts.run_chapter_registry_v2_gemini import (
    BUCKET_IDS,
    _gemini_transport,
    load_gemini_credentials,
)
from pipeline.scripts.run_chapter_registry_v2_real import RESPONSE_FORMAT_JSON
from pipeline.scripts.run_chapter_registry_v4_real import (
    DEFAULT_CHAPTER_ID,
    DEFAULT_DESIGN_DOC,
    DEFAULT_DOCUMENT,
    DEFAULT_FROZEN_DB,
    RUNTIME_ROOT,
    _ensure_empty_output,
    _git_head,
    _load_document,
    _redacted_error,
    _verify_frozen_db,
    _write_json,
)


MODEL_ID = "gemini-3.5-flash"
RUN_SCHEMA_VERSION = "b0_inventory_gemini_comparison_run_v1"
DEFAULT_GOLD = RUNTIME_ROOT / "data" / "eval" / "literary_m4f" / "wh_ch01_registry_gold_v1.json"
DEFAULT_INVENTORY = (
    RUNTIME_ROOT
    / "data"
    / "reports"
    / "literary_m4f_b0_entity_inventory_exp_v1_2_real_20260715_1_openai"
    / "inventory.json"
)
STAGE_CAPS = {
    "b0": {"input": 20_000, "output": 4_096},
    "auditor": {"input": 22_000, "output": 8_192},
}
RPM = 5
TPM = 250_000
RPD = 20
INTERNAL_UTC_DAY_TOKEN_CAP = 225_000


class GeminiComparisonError(RuntimeError):
    pass


def _now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()


def _load_json(path: Path, label: str) -> dict[str, Any]:
    value = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise GeminiComparisonError(f"{label} must be a JSON object")
    return value


def _request_and_context(
    *,
    stage: str,
    document_path: Path,
    design_doc: Path,
    chapter_id: str,
    inventory_path: Path | None,
    model_id: str = MODEL_ID,
) -> tuple[Any, Mapping[str, Any], Mapping[str, Any] | None, Mapping[str, Any]]:
    _document, chapter = _load_document(document_path, chapter_id)
    cap = STAGE_CAPS[stage]
    if stage == "b0":
        request = render_entity_inventory_request(
            chapter=chapter,
            design_doc=design_doc,
            model_id=model_id,
            reasoning_effort="none",
            temperature=1.0,
            seed=20260715,
            max_output_tokens=cap["output"],
        )
        return request, chapter, None, entity_inventory_response_schema()
    if inventory_path is None:
        raise GeminiComparisonError("auditor stage requires --inventory")
    inventory = _load_json(inventory_path, "source inventory")
    request = render_inventory_auditor_request(
        chapter=chapter,
        inventory=inventory,
        design_doc=design_doc,
        model_id=model_id,
        reasoning_effort="none",
        temperature=1.0,
        seed=20260715,
        max_output_tokens=cap["output"],
    )
    return request, chapter, inventory, entity_inventory_auditor_response_schema()


def build_envelope(
    *,
    stage: str,
    document_path: Path,
    design_doc: Path,
    chapter_id: str,
    inventory_path: Path | None,
    model_id: str = MODEL_ID,
    provider_id: str = "google_genai",
    structured_output_contract: StructuredOutputContract | None = None,
) -> tuple[dict[str, Any], Any, Mapping[str, Any], Mapping[str, Any] | None, Mapping[str, Any]]:
    if provider_id not in {"google_genai", "openai"}:
        raise GeminiComparisonError("unsupported B1 provider")
    request, chapter, inventory, response_schema = _request_and_context(
        stage=stage,
        document_path=document_path,
        design_doc=design_doc,
        chapter_id=chapter_id,
        inventory_path=inventory_path,
        model_id=model_id,
    )
    caps = STAGE_CAPS[stage]
    reserve = structured_prompt_reserve_v1(
        messages=request.messages,
        response_schema=response_schema,
        output_token_cap=caps["output"],
        include_schema_transport_overhead=(
            structured_output_contract is None
            or structured_output_contract.native_enforcement
        ),
    )
    if reserve.prompt_token_reserve > caps["input"]:
        raise GeminiComparisonError(
            f"{stage} input reserve {reserve.prompt_token_reserve} "
            f"exceeds cap {caps['input']}"
        )
    if (
        structured_output_contract is not None
        and structured_output_contract.canonical_schema_hash
        != canonical_hash(response_schema)
    ):
        raise GeminiComparisonError(
            "structured-output contract differs from the B1 response schema"
        )
    body = {
        "schema_version": RUN_SCHEMA_VERSION,
        "provider": provider_id,
        "stage": stage,
        "chapter_id": chapter_id,
        "model_contract": {
            "model_id": model_id,
            "temperature": 1.0,
            "seed": 20260715,
            "thinking_budget": 0,
            "max_output_tokens": caps["output"],
            "input_token_cap": caps["input"],
        },
        "quota_contract": (
            {
                "rpm": RPM,
                "tpm": TPM,
                "rpd_per_physical_key": RPD,
                "internal_utc_day_token_cap_per_physical_key": (
                    INTERNAL_UTC_DAY_TOKEN_CAP
                ),
            }
            if provider_id == "google_genai"
            else {
                "quota_scope": "sealed_run_and_stage_caps",
                "provider_fallback_allowed": False,
                "max_calls_for_this_stage": 1,
            }
        ),
        "document_sha256": file_sha256(document_path),
        "design_doc_sha256": file_sha256(design_doc),
        "source_inventory_sha256": (
            file_sha256(inventory_path) if inventory_path is not None else None
        ),
        "source_inventory_hash": inventory.get("inventory_hash") if inventory else None,
        "prompt_id": request.prompt_id,
        "prompt_sha256": request.prompt_sha256,
        "response_schema_hash": canonical_hash(response_schema),
        "structured_output_contract": (
            structured_output_contract.to_payload()
            if structured_output_contract is not None
            else {
                "schema_version": "literary_structured_output_legacy_v1",
                "effective_mode": "legacy_native_schema_unsealed",
                "local_validation_required": True,
            }
        ),
        "request_fingerprint": request.request_fingerprint,
        "estimated_prompt_tokens": reserve.message_token_estimate,
        "response_schema_utf8_bytes": reserve.response_schema_utf8_bytes,
        "transport_prompt_token_reserve": reserve.prompt_token_reserve,
        "reserved_tokens": reserve.total_token_reserve,
        "git_head": _git_head(),
        "gold_access_policy": "POST_RESPONSE_ARTIFACT_ONLY_NOT_IN_REQUEST",
        "production_publish_performed": False,
    }
    return (
        {**body, "envelope_hash": canonical_hash(body)},
        request,
        chapter,
        inventory,
        response_schema,
    )


def scan_current_utc_gemini_usage(
    *,
    roots: Sequence[Path],
    allowed_bucket_ids: Sequence[str] | None = None,
) -> dict[str, Any]:
    utc_date = datetime.now(UTC).date().isoformat()
    allowed = set(allowed_bucket_ids or BUCKET_IDS)
    usage_by_bucket: dict[str, int] = {}
    calls_by_bucket_model: dict[str, int] = {}
    unknown: list[dict[str, str]] = []
    seen: set[tuple[str, str, str]] = set()
    for root in sorted({Path(row).resolve() for row in roots if Path(row).exists()}):
        for path in root.rglob("raw_result.json"):
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError, TypeError):
                continue
            model = str(raw.get("model") or "")
            completed = str(raw.get("completed_at") or "")
            if not model.startswith("gemini-") or completed[:10] != utc_date:
                continue
            if bool(raw.get("from_cache")):
                continue
            bucket = str(raw.get("quota_bucket_id") or "")
            identity = (
                bucket,
                model,
                str(raw.get("cache_key") or path.resolve()),
            )
            if identity in seen:
                continue
            seen.add(identity)
            if bucket not in allowed:
                unknown.append({"path": str(path.resolve()), "reason": "unknown bucket"})
                continue
            usage = raw.get("usage") or {}
            tokens = sum(
                int(usage.get(name) or 0)
                for name in ("prompt_tokens", "completion_tokens", "reasoning_tokens")
            )
            usage_by_bucket[bucket] = usage_by_bucket.get(bucket, 0) + tokens
            key = f"{bucket}|{model}"
            calls_by_bucket_model[key] = calls_by_bucket_model.get(key, 0) + 1
    return {
        "schema_version": "gemini_physical_bucket_usage_v1",
        "utc_date": utc_date,
        "usage_by_bucket": dict(sorted(usage_by_bucket.items())),
        "calls_by_bucket_model": dict(sorted(calls_by_bucket_model.items())),
        "unknown_bucket_rows": unknown,
    }


def run_dry(
    *,
    stage: str,
    document_path: Path,
    design_doc: Path,
    chapter_id: str,
    inventory_path: Path | None,
    output_dir: Path,
    model_id: str = MODEL_ID,
    provider_id: str = "google_genai",
    structured_output_contract: StructuredOutputContract | None = None,
) -> dict[str, Any]:
    output = _ensure_empty_output(output_dir)
    envelope, request, _chapter, _inventory, _schema = build_envelope(
        stage=stage,
        document_path=document_path,
        design_doc=design_doc,
        chapter_id=chapter_id,
        inventory_path=inventory_path,
        model_id=model_id,
        provider_id=provider_id,
        structured_output_contract=structured_output_contract,
    )
    report = {
        "schema_version": "b0_inventory_gemini_comparison_dry_v1",
        "status": "dry_rendered_no_api",
        "stage": stage,
        "model_id": model_id,
        "envelope_hash": envelope["envelope_hash"],
        "request_fingerprint": request.request_fingerprint,
        "estimated_prompt_tokens": envelope["estimated_prompt_tokens"],
        "response_schema_utf8_bytes": envelope["response_schema_utf8_bytes"],
        "transport_prompt_token_reserve": envelope[
            "transport_prompt_token_reserve"
        ],
        "reserved_tokens": envelope["reserved_tokens"],
        "structured_output_contract": envelope["structured_output_contract"],
    }
    _write_json(output / f"run_envelope_{envelope['envelope_hash'][:12]}.json", envelope)
    _write_json(output / "request.json", request.to_dict())
    _write_json(output / "dry_report.json", report)
    return report


def run_live(
    *,
    stage: str,
    document_path: Path,
    design_doc: Path,
    frozen_db: Path,
    chapter_id: str,
    inventory_path: Path | None,
    output_dir: Path,
    approved_envelope_hash: str,
    keys_file: Path | None,
    quota_bucket_id: str,
    usage_roots: Sequence[Path],
    gold_path: Path | None,
    resolved_credential: ResolvedCredential | None = None,
    allowed_quota_bucket_ids: Sequence[str] | None = None,
    provider_profile_hash: str | None = None,
    model_id: str = MODEL_ID,
    structured_output_contract: StructuredOutputContract | None = None,
) -> dict[str, Any]:
    output = _ensure_empty_output(output_dir)
    frozen_hash = _verify_frozen_db(frozen_db)
    provider = (
        resolved_credential.provider
        if resolved_credential is not None
        else "google_genai"
    )
    envelope, request, chapter, inventory, response_schema = build_envelope(
        stage=stage,
        document_path=document_path,
        design_doc=design_doc,
        chapter_id=chapter_id,
        inventory_path=inventory_path,
        model_id=model_id,
        provider_id=provider,
        structured_output_contract=structured_output_contract,
    )
    if approved_envelope_hash != envelope["envelope_hash"]:
        raise GeminiComparisonError(
            f"approved envelope mismatch; current hash is {envelope['envelope_hash']}"
        )
    if resolved_credential is not None:
        if resolved_credential.quota_bucket_id != quota_bucket_id:
            raise GeminiComparisonError("resolved credential bucket does not match")
        if resolved_credential.provider not in {"google_genai", "openai"}:
            raise GeminiComparisonError("resolved credential provider is unsupported")
        provider = resolved_credential.provider
        credentials = {quota_bucket_id: resolved_credential.secret}
        commitments = {quota_bucket_id: resolved_credential.commitment}
        credential_revision = resolved_credential.credential_revision
    else:
        if keys_file is None:
            raise GeminiComparisonError("Gemini key source is required")
        credentials, commitments = load_gemini_credentials(keys_file)
        credential_revision = quota_bucket_id
    if quota_bucket_id not in credentials:
        raise GeminiComparisonError("requested quota bucket is not configured")
    quota = scan_current_utc_gemini_usage(
        roots=usage_roots,
        allowed_bucket_ids=allowed_quota_bucket_ids,
    )
    if quota["unknown_bucket_rows"]:
        raise GeminiComparisonError("quota scan found unknown physical buckets")
    if provider == "google_genai":
        prior_tokens = int(quota["usage_by_bucket"].get(quota_bucket_id, 0))
        prior_calls = int(
            quota["calls_by_bucket_model"].get(f"{quota_bucket_id}|{model_id}", 0)
        )
        if prior_tokens + int(envelope["reserved_tokens"]) > INTERNAL_UTC_DAY_TOKEN_CAP:
            raise GeminiComparisonError(
                "internal Gemini UTC-day token gate would be exceeded"
            )
        if prior_calls >= RPD:
            raise GeminiComparisonError("Gemini per-key RPD gate would be exceeded")
        if int(envelope["reserved_tokens"]) > TPM:
            raise GeminiComparisonError("Gemini per-request TPM reserve is too large")

    _write_json(output / f"run_envelope_{envelope['envelope_hash'][:12]}.json", envelope)
    _write_json(output / "request.json", request.to_dict())
    _write_json(output / "quota_preflight.json", quota)
    manifest = {
        "schema_version": RUN_SCHEMA_VERSION,
        "status": "running",
        "stage": stage,
        "chapter_id": chapter_id,
        "model_id": model_id,
        "provider": provider,
        "quota_bucket_id": quota_bucket_id,
        "credential_revision": credential_revision,
        "credential_commitment": commitments[quota_bucket_id],
        "provider_profile_hash": provider_profile_hash,
        "structured_output_contract": envelope["structured_output_contract"],
        "frozen_db_sha256_before": frozen_hash,
        "gold_loaded_before_response": False,
        "production_publish_performed": False,
        "started_at": _now(),
    }
    _write_json(output / "run_manifest.json", manifest)

    caps = STAGE_CAPS[stage]
    observed_usage: dict[str, int] | None = None
    observed_finish_reason: str | None = None
    try:
        if provider == "openai":
            if resolved_credential is None or structured_output_contract is None:
                raise GeminiComparisonError(
                    "OpenAI-compatible B1 requires a resolved credential and structured output"
                )
            direct = call_openai_compatible_structured_v1(
                credential=resolved_credential,
                model_id=model_id,
                messages=request.messages,
                contract=structured_output_contract,
                schema_name=f"literary_registry_{stage}_v1",
                cache_path=(
                    output / "cache" / quota_bucket_id / f"{stage}.sqlite3"
                ),
                tag=f"literary_registry:{stage}:{chapter_id}",
                prompt_token_cap=caps["input"],
                max_output_tokens=caps["output"],
                temperature=1.0,
                seed=20260715,
                reasoning_effort="none",
            )
            parsed_json = direct.parsed_json
            transport_normalization = "openai_strict_json_schema"
            parse_error = None
            usage = dict(direct.usage)
            observed_finish_reason = "openai_strict_json_schema"
            raw_model = direct.model
            response_text = direct.response_text
            strict_json_error = direct.json_error
            safe_transport_metadata: Mapping[str, Any] = {}
            thinking_budget: int | None = None
            latency_ms = direct.latency_ms
            cost_usd = direct.cost_usd
            from_cache = direct.from_cache
            cache_key = direct.cache_key
        else:
            transport = _gemini_transport(
                api_key=credentials[quota_bucket_id],
                response_json_schema=(
                    response_schema
                    if structured_output_contract is None
                    else gemini_response_json_schema(structured_output_contract)
                ),
                timeout_ms=(
                    resolved_credential.request_timeout_ms
                    if resolved_credential is not None
                    else 120_000
                ),
                base_url=(
                    resolved_credential.base_url if resolved_credential else None
                ),
            )
            client = JudgeClient(
                LLMConfig(
                    model=model_id,
                    temperature=1.0,
                    seed=20260715,
                    reasoning_effort="none",
                    verbosity=None,
                    max_output_tokens=caps["output"],
                    daily_token_cap=INTERNAL_UTC_DAY_TOKEN_CAP,
                    prompt_token_cap=caps["input"],
                    pricing={"input": 0.0, "cached_input": 0.0, "output": 0.0},
                ),
                output / "cache" / quota_bucket_id / f"{stage}.sqlite3",
                transport=transport,
                max_retries=0,
            )
            judged = client.call(
                [dict(row) for row in request.messages],
                response_format=RESPONSE_FORMAT_JSON,
                tag=f"b0_inventory_gemini:{stage}:{chapter_id}",
                bypass_cache=True,
            )
            parse_error: LiteraryTransportJsonError | None = None
            try:
                parsed_json, transport_normalization = parse_structured_response(
                    judged.text
                )
            except LiteraryTransportJsonError as exc:
                parsed_json = None
                transport_normalization = "rejected"
                parse_error = exc
            usage = asdict(judged.usage)
            observed_finish_reason = str(
                transport.last_metadata.get("gemini_finish_reason") or "unknown"
            )
            raw_model = judged.model
            response_text = judged.text
            strict_json_error = judged.json_error
            safe_transport_metadata = dict(transport.last_metadata)
            thinking_budget = 0
            latency_ms = judged.latency_ms
            cost_usd = judged.cost_usd
            from_cache = judged.from_cache
            cache_key = judged.cache_key
        observed_usage = dict(usage)
        raw = {
            "schema_version": "b0_inventory_gemini_raw_v1",
            "stage": stage,
            "chapter_id": chapter_id,
            "model": raw_model,
            "provider": provider,
            "quota_bucket_id": quota_bucket_id,
            "credential_revision": credential_revision,
            "credential_commitment": commitments[quota_bucket_id],
            "completed_at": _now(),
            "from_cache": from_cache,
            "cache_key": cache_key,
            "response_text": response_text,
            "parsed_json": parsed_json,
            "json_error": None,
            "strict_json_error": strict_json_error,
            "transport_normalization": transport_normalization,
            "usage": usage,
            "latency_ms": latency_ms,
            "cost_usd": cost_usd,
            "safe_transport_metadata": dict(safe_transport_metadata),
            "thinking_budget": thinking_budget,
            "structured_output_contract": envelope["structured_output_contract"],
        }
        _write_json(output / "raw_result.json", raw)
        if parse_error is not None:
            raise GeminiComparisonError(str(parse_error)) from parse_error
        validate_structured_payload(
            parsed_json,
            canonical_schema=response_schema,
        )
        if stage == "b0":
            result = validate_entity_inventory_response(
                parsed_json,
                chapter,
                request_fingerprint=request.request_fingerprint,
            )
            artifact_name = "inventory.json"
        else:
            assert inventory is not None
            result = validate_and_apply_auditor_response(
                parsed_json,
                chapter=chapter,
                inventory=inventory,
                request_fingerprint=request.request_fingerprint,
            )
            artifact_name = "audited_inventory.json"
        _write_json(output / artifact_name, result)

        evaluation = None
        if gold_path is not None:
            gold = _load_json(gold_path, "gold evaluation file")
            evaluation = evaluate_inventory_against_gold(result, gold)
            _write_json(output / "post_response_gold_evaluation.json", evaluation)
        report = {
            "schema_version": "b0_inventory_gemini_comparison_live_report_v1",
            "status": "accepted_one_call_comparison",
            "stage": stage,
            "chapter_id": chapter_id,
            "model_id": model_id,
            "quota_bucket_id": quota_bucket_id,
            "envelope_hash": envelope["envelope_hash"],
            "result_hash": result.get("inventory_hash") or result.get("audited_inventory_hash"),
            "entity_count": len(result.get("entity_candidates") or []),
            "pending_entity_count": len(result.get("pending_entity_candidates") or []),
            "glossary_count": len(result.get("glossary_candidates") or []),
            "unresolved_count": len(result.get("unresolved_referents") or []),
            "confirmed_entity_recall": (
                evaluation.get("confirmed_entity_recall") if evaluation else None
            ),
            "glossary_recall": evaluation.get("glossary_recall") if evaluation else None,
            "wrong_merge_count": evaluation.get("wrong_merge_count") if evaluation else None,
            "wrong_split_count": evaluation.get("wrong_split_count") if evaluation else None,
            "wrong_kind_count": evaluation.get("wrong_kind_count") if evaluation else None,
            "unmatched_entity_candidate_count": (
                evaluation.get("unmatched_entity_candidate_count") if evaluation else None
            ),
            "usage": usage,
            "finish_reason": observed_finish_reason,
            "transport_normalization": transport_normalization,
            "thinking_budget": thinking_budget,
            "structured_output_contract": envelope["structured_output_contract"],
            "gold_loaded_only_after_result_persisted": gold_path is not None,
            "production_publish_performed": False,
            "frozen_db_sha256_after": _verify_frozen_db(frozen_db),
            "completed_at": _now(),
        }
        _write_json(output / "experiment_report.json", report)
        _write_json(
            output / "run_manifest.json",
            {**manifest, "status": "accepted", "completed_at": report["completed_at"]},
        )
        return report
    except Exception as exc:
        failure = {
            "schema_version": "b0_inventory_gemini_comparison_failure_v1",
            "status": "halted_fail_closed",
            "stage": stage,
            "chapter_id": chapter_id,
            "model_id": model_id,
            "quota_bucket_id": quota_bucket_id,
            "error_type": type(exc).__name__,
            "message": _redacted_error(exc),
            "usage": observed_usage,
            "finish_reason": observed_finish_reason,
            "production_publish_performed": False,
            "frozen_db_sha256_after": _verify_frozen_db(frozen_db),
            "failed_at": _now(),
        }
        _write_json(output / "experiment_failure.json", failure)
        _write_json(
            output / "run_manifest.json",
            {**manifest, "status": "halted", "failed_at": failure["failed_at"]},
        )
        raise


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Compare B0 inventory stages on Gemini")
    parser.add_argument("mode", choices=("dry", "live"))
    parser.add_argument("--stage", choices=("b0", "auditor"), required=True)
    parser.add_argument("--document", type=Path, default=DEFAULT_DOCUMENT)
    parser.add_argument("--design-doc", type=Path, default=DEFAULT_DESIGN_DOC)
    parser.add_argument("--frozen-db", type=Path, default=DEFAULT_FROZEN_DB)
    parser.add_argument("--chapter-id", default=DEFAULT_CHAPTER_ID)
    parser.add_argument("--inventory", type=Path)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--approved-envelope-hash")
    parser.add_argument("--keys-file", type=Path)
    parser.add_argument("--quota-bucket-id", choices=BUCKET_IDS)
    parser.add_argument("--usage-root", type=Path, action="append", default=[])
    parser.add_argument("--gold", type=Path, default=DEFAULT_GOLD)
    parser.add_argument("--no-gold-eval", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.stage == "auditor" and args.inventory is None:
        raise GeminiComparisonError("auditor stage requires --inventory")
    if args.mode == "dry":
        report = run_dry(
            stage=args.stage,
            document_path=args.document,
            design_doc=args.design_doc,
            chapter_id=args.chapter_id,
            inventory_path=args.inventory,
            output_dir=args.output_dir,
        )
    else:
        if not args.approved_envelope_hash or not args.keys_file or not args.quota_bucket_id:
            raise GeminiComparisonError(
                "live mode requires approved envelope, keys file, and quota bucket"
            )
        report = run_live(
            stage=args.stage,
            document_path=args.document,
            design_doc=args.design_doc,
            frozen_db=args.frozen_db,
            chapter_id=args.chapter_id,
            inventory_path=args.inventory,
            output_dir=args.output_dir,
            approved_envelope_hash=args.approved_envelope_hash,
            keys_file=args.keys_file,
            quota_bucket_id=args.quota_bucket_id,
            usage_roots=args.usage_root or [RUNTIME_ROOT / "data"],
            gold_path=None if args.no_gold_eval else args.gold,
        )
    print(canonical_json(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
