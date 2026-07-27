from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping

import pytest

from pipeline.agents.llm_client import LLMResult, LLMUsage
from pipeline.literary.checkpoint import canonical_hash, file_sha256
from pipeline.scripts import run_chapter_registry_v2_real as real_runner


REPO_ROOT = Path(__file__).resolve().parents[3]
DESIGN_DOC = REPO_ROOT / "design" / "LITERARY_PROMPT_DESIGN.md"
CONFIG = (
    REPO_ROOT
    / "THESIS_RUNTIME_TOOL"
    / "data"
    / "reports"
    / "literary_m4f_b1_prejoined_context_dry_20260714"
    / "run_config_f73034a1a00288ca.json"
)


def _document() -> dict[str, Any]:
    chapters = []
    for number, chapter_id in enumerate(real_runner.CHAPTER_IDS, 1):
        chapters.append(
            {
                "chapter_id": chapter_id,
                "blocks": [
                    {
                        "block_id": f"{chapter_id}_b001",
                        "order_index": 1,
                        "block_type": "heading",
                        "clean_text": f"Chapter {number}",
                    },
                    {
                        "block_id": f"{chapter_id}_b002",
                        "order_index": 2,
                        "block_type": "paragraph",
                        "clean_text": "Arden entered the hall and Bell answered quietly.",
                    },
                ],
            }
        )
    return {"document_id": "book-test", "chapters": chapters}


class FakeExecutor:
    def __init__(self, marker: str = "stable") -> None:
        self.marker = marker
        self.calls: list[str] = []

    @property
    def public_manifest(self) -> Mapping[str, Any]:
        return {"executor": "fake", "marker": self.marker}

    def execute(self, request: Any) -> real_runner.ExecutedRegistryCall:
        self.calls.append(str(request.role))
        if request.role == "b0":
            payload: dict[str, Any] = {
                "gist": "A short chapter orientation.",
                "narrator_hypotheses": [],
                "salient_surface_checklist": [],
            }
        elif request.role == "b1":
            payload = {
                "new_entities": [],
                "new_aliases": [],
                "new_glossary_items": [],
                "local_bindings": [],
                "tickets": [],
            }
        else:  # The empty-delta fixture must never create Auditor work.
            raise AssertionError("unexpected Auditor call")
        text = json.dumps(payload, ensure_ascii=False)
        result = LLMResult(
            text=text,
            parsed_json=payload,
            json_error=None,
            model=f"fake-{request.role}",
            system_fingerprint="fake-system",
            usage=LLMUsage(prompt_tokens=11, completion_tokens=3),
            cost_usd=0.0,
            latency_ms=1,
            from_cache=False,
            cache_key=canonical_hash({"request": request.to_dict()}),
        )
        return real_runner.ExecutedRegistryCall(
            result=result,
            quota_gate_id=f"fake-{request.role}-gate",
            quota_bucket_id="fake-bucket",
            credential_commitment="fake-commitment",
            safe_response_headers={},
            completed_at="2026-07-14T00:00:00+00:00",
        )


@pytest.fixture
def inputs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Path]:
    monkeypatch.setattr(real_runner, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(real_runner, "_git_head", lambda: "test-head")
    monkeypatch.setattr(
        real_runner,
        "_verify_frozen_db",
        lambda path, expected=real_runner.FROZEN_DB_SHA256: file_sha256(path).upper(),
    )
    document = tmp_path / "document.json"
    document.write_text(json.dumps(_document(), ensure_ascii=False), encoding="utf-8")
    design = tmp_path / "design.md"
    design.write_bytes(DESIGN_DOC.read_bytes())
    config = tmp_path / "run_config.json"
    config.write_bytes(CONFIG.read_bytes())
    frozen = tmp_path / "memory.sqlite3"
    frozen.write_bytes(b"frozen-test-db")
    return {
        "document": document,
        "design": design,
        "config": config,
        "frozen": frozen,
        "output": tmp_path / "run",
    }


def _run(
    paths: Mapping[str, Path],
    *,
    through: str,
    resume: bool,
    executor: FakeExecutor,
) -> dict[str, Any]:
    return real_runner.run_phase_c(
        document_path=paths["document"],
        design_doc=paths["design"],
        config_path=paths["config"],
        output_dir=paths["output"],
        frozen_db=paths["frozen"],
        through_chapter=through,
        executor=executor,
        resume=resume,
    )


def test_canary_then_resume_is_append_only_and_reconstructs_exact_usage(
    inputs: dict[str, Path],
) -> None:
    canary_executor = FakeExecutor()
    canary = _run(
        inputs,
        through="wh_ch01",
        resume=False,
        executor=canary_executor,
    )

    assert canary["status"] == "canary_completed"
    assert canary["completed_chapters"] == ["wh_ch01"]
    assert canary["usage"]["calls"] == 2
    assert canary_executor.calls == ["b0", "b1"]
    first_generation = canary["current_generation_id"]
    manifest_bytes = (inputs["output"] / "run_manifest.json").read_bytes()

    resume_executor = FakeExecutor()
    final = _run(
        inputs,
        through="wh_ch02",
        resume=True,
        executor=resume_executor,
    )

    assert final["status"] == "completed"
    assert final["completed_chapters"] == ["wh_ch01", "wh_ch02"]
    assert final["usage"]["calls"] == 4
    assert final["usage"]["api_calls"] == 4
    assert final["usage"]["prompt_tokens"] == 44
    assert final["usage"]["completion_tokens"] == 12
    assert final["usage"]["by_role"] == {
        "b0": {"calls": 2, "tokens": 28},
        "b1": {"calls": 2, "tokens": 28},
    }
    assert resume_executor.calls == ["b0", "b1"]
    assert final["current_generation_id"] != first_generation
    assert (inputs["output"] / "run_manifest.json").read_bytes() == manifest_bytes
    assert (inputs["output"] / "canary_run_report.json").is_file()
    assert (inputs["output"] / "final_run_report.json").is_file()
    transition = json.loads(
        (inputs["output"] / "chapters" / "wh_ch02" / "pointer_transition.json").read_text(
            encoding="utf-8"
        )
    )
    assert transition["before"] == first_generation
    assert transition["after"] == final["current_generation_id"]
    assert len(list((inputs["output"] / "calls").glob("*/request.json"))) == 4
    assert len(list((inputs["output"] / "calls").glob("*/attempt_01/raw_result.json"))) == 4
    usage, calls = real_runner._prior_quota_state(inputs["output"])
    assert usage == {"fake-bucket": 56}
    assert calls == {"fake-bucket": 4}


def test_fresh_run_refuses_nonempty_output(inputs: dict[str, Path]) -> None:
    inputs["output"].mkdir()
    sentinel = inputs["output"] / "keep.txt"
    sentinel.write_text("keep", encoding="utf-8")

    with pytest.raises(real_runner.PhaseCError, match="not empty"):
        _run(
            inputs,
            through="wh_ch01",
            resume=False,
            executor=FakeExecutor(),
        )

    assert sentinel.read_text(encoding="utf-8") == "keep"


def test_resume_rejects_executor_contract_drift(inputs: dict[str, Path]) -> None:
    _run(inputs, through="wh_ch01", resume=False, executor=FakeExecutor("first"))

    with pytest.raises(real_runner.PhaseCError, match="manifest differs"):
        _run(inputs, through="wh_ch02", resume=True, executor=FakeExecutor("changed"))


def test_frozen_db_guard_detects_any_byte_drift(tmp_path: Path) -> None:
    frozen = tmp_path / "memory.sqlite3"
    frozen.write_bytes(b"expected")
    expected = file_sha256(frozen)
    assert real_runner._verify_frozen_db(frozen, expected) == expected.upper()

    frozen.write_bytes(b"changed")
    with pytest.raises(real_runner.PhaseCError, match="hash drift"):
        real_runner._verify_frozen_db(frozen, expected)


def test_halt_report_is_append_only_and_redacts_credentials(tmp_path: Path) -> None:
    run_root = tmp_path / "run"
    run_root.mkdir()
    frozen = tmp_path / "memory.sqlite3"
    frozen.write_bytes(b"frozen")
    secret = "s" + "k-" + "A" * 40

    real_runner._record_halt_report(
        run_root,
        RuntimeError(f"provider rejected {secret}"),
        frozen,
    )
    first = (run_root / "halt_report.json").read_bytes()
    payload = json.loads(first)

    assert payload["status"] == "halted"
    assert payload["persisted_call_count"] == 0
    assert secret not in payload["message"]
    assert "[REDACTED_CREDENTIAL]" in payload["message"]
    real_runner._record_halt_report(run_root, RuntimeError("later"), frozen)
    assert (run_root / "halt_report.json").read_bytes() == first
