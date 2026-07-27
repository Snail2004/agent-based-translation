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
from pipeline.literary.checkpoint import (  # noqa: E402
    canonical_hash,
    canonical_json,
    file_sha256,
    write_checkpoint_atomic,
)
from pipeline.literary.incremental_identity_auditor_v1 import (  # noqa: E402
    apply_incremental_identity_ledger_to_prefix_v1,
    apply_incremental_identity_ledger_to_review_v1,
    build_incremental_identity_index_v1,
    build_incremental_identity_ledger_v1,
    normalize_surface_scope_action_coverage_v1,
    render_incremental_identity_request_v1,
    validate_incremental_identity_response_v1,
    verify_incremental_identity_index_v1,
    verify_incremental_identity_ledger_v1,
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
MODEL_CONTRACT = {
    "model_id": MODEL_ID,
    "temperature": 1.0,
    "seed": 20260716,
    "reasoning_effort": "none",
    "verbosity": "low",
    "max_output_tokens": 4096,
    "prompt_token_cap": 10_000,
}
BUCKET_ORDER = ("openai-row2", "openai-row1")
INTERNAL_UTC_DAY_TOKEN_CAP = 225_000
LOCAL_RPD_CAP = 100


class IncrementalIdentityLiveRunError(RuntimeError):
    pass


def _now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()


def _load_json(path: Path, label: str = "JSON artifact") -> dict[str, Any]:
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError) as exc:
        raise IncrementalIdentityLiveRunError(f"cannot load {label}: {path}") from exc
    if not isinstance(value, dict):
        raise IncrementalIdentityLiveRunError(f"{label} must be an object")
    return value


def _write_json(path: Path, payload: Mapping[str, Any]) -> None:
    if path.is_file():
        if canonical_json(_load_json(path)) != canonical_json(payload):
            raise IncrementalIdentityLiveRunError(f"immutable artifact differs: {path}")
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
    value = str(exc)
    return (
        "credential material was redacted from the transport error"
        if ("sk" + "-") in value
        else value[:1000]
    )


def _credential(path: Path) -> tuple[str, str]:
    try:
        value = Path(path).read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise IncrementalIdentityLiveRunError(
            f"cannot read credential file: {path}"
        ) from exc
    if not value.startswith("sk" + "-") or len(value) < 20:
        raise IncrementalIdentityLiveRunError("OpenAI credential is malformed")
    return value, sha256(value.encode("utf-8")).hexdigest()


def _transport_schema(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {
            str(key): _transport_schema(item)
            for key, item in value.items()
            if key not in {"minItems", "minLength", "uniqueItems"}
        }
    if isinstance(value, list):
        return [_transport_schema(row) for row in value]
    return value


def _response_format(schema: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "type": "json_schema",
        "json_schema": {
            "name": "literary_incremental_identity_surface_auditor_v1",
            "strict": True,
            "schema": _transport_schema(schema),
        },
    }


def _resolved_response_format(
    schema: Mapping[str, Any],
    structured_output_contract: StructuredOutputContract | None,
) -> dict[str, Any]:
    if structured_output_contract is None:
        return _response_format(schema)
    if structured_output_contract.canonical_schema_hash != canonical_hash(schema):
        raise IncrementalIdentityLiveRunError(
            "identity Structured Output contract has a foreign schema"
        )
    return openai_response_format(
        structured_output_contract,
        schema_name="literary_incremental_identity_auditor_v1",
    )


def prepare_incremental_identity_run(
    *,
    document_path: Path,
    prefix_path: Path,
    review_ledger_path: Path,
    design_doc: Path,
    output_dir: Path,
    previous_identity_ledger_path: Path | None = None,
    max_source_blocks: int = 32,
) -> dict[str, Any]:
    output_dir = Path(output_dir).resolve()
    document = _load_json(document_path, "document")
    prefix = _load_json(prefix_path, "chapter prefix")
    review = _load_json(review_ledger_path, "chapter-cycle review ledger")
    previous = (
        _load_json(previous_identity_ledger_path, "previous identity ledger")
        if previous_identity_ledger_path is not None
        else None
    )
    index = build_incremental_identity_index_v1(
        document=document,
        prefix_bundle=prefix,
        review_ledger=review,
        previous_identity_ledger=previous,
        max_source_blocks=max_source_blocks,
    )
    _write_json(output_dir / "identity_index.json", index)
    renderable: list[str] = []
    duplicate_suppressed: list[str] = []
    overflow: list[str] = []
    prompt_estimates: dict[str, int] = {}
    for component in index["components"]:
        component_id = component["component_id"]
        if component["overflow"]:
            overflow.append(component_id)
            continue
        if component["trigger_state"] == "duplicate_suppressed":
            duplicate_suppressed.append(component_id)
            continue
        rendered = render_incremental_identity_request_v1(
            index=index,
            component_id=component_id,
            document=document,
            design_doc=design_doc,
            previous_identity_ledger=previous,
        )
        request = {
            "component_id": rendered.component_id,
            "request_fingerprint": rendered.request_fingerprint,
            "messages": [dict(row) for row in rendered.messages],
            "response_schema": rendered.response_schema,
            "semantic_payload": rendered.semantic_payload,
        }
        _write_json(output_dir / "components" / component_id / "request.json", request)
        prompt_estimates[component_id] = estimate_prompt_tokens(
            request["messages"], _response_format(request["response_schema"])
        )
        renderable.append(component_id)
    body = {
        "schema_version": "incremental_identity_prepare_report_v1",
        "identity_index_hash": index["identity_index_hash"],
        "renderable_component_ids": sorted(renderable),
        "duplicate_suppressed_component_ids": sorted(duplicate_suppressed),
        "overflow_component_ids": sorted(overflow),
        "singleton_review_item_ids": list(index["singleton_review_item_ids"]),
        "prompt_token_estimates": prompt_estimates,
        "source_artifacts": {
            "document_sha256": file_sha256(document_path),
            "prefix_sha256": file_sha256(prefix_path),
            "review_ledger_sha256": file_sha256(review_ledger_path),
            "previous_identity_ledger_sha256": (
                file_sha256(previous_identity_ledger_path)
                if previous_identity_ledger_path is not None
                else None
            ),
        },
        "hidden_oracle_loaded": False,
        "production_publish_performed": False,
        "prepared_at": _now(),
    }
    report = {**body, "report_hash": canonical_hash(body)}
    _write_json(output_dir / "prepare_report.json", report)
    return report


def _prepared_artifacts(
    prepared_dir: Path, *, component_id: str | None = None
) -> tuple[dict[str, Any], dict[str, Any]]:
    index = verify_incremental_identity_index_v1(
        _load_json(prepared_dir / "identity_index.json", "identity index")
    )
    renderable = [
        row["component_id"]
        for row in index["components"]
        if not row["overflow"] and row["trigger_state"] != "duplicate_suppressed"
    ]
    selected = component_id
    if selected is None:
        if len(renderable) != 1:
            raise IncrementalIdentityLiveRunError(
                "multiple renderable components require an explicit component id"
            )
        selected = renderable[0]
    if selected not in renderable:
        raise IncrementalIdentityLiveRunError("component is not renderable")
    request = _load_json(
        prepared_dir / "components" / selected / "request.json", "sealed request"
    )
    required = {
        "component_id",
        "request_fingerprint",
        "messages",
        "response_schema",
        "semantic_payload",
    }
    if set(request) != required or request.get("component_id") != selected:
        raise IncrementalIdentityLiveRunError("sealed request shape drifted")
    if canonical_hash(request["semantic_payload"]) != canonical_hash(
        json.loads(str(request["messages"][-1]["content"]))
    ):
        raise IncrementalIdentityLiveRunError("request payload differs from user bytes")
    return index, request


def _select_bucket(
    *,
    preflight: Mapping[str, Any],
    reserve_tokens: int,
    bucket_order: Sequence[str] = BUCKET_ORDER,
) -> tuple[str, int, int]:
    usage = dict(preflight.get("usage_by_bucket_model") or {})
    calls = dict(preflight.get("calls_by_bucket_model") or {})
    if not bucket_order or len(set(bucket_order)) != len(bucket_order):
        raise IncrementalIdentityLiveRunError("invalid identity bucket order")
    for bucket in bucket_order:
        key = f"{bucket}|{MODEL_ID}"
        prior_tokens = int(usage.get(key, 0))
        prior_calls = int(calls.get(key, 0))
        if (
            prior_tokens + reserve_tokens <= INTERNAL_UTC_DAY_TOKEN_CAP
            and prior_calls < LOCAL_RPD_CAP
        ):
            return bucket, prior_tokens, prior_calls
    raise IncrementalIdentityLiveRunError("no OpenAI bucket can reserve this request")


def build_envelope(
    *,
    prepared_dir: Path,
    frozen_db: Path,
    usage_roots: Sequence[Path],
    exclude_root: Path | None = None,
    component_id: str | None = None,
    bucket_order: Sequence[str] = BUCKET_ORDER,
    structured_output_contract: StructuredOutputContract | None = None,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], dict[str, Any]]:
    index, request = _prepared_artifacts(prepared_dir, component_id=component_id)
    frozen_hash = file_sha256(frozen_db).upper()
    if frozen_hash != FROZEN_DB_SHA256:
        raise IncrementalIdentityLiveRunError("frozen DB differs from baseline")
    response_format = _resolved_response_format(
        request["response_schema"], structured_output_contract
    )
    prompt_estimate = estimate_prompt_tokens(request["messages"], response_format)
    if prompt_estimate > int(MODEL_CONTRACT["prompt_token_cap"]):
        raise IncrementalIdentityLiveRunError("identity request exceeds prompt cap")
    reserve_tokens = prompt_estimate + int(MODEL_CONTRACT["max_output_tokens"])
    preflight = scan_current_utc_usage(
        roots=[Path(row) for row in usage_roots], exclude_root=exclude_root
    )
    if preflight["unknown_bucket_rows"]:
        raise IncrementalIdentityLiveRunError("quota ledger has unknown bucket rows")
    bucket, prior_tokens, prior_calls = _select_bucket(
        preflight=preflight,
        reserve_tokens=reserve_tokens,
        bucket_order=bucket_order,
    )
    body = {
        "schema_version": "incremental_identity_live_envelope_v1",
        "git_head": _git_head(),
        "identity_index_hash": index["identity_index_hash"],
        "component_id": request["component_id"],
        "request_fingerprint": request["request_fingerprint"],
        "request_artifact_sha256": file_sha256(
            prepared_dir / "components" / request["component_id"] / "request.json"
        ),
        "model_contract": MODEL_CONTRACT,
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
        "internal_utc_day_token_cap": INTERNAL_UTC_DAY_TOKEN_CAP,
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
    bucket_order: Sequence[str] = BUCKET_ORDER,
    structured_output_contract: StructuredOutputContract | None = None,
) -> dict[str, Any]:
    output_dir = Path(output_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    envelope, preflight, index, request = build_envelope(
        prepared_dir=prepared_dir,
        frozen_db=frozen_db,
        usage_roots=usage_roots,
        exclude_root=output_dir,
        component_id=component_id,
        bucket_order=bucket_order,
        structured_output_contract=structured_output_contract,
    )
    if envelope["envelope_hash"] != approved_envelope_hash:
        raise IncrementalIdentityLiveRunError(
            f"approved envelope mismatch; current hash is {envelope['envelope_hash']}"
        )
    bucket = envelope["selected_quota_bucket_id"]
    if bucket not in key_paths:
        raise IncrementalIdentityLiveRunError("selected bucket has no credential")
    key, commitment = _credential(key_paths[bucket])
    _write_json(output_dir / "run_envelope.json", envelope)
    _write_json(output_dir / "quota_preflight.json", preflight)
    _write_json(output_dir / "request.json", request)
    manifest = {
        "schema_version": "incremental_identity_live_manifest_v1",
        "status": "running",
        "component_id": request["component_id"],
        "model_id": MODEL_ID,
        "quota_bucket_id": bucket,
        "credential_commitment": commitment,
        "envelope_hash": envelope["envelope_hash"],
        "frozen_db_sha256_before": file_sha256(frozen_db).upper(),
        "hidden_oracle_loaded": False,
        "production_publish_performed": False,
        "structured_output_contract": envelope["structured_output_contract"],
        "started_at": _now(),
    }
    _write_json(output_dir / "run_manifest.json", manifest)
    try:
        from openai import OpenAI

        config = LLMConfig(
            model=MODEL_ID,
            temperature=float(MODEL_CONTRACT["temperature"]),
            seed=int(MODEL_CONTRACT["seed"]),
            reasoning_effort=str(MODEL_CONTRACT["reasoning_effort"]),
            verbosity=str(MODEL_CONTRACT["verbosity"]),
            max_output_tokens=int(MODEL_CONTRACT["max_output_tokens"]),
            daily_token_cap=INTERNAL_UTC_DAY_TOKEN_CAP,
            prompt_token_cap=int(MODEL_CONTRACT["prompt_token_cap"]),
            pricing={"input": 0.0, "cached_input": 0.0, "output": 0.0},
        )
        client = LLMClient(
            config,
            output_dir / "cache" / bucket / "incremental_identity.sqlite3",
            transport=OpenAI(api_key=key).chat.completions.create,
            max_retries=2,
        )
        response_format = _resolved_response_format(
            request["response_schema"], structured_output_contract
        )
        result = client.call(
            [dict(row) for row in request["messages"]],
            response_format=response_format,
            tag=f"incremental_identity:{request['component_id']}",
        )
        raw = {
            "schema_version": "incremental_identity_raw_result_v1",
            "component_id": request["component_id"],
            "request_fingerprint": request["request_fingerprint"],
            "model": result.model,
            "quota_bucket_id": bucket,
            "credential_commitment": commitment,
            "text": result.text,
            "parsed_json": result.parsed_json,
            "json_error": result.json_error,
            "usage": asdict(result.usage),
            "latency_ms": result.latency_ms,
            "from_cache": result.from_cache,
            "cache_key": result.cache_key,
            "structured_output_contract": envelope["structured_output_contract"],
            "completed_at": _now(),
        }
        _write_json(output_dir / "raw_result.json", raw)
        if not isinstance(result.parsed_json, dict):
            raise IncrementalIdentityLiveRunError(
                f"model returned invalid structured JSON: {result.json_error}"
            )
        if structured_output_contract is not None:
            validate_structured_payload(
                result.parsed_json,
                canonical_schema=request["response_schema"],
            )
        normalized_response, surface_scope_normalizations = (
            normalize_surface_scope_action_coverage_v1(
                result.parsed_json,
                index=index,
            )
        )
        _write_json(
            output_dir / "surface_scope_normalizations.json",
            {
                "schema_version": "incremental_identity_surface_scope_normalizations_v1",
                "normalization_count": len(surface_scope_normalizations),
                "normalizations": surface_scope_normalizations,
            },
        )
        decision = validate_incremental_identity_response_v1(
            normalized_response,
            index=index,
            request_fingerprint=request["request_fingerprint"],
        )
        _write_json(output_dir / "decision.json", decision)
        frozen_after = file_sha256(frozen_db).upper()
        if frozen_after != FROZEN_DB_SHA256:
            raise IncrementalIdentityLiveRunError("frozen DB changed during live call")
        report_body = {
            "schema_version": "incremental_identity_live_report_v1",
            "status": "accepted_component_pending_reconciliation",
            "component_id": request["component_id"],
            "decision_hash": decision["decision_hash"],
            "decision_status": decision["status"],
            "candidate_actions": decision["candidate_actions"],
            "surface_scope_normalization_count": len(
                surface_scope_normalizations
            ),
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
            {**manifest, "status": report["status"], "completed_at": report["completed_at"]},
        )
        return report
    except Exception as exc:
        failure = {
            "schema_version": "incremental_identity_live_failure_v1",
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
    *, prepared_dir: Path, output_dir: Path, frozen_db: Path, component_id: str
) -> dict[str, Any]:
    index, request = _prepared_artifacts(prepared_dir, component_id=component_id)
    raw = _load_json(output_dir / "raw_result.json", "persisted raw result")
    if raw.get("component_id") != component_id or not isinstance(
        raw.get("parsed_json"), dict
    ):
        raise IncrementalIdentityLiveRunError("raw result is absent or foreign")
    decision = validate_incremental_identity_response_v1(
        raw["parsed_json"],
        index=index,
        request_fingerprint=request["request_fingerprint"],
    )
    _write_json(output_dir / "decision.json", decision)
    if file_sha256(frozen_db).upper() != FROZEN_DB_SHA256:
        raise IncrementalIdentityLiveRunError("frozen DB changed before recovery")
    body = {
        "schema_version": "incremental_identity_live_report_v1",
        "status": "accepted_component_pending_reconciliation",
        "component_id": component_id,
        "decision_hash": decision["decision_hash"],
        "decision_status": decision["status"],
        "candidate_actions": decision["candidate_actions"],
        "usage": raw.get("usage"),
        "latency_ms": raw.get("latency_ms"),
        "from_cache": raw.get("from_cache"),
        "quota_bucket_id": raw.get("quota_bucket_id"),
        "frozen_db_sha256_after": file_sha256(frozen_db).upper(),
        "hidden_oracle_loaded": False,
        "production_publish_performed": False,
        "recovered_from_persisted_raw_result": True,
        "completed_at": _now(),
    }
    report = {**body, "report_hash": canonical_hash(body)}
    _write_json(output_dir / "live_report.json", report)
    return report


def reconcile_component_results(
    *,
    prepared_dir: Path,
    decision_dirs: Sequence[Path],
    prefix_path: Path,
    review_ledger_path: Path,
    output_dir: Path,
    frozen_db: Path,
    previous_identity_ledger_path: Path | None = None,
) -> dict[str, Any]:
    index = verify_incremental_identity_index_v1(
        _load_json(prepared_dir / "identity_index.json", "identity index")
    )
    decisions = [
        _load_json(Path(row) / "decision.json", "identity decision")
        for row in decision_dirs
    ]
    previous = (
        verify_incremental_identity_ledger_v1(
            _load_json(previous_identity_ledger_path, "previous identity ledger")
        )
        if previous_identity_ledger_path is not None
        else None
    )
    ledger = build_incremental_identity_ledger_v1(
        index=index,
        decisions=decisions,
        previous_identity_ledger=previous,
    )
    reviewed_prefix = apply_incremental_identity_ledger_to_prefix_v1(
        prefix_bundle=_load_json(prefix_path, "chapter prefix"),
        identity_ledger=ledger,
    )
    reviewed_review = apply_incremental_identity_ledger_to_review_v1(
        review_ledger=_load_json(review_ledger_path, "review ledger"),
        identity_ledger=ledger,
    )
    if file_sha256(frozen_db).upper() != FROZEN_DB_SHA256:
        raise IncrementalIdentityLiveRunError("frozen DB changed before reconciliation")
    output_dir = Path(output_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=False)
    _write_json(output_dir / "identity_ledger.json", ledger)
    _write_json(output_dir / "reviewed_prefix.json", reviewed_prefix)
    _write_json(output_dir / "reviewed_review_ledger.json", reviewed_review)
    body = {
        "schema_version": "incremental_identity_reconciliation_report_v1",
        "status": "reconciled_all_renderable_components",
        "identity_index_hash": index["identity_index_hash"],
        "decision_hashes": sorted(row["decision_hash"] for row in decisions),
        "identity_ledger_hash": ledger["identity_ledger_hash"],
        "reviewed_prefix_hash": reviewed_prefix["prefix_bundle_hash"],
        "reviewed_review_ledger_hash": reviewed_review["review_ledger_hash"],
        "component_statuses": {
            row["component_key"]: row["status"] for row in ledger["component_states"]
        },
        "frozen_db_sha256_after": file_sha256(frozen_db).upper(),
        "production_publish_performed": False,
        "completed_at": _now(),
    }
    report = {**body, "report_hash": canonical_hash(body)}
    _write_json(output_dir / "reconciliation_report.json", report)
    return report


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run bounded incremental literary identity hearings"
    )
    parser.add_argument(
        "mode", choices=("prepare", "dry", "live", "recover", "reconcile")
    )
    parser.add_argument("--prepared-dir", type=Path)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--document", type=Path)
    parser.add_argument("--prefix", type=Path)
    parser.add_argument("--review-ledger", type=Path)
    parser.add_argument("--design-doc", type=Path)
    parser.add_argument("--previous-identity-ledger", type=Path)
    parser.add_argument("--frozen-db", type=Path)
    parser.add_argument("--usage-root", type=Path, action="append", default=[])
    parser.add_argument("--component-id")
    parser.add_argument("--decision-dir", type=Path, action="append", default=[])
    parser.add_argument("--approved-envelope-hash")
    parser.add_argument("--key-1", type=Path)
    parser.add_argument("--key-2", type=Path)
    parser.add_argument("--max-source-blocks", type=int, default=32)
    return parser


def _need(args: argparse.Namespace, *names: str) -> None:
    missing = [name for name in names if getattr(args, name) is None]
    if missing:
        raise IncrementalIdentityLiveRunError(
            f"mode {args.mode} requires: {', '.join(missing)}"
        )


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    usage_roots = args.usage_root or [RUNTIME_ROOT / "data"]
    if args.mode == "prepare":
        _need(args, "document", "prefix", "review_ledger", "design_doc")
        result = prepare_incremental_identity_run(
            document_path=args.document,
            prefix_path=args.prefix,
            review_ledger_path=args.review_ledger,
            design_doc=args.design_doc,
            output_dir=args.output_dir,
            previous_identity_ledger_path=args.previous_identity_ledger,
            max_source_blocks=args.max_source_blocks,
        )
    elif args.mode == "dry":
        _need(args, "prepared_dir", "frozen_db")
        args.output_dir.mkdir(parents=True, exist_ok=True)
        envelope, preflight, _, _ = build_envelope(
            prepared_dir=args.prepared_dir,
            frozen_db=args.frozen_db,
            usage_roots=usage_roots,
            exclude_root=args.output_dir,
            component_id=args.component_id,
        )
        _write_json(args.output_dir / "run_envelope.json", envelope)
        _write_json(args.output_dir / "quota_preflight.json", preflight)
        result = envelope
    elif args.mode == "live":
        _need(
            args,
            "prepared_dir",
            "frozen_db",
            "approved_envelope_hash",
            "key_1",
            "key_2",
        )
        result = run_live(
            prepared_dir=args.prepared_dir,
            output_dir=args.output_dir,
            frozen_db=args.frozen_db,
            usage_roots=usage_roots,
            approved_envelope_hash=args.approved_envelope_hash,
            key_paths={"openai-row1": args.key_1, "openai-row2": args.key_2},
            component_id=args.component_id,
        )
    elif args.mode == "recover":
        _need(args, "prepared_dir", "frozen_db", "component_id")
        result = recover_component_result(
            prepared_dir=args.prepared_dir,
            output_dir=args.output_dir,
            frozen_db=args.frozen_db,
            component_id=args.component_id,
        )
    else:
        _need(args, "prepared_dir", "prefix", "review_ledger", "frozen_db")
        if not args.decision_dir:
            raise IncrementalIdentityLiveRunError(
                "reconcile requires at least one decision directory"
            )
        result = reconcile_component_results(
            prepared_dir=args.prepared_dir,
            decision_dirs=args.decision_dir,
            prefix_path=args.prefix,
            review_ledger_path=args.review_ledger,
            output_dir=args.output_dir,
            frozen_db=args.frozen_db,
            previous_identity_ledger_path=args.previous_identity_ledger,
        )
    print(canonical_json(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
