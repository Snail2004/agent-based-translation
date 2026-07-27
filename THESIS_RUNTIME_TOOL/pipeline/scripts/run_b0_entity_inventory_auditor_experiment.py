from __future__ import annotations

import argparse
from dataclasses import replace
from datetime import UTC, datetime
import json
from pathlib import Path
from typing import Any, Mapping, Sequence

from pipeline.literary.b0_entity_inventory_auditor_experiment import (
    AUDITOR_SCHEMA_VERSION,
    PROMPT_ID,
    render_inventory_auditor_request,
    validate_and_apply_auditor_response,
)
from pipeline.literary.b0_entity_inventory_experiment import evaluate_inventory_against_gold
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


RUN_SCHEMA_VERSION = "b0_entity_inventory_auditor_experiment_run_v1_2"
ALLOWED_MODEL_IDS = frozenset({"gpt-5.4", "gpt-5.4-mini"})
DEFAULT_INVENTORY = (
    RUNTIME_ROOT
    / "data"
    / "reports"
    / "literary_m4f_b0_entity_inventory_exp_v1_2_real_20260715_1_openai"
    / "inventory.json"
)
DEFAULT_GOLD = RUNTIME_ROOT / "data" / "eval" / "literary_m4f" / "wh_ch01_registry_gold_v1.json"
OUTPUT_TOKEN_CAP = 8192
# Full-chapter stress cases can carry a larger, still bounded candidate roster.
# This is a transport guard only; it does not alter the prompt or response contract.
INPUT_TOKEN_CAP = 30000
CORE_FILE = (
    RUNTIME_ROOT / "pipeline" / "literary" / "b0_entity_inventory_auditor_experiment.py"
)


class InventoryAuditorExperimentError(RuntimeError):
    pass


def _now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()


def _load_json(path: Path, label: str) -> dict[str, Any]:
    value = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise InventoryAuditorExperimentError(f"{label} must be a JSON object")
    return value


def _transport_config(model_id: str = "gpt-5.4") -> Any:
    if model_id not in ALLOWED_MODEL_IDS:
        raise InventoryAuditorExperimentError(
            f"unsupported comparison model: {model_id}"
        )
    semantic = replace(
        draft_semantic_config_v4(),
        auditor_model_id=model_id,
        auditor_input_token_cap=INPUT_TOKEN_CAP,
    )
    transport = draft_transport_config_v4(semantic)
    gate_suffix = "gpt54" if model_id == "gpt-5.4" else "mini"
    role_gate_ids = dict(transport.role_quota_gate_ids)
    role_gate_ids["auditor"] = tuple(
        f"{bucket}-{gate_suffix}" for bucket in ("openai-row2", "openai-row1")
    )
    return replace(transport, role_quota_gate_ids=role_gate_ids)


def build_auditor_envelope(
    *,
    document_path: Path,
    inventory_path: Path,
    design_doc: Path,
    chapter_id: str,
    model_id: str = "gpt-5.4",
) -> tuple[dict[str, Any], Any, Mapping[str, Any], Mapping[str, Any], Any]:
    _document, chapter = _load_document(document_path, chapter_id)
    inventory = _load_json(inventory_path, "source inventory")
    transport = _transport_config(model_id)
    request = render_inventory_auditor_request(
        chapter=chapter,
        inventory=inventory,
        design_doc=design_doc,
        model_id=transport.auditor_model_id,
        reasoning_effort=transport.auditor_reasoning_effort,
        temperature=transport.auditor_temperature,
        seed=transport.auditor_seed,
        max_output_tokens=OUTPUT_TOKEN_CAP,
    )
    prompt_tokens = estimate_registry_prompt_tokens(request.messages)
    if prompt_tokens > INPUT_TOKEN_CAP:
        raise InventoryAuditorExperimentError(
            f"auditor input {prompt_tokens} exceeds cap {INPUT_TOKEN_CAP}"
        )
    body = {
        "schema_version": RUN_SCHEMA_VERSION,
        "auditor_schema_version": AUDITOR_SCHEMA_VERSION,
        "chapter_id": chapter_id,
        "document_sha256": file_sha256(document_path),
        "source_inventory_sha256": file_sha256(inventory_path),
        "source_inventory_hash": inventory.get("inventory_hash"),
        "design_doc_sha256": file_sha256(design_doc),
        "prompt_id": PROMPT_ID,
        "prompt_sha256": request.prompt_sha256,
        "response_schema_hash": request.response_schema_hash,
        "request_fingerprint": request.request_fingerprint,
        "git_head": _git_head(),
        "model_contract": {
            "model_id": transport.auditor_model_id,
            "reasoning_effort": transport.auditor_reasoning_effort,
            "temperature": transport.auditor_temperature,
            "seed": transport.auditor_seed,
            "max_output_tokens": OUTPUT_TOKEN_CAP,
            "input_token_cap": INPUT_TOKEN_CAP,
        },
        "estimated_prompt_tokens": prompt_tokens,
        "reserved_tokens": prompt_tokens + OUTPUT_TOKEN_CAP,
        "transport_config_hash": transport.config_hash,
        "quota_policy": _quota_policy(),
        "runtime_artifact_hashes": {
            CORE_FILE.relative_to(RUNTIME_ROOT.parent).as_posix(): file_sha256(CORE_FILE),
            Path(__file__).resolve().relative_to(RUNTIME_ROOT.parent).as_posix(): file_sha256(
                Path(__file__).resolve()
            ),
        },
        "gold_access_policy": "POST_AUDITED_INVENTORY_PERSIST_ONLY_NOT_IN_REQUEST",
        "phase_boundary": "one_chapter_one_auditor_call_read_only_no_registry_publish_no_retry",
    }
    return (
        {**body, "envelope_hash": canonical_hash(body)},
        request,
        chapter,
        inventory,
        transport,
    )


def run_dry(
    *,
    document_path: Path,
    inventory_path: Path,
    design_doc: Path,
    output_dir: Path,
    chapter_id: str,
    model_id: str = "gpt-5.4",
) -> dict[str, Any]:
    output = _ensure_empty_output(output_dir)
    envelope, request, _chapter, _inventory, _transport = build_auditor_envelope(
        document_path=document_path,
        inventory_path=inventory_path,
        design_doc=design_doc,
        chapter_id=chapter_id,
        model_id=model_id,
    )
    report = {
        "schema_version": "b0_entity_inventory_auditor_dry_v1_2",
        "status": "dry_rendered_no_api",
        "chapter_id": chapter_id,
        "model_id": envelope["model_contract"]["model_id"],
        "envelope_hash": envelope["envelope_hash"],
        "request_fingerprint": request.request_fingerprint,
        "source_inventory_hash": envelope["source_inventory_hash"],
        "estimated_prompt_tokens": envelope["estimated_prompt_tokens"],
        "output_token_cap": envelope["model_contract"]["max_output_tokens"],
        "reserved_tokens": envelope["reserved_tokens"],
        "audit_case_count": len(request.sections["audit_case_manifest"]),
        "entity_count": len(request.sections["entity_roster"]),
        "glossary_count": len(request.sections["glossary_roster"]),
        "unresolved_count": len(request.sections["unresolved_roster"]),
    }
    _write_json(output / f"run_envelope_{envelope['envelope_hash'][:12]}.json", envelope)
    _write_json(output / "request.json", request.to_dict())
    _write_json(output / "dry_report.json", report)
    return report


def run_replay(
    *,
    document_path: Path,
    inventory_path: Path,
    design_doc: Path,
    output_dir: Path,
    raw_result_path: Path,
    source_request_path: Path,
    gold_path: Path | None,
    chapter_id: str,
    model_id: str = "gpt-5.4",
) -> dict[str, Any]:
    """Replay persisted model bytes through the current validator without an API call."""

    output = _ensure_empty_output(output_dir)
    envelope, request, chapter, inventory, _transport = build_auditor_envelope(
        document_path=document_path,
        inventory_path=inventory_path,
        design_doc=design_doc,
        chapter_id=chapter_id,
        model_id=model_id,
    )
    raw_result = _load_json(raw_result_path, "persisted raw result")
    source_request = _load_json(source_request_path, "persisted source request")
    parsed = raw_result.get("parsed_json")
    if not isinstance(parsed, Mapping):
        raise InventoryAuditorExperimentError("persisted raw result has no parsed JSON object")
    audited = validate_and_apply_auditor_response(
        parsed,
        chapter=chapter,
        inventory=inventory,
        request_fingerprint=request.request_fingerprint,
    )
    _write_json(output / "audited_inventory.json", audited)
    evaluation = None
    if gold_path is not None:
        gold = _load_json(gold_path, "gold evaluation file")
        evaluation = evaluate_inventory_against_gold(audited, gold)
        _write_json(output / "post_audit_gold_evaluation.json", evaluation)
    source_registry_request = source_request.get("registry_request") or {}
    report = {
        "schema_version": "b0_entity_inventory_auditor_replay_report_v1",
        "status": "accepted_offline_compatibility_replay",
        "chapter_id": chapter_id,
        "model_id": envelope["model_contract"]["model_id"],
        "current_envelope_hash": envelope["envelope_hash"],
        "current_prompt_id": PROMPT_ID,
        "current_request_fingerprint": request.request_fingerprint,
        "source_prompt_id": source_registry_request.get("prompt_id"),
        "source_prompt_sha256": source_registry_request.get("prompt_sha256"),
        "source_request_fingerprint": source_registry_request.get("request_fingerprint"),
        "source_request_sha256": file_sha256(source_request_path),
        "source_raw_result_sha256": file_sha256(raw_result_path),
        "source_inventory_hash": inventory.get("inventory_hash"),
        "audited_inventory_hash": audited["audited_inventory_hash"],
        **audited["audit_summary"],
        "confirmed_entity_recall": (
            evaluation.get("confirmed_entity_recall") if evaluation else None
        ),
        "glossary_recall": evaluation.get("glossary_recall") if evaluation else None,
        "wrong_merge_count": evaluation.get("wrong_merge_count") if evaluation else None,
        "wrong_split_count": evaluation.get("wrong_split_count") if evaluation else None,
        "wrong_kind_count": evaluation.get("wrong_kind_count") if evaluation else None,
        "api_calls": 0,
        "compatibility_only_not_fresh_v1_2_generation": True,
        "production_publish_performed": False,
        "gold_loaded_only_after_audited_inventory_persisted": gold_path is not None,
        "completed_at": _now(),
    }
    _write_json(output / "replay_report.json", report)
    return report


def run_live(
    *,
    document_path: Path,
    inventory_path: Path,
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
    envelope, request, chapter, inventory, transport = build_auditor_envelope(
        document_path=document_path,
        inventory_path=inventory_path,
        design_doc=design_doc,
        chapter_id=chapter_id,
        model_id=model_id,
    )
    if approved_envelope_hash != envelope["envelope_hash"]:
        raise InventoryAuditorExperimentError(
            f"approved envelope mismatch; current hash is {envelope['envelope_hash']}"
        )
    preflight = scan_current_utc_usage(roots=usage_roots, exclude_root=output)
    if preflight["unknown_bucket_rows"]:
        raise InventoryAuditorExperimentError(
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
        "source_inventory_hash": inventory.get("inventory_hash"),
        "gold_loaded_before_response": False,
        "production_publish_performed": False,
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
        audited = validate_and_apply_auditor_response(
            raw,
            chapter=chapter,
            inventory=inventory,
            request_fingerprint=request.request_fingerprint,
        )
        _write_json(output / "audited_inventory.json", audited)
        executor.record_chapter_validation(
            chapter_id=chapter_id,
            status="accepted_inventory_audit_experiment",
            payload={"audited_inventory_hash": audited["audited_inventory_hash"]},
        )

        evaluation = None
        if gold_path is not None:
            gold = _load_json(gold_path, "gold evaluation file")
            evaluation = evaluate_inventory_against_gold(audited, gold)
            _write_json(output / "post_audit_gold_evaluation.json", evaluation)

        summary = audited["audit_summary"]
        report = {
            "schema_version": "b0_entity_inventory_auditor_live_report_v1_2",
            "status": "accepted_one_call_auditor_experiment",
            "chapter_id": chapter_id,
            "model_id": envelope["model_contract"]["model_id"],
            "envelope_hash": envelope["envelope_hash"],
            "source_inventory_hash": inventory.get("inventory_hash"),
            "audited_inventory_hash": audited["audited_inventory_hash"],
            **summary,
            "confirmed_entity_recall": (
                evaluation.get("confirmed_entity_recall") if evaluation else None
            ),
            "glossary_recall": evaluation.get("glossary_recall") if evaluation else None,
            "wrong_merge_count": evaluation.get("wrong_merge_count") if evaluation else None,
            "wrong_split_count": evaluation.get("wrong_split_count") if evaluation else None,
            "wrong_kind_count": evaluation.get("wrong_kind_count") if evaluation else None,
            "usage": _aggregate_live_usage(executor.records),
            "gold_loaded_only_after_audited_inventory_persisted": gold_path is not None,
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
            "schema_version": "b0_entity_inventory_auditor_failure_v1_2",
            "status": "halted_fail_closed",
            "chapter_id": chapter_id,
            "error_type": type(exc).__name__,
            "message": _redacted_error(exc),
            "usage": _aggregate_live_usage(executor.records),
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
    parser = argparse.ArgumentParser(description="One-call full-chapter B0 candidate Auditor")
    parser.add_argument("mode", choices=("dry", "replay", "live"))
    parser.add_argument("--document", type=Path, default=DEFAULT_DOCUMENT)
    parser.add_argument("--inventory", type=Path, default=DEFAULT_INVENTORY)
    parser.add_argument("--design-doc", type=Path, default=DEFAULT_DESIGN_DOC)
    parser.add_argument("--frozen-db", type=Path, default=DEFAULT_FROZEN_DB)
    parser.add_argument("--chapter-id", default=DEFAULT_CHAPTER_ID)
    parser.add_argument("--model-id", choices=sorted(ALLOWED_MODEL_IDS), default="gpt-5.4")
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--approved-envelope-hash")
    parser.add_argument("--raw-result", type=Path)
    parser.add_argument("--source-request", type=Path)
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
            inventory_path=args.inventory,
            design_doc=args.design_doc,
            output_dir=args.output_dir,
            chapter_id=args.chapter_id,
            model_id=args.model_id,
        )
    elif args.mode == "replay":
        if not args.raw_result or not args.source_request:
            raise InventoryAuditorExperimentError(
                "replay mode requires --raw-result and --source-request"
            )
        report = run_replay(
            document_path=args.document,
            inventory_path=args.inventory,
            design_doc=args.design_doc,
            output_dir=args.output_dir,
            raw_result_path=args.raw_result,
            source_request_path=args.source_request,
            gold_path=None if args.no_gold_eval else args.gold,
            chapter_id=args.chapter_id,
            model_id=args.model_id,
        )
    else:
        if not args.approved_envelope_hash or not args.key_1 or not args.key_2:
            raise InventoryAuditorExperimentError(
                "live mode requires approved envelope hash and two key file paths"
            )
        report = run_live(
            document_path=args.document,
            inventory_path=args.inventory,
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
