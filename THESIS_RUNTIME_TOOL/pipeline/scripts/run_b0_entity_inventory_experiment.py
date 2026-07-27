from __future__ import annotations

import argparse
from dataclasses import replace
from datetime import UTC, datetime
import json
from pathlib import Path
from typing import Any, Mapping, Sequence

from pipeline.literary.b0_entity_inventory_experiment import (
    EXPERIMENT_SCHEMA_VERSION,
    PROMPT_ID,
    evaluate_inventory_against_gold,
    render_entity_inventory_request,
    validate_entity_inventory_response,
)
from pipeline.literary.checkpoint import canonical_hash, canonical_json, file_sha256
from pipeline.literary.chapter_registry_v4 import estimate_registry_prompt_tokens
from pipeline.scripts.run_chapter_registry_v3_real import (
    PersistedRealExecutorV3,
    RealOpenAIRegistryExecutorV3,
)
from pipeline.scripts.run_chapter_registry_v4_real import (
    DEFAULT_CHAPTER_ID,
    DEFAULT_DESIGN_DOC,
    DEFAULT_DOCUMENT,
    DEFAULT_FROZEN_DB,
    RUNTIME_ROOT,
    _aggregate_live_usage,
    _ensure_empty_output,
    _git_head,
    _load_document,
    _quota_policy,
    _redacted_error,
    _verify_frozen_db,
    _write_json,
    draft_semantic_config_v4,
    draft_transport_config_v4,
    scan_current_utc_usage,
)


RUN_SCHEMA_VERSION = "b0_entity_inventory_experiment_run_v1_2"
ALLOWED_MODEL_IDS = frozenset({"gpt-5.4", "gpt-5.4-mini"})
DEFAULT_GOLD = RUNTIME_ROOT / "data" / "eval" / "literary_m4f" / "wh_ch01_registry_gold_v1.json"
OUTPUT_TOKEN_CAP = 4096
INPUT_TOKEN_CAP = 20000
CORE_FILE = RUNTIME_ROOT / "pipeline" / "literary" / "b0_entity_inventory_experiment.py"


class EntityInventoryExperimentError(RuntimeError):
    pass


def _now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()


def _transport_config(model_id: str = "gpt-5.4") -> Any:
    if model_id not in ALLOWED_MODEL_IDS:
        raise EntityInventoryExperimentError(f"unsupported comparison model: {model_id}")
    semantic = replace(draft_semantic_config_v4(), b0_model_id=model_id)
    transport = draft_transport_config_v4(semantic)
    gate_suffix = "gpt54" if model_id == "gpt-5.4" else "mini"
    role_gate_ids = dict(transport.role_quota_gate_ids)
    role_gate_ids["b0"] = tuple(
        f"{bucket}-{gate_suffix}" for bucket in ("openai-row2", "openai-row1")
    )
    return replace(
        transport,
        b0_output_cap=OUTPUT_TOKEN_CAP,
        b0_input_cap=INPUT_TOKEN_CAP,
        role_quota_gate_ids=role_gate_ids,
    )


def build_experiment_envelope(
    *,
    document_path: Path,
    design_doc: Path,
    chapter_id: str,
    model_id: str = "gpt-5.4",
) -> tuple[dict[str, Any], Any, Mapping[str, Any], Any]:
    document, chapter = _load_document(document_path, chapter_id)
    transport = _transport_config(model_id)
    request = render_entity_inventory_request(
        chapter=chapter,
        design_doc=design_doc,
        model_id=transport.b0_model_id,
        reasoning_effort=transport.b0_reasoning_effort,
        temperature=transport.b0_temperature,
        seed=transport.b0_seed,
        max_output_tokens=transport.b0_output_cap,
    )
    prompt_tokens = estimate_registry_prompt_tokens(request.messages)
    if prompt_tokens > transport.b0_input_cap:
        raise EntityInventoryExperimentError(
            f"entity inventory input {prompt_tokens} exceeds cap {transport.b0_input_cap}"
        )
    body = {
        "schema_version": RUN_SCHEMA_VERSION,
        "experiment_schema_version": EXPERIMENT_SCHEMA_VERSION,
        "chapter_id": chapter_id,
        "document_sha256": file_sha256(document_path),
        "design_doc_sha256": file_sha256(design_doc),
        "prompt_id": PROMPT_ID,
        "prompt_sha256": request.prompt_sha256,
        "response_schema_hash": request.response_schema_hash,
        "request_fingerprint": request.request_fingerprint,
        "git_head": _git_head(),
        "model_contract": {
            "model_id": transport.b0_model_id,
            "reasoning_effort": transport.b0_reasoning_effort,
            "temperature": transport.b0_temperature,
            "seed": transport.b0_seed,
            "max_output_tokens": transport.b0_output_cap,
            "input_token_cap": transport.b0_input_cap,
        },
        "estimated_prompt_tokens": prompt_tokens,
        "reserved_tokens": prompt_tokens + transport.b0_output_cap,
        "transport_config_hash": transport.config_hash,
        "quota_policy": _quota_policy(),
        "runtime_artifact_hashes": {
            CORE_FILE.relative_to(RUNTIME_ROOT.parent).as_posix(): file_sha256(CORE_FILE),
            Path(__file__).resolve().relative_to(RUNTIME_ROOT.parent).as_posix(): file_sha256(
                Path(__file__).resolve()
            ),
        },
        "gold_access_policy": "POST_RESPONSE_EVALUATION_ONLY_NOT_IN_REQUEST",
        "phase_boundary": "one_chapter_one_call_no_registry_publish_no_retry",
    }
    return {**body, "envelope_hash": canonical_hash(body)}, request, chapter, transport


def run_dry(
    *,
    document_path: Path,
    design_doc: Path,
    output_dir: Path,
    chapter_id: str,
    model_id: str = "gpt-5.4",
) -> dict[str, Any]:
    output = _ensure_empty_output(output_dir)
    envelope, request, _chapter, _transport = build_experiment_envelope(
        document_path=document_path,
        design_doc=design_doc,
        chapter_id=chapter_id,
        model_id=model_id,
    )
    report = {
        "schema_version": "b0_entity_inventory_experiment_dry_v1_2",
        "status": "dry_rendered_no_api",
        "chapter_id": chapter_id,
        "model_id": envelope["model_contract"]["model_id"],
        "envelope_hash": envelope["envelope_hash"],
        "request_fingerprint": request.request_fingerprint,
        "estimated_prompt_tokens": envelope["estimated_prompt_tokens"],
        "output_token_cap": envelope["model_contract"]["max_output_tokens"],
        "reserved_tokens": envelope["reserved_tokens"],
    }
    _write_json(output / f"run_envelope_{envelope['envelope_hash'][:12]}.json", envelope)
    _write_json(output / "request.json", request.to_dict())
    _write_json(output / "dry_report.json", report)
    return report


def run_live(
    *,
    document_path: Path,
    design_doc: Path,
    frozen_db: Path,
    output_dir: Path,
    approved_envelope_hash: str,
    key_paths: Mapping[str, Path],
    usage_roots: Sequence[Path],
    gold_path: Path | None,
    chapter_id: str,
    model_id: str = "gpt-5.4",
) -> dict[str, Any]:
    output = _ensure_empty_output(output_dir)
    frozen_hash = _verify_frozen_db(frozen_db)
    envelope, request, chapter, transport = build_experiment_envelope(
        document_path=document_path,
        design_doc=design_doc,
        chapter_id=chapter_id,
        model_id=model_id,
    )
    if approved_envelope_hash != envelope["envelope_hash"]:
        raise EntityInventoryExperimentError(
            f"approved envelope mismatch; current hash is {envelope['envelope_hash']}"
        )
    preflight = scan_current_utc_usage(roots=usage_roots, exclude_root=output)
    if preflight["unknown_bucket_rows"]:
        raise EntityInventoryExperimentError(
            "quota preflight found current-day OpenAI rows with unknown buckets"
        )
    _write_json(output / f"run_envelope_{envelope['envelope_hash'][:12]}.json", envelope)
    _write_json(output / "quota_preflight.json", preflight)
    manifest = {
        "schema_version": RUN_SCHEMA_VERSION,
        "status": "running",
        "chapter_id": chapter_id,
        "model_id": envelope["model_contract"]["model_id"],
        "envelope_hash": envelope["envelope_hash"],
        "gold_loaded_before_response": False,
        "frozen_db_sha256_before": frozen_hash,
        "started_at": _now(),
    }
    _write_json(output / "run_manifest.json", manifest)

    real = RealOpenAIRegistryExecutorV3(
        run_config=transport,
        run_root=output,
        credential_paths=key_paths,
        prior_usage_by_bucket_model=preflight["usage_by_bucket_model"],
        prior_calls_by_bucket_model=preflight["calls_by_bucket_model"],
        min_interval_seconds=float(_quota_policy()["minimum_interval_seconds"]),
        local_rpd_cap=int(_quota_policy()["local_rpd_cap_per_bucket_model"]),
    )
    executor = PersistedRealExecutorV3(
        executor=real,
        run_root=output,
        run_config=transport,
        frozen_db=frozen_db,
    )
    try:
        raw = executor.execute(request)
        inventory = validate_entity_inventory_response(
            raw, chapter, request_fingerprint=request.request_fingerprint
        )
        _write_json(output / "inventory.json", inventory)
        executor.record_chapter_validation(
            chapter_id=chapter_id,
            status="accepted_experiment_inventory",
            payload={"inventory_hash": inventory["inventory_hash"]},
        )

        evaluation = None
        if gold_path is not None:
            gold = json.loads(Path(gold_path).read_text(encoding="utf-8"))
            evaluation = evaluate_inventory_against_gold(inventory, gold)
            _write_json(output / "post_response_gold_evaluation.json", evaluation)

        report = {
            "schema_version": "b0_entity_inventory_experiment_live_report_v1_2",
            "status": "accepted_one_call_experiment",
            "chapter_id": chapter_id,
            "model_id": envelope["model_contract"]["model_id"],
            "envelope_hash": envelope["envelope_hash"],
            "inventory_hash": inventory["inventory_hash"],
            "entity_candidate_count": len(inventory["entity_candidates"]),
            "glossary_candidate_count": len(inventory["glossary_candidates"]),
            "unresolved_referent_count": len(inventory["unresolved_referents"]),
            "rejected_row_count": inventory["validation_report"]["rejected_row_count"],
            "claim_issue_count": inventory["validation_report"]["claim_issue_count"],
            "alternative_name_issue_count": inventory["validation_report"][
                "alternative_name_issue_count"
            ],
            "canonical_name_class_issue_count": inventory["validation_report"][
                "canonical_name_class_issue_count"
            ],
            "pending_surface_repair_count": inventory["validation_report"][
                "pending_surface_repair_count"
            ],
            "pending_auditor_entity_count": inventory["validation_report"][
                "pending_auditor_entity_count"
            ],
            "dormant_unresolved_count": inventory["validation_report"][
                "dormant_unresolved_count"
            ],
            "unlocated_surface_count": inventory["validation_report"][
                "unlocated_surface_count"
            ],
            "evaluation_hash": evaluation.get("evaluation_hash") if evaluation else None,
            "confirmed_entity_recall": (
                evaluation.get("confirmed_entity_recall") if evaluation else None
            ),
            "glossary_recall": evaluation.get("glossary_recall") if evaluation else None,
            "wrong_merge_count": evaluation.get("wrong_merge_count") if evaluation else None,
            "wrong_split_count": evaluation.get("wrong_split_count") if evaluation else None,
            "unmatched_entity_candidate_count": (
                evaluation.get("unmatched_entity_candidate_count") if evaluation else None
            ),
            "usage": _aggregate_live_usage(executor.records),
            "gold_loaded_only_after_inventory_persisted": gold_path is not None,
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
            "schema_version": "b0_entity_inventory_experiment_failure_v1_2",
            "status": "halted_fail_closed",
            "chapter_id": chapter_id,
            "error_type": type(exc).__name__,
            "message": _redacted_error(exc),
            "usage": _aggregate_live_usage(executor.records),
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
    parser = argparse.ArgumentParser(description="One-call full-chapter B0 entity inventory arm")
    parser.add_argument("mode", choices=("dry", "live"))
    parser.add_argument("--document", type=Path, default=DEFAULT_DOCUMENT)
    parser.add_argument("--design-doc", type=Path, default=DEFAULT_DESIGN_DOC)
    parser.add_argument("--frozen-db", type=Path, default=DEFAULT_FROZEN_DB)
    parser.add_argument("--chapter-id", default=DEFAULT_CHAPTER_ID)
    parser.add_argument("--model-id", choices=sorted(ALLOWED_MODEL_IDS), default="gpt-5.4")
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--approved-envelope-hash")
    parser.add_argument("--key-1", type=Path)
    parser.add_argument("--key-2", type=Path)
    parser.add_argument("--usage-root", type=Path, action="append", default=[])
    parser.add_argument("--gold", type=Path, default=DEFAULT_GOLD)
    parser.add_argument("--no-gold-eval", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.mode == "dry":
        report = run_dry(
            document_path=args.document,
            design_doc=args.design_doc,
            output_dir=args.output_dir,
            chapter_id=args.chapter_id,
            model_id=args.model_id,
        )
    else:
        if not args.approved_envelope_hash or not args.key_1 or not args.key_2:
            raise EntityInventoryExperimentError(
                "live mode requires approved envelope hash and two key file paths"
            )
        report = run_live(
            document_path=args.document,
            design_doc=args.design_doc,
            frozen_db=args.frozen_db,
            output_dir=args.output_dir,
            approved_envelope_hash=args.approved_envelope_hash,
            key_paths={"openai-row1": args.key_1, "openai-row2": args.key_2},
            usage_roots=args.usage_root or [RUNTIME_ROOT / "data"],
            gold_path=None if args.no_gold_eval else args.gold,
            chapter_id=args.chapter_id,
            model_id=args.model_id,
        )
    print(canonical_json(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
