from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from pipeline.eval.contracts_v1 import ContractValidationError
from pipeline.eval.terminology_evidence.context_substitution.v2.contracts.common import (
    REQUIRED_SAME_SENSE_CONTEXT_TYPES,
)
from pipeline.eval.terminology_evidence.context_substitution.v2.contracts.input import (
    seal_context_substitution_input,
)
from pipeline.eval.terminology_evidence.context_substitution.v2.dataset.reviewed_support import (
    reviewed_support_to_context_substitution_input,
)
from pipeline.eval.terminology_evidence.context_substitution.v2.providers.base import (
    ContextProviderRoute,
    FailoverStructuredModel,
    ProviderRawResponse,
)
from pipeline.eval.terminology_evidence.context_substitution.v2.providers.ledger import (
    ProviderResponseLedger,
)
from pipeline.eval.terminology_evidence.context_substitution.v2.runtime.aggregation import (
    global_recommendation,
)
from pipeline.eval.terminology_evidence.context_substitution.v2.runtime.calibration import (
    FROZEN_POLICY_STATUS,
    frozen_validation_policy,
)
from pipeline.eval.terminology_evidence.context_substitution.v2.runtime.calibration_artifact import (
    build_calibration_artifact,
    validate_calibration_artifact,
)
from pipeline.eval.terminology_evidence.context_substitution.v2.runtime.engine import (
    _classify_and_select_contexts,
    run_d2l_context_substitution,
)


RUNTIME_ROOT = Path(__file__).resolve().parents[5]
DATASET_ROOT = RUNTIME_ROOT / "pipeline" / "eval" / "terminology_evidence" / "dataset"
V3 = DATASET_ROOT / "d2l_context_support_set_validation_ready_v3"
PILOT = DATASET_ROOT / "pilot_dev_only_v1_1"
HASH_A = "a" * 64
HASH_B = "b" * 64


def _calibration() -> dict:
    return build_calibration_artifact(
        dataset_manifest_sha256=HASH_A,
        gold_dataset_sha256=HASH_B,
        policy_version="d2l_context_status_calibrated_test_v1",
        supported_min_c=0.82,
        unsupported_below_c=0.58,
        supported_min_pass=4,
        supported_max_minor=1,
        unsupported_min_fail=2,
        second_judge_thresholds=(0.58, 0.7, 0.82),
        second_judge_tolerance=0.03,
        pairwise_close_margin=0.05,
        case_count=100,
        positive_case_count=50,
        negative_case_count=50,
        measured_auto_approval_precision=0.97,
    )


def test_calibration_requires_real_nonzero_self_hashed_artifact() -> None:
    artifact = _calibration()
    assert validate_calibration_artifact(artifact) == artifact
    policy = frozen_validation_policy(calibration_artifact=artifact)
    assert policy.policy_status == FROZEN_POLICY_STATUS
    assert policy.calibration_artifact_sha256 == artifact["integrity"]["artifact_sha256"]

    forged = copy.deepcopy(artifact)
    forged["dataset_manifest_sha256"] = "0" * 64
    with pytest.raises(ContractValidationError, match="zero hash"):
        validate_calibration_artifact(forged)


def test_provider_response_ledger_is_content_addressed_and_idempotent(tmp_path: Path) -> None:
    ledger = ProviderResponseLedger(tmp_path)
    first = ledger.capture('{"ok":true}')
    second = ledger.capture('{"ok":true}')
    assert first == second
    assert first["raw_response_storage_status"] == "STORED"
    target = tmp_path / first["raw_response_ref"]
    assert target.read_text(encoding="utf-8") == '{"ok":true}'
    assert len((tmp_path / "provider_attempts.jsonl").read_text(encoding="utf-8").splitlines()) == 2


def test_pinned_non_gemini_model_is_allowed_but_latest_alias_is_not() -> None:
    route = ContextProviderRoute(
        route_id="shopaikey_gemini",
        model_id="gpt-5.5-pinned-2026-07",
        model_family="gpt-5.5",
        independence_group="shopai-gpt-5.5",
        sender=lambda **_: ProviderRawResponse(text="{}", payload={}),
    )
    assert route.model_family == "gpt-5.5"
    with pytest.raises(ValueError, match="latest alias"):
        ContextProviderRoute(
            route_id="gemini_official",
            model_id="gemini-latest",
            sender=lambda **_: ProviderRawResponse(text="{}", payload={}),
        )


def test_model_captures_raw_response_before_validation(tmp_path: Path) -> None:
    payload = {"answer": "recorded"}
    route = ContextProviderRoute(
        route_id="ckey_gemini",
        model_id="gemini-3.5-flash-pinned",
        sender=lambda **_: ProviderRawResponse(
            text=json.dumps(payload, sort_keys=True),
            payload=payload,
            input_tokens=7,
            output_tokens=3,
        ),
    )
    model = FailoverStructuredModel(
        [route], response_ledger=ProviderResponseLedger(tmp_path)
    )
    with model.collect_calls() as collector:
        result, _ = model.call(
            role="context_selector",
            prompt_version="test-v1",
            system_prompt="test",
            payload={"input": True},
            response_schema={"type": "object"},
            validator=lambda value: dict(value),
            tag="capture",
        )
    assert result == payload
    assert collector.attempted_calls[0]["raw_response_storage_status"] == "STORED"
    assert (tmp_path / collector.attempted_calls[0]["raw_response_ref"]).is_file()


def test_frozen_selector_uses_reviewed_rows_without_model_call() -> None:
    payload = reviewed_support_to_context_substitution_input(
        PILOT, parent_v3_source=V3
    )["input"]
    term = copy.deepcopy(payload["terms"][0])
    for index, context in enumerate(term["contexts"]):
        if index < len(REQUIRED_SAME_SENSE_CONTEXT_TYPES):
            relation = "SAME_SENSE"
            context_type = REQUIRED_SAME_SENSE_CONTEXT_TYPES[index]
        else:
            relation = "CONTRASTIVE"
            context_type = "contrastive"
        context["reviewed_selection"] = {
            "sense_relation": relation,
            "context_type": context_type,
            "judgeability": "JUDGEABLE",
            "reason": "frozen human review",
            "review_row_sha256": HASH_A,
        }

    class NoModelCall:
        def call(self, **_: object) -> object:
            raise AssertionError("frozen selector must not call a provider")

    selected = _classify_and_select_contexts(
        model=NoModelCall(),
        term=term,
        selection_contract={"selector_mode": "FROZEN_HUMAN_REVIEWED_SELECTION"},
    )
    assert selected["provenance"] is None
    assert len(selected["same_sense"]) == 5
    assert selected["missing_same_sense_context_types"] == []
    assert selected["contrastive"]


def test_development_input_cannot_smuggle_frozen_reviewed_selection() -> None:
    payload = reviewed_support_to_context_substitution_input(
        PILOT, parent_v3_source=V3
    )["input"]
    forged = copy.deepcopy(payload)
    forged["terms"][0]["contexts"][0]["reviewed_selection"] = {
        "sense_relation": "SAME_SENSE",
        "context_type": "definition",
        "judgeability": "JUDGEABLE",
        "reason": "forged authority",
        "review_row_sha256": HASH_A,
    }
    with pytest.raises(ContractValidationError, match="cannot carry frozen"):
        seal_context_substitution_input(forged)


@pytest.mark.parametrize(
    "flag",
    ["MISSING_CONTRASTIVE_CONTEXT", "INCOMPLETE_CONTEXT_TYPE_COVERAGE"],
)
def test_incomplete_context_support_cannot_become_globally_eligible(flag: str) -> None:
    assert global_recommendation(
        contextual_status_value="CONTEXT_SUPPORTED",
        context_flags=[flag],
        threshold_policy_status=FROZEN_POLICY_STATUS,
    ) == "REQUIRES_GLOBAL_REVIEW"


def test_real_pilot_runs_end_to_end_with_captured_fake_provider(tmp_path: Path) -> None:
    input_payload = reviewed_support_to_context_substitution_input(
        PILOT, parent_v3_source=V3
    )["input"]
    target_id = input_payload["terms"][0]["candidate_targets"][0][
        "candidate_target_id"
    ]
    context_types = dict(enumerate(REQUIRED_SAME_SENSE_CONTEXT_TYPES))

    def sender(**kwargs: object) -> ProviderRawResponse:
        tag = str(kwargs["tag"])
        request = json.loads(str(kwargs["user_payload_json"]))
        if tag.startswith("selector:"):
            annotations = []
            for index, context in enumerate(request["contexts"]):
                same = index < 5
                annotations.append(
                    {
                        "context_id": context["context_id"],
                        "sense_relation": "SAME_SENSE" if same else "CONTRASTIVE",
                        "context_type": context_types[index] if same else "contrastive",
                        "judgeability": "JUDGEABLE",
                        "reason": "deterministic fixture classification",
                    }
                )
            response = {
                "term_id": request["term"]["term_id"],
                "sense_id": request["term"]["sense_id"],
                "scope_id": request["term"]["scope_id"],
                "annotations": annotations,
            }
        elif tag.startswith("trial-gate:"):
            response = {
                "context_id": request["trial"]["context_id"],
                "candidate_id": request["trial"]["candidate_id"],
                "trial_status": "VALID",
                "candidate_usage_valid": True,
                "external_translation_error": False,
                "missing_content": False,
                "added_content": False,
                "reason": "fixture translation is complete",
            }
        elif tag.startswith("trial:"):
            candidate = request["candidate_translation"]
            response = {
                "context_id": request["context_id"],
                "candidate_id": request["candidate_id"],
                "trial_translation": f"Bản thử dùng {candidate} trong ngữ cảnh.",
                "candidate_surface_used": candidate,
                "candidate_usage_confirmed": True,
                "applied_expansion": None,
            }
        elif tag.startswith("context-judge:"):
            response = {
                "context_id": request["source_context"]["context_id"],
                "candidate_id": request["candidate"]["candidate_id"],
                "judgeability": "JUDGEABLE",
                "scores": {
                    "semantic_equivalence": 4,
                    "domain_sense_fit": 2,
                    "collocation_naturalness": 2,
                    "grammatical_fit": 1,
                    "no_candidate_induced_distortion": 1,
                },
                "flags": {
                    "semantic_contradiction": False,
                    "wrong_sense": False,
                    "candidate_induced_distortion": False,
                    "translator_external_error": False,
                    "insufficient_context": False,
                },
                "evidence": {"source_span": "source", "target_span": "target"},
                "variant_observation": {
                    "surface_used": request["candidate"]["candidate_translation"],
                    "requires_expansion": False,
                    "suggested_expansion": None,
                },
                "reason": "fixture candidate preserves the source sense",
            }
        elif tag.startswith("contrastive:"):
            response = {
                "context_id": request["contrastive_context"]["context_id"],
                "candidate_id": request["candidate"]["candidate_id"],
                "tested_sense_id": request["tested_sense_id"],
                "result": "OUT_OF_SCOPE",
                "reason": "fixture contrastive boundary",
            }
        else:
            raise AssertionError(f"unexpected tag: {tag}")
        return ProviderRawResponse(
            text=json.dumps(response, ensure_ascii=False, sort_keys=True),
            payload=response,
            request_id=tag,
            input_tokens=11,
            output_tokens=7,
        )

    model = FailoverStructuredModel(
        [
            ContextProviderRoute(
                route_id="gemini_official",
                model_id="gemini-3.5-flash-pinned-test",
                sender=sender,
            )
        ],
        response_ledger=ProviderResponseLedger(tmp_path / "ledger"),
    )
    result = run_d2l_context_substitution(
        input_payload,
        model,
        candidate_target_ids=[target_id],
    )
    assert len(result["candidates"]) == 1
    assert result["candidates"][0]["contextual_evidence"]["C"] == 1.0
    assert result["execution_policy"]["raw_response_ledger_policy"] == (
        "CONTENT_ADDRESSED_V1"
    )
    assert result["usage"]["attempt_count"] == 18
    assert all(
        row["raw_response_storage_status"] == "STORED"
        for row in result["provider_attempts"]
    )
