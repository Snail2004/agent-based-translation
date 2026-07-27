"""Deterministic request-local references for Literary model contracts.

Persistent ids are useful to code and audit artifacts but are poor model output
targets: they are long, opaque, and easy to copy incorrectly.  This module
creates short references for one request only.  It deliberately does not make
any semantic decision; it only projects known ids into a request-local alphabet
and resolves the model's labels back to the ids supplied by code.
"""

from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import re
from typing import Any, Mapping, Sequence

from pipeline.literary.checkpoint import canonical_hash, canonical_json


MODEL_REF_MAP_SCHEMA_VERSION = "literary_model_ref_map_v8"
MODEL_REF_MODE_CLASSIFIED_V1 = "classified_request_local_v1"
CODE_OWNED_ECHO_SCHEMA_VERSION = "literary_code_owned_echoes_v1"
_LOCAL_REF_RE = re.compile(r"^[A-Z][1-9][0-9]*$")
_OPAQUE_PERSISTENT_REF_RE = re.compile(
    r"^(?:(?:scan|additional|glossary):)?"
    r"(?:b0ent_|b1obs_|b1term_|b1lac_|b1xhear_|"
    r"b2(?:frm|evt|turn|review|slimgap)|"
    r"b3(?:state|comp|batch|pending)|litref1_)"
    r"[A-Za-z0-9_.:-]+$"
)
_PREFIX_BY_NAMESPACE = {
    "entity": "E",
    "scan_observation": "O",
    "glossary_observation": "G",
    "component": "C",
    "frame": "F",
    "event": "V",
    "turn": "T",
    "state": "S",
    "case": "Q",
    "review": "R",
    "batch": "B",
}
_NAMESPACE_ALIASES = {
    "candidate": "entity",
    "observation": "scan_observation",
    "scan": "scan_observation",
    "term_observation": "glossary_observation",
    "glossary": "glossary_observation",
    "pending": "case",
}

# Some Literary fields carry a qualified reference (for example,
# ``scan:b1obs_x``) while the corresponding typed field carries the bare
# value (``b1obs_x``).  The qualifier is part of the value's identity, not a
# reason to mint a second model label.  Value-qualified namespaces therefore
# win over the fallback namespace implied by the field name.
_VALUE_QUALIFIER_BY_PREFIX = {
    "scan": "scan_observation",
    "glossary": "glossary_observation",
    "additional": "entity",
}
_QUALIFIER_BY_NAMESPACE = {
    "scan_observation": "scan",
    "glossary_observation": "glossary",
}
_BARE_VALUE_FIELDS = {
    "scan_observation_id",
    "scan_observation_ids",
    "observation_id",
    "observation_ids",
    "term_observation_id",
    "term_observation_ids",
}
_POLYMORPHIC_REFERENCE_FIELDS = {
    "task_ref",
    "task_refs",
    "subject_ref",
    "subject_refs",
    "target_ref",
    "target_refs",
    "counterpart_ref",
    "counterpart_refs",
    "revised_target_ref",
    "referent_ref",
    "referent_refs",
    "narrator_referent_ref",
    "narrator_referent_refs",
    "subject_referent_refs",
    "counterpart_referent_refs",
    "allowed_target_refs",
    "source_ref",
    "source_refs",
}

# These are deliberately field names for unqualified values.  Qualified
# values are canonicalized by their own prefix before this fallback applies.
# Source addresses (chapter_id/block_id), hashes, fingerprints and schema IDs
# are absent so they remain code-owned and never become model copy targets.
MODEL_REF_FIELDS_V1: Mapping[str, tuple[str, ...]] = {
    "entity": (
        "entity_id",
        "entity_ref",
        "entity_refs",
        "task_ref",
        "subject_ref",
        "ref",
        "prior_card_id",
        "prior_card_ids",
        "candidate_prior_card_ids",
        "candidate_card_id",
        "candidate_card_ids",
        "competing_card_ids",
        "narrowed_candidate_card_ids",
        "excluded_prior_card_ids",
        "merge_target_prior_card_id",
        "supports_excluded_prior_card_ids",
        "relevant_candidate_card_id",
        "relevant_candidate_card_ids",
        "candidate_ref",
        "target_candidate_ref",
        "target_prior_card_id",
        "target_candidate_id",
        "target_ref",
        "counterpart_ref",
        "revised_target_ref",
        "continued_prior_card_id",
        "withheld_prior_card_ids",
        "referent_ref",
        "referent_refs",
        "narrator_referent_ref",
        "narrator_referent_refs",
        "subject_referent_refs",
        "counterpart_referent_refs",
        "co_parked_referent_refs",
        "candidate_entity_id",
        "candidate_entity_ids",
        "current_entity_id",
        "current_entity_ids",
        "candidate_id",
        "candidate_ids",
        "claimant_candidate_id",
        "claimant_candidate_ids",
        "actor_candidate_card_id",
        "addressee_candidate_card_id",
        "target_candidate_card_id",
        "speaker_entity_id",
        "addressee_entity_id",
        "actor_entity_id",
        "target_entity_id",
        "source_entity_id",
        "allowed_target_refs",
        "merge_target_entity_id",
        "alias_entity_id",
        "canonical_entity_id",
        "source_ref",
        "source_refs",
    ),
    "scan_observation": (
        "scan_observation_id",
        "scan_observation_ids",
        "observation_id",
        "observation_ids",
        # Chapter-local identity merges retain the source observations that
        # were grouped and the observation chosen as representative.  These
        # values are always qualified ``scan:...`` references.
        "member_source_refs",
        "representative_source_ref",
    ),
    "glossary_observation": (
        "term_observation_id",
        "term_observation_ids",
    ),
    "component": (
        "component_id",
        "component_ids",
        "source_component_id",
        "source_component_ids",
        "hearing_component_id",
    ),
    "frame": (
        "frame_id",
        "frame_ids",
        "frame_segment_id",
        "frame_segment_ids",
        "frame_refs",
        "source_frame_segment_id",
        "source_frame_segment_ids",
        "source_window_id",
    ),
    "event": (
        "event_id",
        "event_ids",
        "event_ref",
        "event_refs",
        "salient_event_id",
        "salient_event_ids",
        "salient_event_refs",
        "source_event_ids",
        "source_event_id",
        "interaction_event_id",
        "interaction_event_ids",
        "input_event_id",
        "original_event_id",
        "supersedes_event_id",
        "trigger_event_id",
    ),
    "turn": (
        "turn_id",
        "turn_ids",
        "turn_ref",
        "turn_refs",
        "speaker_turn_id",
        "speaker_turn_ids",
        "source_turn_ids",
        "source_row_id",
        "source_row_ids",
    ),
    "state": (
        "state_id",
        "state_ids",
        "state_ref",
        "state_refs",
        "effective_state_refs",
    ),
    "case": (
        "pending_case_id",
        "pending_case_ids",
        "case_id",
        "case_ids",
        "continuity_case_id",
        "continuity_case_ids",
        "unresolved_case_refs",
    ),
    "review": ("review_id", "review_ids", "ticket_id", "ticket_ids", "review_refs"),
    "batch": ("batch_id", "batch_ids"),
}

# Id-shaped response fields that intentionally stay outside model-reference
# transport. Each exemption records why no live model-label round trip exists.
TRANSPORT_EXEMPT_FIELDS_V1: Mapping[str, str] = {
    "glossary_card_id": (
        "Only the retired prior-challenge experiment emits this field; no live "
        "Literary role sends that response schema through model-ref transport."
    ),
}


class ModelRefError(ValueError):
    """Raised when a request-local reference contract is malformed."""


def build_model_ref_map(
    namespaces: Mapping[str, Sequence[str]],
) -> dict[str, Any]:
    """Build a stable map from persistent ids to short request-local labels.

    Ordering is part of the contract.  A model sees classified labels such as
    ``E1`` and ``V2`` only;
    the persistent ids are retained in this sealed artifact for code-side
    resolution and replay.
    """

    entries: list[dict[str, str]] = []
    seen_persistent: set[tuple[str, str]] = set()
    normalized_inputs: dict[str, list[str]] = {}
    seen_input_namespaces: set[str] = set()
    for raw_namespace, raw_values in namespaces.items():
        namespace = _canonical_namespace(raw_namespace)
        if namespace in seen_input_namespaces:
            raise ModelRefError(f"duplicate namespace alias: {namespace}")
        seen_input_namespaces.add(namespace)
        if isinstance(raw_values, (str, bytes)):
            raise ModelRefError(f"{namespace} ids must be a sequence")
        values = list(raw_values)
        raw_seen: set[str] = set()
        for raw_value in values:
            persistent_id = _required_id(raw_value, namespace)
            if persistent_id in raw_seen:
                raise ModelRefError(f"{namespace} ids contain duplicates")
            raw_seen.add(persistent_id)
            target_namespace, canonical_id = _canonical_reference(
                persistent_id, namespace
            )
            normalized_inputs.setdefault(target_namespace, []).append(canonical_id)
    for namespace in sorted(normalized_inputs):
        prefix = _prefix(namespace)
        values = normalized_inputs[namespace]
        # A bare value and its qualified spelling intentionally collapse to one
        # entry; exact duplicate raw values were rejected above.
        normalized = sorted(set(values))
        for ordinal, persistent_id in enumerate(normalized, 1):
            key = (namespace, persistent_id)
            if key in seen_persistent:
                raise ModelRefError(f"duplicate map entry: {namespace}:{persistent_id}")
            seen_persistent.add(key)
            entries.append(
                {
                    "namespace": namespace,
                    "local_ref": f"{prefix}{ordinal}",
                    "persistent_id": persistent_id,
                }
            )
    body = {
        "schema_version": MODEL_REF_MAP_SCHEMA_VERSION,
        "entries": entries,
    }
    return {**body, "map_hash": canonical_hash(body)}


def validate_model_ref_map(value: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ModelRefError("model reference map must be an object")
    body = deepcopy(dict(value))
    observed = body.pop("map_hash", None)
    if body.get("schema_version") != MODEL_REF_MAP_SCHEMA_VERSION:
        raise ModelRefError("foreign model reference map schema")
    if not isinstance(observed, str) or observed != canonical_hash(body):
        raise ModelRefError("model reference map hash mismatch")
    entries = body.get("entries")
    if not isinstance(entries, list):
        raise ModelRefError("model reference map entries must be a list")
    expected_order = sorted(
        entries,
        key=lambda row: (
            str(row.get("namespace")),
            str(row.get("persistent_id")),
        ),
    )
    if entries != expected_order:
        raise ModelRefError("model reference map ordering is not deterministic")
    seen_local: set[tuple[str, str]] = set()
    seen_persistent: set[tuple[str, str]] = set()
    seen_canonical: set[tuple[str, str]] = set()
    for row in entries:
        if not isinstance(row, Mapping) or set(row) != {
            "namespace",
            "local_ref",
            "persistent_id",
        }:
            raise ModelRefError("model reference map entry shape differs")
        namespace = _required_id(row.get("namespace"), "namespace")
        local_ref = _required_id(row.get("local_ref"), "local_ref")
        persistent_id = _required_id(row.get("persistent_id"), "persistent_id")
        canonical_namespace, canonical_id = _canonical_reference(
            persistent_id, namespace
        )
        if canonical_namespace != namespace or canonical_id != persistent_id:
            raise ModelRefError("model reference map entry is not canonical")
        canonical_key = (canonical_namespace, canonical_id)
        if canonical_key in seen_canonical:
            raise ModelRefError("one persistent referent has multiple local labels")
        seen_canonical.add(canonical_key)
        if not _LOCAL_REF_RE.fullmatch(local_ref):
            raise ModelRefError(f"invalid local reference: {local_ref}")
        if not local_ref.startswith(_prefix(namespace)):
            raise ModelRefError(f"local reference prefix differs for {namespace}")
        if (namespace, local_ref) in seen_local:
            raise ModelRefError(f"duplicate local reference: {namespace}:{local_ref}")
        if (namespace, persistent_id) in seen_persistent:
            raise ModelRefError(
                f"duplicate persistent reference: {namespace}:{persistent_id}"
            )
        seen_local.add((namespace, local_ref))
        seen_persistent.add((namespace, persistent_id))
    expected_entries: list[dict[str, str]] = []
    persistent_by_namespace: dict[str, list[str]] = {}
    for row in entries:
        persistent_by_namespace.setdefault(str(row["namespace"]), []).append(
            str(row["persistent_id"])
        )
    for namespace in sorted(persistent_by_namespace):
        prefix = _prefix(namespace)
        for ordinal, persistent_id in enumerate(
            sorted(persistent_by_namespace[namespace]), 1
        ):
            expected_entries.append(
                {
                    "namespace": namespace,
                    "local_ref": f"{prefix}{ordinal}",
                    "persistent_id": persistent_id,
                }
            )
    if entries != expected_entries:
        raise ModelRefError("model reference labels are not deterministic")
    return {**body, "map_hash": observed}


def project_id_fields(
    value: Any,
    *,
    ref_map: Mapping[str, Any],
    field_names_by_namespace: Mapping[str, Sequence[str]],
) -> Any:
    """Replace only explicitly declared id fields with local refs."""

    validated = validate_model_ref_map(ref_map)
    lookup = _persistent_to_local(validated)
    field_namespace = _field_namespace(field_names_by_namespace)
    return _rewrite_id_fields(value, field_namespace, lookup, direction="to_local")


def resolve_id_fields(
    value: Any,
    *,
    ref_map: Mapping[str, Any],
    field_names_by_namespace: Mapping[str, Sequence[str]],
) -> Any:
    """Resolve only declared model id fields; reject foreign or long ids."""

    validated = validate_model_ref_map(ref_map)
    lookup = _local_to_persistent(validated)
    field_namespace = _field_namespace(field_names_by_namespace)
    return _rewrite_id_fields(value, field_namespace, lookup, direction="to_persistent")


def project_schema_id_fields(
    schema: Mapping[str, Any],
    *,
    ref_map: Mapping[str, Any],
    field_names_by_namespace: Mapping[str, Sequence[str]],
) -> dict[str, Any]:
    """Project only ID values already constrained by a response schema.

    Request-specific persistent-ID ``enum``/``const`` constraints are removed
    from the wire schema instead of being replaced by a dynamic ``E1..En``
    enum.  Exact membership is enforced by the sealed map during response
    resolution, before the unchanged canonical semantic validator runs.  This
    keeps the transport schema independent of request cardinality.
    """

    validate_model_ref_map(ref_map)
    field_namespace = _field_namespace(field_names_by_namespace)

    def visit(node: Any, key: str | None = None) -> Any:
        if isinstance(node, list):
            return [visit(item, key) for item in node]
        if not isinstance(node, Mapping):
            return deepcopy(node)
        result = {str(name): visit(child, str(name)) for name, child in node.items()}
        namespace = field_namespace.get(key or "")
        if namespace is not None:
            if "items" in result and isinstance(result["items"], Mapping):
                item = dict(result["items"])
                item.pop("enum", None)
                item.pop("const", None)
                result["items"] = item
            result.pop("enum", None)
            result.pop("const", None)
        return result

    return visit(schema)


def project_model_response_schema_v1(
    response_schema: Mapping[str, Any],
    *,
    field_names_by_namespace: Mapping[str, Sequence[str]] = MODEL_REF_FIELDS_V1,
) -> dict[str, Any]:
    """Return the stable model-facing schema for local-reference transport."""

    without_code_echoes = _project_response_schema_code_echoes(response_schema)
    return project_schema_id_fields(
        without_code_echoes,
        ref_map=build_model_ref_map({}),
        field_names_by_namespace=field_names_by_namespace,
    )


def assert_no_persistent_ids(
    value: Any,
    *,
    ref_map: Mapping[str, Any],
) -> None:
    """Ensure model-visible JSON does not contain any mapped persistent id."""

    validated = validate_model_ref_map(ref_map)
    encoded = canonical_json(value)
    for row in validated["entries"]:
        persistent_id = row["persistent_id"]
        if persistent_id in encoded:
            raise ModelRefError(
                f"persistent id leaked into model-visible payload: {persistent_id}"
            )


def build_model_ref_map_for_request(
    request: Mapping[str, Any],
    *,
    field_names_by_namespace: Mapping[str, Sequence[str]] = MODEL_REF_FIELDS_V1,
) -> dict[str, Any]:
    """Collect only declared opaque IDs from one rendered request.

    The user message is parsed as JSON because all active Literary semantic
    requests use a JSON payload.  Hashes, source locations and other fields
    are never collected unless explicitly listed in the field map.
    """

    if not isinstance(request, Mapping):
        raise ModelRefError("model-local request must be an object")
    values: dict[str, set[str]] = {
        _canonical_namespace(namespace): set()
        for namespace in field_names_by_namespace
    }
    messages = request.get("messages")
    if not isinstance(messages, list):
        raise ModelRefError("model-local request messages must be a list")
    user_payloads: list[Any] = []
    for message in messages:
        if not isinstance(message, Mapping):
            raise ModelRefError("model-local request message is malformed")
        if message.get("role") != "user":
            continue
        content = message.get("content")
        if not isinstance(content, str):
            raise ModelRefError("model-local user message is not text")
        try:
            user_payloads.append(json.loads(content))
        except json.JSONDecodeError as exc:
            raise ModelRefError("model-local user message is not JSON") from exc
    if not user_payloads:
        raise ModelRefError("model-local request has no JSON user message")
    for payload in user_payloads:
        _collect_declared_ids(payload, values, field_names_by_namespace)
    _collect_declared_ids(
        request.get("response_schema"), values, field_names_by_namespace
    )
    for key in ("component_ids", "batch_id"):
        if key in request:
            _collect_declared_ids(
                {key: request[key]}, values, field_names_by_namespace
            )
    return build_model_ref_map(values)


def project_model_request_v1(
    request: Mapping[str, Any],
    *,
    field_names_by_namespace: Mapping[str, Sequence[str]] = MODEL_REF_FIELDS_V1,
    instruction: str | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Project a persistent-ID request into a sealed local-label envelope."""

    if not isinstance(request, Mapping):
        raise ModelRefError("model-local request must be an object")
    body = deepcopy(dict(request))
    if body.get("model_reference_mode") == MODEL_REF_MODE_CLASSIFIED_V1:
        ref_map = validate_model_ref_map(body.get("model_ref_map"))
        if body.get("model_ref_map_hash") != ref_map["map_hash"]:
            raise ModelRefError("model-local map hash is not bound")
        if body.get("model_ref_fields_hash") != model_ref_fields_hash_v1(
            field_names_by_namespace
        ):
            raise ModelRefError("model-local field map hash differs")
        code_owned_echoes = _validate_code_owned_echoes(
            body.get("model_code_owned_echoes")
        )
        if (
            body.get("model_code_owned_echoes_hash")
            != code_owned_echoes["echoes_hash"]
        ):
            raise ModelRefError("model-local code-owned echo hash is not bound")
        expected = body.get("request_fingerprint")
        unsigned = deepcopy(body)
        unsigned.pop("request_fingerprint", None)
        if not isinstance(expected, str) or canonical_hash(unsigned) != expected:
            raise ModelRefError("model-local request fingerprint mismatch")
        assert_no_persistent_ids(body.get("messages"), ref_map=ref_map)
        assert_no_persistent_ids(body.get("response_schema"), ref_map=ref_map)
        if _contains_code_owned_field(body.get("messages")):
            raise ModelRefError("code-owned field leaked into model-visible messages")
        return body, ref_map
    ref_map = build_model_ref_map_for_request(
        body, field_names_by_namespace=field_names_by_namespace
    )
    messages = deepcopy(body["messages"])
    user_seen = False
    user_payloads = [
        json.loads(str(row.get("content")))
        for row in messages
        if isinstance(row, Mapping) and row.get("role") == "user"
    ]
    schema = request.get("response_schema")
    if not isinstance(schema, Mapping):
        raise ModelRefError("model-local response schema is absent")
    code_owned_echoes = _build_code_owned_echoes(
        request=body,
        user_payloads=user_payloads,
        response_schema=schema,
    )
    projected_messages: list[dict[str, Any]] = []
    instruction_seen = False
    for message in messages:
        row = dict(message)
        if row.get("role") == "user":
            payload = json.loads(row["content"])
            payload = _strip_code_owned_fields(payload)
            payload = project_id_fields(
                payload,
                ref_map=ref_map,
                field_names_by_namespace=field_names_by_namespace,
            )
            assert_no_persistent_ids(payload, ref_map=ref_map)
            _assert_no_unmapped_persistent_refs(payload)
            row["content"] = canonical_json(payload)
            user_seen = True
        elif row.get("role") == "system" and instruction:
            row["content"] = f"{row['content']}\n\n{instruction}"
            instruction_seen = True
        projected_messages.append(row)
    if not user_seen:
        raise ModelRefError("model-local request has no user message")
    if instruction and not instruction_seen:
        projected_messages.insert(0, {"role": "system", "content": instruction})
    projected_schema = project_model_response_schema_v1(
        schema,
        field_names_by_namespace=field_names_by_namespace,
    )
    assert_no_persistent_ids(projected_messages, ref_map=ref_map)
    assert_no_persistent_ids(projected_schema, ref_map=ref_map)
    _assert_no_unmapped_persistent_refs(projected_schema)
    if _contains_code_owned_field(projected_messages):
        raise ModelRefError("code-owned field leaked into model-visible messages")
    if _contains_code_owned_field(projected_schema):
        raise ModelRefError("code-owned field leaked into model-visible schema")
    body["messages"] = projected_messages
    body["response_schema"] = projected_schema
    body["response_schema_hash"] = canonical_hash(projected_schema)
    system_text = "\n".join(
        str(row["content"])
        for row in projected_messages
        if row.get("role") == "system"
    )
    if "prompt_sha256" in body:
        body["prompt_sha256"] = hashlib.sha256(
            system_text.encode("utf-8")
        ).hexdigest()
    reserve = body.get("token_reserve")
    if isinstance(reserve, Mapping) and isinstance(
        reserve.get("output_token_cap"), int
    ):
        try:
            from pipeline.literary.structured_prompt_reserve_v1 import (
                structured_prompt_reserve_v1,
            )

            body["token_reserve"] = structured_prompt_reserve_v1(
                messages=projected_messages,
                response_schema=projected_schema,
                output_token_cap=int(reserve["output_token_cap"]),
            ).to_payload()
        except (TypeError, ValueError) as exc:
            raise ModelRefError("model-local token reserve cannot be recomputed") from exc
    body["model_reference_mode"] = MODEL_REF_MODE_CLASSIFIED_V1
    body["model_ref_map"] = ref_map
    body["model_ref_map_hash"] = ref_map["map_hash"]
    body["model_ref_fields_hash"] = model_ref_fields_hash_v1(
        field_names_by_namespace
    )
    body["model_code_owned_echoes"] = code_owned_echoes
    body["model_code_owned_echoes_hash"] = code_owned_echoes["echoes_hash"]
    body.pop("request_fingerprint", None)
    return {**body, "request_fingerprint": canonical_hash(body)}, ref_map


def resolve_model_response_v1(
    request: Mapping[str, Any],
    response: Mapping[str, Any],
    *,
    field_names_by_namespace: Mapping[str, Sequence[str]] = MODEL_REF_FIELDS_V1,
) -> dict[str, Any]:
    """Resolve local labels after transport, before semantic validation."""

    if not isinstance(request, Mapping) or not isinstance(response, Mapping):
        raise ModelRefError("model-local request/response must be objects")
    if request.get("model_reference_mode") != MODEL_REF_MODE_CLASSIFIED_V1:
        raise ModelRefError("request is not a classified local-ref envelope")
    expected = request.get("request_fingerprint")
    unsigned = deepcopy(dict(request))
    unsigned.pop("request_fingerprint", None)
    if not isinstance(expected, str) or canonical_hash(unsigned) != expected:
        raise ModelRefError("model-local request fingerprint mismatch")
    ref_map = validate_model_ref_map(request.get("model_ref_map"))
    if request.get("model_ref_map_hash") != ref_map["map_hash"]:
        raise ModelRefError("model-local map hash is not bound")
    if request.get("model_ref_fields_hash") != model_ref_fields_hash_v1(
        field_names_by_namespace
    ):
        raise ModelRefError("model-local field map hash differs")
    code_owned_echoes = _validate_code_owned_echoes(
        request.get("model_code_owned_echoes")
    )
    if request.get("model_code_owned_echoes_hash") != code_owned_echoes["echoes_hash"]:
        raise ModelRefError("model-local code-owned echo hash is not bound")
    assert_no_persistent_ids(response, ref_map=ref_map)
    resolved = resolve_id_fields(
        response,
        ref_map=ref_map,
        field_names_by_namespace=field_names_by_namespace,
    )
    for field, value in code_owned_echoes["fields"].items():
        if field in resolved:
            raise ModelRefError(f"model returned code-owned field: {field}")
        resolved[field] = deepcopy(value)
    return resolved


def model_ref_instruction_v1() -> str:
    return (
        "Transport reference rule: opaque references use short labels only. "
        "E=entity, O=scan observation, G=glossary observation, C=component, "
        "F=frame, V=event, T=dialogue turn, S=temporal state, Q=pending case, "
        "R=review, B=batch. Determine the response form from the item shown in "
        "this request, never from the output field's name. If an item is shown "
        "with a short label in this request, every structured selection, citation, "
        "or pointer to that item must use that exact label, regardless of the "
        "output field's name. In free-form prose fields such as notes, reasons, "
        "summaries, rationales, and explanations, use the canonical surface or "
        "name from the source, never a transport label. Copy only labels present "
        "in this request. Never output persistent IDs, hashes, fingerprints, "
        "schema hashes, or invent a label. These labels are transport handles, "
        "not semantic decisions."
    )


def model_ref_fields_hash_v1(
    field_names_by_namespace: Mapping[str, Sequence[str]] = MODEL_REF_FIELDS_V1,
) -> str:
    normalized = {
        _canonical_namespace(namespace): sorted(
            {_required_id(name, "field name") for name in names}
        )
        for namespace, names in field_names_by_namespace.items()
    }
    return canonical_hash(normalized)


def _build_code_owned_echoes(
    *,
    request: Mapping[str, Any],
    user_payloads: Sequence[Any],
    response_schema: Mapping[str, Any],
) -> dict[str, Any]:
    properties = response_schema.get("properties")
    if not isinstance(properties, Mapping):
        raise ModelRefError("model-local response schema properties are absent")
    echoes: dict[str, Any] = {}
    for field in sorted(str(name) for name in properties if _is_code_owned_field(str(name))):
        values: list[Any] = []
        for payload in user_payloads:
            values.extend(_values_for_key(payload, field))
        if field in request:
            values.append(request[field])
        property_schema = properties.get(field)
        if isinstance(property_schema, Mapping):
            if "const" in property_schema:
                values.append(property_schema["const"])
            enum = property_schema.get("enum")
            if isinstance(enum, list) and len(enum) == 1:
                values.append(enum[0])
        unique = {canonical_json(value): value for value in values}
        if len(unique) != 1:
            raise ModelRefError(
                f"code-owned response field has no unique request value: {field}"
            )
        echoes[field] = deepcopy(next(iter(unique.values())))
    body = {
        "schema_version": CODE_OWNED_ECHO_SCHEMA_VERSION,
        "fields": echoes,
    }
    return {**body, "echoes_hash": canonical_hash(body)}


def _validate_code_owned_echoes(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ModelRefError("model-local code-owned echoes must be an object")
    body = deepcopy(dict(value))
    observed = body.pop("echoes_hash", None)
    if body.get("schema_version") != CODE_OWNED_ECHO_SCHEMA_VERSION:
        raise ModelRefError("foreign model-local code-owned echo schema")
    fields = body.get("fields")
    if not isinstance(fields, Mapping):
        raise ModelRefError("model-local code-owned echo fields are malformed")
    if not all(
        isinstance(name, str) and _is_code_owned_field(name) for name in fields
    ):
        raise ModelRefError("model-local code-owned echo field is not allowlisted")
    if not isinstance(observed, str) or observed != canonical_hash(body):
        raise ModelRefError("model-local code-owned echo hash mismatch")
    return {**body, "echoes_hash": observed}


def _project_response_schema_code_echoes(
    response_schema: Mapping[str, Any],
) -> dict[str, Any]:
    body = deepcopy(dict(response_schema))
    properties = body.get("properties")
    if not isinstance(properties, Mapping):
        raise ModelRefError("model-local response schema properties are absent")
    projected_properties = dict(properties)
    removed = {
        str(name) for name in projected_properties if _is_code_owned_field(str(name))
    }
    for field in removed:
        projected_properties.pop(field, None)
    body["properties"] = projected_properties
    required = body.get("required")
    if isinstance(required, list):
        body["required"] = [name for name in required if str(name) not in removed]
    _reject_nested_code_owned_schema_fields(body)
    return body


def _reject_nested_code_owned_schema_fields(
    value: Any, *, path: tuple[str, ...] = ()
) -> None:
    if isinstance(value, Mapping):
        properties = value.get("properties")
        if isinstance(properties, Mapping):
            for name in properties:
                if path and _is_code_owned_field(str(name)):
                    raise ModelRefError(
                        "nested code-owned response field needs an explicit transport binding: "
                        + ".".join((*path, str(name)))
                    )
            for name, child in properties.items():
                _reject_nested_code_owned_schema_fields(
                    child, path=(*path, str(name))
                )
        for key in ("items", "anyOf", "oneOf", "allOf"):
            child = value.get(key)
            if child is not None:
                _reject_nested_code_owned_schema_fields(child, path=path)
    elif isinstance(value, list):
        for child in value:
            _reject_nested_code_owned_schema_fields(child, path=path)


def _strip_code_owned_fields(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {
            str(key): _strip_code_owned_fields(child)
            for key, child in value.items()
            if not _is_code_owned_field(str(key))
        }
    if isinstance(value, list):
        return [_strip_code_owned_fields(child) for child in value]
    return deepcopy(value)


def _values_for_key(value: Any, key_name: str) -> list[Any]:
    values: list[Any] = []
    if isinstance(value, Mapping):
        for key, child in value.items():
            if str(key) == key_name:
                values.append(child)
            values.extend(_values_for_key(child, key_name))
    elif isinstance(value, list):
        for child in value:
            values.extend(_values_for_key(child, key_name))
    return values


def _is_code_owned_field(name: str) -> bool:
    return (
        name == "request_fingerprint"
        or name.endswith("_fingerprint")
        or name.endswith("_hash")
        or name.endswith("_sha256")
    )


def _contains_code_owned_field(value: Any) -> bool:
    if isinstance(value, Mapping):
        return any(
            _is_code_owned_field(str(key)) or _contains_code_owned_field(child)
            for key, child in value.items()
        )
    if isinstance(value, list):
        return any(_contains_code_owned_field(child) for child in value)
    if isinstance(value, str):
        try:
            decoded = json.loads(value)
        except json.JSONDecodeError:
            return False
        return _contains_code_owned_field(decoded)
    return False


def _assert_no_unmapped_persistent_refs(value: Any) -> None:
    if isinstance(value, Mapping):
        for child in value.values():
            _assert_no_unmapped_persistent_refs(child)
        return
    if isinstance(value, list):
        for child in value:
            _assert_no_unmapped_persistent_refs(child)
        return
    if isinstance(value, str) and _OPAQUE_PERSISTENT_REF_RE.fullmatch(value):
        raise ModelRefError(f"unmapped persistent reference leaked to model: {value}")


def _rewrite_id_fields(
    value: Any,
    field_namespace: Mapping[str, str],
    lookup: Mapping[str, Mapping[str, str]],
    *,
    direction: str,
    current_key: str | None = None,
) -> Any:
    namespace = field_namespace.get(current_key or "")
    if namespace is not None:
        if isinstance(value, list):
            return [
                _rewrite_scalar(
                    item,
                    namespace,
                    lookup,
                    direction,
                    current_key=current_key,
                )
                for item in value
            ]
        if value is None:
            return None
        return _rewrite_scalar(
            value,
            namespace,
            lookup,
            direction,
            current_key=current_key,
        )
    if isinstance(value, list):
        return [
            _rewrite_id_fields(
                item,
                field_namespace,
                lookup,
                direction=direction,
                current_key=current_key,
            )
            for item in value
        ]
    if isinstance(value, Mapping):
        return {
            str(key): _rewrite_id_fields(
                child,
                field_namespace,
                lookup,
                direction=direction,
                current_key=str(key),
            )
            for key, child in value.items()
        }
    return deepcopy(value)


def _rewrite_scalar(
    value: Any,
    namespace: str,
    lookup: Mapping[str, Mapping[str, str]],
    direction: str,
    *,
    current_key: str | None = None,
) -> str:
    if not isinstance(value, str) or not value:
        raise ModelRefError(f"{namespace} reference must be a non-empty string")
    if direction == "to_local":
        effective_namespace, canonical_id = _canonical_reference(value, namespace)
        table = lookup.get(effective_namespace, {})
        if canonical_id not in table:
            raise ModelRefError(
                f"foreign {effective_namespace} model reference: {value}"
            )
        return table[canonical_id]

    local_namespace = _namespace_for_local_ref(value)
    effective_namespace = namespace
    if local_namespace is not None and local_namespace != namespace:
        if current_key not in _POLYMORPHIC_REFERENCE_FIELDS:
            raise ModelRefError(f"foreign {namespace} model reference: {value}")
        effective_namespace = local_namespace
    table = lookup.get(effective_namespace, {})
    if value not in table:
        raise ModelRefError(f"foreign {effective_namespace} model reference: {value}")
    return _restore_reference_form(
        table[value], effective_namespace, current_key=current_key
    )


def _persistent_to_local(ref_map: Mapping[str, Any]) -> dict[str, dict[str, str]]:
    result: dict[str, dict[str, str]] = {}
    for row in ref_map["entries"]:
        namespace = str(row["namespace"])
        persistent_id = str(row["persistent_id"])
        local_ref = str(row["local_ref"])
        table = result.setdefault(namespace, {})
        existing = table.get(persistent_id)
        if existing is not None and existing != local_ref:
            raise ModelRefError(f"ambiguous persistent reference: {persistent_id}")
        table[persistent_id] = local_ref
        qualifier = _QUALIFIER_BY_NAMESPACE.get(namespace)
        qualified_prefix = f"{qualifier}:" if qualifier else None
        if qualified_prefix and persistent_id.startswith(qualified_prefix):
            bare_id = persistent_id[len(qualified_prefix) :]
            existing = table.get(bare_id)
            if existing is not None and existing != local_ref:
                raise ModelRefError(f"ambiguous persistent alias: {bare_id}")
            table[bare_id] = local_ref
    return result


def _local_to_persistent(ref_map: Mapping[str, Any]) -> dict[str, dict[str, str]]:
    return {
        namespace: {
            row["local_ref"]: row["persistent_id"]
            for row in ref_map["entries"]
            if row["namespace"] == namespace
        }
        for namespace in {row["namespace"] for row in ref_map["entries"]}
    }


def _canonical_reference(value: str, fallback_namespace: str) -> tuple[str, str]:
    """Return one namespace/id pair for qualified or bare reference values."""

    namespace = _canonical_namespace(fallback_namespace)
    prefix, separator, suffix = value.partition(":")
    if separator and suffix and prefix in _VALUE_QUALIFIER_BY_PREFIX:
        namespace = _VALUE_QUALIFIER_BY_PREFIX[prefix]
        if namespace == "entity":
            # ``additional:`` is an entity source-ref qualifier.  Retain it in
            # the canonical ID because it is semantically meaningful to the
            # chapter registry and is not an alias for a bare entity ID.
            return namespace, value
        qualifier = _QUALIFIER_BY_NAMESPACE[namespace]
        return namespace, f"{qualifier}:{suffix}"
    qualifier = _QUALIFIER_BY_NAMESPACE.get(namespace)
    if qualifier and not value.startswith(f"{qualifier}:"):
        return namespace, f"{qualifier}:{value}"
    return namespace, value


def _namespace_for_local_ref(value: str) -> str | None:
    if not _LOCAL_REF_RE.fullmatch(value):
        return None
    for namespace, prefix in _PREFIX_BY_NAMESPACE.items():
        if value.startswith(prefix):
            return namespace
    return None


def _restore_reference_form(
    persistent_id: str,
    namespace: str,
    *,
    current_key: str | None,
) -> str:
    """Restore the canonical Literary spelling expected by a semantic field."""

    qualifier = _QUALIFIER_BY_NAMESPACE.get(namespace)
    if qualifier and current_key in _BARE_VALUE_FIELDS:
        prefix = f"{qualifier}:"
        if persistent_id.startswith(prefix):
            return persistent_id[len(prefix) :]
    return persistent_id


def _field_namespace(
    field_names_by_namespace: Mapping[str, Sequence[str]],
) -> dict[str, str]:
    result: dict[str, str] = {}
    for raw_namespace, names in field_names_by_namespace.items():
        namespace = _canonical_namespace(raw_namespace)
        _prefix(namespace)
        for name in names:
            field = _required_id(name, "field name")
            previous = result.setdefault(field, namespace)
            if previous != namespace:
                raise ModelRefError(f"field is assigned to two namespaces: {field}")
    return result


def _prefix(namespace: str) -> str:
    value = _canonical_namespace(namespace)
    try:
        return _PREFIX_BY_NAMESPACE[value]
    except KeyError as exc:
        raise ModelRefError(f"unsupported model reference namespace: {value}") from exc


def _required_id(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ModelRefError(f"{label} must be non-empty text")
    return value


def _canonical_namespace(namespace: Any) -> str:
    value = _required_id(namespace, "namespace")
    return _NAMESPACE_ALIASES.get(value, value)


def _collect_declared_ids(
    value: Any,
    output: dict[str, set[str]],
    field_names_by_namespace: Mapping[str, Sequence[str]],
    *,
    current_key: str | None = None,
) -> None:
    field_namespace = _field_namespace(field_names_by_namespace)
    namespace = field_namespace.get(current_key or "")
    if namespace is not None:
        if value is None:
            return
        if isinstance(value, Mapping) and "enum" in value:
            values = value.get("enum")
        elif isinstance(value, Mapping) and isinstance(value.get("items"), Mapping):
            values = value["items"].get("enum")
        elif isinstance(value, Mapping):
            values = None
        else:
            values = value
        if values is None:
            return
        if isinstance(values, list):
            candidates = values
        else:
            candidates = [values]
        for item in candidates:
            if not isinstance(item, str) or not item:
                raise ModelRefError(
                    f"{namespace} request reference must be non-empty text"
                )
            target_namespace, canonical_id = _canonical_reference(item, namespace)
            output.setdefault(target_namespace, set()).add(canonical_id)
        if isinstance(value, Mapping):
            for key, child in value.items():
                if key != "enum":
                    _collect_declared_ids(
                        child,
                        output,
                        field_names_by_namespace,
                        current_key=str(key),
                    )
        return
    if isinstance(value, Mapping):
        for key, child in value.items():
            _collect_declared_ids(
                child,
                output,
                field_names_by_namespace,
                current_key=str(key),
            )
    elif isinstance(value, list):
        for child in value:
            _collect_declared_ids(
                child,
                output,
                field_names_by_namespace,
                current_key=current_key,
            )


__all__ = [
    "CODE_OWNED_ECHO_SCHEMA_VERSION",
    "MODEL_REF_FIELDS_V1",
    "MODEL_REF_MAP_SCHEMA_VERSION",
    "TRANSPORT_EXEMPT_FIELDS_V1",
    "MODEL_REF_MODE_CLASSIFIED_V1",
    "ModelRefError",
    "assert_no_persistent_ids",
    "build_model_ref_map",
    "build_model_ref_map_for_request",
    "model_ref_instruction_v1",
    "model_ref_fields_hash_v1",
    "project_id_fields",
    "project_model_request_v1",
    "project_model_response_schema_v1",
    "project_schema_id_fields",
    "resolve_id_fields",
    "resolve_model_response_v1",
    "validate_model_ref_map",
]
