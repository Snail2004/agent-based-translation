from __future__ import annotations

import json
import random
import sqlite3
import time
from dataclasses import asdict, dataclass
from datetime import date
from hashlib import sha256
from pathlib import Path
from typing import Any, Callable

from pipeline.agents.llm_config import LLMConfig


Transport = Callable[..., Any]


@dataclass(frozen=True)
class LLMUsage:
    prompt_tokens: int = 0
    cached_tokens: int = 0
    completion_tokens: int = 0
    reasoning_tokens: int = 0

    @property
    def total_billable_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens


@dataclass(frozen=True)
class LLMResult:
    text: str
    parsed_json: Any | None
    json_error: str | None
    model: str
    system_fingerprint: str | None
    usage: LLMUsage
    cost_usd: float
    latency_ms: int
    from_cache: bool
    cache_key: str


class DailyQuotaExceeded(RuntimeError):
    """Raised before a real transport call would exceed the daily token cap."""


class LLMTransportError(RuntimeError):
    """Transport failure after retry policy has been applied."""

    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class LLMClient:
    """Single OpenAI Chat Completions client for thesis runtime agents."""

    def __init__(
        self,
        config: LLMConfig,
        cache_path: str | Path,
        transport: Transport | None = None,
        *,
        max_retries: int = 5,
    ) -> None:
        self.config = config
        self.cache_path = Path(cache_path)
        self.transport = transport or self._build_openai_transport()
        self.max_retries = max_retries
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def call(
        self,
        messages: list[dict[str, Any]],
        *,
        response_format: dict[str, Any] | None = None,
        tag: str = "",
        bypass_cache: bool = False,
    ) -> LLMResult:
        request_for_key = {
            "model": self.config.model,
            "messages": messages,
            "temperature": self.config.temperature,
            "seed": self.config.seed,
            "reasoning_effort": self.config.reasoning_effort,
            "response_format": response_format,
        }
        request_json = _canonical_json(request_for_key)
        cache_key = sha256(request_json.encode("utf-8")).hexdigest()

        if not bypass_cache:
            cached = self._load_cached(cache_key, response_format)
            if cached is not None:
                return cached

        self._raise_if_over_quota(self._estimate_tokens(messages, response_format))

        api_request = {
            "model": self.config.model,
            "messages": messages,
            "temperature": self.config.temperature,
            "seed": self.config.seed,
            "reasoning_effort": self.config.reasoning_effort,
            "verbosity": self.config.verbosity,
            "response_format": response_format,
            "max_completion_tokens": self.config.max_output_tokens,
        }
        api_request = {key: value for key, value in api_request.items() if value is not None}

        started = time.perf_counter()
        response = self._call_with_retries(api_request)
        latency_ms = int((time.perf_counter() - started) * 1000)

        text = _extract_text(response)
        system_fingerprint = _get_value(response, "system_fingerprint")
        usage = _extract_usage(response)
        cost_usd = self._estimate_cost(usage)
        parsed_json, json_error = _parse_json_if_requested(text, response_format)
        result = LLMResult(
            text=text,
            parsed_json=parsed_json,
            json_error=json_error,
            model=self.config.model,
            system_fingerprint=system_fingerprint,
            usage=usage,
            cost_usd=cost_usd,
            latency_ms=latency_ms,
            from_cache=False,
            cache_key=cache_key,
        )

        self._store_cached(cache_key, tag, request_json, result)
        self._increment_usage_today(usage.total_billable_tokens)
        return result

    def get_usage_today(self) -> dict[str, int | str]:
        today = date.today().isoformat()
        with self._connect() as connection:
            row = connection.execute(
                "SELECT date, total_tokens, calls FROM usage_daily WHERE date = ?",
                (today,),
            ).fetchone()
        if row is None:
            return {"date": today, "total_tokens": 0, "calls": 0}
        return {
            "date": str(row["date"]),
            "total_tokens": int(row["total_tokens"]),
            "calls": int(row["calls"]),
        }

    def _build_openai_transport(self) -> Transport:
        try:
            from openai import OpenAI
        except ImportError as exc:  # pragma: no cover - smoke/manual path.
            raise RuntimeError("openai package is required for real LLM calls") from exc

        client = OpenAI()
        return client.chat.completions.create

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.cache_path)
        connection.row_factory = sqlite3.Row
        return connection

    def _init_db(self) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS llm_call_cache (
                    cache_key TEXT PRIMARY KEY,
                    model TEXT NOT NULL,
                    tag TEXT NOT NULL,
                    request_json TEXT NOT NULL,
                    response_text TEXT NOT NULL,
                    system_fingerprint TEXT,
                    usage_json TEXT NOT NULL,
                    cost_usd REAL NOT NULL,
                    latency_ms INTEGER NOT NULL,
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS usage_daily (
                    date TEXT PRIMARY KEY,
                    total_tokens INTEGER NOT NULL,
                    calls INTEGER NOT NULL
                )
                """
            )

    def _load_cached(
        self,
        cache_key: str,
        response_format: dict[str, Any] | None,
    ) -> LLMResult | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT response_text, model, system_fingerprint, usage_json,
                       cost_usd, latency_ms
                FROM llm_call_cache
                WHERE cache_key = ?
                """,
                (cache_key,),
            ).fetchone()
        if row is None:
            return None

        usage = LLMUsage(**json.loads(str(row["usage_json"])))
        text = str(row["response_text"])
        parsed_json, json_error = _parse_json_if_requested(text, response_format)
        return LLMResult(
            text=text,
            parsed_json=parsed_json,
            json_error=json_error,
            model=str(row["model"]),
            system_fingerprint=row["system_fingerprint"],
            usage=usage,
            cost_usd=float(row["cost_usd"]),
            latency_ms=int(row["latency_ms"]),
            from_cache=True,
            cache_key=cache_key,
        )

    def _store_cached(
        self,
        cache_key: str,
        tag: str,
        request_json: str,
        result: LLMResult,
    ) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                INSERT OR REPLACE INTO llm_call_cache (
                    cache_key, model, tag, request_json, response_text,
                    system_fingerprint, usage_json, cost_usd, latency_ms
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    cache_key,
                    result.model,
                    tag,
                    request_json,
                    result.text,
                    result.system_fingerprint,
                    json.dumps(asdict(result.usage), sort_keys=True),
                    result.cost_usd,
                    result.latency_ms,
                ),
            )

    def _call_with_retries(self, api_request: dict[str, Any]) -> Any:
        attempt = 0
        while True:
            try:
                return self.transport(**api_request)
            except Exception as exc:
                status_code = _status_code(exc)
                retryable = _is_retryable(exc, status_code)
                if not retryable or attempt >= self.max_retries:
                    raise LLMTransportError(
                        str(exc), status_code=status_code
                    ) from exc
                delay = _retry_delay(exc, attempt)
                time.sleep(delay)
                attempt += 1

    def _estimate_cost(self, usage: LLMUsage) -> float:
        pricing = self.config.pricing
        uncached_input = max(usage.prompt_tokens - usage.cached_tokens, 0)
        cost = (
            (uncached_input / 1_000_000) * pricing["input"]
            + (usage.cached_tokens / 1_000_000) * pricing["cached_input"]
            + (usage.completion_tokens / 1_000_000) * pricing["output"]
        )
        return round(cost, 12)

    def _estimate_tokens(
        self,
        messages: list[dict[str, Any]],
        response_format: dict[str, Any] | None,
    ) -> int:
        request = {"messages": messages, "response_format": response_format}
        rough_chars = len(_canonical_json(request))
        return max(1, rough_chars // 4) + self.config.max_output_tokens

    def _raise_if_over_quota(self, estimated_tokens: int) -> None:
        usage = self.get_usage_today()
        projected = int(usage["total_tokens"]) + estimated_tokens
        if projected > self.config.daily_token_cap:
            raise DailyQuotaExceeded(
                "Daily LLM token cap would be exceeded: "
                f"{projected} > {self.config.daily_token_cap}"
            )

    def _increment_usage_today(self, tokens: int) -> None:
        today = date.today().isoformat()
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO usage_daily (date, total_tokens, calls)
                VALUES (?, ?, 1)
                ON CONFLICT(date) DO UPDATE SET
                    total_tokens = total_tokens + excluded.total_tokens,
                    calls = calls + 1
                """,
                (today, tokens),
            )


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _get_value(obj: Any, key: str, default: Any = None) -> Any:
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def _nested_value(obj: Any, path: list[str], default: Any = 0) -> Any:
    current = obj
    for key in path:
        current = _get_value(current, key, None)
        if current is None:
            return default
    return current


def _extract_text(response: Any) -> str:
    choices = _get_value(response, "choices", [])
    if not choices:
        return ""
    choice = choices[0]
    message = _get_value(choice, "message", {})
    content = _get_value(message, "content", "")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for item in content:
            text = _get_value(item, "text", None)
            if text is not None:
                parts.append(str(text))
        return "".join(parts)
    return str(content)


def _extract_usage(response: Any) -> LLMUsage:
    usage = _get_value(response, "usage", {}) or {}
    return LLMUsage(
        prompt_tokens=int(_get_value(usage, "prompt_tokens", 0) or 0),
        cached_tokens=int(
            _nested_value(usage, ["prompt_tokens_details", "cached_tokens"], 0) or 0
        ),
        completion_tokens=int(_get_value(usage, "completion_tokens", 0) or 0),
        reasoning_tokens=int(
            _nested_value(
                usage,
                ["completion_tokens_details", "reasoning_tokens"],
                0,
            )
            or 0
        ),
    )


def _parse_json_if_requested(
    text: str,
    response_format: dict[str, Any] | None,
) -> tuple[Any | None, str | None]:
    if not _wants_json(response_format):
        return None, None
    try:
        return json.loads(text), None
    except json.JSONDecodeError as exc:
        return None, str(exc)


def _wants_json(response_format: dict[str, Any] | None) -> bool:
    if not response_format:
        return False
    format_type = str(response_format.get("type", "")).lower()
    return "json" in format_type


def _status_code(exc: Exception) -> int | None:
    for attr in ("status_code", "status"):
        value = getattr(exc, attr, None)
        if isinstance(value, int):
            return value
    value = getattr(exc, "code", None)
    if isinstance(value, int):
        return value
    return None


def _is_retryable(exc: Exception, status_code: int | None) -> bool:
    if isinstance(exc, TimeoutError):
        return True
    if status_code == 429:
        return True
    if status_code is not None and 500 <= status_code <= 599:
        return True
    return False


def _retry_delay(exc: Exception, attempt: int) -> float:
    retry_after = getattr(exc, "retry_after", None)
    if retry_after is not None:
        try:
            return float(retry_after)
        except (TypeError, ValueError):
            pass
    headers = getattr(exc, "headers", None)
    if isinstance(headers, dict) and headers.get("Retry-After"):
        try:
            return float(headers["Retry-After"])
        except (TypeError, ValueError):
            pass
    return (2**attempt) + random.uniform(0, 0.1)
