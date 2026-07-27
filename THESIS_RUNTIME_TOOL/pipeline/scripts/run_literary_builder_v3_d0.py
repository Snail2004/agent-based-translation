from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from datetime import UTC, datetime
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import shutil
import tempfile
from typing import Any, Callable, Mapping, Sequence
import uuid

from pipeline.agents.llm_client import LLMClient, estimate_prompt_tokens
from pipeline.agents.llm_config import LLMConfig, load_llm_config
from pipeline.literary.b4_handoff_v3 import (
    assemble_b4_input_bundle,
    build_book_source_manifest,
    verify_b4_input_bundle_identity,
)
from pipeline.literary.builder_pilot import (
    RESPONSE_FORMAT_JSON,
    build_literary_windows,
    load_system_prompt_from_design,
    load_wuthering_heights_epub,
    select_chapters,
)
from pipeline.literary.builder_v3_pipeline import (
    DEFAULT_SUMMARY_K,
    DEFAULT_TAIL_K,
    DEFAULT_WINDOW_MAX_BLOCKS,
    DEFAULT_WINDOW_TARGET_TOKENS,
    EXECUTION_MODE_REAL_API,
    RealStageExecutor,
    RealStageSpec,
    _load_m1_chain,
    real_execution_contract_hash,
    run_m1_v3,
    run_m2_v3,
)
from pipeline.literary.checkpoint import canonical_hash, canonical_json, file_sha256
from pipeline.literary.checkpoint_v3 import contract_versions, write_json_exclusive
from pipeline.literary.draft_story_bible import build_identity_target_manifest
from pipeline.literary.source_anchor import nfc_block_string


PREFLIGHT_SCHEMA_VERSION = "literary_m4f_d0_preflight_v3"
RUN_REPORT_SCHEMA_VERSION = "literary_m4f_d0_run_report_v2"
RETRY_POLICY_VERSION = "chapter_atomic_cumulative_fresh_retry_rate_v1"
CAPABILITY_PART = "literary_m4f_s5c_slice"
CHAPTER_IDS = ("wh_ch01", "wh_ch02", "wh_ch03", "wh_ch04")
PROMPT_IDS = {
    "b1": "literary_lexicon_v3",
    "b2": "literary_narrative_v3",
    "b3": "literary_digest_v3",
}
MINI_STAGES = ("b1", "b2")
GPT_STAGES = ("b3",)
MAX_FRESH_TECHNICAL_RETRY_RATE = 0.10
M1_RESUME_SCHEMA_VERSION = "literary_m4f_d0_m1_resume_source_v1"

RUNTIME_ROOT = Path(__file__).resolve().parents[2]
REPO_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_SOURCE = (
    REPO_ROOT
    / "reference"
    / "literary"
    / "wuthering_heights"
    / "en"
    / "wuthering_heights_gutenberg_768_epub3_images.epub"
)
DEFAULT_DESIGN_DOC = REPO_ROOT / "design" / "LITERARY_PROMPT_DESIGN.md"
DEFAULT_MINI_CONFIG = RUNTIME_ROOT / "pipeline" / "configs" / "llm_m4f_d0_mini.yaml"
DEFAULT_GPT_CONFIG = RUNTIME_ROOT / "pipeline" / "configs" / "llm_s5c_slice_gpt54.yaml"
DEFAULT_OUTPUT_PARENT = (
    RUNTIME_ROOT
    / "data"
    / "reports"
    / CAPABILITY_PART
    / "d0_callgraph_arm_a"
)
RUNTIME_CONTRACT_FILES = (
    Path(__file__).resolve(),
    RUNTIME_ROOT / "pipeline" / "literary" / "builder_schema_v3.py",
    RUNTIME_ROOT / "pipeline" / "literary" / "builder_validators_v3.py",
    RUNTIME_ROOT / "pipeline" / "literary" / "source_anchor.py",
    RUNTIME_ROOT / "pipeline" / "literary" / "checkpoint_v3.py",
    RUNTIME_ROOT / "pipeline" / "literary" / "builder_v3_pipeline.py",
    RUNTIME_ROOT / "pipeline" / "literary" / "b4_handoff_v3.py",
)


class D0ContractError(RuntimeError):
    """Fail-closed D0 runner/preflight contract violation."""


def _utc_now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _utc_day(value: str) -> str:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed.astimezone(UTC).date().isoformat()


def _new_run_id() -> str:
    return f"d0_{datetime.now(UTC):%Y%m%dT%H%M%SZ}_{uuid.uuid4().hex[:8]}"


def _relative_path(path: Path) -> str:
    resolved = Path(path).resolve()
    try:
        return resolved.relative_to(REPO_ROOT.resolve()).as_posix()
    except ValueError:
        return resolved.as_posix()


def _require_capability_root(path: Path) -> Path:
    resolved = Path(path).resolve()
    if CAPABILITY_PART not in resolved.parts:
        raise D0ContractError(f"D0 output escapes capability root: {resolved}")
    return resolved


def _validate_bucket_id(value: str) -> str:
    bucket = str(value).strip()
    if not bucket or not re.fullmatch(r"[A-Za-z0-9._-]+", bucket):
        raise D0ContractError("quota_bucket_id must be an opaque label")
    lowered = bucket.lower()
    if lowered.startswith("sk" + "-") or lowered.startswith("aiza") or lowered.startswith("aq.a"):
        raise D0ContractError("quota_bucket_id must not contain credential material")
    return bucket


def _ordered_blocks(chapter: Mapping[str, Any]) -> list[dict[str, Any]]:
    rows = [dict(row) for row in chapter.get("blocks") or [] if row.get("block_id")]
    rows.sort(key=lambda row: (int(row.get("order_index") or 0), str(row["block_id"])))
    return rows


def _block_view(block: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "block_id": str(block.get("block_id") or ""),
        "order_index": int(block.get("order_index") or 0),
        "block_type": str(block.get("block_type") or ""),
        "text": nfc_block_string(block),
    }


def _context_block_view(block: Mapping[str, Any], direction: str) -> dict[str, Any]:
    return {**_block_view(block), "context_only": True, "direction": direction}


def _model_input_messages(
    *,
    prompt_text: str,
    stage: str,
    chapter_id: str,
    window_id: str | None,
    allowlisted_sections: Mapping[str, Any],
) -> list[dict[str, str]]:
    model_input = {
        "stage": stage,
        "chapter_id": chapter_id,
        "window_id": window_id,
        "allowlisted_sections": json.loads(canonical_json(allowlisted_sections)),
    }
    return [
        {"role": "system", "content": prompt_text},
        {"role": "user", "content": canonical_json(model_input)},
    ]


def _prompt_artifacts(
    design_doc: Path,
    mini_config: LLMConfig,
    gpt_config: LLMConfig,
) -> tuple[dict[str, str], dict[str, RealStageSpec]]:
    prompts = {
        stage: load_system_prompt_from_design(design_doc, prompt_id)
        for stage, prompt_id in PROMPT_IDS.items()
    }
    specs = {
        stage: RealStageSpec.create(
            stage=stage,
            prompt_id=PROMPT_IDS[stage],
            prompt_text=prompts[stage],
            config=mini_config if stage in MINI_STAGES else gpt_config,
        )
        for stage in PROMPT_IDS
    }
    return prompts, specs


def _call_topology(
    chapters: Sequence[Mapping[str, Any]],
    *,
    window_target_tokens: int,
    window_max_blocks: int,
) -> tuple[dict[str, dict[str, int]], dict[str, list[Any]]]:
    by_chapter: dict[str, dict[str, int]] = {}
    windows_by_chapter: dict[str, list[Any]] = {}
    for chapter in chapters:
        chapter_id = str(chapter.get("chapter_id") or "")
        windows = build_literary_windows(
            dict(chapter),
            target_tokens=window_target_tokens,
            max_blocks=window_max_blocks,
        )
        windows_by_chapter[chapter_id] = windows
        by_chapter[chapter_id] = {
            "b1": len(windows),
            "b2": len(windows),
            "b3": 1,
        }
    return by_chapter, windows_by_chapter


def _renderable_token_rows(
    chapters: Sequence[Mapping[str, Any]],
    windows_by_chapter: Mapping[str, Sequence[Any]],
    prompts: Mapping[str, str],
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for chapter in chapters:
        chapter_id = str(chapter.get("chapter_id") or "")
        for window in windows_by_chapter[chapter_id]:
            tails = [
                *[_context_block_view(row, "previous") for row in window.previous_tail],
                *[_context_block_view(row, "next") for row in window.next_tail],
            ]
            tails.sort(key=lambda row: (row["order_index"], row["block_id"]))
            sections = {
                "active_window_blocks": [_block_view(row) for row in window.blocks],
                "context_only_tail": tails,
            }
            messages = _model_input_messages(
                prompt_text=prompts["b1"],
                stage="b1",
                chapter_id=chapter_id,
                window_id=str(window.window_id),
                allowlisted_sections=sections,
            )
            rows.append(
                {
                    "call_id": f"b1:{chapter_id}:{window.window_id}",
                    "stage": "b1",
                    "chapter_id": chapter_id,
                    "window_id": str(window.window_id),
                    "status": "exact_now",
                    "prompt_tokens": estimate_prompt_tokens(messages, RESPONSE_FORMAT_JSON),
                    "model_input_hash": canonical_hash(messages),
                    "rendered_messages": messages,
                    "replay_status": "fresh_required",
                    "replay_reason": "request_contract_v2_invalidates_with_b0_fingerprints",
                }
            )
    return rows


def _retry_envelope(
    per_chapter_calls: Sequence[tuple[str, int]], *, per_call_hard_cap: int
) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    prior_fresh_calls = 0
    max_retries = 0
    max_chapter = ""
    for chapter_id, calls in per_chapter_calls:
        allowed_prior_retries = int(prior_fresh_calls * MAX_FRESH_TECHNICAL_RETRY_RATE)
        retries_before_next_halt = allowed_prior_retries + calls
        rows.append(
            {
                "chapter_id": chapter_id,
                "prior_fresh_calls": prior_fresh_calls,
                "allowed_prior_retries_at_or_below_10pct": allowed_prior_retries,
                "current_atomic_chapter_calls": calls,
                "max_retries_before_next_enforceable_halt": retries_before_next_halt,
                "retry_token_burst": retries_before_next_halt * per_call_hard_cap,
            }
        )
        if retries_before_next_halt > max_retries:
            max_retries = retries_before_next_halt
            max_chapter = chapter_id
        prior_fresh_calls += calls
    return {
        "policy_version": RETRY_POLICY_VERSION,
        "fresh_technical_retry_halt_threshold": MAX_FRESH_TECHNICAL_RETRY_RATE,
        "atomic_boundaries": rows,
        "worst_boundary_chapter": max_chapter,
        "max_retries_before_next_enforceable_halt": max_retries,
        "technical_retry_reserve": max_retries * per_call_hard_cap,
    }


def _bucket_budget(
    *,
    quota_bucket_id: str,
    config: LLMConfig,
    call_count: int,
    per_chapter_calls: Sequence[tuple[str, int]],
    declared_used_today: int,
) -> dict[str, Any]:
    prompt_cap = int(config.prompt_token_cap or 0)
    per_call_hard_cap = prompt_cap + int(config.max_output_tokens)
    base = call_count * per_call_hard_cap
    retry = _retry_envelope(per_chapter_calls, per_call_hard_cap=per_call_hard_cap)
    maximum = base + int(retry["technical_retry_reserve"])
    headroom = int(config.daily_token_cap) - int(declared_used_today)
    return {
        "quota_bucket_id": _validate_bucket_id(quota_bucket_id),
        "model": config.model,
        "utc_day_internal_cap": int(config.daily_token_cap),
        "declared_prompt_plus_completion_used_today": int(declared_used_today),
        "declared_usage_source": "operator_snapshot",
        "headroom_before_d0": headroom,
        "call_count": call_count,
        "prompt_token_cap": prompt_cap,
        "max_output_tokens": int(config.max_output_tokens),
        "per_call_hard_cap": per_call_hard_cap,
        "base_hard_reserve": base,
        "retry_policy": retry,
        "maximum_spend_before_next_enforceable_halt": maximum,
        "fits_internal_headroom": headroom >= maximum,
    }


def _config_manifest(path: Path, config: LLMConfig, spec: RealStageSpec) -> dict[str, Any]:
    return {
        "path": _relative_path(path),
        "file_sha256": file_sha256(path),
        "model_config": dict(spec.model_config),
        "model_config_hash": spec.model_config_hash,
    }


def _validated_m1_resume_manifest(
    *,
    document: Mapping[str, Any],
    resume_root: Path,
    execution_contract_hash: str,
) -> dict[str, Any]:
    root = _require_capability_root(resume_root)
    checkpoints, states = _load_m1_chain(
        document=document,
        through_index=len(CHAPTER_IDS) - 1,
        m1v3_dir=root,
        execution_mode=EXECUTION_MODE_REAL_API,
        execution_contract_hash=execution_contract_hash,
    )
    rows = []
    for chapter_id in CHAPTER_IDS:
        checkpoint = checkpoints[chapter_id]
        state = states[chapter_id]
        rows.append(
            {
                "chapter_id": chapter_id,
                "checkpoint_hash": str(checkpoint["checkpoint_hash"]),
                "checkpoint_identity_hash": str(checkpoint["checkpoint_identity_hash"]),
                "semantic_state_hash": str(state["semantic_state_hash"]),
            }
        )
    body = {
        "schema_version": M1_RESUME_SCHEMA_VERSION,
        "status": "verified",
        "root": _relative_path(root),
        "execution_mode": EXECUTION_MODE_REAL_API,
        "execution_contract_hash": execution_contract_hash,
        "chapters": rows,
    }
    return {**body, "manifest_hash": canonical_hash(body)}


def build_d0_preflight(
    *,
    source_path: Path = DEFAULT_SOURCE,
    design_doc: Path = DEFAULT_DESIGN_DOC,
    mini_config_path: Path = DEFAULT_MINI_CONFIG,
    gpt_config_path: Path = DEFAULT_GPT_CONFIG,
    output_root: Path,
    run_id: str,
    mini_quota_bucket_id: str,
    gpt_quota_bucket_id: str,
    mini_used_today: int = 0,
    gpt_used_today: int = 0,
    created_at_utc: str | None = None,
    phase_a_target_gate_accepted: bool = False,
    m1v3_resume_root: Path | None = None,
) -> dict[str, Any]:
    source_path = Path(source_path).resolve()
    design_doc = Path(design_doc).resolve()
    mini_config_path = Path(mini_config_path).resolve()
    gpt_config_path = Path(gpt_config_path).resolve()
    output_root = _require_capability_root(output_root)
    if output_root.name != run_id:
        raise D0ContractError("D0 output directory name must equal run_id")
    if mini_used_today < 0 or gpt_used_today < 0:
        raise D0ContractError("declared usage cannot be negative")

    created = created_at_utc or _utc_now()
    document, chapter_mapping = load_wuthering_heights_epub(source_path)
    selected = select_chapters(document, list(CHAPTER_IDS))
    selected_ids = [str(row.get("chapter_id") or "") for row in selected]
    whole_ids = [str(row.get("chapter_id") or "") for row in document.get("chapters") or []]
    if selected_ids != list(CHAPTER_IDS) or selected_ids != whole_ids[: len(CHAPTER_IDS)]:
        raise D0ContractError("D0 chapters must be the exact WH ch1-4 prefix")

    mini_config = load_llm_config(mini_config_path)
    gpt_config = load_llm_config(gpt_config_path)
    if (
        mini_config.model != "gpt-5.4-mini"
        or mini_config.prompt_token_cap != 9300
        or mini_config.max_output_tokens != 6144
        or mini_config.daily_token_cap != 2_250_000
    ):
        raise D0ContractError("D0 mini config does not match the locked caps")
    if (
        gpt_config.model != "gpt-5.4"
        or gpt_config.prompt_token_cap != 14000
        or gpt_config.max_output_tokens != 12288
        or gpt_config.daily_token_cap != 225_000
    ):
        raise D0ContractError("D0 GPT-5.4 config does not match the locked caps")

    prompts, specs = _prompt_artifacts(design_doc, mini_config, gpt_config)
    m1_contract = real_execution_contract_hash(
        {stage: specs[stage] for stage in MINI_STAGES}
    )
    m2_contract = real_execution_contract_hash(
        {stage: specs[stage] for stage in GPT_STAGES}
    )
    m1_resume = (
        _validated_m1_resume_manifest(
            document=document,
            resume_root=Path(m1v3_resume_root),
            execution_contract_hash=m1_contract,
        )
        if m1v3_resume_root is not None
        else {"status": "not_requested"}
    )
    restore_m1 = m1_resume["status"] == "verified"
    call_counts, windows = _call_topology(
        selected,
        window_target_tokens=DEFAULT_WINDOW_TARGET_TOKENS,
        window_max_blocks=DEFAULT_WINDOW_MAX_BLOCKS,
    )
    stage_totals = {
        stage: sum(row[stage] for row in call_counts.values()) for stage in PROMPT_IDS
    }
    execution_by_chapter = {
        chapter_id: {
            stage: (0 if restore_m1 and stage in MINI_STAGES else count)
            for stage, count in row.items()
        }
        for chapter_id, row in call_counts.items()
    }
    execution_stage_totals = {
        stage: sum(row[stage] for row in execution_by_chapter.values())
        for stage in PROMPT_IDS
    }
    mini_calls = sum(execution_stage_totals[stage] for stage in MINI_STAGES)
    gpt_calls = sum(execution_stage_totals[stage] for stage in GPT_STAGES)
    renderable = _renderable_token_rows(selected, windows, prompts)
    if restore_m1:
        for row in renderable:
            row["replay_status"] = "checkpoint_restored"
            row["replay_reason"] = "verified_m1_checkpoint_chain"
    exact_prompt_total = sum(int(row["prompt_tokens"]) for row in renderable)
    exact_prompt_max = max((int(row["prompt_tokens"]) for row in renderable), default=0)
    prompt_cap_ok = all(
        int(row["prompt_tokens"]) <= int(mini_config.prompt_token_cap or 0)
        for row in renderable
    )

    mini_by_chapter = [
        (
            chapter_id,
            sum(execution_by_chapter[chapter_id][stage] for stage in MINI_STAGES),
        )
        for chapter_id in selected_ids
    ]
    gpt_by_chapter = [
        (chapter_id, sum(call_counts[chapter_id][stage] for stage in GPT_STAGES))
        for chapter_id in selected_ids
    ]
    mini_budget = _bucket_budget(
        quota_bucket_id=mini_quota_bucket_id,
        config=mini_config,
        call_count=mini_calls,
        per_chapter_calls=mini_by_chapter,
        declared_used_today=mini_used_today,
    )
    gpt_budget = _bucket_budget(
        quota_bucket_id=gpt_quota_bucket_id,
        config=gpt_config,
        call_count=gpt_calls,
        per_chapter_calls=gpt_by_chapter,
        declared_used_today=gpt_used_today,
    )
    topology_matches_lock = stage_totals == {"b1": 49, "b2": 49, "b3": 4}
    approval_allowed = bool(
        topology_matches_lock
        and prompt_cap_ok
        and mini_budget["fits_internal_headroom"]
        and gpt_budget["fits_internal_headroom"]
    )

    book_manifest = build_book_source_manifest(document)
    body: dict[str, Any] = {
        "schema_version": PREFLIGHT_SCHEMA_VERSION,
        "zero_api": True,
        "created_at_utc": created,
        "utc_day": _utc_day(created),
        "run_id": run_id,
        "output_root": _relative_path(output_root),
        "source": {
            "path": _relative_path(source_path),
            "file_sha256": file_sha256(source_path),
            "metadata_source_sha256": str(
                (document.get("metadata") or {}).get("source_sha256") or ""
            ).lower(),
            "document_id": str(document.get("doc_id") or ""),
            "whole_book_chapter_count": len(whole_ids),
            "book_source_manifest_hash": str(book_manifest["manifest_hash"]),
            "chapter_mapping_hash": canonical_hash(chapter_mapping),
        },
        "scope": {
            "knowledge_mode": "whole_book_frozen",
            "selected_chapters": selected_ids,
            "window_target_tokens": DEFAULT_WINDOW_TARGET_TOKENS,
            "window_max_blocks": DEFAULT_WINDOW_MAX_BLOCKS,
            "tail_k": DEFAULT_TAIL_K,
            "summary_k": DEFAULT_SUMMARY_K,
        },
        "contracts": {
            "contract_versions": contract_versions(),
            "runtime_file_hashes": {
                _relative_path(path): file_sha256(path)
                for path in RUNTIME_CONTRACT_FILES
            },
            "m1_execution_contract_hash": m1_contract,
            "m2_execution_contract_hash": m2_contract,
        },
        "prompts": {
            stage: {
                "prompt_id": PROMPT_IDS[stage],
                "prompt_sha256": specs[stage].prompt_sha256,
                "prompt_chars": len(prompts[stage]),
            }
            for stage in PROMPT_IDS
        },
        "prompt_source": {
            "path": _relative_path(design_doc),
            "file_sha256": file_sha256(design_doc),
        },
        "model_configs": {
            "mini": _config_manifest(mini_config_path, mini_config, specs["b1"]),
            "gpt54": _config_manifest(gpt_config_path, gpt_config, specs["b3"]),
        },
        "call_topology": {
            "by_chapter": call_counts,
            "stage_totals": stage_totals,
            "execution_by_chapter": execution_by_chapter,
            "execution_stage_totals": execution_stage_totals,
            "mini_call_count": mini_calls,
            "gpt54_call_count": gpt_calls,
            "matches_locked_49_49_4": topology_matches_lock,
        },
        "request_token_status": {
            "exact_now_stages": ["b1"],
            "not_yet_renderable": {
                "b2": "depends_on_validated_b1_outputs",
                "b3": "depends_on_validated_m1v3_and_prior_b3_summaries",
            },
            "exact_now_rows": renderable,
            "exact_now_call_count": len(renderable),
            "exact_now_prompt_tokens_total": exact_prompt_total,
            "exact_now_prompt_tokens_max": exact_prompt_max,
            "all_exact_now_prompts_fit_mini_cap": prompt_cap_ok,
        },
        "m1_resume_source": m1_resume,
        "cache_reuse_status": {
            "policy": "full_request_fingerprint_match_only",
            "prior_with_b0_artifacts_eligible": False,
            "prior_approval_hash_prefix": "94e535",
            "stage_counts": {
                "b1": {
                    "cache_hit": 0,
                    "checkpoint_restored": stage_totals["b1"] if restore_m1 else 0,
                    "fresh_required": execution_stage_totals["b1"],
                    "reason": (
                        "verified_m1_checkpoint_chain"
                        if restore_m1
                        else "request_contract_version_bumped"
                    ),
                },
                "b2": {
                    "cache_hit": 0,
                    "checkpoint_restored": stage_totals["b2"] if restore_m1 else 0,
                    "fresh_required": execution_stage_totals["b2"],
                    "reason": (
                        "verified_m1_checkpoint_chain"
                        if restore_m1
                        else "B0_scene_projection_removed"
                    ),
                },
                "b3": {
                    "cache_hit": 0,
                    "checkpoint_restored": 0,
                    "fresh_required": execution_stage_totals["b3"],
                    "reason": "B0_typed_projection_removed",
                },
            },
            "total_cache_hit": 0,
            "total_checkpoint_restored": (
                stage_totals["b1"] + stage_totals["b2"] if restore_m1 else 0
            ),
            "total_fresh_required": mini_calls + gpt_calls,
        },
        "budget": {
            "accounting_formula": "prompt_tokens+completion_tokens",
            "cached_prompt_tokens_count_toward_internal_gate": True,
            "expected_usage_is_not_approval_basis": True,
            "buckets": [mini_budget, gpt_budget],
            "all_buckets_fit": bool(
                mini_budget["fits_internal_headroom"]
                and gpt_budget["fits_internal_headroom"]
            ),
        },
        "phase_a_dependency": {
            "identity_target_manifest_status": (
                "enabled_after_phase_a_gate"
                if phase_a_target_gate_accepted
                else "pending_phase_a_gate"
            ),
            "phase_a_target_gate_accepted": bool(phase_a_target_gate_accepted),
            "duplicate_target_universe_logic_forbidden": True,
        },
        "approval_allowed": approval_allowed,
    }
    return {**body, "approval_manifest_hash": canonical_hash(body)}


def write_d0_preflight(output_root: Path, manifest: Mapping[str, Any]) -> Path:
    output_root = _require_capability_root(output_root)
    return write_json_exclusive(output_root / "d0_preflight.json", dict(manifest))


def _load_preflight(path: Path) -> dict[str, Any]:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or payload.get("schema_version") != PREFLIGHT_SCHEMA_VERSION:
        raise D0ContractError("invalid D0 preflight schema")
    expected_hash = str(payload.get("approval_manifest_hash") or "")
    body = dict(payload)
    body.pop("approval_manifest_hash", None)
    if not expected_hash or canonical_hash(body) != expected_hash:
        raise D0ContractError("D0 preflight manifest hash mismatch")
    return payload


def validate_real_run_approval(
    *, preflight_path: Path, supplied_approval_hash: str
) -> dict[str, Any]:
    stored = _load_preflight(preflight_path)
    if supplied_approval_hash != stored["approval_manifest_hash"]:
        raise D0ContractError("missing or stale user-approved D0 manifest hash")
    if not stored.get("approval_allowed"):
        raise D0ContractError("D0 preflight did not authorize a real run")
    source = REPO_ROOT / str(stored["source"]["path"])
    design = REPO_ROOT / str(stored["prompt_source"]["path"])
    mini_config = REPO_ROOT / str(stored["model_configs"]["mini"]["path"])
    gpt_config = REPO_ROOT / str(stored["model_configs"]["gpt54"]["path"])
    output_root = REPO_ROOT / str(stored["output_root"])
    buckets = {row["model"]: row for row in stored["budget"]["buckets"]}
    recomputed = build_d0_preflight(
        source_path=source,
        design_doc=design,
        mini_config_path=mini_config,
        gpt_config_path=gpt_config,
        output_root=output_root,
        run_id=str(stored["run_id"]),
        mini_quota_bucket_id=str(buckets["gpt-5.4-mini"]["quota_bucket_id"]),
        gpt_quota_bucket_id=str(buckets["gpt-5.4"]["quota_bucket_id"]),
        mini_used_today=int(
            buckets["gpt-5.4-mini"]["declared_prompt_plus_completion_used_today"]
        ),
        gpt_used_today=int(
            buckets["gpt-5.4"]["declared_prompt_plus_completion_used_today"]
        ),
        created_at_utc=str(stored["created_at_utc"]),
        phase_a_target_gate_accepted=bool(
            stored["phase_a_dependency"]["phase_a_target_gate_accepted"]
        ),
        m1v3_resume_root=(
            REPO_ROOT / str(stored["m1_resume_source"]["root"])
            if stored.get("m1_resume_source", {}).get("status") == "verified"
            else None
        ),
    )
    if canonical_json(recomputed) != canonical_json(stored):
        raise D0ContractError("D0 preflight is stale against current source/config/prompt bytes")
    return stored


def _openai_transport(api_key: str) -> Callable[..., Any]:
    from openai import OpenAI

    client = OpenAI(api_key=api_key)
    return client.chat.completions.create


def _new_client(config: LLMConfig, cache_path: Path, api_key: str) -> LLMClient:
    return LLMClient(
        config,
        cache_path,
        transport=_openai_transport(api_key),
        max_retries=0,
    )


def _audit_rows(output_root: Path, bucket_by_model: Mapping[str, str]) -> list[dict[str, Any]]:
    root = output_root / "builder_v3"
    rows: list[dict[str, Any]] = []
    for raw_path in sorted(root.glob("audit/*/*/attempt_01/raw_result.json")):
        call_dir = raw_path.parent.parent
        request_path = call_dir / "request.json"
        if not request_path.is_file():
            raise D0ContractError(f"audit raw result lacks request: {raw_path}")
        raw = json.loads(raw_path.read_text(encoding="utf-8"))
        request = json.loads(request_path.read_text(encoding="utf-8"))
        model = str((request.get("transport_config") or {}).get("model") or "")
        attempts = list((raw.get("transport_meta") or {}).get("attempts") or [])
        usage = dict(raw.get("usage") or {})
        rows.append(
            {
                "audit_call_id": call_dir.resolve().relative_to(root.resolve()).as_posix(),
                "utc_day": datetime.now(UTC).date().isoformat(),
                "stage": str(request.get("stage") or ""),
                "chapter_id": str(request.get("chapter_id") or ""),
                "window_id": request.get("window_id"),
                "model": model,
                "quota_bucket_id": str(bucket_by_model.get(model) or ""),
                "request_fingerprint": canonical_hash(request),
                "cache_status": "hit" if raw.get("from_cache") else "fresh",
                "technical_attempts": len(attempts),
                "technical_retries": max(0, len(attempts) - 1),
                "usage": {
                    "prompt_tokens": int(usage.get("prompt_tokens") or 0),
                    "cached_tokens": int(usage.get("cached_tokens") or 0),
                    "completion_tokens": int(usage.get("completion_tokens") or 0),
                    "reasoning_tokens": int(usage.get("reasoning_tokens") or 0),
                    "cost_usd": float(usage.get("cost_usd") or 0.0),
                },
            }
        )
    return rows


def _observed_stats(rows: Sequence[Mapping[str, Any]]) -> dict[str, dict[str, Any]]:
    stats: dict[str, dict[str, Any]] = defaultdict(
        lambda: {
            "fresh_calls": 0,
            "cache_hits": 0,
            "technical_retries": 0,
            "prompt_plus_completion_tokens": 0,
            "cost_usd": 0.0,
        }
    )
    for row in rows:
        model = str(row.get("model") or "")
        if row.get("cache_status") == "hit":
            stats[model]["cache_hits"] += 1
            continue
        stats[model]["fresh_calls"] += 1
        stats[model]["technical_retries"] += int(row.get("technical_retries") or 0)
        usage = row.get("usage") or {}
        stats[model]["prompt_plus_completion_tokens"] += int(
            usage.get("prompt_tokens") or 0
        ) + int(usage.get("completion_tokens") or 0)
        stats[model]["cost_usd"] += float(usage.get("cost_usd") or 0.0)
    for model, row in stats.items():
        fresh = int(row["fresh_calls"])
        row["fresh_technical_retry_rate"] = (
            float(row["technical_retries"]) / fresh if fresh else 0.0
        )
    return {key: dict(value) for key, value in sorted(stats.items())}


def _enforce_observed_boundary(
    rows: Sequence[Mapping[str, Any]], preflight: Mapping[str, Any]
) -> dict[str, dict[str, Any]]:
    stats = _observed_stats(rows)
    budgets = {row["model"]: row for row in preflight["budget"]["buckets"]}
    planned_stage_totals = {
        str(key): int(value)
        for key, value in preflight["call_topology"]["execution_stage_totals"].items()
    }
    observed_fresh_by_stage = Counter(
        str(row.get("stage") or "")
        for row in rows
        if row.get("cache_status") == "fresh"
    )
    for stage, count in observed_fresh_by_stage.items():
        if stage not in planned_stage_totals or count > planned_stage_totals[stage]:
            raise D0ContractError(f"fresh call count exceeded approved topology: {stage}")
    for model, observed in stats.items():
        budget = budgets.get(model)
        if budget is None:
            raise D0ContractError(f"observed unplanned model: {model}")
        if float(observed["fresh_technical_retry_rate"]) > MAX_FRESH_TECHNICAL_RETRY_RATE:
            raise D0ContractError(
                f"fresh technical retry rate exceeded 10 percent for {model}"
            )
        used = int(observed["prompt_plus_completion_tokens"])
        if used > int(budget["maximum_spend_before_next_enforceable_halt"]):
            raise D0ContractError(f"observed usage exceeded approved hard envelope: {model}")
        if (
            used + int(budget["declared_prompt_plus_completion_used_today"])
            > int(budget["utc_day_internal_cap"])
        ):
            raise D0ContractError(f"observed usage exceeded internal UTC-day cap: {model}")
    for row in rows:
        if int(row.get("technical_retries") or 0) > 1:
            raise D0ContractError(
                f"call exceeded one technical retry: {row.get('audit_call_id')}"
            )
        if row.get("cache_status") == "hit":
            continue
        model = str(row.get("model") or "")
        budget = budgets[model]
        usage = row.get("usage") or {}
        if int(usage.get("prompt_tokens") or 0) > int(budget["prompt_token_cap"]):
            raise D0ContractError(f"observed prompt exceeded cap: {row.get('audit_call_id')}")
        if int(usage.get("completion_tokens") or 0) > int(budget["max_output_tokens"]):
            raise D0ContractError(f"observed completion exceeded cap: {row.get('audit_call_id')}")
    return stats


def _sealed_generation_files(
    *,
    book_manifest: Mapping[str, Any],
    bundle: Mapping[str, Any],
    target_manifest: Mapping[str, Any] | None,
) -> dict[str, Any]:
    rows: dict[str, Any] = {
        "book_source_manifest.json": dict(book_manifest),
        "b4_input_bundle.json": dict(bundle),
    }
    if target_manifest is not None:
        rows["identity_target_manifest.json"] = dict(target_manifest)
    return rows


def _persist_sealed_generation(
    *,
    output_root: Path,
    book_manifest: Mapping[str, Any],
    bundle: Mapping[str, Any],
    target_manifest: Mapping[str, Any] | None,
) -> Path:
    """Publish all sealed artifacts with one directory rename, never overwrite."""

    bundle_hash = str(bundle.get("bundle_manifest_hash") or "")
    if not re.fullmatch(r"[0-9a-f]{64}", bundle_hash):
        raise D0ContractError("sealed bundle lacks a canonical manifest hash")
    parent = output_root / "sealed_generations"
    final_dir = parent / bundle_hash
    files = _sealed_generation_files(
        book_manifest=book_manifest,
        bundle=bundle,
        target_manifest=target_manifest,
    )
    seal_body = {
        "schema_version": "literary_m4f_d0_sealed_generation_v1",
        "bundle_manifest_hash": bundle_hash,
        "files": {
            name: sha256(
                (json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode(
                    "utf-8"
                )
            ).hexdigest()
            for name, value in sorted(files.items())
        },
    }
    seal = {**seal_body, "seal_manifest_hash": canonical_hash(seal_body)}
    expected = {**files, "seal_manifest.json": seal}
    if final_dir.exists():
        for name, value in expected.items():
            path = final_dir / name
            if not path.is_file() or json.loads(path.read_text(encoding="utf-8")) != value:
                raise D0ContractError("existing sealed generation conflicts with current bundle")
        return final_dir

    parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix=".d0-seal-", dir=parent))
    try:
        for name, value in expected.items():
            encoded = json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
            path = temporary / name
            with path.open("xb") as handle:
                handle.write(encoded.encode("utf-8"))
                handle.flush()
                os.fsync(handle.fileno())
        os.replace(temporary, final_dir)
    finally:
        if temporary.exists():
            shutil.rmtree(temporary)
    return final_dir


def _persist_boundary_ledger(
    *,
    output_root: Path,
    sequence: int,
    stage_group: str,
    chapter_id: str,
    new_rows: Sequence[Mapping[str, Any]],
    all_rows: Sequence[Mapping[str, Any]],
    preflight: Mapping[str, Any],
) -> dict[str, Any]:
    observed = _enforce_observed_boundary(all_rows, preflight)
    payload = {
        "schema_version": "literary_m4f_d0_boundary_ledger_v1",
        "sequence": sequence,
        "stage_group": stage_group,
        "chapter_id": chapter_id,
        "calls": [dict(row) for row in new_rows],
        "cumulative_observed": observed,
        "approval_manifest_hash": preflight["approval_manifest_hash"],
    }
    payload["ledger_hash"] = canonical_hash(payload)
    write_json_exclusive(
        output_root / "ledger" / f"{sequence:02d}_{stage_group}_{chapter_id}.json",
        payload,
    )
    return payload


def run_d0_real(
    *,
    preflight_path: Path,
    supplied_approval_hash: str,
    mini_key_env: str = "OPENAI_API_KEY_MINI",
    gpt_key_env: str = "OPENAI_API_KEY_GPT54",
    client_factory: Callable[[LLMConfig, Path, str], Any] = _new_client,
) -> dict[str, Any]:
    preflight = validate_real_run_approval(
        preflight_path=preflight_path,
        supplied_approval_hash=supplied_approval_hash,
    )
    output_root = _require_capability_root(REPO_ROOT / str(preflight["output_root"]))
    existing_report_path = output_root / "d0_run_report.json"
    if existing_report_path.is_file():
        existing = json.loads(existing_report_path.read_text(encoding="utf-8"))
        body = dict(existing)
        expected_hash = str(body.pop("run_report_hash", ""))
        if (
            existing.get("approval_manifest_hash") != supplied_approval_hash
            or not expected_hash
            or canonical_hash(body) != expected_hash
        ):
            raise D0ContractError("existing D0 run report conflicts with approved manifest")
        return existing
    source_path = REPO_ROOT / str(preflight["source"]["path"])
    design_doc = REPO_ROOT / str(preflight["prompt_source"]["path"])
    mini_config_path = REPO_ROOT / str(preflight["model_configs"]["mini"]["path"])
    gpt_config_path = REPO_ROOT / str(preflight["model_configs"]["gpt54"]["path"])

    # Approval validation above deliberately precedes key access and client construction.
    restore_m1 = preflight.get("m1_resume_source", {}).get("status") == "verified"
    mini_key = os.environ.get(mini_key_env, "")
    gpt_key = os.environ.get(gpt_key_env, "")
    if (not restore_m1 and not mini_key) or not gpt_key:
        raise D0ContractError(
            "real D0 requires the GPT key and requires the mini key unless M1 is restored"
        )
    try:
        document, _ = load_wuthering_heights_epub(source_path)
        mini_config = load_llm_config(mini_config_path)
        gpt_config = load_llm_config(gpt_config_path)
        _, specs = _prompt_artifacts(design_doc, mini_config, gpt_config)
        m1_specs = {stage: specs[stage] for stage in MINI_STAGES}
        m2_specs = {stage: specs[stage] for stage in GPT_STAGES}
        m1_contract = real_execution_contract_hash(m1_specs)
        m2_contract = real_execution_contract_hash(m2_specs)
        if m1_contract != preflight["contracts"]["m1_execution_contract_hash"]:
            raise D0ContractError("M1 execution contract changed after approval")
        if m2_contract != preflight["contracts"]["m2_execution_contract_hash"]:
            raise D0ContractError("M2 execution contract changed after approval")

        cache_root = output_root / "cache"
        gpt_client = client_factory(
            gpt_config,
            cache_root / f"gpt54_{specs['b3'].model_config_hash[:16]}.sqlite3",
            gpt_key,
        )
        clients = {"b3": gpt_client}
        if not restore_m1:
            mini_client = client_factory(
                mini_config,
                cache_root / f"mini_{specs['b1'].model_config_hash[:16]}.sqlite3",
                mini_key,
            )
            clients.update({"b1": mini_client, "b2": mini_client})
        executor = RealStageExecutor(clients, slice_cache_root=output_root)
        budget_rows = {row["model"]: row for row in preflight["budget"]["buckets"]}
        bucket_by_model = {
            model: str(row["quota_bucket_id"]) for model, row in budget_rows.items()
        }
        existing_rows = _audit_rows(output_root, bucket_by_model)
        seen: set[str] = {str(row["audit_call_id"]) for row in existing_rows}
        existing_ledger_paths = sorted((output_root / "ledger").glob("*.json"))
        ledgers: list[dict[str, Any]] = [
            json.loads(path.read_text(encoding="utf-8")) for path in existing_ledger_paths
        ]
        boundary_seq = max(
            (int(row.get("sequence") or 0) for row in ledgers),
            default=0,
        )

        m1v3_dir = output_root
        if restore_m1:
            m1v3_dir = _require_capability_root(
                REPO_ROOT / str(preflight["m1_resume_source"]["root"])
            )
        else:
            for prefix_length, chapter_id in enumerate(CHAPTER_IDS, start=1):
                report = run_m1_v3(
                    document,
                    CHAPTER_IDS[:prefix_length],
                    executor=executor,
                    out_dir=output_root,
                    execution_mode=EXECUTION_MODE_REAL_API,
                    window_target_tokens=DEFAULT_WINDOW_TARGET_TOKENS,
                    window_max_blocks=DEFAULT_WINDOW_MAX_BLOCKS,
                    tail_k=DEFAULT_TAIL_K,
                    resume=True,
                    real_stage_specs=m1_specs,
                )
                all_rows = _audit_rows(output_root, bucket_by_model)
                new_rows = [row for row in all_rows if row["audit_call_id"] not in seen]
                seen.update(str(row["audit_call_id"]) for row in new_rows)
                if new_rows:
                    boundary_seq += 1
                    ledgers.append(
                        _persist_boundary_ledger(
                            output_root=output_root,
                            sequence=boundary_seq,
                            stage_group="m1v3",
                            chapter_id=chapter_id,
                            new_rows=new_rows,
                            all_rows=all_rows,
                            preflight=preflight,
                        )
                    )
                else:
                    _enforce_observed_boundary(all_rows, preflight)
                if report.get("status") != "complete":
                    raise D0ContractError(
                        f"M1V3 halted at {chapter_id}: {report.get('stopping_error')}"
                    )

        for prefix_length, chapter_id in enumerate(CHAPTER_IDS, start=1):
            report = run_m2_v3(
                document,
                CHAPTER_IDS[:prefix_length],
                executor=executor,
                out_dir=output_root,
                m1v3_dir=m1v3_dir,
                execution_mode=EXECUTION_MODE_REAL_API,
                summary_k=DEFAULT_SUMMARY_K,
                resume=True,
                real_stage_specs=m2_specs,
                m1_execution_contract_hash=m1_contract,
            )
            all_rows = _audit_rows(output_root, bucket_by_model)
            new_rows = [row for row in all_rows if row["audit_call_id"] not in seen]
            seen.update(str(row["audit_call_id"]) for row in new_rows)
            if new_rows:
                boundary_seq += 1
                ledgers.append(
                    _persist_boundary_ledger(
                        output_root=output_root,
                        sequence=boundary_seq,
                        stage_group="m2v3",
                        chapter_id=chapter_id,
                        new_rows=new_rows,
                        all_rows=all_rows,
                        preflight=preflight,
                    )
                )
            else:
                _enforce_observed_boundary(all_rows, preflight)
            if report.get("status") != "complete":
                raise D0ContractError(f"M2V3 halted at {chapter_id}: {report.get('stopping_error')}")

        book_manifest = build_book_source_manifest(document)
        bundle = assemble_b4_input_bundle(
            document,
            CHAPTER_IDS,
            book_source_manifest=book_manifest,
            m1v3_dir=m1v3_dir,
            m2v3_dir=output_root,
            execution_mode=EXECUTION_MODE_REAL_API,
            window_target_tokens=DEFAULT_WINDOW_TARGET_TOKENS,
            window_max_blocks=DEFAULT_WINDOW_MAX_BLOCKS,
            tail_k=DEFAULT_TAIL_K,
            summary_k=DEFAULT_SUMMARY_K,
            m1_execution_contract_hash=m1_contract,
            m2_execution_contract_hash=m2_contract,
        )
        verify_b4_input_bundle_identity(bundle)

        target_status = "pending_phase_a_gate"
        target_counts: dict[str, Any] | None = None
        target_manifest: dict[str, Any] | None = None
        if preflight["phase_a_dependency"]["phase_a_target_gate_accepted"]:
            target_manifest = build_identity_target_manifest(bundle)
            target_status = "complete"
            target_counts = dict(target_manifest["counts"])
        sealed_generation = _persist_sealed_generation(
            output_root=output_root,
            book_manifest=book_manifest,
            bundle=bundle,
            target_manifest=target_manifest,
        )

        final_rows = _audit_rows(output_root, bucket_by_model)
        report_body: dict[str, Any] = {
            "schema_version": RUN_REPORT_SCHEMA_VERSION,
            "status": "complete",
            "approval_manifest_hash": preflight["approval_manifest_hash"],
            "execution_contract_hashes": {"m1v3": m1_contract, "m2v3": m2_contract},
            "m1_resume_source": preflight["m1_resume_source"],
            "state_lineage_id": bundle["state_lineage_id"],
            "input_identity_manifest_hash": bundle["input_identity_manifest_hash"],
            "bundle_manifest_hash": bundle["bundle_manifest_hash"],
            "sealed_generation": sealed_generation.resolve()
            .relative_to(output_root.resolve())
            .as_posix(),
            "identity_target_manifest_status": target_status,
            "identity_target_counts": target_counts,
            "observed": _enforce_observed_boundary(final_rows, preflight),
            "boundary_ledger_hashes": [row["ledger_hash"] for row in ledgers],
        }
        report_payload = {**report_body, "run_report_hash": canonical_hash(report_body)}
        write_json_exclusive(output_root / "d0_run_report.json", report_payload)
        return report_payload
    finally:
        os.environ.pop(mini_key_env, None)
        os.environ.pop(gpt_key_env, None)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Builder-v3 D0 preflight/real runner")
    parser.add_argument("--real-run", action="store_true")
    parser.add_argument("--preflight-path", type=Path)
    parser.add_argument("--approval-manifest-hash")
    parser.add_argument("--source", type=Path, default=DEFAULT_SOURCE)
    parser.add_argument("--design-doc", type=Path, default=DEFAULT_DESIGN_DOC)
    parser.add_argument("--mini-config", type=Path, default=DEFAULT_MINI_CONFIG)
    parser.add_argument("--gpt-config", type=Path, default=DEFAULT_GPT_CONFIG)
    parser.add_argument("--run-id", default=None)
    parser.add_argument("--output-root", type=Path, default=None)
    parser.add_argument("--mini-quota-bucket-id", default="openai-key-1-mini")
    parser.add_argument("--gpt-quota-bucket-id", default="openai-key-1-gpt54")
    parser.add_argument("--mini-used-today", type=int, default=0)
    parser.add_argument("--gpt-used-today", type=int, default=0)
    parser.add_argument("--phase-a-target-gate-accepted", action="store_true")
    parser.add_argument("--m1v3-resume-root", type=Path)
    parser.add_argument("--mini-key-env", default="OPENAI_API_KEY_MINI")
    parser.add_argument("--gpt-key-env", default="OPENAI_API_KEY_GPT54")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.real_run:
        if args.preflight_path is None or not args.approval_manifest_hash:
            raise D0ContractError(
                "real D0 requires --preflight-path and --approval-manifest-hash"
            )
        result = run_d0_real(
            preflight_path=args.preflight_path,
            supplied_approval_hash=args.approval_manifest_hash,
            mini_key_env=args.mini_key_env,
            gpt_key_env=args.gpt_key_env,
        )
        print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
        return 0

    run_id = args.run_id or _new_run_id()
    output_root = args.output_root or (DEFAULT_OUTPUT_PARENT / run_id)
    manifest = build_d0_preflight(
        source_path=args.source,
        design_doc=args.design_doc,
        mini_config_path=args.mini_config,
        gpt_config_path=args.gpt_config,
        output_root=output_root,
        run_id=run_id,
        mini_quota_bucket_id=args.mini_quota_bucket_id,
        gpt_quota_bucket_id=args.gpt_quota_bucket_id,
        mini_used_today=args.mini_used_today,
        gpt_used_today=args.gpt_used_today,
        phase_a_target_gate_accepted=args.phase_a_target_gate_accepted,
        m1v3_resume_root=args.m1v3_resume_root,
    )
    path = write_d0_preflight(output_root, manifest)
    summary = {
        "preflight_path": _relative_path(path),
        "approval_manifest_hash": manifest["approval_manifest_hash"],
        "approval_allowed": manifest["approval_allowed"],
        "call_topology": manifest["call_topology"],
        "budget": manifest["budget"],
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
