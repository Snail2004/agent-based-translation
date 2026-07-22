from __future__ import annotations

import json
from pathlib import Path

import pytest

from pipeline.eval.contracts_v1 import ContractValidationError
from pipeline.eval.live_pilot_execution_run_v1 import (
    run_evaluation_live_pilot_execution_v1,
)
from pipeline.eval.live_pilot_preflight_v1 import (
    build_evaluation_live_pilot_canary_preflight,
    build_evaluation_live_pilot_preflight,
)
from pipeline.eval.llm_profiles_v1 import EVALUATION_LLM_ROLE_IDS
from pipeline.llm_backend import (
    ContractValidationError as SharedContractValidationError,
    MappingCredentialProvider,
    RawTransportResponse,
    canonical_json,
    credential_commitment,
)
from pipeline.llm_backend.transport_v1 import TransportCallError
from pipeline.tests.test_evaluation_live_pilot_preflight_v1 import (
    COMMIT,
    NOW,
    _common,
    _config,
)
from pipeline.tests.test_evaluation_method_executors_v1 import (
    _Clock,
    _FailingSender,
    _capability,
)


PROFILE_ID = "evaluation-live-pilot-execution-fixture-v1"
PROFILE_REVISION = "v1"
LOGICAL_RUN_ID = "evaluation-live-pilot-execution-fixture"
ATTEMPT_RUN_ID = "evaluation-live-pilot-execution-fixture-attempt-1"
OUTPUT_ROOT_RELATIVE = "pilot"
SECRET = "evaluation-live-pilot-secret-fixture"


class _GoogleSemanticSender:
    def __init__(self) -> None:
        self.calls = 0
        self.pj_calls = 0

    def send(self, request):
        self.calls += 1
        body = json.loads(request.body.decode("utf-8"))
        generation = body["generationConfig"]
        assert generation["thinkingConfig"] == {"thinkingBudget": 0}
        assert generation["responseMimeType"] == "application/json"
        assert isinstance(generation["responseJsonSchema"], dict)
        assert request.headers_for_transport()["x-goog-api-key"] == SECRET
        prompt = body["contents"][0]["parts"][0]["text"]
        if "independent Vietnamese-to-English back-translator" in prompt:
            output = {"back_translation": "English active claim."}
        elif "You compare two English passages" in prompt:
            output = {
                "score": 75,
                "flags": ["coverage_mismatch"],
                "note": "minor drift",
            }
        elif "strict, impartial evaluator" in prompt:
            self.pj_calls += 1
            output = {
                "overall_verdict": (
                    "candidate_1" if self.pj_calls % 2 == 1 else "candidate_2"
                ),
                "style_verdict": (
                    "candidate_1" if self.pj_calls % 2 == 1 else "candidate_2"
                ),
                "tags": ["meaning"],
                "note": "one candidate preserves the active claim",
            }
        else:
            raise AssertionError("unexpected prompt")
        response = canonical_json(
            {
                "responseId": f"fixture-request-{self.calls}",
                "modelVersion": "evaluation-fixture-model",
                "candidates": [
                    {
                        "finishReason": "STOP",
                        "content": {
                            "parts": [
                                {"text": json.dumps(output, ensure_ascii=False)}
                            ]
                        },
                    }
                ],
                "usageMetadata": {
                    "promptTokenCount": 40,
                    "candidatesTokenCount": 12,
                    "totalTokenCount": 52,
                },
            }
        ).encode("utf-8")
        return RawTransportResponse(
            status_code=200,
            headers={},
            body=response,
            request_id=f"fixture-request-{self.calls}",
        )


class _GooglePromptValidatedSender(_GoogleSemanticSender):
    def send(self, request):
        self.calls += 1
        body = json.loads(request.body.decode("utf-8"))
        generation = body["generationConfig"]
        assert generation["thinkingConfig"] == {"thinkingBudget": 0}
        assert generation["responseMimeType"] == "application/json"
        assert "responseJsonSchema" not in generation
        prompt = body["contents"][0]["parts"][0]["text"]
        assert "Return one raw JSON object" in prompt
        assert request.headers_for_transport()["x-goog-api-key"] == SECRET
        if "independent Vietnamese-to-English back-translator" in prompt:
            output = {"back_translation": "English active claim."}
        elif "You compare two English passages" in prompt:
            output = {
                "score": 75,
                "flags": ["coverage_mismatch"],
                "note": "minor drift",
            }
        elif "strict, impartial evaluator" in prompt:
            self.pj_calls += 1
            output = {
                "overall_verdict": (
                    "candidate_1" if self.pj_calls % 2 == 1 else "candidate_2"
                ),
                "style_verdict": (
                    "candidate_1" if self.pj_calls % 2 == 1 else "candidate_2"
                ),
                "tags": ["meaning"],
                "note": "one candidate preserves the active claim",
            }
        else:
            raise AssertionError("unexpected prompt")
        response = canonical_json(
            {
                "responseId": f"fixture-prompt-request-{self.calls}",
                "modelVersion": "evaluation-fixture-model",
                "candidates": [
                    {
                        "finishReason": "STOP",
                        "content": {
                            "parts": [
                                {"text": json.dumps(output, ensure_ascii=False)}
                            ]
                        },
                    }
                ],
                "usageMetadata": {
                    "promptTokenCount": 40,
                    "candidatesTokenCount": 12,
                    "totalTokenCount": 2218,
                },
            }
        ).encode("utf-8")
        return RawTransportResponse(
            status_code=200,
            headers={},
            body=response,
            request_id=f"fixture-prompt-request-{self.calls}",
        )


class _FailAfterSender:
    def __init__(self, *, fail_on_call: int) -> None:
        self.calls = 0
        self.fail_on_call = fail_on_call
        self.delegate = _GoogleSemanticSender()

    def send(self, request):
        self.calls += 1
        if self.calls == self.fail_on_call:
            raise TransportCallError(
                code="http_429",
                status_code=429,
                safe_message="provider returned HTTP 429",
            )
        return self.delegate.send(request)


def _source() -> dict:
    return {
        "schema_version": "api_source_v1",
        "source_id": "google-gemini-free-row1-fixture-v1",
        "source_revision": "fixture-v1",
        "source_class": "remote_api",
        "adapter_id": "google_genai_rest_v1",
        "protocol": "google_genai_generate_content",
        "route_id": "models_generate_content",
        "endpoint_class": "remote",
        "base_url": "https://generativelanguage.googleapis.com/v1beta",
        "credential_ref": "fixture.google.row1",
        "credential_commitment": credential_commitment(SECRET),
        "physical_quota_bucket_id": "fixture-google-row1-v1",
        "enabled": True,
    }


def _ckey_source() -> dict:
    source = _source()
    source.update(
        {
            "source_id": "ckey-xah-google-evaluation-fixture-v1",
            "source_revision": "ckey-fixture-v1",
            "base_url": "https://api.xah.io/v1beta",
            "credential_ref": "fixture.ckey.account",
            "physical_quota_bucket_id": "fixture-ckey-account-v1",
        }
    )
    return source


class _Predictor:
    checkpoint_sha256 = "6" * 64

    def __init__(self) -> None:
        self.describe_calls = 0
        self.batch_calls = 0

    def describe_runtime(self):
        self.describe_calls += 1
        return {
            "schema_id": "CometKiwiRuntimeDescriptionV1",
            "package_name": "unbabel-comet",
            "package_version": "2.2.7",
            "python_version": "3.11.9",
            "device": "cpu",
            "checkpoint_sha256": self.checkpoint_sha256,
        }

    def __call__(self, rows, batch_size):
        self.batch_calls += 1
        assert batch_size == 8
        return [0.8 for _row in rows]


def _inputs(*, canary: bool = False, structured_output_mode: str = "preferred"):
    common = _common()
    config = _config(common)
    if canary:
        preflight = build_evaluation_live_pilot_canary_preflight(
            common,
            config,
            created_at=NOW,
            producer_code_commit=COMMIT,
            selection_seed="execution-run-canary-fixture-seed",
        )
    else:
        preflight = build_evaluation_live_pilot_preflight(
            common,
            config,
            created_at=NOW,
            producer_code_commit=COMMIT,
            selection_seed="execution-run-fixture-seed",
            requested_unit_count=4,
        )
    source = (
        _ckey_source()
        if structured_output_mode == "prompt_validated"
        else _source()
    )
    capabilities = {
        role_id: _capability(role_id, source)
        for role_id in EVALUATION_LLM_ROLE_IDS
    }
    if structured_output_mode == "prompt_validated":
        for capability in capabilities.values():
            capability["capability_kind"] = "json_object"
    return common, config, preflight, source, capabilities


def _run(
    root: Path,
    *,
    sender,
    predictor: _Predictor,
    profile_revision: str = PROFILE_REVISION,
    output_root_relative: str = OUTPUT_ROOT_RELATIVE,
    credential_provider=None,
    canary: bool = False,
    structured_output_mode: str = "preferred",
):
    common, config, preflight, source, capabilities = _inputs(
        canary=canary,
        structured_output_mode=structured_output_mode,
    )
    return run_evaluation_live_pilot_execution_v1(
        common,
        config,
        preflight,
        api_source=source,
        capabilities_by_role=capabilities,
        credential_provider=(
            credential_provider
            if credential_provider is not None
            else MappingCredentialProvider({source["credential_ref"]: SECRET})
        ),
        sender=sender,
        sf_qe_predictor=predictor,
        output_base_root=root.parent,
        output_root_relative=output_root_relative,
        created_at=NOW,
        producer_code_commit=COMMIT,
        profile_id=PROFILE_ID,
        profile_revision=profile_revision,
        evaluation_logical_run_id=LOGICAL_RUN_ID,
        evaluation_attempt_run_id=ATTEMPT_RUN_ID,
        sf_qe_batch_size=8,
        clock=_Clock(),
        structured_output_mode=structured_output_mode,
    )


def test_four_unit_fake_transport_run_and_complete_replay(tmp_path: Path) -> None:
    root = tmp_path / "pilot"
    sender = _GoogleSemanticSender()
    predictor = _Predictor()

    first = _run(root, sender=sender, predictor=predictor)
    first_calls = sender.calls
    second = _run(root, sender=sender, predictor=predictor)

    assert first.reused_complete_run is False
    assert second.reused_complete_run is True
    assert first.execution == second.execution
    assert first_calls == 24
    assert sender.calls == first_calls
    assert predictor.describe_calls == 1
    assert predictor.batch_calls == 1
    assert first.execution["coverage"] == {
        "selected_unit_count": 4,
        "selected_job_count": 20,
        "succeeded_job_count": 20,
        "failed_job_count": 0,
        "method_job_counts": {"pj": 4, "sf_bt": 8, "sf_qe": 8},
    }
    assert first.execution["claim"] == {
        "scope": "calibration_only",
        "status": "inconclusive",
        "verdict": "INCONCLUSIVE",
        "reason_code": "pilot_not_headline_evidence",
    }
    assert {item.name for item in root.iterdir()} == {
        "_state",
        ".gitattributes",
        "profile.json",
        "local_sf_qe_binding.json",
        "execution.json",
    }
    assert (root / ".gitattributes").read_bytes() == (
        b"*.json text eol=lf\n*.md text eol=lf\n"
    )
    public_bytes = b"".join(
        path.read_bytes()
        for path in (
            first.profile_path,
            first.local_sf_qe_path,
            first.execution_path,
        )
    )
    assert b"human_reference" not in public_bytes
    assert b"gold" not in public_bytes.lower()
    assert SECRET.encode("utf-8") not in public_bytes


def test_three_unit_canary_runs_eighteen_calls_and_stays_inconclusive(
    tmp_path: Path,
) -> None:
    root = tmp_path / "pilot"
    sender = _GoogleSemanticSender()
    predictor = _Predictor()

    result = _run(
        root,
        sender=sender,
        predictor=predictor,
        canary=True,
        structured_output_mode="required",
    )

    assert result.reused_complete_run is False
    assert sender.calls == 18
    assert result.execution["coverage"] == {
        "selected_unit_count": 3,
        "selected_job_count": 15,
        "succeeded_job_count": 15,
        "failed_job_count": 0,
        "method_job_counts": {"pj": 3, "sf_bt": 6, "sf_qe": 6},
    }
    assert result.execution["claim"] == {
        "scope": "calibration_only",
        "status": "inconclusive",
        "verdict": "INCONCLUSIVE",
        "reason_code": "pilot_not_headline_evidence",
    }
    assert all(
        row["structured_output"]["mode"] == "required"
        for row in result.profile["profile"]["role_bindings"]
    )


def test_prompt_validated_ckey_fake_transport_completes_without_native_schema(
    tmp_path: Path,
) -> None:
    root = tmp_path / "pilot"
    sender = _GooglePromptValidatedSender()
    predictor = _Predictor()

    result = _run(
        root,
        sender=sender,
        predictor=predictor,
        structured_output_mode="prompt_validated",
        credential_provider=MappingCredentialProvider(
            {"fixture.ckey.account": SECRET}
        ),
    )

    assert result.reused_complete_run is False
    assert sender.calls == 24
    assert result.execution["coverage"]["failed_job_count"] == 0
    assert all(
        row["structured_output"]["mode"] == "prompt_validated"
        for row in result.profile["profile"]["role_bindings"]
    )
    by_role = {
        row["role_id"]: row
        for row in result.profile["profile"]["role_bindings"]
    }
    assert by_role["evaluation.sf_bt.semantic_judge"]["generation"][
        "max_output_tokens"
    ] == 512
    assert by_role["evaluation.sf_bt.semantic_judge"]["limits"][
        "max_completion_tokens"
    ] == 4_096


def test_foreign_output_entry_fails_before_comet_or_transport(tmp_path: Path) -> None:
    root = tmp_path / "pilot"
    root.mkdir()
    (root / "foreign.json").write_text("{}", encoding="utf-8")
    sender = _GoogleSemanticSender()
    predictor = _Predictor()

    with pytest.raises(ContractValidationError, match="foreign entries"):
        _run(root, sender=sender, predictor=predictor)

    assert predictor.describe_calls == 0
    assert sender.calls == 0


def test_output_root_escape_fails_before_comet_or_transport(tmp_path: Path) -> None:
    sender = _GoogleSemanticSender()
    predictor = _Predictor()

    with pytest.raises(ContractValidationError, match="escapes"):
        _run(
            tmp_path / "pilot",
            sender=sender,
            predictor=predictor,
            output_root_relative="../foreign",
        )

    assert predictor.describe_calls == 0
    assert sender.calls == 0


def test_existing_profile_mismatch_fails_before_comet_or_transport(tmp_path: Path) -> None:
    root = tmp_path / "pilot"
    root.mkdir()
    (root / "profile.json").write_text(
        json.dumps({"profile": "foreign"}) + "\n", encoding="utf-8"
    )
    sender = _GoogleSemanticSender()
    predictor = _Predictor()

    with pytest.raises(ContractValidationError, match="differs from the sealed"):
        _run(root, sender=sender, predictor=predictor)

    assert predictor.describe_calls == 0
    assert sender.calls == 0


def test_wrong_credential_fails_before_comet_or_transport(tmp_path: Path) -> None:
    sender = _GoogleSemanticSender()
    predictor = _Predictor()

    with pytest.raises(SharedContractValidationError, match="commitment"):
        _run(
            tmp_path / "pilot",
            sender=sender,
            predictor=predictor,
            credential_provider=MappingCredentialProvider(
                {"fixture.google.row1": "wrong-secret-fixture-value"}
            ),
        )

    assert predictor.describe_calls == 0
    assert sender.calls == 0


def test_failed_physical_attempt_is_not_silently_retried(tmp_path: Path) -> None:
    root = tmp_path / "pilot"
    predictor = _Predictor()
    failing = _FailingSender()

    with pytest.raises(TransportCallError):
        _run(root, sender=failing, predictor=predictor)
    assert failing.calls == 1
    assert not (root / "execution.json").exists()
    halt = json.loads((root / "halt.json").read_text(encoding="utf-8"))
    assert halt["status"] == "halted"
    assert halt["binding"]["logical_run_id"] == LOGICAL_RUN_ID
    assert halt["binding"]["attempt_run_id"] == ATTEMPT_RUN_ID
    assert halt["progress"] == {
        "expected_physical_call_count": 24,
        "recorded_physical_attempt_count": 1,
        "succeeded_physical_attempt_count": 0,
        "failed_physical_attempt_count": 1,
        "unattempted_physical_call_count": 23,
        "unfinished_physical_call_count": 24,
        "known_prompt_tokens": 0,
        "known_completion_tokens": 0,
        "known_total_tokens": 0,
        "execution_published": False,
        "publishable": False,
    }
    assert halt["terminal_error"]["code"] == "http_500"
    assert halt["terminal_error"]["retry_disposition"] == "do_not_retry"

    replacement = _GoogleSemanticSender()
    replay_predictor = _Predictor()
    with pytest.raises(ContractValidationError, match="terminally halted"):
        _run(root, sender=replacement, predictor=replay_predictor)
    assert replacement.calls == 0
    assert replay_predictor.describe_calls == 0
    assert replay_predictor.batch_calls == 0


def test_halt_marker_tamper_fails_before_comet_or_transport(tmp_path: Path) -> None:
    root = tmp_path / "pilot"
    predictor = _Predictor()
    with pytest.raises(TransportCallError):
        _run(root, sender=_FailingSender(), predictor=predictor)

    halt_path = root / "halt.json"
    halt = json.loads(halt_path.read_text(encoding="utf-8"))
    halt["progress"]["publishable"] = True
    halt_path.write_text(canonical_json(halt) + "\n", encoding="utf-8")

    replacement = _GoogleSemanticSender()
    replay_predictor = _Predictor()
    with pytest.raises(ContractValidationError, match="halt hash differs"):
        _run(root, sender=replacement, predictor=replay_predictor)
    assert replacement.calls == 0
    assert replay_predictor.describe_calls == 0


def test_halt_marker_reconciles_successes_before_terminal_failure(
    tmp_path: Path,
) -> None:
    root = tmp_path / "pilot"
    sender = _FailAfterSender(fail_on_call=4)

    with pytest.raises(TransportCallError):
        _run(root, sender=sender, predictor=_Predictor())

    halt = json.loads((root / "halt.json").read_text(encoding="utf-8"))
    assert halt["progress"] == {
        "expected_physical_call_count": 24,
        "recorded_physical_attempt_count": 4,
        "succeeded_physical_attempt_count": 3,
        "failed_physical_attempt_count": 1,
        "unattempted_physical_call_count": 20,
        "unfinished_physical_call_count": 21,
        "known_prompt_tokens": 120,
        "known_completion_tokens": 36,
        "known_total_tokens": 156,
        "execution_published": False,
        "publishable": False,
    }
    assert halt["terminal_error"]["code"] == "http_429"
    assert halt["terminal_error"]["retry_class"] == "rate_limit"
