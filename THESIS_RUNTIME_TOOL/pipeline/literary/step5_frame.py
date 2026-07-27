"""Source-grounded S5B frame confirmation with synthetic-only execution."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, replace
from typing import Any, Literal, Mapping, Sequence, cast

from pipeline.literary.checkpoint import canonical_hash, canonical_json
from pipeline.literary.source_anchor import SourceInterval, SourcePoint, locate_anchor
from pipeline.literary.step5_authority import promote, validate_blinding
from pipeline.literary.step5_boundary import PromptProvider, RequestExecutor
from pipeline.literary.step5_budget import (
    CALL_SYMBOLS,
    CallPlanEntry,
    CallPlanManifest,
    ExecutionCostClass,
    seal_call_plan,
)
from pipeline.literary.step5_store import Step5Store, StoreError
from pipeline.literary.step5_support import SupportedItem, build_support_reverse_index
from pipeline.literary.step5_types import (
    AgreementRecord,
    AuthorityIndependencePolicy,
    BlindingValidationRecord,
    CheckerInputSection,
    DecisionQuestion,
    DecisionRevision,
    FullScopeChangeSet,
    ModelAuthorityRoute,
    QualificationManifest,
    Step5ContractError,
    SupportSet,
    build_checker_input_section_manifest,
    content_address,
    with_content_address,
)


ADJUDICATOR_PROMPT_ID = "literary_frame_tree_adjudicator_v1"
CHECKER_PROMPT_ID = "literary_frame_tree_checker_v1"
FRAME_CONTRACT_VERSION = "literary_step5_frame_v1"
FRAME_GENERATION_SCHEMA_VERSION = "literary_step5_generation_v1"
FRAME_VALIDATOR_CONTRACT_HASH = canonical_hash(
    {"contract": FRAME_CONTRACT_VERSION, "validator": "frame_tree_validator_v1"}
)
FRAME_POLICY_VERSION = "literary_frame_policy_v1"

FRAME_KINDS = frozenset(
    {
        "primary_narration",
        "embedded_document",
        "letter",
        "diary",
        "dream",
        "vision",
        "tale_told_aloud",
        "quoted_report",
    }
)
STORY_TIME_LABELS = frozenset(
    {"frame_present", "retrospective_past", "anterior_past"}
)
ROUTING_BUCKETS = (
    "person_occurrences",
    "non_person_occurrences",
    "discourse_only",
    "deferred",
    "invalid_flagged",
)


FRAME_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["response_status", "segments"],
    "properties": {
        "response_status": {"type": "string", "enum": ["proposed", "uncertain"]},
        "segments": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": [
                    "local_segment_key",
                    "parent_local_key",
                    "block_range",
                    "start_boundary",
                    "end_boundary",
                    "frame_kind",
                    "story_time_label",
                    "narrator_occurrence_ref",
                    "narrator_surface",
                    "evidence",
                ],
                "properties": {
                    "local_segment_key": {"type": "string"},
                    "parent_local_key": {"type": ["string", "null"]},
                    "block_range": {
                        "type": "array",
                        "minItems": 2,
                        "maxItems": 2,
                        "items": {"type": "string"},
                    },
                    "start_boundary": {"$ref": "#/$defs/boundary_or_null"},
                    "end_boundary": {"$ref": "#/$defs/boundary_or_null"},
                    "frame_kind": {
                        "type": "string",
                        "enum": sorted(FRAME_KINDS),
                    },
                    "story_time_label": {
                        "type": "string",
                        "enum": sorted(STORY_TIME_LABELS),
                    },
                    "narrator_occurrence_ref": {"type": ["string", "null"]},
                    "narrator_surface": {"type": "string"},
                    "evidence": {
                        "type": "array",
                        "minItems": 1,
                        "items": {
                            "type": "object",
                            "additionalProperties": False,
                            "required": ["block_id", "evidence_quote"],
                            "properties": {
                                "block_id": {"type": "string"},
                                "evidence_quote": {"type": "string"},
                            },
                        },
                    },
                },
            },
        },
    },
    "$defs": {
        "boundary": {
            "type": "object",
            "additionalProperties": False,
            "required": ["anchor_text", "evidence_quote"],
            "properties": {
                "anchor_text": {"type": "string"},
                "evidence_quote": {"type": "string"},
                "occurrence_hint": {"type": "integer", "minimum": 1},
            },
        },
        "boundary_or_null": {
            "anyOf": [{"$ref": "#/$defs/boundary"}, {"type": "null"}]
        },
    },
}


class FrameContractError(Step5ContractError):
    """Raised when S5B context, response, or authority state is invalid."""


@dataclass(frozen=True, slots=True)
class FrameCallConfig:
    route: ModelAuthorityRoute
    reasoning_effort: str
    verbosity: str
    max_output_tokens: int
    prompt_token_cap: int
    execution_cost_class: ExecutionCostClass
    quota_bucket_id: str | None

    def __post_init__(self) -> None:
        if not self.reasoning_effort or not self.verbosity:
            raise FrameContractError("frame request configuration is incomplete")
        if self.max_output_tokens <= 0 or self.prompt_token_cap <= 0:
            raise FrameContractError("frame token caps must be positive")
        if self.execution_cost_class == "remote_quota" and not self.quota_bucket_id:
            raise FrameContractError("remote frame call requires a quota bucket")
        if self.execution_cost_class == "local_compute" and self.quota_bucket_id is not None:
            raise FrameContractError("local frame call cannot carry a quota bucket")


@dataclass(frozen=True, slots=True)
class RenderedFrameRequest:
    role: Literal["adjudicator", "checker"]
    request_fingerprint: str
    api_request: dict[str, Any]
    sections: tuple[dict[str, Any], ...]


@dataclass(frozen=True, slots=True)
class PersistedFrameRequest:
    rendered: RenderedFrameRequest
    request_artifact_hash: str
    lineage_artifact_hash: str


@dataclass(frozen=True, slots=True)
class ValidatedFrameTree:
    response_status: Literal["proposed", "uncertain"]
    canonical_signature: dict[str, Any]
    canonical_signature_hash: str
    boundary_relaxed_hash: str
    nodes: tuple[dict[str, Any], ...]
    deepest_spans: tuple[dict[str, Any], ...]
    deepest_by_block: dict[str, str | None]
    evidence_by_node: dict[str, frozenset[str]]


@dataclass(frozen=True, slots=True)
class FrameRunResult:
    frame_view: dict[str, Any]
    frame_view_handle: dict[str, str]
    generation_hash: str
    decision_state: str
    request_artifact_hashes: tuple[str, ...]
    response_artifact_hashes: tuple[str, ...]
    call_plan: CallPlanManifest
    executed_call_count: int
    valid_boundary_disagreement_units: int


def _clone(value: Any) -> Any:
    return deepcopy(value)


def _exact_keys(value: Mapping[str, Any], expected: set[str], label: str) -> None:
    if set(value) != expected:
        raise FrameContractError(
            f"{label} fields mismatch: expected {sorted(expected)}, got {sorted(value)}"
        )


def _verify_bundle_hash(bundle: Mapping[str, Any]) -> None:
    body = _clone(dict(bundle))
    expected = str(body.pop("bundle_manifest_hash", ""))
    if not expected or canonical_hash(body) != expected:
        raise FrameContractError("B4 bundle manifest hash mismatch")


def _routing_by_occurrence(bundle: Mapping[str, Any]) -> dict[str, str]:
    routing = bundle.get("occurrence_routing")
    if not isinstance(routing, Mapping):
        raise FrameContractError("B4 bundle lacks occurrence routing")
    result: dict[str, str] = {}
    for bucket in ROUTING_BUCKETS:
        rows = routing.get(bucket)
        if not isinstance(rows, Sequence) or isinstance(rows, (str, bytes)):
            raise FrameContractError(f"occurrence routing bucket is malformed: {bucket}")
        for row in rows:
            if not isinstance(row, Mapping):
                raise FrameContractError("occurrence routing row must be an object")
            identifier = str(row.get("occurrence_id") or "")
            if not identifier or identifier in result:
                raise FrameContractError(
                    f"occurrence routing is not a unique cover: {identifier!r}"
                )
            result[identifier] = bucket
    return result


def build_frame_selection_universe(
    bundle: Mapping[str, Any], *, unit_id: str
) -> dict[str, Any]:
    """Build the complete source-grounded frame question from one B4 bundle."""

    _verify_bundle_hash(bundle)
    units = bundle.get("unit_manifest")
    if not isinstance(units, Sequence) or isinstance(units, (str, bytes)):
        raise FrameContractError("B4 unit manifest is missing")
    matching = [row for row in units if isinstance(row, Mapping) and row.get("unit_id") == unit_id]
    if len(matching) != 1:
        raise FrameContractError(f"unit manifest does not identify one unit: {unit_id}")
    unit = _clone(dict(matching[0]))
    raw_range = unit.get("block_range")
    if not isinstance(raw_range, Sequence) or len(raw_range) != 2:
        raise FrameContractError("unit block range is malformed")

    catalog = bundle.get("source_block_catalog")
    if not isinstance(catalog, Sequence) or isinstance(catalog, (str, bytes)):
        raise FrameContractError("B4 bundle lacks source block catalog")
    catalog_rows = [_clone(dict(row)) for row in catalog if isinstance(row, Mapping)]
    catalog_ids = [str(row.get("block_id") or "") for row in catalog_rows]
    try:
        start = catalog_ids.index(str(raw_range[0]))
        end = catalog_ids.index(str(raw_range[1]))
    except ValueError as exc:
        raise FrameContractError("unit range is absent from source block catalog") from exc
    if end < start:
        raise FrameContractError("unit source block range is reversed")
    selected = catalog_rows[start : end + 1]
    if not selected or any(str(row.get("chapter_id") or "") != unit.get("parent_chapter") for row in selected):
        raise FrameContractError("unit source catalog projection crosses its parent chapter")

    ordered_source_blocks: list[dict[str, Any]] = []
    for row in selected:
        block_id = str(row.get("block_id") or "")
        text = str(row.get("text") or "")
        if not block_id or not isinstance(row.get("order_index"), int):
            raise FrameContractError("source block catalog row is incomplete")
        normalized = {
            "block_id": block_id,
            "block_type": str(row.get("block_type") or ""),
            "order_index": int(row["order_index"]),
            "nfc_text": text,
        }
        normalized["source_row_hash"] = canonical_hash(normalized)
        ordered_source_blocks.append(normalized)

    narrative_ids = [
        row["block_id"]
        for row in ordered_source_blocks
        if row["block_type"] != "heading"
    ]
    if not narrative_ids:
        raise FrameContractError("frame unit has no narrative blocks")
    if not any(
        row["nfc_text"] for row in ordered_source_blocks if row["block_id"] in narrative_ids
    ):
        raise FrameContractError("frame unit narrative source is empty")

    selected_ids = {row["block_id"] for row in ordered_source_blocks}
    routing = _routing_by_occurrence(bundle)
    cards = bundle.get("occurrence_cards")
    if not isinstance(cards, Sequence) or isinstance(cards, (str, bytes)):
        raise FrameContractError("B4 bundle lacks occurrence cards")
    choices: list[dict[str, Any]] = []
    for card in cards:
        if not isinstance(card, Mapping) or str(card.get("block_id") or "") not in selected_ids:
            continue
        identifier = str(card.get("occurrence_id") or "")
        if identifier not in routing:
            raise FrameContractError(f"occurrence is absent from routing cover: {identifier}")
        context = card.get("context_universe") or {}
        context_ids: list[str] = []
        if isinstance(context, Mapping):
            active = context.get("active_block")
            if isinstance(active, Mapping) and active.get("block_id") in selected_ids:
                context_ids.append(str(active["block_id"]))
            for row in context.get("scene_block_candidates") or []:
                if isinstance(row, Mapping) and row.get("block_id") in selected_ids:
                    context_ids.append(str(row["block_id"]))
        choice = {
            "occurrence_ref": identifier,
            "occurrence_kind": str(card.get("occurrence_kind") or ""),
            "surface": str(card.get("surface") or ""),
            "block_id": str(card.get("block_id") or ""),
            "source_anchor": _clone(card.get("anchor") or {}),
            "context_block_ids": sorted(set(context_ids)),
            "runtime_eligibility": str(
                card.get("runtime_eligibility") or routing[identifier]
            ),
        }
        choice["source_row_hash"] = canonical_hash(choice)
        choices.append(choice)
    choices.sort(
        key=lambda row: (
            next(
                block["order_index"]
                for block in ordered_source_blocks
                if block["block_id"] == row["block_id"]
            ),
            int((row.get("source_anchor") or {}).get("char_start") or 0),
            row["occurrence_ref"],
        )
    )

    ground = bundle.get("ground_evidence") or {}
    ground_ids: list[str] = []
    if isinstance(ground, Mapping):
        for channel in ("frame_claim_inputs", "frame_leaf_index"):
            for row in ground.get(channel) or []:
                if isinstance(row, Mapping) and row.get("chapter_id") == unit.get("parent_chapter"):
                    identifier = str(row.get("ground_item_id") or "")
                    if identifier:
                        ground_ids.append(identifier)

    body = {
        "state_lineage_id": str(bundle.get("state_lineage_id") or ""),
        "bundle_manifest_hash": str(bundle.get("bundle_manifest_hash") or ""),
        "unit_id": unit_id,
        "unit_manifest": unit,
        "unit_manifest_hash": canonical_hash(unit),
        "ordered_source_blocks": ordered_source_blocks,
        "narrative_block_ids": narrative_ids,
        "occurrence_choices": choices,
        "source_ground_item_ids": sorted(set(ground_ids)),
        "frame_policy_version": FRAME_POLICY_VERSION,
    }
    if not body["state_lineage_id"]:
        raise FrameContractError("frame selection lacks state lineage")
    semantic_projection = {
        key: value for key, value in body.items() if key != "source_ground_item_ids"
    }
    body["selection_universe_hash"] = canonical_hash(semantic_projection)
    body["b3_audit_input_hash"] = canonical_hash(body["source_ground_item_ids"])
    return body


def _section(
    section_id: str, content: str, source_hashes: Sequence[str]
) -> dict[str, Any]:
    unique = sorted({str(value) for value in source_hashes if str(value)})
    if not unique:
        raise FrameContractError(f"rendered section lacks source lineage: {section_id}")
    return {
        "section_id": section_id,
        "content": content,
        "section_content_hash": canonical_hash({"rendered_content": content}),
        "direct_source_artifact_hashes": unique,
        "transitive_source_artifact_hashes": unique,
    }


def render_frame_request(
    *,
    selection: Mapping[str, Any],
    prompt_provider: PromptProvider,
    config: FrameCallConfig,
    role: Literal["adjudicator", "checker"],
) -> RenderedFrameRequest:
    expected_role = "adjudicator" if role == "adjudicator" else "checker"
    if config.route.role != expected_role:
        raise FrameContractError("frame route role does not match rendered request role")
    prompt_id = ADJUDICATOR_PROMPT_ID if role == "adjudicator" else CHECKER_PROMPT_ID
    system_prompt = prompt_provider.load_prompt(prompt_id)
    if f"Prompt version: {prompt_id}." not in system_prompt:
        raise FrameContractError(f"loaded prompt lacks its version marker: {prompt_id}")

    source_blocks = [
        {
            "block_id": row["block_id"],
            "block_type": row["block_type"],
            "order_index": row["order_index"],
            "text": row["nfc_text"],
        }
        for row in selection["ordered_source_blocks"]
    ]
    occurrences = [_clone(row) for row in selection["occurrence_choices"]]
    if role == "checker":
        occurrences.sort(
            key=lambda row: canonical_hash(
                {"checker_order": row["occurrence_ref"], "unit": selection["unit_id"]}
            )
        )
    unit_payload = {
        "unit_id": selection["unit_id"],
        "unit_manifest_hash": selection["unit_manifest_hash"],
        "narrative_block_ids": selection["narrative_block_ids"],
    }
    section_values = (
        _section(
            "system_prompt",
            system_prompt,
            [canonical_hash({"prompt_id": prompt_id, "bytes": system_prompt})],
        ),
        _section("unit", canonical_json(unit_payload), [selection["unit_manifest_hash"]]),
        _section(
            "source_blocks",
            canonical_json(source_blocks),
            [row["source_row_hash"] for row in selection["ordered_source_blocks"]],
        ),
        _section(
            "occurrence_choices",
            canonical_json(occurrences),
            [row["source_row_hash"] for row in selection["occurrence_choices"]]
            or [canonical_hash({"empty_occurrence_universe": selection["unit_id"]})],
        ),
        _section(
            "response_schema",
            canonical_json(FRAME_RESPONSE_SCHEMA),
            [canonical_hash(FRAME_RESPONSE_SCHEMA)],
        ),
    )
    user_content = "\n\n".join(
        f"{section['section_id'].upper()}\n{section['content']}"
        for section in section_values[1:]
    )
    api_request = {
        "model": config.route.model_identity.model_id,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_content},
        ],
        "reasoning_effort": config.reasoning_effort,
        "verbosity": config.verbosity,
        "response_format": {
            "type": "json_schema",
            "name": "literary_frame_tree_v1",
            "strict": True,
            "schema": FRAME_RESPONSE_SCHEMA,
        },
        "max_output_tokens": config.max_output_tokens,
        "metadata": {
            "execution_mode": "synthetic",
            "decision_kind": "frame",
            "role": role,
            "unit_id": selection["unit_id"],
            "state_lineage_id": selection["state_lineage_id"],
            "bundle_manifest_hash": selection["bundle_manifest_hash"],
            "selection_universe_hash": selection["selection_universe_hash"],
            "prompt_token_cap": config.prompt_token_cap,
            "frame_contract_version": FRAME_CONTRACT_VERSION,
        },
    }
    fingerprint = canonical_hash(
        {
            "api_request": api_request,
            "authority_route": config.route.to_canonical_payload(),
            "frame_policy_version": FRAME_POLICY_VERSION,
            "validator_contract_hash": FRAME_VALIDATOR_CONTRACT_HASH,
        }
    )
    return RenderedFrameRequest(
        role=role,
        request_fingerprint=fingerprint,
        api_request=api_request,
        sections=tuple(_clone(section) for section in section_values),
    )


def persist_frame_request(
    store: Step5Store, rendered: RenderedFrameRequest
) -> PersistedFrameRequest:
    wrapper = {
        "request_fingerprint": rendered.request_fingerprint,
        "api_request": _clone(rendered.api_request),
        "rendered_sections": [
            {"section_id": row["section_id"], "content": row["content"]}
            for row in rendered.sections
        ],
    }
    request_hash = store.put_content_artifact("requests", wrapper)
    lineage = {
        "request_artifact_hash": request_hash,
        "request_fingerprint": rendered.request_fingerprint,
        "sections": [
            {
                key: _clone(row[key])
                for key in (
                    "section_id",
                    "section_content_hash",
                    "direct_source_artifact_hashes",
                    "transitive_source_artifact_hashes",
                )
            }
            for row in rendered.sections
        ],
    }
    lineage_hash = store.put_content_artifact("request_lineage", lineage)
    return PersistedFrameRequest(
        rendered=rendered,
        request_artifact_hash=request_hash,
        lineage_artifact_hash=lineage_hash,
    )


def checker_manifest_from_persisted_lineage(
    store: Step5Store, lineage_artifact_hash: str
) -> Any:
    lineage = store.load_content_artifact("request_lineage", lineage_artifact_hash)
    request_hash = str(lineage.get("request_artifact_hash") or "")
    request = store.load_content_artifact("requests", request_hash)
    fingerprint = str(lineage.get("request_fingerprint") or "")
    if request.get("request_fingerprint") != fingerprint:
        raise FrameContractError("request lineage fingerprint mismatch")
    rendered_rows = request.get("rendered_sections")
    lineage_rows = lineage.get("sections")
    if not isinstance(rendered_rows, list) or not isinstance(lineage_rows, list):
        raise FrameContractError("persisted request lineage is malformed")
    rendered_by_id = {
        str(row.get("section_id") or ""): row
        for row in rendered_rows
        if isinstance(row, Mapping)
    }
    lineage_by_id = {
        str(row.get("section_id") or ""): row
        for row in lineage_rows
        if isinstance(row, Mapping)
    }
    if (
        not rendered_by_id
        or len(rendered_by_id) != len(rendered_rows)
        or len(lineage_by_id) != len(lineage_rows)
        or set(rendered_by_id) != set(lineage_by_id)
    ):
        raise FrameContractError("request lineage is not an exact section cover")
    sections: list[CheckerInputSection] = []
    for section_id in sorted(rendered_by_id):
        rendered = rendered_by_id[section_id]
        row = lineage_by_id[section_id]
        content = str(rendered.get("content") or "")
        expected_hash = canonical_hash({"rendered_content": content})
        direct = frozenset(str(value) for value in row.get("direct_source_artifact_hashes") or [])
        transitive = frozenset(
            str(value) for value in row.get("transitive_source_artifact_hashes") or []
        )
        if (
            row.get("section_content_hash") != expected_hash
            or not direct
            or not transitive
            or not direct.issubset(transitive)
        ):
            raise FrameContractError("request section lineage is incomplete or forged")
        sections.append(
            CheckerInputSection(
                section_id=section_id,
                section_content_hash=expected_hash,
                source_artifact_hashes=transitive,
            )
        )
    return build_checker_input_section_manifest(
        checker_request_fingerprint=fingerprint,
        checker_input_sections=tuple(sections),
    )


def _all_spans(text: str, needle: str) -> list[tuple[int, int]]:
    if not needle:
        return []
    result: list[tuple[int, int]] = []
    start = 0
    while True:
        found = text.find(needle, start)
        if found < 0:
            return result
        result.append((found, found + len(needle)))
        start = found + len(needle)


def _boundary_offset(
    boundary: Mapping[str, Any] | None,
    *,
    block: Mapping[str, Any],
    end_boundary: bool,
) -> int:
    text = str(block["nfc_text"])
    if boundary is None:
        return len(text) if end_boundary else 0
    if not isinstance(boundary, Mapping):
        raise FrameContractError("frame boundary must be an object or null")
    allowed = {"anchor_text", "evidence_quote", "occurrence_hint"}
    if set(boundary) - allowed or not {"anchor_text", "evidence_quote"}.issubset(boundary):
        raise FrameContractError("frame boundary fields are malformed")
    if not isinstance(boundary.get("anchor_text"), str) or not isinstance(
        boundary.get("evidence_quote"), str
    ):
        raise FrameContractError("frame boundary text fields must be strings")
    hint = boundary.get("occurrence_hint")
    if hint is not None and (not isinstance(hint, int) or hint <= 0):
        raise FrameContractError("frame boundary occurrence_hint must be positive")
    located = locate_anchor(
        {"block_id": block["block_id"], "clean_text": text},
        anchor_text=str(boundary.get("anchor_text") or ""),
        evidence_quote=str(boundary.get("evidence_quote") or ""),
        occurrence_hint=cast(int | None, hint),
    )
    if not located.ok or located.anchor is None:
        raise FrameContractError(
            f"frame boundary failed closed: {located.failure_reason}"
        )
    return located.anchor.char_end if end_boundary else located.anchor.char_start


def _frame_depths(nodes: Mapping[str, Mapping[str, Any]]) -> dict[str, int]:
    depths: dict[str, int] = {}

    def visit(key: str, seen: frozenset[str]) -> int:
        if key in depths:
            return depths[key]
        if key in seen:
            raise FrameContractError("frame tree contains a cycle")
        parent = nodes[key]["parent_local_key"]
        if parent is None:
            depth = 0
        else:
            if parent not in nodes:
                raise FrameContractError(f"frame tree has a missing parent: {key}->{parent}")
            depth = visit(str(parent), seen | {key}) + 1
        depths[key] = depth
        return depth

    for key in nodes:
        visit(key, frozenset())
    return depths


def _interval_payload(interval: SourceInterval) -> dict[str, Any]:
    return interval.to_dict()


def _validate_evidence(
    raw: Any,
    *,
    block_map: Mapping[str, Mapping[str, Any]],
    interval: SourceInterval,
) -> frozenset[str]:
    if not isinstance(raw, list) or not raw:
        raise FrameContractError("frame segment requires evidence")
    result: set[str] = set()
    for row in raw:
        if not isinstance(row, Mapping):
            raise FrameContractError("frame evidence row must be an object")
        _exact_keys(dict(row), {"block_id", "evidence_quote"}, "frame evidence")
        if not isinstance(row.get("block_id"), str) or not isinstance(
            row.get("evidence_quote"), str
        ):
            raise FrameContractError("frame evidence fields must be strings")
        block_id = str(row.get("block_id") or "")
        quote = str(row.get("evidence_quote") or "")
        block = block_map.get(block_id)
        if block is None or len(_all_spans(str(block["nfc_text"]), quote)) != 1:
            raise FrameContractError("frame evidence quote is missing or ambiguous")
        quote_start = _all_spans(str(block["nfc_text"]), quote)[0][0]
        point = SourcePoint(int(block["order_index"]), quote_start)
        if not interval.contains_point(point):
            raise FrameContractError("frame evidence lies outside its segment")
        result.add(
            "fev_"
            + canonical_hash({"block_id": block_id, "evidence_quote": quote})[:24]
        )
    return frozenset(result)


def validate_frame_response(
    response: Mapping[str, Any], *, selection: Mapping[str, Any]
) -> ValidatedFrameTree:
    """Validate source geometry and produce a local-key-independent tree signature."""

    _exact_keys(dict(response), {"response_status", "segments"}, "frame response")
    status = str(response.get("response_status") or "")
    segments_raw = response.get("segments")
    if status not in {"proposed", "uncertain"} or not isinstance(segments_raw, list):
        raise FrameContractError("frame response status/segments are malformed")
    if status == "uncertain":
        if segments_raw:
            raise FrameContractError("uncertain frame response must have no segments")
        payload = {"unit_id": selection["unit_id"], "status": "uncertain"}
        return ValidatedFrameTree(
            response_status="uncertain",
            canonical_signature=payload,
            canonical_signature_hash=canonical_hash(payload),
            boundary_relaxed_hash=canonical_hash(payload),
            nodes=(),
            deepest_spans=(),
            deepest_by_block={block_id: None for block_id in selection["narrative_block_ids"]},
            evidence_by_node={},
        )
    if not segments_raw:
        raise FrameContractError("proposed frame response must contain a complete tree")

    block_rows = list(selection["ordered_source_blocks"])
    block_map = {str(row["block_id"]): row for row in block_rows}
    block_ids = [str(row["block_id"]) for row in block_rows]
    order = {str(row["block_id"]): int(row["order_index"]) for row in block_rows}
    occurrence_ids = {
        str(row["occurrence_ref"]) for row in selection["occurrence_choices"]
    }
    expected_segment_keys = {
        "local_segment_key",
        "parent_local_key",
        "block_range",
        "start_boundary",
        "end_boundary",
        "frame_kind",
        "story_time_label",
        "narrator_occurrence_ref",
        "narrator_surface",
        "evidence",
    }
    nodes: dict[str, dict[str, Any]] = {}
    for raw in segments_raw:
        if not isinstance(raw, Mapping):
            raise FrameContractError("frame segment must be an object")
        _exact_keys(dict(raw), expected_segment_keys, "frame segment")
        if not isinstance(raw.get("local_segment_key"), str):
            raise FrameContractError("frame local key must be a string")
        key = str(raw.get("local_segment_key") or "")
        if not key or key in nodes or key.startswith("synthetic_root"):
            raise FrameContractError("frame local key is empty, duplicate, or reserved")
        parent = raw.get("parent_local_key")
        if parent is not None and not isinstance(parent, str):
            raise FrameContractError("frame parent key must be string or null")
        raw_range = raw.get("block_range")
        if (
            not isinstance(raw_range, list)
            or len(raw_range) != 2
            or not all(isinstance(value, str) for value in raw_range)
        ):
            raise FrameContractError("frame block range must contain two block ids")
        try:
            start_index = block_ids.index(str(raw_range[0]))
            end_index = block_ids.index(str(raw_range[1]))
        except ValueError as exc:
            raise FrameContractError("frame block range is outside the unit") from exc
        if end_index < start_index:
            raise FrameContractError("frame block range is reversed")
        if not isinstance(raw.get("frame_kind"), str) or not isinstance(
            raw.get("story_time_label"), str
        ):
            raise FrameContractError("frame semantic enum fields must be strings")
        frame_kind = str(raw.get("frame_kind") or "")
        story_time = str(raw.get("story_time_label") or "")
        if frame_kind not in FRAME_KINDS or story_time not in STORY_TIME_LABELS:
            raise FrameContractError("frame semantic enum is foreign")
        narrator_ref = raw.get("narrator_occurrence_ref")
        if narrator_ref is not None and not isinstance(narrator_ref, str):
            raise FrameContractError("frame narrator occurrence must be string or null")
        if not isinstance(raw.get("narrator_surface"), str):
            raise FrameContractError("frame narrator surface must be a string")
        if narrator_ref is not None and str(narrator_ref) not in occurrence_ids:
            raise FrameContractError("frame narrator occurrence is foreign to the unit")
        start_block = block_rows[start_index]
        end_block = block_rows[end_index]
        start_offset = _boundary_offset(
            cast(Mapping[str, Any] | None, raw.get("start_boundary")),
            block=start_block,
            end_boundary=False,
        )
        end_offset = _boundary_offset(
            cast(Mapping[str, Any] | None, raw.get("end_boundary")),
            block=end_block,
            end_boundary=True,
        )
        try:
            interval = SourceInterval(
                SourcePoint(order[str(raw_range[0])], start_offset),
                SourcePoint(order[str(raw_range[1])], end_offset),
            )
        except ValueError as exc:
            raise FrameContractError("frame source interval is empty or reversed") from exc
        nodes[key] = {
            "local_segment_key": key,
            "parent_local_key": parent,
            "block_range": [str(raw_range[0]), str(raw_range[1])],
            "source_interval_object": interval,
            "source_interval": _interval_payload(interval),
            "frame_kind": frame_kind,
            "story_time_label": story_time,
            "narrator_occurrence_ref": None
            if narrator_ref is None
            else str(narrator_ref),
            "narrator_surface": str(raw.get("narrator_surface") or ""),
            "raw_evidence": _clone(raw.get("evidence")),
        }

    depths = _frame_depths(nodes)
    for key, node in nodes.items():
        parent = node["parent_local_key"]
        if parent is not None and not nodes[str(parent)][
            "source_interval_object"
        ].contains_interval(node["source_interval_object"]):
            raise FrameContractError(f"frame child lies outside its parent: {key}")

    siblings: dict[str | None, list[dict[str, Any]]] = {}
    for node in nodes.values():
        siblings.setdefault(cast(str | None, node["parent_local_key"]), []).append(node)
    for rows in siblings.values():
        ordered = sorted(
            rows,
            key=lambda row: (
                row["source_interval_object"].start,
                row["source_interval_object"].end,
                row["local_segment_key"],
            ),
        )
        for left, right in zip(ordered, ordered[1:]):
            if left["source_interval_object"].overlaps(
                right["source_interval_object"]
            ):
                raise FrameContractError("frame siblings overlap")

    children: dict[str | None, list[str]] = {}
    for key, node in nodes.items():
        children.setdefault(cast(str | None, node["parent_local_key"]), []).append(key)
    for parent in children:
        children[parent].sort(
            key=lambda key: (
                nodes[key]["source_interval_object"].start,
                nodes[key]["source_interval_object"].end,
                nodes[key]["frame_kind"],
                nodes[key]["story_time_label"],
            )
        )

    paths: dict[str, tuple[int, ...]] = {}

    def assign(parent: str | None, prefix: tuple[int, ...]) -> None:
        for ordinal, key in enumerate(children.get(parent, []), start=1):
            path = prefix + (ordinal,)
            paths[key] = path
            assign(key, path)

    assign(None, ())
    if set(paths) != set(nodes):
        raise FrameContractError("frame tree cannot be rooted under the synthetic root")

    canonical_key_by_local = {
        key: "node_" + canonical_hash({"path": list(paths[key])})[:20]
        for key in nodes
    }
    evidence_by_node: dict[str, frozenset[str]] = {}
    for key, node in nodes.items():
        evidence_by_node[canonical_key_by_local[key]] = _validate_evidence(
            node["raw_evidence"],
            block_map=block_map,
            interval=node["source_interval_object"],
        )

    local_spans: list[dict[str, Any]] = []
    deepest_by_block: dict[str, str | None] = {}
    narrative_set = set(selection["narrative_block_ids"])
    for block in block_rows:
        block_id = str(block["block_id"])
        if block_id not in narrative_set:
            continue
        block_length = len(str(block["nfc_text"]))
        if block_length == 0:
            containing = [
                key
                for key, node in nodes.items()
                if block_id in block_ids[
                    block_ids.index(node["block_range"][0]) : block_ids.index(node["block_range"][1]) + 1
                ]
            ]
            if not containing:
                raise FrameContractError("frame tree does not cover an empty narrative block")
            deepest = max(depths[key] for key in containing)
            selected_keys = [key for key in containing if depths[key] == deepest]
            if len(selected_keys) != 1:
                raise FrameContractError("frame tree has ambiguous deepest empty-block coverage")
            deepest_by_block[block_id] = canonical_key_by_local[selected_keys[0]]
            continue
        boundaries = {0, block_length}
        block_order = int(block["order_index"])
        for node in nodes.values():
            interval = node["source_interval_object"]
            if interval.start.block_order == block_order:
                boundaries.add(interval.start.char_offset)
            if interval.end.block_order == block_order:
                boundaries.add(interval.end.char_offset)
        ordered_boundaries = sorted(value for value in boundaries if 0 <= value <= block_length)
        block_keys: set[str] = set()
        cursor = 0
        for char_start, char_end in zip(ordered_boundaries, ordered_boundaries[1:]):
            if char_start != cursor or char_end <= char_start:
                raise FrameContractError("frame character coverage has a gap or empty span")
            point = SourcePoint(block_order, char_start)
            containing = [
                key
                for key, node in nodes.items()
                if node["source_interval_object"].contains_point(point)
            ]
            if not containing:
                raise FrameContractError("frame tree leaves a narrative span uncovered")
            deepest = max(depths[key] for key in containing)
            selected_keys = sorted(
                key for key in containing if depths[key] == deepest
            )
            if len(selected_keys) != 1:
                raise FrameContractError("frame tree has ambiguous deepest coverage")
            canonical_key = canonical_key_by_local[selected_keys[0]]
            block_keys.add(canonical_key)
            local_spans.append(
                {
                    "block_id": block_id,
                    "char_start": char_start,
                    "char_end": char_end,
                    "canonical_node_key": canonical_key,
                }
            )
            cursor = char_end
        if cursor != block_length:
            raise FrameContractError("frame tree does not exact-cover a narrative block")
        deepest_by_block[block_id] = next(iter(block_keys)) if len(block_keys) == 1 else None

    canonical_nodes: list[dict[str, Any]] = []
    relaxed_nodes: list[dict[str, Any]] = []
    normalized_nodes: list[dict[str, Any]] = []
    for key in sorted(nodes, key=lambda value: paths[value]):
        node = nodes[key]
        parent = cast(str | None, node["parent_local_key"])
        canonical_key = canonical_key_by_local[key]
        parent_key = None if parent is None else canonical_key_by_local[parent]
        canonical_nodes.append(
            {
                "canonical_node_key": canonical_key,
                "parent_canonical_node_key": parent_key,
                "source_interval": node["source_interval"],
                "frame_kind": node["frame_kind"],
                "story_time_label": node["story_time_label"],
                "narrator_occurrence_ref": node["narrator_occurrence_ref"],
            }
        )
        relaxed_nodes.append(
            {
                "path": list(paths[key]),
                "parent_path": None if parent is None else list(paths[parent]),
                "frame_kind": node["frame_kind"],
                "story_time_label": node["story_time_label"],
                "narrator_occurrence_ref": node["narrator_occurrence_ref"],
            }
        )
        normalized_nodes.append(
            {
                **canonical_nodes[-1],
                "block_range": node["block_range"],
                "narrator_surface": node["narrator_surface"],
                "evidence_refs": sorted(evidence_by_node[canonical_key]),
            }
        )
    signature = {
        "unit_id": selection["unit_id"],
        "canonical_nodes": canonical_nodes,
    }
    relaxed = {"unit_id": selection["unit_id"], "nodes": relaxed_nodes}
    return ValidatedFrameTree(
        response_status="proposed",
        canonical_signature=signature,
        canonical_signature_hash=canonical_hash(signature),
        boundary_relaxed_hash=canonical_hash(relaxed),
        nodes=tuple(normalized_nodes),
        deepest_spans=tuple(local_spans),
        deepest_by_block=deepest_by_block,
        evidence_by_node=evidence_by_node,
    )


def _execute_synthetic(
    *,
    executor: RequestExecutor,
    store: Step5Store,
    request: PersistedFrameRequest,
) -> tuple[Mapping[str, Any], str]:
    raw = executor.execute(_clone(request.rendered.api_request))
    if not isinstance(raw, Mapping):
        raise FrameContractError("synthetic frame executor returned a non-object")
    usage = raw.get("usage")
    if not isinstance(usage, Mapping):
        raise FrameContractError("synthetic frame response lacks usage accounting")
    if any(int(usage.get(key) or 0) != 0 for key in ("prompt_tokens", "completion_tokens")):
        raise FrameContractError("synthetic frame execution consumed remote tokens")
    response = raw.get("response")
    if not isinstance(response, Mapping):
        raise FrameContractError("synthetic frame response payload is missing")
    artifact_hash = store.put_content_artifact(
        "responses",
        {
            "request_artifact_hash": request.request_artifact_hash,
            "request_fingerprint": request.rendered.request_fingerprint,
            "raw_response": _clone(dict(raw)),
        },
    )
    return response, artifact_hash


def _estimate_prompt_tokens(api_request: Mapping[str, Any]) -> int:
    return max(1, (len(canonical_json(dict(api_request))) + 3) // 4)


def build_frame_call_plan(
    *,
    adjudicator_request: RenderedFrameRequest,
    checker_request: RenderedFrameRequest,
    adjudicator_config: FrameCallConfig,
    checker_config: FrameCallConfig,
) -> CallPlanManifest:
    adjudicator_prompt_estimate = _estimate_prompt_tokens(
        adjudicator_request.api_request
    )
    checker_prompt_estimate = _estimate_prompt_tokens(checker_request.api_request)
    if adjudicator_prompt_estimate > adjudicator_config.prompt_token_cap:
        raise FrameContractError(
            "frame adjudicator request exceeds its prompt cap; split the unit"
        )
    if checker_prompt_estimate > checker_config.prompt_token_cap:
        raise FrameContractError(
            "frame checker request exceeds its prompt cap; split the unit"
        )
    entries = (
        CallPlanEntry(
            call_plan_entry_id=f"frame:{adjudicator_request.api_request['metadata']['unit_id']}:J",
            call_kind="J",
            decision_kind="frame",
            shard_id=None,
            authority_route_id=adjudicator_config.route.authority_route_id,
            execution_cost_class=adjudicator_config.execution_cost_class,
            quota_bucket_id=adjudicator_config.quota_bucket_id,
            model_id=adjudicator_config.route.model_identity.model_id,
            prompt_tokens_estimate=adjudicator_prompt_estimate,
            max_output_tokens=adjudicator_config.max_output_tokens,
            technical_retry_cap=1,
        ),
        CallPlanEntry(
            call_plan_entry_id=f"frame:{checker_request.api_request['metadata']['unit_id']}:Cd",
            call_kind="Cd",
            decision_kind="frame",
            shard_id=None,
            authority_route_id=checker_config.route.authority_route_id,
            execution_cost_class=checker_config.execution_cost_class,
            quota_bucket_id=checker_config.quota_bucket_id,
            model_id=checker_config.route.model_identity.model_id,
            prompt_tokens_estimate=checker_prompt_estimate,
            max_output_tokens=checker_config.max_output_tokens,
            technical_retry_cap=1,
        ),
    )
    caps = {symbol: 0 for symbol in CALL_SYMBOLS}
    caps["J"] = 1
    caps["Cd"] = 1
    return seal_call_plan(entries=entries, finite_caps=caps)


def _view_hash(payload: Mapping[str, Any]) -> dict[str, Any]:
    body = _clone(dict(payload))
    body["frame_view_hash"] = canonical_hash(body)
    return body


def verify_confirmed_frame_view(frame_view: Mapping[str, Any]) -> None:
    body = _clone(dict(frame_view))
    own_hash = str(body.pop("frame_view_hash", ""))
    if not own_hash or canonical_hash(body) != own_hash:
        raise FrameContractError("confirmed frame view content hash mismatch")
    if body.get("status") not in {"active", "unknown"}:
        raise FrameContractError("confirmed frame view has a foreign status")
    coverage = body.get("verified_frame_coverage")
    if (
        not isinstance(coverage, Mapping)
        or not isinstance(coverage.get("numerator"), int)
        or not isinstance(coverage.get("denominator"), int)
        or int(coverage["denominator"]) <= 0
        or not 0 <= int(coverage["numerator"]) <= int(coverage["denominator"])
    ):
        raise FrameContractError("confirmed frame view coverage is malformed")
    if body["status"] == "unknown" and (
        body.get("segments")
        or body.get("deepest_frame_spans")
        or any(value is not None for value in (body.get("deepest_frame_by_block") or {}).values())
    ):
        raise FrameContractError("unknown frame view contains active frame data")


def _unknown_view(
    selection: Mapping[str, Any], *, parent_generation_hash: str | None
) -> dict[str, Any]:
    denominator = len(selection["narrative_block_ids"])
    return _view_hash(
        {
            "state_lineage_id": selection["state_lineage_id"],
            "unit_id": selection["unit_id"],
            "bundle_manifest_hash": selection["bundle_manifest_hash"],
            "parent_generation_hash": parent_generation_hash,
            "selection_universe_hash": selection["selection_universe_hash"],
            "frame_tree_signature_hash": None,
            "frame_contract_version": FRAME_CONTRACT_VERSION,
            "frame_decision_revision_hash": None,
            "status": "unknown",
            "synthetic_root_id": "root_"
            + canonical_hash(
                {
                    "state_lineage_id": selection["state_lineage_id"],
                    "unit_id": selection["unit_id"],
                }
            )[:24],
            "segments": [],
            "deepest_frame_spans": [],
            "deepest_frame_by_block": {
                block_id: None for block_id in selection["narrative_block_ids"]
            },
            "verified_frame_coverage": {"numerator": 0, "denominator": denominator},
        }
    )


def _support_sets_for_tree(
    adjudicator_tree: ValidatedFrameTree,
    checker_tree: ValidatedFrameTree,
) -> tuple[SupportSet, ...]:
    adjudicator_members = frozenset(
        member
        for values in adjudicator_tree.evidence_by_node.values()
        for member in values
    )
    checker_members = frozenset(
        member for values in checker_tree.evidence_by_node.values() for member in values
    )
    if not adjudicator_members or not checker_members:
        raise FrameContractError("active frame tree lacks independent evidence families")
    return (
        SupportSet(
            support_set_id="frame_adjudicator_evidence",
            member_ids=adjudicator_members,
        ),
        SupportSet(
            support_set_id="frame_checker_evidence",
            member_ids=checker_members,
        ),
    )


def _active_view_and_records(
    *,
    selection: Mapping[str, Any],
    tree: ValidatedFrameTree,
    checker_tree: ValidatedFrameTree,
    decision: DecisionRevision,
    parent_generation_hash: str | None,
    store: Step5Store,
) -> tuple[dict[str, Any], tuple[str, ...], tuple[SupportedItem, ...]]:
    decision_hash = content_address(decision)
    synthetic_root_id = "root_" + canonical_hash(
        {
            "state_lineage_id": selection["state_lineage_id"],
            "unit_id": selection["unit_id"],
        }
    )[:24]
    segment_ids = {
        str(node["canonical_node_key"]): "frm_"
        + canonical_hash(
            {
                "state_lineage_id": selection["state_lineage_id"],
                "unit_id": selection["unit_id"],
                "active_tree_signature_hash": tree.canonical_signature_hash,
                "canonical_node_key": node["canonical_node_key"],
                "frame_contract_version": FRAME_CONTRACT_VERSION,
            }
        )[:24]
        for node in tree.nodes
    }
    overlay_refs: list[str] = []
    supported_items: list[SupportedItem] = []
    confirmed_segments: list[dict[str, Any]] = []
    for node in tree.nodes:
        key = str(node["canonical_node_key"])
        parent_key = node.get("parent_canonical_node_key")
        evidence_sets = (
            SupportSet(
                support_set_id=f"{segment_ids[key]}:adjudicator",
                member_ids=tree.evidence_by_node[key],
            ),
            SupportSet(
                support_set_id=f"{segment_ids[key]}:checker",
                member_ids=checker_tree.evidence_by_node[key],
            ),
        )
        overlay = {
            "record_kind": "active_frame_segment",
            "segment_id": segment_ids[key],
            "parent_segment_id": synthetic_root_id
            if parent_key is None
            else segment_ids[str(parent_key)],
            "unit_id": selection["unit_id"],
            "source_interval": _clone(node["source_interval"]),
            "frame_kind": node["frame_kind"],
            "story_time_label": node["story_time_label"],
            "narrator_occurrence_ref": node["narrator_occurrence_ref"],
            "narrator_entity_id": None,
            "evidence_refs": sorted(
                tree.evidence_by_node[key] | checker_tree.evidence_by_node[key]
            ),
            "decision_revision_hash": decision_hash,
            "frame_contract_version": FRAME_CONTRACT_VERSION,
        }
        overlay_ref = store.put_content_artifact("overlay", overlay)
        overlay_refs.append(overlay_ref)
        supported_items.append(
            SupportedItem(
                item_id=overlay_ref,
                support_alternatives=evidence_sets,
            )
        )
        confirmed_segments.append(
            {
                "segment_id": segment_ids[key],
                "overlay_record_ref": overlay_ref,
                "parent_segment_id": overlay["parent_segment_id"],
                "source_interval": _clone(node["source_interval"]),
                "frame_kind": node["frame_kind"],
                "story_time_label": node["story_time_label"],
                "narrator_occurrence_ref": node["narrator_occurrence_ref"],
                "narrator_entity_id": None,
                "evidence_refs": _clone(overlay["evidence_refs"]),
                "decision_revision_hash": decision_hash,
            }
        )
    spans = [
        {
            **_clone(span),
            "segment_id": segment_ids[str(span["canonical_node_key"])],
        }
        for span in tree.deepest_spans
    ]
    for span in spans:
        span.pop("canonical_node_key", None)
    deepest_by_block = {
        block_id: None if key is None else segment_ids[str(key)]
        for block_id, key in tree.deepest_by_block.items()
    }
    denominator = len(selection["narrative_block_ids"])
    view = _view_hash(
        {
            "state_lineage_id": selection["state_lineage_id"],
            "unit_id": selection["unit_id"],
            "bundle_manifest_hash": selection["bundle_manifest_hash"],
            "parent_generation_hash": parent_generation_hash,
            "selection_universe_hash": selection["selection_universe_hash"],
            "frame_tree_signature_hash": tree.canonical_signature_hash,
            "frame_contract_version": FRAME_CONTRACT_VERSION,
            "frame_decision_revision_hash": decision_hash,
            "status": "active",
            "synthetic_root_id": synthetic_root_id,
            "segments": confirmed_segments,
            "deepest_frame_spans": spans,
            "deepest_frame_by_block": deepest_by_block,
            "verified_frame_coverage": {
                "numerator": denominator,
                "denominator": denominator,
            },
        }
    )
    return view, tuple(sorted(overlay_refs)), tuple(supported_items)


def _parse_supported_items(payload: Mapping[str, Any]) -> list[SupportedItem]:
    result: list[SupportedItem] = []
    for raw in payload.get("supported_items") or []:
        alternatives = tuple(
            SupportSet(
                support_set_id=str(row["support_set_id"]),
                member_ids=frozenset(str(value) for value in row["member_ids"]),
            )
            for row in raw.get("support_alternatives") or []
        )
        result.append(
            SupportedItem(item_id=str(raw["item_id"]), support_alternatives=alternatives)
        )
    return result


def _publish_frame_view(
    *,
    store: Step5Store,
    selection: Mapping[str, Any],
    frame_view: Mapping[str, Any],
    decision: DecisionRevision | None,
    new_overlay_refs: tuple[str, ...],
    new_supported_items: tuple[SupportedItem, ...],
    authority_policy: AuthorityIndependencePolicy,
) -> tuple[str, dict[str, str], dict[str, Any]]:
    verify_confirmed_frame_view(frame_view)
    lineage = str(selection["state_lineage_id"])
    current = store.current_generation_hash(lineage)
    previous_semantic: dict[str, Any] = {
        "schema_version": FRAME_GENERATION_SCHEMA_VERSION,
        "frame_view_refs": {},
        "overlay_record_refs": [],
        "decision_revisions": [],
        "supported_items": [],
    }
    previous_materialized: dict[str, Any] = {
        "schema_version": FRAME_GENERATION_SCHEMA_VERSION,
        "frame_views": {},
    }
    if current is not None:
        generation = store.load_generation(current)
        previous_semantic = store.load_semantic_state(
            str(generation["semantic_state_hash"])
        )
        previous_materialized = store.load_materialized_view(
            str(generation["materialized_view_hash"])
        )
        existing = (previous_materialized.get("frame_views") or {}).get(
            selection["unit_id"]
        )
        if isinstance(existing, Mapping):
            verify_confirmed_frame_view(existing)
        if isinstance(existing, Mapping) and (
            existing.get("selection_universe_hash")
            == frame_view.get("selection_universe_hash")
            and existing.get("frame_tree_signature_hash")
            == frame_view.get("frame_tree_signature_hash")
            and existing.get("status") == frame_view.get("status")
            and existing.get("frame_contract_version") == FRAME_CONTRACT_VERSION
        ):
            return (
                current,
                {
                    "frame_view_hash": str(existing["frame_view_hash"]),
                    "published_generation_hash": current,
                },
                _clone(dict(existing)),
            )

    replaced_overlay_refs: set[str] = set()
    existing_view = (previous_materialized.get("frame_views") or {}).get(
        selection["unit_id"]
    )
    if isinstance(existing_view, Mapping) and existing_view.get("status") == "active":
        for segment in existing_view.get("segments") or []:
            if not isinstance(segment, Mapping) or not segment.get("overlay_record_ref"):
                raise FrameContractError(
                    "active prior frame view lacks overlay provenance"
                )
            replaced_overlay_refs.add(str(segment["overlay_record_ref"]))

    frame_views = _clone(previous_materialized.get("frame_views") or {})
    frame_views[selection["unit_id"]] = _clone(dict(frame_view))
    materialized = {
        "schema_version": FRAME_GENERATION_SCHEMA_VERSION,
        "frame_views": frame_views,
    }
    materialized_hash = canonical_hash(materialized)

    previous_overlay_refs = set(
        str(value) for value in previous_semantic.get("overlay_record_refs") or []
    )
    if not replaced_overlay_refs.issubset(previous_overlay_refs):
        raise FrameContractError("prior frame view overlay refs are absent from semantic state")
    retained_overlay_refs = previous_overlay_refs - replaced_overlay_refs
    all_overlay_refs = retained_overlay_refs | set(new_overlay_refs)
    decisions = list(previous_semantic.get("decision_revisions") or [])
    if decision is not None:
        decisions.append(decision.to_canonical_payload())
    items = _parse_supported_items(previous_semantic)
    by_item = {
        item.item_id: item
        for item in items
        if item.item_id not in replaced_overlay_refs
    }
    by_item.update({item.item_id: item for item in new_supported_items})
    all_items = tuple(by_item[key] for key in sorted(by_item))
    support_index = build_support_reverse_index(all_items)
    semantic = {
        "schema_version": FRAME_GENERATION_SCHEMA_VERSION,
        "frame_view_refs": {
            **_clone(previous_semantic.get("frame_view_refs") or {}),
            selection["unit_id"]: frame_view["frame_view_hash"],
        },
        "overlay_record_refs": sorted(all_overlay_refs),
        "decision_revisions": decisions,
        "supported_items": [item.to_canonical_payload() for item in all_items],
    }
    draft = FullScopeChangeSet(
        state_lineage_id=lineage,
        source_scope_id=str(selection["unit_id"]),
        parent_generation_hash=current,
        bundle_manifest_hash=str(selection["bundle_manifest_hash"]),
        validator_contract_hash=FRAME_VALIDATOR_CONTRACT_HASH,
        decision_revisions=() if decision is None else (decision,),
        overlay_record_refs=frozenset(new_overlay_refs),
        quarantine_record_refs=frozenset(),
        invalidation_refs=tuple(sorted(replaced_overlay_refs)),
        retained_row_ids=frozenset(retained_overlay_refs),
        materialized_view_hash=materialized_hash,
        estimated_apply_cost=0,
    )
    changeset = cast(
        FullScopeChangeSet,
        with_content_address(draft),
    )
    generation = store.publish_generation(
        changeset=changeset,
        support_index=support_index,
        semantic_state=semantic,
        materialized_view=materialized,
        generation_schema_version=FRAME_GENERATION_SCHEMA_VERSION,
        authority_policy_hash=authority_policy.policy_hash,
        qualification_policy_hash=authority_policy.qualification_policy_hash,
        expected_current=current,
    )
    return (
        generation.generation_hash,
        {
            "frame_view_hash": str(frame_view["frame_view_hash"]),
            "published_generation_hash": generation.generation_hash,
        },
        _clone(dict(frame_view)),
    )


def run_frame_confirmation(
    *,
    bundle: Mapping[str, Any],
    unit_id: str,
    prompt_provider: PromptProvider,
    executor: RequestExecutor,
    store: Step5Store,
    adjudicator_config: FrameCallConfig,
    checker_config: FrameCallConfig,
    authority_policy: AuthorityIndependencePolicy,
    qualification_manifests: Mapping[str, QualificationManifest],
) -> FrameRunResult:
    """Run the real S5B renderer/validator/apply path with a synthetic executor."""

    if not authority_policy.policy_hash:
        raise FrameContractError("authority policy must be content-addressed")
    selection = build_frame_selection_universe(bundle, unit_id=unit_id)
    adjudicator_rendered = render_frame_request(
        selection=selection,
        prompt_provider=prompt_provider,
        config=adjudicator_config,
        role="adjudicator",
    )
    checker_rendered = render_frame_request(
        selection=selection,
        prompt_provider=prompt_provider,
        config=checker_config,
        role="checker",
    )
    call_plan = build_frame_call_plan(
        adjudicator_request=adjudicator_rendered,
        checker_request=checker_rendered,
        adjudicator_config=adjudicator_config,
        checker_config=checker_config,
    )
    adjudicator_request = persist_frame_request(store, adjudicator_rendered)
    adjudicator_payload, adjudicator_response_hash = _execute_synthetic(
        executor=executor,
        store=store,
        request=adjudicator_request,
    )
    adjudicator_tree = validate_frame_response(
        adjudicator_payload, selection=selection
    )
    proposal_record_id = "prop_" + canonical_hash(
        {
            "request_fingerprint": adjudicator_rendered.request_fingerprint,
            "signature": adjudicator_tree.canonical_signature_hash,
        }
    )[:24]
    question = DecisionQuestion(
        semantic_question_hash=canonical_hash(
            {
                "decision_kind": "frame",
                "unit_id": unit_id,
                "frame_policy_version": FRAME_POLICY_VERSION,
            }
        ),
        selection_universe_hash=str(selection["selection_universe_hash"]),
    )
    question_hash = content_address(question)
    parent_generation = store.current_generation_hash(str(selection["state_lineage_id"]))
    executed = 1
    request_hashes = [adjudicator_request.request_artifact_hash]
    response_hashes = [adjudicator_response_hash]
    decision_state = "pending_resolution"
    decision: DecisionRevision | None = None
    new_overlay_refs: tuple[str, ...] = ()
    new_supported_items: tuple[SupportedItem, ...] = ()
    boundary_disagreement = 0

    if adjudicator_tree.response_status == "uncertain":
        view = _unknown_view(
            selection, parent_generation_hash=parent_generation
        )
    else:
        checker_request = persist_frame_request(store, checker_rendered)
        checker_payload, checker_response_hash = _execute_synthetic(
            executor=executor,
            store=store,
            request=checker_request,
        )
        checker_tree = validate_frame_response(checker_payload, selection=selection)
        executed += 1
        request_hashes.append(checker_request.request_artifact_hash)
        response_hashes.append(checker_response_hash)
        if checker_tree.response_status == "proposed" and (
            checker_tree.canonical_signature_hash
            == adjudicator_tree.canonical_signature_hash
        ):
            manifest = checker_manifest_from_persisted_lineage(
                store, checker_request.lineage_artifact_hash
            )
            blinding = BlindingValidationRecord(
                checker_request_fingerprint=checker_rendered.request_fingerprint,
                checker_input_section_manifest_hash=(
                    manifest.checker_input_section_manifest_hash
                ),
                checker_input_section_manifest=manifest,
                adjudicator_response_artifact_hash=adjudicator_response_hash,
                validator_contract_hash=FRAME_VALIDATOR_CONTRACT_HASH,
            )
            if validate_blinding(blinding) != "valid":
                raise FrameContractError("frame checker blinding validation failed")
            blinding_hash = store.put_content_artifact(
                "authority", blinding.to_canonical_payload()
            )
            agreement = AgreementRecord(
                proposal_record_id=proposal_record_id,
                question=question,
                request_fingerprint_a=adjudicator_rendered.request_fingerprint,
                request_fingerprint_b=checker_rendered.request_fingerprint,
                canonical_signature_hash_a=adjudicator_tree.canonical_signature_hash,
                canonical_signature_hash_b=checker_tree.canonical_signature_hash,
                blinding_validation_record_hash=blinding_hash,
                adjudicator_route_id=adjudicator_config.route.authority_route_id,
                checker_route_id=checker_config.route.authority_route_id,
            )
            agreement_hash = store.put_content_artifact(
                "authority", agreement.to_canonical_payload()
            )
            decision_state = promote(
                adjudicator_route=adjudicator_config.route,
                checker_route=checker_config.route,
                agreement_records=(agreement,),
                decision_kind="frame",
                decision_question=question,
                canonical_signature_hash_value=adjudicator_tree.canonical_signature_hash,
                proposal_record_id_value=proposal_record_id,
                adjudicator_response_artifact_hash=adjudicator_response_hash,
                authority_policy=authority_policy,
                qualification_manifests=qualification_manifests,
                blinding_records={blinding_hash: blinding},
                persisted_agreement_hashes=frozenset({agreement_hash}),
            )
            if decision_state == "active":
                support_sets = _support_sets_for_tree(
                    adjudicator_tree, checker_tree
                )
                decision_id = "dec_frame_" + canonical_hash(
                    {
                        "state_lineage_id": selection["state_lineage_id"],
                        "unit_id": unit_id,
                        "signature": adjudicator_tree.canonical_signature_hash,
                        "contract": FRAME_CONTRACT_VERSION,
                    }
                )[:24]
                qualification_hashes = frozenset(
                    binding.qualification_manifest_hash
                    for binding in checker_config.route.qualification_bindings
                    if binding.decision_kind == "frame"
                )
                decision = DecisionRevision(
                    decision_id=decision_id,
                    proposal_record_id=proposal_record_id,
                    decision_kind="frame",
                    state="active",
                    question_hash=question_hash,
                    canonical_signature_hash=adjudicator_tree.canonical_signature_hash,
                    support_sets=support_sets,
                    decided_at_scope=unit_id,
                    qualification_manifest_hashes=qualification_hashes,
                    authority_evidence_record_hashes=frozenset(
                        {agreement_hash, blinding_hash}
                    ),
                    authority_policy_hash=authority_policy.policy_hash,
                    validator_contract_hash=FRAME_VALIDATOR_CONTRACT_HASH,
                )
                view, new_overlay_refs, new_supported_items = _active_view_and_records(
                    selection=selection,
                    tree=adjudicator_tree,
                    checker_tree=checker_tree,
                    decision=decision,
                    parent_generation_hash=parent_generation,
                    store=store,
                )
            else:
                view = _unknown_view(
                    selection, parent_generation_hash=parent_generation
                )
        else:
            if (
                checker_tree.response_status == "proposed"
                and checker_tree.boundary_relaxed_hash
                == adjudicator_tree.boundary_relaxed_hash
                and checker_tree.canonical_signature_hash
                != adjudicator_tree.canonical_signature_hash
            ):
                boundary_disagreement = 1
            view = _unknown_view(
                selection, parent_generation_hash=parent_generation
            )

    generation_hash, handle, effective_view = _publish_frame_view(
        store=store,
        selection=selection,
        frame_view=view,
        decision=decision,
        new_overlay_refs=new_overlay_refs,
        new_supported_items=new_supported_items,
        authority_policy=authority_policy,
    )
    return FrameRunResult(
        frame_view=effective_view,
        frame_view_handle=handle,
        generation_hash=generation_hash,
        decision_state=decision_state,
        request_artifact_hashes=tuple(request_hashes),
        response_artifact_hashes=tuple(response_hashes),
        call_plan=call_plan,
        executed_call_count=executed,
        valid_boundary_disagreement_units=boundary_disagreement,
    )


def deepest_frame_at(
    frame_view: Mapping[str, Any], *, block_id: str, char_offset: int
) -> str | None:
    """S5C-facing query that requires no bundle or checkpoint reopen."""

    verify_confirmed_frame_view(frame_view)
    if frame_view.get("status") != "active":
        return None
    for span in frame_view.get("deepest_frame_spans") or []:
        if (
            span.get("block_id") == block_id
            and int(span.get("char_start") or 0)
            <= char_offset
            < int(span.get("char_end") or 0)
        ):
            return str(span.get("segment_id") or "") or None
    return None


def load_confirmed_frame_view(
    store: Step5Store,
    *,
    state_lineage_id: str,
    handle: Mapping[str, str],
) -> dict[str, Any]:
    """Resolve an S5B handle only when it names the current lineage generation."""

    generation_hash = str(handle.get("published_generation_hash") or "")
    frame_view_hash = str(handle.get("frame_view_hash") or "")
    if (
        not generation_hash
        or not frame_view_hash
        or store.current_generation_hash(state_lineage_id) != generation_hash
    ):
        raise FrameContractError("frame view handle is stale or incomplete")
    generation = store.load_generation(generation_hash)
    materialized = store.load_materialized_view(
        str(generation["materialized_view_hash"])
    )
    matches = [
        dict(value)
        for value in (materialized.get("frame_views") or {}).values()
        if isinstance(value, Mapping) and value.get("frame_view_hash") == frame_view_hash
    ]
    if len(matches) != 1:
        raise FrameContractError("frame view handle is not uniquely materialized")
    verify_confirmed_frame_view(matches[0])
    return matches[0]


__all__ = [
    "ADJUDICATOR_PROMPT_ID",
    "CHECKER_PROMPT_ID",
    "FRAME_CONTRACT_VERSION",
    "FRAME_RESPONSE_SCHEMA",
    "FRAME_VALIDATOR_CONTRACT_HASH",
    "FrameCallConfig",
    "FrameContractError",
    "FrameRunResult",
    "PersistedFrameRequest",
    "RenderedFrameRequest",
    "ValidatedFrameTree",
    "build_frame_call_plan",
    "build_frame_selection_universe",
    "checker_manifest_from_persisted_lineage",
    "deepest_frame_at",
    "load_confirmed_frame_view",
    "persist_frame_request",
    "render_frame_request",
    "run_frame_confirmation",
    "validate_frame_response",
    "verify_confirmed_frame_view",
]
