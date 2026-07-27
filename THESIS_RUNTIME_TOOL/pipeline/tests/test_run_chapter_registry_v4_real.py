from __future__ import annotations

import json
from pathlib import Path
import sqlite3

import pytest

from pipeline.literary.checkpoint import canonical_hash
from pipeline.scripts.run_chapter_registry_v4_real import (
    DEFAULT_DESIGN_DOC,
    DEFAULT_DOCUMENT,
    DEFAULT_FROZEN_DB,
    RegistryV4RunError,
    build_parser,
    build_run_envelope,
    draft_semantic_config_v4,
    draft_transport_config_v4,
    run_dry_render,
    run_live_canary,
    scan_current_utc_usage,
)


def test_v4_live_arm_pins_requested_models_and_strong_key_order() -> None:
    semantic = draft_semantic_config_v4()
    transport = draft_transport_config_v4(semantic)
    assert semantic.b0_model_id == "gpt-5.4"
    assert semantic.b1_model_id == "gpt-5.4-mini"
    assert semantic.auditor_model_id == "gpt-5.4"
    assert transport.role_quota_gate_ids["b0"][0] == "openai-row2-gpt54"
    assert transport.role_quota_gate_ids["auditor"][0] == "openai-row2-gpt54"
    assert transport.role_quota_gate_ids["b1"] == (
        "openai-row2-mini",
        "openai-row1-mini",
    )


def test_v4_runner_exposes_bounded_b0_only_live_mode() -> None:
    args = build_parser().parse_args(["b0-live", "--output-dir", "unused"])
    assert args.mode == "b0-live"


def test_v4_dry_render_is_zero_api_and_seals_real_source(tmp_path: Path) -> None:
    output = tmp_path / "dry"
    report = run_dry_render(
        document_path=DEFAULT_DOCUMENT,
        design_doc=DEFAULT_DESIGN_DOC,
        output_dir=output,
        frozen_db=DEFAULT_FROZEN_DB,
    )
    assert report["status"] == "dry_render_only_no_api"
    assert report["window_count"] > 0
    assert report["request_metrics"]["b0"]["calls"] == 1
    assert report["request_metrics"]["b1"]["calls"] == report["window_count"]
    assert report["synthetic_auditor_calls"] == 0
    assert not (output / "calls").exists()
    envelope = next(output.glob("run_envelope_*.json"))
    stored = json.loads(envelope.read_text(encoding="utf-8"))
    assert stored["chapter_id"] == "wh_ch01"
    assert stored["semantic_config"]["b1_model_id"] == "gpt-5.4-mini"


def test_usage_preflight_deduplicates_raw_and_cache_rows(tmp_path: Path) -> None:
    cache_key = "cache-one"
    run_root = tmp_path / "reports" / "run"
    raw_dir = run_root / "calls" / "one" / "attempt_01"
    raw_dir.mkdir(parents=True)
    (raw_dir / "raw_result.json").write_text(
        json.dumps(
            {
                "completed_at": "2099-01-01T01:00:00+00:00",
                "from_cache": False,
                "model": "gpt-5.4",
                "quota_bucket_id": "openai-row2",
                "cache_key": cache_key,
                "safe_response_headers": {"x-request-id": "req-one"},
                "usage": {"prompt_tokens": 100, "completion_tokens": 20},
            }
        ),
        encoding="utf-8",
    )
    cache = run_root / "cache" / "openai-row2" / "b0.sqlite3"
    cache.parent.mkdir(parents=True)
    with sqlite3.connect(cache) as db:
        db.execute(
            """
            CREATE TABLE llm_call_cache (
              cache_key TEXT, model TEXT, usage_json TEXT, created_at TEXT
            )
            """
        )
        db.execute(
            "INSERT INTO llm_call_cache VALUES (?, ?, ?, ?)",
            (
                cache_key,
                "gpt-5.4",
                json.dumps({"prompt_tokens": 100, "completion_tokens": 20}),
                "2099-01-01 01:00:00",
            ),
        )
    # Rewrite the synthetic dates to the runtime UTC day without patching the scanner.
    from datetime import UTC, datetime

    today = datetime.now(UTC).date().isoformat()
    raw = json.loads((raw_dir / "raw_result.json").read_text(encoding="utf-8"))
    raw["completed_at"] = f"{today}T01:00:00+00:00"
    (raw_dir / "raw_result.json").write_text(json.dumps(raw), encoding="utf-8")
    with sqlite3.connect(cache) as db:
        db.execute("UPDATE llm_call_cache SET created_at = ?", (f"{today} 01:00:00",))
    report = scan_current_utc_usage(roots=[tmp_path])
    assert report["unique_call_count"] == 1
    assert report["usage_by_bucket_model"]["openai-row2|gpt-5.4"] == 120
    assert report["unknown_bucket_rows"] == []


def test_usage_preflight_counts_independent_real_calls_with_same_cache_key(
    tmp_path: Path,
) -> None:
    from datetime import UTC, datetime

    today = datetime.now(UTC).date().isoformat()
    for index, tokens in enumerate((120, 140), 1):
        raw_dir = (
            tmp_path
            / "reports"
            / f"run-{index}"
            / "calls"
            / "one"
            / "attempt_01"
        )
        raw_dir.mkdir(parents=True)
        (raw_dir / "raw_result.json").write_text(
            json.dumps(
                {
                    "completed_at": f"{today}T0{index}:00:00+00:00",
                    "from_cache": False,
                    "model": "gpt-5.4",
                    "quota_bucket_id": "openai-row2",
                    "cache_key": "same-request-content",
                    "safe_response_headers": {"x-request-id": f"req-{index}"},
                    "usage": {"prompt_tokens": 100, "completion_tokens": tokens - 100},
                }
            ),
            encoding="utf-8",
        )

    report = scan_current_utc_usage(roots=[tmp_path])
    assert report["unique_call_count"] == 2
    assert report["calls_by_bucket_model"]["openai-row2|gpt-5.4"] == 2
    assert report["usage_by_bucket_model"]["openai-row2|gpt-5.4"] == 260


def test_live_hash_mismatch_halts_before_credentials_or_transport(tmp_path: Path) -> None:
    output = tmp_path / "live"
    with pytest.raises(RegistryV4RunError, match="approved envelope mismatch"):
        run_live_canary(
            document_path=DEFAULT_DOCUMENT,
            design_doc=DEFAULT_DESIGN_DOC,
            output_dir=output,
            frozen_db=DEFAULT_FROZEN_DB,
            approved_envelope_hash="wrong",
            key_paths={
                "openai-row1": tmp_path / "missing-key-1.txt",
                "openai-row2": tmp_path / "missing-key-2.txt",
            },
            usage_roots=[tmp_path],
            stop_after_b0=True,
        )
    assert output.is_dir()
    assert list(output.iterdir()) == []


def test_envelope_is_content_addressed() -> None:
    first, _, _ = build_run_envelope(
        document_path=DEFAULT_DOCUMENT,
        design_doc=DEFAULT_DESIGN_DOC,
        chapter_id="wh_ch01",
    )
    body = {key: value for key, value in first.items() if key != "envelope_hash"}
    assert first["envelope_hash"] == canonical_hash(body)
