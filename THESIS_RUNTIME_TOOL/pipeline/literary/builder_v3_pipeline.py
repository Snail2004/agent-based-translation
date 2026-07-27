from __future__ import annotations

from collections import Counter
from copy import deepcopy
from dataclasses import dataclass
from hashlib import sha256
import json
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Protocol, Sequence
import uuid

from pipeline.agents.llm_config import LLMConfig
from pipeline.literary.builder_pilot import (
    build_literary_windows,
    select_chapters,
)
from pipeline.literary.builder_validators_v3 import (
    ValidationResult,
    validate_digest_v3,
    validate_lexicon_v3,
    validate_narrative_v3,
)
from pipeline.literary.checkpoint import (
    CheckpointError,
    CheckpointLock,
    canonical_hash,
    canonical_json,
    file_sha256,
)
from pipeline.literary.checkpoint_v3 import (
    BUILDER_SCHEMA_V3,
    M1_CHECKPOINT_SCHEMA_VERSION_V3,
    M1_GROUND_STATE_VERSION_V3,
    M2_CHECKPOINT_SCHEMA_VERSION_V3,
    M2_DIGEST_STATE_VERSION_V3,
    REQUEST_CONTRACT_VERSION,
    REAL_EXECUTOR_VERSION,
    SYNTHETIC_EXECUTOR_VERSION,
    builder_v3_root,
    contract_versions,
    current_pointer_path,
    publish_generation,
    read_current_checkpoint,
    read_state_from_checkpoint,
    write_bytes_exclusive,
    write_json_exclusive,
    write_report,
)
from pipeline.literary.source_anchor import nfc_block_string


KNOWLEDGE_MODE = "whole_book_frozen"
EXECUTION_MODE_SYNTHETIC = "synthetic"
EXECUTION_MODE_REAL_API = "real_api"
RESPONSE_FORMAT_JSON = {"type": "json_object"}
DEFAULT_WINDOW_TARGET_TOKENS = 500
DEFAULT_WINDOW_MAX_BLOCKS = 8
DEFAULT_TAIL_K = 2
DEFAULT_SUMMARY_K = 2


def _llm_config_projection(config: LLMConfig) -> dict[str, Any]:
    return {
        "model": config.model,
        "temperature": config.temperature,
        "seed": config.seed,
        "reasoning_effort": config.reasoning_effort,
        "verbosity": config.verbosity,
        "max_output_tokens": config.max_output_tokens,
        "daily_token_cap": config.daily_token_cap,
        "prompt_token_cap": config.prompt_token_cap,
        "pricing": dict(sorted(config.pricing.items())),
    }


@dataclass(frozen=True)
class RealStageSpec:
    stage: str
    provider: str
    prompt_id: str
    prompt_text: str
    prompt_sha256: str
    model_config: dict[str, Any]
    model_config_hash: str
    response_format: dict[str, Any]
    executor_contract_version: str = REAL_EXECUTOR_VERSION

    @classmethod
    def create(
        cls,
        *,
        stage: str,
        prompt_id: str,
        prompt_text: str,
        config: LLMConfig,
        provider: str = "openai",
    ) -> "RealStageSpec":
        if stage not in REQUEST_SHAPE_CONTRACT:
            raise ValueError(f"unsupported real Builder-v3 stage: {stage}")
        marker = f"Prompt version: {prompt_id}."
        if marker not in prompt_text:
            raise ValueError(f"loaded prompt lacks its version marker: {prompt_id}")
        if not provider.strip():
            raise ValueError("real Builder-v3 provider cannot be empty")
        projection = _llm_config_projection(config)
        return cls(
            stage=stage,
            provider=provider.strip(),
            prompt_id=prompt_id,
            prompt_text=prompt_text,
            prompt_sha256=sha256(prompt_text.encode("utf-8")).hexdigest(),
            model_config=projection,
            model_config_hash=canonical_hash(projection),
            response_format=deepcopy(RESPONSE_FORMAT_JSON),
        )

    def contract_payload(self) -> dict[str, Any]:
        return {
            "stage": self.stage,
            "provider": self.provider,
            "prompt_id": self.prompt_id,
            "prompt_sha256": self.prompt_sha256,
            "model_config": deepcopy(self.model_config),
            "model_config_hash": self.model_config_hash,
            "response_format": deepcopy(self.response_format),
            "executor_contract_version": self.executor_contract_version,
        }


def real_execution_contract_hash(specs: Mapping[str, RealStageSpec]) -> str:
    return canonical_hash(
        {stage: specs[stage].contract_payload() for stage in sorted(specs)}
    )


def _execution_contract(
    *,
    execution_mode: str,
    real_stage_specs: Mapping[str, RealStageSpec] | None,
    required_stages: set[str],
) -> tuple[dict[str, RealStageSpec], str | None]:
    specs = dict(real_stage_specs or {})
    if execution_mode == EXECUTION_MODE_SYNTHETIC:
        if specs:
            raise ValueError("synthetic Builder-v3 run cannot carry real stage specs")
        return {}, None
    if execution_mode != EXECUTION_MODE_REAL_API:
        raise ValueError(f"unsupported Builder-v3 execution mode: {execution_mode}")
    if set(specs) != required_stages:
        raise ValueError(
            "real Builder-v3 stage specs mismatch: "
            f"missing={sorted(required_stages-set(specs))}, "
            f"extra={sorted(set(specs)-required_stages)}"
        )
    for stage, spec in specs.items():
        if spec.stage != stage:
            raise ValueError(f"real stage spec key mismatch: {stage} != {spec.stage}")
    return specs, real_execution_contract_hash(specs)


WIRE_SHAPE_CONTRACTS: dict[str, dict[str, Any]] = {
    "SourceAnchorWire": {
        "type": "object",
        "required": ["block_id", "char_start", "char_end"],
        "nullable": [],
        "fields": {"block_id": "str", "char_start": "int", "char_end": "int"},
    },
    "BlockView": {
        "type": "object",
        "required": ["block_id", "order_index", "block_type", "text"],
        "nullable": [],
        "fields": {
            "block_id": "str",
            "order_index": "int",
            "block_type": "str",
            "text": "str",
        },
    },
    "ContextBlockView": {
        "type": "object",
        "extends": "BlockView",
        "required": ["context_only", "direction"],
        "nullable": [],
        "fields": {"context_only": "literal:true", "direction": "previous|next"},
    },
    "WindowMentionView": {
        "type": "object",
        "required": [
            "mention_id",
            "surface",
            "mention_type",
            "referent_kind_claim",
            "block_id",
            "anchor",
        ],
        "nullable": [],
        "fields": {
            "mention_id": "str",
            "surface": "str",
            "mention_type": "str",
            "referent_kind_claim": "str",
            "block_id": "str",
            "anchor": "SourceAnchorWire",
        },
    },
    "OccurrenceRosterRow": {
        "type": "object",
        "required": [
            "id",
            "occurrence_kind",
            "surface",
            "referent_kind_claim",
            "reference_scope",
            "block_id",
            "anchor",
        ],
        "nullable": ["reference_scope"],
        "fields": {
            "id": "str",
            "occurrence_kind": "mention|endpoint",
            "surface": "str",
            "referent_kind_claim": "str",
            "reference_scope": "str|null",
            "block_id": "str",
            "anchor": "SourceAnchorWire",
        },
    },
    "EndpointCompact": {
        "type": "object",
        "required": [
            "endpoint_id",
            "surface",
            "reference_scope",
            "referent_kind_claim",
            "mention_ref",
            "attribution_method",
            "block_id",
            "anchor",
        ],
        "nullable": ["mention_ref"],
        "fields": {
            "endpoint_id": "str",
            "surface": "str",
            "reference_scope": "str",
            "referent_kind_claim": "str",
            "mention_ref": "str|null",
            "attribution_method": "str",
            "block_id": "str",
            "anchor": "SourceAnchorWire",
        },
    },
    "B2EventCompact": {
        "type": "object",
        "required": [
            "event_id",
            "event_type",
            "block_id",
            "evidence_quote",
            "actor",
            "target",
        ],
        "nullable": [],
        "fields": {
            "event_id": "str",
            "event_type": "str",
            "block_id": "str",
            "evidence_quote": "str",
            "actor": "EndpointCompact",
            "target": "EndpointCompact",
        },
    },
    "RollingSummaryView": {
        "type": "object",
        "required": [
            "chapter_id",
            "chapter_rolling_summary",
            "source_m2v3_identity_hash",
            "input_max_order",
        ],
        "nullable": [],
        "fields": {
            "chapter_id": "str",
            "chapter_rolling_summary": "str",
            "source_m2v3_identity_hash": "str",
            "input_max_order": "int",
        },
    },
}


REQUEST_SHAPE_CONTRACT: dict[str, dict[str, Any]] = {
    "b1": {
        "version": REQUEST_CONTRACT_VERSION,
        "sections": {
            "active_window_blocks": {"type": "array", "items": "BlockView", "required": True},
            "context_only_tail": {"type": "array", "items": "ContextBlockView", "required": True},
        },
        "wire_shapes": {
            "BlockView": WIRE_SHAPE_CONTRACTS["BlockView"],
            "ContextBlockView": WIRE_SHAPE_CONTRACTS["ContextBlockView"],
        },
        "ordering": {
            "active_window_blocks": "source_order",
            "context_only_tail": "source_order",
        },
    },
    "b2": {
        "version": REQUEST_CONTRACT_VERSION,
        "sections": {
            "active_window_blocks": {"type": "array", "items": "BlockView", "required": True},
            "context_only_tail": {"type": "array", "items": "ContextBlockView", "required": True},
            "window_mentions": {"type": "array", "items": "WindowMentionView", "required": True},
        },
        "wire_shapes": {
            name: WIRE_SHAPE_CONTRACTS[name]
            for name in ("SourceAnchorWire", "BlockView", "ContextBlockView", "WindowMentionView")
        },
        "ordering": {
            "active_window_blocks": "source_order",
            "context_only_tail": "source_order",
            "window_mentions": "block_order_then_anchor_then_id",
        },
    },
    "b3": {
        "version": REQUEST_CONTRACT_VERSION,
        "sections": {
            "chapter_blocks": {"type": "array", "items": "BlockView", "required": True},
            "occurrence_roster": {"type": "array", "items": "OccurrenceRosterRow", "required": True},
            "prior_rolling_summaries": {"type": "array", "items": "RollingSummaryView", "required": True},
            "b2_events_compact": {"type": "array", "items": "B2EventCompact", "required": True},
        },
        "wire_shapes": {
            name: WIRE_SHAPE_CONTRACTS[name]
            for name in ("SourceAnchorWire", "BlockView", "OccurrenceRosterRow", "EndpointCompact", "B2EventCompact", "RollingSummaryView")
        },
        "ordering": {
            "chapter_blocks": "source_order",
            "occurrence_roster": "source_then_kind_then_id",
            "prior_rolling_summaries": "absolute_chapter_order",
            "b2_events_compact": "position_then_event_id",
        },
    },
}

REQUEST_CONTRACT_HASHES = {
    stage: canonical_hash(contract) for stage, contract in REQUEST_SHAPE_CONTRACT.items()
}


class V3RunHalt(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        stage: str | None = None,
        chapter_id: str | None = None,
        window_id: str | None = None,
        audit_ref: Mapping[str, Any] | None = None,
        audit_artifacts: Sequence[Mapping[str, Any]] = (),
        validation_report: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.stage = stage
        self.chapter_id = chapter_id
        self.window_id = window_id
        self.audit_ref = _json_clone(audit_ref) if audit_ref is not None else None
        self.audit_artifacts = [dict(row) for row in audit_artifacts]
        self.validation_report = (
            _json_clone(validation_report) if validation_report is not None else None
        )


@dataclass(frozen=True)
class V3StageRequest:
    canonical_request_json: str
    request_fingerprint: str

    def body(self) -> dict[str, Any]:
        payload = json.loads(self.canonical_request_json)
        if not isinstance(payload, dict):
            raise ValueError("V3StageRequest body must be an object")
        return payload


@dataclass(frozen=True)
class StageAttemptResult:
    raw_payload: dict[str, Any] | None
    raw_text: str | None
    usage: dict[str, int | float]
    from_cache: bool
    execution_mode: str
    transport_meta: dict[str, Any]
    error: dict[str, str] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "raw_payload": deepcopy(self.raw_payload),
            "raw_text": self.raw_text,
            "usage": deepcopy(self.usage),
            "from_cache": self.from_cache,
            "execution_mode": self.execution_mode,
            "transport_meta": deepcopy(self.transport_meta),
            "error": deepcopy(self.error),
        }


class StageExecutor(Protocol):
    def execute(
        self,
        request: V3StageRequest,
        *,
        attempt_no: int,
        bypass_cache: bool = False,
    ) -> StageAttemptResult: ...


class SyntheticStageExecutor:
    def __init__(self, scripted: Mapping[tuple[str, str, str | None], Mapping[str, Any]]) -> None:
        self._scripted = {key: _json_clone(value) for key, value in scripted.items()}
        self.call_log: list[dict[str, Any]] = []

    def execute(
        self,
        request: V3StageRequest,
        *,
        attempt_no: int,
        bypass_cache: bool = False,
    ) -> StageAttemptResult:
        body = request.body()
        if canonical_hash(body) != request.request_fingerprint:
            raise ValueError("SyntheticStageExecutor received a mismatched request fingerprint")
        key = (
            str(body.get("stage") or ""),
            str(body.get("chapter_id") or ""),
            str(body["window_id"]) if body.get("window_id") is not None else None,
        )
        self.call_log.append(
            {
                "key": list(key),
                "attempt_no": attempt_no,
                "bypass_cache": bypass_cache,
                "request_fingerprint": request.request_fingerprint,
                "canonical_request_json": request.canonical_request_json,
            }
        )
        if key not in self._scripted:
            return StageAttemptResult(
                raw_payload=None,
                raw_text=None,
                usage=_zero_usage(),
                from_cache=False,
                execution_mode=EXECUTION_MODE_SYNTHETIC,
                transport_meta={"executor_version": SYNTHETIC_EXECUTOR_VERSION},
                error={"type": "MissingSyntheticPayload", "message": repr(key)},
            )
        raw = _json_clone(self._scripted[key])
        return StageAttemptResult(
            raw_payload=raw,
            raw_text=canonical_json(raw),
            usage=_zero_usage(),
            from_cache=False,
            execution_mode=EXECUTION_MODE_SYNTHETIC,
            transport_meta={"executor_version": SYNTHETIC_EXECUTOR_VERSION},
            error=None,
        )


class RealStageExecutor:
    """Injectable real transport adapter; construction itself never reads a key."""

    def __init__(self, clients: Mapping[str, Any], *, slice_cache_root: Path) -> None:
        self._clients = dict(clients)
        self._slice_cache_root = Path(slice_cache_root).resolve()
        if "literary_m4f_s5c_slice" not in self._slice_cache_root.parts:
            raise ValueError("real Builder-v3 cache root is outside the S5C slice")
        for stage, client in self._clients.items():
            cache_path = getattr(client, "cache_path", None)
            if cache_path is None:
                raise ValueError(f"real Builder-v3 client lacks cache_path: {stage}")
            resolved = Path(cache_path).resolve()
            if (
                self._slice_cache_root != resolved.parent
                and self._slice_cache_root not in resolved.parents
            ):
                raise ValueError(f"real Builder-v3 cache escapes slice root: {stage}")
        self.call_log: list[dict[str, Any]] = []

    def execute(
        self,
        request: V3StageRequest,
        *,
        attempt_no: int,
        bypass_cache: bool = False,
    ) -> StageAttemptResult:
        body = request.body()
        if canonical_hash(body) != request.request_fingerprint:
            raise ValueError("RealStageExecutor received a mismatched request fingerprint")
        if body.get("execution_mode") != EXECUTION_MODE_REAL_API:
            raise ValueError("RealStageExecutor received a non-real request")
        stage = str(body.get("stage") or "")
        client = self._clients.get(stage)
        if client is None:
            return StageAttemptResult(
                raw_payload=None,
                raw_text=None,
                usage=_zero_usage(),
                from_cache=False,
                execution_mode=EXECUTION_MODE_REAL_API,
                transport_meta={"executor_version": REAL_EXECUTOR_VERSION, "attempts": []},
                error={"type": "MissingRealClient", "message": stage},
            )
        if int(getattr(client, "max_retries", -1)) != 0:
            raise ValueError("real Builder-v3 clients must set max_retries=0")
        client_config = getattr(client, "config", None)
        if not isinstance(client_config, LLMConfig):
            raise ValueError("real Builder-v3 client lacks a pinned LLMConfig")
        expected_config = dict(body.get("transport_config") or {})
        if _llm_config_projection(client_config) != expected_config:
            raise ValueError("real Builder-v3 client config differs from persisted request")
        expected_config_hash = str(body.get("model_config_hash") or "")
        cache_path = Path(getattr(client, "cache_path")).resolve()
        if not expected_config_hash or expected_config_hash[:16] not in cache_path.stem:
            raise ValueError("real Builder-v3 cache is not namespaced by model config")
        provider = str(body.get("provider") or "")
        if not provider:
            raise ValueError("real Builder-v3 request lacks provider metadata")
        messages = body.get("rendered_messages")
        response_format = body.get("response_format")
        if not isinstance(messages, list) or response_format != RESPONSE_FORMAT_JSON:
            raise ValueError("real Builder-v3 request lacks persisted messages/JSON format")

        transport_attempts: list[dict[str, Any]] = []
        result: Any | None = None
        for transport_attempt in range(1, 3):
            try:
                result = client.call(
                    deepcopy(messages),
                    response_format=deepcopy(response_format),
                    tag=f"builder_v3_{stage}",
                    bypass_cache=bool(bypass_cache or transport_attempt > 1),
                )
                transport_attempts.append({"attempt": transport_attempt, "status": "ok"})
                break
            except Exception as exc:  # transport taxonomy is persisted, never hidden
                transport_attempts.append(
                    {
                        "attempt": transport_attempt,
                        "status": "error",
                        "error_type": type(exc).__name__,
                        "message": str(exc),
                    }
                )
                if transport_attempt == 2:
                    return StageAttemptResult(
                        raw_payload=None,
                        raw_text=None,
                        usage=_zero_usage(),
                        from_cache=False,
                        execution_mode=EXECUTION_MODE_REAL_API,
                        transport_meta={
                            "executor_version": REAL_EXECUTOR_VERSION,
                            "attempts": transport_attempts,
                        },
                        error={"type": type(exc).__name__, "message": str(exc)},
                    )

        parsed = getattr(result, "parsed_json", None)
        raw_text = getattr(result, "text", None)
        usage = getattr(result, "usage", None)
        if not isinstance(parsed, Mapping) or usage is None:
            return StageAttemptResult(
                raw_payload=None,
                raw_text=str(raw_text) if raw_text is not None else None,
                usage=_zero_usage(),
                from_cache=bool(getattr(result, "from_cache", False)),
                execution_mode=EXECUTION_MODE_REAL_API,
                transport_meta={
                    "executor_version": REAL_EXECUTOR_VERSION,
                    "provider": provider,
                    "attempts": transport_attempts,
                    "model": str(getattr(result, "model", "")),
                },
                error={
                    "type": "RealJSONParseError",
                    "message": str(getattr(result, "json_error", "missing parsed JSON")),
                },
            )
        usage_payload = {
            "prompt_tokens": int(getattr(usage, "prompt_tokens", 0) or 0),
            "cached_tokens": int(getattr(usage, "cached_tokens", 0) or 0),
            "completion_tokens": int(getattr(usage, "completion_tokens", 0) or 0),
            "reasoning_tokens": int(getattr(usage, "reasoning_tokens", 0) or 0),
            "cost_usd": float(getattr(result, "cost_usd", 0.0) or 0.0),
        }
        transport_meta = {
            "executor_version": REAL_EXECUTOR_VERSION,
            "provider": provider,
            "attempts": transport_attempts,
            "model": str(getattr(result, "model", "")),
            "system_fingerprint": getattr(result, "system_fingerprint", None),
            "cache_key": str(getattr(result, "cache_key", "")),
            "latency_ms": int(getattr(result, "latency_ms", 0) or 0),
        }
        self.call_log.append(
            {
                "stage": stage,
                "request_fingerprint": request.request_fingerprint,
                "rendered_messages": deepcopy(messages),
                "transport_meta": deepcopy(transport_meta),
            }
        )
        return StageAttemptResult(
            raw_payload=_json_clone(dict(parsed)),
            raw_text=str(raw_text) if raw_text is not None else canonical_json(parsed),
            usage=usage_payload,
            from_cache=bool(getattr(result, "from_cache", False)),
            execution_mode=EXECUTION_MODE_REAL_API,
            transport_meta=transport_meta,
            error=None,
        )


@dataclass(frozen=True)
class _WindowRuntime:
    spec: dict[str, Any]
    active_blocks: tuple[dict[str, Any], ...]
    previous_tail: tuple[dict[str, Any], ...]
    next_tail: tuple[dict[str, Any], ...]


def _zero_usage() -> dict[str, int | float]:
    return {
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "cached_tokens": 0,
        "reasoning_tokens": 0,
        "cost_usd": 0.0,
    }


def _json_clone(value: Any) -> Any:
    return json.loads(canonical_json(value))


def _ordered_blocks(chapter: Mapping[str, Any]) -> list[dict[str, Any]]:
    return sorted(
        [dict(block) for block in chapter.get("blocks") or [] if block.get("block_id")],
        key=lambda block: (int(block.get("order_index") or 0), str(block.get("block_id") or "")),
    )


def _non_heading(block: Mapping[str, Any]) -> bool:
    return str(block.get("block_type") or "") in {"paragraph", "dialogue"}


def _block_view(block: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "block_id": str(block.get("block_id") or ""),
        "order_index": int(block.get("order_index") or 0),
        "block_type": str(block.get("block_type") or ""),
        "text": nfc_block_string(block),
    }


def _v3_chapter_source_hash(chapter: Mapping[str, Any]) -> str:
    """Hash every source field that can change v3 selection or anchoring."""

    return canonical_hash([_block_view(block) for block in _ordered_blocks(chapter)])


def _context_block_view(block: Mapping[str, Any], direction: str) -> dict[str, Any]:
    return {**_block_view(block), "context_only": True, "direction": direction}


def _build_windows(
    chapter: Mapping[str, Any],
    *,
    target_tokens: int,
    max_blocks: int,
    tail_k: int,
) -> list[_WindowRuntime]:
    if tail_k < 0:
        raise ValueError("tail_k must be non-negative")
    chapter_dict = dict(chapter)
    base = build_literary_windows(
        chapter_dict,
        target_tokens=target_tokens,
        max_blocks=max_blocks,
    )
    ordered = _ordered_blocks(chapter)
    index = {str(block["block_id"]): position for position, block in enumerate(ordered)}
    base = sorted(
        base,
        key=lambda window: (
            index[str(window.blocks[0]["block_id"])],
            str(window.window_id),
        ),
    )
    result: list[_WindowRuntime] = []
    for ordinal, window in enumerate(base, start=1):
        active = tuple(dict(block) for block in window.blocks)
        first = index[str(active[0]["block_id"])]
        last = index[str(active[-1]["block_id"])]
        previous = tuple(ordered[max(0, first - tail_k) : first])
        following = tuple(ordered[last + 1 : min(len(ordered), last + 1 + tail_k)])
        window_id = f"w_{chapter['chapter_id']}_{ordinal:02d}"
        result.append(
            _WindowRuntime(
                spec={
                    "window_id": window_id,
                    "active_block_ids": [str(block["block_id"]) for block in active],
                    "previous_tail_block_ids": [str(block["block_id"]) for block in previous],
                    "next_tail_block_ids": [str(block["block_id"]) for block in following],
                    "first_active_order": int(active[0].get("order_index") or 0),
                },
                active_blocks=active,
                previous_tail=previous,
                next_tail=following,
            )
        )
    _validate_window_exact_cover(chapter, result)
    return result


def _validate_window_exact_cover(
    chapter: Mapping[str, Any], windows: Sequence[_WindowRuntime]
) -> None:
    expected = [str(block["block_id"]) for block in _ordered_blocks(chapter) if _non_heading(block)]
    observed: list[str] = []
    for window in windows:
        observed.extend(
            str(block["block_id"]) for block in window.active_blocks if _non_heading(block)
        )
    counts = Counter(observed)
    gaps = [block_id for block_id in expected if counts[block_id] == 0]
    overlaps = [block_id for block_id in expected if counts[block_id] > 1]
    extras = sorted(set(observed) - set(expected))
    if gaps or overlaps or extras:
        raise ValueError(
            f"Builder-v3 active-window exact-cover failed: gaps={gaps}, "
            f"overlaps={overlaps}, extras={extras}"
        )


def _lineage_row(
    *,
    source_channel: str,
    source_item_id: str,
    value: Any,
    order_indices: Iterable[int],
    upstream_checkpoint_identity_hash: str | None = None,
) -> dict[str, Any]:
    return {
        "source_channel": source_channel,
        "source_item_id": source_item_id,
        "source_sha256": canonical_hash(value),
        "order_indices": sorted({int(order) for order in order_indices}),
        "upstream_checkpoint_identity_hash": upstream_checkpoint_identity_hash,
    }


def _normalize_lineage(rows: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    by_key: dict[tuple[str, str, str, str], dict[str, Any]] = {}
    for value in rows:
        row = _json_clone(value)
        key = (
            str(row.get("source_channel") or ""),
            str(row.get("source_item_id") or ""),
            str(row.get("source_sha256") or ""),
            str(row.get("upstream_checkpoint_identity_hash") or ""),
        )
        if key in by_key:
            by_key[key]["order_indices"] = sorted(
                set(by_key[key].get("order_indices") or [])
                | set(row.get("order_indices") or [])
            )
        else:
            by_key[key] = row
    return sorted(
        by_key.values(),
        key=lambda row: (
            str(row["source_channel"]),
            str(row["source_item_id"]),
            str(row["source_sha256"]),
        ),
    )


def _block_lineage(blocks: Iterable[Mapping[str, Any]], channel: str) -> list[dict[str, Any]]:
    return [
        _lineage_row(
            source_channel=channel,
            source_item_id=str(block.get("block_id") or ""),
            value=_block_view(block),
            order_indices=[int(block.get("order_index") or 0)],
        )
        for block in blocks
    ]


def _request_lineage_row(request: V3StageRequest, channel: str) -> dict[str, Any]:
    body = request.body()
    orders = [
        int(order)
        for row in body.get("lineage_manifest") or []
        for order in row.get("order_indices") or []
    ]
    return _lineage_row(
        source_channel=channel,
        source_item_id=request.request_fingerprint,
        value=body,
        order_indices=orders,
    )


def _build_request(
    *,
    stage: str,
    chapter_id: str,
    window_id: str | None,
    allowlisted_sections: Mapping[str, Any],
    lineage_manifest: Iterable[Mapping[str, Any]],
    upstream_checkpoint_identity_hashes: Mapping[str, str] | None = None,
    execution_mode: str = EXECUTION_MODE_SYNTHETIC,
    real_spec: RealStageSpec | None = None,
) -> V3StageRequest:
    if stage not in REQUEST_SHAPE_CONTRACT:
        raise ValueError(f"unsupported Builder-v3 request stage: {stage}")
    if execution_mode not in {EXECUTION_MODE_SYNTHETIC, EXECUTION_MODE_REAL_API}:
        raise ValueError(f"unsupported Builder-v3 execution mode: {execution_mode}")
    if execution_mode == EXECUTION_MODE_SYNTHETIC and real_spec is not None:
        raise ValueError("synthetic Builder-v3 request cannot carry a real stage spec")
    if execution_mode == EXECUTION_MODE_REAL_API:
        if real_spec is None or real_spec.stage != stage:
            raise ValueError(f"real Builder-v3 request lacks the {stage} stage spec")
    expected_sections = set(REQUEST_SHAPE_CONTRACT[stage]["sections"])
    actual_sections = {str(key) for key in allowlisted_sections}
    if actual_sections != expected_sections:
        raise ValueError(
            f"Builder-v3 {stage} sections mismatch: "
            f"missing={sorted(expected_sections-actual_sections)}, "
            f"extra={sorted(actual_sections-expected_sections)}"
        )
    lineage = _normalize_lineage(lineage_manifest)
    input_max_order = max(
        (
            int(order)
            for row in lineage
            for order in row.get("order_indices") or []
        ),
        default=-1,
    )
    body: dict[str, Any] = {
        "stage": stage,
        "chapter_id": chapter_id,
        "window_id": window_id,
        "system_prompt_ref": None,
        "execution_mode": execution_mode,
        "executor_contract_version": SYNTHETIC_EXECUTOR_VERSION,
        "transport_config": {"executor_version": SYNTHETIC_EXECUTOR_VERSION},
        "knowledge_mode": KNOWLEDGE_MODE,
        "input_max_order": input_max_order,
        "contract_versions": contract_versions(),
        "request_contract_hash": REQUEST_CONTRACT_HASHES[stage],
        "upstream_checkpoint_identity_hashes": dict(
            sorted((upstream_checkpoint_identity_hashes or {}).items())
        ),
        "allowlisted_sections": _json_clone(allowlisted_sections),
        "lineage_manifest": lineage,
    }
    if execution_mode == EXECUTION_MODE_REAL_API:
        assert real_spec is not None
        model_input = {
            "stage": stage,
            "chapter_id": chapter_id,
            "window_id": window_id,
            "allowlisted_sections": _json_clone(allowlisted_sections),
        }
        rendered_messages = [
            {"role": "system", "content": real_spec.prompt_text},
            {"role": "user", "content": canonical_json(model_input)},
        ]
        body.update(
            {
                "system_prompt_ref": real_spec.prompt_id,
                "provider": real_spec.provider,
                "system_prompt_sha256": real_spec.prompt_sha256,
                "system_prompt_text": real_spec.prompt_text,
                "executor_contract_version": real_spec.executor_contract_version,
                "transport_config": deepcopy(real_spec.model_config),
                "model_config_hash": real_spec.model_config_hash,
                "response_format": deepcopy(real_spec.response_format),
                "rendered_messages": rendered_messages,
                "selection_universe_hash": canonical_hash(allowlisted_sections),
            }
        )
    encoded = canonical_json(body)
    return V3StageRequest(encoded, canonical_hash(body))


@dataclass
class _AuditSession:
    root: Path
    run_id: str
    call_seq: int = 0

    @classmethod
    def create(cls, out_dir: Path) -> "_AuditSession":
        return cls(builder_v3_root(out_dir), uuid.uuid4().hex)

    def execute(
        self,
        *,
        request: V3StageRequest,
        executor: StageExecutor,
        validator: Callable[[Mapping[str, Any]], ValidationResult[dict[str, Any]]],
        stage: str,
        chapter_id: str,
        window_id: str | None,
        operational_upstream_checkpoint_hashes: Mapping[str, str] | None = None,
    ) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], list[dict[str, Any]]]:
        """Persist request/raw/validation in order and return normalized payload."""

        self.call_seq += 1
        call_seq = self.call_seq
        suffix = window_id or "chapter"
        call_dir = (
            self.root
            / "audit"
            / self.run_id
            / f"{call_seq:04d}_{stage}_{chapter_id}_{suffix}"
        )
        request_path = call_dir / "request.json"
        request_bytes = request.canonical_request_json.encode("utf-8")
        write_bytes_exclusive(request_path, request_bytes)

        attempt_dir = call_dir / "attempt_01"
        result = executor.execute(request, attempt_no=1, bypass_cache=False)
        raw_path = attempt_dir / "raw_result.json"
        write_json_exclusive(raw_path, result.to_dict())

        validation_path = attempt_dir / "validation.json"
        transport_contract_errors: list[str] = []
        request_body = request.body()
        expected_mode = str(request_body.get("execution_mode") or "")
        expected_executor = (
            SYNTHETIC_EXECUTOR_VERSION
            if expected_mode == EXECUTION_MODE_SYNTHETIC
            else REAL_EXECUTOR_VERSION
        )
        if result.execution_mode != expected_mode:
            transport_contract_errors.append("execution_mode")
        if result.transport_meta.get("executor_version") != expected_executor:
            transport_contract_errors.append("executor_version")
        if expected_mode == EXECUTION_MODE_SYNTHETIC:
            if any(float(value or 0) != 0 for value in result.usage.values()):
                transport_contract_errors.append("synthetic_usage")
        elif expected_mode == EXECUTION_MODE_REAL_API:
            expected_provider = str(request_body.get("provider") or "")
            expected_model = str((request_body.get("transport_config") or {}).get("model") or "")
            if (
                not expected_provider
                or str(result.transport_meta.get("provider") or "")
                != expected_provider
            ):
                transport_contract_errors.append("provider")
            if str(result.transport_meta.get("model") or "") != expected_model:
                transport_contract_errors.append("model")
            if not str(result.transport_meta.get("cache_key") or ""):
                transport_contract_errors.append("cache_key")
            attempts = result.transport_meta.get("attempts")
            if not isinstance(attempts, list) or not 1 <= len(attempts) <= 2:
                transport_contract_errors.append("attempts")
            for key in (
                "prompt_tokens",
                "cached_tokens",
                "completion_tokens",
                "reasoning_tokens",
                "cost_usd",
            ):
                value = result.usage.get(key)
                if not isinstance(value, (int, float)) or float(value) < 0:
                    transport_contract_errors.append(f"usage:{key}")
        else:
            transport_contract_errors.append("request_execution_mode")
        if result.error is not None or result.raw_payload is None or transport_contract_errors:
            error = deepcopy(result.error) or {
                "type": "StageTransportContractError",
                "message": ",".join(transport_contract_errors),
            }
            validation_record = {
                "ok": False,
                "disposition": "transport_error",
                "report": None,
                "normalized_payload_sha256": None,
                "error": error,
            }
            write_json_exclusive(validation_path, validation_record)
            audit_ref, artifacts = _audit_ref(
                root=self.root,
                call_seq=call_seq,
                stage=stage,
                chapter_id=chapter_id,
                window_id=window_id,
                request=request,
                request_path=request_path,
                raw_path=raw_path,
                validation_path=validation_path,
                operational_upstream_checkpoint_hashes=operational_upstream_checkpoint_hashes,
                result=result,
            )
            raise V3RunHalt(
                f"{stage} transport failed: {error}",
                stage=stage,
                chapter_id=chapter_id,
                window_id=window_id,
                audit_ref=audit_ref,
                audit_artifacts=artifacts,
            )

        validated = validator(_json_clone(result.raw_payload))
        report = validated.report.to_dict()
        normalized_payload = _json_clone(validated.payload)
        validation_record = {
            "ok": bool(validated.report.ok),
            "disposition": "accepted" if validated.report.ok else "rejected",
            "report": report,
            "normalized_payload_sha256": canonical_hash(normalized_payload),
            "error": None,
        }
        write_json_exclusive(validation_path, validation_record)
        audit_ref, artifacts = _audit_ref(
            root=self.root,
            call_seq=call_seq,
            stage=stage,
            chapter_id=chapter_id,
            window_id=window_id,
            request=request,
            request_path=request_path,
            raw_path=raw_path,
            validation_path=validation_path,
            operational_upstream_checkpoint_hashes=operational_upstream_checkpoint_hashes,
            result=result,
        )
        if not validated.report.ok:
            raise V3RunHalt(
                f"{stage} validation failed: {validated.report.errors}",
                stage=stage,
                chapter_id=chapter_id,
                window_id=window_id,
                audit_ref=audit_ref,
                audit_artifacts=artifacts,
                validation_report=report,
            )
        return normalized_payload, report, audit_ref, artifacts


def _audit_ref(
    *,
    root: Path,
    call_seq: int,
    stage: str,
    chapter_id: str,
    window_id: str | None,
    request: V3StageRequest,
    request_path: Path,
    raw_path: Path,
    validation_path: Path,
    operational_upstream_checkpoint_hashes: Mapping[str, str] | None,
    result: StageAttemptResult,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    def relative(path: Path) -> str:
        return path.resolve().relative_to(root.resolve()).as_posix()

    request_sha = file_sha256(request_path)
    raw_sha = file_sha256(raw_path)
    validation_sha = file_sha256(validation_path)
    ref = {
        "call_seq": call_seq,
        "stage": stage,
        "chapter_id": chapter_id,
        "window_id": window_id,
        "request_fingerprint": request.request_fingerprint,
        "request_path": relative(request_path),
        "request_sha256": request_sha,
        "attempts": [
            {
                "attempt_no": 1,
                "raw_path": relative(raw_path),
                "raw_sha256": raw_sha,
                "validation_path": relative(validation_path),
                "validation_sha256": validation_sha,
            }
        ],
        "operational_upstream_checkpoint_hashes": dict(
            sorted((operational_upstream_checkpoint_hashes or {}).items())
        ),
    }
    if result.execution_mode == EXECUTION_MODE_REAL_API:
        ref["usage"] = deepcopy(result.usage)
        ref["from_cache"] = bool(result.from_cache)
        ref["transport_meta"] = deepcopy(result.transport_meta)
    logical_prefix = f"{stage}/{chapter_id}/{window_id or 'chapter'}"
    artifacts = [
        {"role": f"{logical_prefix}/request", "path": request_path},
        {"role": f"{logical_prefix}/attempt_01/raw", "path": raw_path},
        {"role": f"{logical_prefix}/attempt_01/validation", "path": validation_path},
    ]
    return ref, artifacts


def _usage_from_request_refs(rows: Iterable[Mapping[str, Any]]) -> dict[str, int | float]:
    result = _zero_usage()
    for row in rows:
        for key in result:
            result[key] = result[key] + (row.get("usage") or {}).get(key, 0)
    return result


def _block_order_map(chapter: Mapping[str, Any]) -> dict[str, int]:
    return {
        str(block["block_id"]): int(block.get("order_index") or 0)
        for block in _ordered_blocks(chapter)
    }


def _window_mentions(
    payload: Mapping[str, Any], chapter: Mapping[str, Any]
) -> list[dict[str, Any]]:
    order = _block_order_map(chapter)
    rows = [
        {
            "mention_id": str(value.get("mention_id") or ""),
            "surface": str(value.get("surface") or ""),
            "mention_type": str(value.get("mention_type") or ""),
            "referent_kind_claim": str(value.get("referent_kind_claim") or ""),
            "block_id": str(value.get("block_id") or ""),
            "anchor": _json_clone(value.get("anchor")),
        }
        for value in payload.get("character_mentions") or []
    ]
    rows.sort(
        key=lambda row: (
            order.get(row["block_id"], 10**9),
            int((row.get("anchor") or {}).get("char_start") or 0),
            row["mention_id"],
        )
    )
    return rows


def _iter_endpoints(payload: Mapping[str, Any]) -> Iterable[dict[str, Any]]:
    for turn in payload.get("speaker_turns") or []:
        if isinstance(turn.get("speaker"), dict):
            yield turn["speaker"]
        if isinstance(turn.get("addressee"), dict):
            yield turn["addressee"]
    for event in payload.get("relation_events") or []:
        if isinstance(event.get("actor"), dict):
            yield event["actor"]
        if isinstance(event.get("target"), dict):
            yield event["target"]


def _endpoint_compact(endpoint: Mapping[str, Any]) -> dict[str, Any]:
    anchor = endpoint.get("anchor") or {}
    return {
        "endpoint_id": str(endpoint.get("endpoint_id") or ""),
        "surface": str(endpoint.get("surface") or ""),
        "reference_scope": str(endpoint.get("reference_scope") or ""),
        "referent_kind_claim": str(endpoint.get("referent_kind_claim") or ""),
        "mention_ref": (
            str(endpoint.get("mention_ref")) if endpoint.get("mention_ref") is not None else None
        ),
        "attribution_method": str(endpoint.get("attribution_method") or ""),
        "block_id": str(anchor.get("block_id") or ""),
        "anchor": _json_clone(anchor),
    }


def _b2_events_compact(
    b2_by_window: Sequence[Mapping[str, Any]], chapter: Mapping[str, Any]
) -> list[dict[str, Any]]:
    order = _block_order_map(chapter)
    rows: list[dict[str, Any]] = []
    for window_row in b2_by_window:
        for event in (window_row.get("payload") or {}).get("relation_events") or []:
            rows.append(
                {
                    "event_id": str(event.get("event_id") or ""),
                    "event_type": str(event.get("event_type") or ""),
                    "block_id": str(event.get("block_id") or ""),
                    "evidence_quote": str(event.get("evidence_quote") or ""),
                    "actor": _endpoint_compact(event.get("actor") or {}),
                    "target": _endpoint_compact(event.get("target") or {}),
                }
            )
    rows.sort(
        key=lambda row: (
            order.get(row["block_id"], 10**9),
            int((row["actor"].get("anchor") or {}).get("char_start") or 0),
            row["event_id"],
        )
    )
    return rows


def _event_endpoint_map(events: Sequence[Mapping[str, Any]]) -> dict[str, dict[str, str]]:
    mapped: dict[str, dict[str, str]] = {}
    for event in events:
        event_id = str(event.get("event_id") or "")
        row = {
            "actor_endpoint_id": str((event.get("actor") or {}).get("endpoint_id") or ""),
            "target_endpoint_id": str((event.get("target") or {}).get("endpoint_id") or ""),
            "block_id": str(event.get("block_id") or ""),
        }
        if not event_id or not all(row.values()):
            raise V3RunHalt(f"B2 event topology is incomplete: {event_id!r}", stage="b3")
        if event_id in mapped:
            raise V3RunHalt(f"duplicate B2 event topology id: {event_id}", stage="b3")
        mapped[event_id] = row
    return mapped


def _reference_record(
    *,
    identifier: str,
    kind: str,
    owner_stage: str,
    chapter_id: str,
    window_id: str | None,
    anchor: Mapping[str, Any] | None = None,
    source_interval: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    if bool(anchor) == bool(source_interval):
        raise V3RunHalt(
            f"reference {identifier!r} must have exactly one source coordinate",
            stage=owner_stage,
            chapter_id=chapter_id,
            window_id=window_id,
        )
    return {
        "id": identifier,
        "kind": kind,
        "owner_stage": owner_stage,
        "chapter_id": chapter_id,
        "window_id": window_id,
        "block_id": str(anchor.get("block_id")) if anchor else None,
        "anchor": _json_clone(anchor) if anchor else None,
        "source_interval": _json_clone(source_interval) if source_interval else None,
    }


def _m1_reference_index(
    *,
    chapter: Mapping[str, Any],
    b1_by_window: Sequence[Mapping[str, Any]],
    b2_by_window: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    chapter_id = str(chapter.get("chapter_id") or "")
    rows: list[dict[str, Any]] = []
    for window_row in b1_by_window:
        window_id = str(window_row.get("window_id") or "")
        for mention in (window_row.get("payload") or {}).get("character_mentions") or []:
            rows.append(
                _reference_record(
                    identifier=str(mention.get("mention_id") or ""),
                    kind="mention",
                    owner_stage="b1",
                    chapter_id=chapter_id,
                    window_id=window_id,
                    anchor=mention.get("anchor") or {},
                )
            )
    for window_row in b2_by_window:
        window_id = str(window_row.get("window_id") or "")
        payload = window_row.get("payload") or {}
        for turn in payload.get("speaker_turns") or []:
            speaker = turn.get("speaker") or {}
            rows.append(
                _reference_record(
                    identifier=str(turn.get("turn_id") or ""),
                    kind="turn",
                    owner_stage="b2",
                    chapter_id=chapter_id,
                    window_id=window_id,
                    anchor=speaker.get("anchor") or {},
                )
            )
            for endpoint in (speaker, turn.get("addressee")):
                if not isinstance(endpoint, dict):
                    continue
                rows.append(
                    _reference_record(
                        identifier=str(endpoint.get("endpoint_id") or ""),
                        kind="endpoint",
                        owner_stage="b2",
                        chapter_id=chapter_id,
                        window_id=window_id,
                        anchor=endpoint.get("anchor") or {},
                    )
                )
            for term in turn.get("address_terms") or []:
                rows.append(
                    _reference_record(
                        identifier=str(term.get("address_occurrence_id") or ""),
                        kind="address_occurrence",
                        owner_stage="b2",
                        chapter_id=chapter_id,
                        window_id=window_id,
                        anchor=term.get("anchor") or {},
                    )
                )
        for event in payload.get("relation_events") or []:
            actor = event.get("actor") or {}
            rows.append(
                _reference_record(
                    identifier=str(event.get("event_id") or ""),
                    kind="event",
                    owner_stage="b2",
                    chapter_id=chapter_id,
                    window_id=window_id,
                    anchor=actor.get("anchor") or {},
                )
            )
            for endpoint in (actor, event.get("target")):
                if not isinstance(endpoint, dict):
                    continue
                rows.append(
                    _reference_record(
                        identifier=str(endpoint.get("endpoint_id") or ""),
                        kind="endpoint",
                        owner_stage="b2",
                        chapter_id=chapter_id,
                        window_id=window_id,
                        anchor=endpoint.get("anchor") or {},
                    )
                )
    return _sort_reference_index(rows, chapter)


def _m2_reference_index(
    *, chapter: Mapping[str, Any], digest_payload: Mapping[str, Any]
) -> list[dict[str, Any]]:
    chapter_id = str(chapter.get("chapter_id") or "")
    rows = [
        _reference_record(
            identifier=str(frame.get("segment_id") or ""),
            kind="frame_segment",
            owner_stage="b3",
            chapter_id=chapter_id,
            window_id=None,
            source_interval=frame.get("source_interval") or {},
        )
        for frame in digest_payload.get("narration_frame_segments") or []
    ]
    return _sort_reference_index(rows, chapter)


def _sort_reference_index(
    rows: Sequence[Mapping[str, Any]], chapter: Mapping[str, Any]
) -> list[dict[str, Any]]:
    order = _block_order_map(chapter)
    seen: set[str] = set()
    normalized: list[dict[str, Any]] = []
    for raw in rows:
        row = _json_clone(raw)
        identifier = str(row.get("id") or "")
        if not identifier or identifier in seen:
            raise V3RunHalt(
                f"duplicate or empty Builder-v3 reference id: {identifier!r}",
                chapter_id=str(chapter.get("chapter_id") or ""),
            )
        seen.add(identifier)
        normalized.append(row)

    def key(row: Mapping[str, Any]) -> tuple[int, int, str, str]:
        anchor = row.get("anchor") or {}
        interval = row.get("source_interval") or {}
        start = interval.get("start") or {}
        block_order = (
            int(start.get("block_order") or 0)
            if interval
            else order.get(str(anchor.get("block_id") or ""), 10**9)
        )
        char_start = (
            int(start.get("char_offset") or 0)
            if interval
            else int(anchor.get("char_start") or 0)
        )
        return block_order, char_start, str(row.get("kind") or ""), str(row.get("id") or "")

    return sorted(normalized, key=key)


def _occurrence_roster(
    *,
    chapter: Mapping[str, Any],
    b1_by_window: Sequence[Mapping[str, Any]],
    b2_by_window: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    order = _block_order_map(chapter)
    rows: list[dict[str, Any]] = []
    for window_row in b1_by_window:
        for mention in (window_row.get("payload") or {}).get("character_mentions") or []:
            rows.append(
                {
                    "id": str(mention.get("mention_id") or ""),
                    "occurrence_kind": "mention",
                    "surface": str(mention.get("surface") or ""),
                    "referent_kind_claim": str(mention.get("referent_kind_claim") or ""),
                    "reference_scope": None,
                    "block_id": str(mention.get("block_id") or ""),
                    "anchor": _json_clone(mention.get("anchor") or {}),
                }
            )
    for window_row in b2_by_window:
        for endpoint in _iter_endpoints(window_row.get("payload") or {}):
            anchor = endpoint.get("anchor") or {}
            rows.append(
                {
                    "id": str(endpoint.get("endpoint_id") or ""),
                    "occurrence_kind": "endpoint",
                    "surface": str(endpoint.get("surface") or ""),
                    "referent_kind_claim": str(endpoint.get("referent_kind_claim") or ""),
                    "reference_scope": str(endpoint.get("reference_scope") or ""),
                    "block_id": str(anchor.get("block_id") or ""),
                    "anchor": _json_clone(anchor),
                }
            )
    rows.sort(
        key=lambda row: (
            order.get(row["block_id"], 10**9),
            int(row["anchor"].get("char_start") or 0),
            row["occurrence_kind"],
            row["id"],
        )
    )
    if len({row["id"] for row in rows}) != len(rows):
        raise V3RunHalt("occurrence roster contains duplicate IDs")
    return rows


def _validate_m1_closure(state: Mapping[str, Any]) -> None:
    references = {str(row.get("id") or ""): row for row in state.get("reference_index") or []}
    if any(
        row.get("kind") == "cast_claim" or row.get("owner_stage") == "b0"
        for row in references.values()
    ):
        raise V3RunHalt("B0 reference survived in the B0-less M1 state", stage="b1")
    for window_row in state.get("b2_by_window") or []:
        window_id = str(window_row.get("window_id") or "")
        for endpoint in _iter_endpoints(window_row.get("payload") or {}):
            mention_ref = endpoint.get("mention_ref")
            if mention_ref is None:
                continue
            record = references.get(str(mention_ref))
            if record is None or record.get("kind") != "mention" or record.get("window_id") != window_id:
                raise V3RunHalt(
                    f"B2 mention_ref is not a same-window B1 occurrence: {mention_ref}",
                    stage="b2",
                    chapter_id=str(state.get("chapter_id") or ""),
                    window_id=window_id,
                )


def _digest_reference_values(payload: Mapping[str, Any]) -> Iterable[tuple[str, str]]:
    for row in payload.get("narration_frame_segments") or []:
        if row.get("narrator_ref") is not None:
            yield "occurrence", str(row.get("narrator_ref") or "")
    for row in payload.get("relation_observations") or []:
        yield "event", str(row.get("event_id") or "")
        for value in row.get("endpoint_refs") or []:
            yield "endpoint", str(value)
        hint = row.get("transition_hint")
        if isinstance(hint, dict):
            yield "event", str(hint.get("trigger_event_id") or "")
    for row in payload.get("character_state_changes") or []:
        yield "occurrence", str(row.get("subject_ref") or "")
        yield "trigger", str(row.get("trigger_ref") or "")
    for row in payload.get("unresolved_threads") or []:
        for value in row.get("subject_refs") or []:
            yield "occurrence", str(value)
    for row in payload.get("translator_relevant_facts") or []:
        if row.get("subject_ref") is not None:
            yield "occurrence", str(row.get("subject_ref") or "")
        for value in row.get("event_ids") or []:
            yield "event", str(value)
    for row in payload.get("motifs") or []:
        for value in row.get("subject_refs") or []:
            yield "occurrence", str(value)


def _validate_m2_closure(
    digest_payload: Mapping[str, Any], m1_state: Mapping[str, Any]
) -> None:
    records = {str(row.get("id") or ""): str(row.get("kind") or "") for row in m1_state.get("reference_index") or []}
    occurrence_kinds = {"mention", "endpoint"}
    for expected_kind, identifier in _digest_reference_values(digest_payload):
        actual = records.get(identifier)
        if expected_kind == "event" and actual != "event":
            raise V3RunHalt(f"B3 event reference is foreign: {identifier}", stage="b3")
        if expected_kind == "endpoint" and actual != "endpoint":
            raise V3RunHalt(f"B3 endpoint reference is foreign: {identifier}", stage="b3")
        if expected_kind == "occurrence" and actual not in occurrence_kinds:
            raise V3RunHalt(f"B3 occurrence reference is foreign: {identifier}", stage="b3")
        if expected_kind == "trigger" and actual not in occurrence_kinds | {"event"}:
            raise V3RunHalt(f"B3 trigger reference is foreign: {identifier}", stage="b3")


def _merge_counts(target: Counter[str], report: Mapping[str, Any]) -> None:
    for key, value in (report.get("counts") or {}).items():
        target[str(key)] += int(value)


def _m1_semantic_projection(state: Mapping[str, Any]) -> dict[str, Any]:
    projection = _json_clone(state)
    projection.pop("request_manifest", None)
    projection.pop("semantic_state_hash", None)
    return projection


def _m2_semantic_projection(state: Mapping[str, Any]) -> dict[str, Any]:
    projection = _json_clone(state)
    projection.pop("request_manifest", None)
    projection.pop("semantic_state_hash", None)
    projection.pop("input_m1v3_checkpoint_hash", None)
    for row in projection.get("prior_summary_provenance") or []:
        row.pop("source_m2v3_checkpoint_hash", None)
    return projection


def _validate_restored_state(state: Mapping[str, Any], *, stage: str) -> None:
    expected_version = (
        M1_GROUND_STATE_VERSION_V3 if stage == "m1v3" else M2_DIGEST_STATE_VERSION_V3
    )
    if state.get("schema_version") != expected_version:
        raise CheckpointError(f"restored {stage} state has wrong schema_version")
    if stage == "m1v3" and "b0_payload" in state:
        raise CheckpointError("restored m1v3 state carries retired b0_payload")
    projection = (
        _m1_semantic_projection(state) if stage == "m1v3" else _m2_semantic_projection(state)
    )
    if canonical_hash(projection) != state.get("semantic_state_hash"):
        raise CheckpointError(f"restored {stage} semantic_state_hash mismatch")


def _document_chapters(document: Mapping[str, Any]) -> list[dict[str, Any]]:
    return [dict(value) for value in document.get("chapters") or []]


def _selected_chapters(
    document: Mapping[str, Any], chapters: Sequence[str]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    whole = _document_chapters(document)
    selected = select_chapters(dict(document), list(chapters))
    if not selected:
        raise ValueError("at least one chapter is required")
    return whole, [dict(value) for value in selected]


def _require_prefix(whole: Sequence[Mapping[str, Any]], selected: Sequence[Mapping[str, Any]]) -> None:
    selected_ids = [str(row.get("chapter_id") or "") for row in selected]
    prefix_ids = [str(row.get("chapter_id") or "") for row in whole[: len(selected_ids)]]
    if selected_ids != prefix_ids:
        raise ValueError("M1V3 requires the full document chapter prefix")


def _checkpoint_common_expected(
    *,
    stage: str,
    chapter: Mapping[str, Any],
    chapter_index: int,
    chapter_sequence_prefix: Sequence[str],
    execution_mode: str,
    parent_identity_hash: str | None,
    window_target_tokens: int,
    window_max_blocks: int,
    tail_k: int,
    summary_k: int,
    input_m1v3_identity_hash: str | None = None,
    execution_contract_hash: str | None = None,
) -> dict[str, Any]:
    expected = {
        "stage": stage,
        "chapter_id": str(chapter.get("chapter_id") or ""),
        "schema_version": (
            M1_CHECKPOINT_SCHEMA_VERSION_V3
            if stage == "m1v3"
            else M2_CHECKPOINT_SCHEMA_VERSION_V3
        ),
        "builder_schema": BUILDER_SCHEMA_V3,
        "absolute_chapter_index": chapter_index,
        "chapter_sequence_prefix": list(chapter_sequence_prefix),
        "source_hash": _v3_chapter_source_hash(chapter),
        "knowledge_mode": KNOWLEDGE_MODE,
        "execution_mode": execution_mode,
        "contract_versions": contract_versions(),
        "request_contract_hashes": dict(REQUEST_CONTRACT_HASHES),
        "window_target_tokens": window_target_tokens,
        "window_max_blocks": window_max_blocks,
        "tail_k": tail_k,
        "summary_k": summary_k,
        "parent_checkpoint_identity_hash": parent_identity_hash,
    }
    if stage == "m2v3":
        expected["input_m1v3_identity_hash"] = input_m1v3_identity_hash
    if execution_mode == EXECUTION_MODE_REAL_API:
        if not execution_contract_hash:
            raise ValueError("real Builder-v3 checkpoint lacks execution contract hash")
        expected["execution_contract_hash"] = execution_contract_hash
    return expected


def _identity_base(
    *,
    stage: str,
    chapter: Mapping[str, Any],
    chapter_index: int,
    chapter_sequence_prefix: Sequence[str],
    execution_mode: str,
    parent_identity_hash: str | None,
    request_manifest_hash: str,
    window_target_tokens: int,
    window_max_blocks: int,
    tail_k: int,
    summary_k: int,
    input_m1v3_identity_hash: str | None = None,
    execution_contract_hash: str | None = None,
) -> dict[str, Any]:
    return {
        **_checkpoint_common_expected(
            stage=stage,
            chapter=chapter,
            chapter_index=chapter_index,
            chapter_sequence_prefix=chapter_sequence_prefix,
            execution_mode=execution_mode,
            parent_identity_hash=parent_identity_hash,
            window_target_tokens=window_target_tokens,
            window_max_blocks=window_max_blocks,
            tail_k=tail_k,
            summary_k=summary_k,
            input_m1v3_identity_hash=input_m1v3_identity_hash,
            execution_contract_hash=execution_contract_hash,
        ),
        "request_manifest_hash": request_manifest_hash,
    }


def _halt_record(exc: BaseException) -> dict[str, Any]:
    if isinstance(exc, V3RunHalt):
        return {
            "type": type(exc).__name__,
            "message": str(exc),
            "stage": exc.stage,
            "chapter_id": exc.chapter_id,
            "window_id": exc.window_id,
            "audit_ref": exc.audit_ref,
        }
    return {"type": type(exc).__name__, "message": str(exc)}


def run_m1_v3(
    document: Mapping[str, Any],
    chapters: Sequence[str],
    *,
    executor: StageExecutor,
    out_dir: Path,
    knowledge_mode: str = KNOWLEDGE_MODE,
    execution_mode: str = EXECUTION_MODE_SYNTHETIC,
    window_target_tokens: int = DEFAULT_WINDOW_TARGET_TOKENS,
    window_max_blocks: int = DEFAULT_WINDOW_MAX_BLOCKS,
    tail_k: int = DEFAULT_TAIL_K,
    resume: bool = False,
    real_stage_specs: Mapping[str, RealStageSpec] | None = None,
) -> dict[str, Any]:
    if knowledge_mode != KNOWLEDGE_MODE:
        raise ValueError("Builder-v3 Step 3 accepts whole_book_frozen only")
    specs, execution_contract_hash = _execution_contract(
        execution_mode=execution_mode,
        real_stage_specs=real_stage_specs,
        required_stages={"b1", "b2"},
    )
    whole, selected = _selected_chapters(document, chapters)
    _require_prefix(whole, selected)
    selected_ids = [str(row.get("chapter_id") or "") for row in selected]
    report: dict[str, Any] = {
        "milestone": "M1V3",
        "status": "running",
        "requested_chapters": list(chapters),
        "selected_chapters": selected_ids,
        "restored_chapters": [],
        "ran_chapters": [],
        "execution_mode": execution_mode,
        "knowledge_mode": knowledge_mode,
        "contract_versions": contract_versions(),
        "validation_counters": {},
        "request_manifest_hashes": {},
        "semantic_state_hashes": {},
        "checkpoint_paths": {},
        "checkpoint_identity_hashes": {},
        "stopping_error": None,
    }
    if execution_mode == EXECUTION_MODE_REAL_API:
        report["execution_contract_hash"] = execution_contract_hash
        report["usage"] = _zero_usage()
    counts: Counter[str] = Counter()
    audit = _AuditSession.create(Path(out_dir))
    parent_identity: str | None = None
    parent_operational: str | None = None
    start_index = 0

    with CheckpointLock(builder_v3_root(Path(out_dir))):
        try:
            if resume:
                for absolute_index, chapter in enumerate(selected):
                    chapter_id = str(chapter.get("chapter_id") or "")
                    prefix = selected_ids[: absolute_index + 1]
                    checkpoint = read_current_checkpoint(
                        out_dir=Path(out_dir),
                        stage="m1v3",
                        chapter_id=chapter_id,
                        expected=_checkpoint_common_expected(
                            stage="m1v3",
                            chapter=chapter,
                            chapter_index=absolute_index,
                            chapter_sequence_prefix=prefix,
                            execution_mode=execution_mode,
                            parent_identity_hash=parent_identity,
                            window_target_tokens=window_target_tokens,
                            window_max_blocks=window_max_blocks,
                            tail_k=tail_k,
                            summary_k=0,
                            execution_contract_hash=execution_contract_hash,
                        ),
                    )
                    if checkpoint is None:
                        break
                    state = read_state_from_checkpoint(checkpoint, out_dir=Path(out_dir))
                    _validate_restored_state(state, stage="m1v3")
                    report["restored_chapters"].append(chapter_id)
                    report["request_manifest_hashes"][chapter_id] = checkpoint[
                        "request_manifest_hash"
                    ]
                    report["semantic_state_hashes"][chapter_id] = checkpoint[
                        "semantic_state_hash"
                    ]
                    report["checkpoint_identity_hashes"][chapter_id] = checkpoint[
                        "checkpoint_identity_hash"
                    ]
                    report["checkpoint_paths"][chapter_id] = str(
                        current_pointer_path(Path(out_dir), "m1v3", chapter_id)
                    )
                    if execution_mode == EXECUTION_MODE_REAL_API:
                        for usage_key, usage_value in (
                            checkpoint.get("accounting") or {}
                        ).items():
                            if usage_key in report["usage"]:
                                report["usage"][usage_key] += usage_value
                    parent_identity = str(checkpoint["checkpoint_identity_hash"])
                    parent_operational = str(checkpoint["checkpoint_hash"])
                    start_index = absolute_index + 1

            for absolute_index in range(start_index, len(selected)):
                chapter = selected[absolute_index]
                chapter_id = str(chapter.get("chapter_id") or "")
                ordered_blocks = _ordered_blocks(chapter)
                windows = _build_windows(
                    chapter,
                    target_tokens=window_target_tokens,
                    max_blocks=window_max_blocks,
                    tail_k=tail_k,
                )
                request_refs: list[dict[str, Any]] = []
                audit_artifacts: list[dict[str, Any]] = []
                request_fingerprints: list[str] = []

                b1_by_window: list[dict[str, Any]] = []
                b2_by_window: list[dict[str, Any]] = []
                for window in windows:
                    window_id = str(window.spec["window_id"])
                    active_blocks = list(window.active_blocks)
                    tail_rows = [
                        *[_context_block_view(block, "previous") for block in window.previous_tail],
                        *[_context_block_view(block, "next") for block in window.next_tail],
                    ]
                    tail_rows.sort(key=lambda row: (row["order_index"], row["block_id"]))
                    b1_request = _build_request(
                        stage="b1",
                        chapter_id=chapter_id,
                        window_id=window_id,
                        allowlisted_sections={
                            "active_window_blocks": [
                                _block_view(block) for block in active_blocks
                            ],
                            "context_only_tail": tail_rows,
                        },
                        lineage_manifest=[
                            *_block_lineage(active_blocks, "active_window_block"),
                            *_block_lineage(
                                [*window.previous_tail, *window.next_tail],
                                "context_only_tail_block",
                            ),
                        ],
                        execution_mode=execution_mode,
                        real_spec=specs.get("b1"),
                    )
                    b1_payload, b1_report, audit_ref, artifacts = audit.execute(
                        request=b1_request,
                        executor=executor,
                        validator=lambda payload, active=active_blocks: validate_lexicon_v3(
                            payload, blocks=active
                        ),
                        stage="b1",
                        chapter_id=chapter_id,
                        window_id=window_id,
                    )
                    request_refs.append(audit_ref)
                    audit_artifacts.extend(artifacts)
                    request_fingerprints.append(b1_request.request_fingerprint)
                    _merge_counts(counts, b1_report)
                    b1_by_window.append({"window_id": window_id, "payload": b1_payload})

                    mentions = _window_mentions(b1_payload, chapter)
                    b2_request = _build_request(
                        stage="b2",
                        chapter_id=chapter_id,
                        window_id=window_id,
                        allowlisted_sections={
                            "active_window_blocks": [
                                _block_view(block) for block in active_blocks
                            ],
                            "context_only_tail": tail_rows,
                            "window_mentions": mentions,
                        },
                        lineage_manifest=[
                            *_block_lineage(active_blocks, "active_window_block"),
                            *_block_lineage(
                                [*window.previous_tail, *window.next_tail],
                                "context_only_tail_block",
                            ),
                            _request_lineage_row(b1_request, "b1_request"),
                        ],
                        execution_mode=execution_mode,
                        real_spec=specs.get("b2"),
                    )
                    b2_payload, b2_report, audit_ref, artifacts = audit.execute(
                        request=b2_request,
                        executor=executor,
                        validator=lambda payload, active=active_blocks, rows=b1_payload[
                            "character_mentions"
                        ]: validate_narrative_v3(payload, blocks=active, mentions=rows),
                        stage="b2",
                        chapter_id=chapter_id,
                        window_id=window_id,
                    )
                    request_refs.append(audit_ref)
                    audit_artifacts.extend(artifacts)
                    request_fingerprints.append(b2_request.request_fingerprint)
                    _merge_counts(counts, b2_report)
                    b2_by_window.append({"window_id": window_id, "payload": b2_payload})

                reference_index = _m1_reference_index(
                    chapter=chapter,
                    b1_by_window=b1_by_window,
                    b2_by_window=b2_by_window,
                )
                state: dict[str, Any] = {
                    "schema_version": M1_GROUND_STATE_VERSION_V3,
                    "chapter_id": chapter_id,
                    "contract_versions": contract_versions(),
                    "windows": [_json_clone(window.spec) for window in windows],
                    "b1_by_window": _json_clone(b1_by_window),
                    "b2_by_window": _json_clone(b2_by_window),
                    "reference_index": reference_index,
                    "request_manifest": _json_clone(request_refs),
                }
                _validate_m1_closure(state)
                semantic_projection = _m1_semantic_projection(state)
                state["semantic_state_hash"] = canonical_hash(semantic_projection)
                semantic_projection = _m1_semantic_projection(state)
                request_manifest_hash = canonical_hash(request_fingerprints)
                identity_base = _identity_base(
                    stage="m1v3",
                    chapter=chapter,
                    chapter_index=absolute_index,
                    chapter_sequence_prefix=selected_ids[: absolute_index + 1],
                    execution_mode=execution_mode,
                    parent_identity_hash=parent_identity,
                    request_manifest_hash=request_manifest_hash,
                    window_target_tokens=window_target_tokens,
                    window_max_blocks=window_max_blocks,
                    tail_k=tail_k,
                    summary_k=0,
                    execution_contract_hash=execution_contract_hash,
                )
                chapter_usage = _usage_from_request_refs(request_refs)
                checkpoint = publish_generation(
                    out_dir=Path(out_dir),
                    stage="m1v3",
                    chapter_id=chapter_id,
                    state=state,
                    semantic_projection=semantic_projection,
                    identity_base=identity_base,
                    operational_fields={
                        "parent_checkpoint_hash": parent_operational,
                        "run_id": audit.run_id,
                        "accounting": chapter_usage,
                    },
                    audit_artifacts=audit_artifacts,
                )
                report["ran_chapters"].append(chapter_id)
                if execution_mode == EXECUTION_MODE_REAL_API:
                    for usage_key, usage_value in chapter_usage.items():
                        report["usage"][usage_key] += usage_value
                report["request_manifest_hashes"][chapter_id] = request_manifest_hash
                report["semantic_state_hashes"][chapter_id] = state["semantic_state_hash"]
                report["checkpoint_identity_hashes"][chapter_id] = checkpoint[
                    "checkpoint_identity_hash"
                ]
                report["checkpoint_paths"][chapter_id] = str(
                    current_pointer_path(Path(out_dir), "m1v3", chapter_id)
                )
                parent_identity = str(checkpoint["checkpoint_identity_hash"])
                parent_operational = str(checkpoint["checkpoint_hash"])
            report["status"] = "complete"
        except (V3RunHalt, CheckpointError, ValueError) as exc:
            if isinstance(exc, V3RunHalt) and exc.validation_report is not None:
                _merge_counts(counts, exc.validation_report)
            report["status"] = "halted"
            report["stopping_error"] = _halt_record(exc)
        report["validation_counters"] = dict(sorted(counts.items()))
        write_report(Path(out_dir), "m1v3_report.json", report)
    return _json_clone(report)


def _load_m1_chain(
    *,
    document: Mapping[str, Any],
    through_index: int,
    m1v3_dir: Path,
    execution_mode: str,
    execution_contract_hash: str | None,
) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
    whole = _document_chapters(document)
    checkpoints: dict[str, dict[str, Any]] = {}
    states: dict[str, dict[str, Any]] = {}
    parent_identity: str | None = None
    for absolute_index, chapter in enumerate(whole[: through_index + 1]):
        chapter_id = str(chapter.get("chapter_id") or "")
        expected = {
            "stage": "m1v3",
            "chapter_id": chapter_id,
            "schema_version": M1_CHECKPOINT_SCHEMA_VERSION_V3,
            "builder_schema": BUILDER_SCHEMA_V3,
            "absolute_chapter_index": absolute_index,
            "chapter_sequence_prefix": [
                str(row.get("chapter_id") or "")
                for row in whole[: absolute_index + 1]
            ],
            "source_hash": _v3_chapter_source_hash(chapter),
            "knowledge_mode": KNOWLEDGE_MODE,
            "execution_mode": execution_mode,
            "contract_versions": contract_versions(),
            "request_contract_hashes": dict(REQUEST_CONTRACT_HASHES),
            "parent_checkpoint_identity_hash": parent_identity,
        }
        if execution_mode == EXECUTION_MODE_REAL_API:
            if not execution_contract_hash:
                raise ValueError("real M1 chain load lacks execution contract hash")
            expected["execution_contract_hash"] = execution_contract_hash
        checkpoint = read_current_checkpoint(
            out_dir=m1v3_dir,
            stage="m1v3",
            chapter_id=chapter_id,
            expected=expected,
        )
        if checkpoint is None:
            raise CheckpointError(f"missing required M1V3 checkpoint: {chapter_id}")
        state = read_state_from_checkpoint(checkpoint, out_dir=m1v3_dir)
        _validate_restored_state(state, stage="m1v3")
        checkpoints[chapter_id] = checkpoint
        states[chapter_id] = state
        parent_identity = str(checkpoint["checkpoint_identity_hash"])
    return checkpoints, states


def _summary_entry(
    *, checkpoint: Mapping[str, Any], state: Mapping[str, Any]
) -> dict[str, Any]:
    digest = state.get("digest_payload") or {}
    summary = str(digest.get("chapter_rolling_summary") or "").strip()
    if not summary:
        raise CheckpointError(f"empty M2V3 rolling summary: {state.get('chapter_id')}")
    input_max_order = checkpoint.get("input_max_order")
    if not isinstance(input_max_order, int):
        raise CheckpointError(f"M2V3 checkpoint lacks input_max_order: {state.get('chapter_id')}")
    view = {
        "chapter_id": str(state.get("chapter_id") or ""),
        "chapter_rolling_summary": summary,
        "source_m2v3_identity_hash": str(checkpoint.get("checkpoint_identity_hash") or ""),
        "input_max_order": input_max_order,
    }
    return {
        "view": view,
        "provenance": {
            **view,
            "source_m2v3_checkpoint_hash": str(checkpoint.get("checkpoint_hash") or ""),
        },
        "checkpoint": dict(checkpoint),
        "state": _json_clone(state),
    }


def _load_m2_prefix_context(
    *,
    document: Mapping[str, Any],
    count: int,
    context_dir: Path,
    m1_checkpoints: Mapping[str, Mapping[str, Any]],
    execution_mode: str,
    summary_k: int,
    execution_contract_hash: str | None,
) -> tuple[dict[str, dict[str, Any]], str | None, str | None]:
    whole = _document_chapters(document)
    entries: dict[str, dict[str, Any]] = {}
    parent_identity: str | None = None
    parent_operational: str | None = None
    for absolute_index, chapter in enumerate(whole[:count]):
        chapter_id = str(chapter.get("chapter_id") or "")
        m1 = m1_checkpoints[chapter_id]
        checkpoint = read_current_checkpoint(
            out_dir=context_dir,
            stage="m2v3",
            chapter_id=chapter_id,
            expected=_checkpoint_common_expected(
                stage="m2v3",
                chapter=chapter,
                chapter_index=absolute_index,
                chapter_sequence_prefix=[
                    str(row.get("chapter_id") or "")
                    for row in whole[: absolute_index + 1]
                ],
                execution_mode=execution_mode,
                parent_identity_hash=parent_identity,
                window_target_tokens=int(m1.get("window_target_tokens") or 0),
                window_max_blocks=int(m1.get("window_max_blocks") or 0),
                tail_k=int(m1.get("tail_k") or 0),
                summary_k=summary_k,
                input_m1v3_identity_hash=str(m1.get("checkpoint_identity_hash") or ""),
                execution_contract_hash=execution_contract_hash,
            ),
        )
        if checkpoint is None:
            raise CheckpointError(f"missing required M2V3 context checkpoint: {chapter_id}")
        state = read_state_from_checkpoint(checkpoint, out_dir=context_dir)
        _validate_restored_state(state, stage="m2v3")
        entries[chapter_id] = _summary_entry(checkpoint=checkpoint, state=state)
        parent_identity = str(checkpoint["checkpoint_identity_hash"])
        parent_operational = str(checkpoint["checkpoint_hash"])
    return entries, parent_identity, parent_operational


def _b3_lineage(
    *,
    chapter: Mapping[str, Any],
    roster: Sequence[Mapping[str, Any]],
    events: Sequence[Mapping[str, Any]],
    summaries: Sequence[Mapping[str, Any]],
    m1_identity_hash: str,
) -> list[dict[str, Any]]:
    order = _block_order_map(chapter)
    rows = _block_lineage(_ordered_blocks(chapter), "chapter_block")
    for row in roster:
        rows.append(
            _lineage_row(
                source_channel="occurrence_roster",
                source_item_id=str(row.get("id") or ""),
                value=row,
                order_indices=[order.get(str(row.get("block_id") or ""), -1)],
                upstream_checkpoint_identity_hash=m1_identity_hash,
            )
        )
    for row in events:
        rows.append(
            _lineage_row(
                source_channel="b2_event_compact",
                source_item_id=str(row.get("event_id") or ""),
                value=row,
                order_indices=[order.get(str(row.get("block_id") or ""), -1)],
                upstream_checkpoint_identity_hash=m1_identity_hash,
            )
        )
    for row in summaries:
        rows.append(
            _lineage_row(
                source_channel="prior_rolling_summary",
                source_item_id=str(row.get("chapter_id") or ""),
                value=row,
                order_indices=[int(row.get("input_max_order") or -1)],
                upstream_checkpoint_identity_hash=str(
                    row.get("source_m2v3_identity_hash") or ""
                ),
            )
        )
    return rows


def run_m2_v3(
    document: Mapping[str, Any],
    chapters: Sequence[str],
    *,
    executor: StageExecutor,
    out_dir: Path,
    m1v3_dir: Path,
    digest_context: Path | None = None,
    knowledge_mode: str = KNOWLEDGE_MODE,
    execution_mode: str = EXECUTION_MODE_SYNTHETIC,
    summary_k: int = DEFAULT_SUMMARY_K,
    resume: bool = False,
    real_stage_specs: Mapping[str, RealStageSpec] | None = None,
    m1_execution_contract_hash: str | None = None,
) -> dict[str, Any]:
    if knowledge_mode != KNOWLEDGE_MODE:
        raise ValueError("Builder-v3 Step 3 accepts whole_book_frozen only")
    specs, execution_contract_hash = _execution_contract(
        execution_mode=execution_mode,
        real_stage_specs=real_stage_specs,
        required_stages={"b3"},
    )
    if execution_mode == EXECUTION_MODE_REAL_API and not m1_execution_contract_hash:
        raise ValueError("real M2V3 requires the M1 execution contract hash")
    if summary_k < 0:
        raise ValueError("summary_k must be non-negative")
    whole, selected = _selected_chapters(document, chapters)
    absolute_by_id = {
        str(chapter.get("chapter_id") or ""): index for index, chapter in enumerate(whole)
    }
    selected_ids = [str(row.get("chapter_id") or "") for row in selected]
    selected_indices = [absolute_by_id[value] for value in selected_ids]
    if selected_indices != list(range(selected_indices[0], selected_indices[-1] + 1)):
        raise ValueError("M2V3 selected chapters must be contiguous")
    highest_index = selected_indices[-1]

    report: dict[str, Any] = {
        "milestone": "M2V3",
        "status": "running",
        "requested_chapters": list(chapters),
        "selected_chapters": selected_ids,
        "restored_chapters": [],
        "ran_chapters": [],
        "execution_mode": execution_mode,
        "knowledge_mode": knowledge_mode,
        "contract_versions": contract_versions(),
        "validation_counters": {},
        "request_manifest_hashes": {},
        "semantic_state_hashes": {},
        "checkpoint_paths": {},
        "checkpoint_identity_hashes": {},
        "stopping_error": None,
    }
    if execution_mode == EXECUTION_MODE_REAL_API:
        report["execution_contract_hash"] = execution_contract_hash
        report["input_m1_execution_contract_hash"] = m1_execution_contract_hash
        report["usage"] = _zero_usage()
    counts: Counter[str] = Counter()
    audit = _AuditSession.create(Path(out_dir))

    with CheckpointLock(builder_v3_root(Path(out_dir))):
        try:
            m1_checkpoints, m1_states = _load_m1_chain(
                document=document,
                through_index=highest_index,
                m1v3_dir=Path(m1v3_dir),
                execution_mode=execution_mode,
                execution_contract_hash=m1_execution_contract_hash,
            )
            context_source = Path(digest_context) if digest_context is not None else Path(out_dir)
            first_index = selected_indices[0]
            summary_entries: dict[str, dict[str, Any]] = {}
            parent_identity: str | None = None
            parent_operational: str | None = None
            if first_index:
                summary_entries, parent_identity, parent_operational = _load_m2_prefix_context(
                    document=document,
                    count=first_index,
                    context_dir=context_source,
                    m1_checkpoints=m1_checkpoints,
                    execution_mode=execution_mode,
                    summary_k=summary_k,
                    execution_contract_hash=execution_contract_hash,
                )

            start_offset = 0
            if resume:
                for offset, chapter in enumerate(selected):
                    absolute_index = selected_indices[offset]
                    chapter_id = str(chapter.get("chapter_id") or "")
                    m1 = m1_checkpoints[chapter_id]
                    checkpoint = read_current_checkpoint(
                        out_dir=Path(out_dir),
                        stage="m2v3",
                        chapter_id=chapter_id,
                        expected=_checkpoint_common_expected(
                            stage="m2v3",
                            chapter=chapter,
                            chapter_index=absolute_index,
                            chapter_sequence_prefix=[
                                str(row.get("chapter_id") or "")
                                for row in whole[: absolute_index + 1]
                            ],
                            execution_mode=execution_mode,
                            parent_identity_hash=parent_identity,
                            window_target_tokens=int(m1.get("window_target_tokens") or 0),
                            window_max_blocks=int(m1.get("window_max_blocks") or 0),
                            tail_k=int(m1.get("tail_k") or 0),
                            summary_k=summary_k,
                            input_m1v3_identity_hash=str(
                                m1.get("checkpoint_identity_hash") or ""
                            ),
                            execution_contract_hash=execution_contract_hash,
                        ),
                    )
                    if checkpoint is None:
                        break
                    state = read_state_from_checkpoint(checkpoint, out_dir=Path(out_dir))
                    _validate_restored_state(state, stage="m2v3")
                    summary_entries[chapter_id] = _summary_entry(
                        checkpoint=checkpoint, state=state
                    )
                    report["restored_chapters"].append(chapter_id)
                    report["request_manifest_hashes"][chapter_id] = checkpoint[
                        "request_manifest_hash"
                    ]
                    report["semantic_state_hashes"][chapter_id] = checkpoint[
                        "semantic_state_hash"
                    ]
                    report["checkpoint_identity_hashes"][chapter_id] = checkpoint[
                        "checkpoint_identity_hash"
                    ]
                    report["checkpoint_paths"][chapter_id] = str(
                        current_pointer_path(Path(out_dir), "m2v3", chapter_id)
                    )
                    if execution_mode == EXECUTION_MODE_REAL_API:
                        for usage_key, usage_value in (
                            checkpoint.get("accounting") or {}
                        ).items():
                            if usage_key in report["usage"]:
                                report["usage"][usage_key] += usage_value
                    parent_identity = str(checkpoint["checkpoint_identity_hash"])
                    parent_operational = str(checkpoint["checkpoint_hash"])
                    start_offset = offset + 1

            for offset in range(start_offset, len(selected)):
                chapter = selected[offset]
                absolute_index = selected_indices[offset]
                chapter_id = str(chapter.get("chapter_id") or "")
                m1_checkpoint = m1_checkpoints[chapter_id]
                m1_state = m1_states[chapter_id]
                required_ids = [
                    str(row.get("chapter_id") or "")
                    for row in whole[max(0, absolute_index - summary_k) : absolute_index]
                ]
                if any(chapter_value not in summary_entries for chapter_value in required_ids):
                    missing = [
                        chapter_value
                        for chapter_value in required_ids
                        if chapter_value not in summary_entries
                    ]
                    raise V3RunHalt(
                        f"missing required K={summary_k} summaries: {missing}",
                        stage="b3",
                        chapter_id=chapter_id,
                    )
                summary_views = [
                    _json_clone(summary_entries[value]["view"]) for value in required_ids
                ]
                if [row["chapter_id"] for row in summary_views] != required_ids:
                    raise V3RunHalt(
                        "prior summaries are out of absolute chapter order",
                        stage="b3",
                        chapter_id=chapter_id,
                    )
                prior_provenance = [
                    _json_clone(summary_entries[value]["provenance"]) for value in required_ids
                ]
                ordered_blocks = _ordered_blocks(chapter)
                roster = _occurrence_roster(
                    chapter=chapter,
                    b1_by_window=m1_state["b1_by_window"],
                    b2_by_window=m1_state["b2_by_window"],
                )
                events = _b2_events_compact(m1_state["b2_by_window"], chapter)
                event_endpoint_map = _event_endpoint_map(events)
                upstream_identity = {
                    "input_m1v3": str(m1_checkpoint["checkpoint_identity_hash"]),
                    **{
                        f"summary:{row['chapter_id']}": str(
                            row["source_m2v3_identity_hash"]
                        )
                        for row in summary_views
                    },
                }
                b3_request = _build_request(
                    stage="b3",
                    chapter_id=chapter_id,
                    window_id=None,
                    allowlisted_sections={
                        "chapter_blocks": [_block_view(block) for block in ordered_blocks],
                        "occurrence_roster": roster,
                        "prior_rolling_summaries": summary_views,
                        "b2_events_compact": events,
                    },
                    lineage_manifest=_b3_lineage(
                        chapter=chapter,
                        roster=roster,
                        events=events,
                        summaries=summary_views,
                        m1_identity_hash=str(m1_checkpoint["checkpoint_identity_hash"]),
                    ),
                    upstream_checkpoint_identity_hashes=upstream_identity,
                    execution_mode=execution_mode,
                    real_spec=specs.get("b3"),
                )
                mention_ids = {
                    str(row["id"])
                    for row in m1_state["reference_index"]
                    if row.get("kind") == "mention"
                }
                endpoint_ids = {
                    str(row["id"])
                    for row in m1_state["reference_index"]
                    if row.get("kind") == "endpoint"
                }
                event_ids = {
                    str(row["id"])
                    for row in m1_state["reference_index"]
                    if row.get("kind") == "event"
                }
                b3_payload, b3_report, audit_ref, audit_artifacts = audit.execute(
                    request=b3_request,
                    executor=executor,
                    validator=lambda payload: validate_digest_v3(
                        payload,
                        blocks=ordered_blocks,
                        mention_ids=mention_ids,
                        endpoint_ids=endpoint_ids,
                        event_ids=event_ids,
                        event_endpoint_map=event_endpoint_map,
                    ),
                    stage="b3",
                    chapter_id=chapter_id,
                    window_id=None,
                    operational_upstream_checkpoint_hashes={
                        "input_m1v3": str(m1_checkpoint["checkpoint_hash"]),
                        **{
                            f"summary:{row['chapter_id']}": str(
                                summary_entries[row["chapter_id"]]["checkpoint"][
                                    "checkpoint_hash"
                                ]
                            )
                            for row in summary_views
                        },
                    },
                )
                _merge_counts(counts, b3_report)
                _validate_m2_closure(b3_payload, m1_state)
                digest_index = _m2_reference_index(
                    chapter=chapter, digest_payload=b3_payload
                )
                state: dict[str, Any] = {
                    "schema_version": M2_DIGEST_STATE_VERSION_V3,
                    "chapter_id": chapter_id,
                    "input_m1v3_checkpoint_hash": str(m1_checkpoint["checkpoint_hash"]),
                    "input_m1v3_identity_hash": str(
                        m1_checkpoint["checkpoint_identity_hash"]
                    ),
                    "digest_payload": _json_clone(b3_payload),
                    "occurrence_roster": roster,
                    "digest_reference_index": digest_index,
                    "prior_summary_provenance": prior_provenance,
                    "request_manifest": [audit_ref],
                }
                semantic_projection = _m2_semantic_projection(state)
                state["semantic_state_hash"] = canonical_hash(semantic_projection)
                semantic_projection = _m2_semantic_projection(state)
                request_manifest_hash = canonical_hash([b3_request.request_fingerprint])
                identity_base = _identity_base(
                    stage="m2v3",
                    chapter=chapter,
                    chapter_index=absolute_index,
                    chapter_sequence_prefix=[
                        str(row.get("chapter_id") or "")
                        for row in whole[: absolute_index + 1]
                    ],
                    execution_mode=execution_mode,
                    parent_identity_hash=parent_identity,
                    request_manifest_hash=request_manifest_hash,
                    window_target_tokens=int(m1_checkpoint.get("window_target_tokens") or 0),
                    window_max_blocks=int(m1_checkpoint.get("window_max_blocks") or 0),
                    tail_k=int(m1_checkpoint.get("tail_k") or 0),
                    summary_k=summary_k,
                    input_m1v3_identity_hash=str(
                        m1_checkpoint["checkpoint_identity_hash"]
                    ),
                    execution_contract_hash=execution_contract_hash,
                )
                chapter_usage = _usage_from_request_refs([audit_ref])
                checkpoint = publish_generation(
                    out_dir=Path(out_dir),
                    stage="m2v3",
                    chapter_id=chapter_id,
                    state=state,
                    semantic_projection=semantic_projection,
                    identity_base=identity_base,
                    operational_fields={
                        "parent_checkpoint_hash": parent_operational,
                        "input_m1v3_checkpoint_hash": str(
                            m1_checkpoint["checkpoint_hash"]
                        ),
                        "run_id": audit.run_id,
                        "input_max_order": int(b3_request.body()["input_max_order"]),
                        "accounting": chapter_usage,
                    },
                    audit_artifacts=audit_artifacts,
                )
                summary_entries[chapter_id] = _summary_entry(
                    checkpoint=checkpoint, state=state
                )
                report["ran_chapters"].append(chapter_id)
                if execution_mode == EXECUTION_MODE_REAL_API:
                    for usage_key, usage_value in chapter_usage.items():
                        report["usage"][usage_key] += usage_value
                report["request_manifest_hashes"][chapter_id] = request_manifest_hash
                report["semantic_state_hashes"][chapter_id] = state["semantic_state_hash"]
                report["checkpoint_identity_hashes"][chapter_id] = checkpoint[
                    "checkpoint_identity_hash"
                ]
                report["checkpoint_paths"][chapter_id] = str(
                    current_pointer_path(Path(out_dir), "m2v3", chapter_id)
                )
                parent_identity = str(checkpoint["checkpoint_identity_hash"])
                parent_operational = str(checkpoint["checkpoint_hash"])
            report["status"] = "complete"
        except (V3RunHalt, CheckpointError, ValueError) as exc:
            if isinstance(exc, V3RunHalt) and exc.validation_report is not None:
                _merge_counts(counts, exc.validation_report)
            report["status"] = "halted"
            report["stopping_error"] = _halt_record(exc)
        report["validation_counters"] = dict(sorted(counts.items()))
        write_report(Path(out_dir), "m2v3_report.json", report)
    return _json_clone(report)


__all__ = [
    "EXECUTION_MODE_REAL_API",
    "EXECUTION_MODE_SYNTHETIC",
    "KNOWLEDGE_MODE",
    "RealStageExecutor",
    "RealStageSpec",
    "REQUEST_CONTRACT_HASHES",
    "REQUEST_SHAPE_CONTRACT",
    "StageAttemptResult",
    "StageExecutor",
    "SyntheticStageExecutor",
    "V3RunHalt",
    "V3StageRequest",
    "run_m1_v3",
    "run_m2_v3",
    "real_execution_contract_hash",
]
