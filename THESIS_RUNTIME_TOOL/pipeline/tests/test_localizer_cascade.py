from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from pipeline.agents.llm_config import LLMConfig
from pipeline.eval.localizer_cascade import (
    CascadeCase,
    LocalizationResultCache,
    RegistryEntry,
    TargetWindow,
    T2Decision,
    build_t2_messages,
    classify_localized_quote,
    legacy_longest_failures,
    load_registry,
    preflight_dev,
    registry_conflict_count,
    resolve_registry_entry,
    result_cache_key,
    run_t1,
    score_dev_pilot,
    t1_localize,
    validate_t2_payload,
)
from pipeline.eval.localizer import read_gold_csv


def _entry(
    source: str = "membership",
    target: str = "quan hệ thuộc",
    *,
    allowed: tuple[str, ...] = ("thuộc",),
    forbidden: tuple[str, ...] = (),
    scope: str = "global",
    chapter_id: str | None = None,
    glossary_id: str = "g1",
) -> RegistryEntry:
    return RegistryEntry(
        glossary_id, source, target, scope, chapter_id, False, allowed, forbidden
    )


def _case(
    *,
    row_id: str = "item:S0",
    config: str = "S0",
    block_id: str = "b1",
    source_term: str = "membership",
    source_text: str = "This denotes membership in a set.",
    target_text: str = "Điều này biểu thị quan hệ thuộc trong một tập hợp.",
    chapter_id: str = "c1",
) -> CascadeCase:
    start = source_text.index(source_term)
    return CascadeCase(
        row_id=row_id,
        opaque_id="occ_123",
        item_id="item",
        config=config,
        block_id=block_id,
        chapter_id=chapter_id,
        source_term=source_term,
        source_text=source_text,
        source_start=start,
        source_end=start + len(source_term),
        target_text=target_text,
    )


def _config() -> LLMConfig:
    return LLMConfig(
        model="gpt-5.4-mini-2026-03-17",
        temperature=0,
        seed=20260621,
        reasoning_effort="none",
        max_output_tokens=128,
        daily_token_cap=10000,
        prompt_token_cap=2000,
    )


def test_registry_scope_resolver() -> None:
    global_entry = _entry(glossary_id="global")
    chapter_entry = _entry(scope="chapter", chapter_id="c1", glossary_id="chapter")
    registry = {"membership": [global_entry, chapter_entry]}

    assert resolve_registry_entry(registry, "membership", "c1") == (chapter_entry, "chapter")
    assert resolve_registry_entry(registry, "membership", "c2") == (global_entry, "global")
    conflict = {"membership": [chapter_entry, _entry(scope="chapter", chapter_id="c1", glossary_id="c2")]}
    assert resolve_registry_entry(conflict, "membership", "c1")[0] is None


def test_t1_resolves_only_unique_and_short_form_escalates() -> None:
    case = _case()
    decision = t1_localize(case, _entry(allowed=()), "global")
    assert decision.status == "resolved"
    assert decision.quote == "quan hệ thuộc"

    multiple = _case(target_text="quan hệ thuộc rồi quan hệ thuộc")
    assert t1_localize(multiple, _entry(allowed=()), "global").reason == "multiple"

    short = _case(target_text="Ký hiệu được đọc là thuộc.")
    assert t1_localize(short, _entry(), "global").reason == "short_known_form"


def test_registry_conflict_escalates_only_when_conflicted_form_matches() -> None:
    entry = _entry(allowed=("thao tác",), forbidden=("thao tác", "phép toán"))
    conflicted = _case(target_text="Đây là thao tác.")
    decision = t1_localize(conflicted, entry, "global")
    assert decision.status == "residual"
    assert decision.reason == "registry_conflict"

    unambiguous = _case(target_text="Đây là quan hệ thuộc.")
    assert t1_localize(unambiguous, entry, "global").status == "resolved"
    assert registry_conflict_count({"membership": [entry]}) == 1


def test_t1_no_double_claim() -> None:
    first = _case(row_id="a:S0", source_term="membership")
    second = _case(
        row_id="b:S0",
        source_term="relation",
        source_text="This denotes membership and relation in a set.",
    )
    second = CascadeCase(**{**second.__dict__, "opaque_id": "occ_456"})
    registry = {
        "membership": [_entry(source="membership", allowed=())],
        "relation": [_entry(source="relation", allowed=(), glossary_id="g2")],
    }
    decisions = run_t1([first, second], registry)
    assert decisions[first.opaque_id].reason == "double_claim"
    assert decisions[second.opaque_id].reason == "double_claim"


def test_t2_prompt_is_blind_and_per_config_independent() -> None:
    case = _case(config="S0")
    window = TargetWindow(case.target_text, 0, len(case.target_text), 0.5, "full")
    messages = build_t2_messages(case, window)
    rendered = json.dumps(messages, ensure_ascii=False)
    user_payload = json.loads(messages[1]["content"])
    assert set(user_payload) == {"prompt_version", "occurrence_id", "source_context", "target_window"}
    assert "canonical" not in messages[0]["content"]
    assert "allowed" not in messages[0]["content"]
    assert "forbidden" not in messages[0]["content"]
    assert '"S0"' not in rendered and '"S1"' not in rendered
    assert "[[TERM_START]]membership[[TERM_END]]" in rendered
    assert case.opaque_id in rendered


def test_t2_output_validation_reanchors_unique_quote() -> None:
    case = _case(target_text="Điều này biểu thị sự thuộc về một tập hợp.")
    window = TargetWindow(case.target_text, 0, len(case.target_text), 0.5, "full")
    payload = {
        "occurrence_id": case.opaque_id,
        "status": "localized",
        "target_quote": "sự thuộc",
        "left_context": "biểu thị",
    }
    decision = validate_t2_payload(case, window, payload, _entry())
    assert decision.status == "localized"
    assert decision.offset_source == "unique_quote"
    assert case.target_text[decision.start:decision.end] == "sự thuộc"

    ambiguous = _case(target_text="sự thuộc và sự thuộc")
    decision = validate_t2_payload(ambiguous, TargetWindow(ambiguous.target_text, 0, len(ambiguous.target_text), .5, "full"), payload, _entry())
    assert decision.status == "ambiguous"


def test_t2_left_anchor_resolves_repeated_quote() -> None:
    case = _case(target_text="đầu sự thuộc. sau đó sự thuộc.")
    window = TargetWindow(case.target_text, 0, len(case.target_text), 0.5, "full")
    payload = {
        "occurrence_id": case.opaque_id,
        "status": "localized",
        "target_quote": "sự thuộc",
        "left_context": "sau đó",
    }
    decision = validate_t2_payload(case, window, payload, _entry())
    assert decision.status == "localized"
    assert decision.offset_source == "left_anchor"
    assert decision.start == case.target_text.rfind("sự thuộc")


def test_t2_position_reanchor_resolves_repeated_quote_with_clear_margin() -> None:
    case = _case(
        source_text="First clause. This denotes membership in a set.",
        target_text="thuộc ở đầu. Điều này biểu thị sự thuộc trong một tập hợp.",
    )
    window = TargetWindow(case.target_text, 0, len(case.target_text), 0.75, "full")
    payload = {
        "occurrence_id": case.opaque_id,
        "status": "localized",
        "target_quote": "thuộc",
        "left_context": "",
    }
    decision = validate_t2_payload(case, window, payload, _entry())
    assert decision.status == "localized"
    assert decision.offset_source == "position"
    assert decision.start == case.target_text.rfind("thuộc")


def test_t2_ignores_model_offsets_even_when_they_match_a_wrong_occurrence() -> None:
    case = _case(target_text="sự thuộc đầu. đúng là sự thuộc sau.")
    window = TargetWindow(case.target_text, 0, len(case.target_text), 0.5, "full")
    payload = {
        "occurrence_id": case.opaque_id,
        "status": "localized",
        "target_quote": "sự thuộc",
        "left_context": "đúng là",
        "start": 0,
        "end": len("sự thuộc"),
    }
    decision = validate_t2_payload(case, window, payload, _entry())
    assert decision.status == "localized"
    assert decision.offset_source == "left_anchor"
    assert decision.start == case.target_text.rfind("sự thuộc")


def test_t2_classify_after() -> None:
    entry = _entry(allowed=("quan hệ thuộc",), forbidden=("sự thuộc",))
    assert classify_localized_quote("quan hệ thuộc", entry) == "known_allowed"
    assert classify_localized_quote("sự thuộc", entry) == "known_forbidden"
    assert classify_localized_quote("tính thành viên", entry) == "novel"


def test_result_cache_key_has_source_hash_and_cache_is_occurrence_level(tmp_path: Path) -> None:
    case = _case()
    window = TargetWindow(case.target_text, 0, len(case.target_text), .5, "full")
    first = result_cache_key(case, window, _config())
    changed = CascadeCase(**{**case.__dict__, "source_start": case.source_start - 1})
    assert result_cache_key(changed, window, _config()) != first

    cache = LocalizationResultCache(tmp_path / "results.sqlite3")
    value = T2Decision(case.opaque_id, "localized", "novel", "sự thuộc", 1, 8, "model", "ok")
    cache.put(first, value)
    assert cache.get(first).from_cache is True


def test_metrics_split_per_config() -> None:
    s0 = _case(config="S0")
    s0 = CascadeCase(**{**s0.__dict__, "gold_start": 0, "gold_end": 2})
    s1 = _case(config="S1", row_id="item:S1")
    s1 = CascadeCase(**{**s1.__dict__, "opaque_id": "occ_456", "gold_start": 1, "gold_end": 3})
    decisions = {
        s0.opaque_id: T2Decision(s0.opaque_id, "localized", "novel", "xx", 0, 2, "model", "ok"),
        s1.opaque_id: T2Decision(s1.opaque_id, "localized", "novel", "xx", 0, 2, "model", "ok"),
    }
    report = score_dev_pilot([s0, s1], decisions)
    assert report["per_config"]["S0"]["exact_span"] == 1
    assert report["per_config"]["S1"]["exact_span"] == 0
    assert report["dataset_role"] == "dev_regression_not_generalization"


def test_real_dev_preflight_is_zero_api_and_has_eight_legacy_failures() -> None:
    gold = Path("data/eval/localizer_gold.csv")
    db = Path("data/jobs/d2l_p1/memory.sqlite3")
    if not gold.exists() or not db.exists():
        return
    rows = read_gold_csv(gold)
    assert len(legacy_longest_failures(rows)) == 8
    report, cases, _ = preflight_dev(gold_path=gold, db_path=db, config=_config())
    assert len(cases) == 8
    assert report["dataset_role"] == "dev_regression_not_generalization"
    assert report["estimated_total_tokens"] > 0
    assert report["worst_case_three_stage_tokens"] == report["estimated_total_tokens"] * 3
    assert report["t1"]["registry_entries_with_allowed_forbidden_overlap"] == 122
