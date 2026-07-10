from __future__ import annotations

from pathlib import Path

import pytest

from pipeline.agents.llm_config import LLMConfig
from pipeline.literary.builder_pilot import _checkpoint_prompt_hashes, run_m1
from pipeline.tests.test_literary_checkpoint import DESIGN_DOC, FakeLiteraryClient, _document


@pytest.mark.skipif(not DESIGN_DOC.exists(), reason="Prompt design doc is not present")
def test_prompt_checkpoint_hash_ignores_prose_outside_prompt_blockquote(tmp_path: Path) -> None:
    copied = tmp_path / "design.md"
    copied.write_text(
        DESIGN_DOC.read_text(encoding="utf-8") + "\n\nOutside-prompt prose changed.\n",
        encoding="utf-8",
    )
    assert _checkpoint_prompt_hashes(DESIGN_DOC, "m1", "bk_ch01") == _checkpoint_prompt_hashes(
        copied, "m1", "bk_ch01"
    )


@pytest.mark.skipif(not DESIGN_DOC.exists(), reason="Prompt design doc is not present")
def test_resume_rejects_non_prefix_chapter_request(tmp_path: Path) -> None:
    config = LLMConfig(
        model="fake-literary",
        temperature=0.2,
        reasoning_effort="none",
        max_output_tokens=256,
        prompt_token_cap=100_000,
    )
    with pytest.raises(ValueError, match="full chapter prefix"):
        run_m1(
            _document(),
            ["bk_ch02"],
            design_doc=DESIGN_DOC,
            config=config,
            client=FakeLiteraryClient(),
            out_dir=tmp_path / "out",
            confirm_usd=10.0,
            resume=True,
        )
