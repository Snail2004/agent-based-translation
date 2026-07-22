from __future__ import annotations

import copy
import hashlib
import re
from pathlib import Path

import pytest

from pipeline.eval.contracts_v1 import ContractValidationError
from pipeline.eval.scorer_input_packets_v1 import seal_scorer_input_packet
from pipeline.eval.scorer_prompts_v3 import (
    CONTEXT_NOT_AVAILABLE,
    PJ_COMMON_CANDIDATE_ID,
    SF_BT_SEMANTIC_CANDIDATE_ID,
    parse_pj_response_v2,
    parse_sf_bt_semantic_response_v3,
    prepare_pj_prompt_presentations_v3,
    render_sf_bt_reverse_prompt_v3,
    render_sf_bt_semantic_prompt_v3,
)
from pipeline.eval.sf_bt_stage_contracts_v1 import (
    SF_BT_REVERSE_CANDIDATE_ID,
    SF_BT_REVERSE_PROMPT_SHA256,
    seal_sf_bt_semantic_judge_packet,
)


COMMIT = "a" * 40
NOW = "2026-07-18T00:00:00Z"
PROMPT_TASK = (
    Path(__file__).resolve().parents[2] / "tasks" / "TASK_EVAL_SCORER_PROMPTS_V3.md"
)


def _block(
    block_id: str,
    role: str,
    text: str | None,
    *,
    source: bool = False,
    status: str | None = None,
) -> dict:
    return {
        "block_id": block_id,
        "role": role,
        "block_type": "paragraph",
        "status": "source" if source else status or ("translated" if text else "missing"),
        "text": text,
    }


def _scorer_packet(
    method_id: str,
    *,
    first: tuple[str | None, str, str | None],
    second: tuple[str | None, str, str | None] | None = None,
    source: tuple[str, str, str] = (
        "Source before.",
        "Source active.",
        "Source after.",
    ),
) -> dict:
    roles = ("preceding", "active", "following")
    block_ids = ("b001", "b002", "b003")
    source_view = None
    if method_id == "pj":
        source_view = {
            "blocks": [
                _block(block_id, role, text, source=True)
                for block_id, role, text in zip(block_ids, roles, source, strict=True)
            ]
        }
    candidates = [
        {
            "slot_id": "candidate_1",
            "blocks": [
                _block(block_id, role, text)
                for block_id, role, text in zip(block_ids, roles, first, strict=True)
            ],
        }
    ]
    if second is not None:
        candidates.append(
            {
                "slot_id": "candidate_2",
                "blocks": [
                    _block(block_id, role, text)
                    for block_id, role, text in zip(
                        block_ids, roles, second, strict=True
                    )
                ],
            }
        )
    stage = "back_translation" if method_id == "sf_bt" else "pairwise_judgment"
    return seal_scorer_input_packet(
        {
            "schema_id": "EvaluationScorerInputPacketV1",
            "schema_version": "1.0.0",
            "packet_id": "packet-hidden-12345678",
            "created_at": NOW,
            "producer": {
                "workstream": "evaluation",
                "component": "prompt_fixture_component",
                "component_version": "1.0.0",
                "code_commit": COMMIT,
            },
            "binding": {
                "plan_id": "plan-hidden-12345678",
                "plan_sha256": "1" * 64,
                "config_sha256": "2" * 64,
                "input_set_sha256": "3" * 64,
                "job_id": "job-hidden-12345678",
                "method_id": method_id,
                "method_version": "planning-v1",
                "unit_id": "unit-hidden-12345678",
            },
            "languages": {"source_language": "en", "target_language": "vi"},
            "stage": stage,
            "source": source_view,
            "candidates": candidates,
            "integrity": {"packet_sha256": "0" * 64},
        }
    )


def _semantic_packet(
    *,
    source_first: bool = True,
    source_text: str = "Canonical source text.",
    back_translation: str = "Back-translated text.",
) -> dict:
    source_slot = "passage_a" if source_first else "passage_b"
    back_slot = "passage_b" if source_first else "passage_a"
    texts = {source_slot: source_text, back_slot: back_translation}
    return seal_sf_bt_semantic_judge_packet(
        {
            "schema_id": "SFBTSemanticJudgeInputPacketV1",
            "schema_version": "1.0.0",
            "packet_id": "semantic-hidden-12345678",
            "created_at": NOW,
            "producer": {
                "workstream": "evaluation",
                "component": "semantic_fixture_component",
                "component_version": "1.0.0",
                "code_commit": COMMIT,
            },
            "binding": {
                "stage1_result_id": "result-hidden-12345678",
                "stage1_result_sha256": "4" * 64,
                "stage1_packet_id": "stage1-hidden-12345678",
                "stage1_packet_sha256": "5" * 64,
                "plan_id": "plan-hidden-12345678",
                "plan_sha256": "1" * 64,
                "config_sha256": "2" * 64,
                "input_set_sha256": "3" * 64,
                "job_id": "job-hidden-12345678",
                "method_id": "sf_bt",
                "method_version": "planning-v1",
                "unit_id": "unit-hidden-12345678",
                "source_block_id": "b002",
                "source_text_sha256": hashlib.sha256(
                    source_text.encode("utf-8")
                ).hexdigest(),
                "back_translation_sha256": hashlib.sha256(
                    back_translation.encode("utf-8")
                ).hexdigest(),
                "presentation_id": "canonical" if source_first else "reverse",
                "source_slot_id": source_slot,
                "back_translation_slot_id": back_slot,
            },
            "language": "en",
            "stage": "semantic_comparison",
            "passages": [
                {
                    "slot_id": slot_id,
                    "text": texts[slot_id],
                    "text_sha256": hashlib.sha256(
                        texts[slot_id].encode("utf-8")
                    ).hexdigest(),
                }
                for slot_id in ("passage_a", "passage_b")
            ],
            "integrity": {"packet_sha256": "0" * 64},
        }
    )


def test_sf_bt_reverse_renderer_supports_no_context_and_bounded_context():
    packet = _scorer_packet(
        "sf_bt",
        first=("Ngữ cảnh trước.", "Nội dung chính.", None),
    )

    no_context = render_sf_bt_reverse_prompt_v3(
        packet, context_profile="no_context"
    )
    bounded = render_sf_bt_reverse_prompt_v3(
        packet, context_profile="bounded_neighbors"
    )

    assert "[ACTIVE block_id=b002" in no_context.rendered_prompt
    assert no_context.candidate_id == "sf_bt_reverse_v3_1_candidate"
    assert no_context.candidate_id == SF_BT_REVERSE_CANDIDATE_ID
    assert "Ngữ cảnh trước." not in no_context.rendered_prompt
    assert "[PRECEDING block_id=b001" in bounded.rendered_prompt
    assert (
        "A pronoun or other referring expression in ACTIVE may be rendered "
        "with its context-resolved antecedent"
    ) in bounded.rendered_prompt
    assert (
        "it does not authorize adding an entity that ACTIVE never refers to"
    ) in bounded.rendered_prompt
    assert bounded.rendered_prompt.count(CONTEXT_NOT_AVAILABLE) == 1
    assert "job-hidden-12345678" not in bounded.rendered_prompt
    assert no_context.rendered_prompt_sha256 != bounded.rendered_prompt_sha256


def test_reverse_prompt_task_fence_matches_runtime_revision_and_hash():
    task = PROMPT_TASK.read_text(encoding="utf-8")
    match = re.search(
        r"(?s)## SF-BT stage 1: back-translation candidate.*?"
        r"```text\n(.*?)\n```",
        task,
    )
    assert match is not None
    documented_prompt = match.group(1)

    assert f"Candidate ID: `{SF_BT_REVERSE_CANDIDATE_ID}`" in task
    assert f"`{SF_BT_REVERSE_PROMPT_SHA256}`" in task
    assert (
        hashlib.sha256(documented_prompt.encode("utf-8")).hexdigest()
        == SF_BT_REVERSE_PROMPT_SHA256
    )


def test_reverse_renderer_preserves_long_escaped_active_content():
    active = (
        'Dòng "trích dẫn" giữ `code --flag="x"` và C:\\tmp\\a.txt; '
        "URL https://example.test/a?x=1&y=2; "
        r"math \(f(x)=x^2\); literal {target_block_sequence}."
    )
    packet = _scorer_packet(
        "sf_bt",
        first=("Ngữ cảnh.", active, "Sau."),
    )

    rendered = render_sf_bt_reverse_prompt_v3(
        packet, context_profile="bounded_neighbors"
    )

    assert active in rendered.rendered_prompt
    assert rendered.rendered_prompt.count(active) == 1


def test_sf_bt_semantic_renderer_keeps_passage_roles_opaque():
    packet = _semantic_packet(source_first=False)
    rendered = render_sf_bt_semantic_prompt_v3(packet)

    assert rendered.candidate_id == SF_BT_SEMANTIC_CANDIDATE_ID
    assert "PASSAGE A\nBack-translated text." in rendered.rendered_prompt
    assert "PASSAGE B\nCanonical source text." in rendered.rendered_prompt
    assert "original English" not in rendered.rendered_prompt
    assert "back-translation" not in rendered.rendered_prompt
    assert "result-hidden-12345678" not in rendered.rendered_prompt
    assert "job-hidden-12345678" not in rendered.rendered_prompt


def test_prompt_rendering_does_not_reinterpret_placeholders_inside_content():
    semantic = _semantic_packet(
        source_first=True,
        source_text="Literal {passage_b} must remain.",
        back_translation="Literal {passage_a} must also remain.",
    )
    semantic_prompt = render_sf_bt_semantic_prompt_v3(semantic).rendered_prompt
    assert "PASSAGE A\nLiteral {passage_b} must remain." in semantic_prompt
    assert "PASSAGE B\nLiteral {passage_a} must also remain." in semantic_prompt

    pairwise = _scorer_packet(
        "pj",
        source=(
            "Source before.",
            "Literal {candidate_1_block_sequence}.",
            "Source after.",
        ),
        first=("Trước.", "Giữ {candidate_2_block_sequence}.", "Sau."),
        second=("Trước.", "Giữ {source_block_sequence}.", "Sau."),
    )
    pj_prompt = prepare_pj_prompt_presentations_v3(pairwise)
    assert pj_prompt.canonical is not None
    assert "Literal {candidate_1_block_sequence}." in (
        pj_prompt.canonical.rendered_prompt
    )
    assert "Giữ {candidate_2_block_sequence}." in (
        pj_prompt.canonical.rendered_prompt
    )
    assert "Giữ {source_block_sequence}." in pj_prompt.canonical.rendered_prompt


def test_pj_masks_asymmetric_context_for_both_candidates():
    packet = _scorer_packet(
        "pj",
        first=("Có ngữ cảnh.", "Bản dịch một.", "Sau một."),
        second=(None, "Bản dịch hai.", "Sau hai."),
    )

    presentations = prepare_pj_prompt_presentations_v3(packet)
    assert presentations.canonical is not None
    assert presentations.reversed is not None
    assert presentations.canonical.candidate_id == PJ_COMMON_CANDIDATE_ID
    prompt = presentations.canonical.rendered_prompt
    assert "Có ngữ cảnh." not in prompt
    candidate_sequences = prompt.split("CANDIDATE 1 SEQUENCE\n", 1)[1]
    assert candidate_sequences.count(CONTEXT_NOT_AVAILABLE) == 2
    assert "Source before." in prompt


def test_pj_mechanical_equal_uses_complete_displayed_sequences():
    packet = _scorer_packet(
        "pj",
        first=("Trước.", "Giống nhau.", "Sau."),
        second=("Trước.", "Giống nhau.", "Sau."),
    )
    presentations = prepare_pj_prompt_presentations_v3(packet)

    assert presentations.mechanical_equal
    assert not presentations.active_equal_context_diff
    assert presentations.canonical is None
    assert presentations.reversed is None


def test_pj_active_equal_but_context_different_requires_both_orders():
    packet = _scorer_packet(
        "pj",
        first=("Ngữ cảnh A.", "Giống nhau.", "Sau."),
        second=("Ngữ cảnh B.", "Giống nhau.", "Sau."),
    )
    presentations = prepare_pj_prompt_presentations_v3(packet)

    assert not presentations.mechanical_equal
    assert presentations.active_equal_context_diff
    assert presentations.canonical is not None
    assert presentations.reversed is not None
    assert "CANDIDATE 1 SEQUENCE\n[PRECEDING block_id=b001" in (
        presentations.canonical.rendered_prompt
    )
    assert presentations.canonical.rendered_prompt_sha256 != (
        presentations.reversed.rendered_prompt_sha256
    )


def test_pj_mechanical_normalization_accepts_nfc_and_trailing_whitespace():
    decomposed = "To\u0302\u0301i ưu.  \r\n"
    composed = "Tối ưu.\n"
    packet = _scorer_packet(
        "pj",
        first=("Trước.", decomposed, "Sau."),
        second=("Trước.", composed, "Sau."),
    )

    presentations = prepare_pj_prompt_presentations_v3(packet)
    assert presentations.mechanical_equal


@pytest.mark.parametrize(
    "hidden_value",
    [
        "packet-hidden-12345678",
        "plan-hidden-12345678",
        "job-hidden-12345678",
        "unit-hidden-12345678",
        "prompt_fixture_component",
        COMMIT,
        "1" * 64,
    ],
)
def test_renderer_rejects_hidden_identifier_copied_into_candidate_text(
    hidden_value,
):
    packet = _scorer_packet(
        "sf_bt",
        first=("Trước.", hidden_value, "Sau."),
    )
    with pytest.raises(ContractValidationError, match="prompt_identifier_leak"):
        render_sf_bt_reverse_prompt_v3(
            packet, context_profile="bounded_neighbors"
        )


@pytest.mark.parametrize("score", [0, 25, 50, 75, 100])
def test_sf_bt_semantic_response_accepts_only_closed_bands(score):
    parsed = parse_sf_bt_semantic_response_v3(
        f'{{"score":{score},"flags":[],"note":"concise"}}'
    )
    assert parsed["score"] == score


@pytest.mark.parametrize(
    "raw,error",
    [
        ('{"score":74,"flags":[],"note":"x"}', "score_band"),
        ('{"score":100,"flags":["other"],"note":"x"}', "enum"),
        ('{"score":100,"flags":[],"note":"x","extra":1}', "unknown_keys"),
        ('{"score":NaN,"flags":[],"note":"x"}', "response_json"),
        ('{"score":100,"score":0,"flags":[],"note":"x"}', "response_json"),
        ('{"score":100,"flags":[],"note":"x"} trailing', "response_json"),
    ],
)
def test_sf_bt_semantic_response_rejects_open_or_malformed_values(raw, error):
    with pytest.raises(ContractValidationError, match=error):
        parse_sf_bt_semantic_response_v3(raw)


def test_pj_response_contract_is_closed():
    raw = (
        '{"overall_verdict":"candidate_1","style_verdict":"tie",'
        '"tags":["meaning","terminology"],"note":"Candidate one preserves meaning."}'
    )
    assert parse_pj_response_v2(raw) == {
        "overall_verdict": "candidate_1",
        "style_verdict": "tie",
        "tags": ["meaning", "terminology"],
        "note": "Candidate one preserves meaning.",
    }


@pytest.mark.parametrize(
    "raw,error",
    [
        (
            '{"overall_verdict":"candidate_1","style_verdict":"tie",'
            '"tags":[],"note":"x","extra":1}',
            "unknown_keys",
        ),
        (
            '{"overall_verdict":"candidate_1","style_verdict":"tie",'
            '"tags":[],"note":"x"} trailing',
            "response_json",
        ),
        (
            '{"overall_verdict":"candidate_1","style_verdict":"tie",'
            '"tags":["meaning","meaning"],"note":"x"}',
            "duplicate",
        ),
        (
            '{"overall_verdict":"candidate_1","style_verdict":"tie",'
            '"tags":["meaning","grammar","formatting","terminology"],"note":"x"}',
            "array_too_long",
        ),
        (
            '{"overall_verdict":"candidate_1","style_verdict":"tie",'
            '"tags":[],"note":"one two three four five six seven eight nine ten '
            'eleven twelve thirteen fourteen fifteen sixteen seventeen eighteen '
            'nineteen twenty twenty-one twenty-two twenty-three twenty-four '
            'twenty-five twenty-six"}',
            "note_length",
        ),
    ],
)
def test_pj_response_rejects_invalid_diagnostics(raw, error):
    with pytest.raises(ContractValidationError, match=error):
        parse_pj_response_v2(raw)


def test_prompt_preparation_does_not_mutate_packet():
    packet = _scorer_packet(
        "pj",
        first=("Trước A.", "Chính A.", "Sau A."),
        second=(None, "Chính B.", "Sau B."),
    )
    before = copy.deepcopy(packet)

    prepare_pj_prompt_presentations_v3(packet)

    assert packet == before
