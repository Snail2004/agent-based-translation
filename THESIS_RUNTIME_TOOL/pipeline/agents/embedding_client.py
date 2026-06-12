from __future__ import annotations

import json
import sqlite3
import time
import unicodedata
from dataclasses import asdict, dataclass, field
from datetime import date
from hashlib import sha256
from pathlib import Path
from typing import Any, Callable


EMBEDDING_MODEL = "text-embedding-3-large"
DEFAULT_EMBEDDING_PRICING = {"input": 0.13}
Transport = Callable[..., Any]


@dataclass(frozen=True)
class EmbeddingConfig:
    """Configuration for the thesis embedding client."""

    model: str = EMBEDDING_MODEL
    dimensions: int = 3072
    batch_size: int = 64
    pricing: dict[str, float] = field(
        default_factory=lambda: dict(DEFAULT_EMBEDDING_PRICING)
    )

    def __post_init__(self) -> None:
        model_lower = self.model.lower()
        if "latest" in model_lower or self.model != EMBEDDING_MODEL:
            raise ValueError(f"Embedding model must be pinned to {EMBEDDING_MODEL}")
        if self.dimensions <= 0:
            raise ValueError("dimensions must be positive")
        if self.batch_size <= 0:
            raise ValueError("batch_size must be positive")
        if "input" not in self.pricing:
            raise ValueError("pricing is missing key: input")

    @classmethod
    def from_mapping(cls, data: dict[str, Any]) -> "EmbeddingConfig":
        return cls(
            model=str(data.get("model", EMBEDDING_MODEL)),
            dimensions=int(data.get("dimensions", 3072)),
            batch_size=int(data.get("batch_size", 64)),
            pricing={
                key: float(value)
                for key, value in dict(
                    data.get("pricing", DEFAULT_EMBEDDING_PRICING)
                ).items()
            },
        )


@dataclass(frozen=True)
class EmbeddingUsage:
    input_tokens: int = 0
    cost_usd: float = 0.0
    calls: int = 0
    cache_hits: int = 0

    def to_dict(self) -> dict[str, int | float]:
        return asdict(self)


class EmbeddingClient:
    """OpenAI embedding client with per-text SQLite replay cache."""

    def __init__(
        self,
        config: EmbeddingConfig,
        cache_path: str | Path,
        transport: Transport | None = None,
    ) -> None:
        self.config = config
        self.cache_path = Path(cache_path)
        self.transport = transport or self._build_openai_transport()
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()
        self.reset_session_usage()

    def embed(self, texts: list[str]) -> list[list[float]]:
        normalized_texts = [_normalize_text(text) for text in texts]
        results: list[list[float] | None] = [None] * len(normalized_texts)
        misses: list[tuple[int, str, str]] = []

        seen_miss_by_key: dict[str, int] = {}
        duplicate_misses: list[tuple[int, int]] = []
        for index, text in enumerate(normalized_texts):
            cache_key = _cache_key(self.config.model, self.config.dimensions, text)
            cached = self._load_cached(cache_key)
            if cached is not None:
                self._session_cache_hits += 1
                results[index] = cached
                continue
            if cache_key in seen_miss_by_key:
                duplicate_misses.append((index, seen_miss_by_key[cache_key]))
                continue
            seen_miss_by_key[cache_key] = index
            misses.append((index, text, cache_key))

        for batch in _chunks(misses, self.config.batch_size):
            batch_texts = [item[1] for item in batch]
            started = time.perf_counter()
            response = self.transport(
                model=self.config.model,
                input=batch_texts,
                dimensions=self.config.dimensions,
            )
            latency_ms = int((time.perf_counter() - started) * 1000)
            embeddings = _extract_embeddings(response)
            if len(embeddings) != len(batch_texts):
                raise RuntimeError(
                    "Embedding transport returned "
                    f"{len(embeddings)} vectors for {len(batch_texts)} texts"
                )

            input_tokens = _extract_prompt_tokens(response)
            cost_usd = self._estimate_cost(input_tokens)
            self._session_input_tokens += input_tokens
            self._session_cost_usd = round(self._session_cost_usd + cost_usd, 12)
            self._session_calls += 1
            self._increment_usage_today(input_tokens, cost_usd)

            token_shares = _allocate_tokens(input_tokens, batch_texts)
            cost_shares = _allocate_cost(cost_usd, token_shares, input_tokens)
            for (index, text, cache_key), embedding, token_count, row_cost in zip(
                batch, embeddings, token_shares, cost_shares
            ):
                self._store_cached(
                    cache_key,
                    text,
                    embedding,
                    token_count=token_count,
                    cost_usd=row_cost,
                    latency_ms=latency_ms,
                )
                results[index] = embedding

        for index, original_index in duplicate_misses:
            original = results[original_index]
            if original is None:
                raise RuntimeError("Internal duplicate embedding resolution failed")
            results[index] = original

        if any(embedding is None for embedding in results):
            raise RuntimeError("Internal embedding resolution failed")
        return [embedding for embedding in results if embedding is not None]

    def reset_session_usage(self) -> None:
        self._session_input_tokens = 0
        self._session_cost_usd = 0.0
        self._session_calls = 0
        self._session_cache_hits = 0

    @property
    def session_usage(self) -> EmbeddingUsage:
        return EmbeddingUsage(
            input_tokens=self._session_input_tokens,
            cost_usd=round(self._session_cost_usd, 12),
            calls=self._session_calls,
            cache_hits=self._session_cache_hits,
        )

    def get_usage_today(self) -> dict[str, int | float | str]:
        today = date.today().isoformat()
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT date, total_tokens, calls, cost_usd
                FROM usage_daily
                WHERE date = ?
                """,
                (today,),
            ).fetchone()
        if row is None:
            return {"date": today, "total_tokens": 0, "calls": 0, "cost_usd": 0.0}
        return {
            "date": str(row["date"]),
            "total_tokens": int(row["total_tokens"]),
            "calls": int(row["calls"]),
            "cost_usd": float(row["cost_usd"]),
        }

    def _build_openai_transport(self) -> Transport:
        try:
            from openai import OpenAI
        except ImportError as exc:  # pragma: no cover - manual smoke path.
            raise RuntimeError("openai package is required for real embedding calls") from exc

        client = OpenAI()
        return client.embeddings.create

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.cache_path)
        connection.row_factory = sqlite3.Row
        return connection

    def _init_db(self) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS embedding_cache (
                    cache_key TEXT PRIMARY KEY,
                    model TEXT NOT NULL,
                    dimensions INTEGER NOT NULL,
                    text_hash TEXT NOT NULL,
                    text TEXT NOT NULL,
                    embedding_json TEXT NOT NULL,
                    token_count INTEGER NOT NULL,
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
                    calls INTEGER NOT NULL,
                    cost_usd REAL NOT NULL
                )
                """
            )

    def _load_cached(self, cache_key: str) -> list[float] | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT embedding_json
                FROM embedding_cache
                WHERE cache_key = ?
                """,
                (cache_key,),
            ).fetchone()
        if row is None:
            return None
        return [float(value) for value in json.loads(str(row["embedding_json"]))]

    def _store_cached(
        self,
        cache_key: str,
        text: str,
        embedding: list[float],
        *,
        token_count: int,
        cost_usd: float,
        latency_ms: int,
    ) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                INSERT OR REPLACE INTO embedding_cache (
                    cache_key, model, dimensions, text_hash, text,
                    embedding_json, token_count, cost_usd, latency_ms
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    cache_key,
                    self.config.model,
                    self.config.dimensions,
                    sha256(text.encode("utf-8")).hexdigest(),
                    text,
                    json.dumps(embedding, separators=(",", ":")),
                    token_count,
                    cost_usd,
                    latency_ms,
                ),
            )

    def _increment_usage_today(self, tokens: int, cost_usd: float) -> None:
        today = date.today().isoformat()
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO usage_daily (date, total_tokens, calls, cost_usd)
                VALUES (?, ?, 1, ?)
                ON CONFLICT(date) DO UPDATE SET
                    total_tokens = total_tokens + excluded.total_tokens,
                    calls = calls + 1,
                    cost_usd = cost_usd + excluded.cost_usd
                """,
                (today, tokens, cost_usd),
            )

    def _estimate_cost(self, input_tokens: int) -> float:
        return round((input_tokens / 1_000_000) * self.config.pricing["input"], 12)


def load_embedding_config(path: str | Path) -> EmbeddingConfig:
    try:
        import yaml
    except ImportError as exc:  # pragma: no cover - exercised only without deps.
        raise RuntimeError("PyYAML is required to load embedding config files") from exc

    config_path = Path(path)
    data = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"Expected YAML mapping in {config_path}")
    return EmbeddingConfig.from_mapping(data)


def _normalize_text(text: str) -> str:
    return unicodedata.normalize("NFC", str(text))


def _cache_key(model: str, dimensions: int, text: str) -> str:
    request = {
        "model": model,
        "dimensions": dimensions,
        "text": text,
    }
    payload = json.dumps(
        request,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return sha256(payload.encode("utf-8")).hexdigest()


def _get_value(obj: Any, key: str, default: Any = None) -> Any:
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def _extract_embeddings(response: Any) -> list[list[float]]:
    data = _get_value(response, "data", []) or []
    indexed: list[tuple[int, list[float]]] = []
    unindexed: list[list[float]] = []
    for position, item in enumerate(data):
        embedding = _get_value(item, "embedding", None)
        if embedding is None:
            continue
        vector = [float(value) for value in embedding]
        index = _get_value(item, "index", None)
        if isinstance(index, int):
            indexed.append((index, vector))
        else:
            unindexed.append(vector)
            indexed.append((position, vector))
    if unindexed and len(unindexed) == len(data):
        return unindexed
    return [vector for _, vector in sorted(indexed, key=lambda item: item[0])]


def _extract_prompt_tokens(response: Any) -> int:
    usage = _get_value(response, "usage", {}) or {}
    prompt_tokens = _get_value(usage, "prompt_tokens", None)
    if prompt_tokens is None:
        prompt_tokens = _get_value(usage, "total_tokens", 0)
    return int(prompt_tokens or 0)


def _chunks(items: list[Any], size: int) -> list[list[Any]]:
    return [items[index : index + size] for index in range(0, len(items), size)]


def _allocate_tokens(total_tokens: int, texts: list[str]) -> list[int]:
    if not texts:
        return []
    if total_tokens <= 0:
        return [0 for _ in texts]
    weights = [max(1, len(text)) for text in texts]
    weight_total = sum(weights)
    shares = [int(total_tokens * weight / weight_total) for weight in weights]
    remainder = total_tokens - sum(shares)
    for index in range(remainder):
        shares[index % len(shares)] += 1
    return shares


def _allocate_cost(
    total_cost: float,
    token_shares: list[int],
    total_tokens: int,
) -> list[float]:
    if not token_shares:
        return []
    if total_tokens <= 0:
        return [0.0 for _ in token_shares]
    costs = [
        round(total_cost * (token_count / total_tokens), 12)
        for token_count in token_shares
    ]
    if costs:
        costs[-1] = round(total_cost - sum(costs[:-1]), 12)
    return costs
