from __future__ import annotations

import argparse
from dataclasses import replace
from datetime import UTC, datetime
import json
from pathlib import Path
from typing import Any, Mapping, Sequence

from pipeline.agents.provider_profile import ResolvedCredential
from pipeline.literary.b0_entity_conflict_auditor import (
    CONFLICT_SCHEMA_VERSION,
    PROMPT_ID,
    build_identity_conflict_manifest,
    entity_conflict_response_schema,
    normalize_source_boundary_violations,
    render_entity_conflict_request,
    validate_and_apply_conflict_response,
)
from pipeline.literary.b0_entity_inventory_experiment import evaluate_inventory_against_gold
from pipeline.literary.checkpoint import canonical_hash, canonical_json, file_sha256
from pipeline.literary.openai_compatible_structured_call_v1 import (
    call_openai_compatible_structured_v1,
)
from pipeline.literary.structured_output_policy_v1 import (
    StructuredOutputContract,
    openai_response_format,
    validate_structured_payload,
)
from pipeline.literary.structured_prompt_reserve_v1 import (
    structured_prompt_reserve_v1,
)
from pipeline.literary.chapter_registry_v4 import estimate_registry_prompt_tokens
from pipeline.scripts.run_chapter_registry_v3_real import (
    PersistedRealExecutorV3,
    RealOpenAIRegistryExecutorV3,
)
from pipeline.scripts.run_chapter_registry_v4_real import (
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
from pipeline.scripts.run_b0_inventory_gemini_comparison import (
    scan_current_utc_gemini_usage,
)


RUN_SCHEMA_VERSION = "b0_entity_conflict_auditor_experiment_run_v1"
DEFAULT_CHAPTER_ID = "wh_ch17"
DEFAULT_INVENTORY = (
    RUNTIME_ROOT
    / "data"
    / "reports"
    / "literary_m4f_b0_inventory_gemini35_ch17_real_20260715_1"
    / "inventory.json"
)
DEFAULT_GOLD = (
    RUNTIME_ROOT / "data" / "eval" / "literary_m4f" / "wh_ch17_registry_gold_v1.json"
)
MODEL_ID = "gpt-5.4"
INPUT_TOKEN_CAP = 10_000
OUTPUT_TOKEN_CAP = 4_096
CORE_FILE = RUNTIME_ROOT / "pipeline" / "literary" / "b0_entity_conflict_auditor.py"


class ConflictAuditorExperimentError(RuntimeError):
    pass


def _now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()


def _load_json(path: Path, label: str) -> dict[str, Any]:
    value = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ConflictAuditorExperimentError(f"{label} must be a JSON object")
    return value


def _transport_config(
    bucket_order: Sequence[str] = ("openai-row2", "openai-row1"),
    *,
    model_id: str = MODEL_ID,
) -> Any:
    if (
        not bucket_order
        or len(set(bucket_order)) != len(bucket_order)
        or any(
            not isinstance(bucket, str)
            or not bucket
            or len(bucket) > 96
            or any(char.isspace() for char in bucket)
            for bucket in bucket_order
        )
    ):
        raise ConflictAuditorExperimentError("invalid OpenAI bucket order")
    semantic = replace(
        draft_semantic_config_v4(),
        auditor_model_id=model_id,
        auditor_reasoning_effort="none",
        auditor_output_token_cap=OUTPUT_TOKEN_CAP,
        auditor_input_token_cap=INPUT_TOKEN_CAP,
    )
    transport = draft_transport_config_v4(semantic)
    available_gates = dict(transport.quota_gates)
    role_gate_ids: dict[str, tuple[str, ...]] = {}
    quota_gates: dict[str, Mapping[str, Any]] = {}
    for role in ("b0", "b1", "auditor"):
        role_model = str(getattr(transport, f"{role}_model_id"))
        existing_by_bucket = {
            str(gate["quota_bucket_id"]): gate_id
            for gate_id, gate in available_gates.items()
            if str(gate["model_id"]) == role_model
        }
        selected: list[str] = []
        for bucket in bucket_order:
            gate_id = existing_by_bucket.get(bucket)
            if gate_id is None:
                gate_id = role + "-" + canonical_hash(
                    {"quota_bucket_id": bucket, "model_id": role_model}
                )[:16]
                available_gates[gate_id] = {
                    "quota_bucket_id": bucket,
                    "model_id": role_model,
                    "internal_utc_day_token_cap": 10_000_000,
                }
            selected.append(gate_id)
            quota_gates[gate_id] = available_gates[gate_id]
        role_gate_ids[role] = tuple(selected)
    return replace(
        transport,
        role_quota_gate_ids=role_gate_ids,
        quota_gates=quota_gates,
    )


def build_envelope(
    *,
    document_path: Path,
    inventory_path: Path,
    design_doc: Path,
    chapter_id: str,
    bucket_order: Sequence[str] = ("openai-row2", "openai-row1"),
    model_id: str = MODEL_ID,
    structured_output_contract: StructuredOutputContract | None = None,
) -> tuple[dict[str, Any], Any, Mapping[str, Any], Mapping[str, Any], Any]:
    _document, chapter = _load_document(document_path, chapter_id)
    inventory = _load_json(inventory_path, "source inventory")
    transport = _transport_config(bucket_order, model_id=model_id)
    request = render_entity_conflict_request(
        chapter=chapter,
        inventory=inventory,
        design_doc=design_doc,
        model_id=model_id,
        reasoning_effort="none",
        temperature=transport.auditor_temperature,
        seed=transport.auditor_seed,
        max_output_tokens=OUTPUT_TOKEN_CAP,
    )
    response_schema = entity_conflict_response_schema()
    if (
        structured_output_contract is not None
        and structured_output_contract.canonical_schema_hash
        != canonical_hash(response_schema)
    ):
        raise ConflictAuditorExperimentError(
            "structured-output contract differs from the local Auditor schema"
        )
    reserve = structured_prompt_reserve_v1(
        messages=request.messages,
        response_schema=response_schema,
        output_token_cap=OUTPUT_TOKEN_CAP,
    )
    prompt_tokens = reserve.prompt_token_reserve
    if prompt_tokens > INPUT_TOKEN_CAP:
        raise ConflictAuditorExperimentError(
            f"conflict Auditor input {prompt_tokens} exceeds cap {INPUT_TOKEN_CAP}"
        )
    manifest = build_identity_conflict_manifest(inventory, chapter)
    body = {
        "schema_version": RUN_SCHEMA_VERSION,
        "conflict_schema_version": CONFLICT_SCHEMA_VERSION,
        "chapter_id": chapter_id,
        "document_sha256": file_sha256(document_path),
        "source_inventory_sha256": file_sha256(inventory_path),
        "source_inventory_hash": inventory.get("inventory_hash"),
        "conflict_manifest_hash": manifest["manifest_hash"],
        "design_doc_sha256": file_sha256(design_doc),
        "prompt_id": PROMPT_ID,
        "prompt_sha256": request.prompt_sha256,
        "response_schema_hash": request.response_schema_hash,
        "structured_output_contract": (
            structured_output_contract.to_payload()
            if structured_output_contract is not None
            else {
                "schema_version": "literary_structured_output_legacy_v1",
                "effective_mode": "legacy_json_object_unsealed",
                "local_validation_required": True,
            }
        ),
        "request_fingerprint": request.request_fingerprint,
        "git_head": _git_head(),
        "model_contract": {
            "model_id": model_id,
            "reasoning_effort": "none",
            "temperature": transport.auditor_temperature,
            "seed": transport.auditor_seed,
            "max_output_tokens": OUTPUT_TOKEN_CAP,
            "input_token_cap": INPUT_TOKEN_CAP,
        },
        "estimated_prompt_tokens": reserve.message_token_estimate,
        "transport_prompt_token_reserve": prompt_tokens,
        "response_schema_utf8_bytes": reserve.response_schema_utf8_bytes,
        "reserved_tokens": prompt_tokens + OUTPUT_TOKEN_CAP,
        "component_count": len(manifest["components"]),
        "conflict_candidate_count": len(manifest["conflict_candidate_ids"]),
        "clean_candidate_count": len(manifest["clean_candidate_ids"]),
        "source_block_count": sum(
            len(component["source_blocks"]) for component in manifest["components"]
        ),
        "transport_config_hash": transport.config_hash,
        "quota_policy": _quota_policy(),
        "runtime_artifact_hashes": {
            CORE_FILE.relative_to(RUNTIME_ROOT.parent).as_posix(): file_sha256(CORE_FILE),
            Path(__file__).resolve().relative_to(RUNTIME_ROOT.parent).as_posix(): file_sha256(
                Path(__file__).resolve()
            ),
        },
        "gold_access_policy": "POST_AUDITED_INVENTORY_PERSIST_ONLY_NOT_IN_REQUEST",
        "phase_boundary": "one_conflict_only_call_read_only_no_publish_no_retry",
    }
    return {**body, "envelope_hash": canonical_hash(body)}, request, chapter, inventory, transport


def run_dry(
    *,
    document_path: Path,
    inventory_path: Path,
    design_doc: Path,
    output_dir: Path,
    chapter_id: str,
    bucket_order: Sequence[str] = ("openai-row2", "openai-row1"),
    model_id: str = MODEL_ID,
    structured_output_contract: StructuredOutputContract | None = None,
) -> dict[str, Any]:
    output = _ensure_empty_output(output_dir)
    envelope, request, chapter, inventory, _transport = build_envelope(
        document_path=document_path,
        inventory_path=inventory_path,
        design_doc=design_doc,
        chapter_id=chapter_id,
        bucket_order=bucket_order,
        model_id=model_id,
        structured_output_contract=structured_output_contract,
    )
    manifest = build_identity_conflict_manifest(inventory, chapter)
    report = {
        "schema_version": "b0_entity_conflict_auditor_dry_report_v1",
        "status": "dry_rendered_no_api",
        "chapter_id": chapter_id,
        "model_id": model_id,
        "envelope_hash": envelope["envelope_hash"],
        "request_fingerprint": request.request_fingerprint,
        "estimated_prompt_tokens": envelope["estimated_prompt_tokens"],
        "output_token_cap": OUTPUT_TOKEN_CAP,
        "reserved_tokens": envelope["reserved_tokens"],
        "component_count": envelope["component_count"],
        "conflict_candidate_count": envelope["conflict_candidate_count"],
        "clean_candidate_count": envelope["clean_candidate_count"],
        "source_block_count": envelope["source_block_count"],
        "component_shapes": [
            {
                "component_id": row["component_id"],
                "candidate_count": len(row["candidate_ids"]),
                "contested_surface_count": len(row["contested_surfaces"]),
                "source_block_count": len(row["source_blocks"]),
            }
            for row in manifest["components"]
        ],
        "production_publish_performed": False,
        "structured_output_contract": envelope["structured_output_contract"],
    }
    _write_json(output / f"run_envelope_{envelope['envelope_hash'][:12]}.json", envelope)
    _write_json(output / "conflict_manifest.json", manifest)
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
    gold_path: Path | None,
    chapter_id: str,
    bucket_order: Sequence[str] = ("openai-row2", "openai-row1"),
) -> dict[str, Any]:
    output = _ensure_empty_output(output_dir)
    envelope, request, chapter, inventory, _transport = build_envelope(
        document_path=document_path,
        inventory_path=inventory_path,
        design_doc=design_doc,
        chapter_id=chapter_id,
        bucket_order=bucket_order,
    )
    raw_result = _load_json(raw_result_path, "persisted raw result")
    parsed = raw_result.get("parsed_json")
    if not isinstance(parsed, Mapping):
        raise ConflictAuditorExperimentError("persisted raw result has no parsed JSON object")
    normalized, source_boundary_normalizations = normalize_source_boundary_violations(
        parsed,
        chapter=chapter,
        inventory=inventory,
    )
    validate_structured_payload(
        normalized,
        canonical_schema=entity_conflict_response_schema(),
    )
    _write_json(
        output / "source_boundary_normalizations.json",
        {
            "schema_version": "local_auditor_source_boundary_normalizations_v1",
            "normalizations": source_boundary_normalizations,
            "normalization_count": len(source_boundary_normalizations),
        },
    )
    audited = validate_and_apply_conflict_response(
        normalized,
        chapter=chapter,
        inventory=inventory,
        request_fingerprint=request.request_fingerprint,
        source_boundary_normalizations=source_boundary_normalizations,
    )
    _write_json(output / "conflict_audited_inventory.json", audited)
    evaluation = None
    if gold_path is not None:
        gold = _load_json(gold_path, "gold evaluation file")
        evaluation = evaluate_inventory_against_gold(audited, gold)
        _write_json(output / "post_conflict_audit_gold_evaluation.json", evaluation)
    report = {
        "schema_version": "b0_entity_conflict_auditor_replay_report_v1",
        "status": "accepted_offline_replay_no_api",
        "chapter_id": chapter_id,
        "model_id": MODEL_ID,
        "current_envelope_hash": envelope["envelope_hash"],
        "current_request_fingerprint": request.request_fingerprint,
        "source_raw_result_sha256": file_sha256(raw_result_path),
        "source_inventory_hash": inventory.get("inventory_hash"),
        "conflict_audited_inventory_hash": audited["conflict_audited_inventory_hash"],
        **audited["conflict_summary"],
        "confirmed_entity_recall": (
            evaluation.get("confirmed_entity_recall") if evaluation else None
        ),
        "wrong_merge_count": evaluation.get("wrong_merge_count") if evaluation else None,
        "wrong_split_count": evaluation.get("wrong_split_count") if evaluation else None,
        "wrong_kind_count": evaluation.get("wrong_kind_count") if evaluation else None,
        "api_calls": 0,
        "gold_loaded_only_after_audited_inventory_persisted": gold_path is not None,
        "production_publish_performed": False,
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
    bucket_order: Sequence[str] = ("openai-row2", "openai-row1"),
    structured_output_contract: StructuredOutputContract | None = None,
    resolved_credential: ResolvedCredential | None = None,
    provider_profile_hash: str | None = None,
    model_id: str = MODEL_ID,
) -> dict[str, Any]:
    output = _ensure_empty_output(output_dir)
    frozen_hash = _verify_frozen_db(frozen_db)
    envelope, request, chapter, inventory, transport = build_envelope(
        document_path=document_path,
        inventory_path=inventory_path,
        design_doc=design_doc,
        chapter_id=chapter_id,
        bucket_order=bucket_order,
        model_id=model_id,
        structured_output_contract=structured_output_contract,
    )
    if approved_envelope_hash != envelope["envelope_hash"]:
        raise ConflictAuditorExperimentError(
            f"approved envelope mismatch; current hash is {envelope['envelope_hash']}"
        )
    preflight = (
        scan_current_utc_gemini_usage(
            roots=usage_roots,
            allowed_bucket_ids=bucket_order,
        )
        if resolved_credential is not None
        else scan_current_utc_usage(roots=usage_roots, exclude_root=output)
    )
    if preflight["unknown_bucket_rows"]:
        raise ConflictAuditorExperimentError(
            "quota preflight found current-day OpenAI rows with unknown buckets"
        )
    _write_json(output / f"run_envelope_{envelope['envelope_hash'][:12]}.json", envelope)
    _write_json(output / "request.json", request.to_dict())
    _write_json(output / "quota_preflight.json", preflight)
    manifest = {
        "schema_version": RUN_SCHEMA_VERSION,
        "status": "running",
        "chapter_id": chapter_id,
        "model_id": model_id,
        "provider_profile_hash": provider_profile_hash,
        "envelope_hash": envelope["envelope_hash"],
        "source_inventory_hash": inventory.get("inventory_hash"),
        "gold_loaded_before_response": False,
        "production_publish_performed": False,
        "frozen_db_sha256_before": frozen_hash,
        "structured_output_contract": envelope["structured_output_contract"],
        "started_at": _now(),
    }
    _write_json(output / "run_manifest.json", manifest)

    executor: PersistedRealExecutorV3 | None = None
    try:
        if resolved_credential is not None:
            if (
                resolved_credential.provider != "openai"
                or resolved_credential.quota_bucket_id not in bucket_order
            ):
                raise ConflictAuditorExperimentError(
                    "resolved OpenAI-compatible Auditor credential does not match"
                )
            if structured_output_contract is None:
                raise ConflictAuditorExperimentError(
                    "OpenAI-compatible Auditor requires structured output"
                )
            direct = call_openai_compatible_structured_v1(
                credential=resolved_credential,
                model_id=model_id,
                messages=request.messages,
                contract=structured_output_contract,
                schema_name="literary_local_conflict_auditor_v1",
                cache_path=(
                    output
                    / "cache"
                    / resolved_credential.quota_bucket_id
                    / "auditor.sqlite3"
                ),
                tag=f"literary_local_auditor:{chapter_id}",
                prompt_token_cap=INPUT_TOKEN_CAP,
                max_output_tokens=OUTPUT_TOKEN_CAP,
                temperature=1.0,
                seed=20260715,
                reasoning_effort="none",
            )
            if direct.parsed_json is None:
                raise ConflictAuditorExperimentError(
                    direct.json_error or "Auditor returned no parsed JSON object"
                )
            _write_json(
                output / "raw_result.json",
                {
                    "schema_version": "b0_entity_conflict_auditor_raw_v1",
                    "model": direct.model,
                    "provider": resolved_credential.provider,
                    "quota_bucket_id": resolved_credential.quota_bucket_id,
                    "credential_revision": resolved_credential.credential_revision,
                    "credential_commitment": resolved_credential.commitment,
                    "response_text": direct.response_text,
                    "parsed_json": direct.parsed_json,
                    "json_error": direct.json_error,
                    "usage": dict(direct.usage),
                    "latency_ms": direct.latency_ms,
                    "cost_usd": direct.cost_usd,
                    "from_cache": direct.from_cache,
                    "cache_key": direct.cache_key,
                    "completed_at": _now(),
                },
            )
            normalized_raw, source_boundary_normalizations = (
                normalize_source_boundary_violations(
                    direct.parsed_json,
                    chapter=chapter,
                    inventory=inventory,
                )
            )
            validate_structured_payload(
                normalized_raw,
                canonical_schema=entity_conflict_response_schema(),
            )
            _write_json(
                output / "source_boundary_normalizations.json",
                {
                    "schema_version": "local_auditor_source_boundary_normalizations_v1",
                    "normalizations": source_boundary_normalizations,
                    "normalization_count": len(source_boundary_normalizations),
                },
            )
            audited = validate_and_apply_conflict_response(
                normalized_raw,
                chapter=chapter,
                inventory=inventory,
                request_fingerprint=request.request_fingerprint,
                source_boundary_normalizations=source_boundary_normalizations,
            )
            _write_json(output / "conflict_audited_inventory.json", audited)
            report = {
                "schema_version": "b0_entity_conflict_auditor_live_report_v1",
                "status": "accepted_one_conflict_only_call",
                "chapter_id": chapter_id,
                "model_id": model_id,
                "envelope_hash": envelope["envelope_hash"],
                "source_inventory_hash": inventory.get("inventory_hash"),
                "conflict_audited_inventory_hash": audited[
                    "conflict_audited_inventory_hash"
                ],
                **audited["conflict_summary"],
                "confirmed_entity_recall": None,
                "wrong_merge_count": None,
                "wrong_split_count": None,
                "wrong_kind_count": None,
                "usage": dict(direct.usage),
                "gold_loaded_only_after_audited_inventory_persisted": False,
                "production_publish_performed": False,
                "frozen_db_sha256_after": _verify_frozen_db(frozen_db),
                "completed_at": _now(),
            }
            _write_json(output / "experiment_report.json", report)
            _write_json(
                output / "run_manifest.json",
                {
                    **manifest,
                    "status": "accepted",
                    "completed_at": report["completed_at"],
                },
            )
            return report
        real = RealOpenAIRegistryExecutorV3(
            run_config=transport,
            run_root=output,
            credential_paths=key_paths,
            prior_usage_by_bucket_model=preflight["usage_by_bucket_model"],
            prior_calls_by_bucket_model=preflight["calls_by_bucket_model"],
            min_interval_seconds=float(_quota_policy()["minimum_interval_seconds"]),
            local_rpd_cap=int(_quota_policy()["local_rpd_cap_per_bucket_model"]),
            response_formats_by_role=(
                {
                    "auditor": openai_response_format(
                        structured_output_contract,
                        schema_name="literary_local_conflict_auditor_v1",
                    )
                }
                if structured_output_contract is not None
                else None
            ),
            structured_output_contracts_by_role=(
                {"auditor": structured_output_contract.to_payload()}
                if structured_output_contract is not None
                else None
            ),
        )
        executor = PersistedRealExecutorV3(
            executor=real,
            run_root=output,
            run_config=transport,
            frozen_db=frozen_db,
        )
        raw = executor.execute(request)
        normalized_raw, source_boundary_normalizations = (
            normalize_source_boundary_violations(
                raw,
                chapter=chapter,
                inventory=inventory,
            )
        )
        validate_structured_payload(
            normalized_raw,
            canonical_schema=entity_conflict_response_schema(),
        )
        _write_json(
            output / "source_boundary_normalizations.json",
            {
                "schema_version": "local_auditor_source_boundary_normalizations_v1",
                "normalizations": source_boundary_normalizations,
                "normalization_count": len(source_boundary_normalizations),
            },
        )
        audited = validate_and_apply_conflict_response(
            normalized_raw,
            chapter=chapter,
            inventory=inventory,
            request_fingerprint=request.request_fingerprint,
            source_boundary_normalizations=source_boundary_normalizations,
        )
        _write_json(output / "conflict_audited_inventory.json", audited)
        executor.record_chapter_validation(
            chapter_id=chapter_id,
            status="accepted_b0_identity_conflict_audit_experiment",
            payload={
                "conflict_audited_inventory_hash": audited[
                    "conflict_audited_inventory_hash"
                ]
            },
        )

        evaluation = None
        if gold_path is not None:
            gold = _load_json(gold_path, "gold evaluation file")
            evaluation = evaluate_inventory_against_gold(audited, gold)
            _write_json(output / "post_conflict_audit_gold_evaluation.json", evaluation)

        report = {
            "schema_version": "b0_entity_conflict_auditor_live_report_v1",
            "status": "accepted_one_conflict_only_call",
            "chapter_id": chapter_id,
            "model_id": model_id,
            "envelope_hash": envelope["envelope_hash"],
            "source_inventory_hash": inventory.get("inventory_hash"),
            "conflict_audited_inventory_hash": audited[
                "conflict_audited_inventory_hash"
            ],
            **audited["conflict_summary"],
            "confirmed_entity_recall": (
                evaluation.get("confirmed_entity_recall") if evaluation else None
            ),
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
        records = executor.records if executor is not None else ()
        failure = {
            "schema_version": "b0_entity_conflict_auditor_failure_v1",
            "status": "halted_fail_closed",
            "chapter_id": chapter_id,
            "error_type": type(exc).__name__,
            "message": _redacted_error(exc),
            "usage": _aggregate_live_usage(records),
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
    parser = argparse.ArgumentParser(description="Conflict-only B0 identity Auditor")
    parser.add_argument("mode", choices=("dry", "replay", "live"))
    parser.add_argument("--document", type=Path, default=DEFAULT_DOCUMENT)
    parser.add_argument("--inventory", type=Path, default=DEFAULT_INVENTORY)
    parser.add_argument("--design-doc", type=Path, default=DEFAULT_DESIGN_DOC)
    parser.add_argument("--frozen-db", type=Path, default=DEFAULT_FROZEN_DB)
    parser.add_argument("--chapter-id", default=DEFAULT_CHAPTER_ID)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--approved-envelope-hash")
    parser.add_argument("--raw-result", type=Path)
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
        )
    elif args.mode == "replay":
        if not args.raw_result:
            raise ConflictAuditorExperimentError("replay mode requires --raw-result")
        report = run_replay(
            document_path=args.document,
            inventory_path=args.inventory,
            design_doc=args.design_doc,
            output_dir=args.output_dir,
            raw_result_path=args.raw_result,
            gold_path=None if args.no_gold_eval else args.gold,
            chapter_id=args.chapter_id,
        )
    else:
        if not args.approved_envelope_hash or not args.key_1 or not args.key_2:
            raise ConflictAuditorExperimentError(
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
        )
    print(canonical_json(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
