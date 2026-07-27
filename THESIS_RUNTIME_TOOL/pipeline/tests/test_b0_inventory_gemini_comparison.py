from __future__ import annotations

import json
from pathlib import Path

from pipeline.scripts.run_b0_inventory_gemini_comparison import (
    DEFAULT_DOCUMENT,
    MODEL_ID,
    build_envelope,
    scan_current_utc_gemini_usage,
)
from pipeline.scripts.run_chapter_registry_v2_gemini import _gemini_transport


RUNTIME_ROOT = Path(__file__).resolve().parents[2]
DESIGN_DOC = RUNTIME_ROOT.parent / "design" / "LITERARY_PROMPT_DESIGN.md"
CKEY_MODEL_ID = "vuduythanh2023/gemini-3.5-flash"


def test_b0_gemini_envelope_pins_schema_and_disables_thinking() -> None:
    envelope, request, _chapter, _inventory, response_schema = build_envelope(
        stage="b0",
        document_path=DEFAULT_DOCUMENT,
        design_doc=DESIGN_DOC,
        chapter_id="wh_ch01",
        inventory_path=None,
    )
    assert envelope["model_contract"]["model_id"] == MODEL_ID
    assert envelope["model_contract"]["thinking_budget"] == 0
    assert envelope["response_schema_hash"] == request.response_schema_hash
    assert response_schema["additionalProperties"] is False
    assert envelope["gold_access_policy"].startswith("POST_RESPONSE")


def test_b0_gemini_envelope_seals_source_qualified_model_id() -> None:
    envelope, _request, _chapter, _inventory, _response_schema = build_envelope(
        stage="b0",
        document_path=DEFAULT_DOCUMENT,
        design_doc=DESIGN_DOC,
        chapter_id="wh_ch01",
        inventory_path=None,
        model_id=CKEY_MODEL_ID,
    )
    assert envelope["model_contract"]["model_id"] == CKEY_MODEL_ID


def test_b0_envelope_seals_openai_compatible_provider() -> None:
    envelope, _request, _chapter, _inventory, _response_schema = build_envelope(
        stage="b0",
        document_path=DEFAULT_DOCUMENT,
        design_doc=DESIGN_DOC,
        chapter_id="wh_ch01",
        inventory_path=None,
        model_id="gpt-5.4",
        provider_id="openai",
    )
    assert envelope["provider"] == "openai"
    assert envelope["model_contract"]["model_id"] == "gpt-5.4"


def test_gemini_usage_scan_keeps_physical_buckets_separate(tmp_path: Path) -> None:
    day = __import__("datetime").datetime.now(__import__("datetime").UTC).date().isoformat()
    for index, bucket in enumerate(("gemini-free-row1-v1", "gemini-free-row5-v2"), 1):
        path = tmp_path / f"call-{index}" / "raw_result.json"
        path.parent.mkdir(parents=True)
        path.write_text(
            json.dumps(
                {
                    "model": MODEL_ID,
                    "completed_at": f"{day}T01:00:0{index}+00:00",
                    "quota_bucket_id": bucket,
                    "cache_key": "same-request-across-physical-keys",
                    "from_cache": False,
                    "usage": {
                        "prompt_tokens": 10,
                        "completion_tokens": 5,
                        "reasoning_tokens": 0,
                    },
                }
            ),
            encoding="utf-8",
        )
    report = scan_current_utc_gemini_usage(roots=[tmp_path])
    assert report["usage_by_bucket"] == {
        "gemini-free-row1-v1": 15,
        "gemini-free-row5-v2": 15,
    }
    assert report["calls_by_bucket_model"] == {
        f"gemini-free-row1-v1|{MODEL_ID}": 1,
        f"gemini-free-row5-v2|{MODEL_ID}": 1,
    }


def test_gemini_usage_scan_accepts_profile_declared_bucket(tmp_path: Path) -> None:
    day = __import__("datetime").datetime.now(__import__("datetime").UTC).date().isoformat()
    path = tmp_path / "ckey" / "raw_result.json"
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps(
            {
                "model": MODEL_ID,
                "completed_at": f"{day}T01:00:00+00:00",
                "quota_bucket_id": "ckey-account-v1",
                "cache_key": "ckey-call",
                "from_cache": False,
                "usage": {
                    "prompt_tokens": 20,
                    "completion_tokens": 10,
                    "reasoning_tokens": 0,
                },
            }
        ),
        encoding="utf-8",
    )
    report = scan_current_utc_gemini_usage(
        roots=[tmp_path],
        allowed_bucket_ids=["ckey-account-v1"],
    )
    assert report["usage_by_bucket"] == {"ckey-account-v1": 30}
    assert report["unknown_bucket_rows"] == []


def test_gemini_transport_pins_profile_base_url(monkeypatch) -> None:
    from google import genai

    captured = {}

    class FakeClient:
        pass

    def fake_client(**kwargs):
        captured.update(kwargs)
        return FakeClient()

    monkeypatch.setattr(genai, "Client", fake_client)
    _gemini_transport(
        api_key="sk-" + "x" * 40,
        response_json_schema={"type": "object"},
        timeout_ms=120_000,
        base_url="https://api.xah.io",
    )
    assert "api.xah.io" in repr(captured["http_options"])
    assert captured["http_options"].timeout == 120_000
