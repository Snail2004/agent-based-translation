from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
import json
import os
from pathlib import Path
import re
import subprocess
import time
from typing import Any, Mapping, Protocol, Sequence

from pipeline.agents.llm_client import LLMClient, LLMResult, LLMTransportError
from pipeline.agents.llm_config import LLMConfig
from pipeline.literary.b4_handoff_v3 import (
    build_book_source_manifest,
    state_lineage_id_for_manifest,
)
from pipeline.literary.chapter_registry_schema_v2 import (
    RegistryBudgetError,
    RegistryContractError,
    RunConfigV2,
)
from pipeline.literary.chapter_registry_v2 import (
    ChapterRegistryStoreV2,
    ChapterWorkingRegistryV2,
    build_b2_candidate_manifest,
    build_exception_manifest,
    build_registry_generation,
    build_registry_windows,
    chapter_source_manifest_hash,
    estimate_registry_prompt_tokens,
    render_auditor_requests,
    render_b0_request,
    render_b1_request,
    schedule_targeted_recall,
    validate_audit_decisions,
    validate_orientation_response,
)
from pipeline.literary.checkpoint import canonical_hash, canonical_json, file_sha256


RUN_SCHEMA_VERSION = "literary_m4f_b0b1_v2_phase_c_real_v2"
CHAPTER_IDS = ("wh_ch01", "wh_ch02")
RESPONSE_FORMAT_JSON = {"type": "json_object"}
FROZEN_DB_SHA256 = "64D98965F8859869931152B2AA814FB03AFBF15E6A9853532FD0EF28B555C715"

RUNTIME_ROOT = Path(__file__).resolve().parents[2]
REPO_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_DOCUMENT = (
    RUNTIME_ROOT / "data" / "reports" / "literary_l2a0_wh_builder_scaffold" / "document.json"
)
DEFAULT_DESIGN_DOC = REPO_ROOT / "design" / "LITERARY_PROMPT_DESIGN.md"
DEFAULT_CONFIG = (
    RUNTIME_ROOT
    / "data"
    / "reports"
    / "literary_m4f_b1_prejoined_context_dry_20260714"
    / "run_config_f73034a1a00288ca.json"
)
DEFAULT_FROZEN_DB = RUNTIME_ROOT / "data" / "jobs" / "d2l_p1" / "memory.sqlite3"
DEFAULT_KEYS = {
    "openai-row1": REPO_ROOT / "OPENAI-KEY-1.txt",
    "openai-row2": REPO_ROOT / "OPENAI-KEY-2.txt",
}


class PhaseCError(RuntimeError):
    """Raised when the real pilot must halt without publishing a partial chapter."""


@dataclass(frozen=True)
class ExecutedRegistryCall:
    result: LLMResult
    quota_gate_id: str
    quota_bucket_id: str
    credential_commitment: str
    safe_response_headers: Mapping[str, str]
    completed_at: str


class RegistryExecutor(Protocol):
    @property
    def public_manifest(self) -> Mapping[str, Any]: ...

    def execute(self, request: Any) -> ExecutedRegistryCall: ...


def _now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()


def _write_new_bytes(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with path.open("xb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
    except FileExistsError as exc:
        raise PhaseCError(f"append-only artifact already exists: {path}") from exc


def _write_new_json(path: Path, payload: Mapping[str, Any] | Sequence[Any]) -> None:
    _write_new_bytes(path, (canonical_json(payload) + "\n").encode("utf-8"))


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _safe_name(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", value).strip("._") or "unit"


def _git_head() -> str:
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=REPO_ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _verify_frozen_db(path: Path, expected: str = FROZEN_DB_SHA256) -> str:
    actual = file_sha256(path).upper()
    if actual != str(expected).upper():
        raise PhaseCError(f"frozen D2L DB hash drift: {actual}")
    return actual


def _load_config(path: Path) -> tuple[RunConfigV2, dict[str, Any]]:
    payload = _read_json(path)
    if not isinstance(payload, dict) or not isinstance(payload.get("config"), dict):
        raise PhaseCError("RunConfig artifact shape is invalid")
    raw = dict(payload["config"])
    raw["role_quota_gate_ids"] = {
        role: tuple(ids) for role, ids in dict(raw["role_quota_gate_ids"]).items()
    }
    config = RunConfigV2(**raw)
    stored_hash = str(payload.get("config_hash") or "")
    if config.config_hash != stored_hash:
        raise PhaseCError("RunConfig content hash mismatch")
    return config, payload


def _load_document(path: Path) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    document = _read_json(path)
    if not isinstance(document, dict):
        raise PhaseCError("document must be an object")
    chapters = [row for row in document.get("chapters") or [] if isinstance(row, dict)]
    chapter_ids = [str(row.get("chapter_id") or "") for row in chapters]
    if chapter_ids[:2] != list(CHAPTER_IDS):
        raise PhaseCError("Phase C source must start with exact WH chapters 1-2")
    return document, [dict(chapters[chapter_ids.index(chapter_id)]) for chapter_id in CHAPTER_IDS]


def _credential(path: Path) -> tuple[str, str]:
    value = path.read_text(encoding="utf-8").strip()
    if not value.startswith("sk" + "-") or len(value) < 40 or "\n" in value or "\r" in value:
        raise PhaseCError(f"credential file is malformed: {path.name}")
    return value, canonical_hash({"credential": value})


def _safe_headers(headers: Mapping[str, Any]) -> dict[str, str]:
    allowed = {
        "x-request-id",
        "retry-after",
        "x-ratelimit-limit-requests",
        "x-ratelimit-remaining-requests",
        "x-ratelimit-reset-requests",
        "x-ratelimit-limit-tokens",
        "x-ratelimit-remaining-tokens",
        "x-ratelimit-reset-tokens",
    }
    return {
        str(key).casefold(): str(value)
        for key, value in headers.items()
        if str(key).casefold() in allowed
    }


class RealOpenAIRegistryExecutor:
    def __init__(
        self,
        *,
        run_config: RunConfigV2,
        run_root: Path,
        credential_paths: Mapping[str, Path],
        prior_usage_by_bucket: Mapping[str, int] | None = None,
        prior_calls_by_bucket: Mapping[str, int] | None = None,
        min_interval_seconds: float = 12.0,
        local_rpd_cap: int = 100,
    ) -> None:
        if min_interval_seconds < 0 or local_rpd_cap <= 0:
            raise PhaseCError("operational quota policy is invalid")
        self.config = run_config
        self.run_root = Path(run_root)
        self.min_interval_seconds = float(min_interval_seconds)
        self.local_rpd_cap = int(local_rpd_cap)
        self._usage = {str(key): int(value) for key, value in (prior_usage_by_bucket or {}).items()}
        self._calls = {
            str(key): int(value) for key, value in (prior_calls_by_bucket or {}).items()
        }
        self._last_call: dict[str, float] = {}
        self._next_gate: dict[str, int] = {"b0": 0, "b1": 0, "auditor": 0}
        self._clients: dict[tuple[str, str], tuple[LLMClient, dict[str, str]]] = {}
        self._credentials: dict[str, str] = {}
        self._commitments: dict[str, str] = {}
        for bucket_id, path in credential_paths.items():
            key, commitment = _credential(Path(path))
            self._credentials[str(bucket_id)] = key
            self._commitments[str(bucket_id)] = commitment
        configured_buckets = {
            str(gate["quota_bucket_id"]) for gate in self.config.quota_gates.values()
        }
        if configured_buckets != set(self._credentials):
            raise PhaseCError("credential buckets do not exact-cover RunConfig buckets")

    @property
    def public_manifest(self) -> Mapping[str, Any]:
        return {
            "executor": "openai_chat_completions_v1",
            "credential_commitments": dict(sorted(self._commitments.items())),
            "operational_quota_policy": {
                "local_rpm_per_physical_bucket": (
                    60.0 / self.min_interval_seconds if self.min_interval_seconds else None
                ),
                "minimum_interval_seconds": self.min_interval_seconds,
                "local_rpd_cap_per_physical_bucket": self.local_rpd_cap,
                "provider_limits": "runtime headers when available; 429 halts without repair",
                "internal_utc_day_caps": {
                    gate_id: gate["internal_utc_day_token_cap"]
                    for gate_id, gate in sorted(self.config.quota_gates.items())
                },
            },
        }

    def _role_contract(self, role: str) -> dict[str, Any]:
        return {
            "model": getattr(self.config, f"{role}_model_id"),
            "temperature": getattr(self.config, f"{role}_temperature"),
            "seed": getattr(self.config, f"{role}_seed"),
            "reasoning_effort": getattr(self.config, f"{role}_reasoning_effort"),
            "verbosity": getattr(self.config, f"{role}_verbosity"),
            "max_output_tokens": getattr(self.config, f"{role}_output_cap"),
            "prompt_token_cap": (
                self.config.auditor_input_token_cap
                if role == "auditor"
                else getattr(self.config, f"{role}_input_cap")
            ),
        }

    def _client(self, role: str, gate_id: str) -> tuple[LLMClient, dict[str, str]]:
        key = (role, gate_id)
        cached = self._clients.get(key)
        if cached is not None:
            return cached
        gate = self.config.quota_gates[gate_id]
        bucket_id = str(gate["quota_bucket_id"])
        contract = self._role_contract(role)
        pricing = self.config.pricing_usd_per_million[role]
        llm_config = LLMConfig(
            model=str(contract["model"]),
            temperature=float(contract["temperature"]),
            seed=int(contract["seed"]),
            reasoning_effort=str(contract["reasoning_effort"]),
            verbosity=contract["verbosity"],
            max_output_tokens=int(contract["max_output_tokens"]),
            daily_token_cap=int(gate["internal_utc_day_token_cap"]),
            prompt_token_cap=int(contract["prompt_token_cap"]),
            pricing={
                name: float(value) if value is not None else 0.0
                for name, value in pricing.items()
            },
        )
        header_box: dict[str, str] = {}
        try:
            from openai import OpenAI
        except ImportError as exc:  # pragma: no cover - real environment only.
            raise PhaseCError("openai package is required for Phase C") from exc
        openai_client = OpenAI(api_key=self._credentials[bucket_id])

        def transport(**kwargs: Any) -> Any:
            raw = openai_client.chat.completions.with_raw_response.create(**kwargs)
            header_box.clear()
            header_box.update(_safe_headers(raw.headers))
            return raw.parse()

        client = LLMClient(
            llm_config,
            self.run_root / "cache" / bucket_id / f"{role}.sqlite3",
            transport=transport,
            max_retries=0,
        )
        self._clients[key] = (client, header_box)
        return client, header_box

    def _ordered_gate_ids(self, role: str) -> list[str]:
        gate_ids = list(self.config.role_quota_gate_ids[role])
        start = self._next_gate[role] % len(gate_ids)
        return gate_ids[start:] + gate_ids[:start]

    def execute(self, request: Any) -> ExecutedRegistryCall:
        role = str(request.role)
        if role not in {"b0", "b1", "auditor"}:
            raise PhaseCError(f"unsupported registry role: {role}")
        prompt_tokens = estimate_registry_prompt_tokens(request.messages)
        output_cap = int(getattr(self.config, f"{role}_output_cap"))
        selected_gate: str | None = None
        for gate_id in self._ordered_gate_ids(role):
            gate = self.config.quota_gates[gate_id]
            bucket_id = str(gate["quota_bucket_id"])
            projected = self._usage.get(bucket_id, 0) + prompt_tokens + output_cap
            calls = self._calls.get(bucket_id, 0)
            if projected <= int(gate["internal_utc_day_token_cap"]) and calls < self.local_rpd_cap:
                selected_gate = gate_id
                break
        if selected_gate is None:
            raise RegistryBudgetError(f"no quota bucket can reserve {role} request")
        gate = self.config.quota_gates[selected_gate]
        bucket_id = str(gate["quota_bucket_id"])
        prior = self._last_call.get(bucket_id)
        if prior is not None:
            remaining = self.min_interval_seconds - (time.monotonic() - prior)
            if remaining > 0:
                time.sleep(remaining)
        client, headers = self._client(role, selected_gate)
        result = client.call(
            [dict(row) for row in request.messages],
            response_format=RESPONSE_FORMAT_JSON,
            tag=f"registry_v2:{role}:{request.chapter_id}:{request.window_id or 'chapter'}",
        )
        self._last_call[bucket_id] = time.monotonic()
        if not result.from_cache:
            self._usage[bucket_id] = self._usage.get(bucket_id, 0) + result.usage.total_billable_tokens
            self._calls[bucket_id] = self._calls.get(bucket_id, 0) + 1
        gate_ids = list(self.config.role_quota_gate_ids[role])
        self._next_gate[role] = (gate_ids.index(selected_gate) + 1) % len(gate_ids)
        return ExecutedRegistryCall(
            result=result,
            quota_gate_id=selected_gate,
            quota_bucket_id=bucket_id,
            credential_commitment=self._commitments[bucket_id],
            safe_response_headers=dict(headers),
            completed_at=_now(),
        )


def _call_count(run_root: Path) -> int:
    if not (run_root / "calls").is_dir():
        return 0
    indexes = []
    for path in (run_root / "calls").iterdir():
        match = re.match(r"(\d{4})_", path.name)
        if path.is_dir() and match:
            indexes.append(int(match.group(1)))
    return max(indexes, default=0)


def _usage_rows_from_existing_calls(run_root: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for path in sorted((run_root / "calls").glob("*/attempt_01/raw_result.json")):
        raw = _read_json(path)
        request = _read_json(path.parents[1] / "request.json")
        usage = raw.get("usage") or {}
        rows.append(
            {
                "role": str((request.get("registry_request") or {}).get("role") or ""),
                "quota_bucket_id": str(raw.get("quota_bucket_id") or ""),
                "from_cache": bool(raw.get("from_cache")),
                "prompt_tokens": int(usage.get("prompt_tokens") or 0),
                "cached_tokens": int(usage.get("cached_tokens") or 0),
                "completion_tokens": int(usage.get("completion_tokens") or 0),
                "reasoning_tokens": int(usage.get("reasoning_tokens") or 0),
                "cost_usd": float(raw.get("cost_usd") or 0),
                "latency_ms": int(raw.get("latency_ms") or 0),
            }
        )
    return rows


def _prior_quota_state(run_root: Path) -> tuple[dict[str, int], dict[str, int]]:
    usage_by_bucket: dict[str, int] = {}
    calls_by_bucket: dict[str, int] = {}
    for row in _usage_rows_from_existing_calls(run_root):
        if row["from_cache"]:
            continue
        bucket = str(row["quota_bucket_id"])
        tokens = int(row["prompt_tokens"]) + int(row["completion_tokens"])
        usage_by_bucket[bucket] = usage_by_bucket.get(bucket, 0) + tokens
        calls_by_bucket[bucket] = calls_by_bucket.get(bucket, 0) + 1
    return usage_by_bucket, calls_by_bucket


def _persist_call(
    *,
    run_root: Path,
    call_index: int,
    request: Any,
    executor: RegistryExecutor,
    run_config: RunConfigV2,
    frozen_db: Path,
) -> tuple[ExecutedRegistryCall, Path]:
    suffix = request.window_id or "chapter"
    call_dir = run_root / "calls" / (
        f"{call_index:04d}_{request.role}_{_safe_name(request.chapter_id)}_{_safe_name(suffix)}"
    )
    contract = {
        "registry_request": request.to_dict(),
        "run_config_hash": run_config.config_hash,
        "eligible_quota_gate_ids": list(run_config.role_quota_gate_ids[request.role]),
        "response_format": RESPONSE_FORMAT_JSON,
    }
    _write_new_json(call_dir / "request.json", contract)
    _verify_frozen_db(frozen_db)
    try:
        executed = executor.execute(request)
    except Exception as exc:
        _write_new_json(
            call_dir / "attempt_01" / "transport_error.json",
            {
                "error_type": type(exc).__name__,
                "message": str(exc),
                "recorded_at": _now(),
            },
        )
        raise
    _verify_frozen_db(frozen_db)
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
            call_dir / "attempt_01" / "validation.json",
            {
                "status": "failed",
                "error_type": "json_contract",
                "message": result.json_error or "parsed response is not an object",
            },
        )
        raise PhaseCError(f"{request.role} returned invalid JSON at {call_dir.name}")
    return executed, call_dir


def _record_validation(call_dir: Path, *, status: str, payload: Mapping[str, Any]) -> None:
    body = {"status": status, **dict(payload)}
    body["validation_hash"] = canonical_hash(body)
    _write_new_json(call_dir / "attempt_01" / "validation.json", body)


def _call_usage(executed: ExecutedRegistryCall, role: str) -> dict[str, Any]:
    return {
        "role": role,
        "quota_bucket_id": executed.quota_bucket_id,
        "from_cache": executed.result.from_cache,
        "prompt_tokens": executed.result.usage.prompt_tokens,
        "cached_tokens": executed.result.usage.cached_tokens,
        "completion_tokens": executed.result.usage.completion_tokens,
        "reasoning_tokens": executed.result.usage.reasoning_tokens,
        "cost_usd": executed.result.cost_usd,
        "latency_ms": executed.result.latency_ms,
    }


def _aggregate_usage(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    totals = {
        "calls": len(rows),
        "api_calls": sum(not bool(row.get("from_cache")) for row in rows),
        "prompt_tokens": sum(int(row.get("prompt_tokens") or 0) for row in rows),
        "cached_tokens": sum(int(row.get("cached_tokens") or 0) for row in rows),
        "completion_tokens": sum(int(row.get("completion_tokens") or 0) for row in rows),
        "reasoning_tokens": sum(int(row.get("reasoning_tokens") or 0) for row in rows),
        "cost_usd": round(sum(float(row.get("cost_usd") or 0) for row in rows), 12),
    }
    by_role: dict[str, dict[str, int]] = {}
    by_bucket: dict[str, dict[str, int]] = {}
    for row in rows:
        tokens = int(row.get("prompt_tokens") or 0) + int(row.get("completion_tokens") or 0)
        for index, key in ((by_role, str(row["role"])), (by_bucket, str(row["quota_bucket_id"]))):
            index.setdefault(key, {"calls": 0, "tokens": 0})
            index[key]["calls"] += 1
            index[key]["tokens"] += tokens
    return {**totals, "by_role": by_role, "by_bucket": by_bucket}


def _run_chapter(
    *,
    chapter: Mapping[str, Any],
    design_doc: Path,
    config: RunConfigV2,
    executor: RegistryExecutor,
    run_root: Path,
    store: ChapterRegistryStoreV2,
    state_lineage_id: str,
    frozen_db: Path,
) -> dict[str, Any]:
    chapter_id = str(chapter["chapter_id"])
    chapter_root = run_root / "chapters" / chapter_id
    if chapter_root.exists():
        raise PhaseCError(f"partial or duplicate chapter artifact exists: {chapter_id}")
    call_index = _call_count(run_root)
    usage_rows: list[dict[str, Any]] = []
    parent_generation = store.current_generation_id(state_lineage_id)
    parent_snapshot = store.snapshot(state_lineage_id, parent_generation)
    working = ChapterWorkingRegistryV2.create(
        state_lineage_id=state_lineage_id,
        chapter_id=chapter_id,
        source_manifest_hash=chapter_source_manifest_hash(chapter),
        parent_generation_id=parent_generation,
        parent_snapshot=parent_snapshot,
    )
    windows = build_registry_windows(
        chapter,
        target_tokens=config.b1_window_target_tokens,
        max_blocks=config.b1_window_max_blocks,
        preceding_tail_k=config.context_only_tail_k,
    )
    block_order = {
        str(row["block_id"]): int(row.get("order_index") or 0)
        for row in chapter.get("blocks") or []
    }

    b0_request = render_b0_request(chapter=chapter, design_doc=design_doc, run_config=config)
    call_index += 1
    b0_executed, b0_dir = _persist_call(
        run_root=run_root,
        call_index=call_index,
        request=b0_request,
        executor=executor,
        run_config=config,
        frozen_db=frozen_db,
    )
    try:
        orientation = validate_orientation_response(
            b0_executed.result.parsed_json, chapter
        )
    except Exception as exc:
        _record_validation(
            b0_dir,
            status="failed",
            payload={"error_type": type(exc).__name__, "message": str(exc)},
        )
        raise
    _record_validation(
        b0_dir,
        status="passed",
        payload={"validated_payload_hash": canonical_hash(orientation)},
    )
    _write_new_json(chapter_root / "orientation.json", orientation)
    usage_rows.append(_call_usage(b0_executed, "b0"))

    for window_index, window in enumerate(windows, 1):
        request = render_b1_request(
            chapter_id=chapter_id,
            window_id=str(window["window_id"]),
            b0_gist=str(orientation["gist"]),
            active_blocks=window["blocks"],
            context_only_tail=window["context_only_tail"],
            working=working,
            block_order=block_order,
            design_doc=design_doc,
            run_config=config,
        )
        call_index += 1
        executed, call_dir = _persist_call(
            run_root=run_root,
            call_index=call_index,
            request=request,
            executor=executor,
            run_config=config,
            frozen_db=frozen_db,
        )
        try:
            application = working.apply_delta(request, executed.result.parsed_json)
        except Exception as exc:
            _record_validation(
                call_dir,
                status="failed",
                payload={"error_type": type(exc).__name__, "message": str(exc)},
            )
            raise
        _record_validation(call_dir, status="passed", payload=application)
        _write_new_json(
            chapter_root
            / "working_revisions"
            / f"{window_index:02d}_{working.revision_hash}.json",
            {
                "window_id": window["window_id"],
                "working_snapshot": working.snapshot(),
                "application": application,
                "candidate_selection_manifest": request.sections[
                    "candidate_selection_manifest"
                ],
            },
        )
        usage_rows.append(_call_usage(executed, "b1"))

    targeted_plan = schedule_targeted_recall(
        orientation=orientation,
        working_snapshot=working.snapshot(),
        windows=windows,
        call_cap=config.targeted_recall_call_cap,
    )
    _write_new_json(chapter_root / "targeted_recall_plan.json", targeted_plan)
    windows_by_id = {str(row["window_id"]): row for row in windows}
    for target_index, plan in enumerate(targeted_plan, 1):
        window = windows_by_id[str(plan["window_id"])]
        request = render_b1_request(
            chapter_id=chapter_id,
            window_id=f"{window['window_id']}:targeted-{target_index:02d}",
            b0_gist=str(orientation["gist"]),
            active_blocks=window["blocks"],
            context_only_tail=window["context_only_tail"],
            working=working,
            block_order=block_order,
            design_doc=design_doc,
            run_config=config,
            targeted_salient_surfaces=plan["missing_surfaces"],
        )
        call_index += 1
        executed, call_dir = _persist_call(
            run_root=run_root,
            call_index=call_index,
            request=request,
            executor=executor,
            run_config=config,
            frozen_db=frozen_db,
        )
        try:
            application = working.apply_delta(
                request, executed.result.parsed_json, targeted_recall=True
            )
        except Exception as exc:
            _record_validation(
                call_dir,
                status="failed",
                payload={"error_type": type(exc).__name__, "message": str(exc)},
            )
            raise
        _record_validation(call_dir, status="passed", payload=application)
        _write_new_json(
            chapter_root
            / "working_revisions"
            / f"targeted_{target_index:02d}_{working.revision_hash}.json",
            {
                "targeted_plan": plan,
                "working_snapshot": working.snapshot(),
                "application": application,
                "candidate_selection_manifest": request.sections[
                    "candidate_selection_manifest"
                ],
            },
        )
        usage_rows.append(_call_usage(executed, "b1"))

    exception_manifest = build_exception_manifest(working)
    _write_new_json(chapter_root / "exception_manifest.json", exception_manifest)
    audit_requests = render_auditor_requests(
        chapter=chapter,
        b0_gist=str(orientation["gist"]),
        working=working,
        exception_manifest=exception_manifest,
        design_doc=design_doc,
        run_config=config,
    )
    audit_responses: list[Mapping[str, Any]] = []
    audit_fingerprints: list[str] = []
    for audit_index, request in enumerate(audit_requests, 1):
        call_index += 1
        executed, call_dir = _persist_call(
            run_root=run_root,
            call_index=call_index,
            request=request,
            executor=executor,
            run_config=config,
            frozen_db=frozen_db,
        )
        audit_responses.append(executed.result.parsed_json)
        audit_fingerprints.append(request.request_fingerprint)
        usage_rows.append(_call_usage(executed, "auditor"))
        _record_validation(
            call_dir,
            status="parsed_pending_chapter_exact_cover",
            payload={"audit_component_index": audit_index},
        )
    audit_decision = None
    if audit_requests:
        try:
            audit_decision = validate_audit_decisions(
                audit_responses,
                requests=audit_requests,
                exception_manifest=exception_manifest,
                working=working,
            )
        except Exception as exc:
            _write_new_json(
                chapter_root / "audit_validation_failure.json",
                {"error_type": type(exc).__name__, "message": str(exc)},
            )
            raise
        _write_new_json(chapter_root / "audit_decision.json", audit_decision)
        _write_new_json(
            chapter_root / "audit_exact_cover_validation.json",
            {
                "status": "passed",
                "request_fingerprints": audit_fingerprints,
                "exception_manifest_hash": exception_manifest["manifest_hash"],
                "decision_hash": canonical_hash(audit_decision),
            },
        )

    generation = build_registry_generation(
        chapter=chapter,
        working=working,
        b0_request_fingerprint=b0_request.request_fingerprint,
        exception_manifest=exception_manifest,
        audit_request_fingerprints=audit_fingerprints,
        audit_decision=audit_decision,
    )
    _write_new_json(chapter_root / "prepared_generation.json", generation.to_dict())
    before = store.current_generation_id(state_lineage_id)
    store.commit(generation, expected_parent=before)
    after = store.current_generation_id(state_lineage_id)
    if after != generation.generation_id:
        raise PhaseCError("registry pointer did not advance to prepared generation")
    snapshot = store.snapshot(state_lineage_id, after)
    _write_new_json(
        chapter_root / "pointer_transition.json",
        {"before": before, "after": after, "committed_at": _now()},
    )
    _write_new_json(chapter_root / "committed_snapshot.json", snapshot)

    b2_manifests = [
        build_b2_candidate_manifest(
            chapter_id=chapter_id,
            active_blocks=window["blocks"],
            registry_snapshot=snapshot,
            candidate_count_cap=config.candidate_card_count_cap,
        )
        for window in windows
    ]
    _write_new_json(chapter_root / "b2_rescan_manifests.json", b2_manifests)
    b2_overflow = sum(bool(row.get("overflow")) for row in b2_manifests)
    report_body = {
        "schema_version": RUN_SCHEMA_VERSION,
        "chapter_id": chapter_id,
        "status": "completed",
        "parent_generation_id": parent_generation,
        "generation_id": generation.generation_id,
        "b0_request_fingerprint": b0_request.request_fingerprint,
        "ordinary_b1_calls": len(windows),
        "targeted_recall_calls": len(targeted_plan),
        "auditor_calls": len(audit_requests),
        "orientation": {
            "narrator_hypothesis_count": len(orientation["narrator_hypotheses"]),
            "salient_checklist_count": len(orientation["salient_surface_checklist"]),
            "unlocatable_checklist_count": len(orientation["code_audit_rows"]),
        },
        "exception_manifest": {
            "exception_share": exception_manifest.get("exception_share"),
            "component_count": len(exception_manifest.get("components") or []),
            "ticket_count": len(exception_manifest.get("tickets") or []),
            "manifest_hash": exception_manifest.get("manifest_hash"),
        },
        "snapshot_counts": {
            name: len(snapshot.get(name) or [])
            for name in ("entities", "aliases", "glossary_items", "local_bindings", "tickets")
        },
        "b2_rescan": {
            "manifest_count": len(b2_manifests),
            "overflow_count": b2_overflow,
            "candidate_count_max": max(
                (int(row.get("selected_count") or 0) for row in b2_manifests), default=0
            ),
        },
        "usage": _aggregate_usage(usage_rows),
        "frozen_db_sha256": _verify_frozen_db(frozen_db),
    }
    chapter_report = {
        **report_body,
        "chapter_report_hash": canonical_hash(report_body),
        "completed_at": _now(),
    }
    _write_new_json(chapter_root / "chapter_report.json", chapter_report)
    return chapter_report


def _completed_chapters(run_root: Path) -> list[str]:
    return [
        chapter_id
        for chapter_id in CHAPTER_IDS
        if (run_root / "chapters" / chapter_id / "chapter_report.json").is_file()
    ]


def _run_manifest_body(
    *,
    document_path: Path,
    design_doc: Path,
    config_path: Path,
    config: RunConfigV2,
    document: Mapping[str, Any],
    executor: RegistryExecutor,
    frozen_db: Path,
) -> dict[str, Any]:
    book_manifest = build_book_source_manifest(document)
    return {
        "schema_version": RUN_SCHEMA_VERSION,
        "mode": "real_api_two_chapter_pilot",
        "git_commit": _git_head(),
        "source": {
            "path": document_path.resolve().relative_to(REPO_ROOT.resolve()).as_posix(),
            "sha256": file_sha256(document_path),
            "book_source_manifest": book_manifest,
            "chapter_ids": list(CHAPTER_IDS),
        },
        "design_doc": {
            "path": design_doc.resolve().relative_to(REPO_ROOT.resolve()).as_posix(),
            "sha256": file_sha256(design_doc),
        },
        "run_config": {
            "path": config_path.resolve().relative_to(REPO_ROOT.resolve()).as_posix(),
            "sha256": file_sha256(config_path),
            "config_hash": config.config_hash,
            "payload": config.to_dict(),
        },
        "state_lineage_id": state_lineage_id_for_manifest(book_manifest),
        "executor": dict(executor.public_manifest),
        "approval": "user explicitly requested Phase C and waived a second cost approval",
        "frozen_db": {
            "path": frozen_db.resolve().relative_to(REPO_ROOT.resolve()).as_posix(),
            "sha256": _verify_frozen_db(frozen_db),
        },
        "stop_policy": [
            "invalid JSON or schema",
            "stale working revision or CAS parent",
            "B0/B1/Auditor input cap",
            "targeted-recall/Auditor component or exception-share cap",
            "internal token or local rate gate",
            "provider error including 429; no automatic semantic repair",
            "frozen D2L DB hash drift",
        ],
    }


def run_phase_c(
    *,
    document_path: Path,
    design_doc: Path,
    config_path: Path,
    output_dir: Path,
    frozen_db: Path,
    through_chapter: str,
    executor: RegistryExecutor,
    resume: bool,
) -> dict[str, Any]:
    output = Path(output_dir).resolve()
    config, _ = _load_config(config_path)
    document, chapters = _load_document(document_path)
    manifest_body = _run_manifest_body(
        document_path=document_path,
        design_doc=design_doc,
        config_path=config_path,
        config=config,
        document=document,
        executor=executor,
        frozen_db=frozen_db,
    )
    manifest_path = output / "run_manifest.json"
    if resume:
        if not manifest_path.is_file():
            raise PhaseCError("resume requires an existing run manifest")
        stored = _read_json(manifest_path)
        if stored.get("manifest_hash") != canonical_hash(manifest_body):
            raise PhaseCError("resume manifest differs from current source/config/executor")
    else:
        if output.exists() and any(output.iterdir()):
            raise PhaseCError(f"fresh Phase C output is not empty: {output}")
        output.mkdir(parents=True, exist_ok=True)
        _write_new_json(
            manifest_path,
            {**manifest_body, "manifest_hash": canonical_hash(manifest_body), "created_at": _now()},
        )
    state_lineage_id = str(manifest_body["state_lineage_id"])
    store = ChapterRegistryStoreV2(output / "registry_store")
    completed = _completed_chapters(output)
    expected_prefix = list(CHAPTER_IDS[: len(completed)])
    if completed != expected_prefix:
        raise PhaseCError("completed chapters are not an exact prefix")
    target_index = CHAPTER_IDS.index(through_chapter)
    for chapter in chapters[: target_index + 1]:
        chapter_id = str(chapter["chapter_id"])
        if chapter_id in completed:
            continue
        _run_chapter(
            chapter=chapter,
            design_doc=design_doc,
            config=config,
            executor=executor,
            run_root=output,
            store=store,
            state_lineage_id=state_lineage_id,
            frozen_db=frozen_db,
        )
        completed.append(chapter_id)
    chapter_reports = [
        _read_json(output / "chapters" / chapter_id / "chapter_report.json")
        for chapter_id in completed
    ]
    summary_body = {
        "schema_version": RUN_SCHEMA_VERSION,
        "status": "completed" if completed == list(CHAPTER_IDS) else "canary_completed",
        "completed_chapters": completed,
        "state_lineage_id": state_lineage_id,
        "current_generation_id": store.current_generation_id(state_lineage_id),
        "chapter_report_hashes": [row["chapter_report_hash"] for row in chapter_reports],
        "usage": _aggregate_usage(_usage_rows_from_existing_calls(output)),
        "frozen_db_sha256": _verify_frozen_db(frozen_db),
    }
    # Summary snapshots are immutable per milestone instead of overwriting a mutable status file.
    summary = {
        **summary_body,
        "summary_hash": canonical_hash(summary_body),
        "completed_at": _now(),
    }
    milestone = "final_run_report.json" if completed == list(CHAPTER_IDS) else "canary_run_report.json"
    if not (output / milestone).exists():
        _write_new_json(output / milestone, summary)
    return summary


def _record_halt_report(run_root: Path, error: Exception, frozen_db: Path) -> None:
    root = Path(run_root)
    if not root.is_dir():
        return
    target = root / "halt_report.json"
    if target.exists():
        return
    message = re.sub(
        r"(?:s" + r"k-|AIza|AQ\.A)[A-Za-z0-9._-]{16,}",
        "[REDACTED_CREDENTIAL]",
        str(error),
    )
    manifest = _read_json(root / "run_manifest.json") if (root / "run_manifest.json").is_file() else {}
    body = {
        "schema_version": RUN_SCHEMA_VERSION,
        "status": "halted",
        "error_type": type(error).__name__,
        "message": message,
        "completed_chapters": _completed_chapters(root),
        "persisted_call_count": len(_usage_rows_from_existing_calls(root)),
        "usage": _aggregate_usage(_usage_rows_from_existing_calls(root)),
        "run_manifest_hash": manifest.get("manifest_hash"),
        "frozen_db_sha256_observed": (
            file_sha256(frozen_db).upper() if Path(frozen_db).is_file() else None
        ),
        "recorded_at": _now(),
    }
    body["halt_report_hash"] = canonical_hash(body)
    _write_new_json(target, body)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run real M4f B0/B1 registry v2 for WH 1-2")
    parser.add_argument("--document", type=Path, default=DEFAULT_DOCUMENT)
    parser.add_argument("--design-doc", type=Path, default=DEFAULT_DESIGN_DOC)
    parser.add_argument("--run-config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--frozen-db", type=Path, default=DEFAULT_FROZEN_DB)
    parser.add_argument("--through", choices=CHAPTER_IDS, required=True)
    parser.add_argument("--confirm-config-hash", required=True)
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--min-interval-seconds", type=float, default=12.0)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    if not args.execute:
        raise SystemExit("Refusing real API execution without --execute")
    config, _ = _load_config(args.run_config)
    if args.confirm_config_hash != config.config_hash:
        raise SystemExit("--confirm-config-hash does not match the content-addressed RunConfig")
    prior_usage, prior_calls = (
        _prior_quota_state(args.output_dir) if args.resume else ({}, {})
    )
    executor = RealOpenAIRegistryExecutor(
        run_config=config,
        run_root=args.output_dir,
        credential_paths=DEFAULT_KEYS,
        prior_usage_by_bucket=prior_usage,
        prior_calls_by_bucket=prior_calls,
        min_interval_seconds=args.min_interval_seconds,
    )
    try:
        summary = run_phase_c(
            document_path=args.document,
            design_doc=args.design_doc,
            config_path=args.run_config,
            output_dir=args.output_dir,
            frozen_db=args.frozen_db,
            through_chapter=args.through,
            executor=executor,
            resume=args.resume,
        )
    except Exception as exc:
        _record_halt_report(args.output_dir, exc, args.frozen_db)
        raise
    print(canonical_json(summary))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
