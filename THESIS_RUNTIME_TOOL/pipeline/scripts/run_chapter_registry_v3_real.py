from __future__ import annotations

import argparse
from copy import deepcopy
from dataclasses import asdict
from datetime import UTC, datetime, timezone
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import time
from typing import Any, Mapping, Sequence

from pipeline.agents.llm_client import estimate_prompt_tokens
from pipeline.literary.checkpoint import canonical_hash, canonical_json, file_sha256
from pipeline.literary.chapter_registry_schema_v3 import (
    ALIAS_SCOPE_POLICY_VERSION,
    AUDIT_SCHEMA_VERSION,
    B2_RESCAN_POLICY_VERSION,
    CANDIDATE_POLICY_VERSION,
    DELTA_SCHEMA_VERSION,
    ORIENTATION_SCHEMA_VERSION,
    PROMPT_IDS,
    REGISTRY_SCHEMA_VERSION,
    RegistryBudgetError,
    RegistryContractError,
    RunConfigV3,
    VALIDATOR_VERSION,
)
from pipeline.literary.chapter_registry_v3 import (
    ChapterRegistryStoreV3,
    build_registry_windows,
    empty_registry_snapshot_v3,
    estimate_registry_prompt_tokens,
    run_synthetic_registry_chapter_v3,
    select_candidate_packets,
)
from pipeline.scripts.run_chapter_registry_v2_real import (
    ExecutedRegistryCall,
    RealOpenAIRegistryExecutor,
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
CHAPTER_IDS = ("wh_ch01", "wh_ch02")
RESPONSE_FORMAT_JSON = {"type": "json_object"}
REAL_RUN_SCHEMA_VERSION = "literary_m4f_b0b1_v3_phase_d_real_v1"
DEFAULT_KEYS = {
    "openai-row1": REPO_ROOT / "OPENAI-KEY-1.txt",
    "openai-row2": REPO_ROOT / "OPENAI-KEY-2.txt",
}
RUNTIME_SOURCE_FILES = (
    RUNTIME_ROOT / "pipeline" / "literary" / "chapter_registry_schema_v3.py",
    RUNTIME_ROOT / "pipeline" / "literary" / "chapter_registry_v3.py",
    Path(__file__).resolve(),
)


class PhaseCError(RuntimeError):
    """Raised when dry-render cannot produce an auditable approval artifact."""


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(canonical_json(value) + "\n", encoding="utf-8")


def _normalized_text_sha256(path: Path) -> str:
    text = Path(path).read_text(encoding="utf-8")
    return sha256(text.encode("utf-8")).hexdigest()


def _load_document(path: Path, chapter_ids: Sequence[str]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    value = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise PhaseCError("document must be an object")
    chapters = [dict(row) for row in value.get("chapters") or []]
    by_id = {str(row.get("chapter_id") or ""): row for row in chapters}
    missing = [chapter_id for chapter_id in chapter_ids if chapter_id not in by_id]
    if missing:
        raise PhaseCError(f"document lacks requested chapters: {missing}")
    return value, [by_id[chapter_id] for chapter_id in chapter_ids]


def _quota_gates() -> tuple[dict[str, dict[str, Any]], dict[str, tuple[str, ...]]]:
    gates: dict[str, dict[str, Any]] = {}
    b0_auditor: list[str] = []
    b1: list[str] = []
    for row in ("row1", "row2"):
        bucket = f"openai-{row}"
        strong_id = f"{bucket}-gpt54"
        lite_id = f"{bucket}-mini"
        gates[strong_id] = {
            "quota_bucket_id": bucket,
            "model_id": "gpt-5.4",
            "rpm": None,
            "tpm": None,
            "rpd": None,
            "internal_utc_day_token_cap": 225000,
        }
        gates[lite_id] = {
            "quota_bucket_id": bucket,
            "model_id": "gpt-5.4-mini",
            "rpm": None,
            "tpm": None,
            "rpd": None,
            "internal_utc_day_token_cap": 2250000,
        }
        b0_auditor.append(strong_id)
        b1.append(lite_id)
    return gates, {"b0": tuple(b0_auditor), "b1": tuple(b1), "auditor": tuple(b0_auditor)}


def draft_run_config_v3() -> RunConfigV3:
    gates, role_gates = _quota_gates()
    return RunConfigV3(
        b0_model_id="gpt-5.4",
        b0_reasoning_effort="none",
        b0_temperature=1.0,
        b0_seed=20260612,
        b0_output_cap=2048,
        b1_model_id="gpt-5.4-mini",
        b1_reasoning_effort="none",
        b1_temperature=1.0,
        b1_seed=20260612,
        b1_output_cap=4096,
        auditor_model_id="gpt-5.4",
        auditor_reasoning_effort="none",
        auditor_temperature=1.0,
        auditor_seed=20260612,
        auditor_output_cap=8192,
        b1_window_target_tokens=500,
        b1_window_max_blocks=8,
        context_only_tail_k=2,
        recency_k=8,
        candidate_card_count_cap=16,
        candidate_card_token_cap=3500,
        candidate_packet_count_cap=32,
        targeted_recall_call_cap=4,
        ticket_component_cap=32,
        auditor_call_cap=32,
        auditor_input_token_cap=12000,
        auditor_output_token_cap=8192,
        ticket_share_warning=0.45,
        ticket_share_halt=0.80,
        component_share_warning=0.35,
        component_share_halt=0.70,
        b0_input_cap=18000,
        b1_input_cap=14000,
        pricing_usd_per_million={
            role: {"input": None, "cached_input": None, "output": None}
            for role in ("b0", "b1", "auditor")
        },
        quota_gates=gates,
        role_quota_gate_ids=role_gates,
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


class DryRenderExecutorV3:
    """Adaptive synthetic fixture for transport sizing, never semantic evaluation."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.responses: dict[str, dict[str, Any]] = {}
        self.b1_call_count = 0

    @staticmethod
    def _first_surface(request: Any) -> tuple[str, str] | None:
        blocks = request.sections.get("active_window_blocks") or []
        for block in blocks:
            text = str(block.get("text") or "")
            match = re.search(r"[^\W\d_]+(?:['-][^\W\d_]+)?", text, flags=re.UNICODE)
            if match is not None:
                return match.group(0), str(block["block_id"])
        return None

    def execute(self, request: Any) -> dict[str, Any]:
        if request.role == "b0":
            response: dict[str, Any] = {
                "gist": "Synthetic dry-render orientation; not a semantic result.",
                "narrator_hypotheses": [],
                "salient_registry_checklist": [],
            }
        elif request.role == "b1":
            self.b1_call_count += 1
            selected = self._first_surface(request)
            tickets: list[dict[str, Any]] = []
            if selected is not None and self.b1_call_count % 4 == 1:
                surface, block_id = selected
                tickets.append(
                    {
                        "ticket_type": "surface_class_review",
                        "surface": surface,
                        "source_block_ids": [block_id],
                        "candidate_entity_ids": [],
                        "candidate_glossary_ids": [],
                        "referent_kind_claim": None,
                        "proposed_short_description": None,
                        "reason": "Synthetic exception used only to size the bounded Auditor path.",
                    }
                )
            response = {"new_entities": [], "new_glossary_items": [], "tickets": tickets}
        elif request.role == "auditor":
            component = request.sections["ticket_component"]
            response = {
                "ticket_dispositions": [
                    {
                        "ticket_id": ticket_id,
                        "action": "reject_noise",
                        "source_entity_id": None,
                        "target_entity_id": None,
                        "source_glossary_id": None,
                        "target_glossary_id": None,
                        "resolved_referent_kind": None,
                        "revised_identity_summary": None,
                        "name_class": None,
                        "resolution_note": "Synthetic dry-render disposition; no semantic authority.",
                    }
                    for ticket_id in component["ticket_ids"]
                ]
            }
        else:
            raise RegistryContractError(f"unsupported dry-render role: {request.role}")
        response = json.loads(canonical_json(response))
        self.responses[request.request_fingerprint] = response
        self.calls.append(
            {
                "role": request.role,
                "chapter_id": request.chapter_id,
                "window_id": request.window_id,
                "request_fingerprint": request.request_fingerprint,
                "response_hash": canonical_hash(response),
            }
        )
        return response


def _utc_now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()


def _git_head() -> str:
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=REPO_ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _safe_name(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", value).strip("._") or "unit"


def _redacted_error(exc: Exception) -> str:
    return re.sub(r"sk-[A-Za-z0-9_-]+", "[REDACTED]", str(exc))


def _write_new_json(path: Path, value: Mapping[str, Any] | Sequence[Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = (canonical_json(value) + "\n").encode("utf-8")
    try:
        with path.open("xb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
    except FileExistsError as exc:
        raise PhaseCError(f"append-only artifact already exists: {path}") from exc


def _verify_frozen_db(path: Path) -> str:
    actual = file_sha256(path).upper()
    if actual != FROZEN_DB_SHA256:
        raise PhaseCError(f"frozen D2L database hash drift: {actual}")
    return actual


def _usage_key(bucket_id: str, model_id: str) -> str:
    return f"{bucket_id}|{model_id}"


def scan_prior_openai_usage(
    *, report_root: Path, exclude_root: Path | None = None
) -> dict[str, Any]:
    """Audit same-UTC-day cache usage without reading or exposing credential bytes."""

    utc_date = datetime.now(UTC).date().isoformat()
    excluded = Path(exclude_root).resolve() if exclude_root is not None else None
    usage: dict[str, int] = {}
    calls: dict[str, int] = {}
    sources: list[dict[str, Any]] = []
    for cache_path in sorted(Path(report_root).rglob("*.sqlite3")):
        resolved = cache_path.resolve()
        if excluded is not None and (resolved == excluded or excluded in resolved.parents):
            continue
        lowered_parts = {part.casefold() for part in resolved.parts}
        if lowered_parts & {".pytest_cache", ".testtmp", ".tmp"}:
            continue
        bucket_id = next(
            (bucket for bucket in DEFAULT_KEYS if bucket in resolved.parts), None
        )
        if bucket_id is None:
            continue
        try:
            with sqlite3.connect(str(resolved)) as connection:
                table = connection.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='llm_call_cache'"
                ).fetchone()
                if table is None:
                    continue
                rows = connection.execute(
                    """
                    SELECT model, usage_json
                    FROM llm_call_cache
                    WHERE substr(created_at, 1, 10) = ?
                    """,
                    (utc_date,),
                ).fetchall()
        except sqlite3.Error:
            continue
        by_model: dict[str, dict[str, int]] = {}
        for model, usage_json in rows:
            parsed = json.loads(str(usage_json))
            tokens = int(parsed.get("prompt_tokens") or 0) + int(
                parsed.get("completion_tokens") or 0
            )
            key = _usage_key(bucket_id, str(model))
            usage[key] = usage.get(key, 0) + tokens
            calls[key] = calls.get(key, 0) + 1
            row = by_model.setdefault(str(model), {"calls": 0, "tokens": 0})
            row["calls"] += 1
            row["tokens"] += tokens
        if by_model:
            sources.append(
                {
                    "cache_path": resolved.relative_to(REPO_ROOT.resolve()).as_posix(),
                    "quota_bucket_id": bucket_id,
                    "by_model": by_model,
                }
            )
    return {
        "schema_version": "openai_prior_usage_preflight_v1",
        "utc_date": utc_date,
        "usage_by_bucket_model": dict(sorted(usage.items())),
        "calls_by_bucket_model": dict(sorted(calls.items())),
        "sources": sources,
    }


class RealOpenAIRegistryExecutorV3(RealOpenAIRegistryExecutor):
    """OpenAI transport with quota accounting isolated per physical key and model."""

    def __init__(
        self,
        *,
        run_config: RunConfigV3,
        run_root: Path,
        credential_paths: Mapping[str, Path],
        prior_usage_by_bucket_model: Mapping[str, int] | None = None,
        prior_calls_by_bucket_model: Mapping[str, int] | None = None,
        min_interval_seconds: float = 2.0,
        local_rpd_cap: int = 100,
        response_formats_by_role: Mapping[str, Mapping[str, Any]] | None = None,
        structured_output_contracts_by_role: Mapping[
            str, Mapping[str, Any]
        ]
        | None = None,
    ) -> None:
        super().__init__(
            run_config=run_config,  # type: ignore[arg-type]
            run_root=run_root,
            credential_paths=credential_paths,
            prior_usage_by_bucket={},
            prior_calls_by_bucket={},
            min_interval_seconds=min_interval_seconds,
            local_rpd_cap=local_rpd_cap,
        )
        self._usage_by_bucket_model = {
            str(key): int(value)
            for key, value in (prior_usage_by_bucket_model or {}).items()
        }
        self._calls_by_bucket_model = {
            str(key): int(value)
            for key, value in (prior_calls_by_bucket_model or {}).items()
        }
        self._response_formats_by_role = {
            str(role): deepcopy(dict(response_format))
            for role, response_format in (response_formats_by_role or {}).items()
        }
        self._structured_output_contracts_by_role = {
            str(role): deepcopy(dict(contract))
            for role, contract in (structured_output_contracts_by_role or {}).items()
        }

    def response_format_for_role(self, role: str) -> dict[str, Any]:
        return deepcopy(self._response_formats_by_role.get(role, RESPONSE_FORMAT_JSON))

    def structured_output_contract_for_role(
        self, role: str
    ) -> dict[str, Any] | None:
        contract = self._structured_output_contracts_by_role.get(role)
        return deepcopy(contract) if contract is not None else None

    @property
    def public_manifest(self) -> Mapping[str, Any]:
        manifest = dict(super().public_manifest)
        manifest["executor"] = "openai_chat_completions_registry_v3"
        manifest["quota_accounting_scope"] = "physical_bucket_plus_model"
        manifest["prior_usage_by_bucket_model"] = dict(
            sorted(self._usage_by_bucket_model.items())
        )
        manifest["prior_calls_by_bucket_model"] = dict(
            sorted(self._calls_by_bucket_model.items())
        )
        manifest["structured_output_contracts_by_role"] = deepcopy(
            self._structured_output_contracts_by_role
        )
        manifest["response_format_hashes_by_role"] = {
            role: canonical_hash(response_format)
            for role, response_format in sorted(
                self._response_formats_by_role.items()
            )
        }
        return manifest

    def _role_contract(self, role: str) -> dict[str, Any]:
        return {
            "model": getattr(self.config, f"{role}_model_id"),
            "temperature": getattr(self.config, f"{role}_temperature"),
            "seed": getattr(self.config, f"{role}_seed"),
            "reasoning_effort": getattr(self.config, f"{role}_reasoning_effort"),
            "verbosity": None,
            "max_output_tokens": getattr(self.config, f"{role}_output_cap"),
            "prompt_token_cap": (
                self.config.auditor_input_token_cap
                if role == "auditor"
                else getattr(self.config, f"{role}_input_cap")
            ),
        }

    def execute(self, request: Any) -> ExecutedRegistryCall:
        role = str(request.role)
        if role not in {"b0", "b1", "auditor"}:
            raise PhaseCError(f"unsupported registry role: {role}")
        response_format = self.response_format_for_role(role)
        prompt_tokens = estimate_prompt_tokens(request.messages, response_format)
        output_cap = int(getattr(self.config, f"{role}_output_cap"))
        selected_gate: str | None = None
        for gate_id in self._ordered_gate_ids(role):
            gate = self.config.quota_gates[gate_id]
            bucket_id = str(gate["quota_bucket_id"])
            model_id = str(gate["model_id"])
            key = _usage_key(bucket_id, model_id)
            projected = self._usage_by_bucket_model.get(key, 0) + prompt_tokens + output_cap
            if (
                projected <= int(gate["internal_utc_day_token_cap"])
                and self._calls_by_bucket_model.get(key, 0) < self.local_rpd_cap
            ):
                selected_gate = gate_id
                break
        if selected_gate is None:
            raise RegistryBudgetError(f"no quota bucket can reserve {role} request")
        gate = self.config.quota_gates[selected_gate]
        bucket_id = str(gate["quota_bucket_id"])
        model_id = str(gate["model_id"])
        key = _usage_key(bucket_id, model_id)
        prior = self._last_call.get(bucket_id)
        if prior is not None:
            remaining = self.min_interval_seconds - (time.monotonic() - prior)
            if remaining > 0:
                time.sleep(remaining)
        client, headers = self._client(role, selected_gate)
        result = client.call(
            [dict(row) for row in request.messages],
            response_format=response_format,
            tag=f"registry_v3:{role}:{request.chapter_id}:{request.window_id or 'chapter'}",
        )
        self._last_call[bucket_id] = time.monotonic()
        if not result.from_cache:
            self._usage_by_bucket_model[key] = (
                self._usage_by_bucket_model.get(key, 0) + result.usage.total_billable_tokens
            )
            self._calls_by_bucket_model[key] = self._calls_by_bucket_model.get(key, 0) + 1
        gate_ids = list(self.config.role_quota_gate_ids[role])
        self._next_gate[role] = (gate_ids.index(selected_gate) + 1) % len(gate_ids)
        return ExecutedRegistryCall(
            result=result,
            quota_gate_id=selected_gate,
            quota_bucket_id=bucket_id,
            credential_commitment=self._commitments[bucket_id],
            safe_response_headers=dict(headers),
            completed_at=_utc_now(),
        )


class PersistedRealExecutorV3:
    """Adapt real OpenAI calls to the pure v3 chapter fold while preserving raw evidence."""

    def __init__(
        self,
        *,
        executor: RealOpenAIRegistryExecutorV3,
        run_root: Path,
        run_config: RunConfigV3,
        frozen_db: Path,
    ) -> None:
        self.executor = executor
        self.run_root = Path(run_root)
        self.run_config = run_config
        self.frozen_db = Path(frozen_db)
        self.records: list[dict[str, Any]] = []
        indexes = []
        for path in (self.run_root / "calls").glob("[0-9][0-9][0-9][0-9]_*"):
            match = re.match(r"(\d{4})_", path.name)
            if path.is_dir() and match:
                indexes.append(int(match.group(1)))
        self.call_index = max(indexes, default=0)

    @property
    def public_manifest(self) -> Mapping[str, Any]:
        return self.executor.public_manifest

    def execute(self, request: Any) -> dict[str, Any]:
        self.call_index += 1
        suffix = request.window_id or "chapter"
        call_dir = self.run_root / "calls" / (
            f"{self.call_index:04d}_{request.role}_{_safe_name(request.chapter_id)}_"
            f"{_safe_name(suffix)}"
        )
        response_format = self.executor.response_format_for_role(str(request.role))
        _write_new_json(
            call_dir / "request.json",
            {
                "registry_request": request.to_dict(),
                "run_config_hash": self.run_config.config_hash,
                "eligible_quota_gate_ids": list(
                    self.run_config.role_quota_gate_ids[request.role]
                ),
                "response_format": response_format,
                "response_format_hash": canonical_hash(response_format),
                "structured_output_contract": (
                    self.executor.structured_output_contract_for_role(
                        str(request.role)
                    )
                ),
            },
        )
        _verify_frozen_db(self.frozen_db)
        try:
            executed = self.executor.execute(request)
        except Exception as exc:
            _write_new_json(
                call_dir / "attempt_01" / "transport_error.json",
                {
                    "error_type": type(exc).__name__,
                    "message": _redacted_error(exc),
                    "recorded_at": _utc_now(),
                },
            )
            raise
        _verify_frozen_db(self.frozen_db)
        result = executed.result
        _write_new_json(
            call_dir / "attempt_01" / "raw_result.json",
            {
                "response_text": result.text,
                "parsed_json": result.parsed_json,
                "json_error": result.json_error,
                "model": result.model,
                "system_fingerprint": result.system_fingerprint,
                "usage": asdict(result.usage),
                "cost_usd": result.cost_usd,
                "latency_ms": result.latency_ms,
                "from_cache": result.from_cache,
                "cache_key": result.cache_key,
                "quota_gate_id": executed.quota_gate_id,
                "quota_bucket_id": executed.quota_bucket_id,
                "credential_commitment": executed.credential_commitment,
                "safe_response_headers": dict(executed.safe_response_headers),
                "completed_at": executed.completed_at,
            },
        )
        if result.json_error or not isinstance(result.parsed_json, Mapping):
            _write_new_json(
                call_dir / "attempt_01" / "transport_validation.json",
                {
                    "status": "failed",
                    "error_type": "json_contract",
                    "message": result.json_error or "parsed response is not an object",
                },
            )
            raise PhaseCError(f"{request.role} returned invalid JSON at {call_dir.name}")
        _write_new_json(
            call_dir / "attempt_01" / "transport_validation.json",
            {
                "status": "parsed_pending_chapter_contract",
                "parsed_payload_hash": canonical_hash(result.parsed_json),
            },
        )
        record = {
            "call_dir": call_dir,
            "role": str(request.role),
            "chapter_id": str(request.chapter_id),
            "window_id": request.window_id,
            "request_fingerprint": request.request_fingerprint,
            "parsed_json": dict(result.parsed_json),
            "quota_bucket_id": executed.quota_bucket_id,
            "model": result.model,
            "from_cache": result.from_cache,
            "prompt_tokens": result.usage.prompt_tokens,
            "cached_tokens": result.usage.cached_tokens,
            "completion_tokens": result.usage.completion_tokens,
            "reasoning_tokens": result.usage.reasoning_tokens,
            "cost_usd": result.cost_usd,
            "latency_ms": result.latency_ms,
        }
        self.records.append(record)
        return dict(result.parsed_json)

    def record_chapter_validation(
        self, *, chapter_id: str, status: str, payload: Mapping[str, Any]
    ) -> None:
        for record in self.records:
            if record["chapter_id"] != chapter_id:
                continue
            path = Path(record["call_dir"]) / "attempt_01" / "chapter_validation.json"
            if path.exists():
                continue
            _write_new_json(path, {"status": status, **dict(payload)})


def _aggregate_usage(records: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    by_role: dict[str, dict[str, int]] = {}
    by_bucket_model: dict[str, dict[str, int]] = {}
    for row in records:
        tokens = int(row.get("prompt_tokens") or 0) + int(row.get("completion_tokens") or 0)
        for table, key in (
            (by_role, str(row["role"])),
            (
                by_bucket_model,
                _usage_key(str(row["quota_bucket_id"]), str(row["model"])),
            ),
        ):
            entry = table.setdefault(key, {"calls": 0, "tokens": 0})
            entry["calls"] += 1
            entry["tokens"] += tokens
    return {
        "calls": len(records),
        "api_calls": sum(not bool(row.get("from_cache")) for row in records),
        "prompt_tokens": sum(int(row.get("prompt_tokens") or 0) for row in records),
        "cached_tokens": sum(int(row.get("cached_tokens") or 0) for row in records),
        "completion_tokens": sum(
            int(row.get("completion_tokens") or 0) for row in records
        ),
        "reasoning_tokens": sum(
            int(row.get("reasoning_tokens") or 0) for row in records
        ),
        "cost_usd": round(sum(float(row.get("cost_usd") or 0) for row in records), 12),
        "by_role": by_role,
        "by_bucket_model": by_bucket_model,
    }


def _ensure_empty_output(path: Path) -> None:
    if path.exists() and any(path.iterdir()):
        raise PhaseCError(f"output directory is not empty: {path}")
    path.mkdir(parents=True, exist_ok=True)


def _request_metrics(requests: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    by_role: dict[str, dict[str, int]] = {}
    b1_packet_tokens: list[dict[str, Any]] = []
    for request in requests:
        role = str(request["role"])
        messages = request["messages"]
        tokens = estimate_registry_prompt_tokens(messages)
        row = by_role.setdefault(
            role,
            {"calls": 0, "estimated_input_tokens": 0, "max_input_tokens": 0},
        )
        row["calls"] += 1
        row["estimated_input_tokens"] += tokens
        row["max_input_tokens"] = max(row["max_input_tokens"], tokens)
        if role == "b1":
            sections = request["sections"]
            packet_tokens = max(
                0,
                len(
                    canonical_json(
                        {
                            "surface_candidate_packets": sections.get("surface_candidate_packets") or [],
                            "unmatched_recency_cards": sections.get("unmatched_recency_cards") or [],
                        }
                    )
                )
                // 4,
            )
            b1_packet_tokens.append(
                {
                    "chapter_id": request["chapter_id"],
                    "window_id": request["window_id"],
                    "candidate_packet_tokens": packet_tokens,
                    "request_tokens": tokens,
                }
            )
    return {
        "by_role": by_role,
        "total_calls": sum(row["calls"] for row in by_role.values()),
        "total_estimated_input_tokens": sum(
            row["estimated_input_tokens"] for row in by_role.values()
        ),
        "candidate_packet_tokens_by_window": b1_packet_tokens,
        "worst_b1_window_tokens": max(
            (row["request_tokens"] for row in b1_packet_tokens), default=0
        ),
    }


def _candidate_packet_stress(config: RunConfigV3) -> dict[str, Any]:
    """Size the configured packet cap with neutral synthetic cards, not book answers."""

    snapshot = empty_registry_snapshot_v3("synthetic-packet-stress")
    names = [f"SyntheticName{index:02d}" for index in range(1, config.candidate_card_count_cap + 1)]
    snapshot["entities"] = [
        {
            "entity_id": f"ent3_synthetic_{index:02d}",
            "canonical_surface": name,
            "name_class": "proper_name",
            "referent_kind": "unknown",
            "identity_summary": "Neutral synthetic card used only for transport sizing.",
            "created_from_block_ids": ["synthetic_b001"],
            "support_block_ids": ["synthetic_b001"],
            "status": "confirmed",
            "revision_hash": canonical_hash({"name": name}),
        }
        for index, name in enumerate(names, 1)
    ]
    snapshot["snapshot_hash"] = canonical_hash(
        {key: value for key, value in snapshot.items() if key != "snapshot_hash"}
    )
    selection = select_candidate_packets(
        snapshot=snapshot,
        working_revision_hash="work3_synthetic_packet_stress",
        active_blocks=[
            {
                "block_id": "synthetic_b001",
                "order_index": 1,
                "block_type": "paragraph",
                "clean_text": " ".join(names),
            }
        ],
        context_only_tail=[],
        block_order={"synthetic_b001": 1},
        recency_k=0,
        card_count_cap=config.candidate_card_count_cap,
        card_token_cap=config.candidate_card_token_cap,
        packet_count_cap=config.candidate_packet_count_cap,
    )
    encoded = canonical_json(
        {
            "surface_candidate_packets": selection["surface_candidate_packets"],
            "unmatched_recency_cards": selection["unmatched_recency_cards"],
        }
    )
    return {
        "synthetic_only": True,
        "packet_count": len(selection["surface_candidate_packets"]),
        "candidate_count": sum(
            len(row["candidate_entities"]) + len(row["candidate_glossary_items"])
            for row in selection["surface_candidate_packets"]
        ),
        "context_bytes": len(encoded),
        "estimated_context_tokens": max(1, len(encoded) // 4),
        "manifest_hash": selection["candidate_selection_manifest"]["manifest_hash"],
        "warning": "Neutral cap stress, not a semantic or real-book candidate distribution.",
    }


def run_phase_c_dry_render(
    *,
    document_path: Path,
    design_doc: Path,
    output_dir: Path,
    frozen_db: Path,
    chapter_ids: Sequence[str] = CHAPTER_IDS,
    config: RunConfigV3 | None = None,
) -> dict[str, Any]:
    output_dir = Path(output_dir).resolve()
    _ensure_empty_output(output_dir)
    frozen_before = file_sha256(frozen_db).upper()
    if frozen_before != FROZEN_DB_SHA256:
        raise PhaseCError("frozen D2L database hash drift before dry render")
    _, chapters = _load_document(document_path, chapter_ids)
    run_config = config or draft_run_config_v3()
    state_lineage_id = canonical_hash(
        {
            "document_sha256": file_sha256(document_path),
            "ordered_chapter_ids": [str(row["chapter_id"]) for row in chapters],
            "registry_schema": REGISTRY_SCHEMA_VERSION,
        }
    )
    store = ChapterRegistryStoreV3(output_dir / "registry_store")
    executor = DryRenderExecutorV3()
    parent = empty_registry_snapshot_v3(state_lineage_id)
    chapter_reports: list[dict[str, Any]] = []
    all_requests: list[dict[str, Any]] = []
    for chapter in chapters:
        result = run_synthetic_registry_chapter_v3(
            chapter=chapter,
            state_lineage_id=state_lineage_id,
            parent_snapshot=parent,
            executor=executor,
            design_doc=design_doc,
            run_config=run_config,
            store=store,
        )
        parent = result["snapshot"]
        all_requests.extend(result["requests"])
        chapter_reports.append(
            {
                "chapter_id": chapter["chapter_id"],
                "window_count": len(result["windows"]),
                "request_count": len(result["requests"]),
                "targeted_recall_call_count": len(result["targeted_recall"]),
                "auditor_decision_count": len(result["audits"]),
                "entity_count": len(result["snapshot"]["entities"]),
                "glossary_count": len(result["snapshot"]["glossary_items"]),
                "ticket_count": len(result["snapshot"]["tickets"]),
                "generation_id": result["generation"]["generation_id"],
                "b2_candidate_count": result["b2_candidate_preview"]["selected_count"],
            }
        )
    for index, call in enumerate(executor.calls, 1):
        request = next(
            row for row in all_requests if row["request_fingerprint"] == call["request_fingerprint"]
        )
        call_dir = output_dir / "calls" / f"{index:04d}_{call['role']}"
        _write_json(call_dir / "request.json", request)
        _write_json(call_dir / "synthetic_response.json", executor.responses[call["request_fingerprint"]])
    metrics = _request_metrics(all_requests)
    synthetic_ticket_count = sum(
        len(response.get("tickets") or [])
        for response in executor.responses.values()
        if isinstance(response, Mapping)
    )
    b1_calls = metrics["by_role"].get("b1", {}).get("calls", 0)
    synthetic_output_tokens: dict[str, int] = {}
    for call in executor.calls:
        role = str(call["role"])
        response = executor.responses[call["request_fingerprint"]]
        synthetic_output_tokens[role] = synthetic_output_tokens.get(role, 0) + max(
            1, len(canonical_json(response)) // 4
        )
    role_caps = {
        "b0": (run_config.b0_input_cap, run_config.b0_output_cap),
        "b1": (run_config.b1_input_cap, run_config.b1_output_cap),
        "auditor": (run_config.auditor_input_token_cap, run_config.auditor_output_token_cap),
    }
    budget_envelope = {
        role: {
            **metrics["by_role"].get(
                role,
                {"calls": 0, "estimated_input_tokens": 0, "max_input_tokens": 0},
            ),
            "synthetic_output_tokens": synthetic_output_tokens.get(role, 0),
            "configured_input_token_cap_per_call": role_caps[role][0],
            "configured_output_token_cap_per_call": role_caps[role][1],
            "worst_case_output_token_reserve": (
                metrics["by_role"].get(role, {}).get("calls", 0) * role_caps[role][1]
            ),
        }
        for role in ("b0", "b1", "auditor")
    }
    ticket_component_count = sum(row["auditor_decision_count"] for row in chapter_reports)
    prompt_manifest: dict[str, dict[str, Any]] = {}
    for request in all_requests:
        role = str(request["role"])
        if role in prompt_manifest:
            continue
        system_prompt = str(request["messages"][0]["content"])
        prompt_manifest[role] = {
            "prompt_id": request["prompt_id"],
            "prompt_sha256": request["prompt_sha256"],
            "prompt_bytes": len(system_prompt.encode("utf-8")),
        }
    config_payload = run_config.to_dict()
    config_path = output_dir / f"run_config_{run_config.config_hash[:12]}.json"
    _write_json(config_path, {"config_hash": run_config.config_hash, "payload": config_payload})
    frozen_after = file_sha256(frozen_db).upper()
    if frozen_after != frozen_before:
        raise PhaseCError("frozen D2L database changed during dry render")
    report = {
        "schema_version": "literary_m4f_b0b1_v3_phase_c_dry_v1",
        "status": "approval_required",
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "mode": "synthetic_transport_sizing_only",
        "semantic_quality_claim": False,
        "api_calls": 0,
        "chapter_ids": list(chapter_ids),
        "state_lineage_id": state_lineage_id,
        "document": {"path": str(document_path), "sha256": file_sha256(document_path)},
        "design_doc": {
            "path": str(design_doc),
            "workspace_file_sha256": file_sha256(design_doc),
            "normalized_text_sha256": _normalized_text_sha256(design_doc),
        },
        "prompt_manifest": prompt_manifest,
        "runtime_sources": [
            {
                "path": source.relative_to(REPO_ROOT).as_posix(),
                "sha256": file_sha256(source),
            }
            for source in RUNTIME_SOURCE_FILES
        ],
        "run_config": {
            "path": str(config_path),
            "config_hash": run_config.config_hash,
        },
        "chapter_reports": chapter_reports,
        "request_metrics": metrics,
        "budget_envelope": budget_envelope,
        "candidate_packet_cap_stress": _candidate_packet_stress(run_config),
        "synthetic_fixture_metrics": {
            "model_ticket_count": synthetic_ticket_count,
            "ticket_share": synthetic_ticket_count / max(1, b1_calls),
            "ticket_component_count": ticket_component_count,
            "component_share": ticket_component_count / max(1, b1_calls),
            "estimated_auditor_calls": budget_envelope["auditor"]["calls"],
            "estimated_auditor_input_tokens": budget_envelope["auditor"][
                "estimated_input_tokens"
            ],
            "estimated_auditor_output_token_reserve": budget_envelope["auditor"][
                "worst_case_output_token_reserve"
            ],
            "clean_new_entity_count": 0,
            "clean_new_glossary_count": 0,
            "code_ticket_count": 0,
            "targeted_recall_call_count": sum(
                row["targeted_recall_call_count"] for row in chapter_reports
            ),
            "worst_window_tokens": metrics["worst_b1_window_tokens"],
            "warning": "Synthetic rows exercise transport only; they do not calibrate semantic quality.",
        },
        "frozen_db_sha256_before": frozen_before,
        "frozen_db_sha256_after": frozen_after,
        "approval_boundary": {
            "required_before_api": True,
            "approved_config_hash": None,
            "phase_d_runner_enabled": True,
        },
    }
    report["report_hash"] = canonical_hash(report)
    _write_json(output_dir / "dry_render_report.json", report)
    return report


def _existing_usage_records(run_root: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for raw_path in sorted((Path(run_root) / "calls").glob("*/attempt_01/raw_result.json")):
        request_path = raw_path.parents[1] / "request.json"
        if not request_path.is_file():
            continue
        raw = json.loads(raw_path.read_text(encoding="utf-8"))
        request = json.loads(request_path.read_text(encoding="utf-8"))["registry_request"]
        usage = raw.get("usage") or {}
        records.append(
            {
                "call_dir": raw_path.parents[1],
                "role": str(request.get("role") or ""),
                "chapter_id": str(request.get("chapter_id") or ""),
                "window_id": request.get("window_id"),
                "request_fingerprint": str(request.get("request_fingerprint") or ""),
                "quota_bucket_id": str(raw.get("quota_bucket_id") or ""),
                "model": str(raw.get("model") or ""),
                "from_cache": bool(raw.get("from_cache")),
                "prompt_tokens": int(usage.get("prompt_tokens") or 0),
                "cached_tokens": int(usage.get("cached_tokens") or 0),
                "completion_tokens": int(usage.get("completion_tokens") or 0),
                "reasoning_tokens": int(usage.get("reasoning_tokens") or 0),
                "cost_usd": float(raw.get("cost_usd") or 0),
                "latency_ms": int(raw.get("latency_ms") or 0),
            }
        )
    return records


def _quota_headroom(config: RunConfigV3, preflight: Mapping[str, Any]) -> list[dict[str, Any]]:
    prior = preflight.get("usage_by_bucket_model") or {}
    rows: list[dict[str, Any]] = []
    for gate_id, gate in sorted(config.quota_gates.items()):
        key = _usage_key(str(gate["quota_bucket_id"]), str(gate["model_id"]))
        used = int(prior.get(key) or 0)
        cap = int(gate["internal_utc_day_token_cap"])
        rows.append(
            {
                "quota_gate_id": gate_id,
                "quota_bucket_id": gate["quota_bucket_id"],
                "model_id": gate["model_id"],
                "used_before_run": used,
                "internal_utc_day_token_cap": cap,
                "headroom_before_run": cap - used,
            }
        )
    return rows


def _real_manifest_contract(
    *,
    document_path: Path,
    design_doc: Path,
    config: RunConfigV3,
    state_lineage_id: str,
    executor: PersistedRealExecutorV3,
) -> dict[str, Any]:
    public_executor = dict(executor.public_manifest)
    return {
        "schema_version": REAL_RUN_SCHEMA_VERSION,
        "mode": "real_api_two_chapter_pilot",
        "git_commit": _git_head(),
        "source": {
            "path": document_path.resolve().relative_to(REPO_ROOT.resolve()).as_posix(),
            "sha256": file_sha256(document_path),
            "chapter_ids": list(CHAPTER_IDS),
        },
        "design_doc": {
            "path": design_doc.resolve().relative_to(REPO_ROOT.resolve()).as_posix(),
            "sha256": file_sha256(design_doc),
        },
        "run_config_hash": config.config_hash,
        "run_config_payload": config.to_dict(),
        "state_lineage_id": state_lineage_id,
        "credential_commitments": public_executor.get("credential_commitments"),
        "executor_contract": {
            "executor": public_executor.get("executor"),
            "quota_accounting_scope": public_executor.get("quota_accounting_scope"),
            "operational_quota_policy": public_executor.get("operational_quota_policy"),
        },
        "approval": (
            "User explicitly approved gpt-5.4 for B0/Auditor and gpt-5.4-mini for B1."
        ),
        "stop_policy": [
            "invalid JSON or typed schema",
            "stale working revision or CAS parent",
            "targeted recall, ticket share, component share, or Auditor-call cap",
            "candidate overflow forbids authoritative mutation",
            "internal UTC-day token or local rate gate",
            "provider error including 429; no semantic repair",
            "frozen D2L database hash drift",
        ],
    }


def _completed_chapters(run_root: Path) -> list[str]:
    return [
        chapter_id
        for chapter_id in CHAPTER_IDS
        if (Path(run_root) / "chapters" / chapter_id / "chapter_report.json").is_file()
    ]


def run_phase_d_real(
    *,
    document_path: Path,
    design_doc: Path,
    output_dir: Path,
    frozen_db: Path,
    through_chapter: str,
    config: RunConfigV3,
    approved_config_hash: str,
    executor: PersistedRealExecutorV3,
    preflight: Mapping[str, Any],
    resume: bool,
) -> dict[str, Any]:
    if approved_config_hash != config.config_hash:
        raise PhaseCError("approved config hash does not match the real RunConfigV3")
    if through_chapter not in CHAPTER_IDS:
        raise PhaseCError(f"unsupported through_chapter: {through_chapter}")
    output = Path(output_dir).resolve()
    _verify_frozen_db(frozen_db)
    _, chapters = _load_document(document_path, CHAPTER_IDS)
    state_lineage_id = canonical_hash(
        {
            "document_sha256": file_sha256(document_path),
            "ordered_chapter_ids": list(CHAPTER_IDS),
            "registry_schema": REGISTRY_SCHEMA_VERSION,
        }
    )
    contract = _real_manifest_contract(
        document_path=document_path,
        design_doc=design_doc,
        config=config,
        state_lineage_id=state_lineage_id,
        executor=executor,
    )
    manifest_path = output / "run_manifest.json"
    if resume:
        if not manifest_path.is_file():
            raise PhaseCError("resume requires an existing v3 run manifest")
        stored = json.loads(manifest_path.read_text(encoding="utf-8"))
        if stored.get("manifest_contract_hash") != canonical_hash(contract):
            raise PhaseCError("resume source/config/executor contract differs from initial run")
    else:
        _ensure_empty_output(output)
        config_path = output / f"run_config_{config.config_hash[:12]}.json"
        _write_new_json(
            config_path, {"config_hash": config.config_hash, "payload": config.to_dict()}
        )
        _write_new_json(
            manifest_path,
            {
                **contract,
                "run_config_path": config_path.relative_to(output).as_posix(),
                "manifest_contract_hash": canonical_hash(contract),
                "created_at": _utc_now(),
            },
        )
    preflight_body = {
        **dict(preflight),
        "quota_headroom": _quota_headroom(config, preflight),
        "config_hash": config.config_hash,
        "frozen_db_sha256": _verify_frozen_db(frozen_db),
        "recorded_at": _utc_now(),
    }
    preflight_name = "preflight_quota_" + re.sub(r"[^0-9A-Za-z]+", "", _utc_now()) + ".json"
    _write_new_json(output / preflight_name, preflight_body)

    store = ChapterRegistryStoreV3(output / "registry_store")
    completed = _completed_chapters(output)
    if completed != list(CHAPTER_IDS[: len(completed)]):
        raise PhaseCError("completed v3 chapters are not an exact prefix")
    target_index = CHAPTER_IDS.index(through_chapter)
    for chapter in chapters[: target_index + 1]:
        chapter_id = str(chapter["chapter_id"])
        if chapter_id in completed:
            continue
        chapter_root = output / "chapters" / chapter_id
        if chapter_root.exists():
            raise PhaseCError(f"partial chapter artifact requires a fresh run: {chapter_id}")
        parent_generation = store.current_generation_id(state_lineage_id)
        parent_snapshot = store.snapshot(state_lineage_id, parent_generation)
        try:
            result = run_synthetic_registry_chapter_v3(
                chapter=chapter,
                state_lineage_id=state_lineage_id,
                parent_snapshot=parent_snapshot,
                executor=executor,  # type: ignore[arg-type]
                design_doc=design_doc,
                run_config=config,
                store=store,
            )
        except Exception as exc:
            executor.record_chapter_validation(
                chapter_id=chapter_id,
                status="failed",
                payload={"error_type": type(exc).__name__, "message": _redacted_error(exc)},
            )
            _write_new_json(
                chapter_root / "chapter_failure.json",
                {
                    "chapter_id": chapter_id,
                    "status": "halted_fail_closed",
                    "error_type": type(exc).__name__,
                    "message": _redacted_error(exc),
                    "parent_generation_id": parent_generation,
                    "current_generation_id": store.current_generation_id(state_lineage_id),
                    "frozen_db_sha256": _verify_frozen_db(frozen_db),
                    "recorded_at": _utc_now(),
                },
            )
            raise
        executor.record_chapter_validation(
            chapter_id=chapter_id,
            status="passed",
            payload={
                "generation_id": result["generation"]["generation_id"],
                "snapshot_hash": result["snapshot"]["snapshot_hash"],
            },
        )
        _write_new_json(chapter_root / "orientation.json", result["orientation"])
        _write_new_json(chapter_root / "targeted_recall_plan.json", result["targeted_recall"])
        _write_new_json(chapter_root / "audit_decisions.json", result["audits"])
        _write_new_json(chapter_root / "prepared_generation.json", result["generation"])
        _write_new_json(chapter_root / "committed_snapshot.json", result["snapshot"])
        _write_new_json(chapter_root / "b2_candidate_preview.json", result["b2_candidate_preview"])
        _write_new_json(chapter_root / "request_manifest.json", result["requests"])
        chapter_records = [
            row for row in executor.records if row["chapter_id"] == chapter_id
        ]
        snapshot = result["snapshot"]
        report_body = {
            "schema_version": REAL_RUN_SCHEMA_VERSION,
            "chapter_id": chapter_id,
            "status": "completed_requires_semantic_review",
            "parent_generation_id": parent_generation,
            "generation_id": result["generation"]["generation_id"],
            "ordinary_b1_calls": len(result["windows"]),
            "targeted_recall_calls": len(result["targeted_recall"]),
            "auditor_calls": len(result["audits"]),
            "exception_budget": result["exception_budget"],
            "snapshot_counts": {
                key: len(snapshot.get(key) or [])
                for key in ("entities", "aliases", "glossary_items", "tickets")
            },
            "pending_ticket_count": sum(
                row.get("status") in {"open", "pending"} for row in snapshot.get("tickets") or []
            ),
            "b2_candidate_preview": {
                "selected_count": result["b2_candidate_preview"].get("selected_count"),
                "overflow": result["b2_candidate_preview"].get("overflow"),
            },
            "usage": _aggregate_usage(chapter_records),
            "frozen_db_sha256": _verify_frozen_db(frozen_db),
        }
        chapter_report = {
            **report_body,
            "chapter_report_hash": canonical_hash(report_body),
            "completed_at": _utc_now(),
        }
        _write_new_json(chapter_root / "chapter_report.json", chapter_report)
        completed.append(chapter_id)

    all_records = _existing_usage_records(output)
    reports = [
        json.loads(
            (output / "chapters" / chapter_id / "chapter_report.json").read_text(
                encoding="utf-8"
            )
        )
        for chapter_id in completed
    ]
    summary_body = {
        "schema_version": REAL_RUN_SCHEMA_VERSION,
        "status": "completed" if completed == list(CHAPTER_IDS) else "canary_completed",
        "completed_chapters": completed,
        "state_lineage_id": state_lineage_id,
        "current_generation_id": store.current_generation_id(state_lineage_id),
        "chapter_report_hashes": [row["chapter_report_hash"] for row in reports],
        "usage": _aggregate_usage(all_records),
        "frozen_db_sha256": _verify_frozen_db(frozen_db),
        "semantic_quality_claim": False,
        "next_gate": (
            "independent chapter-1 artifact review"
            if completed == [CHAPTER_IDS[0]]
            else "independent two-chapter artifact review"
        ),
    }
    summary = {
        **summary_body,
        "run_report_hash": canonical_hash(summary_body),
        "recorded_at": _utc_now(),
    }
    _write_new_json(output / f"run_report_{through_chapter}.json", summary)
    return summary


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Chapter Registry v3 dry-render and real pilot")
    parser.add_argument("--document", type=Path, default=DEFAULT_DOCUMENT)
    parser.add_argument("--design-doc", type=Path, default=DEFAULT_DESIGN_DOC)
    parser.add_argument("--frozen-db", type=Path, default=DEFAULT_FROZEN_DB)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--execute-real", action="store_true")
    parser.add_argument("--approved-config-hash")
    parser.add_argument("--through-chapter", choices=CHAPTER_IDS, default=CHAPTER_IDS[0])
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--key-1", type=Path, default=DEFAULT_KEYS["openai-row1"])
    parser.add_argument("--key-2", type=Path, default=DEFAULT_KEYS["openai-row2"])
    parser.add_argument("--min-interval-seconds", type=float, default=2.0)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.execute_real:
        config = draft_run_config_v3()
        if not args.approved_config_hash:
            raise PhaseCError("Phase D requires --approved-config-hash")
        if args.approved_config_hash != config.config_hash:
            raise PhaseCError(
                f"approved config hash mismatch; current hash is {config.config_hash}"
            )
        output = Path(args.output_dir).resolve()
        preflight = scan_prior_openai_usage(
            report_root=RUNTIME_ROOT / "data" / "reports", exclude_root=output
        )
        usage = dict(preflight["usage_by_bucket_model"])
        calls = dict(preflight["calls_by_bucket_model"])
        if args.resume:
            for row in _existing_usage_records(output):
                if row.get("from_cache"):
                    continue
                key = _usage_key(str(row["quota_bucket_id"]), str(row["model"]))
                usage[key] = usage.get(key, 0) + int(row["prompt_tokens"]) + int(
                    row["completion_tokens"]
                )
                calls[key] = calls.get(key, 0) + 1
            preflight = {
                **preflight,
                "usage_by_bucket_model": dict(sorted(usage.items())),
                "calls_by_bucket_model": dict(sorted(calls.items())),
                "includes_existing_current_run_usage": True,
            }
        real_executor = RealOpenAIRegistryExecutorV3(
            run_config=config,
            run_root=output,
            credential_paths={
                "openai-row1": Path(args.key_1),
                "openai-row2": Path(args.key_2),
            },
            prior_usage_by_bucket_model=usage,
            prior_calls_by_bucket_model=calls,
            min_interval_seconds=args.min_interval_seconds,
        )
        executor = PersistedRealExecutorV3(
            executor=real_executor,
            run_root=output,
            run_config=config,
            frozen_db=args.frozen_db,
        )
        report = run_phase_d_real(
            document_path=args.document,
            design_doc=args.design_doc,
            output_dir=output,
            frozen_db=args.frozen_db,
            through_chapter=args.through_chapter,
            config=config,
            approved_config_hash=args.approved_config_hash,
            executor=executor,
            preflight=preflight,
            resume=args.resume,
        )
        print(canonical_json(report))
        return 0
    report = run_phase_c_dry_render(
        document_path=args.document,
        design_doc=args.design_doc,
        output_dir=args.output_dir,
        frozen_db=args.frozen_db,
    )
    print(canonical_json(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
