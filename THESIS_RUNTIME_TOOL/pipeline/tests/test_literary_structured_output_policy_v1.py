from __future__ import annotations

import json
from pathlib import Path

import pytest

from pipeline.literary.structured_output_policy_v1 import (
    LiteraryStructuredOutputError,
    LiteraryStructuredOutputValidationError,
    gemini_response_json_schema,
    load_literary_structured_output_policy,
    openai_response_format,
    project_transport_schema_v1,
    resolve_structured_output_contract,
    validate_structured_payload,
)


RUNTIME_ROOT = Path(__file__).resolve().parents[2]
POLICY_PATH = (
    RUNTIME_ROOT
    / "pipeline"
    / "configs"
    / "literary_structured_output_policy_v1.json"
)


def _schema() -> dict[str, object]:
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["status", "values"],
        "properties": {
            "status": {"type": "string", "enum": ["ok"]},
            "values": {
                "type": "array",
                "minItems": 1,
                "uniqueItems": True,
                "items": {"type": "string", "minLength": 1},
            },
        },
    }


def test_ckey_auto_uses_prompt_plus_local_validation() -> None:
    policy = load_literary_structured_output_policy(POLICY_PATH)
    contract = resolve_structured_output_contract(
        policy,
        role_id="literary_b0",
        provider="google_genai",
        base_url="https://api.xah.io/",
        model_id="vuduythanh2023/gemini-3.5-flash",
        canonical_schema=_schema(),
    )

    assert contract.effective_mode == "prompt_plus_local_validation"
    assert contract.capability_status == "prompt_validated_only"
    assert gemini_response_json_schema(contract) is None
    assert contract.to_payload()["local_validation_required"] is True


def test_native_gemini_uses_schema_without_silently_losing_constraints() -> None:
    policy = load_literary_structured_output_policy(POLICY_PATH)
    contract = resolve_structured_output_contract(
        policy,
        role_id="literary_b0",
        provider="google_genai",
        base_url=None,
        model_id="gemini-3.5-flash",
        canonical_schema=_schema(),
    )
    transport_schema = gemini_response_json_schema(contract)

    assert contract.effective_mode == "native_schema"
    assert transport_schema is not None
    assert "minItems" not in transport_schema["properties"]["values"]  # type: ignore[index]
    omissions = contract.to_payload()["omitted_transport_constraints"]
    assert {row["keyword"] for row in omissions} == {
        "minItems",
        "minLength",
        "uniqueItems",
    }


def test_public_transport_projection_is_pure_and_allowlisted() -> None:
    canonical = _schema()
    before = json.dumps(canonical, sort_keys=True)
    projected, omissions = project_transport_schema_v1(canonical)

    assert json.dumps(canonical, sort_keys=True) == before
    assert projected["required"] == canonical["required"]
    assert projected["additionalProperties"] is False
    assert "minItems" not in projected["properties"]["values"]
    assert "uniqueItems" not in projected["properties"]["values"]
    assert "minLength" not in projected["properties"]["values"]["items"]
    assert {row["keyword"] for row in omissions} == {
        "minItems",
        "minLength",
        "uniqueItems",
    }


def test_openai_required_builds_strict_json_schema_format() -> None:
    policy = load_literary_structured_output_policy(POLICY_PATH)
    contract = resolve_structured_output_contract(
        policy,
        role_id="literary_local_conflict_auditor",
        provider="openai",
        base_url=None,
        model_id="gpt-5.4",
        canonical_schema=_schema(),
    )
    response_format = openai_response_format(
        contract, schema_name="literary_test_schema"
    )

    assert response_format["type"] == "json_schema"
    assert response_format["json_schema"]["strict"] is True
    assert response_format["json_schema"]["name"] == "literary_test_schema"


def test_required_role_rejects_unknown_or_proxy_route() -> None:
    policy = load_literary_structured_output_policy(POLICY_PATH)

    with pytest.raises(
        LiteraryStructuredOutputError,
        match="requires verified native",
    ):
        resolve_structured_output_contract(
            policy,
            role_id="literary_b2_interaction",
            provider="google_genai",
            base_url="https://api.xah.io",
            model_id="vuduythanh2023/gemini-3.5-flash",
            canonical_schema=_schema(),
        )


def test_local_validator_rejects_schema_valid_json_with_wrong_shape() -> None:
    validate_structured_payload(
        {"status": "ok", "values": ["alpha"]}, canonical_schema=_schema()
    )

    with pytest.raises(
        LiteraryStructuredOutputValidationError,
        match="violates canonical schema",
    ):
        validate_structured_payload(
            {"status": "ok", "values": []}, canonical_schema=_schema()
        )


def test_policy_rejects_duplicate_capability_match_key(tmp_path: Path) -> None:
    payload = json.loads(POLICY_PATH.read_text(encoding="utf-8"))
    duplicate = dict(payload["capabilities"][0])
    duplicate["capability_id"] = "duplicate-match"
    payload["capabilities"].append(duplicate)
    target = tmp_path / "policy.json"
    target.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(
        LiteraryStructuredOutputError,
        match="match key is duplicated",
    ):
        load_literary_structured_output_policy(target)
