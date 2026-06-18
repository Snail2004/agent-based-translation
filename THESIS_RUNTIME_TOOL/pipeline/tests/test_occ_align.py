from __future__ import annotations

import csv
import json
import sqlite3
from pathlib import Path

import pytest

from pipeline.agents.llm_client import LLMResult, LLMUsage
from pipeline.agents.llm_config import LLMConfig
from pipeline.eval.occ_align import (
    OccItem,
    Proposal,
    align_independent,
    align_selfreport,
    audit_occ_align,
    build_gold_sample_rows,
    build_occ_frame,
    load_translation_model,
    preview_selfreport,
    render_selfreport_messages,
    simalign_cached,
    verify_presence,
    write_gold_sample_csv,
    write_jsonl,
)
from pipeline.eval.d2l_translate_score import ScopeBlock


class FakeAligner:
    def get_word_aligns(self, src_sent: list[str], trg_sent: list[str]) -> dict[str, list[tuple[int, int]]]:
        # An agent builds a model. -> Mot tac nhan xay mot mo hinh.
        return {"itermax": [(1, 1), (1, 2), (4, 5), (4, 6)]}


class FakeClient:
    def __init__(self, payload: dict) -> None:
        self.payload = payload
        self.config = LLMConfig(model="gpt-5.4-mini")
        self.calls: list[list[dict[str, str]]] = []

    def call(self, messages: list[dict[str, str]], **kwargs: object) -> LLMResult:
        self.calls.append(messages)
        text = json.dumps(self.payload, ensure_ascii=False)
        return LLMResult(
            text=text,
            parsed_json=self.payload,
            json_error=None,
            model=self.config.model,
            system_fingerprint=None,
            usage=LLMUsage(),
            cost_usd=0.0,
            latency_ms=0,
            from_cache=False,
            cache_key="fake",
        )


def _blocks() -> list[ScopeBlock]:
    return [
        ScopeBlock(
            block_id="b1",
            chapter_id="d2l_preliminaries",
            order_index=1,
            block_type="prose",
            text="An agent builds a model. The agent learns.",
        )
    ]


def _terms() -> list[dict[str, object]]:
    return [
        {
            "glossary_id": "gl_agent",
            "source_term": "agent",
            "target_term": "tac nhan",
            "allowed_variants_json": json.dumps(["tac tu"]),
            "constraint_strength": "hard",
        },
        {
            "glossary_id": "gl_model",
            "source_term": "model",
            "target_term": "mo hinh",
            "allowed_variants_json": "[]",
            "constraint_strength": "hard",
        },
    ]


def test_occ_frame_deterministic() -> None:
    first = build_occ_frame(_blocks(), _terms(), chapter="d2l_preliminaries")
    second = build_occ_frame(_blocks(), _terms(), chapter="d2l_preliminaries")

    assert first == second
    assert [item.source_term for item in first] == ["agent", "model", "agent"]
    assert first[0].occ_id.startswith("b1:3:8:agent")


def test_independent_fake_aligner_maps_target_span() -> None:
    occ_frame = build_occ_frame(_blocks(), _terms(), chapter="d2l_preliminaries")

    proposals = align_independent(
        occ_frame[:2],
        {"b1": _blocks()[0].text},
        {"b1": "Mot tac nhan xay mot mo hinh."},
        config="S0",
        aligner=FakeAligner(),
        model="fake-aligner",
    )

    by_term = {item.occ_id: item for item in proposals}
    assert by_term[occ_frame[0].occ_id].target_surface == "tac nhan"
    assert by_term[occ_frame[1].occ_id].target_surface == "mo hinh"


def test_selfreport_posthoc_only() -> None:
    occ = build_occ_frame(_blocks(), _terms(), chapter="d2l_preliminaries")[0]
    client = FakeClient({"items": [{"occ_id": occ.occ_id, "status": "ALIGNED", "target_surface": "tac nhan"}]})

    proposals = align_selfreport(
        [occ],
        {"b1": _blocks()[0].text},
        {"b1": "Mot tac nhan xay mot mo hinh."},
        config="S0",
        client=client,  # type: ignore[arg-type]
    )

    assert len(client.calls) == 1
    assert "target_frozen_translation" in client.calls[0][1]["content"]
    assert proposals[0].target_surface == "tac nhan"


def test_selfreport_model_is_translator(tmp_path: Path) -> None:
    db_path = tmp_path / "memory.sqlite3"
    conn = sqlite3.connect(db_path)
    conn.execute(
        """
        CREATE TABLE translation_runs (
          experiment_id TEXT, config TEXT, stage TEXT, model TEXT
        )
        """
    )
    conn.execute(
        "INSERT INTO translation_runs VALUES ('d2l_p3', 'S0', 'draft', 'gpt-5.4-mini')"
    )
    conn.commit()
    conn.close()

    assert load_translation_model(db_path, config="S0") == "gpt-5.4-mini"
    assert load_translation_model(db_path, config="S0") == LLMConfig(model="gpt-5.4-mini").model


def test_verify_catches_hallucination() -> None:
    proposal = Proposal(
        occ_id="o1",
        block_id="b1",
        config="S0",
        branch="selfreport",
        source="selfreport",
        target_start=0,
        target_end=7,
        target_surface="khong co",
        status="aligned",
        present=True,
    )

    verified = verify_presence("Mot tac nhan.", proposal)

    assert verified.present is False
    assert verified.status == "instrument_error"


def test_verify_misses_misattribution() -> None:
    proposal = Proposal(
        occ_id="o1",
        block_id="b1",
        config="S1",
        branch="selfreport",
        source="selfreport",
        target_start=4,
        target_end=7,
        target_surface="tap",
        status="aligned",
        present=True,
    )

    verified = verify_presence("Mot tap du lieu.", proposal)

    assert verified.present is True
    assert verified.status == "aligned"


def test_s0_prompt_has_no_glossary() -> None:
    occ = build_occ_frame(_blocks(), _terms(), chapter="d2l_preliminaries")[0]

    messages = render_selfreport_messages(
        [occ],
        source_text=_blocks()[0].text,
        frozen_target="Mot tac nhan xay mot mo hinh.",
        config="S0",
    )
    combined = "\n".join(item["content"] for item in messages)

    assert "tac nhan" in combined  # output text is visible
    assert "allowed_variants" not in combined
    assert "glossary" in combined.lower()  # only the rule says not to use one


def test_single_annotator_blocks_headline(tmp_path: Path) -> None:
    proposal_path = tmp_path / "proposal.jsonl"
    gold_path = tmp_path / "gold.csv"
    proposal = {
        "branch": "simalign",
        "config": "S0",
        "occ_id": "o1",
        "target_start": 0,
        "target_end": 3,
        "target_surface": "abc",
        "present": True,
    }
    write_jsonl(proposal_path, [proposal, {**proposal, "config": "S1"}])
    with gold_path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(
            fh,
            fieldnames=[
                "config",
                "occ_id",
                "src_term",
                "accepted_forms",
                "gold_target_start",
                "gold_target_end",
                "gold_surface",
                "annotator",
            ],
        )
        writer.writeheader()
        writer.writerow({"config": "S0", "occ_id": "o1", "gold_target_start": 0, "gold_target_end": 3, "gold_surface": "abc", "annotator": "me"})
        writer.writerow({"config": "S1", "occ_id": "o1", "gold_target_start": 0, "gold_target_end": 3, "gold_surface": "abc", "annotator": "me"})

    report = audit_occ_align([proposal_path], gold_path, out_path=tmp_path / "audit.json")

    assert report["per_branch"]["simalign"]["acc_S0"] == 1.0
    assert report["gate"]["decision"] == "diagnostic_only"
    assert report["iaa"]["single_annotator"] is True


def test_sampler_stratified_shuffle(tmp_path: Path) -> None:
    occ_frame = build_occ_frame(_blocks(), _terms(), chapter="d2l_preliminaries")
    report = {
        "D_registry_consistency": {
            "S0": {"terms_all": [{"source_term": "agent", "status": "drift"}, {"source_term": "model", "status": "consistent"}]},
            "S1": {"terms_all": [{"source_term": "agent", "status": "consistent"}, {"source_term": "model", "status": "consistent"}]},
        }
    }

    rows_a = build_gold_sample_rows(
        occ_frame,
        {"S0": {"b1": "Mot tac nhan."}, "S1": {"b1": "Mot tac nhan."}},
        report,
        cap_per_term=1,
        seed=42,
        max_rows=4,
    )
    rows_b = build_gold_sample_rows(
        occ_frame,
        {"S0": {"b1": "Mot tac nhan."}, "S1": {"b1": "Mot tac nhan."}},
        report,
        cap_per_term=1,
        seed=42,
        max_rows=4,
    )
    out = tmp_path / "sample.csv"
    write_gold_sample_csv(out, rows_a, seed=42, cap_per_term=1, max_rows=4)

    assert rows_a == rows_b
    assert max(sum(1 for row in rows_a if row["config"] == cfg and row["src_term"] == term) for cfg in ["S0", "S1"] for term in ["agent", "model"]) <= 1
    assert len(rows_a) <= 4
    assert "target_start" not in rows_a[0]
    assert out.read_text(encoding="utf-8").startswith("# provenance: seed=42")


def test_not_rendered_option() -> None:
    occ = build_occ_frame(_blocks(), _terms(), chapter="d2l_preliminaries")[0]
    client = FakeClient({"items": [{"occ_id": occ.occ_id, "status": "NOT_RENDERED", "target_surface": ""}]})

    proposals = align_selfreport(
        [occ],
        {"b1": _blocks()[0].text},
        {"b1": "Mot cau khac."},
        config="S0",
        client=client,  # type: ignore[arg-type]
    )

    assert proposals[0].target_start is None
    assert proposals[0].status == "not_rendered"


def test_simalign_cache_byte_identical(tmp_path: Path) -> None:
    calls = 0

    def compute() -> list[dict[str, object]]:
        nonlocal calls
        calls += 1
        return [{"branch": "simalign", "config": "S0", "occ_id": "o1"}]

    first = simalign_cached(cache_dir=tmp_path, cache_key="abc", compute=compute)
    first_bytes = (tmp_path / "abc.jsonl").read_bytes()
    second = simalign_cached(cache_dir=tmp_path, cache_key="abc", compute=compute)
    second_bytes = (tmp_path / "abc.jsonl").read_bytes()

    assert first == second
    assert first_bytes == second_bytes
    assert calls == 1


def test_preview_selfreport_cost_gate_has_confirm_token() -> None:
    occ = build_occ_frame(_blocks(), _terms(), chapter="d2l_preliminaries")[0]

    preview = preview_selfreport(
        [occ],
        {"b1": _blocks()[0].text},
        {"b1": "Mot tac nhan xay mot mo hinh."},
        config="S0",
        llm_config=LLMConfig(model="gpt-5.4-mini", max_output_tokens=128),
    )

    assert preview["calls"] == 1
    assert preview["estimated_prompt_tokens"] > 0
    assert len(preview["confirm_token"]) == 12
