from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable
from urllib.parse import urlparse, urlunparse

import requests


DEFAULT_EMBED_ENDPOINT = "http://127.0.0.1:1234/v1/embeddings"
DEFAULT_EMBED_MODEL = "text-embedding-labse"


@dataclass(frozen=True)
class EmbeddingModelConfig:
    alias: str
    endpoint_model: str
    model_version: str
    query_prefix: str = ""
    passage_prefix: str = ""

    @property
    def prefix_profile(self) -> str:
        return f"q={self.query_prefix!r};p={self.passage_prefix!r}"


@dataclass(frozen=True)
class EmbeddingModelIdentity:
    alias: str
    endpoint_model: str
    model_version: str
    query_prefix: str
    passage_prefix: str
    embedding_dim: int | None
    hf_repo: str | None = None
    quant: str | None = None
    display_name: str | None = None
    context_length: int | None = None
    status: str = "available"
    skipped_with_reason: str | None = None

    def cache_model_version(self) -> str:
        pieces = [
            self.model_version,
            f"hf={self.hf_repo or 'unknown'}",
            f"quant={self.quant or 'unknown'}",
            f"dim={self.embedding_dim or 'unknown'}",
        ]
        return "|".join(pieces)


DEFAULT_MODEL_CONFIGS: dict[str, EmbeddingModelConfig] = {
    "labse": EmbeddingModelConfig(
        alias="labse",
        endpoint_model="text-embedding-labse",
        model_version="ChristianAzinn/labse-gguf:Q8_0",
    ),
    "bge-m3": EmbeddingModelConfig(
        alias="bge-m3",
        endpoint_model="text-embedding-bge-m3",
        model_version="gpustack/bge-m3-GGUF:Q8_0",
    ),
    "e5": EmbeddingModelConfig(
        alias="e5",
        endpoint_model="text-embedding-multilingual-e5-large-instruct",
        model_version="Ralriki/multilingual-e5-large-instruct-GGUF:Q8_0",
        query_prefix="query: ",
        passage_prefix="passage: ",
    ),
}


@dataclass(frozen=True)
class TextUnit:
    start: int
    end: int
    text: str


@dataclass(frozen=True)
class RankedTextUnit:
    index: int
    unit: TextUnit
    score: float


class EmbeddingCacheClient:
    """OpenAI-compatible embedding client with a frozen per-text disk cache."""

    def __init__(
        self,
        *,
        endpoint: str = DEFAULT_EMBED_ENDPOINT,
        model: str = DEFAULT_EMBED_MODEL,
        cache_dir: str | Path,
        model_version: str = "lmstudio-local",
        model_alias: str = "default",
        query_prefix: str = "",
        passage_prefix: str = "",
        prefix_profile: str | None = None,
        timeout: int = 180,
        batch_size: int = 96,
        post: Callable[..., Any] | None = None,
    ) -> None:
        self.endpoint = endpoint
        self.model = model
        self.model_version = model_version
        self.model_alias = model_alias
        self.query_prefix = query_prefix
        self.passage_prefix = passage_prefix
        self.prefix_profile = prefix_profile or f"q={query_prefix!r};p={passage_prefix!r}"
        self.cache_dir = Path(cache_dir)
        self.timeout = timeout
        self.batch_size = batch_size
        self._post = post or requests.post
        self.requests = 0
        self.inputs = 0
        self.cache_hits = 0
        self.cache_misses = 0
        self.cache_dir.mkdir(parents=True, exist_ok=True)

    def embed(self, texts: Iterable[str]) -> list[list[float]]:
        ordered = [_normalize_text(text) for text in texts]
        result: dict[str, list[float]] = {}
        missing: list[str] = []
        for text in ordered:
            cached = self._read_cache(text)
            if cached is None:
                missing.append(text)
                self.cache_misses += 1
            else:
                result[text] = cached
                self.cache_hits += 1

        for start in range(0, len(missing), self.batch_size):
            batch = missing[start:start + self.batch_size]
            if not batch:
                continue
            payload = {"model": self.model, "input": batch}
            response = self._post(self.endpoint, json=payload, timeout=self.timeout)
            response.raise_for_status()
            body = response.json()
            response_model = str(body.get("model") or self.model)
            if response_model != self.model:
                raise RuntimeError(
                    f"Embedding endpoint returned model {response_model!r}, expected {self.model!r}"
                )
            self.requests += 1
            self.inputs += len(batch)
            for text, item in zip(batch, body["data"], strict=True):
                vector = [float(value) for value in item["embedding"]]
                self._write_cache(text, vector)
                result[text] = vector

        return [result[text] for text in ordered]

    def stats(self) -> dict[str, Any]:
        return {
            "endpoint": self.endpoint,
            "model_alias": self.model_alias,
            "model_id": self.model,
            "model_version": self.model_version,
            "query_prefix": self.query_prefix,
            "passage_prefix": self.passage_prefix,
            "prefix_profile": self.prefix_profile,
            "cache_dir": str(self.cache_dir),
            "cache_hits": self.cache_hits,
            "cache_misses": self.cache_misses,
            "requests": self.requests,
            "inputs": self.inputs,
        }

    def _cache_path(self, text: str) -> Path:
        key = cache_key(self.model_alias, self.model, self.model_version, self.prefix_profile, text)
        return self.cache_dir / f"{key}.json"

    def _read_cache(self, text: str) -> list[float] | None:
        path = self._cache_path(text)
        if not path.exists():
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
        return [float(value) for value in data["embedding"]]

    def _write_cache(self, text: str, vector: list[float]) -> None:
        path = self._cache_path(text)
        path.write_text(
            json.dumps(
                {
                    "model_id": self.model,
                    "model_alias": self.model_alias,
                    "model_version": self.model_version,
                    "prefix_profile": self.prefix_profile,
                    "normalized_text": text,
                    "embedding": vector,
                },
                ensure_ascii=False,
                separators=(",", ":"),
            ),
            encoding="utf-8",
        )


def cache_key(
    model_alias: str,
    endpoint_model: str,
    model_version: str,
    prefix_profile: str,
    normalized_text: str,
) -> str:
    raw = (
        f"{model_alias}|{endpoint_model}|{model_version}|{prefix_profile}|"
        f"{_normalize_text(normalized_text)}"
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def split_sentences(text: str) -> list[TextUnit]:
    value = str(text or "")
    pattern = re.compile(r"(?<=[.!?。！？])\s+|\n\s*\n+")
    return _split_units(value, pattern)


def containing_unit(units: list[TextUnit], start: int, end: int) -> TextUnit | None:
    for unit in units:
        if unit.start <= start and end <= unit.end:
            return unit
    return None


def top_k_target_sentences(
    source_query: str,
    target_sentences: list[TextUnit],
    *,
    k: int,
    client: EmbeddingCacheClient,
) -> list[RankedTextUnit]:
    if k <= 0 or not target_sentences:
        return []
    vectors = client.embed([
        f"{client.query_prefix}{source_query}",
        *[f"{client.passage_prefix}{unit.text}" for unit in target_sentences],
    ])
    query = vectors[0]
    ranked = [
        RankedTextUnit(index=index, unit=unit, score=_cosine(query, vector))
        for index, (unit, vector) in enumerate(zip(target_sentences, vectors[1:], strict=True))
    ]
    return sorted(ranked, key=lambda item: (-item.score, item.index))[:k]


def parse_model_specs(raw: str | None) -> list[EmbeddingModelConfig]:
    if not raw:
        return [DEFAULT_MODEL_CONFIGS["labse"]]
    configs: list[EmbeddingModelConfig] = []
    for part in raw.split(","):
        item = part.strip()
        if not item:
            continue
        if "=" in item:
            alias, value = item.split("=", 1)
            alias = alias.strip()
        else:
            value = item
            alias = value.split("@", 1)[0].strip()
        endpoint_model, version = (value.split("@", 1) + [""])[:2] if "@" in value else (value.strip(), "")
        endpoint_model = endpoint_model.strip()
        version = version.strip()
        base = DEFAULT_MODEL_CONFIGS.get(alias)
        if base is None and endpoint_model in {cfg.endpoint_model for cfg in DEFAULT_MODEL_CONFIGS.values()}:
            base = next(cfg for cfg in DEFAULT_MODEL_CONFIGS.values() if cfg.endpoint_model == endpoint_model)
        if base is None:
            configs.append(EmbeddingModelConfig(alias=alias, endpoint_model=endpoint_model, model_version=version or "unknown"))
            continue
        # The spec examples use short endpoint names. For known aliases, keep the
        # tested LM Studio endpoint id from the config unless the user passes a
        # concrete text-embedding-* id.
        resolved_endpoint = endpoint_model if endpoint_model.startswith("text-embedding-") else base.endpoint_model
        configs.append(
            EmbeddingModelConfig(
                alias=alias,
                endpoint_model=resolved_endpoint,
                model_version=version or base.model_version,
                query_prefix=base.query_prefix,
                passage_prefix=base.passage_prefix,
            )
        )
    return configs


def preflight_embedding_model(
    *,
    endpoint: str,
    config: EmbeddingModelConfig,
    timeout: int = 30,
    get: Callable[..., Any] | None = None,
    post: Callable[..., Any] | None = None,
) -> EmbeddingModelIdentity:
    getter = get or requests.get
    poster = post or requests.post
    try:
        compat = getter(_models_url(endpoint), timeout=timeout)
        compat.raise_for_status()
        compat_body = compat.json()
        ids = {str(item.get("id")) for item in compat_body.get("data", [])}
    except Exception as exc:  # pragma: no cover - exercised through CLI integration
        return _skipped_identity(config, f"model_list_failed: {exc}")
    if config.endpoint_model not in ids:
        return _skipped_identity(config, f"model_not_loaded: {config.endpoint_model}")

    native = _native_model_metadata(endpoint, config.endpoint_model, getter=getter, timeout=timeout)
    try:
        body = {"model": config.endpoint_model, "input": ["identity probe"]}
        response = poster(endpoint, json=body, timeout=timeout)
        response.raise_for_status()
        probe = response.json()
        response_model = str(probe.get("model") or config.endpoint_model)
        if response_model != config.endpoint_model:
            return _skipped_identity(config, f"response_model_mismatch: {response_model}")
        data = probe.get("data") or []
        dim = len(data[0]["embedding"]) if data else None
    except Exception as exc:  # pragma: no cover - exercised through CLI integration
        return _skipped_identity(config, f"embed_probe_failed: {exc}")

    quant = None
    if isinstance(native.get("quantization"), dict):
        quant = native["quantization"].get("name")
    return EmbeddingModelIdentity(
        alias=config.alias,
        endpoint_model=config.endpoint_model,
        model_version=config.model_version,
        query_prefix=config.query_prefix,
        passage_prefix=config.passage_prefix,
        embedding_dim=dim,
        hf_repo=_hf_repo_from_native(native),
        quant=quant,
        display_name=native.get("display_name"),
        context_length=_context_length_from_native(native),
    )


def _skipped_identity(config: EmbeddingModelConfig, reason: str) -> EmbeddingModelIdentity:
    return EmbeddingModelIdentity(
        alias=config.alias,
        endpoint_model=config.endpoint_model,
        model_version=config.model_version,
        query_prefix=config.query_prefix,
        passage_prefix=config.passage_prefix,
        embedding_dim=None,
        status="model_unavailable",
        skipped_with_reason=reason,
    )


def _models_url(endpoint: str) -> str:
    parsed = urlparse(endpoint)
    path = parsed.path
    if path.endswith("/embeddings"):
        path = path[: -len("/embeddings")] + "/models"
    else:
        path = "/v1/models"
    return urlunparse(parsed._replace(path=path, query="", fragment=""))


def _native_models_url(endpoint: str) -> str:
    parsed = urlparse(endpoint)
    return urlunparse(parsed._replace(path="/api/v1/models", query="", fragment=""))


def _native_model_metadata(
    endpoint: str,
    endpoint_model: str,
    *,
    getter: Callable[..., Any],
    timeout: int,
) -> dict[str, Any]:
    try:
        response = getter(_native_models_url(endpoint), timeout=timeout)
        response.raise_for_status()
        body = response.json()
    except Exception:
        return {}
    for item in body.get("models", []):
        loaded_ids = {
            str(instance.get("id"))
            for instance in item.get("loaded_instances", [])
            if isinstance(instance, dict)
        }
        if item.get("key") == endpoint_model or endpoint_model in loaded_ids:
            return dict(item)
    return {}


def _hf_repo_from_native(native: dict[str, Any]) -> str | None:
    publisher = native.get("publisher")
    key = native.get("key")
    if publisher and key and "/" not in str(key):
        return f"{publisher}/{key}"
    return str(key) if key else None


def _context_length_from_native(native: dict[str, Any]) -> int | None:
    instances = native.get("loaded_instances") or []
    for instance in instances:
        if isinstance(instance, dict):
            config = instance.get("config") or {}
            if config.get("context_length") is not None:
                return int(config["context_length"])
    if native.get("max_context_length") is not None:
        return int(native["max_context_length"])
    return None


def union_ranges(units: Iterable[TextUnit]) -> list[tuple[int, int]]:
    ranges = sorted((unit.start, unit.end) for unit in units if unit.end > unit.start)
    merged: list[tuple[int, int]] = []
    for start, end in ranges:
        if not merged or start > merged[-1][1]:
            merged.append((start, end))
        else:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
    return merged


def span_in_ranges(start: int, end: int, ranges: Iterable[tuple[int, int]]) -> bool:
    return any(lo <= start and end <= hi for lo, hi in ranges)


def _split_units(text: str, pattern: re.Pattern[str]) -> list[TextUnit]:
    units: list[TextUnit] = []
    start = 0
    for match in pattern.finditer(text):
        _append_unit(units, text, start, match.start())
        start = match.end()
    _append_unit(units, text, start, len(text))
    return units


def _append_unit(units: list[TextUnit], text: str, start: int, end: int) -> None:
    raw = text[start:end]
    if not raw.strip():
        return
    leading = len(raw) - len(raw.lstrip())
    trailing = len(raw) - len(raw.rstrip())
    unit_start = start + leading
    unit_end = end - trailing
    if unit_end > unit_start:
        units.append(TextUnit(unit_start, unit_end, text[unit_start:unit_end]))


def _normalize_text(text: str) -> str:
    return " ".join(str(text or "").replace("\r\n", "\n").split())


def _cosine(a: list[float], b: list[float]) -> float:
    norm_a = math.sqrt(sum(value * value for value in a))
    norm_b = math.sqrt(sum(value * value for value in b))
    if norm_a == 0 or norm_b == 0:
        return -1.0
    return sum(x * y for x, y in zip(a, b, strict=True)) / (norm_a * norm_b)
