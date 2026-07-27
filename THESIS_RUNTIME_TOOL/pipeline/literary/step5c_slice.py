"""Read-only S5C identity vertical-slice contracts and Phase-A gates.

This module deliberately contains no provider client and no semantic store.  It
only renders, validates, batches, measures, and pre-registers proposal-only
experiments over an already verified Step-4 bundle.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from datetime import date
from hashlib import sha256
import json
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence

from pipeline.literary.builder_pilot import load_system_prompt_from_design
from pipeline.literary.checkpoint import canonical_hash, canonical_json
from pipeline.literary.checkpoint_v3 import write_json_exclusive
from pipeline.literary.step5_frame import (
    ValidatedFrameTree,
    validate_frame_response,
)


UPSTREAM_PROMPT_IDS = {
    "b1": "literary_lexicon_v3",
    "b2": "literary_narrative_v3",
    "b3": "literary_digest_v3",
}
IDENTITY_RETRIEVAL_PROMPT_ID = "literary_identity_retrieval_slice_v1"
IDENTITY_PROPOSAL_PROMPT_ID = "literary_identity_proposal_slice_v1"
SLICE_CONTRACT_VERSION = "literary_step5c_slice_phase_a_v2"
SLICE_REPORT_PREFIX = "literary_m4f_s5c_slice"

REFERENT_KINDS = frozenset(
    {
        "person",
        "animal",
        "nonhuman_character",
        "place",
        "group_reference",
        "object",
        "unknown",
    }
)
OUTCOMES = frozenset(
    {
        "correct",
        "wrong",
        "unknown",
        "ground_truth_ambiguous",
        "upstream_occurrence_missing",
    }
)
ROOT_CAUSES = frozenset(
    {
        "context_missing",
        "retrieval_miss",
        "upstream_frame_error",
        "prompt_defect",
        "model_error",
        "validator_drop",
        "source_ambiguity",
        "indeterminate",
    }
)
FORBIDDEN_MODEL_KEYS = frozenset(
    {
        "entity_id",
        "binding_id",
        "decision_id",
        "witness_id",
        "active_id",
        "group_id",
    }
)

RETRIEVAL_OUTPUT_SCHEMA = {
    "type": "object",
    "required": ["targets"],
    "additionalProperties": False,
}
PROPOSAL_OUTPUT_SCHEMA = {
    "type": "object",
    "required": ["proposals"],
    "additionalProperties": False,
}


class SliceContractError(ValueError):
    """Raised when a Phase-A or read-only slice invariant is violated."""


@dataclass(frozen=True, slots=True)
class IdentitySliceRequest:
    role: str
    canonical_request_json: str
    request_fingerprint: str

    def body(self) -> dict[str, Any]:
        value = json.loads(self.canonical_request_json)
        if not isinstance(value, dict):
            raise SliceContractError("identity slice request must be an object")
        return value


@dataclass(frozen=True, slots=True)
class IdentitySliceCallResult:
    role: str
    request_fingerprint: str
    normalized_response: dict[str, Any]
    usage: dict[str, int | float]
    provider: str
    model: str
    request_path: str
    attempt_paths: tuple[str, ...]
    validation_path: str


def _clone(value: Any) -> Any:
    return deepcopy(value)


def _require_mapping(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise SliceContractError(f"{label} must be an object")
    return value


def _require_sequence(value: Any, label: str) -> Sequence[Any]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise SliceContractError(f"{label} must be a list")
    return value


def _exact_keys(value: Mapping[str, Any], expected: set[str], label: str) -> None:
    if set(value) != expected:
        raise SliceContractError(
            f"{label} fields mismatch: expected={sorted(expected)}, got={sorted(value)}"
        )


def load_slice_prompt(design_doc: Path, prompt_id: str) -> str:
    allowed = set(UPSTREAM_PROMPT_IDS.values()) | {
        IDENTITY_RETRIEVAL_PROMPT_ID,
        IDENTITY_PROPOSAL_PROMPT_ID,
    }
    if prompt_id not in allowed:
        raise SliceContractError(f"foreign S5C prompt id: {prompt_id}")
    prompt = load_system_prompt_from_design(Path(design_doc), prompt_id)
    if f"Prompt version: {prompt_id}." not in prompt:
        raise SliceContractError(f"prompt marker mismatch: {prompt_id}")
    if "JSON" not in prompt:
        raise SliceContractError(f"prompt lacks JSON instruction: {prompt_id}")
    return prompt


def prompt_manifest(design_doc: Path) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    for prompt_id in [
        *UPSTREAM_PROMPT_IDS.values(),
        IDENTITY_RETRIEVAL_PROMPT_ID,
        IDENTITY_PROPOSAL_PROMPT_ID,
    ]:
        text = load_slice_prompt(design_doc, prompt_id)
        rows.append(
            {
                "prompt_id": prompt_id,
                "sha256": sha256(text.encode("utf-8")).hexdigest(),
                "utf8_bytes": len(text.encode("utf-8")),
            }
        )
    payload = {"contract_version": SLICE_CONTRACT_VERSION, "prompts": rows}
    return {**payload, "manifest_hash": canonical_hash(payload)}


def assert_book_neutral(prompts: Iterable[str], forbidden_terms: Iterable[str]) -> None:
    folded = "\n".join(prompts).casefold()
    leaks = sorted({term for term in forbidden_terms if term.casefold() in folded})
    if leaks:
        raise SliceContractError(f"book-specific prompt terms found: {leaks}")


def render_identity_slice_request(
    *,
    role: str,
    payload: Mapping[str, Any],
    prompt_text: str,
    provider: str,
    model_config: Mapping[str, Any],
    upstream_lineage_hashes: Mapping[str, str],
) -> IdentitySliceRequest:
    """Freeze the exact request that a Phase-B callback may transmit."""

    if role == "retrieval":
        prompt_id = IDENTITY_RETRIEVAL_PROMPT_ID
        output_schema = RETRIEVAL_OUTPUT_SCHEMA
    elif role == "proposal":
        prompt_id = IDENTITY_PROPOSAL_PROMPT_ID
        output_schema = PROPOSAL_OUTPUT_SCHEMA
    else:
        raise SliceContractError(f"foreign identity slice role: {role}")
    if f"Prompt version: {prompt_id}." not in prompt_text:
        raise SliceContractError("identity prompt marker mismatch")
    if not provider.strip() or not model_config.get("model"):
        raise SliceContractError("identity provider/model config is incomplete")
    if not upstream_lineage_hashes or any(not str(value) for value in upstream_lineage_hashes.values()):
        raise SliceContractError("identity request lacks complete upstream lineage")
    model_config_copy = _clone(dict(model_config))
    payload_copy = _clone(dict(payload))
    model_input = {
        "role": role,
        "payload": payload_copy,
        "output_schema": _clone(output_schema),
    }
    messages = [
        {"role": "system", "content": prompt_text},
        {"role": "user", "content": canonical_json(model_input)},
    ]
    model_config_hash = canonical_hash(model_config_copy)
    body = {
        "contract_version": SLICE_CONTRACT_VERSION,
        "execution_mode": "real_api",
        "proposal_only": True,
        "role": role,
        "provider": provider.strip(),
        "prompt_id": prompt_id,
        "prompt_text": prompt_text,
        "prompt_sha256": sha256(prompt_text.encode("utf-8")).hexdigest(),
        "model_config": model_config_copy,
        "model_config_hash": model_config_hash,
        "cache_namespace": f"{role}_{model_config_hash[:16]}",
        "output_schema": _clone(output_schema),
        "output_schema_hash": canonical_hash(output_schema),
        "payload": payload_copy,
        "payload_hash": canonical_hash(payload_copy),
        "upstream_lineage_hashes": dict(sorted(upstream_lineage_hashes.items())),
        "rendered_messages": messages,
        "response_format": {"type": "json_object"},
    }
    encoded = canonical_json(body)
    return IdentitySliceRequest(
        role=role,
        canonical_request_json=encoded,
        request_fingerprint=canonical_hash(body),
    )


def _validate_usage(value: Any) -> dict[str, int | float]:
    usage = _require_mapping(value, "identity usage")
    expected = {
        "prompt_tokens",
        "completion_tokens",
        "cached_tokens",
        "reasoning_tokens",
        "cost_usd",
    }
    _exact_keys(usage, expected, "identity usage")
    result: dict[str, int | float] = {}
    for key in expected:
        raw = usage.get(key)
        if not isinstance(raw, (int, float)) or float(raw) < 0:
            raise SliceContractError(f"identity usage is malformed: {key}")
        result[key] = raw
    return result


def _validate_identity_raw_result(
    raw_result: Mapping[str, Any],
    *,
    request_body: Mapping[str, Any],
    role: str,
) -> tuple[dict[str, Any], dict[str, int | float], str, str]:
    _exact_keys(
        raw_result,
        {"response", "usage", "provider", "model", "cache_key"},
        "identity raw result",
    )
    provider = str(raw_result.get("provider") or "")
    if provider != request_body["provider"]:
        raise SliceContractError("identity provider metadata mismatch")
    expected_model = str((request_body.get("model_config") or {}).get("model") or "")
    model = str(raw_result.get("model") or "")
    if model != expected_model:
        raise SliceContractError("identity model metadata mismatch")
    if not str(raw_result.get("cache_key") or ""):
        raise SliceContractError("identity callback omitted cache key")
    usage = _validate_usage(raw_result.get("usage"))
    response = _require_mapping(raw_result.get("response"), "identity response")
    if role == "retrieval":
        normalized = validate_retrieval_response(
            response, request_payload=request_body["payload"]
        )
    else:
        normalized = validate_proposal_response(
            response, request_payload=request_body["payload"]
        )
    return normalized, usage, provider, model


def execute_identity_slice_request(
    request: IdentitySliceRequest,
    *,
    request_llm: Callable[[list[dict[str, Any]], Mapping[str, Any], bool], Mapping[str, Any]],
    out_dir: Path,
    reports_root: Path,
) -> IdentitySliceCallResult:
    """Persist-call-persist-validate with one technical retry and no semantic retry."""

    body = request.body()
    if canonical_hash(body) != request.request_fingerprint or body.get("role") != request.role:
        raise SliceContractError("identity request fingerprint/role mismatch")
    resolved_root = assert_slice_output_root(Path(out_dir), reports_root=Path(reports_root))
    call_dir = resolved_root / "identity_calls" / request.request_fingerprint
    request_path = call_dir / "request.json"
    write_json_exclusive(request_path, body)
    raw_result: Mapping[str, Any] | None = None
    attempt_paths: list[Path] = []
    last_error: Exception | None = None
    for attempt in range(1, 3):
        try:
            raw_result = request_llm(
                _clone(body["rendered_messages"]),
                {
                    "provider": body["provider"],
                    "model_config": _clone(body["model_config"]),
                    "request_fingerprint": request.request_fingerprint,
                    "cache_namespace": body["cache_namespace"],
                    "response_format": _clone(body["response_format"]),
                },
                attempt == 2,
            )
            if not isinstance(raw_result, Mapping):
                raise SliceContractError("identity callback returned a non-object")
            attempt_path = call_dir / f"attempt_{attempt:02d}_raw.json"
            write_json_exclusive(attempt_path, raw_result)
            attempt_paths.append(attempt_path)
            break
        except SliceContractError:
            raise
        except Exception as exc:
            last_error = exc
            attempt_path = call_dir / f"attempt_{attempt:02d}_transport_error.json"
            write_json_exclusive(
                attempt_path,
                {"error_type": type(exc).__name__, "message": str(exc)},
            )
            attempt_paths.append(attempt_path)
    if raw_result is None:
        raise SliceContractError(f"identity transport failed after one retry: {last_error}")

    validation_path = call_dir / "validation.json"
    try:
        normalized, usage, provider, model = _validate_identity_raw_result(
            raw_result,
            request_body=body,
            role=request.role,
        )
    except SliceContractError as exc:
        write_json_exclusive(
            validation_path,
            {"ok": False, "error_type": type(exc).__name__, "message": str(exc)},
        )
        raise
    write_json_exclusive(
        validation_path,
        {
            "ok": True,
            "normalized_response_hash": canonical_hash(normalized),
            "proposal_only": True,
        },
    )
    relative = lambda path: path.resolve().relative_to(resolved_root).as_posix()
    return IdentitySliceCallResult(
        role=request.role,
        request_fingerprint=request.request_fingerprint,
        normalized_response=_clone(normalized),
        usage=usage,
        provider=provider,
        model=model,
        request_path=relative(request_path),
        attempt_paths=tuple(relative(path) for path in attempt_paths),
        validation_path=relative(validation_path),
    )


def _verify_bundle(bundle: Mapping[str, Any]) -> None:
    body = _clone(dict(bundle))
    expected = str(body.pop("bundle_manifest_hash", ""))
    if not expected or canonical_hash(body) != expected:
        raise SliceContractError("Step-4 bundle manifest hash mismatch")
    if not str(bundle.get("state_lineage_id") or ""):
        raise SliceContractError("Step-4 bundle lacks state lineage")


def _routing_map(bundle: Mapping[str, Any]) -> dict[str, str]:
    routing = _require_mapping(bundle.get("occurrence_routing"), "occurrence routing")
    result: dict[str, str] = {}
    for bucket in (
        "person_occurrences",
        "non_person_occurrences",
        "discourse_only",
        "deferred",
        "invalid_flagged",
    ):
        for row in _require_sequence(routing.get(bucket), f"routing bucket {bucket}"):
            item = _require_mapping(row, "routing row")
            occurrence_id = str(item.get("occurrence_id") or "")
            if not occurrence_id or occurrence_id in result:
                raise SliceContractError("routing is not a unique occurrence cover")
            result[occurrence_id] = bucket
    return result


def build_lightweight_roster(bundle: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Project all occurrences without ranking or dropping deferred rows."""

    _verify_bundle(bundle)
    routing = _routing_map(bundle)
    cards = _require_sequence(bundle.get("occurrence_cards"), "occurrence cards")
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in cards:
        card = _require_mapping(raw, "occurrence card")
        occurrence_id = str(card.get("occurrence_id") or "")
        anchor = _require_mapping(card.get("anchor"), "occurrence anchor")
        if not occurrence_id or occurrence_id in seen or occurrence_id not in routing:
            raise SliceContractError(f"occurrence card/routing mismatch: {occurrence_id!r}")
        seen.add(occurrence_id)
        rows.append(
            {
                "occurrence_id": occurrence_id,
                "occurrence_kind": str(card.get("occurrence_kind") or ""),
                "surface": str(card.get("surface") or ""),
                "referent_kind_claim": str(card.get("referent_kind_claim") or "unknown"),
                "chapter_id": str(card.get("chapter_id") or ""),
                "block_id": str(card.get("block_id") or anchor.get("block_id") or ""),
                "source_anchor": {
                    "block_id": str(anchor.get("block_id") or ""),
                    "char_start": int(anchor.get("char_start") or 0),
                    "char_end": int(anchor.get("char_end") or 0),
                },
                "routing_bucket": routing[occurrence_id],
            }
        )
    if seen != set(routing):
        raise SliceContractError("roster does not exact-cover occurrence routing")
    rows.sort(
        key=lambda row: (
            row["chapter_id"],
            row["source_anchor"]["block_id"],
            row["source_anchor"]["char_start"],
            row["source_anchor"]["char_end"],
            row["occurrence_id"],
        )
    )
    return rows


def _card_map(bundle: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for raw in _require_sequence(bundle.get("occurrence_cards"), "occurrence cards"):
        card = _clone(dict(_require_mapping(raw, "occurrence card")))
        occurrence_id = str(card.get("occurrence_id") or "")
        if not occurrence_id or occurrence_id in result:
            raise SliceContractError("occurrence card id is empty or duplicate")
        result[occurrence_id] = card
    return result


def _ground_map(bundle: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    ground = _require_mapping(bundle.get("ground_evidence"), "ground evidence")
    if "cast_claim_inputs" in ground:
        raise SliceContractError("retired cast_claim_inputs channel is forbidden")
    result: dict[str, dict[str, Any]] = {}
    for channel, raw_rows in ground.items():
        if channel == "counts":
            continue
        if not isinstance(raw_rows, Sequence) or isinstance(raw_rows, (str, bytes)):
            continue
        for raw in raw_rows:
            if not isinstance(raw, Mapping):
                continue
            row = _clone(dict(raw))
            identifier = str(row.get("ground_item_id") or "")
            if not identifier or identifier in result:
                raise SliceContractError("ground evidence id is empty or duplicate")
            row["channel"] = str(channel)
            result[identifier] = row
    return result


@dataclass(frozen=True, slots=True)
class UntrustedFrameProposal:
    unit_id: str
    trust: str
    response_status: str
    canonical_signature_hash: str
    boundary_relaxed_hash: str
    nodes: tuple[dict[str, Any], ...]
    deepest_spans: tuple[dict[str, Any], ...]
    deepest_by_block: dict[str, str | None]

    def payload(self) -> dict[str, Any]:
        return {
            "unit_id": self.unit_id,
            "trust": self.trust,
            "response_status": self.response_status,
            "canonical_signature_hash": self.canonical_signature_hash,
            "boundary_relaxed_hash": self.boundary_relaxed_hash,
            "nodes": _clone(list(self.nodes)),
            "deepest_spans": _clone(list(self.deepest_spans)),
            "deepest_by_block": _clone(self.deepest_by_block),
        }


def build_untrusted_frame_proposal(
    response: Mapping[str, Any], *, selection: Mapping[str, Any]
) -> UntrustedFrameProposal:
    validated: ValidatedFrameTree = validate_frame_response(response, selection=selection)
    return UntrustedFrameProposal(
        unit_id=str(selection.get("unit_id") or ""),
        trust="untrusted",
        response_status=validated.response_status,
        canonical_signature_hash=validated.canonical_signature_hash,
        boundary_relaxed_hash=validated.boundary_relaxed_hash,
        nodes=tuple(_clone(list(validated.nodes))),
        deepest_spans=tuple(_clone(list(validated.deepest_spans))),
        deepest_by_block=_clone(validated.deepest_by_block),
    )


def frame_raw_agreement(
    primary: UntrustedFrameProposal, checker: UntrustedFrameProposal
) -> dict[str, Any]:
    if primary.unit_id != checker.unit_id:
        raise SliceContractError("frame agreement inputs belong to different units")
    return {
        "unit_id": primary.unit_id,
        "qualification_status": "unqualified",
        "raw_unqualified_frame_exact_agreement": (
            primary.canonical_signature_hash == checker.canonical_signature_hash
        ),
        "raw_unqualified_frame_relaxed_agreement": (
            primary.boundary_relaxed_hash == checker.boundary_relaxed_hash
        ),
    }


def build_retrieval_request_payload(
    bundle: Mapping[str, Any],
    *,
    target_occurrence_ids: Sequence[str],
    frame: UntrustedFrameProposal | None,
) -> dict[str, Any]:
    roster = build_lightweight_roster(bundle)
    roster_ids = {row["occurrence_id"] for row in roster}
    targets = list(target_occurrence_ids)
    if not targets or len(set(targets)) != len(targets) or not set(targets) <= roster_ids:
        raise SliceContractError("retrieval targets are empty, duplicate, or foreign")
    cards = _card_map(bundle)
    ground = _ground_map(bundle)
    payload = {
        "contract_version": SLICE_CONTRACT_VERSION,
        "state_lineage_id": str(bundle.get("state_lineage_id") or ""),
        "bundle_manifest_hash": str(bundle.get("bundle_manifest_hash") or ""),
        "targets": [_clone(cards[target]) for target in targets],
        "global_light_roster": roster,
        "target_cards": [_clone(cards[target]) for target in targets],
        "untrusted_frame": {"trust": "unknown"} if frame is None else frame.payload(),
        "evidence_items": [_clone(ground[key]) for key in sorted(ground)],
    }
    payload["target_ids"] = targets
    payload["roster_universe_hash"] = canonical_hash(roster)
    payload["evidence_ref_universe"] = sorted(ground)
    payload["evidence_ref_universe_hash"] = canonical_hash(sorted(ground))
    return payload


def validate_retrieval_response(
    response: Mapping[str, Any], *, request_payload: Mapping[str, Any]
) -> dict[str, Any]:
    _exact_keys(dict(response), {"targets"}, "retrieval response")
    target_ids = list(request_payload.get("target_ids") or [])
    roster_ids = {
        str(row.get("occurrence_id") or "")
        for row in request_payload.get("global_light_roster") or []
        if isinstance(row, Mapping)
    }
    evidence_ids = set(request_payload.get("evidence_ref_universe") or [])
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in _require_sequence(response.get("targets"), "retrieval targets"):
        row = _require_mapping(raw, "retrieval target row")
        _exact_keys(
            row,
            {
                "target_occurrence_id",
                "candidate_occurrence_ids",
                "status",
                "evidence_refs",
            },
            "retrieval target row",
        )
        target = str(row.get("target_occurrence_id") or "")
        status = str(row.get("status") or "")
        candidates = list(
            _require_sequence(row.get("candidate_occurrence_ids"), "candidate ids")
        )
        evidence = list(_require_sequence(row.get("evidence_refs"), "evidence refs"))
        if target not in target_ids or target in seen:
            raise SliceContractError("retrieval target is foreign or duplicate")
        if status not in {"selected", "unknown"}:
            raise SliceContractError("retrieval status is foreign")
        if len(set(candidates)) != len(candidates) or not set(candidates) <= roster_ids:
            raise SliceContractError("retrieval candidates are duplicate or foreign")
        if target in candidates:
            raise SliceContractError("retrieval candidate list must exclude its target")
        if len(set(evidence)) != len(evidence) or not set(evidence) <= evidence_ids:
            raise SliceContractError("retrieval evidence is duplicate or foreign")
        if status == "unknown" and (candidates or evidence):
            raise SliceContractError("unknown retrieval row must be empty")
        seen.add(target)
        rows.append(
            {
                "target_occurrence_id": target,
                "candidate_occurrence_ids": candidates,
                "status": status,
                "evidence_refs": evidence,
            }
        )
    if seen != set(target_ids):
        raise SliceContractError("retrieval response does not exact-cover targets")
    rows.sort(key=lambda row: target_ids.index(row["target_occurrence_id"]))
    return {"targets": rows}


def build_proposal_request_payload(
    bundle: Mapping[str, Any],
    *,
    retrieval: Mapping[str, Any],
    frame: UntrustedFrameProposal | None,
) -> dict[str, Any]:
    cards = _card_map(bundle)
    ground = _ground_map(bundle)
    target_rows: list[dict[str, Any]] = []
    for raw in _require_sequence(retrieval.get("targets"), "validated retrieval rows"):
        row = _require_mapping(raw, "validated retrieval row")
        if row.get("status") != "selected":
            continue
        target = str(row.get("target_occurrence_id") or "")
        selection_ids = [target, *[str(value) for value in row.get("candidate_occurrence_ids") or []]]
        if len(set(selection_ids)) != len(selection_ids) or not set(selection_ids) <= set(cards):
            raise SliceContractError("proposal selection universe is invalid")
        target_rows.append(
            {
                "target_occurrence_id": target,
                "target_card": _clone(cards[target]),
                "candidate_cards": [_clone(cards[value]) for value in selection_ids[1:]],
                "selection_occurrence_ids": selection_ids,
                "selection_universe_hash": canonical_hash(selection_ids),
            }
        )
    payload = {
        "contract_version": SLICE_CONTRACT_VERSION,
        "state_lineage_id": str(bundle.get("state_lineage_id") or ""),
        "bundle_manifest_hash": str(bundle.get("bundle_manifest_hash") or ""),
        "targets": target_rows,
        "untrusted_frame": {"trust": "unknown"} if frame is None else frame.payload(),
        "evidence_items": [_clone(ground[key]) for key in sorted(ground)],
        "evidence_ref_universe": sorted(ground),
    }
    payload["target_ids"] = [row["target_occurrence_id"] for row in target_rows]
    payload["evidence_ref_universe_hash"] = canonical_hash(payload["evidence_ref_universe"])
    return payload


def _reject_semantic_ids(value: Any) -> None:
    if isinstance(value, Mapping):
        bad = set(value) & FORBIDDEN_MODEL_KEYS
        if bad:
            raise SliceContractError(f"model response minted semantic ids: {sorted(bad)}")
        for nested in value.values():
            _reject_semantic_ids(nested)
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        for nested in value:
            _reject_semantic_ids(nested)


def validate_proposal_response(
    response: Mapping[str, Any], *, request_payload: Mapping[str, Any]
) -> dict[str, Any]:
    _reject_semantic_ids(response)
    _exact_keys(dict(response), {"proposals"}, "proposal response")
    targets = {
        str(row.get("target_occurrence_id") or ""): row
        for row in request_payload.get("targets") or []
        if isinstance(row, Mapping)
    }
    evidence_ids = set(request_payload.get("evidence_ref_universe") or [])
    seen: set[str] = set()
    rows: list[dict[str, Any]] = []
    for raw in _require_sequence(response.get("proposals"), "proposal rows"):
        row = _require_mapping(raw, "proposal row")
        _exact_keys(
            row,
            {
                "target_occurrence_id",
                "status",
                "same_referent_occurrence_ids",
                "different_referent_occurrence_ids",
                "referent_kind",
                "canonical_surface_guess",
                "evidence_refs",
            },
            "proposal row",
        )
        target = str(row.get("target_occurrence_id") or "")
        status = str(row.get("status") or "")
        same = list(_require_sequence(row.get("same_referent_occurrence_ids"), "same ids"))
        different = list(
            _require_sequence(row.get("different_referent_occurrence_ids"), "different ids")
        )
        evidence = list(_require_sequence(row.get("evidence_refs"), "proposal evidence"))
        kind = str(row.get("referent_kind") or "")
        if target not in targets or target in seen:
            raise SliceContractError("proposal target is foreign or duplicate")
        universe = set(targets[target].get("selection_occurrence_ids") or [])
        if status not in {"proposed", "unknown"} or kind not in REFERENT_KINDS:
            raise SliceContractError("proposal status or referent kind is foreign")
        if len(set(same)) != len(same) or len(set(different)) != len(different):
            raise SliceContractError("proposal occurrence set contains duplicates")
        if not set(same) <= universe or not set(different) <= universe or set(same) & set(different):
            raise SliceContractError("proposal occurrence sets are foreign or overlap")
        if len(set(evidence)) != len(evidence) or not set(evidence) <= evidence_ids:
            raise SliceContractError("proposal evidence is duplicate or foreign")
        if status == "proposed" and same.count(target) != 1:
            raise SliceContractError("proposed same-referent set must contain target once")
        if status == "unknown" and (same or different or evidence):
            raise SliceContractError("unknown proposal must have empty sets and evidence")
        seen.add(target)
        rows.append(_clone(dict(row)))
    if seen != set(targets):
        raise SliceContractError("proposal response does not exact-cover targets")
    order = list(request_payload.get("target_ids") or [])
    rows.sort(key=lambda row: order.index(row["target_occurrence_id"]))
    return {"proposals": rows}


def run_recorded_identity_batch(
    bundle: Mapping[str, Any],
    *,
    target_occurrence_ids: Sequence[str],
    frame: UntrustedFrameProposal | None,
    retrieval_prompt: str,
    proposal_prompt: str,
    provider: str,
    model_config: Mapping[str, Any],
    request_llm: Callable[[list[dict[str, Any]], Mapping[str, Any], bool], Mapping[str, Any]],
    out_dir: Path,
    reports_root: Path,
) -> dict[str, Any]:
    """Run one recorded retrieval/proposal batch without publishing identity state."""

    lineage = {
        "state_lineage_id": str(bundle.get("state_lineage_id") or ""),
        "bundle_manifest_hash": str(bundle.get("bundle_manifest_hash") or ""),
    }
    retrieval_payload = build_retrieval_request_payload(
        bundle,
        target_occurrence_ids=target_occurrence_ids,
        frame=frame,
    )
    retrieval_request = render_identity_slice_request(
        role="retrieval",
        payload=retrieval_payload,
        prompt_text=retrieval_prompt,
        provider=provider,
        model_config=model_config,
        upstream_lineage_hashes=lineage,
    )
    retrieval_result = execute_identity_slice_request(
        retrieval_request,
        request_llm=request_llm,
        out_dir=out_dir,
        reports_root=reports_root,
    )
    proposal_payload = build_proposal_request_payload(
        bundle,
        retrieval=retrieval_result.normalized_response,
        frame=frame,
    )
    proposal_result: IdentitySliceCallResult | None = None
    if proposal_payload["target_ids"]:
        proposal_request = render_identity_slice_request(
            role="proposal",
            payload=proposal_payload,
            prompt_text=proposal_prompt,
            provider=provider,
            model_config=model_config,
            upstream_lineage_hashes={
                **lineage,
                "retrieval_request_fingerprint": retrieval_result.request_fingerprint,
                "retrieval_response_hash": canonical_hash(
                    retrieval_result.normalized_response
                ),
            },
        )
        proposal_result = execute_identity_slice_request(
            proposal_request,
            request_llm=request_llm,
            out_dir=out_dir,
            reports_root=reports_root,
        )
    proposal_by_target = {
        str(row["target_occurrence_id"]): row
        for row in (
            proposal_result.normalized_response.get("proposals", [])
            if proposal_result is not None
            else []
        )
    }
    retrieval_by_target = {
        str(row["target_occurrence_id"]): row
        for row in retrieval_result.normalized_response["targets"]
    }
    target_results: list[dict[str, Any]] = []
    for target in target_occurrence_ids:
        retrieval_row = retrieval_by_target[target]
        if retrieval_row["status"] == "unknown":
            target_results.append(
                {"target_occurrence_id": target, "slice_status": "unknown"}
            )
        else:
            proposal_row = proposal_by_target.get(target)
            if proposal_row is None:
                raise SliceContractError("selected retrieval target lacks proposal result")
            target_results.append(
                {
                    "target_occurrence_id": target,
                    "slice_status": str(proposal_row["status"]),
                    "proposal": _clone(proposal_row),
                }
            )
    artifact = {
        "schema_version": "literary_s5c_recorded_batch_v1",
        "proposal_only": True,
        "target_results": target_results,
        "retrieval_request_fingerprint": retrieval_result.request_fingerprint,
        "proposal_request_fingerprint": (
            proposal_result.request_fingerprint if proposal_result is not None else None
        ),
        "usage": {
            "prompt_tokens": int(retrieval_result.usage["prompt_tokens"])
            + int(proposal_result.usage["prompt_tokens"] if proposal_result else 0),
            "completion_tokens": int(retrieval_result.usage["completion_tokens"])
            + int(proposal_result.usage["completion_tokens"] if proposal_result else 0),
        },
    }
    artifact["artifact_hash"] = canonical_hash(artifact)
    resolved_root = assert_slice_output_root(Path(out_dir), reports_root=Path(reports_root))
    write_json_exclusive(
        resolved_root / "identity_batches" / f"{artifact['artifact_hash']}.json",
        artifact,
    )
    return _clone(artifact)


def split_target_batches(
    target_ids: Sequence[str],
    *,
    render_payload: Callable[[Sequence[str]], Mapping[str, Any]],
    prompt_token_cap: int,
    estimate_tokens: Callable[[Mapping[str, Any]], int] | None = None,
) -> list[list[str]]:
    """Split only target rows; the renderer must keep all shared context intact."""

    if prompt_token_cap <= 0 or not target_ids or len(set(target_ids)) != len(target_ids):
        raise SliceContractError("batch targets/cap are invalid")
    estimator = estimate_tokens or (
        lambda payload: max(1, (len(canonical_json(payload).encode("utf-8")) + 3) // 4)
    )
    result: list[list[str]] = []
    current: list[str] = []
    for target in target_ids:
        trial = [*current, target]
        if estimator(render_payload(trial)) <= prompt_token_cap:
            current = trial
            continue
        if not current:
            raise SliceContractError(f"single target exceeds prompt cap: {target}")
        result.append(current)
        current = [target]
        if estimator(render_payload(current)) > prompt_token_cap:
            raise SliceContractError(f"single target exceeds prompt cap: {target}")
    if current:
        result.append(current)
    flattened = [target for batch in result for target in batch]
    if flattened != list(target_ids):
        raise SliceContractError("target batching lost or reordered targets")
    return result


def _stratum(row: Mapping[str, Any]) -> str:
    explicit = str(row.get("stratum") or "")
    if explicit in {"proper_name", "descriptor", "endpoint"}:
        return explicit
    if str(row.get("occurrence_kind") or "") == "endpoint":
        return "endpoint"
    mention_type = str(row.get("mention_type") or "")
    return "proper_name" if mention_type == "name" else "descriptor"


def build_public_heldout_manifest(
    source_universe: Sequence[Mapping[str, Any]],
    *,
    seed: str,
    canary_occurrence_ids: Iterable[str] = (),
    per_chapter: int = 6,
) -> dict[str, Any]:
    if per_chapter != 6:
        raise SliceContractError("S5C held-out contract fixes six rows per chapter")
    canaries = set(canary_occurrence_ids)
    eligible: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in source_universe:
        row = _clone(dict(raw))
        occurrence_id = str(row.get("occurrence_id") or "")
        if not occurrence_id or occurrence_id in seen:
            raise SliceContractError("source occurrence universe has empty/duplicate ids")
        seen.add(occurrence_id)
        if occurrence_id in canaries:
            continue
        row["stratum"] = _stratum(row)
        row["selection_rank"] = sha256(
            f"{seed}\x00{occurrence_id}".encode("utf-8")
        ).hexdigest()
        eligible.append(row)
    selected: list[dict[str, Any]] = []
    for chapter in sorted({str(row.get("chapter_id") or "") for row in eligible}):
        rows = [row for row in eligible if str(row.get("chapter_id") or "") == chapter]
        chapter_selected: list[dict[str, Any]] = []
        for stratum in ("proper_name", "descriptor", "endpoint"):
            choices = sorted(
                [row for row in rows if row["stratum"] == stratum],
                key=lambda row: (row["selection_rank"], row["occurrence_id"]),
            )
            chapter_selected.extend(choices[:2])
        if len(chapter_selected) < per_chapter:
            used = {row["occurrence_id"] for row in chapter_selected}
            fallback = sorted(
                [row for row in rows if row["occurrence_id"] not in used],
                key=lambda row: (row["selection_rank"], row["occurrence_id"]),
            )
            chapter_selected.extend(fallback[: per_chapter - len(chapter_selected)])
        if len(chapter_selected) != per_chapter:
            raise SliceContractError(f"held-out chapter lacks six occurrences: {chapter}")
        selected.extend(chapter_selected)
    public_rows = [
        {
            "occurrence_id": str(row["occurrence_id"]),
            "chapter_id": str(row.get("chapter_id") or ""),
            "block_id": str(row.get("block_id") or ""),
            "source_anchor": _clone(row.get("source_anchor") or row.get("anchor") or {}),
            "stratum": str(row["stratum"]),
        }
        for row in selected
    ]
    selection_projection = [
        {
            "occurrence_id": str(row.get("occurrence_id") or ""),
            "chapter_id": str(row.get("chapter_id") or ""),
            "block_id": str(row.get("block_id") or ""),
            "source_anchor": _clone(row.get("source_anchor") or row.get("anchor") or {}),
            "stratum": _stratum(row),
        }
        for row in source_universe
    ]
    payload = {
        "schema_version": "literary_s5c_heldout_public_v1",
        "seed": seed,
        "selection_universe_hash": canonical_hash(selection_projection),
        "canary_exclusion_hash": canonical_hash(sorted(canaries)),
        "targets": public_rows,
        "label_commitments": {"claude": None, "codex": None},
        "adjudication_status": "labels_not_opened",
    }
    payload["manifest_hash"] = canonical_hash(payload)
    return payload


def private_label_commitment(labels: Mapping[str, Any], *, reviewer: str) -> str:
    if reviewer not in {"claude", "codex"} or not labels:
        raise SliceContractError("private labels/reviewer are invalid")
    return canonical_hash({"reviewer": reviewer, "labels": labels})


def score_extraction_presence(
    public_manifest: Mapping[str, Any], extracted_occurrence_ids: Iterable[str]
) -> list[dict[str, Any]]:
    extracted = set(extracted_occurrence_ids)
    return [
        {
            "occurrence_id": str(row.get("occurrence_id") or ""),
            "outcome": (
                "present"
                if str(row.get("occurrence_id") or "") in extracted
                else "upstream_occurrence_missing"
            ),
        }
        for row in public_manifest.get("targets") or []
        if isinstance(row, Mapping)
    ]


def classify_attribution(
    *,
    outcome: str,
    evidence_delivered: bool,
    known_noise_absent: bool,
    retrieval_delivered: bool,
    prompt_gate_passed: bool,
    validator_preserved_response: bool,
    source_ambiguous: bool = False,
    frame_load_bearing: bool = False,
    frame_reference_agreed: bool = False,
    delivered_frame_wrong: bool = False,
    no_frame_pair_flipped_correct: bool = False,
) -> str:
    if outcome not in OUTCOMES:
        raise SliceContractError(f"foreign identity outcome: {outcome}")
    if source_ambiguous or outcome == "ground_truth_ambiguous":
        return "source_ambiguity"
    if outcome == "upstream_occurrence_missing":
        return "context_missing"
    if not evidence_delivered:
        return "context_missing"
    if not retrieval_delivered:
        return "retrieval_miss"
    if not prompt_gate_passed:
        return "prompt_defect"
    if not validator_preserved_response:
        return "validator_drop"
    if (
        outcome == "wrong"
        and frame_load_bearing
        and frame_reference_agreed
        and delivered_frame_wrong
        and no_frame_pair_flipped_correct
    ):
        return "upstream_frame_error"
    if outcome == "wrong" and known_noise_absent:
        return "model_error"
    return "indeterminate"


@dataclass(frozen=True, slots=True)
class QuotaBucket:
    quota_bucket_id: str
    utc_date: str
    rpm_limit: int
    rpd_limit: int
    token_safety_cap: int
    used_prompt_tokens: int = 0
    used_completion_tokens: int = 0
    used_requests: int = 0

    def __post_init__(self) -> None:
        try:
            date.fromisoformat(self.utc_date)
        except ValueError as exc:
            raise SliceContractError("quota bucket date must be UTC YYYY-MM-DD") from exc
        if min(self.rpm_limit, self.rpd_limit, self.token_safety_cap) <= 0:
            raise SliceContractError("quota limits must be positive")

    @property
    def used_tokens(self) -> int:
        return self.used_prompt_tokens + self.used_completion_tokens


def gate_budget(
    bucket: QuotaBucket,
    *,
    next_prompt_tokens: int,
    next_completion_reserve: int,
    remaining_token_reserve: int,
    remaining_call_reserve: int,
) -> dict[str, Any]:
    values = (
        next_prompt_tokens,
        next_completion_reserve,
        remaining_token_reserve,
        remaining_call_reserve,
    )
    if any(value < 0 for value in values):
        raise SliceContractError("budget values cannot be negative")
    required_tokens = (
        bucket.used_tokens
        + next_prompt_tokens
        + next_completion_reserve
        + remaining_token_reserve
    )
    required_calls = bucket.used_requests + 1 + remaining_call_reserve
    if required_tokens > bucket.token_safety_cap:
        raise SliceContractError("quota token gate would be exceeded before call")
    if required_calls > bucket.rpd_limit:
        raise SliceContractError("quota RPD gate would be exceeded before call")
    return {
        "quota_bucket_id": bucket.quota_bucket_id,
        "utc_date": bucket.utc_date,
        "used_tokens": bucket.used_tokens,
        "required_tokens_with_reserve": required_tokens,
        "token_safety_cap": bucket.token_safety_cap,
        "used_requests": bucket.used_requests,
        "required_requests_with_reserve": required_calls,
        "rpd_limit": bucket.rpd_limit,
        "rpm_limit": bucket.rpm_limit,
        "accounting_formula": "prompt_tokens+completion_tokens",
        "status": "approved",
    }


def assert_slice_output_root(path: Path, *, reports_root: Path) -> Path:
    root = Path(reports_root).resolve()
    resolved = Path(path).resolve()
    allowed = root / SLICE_REPORT_PREFIX
    if resolved != allowed and allowed not in resolved.parents:
        raise SliceContractError(f"write escapes S5C slice root: {resolved}")
    return resolved


def context_only_pair_diff(
    left: Mapping[str, Any],
    right: Mapping[str, Any],
    *,
    allowed_context_sections: set[str],
) -> dict[str, Any]:
    fixed = {"prompt_sha256", "model_config_hash", "target_ids", "output_schema_hash"}
    for key in fixed:
        if left.get(key) != right.get(key):
            raise SliceContractError(f"paired causal request differs outside context: {key}")
    left_context = _require_mapping(left.get("context_sections"), "left context sections")
    right_context = _require_mapping(right.get("context_sections"), "right context sections")
    changed = sorted(
        key
        for key in set(left_context) | set(right_context)
        if left_context.get(key) != right_context.get(key)
    )
    if not set(changed) <= allowed_context_sections:
        raise SliceContractError("paired causal request changed an unregistered section")
    return {
        "comparison_kind": "context_policy",
        "changed_context_sections": changed,
        "canonical_diff_hash": canonical_hash(
            {
                key: {"left": left_context.get(key), "right": right_context.get(key)}
                for key in changed
            }
        ),
    }


__all__ = [
    "IDENTITY_PROPOSAL_PROMPT_ID",
    "IDENTITY_RETRIEVAL_PROMPT_ID",
    "IdentitySliceCallResult",
    "IdentitySliceRequest",
    "OUTCOMES",
    "QuotaBucket",
    "ROOT_CAUSES",
    "SLICE_CONTRACT_VERSION",
    "SliceContractError",
    "UPSTREAM_PROMPT_IDS",
    "UntrustedFrameProposal",
    "assert_book_neutral",
    "assert_slice_output_root",
    "build_lightweight_roster",
    "build_proposal_request_payload",
    "build_public_heldout_manifest",
    "build_retrieval_request_payload",
    "build_untrusted_frame_proposal",
    "classify_attribution",
    "context_only_pair_diff",
    "execute_identity_slice_request",
    "frame_raw_agreement",
    "gate_budget",
    "load_slice_prompt",
    "private_label_commitment",
    "prompt_manifest",
    "score_extraction_presence",
    "render_identity_slice_request",
    "run_recorded_identity_batch",
    "split_target_batches",
    "validate_proposal_response",
    "validate_retrieval_response",
]
