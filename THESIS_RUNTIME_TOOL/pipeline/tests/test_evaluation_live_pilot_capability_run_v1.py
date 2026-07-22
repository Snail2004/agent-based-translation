from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta, timezone
import importlib.util
import json
from pathlib import Path

import pytest

from pipeline.eval.contracts_v1 import ContractValidationError
from pipeline.eval.live_pilot_capability_probe_v1 import (
    SHARED_CAPABILITY_PROBE_REVISION,
    build_evaluation_json_object_capability_probe_plan_v1,
)
from pipeline.eval.live_pilot_capability_run_v1 import (
    EVALUATION_CAPABILITY_PROBE_ROLE_ORDER,
    capabilities_by_role_from_probe_run_v1,
    run_evaluation_capability_probes_v1,
    validate_evaluation_capability_probe_run_summary_v1,
)
from pipeline.eval.llm_profiles_v1 import (
    PJ_JUDGE_ROLE_ID,
    SF_BT_BACK_TRANSLATOR_ROLE_ID,
    SF_BT_SEMANTIC_JUDGE_ROLE_ID,
)
from pipeline.llm_backend import (
    MappingCredentialProvider,
    RawTransportResponse,
    TransportCallError,
    canonical_json,
    credential_commitment,
)


SECRET = "evaluation-live-probe-secret-value"
MODELS = {
    SF_BT_BACK_TRANSLATOR_ROLE_ID: "gemini-2.5-flash",
    SF_BT_SEMANTIC_JUDGE_ROLE_ID: "gemini-3.5-flash",
    PJ_JUDGE_ROLE_ID: "gemini-3.5-flash",
}


def _source() -> dict:
    return {
        "schema_version": "api_source_v1",
        "source_id": "google-gemini-free-row1-v1",
        "source_revision": "gemini-free-row1-v1",
        "source_class": "remote_api",
        "adapter_id": "google_genai_rest_v1",
        "protocol": "google_genai_generate_content",
        "route_id": "models_generate_content",
        "endpoint_class": "remote",
        "base_url": "https://generativelanguage.googleapis.com/v1beta",
        "credential_ref": "shared.google.gemini_free.row1",
        "credential_commitment": credential_commitment(SECRET),
        "physical_quota_bucket_id": "gemini-free-row1-v1",
        "enabled": True,
    }


def _ckey_source() -> dict:
    return {
        "schema_version": "api_source_v1",
        "source_id": "ckey-evaluation-fixture-v1",
        "source_revision": "ckey-evaluation-fixture-revision-v1",
        "source_class": "remote_api",
        "adapter_id": "openai_compatible_chat_v1",
        "protocol": "openai_chat_completions",
        "route_id": "chat_completions",
        "endpoint_class": "remote",
        "base_url": "https://proxy.example.test/v1",
        "credential_ref": "shared.ckey.fixture",
        "credential_commitment": credential_commitment(SECRET),
        "physical_quota_bucket_id": "ckey-fixture-v1",
        "enabled": True,
    }


def _binding() -> dict:
    return {
        "shared_core_revision": SHARED_CAPABILITY_PROBE_REVISION,
        "consumer_revision": "1" * 40,
        "consumer_implementation_sha256": "2" * 64,
    }


def _payload(role_id: str) -> dict:
    return {
        SF_BT_BACK_TRANSLATOR_ROLE_ID: {
            "back_translation": "The system stores three rows."
        },
        SF_BT_SEMANTIC_JUDGE_ROLE_ID: {
            "score": 100,
            "flags": [],
            "note": "The synthetic passages preserve the same meaning.",
        },
        PJ_JUDGE_ROLE_ID: {
            "overall_verdict": "tie",
            "style_verdict": "tie",
            "tags": [],
            "note": "no meaningful difference",
        },
    }[role_id]


class _Clock:
    def __init__(self) -> None:
        start = datetime(2026, 7, 20, tzinfo=timezone.utc)
        self.values = [start + timedelta(seconds=index) for index in range(32)]

    def __call__(self) -> datetime:
        return self.values.pop(0)


class _QueuedGoogleSender:
    def __init__(self, *, fail_on_call: int | None = None) -> None:
        self.fail_on_call = fail_on_call
        self.calls = 0

    def send(self, request):
        self.calls += 1
        if self.calls == self.fail_on_call:
            raise TransportCallError(
                code="http_503",
                status_code=503,
                safe_message="provider returned HTTP 503",
            )
        role_id = EVALUATION_CAPABILITY_PROBE_ROLE_ORDER[self.calls - 1]
        model = MODELS[role_id]
        assert request.url.endswith(f"/models/{model}:generateContent")
        assert request.headers_for_transport()["x-goog-api-key"] == SECRET
        body = canonical_json(
            {
                "responseId": f"evaluation-probe-{self.calls}",
                "modelVersion": model,
                "candidates": [
                    {
                        "finishReason": "STOP",
                        "content": {
                            "parts": [
                                {
                                    "text": json.dumps(
                                        _payload(role_id), ensure_ascii=False
                                    )
                                }
                            ]
                        },
                    }
                ],
                "usageMetadata": {
                    "promptTokenCount": 90,
                    "candidatesTokenCount": 25,
                    "totalTokenCount": 115,
                },
            }
        ).encode("utf-8")
        return RawTransportResponse(
            status_code=200,
            headers={},
            body=body,
            request_id=f"evaluation-probe-{self.calls}",
        )


class _QueuedOpenAiCompatibleSender:
    def __init__(self) -> None:
        self.calls = 0

    def send(self, request):
        self.calls += 1
        role_id = EVALUATION_CAPABILITY_PROBE_ROLE_ORDER[self.calls - 1]
        model = MODELS[role_id]
        assert request.url == "https://proxy.example.test/v1/chat/completions"
        assert request.headers_for_transport()["authorization"] == f"Bearer {SECRET}"
        body = json.loads(request.body.decode("utf-8"))
        assert body["model"] == model
        assert body["response_format"] == {"type": "json_object"}
        assert "json_schema" not in json.dumps(body)
        response = {
            "id": f"evaluation-ckey-probe-{self.calls}",
            "model": model,
            "choices": [
                {
                    "finish_reason": "stop",
                    "message": {
                        "content": json.dumps(_payload(role_id), ensure_ascii=False)
                    },
                }
            ],
            "usage": {
                "prompt_tokens": 90,
                "completion_tokens": 25,
                "total_tokens": 115,
            },
        }
        return RawTransportResponse(
            status_code=200,
            headers={},
            body=canonical_json(response).encode("utf-8"),
            request_id=f"evaluation-ckey-probe-{self.calls}",
        )


def _run(tmp_path: Path, sender) -> dict:
    return run_evaluation_capability_probes_v1(
        source=_source(),
        requested_models_by_role=MODELS,
        accepted_observed_models_by_role={
            role_id: [model_id] for role_id, model_id in MODELS.items()
        },
        credential_provider=MappingCredentialProvider(
            {_source()["credential_ref"]: SECRET}
        ),
        output_root=tmp_path / "run",
        probe_run_prefix="evaluation_mlp_probe_20260720",
        issued_at_utc="2026-07-20T00:00:00Z",
        implementation_binding=_binding(),
        sender=sender,
        clock=_Clock(),
    )


def _run_ckey(tmp_path: Path, sender) -> dict:
    source = _ckey_source()
    return run_evaluation_capability_probes_v1(
        source=source,
        requested_models_by_role=MODELS,
        accepted_observed_models_by_role={
            role_id: [model_id] for role_id, model_id in MODELS.items()
        },
        credential_provider=MappingCredentialProvider(
            {source["credential_ref"]: SECRET}
        ),
        output_root=tmp_path / "ckey-run",
        probe_run_prefix="evaluation_ckey_probe_20260720",
        issued_at_utc="2026-07-20T00:00:00Z",
        implementation_binding=_binding(),
        sender=sender,
        clock=_Clock(),
        plan_builder=build_evaluation_json_object_capability_probe_plan_v1,
    )


def test_three_roles_qualify_sequentially_and_publish_reusable_evidence(
    tmp_path: Path,
) -> None:
    sender = _QueuedGoogleSender()
    summary = _run(tmp_path, sender)
    assert summary["status"] == "qualified"
    assert sender.calls == 3
    assert [row["role_id"] for row in summary["attempted_roles"]] == list(
        EVALUATION_CAPABILITY_PROBE_ROLE_ORDER
    )
    assert set(
        capabilities_by_role_from_probe_run_v1(
            summary, output_root=tmp_path / "run"
        )
    ) == set(
        EVALUATION_CAPABILITY_PROBE_ROLE_ORDER
    )
    stored = json.loads(
        (tmp_path / "run" / "run_summary.json").read_text(encoding="utf-8")
    )
    assert validate_evaluation_capability_probe_run_summary_v1(stored) == summary
    for row in summary["attempted_roles"]:
        for artifact in row["artifacts"].values():
            assert (tmp_path / "run" / artifact["path"]).is_file()


def test_three_roles_qualify_on_third_party_json_object_without_native_schema(
    tmp_path: Path,
) -> None:
    sender = _QueuedOpenAiCompatibleSender()
    summary = _run_ckey(tmp_path, sender)
    assert summary["status"] == "qualified"
    assert sender.calls == 3
    capabilities = capabilities_by_role_from_probe_run_v1(
        summary, output_root=tmp_path / "ckey-run"
    )
    assert set(capabilities) == set(EVALUATION_CAPABILITY_PROBE_ROLE_ORDER)
    assert {
        row["capability_kind"] for row in capabilities.values()
    } == {"json_object"}


def test_failure_halts_before_later_roles_and_cannot_authorize_profile(
    tmp_path: Path,
) -> None:
    sender = _QueuedGoogleSender(fail_on_call=2)
    summary = _run(tmp_path, sender)
    assert summary["status"] == "failed"
    assert sender.calls == 2
    assert summary["halt"] == {
        "role_id": SF_BT_SEMANTIC_JUDGE_ROLE_ID,
        "failure_code": "http_503",
    }
    assert len(summary["attempted_roles"]) == 2
    assert not (tmp_path / "run" / "roles" / "evaluation_pj_judge").exists()
    with pytest.raises(ContractValidationError, match="cannot authorize"):
        capabilities_by_role_from_probe_run_v1(
            summary, output_root=tmp_path / "run"
        )


def test_nonempty_output_root_fails_before_transport(tmp_path: Path) -> None:
    output_root = tmp_path / "run"
    output_root.mkdir()
    (output_root / "foreign.txt").write_text("occupied", encoding="utf-8")
    sender = _QueuedGoogleSender()
    with pytest.raises(ContractValidationError, match="absent or empty"):
        run_evaluation_capability_probes_v1(
            source=_source(),
            requested_models_by_role=MODELS,
            accepted_observed_models_by_role={
                role_id: [model_id] for role_id, model_id in MODELS.items()
            },
            credential_provider=MappingCredentialProvider(
                {_source()["credential_ref"]: SECRET}
            ),
            output_root=output_root,
            probe_run_prefix="evaluation_mlp_probe_20260720",
            issued_at_utc="2026-07-20T00:00:00Z",
            implementation_binding=_binding(),
            sender=sender,
            clock=_Clock(),
        )
    assert sender.calls == 0


def test_non_normalized_prefix_fails_before_output_or_transport(tmp_path: Path) -> None:
    output_root = tmp_path / "uppercase-prefix-run"
    sender = _QueuedGoogleSender()
    with pytest.raises(ContractValidationError, match="normalized lowercase"):
        run_evaluation_capability_probes_v1(
            source=_source(),
            requested_models_by_role=MODELS,
            accepted_observed_models_by_role={
                role_id: [model_id] for role_id, model_id in MODELS.items()
            },
            credential_provider=MappingCredentialProvider(
                {_source()["credential_ref"]: SECRET}
            ),
            output_root=output_root,
            probe_run_prefix="evaluation_probe_20260720T152919Z",
            issued_at_utc="2026-07-20T15:29:19Z",
            implementation_binding=_binding(),
            sender=sender,
            clock=_Clock(),
        )
    assert sender.calls == 0
    assert not output_root.exists()


def test_output_never_contains_plaintext_credential(tmp_path: Path) -> None:
    _run(tmp_path, _QueuedGoogleSender())
    secret = SECRET.encode("utf-8")
    for path in (tmp_path / "run").rglob("*"):
        if path.is_file():
            assert secret not in path.read_bytes(), path


def test_summary_tampering_and_foreign_source_evidence_fail_closed(
    tmp_path: Path,
) -> None:
    summary = _run(tmp_path, _QueuedGoogleSender())
    tampered = deepcopy(summary)
    tampered["attempted_roles"][0]["requested_model_id"] = "foreign-model"
    with pytest.raises(ContractValidationError):
        validate_evaluation_capability_probe_run_summary_v1(tampered)

    foreign = deepcopy(summary)
    foreign["attempted_roles"][0]["capability_evidence"]["source_id"] = (
        "foreign-source"
    )
    with pytest.raises(ContractValidationError):
        validate_evaluation_capability_probe_run_summary_v1(foreign)


def test_stored_probe_bundle_tampering_fails_before_capability_reuse(
    tmp_path: Path,
) -> None:
    summary = _run(tmp_path, _QueuedGoogleSender())
    result_ref = summary["attempted_roles"][0]["artifacts"]["probe_result"]
    result_path = tmp_path / "run" / result_ref["path"]
    result_path.write_text('{"status":"qualified"}\n', encoding="utf-8")
    with pytest.raises(ContractValidationError, match="missing or differs"):
        capabilities_by_role_from_probe_run_v1(
            summary, output_root=tmp_path / "run"
        )


def test_cli_key_loader_selects_one_row_without_exposing_siblings(
    tmp_path: Path,
) -> None:
    script = (
        Path(__file__).parents[1]
        / "scripts"
        / "run_evaluation_live_pilot_capability_probe_v1.py"
    )
    spec = importlib.util.spec_from_file_location("evaluation_probe_cli", script)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    rows = [
        f"fixture_google_credential_value_row_{index}" for index in range(1, 6)
    ]
    path = tmp_path / "keys.txt"
    path.write_text("\n".join(rows) + "\n", encoding="utf-8")
    selected = module.load_selected_google_credential_v1(
        path, physical_row=3, expected_row_count=5
    )
    assert selected == rows[2]
    with pytest.raises(ValueError, match="exactly 4"):
        module.load_selected_google_credential_v1(
            path, physical_row=1, expected_row_count=4
        )
