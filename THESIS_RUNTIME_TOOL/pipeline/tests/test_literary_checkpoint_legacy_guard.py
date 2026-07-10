from __future__ import annotations

import json
from pathlib import Path

import pytest

from pipeline.agents.llm_config import LLMConfig
from pipeline.literary.builder_pilot import estimate_m2


REPO_ROOT = Path(__file__).resolve().parents[3]
DESIGN_DOC = REPO_ROOT / "design" / "LITERARY_PROMPT_DESIGN.md"


def _document() -> dict:
    return {
        "chapters": [
            {
                "chapter_id": "bk_ch01",
                "blocks": [
                    {
                        "block_id": "bk_ch01_b001",
                        "order_index": 1,
                        "clean_text": "Alice enters the hall.",
                    }
                ],
            },
            {
                "chapter_id": "bk_ch02",
                "blocks": [
                    {
                        "block_id": "bk_ch02_b001",
                        "order_index": 2,
                        "clean_text": "Bob greets Alice.",
                    }
                ],
            },
        ]
    }


@pytest.mark.skipif(not DESIGN_DOC.exists(), reason="Prompt design doc is not present")
def test_m2_rejects_legacy_multi_chapter_final_ledger_without_checkpoint(
    tmp_path: Path,
) -> None:
    """A one-chapter digest must not use a multi-chapter final M1 ledger."""
    m1_dir = tmp_path / "legacy_m1"
    m1_dir.mkdir()
    (m1_dir / "m1_report.json").write_text(
        json.dumps(
            {
                "chapters_selected": ["bk_ch01", "bk_ch02"],
                "validation_counts": {"lexicon_failed": 0, "narrative_failed": 0},
                "entity_ledger": {
                    "ent_alice": {"entity_id": "ent_alice", "canonical": "Alice"},
                    "ent_bob": {"entity_id": "ent_bob", "canonical": "Bob"},
                },
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="requires M1 as-of checkpoint for bk_ch01"):
        estimate_m2(
            _document(),
            ["bk_ch01"],
            design_doc=DESIGN_DOC,
            config=LLMConfig(
                model="fake-literary",
                temperature=0.2,
                reasoning_effort="none",
                max_output_tokens=256,
                prompt_token_cap=100_000,
            ),
            m1_dir=m1_dir,
        )
