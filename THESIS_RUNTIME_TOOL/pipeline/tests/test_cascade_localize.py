from __future__ import annotations

from pipeline.eval.cascade_localize import (
    CandidateSpan,
    CascadeOccurrence,
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


def test_llm_adjudicator_prompt_is_review_gated_and_validates_quote() -> None:
    item = AdjudicationInput(
        occurrence_id="occ-1",
        source_term="user",
        source_sentence="The user clicks.",
        target_region="Người dùng nhấp.",
        candidate_quotes=("người dùng",),
    )
    messages = build_messages(item)
    assert "occurrence-level localization adjudicator" in messages[0]["content"]
    assert "occ-1" in messages[1]["content"]
    assert validate_payload(
        {
            "occurrence_id": "occ-1",
            "status": "localized",
            "target_quote": "Người dùng",
            "confidence": "high",
            "reason": "present",
        },
        "occ-1",
        "Người dùng nhấp.",
    )["status"] == "localized"
    assert validate_payload(
        {
            "occurrence_id": "occ-1",
            "status": "localized",
            "target_quote": "khách hàng",
            "confidence": "high",
            "reason": "present",
        },
        "occ-1",
        "Người dùng nhấp.",
    )["status"] == "not_found"
