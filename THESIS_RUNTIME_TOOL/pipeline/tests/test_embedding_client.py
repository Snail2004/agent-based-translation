from __future__ import annotations

import pytest

from pipeline.agents.embedding_client import EmbeddingClient, EmbeddingConfig


class FakeEmbeddingTransport:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def __call__(self, **kwargs):
        texts = list(kwargs["input"])
        dimensions = int(kwargs["dimensions"])
        self.calls.append({**kwargs, "input": texts})
        return {
            "data": [
                {"index": index, "embedding": _vector_for(text, dimensions)}
                for index, text in enumerate(texts)
            ],
            "usage": {"prompt_tokens": len(texts) * 10},
        }


def _config(**overrides) -> EmbeddingConfig:
    data = {
        "model": "text-embedding-3-large",
        "dimensions": 4,
        "batch_size": 64,
        "pricing": {"input": 0.13},
    }
    data.update(overrides)
    return EmbeddingConfig(**data)


def _vector_for(text: str, dimensions: int) -> list[float]:
    base = sum(ord(char) for char in text)
    return [float((base + index) % 17) for index in range(dimensions)]


def test_embed_cache_per_text(tmp_path):
    transport = FakeEmbeddingTransport()
    client = EmbeddingClient(
        _config(),
        tmp_path / "embedding_cache.sqlite3",
        transport=transport,
    )

    first = client.embed(["alpha", "beta"])
    second = client.embed(["beta", "gamma"])

    assert len(transport.calls) == 2
    assert transport.calls[0]["input"] == ["alpha", "beta"]
    assert transport.calls[1]["input"] == ["gamma"]
    assert first[1] == second[0]
    assert second[1] == _vector_for("gamma", 4)
    assert client.session_usage.cache_hits == 1
    assert client.session_usage.calls == 2
    assert client.get_usage_today()["total_tokens"] == 30


def test_embed_batching(tmp_path):
    transport = FakeEmbeddingTransport()
    client = EmbeddingClient(
        _config(batch_size=64),
        tmp_path / "embedding_cache.sqlite3",
        transport=transport,
    )

    texts = [f"text {index}" for index in range(130)]
    vectors = client.embed(texts)

    assert len(vectors) == 130
    assert [len(call["input"]) for call in transport.calls] == [64, 64, 2]
    assert transport.calls[0]["model"] == "text-embedding-3-large"
    assert transport.calls[0]["dimensions"] == 4


def test_model_pin():
    with pytest.raises(ValueError, match="pinned"):
        EmbeddingConfig(model="latest", dimensions=4)

    with pytest.raises(ValueError, match="pinned"):
        EmbeddingConfig(model="text-embedding-3-small", dimensions=4)
