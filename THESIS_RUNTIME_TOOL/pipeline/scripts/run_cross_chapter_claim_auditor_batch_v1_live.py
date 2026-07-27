from __future__ import annotations

import argparse
from dataclasses import asdict
from pathlib import Path
import sys
from typing import Any, Mapping, Sequence


RUNTIME_ROOT = Path(__file__).resolve().parents[2]
if str(RUNTIME_ROOT) not in sys.path:
    sys.path.insert(0, str(RUNTIME_ROOT))

from pipeline.agents.llm_client import LLMClient, estimate_prompt_tokens  # noqa: E402
from pipeline.agents.llm_config import LLMConfig  # noqa: E402
from pipeline.literary.book_entity_claim_auditor_batch_v1 import (  # noqa: E402
    RenderedPriorClaimBatchRequestV1,
    render_prior_claim_batch_request_v1,
    validate_prior_claim_batch_response_v1,
)
from pipeline.literary.book_entity_claim_auditor_v1 import (  # noqa: E402
    build_prior_claim_projection_v1,
    build_prior_claim_revision_ledger_v1,
    verify_prior_claim_ticket_index_v1,
)
from pipeline.literary.checkpoint import canonical_hash, file_sha256  # noqa: E402
from pipeline.scripts.run_chapter_registry_v4_real import (  # noqa: E402
    FROZEN_DB_SHA256,
    scan_current_utc_usage,
)
from pipeline.scripts.run_cross_chapter_claim_auditor_v1_live import (  # noqa: E402
    INTERNAL_UTC_DAY_TOKEN_CAP,
    MODEL_CONTRACT,
    MODEL_ID,
    PriorClaimLiveRunError,
    _credential,
    _git_head,
    _load_json,
    _now,
    _openai_transport_schema,
    _redacted_error,
    _select_bucket,
    _write_json,
)


def _response_format(schema: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "type": "json_schema",
        "json_schema": {
            "name": "literary_cross_chapter_prior_claim_auditor_batch_v1",
            "strict": True,
            "schema": _openai_transport_schema(schema),
        },
    }


def _prepared_batch(
    *,
    prepared_dir: Path,
    component_ids: Sequence[str],
    design_doc: Path,
) -> tuple[dict[str, Any], RenderedPriorClaimBatchRequestV1]:
    index = verify_prior_claim_ticket_index_v1(
        _load_json(prepared_dir / "ticket_index.json")
    )
    requested = [str(value) for value in component_ids]
    if len(requested) != len(set(requested)):
        raise PriorClaimLiveRunError("batch repeats a component id")
    renderable = {
        str(row["component_id"])
        for row in index["claim_components"]
        if not row["overflow"]
    }
    if len(requested) < 2 or not set(requested) <= renderable:
        raise PriorClaimLiveRunError(
            "batch requires at least two distinct renderable component ids"
        )
    component_requests = [
        _load_json(prepared_dir / "components" / component_id / "request.json")
        for component_id in requested
    ]
    request = render_prior_claim_batch_request_v1(
        component_requests=component_requests,
        design_doc=design_doc,
    )
    return index, request


def build_batch_envelope(
    *,
    prepared_dir: Path,
    component_ids: Sequence[str],
    design_doc: Path,
    frozen_db: Path,
    usage_roots: Sequence[Path],
    exclude_root: Path | None = None,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], RenderedPriorClaimBatchRequestV1]:
    index, request = _prepared_batch(
        prepared_dir=prepared_dir,
        component_ids=component_ids,
        design_doc=design_doc,
    )
    frozen_hash = file_sha256(frozen_db).upper()
    if frozen_hash != FROZEN_DB_SHA256:
        raise PriorClaimLiveRunError("frozen DB hash differs from the accepted baseline")
    response_format = _response_format(request.response_schema)
    prompt_estimate = estimate_prompt_tokens(request.messages, response_format)
    if prompt_estimate > int(MODEL_CONTRACT["prompt_token_cap"]):
        raise PriorClaimLiveRunError("batch prompt estimate exceeds the sealed cap")
    reserve_tokens = prompt_estimate + int(MODEL_CONTRACT["max_output_tokens"])
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
    )
    request_dict = asdict(request)
    body = {
        "schema_version": "cross_chapter_prior_claim_batch_live_envelope_v1",
        "git_head": _git_head(),
        "ticket_index_hash": index["ticket_index_hash"],
        "batch_id": request.batch_id,
        "component_ids": list(request.component_ids),
        "request_fingerprint": request.request_fingerprint,
        "request_artifact_hash": canonical_hash(request_dict),
        "model_contract": MODEL_CONTRACT,
        "response_format_hash": canonical_hash(response_format),
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


def run_batch_live(
    *,
    prepared_dir: Path,
    component_ids: Sequence[str],
    design_doc: Path,
    output_dir: Path,
    frozen_db: Path,
    usage_roots: Sequence[Path],
    approved_envelope_hash: str,
    key_paths: Mapping[str, Path],
) -> dict[str, Any]:
    output_dir = output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    envelope, preflight, index, request = build_batch_envelope(
        prepared_dir=prepared_dir,
        component_ids=component_ids,
        design_doc=design_doc,
        frozen_db=frozen_db,
        usage_roots=usage_roots,
        exclude_root=output_dir,
    )
    if envelope["envelope_hash"] != approved_envelope_hash:
        raise PriorClaimLiveRunError(
            f"approved envelope mismatch; current hash is {envelope['envelope_hash']}"
        )
    bucket = envelope["selected_quota_bucket_id"]
    if bucket not in key_paths:
        raise PriorClaimLiveRunError("selected quota bucket has no credential path")
    key, commitment = _credential(key_paths[bucket])
    request_dict = asdict(request)
    _write_json(output_dir / "run_envelope.json", envelope)
    _write_json(output_dir / "quota_preflight.json", preflight)
    _write_json(output_dir / "request.json", request_dict)
    manifest = {
        "schema_version": "cross_chapter_prior_claim_batch_live_manifest_v1",
        "status": "running",
        "model_id": MODEL_ID,
        "batch_id": request.batch_id,
        "component_ids": list(request.component_ids),
        "envelope_hash": envelope["envelope_hash"],
        "quota_bucket_id": bucket,
        "credential_commitment": commitment,
        "frozen_db_sha256_before": file_sha256(frozen_db).upper(),
        "hidden_oracle_loaded": False,
        "production_publish_performed": False,
        "started_at": _now(),
    }
    _write_json(output_dir / "run_manifest.json", manifest)
    try:
        from openai import OpenAI

        transport = OpenAI(api_key=key).chat.completions.create
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
            output_dir / "cache" / bucket / "prior_claim_batch_auditor.sqlite3",
            transport=transport,
            max_retries=2,
        )
        result = client.call(
            [dict(row) for row in request.messages],
            response_format=_response_format(request.response_schema),
            tag=f"prior_claim_batch:{request.batch_id}",
        )
        raw = {
            "schema_version": "cross_chapter_prior_claim_batch_raw_result_v1",
            "batch_id": request.batch_id,
            "component_ids": list(request.component_ids),
            "request_fingerprint": request.request_fingerprint,
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
            "completed_at": _now(),
        }
        _write_json(output_dir / "raw_result.json", raw)
        if not isinstance(result.parsed_json, dict):
            raise PriorClaimLiveRunError(
                f"GPT-5.4 did not return valid batch JSON: {result.json_error}"
            )
        batch_decision = validate_prior_claim_batch_response_v1(
            result.parsed_json,
            index=index,
            request=request,
        )
        _write_json(output_dir / "batch_decision.json", batch_decision)
        decision_dir = output_dir / "decisions"
        decision_dir.mkdir(parents=True, exist_ok=True)
        for decision in batch_decision["component_decisions"]:
            _write_json(decision_dir / f"{decision['component_id']}.json", decision)

        renderable_ids = {
            str(row["component_id"])
            for row in index["claim_components"]
            if not row["overflow"]
        }
        ledger = None
        projection = None
        status = "accepted_batch_pending_reconciliation"
        if set(request.component_ids) == renderable_ids:
            ledger = build_prior_claim_revision_ledger_v1(
                index=index,
                decisions=batch_decision["component_decisions"],
            )
            projection = build_prior_claim_projection_v1(
                prior_cards=index["prior_cards"],
                ledger=ledger,
            )
            _write_json(output_dir / "claim_revision_ledger.json", ledger)
            _write_json(output_dir / "claim_projection.json", projection)
            status = "accepted_batch_reconciled"
        frozen_after = file_sha256(frozen_db).upper()
        if frozen_after != FROZEN_DB_SHA256:
            raise PriorClaimLiveRunError("frozen DB changed during the batch call")
        report_body = {
            "schema_version": "cross_chapter_prior_claim_batch_live_report_v1",
            "status": status,
            "batch_id": request.batch_id,
            "component_ids": list(request.component_ids),
            "batch_decision_hash": batch_decision["batch_decision_hash"],
            "component_decision_hashes": [
                row["decision_hash"] for row in batch_decision["component_decisions"]
            ],
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
            "schema_version": "cross_chapter_prior_claim_batch_live_failure_v1",
            "status": "halted_fail_closed",
            "batch_id": request.batch_id,
            "component_ids": list(request.component_ids),
            "error_type": type(exc).__name__,
            "message": _redacted_error(exc),
            "frozen_db_sha256_after": file_sha256(frozen_db).upper(),
            "production_publish_performed": False,
            "failed_at": _now(),
        }
        _write_json(output_dir / "failure.json", failure)
        raise


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run one bounded GPT-5.4 transport batch of claim components."
    )
    parser.add_argument("mode", choices=("dry", "live"))
    parser.add_argument("--prepared-dir", type=Path, required=True)
    parser.add_argument("--component-id", action="append", default=[])
    parser.add_argument(
        "--design-doc",
        type=Path,
        default=RUNTIME_ROOT.parent / "design" / "LITERARY_PROMPT_DESIGN.md",
    )
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--frozen-db", type=Path, required=True)
    parser.add_argument("--usage-root", type=Path, action="append", default=[])
    parser.add_argument("--approved-envelope-hash")
    parser.add_argument("--key-1", type=Path)
    parser.add_argument("--key-2", type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    usage_roots = args.usage_root or [RUNTIME_ROOT / "data"]
    if args.mode == "dry":
        args.output_dir.mkdir(parents=True, exist_ok=True)
        envelope, preflight, _, request = build_batch_envelope(
            prepared_dir=args.prepared_dir,
            component_ids=args.component_id,
            design_doc=args.design_doc,
            frozen_db=args.frozen_db,
            usage_roots=usage_roots,
            exclude_root=args.output_dir,
        )
        _write_json(args.output_dir / "request.json", asdict(request))
        _write_json(args.output_dir / "run_envelope.json", envelope)
        _write_json(args.output_dir / "quota_preflight.json", preflight)
        print(canonical_hash(envelope))
        print(envelope["envelope_hash"])
        return 0
    if not args.approved_envelope_hash or not args.key_1 or not args.key_2:
        raise PriorClaimLiveRunError(
            "live mode requires approved envelope hash and two credential paths"
        )
    report = run_batch_live(
        prepared_dir=args.prepared_dir,
        component_ids=args.component_id,
        design_doc=args.design_doc,
        output_dir=args.output_dir,
        frozen_db=args.frozen_db,
        usage_roots=usage_roots,
        approved_envelope_hash=args.approved_envelope_hash,
        key_paths={"openai-row1": args.key_1, "openai-row2": args.key_2},
    )
    print(canonical_hash(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
