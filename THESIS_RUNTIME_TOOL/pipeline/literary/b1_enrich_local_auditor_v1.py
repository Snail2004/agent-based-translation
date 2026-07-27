"""Exception-only Local Auditor for a validated B1-Enrich chapter artifact.

The model reviews only explicit Enrich exceptions and unreviewed relation/alias
proposals. Code owns packet construction, reference membership, source bounds,
exact coverage and the append-only decision artifact; it does not decide literary
meaning, repair model citations, or rewrite otherwise clean dossier claims.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
import hashlib
from pathlib import Path
import re
from typing import Any, Callable, Iterable, Mapping, Sequence

from pipeline.literary.b1_enrich_v1 import (
    KINSHIP_RELATIONS,
    LINK_RELATIONS,
)
from pipeline.literary.b1_scan_v1 import (
    PRESENCE_BASES,
    _exact_keys,
    _normalized_surface,
    _required_string,
    _source_blocks,
    _string_list,
)
from pipeline.literary.builder_pilot import load_system_prompt_from_design
from pipeline.literary.chapter_registry_schema_v4 import RenderedRegistryRequestV4
from pipeline.literary.checkpoint import canonical_hash, canonical_json
from pipeline.literary.response_normalization_v1 import (
    attach_response_normalization_notes_v1,
    normalize_code_owned_response_echoes_v1,
)
from pipeline.literary.request_token_preflight_v1 import (
    LiteraryRequestTokenPreflightV1,
    measure_literary_request_token_preflight_v1,
)


PROMPT_ID = "literary_b1_enrich_local_auditor_v1_6"
ROLE_ID = "literary.audit.b1_enrich_local"
REQUEST_SCHEMA_VERSION = "literary_b1_enrich_local_audit_request_v1"
OUTPUT_SCHEMA_ID = "LiteraryB1EnrichLocalAuditOutputV1"
ARTIFACT_SCHEMA_VERSION = "literary_b1_enrich_local_audit_artifact_v1"
MANIFEST_SCHEMA_VERSION = "literary_b1_enrich_local_audit_manifest_v1"

COMPONENT_KINDS = frozenset(
    {
        "additional_entity",
        "alias_proposal",
        "entity_link",
        "glossary_ambiguity",
        "kinship_link",
        "presence_correction",
        "same_referent_proposal",
        "spurious_challenge",
    }
)
DECISION_ACTIONS = frozenset(
    {
        "accept_proposal",
        "revise_proposal",
        "reject_proposal",
        "keep_pending",
        "refer_cross_chapter",
    }
)
LINK_COMPONENT_KINDS = frozenset({"entity_link", "kinship_link"})
ALLOWED_ACTIONS_BY_KIND = {
    "additional_entity": frozenset(
        {"accept_proposal", "reject_proposal", "keep_pending", "refer_cross_chapter"}
    ),
    "alias_proposal": frozenset(
        {"accept_proposal", "reject_proposal", "keep_pending", "refer_cross_chapter"}
    ),
    "entity_link": DECISION_ACTIONS,
    "glossary_ambiguity": frozenset(
        {"accept_proposal", "reject_proposal", "keep_pending", "refer_cross_chapter"}
    ),
    "kinship_link": DECISION_ACTIONS,
    "presence_correction": frozenset(
        {"accept_proposal", "reject_proposal", "keep_pending"}
    ),
    "same_referent_proposal": frozenset(
        {"accept_proposal", "reject_proposal", "keep_pending", "refer_cross_chapter"}
    ),
    "spurious_challenge": frozenset(
        {"accept_proposal", "reject_proposal", "keep_pending", "refer_cross_chapter"}
    ),
}
MAX_COMPONENTS = 32


class B1EnrichLocalAuditError(ValueError):
    pass


def _nullable_string(enum: Iterable[str] | None = None) -> dict[str, Any]:
    schema: dict[str, Any] = {"type": "string", "minLength": 1}
    if enum is not None:
        schema["enum"] = sorted(enum)
    return {"anyOf": [schema, {"type": "null"}]}


def b1_enrich_local_audit_response_schema_v1() -> dict[str, Any]:
    block_ids = {
        "type": "array",
        "items": {"type": "string", "minLength": 1},
        "minItems": 1,
        "maxItems": 8,
        "uniqueItems": True,
    }
    decision = {
        "type": "object",
        "additionalProperties": False,
        "required": [
            "component_id",
            "action",
            "revised_relation",
            "revised_relation_note",
            "revised_target_ref",
            "source_block_ids",
            "resolution_note",
        ],
        "properties": {
            "component_id": {"type": "string", "minLength": 1},
            "action": {"type": "string", "enum": sorted(DECISION_ACTIONS)},
            "revised_relation": _nullable_string(KINSHIP_RELATIONS | LINK_RELATIONS),
            "revised_relation_note": _nullable_string(),
            "revised_target_ref": _nullable_string(),
            "source_block_ids": block_ids,
            "resolution_note": {"type": "string", "minLength": 1},
        },
    }
    return {
        "type": "object",
        "additionalProperties": False,
        "required": [
            "schema_id",
            "chapter_id",
            "manifest_hash",
            "decisions",
            "unasked_same_referent_observations",
        ],
        "properties": {
            "schema_id": {"type": "string", "enum": [OUTPUT_SCHEMA_ID]},
            "chapter_id": {"type": "string", "minLength": 1},
            "manifest_hash": {"type": "string", "minLength": 64, "maxLength": 64},
            "decisions": {"type": "array", "items": decision, "maxItems": MAX_COMPONENTS},
            "unasked_same_referent_observations": {
                "type": "array",
                "maxItems": 8,
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": [
                        "subject_ref",
                        "target_ref",
                        "source_block_ids",
                        "reason",
                    ],
                    "properties": {
                        "subject_ref": {"type": "string", "minLength": 1},
                        "target_ref": {"type": "string", "minLength": 1},
                        "source_block_ids": block_ids,
                        "reason": {"type": "string", "minLength": 1},
                    },
                },
            },
        },
    }


def build_b1_enrich_local_audit_manifest_v1(
    *,
    chapter: Mapping[str, Any],
    scan_artifact: Mapping[str, Any],
    enrich_artifact: Mapping[str, Any],
    component_ids: Sequence[str] | None = None,
) -> dict[str, Any]:
    chapter_id = _required_string(chapter.get("chapter_id"), "chapter_id")
    if scan_artifact.get("chapter_id") != chapter_id:
        raise B1EnrichLocalAuditError("B1-Scan artifact belongs to another chapter")
    if enrich_artifact.get("chapter_id") != chapter_id:
        raise B1EnrichLocalAuditError("B1-Enrich artifact belongs to another chapter")
    if enrich_artifact.get("scan_artifact_hash") != scan_artifact.get("artifact_hash"):
        raise B1EnrichLocalAuditError("B1-Enrich lineage differs from B1-Scan")

    blocks = _source_blocks(chapter)
    block_by_id = {row["block_id"]: row for row in blocks}
    block_order = {row["block_id"]: index for index, row in enumerate(blocks)}
    scan_rows = _scan_rows(scan_artifact)
    scan_by_id = {row["observation_id"]: row for row in scan_rows}

    cards: dict[str, dict[str, Any]] = {}
    dossier_by_scan_id: dict[str, Mapping[str, Any]] = {}
    for raw in enrich_artifact.get("entity_dossiers") or []:
        if not isinstance(raw, Mapping):
            raise B1EnrichLocalAuditError("B1-Enrich entity dossier is malformed")
        scan_id = _required_string(raw.get("scan_observation_id"), "scan_observation_id")
        if scan_id not in scan_by_id or scan_id in dossier_by_scan_id:
            raise B1EnrichLocalAuditError("B1-Enrich dossier cites a foreign/duplicate scan row")
        dossier_by_scan_id[scan_id] = raw
        task_ref = _required_string(raw.get("task_ref"), "task_ref")
        expected_ref = f"scan:{scan_id}"
        if task_ref != expected_ref:
            raise B1EnrichLocalAuditError("B1-Enrich task_ref differs from its scan row")
        cards[task_ref] = _dossier_card(raw, scan_by_id[scan_id])

    additional_by_ref: dict[str, Mapping[str, Any]] = {}
    for raw in enrich_artifact.get("additional_entity_dossiers") or []:
        if not isinstance(raw, Mapping):
            raise B1EnrichLocalAuditError("additional entity dossier is malformed")
        additional_id = _required_string(raw.get("additional_entity_id"), "additional_entity_id")
        ref = f"additional:{additional_id}"
        if ref in cards:
            raise B1EnrichLocalAuditError("additional entity ref is duplicated")
        additional_by_ref[ref] = raw
        cards[ref] = _additional_card(raw, ref)

    components: list[dict[str, Any]] = []
    mechanical_noops: list[dict[str, Any]] = []
    quarantined_rows: list[dict[str, Any]] = []
    component_keys: set[str] = set()

    def add_component(
        *,
        kind: str,
        subject_ref: str,
        proposal: Mapping[str, Any],
        direct_source_block_ids: Sequence[str],
    ) -> None:
        if kind not in COMPONENT_KINDS:
            raise B1EnrichLocalAuditError("unknown local-audit component kind")
        source_ids = _known_block_ids(
            direct_source_block_ids, block_by_id, "component source_block_ids"
        )
        body = {
            "component_kind": kind,
            "subject_ref": subject_ref,
            "proposal": deepcopy(dict(proposal)),
            "direct_source_block_ids": source_ids,
        }
        key = canonical_hash(body)
        if key in component_keys:
            return
        component_keys.add(key)
        components.append(
            {
                "component_id": f"b1lac_{key[:20]}",
                **body,
            }
        )

    for raw in enrich_artifact.get("spurious_challenges") or []:
        scan_id = _required_string(raw.get("scan_observation_id"), "challenge scan id")
        scan = scan_by_id.get(scan_id)
        if scan is None:
            raise B1EnrichLocalAuditError("challenge cites a foreign scan row")
        add_component(
            kind="spurious_challenge",
            subject_ref=f"scan:{scan_id}",
            proposal={"scan_observation": scan, "challenge": raw},
            direct_source_block_ids=raw.get("source_block_ids") or [],
        )

    for ref, raw in sorted(additional_by_ref.items()):
        add_component(
            kind="additional_entity",
            subject_ref=ref,
            proposal=raw,
            direct_source_block_ids=raw.get("source_block_ids") or [],
        )

    for raw in enrich_artifact.get("presence_correction_findings") or []:
        scan_id = _required_string(raw.get("scan_observation_id"), "presence scan id")
        scan = scan_by_id.get(scan_id)
        if scan is None:
            raise B1EnrichLocalAuditError("presence finding cites a foreign scan row")
        proposed = _required_string(
            raw.get("proposed_presence_basis"), "proposed_presence_basis"
        )
        if proposed not in PRESENCE_BASES:
            raise B1EnrichLocalAuditError("presence finding uses an unknown basis")
        if proposed == scan["presence_basis"]:
            mechanical_noops.append(
                {
                    "kind": "presence_correction_same_as_scan",
                    "scan_observation_id": scan_id,
                    "presence_basis": proposed,
                }
            )
            continue
        add_component(
            kind="presence_correction",
            subject_ref=f"scan:{scan_id}",
            proposal={"scan_presence_basis": scan["presence_basis"], "finding": raw},
            direct_source_block_ids=raw.get("source_block_ids") or [],
        )

    allowed_refs = set(cards)

    named_rows = [
        row
        for row in scan_rows
        if row.get("record_class") == "named_entity_candidate"
        and row.get("observation_id") in dossier_by_scan_id
    ]
    for left_index, left in enumerate(named_rows):
        for right in named_rows[left_index + 1 :]:
            if left.get("referent_kind_claim") != right.get("referent_kind_claim"):
                continue
            left_tokens = _name_tokens(left.get("surface"))
            right_tokens = _name_tokens(right.get("surface"))
            orientation = _strict_name_variant_orientation(
                left_tokens=left_tokens,
                right_tokens=right_tokens,
            )
            if orientation is None:
                continue
            subject, target = (left, right) if orientation == "left" else (right, left)
            subject_ref = f"scan:{subject['observation_id']}"
            target_ref = f"scan:{target['observation_id']}"
            source_ids = _bounded_pair_evidence_ids(
                subject.get("source_block_ids") or [],
                target.get("source_block_ids") or [],
                block_order=block_order,
            )
            add_component(
                kind="same_referent_proposal",
                subject_ref=subject_ref,
                proposal={
                    "target_ref": target_ref,
                    "proposal_basis": "name_variant",
                    "source_block_ids": source_ids,
                    "reason": (
                        "One supplied named surface is a strict token subsequence "
                        "of the other; this is a review proposal, not an identity decision."
                    ),
                    "retrieval_surface_policy": "subject_stable_name_variant",
                    "identity_authority_granted": False,
                },
                direct_source_block_ids=source_ids,
            )

    for raw in enrich_artifact.get("same_referent_proposals") or []:
        if not isinstance(raw, Mapping):
            raise B1EnrichLocalAuditError("same-referent proposal is malformed")
        subject_ref = _required_string(raw.get("subject_ref"), "subject_ref")
        target_ref = _required_string(raw.get("target_ref"), "target_ref")
        if (
            subject_ref == target_ref
            or subject_ref not in allowed_refs
            or target_ref not in allowed_refs
        ):
            raise B1EnrichLocalAuditError(
                "same-referent proposal cites a foreign or reflexive ref"
            )
        if (
            raw.get("proposal_basis") != "chapter_context_description"
            or raw.get("retrieval_surface_policy") != "subject_evidence_only"
            or raw.get("identity_authority_granted") is not False
        ):
            raise B1EnrichLocalAuditError("same-referent proposal policy differs")
        add_component(
            kind="same_referent_proposal",
            subject_ref=subject_ref,
            proposal=raw,
            direct_source_block_ids=raw.get("source_block_ids") or [],
        )

    for scan_id, dossier in sorted(dossier_by_scan_id.items()):
        subject_ref = f"scan:{scan_id}"
        for collection_name, kind, allowed_relations in (
            ("kinship_links", "kinship_link", KINSHIP_RELATIONS),
            ("links", "entity_link", LINK_RELATIONS),
        ):
            for index, raw in enumerate(dossier.get(collection_name) or []):
                if not isinstance(raw, Mapping):
                    raise B1EnrichLocalAuditError("relation proposal is malformed")
                relation = _required_string(raw.get("relation"), "relation")
                target_ref = _required_string(raw.get("target_ref"), "target_ref")
                quarantine_reason = None
                if relation not in allowed_relations:
                    quarantine_reason = "relation_outside_closed_family"
                elif target_ref not in allowed_refs:
                    quarantine_reason = "target_ref_outside_supplied_entity_pool"
                if quarantine_reason is not None:
                    quarantined_rows.append(
                        {
                            "row_type": kind,
                            "subject_ref": subject_ref,
                            "row_index": index,
                            "reason": quarantine_reason,
                            "raw_row": deepcopy(dict(raw)),
                        }
                    )
                    continue
                add_component(
                    kind=kind,
                    subject_ref=subject_ref,
                    proposal=raw,
                    direct_source_block_ids=raw.get("anchor_block_ids") or [],
                )
        for index, raw in enumerate(dossier.get("aliases_observed") or []):
            if not isinstance(raw, Mapping):
                raise B1EnrichLocalAuditError("alias proposal is malformed")
            add_component(
                kind="alias_proposal",
                subject_ref=subject_ref,
                proposal=raw,
                direct_source_block_ids=raw.get("anchor_block_ids") or [],
            )

    for raw in enrich_artifact.get("glossary_items") or []:
        if not isinstance(raw, Mapping):
            raise B1EnrichLocalAuditError("glossary item is malformed")
        if raw.get("ambiguity_status") == "clear":
            continue
        term_id = _required_string(raw.get("term_observation_id"), "term_observation_id")
        add_component(
            kind="glossary_ambiguity",
            subject_ref=f"glossary:{term_id}",
            proposal=raw,
            direct_source_block_ids=raw.get("source_block_ids") or [],
        )

    components.sort(key=lambda row: (row["component_kind"], row["component_id"]))
    if component_ids is not None:
        requested_ids = list(component_ids)
        if not all(isinstance(value, str) and value for value in requested_ids):
            raise B1EnrichLocalAuditError("local-audit component ids are malformed")
        if len(requested_ids) != len(set(requested_ids)):
            raise B1EnrichLocalAuditError("local-audit component ids are duplicated")
        component_by_id = {row["component_id"]: row for row in components}
        if not set(requested_ids) <= set(component_by_id):
            raise B1EnrichLocalAuditError("local-audit batch cites a foreign component")
        components = [component_by_id[value] for value in requested_ids]
    used_refs = {
        ref
        for component in components
        for ref in (
            component["subject_ref"],
            str(component["proposal"].get("target_ref") or ""),
        )
        if ref in cards
    }
    direct_ids = sorted(
        {
            block_id
            for component in components
            for block_id in component["direct_source_block_ids"]
        },
        key=block_order.__getitem__,
    )
    selected_ids = list(direct_ids)
    for block_id in list(direct_ids):
        center = block_order[block_id]
        for offset in (center - 1, center + 1):
            if 0 <= offset < len(blocks):
                neighbor = blocks[offset]["block_id"]
                if neighbor not in selected_ids:
                    selected_ids.append(neighbor)
    selected_ids.sort(key=block_order.__getitem__)
    body = {
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "chapter_id": chapter_id,
        "scan_artifact_hash": _required_string(
            scan_artifact.get("artifact_hash"), "scan artifact hash"
        ),
        "enrich_artifact_hash": _required_string(
            enrich_artifact.get("artifact_hash"), "enrich artifact hash"
        ),
        "entity_cards": [cards[ref] for ref in sorted(used_refs)],
        "components": components,
        "allowed_target_refs": sorted(cards),
        "allowed_source_block_ids": selected_ids,
        "source_blocks": [
            {"block_id": block_id, "text": block_by_id[block_id]["text"]}
            for block_id in selected_ids
        ],
        "mechanical_noops": sorted(
            mechanical_noops,
            key=lambda row: (row["kind"], row["scan_observation_id"]),
        ),
        "quarantined_rows": quarantined_rows,
        "deferred_enrich_rows": {
            "quarantined_tasks": deepcopy(
                list(enrich_artifact.get("quarantined_tasks") or [])
            ),
            "review_issues": deepcopy(list(enrich_artifact.get("review_issues") or [])),
        },
    }
    return {**body, "manifest_hash": canonical_hash(body)}


def render_b1_enrich_local_audit_request_v1(
    *,
    chapter: Mapping[str, Any],
    scan_artifact: Mapping[str, Any],
    enrich_artifact: Mapping[str, Any],
    design_doc: Path,
    model_id: str = "gpt-5.4",
    reasoning_effort: str = "none",
    temperature: float = 1.0,
    seed: int = 20260721,
    max_output_tokens: int = 4096,
    component_ids: Sequence[str] | None = None,
) -> RenderedRegistryRequestV4:
    manifest = build_b1_enrich_local_audit_manifest_v1(
        chapter=chapter,
        scan_artifact=scan_artifact,
        enrich_artifact=enrich_artifact,
        component_ids=component_ids,
    )
    if len(manifest["components"]) > MAX_COMPONENTS:
        raise B1EnrichLocalAuditError(
            f"local-audit request has {len(manifest['components'])} components; "
            f"response schema cap is {MAX_COMPONENTS}; use component batching"
        )
    prompt = load_system_prompt_from_design(Path(design_doc), PROMPT_ID)
    prompt_sha = hashlib.sha256(prompt.encode("utf-8")).hexdigest()
    schema = b1_enrich_local_audit_response_schema_v1()
    sections = {
        "manifest_hash": manifest["manifest_hash"],
        "entity_cards": manifest["entity_cards"],
        "exception_components": manifest["components"],
        "allowed_target_refs": manifest["allowed_target_refs"],
        "allowed_source_block_ids": manifest["allowed_source_block_ids"],
        "source_blocks": manifest["source_blocks"],
    }
    payload = {
        "schema_version": REQUEST_SCHEMA_VERSION,
        "role": "b1_enrich_local_auditor",
        "chapter_id": manifest["chapter_id"],
        "allowlisted_sections": sections,
    }
    messages = (
        {"role": "system", "content": prompt},
        {"role": "user", "content": canonical_json(payload)},
    )
    model_contract = {
        "model_id": model_id,
        "reasoning_effort": reasoning_effort,
        "temperature": temperature,
        "seed": seed,
        "max_output_tokens": max_output_tokens,
    }
    request_fingerprint = canonical_hash(
        {
            "request_schema_version": REQUEST_SCHEMA_VERSION,
            "manifest_hash": manifest["manifest_hash"],
            "prompt_id": PROMPT_ID,
            "prompt_sha256": prompt_sha,
            "response_schema_hash": canonical_hash(schema),
            "model_contract": model_contract,
            "sections_hash": canonical_hash(sections),
        }
    )
    return RenderedRegistryRequestV4(
        role="auditor",
        prompt_id=PROMPT_ID,
        prompt_sha256=prompt_sha,
        response_schema_hash=canonical_hash(schema),
        chapter_id=manifest["chapter_id"],
        window_id=None,
        parent_working_revision_hash=manifest["enrich_artifact_hash"],
        sections=sections,
        messages=messages,
        request_fingerprint=request_fingerprint,
    )


def shared_b1_enrich_local_audit_request_v1(
    rendered: RenderedRegistryRequestV4,
) -> dict[str, Any]:
    schema = b1_enrich_local_audit_response_schema_v1()
    if rendered.response_schema_hash != canonical_hash(schema):
        raise B1EnrichLocalAuditError("rendered Local Auditor schema binding differs")
    return {
        "messages": [dict(row) for row in rendered.messages],
        "response_schema": schema,
        "request_fingerprint": rendered.request_fingerprint,
    }


@dataclass(frozen=True)
class B1EnrichLocalAuditBatchV1:
    batch_id: str
    component_ids: tuple[str, ...]
    manifest: Mapping[str, Any]
    rendered: RenderedRegistryRequestV4
    request: Mapping[str, Any]
    token_preflight: LiteraryRequestTokenPreflightV1

    def plan_row(self, *, batch_index: int) -> dict[str, Any]:
        return {
            "batch_id": self.batch_id,
            "batch_index": batch_index,
            "component_ids": list(self.component_ids),
            "component_count": len(self.component_ids),
            "source_block_count": len(self.manifest["source_blocks"]),
            "manifest_hash": self.manifest["manifest_hash"],
            "request_fingerprint": self.rendered.request_fingerprint,
            "token_preflight": self.token_preflight.to_payload(),
        }


def plan_b1_enrich_local_audit_batches_v1(
    *,
    chapter: Mapping[str, Any],
    scan_artifact: Mapping[str, Any],
    enrich_artifact: Mapping[str, Any],
    design_doc: Path,
    prompt_token_cap: int,
    output_token_cap: int,
    model_id: str = "gpt-5.4",
    reasoning_effort: str = "none",
    temperature: float = 1.0,
    seed: int = 20260721,
) -> tuple[dict[str, Any], tuple[B1EnrichLocalAuditBatchV1, ...]]:
    """Pack complete components by measured model-visible prompt reserve."""

    full_manifest = build_b1_enrich_local_audit_manifest_v1(
        chapter=chapter,
        scan_artifact=scan_artifact,
        enrich_artifact=enrich_artifact,
    )
    all_component_ids = [
        str(row["component_id"]) for row in full_manifest["components"]
    ]

    def render_group(component_ids: Sequence[str]) -> B1EnrichLocalAuditBatchV1:
        rendered = render_b1_enrich_local_audit_request_v1(
            chapter=chapter,
            scan_artifact=scan_artifact,
            enrich_artifact=enrich_artifact,
            design_doc=design_doc,
            model_id=model_id,
            reasoning_effort=reasoning_effort,
            temperature=temperature,
            seed=seed,
            max_output_tokens=output_token_cap,
            component_ids=component_ids,
        )
        request = shared_b1_enrich_local_audit_request_v1(rendered)
        preflight = measure_literary_request_token_preflight_v1(
            request,
            prompt_token_cap=prompt_token_cap,
            output_token_cap=output_token_cap,
        )
        manifest = build_b1_enrich_local_audit_manifest_v1(
            chapter=chapter,
            scan_artifact=scan_artifact,
            enrich_artifact=enrich_artifact,
            component_ids=component_ids,
        )
        batch_body = {
            "chapter_id": manifest["chapter_id"],
            "component_ids": list(component_ids),
            "manifest_hash": manifest["manifest_hash"],
            "request_fingerprint": rendered.request_fingerprint,
        }
        return B1EnrichLocalAuditBatchV1(
            batch_id=f"b1lab_{canonical_hash(batch_body)[:20]}",
            component_ids=tuple(component_ids),
            manifest=manifest,
            rendered=rendered,
            request=request,
            token_preflight=preflight,
        )

    batches: list[B1EnrichLocalAuditBatchV1] = []
    current_ids: list[str] = []
    current_batch: B1EnrichLocalAuditBatchV1 | None = None
    for component_id in all_component_ids:
        proposed_ids = [*current_ids, component_id]
        if len(proposed_ids) > MAX_COMPONENTS:
            if current_batch is None:
                raise B1EnrichLocalAuditError(
                    "local-audit planner lost its current component batch"
                )
            batches.append(current_batch)
            current_ids = []
            current_batch = None
            proposed_ids = [component_id]

        proposed_batch = render_group(proposed_ids)
        if proposed_batch.token_preflight.fits_prompt_cap:
            current_ids = proposed_ids
            current_batch = proposed_batch
            continue
        if current_batch is not None:
            batches.append(current_batch)
            current_ids = []
            current_batch = None
            proposed_batch = render_group([component_id])
        if not proposed_batch.token_preflight.fits_prompt_cap:
            raise B1EnrichLocalAuditError(
                "local-audit component "
                f"{component_id} alone needs prompt reserve "
                f"{proposed_batch.token_preflight.prompt_token_reserve}, "
                f"above input cap {prompt_token_cap}; source blocks "
                f"{len(proposed_batch.manifest['source_blocks'])}"
            )
        current_ids = [component_id]
        current_batch = proposed_batch
    if current_batch is not None:
        batches.append(current_batch)

    covered_ids = [
        component_id for batch in batches for component_id in batch.component_ids
    ]
    if covered_ids != all_component_ids or len(covered_ids) != len(set(covered_ids)):
        raise B1EnrichLocalAuditError(
            "local-audit batch plan does not exact-cover chapter components"
        )
    body = {
        "schema_version": "literary_b1_enrich_local_audit_batch_plan_v1",
        "chapter_id": full_manifest["chapter_id"],
        "scan_artifact_hash": full_manifest["scan_artifact_hash"],
        "enrich_artifact_hash": full_manifest["enrich_artifact_hash"],
        "full_manifest_hash": full_manifest["manifest_hash"],
        "component_ids": all_component_ids,
        "component_count": len(all_component_ids),
        "prompt_token_cap": prompt_token_cap,
        "output_token_cap": output_token_cap,
        "batches": [
            batch.plan_row(batch_index=index)
            for index, batch in enumerate(batches, start=1)
        ],
    }
    return {**body, "batch_plan_hash": canonical_hash(body)}, tuple(batches)


def _partition_local_audit_decisions(
    decisions: Sequence[Mapping[str, Any]],
) -> tuple[list[dict[str, Any]], ...]:
    accepted: list[dict[str, Any]] = []
    revised: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    pending: list[dict[str, Any]] = []
    referred: list[dict[str, Any]] = []
    for raw_row in decisions:
        row = deepcopy(dict(raw_row))
        action = row["action"]
        outcome = deepcopy(row)
        if action == "accept_proposal":
            outcome["authority_scope"] = _accepted_scope(row["component_kind"])
            accepted.append(outcome)
        elif action == "revise_proposal":
            proposal = deepcopy(dict(row["original_proposal"]))
            proposal["relation"] = row["revised_relation"]
            proposal["target_ref"] = row["revised_target_ref"]
            if row["component_kind"] == "entity_link":
                proposal["relation_note"] = row["revised_relation_note"]
                proposal["relation_raw"] = (
                    row["revised_relation_note"]
                    if row["revised_relation"] == "other_link"
                    else None
                )
                proposal["relation_status"] = (
                    "model_other"
                    if row["revised_relation"] == "other_link"
                    else "in_vocabulary"
                )
            elif row["component_kind"] == "kinship_link":
                proposal["relation_note"] = row["revised_relation_note"]
            outcome["revised_proposal"] = proposal
            outcome["authority_scope"] = "chapter_confirmed"
            revised.append(outcome)
        elif action == "reject_proposal":
            outcome["authority_scope"] = "rejected_with_history"
            rejected.append(outcome)
        elif action == "keep_pending":
            outcome["authority_scope"] = "pending_no_authority"
            pending.append(outcome)
        else:
            outcome["authority_scope"] = "cross_chapter_pending_no_authority"
            referred.append(outcome)
    return accepted, revised, rejected, pending, referred


def merge_b1_enrich_local_audit_batch_artifacts_v1(
    *,
    chapter: Mapping[str, Any],
    scan_artifact: Mapping[str, Any],
    enrich_artifact: Mapping[str, Any],
    batch_plan: Mapping[str, Any],
    batch_artifacts: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Merge locally validated batches into the existing chapter artifact shape."""

    plan_body = deepcopy(dict(batch_plan))
    observed_plan_hash = plan_body.pop("batch_plan_hash", None)
    if not isinstance(observed_plan_hash, str) or canonical_hash(plan_body) != observed_plan_hash:
        raise B1EnrichLocalAuditError("local-audit batch plan hash differs")
    full_manifest = build_b1_enrich_local_audit_manifest_v1(
        chapter=chapter,
        scan_artifact=scan_artifact,
        enrich_artifact=enrich_artifact,
    )
    expected_ids = [row["component_id"] for row in full_manifest["components"]]
    if (
        batch_plan.get("chapter_id") != full_manifest["chapter_id"]
        or batch_plan.get("scan_artifact_hash") != full_manifest["scan_artifact_hash"]
        or batch_plan.get("enrich_artifact_hash")
        != full_manifest["enrich_artifact_hash"]
        or batch_plan.get("full_manifest_hash") != full_manifest["manifest_hash"]
        or batch_plan.get("component_ids") != expected_ids
    ):
        raise B1EnrichLocalAuditError("local-audit batch plan lineage differs")
    raw_batch_rows = batch_plan.get("batches")
    if not isinstance(raw_batch_rows, list) or len(raw_batch_rows) != len(batch_artifacts):
        raise B1EnrichLocalAuditError("local-audit batch artifact count differs")

    decisions: list[dict[str, Any]] = []
    review_issues: list[dict[str, Any]] = []
    observations: list[dict[str, Any]] = []
    observation_review_issues: list[dict[str, Any]] = []
    normalization_notes: list[dict[str, Any]] = []
    covered: set[str] = set()
    for batch_row, raw_artifact in zip(raw_batch_rows, batch_artifacts):
        if not isinstance(batch_row, Mapping) or not isinstance(raw_artifact, Mapping):
            raise B1EnrichLocalAuditError("local-audit batch row is malformed")
        artifact = deepcopy(dict(raw_artifact))
        observed_artifact_hash = artifact.pop("artifact_hash", None)
        if not isinstance(observed_artifact_hash, str) or (
            canonical_hash(artifact) != observed_artifact_hash
        ):
            raise B1EnrichLocalAuditError("local-audit batch artifact hash differs")
        component_ids = batch_row.get("component_ids")
        if not isinstance(component_ids, list) or not component_ids:
            raise B1EnrichLocalAuditError("local-audit batch component ids are malformed")
        if (
            artifact.get("schema_version") != ARTIFACT_SCHEMA_VERSION
            or artifact.get("chapter_id") != full_manifest["chapter_id"]
            or artifact.get("scan_artifact_hash") != full_manifest["scan_artifact_hash"]
            or artifact.get("enrich_artifact_hash")
            != full_manifest["enrich_artifact_hash"]
            or artifact.get("manifest_hash") != batch_row.get("manifest_hash")
            or artifact.get("request_fingerprint")
            != batch_row.get("request_fingerprint")
        ):
            raise B1EnrichLocalAuditError("local-audit batch artifact lineage differs")
        batch_decisions = artifact.get("decisions")
        if not isinstance(batch_decisions, list):
            raise B1EnrichLocalAuditError("local-audit batch decisions are malformed")
        batch_review_issues = artifact.get("review_issues") or []
        if not isinstance(batch_review_issues, list) or not all(
            isinstance(row, Mapping) for row in batch_review_issues
        ):
            raise B1EnrichLocalAuditError(
                "local-audit batch review issues are malformed"
            )
        batch_observations = artifact.get(
            "unasked_same_referent_observations"
        ) or []
        batch_observation_issues = artifact.get("observation_review_issues") or []
        if not isinstance(batch_observations, list) or not all(
            isinstance(row, Mapping) for row in batch_observations
        ):
            raise B1EnrichLocalAuditError(
                "local-audit batch same-referent observations are malformed"
            )
        if not isinstance(batch_observation_issues, list) or not all(
            isinstance(row, Mapping) for row in batch_observation_issues
        ):
            raise B1EnrichLocalAuditError(
                "local-audit batch observation review issues are malformed"
            )
        decision_ids = [row.get("component_id") for row in batch_decisions]
        issue_ids = [row.get("component_id") for row in batch_review_issues]
        actual_ids = decision_ids + issue_ids
        if (
            set(decision_ids).intersection(issue_ids)
            or set(actual_ids) != set(component_ids)
            or len(actual_ids) != len(component_ids)
        ):
            raise B1EnrichLocalAuditError(
                "local-audit batch dispositions do not exact-cover their components"
            )
        if covered.intersection(component_ids):
            raise B1EnrichLocalAuditError("local-audit component was reviewed twice")
        covered.update(component_ids)
        decisions.extend(deepcopy(batch_decisions))
        review_issues.extend(deepcopy(batch_review_issues))
        observations.extend(deepcopy(batch_observations))
        observation_review_issues.extend(deepcopy(batch_observation_issues))
        for note in artifact.get("response_normalization_notes") or []:
            normalized_note = deepcopy(dict(note))
            normalized_note["field_path"] = (
                f"/batches/{batch_row['batch_id']}/{normalized_note['field']}"
            )
            normalization_notes.append(normalized_note)
    if (
        covered != set(expected_ids)
        or len(decisions) + len(review_issues) != len(expected_ids)
    ):
        raise B1EnrichLocalAuditError(
            "local-audit merged dispositions do not exact-cover chapter components"
        )
    component_index = {
        row["component_id"]: row for row in full_manifest["components"]
    }
    for row in decisions:
        component = component_index.get(row.get("component_id"))
        if (
            component is None
            or row.get("component_kind") != component["component_kind"]
            or row.get("subject_ref") != component["subject_ref"]
            or canonical_hash(row.get("original_proposal"))
            != canonical_hash(component["proposal"])
        ):
            raise B1EnrichLocalAuditError(
                "local-audit merged decision differs from its component"
            )
    for row in review_issues:
        component = component_index.get(row.get("component_id"))
        cited_source_block_ids = row.get("cited_source_block_ids")
        if (
            component is None
            or row.get("row_type") != "local_audit_decision"
            or row.get("state") != "unreviewed"
            or row.get("component_kind") != component["component_kind"]
            or row.get("subject_ref") != component["subject_ref"]
            or row.get("reason") != "decision cites no direct component evidence"
            or row.get("direct_source_block_ids")
            != component["direct_source_block_ids"]
            or not isinstance(cited_source_block_ids, list)
            or not cited_source_block_ids
            or len(cited_source_block_ids) != len(set(cited_source_block_ids))
            or not set(cited_source_block_ids) <= set(
                full_manifest["allowed_source_block_ids"]
            )
            or not isinstance(row.get("raw_row"), Mapping)
            or row["raw_row"].get("component_id") != component["component_id"]
            or cited_source_block_ids != row["raw_row"].get("source_block_ids")
            or set(cited_source_block_ids).intersection(
                component["direct_source_block_ids"]
            )
        ):
            raise B1EnrichLocalAuditError(
                "local-audit merged review issue differs from its component"
            )
    decisions.sort(key=lambda row: row["component_id"])
    review_issues.sort(key=lambda row: row["component_id"])
    observations = _dedupe_unasked_same_referent_observations(observations)
    accepted, revised, rejected, pending, referred = (
        _partition_local_audit_decisions(decisions)
    )
    body = attach_response_normalization_notes_v1(
        {
            "schema_version": ARTIFACT_SCHEMA_VERSION,
            "chapter_id": full_manifest["chapter_id"],
            "request_fingerprint": observed_plan_hash,
            "manifest_hash": full_manifest["manifest_hash"],
            "scan_artifact_hash": full_manifest["scan_artifact_hash"],
            "enrich_artifact_hash": full_manifest["enrich_artifact_hash"],
            "decisions": decisions,
            "review_issues": review_issues,
            "unasked_same_referent_observations": observations,
            "observation_review_issues": observation_review_issues,
            "accepted_components": accepted,
            "revised_components": revised,
            "rejected_components": rejected,
            "pending_components": pending,
            "cross_chapter_referrals": referred,
            "mechanical_noops": full_manifest["mechanical_noops"],
            "quarantined_rows": [
                *deepcopy(full_manifest["quarantined_rows"]),
                *deepcopy(review_issues),
            ],
            "deferred_enrich_rows": full_manifest["deferred_enrich_rows"],
            "identity_authority_granted": False,
            "book_authority_granted": False,
            "registry_mutation_performed": False,
        },
        normalization_notes,
    )
    body["metrics"] = {
        "component_count": len(expected_ids),
        "reviewed_component_count": len(decisions),
        "unreviewed_component_count": len(review_issues),
        "accepted_count": len(accepted),
        "revised_count": len(revised),
        "rejected_count": len(rejected),
        "pending_count": len(pending),
        "cross_chapter_referral_count": len(referred),
        "mechanical_noop_count": len(full_manifest["mechanical_noops"]),
        "quarantined_row_count": len(full_manifest["quarantined_rows"])
        + len(review_issues),
        "review_issue_count": len(review_issues),
        "unasked_same_referent_observation_count": len(observations),
        "observation_review_issue_count": len(observation_review_issues),
        "batch_count": len(raw_batch_rows),
    }
    return {**body, "artifact_hash": canonical_hash(body)}


def validate_b1_enrich_local_audit_response_v1(
    response: Mapping[str, Any],
    *,
    chapter: Mapping[str, Any],
    scan_artifact: Mapping[str, Any],
    enrich_artifact: Mapping[str, Any],
    request_fingerprint: str,
    component_ids: Sequence[str] | None = None,
) -> dict[str, Any]:
    if not isinstance(response, Mapping):
        raise B1EnrichLocalAuditError("Local Auditor response must be an object")
    _exact_keys(
        response,
        {
            "schema_id",
            "chapter_id",
            "manifest_hash",
            "decisions",
            "unasked_same_referent_observations",
        },
        "Local Auditor response",
    )
    if response.get("schema_id") != OUTPUT_SCHEMA_ID:
        raise B1EnrichLocalAuditError("Local Auditor schema_id differs")
    manifest = build_b1_enrich_local_audit_manifest_v1(
        chapter=chapter,
        scan_artifact=scan_artifact,
        enrich_artifact=enrich_artifact,
        component_ids=component_ids,
    )
    response, normalization_notes = normalize_code_owned_response_echoes_v1(
        response,
        expected={"chapter_id": manifest["chapter_id"]},
    )
    if response.get("manifest_hash") != manifest["manifest_hash"]:
        raise B1EnrichLocalAuditError("Local Auditor manifest hash differs")
    raw_decisions = response.get("decisions")
    if not isinstance(raw_decisions, list):
        raise B1EnrichLocalAuditError("decisions must be a list")
    components = {row["component_id"]: row for row in manifest["components"]}
    allowed_refs = set(manifest["allowed_target_refs"])
    decisions: list[dict[str, Any]] = []
    review_issues: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in raw_decisions:
        if not isinstance(raw, Mapping):
            raise B1EnrichLocalAuditError("decision must be an object")
        _exact_keys(
            raw,
            {
                "component_id",
                "action",
                "revised_relation",
                "revised_relation_note",
                "revised_target_ref",
                "source_block_ids",
                "resolution_note",
            },
            "Local Auditor decision",
        )
        component_id = _required_string(raw.get("component_id"), "component_id")
        component = components.get(component_id)
        if component is None or component_id in seen:
            raise B1EnrichLocalAuditError("decision cites a foreign/duplicate component")
        seen.add(component_id)
        kind = component["component_kind"]
        action = _required_string(raw.get("action"), "action")
        if action not in ALLOWED_ACTIONS_BY_KIND[kind]:
            raise B1EnrichLocalAuditError("decision action is invalid for component kind")
        sources = _string_list(
            raw.get("source_block_ids"),
            "decision source_block_ids",
            minimum=1,
            maximum=8,
        )
        allowed_sources = set(manifest["allowed_source_block_ids"])
        if not set(sources) <= allowed_sources:
            raise B1EnrichLocalAuditError("decision cites a block outside the packet")
        if not set(sources).intersection(component["direct_source_block_ids"]):
            review_issues.append(
                {
                    "row_type": "local_audit_decision",
                    "state": "unreviewed",
                    "component_id": component_id,
                    "component_kind": kind,
                    "subject_ref": component["subject_ref"],
                    "reason": "decision cites no direct component evidence",
                    "cited_source_block_ids": list(sources),
                    "direct_source_block_ids": deepcopy(
                        component["direct_source_block_ids"]
                    ),
                    "raw_row": deepcopy(dict(raw)),
                }
            )
            continue
        revised_relation = raw.get("revised_relation")
        revised_relation_note = raw.get("revised_relation_note")
        revised_target_ref = raw.get("revised_target_ref")
        if action == "revise_proposal":
            if kind not in LINK_COMPONENT_KINDS:
                raise B1EnrichLocalAuditError("only a relation proposal may be revised")
            revised_relation = _required_string(revised_relation, "revised_relation")
            family = KINSHIP_RELATIONS if kind == "kinship_link" else LINK_RELATIONS
            if revised_relation not in family:
                review_issues.append(
                    {
                        "row_type": "local_audit_decision",
                        "state": "unreviewed",
                        "component_id": component_id,
                        "component_kind": kind,
                        "subject_ref": component["subject_ref"],
                        "reason": "revised relation crosses its closed family",
                        "cited_source_block_ids": list(sources),
                        "direct_source_block_ids": deepcopy(
                            component["direct_source_block_ids"]
                        ),
                        "raw_row": deepcopy(dict(raw)),
                    }
                )
                continue
            if kind == "entity_link":
                if revised_relation == "other_link":
                    revised_relation_note = _required_string(
                        revised_relation_note,
                        "revised_relation_note",
                    )
                elif revised_relation_note is not None:
                    raise B1EnrichLocalAuditError(
                        "closed entity-link revision must not carry relation_note"
                    )
            elif revised_relation == "other_kin":
                revised_relation_note = _required_string(
                    revised_relation_note,
                    "revised_relation_note",
                )
            elif revised_relation_note is not None:
                raise B1EnrichLocalAuditError(
                    "closed kinship revision must not carry relation_note"
                )
            revised_target_ref = _required_string(
                revised_target_ref, "revised_target_ref"
            )
            if revised_target_ref not in allowed_refs:
                raise B1EnrichLocalAuditError("revised target is outside supplied refs")
        elif (
            revised_relation is not None
            or revised_relation_note is not None
            or revised_target_ref is not None
        ):
            raise B1EnrichLocalAuditError("non-revision action must not carry revision data")
        decisions.append(
            {
                "component_id": component_id,
                "component_kind": kind,
                "subject_ref": component["subject_ref"],
                "action": action,
                "revised_relation": revised_relation,
                "revised_relation_note": revised_relation_note,
                "revised_target_ref": revised_target_ref,
                "source_block_ids": sources,
                "resolution_note": _required_string(
                    raw.get("resolution_note"), "resolution_note"
                ),
                "original_proposal": deepcopy(component["proposal"]),
            }
        )
    if seen != set(components):
        raise B1EnrichLocalAuditError("decisions must exact-cover every component")
    if not decisions:
        raise B1EnrichLocalAuditError(
            "all local-audit decisions failed row validation"
        )
    observations, observation_review_issues = (
        _validate_unasked_same_referent_observations(
            response.get("unasked_same_referent_observations"),
            manifest=manifest,
        )
    )
    decisions.sort(key=lambda row: row["component_id"])
    review_issues.sort(key=lambda row: row["component_id"])

    accepted, revised, rejected, pending, referred = (
        _partition_local_audit_decisions(decisions)
    )

    body = attach_response_normalization_notes_v1(
        {
        "schema_version": ARTIFACT_SCHEMA_VERSION,
        "chapter_id": manifest["chapter_id"],
        "request_fingerprint": _required_string(
            request_fingerprint, "request_fingerprint"
        ),
        "manifest_hash": manifest["manifest_hash"],
        "scan_artifact_hash": manifest["scan_artifact_hash"],
        "enrich_artifact_hash": manifest["enrich_artifact_hash"],
        "decisions": decisions,
        "review_issues": review_issues,
        "unasked_same_referent_observations": observations,
        "observation_review_issues": observation_review_issues,
        "accepted_components": accepted,
        "revised_components": revised,
        "rejected_components": rejected,
        "pending_components": pending,
        "cross_chapter_referrals": referred,
        "mechanical_noops": manifest["mechanical_noops"],
        "quarantined_rows": [
            *deepcopy(manifest["quarantined_rows"]),
            *deepcopy(review_issues),
        ],
        "deferred_enrich_rows": manifest["deferred_enrich_rows"],
        "identity_authority_granted": False,
        "book_authority_granted": False,
        "registry_mutation_performed": False,
        },
        normalization_notes,
    )
    body["metrics"] = {
        "component_count": len(components),
        "reviewed_component_count": len(decisions),
        "unreviewed_component_count": len(review_issues),
        "accepted_count": len(accepted),
        "revised_count": len(revised),
        "rejected_count": len(rejected),
        "pending_count": len(pending),
        "cross_chapter_referral_count": len(referred),
        "mechanical_noop_count": len(manifest["mechanical_noops"]),
        "quarantined_row_count": len(manifest["quarantined_rows"])
        + len(review_issues),
        "review_issue_count": len(review_issues),
        "unasked_same_referent_observation_count": len(observations),
        "observation_review_issue_count": len(observation_review_issues),
    }
    return {**body, "artifact_hash": canonical_hash(body)}


def make_b1_enrich_local_audit_semantic_validator_v1(
    *,
    chapter: Mapping[str, Any],
    scan_artifact: Mapping[str, Any],
    enrich_artifact: Mapping[str, Any],
    rendered: RenderedRegistryRequestV4,
    component_ids: Sequence[str] | None = None,
) -> Callable[[Mapping[str, Any]], Mapping[str, Any]]:
    def validate(payload: Mapping[str, Any]) -> Mapping[str, Any]:
        return validate_b1_enrich_local_audit_response_v1(
            payload,
            chapter=chapter,
            scan_artifact=scan_artifact,
            enrich_artifact=enrich_artifact,
            request_fingerprint=rendered.request_fingerprint,
            component_ids=component_ids,
        )

    return validate


def validate_b1_enrich_local_audit_capability_payload_v1(
    payload: Mapping[str, Any],
    *,
    chapter: Mapping[str, Any],
    scan_artifact: Mapping[str, Any],
    enrich_artifact: Mapping[str, Any],
    request_fingerprint: str,
) -> Mapping[str, Any]:
    """Require one well-formed synthetic decision without prescribing its verdict."""

    artifact = validate_b1_enrich_local_audit_response_v1(
        payload,
        chapter=chapter,
        scan_artifact=scan_artifact,
        enrich_artifact=enrich_artifact,
        request_fingerprint=request_fingerprint,
    )
    metrics = artifact["metrics"]
    disposition_count = sum(
        metrics[key]
        for key in (
            "accepted_count",
            "revised_count",
            "rejected_count",
            "pending_count",
            "cross_chapter_referral_count",
        )
    )
    if (
        metrics["component_count"] != 1
        or disposition_count != 1
        or metrics["mechanical_noop_count"] != 0
        or metrics["quarantined_row_count"] != 0
    ):
        raise B1EnrichLocalAuditError(
            "capability payload is not one valid synthetic disposition"
        )
    return artifact


def _scan_rows(scan_artifact: Mapping[str, Any]) -> list[dict[str, Any]]:
    raw_rows = scan_artifact.get("entity_observations")
    if not isinstance(raw_rows, list):
        raise B1EnrichLocalAuditError("B1-Scan entity observations are malformed")
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in raw_rows:
        if not isinstance(raw, Mapping):
            raise B1EnrichLocalAuditError("B1-Scan entity observation is malformed")
        observation_id = _required_string(raw.get("observation_id"), "observation_id")
        if observation_id in seen:
            raise B1EnrichLocalAuditError("B1-Scan observation id is duplicated")
        seen.add(observation_id)
        rows.append(deepcopy(dict(raw)))
    return rows


def _validate_unasked_same_referent_observations(
    value: Any, *, manifest: Mapping[str, Any]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    if not isinstance(value, list):
        raise B1EnrichLocalAuditError(
            "unasked_same_referent_observations must be a list"
        )
    if len(value) > 8:
        raise B1EnrichLocalAuditError(
            "unasked_same_referent_observations exceeds the batch cap"
        )
    visible_refs = {
        _required_string(row.get("ref"), "entity card ref")
        for row in manifest["entity_cards"]
    }
    supplied_pairs = {
        frozenset(
            (
                row["subject_ref"],
                _required_string(row["proposal"].get("target_ref"), "target_ref"),
            )
        )
        for row in manifest["components"]
        if row["component_kind"] == "same_referent_proposal"
    }
    allowed_blocks = set(manifest["allowed_source_block_ids"])
    rows: list[dict[str, Any]] = []
    issues: list[dict[str, Any]] = []
    seen_pairs: set[frozenset[str]] = set()
    for index, raw in enumerate(value):
        try:
            if not isinstance(raw, Mapping):
                raise B1EnrichLocalAuditError(
                    "unasked same-referent observation must be an object"
                )
            _exact_keys(
                raw,
                {"subject_ref", "target_ref", "source_block_ids", "reason"},
                "unasked same-referent observation",
            )
            left_ref = _required_string(raw.get("subject_ref"), "subject_ref")
            right_ref = _required_string(raw.get("target_ref"), "target_ref")
            if (
                left_ref == right_ref
                or left_ref not in visible_refs
                or right_ref not in visible_refs
            ):
                raise B1EnrichLocalAuditError(
                    "unasked same-referent observation cites an unseen/reflexive ref"
                )
            pair = frozenset((left_ref, right_ref))
            if pair in supplied_pairs:
                raise B1EnrichLocalAuditError(
                    "unasked observation duplicates a supplied component"
                )
            if pair in seen_pairs:
                raise B1EnrichLocalAuditError(
                    "unasked same-referent observation duplicates a pair"
                )
            source_ids = _string_list(
                raw.get("source_block_ids"),
                "unasked observation source_block_ids",
                minimum=1,
                maximum=8,
            )
            if not set(source_ids) <= allowed_blocks:
                raise B1EnrichLocalAuditError(
                    "unasked same-referent observation cites a block outside the packet"
                )
            reason = _required_string(raw.get("reason"), "observation reason")
            if len(reason) > 320:
                raise B1EnrichLocalAuditError(
                    "unasked same-referent observation reason exceeds 320 characters"
                )
            seen_pairs.add(pair)
            rows.append(
                {
                    "left_ref": left_ref,
                    "right_ref": right_ref,
                    "source_block_ids": source_ids,
                    "reason": reason,
                    "lifecycle_state": "proposed_for_next_pass",
                    "identity_authority_granted": False,
                    "applied_in_current_pass": False,
                }
            )
        except (B1EnrichLocalAuditError, ValueError) as exc:
            issues.append(
                {
                    "row_type": "unasked_same_referent_observation",
                    "row_index": index,
                    "reason": str(exc),
                    "raw_row": deepcopy(raw),
                }
            )
    return rows, issues


def _dedupe_unasked_same_referent_observations(
    rows: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in rows:
        if (
            raw.get("identity_authority_granted") is not False
            or raw.get("applied_in_current_pass") is not False
            or raw.get("lifecycle_state") != "proposed_for_next_pass"
        ):
            raise B1EnrichLocalAuditError(
                "unasked same-referent observation grants current authority"
            )
        row = deepcopy(dict(raw))
        key = canonical_hash(
            {
                "refs": sorted((row.get("left_ref"), row.get("right_ref"))),
                "source_block_ids": row.get("source_block_ids"),
                "reason": row.get("reason"),
            }
        )
        if key in seen:
            continue
        seen.add(key)
        result.append(row)
    result.sort(
        key=lambda row: (
            min(row["left_ref"], row["right_ref"]),
            max(row["left_ref"], row["right_ref"]),
            canonical_hash(row),
        )
    )
    return result


def _dossier_card(dossier: Mapping[str, Any], scan: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "ref": dossier["task_ref"],
        "surface": dossier["surface"],
        "referent_kind_claim": dossier["referent_kind_claim"],
        "record_class": scan.get("record_class"),
        "presence_basis": scan.get("presence_basis"),
        "identity_summary": dossier.get("identity_summary"),
        "distinguishing_note": dossier.get("distinguishing_note"),
        "claims": deepcopy(list(dossier.get("claims") or [])),
    }


def _additional_card(dossier: Mapping[str, Any], ref: str) -> dict[str, Any]:
    return {
        "ref": ref,
        "surface": dossier.get("surface"),
        "referent_kind_claim": dossier.get("referent_kind_claim"),
        "record_class": "additional_entity_proposal",
        "presence_basis": None,
        "identity_summary": dossier.get("identity_summary"),
        "distinguishing_note": dossier.get("distinguishing_note"),
        "claims": deepcopy(list(dossier.get("claims") or [])),
    }


def _known_block_ids(
    value: Any,
    block_by_id: Mapping[str, Mapping[str, Any]],
    label: str,
) -> list[str]:
    rows = _string_list(value, label, minimum=1, maximum=8)
    if any(row not in block_by_id for row in rows):
        raise B1EnrichLocalAuditError(f"{label} cites a foreign block")
    return rows


def _name_tokens(value: Any) -> tuple[str, ...]:
    return tuple(re.findall(r"\w+", _normalized_surface(value), flags=re.UNICODE))


def _strict_name_variant_orientation(
    *, left_tokens: Sequence[str], right_tokens: Sequence[str]
) -> str | None:
    if not left_tokens or not right_tokens or left_tokens == right_tokens:
        return None

    def contained(shorter: Sequence[str], longer: Sequence[str]) -> bool:
        width = len(shorter)
        return width < len(longer) and any(
            tuple(longer[index : index + width]) == tuple(shorter)
            for index in range(len(longer) - width + 1)
        )

    if contained(left_tokens, right_tokens):
        return "left"
    if contained(right_tokens, left_tokens):
        return "right"
    return None


def _bounded_pair_evidence_ids(
    left_ids: Sequence[str],
    right_ids: Sequence[str],
    *,
    block_order: Mapping[str, int],
) -> list[str]:
    left = [value for value in left_ids if value in block_order]
    right = [value for value in right_ids if value in block_order]
    if not left or not right:
        raise B1EnrichLocalAuditError(
            "same-referent name proposal lacks evidence on one side"
        )
    selected = [left[0], right[0]]
    remaining = sorted(
        set(left + right) - set(selected),
        key=block_order.__getitem__,
    )
    selected.extend(remaining[: 8 - len(set(selected))])
    return sorted(set(selected), key=block_order.__getitem__)


def _accepted_scope(kind: str) -> str:
    if kind == "alias_proposal":
        return "chapter_confirmed_alias_no_global_authority"
    if kind == "spurious_challenge":
        return "chapter_dormant_with_history"
    if kind == "additional_entity":
        return "chapter_confirmed_entity_no_book_identity_authority"
    if kind == "glossary_ambiguity":
        return "chapter_confirmed_glossary_advisory_only"
    if kind == "same_referent_proposal":
        return "chapter_confirmed_same_referent_no_book_authority"
    return "chapter_confirmed"


__all__ = [
    "ARTIFACT_SCHEMA_VERSION",
    "B1EnrichLocalAuditBatchV1",
    "B1EnrichLocalAuditError",
    "OUTPUT_SCHEMA_ID",
    "PROMPT_ID",
    "ROLE_ID",
    "b1_enrich_local_audit_response_schema_v1",
    "build_b1_enrich_local_audit_manifest_v1",
    "make_b1_enrich_local_audit_semantic_validator_v1",
    "merge_b1_enrich_local_audit_batch_artifacts_v1",
    "plan_b1_enrich_local_audit_batches_v1",
    "render_b1_enrich_local_audit_request_v1",
    "shared_b1_enrich_local_audit_request_v1",
    "validate_b1_enrich_local_audit_capability_payload_v1",
    "validate_b1_enrich_local_audit_response_v1",
]
