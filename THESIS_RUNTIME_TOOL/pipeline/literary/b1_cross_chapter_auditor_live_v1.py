"""Pure planning and validation for live B1 cross-chapter hearings.

The offline bridge remains the only place that assembles hearing evidence.  This
module verifies that immutable output, re-renders it under a selected Literary
runtime preset, measures the exact model-visible request, and exposes one local
validator per hearing route.  It performs no provider call and grants no
registry authority.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, replace
import json
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from pipeline.literary.b1_cross_chapter_audit_bridge_v1 import (
    B1CrossChapterAuditBridgeError,
    identity_hearing_response_schema_v1,
    partition_hearing_queue_v1,
    render_identity_hearing_request_v1,
    render_stable_claim_hearing_request_v1,
    stable_claim_hearing_response_schema_v1,
    validate_identity_hearing_response_v1,
    validate_stable_claim_hearing_response_v1,
    verify_hearing_queue_binding_v1,
)
from pipeline.literary.chapter_cycle_shared_runtime_v1 import (
    build_literary_code_ref_v1,
)
from pipeline.literary.checkpoint import canonical_hash, canonical_json
from pipeline.literary.model_ref_v1 import (
    MODEL_REF_FIELDS_V1,
    MODEL_REF_MODE_CLASSIFIED_V1,
    model_ref_instruction_v1,
    project_model_request_v1,
)
from pipeline.literary.request_token_preflight_v1 import (
    LiteraryRequestTokenPreflightV1,
    measure_literary_request_token_preflight_v1,
)


IDENTITY_ROLE_ID = "literary.audit.identity_surface"
STABLE_CLAIM_ROLE_ID = "literary.audit.stable_claim"

IDENTITY_ROUTE = "identity_auditor"
STABLE_CLAIM_ROUTE = "stable_claim_auditor"

SCHEMA_NAME_BY_ROUTE = {
    IDENTITY_ROUTE: "literary_cross_chapter_identity_hearing_v1_2",
    STABLE_CLAIM_ROUTE: "literary_cross_chapter_stable_claim_hearing_v1_2",
}
ROLE_ID_BY_ROUTE = {
    IDENTITY_ROUTE: IDENTITY_ROLE_ID,
    STABLE_CLAIM_ROUTE: STABLE_CLAIM_ROLE_ID,
}

# Keep role-local response fields and registry snapshot references here so
# unrelated Literary seals retain the common field map.
CROSS_CHAPTER_MODEL_REF_FIELDS_V1: Mapping[str, tuple[str, ...]] = {
    namespace: (
        tuple(names)
        + (
            "merge_target_prior_card_id",
            "excluded_prior_card_ids",
            "supports_excluded_prior_card_ids",
            "counterpart_entity_id",
        )
        if namespace == "entity"
        else tuple(names)
    )
    for namespace, names in MODEL_REF_FIELDS_V1.items()
}


class B1CrossChapterAuditorLiveError(ValueError):
    pass


@dataclass(frozen=True)
class PreparedLiveHearingV1:
    component: Mapping[str, Any]
    stored_request: Mapping[str, Any]
    live_request: Mapping[str, Any]
    role_id: str
    schema_name: str
    validator_ref: Mapping[str, str]
    token_preflight: LiteraryRequestTokenPreflightV1

    @property
    def component_id(self) -> str:
        return str(self.component["component_id"])

    @property
    def route(self) -> str:
        return str(self.component["review_route"])


@dataclass(frozen=True)
class B1CrossChapterLivePlanV1:
    hearings: tuple[PreparedLiveHearingV1, ...]
    waiting_components: tuple[Mapping[str, Any], ...]
    unconsumed_ready: Mapping[str, Sequence[str]]
    batch_index: int
    batch_count: int
    deferred_ready_component_ids: tuple[str, ...]
    queue_hash: str
    registry_hash: str
    plan_hash: str

    def to_payload(self) -> dict[str, Any]:
        body = {
            "schema_version": "literary_b1_cross_chapter_live_plan_v1",
            "queue_hash": self.queue_hash,
            "registry_hash": self.registry_hash,
            "ready_hearings": [
                {
                    "component_id": row.component_id,
                    "review_route": row.route,
                    "role_id": row.role_id,
                    "schema_name": row.schema_name,
                    "stored_request_sha256": canonical_hash(row.stored_request),
                    "live_request_fingerprint": row.live_request[
                        "request_fingerprint"
                    ],
                    "token_preflight": row.token_preflight.to_payload(),
                }
                for row in self.hearings
            ],
            "waiting_components": [
                {
                    "component_id": row.get("component_id"),
                    "review_route": row.get("review_route"),
                    "lifecycle_state": row.get("lifecycle_state"),
                    "question_type": row.get("question_type"),
                }
                for row in self.waiting_components
            ],
            "unconsumed_ready": {
                key: list(value) for key, value in self.unconsumed_ready.items()
            },
            "batch_index": self.batch_index,
            "batch_count": self.batch_count,
            "deferred_ready_component_ids": list(
                self.deferred_ready_component_ids
            ),
            "ready_count": len(self.hearings),
            "waiting_count": len(self.waiting_components),
            "model_reference_mode": MODEL_REF_MODE_CLASSIFIED_V1,
            "provider_calls": 0,
            "identity_authority_granted": False,
            "claim_authority_granted": False,
        }
        return {**body, "plan_hash": self.plan_hash}


def collect_source_blocks_v1(chapter_paths: Sequence[Path]) -> dict[str, str]:
    blocks: dict[str, str] = {}
    if not chapter_paths:
        raise B1CrossChapterAuditorLiveError("at least one chapter source is required")
    for path in chapter_paths:
        try:
            chapter = json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, TypeError, ValueError) as exc:
            raise B1CrossChapterAuditorLiveError(
                f"cannot load chapter source: {path}"
            ) from exc
        rows = chapter.get("blocks") if isinstance(chapter, Mapping) else None
        if not isinstance(rows, list) and isinstance(chapter, Mapping):
            sections = chapter.get("sections")
            rows = (
                sections.get("source_blocks")
                if isinstance(sections, Mapping)
                else None
            )
        if not isinstance(rows, list) or not rows:
            raise B1CrossChapterAuditorLiveError(
                f"chapter source has no blocks: {path}"
            )
        for row in rows:
            if not isinstance(row, Mapping):
                raise B1CrossChapterAuditorLiveError("chapter block is malformed")
            block_id = row.get("block_id")
            text = row.get("text")
            if not isinstance(block_id, str) or not block_id:
                raise B1CrossChapterAuditorLiveError("chapter block id is malformed")
            if not isinstance(text, str) or not text.strip():
                raise B1CrossChapterAuditorLiveError(
                    f"chapter block text is absent: {block_id}"
                )
            prior = blocks.get(block_id)
            if prior is not None and prior != text:
                raise B1CrossChapterAuditorLiveError(
                    f"conflicting chapter text supplied for block: {block_id}"
                )
            blocks[block_id] = text
    return blocks


def load_prepared_requests_v1(prepared_dir: Path) -> list[dict[str, Any]]:
    root = Path(prepared_dir)
    if not root.is_dir():
        raise B1CrossChapterAuditorLiveError("prepared request directory is absent")
    rows: list[dict[str, Any]] = []
    for path in sorted(root.glob("b1xhear_*.json")):
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, TypeError, ValueError) as exc:
            raise B1CrossChapterAuditorLiveError(
                f"cannot load prepared hearing: {path.name}"
            ) from exc
        if not isinstance(value, dict):
            raise B1CrossChapterAuditorLiveError("prepared hearing must be an object")
        if path.stem != value.get("component_id"):
            raise B1CrossChapterAuditorLiveError(
                "prepared hearing filename differs from its component id"
            )
        rows.append(value)
    return rows


def response_schema_for_route_v1(route: str) -> dict[str, Any]:
    if route == IDENTITY_ROUTE:
        return identity_hearing_response_schema_v1()
    if route == STABLE_CLAIM_ROUTE:
        return stable_claim_hearing_response_schema_v1()
    raise B1CrossChapterAuditorLiveError("hearing route has no live response schema")


def validator_ref_for_route_v1(route: str) -> dict[str, str]:
    if route == IDENTITY_ROUTE:
        callables = (
            identity_hearing_response_schema_v1,
            validate_identity_hearing_response_v1,
            make_hearing_semantic_validator_v1,
        )
        identifier = "literary.audit.b1_cross_chapter_identity.validator"
    elif route == STABLE_CLAIM_ROUTE:
        callables = (
            stable_claim_hearing_response_schema_v1,
            validate_stable_claim_hearing_response_v1,
            make_hearing_semantic_validator_v1,
        )
        identifier = "literary.audit.b1_cross_chapter_stable_claim.validator"
    else:
        raise B1CrossChapterAuditorLiveError("hearing route has no validator")
    return build_literary_code_ref_v1(
        identifier=identifier,
        revision="v3",
        callables=callables,
    )


def make_hearing_semantic_validator_v1(
    *,
    component: Mapping[str, Any],
    rendered_request: Mapping[str, Any],
) -> Callable[[Mapping[str, Any]], Mapping[str, Any]]:
    route = str(component.get("review_route") or "")
    sections = rendered_request.get("sections")
    source_blocks = sections.get("source_blocks") if isinstance(sections, Mapping) else None
    if not isinstance(source_blocks, list) or not source_blocks:
        raise B1CrossChapterAuditorLiveError("hearing request has no source blocks")
    supplied = []
    for row in source_blocks:
        block_id = row.get("block_id") if isinstance(row, Mapping) else None
        if not isinstance(block_id, str) or not block_id:
            raise B1CrossChapterAuditorLiveError(
                "hearing request carries a malformed source block"
            )
        supplied.append(block_id)

    def validate(payload: Mapping[str, Any]) -> Mapping[str, Any]:
        if route == IDENTITY_ROUTE:
            return validate_identity_hearing_response_v1(
                payload,
                component=component,
                supplied_block_ids=supplied,
            )
        if route == STABLE_CLAIM_ROUTE:
            return validate_stable_claim_hearing_response_v1(
                payload,
                component=component,
                supplied_block_ids=supplied,
            )
        raise B1CrossChapterAuditorLiveError("hearing route has no semantic validator")

    return validate


def build_live_hearing_plan_v1(
    *,
    queue: Mapping[str, Any],
    registry: Mapping[str, Any],
    prepared_requests: Sequence[Mapping[str, Any]],
    source_blocks: Mapping[str, str],
    design_doc: Path,
    runtime: Any,
    batch_index: int | None = None,
) -> B1CrossChapterLivePlanV1:
    registry_hash = _required_hash(registry.get("registry_hash"), "registry_hash")
    verify_hearing_queue_binding_v1(queue, expected_registry_hash=registry_hash)
    partition = partition_hearing_queue_v1(queue)
    components = {
        str(row["component_id"]): row for row in queue.get("components") or []
    }
    ready = [
        *partition["ready_identity"],
        *partition["ready_stable_claim"],
    ]
    expected_ids = {str(row["component_id"]) for row in ready}
    supplied: dict[str, Mapping[str, Any]] = {}
    for row in prepared_requests:
        component_id = row.get("component_id")
        if not isinstance(component_id, str) or not component_id:
            raise B1CrossChapterAuditorLiveError(
                "prepared hearing lacks a component id"
            )
        if component_id in supplied:
            raise B1CrossChapterAuditorLiveError(
                "one hearing component was prepared twice"
            )
        supplied[component_id] = row
    if set(supplied) != expected_ids:
        raise B1CrossChapterAuditorLiveError(
            "prepared hearings do not exactly cover ready queue components"
        )
    _require_one_open_hearing_per_prior_card(queue.get("components") or [])

    hearings: list[PreparedLiveHearingV1] = []
    for component_id in sorted(expected_ids):
        component = components[component_id]
        stored = supplied[component_id]
        route = str(component.get("review_route") or "")
        role_id = ROLE_ID_BY_ROUTE.get(route)
        if role_id is None:
            raise B1CrossChapterAuditorLiveError("ready hearing route is unsupported")
        preset = runtime.role_presets[role_id]
        live_contract = {
            "model_id": preset.requested_model_id,
            "reasoning_effort": preset.generation["reasoning_effort"],
            "temperature": preset.generation["temperature"],
            "seed": preset.generation["seed"],
            "max_output_tokens": preset.generation["max_output_tokens"],
        }
        dry_contract = stored.get("model_contract")
        if not isinstance(dry_contract, Mapping):
            raise B1CrossChapterAuditorLiveError(
                "prepared hearing lacks its dry model contract"
            )
        stored_sections = stored.get("sections")
        expansion = (
            stored_sections.get("prior_evidence_expansion")
            if isinstance(stored_sections, Mapping)
            else None
        )
        expand_prior_evidence = bool(
            isinstance(expansion, Mapping) and expansion.get("enabled") is True
        )
        rerendered_dry = _render(
            route=route,
            component=component,
            queue=queue,
            source_blocks=source_blocks,
            design_doc=design_doc,
            model_contract=dry_contract,
            expand_prior_evidence=expand_prior_evidence,
        )
        if canonical_json(rerendered_dry) != canonical_json(stored):
            raise B1CrossChapterAuditorLiveError(
                "prepared hearing differs from authoritative re-render"
            )
        live_request = _render(
            route=route,
            component=component,
            queue=queue,
            source_blocks=source_blocks,
            design_doc=design_doc,
            model_contract=live_contract,
            expand_prior_evidence=expand_prior_evidence,
        )
        preflight = _require_cross_chapter_request_within_prompt_cap_v1(
            live_request,
            role_id=role_id,
            prompt_token_cap=int(preset.generation["max_input_tokens"]),
            output_token_cap=int(preset.generation["max_output_tokens"]),
        )
        hearings.append(
            PreparedLiveHearingV1(
                component=deepcopy(dict(component)),
                stored_request=deepcopy(dict(stored)),
                live_request=live_request,
                role_id=role_id,
                schema_name=SCHEMA_NAME_BY_ROUTE[route],
                validator_ref=validator_ref_for_route_v1(route),
                token_preflight=preflight,
            )
        )

    role_hearings: dict[str, list[PreparedLiveHearingV1]] = {}
    for hearing in hearings:
        role_hearings.setdefault(hearing.role_id, []).append(hearing)
    batch_count = max(
        (
            (len(rows) + int(runtime.role_presets[role_id].limits["max_calls"]) - 1)
            // int(runtime.role_presets[role_id].limits["max_calls"])
            for role_id, rows in role_hearings.items()
        ),
        default=1,
    )
    if batch_index is None:
        if batch_count > 1:
            raise B1CrossChapterAuditorLiveError(
                "ready hearings require "
                f"{batch_count} sealed batches; select --batch-index 1..{batch_count}"
            )
        selected_batch_index = 1
    else:
        if (
            not isinstance(batch_index, int)
            or isinstance(batch_index, bool)
            or batch_index < 1
            or batch_index > batch_count
        ):
            raise B1CrossChapterAuditorLiveError(
                f"batch_index must be within 1..{batch_count}"
            )
        selected_batch_index = batch_index

    selected: list[PreparedLiveHearingV1] = []
    for role_id in sorted(role_hearings):
        max_calls = int(runtime.role_presets[role_id].limits["max_calls"])
        start = (selected_batch_index - 1) * max_calls
        selected.extend(role_hearings[role_id][start : start + max_calls])
    selected.sort(key=lambda row: row.component_id)
    selected_ids = {row.component_id for row in selected}
    deferred_ids = tuple(
        row.component_id for row in hearings if row.component_id not in selected_ids
    )
    unconsumed = {
        key: tuple(value)
        for key, value in partition["unconsumed_ready"].items()
        if value
    }
    body = {
        "queue_hash": queue.get("queue_hash"),
        "registry_hash": registry_hash,
        "selected_ready": [
            {
                "component_id": row.component_id,
                "route": row.route,
                "request_fingerprint": row.live_request["request_fingerprint"],
                "preflight": row.token_preflight.to_payload(),
            }
            for row in selected
        ],
        "batch_index": selected_batch_index,
        "batch_count": batch_count,
        "deferred_ready_component_ids": list(deferred_ids),
        "waiting_component_ids": sorted(
            str(row.get("component_id")) for row in partition["waiting"]
        ),
        "unconsumed_ready": {key: list(value) for key, value in unconsumed.items()},
    }
    return B1CrossChapterLivePlanV1(
        hearings=tuple(selected),
        waiting_components=tuple(deepcopy(partition["waiting"])),
        unconsumed_ready=unconsumed,
        batch_index=selected_batch_index,
        batch_count=batch_count,
        deferred_ready_component_ids=deferred_ids,
        queue_hash=_required_hash(queue.get("queue_hash"), "queue_hash"),
        registry_hash=registry_hash,
        plan_hash=canonical_hash(body),
    )


def _render(
    *,
    route: str,
    component: Mapping[str, Any],
    queue: Mapping[str, Any],
    source_blocks: Mapping[str, str],
    design_doc: Path,
    model_contract: Mapping[str, Any],
    expand_prior_evidence: bool = False,
) -> dict[str, Any]:
    try:
        if route == IDENTITY_ROUTE:
            return render_identity_hearing_request_v1(
                component,
                queue=queue,
                source_blocks=source_blocks,
                design_doc=design_doc,
                model_contract=model_contract,
                expand_prior_evidence=expand_prior_evidence,
            )
        if route == STABLE_CLAIM_ROUTE:
            return render_stable_claim_hearing_request_v1(
                component,
                queue=queue,
                source_blocks=source_blocks,
                design_doc=design_doc,
                model_contract=model_contract,
                expand_prior_evidence=expand_prior_evidence,
            )
    except B1CrossChapterAuditBridgeError as exc:
        raise B1CrossChapterAuditorLiveError(str(exc)) from exc
    raise B1CrossChapterAuditorLiveError("hearing route cannot be rendered live")


def _require_cross_chapter_request_within_prompt_cap_v1(
    request: Mapping[str, Any],
    *,
    role_id: str,
    prompt_token_cap: int,
    output_token_cap: int,
) -> LiteraryRequestTokenPreflightV1:
    """Measure the exact hearing envelope with its role-local reference map."""

    try:
        projected, _ref_map = project_model_request_v1(
            request,
            field_names_by_namespace=CROSS_CHAPTER_MODEL_REF_FIELDS_V1,
            instruction=model_ref_instruction_v1(),
        )
        measured = measure_literary_request_token_preflight_v1(
            projected,
            prompt_token_cap=prompt_token_cap,
            output_token_cap=output_token_cap,
            model_reference_mode=None,
        )
    except (TypeError, ValueError) as exc:
        raise B1CrossChapterAuditorLiveError(str(exc)) from exc
    preflight = replace(
        measured,
        model_reference_mode=MODEL_REF_MODE_CLASSIFIED_V1,
        projected_request_fingerprint=str(projected["request_fingerprint"]),
    )
    if not preflight.fits_prompt_cap:
        raise B1CrossChapterAuditorLiveError(
            f"{role_id} prompt reserve {preflight.prompt_token_reserve} exceeds "
            f"input cap {preflight.prompt_token_cap}"
        )
    return preflight


def _require_one_open_hearing_per_prior_card(
    components: Sequence[Mapping[str, Any]],
) -> None:
    seen: dict[str, str] = {}
    for row in components:
        if not isinstance(row, Mapping):
            raise B1CrossChapterAuditorLiveError("hearing component is malformed")
        component_id = row.get("component_id")
        if not isinstance(component_id, str) or not component_id:
            raise B1CrossChapterAuditorLiveError("hearing component id is malformed")
        prior_ids = row.get("prior_card_ids")
        if not isinstance(prior_ids, list):
            singular = row.get("prior_card_id")
            prior_ids = [singular] if isinstance(singular, str) and singular else []
        for prior_card_id in prior_ids:
            if not isinstance(prior_card_id, str) or not prior_card_id:
                raise B1CrossChapterAuditorLiveError(
                    "hearing prior candidate id is malformed"
                )
            prior = seen.get(prior_card_id)
            if prior is not None and prior != component_id:
                raise B1CrossChapterAuditorLiveError(
                    "one prior card has more than one open cross-chapter hearing"
                )
            seen[prior_card_id] = component_id


def _required_hash(value: Any, label: str) -> str:
    if not isinstance(value, str) or len(value) != 64:
        raise B1CrossChapterAuditorLiveError(f"{label} is malformed")
    return value


__all__ = [
    "B1CrossChapterAuditorLiveError",
    "B1CrossChapterLivePlanV1",
    "CROSS_CHAPTER_MODEL_REF_FIELDS_V1",
    "PreparedLiveHearingV1",
    "IDENTITY_ROLE_ID",
    "STABLE_CLAIM_ROLE_ID",
    "IDENTITY_ROUTE",
    "STABLE_CLAIM_ROUTE",
    "ROLE_ID_BY_ROUTE",
    "SCHEMA_NAME_BY_ROUTE",
    "build_live_hearing_plan_v1",
    "collect_source_blocks_v1",
    "load_prepared_requests_v1",
    "make_hearing_semantic_validator_v1",
    "response_schema_for_route_v1",
    "validator_ref_for_route_v1",
]
