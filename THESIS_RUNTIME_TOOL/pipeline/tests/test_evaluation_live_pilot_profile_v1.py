from __future__ import annotations

import copy
import json

import pytest

from pipeline.eval.contracts_v1 import ContractValidationError
from pipeline.eval.live_pilot_preflight_v1 import (
    build_evaluation_live_pilot_preflight,
)
from pipeline.eval.live_pilot_profile_v1 import (
    build_evaluation_live_pilot_profile_v1,
    seal_evaluation_live_pilot_profile_v1,
    validate_evaluation_live_pilot_profile_binding_v1,
    validate_evaluation_live_pilot_profile_v1,
)
from pipeline.eval.llm_profiles_v1 import (
    EVALUATION_LLM_ROLE_IDS,
    PJ_JUDGE_ROLE_ID,
    SF_BT_BACK_TRANSLATOR_ROLE_ID,
    SF_BT_SEMANTIC_JUDGE_ROLE_ID,
    evaluation_role_contract_v1,
)
from pipeline.llm_backend import canonical_sha256
from pipeline.tests.test_evaluation_live_pilot_preflight_v1 import (
    COMMIT,
    NOW,
    _common,
    _config,
)


PROFILE_ID = "evaluation-d2l-mlp-live-pilot-google-row1-v1"
PROFILE_REVISION = "v1"
LOGICAL_RUN_ID = "evaluation-d2l-mlp-live-pilot-v1"
ATTEMPT_RUN_ID = "evaluation-d2l-mlp-live-pilot-v1-attempt-1"
OUTPUT_ROOT = "data/reports/evaluation_v1/d2l_mlp_live_pilot_v1"


def _source() -> dict:
    return {
        "schema_version": "api_source_v1",
        "source_id": "google-gemini-free-row1-v1",
        "source_revision": "gemini-free-row1-v1",
        "source_class": "remote_api",
        "adapter_id": "google_genai_generate_content_v1",
        "protocol": "google_genai_generate_content",
        "route_id": "models_generate_content",
        "endpoint_class": "remote",
        "base_url": "https://generativelanguage.googleapis.com/v1beta",
        "credential_ref": "shared.google.gemini_free.row1",
        "credential_commitment": "a" * 64,
        "physical_quota_bucket_id": "gemini-free-row1-v1",
        "enabled": True,
    }


def _models() -> dict[str, str]:
    return {
        SF_BT_BACK_TRANSLATOR_ROLE_ID: "gemma-4-31b-it",
        SF_BT_SEMANTIC_JUDGE_ROLE_ID: "gemini-3.5-flash",
        PJ_JUDGE_ROLE_ID: "gemini-3.5-flash",
    }


def _capabilities(source=None) -> dict[str, dict]:
    source = source or _source()
    result = {}
    for role_id, model_id in _models().items():
        contract = evaluation_role_contract_v1(role_id)
        result[role_id] = {
            "schema_version": "capability_evidence_v1",
            "capability_id": role_id.replace(".", "_") + "_row1_v1",
            "capability_revision": "probe_20260720_v1",
            "source_id": source["source_id"],
            "source_revision": source["source_revision"],
            "adapter_id": source["adapter_id"],
            "protocol": source["protocol"],
            "route_id": source["route_id"],
            "base_url": source["base_url"],
            "requested_model_id": model_id,
            "observed_model_id": model_id,
            "capability_kind": "native_structured_output",
            "schema_dialect": "json_schema_2020_12",
            "schema_sha256": contract["response_schema"]["sha256"],
            "local_validator_id": contract["validator"]["id"],
            "local_validator_sha256": contract["validator"]["sha256"],
            "verdict": "qualified",
            "probe_id": role_id.replace(".", "_") + "_schema_probe_v1",
            "evidence_sha256": "e" * 64,
            "observed_at_utc": NOW,
        }
    return result


def _preflight(common, config, *, count=4):
    return build_evaluation_live_pilot_preflight(
        common,
        config,
        created_at=NOW,
        producer_code_commit=COMMIT,
        selection_seed="profile-fixture-seed",
        requested_unit_count=count,
    )


def _bundle(
    *,
    count=4,
    source=None,
    capabilities=None,
    structured_output_mode="preferred",
):
    common = _common()
    config = _config(common)
    preflight = _preflight(common, config, count=count)
    source = source or _source()
    capabilities = capabilities or _capabilities(source)
    bundle = build_evaluation_live_pilot_profile_v1(
        common,
        config,
        preflight,
        source,
        capabilities,
        created_at=NOW,
        producer_code_commit=COMMIT,
        profile_id=PROFILE_ID,
        profile_revision=PROFILE_REVISION,
        evaluation_logical_run_id=LOGICAL_RUN_ID,
        evaluation_attempt_run_id=ATTEMPT_RUN_ID,
        output_root_relative=OUTPUT_ROOT,
        cache_mode="read_write",
        structured_output_mode=structured_output_mode,
    )
    return common, config, preflight, source, capabilities, bundle


def _validate(
    artifact,
    common,
    config,
    preflight,
    source,
    capabilities,
    *,
    structured_output_mode="preferred",
):
    return validate_evaluation_live_pilot_profile_binding_v1(
        artifact,
        common,
        config,
        preflight,
        expected_api_source=source,
        expected_capabilities_by_role=capabilities,
        expected_profile_id=PROFILE_ID,
        expected_profile_revision=PROFILE_REVISION,
        evaluation_logical_run_id=LOGICAL_RUN_ID,
        evaluation_attempt_run_id=ATTEMPT_RUN_ID,
        output_root_relative=OUTPUT_ROOT,
        cache_mode="read_write",
        expected_structured_output_mode=structured_output_mode,
    )


def _ckey_source() -> dict:
    source = _source()
    source.update(
        {
            "source_id": "ckey-xah-google-evaluation-v1",
            "source_revision": "ckey-xah-google-evaluation-test-v1",
            "adapter_id": "google_genai_rest_v1",
            "base_url": "https://api.xah.io/v1beta",
            "credential_ref": "shared.ckey.account-v1",
            "credential_commitment": "b" * 64,
            "physical_quota_bucket_id": "ckey-account-v1",
        }
    )
    return source


def _ckey_capabilities(source=None) -> dict[str, dict]:
    source = source or _ckey_source()
    capabilities = _capabilities(source)
    for row in capabilities.values():
        row.update(
            {
                "capability_kind": "json_object",
                "capability_revision": "third_party_json_object_test_v1",
                "requested_model_id": "vendor/gemini-3.5-flash",
                "observed_model_id": "gemini-3.5-flash",
            }
        )
    return capabilities


def test_builds_one_source_three_role_profile_and_call_envelope():
    common, config, preflight, source, capabilities, bundle = _bundle()
    artifact = bundle.artifact

    assert artifact["api_source"] == source
    assert {row["role_id"] for row in artifact["role_capabilities"]} == set(
        EVALUATION_LLM_ROLE_IDS
    )
    assert len(artifact["profile"]["role_bindings"]) == 3
    assert all(
        row["fallback_plan"] == {"enabled": False, "steps": []}
        for row in artifact["profile"]["role_bindings"]
    )
    physical = preflight["workload"]["physical_call_counts"]
    assert artifact["workload"]["scorer_api_call_count"] == physical[
        "total_api_calls"
    ]
    model_counts = {
        row["requested_model_id"]: row["call_count"]
        for row in artifact["workload"]["model_reservations"]
    }
    assert model_counts["gemma-4-31b-it"] == physical[
        "sf_bt_back_translation"
    ]
    assert model_counts["gemini-3.5-flash"] == (
        physical["sf_bt_semantic_judge"] + physical["pj_judge"]
    )
    assert _validate(
        artifact, common, config, preflight, source, capabilities
    ) == artifact


def test_prompt_validated_profile_accepts_exact_json_object_capability_and_reserves_proxy_usage():
    source = _ckey_source()
    capabilities = _ckey_capabilities(source)
    common, config, preflight, _, _, bundle = _bundle(
        source=source,
        capabilities=capabilities,
        structured_output_mode="prompt_validated",
    )
    roles = {
        row["role_id"]: row for row in bundle.profile["role_bindings"]
    }

    assert all(
        row["structured_output"]["mode"] == "prompt_validated"
        for row in roles.values()
    )
    assert roles[SF_BT_BACK_TRANSLATOR_ROLE_ID]["generation"][
        "max_output_tokens"
    ] == 4_096
    assert roles[SF_BT_BACK_TRANSLATOR_ROLE_ID]["limits"][
        "max_completion_tokens"
    ] == 4_096
    for role_id in (SF_BT_SEMANTIC_JUDGE_ROLE_ID, PJ_JUDGE_ROLE_ID):
        assert roles[role_id]["generation"]["max_output_tokens"] == 512
        assert roles[role_id]["limits"]["max_completion_tokens"] == 4_096
        assert roles[role_id]["limits"]["max_total_tokens"] == 16_096

    reservations = {
        row["role_id"]: row
        for row in bundle.artifact["workload"]["role_reservations"]
    }
    for role_id, role in roles.items():
        assert reservations[role_id]["reserved_max_completion_tokens"] == (
            reservations[role_id]["call_count"]
            * role["limits"]["max_completion_tokens"]
        )
    assert bundle.artifact["workload"][
        "reserved_max_completion_tokens"
    ] > preflight["workload"]["token_envelope"][
        "reserved_max_completion_tokens"
    ]
    assert _validate(
        bundle.artifact,
        common,
        config,
        preflight,
        source,
        capabilities,
        structured_output_mode="prompt_validated",
    ) == bundle.artifact


def test_output_mode_and_capability_kind_cannot_be_cross_substituted():
    source = _ckey_source()
    json_capabilities = _ckey_capabilities(source)
    with pytest.raises(ContractValidationError, match="capability kind"):
        _bundle(
            source=source,
            capabilities=json_capabilities,
            structured_output_mode="required",
        )

    native_capabilities = _capabilities(source)
    with pytest.raises(ContractValidationError, match="capability kind"):
        _bundle(
            source=source,
            capabilities=native_capabilities,
            structured_output_mode="prompt_validated",
        )


def test_profile_artifact_contains_no_secret_or_evaluation_authority():
    *_, bundle = _bundle()
    rendered = json.dumps(bundle.artifact, sort_keys=True)
    assert "credential_ref" in rendered
    assert "plaintext-secret" not in rendered
    assert "human_reference" not in rendered
    assert "gold" not in rendered
    assert "result_callback" not in rendered
    assert "winner" not in rendered


def test_forbidden_authority_identifier_fails_closed():
    source = _source()
    source["source_id"] = "human_reference_source_v1"
    capabilities = _capabilities(source)
    with pytest.raises(Exception, match="runtime authority token"):
        _bundle(source=source, capabilities=capabilities)


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("capability_kind", "json_object", "capability kind"),
        ("schema_sha256", "9" * 64, "exact role contract"),
        ("local_validator_sha256", "8" * 64, "exact role contract"),
        ("verdict", "unknown", "qualified capability"),
    ],
)
def test_unqualified_or_inexact_capability_fails(field, value, message):
    source = _source()
    capabilities = _capabilities(source)
    capabilities[PJ_JUDGE_ROLE_ID][field] = value
    with pytest.raises(ContractValidationError, match=message):
        _bundle(source=source, capabilities=capabilities)


def test_cross_source_capability_fails():
    source = _source()
    capabilities = _capabilities(source)
    capabilities[SF_BT_BACK_TRANSLATOR_ROLE_ID]["source_revision"] = "row2-v1"
    with pytest.raises(ContractValidationError, match="another source"):
        _bundle(source=source, capabilities=capabilities)


def test_resealed_source_substitution_fails_external_binding():
    common, config, preflight, source, capabilities, bundle = _bundle()
    changed = copy.deepcopy(bundle.artifact)
    changed["api_source"]["physical_quota_bucket_id"] = "gemini-free-row2-v1"
    changed["binding"]["physical_quota_bucket_id"] = "gemini-free-row2-v1"
    changed["binding"]["source_record_sha256"] = canonical_sha256(
        changed["api_source"]
    )
    for role in changed["profile"]["role_bindings"]:
        role["primary"]["source_record_sha256"] = changed["binding"][
            "source_record_sha256"
        ]
    changed["binding"]["profile_sha256"] = canonical_sha256(changed["profile"])
    changed = seal_evaluation_live_pilot_profile_v1(changed)

    validate_evaluation_live_pilot_profile_v1(changed)
    with pytest.raises(ContractValidationError, match="another run, source"):
        _validate(changed, common, config, preflight, source, capabilities)


def test_resealed_capability_model_substitution_fails_external_binding():
    common, config, preflight, source, capabilities, bundle = _bundle()
    changed = copy.deepcopy(bundle.artifact)
    row = next(
        item
        for item in changed["role_capabilities"]
        if item["role_id"] == PJ_JUDGE_ROLE_ID
    )
    row["capability"]["requested_model_id"] = "foreign-model"
    row["capability"]["observed_model_id"] = "foreign-model"
    cap_hash = canonical_sha256(row["capability"])
    role = next(
        item
        for item in changed["profile"]["role_bindings"]
        if item["role_id"] == PJ_JUDGE_ROLE_ID
    )
    role["primary"]["requested_model_id"] = "foreign-model"
    role["primary"]["capability_record_sha256"] = cap_hash
    reservation = next(
        item
        for item in changed["workload"]["role_reservations"]
        if item["role_id"] == PJ_JUDGE_ROLE_ID
    )
    reservation["requested_model_id"] = "foreign-model"
    by_model = {}
    for item in changed["workload"]["role_reservations"]:
        aggregate = by_model.setdefault(
            item["requested_model_id"],
            {
                "call_count": 0,
                "reserved_max_prompt_tokens": 0,
                "reserved_max_completion_tokens": 0,
                "reserved_max_total_tokens": 0,
            },
        )
        for field in aggregate:
            aggregate[field] += item[field]
    changed["workload"]["model_reservations"] = [
        {"requested_model_id": model_id, **by_model[model_id]}
        for model_id in sorted(by_model)
    ]
    changed["binding"]["profile_sha256"] = canonical_sha256(changed["profile"])
    changed = seal_evaluation_live_pilot_profile_v1(changed)

    validate_evaluation_live_pilot_profile_v1(changed)
    with pytest.raises(ContractValidationError, match="capability evidence differs"):
        _validate(changed, common, config, preflight, source, capabilities)


def test_unknown_key_and_unsafe_output_root_fail_closed():
    *_, bundle = _bundle()
    changed = copy.deepcopy(bundle.artifact)
    changed["profile"]["winner"] = "S1"
    with pytest.raises(ContractValidationError):
        seal_evaluation_live_pilot_profile_v1(changed)

    common = _common()
    config = _config(common)
    preflight = _preflight(common, config)
    with pytest.raises(ContractValidationError, match="relative POSIX"):
        build_evaluation_live_pilot_profile_v1(
            common,
            config,
            preflight,
            _source(),
            _capabilities(),
            created_at=NOW,
            producer_code_commit=COMMIT,
            profile_id=PROFILE_ID,
            profile_revision=PROFILE_REVISION,
            evaluation_logical_run_id=LOGICAL_RUN_ID,
            evaluation_attempt_run_id=ATTEMPT_RUN_ID,
            output_root_relative="C:\\foreign",
            cache_mode="read_write",
        )


def test_builder_does_not_mutate_inputs():
    common = _common()
    config = _config(common)
    preflight = _preflight(common, config)
    source = _source()
    capabilities = _capabilities(source)
    before = copy.deepcopy((common, config, preflight, source, capabilities))

    build_evaluation_live_pilot_profile_v1(
        common,
        config,
        preflight,
        source,
        capabilities,
        created_at=NOW,
        producer_code_commit=COMMIT,
        profile_id=PROFILE_ID,
        profile_revision=PROFILE_REVISION,
        evaluation_logical_run_id=LOGICAL_RUN_ID,
        evaluation_attempt_run_id=ATTEMPT_RUN_ID,
        output_root_relative=OUTPUT_ROOT,
        cache_mode="read_write",
    )

    assert (common, config, preflight, source, capabilities) == before
