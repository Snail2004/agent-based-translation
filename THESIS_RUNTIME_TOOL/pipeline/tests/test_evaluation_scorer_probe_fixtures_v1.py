from __future__ import annotations

import copy
import json

import pytest

from pipeline.eval.contracts_v1 import ContractValidationError
from pipeline.eval.scorer_input_packets_v1 import (
    seal_scorer_input_packet,
    validate_scorer_input_packet,
)
from pipeline.eval.scorer_probe_fixtures_v1 import (
    DEFAULT_SCORER_PROBE_FIXTURE_PATH,
    PJ_REQUIRED_CATEGORIES,
    SF_BT_STRATA,
    load_default_scorer_probe_fixture_set,
    planted_marker_present,
    scorer_probe_fixture_sha256,
    validate_scorer_probe_fixture_set,
)
from pipeline.eval.scorer_prompts_v3 import (
    prepare_pj_prompt_presentations_v3,
    render_sf_bt_reverse_prompt_v3,
)


EXPECTED_APPROVED_SHA256 = (
    "2d34aa5cd08885316a538af7b340e184184d43cde2341f9f6cbf80a03d4f56b0"
)
COMMIT = "a" * 40
NOW = "2026-07-18T00:00:00Z"


def _raw() -> dict:
    return json.loads(DEFAULT_SCORER_PROBE_FIXTURE_PATH.read_text(encoding="utf-8"))


def _seal_sf_bt_packet(row: dict) -> dict:
    blocks = []
    for block_id, role, text in (
        ("probe-before", "preceding", row["target_preceding_vi"]),
        ("probe-active", "active", row["target_active_vi"]),
        ("probe-after", "following", row["target_following_vi"]),
    ):
        blocks.append(
            {
                "block_id": block_id,
                "role": role,
                "block_type": "paragraph",
                "status": "translated" if text is not None else "missing",
                "text": text,
            }
        )
    return _seal_packet("sf_bt", None, [blocks])


def _seal_pj_packet(row: dict) -> dict:
    source = [
        {
            "block_id": "probe-active",
            "role": "active",
            "block_type": row["block_type"],
            "status": "source",
            "text": row["source_en"],
        }
    ]
    candidates = [
        [
            {
                "block_id": "probe-active",
                "role": "active",
                "block_type": row["block_type"],
                "status": "translated",
                "text": text,
            }
        ]
        for text in (row["candidate_a_vi"], row["candidate_b_vi"])
    ]
    return _seal_packet("pj", source, candidates)


def _seal_packet(method_id: str, source: list | None, candidates: list[list]) -> dict:
    stage = "back_translation" if method_id == "sf_bt" else "pairwise_judgment"
    return seal_scorer_input_packet(
        {
            "schema_id": "EvaluationScorerInputPacketV1",
            "schema_version": "1.0.0",
            "packet_id": f"fixture-packet-{method_id}-12345678",
            "created_at": NOW,
            "producer": {
                "workstream": "evaluation",
                "component": "scorer_probe_fixture_test",
                "component_version": "1.0.0",
                "code_commit": COMMIT,
            },
            "binding": {
                "plan_id": "fixture-plan-12345678",
                "plan_sha256": "1" * 64,
                "config_sha256": "2" * 64,
                "input_set_sha256": "3" * 64,
                "job_id": f"fixture-job-{method_id}-12345678",
                "method_id": method_id,
                "method_version": "fixture-v1",
                "unit_id": "fixture-unit-12345678",
            },
            "languages": {"source_language": "en", "target_language": "vi"},
            "stage": stage,
            "source": None if source is None else {"blocks": source},
            "candidates": [
                {
                    "slot_id": f"candidate_{index}",
                    "blocks": blocks,
                }
                for index, blocks in enumerate(candidates, 1)
            ],
            "integrity": {"packet_sha256": "0" * 64},
        }
    )


def test_default_fixture_is_closed_balanced_eval_only_and_deterministic():
    fixture = load_default_scorer_probe_fixture_set()

    assert fixture["review_status"] == "approved_external_review"
    assert fixture["fixture_set_id"] == "eval-scorer-planted-book-neutral-v1-1"
    assert fixture["book_neutral"] is True
    assert fixture["runtime_admission"] == "forbidden"
    assert len(fixture["sf_bt_context_ablation"]) == 50
    assert {
        stratum: sum(
            row["stratum"] == stratum
            for row in fixture["sf_bt_context_ablation"]
        )
        for stratum in SF_BT_STRATA
    } == {stratum: 10 for stratum in SF_BT_STRATA}
    assert {row["category"] for row in fixture["pj_cases"]} == (
        PJ_REQUIRED_CATEGORIES
    )
    assert scorer_probe_fixture_sha256(fixture) == EXPECTED_APPROVED_SHA256
    assert scorer_probe_fixture_sha256(_raw()) == EXPECTED_APPROVED_SHA256


def test_direction_sensitive_strata_are_balanced():
    fixture = load_default_scorer_probe_fixture_set()
    for stratum in {
        "P1_context_repair_risk",
        "P4_ambiguity_resolution",
        "P5_context_import_bait",
    }:
        rows = [
            row
            for row in fixture["sf_bt_context_ablation"]
            if row["stratum"] == stratum
        ]
        assert (
            sum(
                row["target_preceding_vi"] is not None
                and row["target_following_vi"] is None
                for row in rows
            )
            == 5
        )
        assert (
            sum(
                row["target_preceding_vi"] is None
                and row["target_following_vi"] is not None
                for row in rows
            )
            == 5
        )


def test_planted_marker_match_is_nfc_casefolded_and_boundary_aware():
    assert not planted_marker_present("Any text.", "")
    assert planted_marker_present("Cả 14 trang đều có sơ đồ.", "14")
    assert planted_marker_present("MIRA signed the receipt.", "Mira")
    assert not planted_marker_present("Có 214 trang.", "14")
    assert not planted_marker_present("Batch K90 failed.", "K9")


def test_fixture_cannot_masquerade_as_a_model_facing_scorer_packet():
    with pytest.raises(ContractValidationError):
        validate_scorer_input_packet(_raw())


def test_all_sf_bt_fixture_rows_dry_render_both_context_profiles():
    fixture = load_default_scorer_probe_fixture_set()
    for row in fixture["sf_bt_context_ablation"]:
        packet = _seal_sf_bt_packet(row)
        no_context = render_sf_bt_reverse_prompt_v3(
            packet, context_profile="no_context"
        ).rendered_prompt
        bounded = render_sf_bt_reverse_prompt_v3(
            packet, context_profile="bounded_neighbors"
        ).rendered_prompt

        assert row["target_active_vi"] in no_context
        no_context_sequence = no_context.split("VIETNAMESE BLOCK SEQUENCE\n", 1)[1]
        assert "[PRECEDING " not in no_context_sequence
        assert "[FOLLOWING " not in no_context_sequence
        assert row["target_active_vi"] in bounded
        assert no_context != bounded
        assert row["author_note"] not in no_context
        assert row["author_note"] not in bounded
        if row["stratum"] in {
            "P1_context_repair_risk",
            "P3_anaphora_false_alarm",
            "P5_context_import_bait",
        }:
            assert row["planted_marker"] not in no_context
            assert row["planted_marker"] in bounded


def test_all_pj_fixture_rows_take_declared_mechanical_or_both_order_path():
    fixture = load_default_scorer_probe_fixture_set()
    for row in fixture["pj_cases"]:
        presentations = prepare_pj_prompt_presentations_v3(_seal_pj_packet(row))
        if row["category"] == "identical":
            assert presentations.mechanical_equal
            assert presentations.canonical is None
            assert presentations.reversed is None
        else:
            assert not presentations.mechanical_equal
            assert presentations.canonical is not None
            assert presentations.reversed is not None
            prompt = presentations.canonical.rendered_prompt
            assert row["source_en"] in prompt
            assert row["candidate_a_vi"] in prompt
            assert row["candidate_b_vi"] in prompt
            assert row["author_note"] not in prompt


def test_fixture_validation_is_detached_and_does_not_mutate_input():
    raw = _raw()
    before = copy.deepcopy(raw)

    validated = validate_scorer_probe_fixture_set(raw)
    validated["pj_cases"][0]["author_note"] = "changed output copy"

    assert raw == before
    assert raw["pj_cases"][0]["author_note"] != "changed output copy"


def test_unknown_root_or_row_key_fails_closed():
    root = _raw()
    root["unexpected"] = True
    with pytest.raises(ContractValidationError, match="unknown_keys"):
        validate_scorer_probe_fixture_set(root)

    row = _raw()
    row["sf_bt_context_ablation"][0]["unexpected"] = True
    with pytest.raises(ContractValidationError, match="unknown_keys"):
        validate_scorer_probe_fixture_set(row)


def test_duplicate_case_id_fails_closed():
    payload = _raw()
    payload["sf_bt_context_ablation"][1]["case_id"] = (
        payload["sf_bt_context_ablation"][0]["case_id"]
    )
    with pytest.raises(ContractValidationError, match="duplicate"):
        validate_scorer_probe_fixture_set(payload)


def test_wrong_stratum_count_fails_closed():
    payload = _raw()
    payload["sf_bt_context_ablation"].pop()
    with pytest.raises(ContractValidationError, match="stratum_count"):
        validate_scorer_probe_fixture_set(payload)


def test_unbalanced_context_direction_fails_closed():
    payload = _raw()
    row = next(
        item
        for item in payload["sf_bt_context_ablation"]
        if item["case_id"] == "P1_06_day"
    )
    row["target_preceding_vi"] = row["target_following_vi"]
    row["target_following_vi"] = None
    with pytest.raises(ContractValidationError, match="context_direction_balance"):
        validate_scorer_probe_fixture_set(payload)


@pytest.mark.parametrize(
    "case_id",
    [
        "P1_01_time",
        "P2_01_bolts",
        "P3_01_mira",
        "P4_01_lock",
        "P5_01_weight",
    ],
)
def test_bad_marker_placement_fails_closed(case_id):
    payload = _raw()
    row = next(
        item
        for item in payload["sf_bt_context_ablation"]
        if item["case_id"] == case_id
    )
    row["planted_marker"] = "marker-that-appears-nowhere"
    with pytest.raises(ContractValidationError, match="marker_placement"):
        validate_scorer_probe_fixture_set(payload)


def test_measurement_must_match_stratum():
    payload = _raw()
    payload["sf_bt_context_ablation"][0]["measurement"] = "context_only_import"
    with pytest.raises(ContractValidationError, match="enum"):
        validate_scorer_probe_fixture_set(payload)


def test_missing_pj_category_or_bad_expected_label_fails_closed():
    missing = _raw()
    missing["pj_cases"] = [
        row for row in missing["pj_cases"] if row["category"] != "grammar"
    ]
    with pytest.raises(ContractValidationError, match="category_coverage"):
        validate_scorer_probe_fixture_set(missing)

    bad_label = _raw()
    bad_label["pj_cases"][0]["expected_overall"] = "candidate_1"
    with pytest.raises(ContractValidationError, match="enum"):
        validate_scorer_probe_fixture_set(bad_label)


def test_fixture_has_no_selected_book_or_runtime_instance_names():
    rendered = json.dumps(_raw(), ensure_ascii=False).casefold()
    forbidden = {
        "wuthering heights",
        "heathcliff",
        "catherine earnshaw",
        "lockwood",
        "the great gatsby",
        "jay gatsby",
        "multilayer perceptrons",
        "d2l_multilayer_perceptrons",
        "exp_s0s1",
    }
    assert not {value for value in forbidden if value in rendered}
