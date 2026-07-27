from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
import json
import os
from pathlib import Path
import re
import sqlite3
import subprocess
from typing import Any, Mapping, Sequence

from pipeline.literary.checkpoint import (
    canonical_hash,
    canonical_json,
    file_sha256,
    write_checkpoint_atomic,
)
from pipeline.literary.chapter_registry_prompts_v4 import PROMPT_IDS
from pipeline.literary.chapter_registry_schema_v4 import (
    ALIAS_SCOPE_POLICY_VERSION,
    AUDIT_SCHEMA_VERSION,
    B2_RESCAN_POLICY_VERSION,
    CANDIDATE_POLICY_VERSION,
    DELTA_SCHEMA_VERSION,
    ORIENTATION_SCHEMA_VERSION,
    REGISTRY_SCHEMA_VERSION,
    RegistryBudgetError,
    RunConfigV4,
    VALIDATOR_VERSION,
)
from pipeline.literary.chapter_registry_v4 import (
    ChapterRegistryStoreV4,
    ChapterWorkingRegistryV4,
    apply_audit_responses,
    build_b2_candidate_manifest,
    build_exception_components,
    build_registry_generation,
    build_registry_windows,
    chapter_source_manifest_hash,
    empty_registry_snapshot_v4,
    estimate_registry_prompt_tokens,
    render_auditor_requests,
    render_b0_request,
    render_b1_request,
    validate_orientation_response,
)
from pipeline.scripts.run_chapter_registry_v3_real import (
    PersistedRealExecutorV3,
    RealOpenAIRegistryExecutorV3,
)


RUNTIME_ROOT = Path(__file__).resolve().parents[2]
REPO_ROOT = RUNTIME_ROOT.parent
DEFAULT_DOCUMENT = (
    RUNTIME_ROOT
    / "data"
    / "reports"
    / "literary_l2a0_wh_builder_scaffold"
    / "document.json"
)
DEFAULT_DESIGN_DOC = REPO_ROOT / "design" / "LITERARY_PROMPT_DESIGN.md"
DEFAULT_FROZEN_DB = RUNTIME_ROOT / "data" / "jobs" / "d2l_p1" / "memory.sqlite3"
FROZEN_DB_SHA256 = "64D98965F8859869931152B2AA814FB03AFBF15E6A9853532FD0EF28B555C715"
DEFAULT_CHAPTER_ID = "wh_ch01"
TARGET_MODELS = frozenset({"gpt-5.4", "gpt-5.4-mini"})
TARGET_BUCKETS = frozenset({"openai-row1", "openai-row2"})
RESPONSE_FORMAT_JSON = {"type": "json_object"}
RUN_SCHEMA_VERSION = "literary_m4f_b0b1_v4_live_canary_v1"
RUNTIME_SOURCE_FILES = (
    RUNTIME_ROOT / "pipeline" / "literary" / "chapter_registry_prompts_v4.py",
    RUNTIME_ROOT / "pipeline" / "literary" / "chapter_registry_schema_v4.py",
    RUNTIME_ROOT / "pipeline" / "literary" / "chapter_registry_v4.py",
    Path(__file__).resolve(),
)


class RegistryV4RunError(RuntimeError):
    """Raised when a bounded v4 dry/live run must halt fail-closed."""


def _now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()


def _redacted_error(exc: BaseException) -> str:
    text = str(exc)
    text = re.sub(r"sk-[A-Za-z0-9_-]{12,}", "[REDACTED_OPENAI_KEY]", text)
    return text[:2000]


def _write_json(path: Path, value: Mapping[str, Any] | Sequence[Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    write_checkpoint_atomic(path, value)


def _ensure_empty_output(path: Path) -> Path:
    resolved = Path(path).resolve()
    if resolved.exists() and any(resolved.iterdir()):
        raise RegistryV4RunError(f"output directory is not empty: {resolved}")
    resolved.mkdir(parents=True, exist_ok=True)
    return resolved


def _verify_frozen_db(path: Path) -> str:
    actual = file_sha256(path).upper()
    if actual != FROZEN_DB_SHA256:
        raise RegistryV4RunError(f"frozen D2L database hash drift: {actual}")
    return actual


def _git_head() -> str:
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=REPO_ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _load_document(path: Path, chapter_id: str) -> tuple[dict[str, Any], dict[str, Any]]:
    value = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise RegistryV4RunError("document must be an object")
    chapters = [dict(row) for row in value.get("chapters") or [] if isinstance(row, dict)]
    matches = [row for row in chapters if str(row.get("chapter_id") or "") == chapter_id]
    if len(matches) != 1:
        raise RegistryV4RunError(f"document must contain exactly one {chapter_id}")
    return value, matches[0]


def _state_lineage_id(document: Mapping[str, Any]) -> str:
    chapters = []
    for chapter in document.get("chapters") or []:
        blocks = [
            {
                "block_id": str(block.get("block_id") or ""),
                "order_index": int(block.get("order_index") or 0),
                "block_type": str(block.get("block_type") or ""),
                "text_hash": canonical_hash(
                    str(block.get("clean_text") or block.get("source_text") or "")
                ),
            }
            for block in chapter.get("blocks") or []
        ]
        chapters.append(
            {"chapter_id": str(chapter.get("chapter_id") or ""), "blocks": blocks}
        )
    return "state4_" + canonical_hash({"chapters": chapters})[:32]


def _quota_policy() -> dict[str, Any]:
    gates: dict[str, dict[str, Any]] = {}
    for bucket in sorted(TARGET_BUCKETS):
        gates[f"{bucket}-gpt54"] = {
            "quota_bucket_id": bucket,
            "model_id": "gpt-5.4",
            "internal_utc_day_token_cap": 225000,
        }
        gates[f"{bucket}-mini"] = {
            "quota_bucket_id": bucket,
            "model_id": "gpt-5.4-mini",
            "internal_utc_day_token_cap": 2250000,
        }
    return {
        "schema_version": "openai_registry_v4_quota_policy_v1",
        "accounting_scope": "physical_bucket_plus_model_plus_utc_day",
        "gates": gates,
        "role_gate_ids": {
            "b0": ["openai-row2-gpt54", "openai-row1-gpt54"],
            "b1": ["openai-row2-mini", "openai-row1-mini"],
            "auditor": ["openai-row2-gpt54", "openai-row1-gpt54"],
        },
        "minimum_interval_seconds": 2.0,
        "local_rpd_cap_per_bucket_model": 100,
        "transport_retries": 0,
    }


def draft_semantic_config_v4() -> RunConfigV4:
    policy = _quota_policy()
    return RunConfigV4(
        b0_model_id="gpt-5.4",
        b0_reasoning_effort="none",
        b0_temperature=1.0,
        b0_seed=20260715,
        b0_output_token_cap=2048,
        b1_model_id="gpt-5.4-mini",
        b1_reasoning_effort="none",
        b1_temperature=1.0,
        b1_seed=20260715,
        b1_output_token_cap=4096,
        auditor_model_id="gpt-5.4",
        auditor_reasoning_effort="none",
        auditor_temperature=1.0,
        auditor_seed=20260715,
        auditor_output_token_cap=8192,
        b0_attention_context_mode="advisory_active_window",
        b0_input_token_cap=18000,
        b1_input_token_cap=14000,
        active_window_source_token_target=500,
        active_window_max_blocks=8,
        preceding_tail_block_cap=2,
        attention_packet_cap_per_window=16,
        known_surface_packet_cap_per_window=32,
        candidate_cards_total_cap_per_window=16,
        candidate_context_token_cap=3500,
        recency_neighbor_distance_blocks=8,
        candidate_overflow_policy="ticket",
        auditor_tickets_per_component_cap=16,
        auditor_calls_per_chapter_cap=8,
        auditor_neighbor_blocks_each_side=0,
        auditor_input_token_cap=12000,
        provider_quota_policy_hash=canonical_hash(policy),
        prompt_versions=dict(PROMPT_IDS),
        schema_versions={
            "registry": REGISTRY_SCHEMA_VERSION,
            "b0": ORIENTATION_SCHEMA_VERSION,
            "b1": DELTA_SCHEMA_VERSION,
            "auditor": AUDIT_SCHEMA_VERSION,
        },
        validator_version=VALIDATOR_VERSION,
        policy_versions={
            "candidate_selection": CANDIDATE_POLICY_VERSION,
            "alias_scope": ALIAS_SCOPE_POLICY_VERSION,
            "b2_rescan": B2_RESCAN_POLICY_VERSION,
        },
    )


@dataclass(frozen=True)
class TransportConfigV4:
    semantic_config_hash: str
    b0_model_id: str
    b0_reasoning_effort: str
    b0_temperature: float
    b0_seed: int
    b0_output_cap: int
    b0_input_cap: int
    b1_model_id: str
    b1_reasoning_effort: str
    b1_temperature: float
    b1_seed: int
    b1_output_cap: int
    b1_input_cap: int
    auditor_model_id: str
    auditor_reasoning_effort: str
    auditor_temperature: float
    auditor_seed: int
    auditor_output_cap: int
    auditor_input_token_cap: int
    quota_gates: Mapping[str, Mapping[str, Any]]
    role_quota_gate_ids: Mapping[str, tuple[str, ...]]
    pricing_usd_per_million: Mapping[str, Mapping[str, float | None]]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @property
    def config_hash(self) -> str:
        return canonical_hash(self.to_dict())


def draft_transport_config_v4(semantic: RunConfigV4) -> TransportConfigV4:
    policy = _quota_policy()
    return TransportConfigV4(
        semantic_config_hash=semantic.config_hash,
        b0_model_id=semantic.b0_model_id,
        b0_reasoning_effort=semantic.b0_reasoning_effort,
        b0_temperature=semantic.b0_temperature,
        b0_seed=semantic.b0_seed,
        b0_output_cap=semantic.b0_output_token_cap,
        b0_input_cap=semantic.b0_input_token_cap,
        b1_model_id=semantic.b1_model_id,
        b1_reasoning_effort=semantic.b1_reasoning_effort,
        b1_temperature=semantic.b1_temperature,
        b1_seed=semantic.b1_seed,
        b1_output_cap=semantic.b1_output_token_cap,
        b1_input_cap=semantic.b1_input_token_cap,
        auditor_model_id=semantic.auditor_model_id,
        auditor_reasoning_effort=semantic.auditor_reasoning_effort,
        auditor_temperature=semantic.auditor_temperature,
        auditor_seed=semantic.auditor_seed,
        auditor_output_cap=semantic.auditor_output_token_cap,
        auditor_input_token_cap=semantic.auditor_input_token_cap,
        quota_gates=dict(policy["gates"]),
        role_quota_gate_ids={
            role: tuple(ids) for role, ids in policy["role_gate_ids"].items()
        },
        pricing_usd_per_million={
            role: {"input": None, "cached_input": None, "output": None}
            for role in ("b0", "b1", "auditor")
        },
    )


def build_run_envelope(
    *, document_path: Path, design_doc: Path, chapter_id: str
) -> tuple[dict[str, Any], RunConfigV4, TransportConfigV4]:
    semantic = draft_semantic_config_v4()
    transport = draft_transport_config_v4(semantic)
    body = {
        "schema_version": RUN_SCHEMA_VERSION,
        "chapter_id": chapter_id,
        "document_sha256": file_sha256(document_path),
        "design_doc_sha256": file_sha256(design_doc),
        "git_head": _git_head(),
        "semantic_config": semantic.to_dict(),
        "semantic_config_hash": semantic.config_hash,
        "transport_config": transport.to_dict(),
        "transport_config_hash": transport.config_hash,
        "quota_policy": _quota_policy(),
        "runtime_artifact_hashes": {
            path.relative_to(REPO_ROOT).as_posix(): file_sha256(path)
            for path in RUNTIME_SOURCE_FILES
        },
        "response_format": RESPONSE_FORMAT_JSON,
        "phase_boundary": "one_chapter_live_canary_no_retry",
    }
    return {**body, "envelope_hash": canonical_hash(body)}, semantic, transport


def _bucket_from_path(path: Path) -> str | None:
    lowered = path.as_posix().casefold()
    if any(marker in lowered for marker in ("openai-row2", "openai_key2", "key2")):
        return "openai-row2"
    if any(marker in lowered for marker in ("openai-row1", "openai_key1", "key1")):
        return "openai-row1"
    return None


def _artifact_run_root(path: Path, marker: str) -> Path:
    for parent in path.parents:
        if parent.name.casefold() == marker.casefold():
            return parent.parent
    return path.parent


def scan_current_utc_usage(
    *, roots: Sequence[Path], exclude_root: Path | None = None
) -> dict[str, Any]:
    """Deduplicate current-day OpenAI usage across raw audit files and SQLite caches."""

    utc_date = datetime.now(UTC).date().isoformat()
    excluded = Path(exclude_root).resolve() if exclude_root is not None else None
    records: dict[tuple[str, str, str, str], dict[str, Any]] = {}
    raw_bucket_by_run_cache_key: dict[tuple[str, str], str] = {}
    raw_coverage: set[tuple[str, str]] = set()
    unknown: list[dict[str, str]] = []
    resolved_roots = sorted({Path(root).resolve() for root in roots if Path(root).exists()})

    def excluded_path(path: Path) -> bool:
        return excluded is not None and (path == excluded or excluded in path.parents)

    for root in resolved_roots:
        for path in root.rglob("raw_result.json"):
            resolved = path.resolve()
            if excluded_path(resolved):
                continue
            try:
                row = json.loads(path.read_text(encoding="utf-8"))
                completed = str(row.get("completed_at") or "")
                model = str(row.get("model") or "")
                if completed[:10] != utc_date or model not in TARGET_MODELS:
                    continue
                if bool(row.get("from_cache")):
                    continue
                bucket = str(row.get("quota_bucket_id") or "")
                if bucket not in TARGET_BUCKETS:
                    unknown.append({"path": str(resolved), "reason": "unknown raw bucket"})
                    continue
                usage = row.get("usage") or {}
                cache_key = str(row.get("cache_key") or canonical_hash(str(resolved)))
                run_root = _artifact_run_root(resolved, "calls")
                run_cache_key = (str(run_root), cache_key)
                raw_bucket_by_run_cache_key[run_cache_key] = bucket
                raw_coverage.add(run_cache_key)
                headers = row.get("safe_response_headers") or {}
                request_id = (
                    str(headers.get("x-request-id") or "")
                    if isinstance(headers, Mapping)
                    else ""
                )
                call_identity = request_id or canonical_hash(str(resolved))
                records[("raw", bucket, model, call_identity)] = {
                    "bucket": bucket,
                    "model": model,
                    "cache_key": cache_key,
                    "prompt_tokens": int(usage.get("prompt_tokens") or 0),
                    "completion_tokens": int(usage.get("completion_tokens") or 0),
                    "source": "raw_result",
                }
            except (OSError, ValueError, TypeError):
                continue

    for root in resolved_roots:
        for path in root.rglob("*.sqlite3"):
            resolved = path.resolve()
            if excluded_path(resolved):
                continue
            try:
                with sqlite3.connect(f"file:{resolved.as_posix()}?mode=ro", uri=True) as db:
                    table = db.execute(
                        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='llm_call_cache'"
                    ).fetchone()
                    if table is None:
                        continue
                    rows = db.execute(
                        """
                        SELECT cache_key, model, usage_json
                        FROM llm_call_cache
                        WHERE substr(created_at, 1, 10) = ?
                        """,
                        (utc_date,),
                    ).fetchall()
            except sqlite3.Error:
                continue
            for cache_key_raw, model_raw, usage_json in rows:
                cache_key = str(cache_key_raw)
                model = str(model_raw)
                if model not in TARGET_MODELS:
                    continue
                run_root = _artifact_run_root(resolved, "cache")
                run_cache_key = (str(run_root), cache_key)
                if run_cache_key in raw_coverage:
                    continue
                bucket = _bucket_from_path(resolved) or raw_bucket_by_run_cache_key.get(
                    run_cache_key
                )
                if bucket not in TARGET_BUCKETS:
                    unknown.append({"path": str(resolved), "reason": f"unknown bucket for {model}"})
                    continue
                try:
                    usage = json.loads(str(usage_json or "{}"))
                except (TypeError, ValueError):
                    usage = {}
                records.setdefault(
                    ("sqlite", bucket, model, canonical_hash(run_cache_key)),
                    {
                        "bucket": bucket,
                        "model": model,
                        "cache_key": cache_key,
                        "prompt_tokens": int(usage.get("prompt_tokens") or 0),
                        "completion_tokens": int(usage.get("completion_tokens") or 0),
                        "source": "sqlite",
                    },
                )

    usage: dict[str, int] = {}
    calls: dict[str, int] = {}
    for row in records.values():
        key = f"{row['bucket']}|{row['model']}"
        usage[key] = usage.get(key, 0) + int(row["prompt_tokens"]) + int(
            row["completion_tokens"]
        )
        calls[key] = calls.get(key, 0) + 1
    return {
        "schema_version": "openai_registry_v4_usage_preflight_v1",
        "utc_date": utc_date,
        "roots": [str(root) for root in resolved_roots],
        "unique_call_count": len(records),
        "usage_by_bucket_model": dict(sorted(usage.items())),
        "calls_by_bucket_model": dict(sorted(calls.items())),
        "unknown_bucket_rows": unknown,
        "preflight_hash": canonical_hash(
            {
                "utc_date": utc_date,
                "usage_by_bucket_model": dict(sorted(usage.items())),
                "calls_by_bucket_model": dict(sorted(calls.items())),
                "unknown_bucket_rows": unknown,
            }
        ),
    }


def _request_metrics(requests: Sequence[Any]) -> dict[str, Any]:
    by_role: dict[str, dict[str, int]] = {}
    for request in requests:
        tokens = estimate_registry_prompt_tokens(request.messages)
        row = by_role.setdefault(
            str(request.role),
            {"calls": 0, "estimated_input_tokens": 0, "max_input_tokens": 0},
        )
        row["calls"] += 1
        row["estimated_input_tokens"] += tokens
        row["max_input_tokens"] = max(row["max_input_tokens"], tokens)
    return by_role


def _aggregate_live_usage(records: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    by_role: dict[str, dict[str, int]] = {}
    by_bucket_model: dict[str, dict[str, int]] = {}
    for raw in records:
        tokens = int(raw.get("prompt_tokens") or 0) + int(raw.get("completion_tokens") or 0)
        for table, key in (
            (by_role, str(raw["role"])),
            (
                by_bucket_model,
                f"{raw['quota_bucket_id']}|{raw['model']}",
            ),
        ):
            row = table.setdefault(key, {"calls": 0, "tokens": 0})
            row["calls"] += 1
            row["tokens"] += tokens
    return {
        "calls": len(records),
        "api_calls": sum(not bool(row.get("from_cache")) for row in records),
        "prompt_tokens": sum(int(row.get("prompt_tokens") or 0) for row in records),
        "completion_tokens": sum(int(row.get("completion_tokens") or 0) for row in records),
        "cached_tokens": sum(int(row.get("cached_tokens") or 0) for row in records),
        "reasoning_tokens": sum(int(row.get("reasoning_tokens") or 0) for row in records),
        "by_role": by_role,
        "by_bucket_model": by_bucket_model,
    }


def run_dry_render(
    *,
    document_path: Path,
    design_doc: Path,
    output_dir: Path,
    frozen_db: Path,
    chapter_id: str = DEFAULT_CHAPTER_ID,
) -> dict[str, Any]:
    output = _ensure_empty_output(output_dir)
    frozen_hash = _verify_frozen_db(frozen_db)
    document, chapter = _load_document(document_path, chapter_id)
    envelope, config, _ = build_run_envelope(
        document_path=document_path, design_doc=design_doc, chapter_id=chapter_id
    )
    lineage = _state_lineage_id(document)
    b0_request = render_b0_request(chapter=chapter, design_doc=design_doc, run_config=config)
    orientation = validate_orientation_response(
        {
            "orientation_draft": "Synthetic transport-sizing orientation for one complete chapter.",
            "narrative_context": {
                "mode": "uncertain",
                "note": "Synthetic chapter-level narrative orientation for transport sizing.",
                "support_block_ids": [str(chapter["blocks"][0]["block_id"])],
            },
            "attention_items": [],
        },
        chapter,
        b0_request_fingerprint=b0_request.request_fingerprint,
    )
    working = ChapterWorkingRegistryV4.create(
        state_lineage_id=lineage,
        chapter_id=chapter_id,
        source_manifest_hash=chapter_source_manifest_hash(chapter),
        parent_snapshot=empty_registry_snapshot_v4(lineage),
    )
    working.install_attention_ledger(orientation["attention_ledger"])
    windows = build_registry_windows(
        chapter,
        target_tokens=config.active_window_source_token_target,
        max_blocks=config.active_window_max_blocks,
        preceding_tail_k=config.preceding_tail_block_cap,
    )
    order = {str(row["block_id"]): int(row.get("order_index") or 0) for row in chapter["blocks"]}
    requests = [b0_request]
    for window in windows:
        request = render_b1_request(
            chapter_id=chapter_id,
            window_id=str(window["window_id"]),
            orientation=orientation,
            active_blocks=window["blocks"],
            context_only_tail=window["context_only_tail"],
            working=working,
            block_order=order,
            design_doc=design_doc,
            run_config=config,
        )
        requests.append(request)
        working.apply_b1_response(
            request=request,
            response={
                "new_entities": [],
                "new_glossary_items": [],
                "surface_updates": [],
                "tickets": [],
            },
        )
    apply_audit_responses(
        working=working,
        requests=[],
        responses=[],
        source_catalog={
            str(row["block_id"]): str(row.get("clean_text") or row.get("source_text") or "")
            for row in chapter["blocks"]
        },
    )
    metrics = _request_metrics(requests)
    b0_reserve = metrics["b0"]["estimated_input_tokens"] + config.b0_output_token_cap
    b1_reserve = metrics["b1"]["estimated_input_tokens"] + (
        metrics["b1"]["calls"] * config.b1_output_token_cap
    )
    auditor_worst_case = config.auditor_calls_per_chapter_cap * (
        config.auditor_input_token_cap + config.auditor_output_token_cap
    )
    report = {
        "schema_version": "literary_m4f_b0b1_v4_dry_render_v1",
        "status": "dry_render_only_no_api",
        "chapter_id": chapter_id,
        "state_lineage_id": lineage,
        "envelope_hash": envelope["envelope_hash"],
        "request_metrics": metrics,
        "reserve_tokens": {
            "b0_actual_input_plus_output_cap": b0_reserve,
            "b1_actual_inputs_plus_output_caps": b1_reserve,
            "auditor_worst_case_input_plus_output_caps": auditor_worst_case,
        },
        "window_count": len(windows),
        "synthetic_auditor_calls": 0,
        "warning": "Auditor share and real semantic quality are not inferred from synthetic output.",
        "frozen_db_sha256": frozen_hash,
    }
    _write_json(output / f"run_envelope_{envelope['envelope_hash'][:12]}.json", envelope)
    _write_json(output / "dry_render_report.json", report)
    return report


def run_live_canary(
    *,
    document_path: Path,
    design_doc: Path,
    output_dir: Path,
    frozen_db: Path,
    approved_envelope_hash: str,
    key_paths: Mapping[str, Path],
    usage_roots: Sequence[Path],
    chapter_id: str = DEFAULT_CHAPTER_ID,
    stop_after_b0: bool = False,
) -> dict[str, Any]:
    output = _ensure_empty_output(output_dir)
    frozen_hash = _verify_frozen_db(frozen_db)
    document, chapter = _load_document(document_path, chapter_id)
    envelope, config, transport = build_run_envelope(
        document_path=document_path, design_doc=design_doc, chapter_id=chapter_id
    )
    if approved_envelope_hash != envelope["envelope_hash"]:
        raise RegistryV4RunError(
            f"approved envelope mismatch; current hash is {envelope['envelope_hash']}"
        )
    preflight = scan_current_utc_usage(roots=usage_roots, exclude_root=output)
    if preflight["unknown_bucket_rows"]:
        raise RegistryV4RunError("quota preflight found current-day OpenAI rows with unknown buckets")
    _write_json(output / f"run_envelope_{envelope['envelope_hash'][:12]}.json", envelope)
    _write_json(output / "quota_preflight.json", preflight)
    manifest = {
        "schema_version": RUN_SCHEMA_VERSION,
        "status": "running",
        "started_at": _now(),
        "chapter_id": chapter_id,
        "envelope_hash": envelope["envelope_hash"],
        "frozen_db_sha256_before": frozen_hash,
        "quota_preflight_hash": preflight["preflight_hash"],
    }
    _write_json(output / "run_manifest.json", manifest)

    real = RealOpenAIRegistryExecutorV3(
        run_config=transport,  # type: ignore[arg-type]
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
        run_config=transport,  # type: ignore[arg-type]
        frozen_db=frozen_db,
    )
    lineage = _state_lineage_id(document)
    chapter_dir = output / "chapters" / chapter_id
    source_catalog = {
        str(row["block_id"]): str(row.get("clean_text") or row.get("source_text") or "")
        for row in chapter["blocks"]
    }
    order = {str(row["block_id"]): int(row.get("order_index") or 0) for row in chapter["blocks"]}
    store = ChapterRegistryStoreV4(output / "registry_store")
    working = ChapterWorkingRegistryV4.create(
        state_lineage_id=lineage,
        chapter_id=chapter_id,
        source_manifest_hash=chapter_source_manifest_hash(chapter),
        parent_snapshot=empty_registry_snapshot_v4(lineage),
    )
    try:
        b0_request = render_b0_request(chapter=chapter, design_doc=design_doc, run_config=config)
        b0_raw = executor.execute(b0_request)
        orientation = validate_orientation_response(
            b0_raw, chapter, b0_request_fingerprint=b0_request.request_fingerprint
        )
        working.install_attention_ledger(orientation["attention_ledger"])
        _write_json(chapter_dir / "orientation.json", orientation)

        if stop_after_b0:
            executor.record_chapter_validation(
                chapter_id=chapter_id,
                status="accepted_b0_only",
                payload={
                    "orientation_hash": orientation["orientation_hash"],
                    "narrative_mode": orientation["narrative_context"]["mode"],
                },
            )
            usage = _aggregate_live_usage(executor.records)
            attention_report = orientation["attention_validation_report"]
            report = {
                "schema_version": "literary_m4f_b0_v4_1_live_report_v1",
                "status": "accepted_b0_only_canary",
                "chapter_id": chapter_id,
                "state_lineage_id": lineage,
                "orientation_hash": orientation["orientation_hash"],
                "narrative_mode": orientation["narrative_context"]["mode"],
                "attention_input_count": attention_report["input_count"],
                "attention_accepted_count": attention_report["accepted_count"],
                "attention_dropped_count": attention_report["dropped_count"],
                "envelope_hash": envelope["envelope_hash"],
                "usage": usage,
                "frozen_db_sha256_after": _verify_frozen_db(frozen_db),
                "completed_at": _now(),
            }
            _write_json(chapter_dir / "b0_report.json", report)
            _write_json(
                output / "run_manifest.json",
                {
                    **manifest,
                    "status": "accepted_b0_only",
                    "completed_at": report["completed_at"],
                },
            )
            return report

        windows = build_registry_windows(
            chapter,
            target_tokens=config.active_window_source_token_target,
            max_blocks=config.active_window_max_blocks,
            preceding_tail_k=config.preceding_tail_block_cap,
        )
        for index, window in enumerate(windows, 1):
            request = render_b1_request(
                chapter_id=chapter_id,
                window_id=str(window["window_id"]),
                orientation=orientation,
                active_blocks=window["blocks"],
                context_only_tail=window["context_only_tail"],
                working=working,
                block_order=order,
                design_doc=design_doc,
                run_config=config,
            )
            response = executor.execute(request)
            working.apply_b1_response(request=request, response=response)
            _write_json(
                chapter_dir
                / "working_revisions"
                / f"{index:02d}_{working.revision_hash[:20]}.json",
                working.snapshot(),
            )

        exceptions = build_exception_components(working)
        _write_json(chapter_dir / "exception_manifest.json", exceptions)
        auditor_requests = render_auditor_requests(
            working=working,
            orientation=orientation,
            source_catalog=source_catalog,
            block_order=order,
            design_doc=design_doc,
            run_config=config,
        )
        auditor_responses = [executor.execute(request) for request in auditor_requests]
        decisions = apply_audit_responses(
            working=working,
            requests=auditor_requests,
            responses=auditor_responses,
            source_catalog=source_catalog,
        )
        _write_json(chapter_dir / "audit_decisions.json", decisions)

        generation = build_registry_generation(
            working=working,
            orientation=orientation,
            b0_request=b0_request,
            source_catalog=source_catalog,
            run_config=config,
            audit_decisions=decisions,
        )
        store.commit(generation, expected_parent=None)
        committed = store.snapshot(lineage, generation.generation_id)
        b2_manifests = [
            build_b2_candidate_manifest(
                chapter_id=chapter_id,
                active_blocks=window["blocks"],
                registry_snapshot=committed,
                candidate_count_cap=32,
            )
            for window in windows
        ]
        _write_json(chapter_dir / "prepared_generation.json", generation.to_dict())
        _write_json(chapter_dir / "committed_snapshot.json", committed)
        _write_json(chapter_dir / "b2_candidate_manifests.json", b2_manifests)
        executor.record_chapter_validation(
            chapter_id=chapter_id,
            status="accepted",
            payload={
                "generation_id": generation.generation_id,
                "snapshot_hash": committed["snapshot_hash"],
            },
        )
        usage = _aggregate_live_usage(executor.records)
        report = {
            "schema_version": "literary_m4f_b0b1_v4_live_report_v1",
            "status": "accepted_one_chapter_canary",
            "chapter_id": chapter_id,
            "state_lineage_id": lineage,
            "generation_id": generation.generation_id,
            "envelope_hash": envelope["envelope_hash"],
            "window_count": len(windows),
            "auditor_component_count": exceptions["component_count"],
            "auditor_call_count": len(auditor_requests),
            "entity_count": len(committed["entities"]),
            "glossary_count": len(committed["glossary_items"]),
            "global_alias_count": len(committed["global_aliases"]),
            "local_reference_count": len(committed["block_local_references"]),
            "ticket_count": len(committed["tickets"]),
            "pending_entity_count": sum(
                row.get("status") == "pending" for row in committed["entities"]
            ),
            "usage": usage,
            "frozen_db_sha256_after": _verify_frozen_db(frozen_db),
            "completed_at": _now(),
        }
        _write_json(chapter_dir / "chapter_report.json", report)
        _write_json(
            output / "run_manifest.json",
            {**manifest, "status": "accepted", "completed_at": report["completed_at"]},
        )
        return report
    except Exception as exc:
        executor.record_chapter_validation(
            chapter_id=chapter_id,
            status="failed",
            payload={"error_type": type(exc).__name__, "message": _redacted_error(exc)},
        )
        failure = {
            "schema_version": "literary_m4f_b0b1_v4_failure_v1",
            "status": "halted_fail_closed",
            "chapter_id": chapter_id,
            "error_type": type(exc).__name__,
            "message": _redacted_error(exc),
            "usage": _aggregate_live_usage(executor.records),
            "frozen_db_sha256_after": _verify_frozen_db(frozen_db),
            "failed_at": _now(),
        }
        _write_json(chapter_dir / "chapter_failure.json", failure)
        _write_json(
            output / "run_manifest.json",
            {**manifest, "status": "halted", "failed_at": failure["failed_at"]},
        )
        raise


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Bounded v4 chapter-registry dry/live canary")
    parser.add_argument("mode", choices=("dry", "b0-live", "live"))
    parser.add_argument("--document", type=Path, default=DEFAULT_DOCUMENT)
    parser.add_argument("--design-doc", type=Path, default=DEFAULT_DESIGN_DOC)
    parser.add_argument("--frozen-db", type=Path, default=DEFAULT_FROZEN_DB)
    parser.add_argument("--chapter-id", default=DEFAULT_CHAPTER_ID)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--approved-envelope-hash")
    parser.add_argument("--key-1", type=Path)
    parser.add_argument("--key-2", type=Path)
    parser.add_argument("--usage-root", type=Path, action="append", default=[])
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.mode == "dry":
        report = run_dry_render(
            document_path=args.document,
            design_doc=args.design_doc,
            output_dir=args.output_dir,
            frozen_db=args.frozen_db,
            chapter_id=args.chapter_id,
        )
    else:
        if not args.approved_envelope_hash or not args.key_1 or not args.key_2:
            raise RegistryV4RunError(
                "live mode requires approved envelope hash and two key file paths"
            )
        roots = args.usage_root or [RUNTIME_ROOT / "data"]
        report = run_live_canary(
            document_path=args.document,
            design_doc=args.design_doc,
            output_dir=args.output_dir,
            frozen_db=args.frozen_db,
            approved_envelope_hash=args.approved_envelope_hash,
            key_paths={"openai-row1": args.key_1, "openai-row2": args.key_2},
            usage_roots=roots,
            chapter_id=args.chapter_id,
            stop_after_b0=args.mode == "b0-live",
        )
    print(canonical_json(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
