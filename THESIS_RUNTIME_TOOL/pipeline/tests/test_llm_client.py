from __future__ import annotations

import pytest

from pipeline.agents.llm_client import (
    DailyQuotaExceeded,
    LLMClient,
    LLMTransportError,
)
from pipeline.agents.llm_config import LLMConfig


class FakeHTTPError(Exception):
    def __init__(self, status_code: int, message: str = "fake error") -> None:
        super().__init__(message)
        self.status_code = status_code


class FakeTransport:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def __call__(self, **kwargs):
        self.calls.append(kwargs)
        if not self.responses:
            raise AssertionError("FakeTransport has no response left")
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def _config(**overrides) -> LLMConfig:
    data = {
        "model": "gpt-5.4-mini",
        "temperature": 0.3,
        "seed": 20260612,
        "reasoning_effort": "minimal",
        "verbosity": "low",
        "max_output_tokens": 128,
        "daily_token_cap": 2_400_000,
        "pricing": {"input": 0.25, "cached_input": 0.025, "output": 2.0},
    }
    data.update(overrides)
    return LLMConfig(**data)


def _response(text: str = "Xin chao", *, prompt=10, cached=0, completion=5, reasoning=0):
    return {
        "choices": [{"message": {"content": text}}],
        "system_fingerprint": "fp_test",
        "usage": {
            "prompt_tokens": prompt,
            "prompt_tokens_details": {"cached_tokens": cached},
            "completion_tokens": completion,
            "completion_tokens_details": {"reasoning_tokens": reasoning},
        },
    }


def test_cache_hit_skips_transport(tmp_path):
    transport = FakeTransport([_response("cached result", prompt=20, completion=10)])
    client = LLMClient(_config(), tmp_path / "cache.sqlite3", transport=transport)
    messages = [{"role": "user", "content": "Translate one sentence."}]

    first = client.call(messages, tag="cache")
    usage_after_first = client.get_usage_today()
    second = client.call(messages, tag="cache")
    usage_after_second = client.get_usage_today()

    assert len(transport.calls) == 1
    assert transport.calls[0]["verbosity"] == "low"
    assert "tools" not in transport.calls[0]
    assert "stream" not in transport.calls[0]
    assert first.from_cache is False
    assert second.from_cache is True
    assert second.text == first.text
    assert second.cache_key == first.cache_key
    assert usage_after_first == usage_after_second


def test_cache_key_sensitivity(tmp_path):
    base = [{"role": "user", "content": "abc"}]
    base_client = LLMClient(
        _config(), tmp_path / "base.sqlite3", transport=FakeTransport([_response()])
    )
    base_key = base_client.call(base).cache_key

    seed_client = LLMClient(
        _config(seed=20260613),
        tmp_path / "seed.sqlite3",
        transport=FakeTransport([_response()]),
    )
    model_client = LLMClient(
        _config(model="gpt-5.4-mini-2026-06-12"),
        tmp_path / "model.sqlite3",
        transport=FakeTransport([_response()]),
    )
    message_client = LLMClient(
        _config(), tmp_path / "message.sqlite3", transport=FakeTransport([_response()])
    )
    format_client = LLMClient(
        _config(), tmp_path / "format.sqlite3", transport=FakeTransport([_response("{}")])
    )

    keys = {
        seed_client.call(base).cache_key,
        model_client.call(base).cache_key,
        message_client.call([{"role": "user", "content": "abd"}]).cache_key,
        format_client.call(base, response_format={"type": "json_object"}).cache_key,
    }

    assert len(keys) == 4
    assert base_key not in keys


def test_retry_backoff_429(tmp_path, monkeypatch):
    sleeps = []
    monkeypatch.setattr("pipeline.agents.llm_client.time.sleep", sleeps.append)
    monkeypatch.setattr("pipeline.agents.llm_client.random.uniform", lambda *_: 0)
    transport = FakeTransport(
        [FakeHTTPError(429), FakeHTTPError(429), _response("ok")]
    )
    client = LLMClient(_config(), tmp_path / "cache.sqlite3", transport=transport)

    result = client.call([{"role": "user", "content": "hello"}])

    assert result.text == "ok"
    assert len(transport.calls) == 3
    assert sleeps == [1, 2]


def test_no_retry_on_400(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "pipeline.agents.llm_client.time.sleep",
        lambda *_: pytest.fail("400 must not sleep/retry"),
    )
    transport = FakeTransport([FakeHTTPError(400)])
    client = LLMClient(_config(), tmp_path / "cache.sqlite3", transport=transport)

    with pytest.raises(LLMTransportError) as excinfo:
        client.call([{"role": "user", "content": "bad request"}])

    assert excinfo.value.status_code == 400
    assert len(transport.calls) == 1


def test_usage_and_quota(tmp_path):
    transport = FakeTransport(
        [
            _response("one", prompt=50, completion=10),
            _response("two", prompt=60, completion=20),
            _response("three", prompt=100, completion=100),
        ]
    )
    client = LLMClient(
        _config(daily_token_cap=150, max_output_tokens=1),
        tmp_path / "cache.sqlite3",
        transport=transport,
    )

    first = client.call([{"role": "user", "content": "first"}])
    second = client.call([{"role": "user", "content": "second"}])
    usage = client.get_usage_today()

    assert first.usage.total_billable_tokens == 60
    assert second.usage.total_billable_tokens == 80
    assert usage["total_tokens"] == 140
    assert usage["calls"] == 2

    cached = client.call([{"role": "user", "content": "first"}])
    assert cached.from_cache is True
    assert client.get_usage_today() == usage

    with pytest.raises(DailyQuotaExceeded):
        client.call([{"role": "user", "content": "third"}])
    assert len(transport.calls) == 2


def test_json_mode(tmp_path):
    transport = FakeTransport(
        [
            _response('{"ok": true}', prompt=5, completion=5),
            _response('{"ok":', prompt=5, completion=5),
        ]
    )
    client = LLMClient(_config(), tmp_path / "cache.sqlite3", transport=transport)

    good = client.call(
        [{"role": "user", "content": "json good"}],
        response_format={"type": "json_object"},
    )
    bad = client.call(
        [{"role": "user", "content": "json bad"}],
        response_format={"type": "json_object"},
    )

    assert good.parsed_json == {"ok": True}
    assert good.json_error is None
    assert bad.parsed_json is None
    assert bad.json_error is not None
    assert len(transport.calls) == 2


def test_cost_estimate(tmp_path):
    transport = FakeTransport(
        [_response("cost", prompt=1_000_000, cached=400_000, completion=100_000)]
    )
    client = LLMClient(_config(), tmp_path / "cache.sqlite3", transport=transport)

    result = client.call([{"role": "user", "content": "cost"}])

    expected = (600_000 / 1_000_000) * 0.25
    expected += (400_000 / 1_000_000) * 0.025
    expected += (100_000 / 1_000_000) * 2.0
    assert result.cost_usd == expected
