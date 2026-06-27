from __future__ import annotations

from pipeline.eval.cascade_localize import (
    CandidateSpan,
    CascadeOccurrence,
    _load_reusable_gold,
    _occurrence_index_in_source_sentence,
    _score_locate_only_against_reused_gold,
    _score_locate_only_by_code,
    run_t1_region,
    run_t2_rules,
)
from pipeline.eval.occurrence_adherence import AdherenceTerm
from pipeline.eval.region_align import EmbeddingCacheClient
from pipeline.eval.llm_adjudicator import AdjudicationInput, build_messages, validate_payload


class _Response:
    def __init__(self, inputs: list[str]) -> None:
        self.inputs = inputs

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict:
        return {
            "model": "text-embedding-bge-m3",
            "data": [{"embedding": _vector(text)} for text in self.inputs],
        }


def _vector(text: str) -> list[float]:
    if "sentence A" in text or "cau A" in text:
        return [1.0, 0.0]
    if "cau B" in text:
        return [0.95, 0.05]
    return [0.0, 1.0]


def _post(_endpoint: str, *, json: dict, timeout: int) -> _Response:
    del timeout
    return _Response(list(json["input"]))


def _occurrence() -> CascadeOccurrence:
    return CascadeOccurrence(
        occ_id="S0:block:registry:user:0",
        config="S0",
        block_id="block",
        chapter_id="chapter",
        source_term="user",
        term_id="registry:user",
        source_start=0,
        source_end=4,
        source_surface="user",
        source_sentence_idx=0,
        source_sentence="sentence A about user.",
        source_text="sentence A about user.",
        target_text="cau A ve nguoi dung. cau B ve khach hang.",
    )


def test_t1_low_margin_expands_to_two_sentences(tmp_path) -> None:
    client = EmbeddingCacheClient(
        cache_dir=tmp_path,
        post=_post,
        model="text-embedding-bge-m3",
        model_alias="bge-m3",
        model_version="v",
    )
    t1 = run_t1_region(_occurrence(), client=client, margin_threshold=0.20)
    assert t1.reason == "low_margin_top2"
    assert t1.sentence_indices == (0, 1)


def test_t2_primary_single_candidate_credits() -> None:
    occurrence = _occurrence()
    term = AdherenceTerm("registry:user", "user", ("nguoi dung", "khach hang"))
    t1 = type("T1", (), {
        "status": "ranked",
        "ranges": ((0, 20),),
        "__iter__": lambda self: iter(()),
    })()
    # Use the real dataclass shape via run_t1 would be overkill; this test only
    # needs the fields consumed by run_t2_rules.
    from pipeline.eval.cascade_localize import T1Region
    t1 = T1Region("ranked", (0,), ((0, 20),), (1.0,), 0.4, "top1")
    decision = run_t2_rules(
        occurrence,
        term,
        [CandidateSpan(9, 19, "nguoi dung", "active", ("registry:user",), "nguoi dung")],
        t1,
        [term],
    )
    assert decision.resolved_by == "t2_credit"
    assert decision.escalate_reason == "C1_primary"
    assert decision.target_surface == "nguoi dung"


def test_t2_no_form_in_region_flags_masquerade_suspect() -> None:
    occurrence = _occurrence()
    term = AdherenceTerm("registry:user", "user", ("nguoi dung", "khach hang"))
    from pipeline.eval.cascade_localize import T1Region
    t1 = T1Region("ranked", (0,), ((0, 20),), (1.0,), 0.4, "top1")
    decision = run_t2_rules(
        occurrence,
        term,
        [CandidateSpan(29, 39, "khach hang", "active", ("registry:user",), "khach hang")],
        t1,
        [term],
    )
    assert decision.resolved_by == "t3_stub"
    assert decision.decision == "not_rendered"
    assert decision.escalate_reason == "C0_no_accepted_form_in_t1_region"
    assert decision.masquerade_suspect is True


def test_t2_shared_variant_escalates() -> None:
    occurrence = _occurrence()
    user = AdherenceTerm("registry:user", "user", ("nguoi dung", "khach hang"))
    customer = AdherenceTerm("registry:customer", "customer", ("khach hang",))
    from pipeline.eval.cascade_localize import T1Region
    t1 = T1Region("ranked", (0,), ((0, 30),), (1.0,), 0.4, "top1")
    decision = run_t2_rules(
        occurrence,
        user,
        [CandidateSpan(9, 19, "khach hang", "active", ("registry:user",), "khach hang")],
        t1,
        [user, customer],
    )
    assert decision.resolved_by == "t3_stub"
    assert decision.escalate_reason == "C1_variant_shared_with_other_term"


def test_llm_adjudicator_prompt_is_locate_only_and_validates_quote() -> None:
    item = AdjudicationInput(
        occurrence_id="occ-1",
        source_term="user",
        occurrence_index=1,
        source_sentence="The user clicks.",
        target_region="Người dùng nhấp.",
    )
    messages = build_messages(item)
    assert "occurrence-level LOCATOR" in messages[0]["content"]
    assert "OCCURRENCE_INDEX" in messages[0]["content"]
    assert "accepted_forms" not in messages[1]["content"]
    assert "occ-1" in messages[1]["content"]
    assert validate_payload(
        {
            "occurrence_id": "occ-1",
            "found": True,
            "target_quote": "Người dùng",
            "left_context": "",
            "confidence": "high",
        },
        "occ-1",
        "Người dùng nhấp.",
    )["found"] is True
    assert validate_payload(
        {
            "occurrence_id": "occ-1",
            "found": True,
            "target_quote": "khách hàng",
            "left_context": "",
            "confidence": "high",
        },
        "occ-1",
        "Người dùng nhấp.",
    )["found"] is False


def test_locate_only_reused_gold_scoring_accepts_containment_and_not_rendered() -> None:
    assert _score_locate_only_against_reused_gold(
        {"found": True, "target_quote": "với tập dữ liệu MNIST này"},
        {"gold_label": "rendered", "gold_target_span": "tập dữ liệu MNIST"},
    )["correct"] is True
    assert _score_locate_only_against_reused_gold(
        {"found": False, "target_quote": ""},
        {"gold_label": "not_rendered", "gold_target_span": ""},
    )["correct"] is True
    assert _score_locate_only_against_reused_gold(
        {"found": True, "target_quote": "khách hàng"},
        {"gold_label": "not_rendered", "gold_target_span": ""},
    )["correct"] is False


def test_locate_only_code_scores_containment_not_model_verdict() -> None:
    scored = _score_locate_only_by_code(
        {"found": True, "target_quote": "với hai ví dụ dữ liệu này"},
        {"source_term": "data examples", "accepted_forms": ("ví dụ dữ liệu", "mẫu dữ liệu")},
    )
    assert scored["adherence_label"] == "adherent"
    assert scored["highlight_surface"] == "ví dụ dữ liệu"
    off = _score_locate_only_by_code(
        {"found": True, "target_quote": "một bộ kỹ thuật"},
        {"source_term": "set", "accepted_forms": ("tập hợp", "tập")},
    )
    assert off["adherence_label"] == "off_glossary"


def test_locate_only_containment_uses_boundary_substring_not_segmenter() -> None:
    assert _score_locate_only_by_code(
        {"found": True, "target_quote": "từ nguyên lý cơ bản"},
        {"source_term": "first principles", "accepted_forms": ("nguyên lý cơ bản",)},
    )["adherence_label"] == "adherent"
    assert _score_locate_only_by_code(
        {"found": True, "target_quote": "các luật"},
        {"source_term": "rules", "accepted_forms": ("luật",)},
    )["adherence_label"] == "adherent"
    assert _score_locate_only_by_code(
        {"found": True, "target_quote": "một mục"},
        {"source_term": "item", "accepted_forms": ("mục",)},
    )["adherence_label"] == "adherent"
    assert _score_locate_only_by_code(
        {"found": True, "target_quote": "cạnh tranh"},
        {"source_term": "competition", "accepted_forms": ("cuộc thi",)},
    )["adherence_label"] == "off_glossary"
    assert _score_locate_only_by_code(
        {"found": True, "target_quote": "một chút"},
        {"source_term": "bit", "accepted_forms": ("bit",)},
    )["adherence_label"] == "off_glossary"
    assert _score_locate_only_by_code(
        {"found": True, "target_quote": "mụcđích"},
        {"source_term": "item", "accepted_forms": ("mục",)},
    )["adherence_label"] == "off_glossary"


def test_occurrence_index_is_sentence_local() -> None:
    decision = {
        "source_term": "model",
        "source_text": "The model is trained. The model and model are evaluated.",
        "source_sentence": "The model and model are evaluated.",
        "source_start": 38,
    }
    assert _occurrence_index_in_source_sentence(decision) == 2


def test_reusable_gold_loader_converts_collision_offset_to_quote(tmp_path) -> None:
    path = tmp_path / "gold.csv"
    path.write_text(
        "config,block_id,source_span,target_text,gold_target_span,gold_label\n"
        "S0,b,1:5:user,abc người dùng xyz,4:14,rendered\n",
        encoding="utf-8",
    )
    rows = _load_reusable_gold([path])
    assert rows[("S0", "b", 1, 5)]["gold_target_span"] == "người dùng"
    assert rows[("S0", "b", 1, 5)]["gold_target_start"] == 4
    assert rows[("S0", "b", 1, 5)]["gold_target_end"] == 14
