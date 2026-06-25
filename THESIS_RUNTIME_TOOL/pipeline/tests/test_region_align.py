from __future__ import annotations

from pipeline.eval.region_align import (
    EmbeddingCacheClient,
    EmbeddingModelConfig,
    cache_key,
    parse_model_specs,
    preflight_embedding_model,
    split_sentences,
    top_k_target_sentences,
)


class _Response:
    def __init__(self, inputs: list[str] | None = None, *, body: dict | None = None) -> None:
        self.inputs = inputs or []
        self._body = body

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict:
        if self._body is not None:
            return self._body
        return {
            "model": "text-embedding-labse",
            "data": [{"embedding": _vector(text)} for text in self.inputs],
        }


def _vector(text: str) -> list[float]:
    if "user" in text or "nguoi dung" in text:
        return [1.0, 0.0]
    return [0.0, 1.0]


def test_cache_key_stable_after_whitespace_normalization() -> None:
    assert cache_key("a", "m", "v", "p", " user   ID ") == cache_key("a", "m", "v", "p", "user ID")
    assert cache_key("a", "m", "v", "p1", "user ID") != cache_key("a", "m", "v", "p2", "user ID")


def test_embedding_cache_hits_disk_on_second_client(tmp_path) -> None:
    calls: list[list[str]] = []

    def post(_endpoint: str, *, json: dict, timeout: int) -> _Response:
        del timeout
        calls.append(list(json["input"]))
        return _Response(list(json["input"]))

    client = EmbeddingCacheClient(cache_dir=tmp_path, post=post, model_version="v")
    assert client.embed(["user ID"])[0] == [1.0, 0.0]
    assert calls == [["user ID"]]

    def fail_post(*_args, **_kwargs):
        raise AssertionError("warm cache should not call the embedding endpoint")

    warm = EmbeddingCacheClient(cache_dir=tmp_path, post=fail_post, model_version="v")
    assert warm.embed(["user ID"])[0] == [1.0, 0.0]
    assert warm.stats()["cache_hits"] == 1
    assert warm.stats()["cache_misses"] == 0


def test_top_k_target_sentences_ranks_by_cosine(tmp_path) -> None:
    def post(_endpoint: str, *, json: dict, timeout: int) -> _Response:
        del timeout
        return _Response(list(json["input"]))

    client = EmbeddingCacheClient(cache_dir=tmp_path, post=post, model_version="v")
    targets = split_sentences("Mot khach hang bam nut. ID nguoi dung duoc lien ket.")
    ranked = top_k_target_sentences("user ID", targets, k=1, client=client)
    assert len(ranked) == 1
    assert ranked[0].unit.text == "ID nguoi dung duoc lien ket."
    assert ranked[0].score == 1.0


def test_e5_prefix_is_applied_before_cache_and_request(tmp_path) -> None:
    calls: list[list[str]] = []

    def post(_endpoint: str, *, json: dict, timeout: int) -> _Response:
        del timeout
        calls.append(list(json["input"]))
        return _Response(list(json["input"]))

    client = EmbeddingCacheClient(
        cache_dir=tmp_path,
        post=post,
        model_alias="e5",
        model_version="v",
        query_prefix="query: ",
        passage_prefix="passage: ",
    )
    targets = split_sentences("ID nguoi dung duoc lien ket.")
    top_k_target_sentences("user ID", targets, k=1, client=client)
    assert calls == [["query: user ID", "passage: ID nguoi dung duoc lien ket."]]


def test_parse_model_specs_uses_known_lmstudio_endpoint_for_short_alias() -> None:
    configs = parse_model_specs("bge-m3=bge-m3@Q8_0,e5=multilingual-e5-large@Q8_0")
    by_alias = {item.alias: item for item in configs}
    assert by_alias["bge-m3"].endpoint_model == "text-embedding-bge-m3"
    assert by_alias["e5"].endpoint_model == "text-embedding-multilingual-e5-large-instruct"
    assert by_alias["e5"].query_prefix == "query: "
    assert by_alias["e5"].passage_prefix == "passage: "


def test_preflight_records_real_identity() -> None:
    def get(url: str, *, timeout: int) -> _Response:
        del timeout
        if url.endswith("/api/v1/models"):
            return _Response(body={
            "models": [{
                "type": "embedding",
                "publisher": "gpustack",
                "key": "text-embedding-bge-m3",
                "display_name": "Bge M3",
                "quantization": {"name": "Q8_0"},
                "loaded_instances": [{"id": "text-embedding-bge-m3", "config": {"context_length": 8192}}],
            }]
            })
        return _Response(body={"data": [{"id": "text-embedding-bge-m3"}]})

    def post(_endpoint: str, *, json: dict, timeout: int) -> _Response:
        del timeout
        assert json["model"] == "text-embedding-bge-m3"
        return _Response(body={"model": "text-embedding-bge-m3", "data": [{"embedding": [0.1, 0.2, 0.3]}]})

    identity = preflight_embedding_model(
        endpoint="http://localhost:1234/v1/embeddings",
        config=EmbeddingModelConfig("bge-m3", "text-embedding-bge-m3", "gpustack/bge-m3-GGUF:Q8_0"),
        get=get,
        post=post,
    )
    assert identity.status == "available"
    assert identity.embedding_dim == 3
    assert identity.hf_repo == "gpustack/text-embedding-bge-m3"
    assert identity.quant == "Q8_0"
