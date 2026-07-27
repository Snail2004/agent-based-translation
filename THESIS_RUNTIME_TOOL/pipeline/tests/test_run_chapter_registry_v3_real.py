from __future__ import annotations

from dataclasses import replace
from hashlib import sha256
import json
from pathlib import Path

import pytest

from pipeline.literary.checkpoint import file_sha256
from pipeline.scripts import run_chapter_registry_v3_real as runner


REPO_ROOT = Path(__file__).resolve().parents[3]
DESIGN_DOC = REPO_ROOT / "design" / "LITERARY_PROMPT_DESIGN.md"


def _document() -> dict[str, object]:
    return {
        "document_id": "book-test",
        "chapters": [
            {
                "chapter_id": chapter_id,
                "blocks": [
                    {
                        "block_id": f"{chapter_id}_b001",
                        "order_index": 1,
                        "block_type": "heading",
                        "clean_text": "Chapter",
                    },
                    {
                        "block_id": f"{chapter_id}_b002",
                        "order_index": 2,
                        "block_type": "paragraph",
                        "clean_text": "Arden entered the hall and Bell answered quietly.",
                    },
                ],
            }
            for chapter_id in runner.CHAPTER_IDS
        ],
    }


def test_phase_c_dry_render_is_zero_api_and_content_addresses_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    document = tmp_path / "document.json"
    document.write_text(json.dumps(_document()), encoding="utf-8")
    frozen = tmp_path / "memory.sqlite3"
    frozen.write_bytes(b"frozen-test")
    monkeypatch.setattr(runner, "FROZEN_DB_SHA256", file_sha256(frozen).upper())

    config = replace(
        runner.draft_run_config_v3(),
        ticket_share_halt=1.0,
        component_share_halt=1.0,
    )
    report = runner.run_phase_c_dry_render(
        document_path=document,
        design_doc=DESIGN_DOC,
        output_dir=tmp_path / "dry",
        frozen_db=frozen,
        config=config,
    )

    assert report["status"] == "approval_required"
    assert report["api_calls"] == 0
    assert report["semantic_quality_claim"] is False
    assert report["approval_boundary"]["phase_d_runner_enabled"] is True
    config_path = Path(report["run_config"]["path"])
    config = json.loads(config_path.read_text(encoding="utf-8"))
    assert config["config_hash"] == report["run_config"]["config_hash"]
    assert report["request_metrics"]["by_role"]["b0"]["calls"] == 2
    assert report["request_metrics"]["by_role"]["b1"]["calls"] == 2
    normalized_design = DESIGN_DOC.read_text(encoding="utf-8").encode("utf-8")
    assert report["design_doc"]["normalized_text_sha256"] == sha256(
        normalized_design
    ).hexdigest()
    assert set(report["prompt_manifest"]) == {"b0", "b1", "auditor"}
    assert all(row["prompt_bytes"] > 0 for row in report["prompt_manifest"].values())
    assert report["frozen_db_sha256_before"] == report["frozen_db_sha256_after"]
    assert list((tmp_path / "dry" / "calls").glob("*/request.json"))


def test_phase_c_refuses_nonempty_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    document = tmp_path / "document.json"
    document.write_text(json.dumps(_document()), encoding="utf-8")
    frozen = tmp_path / "memory.sqlite3"
    frozen.write_bytes(b"frozen-test")
    monkeypatch.setattr(runner, "FROZEN_DB_SHA256", file_sha256(frozen).upper())
    output = tmp_path / "dry"
    output.mkdir()
    (output / "keep.txt").write_text("keep", encoding="utf-8")

    with pytest.raises(runner.PhaseCError, match="not empty"):
        runner.run_phase_c_dry_render(
            document_path=document,
            design_doc=DESIGN_DOC,
            output_dir=output,
            frozen_db=frozen,
        )

    assert (output / "keep.txt").read_text(encoding="utf-8") == "keep"


def test_real_execution_requires_content_addressed_approval(tmp_path: Path) -> None:
    with pytest.raises(runner.PhaseCError, match="approved-config-hash"):
        runner.main(["--output-dir", str(tmp_path / "run"), "--execute-real"])


def test_openai_role_assignment_and_quota_buckets_are_locked() -> None:
    config = runner.draft_run_config_v3()

    assert config.b0_model_id == "gpt-5.4"
    assert config.b1_model_id == "gpt-5.4-mini"
    assert config.auditor_model_id == "gpt-5.4"
    assert {
        config.quota_gates[gate_id]["quota_bucket_id"]
        for gate_id in config.role_quota_gate_ids["b1"]
    } == {"openai-row1", "openai-row2"}
    assert all(
        config.quota_gates[gate_id]["model_id"] == "gpt-5.4-mini"
        for gate_id in config.role_quota_gate_ids["b1"]
    )
