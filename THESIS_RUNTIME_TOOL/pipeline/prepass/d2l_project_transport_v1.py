"""Production D2L transport bridge for one sealed project campaign.

The campaign owns semantic roles and transport selection. Shared Backend owns
one physical request, quota locking, response-cache observation and attempt
usage persistence. This module only projects the campaign records into those
shared contracts; it never selects a fallback or changes a model.
"""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from typing import Any, Mapping

from pipeline.agents.llm_config import LLMConfig
from pipeline.llm_backend import (
    canonical_sha256,
    credential_commitment,
    validate_api_source,
    validate_capability_evidence,
)
from pipeline.llm_backend.credentials_v1 import CredentialProvider
from pipeline.llm_backend.transport_v1 import TransportSender
from pipeline.prepass.d2l_project_campaign_v2 import (
    D2LCampaignError,
    load_campaign,
    load_transport_attempt_seal,
)
from pipeline.prepass.d2l_shared_llm_adapter_v1 import (
    D2LSharedLlmAttemptAdapter,
    D2LSharedLlmClient,
)
from pipeline.prepass.d2l_shared_llm_profiles_v1 import D2LRolePreset


TRANSPORT_VERSION = "d2l_project_transport_v1"
STRUCTURED_OUTPUT = {"mode": "disabled", "schema_dialect": None}


class D2LProjectTransportError(RuntimeError):
    """Raised before transport when campaign/runtime identity disagrees."""


_CAPABILITY_RECORDS: dict[tuple[str, str, str], dict[str, Any]] = {
    (
        "shopaikey_gemini_proxy_v2",
        "prompt_json_v2",
        "gemini-3.5-flash",
    ): {
        "schema_version": "capability_evidence_v1",
        "capability_id": "d2l_shopaikey_gemini35_text_generation_v1",
        "capability_revision": "prompt_json_v2",
        "source_id": "shopaikey_gemini_proxy_v2",
        "source_revision": "prompt_json_v2",
        "adapter_id": "shared_urllib_google_genai_v1",
        "protocol": "google_genai_generate_content",
        "route_id": "generate_content",
        "base_url": "https://api.shopaikey.com/v1beta",
        "requested_model_id": "gemini-3.5-flash",
        "observed_model_id": "gemini-3.5-flash",
        "capability_kind": "text_generation",
        "schema_dialect": None,
        "schema_sha256": None,
        "local_validator_id": None,
        "local_validator_sha256": None,
        "probe_id": "d2l_shopaikey_prior_basic_generation_v1",
        "evidence_sha256": (
            "3fc7307bfcd8697efb306a7100a608a6f9a8bc3151e15daddbb67ea29d6f3565"
        ),
        "observed_at_utc": "2026-07-19T21:45:54.326Z",
        "verdict": "qualified",
    },
    ("modelapi_shared_v1", "modelapi_profile_v1", "gpt-5.4"): {
        "schema_version": "capability_evidence_v1",
        "capability_id": "d2l_modelapi_gpt54_text_generation_v1",
        "capability_revision": "chat_completions_prompt_json_probe_v1",
        "source_id": "modelapi_shared_v1",
        "source_revision": "modelapi_profile_v1",
        "adapter_id": "openai_python_v1",
        "protocol": "openai_chat_completions",
        "route_id": "chat_completions_create",
        "base_url": "https://modelapi.vn/v1",
        "requested_model_id": "gpt-5.4",
        "observed_model_id": "gpt-5.4",
        "capability_kind": "text_generation",
        "schema_dialect": None,
        "schema_sha256": None,
        "local_validator_id": None,
        "local_validator_sha256": None,
        "probe_id": "d2l_modelapi_gpt54_basic_generation_v1",
        "evidence_sha256": (
            "c23b101e20678d3cfba29a598c3142a94090952d6f53fa0e10fd4152439e0875"
        ),
        "observed_at_utc": "2026-07-20T19:38:00.604Z",
        "verdict": "qualified",
    },
    ("modelapi_shared_v1", "modelapi_profile_v1", "gpt-5.5"): {
        "schema_version": "capability_evidence_v1",
        "capability_id": "d2l_modelapi_gpt55_text_generation_v1",
        "capability_revision": "chat_completions_prompt_json_probe_v1",
        "source_id": "modelapi_shared_v1",
        "source_revision": "modelapi_profile_v1",
        "adapter_id": "openai_python_v1",
        "protocol": "openai_chat_completions",
        "route_id": "chat_completions_create",
        "base_url": "https://modelapi.vn/v1",
        "requested_model_id": "gpt-5.5",
        "observed_model_id": "gpt-5.5",
        "capability_kind": "text_generation",
        "schema_dialect": None,
        "schema_sha256": None,
        "local_validator_id": None,
        "local_validator_sha256": None,
        "probe_id": "d2l_modelapi_gpt55_basic_generation_v1",
        "evidence_sha256": (
            "8192c50145000f1071491a3d19e8a47aed41691152220161011d43b15e0ad15e"
        ),
        "observed_at_utc": "2026-07-20T16:17:07.871Z",
        "verdict": "qualified",
    },
}


def build_runtime_api_source(
    source_record: Mapping[str, Any],
    *,
    credential_provider: CredentialProvider,
) -> dict[str, Any]:
    """Bind an external credential commitment without serializing its value."""

    if source_record.get("output_mode") != "prompt_generated_json":
        raise D2LProjectTransportError(
            "production third-party source must use prompt-generated JSON"
        )
    if source_record.get("native_schema_parameter_sent") is not False:
        raise D2LProjectTransportError(
            "production third-party source may not send native schema"
        )
    credential_ref = str(source_record.get("credential_ref") or "")
    value = credential_provider.resolve(credential_ref)
    if value is None:
        raise D2LProjectTransportError(
            f"credential reference is unavailable: {credential_ref}"
        )
    try:
        return validate_api_source(
            {
                "schema_version": "api_source_v1",
                "source_id": source_record["source_id"],
                "source_revision": source_record["source_revision"],
                "source_class": source_record["source_class"],
                "adapter_id": source_record["adapter_id"],
                "protocol": source_record["protocol"],
                "route_id": source_record["route_id"],
                "endpoint_class": source_record["endpoint_class"],
                "base_url": source_record["base_url"],
                "credential_ref": credential_ref,
                "credential_commitment": credential_commitment(value),
                "physical_quota_bucket_id": source_record[
                    "physical_quota_bucket_id"
                ],
                "enabled": True,
            }
        )
    except Exception as exc:
        raise D2LProjectTransportError("runtime API source is invalid") from exc


def trusted_capability_for(
    api_source: Mapping[str, Any],
    *,
    model_id: str,
    override: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    key = (
        str(api_source["source_id"]),
        str(api_source["source_revision"]),
        model_id,
    )
    raw = deepcopy(dict(override)) if override is not None else deepcopy(
        _CAPABILITY_RECORDS.get(key)
    )
    if raw is None:
        raise D2LProjectTransportError(
            "source/revision/model lacks qualified text-generation evidence"
        )
    try:
        capability = validate_capability_evidence(raw)
    except Exception as exc:
        raise D2LProjectTransportError("capability evidence is invalid") from exc
    expected = {
        "source_id": api_source["source_id"],
        "source_revision": api_source["source_revision"],
        "adapter_id": api_source["adapter_id"],
        "protocol": api_source["protocol"],
        "route_id": api_source["route_id"],
        "base_url": api_source["base_url"],
        "requested_model_id": model_id,
        "observed_model_id": model_id,
        "capability_kind": "text_generation",
        "verdict": "qualified",
    }
    if any(capability[field] != value for field, value in expected.items()):
        raise D2LProjectTransportError(
            "capability evidence differs from the sealed source/model"
        )
    return capability


def role_preset(role: Mapping[str, Any], source_id: str) -> D2LRolePreset:
    generation = {
        "context_window_tokens": None,
        **deepcopy(dict(role["generation"])),
    }
    semantic_retries = int(role["semantic_retry_cap"])
    role_id = str(role["role_id"])
    revision = str(role["semantic_role_sha256"])[:16].lower()
    return D2LRolePreset(
        role_id=role_id,
        preset_id=f"{role_id}.production_{revision}",
        preset_revision="campaign_v2",
        lifecycle="active",
        source_choice=source_id,
        requested_model_id=str(role["model_id"]),
        generation=generation,
        transport_retry={
            "max_retries": 0,
            "backoff_policy": "none",
            "initial_delay_ms": 0,
            "max_delay_ms": 0,
            "retryable_codes": [],
        },
        semantic_retry={
            "max_retries": semantic_retries,
            "retryable_categories": (
                ["pipeline_semantic"] if semantic_retries else []
            ),
        },
        namespaces={
            "output": f"{role_id}.{revision}.output",
            "checkpoint": f"{role_id}.{revision}.checkpoint",
            "cache": f"{role_id}.{revision}.cache",
        },
    )


def role_artifact_refs(
    role: Mapping[str, Any],
) -> tuple[dict[str, str], dict[str, str], dict[str, str], dict[str, str]]:
    role_id = str(role["role_id"])
    role_hash = str(role["semantic_role_sha256"])
    prompt = role["prompt"]
    return (
        {
            "id": str(prompt["id"]),
            "revision": "campaign_v2",
            "sha256": str(prompt["sha256"]).lower(),
        },
        {
            "id": f"{role_id}.canonical_response_contract",
            "revision": "campaign_v2",
            "sha256": str(role["response_schema_sha256"]).lower(),
        },
        {
            "id": str(role["validator_id"]),
            "revision": "campaign_v2",
            "sha256": str(role["validator_sha256"]).lower(),
        },
        {
            "id": f"{role_id}.campaign_semantics",
            "schema_version": "d2l_project_campaign_role_v2",
            "sha256": role_hash.lower(),
        },
    )


def role_limits(
    campaign_config: Mapping[str, Any], role_id: str
) -> dict[str, Any]:
    row = next(
        (
            item
            for item in campaign_config["limits"]["roles"]
            if item["role_id"] == role_id
        ),
        None,
    )
    if row is None:
        raise D2LProjectTransportError(f"role limit is missing: {role_id}")
    return {
        "max_calls": int(row["physical_attempt_cap"]),
        "max_prompt_tokens": int(row["max_input_tokens_per_attempt"])
        * int(row["physical_attempt_cap"]),
        "max_completion_tokens": int(row["max_output_tokens_per_attempt"])
        * int(row["physical_attempt_cap"]),
        "max_total_tokens": int(row["max_total_tokens"]),
        "max_cost_usd": None,
        "request_timeout_ms": 300_000,
    }


class D2LProjectTransport:
    """Create role-specific clients over one campaign-local shared runtime."""

    def __init__(
        self,
        *,
        campaign_root: str | Path,
        runtime_root: str | Path,
        credential_provider: CredentialProvider,
        sender: TransportSender | None = None,
    ) -> None:
        self.campaign_root = Path(campaign_root).resolve()
        self.loaded = load_campaign(self.campaign_root)
        self.credential_provider = credential_provider
        self.adapter = D2LSharedLlmAttemptAdapter(
            runtime_root=runtime_root,
            credential_provider=credential_provider,
            sender=sender,
        )

    def build_client(
        self,
        role_id: str,
        *,
        component_attempt_id: int = 1,
        transport_attempt_index: int = 1,
        capability_override: Mapping[str, Any] | None = None,
    ) -> D2LSharedLlmClient:
        config = self.loaded["config"]
        role = next(
            (row for row in config["semantic_roles"] if row["role_id"] == role_id),
            None,
        )
        if role is None:
            raise D2LProjectTransportError(f"unknown semantic role: {role_id}")
        try:
            attempt = load_transport_attempt_seal(
                self.campaign_root,
                role_id=role_id,
                transport_attempt_index=transport_attempt_index,
            )
        except D2LCampaignError as exc:
            raise D2LProjectTransportError(str(exc)) from exc
        if int(attempt["component_attempt_id"]) != component_attempt_id:
            raise D2LProjectTransportError(
                "transport attempt belongs to a different component attempt"
            )
        api_source = build_runtime_api_source(
            attempt["source"], credential_provider=self.credential_provider
        )
        capability = trusted_capability_for(
            api_source,
            model_id=str(role["model_id"]),
            override=capability_override,
        )
        preset = role_preset(role, str(api_source["source_id"]))
        prompt_ref, schema_ref, validator_ref, extension_ref = role_artifact_refs(role)
        limits = role_limits(config, role_id)
        generation = role["generation"]
        client_config = LLMConfig(
            model=str(role["model_id"]),
            temperature=float(generation["temperature"]),
            seed=generation["seed"],
            reasoning_effort=str(generation["reasoning_effort"]),
            verbosity=str(generation["verbosity"]),
            max_output_tokens=int(generation["max_output_tokens"]),
            daily_token_cap=int(config["limits"]["hard_total_token_cap"]),
            prompt_token_cap=int(generation["max_input_tokens"]),
        )
        return D2LSharedLlmClient(
            adapter=self.adapter,
            config=client_config,
            preset=preset,
            api_source=api_source,
            capability=capability,
            prompt_ref=prompt_ref,
            response_schema_ref=schema_ref,
            validator_ref=validator_ref,
            semantic_extension_ref=extension_ref,
            structured_output=STRUCTURED_OUTPUT,
            limits=limits,
            run_id=str(config["component_run_id"]),
            attempt_run_id=(
                f"{config['component_run_id']}.attempt_{component_attempt_id:04d}"
            ),
            stage_id=str(role["stage_id"]),
            google_response_json_schema=None,
        )

    def runtime_identity(self, role_id: str) -> str:
        role = next(
            row
            for row in self.loaded["config"]["semantic_roles"]
            if row["role_id"] == role_id
        )
        attempt = load_transport_attempt_seal(
            self.campaign_root, role_id=role_id, transport_attempt_index=1
        )
        return canonical_sha256(
            {
                "transport_version": TRANSPORT_VERSION,
                "campaign_sha256": self.loaded["seal"]["integrity"][
                    "payload_sha256"
                ],
                "role_sha256": role["semantic_role_sha256"],
                "transport_attempt_sha256": attempt["integrity"]["payload_sha256"],
            }
        )


__all__ = [
    "D2LProjectTransport",
    "D2LProjectTransportError",
    "STRUCTURED_OUTPUT",
    "TRANSPORT_VERSION",
    "build_runtime_api_source",
    "role_artifact_refs",
    "role_limits",
    "role_preset",
    "trusted_capability_for",
]
