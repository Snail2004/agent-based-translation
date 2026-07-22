from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
import sqlite3

import pytest

import pipeline.eval.google_translate_baseline_v1 as google_baseline
from pipeline.eval.contracts_v1 import ContractValidationError
from pipeline.eval.d2l_input_v1 import seal_d2l_evaluation_input
from pipeline.eval.google_translate_baseline_v1 import (
    GoogleTranslateBaselineError,
    GoogleTranslateConfigV1,
    GoogleTranslateTransportError,
    build_google_translate_plan_v1,
    build_google_translate_source_input_v1,
    execute_google_translate_plan_v1,
    parse_google_translated_html_v1,
    validate_google_translate_capture_v1,
    validate_google_translate_checkpoint_v1,
    validate_google_translate_plan_v1,
    validate_google_translate_source_input_v1,
)


FIXTURE = (
    Path(__file__).parent
    / "fixtures"
    / "evaluation_v1"
    / "d2l_input_valid.json"
)
NOW = "2026-07-21T12:00:00Z"
COMMIT = "c" * 40


def _package() -> dict:
    payload = json.loads(FIXTURE.read_text(encoding="utf-8"))
    payload["blocks"] = [
        {
            "block_id": "b001",
            "chapter_id": "chapter-intro",
            "order_index": 0,
            "block_type": "paragraph",
            "source_text": "A tensor stores numerical data.",
            "admission": "translate",
        },
        {
            "block_id": "b002",
            "chapter_id": "chapter-intro",
            "order_index": 1,
            "block_type": "code",
            "source_text": "tensor.shape",
            "admission": "preserve",
        },
        {
            "block_id": "b003",
            "chapter_id": "chapter-intro",
            "order_index": 2,
            "block_type": "paragraph",
            "source_text": "The model generalizes to new examples.",
            "admission": "translate",
        },
    ]
    payload["translations"] = [
        {
            "arm_id": "s1",
            "block_id": "b001",
            "status": "translated",
            "target_text": "Tensor luu tru du lieu so.",
            "error_code": None,
            "source_artifact_id": "artifact-s1",
        },
        {
            "arm_id": "s1",
            "block_id": "b002",
            "status": "passthrough",
            "target_text": "tensor.shape",
            "error_code": None,
            "source_artifact_id": "artifact-s1",
        },
        {
            "arm_id": "s1",
            "block_id": "b003",
            "status": "translated",
            "target_text": "Mo hinh khai quat hoa.",
            "error_code": None,
            "source_artifact_id": "artifact-s1",
        },
    ]
    payload["runtime_terms"] = []
    payload["injection_rows"] = []
    return seal_d2l_evaluation_input(payload)


def _config(**overrides) -> GoogleTranslateConfigV1:
    values = {
        "chapter_id": "chapter-intro",
        "max_request_characters": 250,
        "hard_source_character_cap": 1_000,
        "timeout_seconds": 10,
        "key_bucket_id": "test-google-key-bucket",
    }
    values.update(overrides)
    return GoogleTranslateConfigV1(**values)


def _plan(package: dict, **config_overrides) -> dict:
    return build_google_translate_plan_v1(
        package,
        package_file_sha256="8" * 64,
        config=_config(**config_overrides),
        logical_run_id="google-nmt-test",
        attempt_run_id="google-nmt-test-attempt-1",
        created_at=NOW,
        producer_code_commit=COMMIT,
    )


def _fake_transport(request_text: str, _config: GoogleTranslateConfigV1) -> dict:
    translated = request_text.replace(
        "A tensor stores numerical data.", "Tensor luu tru du lieu so."
    ).replace(
        "The model generalizes to new examples.",
        "Mo hinh khai quat hoa cho cac vi du moi.",
    )
    return {"data": {"translations": [{"translatedText": translated}]}}


def _source_database(path: Path) -> Path:
    connection = sqlite3.connect(path)
    try:
        connection.executescript(
            """
            CREATE TABLE documents (
              doc_id TEXT PRIMARY KEY,
              source_lang TEXT,
              target_lang TEXT
            );
            CREATE TABLE blocks (
              block_id TEXT PRIMARY KEY,
              doc_id TEXT NOT NULL,
              order_index INTEGER NOT NULL,
              block_type TEXT,
              chapter_id TEXT,
              text TEXT,
              original_text TEXT
            );
            INSERT INTO documents VALUES ('d2l', 'en', 'vi');
            INSERT INTO blocks VALUES
              ('b001', 'd2l', 1, 'heading', 'chapter-intro', 'Heading', 'Heading'),
              ('b002', 'd2l', 2, 'prose', 'chapter-intro', 'A tensor stores numerical data.', 'A tensor stores numerical data.'),
              ('b003', 'd2l', 3, 'code', 'chapter-intro', 'tensor.shape', 'tensor.shape');
            """
        )
        connection.commit()
    finally:
        connection.close()
    return path


def test_plan_is_closed_exact_cover_and_breaks_at_preserve_blocks() -> None:
    package = _package()
    plan = _plan(package)

    assert plan["coverage_plan"] == {
        "source_block_count": 3,
        "translate_block_count": 2,
        "preserve_block_count": 1,
        "chunk_count": 2,
        "planned_source_character_count": sum(
            row["request_character_count"] for row in plan["chunks"]
        ),
    }
    assert [
        [ref["block_id"] for ref in chunk["block_refs"]]
        for chunk in plan["chunks"]
    ] == [["b001"], ["b003"]]
    assert plan["profile"]["transport_attempts_per_chunk"] == 1
    assert plan["profile"]["semantic_retries_per_chunk"] == 0
    assert plan["profile"]["fallback_policy"] == "none"

    tampered = copy.deepcopy(plan)
    tampered["unexpected"] = True
    with pytest.raises(ContractValidationError, match="keys"):
        validate_google_translate_plan_v1(tampered)


def test_source_only_input_is_sealed_read_only_and_reuses_normal_runner(
    tmp_path: Path,
) -> None:
    database = _source_database(tmp_path / "source.sqlite3")
    before = hashlib.sha256(database.read_bytes()).hexdigest()
    source = build_google_translate_source_input_v1(
        database,
        chapter_id="chapter-intro",
        created_at=NOW,
        producer_code_commit=COMMIT,
    )
    assert hashlib.sha256(database.read_bytes()).hexdigest() == before
    assert [row["admission"] for row in source["blocks"]] == [
        "translate",
        "translate",
        "preserve",
    ]
    assert validate_google_translate_source_input_v1(source) == source

    plan = _plan(source)
    paths = execute_google_translate_plan_v1(
        source,
        plan_payload=plan,
        output_root=tmp_path / "run",
        transport=_fake_transport,
        resume=False,
    )
    capture = validate_google_translate_capture_v1(
        json.loads(paths.capture_path.read_text(encoding="utf-8"))
    )
    assert capture["coverage"] == {
        "source_block_count": 3,
        "translated_count": 2,
        "preserved_count": 1,
        "missing_count": 0,
        "failed_count": 0,
    }


def test_source_only_input_rejects_admission_and_hash_tampering(tmp_path: Path) -> None:
    source = build_google_translate_source_input_v1(
        _source_database(tmp_path / "source.sqlite3"),
        chapter_id="chapter-intro",
        created_at=NOW,
        producer_code_commit=COMMIT,
    )
    tampered = copy.deepcopy(source)
    tampered["blocks"][0]["admission"] = "preserve"
    tampered = google_baseline._seal(tampered, ("integrity", "package_sha256"))
    with pytest.raises(ContractValidationError, match="admission drift"):
        validate_google_translate_source_input_v1(tampered)

    unknown = copy.deepcopy(source)
    unknown["gold_reference"] = "forbidden"
    with pytest.raises(ContractValidationError, match="keys"):
        validate_google_translate_source_input_v1(unknown)


def test_plan_fails_before_api_when_hard_cap_is_too_small() -> None:
    with pytest.raises(GoogleTranslateBaselineError, match="exceed hard cap"):
        _plan(_package(), hard_source_character_cap=10)


def test_html_parser_preserves_block_mapping_and_rejects_reordering() -> None:
    payload = (
        '<div data-eval-block="b000001">Dong mot.<br> Dong hai.</div>'
        '<div data-eval-block="b000002">Dong ba.</div>'
    )
    assert parse_google_translated_html_v1(
        payload, expected_markers=["b000001", "b000002"]
    ) == {
        "b000001": "Dong mot.\nDong hai.",
        "b000002": "Dong ba.",
    }
    with pytest.raises(GoogleTranslateBaselineError, match="markers/order"):
        parse_google_translated_html_v1(
            payload, expected_markers=["b000002", "b000001"]
        )


def test_fake_transport_run_is_exact_cover_private_and_idempotent_resume(
    tmp_path: Path,
) -> None:
    package = _package()
    plan = _plan(package)
    calls: list[str] = []

    def transport(text: str, config: GoogleTranslateConfigV1) -> dict:
        calls.append(text)
        return _fake_transport(text, config)

    paths = execute_google_translate_plan_v1(
        package,
        plan_payload=plan,
        output_root=tmp_path / "run",
        transport=transport,
        resume=False,
    )
    assert len(calls) == 2
    capture = validate_google_translate_capture_v1(
        json.loads(paths.capture_path.read_text(encoding="utf-8"))
    )
    assert capture["authority"] == {
        "artifact_kind": "evaluation_private_baseline_capture",
        "public_translation_artifact": False,
        "requires_producer_promotion": True,
    }
    assert [(row["block_id"], row["status"]) for row in capture["translations"]] == [
        ("b001", "translated"),
        ("b002", "preserved"),
        ("b003", "translated"),
    ]
    assert capture["translations"][1]["target_text"] == "tensor.shape"
    checkpoint = validate_google_translate_checkpoint_v1(
        json.loads(paths.checkpoint_path.read_text(encoding="utf-8"))
    )
    assert checkpoint["status"] == "complete"
    assert checkpoint["usage"]["physical_request_count"] == 2
    assert checkpoint["usage"]["provider_reported_cost_usd"] is None

    execute_google_translate_plan_v1(
        package,
        plan_payload=plan,
        output_root=tmp_path / "run",
        transport=lambda *_args: pytest.fail("complete resume must call no API"),
        resume=True,
    )


def test_unknown_outcome_is_checkpointed_and_never_retried_automatically(
    tmp_path: Path,
) -> None:
    package = _package()
    plan = _plan(package)

    def timeout(_text: str, _config: GoogleTranslateConfigV1) -> dict:
        raise GoogleTranslateTransportError(
            "timeout",
            error_code="timeout_unknown_outcome",
            outcome_known=False,
        )

    with pytest.raises(GoogleTranslateTransportError):
        execute_google_translate_plan_v1(
            package,
            plan_payload=plan,
            output_root=tmp_path / "run",
            transport=timeout,
            resume=False,
        )
    checkpoint_path = tmp_path / "run" / "checkpoint.json"
    checkpoint = validate_google_translate_checkpoint_v1(
        json.loads(checkpoint_path.read_text(encoding="utf-8"))
    )
    assert checkpoint["status"] == "halted_pending_unknown"
    assert checkpoint["attempts"][0]["status"] == "pending_unknown"
    assert checkpoint["usage"]["reserved_source_character_count"] > 0

    with pytest.raises(GoogleTranslateBaselineError, match="unresolved physical attempt"):
        execute_google_translate_plan_v1(
            package,
            plan_payload=plan,
            output_root=tmp_path / "run",
            transport=lambda *_args: pytest.fail("must not retry"),
            resume=True,
        )


def test_response_contract_failure_is_known_failed_and_key_material_is_absent(
    tmp_path: Path,
) -> None:
    package = _package()
    plan = _plan(package)
    secret = "not-a-real-key-secret-sentinel"

    def bad_response(_text: str, _config: GoogleTranslateConfigV1) -> dict:
        assert secret
        return {"data": {"translations": [{"translatedText": "no envelopes"}]}}

    with pytest.raises(GoogleTranslateBaselineError, match="local validation"):
        execute_google_translate_plan_v1(
            package,
            plan_payload=plan,
            output_root=tmp_path / "run",
            transport=bad_response,
            resume=False,
        )
    checkpoint = validate_google_translate_checkpoint_v1(
        json.loads((tmp_path / "run" / "checkpoint.json").read_text(encoding="utf-8"))
    )
    assert checkpoint["status"] == "halted_failed"
    assert checkpoint["attempts"][0]["status"] == "failed_known"
    assert secret.encode("utf-8") not in b"".join(
        path.read_bytes() for path in (tmp_path / "run").rglob("*.json")
    )


def test_resume_recovers_valid_response_written_before_checkpoint_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    package = _package()
    plan = _plan(package)
    original_write_checkpoint = google_baseline._write_checkpoint
    write_count = 0

    def fail_first_post_response_checkpoint(path: Path, payload: dict) -> None:
        nonlocal write_count
        write_count += 1
        if write_count == 3:
            raise PermissionError("simulated Windows sharing violation")
        original_write_checkpoint(path, payload)

    monkeypatch.setattr(
        google_baseline, "_write_checkpoint", fail_first_post_response_checkpoint
    )
    with pytest.raises(PermissionError, match="sharing violation"):
        execute_google_translate_plan_v1(
            package,
            plan_payload=plan,
            output_root=tmp_path / "run",
            transport=_fake_transport,
            resume=False,
        )
    checkpoint = validate_google_translate_checkpoint_v1(
        json.loads((tmp_path / "run" / "checkpoint.json").read_text(encoding="utf-8"))
    )
    assert checkpoint["attempts"][0]["status"] == "pending_unknown"
    assert (tmp_path / "run" / "responses" / f"{plan['chunks'][0]['chunk_id']}.json").exists()

    monkeypatch.setattr(google_baseline, "_write_checkpoint", original_write_checkpoint)
    resumed_calls: list[str] = []

    def remaining_transport(text: str, config: GoogleTranslateConfigV1) -> dict:
        resumed_calls.append(text)
        return _fake_transport(text, config)

    paths = execute_google_translate_plan_v1(
        package,
        plan_payload=plan,
        output_root=tmp_path / "run",
        transport=remaining_transport,
        resume=True,
    )
    assert len(resumed_calls) == 1
    assert "b000002" in resumed_calls[0]
    receipt = (
        tmp_path
        / "run"
        / "recoveries"
        / f"{plan['chunks'][0]['chunk_id']}.json"
    )
    assert json.loads(receipt.read_text(encoding="utf-8"))["provider_calls_added"] == 0
    assert validate_google_translate_checkpoint_v1(
        json.loads(paths.checkpoint_path.read_text(encoding="utf-8"))
    )["status"] == "complete"


def test_atomic_replace_retries_transient_windows_sharing_violation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_replace = google_baseline.os.replace
    attempts = 0

    def flaky_replace(source: Path, target: Path) -> None:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise PermissionError("sharing violation")
        real_replace(source, target)

    monkeypatch.setattr(google_baseline.os, "replace", flaky_replace)
    target = tmp_path / "atomic.json"
    google_baseline._write_json_atomic(target, {"ok": True})
    assert attempts == 2
    assert json.loads(target.read_text(encoding="utf-8")) == {"ok": True}
