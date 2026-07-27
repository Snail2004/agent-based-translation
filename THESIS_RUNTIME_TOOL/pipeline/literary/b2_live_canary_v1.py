"""Bounded live canary runner for Literary B2 on one sealed chapter."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
import json
from pathlib import Path
from typing import Any, Mapping, Sequence

from pipeline.agents.judge_client import JudgeClient
from pipeline.agents.llm_client import LLMClient
from pipeline.agents.llm_config import LLMConfig
from pipeline.agents.provider_profile import (
    ProviderProfile,
    ProviderRole,
    ResolvedCredential,
    load_provider_profile,
    resolve_role_credential,
)
from pipeline.literary.b2_context_v1 import (
    B2PhaseAProfile,
    build_b2_windows_v1,
    load_b2_phase_a_profile,
    render_b2_frame_request_v1,
    render_b2_interaction_request_v1,
)
from pipeline.literary.b1_registry_to_b2_input_v1 import load_b2_source_input_v1
from pipeline.literary.b2_context_v2 import render_b2_interaction_request_v2
from pipeline.literary.b2_context_v3 import (
    render_b2_frame_request_v2,
    render_b2_interaction_request_v3,
)
from pipeline.literary.b2_contract_v1 import (
    B2ContractError,
    normalize_b2_frame_response_v1,
    normalize_b2_interaction_response_v1,
)
from pipeline.literary.b2_contract_v2 import normalize_b2_interaction_response_v2
from pipeline.literary.b2_contract_v3 import (
    normalize_b2_frame_response_v2,
    normalize_b2_interaction_response_v3,
)
from pipeline.literary.b2_review_routing_v1 import (
    ReviewRoutingError,
    route_review,
)
from pipeline.literary.chapter_cycle_shared_runtime_v1 import (
    BACKEND_MODE_LEGACY,
    BACKEND_MODE_SHARED_V1,
    LiterarySharedRunnerBindingsV1,
    build_literary_code_ref_v1,
    capability_binding_key,
)
from pipeline.literary.checkpoint import (
    canonical_hash,
    file_sha256,
    write_checkpoint_atomic,
)
from pipeline.literary.model_ref_v1 import (
    MODEL_REF_MODE_CLASSIFIED_V1,
    project_model_response_schema_v1,
)
from pipeline.literary.transport_json import (
    LiteraryTransportJsonError,
    parse_structured_response,
)
from pipeline.literary.structured_output_policy_v1 import (
    LiteraryStructuredOutputPolicy,
    StructuredOutputContract,
    gemini_response_json_schema,
    load_literary_structured_output_policy,
    openai_response_format,
    resolve_structured_output_contract,
    validate_structured_payload,
)
from pipeline.literary.structured_prompt_reserve_v1 import (
    structured_prompt_reserve_v1,
)
from pipeline.scripts.run_chapter_registry_v2_gemini import _gemini_transport
from pipeline.scripts.run_chapter_registry_v2_real import RESPONSE_FORMAT_JSON
from pipeline.scripts.run_chapter_registry_v4_real import FROZEN_DB_SHA256


CANARY_PROFILE_SCHEMA_VERSION = "literary_b2_ch1_canary_profile_v1"
CANARY_PROFILE_SCHEMA_VERSION_V2 = "literary_b2_ch1_canary_profile_v2"
CANARY_PROFILE_SCHEMA_VERSION_V3 = "literary_b2_ch1_canary_profile_v3"
CANARY_PROFILE_SCHEMA_VERSION_V4 = "literary_b2_ch1_canary_profile_v4"
RUN_SEAL_SCHEMA_VERSION = "literary_b2_ch1_canary_seal_v1"
RUN_SEAL_SCHEMA_VERSION_V2 = "literary_b2_ch1_canary_seal_v2"
RUN_SEAL_SCHEMA_VERSION_V3 = "literary_b2_ch1_canary_seal_v3"
RUN_SEAL_SCHEMA_VERSION_V4 = "literary_b2_ch1_canary_seal_v4_shared_backend"
INTERACTION_SEAL_SCHEMA_VERSION = "literary_b2_interaction_seal_v1"
INTERACTION_SEAL_SCHEMA_VERSION_V2 = "literary_b2_interaction_seal_v2"
INTERACTION_CONTRACT_PREREGISTER_SCHEMA_VERSION = (
    "literary_b2_interaction_contract_preregister_v1"
)
CHAPTER_ARTIFACT_SCHEMA_VERSION = "literary_b2_chapter_artifact_v1"
SLIM_CHAPTER_ARTIFACT_SCHEMA_VERSION = "literary_b2_slim_chapter_artifact_v1"
LIVE_REPORT_SCHEMA_VERSION = "literary_b2_ch1_canary_report_v1"
SLIM_LIVE_REPORT_SCHEMA_VERSION = "literary_b2_slim_canary_report_v1"

_SHARED_B2_ROLE_IDS = {
    "frame": "literary.b2.frame",
    "interaction": "literary.b2.interaction",
}


class B2LiveCanaryError(RuntimeError):
    """A sealed-input, transport, validation, or run-boundary failure."""


@dataclass(frozen=True)
class B2CanaryProfile:
    source_path: Path
    profile_id: str
    b2_profile_path: Path
    provider_profile_path: Path
    structured_output_policy_path: Path | None
    chapter_id: str
    frame_role_id: str
    interaction_role_id: str
    frame_contract_version: str
    interaction_contract_version: str
    frame_calls: int
    interaction_calls: int
    exception_calls: int
    max_total_calls: int
    max_retries_per_call: int
    hard_visible_token_cap: int
    prior_frame_candidate_carry_required: bool
    safety: Mapping[str, Any]
    profile_hash: str


def load_b2_canary_profile_v1(path: Path) -> B2CanaryProfile:
    source = Path(path).resolve()
    payload = _read_object(source, "B2 canary profile")
    schema_version = payload.get("schema_version")
    if schema_version not in {
        CANARY_PROFILE_SCHEMA_VERSION,
        CANARY_PROFILE_SCHEMA_VERSION_V2,
        CANARY_PROFILE_SCHEMA_VERSION_V3,
        CANARY_PROFILE_SCHEMA_VERSION_V4,
    }:
        raise B2LiveCanaryError("foreign B2 canary profile schema")
    expected_keys = {
        "schema_version",
        "profile_id",
        "b2_profile",
        "provider_profile",
        "chapter_id",
        "role_bindings",
        "limits",
        "safety",
    }
    if schema_version == CANARY_PROFILE_SCHEMA_VERSION_V2:
        expected_keys.add("structured_output_policy")
    if schema_version in {
        CANARY_PROFILE_SCHEMA_VERSION_V3,
        CANARY_PROFILE_SCHEMA_VERSION_V4,
    }:
        expected_keys.update({"structured_output_policy", "contract_versions"})
    _exact_keys(payload, expected_keys, "B2 canary profile")
    roles = _object(payload.get("role_bindings"), "role_bindings")
    _exact_keys(roles, {"frame", "interaction"}, "role_bindings")
    contract_versions = (
        _object(payload.get("contract_versions"), "contract_versions")
        if schema_version
        in {CANARY_PROFILE_SCHEMA_VERSION_V3, CANARY_PROFILE_SCHEMA_VERSION_V4}
        else {"frame": "v1", "interaction": "v1"}
    )
    _exact_keys(contract_versions, {"frame", "interaction"}, "contract_versions")
    contract_pair = (
        contract_versions.get("frame"),
        contract_versions.get("interaction"),
    )
    if contract_pair not in {("v1", "v1"), ("v1", "v2"), ("v2", "v3")}:
        raise B2LiveCanaryError("B2 canary contract versions are unsupported")
    limits = _object(payload.get("limits"), "limits")
    _exact_keys(
        limits,
        {
            "frame_calls",
            "interaction_calls",
            "exception_calls",
            "max_total_calls",
            "max_retries_per_call",
            "hard_visible_token_cap",
        },
        "limits",
    )
    safety = _object(payload.get("safety"), "safety")
    safety_keys = {
        "source_run_may_be_historical",
        "certification_claim_allowed",
        "semantic_review_action",
        "integrity_failure_action",
        "provider_fallback_allowed",
        "production_publish_enabled",
        "stop_after_chapter_id",
    }
    if schema_version == CANARY_PROFILE_SCHEMA_VERSION_V4:
        safety_keys.add("prior_frame_candidate_carry_required")
    _exact_keys(safety, safety_keys, "safety")
    if (
        safety.get("source_run_may_be_historical") is not True
        or safety.get("certification_claim_allowed") is not False
        or safety.get("semantic_review_action") != "persist_and_continue"
        or safety.get("integrity_failure_action") != "halt_before_next_call"
        or safety.get("provider_fallback_allowed") is not False
        or safety.get("production_publish_enabled") is not False
        or safety.get("stop_after_chapter_id") != payload.get("chapter_id")
        or (
            schema_version == CANARY_PROFILE_SCHEMA_VERSION_V4
            and not isinstance(
                safety.get("prior_frame_candidate_carry_required"), bool
            )
        )
    ):
        raise B2LiveCanaryError("B2 canary safety policy is not fail-closed")
    result = B2CanaryProfile(
        source_path=source,
        profile_id=_required_string(payload.get("profile_id"), "profile_id"),
        b2_profile_path=_sibling_file(
            source, payload.get("b2_profile"), "b2_profile"
        ),
        provider_profile_path=_sibling_file(
            source, payload.get("provider_profile"), "provider_profile"
        ),
        structured_output_policy_path=(
            _sibling_file(
                source,
                payload.get("structured_output_policy"),
                "structured_output_policy",
            )
            if schema_version
            in {
                CANARY_PROFILE_SCHEMA_VERSION_V2,
                CANARY_PROFILE_SCHEMA_VERSION_V3,
                CANARY_PROFILE_SCHEMA_VERSION_V4,
            }
            else None
        ),
        chapter_id=_required_string(payload.get("chapter_id"), "chapter_id"),
        frame_role_id=_required_string(roles.get("frame"), "frame role"),
        interaction_role_id=_required_string(
            roles.get("interaction"), "interaction role"
        ),
        frame_contract_version=str(contract_versions["frame"]),
        interaction_contract_version=str(contract_versions["interaction"]),
        frame_calls=_bounded_int(limits.get("frame_calls"), "frame_calls", 1, 1),
        interaction_calls=_bounded_int(
            limits.get("interaction_calls"), "interaction_calls", 1, 6
        ),
        exception_calls=_bounded_int(
            limits.get("exception_calls"), "exception_calls", 0, 0
        ),
        max_total_calls=_bounded_int(
            limits.get("max_total_calls"), "max_total_calls", 2, 7
        ),
        max_retries_per_call=_bounded_int(
            limits.get("max_retries_per_call"),
            "max_retries_per_call",
            0,
            0,
        ),
        hard_visible_token_cap=_bounded_int(
            limits.get("hard_visible_token_cap"),
            "hard_visible_token_cap",
            20_000,
            500_000,
        ),
        prior_frame_candidate_carry_required=(
            bool(safety.get("prior_frame_candidate_carry_required"))
            if schema_version == CANARY_PROFILE_SCHEMA_VERSION_V4
            else False
        ),
        safety=dict(safety),
        profile_hash=canonical_hash(payload),
    )
    if result.max_total_calls != result.frame_calls + result.interaction_calls:
        raise B2LiveCanaryError("B2 canary call caps do not exact-cover the run")
    return result


def authorize_b2_request_for_live_v1(
    request: Mapping[str, Any],
) -> dict[str, Any]:
    """Change only Phase-A transport metadata and re-fingerprint the request."""

    body = deepcopy(dict(request))
    observed = _required_string(
        body.pop("request_fingerprint", None), "request_fingerprint"
    )
    if canonical_hash(body) != observed:
        raise B2LiveCanaryError("B2 request fingerprint differs before authorization")
    if body.get("dependency_status") != "ready":
        raise B2LiveCanaryError("B2 request dependency is not ready for live use")
    reasons = body.get("api_ineligible_reasons")
    if body.get("api_eligible") is not False or not isinstance(reasons, list):
        raise B2LiveCanaryError("B2 Phase-A request has unexpected API metadata")
    unexpected = set(reasons).difference({"phase_a_zero_api"})
    if unexpected:
        raise B2LiveCanaryError(
            f"B2 request remains ineligible for live use: {sorted(unexpected)}"
        )
    body["api_eligible"] = True
    body["api_ineligible_reasons"] = []
    return {**body, "request_fingerprint": canonical_hash(body)}


def build_frame_context_for_window_v1(
    *,
    frame_artifact: Mapping[str, Any],
    window: Mapping[str, Any],
) -> dict[str, Any]:
    relevant_ids = set(window.get("active_block_ids") or []).union(
        window.get("preceding_tail_block_ids") or []
    )
    segments = [
        {
            "frame_segment_id": row.get("frame_segment_id"),
            "start_block_id": row.get("start_block_id"),
            "end_block_id": row.get("end_block_id"),
            "narrator_surface": row.get("narrator_surface"),
            "narrator_status": row.get("narrator_status"),
            "candidate_card_ids": list(row.get("candidate_card_ids") or []),
            "story_time_label": row.get("story_time_label"),
            "normalization_status": row.get("normalization_status"),
            "applicable_block_ids": [
                block_id
                for block_id in row.get("covered_block_ids") or []
                if block_id in relevant_ids
            ],
        }
        for row in frame_artifact.get("frame_segments") or []
        if relevant_ids.intersection(row.get("covered_block_ids") or [])
    ]
    if not segments:
        raise B2LiveCanaryError("B2 frame artifact does not cover the interaction window")
    reviews = [
        deepcopy(dict(row))
        for row in frame_artifact.get("review_requests") or []
        if relevant_ids.intersection(row.get("source_block_ids") or [])
    ]
    body = {
        "schema_version": "literary_b2_window_frame_context_v1",
        "frame_artifact_hash": _required_string(
            frame_artifact.get("artifact_hash"), "frame artifact hash"
        ),
        "chapter_orientation": deepcopy(
            dict(frame_artifact.get("chapter_orientation") or {})
        ),
        "applicable_segments": segments,
        "applicable_review_requests": reviews,
    }
    return {**body, "frame_context_hash": canonical_hash(body)}


def build_prior_frame_candidate_context_v1(
    *,
    prior_b2_root: Path,
    current_source_document_sha256: str,
    chapter_ids: Sequence[str],
    target_chapter_id: str,
    current_prefix_bundle: Mapping[str, Any],
) -> dict[str, Any]:
    """Load only resolved prior-frame narrator candidates as non-authoritative hints."""

    root = Path(prior_b2_root).resolve()
    if not root.is_dir():
        raise B2LiveCanaryError("prior B2 root is absent")
    tree_hash = _tree_hash(root)
    prior_seal = _verified_hashed_payload(
        root / "run_seal.json", hash_field="seal_hash", label="prior B2 run seal"
    )
    if prior_seal.get("schema_version") not in {
        RUN_SEAL_SCHEMA_VERSION,
        RUN_SEAL_SCHEMA_VERSION_V2,
        RUN_SEAL_SCHEMA_VERSION_V3,
        RUN_SEAL_SCHEMA_VERSION_V4,
    }:
        raise B2LiveCanaryError("prior B2 root has a foreign run seal")
    if prior_seal.get("source_document_sha256") != current_source_document_sha256:
        raise B2LiveCanaryError("prior B2 root belongs to another source document")
    ordered_chapters = [
        _required_string(value, "source chapter id") for value in chapter_ids
    ]
    if len(ordered_chapters) != len(set(ordered_chapters)):
        raise B2LiveCanaryError("source chapter order repeats a chapter id")
    try:
        target_index = ordered_chapters.index(target_chapter_id)
    except ValueError as exc:
        raise B2LiveCanaryError("target chapter is absent from source order") from exc
    if target_index == 0:
        raise B2LiveCanaryError("first chapter cannot require prior-frame carry")
    source_chapter_id = _required_string(
        prior_seal.get("chapter_id"), "prior B2 chapter id"
    )
    if source_chapter_id != ordered_chapters[target_index - 1]:
        raise B2LiveCanaryError("prior B2 root is not the preceding chapter")

    frame = _verified_hashed_artifact(
        root / "frame" / "frame_artifact.json", "prior B2 frame artifact"
    )
    if frame.get("chapter_id") != source_chapter_id:
        raise B2LiveCanaryError("prior frame artifact differs from its run seal")
    current_cards = list(current_prefix_bundle.get("b0_context_cards") or []) + list(
        current_prefix_bundle.get("candidate_only_context_cards") or []
    )
    current_ids = {
        _required_string(
            _object(row, "current prefix candidate card").get("prior_card_id"),
            "current prefix candidate card id",
        )
        for row in current_cards
    }
    by_card: dict[str, list[str]] = {}
    for raw_segment in frame.get("frame_segments") or []:
        segment = _object(raw_segment, "prior frame segment")
        if segment.get("normalization_status") != "accepted" or segment.get(
            "narrator_status"
        ) not in {"resolved_candidate", "ambiguous_candidates"}:
            continue
        segment_id = _required_string(
            segment.get("frame_segment_id"), "prior frame segment id"
        )
        candidate_ids = segment.get("candidate_card_ids")
        if not isinstance(candidate_ids, list) or not candidate_ids:
            raise B2LiveCanaryError(
                "candidate-bearing prior frame segment has no candidate card"
            )
        for value in candidate_ids:
            card_id = _required_string(value, "prior frame candidate card id")
            if card_id not in current_ids:
                raise B2LiveCanaryError(
                    "prior frame narrator candidate is absent from current prefix"
                )
            by_card.setdefault(card_id, [])
            if segment_id not in by_card[card_id]:
                by_card[card_id].append(segment_id)
    candidate_sources = [
        {
            "candidate_card_id": card_id,
            "source_kind": "prior_frame_narrator_candidate",
            "source_chapter_id": source_chapter_id,
            "source_artifact_hash": frame["artifact_hash"],
            "source_frame_segment_ids": sorted(segment_ids),
        }
        for card_id, segment_ids in sorted(by_card.items())
    ]
    body = {
        "schema_version": "literary_b2_prior_frame_candidate_context_v1",
        "prior_b2_root": str(root),
        "prior_b2_tree_hash": tree_hash,
        "prior_run_seal_hash": prior_seal["seal_hash"],
        "source_document_sha256": current_source_document_sha256,
        "source_chapter_id": source_chapter_id,
        "target_chapter_id": target_chapter_id,
        "prior_frame_artifact_hash": frame["artifact_hash"],
        "candidate_sources": candidate_sources,
    }
    return {**body, "context_hash": canonical_hash(body)}


def _validate_backend_mode_v1(
    *,
    backend_mode: str,
    credential_root: Path | None,
    shared_runtime: LiterarySharedRunnerBindingsV1 | None,
) -> None:
    if backend_mode not in {BACKEND_MODE_LEGACY, BACKEND_MODE_SHARED_V1}:
        raise B2LiveCanaryError("B2 backend mode is outside the closed enum")
    if backend_mode == BACKEND_MODE_SHARED_V1:
        if shared_runtime is None:
            raise B2LiveCanaryError("shared_v1 B2 requires a shared runtime")
        if credential_root is not None:
            raise B2LiveCanaryError(
                "shared_v1 B2 cannot receive a legacy credential root"
            )
    else:
        if shared_runtime is not None:
            raise B2LiveCanaryError("legacy B2 cannot receive a shared runtime")
        if credential_root is None:
            raise B2LiveCanaryError("legacy B2 requires a credential root")


def _shared_b2_role_binding_v1(
    *,
    role_name: str,
    response_schema: Mapping[str, Any],
    shared_runtime: LiterarySharedRunnerBindingsV1,
) -> dict[str, Any]:
    role_id = _SHARED_B2_ROLE_IDS[role_name]
    preset = shared_runtime.role_preset_for(role_id)
    source = shared_runtime.api_source_for(role_id)
    capability = shared_runtime.capability_for(
        role_id=role_id,
        response_schema=project_model_response_schema_v1(response_schema),
        binding_schema=response_schema,
    )
    return {
        "role_id": role_id,
        "model_id": preset.requested_model_id,
        "api_source_id": source["source_id"],
        "api_source_revision": source["source_revision"],
        "physical_quota_bucket_id": source["physical_quota_bucket_id"],
        "capability_binding_key": capability_binding_key(
            role_id, response_schema
        ),
        "capability_record_hash": canonical_hash(capability),
    }


def _verify_shared_b2_role_binding_v1(
    *,
    role_name: str,
    sealed_binding: Mapping[str, Any],
    shared_runtime: LiterarySharedRunnerBindingsV1,
) -> None:
    role_id = _SHARED_B2_ROLE_IDS[role_name]
    key = _required_string(
        sealed_binding.get("capability_binding_key"),
        f"shared {role_name} capability binding key",
    )
    capability = shared_runtime.capabilities.get(key)
    if capability is None:
        raise B2LiveCanaryError(
            f"shared {role_name} capability is absent after seal"
        )
    preset = shared_runtime.role_preset_for(role_id)
    source = shared_runtime.api_source_for(role_id)
    expected = {
        "role_id": role_id,
        "model_id": preset.requested_model_id,
        "api_source_id": source["source_id"],
        "api_source_revision": source["source_revision"],
        "physical_quota_bucket_id": source["physical_quota_bucket_id"],
        "capability_binding_key": key,
        "capability_record_hash": canonical_hash(capability),
    }
    if dict(sealed_binding) != expected:
        raise B2LiveCanaryError(
            f"shared {role_name} role binding differs from run seal"
        )


def prepare_b2_ch1_canary_v1(
    *,
    source_run_root: Path,
    output_root: Path,
    canary_profile_path: Path,
    credential_root: Path | None,
    frozen_db: Path,
    current_git_head: str,
    prior_b2_root: Path | None = None,
    backend_mode: str = BACKEND_MODE_LEGACY,
    shared_runtime: LiterarySharedRunnerBindingsV1 | None = None,
) -> dict[str, Any]:
    _validate_backend_mode_v1(
        backend_mode=backend_mode,
        credential_root=credential_root,
        shared_runtime=shared_runtime,
    )
    output = Path(output_root).resolve()
    if output.exists():
        raise B2LiveCanaryError("B2 canary output root must not exist")
    if file_sha256(frozen_db).upper() != FROZEN_DB_SHA256:
        raise B2LiveCanaryError("frozen DB differs from the accepted baseline")
    canary = load_b2_canary_profile_v1(canary_profile_path)
    b2_profile = load_b2_phase_a_profile(canary.b2_profile_path)
    provider: ProviderProfile | None = None
    frame_role: ProviderRole | None = None
    interaction_role: ProviderRole | None = None
    frame_credential: ResolvedCredential | None = None
    interaction_credential: ResolvedCredential | None = None
    if backend_mode == BACKEND_MODE_LEGACY:
        provider = load_provider_profile(canary.provider_profile_path)
        frame_role, interaction_role = _resolve_canary_roles(provider, canary)
        assert credential_root is not None
        frame_credential = resolve_role_credential(
            provider,
            role_id=canary.frame_role_id,
            credential_root=credential_root,
        )
        interaction_credential = resolve_role_credential(
            provider,
            role_id=canary.interaction_role_id,
            credential_root=credential_root,
        )
        if frame_role.bucket_order != (frame_credential.quota_bucket_id,):
            raise B2LiveCanaryError("B2 frame role has an unsealed fallback bucket")
        if interaction_role.bucket_order != (
            interaction_credential.quota_bucket_id,
        ):
            raise B2LiveCanaryError(
                "B2 interaction role has an unsealed fallback bucket"
            )

    source_root = Path(source_run_root).resolve()
    source_tree_hash = _tree_hash(source_root)
    real_input = load_b2_source_input_v1(
        source_root, current_git_head=current_git_head
    )
    chapter_row = _selected_chapter(real_input, canary.chapter_id)
    chapter = _object(chapter_row.get("chapter"), "chapter")
    prefix = _object(chapter_row.get("prefix_bundle"), "prefix bundle")
    prior_frame_context: dict[str, Any] | None = None
    if canary.prior_frame_candidate_carry_required:
        if prior_b2_root is None:
            raise B2LiveCanaryError(
                "sealed profile requires the preceding B2 chapter root"
            )
        prior_frame_context = build_prior_frame_candidate_context_v1(
            prior_b2_root=prior_b2_root,
            current_source_document_sha256=real_input["source_document_sha256"],
            chapter_ids=[
                _required_string(row.get("chapter_id"), "source chapter id")
                for row in real_input.get("chapters") or []
                if isinstance(row, Mapping)
            ],
            target_chapter_id=canary.chapter_id,
            current_prefix_bundle=prefix,
        )
    elif prior_b2_root is not None:
        raise B2LiveCanaryError(
            "prior B2 root was supplied to a profile that does not seal carry"
        )
    frame_request = authorize_b2_request_for_live_v1(
        _render_frame_request(
            contract_version=canary.frame_contract_version,
            chapter=chapter,
            prefix_bundle=prefix,
            profile=b2_profile,
            supplemental_candidate_sources=(
                prior_frame_context["candidate_sources"]
                if prior_frame_context is not None
                else []
            ),
        )
    )
    windows = build_b2_windows_v1(chapter, profile=b2_profile)
    if len(windows) != canary.interaction_calls:
        raise B2LiveCanaryError(
            "B2 Ch1 window count differs from the sealed interaction-call cap"
        )
    pending_requests = [
        _render_interaction_request(
            contract_version=canary.interaction_contract_version,
            window=window,
            prefix_bundle=prefix,
            profile=b2_profile,
            frame_context=None,
        )
        for window in windows
    ]
    structured_output_policy = (
        load_literary_structured_output_policy(
            canary.structured_output_policy_path
        )
        if (
            backend_mode == BACKEND_MODE_LEGACY
            and canary.structured_output_policy_path is not None
        )
        else None
    )
    if backend_mode == BACKEND_MODE_LEGACY:
        assert frame_role is not None and interaction_role is not None
        assert frame_credential is not None and interaction_credential is not None
        frame_contract = _resolve_b2_structured_contract(
            policy=structured_output_policy,
            role_id=canary.frame_role_id,
            role=frame_role,
            credential=frame_credential,
            canonical_schema=frame_request["response_schema"],
        )
        interaction_contracts = [
            _resolve_b2_structured_contract(
                policy=structured_output_policy,
                role_id=canary.interaction_role_id,
                role=interaction_role,
                credential=interaction_credential,
                canonical_schema=request["response_schema"],
            )
            for request in pending_requests
        ]
    else:
        assert shared_runtime is not None
        _shared_b2_role_binding_v1(
            role_name="frame",
            response_schema=frame_request["response_schema"],
            shared_runtime=shared_runtime,
        )
        for request in pending_requests:
            _shared_b2_role_binding_v1(
                role_name="interaction",
                response_schema=request["response_schema"],
                shared_runtime=shared_runtime,
            )
        frame_contract = None
        interaction_contracts = [None for _request in pending_requests]
    interaction_preregister_rows: list[dict[str, Any]] = []
    interaction_reserves: list[int] = []
    for window, request, contract in zip(
        windows, pending_requests, interaction_contracts, strict=True
    ):
        contract_payload = contract.to_payload() if contract is not None else None
        interaction_preregister_rows.append(
            {
                "window_id": window["window_id"],
                "window_hash": window["window_hash"],
                "pending_request_fingerprint": request["request_fingerprint"],
                "structured_output_contract_hash": (
                    canonical_hash(contract_payload)
                    if contract_payload is not None
                    else None
                ),
            }
        )
        interaction_reserves.append(
            _request_total_reserve(
                request, structured_output_contract=contract
            )
        )
    conservative_total = _request_total_reserve(
        frame_request, structured_output_contract=frame_contract
    ) + sum(interaction_reserves)
    if conservative_total > canary.hard_visible_token_cap:
        raise B2LiveCanaryError("B2 canary conservative reserve exceeds hard cap")

    if backend_mode == BACKEND_MODE_SHARED_V1:
        assert shared_runtime is not None
        shared_identity = shared_runtime.identity_payload()
        role_bindings = {
            "frame": _shared_b2_role_binding_v1(
                role_name="frame",
                response_schema=frame_request["response_schema"],
                shared_runtime=shared_runtime,
            ),
            "interaction": _shared_b2_role_binding_v1(
                role_name="interaction",
                response_schema=pending_requests[0]["response_schema"],
                shared_runtime=shared_runtime,
            ),
        }
    else:
        assert provider is not None
        assert frame_role is not None and interaction_role is not None
        assert frame_credential is not None and interaction_credential is not None
        shared_identity = None
        role_bindings = {
            "frame": {
                "role_id": canary.frame_role_id,
                "provider": frame_role.provider,
                "model_id": frame_role.model_id,
                "quota_bucket_id": frame_credential.quota_bucket_id,
                "credential_revision": frame_credential.credential_revision,
                "credential_commitment": frame_credential.commitment,
            },
            "interaction": {
                "role_id": canary.interaction_role_id,
                "provider": interaction_role.provider,
                "model_id": interaction_role.model_id,
                "quota_bucket_id": interaction_credential.quota_bucket_id,
                "credential_revision": interaction_credential.credential_revision,
                "credential_commitment": interaction_credential.commitment,
            },
        }

    seal_body = {
        "schema_version": (
            RUN_SEAL_SCHEMA_VERSION_V4
            if backend_mode == BACKEND_MODE_SHARED_V1
            else RUN_SEAL_SCHEMA_VERSION_V3
        ),
        "status": "sealed_before_api",
        "git_head": current_git_head,
        "output_root": str(output),
        "source_run_root": str(source_root),
        "source_tree_hash": source_tree_hash,
        "source_input_hash": real_input["input_hash"],
        "source_document_sha256": real_input["source_document_sha256"],
        "source_run_git_head": real_input["source_run_git_head"],
        "certification_eligible": False,
        "certification_blockers": sorted(
            set(real_input.get("certification_blockers") or [])
            | {"exploratory_b2_chapter_canary"}
        ),
        "chapter_id": canary.chapter_id,
        "contract_versions": {
            "frame": canary.frame_contract_version,
            "interaction": canary.interaction_contract_version,
        },
        "source_chapter_report_hash": chapter_row["chapter_report_hash"],
        "source_prefix_bundle_hash": chapter_row["prefix_bundle_hash"],
        "prior_frame_candidate_context": (
            None
            if prior_frame_context is None
            else {
                "prior_b2_root": prior_frame_context["prior_b2_root"],
                "prior_b2_tree_hash": prior_frame_context["prior_b2_tree_hash"],
                "prior_run_seal_hash": prior_frame_context["prior_run_seal_hash"],
                "prior_frame_artifact_hash": prior_frame_context[
                    "prior_frame_artifact_hash"
                ],
                "context_hash": prior_frame_context["context_hash"],
            }
        ),
        "b2_profile_id": b2_profile.profile_id,
        "b2_profile_hash": b2_profile.profile_hash,
        "b2_profile_sha256": file_sha256(canary.b2_profile_path),
        "canary_profile_id": canary.profile_id,
        "canary_profile_path": str(canary.source_path),
        "canary_profile_hash": canary.profile_hash,
        "canary_profile_sha256": file_sha256(canary.source_path),
        "provider_profile_id": provider.profile_id if provider is not None else None,
        "provider_profile_hash": provider.profile_hash if provider is not None else None,
        "provider_profile_sha256": (
            file_sha256(provider.source_path) if provider is not None else None
        ),
        "structured_output_policy": (
            None
            if structured_output_policy is None
            else {
                "path": str(structured_output_policy.source_path),
                "sha256": structured_output_policy.source_sha256,
                "policy_id": structured_output_policy.policy_id,
                "policy_hash": structured_output_policy.policy_hash,
            }
        ),
        "structured_output_contracts": {
            "frame": frame_contract.to_payload() if frame_contract else None,
            "interaction": {
                "schema_version": (
                    INTERACTION_CONTRACT_PREREGISTER_SCHEMA_VERSION
                ),
                "mode": "per_window_deferred_to_interaction_seal",
                "pending_windows": interaction_preregister_rows,
            },
        },
        "frame_request_fingerprint": frame_request["request_fingerprint"],
        "window_hashes": [row["window_hash"] for row in windows],
        "window_ids": [row["window_id"] for row in windows],
        "role_bindings": role_bindings,
        "limits": {
            "frame_calls": canary.frame_calls,
            "interaction_calls": canary.interaction_calls,
            "exception_calls": canary.exception_calls,
            "max_total_calls": canary.max_total_calls,
            "max_retries_per_call": canary.max_retries_per_call,
            "hard_visible_token_cap": canary.hard_visible_token_cap,
            "conservative_total_token_reserve": conservative_total,
        },
        "safety": dict(canary.safety),
        "frozen_db_sha256_before": FROZEN_DB_SHA256,
        "gold_or_oracle_loaded": False,
        "source_artifact_mutated": False,
        "production_publish_performed": False,
        "sealed_at": _now(),
    }
    if backend_mode == BACKEND_MODE_SHARED_V1:
        seal_body["backend_mode"] = BACKEND_MODE_SHARED_V1
        seal_body["shared_runtime_identity"] = shared_identity
    seal = {**seal_body, "seal_hash": canonical_hash(seal_body)}
    window_plan_body = {
        "schema_version": "literary_b2_ch1_window_plan_v1",
        "chapter_id": canary.chapter_id,
        "windows": [
            {
                "window_id": row["window_id"],
                "window_hash": row["window_hash"],
                "active_block_ids": list(row["active_block_ids"]),
                "preceding_tail_block_ids": list(
                    row["preceding_tail_block_ids"]
                ),
                "estimated_active_source_tokens": row[
                    "estimated_active_source_tokens"
                ],
            }
            for row in windows
        ],
    }
    window_plan = {
        **window_plan_body,
        "window_plan_hash": canonical_hash(window_plan_body),
    }
    output.mkdir(parents=True)
    _write_new_json(output / "run_seal.json", seal)
    _write_new_json(output / "window_plan.json", window_plan)
    if prior_frame_context is not None:
        _write_new_json(output / "prior_frame_context.json", prior_frame_context)
    _write_new_json(output / "frame" / "request.json", frame_request)
    if _tree_hash(source_root) != source_tree_hash:
        raise B2LiveCanaryError("source B1 artifact changed during canary preparation")
    return seal


def _shared_b2_usage_v1(stage_dir: Path) -> dict[str, Any]:
    receipt = _read_object(
        Path(stage_dir) / "shared_attempt_receipt.json",
        "shared B2 attempt receipt",
    )
    usage = receipt.get("usage")
    if not isinstance(usage, Mapping):
        raise B2LiveCanaryError(
            "shared B2 usage is unknown and cannot certify the finite run cap"
        )
    prompt = usage.get("prompt_tokens")
    completion = usage.get("completion_tokens")
    total = usage.get("total_tokens")
    if not all(isinstance(value, int) and value >= 0 for value in (prompt, completion, total)):
        raise B2LiveCanaryError(
            "shared B2 token usage is incomplete for the finite run cap"
        )
    if prompt + completion != total:
        raise B2LiveCanaryError("shared B2 token usage is internally inconsistent")
    return dict(usage)


def _execute_b2_frame_shared_v1(
    *,
    output: Path,
    state: Mapping[str, Any],
    request: Mapping[str, Any],
) -> dict[str, Any]:
    runtime = state.get("shared_runtime")
    if not isinstance(runtime, LiterarySharedRunnerBindingsV1):
        raise B2LiveCanaryError("shared B2 frame runtime is absent")
    seal = _object(state.get("seal"), "shared B2 seal")
    stage_dir = output / "frame"
    source = runtime.api_source_for(_SHARED_B2_ROLE_IDS["frame"])
    _shared_b2_role_binding_v1(
        role_name="frame",
        response_schema=_object(request.get("response_schema"), "frame schema"),
        shared_runtime=runtime,
    )
    _verify_b2_sealed_contract(seal=seal, role_name="frame", contract=None)
    _ensure_run_budget(
        output=output,
        next_request=request,
        hard_cap=int(seal["limits"]["hard_visible_token_cap"]),
        structured_output_contract=None,
    )
    _write_new_json(
        stage_dir / "stage_started.json",
        {
            "schema_version": "literary_b2_live_stage_started_v1",
            "backend_mode": BACKEND_MODE_SHARED_V1,
            "stage": "frame",
            "seal_hash": seal["seal_hash"],
            "request_fingerprint": request["request_fingerprint"],
            "api_source_id": source["source_id"],
            "physical_quota_bucket_id": source["physical_quota_bucket_id"],
            "started_at": _now(),
        },
    )

    response_schema = _object(request.get("response_schema"), "frame schema")
    contract_version = state["canary"].frame_contract_version
    frame_normalizer = _frame_normalizer(contract_version)

    def validate_frame(raw: Mapping[str, Any]) -> Mapping[str, Any]:
        validate_structured_payload(raw, canonical_schema=response_schema)
        return frame_normalizer(request=request, response=raw)

    result = runtime.execute_accepted_request(
        role_id=_SHARED_B2_ROLE_IDS["frame"],
        stage_id=f"b2_frame_{seal['chapter_id']}",
        logical_request_id=(
            f"b2_frame_{seal['chapter_id']}_{request['request_fingerprint'][:24]}"
        ),
        request=request,
        schema_name=f"literary_b2_frame_{contract_version}",
        semantic_validator=validate_frame,
        validator_ref=build_literary_code_ref_v1(
            identifier="literary.b2.frame.validator",
            revision=contract_version,
            callables=(
                validate_structured_payload,
                frame_normalizer,
            ),
        ),
        application_contract_id="literary.b2.frame.apply_v1",
        application_contract_revision=contract_version,
        output_dir=stage_dir,
        model_reference_mode=MODEL_REF_MODE_CLASSIFIED_V1,
        additional_input_bindings=(
            {"name": "b2_run_seal", "sha256": seal["seal_hash"]},
            {"name": "b1_source_input", "sha256": seal["source_input_hash"]},
            {
                "name": "b1_prefix_bundle",
                "sha256": seal["source_prefix_bundle_hash"],
            },
        ),
    )
    artifact = dict(result.semantic_payload)
    usage = _shared_b2_usage_v1(stage_dir)
    _write_new_json(stage_dir / "frame_artifact.json", artifact)
    _write_new_json(
        stage_dir / "stage_report.json",
        {
            "schema_version": "literary_b2_live_stage_report_v1",
            "status": "accepted",
            "backend_mode": BACKEND_MODE_SHARED_V1,
            "stage": "frame",
            "artifact_hash": artifact["artifact_hash"],
            "review_request_count": len(artifact["review_requests"]),
            "usage": usage,
            "quota_bucket_id": source["physical_quota_bucket_id"],
            "shared_attempt_receipt_sha256": file_sha256(
                stage_dir / "shared_attempt_receipt.json"
            ),
            "production_publish_performed": False,
            "completed_at": _now(),
        },
    )
    return artifact


def _execute_b2_interaction_shared_v1(
    *,
    output: Path,
    state: Mapping[str, Any],
    interaction_seal: Mapping[str, Any],
    request_row: Mapping[str, Any],
    request: Mapping[str, Any],
    stage_dir: Path,
) -> dict[str, Any]:
    runtime = state.get("shared_runtime")
    if not isinstance(runtime, LiterarySharedRunnerBindingsV1):
        raise B2LiveCanaryError("shared B2 interaction runtime is absent")
    seal = _object(state.get("seal"), "shared B2 seal")
    source = runtime.api_source_for(_SHARED_B2_ROLE_IDS["interaction"])
    actual_binding = _shared_b2_role_binding_v1(
        role_name="interaction",
        response_schema=_object(request.get("response_schema"), "interaction schema"),
        shared_runtime=runtime,
    )
    sealed_binding = request_row.get("shared_capability_binding")
    if not isinstance(sealed_binding, Mapping) or dict(sealed_binding) != actual_binding:
        raise B2LiveCanaryError(
            "shared interaction capability differs from its interaction seal"
        )
    _verify_b2_interaction_sealed_contract(
        seal=seal,
        interaction_seal=interaction_seal,
        request_row=request_row,
        contract=None,
    )
    _ensure_run_budget(
        output=output,
        next_request=request,
        hard_cap=int(seal["limits"]["hard_visible_token_cap"]),
        structured_output_contract=None,
    )
    _write_new_json(
        stage_dir / "stage_started.json",
        {
            "schema_version": "literary_b2_live_stage_started_v1",
            "backend_mode": BACKEND_MODE_SHARED_V1,
            "stage": "interaction",
            "window_id": request_row["window_id"],
            "interaction_seal_hash": interaction_seal["interaction_seal_hash"],
            "request_fingerprint": request["request_fingerprint"],
            "api_source_id": source["source_id"],
            "physical_quota_bucket_id": source["physical_quota_bucket_id"],
            "started_at": _now(),
        },
    )

    contract_version = state["canary"].interaction_contract_version

    def validate_interaction(raw: Mapping[str, Any]) -> Mapping[str, Any]:
        return _normalize_interaction_response(
            contract_version=contract_version,
            request=request,
            response=raw,
        )

    result = runtime.execute_accepted_request(
        role_id=_SHARED_B2_ROLE_IDS["interaction"],
        stage_id=f"b2_interaction_{seal['chapter_id']}",
        logical_request_id=(
            f"b2_interaction_{canonical_hash({'window_id': request_row['window_id'], 'request_fingerprint': request['request_fingerprint']})[:24]}"
        ),
        request=request,
        schema_name=f"literary_b2_interaction_{contract_version}",
        semantic_validator=validate_interaction,
        validator_ref=build_literary_code_ref_v1(
            identifier="literary.b2.interaction.validator",
            revision=contract_version,
            callables=(_normalize_interaction_response,),
        ),
        application_contract_id="literary.b2.interaction.apply_v1",
        application_contract_revision=contract_version,
        output_dir=stage_dir,
        model_reference_mode=MODEL_REF_MODE_CLASSIFIED_V1,
        additional_input_bindings=(
            {"name": "b2_run_seal", "sha256": seal["seal_hash"]},
            {
                "name": "b2_interaction_seal",
                "sha256": interaction_seal["interaction_seal_hash"],
            },
            {
                "name": "b2_frame_artifact",
                "sha256": interaction_seal["frame_artifact_hash"],
            },
        ),
    )
    artifact = dict(result.semantic_payload)
    usage = _shared_b2_usage_v1(stage_dir)
    _write_new_json(stage_dir / "interaction_artifact.json", artifact)
    raw = {
        "usage": usage,
        "quota_bucket_id": source["physical_quota_bucket_id"],
        "completed_at": _now(),
    }
    report = _interaction_stage_report(
        artifact=artifact,
        raw=raw,
        status="accepted",
    )
    report["backend_mode"] = BACKEND_MODE_SHARED_V1
    report["shared_attempt_receipt_sha256"] = file_sha256(
        stage_dir / "shared_attempt_receipt.json"
    )
    _write_new_json(stage_dir / "stage_report.json", report)
    return artifact


def execute_b2_frame_live_v1(
    *,
    output_root: Path,
    credential_root: Path | None,
    frozen_db: Path,
    current_git_head: str,
    backend_mode: str = BACKEND_MODE_LEGACY,
    shared_runtime: LiterarySharedRunnerBindingsV1 | None = None,
) -> dict[str, Any]:
    _validate_backend_mode_v1(
        backend_mode=backend_mode,
        credential_root=credential_root,
        shared_runtime=shared_runtime,
    )
    output = Path(output_root).resolve()
    artifact_path = output / "frame" / "frame_artifact.json"
    if artifact_path.is_file():
        state = _verify_prepared_run(
            output=output,
            credential_root=credential_root,
            frozen_db=frozen_db,
            current_git_head=current_git_head,
            backend_mode=backend_mode,
            shared_runtime=shared_runtime,
        )
        artifact = _verified_hashed_artifact(artifact_path, "frame artifact")
        if backend_mode == BACKEND_MODE_SHARED_V1:
            runtime = state.get("shared_runtime")
            if not isinstance(runtime, LiterarySharedRunnerBindingsV1):
                raise B2LiveCanaryError("shared B2 frame runtime is absent")
            _ensure_existing_shared_frame_stage_report_v1(
                stage_dir=output / "frame",
                artifact=artifact,
                shared_runtime=runtime,
            )
        _prepare_interaction_seal(
            output=output,
            state=state,
            frame_artifact=artifact,
        )
        return artifact
    if (output / "frame" / "stage_failure.json").is_file():
        raise B2LiveCanaryError("B2 frame stage has a recorded failure")
    state = _verify_prepared_run(
        output=output,
        credential_root=credential_root,
        frozen_db=frozen_db,
        current_git_head=current_git_head,
        backend_mode=backend_mode,
        shared_runtime=shared_runtime,
    )
    request = _read_object(output / "frame" / "request.json", "frame request")
    seal = state["seal"]
    if backend_mode == BACKEND_MODE_SHARED_V1:
        try:
            artifact = _execute_b2_frame_shared_v1(
                output=output,
                state=state,
                request=request,
            )
            _prepare_interaction_seal(
                output=output,
                state=state,
                frame_artifact=artifact,
            )
            _verify_post_call_invariants(seal=seal, frozen_db=frozen_db)
            return artifact
        except Exception as exc:
            _write_new_json(
                output / "frame" / "stage_failure.json",
                _failure_payload("frame", exc, frozen_db),
            )
            raise
    credential = state["frame_credential"]
    role = state["frame_role"]
    contract = _resolve_b2_structured_contract(
        policy=state["structured_output_policy"],
        role_id=state["canary"].frame_role_id,
        role=role,
        credential=credential,
        canonical_schema=request["response_schema"],
    )
    _verify_b2_sealed_contract(
        seal=seal, role_name="frame", contract=contract
    )
    _ensure_run_budget(
        output=output,
        next_request=request,
        hard_cap=int(seal["limits"]["hard_visible_token_cap"]),
        structured_output_contract=contract,
    )
    stage_dir = output / "frame"
    _write_new_json(
        stage_dir / "stage_started.json",
        {
            "schema_version": "literary_b2_live_stage_started_v1",
            "stage": "frame",
            "seal_hash": seal["seal_hash"],
            "request_fingerprint": request["request_fingerprint"],
            "quota_bucket_id": credential.quota_bucket_id,
            "credential_commitment": credential.commitment,
            "started_at": _now(),
        },
    )
    try:
        from openai import OpenAI

        client = LLMClient(
            LLMConfig(
                model=role.model_id,
                temperature=1.0,
                seed=20260717,
                reasoning_effort="none",
                verbosity="low",
                max_output_tokens=int(
                    state["b2_profile"].frame_output_tokens
                ),
                daily_token_cap=int(seal["limits"]["hard_visible_token_cap"]),
                prompt_token_cap=int(
                    state["b2_profile"].frame_prompt_tokens
                ),
                pricing={"input": 0.0, "cached_input": 0.0, "output": 0.0},
            ),
            stage_dir / "cache" / credential.quota_bucket_id / "frame.sqlite3",
            transport=OpenAI(
                api_key=credential.secret,
                base_url=credential.base_url,
            ).chat.completions.create,
            max_retries=0,
        )
        result = client.call(
            [dict(row) for row in request["messages"]],
            response_format=(
                openai_response_format(
                    contract,
                    schema_name=(
                        f"literary_b2_frame_"
                        f"{state['canary'].frame_contract_version}"
                    ),
                )
                if contract is not None
                else _openai_response_format(
                    request["response_schema"],
                    f"literary_b2_frame_{state['canary'].frame_contract_version}",
                )
            ),
            tag=f"literary_b2:frame:{seal['chapter_id']}",
            bypass_cache=True,
        )
        raw = {
            "schema_version": "literary_b2_frame_raw_result_v1",
            "request_fingerprint": request["request_fingerprint"],
            "model": result.model,
            "quota_bucket_id": credential.quota_bucket_id,
            "credential_revision": credential.credential_revision,
            "credential_commitment": credential.commitment,
            "response_text": result.text,
            "parsed_json": result.parsed_json,
            "json_error": result.json_error,
            "usage": asdict(result.usage),
            "latency_ms": result.latency_ms,
            "cost_usd": result.cost_usd,
            "from_cache": result.from_cache,
            "cache_key": result.cache_key,
            "completed_at": _now(),
            "structured_output_contract": (
                contract.to_payload() if contract else None
            ),
        }
        _write_new_json(stage_dir / "raw_result.json", raw)
        if not isinstance(result.parsed_json, Mapping):
            raise B2LiveCanaryError(
                f"GPT frame response is not structured JSON: {result.json_error}"
            )
        validate_structured_payload(
            result.parsed_json,
            canonical_schema=request["response_schema"],
        )
        artifact = _frame_normalizer(
            state["canary"].frame_contract_version
        )(request=request, response=result.parsed_json)
        _write_new_json(artifact_path, artifact)
        _write_new_json(
            stage_dir / "stage_report.json",
            {
                "schema_version": "literary_b2_live_stage_report_v1",
                "status": "accepted",
                "stage": "frame",
                "artifact_hash": artifact["artifact_hash"],
                "review_request_count": len(artifact["review_requests"]),
                "usage": asdict(result.usage),
                "quota_bucket_id": credential.quota_bucket_id,
                "production_publish_performed": False,
                "completed_at": _now(),
            },
        )
        _prepare_interaction_seal(
            output=output,
            state=state,
            frame_artifact=artifact,
        )
        _verify_post_call_invariants(
            seal=seal,
            frozen_db=frozen_db,
        )
        return artifact
    except Exception as exc:
        _write_new_json(
            stage_dir / "stage_failure.json",
            _failure_payload("frame", exc, frozen_db),
        )
        raise


def execute_b2_interactions_live_v1(
    *,
    output_root: Path,
    credential_root: Path | None,
    frozen_db: Path,
    current_git_head: str,
    backend_mode: str = BACKEND_MODE_LEGACY,
    shared_runtime: LiterarySharedRunnerBindingsV1 | None = None,
) -> dict[str, Any]:
    _validate_backend_mode_v1(
        backend_mode=backend_mode,
        credential_root=credential_root,
        shared_runtime=shared_runtime,
    )
    output = Path(output_root).resolve()
    final_path = output / "chapter_b2_artifact.json"
    if final_path.is_file():
        state = _verify_prepared_run(
            output=output,
            credential_root=credential_root,
            frozen_db=frozen_db,
            current_git_head=current_git_head,
            backend_mode=backend_mode,
            shared_runtime=shared_runtime,
        )
        final = _verified_hashed_artifact(final_path, "chapter B2 artifact")
        if final.get("run_seal_hash") != state["seal"].get("seal_hash"):
            raise B2LiveCanaryError("chapter B2 artifact points to another run seal")
        return final
    state = _verify_prepared_run(
        output=output,
        credential_root=credential_root,
        frozen_db=frozen_db,
        current_git_head=current_git_head,
        backend_mode=backend_mode,
        shared_runtime=shared_runtime,
    )
    seal = state["seal"]
    frame = _verified_hashed_artifact(
        output / "frame" / "frame_artifact.json", "frame artifact"
    )
    interaction_seal = _verified_hashed_payload(
        output / "interaction_seal.json",
        hash_field="interaction_seal_hash",
        label="interaction seal",
    )
    if interaction_seal.get("schema_version") not in {
        INTERACTION_SEAL_SCHEMA_VERSION,
        INTERACTION_SEAL_SCHEMA_VERSION_V2,
    }:
        raise B2LiveCanaryError("foreign B2 interaction seal")
    if interaction_seal.get("frame_artifact_hash") != frame.get("artifact_hash"):
        raise B2LiveCanaryError("interaction seal points to another frame artifact")
    requests = interaction_seal.get("requests")
    if not isinstance(requests, list) or len(requests) != state[
        "canary"
    ].interaction_calls:
        raise B2LiveCanaryError("interaction seal does not exact-cover call cap")
    credential = state["interaction_credential"]
    role = state["interaction_role"]
    artifacts: list[dict[str, Any]] = []
    for row in requests:
        stage_dir = _contained_path(
            output, row.get("stage_dir"), "interaction stage directory"
        )
        request_path = stage_dir / "request.json"
        artifact_path = stage_dir / "interaction_artifact.json"
        if artifact_path.is_file():
            artifact = _verified_hashed_artifact(
                artifact_path, "interaction artifact"
            )
            _ensure_existing_interaction_stage_report(
                stage_dir=stage_dir,
                artifact=artifact,
                shared_runtime=(
                    state.get("shared_runtime")
                    if backend_mode == BACKEND_MODE_SHARED_V1
                    else None
                ),
            )
            artifacts.append(artifact)
            continue
        if (stage_dir / "stage_failure.json").is_file():
            raise B2LiveCanaryError(
                f"interaction stage has a recorded failure: {row.get('window_id')}"
            )
        request = _read_object(request_path, "interaction request")
        if request.get("request_fingerprint") != row.get("request_fingerprint"):
            raise B2LiveCanaryError("interaction request differs from its seal")
        if backend_mode == BACKEND_MODE_SHARED_V1:
            try:
                artifact = _execute_b2_interaction_shared_v1(
                    output=output,
                    state=state,
                    interaction_seal=interaction_seal,
                    request_row=row,
                    request=request,
                    stage_dir=stage_dir,
                )
                _verify_post_call_invariants(seal=seal, frozen_db=frozen_db)
                artifacts.append(artifact)
                continue
            except Exception as exc:
                _write_new_json(
                    stage_dir / "stage_failure.json",
                    _failure_payload(
                        f"interaction:{row.get('window_id')}", exc, frozen_db
                    ),
                )
                raise
        contract = _resolve_b2_structured_contract(
            policy=state["structured_output_policy"],
            role_id=state["canary"].interaction_role_id,
            role=role,
            credential=credential,
            canonical_schema=request["response_schema"],
        )
        _verify_b2_interaction_sealed_contract(
            seal=seal,
            interaction_seal=interaction_seal,
            request_row=row,
            contract=contract,
        )
        _ensure_run_budget(
            output=output,
            next_request=request,
            hard_cap=int(seal["limits"]["hard_visible_token_cap"]),
            structured_output_contract=contract,
        )
        _write_new_json(
            stage_dir / "stage_started.json",
            {
                "schema_version": "literary_b2_live_stage_started_v1",
                "stage": "interaction",
                "window_id": row["window_id"],
                "interaction_seal_hash": interaction_seal[
                    "interaction_seal_hash"
                ],
                "request_fingerprint": request["request_fingerprint"],
                "quota_bucket_id": credential.quota_bucket_id,
                "credential_commitment": credential.commitment,
                "started_at": _now(),
            },
        )
        try:
            raw, parsed = _call_interaction_model(
                stage_dir=stage_dir,
                request=request,
                window_id=str(row["window_id"]),
                role=role,
                credential=credential,
                b2_profile=state["b2_profile"],
                hard_visible_token_cap=int(
                    seal["limits"]["hard_visible_token_cap"]
                ),
                structured_output_contract=contract,
                contract_version=state["canary"].interaction_contract_version,
            )
            _write_new_json(stage_dir / "raw_result.json", raw)
            if not isinstance(parsed, Mapping):
                raise B2LiveCanaryError(
                    f"{role.model_id} interaction response is not structured JSON: "
                    f"{raw.get('json_error')}"
                )
            validate_structured_payload(
                parsed,
                canonical_schema=request["response_schema"],
            )
            artifact = _normalize_interaction_response(
                contract_version=state["canary"].interaction_contract_version,
                request=request,
                response=parsed,
            )
            _write_new_json(artifact_path, artifact)
            _write_new_json(
                stage_dir / "stage_report.json",
                _interaction_stage_report(
                    artifact=artifact,
                    raw=raw,
                    status="accepted",
                ),
            )
            _verify_post_call_invariants(seal=seal, frozen_db=frozen_db)
            artifacts.append(artifact)
        except Exception as exc:
            _write_new_json(
                stage_dir / "stage_failure.json",
                _failure_payload(
                    f"interaction:{row.get('window_id')}", exc, frozen_db
                ),
            )
            raise
    chapter_artifact = _finalize_chapter_artifact(
        seal=seal,
        frame=frame,
        interaction_seal=interaction_seal,
        interactions=artifacts,
    )
    _write_new_json(final_path, chapter_artifact)
    report = _finalize_live_report(
        output=output,
        seal=seal,
        chapter_artifact=chapter_artifact,
    )
    _write_new_json(output / "live_report.json", report)
    _verify_post_call_invariants(seal=seal, frozen_db=frozen_db)
    return chapter_artifact


def _prepare_interaction_seal(
    *,
    output: Path,
    state: Mapping[str, Any],
    frame_artifact: Mapping[str, Any],
) -> dict[str, Any]:
    path = output / "interaction_seal.json"
    if path.is_file():
        existing = _verified_hashed_payload(
            path, hash_field="interaction_seal_hash", label="interaction seal"
        )
        if existing.get("schema_version") not in {
            INTERACTION_SEAL_SCHEMA_VERSION,
            INTERACTION_SEAL_SCHEMA_VERSION_V2,
        }:
            raise B2LiveCanaryError("foreign B2 interaction seal")
        return existing
    chapter_row = state["chapter_row"]
    chapter = _object(chapter_row.get("chapter"), "chapter")
    prefix = _object(chapter_row.get("prefix_bundle"), "prefix bundle")
    windows = build_b2_windows_v1(chapter, profile=state["b2_profile"])
    rows: list[dict[str, Any]] = []
    total_reserve = 0
    for ordinal, window in enumerate(windows, 1):
        frame_context = build_frame_context_for_window_v1(
            frame_artifact=frame_artifact, window=window
        )
        request = authorize_b2_request_for_live_v1(
            _render_interaction_request(
                contract_version=state["canary"].interaction_contract_version,
                window=window,
                prefix_bundle=prefix,
                profile=state["b2_profile"],
                frame_context=frame_context,
            )
        )
        if state.get("backend_mode") == BACKEND_MODE_SHARED_V1:
            runtime = state.get("shared_runtime")
            if not isinstance(runtime, LiterarySharedRunnerBindingsV1):
                raise B2LiveCanaryError("shared B2 runtime is absent")
            shared_capability_binding = _shared_b2_role_binding_v1(
                role_name="interaction",
                response_schema=request["response_schema"],
                shared_runtime=runtime,
            )
            contract = None
        else:
            shared_capability_binding = None
            contract = _resolve_b2_structured_contract(
                policy=state["structured_output_policy"],
                role_id=state["canary"].interaction_role_id,
                role=state["interaction_role"],
                credential=state["interaction_credential"],
                canonical_schema=request["response_schema"],
            )
        request_reserve = _request_total_reserve(
            request, structured_output_contract=contract
        )
        stage_dir = (
            output
            / "interactions"
            / f"{ordinal:02d}_{_path_component(window['window_id'])}"
        )
        _write_new_json(stage_dir / "request.json", request)
        request_row = {
            "ordinal": ordinal,
            "window_id": window["window_id"],
            "window_hash": window["window_hash"],
            "active_block_ids": list(window["active_block_ids"]),
            "preceding_tail_block_ids": list(
                window["preceding_tail_block_ids"]
            ),
            "frame_context_hash": frame_context["frame_context_hash"],
            "request_fingerprint": request["request_fingerprint"],
            "request_total_token_reserve": request_reserve,
            "structured_output_contract": (
                contract.to_payload() if contract is not None else None
            ),
            "stage_dir": str(stage_dir.relative_to(output)).replace("\\", "/"),
        }
        if shared_capability_binding is not None:
            request_row["shared_capability_binding"] = shared_capability_binding
        rows.append(request_row)
        total_reserve += request_reserve
    if len(rows) != state["canary"].interaction_calls:
        raise B2LiveCanaryError("actual B2 interaction count differs from seal")
    frame_report = _read_object(output / "frame" / "stage_report.json", "frame report")
    actual_frame_tokens = _usage_total(frame_report.get("usage"))
    if (
        actual_frame_tokens + total_reserve
        > state["canary"].hard_visible_token_cap
    ):
        raise B2LiveCanaryError(
            "actual frame usage plus interaction reserve exceeds hard run cap"
        )
    body = {
        "schema_version": INTERACTION_SEAL_SCHEMA_VERSION_V2,
        "run_seal_hash": state["seal"]["seal_hash"],
        "chapter_id": state["canary"].chapter_id,
        "frame_artifact_hash": frame_artifact["artifact_hash"],
        "frame_actual_visible_tokens": actual_frame_tokens,
        "interaction_total_token_reserve": total_reserve,
        "requests": rows,
        "provider_fallback_allowed": False,
        "exception_calls_allowed": 0,
        "production_publish_performed": False,
        "sealed_at": _now(),
    }
    value = {**body, "interaction_seal_hash": canonical_hash(body)}
    _write_new_json(path, value)
    return value


def _verify_prepared_run(
    *,
    output: Path,
    credential_root: Path | None,
    frozen_db: Path,
    current_git_head: str,
    backend_mode: str = BACKEND_MODE_LEGACY,
    shared_runtime: LiterarySharedRunnerBindingsV1 | None = None,
) -> dict[str, Any]:
    _validate_backend_mode_v1(
        backend_mode=backend_mode,
        credential_root=credential_root,
        shared_runtime=shared_runtime,
    )
    seal = _verified_hashed_payload(
        output / "run_seal.json", hash_field="seal_hash", label="run seal"
    )
    if seal.get("schema_version") not in {
        RUN_SEAL_SCHEMA_VERSION,
        RUN_SEAL_SCHEMA_VERSION_V2,
        RUN_SEAL_SCHEMA_VERSION_V3,
        RUN_SEAL_SCHEMA_VERSION_V4,
    }:
        raise B2LiveCanaryError("foreign B2 live seal")
    sealed_backend_mode = str(seal.get("backend_mode") or BACKEND_MODE_LEGACY)
    if sealed_backend_mode != backend_mode:
        raise B2LiveCanaryError("B2 backend mode differs from the run seal")
    if backend_mode == BACKEND_MODE_SHARED_V1:
        assert shared_runtime is not None
        if seal.get("schema_version") != RUN_SEAL_SCHEMA_VERSION_V4:
            raise B2LiveCanaryError("shared_v1 B2 requires a shared run seal")
        if seal.get("shared_runtime_identity") != shared_runtime.identity_payload():
            raise B2LiveCanaryError("shared B2 runtime identity differs from run seal")
    elif seal.get("schema_version") == RUN_SEAL_SCHEMA_VERSION_V4:
        raise B2LiveCanaryError("legacy B2 cannot resume a shared run seal")
    if Path(str(seal.get("output_root") or "")).resolve() != output:
        raise B2LiveCanaryError("B2 live output root differs from run seal")
    if seal.get("git_head") != current_git_head:
        raise B2LiveCanaryError("current Git HEAD differs from B2 live seal")
    if file_sha256(frozen_db).upper() != FROZEN_DB_SHA256:
        raise B2LiveCanaryError("frozen DB changed after B2 live seal")
    canary_path = Path(
        _required_string(
            seal.get("source_run_root"), "source run root"
        )
    )
    if _tree_hash(canary_path) != seal.get("source_tree_hash"):
        raise B2LiveCanaryError("source B1 artifact tree changed after seal")

    # Profile paths remain fixed by the checked-in canary configuration.
    canary = load_b2_canary_profile_v1(
        Path(_required_string(seal.get("canary_profile_path"), "canary profile path"))
    )
    if (
        canary.profile_id != seal.get("canary_profile_id")
        or canary.profile_hash != seal.get("canary_profile_hash")
        or file_sha256(canary.source_path)
        != seal.get("canary_profile_sha256")
    ):
        raise B2LiveCanaryError("B2 canary profile changed after seal")
    b2_profile = load_b2_phase_a_profile(canary.b2_profile_path)
    if (
        b2_profile.profile_id != seal.get("b2_profile_id")
        or b2_profile.profile_hash != seal.get("b2_profile_hash")
        or file_sha256(canary.b2_profile_path)
        != seal.get("b2_profile_sha256")
    ):
        raise B2LiveCanaryError("B2 phase profile changed after seal")
    provider: ProviderProfile | None = None
    if backend_mode == BACKEND_MODE_LEGACY:
        provider = load_provider_profile(canary.provider_profile_path)
        if (
            provider.profile_id != seal.get("provider_profile_id")
            or provider.profile_hash != seal.get("provider_profile_hash")
            or file_sha256(provider.source_path)
            != seal.get("provider_profile_sha256")
        ):
            raise B2LiveCanaryError("B2 provider profile changed after seal")
    elif any(
        seal.get(key) is not None
        for key in (
            "provider_profile_id",
            "provider_profile_hash",
            "provider_profile_sha256",
        )
    ):
        raise B2LiveCanaryError("shared B2 seal contains legacy provider authority")
    structured_output_policy = (
        load_literary_structured_output_policy(
            canary.structured_output_policy_path
        )
        if (
            backend_mode == BACKEND_MODE_LEGACY
            and canary.structured_output_policy_path is not None
        )
        else None
    )
    sealed_policy = seal.get("structured_output_policy")
    if structured_output_policy is None:
        if sealed_policy is not None:
            raise B2LiveCanaryError("B2 seal gained a foreign structured-output policy")
    else:
        expected_policy = {
            "path": str(structured_output_policy.source_path),
            "sha256": structured_output_policy.source_sha256,
            "policy_id": structured_output_policy.policy_id,
            "policy_hash": structured_output_policy.policy_hash,
        }
        if sealed_policy != expected_policy:
            raise B2LiveCanaryError("B2 Structured Output policy changed after seal")
    frame_role: ProviderRole | None = None
    interaction_role: ProviderRole | None = None
    frame_credential: ResolvedCredential | None = None
    interaction_credential: ResolvedCredential | None = None
    role_seals = _object(seal.get("role_bindings"), "sealed role bindings")
    if backend_mode == BACKEND_MODE_LEGACY:
        assert provider is not None and credential_root is not None
        frame_role, interaction_role = _resolve_canary_roles(provider, canary)
        frame_credential = resolve_role_credential(
            provider,
            role_id=canary.frame_role_id,
            credential_root=credential_root,
        )
        interaction_credential = resolve_role_credential(
            provider,
            role_id=canary.interaction_role_id,
            credential_root=credential_root,
        )
        for role_name, role, credential in (
            ("frame", frame_role, frame_credential),
            ("interaction", interaction_role, interaction_credential),
        ):
            row = _object(role_seals.get(role_name), f"sealed {role_name} role")
            if (
                row.get("provider") != role.provider
                or row.get("model_id") != role.model_id
                or row.get("quota_bucket_id") != credential.quota_bucket_id
                or row.get("credential_revision") != credential.credential_revision
                or row.get("credential_commitment") != credential.commitment
            ):
                raise B2LiveCanaryError(
                    f"{role_name} credential differs from run seal"
                )
    else:
        assert shared_runtime is not None
        if set(role_seals) != {"frame", "interaction"}:
            raise B2LiveCanaryError("shared B2 role bindings are incomplete")
        for role_name in ("frame", "interaction"):
            _verify_shared_b2_role_binding_v1(
                role_name=role_name,
                sealed_binding=_object(
                    role_seals.get(role_name), f"shared {role_name} role binding"
                ),
                shared_runtime=shared_runtime,
            )
    real_input = load_b2_source_input_v1(
        Path(seal["source_run_root"]), current_git_head=current_git_head
    )
    if (
        real_input.get("input_hash") != seal.get("source_input_hash")
        or real_input.get("source_document_sha256")
        != seal.get("source_document_sha256")
    ):
        raise B2LiveCanaryError("B2 source input differs from run seal")
    chapter_row = _selected_chapter(real_input, canary.chapter_id)
    if (
        chapter_row.get("chapter_report_hash")
        != seal.get("source_chapter_report_hash")
        or chapter_row.get("prefix_bundle_hash")
        != seal.get("source_prefix_bundle_hash")
    ):
        raise B2LiveCanaryError("B2 Ch1 handoff differs from run seal")
    prior_frame_context: dict[str, Any] | None = None
    sealed_prior = seal.get("prior_frame_candidate_context")
    if canary.prior_frame_candidate_carry_required:
        if not isinstance(sealed_prior, Mapping):
            raise B2LiveCanaryError("required prior-frame context is absent from seal")
        prior_frame_context = _verified_hashed_payload(
            output / "prior_frame_context.json",
            hash_field="context_hash",
            label="prior-frame candidate context",
        )
        expected_prior = {
            "prior_b2_root": prior_frame_context["prior_b2_root"],
            "prior_b2_tree_hash": prior_frame_context["prior_b2_tree_hash"],
            "prior_run_seal_hash": prior_frame_context["prior_run_seal_hash"],
            "prior_frame_artifact_hash": prior_frame_context[
                "prior_frame_artifact_hash"
            ],
            "context_hash": prior_frame_context["context_hash"],
        }
        if dict(sealed_prior) != expected_prior:
            raise B2LiveCanaryError("prior-frame context differs from run seal")
        if _tree_hash(Path(prior_frame_context["prior_b2_root"])) != (
            prior_frame_context["prior_b2_tree_hash"]
        ):
            raise B2LiveCanaryError("prior B2 root changed after seal")
    elif sealed_prior is not None:
        raise B2LiveCanaryError("unrequested prior-frame context appeared in seal")
    frame_request = _read_object(output / "frame" / "request.json", "frame request")
    if frame_request.get("request_fingerprint") != seal.get(
        "frame_request_fingerprint"
    ):
        raise B2LiveCanaryError("B2 frame request differs from run seal")
    return {
        "seal": seal,
        "canary": canary,
        "b2_profile": b2_profile,
        "provider": provider,
        "frame_role": frame_role,
        "interaction_role": interaction_role,
        "frame_credential": frame_credential,
        "interaction_credential": interaction_credential,
        "structured_output_policy": structured_output_policy,
        "real_input": real_input,
        "chapter_row": chapter_row,
        "prior_frame_context": prior_frame_context,
        "backend_mode": backend_mode,
        "shared_runtime": shared_runtime,
    }


def _finalize_chapter_artifact(
    *,
    seal: Mapping[str, Any],
    frame: Mapping[str, Any],
    interaction_seal: Mapping[str, Any],
    interactions: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    expected_windows = interaction_seal["requests"]
    by_window = {
        _required_string(row.get("window_id"), "interaction window_id"): row
        for row in interactions
    }
    if set(by_window) != {row["window_id"] for row in expected_windows}:
        raise B2LiveCanaryError("B2 interaction artifacts do not exact-cover windows")
    expected_blocks = [
        block_id
        for row in expected_windows
        for block_id in row["active_block_ids"]
    ]
    if len(expected_blocks) != len(set(expected_blocks)):
        raise B2LiveCanaryError("B2 interaction windows overlap")
    ordered = [by_window[row["window_id"]] for row in expected_windows]
    turns = [deepcopy(item) for row in ordered for item in row["speaker_turns"]]
    slim_contract = seal["contract_versions"] == {
        "frame": "v2",
        "interaction": "v3",
    }
    event_field = "salient_events" if slim_contract else "interaction_events"
    events = [deepcopy(item) for row in ordered for item in row[event_field]]
    reviews = [
        {"origin_stage": "frame", **deepcopy(dict(row))}
        for row in frame["review_requests"]
    ] + [
        {
            "origin_stage": "interaction",
            "origin_window_id": artifact["window_id"],
            **deepcopy(dict(row)),
        }
        for artifact in ordered
        for row in artifact["review_requests"]
    ]
    downstream_reviews, frame_structure_reviews = (
        partition_b2_frame_structure_reviews_v1(
            reviews=reviews,
            frame_segments=frame["frame_segments"],
        )
    )
    body = {
        "schema_version": (
            SLIM_CHAPTER_ARTIFACT_SCHEMA_VERSION
            if slim_contract
            else CHAPTER_ARTIFACT_SCHEMA_VERSION
        ),
        "run_seal_hash": seal["seal_hash"],
        "chapter_id": seal["chapter_id"],
        "source_input_hash": seal["source_input_hash"],
        "source_document_sha256": seal["source_document_sha256"],
        "source_prefix_bundle_hash": seal["source_prefix_bundle_hash"],
        "certification_eligible": False,
        "certification_blockers": list(seal["certification_blockers"]),
        "frame_artifact_hash": frame["artifact_hash"],
        "frame_segments": deepcopy(frame["frame_segments"]),
        "interaction_artifacts": [
            {
                "window_id": row["window_id"],
                "artifact_hash": row["artifact_hash"],
            }
            for row in ordered
        ],
        "speaker_turns": turns,
        event_field: events,
        "review_requests": downstream_reviews,
        "frame_structure_reviews": frame_structure_reviews,
        "active_block_coverage": {
            "covered_block_ids": expected_blocks,
            "exact_cover": True,
        },
        "identity_or_claim_mutation_performed": False,
        "relation_phase_inference_performed": False,
        "translation_performed": False,
        "production_publish_performed": False,
    }
    if not slim_contract:
        body["chapter_orientation"] = deepcopy(frame["chapter_orientation"])
    return {**body, "artifact_hash": canonical_hash(body)}


def partition_b2_frame_structure_reviews_v1(
    *,
    reviews: Sequence[Mapping[str, Any]],
    frame_segments: Sequence[Mapping[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Separate inert frame-structure holds from downstream review traffic."""

    frame_by_block = {
        str(block_id): str(segment["frame_segment_id"])
        for segment in frame_segments
        for block_id in segment.get("covered_block_ids") or []
    }
    downstream_reviews: list[dict[str, Any]] = []
    frame_structure_reviews: list[dict[str, Any]] = []
    for review in reviews:
        try:
            destination = route_review(review)
        except (KeyError, ReviewRoutingError) as exc:
            raise B2LiveCanaryError(
                "B2 review has no valid typed route during chapter assembly"
            ) from exc
        if destination != "E":
            downstream_reviews.append(review)
            continue
        source_block_ids = list(review.get("source_block_ids") or [])
        if not source_block_ids or any(
            block_id not in frame_by_block for block_id in source_block_ids
        ):
            raise B2LiveCanaryError(
                "frame-structure review cites blocks outside the chapter frames"
            )
        frame_structure_reviews.append(
            {
                "review_kind": review.get("review_kind"),
                "blocking_kind": "frame_structure",
                "source_block_ids": source_block_ids,
                "frame_segment_ids": sorted(
                    {frame_by_block[block_id] for block_id in source_block_ids}
                ),
                "origin": review.get("origin"),
                "reason": review.get("reason"),
            }
        )
    return downstream_reviews, frame_structure_reviews


def _finalize_live_report(
    *,
    output: Path,
    seal: Mapping[str, Any],
    chapter_artifact: Mapping[str, Any],
) -> dict[str, Any]:
    reports = [
        _read_object(path, "B2 stage report")
        for path in sorted(output.glob("**/stage_report.json"))
    ]
    usage_by_stage = [
        {
            "stage": row.get("stage"),
            "window_id": row.get("window_id"),
            "quota_bucket_id": row.get("quota_bucket_id"),
            "usage": deepcopy(dict(row.get("usage") or {})),
        }
        for row in reports
    ]
    visible_tokens = sum(_usage_total(row.get("usage")) for row in reports)
    if visible_tokens > int(seal["limits"]["hard_visible_token_cap"]):
        raise B2LiveCanaryError("B2 actual usage exceeds hard run cap")
    slim_contract = chapter_artifact.get("schema_version") == (
        SLIM_CHAPTER_ARTIFACT_SCHEMA_VERSION
    )
    event_field = "salient_events" if slim_contract else "interaction_events"
    body = {
        "schema_version": (
            SLIM_LIVE_REPORT_SCHEMA_VERSION
            if slim_contract
            else LIVE_REPORT_SCHEMA_VERSION
        ),
        "status": "complete_exploratory_chapter_canary",
        "run_seal_hash": seal["seal_hash"],
        "chapter_artifact_hash": chapter_artifact["artifact_hash"],
        "chapter_id": seal["chapter_id"],
        "calls_performed": len(reports),
        "expected_calls": seal["limits"]["max_total_calls"],
        "visible_tokens": visible_tokens,
        "hard_visible_token_cap": seal["limits"]["hard_visible_token_cap"],
        "usage_by_stage": usage_by_stage,
        "frame_segment_count": len(chapter_artifact["frame_segments"]),
        "speaker_turn_count": len(chapter_artifact["speaker_turns"]),
        (
            "salient_event_count" if slim_contract else "interaction_event_count"
        ): len(chapter_artifact[event_field]),
        "review_request_count": len(chapter_artifact["review_requests"]),
        "certification_eligible": False,
        "certification_blockers": list(seal["certification_blockers"]),
        "frozen_db_sha256_after": FROZEN_DB_SHA256,
        "source_artifact_mutated": False,
        "gold_or_oracle_loaded": False,
        "production_publish_performed": False,
        "completed_at": _now(),
    }
    if body["calls_performed"] != body["expected_calls"]:
        raise B2LiveCanaryError("B2 final report call count differs from seal")
    return {**body, "report_hash": canonical_hash(body)}


def _ensure_run_budget(
    *,
    output: Path,
    next_request: Mapping[str, Any],
    hard_cap: int,
    structured_output_contract: StructuredOutputContract | None = None,
) -> None:
    reports = [
        _read_object(path, "B2 stage report")
        for path in sorted(output.glob("**/stage_report.json"))
    ]
    actual = sum(_usage_total(row.get("usage")) for row in reports)
    if (
        actual
        + _request_total_reserve(
            next_request,
            structured_output_contract=structured_output_contract,
        )
        > hard_cap
    ):
        raise B2LiveCanaryError("next B2 call would exceed hard visible-token cap")


def _ensure_existing_shared_frame_stage_report_v1(
    *,
    stage_dir: Path,
    artifact: Mapping[str, Any],
    shared_runtime: LiterarySharedRunnerBindingsV1,
) -> None:
    report_path = stage_dir / "stage_report.json"
    if report_path.is_file():
        _read_object(report_path, "shared frame stage report")
        return
    usage = _shared_b2_usage_v1(stage_dir)
    _write_new_json(
        report_path,
        {
            "schema_version": "literary_b2_live_stage_report_v1",
            "status": "accepted_recovered_from_shared_receipt",
            "backend_mode": BACKEND_MODE_SHARED_V1,
            "stage": "frame",
            "artifact_hash": artifact["artifact_hash"],
            "review_request_count": len(artifact["review_requests"]),
            "usage": usage,
            "quota_bucket_id": shared_runtime.api_source_for(
                _SHARED_B2_ROLE_IDS["frame"]
            )["physical_quota_bucket_id"],
            "shared_attempt_receipt_sha256": file_sha256(
                stage_dir / "shared_attempt_receipt.json"
            ),
            "production_publish_performed": False,
            "completed_at": _now(),
            "recovery_source_files": [
                "shared_attempt_receipt.json",
                "frame_artifact.json",
            ],
            "semantic_output_modified_during_recovery": False,
        },
    )


def _ensure_existing_interaction_stage_report(
    *,
    stage_dir: Path,
    artifact: Mapping[str, Any],
    shared_runtime: LiterarySharedRunnerBindingsV1 | None = None,
) -> None:
    report_path = stage_dir / "stage_report.json"
    if report_path.is_file():
        _read_object(report_path, "interaction stage report")
        return
    if shared_runtime is not None:
        usage = _shared_b2_usage_v1(stage_dir)
        raw = {
            "usage": usage,
            "quota_bucket_id": shared_runtime.api_source_for(
                _SHARED_B2_ROLE_IDS["interaction"]
            )["physical_quota_bucket_id"],
            "completed_at": _now(),
        }
        report = _interaction_stage_report(
            artifact=artifact,
            raw=raw,
            status="accepted_recovered_from_shared_receipt",
            recovery_source_files=[
                "shared_attempt_receipt.json",
                "interaction_artifact.json",
            ],
        )
        report["backend_mode"] = BACKEND_MODE_SHARED_V1
        report["shared_attempt_receipt_sha256"] = file_sha256(
            stage_dir / "shared_attempt_receipt.json"
        )
        _write_new_json(report_path, report)
        return
    raw = _read_object(stage_dir / "raw_result.json", "interaction raw result")
    if raw.get("request_fingerprint") != artifact.get("request_fingerprint"):
        raise B2LiveCanaryError(
            "interaction raw result and artifact have different requests"
        )
    _write_new_json(
        report_path,
        _interaction_stage_report(
            artifact=artifact,
            raw=raw,
            status="accepted_recovered_from_persisted_artifact",
            recovery_source_files=[
                "raw_result.json",
                "interaction_artifact.json",
            ],
        ),
    )


def _interaction_stage_report(
    *,
    artifact: Mapping[str, Any],
    raw: Mapping[str, Any],
    status: str,
    recovery_source_files: Sequence[str] | None = None,
) -> dict[str, Any]:
    slim_contract = "salient_events" in artifact
    event_field = "salient_events" if slim_contract else "interaction_events"
    report = {
        "schema_version": "literary_b2_live_stage_report_v1",
        "status": status,
        "stage": "interaction",
        "window_id": artifact["window_id"],
        "artifact_hash": artifact["artifact_hash"],
        "speaker_turn_count": len(artifact["speaker_turns"]),
        (
            "salient_event_count" if slim_contract else "interaction_event_count"
        ): len(artifact[event_field]),
        "review_request_count": len(artifact["review_requests"]),
        "usage": deepcopy(dict(raw.get("usage") or {})),
        "quota_bucket_id": raw.get("quota_bucket_id"),
        "production_publish_performed": False,
        "completed_at": raw.get("completed_at"),
    }
    if recovery_source_files:
        report["recovery_source_files"] = list(recovery_source_files)
        report["semantic_output_modified_during_recovery"] = False
    return report


def _request_total_reserve(
    request: Mapping[str, Any],
    *,
    structured_output_contract: StructuredOutputContract | None = None,
) -> int:
    reserve = _object(request.get("token_reserve"), "token reserve")
    value = reserve.get("conservative_total_token_reserve")
    if value is None:
        prompt = reserve.get("prompt_token_reserve")
        output = reserve.get("output_token_cap")
        value = int(prompt) + int(output)
    existing = _bounded_int(value, "request token reserve", 1, 1_000_000)
    if structured_output_contract is None:
        return existing
    output_cap = _bounded_int(
        reserve.get("output_token_cap"),
        "request output token cap",
        0,
        1_000_000,
    )
    messages = request.get("messages")
    if not isinstance(messages, list) or not all(
        isinstance(row, Mapping) for row in messages
    ):
        raise B2LiveCanaryError("request messages must be an array of objects")
    structured = structured_prompt_reserve_v1(
        messages=messages,
        response_schema=_object(
            request.get("response_schema"), "request response schema"
        ),
        output_token_cap=output_cap,
        include_schema_transport_overhead=(
            structured_output_contract.native_enforcement
        ),
    )
    return max(existing, structured.total_token_reserve)


def _usage_total(value: Any) -> int:
    usage = _object(value, "usage")
    return int(usage.get("prompt_tokens") or 0) + int(
        usage.get("completion_tokens") or 0
    )


def _openai_transport_schema(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {
            str(key): _openai_transport_schema(item)
            for key, item in value.items()
            if key not in {"minItems", "minLength", "uniqueItems"}
        }
    if isinstance(value, list):
        return [_openai_transport_schema(item) for item in value]
    return value


def _openai_response_format(
    schema: Mapping[str, Any], name: str
) -> dict[str, Any]:
    return {
        "type": "json_schema",
        "json_schema": {
            "name": name,
            "strict": True,
            "schema": _openai_transport_schema(schema),
        },
    }


def _call_interaction_model(
    *,
    stage_dir: Path,
    request: Mapping[str, Any],
    window_id: str,
    role: ProviderRole,
    credential: ResolvedCredential,
    b2_profile: B2PhaseAProfile,
    hard_visible_token_cap: int,
    structured_output_contract: StructuredOutputContract | None,
    contract_version: str,
) -> tuple[dict[str, Any], Mapping[str, Any] | None]:
    config = LLMConfig(
        model=role.model_id,
        temperature=1.0,
        seed=20260717,
        reasoning_effort="none",
        verbosity="low" if role.provider == "openai" else None,
        max_output_tokens=int(b2_profile.interaction_output_tokens),
        daily_token_cap=hard_visible_token_cap,
        prompt_token_cap=int(b2_profile.interaction_prompt_tokens),
        pricing={"input": 0.0, "cached_input": 0.0, "output": 0.0},
    )
    if role.provider == "google_genai":
        transport = _gemini_transport(
            api_key=credential.secret,
            response_json_schema=(
                request["response_schema"]
                if structured_output_contract is None
                else gemini_response_json_schema(structured_output_contract)
            ),
            timeout_ms=credential.request_timeout_ms,
            base_url=credential.base_url,
        )
        client = JudgeClient(
            config,
            stage_dir
            / "cache"
            / credential.quota_bucket_id
            / "interaction.sqlite3",
            transport=transport,
            max_retries=0,
        )
        result = client.call(
            [dict(item) for item in request["messages"]],
            response_format=RESPONSE_FORMAT_JSON,
            tag=f"literary_b2:interaction:{window_id}",
            bypass_cache=True,
        )
        parse_error: LiteraryTransportJsonError | None = None
        try:
            parsed, transport_normalization = parse_structured_response(
                result.text
            )
        except LiteraryTransportJsonError as exc:
            parsed = None
            transport_normalization = "rejected"
            parse_error = exc
        raw = {
            "schema_version": "literary_b2_interaction_raw_result_v1",
            "request_fingerprint": request["request_fingerprint"],
            "window_id": window_id,
            "model": result.model,
            "provider": role.provider,
            "quota_bucket_id": credential.quota_bucket_id,
            "credential_revision": credential.credential_revision,
            "credential_commitment": credential.commitment,
            "response_text": result.text,
            "parsed_json": parsed,
            "json_error": str(parse_error) if parse_error else None,
            "transport_normalization": transport_normalization,
            "safe_transport_metadata": dict(transport.last_metadata),
            "thinking_budget": 0,
            "usage": asdict(result.usage),
            "latency_ms": result.latency_ms,
            "cost_usd": result.cost_usd,
            "from_cache": result.from_cache,
            "cache_key": result.cache_key,
            "completed_at": _now(),
            "structured_output_contract": (
                structured_output_contract.to_payload()
                if structured_output_contract is not None
                else None
            ),
        }
        return raw, parsed if isinstance(parsed, Mapping) else None
    if role.provider == "openai":
        from openai import OpenAI

        client = LLMClient(
            config,
            stage_dir
            / "cache"
            / credential.quota_bucket_id
            / "interaction.sqlite3",
            transport=OpenAI(
                api_key=credential.secret,
                base_url=credential.base_url,
            ).chat.completions.create,
            max_retries=0,
        )
        result = client.call(
            [dict(item) for item in request["messages"]],
            response_format=(
                openai_response_format(
                    structured_output_contract,
                    schema_name=f"literary_b2_interaction_{contract_version}",
                )
                if structured_output_contract is not None
                else _openai_response_format(
                    request["response_schema"],
                    f"literary_b2_interaction_{contract_version}",
                )
            ),
            tag=f"literary_b2:interaction:{window_id}",
            bypass_cache=True,
        )
        parsed = result.parsed_json if isinstance(result.parsed_json, Mapping) else None
        raw = {
            "schema_version": "literary_b2_interaction_raw_result_v1",
            "request_fingerprint": request["request_fingerprint"],
            "window_id": window_id,
            "model": result.model,
            "provider": role.provider,
            "quota_bucket_id": credential.quota_bucket_id,
            "credential_revision": credential.credential_revision,
            "credential_commitment": credential.commitment,
            "response_text": result.text,
            "parsed_json": result.parsed_json,
            "json_error": result.json_error,
            "transport_normalization": "openai_strict_json_schema",
            "safe_transport_metadata": {},
            "thinking_budget": None,
            "usage": asdict(result.usage),
            "latency_ms": result.latency_ms,
            "cost_usd": result.cost_usd,
            "from_cache": result.from_cache,
            "cache_key": result.cache_key,
            "completed_at": _now(),
            "structured_output_contract": (
                structured_output_contract.to_payload()
                if structured_output_contract is not None
                else None
            ),
        }
        return raw, parsed
    raise B2LiveCanaryError(
        f"unsupported B2 interaction provider: {role.provider}"
    )


def _resolve_b2_structured_contract(
    *,
    policy: LiteraryStructuredOutputPolicy | None,
    role_id: str,
    role: ProviderRole,
    credential: ResolvedCredential,
    canonical_schema: Mapping[str, Any],
) -> StructuredOutputContract | None:
    if policy is None:
        return None
    return resolve_structured_output_contract(
        policy,
        role_id=role_id,
        provider=role.provider,
        base_url=credential.base_url,
        model_id=role.model_id,
        canonical_schema=canonical_schema,
    )


def _render_interaction_request(
    *,
    contract_version: str,
    window: Mapping[str, Any],
    prefix_bundle: Mapping[str, Any],
    profile: B2PhaseAProfile,
    frame_context: Mapping[str, Any] | None,
) -> dict[str, Any]:
    if contract_version == "v1":
        return render_b2_interaction_request_v1(
            window=window,
            prefix_bundle=prefix_bundle,
            profile=profile,
            frame_context=frame_context,
        )
    if contract_version == "v2":
        return render_b2_interaction_request_v2(
            window=window,
            prefix_bundle=prefix_bundle,
            profile=profile,
            frame_context=frame_context,
        )
    if contract_version == "v3":
        return render_b2_interaction_request_v3(
            window=window,
            prefix_bundle=prefix_bundle,
            profile=profile,
            frame_context=frame_context,
        )
    raise B2LiveCanaryError(
        f"unsupported B2 interaction contract: {contract_version}"
    )


def _normalize_interaction_response(
    *,
    contract_version: str,
    request: Mapping[str, Any],
    response: Mapping[str, Any],
) -> dict[str, Any]:
    for field in ("chapter_id", "window_id"):
        if field in request and response.get(field) != request[field]:
            raise B2ContractError(f"B2 response {field} differs from request")
    if contract_version == "v1":
        return normalize_b2_interaction_response_v1(
            request=request, response=response
        )
    if contract_version == "v2":
        return normalize_b2_interaction_response_v2(
            request=request, response=response
        )
    if contract_version == "v3":
        return normalize_b2_interaction_response_v3(
            request=request, response=response
        )
    raise B2LiveCanaryError(
        f"unsupported B2 interaction contract: {contract_version}"
    )


def _render_frame_request(
    *,
    contract_version: str,
    chapter: Mapping[str, Any],
    prefix_bundle: Mapping[str, Any],
    profile: B2PhaseAProfile,
    supplemental_candidate_sources: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    if contract_version == "v1":
        return render_b2_frame_request_v1(
            chapter=chapter,
            prefix_bundle=prefix_bundle,
            profile=profile,
            supplemental_candidate_sources=supplemental_candidate_sources,
        )
    if contract_version == "v2":
        return render_b2_frame_request_v2(
            chapter=chapter,
            prefix_bundle=prefix_bundle,
            profile=profile,
            supplemental_candidate_sources=supplemental_candidate_sources,
        )
    raise B2LiveCanaryError(f"unsupported B2 frame contract: {contract_version}")


def _frame_normalizer(contract_version: str):
    if contract_version == "v1":
        return normalize_b2_frame_response_v1
    if contract_version == "v2":
        return normalize_b2_frame_response_v2
    raise B2LiveCanaryError(f"unsupported B2 frame contract: {contract_version}")


def _verify_b2_sealed_contract(
    *,
    seal: Mapping[str, Any],
    role_name: str,
    contract: StructuredOutputContract | None,
) -> None:
    contracts = _object(
        seal.get("structured_output_contracts"),
        "sealed Structured Output contracts",
    )
    observed = contracts.get(role_name)
    expected = contract.to_payload() if contract is not None else None
    if observed != expected:
        raise B2LiveCanaryError(
            f"B2 {role_name} Structured Output contract differs from seal"
        )


def _verify_b2_interaction_sealed_contract(
    *,
    seal: Mapping[str, Any],
    interaction_seal: Mapping[str, Any],
    request_row: Mapping[str, Any],
    contract: StructuredOutputContract | None,
) -> None:
    schema_version = interaction_seal.get("schema_version")
    if schema_version == INTERACTION_SEAL_SCHEMA_VERSION:
        _verify_b2_sealed_contract(
            seal=seal,
            role_name="interaction",
            contract=contract,
        )
        return
    if schema_version != INTERACTION_SEAL_SCHEMA_VERSION_V2:
        raise B2LiveCanaryError("foreign B2 interaction seal")
    if "structured_output_contract" not in request_row:
        raise B2LiveCanaryError(
            "B2 interaction request lacks its sealed Structured Output contract"
        )
    observed = request_row.get("structured_output_contract")
    expected = contract.to_payload() if contract is not None else None
    if observed != expected:
        raise B2LiveCanaryError(
            "B2 interaction Structured Output contract differs from its window seal"
        )


def _checked_role(
    provider: ProviderProfile,
    role_id: str,
    *,
    provider_name: str,
    model_id: str,
) -> ProviderRole:
    role = provider.roles.get(role_id)
    if (
        role is None
        or role.provider != provider_name
        or role.model_id != model_id
        or len(role.bucket_order) != 1
    ):
        raise B2LiveCanaryError(f"B2 provider role is not sealed: {role_id}")
    return role


def _checked_frame_role(
    provider: ProviderProfile, role_id: str
) -> ProviderRole:
    role = provider.roles.get(role_id)
    allowed = {("openai", "gpt-5.4"), ("openai", "gpt-5.5")}
    if (
        role is None
        or (role.provider, role.model_id) not in allowed
        or len(role.bucket_order) != 1
    ):
        raise B2LiveCanaryError(
            f"B2 frame role is not an accepted sealed arm: {role_id}"
        )
    return role


def _resolve_canary_roles(
    provider: ProviderProfile, canary: B2CanaryProfile
) -> tuple[ProviderRole, ProviderRole]:
    return (
        _checked_frame_role(provider, canary.frame_role_id),
        _checked_interaction_role(provider, canary.interaction_role_id),
    )


def _checked_interaction_role(
    provider: ProviderProfile, role_id: str
) -> ProviderRole:
    role = provider.roles.get(role_id)
    allowed = {
        ("google_genai", "gemini-3.5-flash"),
        ("google_genai", "vuduythanh2023/gemini-3.5-flash"),
        ("openai", "gpt-5.4"),
        ("openai", "gpt-5.5"),
    }
    if (
        role is None
        or (role.provider, role.model_id) not in allowed
        or len(role.bucket_order) != 1
    ):
        raise B2LiveCanaryError(
            f"B2 interaction role is not an accepted sealed arm: {role_id}"
        )
    return role


def _selected_chapter(
    real_input: Mapping[str, Any], chapter_id: str
) -> dict[str, Any]:
    matches = [
        deepcopy(dict(row))
        for row in real_input.get("chapters") or []
        if isinstance(row, Mapping) and row.get("chapter_id") == chapter_id
    ]
    if len(matches) != 1:
        raise B2LiveCanaryError("B2 canary chapter is absent or repeated")
    return matches[0]


def _verify_post_call_invariants(
    *, seal: Mapping[str, Any], frozen_db: Path
) -> None:
    if file_sha256(frozen_db).upper() != FROZEN_DB_SHA256:
        raise B2LiveCanaryError("frozen DB changed during B2 live canary")
    if _tree_hash(Path(seal["source_run_root"])) != seal["source_tree_hash"]:
        raise B2LiveCanaryError("source B1 artifact changed during B2 live canary")
    prior = seal.get("prior_frame_candidate_context")
    if isinstance(prior, Mapping) and _tree_hash(
        Path(_required_string(prior.get("prior_b2_root"), "prior B2 root"))
    ) != _required_string(prior.get("prior_b2_tree_hash"), "prior B2 tree hash"):
        raise B2LiveCanaryError("prior B2 artifact changed during B2 live canary")


def _verified_hashed_artifact(path: Path, label: str) -> dict[str, Any]:
    return _verified_hashed_payload(path, hash_field="artifact_hash", label=label)


def _verified_hashed_payload(
    path: Path, *, hash_field: str, label: str
) -> dict[str, Any]:
    value = _read_object(path, label)
    body = deepcopy(value)
    observed = _required_string(body.pop(hash_field, None), hash_field)
    if canonical_hash(body) != observed:
        raise B2LiveCanaryError(f"{label} embedded hash differs")
    return value


def _failure_payload(stage: str, exc: Exception, frozen_db: Path) -> dict[str, Any]:
    message = str(exc)
    if "sk-" in message:
        message = "credential material was redacted from the transport error"
    return {
        "schema_version": "literary_b2_live_stage_failure_v1",
        "status": "halted_fail_closed",
        "stage": stage,
        "error_type": type(exc).__name__,
        "message": message[:1200],
        "frozen_db_sha256_after": file_sha256(frozen_db).upper(),
        "production_publish_performed": False,
        "failed_at": _now(),
    }


def _tree_hash(root: Path) -> str:
    if not root.is_dir():
        raise B2LiveCanaryError(f"source artifact root is absent: {root}")
    rows = [
        {
            "relative_path": str(path.relative_to(root)).replace("\\", "/"),
            "sha256": file_sha256(path),
        }
        for path in sorted(path for path in root.rglob("*") if path.is_file())
    ]
    return canonical_hash(rows)


def _write_new_json(path: Path, payload: Mapping[str, Any]) -> None:
    if path.exists():
        raise B2LiveCanaryError(f"immutable B2 artifact already exists: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    write_checkpoint_atomic(path, dict(payload))


def _contained_path(root: Path, value: Any, label: str) -> Path:
    relative = Path(_required_string(value, label))
    if relative.is_absolute():
        raise B2LiveCanaryError(f"{label} must be relative")
    result = (root / relative).resolve()
    try:
        result.relative_to(root)
    except ValueError as exc:
        raise B2LiveCanaryError(f"{label} escapes run root") from exc
    return result


def _sibling_file(source: Path, value: Any, label: str) -> Path:
    name = _required_string(value, label)
    if Path(name).name != name:
        raise B2LiveCanaryError(f"{label} must be a sibling file name")
    result = (source.parent / name).resolve()
    if not result.is_file():
        raise B2LiveCanaryError(f"{label} file is absent")
    return result


def _read_object(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError) as exc:
        raise B2LiveCanaryError(f"cannot read {label}: {path}") from exc
    if not isinstance(value, dict):
        raise B2LiveCanaryError(f"{label} must be a JSON object")
    return value


def _object(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise B2LiveCanaryError(f"{label} must be an object")
    return dict(value)


def _exact_keys(value: Mapping[str, Any], expected: set[str], label: str) -> None:
    if set(value) != expected:
        raise B2LiveCanaryError(f"{label} keys differ from contract")


def _required_string(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise B2LiveCanaryError(f"{label} must be a non-empty string")
    return value


def _bounded_int(
    value: Any, label: str, minimum: int, maximum: int
) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise B2LiveCanaryError(f"{label} must be an integer")
    if value < minimum or value > maximum:
        raise B2LiveCanaryError(f"{label} is outside [{minimum}, {maximum}]")
    return value


def _path_component(value: str) -> str:
    return "".join(character if character.isalnum() or character in "-_" else "_" for character in value)


def _now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()


__all__ = [
    "B2CanaryProfile",
    "B2LiveCanaryError",
    "authorize_b2_request_for_live_v1",
    "build_frame_context_for_window_v1",
    "build_prior_frame_candidate_context_v1",
    "execute_b2_frame_live_v1",
    "execute_b2_interactions_live_v1",
    "load_b2_canary_profile_v1",
    "partition_b2_frame_structure_reviews_v1",
    "prepare_b2_ch1_canary_v1",
]
