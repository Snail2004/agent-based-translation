from __future__ import annotations

import json
import os
import re
import sqlite3
import time
from dataclasses import asdict, dataclass
from datetime import date
from hashlib import sha256
from pathlib import Path
from typing import Any, Callable

from pipeline.agents.llm_client import LLMUsage
from pipeline.agents.llm_config import LLMConfig


Transport = Callable[..., Any]


@dataclass(frozen=True)
class JudgeResult:
    text: str
    parsed_json: Any | None
    json_error: str | None
    model: str
    usage: LLMUsage
    cost_usd: float
    latency_ms: int
    from_cache: bool
    cache_key: str


class JudgeConfigError(ValueError):
    """Raised when judge configuration violates the cross-provider rule."""


class JudgeTransportError(RuntimeError):
    """Raised when the real Gemini transport cannot be called."""


class JudgeClient:
    """Gemini-backed judge client with replay cache and injectable transport."""

    def __init__(
        self,
        config: LLMConfig,
        cache_path: str | Path,
        transport: Transport | None = None,
        *,
        max_retries: int = 5,
    ) -> None:
        _guard_cross_provider(config.model)
        self.config = config
        self.cache_path = Path(cache_path)
        self.base_url = _load_gemini_base_url(_peek_gemini_key())
        self.transport = transport or self._build_gemini_transport()
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
    ) -> JudgeResult:
        request_for_key = {
            "model": self.config.model,
            "base_url": self.base_url or "google_official",
            "messages": messages,
            "temperature": self.config.temperature,
            "response_format": response_format,
        }
        request_json = _canonical_json(request_for_key)
        cache_key = sha256(request_json.encode("utf-8")).hexdigest()

        if not bypass_cache:
            cached = self._load_cached(cache_key, response_format)
            if cached is not None:
                return cached

        api_request = {
            "model": self.config.model,
            "messages": messages,
            "temperature": self.config.temperature,
            "response_format": response_format,
            "max_output_tokens": self.config.max_output_tokens,
        }
        started = time.perf_counter()
        response = self._call_with_retries(api_request)
        latency_ms = int((time.perf_counter() - started) * 1000)

        text = _extract_text(response)
        usage = _extract_usage(response)
        parsed_json, json_error = _parse_json_if_requested(text, response_format)
        result = JudgeResult(
            text=text,
            parsed_json=parsed_json,
            json_error=json_error,
            model=self.config.model,
            usage=usage,
            cost_usd=self._estimate_cost(usage),
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

    def _build_gemini_transport(self) -> Transport:
        api_key = _load_gemini_key()
        if not api_key:
            raise JudgeTransportError(
                "Gemini API key not found. Set GEMINI_API_KEY/GOOGLE_API_KEY "
                "or create GEMINI-KEY.txt in the repo root."
            )
        try:
            from google import genai
            from google.genai import types
        except ImportError as exc:  # pragma: no cover - manual path.
            raise JudgeTransportError(
                "google-genai package is required for real Gemini judge calls"
            ) from exc

        http_options = None
        if self.base_url:
            http_options = types.HttpOptions(baseUrl=self.base_url, timeout=120_000)
        else:
            http_options = types.HttpOptions(timeout=120_000)
        client = genai.Client(api_key=api_key, http_options=http_options)

        def _transport(**kwargs: Any) -> Any:
            messages = list(kwargs["messages"])
            system_parts: list[str] = []
            contents: list[str] = []
            for message in messages:
                role = str(message.get("role", "user"))
                content = str(message.get("content", ""))
                if role == "system":
                    system_parts.append(content)
                else:
                    contents.append(content)

            response_format = kwargs.get("response_format") or {}
            wants_json = _wants_json(response_format)
            generate_config = types.GenerateContentConfig(
                temperature=float(kwargs.get("temperature", 0.0)),
                max_output_tokens=int(kwargs.get("max_output_tokens", 2048)),
                response_mime_type="application/json" if wants_json else "text/plain",
                system_instruction="\n\n".join(system_parts) or None,
            )
            return client.models.generate_content(
                model=str(kwargs["model"]),
                contents="\n\n".join(contents),
                config=generate_config,
            )

        return _transport

    def _call_with_retries(self, api_request: dict[str, Any]) -> Any:
        attempt = 0
        while True:
            try:
                return self.transport(**api_request)
            except Exception as exc:
                status_code = _status_code(exc)
                if not _is_retryable(exc, status_code) or attempt >= self.max_retries:
                    raise
                delay = _retry_delay(exc, attempt)
                time.sleep(delay)
                attempt += 1

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.cache_path)
        connection.row_factory = sqlite3.Row
        return connection

    def _init_db(self) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS judge_call_cache (
                    cache_key TEXT PRIMARY KEY,
                    model TEXT NOT NULL,
                    tag TEXT NOT NULL,
                    request_json TEXT NOT NULL,
                    response_text TEXT NOT NULL,
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
    ) -> JudgeResult | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT response_text, model, usage_json, cost_usd, latency_ms
                FROM judge_call_cache
                WHERE cache_key = ?
                """,
                (cache_key,),
            ).fetchone()
        if row is None:
            return None
        text = str(row["response_text"])
        parsed_json, json_error = _parse_json_if_requested(text, response_format)
        return JudgeResult(
            text=text,
            parsed_json=parsed_json,
            json_error=json_error,
            model=str(row["model"]),
            usage=LLMUsage(**json.loads(str(row["usage_json"]))),
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
        result: JudgeResult,
    ) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                INSERT OR REPLACE INTO judge_call_cache (
                    cache_key, model, tag, request_json, response_text,
                    usage_json, cost_usd, latency_ms
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    cache_key,
                    result.model,
                    tag,
                    request_json,
                    result.text,
                    json.dumps(asdict(result.usage), sort_keys=True),
                    result.cost_usd,
                    result.latency_ms,
                ),
            )

    def _estimate_cost(self, usage: LLMUsage) -> float:
        pricing = self.config.pricing
        uncached_input = max(usage.prompt_tokens - usage.cached_tokens, 0)
        cost = (
            (uncached_input / 1_000_000) * pricing["input"]
            + (usage.cached_tokens / 1_000_000) * pricing["cached_input"]
            + (usage.completion_tokens / 1_000_000) * pricing["output"]
        )
        return round(cost, 12)

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


def _guard_cross_provider(model: str) -> None:
    model_lower = model.lower()
    forbidden = ("gpt", "o1", "o3", "openai")
    if any(token in model_lower for token in forbidden):
        raise JudgeConfigError(f"Judge model must not be GPT/OpenAI: {model}")
    if "gemini" not in model_lower:
        raise JudgeConfigError(f"Judge model must be Gemini for EV-02: {model}")


def _load_gemini_key() -> str:
    return _peek_gemini_key()


def _peek_gemini_key() -> str:
    for env_name in ("GEMINI_API_KEY", "GOOGLE_API_KEY"):
        value = os.environ.get(env_name, "").strip()
        if value:
            return value

    for parent in [Path.cwd(), *Path(__file__).resolve().parents]:
        candidate = parent / "GEMINI-KEY.txt"
        if candidate.exists():
            return candidate.read_text(encoding="utf-8").strip()
    return ""


def _load_gemini_base_url(api_key: str) -> str | None:
    value = os.environ.get("GEMINI_BASE_URL", "").strip()
    if value:
        return value.rstrip("/")
    for parent in [Path.cwd(), *Path(__file__).resolve().parents]:
        candidate = parent / "GEMINI-BASE-URL.txt"
        if candidate.exists():
            text = candidate.read_text(encoding="utf-8").strip()
            if text:
                return text.rstrip("/")
    if api_key.startswith("sk-"):
        return "https://api.shopaikey.com"
    return None


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _get_value(obj: Any, key: str, default: Any = None) -> Any:
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def _extract_text(response: Any) -> str:
    text = _get_value(response, "text", None)
    if text is not None:
        return str(text)
    candidates = _get_value(response, "candidates", []) or []
    if candidates:
        candidate = candidates[0]
        content = _get_value(candidate, "content", {})
        parts = _get_value(content, "parts", []) or []
        texts = []
        for part in parts:
            part_text = _get_value(part, "text", None)
            if part_text is not None:
                texts.append(str(part_text))
        return "".join(texts)
    return str(response)


def _extract_usage(response: Any) -> LLMUsage:
    usage = _get_value(response, "usage_metadata", None)
    if usage is None:
        usage = _get_value(response, "usage", {}) or {}
    prompt = (
        _get_value(usage, "prompt_token_count", None)
        or _get_value(usage, "prompt_tokens", 0)
        or 0
    )
    completion = (
        _get_value(usage, "candidates_token_count", None)
        or _get_value(usage, "completion_tokens", 0)
        or 0
    )
    return LLMUsage(prompt_tokens=int(prompt), completion_tokens=int(completion))


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
    return None


def _is_retryable(exc: Exception, status_code: int | None) -> bool:
    if status_code == 429 or (status_code is not None and 500 <= status_code <= 599):
        return True
    class_name = exc.__class__.__name__.casefold()
    return "timeout" in class_name or "connection" in class_name


def _retry_delay(exc: Exception, attempt: int) -> float:
    response_json = getattr(exc, "response_json", None)
    if isinstance(response_json, dict):
        delay = _retry_delay_from_payload(response_json)
        if delay is not None:
            return delay
    message = str(exc)
    match = re.search(r"retry in ([0-9.]+)s", message, flags=re.IGNORECASE)
    if match:
        return float(match.group(1)) + 0.5
    return min(60.0, float(2**attempt))


def _retry_delay_from_payload(payload: dict[str, Any]) -> float | None:
    details = payload.get("error", {}).get("details", [])
    if not isinstance(details, list):
        return None
    for item in details:
        if not isinstance(item, dict):
            continue
        retry_delay = item.get("retryDelay")
        if not retry_delay:
            continue
        text = str(retry_delay)
        if text.endswith("s"):
            text = text[:-1]
        try:
            return float(text) + 0.5
        except ValueError:
            return None
    return None
