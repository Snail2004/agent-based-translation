from __future__ import annotations

from pathlib import Path

import pytest

from pipeline.literary.structured_output_policy_v1 import (
    load_literary_structured_output_policy,
    resolve_structured_output_contract,
)
from pipeline.scripts.run_cross_chapter_claim_auditor_v1_live import (
    INTERNAL_UTC_DAY_TOKEN_CAP,
    INTERNAL_UTC_DAY_TOKEN_CAPS,
    PriorClaimLiveRunError,
    _resolved_response_format,
    _response_format,
    _select_component_id,
    _select_bucket,
)


POLICY_PATH = (
    Path(__file__).resolve().parents[1]
    / "configs"
    / "literary_structured_output_policy_v1.json"
)


def test_openai_transport_schema_keeps_semantics_in_runtime_validator() -> None:
    source_schema = {
        "type": "object",
        "additionalProperties": False,
        "required": ["rows"],
        "properties": {
            "rows": {
                "type": "array",
                "minItems": 1,
                "uniqueItems": True,
                "items": {"type": "string", "minLength": 1},
            }
        },
    }

    projected = _response_format(source_schema)
    transport_schema = projected["json_schema"]["schema"]

    assert projected["json_schema"]["strict"] is True
    assert transport_schema["required"] == ["rows"]
    assert transport_schema["additionalProperties"] is False
    assert "minItems" not in transport_schema["properties"]["rows"]
    assert "uniqueItems" not in transport_schema["properties"]["rows"]
    assert "minLength" not in transport_schema["properties"]["rows"]["items"]
    assert source_schema["properties"]["rows"]["uniqueItems"] is True


def test_prior_claim_transport_uses_the_sealed_structured_output_contract() -> None:
    schema = {
        "type": "object",
        "additionalProperties": False,
        "required": ["rows"],
        "properties": {"rows": {"type": "array", "items": {"type": "string"}}},
    }
    policy = load_literary_structured_output_policy(POLICY_PATH)
    contract = resolve_structured_output_contract(
        policy,
        role_id="literary_stable_claim_auditor",
        provider="openai",
        base_url=None,
        model_id="gpt-5.4",
        canonical_schema=schema,
    )

    response_format = _resolved_response_format(schema, contract)

    assert response_format["type"] == "json_schema"
    assert response_format["json_schema"]["strict"] is True
    assert response_format["json_schema"]["schema"] == contract.transport_schema


def test_quota_selector_uses_next_physical_bucket_without_collapsing_usage() -> None:
    preflight = {
        "usage_by_bucket_model": {
            "openai-row2|gpt-5.4": INTERNAL_UTC_DAY_TOKEN_CAP - 100,
            "openai-row1|gpt-5.4": 500,
        },
        "calls_by_bucket_model": {
            "openai-row2|gpt-5.4": 3,
            "openai-row1|gpt-5.4": 1,
        },
    }

    assert _select_bucket(preflight=preflight, reserve_tokens=200) == (
        "openai-row1",
        500,
        1,
    )

    with pytest.raises(PriorClaimLiveRunError):
        _select_bucket(
            preflight={
                "usage_by_bucket_model": {
                    "openai-row2|gpt-5.4": INTERNAL_UTC_DAY_TOKEN_CAP,
                    "openai-row1|gpt-5.4": INTERNAL_UTC_DAY_TOKEN_CAP,
                },
                "calls_by_bucket_model": {},
            },
            reserve_tokens=1,
        )


def test_quota_selector_accounts_for_mini_separately() -> None:
    preflight = {
        "usage_by_bucket_model": {
            "openai-row2|gpt-5.4": INTERNAL_UTC_DAY_TOKEN_CAP,
            "openai-row2|gpt-5.4-mini": 500,
        },
        "calls_by_bucket_model": {
            "openai-row2|gpt-5.4": 9,
            "openai-row2|gpt-5.4-mini": 1,
        },
    }

    assert _select_bucket(
        preflight=preflight,
        reserve_tokens=200,
        model_id="gpt-5.4-mini",
    ) == ("openai-row2", 500, 1)
    assert INTERNAL_UTC_DAY_TOKEN_CAPS["gpt-5.4-mini"] == 2_250_000

    with pytest.raises(PriorClaimLiveRunError, match="unsupported"):
        _select_bucket(
            preflight=preflight,
            reserve_tokens=1,
            model_id="foreign-model",
        )


def test_multiple_components_require_an_explicit_component_id() -> None:
    index = {
        "claim_components": [
            {"component_id": "comp_a", "overflow": False},
            {"component_id": "comp_b", "overflow": False},
            {"component_id": "comp_overflow", "overflow": True},
        ]
    }
    with pytest.raises(PriorClaimLiveRunError, match="requires --component-id"):
        _select_component_id(index, None)
    assert _select_component_id(index, "comp_b") == "comp_b"
    with pytest.raises(PriorClaimLiveRunError, match="absent"):
        _select_component_id(index, "comp_overflow")
