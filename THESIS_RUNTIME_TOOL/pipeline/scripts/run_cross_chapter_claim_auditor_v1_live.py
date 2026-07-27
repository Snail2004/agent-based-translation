from __future__ import annotations

import argparse
from dataclasses import asdict
from datetime import UTC, datetime
from hashlib import sha256
import json
from pathlib import Path
import subprocess
import sys
from typing import Any, Mapping, Sequence


RUNTIME_ROOT = Path(__file__).resolve().parents[2]
if str(RUNTIME_ROOT) not in sys.path:
    sys.path.insert(0, str(RUNTIME_ROOT))

from pipeline.agents.llm_client import LLMClient, estimate_prompt_tokens  # noqa: E402
from pipeline.agents.llm_config import LLMConfig  # noqa: E402
from pipeline.literary.book_entity_claim_auditor_v1 import (  # noqa: E402
    BookEntityClaimContractError,
    build_prior_claim_projection_v1,
    build_prior_claim_revision_ledger_v1,
    validate_prior_claim_response_v1,
    verify_prior_claim_ticket_index_v1,
)
from pipeline.literary.checkpoint import (  # noqa: E402
    canonical_hash,
    canonical_json,
    file_sha256,
    write_checkpoint_atomic,
)
from pipeline.literary.structured_output_policy_v1 import (  # noqa: E402
    StructuredOutputContract,
    openai_response_format,
    validate_structured_payload,
)
from pipeline.scripts.run_chapter_registry_v4_real import (  # noqa: E402
    FROZEN_DB_SHA256,
    scan_current_utc_usage,
)


MODEL_ID = "gpt-5.4"
MODEL_CONTRACTS = {
    model_id: {
        "model_id": model_id,
        "temperature": 1.0,
        "seed": 20260715,
        "reasoning_effort": "none",
        "verbosity": "low",
        "max_output_tokens": 4096,
        "prompt_token_cap": 10_000,
    }
    for model_id in ("gpt-5.4", "gpt-5.4-mini")
}
MODEL_CONTRACT = MODEL_CONTRACTS[MODEL_ID]
BUCKET_ORDER = ("openai-row2", "openai-row1")
INTERNAL_UTC_DAY_TOKEN_CAPS = {
    "gpt-5.4": 225_000,
    "gpt-5.4-mini": 2_250_000,
}
INTERNAL_UTC_DAY_TOKEN_CAP = INTERNAL_UTC_DAY_TOKEN_CAPS[MODEL_ID]
LOCAL_RPD_CAP = 100


class PriorClaimLiveRunError(RuntimeError):
    pass


def _now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()


def _load_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError) as exc:
        raise PriorClaimLiveRunError(f"cannot read JSON artifact: {path}") from exc


def _write_json(path: Path, payload: Mapping[str, Any]) -> None:
    if path.is_file():
        existing = _load_json(path)
        if canonical_json(existing) != canonical_json(payload):
            raise PriorClaimLiveRunError(f"immutable artifact differs: {path}")
        return
    write_checkpoint_atomic(path, dict(payload))


def _git_head() -> str:
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=RUNTIME_ROOT.parent,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _redacted_error(exc: Exception) -> str:
    text = str(exc)
    if "sk-" in text:
        return "credential material was redacted from the transport error"
    return text[:1000]


def _credential(path: Path) -> tuple[str, str]:
    try:
        value = path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise PriorClaimLiveRunError(f"cannot read credential file: {path}") from exc
    if not value.startswith("sk-") or len(value) < 40:
        raise PriorClaimLiveRunError(f"credential file is not a usable OpenAI key: {path}")
    return value, sha256(value.encode("utf-8")).hexdigest()


def _select_component_id(
    index: Mapping[str, Any], requested_component_id: str | None
) -> str:
    renderable = [
        str(row["component_id"])
        for row in index["claim_components"]
        if not row["overflow"]
    ]
    if requested_component_id is not None:
        if requested_component_id not in renderable:
            raise PriorClaimLiveRunError(
                "requested component is absent, overflowed, or foreign"
            )
        return requested_component_id
    if len(renderable) != 1:
        raise PriorClaimLiveRunError(
            "live run with multiple components requires --component-id"
        )
    return renderable[0]


def _prepared_artifacts(
    prepared_dir: Path, *, component_id: str | None = None
) -> tuple[dict[str, Any], dict[str, Any]]:
    index = verify_prior_claim_ticket_index_v1(
        _load_json(prepared_dir / "ticket_index.json")
    )
    selected_id = _select_component_id(index, component_id)
    request = _load_json(prepared_dir / "components" / selected_id / "request.json")
    if not isinstance(request, dict):
        raise PriorClaimLiveRunError("prepared request must be an object")
    required = {
        "component_id",
        "request_fingerprint",
        "messages",
        "response_schema",
        "semantic_payload",
    }
    if set(request) != required or request.get("component_id") != selected_id:
        raise PriorClaimLiveRunError("prepared request differs from the sealed shape")
    messages = request.get("messages")
    schema = request.get("response_schema")
    if not isinstance(messages, list) or not messages or not isinstance(schema, dict):
        raise PriorClaimLiveRunError("prepared request has invalid messages or schema")
    if canonical_hash(request.get("semantic_payload")) != canonical_hash(
        json.loads(str(messages[-1].get("content") or "{}"))
    ):
        raise PriorClaimLiveRunError("semantic payload differs from the user message")
    return index, request


def _openai_transport_schema(value: Any) -> Any:
    """Project the full validator schema onto OpenAI's supported strict subset."""
    if isinstance(value, Mapping):
        return {
            str(key): _openai_transport_schema(item)
            for key, item in value.items()
            if key not in {"minItems", "minLength", "uniqueItems"}
        }
    if isinstance(value, list):
        return [_openai_transport_schema(item) for item in value]
    return value


def _response_format(schema: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "type": "json_schema",
        "json_schema": {
            "name": "literary_cross_chapter_prior_claim_auditor_v2",
            "strict": True,
            "schema": _openai_transport_schema(schema),
        },
    }


def _resolved_response_format(
    schema: Mapping[str, Any],
    structured_output_contract: StructuredOutputContract | None,
) -> dict[str, Any]:
    if structured_output_contract is None:
        return _response_format(schema)
    if structured_output_contract.canonical_schema_hash != canonical_hash(schema):
        raise PriorClaimLiveRunError(
            "prior-claim Structured Output contract has a foreign schema"
        )
    return openai_response_format(
        structured_output_contract,
        schema_name="literary_cross_chapter_prior_claim_auditor_v2",
    )


def _select_bucket(
    *,
    preflight: Mapping[str, Any],
    reserve_tokens: int,
    model_id: str = MODEL_ID,
    bucket_order: Sequence[str] = BUCKET_ORDER,
) -> tuple[str, int, int]:
    if model_id not in MODEL_CONTRACTS:
        raise PriorClaimLiveRunError(f"unsupported prior-claim Auditor model: {model_id}")
    internal_cap = INTERNAL_UTC_DAY_TOKEN_CAPS[model_id]
    usage = dict(preflight.get("usage_by_bucket_model") or {})
    calls = dict(preflight.get("calls_by_bucket_model") or {})
    if not bucket_order or len(set(bucket_order)) != len(bucket_order):
        raise PriorClaimLiveRunError("invalid prior-claim bucket order")
    for bucket in bucket_order:
        key = f"{bucket}|{model_id}"
        prior_tokens = int(usage.get(key, 0))
        prior_calls = int(calls.get(key, 0))
        if (
            prior_tokens + reserve_tokens <= internal_cap
            and prior_calls < LOCAL_RPD_CAP
        ):
            return bucket, prior_tokens, prior_calls
    raise PriorClaimLiveRunError(
        f"no {model_id} quota bucket can reserve this request"
    )


def build_envelope(
    *,
    prepared_dir: Path,
    frozen_db: Path,
    usage_roots: Sequence[Path],
    exclude_root: Path | None = None,
    component_id: str | None = None,
    model_id: str = MODEL_ID,
    bucket_order: Sequence[str] = BUCKET_ORDER,
    structured_output_contract: StructuredOutputContract | None = None,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], dict[str, Any]]:
    try:
        model_contract = MODEL_CONTRACTS[model_id]
        internal_cap = INTERNAL_UTC_DAY_TOKEN_CAPS[model_id]
    except KeyError as exc:
        raise PriorClaimLiveRunError(
            f"unsupported prior-claim Auditor model: {model_id}"
        ) from exc
    index, request = _prepared_artifacts(prepared_dir, component_id=component_id)
    frozen_hash = file_sha256(frozen_db).upper()
    if frozen_hash != FROZEN_DB_SHA256:
        raise PriorClaimLiveRunError("frozen DB hash differs from the accepted baseline")
    response_format = _resolved_response_format(
        request["response_schema"], structured_output_contract
    )
    prompt_estimate = estimate_prompt_tokens(request["messages"], response_format)
    if prompt_estimate > int(model_contract["prompt_token_cap"]):
        raise PriorClaimLiveRunError("prompt estimate exceeds the sealed cap")
    reserve_tokens = prompt_estimate + int(model_contract["max_output_tokens"])
    preflight = scan_current_utc_usage(
        roots=[Path(row) for row in usage_roots],
        exclude_root=exclude_root,
    )
    if preflight["unknown_bucket_rows"]:
        raise PriorClaimLiveRunError(
            "quota preflight found current-day usage with an unknown bucket"
        )
    bucket, prior_tokens, prior_calls = _select_bucket(
        preflight=preflight,
        reserve_tokens=reserve_tokens,
        model_id=model_id,
        bucket_order=bucket_order,
    )
    body = {
        "schema_version": "cross_chapter_prior_claim_live_envelope_v1",
        "git_head": _git_head(),
        "ticket_index_hash": index["ticket_index_hash"],
        "component_id": request["component_id"],
        "request_fingerprint": request["request_fingerprint"],
        "request_artifact_sha256": file_sha256(
            prepared_dir
            / "components"
            / request["component_id"]
            / "request.json"
        ),
        "model_contract": model_contract,
        "response_format_hash": canonical_hash(response_format),
        "structured_output_contract": (
            structured_output_contract.to_payload()
            if structured_output_contract is not None
            else None
        ),
        "prompt_tokens_estimate": prompt_estimate,
        "reserve_tokens": reserve_tokens,
        "selected_quota_bucket_id": bucket,
        "prior_bucket_tokens": prior_tokens,
        "prior_bucket_calls": prior_calls,
        "internal_utc_day_token_cap": internal_cap,
        "quota_preflight_hash": preflight["preflight_hash"],
        "frozen_db_sha256": frozen_hash,
        "hidden_oracle_in_request": False,
        "production_publish_performed": False,
    }
    envelope = {**body, "envelope_hash": canonical_hash(body)}
    return envelope, preflight, index, request


def run_live(
    *,
    prepared_dir: Path,
    output_dir: Path,
    frozen_db: Path,
    usage_roots: Sequence[Path],
    approved_envelope_hash: str,
    key_paths: Mapping[str, Path],
    component_id: str | None = None,
    model_id: str = MODEL_ID,
    bucket_order: Sequence[str] = BUCKET_ORDER,
    structured_output_contract: StructuredOutputContract | None = None,
) -> dict[str, Any]:
    output_dir = output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    envelope, preflight, index, request = build_envelope(
        prepared_dir=prepared_dir,
        frozen_db=frozen_db,
        usage_roots=usage_roots,
        exclude_root=output_dir,
        component_id=component_id,
        model_id=model_id,
        bucket_order=bucket_order,
        structured_output_contract=structured_output_contract,
    )
    if envelope["envelope_hash"] != approved_envelope_hash:
        raise PriorClaimLiveRunError(
            f"approved envelope mismatch; current hash is {envelope['envelope_hash']}"
        )
    bucket = envelope["selected_quota_bucket_id"]
    if bucket not in key_paths:
        raise PriorClaimLiveRunError("selected quota bucket has no credential path")
    key, commitment = _credential(key_paths[bucket])
    _write_json(output_dir / "run_envelope.json", envelope)
    _write_json(output_dir / "quota_preflight.json", preflight)
    _write_json(output_dir / "request.json", request)
    manifest = {
        "schema_version": "cross_chapter_prior_claim_live_manifest_v1",
        "status": "running",
        "model_id": model_id,
        "component_id": request["component_id"],
        "envelope_hash": envelope["envelope_hash"],
        "quota_bucket_id": bucket,
        "credential_commitment": commitment,
        "frozen_db_sha256_before": file_sha256(frozen_db).upper(),
        "hidden_oracle_loaded": False,
        "production_publish_performed": False,
        "structured_output_contract": envelope["structured_output_contract"],
        "started_at": _now(),
    }
    _write_json(output_dir / "run_manifest.json", manifest)

    try:
        from openai import OpenAI

        transport = OpenAI(api_key=key).chat.completions.create
        model_contract = MODEL_CONTRACTS[model_id]
        config = LLMConfig(
            model=model_id,
            temperature=float(model_contract["temperature"]),
            seed=int(model_contract["seed"]),
            reasoning_effort=str(model_contract["reasoning_effort"]),
            verbosity=str(model_contract["verbosity"]),
            max_output_tokens=int(model_contract["max_output_tokens"]),
            daily_token_cap=INTERNAL_UTC_DAY_TOKEN_CAPS[model_id],
            prompt_token_cap=int(model_contract["prompt_token_cap"]),
            pricing={"input": 0.0, "cached_input": 0.0, "output": 0.0},
        )
        client = LLMClient(
            config,
            output_dir / "cache" / bucket / "prior_claim_auditor.sqlite3",
            transport=transport,
            max_retries=2,
        )
        response_format = _resolved_response_format(
            request["response_schema"], structured_output_contract
        )
        result = client.call(
            [dict(row) for row in request["messages"]],
            response_format=response_format,
            tag=f"prior_claim:{request['component_id']}",
        )
        raw = {
            "schema_version": "cross_chapter_prior_claim_raw_result_v1",
            "component_id": request["component_id"],
            "request_fingerprint": request["request_fingerprint"],
            "model": result.model,
            "quota_bucket_id": bucket,
            "credential_commitment": commitment,
            "text": result.text,
            "parsed_json": result.parsed_json,
            "json_error": result.json_error,
            "usage": asdict(result.usage),
            "cost_usd": result.cost_usd,
            "latency_ms": result.latency_ms,
            "from_cache": result.from_cache,
            "cache_key": result.cache_key,
            "structured_output_contract": envelope["structured_output_contract"],
            "completed_at": _now(),
        }
        _write_json(output_dir / "raw_result.json", raw)
        if not isinstance(result.parsed_json, dict):
            raise PriorClaimLiveRunError(
                f"{model_id} did not return valid structured JSON: {result.json_error}"
            )
        if structured_output_contract is not None:
            validate_structured_payload(
                result.parsed_json,
                canonical_schema=request["response_schema"],
            )
        decision = validate_prior_claim_response_v1(
            result.parsed_json,
            index=index,
            request_fingerprint=request["request_fingerprint"],
        )
        _write_json(output_dir / "decision.json", decision)
        frozen_after = file_sha256(frozen_db).upper()
        if frozen_after != FROZEN_DB_SHA256:
            raise PriorClaimLiveRunError("frozen DB changed during the live call")
        renderable_count = sum(
            not row["overflow"] for row in index["claim_components"]
        )
        ledger = None
        projection = None
        status = "accepted_component_pending_reconciliation"
        if renderable_count == 1:
            ledger = build_prior_claim_revision_ledger_v1(
                index=index,
                decisions=[decision],
            )
            projection = build_prior_claim_projection_v1(
                prior_cards=index["prior_cards"],
                ledger=ledger,
            )
            _write_json(output_dir / "claim_revision_ledger.json", ledger)
            _write_json(output_dir / "claim_projection.json", projection)
            status = "accepted"
        report_body = {
            "schema_version": "cross_chapter_prior_claim_live_report_v1",
            "status": status,
            "component_id": request["component_id"],
            "decision_hash": decision["decision_hash"],
            "ticket_actions": decision["ticket_actions"],
            "claim_ledger_hash": ledger["claim_ledger_hash"] if ledger else None,
            "claim_projection_hash": projection["projection_hash"] if projection else None,
            "usage": asdict(result.usage),
            "latency_ms": result.latency_ms,
            "from_cache": result.from_cache,
            "quota_bucket_id": bucket,
            "frozen_db_sha256_after": frozen_after,
            "hidden_oracle_loaded": False,
            "production_publish_performed": False,
            "completed_at": _now(),
        }
        report = {**report_body, "report_hash": canonical_hash(report_body)}
        _write_json(output_dir / "live_report.json", report)
        _write_json(
            output_dir / "run_manifest_final.json",
            {**manifest, "status": status, "completed_at": report["completed_at"]},
        )
        return report
    except Exception as exc:
        failure = {
            "schema_version": "cross_chapter_prior_claim_live_failure_v1",
            "status": "halted_fail_closed",
            "component_id": request["component_id"],
            "error_type": type(exc).__name__,
            "message": _redacted_error(exc),
            "frozen_db_sha256_after": file_sha256(frozen_db).upper(),
            "production_publish_performed": False,
            "failed_at": _now(),
        }
        _write_json(output_dir / "failure.json", failure)
        raise


def recover_component_result(
    *,
    prepared_dir: Path,
    output_dir: Path,
    frozen_db: Path,
    component_id: str,
) -> dict[str, Any]:
    index, request = _prepared_artifacts(prepared_dir, component_id=component_id)
    raw = _load_json(output_dir / "raw_result.json")
    if not isinstance(raw, Mapping) or raw.get("component_id") != component_id:
        raise PriorClaimLiveRunError("raw result does not belong to the requested component")
    parsed = raw.get("parsed_json")
    if not isinstance(parsed, dict):
        raise PriorClaimLiveRunError("raw result has no valid parsed JSON")
    decision = validate_prior_claim_response_v1(
        parsed,
        index=index,
        request_fingerprint=request["request_fingerprint"],
    )
    _write_json(output_dir / "decision.json", decision)
    frozen_after = file_sha256(frozen_db).upper()
    if frozen_after != FROZEN_DB_SHA256:
        raise PriorClaimLiveRunError("frozen DB changed before recovery")
    report_body = {
        "schema_version": "cross_chapter_prior_claim_live_report_v1",
        "status": "accepted_component_pending_reconciliation",
        "component_id": component_id,
        "decision_hash": decision["decision_hash"],
        "ticket_actions": decision["ticket_actions"],
        "claim_ledger_hash": None,
        "claim_projection_hash": None,
        "usage": raw.get("usage"),
        "latency_ms": raw.get("latency_ms"),
        "from_cache": raw.get("from_cache"),
        "quota_bucket_id": raw.get("quota_bucket_id"),
        "frozen_db_sha256_after": frozen_after,
        "hidden_oracle_loaded": False,
        "production_publish_performed": False,
        "recovered_from_persisted_raw_result": True,
        "completed_at": _now(),
    }
    report = {**report_body, "report_hash": canonical_hash(report_body)}
    _write_json(output_dir / "live_report.json", report)
    _write_json(
        output_dir / "run_manifest_recovered.json",
        {
            "schema_version": "cross_chapter_prior_claim_recovery_manifest_v1",
            "status": report["status"],
            "component_id": component_id,
            "decision_hash": decision["decision_hash"],
            "recovered_at": report["completed_at"],
            "production_publish_performed": False,
        },
    )
    return report


def reconcile_component_results(
    *,
    prepared_dir: Path,
    decision_dirs: Sequence[Path],
    output_dir: Path,
    frozen_db: Path,
) -> dict[str, Any]:
    index = verify_prior_claim_ticket_index_v1(
        _load_json(prepared_dir / "ticket_index.json")
    )
    decisions = [_load_json(path / "decision.json") for path in decision_dirs]
    if not all(isinstance(row, dict) for row in decisions):
        raise PriorClaimLiveRunError("component decision artifact must be an object")
    ledger = build_prior_claim_revision_ledger_v1(
        index=index,
        decisions=decisions,
    )
    projection = build_prior_claim_projection_v1(
        prior_cards=index["prior_cards"],
        ledger=ledger,
    )
    frozen_after = file_sha256(frozen_db).upper()
    if frozen_after != FROZEN_DB_SHA256:
        raise PriorClaimLiveRunError("frozen DB changed before reconciliation")
    output_dir.mkdir(parents=True, exist_ok=False)
    _write_json(output_dir / "claim_revision_ledger.json", ledger)
    _write_json(output_dir / "claim_projection.json", projection)
    body = {
        "schema_version": "cross_chapter_prior_claim_reconciliation_report_v1",
        "status": "reconciled_all_components",
        "ticket_index_hash": index["ticket_index_hash"],
        "decision_hashes": sorted(row["decision_hash"] for row in decisions),
        "claim_ledger_hash": ledger["claim_ledger_hash"],
        "claim_projection_hash": projection["projection_hash"],
        "frozen_db_sha256_after": frozen_after,
        "production_publish_performed": False,
        "completed_at": _now(),
    }
    report = {**body, "report_hash": canonical_hash(body)}
    _write_json(output_dir / "reconciliation_report.json", report)
    return report


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run one bounded prior-claim Auditor component."
    )
    parser.add_argument("mode", choices=("dry", "live", "recover", "reconcile"))
    parser.add_argument("--prepared-dir", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--frozen-db", type=Path, required=True)
    parser.add_argument("--usage-root", type=Path, action="append", default=[])
    parser.add_argument("--approved-envelope-hash")
    parser.add_argument("--component-id")
    parser.add_argument("--decision-dir", type=Path, action="append", default=[])
    parser.add_argument(
        "--model-id",
        choices=tuple(MODEL_CONTRACTS),
        default=MODEL_ID,
    )
    parser.add_argument("--key-1", type=Path)
    parser.add_argument("--key-2", type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    usage_roots = args.usage_root or [RUNTIME_ROOT / "data"]
    if args.mode == "recover":
        if not args.component_id:
            raise PriorClaimLiveRunError("recover mode requires --component-id")
        report = recover_component_result(
            prepared_dir=args.prepared_dir,
            output_dir=args.output_dir,
            frozen_db=args.frozen_db,
            component_id=args.component_id,
        )
        print(canonical_json(report))
        return 0
    if args.mode == "reconcile":
        if not args.decision_dir:
            raise PriorClaimLiveRunError("reconcile mode requires --decision-dir")
        report = reconcile_component_results(
            prepared_dir=args.prepared_dir,
            decision_dirs=args.decision_dir,
            output_dir=args.output_dir,
            frozen_db=args.frozen_db,
        )
        print(canonical_json(report))
        return 0
    if args.mode == "dry":
        args.output_dir.mkdir(parents=True, exist_ok=True)
        envelope, preflight, _, _ = build_envelope(
            prepared_dir=args.prepared_dir,
            frozen_db=args.frozen_db,
            usage_roots=usage_roots,
            exclude_root=args.output_dir,
            component_id=args.component_id,
            model_id=args.model_id,
        )
        _write_json(args.output_dir / "run_envelope.json", envelope)
        _write_json(args.output_dir / "quota_preflight.json", preflight)
        print(canonical_json(envelope))
        return 0
    if not args.approved_envelope_hash or not args.key_1 or not args.key_2:
        raise PriorClaimLiveRunError(
            "live mode requires approved envelope hash and two credential paths"
        )
    report = run_live(
        prepared_dir=args.prepared_dir,
        output_dir=args.output_dir,
        frozen_db=args.frozen_db,
        usage_roots=usage_roots,
        approved_envelope_hash=args.approved_envelope_hash,
        key_paths={"openai-row1": args.key_1, "openai-row2": args.key_2},
        component_id=args.component_id,
        model_id=args.model_id,
    )
    print(canonical_json(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
