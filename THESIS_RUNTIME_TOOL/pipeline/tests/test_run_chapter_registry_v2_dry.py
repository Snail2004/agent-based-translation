from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest

from pipeline.literary.chapter_registry_schema_v2 import RunConfigV2
from pipeline.scripts import run_chapter_registry_v2_dry as dry_runner


REPO_ROOT = Path(__file__).resolve().parents[3]
DESIGN_DOC = REPO_ROOT / "design" / "LITERARY_PROMPT_DESIGN.md"
RUNNER_PATH = (
    REPO_ROOT
    / "THESIS_RUNTIME_TOOL"
    / "pipeline"
    / "scripts"
    / "run_chapter_registry_v2_dry.py"
)


def _document() -> dict[str, object]:
    return {
        "chapters": [
            {
                "chapter_id": chapter_id,
                "blocks": [
                    {
                        "block_id": f"{chapter_id}_b001",
                        "order_index": 1,
                        "block_type": "heading",
                        "clean_text": f"Chapter {chapter_id[-2:]}",
                    },
                    {
                        "block_id": f"{chapter_id}_b002",
                        "order_index": 2,
                        "block_type": "paragraph",
                        "clean_text": (
                            "Arden entered the western hall and greeted Bell beside silent "
                            "windows, carved panels, bronze lamps, velvet curtains, and oak stairs."
                        ),
                    },
                    {
                        "block_id": f"{chapter_id}_b003",
                        "order_index": 3,
                        "block_type": "paragraph",
                        "clean_text": (
                            "Bell answered calmly while the hound waited outside beneath winter "
                            "clouds near the garden wall."
                        ),
                    },
                ],
            }
            for chapter_id in dry_runner.CHAPTER_IDS
        ]
    }


def _write_document(root: Path) -> Path:
    path = root / "document.json"
    path.write_text(json.dumps(_document(), ensure_ascii=False), encoding="utf-8")
    return path


def test_proposed_config_is_content_addressed_and_blocks_unknown_provider_limits() -> None:
    config = dry_runner.proposed_run_config()

    assert isinstance(config, RunConfigV2)
    assert len(config.config_hash) == 64
    assert config.role_quota_gate_ids == {
        "b0": ("openai-row1-gpt54", "openai-row2-gpt54"),
        "b1": ("openai-row1-mini", "openai-row2-mini"),
        "auditor": ("openai-row1-gpt54", "openai-row2-gpt54"),
    }
    assert config.unknown_provider_limit_gate_ids == (
        "openai-row1-gpt54",
        "openai-row1-mini",
        "openai-row2-gpt54",
        "openai-row2-mini",
    )
    assert all(
        gate["rpm"] is None and gate["tpm"] is None and gate["rpd"] is None
        for gate in config.quota_gates.values()
    )
    assert config.b0_temperature == config.b1_temperature == config.auditor_temperature == 1.0
    assert config.b0_seed == config.b1_seed == config.auditor_seed == 20260612
    assert config.b0_verbosity == config.b1_verbosity == config.auditor_verbosity == "low"
    assert config.pricing_usd_per_million["b0"]["input"] is None
    assert config.pricing_usd_per_million["b1"] == {
        "input": 0.25,
        "cached_input": 0.025,
        "output": 2.0,
    }


def test_dry_render_writes_reproducible_offline_artifacts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(dry_runner, "REPO_ROOT", tmp_path)
    document = _write_document(tmp_path)
    first_output = tmp_path / "first"
    second_output = tmp_path / "second"

    first = dry_runner.dry_render(
        document_path=document,
        design_doc=DESIGN_DOC,
        output_dir=first_output,
    )
    second = dry_runner.dry_render(
        document_path=document,
        design_doc=DESIGN_DOC,
        output_dir=second_output,
    )

    assert first["api_calls"] == 0
    assert first["mode"] == "dry_render_only"
    assert first["phase_c_ready"] is False
    assert first["report_hash"] == second["report_hash"]
    assert first["run_config_hash"] == second["run_config_hash"]
    assert first["call_counts"]["exact_source_minimum"] == {
        "b0": 2,
        "ordinary_b1": 2,
        "targeted_recall": 0,
        "auditor": 0,
        "total": 4,
    }
    assert first["quota"]["phase_c_blocked_on_unknown_provider_limits"] is True
    assert first["configured_cost_upper_bound_usd"]["b0"] is None
    assert first["configured_cost_upper_bound_usd"]["auditor"] is None
    assert first["configured_cost_upper_bound_usd"]["b1"] > 0
    assert first["candidate_envelope"]["selected_cards"]["min"] == 16
    assert first["candidate_envelope"]["selected_cards"]["max"] == 16
    assert first["candidate_envelope"]["surface_candidate_packets"]["min"] == 16
    assert first["candidate_envelope"]["surface_candidate_packets"]["max"] == 16
    assert first["candidate_envelope"]["packet_candidates"]["min"] == 16
    assert first["candidate_envelope"]["packet_candidates"]["max"] == 16
    assert first["candidate_envelope"]["unmatched_recency_cards"]["max"] == 0
    assert first["candidate_envelope"]["legacy_separate_context_bytes"]["sum"] > 0
    assert first["candidate_envelope"]["prejoined_context_bytes"]["sum"] > 0
    assert first["candidate_envelope"]["prejoined_to_legacy_context_ratio"] is not None

    config_files = list(first_output.glob("run_config_*.json"))
    assert len(config_files) == 1
    assert config_files[0].stem.endswith(first["run_config_hash"][:16])
    assert {path.name for path in first_output.iterdir()} == {
        "README.md",
        "dry_render_report.json",
        "rendered_requests.jsonl",
        config_files[0].name,
    }
    persisted = json.loads((first_output / "dry_render_report.json").read_text(encoding="utf-8"))
    assert persisted["report_hash"] == first["report_hash"]
    rendered_lines = (first_output / "rendered_requests.jsonl").read_text(
        encoding="utf-8"
    ).splitlines()
    assert len(rendered_lines) == 6
    assert all(json.loads(line)["request"]["request_fingerprint"] for line in rendered_lines)


def test_dry_render_refuses_nonempty_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(dry_runner, "REPO_ROOT", tmp_path)
    document = _write_document(tmp_path)
    output = tmp_path / "occupied"
    output.mkdir()
    (output / "sentinel.txt").write_text("keep", encoding="utf-8")

    with pytest.raises(dry_runner.DryRenderError, match="not empty"):
        dry_runner.dry_render(
            document_path=document,
            design_doc=DESIGN_DOC,
            output_dir=output,
        )

    assert (output / "sentinel.txt").read_text(encoding="utf-8") == "keep"


def test_runner_has_no_direct_api_or_network_import() -> None:
    tree = ast.parse(RUNNER_PATH.read_text(encoding="utf-8"))
    imported = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    }
    imported.update(
        node.module or "" for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)
    )

    forbidden = {
        "httpx",
        "openai",
        "requests",
        "socket",
        "urllib",
        "pipeline.agents.llm_client",
    }
    assert not {name for name in imported if name in forbidden}
