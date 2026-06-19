from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from pipeline.agents.llm_config import LLMConfig
from pipeline.eval.memory_tradeoff import (
    DEFAULT_AGREEMENT_TRUST_THRESHOLD,
    DEFAULT_CHAPTERS,
    KeyRow,
    OverrideItem,
    WorksheetRow,
    build_judge_worksheet,
    build_override_set,
    display_label_to_system,
    preview_judge_calls,
    parse_judge_payload,
    read_human_json,
    render_judge_prompt,
    render_worksheet_html,
    resolve_pair,
    score_memory_tradeoff,
    select_human_subset,
    stable_item_id,
    validate_dummy_not_real,
    write_key_json,
)


def _fixture_report() -> dict:
    def term(source: str, status: str, forms: dict[str, int], tier: str = "hard") -> dict:
        return {
            "source_term": source,
            "target_term": "",
            "source_blocks": 2,
            "status": status,
            "forms_used": forms,
            "case_sensitive": False,
            "constraint_strength": tier,
        }

    return {
        "D_registry_consistency": {
            "S0": {
                "terms_all": [
                    term("rules", "consistent", {"quy tac": 2}, "ignore_for_consistency"),
                    term("framework", "consistent", {"framework": 2}, "soft"),
                    term("model", "drift", {"mo hinh": 1, "mau": 1}, "hard"),
                ]
            },
            "S1": {
                "terms_all": [
                    term("rules", "consistent", {"luat": 2}, "ignore_for_consistency"),
                    term("framework", "consistent", {"khung phan mem": 2}, "soft"),
                    term("model", "consistent", {"mo hinh": 2}, "hard"),
                ]
            },
        }
    }


def _make_db(path: Path) -> None:
    conn = sqlite3.connect(path)
    conn.execute(
        """
        CREATE TABLE blocks (
          block_id TEXT PRIMARY KEY, doc_id TEXT, order_index INTEGER,
          block_type TEXT, chapter_id TEXT, text TEXT, original_text TEXT
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE translation_runs (
          run_id TEXT PRIMARY KEY, experiment_id TEXT, doc_id TEXT, block_id TEXT,
          config TEXT, stage TEXT, output_text TEXT, model TEXT
        )
        """
    )
    conn.execute(
        "INSERT INTO blocks VALUES (?,?,?,?,?,?,?)",
        (
            "b1",
            "d2l",
            1,
            "prose",
            "d2l_preliminaries",
            "These rules define a framework.",
            None,
        ),
    )
    rows = [
        ("tr_s0_b1", "S0", "Cac quy tac dinh nghia framework."),
        ("tr_s1_b1", "S1", "Cac luat dinh nghia khung phan mem."),
    ]
    for run_id, config, output in rows:
        conn.execute(
            "INSERT INTO translation_runs VALUES (?,?,?,?,?,?,?,?)",
            (run_id, "d2l_p3", "d2l", "b1", config, "draft", output, "gpt-5.4-mini"),
        )
    conn.commit()
    conn.close()


def test_override_set_deterministic(tmp_path: Path) -> None:
    db = tmp_path / "memory.sqlite3"
    _make_db(db)
    first = build_override_set(_fixture_report(), db, chapters=["preliminaries"])
    second = build_override_set(_fixture_report(), db, chapters=["preliminaries"])

    assert first == second
    assert [item.source_term for item in first] == ["framework", "rules"]
    assert all(item.rep_resolved for item in first)


def test_override_count_crosscheck_real_report() -> None:
    report_path = Path("data/reports/d2l_translation_metrics_v2.json")
    db_path = Path("data/jobs/d2l_p1/memory.sqlite3")
    if not report_path.exists() or not db_path.exists():
        pytest.skip("production D2L artifacts are not available")
    report = json.loads(report_path.read_text(encoding="utf-8"))
    items = build_override_set(report, db_path, chapters=DEFAULT_CHAPTERS)

    assert 50 <= len(items) <= 65
    assert len(items) == 57
    assert {item.tier for item in items} >= {"hard", "soft", "preserve", "ignore_for_consistency"}


def test_worksheet_is_blind(tmp_path: Path) -> None:
    db = tmp_path / "memory.sqlite3"
    _make_db(db)
    items = build_override_set(_fixture_report(), db, chapters=["preliminaries"])
    rows, _key = build_judge_worksheet(items, seed=42)
    html = render_worksheet_html(rows)
    combined = json.dumps([row.__dict__ for row in rows], ensure_ascii=False) + html

    assert "S0" not in combined
    assert "S1" not in combined
    assert "glossary" not in combined.casefold()
    assert "provenance" not in combined.casefold()
    assert 'download = "judgments_human.json"' in html


def test_key_separate_folder(tmp_path: Path) -> None:
    key_path = tmp_path / "KEY" / "worksheet_KEY.json"
    write_key_json(
        key_path,
        [KeyRow("i1", "S0", "S1", "rules", "hard", "quy tac", "luat", "b1")],
        seed=42,
    )
    readme = tmp_path / "KEY" / "README.md"
    readme.write_text("DO NOT OPEN\n", encoding="utf-8")

    assert key_path.exists()
    assert "DO NOT OPEN" in readme.read_text(encoding="utf-8")


def test_prompt_uses_dummy_not_real(tmp_path: Path) -> None:
    db = tmp_path / "memory.sqlite3"
    _make_db(db)
    rows, _ = build_judge_worksheet(build_override_set(_fixture_report(), db, chapters=["preliminaries"]))
    validate_dummy_not_real(rows)
    messages = render_judge_prompt(rows[0])
    combined = "\n".join(message["content"] for message in messages)
    user_payload = json.loads(messages[1]["content"])
    dummy_text = json.dumps(user_payload["dummy_example"], ensure_ascii=False)

    assert "widget coefficient" in combined
    assert "widget coefficient" in dummy_text
    assert "quy tac" not in dummy_text
    assert "khung phan mem" not in dummy_text


def test_judge_swaps_two_orientations() -> None:
    row = WorksheetRow(
        item_id="i1",
        en_sentence="These rules matter.",
        en_term="rules",
        version_A="Cac «quy tac» quan trong.",
        version_B="Cac «luat» quan trong.",
        tier="ignore_for_consistency",
    )
    forward = render_judge_prompt(row, orientation="forward")[1]["content"]
    reverse = render_judge_prompt(row, orientation="reverse")[1]["content"]

    assert "Cac «quy tac»" in forward
    assert "Cac «luat»" in reverse


def test_resolve_conservative() -> None:
    key = KeyRow("i1", "S0", "S1", "rules", "ignore", "quy tac", "luat", "b1")

    assert resolve_pair("A_better", "A_better", key) == "ambiguous"


def test_resolve_mapping() -> None:
    key = KeyRow("i1", "S0", "S1", "rules", "ignore", "quy tac", "luat", "b1")

    assert resolve_pair("A_better", "B_better", key) == "S0_better"
    assert resolve_pair("B_better", "A_better", key) == "S1_better"
    assert resolve_pair("equivalent", "equivalent", key) == "equivalent"


def test_scorer_refuses_incomplete_gemini() -> None:
    row = WorksheetRow("i1", "Source.", "term", "A", "B", "hard")
    key = KeyRow("i1", "S0", "S1", "term", "hard", "a", "b", "b1")

    with pytest.raises(ValueError, match="blind not complete"):
        score_memory_tradeoff(
            worksheet_rows=[row],
            key_rows=[key],
            gemini_rows=[{"item_id": "i1", "orientation": "forward", "judgment": {"label": "A_better"}}],
            human_rows=[],
        )


def test_human_subset_stratified() -> None:
    items = [
        OverrideItem(
            item_id=f"i{index}",
            source_term=f"term{index}",
            tier=tier,
            s0_surface="a",
            s1_surface="b",
            s0_count=1,
            s1_count=1,
            status_s0="consistent",
            status_s1="consistent",
            rep_block_id="b1",
            en_sentence="Source.",
            s0_window="A",
            s1_window="B",
            rep_resolved=True,
        )
        for index, tier in enumerate(["hard"] * 8 + ["soft"] * 5 + ["preserve"] * 2 + ["ignore_for_consistency"] * 1)
    ]
    selected = set(select_human_subset(items, seed=42))
    selected_tiers = {item.tier for item in items if item.item_id in selected}

    assert 12 <= len(selected) <= 15
    assert selected_tiers == {"hard", "soft", "preserve", "ignore_for_consistency"}


def test_judge_model_not_translator() -> None:
    from pipeline.agents.judge_client import JudgeConfigError, JudgeClient

    with pytest.raises(JudgeConfigError):
        JudgeClient(LLMConfig(model="gpt-5.4-mini"), ":memory:", transport=lambda **_: {})


def test_preview_has_cost_gate_and_caveats() -> None:
    rows = [WorksheetRow("i1", "Source.", "term", "A", "B", "hard")]
    preview = preview_judge_calls(rows, LLMConfig(model="gemini-2.5-flash", temperature=0, pricing={"input": 0.3, "cached_input": 0.0, "output": 2.5}))

    assert preview["calls"] == 2
    assert preview["confirm_token"]
    assert any("calibrated=false" in caveat for caveat in preview["caveats"])
    assert DEFAULT_AGREEMENT_TRUST_THRESHOLD == 0.80


def test_eval_only_no_runtime_write(tmp_path: Path) -> None:
    db = tmp_path / "memory.sqlite3"
    _make_db(db)
    before = db.read_bytes()
    build_override_set(_fixture_report(), db, chapters=["preliminaries"])
    after = db.read_bytes()

    assert before == after


def test_human_json_exact_schema(tmp_path: Path) -> None:
    path = tmp_path / "judgments_human.json"
    path.write_text(json.dumps({"items": [{"item_id": "i1", "human_label": "A_better", "note": ""}]}), encoding="utf-8")

    assert read_human_json(path)[0]["human_label"] == "A_better"


def test_display_label_to_system() -> None:
    key = KeyRow("i1", "S1", "S0", "term", "hard", "a", "b", "b1")

    assert display_label_to_system("A_better", key, orientation="forward") == "S1_better"
    assert display_label_to_system("A_better", key, orientation="reverse") == "S0_better"
    assert stable_item_id("rules") == stable_item_id("Rules")


def test_repair_truncated_judge_json() -> None:
    payload = '{\n  "label": "B_better",\n  "confidence": 0.8,\n  "reason": "Câu trả lời bị cắt'

    parsed = parse_judge_payload(payload)

    assert parsed["label"] == "B_better"
    assert parsed["confidence"] == 0.8
    assert parsed["raw"]["_parse_repaired"] is True
