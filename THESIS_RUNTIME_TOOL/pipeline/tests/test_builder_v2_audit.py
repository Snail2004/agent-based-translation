from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from pipeline.prepass import builder_v2_audit as audit
from pipeline.scripts import builder_v2_c3_audit as audit_script


def _db(tmp_path: Path) -> Path:
    path = tmp_path / "memory.sqlite3"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE blocks (block_id TEXT PRIMARY KEY, text TEXT, chapter_id TEXT, block_type TEXT)")
    conn.executemany(
        "INSERT INTO blocks VALUES (?,?,?,?)",
        [
            ("b_prose", "The tensor shape (2, 3, 4) describes a target shape.", "ch1", "prose"),
            ("b_math", "$H = 0$", "ch1", "math"),
        ],
    )
    conn.commit()
    conn.close()
    return path


def _entry(
    source: str,
    target: str = "x",
    *,
    key: str | None = None,
    occurrences: int = 2,
    variants: list[dict] | None = None,
    blocks: list[str] | None = None,
) -> dict:
    variants = variants or [
        {
            "surface": source,
            "evidence_block_ids": blocks or ["b_prose"],
            "occurrence_count": occurrences,
        }
    ]
    return {
        "concept_key": key or source.casefold(),
        "canonical_source_term": source,
        "canonical_target_vi": target,
        "occurrences_total": occurrences,
        "source_variants": variants,
        "target_variants": [],
        "conflict_ledger": [],
        "decision_log": [],
        "do_not_translate": False,
        "term_type": "term",
        "status": "ok",
    }


def _config(path: Path) -> None:
    path.write_text(
        "\n".join(
            [
                'model: "gpt-5.4-mini"',
                "temperature: 1.0",
                "seed: 20260612",
                'reasoning_effort: "none"',
                'verbosity: "low"',
                "max_output_tokens: 6144",
                "daily_token_cap: 2400000",
                "prompt_token_cap: 6000",
                "pricing:",
                "  input: 0.25",
                "  cached_input: 0.025",
                "  output: 2.00",
                "",
            ]
        ),
        encoding="utf-8",
    )


def test_card_builder_keeps_no_prose_empty_and_flags_real_overmerge(tmp_path: Path):
    db = _db(tmp_path)
    conn = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True)
    try:
        cur = conn.cursor()
        shape = audit.build_card(
            cur,
            _entry(
                "shape",
                variants=[
                    {"surface": "shape", "evidence_block_ids": ["b_prose"], "occurrence_count": 1},
                    {"surface": "shape (2, 3, 4)", "evidence_block_ids": ["b_prose"], "occurrence_count": 1},
                ],
            ),
        )
        one = audit.build_card(
            cur,
            _entry(
                "one",
                variants=[
                    {"surface": "one", "evidence_block_ids": ["b_prose"], "occurrence_count": 1},
                    {"surface": "H = 0", "evidence_block_ids": ["b_math"], "occurrence_count": 1},
                ],
            ),
        )
        code_only = audit.build_card(
            cur,
            _entry("zeros_like", blocks=["b_math"], variants=[
                {"surface": "zeros_like", "evidence_block_ids": ["b_math"], "occurrence_count": 1}
            ]),
        )
    finally:
        conn.close()

    assert shape["signals"]["overmerge_suspected"] is False
    assert one["signals"]["overmerge_suspected"] is True
    assert code_only["evidence"] == []
    assert code_only["evidence_missing_reason"] == "no prose occurrence"


def test_validate_apply_and_injection_sorting_use_auditor_tier():
    notebook = {
        "entries": [
            _entry("generic", occurrences=10),
            _entry("poly", occurrences=3),
            _entry("term", occurrences=2),
            _entry("rare", occurrences=1),
        ]
    }
    rows = audit.validate_audit_results(
        [
            {
                "entry_id": "generic",
                "audit_label": "generic_low_value",
                "priority_tier": "low",
                "injection_action": "deprioritize",
                "confidence": "high",
                "reason": "ordinary vocabulary",
            },
            {
                "entry_id": "poly",
                "audit_label": "polysemy_or_context_dependent",
                "priority_tier": "medium",
                "injection_action": "context_sensitive_translate",
                "confidence": "high",
                "reason": "conflicting contexts",
            },
            {
                "entry_id": "term",
                "audit_label": "keep_as_translate_term",
                "priority_tier": "high",
                "injection_action": "translate",
                "confidence": "high",
                "reason": "domain concept",
            },
            {
                "entry_id": "rare",
                "audit_label": "preserve_token",
                "priority_tier": "high",
                "injection_action": "preserve",
                "confidence": "high",
                "reason": "code token",
            },
        ],
        ["generic", "poly", "term", "rare"],
    )
    audited = audit.apply_audit_to_notebook(notebook, rows)
    ordered = audit.simulate_injection_order(audited["entries"], min_injection_occurrences=2)

    assert [item["entry_id"] for item in ordered] == ["term", "poly", "generic"]
    assert audited["entries"][3]["do_not_translate"] is True


def test_estimate_only_archives_prompts_without_api_or_db_write(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    db = _db(tmp_path)
    notebook = {"entries": [_entry("shape"), _entry("one", variants=[
        {"surface": "one", "evidence_block_ids": ["b_prose"], "occurrence_count": 1},
        {"surface": "H = 0", "evidence_block_ids": ["b_math"], "occurrence_count": 1},
    ])]}
    notebook_path = tmp_path / "notebook.json"
    notebook_path.write_text(json.dumps(notebook), encoding="utf-8")
    config_path = tmp_path / "llm_prepass.yaml"
    _config(config_path)
    out = tmp_path / "out"
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    report = audit_script.run_c3(
        db_path=db,
        notebook_path=notebook_path,
        config_path=config_path,
        out_dir=out,
        chunk_size=1,
        estimate_only=True,
    )

    assert report["zero_api"] is True
    assert report["db_hash_unchanged"] is True
    assert report["cards"] == 2
    assert report["calls"] == 2
    assert (out / "cards.json").exists()
    assert (out / "chunks.json").exists()
    prompt_files = sorted((out / "prompts").glob("*.txt"))
    assert len(prompt_files) == 2
    assert "You are a terminology auditor" in prompt_files[0].read_text(encoding="utf-8")


def test_validate_audit_results_rejects_missing_or_reordered_rows():
    with pytest.raises(ValueError):
        audit.validate_audit_results(
            [
                {
                    "entry_id": "b",
                    "audit_label": "keep_as_translate_term",
                    "priority_tier": "high",
                    "injection_action": "translate",
                    "confidence": "high",
                    "reason": "domain concept",
                }
            ],
            ["a"],
        )


def test_real_audit_helper_reasks_once_and_logs_cost(tmp_path: Path):
    config_path = tmp_path / "llm_prepass.yaml"
    _config(config_path)
    config = audit_script.load_llm_config(config_path)
    chunk = audit.chunk_cards([audit.build_card(sqlite3.connect(_db(tmp_path)).cursor(), _entry("shape"))], 1)[0]
    responses = [
        "not json",
        json.dumps(
            [
                {
                    "entry_id": "shape",
                    "audit_label": "keep_as_translate_term",
                    "priority_tier": "high",
                    "injection_action": "translate",
                    "confidence": "high",
                    "reason": "domain concept",
                }
            ]
        ),
    ]

    def transport(**kwargs):
        text = responses.pop(0)
        return {
            "choices": [{"message": {"content": text}}],
            "usage": {"prompt_tokens": 20, "completion_tokens": 5},
        }

    report = audit_script._run_real_audit(
        chunks=[chunk],
        config=config,
        cache_path=tmp_path / "llm_cache.sqlite3",
        transport=transport,
    )

    assert report["status"] == "completed"
    assert report["parse_failure_count"] == 1
    assert report["calls_logged"] == 2
    assert report["audit_rows"][0]["entry_id"] == "shape"
    assert (tmp_path / "cost_log.json").exists()
    assert (tmp_path / "raw_outputs.json").exists()
